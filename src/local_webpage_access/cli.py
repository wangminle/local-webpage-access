"""``lwa`` CLI 入口。

命令语义见 V1 设计说明第 10 节。Phase 0~2 先实现 init/version；
后续 import/scan/start/stop 等命令在各阶段逐步补充。
"""

from __future__ import annotations

import sys

import typer

from local_webpage_access import PRODUCT_NAME
from local_webpage_access.errors import LwaError
from local_webpage_access.logging import get_logger, setup_logging

app = typer.Typer(
    name="lwa",
    help=f"{PRODUCT_NAME} — 面向局域网小主机的本地网页部署基座",
    no_args_is_help=True,
    add_completion=False,
)

log = get_logger("cli")


def _bootstrap(level: str = "INFO") -> None:
    """在每条命令执行前初始化日志（幂等）。"""
    setup_logging(level=level)  # type: ignore[arg-type]


@app.callback()
def main_callback(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="输出 DEBUG 级别日志"),
) -> None:
    f"""{PRODUCT_NAME} 命令行工具。"""
    _bootstrap("DEBUG" if verbose else "INFO")


@app.command()
def version() -> None:
    """显示版本号（与 Git commit 主题 ``V0.4.3-Build...`` 对齐）。"""
    from local_webpage_access.version_info import display_version

    typer.echo(display_version())


@app.command()
def init(
    workspace: str = typer.Option(
        ".",
        "--workspace",
        "-w",
        help="工作区根目录，默认当前目录",
    ),
    force: bool = typer.Option(False, "--force", help="已存在时仍重新生成配置和模板"),
) -> None:
    f"""初始化 {PRODUCT_NAME} 工作区（目录 / 配置 / SQLite registry）。"""
    from pathlib import Path

    from local_webpage_access.init_workspace import init_workspace

    try:
        ws = Path(workspace).resolve()
        summary = init_workspace(ws, force=force)
        typer.secho(f"已初始化工作区：{ws}", fg=typer.colors.GREEN)
        typer.echo(summary)
        typer.echo("\n下一步：把 zip 放入 inbox/ 后执行 `lwa import inbox/xxx.zip`")
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


def _open_workspace_registry():
    """定位工作区并打开 registry，返回 (workspace, config, registry)。"""
    from local_webpage_access.config import load_config
    from local_webpage_access.paths import require_workspace
    from local_webpage_access.registry import Registry

    ws = require_workspace()
    config = load_config(ws)
    reg = Registry(ws.db_path)
    reg.open()
    return ws, config, reg


@app.command("import")
def import_cmd(
    zip_path: str = typer.Argument(..., help="要导入的 zip 文件路径"),
    name: str = typer.Option(None, "--name", "-n", help="实例显示名称（默认从文件名推导）"),
    path_alias: str = typer.Option(
        None,
        "--path-alias",
        help="路径别名 slug（IMP-006，仅静态站点）；启用后可通过 http://<LAN-IP>:<staticGatewayPort>/<alias>/ 访问",
    ),
    update: str = typer.Option(
        None,
        "--update",
        "-u",
        help="更新已有实例（IMP-009）：原地覆盖 current/、保留 id/hostPort/data/，而非新建",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="非交互确认（CI / daemon 调用）"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="仅预演：展示 hash 差异与形态变化，不写盘"
    ),
    no_restart: bool = typer.Option(
        False,
        "--no-restart",
        help="更新后不自动 restart（维护窗口；默认：若原 running 则 restart）",
    ),
    no_keep_data: bool = typer.Option(
        False,
        "--no-keep-data",
        help="更新时清空 data/（默认保留 data/）",
    ),
    force_kind_change: bool = typer.Option(
        False,
        "--force-kind-change",
        help="允许新 zip 的 kind/runtime 与原实例不同（默认拒绝；确认迁移时仍保留 hostPort 登记）",
    ),
) -> None:
    """导入一个 zip 包：解压、识别、登记实例。

    加 ``--update <id>``（IMP-009）则改为原地更新已有实例：保留 instance_id、
    hostPort、data/、desiredState 与路径别名，仅覆盖业务源码并按需 restart。
    """
    from local_webpage_access.importer import Importer

    try:
        ws, config, reg = _open_workspace_registry()
        try:
            importer = Importer(ws, config, reg)
            if update is not None:
                _do_update(
                    importer,
                    ws,
                    config,
                    reg,
                    zip_path=zip_path,
                    instance_id=update,
                    restart=not no_restart,
                    keep_data=not no_keep_data,
                    yes=yes,
                    dry_run=dry_run,
                    force_kind_change=force_kind_change,
                )
            else:
                # IMP-009：CLI 路径下 slug 冲突不再 silent 建 -2，提示 --update
                result = importer.import_zip(
                    zip_path,
                    name=name,
                    path_alias=path_alias,
                    on_conflict="error",
                )
                _print_import_result(result, config)
        finally:
            reg.close()
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


