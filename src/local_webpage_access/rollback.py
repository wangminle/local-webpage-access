"""S3-01 有限回滚：把实例恢复到上一个（或指定的）留档版本（DEV-157 / issue #51 远期 / D8）。

范围声明（D8）：**只回滚镜像与生成配置（compose.yaml / Dockerfile / .env 的快照），
不回滚 ``data/``**。回滚用快照中的生成物 + 对应 ``b<build_id>`` 镜像执行 up，
随后跑实证验证与别名守卫；验证失败时补偿恢复原生成物与原镜像并重新 up。
当前 manifest 的 ``entry.start`` / ``preStart`` / ``buildHooks``，或目标版本留档的启动/构建命令
（旧镜像 CMD 里的 preStart）含迁移命令时，必须显式 ``yes=True``（CLI 即
``--yes``），否则抛 :class:`RollbackNeedsYes`。回滚后的旧代码可能与新数据库
结构不兼容，且 ``runtime.up()`` 会执行旧镜像里的迁移命令；``data/`` 不回滚，
验证失败后的镜像补偿也无法撤销这些数据库修改。

revision 语义同 S2-09：回滚是生命周期操作。manifest、回滚目标、迁移确认与
补偿现场都在持锁之后读取，成功落盘与 revision CAS 也在同一把锁内完成，
避免锁前陈旧副本或锁外 save 覆盖并发操作。指纹写入 ``rollbackBuildId``，
start/restart 在显式 rebuild 前不再按当前源码重渲染生成物。
"""

from __future__ import annotations

import contextlib
import re
from pathlib import Path
from typing import Any

from local_webpage_access import rebuild_txn, versions
from local_webpage_access.docker_runtime import DockerRuntime
from local_webpage_access.errors import DockerError, HostingError
from local_webpage_access.logging import get_logger, now_iso
from local_webpage_access.models import DesiredState, InstanceManifest, Status
from local_webpage_access.paths import Workspace
from local_webpage_access.registry.dao import Registry

log = get_logger("rollback")

GENERATED_FILES = rebuild_txn.GENERATED_FILES

#: 启动/构建命令中的迁移命令特征（小写子串匹配）。
MIGRATION_KEYWORDS = ("alembic", "migrate", "prisma")


class RollbackNeedsYes(HostingError):
    """D8：启动/构建命令含迁移，回滚必须显式确认（CLI 用 --yes）。"""

    code = "rollback_needs_yes"


def text_has_migration(text: str | None) -> bool:
    """命令文本是否含数据库迁移类关键字（大小写不敏感）。"""
    if not text:
        return False
    blob = text.lower()
    return any(kw in blob for kw in MIGRATION_KEYWORDS)


def migration_commands_blob(manifest: InstanceManifest) -> str:
    """该版本镜像的实际启动命令、preStart 与 buildHooks 原文。"""
    parts: list[str] = []
    if manifest.entry.start:
        parts.append(manifest.entry.start)
    if manifest.preStart:
        parts.append(manifest.preStart)
    parts.extend(hook for hook in (manifest.buildHooks or []) if hook)
    return "\n".join(parts)


def prestart_has_migration(manifest: InstanceManifest) -> bool:
    """manifest 的启动/构建命令是否含数据库迁移（D8 确认门槛）。"""
    return text_has_migration(migration_commands_blob(manifest))


def _dockerfile_command_text(dockerfile: str) -> str:
    """抽出 Dockerfile 里会进镜像的 CMD/RUN/ENTRYPOINT（含续行），忽略注释。"""
    chunks: list[str] = []
    capturing = False
    for raw in dockerfile.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            capturing = False
            continue
        upper = line.upper()
        if upper.startswith(("CMD ", "RUN ", "ENTRYPOINT ")):
            chunks.append(line)
            capturing = line.endswith("\\")
            continue
        if capturing:
            chunks.append(line)
            capturing = line.endswith("\\")
    return "\n".join(chunks)


