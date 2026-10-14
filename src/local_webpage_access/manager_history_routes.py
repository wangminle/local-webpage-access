"""实例「历史」类管理页路由：操作时间线、镜像版本留档与有限回滚（S3-06 拆分）。

原先与其余 ``/api/*`` 路由一同定义在 :func:`manager_api._register_routes` 的闭包里；
S3-01（版本/回滚）、S3-04（时间线）新增的这三条路由与两个负载函数集中到本模块，
行为、路径、状态码与错误格式与拆分前一致。

* ``GET  /api/instances/{id}/timeline``  —— 操作时间线（S3-04 / DEV-160）
* ``GET  /api/instances/{id}/versions``  —— 版本留档列表（S3-01 / DEV-157）
* ``POST /api/instances/{id}/rollback``  —— 有限回滚（S3-01 / DEV-157）

依赖 ``manager_api`` 的请求上下文与辅助函数；``manager_api`` 在
``_register_routes`` 内惰性导入本模块，避免循环导入。
"""

from __future__ import annotations

import contextlib
from typing import Any, Callable

from fastapi import Body, FastAPI, HTTPException, Query, status

from local_webpage_access.errors import LwaError
from local_webpage_access.manager_api import (
    _body_bool,
    _body_optional_int,
    _Ctx,
    _load_manifest_dict,
    _require_instance,
)


def versions_payload(
    workspace: Any,
    config: Any,
    registry: Any,
    instance_id: str,
) -> dict[str, Any] | None:
    """S3-01：实例详情/versions 端点共用的版本留档负载；非容器实例返回 None。"""
    row = registry.get_instance(instance_id) or {}
    if row.get("runtime") != "docker-compose":
        return None
    from local_webpage_access import versions as versions_mod
    from local_webpage_access.models import InstanceManifest
    from local_webpage_access.rollback import rollback_needs_confirmation, target_version_has_migration
    from local_webpage_access.versions import normalize_image_id

    rows = versions_mod.list_versions(registry, instance_id)
    _count, usage = versions_mod.retained_disk_usage_bytes(registry, instance_id)
    container_row = registry.get_container(instance_id) or {}
    # BUG-801：containers 行存 docker inspect 原样值（带 sha256: 前缀），builds 行
    # 已去前缀——不归一化时 inUse 恒 False，当前在用版本会被显示成可回滚。
    in_use = normalize_image_id(container_row.get("image_id"))
    for item in rows:
        item["inUse"] = bool(in_use) and normalize_image_id(item.get("imageId")) == in_use
    manifest_row = _load_manifest_dict(workspace, instance_id)
    manifest_obj = None
    if manifest_row and not manifest_row.get("_error"):
        with contextlib.suppress(Exception):
            manifest_obj = InstanceManifest.load(workspace.app_manifest_path(instance_id))
    for item in rows:
        build_id = int(item["id"])
        item["hasMigration"] = target_version_has_migration(workspace, instance_id, build_id)
        # 与 rollback_instance 相同：当前配置或该目标版本留档命令含迁移。
        item["requiresConfirm"] = rollback_needs_confirmation(
            manifest_obj, workspace, instance_id, build_id
        )
    requires_confirm = any(bool(item.get("requiresConfirm")) for item in rows)
    if manifest_obj is not None and not requires_confirm:
        from local_webpage_access.rollback import prestart_has_migration

        requires_confirm = prestart_has_migration(manifest_obj)
    return {
        "retention": getattr(config, "imageRetention", 3),
        "versions": rows,
        "diskUsageBytes": usage,
        "requiresConfirm": requires_confirm,
        "scopeNote": "回滚只还原镜像与生成配置，不回滚 data/（D8）",
    }


def timeline_payload(
    ctx: Any,
    instance_id: str,
    manifest: dict[str, Any] | None,
    *,
    limit: int,
) -> dict[str, Any]:
    """S3-04：实例操作时间线负载（详情抽屉与 timeline 端点共用）。"""
    from local_webpage_access.timeline import build_timeline

    last_operation = None
    if manifest and not manifest.get("_error"):
        last_operation = manifest.get("lastOperation")
    items = build_timeline(
        ctx.registry,
        instance_id,
        last_operation=last_operation,
        limit=limit,
    )
    return {"items": items}


