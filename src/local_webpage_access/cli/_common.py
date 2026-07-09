"""CLI 子模块共享工具：日志初始化、工作区定位、字节格式化。

DEV-044（WBS-20260708 阶段5.1）：从原 ``cli.py`` 抽出，供各命令子模块复用。
"""

from __future__ import annotations

from local_webpage_access.logging import get_logger, setup_logging

log = get_logger("cli")


def bootstrap(level: str = "INFO") -> None:
    """在每条命令执行前初始化日志（幂等）。"""
    setup_logging(level=level)  # type: ignore[arg-type]


def open_workspace_registry():
    """定位工作区并打开 registry，返回 (workspace, config, registry)。"""
    from local_webpage_access.config import load_config
    from local_webpage_access.paths import require_workspace
    from local_webpage_access.registry import Registry

    ws = require_workspace()
    config = load_config(ws)
    reg = Registry(ws.db_path)
    reg.open()
    return ws, config, reg


def fmt_bytes(n: int | None) -> str:
    """字节数格式化为人类可读。"""
    if n is None:
        return "-"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PiB"