def _print_import_result(result, config) -> None:
    """渲染 :class:`ImportResult`（导入与更新共用）。"""
    typer.secho(f"已导入实例：{result.instance_id}", fg=typer.colors.GREEN)
    typer.echo(f"  名称：{result.manifest.name}")
    typer.echo(f"  形态：{result.detection.form}（置信度 {result.detection.confidence}）")
    typer.echo(f"  类型：{result.manifest.kind} / {result.manifest.runtime}")
    typer.echo(f"  目录：{result.app_dir}")
    typer.echo(f"  sha256：{result.zip_hash}")
    # IMP-006：导入时登记了路径别名则提示（实际 URL 在 start 后生成）
    if (
        result.manifest.static is not None
        and result.manifest.static.routeMode == "name"
        and result.manifest.static.routeHost
    ):
        typer.secho(
            f"  路径别名：/{result.manifest.static.routeHost}/"
            f"（lwa start 后生效，入口端口 {config.staticGatewayPort}）",
            fg=typer.colors.CYAN,
        )
    # IMP-001：剥离摘要（仅当实际剥离了冗余成员时显示）
    if result.sanitized and result.sanitized.stripped_names:
        san = result.sanitized
        parts = ", ".join(
            f"{rule}×{n}"
            for rule, n in sorted(san.categories.items(), key=lambda kv: -kv[1])
        )
        typer.secho(
            f"  已剥离冗余成员 {len(san.stripped_names)} 项"
            f"（含 symlink {san.stripped_symlink_count}）：{parts}",
            fg=typer.colors.CYAN,
        )
    if result.detection.pending:
        typer.secho(
            f"  注意：{result.manifest.lastError}（已标记 pending，需人工或 skill 介入）",
            fg=typer.colors.YELLOW,
        )


def _do_update(
    importer,
    ws,
    config,
    reg,
    *,
    zip_path: str,
    instance_id: str,
    restart: bool,
    keep_data: bool,
    yes: bool,
    dry_run: bool,
    force_kind_change: bool,
) -> None:
    """IMP-009：``lwa import --update <id>`` 的编排（数据层 + 可选 restart）。"""
    result = importer.update_zip(
        zip_path,
        instance_id,
        restart=restart,
        keep_data=keep_data,
        yes=yes,
        dry_run=dry_run,
        force_kind_change=force_kind_change,
    )

    prev_short = result.prev_hash[:12] if result.prev_hash else "∅"
    new_short = result.zip_hash[:12]

    if result.skipped:
        typer.secho(
            f"实例 {instance_id} 的 zip 未变化（sha256 {new_short}），已跳过更新。",
            fg=typer.colors.YELLOW,
        )
        return

    if result.dry_run:
        typer.secho(
            f"[dry-run] 实例 {instance_id}：sha256 {prev_short} → {new_short}",
            fg=typer.colors.CYAN,
        )
        if result.detection is not None:
            typer.echo(f"  新形态：{result.detection.form}")
        if result.kind_changed:
            typer.secho(
                "  ⚠ 形态将变化，需 --force-kind-change 才能实际更新",
                fg=typer.colors.YELLOW,
            )
        if result.was_running:
            typer.echo("  原状态：running（实际更新后将 restart）")
        if result.sanitized and result.sanitized.stripped_names:
            typer.echo(
                f"  将剥离冗余成员 {len(result.sanitized.stripped_names)} 项"
            )
        return

    typer.secho(f"已更新实例：{instance_id}", fg=typer.colors.GREEN)
    typer.echo(f"  sha256：{prev_short} → {new_short}")
    if result.detection is not None:
        typer.echo(f"  形态：{result.detection.form}（置信度 {result.detection.confidence}）")
    typer.echo(f"  目录：{result.app_dir}")
    if result.sanitized and result.sanitized.stripped_names:
        san = result.sanitized
        parts = ", ".join(
            f"{rule}×{n}"
            for rule, n in sorted(san.categories.items(), key=lambda kv: -kv[1])
        )
        typer.secho(
            f"  已剥离冗余成员 {len(san.stripped_names)} 项"
            f"（含 symlink {san.stripped_symlink_count}）：{parts}",
            fg=typer.colors.CYAN,
        )

    # needs_restart=True：调用方执行 restart（hostPort 由 hosting 复用，不变）
    if result.needs_restart:
        from local_webpage_access.lifecycle import restart_instance

        typer.secho("  正在 restart…", fg=typer.colors.CYAN)
        restart_instance(ws, config, reg, instance_id)
        typer.secho("  已 restart，端口不变", fg=typer.colors.GREEN)


