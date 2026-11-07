from __future__ import annotations

import json
import plistlib
import shlex
import threading
import time
import zipfile
from pathlib import Path

import pytest

from local_webpage_access import autostart as asm
from local_webpage_access.access import refresh_network_entries
from local_webpage_access.atomic_io import atomic_write_text
from local_webpage_access.config import Config
from local_webpage_access.dockerfile_templates import _to_exec_form
from local_webpage_access.errors import HostingError, ZipImportError
from local_webpage_access.hosting import (
    _clear_container_identity,
    _compensate_build_first,
    _split_shell_and_segments,
    build_and_host_frontend,
    sync_dir,
)
from local_webpage_access.importer import Importer
from local_webpage_access.lifecycle import instance_lock
from local_webpage_access.models import (
    ContainerConfig,
    DesiredState,
    InstanceManifest,
    Kind,
    ResourceProfile,
    Runtime,
    ServingMode,
    Status,
)
from local_webpage_access.paths import Workspace
from local_webpage_access.scanner import _flask_empty_db_condition


def _zip(path: Path, body: str) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("index.html", body)
    return path


def test_retry_restores_old_current_instead_of_deleting_it(workspace, registry, monkeypatch) -> None:
    importer = Importer(workspace, Config(), registry)
    iid = importer.import_zip(_zip(workspace.root / "v1.zip", "v1")).instance_id
    from local_webpage_access import importer as importer_mod

    original_save = importer_mod.InstanceManifest.save
    original_hash = InstanceManifest.load(workspace.app_manifest_path(iid)).sourceZipHash
    original_replace = importer_mod.os.replace

    def fail_manifest(self, path):
        if self.sourceZipHash != original_hash:
            raise OSError("manifest failed")
        return original_save(self, path)

    def fail_rollback(src, dst):
        if str(src).endswith("current.old"):
            raise OSError("rollback failed")
        return original_replace(src, dst)

    monkeypatch.setattr(importer_mod.InstanceManifest, "save", fail_manifest)
    monkeypatch.setattr(importer_mod.os, "replace", fail_rollback)
    with pytest.raises(ZipImportError):
        importer.update_zip(_zip(workspace.root / "v2.zip", "v2"), iid, restart=False)
    assert not workspace.app_current(iid).exists()
    assert (workspace.app_current(iid).parent / "current.old" / "index.html").read_text() == "v1"

    monkeypatch.undo()
    monkeypatch.setattr(
        importer_mod.Scanner,
        "detect",
        lambda self, path: (_ for _ in ()).throw(RuntimeError("second scan failed")),
    )
    with pytest.raises(ZipImportError, match="second scan failed"):
        importer.update_zip(_zip(workspace.root / "v3.zip", "v3"), iid, restart=False)
    assert (workspace.app_current(iid) / "index.html").read_text() == "v1"


def test_container_compensation_fails_closed_when_http_passes_without_new_id(
    workspace, registry, monkeypatch
) -> None:
    iid = "demo"
    manifest = InstanceManifest(
        id=iid,
        name=iid,
        version="1",
        kind=Kind.PYTHON,
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.TINY,
        status=Status.RUNNING,
        desiredState=DesiredState.RUNNING,
        container=ContainerConfig(
            projectName="demo",
            internalPort=8000,
            composePath="docker/compose.yaml",
            dockerfilePath="docker/Dockerfile",
            containerId="old-container",
            imageId="old-image",
        ),
    )
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)
    _clear_container_identity(workspace, manifest)

    class FakeRuntime:
        def down(self, _iid):
            return None

        def up(self, _iid):
            return None

        def container_id(self, _iid):
            return None

        def image_id(self, _iid):
            return None

    monkeypatch.setattr("local_webpage_access.hosting.rebuild_txn.restore_generated", lambda ws, iid: True)
    monkeypatch.setattr("local_webpage_access.hosting.rebuild_txn.restore_last_good_env", lambda ws, iid: True)
    monkeypatch.setattr("local_webpage_access.hosting.rebuild_txn.restore_previous_image_tag", lambda manifest: None)
    monkeypatch.setattr("local_webpage_access.hosting._wait_for_http", lambda port: True)
    monkeypatch.setattr("local_webpage_access.hosting.time.sleep", lambda _s: None)
    with pytest.raises(HostingError, match="回滚未完成"):
        _compensate_build_first(workspace, registry, FakeRuntime(), manifest, 18000, 1, RuntimeError("boom"))
    saved = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert saved.status == Status.FAILED
    assert saved.container.containerId is None
    assert saved.container.imageId is None
    assert "无法确认新容器 ID" in (saved.lastError or "")


