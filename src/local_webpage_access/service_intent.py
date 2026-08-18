"""自有服务（daemon/manager/gateway）的期望态判定（IMP-059.01）。

与实例级 ``desired_state`` reconcile 同构：服务级也有"用户想要的运行状态"——
``run/*.json`` 持久化的 ``enabled`` 字段就是意图来源，配置
（``managerEnabled`` / ``staticGateway``）做交叉校验。

设计约束（§14.3 / PLN-040）：

* **纯函数**：本模块只读状态文件与 config，不做任何 IO 副作用（不拉起、不写盘）；
* 意图缺失（状态文件不存在 / 未启用）一律按 ``disabled`` 处理——update 不会
  意外拉起从未开过的服务；
* 交叉校验向"停用"方向收敛：``manager.json enabled=true`` 但
  ``managerEnabled=false`` 时视为 disabled（不与用户配置对着干）；
* gateway 在 ``staticGateway != caddy`` 时为 ``NOT_APPLICABLE``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from local_webpage_access.config import Config
from local_webpage_access.logging import get_logger
from local_webpage_access.paths import Workspace

log = get_logger("service_intent")

INTENT_ENABLED = "enabled"
INTENT_DISABLED = "disabled"
INTENT_NOT_APPLICABLE = "n.a."

SERVICE_NAMES = ("daemon", "manager", "gateway")


@dataclass(frozen=True)
class ServiceIntent:
    """三服务的期望态（enabled / disabled / n.a.）。"""

    daemon: str
    manager: str
    gateway: str

    def get(self, name: str) -> str:
        return getattr(self, name)

    def to_dict(self) -> dict[str, str]:
        return {"daemon": self.daemon, "manager": self.manager, "gateway": self.gateway}


def _state_enabled(read_state_fn: Any, ws: Workspace) -> bool | None:
    """读取状态文件 enabled；文件缺失/损坏返回 ``None``（无法判定）。"""
    try:
        state = read_state_fn(ws)
    except Exception:  # noqa: BLE001 — 状态文件损坏按"未启用"处理，不阻断判定
        return None
    if state is None:
        return None
    return bool(state.enabled)


def service_intent(ws: Workspace, config: Config) -> ServiceIntent:
    """判定 daemon/manager/gateway 的期望态（无 IO 副作用）。

    * daemon：``run/daemon.json`` enabled（缺失 → disabled）；
    * manager：``run/manager.json`` enabled **且** ``config.managerEnabled``
      （交叉校验，任一为假 → disabled）；
    * gateway：``staticGateway != caddy`` → ``n.a.``；否则
      ``run/gateway.json`` enabled（缺失 → disabled）。
    """
    from local_webpage_access import daemon as daemon_mod
    from local_webpage_access import gateway_service
    from local_webpage_access import manager_service

    daemon_enabled = _state_enabled(daemon_mod.read_state, ws)
    daemon = INTENT_ENABLED if daemon_enabled else INTENT_DISABLED

    manager_enabled = _state_enabled(manager_service.read_state, ws)
    manager = INTENT_ENABLED if (manager_enabled and config.managerEnabled) else INTENT_DISABLED

    if config.staticGateway != "caddy":
        gateway = INTENT_NOT_APPLICABLE
    else:
        gateway_enabled = _state_enabled(gateway_service.read_state, ws)
        gateway = INTENT_ENABLED if gateway_enabled else INTENT_DISABLED

    return ServiceIntent(daemon=daemon, manager=manager, gateway=gateway)


def intent_enabled(intent: ServiceIntent, name: str) -> bool:
    """便捷谓词：指定服务意图是否为 enabled。"""
    return intent.get(name) == INTENT_ENABLED


# ---- 中断时长估算（059.04）---------------------------------------------------


def _parse_iso(value: str | None) -> float | None:
    if not value:
        return None
    from datetime import datetime

    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        from datetime import timezone

        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _daemon_lock_heartbeat_ts(ws: Workspace) -> float | None:
    """daemon 锁文件第二行的心跳时间戳（epoch 秒）。"""
    from local_webpage_access.daemon import lock_path

    path = lock_path(ws)
    try:
        content = path.read_text(encoding="utf-8").strip().splitlines()
        if len(content) >= 2:
            return float(content[1])
    except (OSError, ValueError):
        pass
    return None


def estimate_down_since(name: str, ws: Workspace) -> float | None:
    """估算服务"从何时开始不可用"（epoch 秒）；不可得返回 ``None``。

    * daemon：优先锁文件心跳时间戳（最后一次证明活着的时刻）；
    * manager/gateway：状态文件 ``started_at``（上次启动时刻，中断时长上界）；
    * 均不可得 → ``None``，调用方只说「意外未运行，已恢复」。
    """
    started_at: str | None = None
    if name == "daemon":
        ts = _daemon_lock_heartbeat_ts(ws)
        if ts is not None:
            return ts
        from local_webpage_access import daemon as daemon_mod

        state = None
        try:
            state = daemon_mod.read_state(ws)
        except Exception:  # noqa: BLE001
            state = None
        started_at = state.started_at if state else None
    elif name == "manager":
        from local_webpage_access import manager_service

        manager_state = None
        try:
            manager_state = manager_service.read_state(ws)
        except Exception:  # noqa: BLE001
            manager_state = None
        started_at = manager_state.started_at if manager_state else None
    elif name == "gateway":
        from local_webpage_access import gateway_service

        gateway_state = None
        try:
            gateway_state = gateway_service.read_state(ws)
        except Exception:  # noqa: BLE001
            gateway_state = None
        started_at = gateway_state.started_at if gateway_state else None
    else:
        return None
    return _parse_iso(started_at)


def format_down_duration(down_since: float | None, *, now: float | None = None) -> str:
    """把中断时长格式化为人类可读文案；不可得返回空串。

    返回形如「，中断约 3.5 小时」的前缀片段（含逗号），供拼接进消息；
    ``down_since`` 为 ``None`` 时返回 ""。
    """
    if down_since is None:
        return ""
    import time as time_mod

    now_ts = now if now is not None else time_mod.time()
    seconds = max(0.0, now_ts - down_since)
    if seconds < 90:
        text = f"{seconds:.0f} 秒"
    elif seconds < 90 * 60:
        text = f"{seconds / 60:.0f} 分钟"
    elif seconds < 36 * 3600:
        text = f"{seconds / 3600:.1f} 小时"
    else:
        text = f"{seconds / 86400:.1f} 天"
    return f"，中断约 {text}"


__all__ = [
    "INTENT_ENABLED",
    "INTENT_DISABLED",
    "INTENT_NOT_APPLICABLE",
    "SERVICE_NAMES",
    "ServiceIntent",
    "service_intent",
    "intent_enabled",
    "estimate_down_since",
    "format_down_duration",
]
