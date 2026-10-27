"""普通容器镜像构建未完成时，同源更新必须可重试且跳过判断持有实例锁。"""

from __future__ import annotations

import threading
import zipfile
from pathlib import Path

from local_webpage_access.importer import Importer
from local_webpage_access.lifecycle import (
    _compute_deployment_fingerprints,
    instance_lock,
)
from local_webpage_access.models import DesiredState, InstanceManifest, Status
from tests.scripts.test_bug1003_zip_hostbuild_retry import _make_python_zip


def _mark_deployed(workspace, manifest: InstanceManifest) -> None:
    manifest.container.containerId = "running-container"
    manifest.status = Status.RUNNING
    manifest.desiredState = DesiredState.RUNNING
    manifest.deploymentFingerprints = _compute_deployment_fingerprints(workspace, manifest)
    manifest.save(workspace.app_manifest_path(manifest.id))


def test_same_zip_retries_until_image_deploys_then_skips(
    workspace, registry, config, tmp_path: Path
) -> None:
    importer = Importer(workspace, config, registry)
    old = _make_python_zip(tmp_path / "old.zip")
    iid = importer.import_zip(old).instance_id
    manifest = InstanceManifest.load(workspace.app_manifest_path(iid))
    _mark_deployed(workspace, manifest)

    new = _make_python_zip(tmp_path / "new.zip")
    with zipfile.ZipFile(new, "a") as archive:
        archive.writestr("revision.txt", "new")
    first = importer.update_zip(new, iid, restart=True)
    assert first.skipped is False and first.needs_rebuild is True

    pending = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert pending.hostBuildPending is False
    assert pending.deploymentFingerprints["sourceHash"] == manifest.deploymentFingerprints["sourceHash"]
    retry = importer.update_zip(new, iid, restart=True)
    assert retry.skipped is False and retry.needs_rebuild is True

    _mark_deployed(workspace, InstanceManifest.load(workspace.app_manifest_path(iid)))
    assert importer.update_zip(new, iid, restart=True).skipped is True


def test_same_zip_skip_waits_for_instance_lock(
    workspace, registry, config, tmp_path: Path
) -> None:
    importer = Importer(workspace, config, registry)
    archive = _make_python_zip(tmp_path / "site.zip")
    iid = importer.import_zip(archive).instance_id
    started = threading.Event()
    finished = threading.Event()
    results = []
    errors = []

    def update() -> None:
        started.set()
        try:
            results.append(importer.update_zip(archive, iid))
        except Exception as exc:  # noqa: BLE001 - 传回主线程断言
            errors.append(exc)
        finally:
            finished.set()

    with instance_lock(workspace, iid):
        worker = threading.Thread(target=update)
        worker.start()
        assert started.wait(5)
        assert not finished.wait(0.2)
    worker.join(5)
    assert not worker.is_alive()
    assert errors == []
    assert len(results) == 1 and results[0].skipped is True
