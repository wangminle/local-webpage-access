"""验证探针子命令（CHK-252 第三批）：``lwa probe show/set/reset``。"""

from __future__ import annotations

import json

import typer

from local_webpage_access.cli._common import log, open_workspace_registry
from local_webpage_access.errors import LwaError
from local_webpage_access.lifecycle import instance_lock
from local_webpage_access.models import InstanceManifest
from local_webpage_access.verification_config import (
    get_verification_overrides,
    overrides_to_dict,
    set_verification_overrides,
)

app = typer.Typer(help="管理实例验证探针覆盖层（verificationOverrides）")


def _load_manifest(workspace, instance_id: str) -> InstanceManifest:
    mpath = workspace.app_manifest_path(instance_id)
    if not mpath.is_file():
        raise LwaError(f"实例 {instance_id} 不存在", instance_id=instance_id)
    return InstanceManifest.load(mpath)


@app.command("show")
def probe_show(
    instance_id: str = typer.Argument(..., help="实例 ID"),
) -> None:
    """显示继承探针、用户覆盖与合并后的有效探针。"""
    try:
        ws, _config, _reg = open_workspace_registry()
        manifest = _load_manifest(ws, instance_id)
        data = overrides_to_dict(manifest)
        typer.echo(json.dumps(data, ensure_ascii=False, indent=2))
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command("set")
def probe_set(
    instance_id: str = typer.Argument(..., help="实例 ID"),
    path: str = typer.Argument(..., help="探针路径，如 /health"),
    expected_status: int = typer.Option(200, "--expected-status", help="期望 HTTP 状态码"),
    description: str = typer.Option("", "--description", help="人类可读说明"),
    disable_auto: bool = typer.Option(
        False,
        "--disable-auto",
        help="关闭契约中的 guessed/discovered 自动探针",
    ),
) -> None:
    """添加或更新用户显式就绪探针（mandatory 门槛）。"""
    try:
        ws, _config, reg = open_workspace_registry()
        try:
            # BUG-167 纪律：manifest 读-改-写须持实例锁，防止与 import/
            # rebuild 并发时互相覆盖对方的写入。
            with instance_lock(ws, instance_id):
                manifest = _load_manifest(ws, instance_id)
                overrides = get_verification_overrides(manifest)
                probes = list(overrides.get("probes") or [])
                normalized_path = path if path.startswith("/") else f"/{path}"
                updated = False
                for item in probes:
                    if isinstance(item, dict) and str(item.get("path")) == normalized_path:
                        item["expectedStatus"] = expected_status
                        item["description"] = description or item.get("description") or "用户显式配置探针"
                        updated = True
                        break
                if not updated:
                    probes.append(
                        {
                            "path": normalized_path,
                            "method": "GET",
                            "expectedStatus": expected_status,
                            "description": description or "用户显式配置探针",
                        }
                    )
                overrides["probes"] = probes
                if disable_auto:
                    overrides["disableAutoProbes"] = True
                set_verification_overrides(manifest, overrides)
                manifest.save(ws.app_manifest_path(instance_id))
            reg.add_event(instance_id, "probe", f"设置就绪探针 {normalized_path}（期望 {expected_status}）")
        finally:
            reg.close()
        typer.secho(f"已设置实例 {instance_id} 探针 {normalized_path}", fg=typer.colors.GREEN)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command("reset")
def probe_reset(
    instance_id: str = typer.Argument(..., help="实例 ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
) -> None:
    """清除实例的 verificationOverrides（恢复仅使用契约探针）。"""
    try:
        if not yes and not typer.confirm(f"确认清除实例 {instance_id} 的探针覆盖层？"):
            raise typer.Exit(code=0)
        ws, _config, reg = open_workspace_registry()
        try:
            with instance_lock(ws, instance_id):
                manifest = _load_manifest(ws, instance_id)
                set_verification_overrides(manifest, None)
                manifest.save(ws.app_manifest_path(instance_id))
            reg.add_event(instance_id, "probe", "已清除 verificationOverrides")
        finally:
            reg.close()
        typer.secho(f"已清除实例 {instance_id} 的探针覆盖层", fg=typer.colors.GREEN)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command("auto")
def probe_auto(
    instance_id: str = typer.Argument(..., help="实例 ID"),
    state: str = typer.Argument(..., help="on 开启 / off 关闭自动探针（guessed/discovered）"),
) -> None:
    """开关契约自动探针（关闭后仅执行用户显式声明的探针）。"""
    normalized = state.strip().lower()
    if normalized not in ("on", "off"):
        typer.secho("state 仅支持 on / off", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    disable = normalized == "off"
    try:
        ws, _config, reg = open_workspace_registry()
        try:
            with instance_lock(ws, instance_id):
                manifest = _load_manifest(ws, instance_id)
                overrides = get_verification_overrides(manifest)
                overrides["disableAutoProbes"] = disable
                set_verification_overrides(manifest, overrides)
                manifest.save(ws.app_manifest_path(instance_id))
            reg.add_event(
                instance_id,
                "probe",
                f"自动探针已{'关闭' if disable else '开启'}（disableAutoProbes={disable}）",
            )
        finally:
            reg.close()
        typer.secho(
            f"实例 {instance_id} 自动探针已{'关闭（仅执行用户显式探针）' if disable else '开启'}",
            fg=typer.colors.GREEN,
        )
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