def register_history_routes(
    app: FastAPI,
    *,
    api: Any,
    docker_ops_blocked_reason: Callable[[Any, str], str | None],
) -> None:
    """注册 timeline / versions / rollback 三条路由。

    ``api`` 是 ``Depends(require_token)``；``docker_ops_blocked_reason`` 是 Docker 能力
    降级时的服务端 fail-closed 判定（BUG-237），与生命周期路由共用同一实现。
    """
    from local_webpage_access.models import Status
    from local_webpage_access.status import instance_status, sync_status

    # ---- 操作时间线（S3-04 / DEV-160）----
    @app.get(
        "/api/instances/{instance_id}/timeline",
        dependencies=[api],
        tags=["instances"],
    )
    def get_timeline(
        instance_id: str,
        limit: int = Query(50, ge=1, le=200),
    ) -> dict[str, Any]:
        ctx = _Ctx(app)
        _require_instance(ctx, instance_id)
        manifest = _load_manifest_dict(ctx.workspace, instance_id)
        return {
            "instanceId": instance_id,
            "items": timeline_payload(ctx, instance_id, manifest, limit=limit)["items"],
        }

    # ---- S3-01 / DEV-157：版本留档与有限回滚 ----

    @app.get(
        "/api/instances/{instance_id}/versions",
        dependencies=[api],
        tags=["instances"],
    )
    def versions_op(instance_id: str) -> dict[str, Any]:
        """实例留档版本列表（构建号/时间/源码哈希/镜像是否仍在/结果/占用）。

        非容器实例返回 409。payload 附带 ``requiresConfirm``（当前启动/构建命令
        或任一留档版本的启动命令含迁移时回滚需显式确认，与 rollback 相同判断）
        与 ``scopeNote`` 范围说明。各版本另有 ``hasMigration`` / ``requiresConfirm``。
        """
        ctx = _Ctx(app)
        _require_instance(ctx, instance_id)
        payload = versions_payload(ctx.workspace, ctx.config, ctx.registry, instance_id)
        if payload is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": {
                        "code": "not_container",
                        "message": f"实例 {instance_id} 不是容器实例，无镜像版本留档",
                    }
                },
            )
        return {"instanceId": instance_id, **payload}

    @app.post(
        "/api/instances/{instance_id}/rollback",
        dependencies=[api],
        tags=["instances"],
    )
    def rollback_op(instance_id: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """S3-01 有限回滚：恢复到指定（默认上一可用）留档版本。

        Body: ``{"toBuildId": null, "expectedRevision": null, "confirm": false}``。
        D8：只回滚镜像与生成配置，不回滚 ``data/``；启动/构建命令含迁移时须
        ``confirm=true``，否则 409 ``rollback_needs_yes``。
        """
        from local_webpage_access.rollback import RollbackNeedsYes, rollback_instance

        ctx = _Ctx(app)
        _require_instance(ctx, instance_id)
        row = ctx.registry.get_instance(instance_id) or {}
        if row.get("status") == Status.CANCELLING.value:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": {
                        "code": "cancelling",
                        "message": "实例正在取消构建，暂时不能 rollback",
                    }
                },
            )
        expected_revision = _body_optional_int(payload, "expectedRevision")
        # S2-09：锁外预检，友好 409；权威复核在 rollback_instance 锁内完成
        if expected_revision is not None:
            current = ctx.registry.get_revision(instance_id)
            if current is None or int(current) != int(expected_revision):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "error": {
                            "code": "revision_conflict",
                            "message": (
                                f"实例已被其他操作更新（期望 revision="
                                f"{int(expected_revision)}，当前 {current}），请刷新后重试"
                            ),
                            "expected": int(expected_revision),
                            "actual": current,
                        }
                    },
                )
        blocked = docker_ops_blocked_reason(ctx, instance_id)
        if blocked is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": {"code": "capability_denied", "message": f"拒绝 rollback：{blocked}"}},
            )
        to_build_id = _body_optional_int(payload, "toBuildId")
        confirm = _body_bool(payload, "confirm", False)
        try:
            rollback_instance(
                ctx.workspace,
                ctx.config,
                ctx.registry,
                instance_id,
                to_build_id=to_build_id,
                yes=confirm,
                expected_revision=expected_revision,
                source="manager",
            )
        except RollbackNeedsYes as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": {
                        "code": "rollback_needs_yes",
                        "message": str(exc),
                    }
                },
            ) from exc
        except LwaError:
            raise  # _ERROR_STATUS 映射由全局异常处理承载
        sync_status(ctx.workspace, ctx.config, ctx.registry, instance_id)
        snap = instance_status(ctx.workspace, ctx.config, ctx.registry, instance_id)
        return {"instanceId": instance_id, "action": "rollback", "instance": snap.to_dict()}
