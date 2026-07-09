"""系统/环境命令：``lwa setup`` / ``lwa doctor`` / ``lwa update``。

DEV-044（WBS-20260708 阶段5.1）：从原 ``cli.py`` 按功能域拆出。
"""

from __future__ import annotations

import typer

from local_webpage_access.cli._common import log, open_workspace_registry
from local_webpage_access.errors import LwaError


def setup_cmd(
    script: bool = typer.Option(
        False, "--script", help="输出当前平台的参考安装脚本（不自动执行）"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出 JSON 报告"),
    static_gateway: str = typer.Option(
        "caddy",
        "--static-gateway",
        help="预期静态网关（caddy 优先；未安装 Caddy 时降级 builtin）",
    ),
    autostart: bool = typer.Option(
        False,
        "--autostart",
        help="OPS-025：生成 macOS launchd plist，登录自启 daemon + manager（需已初始化工作区）",
    ),
    with_caddy: bool = typer.Option(
        False,
        "--with-caddy",
        help="与 --autostart 配合：额外生成 caddy 网关自启（仅 staticGateway=caddy）",
    ),
) -> None:
    """检测宿主机工具环境并给出安装指引（可在 ``lwa init`` 之前运行）。

    检查 Python、lwa 包、Docker、Compose、Caddy、Node；不依赖工作区。
    工作区就绪后用 ``lwa doctor`` 做完整诊断（含端口池与 registry）。

    ``--autostart``（OPS-025）：基于当前工作区生成 launchd plist，开机自启
    daemon + manager（``--with-caddy`` 额外含 caddy）。
    """
    import json as json_mod

    from local_webpage_access.setup import (
        format_autostart_report,
        format_setup_report,
        generate_launchd_plists,
        render_setup_script,
        run_setup,
    )

    if script:
        typer.echo(render_setup_script())
        return

    if autostart:
        # 自启需要已初始化的工作区（daemon/manager 依赖 local-web.yml）
        try:
            ws, config, _reg = open_workspace_registry()
        except LwaError as exc:
            log.error(str(exc), extra=exc.context)
            typer.secho(
                "开机自启需要已初始化的工作区，请先在目标目录执行 `lwa init`",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1) from exc
        skipped_caddy = with_caddy and config.staticGateway != "caddy"
        written = generate_launchd_plists(
            ws.root, config, include_caddy=with_caddy
        )
        typer.echo(format_autostart_report(written, skipped_caddy=skipped_caddy))
        return

    report = run_setup(static_gateway=static_gateway)
    if json_output:
        typer.echo(
            json_mod.dumps(
                {
                    "platform": report.platform,
                    "ready": report.ready,
                    "items": [i.to_dict() for i in report.items],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        typer.echo(format_setup_report(report))
    if not report.ready:
        raise typer.Exit(code=1)


def doctor_cmd(
    instance_id: str = typer.Argument(
        None, help="可选：对单个实例执行健康诊断"
    ),
    json_output: bool = typer.Option(
        False, "--json", help="输出 JSON 报告（便于脚本解析）"
    ),
) -> None:
    """诊断环境与实例问题（WBS-26）。

    检查 Python/Docker/Compose/端口/registry/磁盘/内存；提供 instance_id 时
    附加该实例的 manifest、状态、最近事件与日志诊断，并给出修复建议。
    """
    import json as json_mod

    from local_webpage_access.doctor import format_report, run_doctor

    try:
        ws, config, _reg = open_workspace_registry()
        _reg.close()  # doctor 自行打开 registry
        report = run_doctor(ws, config, instance_id=instance_id)
        if json_output:
            typer.echo(
                json_mod.dumps(
                    {
                        "overall": report.overall,
                        "instance_id": report.instance_id,
                        "checks": [c.to_dict() for c in report.checks],
                        "instance_checks": [
                            c.to_dict() for c in report.instance_checks
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            typer.echo(format_report(report))
        if report.has_failures:
            raise typer.Exit(code=1)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


def update_cmd(
    workspace: str = typer.Option(
        None,
        "--workspace",
        "-w",
        help="工作区根（默认自动识别 local-web.yml）",
    ),
    repo: str = typer.Option(
        None,
        "--repo",
        help="lwa 源码根（默认识别 editable 安装路径或 git 根）",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="仅展示计划，不执行 pip/同步/重启"
    ),
    skip_pip: bool = typer.Option(
        False, "--skip-pip", help="跳过 pip install -e .（已手动装过）"
    ),
    sync_templates: bool = typer.Option(
        False,
        "--sync-templates",
        help="同步 templates/（默认关，避免覆盖用户改过的模板）",
    ),
    no_doctor: bool = typer.Option(
        False, "--no-doctor", help="结束后不跑 lwa doctor"
    ),
    restart_instances: bool = typer.Option(
        False,
        "--restart-instances",
        help="对所有可重启实例执行 restart（默认关，耗时长）",
    ),
    no_sync_skills: bool = typer.Option(
        False, "--no-sync-skills", help="跳过同步 skills/（默认同步）"
    ),
    no_restart_manager: bool = typer.Option(
        False, "--no-restart-manager", help="跳过重启管理页"
    ),
    no_restart_daemon: bool = typer.Option(
        False, "--no-restart-daemon", help="跳过重启 daemon"
    ),
    json_output: bool = typer.Option(
        False, "--json", help="输出机器可读的 JSON 摘要"
    ),
) -> None:
    """刷新 lwa 安装、同步工作区附属物、重启自有服务（IMP-008）。

    ``git pull`` / 改代码后一条命令收敛运行态。默认**不动**已导入的实例
    （apps/）；变更涉及托管逻辑时加 ``--restart-instances``。仅当 manager /
    daemon 原本 running 时才重启，原本 stopped 不会被自动开启。
    """
    import json as json_mod
    from pathlib import Path

    from local_webpage_access.paths import require_workspace
    from local_webpage_access.updater import UpdateOptions, format_report, run_update

    try:
        ws = (
            require_workspace(Path(workspace))
            if workspace
            else require_workspace()
        )
        from local_webpage_access.config import load_config

        config = load_config(ws)
        from local_webpage_access.registry import Registry

        reg = Registry(ws.db_path)
        reg.open()
        try:
            options = UpdateOptions(
                dry_run=dry_run,
                skip_pip=skip_pip,
                sync_skills=not no_sync_skills,
                sync_templates=sync_templates,
                restart_manager=not no_restart_manager,
                restart_daemon=not no_restart_daemon,
                restart_instances=restart_instances,
                run_doctor=not no_doctor,
                repo=repo,
            )
            report = run_update(ws, config, reg, options=options)
        finally:
            reg.close()

        if json_output:
            typer.echo(
                json_mod.dumps(report.to_dict(), ensure_ascii=False, indent=2)
            )
        else:
            typer.echo(format_report(report))
        if report.has_failures:
            raise typer.Exit(code=1)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


def register(app: typer.Typer) -> None:
    """把本模块命令注册到根 app（保持顶层命令名不变）。"""
    app.command("setup")(setup_cmd)
    app.command("doctor")(doctor_cmd)
    app.command("update")(update_cmd)