def archive_version_migration(
    workspace: Workspace,
    instance_id: str,
    build_id: int,
    manifest: InstanceManifest,
) -> None:
    """把当前 manifest 的迁移命令随该版本快照留档。"""
    versions.write_migration_archive(
        workspace,
        instance_id,
        build_id,
        migration_commands_blob(manifest),
    )


def target_version_has_migration(
    workspace: Workspace,
    instance_id: str,
    build_id: int,
) -> bool:
    """目标留档版本的启动/构建命令是否含迁移。

    合并检查显式命令留档与快照 Dockerfile 的 CMD/RUN/ENTRYPOINT，避免旧版
    不完整的命令留档遮蔽镜像里的实际迁移命令。
    """
    archived = versions.read_migration_archive(workspace, instance_id, build_id)
    if archived is None:
        return False
    text, source = archived
    if source == "dockerfile":
        return text_has_migration(_dockerfile_command_text(text))
    if text_has_migration(text):
        return True
    dockerfile = versions.build_snapshot_dir(workspace, instance_id, build_id) / "Dockerfile"
    return dockerfile.is_file() and text_has_migration(
        _dockerfile_command_text(dockerfile.read_text(encoding="utf-8"))
    )


def rollback_needs_confirmation(
    manifest: InstanceManifest | None,
    workspace: Workspace,
    instance_id: str,
    build_id: int,
) -> bool:
    """回滚到 ``build_id`` 是否必须显式确认。

    当前配置与目标版本留档命令任一含迁移即需要确认。管理页的
    ``requiresConfirm`` 与这里使用同一判断。
    """
    current = prestart_has_migration(manifest) if manifest is not None else False
    return current or target_version_has_migration(workspace, instance_id, build_id)


def _normalize_image_id(value: Any) -> str | None:
    """镜像 ID 归一化（统一入口在 versions.normalize_image_id，BUG-800/801）。"""
    return versions.normalize_image_id(value)


def select_rollback_target(
    workspace: Workspace,
    registry: Registry,
    instance_id: str,
    *,
    exclude_build_id: int | None = None,
    exclude_image_id: str | None = None,
) -> dict[str, Any] | None:
    """最近一个可回滚版本：验证通过后留档、快照齐全且镜像仍在。

    资格见 :func:`versions.is_rollback_candidate`（``status=success`` 且已写入
    镜像引用）。镜像构建成功但部署验证失败的行没有这份资格。
    ``exclude_build_id`` 用于 daemon 自愈——排除触发本次失败的构建本身。
    ``exclude_image_id`` 用于手动回滚（未指定 ``--to``）——排除当前在用的镜像，
    否则当前版本本身会被当作“上一可用版本”选中，回滚等于原地重部署。
    """
    in_use = _normalize_image_id(exclude_image_id)
    for row in registry.list_builds(instance_id, limit=100):
        if int(row["id"]) == exclude_build_id:
            continue
        if in_use and _normalize_image_id(row.get("image_id")) == in_use:
            continue
        if not versions.is_rollback_candidate(row):
            continue
        if not versions.snapshot_complete(workspace, instance_id, int(row["id"])):
            continue
        if not versions.image_exists(str(row["image_ref"])):
            continue
        return dict(row)
    return None


def _read_generated(docker_dir: Path) -> dict[str, bytes | None]:
    """内存暂存三件生成物（None=不存在），供回滚失败时无损恢复。"""
    out: dict[str, bytes | None] = {}
    for name in GENERATED_FILES:
        path = docker_dir / name
        out[name] = path.read_bytes() if path.is_file() else None
    return out


