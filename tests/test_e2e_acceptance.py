"""V1 端到端验收（WBS-29）。

用真实样例验证 V1 完整闭环中**不依赖 Docker 守护进程**的部分：

* WBS-29.01 ``lwa init`` 干净工作区。
* WBS-29.02~04 导入静态 HTML，验证目录结构与静态托管可访问。
* WBS-29.05~07 导入 Vite/React，验证识别与前端形态。
* WBS-29.08 导入 Node/Express，验证识别与 compose 生成。
* WBS-29.10 导入 FastAPI+SQLite，验证识别与 compose 生成。
* WBS-29.13 start/stop/restart（静态实例，真实 HTTP 服务器）。
* WBS-29.14 logs/status/stats 可查询。
* WBS-29.15 管理页实例列表与 CLI 状态一致。
* WBS-29.17 failed/pending 展示。

Docker 容器构建/启动（WBS-29.09/11/12）依赖真实 Docker，见
``docs/acceptance-checklist.md`` 手工验收清单。

运行：``python -m pytest tests/test_e2e_acceptance.py -v``
"""

from __future__ import annotations

import socket
import time
import urllib.request
from pathlib import Path

import pytest

from tests.fixtures import build_zip


# ---- 工作区 fixture -------------------------------------------------------


@pytest.fixture
def ws_env(tmp_path: Path):
    from local_webpage_access.config import example_config_text, load_config
    from local_webpage_access.importer import Importer
    from local_webpage_access.init_workspace import init_workspace
    from local_webpage_access.paths import Workspace
    from local_webpage_access.registry import Registry

    root = tmp_path / "e2e-ws"
    init_workspace(root)
    ws = Workspace(root)
    ws.config_path.write_text(
        example_config_text().replace("staticGateway: caddy", "staticGateway: builtin"),
        encoding="utf-8",
    )
    config = load_config(ws)
    # BUG-121：双保险，避免示例配置回退
    config.staticGateway = "builtin"
    reg = Registry(ws.db_path)
    reg.open()
    importer = Importer(ws, config, reg)
    yield {"ws": ws, "config": config, "reg": reg, "importer": importer}
    reg.close()


def _import(ws_env, sample: str, tmp_path: Path, *, name: str | None = None):
    """导入样例，返回 ImportResult。"""
    zp = build_zip(sample, tmp_path / f"{name or sample}.zip")
    return ws_env["importer"].import_zip(str(zp))


def _wait_port_ready(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        try:
            sock.connect(("127.0.0.1", port))
            sock.close()
            return
        except OSError:
            time.sleep(0.2)
        finally:
            sock.close()
    pytest.fail(f"端口 {port} 在 {timeout}s 内未就绪")


# ---- WBS-29.01 干净工作区 -------------------------------------------------


def test_e2e_init_creates_clean_workspace(tmp_path: Path) -> None:
    from local_webpage_access.init_workspace import init_workspace
    from local_webpage_access.paths import Workspace

    root = tmp_path / "fresh"
    init_workspace(root)
    ws = Workspace(root)
    assert ws.config_path.is_file()
    assert ws.db_path.is_file()
    assert ws.inbox.is_dir()
    assert ws.apps.is_dir()
    assert ws.skills.is_dir()
    # 15 个内置 skills
    assert len(list(ws.skills.rglob("SKILL.md"))) == 15


# ---- WBS-29.02~04 静态 HTML 全链路 ----------------------------------------


def test_e2e_static_html_import_and_structure(ws_env, tmp_path: Path) -> None:
    result = _import(ws_env, "static_html", tmp_path)
    ws = ws_env["ws"]

    assert result.detection.kind.value == "static"
    assert not result.detection.pending

    # WBS-29.03 目录结构
    app_dir = ws.app_dir(result.instance_id)
    assert app_dir.is_dir()
    assert (app_dir / "local-web.json").is_file()
    # current/ 应有解压内容
    assert (ws.app_current(result.instance_id) / "index.html").is_file()

    # registry 登记
    assert ws_env["reg"].instance_exists(result.instance_id)


def test_e2e_static_html_accessible_via_http(ws_env, tmp_path: Path) -> None:
    """WBS-29.04：静态实例启动后可通过 HTTP 访问。"""
    result = _import(ws_env, "static_html", tmp_path, name="static-http")
    ws_env["config"].portPool.start = 21100
    ws_env["config"].portPool.end = 21150

    from local_webpage_access.lifecycle import start_instance, stop_instance_op
    from local_webpage_access.status import sync_status

    ws = ws_env["ws"]
    reg = ws_env["reg"]
    config = ws_env["config"]

    start_instance(ws, config, reg, result.instance_id)
    sync_status(ws, config, reg, result.instance_id)
    try:
        manifest_path = ws.app_manifest_path(result.instance_id)
        import json

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        host_port = manifest.get("static", {}).get("hostPort")
        assert host_port, f"静态实例未分配端口：{manifest}"

        _wait_port_ready(host_port, timeout=10)
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{host_port}/", timeout=5
        )
        body = resp.read().decode("utf-8")
        assert "static demo" in body.lower()
    finally:
        stop_instance_op(ws, config, reg, result.instance_id)


