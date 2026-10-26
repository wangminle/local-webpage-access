"""BUG-998：recreate/重建补偿必须恢复 last-good 业务环境（``.env.local``）。

``.env.local`` 是用户维护的业务环境文件，不属于生成物快照。改坏后 recreate 的
新 up 失败属预期，但补偿路径拉起旧版本时若仍读同一个坏文件，旧服务也起不来，
最终 FAILED。修复：每次部署/recreate 成功后记录 last-good 副本（含「无此文件」
语义），补偿 up 前恢复。缺少副本且无法证明现场环境一致时，在停机前中止；
完整重建补偿成功后外层不得再次清理旧服务，业务环境变化时 stop-first 改走先建后换。
"""

from __future__ import annotations

import shutil
import stat
from pathlib import Path

import pytest

from local_webpage_access import generated_convergence, hosting, lifecycle, rebuild_txn
from local_webpage_access.errors import DockerError, HostingError
from local_webpage_access.models import DesiredState, InstanceManifest, Status
from tests.scripts.test_s2_04_convergence import _baseline

BAD = "REVIEW_STATE=bad\n"
GOOD = "REVIEW_STATE=good\n"
GOOD2 = "REVIEW_STATE=good2\n"


def _env_local(workspace, iid: str = "api") -> Path:
    return workspace.app_env_local_path(iid)


def _last_good_copy(workspace, iid: str = "api") -> Path:
    return workspace.app_docker(iid) / rebuild_txn.LAST_GOOD_DIRNAME / ".env.local"


def _seed_running(workspace, registry, config, env_text: str | None):
    """已部署实例 + 模拟一次成功部署登记的 last-good 业务环境。"""
    manifest = _baseline(workspace, registry, config)
    if env_text is not None:
        _env_local(workspace).write_text(env_text, encoding="utf-8")
    generated_convergence.converge_generated_artifacts(workspace, config, manifest)
    manifest.status = Status.RUNNING
    manifest.desiredState = DesiredState.RUNNING
    manifest.deploymentFingerprints = lifecycle._compute_deployment_fingerprints(
        workspace, manifest
    )
    manifest.save(workspace.app_manifest_path("api"))
    registry.upsert_from_manifest(manifest)
    rebuild_txn.save_last_good_env(workspace, "api")
    return manifest


def _decide(workspace, config, registry) -> None:
    """真实路径：start/restart 先经 _deployed_action 快照生成物并决策。"""
    manifest = InstanceManifest.load(workspace.app_manifest_path("api"))
    action, _ = lifecycle._deployed_action(workspace, config, registry, "api", manifest)
    assert action == "runtime_recreate"


class _EnvRuntime:
    """按 .env.local 内容决定 up 成败的 DockerRuntime 替身。"""

    attempts: list[str] = []
    destructive: list[str] = []

    def __init__(self, workspace=None, registry=None) -> None:
        self.workspace = workspace
        self.registry = registry

    @classmethod
    def ensure_available(cls) -> None:
        return None

    @classmethod
    def is_available(cls) -> bool:
        return True

    def is_running(self, iid) -> bool:
        return True

    def container_id(self, iid, **kw):
        return "cid-x"

    def image_id(self, iid):
        return "sha256:img"

    def stop(self, iid, **kw) -> None:
        type(self).destructive.append("stop")

    def down(self, iid, **kw) -> None:
        type(self).destructive.append("down")

    def up(self, iid, **kw) -> None:
        path = _env_local(self.workspace, iid)
        state = path.read_text(encoding="utf-8").strip() if path.is_file() else "<missing>"
        type(self).attempts.append(state)
        if state in (BAD.strip(), "<missing>"):
            raise DockerError("controlled bad environment")

    def build(self, iid, **kw) -> None:
        return None

    def status(self, iid):
        return None

    def remove_image(self, image) -> None:
        return None


@pytest.fixture()
def env_runtime(monkeypatch):
    _EnvRuntime.attempts = []
    _EnvRuntime.destructive = []
    monkeypatch.setattr("local_webpage_access.hosting.DockerRuntime", _EnvRuntime)
    monkeypatch.setattr("local_webpage_access.docker_runtime.DockerRuntime", _EnvRuntime)
    monkeypatch.setattr(
        "local_webpage_access.hosting._can_recreate_container_without_rebuild",
        lambda *a, **kw: True,
    )
    monkeypatch.setattr(
        "local_webpage_access.hosting._rescue_container_data_before_rebuild",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "local_webpage_access.hosting._container_probe_context", lambda *a, **kw: {}
    )
    monkeypatch.setattr("local_webpage_access.hosting._http_ok", lambda port, **kw: True)
    monkeypatch.setattr(rebuild_txn, "tag_image", lambda *a: None)
    monkeypatch.setattr("local_webpage_access.versions.resolve_repo_image_id", lambda *a: None)
    return _EnvRuntime