def test_container_compensation_allows_delayed_container_id(
    workspace, registry, monkeypatch
) -> None:
    iid = "demo"
    manifest = InstanceManifest(
        id=iid,
        name=iid,
        version="1",
        kind=Kind.PYTHON,
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.TINY,
        status=Status.RUNNING,
        desiredState=DesiredState.RUNNING,
        container=ContainerConfig(
            projectName="demo",
            internalPort=8000,
            composePath="docker/compose.yaml",
            dockerfilePath="docker/Dockerfile",
            containerId="old-container",
            imageId="old-image",
        ),
    )
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)
    _clear_container_identity(workspace, manifest)

    class DelayedRuntime:
        def __init__(self):
            self.lookups = 0

        def down(self, _iid):
            return None

        def up(self, _iid):
            return None

        def container_id(self, _iid):
            self.lookups += 1
            return "new-container" if self.lookups >= 4 else None

        def image_id(self, _iid):
            return "new-image"

    runtime = DelayedRuntime()
    monkeypatch.setattr("local_webpage_access.hosting.rebuild_txn.restore_generated", lambda ws, iid: True)
    monkeypatch.setattr("local_webpage_access.hosting.rebuild_txn.restore_last_good_env", lambda ws, iid: True)
    monkeypatch.setattr("local_webpage_access.hosting.rebuild_txn.restore_previous_image_tag", lambda manifest: None)
    monkeypatch.setattr("local_webpage_access.hosting._wait_for_http", lambda port: True)
    monkeypatch.setattr("local_webpage_access.hosting.time.sleep", lambda _s: None)
    with pytest.raises(HostingError, match="恢复服务"):
        _compensate_build_first(workspace, registry, runtime, manifest, 18000, 1, RuntimeError("boom"))
    saved = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert saved.status == Status.RUNNING
    assert saved.container.containerId == "new-container"
    assert saved.container.imageId == "new-image"
    assert runtime.lookups == 4


def test_container_compensation_keeps_old_image_when_new_image_missing(
    workspace, registry, monkeypatch
) -> None:
    """身份尚未清空时，新 imageId 拿不到不得把旧值覆盖成 None。"""
    iid = "demo"
    manifest = InstanceManifest(
        id=iid,
        name=iid,
        version="1",
        kind=Kind.PYTHON,
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.TINY,
        status=Status.RUNNING,
        desiredState=DesiredState.RUNNING,
        container=ContainerConfig(
            projectName="demo",
            internalPort=8000,
            composePath="docker/compose.yaml",
            dockerfilePath="docker/Dockerfile",
            containerId="old-container",
            imageId="old-image",
        ),
    )
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)

    class FakeRuntime:
        def down(self, _iid):
            return None

        def up(self, _iid):
            return None

        def container_id(self, _iid):
            return "new-container"

        def image_id(self, _iid):
            return None

    monkeypatch.setattr("local_webpage_access.hosting.rebuild_txn.restore_generated", lambda ws, iid: True)
    monkeypatch.setattr("local_webpage_access.hosting.rebuild_txn.restore_last_good_env", lambda ws, iid: True)
    monkeypatch.setattr("local_webpage_access.hosting.rebuild_txn.restore_previous_image_tag", lambda manifest: None)
    monkeypatch.setattr("local_webpage_access.hosting._wait_for_http", lambda port: True)
    monkeypatch.setattr("local_webpage_access.hosting.time.sleep", lambda _s: None)
    with pytest.raises(HostingError, match="已回滚到旧版本并恢复服务"):
        _compensate_build_first(workspace, registry, FakeRuntime(), manifest, 18000, 1, RuntimeError("boom"))
    saved = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert saved.status == Status.RUNNING
    assert saved.container.containerId == "new-container"
    assert saved.container.imageId == "old-image"


