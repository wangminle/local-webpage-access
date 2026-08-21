"""生命周期命令：``lwa start/stop/restart/recover/rebuild/remove/logs``。

DEV-044（WBS-20260708 阶段5.1）：从原 ``cli.py`` 按功能域拆出。
"""

from __future__ import annotations

from typing import Any

import typer

from local_webpage_access.cli._common import log, open_workspace_registry
from local_webpage_access.errors import LwaError


def start(
    instance_id: str = typer.Argument(..., help="要启动的实例 ID"),
    auto_fallback: bool = typer.Option(
        False,
        "--auto-fallback",
        help="top-1 候选失败时自动降级到等价计划（跳过确认）",
    ),
    no_fallback: bool = typer.Option(
        False,
        "--no-fallback",
        help="top-1 候选失败时不降级（直接失败）",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="非交互确认（CI / 脚本调用，等价于 --auto-fallback）",
    ),
) -> None:
    """启动实例（静态 / 前端 / 容器统一入口）。

    C.R03：当 top-1 候选部署失败且存在等价 fallback 计划时：
    - 默认（交互式）：展示等价计划并等待用户确认后降级
    - ``--auto-fallback`` / ``--yes``：自动降级，不等待确认
    - ``--no-fallback``：不降级，直接失败
    """
    from local_webpage_access.lifecycle import (
        FallbackConfirmationRequired,
        start_instance,
    )

    # 确定降级策略
    if no_fallback:
        fallback_policy = "disabled"
    elif auto_fallback or yes:
        fallback_policy = "auto-equivalent"
    else:
        fallback_policy = "confirm"

    try:
        ws, config, reg = open_workspace_registry()
        try:
            try:
                manifest = start_instance(
                    ws,
                    config,
                    reg,
                    instance_id,
                    fallback_policy=fallback_policy,
                )
            except FallbackConfirmationRequired as fcr:
                # C.R03：交互式确认--展示等价计划并等待用户选择
                manifest = _handle_fallback_confirmation(
                    ws,
                    config,
                    reg,
                    instance_id,
                    fcr,
                )
        finally:
            reg.close()
        typer.secho(f"已启动实例：{instance_id}", fg=typer.colors.GREEN)
        typer.echo(f"  形态：{manifest.runtime} / {manifest.servingMode}")
        if manifest.network.hostPort:
            typer.echo(f"  端口：{manifest.network.hostPort}")
        if manifest.network.lanUrl:
            typer.echo(f"  局域网：{manifest.network.lanUrl}")
        # IMP-006：路径别名入口 URL（routeMode=name 时填充）
        if manifest.network.routeUrl:
            typer.secho(f"  路径：{manifest.network.routeUrl}", fg=typer.colors.CYAN)
        elif (
            manifest.static is not None
            and manifest.static.routeMode == "name"
            and manifest.static.routeHost
        ):
            # 别名已登记但入口端口未配置或 LAN IP 未探测到
            typer.secho(
                f"  路径别名 /{manifest.static.routeHost}/ 已登记，但入口未就绪"
                f"（检查 local-web.yml 的 staticGatewayPort 与 LAN IP）",
                fg=typer.colors.YELLOW,
            )
        typer.echo(f"  健康：{manifest.network.healthUrl}")
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


