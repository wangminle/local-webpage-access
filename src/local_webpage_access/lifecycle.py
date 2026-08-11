"""实例生命周期编排（WBS-17）。

统一静态实例与容器实例的 ``start`` / ``stop`` / ``restart`` / ``rebuild`` /
``remove`` 操作，并负责：

* ``desiredState`` 与用户操作保持一致（WBS-17.06）；
* 所有生命周期动作写入 events（WBS-17.11）；
* 同一实例的并发操作用文件锁串行化（WBS-17.12），避免孤儿进程 / 端口冲突；
* ``status`` 的观测与回写（WBS-17.07），见 :func:`observe_status`。

设计参考：V1 设计说明 §8.1（desiredState 与 status）、§14（共享静态托管）。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from local_webpage_access.config import Config
from local_webpage_access.errors import DataNonemptyError, LifecycleError, LwaError
from local_webpage_access.file_lock import (
    ensure_lockable,
    release_exclusive,
    try_acquire_exclusive,
    write_lock_payload,
)
from local_webpage_access.logging import get_logger
from local_webpage_access.models import InstanceManifest, Status
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry

log = get_logger("lifecycle")

_LOCK_TIMEOUT = 30.0  # 实例级锁默认等待上限（秒）
# 进程崩溃未释放锁时的兜底观测阈值（心跳超时提示）；互斥本身由 file_lock 保证。
_STALE_LOCK_SECONDS = 1800.0
# 心跳刷新间隔：明显小于 _STALE_LOCK_SECONDS，确保长耗时 rebuild/build
# 期间锁不会被误判陈旧（BUG-046）。取 staleness 的 1/3 且上限 300s。
_LOCK_HEARTBEAT_INTERVAL = min(_STALE_LOCK_SECONDS / 3.0, 300.0)

# Gate-C 成本控制（IMP-058 §6.5）
_MAX_FALLBACK_CANDIDATES = 3  # 最多尝试的 fallback 候选数（不含 top-1）
_CANDIDATE_TIMEOUT_SECONDS = 300.0  # 单候选 Layer 3 超时（5 分钟）

# Gate-C C.07 fallback 策略
_FALLBACK_CONFIRM = "confirm"        # 默认：需用户确认才降级
_FALLBACK_AUTO_EQUIVALENT = "auto-equivalent"  # 自动降级（仅能力等价）
_FALLBACK_DISABLED = "disabled"      # 禁止降级

# 进程内每个实例一把可重入锁；与文件锁叠加，使同一进程的线程也互斥，
# 避免文件锁的 PID 检查在同进程多线程下失效（PID 相同）。
_thread_locks: dict[str, threading.RLock] = {}
_thread_locks_guard = threading.Lock()


def _get_thread_lock(instance_id: str) -> threading.RLock:
    with _thread_locks_guard:
        lock = _thread_locks.get(instance_id)
        if lock is None:
            lock = threading.RLock()
            _thread_locks[instance_id] = lock
        return lock


# ---- 并发锁（WBS-17.12）-----------------------------------------------------


def _touch_lock_heartbeat(lock_path: Path) -> None:
    """刷新锁文件的心跳时间戳（BUG-046）。

    原地改写同一 inode（不得 ``os.replace`` / unlink），以免打断持有者的文件锁。
    锁文件不存在或不可读时为空操作（仅在已持锁时调用，故不应发生）。
    """
    try:
        with open(lock_path, "r+", encoding="utf-8") as fh:
            content = fh.read().strip().splitlines()
            pid_line = content[0] if content else str(os.getpid())
            fh.seek(0)
            fh.truncate()
            fh.write(f"{pid_line}\n{time.time():.3f}\n")
            fh.flush()
    except OSError:
        return


@contextlib.contextmanager
def instance_lock(
    workspace: Workspace,
    instance_id: str,
    *,
    timeout: float = _LOCK_TIMEOUT,
) -> Iterator[None]:
    """同一实例的生命周期操作互斥锁。

    双层锁：
    1. 进程内 ``threading.RLock`` —— 同进程多线程（如 daemon）串行；
    2. 跨进程 :mod:`file_lock`（POSIX flock / Windows msvcrt）—— 多进程串行。

    锁文件写入持有进程 PID 与时间戳，供观测与心跳（BUG-046）。释放时**不
    unlink**，避免等待者锁旧 inode、第三者新建并锁新 inode 的并行持锁竞态
    （BUG-213）。超时仍拿不到锁抛 :class:`LifecycleError`。

    实例 ID 在入口校验（BUG-025），避免 ``..`` / ``/`` 等片段把锁文件
    写到 ``run/`` 之外。
    """
    from local_webpage_access.paths import validate_instance_id

    validate_instance_id(instance_id)
    tlock = _get_thread_lock(instance_id)
    if not tlock.acquire(timeout=timeout):
        raise LifecycleError(
            f"实例 {instance_id} 正在被其他操作占用，等待超时（{timeout}s）",
            instance_id=instance_id,
        )
    fd: int | None = None
    file_acquired = False
    lock_path = workspace.run / f"lifecycle-{instance_id}.lock"
    heartbeat_stop: threading.Event | None = None
    heartbeat_thread: threading.Thread | None = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        while True:
            try:
                ensure_lockable(fd)
                try_acquire_exclusive(fd)
                write_lock_payload(
                    fd, f"{os.getpid()}\n{time.time():.3f}\n".encode()
                )
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LifecycleError(
                        f"实例 {instance_id} 正在被其他操作占用，等待超时（{timeout}s）",
                        instance_id=instance_id,
                    )
                time.sleep(0.1)
        file_acquired = True

        # 启动心跳线程：长耗时 rebuild/build 期间持续刷新时间戳（BUG-046）
        heartbeat_stop = threading.Event()

        def _heartbeat_loop() -> None:
            while not heartbeat_stop.wait(_LOCK_HEARTBEAT_INTERVAL):
                _touch_lock_heartbeat(lock_path)

        heartbeat_thread = threading.Thread(
            target=_heartbeat_loop,
            name=f"lwa-lock-hb-{instance_id}",
            daemon=True,
        )
        heartbeat_thread.start()

        try:
            yield
        finally:
            if heartbeat_stop is not None:
                heartbeat_stop.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=5.0)
            if file_acquired and fd is not None:
                release_exclusive(fd)
                # BUG-213：释放后不 unlink，保持同一 inode 供后续竞争者复用
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        tlock.release()


def _lock_is_stale(lock_path: Path) -> bool:
    """锁是否可视为陈旧（观测用）：持有进程已不存活，或存活但超过 :data:`_STALE_LOCK_SECONDS`。

    互斥已由 :mod:`file_lock` 保证；本函数保留供测试与诊断，不再用于抢锁。
    """
    try:
        content = lock_path.read_text(encoding="utf-8").strip().splitlines()
        pid = int(content[0]) if content else 0
        ts = float(content[1]) if len(content) > 1 else 0.0
    except (OSError, ValueError):
        return True
    if pid and _pid_alive(pid):
        # 进程仍在：仅超时才视为陈旧
        if ts <= 0.0:
            # 无心跳行：用 mtime 兜底
            try:
                ts = lock_path.stat().st_mtime
            except OSError:
                return True
        return (time.time() - ts) > _STALE_LOCK_SECONDS
    return True


def _pid_alive(pid: int) -> bool:
    """跨平台的进程存活探测。"""
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            SYNCHRONIZE = 0x00100000
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, ProcessLookupError):
        return False


# ---- 内部辅助 ---------------------------------------------------------------


def _load(workspace: Workspace, instance_id: str) -> InstanceManifest:
    from local_webpage_access.hosting import _load_manifest

    return _load_manifest(workspace, instance_id)


def _load_optional(
    workspace: Workspace, instance_id: str
) -> InstanceManifest | None:
    path = workspace.app_manifest_path(instance_id)
    if not path.is_file():
        return None
    try:
        return InstanceManifest.load(path)
    except Exception as exc:  # noqa: BLE001
        log.warning("实例 %s manifest 损坏，remove 将按 registry 降级清理：%s", instance_id, exc)
        return None


def _is_deployed_container(manifest: InstanceManifest) -> bool:
    """容器实例是否已部署过（有 containerId 落库），可走轻量 start。"""
    return (
        manifest.runtime.value == "docker-compose"
        and manifest.container is not None
        and bool(manifest.container.containerId)
    )


# ---- 公开生命周期入口 -------------------------------------------------------


def start_instance(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
    *,
    fallback_policy: str = _FALLBACK_CONFIRM,
) -> InstanceManifest:
    """启动实例（WBS-17.01）。

    * 容器实例已部署过 → :func:`start_container`（容器仍在则 ``compose start``；
      外部 down 后若 compose/镜像仍在则 ``up -d`` 自愈；否则回退完整重建）；
    * 否则（首次启动 / 静态 / 前端）→ 全量 :func:`host_instance`。

    Gate-C（IMP-058 §6.5 / §6.1.1 / C.06-C.07）：首次部署的 docker-compose 实例
    若 top-1 候选 build/start 失败：

    1. 记录失败诊断并**回滚本次 attempt**（容器、端口、生成文件、manifest 选中状态）；
    2. 仅当回滚成功且 ``automatic_fallback_safe=True`` 时，才尝试下一个候选；
    3. 降级策略由 ``fallback_policy`` 控制：
       - ``"confirm"``（默认）：交互式 CLI 由上层展示等价计划并等待确认；
         非交互调用返回 :class:`FallbackConfirmationRequired`。
       - ``"auto-equivalent"``：自动降级到能力契约等价的候选。
       - ``"disabled"``：不降级，直接失败。
    4. 能力守恒（§6.1.1）：后端候选不得降级到静态/前端候选。
    5. 最多尝试 ``_MAX_FALLBACK_CANDIDATES`` 个等价候选。
    6. 全部失败时输出 Layer 4 诊断报告。

    最终 ``desiredState=running``。
    """
    from local_webpage_access.hosting import host_instance, start_container

    with instance_lock(workspace, instance_id):
        manifest = _load(workspace, instance_id)
        if _is_deployed_container(manifest):
            log.info("实例 %s 已部署，使用轻量 start", instance_id)
            manifest = start_container(workspace, config, registry, instance_id)
        else:
            # Gate-C：尝试 top-1 候选，失败时降级到 fallback
            manifest = _try_host_with_fallback(
                workspace, config, registry, instance_id, manifest,
                host_instance,
                fallback_policy=fallback_policy,
            )
        # BUG-084 / IMP-021：首次启动前设置的容器别名此时才拿到 hostPort，
        # 同步生成别名片段（端口漂移时也会重写）；静态别名由 _enable_static 处理，
        # 此处对其为 no-op（conf 已是正确端口）。
        _sync_alias_port(workspace, config, instance_id, manifest)
        return manifest


def _try_host_with_fallback(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
    manifest: InstanceManifest,
    host_fn: Any,
    *,
    fallback_policy: str = _FALLBACK_CONFIRM,
) -> InstanceManifest:
    """Gate-C C.06/C.07/C.08：尝试 top-1 候选，失败时按策略降级到 fallback。

    仅对 docker-compose 实例的首次部署生效（已部署容器走轻量 start，不进此路径）。
    静态/前端实例通常无 fallback 候选，直接走原逻辑。

    IMP-058 §6.1.1 硬性约束（能力守恒）：不得把后端容器降级为前端静态站并
    仍声明部署成功。降级前必须验证 fallback 候选与 top-1 候选能力等价——
    后端候选（kind=python/node, form=backend-container/fullstack-sqlite）
    只能降级到同族后端候选，不得降级到 static / frontend-static。

    事务模型（§6.5 候选切换事务）：
    1. Prepare：快照 manifest 选中状态
    2. Execute：尝试 host（独立 attempt ID）
    3. Verify：评估成功谓词（由 host_fn 内部完成）
    4. Commit：验证成功 → 写入选中计划、容器 ID、网络入口、RUNNING
    5. Rollback：验证失败 → 回滚容器、端口、生成文件、manifest 选中状态

    回滚边界：``rollback_succeeded`` 只表示已声明的回滚步骤全部成功。
    若 attempt 可能执行了数据库迁移、队列发布或外部 API 写入，
    则只有满足下列任一条件才允许 ``automatic_fallback_safe=True``：
    1. 副作用运行在可丢弃的隔离环境；
    2. 已有可验证的事务回滚；
    3. 已从本次快照恢复，并通过恢复后探针。

    降级策略（``fallback_policy``）：
    - ``"confirm"``（默认）：非交互调用 → :class:`FallbackConfirmationRequired`。
    - ``"auto-equivalent"``：自动降级到等价候选。
    - ``"disabled"``：不降级。
    """
    from local_webpage_access.errors import DockerError, HostingError
    from local_webpage_access.models import (
        CandidateDiagnosis,
        DiagnosisReport,
        VerificationResult,
    )

    # 尝试 top-1
    attempt_id = f"attempt-{instance_id}-0"
    try:
        return host_fn(workspace, config, registry, instance_id)
    except (HostingError, DockerError) as exc:
        # CHK-192/P1：DockerError（build/start 最常见失败）也需进入 fallback 流程，
        # 不能只捕获 HostingError 而让 DockerError 绕过降级。
        # C.06：回滚本次 attempt（容器、端口、生成文件、manifest 选中状态）
        rollback = _rollback_attempt(
            workspace, config, registry, instance_id, manifest,
            attempt_id=attempt_id,
        )

        # 构建 top-1 失败诊断
        top1_diagnosis = CandidateDiagnosis(
            candidateIndex=0,
            candidateTier="primary",
            attemptId=attempt_id,
            failureLayer=_infer_failure_layer(exc),
            failureReason=str(exc)[:500],
            fixSuggestion=_suggest_fix(exc),
            verification=VerificationResult(
                attemptId=attempt_id,
                candidateIndex=0,
                candidateTier="primary",
                buildSucceeded=False,
                overallStatus="failed",
                error=str(exc)[:500],
            ),
            rollback=rollback,
        )

        fallbacks = getattr(manifest, "deploymentCandidates", []) or []
        if not fallbacks:
            # 无 fallback → 直接输出诊断
            _write_diagnosis(workspace, registry, instance_id, DiagnosisReport(
                instanceId=instance_id,
                overallStatus="failed",
                candidatesTried=[top1_diagnosis],
                recommendedAction="无 fallback 候选；请检查 Dockerfile 与源码兼容性",
            ))
            raise

        # fallback_policy="disabled" → 不降级
        if fallback_policy == _FALLBACK_DISABLED:
            _write_diagnosis(workspace, registry, instance_id, DiagnosisReport(
                instanceId=instance_id,
                overallStatus="failed",
                candidatesTried=[top1_diagnosis],
                recommendedAction="fallback_policy=disabled，未尝试降级",
            ))
            raise

        # 静态实例不参与降级
        if manifest.runtime.value == "shared-static":
            raise

        # 能力守恒（§6.1.1）：过滤等价候选
        primary_capability = _candidate_capability_family(manifest)
        equivalent_fallbacks = []
        skipped_inequivalent = []
        for i, candidate_dict in enumerate(fallbacks):
            cand_capability = _dict_capability_family(candidate_dict)
            if _capabilities_equivalent(primary_capability, cand_capability):
                equivalent_fallbacks.append((i, candidate_dict))
            else:
                skipped_inequivalent.append((i, candidate_dict))

        if skipped_inequivalent:
            log.warning(
                "实例 %s 能力守恒：top-1 候选能力族=%s，跳过 %d 个不等价 fallback 候选"
                "（能力族: %s）",
                instance_id,
                primary_capability,
                len(skipped_inequivalent),
                ", ".join(
                    _dict_capability_family(d) for _, d in skipped_inequivalent
                ),
            )
            registry.add_event(
                instance_id,
                "lifecycle_stage",
                f"Gate-C 能力守恒：跳过 {len(skipped_inequivalent)} 个不等价 fallback 候选"
                f"（后端不得降级为静态）",
            )

        if not equivalent_fallbacks:
            _write_diagnosis(workspace, registry, instance_id, DiagnosisReport(
                instanceId=instance_id,
                overallStatus="failed",
                candidatesTried=[top1_diagnosis],
                recommendedAction="所有 fallback 候选能力不等价，未降级",
            ))
            raise

        # C.06：回滚不成功 → 不允许自动降级
        if not rollback.rollbackSucceeded:
            log.warning(
                "实例 %s top-1 回滚失败（%s），不允许自动降级",
                instance_id,
                ", ".join(rollback.rolledBackItems) or "无",
            )
            top1_diagnosis.rollback = rollback
            _write_diagnosis(workspace, registry, instance_id, DiagnosisReport(
                instanceId=instance_id,
                overallStatus="failed",
                candidatesTried=[top1_diagnosis],
                recommendedAction="回滚未完成，不允许自动降级；请手动检查残留容器/端口",
            ))
            raise

        # C.07：confirm 策略 → 需用户确认（非交互调用抛 FallbackConfirmationRequired）
        if fallback_policy == _FALLBACK_CONFIRM:
            raise FallbackConfirmationRequired(
                instance_id,
                primary_failure=str(exc)[:500],
                equivalent_candidates=[
                    {"index": i + 1, **c}
                    for i, c in equivalent_fallbacks[:_MAX_FALLBACK_CANDIDATES]
                ],
            )

        # CHK-192/P1：auto-equivalent 策略需检查 automaticFallbackSafe。
        # confirm 策略已在上方处理（用户可手动确认覆盖不安全状态）。
        # 若回滚标记为不安全（容器已启动且项目需要迁移），禁止自动降级。
        if not rollback.automaticFallbackSafe:
            log.warning(
                "实例 %s top-1 回滚标记 automaticFallbackSafe=False（可能已执行不可逆操作），"
                "禁止自动降级",
                instance_id,
            )
            top1_diagnosis.rollback = rollback
            _write_diagnosis(workspace, registry, instance_id, DiagnosisReport(
                instanceId=instance_id,
                overallStatus="failed",
                candidatesTried=[top1_diagnosis],
                recommendedAction="回滚不安全（可能已执行迁移等不可逆操作），"
                                  "禁止自动降级；请手动检查数据一致性后重试",
            ))
            raise

        # C.07：auto-equivalent → 自动降级
        log.warning(
            "实例 %s top-1 候选失败（%s），开始尝试 %d 个等价 fallback 候选",
            instance_id,
            str(exc)[:200],
            min(len(equivalent_fallbacks), _MAX_FALLBACK_CANDIDATES),
        )

        diagnoses: list[CandidateDiagnosis] = [top1_diagnosis]

        # 尝试等价 fallback 候选
        for attempt, (orig_index, candidate_dict) in enumerate(
            equivalent_fallbacks[:_MAX_FALLBACK_CANDIDATES]
        ):
            tier = candidate_dict.get("confidenceTier", "fallback")
            fb_attempt_id = f"attempt-{instance_id}-{attempt + 1}"
            log.info(
                "实例 %s 尝试 fallback 候选 %d/%d（tier=%s）",
                instance_id,
                attempt + 1,
                min(len(equivalent_fallbacks), _MAX_FALLBACK_CANDIDATES),
                tier,
            )
            registry.add_event(
                instance_id,
                "lifecycle_stage",
                f"Gate-C 降级：尝试 fallback 候选 {attempt + 1}（tier={tier}）",
            )
            try:
                return _apply_candidate_and_host(
                    workspace, config, registry, instance_id, manifest,
                    candidate_dict, host_fn,
                )
            except (HostingError, DockerError) as fallback_exc:
                # CHK-192/P1：fallback 候选的 DockerError 同样需要捕获
                log.warning(
                    "实例 %s fallback 候选 %d 失败：%s",
                    instance_id,
                    attempt + 1,
                    str(fallback_exc)[:200],
                )
                fb_rollback = _rollback_attempt(
                    workspace, config, registry, instance_id, manifest,
                    attempt_id=fb_attempt_id,
                )
                diagnoses.append(CandidateDiagnosis(
                    candidateIndex=orig_index + 1,
                    candidateTier=tier,
                    failureLayer=_infer_failure_layer(fallback_exc),
                    failureReason=str(fallback_exc)[:500],
                    fixSuggestion=_suggest_fix(fallback_exc),
                    verification=VerificationResult(
                        attemptId=fb_attempt_id,
                        candidateIndex=orig_index + 1,
                        candidateTier=tier,
                        buildSucceeded=False,
                        overallStatus="failed",
                        error=str(fallback_exc)[:500],
                    ),
                    rollback=fb_rollback,
                ))
                continue

        # 全部失败 → Layer 4 诊断报告
        report = DiagnosisReport(
            instanceId=instance_id,
            overallStatus="failed",
            candidatesTried=diagnoses,
            recommendedAction="所有候选均部署失败；请检查源码、Dockerfile 或手动配置",
            notes=[d.failureReason for d in diagnoses if d.failureReason],
        )
        _write_diagnosis(workspace, registry, instance_id, report)
        raise HostingError(
            f"实例 {instance_id} 所有候选均部署失败（{len(diagnoses)} 个尝试）；"
            f"诊断报告已写入 lastError",
            instance_id=instance_id,
        )


def _candidate_capability_family(manifest: InstanceManifest) -> str:
    """推断 manifest（top-1 候选）的能力族。

    IMP-058 §6.1.1：用于能力守恒校验。返回 ``"backend"`` / ``"static"`` /
    ``"frontend"``。后端候选含 API/DB 能力，不得降级到 static/frontend。

    InstanceManifest 没有 ``form`` 字段（那是 DetectionResult / DeploymentCandidate
    的），用 ``kind`` + ``servingMode`` + ``hasDatabase`` 做代理判断：
    - 容器化 python/node → backend（含 API 能力，可能含 DB）
    - shared-static + node kind → frontend（构建型静态站）
    - shared-static + static kind → static（纯静态）
    """
    kind = _enum_value(manifest.kind).lower()
    serving_mode = _enum_value(manifest.servingMode).lower()
    if serving_mode == "container" and kind in ("python", "node"):
        return "backend"
    if kind == "node" and serving_mode == "shared_static":
        return "frontend"
    return "static"


# ---- Gate-C C.06/C.07/C.08 辅助 ---------------------------------------------


def _rollback_attempt(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
    manifest: InstanceManifest,
    *,
    attempt_id: str,
) -> Any:
    """Gate-C C.06：回滚单次 attempt 的基础设施状态。

    回滚范围（§6.5 候选切换事务 Rollback 阶段）：
    - 容器（``docker compose down``）
    - 端口（释放本次 attempt 新分配的端口）
    - manifest 选中状态（恢复到失败前的快照）

    **回滚边界**（§6.5）：
    ``rollback_succeeded`` 只表示已声明的回滚步骤全部成功，不得解释为
    "系统没有任何副作用"。数据库 schema/data、外部服务写入不因容器 down 自动恢复。
    ``automatic_fallback_safe`` 仅当确认本次 attempt 未执行不可逆操作时才为 True。

    CHK-192/P1：``automaticFallbackSafe`` 不再恒为 False。当回滚成功且
    容器从未启动（build 阶段失败）或项目不需要迁移时，标记为 True，
    允许 auto-equivalent 降级。容器已启动且项目需要迁移时保守标记 False。
    """
    from local_webpage_access.models import RollbackResult

    rolled_back: list[str] = []
    container_was_running = False

    # 1. 回滚容器
    try:
        from local_webpage_access.docker_runtime import DockerRuntime
        runtime = DockerRuntime(workspace, registry)
        if runtime.is_running(instance_id):
            container_was_running = True
            runtime.down(instance_id)
            rolled_back.append("container")
    except Exception as exc:  # noqa: BLE001
        log.warning("实例 %s 回滚容器失败：%s", instance_id, exc)

    # 2. 回滚端口（仅释放本次 attempt 新分配的）
    try:
        from local_webpage_access.ports import PortAllocator
        allocator = PortAllocator(config, registry)
        # 只释放没有成功部署登记的端口
        if not manifest.container or not manifest.container.containerId:
            allocator.release_instance(instance_id)
            rolled_back.append("port")
    except Exception as exc:  # noqa: BLE001
        log.debug("实例 %s 回滚端口跳过：%s", instance_id, exc)

    # 3. manifest 不恢复原值--失败诊断需要写入 manifest.lastError
    #    上层 _write_diagnosis 会设置 status=FAILED

    rollback_ok = len(rolled_back) > 0 or not _has_active_resources(
        workspace, registry, instance_id
    )

    # CHK-192/P1：判断 automaticFallbackSafe
    # - 容器从未启动（build 阶段失败）-> 不可能执行迁移 -> safe
    # - 容器已启动但项目不需要迁移 -> safe
    # - 容器已启动且项目需要迁移 -> 可能已执行不可逆操作 -> unsafe
    contract = getattr(manifest, "capabilityContract", None)
    requires_migrations = False
    if isinstance(contract, dict):
        requires_migrations = contract.get("requiresMigrations", False)
    automatic_fallback_safe = rollback_ok and (
        not container_was_running or not requires_migrations
    )

    return RollbackResult(
        attemptId=attempt_id,
        rollbackSucceeded=rollback_ok,
        rolledBackItems=rolled_back,
        externalSideEffects=[],  # 由 host_fn 在 migration 执行时填充
        automaticFallbackSafe=automatic_fallback_safe,
    )


def _has_active_resources(
    workspace: Workspace,
    registry: Registry,
    instance_id: str,
) -> bool:
    """检查实例是否有活跃的容器或端口登记。"""
    try:
        from local_webpage_access.docker_runtime import DockerRuntime
        runtime = DockerRuntime(workspace, registry)
        if runtime.is_running(instance_id):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _infer_failure_layer(exc: Exception) -> str:
    """Gate-C C.08：从异常推断失败层。"""
    exc_str = str(exc).lower()
    if "build" in exc_str or "dockerfile" in exc_str:
        return "build"
    if "health" in exc_str or "probe" in exc_str or "wait" in exc_str:
        return "health"
    if "up" in exc_str and "compose" in exc_str:
        return "start"
    if "port" in exc_str:
        return "start"
    return "build"


def _suggest_fix(exc: Exception) -> str:
    """Gate-C C.08：从异常推断修复建议。"""
    exc_str = str(exc).lower()
    if "dockerfile" in exc_str:
        return "检查 Dockerfile 语法与源码兼容性"
    if "port" in exc_str:
        return "检查端口占用或手动指定端口"
    if "build" in exc_str:
        return "检查构建依赖、源码与 Dockerfile 兼容性"
    if "health" in exc_str:
        return "检查应用启动逻辑与健康探针配置"
    return "检查源码、Dockerfile 或手动配置"


class FallbackConfirmationRequired(LwaError):
    """Gate-C C.07：需要用户确认才能降级到等价候选。

    非交互调用收到此异常后，由调用方决定是否携带明确的计划选择重试
    （``fallback_policy="auto-equivalent"``）。

    该结果**不是**部署成功，也不应被折叠为普通诊断失败。
    """

    def __init__(
        self,
        instance_id: str,
        *,
        primary_failure: str = "",
        equivalent_candidates: list[dict] | None = None,
    ) -> None:
        self.instance_id = instance_id
        self.primary_failure = primary_failure
        self.equivalent_candidates = equivalent_candidates or []
        candidate_desc = ", ".join(
            f"#{c.get('index', '?')}({c.get('kind', '?')})"
            for c in (equivalent_candidates or [])
        )
        msg = (
            f"实例 {instance_id} top-1 候选部署失败（{primary_failure[:100]}），"
            f"存在 {len(self.equivalent_candidates)} 个等价 fallback 候选"
            f"（{candidate_desc}）。需用户确认后降级。"
        )
        super().__init__(msg, instance_id=instance_id)


def _enum_value(val: Any) -> str:
    """从枚举或字符串获取小写字符串值（兼容 Kind/ServingMode/Runtime 枚举）。"""
    if val is None:
        return ""
    # 枚举成员：优先 .value；字符串直接返回
    value = getattr(val, "value", val)
    return str(value)


def _dict_capability_family(candidate: dict) -> str:
    """推断候选字典的能力族（与 ``_candidate_capability_family`` 对齐）。

    候选字典来自 ``DeploymentCandidate.model_dump()``，可能有 ``form`` 字段
    （candidate_generator 设置）也可能没有（旧版/手写）。优先用 ``form``，
    缺失时用 ``kind`` + ``servingMode`` 做代理判断。
    """
    kind = str(candidate.get("kind", "")).lower()
    form = str(candidate.get("form", "")).lower()
    serving_mode = str(candidate.get("servingMode", "")).lower()
    # 优先用 form（candidate_generator 会设置）
    if kind in ("python", "node") and form in ("backend-container", "fullstack-sqlite"):
        return "backend"
    if form == "frontend-static":
        return "frontend"
    # form 缺失时用 kind + servingMode（与 _candidate_capability_family 一致）
    if serving_mode == "container" and kind in ("python", "node"):
        return "backend"
    if kind == "node" and serving_mode == "shared_static":
        return "frontend"
    return "static"


def _capabilities_equivalent(primary: str, candidate: str) -> bool:
    """两个能力族是否等价（可互相降级）。

    IMP-058 §6.1.1 硬性约束：backend 不得降级到 static/frontend。
    backend 只与 backend 等价；static/frontend 之间互相等价。
    """
    if primary == "backend":
        return candidate == "backend"
    # static / frontend 之间允许互相降级（纯静态站点族）
    return candidate in ("static", "frontend")


def _apply_candidate_and_host(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
    manifest: InstanceManifest,
    candidate_dict: dict,
    host_fn: Any,
) -> InstanceManifest:
    """Gate-C C.03：将 fallback 候选应用到 manifest 并重新尝试 host。

    只覆盖与部署相关的字段（kind/runtime/entry/sourceSubdir），保留
    id/name/data/路径别名等用户数据。
    """
    # 应用候选配置到 manifest
    from local_webpage_access.models import EntryConfig, Kind, Runtime, ServingMode

    candidate_kind = candidate_dict.get("kind")
    candidate_runtime = candidate_dict.get("runtime", "").replace("-", "_")
    candidate_entry = candidate_dict.get("entry", {})
    candidate_subdir = candidate_dict.get("sourceSubdir")

    if candidate_kind:
        try:
            manifest.kind = Kind(candidate_kind)
        except ValueError:
            pass
    if candidate_runtime:
        try:
            rt = Runtime(candidate_runtime.replace("_", "-"))
            manifest.runtime = rt
            manifest.servingMode = (
                ServingMode.SHARED_STATIC
                if rt == Runtime.SHARED_STATIC
                else ServingMode.CONTAINER
            )
        except ValueError:
            pass
    if candidate_entry:
        manifest.entry = EntryConfig(**candidate_entry)
    if candidate_subdir:
        manifest.sourceSubdir = candidate_subdir

    manifest.touch()
    manifest.save(workspace.app_manifest_path(instance_id))

    return host_fn(workspace, config, registry, instance_id)


def _write_diagnosis(
    workspace: Workspace,
    registry: Registry,
    instance_id: str,
    report: Any,
) -> None:
    """Gate-C C.08：将诊断报告写入 manifest.lastError + registry 事件。

    Layer 4 诊断包含每个 attempt 的：
    - 失败层（preflight / build / start / health / api）
    - 失败原因
    - 回滚结果（C.06）
    - 能力差异（C.01/C.07）
    - 修复建议
    """
    lines = []
    lines.append(f"部署诊断（{report.overallStatus}）：")
    lines.append(f"  尝试了 {len(report.candidatesTried)} 个候选")
    for d in report.candidatesTried:
        tier_label = f"[{d.candidateTier}]" if d.candidateTier else ""
        lines.append(
            f"  候选 {d.candidateIndex} {tier_label}: "
            f"{d.failureLayer} 阶段失败 — {d.failureReason[:120]}"
        )
        # C.06：回滚结果
        if d.rollback:
            rb = d.rollback
            lines.append(
                f"    回滚: {'成功' if rb.rollbackSucceeded else '失败'}"
                f"（{', '.join(rb.rolledBackItems) or '无'}）"
            )
            if rb.externalSideEffects:
                lines.append(
                    f"    ⚠️ 残余副作用: {', '.join(rb.externalSideEffects)}"
                )
        # C.01/C.07：能力差异
        if d.capabilityDiff:
            lines.append(f"    能力差异: {d.capabilityDiff}")
    if report.recommendedAction:
        lines.append(f"  建议：{report.recommendedAction}")

    diagnosis_text = "\n".join(lines)[:2000]

    try:
        manifest = _load(workspace, instance_id)
        manifest.lastError = diagnosis_text
        manifest.status = Status.FAILED
        manifest.touch()
        manifest.save(workspace.app_manifest_path(instance_id))
    except Exception:  # noqa: BLE001
        log.warning("实例 %s 写入诊断 manifest 失败", instance_id)

    registry.update_status(
        instance_id, Status.FAILED.value, last_error=diagnosis_text[:500]
    )
    registry.add_event(
        instance_id,
        "diagnosis",
        f"Gate-C 诊断：{len(report.candidatesTried)} 个候选全部失败",
    )


def stop_instance_op(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
) -> InstanceManifest:
    """停止实例（WBS-17.02）。最终 ``desiredState=stopped``。

    容器：``compose stop``；静态：禁用网关 + 释放端口。**不删容器与数据**。
    """
    from local_webpage_access.hosting import stop_instance

    with instance_lock(workspace, instance_id):
        return stop_instance(workspace, config, registry, instance_id)


def restart_instance(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
) -> InstanceManifest:
    """重启实例（WBS-17.03）：先 stop 再 start。

    在同一把锁内完成，保证原子性。已部署的容器走轻量 start，不重建镜像。
    IMP-021：重启后若实例有路径别名且 hostPort 发生漂移，重写别名片段并 reload。
    """
    with instance_lock(workspace, instance_id):
        return _restart_instance_locked(workspace, config, registry, instance_id)


def _restart_instance_locked(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
) -> InstanceManifest:
    """``restart_instance`` 的已持锁实现（供 recover 等复用，避免文件锁重入）。"""
    from local_webpage_access.hosting import (
        host_instance,
        start_container,
        stop_instance,
    )

    manifest = _load(workspace, instance_id)
    deployed_container = _is_deployed_container(manifest)
    # 先停：容忍"本来就没在跑"的噪声（含 Docker/网关不可用等 stop 失败）
    try:
        stop_instance(workspace, config, registry, instance_id)
    except LwaError as exc:
        log.warning("restart 前停止失败（忽略并继续启动）：%s", exc)

    if deployed_container:
        manifest = start_container(workspace, config, registry, instance_id)
    else:
        manifest = host_instance(workspace, config, registry, instance_id)
    # IMP-021：容器别名入口 reverse_proxy 到 hostPort，端口漂移时同步别名片段。
    _sync_alias_port(workspace, config, instance_id, manifest)
    return manifest


def recover_instance(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
) -> InstanceManifest:
    """DEV-043 配套：恢复处于 ``gateway_down`` / ``config_invalid`` / 掉线的实例。

    针对管理页"一键 recover"：对静态实例，若 Caddy master 离线，先
    :func:`~local_webpage_access.gateway_service.maybe_start_gateway` 拉起 master
    （失败不阻断，可降级 builtin），再 restart 重新托管——后者
    的 reload 会把站点/别名片段重新注入主配置。容器实例等价于 restart。
    最终 ``desiredState=running``。

    全程在 ``instance_lock`` 内执行，避免与并发 ``remove_instance`` 竞态。
    """
    from local_webpage_access.gateway_service import maybe_start_gateway

    with instance_lock(workspace, instance_id):
        manifest = _load(workspace, instance_id)
        if manifest.runtime.value == "shared-static":
            try:
                from local_webpage_access.static_gateway import StaticGateway

                gw = StaticGateway(workspace, config)
                if gw.detect_backend() == "caddy" and not gw._admin_alive():
                    log.info(
                        "recover %s: Caddy master 离线，先 maybe_start_gateway",
                        instance_id,
                    )
                    maybe_start_gateway(workspace, config)
            except Exception as exc:  # noqa: BLE001 — 网关拉起失败不阻断 restart
                log.warning(
                    "recover %s: 拉起网关失败（继续 restart）：%s", instance_id, exc
                )
        return _restart_instance_locked(workspace, config, registry, instance_id)


def rebuild_instance(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
) -> InstanceManifest:
    """重建实例（WBS-17.04）：强制重新构建。

    * 容器：``compose down`` 旧容器 → 重新生成模板 → ``build`` → ``up``
      （由 :func:`host_container` 完成）；
    * 静态 / 前端：重新同步 / 重新构建产物（由 :func:`host_instance` 完成）。

    构建通过 :class:`~local_webpage_access.build_queue.BuildQueue` 限流，
    默认并发 1（WBS-20），避免小主机并发构建 OOM。

    队列取进程内单例（:func:`~local_webpage_access.build_queue.get_build_queue`，
    BUG-022），否则每次 rebuild 各建独立信号量，并发上限失效。
    """
    from local_webpage_access.build_queue import get_build_queue
    from local_webpage_access.hosting import host_container, host_instance

    with instance_lock(workspace, instance_id):
        manifest = _load(workspace, instance_id)
        is_container = manifest.runtime.value == "docker-compose"

        def _builder(iid: str) -> InstanceManifest:
            if is_container:
                # host_container 内部会 down 旧容器再 build + up
                return host_container(workspace, config, registry, iid)
            return host_instance(workspace, config, registry, iid)

        queue = get_build_queue(config, registry)
        manifest = queue.run(instance_id, _builder)
        # IMP-021：重建后端口可能漂移，同步别名片段（容器别名 reverse_proxy hostPort）。
        _sync_alias_port(workspace, config, instance_id, manifest)
        return manifest


def cancel_build(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
) -> Any:
    """取消排队中或进行中的构建（IMP-039）。

    不持实例锁：构建线程可能正持有 ``instance_lock``；取消必须能并发介入。
    返回 :class:`~local_webpage_access.build_queue.CancelResult`。
    """
    from local_webpage_access.build_queue import get_build_queue

    _load(workspace, instance_id)  # 确认实例存在
    queue = get_build_queue(config, registry)
    return queue.cancel_build(instance_id)


def remove_instance(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
    *,
    purge: bool = False,
    force: bool = False,
) -> None:
    """移除实例（WBS-17.05 / WBS-17.10）。

    默认行为（``purge=False``）：
    1. 停止实例（容器 ``compose stop`` + ``down``；静态禁用网关 + 释放端口）；
    2. 删除 registry 中所有相关记录（级联 containers / static_sites / ports /
       events / builds / resources）；
    3. **保留** ``apps/<id>/`` 整个目录（含 data/），便于事后排查或重新导入。

    ``purge=True``：额外删除 ``apps/<id>/`` 整个目录。当 ``data/`` 非空时必须
    同时传 ``force=True``，避免误删数据库与上传文件（WBS-17.10）。

    IMP-041：各清理阶段写 INFO/WARNING 与 orphan ``remove_stage`` 事件，便于
    删除后对账；总览 orphan ``remove`` 事件（BUG-047）仍保留。
    """
    from local_webpage_access.docker_runtime import DockerRuntime
    from local_webpage_access.hosting import stop_instance

    with instance_lock(workspace, instance_id):
        manifest = _load_optional(workspace, instance_id)
        registry_row = registry.get_instance(instance_id) or {}

        # data/ 保护：purge 时若数据目录非空，必须显式 force
        data_dir = workspace.app_data(instance_id)
        data_nonempty = data_dir.is_dir() and any(data_dir.iterdir())
        if purge and data_nonempty and not force:
            _log_remove_stage(
                registry,
                instance_id,
                "data_guard",
                "fail",
                purge=purge,
                force=force,
                detail="data_nonempty",
            )
            raise DataNonemptyError(
                f"实例 {instance_id} 的 data/ 目录非空，删除前请确认"
                f"（使用 --force 强制删除数据）",
                instance_id=instance_id,
            )

        # 1. 先记 remove 事件，且以 orphan event（instance_id=NULL）写入（BUG-047）。
        #    events.instance_id 带 ON DELETE CASCADE，若关联实例行则删除时会被
        #    级联清除、审计链断裂。列定义本就 nullable，写 NULL 后不受级联影响，
        #    同时在 message 中保留实例 ID 文本，便于追溯。
        with contextlib.suppress(Exception):
            registry.add_event(
                None,
                "remove",
                f"移除实例 {instance_id}（purge={purge}, force={force}）",
            )
        _log_remove_stage(
            registry, instance_id, "begin", "ok", purge=purge, force=force
        )

        # 2. 停止实例（容忍缺失 manifest 或已停止）
        if manifest is not None:
            try:
                stop_instance(workspace, config, registry, instance_id)
                _log_remove_stage(
                    registry, instance_id, "stop", "ok", purge=purge, force=force
                )
            except LwaError as exc:
                # 停止失败不应阻塞移除；继续清理 registry / 别名 / 磁盘。
                _log_remove_stage(
                    registry,
                    instance_id,
                    "stop",
                    "warn",
                    purge=purge,
                    force=force,
                    detail=str(exc),
                )
            # 容器：彻底 down 释放容器（不删卷，data/ 是 bind mount 安全）
            if manifest.runtime.value == "docker-compose":
                try:
                    DockerRuntime(workspace, registry).down(instance_id)
                    _log_remove_stage(
                        registry,
                        instance_id,
                        "compose_down",
                        "ok",
                        purge=purge,
                        force=force,
                    )
                except Exception as exc:  # noqa: BLE001
                    _log_remove_stage(
                        registry,
                        instance_id,
                        "compose_down",
                        "warn",
                        purge=purge,
                        force=force,
                        detail=str(exc),
                    )
                if purge and manifest.container is not None:
                    image_ref = manifest.container.imageId or manifest.container.image
                    if image_ref:
                        with contextlib.suppress(Exception):
                            DockerRuntime(workspace, registry).remove_image(image_ref)
            else:
                _log_remove_stage(
                    registry,
                    instance_id,
                    "compose_down",
                    "skip",
                    purge=purge,
                    force=force,
                    detail="not docker-compose",
                )
        else:
            # BUG-319：manifest 可能被误删，但 registry 仍保有 runtime。
            # 按 registry 降级清理，避免容器或 builtin 服务泄漏。
            runtime_value = registry_row.get("runtime")
            if runtime_value == "docker-compose":
                try:
                    DockerRuntime(workspace, registry).down(instance_id)
                    _log_remove_stage(
                        registry, instance_id, "compose_down", "ok",
                        purge=purge, force=force, detail="registry fallback",
                    )
                except Exception as exc:  # noqa: BLE001
                    _log_remove_stage(
                        registry, instance_id, "compose_down", "warn",
                        purge=purge, force=force, detail=f"registry fallback: {exc}",
                    )
                _log_remove_stage(
                    registry, instance_id, "stop", "skip",
                    purge=purge, force=force, detail="no manifest; compose down used",
                )
            elif runtime_value == "shared-static":
                try:
                    from local_webpage_access.static_gateway import StaticGateway

                    StaticGateway(workspace, config).disable(instance_id)
                    result, detail = "ok", "registry fallback"
                except Exception as exc:  # noqa: BLE001
                    result, detail = "warn", f"registry fallback: {exc}"
                _log_remove_stage(
                    registry, instance_id, "stop", result,
                    purge=purge, force=force, detail=detail,
                )
                _log_remove_stage(
                    registry, instance_id, "compose_down", "skip",
                    purge=purge, force=force, detail="shared-static",
                )
            else:
                _log_remove_stage(
                    registry, instance_id, "stop", "skip",
                    purge=purge, force=force, detail="no manifest or runtime",
                )
                _log_remove_stage(
                    registry, instance_id, "compose_down", "skip",
                    purge=purge, force=force, detail="no manifest or runtime",
                )

        # 2.5 BUG-268 / IMP-041：全 runtime 清理路径别名（容器 stop 不走 disable）。
        had_alias = workspace.app_alias_config(instance_id).is_file()
        try:
            from local_webpage_access.static_gateway import StaticGateway

            StaticGateway(workspace, config).cleanup_instance_routes(instance_id)
            _log_remove_stage(
                registry,
                instance_id,
                "alias_cleanup",
                "ok" if had_alias else "skip",
                purge=purge,
                force=force,
                detail="" if had_alias else "no alias",
            )
        except Exception as exc:  # noqa: BLE001 — 别名清理失败不阻断 remove
            _log_remove_stage(
                registry,
                instance_id,
                "alias_cleanup",
                "warn",
                purge=purge,
                force=force,
                detail=str(exc),
            )

        # 3. 删除 registry 记录（级联）
        registry.delete_instance(instance_id)
        _log_remove_stage(
            registry,
            instance_id,
            "registry_delete",
            "ok",
            purge=purge,
            force=force,
        )

        # 3.5 清理浏览量统计（BUG-090）
        try:
            from local_webpage_access.pageviews import clear_instance_pageviews

            clear_instance_pageviews(workspace, instance_id)
            _log_remove_stage(
                registry,
                instance_id,
                "pageviews_clear",
                "ok",
                purge=purge,
                force=force,
            )
        except Exception as exc:  # noqa: BLE001
            _log_remove_stage(
                registry,
                instance_id,
                "pageviews_clear",
                "warn",
                purge=purge,
                force=force,
                detail=str(exc),
            )

        # 4. 可选：删除磁盘文件
        if purge:
            app_dir = workspace.app_dir(instance_id)
            # 防御纵深（BUG-025）：即便 instance_id 绕过入口校验，resolve 后
            # 必须仍落在 apps/ 之内，才允许 rmtree，杜绝越界删除。
            apps_root = workspace.apps.resolve()
            if app_dir.is_dir():
                resolved = app_dir.resolve()
                if not resolved.is_relative_to(apps_root):
                    _log_remove_stage(
                        registry,
                        instance_id,
                        "purge_tree",
                        "fail",
                        purge=purge,
                        force=force,
                        detail="path outside apps/",
                    )
                    raise LifecycleError(
                        f"实例 {instance_id} 的目录解析到 apps/ 之外，拒绝删除",
                        instance_id=instance_id,
                    )
                try:
                    # BUG-279：禁止 ignore_errors 假绿；失败须可观测并可重试
                    shutil.rmtree(resolved)
                except OSError as exc:
                    _log_remove_stage(
                        registry,
                        instance_id,
                        "purge_tree",
                        "fail",
                        purge=purge,
                        force=force,
                        detail=str(exc),
                    )
                    raise LifecycleError(
                        f"实例 {instance_id} 磁盘删除失败：{exc}",
                        instance_id=instance_id,
                    ) from exc
                if resolved.exists():
                    _log_remove_stage(
                        registry,
                        instance_id,
                        "purge_tree",
                        "fail",
                        purge=purge,
                        force=force,
                        detail="directory still exists after rmtree",
                    )
                    raise LifecycleError(
                        f"实例 {instance_id} 磁盘删除失败：未能删除 apps/{instance_id}",
                        instance_id=instance_id,
                    )
            _log_remove_stage(
                registry,
                instance_id,
                "purge_tree",
                "ok",
                purge=purge,
                force=force,
            )
        else:
            _log_remove_stage(
                registry,
                instance_id,
                "purge_tree",
                "skip",
                purge=purge,
                force=force,
                detail="purge=false",
            )

        _log_remove_stage(
            registry,
            instance_id,
            "done",
            "ok",
            purge=purge,
            force=force,
            detail="with_disk" if purge else "registry_only",
        )
        if purge:
            log.info("实例 %s 已移除（含磁盘文件）", instance_id)
        else:
            log.info("实例 %s 已从 registry 移除（保留 apps/ 目录）", instance_id)


def _log_remove_stage(
    registry: Registry,
    instance_id: str,
    stage: str,
    result: str,
    *,
    purge: bool,
    force: bool,
    detail: str = "",
) -> None:
    """IMP-041：删除阶段 INFO/WARNING + orphan ``remove_stage`` 事件。"""
    msg = (
        f"remove stage={stage} instance={instance_id} "
        f"purge={str(purge).lower()} force={str(force).lower()} "
        f"result={result}"
    )
    if detail:
        msg = f"{msg} detail={detail}"
    if result in ("warn", "fail"):
        log.warning("%s", msg)
    else:
        log.info("%s", msg)
    with contextlib.suppress(Exception):
        registry.add_event(None, "remove_stage", msg)


# ---- IMP-012：冗余实例批量清理 --------------------------------------------


def _instance_zip_fingerprint(workspace: Workspace, instance_id: str) -> str | None:
    """IMP-012：计算实例原始 zip 的 sha256 作为去重指纹；无 zip/读取失败返回 None。

    由同一 zip 导入的实例得到相同指纹，等价于 ``sourceZipHash``，但运行时从
    已落盘的原始 zip（``apps/<id>/original.zip``）重算，无需 registry 新增列。
    """
    zip_path = workspace.app_original_zip(instance_id)
    if not zip_path.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with zip_path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def list_redundant_instances(
    workspace: Workspace, registry: Registry
) -> list[dict[str, Any]]:
    """IMP-012：列出冗余实例（按原始 zip 指纹分组，保留 createdAt 最早者）。

    返回每个冗余实例的描述字典（``id`` / ``name`` / ``sourceZipHash`` /
    ``createdAt``），按 createdAt 升序。空指纹（无原始 zip 或读取失败）的实例
    不参与分组，避免误删无法证明同源的实例。每组保留最早者，其余视为冗余。
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in registry.list_instances():  # 已按 created_at ASC
        fp = _instance_zip_fingerprint(workspace, row["id"])
        if not fp:
            continue  # 空 hash 不参与
        groups.setdefault(fp, []).append(row)
    redundant: list[dict[str, Any]] = []
    for fp, members in groups.items():
        if len(members) < 2:
            continue
        # members 已按 created_at ASC：首个为最早者保留，其余冗余
        for row in members[1:]:
            redundant.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "sourceZipHash": fp,
                    "createdAt": row["created_at"],
                }
            )
    redundant.sort(key=lambda r: r["createdAt"] or "")
    return redundant