@app.command()
def scan(
    instance_id: str = typer.Argument(None, help="要重新扫描的实例 ID（省略则扫所有 pending）"),
) -> None:
    """重新扫描实例（或所有 pending 实例），刷新运行形态识别。"""
    from local_webpage_access.importer import apply_detection_to_manifest
    from local_webpage_access.models import InstanceManifest, Status
    from local_webpage_access.scanner import Scanner

    try:
        ws, config, reg = _open_workspace_registry()
        try:
            scanner = Scanner()
            ids: list[str]
            if instance_id:
                ids = [instance_id]
            else:
                ids = [
                    row["id"]
                    for row in reg.list_instances()
                    if row["status"] == Status.PENDING.value
                ]

            if not ids:
                typer.echo("没有待扫描的实例。")
                return

            for iid in ids:
                current_dir = ws.app_current(iid)
                detection = scanner.detect(current_dir)
                manifest_path = ws.app_manifest_path(iid)
                if not manifest_path.is_file():
                    typer.secho(f"  {iid}：缺少 local-web.json，跳过", fg=typer.colors.YELLOW)
                    continue
                manifest = InstanceManifest.load(manifest_path)
                manifest = apply_detection_to_manifest(manifest, detection, ws)
                manifest.save(manifest_path)
                reg.upsert_from_manifest(manifest)
                reg.add_event(iid, "scan", f"重新扫描：{detection.form}（{detection.confidence}）")
                status_label = detection.form if not detection.pending else "pending"
                typer.echo(f"  {iid}：{status_label}（{detection.confidence}）")
        finally:
            reg.close()
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command()
def start(instance_id: str = typer.Argument(..., help="要启动的实例 ID")) -> None:
    """启动实例（静态 / 前端 / 容器统一入口）。"""
    from local_webpage_access.lifecycle import start_instance

    try:
        ws, config, reg = _open_workspace_registry()
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


alias_app = typer.Typer(help="管理实例路径别名（IMP-006）")


