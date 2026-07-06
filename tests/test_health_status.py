"""健康检查与状态汇总测试（WBS-18.04/05/06/07/08/10）。"""

from __future__ import annotations

import socket
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest

from local_webpage_access.health import HealthResult, check_health, http_ok
from local_webpage_access.models import (
    ContainerConfig,
    DatabaseConfig,
    DesiredState,
    InstanceManifest,
    Kind,
    ResourceProfile,
    Runtime,
    ServingMode,
    StaticConfig,
    Status,
)
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry
from local_webpage_access.status import (
    InstanceStatus,
    all_statuses,
    instance_status,
    status_counts,
    sync_status,
)


# ---- fixtures --------------------------------------------------------------


@pytest.fixture()
def workspace(workspace_root: Path) -> Workspace:
    ws = Workspace(workspace_root)
    ws.ensure_workspace_dirs()
    return ws


@pytest.fixture()
def registry(workspace_root: Path) -> Registry:
    workspace_root.joinpath("registry").mkdir(parents=True, exist_ok=True)
    reg = Registry(workspace_root / "registry" / "local-web.db")
    reg.open()
    yield reg
    reg.close()


@pytest.fixture()
def config(workspace_root: Path):
    from local_webpage_access.config import Config, PortPool

    return Config(portPool=PortPool(start=21000, end=21050))


def _seed_container(
    workspace: Workspace,
    registry: Registry,
    iid: str = "api",
    *,
    host_port: int | None = 21000,
    has_database: bool = False,
) -> InstanceManifest:
    workspace.ensure_app_dirs(iid)
    manifest = InstanceManifest(
        id=iid,
        name=iid,
        version="1",
        kind=Kind.PYTHON,
        stack=["fastapi"],
        hasDatabase=has_database,
        database=DatabaseConfig(type="sqlite", dataDir="data") if has_database else None,
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.SMALL,
        status=Status.RUNNING,
        desiredState=DesiredState.RUNNING,
        container=ContainerConfig(
            projectName=f"lwa-{iid}",
            internalPort=8000,
            composePath="docker/compose.yaml",
            dockerfilePath="docker/Dockerfile",
            hostPort=host_port,
        ),
    )
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)
    return manifest


def _seed_static(
    workspace: Workspace, registry: Registry, iid: str = "demo", *, host_port: int = 21100
) -> InstanceManifest:
    workspace.ensure_app_dirs(iid)
    manifest = InstanceManifest(
        id=iid,
        name=iid,
        version="1",
        kind=Kind.STATIC,
        runtime=Runtime.SHARED_STATIC,
        servingMode=ServingMode.SHARED_STATIC,
        resourceProfile=ResourceProfile.TINY,
        status=Status.RUNNING,
        desiredState=DesiredState.RUNNING,
        static=StaticConfig(hostPort=host_port),
    )
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)
    return manifest


# ---- http_ok ----------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):  # noqa: D401,N802
        pass


@pytest.fixture()
def http_server():
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _OkHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port
    server.shutdown()
    server.server_close()


def test_http_ok_true_on_200(http_server) -> None:
    ok, code = http_ok(http_server, timeout=2.0)
    assert ok is True
    assert code == 200


def test_http_ok_false_on_closed_port() -> None:
    port = _free_port()  # 立刻关闭，没人监听
    ok, code = http_ok(port, timeout=1.0)
    assert ok is False
    assert code is None


# ---- check_health ----------------------------------------------------------


def test_check_health_success_records_timestamp(
    workspace, registry, config, http_server
) -> None:
    _seed_container(workspace, registry, "api", host_port=http_server)
    result = check_health(workspace, config, registry, "api")
    assert isinstance(result, HealthResult)
    assert result.ok is True
    assert result.host_port == http_server
    row = registry.get_instance("api")
    assert row["last_health_check_at"] is not None


def test_check_health_failure_records_last_error(
    workspace, registry, config
) -> None:
    port = _free_port()  # 无人监听
    _seed_container(workspace, registry, "api", host_port=port)
    result = check_health(workspace, config, registry, "api", timeout=1.0)
    assert result.ok is False
    row = registry.get_instance("api")
    assert row["last_error"]
    events = registry.list_events("api")
    assert any(e["event_type"] == "health_check" for e in events)


def test_check_health_skips_when_no_port(workspace, registry, config) -> None:
    """无 hostPort 的实例（未部署）→ 跳过，不写 last_error。"""
    _seed_container(workspace, registry, "api", host_port=None)
    result = check_health(workspace, config, registry, "api")
    assert result.ok is False
    assert result.host_port is None


# ---- instance_status -------------------------------------------------------


def test_instance_status_container(workspace, registry, config) -> None:
    _seed_container(workspace, registry, "api", host_port=21000)
    st = instance_status(workspace, config, registry, "api")
    assert isinstance(st, InstanceStatus)
    assert st.id == "api"
    assert st.runtime == "docker-compose"
    assert st.status == "running"
    assert st.host_port == 21000
    assert st.internal_port == 8000


