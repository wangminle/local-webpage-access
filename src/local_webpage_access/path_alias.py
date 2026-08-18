"""IMP-006：实例路径别名在线设置与清除（管理页 API / CLI 共用）。"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from local_webpage_access.config import Config
from local_webpage_access.errors import GatewayError, LifecycleError, RecognitionError
from local_webpage_access.file_lock import (
    ensure_lockable,
    release_exclusive,
    try_acquire_exclusive,
    write_lock_payload,
)
from local_webpage_access.logging import get_logger
from local_webpage_access.models import (
    InstanceManifest,
    NetworkConfig,
    RouteMode,
    Runtime,
    StaticConfig,
)
from local_webpage_access.paths import Workspace, validate_path_alias
from local_webpage_access.ports import build_network_entry
from local_webpage_access.registry import Registry
from local_webpage_access.static_gateway import StaticGateway

log = get_logger("path_alias")

# BUG-167：工作区级别名锁，串行化「查唯一性 → 写 manifest/子表/Caddy」全流程。
_ALIAS_LOCK_TIMEOUT = 30.0
_alias_thread_lock = threading.RLock()


@dataclass(frozen=True)
class PathAliasResult:
    instance_id: str
    alias: str | None
    route_url: str | None
    alias_entry_enabled: bool
    gateway_reloaded: bool
    unchanged: bool
    html_verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "instanceId": self.instance_id,
            "alias": self.alias,
            "routeUrl": self.route_url,
            "aliasEntryEnabled": self.alias_entry_enabled,
            "gatewayReloaded": self.gateway_reloaded,
            "unchanged": self.unchanged,
            "htmlVerified": self.html_verified,
        }


def _current_alias(manifest: InstanceManifest) -> str | None:
    """读取当前别名（静态站点或容器实例，IMP-014 放开容器别名后两者共用）。"""
    static = manifest.static
    if static is not None and static.routeMode == RouteMode.NAME.value and static.routeHost:
        return static.routeHost
    container = manifest.container
    if (
        container is not None
        and container.routeMode == RouteMode.NAME.value
        and container.routeHost
    ):
        return container.routeHost
    return None


def reject_alias_if_absolute_spa_assets(
    *,
    html: str | None,
    alias: str,
    instance_id: str,
) -> None:
    """入口 HTML 含绝对路径资源时拒绝设别名（IMP-023 / IMP-055 硬拦截）。

    路径别名 ``handle_path`` 会去前缀；浏览器仍按 ``/assets/...`` 打到统一入口根，
    常见结果是空 200 / 白屏。能证明会挂时明确失败，避免「设置成功但打不开」。
    ``html`` 为空（探不到入口）时不拦截——无法证明不可用。
    .. note:: IMP-055 撤销 docker-compose 豁免

        此前 BUG-465 曾为 docker-compose 追加全局 ``/assets`` 回退路由并跳过本守卫。
        IMP-055 收敛该回退（多实例争抢 ``/assets`` 且管不住 ``/api`` 与 Router），
        恢复对所有 runtime 的硬拦截。应用侧须按显式 base path 方案改造（方案 B）。
    """
    if not html:
        return
    from local_webpage_access.access import _extract_absolute_resources

    paths = _extract_absolute_resources(html)
    if not paths:
        return
    sample = ", ".join(paths[:3])
    more = "…" if len(paths) > 3 else ""
    raise RecognitionError(
        f"入口 HTML 含绝对路径资源（{sample}{more}），"
        f"设置路径别名 /{alias}/ 后浏览器会绕过别名加载这些资源，页面会白屏（IMP-023 / IMP-055）。\n"
        f"解决方法（方案 B - 显式、可配置的 base path，选一）：\n"
        f"  1. 构建时设 --base=/{alias}/（Vite: vite build --base=/{alias}/），"
        f"同步重建静态产物后重新设置别名；\n"
        f"  2. Vue Router 用 createWebHistory(import.meta.env.BASE_URL)，"
        f"前端 API 客户端从 BASE_URL 派生请求路径（如 /{alias}/api/v1）；\n"
        f"  3. 若无源码或无法重建（C 类），路径别名模型下无解，"
        f"请继续用 hostPort 端口直达。\n"
        f"注意：base: './' 可消除绝对资源路径但不推荐作为最终方案"
        f"（Router/API 仍需跟 BASE_URL）。",
        instance_id=instance_id,
    )


def _fetch_entrypoint_html_for_alias_guard(
    *,
    workspace: Workspace,
    manifest: InstanceManifest,
    host_port: int | None,
) -> str | None:
    """best-effort 取入口 HTML，供设别名前的 IMP-023 守卫。

    优先 GET ``http://127.0.0.1:{hostPort}/``；静态站再尝试磁盘 ``index.html``。
    失败返回 ``None``（调用方不拦截）。
    """
    from local_webpage_access.access import _fetch_text

    if host_port is not None:
        html = _fetch_text(f"http://127.0.0.1:{host_port}/")
        if html:
            return html

    if manifest.runtime == Runtime.SHARED_STATIC:
        root = workspace.app_current(manifest.id)
        for candidate in (root / "index.html", root / "public" / "index.html"):
            if not candidate.is_file():
                continue
            try:
                return candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
    return None


def _resolve_host_port(manifest: InstanceManifest) -> tuple[int | None, int | None]:
    """解析实例对外 hostPort / internalPort（静态站点或容器实例共用）。"""
    host_port: int | None = None
    internal_port: int | None = None
    if manifest.static is not None and manifest.static.hostPort is not None:
        host_port = manifest.static.hostPort
    if manifest.container is not None and manifest.container.hostPort is not None:
        host_port = host_port or manifest.container.hostPort
        internal_port = manifest.container.internalPort
    if manifest.network is not None:
        host_port = host_port or manifest.network.hostPort
        internal_port = internal_port or manifest.network.internalPort
    return host_port, internal_port


def _apply_manifest_alias(
    manifest: InstanceManifest,
    config: Config,
    alias: str | None,
) -> None:
    """写入 manifest.static（静态站点）或 manifest.container（容器，IMP-014）
    与 manifest.network（不持久化）。"""
    new_mode = RouteMode.NAME.value if alias else RouteMode.PORT.value
    if manifest.runtime == Runtime.DOCKER_COMPOSE:
        # IMP-014：容器别名写入 container.routeMode/routeHost，registry 容器表据此联动。
        if manifest.container is not None:
            manifest.container = manifest.container.model_copy(
                update={"routeMode": new_mode, "routeHost": alias}
            )
    else:
        static = manifest.static or StaticConfig()
        manifest.static = static.model_copy(
            update={
                "routeMode": new_mode,
                "routeHost": alias,
            }
        )

    host_port, internal_port = _resolve_host_port(manifest)
    if host_port is not None:
        entry = build_network_entry(
            config,
            host_port,
            internal_port=internal_port,
            path_alias=alias,
        )
        manifest.network = NetworkConfig(**entry)
        return

    if alias is None:
        manifest.network = manifest.network.model_copy(
            update={
                "routeMode": RouteMode.PORT.value,
                "routeHost": None,
                "routeUrl": None,
            }
        )
    else:
        manifest.network = manifest.network.model_copy(
            update={
                "routeMode": RouteMode.NAME.value,
                "routeHost": alias,
                "routeUrl": None,
            }
        )


def _rollback_alias_config(
    gateway: StaticGateway,
    instance_id: str,
    *,
    previous_alias: str | None,
    host_port: int | None,
    had_fragment: bool,
    previous_fragment: str | None,
    runtime: str | None = None,
) -> None:
    """Caddy reload 失败后恢复别名片段文件到变更前状态。"""
    path = gateway.ws.app_alias_config(instance_id)
    if had_fragment and previous_fragment is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(previous_fragment, encoding="utf-8")
    elif path.exists():
        path.unlink()
    elif previous_alias and host_port is not None:
        gateway.generate_alias_config(instance_id, previous_alias, host_port, runtime=runtime)


def _apply_gateway_alias(
    workspace: Workspace,
    config: Config,
    instance_id: str,
    alias: str | None,
    host_port: int | None,
    *,
    previous_alias: str | None,
    runtime: str,
) -> tuple[bool, bool]:
    """运行中实例同步 Caddy 别名片段。返回 (alias_entry_enabled, gateway_reloaded)。

    须在 manifest/registry 落盘**之前**调用：reload 失败时回滚别名片段并抛
    :class:`GatewayError`，调用方不得持久化新别名。

    静态站点（``runtime=shared-static``）仅在 ``gateway.is_enabled`` 时同步，
    与既有行为一致；容器实例（``runtime=docker-compose``，IMP-014）由 Docker
    托管进程、不经过 StaticGateway.enable，因此无 ``is_enabled`` 语义，只要
    Caddy 后端在线且 ``host_port`` 已知即生成别名片段（reverse_proxy hostPort）。
    """
    gateway = StaticGateway(workspace, config)
    backend = gateway.detect_backend()
    if host_port is None:
        return False, False
    if runtime == Runtime.SHARED_STATIC.value and not gateway.is_enabled(instance_id):
        return False, False

    if backend == "caddy":
        fragment_path = gateway.ws.app_alias_config(instance_id)
        had_fragment = fragment_path.is_file()
        previous_fragment = fragment_path.read_text(encoding="utf-8") if had_fragment else None
        try:
            if alias:
                gateway.generate_alias_config(instance_id, alias, host_port, runtime=runtime)
            else:
                gateway.remove_alias_config(instance_id)
            gateway.reload_all()
        except GatewayError:
            _rollback_alias_config(
                gateway,
                instance_id,
                previous_alias=previous_alias,
                host_port=host_port,
                had_fragment=had_fragment,
                previous_fragment=previous_fragment,
                runtime=runtime,
            )
            raise
        return bool(alias), True

    if alias:
        log.warning(
            "实例 %s 配置了路径别名 %s，但当前静态后端为 %s，别名入口未启用（仅通过端口 %s 访问）",
            instance_id,
            alias,
            backend,
            host_port,
        )
    gateway.remove_alias_config(instance_id)
    return False, False


def _alias_lock_path(workspace: Workspace):
    return workspace.run / "path-alias.lock"


def _alias_lock_is_stale(lock_path) -> bool:
    """别名锁是否可回收（进程已死或超时）。"""
    from local_webpage_access.lifecycle import _lock_is_stale

    return _lock_is_stale(lock_path)


@contextlib.contextmanager
def path_alias_lock(
    workspace: Workspace, *, timeout: float = _ALIAS_LOCK_TIMEOUT
) -> Iterator[None]:
    """工作区级路径别名互斥锁（BUG-167）。

    双层锁：进程内 ``RLock`` + 跨进程文件锁。须在 :func:`instance_lock` **之前**
    获取，避免与生命周期锁交叉死锁。
    """
    if not _alias_thread_lock.acquire(timeout=timeout):
        raise LifecycleError(f"路径别名锁等待超时（{timeout}s）")
    file_acquired = False
    fd: int | None = None
    lock_path = _alias_lock_path(workspace)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        ensure_lockable(fd)
        while True:
            try:
                try_acquire_exclusive(fd)
                write_lock_payload(fd, f"{os.getpid()}\n{time.time():.3f}\n".encode())
                file_acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LifecycleError(f"路径别名锁被占用，等待超时（{timeout}s）")
                time.sleep(0.05)
        yield
    finally:
        if file_acquired and fd is not None:
            release_exclusive(fd)
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        _alias_thread_lock.release()


def set_instance_path_alias(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
    alias: str | None,
) -> PathAliasResult:
    """设置或清除实例的路径别名 slug（IMP-006 静态站点 / IMP-014 容器实例）。

    BUG-167：持工作区别名锁 + 实例生命周期锁，并在锁内重新校验唯一性，
    避免并发「先查后写」写入重复别名或丢失 manifest 更新。
    """
    from local_webpage_access.lifecycle import instance_lock

    if alias is not None:
        alias = alias.strip() or None

    with path_alias_lock(workspace):
        with instance_lock(workspace, instance_id):
            return _set_instance_path_alias_locked(workspace, config, registry, instance_id, alias)


def _set_instance_path_alias_locked(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
    alias: str | None,
) -> PathAliasResult:
    """锁内实现：重新加载 manifest 后校验并落盘。"""
    mpath = workspace.app_manifest_path(instance_id)
    manifest = InstanceManifest.load(mpath)

    runtime = manifest.runtime
    if runtime not in (Runtime.SHARED_STATIC, Runtime.DOCKER_COMPOSE):
        raise RecognitionError(
            f"路径别名仅支持 shared-static / docker-compose 实例，当前为 {runtime.value}",
            instance_id=instance_id,
        )

    current = _current_alias(manifest)
    if alias == current:
        route_url = manifest.network.routeUrl if manifest.network else None
        return PathAliasResult(
            instance_id=instance_id,
            alias=current,
            route_url=route_url,
            alias_entry_enabled=False,
            gateway_reloaded=False,
            unchanged=True,
            html_verified=True,
        )

    if alias is not None:
        # 锁内再查一次，消除 TOCTOU（BUG-167）
        existing = set(registry.list_route_hosts(exclude_instance=instance_id).keys())
        validate_path_alias(alias, existing_aliases=existing)
        # IMP-022（WBS-20260708 阶段4.1）：路径别名依赖 Caddy 统一入口
        # （:{staticGatewayPort} 的 import 块），builtin 多端口模式无统一入口，
        # 别名设置了也访问不到。显式拦截，不再无声写元数据造成「设置成功但访问失败」。
        # 清除别名（alias=None）在 builtin 下仍允许（清除恒安全）。
        backend = StaticGateway(workspace, config).detect_backend()
        if backend != "caddy":
            raise RecognitionError(
                f"路径别名需要 Caddy 网关统一入口，当前静态后端为 {backend}（无 "
                f":{config.staticGatewayPort} 别名入口）。请先 `lwa gateway on` 启用 "
                f"Caddy（或安装 caddy 可执行文件），或继续通过 hostPort 端口直达。",
                instance_id=instance_id,
            )

    host_port, _ = _resolve_host_port(manifest)

    # IMP-023 / IMP-055：设别名前检测入口 HTML 绝对路径资源；
    # 能证明会白屏则硬失败（对齐 IMP-022）。清除别名（alias=None）恒安全，跳过。
    # 探不到 HTML 时不拦截（无法证明），但成功路径提示「未验证入口 HTML」。
    html_verified = False
    if alias is not None:
        html = _fetch_entrypoint_html_for_alias_guard(
            workspace=workspace, manifest=manifest, host_port=host_port
        )
        if html is not None:
            html_verified = True
            reject_alias_if_absolute_spa_assets(html=html, alias=alias, instance_id=instance_id)

    # 运行中 + Caddy：先网关重载，成功后再落盘，避免「manifest 已改但入口未生效」
    alias_entry_enabled, gateway_reloaded = _apply_gateway_alias(
        workspace,
        config,
        instance_id,
        alias,
        host_port,
        previous_alias=current,
        runtime=runtime.value,
    )

    _apply_manifest_alias(manifest, config, alias)
    manifest.save(mpath)

    # 持久化别名到对应子表：静态站点 / 容器实例（IMP-014 容器别名落 containers 表）
    if runtime == Runtime.DOCKER_COMPOSE and manifest.container is not None:
        registry.upsert_container(instance_id, manifest.container.model_dump())
    else:
        static_dump = manifest.static.model_dump() if manifest.static else {}
        registry.upsert_static_site(instance_id, static_dump)
    registry.add_event(
        instance_id,
        "path-alias",
        f"路径别名：{current or '(无)'} → {alias or '(无)'}",
    )

    route_url = manifest.network.routeUrl if manifest.network else None

    return PathAliasResult(
        instance_id=instance_id,
        alias=alias,
        route_url=route_url,
        alias_entry_enabled=alias_entry_enabled,
        gateway_reloaded=gateway_reloaded,
        unchanged=False,
        html_verified=html_verified,
    )


__all__ = [
    "PathAliasResult",
    "path_alias_lock",
    "reject_alias_if_absolute_spa_assets",
    "set_instance_path_alias",
]
