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
    from local_webpage_access.manager_api import (
        TOKEN_ROTATE_HOURS_DEFAULT,
        maybe_rotate_token,
        read_token,
    )
    from local_webpage_access.manager_service import start_manager
    from local_webpage_access.ports import resolve_lan_ip

    try:
        ws, config, _reg = open_workspace_registry()
        _reg.close()
        # BUG-446：打印前同步到期轮换，再 read_token，避免打印已失效 token。
        rotate_hours = (
            getattr(config, "managerTokenRotateHours", TOKEN_ROTATE_HOURS_DEFAULT)
            or TOKEN_ROTATE_HOURS_DEFAULT
        )
        maybe_rotate_token(ws, hours=rotate_hours)
        pid = start_manager(ws, config)
        token = read_token(ws) or ""
        lan_ip = resolve_lan_ip(config) or "127.0.0.1"
        from local_webpage_access.ports import format_http_host

        typer.secho(f"管理页已启动（pid={pid}）", fg=typer.colors.GREEN)
        typer.echo(f"  本机：http://127.0.0.1:{config.managerPort}/")
        typer.echo(f"  局域网：http://{format_http_host(lan_ip)}:{config.managerPort}/")
        typer.echo(f"  token：{token}")
        typer.echo("  停止：lwa manager off")
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command("off")
def manager_off() -> None:
    """停止后台管理页。"""
    from local_webpage_access.manager_service import foreign_manager_hint, stop_manager

    try:
        ws, config, _reg = open_workspace_registry()
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
        # BUG-456：本工作区已停，但端口上仍可能是其他工作区的管理页。
        tip = foreign_manager_hint(ws, config)
        if tip:
            typer.secho(tip, fg=typer.colors.YELLOW, err=True)
        else:
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
        from local_webpage_access.ports import format_http_host

        typer.echo(f"  地址：http://{format_http_host(lan_ip)}:{st['port']}/")
        token = read_token(ws)
        if token:
            typer.echo(f"  token：{token}")
        if not st["running"] and st["configured"]:
            typer.echo("  启动：lwa manager on")
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command("token")
def manager_token(
    json_output: bool = typer.Option(
        False, "--json", help="以 JSON 格式输出（便于 Agent 解析）"
    ),
) -> None:
    """IMP-046：查看当前管理页 API token 及轮换信息。

    显示 token 明文、颁发时间（createdAt）、下次轮换时间。
    本机 loopback 访问免 token，但 token 仍用于局域网访问。
    """
    from local_webpage_access.manager_api import read_token_metadata, TOKEN_ROTATE_HOURS_DEFAULT

    try:
        ws, config, _reg = open_workspace_registry()
        _reg.close()
        meta = read_token_metadata(ws)
        token = meta["token"]
        created_at = meta["createdAt"]
        rotate_hours = getattr(
            config, "managerTokenRotateHours", TOKEN_ROTATE_HOURS_DEFAULT
        ) or TOKEN_ROTATE_HOURS_DEFAULT

        if not token:
            if json_output:
                typer.echo('{"token": null, "message": "token 未颁发；请运行 lwa manager on"}')
            else:
                typer.secho("token 未颁发；请运行 lwa manager on", fg=typer.colors.YELLOW)
            raise typer.Exit(code=1)

        # 计算下次轮换时间
        next_rotate_at = None
        if created_at:
            from datetime import datetime, timedelta
            try:
                created_dt = datetime.fromisoformat(created_at)
                if created_dt.tzinfo is None:
                    created_dt = created_dt.astimezone()
                next_rotate_at = (created_dt + timedelta(hours=rotate_hours)).isoformat(timespec="seconds")
            except (ValueError, TypeError):
                pass

        if json_output:
            import json as _json
            payload = {
                "token": token,
                "createdAt": created_at,
                "rotateHours": rotate_hours,
                "nextRotateAt": next_rotate_at,
            }
            typer.echo(_json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            typer.echo(f"token：{token}")
            if created_at:
                typer.echo(f"  颁发时间：{created_at}")
            else:
                typer.echo("  颁发时间：未知（旧文件缺少 createdAt）")
            typer.echo(f"  轮换周期：{rotate_hours} 小时")
            if next_rotate_at:
                typer.echo(f"  下次轮换：{next_rotate_at}")
            typer.echo("  本机访问免 token（loopback）；局域网访问须携带此 token。")
            typer.echo("  轮换后旧 token 立即失效，局域网客户端需更新。")
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
        None,
        "--port",
        min=1,
        max=65535,
        help="监听端口（默认用配置 managerPort，通常是 17800）",
    ),
) -> None:
    """启动管理页 HTTP 服务（前台运行，Ctrl+C 退出）。"""
    from local_webpage_access.manager_api import (
        TOKEN_ROTATE_HOURS_DEFAULT,
        maybe_rotate_token,
        read_token,
        run_manager,
    )
    from local_webpage_access.ports import format_http_host
    from local_webpage_access.security import assert_no_critical, validate_manager_binding

    try:
        ws, config, _reg = open_workspace_registry()
        _reg.close()  # manager 会自行打开 registry
        # BUG-446：打印前同步到期轮换，避免冷启动打印已失效 token。
        rotate_hours = (
            getattr(config, "managerTokenRotateHours", TOKEN_ROTATE_HOURS_DEFAULT)
            or TOKEN_ROTATE_HOURS_DEFAULT
        )
        maybe_rotate_token(ws, hours=rotate_hours)
        token = read_token(ws) or ""
        # 直接调用（非经 typer 解析）时 host/port 为 OptionInfo，按未提供处理。
        bind_host = host if isinstance(host, str) else config.managerHost
        bind_port = port if isinstance(port, int) else config.managerPort
        assert_no_critical(
            validate_manager_binding(bind_host, has_token=bool(token), port=bind_port)
        )
        typer.secho(
            f"管理页启动中：http://{format_http_host(bind_host)}:{bind_port}",
            fg=typer.colors.GREEN,
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
