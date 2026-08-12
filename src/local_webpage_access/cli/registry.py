"""``lwa registry`` 子命令组：registry 数据维护（BUG-473）。

暴露 ``app`` 供根 CLI 通过 ``add_typer`` 挂载为 ``lwa registry ...`` 子命令组。

* ``lwa registry check``  —— 只读扫描孤儿子表行（子表有行、instances 主表无对应）
* ``lwa registry repair`` —— 交互确认后删除孤儿行（``--yes`` 跳过确认；非 TTY 须 ``--yes``）

背景：历史版本删除实例时外键级联未生效，残留的孤儿子表行会占用路径别名却
``lwa list`` 查不到，挡住同名重新导入（BUG-473）。
"""

from __future__ import annotations

import sys

import typer

from local_webpage_access.cli._common import log, open_workspace_registry
from local_webpage_access.errors import LwaError

app = typer.Typer(no_args_is_help=True, help="registry 数据维护（孤儿扫描/清理）")


def _summarize(orphans: list[dict]) -> dict[str, list[str]]:
    """按表聚合孤儿 instance_id，便于展示。"""
    by_table: dict[str, list[str]] = {}
    for row in orphans:
        by_table.setdefault(row["table"], []).append(row["instance_id"])
    return by_table


@app.command("check")
def check_cmd(
    json_output: bool = typer.Option(False, "--json", help="输出 JSON 报告"),
) -> None:
    """扫描 registry 子表中的孤儿行（只读，无破坏性）。"""
    import json as json_mod

    try:
        _ws, _config, reg = open_workspace_registry()
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    try:
        orphans = reg.find_orphan_rows()
    finally:
        reg.close()

    if json_output:
        typer.echo(
            json_mod.dumps(
                {"count": len(orphans), "orphans": orphans},
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if not orphans:
        typer.secho("✓ registry 无孤儿数据", fg=typer.colors.GREEN)
        return
    typer.secho(
        f"发现 {len(orphans)} 条孤儿数据（子表有行、instances 主表无对应）：",
        fg=typer.colors.YELLOW,
    )
    for table, ids in _summarize(orphans).items():
        typer.echo(f"  {table}（{len(ids)}）：{', '.join(ids)}")
    typer.echo("清理：lwa registry repair（需确认）")


@app.command("repair")
def repair_cmd(
    yes: bool = typer.Option(
        False, "--yes", "-y", help="跳过确认直接清理（非 TTY 时必须）"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出 JSON 摘要"),
) -> None:
    """删除 registry 子表中的孤儿行（破坏性，默认交互确认）。"""
    import json as json_mod

    try:
        _ws, _config, reg = open_workspace_registry()
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    try:
        orphans = reg.find_orphan_rows()
        if not orphans:
            if json_output:
                typer.echo(json_mod.dumps({"deleted": 0}, ensure_ascii=False))
            else:
                typer.secho("✓ 无孤儿数据，无需清理", fg=typer.colors.GREEN)
            return

        if not yes:
            if not sys.stdin.isatty():
                typer.secho(
                    "非交互环境检测到孤儿数据，请加 --yes 确认后清理",
                    fg=typer.colors.RED,
                    err=True,
                )
                raise typer.Exit(code=1)
            typer.secho(
                f"将清理 {len(orphans)} 条孤儿数据：",
                fg=typer.colors.YELLOW,
            )
            for table, ids in _summarize(orphans).items():
                typer.echo(f"  {table}（{len(ids)}）：{', '.join(ids)}")
            typer.confirm("确认清理这些孤儿行？", default=False, abort=True)

        deleted = reg.purge_orphan_rows()
    finally:
        reg.close()

    if json_output:
        typer.echo(json_mod.dumps({"deleted": deleted}, ensure_ascii=False))
    else:
        typer.secho(f"✓ 已清理 {deleted} 条孤儿数据", fg=typer.colors.GREEN)
