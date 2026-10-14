"""S3-01 版本留档命令：``lwa versions`` / ``lwa rollback``（DEV-157 / issue #51 远期 / D8）。

* ``lwa versions <id>``：列出构建号、时间、源码哈希、镜像是否仍在、构建结果与保留占用；
* ``lwa rollback <id> [--to N] [--yes]``：回滚到指定（默认上一个可用）留档版本，
  随后跑实证验证与别名守卫（退出码语义同 rebuild：内容级别名失败为 4）。

D8 范围声明：只回滚镜像与生成配置，**不回滚 data/**；启动/构建命令含迁移
时必须 ``--yes``，CLI 会额外醒目提示数据库结构可能不兼容。
"""

from __future__ import annotations

from typing import Optional

import typer

from local_webpage_access.cli._common import fmt_bytes, log, open_workspace_registry
from local_webpage_access.errors import LwaError


def versions(instance_id: str = typer.Argument(..., help="实例 ID")) -> None:
    """列出实例的留档版本（构建号 / 时间 / 源码哈希 / 镜像是否仍在 / 构建结果）。"""
    from local_webpage_access import versions as versions_mod

    try:
        ws, config, reg = open_workspace_registry()
        try:
            row = reg.get_instance(instance_id)
            if row is None:
                typer.secho(f"实例不存在：{instance_id}", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=1)
            if row.get("runtime") != "docker-compose":
                typer.secho(
                    f"实例 {instance_id} 不是容器实例（runtime={row.get('runtime')}），无镜像版本留档",
                    fg=typer.colors.RED,
                    err=True,
                )
                raise typer.Exit(code=1)
            rows = versions_mod.list_versions(reg, instance_id)
            total, usage = versions_mod.retained_disk_usage_bytes(reg, instance_id)
        finally:
            reg.close()

        if not rows:
            typer.echo("（暂无留档版本——成功部署容器实例后自动产生）")
            return
        typer.echo(
            f"{'BUILD':8} {'时间':20} {'源码哈希':12} {'镜像':6} {'结果':10} 镜像引用"
        )
        for r in rows:
            when = (r["finishedAt"] or r["startedAt"] or "")[:19]
            present = "在" if r["imagePresent"] else "已清理"
            result = r["status"] or "?"
            if r["errorSummary"]:
                result += f"({r['errorSummary'][:24]})"
            typer.echo(
                f"#{r['id']:<7} {when:20} {(r['sourceHash'] or '-')[:12]:12} "
                f"{present:6} {result:10} {r['imageRef']}"
            )
        typer.echo(
            f"保留版本：{total} 个（imageRetention={config.imageRetention}），"
            f"占用 {fmt_bytes(usage)}；`lwa rollback {instance_id} --to <BUILD>` 可回滚"
        )
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


def rollback(
    instance_id: str = typer.Argument(..., help="实例 ID"),
    to: Optional[int] = typer.Option(
        None, "--to", help="回滚到指定构建号（默认：上一个可用版本）"
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="确认回滚（D8：启动/构建命令含迁移时必传；不回滚 data/）",
    ),
) -> None:
    """把容器实例回滚到留档版本（只回滚镜像与生成配置，不回滚 data/）。

    回滚后用快照生成物与对应镜像执行 up，随后实证验证并跑别名守卫；验证失败
    时自动恢复原版本。回滚是一次生命周期操作：revision +1，可用
    ``lwa versions <id>`` 查看可回滚版本。
    """
    from local_webpage_access import rollback as rollback_mod
    from local_webpage_access.cli.lifecycle import _print_alias_guard_summary

    try:
        ws, config, reg = open_workspace_registry()
        try:
            manifest = rollback_mod.rollback_instance(
                ws, config, reg, instance_id, to_build_id=to, yes=yes
            )
        finally:
            reg.close()
        typer.secho(
            f"已回滚实例：{instance_id}（构建 #{manifest.lastOperation.get('toBuildId')}）",
            fg=typer.colors.GREEN,
        )
        typer.echo("  范围：只回滚镜像与生成配置，data/ 未回滚（D8）")
        if manifest.network.hostPort:
            typer.echo(f"  端口：{manifest.network.hostPort}")
        guard_code = _print_alias_guard_summary(manifest)
        if guard_code:
            raise typer.Exit(code=guard_code)
    except rollback_mod.RollbackNeedsYes as exc:
        # D8：启动/构建命令含迁移——醒目提示数据库结构可能不兼容
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        typer.secho(
            "⚠ 当前配置或目标版本留档的启动命令含数据库迁移命令：回滚不回滚 data/，"
            "启动旧镜像可能执行迁移，且无法随镜像补偿撤销。确认无误后请加 --yes 重试。",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(code=1) from exc
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


def register(app: typer.Typer) -> None:
    """把本模块命令注册到根 app（保持顶层命令名不变）。"""
    app.command()(versions)
    app.command()(rollback)
