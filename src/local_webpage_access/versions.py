"""S3-01 镜像版本留档：构建产物登记、生成物快照与 GC（DEV-157 / issue #51 远期 / D8）。

部署验证通过后，host_container 会把新镜像打上 ``lwa-<id>-app:b<build_id>`` 标签、
把三件生成物（compose.yaml / Dockerfile / .env）与当时的迁移命令快照到
``apps/<id>/docker/builds/<build_id>/``，并把镜像引用、镜像 ID、源码哈希、
快照路径回写到 builds 表（schema v7）。镜像构建成功但启动或必选探针失败的
版本不写入这些产物（失败路径若已写入则撤销），因此不能被当作可回滚版本。

部署成功后按全局配置 ``imageRetention``（默认 3）执行 GC：只清理超出保留数的
历史版本，**永远不删除**正在使用的镜像与最近一个可用版本（二者计入保留数）。
GC 删除镜像标签（``docker image rm <ref>``，共享层不受影响）并清理对应快照目录；
任何 GC 失败只告警，不阻断部署。
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from local_webpage_access import rebuild_txn
from local_webpage_access.errors import DockerError
from local_webpage_access.paths import Workspace

GENERATED_FILES = rebuild_txn.GENERATED_FILES

#: 留档版本当时的 entry.start/preStart/buildHooks 原文，供回滚确认迁移风险。
MIGRATION_COMMANDS_FILE = "migration-commands.txt"


def normalize_image_id(value: Any) -> str | None:
    """镜像 ID 归一化（去 ``sha256:`` 前缀）；空值返回 None。

    ``docker inspect`` 的 ``.Image`` / ``{{.Id}}`` 带 sha256: 前缀（manifest 与
    containers 行存原样值），builds 行的 image_id 存的是去前缀值——两侧任何
    相等比较都必须先归一化（BUG-800/801）。
    """
    if not value:
        return None
    return str(value).removeprefix("sha256:") or None

#: 版本标签格式：``<repo>:b<build_id>``
def build_image_ref(repo: str, build_id: int) -> str:
    return f"{repo}:b{build_id}"


def tag_build_image(image_id: str, ref: str) -> None:
    """给留档镜像打标签；经 rebuild_txn.tag_image 包装，便于单测替换。"""
    rebuild_txn.tag_image(image_id, ref)


def build_snapshot_dir(workspace: Workspace, instance_id: str, build_id: int) -> Path:
    """生成物快照目录：``apps/<id>/docker/builds/<build_id>/``。"""
    return workspace.app_docker(instance_id) / "builds" / str(build_id)


def snapshot_config(workspace: Workspace, instance_id: str, build_id: int) -> Path:
    """把当前三件生成物复制到快照目录（先清空旧快照）；返回快照目录。"""
    target = build_snapshot_dir(workspace, instance_id, build_id)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    docker_dir = workspace.app_docker(instance_id)
    for name in rebuild_txn.ALL_GENERATED_FILES:
        src = docker_dir / name
        if src.is_file():
            shutil.copy2(src, target / name)
    return target


def is_rollback_candidate(row: dict[str, Any]) -> bool:
    """该构建行是否具备可回滚资格。

    资格只在部署验证通过、产物列写入之后成立。``status=success`` 仅表示镜像
    构建成功；没有 ``image_ref`` 的行（验证失败，或失败后已撤销留档）既不能
    被选为回滚目标，也不能被 GC 当成「上一可用版本」保护。
    """
    return row.get("status") == "success" and bool(row.get("image_ref"))


def write_migration_archive(
    workspace: Workspace,
    instance_id: str,
    build_id: int,
    commands: str,
) -> None:
    """把该版本构建时的迁移相关命令原文写入快照目录（可为空串）。"""
    target = build_snapshot_dir(workspace, instance_id, build_id)
    target.mkdir(parents=True, exist_ok=True)
    (target / MIGRATION_COMMANDS_FILE).write_text(commands, encoding="utf-8")


def read_migration_archive(
    workspace: Workspace,
    instance_id: str,
    build_id: int,
) -> tuple[str, str] | None:
    """读取目标版本留档的迁移信息。

    返回 ``(text, source)``：``source="commands"`` 是显式留档的命令原文；
    ``source="dockerfile"`` 是旧留档没有命令文件时的 Dockerfile 回退。
    两者都没有时返回 None。
    """
    snap = build_snapshot_dir(workspace, instance_id, build_id)
    sidecar = snap / MIGRATION_COMMANDS_FILE
    if sidecar.is_file():
        return sidecar.read_text(encoding="utf-8"), "commands"
    dockerfile = snap / "Dockerfile"
    if dockerfile.is_file():
        return dockerfile.read_text(encoding="utf-8"), "dockerfile"
    return None


def snapshot_complete(workspace: Workspace, instance_id: str, build_id: int) -> bool:
    """快照目录存在且三件生成物齐全（回滚要求快照可完整恢复现场）。"""
    snap = build_snapshot_dir(workspace, instance_id, build_id)
    if not snap.is_dir() or not all((snap / name).is_file() for name in GENERATED_FILES):
        return False
    try:
        dockerfile = (snap / "Dockerfile").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    return all(
        name not in dockerfile or (snap / name).is_file()
        for name in rebuild_txn.OPTIONAL_GENERATED_FILES
    )


def restore_config_snapshot(workspace: Workspace, instance_id: str, build_id: int) -> bool:
    """把 ``builds/<build_id>/`` 快照恢复到 ``docker/``；快照缺失返回 False 且不动现场。

    与 rebuild_txn.restore_generated 同语义：快照中不存在的生成物会从 ``docker/``
    删除，使恢复结果与快照时刻一致。
    """
    source = build_snapshot_dir(workspace, instance_id, build_id)
    if not source.is_dir():
        return False
    docker_dir = workspace.app_docker(instance_id)
    docker_dir.mkdir(parents=True, exist_ok=True)
    for name in rebuild_txn.ALL_GENERATED_FILES:
        src = source / name
        dst = docker_dir / name
        if src.is_file():
            shutil.copy2(src, dst)
        elif dst.exists():
            dst.unlink()
    return True


# ---- docker 查询原语 ----------------------------------------------------------


def _docker(args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(  # noqa: S603 — 参数均为受控的镜像引用/固定子命令
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DockerError(f"docker {' '.join(args)} 执行失败：{exc}") from exc


def resolve_repo_image_id(repo: str) -> str | None:
    """``docker image inspect <repo>:latest`` 的镜像 ID（去 sha256: 前缀）；失败返回 None。"""
    try:
        result = _docker(["image", "inspect", "-f", "{{.Id}}", repo])
    except DockerError:
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip().removeprefix("sha256:") or None


def image_inspect_status(ref: str) -> str:
    """查询镜像标签是否存在。

    返回 ``present`` / ``missing`` / ``unavailable``。Docker daemon 不可达、
    权限不足或 inspect 因非「镜像不存在」原因失败时返回 ``unavailable``，
    不得当成缺失去清空产物列。
    """
    try:
        result = _docker(["image", "inspect", ref])
    except DockerError:
        return "unavailable"
    if result.returncode == 0:
        return "present"
    blob = f"{result.stderr or ''}{result.stdout or ''}".lower()
    # 只认镜像本身不存在。「context not found」是客户端上下文错误，不能当缺失。
    if "no such image" in blob or "no such object" in blob:
        return "missing"
    return "unavailable"


def image_exists(ref: str) -> bool:
    return image_inspect_status(ref) == "present"


def image_disk_usage_bytes(refs: list[str]) -> int:
    """给定镜像引用合计占用字节数；不存在/查询失败的引用按 0 计。

    ``docker image inspect`` 只要参数中有一个引用不存在（如已被 GC）就以非零退出，
    但 stdout 仍输出其余存在镜像的 JSON。因此不以返回码为准，直接解析 stdout，
    否则一个旧镜像被清理就会让整组占用统计归零。
    """
    if not refs:
        return 0
    try:
        result = _docker(["image", "inspect", *refs])
    except DockerError:
        return 0
    text = (result.stdout or "").strip()
    if not text:
        return 0
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, list):
        return 0
    return sum(int(item.get("Size") or 0) for item in payload if isinstance(item, dict))


def _image_rm(ref: str) -> None:
    """GC 用：删除镜像标签（共享层在有其他引用时保留）；调用方负责容错。"""
    _docker(["image", "rm", ref], timeout=120)


# ---- GC ------------------------------------------------------------------------


def gc_build_artifacts(
    workspace: Workspace,
    config: Any,
    registry: Any,
    instance_id: str,
    *,
    in_use_image_id: str | None = None,
) -> dict[str, list[int]]:
    """按 ``imageRetention`` 清理超保留期的版本镜像标签与快照目录。

    保护规则（D8 之前的硬约束）：正在使用的**构建**与最近一个不同镜像的成功
    构建永不删除，且都计入保留数。构建缓存命中导致多行共享同一 ``image_id``
    时，只保护必要版本标签，不把同镜像的全部历史当受保护对象。无产物信息
    （image_ref 为空）的行不参与 GC。

    返回 ``{"kept": [build_id...], "removed": [build_id...]}``；删除失败只告警。
    """
    from local_webpage_access.logging import get_logger

    log = get_logger("versions")
    retention = max(1, int(getattr(config, "imageRetention", 3) or 3))
    in_use_image_id = normalize_image_id(in_use_image_id)
    if in_use_image_id is None:
        container_row = registry.get_container(instance_id) or {}
        in_use_image_id = normalize_image_id(container_row.get("image_id"))
    rows = [r for r in registry.list_builds(instance_id, limit=500) if r.get("image_ref")]
    # 上一可用版本只从「验证通过后留档」的行里挑，与回滚选版使用同一资格。
    success_rows = [r for r in rows if is_rollback_candidate(r)]
    # BUG-800：上一可用版本 = 最新的、镜像**不同于在用镜像**的成功构建——
    # 部署成功后调用时 success_rows[0] 就是刚构建的当前版本，与 in_use 相同，
    # 不能提供第二重保护（imageRetention=1 时上一可用版本会被 GC，回滚无目标）。
    in_use_build_id = next(
        (
            int(row["id"])
            for row in success_rows
            if normalize_image_id(row.get("image_id")) == in_use_image_id
        ),
        None,
    )
    prev_good_build_id = next(
        (
            int(row["id"])
            for row in success_rows
            if (image := normalize_image_id(row.get("image_id"))) != in_use_image_id
            and image is not None
        ),
        None,
    )
    protected_ids = {i for i in (in_use_build_id, prev_good_build_id) if i}

    kept: list[int] = []
    removed: list[int] = []
    kept_count = 0
    for row in rows:  # list_builds 按 id 降序：新的在前
        build_id = int(row["id"])
        if kept_count < retention or build_id in protected_ids:
            kept.append(build_id)
            kept_count += 1
            continue
        removed.append(build_id)
        ref = str(row["image_ref"])
        try:
            _image_rm(ref)
        except Exception:  # noqa: BLE001 — GC 失败不阻断部署
            log.warning("实例 %s 构建 #%s GC 删除镜像 %s 失败",
                        instance_id, row["id"], ref)
        status = image_inspect_status(ref)
        snap_paths = {build_snapshot_dir(workspace, instance_id, build_id)}
        stored = row.get("config_snapshot")
        if stored:
            snap_paths.add(Path(stored))
        for snap in snap_paths:
            shutil.rmtree(snap, ignore_errors=True)
        if status == "unavailable":
            log.warning(
                "实例 %s 构建 #%s：Docker 查询失败，保留产物登记以便下轮 GC 重试",
                instance_id, build_id,
            )
            continue
        # 仅在确认该版本标签已不存在后才置空产物列；daemon 故障不得当缺失。
        if status == "missing":
            with contextlib.suppress(Exception):
                registry.clear_build_artifacts(build_id)
    if removed:
        log.info("实例 %s GC：保留 %d 个版本，清理 %d 个", instance_id, len(kept), len(removed))
    return {"kept": kept, "removed": removed}


# ---- 列表 / 占用 ----------------------------------------------------------------


def list_versions(registry: Any, instance_id: str) -> list[dict[str, Any]]:
    """版本列表（新→旧）：构建号、时间、源码哈希、镜像是否仍在、构建结果。"""
    versions_out: list[dict[str, Any]] = []
    for row in registry.list_builds(instance_id, limit=100):
        if not row.get("image_ref"):
            continue
        versions_out.append(
            {
                "id": int(row["id"]),
                "startedAt": row.get("started_at"),
                "finishedAt": row.get("finished_at"),
                "status": row.get("status"),
                "errorSummary": row.get("error_summary"),
                "imageRef": row.get("image_ref"),
                "imageId": row.get("image_id"),
                "sourceHash": row.get("source_hash"),
                "configSnapshot": row.get("config_snapshot"),
                "imagePresent": image_exists(str(row["image_ref"])),
            }
        )
    return versions_out


def _repo_of_ref(ref: str) -> str | None:
    if ":" not in ref:
        return None
    repo, _tag = ref.rsplit(":", 1)
    return repo or None


def iter_instance_image_refs(
    registry: Any,
    instance_id: str,
    manifest: Any | None = None,
) -> list[str]:
    """实例留档标签（``b<build_id>``）、``:latest`` 与 build-first 的 ``:prev``。

    manifest 缺失时从已登记的 ``image_ref`` 推导仓库名，避免 purge 漏掉 prev。
    二者皆无（旧版升级实例 / 无留档实例）时从 containers 表兜底推导
    ``compose_project-service_name`` 仓库名，并把 ``image_id`` 一并纳入——
    这是 purge 删除登记前该实例镜像的最后已知引用（BUG-821）。``image_id``
    放在末尾：先摘标签再按 ID 删，避免多标签引用导致 rmi 失败。
    """
    refs: list[str] = []
    seen: set[str] = set()
    repos: list[str] = []

    def _add(ref: str | None) -> None:
        if not ref or ref in seen:
            return
        seen.add(ref)
        refs.append(ref)

    for row in registry.list_builds(instance_id, limit=500):
        ref = row.get("image_ref")
        if not ref:
            continue
        ref = str(ref)
        _add(ref)
        repo = _repo_of_ref(ref)
        if repo and repo not in repos:
            repos.append(repo)
    container_row = registry.get_container(instance_id) or {}
    if manifest is not None and getattr(manifest, "container", None) is not None:
        repo = rebuild_txn.image_repo(manifest)
        if repo not in repos:
            repos.append(repo)
    elif container_row.get("compose_project") and container_row.get("service_name"):
        repo = f"{container_row['compose_project']}-{container_row['service_name']}"
        if repo not in repos:
            repos.append(repo)
    for repo in repos:
        _add(f"{repo}:{rebuild_txn.LATEST_TAG}")
        _add(f"{repo}:{rebuild_txn.PREV_TAG}")
    if manifest is None:
        # manifest 缺失时 containers.image_id 是唯一身份引用（manifest 在场时由
        # remove 路径在标签之后单独清理，不重复纳入）
        _add(str(container_row["image_id"]) if container_row.get("image_id") else None)
    return refs


def retained_disk_usage_bytes(registry: Any, instance_id: str) -> tuple[int, int]:
    """返回 (有产物信息的构建行数, 这些镜像引用合计占用字节数)。"""
    refs = [
        str(row["image_ref"])
        for row in registry.list_builds(instance_id, limit=100)
        if row.get("image_ref")
    ]
    return len(refs), image_disk_usage_bytes(refs)


__all__ = [
    "build_image_ref",
    "build_snapshot_dir",
    "normalize_image_id",
    "is_rollback_candidate",
    "snapshot_config",
    "snapshot_complete",
    "write_migration_archive",
    "read_migration_archive",
    "restore_config_snapshot",
    "tag_build_image",
    "resolve_repo_image_id",
    "image_exists",
    "image_inspect_status",
    "image_disk_usage_bytes",
    "gc_build_artifacts",
    "iter_instance_image_refs",
    "list_versions",
    "retained_disk_usage_bytes",
]
