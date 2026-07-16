"""``lwa autostart`` 子命令组（IMP-030）：跨平台开机自启动配置与完备性检查。

CLI 通过 ``add_typer`` 挂载为 ``lwa autostart ...``。逐步替代 ``lwa setup
--autostart`` 的"只写 plist"语义：在三平台**直接监管前台进程**（daemon /
manager / gateway），并提供 install/enable/disable/status/check/repair/uninstall
完整生命周期与完备性深检。

核心逻辑见 :mod:`local_webpage_access.autostart`。
"""

from __future__ import annotations

import typer

from local_webpage_access.autostart import AutostartError, EXIT_UNSUPPORTED
from local_webpage_access.cli._common import log, open_workspace_registry
from local_webpage_access.errors import LwaError

app = typer.Typer(help="配置开机自启动（macOS launchd / Linux·WSL systemd user）并做完备性检查")


def _unsupported_exit(exc: AutostartError) -> None:
    log.error(str(exc))
    typer.secho(str(exc), fg=typer.colors.RED, err=True)
    raise typer.Exit(code=EXIT_UNSUPPORTED)


@app.command("install")
def autostart_install(
    with_caddy: bool = typer.Option(
        False, "--with-caddy", help="额外监管 caddy 网关（仅 staticGateway=caddy）"
    ),
    no_enable: bool = typer.Option(
        False, "--no-enable", help="只生成单元文件，不立即启用（默认安装即启用）"
    ),
    linger: bool = typer.Option(
        False, "--linger", help="Linux/WSL 下尝试 loginctl enable-linger（登出保活）"
    ),
) -> None:
    """生成并启用自启动单元（前台监管 daemon/manager/可选 gateway）。"""
    from local_webpage_access import autostart as asm

    try:
        ws, config, _reg = open_workspace_registry()
        _reg.close()
        result = asm.install(
            ws, config, with_caddy=with_caddy, enable=not no_enable, linger=linger
        )
    except AutostartError as exc:
        _unsupported_exit(exc)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    typer.secho(
        f"── 自启动安装（{result.platform}）监管服务：{', '.join(result.services)} ──",
        fg=typer.colors.GREEN,
    )
    for unit in result.written:
        tag = "（迁移旧单元）" if unit.legacy else ""
        typer.echo(f"  · {unit.name}: {unit.path}{tag}")
    enable_failed = False
    if result.enabled:
        if not result.enable_ok:
            enable_failed = True
            typer.secho("启用失败（单元可能未真正加载）：", fg=typer.colors.YELLOW)
            for o in result.enable_outcomes:
                if not o.ok:
                    typer.echo(f"    {' '.join(o.cmd)} → {o.returncode} {o.stderr.strip()}")
        else:
            typer.secho("已启用（单元已加载，登录/开机后自动拉起前台进程）", fg=typer.colors.GREEN)
    else:
        typer.echo("提示：未启用，可稍后 `lwa autostart enable`")
    if result.linger_attempted and not result.linger_ok:
        typer.secho(
            "enable-linger 失败，请手动：sudo loginctl enable-linger $USER",
            fg=typer.colors.YELLOW,
        )
    if result.wsl_windows_script:
        typer.echo("\n── WSL Windows 保活脚本（在 Windows 侧任务计划程序登录时运行）──")
        typer.echo(result.wsl_windows_script)
    for note in result.notes:
        typer.secho(f"提示：{note}", fg=typer.colors.YELLOW)
    if enable_failed:
        raise typer.Exit(code=1)


@app.command("enable")
def autostart_enable() -> None:
    """启用已安装的自启动单元。"""
    from local_webpage_access import autostart as asm

    try:
        ws, config, _reg = open_workspace_registry()
        _reg.close()
        op = asm.enable(ws, config)
    except AutostartError as exc:
        _unsupported_exit(exc)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    _print_op(op, ok_msg="已启用自启动单元")


@app.command("disable")
def autostart_disable() -> None:
    """停用自启动单元（持久 disable；不删除单元文件）。"""
    from local_webpage_access import autostart as asm

    try:
        ws, config, _reg = open_workspace_registry()
        _reg.close()
        op = asm.disable(ws, config)
    except AutostartError as exc:
        _unsupported_exit(exc)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    _print_op(op, ok_msg="已停用自启动单元（launchctl disable 持久化；单元文件保留）")


@app.command("uninstall")
def autostart_uninstall(
    purge_linger: bool = typer.Option(
        False, "--purge-linger", help="同时 disable-linger（默认不动 linger）"
    ),
) -> None:
    """卸载自启动单元（停服务 + 删单元文件；不删工作区数据）。"""
    from local_webpage_access import autostart as asm

    try:
        ws, config, _reg = open_workspace_registry()
        _reg.close()
        op = asm.uninstall(ws, config, purge_linger=purge_linger)
    except AutostartError as exc:
        _unsupported_exit(exc)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    _print_op(op, ok_msg="已卸载自启动单元")
    if purge_linger and not op.success:
        typer.secho("提示：disable-linger 失败，请手动 sudo loginctl disable-linger $USER",
                    fg=typer.colors.YELLOW)


