"""IMP-006：实例路径别名在线设置与清除（管理页 API / CLI 共用）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from local_webpage_access.config import Config
from local_webpage_access.errors import GatewayError, RecognitionError
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


@dataclass(frozen=True)
class PathAliasResult:
    instance_id: str
    alias: str | None
    route_url: str | None
    alias_entry_enabled: bool
    gateway_reloaded: bool
    unchanged: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "instanceId": self.instance_id,
            "alias": self.alias,
            "routeUrl": self.route_url,
            "aliasEntryEnabled": self.alias_entry_enabled,
            "gatewayReloaded": self.gateway_reloaded,
            "unchanged": self.unchanged,
        }


def _current_alias(manifest: InstanceManifest) -> str | None:
    static = manifest.static
    if static is not None and static.routeMode == RouteMode.NAME.value and static.routeHost:
        return static.routeHost
    return None


def _resolve_host_port(manifest: InstanceManifest) -> tuple[int | None, int | None]:
    host_port: int | None = None
    internal_port: int | None = None
    if manifest.static is not None and manifest.static.hostPort is not None:
        host_port = manifest.static.hostPort
    if manifest.network is not None:
        host_port = host_port or manifest.network.hostPort
        internal_port = manifest.network.internalPort
    return host_port, internal_port


def _apply_manifest_alias(
    manifest: InstanceManifest,
    config: Config,
    alias: str | None,
) -> None:
    """写入 manifest.static / manifest.network（不持久化）。"""
    static = manifest.static or StaticConfig()
    manifest.static = static.model_copy(
        update={
            "routeMode": (RouteMode.NAME.value if alias else RouteMode.PORT.value),
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
) -> None:
    """Caddy reload 失败后恢复别名片段文件到变更前状态。"""
    path = gateway.ws.app_alias_config(instance_id)
    if had_fragment and previous_fragment is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(previous_fragment, encoding="utf-8")
    elif path.exists():
        path.unlink()
    elif previous_alias and host_port is not None:
        gateway.generate_alias_config(instance_id, previous_alias, host_port)


def _apply_gateway_alias(
    workspace: Workspace,
    config: Config,
    instance_id: str,
    alias: str | None,
    host_port: int | None,
    *,
    previous_alias: str | None,
) -> tuple[bool, bool]:
    """运行中实例同步 Caddy 别名片段。返回 (alias_entry_enabled, gateway_reloaded)。

    须在 manifest/registry 落盘**之前**调用：reload 失败时回滚别名片段并抛
    :class:`GatewayError`，调用方不得持久化新别名。
    """
    gateway = StaticGateway(workspace, config)
    backend = gateway.detect_backend()
    if not gateway.is_enabled(instance_id) or host_port is None:
        return False, False

    if backend == "caddy":
        fragment_path = gateway.ws.app_alias_config(instance_id)
        had_fragment = fragment_path.is_file()
        previous_fragment = (
            fragment_path.read_text(encoding="utf-8") if had_fragment else None
        )
        try:
            if alias:
                gateway.generate_alias_config(instance_id, alias, host_port)
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
            )
            raise
        return bool(alias), True

    if alias:
        log.warning(
            "实例 %s 配置了路径别名 %s，但当前静态后端为 %s，别名入口未启用"
            "（仅通过端口 %s 访问）",
            instance_id,
            alias,
            backend,
            host_port,
        )
    gateway.remove_alias_config(instance_id)
    return False, False


def set_instance_path_alias(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
    alias: str | None,
) -> PathAliasResult:
    """设置或清除 shared-static 实例的路径别名 slug。"""
    if alias is not None:
        alias = alias.strip() or None

    mpath = workspace.app_manifest_path(instance_id)
    manifest = InstanceManifest.load(mpath)

    if manifest.runtime != Runtime.SHARED_STATIC:
        raise RecognitionError(
            f"路径别名仅支持 shared-static 实例，当前为 {manifest.runtime.value}",
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
        )

    if alias is not None:
        existing = set(registry.list_route_hosts(exclude_instance=instance_id).keys())
        validate_path_alias(alias, existing_aliases=existing)

    host_port, _ = _resolve_host_port(manifest)

    # 运行中 + Caddy：先网关重载，成功后再落盘，避免「manifest 已改但入口未生效」
    alias_entry_enabled, gateway_reloaded = _apply_gateway_alias(
        workspace,
        config,
        instance_id,
        alias,
        host_port,
        previous_alias=current,
    )

    _apply_manifest_alias(manifest, config, alias)
    manifest.save(mpath)

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
    )


__all__ = ["PathAliasResult", "set_instance_path_alias"]
