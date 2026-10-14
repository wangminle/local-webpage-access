"""BUG-787 回归：import --update 重建 manifest 必须保留容器身份与运维态。

S2-11 演练⑧实案（patent-disclosure-workflow-v1）：update 清空 containerId/
imageId 与 hostPort 登记 → 随后 rebuild 退化为「首次部署」路径（旧容器被
compose down、无 docker/.prev 快照与 :prev 标签可回退），构建失败无任何补偿
→ 在用服务中断（别名 502）。

覆盖：
- update 后 containerId/imageId/hostPort、registry 端口登记、
  deploymentFingerprints / lastOperation 保留；
- update 后 rebuild 走 S2-03 先建后换（build-first），而非首次部署路径；
- update 后 restart/start 的 S2-04 指纹决策为 full_rebuild（源码已换不误判）；
- 缺指纹基线的旧实例 update 后补记基线，不因身份保留而假绿；
- pending 实例不被「源指纹未变」短路（zip / folder 两条更新路径）。
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from local_webpage_access import hosting, lifecycle
from local_webpage_access.importer import Importer
from local_webpage_access.models import DesiredState, InstanceManifest, Status
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry


def _make_zip(zip_path: Path, files: dict[str, str]) -> Path:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for member, content in files.items():
            zf.writestr(member, content)
    return zip_path


def _container_zip(path: Path, app_body: str) -> Path:
    return _make_zip(
        path,
        {"requirements.txt": "fastapi\nuvicorn\n", "main.py": app_body},
    )


@pytest.fixture()
def importer(workspace: Workspace, config, registry: Registry) -> Importer:
    return Importer(workspace, config, registry)


def _seed_deployed_container(
    workspace: Workspace,
    registry: Registry,
    iid: str,
    *,
    with_fingerprints: bool = True,
) -> dict:
    """把已导入的容器实例伪装成「已部署且在跑」：身份/端口/生成物/基线落库。"""
    mpath = workspace.app_manifest_path(iid)
    m = InstanceManifest.load(mpath)
    assert m.container is not None
    m.container.containerId = "old-container-id"
    m.container.imageId = "old-image"
    m.container.hostPort = 21001
    m.container.composePath = str(workspace.app_compose_path(iid))
    m.container.dockerfilePath = str(workspace.app_dockerfile_path(iid))
    docker_dir = workspace.app_docker(iid)
    docker_dir.mkdir(parents=True, exist_ok=True)
    workspace.app_compose_path(iid).write_text("services:\n  app:\n    image: old\n", encoding="utf-8")
    workspace.app_dockerfile_path(iid).write_text("FROM python:3.12\n", encoding="utf-8")
    workspace.app_env_path(iid).write_text("PORT=8000\n", encoding="utf-8")
    m.desiredState = DesiredState.RUNNING
    m.lastOperation = {"kind": "deploy", "status": "succeeded", "buildId": 1}
    baseline: dict | None = None
    if with_fingerprints:
        baseline = lifecycle._compute_deployment_fingerprints(workspace, m)
        m.deploymentFingerprints = baseline
    m.save(mpath)
    registry.upsert_from_manifest(m)
    return {"manifest_path": mpath, "baseline": baseline}


def test_update_preserves_container_identity_and_ops_state(
    importer: Importer,
    workspace: Workspace,
    registry: Registry,
    tmp_path: Path,
) -> None:
    """update 后 containerId/imageId/hostPort/端口登记/指纹基线/lastOperation 全保留。"""
    iid = importer.import_zip(_container_zip(tmp_path / "api.zip", "app=1\n")).instance_id
    seeded = _seed_deployed_container(workspace, registry, iid)
    baseline = seeded["baseline"]
    assert baseline is not None

    result = importer.update_zip(_container_zip(tmp_path / "api-v2.zip", "app=2\n"), iid)
    assert result.needs_rebuild is True

    reloaded = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert reloaded.container is not None
    assert reloaded.container.containerId == "old-container-id"
    assert reloaded.container.imageId == "old-image"
    assert reloaded.container.hostPort == 21001
    assert reloaded.deploymentFingerprints == baseline
    assert reloaded.lastOperation == {"kind": "deploy", "status": "succeeded", "buildId": 1}

    row = registry.get_container(iid)
    assert row is not None
    assert row.get("container_id") == "old-container-id"
    assert row.get("image_id") == "old-image"
    assert row.get("host_port") == 21001


def test_update_then_restart_fingerprint_decision_is_full_rebuild(
    importer: Importer,
    workspace: Workspace,
    config,
    registry: Registry,
    tmp_path: Path,
) -> None:
    """S2-04：身份保留后 restart/start 的指纹决策不得误判「未变」。

    旧指纹基线（上次成功部署）对比按新 manifest 重渲染的生成物：
    sourceHash 必变 → full_rebuild，而不是对旧镜像走轻量 compose start。
    """
    iid = importer.import_zip(_container_zip(tmp_path / "api.zip", "app=1\n")).instance_id
    _seed_deployed_container(workspace, registry, iid)
    importer.update_zip(_container_zip(tmp_path / "api-v2.zip", "app=2\n"), iid)

    reloaded = InstanceManifest.load(workspace.app_manifest_path(iid))
    action, changed = lifecycle._deployed_action(workspace, config, registry, iid, reloaded)
    assert action == "full_rebuild"
    assert "sourceHash" in changed


def test_update_then_rebuild_uses_build_first_transaction(
    importer: Importer,
    workspace: Workspace,
    config,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S2-03：update 后的 rebuild 须走先建后换（build-first），不是首次部署。

    首次部署路径会先停旧容器再 build、不打 :prev 标签；先建后换则是
    build（旧容器仍在服务）→ 救数据 → down → up，并把旧镜像打 :prev。
    实案中正是身份丢失导致退化、构建失败时旧容器已被 down 且无回退。
    """
    from local_webpage_access import rebuild_txn

    iid = importer.import_zip(_container_zip(tmp_path / "api.zip", "app=1\n")).instance_id
    _seed_deployed_container(workspace, registry, iid)
    importer.update_zip(_container_zip(tmp_path / "api-v2.zip", "app=2\n"), iid)

    order: list[str] = []
    tags: list[tuple[str, str]] = []
    rt = MagicMock(name="runtime")
    rt.is_running.return_value = True
    rt.image_id.return_value = "old-image"
    rt.container_id.return_value = "new-container"

    def _rec(name: str):
        def _call(*a, **k):
            order.append(name)

        return _call

    rt.build.side_effect = _rec("build")
    rt.down.side_effect = _rec("down")
    rt.up.side_effect = _rec("up")

    monkeypatch.setattr(hosting, "DockerRuntime", MagicMock(return_value=rt))
    monkeypatch.setattr(rebuild_txn, "tag_image", lambda src, dst: tags.append((src, dst)))
    monkeypatch.setattr(
        "local_webpage_access.container_identity.ensure_non_root_identity_ready",
        lambda *a: None,
    )
    monkeypatch.setattr(
        hosting,
        "_rescue_container_data_before_rebuild",
        lambda *a, **k: order.append("rescue"),
    )
    monkeypatch.setattr(hosting, "generate_dockerfile", lambda *a, **k: None)
    monkeypatch.setattr(hosting, "generate_compose", lambda *a, **k: None)
    monkeypatch.setattr(hosting, "generate_env", lambda *a, **k: None)
    monkeypatch.setattr(hosting, "_ensure_container_port", lambda *a, **k: (21001, False))
    monkeypatch.setattr(hosting, "_wait_for_http", lambda *a, **k: True)
    monkeypatch.setattr(
        hosting,
        "_evaluate_container_verification",
        lambda *a, **k: {
            "overall_status": "passed",
            "liveness_passed": True,
            "mandatory_all_passed": True,
            "optional_warnings": [],
            "probe_notes": [],
            "observed_capabilities": ["ui"],
        },
    )

    manifest = hosting.host_container(workspace, config, registry, iid)

    # 先建后换：build 在停机之前，旧镜像被打 :prev（首次部署路径二者都不成立）
    assert order == ["build", "rescue", "down", "up"]
    assert len(tags) == 1 and tags[0] == ("old-image", f"lwa-{iid}-app:prev")
    assert manifest.lastOperation is not None
    assert manifest.lastOperation["kind"] == "rebuild"


