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
        # IMP-023（WBS-20260708 阶段4.2）：SPA 子路径资源加载提示。
        # 路径别名 reverse_proxy 去 /<alias>/ 前缀转发，相对路径资源（./assets/…）正常；
        # 但绝对路径资源（/assets/…，Vue/React 默认 base: '/'）会绕过别名打到入口根 → 404。
        # 提示构建时用相对 base 或显式 --base=/<alias>/。纯静态 HTML（相对路径）不受影响。
        typer.secho(
            f"  SPA 提示：Vue/React 等若用绝对资源路径（/assets/…），/{slug}/ 下会 404；"
            f"构建时设 base 为相对路径（Vite: base: './'）或 --base=/{slug}/，"
            f"相对路径资源不受影响。",
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