@alias_app.command("set")
def alias_set(
    instance_id: str = typer.Argument(..., help="实例 ID"),
    slug: str = typer.Argument(..., help="路径别名 slug"),
) -> None:
    """为静态实例设置路径别名。"""
    from local_webpage_access.path_alias import set_instance_path_alias

    try:
        ws, config, reg = _open_workspace_registry()
        try:
            result = set_instance_path_alias(ws, config, reg, instance_id, slug)
        finally:
            reg.close()
        if result.unchanged:
            typer.echo(f"实例 {instance_id} 路径别名未变化：{slug}")
            return
        typer.secho(f"已设置路径别名：/{slug}/", fg=typer.colors.GREEN)
        if result.route_url:
            typer.echo(f"  入口：{result.route_url}")
        elif slug and not result.alias_entry_enabled:
            typer.secho(
                "  别名已登记，但当前静态后端非 Caddy，仅端口可达",
                fg=typer.colors.YELLOW,
            )
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@alias_app.command("clear")
def alias_clear(
    instance_id: str = typer.Argument(..., help="实例 ID"),
) -> None:
    """清除静态实例的路径别名。"""
    from local_webpage_access.path_alias import set_instance_path_alias

    try:
        ws, config, reg = _open_workspace_registry()
        try:
            result = set_instance_path_alias(ws, config, reg, instance_id, None)
        finally:
            reg.close()
        if result.unchanged:
            typer.echo(f"实例 {instance_id} 本无路径别名")
            return
        typer.secho(f"已清除实例 {instance_id} 的路径别名", fg=typer.colors.GREEN)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


app.add_typer(alias_app, name="alias")


@app.command()
def stop(instance_id: str = typer.Argument(..., help="要停止的实例 ID")) -> None:
    """停止实例（禁用静态路由 / 容器 compose stop，不删数据）。"""
    from local_webpage_access.lifecycle import stop_instance_op

    try:
        ws, config, reg = _open_workspace_registry()
        try:
            stop_instance_op(ws, config, reg, instance_id)
        finally:
            reg.close()
        typer.secho(f"已停止实例：{instance_id}", fg=typer.colors.GREEN)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command()
def restart(instance_id: str = typer.Argument(..., help="要重启的实例 ID")) -> None:
    """重启实例（先停再启，容器走轻量 compose start）。"""
    from local_webpage_access.lifecycle import restart_instance

    try:
        ws, config, reg = _open_workspace_registry()
        try:
            restart_instance(ws, config, reg, instance_id)
        finally:
            reg.close()
        typer.secho(f"已重启实例：{instance_id}", fg=typer.colors.GREEN)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command()
def rebuild(instance_id: str = typer.Argument(..., help="要重建的实例 ID")) -> None:
    """重建实例（强制重新构建镜像 / 产物，经构建队列限流）。"""
    from local_webpage_access.lifecycle import rebuild_instance

    try:
        ws, config, reg = _open_workspace_registry()
        try:
            rebuild_instance(ws, config, reg, instance_id)
        finally:
            reg.close()
        typer.secho(f"已重建实例：{instance_id}", fg=typer.colors.GREEN)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command()
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
        ws, config, reg = _open_workspace_registry()
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


@app.command()
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
        ws, _config, reg = _open_workspace_registry()
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


@app.command()
def status(
    instance_id: str = typer.Argument(None, help="实例 ID（省略则显示全部）"),
) -> None:
    """查看实例状态（省略 ID 时显示所有实例）。"""
    from local_webpage_access.status import all_statuses, instance_status, sync_status

    try:
        ws, config, reg = _open_workspace_registry()
        try:
            sync_status(ws, config, reg, instance_id)
            if instance_id:
                statuses = [instance_status(ws, config, reg, instance_id)]
            else:
                statuses = all_statuses(ws, config, reg)
        finally:
            reg.close()

        if not statuses:
            typer.echo("（暂无实例）")
            return
        typer.echo(
            f"{'ID':20} {'KIND':8} {'RUNTIME':16} {'STATUS':10} {'DESIRED':10} {'PORT':6} NAME"
        )
        for s in statuses:
            port = str(s.host_port) if s.host_port else "-"
            typer.echo(
                f"{s.id[:20]:20} {s.kind:8} {s.runtime:16} "
                f"{s.status:10} {s.desired_state:10} {port:6} {s.name}"
            )
            # IMP-007：容器实例展示端口映射（internalPort→hostPort）
            if s.port_mapping_label:
                typer.echo(f"  ↳ 映射：{s.port_mapping_label}")
            # IMP-006：路径别名入口 URL
            if s.route_url:
                typer.secho(f"  ↳ 路径：{s.route_url}", fg=typer.colors.CYAN)
            if s.last_error:
                typer.secho(f"  ↳ lastError: {s.last_error}", fg=typer.colors.RED)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command()
