"""S3-01 镜像版本留档：快照、标签、GC 保留规则、占用统计（mock 层）。

覆盖 versions.py 的契约：
- 生成物快照（apps/<id>/docker/builds/<build_id>/）可完整往返；
- GC 保留最新 imageRetention 个，**永不删除**正在使用的镜像与最近一个可用版本；
- 被 GC 的构建同时清理镜像标签与快照目录；无产物信息的行不参与；
- image_exists / 磁盘占用 / list_versions 输出形状。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from local_webpage_access import versions
from local_webpage_access.models import Status
from tests.scripts._helpers import make_container_manifest

COMPOSE_V1 = "services:\n  app:\n    image: v1\n"
COMPOSE_V2 = "services:\n  app:\n    image: v2\n"
DOCKERFILE = "FROM python:3.13-slim\n"
ENV = "PORT=8000\n"


@pytest.fixture()
def deployed(workspace, registry):
    """容器实例（running）+ docker/ 下当前生成物。"""
    workspace.ensure_app_dirs("demo")
    manifest = make_container_manifest("demo", status=Status.RUNNING)
    manifest.container.containerId = "cur-container"
    manifest.container.imageId = "img-current"
    manifest.container.hostPort = 21001
    manifest.save(workspace.app_manifest_path("demo"))
    registry.upsert_from_manifest(manifest)
    docker_dir = workspace.app_docker("demo")
    docker_dir.mkdir(parents=True, exist_ok=True)
    (docker_dir / "compose.yaml").write_text(COMPOSE_V2, encoding="utf-8")
    (docker_dir / "Dockerfile").write_text(DOCKERFILE, encoding="utf-8")
    (docker_dir / ".env").write_text(ENV, encoding="utf-8")
    return manifest


def _mk_build(registry, instance_id: str, *, status: str = "success",
              image_id: str | None = "img", with_artifacts: bool = True) -> int:
    """登记一条构建行（可选产物信息），返回 build_id。"""
    build_id = registry.add_build(instance_id, status="running")
    registry.finish_build(build_id, status=status)
    if with_artifacts and status == "success":
        registry.record_build_artifacts(
            build_id,
            image_ref=f"lwa-demo-app:b{build_id}",
            image_id=image_id,
            source_hash=f"hash-{build_id}",
            config_snapshot=str(Path(f"/snap/{build_id}")),
        )
    return build_id


# ---- 快照往返 -----------------------------------------------------------------


def test_snapshot_and_restore_roundtrip(workspace, deployed) -> None:
    snap = versions.snapshot_config(workspace, "demo", 7)
    assert snap is not None and snap.is_dir()
    assert (snap / "compose.yaml").read_text(encoding="utf-8") == COMPOSE_V2

    # 改动现场（含删除一个生成物、增加一个无关文件）后恢复
    docker_dir = workspace.app_docker("demo")
    (docker_dir / "compose.yaml").write_text("# tampered\n", encoding="utf-8")
    (docker_dir / ".env").unlink()
    (docker_dir / "unrelated.txt").write_text("keep\n", encoding="utf-8")

    assert versions.restore_config_snapshot(workspace, "demo", 7) is True
    assert (docker_dir / "compose.yaml").read_text(encoding="utf-8") == COMPOSE_V2
    assert (docker_dir / ".env").read_text(encoding="utf-8") == ENV
    assert (docker_dir / "Dockerfile").read_text(encoding="utf-8") == DOCKERFILE
    # 快照之外的文件不动
    assert (docker_dir / "unrelated.txt").read_text(encoding="utf-8") == "keep\n"


def test_restore_missing_snapshot_is_noop(workspace, deployed) -> None:
    assert versions.restore_config_snapshot(workspace, "demo", 999) is False
    assert (workspace.app_docker("demo") / "compose.yaml").read_text(encoding="utf-8") == COMPOSE_V2


# ---- GC 保留规则 ---------------------------------------------------------------


@pytest.fixture()
def rm_calls(monkeypatch):
    """记录 GC 触达的 docker image rm 调用。"""
    calls: list[str] = []
    monkeypatch.setattr(versions, "_image_rm", lambda ref: calls.append(ref))
    return calls


def _mk_snapshot_dir(workspace, registry, build_id: int) -> Path:
    snap = versions.build_snapshot_dir(workspace, "demo", build_id)
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "compose.yaml").write_text(COMPOSE_V1, encoding="utf-8")
    # 把真实快照路径回写到 builds 行（GC 按行里的 config_snapshot 清理）
    registry.record_build_artifacts(
        build_id, config_snapshot=str(snap),
        image_ref=f"lwa-demo-app:b{build_id}", image_id=f"img-{build_id}",
        source_hash=f"hash-{build_id}",
    )
    return snap


def test_gc_keeps_retention_count_and_never_removes_in_use(workspace, registry, config,
                                                           deployed, rm_calls) -> None:
    ids = [_mk_build(registry, "demo", image_id=f"img-{i}") for i in range(1, 6)]
    snaps = {bid: _mk_snapshot_dir(workspace, registry, bid) for bid in ids}
    # 当前在用 img-5（最新成功）——保留 3 个：img-5/img-4/img-3
    result = versions.gc_build_artifacts(workspace, config, registry,
                                         "demo", in_use_image_id="img-5")

    assert set(result["removed"]) == {ids[0], ids[1]}
    # GC 按新→旧遍历：先删次旧 b2，再删最旧 b1
    assert rm_calls == [f"lwa-demo-app:b{ids[1]}", f"lwa-demo-app:b{ids[0]}"]
    assert not snaps[ids[0]].exists() and not snaps[ids[1]].exists()
    assert snaps[ids[2]].exists() and snaps[ids[4]].exists()


def test_gc_never_removes_previous_good_when_not_in_use(workspace, registry, config,
                                                        deployed, rm_calls) -> None:
    """in-use 是手动指定的旁支镜像时，最近一个成功版本仍受保护。"""
    ids = [_mk_build(registry, "demo", image_id=f"img-{i}") for i in range(1, 5)]
    _mk_snapshot_dir(workspace, registry, ids[3])
    result = versions.gc_build_artifacts(workspace, config, registry,
                                         "demo", in_use_image_id="img-side")
    # retention=3：img-4（上一可用，受保护）/img-3/img-2 保留，img-1 删除
    assert result["removed"] == [ids[0]]
    assert rm_calls == [f"lwa-demo-app:b{ids[0]}"]


def test_gc_failed_builds_and_rows_without_artifacts_are_skipped(workspace, registry, config,
                                                                 deployed, rm_calls) -> None:
    ok1 = _mk_build(registry, "demo", image_id="img-a")
    failed = _mk_build(registry, "demo", status="failed", image_id="img-f")
    registry.record_build_artifacts(
        failed, image_ref=f"lwa-demo-app:b{failed}", image_id="img-f",
        source_hash=None, config_snapshot=None,
    )
    bare = registry.add_build("demo", status="success")  # 无产物信息（S3-01 之前的老行）
    registry.finish_build(bare, status="success")
    _mk_snapshot_dir(workspace, registry, ok1)
    result = versions.gc_build_artifacts(workspace, config, registry,
                                         "demo", in_use_image_id="img-a")
    assert result["removed"] == []
    assert rm_calls == []
    # 行本身不被改动
    rows = {r["id"]: r for r in registry.list_builds("demo")}
    assert rows[failed]["image_ref"] == f"lwa-demo-app:b{failed}"
    assert rows[bare]["image_ref"] is None


def test_gc_uses_config_retention(workspace, registry, config, deployed, rm_calls) -> None:
    # config fixture 默认 imageRetention=3；此处显式造 2 校验配置生效
    from local_webpage_access.config import Config, PortPool

    cfg = Config(staticGateway="builtin", portPool=PortPool(start=21000, end=21050),
                 imageRetention=2)
    ids = [_mk_build(registry, "demo", image_id=f"img-{i}") for i in range(1, 5)]
    result = versions.gc_build_artifacts(workspace, cfg, registry, "demo",
                                         in_use_image_id="img-4")
    assert result["removed"] == [ids[1], ids[0]]
    assert rm_calls == [f"lwa-demo-app:b{ids[1]}", f"lwa-demo-app:b{ids[0]}"]


def test_gc_falls_back_to_registry_container_image(workspace, registry, config, deployed, rm_calls) -> None:
    """未显式传 in_use 时，用 containers 子表登记的 image_id。"""
    [_mk_build(registry, "demo", image_id=f"img-{i}") for i in range(1, 4)]
    row = registry.get_container("demo") or {}
    assert row.get("image_id") == "img-current"
    result = versions.gc_build_artifacts(workspace, config, registry, "demo")
    # img-current 不在构建行里：保护最近成功 img-3，retention=3 → 全保留
    assert result["removed"] == []


# ---- docker 查询辅助（mock subprocess）------------------------------------------


def _cp(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["docker"], returncode, stdout, "")


def test_resolve_repo_image_id(monkeypatch) -> None:
    monkeypatch.setattr(versions, "_docker",
                        lambda args, timeout=60: _cp("sha256:abc123\n"))
    assert versions.resolve_repo_image_id("lwa-demo-app") == "abc123"
    monkeypatch.setattr(versions, "_docker",
                        lambda args, timeout=60: _cp("", returncode=1))
    assert versions.resolve_repo_image_id("lwa-demo-app") is None


def test_image_exists_and_disk_usage(monkeypatch) -> None:
    payload = json.dumps([{"Size": 100}, {"Size": 250}])
    seen: list[list[str]] = []

    def _fake(args, timeout=60):
        seen.append(args)
        if args[:2] == ["image", "inspect"] and len(args) == 3:
            return _cp("", returncode=1)  # image_exists 单查路径
        return _cp(payload)

    monkeypatch.setattr(versions, "_docker", _fake)
    assert versions.image_exists("lwa-demo-app:b1") is False
    assert versions.image_disk_usage_bytes(["lwa-demo-app:b1", "lwa-demo-app:b2"]) == 350


def test_list_versions_reports_presence_and_fields(workspace, registry, deployed, monkeypatch) -> None:
    b1 = _mk_build(registry, "demo", image_id="img-old")
    registry.add_build("demo", status="running")  # 一条进行中的行，无产物
    present: set[str] = set()

    def _fake(args, timeout=60):
        ref = args[-1]
        return _cp("", returncode=0 if ref in present else 1)

    monkeypatch.setattr(versions, "_docker", _fake)
    rows = versions.list_versions(registry, "demo")
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == b1
    assert row["imageRef"] == f"lwa-demo-app:b{b1}"
    assert row["imageId"] == "img-old"
    assert row["sourceHash"] == f"hash-{b1}"
    assert row["imagePresent"] is False
    present.add(f"lwa-demo-app:b{b1}")
    assert versions.list_versions(registry, "demo")[0]["imagePresent"] is True


def test_disk_usage_counts_existing_images_when_some_refs_were_gced(monkeypatch) -> None:
    """GC 过的旧引用让 ``docker image inspect`` 以非零退出，但 stdout 仍含存在镜像的 JSON：
    占用统计必须累加它们，不能因返回码非零整组归零。"""
    payload = json.dumps([{"Size": 100}, {"Size": 250}])
    monkeypatch.setattr(versions, "_docker", lambda args, timeout=60: _cp(payload, returncode=1))
    assert versions.image_disk_usage_bytes(["gone:b1", "lwa-demo-app:b2", "lwa-demo-app:b3"]) == 350


def test_disk_usage_is_zero_when_nothing_parsable(monkeypatch) -> None:
    for out in ("", "not json", json.dumps({"Size": 5})):
        monkeypatch.setattr(versions, "_docker", lambda args, timeout=60, _o=out: _cp(_o, 1))
        assert versions.image_disk_usage_bytes(["gone:b1"]) == 0
    assert versions.image_disk_usage_bytes([]) == 0


def test_image_inspect_context_not_found_is_unavailable(monkeypatch) -> None:
    """DOCKER_CONTEXT 错误的 context not found 不是镜像缺失。"""

    def _fake(args, timeout=60):
        return subprocess.CompletedProcess(
            args, 1, "", 'error during connect: context "nope": context not found\n'
        )

    monkeypatch.setattr(versions, "_docker", _fake)
    assert versions.image_inspect_status("lwa-demo-app:b1") == "unavailable"


# ---- purge 引用枚举（BUG-821）----------------------------------------------------


def test_iter_image_refs_falls_back_to_containers_row(workspace, registry, deployed) -> None:
    """BUG-821：manifest 缺失且无任何留档行时，从 containers 表兜底推导并纳入 image_id。"""
    # deployed fixture：containers 行有 compose_project/service_name/image_id，builds 无产物行
    refs = versions.iter_instance_image_refs(registry, "demo", manifest=None)

    assert refs == [
        "lwa-demo-app:latest",
        "lwa-demo-app:prev",
        "img-current",  # containers.image_id 置于末尾：先摘标签再按 ID 删
    ]


def test_iter_image_refs_prefers_manifest_and_build_refs(workspace, registry, deployed) -> None:
    """manifest 在场时以 manifest 仓库名为准；image_id 不重复纳入（remove 路径单独清）。"""
    from tests.scripts._helpers import make_container_manifest

    manifest = make_container_manifest("demo")
    bid = _mk_build(registry, "demo", image_id="img-9")
    refs = versions.iter_instance_image_refs(registry, "demo", manifest=manifest)

    assert f"lwa-demo-app:b{bid}" in refs
    assert "lwa-demo-app:latest" in refs and "lwa-demo-app:prev" in refs
    assert "img-current" not in refs


def test_image_inspect_no_such_image_is_missing(monkeypatch) -> None:
    def _fake(args, timeout=60):
        return subprocess.CompletedProcess(args, 1, "", "Error: No such image: lwa-demo-app:b1\n")

    monkeypatch.setattr(versions, "_docker", _fake)
    assert versions.image_inspect_status("lwa-demo-app:b1") == "missing"


def test_finalize_build_artifacts_failure_removes_orphan_tag(
    workspace, registry, deployed, monkeypatch
) -> None:
    """快照失败时撤回已打上的 b 标签，并清掉残缺快照目录。"""
    from local_webpage_access import hosting

    build_id = registry.add_build("demo", status="running")
    registry.finish_build(build_id, status="success")
    removed: list[str] = []
    monkeypatch.setattr(versions, "resolve_repo_image_id", lambda _repo: "img-x")
    monkeypatch.setattr(versions, "tag_build_image", lambda *_a, **_k: None)
    monkeypatch.setattr(versions, "_image_rm", lambda ref: removed.append(ref))

    def _snap(*_a, **_k):
        snap = versions.build_snapshot_dir(workspace, "demo", build_id)
        snap.mkdir(parents=True, exist_ok=True)
        (snap / "compose.yaml").write_text("partial\n", encoding="utf-8")
        raise OSError("disk full")

    monkeypatch.setattr(versions, "snapshot_config", _snap)
    hosting._finalize_build_artifacts(workspace, registry, "demo", deployed, build_id)
    assert removed == [f"lwa-demo-app:b{build_id}"]
    assert not versions.build_snapshot_dir(workspace, "demo", build_id).exists()
    row = next(r for r in registry.list_builds("demo") if int(r["id"]) == build_id)
    assert row["image_ref"] is None


@pytest.mark.parametrize("exc", [OSError("disk full"), RuntimeError("snapshot boom")])
def test_finalize_build_artifacts_snapshot_failure_does_not_block_deploy(
    workspace, registry, deployed, monkeypatch, exc
) -> None:
    """生成物快照失败只告警，不得阻断本次部署（镜像标签已打上也不回滚）。"""
    from local_webpage_access import hosting

    build_id = registry.add_build("demo", status="running")
    registry.finish_build(build_id, status="success")
    tagged: list[tuple[str, str]] = []
    recorded: list[int] = []
    monkeypatch.setattr(versions, "resolve_repo_image_id", lambda _repo: "img-x")
    monkeypatch.setattr(
        versions, "tag_build_image", lambda image_id, ref: tagged.append((image_id, ref))
    )
    monkeypatch.setattr(
        versions,
        "snapshot_config",
        lambda *_a, **_k: (_ for _ in ()).throw(exc),
    )
    monkeypatch.setattr(
        registry, "record_build_artifacts", lambda *_a, **_k: recorded.append(1)
    )
    hosting._finalize_build_artifacts(workspace, registry, "demo", deployed, build_id)
    assert tagged == [("img-x", f"lwa-demo-app:b{build_id}")]
    assert recorded == []


def test_gc_does_not_keep_every_row_sharing_cached_image_id(
    workspace, registry, config, deployed, rm_calls
) -> None:
    """构建缓存命中时多次构建同一 image_id；imageRetention=3 只保留 3 个版本标签。"""
    ids = [_mk_build(registry, "demo", image_id="img-cached") for _ in range(7)]
    snaps: dict[int, Path] = {}
    for bid in ids:
        snap = versions.build_snapshot_dir(workspace, "demo", bid)
        snap.mkdir(parents=True, exist_ok=True)
        (snap / "compose.yaml").write_text(COMPOSE_V1, encoding="utf-8")
        snaps[bid] = snap
    result = versions.gc_build_artifacts(
        workspace, config, registry, "demo", in_use_image_id="img-cached"
    )
    assert set(result["kept"]) == set(ids[-3:])
    assert set(result["removed"]) == set(ids[:4])
    for bid in ids[:4]:
        assert not snaps[bid].exists()
    for bid in ids[-3:]:
        assert snaps[bid].exists()


def test_gc_does_not_clear_artifacts_when_docker_unavailable(
    workspace, registry, config, deployed, monkeypatch
) -> None:
    """Docker daemon 不可达时不得把产物列清空，否则后续 GC 无法重试。"""
    from local_webpage_access.errors import DockerError

    ids = [_mk_build(registry, "demo", image_id=f"img-{i}") for i in range(1, 5)]
    _mk_snapshot_dir(workspace, registry, ids[0])

    def _boom(*_a, **_k):
        raise DockerError("Cannot connect to the Docker daemon")

    monkeypatch.setattr(versions, "_docker", _boom)
    result = versions.gc_build_artifacts(
        workspace, config, registry, "demo", in_use_image_id="img-4"
    )
    assert ids[0] in result["removed"]
    rows = {r["id"]: r for r in registry.list_builds("demo")}
    assert rows[ids[0]]["image_ref"] == f"lwa-demo-app:b{ids[0]}"


def test_gc_cleans_snapshot_under_current_workspace_when_stored_path_is_stale(
    workspace, registry, config, deployed, rm_calls
) -> None:
    """工作区迁移后 config_snapshot 仍指向旧根时，GC 须按当前工作区推导路径清理。"""
    bid = _mk_build(registry, "demo", image_id="img-old")
    for _ in range(3):
        _mk_build(registry, "demo", image_id=f"img-keep-{_}")
    snap = versions.build_snapshot_dir(workspace, "demo", bid)
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "compose.yaml").write_text(COMPOSE_V1, encoding="utf-8")
    registry.record_build_artifacts(
        bid,
        image_ref=f"lwa-demo-app:b{bid}",
        image_id="img-old",
        source_hash="hash-stale",
        config_snapshot="/old-ws/apps/demo/docker/builds/" + str(bid),
    )
    result = versions.gc_build_artifacts(
        workspace, config, registry, "demo", in_use_image_id="img-keep-2"
    )
    assert bid in result["removed"]
    assert not snap.exists()
