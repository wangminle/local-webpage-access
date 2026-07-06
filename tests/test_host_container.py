"""容器托管流程测试（WBS-15 / WBS-16）。

用 fake DockerRuntime 替换真实 Docker，验证 host_container/stop_container
的编排逻辑（生成 Dockerfile/Compose/.env、端口分配、build+up、健康检查、
manifest/registry 写回、失败诊断）。不依赖真实 Docker。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_webpage_access.compose import generate_compose, generate_env
from local_webpage_access.dockerfile_templates import generate_dockerfile
from local_webpage_access.errors import DockerError, HostingError
from local_webpage_access.hosting import (
    _ensure_container_port,
    _http_ok,
    _wait_for_http,
    host_container,
    host_instance,
    stop_container,
    stop_instance,
)
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
from local_webpage_access.registry import Registry


# ---- fixtures ----------------------------------------------------------------


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


def _seed_container_instance(
    workspace: Workspace,
    registry: Registry,
    iid: str = "api",
    *,
    kind: Kind = Kind.PYTHON,
    has_database: bool = False,
    database_type: str | None = None,
    internal_port: int = 8000,
) -> InstanceManifest:
    """构造一个已导入的容器实例：current/ + manifest + registry。"""
    workspace.ensure_app_dirs(iid)
    current = workspace.app_current(iid)
    (current / "requirements.txt").write_text("fastapi")
    (current / "main.py").write_text("app = None")

    manifest = InstanceManifest(
        id=iid,
        name=iid,
        version="1",
        kind=kind,
        stack=["fastapi"],
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.SMALL,
        container=ContainerConfig(
            projectName=f"lwa-{iid}",
            internalPort=internal_port,
            composePath="docker/compose.yaml",
            dockerfilePath="docker/Dockerfile",
        ),
        entry={"install": "pip install -r requirements.txt", "start": "uvicorn main:app --host 0.0.0.0 --port 8000"},
        hasDatabase=has_database,
        database={"type": database_type} if has_database and database_type else None,
    )
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)
    return manifest


class _FakeRuntime:
    """替身 DockerRuntime，记录所有调用，不接触真实 Docker。"""

    _running_state = False  # 类变量，便于跨实例共享（模拟容器状态）
    calls: list[str] = []  # 类变量，跨实例累积（host_container 每次新建实例）

    def __init__(self, workspace=None, registry=None) -> None:
        self.workspace = workspace
        self.registry = registry

    @classmethod
    def ensure_available(cls) -> None:
        return None

    @classmethod
    def is_available(cls) -> bool:
        return True

    def is_running(self, iid: str) -> bool:
        return self._running_state

    def build(self, iid, *, build_id=None, **kw):
        type(self).calls.append("build")
        # 模拟真实 DockerRuntime.build：成功时 finish build 记录
        if build_id is not None and self.registry is not None:
            self.registry.finish_build(build_id, status="success")
        return None

    def up(self, iid, **kw):
        type(self).calls.append("up")
        return None

    def stop(self, iid, **kw):
        type(self).calls.append("stop")
        type(self)._running_state = False
        return None

    def down(self, iid, **kw):
        type(self).calls.append("down")
        type(self)._running_state = False
        return None

    def start(self, iid, **kw):
        type(self).calls.append("start")
        type(self)._running_state = True
        return None

    def restart(self, iid, **kw):
        type(self).calls.append("restart")
        return None

    def container_id(self, iid):
        return "abc123def"

    def image_id(self, iid):
        return "sha256:deadbeef"

    def status(self, iid):
        return None


@pytest.fixture()
def fake_runtime(monkeypatch):
    """替换 hosting.DockerRuntime 为 _FakeRuntime，并重置运行状态。"""
    _FakeRuntime._running_state = False
    _FakeRuntime.calls = []
    monkeypatch.setattr("local_webpage_access.hosting.DockerRuntime", _FakeRuntime)
    # 健康检查直接成功，避免真实 HTTP 等待
    monkeypatch.setattr("local_webpage_access.hosting._http_ok", lambda port, **kw: True)
    # 端口探测恒返回"未占用"，使分配确定性地取池首端口（避免宿主机真实占用干扰）
    monkeypatch.setattr("local_webpage_access.ports.is_port_in_use", lambda *a, **kw: False)
    monkeypatch.setattr("local_webpage_access.hosting.is_port_listening", lambda *a, **kw: False)
    return _FakeRuntime


# ---- host_container 成功路径 -----------------------------------------------


def test_host_container_success_generates_all_artifacts(
    workspace, registry, config, fake_runtime
) -> None:
    _seed_container_instance(workspace, registry, "api")
    manifest = host_container(workspace, config, registry, "api")

    # 状态
    assert manifest.status == Status.RUNNING
    assert manifest.desiredState == DesiredState.RUNNING
    assert manifest.lastError is None

    # 三件套已生成
    assert workspace.app_dockerfile_path("api").is_file()
    assert workspace.app_compose_path("api").is_file()
    assert workspace.app_env_path("api").is_file()

    # 端口分配
    assert manifest.container.hostPort == 21000
    assert 21000 in registry.allocated_ports()
    assert manifest.network.hostPort == 21000
    assert manifest.network.internalPort == 8000
    assert manifest.network.lanUrl is not None

    # containerId/imageId 已观测并写回
    assert manifest.container.containerId == "abc123def"
    assert manifest.container.imageId == "sha256:deadbeef"

    # build 记录成功
    builds = registry.list_builds("api")
    assert len(builds) == 1
    assert builds[0]["status"] == "success"

    # 事件
    events = registry.list_events("api")
    assert any(e["event_type"] == "start" for e in events)

    # registry 状态
    row = registry.get_instance("api")
    assert row["status"] == "running"
    assert row["desired_state"] == "running"
    crow = registry.get_container("api")
    assert crow["host_port"] == 21000
    assert crow["container_id"] == "abc123def"
    assert crow["image_id"] == "sha256:deadbeef"

    # 编排顺序：build 在 up 之前
    assert fake_runtime.calls.index("build") < fake_runtime.calls.index("up")


def test_host_container_downs_old_container_on_rebuild(
    workspace, registry, config, fake_runtime
) -> None:
    """重建场景：旧容器在跑时应先 down。"""
    _seed_container_instance(workspace, registry, "api")
    fake_runtime._running_state = True  # 旧容器在跑

    host_container(workspace, config, registry, "api")
    assert "down" in fake_runtime.calls
    # down 后继续 build + up
    assert "build" in fake_runtime.calls
    assert "up" in fake_runtime.calls


def test_host_container_health_check_recorded(
    workspace, registry, config, fake_runtime, monkeypatch
) -> None:
    """健康检查成功时记录 last_health_check_at。"""
    _seed_container_instance(workspace, registry, "api")
    host_container(workspace, config, registry, "api")
    row = registry.get_instance("api")
    assert row["last_health_check_at"] is not None


def test_host_container_health_check_failure_does_not_block(
    workspace, registry, config, fake_runtime, monkeypatch
) -> None:
    """健康检查失败不阻塞 RUNNING 标记（best-effort）。"""
    monkeypatch.setattr("local_webpage_access.hosting._http_ok", lambda port, **kw: False)
    _seed_container_instance(workspace, registry, "api")
    manifest = host_container(workspace, config, registry, "api")
    assert manifest.status == Status.RUNNING
    row = registry.get_instance("api")
    assert row["last_health_check_at"] is None  # 未记录健康


def test_host_container_sqlite_project_injects_database_url(
    workspace, registry, config, fake_runtime
) -> None:
    """SQLite 项目：.env 含 DATABASE_URL，Dockerfile 创建 /app/data。"""
    _seed_container_instance(
        workspace, registry, "api", has_database=True, database_type="sqlite"
    )
    host_container(workspace, config, registry, "api")
    env_text = workspace.app_env_path("api").read_text(encoding="utf-8")
    assert "DATABASE_URL=sqlite:////app/data/app.sqlite" in env_text
    dockerfile_text = workspace.app_dockerfile_path("api").read_text(encoding="utf-8")
    assert "RUN mkdir -p /app/data" in dockerfile_text


def test_host_container_node_project_uses_node_template(
    workspace, registry, config, fake_runtime
) -> None:
    """Node 项目生成 node:24-alpine Dockerfile。"""
    workspace.ensure_app_dirs("node-api")
    current = workspace.app_current("node-api")
    (current / "package.json").write_text('{"scripts":{"start":"node server.js"}}')
    manifest = InstanceManifest(
        id="node-api",
        name="node-api",
        version="1",
        kind=Kind.NODE,
        stack=["express"],
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.SMALL,
        container=ContainerConfig(
            projectName="lwa-node-api",
            internalPort=3000,
            composePath="docker/compose.yaml",
            dockerfilePath="docker/Dockerfile",
        ),
        entry={"install": "npm ci", "start": "npm run start"},
    )
    manifest.save(workspace.app_manifest_path("node-api"))
    registry.upsert_from_manifest(manifest)

    host_container(workspace, config, registry, "node-api")
    dockerfile_text = workspace.app_dockerfile_path("node-api").read_text(encoding="utf-8")
    assert "FROM node:24-alpine" in dockerfile_text


# ---- 失败路径 ---------------------------------------------------------------


def test_host_container_build_failure_marks_failed(
    workspace, registry, config, fake_runtime, monkeypatch
) -> None:
    _seed_container_instance(workspace, registry, "api")

    def fail_build(self, iid, *, build_id=None, **kw):
        if build_id is not None and self.registry is not None:
            self.registry.finish_build(build_id, status="failed", error_summary="build 失败")
        raise DockerError("build 失败", instance_id=iid)

    monkeypatch.setattr(fake_runtime, "build", fail_build)
    with pytest.raises(DockerError, match="build 失败"):
        host_container(workspace, config, registry, "api")

    row = registry.get_instance("api")
    assert row["status"] == "failed"
    assert row["last_error"]
    builds = registry.list_builds("api")
    assert builds[0]["status"] == "failed"
    assert builds[0]["error_summary"]
    events = registry.list_events("api")
    assert any(e["event_type"] == "error" for e in events)


def test_host_container_up_failure_marks_failed(
    workspace, registry, config, fake_runtime, monkeypatch
) -> None:
    """build 成功但 up 失败：build 保持 success（构建本身确实成功），实例 failed。"""
    _seed_container_instance(workspace, registry, "api")

    def fail_up(self, iid, **kw):
        raise DockerError("up 失败：端口冲突", instance_id=iid)

    monkeypatch.setattr(fake_runtime, "up", fail_up)
    with pytest.raises(DockerError, match="up 失败"):
        host_container(workspace, config, registry, "api")

    row = registry.get_instance("api")
    assert row["status"] == "failed"
    builds = registry.list_builds("api")
    # build 本身成功（up 失败不回滚 build 状态）
    assert builds[0]["status"] == "success"


def test_host_container_rejects_non_container_manifest(
    workspace, registry, config, fake_runtime
) -> None:
    """非 docker-compose 实例调用 host_container 抛 HostingError。"""
    from tests._helpers import make_static_manifest

    workspace.ensure_app_dirs("demo")
    m = make_static_manifest("demo")
    m.save(workspace.app_manifest_path("demo"))
    registry.upsert_from_manifest(m)

    with pytest.raises(HostingError, match="不是容器实例"):
        host_container(workspace, config, registry, "demo")


def test_host_instance_dispatches_to_host_container(
    workspace, registry, config, fake_runtime
) -> None:
    """host_instance 对容器实例派发到 host_container（端到端调度）。"""
    _seed_container_instance(workspace, registry, "api")
    manifest = host_instance(workspace, config, registry, "api")
    assert manifest.status == Status.RUNNING


# ---- stop_container ---------------------------------------------------------


def test_stop_container_calls_compose_stop(
    workspace, registry, config, fake_runtime
) -> None:
    _seed_container_instance(workspace, registry, "api")
    # 先"启动"
    host_container(workspace, config, registry, "api")
    allocated_before = set(registry.allocated_ports())

    manifest = stop_container(workspace, config, registry, "api")
    assert manifest.status == Status.STOPPED
    assert manifest.desiredState == DesiredState.STOPPED
    assert "stop" in fake_runtime.calls

    row = registry.get_instance("api")
    assert row["status"] == "stopped"
    assert row["desired_state"] == "stopped"
    # 端口保留（不释放）
    assert set(registry.allocated_ports()) == allocated_before


def test_stop_container_preserves_port_for_restart(
    workspace, registry, config, fake_runtime
) -> None:
    """stop 后端口仍登记，重建时 _ensure_container_port 应复用同一端口。"""
    _seed_container_instance(workspace, registry, "api")
    host_container(workspace, config, registry, "api")
    first_port = registry.get_container("api")["host_port"]

    stop_container(workspace, config, registry, "api")
    # 模拟容器已停（端口不再被 Docker 绑定）
    fake_runtime._running_state = False

    # 再次 host_container 应复用端口
    host_container(workspace, config, registry, "api")
    second_port = registry.get_container("api")["host_port"]
    assert second_port == first_port


def test_stop_instance_dispatches_to_stop_container(
    workspace, registry, config, fake_runtime
) -> None:
    _seed_container_instance(workspace, registry, "api")
    host_container(workspace, config, registry, "api")
    manifest = stop_instance(workspace, config, registry, "api")
    assert manifest.status == Status.STOPPED


# ---- _ensure_container_port 辅助 -------------------------------------------


def test_ensure_container_port_allocates_new(workspace, registry, config) -> None:
    """无历史端口时新分配。"""
    _seed_container_instance(workspace, registry, "api")
    port = _ensure_container_port(config, registry, "api")
    assert port in range(21000, 21051)
    assert port in registry.allocated_ports()


def test_ensure_container_port_reuses_existing(workspace, registry, config) -> None:
    """有历史端口且空闲时复用。"""
    _seed_container_instance(workspace, registry, "api")
    registry.upsert_container("api", {"projectName": "lwa-api", "internalPort": 8000,
                                       "composePath": "x", "dockerfilePath": "y",
                                       "hostPort": 21500})
    port = _ensure_container_port(config, registry, "api")
    assert port == 21500


# ---- _wait_for_http / _http_ok ---------------------------------------------


def test_http_ok_returns_true_on_success(monkeypatch) -> None:
    class _FakeResp:
        status = 200

    def fake_urlopen(url, timeout=None):
        return _FakeResp()

    import local_webpage_access.hosting as h

    monkeypatch.setattr(h.urllib.request, "urlopen", fake_urlopen)
    assert _http_ok(9999) is True


def test_http_ok_returns_false_on_exception(monkeypatch) -> None:
    def fake_urlopen(url, timeout=None):
        raise ConnectionError("no")

    import local_webpage_access.hosting as h

    monkeypatch.setattr(h.urllib.request, "urlopen", fake_urlopen)
    assert _http_ok(9999) is False


def test_wait_for_http_polls_until_success(monkeypatch) -> None:
    """_wait_for_http 在第 N 次探测成功时返回 True。"""
    calls = {"n": 0}

    def eventually_ok(port, **kw):
        calls["n"] += 1
        return calls["n"] >= 3

    monkeypatch.setattr("local_webpage_access.hosting._http_ok", eventually_ok)
    monkeypatch.setattr("local_webpage_access.hosting.time.sleep", lambda s: None)
    assert _wait_for_http(9999, attempts=5, delay=0) is True
    assert calls["n"] == 3


def test_wait_for_http_returns_false_after_timeout(monkeypatch) -> None:
    monkeypatch.setattr("local_webpage_access.hosting._http_ok", lambda port, **kw: False)
    monkeypatch.setattr("local_webpage_access.hosting.time.sleep", lambda s: None)
    assert _wait_for_http(9999, attempts=3, delay=0) is False
