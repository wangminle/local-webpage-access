"""M1 本机协作端到端验收（AGC-W15）。

验收场景（设计 §9.3 / 计划步骤 6）：
1. 双客户端视角：client A 提交 plan→apply，client B 轮询 operation 到 succeeded
   并读取实例与访问地址；registry 直读（等价 CLI/daemon 视角）同样可见；
2. 同键重试返回同一 operationId（受理幂等）；
3. 不同键同实例并发 update：先到 succeeded（revision+1），后到以
   ``revision_conflict`` 终态失败且 error 可解释（expected/actual）；
4. 容器实例 operation 不悬挂（无 Docker 环境跳过并记录）。
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
from tests._helpers import make_container_manifest
from tests.conftest import requires_docker, stop_workspace_test_builtins

BASE = "/api/agent/v1"
TERMINAL = {"succeeded", "failed", "cancelled", "cancel_failed", "needs_input", "interrupted"}


@pytest.fixture()
def acc_env(workspace_root: Path):
    ws = Workspace(workspace_root)
    ws.ensure_workspace_dirs()
    config = Config(
        staticGateway="builtin",
        portPool=PortPool(start=23000, end=23050),
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


def _static_src(root: Path, name: str = "acc-site-src") -> Path:
    src = root / name
    src.mkdir(parents=True, exist_ok=True)
    (src / "index.html").write_text("<h1>acceptance</h1>\n", encoding="utf-8")
    return src


def _poll_operation(client: TestClient, headers: dict[str, str], op_id: str) -> dict:
    deadline = time.monotonic() + 60
    view = None
    while time.monotonic() < deadline:
        resp = client.get(f"{BASE}/operations/{op_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        view = resp.json()
        if view["status"] in TERMINAL:
            return view
        time.sleep(0.2)
    pytest.fail(f"operation {op_id} 未在 60s 内到达终态：{view}")


def _plan_and_apply(
    client: TestClient, headers: dict[str, str], src: Path, key: str, **plan_kw
) -> str:
    plan_resp = client.post(
        f"{BASE}/plans",
        headers=headers,
        json={
            "source": {"type": "server_directory", "path": str(src)},
            **plan_kw,
        },
    )
    assert plan_resp.status_code == 200, plan_resp.text
    apply_resp = client.post(
        f"{BASE}/deployments",
        headers=headers,
        json={"planId": plan_resp.json()["planId"], "idempotencyKey": key},
    )
    assert apply_resp.status_code == 202, apply_resp.text
    return apply_resp.json()["operationId"]


# ---- 场景 1+2：双客户端部署全流程 + 同键重试 ---------------------------------------


def test_dual_client_deploy_flow_and_idempotent_replay(acc_env) -> None:
    ws, _config, reg, app, token = acc_env
    src = _static_src(ws.root)
    headers = _auth(token)

    with _client(app) as client_a, _client(app) as client_b:
        # A 计划并受理
        plan_resp = client_a.post(
            f"{BASE}/plans",
            headers=headers,
            json={
                "source": {"type": "server_directory", "path": str(src)},
                "intent": "create",
                "displayName": "acc-site",
            },
        )
        assert plan_resp.status_code == 200, plan_resp.text
        plan_id = plan_resp.json()["planId"]
        accepted = client_a.post(
            f"{BASE}/deployments",
            headers=headers,
            json={"planId": plan_id, "idempotencyKey": "acc-key-1"},
        )
        assert accepted.status_code == 202, accepted.text
        op_id = accepted.json()["operationId"]

        # A 同键重试同一 apply 请求 → 同一 operationId（受理幂等）
        replay = client_a.post(
            f"{BASE}/deployments",
            headers=headers,
            json={"planId": plan_id, "idempotencyKey": "acc-key-1"},
        )
        assert replay.status_code == 202
        assert replay.json()["operationId"] == op_id

        # B 轮询到 succeeded 并读取实例
        view = _poll_operation(client_b, headers, op_id)
        assert view["status"] == "succeeded", view
        assert view["instanceId"] == "acc-site"

        inst = client_b.get(f"{BASE}/instances/acc-site", headers=headers)
        assert inst.status_code == 200
        assert inst.json()["revision"] == 1
        urls = client_b.get(f"{BASE}/instances/acc-site/access-urls", headers=headers)
        assert urls.status_code == 200
        assert urls.json()["urls"]

        # B 能看到 A 的 operation（同一本机 owner 主体，共享操作视图）
        listing = client_b.get(f"{BASE}/instances/acc-site", headers=headers).json()
        assert listing.get("recentOperation") is not None
        assert listing["recentOperation"]["operationId"] == op_id

        # CLI/daemon 视角：registry 直读实例已落盘
        # （须在 lifespan 内做——退出 with 时 manager 会关闭共享 registry 连接）
        row = reg.get_instance("acc-site")
        assert row is not None and row["status"] in ("running", "stopped")


# ---- 场景 3：并发 update 的 revision 冲突可解释 -------------------------------------


def test_concurrent_update_loser_gets_revision_conflict(acc_env) -> None:
    ws, _config, _reg, app, token = acc_env
    src = _static_src(ws.root)
    headers = _auth(token)

    with _client(app) as client:
        create_op = _plan_and_apply(
            client, headers, src, "acc-create", intent="create", displayName="acc-upd"
        )
        assert _poll_operation(client, headers, create_op)["status"] == "succeeded"

        # 源内容变更：让先到的 update 真正推进 revision
        # （zip 未变化的 update 走「跳过更新」短路，不增 revision，无法触发冲突）
        (src / "index.html").write_text("<h1>acceptance v2</h1>\n", encoding="utf-8")

        # 两个 update 计划都基于 revision=1，不同幂等键
        winner = _plan_and_apply(
            client,
            headers,
            src,
            "acc-upd-1",
            intent="update",
            targetInstanceId="acc-upd",
            expectedRevision=1,
        )
        loser = _plan_and_apply(
            client,
            headers,
            src,
            "acc-upd-2",
            intent="update",
            targetInstanceId="acc-upd",
            expectedRevision=1,
        )

        win_view = _poll_operation(client, headers, winner)
        lose_view = _poll_operation(client, headers, loser)
        # 认领顺序不按提交先后保证（created_at 秒级精度可能并列）：
        # 断言与顺序无关——恰好一个成功，另一个以 revision_conflict 终态失败
        views = [win_view, lose_view]
        succeeded = [v for v in views if v["status"] == "succeeded"]
        conflicted = [v for v in views if v["status"] == "failed"]
        assert len(succeeded) == 1 and len(conflicted) == 1, views
        err = conflicted[0]["error"]
        assert err["code"] == "revision_conflict"
        assert err["detail"]["expected"] == 1
        assert err["detail"]["actual"] == 2, "error 应可解释：期望 1，实际已被先到操作推进到 2"

        inst = client.get(f"{BASE}/instances/acc-upd", headers=headers).json()
        assert inst["revision"] == 2


# ---- 场景 4：容器实例 operation 不悬挂（无 Docker 跳过并记录） -----------------------


@requires_docker
def test_container_operation_reaches_terminal_state(acc_env) -> None:
    """容器实例 rebuild 操作必须到达终态（无 daemon 时允许 failed，不得悬挂）。

    无 docker 命令的环境整条跳过（pytest skip reason 已记录）。
    """
    ws, _config, reg, app, token = acc_env
    headers = _auth(token)

    instance_id = "acc-container"
    ws.ensure_app_dirs(instance_id)
    manifest = make_container_manifest(instance_id)
    manifest.save(ws.app_manifest_path(instance_id))
    reg.upsert_from_manifest(manifest)

    with _client(app) as client:
        resp = client.post(
            f"{BASE}/instances/{instance_id}/rebuild",
            headers=headers,
            json={"instanceId": instance_id, "expectedRevision": 1, "idempotencyKey": "acc-rb"},
        )
        assert resp.status_code == 202, resp.text
        view = _poll_operation(client, headers, resp.json()["operationId"])
        # daemon 不可用时允许 failed（build_failed/manager_unavailable 等），但必须是终态
        assert view["status"] in TERMINAL
        if view["status"] == "failed":
            assert view["error"]["code"], "失败必须带可解释 error.code"