def test_sync_cleanup_failure_keeps_committed_tree(tmp_path: Path, monkeypatch) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "new.txt").write_text("new", encoding="utf-8")
    (dst / "old.txt").write_text("old", encoding="utf-8")
    monkeypatch.setattr(
        "local_webpage_access.hosting._remove_path",
        lambda path: (_ for _ in ()).throw(OSError("cleanup denied")),
    )
    sync_dir(src, dst)
    assert (dst / "new.txt").read_text(encoding="utf-8") == "new"
    assert not (dst / "old.txt").exists()


def test_refresh_lock_contention_skips_instance(workspace, registry, config, monkeypatch) -> None:
    from tests.scripts.test_access import _seed_static

    _seed_static(workspace, registry, "demo")
    monkeypatch.setattr("local_webpage_access.access.resolve_lan_ip", lambda cfg: "192.168.1.50")
    acquired = threading.Event()
    release = threading.Event()

    def hold():
        with instance_lock(workspace, "demo"):
            acquired.set()
            release.wait(2)

    holder = threading.Thread(target=hold)
    holder.start()
    assert acquired.wait(1)
    started = time.monotonic()
    report = refresh_network_entries(workspace, config, registry)
    release.set()
    holder.join()
    assert "demo" in report.skipped
    assert time.monotonic() - started < 1.0


def test_repair_rebuilds_corrupt_unit(tmp_path: Path, monkeypatch) -> None:
    root, ws, config = _autostart_workspace(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "macos")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    backend = asm.MacLaunchdBackend()
    path = backend.unit_path("manager")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps({"Label": asm.launchd_label("manager")}))

    def runner(cmd, **kwargs):
        from subprocess import CompletedProcess

        return CompletedProcess(cmd, 0, "", "")

    result, _actions = asm.repair(ws, config, runner=runner)
    assert result.enable_ok is True
    assert asm.unit_points_to_workspace(backend, "manager", root)


def test_install_rejects_corrupt_units_without_override(tmp_path: Path, monkeypatch) -> None:
    root, ws, config = _autostart_workspace(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "macos")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    backend = asm.MacLaunchdBackend()
    path = backend.unit_path("manager")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps({"Label": asm.launchd_label("manager")}))
    original = path.read_bytes()
    with pytest.raises(asm.AutostartError, match="其他工作区"):
        asm.install(ws, config)
    assert path.read_bytes() == original


def _autostart_workspace(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    return root, Workspace(root), Config(staticGateway="builtin")


def test_build_registration_failure_finishes_build(workspace, registry, config, monkeypatch) -> None:
    from tests.scripts.test_hosting import _seed_frontend_instance

    _seed_frontend_instance(workspace, registry, "spa")
    original = registry.update_status

    def fail_building(iid, status, **kwargs):
        if status == Status.BUILDING.value:
            raise OSError("status write failed")
        return original(iid, status, **kwargs)

    monkeypatch.setattr(registry, "update_status", fail_building)
    with pytest.raises(OSError, match="status write failed"):
        build_and_host_frontend(workspace, config, registry, "spa")
    builds = registry.list_builds("spa")
    assert builds[0]["status"] == "failed"


def test_atomic_write_tolerates_target_disappearing(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "state.json"
    path.write_text("old", encoding="utf-8")
    original_stat = Path.stat

    def disappear_only_target(self, **kwargs):
        if self == path:
            raise FileNotFoundError()
        return original_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", disappear_only_target)
    atomic_write_text(path, "new")
    assert path.read_text(encoding="utf-8") == "new"


def test_shell_quote_survives_exec_form_and_nested_split() -> None:
    condition = _flask_empty_db_condition("/tmp/path with space/app.db")
    script = f"if {condition}; then true; fi"
    command = f"sh -c {shlex.quote(script)}"
    assert json.loads(_to_exec_form(command)) == ["sh", "-c", script]
    nested = "echo \"$(printf 'a && b')\" && yarn build"
    assert len(_split_shell_and_segments(nested)) == 2
    nested_parens = "echo $(foo (bar) && baz) && yarn build"
    assert _split_shell_and_segments(nested_parens) == [
        "echo $(foo (bar) && baz) ",
        " yarn build",
    ]
