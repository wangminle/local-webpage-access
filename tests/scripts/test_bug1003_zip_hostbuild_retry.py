"""BUG-1003：zip 更新宿主预构建失败后的同包重试闭环。

独立核查（CHK-496）复现：zip 更新落盘并更新 ``sourceZipHash`` 后 hostBuild
失败，旧服务未停；排除失败条件后再次 import 同一 zip，``result.skipped`` 直
接短路——产物仍不存在且未进入容器构建。普通 rebuild 不执行预构建、
``rebuild --sync`` 又拒绝 zip 源，没有任何入口能重试。

修复：manifest 新增 ``hostBuildPending``——rebuild(host_build=True) 触发即置
位（预构建或镜像构建失败时保留）、重建成功后清位；importer 的同 hash 跳过
对 pending 实例放行，同一 zip 重新 import 即可完整重试。
"""

from __future__ import annotations

import shlex
import sys
import threading
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from local_webpage_access.cli import app
from local_webpage_access.config import Config
from local_webpage_access.errors import BuildError, DockerError, LifecycleError
from local_webpage_access.importer import Importer
from local_webpage_access.lifecycle import rebuild_instance
from local_webpage_access.models import (
    ContainerConfig,
    DesiredState,
    EntryConfig,
    InstanceManifest,
    Kind,
    ResourceProfile,
    Runtime,
    ServingMode,
    Status,
)
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry


def _make_python_zip(zip_path: Path) -> Path:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("requirements.txt", "flask\n")
        zf.writestr("app.py", "app = None\n")
    return zip_path


def _container_manifest(iid: str) -> InstanceManifest:
    return InstanceManifest(
        id=iid,
        name=iid,
        version="1",
        kind=Kind.PYTHON,
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.SMALL,
        container=ContainerConfig(
            projectName=f"lwa-{iid}",
            internalPort=8000,
            composePath="docker/compose.yaml",
            dockerfilePath="docker/Dockerfile",
        ),
        entry=EntryConfig(install="pip install -r requirements.txt", start="python app.py"),
        desiredState=DesiredState.RUNNING,
        status=Status.RUNNING,
    )


def _seed(workspace: Workspace, registry: Registry, manifest: InstanceManifest) -> None:
    workspace.ensure_app_dirs(manifest.id)
    current = workspace.app_current(manifest.id)
    current.mkdir(parents=True, exist_ok=True)
    (current / "app.py").write_text("app = None\n", encoding="utf-8")
    manifest.save(workspace.app_manifest_path(manifest.id))
    registry.upsert_from_manifest(manifest)


def _fake_host_container_ok(ws: Workspace, cfg, reg: Registry, iid: str, **_kw):
    manifest = InstanceManifest.load(ws.app_manifest_path(iid))
    manifest.status = Status.RUNNING
    manifest.save(ws.app_manifest_path(iid))
    return manifest


# ---- lifecycle：pending 的置位 / 清位 / 保持 ----------------------------------


def test_host_build_failure_leaves_pending_flag(
    workspace: Workspace, registry: Registry, config: Config, monkeypatch
) -> None:
    """预构建失败 → BuildError，且 manifest.hostBuildPending 落盘为 True。"""
    monkeypatch.setattr(
        "local_webpage_access.hosting.host_container",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("失败时不应进入 host_container")),
    )
    manifest = _container_manifest("demo")
    manifest.hostBuild = "exit 3"
    _seed(workspace, registry, manifest)

    with pytest.raises(BuildError):
        rebuild_instance(workspace, config, registry, "demo", host_build=True)

    reloaded = InstanceManifest.load(workspace.app_manifest_path("demo"))
    assert reloaded.hostBuildPending is True


