"""路径别名子命令（IMP-006）：``lwa alias set/clear``。

DEV-044（WBS-20260708 阶段5.1）：从原 ``cli.py`` 拆出。暴露 ``app`` 供根
CLI 通过 ``add_typer`` 挂载为 ``lwa alias ...`` 子命令组。
"""

from __future__ import annotations

import typer

from local_webpage_access.cli._common import log, open_workspace_registry
from local_webpage_access.errors import LwaError

app = typer.Typer(help="管理实例路径别名（IMP-006）")


@app.command("set")
def alias_set(
    instance_id: str = typer.Argument(..., help="实例 ID"),
    slug: str = typer.Argument(..., help="路径别名 slug"),
) -> None:
    """为静态实例设置路径别名。"""
    from local_webpage_access.path_alias import set_instance_path_alias

    try:
        ws, config, reg = open_workspace_registry()
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
        # IMP-023 / IMP-055：设别名时已对入口 HTML 绝对路径资源硬拦截。
        # html_verified=False 表示探不到入口（实例未监听 / HTML 拉不到），守卫跳过。
        if not result.html_verified:
            typer.secho(
                f"  提示：未验证入口 HTML（实例可能未监听或 HTML 拉不到），守卫已跳过。"
                f"若 SPA 使用绝对 base（/assets/…），别名下仍会白屏。"
                f"请确认构建时设 --base=/{slug}/（Vite）或等价配置后重新设置别名。",
                fg=typer.colors.CYAN,
            )
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command("clear")
def alias_clear(
    instance_id: str = typer.Argument(..., help="实例 ID"),
) -> None:
    """清除静态实例的路径别名。"""
    from local_webpage_access.path_alias import set_instance_path_alias

    try:
        ws, config, reg = open_workspace_registry()
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
