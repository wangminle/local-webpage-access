"""BUG-706～709：Agent M1 审查（CHK-352）确认缺陷的修复回归。

- BUG-706（P1）：生命周期操作 expectedRevision 只在 worker 锁外预检；
  CLI/daemon 在窗口内更新后，旧请求仍会 stop/restart 并发更新后的实例。
- BUG-707（P2）：同键重试先被计划 TTL / 当前 revision 校验阻断，无法取回
  原 operation，违背 §6.3 幂等恢复约定。
- BUG-708（P2）：跨字段校验错误的 ctx 携带 ValueError 对象，HTTP 500 /
  MCP 序列化 TypeError（应 422 / 错误结果）。
- BUG-709（P2）：HTTP OpenAPI 缺 requestBody 与响应模型；MCP 未发布
  output schema。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from local_webpage_access.config import AgentConfig, Config, PortPool
from local_webpage_access.manager_api import create_app, ensure_token
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry
from tests._helpers import make_static_manifest
from tests.conftest import stop_workspace_test_builtins

BASE = "/api/agent/v1"


@pytest.fixture()
def agent_env(workspace_root: Path):
    ws = Workspace(workspace_root)
    ws.ensure_workspace_dirs()
    config = Config(
        staticGateway="builtin",
        portPool=PortPool(start=21000, end=21050),
        agent=AgentConfig(allowedSourceRoots=[workspace_root]),
    )
    reg = Registry(ws.db_path)
    reg.open()
    token = ensure_token(ws)
    app = create_app(ws, config, reg, token=token)
    yield ws, config, reg, app, token
    stop_workspace_test_builtins(ws)
    reg.close()


def _client(app) -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1:17800", client=("127.0.0.1", 50000))


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _static_src(root: Path) -> Path:
    src = root / "site"
    src.mkdir(parents=True, exist_ok=True)
    (src / "index.html").write_text("<h1>guards</h1>\n", encoding="utf-8")
    return src


def _seed_instance(ws: Workspace, reg: Registry, instance_id: str = "demo") -> None:
    ws.ensure_app_dirs(instance_id)
    manifest = make_static_manifest(instance_id)
    manifest.save(ws.app_manifest_path(instance_id))
    reg.upsert_from_manifest(manifest)


# ---- BUG-706：生命周期锁内复验 expectedRevision ---------------------------------


def test_stop_rejects_stale_revision_inside_lock(
    agent_env, monkeypatch
) -> None:
    """BUG-706：锁外预检通过后、取得实例锁前 revision 被外部更新 → 锁内拒绝。"""
    ws, config, reg, _app, _token = agent_env
    _seed_instance(ws, reg)
    assert reg.get_revision("demo") == 1

    import local_webpage_access.lifecycle as lifecycle
    from local_webpage_access.errors import LifecycleError

    real_lock = lifecycle.instance_lock

    class _BumpLock:
        """进入实例锁时模拟 CLI/daemon 在预检后完成一次更新（r1→r2）。"""

        def __init__(self, cm):
            self._cm = cm

        def __enter__(self):
            current = reg.get_revision("demo")
            reg.cas_increment_revision("demo", current)
            return self._cm.__enter__()

        def __exit__(self, *exc):
            return self._cm.__exit__(*exc)

    def fake_lock(workspace, instance_id, **kwargs):
        return _BumpLock(real_lock(workspace, instance_id, **kwargs))

    monkeypatch.setattr(lifecycle, "instance_lock", fake_lock)

    from local_webpage_access.lifecycle import stop_instance_op

    with pytest.raises(LifecycleError) as exc:
        stop_instance_op(ws, config, reg, "demo", expected_revision=1)
    assert exc.value.code == "revision_conflict"
    # 实例期望状态未被旧请求改动
    assert reg.get_instance("demo")["desired_state"] == make_static_manifest(
        "demo"
    ).desiredState.value


def test_stop_without_expected_revision_keeps_old_behavior(
    agent_env, monkeypatch
) -> None:
    """BUG-706 对照：缺省 None 不复验——既有 CLI 调用行为不变。"""
    ws, config, reg, _app, _token = agent_env
    _seed_instance(ws, reg)

    called = []
    monkeypatch.setattr(
        "local_webpage_access.hosting.stop_instance",
        lambda *a, **k: called.append(True) or make_static_manifest("demo"),
    )
    from local_webpage_access.lifecycle import stop_instance_op

    manifest = stop_instance_op(ws, config, reg, "demo")
    assert called, "缺省 None 不做复验，直接执行既有停止路径"
    assert manifest.id == "demo"


def test_worker_lifecycle_conflict_during_phase_switch(agent_env) -> None:
    """BUG-706 端到端：预检后、handler 执行前 revision 变化 → failed + revision_conflict。"""
    ws, config, reg, _app, _token = agent_env
    _seed_instance(ws, reg)

    from local_webpage_access.agent.auth import M1_LOCAL_OWNER_SCOPES, Principal
    from local_webpage_access.agent.contracts import (
        LifecycleInput,
        OperationAction,
    )
    from local_webpage_access.agent.operations import AgentOperationService, AgentWorker

    ops = AgentOperationService(
        ws, config, reg, Principal("local-owner", "local_owner", M1_LOCAL_OWNER_SCOPES)
    )
    accepted = ops.submit_lifecycle(
        OperationAction.stop,
        LifecycleInput(instanceId="demo", expectedRevision=1, idempotencyKey="k-1"),
    )

    worker = AgentWorker(ws, config, reg)
    original_advance = worker._advance
    bumped = {"done": False}

    def _advance_bump(op_id, phase):
        # 模拟预检通过后、生命周期 handler 取锁前，CLI/daemon 完成更新 r1→r2
        if not bumped["done"]:
            bumped["done"] = True
            current = reg.get_revision("demo")
            reg.cas_increment_revision("demo", current)
        return original_advance(op_id, phase)

    worker._advance = _advance_bump
    record = worker.claim_next()
    assert record is not None
    worker._execute(record)

    view = ops.get_operation(accepted.operationId)
    assert view.status.value == "failed", view.result
    assert view.error is not None and view.error.code.value == "revision_conflict"


# ---- BUG-707：幂等重放先于易变校验 ----------------------------------------------


def test_apply_replay_survives_plan_expiry(agent_env) -> None:
    """BUG-707：同键同 plan 在计划过期后重试 → 取回原 operation（非 needs_input）。"""
    ws, config, reg, _app, _token = agent_env
    src = _static_src(ws.root)

    from local_webpage_access.agent.auth import M1_LOCAL_OWNER_SCOPES, Principal
    from local_webpage_access.agent.contracts import (
        ApplyDeploymentInput,
        PlanDeploymentInput,
        ServerDirectorySource,
    )
    from local_webpage_access.agent.operations import AgentOperationService
    from local_webpage_access.agent.service import AgentService

    principal = Principal("local-owner", "local_owner", M1_LOCAL_OWNER_SCOPES)
    created = datetime(2026, 9, 18, 0, 0, tzinfo=timezone.utc)
    svc = AgentService(ws, config, reg, principal, now=lambda: created)
    plan = svc.plan_deployment(
        PlanDeploymentInput(
            source=ServerDirectorySource(type="server_directory", path=str(src)),
            intent="create",
        )
    )
    ops = AgentOperationService(ws, config, reg, principal, now=lambda: created)
    first = ops.apply_deployment(
        ApplyDeploymentInput(planId=plan.planId, idempotencyKey="k-1")
    )

    # 1 小时后同键同 plan 重试：即使计划已过期也必须重放原 operation
    stale_ops = AgentOperationService(
        ws, config, reg, principal, now=lambda: created + timedelta(hours=1)
    )
    replay = stale_ops.apply_deployment(
        ApplyDeploymentInput(planId=plan.planId, idempotencyKey="k-1")
    )
    assert replay.operationId == first.operationId
    assert replay.status.value == first.status.value


def test_apply_replay_rejects_same_key_different_plan(agent_env) -> None:
    """BUG-707 边界：重放等价性仍生效——同键不同 plan 报 idempotency_conflict。"""
    ws, config, reg, _app, _token = agent_env
    src = _static_src(ws.root)

    from local_webpage_access.agent.auth import M1_LOCAL_OWNER_SCOPES, Principal
    from local_webpage_access.agent.contracts import (
        ApplyDeploymentInput,
        PlanDeploymentInput,
        ServerDirectorySource,
    )
    from local_webpage_access.agent.operations import AgentOperationService
    from local_webpage_access.agent.service import AgentService, AgentServiceError

    principal = Principal("local-owner", "local_owner", M1_LOCAL_OWNER_SCOPES)
    svc = AgentService(ws, config, reg, principal)
    plan_a = svc.plan_deployment(
        PlanDeploymentInput(
            source=ServerDirectorySource(type="server_directory", path=str(src)),
            intent="create",
            displayName="a",
        )
    )
    plan_b = svc.plan_deployment(
        PlanDeploymentInput(
            source=ServerDirectorySource(type="server_directory", path=str(src)),
            intent="create",
            displayName="b",
        )
    )
    ops = AgentOperationService(ws, config, reg, principal)
    ops.apply_deployment(ApplyDeploymentInput(planId=plan_a.planId, idempotencyKey="k"))
    with pytest.raises(AgentServiceError) as exc:
        ops.apply_deployment(
            ApplyDeploymentInput(planId=plan_b.planId, idempotencyKey="k")
        )
    assert exc.value.code == "idempotency_conflict"


def test_lifecycle_replay_survives_revision_change(agent_env) -> None:
    """BUG-707：同键同参数 lifecycle 在实例被其他通道更新后 → 取回原 operation。"""
    ws, config, reg, _app, _token = agent_env
    _seed_instance(ws, reg)

    from local_webpage_access.agent.auth import M1_LOCAL_OWNER_SCOPES, Principal
    from local_webpage_access.agent.contracts import LifecycleInput, OperationAction
    from local_webpage_access.agent.operations import AgentOperationService
    from local_webpage_access.agent.service import AgentServiceError

    principal = Principal("local-owner", "local_owner", M1_LOCAL_OWNER_SCOPES)
    ops = AgentOperationService(ws, config, reg, principal)
    first = ops.submit_lifecycle(
        OperationAction.stop,
        LifecycleInput(instanceId="demo", expectedRevision=1, idempotencyKey="k-1"),
    )
    # 其他通道把 revision 推进到 2
    reg.cas_increment_revision("demo", 1)

    replay = ops.submit_lifecycle(
        OperationAction.stop,
        LifecycleInput(instanceId="demo", expectedRevision=1, idempotencyKey="k-1"),
    )
    assert replay.operationId == first.operationId

    # 同键不同参数仍拒绝
    with pytest.raises(AgentServiceError) as exc:
        ops.submit_lifecycle(
            OperationAction.stop,
            LifecycleInput(instanceId="demo", expectedRevision=2, idempotencyKey="k-1"),
        )
    assert exc.value.code == "idempotency_conflict"


# ---- BUG-708：跨字段校验错误 JSON 安全 ------------------------------------------


def test_logs_missing_target_returns_422_not_500(agent_env) -> None:
    """BUG-708：GET /logs 不带目标 → 422 needs_input，错误体可 JSON 解析。"""
    _ws, _config, _reg, app, token = agent_env
    client = _client(app)
    resp = client.get(f"{BASE}/logs", headers=_auth(token))
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"]["code"] == "needs_input"
    assert "issues" in (body["error"].get("detail") or {})
    # ctx.error 已降级为字符串，不再携带 ValueError 对象
    issues = body["error"]["detail"]["issues"]
    assert json.dumps(issues), "issues 必须可 JSON 序列化"


def test_plans_update_missing_target_returns_422_not_500(agent_env) -> None:
    """BUG-708：POST /plans update 缺 targetInstanceId → 422（跨字段校验）。"""
    _ws, _config, _reg, app, token = agent_env
    client = _client(app)
    resp = client.post(
        f"{BASE}/plans",
        headers=_auth(token),
        json={"source": {"type": "server_directory", "path": "/tmp"}, "intent": "update"},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"]["code"] == "needs_input"
    assert json.dumps(body)


def test_mcp_get_logs_invalid_args_returns_json_safe_error(tmp_path: Path) -> None:
    """BUG-708（MCP 侧）：lwa_get_logs({}) 返回错误结果而非序列化崩溃。"""
    root = tmp_path / "ws"
    root.mkdir()
    (root / "local-web.yml").write_text("managerPort: 17899\n", encoding="utf-8")
    ws = Workspace(root)
    ws.ensure_workspace_dirs()

    from local_webpage_access.mcp.client_bridge import AgentBridge

    bridge = AgentBridge(ws)
    result, error = bridge.call_tool("lwa_get_logs", {})
    assert result is None
    assert error is not None
    assert error["code"] == "needs_input"
    assert json.dumps(error), "MCP 错误结果必须可 JSON 序列化（BUG-708）"


# ---- BUG-709：契约 schema 完整发布 --------------------------------------------


def test_openapi_publishes_request_body_and_response_models(agent_env) -> None:
    """BUG-709：/plans、/deployments 与生命周期 POST 有 requestBody；响应有模型。"""
    _ws, _config, _reg, app, _token = agent_env
    spec = app.openapi()

    plans = spec["paths"][f"{BASE}/plans"]["post"]
    assert "requestBody" in plans, "POST /plans 必须发布请求体 schema"
    assert "application/json" in plans["requestBody"]["content"]
    assert plans["requestBody"]["content"]["application/json"]["schema"]
    plans_resp = plans["responses"]["200"]["content"]["application/json"]["schema"]
    assert plans_resp.get("$ref") or plans_resp.get("properties"), (
        "POST /plans 200 响应必须有模型（不再空 schema）"
    )

    deployments = spec["paths"][f"{BASE}/deployments"]["post"]
    assert "requestBody" in deployments
    deploy_schema = deployments["responses"]["202"]["content"]["application/json"]["schema"]
    assert deploy_schema.get("$ref"), "202 响应应引用 OperationAccepted 组件"
    assert "OperationAccepted" in spec["components"]["schemas"]

    for action in ("start", "stop", "restart", "rebuild"):
        route = spec["paths"][f"{BASE}/instances/{{instance_id}}/{action}"]["post"]
        assert "requestBody" in route, f"{action} 必须发布请求体 schema"

    logs = spec["paths"][f"{BASE}/logs"]["get"]
    logs_schema = logs["responses"]["200"]["content"]["application/json"]["schema"]
    assert logs_schema.get("$ref") or logs_schema.get("properties")


def test_mcp_tools_publish_output_schema() -> None:
    """BUG-709（MCP 侧）：tool_to_mcp 发布与 TOOL_SPECS 共源的 output schema。"""
    from local_webpage_access.agent.contracts import TOOL_SPECS
    from local_webpage_access.mcp.server import tool_to_mcp

    published = 0
    for spec in TOOL_SPECS.values():
        tool = tool_to_mcp(spec)
        assert tool.input_schema, f"{spec.name} 必须有 input schema"
        assert tool.output_schema, f"{spec.name} 必须发布 output schema（BUG-709）"
        assert tool.output_schema == spec.output_model.model_json_schema(), (
            f"{spec.name} output schema 必须与契约 output_model 共源"
        )
        json.dumps(tool.output_schema)
        published += 1
    assert published == len(TOOL_SPECS)
