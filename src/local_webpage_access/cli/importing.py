"""导入与扫描命令：``lwa import`` / ``lwa scan``。

DEV-044（WBS-20260708 阶段5.1）：从原 ``cli.py`` 按功能域拆出。
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import typer

from local_webpage_access.cli._common import log, open_workspace_registry
from local_webpage_access.errors import LwaError


def import_cmd(
    zip_path: str = typer.Argument(
        None, help="要导入的 zip 文件路径（与 --from-dir / --from-git 互斥）"
    ),
    name: str = typer.Option(None, "--name", "-n", help="实例显示名称（默认从文件名推导）"),
    path_alias: str = typer.Option(
        None,
        "--path-alias",
        help="路径别名 slug（IMP-006，静态或容器实例均可；需 Caddy）；启用后可通过 http://<LAN-IP>:<staticGatewayPort>/<alias>/ 访问",
    ),
    update: str = typer.Option(
        None,
        "--update",
        "-u",
        help="更新已有实例（IMP-009）：原地覆盖 current/、保留 id/hostPort/data/，而非新建",
    ),
    from_dir: str = typer.Option(
        None,
        "--from-dir",
        help="IMP-047：从本机文件夹源导入（复制进工作区，非就地运行）。"
        "与 zip_path 互斥；加 --update <id> 时从关联源目录更新。",
    ),
    from_git: str = typer.Option(
        None,
        "--from-git",
        help="IMP-065：从 GitHub 仓库导入（https://github.com/<owner>/<repo>，"
        "浅克隆后复制进工作区）。与 zip_path / --from-dir 互斥；"
        "加 --update <id> 时对该实例做远端探测更新。",
    ),
    ref: str = typer.Option(
        None,
        "--ref",
        help="IMP-065：GitHub 分支或标签名；缺省跟仓库默认分支（仅 --from-git 时可用）",
    ),
    subdir: str = typer.Option(
        None,
        "--subdir",
        help="IMP-065：仓库内子目录（monorepo 场景只打包该目录；仅 --from-git 时可用）",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="非交互确认（CI / daemon 调用）"),
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
    """导入一个 zip 包、本机文件夹源或 GitHub 仓库：解压、识别、登记实例。

    加 ``--update <id>``（IMP-009）则改为原地更新已有实例：保留 instance_id、
    hostPort、data/、desiredState 与路径别名，仅覆盖业务源码并按需 restart。

    加 ``--from-dir <path>``（IMP-047）则从本机文件夹源导入：复制源目录内容
    进入工作区实例目录，而非就地运行关联目录。加 ``--update <id>`` 时
    从该实例的关联源目录更新。

    加 ``--from-git <url>``（IMP-065）则从 GitHub 仓库导入：一次性浅克隆到
    工作区外临时目录后复制进工作区；``--ref`` 指定分支/标签（缺省跟默认
    分支）、``--subdir`` 指定仓库子目录。加 ``--update <id>`` 时对已关联
    仓库做 ``ls-remote`` 无变更探测后原地升级。
    """
    from local_webpage_access.importer import Importer

    try:
        # 三源互斥（065.19）：zip 位置参数 / --from-dir / --from-git 恰选其一
        sources = [
            ("zip_path", zip_path),
            ("--from-dir", from_dir),
            ("--from-git", from_git),
        ]
        provided = [label for label, value in sources if value is not None]
        if len(provided) > 1:
            typer.secho(
                f"导入来源互斥（{'、'.join(provided)}），请只指定一个",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2)
        if not provided:
            typer.secho(
                "请提供 zip 文件路径、--from-dir <目录> 或 --from-git <仓库地址>",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2)

        if from_git is None and (ref is not None or subdir is not None):
            typer.secho(
                "--ref / --subdir 仅可与 --from-git 同时使用",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2)

        if update is not None and path_alias is not None:
            typer.secho(
                "--path-alias 不能与 --update 同时使用",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2)
        ws, config, reg = open_workspace_registry()
        try:
            importer = Importer(ws, config, reg)
            if from_git is not None:
                # IMP-065：GitHub 源
                if update is not None:
                    # BUG-556：更新按实例已记录的 ref/打包子目录进行；
                    # 显式传 --ref/--subdir 会被静默丢弃，须直接拒绝。
                    if ref is not None or subdir is not None:
                        typer.secho(
                            "--ref / --subdir 不能与 --from-git --update 同时使用："
                            "更新按实例导入时记录的分支与子目录进行；"
                            "如需更换，请删除实例后用新参数重新导入。",
                            fg=typer.colors.RED,
                            err=True,
                        )
                        raise typer.Exit(code=2)
                    _do_update_from_git(
                        importer,
                        ws,
                        config,
                        reg,
                        instance_id=update,
                        url=from_git,
                        restart=not no_restart,
                        keep_data=not no_keep_data,
                        yes=yes,
                        dry_run=dry_run,
                        force_kind_change=force_kind_change,
                    )
                else:
                    result = importer.import_from_git(
                        from_git,
                        ref=ref,
                        subdir=subdir,
                        name=name,
                        path_alias=path_alias,
                        on_conflict="error",
                    )
                    _print_import_result(result, config)
                    _print_git_source_note(result, importer)
            elif from_dir is not None:
                # IMP-047：文件夹源路径
                if update is not None:
                    _do_update_from_dir(
                        importer,
                        ws,
                        config,
                        reg,
                        instance_id=update,
                        from_dir=from_dir,
                        restart=not no_restart,
                        keep_data=not no_keep_data,
                        yes=yes,
                        dry_run=dry_run,
                        force_kind_change=force_kind_change,
                    )
                else:
                    result = importer.import_from_dir(
                        from_dir,
                        name=name,
                        path_alias=path_alias,
                        on_conflict="error",
                    )
                    _print_import_result(result, config)
                    _print_folder_source_note(result, importer)
            elif update is not None:
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
    # IMP-006 / IMP-014：导入时登记了路径别名则提示（实际 URL 在 start 后生成）
    alias = None
    if (
        result.manifest.static is not None
        and result.manifest.static.routeMode == "name"
        and result.manifest.static.routeHost
    ):
        alias = result.manifest.static.routeHost
    elif (
        result.manifest.container is not None
        and result.manifest.container.routeMode == "name"
        and result.manifest.container.routeHost
    ):
        alias = result.manifest.container.routeHost
    if alias:
        typer.secho(
            f"  路径别名：/{alias}/（lwa start 后生效，入口端口 {config.staticGatewayPort}）",
            fg=typer.colors.CYAN,
        )
    # IMP-015：检测到业务 .env.example 时提示用户部署后填写密钥（不自动填）。
    if (result.app_dir / "current" / ".env.example").is_file():
        typer.secho(
            "  检测到 .env.example：部署后会复制为 docker/.env.example；"
            "业务密钥请填入 docker/.env.local（compose 自动注入，缺失不报错）",
            fg=typer.colors.CYAN,
        )
    # issue #12：.env.local 配置指引改为无条件输出（此前只在检测到 .env.example
    # 时提示，源项目没有 example 就完全不知道往哪填密钥）。
    typer.secho(
        "  业务密钥/自定义环境变量：填入 docker/.env.local（compose 自动注入，缺失不报错）",
        fg=typer.colors.CYAN,
    )
    # issue #12：源目录 .env 会被 .dockerignore 排除、运行时也不注入容器，
    # 明确告警并指引迁移路径；绝不自动复制进镜像（密钥可能入 layer）。
    # BUG-591 同步：.dockerignore 用 **/.env 排除所有层级，检测同样覆盖
    # resolve_source_workdir 之后的实际项目根（sourceSubdir/monorepo，
    # 如 current/backend/.env），消息展示相对 current/ 的路径。
    current_dir = result.app_dir / "current"
    env_candidates: list[tuple[Path, str]] = [(current_dir, ".env")]
    source_subdir = getattr(result.manifest, "sourceSubdir", None)
    if source_subdir:
        from local_webpage_access.paths import resolve_source_workdir

        workdir = resolve_source_workdir(current_dir, source_subdir)
        if workdir != current_dir.resolve():
            env_candidates.append((workdir, f"{source_subdir}/.env"))
    for env_dir, env_rel in env_candidates:
        if (env_dir / ".env").is_file():
            typer.secho(
                f"  警告：检测到源目录 {env_rel}：不会进入镜像或容器（.dockerignore 排除）；"
                "业务键请手动迁移到 docker/.env.local",
                fg=typer.colors.YELLOW,
            )
    # IMP-001：剥离摘要（仅当实际剥离了冗余成员时显示）
    if result.sanitized and result.sanitized.stripped_names:
        san = result.sanitized
        parts = ", ".join(
            f"{rule}×{n}" for rule, n in sorted(san.categories.items(), key=lambda kv: -kv[1])
        )
        typer.secho(
            f"  已剥离冗余成员 {len(san.stripped_names)} 项"
            f"（含 symlink {san.stripped_symlink_count}）：{parts}",
            fg=typer.colors.CYAN,
        )
    # IMP-056 Gate-2：兼容性预检发现（B.05）
    _print_compatibility_findings(result.manifest.compatibilityFindings)
    if result.detection.pending:
        typer.secho(
            f"  注意：{result.manifest.lastError}（已标记 pending，需人工或 skill 介入）",
            fg=typer.colors.YELLOW,
        )


def _print_compatibility_findings(findings) -> None:
    """IMP-056 Gate-2：打印兼容性预检结果（B.05）。

    仅展示分级，不阻断 import/start/alias。
    """
    if not findings:
        return
    typer.secho(
        f"  兼容性预检：{len(findings)} 项（不阻断 / 仍以 IMP-055 为准）",
        fg=typer.colors.YELLOW,
    )
    for f in findings:
        icon = {"critical": "🔴", "warning": "🟡", "info": "🔵"}.get(f.severity, "⚪")
        loc = f" @ {f.file}:{f.line}" if f.file else ""
        typer.echo(f"    {icon} [{f.checkId}/{f.severity}] {f.title}{loc}")
        if f.code:
            typer.echo(f"       代码：{f.code}")
        typer.echo(f"       影响：{f.impact}")
        typer.echo(f"       建议：{f.fix}")


def _print_folder_source_note(result, importer) -> None:
    """IMP-047：文件夹源导入成功后的补充提示。"""
    from local_webpage_access.models import InstanceManifest

    manifest_path = importer.ws.app_manifest_path(result.instance_id)
    if manifest_path.is_file():
        manifest = InstanceManifest.load(manifest_path)
        source_dir = getattr(manifest, "sourceDirPath", None)
        if source_dir:
            typer.secho(
                f"  来源类型：本机文件夹（{source_dir}）",
                fg=typer.colors.CYAN,
            )
            typer.secho(
                f"  更新：lwa import --from-dir <目录> --update {result.instance_id}",
                fg=typer.colors.CYAN,
            )


def _print_git_source_note(result, importer) -> None:
    """IMP-065：GitHub 源导入成功后的补充提示（url / ref / 短 SHA）。"""
    from local_webpage_access.models import InstanceManifest

    manifest_path = importer.ws.app_manifest_path(result.instance_id)
    if not manifest_path.is_file():
        return
    manifest = InstanceManifest.load(manifest_path)
    url = getattr(manifest, "sourceGitUrl", None)
    ref = getattr(manifest, "sourceGitRef", None)
    ref_kind = getattr(manifest, "sourceGitRefKind", None)
    commit = getattr(manifest, "sourceGitCommit", None)
    if not url:
        return
    ref_label = f"{ref_kind} {ref}" if ref else "默认分支"
    short_sha = (commit or "")[:8]
    typer.secho(
        f"  来源类型：GitHub（{url}，{ref_label} @ {short_sha or '未知 OID'}）",
        fg=typer.colors.CYAN,
    )
    typer.secho(
        f"  更新：lwa import --from-git {url} --update {result.instance_id}",
        fg=typer.colors.CYAN,
    )


def _do_update_from_git(
    importer,
    ws,
    config,
    reg,
    *,
    instance_id: str,
    url: str,
    restart: bool,
    keep_data: bool,
    yes: bool,
    dry_run: bool,
    force_kind_change: bool,
) -> None:
    """IMP-065：``lwa import --from-git <url> --update <id>`` 的编排。

    传入 URL 经规范化后须与 manifest 的 ``sourceGitUrl`` 一致，否则
    ``source_mismatch``（在 ``update_from_git`` 内判定，禁止用另一仓库覆盖）。
    """
    # BUG-553：旧侧 OID 从更新前的 manifest 读取——UpdateResult.prev_hash 在
    # 真正升级后是打包 zip 的 sha256，与 commit 不是同一单位，不能混排展示。
    from local_webpage_access import git_source
    from local_webpage_access.models import InstanceManifest

    prev_commit = None
    stored_url: str | None = None
    manifest_path = ws.app_manifest_path(instance_id)
    if manifest_path.is_file():
        with contextlib.suppress(Exception):
            _manifest = InstanceManifest.load(manifest_path)
            prev_commit = getattr(_manifest, "sourceGitCommit", None)
            stored_url = getattr(_manifest, "sourceGitUrl", None)

    # 换源拒绝在 CLI 层前置为 exit 2（CHK-239 low-1：与 folder 的 BUG-440
    # 目录一致性预检同档；importer 的 source_mismatch 判定保留作 API 兜底）。
    if stored_url:
        try:
            provided_url = git_source.parse_github_url(url).url
        except LwaError:
            provided_url = None  # 非法 URL 交给 update_from_git 出结构化 errorKind
        if provided_url is not None and provided_url != stored_url:
            typer.secho(
                f"传入的仓库 {provided_url} 与实例 {instance_id} 关联的 {stored_url} 不一致。\n"
                "如需更换来源，请先删除实例再用新仓库重新导入。",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2)

    result = importer.update_from_git(
        instance_id,
        url=url,
        restart=restart,
        keep_data=keep_data,
        yes=yes,
        dry_run=dry_run,
        force_kind_change=force_kind_change,
    )

    prev_oid = (prev_commit or "")[:12] or "∅"
    new_oid = (getattr(result.manifest, "sourceGitCommit", None) or "")[:12]

    if result.skipped:
        typer.secho(
            f"实例 {instance_id} 的远端 git 源无变更（{new_oid or 'OID 未知'}），已跳过更新。",
            fg=typer.colors.YELLOW,
        )
        return

    if result.dry_run:
        typer.secho(
            f"[dry-run] 实例 {instance_id}：远端有新提交，将重新克隆打包并原地更新",
            fg=typer.colors.CYAN,
        )
        # dry-run 只能拿到打包 zip 的内容指纹（新旧 current 逻辑内容对比），
        # 不冒充 commit OID（BUG-553：单位不同不得混标）
        typer.echo(
            f"  打包内容指纹：{(result.prev_hash or '')[:12] or '∅'} -> "
            f"{(result.zip_hash or '')[:12]}"
        )
        if result.detection is not None:
            typer.echo(f"  新形态：{result.detection.form}")
        if result.kind_changed:
            typer.secho(
                "  ⚠ 形态将变化，需 --force-kind-change 才能实际更新",
                fg=typer.colors.YELLOW,
            )
        return

    typer.secho(f"已从 GitHub 源更新实例：{instance_id}", fg=typer.colors.GREEN)
    # 远端 OID 用更新前后 manifest 的 commit（result.manifest 已由
    # update_from_git 同步磁盘身份）；zip_hash 是打包内容指纹，单位不同，
    # 单独一行展示，不得混标（BUG-553）。
    typer.echo(f"  远端 OID：{prev_oid} -> {new_oid or '未知'}")
    typer.echo(
        f"  打包内容指纹：{(result.prev_hash or '')[:12] or '∅'} -> "
        f"{(result.zip_hash or '')[:12]}"
    )
    if result.detection is not None:
        typer.echo(f"  形态：{result.detection.form}（置信度 {result.detection.confidence}）")
    typer.echo(f"  目录：{result.app_dir}")

    # DEV-067 / BUG-112：容器源码已换 -> rebuild 镜像；静态/前端 -> restart。
    if result.needs_rebuild:
        from local_webpage_access.lifecycle import rebuild_instance

        typer.secho(
            "  正在 rebuild（容器源码已更新，须重建镜像）…",
            fg=typer.colors.CYAN,
        )
        rebuild_instance(ws, config, reg, instance_id)
        typer.secho("  已 rebuild，端口不变", fg=typer.colors.GREEN)
    elif result.needs_restart:
        from local_webpage_access.lifecycle import restart_instance

        typer.secho("  正在 restart…", fg=typer.colors.CYAN)
        restart_instance(ws, config, reg, instance_id)
        typer.secho("  已 restart，端口不变", fg=typer.colors.GREEN)


def _do_update_from_dir(
    importer,
    ws,
    config,
    reg,
    *,
    instance_id: str,
    from_dir: str | None,
    restart: bool,
    keep_data: bool,
    yes: bool,
    dry_run: bool,
    force_kind_change: bool,
) -> None:
    """IMP-047：``lwa import --from-dir --update <id>`` 的编排。

    ``from_dir`` 是用户在命令行传入的目录；若提供则须与 manifest 中记录的
    ``sourceDirPath`` 一致，否则拒绝（防止更新时误传另一个目录）。
    不传时使用 manifest 中的关联目录（向后兼容）。
    """
    if from_dir is not None:
        from local_webpage_access.models import InstanceManifest

        manifest_path = ws.app_manifest_path(instance_id)
        if manifest_path.is_file():
            manifest = InstanceManifest.load(manifest_path)
            recorded = getattr(manifest, "sourceDirPath", None)
            if recorded and str(Path(from_dir).resolve()) != str(Path(recorded).resolve()):
                typer.secho(
                    f"传入的目录 {from_dir} 与实例 {instance_id} 关联的源目录 {recorded} 不一致。\n"
                    "如需更换关联目录，请先删除实例再用新目录重新导入。",
                    fg=typer.colors.RED,
                    err=True,
                )
                raise typer.Exit(code=2)

    result = importer.update_from_dir(
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
            f"实例 {instance_id} 的文件夹源内容未变化（指纹 {new_short}），已跳过更新。",
            fg=typer.colors.YELLOW,
        )
        return

    if result.dry_run:
        typer.secho(
            f"[dry-run] 实例 {instance_id}：文件夹源指纹 {prev_short} -> {new_short}",
            fg=typer.colors.CYAN,
        )
        if result.detection is not None:
            typer.echo(f"  新形态：{result.detection.form}")
        if result.kind_changed:
            typer.secho(
                "  ⚠ 形态将变化，需 --force-kind-change 才能实际更新",
                fg=typer.colors.YELLOW,
            )
        return

    typer.secho(f"已从文件夹源更新实例：{instance_id}", fg=typer.colors.GREEN)
    typer.echo(f"  指纹：{prev_short} -> {new_short}")
    if result.detection is not None:
        typer.echo(f"  形态：{result.detection.form}（置信度 {result.detection.confidence}）")
    typer.echo(f"  目录：{result.app_dir}")

    # DEV-067 / BUG-112：容器源码已换 -> rebuild 镜像；静态/前端 -> restart。
    if result.needs_rebuild:
        from local_webpage_access.lifecycle import rebuild_instance

        typer.secho(
            "  正在 rebuild（容器源码已更新，须重建镜像）…",
            fg=typer.colors.CYAN,
        )
        rebuild_instance(ws, config, reg, instance_id)
        typer.secho("  已 rebuild，端口不变", fg=typer.colors.GREEN)
    elif result.needs_restart:
        from local_webpage_access.lifecycle import restart_instance

        typer.secho("  正在 restart…", fg=typer.colors.CYAN)
        restart_instance(ws, config, reg, instance_id)
        typer.secho("  已 restart，端口不变", fg=typer.colors.GREEN)


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
            if result.needs_rebuild:
                action = "rebuild"
            elif result.needs_restart:
                action = "restart"
            else:
                # --no-restart 或非 running 路径：仅报告原状态
                action = None
            if action:
                typer.echo(f"  原状态：running（实际更新后将 {action}）")
            else:
                typer.echo("  原状态：running（--no-restart，不会自动 rebuild/restart）")
        if result.sanitized and result.sanitized.stripped_names:
            typer.echo(f"  将剥离冗余成员 {len(result.sanitized.stripped_names)} 项")
        return

    typer.secho(f"已更新实例：{instance_id}", fg=typer.colors.GREEN)
    typer.echo(f"  sha256：{prev_short} → {new_short}")
    if result.detection is not None:
        typer.echo(f"  形态：{result.detection.form}（置信度 {result.detection.confidence}）")
    typer.echo(f"  目录：{result.app_dir}")
    if result.sanitized and result.sanitized.stripped_names:
        san = result.sanitized
        parts = ", ".join(
            f"{rule}×{n}" for rule, n in sorted(san.categories.items(), key=lambda kv: -kv[1])
        )
        typer.secho(
            f"  已剥离冗余成员 {len(san.stripped_names)} 项"
            f"（含 symlink {san.stripped_symlink_count}）：{parts}",
            fg=typer.colors.CYAN,
        )

    # DEV-067 / BUG-112：容器源码已换 → rebuild 镜像；静态/前端 → restart。
    if result.needs_rebuild:
        from local_webpage_access.lifecycle import rebuild_instance

        typer.secho(
            "  正在 rebuild（容器源码已更新，须重建镜像）…",
            fg=typer.colors.CYAN,
        )
        rebuild_instance(ws, config, reg, instance_id)
        typer.secho("  已 rebuild，端口不变", fg=typer.colors.GREEN)
    elif result.needs_restart:
        from local_webpage_access.lifecycle import restart_instance

        typer.secho("  正在 restart…", fg=typer.colors.CYAN)
        restart_instance(ws, config, reg, instance_id)
        typer.secho("  已 restart，端口不变", fg=typer.colors.GREEN)
    elif not result.skipped and result.manifest.runtime.value == "docker-compose" and not restart:
        typer.secho(
            "  提示：容器源码已更新但未重建镜像（--no-restart）。"
            "运行中的仍是旧镜像；就绪后请执行："
            f" lwa rebuild {instance_id}",
            fg=typer.colors.YELLOW,
        )


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
                from local_webpage_access.lifecycle import instance_lock

                with instance_lock(ws, iid):
                    current_dir = ws.app_current(iid)
                    detection = scanner.detect(current_dir)
                    manifest_path = ws.app_manifest_path(iid)
                    if not manifest_path.is_file():
                        typer.secho(
                            f"  {iid}：缺少 local-web.json，跳过",
                            fg=typer.colors.YELLOW,
                        )
                        continue
                    manifest = InstanceManifest.load(manifest_path)
                    manifest = apply_detection_to_manifest(manifest, detection, ws)
                    manifest.save(manifest_path)
                    reg.upsert_from_manifest(manifest)
                    reg.add_event(
                        iid,
                        "scan",
                        f"重新扫描：{detection.form}（{detection.confidence}）",
                    )
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
