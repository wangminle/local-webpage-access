"""Agent 查询、能力映射与部署计划（AGC-W07/W08，M1）。

本模块只做只读查询与「计划」：不启动实例、不跑用户构建脚本。
能力读取 ``run/capability-manager.json`` 缓存，禁止同步全量探测。
实例内容变更的 revision CAS 见 :func:`claim_content_revision`（W09）。
"""

from __future__ import annotations

import base64
import json
import shutil
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from local_webpage_access.agent.auth import (
    AgentAuthError,
    Principal,
    validate_source_root,
)
from local_webpage_access.agent.contracts import (
    AGENT_CONTRACT_VERSION,
    PLAN_TTL_SECONDS,
    AccessUrlEntry,
    AccessUrlsResult,
    AgentErrorCode,
    ArtifactSource,
    CapabilitiesResult,
    DeploySource,
    GetAccessUrlsInput,
    GitSource,
    InstanceDetail,
    InstanceListResult,
    InstanceSummary,
    ListInstancesInput,
    OperationAction,
    OperationPhase,
    OperationStatus,
    OperationView,
    PlanDeploymentInput,
    PlanRecord,
    PlanRisks,
    ServerDirectorySource,
    canonical_json,
    request_hash,
)
from local_webpage_access.config import Config
from local_webpage_access.errors import GitSourceError, LwaError, RegistryError
from local_webpage_access.folder_source import compute_source_hash
from local_webpage_access.logging import get_logger
from local_webpage_access.models import InstanceManifest, Runtime
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry

log = get_logger("agent.service")

#: M1 策略版本（与产品版本独立；apply 校验此值）
PLAN_POLICY_VERSION = "1"

#: M1 可计划的输入类型（artifact 属 M2，不出现在 capabilities.inputTypes）
M1_INPUT_TYPES = ["server_directory", "git"]

_GIT_SOURCE_DENIED = frozenset(
    {"invalid_url", "host_not_allowed", "userinfo_forbidden", "source_mismatch"}
)


class AgentServiceError(LwaError):
    """Agent 查询/计划失败；``code`` 取契约错误码。"""