def test_recreate_failure_restores_last_good_env(
    workspace, registry, config, env_runtime
) -> None:
    """改坏 .env.local 触发 recreate：新 up（坏环境）失败，补偿 up 前恢复
    last-good，旧版本用好的环境拉起，实例保持 RUNNING。"""
    _seed_running(workspace, registry, config, GOOD)
    _env_local(workspace).write_text(BAD, encoding="utf-8")
    _decide(workspace, config, registry)

    with pytest.raises(HostingError, match="已回滚到旧配置并恢复服务"):
        hosting.recreate_container_runtime(workspace, config, registry, "api")

    assert _EnvRuntime.attempts == [BAD.strip(), GOOD.strip()]
    assert _env_local(workspace).read_text(encoding="utf-8") == GOOD
    manifest = InstanceManifest.load(workspace.app_manifest_path("api"))
    assert manifest.status == Status.RUNNING


@pytest.mark.parametrize("change", ["modified", "deleted", "added"])
@pytest.mark.parametrize("baseline_version", [2, 3])
@pytest.mark.parametrize("operation", [
    "recreate", "start", "restart", "rebuild", "stop-first", "start-rebuild",
])
def test_without_last_good_record_preserves_old_service(
    workspace, registry, config, env_runtime, change, operation, baseline_version
) -> None:
    """升级前旧实例无恢复记录：拒绝替换，旧容器与 RUNNING 状态保持完整。"""
    manifest = _seed_running(workspace, registry, config, None if change == "added" else GOOD)
    if baseline_version == 2:
        manifest.deploymentFingerprints["runtimeConfigHashVersion"] = 2
        manifest.deploymentFingerprints["runtimeConfigHash"] = (
            lifecycle._compute_runtime_config_fingerprint(
                workspace.app_compose_path("api"), workspace.app_env_path("api")
            )
        )
        manifest.save(workspace.app_manifest_path("api"))
    shutil.rmtree(_last_good_copy(workspace).parent)
    old_compose = workspace.app_compose_path("api").read_bytes()
    if change == "deleted":
        _env_local(workspace).unlink()
    else:
        _env_local(workspace).write_text(BAD, encoding="utf-8")
    if operation == "stop-first":
        manifest.container.rebuildStrategy = "stop-first"
        manifest.save(workspace.app_manifest_path("api"))
    if operation == "start-rebuild":
        manifest.buildHooks = ["echo rebuild"]
        manifest.save(workspace.app_manifest_path("api"))
    if operation == "recreate":
        rebuild_txn.snapshot_generated(workspace, "api")
        generated_convergence.converge_generated_artifacts(workspace, config, manifest)

    entry = {
        "recreate": hosting.recreate_container_runtime,
        "start": lifecycle.start_instance,
        "start-rebuild": lifecycle.start_instance,
        "restart": lifecycle.restart_instance,
    }.get(operation, hosting.host_container)
    with pytest.raises(HostingError, match="缺少.*业务环境.*已中止"):
        entry(workspace, config, registry, "api")

    assert _EnvRuntime.attempts == []
    assert _EnvRuntime.destructive == []
    current = InstanceManifest.load(workspace.app_manifest_path("api"))
    assert current.status == Status.RUNNING
    assert current.container.containerId == manifest.container.containerId
    assert registry.get_instance("api")["status"] == Status.RUNNING.value
    assert workspace.app_compose_path("api").read_bytes() == old_compose


@pytest.mark.parametrize("env_text", [GOOD, None])
def test_unchanged_legacy_env_can_be_safely_recorded(
    workspace, registry, config, env_runtime, env_text
) -> None:
    manifest = _seed_running(workspace, registry, config, env_text)
    shutil.rmtree(_last_good_copy(workspace).parent)
    # 业务环境未改变，仅新增挂载导致 runtime recreate。
    manifest.container.extraVolumes = [f"{Path.home()}/lwa-bug998-share:/data/share:ro"]
    manifest.save(workspace.app_manifest_path("api"))
    _decide(workspace, config, registry)
    rebuild_txn.ensure_env_rollback_ready(workspace, manifest, use_previous=True)
    assert rebuild_txn.restore_last_good_env(workspace, "api") is True
    if env_text is None:
        assert not _env_local(workspace).exists()
    else:
        assert _env_local(workspace).read_text() == env_text


def test_mandatory_probe_failure_restores_last_good(
    workspace, registry, config, env_runtime, monkeypatch
) -> None:
    _seed_running(workspace, registry, config, GOOD)
    _env_local(workspace).write_text(GOOD2, encoding="utf-8")
    _decide(workspace, config, registry)
    monkeypatch.setattr(hosting, "_evaluate_container_verification", lambda *a: {
        "overall_status": "failed", "error": "required probe failed",
    })
    with pytest.raises(HostingError, match="已回滚到旧配置并恢复服务"):
        hosting.recreate_container_runtime(workspace, config, registry, "api")
    assert _EnvRuntime.attempts == [GOOD2.strip(), GOOD.strip()]
    assert InstanceManifest.load(workspace.app_manifest_path("api")).status == Status.RUNNING


