"""实例状态汇总与同步（WBS-18.04 / 18.08 / 18.09 / 18.10）。

* :func:`instance_status` —— 单实例状态快照（合并 manifest + registry + 观测态），
  供 ``lwa status <id>`` 与管理页使用（WBS-18.04）；
* :func:`all_statuses` —— 全部实例状态快照；
* :func:`sync_status` —— 对单个或全部实例做观测回写（WBS-18.08），
  状态变化写入 events（WBS-18.10）。

状态流转（WBS-18.09）由各阶段动作驱动：

::

    pending → building → running ⇄ stopped
                    ↘ failed ↗
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from local_web_access.config import Config
from local_web_access.models import Status
from local_web_access.paths import Workspace
from local_web_access.registry import Registry


@dataclass
class InstanceStatus:
    """实例状态快照。"""

    id: str
    name: str
    kind: str
    runtime: str
    status: str
    desired_state: str
    host_port: int | None = None
    internal_port: int | None = None
    lan_url: str | None = None
    last_error: str | None = None
    last_started_at: str | None = None
    last_health_check_at: str | None = None
    updated_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "runtime": self.runtime,
            "status": self.status,
            "desiredState": self.desired_state,
            "hostPort": self.host_port,
            "internalPort": self.internal_port,
            "lanUrl": self.lan_url,
            "lastError": self.last_error,
            "lastStartedAt": self.last_started_at,
            "lastHealthCheckAt": self.last_health_check_at,
            "updatedAt": self.updated_at,
            **self.extra,
        }


def instance_status(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
) -> InstanceStatus:
    """构造单实例状态快照（WBS-18.04）。"""
    row = registry.get_instance(instance_id)
    if row is None:
        from local_web_access.errors import LifecycleError

        raise LifecycleError(
            f"实例 {instance_id} 不存在", instance_id=instance_id
        )

    host_port, internal_port = _resolve_ports(registry, instance_id, row["runtime"])
    lan_url = _resolve_lan_url(workspace, instance_id, host_port)

    return InstanceStatus(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        runtime=row["runtime"],
        status=row["status"],
        desired_state=row["desired_state"],
        host_port=host_port,
        internal_port=internal_port,
        lan_url=lan_url,
        last_error=row.get("last_error"),
        last_started_at=row.get("last_started_at"),
        last_health_check_at=row.get("last_health_check_at"),
        updated_at=row.get("updated_at"),
    )


def all_statuses(
    workspace: Workspace, config: Config, registry: Registry
) -> list[InstanceStatus]:
    """全部实例状态快照（按创建时间排序）。"""
    statuses: list[InstanceStatus] = []
    for row in registry.list_instances():
        statuses.append(instance_status(workspace, config, registry, row["id"]))
    return statuses


def sync_status(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str | None = None,
) -> dict[str, str]:
    """对单个或全部实例做状态观测回写（WBS-18.08 / 18.10）。

    返回 ``{instance_id: observed_status}``，仅包含**发生状态变化**的实例。
    """
    from local_web_access.lifecycle import observe_status

    ids = [instance_id] if instance_id else [r["id"] for r in registry.list_instances()]
    changed: dict[str, str] = {}
    for iid in ids:
        before = _registry_status(registry, iid)
        try:
            observed = observe_status(workspace, config, registry, iid)
        except Exception:  # noqa: BLE001 — 单实例观测失败不影响其它
            continue
        if observed.value != before:
            changed[iid] = observed.value
    return changed


def status_counts(registry: Registry) -> dict[str, int]:
    """各状态的实例计数，供管理页顶部统计（WBS-18.04）。"""
    return registry.status_counts()


# ---- 辅助 -------------------------------------------------------------------


def _resolve_ports(
    registry: Registry, instance_id: str, runtime: str
) -> tuple[int | None, int | None]:
    if runtime == "docker-compose":
        row = registry.get_container(instance_id)
        if row:
            return (
                _as_int(row.get("host_port")),
                _as_int(row.get("internal_port")),
            )
    else:
        row = registry.get_static_site(instance_id)
        if row:
            return _as_int(row.get("host_port")), None
    return None, None


def _resolve_lan_url(
    workspace: Workspace, instance_id: str, host_port: int | None
) -> str | None:
    if not host_port:
        return None
    manifest_path = workspace.app_manifest_path(instance_id)
    if not manifest_path.is_file():
        return None
    from local_web_access.models import InstanceManifest

    try:
        manifest = InstanceManifest.load(manifest_path)
    except Exception:  # noqa: BLE001
        return None
    if manifest.network and manifest.network.lanUrl:
        return manifest.network.lanUrl
    return None


def _registry_status(registry: Registry, instance_id: str) -> str | None:
    row = registry.get_instance(instance_id)
    return row["status"] if row else None


def _as_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


__all__ = [
    "InstanceStatus",
    "instance_status",
    "all_statuses",
    "sync_status",
    "status_counts",
]
