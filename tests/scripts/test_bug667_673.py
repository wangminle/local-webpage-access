"""CHK-331 复核：BUG-667～BUG-673 复现与回归。"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from local_webpage_access.config import default_config
from local_webpage_access.doctor import STATUS_OK, STATUS_WARN, check_service_version_drift
from local_webpage_access.models import (
    ContainerConfig,
    EntryConfig,
    InstanceManifest,
    Kind,
    ResourceProfile,
    Runtime,
    ServingMode,
)
from local_webpage_access.paths import Workspace


def _container_manifest(iid: str = "demo") -> InstanceManifest:
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
        entry=EntryConfig(install="pip install .", start="uvicorn main:app"),
    )


def test_fill_missing_bind_version_does_not_mutate_or_clobber_child(
    workspace: Workspace,
) -> None:
    """BUG-667：父进程 write_state 不得把子进程新 bind 覆写成盘上旧值。"""
    from local_webpage_access.daemon import DaemonState, write_state, state_path
    from local_webpage_access.version_info import fill_missing_bind_version

    path = state_path(workspace)
    write_state(
        workspace,
        DaemonState(
            enabled=True,
            pid=1,
            bind_version="V0.8.16",
            bind_revision="aaa111aaa111",
        ),
    )
    parent = DaemonState(enabled=True, pid=1)
    payload = fill_missing_bind_version(parent, path)
    assert payload["bind_version"] == "V0.8.16"
    assert parent.bind_version is None
    assert parent.bind_revision is None

    write_state(workspace, parent)
    write_state(
        workspace,
        DaemonState(
            enabled=True,
            pid=42,
            bind_version="V0.8.17",
            bind_revision="bbb222bbb222",
        ),
    )
    parent.pid = 42
    write_state(workspace, parent)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["bind_version"] == "V0.8.17"
    assert data["bind_revision"] == "bbb222bbb222"
    assert parent.bind_version is None


def test_persist_circuit_skips_deleted_manifest_and_merges_latest(
    workspace: Workspace,
) -> None:
    """BUG-668：熔断落盘持锁、不复活已删实例，且不覆盖盘上其它字段。"""
    from local_webpage_access.reconcile_circuit import persist_circuit, record_failure

    iid = "gone"
    workspace.ensure_app_dirs(iid)
    path = workspace.app_manifest_path(iid)
    live = _container_manifest(iid)
    live.deploymentFingerprints = {"sourceHash": "abc"}
    live.save(path)

    stale = InstanceManifest.load(path)
    stale.deploymentFingerprints = None
    record_failure(stale)
    persist_circuit(workspace, iid, stale)
    merged = InstanceManifest.load(path)
    assert merged.consecutiveReconcileFailures == 1
    assert merged.deploymentFingerprints == {"sourceHash": "abc"}

    snapshot = InstanceManifest.load(path)
    record_failure(snapshot)
    path.unlink()
    persist_circuit(workspace, iid, snapshot)
    assert not path.is_file()


def test_node_toolchain_retries_distinct_tarball_urls(workspace: Workspace) -> None:
    """BUG-670：Node tarball 各次尝试必须换 host，不能三次同一 URL。"""
    from local_webpage_access.dockerfile_templates import generate_dockerfile

    workspace.ensure_app_dirs("api")
    (workspace.app_current("api") / "package.json").write_text(
        '{"name":"app","dependencies":{}}', encoding="utf-8"
    )
    manifest = _container_manifest("api")
    content = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    urls = set()
    for token in content.replace("\\\n", " ").split():
        if "node-v24.16.0-linux-" in token and "http" in token:
            urls.add(token.strip('"'))
    assert len(urls) >= 2
    apt_runs = [line for line in content.splitlines() if "apt-get" in line]
    tarball_runs = [line for line in content.splitlines() if "node-v24.16.0-linux-" in line]
    assert apt_runs
    assert tarball_runs
    assert all("node-v24.16.0-linux-" not in line for line in apt_runs)
    assert "nodeDistBase" in content


def test_doctor_probe_none_is_unknown_not_ok(workspace: Workspace, monkeypatch) -> None:
    """BUG-671：探测异常不得整项假绿。"""
    from local_webpage_access import doctor as doctor_mod

    monkeypatch.setattr(
        doctor_mod,
        "_service_observed_running",
        lambda name, ws, config: None,
    )
    result = check_service_version_drift(workspace, default_config())
    assert result.status == STATUS_WARN
    assert "未知" in result.message
    assert result.status != STATUS_OK


def test_read_manager_health_version_uses_bound_host(
    workspace: Workspace, monkeypatch
) -> None:
    """BUG-671：manager 健康探测复用 _health_check_host，不用写死 127.0.0.1。"""
    from local_webpage_access import doctor as doctor_mod
    from local_webpage_access.manager_service import ManagerState
    from local_webpage_access.ports import format_http_host

    cfg = default_config()
    cfg.managerHost = "::"
    cfg.managerPort = 17800
    monkeypatch.setattr(
        "local_webpage_access.manager_service.read_state",
        lambda ws: ManagerState(enabled=True, pid=1, host="::", port=17800),
    )
    seen: list[str] = []

    class _Resp:
        def read(self) -> bytes:
            return b'{"version":"V0.8.16","ok":true}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_open(url, timeout=2.0):
        seen.append(url)
        return _Resp()

    monkeypatch.setattr("local_webpage_access.probe.urlopen_direct", fake_open)
    version = doctor_mod._read_manager_health_version(workspace, cfg)
    assert version == "V0.8.16"
    assert seen
    assert format_http_host("::1") in seen[0]
    assert "127.0.0.1" not in seen[0]


def test_bind_process_revision_is_fixed_length(monkeypatch) -> None:
    """BUG-673：git --short 变长时仍归一成 12 位，避免同提交误报漂移。"""
    from local_webpage_access import version_info

    monkeypatch.setattr(version_info, "_repo_root", lambda: "/tmp/lwa")

    class _Result:
        returncode = 0
        stdout = "abcdef1234567\n"

    monkeypatch.setattr(
        version_info.subprocess,
        "run",
        lambda *a, **k: _Result(),
    )
    rev = version_info.bind_process_revision()
    assert rev == "abcdef123456"
    assert version_info.revisions_equivalent("abcdef123456", "abcdef1234567") is True
    assert version_info.revisions_equivalent("abcdef123456", "ffffffff0000") is False


def test_retry_at_uses_local_timezone() -> None:
    """BUG-673：熔断下次重试时间按本地时区落盘。"""
    from local_webpage_access.reconcile_circuit import record_failure

    manifest = _container_manifest()
    record_failure(manifest)
    until = manifest.reconcileNextRetryAt
    assert until
    dt = datetime.fromisoformat(until)
    assert dt.tzinfo is not None
    assert dt.utcoffset() == datetime.now().astimezone().utcoffset()


def test_inspect_state_swallows_status_errors(workspace, registry, monkeypatch) -> None:
    """BUG-673：inspect_state 在 status() 抛错时返回空 dict。"""
    from local_webpage_access.docker_runtime import DockerError, DockerRuntime

    monkeypatch.setattr(
        DockerRuntime,
        "status",
        lambda self, iid: (_ for _ in ()).throw(DockerError("boom")),
    )
    ins = DockerRuntime(workspace, registry).inspect_state("api")
    assert ins == {}


def test_probe_context_keeps_build_log_when_logs_fail(
    workspace, registry, monkeypatch
) -> None:
    """BUG-673：logs() 失败不得丢掉 build_log。"""
    from local_webpage_access import hosting
    from local_webpage_access.docker_runtime import DockerRuntime

    class _Boom(DockerRuntime):
        def status(self, instance_id):
            return SimpleNamespace(state="restarting", container_id="c1")

        def inspect_state(self, instance_id):
            return {"exit_code": 1, "oom_killed": False, "restart_count": 2}

        def logs(self, instance_id, tail=80):
            raise RuntimeError("logs unavailable")

    monkeypatch.setattr(hosting, "DockerRuntime", _Boom)
    extra = hosting._container_probe_context(workspace, registry, "api")
    assert extra.get("build_log")
    assert extra["build_log"].endswith("logs/build.log")


def test_restart_clears_reconcile_circuit(
    workspace: Workspace, registry, config, monkeypatch
) -> None:
    """BUG-672：lwa restart 成功路径必须清熔断。"""
    from local_webpage_access import lifecycle
    from local_webpage_access.reconcile_circuit import MANUAL_THRESHOLD, record_failure

    iid = "demo"
    workspace.ensure_app_dirs(iid)
    manifest = _container_manifest(iid)
    for _ in range(MANUAL_THRESHOLD):
        record_failure(manifest)
    assert manifest.reconcileCircuitManual is True
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)

    monkeypatch.setattr(
        "local_webpage_access.hosting.stop_instance", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "local_webpage_access.hosting.start_container",
        lambda ws, cfg, reg, instance_id: InstanceManifest.load(
            ws.app_manifest_path(instance_id)
        ),
    )
    monkeypatch.setattr(
        "local_webpage_access.lifecycle._is_deployed_container", lambda m: True
    )
    monkeypatch.setattr("local_webpage_access.lifecycle._sync_alias_port", lambda *a, **k: None)

    result = lifecycle.restart_instance(workspace, config, registry, iid)
    assert result.reconcileCircuitManual is False
    assert result.consecutiveReconcileFailures == 0
    disk = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert disk.reconcileCircuitManual is False
