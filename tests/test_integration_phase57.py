"""Phase 5-7 集成测试（WBS-28）。

跨模块串联 daemon / manager_api / security / doctor，验证新模块协同工作：

1. daemon.process_zip 导入后，manager API 能列出该实例。
2. pending 实例在 manager API 的 /api/pending 中出现，且 doctor 诊断为 pending。
3. security.audit_compose 对 generate_compose 产出的文件零 critical。
4. doctor 对失败实例给出可读建议。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from tests.fixtures import build_zip


@pytest.fixture
def env(tmp_path: Path):
    from local_web_access.config import example_config_text, load_config
    from local_web_access.init_workspace import init_workspace
    from local_web_access.importer import Importer
    from local_web_access.manager_api import ensure_token
    from local_web_access.paths import Workspace
    from local_web_access.registry import Registry

    root = tmp_path / "ws"
    init_workspace(root)
    ws = Workspace(root)
    ws.config_path.write_text(example_config_text(), encoding="utf-8")
    config = load_config(ws)
    reg = Registry(ws.db_path)
    reg.open()
    token = ensure_token(ws)
    importer = Importer(ws, config, reg)
    yield {
        "ws": ws,
        "config": config,
        "reg": reg,
        "token": token,
        "importer": importer,
    }
    reg.close()


def _make_static_zip(tmp_path: Path) -> Path:
    return build_zip("static_html", tmp_path / "static.zip")


# ---- daemon → manager API 串联 -------------------------------------------


def test_daemon_import_visible_in_manager_api(env, tmp_path: Path) -> None:
    """daemon.process_zip 导入的实例应出现在 manager API /api/instances。"""
    from fastapi.testclient import TestClient

    from local_web_access.daemon import process_zip
    from local_web_access.manager_api import create_app

    ws = env["ws"]
    reg = env["reg"]
    zp = _make_static_zip(tmp_path)
    # 复制到 inbox
    inbox_zip = ws.inbox / "static.zip"
    inbox_zip.write_bytes(zp.read_bytes())

    process_zip(ws, env["config"], reg, inbox_zip)

    app = create_app(ws, env["config"], reg, token=env["token"])
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {env['token']}"}
    r = client.get("/api/instances", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert any(i["id"] == "static" for i in data["instances"])
    # BUG：泄漏兜底——process_zip 自动 start 的内置静态服务（http.server 子进程）
    # 在测试结束未停会成为孤儿，跨用例累积会占满端口池、使全量测试连跑即红。
    from local_web_access.lifecycle import stop_instance_op

    stop_instance_op(ws, env["config"], reg, "static")


def test_pending_instance_shown_in_manager_pending_endpoint(
    env, tmp_path: Path
) -> None:
    """pending 实例应出现在 /api/pending。"""
    from fastapi.testclient import TestClient

    zp = build_zip("pending_unknown", tmp_path / "pending.zip")
    env["importer"].import_zip(str(zp))

    from local_web_access.manager_api import create_app

    app = create_app(env["ws"], env["config"], env["reg"], token=env["token"])
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {env['token']}"}
    r = client.get("/api/pending", headers=headers)
    assert r.status_code == 200
    ids = [p["id"] for p in r.json()["pending"]]
    assert "pending" in ids


# ---- security × compose ---------------------------------------------------


def test_security_audit_generated_compose_clean(env, tmp_path: Path) -> None:
    """为容器实例生成的 compose 必须通过 security.audit_compose。"""
    from local_web_access.compose import generate_compose
    from local_web_access.security import audit_compose, has_critical

    from tests._helpers import make_container_manifest

    manifest = make_container_manifest("itest-api")
    out = generate_compose(manifest, env["ws"], host_port=18100)
    findings = audit_compose(out.read_text(encoding="utf-8"))
    assert not has_critical(findings), [f.code for f in findings if f.level == "critical"]


# ---- doctor × 失败实例 ----------------------------------------------------


def test_doctor_diagnoses_failed_instance_with_suggestion(env, tmp_path: Path) -> None:
    """doctor 对 failed 实例应输出 fail 级检查与可读建议。"""
    from tests._helpers import make_static_manifest

    from local_web_access.doctor import diagnose_instance

    env["reg"].upsert_from_manifest(make_static_manifest("dead"))
    env["reg"].update_status(
        "dead", "failed", last_error="容器启动后立即退出（exit 137）"
    )
    results = diagnose_instance(env["ws"], env["reg"], "dead")
    status_check = [r for r in results if r.name.endswith(":status")]
    assert status_check
    assert status_check[0].status == "fail"
    assert status_check[0].detail == "容器启动后立即退出（exit 137）"
    assert status_check[0].suggestion  # 必须给出建议


# ---- token 安全 × manager API --------------------------------------------


def test_manager_api_rejects_unauthorized(env) -> None:
    from fastapi.testclient import TestClient

    from local_web_access.manager_api import create_app

    app = create_app(env["ws"], env["config"], env["reg"], token=env["token"])
    client = TestClient(app)
    # 无 token
    r = client.get("/api/instances")
    assert r.status_code == 401
    # 错误 token
    r2 = client.get(
        "/api/instances", headers={"Authorization": "Bearer wrong-token"}
    )
    assert r2.status_code == 401
    # /api/health 不需要 token
    r3 = client.get("/api/health")
    assert r3.status_code == 200


def test_manager_api_accepts_query_token(env) -> None:
    from fastapi.testclient import TestClient

    from local_web_access.manager_api import create_app

    app = create_app(env["ws"], env["config"], env["reg"], token=env["token"])
    client = TestClient(app)
    r = client.get(f"/api/stats?token={env['token']}")
    assert r.status_code == 200


# ---- doctor × 安全绑定检查 ------------------------------------------------


def test_doctor_lan_binding_check_in_report(env) -> None:
    """run_doctor 报告应包含端口池检查（WBS-26.05）。"""
    from local_web_access.doctor import run_doctor

    report = run_doctor(
        env["ws"], env["config"],
        port_in_use=lambda _p: False,
        runner=lambda args: __import__("subprocess").CompletedProcess(
            args=list(args), returncode=127, stdout="", stderr="no docker"
        ),
    )
    names = [c.name for c in report.checks]
    assert "port_pool" in names
