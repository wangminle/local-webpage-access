"""实例显式配置的持锁更新及构建环境解析。"""

from typing import Any

from local_webpage_access.errors import LwaError
from local_webpage_access.lifecycle import instance_lock
from local_webpage_access.models import InstanceManifest, Kind, Runtime, ServingMode, Status
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


def validate_extra_volumes(instance_id: str, entries: list[str]) -> list[str]:
    """S3-05：校验 extraVolumes 结构，并对整组挂载预跑 compose 安全审计。

    结构复用 compose 渲染时的 ``_validate_extra_volume``；审计与生成 compose 时同口径，
    出现 critical 即拒绝保存，避免把不会被渲染通过的配置写入 manifest。
    """
    import yaml

    from local_webpage_access.compose import _validate_extra_volume
    from local_webpage_access.security import LEVEL_CRITICAL, audit_compose

    normalized = [_validate_extra_volume(entry, instance_id=instance_id) for entry in entries]
    if normalized:
        probe = yaml.safe_dump({"services": {"app": {"volumes": normalized}}})
        critical = [f for f in audit_compose(probe) if f.level == LEVEL_CRITICAL]
        if critical:
            detail = "；".join(f.message for f in critical)
            raise ValueError(f"extraVolumes 未通过安全审计：{detail}")
    return normalized


def update_instance_settings(
    workspace: Workspace, registry: Registry, instance_id: str, changes: dict[str, Any]
) -> InstanceManifest:
    allowed = {
        "buildEnv",
        "buildBaseFromAlias",
        "redundancyAcknowledged",
        "systemDeps",
        "buildHooks",
        "extraVolumes",
    }
    if not changes or set(changes) - allowed:
        raise ValueError(
            "仅允许设置 buildEnv、buildBaseFromAlias、redundancyAcknowledged、"
            "systemDeps、buildHooks、extraVolumes"
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
        data = {**old.model_dump(), **{k: v for k, v in changes.items() if k != "extraVolumes"}}
        if "extraVolumes" in changes:
            if old.container is None:
                raise ValueError("extraVolumes 仅支持容器实例")
            entries = changes["extraVolumes"]
            if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
                raise ValueError("extraVolumes 必须是字符串列表（SRC:DST[:ro]）")
            container = dict(data["container"] or {})
            container["extraVolumes"] = validate_extra_volumes(instance_id, entries)
            data["container"] = container
        updated = InstanceManifest.model_validate(data)
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
        # Python 走 apt 切源链；Node(alpine) 在 npm 安装前 apk add。
        # 空列表仍允许，用来清空。buildHooks 在依赖安装之后，不能替代这一层。
        if (
            "systemDeps" in changes
            and (changes.get("systemDeps") or [])
            and old.kind not in (Kind.PYTHON, Kind.NODE)
        ):
            raise ValueError(
                "systemDeps 仅支持 Python（Debian/apt）与 Node（Alpine/apk）容器"
            )
        updated.touch()
        updated.save(path)
        registry.add_event(instance_id, "config", "更新实例配置：" + "、".join(sorted(changes)))
        return updated


def confirm_instance_shape(
    workspace: Workspace,
    registry: Registry,
    instance_id: str,
    *,
    kind: Kind,
    runtime: Runtime,
) -> InstanceManifest:
    """BUG-869：人工确认 pending 实例形态（kind/runtime/servingMode）的官方通道。

    pending 实例此前只能手改 manifest + 直接改 sqlite registry：configure
    因 registry 未同步会拒绝 systemDeps（「systemDeps 仅支持容器实例」），
    ``lwa scan`` 重扫又会覆盖手改。本函数一次完成：

    1. 仅允许 status=pending 的实例（形态已确认的实例不得静默改写）；
    2. 以旧 manifest 为底改形态字段，补齐目标形态的默认 static/container
       配置（容器缺省 internalPort 取 network.internalPort，兜底 8000）；
    3. status 由 pending 升为 stopped——与重扫识别成功同语义（形态已确认，
       可启动），同时使 ``lwa scan`` / ``import --update`` 的保留语义
       （BUG-876：已确认形态不被重扫覆盖）生效；
    4. 复用 :meth:`Registry.upsert_from_manifest` 同步 registry，
       manifest 与 registry 一致，后续 ``configure --system-deps`` 等
       依赖 runtime 的校验即可用。

    返回更新后的 manifest（调用方负责输出建议下一步）。
    """
    with instance_lock(workspace, instance_id):
        path = workspace.app_manifest_path(instance_id)
        if not registry.instance_exists(instance_id) or not path.is_file():
            raise LwaError(f"实例 {instance_id} 不存在", instance_id=instance_id)
        old = InstanceManifest.load(path)
        if old.status != Status.PENDING:
            raise LwaError(
                f"实例 {instance_id} 当前状态为 {old.status.value}，形态已确认；"
                "--kind/--runtime 仅用于确认 pending 实例的形态",
                instance_id=instance_id,
            )
        serving = (
            ServingMode.CONTAINER
            if runtime == Runtime.DOCKER_COMPOSE
            else ServingMode.SHARED_STATIC
        )
        data = {**old.model_dump(), "kind": kind, "runtime": runtime, "servingMode": serving}
        # BUG-891：pending 草稿（static 兜底形态）可能已在导入时登记路径别名
        # （--path-alias → static.routeMode="name" + routeHost）；确认形态重写
        # static/container 时必须把别名带进目标配置，否则别名被静默抹掉。
        alias_host = None
        if old.static is not None and old.static.routeMode == "name":
            alias_host = old.static.routeHost
        if runtime == Runtime.DOCKER_COMPOSE:
            from local_webpage_access.resource_profiles import profile_to_limits

            data["container"] = {
                "projectName": f"lwa-{instance_id}",
                "serviceName": "app",
                "internalPort": old.network.internalPort or 8000,
                "composePath": str(workspace.app_compose_path(instance_id)),
                "dockerfilePath": str(workspace.app_dockerfile_path(instance_id)),
                # 与 build_manifest_from_detection 新导入口径对齐：显式非 root
                # （None 会被当 legacy root 实例告警）+ 按 resourceProfile 映射配额。
                "runAsNonRoot": True,
                "resourceLimits": profile_to_limits(old.resourceProfile).model_dump(),
            }
            if alias_host:
                data["container"]["routeMode"] = "name"
                data["container"]["routeHost"] = alias_host
            data["static"] = None
        else:
            data["static"] = {"root": "public", "gateway": "caddy"}
            if alias_host:
                data["static"]["routeMode"] = "name"
                data["static"]["routeHost"] = alias_host
            data["container"] = None
        data["status"] = Status.STOPPED
        updated = InstanceManifest.model_validate(data)
        updated.touch()
        updated.save(path)
        registry.upsert_from_manifest(
            updated,
            app_path=str(workspace.app_current(instance_id)),
            source_zip_path=str(workspace.app_original_zip(instance_id)),
        )
        registry.add_event(
            instance_id,
            "config",
            f"人工确认形态：{kind.value}/{runtime.value}（pending → stopped）",
        )
        return updated