def _align_restored_host_port(env_path: Path, host_port: int | None) -> None:
    """快照 .env 可能含历史 HOST_PORT；容器必须继续监听当前已分配端口。"""
    if host_port is None:
        return
    line = f"HOST_PORT={int(host_port)}"
    if not env_path.is_file():
        env_path.write_text(line + "\n", encoding="utf-8")
        return
    text = env_path.read_text(encoding="utf-8")
    if re.search(r"(?m)^HOST_PORT=", text):
        text = re.sub(r"(?m)^HOST_PORT=.*$", line, text)
    else:
        text = text.rstrip("\n") + "\n" + line + "\n"
    env_path.write_text(text, encoding="utf-8")


def _generated_matches(docker_dir: Path, expected: dict[str, bytes | None]) -> bool:
    """磁盘上的三件生成物是否与补偿前暂存的原内容一致。"""
    for name, content in expected.items():
        path = docker_dir / name
        if content is None:
            if path.exists():
                return False
            continue
        if not path.is_file() or path.read_bytes() != content:
            return False
    return True


def _write_generated(docker_dir: Path, data: dict[str, bytes | None]) -> None:
    docker_dir.mkdir(parents=True, exist_ok=True)
    for name, content in data.items():
        path = docker_dir / name
        if content is None:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        else:
            path.write_bytes(content)


def _validate_target(
    workspace: Workspace,
    registry: Registry,
    instance_id: str,
    to_build_id: int,
) -> dict[str, Any]:
    row = next(
        (r for r in registry.list_builds(instance_id, limit=200) if int(r["id"]) == to_build_id),
        None,
    )
    if row is None:
        raise HostingError(
            f"实例 {instance_id} 不存在构建 #{to_build_id}，无法回滚", instance_id=instance_id
        )
    if not versions.is_rollback_candidate(row):
        if row.get("status") == "success":
            # 产物列被 GC 置空（BUG-802）或 S3-01 之前的老行
            raise HostingError(
                f"实例 {instance_id} 构建 #{to_build_id} 的镜像留档已被 GC 清理，"
                "无法回滚（可调大 imageRetention 后重建留档）",
                instance_id=instance_id,
            )
        raise HostingError(
            f"实例 {instance_id} 构建 #{to_build_id} 不是成功构建（status={row.get('status')}），"
            "没有可回滚的镜像留档",
            instance_id=instance_id,
        )
    if not versions.snapshot_complete(workspace, instance_id, to_build_id):
        raise HostingError(
            f"实例 {instance_id} 构建 #{to_build_id} 的生成物快照缺失或不完整，无法回滚",
            instance_id=instance_id,
        )
    if not versions.image_exists(str(row["image_ref"])):
        raise HostingError(
            f"实例 {instance_id} 构建 #{to_build_id} 的镜像 {row['image_ref']} 已被清理，"
            "无法回滚（可调大 imageRetention 后重建留档）",
            instance_id=instance_id,
        )
    return dict(row)


