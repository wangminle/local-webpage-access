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

import contextlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from local_webpage_access.config import Config
from local_webpage_access.models import Status
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry

log = logging.getLogger("lwa.status")

# 孤儿 building 回收阈值（BUG-048）：构建进程崩溃后实例可能永久卡在 building。
# build_queue 默认 wait_timeout=1800s，Docker build 本身可能更长，故阈值取
# 3600s（1 小时）以明显大于真实最长构建，避免误杀正常进行中的构建。
_STALE_BUILDING_SECONDS = 3600.0


@dataclass
class InstanceStatus:
    """实例状态快照。"""

    id: str
    name: str
    kind: str
    runtime: str
    serving_mode: str
    resource_profile: str
    status: str
    desired_state: str
    stack: list[str] = field(default_factory=list)
    database: str | None = None
    host_port: int | None = None
    internal_port: int | None = None
    lan_url: str | None = None
    # IMP-006：路径别名（routeMode=name 时从 manifest.network 读取）
    route_host: str | None = None
    route_url: str | None = None
    source_size_bytes: int | None = None
    public_size_bytes: int | None = None
    data_size_bytes: int | None = None
    image_size_bytes: int | None = None
    last_memory_bytes: int | None = None
    last_cpu_percent: float | None = None
    last_error: str | None = None
    last_started_at: str | None = None
    last_health_check_at: str | None = None
    updated_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def port_mapping_label(self) -> str | None:
        """IMP-007：端口映射的人类可读标签（列表与详情统一口径）。

        * 容器实例同时有 internalPort 与 hostPort 且不同 → ``"8000→18100"``；
        * 二者相同、或 internalPort 缺失（静态托管）→ ``None``，前端只显示 hostPort，
          避免给静态站点展示误导性的 ``80→hostPort``。

        数据口径：``hostPort``=宿主访问端口；``internalPort``=容器/应用内部监听端口。
        """
        if (
            self.internal_port is not None
            and self.host_port is not None
            and self.internal_port != self.host_port
        ):
            return f"{self.internal_port}→{self.host_port}"
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "runtime": self.runtime,
            "servingMode": self.serving_mode,
            "resourceProfile": self.resource_profile,
            "status": self.status,
            "desiredState": self.desired_state,
            "stack": self.stack,
            "database": self.database,
            "hostPort": self.host_port,
            "internalPort": self.internal_port,
            "portMappingLabel": self.port_mapping_label,
            "lanUrl": self.lan_url,
            "routeHost": self.route_host,
            "routeUrl": self.route_url,
            "sourceSizeBytes": self.source_size_bytes,
            "publicSizeBytes": self.public_size_bytes,
            "dataSizeBytes": self.data_size_bytes,
            "imageSizeBytes": self.image_size_bytes,
            "lastMemoryBytes": self.last_memory_bytes,
            "lastCpuPercent": self.last_cpu_percent,
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
        from local_webpage_access.errors import LifecycleError

        raise LifecycleError(
            f"实例 {instance_id} 不存在", instance_id=instance_id
        )

    host_port, internal_port = _resolve_ports(registry, instance_id, row["runtime"])
    lan_url = _resolve_lan_url(workspace, instance_id, host_port)
    route_host, route_url = _resolve_route(workspace, instance_id)
    resources = registry.get_resources(instance_id) or {}

    return InstanceStatus(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        runtime=row["runtime"],
        serving_mode=row["serving_mode"],
        resource_profile=row["resource_profile"],
        status=row["status"],
        desired_state=row["desired_state"],
        stack=_parse_stack(row.get("stack_json")),
        database=row.get("database_type"),
        host_port=host_port,
        internal_port=internal_port,
        lan_url=lan_url,
        route_host=route_host,
        route_url=route_url,
        source_size_bytes=_as_int(resources.get("source_size_bytes")),
        public_size_bytes=_as_int(resources.get("public_size_bytes")),
        data_size_bytes=_as_int(resources.get("data_size_bytes")),
        image_size_bytes=_as_int(resources.get("image_size_bytes")),
        last_memory_bytes=_as_int(resources.get("last_memory_bytes")),
        last_cpu_percent=_as_float(resources.get("last_cpu_percent")),
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
    from local_webpage_access.lifecycle import observe_status

    ids = [instance_id] if instance_id else [r["id"] for r in registry.list_instances()]
    changed: dict[str, str] = {}
    for iid in ids:
        before = _registry_status(registry, iid)
        if before in {
            Status.PENDING.value,
            Status.QUEUED.value,
        }:
            continue
        if before == Status.BUILDING.value:
            # 孤儿 building 回收（BUG-048）：构建进程崩溃后 sync_status 会
            # 永远跳过 building 状态，实例卡死。先判断是否 stale，是则回写
            # failed 并清理孤儿 builds 行；未超时则保留 building（观测无法
            # 判定仍在进行的构建，交给构建流程自己收尾）。
            if _recover_stale_building(registry, iid):
                changed[iid] = Status.FAILED.value
            continue
        try:
            observed = observe_status(workspace, config, registry, iid)
        except Exception:  # noqa: BLE001 — 单实例观测失败不影响其它
            continue
        if observed.value != before:
            changed[iid] = observed.value
    return changed


def _recover_stale_building(registry: Registry, instance_id: str) -> bool:
    """检测并回收孤儿 building 状态（BUG-048）。

    判据（任一满足即视为孤儿）：
    1. 该实例最新 builds 行 status=running 且 started_at 距今超过阈值
       （覆盖前端/容器构建——它们进入 building 时会 add_build）；
    2. 无 builds 行（如静态托管 host_static 只写 status 不写 builds），
       但实例 updated_at 距今超过阈值（粗略兜底）。

    回收动作：把实例 status 置 failed、写 last_error、写 build_recover 事件、
    把孤儿 running builds 行标记为 failed。返回是否执行了回收。
    """
    builds = registry.list_builds(instance_id, limit=1)
    is_stale = False
    detail = ""
    if builds:
        latest = builds[0]
        if latest.get("status") == "running":
            started_at = latest.get("started_at")
            if started_at and _age_seconds(started_at) > _STALE_BUILDING_SECONDS:
                is_stale = True
                detail = f"构建已运行 {_age_seconds(started_at):.0f}s 未结束"
                # 收尾孤儿 builds 行
                try:
                    registry.finish_build(
                        latest["id"],
                        status="failed",
                        error_summary="构建进程疑似崩溃（stale building 回收）",
                    )
                except Exception:  # noqa: BLE001
                    pass
    else:
        # 无 builds 行：用 instances.updated_at 兜底（host_static 路径）
        row = registry.get_instance(instance_id)
        if row:
            updated_at = row.get("updated_at")
            if updated_at and _age_seconds(updated_at) > _STALE_BUILDING_SECONDS:
                is_stale = True
                detail = f"building 状态已停留 {_age_seconds(updated_at):.0f}s"

    if not is_stale:
        return False

    error_msg = f"构建进程疑似崩溃，已自动回收 stale building（{detail}）"
    registry.update_status(
        instance_id, Status.FAILED.value, last_error=error_msg
    )
    with contextlib.suppress(Exception):
        registry.add_event(instance_id, "build_recover", error_msg)
    log.warning("实例 %s %s", instance_id, error_msg)
    return True


def _age_seconds(iso_ts: str) -> float:
    """ISO 时间戳距今的秒数（解析失败返回 0，保守视为未超时）。"""
    try:
        # 兼容带/不带微秒、带/不带时区的 ISO 字符串
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        return max(0.0, (now - dt).total_seconds())
    except (ValueError, TypeError):
        return 0.0


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
    from local_webpage_access.models import InstanceManifest

    try:
        manifest = InstanceManifest.load(manifest_path)
    except Exception:  # noqa: BLE001
        return None
    if manifest.network and manifest.network.lanUrl:
        return manifest.network.lanUrl
    return None


def _resolve_route(
    workspace: Workspace, instance_id: str
) -> tuple[str | None, str | None]:
    """IMP-006：读取路径别名与统一入口 URL（``routeMode=name`` 时）。"""
    manifest_path = workspace.app_manifest_path(instance_id)
    if not manifest_path.is_file():
        return None, None
    from local_webpage_access.models import InstanceManifest

    try:
        manifest = InstanceManifest.load(manifest_path)
    except Exception:  # noqa: BLE001
        return None, None
    net = manifest.network
    if net and net.routeMode == "name":
        return net.routeHost, net.routeUrl
    return None, None


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


def _as_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_stack(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw]
    try:
        data = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data]


__all__ = [
    "InstanceStatus",
    "instance_status",
    "all_statuses",
    "sync_status",
    "status_counts",
]
