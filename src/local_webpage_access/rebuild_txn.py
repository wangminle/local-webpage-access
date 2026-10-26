"""容器重建事务化：先建后换所需的快照、镜像保留与补偿辅助（PLN-052 S2-03 / D4 / D5）。

已经在跑的实例执行 rebuild 时，编排顺序为：

1. 快照当前生成物到 ``docker/.prev/``，并把旧镜像保留为 ``<repo>:prev``；
2. 渲染新生成物并 ``build``——旧容器在此期间持续服务；
3. 数据救援、``stop_or_abort`` 停机、清空身份、``up``、验证。

第 3 步之后失败时，按补偿路径恢复旧版本：down 新容器 → 恢复快照 → 把 ``:prev``
重新打回 ``:latest`` → 拉起旧版本。这里只做与镜像和生成物有关的辅助，
编排留在 :mod:`local_webpage_access.hosting`。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from local_webpage_access.errors import DockerError, HostingError
from local_webpage_access.models import InstanceManifest
from local_webpage_access.paths import Workspace

# 影响镜像与运行形态的三件生成物；快照与恢复只处理这些文件。
GENERATED_FILES: tuple[str, ...] = ("compose.yaml", "Dockerfile", ".env")
# helper 只在 Django 静态配方命中时存在，不能变成所有历史快照的必需文件。
OPTIONAL_GENERATED_FILES: tuple[str, ...] = ("lwa_static_wsgi.py",)
ALL_GENERATED_FILES = GENERATED_FILES + OPTIONAL_GENERATED_FILES
PREV_DIRNAME = ".prev"
PREV_TAG = "prev"
LATEST_TAG = "latest"
REBUILD_STRATEGIES: tuple[str, ...] = ("build-first", "stop-first")


def rebuild_strategy(manifest: InstanceManifest) -> str:
    """实例的重建策略；未设置时为 ``build-first``（先建后换）。"""
    value = getattr(manifest.container, "rebuildStrategy", None) if manifest.container else None
    return value if value in REBUILD_STRATEGIES else "build-first"


def image_repo(manifest: InstanceManifest) -> str:
    """compose 对未指定 ``image:`` 的 build 服务默认产生的镜像仓库名：``<project>-<service>``。"""
    container = manifest.container
    assert container is not None
    return f"{container.projectName}-{container.serviceName}"


def prev_dir(workspace: Workspace, instance_id: str) -> Path:
    return workspace.app_docker(instance_id) / PREV_DIRNAME


def snapshot_generated(workspace: Workspace, instance_id: str) -> Path:
    """把当前生成物复制到 ``docker/.prev/``；先清空旧快照，缺失的文件记为不存在。

    BUG-835：先写临时目录再整体交换，快照中途失败不会留下半份快照；此时
    连上一轮的陈旧 ``.prev`` 一并移除——失败恢复时 :func:`restore_generated`
    返回 False 且不改动现场，杜绝把过期快照盖回 ``docker/``。
    """
    target = prev_dir(workspace, instance_id)
    staging = target.parent / f"{PREV_DIRNAME}.staging-{os.getpid()}"
    try:
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        docker_dir = workspace.app_docker(instance_id)
        for name in ALL_GENERATED_FILES:
            src = docker_dir / name
            if src.is_file():
                shutil.copy2(src, staging / name)
        shutil.rmtree(target, ignore_errors=True)
        staging.rename(target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        # 本轮快照未建立：上一轮的陈旧 .prev 不得再被恢复（BUG-835）
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target


def restore_generated(workspace: Workspace, instance_id: str) -> bool:
    """把 ``docker/.prev/`` 恢复到 ``docker/``。快照缺失返回 False 且不改动现场。

    快照中不存在的文件会从 ``docker/`` 删除，使恢复结果与快照时刻一致。
    """
    source = prev_dir(workspace, instance_id)
    if not source.is_dir():
        return False
    docker_dir = workspace.app_docker(instance_id)
    docker_dir.mkdir(parents=True, exist_ok=True)
    for name in ALL_GENERATED_FILES:
        src = source / name
        dst = docker_dir / name
        if src.is_file():
            shutil.copy2(src, dst)
        elif dst.exists():
            dst.unlink()
    return True


def tag_image(source_ref: str, target_ref: str) -> None:
    """``docker tag``；失败抛 :class:`DockerError`。"""
    try:
        result = subprocess.run(  # noqa: S603, S607 — 参数来自受控的镜像引用
            ["docker", "tag", source_ref, target_ref],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DockerError(f"docker tag {source_ref} {target_ref} 执行失败：{exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:300]
        raise DockerError(f"docker tag {source_ref} {target_ref} 失败：{detail}")


def prepare_build_first(
    workspace: Workspace,
    runtime,
    manifest: InstanceManifest,
) -> None:
    """build 之前的准备：快照生成物并保留旧镜像为 ``:prev``。

    BUG-835：快照在前、打标签在后——快照的就是「当前仍在服务的旧生成物」，
    随后打 ``:prev`` 失败而中止时，失败收尾里的恢复等价于无操作，绝不会再
    翻出上一轮的陈旧 ``.prev`` 盖掉当前 compose。无法确定旧镜像或无法打上
    ``:prev`` 标签时抛出 :class:`HostingError`，此时容器未被改动，旧版本
    保持服务。
    """
    instance_id = manifest.id
    old_image = manifest.container.imageId if manifest.container else None
    if not old_image:
        try:
            old_image = runtime.image_id(instance_id)
        except DockerError:
            old_image = None
    if not old_image:
        raise HostingError(
            f"实例 {instance_id} 无法确定当前镜像，已中止重建（旧版本仍在服务）；"
            "如需强制重建请在 manifest 设置 rebuildStrategy: stop-first",
            instance_id=instance_id,
        )
    snapshot_generated(workspace, instance_id)
    repo = image_repo(manifest)
    try:
        tag_image(old_image, f"{repo}:{PREV_TAG}")
    except DockerError as exc:
        raise HostingError(
            f"实例 {instance_id} 无法保留旧镜像，已中止重建（旧版本仍在服务）：{exc}",
            instance_id=instance_id,
        ) from exc


def restore_previous_image_tag(manifest: InstanceManifest) -> None:
    """补偿时把 ``:prev`` 重新打回 ``:latest``，让 ``compose up`` 使用旧镜像。"""
    repo = image_repo(manifest)
    tag_image(f"{repo}:{PREV_TAG}", f"{repo}:{LATEST_TAG}")


# BUG-998：``.env.local`` 是用户维护的业务环境文件，不属于生成物，但 compose
# 以 env_file 引用它。改坏后 recreate 的新 up 失败属预期，补偿 up 若仍读同一个
# 坏文件则旧服务也无法恢复。因此在每次部署/recreate **成功**后记录一份
# last-good 副本，补偿时用它覆盖现场。目录内 ``recorded`` 标记与副本分离——
# 用户删除 ``.env.local`` 后成功运行过一次，副本不存在但语义是「该实例应无
# 此文件」，恢复时必须把现场的文件删掉，而不是回退到更早的内容。
LAST_GOOD_DIRNAME = ".env-good"
_LAST_GOOD_ENV_NAME = ".env.local"
_LAST_GOOD_RECORDED = "recorded"


def _last_good_dir(workspace: Workspace, instance_id: str) -> Path:
    return workspace.app_docker(instance_id) / LAST_GOOD_DIRNAME


def business_env_changed(workspace: Workspace, instance_id: str) -> bool:
    """比较当前业务环境与恢复副本，包含新增/删除语义。预检成功后调用。"""
    current = workspace.app_env_local_path(instance_id)
    previous = _last_good_dir(workspace, instance_id) / _LAST_GOOD_ENV_NAME
    if current.is_file() != previous.is_file():
        return True
    return current.is_file() and current.read_bytes() != previous.read_bytes()


def ensure_env_rollback_ready(
    workspace: Workspace, manifest: InstanceManifest, *, use_previous: bool = False
) -> None:
    """停旧容器前确认业务环境可恢复；不能证明安全时保留旧容器。

    旧实例可用 v3 指纹证明现场文件仍与成功部署一致，此时补记 last-good。
    文件已变更（或旧指纹未覆盖业务环境）时不能把当前坏文件当成旧环境。
    ``use_previous`` 用于收敛后的决策，读取重渲染前的生成物。
    """
    iid = manifest.id
    if (_last_good_dir(workspace, iid) / _LAST_GOOD_RECORDED).is_file():
        return
    from local_webpage_access.lifecycle import (
        _RUNTIME_CONFIG_HASH_VERSION,
        _compute_runtime_config_fingerprint,
    )

    docker_dir = workspace.app_docker(iid)
    previous = prev_dir(workspace, iid)
    generated_dir = previous if use_previous and previous.is_dir() else docker_dir
    env_local = workspace.app_env_local_path(iid)
    stored = manifest.deploymentFingerprints or {}
    if (
        stored.get("runtimeConfigHashVersion") == _RUNTIME_CONFIG_HASH_VERSION
        and stored.get("runtimeConfigHash") == _compute_runtime_config_fingerprint(
            generated_dir / "compose.yaml", generated_dir / ".env", env_local
        )
    ):
        save_last_good_env(workspace, iid)
        return

    compose = generated_dir / "compose.yaml"
    references_env = compose.is_file() and ".env.local" in compose.read_text(encoding="utf-8")
    if env_local.exists() or references_env:
        raise HostingError(
            f"实例 {iid} 缺少可恢复的旧业务环境记录，已中止替换并保留旧容器；"
            "请先将 .env.local 恢复到上次成功部署的内容后重试。"
            "旧版指纹无法校验时，请在维护窗口备份并迁移旧容器的运行环境",
            instance_id=iid,
        )
    # 旧生成物没有业务环境引用，现场也没有文件：可确定旧环境为「无文件」。
    save_last_good_env(workspace, iid)


def save_last_good_env(workspace: Workspace, instance_id: str) -> None:
    """部署/recreate 成功后记录当前业务环境文件为 last-good 副本。

    先写 staging 目录再整体交换，中途失败不会留下半份记录（仿
    :func:`snapshot_generated` 的 BUG-835 防护）。副本权限固定 0600——
    业务环境可能含密钥。
    """
    target = _last_good_dir(workspace, instance_id)
    staging = target.parent / f"{LAST_GOOD_DIRNAME}.staging-{os.getpid()}"
    try:
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, mode=0o700)
        src = workspace.app_docker(instance_id) / _LAST_GOOD_ENV_NAME
        if src.is_file():
            dst = staging / _LAST_GOOD_ENV_NAME
            fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(src.read_bytes())
        (staging / _LAST_GOOD_RECORDED).write_text("ok\n", encoding="utf-8")
        shutil.rmtree(target, ignore_errors=True)
        staging.rename(target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def restore_last_good_env(workspace: Workspace, instance_id: str) -> bool:
    """补偿路径恢复 last-good 业务环境；无记录时返回 False 且不改动现场。

    有 ``recorded`` 标记时精确恢复记录时刻的状态：有副本则以 0600 写回
    ``docker/.env.local``，无副本则删除现场的 ``.env.local``（记录时刻该
    文件不存在）。
    """
    source = _last_good_dir(workspace, instance_id)
    if not (source / _LAST_GOOD_RECORDED).is_file():
        return False
    dst = workspace.app_docker(instance_id) / _LAST_GOOD_ENV_NAME
    src = source / _LAST_GOOD_ENV_NAME
    if src.is_file():
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(src.read_bytes())
    elif dst.exists():
        dst.unlink()
    return True