def rollback_instance(
    workspace: Workspace,
    config: Any,
    registry: Registry,
    instance_id: str,
    *,
    to_build_id: int | None = None,
    yes: bool = False,
    expected_revision: int | None = None,
    source: str = "cli",
    run_alias_guard: bool = True,
) -> InstanceManifest:
    """把容器实例回滚到指定（或上一个可用）留档版本；返回更新后的 manifest。

    D8：只回滚镜像与生成配置，不动 ``data/``；preStart/buildHooks 含迁移命令时
    必须 ``yes=True``。验证失败时补偿恢复原版本并抛 :class:`HostingError`。
    """
    from local_webpage_access import hosting as hosting_mod
    from local_webpage_access.lifecycle import _ensure_revision_locked, instance_lock

    DockerRuntime.ensure_available()
    runtime = DockerRuntime(workspace, registry)
    started_at = now_iso()
    docker_dir = workspace.app_docker(instance_id)
    # BUG-799：停机成功后的任何失败（快照恢复失败 / docker tag 失败 / up 失败 /
    # 验证失败）都必须补偿拉回原版本——不能按「现场未被改写」收尾，否则容器
    # 已停、无人拉起，服务中断且文案误导。补偿各步对未改写的现场幂等。
    stopped = False

    def _record(status: str, *, error: str | None = None) -> None:
        operation = {
            "kind": "rollback",
            "status": status,
            "toBuildId": target_id,
            "startedAt": started_at,
            "finishedAt": now_iso(),
            "source": source,
        }
        if error:
            operation["error"] = error[:500]
        with contextlib.suppress(Exception):
            registry.update_last_operation(instance_id, operation)
        manifest.lastOperation = operation
        if error:
            manifest.lastError = error[:500]
        with contextlib.suppress(Exception):
            manifest.save(workspace.app_manifest_path(instance_id))

    with instance_lock(workspace, instance_id):
        # 锁前读到的 manifest / 目标 / 补偿现场会盖掉等锁期间已完成的配置或回滚。
        manifest = InstanceManifest.load(workspace.app_manifest_path(instance_id))
        if manifest.runtime.value != "docker-compose" or manifest.container is None:
            raise HostingError(
                f"实例 {instance_id} 不是容器实例（runtime={manifest.runtime.value}），无法回滚镜像版本",
                instance_id=instance_id,
                runtime=manifest.runtime.value,
            )
        if to_build_id is None:
            in_use_image = (registry.get_container(instance_id) or {}).get(
                "image_id"
            ) or manifest.container.imageId
            target = select_rollback_target(
                workspace, registry, instance_id, exclude_image_id=in_use_image
            )
            if target is None:
                raise HostingError(
                    f"实例 {instance_id} 没有可回滚的留档版本（需要成功构建 + 镜像仍在 + 快照齐全）",
                    instance_id=instance_id,
                )
        else:
            target = _validate_target(workspace, registry, instance_id, to_build_id)
        target_id = int(target["id"])
        target_ref = str(target["image_ref"])
        repo = rebuild_txn.image_repo(manifest)
        current_migration = prestart_has_migration(manifest)
        target_migration = target_version_has_migration(workspace, instance_id, target_id)
        if (current_migration or target_migration) and not yes:
            parts: list[str] = []
            if current_migration:
                parts.append("当前启动/构建命令")
            if target_migration:
                parts.append(f"目标构建 #{target_id} 留档的启动或构建命令")
            raise RollbackNeedsYes(
                f"实例 {instance_id} 的{'、'.join(parts)}含数据库迁移命令：回滚只还原镜像与"
                "生成配置，不回滚 data/。启动旧镜像可能执行其中的迁移命令，"
                "验证失败后的镜像补偿也无法撤销数据库修改；旧版本代码可能无法兼容当前数据库结构。"
                "如确认继续，请显式加 --yes（管理页勾选「我已了解风险」）",
                instance_id=instance_id,
            )
        _ensure_revision_locked(registry, instance_id, expected_revision)
        observed_revision = registry.get_revision(instance_id)
        current_files = _read_generated(docker_dir)
        current_image = manifest.container.imageId
        if not current_image:
            with contextlib.suppress(Exception):
                current_image = runtime.image_id(instance_id)
        host_port = manifest.container.hostPort
        try:
            hosting_mod.stop_or_abort(runtime, instance_id, purpose="回滚")
            stopped = True
            if not versions.restore_config_snapshot(workspace, instance_id, target_id):
                raise HostingError(
                    f"实例 {instance_id} 构建 #{target_id} 的生成物快照缺失，回滚中止",
                    instance_id=instance_id,
                )
            _align_restored_host_port(workspace.app_env_path(instance_id), host_port)
            # 目标镜像打回 :latest，compose up 直接使用留档镜像，不再构建
            rebuild_txn.tag_image(target_ref, f"{repo}:{rebuild_txn.LATEST_TAG}")
            runtime.up(instance_id)
            verification = hosting_mod._evaluate_container_verification(
                host_port, manifest, workspace, registry, instance_id
            )
            if verification["overall_status"] == "failed":
                detail = verification.get("error", "必选探针未通过")
                raise HostingError(
                    f"回滚后验证失败（host_port={host_port}）：{detail}",
                    instance_id=instance_id,
                )
        except Exception as exc:
            if stopped:
                # 补偿：down → 恢复原生成物 → 原镜像打回 latest → up → 恢复原版本。
                # 停机后、改写前的失败同样适用：文件重写为原内容、latest 本就指向
                # 原镜像（重打一次无害），最终 up 把原版本拉回服务。
                residual: list[str] = []
                files_restored = False
                image_retagged = False
                compensated_up = False
                with contextlib.suppress(Exception):
                    runtime.down(instance_id)
                try:
                    _write_generated(docker_dir, current_files)
                    files_restored = _generated_matches(docker_dir, current_files)
                    if not files_restored:
                        residual.append("生成物恢复后与原内容不一致")
                except OSError as wexc:  # noqa: PERF203
                    residual.append(f"生成物恢复失败：{wexc}")
                if current_image:
                    try:
                        rebuild_txn.tag_image(current_image, f"{repo}:{rebuild_txn.LATEST_TAG}")
                        image_retagged = True
                    except DockerError as texc:
                        residual.append(f"原镜像打回 latest 失败：{texc}")
                else:
                    residual.append("缺少原镜像 ID，无法确认镜像已恢复")
                try:
                    runtime.up(instance_id)
                    compensated_up = True
                except Exception as upexc:  # noqa: BLE001
                    residual.append(f"原版本重新拉起失败：{upexc}")
                restored = False
                observed_cid = None
                observed_image = None
                if compensated_up:
                    with contextlib.suppress(Exception):
                        observed_cid = runtime.container_id(instance_id)
                    with contextlib.suppress(Exception):
                        observed_image = runtime.image_id(instance_id)
                    # 探针通过不够：目标镜像可能仍占着 latest，第二次 up 会把它
                    # 拉起来。必须同时确认生成物已写回、原镜像已打回 latest，
                    # 且观测到的 image ID 等于原镜像。
                    try:
                        compensation_verification = (
                            hosting_mod._evaluate_container_verification(
                                host_port, manifest, workspace, registry, instance_id
                            )
                        )
                        service_recovered = (
                            compensation_verification.get("overall_status") != "failed"
                        )
                    except Exception as excv:  # noqa: BLE001 — 验证异常按未恢复处理
                        residual.append(f"原版本恢复验证异常：{excv}")
                        service_recovered = False
                    observed_norm = _normalize_image_id(observed_image)
                    original_norm = _normalize_image_id(current_image)
                    image_id_matches = (
                        original_norm is not None and observed_norm == original_norm
                    )
                    if not service_recovered:
                        residual.append("原版本拉起后验证未通过（服务未确认恢复）")
                    elif not image_retagged:
                        if not any("镜像" in item for item in residual):
                            residual.append("原镜像未确认打回 latest")
                    elif not image_id_matches:
                        residual.append("观测到的镜像与原镜像不一致，不能视为已恢复原版本")
                    if (
                        service_recovered
                        and files_restored
                        and image_retagged
                        and image_id_matches
                    ):
                        if observed_cid:
                            manifest.container.containerId = observed_cid
                        if observed_image:
                            manifest.container.imageId = observed_image
                        manifest.status = Status.RUNNING
                        manifest.desiredState = DesiredState.RUNNING
                        restored = True
                    else:
                        # 身份不可信：清 containerId，避免下次 start 对错误镜像
                        # 做 compose start（BUG-300）。也不把目标镜像登记成原版本。
                        manifest.container.containerId = None
                        manifest.status = Status.FAILED
                        manifest.desiredState = DesiredState.RUNNING
                else:
                    manifest.container.containerId = None
                    manifest.status = Status.FAILED
                    manifest.desiredState = DesiredState.RUNNING
                suffix = "" if not residual else "；补偿残留：" + "；".join(residual)
                if residual:
                    with contextlib.suppress(Exception):
                        registry.add_event(
                            instance_id,
                            "rollback_failed",
                            f"回滚验证失败且补偿存在残留：{'；'.join(residual)}",
                        )
                _record("failed", error=str(exc))
                with contextlib.suppress(Exception):
                    registry.upsert_from_manifest(manifest)
                    registry.update_status(
                        instance_id,
                        manifest.status.value,
                        desired_state=DesiredState.RUNNING.value,
                    )
                outcome = "已恢复原版本" if restored else "未能恢复原版本"
                raise HostingError(
                    f"实例 {instance_id} 回滚到构建 #{target_id} 失败：{exc}，{outcome}{suffix}",
                    instance_id=instance_id,
                ) from exc
            # 停机失败：现场未被改写，旧容器仍在服务，当前版本未受影响
            _record("failed", error=str(exc))
            raise

        # 成功落盘必须在锁内：锁外 save 会覆盖并发 stop 写下的 STOPPED。
        with contextlib.suppress(Exception):
            cid = runtime.container_id(instance_id)
            if cid:
                manifest.container.containerId = cid
        observed_image = None
        with contextlib.suppress(Exception):
            observed_image = runtime.image_id(instance_id)
        manifest.container.imageId = observed_image or target.get("image_id")
        manifest.status = Status.RUNNING
        manifest.desiredState = DesiredState.RUNNING
        manifest.lastError = None
        try:
            from local_webpage_access.lifecycle import _compute_deployment_fingerprints

            fps = _compute_deployment_fingerprints(workspace, manifest)
            fps["rollbackBuildId"] = target_id
            manifest.deploymentFingerprints = fps
        except Exception:  # noqa: BLE001 — 指纹失败仍钉住，避免 start 按当前源码重建
            log.debug("实例 %s 回滚后指纹计算失败，仅写入 rollbackBuildId", instance_id)
            prev = dict(manifest.deploymentFingerprints or {})
            prev["rollbackBuildId"] = target_id
            manifest.deploymentFingerprints = prev
        manifest.touch()
        manifest.save(workspace.app_manifest_path(instance_id))
        registry.upsert_from_manifest(manifest)
        registry.update_status(
            instance_id, Status.RUNNING.value, desired_state=DesiredState.RUNNING.value,
            clear_last_error=True,
        )
        registry.record_started(instance_id)
        _record("succeeded")
        registry.add_event(
            instance_id,
            "rollback",
            f"已回滚到构建 #{target_id}（镜像 {target_ref}，源码哈希 "
            f"{(target.get('source_hash') or '?')[:12]}）；data/ 未回滚",
        )
        if observed_revision is not None:
            from local_webpage_access.errors import RegistryError

            try:
                registry.cas_increment_revision(instance_id, observed_revision)
            except RegistryError as exc:
                if exc.code == "revision_conflict":
                    from local_webpage_access.errors import LifecycleError

                    raise LifecycleError(
                        f"实例 {instance_id} 已被其他通道更新（revision 冲突）",
                        code="revision_conflict",
                        instance_id=instance_id,
                    ) from exc
                raise

        if run_alias_guard:
            from local_webpage_access.path_alias import maybe_verify_alias_after_start

            alias_fragment_preexisting = workspace.app_alias_config(instance_id).is_file()
            maybe_verify_alias_after_start(
                workspace,
                config,
                registry,
                instance_id,
                manifest,
                alias_fragment_preexisting=alias_fragment_preexisting,
            )
            with contextlib.suppress(Exception):
                manifest.save(workspace.app_manifest_path(instance_id))
    return manifest


__all__ = [
    "RollbackNeedsYes",
    "MIGRATION_KEYWORDS",
    "text_has_migration",
    "migration_commands_blob",
    "prestart_has_migration",
    "archive_version_migration",
    "target_version_has_migration",
    "rollback_needs_confirmation",
    "select_rollback_target",
    "rollback_instance",
]
