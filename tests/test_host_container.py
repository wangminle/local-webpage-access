"""容器托管流程测试（WBS-15 / WBS-16）。

用 fake DockerRuntime 替换真实 Docker，验证 host_container/stop_container
的编排逻辑（生成 Dockerfile/Compose/.env、端口分配、build+up、健康检查、
manifest/registry 写回、失败诊断）。不依赖真实 Docker。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_webpage_access.docker_runtime import BindMount
from local_webpage_access.errors import DockerError, HostingError
from local_webpage_access.hosting import (
    _ensure_container_port,
    _http_ok,
    _wait_for_http,
    host_container,
    host_instance,
    start_container,
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
    # A.R01：SQLite 项目默认模拟消费 DATABASE_URL（向后兼容已有测试）
    if has_database and database_type == "sqlite":
        manifest.databaseConfig = {"consumesDatabaseUrl": True, "sourcePath": "config.py"}
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)
    return manifest


class _FakeRuntime:
    """替身 DockerRuntime，记录所有调用，不接触真实 Docker。"""

    _running_state = False  # 类变量，便于跨实例共享（模拟容器状态）
    calls: list[str] = []  # 类变量，跨实例累积（host_container 每次新建实例）
    _bind_mounts: list = []  # BUG-421：可配置的 bind mount 观测结果
    _bind_mounts_error: BaseException | None = None
    _rescue_result = 0  # BUG-424：可配置的救援救出文件数
    _down_error: BaseException | None = None  # BUG-423：可配置的 down 失败
    _ps_error: BaseException | None = None  # BUG-429：可配置的容器查询失败

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
        type(self)._running_state = True
        return None

    def stop(self, iid, **kw):
        type(self).calls.append("stop")
        type(self)._running_state = False
        return None

    def down(self, iid, **kw):
        type(self).calls.append("down")
        err = type(self)._down_error
        if err is not None:
            raise err
        type(self)._running_state = False
        return None

    def start(self, iid, **kw):
        type(self).calls.append("start")
        type(self)._running_state = True
        return None

    def restart(self, iid, **kw):
        type(self).calls.append("restart")
        return None

    def container_id(self, iid, *, all_containers: bool = False):
        # 运行中始终有 id；已停止但仍有容器时，仅 all_containers=True 能查到
        if type(self)._running_state:
            return "abc123def"
        if all_containers:
            return "abc123def"
        return "abc123def"

    def container_id_strict(self, iid, *, all_containers: bool = False):
        """BUG-429：严格版容器查询替身，查询失败抛错而非折叠为 None。"""
        err = type(self)._ps_error
        if err is not None:
            raise err
        return self.container_id(iid, all_containers=all_containers)

    def image_id(self, iid):
        return "sha256:deadbeef"

    def status(self, iid):
        return None

    def bind_mounts(self, iid, *, all_containers: bool = True):
        """BUG-421：只读挂载观测替身。"""
        type(self).calls.append("bind_mounts")
        err = type(self)._bind_mounts_error
        if err is not None:
            raise err
        return list(type(self)._bind_mounts)

    def rescue_container_data(self, iid, host_data, candidates, *, log_path=None, **kw):
        """BUG-205：记录调用（重建 down 前的数据救出）。"""
        type(self).calls.append("rescue")
        return type(self)._rescue_result


@pytest.fixture()
def fake_runtime(monkeypatch):
    """替换 hosting.DockerRuntime 为 _FakeRuntime，并重置运行状态。"""
    _FakeRuntime._running_state = False
    _FakeRuntime.calls = []
    _FakeRuntime._bind_mounts = []
    _FakeRuntime._bind_mounts_error = None
    _FakeRuntime._rescue_result = 0
    _FakeRuntime._down_error = None
    _FakeRuntime._ps_error = None
    monkeypatch.setattr("local_webpage_access.hosting.DockerRuntime", _FakeRuntime)
    # 健康检查直接成功，避免真实 HTTP 等待
    monkeypatch.setattr("local_webpage_access.hosting._http_ok", lambda port, **kw: True)
    # 端口探测恒返回"未占用"，使分配确定性地取池首端口（避免宿主机真实占用干扰）
    monkeypatch.setattr("local_webpage_access.ports.is_port_in_use", lambda *a, **kw: False)
    monkeypatch.setattr("local_webpage_access.ports.is_port_listening", lambda *a, **kw: False)
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


def test_host_container_rescues_data_before_down_on_rebuild(
    workspace, registry, config, fake_runtime
) -> None:
    """BUG-205：SQLite 容器重建时，down 前先把容器内数据救出到宿主 data/。

    顺序硬约束：rescue 必须在 down 之前——容器删除后数据无法再救出。
    """
    _seed_container_instance(
        workspace, registry, "api", has_database=True, database_type="sqlite"
    )
    fake_runtime._running_state = True  # 旧容器在跑 → 触发 down 重建

    host_container(workspace, config, registry, "api")
    assert "rescue" in fake_runtime.calls
    assert "down" in fake_runtime.calls
    assert fake_runtime.calls.index("rescue") < fake_runtime.calls.index("down")


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
    """Gate-C C.04：必选探针（存活）失败 → 实例标记 FAILED（不假报 RUNNING）。

    IMP-058 §6.5 评审决议第 3 点：必选探针失败不得假报 running。
    旧行为（健康检查 best-effort，不阻塞 RUNNING）已被 Gate-C 替代——
    首次部署的容器，存活探针超时 = 部署失败。
    """
    monkeypatch.setattr("local_webpage_access.hosting._http_ok", lambda port, **kw: False)
    _seed_container_instance(workspace, registry, "api")
    with pytest.raises(HostingError, match="必选探针未通过"):
        host_container(workspace, config, registry, "api")
    # 实例标记 FAILED（非 RUNNING）
    row = registry.get_instance("api")
    assert row["status"] == "failed"
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
    old = _seed_container_instance(workspace, registry, "api")
    old.container.containerId = "stale-container"
    old.container.imageId = "sha256:stale"
    old.save(workspace.app_manifest_path("api"))
    registry.upsert_from_manifest(old)

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
    failed_manifest = InstanceManifest.load(workspace.app_manifest_path("api"))
    assert failed_manifest.container.containerId is None
    assert failed_manifest.container.imageId is None
    assert registry.get_container("api")["container_id"] is None
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


def test_host_container_observation_failure_stays_failed_without_stale_id(
    workspace, registry, config, fake_runtime, monkeypatch
) -> None:
    """BUG-344 / BUG-300：观测不到新 containerId 时不得假报 running 或保留陈旧身份。"""
    old = _seed_container_instance(workspace, registry, "api")
    old.container.containerId = "stale-container"
    old.container.imageId = "sha256:stale"
    old.save(workspace.app_manifest_path("api"))
    registry.upsert_from_manifest(old)

    monkeypatch.setattr(
        fake_runtime,
        "container_id",
        lambda self, iid, *, all_containers=False: None,
    )
    monkeypatch.setattr(
        fake_runtime,
        "image_id",
        lambda self, iid: None,
    )

    with pytest.raises(HostingError, match="未能观测到新 containerId"):
        host_container(workspace, config, registry, "api")

    row = registry.get_instance("api")
    assert row["status"] == "failed"
    failed = InstanceManifest.load(workspace.app_manifest_path("api"))
    assert failed.container.containerId is None
    assert failed.container.imageId is None
    assert "down" in fake_runtime.calls


def test_start_container_keeps_ids_when_observe_returns_none(
    workspace, registry, config, fake_runtime, monkeypatch
) -> None:
    """BUG-344：轻量 start 观测为 None 时保留已落库 containerId/imageId。"""
    m = _seed_container_instance(workspace, registry, "api")
    m.container.containerId = "cid-keep"
    m.container.imageId = "sha256:keep"
    m.container.hostPort = 21000
    m.save(workspace.app_manifest_path("api"))
    registry.upsert_from_manifest(m)
    registry.upsert_container(
        "api",
        {
            "projectName": "lwa-api",
            "internalPort": 8000,
            "composePath": "x",
            "dockerfilePath": "y",
            "hostPort": 21000,
        },
    )

    monkeypatch.setattr(
        fake_runtime,
        "container_id",
        lambda self, iid, *, all_containers=False: (
            "cid-keep" if all_containers else None
        ),
    )
    monkeypatch.setattr(
        fake_runtime,
        "image_id",
        lambda self, iid: None,
    )

    started = start_container(workspace, config, registry, "api")
    assert started.container.containerId == "cid-keep"
    assert started.container.imageId == "sha256:keep"
    assert started.status == Status.RUNNING
    assert "start" in fake_runtime.calls
    assert "up" not in fake_runtime.calls
    assert "build" not in fake_runtime.calls


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
    port, fresh = _ensure_container_port(config, registry, "api")
    assert fresh is True
    assert port in range(21000, 21051)
    assert port in registry.allocated_ports()


def test_ensure_container_port_reuses_existing(workspace, registry, config) -> None:
    """有历史端口且空闲时复用。"""
    _seed_container_instance(workspace, registry, "api")
    registry.upsert_container("api", {"projectName": "lwa-api", "internalPort": 8000,
                                       "composePath": "x", "dockerfilePath": "y",
                                       "hostPort": 21500})
    port, fresh = _ensure_container_port(config, registry, "api")
    assert fresh is False
    assert port == 21500


# ---- _wait_for_http / _http_ok ---------------------------------------------


def test_http_ok_returns_true_on_success(monkeypatch) -> None:
    class _FakeResp:
        status = 200

    def fake_urlopen(url, timeout=None):
        return _FakeResp()

    import local_webpage_access.hosting as h

    monkeypatch.setattr(h, "urlopen_direct", fake_urlopen)
    assert _http_ok(9999) is True


def test_http_ok_returns_false_on_exception(monkeypatch) -> None:
    def fake_urlopen(url, timeout=None):
        raise ConnectionError("no")

    import local_webpage_access.hosting as h

    monkeypatch.setattr(h, "urlopen_direct", fake_urlopen)
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


# ---- BUG-421：start_container 挂载漂移防护 ---------------------------------


def _seed_deployed_sqlite(
    workspace: Workspace,
    registry: Registry,
    iid: str = "api",
) -> InstanceManifest:
    """已部署的 SQLite 容器实例（有 containerId / hostPort）。"""
    m = _seed_container_instance(
        workspace, registry, iid, has_database=True, database_type="sqlite"
    )
    m.container.containerId = "abc123def"
    m.container.imageId = "sha256:deadbeef"
    m.container.hostPort = 21000
    m.status = Status.STOPPED
    m.desiredState = DesiredState.STOPPED
    m.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(m)
    registry.upsert_container(
        iid,
        {
            "projectName": f"lwa-{iid}",
            "internalPort": 8000,
            "composePath": "docker/compose.yaml",
            "dockerfilePath": "docker/Dockerfile",
            "hostPort": 21000,
            "containerId": "abc123def",
            "imageId": "sha256:deadbeef",
        },
    )
    # 轻量 up 路径所需产物
    workspace.app_compose_path(iid).parent.mkdir(parents=True, exist_ok=True)
    workspace.app_compose_path(iid).write_text("services:\n  app:\n    image: x\n")
    workspace.app_env_path(iid).write_text("HOST_PORT=21000\n")
    return m


def _matching_data_mount(workspace: Workspace, iid: str = "api") -> BindMount:
    return BindMount(
        source=str(workspace.app_data(iid).resolve()),
        destination="/app/data",
        type="bind",
    )


def _drifted_data_mount() -> BindMount:
    return BindMount(
        source="/old/workspace/apps/api/data",
        destination="/app/data",
        type="bind",
    )


def test_mount_drift_running_consistent_skips_start(
    workspace, registry, config, fake_runtime
) -> None:
    """running + SQLite data mount 一致 → 跳过 start/down/up。"""
    _seed_deployed_sqlite(workspace, registry, "api")
    fake_runtime._running_state = True
    fake_runtime._bind_mounts = [_matching_data_mount(workspace, "api")]

    started = start_container(workspace, config, registry, "api")
    assert started.status == Status.RUNNING
    assert "start" not in fake_runtime.calls
    assert "down" not in fake_runtime.calls
    assert "up" not in fake_runtime.calls
    assert "rescue" not in fake_runtime.calls


def test_mount_drift_stopped_consistent_uses_compose_start(
    workspace, registry, config, fake_runtime
) -> None:
    """stopped + mount 一致 → compose start，不 down/up。"""
    _seed_deployed_sqlite(workspace, registry, "api")
    fake_runtime._running_state = False
    fake_runtime._bind_mounts = [_matching_data_mount(workspace, "api")]

    started = start_container(workspace, config, registry, "api")
    assert started.status == Status.RUNNING
    assert "start" in fake_runtime.calls
    assert "down" not in fake_runtime.calls
    assert "up" not in fake_runtime.calls
    assert "rescue" not in fake_runtime.calls


@pytest.mark.parametrize("running", [True, False], ids=["running", "stopped"])
def test_mount_drift_sqlite_data_triggers_rescue_down_up(
    workspace, registry, config, fake_runtime, monkeypatch, running: bool
) -> None:
    """SQLite data mount 漂移 + 宿主 data/ 为空 + 救援成功 → rescue → down → 清 ID → up。"""
    m = _seed_deployed_sqlite(workspace, registry, "api")
    assert m.container.containerId == "abc123def"
    fake_runtime._running_state = running
    fake_runtime._bind_mounts = [_drifted_data_mount()]
    fake_runtime._rescue_result = 3  # BUG-424：strict 救援必须救出文件才继续

    # up 后观测到新身份
    monkeypatch.setattr(
        fake_runtime,
        "container_id",
        lambda self, iid, *, all_containers=False: (
            "cid-new" if "up" in type(self).calls else "abc123def"
        ),
    )
    monkeypatch.setattr(
        fake_runtime,
        "image_id",
        lambda self, iid: (
            "sha256:new" if "up" in type(self).calls else "sha256:deadbeef"
        ),
    )

    started = start_container(workspace, config, registry, "api")
    assert started.status == Status.RUNNING
    assert started.container.containerId == "cid-new"
    assert started.container.imageId == "sha256:new"
    assert "start" not in fake_runtime.calls
    assert "rescue" in fake_runtime.calls
    assert "down" in fake_runtime.calls
    assert "up" in fake_runtime.calls
    assert fake_runtime.calls.index("rescue") < fake_runtime.calls.index("down")
    assert fake_runtime.calls.index("down") < fake_runtime.calls.index("up")


def test_mount_drift_inspect_failure_raises_without_destructive_ops(
    workspace, registry, config, fake_runtime
) -> None:
    """bind_mounts/inspect 失败 → 不 down/up，抛可诊断 HostingError。"""
    _seed_deployed_sqlite(workspace, registry, "api")
    fake_runtime._running_state = False
    fake_runtime._bind_mounts_error = DockerError(
        "读取容器挂载失败（实例 api，inspect Mounts，exit 1）：permission denied"
    )

    with pytest.raises(HostingError, match="挂载|漂移|inspect"):
        start_container(workspace, config, registry, "api")

    assert "down" not in fake_runtime.calls
    assert "up" not in fake_runtime.calls
    assert "start" not in fake_runtime.calls
    assert "rescue" not in fake_runtime.calls


def test_mount_drift_down_failure_aborts_before_up(
    workspace, registry, config, fake_runtime
) -> None:
    """BUG-423：漂移修复 down 失败 → HostingError 中止，禁止继续 up 复用旧挂载。"""
    _seed_deployed_sqlite(workspace, registry, "api")
    fake_runtime._running_state = True
    fake_runtime._bind_mounts = [_drifted_data_mount()]
    fake_runtime._rescue_result = 2
    fake_runtime._down_error = DockerError(
        "compose down failed", instance_id="api", action="down"
    )

    with pytest.raises(HostingError, match="down 失败.*已中止"):
        start_container(workspace, config, registry, "api")

    assert "rescue" in fake_runtime.calls
    assert "down" in fake_runtime.calls
    assert "up" not in fake_runtime.calls
    assert "start" not in fake_runtime.calls


def test_mount_drift_host_data_conflict_aborts_for_manual_check(
    workspace, registry, config, fake_runtime
) -> None:
    """BUG-424：漂移且宿主 data/ 非空 → 视为两侧数据冲突，中止并要求人工确认。"""
    _seed_deployed_sqlite(workspace, registry, "api")
    fake_runtime._running_state = True
    fake_runtime._bind_mounts = [_drifted_data_mount()]
    # 宿主 data/ 已有数据（裸 mv 后真实数据随工作区搬迁的典型形态）
    host_data = workspace.app_data("api")
    host_data.mkdir(parents=True, exist_ok=True)
    (host_data / "app.db").write_bytes(b"real-data")

    with pytest.raises(HostingError, match="非空|冲突|人工"):
        start_container(workspace, config, registry, "api")

    # 冲突时连 rescue 都不应尝试，更不得 down/up
    assert "rescue" not in fake_runtime.calls
    assert "down" not in fake_runtime.calls
    assert "up" not in fake_runtime.calls
    assert "start" not in fake_runtime.calls


def test_mount_drift_rescue_empty_aborts(
    workspace, registry, config, fake_runtime
) -> None:
    """BUG-424：漂移 + 宿主 data/ 为空 + 救援未救出任何文件 → 中止，不得继续。"""
    _seed_deployed_sqlite(workspace, registry, "api")
    fake_runtime._running_state = False
    fake_runtime._bind_mounts = [_drifted_data_mount()]
    fake_runtime._rescue_result = 0  # 救援失败/容器内无数据

    with pytest.raises(HostingError, match="未能从旧容器救出"):
        start_container(workspace, config, registry, "api")

    assert "rescue" in fake_runtime.calls
    assert "down" not in fake_runtime.calls
    assert "up" not in fake_runtime.calls
    assert "start" not in fake_runtime.calls


def test_mount_drift_rescue_exception_aborts(
    workspace, registry, config, fake_runtime, monkeypatch
) -> None:
    """BUG-424：救援过程异常 → strict 模式不得吞掉，抛 HostingError 中止。"""
    _seed_deployed_sqlite(workspace, registry, "api")
    fake_runtime._running_state = False
    fake_runtime._bind_mounts = [_drifted_data_mount()]

    def _boom(self, iid, host_data, candidates, **kw):
        type(self).calls.append("rescue")
        raise DockerError("docker cp failed", instance_id=iid, action="rescue")

    monkeypatch.setattr(_FakeRuntime, "rescue_container_data", _boom)

    with pytest.raises(HostingError, match="救援异常|已中止"):
        start_container(workspace, config, registry, "api")

    assert "rescue" in fake_runtime.calls
    assert "down" not in fake_runtime.calls
    assert "up" not in fake_runtime.calls
    assert "start" not in fake_runtime.calls


def test_mount_drift_ps_failure_aborts_without_start(
    workspace, registry, config, fake_runtime
) -> None:
    """BUG-429：容器查询失败不得当作"无容器"——中止，禁止 compose start 带旧挂载。"""
    _seed_deployed_sqlite(workspace, registry, "api")
    fake_runtime._running_state = False
    fake_runtime._ps_error = DockerError(
        "compose ps failed", instance_id="api", action="ps"
    )

    with pytest.raises(HostingError, match="无法查询容器状态"):
        start_container(workspace, config, registry, "api")

    assert "start" not in fake_runtime.calls
    assert "down" not in fake_runtime.calls
    assert "up" not in fake_runtime.calls
    assert "rescue" not in fake_runtime.calls


def test_mount_drift_non_sqlite_keeps_original_start(
    workspace, registry, config, fake_runtime
) -> None:
    """非 SQLite → 不检查挂载漂移，保持 compose start。"""
    m = _seed_container_instance(workspace, registry, "api")
    m.container.containerId = "abc123def"
    m.container.imageId = "sha256:deadbeef"
    m.container.hostPort = 21000
    m.save(workspace.app_manifest_path("api"))
    registry.upsert_from_manifest(m)
    fake_runtime._running_state = False
    # 即使配置了漂移挂载，非 SQLite 也不应触发重建
    fake_runtime._bind_mounts = [_drifted_data_mount()]

    started = start_container(workspace, config, registry, "api")
    assert started.status == Status.RUNNING
    assert "start" in fake_runtime.calls
    assert "down" not in fake_runtime.calls
    assert "up" not in fake_runtime.calls
    assert "bind_mounts" not in fake_runtime.calls


def test_mount_drift_sqlite_without_managed_data_mount_keeps_start(
    workspace, registry, config, fake_runtime
) -> None:
    """SQLite 但无管理 data mount → 保持原 start 行为。"""
    _seed_deployed_sqlite(workspace, registry, "api")
    fake_runtime._running_state = False
    # 只有非 data 的 bind（如代码目录），不算 LWA 管理 data mount
    fake_runtime._bind_mounts = [
        BindMount(source="/old/current", destination="/app", type="bind")
    ]

    started = start_container(workspace, config, registry, "api")
    assert started.status == Status.RUNNING
    assert "start" in fake_runtime.calls
    assert "down" not in fake_runtime.calls
    assert "up" not in fake_runtime.calls
    assert "rescue" not in fake_runtime.calls


# ---- BUG-422：派生路径回写 -------------------------------------------------


_OLD_APP = "/old/workspace/apps/api/current"
_OLD_COMPOSE = "/old/workspace/apps/api/docker/compose.yaml"
_OLD_DOCKERFILE = "/old/workspace/apps/api/docker/Dockerfile"
_EXTERNAL_ZIP = "/external/old.zip"


def _apply_stale_container_paths(
    workspace: Workspace,
    registry: Registry,
    manifest: InstanceManifest,
    iid: str = "api",
) -> None:
    """模拟裸 mv 后 manifest/registry 仍持有旧绝对路径。"""
    assert manifest.container is not None
    manifest.appPath = _OLD_APP
    manifest.sourceZipPath = _EXTERNAL_ZIP
    manifest.container.composePath = _OLD_COMPOSE
    manifest.container.dockerfilePath = _OLD_DOCKERFILE
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)


def _assert_derived_paths_refreshed(
    workspace: Workspace,
    registry: Registry,
    manifest: InstanceManifest,
    iid: str = "api",
) -> None:
    """断言成功 host/start 后派生路径已回写，外部 sourceZipPath 不变。"""
    expected_app = str(workspace.app_current(iid))
    expected_compose = str(workspace.app_compose_path(iid))
    expected_dockerfile = str(workspace.app_dockerfile_path(iid))

    assert manifest.appPath == expected_app
    assert manifest.sourceZipPath == _EXTERNAL_ZIP
    assert manifest.container is not None
    assert manifest.container.composePath == expected_compose
    assert manifest.container.dockerfilePath == expected_dockerfile

    # 盘上 manifest 与 registry 同步
    reloaded = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert reloaded.appPath == expected_app
    assert reloaded.sourceZipPath == _EXTERNAL_ZIP
    assert reloaded.container is not None
    assert reloaded.container.composePath == expected_compose
    assert reloaded.container.dockerfilePath == expected_dockerfile

    row = registry.get_instance(iid)
    assert row is not None
    assert row["app_path"] == expected_app
    assert row["source_zip_path"] == _EXTERNAL_ZIP
    crow = registry.get_container(iid)
    assert crow is not None
    assert crow["compose_path"] == expected_compose
    assert crow["dockerfile_path"] == expected_dockerfile


def test_derived_path_start_container_refreshes_stale_paths(
    workspace, registry, config, fake_runtime
) -> None:
    """start_container 成功后刷新陈旧 appPath/compose/dockerfile，保留外部 zip。"""
    m = _seed_container_instance(workspace, registry, "api")
    m.container.containerId = "abc123def"
    m.container.imageId = "sha256:deadbeef"
    m.container.hostPort = 21000
    m.status = Status.STOPPED
    m.desiredState = DesiredState.STOPPED
    m.save(workspace.app_manifest_path("api"))
    registry.upsert_from_manifest(m)
    _apply_stale_container_paths(workspace, registry, m, "api")
    fake_runtime._running_state = False

    started = start_container(workspace, config, registry, "api")
    assert started.status == Status.RUNNING
    _assert_derived_paths_refreshed(workspace, registry, started, "api")


def test_derived_path_host_container_refreshes_stale_paths(
    workspace, registry, config, fake_runtime
) -> None:
    """host_container 成功后刷新陈旧派生路径，保留外部 sourceZipPath。"""
    m = _seed_container_instance(workspace, registry, "api")
    _apply_stale_container_paths(workspace, registry, m, "api")

    hosted = host_container(workspace, config, registry, "api")
    assert hosted.status == Status.RUNNING
    _assert_derived_paths_refreshed(workspace, registry, hosted, "api")
