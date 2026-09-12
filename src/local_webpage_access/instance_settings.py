"""实例显式配置的持锁更新及构建环境解析。"""

from typing import Any

from local_webpage_access.errors import LwaError
from local_webpage_access.lifecycle import instance_lock
from local_webpage_access.models import InstanceManifest, Runtime
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry


def effective_build_env(manifest: InstanceManifest) -> dict[str, str]:
    from local_webpage_access.path_alias import _current_alias

    result = dict(manifest.buildEnv or {})
    if manifest.buildBaseFromAlias:
        alias = _current_alias(manifest)
        result["VITE_BASE"] = f"/{alias}/" if alias else "/"
    return result


def update_instance_settings(
    workspace: Workspace, registry: Registry, instance_id: str, changes: dict[str, Any]
) -> InstanceManifest:
    allowed = {"buildEnv", "buildBaseFromAlias", "redundancyAcknowledged"}
    if not changes or set(changes) - allowed:
        raise ValueError("仅允许设置 buildEnv、buildBaseFromAlias、redundancyAcknowledged")
    for key in ("buildBaseFromAlias", "redundancyAcknowledged"):
        if key in changes and not isinstance(changes[key], bool):
            raise ValueError(f"{key} 必须是布尔值")
    with instance_lock(workspace, instance_id):
        path = workspace.app_manifest_path(instance_id)
        if not registry.instance_exists(instance_id) or not path.is_file():
            raise LwaError(f"实例 {instance_id} 不存在", instance_id=instance_id)
        old = InstanceManifest.load(path)
        # model_copy(update=...) 不执行校验，须完整 validate 后才允许落盘。
        updated = InstanceManifest.model_validate({**old.model_dump(), **changes})
        if set(changes) & {"buildEnv", "buildBaseFromAlias"}:
            if (
                updated.buildEnv or updated.buildBaseFromAlias
            ) and old.runtime != Runtime.SHARED_STATIC:
                raise ValueError("buildEnv / 别名 base 跟随仅支持宿主前端构建；容器构建不支持")
        updated.touch()
        updated.save(path)
        registry.add_event(instance_id, "config", "更新实例配置：" + "、".join(sorted(changes)))
        return updated