def _handle_fallback_confirmation(
    ws: Any,
    config: Any,
    reg: Any,
    instance_id: str,
    fcr: Any,
) -> Any:
    """C.R03：处理 FallbackConfirmationRequired--展示等价计划并等待用户确认。

    在交互式终端中列出等价 fallback 计划，用户确认后以 auto-equivalent 重试。
    非交互式环境（无 TTY）直接失败（fail-closed）。
    """
    import sys

    from local_webpage_access.lifecycle import start_instance

    typer.secho(
        f"\n实例 {instance_id} top-1 候选部署失败：{fcr.primary_failure[:200]}",
        fg=typer.colors.RED,
    )
    typer.echo(f"\n存在 {len(fcr.equivalent_candidates)} 个等价 fallback 候选：\n")

    for i, cand in enumerate(fcr.equivalent_candidates):
        idx = cand.get("index", i + 1)
        kind = cand.get("kind", "?")
        tier = cand.get("confidenceTier", "fallback")
        # 显示能力信息（如果有）
        contract = cand.get("capabilityContract") or {}
        caps = []
        if contract.get("servesApi"):
            caps.append("API")
        if contract.get("requiresDatabase"):
            caps.append("DB")
        if contract.get("requiresMigrations"):
            caps.append("迁移")
        if contract.get("servesUi"):
            caps.append("UI")
        cap_str = f" [{', '.join(caps)}]" if caps else ""
        typer.echo(f"  #{idx} {kind}（tier={tier}）{cap_str}")

    # 非交互式环境 -> fail-closed
    if not sys.stdin.isatty():
        typer.secho(
            "\n非交互式环境：无法确认降级。使用 --auto-fallback 或 --yes 自动降级。",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    # 交互式确认
    if not typer.confirm("\n是否降级到等价候选？", default=False):
        typer.echo("已取消降级")
        raise typer.Exit(code=1)

    # 确认后以 auto-equivalent 重试
    typer.secho("正在降级到等价候选...", fg=typer.colors.YELLOW)
    return start_instance(
        ws,
        config,
        reg,
        instance_id,
        fallback_policy="auto-equivalent",
    )


def stop(instance_id: str = typer.Argument(..., help="要停止的实例 ID")) -> None:
    """停止实例（禁用静态路由 / 容器 compose stop，不删数据）。"""
    from local_webpage_access.lifecycle import stop_instance_op

    try:
        ws, config, reg = open_workspace_registry()
        try:
            stop_instance_op(ws, config, reg, instance_id)
        finally:
            reg.close()
        typer.secho(f"已停止实例：{instance_id}", fg=typer.colors.GREEN)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


def restart(instance_id: str = typer.Argument(..., help="要重启的实例 ID")) -> None:
    """重启实例（先停再启，容器走轻量 compose start）。"""
    from local_webpage_access.lifecycle import restart_instance

    try:
        ws, config, reg = open_workspace_registry()
        try:
            restart_instance(ws, config, reg, instance_id)
        finally:
            reg.close()
        typer.secho(f"已重启实例：{instance_id}", fg=typer.colors.GREEN)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


def recover(instance_id: str = typer.Argument(..., help="要恢复的实例 ID")) -> None:
    """恢复实例（网关掉线时先拉起 Caddy，再 restart；对齐管理页 recover）。"""
    from local_webpage_access.lifecycle import recover_instance

    try:
        ws, config, reg = open_workspace_registry()
        try:
            recover_instance(ws, config, reg, instance_id)
        finally:
            reg.close()
        typer.secho(f"已恢复实例：{instance_id}", fg=typer.colors.GREEN)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


def rebuild(
    instance_id: str = typer.Argument(..., help="要重建的实例 ID"),
    sync: bool = typer.Option(
        False,
        "--sync",
        help="issue #8：重建前先同步上游源码（folder 源走 --from-dir 更新管线，git 源走 --from-git 更新管线）",
    ),
) -> None:
    """重建实例（强制重新构建镜像 / 产物，经构建队列限流）。

    issue #8：重建前检测上游源码是否已变更——默认只打印醒目警告不阻断；
    ``--sync`` 时先走更新管线同步源码再重建（zip / 无源实例不支持）。
    """
    from local_webpage_access.lifecycle import rebuild_instance

    try:
        ws, config, reg = open_workspace_registry()
        try:
            if sync:
                _sync_source_before_rebuild(ws, config, reg, instance_id)
            stale_warnings: list[str] = []
            rebuild_instance(ws, config, reg, instance_id, out=stale_warnings)
        finally:
            reg.close()
        for warning in stale_warnings:
            typer.secho(f"⚠ 源码陈旧：{warning}", fg=typer.colors.YELLOW)
        typer.secho(f"已重建实例：{instance_id}", fg=typer.colors.GREEN)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


def _sync_source_before_rebuild(ws: Any, config: Any, reg: Any, instance_id: str) -> None:
    """issue #8：``lwa rebuild --sync`` 的前置源码同步。

    按 manifest 的 ``sourceKind`` 复用导入更新管线（与
    ``lwa import --from-dir/--from-git --update`` 同一路径，不重写）：
    folder → ``Importer.update_from_dir``；git → ``Importer.update_from_git``。
    更新管线自带无变更短路；这里 ``restart=False``，由随后的 rebuild 统一重建。
    zip / 无源实例不支持 --sync，直接报错退出（exit 2）。
    """
    from local_webpage_access.importer import Importer
    from local_webpage_access.models import InstanceManifest

    manifest_path = ws.app_manifest_path(instance_id)
    if not manifest_path.is_file():
        return  # 实例不存在：交给 rebuild_instance 出标准错误
    source_kind = getattr(InstanceManifest.load(manifest_path), "sourceKind", "zip")
    importer = Importer(ws, config, reg)
    if source_kind == "folder":
        typer.secho(f"正在从关联源码目录同步：{instance_id} …", fg=typer.colors.CYAN)
        result = importer.update_from_dir(instance_id, restart=False, keep_data=True, yes=True)
    elif source_kind == "git":
        typer.secho(f"正在从 GitHub 远端同步：{instance_id} …", fg=typer.colors.CYAN)
        result = importer.update_from_git(instance_id, restart=False, keep_data=True, yes=True)
    else:
        typer.secho(
            f"实例 {instance_id} 无上游源码（sourceKind={source_kind!r}），"
            "--sync 仅支持 folder/git 源实例；zip 实例请用 lwa import --update 加新 zip。",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    if getattr(result, "skipped", False):
        typer.echo("  源码无变更，直接重建。")
    else:
        typer.secho("  源码已同步，继续重建。", fg=typer.colors.GREEN)


def cancel_build(
    instance_id: str = typer.Argument(..., help="要取消构建的实例 ID"),
) -> None:
    """取消排队中或进行中的构建（IMP-039）。

    排队任务立即 cancelled；进行中任务先 cancelling 再杀进程树，成功为
    cancelled、失败为 cancel_failed——不会在仅收到请求时假报已停。
    """
    from local_webpage_access.lifecycle import cancel_build as do_cancel

    try:
        ws, config, reg = open_workspace_registry()
        try:
            result = do_cancel(ws, config, reg, instance_id)
        finally:
            reg.close()
        outcome = getattr(result, "outcome", "unknown")
        message = getattr(result, "message", "") or outcome
        if outcome == "cancelled":
            typer.secho(f"已取消构建：{instance_id}（{message}）", fg=typer.colors.GREEN)
        elif outcome == "cancel_failed":
            typer.secho(
                f"取消失败：{instance_id}（{message}）",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        elif outcome == "already_done":
            typer.secho(
                f"无活动构建可取消：{instance_id}（{message}）",
                fg=typer.colors.YELLOW,
            )
        else:
            typer.secho(
                f"取消请求结果={outcome}：{instance_id}（{message}）",
                fg=typer.colors.YELLOW,
            )
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


def remove(
    instance_id: str = typer.Argument(None, help="要移除的实例 ID"),
    purge: bool = typer.Option(False, "--purge", help="同时删除 apps/<id>/ 磁盘文件"),
    force: bool = typer.Option(False, "--force", help="purge 时强制删除非空 data/（默认保护）"),
    redundant: bool = typer.Option(
        False,
        "--redundant",
        help="IMP-012：批量移除冗余实例（按原始 zip 指纹去重，保留每组最早者）",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="非交互确认（CI / 脚本调用）"),
) -> None:
    """移除实例（默认保留磁盘文件与 data/，仅删 registry 索引）。

    ``--redundant``（IMP-012）：批量清理冗余实例——由同一原始 zip 重复导入产生，
    按其 sha256 指纹分组，保留每组 createdAt 最早者，其余移除。执行前打印待删
    列表与指纹供确认。
    """
    from local_webpage_access.lifecycle import (
        list_redundant_instances,
        remove_instance,
        remove_redundant,
    )

    try:
        ws, config, reg = open_workspace_registry()
        try:
            if redundant:
                targets = list_redundant_instances(ws, reg)
                if not targets:
                    typer.secho(
                        "没有冗余实例（所有实例的原始 zip 指纹均唯一）",
                        fg=typer.colors.GREEN,
                    )
                    return
                typer.secho(
                    f"发现 {len(targets)} 个冗余实例（将保留每组最早者）：",
                    fg=typer.colors.YELLOW,
                )
                for desc in targets:
                    typer.echo(
                        f"  {desc['id']:<24} {desc['name']:<16} "
                        f"sha256:{desc['sourceZipHash'][:12]} ({desc['createdAt']})"
                    )
                if not yes:
                    if not typer.confirm("确认移除以上冗余实例？", default=False):
                        typer.echo("已取消")
                        return
                removed = remove_redundant(ws, config, reg, purge=purge, force=force)
                typer.secho(
                    f"已移除 {len(removed)} 个冗余实例",
                    fg=typer.colors.GREEN,
                )
                return

            if not instance_id:
                typer.secho(
                    "请提供实例 ID，或使用 --redundant 批量清理冗余实例",
                    fg=typer.colors.RED,
                    err=True,
                )
                raise typer.Exit(code=1)
            remove_instance(ws, config, reg, instance_id, purge=purge, force=force)
        finally:
            reg.close()
        if purge:
            typer.secho(f"已移除实例（含磁盘文件）：{instance_id}", fg=typer.colors.GREEN)
        else:
            typer.secho(f"已移除实例（保留磁盘文件）：{instance_id}", fg=typer.colors.GREEN)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


def logs(
    instance_id: str = typer.Argument(..., help="实例 ID"),
    category: str = typer.Option(
        "run", "--category", "-c", help="日志分类：build/run/gateway/import/scan"
    ),
    tail: int = typer.Option(200, "--tail", "-n", help="显示最近 N 行"),
) -> None:
    """查看实例日志（默认 run，可选 build/gateway 等）。"""
    from local_webpage_access.logs import list_logs, read_log

    try:
        ws, _config, reg = open_workspace_registry()
        try:
            text = read_log(ws, instance_id, category, tail=tail)
            if not text:
                available = [i.category for i in list_logs(ws, instance_id)]
                hint = f"（可用分类：{', '.join(available) or '无'}）" if available else ""
                typer.secho(f"日志 {category}.log 不存在或为空{hint}", fg=typer.colors.YELLOW)
            else:
                typer.echo(text)
        finally:
            reg.close()
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


def register(app: typer.Typer) -> None:
    """把本模块命令注册到根 app（保持顶层命令名不变）。"""
    app.command()(start)
    app.command()(stop)
    app.command()(restart)
    app.command()(recover)
    app.command()(rebuild)
    app.command("cancel-build")(cancel_build)
    app.command()(remove)
    app.command()(logs)
