"""Agent 持久操作：受理、幂等、worker 租约执行与崩溃恢复（AGC-W10/W11，M1）。

- 受理层 :class:`AgentOperationService`：apply/lifecycle 请求落库（幂等键去重），
  事务提交后才返回受理（HTTP 202 / MCP 正常工具结果）。
- 执行层 :class:`AgentWorker`：manager lifespan 内单 daemon 线程轮询 registry，
  租约认领 + 心跳续约 + 相位推进；连接中断不影响任务推进（§6.3）。
- 恢复层：租约过期或 worker 进程已死的 running/cancelling 操作标记
  ``interrupted``，绝不自动重复 create/restart；终态操作保留 7 天，过期计划
  （未被非终态操作引用）连同行与快照目录清理。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from local_webpage_access.agent.contracts import (
    AgentError,
    AgentErrorCode,
    ApplyDeploymentInput,
    CancelResult,
    LifecycleInput,
    OperationAccepted,
    OperationAction,
    OperationPhase,
    OperationStatus,
    OperationView,
    PlanRecord,
    request_hash,
)
from local_webpage_access.agent.service import (
    AgentService,
    AgentServiceError,
    agent_exc_message,
)
from local_webpage_access.config import Config
from local_webpage_access.errors import (
    BuildError,
    DockerError,
    GitSourceError,
    HostingError,
    LifecycleError,
    LwaError,
    RecognitionError,
    RegistryError,
    ZipImportError,
)
from local_webpage_access.logging import get_logger
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry
from local_webpage_access.registry.dao import TERMINAL_OPERATION_STATUSES

if TYPE_CHECKING:
    from local_webpage_access.agent.auth import Principal

log = get_logger("agent.operations")

#: worker 租约时长与续约间隔（秒）；租约远长于续约间隔，抖动不致误回收
LEASE_SECONDS = 30
LEASE_RENEW_INTERVAL = 5.0
#: 终态操作保留期（§4.2 默认 7 天）
OPERATION_RETENTION_SECONDS = 7 * 24 * 3600
#: 过期清理周期（秒）
SWEEP_INTERVAL_SECONDS = 600.0
#: 队列上限超限时的建议退避
BUSY_RETRY_AFTER_MS = 2000

#: lifecycle 动作的受理 scope（§6.1：start/stop/restart 为 operate；rebuild 为更新）
_LIFECYCLE_SCOPES = {
    OperationAction.start: "instances:operate",
    OperationAction.stop: "instances:operate",
    OperationAction.restart: "instances:operate",
    OperationAction.rebuild: "deploy:update",
}

#: 活跃（非终态）状态集合
_ACTIVE_STATUSES = (
    OperationStatus.queued.value,
    OperationStatus.running.value,
    OperationStatus.cancelling.value,
)

#: 进程内正在执行的操作集合（BUG-691）：``recover_stale`` 以此判定「正在执行」，
#: 不再按 worker_identity 一刀切跳过——同进程身份、租约已过期的悬挂行也必须回收
#: （此前只要身份是自己就永不回收，悬挂 running 占死队列名额）。
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT_OPS: set[str] = set()


def _inflight(operation_id: str) -> bool:
    with _INFLIGHT_LOCK:
        return operation_id in _INFLIGHT_OPS


class _OperationCancelled(Exception):
    """worker 内部：相位边界检测到取消请求。"""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _error_payload(
    code: AgentErrorCode,
    message: str,
    *,
    operation_id: str | None = None,
    detail: dict[str, Any] | None = None,
    next_actions: list[str] | None = None,
) -> dict[str, Any]:
    spec_retryable = {
        AgentErrorCode.quota_exceeded,
        AgentErrorCode.manager_unavailable,
        AgentErrorCode.busy,
    }
    return AgentError(
        code=code,
        message=message,
        detail=detail,
        retryable=code in spec_retryable,
        operationId=operation_id,
        nextActions=next_actions or [],
    ).model_dump(mode="json")


class AgentOperationService:
    """操作受理与查询（不执行；执行由 :class:`AgentWorker` 异步推进）。"""

    def __init__(
        self,
        workspace: Workspace,
        config: Config,
        registry: Registry,
        principal: Principal,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.workspace = workspace
        self.config = config
        self.registry = registry
        self.principal = principal
        self._now = now or _now_utc
        self._service = AgentService(workspace, config, registry, principal, now=now)

    def _require(self, *scopes: str) -> None:
        if not any(scope in self.principal.scopes for scope in scopes):
            raise AgentServiceError(
                "主体无权执行该操作",
                code=AgentErrorCode.permission_denied.value,
            )

    def apply_deployment(self, request: ApplyDeploymentInput) -> OperationAccepted:
        """受理部署：校验计划归属/有效期，落库 queued 操作后返回（§6.3）。"""
        plan = self._service.load_plan(request.planId)
        self._require("deploy:create" if plan.intent == "create" else "deploy:update")
        self._service.assert_plan_fresh(plan)
        if plan.principalId != self.principal.principal_id:
            raise AgentServiceError(
                "计划不属于当前主体",
                code=AgentErrorCode.permission_denied.value,
                planId=plan.planId,
            )
        if plan.workspaceId != self.registry.get_or_create_workspace_id():
            raise AgentServiceError(
                "计划不属于本工作区",
                code=AgentErrorCode.permission_denied.value,
                planId=plan.planId,
            )
        payload = {
            "action": OperationAction.deploy.value,
            "planId": plan.planId,
        }
        digest_payload = {
            **payload,
            "requestHash": plan.requestHash,
            "policyVersion": plan.policyVersion,
            "targetInstanceId": plan.targetInstanceId,
            "expectedRevision": plan.expectedRevision,
        }
        return self._accept(
            action=OperationAction.deploy.value,
            idempotency_key=request.idempotencyKey,
            payload=payload,
            digest_payload=digest_payload,
            plan_id=plan.planId,
            target_instance_id=plan.targetInstanceId,
        )

    def submit_lifecycle(
        self, action: OperationAction, request: LifecycleInput
    ) -> OperationAccepted:
        """受理 start/stop/restart/rebuild：先做 revision 预检（§6.3）。"""
        if action not in _LIFECYCLE_SCOPES:
            raise AgentServiceError(
                f"不支持的操作动作: {action}",
                code=AgentErrorCode.needs_input.value,
            )
        self._require(_LIFECYCLE_SCOPES[action])
        current = self.registry.get_revision(request.instanceId)
        if current is None:
            raise AgentServiceError(
                f"实例 {request.instanceId} 不存在",
                code=AgentErrorCode.needs_input.value,
                instanceId=request.instanceId,
            )
        if current != request.expectedRevision:
            raise AgentServiceError(
                "实例 revision 与期望不符",
                code=AgentErrorCode.revision_conflict.value,
                instanceId=request.instanceId,
                expected=request.expectedRevision,
                actual=current,
            )
        payload = {
            "action": action.value,
            "instanceId": request.instanceId,
            "expectedRevision": request.expectedRevision,
        }
        return self._accept(
            action=action.value,
            idempotency_key=request.idempotencyKey,
            payload=payload,
            digest_payload=payload,
            target_instance_id=request.instanceId,
        )

    def get_operation(self, operation_id: str) -> OperationView:
        """查询操作（校验归属；他主体的操作一律视为不存在，不泄露存在性）。"""
        row = self._owned_row(operation_id)
        view = AgentService._operation_view(row)  # noqa: SLF001
        assert view is not None  # _owned_row 已保证行存在
        return view

    def cancel_operation(self, operation_id: str) -> CancelResult:
        """请求取消：queued 立即取消；running 置 cancelling 并联动构建取消（W11）。

        取消不撤销已完成的文件/容器变更；无法安全取消时返回当前状态，
        不假报已取消（§6.3）。
        """
        row = self._owned_row(operation_id)
        status = str(row["status"])
        now = _iso(self._now())
        if status in TERMINAL_OPERATION_STATUSES:
            return CancelResult(operationId=operation_id, status=OperationStatus(status))
        if status == OperationStatus.queued.value:
            moved = self.registry.cas_agent_operation_status(
                operation_id,
                expected_statuses=(OperationStatus.queued.value,),
                new_status=OperationStatus.cancelled.value,
                now=now,
                error=_error_payload(
                    AgentErrorCode.interrupted,
                    "操作在执行前被取消",
                    operation_id=operation_id,
                ),
            )
            if not moved:
                # BUG-688：读状态与 CAS 之间操作可能已被 worker 认领为
                # running——CAS 未命中时不得假报已取消，重读实际状态如实返回。
                latest = self.registry.get_agent_operation(operation_id)
                latest_status = str(latest["status"]) if latest else status
                return CancelResult(
                    operationId=operation_id, status=OperationStatus(latest_status)
                )
            return CancelResult(
                operationId=operation_id, status=OperationStatus.cancelled
            )
        # running：先置 cancelling，worker 在相位边界收敛为 cancelled
        moved = self.registry.cas_agent_operation_status(
            operation_id,
            expected_statuses=(OperationStatus.running.value,),
            new_status=OperationStatus.cancelling.value,
            now=now,
        )
        if not moved:
            latest = self.registry.get_agent_operation(operation_id)
            latest_status = str(latest["status"]) if latest else status
            return CancelResult(
                operationId=operation_id, status=OperationStatus(latest_status)
            )
        # 构建相位：复用 build token 代次机制真实取消（不杀错 PID）。
        # 取消是请求而非阻塞等待：短超时返回，worker 随后收敛终态。
        if row.get("phase") == OperationPhase.build.value and row.get("target_instance_id"):
            from local_webpage_access.build_queue import get_build_queue

            outcome = get_build_queue(self.config, self.registry).cancel(
                str(row["target_instance_id"]), wait_timeout=3.0
            )
            log.info(
                "操作 %s 构建取消请求结果：%s（%s）",
                operation_id,
                outcome.outcome,
                outcome.message,
            )
        return CancelResult(
            operationId=operation_id, status=OperationStatus.cancelling
        )

    # ---- 内部 ---------------------------------------------------------------

    def _owned_row(self, operation_id: str) -> dict[str, Any]:
        row = self.registry.get_agent_operation(operation_id)
        if row is None or row["principal_id"] != self.principal.principal_id:
            raise AgentServiceError(
                f"操作 {operation_id} 不存在",
                code=AgentErrorCode.needs_input.value,
                operationId=operation_id,
            )
        return row

    def _accept(
        self,
        *,
        action: str,
        idempotency_key: str,
        payload: dict[str, Any],
        digest_payload: dict[str, Any],
        plan_id: str | None = None,
        target_instance_id: str | None = None,
    ) -> OperationAccepted:
        workspace_id = self.registry.get_or_create_workspace_id()
        digest = request_hash(digest_payload)
        # BUG-689：幂等重放必须先于队列上限判定——同键重试不占新名额，
        # 队列满时也应返回原 operationId，而不是 busy。
        existing = self.registry.get_agent_operation_by_idempotency(
            self.principal.principal_id, workspace_id, idempotency_key
        )
        if existing is not None:
            if existing["request_hash"] != digest:
                raise AgentServiceError(
                    "幂等键已绑定不同请求内容",
                    code=AgentErrorCode.idempotency_conflict.value,
                    idempotency_key=idempotency_key,
                )
            log.info(
                "幂等重放操作 %s（action=%s，status=%s）",
                existing["operation_id"],
                action,
                existing["status"],
            )
            return OperationAccepted(
                operationId=str(existing["operation_id"]),
                status=OperationStatus(str(existing["status"])),
                instanceId=existing.get("target_instance_id"),
            )
        pending = self.registry.count_agent_operations(statuses=_ACTIVE_STATUSES)
        if pending >= self.config.agent.maxPendingOperations:
            raise AgentServiceError(
                f"待执行操作已达上限 {self.config.agent.maxPendingOperations}，请稍后重试",
                code=AgentErrorCode.busy.value,
                retryAfterMs=BUSY_RETRY_AFTER_MS,
            )
        now = _iso(self._now())
        try:
            record = self.registry.create_agent_operation(
                {
                    "operation_id": f"op_{uuid.uuid4().hex}",
                    "principal_id": self.principal.principal_id,
                    "workspace_id": workspace_id,
                    "action": action,
                    "target_instance_id": target_instance_id,
                    "request_hash": digest,
                    "idempotency_key": idempotency_key,
                    "plan_id": plan_id,
                    "status": OperationStatus.queued.value,
                    "phase": None,
                    "created_at": now,
                    "updated_at": now,
                    "payload": payload,
                }
            )
        except RegistryError as exc:
            if exc.code == "idempotency_conflict":
                raise AgentServiceError(
                    "幂等键已绑定不同请求内容",
                    code=AgentErrorCode.idempotency_conflict.value,
                    **exc.context,
                ) from exc
            raise
        log.info(
            "已受理操作 %s（action=%s，target=%s）",
            record["operation_id"],
            action,
            target_instance_id,
        )
        return OperationAccepted(
            operationId=str(record["operation_id"]),
            status=OperationStatus(str(record["status"])),
            instanceId=record.get("target_instance_id"),
        )


class AgentWorker:
    """持久操作执行 worker：manager lifespan 内的单 daemon 线程。

    租约认领 + 心跳续约保证：误启多个 manager 时同一操作只有一个执行者；
    执行不占 FastAPI 事件循环；构建并发仍走既有 build gate。
    """

    def __init__(
        self,
        workspace: Workspace,
        config: Config,
        registry: Registry,
        *,
        now: Callable[[], datetime] | None = None,
        poll_interval: float = 0.5,
        lease_seconds: int = LEASE_SECONDS,
    ) -> None:
        from local_webpage_access.build_process import (
            owner_process_identity,
            worker_identity_token,
        )

        self.workspace = workspace
        self.config = config
        self.registry = registry
        self._now = now or _now_utc
        self.poll_interval = poll_interval
        self.lease_seconds = lease_seconds
        self.identity = f"{os.getpid()}:{worker_identity_token(owner_process_identity())}"
        self._active_heartbeat: _LeaseHeartbeat | None = None

    # ---- 主循环 ---------------------------------------------------------------

    def run_forever(self, stop_event: threading.Event) -> None:
        """worker 主循环：恢复 → 认领 → 执行；空闲时等待并周期清理。"""
        last_sweep = 0.0
        while not stop_event.is_set():
            try:
                recovered = self.recover_stale()
                if recovered:
                    log.info("回收了 %d 个中断操作", recovered)
            except Exception:  # noqa: BLE001 — 恢复失败不阻断后续认领
                log.exception("操作恢复扫描失败")
            record = None
            try:
                record = self.claim_next()
            except Exception:  # noqa: BLE001
                log.exception("认领操作失败")
            if record is not None:
                self._execute(record)
                continue
            if time.monotonic() - last_sweep >= SWEEP_INTERVAL_SECONDS:
                last_sweep = time.monotonic()
                try:
                    self.sweep()
                except Exception:  # noqa: BLE001
                    log.exception("操作/计划过期清理失败")
            stop_event.wait(self.poll_interval)

    def claim_next(self) -> dict[str, Any] | None:
        now = self._now()
        return self.registry.claim_next_agent_operation(
            worker_identity=self.identity,
            now=_iso(now),
            lease_until=_iso(now + timedelta(seconds=self.lease_seconds)),
        )

    # ---- 执行 -----------------------------------------------------------------

    def _execute(self, record: dict[str, Any]) -> None:
        op_id = str(record["operation_id"])
        with _INFLIGHT_LOCK:
            _INFLIGHT_OPS.add(op_id)
        heartbeat = _LeaseHeartbeat(self, op_id)
        heartbeat.start()
        try:
            if record["action"] == OperationAction.deploy.value:
                self._run_deploy(record)
            else:
                self._run_lifecycle(record)
        except _OperationCancelled:
            self._finish(
                op_id,
                status=OperationStatus.cancelled.value,
                error=_error_payload(
                    AgentErrorCode.interrupted,
                    "操作已被取消（已完成的文件/容器变更不回滚）",
                    operation_id=op_id,
                ),
            )
        except AgentServiceError as exc:
            self._finish_agent_error(op_id, exc)
        except LwaError as exc:
            self._finish_lwa_error(op_id, exc, current_phase=heartbeat.phase)
        except Exception as exc:  # noqa: BLE001 — 未知异常不得击穿 worker 循环
            log.exception("操作 %s 执行出现未知异常", op_id)
            self._finish(
                op_id,
                status=OperationStatus.failed.value,
                error=_error_payload(
                    AgentErrorCode.interrupted,
                    f"操作执行出现内部错误: {exc}",
                    operation_id=op_id,
                    next_actions=["查看 manager.log 后重试"],
                ),
            )
        finally:
            heartbeat.stop()
            with _INFLIGHT_LOCK:
                _INFLIGHT_OPS.discard(op_id)

    def _run_deploy(self, record: dict[str, Any]) -> None:
        from local_webpage_access.importer import Importer
        from local_webpage_access.lifecycle import (
            FallbackConfirmationRequired,
            rebuild_instance,
            restart_instance,
            start_instance,
        )

        op_id = str(record["operation_id"])
        heartbeat = self._current_heartbeat
        plan = self._load_plan_for_exec(record)

        # 相位 validate：计划有效期与目标 revision 复检
        heartbeat.set_phase(OperationPhase.validate.value)
        self._abort_if_cancelling(op_id)
        try:
            self._service().assert_plan_fresh(plan)
        except AgentServiceError as exc:
            self._finish(
                op_id,
                status=OperationStatus.needs_input.value,
                error=_error_payload(
                    AgentErrorCode.needs_input,
                    agent_exc_message(exc),
                    operation_id=op_id,
                    next_actions=["重新生成部署计划后再 apply"],
                ),
            )
            return
        target = plan.targetInstanceId
        if plan.intent == "update":
            assert target is not None
            current = self.registry.get_revision(target)
            if current is None:
                raise AgentServiceError(
                    f"目标实例 {target} 不存在",
                    code=AgentErrorCode.needs_input.value,
                    instanceId=target,
                )
            if current != plan.expectedRevision:
                raise AgentServiceError(
                    "实例 revision 与计划不符",
                    code=AgentErrorCode.revision_conflict.value,
                    instanceId=target,
                    expected=plan.expectedRevision,
                    actual=current,
                )
            # BUG-690 双保险：计划期已拒绝 git 目标更新（service.plan_deployment），
            # 此处兜底旧计划/竞态路径，避免受理后在 import 相位撞 update_zip 硬拒。
            target_manifest = self.workspace.app_manifest_path(target)
            if target_manifest.is_file():
                from local_webpage_access.models import InstanceManifest

                existing = InstanceManifest.load(target_manifest)
                if getattr(existing, "sourceKind", "zip") == "git":
                    raise AgentServiceError(
                        f"实例 {target} 是 GitHub 源实例，Agent 通道不支持 zip 更新；"
                        f"请用 `lwa import --from-git --update {target}`"
                        " 或管理页 update-from-git 流程",
                        code=AgentErrorCode.needs_input.value,
                        instanceId=target,
                    )
        snapshot = self._service().plan_snapshot_path(plan.planId)
        if not snapshot.is_dir():
            raise AgentServiceError(
                "计划快照不存在（可能已被清理），请重新计划",
                code=AgentErrorCode.needs_input.value,
                planId=plan.planId,
            )

        # 相位 import：快照视为不可变制品打包 zip 复用 import_zip/update_zip
        # （import_from_dir 会拒绝工作区内部目录——那是防用户误导运行根的护栏，
        # 快照是 plan 期固化的只读副本，走 zip 通道语义正确）。
        self._advance(op_id, OperationPhase.import_.value)
        self._abort_if_cancelling(op_id)
        from local_webpage_access.folder_source import pack_source_dir

        importer = Importer(self.workspace, self.config, self.registry)
        fd, tmp_zip_path = tempfile.mkstemp(suffix=".zip", prefix="lwa-agent-deploy-")
        os.close(fd)
        tmp_zip = Path(tmp_zip_path)
        try:
            pack_source_dir(snapshot, dest_zip=tmp_zip)
            if plan.intent == "create":
                imported = importer.import_zip(
                    tmp_zip,
                    name=plan.displayName or self._default_instance_name(plan),
                )
                instance_id = imported.instance_id
                # 导入创建时持久关联 operationId ↔ instanceId（崩溃恢复依据）
                self.registry.update_agent_operation(
                    op_id, updated_at=_iso(self._now()), target_instance_id=instance_id
                )
                needs_rebuild = False
                needs_restart = False
                was_running = False
            else:
                assert target is not None
                updated = importer.update_zip(
                    tmp_zip,
                    target,
                    restart=True,
                    yes=True,
                )
                instance_id = target
                needs_rebuild = updated.needs_rebuild
                needs_restart = updated.needs_restart
                was_running = updated.was_running
        except FallbackConfirmationRequired as exc:
            self._finish(
                op_id,
                status=OperationStatus.needs_input.value,
                error=_error_payload(
                    AgentErrorCode.needs_input,
                    agent_exc_message(exc),
                    operation_id=op_id,
                    next_actions=["确认降级计划后以新计划重试"],
                ),
            )
            return
        finally:
            with contextlib.suppress(OSError):
                tmp_zip.unlink(missing_ok=True)

        # BUG-690：内容经 zip 通道部署后，把源身份按计划写回 manifest——
        # git/folder 源实例不再静默变成 zip 身份，CLI/管理页按原源更新才能对上。
        identity_warnings = self._writeback_source_identity(plan, instance_id, snapshot)

        # 相位 build/start：容器实例重建或重启
        self._abort_if_cancelling(op_id)
        try:
            if plan.intent == "create":
                self._advance(op_id, OperationPhase.build.value)
                start_instance(self.workspace, self.config, self.registry, instance_id)
            elif needs_rebuild:
                self._advance(op_id, OperationPhase.build.value)
                self._abort_if_cancelling(op_id)
                rebuild_instance(self.workspace, self.config, self.registry, instance_id)
            elif needs_restart and was_running:
                self._advance(op_id, OperationPhase.start.value)
                self._abort_if_cancelling(op_id)
                restart_instance(self.workspace, self.config, self.registry, instance_id)
        except FallbackConfirmationRequired as exc:
            self._finish(
                op_id,
                status=OperationStatus.needs_input.value,
                error=_error_payload(
                    AgentErrorCode.needs_input,
                    agent_exc_message(exc),
                    operation_id=op_id,
                    next_actions=["确认降级计划后以新计划重试"],
                ),
            )
            return

        # 相位 healthcheck：读回实例状态与访问地址组装结果
        self._advance(op_id, OperationPhase.healthcheck.value)
        self._abort_if_cancelling(op_id)
        self._finish_deploy_success(op_id, instance_id, warnings=identity_warnings)

    def _run_lifecycle(self, record: dict[str, Any]) -> None:
        from local_webpage_access.lifecycle import (
            rebuild_instance,
            restart_instance,
            start_instance,
            stop_instance_op,
        )

        op_id = str(record["operation_id"])
        action = str(record["action"])
        payload = record.get("payload") or {}
        instance_id = str(record["target_instance_id"])
        expected = payload.get("expectedRevision")

        current = self.registry.get_revision(instance_id)
        if current is None:
            raise AgentServiceError(
                f"实例 {instance_id} 不存在",
                code=AgentErrorCode.needs_input.value,
                instanceId=instance_id,
            )
        if expected is not None and current != int(expected):
            raise AgentServiceError(
                "实例 revision 与期望不符",
                code=AgentErrorCode.revision_conflict.value,
                instanceId=instance_id,
                expected=int(expected),
                actual=current,
            )
        self._abort_if_cancelling(op_id)
        if action == OperationAction.rebuild.value:
            self._advance(op_id, OperationPhase.build.value)
        else:
            self._advance(op_id, OperationPhase.start.value)
        handlers = {
            OperationAction.start.value: start_instance,
            OperationAction.stop.value: stop_instance_op,
            OperationAction.restart.value: restart_instance,
            OperationAction.rebuild.value: rebuild_instance,
        }
        handler = handlers.get(action)
        if handler is None:
            raise AgentServiceError(
                f"不支持的操作动作: {action}",
                code=AgentErrorCode.needs_input.value,
            )
        handler(self.workspace, self.config, self.registry, instance_id)
        self._abort_if_cancelling(op_id)
        self._finish(
            op_id,
            status=OperationStatus.succeeded.value,
            result={
                "instanceId": instance_id,
                "revision": self.registry.get_revision(instance_id),
                "warnings": [],
                "nextActions": [],
            },
        )

    # ---- 恢复与清理（W11） ------------------------------------------------------

    def recover_stale(self) -> int:
        """回收租约过期或 worker 进程已死的活跃操作 → interrupted（不自动重试）。"""
        from local_webpage_access.build_queue import _pid_alive

        recovered = 0
        now_iso = _iso(self._now())
        for row in self.registry.list_agent_operations(
            statuses=(OperationStatus.running.value, OperationStatus.cancelling.value)
        ):
            op_id = str(row["operation_id"])
            if _inflight(op_id):
                # 本进程正在执行（含 claim 后未启动心跳前的窗口）不回收；
                # 心跳线程会持续续约。BUG-691：不再按 worker_identity 跳过——
                # 身份是本进程但不在执行的悬挂行，租约过期后同样回收。
                continue
            lease = row.get("lease_until")
            pid = _identity_pid(row.get("worker_identity"))
            stale = lease is None or str(lease) <= now_iso or (pid is not None and not _pid_alive(pid))
            if not stale:
                continue
            self.registry.cas_agent_operation_status(
                str(row["operation_id"]),
                expected_statuses=(
                    OperationStatus.running.value,
                    OperationStatus.cancelling.value,
                ),
                new_status=OperationStatus.interrupted.value,
                now=now_iso,
                error=_error_payload(
                    AgentErrorCode.interrupted,
                    "操作执行中断（worker 租约过期或进程退出），结果未知",
                    operation_id=str(row["operation_id"]),
                    next_actions=[
                        "用 lwa_get_instance 核对实例现状",
                        "确认后以新幂等键重新提交（不自动重复 create/restart）",
                    ],
                ),
            )
            recovered += 1
        return recovered

    def sweep(self) -> None:
        """过期清理：终态操作 7 天保留；过期且未被活跃操作引用的计划连快照删除。"""
        now = self._now()
        cutoff = _iso(now - timedelta(seconds=OPERATION_RETENTION_SECONDS))
        removed = self.registry.sweep_terminal_agent_operations(before=cutoff)
        if removed:
            log.info("清理终态操作 %d 条", removed)
        for plan_id in self.registry.delete_expired_agent_plans(now=_iso(now)):
            plan_dir = self.workspace.root / "run" / "agent-plans" / plan_id
            shutil.rmtree(plan_dir, ignore_errors=True)

    # ---- 内部 -----------------------------------------------------------------

    @property
    def _current_heartbeat(self) -> "_LeaseHeartbeat":
        hb = self._active_heartbeat
        assert hb is not None, "执行期必须有活跃心跳"
        return hb

    def _service(self, record: dict[str, Any] | None = None) -> AgentService:
        from local_webpage_access.agent.auth import (
            M1_LOCAL_OWNER_SCOPES,
            Principal,
        )

        principal_id = "local-owner"
        if record is not None:
            principal_id = str(record.get("principal_id") or principal_id)
        return AgentService(
            self.workspace,
            self.config,
            self.registry,
            Principal(principal_id=principal_id, kind="local_owner", scopes=M1_LOCAL_OWNER_SCOPES),
            now=self._now,
        )

    def _load_plan_for_exec(self, record: dict[str, Any]) -> PlanRecord:
        plan_id = record.get("plan_id")
        if not plan_id:
            raise AgentServiceError(
                "操作缺少关联计划",
                code=AgentErrorCode.needs_input.value,
                operationId=str(record["operation_id"]),
            )
        return self._service(record).load_plan(str(plan_id))

    def _advance(self, op_id: str, phase: str) -> None:
        self.registry.update_agent_operation(
            op_id, updated_at=_iso(self._now()), phase=phase
        )
        self._current_heartbeat.set_phase(phase)

    def _abort_if_cancelling(self, op_id: str) -> None:
        row = self.registry.get_agent_operation(op_id)
        if row is not None and row["status"] == OperationStatus.cancelling.value:
            raise _OperationCancelled()

    def _finish(
        self,
        op_id: str,
        *,
        status: str,
        error: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        instance_id: str | None = None,
    ) -> None:
        ok = self.registry.cas_agent_operation_status(
            op_id,
            expected_statuses=(
                OperationStatus.running.value,
                OperationStatus.cancelling.value,
            ),
            new_status=status,
            now=_iso(self._now()),
            error=error,
            result=result,
            target_instance_id=instance_id,
        )
        if not ok:
            log.warning("操作 %s 收尾 CAS 未命中（状态已被接管），保持现状", op_id)

    def _finish_agent_error(self, op_id: str, exc: AgentServiceError) -> None:
        try:
            code = AgentErrorCode(str(exc.code))
        except ValueError:
            code = AgentErrorCode.interrupted
        status = (
            OperationStatus.needs_input.value
            if code == AgentErrorCode.needs_input
            else OperationStatus.failed.value
        )
        self._finish(
            op_id,
            status=status,
            error=_error_payload(
                code,
                agent_exc_message(exc),
                operation_id=op_id,
                detail=dict(exc.context) if exc.context else None,
            ),
        )

    def _finish_lwa_error(
        self, op_id: str, exc: LwaError, *, current_phase: str | None
    ) -> None:
        if isinstance(exc, (BuildError, DockerError)):
            code = AgentErrorCode.build_failed
        elif isinstance(exc, HostingError) and current_phase in (
            OperationPhase.start.value,
            OperationPhase.healthcheck.value,
        ):
            code = AgentErrorCode.healthcheck_failed
        elif isinstance(exc, (ZipImportError, RecognitionError, GitSourceError)):
            code = AgentErrorCode.needs_input
        elif isinstance(exc, LifecycleError) and "已取消" in str(exc):
            self._finish(
                op_id,
                status=OperationStatus.cancelled.value,
                error=_error_payload(
                    AgentErrorCode.interrupted,
                    agent_exc_message(exc),
                    operation_id=op_id,
                ),
            )
            return
        else:
            code = AgentErrorCode.interrupted
        status = (
            OperationStatus.needs_input.value
            if code == AgentErrorCode.needs_input
            else OperationStatus.failed.value
        )
        self._finish(
            op_id,
            status=status,
            error=_error_payload(code, agent_exc_message(exc), operation_id=op_id),
        )

    def _finish_deploy_success(
        self, op_id: str, instance_id: str, *, warnings: list[str] | None = None
    ) -> None:
        from local_webpage_access.agent.contracts import GetAccessUrlsInput

        service = self._service()
        urls = service.get_access_urls(GetAccessUrlsInput(instanceId=instance_id))
        row = self.registry.get_instance(instance_id) or {}
        result = {
            "instanceId": instance_id,
            "revision": self.registry.get_revision(instance_id),
            "access": [entry.model_dump(mode="json") for entry in urls.urls],
            "warnings": warnings or [],
            "nextActions": [],
        }
        status = str(row.get("status") or "")
        if status == "failed":
            self._finish(
                op_id,
                status=OperationStatus.failed.value,
                error=_error_payload(
                    AgentErrorCode.healthcheck_failed,
                    f"实例 {instance_id} 部署后健康检查未通过",
                    operation_id=op_id,
                    detail={"instanceId": instance_id},
                    next_actions=["用 lwa_get_logs 查看构建/运行日志"],
                ),
                result=result,
                instance_id=instance_id,
            )
            return
        self._finish(
            op_id,
            status=OperationStatus.succeeded.value,
            result=result,
            instance_id=instance_id,
        )

    def _writeback_source_identity(
        self, plan: PlanRecord, instance_id: str, snapshot: Path
    ) -> list[str]:
        """BUG-690：apply 后把源身份写回 manifest（zip 内容通道 + 源身份一致）。

        - ``server_directory`` → ``sourceKind=folder`` + ``sourceDirPath``（受控
          根内真实路径）+ ``sourceSyncHash``（快照指纹，后续 ``update-from-dir``
          的无变更短路据此判定）；
        - ``git`` → §17.2.1 git 身份（url/ref/refKind/commit/subdir 来自计划
          快照期解析，见 ``AgentService._snapshot_git``）。

        目录源在 apply 期越出 ``allowedSourceRoots`` 或已消失时**保留 zip 身份
        并返回警告**（内容部署本身成功，不回滚——与 git 导入路径的半成品补偿
        删除不同：这里不是导入中断，只是身份降级为 zip）。
        """
        from local_webpage_access.agent.auth import AgentAuthError, validate_source_root
        from local_webpage_access.agent.contracts import GitSource, ServerDirectorySource
        from local_webpage_access.models import InstanceManifest

        if not isinstance(plan.source, (ServerDirectorySource, GitSource)):
            return []
        manifest_path = self.workspace.app_manifest_path(instance_id)
        if not manifest_path.is_file():
            return []
        manifest = InstanceManifest.load(manifest_path)
        if isinstance(plan.source, ServerDirectorySource):
            try:
                real = validate_source_root(
                    plan.source.path, self.config.agent.allowedSourceRoots
                )
            except AgentAuthError as exc:
                return [f"源目录在 apply 期不可用，保留 zip 源身份：{exc.message}"]
            from local_webpage_access.folder_source import compute_source_hash

            sync_hash = compute_source_hash(snapshot)
            manifest.sourceKind = "folder"
            manifest.sourceDirPath = str(real)
            manifest.sourceSyncHash = sync_hash
            # 切源场景清 git 身份残留（与 update_from_dir 的清理对齐）
            manifest.sourceGitUrl = None
            manifest.sourceGitRef = None
            manifest.sourceGitRefKind = None
            manifest.sourceGitCommit = None
            manifest.sourceGitSubdir = None
            note = f"Agent 部署写回文件夹源身份：{real}（指纹 {sync_hash[:12]}）"
        else:
            commit = plan.source.commit or (plan.sourceDigest.partition(":")[0] or None)
            manifest.sourceKind = "git"
            manifest.sourceDirPath = None
            manifest.sourceSyncHash = None
            manifest.sourceGitUrl = plan.source.url
            manifest.sourceGitRef = plan.source.ref
            manifest.sourceGitRefKind = plan.source.refKind or "branch"
            manifest.sourceGitCommit = commit
            manifest.sourceGitSubdir = plan.source.subdir
            note = (
                f"Agent 部署写回 git 源身份：{plan.source.url}"
                f"（{manifest.sourceGitRefKind} {plan.source.ref or '默认'}）"
            )
        manifest.touch()
        manifest.save(manifest_path)
        self.registry.add_event(instance_id, "update", note)
        return []

    @staticmethod
    def _default_instance_name(plan: PlanRecord) -> str | None:
        """计划未给显示名时从源推导实例名（快照目录恒为 ``snapshot``，不可用）。"""
        source = plan.source
        path = getattr(source, "path", None)
        if path:
            return str(path).rstrip("/").rsplit("/", 1)[-1] or None
        url = getattr(source, "url", None)
        if url:
            tail = str(url).rstrip("/").rsplit("/", 1)[-1]
            return tail.removesuffix(".git") or None
        return None


class _LeaseHeartbeat(threading.Thread):
    """执行期租约心跳：续约 + 构建相位时同步 build token（W11 关联）。"""

    def __init__(self, worker: AgentWorker, operation_id: str) -> None:
        super().__init__(name=f"lwa-agent-lease-{operation_id[:12]}", daemon=True)
        self._worker = worker
        self._operation_id = operation_id
        self._stop_event = threading.Event()
        self.phase: str | None = None
        worker._active_heartbeat = self

    def set_phase(self, phase: str) -> None:
        self.phase = phase

    def stop(self) -> None:
        self._stop_event.set()
        if self.ident is not None:
            self.join(timeout=2.0)
        if self._worker._active_heartbeat is self:  # noqa: SLF001
            self._worker._active_heartbeat = None  # noqa: SLF001

    def run(self) -> None:
        worker = self._worker
        while not self._stop_event.wait(LEASE_RENEW_INTERVAL):
            now = worker._now()  # noqa: SLF001
            try:
                worker.registry.renew_agent_operation_lease(
                    self._operation_id,
                    worker_identity=worker.identity,
                    now=_iso(now),
                    lease_until=_iso(now + timedelta(seconds=worker.lease_seconds)),
                )
                self._sync_build_token()
            except Exception:  # noqa: BLE001 — 心跳失败由租约过期恢复兜底
                log.debug("操作 %s 租约心跳失败", self._operation_id, exc_info=True)

    def _sync_build_token(self) -> None:
        if self.phase != OperationPhase.build.value:
            return
        row = self._worker.registry.get_agent_operation(self._operation_id)
        if row is None or row.get("build_token"):
            return
        instance_id = row.get("target_instance_id")
        if not instance_id:
            return
        from local_webpage_access.build_queue import get_build_queue

        token = get_build_queue(self._worker.config, self._worker.registry).current_build_token(
            str(instance_id)
        )
        if token:
            self._worker.registry.update_agent_operation(
                self._operation_id,
                updated_at=_iso(self._worker._now()),  # noqa: SLF001
                build_token=token,
            )


def _identity_pid(identity: str | None) -> int | None:
    """从 worker_identity（``<pid>:<token>``）解析 PID；无法解析返回 None。"""
    if not identity:
        return None
    head, _, _rest = str(identity).partition(":")
    try:
        return int(head)
    except ValueError:
        return None
