"""访问地址刷新 / 复核的共享编排（IMP-038 / IMP-040）。

供 ``lwa update``、``lwa gateway on``、``lwa doctor --access``、管理页列表旁路
与 daemon reconcile 共用，避免各入口各写一套 refresh/review 时序与结果模型。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from local_webpage_access.access import (
    AccessReviewReport,
    RefreshReport,
    refresh_network_entries,
    review_access,
)
from local_webpage_access.config import Config
from local_webpage_access.logging import get_logger
from local_webpage_access.paths import Workspace
from local_webpage_access.ports import resolve_lan_ip
from local_webpage_access.registry import Registry

log = get_logger("access_workflow")

# 管理页 / daemon 节流落盘默认间隔（秒）
DEFAULT_LAN_REFRESH_INTERVAL = 60.0
# resolve_lan_ip 短 TTL 缓存，避免列表接口每请求 UDP 探测
_LAN_IP_CACHE_TTL = 10.0

_throttle_lock = threading.Lock()
_inflight = False
_last_refresh_mono: float = 0.0
_last_resolved_lan_ip: str | None = None
_lan_ip_cache: tuple[float, str | None] | None = None


@dataclass
class AccessPassResult:
    """一次 refresh（+ 可选 review）的编排结果。"""

    refresh: RefreshReport | None = None
    review: AccessReviewReport | None = None
    refresh_error: str | None = None
    review_error: str | None = None
    skipped: bool = False
    skip_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"skipped": self.skipped}
        if self.skip_reason:
            d["skipReason"] = self.skip_reason
        if self.refresh is not None:
            d["refresh"] = self.refresh.to_dict()
        if self.refresh_error:
            d["refreshError"] = self.refresh_error
        if self.review is not None:
            d["review"] = self.review.to_dict()
        if self.review_error:
            d["reviewError"] = self.review_error
        return d


def reset_lan_refresh_throttle_state() -> None:
    """测试钩子：清空节流 / 单飞 / IP 缓存状态。"""
    global _inflight, _last_refresh_mono, _last_resolved_lan_ip, _lan_ip_cache
    with _throttle_lock:
        _inflight = False
        _last_refresh_mono = 0.0
        _last_resolved_lan_ip = None
        _lan_ip_cache = None


def cached_resolve_lan_ip(config: Config, *, ttl: float = _LAN_IP_CACHE_TTL) -> str | None:
    """进程内短 TTL 缓存的 :func:`resolve_lan_ip`。"""
    global _lan_ip_cache
    now = time.monotonic()
    cached = _lan_ip_cache
    if cached is not None and (now - cached[0]) <= ttl:
        return cached[1]
    ip = resolve_lan_ip(config)
    _lan_ip_cache = (now, ip)
    return ip


def run_access_pass(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    *,
    review: bool = True,
    dry_run: bool = False,
) -> AccessPassResult:
    """执行 access refresh，可选轻量 review（update / gateway / doctor 共用）。

    ``dry_run=True`` 时不探测、不写盘。refresh / review 异常彼此独立记录。
    """
    if dry_run:
        return AccessPassResult(skipped=True, skip_reason="dry-run")

    result = AccessPassResult()
    try:
        result.refresh = refresh_network_entries(workspace, config, registry)
    except Exception as exc:  # noqa: BLE001 — 单步失败不阻断后续 review
        result.refresh_error = str(exc)
        log.warning("access refresh 失败：%s", exc)

    if review:
        try:
            result.review = review_access(workspace, config, registry)
        except Exception as exc:  # noqa: BLE001
            result.review_error = str(exc)
            log.warning("access review 失败：%s", exc)
    return result


def _persisted_lan_hosts_differ(workspace: Workspace, registry: Registry, lan_ip: str) -> bool:
    """任一实例落盘 lanUrl host 与 ``lan_ip`` 不一致则视为漂移。"""
    from local_webpage_access.models import InstanceManifest

    for row in registry.list_instances():
        path = workspace.app_manifest_path(row["id"])
        if not path.is_file():
            continue
        try:
            manifest = InstanceManifest.load(path)
        except Exception:  # noqa: BLE001
            continue
        lan_url = manifest.network.lanUrl if manifest.network else None
        if not lan_url:
            continue
        host = urlparse(lan_url).hostname
        if host and host not in (lan_ip, "127.0.0.1"):
            return True
    return False


def maybe_throttled_lan_refresh(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    *,
    min_interval: float = DEFAULT_LAN_REFRESH_INTERVAL,
    force: bool = False,
) -> RefreshReport | None:
    """发现 LAN 漂移后节流 + 单飞调用 :func:`refresh_network_entries`（IMP-040 R2/R3）。

    * ``lanIpStrategy=manual``：**不**自动写盘（仅读时合成由 status 负责）。
    * 探测失败（``None``）：不写盘，避免批量写坏 manifest。
    * 同进程最短间隔 ``min_interval``；并发请求只跑一次。
    """
    global _inflight, _last_refresh_mono, _last_resolved_lan_ip

    if config.lanIpStrategy == "manual" and not force:
        return None

    lan_ip = cached_resolve_lan_ip(config)
    if not lan_ip:
        log.debug("LAN IP 探测失败，跳过节流 refresh（不写盘）")
        return None

    with _throttle_lock:
        drifted = bool(force)
        if not drifted:
            if _last_resolved_lan_ip is not None and _last_resolved_lan_ip != lan_ip:
                drifted = True
            elif _persisted_lan_hosts_differ(workspace, registry, lan_ip):
                drifted = True
        if not drifted:
            _last_resolved_lan_ip = lan_ip
            return None
        now = time.monotonic()
        if not force and _last_refresh_mono and (now - _last_refresh_mono) < min_interval:
            return None
        if _inflight:
            return None
        _inflight = True

    try:
        report = refresh_network_entries(workspace, config, registry)
        with _throttle_lock:
            _last_refresh_mono = time.monotonic()
            _last_resolved_lan_ip = lan_ip
        return report
    except Exception as exc:  # noqa: BLE001
        log.warning("节流 LAN refresh 失败：%s", exc)
        return None
    finally:
        with _throttle_lock:
            _inflight = False


__all__ = [
    "AccessPassResult",
    "DEFAULT_LAN_REFRESH_INTERVAL",
    "cached_resolve_lan_ip",
    "maybe_throttled_lan_refresh",
    "refresh_network_entries",
    "reset_lan_refresh_throttle_state",
    "review_access",
    "run_access_pass",
]
