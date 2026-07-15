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


# ---- IMP-007：端口映射标签 ------------------------------------------------


def test_port_mapping_label_container(workspace, registry, config) -> None:
    """容器实例 internalPort≠hostPort 时生成 ``internalPort→hostPort`` 标签。"""
    _seed_container(workspace, registry, "api", host_port=21000)
    st = instance_status(workspace, config, registry, "api")
    assert st.port_mapping_label == "8000→21000"


def test_port_mapping_label_static_is_none(workspace, registry, config) -> None:
    """纯静态 HTML（manifest 无 internalPort）不展示误导性映射。"""
    _seed_static(workspace, registry, "demo", host_port=21100)
    st = instance_status(workspace, config, registry, "demo")
    assert st.port_mapping_label is None


def test_port_mapping_label_static_from_manifest_internal_port(
    workspace, registry, config
) -> None:
    """前端/静态项目：manifest.network.internalPort 与 hostPort 不同时展示映射。"""
    workspace.ensure_app_dirs("voiceprint")
    manifest = InstanceManifest(
        id="voiceprint",
        name="声纹演示",
        version="1",
        kind=Kind.STATIC,
        runtime=Runtime.SHARED_STATIC,
        servingMode=ServingMode.SHARED_STATIC,
        resourceProfile=ResourceProfile.TINY,
        status=Status.RUNNING,
        desiredState=DesiredState.RUNNING,
        static=StaticConfig(hostPort=18001, routeMode="name", routeHost="voiceprint"),
    )
    manifest.network.internalPort = 33001
    manifest.network.routeMode = "name"
    manifest.network.routeHost = "voiceprint"
    manifest.save(workspace.app_manifest_path("voiceprint"))
    registry.upsert_from_manifest(manifest)
    st = instance_status(workspace, config, registry, "voiceprint")
    assert st.host_port == 18001
    assert st.internal_port == 33001
    assert st.port_mapping_label == "33001→18001"
    assert st.route_host == "voiceprint"


def test_port_mapping_label_same_port_is_none(workspace, registry, config) -> None:
    """internalPort==hostPort 时映射冗余，标签为 None。"""
    _seed_container(workspace, registry, "api", host_port=8000)
    st = instance_status(workspace, config, registry, "api")
    # internalPort=8000, hostPort=8000 → 无需展示映射
    assert st.port_mapping_label is None


def test_port_mapping_label_in_to_dict(workspace, registry, config) -> None:
    """to_dict() 暴露 portMappingLabel 字段供 API 使用。"""
    _seed_container(workspace, registry, "api", host_port=21000)
    st = instance_status(workspace, config, registry, "api")
    d = st.to_dict()
    assert d["portMappingLabel"] == "8000→21000"
    assert d["internalPort"] == 8000
    assert d["hostPort"] == 21000


def test_port_mapping_label_none_in_to_dict_for_static(
    workspace, registry, config
) -> None:
    """静态实例 to_dict() 的 portMappingLabel 为 None。"""
    _seed_static(workspace, registry, "demo", host_port=21100)
    st = instance_status(workspace, config, registry, "demo")
    d = st.to_dict()
    assert d["portMappingLabel"] is None


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


# ---- 回归测试：BUG-048 ----------------------------------------------------
#
# BUG-048：构建进程崩溃后实例永久卡在 building，sync_status 又跳过 building 状态。
#          修复后 sync_status 对 building 状态做 stale 检测：超过阈值则回写 failed。


def test_sync_status_recovers_stale_building_with_build_row(
    workspace, registry, config, monkeypatch
) -> None:
    """building 状态且有超时的 running builds 行 → sync_status 回收为 failed（BUG-048）。"""
    from local_webpage_access import status as status_mod

    _seed_container(workspace, registry, "api")
    registry.update_status("api", Status.BUILDING.value)
    # 写一条 started_at 远早于现在的 builds 行
    old_ts = "2020-01-01T00:00:00"
    registry.add_build("api", status="running", started_at=old_ts)
    # 压低阈值让测试快速触发
    monkeypatch.setattr(status_mod, "_STALE_BUILDING_SECONDS", 0.0)

    changed = sync_status(workspace, config, registry, "api")

    assert changed == {"api": "failed"}
    row = registry.get_instance("api")
    assert row["status"] == "failed"
    assert "stale building" in row["last_error"].lower()
    # 孤儿 builds 行应被收尾为 failed
    builds = registry.list_builds("api")
    assert builds[0]["status"] == "failed"
    # 回收事件
    events = registry.list_events("api")
    assert any(e["event_type"] == "build_recover" for e in events)


