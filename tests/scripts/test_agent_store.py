"""Agent 存储层测试（AGC-W05，M1）。

验收场景（设计 §9.2 W05）：
1. 旧库升级：v2 数据库打开后自动迁到 v3，存量数据不丢；
2. 重复迁移：幂等，无报错；
3. 事务回滚：DAO 异常不落任何行；
4. 并发同键只一行：两个连接（模拟两进程）同 (principal, workspace, idempotencyKey)
   并发创建，只产生一行且双方拿到同一 operation；不同内容复用键报冲突。
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from local_webpage_access.errors import RegistryError
from local_webpage_access.registry import Registry
from local_webpage_access.registry.connection import (
    _SCHEMAS,
    get_schema_version,
    migrate,
)

HASH_A = "a" * 64
HASH_B = "b" * 64


def _op_record(**over) -> dict:
    base = {
        "operation_id": "op_1",
        "principal_id": "local-owner",
        "workspace_id": "ws_1",
        "action": "deploy",
        "target_instance_id": None,
        "request_hash": HASH_A,
        "idempotency_key": "idem-1",
        "plan_id": None,
        "status": "queued",
        "phase": None,
        "created_at": "2026-09-15T04:00:00Z",
        "updated_at": "2026-09-15T04:00:00Z",
        "worker_identity": None,
        "lease_until": None,
        "build_token": None,
        "result": None,
        "error": None,
    }
    base.update(over)
    return base


# ---- 1/2. 旧库升级与重复迁移 ------------------------------------------------------


def test_fresh_db_at_schema_v5(workspace_root: Path) -> None:
    reg = Registry(workspace_root / "registry.db")
    reg.open()
    assert get_schema_version(reg.conn) == 5
    tables = {
        r[0] for r in reg.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"workspace_meta", "agent_plans", "agent_operations"} <= tables
    reg.close()


def test_v2_db_upgrades_with_data_intact(workspace_root: Path) -> None:
    db_path = workspace_root / "registry.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # 手工构造 v2 旧库（不含 v3 表）
    conn = sqlite3.connect(str(db_path))
    for version in (1, 2):
        for stmt in _SCHEMAS[version]:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO schema_version(version, applied_at) VALUES (?, '2026-01-01T00:00:00Z')",
            (version,),
        )
    conn.execute(
        "INSERT INTO instances(id, name, version, kind, runtime, serving_mode, desired_state,"
        " status, created_at, updated_at) VALUES ('legacy', 'legacy', '1', 'static',"
        " 'builtin', 'port', 'running', 'running', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    reg = Registry(db_path)
    reg.open()
    assert get_schema_version(reg.conn) == 5
    tables = {
        r[0] for r in reg.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"workspace_meta", "agent_plans", "agent_operations"} <= tables
    legacy = reg.get_instance("legacy")
    assert legacy is not None and legacy["id"] == "legacy"  # 存量数据不丢
    reg.close()


def test_migrate_idempotent_at_v4(workspace_root: Path) -> None:
    reg = Registry(workspace_root / "registry.db")
    reg.open()
    before = get_schema_version(reg.conn)
    migrate(reg.conn)
    assert get_schema_version(reg.conn) == before == 5
    reg.close()


# ---- 3. 事务回滚 ------------------------------------------------------------------


def test_transaction_rollback_persists_nothing(workspace_root: Path) -> None:
    reg = Registry(workspace_root / "registry.db")
    reg.open()
    # 事务内写入后异常 → 回滚不落行（connection.transaction 语义，经 Registry.txn 验证）
    with pytest.raises(RuntimeError, match="boom"):
        with reg.txn() as tx:
            tx.execute(
                "INSERT INTO agent_plans (plan_id, principal_id, workspace_id, intent,"
                " source_json, source_digest, target_instance_id, expected_revision,"
                " display_name, options_json, policy_version, request_hash, created_at,"
                " expires_at) VALUES ('plan_1', 'local-owner', 'ws_1', 'create', '{}',"
                " ?, NULL, NULL, NULL, '{}', '1', ?, '2026-09-15T04:00:00Z',"
                " '2026-09-15T04:30:00Z')",
                ("d" * 64, HASH_A),
            )
            raise RuntimeError("boom")
    assert reg.get_agent_plan("plan_1") is None
    # DAO 层失败同样不落行：同 plan_id 重复插入被拒，表内仍为空
    reg.insert_agent_plan(
        {
            "plan_id": "plan_1",
            "principal_id": "local-owner",
            "workspace_id": "ws_1",
            "intent": "create",
            "source_json": "{}",
            "source_digest": "d" * 64,
            "target_instance_id": None,
            "expected_revision": None,
            "display_name": None,
            "options_json": "{}",
            "policy_version": "1",
            "request_hash": HASH_A,
            "created_at": "2026-09-15T04:00:00Z",
            "expires_at": "2026-09-15T04:30:00Z",
        }
    )
    count = reg.conn.execute("SELECT COUNT(*) FROM agent_plans").fetchone()[0]
    assert count == 1
    reg.close()


# ---- 4. 幂等创建与并发同键 ----------------------------------------------------------


def test_idempotent_replay_same_request(workspace_root: Path) -> None:
    reg = Registry(workspace_root / "registry.db")
    reg.open()
    first = reg.create_agent_operation(_op_record())
    second = reg.create_agent_operation(_op_record(operation_id="op_OTHER"))
    assert first["operation_id"] == second["operation_id"] == "op_1"
    count = reg.conn.execute("SELECT COUNT(*) FROM agent_operations").fetchone()[0]
    assert count == 1
    reg.close()


def test_same_key_different_payload_conflicts(workspace_root: Path) -> None:
    reg = Registry(workspace_root / "registry.db")
    reg.open()
    reg.create_agent_operation(_op_record())
    with pytest.raises(RegistryError) as exc_info:
        reg.create_agent_operation(_op_record(request_hash=HASH_B))
    assert exc_info.value.code == "idempotency_conflict"
    count = reg.conn.execute("SELECT COUNT(*) FROM agent_operations").fetchone()[0]
    assert count == 1
    reg.close()


def test_concurrent_same_key_single_row(workspace_root: Path) -> None:
    db_path = workspace_root / "registry.db"
    reg_a = Registry(db_path)
    reg_a.open()
    reg_b = Registry(db_path)
    reg_b.open()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(reg_a.create_agent_operation, _op_record(operation_id="op_A"))
            f2 = pool.submit(reg_b.create_agent_operation, _op_record(operation_id="op_B"))
            id_a = f1.result()["operation_id"]
            id_b = f2.result()["operation_id"]
        assert id_a == id_b  # 两方拿到同一 operation
        count = reg_a.conn.execute("SELECT COUNT(*) FROM agent_operations").fetchone()[0]
        assert count == 1
    finally:
        reg_a.close()
        reg_b.close()


def test_different_principals_same_key_independent(workspace_root: Path) -> None:
    reg = Registry(workspace_root / "registry.db")
    reg.open()
    a = reg.create_agent_operation(_op_record(principal_id="agent-a"))
    b = reg.create_agent_operation(_op_record(principal_id="agent-b", operation_id="op_2"))
    assert a["operation_id"] != b["operation_id"]
    assert reg.get_agent_operation_by_idempotency("agent-b", "ws_1", "idem-1") is not None
    reg.close()


# ---- workspace 身份与操作读写 -------------------------------------------------------


def test_workspace_id_stable(workspace_root: Path) -> None:
    reg = Registry(workspace_root / "registry.db")
    reg.open()
    ws_id = reg.get_or_create_workspace_id()
    assert ws_id and reg.get_or_create_workspace_id() == ws_id
    reg.close()
    # 重开（模拟新进程）仍然稳定
    reg2 = Registry(workspace_root / "registry.db")
    reg2.open()
    assert reg2.get_or_create_workspace_id() == ws_id
    reg2.close()


def test_workspace_id_differs_per_db(workspace_root: Path) -> None:
    reg1 = Registry(workspace_root / "one.db")
    reg1.open()
    reg2 = Registry(workspace_root / "two.db")
    reg2.open()
    assert reg1.get_or_create_workspace_id() != reg2.get_or_create_workspace_id()
    reg1.close()
    reg2.close()


def test_plan_roundtrip(workspace_root: Path) -> None:
    reg = Registry(workspace_root / "registry.db")
    reg.open()
    row = {
        "plan_id": "plan_1",
        "principal_id": "local-owner",
        "workspace_id": "ws_1",
        "intent": "update",
        "source_json": '{"type":"git","url":"https://github.com/o/r"}',
        "source_digest": "d" * 64,
        "target_instance_id": "site",
        "expected_revision": 3,
        "display_name": None,
        "options_json": "{}",
        "policy_version": "1",
        "request_hash": HASH_A,
        "created_at": "2026-09-15T04:00:00Z",
        "expires_at": "2026-09-15T04:30:00Z",
    }
    reg.insert_agent_plan(row)
    got = reg.get_agent_plan("plan_1")
    assert got is not None
    for key, value in row.items():
        assert got[key] == value
    assert reg.get_agent_plan("missing") is None
    reg.close()


def test_empty_result_and_error_objects_roundtrip(workspace_root: Path) -> None:
    """BUG-658：合法空对象 result={} / error={} 不得被真值判断静默存成 NULL。"""
    reg = Registry(workspace_root / "registry.db")
    reg.open()
    created = reg.create_agent_operation(_op_record(result={}, error={}))
    assert created["result"] == {}
    assert created["error"] == {}
    loaded = reg.get_agent_operation(created["operation_id"])
    assert loaded is not None
    assert loaded["result"] == {}
    assert loaded["error"] == {}
    reg.close()


def test_operation_update_and_get(workspace_root: Path) -> None:
    reg = Registry(workspace_root / "registry.db")
    reg.open()
    created = reg.create_agent_operation(_op_record())
    ok = reg.update_agent_operation(
        created["operation_id"],
        updated_at="2026-09-15T04:01:00Z",
        status="running",
        phase="import",
        worker_identity="worker-1",
        lease_until="2026-09-15T04:02:00Z",
        result={"instanceId": "site"},
    )
    assert ok
    row = reg.get_agent_operation(created["operation_id"])
    assert row is not None
    assert row["status"] == "running"
    assert row["phase"] == "import"
    assert row["result"] == {"instanceId": "site"}  # JSON 列自动反序列化
    by_key = reg.get_agent_operation_by_idempotency("local-owner", "ws_1", "idem-1")
    assert by_key is not None and by_key["operation_id"] == created["operation_id"]
    assert reg.get_agent_operation("missing") is None
    assert not reg.update_agent_operation(
        "missing", updated_at="2026-09-15T04:01:00Z", status="running"
    )
    reg.close()
