"""实例构建环境与重复实例保留配置入口。"""

import json
from typing import Any
import typer
from local_webpage_access.cli._common import open_workspace_registry
from local_webpage_access.errors import LwaError
from local_webpage_access.instance_settings import update_instance_settings
from local_webpage_access.models import InstanceManifest


def configure(
    instance_id: str = typer.Argument(..., help="实例 ID"),
    build_env: list[str] | None = typer.Option(
        None, "--build-env", help="替换构建环境映射，可重复 KEY=VALUE；仅宿主前端构建"
    ),
    clear_build_env: bool = typer.Option(False, "--clear-build-env", help="清空构建环境映射"),
    follow_alias_base: bool | None = typer.Option(
        None,
        "--follow-alias-base/--no-follow-alias-base",
        help="构建时 VITE_BASE 跟随当前别名（项目须读取此变量）；清除别名后为 /",
    ),
    acknowledge_redundancy: bool | None = typer.Option(
        None,
        "--acknowledge-redundancy/--no-acknowledge-redundancy",
        help="将重复实例标记为有意保留，退出冗余候选；可撤销",
    ),
    system_deps: list[str] | None = typer.Option(
        None,
        "--system-deps",
        help="替换系统依赖包列表（可重复）；仅 Debian 系 Python 容器走 apt 切源链，Node/Alpine 请用 --build-hook",
    ),
    clear_system_deps: bool = typer.Option(
        False, "--clear-system-deps", help="清空 systemDeps"
    ),
    build_hook: list[str] | None = typer.Option(
        None,
        "--build-hook",
        help="替换构建钩子（可重复）；系统包请优先用 --system-deps",
    ),
    clear_build_hooks: bool = typer.Option(
        False, "--clear-build-hooks", help="清空 buildHooks"
    ),
) -> None:
    """配置实例；不带选项时显示当前配置，修改后需 rebuild 生效。"""
    try:
        changes: dict[str, Any] = {}
        if build_env and clear_build_env:
            raise ValueError("--build-env 与 --clear-build-env 互斥")
        if build_env:
            values = {}
            for item in build_env:
                key, sep, value = item.partition("=")
                if not sep:
                    raise ValueError("--build-env 必须为 KEY=VALUE")
                values[key] = value
            changes["buildEnv"] = values
        if clear_build_env:
            changes["buildEnv"] = None
        if follow_alias_base is not None:
            changes["buildBaseFromAlias"] = follow_alias_base
        if acknowledge_redundancy is not None:
            changes["redundancyAcknowledged"] = acknowledge_redundancy
        if system_deps and clear_system_deps:
            raise ValueError("--system-deps 与 --clear-system-deps 互斥")
        if system_deps:
            changes["systemDeps"] = system_deps
        if clear_system_deps:
            changes["systemDeps"] = []
        if build_hook and clear_build_hooks:
            raise ValueError("--build-hook 与 --clear-build-hooks 互斥")
        if build_hook:
            changes["buildHooks"] = build_hook
        if clear_build_hooks:
            changes["buildHooks"] = []
        ws, _, reg = open_workspace_registry()
        try:
            if changes:
                manifest = update_instance_settings(ws, reg, instance_id, changes)
            else:
                manifest = InstanceManifest.load(ws.app_manifest_path(instance_id))
            typer.echo(
                json.dumps(
                    {
                        k: getattr(manifest, k)
                        for k in (
                            "buildEnv",
                            "buildBaseFromAlias",
                            "redundancyAcknowledged",
                            "systemDeps",
                            "buildHooks",
                        )
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            if changes:
                typer.echo("配置已保存；构建参数在下次 rebuild 时生效。")
        finally:
            reg.close()
    except (ValueError, OSError, LwaError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc


def register(app: typer.Typer) -> None:
    app.command("configure")(configure)
