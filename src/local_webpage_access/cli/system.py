"""系统/环境命令：``lwa setup`` / ``lwa doctor`` / ``lwa update``。

DEV-044（WBS-20260708 阶段5.1）：从原 ``cli.py`` 按功能域拆出。
"""

from __future__ import annotations

from typing import Any

import typer

from local_webpage_access.cli._common import log, open_workspace_registry
from local_webpage_access.errors import LwaError


def setup_cmd(
    script: bool = typer.Option(
        False, "--script", help="输出当前平台内置安装脚本路径（不自动执行）"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出 JSON 报告"),
    static_gateway: str = typer.Option(
        "caddy",
        "--static-gateway",
        help="预期静态网关（caddy 优先；未安装 Caddy 时降级 builtin）",
    ),
    default_profile: bool = typer.Option(
        False,
        "--default",
        help="装配档位：仅检测+指引（缺省行为；可询问安装 Docker）",
    ),
    full_profile: bool = typer.Option(
        False,
        "--full",
        help="装配档位：检查并安装 Caddy + Docker Engine + Compose 至最低版本",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="full 档跳过确认直接安装（非 TTY 时必须）"
    ),
    resume: bool = typer.Option(
        False,
        "--resume",
        help="full 档：从上次未完成的 Full Profile 安装继续（IMP-033）",
    ),
    install_docker: bool = typer.Option(
        False,
        "--install-docker",
        help="default 档：强制执行内置 Docker 安装脚本",
    ),
    no_install_docker: bool = typer.Option(
        False,
        "--no-install-docker",
        help="default 档：跳过 Docker 安装询问",
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
    ``--default`` / ``--full``（IMP-032）：环境装配档位；缺省即 default。
    工作区就绪后用 ``lwa doctor`` 做完整诊断（含端口池与 registry）。

    ``--autostart``（OPS-025）：基于当前工作区生成 launchd plist，开机自启
    daemon + manager（``--with-caddy`` 额外含 caddy）。
    """
    import json as json_mod

    from local_webpage_access.host_bootstrap import (
        format_script_catalog,
        maybe_offer_docker_install,
        resolve_profile,
        run_full_bootstrap,
    )
    from local_webpage_access.setup import (
        format_setup_report,
        render_setup_script,
        run_setup,
    )

    try:
        profile = resolve_profile(default=default_profile, full=full_profile)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    if install_docker and no_install_docker:
        typer.secho(
            "--install-docker 与 --no-install-docker 互斥",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    if script:
        catalog = format_script_catalog(full=profile == "full")
        typer.echo(catalog)
        typer.echo("# —— 以下为历史参考脚本（注释为主）——")
        typer.echo(render_setup_script())
        return

    if autostart:
        # IMP-030：`setup --autostart` 委托给统一的 `autostart install`（前台监管）。
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
        typer.secho(
            "提示：`lwa setup --autostart` 已委托给 `lwa autostart install`（IMP-030，"
            "推荐直接使用 `lwa autostart install/check/...`）。",
            fg=typer.colors.CYAN,
        )
        from local_webpage_access import autostart as asm
        from local_webpage_access.autostart import (
            EXIT_UNSUPPORTED,
            AutostartError,
        )

        try:
            result = asm.install(ws, config, with_caddy=with_caddy, enable=True)
        except AutostartError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=EXIT_UNSUPPORTED)
        for unit in result.written:
            typer.echo(f"  · {unit.name}: {unit.path}")
        if not result.enable_ok:
            typer.secho(
                "⚠️ 自启动单元已生成但启用失败（单元可能未真正加载）；"
                "请运行 `lwa autostart check` 复核并按提示修复",
                fg=typer.colors.YELLOW,
                err=True,
            )
            raise typer.Exit(code=1)
        typer.secho(
            "已生成并启用自启动单元。运行 `lwa autostart check` 复核完备性。",
            fg=typer.colors.GREEN,
        )
        return

    boot = None
    if profile == "full":
        ws_root = None
        try:
            from local_webpage_access.paths import find_workspace_root

            ws_root = find_workspace_root()
        except Exception:  # noqa: BLE001
            ws_root = None
        boot = run_full_bootstrap(yes=yes, resume=resume, workspace_root=ws_root)
        if not json_output:
            typer.secho("── 完整装配（--full）──", fg=typer.colors.CYAN)
            for msg in boot.messages:
                typer.echo(msg)
        else:
            # BUG-196：人类可读日志走 stderr，stdout 仅 JSON
            for msg in boot.messages:
                typer.echo(msg, err=True)
        if not boot.ok:
            if json_output:
                typer.echo(
                    json_mod.dumps(
                        {
                            "ok": False,
                            "profile": profile,
                            "overall": boot.overall,
                            "sessionRefreshRequired": boot.session_refresh_required,
                            "exitCode": boot.exit_code,
                            "messages": boot.messages,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            raise typer.Exit(code=boot.exit_code or 1)

    report = run_setup(static_gateway=static_gateway)
    if not json_output:
        typer.echo(format_setup_report(report))

    docker_offer = None
    if profile == "default":
        flag: bool | None
        if install_docker:
            flag = True
        elif no_install_docker:
            flag = False
        else:
            flag = None
        docker_offer = maybe_offer_docker_install(install_docker=flag)
        if not json_output:
            for msg in docker_offer.messages:
                typer.echo(msg)
        # BUG-197：安装成功后复检，用新报告决定退出码
        if docker_offer.attempted and docker_offer.script_ok:
            report = run_setup(static_gateway=static_gateway)
            if not json_output:
                typer.echo("── 安装后复检 ──")
                typer.echo(format_setup_report(report))

    if json_output:
        payload: dict = {
            "platform": report.platform,
            "profile": profile,
            "ready": report.ready,
            "items": [i.to_dict() for i in report.items],
        }
        if boot is not None:
            payload["bootstrap"] = {
                "ok": boot.ok,
                "messages": boot.messages,
            }
        if docker_offer is not None:
            payload["docker_offer"] = {
                "attempted": docker_offer.attempted,
                "script_ok": docker_offer.script_ok,
                "recheck_ok": docker_offer.recheck_ok,
                "messages": docker_offer.messages,
            }
        typer.echo(json_mod.dumps(payload, ensure_ascii=False, indent=2))

    # BUG-197：安装脚本失败或复检失败 → 非零退出
    if docker_offer is not None and docker_offer.attempted:
        if docker_offer.script_ok is False or docker_offer.recheck_ok is False:
            raise typer.Exit(code=1)

    if not report.ready:
        raise typer.Exit(code=1)


def doctor_cmd(
    instance_id: str = typer.Argument(
        None, help="可选：对单个实例执行健康诊断"
    ),
    json_output: bool = typer.Option(
        False, "--json", help="输出 JSON 报告（便于脚本解析）"
    ),
    profile: str = typer.Option(
        None,
        "--profile",
        help="档位：default|full（IMP-033；full 附加能力契约检查）",
    ),
) -> None:
    """诊断环境与实例问题（WBS-26）。

    检查 Python/Docker/Compose/端口/registry/磁盘/内存；提供 instance_id 时
    附加该实例的 manifest、状态、最近事件与日志诊断，并给出修复建议。
    ``--profile full`` 时额外输出 CapabilityReport 并按 Full 契约判定整体。
    """
    import json as json_mod

    from local_webpage_access.doctor import format_report, run_doctor

    try:
        ws, config, _reg = open_workspace_registry()
        _reg.close()  # doctor 自行打开 registry
        report = run_doctor(ws, config, instance_id=instance_id)
        cap_payload: dict[str, Any] | None = None
        if profile in ("full", "default") or getattr(config, "profile", None) == "full":
            from typing import Literal, cast

            from local_webpage_access.capability import collect_capability_report

            raw_profile = profile or getattr(config, "profile", None) or "default"
            use_profile = cast(
                Literal["default", "full"],
                raw_profile if raw_profile in ("full", "default") else "default",
            )
            cap = collect_capability_report(
                workspace_root=ws.root,
                profile=use_profile,
                role="cli",
                config_profile=getattr(config, "profile", None),
            )
            cap_payload = cap.to_dict()
            if use_profile == "full" and not json_output:
                typer.secho(
                    f"\n[Full Profile] overall={cap.overall} "
                    f"serviceUser={cap.service_user} "
                    f"sessionRefreshRequired={cap.session_refresh_required}",
                    fg=(
                        typer.colors.GREEN
                        if cap.overall == "ready"
                        else typer.colors.YELLOW
                        if cap.overall == "degraded"
                        else typer.colors.RED
                    ),
                )
                for label, val in (
                    ("CLI Docker", cap.cli_docker_access),
                    ("Manager Docker", cap.manager_docker_access),
                    ("Daemon Docker", cap.daemon_docker_access),
                    ("Caddy binary", cap.caddy_binary),
                    ("Caddy runtime", cap.caddy_runtime),
                    ("Caddy owner", cap.caddy_owner),
                ):
                    typer.echo(f"  {label:18s} {val}")
                if cap.action:
                    typer.echo(f"  建议：{cap.action}")
        if json_output:
            payload: dict[str, Any] = {
                "overall": report.overall,
                "instance_id": report.instance_id,
                "checks": [c.to_dict() for c in report.checks],
                "instance_checks": [
                    c.to_dict() for c in report.instance_checks
                ],
            }
            if cap_payload is not None:
                payload["capabilities"] = cap_payload
            typer.echo(
                json_mod.dumps(payload, ensure_ascii=False, indent=2)
            )
        else:
            typer.echo(format_report(report))
        fail = report.has_failures
        if cap_payload and (profile == "full" or getattr(config, "profile", None) == "full"):
            if cap_payload.get("overall") in ("unready", "degraded"):
                fail = True
        if fail:
            raise typer.Exit(code=1)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


def capabilities_cmd(
    json_output: bool = typer.Option(
        False, "--json", help="输出 CapabilityReport JSON"
    ),
) -> None:
    """输出当前工作区能力报告（IMP-033 ``lwa capabilities``）。"""
    import json as json_mod

    from local_webpage_access.capability import collect_capability_report

    try:
        ws, config, reg = open_workspace_registry()
        reg.close()
        report = collect_capability_report(
            workspace_root=ws.root,
            role="cli",
            config_profile=getattr(config, "profile", None),
        )
        if json_output:
            typer.echo(
                json_mod.dumps(report.to_dict(), ensure_ascii=False, indent=2)
            )
        else:
            typer.echo(
                f"profile={report.profile} overall={report.overall} "
                f"serviceUser={report.service_user}"
            )
            caps = report.to_dict()["capabilities"]
            for k, v in caps.items():
                typer.echo(f"  {k}: {v}")
            if report.action:
                typer.echo(f"action: {report.action}")
        if report.overall == "unready":
            raise typer.Exit(code=1)
        if report.overall == "degraded":
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
    app.command("capabilities")(capabilities_cmd)
    app.command("update")(update_cmd)
