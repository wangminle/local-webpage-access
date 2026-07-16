"""manager 子命令（WBS-22.13）：``lwa manager on/off/status/start``。

DEV-044（WBS-20260708 阶段5.1）：从原 ``cli.py`` 拆出。暴露 ``app`` 供根
CLI 通过 ``add_typer`` 挂载为 ``lwa manager ...`` 子命令组。
"""

from __future__ import annotations

import typer

from local_webpage_access.cli._common import coordinated_autostart_disable, log, open_workspace_registry
from local_webpage_access.errors import LwaError

app = typer.Typer(help="控制管理页 HTTP 服务")


@app.command("on")
def manager_on() -> None:
    """后台启动管理页（默认 init 后自动执行；managerEnabled=false 时禁用）。"""
    from local_webpage_access.manager_api import ensure_token
    from local_webpage_access.manager_service import start_manager
    from local_webpage_access.ports import resolve_lan_ip

    try:
        ws, config, _reg = open_workspace_registry()
        _reg.close()
        pid = start_manager(ws, config)
        token = ensure_token(ws)
        lan_ip = resolve_lan_ip(config) or "127.0.0.1"
        typer.secho(f"管理页已启动（pid={pid}）", fg=typer.colors.GREEN)
        typer.echo(f"  本机：http://127.0.0.1:{config.managerPort}/")
        typer.echo(f"  局域网：http://{lan_ip}:{config.managerPort}/")
        typer.echo(f"  token：{token}")
        typer.echo("  停止：lwa manager off")
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command("off")
def manager_off() -> None:
    """停止后台管理页。"""
    from local_webpage_access.manager_service import stop_manager

    try:
        ws, _config, _reg = open_workspace_registry()
        _reg.close()
        # IMP-030/030.b：若 manager 自启动单元已加载/启用，先停用，避免 KeepAlive 立刻拉回。
        note, ok = coordinated_autostart_disable(ws, "manager")
        if note:
            typer.secho(note, fg=typer.colors.GREEN if ok else typer.colors.YELLOW)
        if not ok:
            # 单元未能停用 → 停进程会被立即拉回，off 无法生效：阻断并提示先 disable（BUG-147）。
            raise typer.Exit(code=1)
        if not stop_manager(ws):
            typer.secho(
                "管理页停止失败，进程可能仍在运行；请检查 pid 或端口占用后重试",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        typer.secho("管理页已停止", fg=typer.colors.GREEN)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command("status")
def manager_status_cmd() -> None:
    """查看管理页运行状态。"""
    from local_webpage_access.manager_api import read_token
    from local_webpage_access.manager_service import manager_status
    from local_webpage_access.ports import resolve_lan_ip

    try:
        ws, config, _reg = open_workspace_registry()
        _reg.close()
        st = manager_status(ws, config)
        lan_ip = resolve_lan_ip(config) or "127.0.0.1"
        running = "运行中" if st["running"] else "未运行"
        typer.echo(f"管理页：{running}")
        typer.echo(f"  配置启用：{st['configured']}（managerEnabled）")
        typer.echo(f"  状态启用：{st['enabled']}")
        if st.get("pid"):
            typer.echo(f"  pid：{st['pid']}")
        typer.echo(f"  地址：http://{lan_ip}:{st['port']}/")
        token = read_token(ws)
        if token:
            typer.echo(f"  token：{token}")
        if not st["running"] and st["configured"]:
            typer.echo("  启动：lwa manager on")
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command("start")
def manager_start(
    host: str = typer.Option(
        None,
        "--host",
        help="监听地址（默认用配置 managerHost，通常是 0.0.0.0 即局域网可达）",
    ),
    port: int = typer.Option(
        None, "--port", help="监听端口（默认用配置 managerPort，通常是 17800）"
    ),
) -> None:
    """启动管理页 HTTP 服务（前台运行，Ctrl+C 退出）。"""
    from local_webpage_access.manager_api import ensure_token, run_manager
    from local_webpage_access.security import assert_no_critical, validate_manager_binding

    try:
        ws, config, _reg = open_workspace_registry()
        _reg.close()  # manager 会自行打开 registry
        token = ensure_token(ws)
        bind_host = host or config.managerHost
        bind_port = port if port is not None else config.managerPort
        assert_no_critical(
            validate_manager_binding(bind_host, has_token=bool(token), port=bind_port)
        )
        typer.secho(
            f"管理页启动中：http://{bind_host}:{bind_port}", fg=typer.colors.GREEN
        )
        typer.echo(f"  API token：{token}")
        typer.echo("  未带 token 的 /api/* 请求将被拒绝（401）。")
        typer.echo("  把 zip 放进 inbox/ 或在此页面管理已导入实例。Ctrl+C 退出。")
        run_manager(ws, config, host=bind_host, port=bind_port)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command("logs")
def manager_logs(
    tail: int = typer.Option(200, "--tail", "-n", help="显示最近 N 行（0=全文）"),
) -> None:
    """查看管理页运行时日志（``logs/manager.log``）。"""
    from local_webpage_access.manager_service import log_file_path, read_manager_log

    try:
        ws, _config, _reg = open_workspace_registry()
        _reg.close()
        text = read_manager_log(ws, tail=tail)
        if not text:
            path = log_file_path(ws)
            typer.secho(
                f"管理页日志不存在或为空：{path}",
                fg=typer.colors.YELLOW,
            )
        else:
            typer.echo(text)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
