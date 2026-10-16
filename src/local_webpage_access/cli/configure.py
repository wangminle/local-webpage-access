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
        None, "--build-env", help="替换构建环境映射，可重复 KEY=VALUE；宿主前端命令环境与容器 Docker ARG/ENV"
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
        help="替换系统依赖包列表（可重复）。Python(Debian) 走 apt 切源链；Node(Alpine) 以 apk 安装在依赖层之前——原生编译工具（node-gyp 需 python3/make/g++）用这里，勿用 buildHooks（其在依赖安装后执行）",
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
    extra_volume: list[str] | None = typer.Option(
        None,
        "--extra-volume",
        help="替换额外 bind 挂载（可重复）SRC:DST[:ro]；整组替换，改后 lwa restart 生效",
    ),
    clear_extra_volumes: bool = typer.Option(
        False, "--clear-extra-volumes", help="清空额外 bind 挂载"
    ),
    kind: str | None = typer.Option(
        None,
        "--kind",
        help="BUG-869：人工确认 pending 实例的 kind（node / python / static），"
        "须与 --runtime 同用；执行后 manifest 与 registry 同步，"
        "且不要与其他配置项组合",
    ),
    runtime: str | None = typer.Option(
        None,
        "--runtime",
        help="BUG-869：人工确认 pending 实例的 runtime（docker-compose / "
        "shared-static），须与 --kind 同用",
    ),
) -> None:
    """配置实例；不带选项时显示当前配置。构建参数改后需 rebuild，挂载改后 restart 即可。"""
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
        if extra_volume and clear_extra_volumes:
            raise ValueError("--extra-volume 与 --clear-extra-volumes 互斥")
        if extra_volume:
            changes["extraVolumes"] = list(extra_volume)
        if clear_extra_volumes:
            changes["extraVolumes"] = []
        # BUG-869：pending 实例的人工形态确认通道。
        if (kind is None) != (runtime is None):
            raise ValueError("--kind 与 --runtime 须同时使用")
        if kind is not None and changes:
            raise ValueError("确认形态（--kind/--runtime）请单独使用，勿与其他配置项组合")
        kind_enum = runtime_enum = None
        if kind is not None:
            from local_webpage_access.models import Kind, Runtime

            try:
                kind_enum = Kind(kind)
            except ValueError:
                raise ValueError(f"--kind 取值须为 node / python / static：{kind}") from None
            try:
                runtime_enum = Runtime(runtime)
            except ValueError:
                raise ValueError(
                    f"--runtime 取值须为 docker-compose / shared-static：{runtime}"
                ) from None
        ws, _, reg = open_workspace_registry()
        try:
            if kind_enum is not None:
                assert runtime_enum is not None  # 成对校验已保证
                from local_webpage_access.instance_settings import confirm_instance_shape

                manifest = confirm_instance_shape(
                    ws, reg, instance_id, kind=kind_enum, runtime=runtime_enum
                )
                typer.secho(
                    f"已确认实例 {instance_id} 形态：{kind_enum.value}/{runtime_enum.value}"
                    "（manifest 与 registry 已同步，状态 pending → stopped）",
                    fg=typer.colors.GREEN,
                )
                typer.secho(
                    "建议下一步：按需 lwa configure <id> --system-deps <包> / 核对 entry 后"
                    f"执行 lwa start {instance_id}",
                    fg=typer.colors.CYAN,
                )
            elif changes:
                manifest = update_instance_settings(ws, reg, instance_id, changes)
            else:
                manifest = InstanceManifest.load(ws.app_manifest_path(instance_id))
            extra_volumes = manifest.container.extraVolumes if manifest.container else None
            typer.echo(
                json.dumps(
                    {
                        **{
                            k: getattr(manifest, k)
                            for k in (
                                "buildEnv",
                                "buildBaseFromAlias",
                                "redundancyAcknowledged",
                                "systemDeps",
                                "buildHooks",
                            )
                        },
                        "extraVolumes": extra_volumes,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            if changes:
                if set(changes) - {"extraVolumes"}:
                    typer.echo("配置已保存；构建参数在下次 rebuild 时生效。")
                if "extraVolumes" in changes:
                    typer.echo("挂载配置已保存；执行 lwa restart 生效（无需 rebuild）。")
        finally:
            reg.close()
    except (ValueError, OSError, LwaError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc


def register(app: typer.Typer) -> None:
    app.command("configure")(configure)
