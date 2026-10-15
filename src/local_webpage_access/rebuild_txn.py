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
        for name in GENERATED_FILES:
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
    for name in GENERATED_FILES:
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