def test_successful_host_build_rebuild_clears_pending(
    workspace: Workspace, registry: Registry, config: Config, monkeypatch
) -> None:
    """重试成功（预构建 + 镜像构建）→ pending 立即收口为 False。"""
    monkeypatch.setattr(
        "local_webpage_access.hosting.host_container", _fake_host_container_ok
    )
    manifest = _container_manifest("demo")
    manifest.hostBuild = "echo rebuilt > built.txt"
    manifest.hostBuildPending = True
    _seed(workspace, registry, manifest)

    rebuild_instance(workspace, config, registry, "demo", host_build=True)

    reloaded = InstanceManifest.load(workspace.app_manifest_path("demo"))
    assert reloaded.hostBuildPending is False
    assert (workspace.app_current("demo") / "built.txt").is_file()


def test_explicit_host_build_opt_out_keeps_pending(
    workspace: Workspace, registry: Registry, config: Config, monkeypatch
) -> None:
    """内部调用显式禁用预构建时不执行用户命令，也不误清 pending。"""
    monkeypatch.setattr(
        "local_webpage_access.hosting.host_container", _fake_host_container_ok
    )
    manifest = _container_manifest("demo")
    manifest.hostBuild = "echo rebuilt > built.txt"
    manifest.hostBuildPending = True
    _seed(workspace, registry, manifest)

    rebuild_instance(workspace, config, registry, "demo", host_build=False)

    reloaded = InstanceManifest.load(workspace.app_manifest_path("demo"))
    assert reloaded.hostBuildPending is True
    assert not (workspace.app_current("demo") / "built.txt").exists()


# ---- importer：同 hash 跳过对 pending 放行 ------------------------------------


def test_same_zip_reimport_not_skipped_when_pending(
    workspace: Workspace, registry: Registry, tmp_path: Path
) -> None:
    """pending 实例同一 zip 重新 update 不再被 sourceZipHash 短路。"""
    importer = Importer(workspace, Config(), registry)
    zip_path = _make_python_zip(tmp_path / "site.zip")
    iid = importer.import_zip(zip_path).instance_id

    # 正常对照：无 pending 时同包更新 → skipped（既有 BUG-112 语义不变）。
    assert importer.update_zip(zip_path, iid, restart=False).skipped is True

    manifest = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert manifest.runtime.value == "docker-compose"
    manifest.hostBuild = "echo built > built.txt"
    manifest.hostBuildPending = True
    manifest.save(workspace.app_manifest_path(iid))

    result = importer.update_zip(zip_path, iid, restart=False)
    assert result.skipped is False
    # 重扫生成的新 manifest 保留 pending 与 hostBuild（本轮 needs_rebuild=False
    # 时也不会被默认值抹掉）。
    assert result.manifest.hostBuildPending is True
    assert result.manifest.hostBuild == "echo built > built.txt"


def test_pending_flag_survives_manifest_roundtrip(workspace: Workspace) -> None:
    """hostBuildPending 随 manifest 序列化往返，默认 False。"""
    manifest = _container_manifest("demo")
    assert manifest.hostBuildPending is False
    manifest.hostBuildPending = True
    manifest.save(workspace.app_manifest_path("demo"))
    assert InstanceManifest.load(workspace.app_manifest_path("demo")).hostBuildPending is True


def test_zip_hash_and_pending_are_committed_together(
    workspace, registry, config, tmp_path
) -> None:
    """导入后尚未进入重建就中断，也必须能同包重试，不能留新哈希/旧标记窗口。"""
    importer = Importer(workspace, config, registry)
    iid = importer.import_zip(_make_python_zip(tmp_path / "old.zip")).instance_id
    manifest = InstanceManifest.load(workspace.app_manifest_path(iid))
    manifest.hostBuild = "echo fresh > built.txt"
    manifest.desiredState = DesiredState.RUNNING
    manifest.status = Status.RUNNING
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)
    updated = _make_python_zip(tmp_path / "new.zip")
    with zipfile.ZipFile(updated, "a") as archive:
        archive.writestr("revision.txt", "new")

    result = importer.update_zip(updated, iid, restart=False)
    saved = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert saved.sourceZipHash == result.zip_hash
    assert saved.hostBuildPending is True
    retry = importer.update_zip(updated, iid, restart=True)
    assert retry.skipped is False
    assert retry.needs_rebuild is True


