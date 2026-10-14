"""扫描器生成的迁移启动命令必须触发回滚确认，旧命令留档不能遮蔽快照。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from local_webpage_access import hosting, rollback, scanner, versions
from local_webpage_access.manager_history_routes import register_history_routes, versions_payload
from tests.scripts import test_s3_01_rollback as s301
from tests.scripts._helpers import make_container_manifest

deployed = s301.deployed
rt = s301.rt


@pytest.fixture()
def scanner_start(tmp_path):
    project = tmp_path / "fastapi"
    project.mkdir()
    (project / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
    (project / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    command = scanner._python_start_command(["fastapi"], scanner.summarize(project))
    assert command and "alembic upgrade head" in command
    return command


@pytest.fixture()
def target_start(workspace, deployed, scanner_start):
    """目标有迁移，当前配置没有；模拟已经产生的空命令留档。"""
    snap = versions.build_snapshot_dir(workspace, "demo", deployed["b1"])
    (snap / "Dockerfile").write_text(
        "FROM python:3.13\nCMD " + json.dumps(["sh", "-c", scanner_start]) + "\n",
        encoding="utf-8",
    )
    versions.write_migration_archive(workspace, "demo", deployed["b1"], "")
    assert not rollback.prestart_has_migration(deployed["manifest"])
    return deployed["b1"]


@pytest.fixture()
def verification_passed(monkeypatch):
    monkeypatch.setattr(
        hosting, "_evaluate_container_verification", lambda *a, **k: s301._verify("passed")
    )


def test_scanner_start_requires_current_confirmation(scanner_start):
    manifest = make_container_manifest("demo")
    manifest.entry.start = scanner_start
    assert not manifest.preStart and not manifest.buildHooks
    assert rollback.prestart_has_migration(manifest)


def test_archive_includes_scanner_start(workspace, scanner_start):
    manifest = make_container_manifest("demo")
    manifest.entry.start = scanner_start
    rollback.archive_version_migration(workspace, "demo", 1, manifest)
    archived = versions.read_migration_archive(workspace, "demo", 1)
    assert archived == (scanner_start, "commands")


def test_archiving_does_not_hide_scanner_migration(workspace, deployed, target_start, scanner_start):
    snap = versions.build_snapshot_dir(workspace, "demo", target_start)
    (snap / versions.MIGRATION_COMMANDS_FILE).unlink()
    assert rollback.target_version_has_migration(workspace, "demo", target_start)
    manifest = deployed["manifest"].model_copy(deep=True)
    manifest.entry.start = scanner_start
    rollback.archive_version_migration(workspace, "demo", target_start, manifest)
    assert rollback.target_version_has_migration(workspace, "demo", target_start)


@pytest.mark.parametrize("commands", ["", "echo ready"])
def test_incomplete_archive_cannot_hide_snapshot_migration(workspace, target_start, commands):
    versions.write_migration_archive(workspace, "demo", target_start, commands)
    assert rollback.target_version_has_migration(workspace, "demo", target_start)


def test_snapshot_comments_do_not_require_confirmation(workspace):
    versions.write_migration_archive(workspace, "demo", 1, "echo ready")
    snap = versions.build_snapshot_dir(workspace, "demo", 1)
    (snap / "Dockerfile").write_text(
        '# RUN alembic upgrade head\nFROM python:3.13\nCMD ["python", "app.py"]\n',
        encoding="utf-8",
    )
    assert not rollback.target_version_has_migration(workspace, "demo", 1)


def test_rollback_rejects_incomplete_archive_before_side_effects(
    workspace, registry, config, target_start, rt, verification_passed
):
    _, order, tags = rt
    before = workspace.app_manifest_path("demo").read_bytes()
    revision = registry.get_revision("demo")
    with pytest.raises(rollback.RollbackNeedsYes):
        rollback.rollback_instance(workspace, config, registry, "demo", to_build_id=target_start)
    assert order == [] and tags == []
    assert workspace.app_manifest_path("demo").read_bytes() == before
    assert registry.get_revision("demo") == revision


def test_rollback_explicit_confirmation_allows_scanner_migration(
    workspace, registry, config, target_start, rt, verification_passed
):
    _, order, _ = rt
    manifest = rollback.rollback_instance(
        workspace, config, registry, "demo", to_build_id=target_start, yes=True
    )
    assert manifest.lastOperation["toBuildId"] == target_start
    assert order == ["stop", "up"]


def test_cli_rejects_target_scanner_migration_without_yes(
    workspace, registry, config, target_start, rt, verification_passed, monkeypatch
):
    from local_webpage_access.cli import app

    monkeypatch.setattr(
        "local_webpage_access.cli.versions.open_workspace_registry",
        lambda: (workspace, config, registry),
    )
    _, order, tags = rt
    result = CliRunner().invoke(app, ["rollback", "demo", "--to", str(target_start)])
    assert result.exit_code == 1, result.output
    assert "--yes" in result.output
    assert order == [] and tags == []


def test_manager_version_flags_include_target_scanner_migration(
    workspace, registry, config, target_start, rt, monkeypatch
):
    monkeypatch.setattr(versions, "image_disk_usage_bytes", lambda refs: 0)
    payload = versions_payload(workspace, config, registry, "demo")
    item = next(row for row in payload["versions"] if row["id"] == target_start)
    assert item["hasMigration"] and item["requiresConfirm"]
    assert payload["requiresConfirm"]


def test_manager_rejects_target_scanner_migration_without_confirm(
    workspace, registry, config, target_start, rt, verification_passed, monkeypatch
):
    # 真实历史路由、Registry 和回滚守卫；只隔离认证、Docker 能力与状态探测。
    monkeypatch.setattr("local_webpage_access.status.sync_status", lambda *a, **k: None)
    monkeypatch.setattr(
        "local_webpage_access.status.instance_status",
        lambda *a, **k: SimpleNamespace(to_dict=lambda: {}),
    )
    app = FastAPI()
    app.state.workspace = workspace
    app.state.config = config
    app.state.registry = registry
    register_history_routes(
        app, api=Depends(lambda: None), docker_ops_blocked_reason=lambda *a: None
    )
    _, order, tags = rt
    with TestClient(app) as client:
        response = client.post(
            "/api/instances/demo/rollback", json={"toBuildId": target_start, "confirm": False}
        )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"]["code"] == "rollback_needs_yes"
    assert order == [] and tags == []