def stats(
    instance_id: str = typer.Argument(None, help="实例 ID（省略则显示全部+整机）"),
) -> None:
    """查看资源占用（整机 + 实例目录/容器资源）。"""
    from local_webpage_access.stats import (
        all_instance_resources,
        host_resources,
        instance_resources,
    )

    try:
        ws, config, reg = _open_workspace_registry()
        try:
            host = host_resources(root=ws.root)
            if instance_id:
                infos = [instance_resources(ws, config, reg, instance_id)]
            else:
                infos = all_instance_resources(ws, config, reg)
        finally:
            reg.close()

        # 整机
        typer.secho("== 整机 ==", fg=typer.colors.CYAN)
        if host.mem_total_bytes is not None:
            mem_used = host.mem_used_bytes or 0
            typer.echo(
                f"  内存：{_fmt_bytes(mem_used)} / {_fmt_bytes(host.mem_total_bytes)}"
            )
        else:
            typer.echo("  内存：（非 Linux，已跳过）")
        if host.load_avg_1m is not None:
            typer.echo(f"  负载：1m={host.load_avg_1m:.2f} 5m={host.load_avg_5m:.2f}")
        typer.echo(
            f"  磁盘：{_fmt_bytes(host.disk_used_bytes or 0)} / "
            f"{_fmt_bytes(host.disk_total_bytes or 0)}"
        )

        # 实例
        typer.secho("== 实例 ==", fg=typer.colors.CYAN)
        if not infos:
            typer.echo("（暂无实例）")
            return
        for info in infos:
            typer.echo(f"  {info.instance_id}")
            typer.echo(f"    源码：{_fmt_bytes(info.source_size_bytes)}")
            typer.echo(f"    public：{_fmt_bytes(info.public_size_bytes)}")
            typer.echo(f"    data：{_fmt_bytes(info.data_size_bytes)}")
            if info.image_size_bytes is not None:
                typer.echo(f"    镜像：{_fmt_bytes(info.image_size_bytes)}")
            if info.last_memory_bytes is not None:
                typer.echo(f"    容器内存：{_fmt_bytes(info.last_memory_bytes)}")
            if info.last_cpu_percent is not None:
                typer.echo(f"    容器CPU：{info.last_cpu_percent:.2f}%")
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


def _fmt_bytes(n: int | None) -> str:
    """字节数格式化为人类可读。"""
    if n is None:
        return "-"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PiB"


@app.command("list")
def list_cmd() -> None:
    """列出所有实例及其状态。"""
    from local_webpage_access.status import all_statuses

    try:
        ws, config, reg = _open_workspace_registry()
        try:
            statuses = all_statuses(ws, config, reg)
        finally:
            reg.close()
        if not statuses:
            typer.echo("（暂无实例）")
            return
        typer.echo(f"{'ID':20} {'KIND':8} {'RUNTIME':16} {'STATUS':10} {'PORT':6} NAME")
        for s in statuses:
            port = str(s.host_port) if s.host_port else "-"
            typer.echo(
                f"{s.id[:20]:20} {s.kind:8} {s.runtime:16} "
                f"{s.status:10} {port:6} {s.name}"
            )
            # IMP-007：容器实例展示端口映射（internalPort→hostPort）
            if s.port_mapping_label:
                typer.echo(f"  ↳ 映射：{s.port_mapping_label}")
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


# ---- daemon 子命令（WBS-21）-------------------------------------------------

daemon_app = typer.Typer(help="控制 daemon 自动导入模式")


@daemon_app.command("on")
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
        ws, config, _reg = _open_workspace_registry()
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


@daemon_app.command("off")
def daemon_off() -> None:
    """关闭 daemon：停止自动监听 inbox/。"""
    from local_webpage_access import daemon as daemon_mod
    from local_webpage_access.paths import require_workspace

    try:
        ws = require_workspace()
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


@daemon_app.command("status")
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


app.add_typer(daemon_app, name="daemon")


# ---- manager 子命令（WBS-22.13）---------------------------------------------

