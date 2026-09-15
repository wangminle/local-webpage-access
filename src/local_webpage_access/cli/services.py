"""自有服务协调操作（issue #33 / CHK-326）。

``lwa services restart`` 复用 updater 的 manager/daemon/gateway 重启编排，
保留自启动意图，不拉取源码、不重装 pip。
"""

from __future__ import annotations

import typer

from local_webpage_access.cli._common import log, open_workspace_registry
from local_webpage_access.errors import LwaError

app = typer.Typer(help="协调重启 manager / daemon / gateway（保留自启动意图）")


@app.command("restart")
def services_restart(
    no_reconcile: bool = typer.Option(
        False,
        "--no-reconcile",
        help="仅排障用：不做 enabled 但未运行的自有服务自动拉起",
    ),
) -> None:
    """协调重启自有服务，使运行中进程加载当前磁盘代码。"""
    from local_webpage_access.updater import restart_daemon, restart_gateway, restart_manager

    ws, config, reg = open_workspace_registry()
    failed = False
    try:
        steps = (
            ("manager", restart_manager),
            ("daemon", restart_daemon),
            ("gateway", restart_gateway),
        )
        for name, fn in steps:
            try:
                info = fn(ws, config, reconcile=not no_reconcile)
                message = ""
                if isinstance(info, dict):
                    message = str(info.get("message") or "")
                    if info.get("circuitBlocked"):
                        failed = True
                        typer.secho(
                            f"{name} 未重启（熔断）：{message or '启动熔断中'}",
                            fg=typer.colors.RED,
                            err=True,
                        )
                        continue
                    skipped = info.get("wasRunning") is False and not info.get("reconciled")
                    if skipped:
                        typer.secho(f"{name} 跳过：{message or '未运行'}", fg=typer.colors.YELLOW)
                        continue
                suffix = f"：{message}" if message else ""
                typer.secho(f"{name} 已协调重启{suffix}", fg=typer.colors.GREEN)
            except LwaError as exc:
                failed = True
                log.error(str(exc), extra=exc.context)
                typer.secho(f"{name} 重启失败：{exc}", fg=typer.colors.RED, err=True)
            except Exception as exc:  # noqa: BLE001
                failed = True
                log.exception("%s 协调重启失败", name)
                typer.secho(f"{name} 重启失败：{exc}", fg=typer.colors.RED, err=True)
    finally:
        reg.close()
    if failed:
        raise typer.Exit(code=1)
