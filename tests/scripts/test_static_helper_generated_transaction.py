"""静态 WSGI helper 属于可选生成物，必须跟随指纹、快照、回滚与留档。"""

import pytest

from local_webpage_access import lifecycle, rebuild_txn, rollback, versions
from tests.scripts.test_bug1003_zip_hostbuild_retry import _container_manifest, _seed


@pytest.mark.parametrize("present", [False, True])
def test_optional_helper_previous_snapshot_restores_presence(workspace, present):
    workspace.ensure_app_dirs("demo")
    helper = workspace.app_docker("demo") / "lwa_static_wsgi.py"
    if present:
        helper.write_text("old-helper")
    rebuild_txn.snapshot_generated(workspace, "demo")
    helper.write_text("new-helper")
    assert rebuild_txn.restore_generated(workspace, "demo")
    assert helper.read_text() == "old-helper" if present else not helper.exists()


def test_config_and_build_hash_include_optional_helper_and_legacy_missing_is_stable(workspace):
    workspace.ensure_app_dirs("demo")
    compose = workspace.app_compose_path("demo")
    dockerfile = workspace.app_dockerfile_path("demo")
    env = workspace.app_env_path("demo")
    helper = dockerfile.with_name("lwa_static_wsgi.py")
    for path in (compose, dockerfile, env):
        path.write_text("unchanged")

    def hashes():
        return (
            lifecycle._compute_config_fingerprint(compose, dockerfile, env),
            lifecycle._compute_build_config_fingerprint(dockerfile),
        )

    legacy = hashes()
    helper.write_text("old-module")
    before = hashes()
    helper.write_text("new-module")
    after = hashes()
    assert all(x != y for x, y in zip(legacy, before, strict=True))
    assert all(x != y for x, y in zip(before, after, strict=True))
    helper.unlink()
    assert hashes() == legacy


@pytest.mark.parametrize("present", [False, True])
def test_versions_keep_optional_helper_without_rejecting_legacy_snapshots(workspace, present):
    workspace.ensure_app_dirs("demo")
    for name in ("compose.yaml", "Dockerfile", ".env"):
        (workspace.app_docker("demo") / name).write_text("legacy")
    helper = workspace.app_docker("demo") / "lwa_static_wsgi.py"
    if present:
        helper.write_text("old-helper")
    versions.snapshot_config(workspace, "demo", 1)
    assert versions.snapshot_complete(workspace, "demo", 1)
    helper.write_text("new-helper")
    assert versions.restore_config_snapshot(workspace, "demo", 1)
    assert helper.read_text() == "old-helper" if present else not helper.exists()


def test_archive_referencing_helper_requires_its_copy(workspace):
    workspace.ensure_app_dirs("demo")
    for name in ("compose.yaml", ".env"):
        (workspace.app_docker("demo") / name).write_text("legacy")
    workspace.app_dockerfile_path("demo").write_text(
        "COPY docker/lwa_static_wsgi.py /app/lwa_static_wsgi.py\n"
    )
    versions.snapshot_config(workspace, "demo", 1)
    assert not versions.snapshot_complete(workspace, "demo", 1)


def test_attempt_rollback_restores_helper(workspace, registry):
    manifest = _container_manifest("demo")
    _seed(workspace, registry, manifest)
    helper = workspace.app_docker("demo") / "lwa_static_wsgi.py"
    helper.write_text("old-helper")
    snapshot = lifecycle._snapshot_attempt(workspace, registry, "demo", manifest)
    helper.write_text("new-helper")
    lifecycle._restore_from_snapshot(workspace, registry, "demo", manifest, snapshot)
    assert helper.read_text() == "old-helper"


@pytest.mark.parametrize("present", [False, True])
def test_failed_version_rollback_preserves_current_helper(workspace, present):
    workspace.ensure_app_dirs("demo")
    docker = workspace.app_docker("demo")
    helper = docker / "lwa_static_wsgi.py"
    if present:
        helper.write_text("current-helper")
    previous = rollback._read_generated(docker)
    helper.write_text("target-helper")
    rollback._write_generated(docker, previous)
    assert helper.read_text() == "current-helper" if present else not helper.exists()
