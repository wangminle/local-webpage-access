"""S3-04 / DEV-160 / issue #59：操作时间线（事件来源、合并排序、API、CLI）。"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from local_webpage_access.cli import app as cli_app
from local_webpage_access.config import Config, PortPool
from local_webpage_access.manager_api import create_app, ensure_token
from local_webpage_access.models import InstanceManifest
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry
from local_webpage_access.registry.connection import CURRENT_SCHEMA_VERSION, get_schema_version
from local_webpage_access.timeline import build_timeline, format_timeline_lines


def _make_static_zip(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("index.html", "<h1>hello</h1>")


@pytest.fixture()
def manager_env(workspace_root: Path):
    ws = Workspace(workspace_root)
    ws.ensure_workspace_dirs()
    from local_webpage_access.config import example_config_text

    if not ws.config_path.is_file():
        ws.config_path.write_text(example_config_text(), encoding="utf-8")
    config = Config(staticGateway="builtin", portPool=PortPool(start=21000, end=21050))
    reg = Registry(ws.db_path)
    reg.open()

    from local_webpage_access.importer import Importer

    zip_path = ws.inbox / "static.zip"
    _make_static_zip(zip_path)
    result = Importer(ws, config, reg).import_zip(str(zip_path))

    token = ensure_token(ws)
    app = create_app(ws, config, reg, token=token)
    with TestClient(app) as client:
        yield {
            "workspace": ws,
            "config": config,
            "registry": reg,
            "client": client,
            "token": token,
            "instance_id": result.instance_id,
        }


def _headers(env: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {env['token']}"}


def _seed_demo(workspace: Workspace, registry: Registry) -> None:
    from local_webpage_access.models import Kind, Runtime

    workspace.ensure_app_dirs("demo")
    m = InstanceManifest(
        id="demo",
        name="demo",
        kind=Kind.STATIC,
        runtime=Runtime.SHARED_STATIC,
        version="1",
        servingMode="shared-static",
    )
    m.save(workspace.app_manifest_path("demo"))
    registry.upsert_from_manifest(m)


# ---- schema / 事件来源 -------------------------------------------------------


def test_schema_v8_adds_event_source_column(workspace_root: Path) -> None:
    reg = Registry(Workspace(workspace_root).db_path)
    reg.open()
    try:
        assert get_schema_version(reg.conn) == CURRENT_SCHEMA_VERSION == 8
        columns = {row["name"] for row in reg.conn.execute("PRAGMA table_info(events)")}
        assert "source" in columns
    finally:
        reg.close()


def test_add_event_defaults_source_to_current_op_source(
    workspace: Workspace, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LWA_OP_SOURCE", "daemon")
    _seed_demo(workspace, registry)
    registry.add_event("demo", "lifecycle_stage", "stage=x")
    row = registry.list_events("demo", limit=1)[0]
    assert row["source"] == "daemon"

    registry.add_event("demo", "config", "显式来源", source="agent")
    row = registry.list_events("demo", limit=1)[0]
    assert row["source"] == "agent"


# ---- 合并与排序 ----------------------------------------------------------------


def test_build_timeline_merges_and_sorts_newest_first(
    workspace: Workspace, registry: Registry
) -> None:
    _seed_demo(workspace, registry)
    registry.add_event("demo", "lifecycle_stage", "stage=early", source="cli")
    registry.conn.execute("UPDATE events SET created_at = '2001-01-01T00:00:00+00:00'")
    build_id = registry.add_build("demo", status="running", started_at="2000-01-01T00:00:00+00:00")
    registry.finish_build(build_id, status="failed", error_summary="构建失败：缺少依赖")

    items = build_timeline(
        registry,
        "demo",
        last_operation={
            "kind": "rebuild",
            "status": "failed",
            "startedAt": "2099-01-01T00:00:00+00:00",
            "finishedAt": "2099-01-01T00:01:30+00:00",
            "error": "构建失败",
            "source": "manager",
        },
    )

    kinds = [item["kind"] for item in items]
    assert kinds == ["operation", "build", "event"]
    op = items[0]
    assert op["source"] == "manager" and op["status"] == "failed"
    assert op["durationSeconds"] == 90
    build = items[1]
    assert build["status"] == "failed" and build["message"] == "构建失败：缺少依赖"
    assert build["source"] == "unknown"
    assert items[2]["source"] == "cli"


def test_build_timeline_prefers_registry_last_operation(
    workspace: Workspace, registry: Registry
) -> None:
    """构建失败只写 registry.last_operation_json 时，不得因 manifest 陈旧而漏掉失败操作。"""
    _seed_demo(workspace, registry)
    registry.update_last_operation(
        "demo",
        {
            "kind": "rebuild",
            "status": "failed",
            "startedAt": "2099-01-01T00:00:00+00:00",
            "finishedAt": "2099-01-01T00:00:08+00:00",
            "error": "构建失败，旧版本仍在服务",
            "source": "cli",
        },
    )
    items = build_timeline(
        registry,
        "demo",
        last_operation={
            "kind": "start",
            "status": "succeeded",
            "startedAt": "2000-01-01T00:00:00+00:00",
            "finishedAt": "2000-01-01T00:00:01+00:00",
            "source": "cli",
        },
    )
    ops = [item for item in items if item["kind"] == "operation"]
    assert ops and ops[0]["status"] == "failed"
    assert ops[0]["message"] == "构建失败，旧版本仍在服务"


def test_build_timeline_marks_legacy_event_source_unknown(
    workspace: Workspace, registry: Registry
) -> None:
    _seed_demo(workspace, registry)
    registry.add_event("demo", "start", "旧事件")
    registry.conn.execute("UPDATE events SET source = NULL")
    items = build_timeline(registry, "demo")
    assert items[0]["source"] == "unknown"


def test_build_timeline_respects_limit_and_skips_missing_timestamps(
    workspace: Workspace, registry: Registry
) -> None:
    _seed_demo(workspace, registry)
    for i in range(5):
        registry.add_event("demo", "start", f"事件 {i}")
    items = build_timeline(
        registry,
        "demo",
        last_operation={"kind": "start", "status": "running"},  # 无时间戳 → 跳过
        limit=3,
    )
    assert len(items) == 3
    assert all(item["kind"] == "event" for item in items)


def test_format_timeline_lines_shows_source_and_status() -> None:
    lines = format_timeline_lines(
        [
            {
                "at": "2026-10-14T10:00:00+08:00",
                "kind": "operation",
                "type": "rebuild",
                "status": "failed",
                "source": "daemon",
                "message": "构建失败",
                "durationSeconds": 12,
            }
        ]
    )
    assert lines == [
        "2026-10-14T10:00:00+08:00  [daemon] rebuild (failed) 耗时 12s  构建失败"
    ]


# ---- API ----------------------------------------------------------------------


def test_timeline_api_returns_merged_items(manager_env: dict) -> None:
    iid = manager_env["instance_id"]
    reg = manager_env["registry"]
    reg.add_event(iid, "lifecycle_stage", "stage=manual", source="manager")

    ws = manager_env["workspace"]
    manifest = InstanceManifest.load(ws.app_manifest_path(iid))
    manifest.lastOperation = {
        "kind": "rebuild",
        "status": "failed",
        "startedAt": "2099-01-01T00:00:00+00:00",
        "finishedAt": "2099-01-01T00:00:05+00:00",
        "error": "验证失败",
        "source": "daemon",
    }
    manifest.save(ws.app_manifest_path(iid))

    resp = manager_env["client"].get(
        f"/api/instances/{iid}/timeline?limit=10", headers=_headers(manager_env)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["instanceId"] == iid
    items = body["items"]
    assert items[0]["kind"] == "operation"
    assert items[0]["source"] == "daemon"
    assert items[0]["status"] == "failed"
    event = next(item for item in items if item["kind"] == "event" and item["source"] == "manager")
    assert event["type"] == "lifecycle_stage"


def test_instance_detail_includes_timeline_drawer_data(manager_env: dict) -> None:
    iid = manager_env["instance_id"]
    resp = manager_env["client"].get(f"/api/instances/{iid}", headers=_headers(manager_env))
    assert resp.status_code == 200
    timeline = resp.json()["timeline"]
    assert isinstance(timeline["items"], list)
    assert timeline["items"] and all("source" in item for item in timeline["items"])


def test_timeline_api_unknown_instance_404(manager_env: dict) -> None:
    resp = manager_env["client"].get(
        "/api/instances/no-such-instance/timeline", headers=_headers(manager_env)
    )
    assert resp.status_code == 404


def test_timeline_api_limit_validation(manager_env: dict) -> None:
    iid = manager_env["instance_id"]
    resp = manager_env["client"].get(
        f"/api/instances/{iid}/timeline?limit=0", headers=_headers(manager_env)
    )
    assert resp.status_code == 422


# ---- CLI ----------------------------------------------------------------------


def test_logs_timeline_cli_prints_sources(
    manager_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    iid = manager_env["instance_id"]
    manager_env["registry"].add_event(iid, "lifecycle_stage", "stage=cli", source="cli")

    def open_env():
        return manager_env["workspace"], manager_env["config"], manager_env["registry"]

    monkeypatch.setattr("local_webpage_access.cli.lifecycle.open_workspace_registry", open_env)
    result = CliRunner().invoke(cli_app, ["logs", iid, "--timeline", "--tail", "5"])
    assert result.exit_code == 0, result.output
    assert "[cli] lifecycle_stage" in result.output


def test_logs_timeline_cli_unknown_instance_exits_1(
    manager_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    def open_env():
        return manager_env["workspace"], manager_env["config"], manager_env["registry"]

    monkeypatch.setattr("local_webpage_access.cli.lifecycle.open_workspace_registry", open_env)
    result = CliRunner().invoke(cli_app, ["logs", "no-such", "--timeline"])
    assert result.exit_code == 1


def test_logs_timeline_cli_survives_corrupt_manifest(
    manager_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """manifest 损坏时 CLI 时间线降级为只含事件/构建（并提示），不能抛异常中断。"""
    iid = manager_env["instance_id"]
    manager_env["registry"].add_event(iid, "lifecycle_stage", "stage=cli", source="cli")
    manager_env["workspace"].app_manifest_path(iid).write_text("{not json", encoding="utf-8")

    def open_env():
        return manager_env["workspace"], manager_env["config"], manager_env["registry"]

    monkeypatch.setattr("local_webpage_access.cli.lifecycle.open_workspace_registry", open_env)
    result = CliRunner().invoke(cli_app, ["logs", iid, "--timeline", "--tail", "5"])
    assert result.exit_code == 0, result.output
    assert "manifest 读取失败" in result.output
    assert "[cli] lifecycle_stage" in result.output
