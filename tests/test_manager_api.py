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
    config = Config(
        staticGateway="builtin",
        portPool=PortPool(start=21000, end=21050),
    )

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
    # BUG-171：须以 context manager 进入 lifespan，teardown 才会关闭 registry
    with TestClient(app) as client:
        yield EnvBundle(
            workspace=ws,
            config=config,
            registry=reg,
            app=app,
            client=client,
            token=token,
            instance_id=instance_id,
        )


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


def test_rotate_token_replaces_existing(workspace: Workspace) -> None:
    """BUG-118：rotate_token 生成新 token 并覆盖旧文件。"""
    from local_webpage_access.manager_api import rotate_token

    old = ensure_token(workspace)
    new = rotate_token(workspace)
    assert new != old
    assert read_token(workspace) == new
    assert token_path(workspace).stat().st_mode & 0o777 == 0o600


def test_run_manager_does_not_log_full_token(
    workspace: Workspace, config, monkeypatch
) -> None:
    """BUG-118：run_manager 不得把完整 token 写入日志。"""
    from local_webpage_access import manager_api as ma

    workspace.ensure_workspace_dirs()
    _write_config(workspace)
    token = ensure_token(workspace)
    logged: list[str] = []

    class _FakeServer:
        def run(self):
            return None

    real_info = ma.log.info

    def capture_info(msg, *args, **kwargs):  # noqa: ANN001
        logged.append(msg % args if args else str(msg))
        return real_info(msg, *args, **kwargs)

    monkeypatch.setattr(ma.log, "info", capture_info)
    monkeypatch.setattr("uvicorn.Config", lambda *a, **kw: object())
    monkeypatch.setattr("uvicorn.Server", lambda *a, **kw: _FakeServer())
    ma.run_manager(workspace, config)
    joined = "\n".join(logged)
    assert token not in joined
    assert any("管理页已就绪" in line for line in logged)


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
    assert "version" in body
    # TestClient 默认 client host 为 testclient（非回环）→ 不泄露路径（BUG-169）
    assert "workspaceRoot" not in body


def test_is_loopback_host_handles_ipv4_mapped_ipv6() -> None:
    """BUG-194：::ffff:127.0.0.1 与整个 127.x 段都判为回环，不再仅认字面集合。"""
    from local_webpage_access.manager_api import _is_loopback_host

    for h in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "127.0.0.2", "localhost"):
        assert _is_loopback_host(h) is True, h
    for h in ("10.0.0.8", "192.168.1.5", "::ffff:10.0.0.8", ""):
        assert _is_loopback_host(h) is False, h


def test_is_localhost_client_recognizes_mapped_loopback() -> None:
    """BUG-194：双栈 :: 监听下 IPv4 回环 client.host=::ffff:127.0.0.1 视为本机，
    使 /api/health 仍回 workspaceRoot 供 manager_service 归属校验。"""
    from unittest.mock import Mock

    from local_webpage_access.manager_api import _is_localhost_client

    request = Mock()
    request.client = Mock(host="::ffff:127.0.0.1")
    assert _is_localhost_client(request) is True


def test_is_self_connection_matches_lan_bind_host() -> None:
    """BUG-194：managerHost=LAN IP 时，本机自连（client.host==bind）判为自连；
    他机 LAN 源 IP 不匹配；通配绑定交由 localhost 判定。"""
    from unittest.mock import Mock

    from local_webpage_access.manager_api import _is_self_connection

    cfg_lan = Config(managerHost="192.168.1.10", portPool=PortPool(start=21000, end=21050))
    req_self = Mock()
    req_self.client = Mock(host="192.168.1.10")
    assert _is_self_connection(req_self, cfg_lan) is True

    req_other = Mock()
    req_other.client = Mock(host="192.168.1.55")
    assert _is_self_connection(req_other, cfg_lan) is False

    cfg_wild = Config(managerHost="0.0.0.0", portPool=PortPool(start=21000, end=21050))
    req_wild = Mock()
    req_wild.client = Mock(host="0.0.0.0")
    assert _is_self_connection(req_wild, cfg_wild) is False