@pytest.mark.parametrize("strategy", ["build-first", "stop-first"])
@pytest.mark.parametrize("operation", ["direct", "start", "restart"])
def test_full_rebuild_keeps_compensated_service_running(
    workspace, registry, config, env_runtime, strategy, operation
) -> None:
    manifest = _seed_running(workspace, registry, config, GOOD)
    manifest.container.rebuildStrategy = strategy
    manifest.buildHooks = ["echo rebuild"]
    manifest.save(workspace.app_manifest_path("api"))
    _env_local(workspace).write_text(BAD, encoding="utf-8")
    entry = {
        "direct": hosting.host_container,
        "start": lifecycle.start_instance,
        "restart": lifecycle.restart_instance,
    }[operation]
    with pytest.raises(HostingError, match="已回滚到旧版本并恢复服务"):
        entry(workspace, config, registry, "api")
    assert _EnvRuntime.attempts == [BAD.strip(), GOOD.strip()]
    # down 新容器后恢复 good；外层不得再次 down 掉补偿启动的旧服务。
    assert _EnvRuntime.destructive == ["down", "down"]
    assert _env_local(workspace).read_text() == GOOD
    assert InstanceManifest.load(workspace.app_manifest_path("api")).status == Status.RUNNING
    assert registry.get_instance("api")["status"] == Status.RUNNING.value


def test_stop_first_status_query_failure_does_not_trigger_outer_cleanup(
    workspace, registry, config, env_runtime, monkeypatch
) -> None:
    manifest = _seed_running(workspace, registry, config, GOOD)
    manifest.container.rebuildStrategy = "stop-first"
    manifest.buildHooks = ["echo rebuild"]
    manifest.save(workspace.app_manifest_path("api"))
    _env_local(workspace).write_text(BAD, encoding="utf-8")
    calls = []

    def is_running(self, iid):
        calls.append(iid)
        if len(calls) == 1:
            raise DockerError("controlled state query failure")
        return True

    monkeypatch.setattr(env_runtime, "is_running", is_running)
    with pytest.raises(DockerError, match="controlled state query failure"):
        lifecycle.start_instance(workspace, config, registry, "api")
    assert _EnvRuntime.attempts == []
    assert _EnvRuntime.destructive == []
    assert InstanceManifest.load(workspace.app_manifest_path("api")).status == Status.RUNNING
    assert registry.get_instance("api")["status"] == Status.RUNNING.value


def test_deleted_env_local_restored_from_last_good(
    workspace, registry, config, env_runtime
) -> None:
    """用户删除 .env.local 后 recreate 失败：补偿从 last-good 恢复文件。"""
    _seed_running(workspace, registry, config, GOOD)
    _env_local(workspace).unlink()
    _decide(workspace, config, registry)

    with pytest.raises(HostingError, match="已回滚到旧配置并恢复服务"):
        hosting.recreate_container_runtime(workspace, config, registry, "api")

    assert _EnvRuntime.attempts == ["<missing>", GOOD.strip()]
    assert _env_local(workspace).read_text(encoding="utf-8") == GOOD


def test_last_good_record_permissions(workspace) -> None:
    """last-good 目录 0700、副本 0600（业务环境可能含密钥）。"""
    docker_dir = workspace.app_docker("api")
    docker_dir.mkdir(parents=True, exist_ok=True)
    _env_local(workspace).write_text(GOOD, encoding="utf-8")
    old_mask = __import__("os").umask(0o022)
    try:
        rebuild_txn.save_last_good_env(workspace, "api")
    finally:
        __import__("os").umask(old_mask)

    good_dir = workspace.app_docker("api") / rebuild_txn.LAST_GOOD_DIRNAME
    assert stat.S_IMODE(good_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(_last_good_copy(workspace).stat().st_mode) == 0o600
    assert _last_good_copy(workspace).read_text(encoding="utf-8") == GOOD


def test_last_good_record_of_missing_env_deletes_file(workspace) -> None:
    """记录时刻无 .env.local：恢复时删除现场文件，而不是回退到更早内容。"""
    workspace.app_docker("api").mkdir(parents=True, exist_ok=True)
    rebuild_txn.save_last_good_env(workspace, "api")

    bad = _env_local(workspace)
    bad.write_text(BAD, encoding="utf-8")
    assert rebuild_txn.restore_last_good_env(workspace, "api") is True
    assert not bad.exists()


def test_restore_without_record_leaves_disk_untouched(workspace) -> None:
    workspace.app_docker("api").mkdir(parents=True, exist_ok=True)
    current = _env_local(workspace)
    current.write_text(BAD, encoding="utf-8")

    assert rebuild_txn.restore_last_good_env(workspace, "api") is False
    assert current.read_text(encoding="utf-8") == BAD


def test_recreate_success_advances_last_good(
    workspace, registry, config, env_runtime
) -> None:
    """recreate 成功后 last-good 推进到新环境，下一次补偿用得上。"""
    _seed_running(workspace, registry, config, GOOD)
    _env_local(workspace).write_text(GOOD2, encoding="utf-8")
    _decide(workspace, config, registry)

    hosting.recreate_container_runtime(workspace, config, registry, "api")

    assert _EnvRuntime.attempts == [GOOD2.strip()]
    assert _last_good_copy(workspace).read_text(encoding="utf-8") == GOOD2
    assert (
        InstanceManifest.load(workspace.app_manifest_path("api")).status == Status.RUNNING
    )