@app.command("status")
def autostart_status() -> None:
    """查看自启动单元与对应前台进程状态。"""
    from local_webpage_access import autostart as asm

    try:
        ws, config, _reg = open_workspace_registry()
        _reg.close()
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    plat = asm.detect_platform()
    typer.echo(f"平台：{plat}")
    try:
        backend = asm.select_backend()
    except AutostartError as exc:
        typer.secho(str(exc), fg=typer.colors.YELLOW)
        raise typer.Exit(code=EXIT_UNSUPPORTED)

    services = asm.installed_services(ws, backend)
    if not services:
        typer.echo("尚未安装任何自启动单元（lwa autostart install）")
        return
    for name in services:
        path = backend.unit_path(name)
        exists = path.is_file()
        legacy = backend.is_legacy(name) if exists else False
        loaded = asm.is_service_loaded(name)
        typer.echo(f"\n[{name}]")
        typer.echo(f"  单元文件：{path}（{'存在' if exists else '未安装'}）")
        if legacy:
            typer.secho("  形态：旧 detached 启动器（建议 lwa autostart repair）", fg=typer.colors.YELLOW)
        typer.echo(f"  已加载/激活：{'是' if loaded else '否'}")
        _echo_process_status(ws, config, name)


@app.command("check")
def autostart_check(
    json_output: bool = typer.Option(False, "--json", help="输出 JSON 报告"),
) -> None:
    """完备性深检（解释器/工作区/单元形态/启用态/进程/Caddy/linger/WSL/Docker）。"""
    import json as json_mod

    from local_webpage_access import autostart as asm

    try:
        ws, config, _reg = open_workspace_registry()
        _reg.close()
        report = asm.run_check(ws, config)
    except AutostartError as exc:
        _unsupported_exit(exc)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if json_output:
        typer.echo(json_mod.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        typer.echo(f"── 自启动完备性检查（{report.platform}）overall={report.overall} ──")
        for item in report.items:
            color = {"ok": typer.colors.GREEN, "warn": typer.colors.YELLOW, "fail": typer.colors.RED}.get(
                item.status, None
            )
            typer.secho(f"  [{item.status.upper():4}] {item.category}/{item.name}: {item.message}", fg=color)
            if item.fix:
                typer.echo(f"           修复：{item.fix}")
    if report.overall == "fail":
        raise typer.Exit(code=1)


@app.command("repair")
def autostart_repair(
    with_caddy: bool = typer.Option(False, "--with-caddy", help="一并修复/启用 gateway 单元"),
) -> None:
    """修复：重写失效路径、迁移旧启动器单元、重新启用。"""
    from local_webpage_access import autostart as asm

    try:
        ws, config, _reg = open_workspace_registry()
        _reg.close()
        result, actions = asm.repair(ws, config, with_caddy=with_caddy)
    except AutostartError as exc:
        _unsupported_exit(exc)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.secho("── 自启动修复 ──", fg=typer.colors.GREEN)
    for action in actions:
        typer.echo(f"  · {action}")
    for unit in result.written:
        typer.echo(f"  · {unit.name}: {unit.path}")
    if not result.enable_ok:
        typer.secho("启用失败（单元可能未真正加载）：", fg=typer.colors.YELLOW)
        for o in result.enable_outcomes:
            if not o.ok:
                typer.echo(f"    {' '.join(o.cmd)} → {o.returncode} {o.stderr.strip()}")
        raise typer.Exit(code=1)
    typer.secho("修复完成并已重新启用", fg=typer.colors.GREEN)


@app.command("doctor-hints")
def autostart_doctor_hints() -> None:
    """输出人工待办（Docker Desktop 登录启动 / WSL 网络等），不自动改系统设置。"""
    from local_webpage_access import autostart as asm

    try:
        ws, config, _reg = open_workspace_registry()
        _reg.close()
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.echo(asm.doctor_hints(ws, config))


# ---- 辅助 ------------------------------------------------------------------


def _print_op(op, *, ok_msg: str) -> None:
    """打印 OpResult；以 op.success（执行后状态化）为权威成败，失败时退出码 1。

    不再用单条命令 rc 反推成败——launchd 首次 enable 时对"可能没有旧实例"的
    bootout 会返回非零，这是预期行为，不应判为整体失败（BUG-142/147）。失败时
    仍打印非零命令作排障线索。
    """
    if not op.success:
        failed = [o for o in op.outcomes if not o.ok]
        for o in failed:
            typer.echo(f"  {' '.join(o.cmd)} → {o.returncode} {o.stderr.strip()}")
        typer.secho(f"{ok_msg}（失败，详见上方）", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.secho(ok_msg, fg=typer.colors.GREEN)


def _echo_process_status(ws, config, name: str) -> None:
    try:
        if name == "daemon":
            from local_webpage_access import daemon as daemon_mod

            running = bool(daemon_mod.is_running(ws))
        elif name == "manager":
            from local_webpage_access.manager_service import manager_status

            st = manager_status(ws, config)
            running = bool(st.get("running")) if isinstance(st, dict) else False
        else:
            from local_webpage_access.gateway_service import is_gateway_running

            running = bool(is_gateway_running(ws, config))
        typer.echo(f"  前台进程：{'运行中' if running else '未运行'}")
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"  前台进程：探测失败（{exc}）")