def test_health_workspace_root_only_for_localhost(manager_env: EnvBundle) -> None:
    """BUG-169：仅本机回环客户端可见 workspaceRoot。"""
    with TestClient(manager_env.app, client=("127.0.0.1", 50000)) as local_client:
        body = local_client.get("/api/health").json()
        assert body["ok"] is True
        assert body["workspaceRoot"] == str(manager_env.workspace.root.resolve())

    with TestClient(manager_env.app, client=("10.0.0.8", 50000)) as lan_client:
        body = lan_client.get("/api/health").json()
        assert body["ok"] is True
        assert "workspaceRoot" not in body


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


def test_stats_includes_recoverable_status_counts(
    manager_env: EnvBundle, monkeypatch
) -> None:
    """BUG-081：/api/stats counts 应含 gateway_down / config_invalid 可恢复态。"""
    from local_webpage_access.models import Status

    # 先置为非 pending/queued，确保 sync_status 会调用 observe_status
    manager_env.registry.update_status(manager_env.instance_id, "stopped")

    def fake_observe(ws, cfg, reg, iid):
        reg.update_status(iid, "gateway_down")
        return Status.GATEWAY_DOWN

    monkeypatch.setattr("local_webpage_access.lifecycle.observe_status", fake_observe)
    resp = manager_env.client.get(
        "/api/stats", headers=manager_env.auth_headers()
    )
    body = resp.json()
    assert body["counts"]["gateway_down"] >= 1
    assert "config_invalid" in body["counts"]


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


def test_recover_operation_calls_lifecycle(manager_env: EnvBundle) -> None:
    """DEV-043：recover API 应调用 lifecycle.recover_instance（一键恢复网关不可达实例）。"""
    with pytest.MonkeyPatch.context() as mp:
        called: list[str] = []
        mp.setattr(
            "local_webpage_access.lifecycle.recover_instance",
            lambda ws, cfg, reg, iid: called.append(iid),
        )
        resp = manager_env.client.post(
            f"/api/instances/{manager_env.instance_id}/recover",
            headers=manager_env.auth_headers(),
        )
    assert resp.status_code == 200, resp.text
    assert called == [manager_env.instance_id]
    assert resp.json()["action"] == "recover"


def test_operation_not_found(manager_env: EnvBundle) -> None:
    resp = manager_env.client.post(
        "/api/instances/no-such-id/start", headers=manager_env.auth_headers()
    )
    assert resp.status_code == 404


# ---- IMP-009：实例 zip 原地更新（管理页 API）----------------------------------


