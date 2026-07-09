"""生命周期命令：``lwa start/stop/restart/rebuild/remove/logs``。

DEV-044（WBS-20260708 阶段5.1）：从原 ``cli.py`` 按功能域拆出。
"""

from __future__ import annotations

import typer

from local_webpage_access.cli._common import log, open_workspace_registry
from local_webpage_access.errors import LwaError


def start(instance_id: str = typer.Argument(..., help="要启动的实例 ID")) -> None:
    """启动实例（静态 / 前端 / 容器统一入口）。"""
    from local_webpage_access.lifecycle import start_instance

    try:
        ws, config, reg = open_workspace_registry()
        try:
            manifest = start_instance(ws, config, reg, instance_id)
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


def rebuild(instance_id: str = typer.Argument(..., help="要重建的实例 ID")) -> None:
    """重建实例（强制重新构建镜像 / 产物，经构建队列限流）。"""
    from local_webpage_access.lifecycle import rebuild_instance

    try:
        ws, config, reg = open_workspace_registry()
        try:
            rebuild_instance(ws, config, reg, instance_id)
        finally:
            reg.close()
        typer.secho(f"已重建实例：{instance_id}", fg=typer.colors.GREEN)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


def remove(
    instance_id: str = typer.Argument(None, help="要移除的实例 ID"),
    purge: bool = typer.Option(False, "--purge", help="同时删除 apps/<id>/ 磁盘文件"),
    force: bool = typer.Option(
        False, "--force", help="purge 时强制删除非空 data/（默认保护）"
    ),
    redundant: bool = typer.Option(
        False,
        "--redundant",
        help="IMP-012：批量移除冗余实例（按原始 zip 指纹去重，保留每组最早者）",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="非交互确认（CI / 脚本调用）"
    ),
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
            typer.secho(
                f"已移除实例（保留磁盘文件）：{instance_id}", fg=typer.colors.GREEN
            )
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
                typer.secho(
                    f"日志 {category}.log 不存在或为空{hint}", fg=typer.colors.YELLOW
                )
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
    app.command()(rebuild)
    app.command()(remove)
    app.command()(logs)
