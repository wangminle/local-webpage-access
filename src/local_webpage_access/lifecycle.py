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
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Iterator

from local_webpage_access.config import Config
from local_webpage_access.errors import LifecycleError, LwaError
from local_webpage_access.logging import get_logger
from local_webpage_access.models import DesiredState, InstanceManifest, Status
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry

log = get_logger("lifecycle")

_LOCK_TIMEOUT = 30.0  # 实例级锁默认等待上限（秒）
# 进程崩溃未释放锁时的兜底回收阈值：超过该时长视为陈旧锁。
_STALE_LOCK_SECONDS = 1800.0
# 心跳刷新间隔：明显小于 _STALE_LOCK_SECONDS，确保长耗时 rebuild/build
# 期间锁不会被误判陈旧（BUG-046）。取 staleness 的 1/3 且上限 300s。
_LOCK_HEARTBEAT_INTERVAL = min(_STALE_LOCK_SECONDS / 3.0, 300.0)

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

    用临时文件 + ``os.replace`` 原子替换，避免并发 ``_lock_is_stale`` 读到半写
    内容。锁文件不存在或不可读时为空操作（仅在已持锁时调用，故不应发生）。
    参考实现：``daemon.touch_lock_heartbeat``（BUG-030）。
    """
    try:
        content = lock_path.read_text(encoding="utf-8").strip().splitlines()
    except OSError:
        return
    pid_line = content[0] if content else str(os.getpid())
    tmp = lock_path.with_name(lock_path.name + ".hb")
    try:
        tmp.write_text(f"{pid_line}\n{time.time():.3f}\n", encoding="utf-8")
        os.replace(str(tmp), str(lock_path))
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


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
    2. 跨进程文件锁（``O_CREAT | O_EXCL``）—— 多个 ``lwa`` 进程串行。

    锁文件写入持有进程 PID 与时间戳，进程崩溃未释放时按
    :data:`_STALE_LOCK_SECONDS` 回收。超时仍拿不到锁抛
    :class:`LifecycleError`。

    长耗时操作（rebuild/build）期间以独立线程周期性刷新时间戳（BUG-046），
    避免超过 ``_STALE_LOCK_SECONDS`` 后被另一进程误回收导致跨进程并发。

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
    file_acquired = False
    lock_path = workspace.run / f"lifecycle-{instance_id}.lock"
    heartbeat_stop: threading.Event | None = None
    heartbeat_thread: threading.Thread | None = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            except FileExistsError:
                if _lock_is_stale(lock_path):
                    with contextlib.suppress(FileNotFoundError):
                        lock_path.unlink()
                    continue
                if time.monotonic() >= deadline:
                    raise LifecycleError(
                        f"实例 {instance_id} 正在被其他操作占用，等待超时（{timeout}s）",
                        instance_id=instance_id,
                    )
                time.sleep(0.1)
                continue
            os.write(fd, f"{os.getpid()}\n{time.time():.3f}\n".encode())
            os.close(fd)
            file_acquired = True
            break

        # 启动心跳线程：长耗时 rebuild/build 期间持续刷新时间戳，
        # 避免锁被误判陈旧（BUG-046）。daemon 锁采用轮询回调刷新
        # （watcher 本身是循环），lifecycle 的阻塞式 yield 无法轮询，
        # 故用后台线程。
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
            if file_acquired:
                with contextlib.suppress(FileNotFoundError, PermissionError):
                    lock_path.unlink()
    finally:
        tlock.release()


def _lock_is_stale(lock_path: Path) -> bool:
    """锁是否可回收：持有进程已不存活，或存活但超过 :data:`_STALE_LOCK_SECONDS`。"""
    try:
        content = lock_path.read_text(encoding="utf-8").strip().splitlines()
        pid = int(content[0]) if content else 0
        ts = float(content[1]) if len(content) > 1 else 0.0
    except (OSError, ValueError):
        return True
    if pid and _pid_alive(pid):
        # 进程仍在：仅超时才回收，避免误抢活跃锁
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
    return InstanceManifest.load(path)


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
) -> InstanceManifest:
    """启动实例（WBS-17.01）。

    * 容器实例已部署过 → 轻量 ``compose start``（:func:`start_container`）；
    * 否则（首次启动 / 静态 / 前端）→ 全量 :func:`host_instance`。
    最终 ``desiredState=running``。
    """
    from local_webpage_access.hosting import host_instance, start_container

    with instance_lock(workspace, instance_id):
        manifest = _load(workspace, instance_id)
        if _is_deployed_container(manifest):
            log.info("实例 %s 已部署，使用轻量 start", instance_id)
            return start_container(workspace, config, registry, instance_id)
        return host_instance(workspace, config, registry, instance_id)


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
    """
    from local_webpage_access.hosting import (
        host_instance,
        start_container,
        stop_instance,
    )

    with instance_lock(workspace, instance_id):
        manifest = _load(workspace, instance_id)
        deployed_container = _is_deployed_container(manifest)
        # 先停：容忍"本来就没在跑"的噪声（含 Docker/网关不可用等 stop 失败）
        try:
            stop_instance(workspace, config, registry, instance_id)
        except LwaError as exc:
            log.warning("restart 前停止失败（忽略并继续启动）：%s", exc)

        if deployed_container:
            return start_container(workspace, config, registry, instance_id)
        return host_instance(workspace, config, registry, instance_id)


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
        return queue.run(instance_id, _builder)


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
    """
    from local_webpage_access.docker_runtime import DockerRuntime
    from local_webpage_access.hosting import stop_instance

    with instance_lock(workspace, instance_id):
        manifest = _load_optional(workspace, instance_id)

        # data/ 保护：purge 时若数据目录非空，必须显式 force
        data_dir = workspace.app_data(instance_id)
        data_nonempty = data_dir.is_dir() and any(data_dir.iterdir())
        if purge and data_nonempty and not force:
            raise LifecycleError(
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

        # 2. 停止实例（容忍缺失 manifest 或已停止）
        if manifest is not None:
            try:
                stop_instance(workspace, config, registry, instance_id)
            except LwaError as exc:
                # 停止失败（Docker 不可用 / compose 缺失 / 网关异常等）不应阻塞移除，
                # remove 默认只需清 registry 索引；容器残留由后续 down 兜底。
                log.warning("移除前停止失败（继续清理）：%s", exc)
            # 容器：彻底 down 释放容器（不删卷，data/ 是 bind mount 安全）
            if manifest.runtime.value == "docker-compose":
                with contextlib.suppress(Exception):
                    DockerRuntime(workspace, registry).down(instance_id)

        # 3. 删除 registry 记录（级联）
        registry.delete_instance(instance_id)

        # 4. 可选：删除磁盘文件
        if purge:
            app_dir = workspace.app_dir(instance_id)
            # 防御纵深（BUG-025）：即便 instance_id 绕过入口校验，resolve 后
            # 必须仍落在 apps/ 之内，才允许 rmtree，杜绝越界删除。
            apps_root = workspace.apps.resolve()
            if app_dir.is_dir():
                resolved = app_dir.resolve()
                if not resolved.is_relative_to(apps_root):
                    raise LifecycleError(
                        f"实例 {instance_id} 的目录解析到 apps/ 之外，拒绝删除",
                        instance_id=instance_id,
                    )
                shutil.rmtree(resolved, ignore_errors=True)
            log.info("实例 %s 已移除（含磁盘文件）", instance_id)
        else:
            log.info("实例 %s 已从 registry 移除（保留 apps/ 目录）", instance_id)


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
        manifest.status = observed
        manifest.touch()
        with contextlib.suppress(Exception):
            manifest.save(workspace.app_manifest_path(instance_id))
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
    from local_webpage_access.docker_runtime import DockerRuntime

    try:
        runtime = DockerRuntime(workspace, registry)
        if runtime.is_running(instance_id):
            return Status.RUNNING
    except Exception as exc:  # noqa: BLE001 — 观测失败不抛
        log.warning("观测容器状态失败（%s），按 stopped 处理", exc)
    return Status.STOPPED


def _observe_static_status(
    workspace: Workspace, config: Config, registry: Registry, instance_id: str
) -> Status:
    from local_webpage_access.static_gateway import StaticGateway

    row = registry.get_static_site(instance_id)
    if not row or not row.get("enabled"):
        return Status.STOPPED
    gateway = StaticGateway(workspace, config)
    host_port = row.get("host_port")
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
    # PID 文件缺失或进程已退出时，仍以 HTTP 探测为准（BUG-052 防御）：
    # 跨线程 registry 误读或 PID 抖动不应把仍在服务的站点标为 stopped。
    if host_port is not None and gateway.health_check(int(host_port)):
        return Status.RUNNING
    return Status.STOPPED


__all__ = [
    "instance_lock",
    "start_instance",
    "stop_instance_op",
    "restart_instance",
    "rebuild_instance",
    "remove_instance",
    "observe_status",
]
