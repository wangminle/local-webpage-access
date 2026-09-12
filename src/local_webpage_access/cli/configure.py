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
                        for k in ("buildEnv", "buildBaseFromAlias", "redundancyAcknowledged")
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