def remove_redundant(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    *,
    purge: bool = False,
    force: bool = False,
) -> list[str]:
    """IMP-012：批量移除冗余实例，返回被移除的 instance_id 列表。

    先 :func:`list_redundant_instances` 取目标，逐个调 :func:`remove_instance`
    （共享 stop + registry 清理 + 可选 purge 流程）。单实例移除失败不中断整体，
    仅记 warning；返回实际成功移除的 id。
    """
    targets = list_redundant_instances(workspace, registry)
    removed: list[str] = []
    for desc in targets:
        iid = desc["id"]
        try:
            remove_instance(workspace, config, registry, iid, purge=purge, force=force)
            removed.append(iid)
        except LwaError as exc:
            log.warning("移除冗余实例 %s 失败（跳过）：%s", iid, exc)
    log.info("冗余清理完成：移除 %d / 目标 %d", len(removed), len(targets))
    return removed


# ---- IMP-021：端口漂移时同步别名片段 ----------------------------------------


def _sync_alias_port(
    workspace: Workspace,
    config: Config,
    instance_id: str,
    manifest: InstanceManifest,
) -> bool:
    """IMP-021（WBS-20260708 阶段3.4）：容器/静态实例重启后若 hostPort 漂移，
    重写路径别名片段并 reload，避免别名入口 reverse_proxy 到已失效的旧端口。

    仅在 Caddy 后端、实例已配置别名、且别名片段记录的端口与当前 hostPort 不一致
    （或片段缺失）时触发；端口未变化为空操作（避免无谓 reload）。reload 失败仅记
    WARN——别名片段已按正确端口落盘，下次 ``caddy start``/reload 会加载它。
    """
    from local_webpage_access.path_alias import _current_alias, _resolve_host_port
    from local_webpage_access.static_gateway import StaticGateway

    gateway = StaticGateway(workspace, config)
    if gateway.detect_backend() != "caddy":
        return False
    alias = _current_alias(manifest)
    host_port, _ = _resolve_host_port(manifest)
    if not alias or host_port is None:
        return False

    conf_path = workspace.app_alias_config(instance_id)
    if conf_path.is_file():
        try:
            text = conf_path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        m = re.search(r"127\.0\.0\.1:(\d+)", text)
        if m and int(m.group(1)) == host_port:
            return False  # 端口未漂移，别名片段仍有效
    try:
        gateway.generate_alias_config(instance_id, alias, host_port, runtime=manifest.runtime.value)
        gateway.reload_all()
        log.info(
            "实例 %s 别名片段已按新端口 %d 重写并 reload（IMP-021）",
            instance_id,
            host_port,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — 别名同步失败不阻断主流程
        log.warning("同步实例 %s 别名端口失败：%s", instance_id, exc)
        return False


# ---- status 观测与回写（WBS-17.07）-----------------------------------------


def observe_status(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
) -> Status:
    """观测实例真实状态并回写 registry（WBS-17.07）。

    * 容器：``docker compose ps`` 判定 running / exited；
    * 静态：检查网关是否启用 + PID 是否存活。

    返回观测到的 :class:`Status`。仅做观测与回写，不改变 ``desiredState``。
    """
    with instance_lock(workspace, instance_id):
        return _observe_status_locked(workspace, config, registry, instance_id)


def _observe_status_locked(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
) -> Status:
    """在实例生命周期锁内执行真实状态观测及 manifest 回写（BUG-320）。"""
    manifest = _load(workspace, instance_id)
    runtime_value = manifest.runtime.value

    if runtime_value == "docker-compose":
        observed = _observe_container_status(workspace, registry, instance_id)
    elif runtime_value == "shared-static":
        observed = _observe_static_status(workspace, config, registry, instance_id)
    else:
        return Status(manifest.status.value if isinstance(manifest.status, Status) else manifest.status)

    # 仅在状态发生变化时回写，减少无谓写入
    current = (
        manifest.status.value if isinstance(manifest.status, Status) else manifest.status
    )
    if observed.value != current:
        registry.update_status(instance_id, observed.value)
        # BUG-320：只更新 status 相关字段，避免全量 save 覆盖并发生命周期写入。
        path = workspace.app_manifest_path(instance_id)
        try:
            fresh = InstanceManifest.load(path)
            fresh.status = observed
            fresh.touch()
            fresh.save(path)
        except Exception:  # noqa: BLE001
            with contextlib.suppress(Exception):
                manifest.status = observed
                manifest.touch()
                manifest.save(path)
        registry.add_event(
            instance_id,
            "status_change",
            f"状态变更：{current} → {observed.value}",
        )
        log.info("实例 %s 状态观测变更：%s → %s", instance_id, current, observed.value)
    return observed


def _observe_container_status(
    workspace: Workspace, registry: Registry, instance_id: str
) -> Status:
    """观测容器真实状态（WBS-17.07；IMP-033 正式观测模型）。

    * ``docker compose ps`` 成功且 running → ``RUNNING``；
    * 成功且非 running / 无容器 → ``STOPPED``；
    * 权限不足 / daemon 不可达 / 超时 / 解析失败 → ``observed_state=unknown``，
      **不覆盖** ``status`` / ``last_trusted_state``（BUG-230 止血升级）。
    * 权限不足时优先 hostPort HTTP 探活；可达则按 running 并记降级原因。
    """
    from local_webpage_access.capability import classify_docker_observation_error
    from local_webpage_access.docker_runtime import (
        DOCKER_PERMISSION_HINT,
        DockerRuntime,
        is_docker_permission_error,
    )
    from local_webpage_access.errors import DockerError
    from local_webpage_access.health import http_ok
    from local_webpage_access.logging import now_iso

    row = registry.get_instance(instance_id) or {}
    current_raw = row.get("status") or Status.STOPPED.value
    try:
        current = Status(current_raw)
    except ValueError:
        current = Status.STOPPED
    trusted = row.get("last_trusted_state") or current.value

    host_port: int | None = None
    crow = registry.get_container(instance_id)
    if crow and crow.get("host_port") is not None:
        try:
            host_port = int(crow["host_port"])
        except (TypeError, ValueError):
            host_port = None

    def _record_ok(status: Status) -> Status:
        registry.update_status(
            instance_id,
            status.value,
            last_error="",
            observed_state=status.value,
            observation_error=None,
            clear_observation_error=True,
            last_trusted_state=status.value,
            last_observed_at=now_iso(),
            runtime_access="ready",
        )
        return status

    def _record_unknown(
        *,
        error: str,
        msg: str,
        keep: Status,
        via_http: bool = False,
    ) -> Status:
        # BUG-369：写回前重读，避免入口快照覆盖并发更新的可信状态。
        latest = registry.get_instance(instance_id) or {}
        latest_trusted = (
            latest.get("last_trusted_state")
            or latest.get("status")
            or trusted
        )
        latest_status_raw = latest.get("status")
        if latest_status_raw:
            with contextlib.suppress(ValueError):
                keep = Status(latest_status_raw)
        suffix = "（观测已降级为 HTTP 探活）" if via_http else ""
        full_msg = f"{msg}{suffix}"
        observed_label = keep.value if via_http else "unknown"
        registry.update_status(
            instance_id,
            keep.value,  # 兼容字段：不把 unknown 写入旧 status
            last_error=full_msg[:500],
            observed_state=observed_label,
            observation_error=error,
            last_trusted_state=latest_trusted,
            last_observed_at=now_iso(),
            runtime_access=error,
        )
        with contextlib.suppress(Exception):
            registry.add_event(
                instance_id,
                "observe_degraded",
                f"observationError={error} observedState={observed_label} "
                f"lastTrustedState={latest_trusted} role=observer {full_msg[:200]}",
            )
        log.warning(
            "实例 %s：观测降级 observationError=%s，保留 status=%s",
            instance_id,
            error,
            keep.value,
        )
        return keep

    try:
        runtime = DockerRuntime(workspace, registry)
        if runtime.is_running(instance_id):
            return _record_ok(Status.RUNNING)
        return _record_ok(Status.STOPPED)
    except DockerError as exc:
        err_text = str(exc)
        kind = classify_docker_observation_error(err_text) or "unknown"
        if kind == "permission_denied" or is_docker_permission_error(err_text):
            kind = "permission_denied"
            if host_port is not None:
                ok, _code = http_ok(host_port)
                if ok:
                    log.warning(
                        "实例 %s：Docker 权限不足，但 HTTP :%s 可达，按 running 处理",
                        instance_id,
                        host_port,
                    )
                    registry.update_status(
                        instance_id,
                        Status.RUNNING.value,
                        last_error=(
                            f"Docker 权限不足，无法用 compose ps 观测"
                            f"（观测已降级为 HTTP 探活）；{DOCKER_PERMISSION_HINT}"
                        )[:500],
                        observed_state=Status.RUNNING.value,
                        observation_error="permission_denied",
                        last_trusted_state=Status.RUNNING.value,
                        last_observed_at=now_iso(),
                        runtime_access="permission_denied",
                    )
                    with contextlib.suppress(Exception):
                        registry.add_event(
                            instance_id,
                            "observe_degraded",
                            "observationError=permission_denied via=http "
                            f"observedState=running {DOCKER_PERMISSION_HINT[:120]}",
                        )
                    return Status.RUNNING
            return _record_unknown(
                error="permission_denied",
                msg=f"Docker 权限不足，无法用 compose ps 观测；{DOCKER_PERMISSION_HINT}",
                keep=current,
            )
        if kind in ("daemon_unavailable", "timeout", "parse_error", "unknown"):
            return _record_unknown(
                error=kind,
                msg=f"Docker 观测失败（{kind}）：{err_text}",
                keep=current,
            )
        return _record_unknown(
            error="unknown",
            msg=f"Docker 观测失败（unknown）：{err_text}",
            keep=current,
        )
    except Exception as exc:  # noqa: BLE001 — 观测失败不抛
        kind = classify_docker_observation_error(str(exc)) or "unknown"
        return _record_unknown(
            error=kind,
            msg=f"Docker 观测异常：{exc}",
            keep=current,
        )

def _observe_static_status(
    workspace: Workspace, config: Config, registry: Registry, instance_id: str
) -> Status:
    """观测静态实例真实状态（WBS-17.07；BUG-071 / DEV-043 修复）。

    判定优先级（**健康优先**，保证向后兼容）：

    1. 未启用 → ``STOPPED``；
    2. pid 存活 或 hostPort HTTP 可达 → ``RUNNING``（BUG-052 防御：pid 抖动/缺失但
       服务在跑时按 HTTP 兜底）；
    3. 既未 pid 存活也不可达时，按后端细化（DEV-043）：

       * Caddy 后端 + admin :2019 不可达 → ``GATEWAY_DOWN``（master 挂了，
         enabled 站点实际不可达——BUG-071：不再误标普通 stopped）；
       * Caddy 后端 + master 在线但站点端口不通 → ``CONFIG_INVALID``
         （路由/配置问题，BUG-069 类悬空 import 征兆）；
       * builtin 后端 → ``STOPPED``（进程已死，由 daemon reconcile 自愈）。
    """
    from local_webpage_access.static_gateway import StaticGateway

    row = registry.get_static_site(instance_id)
    if not row or not row.get("enabled"):
        return Status.STOPPED
    gateway = StaticGateway(workspace, config)
    host_port = row.get("host_port")

    # 1) pid 存活 → running
    pid_alive = False
    pid_path = workspace.run / f"static-{instance_id}.pid"
    if pid_path.is_file():
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except ValueError:
            pid = None
        if pid is not None:
            pid_alive = gateway._pid_alive(pid)
    if pid_alive:
        return Status.RUNNING
    # 2) hostPort HTTP 可达 → running（BUG-052：pid 缺失/抖动时 HTTP 兜底）
    if host_port is not None and gateway.health_check(int(host_port)):
        return Status.RUNNING

    # 3) 既无 pid 也不可达：Caddy 模式下区分"网关不可达"与"配置无效"（DEV-043）
    if gateway.detect_backend() == "caddy":
        if not gateway._admin_alive():
            return Status.GATEWAY_DOWN
        return Status.CONFIG_INVALID
    return Status.STOPPED


__all__ = [
    "instance_lock",
    "start_instance",
    "stop_instance_op",
    "restart_instance",
    "recover_instance",
    "rebuild_instance",
    "cancel_build",
    "remove_instance",
    "observe_status",
]