# ---- CLI：--sync 拒绝 zip 的提示包含重试入口 -----------------------------------


def test_sync_refusal_message_mentions_zip_retry(
    workspace: Workspace, registry: Registry, config: Config, monkeypatch, tmp_path: Path
) -> None:
    """zip 实例 rebuild --sync 的报错指明「同一 zip 重新 import 即可重试」。"""
    importer = Importer(workspace, Config(), registry)
    iid = importer.import_zip(_make_python_zip(tmp_path / "site.zip")).instance_id

    def open_env():
        reg = Registry(workspace.db_path)
        reg.open()
        return workspace, config, reg

    monkeypatch.setattr(
        "local_webpage_access.cli.lifecycle.open_workspace_registry", open_env
    )
    result = CliRunner().invoke(app, ["rebuild", iid, "--sync"])
    assert result.exit_code == 2
    assert "同一 zip 重新 import 即可重试" in result.output


@pytest.mark.parametrize("failure_stage", ["host", "image"])
@pytest.mark.parametrize("transport", ["cli", "api"])
def test_cli_same_zip_retry_rebuilds_artifact_and_clears_pending(
    workspace, registry, config, monkeypatch, tmp_path, failure_stage, transport, request
) -> None:
    """真实导入/CLI/预构建链：失败保旧，同包重试后产物进入重建且标记收口。"""
    from local_webpage_access.cli.importing import _do_update

    importer = Importer(workspace, config, registry)
    old = _make_python_zip(tmp_path / "old.zip")
    iid = importer.import_zip(old).instance_id
    ready = tmp_path / "dependency-ready"
    manifest = InstanceManifest.load(workspace.app_manifest_path(iid))
    manifest.status = Status.RUNNING
    manifest.desiredState = DesiredState.RUNNING
    manifest.container.containerId = "healthy-old"
    manifest.hostBuild = (
        f"test -f {shlex.quote(str(ready))} && printf fresh > frontend-artifact.txt"
    )
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)
    updated = _make_python_zip(workspace.inbox / "new.zip")
    with zipfile.ZipFile(updated, "a") as archive:
        archive.writestr("revision.txt", "new")
    builds = []

    def host(ws, cfg, reg, target, **kwargs):
        builds.append((ws.app_current(target) / "frontend-artifact.txt").read_text())
        if failure_stage == "image" and len(builds) == 1:
            raise DockerError("controlled image build failure")
        return _fake_host_container_ok(ws, cfg, reg, target)

    monkeypatch.setattr("local_webpage_access.hosting.host_container", host)
    client = request.getfixturevalue("update_client") if transport == "api" else None

    def update():
        if client is not None:
            return client.post(
                f"/api/instances/{iid}/update",
                json={"zipPath": updated.name},
            )
        _do_update(
            importer, workspace, config, registry, zip_path=str(updated), instance_id=iid,
            restart=True, keep_data=True, yes=True, dry_run=False, force_kind_change=False,
        )

    if failure_stage == "image":
        ready.touch()
    if transport == "cli":
        with pytest.raises(BuildError if failure_stage == "host" else DockerError):
            update()
    else:
        response = update()
        assert response.status_code >= 400, response.text
    current = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert current.hostBuildPending is True, response.text if transport == "api" else ""
    assert current.status == Status.RUNNING
    assert current.container.containerId == "healthy-old"
    ready.touch()
    response = update()
    if client is not None:
        assert response.status_code == 200, response.text
        assert response.json()["rebuiltRuntime"] is True
    assert (workspace.app_current(iid) / "frontend-artifact.txt").read_text() == "fresh"
    assert builds == (["fresh"] if failure_stage == "host" else ["fresh", "fresh"])
    assert InstanceManifest.load(workspace.app_manifest_path(iid)).hostBuildPending is False
    # 成功后的第三次同包更新应恢复正常短路，不重复执行用户命令。
    before = len(builds)
    response = update()
    if client is not None:
        assert response.status_code == 200, response.text
        assert response.json()["skipped"] is True
    assert len(builds) == before


