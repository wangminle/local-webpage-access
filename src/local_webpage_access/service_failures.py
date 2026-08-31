"""自有服务启动失败观测与自动拉起熔断（IMP-064.01 / 064.04）。

``run/{manager,daemon,gateway}.json`` 的 ``enabled`` 回归纯用户意图
（§16.2 写入契约）；失败事实由本模块的字段承载：

* ``last_start_error``：最近一次启动失败（message / at / source）；
* ``consecutive_start_failures``：连续失败计数——启动成功（含已在运行
  早退）清零，用户级 ``off`` 重置。

熔断（064.04）只拦 ``updater`` reconcile 的**自动拉起**（连续 ≥3 次且
24h 内），不进 ``start_*``、不挡手动 ``lwa <svc> on``、不约束监督器
KeepAlive / JobRestart。纯函数无 IO 副作用。

存量污染（本 IMP 落地前已写成 ``enabled=false`` 且无失败记录的文件）与
真·用户 off 无法区分，读侧按 disabled 处理、不自动翻回（§16.2）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from local_webpage_access.logging import now_iso

# 连续失败达到该次数且最近一次失败在窗口内 → 熔断自动拉起（064.04）。
START_FAILURE_THRESHOLD = 3
# 熔断冷却窗口：最近一次失败距今超过该秒数则放行再试一次（计数保留）。
START_FAILURE_WINDOW_SECONDS = 24 * 3600

# last_start_error.source 闭集（§16.2）：谁触发了这次启动。
START_FAILURE_SOURCES = frozenset({"manual", "update-restart", "reconcile", "autostart"})


@dataclass
class LastStartError:
    """最近一次启动失败的观测记录（非用户意图）。"""

    message: str
    at: str
    source: str = "manual"

    def to_dict(self) -> dict[str, Any]:
        return {"message": self.message, "at": self.at, "source": self.source}


def parse_last_start_error(data: dict[str, Any]) -> LastStartError | None:
    """从状态文件 JSON 解析 ``last_start_error``；缺失/损坏返回 ``None``。

    旧状态文件无此字段 → ``None``（默认值语义，不做 schema 迁移）。
    """
    raw = data.get("last_start_error")
    if not isinstance(raw, dict):
        return None
    message = raw.get("message")
    if not isinstance(message, str) or not message:
        return None
    at = raw.get("at")
    source = raw.get("source")
    return LastStartError(
        message=message,
        at=str(at) if isinstance(at, str) and at else now_iso(),
        source=str(source) if source in START_FAILURE_SOURCES else "manual",
    )


def parse_consecutive_failures(data: dict[str, Any]) -> int:
    """解析 ``consecutive_start_failures``；缺失/非法按 0。"""
    raw = data.get("consecutive_start_failures", 0)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def record_start_failure(
    state: Any,
    message: str,
    *,
    source: str = "manual",
) -> None:
    """把一次启动失败写入状态对象（调用方负责 ``write_state`` 落盘）。

    仅更新观测字段，不改 ``enabled``（064.02 去污染核心契约）。
    """
    count = int(getattr(state, "consecutive_start_failures", 0) or 0)
    state.consecutive_start_failures = count + 1
    state.last_start_error = LastStartError(
        message=str(message)[:500],
        at=now_iso(),
        source=source if source in START_FAILURE_SOURCES else "manual",
    )


def clear_start_failures(state: Any) -> None:
    """启动成功 / 用户级 off 时清零失败观测（064.02 / 064.06）。"""
    state.consecutive_start_failures = 0
    state.last_start_error = None


def has_start_failures(state: Any) -> bool:
    """状态是否携带未清零的失败观测（供「仅在变化时写盘」判断）。"""
    return bool(
        getattr(state, "consecutive_start_failures", 0)
        or getattr(state, "last_start_error", None)
    )


def _parse_iso_ts(value: str | None) -> float | None:
    if not value:
        return None
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def start_failure_circuit_open(
    state: Any,
    *,
    now: float | None = None,
    threshold: int = START_FAILURE_THRESHOLD,
    window_seconds: float = START_FAILURE_WINDOW_SECONDS,
) -> bool:
    """064.04 熔断判定（纯函数）：是否应跳过 reconcile 自动拉起。

    连续失败 ≥ ``threshold`` 且最近一次失败距今 ≤ ``window_seconds`` → True。
    冷却过期（>窗口）返回 False（放行再试一次；计数保留，再失败按新 at
    重新熔断）。无失败记录恒 False。
    """
    import time as time_mod

    count = int(getattr(state, "consecutive_start_failures", 0) or 0)
    if count < threshold:
        return False
    err = getattr(state, "last_start_error", None)
    if err is None:
        return False
    at = _parse_iso_ts(getattr(err, "at", None))
    if at is None:
        return False
    now_ts = now if now is not None else time_mod.time()
    return (now_ts - at) <= window_seconds


def failure_note(state: Any) -> str | None:
    """doctor / status 用的失败摘要；无失败记录返回 ``None``。"""
    err = getattr(state, "last_start_error", None)
    if err is None:
        return None
    count = int(getattr(state, "consecutive_start_failures", 0) or 0)
    note = f"上次启动失败：{err.message}（{err.at}，source={err.source}）"
    if count > 1:
        note += f"；连续失败 {count} 次"
    return note


__all__ = [
    "LastStartError",
    "START_FAILURE_SOURCES",
    "START_FAILURE_THRESHOLD",
    "START_FAILURE_WINDOW_SECONDS",
    "clear_start_failures",
    "failure_note",
    "has_start_failures",
    "parse_consecutive_failures",
    "parse_last_start_error",
    "record_start_failure",
    "start_failure_circuit_open",
]
