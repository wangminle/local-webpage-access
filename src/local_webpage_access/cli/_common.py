"""CLI 子模块共享工具：日志初始化、工作区定位、字节格式化。

DEV-044（WBS-20260708 阶段5.1）：从原 ``cli.py`` 抽出，供各命令子模块复用。
"""

from __future__ import annotations

from local_webpage_access.logging import get_logger, setup_logging

log = get_logger("cli")


def bootstrap(level: str = "INFO") -> None:
    """在每条命令执行前初始化日志（幂等）。

    IMP-034.01：若已能定位工作区，把 ``local_webpage_access.*`` 追加到
    ``logs/lwa.log``（0600）；尚未 init 的命令仍仅控制台。
    """
    log_dir = None
    try:
        from local_webpage_access.paths import Workspace, find_workspace_root

        root = find_workspace_root()
        if root is not None:
            log_dir = Workspace(root).logs
    except Exception:  # noqa: BLE001 — 日志初始化不得阻断 CLI
        log_dir = None
    setup_logging(level=level, log_dir=log_dir)  # type: ignore[arg-type]


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


def coordinated_autostart_disable(ws, service_name: str) -> tuple[str | None, bool]:
    """IMP-030/030.b：``lwa X off`` 前若自启动单元已加载/启用则先停用，避免被立刻拉回。

    返回 ``(note, ok)``：``note`` 为提示文本（有动作时）；``ok=False`` 表示停用失败，
    调用方应**阻断后续 stop**（KeepAlive/Restart 会立即把进程拉回，off 无法生效，
    应提示用户先 ``lwa autostart disable`` 再停服，BUG-147）。
    """
    try:
        from local_webpage_access import autostart as asm

        res = asm.coordinated_disable(ws, service_name)
        return res.note, res.ok
    except Exception:  # noqa: BLE001 — 状态未知时 fail-closed，阻断后续 stop（BUG-154）
        return (
            "⚠️ 自启动协调异常，停用状态未知；请先 `lwa autostart disable` 再停服",
            False,
        )


def coordinated_autostart_restart(
    ws, service_name: str
) -> tuple[str | None, bool, bool]:
    """IMP-030/BUG-191：``lwa update`` 重启前若自启动单元已加载/启用，则交监督器重启
    （单一进程），避免 stop 杀后 KeepAlive/Restart 立即拉回与 detached spawn 抢锁。

    返回 ``(note, ok, managed)``：``managed=True`` 表示自启动已接管重启，调用方
    **不应** 再 stop+start（否则额外 detached 出第二个进程）。``managed=False`` 时
    按原 stop+start 流程。
    """
    try:
        from local_webpage_access import autostart as asm

        res = asm.coordinated_restart(ws, service_name)
        return res.note, res.ok, res.managed
    except Exception:  # noqa: BLE001 — 协调异常时回退 stop+start（managed=False）
        return None, True, False
