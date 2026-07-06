"""管理页后端 API 测试（WBS-22）。

使用 FastAPI TestClient（基于 httpx）进行端到端 API 验证：
token 鉴权、统计、列表、详情、日志、端口池、pending、操作。
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from local_webpage_access.config import Config, PortPool
from local_webpage_access.manager_api import (
    create_app,
    ensure_token,
    read_token,
    token_path,
)
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry


# ---- 夹具 -------------------------------------------------------------------


def _write_config(ws: Workspace) -> None:
    from local_webpage_access.config import example_config_text

    if not ws.config_path.is_file():
        ws.config_path.write_text(example_config_text(), encoding="utf-8")


def _make_static_zip(path: Path, html: str = "<h1>hello</h1>") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("index.html", html)


@pytest.fixture()
def manager_env(workspace_root: Path):
    """创建一个带静态实例的管理页环境。

    返回 ``(workspace, config, registry, app, client, token, instance_id)``。
    """
    ws = Workspace(workspace_root)
    ws.ensure_workspace_dirs()
    _write_config(ws)
    config = Config(portPool=PortPool(start=21000, end=21050))

    reg = Registry(ws.db_path)
    reg.open()

    # 导入一个静态实例
    from local_webpage_access.importer import Importer

    zip_path = ws.inbox / "static.zip"
    _make_static_zip(zip_path)
    importer = Importer(ws, config, reg)
    result = importer.import_zip(str(zip_path))
    instance_id = result.instance_id

    token = ensure_token(ws)
    app = create_app(ws, config, reg, token=token)
    client = TestClient(app)

    yield EnvBundle(
        workspace=ws,
        config=config,
        registry=reg,
        app=app,
        client=client,
        token=token,
        instance_id=instance_id,
    )

    client.close()  # 触发 lifespan shutdown（关闭 registry）


class EnvBundle:
    """打包 manager 测试环境。"""

    def __init__(
        self,
        *,
        workspace: Workspace,
        config: Config,
        registry: Registry,
        app: Any,
        client: TestClient,
        token: str,
        instance_id: str,
    ) -> None:
        self.workspace = workspace
        self.config = config
        self.registry = registry
        self.app = app
        self.client = client
        self.token = token
        self.instance_id = instance_id

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


# ---- token 机制（WBS-22.12）-------------------------------------------------


def test_ensure_token_generates_and_persists(workspace: Workspace) -> None:
    token = ensure_token(workspace)
    assert token
    assert read_token(workspace) == token
    assert token_path(workspace).is_file()


def test_ensure_token_idempotent(workspace: Workspace) -> None:
    t1 = ensure_token(workspace)
    t2 = ensure_token(workspace)
    assert t1 == t2  # 幂等


def test_read_token_none_when_absent(workspace: Workspace) -> None:
    assert read_token(workspace) is None


# ---- 鉴权（WBS-22.12）-------------------------------------------------------


def test_api_rejects_missing_token(manager_env: EnvBundle) -> None:
    resp = manager_env.client.get("/api/instances")
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "unauthorized"


def test_api_localhost_bypass_without_token(manager_env: EnvBundle) -> None:
    """IMP-003：本机 loopback 访问免 token。"""
    from unittest.mock import Mock

    from fastapi import HTTPException

    from local_webpage_access.manager_api import _is_localhost_client, require_token

    for host in ("127.0.0.1", "::1", "localhost"):
        request = Mock()
        request.client = Mock(host=host)
        assert _is_localhost_client(request) is True
        require_token(request)  # 不应抛错

    request = Mock()
    request.client = Mock(host="10.0.0.8")
    request.app = manager_env.app
    request.headers = Mock(get=lambda _k, default="": default)
    request.query_params = Mock(get=lambda _k: None)
    with pytest.raises(HTTPException) as exc:
        require_token(request)
    assert exc.value.status_code == 401


def test_api_rejects_wrong_token(manager_env: EnvBundle) -> None:
    resp = manager_env.client.get(
        "/api/instances", headers={"Authorization": "Bearer wrong-token"}
    )
    assert resp.status_code == 401


def test_api_accepts_bearer_token(manager_env: EnvBundle) -> None:
    resp = manager_env.client.get(
        "/api/instances", headers=manager_env.auth_headers()
    )
    assert resp.status_code == 200


def test_api_accepts_query_token(manager_env: EnvBundle) -> None:
    """前端在打开新标签时可能用 ?token= 传递。"""
    resp = manager_env.client.get(f"/api/stats?token={manager_env.token}")
    assert resp.status_code == 200


def test_api_accepts_x_lwa_token_header(manager_env: EnvBundle) -> None:
    resp = manager_env.client.get(
        "/api/stats", headers={"X-LWA-Token": manager_env.token}
    )
    assert resp.status_code == 200


def test_health_endpoint_no_auth(manager_env: EnvBundle) -> None:
    resp = manager_env.client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["workspaceRoot"] == str(manager_env.workspace.root.resolve())


# ---- 统计（WBS-22.05/06）----------------------------------------------------


def test_stats_returns_counts_and_host(manager_env: EnvBundle) -> None:
    resp = manager_env.client.get(
        "/api/stats", headers=manager_env.auth_headers()
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["counts"]["total"] >= 1
    assert "static" in body["typeDistribution"]
    assert "host" in body
    assert "portPool" in body
    pp = body["portPool"]
    assert pp["start"] == 21000
    assert pp["end"] == 21050


# ---- 实例列表（WBS-22.03）---------------------------------------------------


def test_list_instances(manager_env: EnvBundle) -> None:
    resp = manager_env.client.get(
        "/api/instances", headers=manager_env.auth_headers()
    )
    assert resp.status_code == 200
    instances = resp.json()["instances"]
    assert any(i["id"] == manager_env.instance_id for i in instances)


def test_list_instances_includes_stack_database_serving_and_resources(
    manager_env: EnvBundle,
) -> None:
    """BUG-028/035：列表 API 应提供前端列所需字段。"""
    manager_env.registry.upsert_resources(
        manager_env.instance_id,
        source_size_bytes=10,
        public_size_bytes=20,
        data_size_bytes=30,
        last_memory_bytes=4096,
        last_cpu_percent=1.25,
    )
    resp = manager_env.client.get(
        "/api/instances", headers=manager_env.auth_headers()
    )
    assert resp.status_code == 200
    row = next(i for i in resp.json()["instances"] if i["id"] == manager_env.instance_id)
    assert "stack" in row
    assert "database" in row
    assert row["servingMode"] == "shared-static"
    assert row["lastMemoryBytes"] == 4096
    assert row["lastCpuPercent"] == 1.25


# ---- 实例详情（WBS-22.04）---------------------------------------------------


def test_instance_detail(manager_env: EnvBundle) -> None:
    resp = manager_env.client.get(
        f"/api/instances/{manager_env.instance_id}",
        headers=manager_env.auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["instance"]["id"] == manager_env.instance_id
    assert body["manifest"] is not None
    assert "builds" in body
    assert "events" in body


def test_instance_detail_returns_camel_case_builds_and_events(
    manager_env: EnvBundle,
) -> None:
    """BUG-043：详情接口字段应与前端读取的 camelCase 匹配。"""
    build_id = manager_env.registry.add_build(
        manager_env.instance_id, status="running", started_at="2026-07-06T01:00:00"
    )
    manager_env.registry.finish_build(
        build_id, status="failed", error_summary="boom"
    )
    manager_env.registry.add_event(manager_env.instance_id, "scan", "done")

    resp = manager_env.client.get(
        f"/api/instances/{manager_env.instance_id}",
        headers=manager_env.auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["builds"][0]["startedAt"]
    assert body["builds"][0]["errorSummary"] == "boom"
    scan_event = next(ev for ev in body["events"] if ev["eventType"] == "scan")
    assert scan_event["createdAt"]


def test_instance_detail_not_found(manager_env: EnvBundle) -> None:
    resp = manager_env.client.get(
        "/api/instances/nonexistent-id", headers=manager_env.auth_headers()
    )
    assert resp.status_code == 404


def test_instance_detail_rejects_invalid_id(manager_env: EnvBundle) -> None:
    """非法 slug 应被 validate_instance_id 拦截（BUG-025）。"""
    resp = manager_env.client.get(
        "/api/instances/..%2Fetc", headers=manager_env.auth_headers()
    )
    assert resp.status_code in (400, 404)


def test_instance_detail_rejects_invalid_id_with_400(
    manager_env: EnvBundle,
) -> None:
    """BUG-044：非法实例 ID 应返回 400，而不是 500。"""
    resp = manager_env.client.get(
        "/api/instances/Bad_ID", headers=manager_env.auth_headers()
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"


# ---- 日志（WBS-22.07）-------------------------------------------------------


def test_logs_returns_available_and_content(manager_env: EnvBundle) -> None:
    resp = manager_env.client.get(
        f"/api/instances/{manager_env.instance_id}/logs?category=run&tail=50",
        headers=manager_env.auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["instanceId"] == manager_env.instance_id
    assert body["category"] == "run"
    assert "available" in body
    assert "content" in body


def test_logs_rejects_invalid_category(manager_env: EnvBundle) -> None:
    """BUG-040：日志分类不得允许路径穿越。"""
    resp = manager_env.client.get(
        f"/api/instances/{manager_env.instance_id}/logs?category=..%2F..%2Fsecret",
        headers=manager_env.auth_headers(),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"


# ---- 资源（WBS-22.06）-------------------------------------------------------


def test_instance_resources(manager_env: EnvBundle) -> None:
    resp = manager_env.client.get(
        f"/api/instances/{manager_env.instance_id}/resources",
        headers=manager_env.auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["instanceId"] == manager_env.instance_id
    assert "resources" in body


# ---- pending 列表（WBS-22.09）-----------------------------------------------


def test_pending_list(manager_env: EnvBundle) -> None:
    resp = manager_env.client.get(
        "/api/pending", headers=manager_env.auth_headers()
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "pending" in body
    assert "failed" in body
    # 导入的静态实例默认不是 pending（determinable），但也可能未被启动；
    # 这里只验证结构，不强断言内容


def test_stats_syncs_status_before_counting(manager_env: EnvBundle, monkeypatch) -> None:
    """BUG-030：/api/stats 读取前应先 sync_status。"""
    from local_webpage_access.models import Status

    manager_env.registry.update_status(manager_env.instance_id, "running")

    def fake_observe(ws, cfg, reg, iid):
        reg.update_status(iid, "stopped")
        return Status.STOPPED

    monkeypatch.setattr("local_webpage_access.lifecycle.observe_status", fake_observe)
    resp = manager_env.client.get("/api/stats", headers=manager_env.auth_headers())
    assert resp.status_code == 200
    assert resp.json()["counts"]["stopped"] >= 1


def test_pending_syncs_status_before_listing(
    manager_env: EnvBundle, monkeypatch
) -> None:
    """BUG-030：/api/pending 读取前应先 sync_status。"""
    from local_webpage_access.models import Status

    manager_env.registry.update_status(manager_env.instance_id, "running")

    def fake_observe(ws, cfg, reg, iid):
        reg.update_status(iid, "failed")
        return Status.FAILED

    monkeypatch.setattr("local_webpage_access.lifecycle.observe_status", fake_observe)
    resp = manager_env.client.get("/api/pending", headers=manager_env.auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert any(i["id"] == manager_env.instance_id for i in body["failed"])


# ---- 端口池（WBS-22.10）-----------------------------------------------------


def test_port_pool(manager_env: EnvBundle) -> None:
    resp = manager_env.client.get(
        "/api/port-pool", headers=manager_env.auth_headers()
    )
    assert resp.status_code == 200
    pp = resp.json()["portPool"]
    assert pp["start"] == 21000
    assert pp["end"] == 21050
    assert pp["total"] == 51
    assert pp["allocated"] >= 0
    assert "ports" in pp


# ---- 操作（WBS-22.08）调用同一套 lifecycle --------------------------------


def test_start_operation_calls_lifecycle(manager_env: EnvBundle) -> None:
    """start API 应调用 lifecycle.start_instance（不真正起 Docker）。"""
    with pytest.MonkeyPatch.context() as mp:
        called: list[str] = []

        def fake_start(ws, cfg, reg, iid):
            called.append(iid)
            from local_webpage_access.models import InstanceManifest

            return InstanceManifest.load(ws.app_manifest_path(iid))

        mp.setattr("local_webpage_access.lifecycle.start_instance", fake_start)
        resp = manager_env.client.post(
            f"/api/instances/{manager_env.instance_id}/start",
            headers=manager_env.auth_headers(),
        )
    assert resp.status_code == 200, resp.text
    assert called == [manager_env.instance_id]
    assert resp.json()["action"] == "start"


def test_stop_operation_calls_lifecycle(manager_env: EnvBundle) -> None:
    with pytest.MonkeyPatch.context() as mp:
        called: list[str] = []
        mp.setattr(
            "local_webpage_access.lifecycle.stop_instance_op",
            lambda ws, cfg, reg, iid: called.append(iid),
        )
        resp = manager_env.client.post(
            f"/api/instances/{manager_env.instance_id}/stop",
            headers=manager_env.auth_headers(),
        )
    assert resp.status_code == 200
    assert called == [manager_env.instance_id]


def test_restart_operation_calls_lifecycle(manager_env: EnvBundle) -> None:
    with pytest.MonkeyPatch.context() as mp:
        called: list[str] = []
        mp.setattr(
            "local_webpage_access.lifecycle.restart_instance",
            lambda ws, cfg, reg, iid: called.append(iid),
        )
        resp = manager_env.client.post(
            f"/api/instances/{manager_env.instance_id}/restart",
            headers=manager_env.auth_headers(),
        )
    assert resp.status_code == 200
    assert called == [manager_env.instance_id]


def test_rebuild_operation_calls_lifecycle(manager_env: EnvBundle) -> None:
    with pytest.MonkeyPatch.context() as mp:
        called: list[str] = []
        mp.setattr(
            "local_webpage_access.lifecycle.rebuild_instance",
            lambda ws, cfg, reg, iid: called.append(iid),
        )
        resp = manager_env.client.post(
            f"/api/instances/{manager_env.instance_id}/rebuild",
            headers=manager_env.auth_headers(),
        )
    assert resp.status_code == 200
    assert called == [manager_env.instance_id]


def test_operation_not_found(manager_env: EnvBundle) -> None:
    resp = manager_env.client.post(
        "/api/instances/no-such-id/start", headers=manager_env.auth_headers()
    )
    assert resp.status_code == 404


# ---- 静态资源托管（WBS-22.02 / WBS-23 前端）----------------------------------


def test_root_serves_index(manager_env: EnvBundle) -> None:
    """``/`` 返回管理页前端 ``index.html``（WBS-23）。"""
    resp = manager_env.client.get("/")
    assert resp.status_code == 200
    assert "Local Webpage Access" in resp.text
    assert "<html" in resp.text.lower()


def test_static_assets_served(manager_env: EnvBundle) -> None:
    """style.css / app.js 可被 StaticFiles 正常托管。"""
    for asset in ("/style.css", "/app.js"):
        resp = manager_env.client.get(asset)
        assert resp.status_code == 200, asset
        assert resp.text.strip()


def test_unknown_api_path_returns_401_or_404(manager_env: EnvBundle) -> None:
    resp = manager_env.client.get("/api/nonexistent-endpoint")
    # 无 token → 401；有 token → 404
    assert resp.status_code in (401, 404)


# ---- 错误码映射（BUG-033）--------------------------------------------------


def test_lwa_error_code_maps_client_errors_to_4xx() -> None:
    """BUG-033：客户端输入/配置类异常不得映射成 500。"""
    from local_webpage_access.errors import (
        ConfigError,
        PathError,
        RecognitionError,
        SchemaError,
        ZipImportError,
    )
    from local_webpage_access.manager_api import _lwa_error_code

    for exc in (
        ConfigError("bad config"),
        SchemaError("bad schema"),
        PathError("bad path"),
        ZipImportError("bad zip"),
        RecognitionError("cannot recognize"),
    ):
        code = _lwa_error_code(exc)
        assert code == "bad_request", exc


def test_lwa_error_code_maps_unavailable_to_503() -> None:
    """BUG-033：端口池耗尽 / Docker 不可用应映射 503，而非 500。"""
    from local_webpage_access.errors import DockerError, PortError
    from local_webpage_access.manager_api import _ERROR_STATUS, _lwa_error_code

    for exc in (PortError("pool exhausted"), DockerError("docker down")):
        code = _lwa_error_code(exc)
        assert code == "service_unavailable", exc
        assert _ERROR_STATUS[code] == 503


def test_lwa_error_code_maps_server_errors_to_500() -> None:
    """BUG-033：服务端处理失败仍为 500。"""
    from local_webpage_access.errors import (
        BuildError,
        GatewayError,
        HostingError,
        LifecycleError,
        RegistryError,
    )
    from local_webpage_access.manager_api import _lwa_error_code

    for exc in (
        RegistryError("db error"),
        GatewayError("gateway error"),
        BuildError("build failed"),
        LifecycleError("lifecycle error"),
        HostingError("hosting error"),
    ):
        assert _lwa_error_code(exc) == "internal", exc


def test_error_response_http_status_matches_code() -> None:
    """BUG-033：error_response 按错误码选 HTTP 状态，覆盖 4xx/5xx 全档。"""
    from local_webpage_access.manager_api import error_response

    assert error_response("bad_request", "x").status_code == 400
    assert error_response("not_found", "x").status_code == 404
    assert error_response("conflict", "x").status_code == 409
    assert error_response("unauthorized", "x").status_code == 401
    assert error_response("service_unavailable", "x").status_code == 503
    assert error_response("internal", "x").status_code == 500


def test_cli_manager_start_rejects_lan_binding_without_token(
    workspace_root: Path, monkeypatch
) -> None:
    """BUG-029：CLI 启动流程必须接入 validate_manager_binding。"""
    from typer.testing import CliRunner

    from local_webpage_access.cli import app
    from local_webpage_access.init_workspace import init_workspace

    init_workspace(workspace_root)
    monkeypatch.chdir(workspace_root)
    monkeypatch.setattr("local_webpage_access.manager_api.ensure_token", lambda ws: "")
    monkeypatch.setattr(
        "local_webpage_access.manager_api.run_manager",
        lambda *args, **kwargs: None,
    )

    result = CliRunner().invoke(app, ["manager", "start"])
    assert result.exit_code == 1
    assert "lan_bind_no_token" in result.output or "critical" in result.output
