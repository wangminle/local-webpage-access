"""Agent 操作恢复、取消与过期清理测试（AGC-W11，M1）。

验收场景（设计 §9.2 W11 / §6.3）：
1. 受理后断连：客户端不再轮询，worker 独立推进到终态；
2. 创建中崩溃：running 操作租约过期/worker 死亡 → interrupted，不重复创建实例；
3. PID 身份：存活 worker 的操作不误回收；
4. build token 关联与构建相位真实取消；
5. 过期清理：终态操作 7 天保留；过期计划连快照删除；活跃引用不误删。
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from local_webpage_access.agent.auth import M1_LOCAL_OWNER_SCOPES, Principal
from local_webpage_access.agent.contracts import (
    ApplyDeploymentInput,
    LifecycleInput,
    OperationAction,
    PlanDeploymentInput,
    ServerDirectorySource,
)
from local_webpage_access.agent.operations import (
    AgentOperationService,
    AgentWorker,
    _LeaseHeartbeat,
    _iso,
)
from local_webpage_access.agent.service import AgentService
from local_webpage_access.config import AgentConfig, Config, PortPool
from local_webpage_access.paths import Workspace
from tests.conftest import stop_workspace_test_builtins


def _principal() -> Principal:
    return Principal("local-owner", "local_owner", M1_LOCAL_OWNER_SCOPES)


def _config(allowed: Path) -> Config:
    return Config(
        staticGateway="builtin",
        portPool=PortPool(start=21000, end=21050),
        agent=AgentConfig(allowedSourceRoots=[allowed]),
    )


@pytest.fixture(autouse=True)
def _stop_agent_recovery_builtins(workspace: Workspace) -> None:
    yield
    stop_workspace_test_builtins(workspace)


def _static_src(root: Path) -> Path:
    src = root / "site"
    src.mkdir(parents=True)
    (src / "index.html").write_text("<h1>recovery</h1>\n", encoding="utf-8")
    return src


def _make_plan(workspace: Workspace, config: Config, registry, src: Path, **kwargs):
    svc = AgentService(workspace, config, registry, _principal())
    return svc.plan_deployment(
        PlanDeploymentInput(
            source=ServerDirectorySource(type="server_directory", path=str(src)),
            intent="create",
            **kwargs,
        )
    )


def _accepted_deploy(workspace: Workspace, config: Config, registry, tmp_path: Path, key="k-1"):
    src = _static_src(tmp_path)
    plan = _make_plan(workspace, config, registry, src, displayName=f"site-{key}")
    ops = AgentOperationService(workspace, config, registry, _principal())
    return ops, ops.apply_deployment(
        ApplyDeploymentInput(planId=plan.planId, idempotencyKey=key)
    )


# ---- 受理后断连 -------------------------------------------------------------------


def test_disconnect_after_acceptance_does_not_affect_operation(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    """受理（202）后客户端断连（不再查询）：worker 仍推进到 succeeded。"""
    config = _config(tmp_path)
    ops, accepted = _accepted_deploy(workspace, config, registry, tmp_path)
    # 客户端断连：此后不再调用 get_operation，直接由 worker 推进
    worker = AgentWorker(workspace, config, registry)
    record = worker.claim_next()
    assert record is not None
    worker._execute(record)
    row = registry.get_agent_operation(accepted.operationId)
    assert row is not None and row["status"] == "succeeded"


# ---- 创建中崩溃恢复 ------------------------------------------------------------------


def _seed_running_operation(registry, *, lease_offset: int, pid: int, op_id: str) -> None:
    now = datetime.now(timezone.utc)
    registry.create_agent_operation(
        {
            "operation_id": op_id,
            "principal_id": "local-owner",
            "workspace_id": registry.get_or_create_workspace_id(),
            "action": "deploy",
            "target_instance_id": "half-created",
            "request_hash": "ab" * 32,
            "idempotency_key": f"key-{op_id}",
            "plan_id": None,
            "status": "running",
            "phase": "import",
            "created_at": _iso(now),
            "updated_at": _iso(now),
            "worker_identity": f"{pid}:test-token",
            "lease_until": _iso(now + timedelta(seconds=lease_offset)),
        }
    )


def test_crashed_worker_operation_marked_interrupted(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    """worker 进程已死 + 租约过期 → interrupted；不自动重复 create。"""
    config = _config(tmp_path)
    dead_pid = 999999
    _seed_running_operation(registry, lease_offset=-60, pid=dead_pid, op_id="op_dead")

    worker = AgentWorker(workspace, config, registry)
    assert worker.recover_stale() == 1

    row = registry.get_agent_operation("op_dead")
    assert row is not None
    assert row["status"] == "interrupted"
    error = row["error"] or {}
    assert error.get("code") == "interrupted"
    assert error.get("nextActions")
    # 未知态不重建：不新增任何实例、不重复受理
    assert registry.list_instances() == []
    assert registry.claim_next_agent_operation(
        worker_identity="w", now=_iso(datetime.now(timezone.utc)),
        lease_until=_iso(datetime.now(timezone.utc)),
    ) is None


def test_alive_worker_with_fresh_lease_not_recovered(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    """PID 身份存活 + 租约新鲜 → 不得误回收。"""
    config = _config(tmp_path)
    _seed_running_operation(registry, lease_offset=60, pid=os.getpid(), op_id="op_alive")

    worker = AgentWorker(workspace, config, registry)
    assert worker.recover_stale() == 0
    row = registry.get_agent_operation("op_alive")
    assert row is not None and row["status"] == "running"


def test_alive_pid_but_expired_lease_recovered(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    """PID 活着但租约过期（心跳线程卡死）→ 仍回收。"""
    config = _config(tmp_path)
    _seed_running_operation(registry, lease_offset=-1, pid=os.getpid(), op_id="op_stale")
    worker = AgentWorker(workspace, config, registry)
    assert worker.recover_stale() == 1
    row = registry.get_agent_operation("op_stale")
    assert row is not None and row["status"] == "interrupted"


# ---- build token 关联与构建相位取消 ----------------------------------------------------


def test_build_token_synced_and_build_phase_cancel(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    """operation 在 build 相位：心跳关联 build token；cancel 联动 BuildQueue 真实取消。"""
    from local_webpage_access.build_queue import get_build_queue

    from tests._helpers import make_static_manifest

    config = _config(tmp_path)
    ops = AgentOperationService(workspace, config, registry, _principal())
    # 构建事件对 instances 有外键：先种入目标实例
    workspace.ensure_app_dirs("half-created")
    manifest = make_static_manifest("half-created")
    manifest.save(workspace.app_manifest_path("half-created"))
    registry.upsert_from_manifest(manifest)
    _seed_running_operation(registry, lease_offset=600, pid=os.getpid(), op_id="op_build")
    registry.update_agent_operation(
        "op_build",
        updated_at=_iso(datetime.now(timezone.utc)),
        phase="build",
    )

    worker = AgentWorker(workspace, config, registry)
    queue = get_build_queue(config, registry)
    entered = threading.Event()
    release = threading.Event()

    def slow_builder(instance_id: str) -> str:
        entered.set()
        release.wait(timeout=30)
        return "done"

    from local_webpage_access.errors import LifecycleError

    def run_build() -> None:
        with pytest.raises(LifecycleError, match="已取消"):
            queue.run("half-created", slow_builder)

    build_thread = threading.Thread(target=run_build, daemon=True)
    build_thread.start()
    assert entered.wait(timeout=10), "构建应已进入 building"

    # 心跳同步 build token 到 operation
    heartbeat = _LeaseHeartbeat(worker, "op_build")
    heartbeat.set_phase("build")
    try:
        heartbeat._sync_build_token()
    finally:
        heartbeat.stop()
    row = registry.get_agent_operation("op_build")
    assert row is not None and row["build_token"], "build 相位应关联 build token"
    assert row["build_token"] == queue.current_build_token("half-created")

    # 构建相位取消：operation → cancelling，builder 被取消
    result = ops.cancel_operation("op_build")
    assert result.status.value == "cancelling"
    release.set()
    build_thread.join(timeout=15)
    assert not build_thread.is_alive()


# ---- 过期清理 ------------------------------------------------------------------------


def test_sweep_terminal_operations_and_expired_plans(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    now = datetime.now(timezone.utc)
    old = _iso(now - timedelta(days=8))
    ws_id = registry.get_or_create_workspace_id()

    # 终态操作 >7 天 → 删除；活跃操作不动
    registry.create_agent_operation(
        {
            "operation_id": "op_old",
            "principal_id": "local-owner",
            "workspace_id": ws_id,
            "action": "stop",
            "target_instance_id": "demo",
            "request_hash": "cd" * 32,
            "idempotency_key": "old-key",
            "plan_id": None,
            "status": "succeeded",
            "phase": None,
            "created_at": old,
            "updated_at": old,
        }
    )
    registry.create_agent_operation(
        {
            "operation_id": "op_active",
            "principal_id": "local-owner",
            "workspace_id": ws_id,
            "action": "stop",
            "target_instance_id": "demo",
            "request_hash": "ef" * 32,
            "idempotency_key": "active-key",
            "plan_id": None,
            "status": "running",
            "phase": "start",
            "created_at": old,
            "updated_at": old,
            "lease_until": _iso(now + timedelta(seconds=600)),
        }
    )

    # 过期计划：一个无引用（应删），一个被活跃操作引用（保留）
    src = _static_src(tmp_path)
    stale_plan = _make_plan(workspace, config, registry, src, displayName="stale-plan")
    pinned_plan = _make_plan(workspace, config, registry, src, displayName="pinned-plan")
    for pid in (stale_plan.planId, pinned_plan.planId):
        plan_dir = workspace.root / "run" / "agent-plans" / pid
        assert plan_dir.is_dir()
    past = _iso(now - timedelta(seconds=3600))
    with registry.txn() as tx:
        tx.execute(
            "UPDATE agent_plans SET expires_at = ? WHERE plan_id IN (?, ?)",
            (past, stale_plan.planId, pinned_plan.planId),
        )
        tx.execute(
            "UPDATE agent_operations SET plan_id = ? WHERE operation_id = 'op_active'",
            (pinned_plan.planId,),
        )

    worker = AgentWorker(workspace, config, registry)
    worker.sweep()

    assert registry.get_agent_operation("op_old") is None
    assert registry.get_agent_operation("op_active") is not None, "活跃操作不得清理"
    assert registry.get_agent_plan(stale_plan.planId) is None
    assert not (workspace.root / "run" / "agent-plans" / stale_plan.planId).exists()
    assert registry.get_agent_plan(pinned_plan.planId) is not None, "被活跃操作引用的计划保留"
    assert (workspace.root / "run" / "agent-plans" / pinned_plan.planId).is_dir()


def test_cancel_running_operation_at_phase_boundary(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    """running 操作取消后，worker 在相位边界收敛为 cancelled（不假报成功）。"""
    from tests._helpers import make_static_manifest

    config = _config(tmp_path)
    workspace.ensure_app_dirs("demo")
    manifest = make_static_manifest("demo")
    manifest.save(workspace.app_manifest_path("demo"))
    registry.upsert_from_manifest(manifest)

    ops = AgentOperationService(workspace, config, registry, _principal())
    accepted = ops.submit_lifecycle(
        OperationAction.stop,
        LifecycleInput(instanceId="demo", expectedRevision=1, idempotencyKey="k-1"),
    )
    worker = AgentWorker(workspace, config, registry)
    record = worker.claim_next()
    assert record is not None

    cancel = ops.cancel_operation(accepted.operationId)
    assert cancel.status.value == "cancelling"

    worker._execute(record)
    row = registry.get_agent_operation(accepted.operationId)
    assert row is not None and row["status"] == "cancelled"


# ---- BUG-691：本进程身份的悬挂行回收 ---------------------------------------------------


def _seed_running_with_identity(
    registry, *, identity: str, lease_offset: int, op_id: str
) -> None:
    now = datetime.now(timezone.utc)
    registry.create_agent_operation(
        {
            "operation_id": op_id,
            "principal_id": "local-owner",
            "workspace_id": registry.get_or_create_workspace_id(),
            "action": "deploy",
            "target_instance_id": "some-site",
            "request_hash": "ab" * 32,
            "idempotency_key": f"key-{op_id}",
            "plan_id": None,
            "status": "running",
            "phase": "build",
            "created_at": _iso(now),
            "updated_at": _iso(now),
            "worker_identity": identity,
            "lease_until": _iso(now + timedelta(seconds=lease_offset)),
        }
    )


def test_own_identity_expired_lease_recovered(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    """BUG-691：身份是本 worker 自己、租约已过期且不在执行 → 必须回收。

    修复前只要 worker_identity 等于自己就跳过，悬挂 running 永不回收
    （现有测试用 ``{pid}:test-token`` 假身份，未覆盖真实 identity 分支）。
    """
    config = _config(tmp_path)
    worker = AgentWorker(workspace, config, registry)
    _seed_running_with_identity(
        registry, identity=worker.identity, lease_offset=-60, op_id="op_own_stale"
    )

    assert worker.recover_stale() == 1
    row = registry.get_agent_operation("op_own_stale")
    assert row is not None and row["status"] == "interrupted"


def test_inflight_operation_not_recovered_even_if_lease_expired(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    """BUG-691 修复的安全侧：本进程正在执行（in-flight）的操作不回收。"""
    from local_webpage_access.agent.operations import _INFLIGHT_LOCK, _INFLIGHT_OPS

    config = _config(tmp_path)
    worker = AgentWorker(workspace, config, registry)
    _seed_running_with_identity(
        registry, identity=worker.identity, lease_offset=-60, op_id="op_inflight"
    )
    with _INFLIGHT_LOCK:
        _INFLIGHT_OPS.add("op_inflight")
    try:
        assert worker.recover_stale() == 0
        row = registry.get_agent_operation("op_inflight")
        assert row is not None and row["status"] == "running"
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT_OPS.discard("op_inflight")


# ---- BUG-692：cancelling 期间租约续约 --------------------------------------------------


def test_lease_renewal_allowed_while_cancelling(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    """BUG-692：cancelling 期间心跳续约必须成功（否则长取消占死队列名额）。"""
    now = datetime.now(timezone.utc)
    config = _config(tmp_path)
    worker = AgentWorker(workspace, config, registry)
    _seed_running_with_identity(
        registry, identity=worker.identity, lease_offset=600, op_id="op_cancel"
    )
    moved = registry.cas_agent_operation_status(
        "op_cancel",
        expected_statuses=("running",),
        new_status="cancelling",
        now=_iso(now),
    )
    assert moved

    ok = registry.renew_agent_operation_lease(
        "op_cancel",
        worker_identity=worker.identity,
        now=_iso(now),
        lease_until=_iso(now + timedelta(seconds=30)),
    )
    assert ok, "cancelling 期间应可续约"
    # 身份不符（已被接管/恢复）仍拒绝——续约的身份门禁不变
    assert not registry.renew_agent_operation_lease(
        "op_cancel",
        worker_identity="other-worker:token",
        now=_iso(now),
        lease_until=_iso(now + timedelta(seconds=30)),
    )