# ---- WBS-29.05~07 Vite/React 前端 -----------------------------------------


def test_e2e_vite_react_detected_as_frontend(ws_env, tmp_path: Path) -> None:
    result = _import(ws_env, "vite_react", tmp_path)
    assert result.detection.kind.value == "node"
    assert not result.detection.pending
    assert result.detection.form == "frontend-static"


# ---- WBS-29.08 Node/Express 后端 ------------------------------------------


def test_e2e_node_express_detected_and_compose_generated(
    ws_env, tmp_path: Path
) -> None:
    result = _import(ws_env, "node_express", tmp_path)
    assert result.detection.kind.value == "node"
    assert result.detection.form == "backend-container"
    assert not result.detection.pending

    from local_webpage_access.compose import generate_compose

    ws = ws_env["ws"]
    manifest = result.manifest
    assert manifest.container is not None, "后端容器实例应有 container 配置"
    out = generate_compose(manifest, ws, host_port=18200)
    assert out.is_file()
    text = out.read_text(encoding="utf-8")
    assert f"lwa-{result.instance_id}" in text
    assert "../data:/app/data" in text


# ---- WBS-29.10 FastAPI + SQLite -------------------------------------------


def test_e2e_fastapi_sqlite_detected_and_compose_generated(
    ws_env, tmp_path: Path
) -> None:
    result = _import(ws_env, "fastapi_sqlite", tmp_path)
    assert result.detection.kind.value == "python"
    assert not result.detection.pending

    from local_webpage_access.compose import generate_compose

    ws = ws_env["ws"]
    manifest = result.manifest
    assert manifest.container is not None
    out = generate_compose(manifest, ws, host_port=18300)
    text = out.read_text(encoding="utf-8")
    # WBS-29.12 前置：compose 含 data 挂载
    assert "../data:/app/data" in text


# ---- WBS-29.13 start/stop/restart（静态）----------------------------------


def test_e2e_start_stop_restart_static(ws_env, tmp_path: Path) -> None:
    ws_env["config"].portPool.start = 21200
    ws_env["config"].portPool.end = 21250

    from local_webpage_access.lifecycle import (
        restart_instance,
        start_instance,
        stop_instance_op,
    )
    from local_webpage_access.status import sync_status

    result = _import(ws_env, "static_html", tmp_path, name="lifecycle")
    ws = ws_env["ws"]
    reg = ws_env["reg"]
    config = ws_env["config"]
    iid = result.instance_id

    start_instance(ws, config, reg, iid)
    sync_status(ws, config, reg, iid)
    assert reg.get_instance(iid)["status"] == "running"

    restart_instance(ws, config, reg, iid)
    sync_status(ws, config, reg, iid)
    assert reg.get_instance(iid)["status"] == "running"

    stop_instance_op(ws, config, reg, iid)
    sync_status(ws, config, reg, iid)
    assert reg.get_instance(iid)["status"] == "stopped"


# ---- WBS-29.14 logs/status/stats ------------------------------------------