manager_app = typer.Typer(help="控制管理页 HTTP 服务")


@manager_app.command("on")
def manager_on() -> None:
    """后台启动管理页（默认 init 后自动执行；managerEnabled=false 时禁用）。"""
    from local_webpage_access.manager_api import ensure_token
    from local_webpage_access.manager_service import start_manager
    from local_webpage_access.ports import resolve_lan_ip

    try:
        ws, config, _reg = _open_workspace_registry()
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


@manager_app.command("off")
def manager_off() -> None:
    """停止后台管理页。"""
    from local_webpage_access.manager_service import stop_manager

    try:
        ws, _config, _reg = _open_workspace_registry()
        _reg.close()
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


@manager_app.command("status")
def manager_status_cmd() -> None:
    """查看管理页运行状态。"""
    from local_webpage_access.manager_api import read_token
    from local_webpage_access.manager_service import manager_status
    from local_webpage_access.ports import resolve_lan_ip

    try:
        ws, config, _reg = _open_workspace_registry()
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


@manager_app.command("start")
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
        ws, config, _reg = _open_workspace_registry()
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


app.add_typer(manager_app, name="manager")


# ---- gateway 子命令（IMP-010 / DEV-041，WBS 0.7）---------------------------

gateway_app = typer.Typer(help="控制 Caddy 网关（master 生命周期与 :8080 别名入口）")


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


@gateway_app.command("on")
def gateway_on() -> None:
    """启动 Caddy 网关（master + admin :2019，别名入口随配置就绪）。"""
    from local_webpage_access.gateway_service import start_gateway
    from local_webpage_access.ports import resolve_lan_ip

    try:
        ws, config, _reg = _open_workspace_registry()
        _reg.close()
        _require_caddy_version(config)
        pid = start_gateway(ws, config)
        lan_ip = resolve_lan_ip(config) or "127.0.0.1"
        typer.secho(f"网关已启动（pid={pid}）", fg=typer.colors.GREEN)
        typer.echo("  admin：http://127.0.0.1:2019/")
        port = config.staticGatewayPort
        if port:
            typer.echo(f"  别名入口：http://{lan_ip}:{port}/<alias>/")
        typer.echo("  停止：lwa gateway off；状态：lwa gateway status")
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@gateway_app.command("off")
def gateway_off() -> None:
    """停止 Caddy 网关（master 优雅关闭）。

    不做 ``MIN_CADDY_VERSION`` 校验：``stop_gateway`` 自身按 ``detect_backend``
    分支处理（caddy 走 admin API 优雅关闭；builtin 仅清残留服务态），即使当前
    ``staticGateway=builtin``（如刚从 caddy 切走）也能关掉仍在跑的 master 并清
    ``run/gateway.json``，避免"切 builtin 后无法用 CLI 关 Caddy"的死局。
    """
    from local_webpage_access.gateway_service import stop_gateway

    try:
        ws, config, _reg = _open_workspace_registry()
        _reg.close()
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


@gateway_app.command("status")
def gateway_status_cmd() -> None:
    """查看 Caddy 网关运行状态。"""
    from local_webpage_access.gateway_service import gateway_status
    from local_webpage_access.ports import resolve_lan_ip

    try:
        ws, config, _reg = _open_workspace_registry()
        _reg.close()
        st = gateway_status(ws, config)
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


app.add_typer(gateway_app, name="gateway")


# ---- setup 子命令（宿主机环境）----------------------------------------------


@app.command("setup")
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
            ws, config, _reg = _open_workspace_registry()
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


# ---- doctor 子命令（WBS-26）------------------------------------------------


@app.command("doctor")
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
        ws, config, _reg = _open_workspace_registry()
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


# ---- update 子命令（IMP-008：工作区热重载）----------------------------------


@app.command("update")
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


def run() -> None:
    """供 ``python -m local_webpage_access`` 或 entry point 调用。"""
    try:
        app()
    except LwaError as exc:
        # 兜底：子命令内部未捕获的业务异常
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        sys.exit(1)


if __name__ == "__main__":
    run()
