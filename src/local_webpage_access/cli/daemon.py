"""daemon 子命令（WBS-21）：``lwa daemon on/off/status``。

DEV-044（WBS-20260708 阶段5.1）：从原 ``cli.py`` 拆出。暴露 ``app`` 供根
CLI 通过 ``add_typer`` 挂载为 ``lwa daemon ...`` 子命令组。
"""

from __future__ import annotations

import typer

from local_webpage_access.cli._common import coordinated_autostart_disable, log, open_workspace_registry
from local_webpage_access.errors import LwaError

app = typer.Typer(help="控制 daemon 自动导入模式")


@app.command("on")
def daemon_on(
    poll: float = typer.Option(
        None,
        "--poll",
        "-p",
        help="inbox 扫描间隔（秒），默认 5",
    ),
) -> None:
    """开启 daemon：自动监听 inbox/，导入并启动可确定的轻量实例。"""
    from local_webpage_access import daemon as daemon_mod

    try:
        ws, config, _reg = open_workspace_registry()
        # daemon 子进程会自行打开 registry；这里只读状态
        _reg.close()
        pid = daemon_mod.start_daemon(ws, config, poll_interval=poll)
        typer.secho(f"daemon 已启动（pid={pid}）", fg=typer.colors.GREEN)
        typer.echo(f"  监听目录：{ws.inbox}")
        typer.echo("  把 zip 放入 inbox/ 即可自动导入；uncertain/heavy 实例标记 pending。")
        typer.echo("  停止：lwa daemon off")
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command("off")
def daemon_off() -> None:
    """关闭 daemon：停止自动监听 inbox/。"""
    from local_webpage_access import daemon as daemon_mod
    from local_webpage_access.paths import require_workspace

    try:
        ws = require_workspace()
        # IMP-030/030.b：若自启动单元已加载/启用，先停用，避免 KeepAlive/Restart 立刻拉回。
        note, ok = coordinated_autostart_disable(ws, "daemon")
        if note:
            typer.secho(note, fg=typer.colors.GREEN if ok else typer.colors.YELLOW)
        if not ok:
            # 单元未能停用 → 停进程会被立即拉回，off 无法生效：阻断并提示先 disable（BUG-147）。
            raise typer.Exit(code=1)
        stopped = daemon_mod.stop_daemon(ws)
        if stopped:
            typer.secho("daemon 已停止", fg=typer.colors.GREEN)
        else:
            typer.secho(
                "daemon 已请求停止，但子进程未在超时内退出（可能需要手动 kill）",
                fg=typer.colors.YELLOW,
            )
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command("status")
def daemon_status() -> None:
    """查看 daemon 运行状态。"""
    from local_webpage_access import daemon as daemon_mod
    from local_webpage_access.paths import require_workspace

    try:
        ws = require_workspace()
        info = daemon_mod.daemon_status(ws)
        if info["running"]:
            typer.secho(
                f"daemon 运行中（pid={info['pid']}, poll={info['pollInterval']}s）",
                fg=typer.colors.GREEN,
            )
        elif info["enabled"]:
            typer.secho(
                f"daemon 已标记开启但未检测到进程（pid={info['pid']}）",
                fg=typer.colors.YELLOW,
            )
        else:
            typer.echo("daemon 未开启（lwa daemon on 启动）")
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