def test_instance_status_static(workspace, registry, config) -> None:
    _seed_static(workspace, registry, "demo", host_port=21100)
    st = instance_status(workspace, config, registry, "demo")
    assert st.runtime == "shared-static"
    assert st.host_port == 21100


def test_instance_status_missing_raises(workspace, registry, config) -> None:
    from local_webpage_access.errors import LifecycleError

    with pytest.raises(LifecycleError):
        instance_status(workspace, config, registry, "nope")


def test_all_statuses_returns_all(workspace, registry, config) -> None:
    _seed_container(workspace, registry, "api")
    _seed_static(workspace, registry, "demo")
    statuses = all_statuses(workspace, config, registry)
    assert {s.id for s in statuses} == {"api", "demo"}


def test_status_counts(workspace, registry, config) -> None:
    _seed_container(workspace, registry, "api")  # running
    _seed_static(workspace, registry, "demo")  # running
    counts = status_counts(registry)
    assert counts.get("running", 0) >= 2


def test_status_to_dict(workspace, registry, config) -> None:
    _seed_container(workspace, registry, "api")
    d = instance_status(workspace, config, registry, "api").to_dict()
    assert d["id"] == "api"
    assert d["hostPort"] == 21000
    assert "desiredState" in d


def test_status_to_dict_includes_manager_list_fields(
    workspace, registry, config
) -> None:
    """BUG-028：管理页列表需要 stack/database/servingMode/资源字段。"""
    _seed_container(workspace, registry, "api", has_database=True)
    registry.upsert_resources(
        "api",
        source_size_bytes=100,
        public_size_bytes=20,
        data_size_bytes=30,
        image_size_bytes=400,
        last_memory_bytes=123456,
        last_cpu_percent=7.5,
    )
    d = instance_status(workspace, config, registry, "api").to_dict()
    assert d["servingMode"] == "container"
    assert d["resourceProfile"] == "small"
    assert d["stack"] == ["fastapi"]
    assert d["database"] == "sqlite"
    assert d["sourceSizeBytes"] == 100
    assert d["publicSizeBytes"] == 20
    assert d["dataSizeBytes"] == 30
    assert d["imageSizeBytes"] == 400
    assert d["lastMemoryBytes"] == 123456
    assert d["lastCpuPercent"] == 7.5


# ---- sync_status -----------------------------------------------------------


def test_sync_status_observes_and_reports_change(
    workspace, registry, config, monkeypatch
) -> None:
    """sync_status 调用 observe_status，状态变化时返回映射。"""
    _seed_container(workspace, registry, "api")
    # manifest 落 running，但容器实际没跑 → observe 改写 stopped
    class _FakeRT:
        def __init__(self, *a, **kw):
            pass

        def is_running(self, iid):
            return False

    monkeypatch.setattr("local_webpage_access.hosting.DockerRuntime", _FakeRT)
    monkeypatch.setattr("local_webpage_access.docker_runtime.DockerRuntime", _FakeRT)

    changed = sync_status(workspace, config, registry, "api")
    assert changed == {"api": "stopped"}
    assert registry.get_instance("api")["status"] == "stopped"


def test_sync_status_all_instances(workspace, registry, config, monkeypatch) -> None:
    _seed_container(workspace, registry, "api")
    _seed_static(workspace, registry, "demo")

    class _FakeRT:
        def __init__(self, *a, **kw):
            pass

        def is_running(self, iid):
            return False

    monkeypatch.setattr("local_webpage_access.hosting.DockerRuntime", _FakeRT)
    monkeypatch.setattr("local_webpage_access.docker_runtime.DockerRuntime", _FakeRT)
    # 静态实例没在跑（无 pid 文件），也应被观测为 stopped
    changed = sync_status(workspace, config, registry)
    assert "api" in changed


def test_cli_status_syncs_before_display(
    workspace, registry, config, monkeypatch
) -> None:
    """BUG-026：lwa status 输出前应先 observe/sync，避免显示陈旧 running。"""
    from typer.testing import CliRunner

    from local_webpage_access.cli import app

    _seed_container(workspace, registry, "api")
    workspace.config_path.write_text(config.to_yaml(), encoding="utf-8")

    class _FakeRT:
        def __init__(self, *a, **kw):
            pass

        def is_running(self, iid):
            return False

    monkeypatch.setattr("local_webpage_access.docker_runtime.DockerRuntime", _FakeRT)
    monkeypatch.chdir(workspace.root)

    result = CliRunner().invoke(app, ["status", "api"])
    assert result.exit_code == 0, result.output
    assert "stopped" in result.output
    assert registry.get_instance("api")["status"] == "stopped"
