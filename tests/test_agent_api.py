"""Agent HTTP API 测试（AGC-W12，M1）。

验收场景（设计 §9.2 W12）：
1. 202 受理 / 401 无凭据 / 403 LAN 拒绝 / 409 冲突 / 422 契约校验；
2. 路径参数校验与旧 API 隔离（SPA catch-all 不吞新路由）；
3. worker 在 lifespan 内运行：apply 后轮询到 succeeded（端到端）。
"""

from __future__ import annotations

import time
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


def _client(app, *, client_addr=("127.0.0.1", 50000)) -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1:17800", client=client_addr)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _static_src(root: Path) -> Path:
    src = root / "mysite"
    src.mkdir(parents=True, exist_ok=True)
    (src / "index.html").write_text("<h1>api</h1>\n", encoding="utf-8")
    return src


def _seed_instance(ws: Workspace, reg: Registry, instance_id: str = "demo") -> None:
    ws.ensure_app_dirs(instance_id)
    manifest = make_static_manifest(instance_id)
    manifest.save(ws.app_manifest_path(instance_id))
    reg.upsert_from_manifest(manifest)


# ---- 鉴权门禁 ------------------------------------------------------------------


def test_capabilities_ok(agent_env) -> None:
    _ws, _config, reg, app, token = agent_env
    client = _client(app)
    resp = client.get(f"{BASE}/capabilities", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["workspaceId"] == reg.get_or_create_workspace_id()
    assert body["contractVersion"] == "1"
    assert body["inputTypes"] == ["server_directory", "git"]
    assert body["observedAt"]


def test_unauthenticated_without_token(agent_env) -> None:
    _ws, _config, _reg, app, _token = agent_env
    client = _client(app)
    resp = client.get(f"{BASE}/capabilities")
    assert resp.status_code == 401
    err = resp.json()["error"]
    assert err["code"] == "unauthenticated"
    # CHK-345 P3：契约 message 不得再套一层 LwaError ``[code] `` 前缀
    assert not err["message"].startswith("["), err["message"]
    assert "凭据" in err["message"]


def test_lan_client_rejected_even_with_token(agent_env) -> None:
    _ws, _config, _reg, app, token = agent_env
    client = _client(app, client_addr=("10.0.0.8", 50000))
    resp = client.get(f"{BASE}/capabilities", headers=_auth(token))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "permission_denied"


def test_query_token_not_accepted(agent_env) -> None:
    """Agent 通道只收 Header 凭据，?token= 一律不收（防 URL 泄漏）。"""
    _ws, _config, _reg, app, token = agent_env
    client = _client(app)
    resp = client.get(f"{BASE}/capabilities?token={token}")
    assert resp.status_code == 401


# ---- 计划与受理 ------------------------------------------------------------------


def test_plan_apply_flow_and_idempotency(agent_env) -> None:
    ws, _config, _reg, app, token = agent_env
    src = _static_src(ws.root)
    client = _client(app)
    headers = _auth(token)

    plan_resp = client.post(
        f"{BASE}/plans",
        headers=headers,
        json={
            "source": {"type": "server_directory", "path": str(src)},
            "intent": "create",
            "displayName": "api-site",
        },
    )
    assert plan_resp.status_code == 200, plan_resp.text
    plan = plan_resp.json()
    assert plan["planId"].startswith("plan_")
    assert plan["sourceDigest"]
    assert plan["policyVersion"] == "1"

    apply_resp = client.post(
        f"{BASE}/deployments",
        headers=headers,
        json={"planId": plan["planId"], "idempotencyKey": "api-key-1"},
    )
    assert apply_resp.status_code == 202, apply_resp.text
    accepted = apply_resp.json()
    assert accepted["operationId"].startswith("op_")
    assert accepted["status"] == "queued"

    # 同键重试 → 同一 operation
    replay = client.post(
        f"{BASE}/deployments",
        headers=headers,
        json={"planId": plan["planId"], "idempotencyKey": "api-key-1"},
    )
    assert replay.status_code == 202
    assert replay.json()["operationId"] == accepted["operationId"]

    # 同键不同载荷 → 409
    other_plan = client.post(
        f"{BASE}/plans",
        headers=headers,
        json={
            "source": {"type": "server_directory", "path": str(src)},
            "intent": "create",
            "displayName": "api-site-2",
        },
    ).json()
    conflict = client.post(
        f"{BASE}/deployments",
        headers=headers,
        json={"planId": other_plan["planId"], "idempotencyKey": "api-key-1"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"

    # 查询 operation（归属校验通过）
    view = client.get(f"{BASE}/operations/{accepted['operationId']}", headers=headers)
    assert view.status_code == 200
    assert view.json()["action"] == "deploy"


def test_lifecycle_revision_conflict_409(agent_env) -> None:
    ws, _config, _reg, app, token = agent_env
    _seed_instance(ws, _reg)
    client = _client(app)
    resp = client.post(
        f"{BASE}/instances/demo/start",
        headers=_auth(token),
        json={"instanceId": "demo", "expectedRevision": 99, "idempotencyKey": "k-1"},
    )
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "revision_conflict"
    assert not err["message"].startswith("["), err["message"]


def test_lifecycle_path_body_mismatch_422(agent_env) -> None:
    ws, _config, _reg, app, token = agent_env
    _seed_instance(ws, _reg)
    client = _client(app)
    resp = client.post(
        f"{BASE}/instances/demo/start",
        headers=_auth(token),
        json={"instanceId": "other", "expectedRevision": 1, "idempotencyKey": "k-1"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "needs_input"


def test_unknown_field_rejected_422(agent_env) -> None:
    _ws, _config, _reg, app, token = agent_env
    client = _client(app)
    resp = client.post(
        f"{BASE}/plans",
        headers=_auth(token),
        json={"source": {"type": "server_directory", "path": "/x"}, "intent": "create", "hack": 1},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "needs_input"


# ---- 与旧 API / SPA 的隔离 --------------------------------------------------------


def test_old_api_and_spa_isolation(agent_env) -> None:
    ws, _config, _reg, app, token = agent_env
    _seed_instance(ws, _reg)
    client = _client(app)

    # 旧 API 行为不变（回环 GET 免 token，IMP-003）
    old = client.get("/api/instances")
    assert old.status_code == 200

    # Agent 未知路径返回 JSON 404，不被 SPA catch-all 吞成 HTML
    missing = client.get(f"{BASE}/nope", headers=_auth(token))
    assert missing.status_code == 404
    assert "text/html" not in missing.headers.get("content-type", "")

    # 发现端点 apiBase 已指向新 API
    info = client.get("/agent-info.json")
    assert info.status_code == 200
    assert info.json()["apiBase"] == BASE


# ---- worker 端到端（lifespan 内） ---------------------------------------------------


def test_apply_to_succeeded_via_lifespan_worker(agent_env) -> None:
    ws, config, reg, app, token = agent_env
    src = _static_src(ws.root)
    headers = _auth(token)

    with TestClient(app, base_url="http://127.0.0.1:17800", client=("127.0.0.1", 50000)) as client:
        plan = client.post(
            f"{BASE}/plans",
            headers=headers,
            json={
                "source": {"type": "server_directory", "path": str(src)},
                "intent": "create",
                "displayName": "e2e-site",
            },
        ).json()
        accepted = client.post(
            f"{BASE}/deployments",
            headers=headers,
            json={"planId": plan["planId"], "idempotencyKey": "e2e-key"},
        )
        assert accepted.status_code == 202
        op_id = accepted.json()["operationId"]

        deadline = time.monotonic() + 30
        view = None
        while time.monotonic() < deadline:
            view = client.get(f"{BASE}/operations/{op_id}", headers=headers).json()
            if view["status"] not in ("queued", "running", "cancelling"):
                break
            time.sleep(0.2)
        assert view is not None and view["status"] == "succeeded", view
        assert view["instanceId"] == "e2e-site"
        assert (view["result"] or {}).get("access")

        # 实例出现在列表，URL 可查
        inst = client.get(f"{BASE}/instances/e2e-site", headers=headers)
        assert inst.status_code == 200
        assert inst.json()["revision"] == 1
        urls = client.get(f"{BASE}/instances/e2e-site/access-urls", headers=headers)
        assert urls.status_code == 200
        assert urls.json()["urls"], "应有 localhost 访问地址"