@pytest.fixture()
def update_client(workspace, registry, config, monkeypatch):
    from local_webpage_access.manager_api import create_app, ensure_token

    # 容器构建使用替身；不要让状态采集触碰宿主真实 Docker。
    monkeypatch.setattr("local_webpage_access.status.sync_status", lambda *a, **k: None)
    token = ensure_token(workspace)
    with TestClient(create_app(workspace, config, registry, token=token)) as client:
        client.headers["Authorization"] = f"Bearer {token}"
        yield client


def test_pending_write_failure_aborts_before_user_command(
    workspace, registry, config, monkeypatch
) -> None:
    """关键重试状态无法持久化时，禁止继续运行可能失败的用户命令。"""
    manifest = _container_manifest("demo")
    manifest.hostBuild = "printf executed > should-not-exist.txt"
    _seed(workspace, registry, manifest)
    real_save = InstanceManifest.save

    def save(self, path):
        if self.hostBuildPending:
            raise OSError("controlled manifest write failure")
        return real_save(self, path)

    monkeypatch.setattr(InstanceManifest, "save", save)
    monkeypatch.setattr("local_webpage_access.hosting.host_container", _fake_host_container_ok)
    with pytest.raises(OSError, match="controlled manifest write failure"):
        rebuild_instance(workspace, config, registry, "demo", host_build=True)
    assert not (workspace.app_current("demo") / "should-not-exist.txt").exists()
    assert InstanceManifest.load(workspace.app_manifest_path("demo")).status == Status.RUNNING


@pytest.mark.skipif(sys.platform == "win32", reason="取消探针使用 POSIX 子进程")
def test_cancelled_host_build_keeps_pending_and_old_service(
    workspace, registry, config, monkeypatch, tmp_path
) -> None:
    from local_webpage_access import lifecycle
    from local_webpage_access.build_queue import _reset_global_queue
    from tests.scripts.test_bug990_start_build_cancel import (
        _assert_pid_dead, _capture, _wait_child_pid,
    )

    _reset_global_queue()
    child_pid_file = tmp_path / "host-child.pid"
    manifest = _container_manifest("demo")
    manifest.container.containerId = "healthy-old"
    manifest.hostBuild = f"sleep 60 & echo $! > {shlex.quote(str(child_pid_file))}; wait"
    _seed(workspace, registry, manifest)
    builds = []
    monkeypatch.setattr(
        "local_webpage_access.hosting.host_container", lambda *a, **k: builds.append(a)
    )
    errors = []
    worker = threading.Thread(
        target=_capture,
        args=(errors, rebuild_instance, workspace, config, registry, "demo"),
        kwargs={"host_build": True},
    )
    worker.start()
    try:
        child_pid = _wait_child_pid(child_pid_file)
        result = lifecycle.cancel_build(workspace, config, registry, "demo")
        assert result.outcome == "cancelled"
        worker.join(10)
        assert not worker.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], LifecycleError), errors
        _assert_pid_dead(child_pid)
        current = InstanceManifest.load(workspace.app_manifest_path("demo"))
        assert current.hostBuildPending is True
        assert current.status == Status.RUNNING
        assert current.container.containerId == "healthy-old"
        assert registry.get_instance("demo")["status"] == Status.RUNNING.value
        assert builds == []
    finally:
        if worker.is_alive():
            lifecycle.cancel_build(workspace, config, registry, "demo")
            worker.join(10)
        _reset_global_queue()