def test_update_endpoint_replaces_content(manager_env: EnvBundle) -> None:
    """POST /api/instances/{id}/update：原地覆盖 current/，返回 rebuilt=True。"""
    iid = manager_env.instance_id
    # 在 inbox 放一个 v2 zip
    v2_zip = manager_env.workspace.inbox / "v2.zip"
    _make_static_zip(v2_zip, html="<h1>v2 from api</h1>")

    resp = manager_env.client.post(
        f"/api/instances/{iid}/update",
        headers=manager_env.auth_headers(),
        json={"zipPath": "v2.zip"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["action"] == "update"
    assert body["skipped"] is False
    assert body["rebuilt"] is True
    assert body["restarted"] is False  # 实例未 running，无需 restart
    # current/ 内容已更新
    idx = manager_env.workspace.app_current(iid) / "index.html"
    assert "v2 from api" in idx.read_text()


def test_update_endpoint_same_hash_skips(manager_env: EnvBundle) -> None:
    """相同 hash 的 zip → skipped=True，不 rebuild。"""
    iid = manager_env.instance_id
    # 用与导入时相同的 zip 内容（inbox/static.zip 已存在）
    resp = manager_env.client.post(
        f"/api/instances/{iid}/update",
        headers=manager_env.auth_headers(),
        json={"zipPath": "static.zip"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["skipped"] is True
    assert body["rebuilt"] is False


def test_update_endpoint_missing_zippath_400(manager_env: EnvBundle) -> None:
    """缺少 zipPath → 400。"""
    iid = manager_env.instance_id
    resp = manager_env.client.post(
        f"/api/instances/{iid}/update",
        headers=manager_env.auth_headers(),
        json={},
    )
    assert resp.status_code == 400


def test_update_endpoint_rejects_relative_path_escape(
    manager_env: EnvBundle,
) -> None:
    """相对 zipPath 只能解析到 inbox/ 内，../ 逃逸应返回 400。"""
    iid = manager_env.instance_id
    outside_inbox = manager_env.workspace.root / "escape.zip"
    _make_static_zip(outside_inbox, html="<h1>escaped</h1>")

    before = (manager_env.workspace.app_current(iid) / "index.html").read_text()
    resp = manager_env.client.post(
        f"/api/instances/{iid}/update",
        headers=manager_env.auth_headers(),
        json={"zipPath": "../escape.zip"},
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"
    assert (manager_env.workspace.app_current(iid) / "index.html").read_text() == before


def test_update_endpoint_rejects_absolute_path_outside_inbox(
    manager_env: EnvBundle, tmp_path: Path
) -> None:
    """绝对 zipPath 也必须位于 inbox/ 内，不能更新任意本机 zip。"""
    iid = manager_env.instance_id
    external_zip = tmp_path / "outside.zip"
    _make_static_zip(external_zip, html="<h1>absolute outside</h1>")

    before = (manager_env.workspace.app_current(iid) / "index.html").read_text()
    resp = manager_env.client.post(
        f"/api/instances/{iid}/update",
        headers=manager_env.auth_headers(),
        json={"zipPath": str(external_zip)},
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"
    assert (manager_env.workspace.app_current(iid) / "index.html").read_text() == before


def test_update_endpoint_not_found(manager_env: EnvBundle) -> None:
    """更新不存在的实例 → 404。"""
    v2_zip = manager_env.workspace.inbox / "v2.zip"
    _make_static_zip(v2_zip, html="<h1>v2</h1>")
    resp = manager_env.client.post(
        "/api/instances/no-such-id/update",
        headers=manager_env.auth_headers(),
        json={"zipPath": "v2.zip"},
    )
    assert resp.status_code == 404


def test_update_endpoint_restart_when_running(manager_env: EnvBundle) -> None:
    """实例 running 时更新 → API 自动 restart（restarted=True）。"""
    from local_webpage_access.models import DesiredState, InstanceManifest

    iid = manager_env.instance_id
    # 模拟已启动：manifest desiredState=running
    mpath = manager_env.workspace.app_manifest_path(iid)
    m = InstanceManifest.load(mpath)
    m.desiredState = DesiredState.RUNNING
    m.save(mpath)

    v2_zip = manager_env.workspace.inbox / "v2.zip"
    _make_static_zip(v2_zip, html="<h1>v2 running</h1>")

    with pytest.MonkeyPatch.context() as mp:
        restarted: list[str] = []
        mp.setattr(
            "local_webpage_access.lifecycle.restart_instance",
            lambda ws, cfg, reg, _iid: restarted.append(_iid),
        )
        resp = manager_env.client.post(
            f"/api/instances/{iid}/update",
            headers=manager_env.auth_headers(),
            json={"zipPath": "v2.zip"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["restarted"] is True
    assert body.get("rebuiltRuntime") is False
    assert restarted == [iid]


def test_update_endpoint_container_rebuild_when_running(
    manager_env: EnvBundle,
) -> None:
    """DEV-067：容器 running 更新 → API 走 rebuild，不走轻量 restart。"""
    from local_webpage_access.importer import Importer
    from local_webpage_access.models import DesiredState, InstanceManifest

    # 导入一个 python 容器实例
    api_zip = manager_env.workspace.inbox / "api.zip"
    with zipfile.ZipFile(api_zip, "w") as zf:
        zf.writestr("requirements.txt", "fastapi\nuvicorn\n")
        zf.writestr("main.py", "app=1\n")
    importer = Importer(
        manager_env.workspace, manager_env.config, manager_env.registry
    )
    iid = importer.import_zip(api_zip, name="api-upd").instance_id
    mpath = manager_env.workspace.app_manifest_path(iid)
    m = InstanceManifest.load(mpath)
    m.desiredState = DesiredState.RUNNING
    assert m.container is not None
    m.container.containerId = "c-old"
    m.save(mpath)
    manager_env.registry.upsert_from_manifest(m)

    v2 = manager_env.workspace.inbox / "api-v2.zip"
    with zipfile.ZipFile(v2, "w") as zf:
        zf.writestr("requirements.txt", "fastapi\nuvicorn\n")
        zf.writestr("main.py", "app=2\n")

    with pytest.MonkeyPatch.context() as mp:
        rebuilt: list[str] = []
        restarted: list[str] = []
        mp.setattr(
            "local_webpage_access.lifecycle.rebuild_instance",
            lambda ws, cfg, reg, _iid: rebuilt.append(_iid),
        )
        mp.setattr(
            "local_webpage_access.lifecycle.restart_instance",
            lambda ws, cfg, reg, _iid: restarted.append(_iid),
        )
        resp = manager_env.client.post(
            f"/api/instances/{iid}/update",
            headers=manager_env.auth_headers(),
            json={"zipPath": "api-v2.zip"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rebuiltRuntime"] is True
    assert body["restarted"] is False
    assert body["needsRebuild"] is True
    assert rebuilt == [iid]
    assert restarted == []


# ---- 路径别名 API（IMP-006 / WBS-006.08~006.10）----------------------------


@pytest.fixture
def caddy_alias_gateway(monkeypatch):
    """用 Caddy 替身替换 ``path_alias.StaticGateway``（IMP-022）。

    别名设置类用例不依赖本机已安装/运行 caddy——无 caddy 的 CI 上
    ``detect_backend()`` 会回 builtin 触发 400，使本应成功的用例误报失败。
    此 fixture 强制走 caddy 分支并桩掉 reload，保持用例可移植。
    """
    from local_webpage_access import path_alias

    class _FakeGW:
        def __init__(self, ws, cfg):
            self.ws = ws

        def detect_backend(self):
            return "caddy"

        def is_enabled(self, iid):
            return False

        def generate_alias_config(self, iid, alias, hp):
            p = self.ws.app_alias_config(iid)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"reverse_proxy 127.0.0.1:{hp}\n", encoding="utf-8")

        def reload_all(self):
            return None

        def remove_alias_config(self, iid):
            p = self.ws.app_alias_config(iid)
            if p.is_file():
                p.unlink()

    monkeypatch.setattr(path_alias, "StaticGateway", _FakeGW)


def test_path_alias_set_and_clear(manager_env: EnvBundle, caddy_alias_gateway) -> None:
    """PATCH path-alias：设置与清除别名，写入 manifest 与 API 响应。"""
    from local_webpage_access.models import InstanceManifest, RouteMode

    iid = manager_env.instance_id
    resp = manager_env.client.patch(
        f"/api/instances/{iid}/path-alias",
        headers=manager_env.auth_headers(),
        json={"alias": "demo-alias"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["alias"] == "demo-alias"
    assert body["action"] == "path-alias"
    assert body["instance"]["routeHost"] == "demo-alias"

    manifest = InstanceManifest.load(manager_env.workspace.app_manifest_path(iid))
    assert manifest.static is not None
    assert manifest.static.routeMode == RouteMode.NAME.value
    assert manifest.static.routeHost == "demo-alias"
    assert manifest.network is not None
    assert manifest.network.routeHost == "demo-alias"

    resp2 = manager_env.client.patch(
        f"/api/instances/{iid}/path-alias",
        headers=manager_env.auth_headers(),
        json={"alias": None},
    )
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["alias"] is None
    manifest = InstanceManifest.load(manager_env.workspace.app_manifest_path(iid))
    assert manifest.static.routeMode == RouteMode.PORT.value
    assert manifest.static.routeHost is None


def test_path_alias_rejects_reserved_slug(manager_env: EnvBundle) -> None:
    resp = manager_env.client.patch(
        f"/api/instances/{manager_env.instance_id}/path-alias",
        headers=manager_env.auth_headers(),
        json={"alias": "api"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"


def test_path_alias_rejects_duplicate_slug(
    manager_env: EnvBundle, caddy_alias_gateway
) -> None:
    from local_webpage_access.importer import Importer

    zip2 = manager_env.workspace.inbox / "static2.zip"
    _make_static_zip(zip2, html="<h1>two</h1>")
    importer = Importer(manager_env.workspace, manager_env.config, manager_env.registry)
    second_id = importer.import_zip(str(zip2)).instance_id

    resp1 = manager_env.client.patch(
        f"/api/instances/{manager_env.instance_id}/path-alias",
        headers=manager_env.auth_headers(),
        json={"alias": "shared-slug"},
    )
    assert resp1.status_code == 200

    resp2 = manager_env.client.patch(
        f"/api/instances/{second_id}/path-alias",
        headers=manager_env.auth_headers(),
        json={"alias": "shared-slug"},
    )
    assert resp2.status_code == 400
    assert "占用" in resp2.json()["error"]["message"]


def test_path_alias_accepts_container_instance(
    manager_env: EnvBundle, caddy_alias_gateway
) -> None:
    """IMP-014：容器实例（docker-compose）也可设置路径别名，写入 container.routeHost。"""
    from local_webpage_access.models import InstanceManifest
    from tests._helpers import make_container_manifest

    ws = manager_env.workspace
    cid = "api-alias-test"
    ws.ensure_app_dirs(cid)
    manifest = make_container_manifest(cid)
    # 容器需有 hostPort，别名 reverse_proxy 才有目标端口
    manifest.container.hostPort = 21100
    manifest.save(ws.app_manifest_path(cid))
    manager_env.registry.upsert_from_manifest(manifest)

    resp = manager_env.client.patch(
        f"/api/instances/{cid}/path-alias",
        headers=manager_env.auth_headers(),
        json={"alias": "api-alias"},
    )
    assert resp.status_code == 200
    # 别名已落 manifest.container.routeMode/routeHost（IMP-014）
    reloaded = InstanceManifest.load(ws.app_manifest_path(cid))
    assert reloaded.container is not None
    assert reloaded.container.routeMode == "name"
    assert reloaded.container.routeHost == "api-alias"


def test_path_alias_missing_field(manager_env: EnvBundle) -> None:
    resp = manager_env.client.patch(
        f"/api/instances/{manager_env.instance_id}/path-alias",
        headers=manager_env.auth_headers(),
        json={},
    )
    assert resp.status_code == 400


def test_path_alias_running_triggers_gateway_reload(
    manager_env: EnvBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """running 实例改别名应 regenerate 别名片段并 reload（mock gateway）。"""
    from local_webpage_access.models import (
        DesiredState,
        InstanceManifest,
        NetworkConfig,
        StaticConfig,
    )

    iid = manager_env.instance_id
    mpath = manager_env.workspace.app_manifest_path(iid)
    manifest = InstanceManifest.load(mpath)
    manifest.desiredState = DesiredState.RUNNING
    manifest.static = StaticConfig(hostPort=21001, enabled=True)
    manifest.network = NetworkConfig(hostPort=21001)
    manifest.save(mpath)

    reloaded: list[bool] = []
    generated: list[tuple[str, str, int]] = []

    monkeypatch.setattr(
        "local_webpage_access.path_alias.StaticGateway.is_enabled",
        lambda self, instance_id: instance_id == iid,
    )
    monkeypatch.setattr(
        "local_webpage_access.path_alias.StaticGateway.detect_backend",
        lambda self: "caddy",
    )
    monkeypatch.setattr(
        "local_webpage_access.path_alias.StaticGateway.generate_alias_config",
        lambda self, instance_id, alias, host_port: generated.append(
            (instance_id, alias, host_port)
        ),
    )
    monkeypatch.setattr(
        "local_webpage_access.path_alias.StaticGateway.reload_all",
        lambda self: reloaded.append(True),
    )

    resp = manager_env.client.patch(
        f"/api/instances/{iid}/path-alias",
        headers=manager_env.auth_headers(),
        json={"alias": "live-alias"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["gatewayReloaded"] is True
    assert resp.json()["aliasEntryEnabled"] is True
    assert generated == [(iid, "live-alias", 21001)]
    assert reloaded == [True]


def test_path_alias_reload_failure_does_not_persist(
    manager_env: EnvBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Caddy reload 失败 → API 500，manifest/registry 保持旧别名状态。"""
    from local_webpage_access.errors import GatewayError
    from local_webpage_access.models import (
        DesiredState,
        InstanceManifest,
        NetworkConfig,
        StaticConfig,
    )

    iid = manager_env.instance_id
    mpath = manager_env.workspace.app_manifest_path(iid)
    manifest = InstanceManifest.load(mpath)
    manifest.desiredState = DesiredState.RUNNING
    manifest.static = StaticConfig(
        hostPort=21001,
        enabled=True,
        routeMode="name",
        routeHost="keep-me",
    )
    manifest.network = NetworkConfig(hostPort=21001, routeHost="keep-me")
    manifest.save(mpath)
    manager_env.registry.upsert_static_site(
        iid,
        manifest.static.model_dump(),
    )

    monkeypatch.setattr(
        "local_webpage_access.path_alias.StaticGateway.is_enabled",
        lambda self, instance_id: instance_id == iid,
    )
    monkeypatch.setattr(
        "local_webpage_access.path_alias.StaticGateway.detect_backend",
        lambda self: "caddy",
    )
    monkeypatch.setattr(
        "local_webpage_access.path_alias.StaticGateway.generate_alias_config",
        lambda self, instance_id, alias, host_port: None,
    )
    monkeypatch.setattr(
        "local_webpage_access.path_alias.StaticGateway.reload_all",
        lambda self: (_ for _ in ()).throw(GatewayError("Caddy reload 失败")),
    )

    resp = manager_env.client.patch(
        f"/api/instances/{iid}/path-alias",
        headers=manager_env.auth_headers(),
        json={"alias": "should-not-stick"},
    )
    assert resp.status_code == 500

    saved = InstanceManifest.load(mpath)
    assert saved.static is not None
    assert saved.static.routeHost == "keep-me"
    site = manager_env.registry.get_static_site(iid)
    assert site is not None
    assert site["route_host"] == "keep-me"


# ---- 浏览量统计 API（IMP-024 / DEV-061）--------------------------------------


def test_pageviews_summary_returns_dict(manager_env: EnvBundle) -> None:
    """GET /api/pageviews 返回 200 与 instances 映射（即便无数据也不报错）。"""
    resp = manager_env.client.get("/api/pageviews", headers=manager_env.auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert "instances" in data
    assert isinstance(data["instances"], dict)


def test_pageviews_ingests_builtin_gateway_log(manager_env: EnvBundle) -> None:
    """builtin 模式：写入 gateway.log CLF 后，浏览量汇总与详情都应计数。"""
    # 切到 builtin 后端，使摄入读取 per-instance gateway.log
    manager_env.config.staticGateway = "builtin"
    iid = manager_env.instance_id
    log_path = manager_env.workspace.app_logs(iid) / "gateway.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        '127.0.0.1 - - [09/Jul/2026 10:00:00] "GET / HTTP/1.1" 200 -\n'
        '192.168.1.7 - - [09/Jul/2026 10:01:00] "GET /about HTTP/1.1" 200 -\n',
        encoding="utf-8",
    )

    resp = manager_env.client.get("/api/pageviews", headers=manager_env.auth_headers())
    assert resp.status_code == 200
    pv = resp.json()["instances"].get(iid, {})
    assert pv.get("hits") == 2

    # 详情端点
    resp2 = manager_env.client.get(
        f"/api/instances/{iid}/pageviews", headers=manager_env.auth_headers()
    )
    assert resp2.status_code == 200
    detail = resp2.json()
    assert detail["instanceId"] == iid
    assert any(d["hits"] == 2 for d in detail["byDay"])
    assert len(detail["recent"]) == 2


def test_pageviews_detail_404_for_unknown_instance(manager_env: EnvBundle) -> None:
    """未知实例的详情端点返回 404（与实例详情一致）。"""
    resp = manager_env.client.get(
        "/api/instances/no-such-id/pageviews", headers=manager_env.auth_headers()
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


# ---- IMP-019：冗余实例 API + 管理页筛选（WBS-22.13）-------------------------


def _seed_redundant_duplicate(manager_env: EnvBundle) -> str:
    """导入第二个静态实例，并把它 original.zip 覆盖为首个实例同字节，
    使二者同指纹 → 第二个（更晚 created_at）成为冗余。"""
    from local_webpage_access.importer import Importer

    ws = manager_env.workspace
    zip2 = ws.inbox / "static-dup.zip"
    _make_static_zip(zip2, html="<h1>duplicate</h1>")
    importer = Importer(ws, manager_env.config, manager_env.registry)
    second_id = importer.import_zip(str(zip2)).instance_id
    # 覆盖 original.zip → 与首个实例同指纹
    first_zip = ws.app_original_zip(manager_env.instance_id)
    ws.app_original_zip(second_id).write_bytes(first_zip.read_bytes())
    return second_id


def test_instances_redundant_flag_false_when_unique(manager_env: EnvBundle) -> None:
    """IMP-019：唯一实例 redundant=false。"""
    resp = manager_env.client.get("/api/instances", headers=manager_env.auth_headers())
    assert resp.status_code == 200
    items = resp.json()["instances"]
    assert len(items) == 1
    assert items[0]["redundant"] is False


def test_api_redundant_empty_when_unique(manager_env: EnvBundle) -> None:
    """IMP-019：无冗余时 GET /api/redundant 返回空。"""
    resp = manager_env.client.get("/api/redundant", headers=manager_env.auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0
    assert body["instances"] == []


def test_api_redundant_lists_duplicate(manager_env: EnvBundle) -> None:
    """IMP-019：同指纹的第二个实例被列为冗余（保留最早者）。"""
    dup_id = _seed_redundant_duplicate(manager_env)
    resp = manager_env.client.get("/api/redundant", headers=manager_env.auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    ids = [i["id"] for i in body["instances"]]
    assert ids == [dup_id]
    assert manager_env.instance_id not in ids  # 最早者保留


def test_api_redundant_item_has_camel_case_keys(manager_env: EnvBundle) -> None:
    """IMP-019：冗余项字段为 camelCase（sourceZipHash / createdAt）。"""
    _seed_redundant_duplicate(manager_env)
    resp = manager_env.client.get("/api/redundant", headers=manager_env.auth_headers())
    item = resp.json()["instances"][0]
    assert set(item.keys()) == {"id", "name", "sourceZipHash", "createdAt"}
    assert item["sourceZipHash"]  # 带指纹


def test_instances_redundant_flag_true_for_duplicate(manager_env: EnvBundle) -> None:
    """IMP-019：/api/instances 对冗余实例标记 redundant=true，最早者仍 false。"""
    dup_id = _seed_redundant_duplicate(manager_env)
    resp = manager_env.client.get("/api/instances", headers=manager_env.auth_headers())
    assert resp.status_code == 200
    by_id = {i["id"]: i for i in resp.json()["instances"]}
    assert by_id[manager_env.instance_id]["redundant"] is False
    assert by_id[dup_id]["redundant"] is True


def test_api_remove_single_instance(manager_env: EnvBundle) -> None:
    """IMP-019：POST /api/instances/{id}/remove 移除单个实例（保留 apps/ 目录）。"""
    ws = manager_env.workspace
    iid = manager_env.instance_id
    assert ws.app_dir(iid).is_dir()
    resp = manager_env.client.post(
        f"/api/instances/{iid}/remove", headers=manager_env.auth_headers()
    )
    assert resp.status_code == 200
    assert resp.json()["action"] == "remove"
    # registry 已清，但 apps/ 目录保留（purge 默认 false）
    resp2 = manager_env.client.get(
        "/api/instances", headers=manager_env.auth_headers()
    )
    ids = [i["id"] for i in resp2.json()["instances"]]
    assert iid not in ids
    assert ws.app_dir(iid).is_dir()


def test_api_remove_unknown_instance_404(manager_env: EnvBundle) -> None:
    """IMP-019：移除不存在的实例 → 404。"""
    resp = manager_env.client.post(
        "/api/instances/nonexistent-id/remove", headers=manager_env.auth_headers()
    )
    assert resp.status_code == 404


def test_api_redundant_remove_batch(manager_env: EnvBundle) -> None:
    """IMP-019：POST /api/redundant/remove 批量移除冗余，保留最早者。"""
    dup_id = _seed_redundant_duplicate(manager_env)
    resp = manager_env.client.post(
        "/api/redundant/remove", headers=manager_env.auth_headers()
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "remove-redundant"
    assert body["removed"] == [dup_id]
    assert body["count"] == 1
    resp2 = manager_env.client.get(
        "/api/instances", headers=manager_env.auth_headers()
    )
    ids = [i["id"] for i in resp2.json()["instances"]]
    assert dup_id not in ids
    assert manager_env.instance_id in ids  # 最早者保留
