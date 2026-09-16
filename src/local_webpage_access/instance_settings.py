"""实例显式配置的持锁更新及构建环境解析。"""

from typing import Any

from local_webpage_access.errors import LwaError
from local_webpage_access.lifecycle import instance_lock
from local_webpage_access.models import InstanceManifest, Kind, Runtime
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry

# BUG-680：与 compose / 容器运行管理键隔离，禁止经 buildEnv 改写。
_BUILD_ENV_RESERVED_KEYS = frozenset(
    {
        "HOST_PORT",
        "INTERNAL_PORT",
        "MEMORY_LIMIT",
        "CPU_LIMIT",
        "DATABASE_URL",
        "PORT",
    }
)


def effective_build_env(manifest: InstanceManifest) -> dict[str, str]:
    from local_webpage_access.path_alias import _current_alias

    result = dict(manifest.buildEnv or {})
    if manifest.buildBaseFromAlias:
        alias = _current_alias(manifest)
        result["VITE_BASE"] = f"/{alias}/" if alias else "/"
    for reserved in _BUILD_ENV_RESERVED_KEYS:
        result.pop(reserved, None)
    return result


def update_instance_settings(
    workspace: Workspace, registry: Registry, instance_id: str, changes: dict[str, Any]
) -> InstanceManifest:
    allowed = {
        "buildEnv",
        "buildBaseFromAlias",
        "redundancyAcknowledged",
        "systemDeps",
        "buildHooks",
    }
    if not changes or set(changes) - allowed:
        raise ValueError(
            "仅允许设置 buildEnv、buildBaseFromAlias、redundancyAcknowledged、"
            "systemDeps、buildHooks"
        )
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
            ) and old.runtime not in (Runtime.SHARED_STATIC, Runtime.DOCKER_COMPOSE):
                raise ValueError(
                    "buildEnv / 别名 base 跟随仅支持宿主前端构建与 docker-compose 容器构建"
                )
            reserved = sorted(
                key for key in (updated.buildEnv or {}) if key in _BUILD_ENV_RESERVED_KEYS
            )
            if reserved:
                raise ValueError(
                    "buildEnv 不得覆盖 LWA 运行管理参数：" + "、".join(reserved)
                )
        if "systemDeps" in changes and old.runtime != Runtime.DOCKER_COMPOSE:
            raise ValueError("systemDeps 仅支持容器实例")
        if "systemDeps" in changes and (changes.get("systemDeps") or []) and old.kind != Kind.PYTHON:
            raise ValueError(
                "systemDeps 使用 apt，仅支持 Debian 系 Python 容器；"
                "Node 镜像为 Alpine，请改用 buildHooks"
            )
        updated.touch()
        updated.save(path)
        registry.add_event(instance_id, "config", "更新实例配置：" + "、".join(sorted(changes)))
        return updated
