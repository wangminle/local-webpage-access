"""实例 reconcile 构建熔断（issue #35 建议 5）。

daemon 自愈对失败构建若按固定短间隔重试，长 apt 构建会在内存紧张时放大 OOM。
连续失败达到小时级退避；达到人工阈值后停止自动重试，直到 ``lwa start`` /
``lwa rebuild`` 清熔断。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from local_webpage_access.logging import now_iso

HOUR_THRESHOLD = 3
MANUAL_THRESHOLD = 5
HOUR_BACKOFF_SECONDS = 3600
BASE_BACKOFF_SECONDS = 60


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _now(now: datetime | None) -> datetime:
    if now is not None:
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc)
        return now
    return datetime.now(timezone.utc)


def record_failure(manifest: Any, *, now: datetime | None = None) -> None:
    """记一次 reconcile 失败并写入退避 / 人工熔断。"""
    stamp = _now(now)
    count = int(getattr(manifest, "consecutiveReconcileFailures", 0) or 0) + 1
    manifest.consecutiveReconcileFailures = count
    if count >= MANUAL_THRESHOLD:
        manifest.reconcileCircuitManual = True
        manifest.reconcileNextRetryAt = None
        return
    if count >= HOUR_THRESHOLD:
        delay = timedelta(seconds=HOUR_BACKOFF_SECONDS)
    else:
        delay = timedelta(seconds=BASE_BACKOFF_SECONDS * (2 ** (count - 1)))
    manifest.reconcileCircuitManual = False
    manifest.reconcileNextRetryAt = (stamp + delay).isoformat()


def clear_circuit(manifest: Any) -> None:
    manifest.consecutiveReconcileFailures = 0
    manifest.reconcileCircuitManual = False
    manifest.reconcileNextRetryAt = None


def is_blocked(manifest: Any, *, now: datetime | None = None) -> bool:
    if getattr(manifest, "reconcileCircuitManual", False):
        return True
    until = _parse_iso(getattr(manifest, "reconcileNextRetryAt", None))
    if until is None:
        return False
    return _now(now) < until


def status_note(manifest: Any, *, now: datetime | None = None) -> str | None:
    count = int(getattr(manifest, "consecutiveReconcileFailures", 0) or 0)
    if getattr(manifest, "reconcileCircuitManual", False):
        return (
            f"构建连续失败熔断中（{count} 次），已停止自动重试，需人工介入："
            "执行 lwa start / lwa rebuild 清除熔断并重试"
        )
    if not is_blocked(manifest, now=now):
        return None
    until = getattr(manifest, "reconcileNextRetryAt", None) or ""
    return (
        f"构建连续失败熔断中（{count} 次），下次自动重试 {until}；"
        "也可执行 lwa start / lwa rebuild 立即重试"
    )


def persist_circuit(workspace: Any, instance_id: str, manifest: Any) -> None:
    """把熔断字段落盘（失败静默，不阻断自愈主流程）。"""
    try:
        manifest.touch()
        manifest.save(workspace.app_manifest_path(instance_id))
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "BASE_BACKOFF_SECONDS",
    "HOUR_BACKOFF_SECONDS",
    "HOUR_THRESHOLD",
    "MANUAL_THRESHOLD",
    "clear_circuit",
    "is_blocked",
    "now_iso",
    "persist_circuit",
    "record_failure",
    "status_note",
]
