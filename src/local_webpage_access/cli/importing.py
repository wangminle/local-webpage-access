"""导入与扫描命令：``lwa import`` / ``lwa scan``。

DEV-044（WBS-20260708 阶段5.1）：从原 ``cli.py`` 按功能域拆出。
"""

from __future__ import annotations

import typer

from local_webpage_access.cli._common import log, open_workspace_registry
from local_webpage_access.errors import LwaError


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
        ws, config, reg = open_workspace_registry()
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
    # IMP-015：检测到业务 .env.example 时提示用户部署后填写密钥（不自动填）。
    if (result.app_dir / "current" / ".env.example").is_file():
        typer.secho(
            "  检测到 .env.example：部署后会复制为 docker/.env.example；"
            "业务密钥请填入 docker/.env.local（compose 自动注入，缺失不报错）",
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


def scan(
    instance_id: str = typer.Argument(None, help="要重新扫描的实例 ID（省略则扫所有 pending）"),
) -> None:
    """重新扫描实例（或所有 pending 实例），刷新运行形态识别。"""
    from local_webpage_access.importer import apply_detection_to_manifest
    from local_webpage_access.models import InstanceManifest, Status
    from local_webpage_access.scanner import Scanner

    try:
        ws, config, reg = open_workspace_registry()
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


def register(app: typer.Typer) -> None:
    """把本模块命令注册到根 app（保持顶层命令名不变）。"""
    app.command("import")(import_cmd)
    app.command()(scan)
