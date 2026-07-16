"""gateway 子命令（IMP-010 / DEV-041，WBS 0.7）：``lwa gateway on/off/status``。

DEV-044（WBS-20260708 阶段5.1）：从原 ``cli.py`` 拆出。暴露 ``app`` 供根
CLI 通过 ``add_typer`` 挂载为 ``lwa gateway ...`` 子命令组。
"""

from __future__ import annotations

import typer

from local_webpage_access.cli._common import coordinated_autostart_disable, log, open_workspace_registry
from local_webpage_access.errors import LwaError

app = typer.Typer(help="控制 Caddy 网关（master 生命周期与 :8080 别名入口）")


def _require_caddy_version(config) -> None:
    """``lwa gateway on/off`` 前置：校验 ``staticGateway=caddy`` 且 Caddy 满足最低版本。

    与 ``doctor.check_caddy`` 同源判定，不满足时抛 :class:`LwaError` 由 CLI 统一格式化。
    """
    import shutil
    import subprocess

    from local_webpage_access.version_requirements import MIN_CADDY_VERSION, version_ge

    if config.staticGateway != "caddy":
        raise LwaError(
            f"staticGateway={config.staticGateway}，网关命令仅适用于 caddy 后端",
            code="GATEWAY_BACKEND_MISMATCH",
            suggestion="在 local-web.yml 设置 staticGateway: caddy",
        )
    if not shutil.which("caddy"):
        raise LwaError(
            "未找到 caddy 可执行文件",
            code="GATEWAY_CADDY_MISSING",
            suggestion=f"安装 Caddy ≥ {MIN_CADDY_VERSION} 并加入 PATH",
        )
    try:
        result = subprocess.run(
            ["caddy", "version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LwaError(
            f"无法获取 Caddy 版本：{exc}",
            code="GATEWAY_VERSION_UNKNOWN",
            suggestion=f"确认 caddy 可执行且版本 ≥ {MIN_CADDY_VERSION}",
        ) from exc
    if result.returncode != 0 or not version_ge(
        (result.stdout or "").strip() or (result.stderr or "").strip(),
        MIN_CADDY_VERSION,
    ):
        raise LwaError(
            "Caddy 版本不满足要求",
            code="GATEWAY_VERSION_TOO_LOW",
            suggestion=f"升级 Caddy 至 ≥ {MIN_CADDY_VERSION}",
        )


@app.command("on")
def gateway_on(
    rebuild_if_needed: bool = typer.Option(
        False,
        "--rebuild-if-needed",
        help="交接收尾后对检出 IMP-023 空 200 的实例自动 rebuild（默认仅检查并提示）",
    ),
) -> None:
    """启动 Caddy 网关（master + admin :2019，别名入口随配置就绪）。

    建议项 A/B/F/I（gateway-switch-access-review）：启动同时执行切换事务收尾——
    停掉残留 builtin 静态进程、用当前 LAN IP 刷新各实例访问地址、记审计事件；
    随后默认跑 access review（G6），可选 ``--rebuild-if-needed`` 自动重建。
    """
    from local_webpage_access.access import (
        format_review_report,
        maybe_rebuild_after_review,
        review_access,
    )
    from local_webpage_access.gateway_service import start_gateway
    from local_webpage_access.ports import resolve_lan_ip

    review_failed = False
    rebuild_failed = False
    try:
        ws, config, reg = open_workspace_registry()
        try:
            _require_caddy_version(config)
            pid = start_gateway(ws, config, registry=reg)
            lan_ip = resolve_lan_ip(config) or "127.0.0.1"
            typer.secho(f"网关已启动（pid={pid}）", fg=typer.colors.GREEN)
            typer.echo("  admin：http://127.0.0.1:2019/")
            port = config.staticGatewayPort
            if port:
                typer.echo(f"  别名入口：http://{lan_ip}:{port}/<alias>/")
            typer.echo("  停止：lwa gateway off；状态：lwa gateway status")
            typer.echo("  刷新地址：lwa access refresh")
            # G6：交接收尾后默认复核访问；可选自动 rebuild。
            try:
                report = review_access(ws, config, reg)
                rebuild_report = maybe_rebuild_after_review(
                    ws,
                    config,
                    reg,
                    report,
                    rebuild_if_needed=rebuild_if_needed,
                )
                typer.echo("")
                typer.echo(
                    format_review_report(report, rebuild_report=rebuild_report)
                )
                review_failed = report.has_failures
                rebuild_failed = not rebuild_report.all_ok
            except Exception as exc:  # noqa: BLE001 — review 失败不掩盖网关已启动
                log.warning(
                    "gateway on 后 access review 失败（不阻断启动）：%s", exc
                )
                typer.secho(
                    f"  访问复核失败（网关已启动）：{exc}",
                    fg=typer.colors.YELLOW,
                    err=True,
                )
                typer.echo("  请手动运行：lwa access review")
                review_failed = True
        finally:
            reg.close()
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    if review_failed or rebuild_failed:
        raise typer.Exit(code=1)


@app.command("off")
def gateway_off() -> None:
    """停止 Caddy 网关（master 优雅关闭）。

    不做 ``MIN_CADDY_VERSION`` 校验：``stop_gateway`` 自身按 ``detect_backend``
    分支处理（caddy 走 admin API 优雅关闭；builtin 仅清残留服务态），即使当前
    ``staticGateway=builtin``（如刚从 caddy 切走）也能关掉仍在跑的 master 并清
    ``run/gateway.json``，避免"切 builtin 后无法用 CLI 关 Caddy"的死局。
    """
    from local_webpage_access.gateway_service import stop_gateway

    try:
        ws, config, _reg = open_workspace_registry()
        _reg.close()
        # IMP-030/030.b：若 gateway 自启动单元已加载/启用，先停用，避免 KeepAlive 立刻拉回。
        note, ok = coordinated_autostart_disable(ws, "gateway")
        if note:
            typer.secho(note, fg=typer.colors.GREEN if ok else typer.colors.YELLOW)
        if not ok:
            # 单元未能停用 → 停进程会被立即拉回，off 无法生效：阻断并提示先 disable（BUG-147）。
            raise typer.Exit(code=1)
        if not stop_gateway(ws, config):
            typer.secho(
                "网关停止失败，Caddy master 可能仍在运行；请检查 admin :2019 后重试",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        typer.secho("网关已停止", fg=typer.colors.GREEN)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command("status")
def gateway_status_cmd() -> None:
    """查看 Caddy 网关运行状态。"""
    from local_webpage_access.gateway_service import gateway_status
    from local_webpage_access.ports import resolve_lan_ip

    try:
        ws, config, _reg = open_workspace_registry()
        _reg.close()
        st = gateway_status(ws, config)
        if st.get("orphanMaster"):
            typer.secho(
                "网关：运行中（残留 Caddy master；配置已非 caddy）",
                fg=typer.colors.YELLOW,
            )
            typer.echo("  处置：lwa gateway off（可在 builtin 配置下关停残留 master）")
        else:
            running = "运行中" if st["running"] else "未运行"
            typer.echo(f"网关：{running}")
        typer.echo(f"  后端：{st['backend']}（staticGateway={st['configured']}）")
        typer.echo(f"  状态启用：{st['enabled']}")
        if st.get("pid"):
            typer.echo(f"  pid：{st['pid']}")
        typer.echo(f"  admin：http://127.0.0.1:{st['adminPort']}/")
        port = st["port"]
        if port:
            lan_ip = resolve_lan_ip(config) or "127.0.0.1"
            typer.echo(f"  别名入口：http://{lan_ip}:{port}/<alias>/")
        if not st["running"] and st["backend"] == "caddy":
            typer.echo("  启动：lwa gateway on")
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
