"""registry 模块测试（WBS-05）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_web_access.models import (
    ContainerConfig,
    DesiredState,
    InstanceManifest,
    Kind,
    ResourceProfile,
    Runtime,
    ServingMode,
    StaticConfig,
    Status,
)
from local_web_access.registry import Registry
from local_web_access.registry.connection import get_schema_version, migrate


@pytest.fixture()
def registry(workspace_root: Path) -> Registry:
    workspace_root.mkdir(parents=True, exist_ok=True)
    db_path = workspace_root / "registry" / "local-web.db"
    reg = Registry(db_path)
    reg.open()
    yield reg
    reg.close()


def _static_manifest(mid: str = "demo") -> InstanceManifest:
    return InstanceManifest(
        id=mid,
        name="Demo",
        version="1",
        kind=Kind.STATIC,
        runtime=Runtime.SHARED_STATIC,
        servingMode=ServingMode.SHARED_STATIC,
        resourceProfile=ResourceProfile.TINY,
        static=StaticConfig(hostPort=18001),
        desiredState=DesiredState.STOPPED,
        status=Status.PENDING,
    )


def _container_manifest(mid: str = "api") -> InstanceManifest:
    return InstanceManifest(
        id=mid,
        name="API",
        version="1",
        kind=Kind.PYTHON,
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.SMALL,
        container=ContainerConfig(
            projectName=f"lwa-{mid}",
            internalPort=8000,
            composePath="docker/compose.yaml",
            dockerfilePath="docker/Dockerfile",
        ),
        desiredState=DesiredState.STOPPED,
        status=Status.PENDING,
    )


# ---- 迁移 -------------------------------------------------------------------


def test_migrate_creates_tables(registry: Registry) -> None:
    tables = {
        r[0]
        for r in registry.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    for expected in (
        "schema_version",
        "instances",
        "containers",
        "static_sites",
        "ports",
        "events",
        "builds",
        "resources",
    ):
        assert expected in tables


def test_migrate_idempotent(registry: Registry) -> None:
    # 再次迁移不应报错，版本不变
    version_before = get_schema_version(registry.conn)
    migrate(registry.conn)
    assert get_schema_version(registry.conn) == version_before


def test_migrate_runs_on_fresh_db(workspace_root: Path) -> None:
    db_path = workspace_root / "registry" / "local-web.db"
    workspace_root.mkdir(parents=True, exist_ok=True)
    reg = Registry(db_path)
    reg.open()
    assert get_schema_version(reg.conn) == 1
    reg.close()


# ---- 实例同步 ---------------------------------------------------------------


def test_upsert_from_manifest_static(registry: Registry) -> None:
    m = _static_manifest()
    registry.upsert_from_manifest(m, app_path="/apps/demo", source_zip_path="/x.zip")
    row = registry.get_instance("demo")
    assert row is not None
    assert row["kind"] == "static"
    assert row["runtime"] == "shared-static"
    assert row["resource_profile"] == "tiny"
    assert json.loads(row["stack_json"]) == []
    assert row["app_path"] == "/apps/demo"
    # 静态站点应同步写入
    site = registry.get_static_site("demo")
    assert site is not None
    assert site["gateway"] == "caddy"


def test_upsert_from_manifest_container(registry: Registry) -> None:
    m = _container_manifest()
    registry.upsert_from_manifest(m)
    container = registry.get_container("api")
    assert container is not None
    assert container["compose_project"] == "lwa-api"
    assert container["internal_port"] == 8000
    assert container["memory_limit"] == "512m"


def test_upsert_is_update_on_conflict(registry: Registry) -> None:
    m = _static_manifest()
    registry.upsert_from_manifest(m)
    m.status = Status.RUNNING
    m.touch()
    registry.upsert_from_manifest(m)
    row = registry.get_instance("demo")
    assert row["status"] == "running"
    assert registry.total_count() == 1


def test_list_instances(registry: Registry) -> None:
    registry.upsert_from_manifest(_static_manifest("a"))
    registry.upsert_from_manifest(_container_manifest("b"))
    ids = {r["id"] for r in registry.list_instances()}
    assert ids == {"a", "b"}


def test_instance_exists(registry: Registry) -> None:
    registry.upsert_from_manifest(_static_manifest())
    assert registry.instance_exists("demo")
    assert not registry.instance_exists("missing")


# ---- 状态更新 ---------------------------------------------------------------


def test_update_status(registry: Registry) -> None:
    registry.upsert_from_manifest(_static_manifest())
    registry.update_status("demo", "failed", last_error="boom")
    row = registry.get_instance("demo")
    assert row["status"] == "failed"
    assert row["last_error"] == "boom"


def test_update_status_with_desired(registry: Registry) -> None:
    registry.upsert_from_manifest(_static_manifest())
    registry.update_status("demo", "stopped", desired_state="stopped")
    row = registry.get_instance("demo")
    assert row["desired_state"] == "stopped"


def test_record_started_and_health(registry: Registry) -> None:
    registry.upsert_from_manifest(_static_manifest())
    registry.record_started("demo")
    registry.record_health_check("demo")
    row = registry.get_instance("demo")
    assert row["last_started_at"] is not None
    assert row["last_health_check_at"] is not None


def test_delete_instance_cascades(registry: Registry) -> None:
    m = _static_manifest()
    registry.upsert_from_manifest(m)
    registry.allocate_port("demo", 18001)
    registry.add_event("demo", "info", "created")
    registry.delete_instance("demo")
    assert registry.get_instance("demo") is None
    assert registry.get_static_site("demo") is None
    assert 18001 not in registry.allocated_ports()
    assert registry.list_events("demo") == []


# ---- 端口 -------------------------------------------------------------------


def test_allocate_and_release_port(registry: Registry) -> None:
    registry.upsert_from_manifest(_static_manifest())
    registry.allocate_port("demo", 18010)
    assert 18010 in registry.allocated_ports()
    assert registry.port_owner(18010) == "demo"
    registry.release_port(18010)
    assert 18010 not in registry.allocated_ports()


def test_release_instance_ports(registry: Registry) -> None:
    registry.upsert_from_manifest(_static_manifest())
    registry.allocate_port("demo", 18010)
    registry.allocate_port("demo", 18011)
    registry.release_instance_ports("demo")
    assert registry.allocated_ports() == []


def test_port_owner_none(registry: Registry) -> None:
    assert registry.port_owner(99999) is None


# ---- 事件 / 构建 / 资源 -----------------------------------------------------


def test_add_and_list_events(registry: Registry) -> None:
    registry.upsert_from_manifest(_static_manifest())
    eid = registry.add_event("demo", "import", "imported zip")
    assert eid > 0
    events = registry.list_events("demo")
    assert len(events) == 1
    assert events[0]["event_type"] == "import"


def test_add_and_finish_build(registry: Registry) -> None:
    registry.upsert_from_manifest(_container_manifest())
    bid = registry.add_build("api", status="running", log_path="logs/build.log")
    registry.finish_build(bid, status="success")
    builds = registry.list_builds("api")
    assert len(builds) == 1
    assert builds[0]["status"] == "success"
    assert builds[0]["finished_at"] is not None


def test_finish_build_with_error(registry: Registry) -> None:
    registry.upsert_from_manifest(_container_manifest())
    bid = registry.add_build("api")
    registry.finish_build(bid, status="failed", error_summary="npm install OOM")
    builds = registry.list_builds("api")
    assert builds[0]["error_summary"] == "npm install OOM"


def test_upsert_resources(registry: Registry) -> None:
    registry.upsert_from_manifest(_static_manifest())
    registry.upsert_resources(
        "demo",
        source_size_bytes=1024,
        public_size_bytes=2048,
        last_cpu_percent=1.5,
    )
    res = registry.get_resources("demo")
    assert res is not None
    assert res["source_size_bytes"] == 1024
    assert res["last_cpu_percent"] == 1.5
    # 再次 upsert 应更新而非新增
    registry.upsert_resources("demo", source_size_bytes=4096)
    res2 = registry.get_resources("demo")
    assert res2["source_size_bytes"] == 4096


# ---- 统计 -------------------------------------------------------------------


def test_status_counts(registry: Registry) -> None:
    s = _static_manifest()
    s.status = Status.RUNNING
    registry.upsert_from_manifest(s)
    c = _container_manifest()
    c.status = Status.FAILED
    registry.upsert_from_manifest(c)
    counts = registry.status_counts()
    assert counts.get("running") == 1
    assert counts.get("failed") == 1


def test_total_count(registry: Registry) -> None:
    assert registry.total_count() == 0
    registry.upsert_from_manifest(_static_manifest())
    assert registry.total_count() == 1


# ---- 事务 / 错误 ------------------------------------------------------------


def test_transaction_rolls_back_on_error(registry: Registry) -> None:
    registry.upsert_from_manifest(_static_manifest())
    # 模拟一次失败的写：先正常写，再故意触发唯一约束冲突
    try:
        with registry.txn() as tx:
            tx.execute(
                "UPDATE instances SET status = ? WHERE id = ?",
                ("building", "demo"),
            )
            # 故意重复主键
            tx.execute(
                "INSERT INTO ports(port, instance_id, status, created_at) "
                "VALUES (1, 'demo', 'x', 't')",
            )
            tx.execute(
                "INSERT INTO ports(port, instance_id, status, created_at) "
                "VALUES (1, 'demo', 'x', 't')",
            )
    except Exception:
        pass
    # 回滚后 status 不应变成 building
    row = registry.get_instance("demo")
    assert row["status"] == "pending"


def test_registry_requires_open(workspace_root: Path) -> None:
    reg = Registry(workspace_root / "x.db")
    with pytest.raises(Exception):
        _ = reg.conn


def test_context_manager(workspace_root: Path) -> None:
    workspace_root.mkdir(parents=True, exist_ok=True)
    db_path = workspace_root / "registry" / "local-web.db"
    with Registry(db_path) as reg:
        reg.upsert_from_manifest(_static_manifest())
        assert reg.total_count() == 1
    # 退出后连接应关闭
    assert reg._conn is None


# ---- 回归测试：BUG-005 ----------------------------------------------------
#
# BUG-005：upsert_from_manifest 只 upsert 当前 runtime 子表，runtime 切换后
# static_sites / containers 另一侧旧行残留，造成同一实例两份矛盾配置。


def test_upsert_clears_static_site_when_switching_to_container(registry: Registry) -> None:
    """BUG-005：static → container 切换后，static_sites 旧行应被清除。"""
    registry.upsert_from_manifest(_static_manifest("demo"))
    assert registry.get_static_site("demo") is not None
    assert registry.get_container("demo") is None

    registry.upsert_from_manifest(_container_manifest("demo"))

    assert registry.get_container("demo") is not None
    # 不能两边同时残留
    assert registry.get_static_site("demo") is None


def test_upsert_clears_container_when_switching_to_static(registry: Registry) -> None:
    """BUG-005：container → static 切换后，containers 旧行应被清除。"""
    registry.upsert_from_manifest(_container_manifest("demo"))
    assert registry.get_container("demo") is not None
    assert registry.get_static_site("demo") is None

    registry.upsert_from_manifest(_static_manifest("demo"))

    assert registry.get_static_site("demo") is not None
    assert registry.get_container("demo") is None


# ---- 回归测试：BUG-017 ----------------------------------------------------
#
# BUG-017：allocate_port 此前用 INSERT OR REPLACE，两个并发分配同时选中同一
#          空闲端口时，后写者覆盖前者归属。改用 INSERT OR IGNORE + rowcount +
#          归属校验，返回 False 告知竞争输家。


def test_allocate_port_first_call_succeeds(registry: Registry) -> None:
    """BUG-017：首次登记端口返回 True。"""
    registry.upsert_from_manifest(_static_manifest("inst-a"))
    assert registry.allocate_port("inst-a", 20010) is True
    assert registry.port_owner(20010) == "inst-a"


def test_allocate_port_same_instance_idempotent(registry: Registry) -> None:
    """BUG-017：同一实例重复登记同一端口仍返回 True（幂等）。"""
    registry.upsert_from_manifest(_static_manifest("inst-a"))
    assert registry.allocate_port("inst-a", 20010) is True
    # 再次登记：端口已属于 inst-a，不算冲突
    assert registry.allocate_port("inst-a", 20010) is True
    assert registry.port_owner(20010) == "inst-a"


def test_allocate_port_rejects_other_instance(registry: Registry) -> None:
    """BUG-017：端口已被其他实例占有时，新实例登记返回 False，归属不变。"""
    registry.upsert_from_manifest(_static_manifest("inst-a"))
    registry.upsert_from_manifest(_static_manifest("inst-b"))
    assert registry.allocate_port("inst-a", 20010) is True
    # inst-b 想抢同一端口：必须失败，且不能改写归属
    assert registry.allocate_port("inst-b", 20010) is False
    assert registry.port_owner(20010) == "inst-a"