def _rfc3339(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def claim_content_revision(registry: Registry, instance_id: str, expected: int) -> int:
    """内容/配置变更 CAS：期望 revision 不匹配则 ``revision_conflict``。"""
    try:
        return registry.cas_increment_revision(instance_id, expected)
    except RegistryError as exc:
        if exc.code == "revision_conflict":
            raise AgentServiceError(
                "实例 revision 与期望不符",
                code=AgentErrorCode.revision_conflict.value,
                **exc.context,
            ) from exc
        raise


def _encode_cursor(instance_id: str) -> str:
    return base64.urlsafe_b64encode(instance_id.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> str:
    try:
        text = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise AgentServiceError("分页 cursor 无效", code=AgentErrorCode.needs_input.value) from exc
    if not text:
        raise AgentServiceError("分页 cursor 无效", code=AgentErrorCode.needs_input.value)
    return text


class AgentService:
    """本机 owner 的查询与计划服务（不执行 apply / 生命周期）。"""

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

    def plan_snapshot_path(self, plan_id: str) -> Path:
        return self.workspace.root / "run" / "agent-plans" / plan_id / "snapshot"

    def _require(self, *scopes: str) -> None:
        if not any(scope in self.principal.scopes for scope in scopes):
            raise AgentServiceError(
                "主体无权执行该操作",
                code=AgentErrorCode.permission_denied.value,
            )

    def get_capabilities(self) -> CapabilitiesResult:
        self._require("instances:read", "deploy:create", "deploy:update")
        runtime, observed_at = self._runtime_from_cache()
        return CapabilitiesResult(
            workspaceId=self.registry.get_or_create_workspace_id(),
            contractVersion=AGENT_CONTRACT_VERSION,
            inputTypes=list(M1_INPUT_TYPES),
            runtime=runtime,
            observedAt=observed_at,
        )

    def list_instances(self, request: ListInstancesInput) -> InstanceListResult:
        self._require("instances:read")
        rows = sorted(self.registry.list_instances(), key=lambda row: str(row["id"]))
        after = _decode_cursor(request.cursor) if request.cursor else None
        if after is not None:
            rows = [row for row in rows if str(row["id"]) > after]
        window = rows[: request.limit]
        summaries = [self._summary_from_row(row) for row in window]
        next_cursor = None
        if len(rows) > request.limit and window:
            next_cursor = _encode_cursor(str(window[-1]["id"]))
        return InstanceListResult(instances=summaries, nextCursor=next_cursor)

    def get_instance(self, instance_id: str) -> InstanceDetail:
        self._require("instances:read")
        row = self.registry.get_instance(instance_id)
        if row is None:
            raise AgentServiceError(
                f"实例 {instance_id} 不存在",
                code=AgentErrorCode.needs_input.value,
                instanceId=instance_id,
            )
        summary = self._summary_from_row(row)
        recent = self._operation_view(
            self.registry.get_latest_agent_operation_for_instance(instance_id)
        )
        return InstanceDetail(
            instanceId=summary.instanceId,
            name=summary.name,
            kind=summary.kind,
            runtime=summary.runtime,
            status=summary.status,
            revision=summary.revision,
            desiredState=str(row.get("desired_state") or "stopped"),
            updatedAt=str(row.get("updated_at") or _rfc3339(self._now())),
            recentOperation=recent,
        )

    def get_access_urls(self, request: GetAccessUrlsInput) -> AccessUrlsResult:
        self._require("instances:read")
        row = self.registry.get_instance(request.instanceId)
        if row is None:
            raise AgentServiceError(
                f"实例 {request.instanceId} 不存在",
                code=AgentErrorCode.needs_input.value,
                instanceId=request.instanceId,
            )
        manifest_path = self.workspace.app_manifest_path(request.instanceId)
        if not manifest_path.is_file():
            raise AgentServiceError(
                f"实例 {request.instanceId} 缺少 manifest",
                code=AgentErrorCode.needs_input.value,
                instanceId=request.instanceId,
            )
        manifest = InstanceManifest.load(manifest_path)
        observed = (
            row.get("last_observed_at")
            or row.get("last_health_check_at")
            or row.get("updated_at")
            or _rfc3339(self._now())
        )
        urls: list[AccessUrlEntry] = []
        network = manifest.network
        host_port = network.hostPort if network is not None else None
        localhost = f"http://127.0.0.1:{host_port}/" if host_port else None
        lan = network.lanUrl if network is not None else None
        wanted = request.perspective
        if localhost and wanted in (None, "localhost"):
            urls.append(
                AccessUrlEntry(
                    url=localhost,
                    audience="localhost",
                    serverProbe="unknown",
                    clientReachability="unknown",
                    observedAt=str(observed),
                )
            )
        if lan and wanted in (None, "lan"):
            urls.append(
                AccessUrlEntry(
                    url=lan,
                    audience="lan",
                    serverProbe="unknown",
                    clientReachability="unknown",
                    observedAt=str(observed),
                )
            )
        return AccessUrlsResult(instanceId=request.instanceId, urls=urls)

    def plan_deployment(self, request: PlanDeploymentInput) -> PlanRecord:
        if request.intent == "create":
            self._require("deploy:create")
        else:
            self._require("deploy:update")
        if request.intent == "update":
            assert request.targetInstanceId is not None
            assert request.expectedRevision is not None
            current = self.registry.get_revision(request.targetInstanceId)
            if current is None:
                raise AgentServiceError(
                    f"目标实例 {request.targetInstanceId} 不存在",
                    code=AgentErrorCode.needs_input.value,
                    instanceId=request.targetInstanceId,
                )
            if current != request.expectedRevision:
                raise AgentServiceError(
                    "实例 revision 与期望不符",
                    code=AgentErrorCode.revision_conflict.value,
                    instanceId=request.targetInstanceId,
                    expected=request.expectedRevision,
                    actual=current,
                )

        plan_id = f"plan_{uuid.uuid4().hex}"
        snapshot = self.plan_snapshot_path(plan_id)
        digest, stored_source = self._snapshot_source(request.source, snapshot)
        required, gaps = self._capability_requirements(snapshot)
        risks = PlanRisks(
            keepData=True if request.intent == "update" else None,
            resourceProfile=request.options.resourceProfile,
            entryChange=False,
            possibleDowntime=request.intent == "update",
            capabilityGaps=gaps,
        )
        created = self._now()
        expires = created + timedelta(seconds=PLAN_TTL_SECONDS)
        workspace_id = self.registry.get_or_create_workspace_id()
        hash_payload = {
            "source": stored_source.model_dump(mode="json"),
            "intent": request.intent,
            "targetInstanceId": request.targetInstanceId,
            "expectedRevision": request.expectedRevision,
            "displayName": request.displayName,
            "options": request.options.model_dump(mode="json"),
            "policyVersion": PLAN_POLICY_VERSION,
        }
        record = PlanRecord(
            planId=plan_id,
            principalId=self.principal.principal_id,
            workspaceId=workspace_id,
            intent=request.intent,
            source=stored_source,
            sourceDigest=digest,
            targetInstanceId=request.targetInstanceId,
            expectedRevision=request.expectedRevision,
            displayName=request.displayName,
            options=request.options,
            policyVersion=PLAN_POLICY_VERSION,
            requestHash=request_hash(hash_payload),
            createdAt=_rfc3339(created),
            expiresAt=_rfc3339(expires),
            risks=risks,
            requiredCapabilities=required,
        )
        self.registry.insert_agent_plan(
            {
                "plan_id": record.planId,
                "principal_id": record.principalId,
                "workspace_id": record.workspaceId,
                "intent": record.intent,
                "source_json": canonical_json(record.source.model_dump(mode="json")),
                "source_digest": record.sourceDigest,
                "target_instance_id": record.targetInstanceId,
                "expected_revision": record.expectedRevision,
                "display_name": record.displayName,
                "options_json": canonical_json(record.options.model_dump(mode="json")),
                "policy_version": record.policyVersion,
                "request_hash": record.requestHash,
                "created_at": record.createdAt,
                "expires_at": record.expiresAt,
                "risks_json": canonical_json(record.risks.model_dump(mode="json")),
                "required_capabilities_json": canonical_json(record.requiredCapabilities),
            }
        )
        log.info("已生成部署计划 %s（intent=%s，ttl=%ss）", plan_id, request.intent, PLAN_TTL_SECONDS)
        return record

    def assert_plan_fresh(self, plan: PlanRecord) -> None:
        """W08/W10：过期计划必须重新 plan，不得继续 apply。"""
        expires = datetime.fromisoformat(plan.expiresAt.replace("Z", "+00:00"))
        if self._now() >= expires:
            raise AgentServiceError(
                "部署计划已过期，请重新计划",
                code=AgentErrorCode.needs_input.value,
                planId=plan.planId,
            )

    def _runtime_from_cache(self) -> tuple[dict[str, Any], str]:
        """只读能力缓存，不调用 collect_capability_report。"""
        path = self.workspace.root / "run" / "capability-manager.json"
        fallback_at = _rfc3339(self._now())
        unknown: dict[str, Any] = {"overall": "unknown", "capabilities": {}}
        if not path.is_file():
            return unknown, fallback_at
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return unknown, fallback_at
        if not isinstance(data, dict):
            return unknown, fallback_at
        runtime = {
            "overall": data.get("overall", "unknown"),
            "profile": data.get("profile"),
            "capabilities": data.get("capabilities") or {},
            "action": data.get("action"),
        }
        observed = data.get("checkedAt") or fallback_at
        return runtime, str(observed)

    def _snapshot_source(self, source: DeploySource, dest: Path) -> tuple[str, DeploySource]:
        if isinstance(source, ArtifactSource):
            raise AgentServiceError(
                "M1 不支持 artifact 源；请使用 server_directory 或 git",
                code=AgentErrorCode.source_not_allowed.value,
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        if isinstance(source, ServerDirectorySource):
            try:
                real = validate_source_root(source.path, self.config.agent.allowedSourceRoots)
            except AgentAuthError as exc:
                raise AgentServiceError(
                    exc.message,
                    code=exc.code,
                    **exc.context,
                ) from exc
            if not real.is_dir():
                raise AgentServiceError(
                    f"server_directory 不是可读目录: {source.path}",
                    code=AgentErrorCode.source_not_allowed.value,
                )
            shutil.copytree(real, dest, ignore=_ignore_vcs, dirs_exist_ok=False)
            return compute_source_hash(dest), source
        return self._snapshot_git(source, dest)

    def _snapshot_git(self, source: GitSource, dest: Path) -> tuple[str, GitSource]:
        from local_webpage_access import git_source

        try:
            target = git_source.parse_github_url(source.url)
            with git_source.stage_git_clone(target, ref=source.ref) as cloned:
                tree = cloned.directory
                if source.subdir:
                    # 根与子路径都 resolve 后再比包含：macOS tempfile 位于 /var
                    #（→/private/var 符号链接），未 resolve 的根会误杀合法 subdir（BUG-674）。
                    root = cloned.directory.resolve()
                    tree = (root / source.subdir).resolve()
                    if not tree.is_relative_to(root):
                        raise AgentServiceError(
                            "git subdir 越出仓库根",
                            code=AgentErrorCode.source_not_allowed.value,
                        )
                    if not tree.is_dir():
                        raise AgentServiceError(
                            f"git subdir 不存在: {source.subdir}",
                            code=AgentErrorCode.source_not_allowed.value,
                        )
                shutil.copytree(tree, dest, ignore=_ignore_vcs, dirs_exist_ok=False)
                digest = f"{cloned.commit}:{compute_source_hash(dest)}"
                stored = GitSource(
                    type="git",
                    url=target.url,
                    ref=cloned.ref,
                    subdir=source.subdir,
                )
        except AgentServiceError:
            raise
        except GitSourceError as exc:
            kind = str(exc.context.get("kind") or "")
            code = (
                AgentErrorCode.source_not_allowed.value
                if kind in _GIT_SOURCE_DENIED
                else AgentErrorCode.capability_unavailable.value
            )
            raise AgentServiceError(exc.message, code=code, **exc.context) from exc
        return digest, stored

    def _capability_requirements(self, snapshot: Path) -> tuple[list[str], list[str]]:
        from local_webpage_access.scanner import Scanner

        detection = Scanner().detect(snapshot)
        required: list[str] = []
        if detection.runtime == Runtime.DOCKER_COMPOSE:
            required.append("docker")
        runtime, _observed = self._runtime_from_cache()
        raw_caps = runtime.get("capabilities")
        caps: dict[str, Any] = raw_caps if isinstance(raw_caps, dict) else {}
        gaps: list[str] = []
        if "docker" in required:
            docker_state = caps.get("dockerEngine") or caps.get("dockerAccess")
            if docker_state != "ready":
                gaps.append("docker")
        return required, gaps

    @staticmethod
    def _summary_from_row(row: dict[str, Any]) -> InstanceSummary:
        revision = row.get("revision")
        return InstanceSummary(
            instanceId=str(row["id"]),
            name=str(row.get("name") or row["id"]),
            kind=str(row.get("kind") or "unknown"),
            runtime=str(row.get("runtime") or "unknown"),
            status=str(row.get("status") or "pending"),
            revision=1 if revision is None else int(revision),
        )

    @staticmethod
    def _operation_view(row: dict[str, Any] | None) -> OperationView | None:
        if not row:
            return None
        error = None
        raw_error = row.get("error")
        if isinstance(raw_error, dict):
            from local_webpage_access.agent.contracts import AgentError

            error = AgentError.model_validate(raw_error)
        raw_phase = row.get("phase")
        phase = OperationPhase(raw_phase) if raw_phase else None
        return OperationView(
            operationId=str(row["operation_id"]),
            action=OperationAction(row["action"]),
            status=OperationStatus(row["status"]),
            phase=phase,
            instanceId=row.get("target_instance_id"),
            result=row.get("result") if isinstance(row.get("result"), dict) else None,
            error=error,
            createdAt=str(row["created_at"]),
            updatedAt=str(row["updated_at"]),
        )


def _ignore_vcs(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in {".git", ".hg", ".svn"}}