def test_e2e_logs_status_stats_queryable(ws_env, tmp_path: Path) -> None:
    from local_webpage_access.logs import list_logs
    from local_webpage_access.status import all_statuses, status_counts

    result = _import(ws_env, "static_html", tmp_path, name="queryable")
    reg = ws_env["reg"]
    ws = ws_env["ws"]
    config = ws_env["config"]

    # status
    statuses = all_statuses(ws, config, reg)
    assert any(s.id == result.instance_id for s in statuses)

    # counts
    counts = status_counts(reg)
    assert sum(counts.values()) >= 1

    # logs（至少有事件可查）
    events = reg.list_events(result.instance_id, limit=100)
    assert events
    # list_logs 返回 LogInfo 列表（即使没有日志文件也不应报错）
    list_logs(ws, result.instance_id)


# ---- WBS-29.15 管理页实例列表与 CLI 一致 -----------------------------------


def test_e2e_manager_api_matches_cli_status(ws_env, tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from local_webpage_access.manager_api import create_app, ensure_token
    from local_webpage_access.status import all_statuses

    _import(ws_env, "static_html", tmp_path, name="mgr")
    _import(ws_env, "pending_unknown", tmp_path, name="mgr-pending")

    ws = ws_env["ws"]
    reg = ws_env["reg"]
    config = ws_env["config"]
    token = ensure_token(ws)
    app = create_app(ws, config, reg, token=token)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}

    r = client.get("/api/instances", headers=headers)
    assert r.status_code == 200
    api_ids = {i["id"] for i in r.json()["instances"]}

    cli_ids = {s.id for s in all_statuses(ws, config, reg)}
    assert api_ids == cli_ids, "管理页与 CLI 状态不一致"


# ---- WBS-29.17 failed/pending 展示 ----------------------------------------


def test_e2e_failed_and_pending_display(ws_env, tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from local_webpage_access.manager_api import create_app, ensure_token

    _import(ws_env, "pending_unknown", tmp_path, name="e2e-pending")

    ws = ws_env["ws"]
    reg = ws_env["reg"]
    config = ws_env["config"]
    token = ensure_token(ws)

    # 模拟一个 failed 实例
    from tests._helpers import make_static_manifest

    reg.upsert_from_manifest(make_static_manifest("e2e-failed"))
    reg.update_status("e2e-failed", "failed", last_error="模拟构建失败")

    app = create_app(ws, config, reg, token=token)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}

    # pending 端点（同时含 pending 与 failed）
    r = client.get("/api/pending", headers=headers)
    assert r.status_code == 200
    body = r.json()
    pending_ids = {p["id"] for p in body["pending"]}
    failed_ids = {f["id"] for f in body["failed"]}
    assert "e2e-pending" in pending_ids
    assert "e2e-failed" in failed_ids

    # failed 实例详情
    r2 = client.get("/api/instances/e2e-failed", headers=headers)
    assert r2.status_code == 200
    detail = r2.json()["instance"]
    assert detail["status"] == "failed"


# ---- WBS-29.16 doctor 可对实例诊断 ----------------------------------------


def test_e2e_doctor_diagnoses_instance(ws_env, tmp_path: Path, monkeypatch) -> None:
    import subprocess

    from local_webpage_access.doctor import CheckResult, STATUS_OK, run_doctor

    result = _import(ws_env, "static_html", tmp_path, name="doctor-target")
    ws = ws_env["ws"]
    config = ws_env["config"]
    config.staticGateway = "builtin"

    monkeypatch.setattr(
        "local_webpage_access.doctor.check_python_packages",
        lambda: CheckResult("python_packages", STATUS_OK, "mocked ok"),
    )

    # 注入一个让 docker / docker compose 检查通过的 runner，
    # 使本测试不依赖宿主机真实 Docker（实例诊断本身不需要 Docker）。
    def _ok_runner(args):
        if len(args) >= 2 and args[0] == "docker" and args[1] == "version":
            return subprocess.CompletedProcess(args, 0, stdout="29.6.1\n", stderr="")
        if len(args) >= 3 and args[:3] == ["docker", "compose", "version"]:
            return subprocess.CompletedProcess(args, 0, stdout="5.2.0\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="ok\n", stderr="")

    def _all_ports_free(port: int) -> bool:
        return False

    report = run_doctor(
        ws,
        config,
        instance_id=result.instance_id,
        runner=_ok_runner,
        port_in_use=_all_ports_free,
    )
    # 针对存在的实例不应产生 fail（warning 可接受）
    assert not report.has_failures, (
        f"实例 {result.instance_id} 诊断失败：{[c.to_dict() for c in report.failures()]}"
    )