def test_sync_status_keeps_recent_building(workspace, registry, config, monkeypatch) -> None:
    """building 状态但 builds 行 started_at 未超时 → 不回收（BUG-048 边界）。"""
    from local_webpage_access import status as status_mod
    from local_webpage_access.logging import now_iso

    _seed_container(workspace, registry, "api")
    registry.update_status("api", Status.BUILDING.value)
    # 刚开始的构建
    registry.add_build("api", status="running", started_at=now_iso())
    # 阈值设大，确保不触发
    monkeypatch.setattr(status_mod, "_STALE_BUILDING_SECONDS", 999999.0)

    changed = sync_status(workspace, config, registry, "api")

    # building 状态保持不变，不触发 observe 也不回收
    assert changed == {}
    assert registry.get_instance("api")["status"] == "building"


def test_sync_status_recovers_stale_building_no_build_row(
    workspace, registry, config, monkeypatch
) -> None:
    """building 状态但无 builds 行（如 host_static 路径）→ 用 updated_at 兜底（BUG-048）。"""
    from local_webpage_access import status as status_mod

    _seed_static(workspace, registry, "demo")
    registry.update_status("demo", Status.BUILDING.value)
    # 手动写入一个很久以前的 updated_at
    with registry.conn:
        registry.conn.execute(
            "UPDATE instances SET updated_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00", "demo"),
        )
    monkeypatch.setattr(status_mod, "_STALE_BUILDING_SECONDS", 0.0)

    changed = sync_status(workspace, config, registry, "demo")

    assert changed == {"demo": "failed"}
    assert registry.get_instance("demo")["status"] == "failed"


@pytest.mark.parametrize("build_status", ["success", "failed"])
def test_sync_status_recovers_building_with_old_finished_build(
    workspace, registry, config, monkeypatch, build_status
) -> None:
    """BUG-129：最新构建已结束很久但实例仍 building 时也应自动回收。"""
    from local_webpage_access import status as status_mod

    _seed_container(workspace, registry, "api")
    registry.update_status("api", Status.BUILDING.value)
    build_id = registry.add_build(
        "api", status=build_status, started_at="2020-01-01T00:00:00"
    )
    with registry.conn:
        registry.conn.execute(
            "UPDATE builds SET finished_at = ? WHERE id = ?",
            ("2020-01-01T01:00:00", build_id),
        )
    monkeypatch.setattr(status_mod, "_STALE_BUILDING_SECONDS", 0.0)

    changed = sync_status(workspace, config, registry, "api")

    assert changed == {"api": "failed"}
    assert registry.get_instance("api")["status"] == "failed"


# ---- 回归测试：BUG-119 ----------------------------------------------------


def test_add_build_fails_prior_running_siblings(workspace, registry) -> None:
    """BUG-119：新 add_build 立刻关闭同实例其它 running 行。"""
    _seed_container(workspace, registry, "api")
    old_id = registry.add_build("api", status="running", started_at="2020-01-01T00:00:00")
    new_id = registry.add_build("api", status="running")
    builds = {b["id"]: b for b in registry.list_builds("api")}
    assert builds[old_id]["status"] == "failed"
    assert "被后续构建取代" in (builds[old_id].get("error_summary") or "")
    assert builds[new_id]["status"] == "running"


def test_sync_status_fails_shadowed_orphan_running_build(
    workspace, registry, config, monkeypatch
) -> None:
    """BUG-119：实例已 running 且最新构建成功时，仍回收被遮蔽的旧 running 行。"""
    from local_webpage_access import status as status_mod
    from local_webpage_access.logging import now_iso

    _seed_container(workspace, registry, "api")
    registry.update_status("api", Status.RUNNING.value)
    # 直接 SQL 插入历史孤儿（绕过 add_build 的兄弟关闭，模拟修复前数据）
    with registry.conn:
        registry.conn.execute(
            "INSERT INTO builds(instance_id, status, started_at, log_path) "
            "VALUES (?, 'running', ?, NULL)",
            ("api", "2020-01-01T00:00:00+08:00"),
        )
        orphan_id = int(
            registry.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        )
    success_id = registry.add_build("api", status="running", started_at=now_iso())
    # add_build 会把孤儿标 failed；再手工改回 running 以模拟旧库状态
    with registry.conn:
        registry.conn.execute(
            "UPDATE builds SET status='running', finished_at=NULL, error_summary=NULL "
            "WHERE id=?",
            (orphan_id,),
        )
    registry.finish_build(success_id, status="success")
    monkeypatch.setattr(status_mod, "_STALE_BUILDING_SECONDS", 0.0)

    # observe 会改实例状态；桩掉避免依赖 docker
    monkeypatch.setattr(
        "local_webpage_access.lifecycle.observe_status",
        lambda *a, **kw: Status.RUNNING,
    )
    changed = sync_status(workspace, config, registry, "api")

    assert changed == {}
    assert registry.get_instance("api")["status"] == "running"
    by_id = {b["id"]: b for b in registry.list_builds("api")}
    assert by_id[orphan_id]["status"] == "failed"
    assert by_id[success_id]["status"] == "success"
