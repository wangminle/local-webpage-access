"""实例操作时间线（S3-04 / DEV-160 / issue #59）。

把三类记录按时间倒序合并为同一列表：

* ``event``：registry 事件（生命周期阶段、配置变更、导入等），带发起方 ``source``；
* ``build``：构建记录（builds 表），附耗时；
* ``operation``：registry ``last_operation_json``（D4 操作态，权威来源）；
  调用方传入的 manifest ``lastOperation`` 仅作缺省回退。

时间线只读，不改变任何状态；API、管理页抽屉与 ``lwa logs --timeline`` 共用本模块。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

TIMELINE_DEFAULT_LIMIT = 50
TIMELINE_MAX_LIMIT = 200
UNKNOWN_SOURCE = "unknown"


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _duration_seconds(started: str | None, finished: str | None) -> int | None:
    if not started or not finished:
        return None
    a = _parse_ts(started)
    b = _parse_ts(finished)
    if a is None or b is None:
        return None
    try:
        return max(0, int((b - a).total_seconds()))
    except TypeError:
        return None


def _sort_key(item: dict[str, Any]) -> tuple[int, Any]:
    parsed = _parse_ts(str(item.get("at") or ""))
    if parsed is None:
        return (0, str(item.get("at") or ""))
    try:
        return (1, parsed.timestamp())
    except (OverflowError, OSError, ValueError):
        return (0, str(item.get("at") or ""))


def _registry_last_operation(registry: Any, instance_id: str) -> dict[str, Any] | None:
    """D4 权威操作态：registry.last_operation_json（构建失败可能只写这一列）。"""
    try:
        row = registry.get_instance(instance_id) or {}
    except Exception:  # noqa: BLE001 — 时间线只读，registry 异常时回退
        return None
    raw = row.get("last_operation_json")
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def build_timeline(
    registry: Any,
    instance_id: str,
    *,
    last_operation: dict[str, Any] | None = None,
    limit: int = TIMELINE_DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """返回实例时间线（新→旧），最多 ``limit`` 条。

    优先读 registry 的 ``last_operation_json``；仅当该列为空时才用传入的
    manifest ``lastOperation``。
    """
    limit = max(1, min(int(limit), TIMELINE_MAX_LIMIT))
    last_operation = _registry_last_operation(registry, instance_id) or last_operation
    items: list[dict[str, Any]] = []

    for row in registry.list_events(instance_id, limit=limit):
        items.append(
            {
                "at": row.get("created_at"),
                "kind": "event",
                "type": row.get("event_type"),
                "status": None,
                "source": row.get("source") or UNKNOWN_SOURCE,
                "message": row.get("message") or "",
                "durationSeconds": None,
            }
        )

    for row in registry.list_builds(instance_id, limit=limit):
        status = row.get("status")
        message = row.get("error_summary") or f"构建 #{row.get('id')} {status or ''}".strip()
        items.append(
            {
                "at": row.get("finished_at") or row.get("started_at"),
                "kind": "build",
                "type": "build",
                "status": status,
                "source": UNKNOWN_SOURCE,
                "message": message,
                "durationSeconds": _duration_seconds(
                    row.get("started_at"), row.get("finished_at")
                ),
            }
        )

    if last_operation:
        op = dict(last_operation)
        at = op.get("finishedAt") or op.get("startedAt")
        items.append(
            {
                "at": at,
                "kind": "operation",
                "type": op.get("kind"),
                "status": op.get("status"),
                "source": op.get("source") or UNKNOWN_SOURCE,
                "message": op.get("error") or "最近一次操作",
                "durationSeconds": _duration_seconds(op.get("startedAt"), op.get("finishedAt")),
            }
        )

    items = [item for item in items if item.get("at")]
    items.sort(key=_sort_key, reverse=True)
    return items[:limit]


def format_timeline_lines(items: list[dict[str, Any]]) -> list[str]:
    """CLI 文本行：``时间  [来源] 类型 状态  说明``（耗时非空时附加）。"""
    lines: list[str] = []
    for item in items:
        status = item.get("status")
        head = f"{item.get('at')}  [{item.get('source')}] {item.get('type')}"
        if status:
            head += f" ({status})"
        duration = item.get("durationSeconds")
        if duration is not None:
            head += f" 耗时 {duration}s"
        lines.append(f"{head}  {item.get('message') or ''}".rstrip())
    return lines


__all__ = [
    "TIMELINE_DEFAULT_LIMIT",
    "TIMELINE_MAX_LIMIT",
    "build_timeline",
    "format_timeline_lines",
]
