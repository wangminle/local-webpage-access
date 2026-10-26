"""待处理宿主预构建只能由用户生命周期操作消费，自动自愈不执行命令。"""

import pytest

from local_webpage_access import lifecycle
from local_webpage_access.errors import BuildError, LifecycleError
from local_webpage_access.models import InstanceManifest, Status
from tests.scripts.test_bug1003_zip_hostbuild_retry import _container_manifest, _seed


@pytest.mark.parametrize("operation", ["start", "rebuild"])
@pytest.mark.parametrize("deployed", [False, True])
def test_manual_operation_consumes_pending_before_container_build(
    workspace, registry, config, monkeypatch, operation, deployed
):
    manifest = _container_manifest("demo")
    manifest.hostBuild = "printf fresh > artifact.txt"
    manifest.hostBuildPending = True
    manifest.status = Status.STOPPED
    if deployed:
        manifest.container.containerId = "old-container"
    _seed(workspace, registry, manifest)
    built = []

    def host(ws, cfg, reg, iid, **kw):
        built.append((ws.app_current(iid) / "artifact.txt").read_text())
        result = InstanceManifest.load(ws.app_manifest_path(iid))
        result.status = Status.RUNNING
        result.save(ws.app_manifest_path(iid))
        return result

    monkeypatch.setattr("local_webpage_access.hosting.host_instance", host)
    monkeypatch.setattr("local_webpage_access.hosting.host_container", host)
    call = lifecycle.start_instance if operation == "start" else lifecycle.rebuild_instance
    call(workspace, config, registry, "demo")
    assert built == ["fresh"]
    assert InstanceManifest.load(workspace.app_manifest_path("demo")).hostBuildPending is False


@pytest.mark.parametrize("operation", ["start", "rebuild"])
def test_pending_failure_keeps_service_and_flag(
    workspace, registry, config, monkeypatch, operation
):
    manifest = _container_manifest("demo")
    manifest.container.containerId = "old-container"
    manifest.hostBuild = "exit 7"
    manifest.hostBuildPending = True
    _seed(workspace, registry, manifest)

    def must_not_build(*a, **k):
        pytest.fail("预构建失败时不得进入容器构建")

    monkeypatch.setattr("local_webpage_access.hosting.host_instance", must_not_build)
    monkeypatch.setattr("local_webpage_access.hosting.host_container", must_not_build)
    call = lifecycle.start_instance if operation == "start" else lifecycle.rebuild_instance
    with pytest.raises(BuildError):
        call(workspace, config, registry, "demo")
    current = InstanceManifest.load(workspace.app_manifest_path("demo"))
    assert current.status == Status.RUNNING
    assert current.hostBuildPending is True
    assert current.container.containerId == "old-container"


def test_reconcile_does_not_execute_or_deploy_pending_host_command(
    workspace, registry, config, monkeypatch
):
    manifest = _container_manifest("demo")
    manifest.hostBuild = "printf unwanted > artifact.txt"
    manifest.hostBuildPending = True
    _seed(workspace, registry, manifest)
    calls = []
    monkeypatch.setattr("local_webpage_access.hosting.host_instance", lambda *a: calls.append(a))
    with pytest.raises(LifecycleError, match="预构建"):
        lifecycle.start_instance(workspace, config, registry, "demo", from_reconcile=True)
    assert calls == []
    assert not (workspace.app_current("demo") / "artifact.txt").exists()
    assert InstanceManifest.load(workspace.app_manifest_path("demo")).hostBuildPending is True


def test_restart_requires_manual_pending_build_instead_of_deploying_stale_artifacts(
    workspace, registry, config, monkeypatch
):
    manifest = _container_manifest("demo")
    manifest.hostBuild = "printf fresh > artifact.txt"
    manifest.hostBuildPending = True
    _seed(workspace, registry, manifest)
    monkeypatch.setattr("local_webpage_access.hosting.host_instance", lambda *a: None)
    with pytest.raises(LifecycleError, match="预构建"):
        lifecycle.restart_instance(workspace, config, registry, "demo")
    assert not (workspace.app_current("demo") / "artifact.txt").exists()
