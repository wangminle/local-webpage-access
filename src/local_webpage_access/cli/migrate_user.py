"""容器运行身份迁移命令（issue #20）：``lwa migrate-user <id>``。

对**通过写权限检查**的旧实例提供显式迁移出口：把
``container.runAsNonRoot`` 置 True 并提示 rebuild 生效。不做一次性
强制切换——data/ 属主为 root 或属主不可写的实例会被拒绝并给出 chown
指引；确需 root 兼容的实例可 ``--root`` 显式选择 False。
"""

from __future__ import annotations

import typer

from local_webpage_access.cli._common import log, open_workspace_registry
from local_webpage_access.errors import LwaError
from local_webpage_access.lifecycle import instance_lock
from local_webpage_access.models import ContainerConfig, InstanceManifest


def _load_manifest(workspace, instance_id: str) -> tuple[InstanceManifest, ContainerConfig]:
    mpath = workspace.app_manifest_path(instance_id)
    if not mpath.is_file():
        raise LwaError(f"实例 {instance_id} 不存在", instance_id=instance_id)
    manifest = InstanceManifest.load(mpath)
    if manifest.runtime.value != "docker-compose" or manifest.container is None:
        raise LwaError(
            f"实例 {instance_id} 不是容器实例（runtime={manifest.runtime.value}）",
            instance_id=instance_id,
        )
    return manifest, manifest.container


def migrate_user(
    instance_id: str = typer.Argument(..., help="实例 ID"),
    root: bool = typer.Option(
        False,
        "--root",
        help="反向操作：显式选择 root 兼容（runAsNonRoot=false），跳过预检",
    ),
) -> None:
    """把旧实例迁移到非 root 运行（预检通过后才写 manifest）。"""
    try:
        ws, _config, reg = open_workspace_registry()
        try:
            identity = None
            with instance_lock(ws, instance_id):
                manifest, container = _load_manifest(ws, instance_id)
                if root:
                    container.runAsNonRoot = False
                    manifest.save(ws.app_manifest_path(instance_id))
                    reg.add_event(
                        instance_id,
                        "security",
                        "runAsNonRoot=false（用户显式选择 root 兼容）",
                    )
                    typer.secho(
                        f"实例 {instance_id} 已显式选择 root 运行"
                        f"（runAsNonRoot=false）；rebuild 后生效。",
                        fg=typer.colors.YELLOW,
                    )
                    return

                # 预检通过才落盘：临时置 True 走 ensure_non_root_identity_ready
                # 的完整检查（属主非 root + 属主可写），失败抛 ConfigError 不落盘。
                from local_webpage_access.container_identity import (
                    ensure_non_root_identity_ready,
                )

                container.runAsNonRoot = True
                identity = ensure_non_root_identity_ready(manifest, ws)
                if identity is None:  # pragma: no cover - 置 True 后必非 None
                    raise LwaError(
                        f"实例 {instance_id} 身份解析异常（runAsNonRoot=True 仍返回 None）",
                        instance_id=instance_id,
                    )
                manifest.save(ws.app_manifest_path(instance_id))
                reg.add_event(
                    instance_id,
                    "security",
                    f"runAsNonRoot=true 迁移完成（容器身份 {identity.docker_user()}）",
                )
            typer.secho(
                f"实例 {instance_id} 已迁移到非 root 运行"
                f"（容器身份 {identity.docker_user()}，对齐宿主 data/ 属主）。\n"
                f"执行 lwa rebuild {instance_id}（或重启实例）后生效。",
                fg=typer.colors.GREEN,
            )
        finally:
            reg.close()
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


def register(app: typer.Typer) -> None:
    """注册顶层命令 ``lwa migrate-user``。"""
    app.command("migrate-user")(migrate_user)