def test_update_backfills_fingerprint_baseline_for_legacy_instance(
    importer: Importer,
    workspace: Workspace,
    registry: Registry,
    tmp_path: Path,
) -> None:
    """S2-04 之前部署的旧实例无指纹基线：update 后补记，身份保留不再假绿。"""
    iid = importer.import_zip(_container_zip(tmp_path / "api.zip", "app=1\n")).instance_id
    _seed_deployed_container(workspace, registry, iid, with_fingerprints=False)

    importer.update_zip(_container_zip(tmp_path / "api-v2.zip", "app=2\n"), iid)

    reloaded = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert reloaded.deploymentFingerprints  # 基线已补记
    current = lifecycle._compute_deployment_fingerprints(workspace, reloaded)
    action, changed = lifecycle._fingerprint_change_action(
        reloaded.deploymentFingerprints, current
    )
    assert action == "full_rebuild"
    assert "sourceHash" in changed


def test_pending_instance_same_zip_rescans(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """BUG-787：pending（无部署计划）实例不被 zip 指纹未变短路，可借 update 重获扫描。"""
    zip_path = _make_zip(tmp_path / "mystery.zip", {"notes.txt": "hello"})
    iid = importer.import_zip(zip_path).instance_id
    assert InstanceManifest.load(workspace.app_manifest_path(iid)).status == Status.PENDING

    result = importer.update_zip(zip_path, iid, restart=False)
    assert result.skipped is False
    assert result.detection is not None

    reloaded = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert reloaded.status == Status.PENDING
    assert (workspace.app_current(iid) / "notes.txt").is_file()


def test_pending_folder_instance_unchanged_rescans(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """BUG-787：folder 源 pending 实例源目录未变时同样不短路（重获扫描结果）。"""
    src = tmp_path / "mystery-site"
    src.mkdir()
    src.joinpath("notes.txt").write_text("hello", encoding="utf-8")

    iid = importer.import_from_dir(src).instance_id
    assert InstanceManifest.load(workspace.app_manifest_path(iid)).status == Status.PENDING

    result = importer.update_from_dir(iid, restart=False)
    assert result.skipped is False

    reloaded = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert reloaded.status == Status.PENDING
    assert reloaded.sourceKind == "folder"
    assert reloaded.sourceSyncHash is not None


def test_update_preserves_internal_port_when_scanner_guessed(
    importer: Importer,
    workspace: Workspace,
    registry: Registry,
    tmp_path: Path,
) -> None:
    """BUG-788：internalPortGuessed（源码只读 process.env.PORT、无字面回退）时
    保留 manifest 既有 internalPort，不用兜底推断覆盖运维确认的端口。"""
    from local_webpage_access.importer import apply_detection_to_manifest
    from local_webpage_access.models import Kind, Runtime, ServingMode
    from local_webpage_access.scanner import DetectionResult

    iid = importer.import_zip(_container_zip(tmp_path / "api.zip", "app=1\n")).instance_id
    _seed_deployed_container(workspace, registry, iid)
    mpath = workspace.app_manifest_path(iid)
    old = InstanceManifest.load(mpath)
    assert old.container is not None
    old.container.internalPort = 17717
    old.save(mpath)

    base = dict(
        kind=Kind.PYTHON,
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        form="backend-container",
        internalPort=3000,
    )
    guessed = apply_detection_to_manifest(
        old, DetectionResult(**base, internalPortGuessed=True), workspace
    )
    assert guessed.container is not None
    assert guessed.container.internalPort == 17717

    reliable = apply_detection_to_manifest(
        old, DetectionResult(**base, internalPortGuessed=False), workspace
    )
    assert reliable.container is not None
    assert reliable.container.internalPort == 3000
