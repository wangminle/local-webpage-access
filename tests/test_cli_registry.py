"""``lwa registry check/repair`` CLI 端到端测试（BUG-473）。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from local_webpage_access.init_workspace import init_workspace
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry
from tests._helpers import make_static_manifest


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "ws"
    init_workspace(root)
    return Workspace(root)


def _inject_orphan(db_path: Path, instance_id: str, route_host: str) -> None:
    """用关闭外键的裸连接塞入孤儿子表行，模拟历史残留。"""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO static_sites(instance_id, route_mode, route_host) VALUES (?, 'name', ?)",
            (instance_id, route_host),
        )
        conn.commit()
    finally:
        conn.close()


def test_registry_check_reports_orphans(workspace: Workspace, monkeypatch) -> None:
    """``lwa registry check --json`` 列出孤儿、不误报真实实例。"""
    from local_webpage_access.cli.registry import app

    reg = Registry(workspace.db_path)
    reg.open()
    try:
        reg.upsert_from_manifest(make_static_manifest("real"))
    finally:
        reg.close()
    _inject_orphan(workspace.db_path, "ghost", "ghost-alias")

    monkeypatch.chdir(workspace.root)
    result = CliRunner().invoke(app, ["check", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["count"] >= 1
    assert any(
        o["instance_id"] == "ghost" and o["table"] == "static_sites" for o in data["orphans"]
    )
    # 真实实例不算孤儿
    assert all(o["instance_id"] != "real" for o in data["orphans"])


def test_registry_repair_cleans_orphans(workspace: Workspace, monkeypatch) -> None:
    """``lwa registry repair --yes`` 删除孤儿并持久化。"""
    from local_webpage_access.cli.registry import app

    _inject_orphan(workspace.db_path, "ghost", "ghost-alias")
    monkeypatch.chdir(workspace.root)

    result = CliRunner().invoke(app, ["repair", "--yes", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["deleted"] >= 1

    reg = Registry(workspace.db_path)
    reg.open()
    try:
        assert reg.find_orphan_rows() == []
    finally:
        reg.close()


def test_registry_repair_requires_yes_in_non_tty(workspace: Workspace, monkeypatch) -> None:
    """非 TTY 且无 --yes 时拒绝破坏性清理（CliRunner 默认非 TTY）。"""
    from local_webpage_access.cli.registry import app

    _inject_orphan(workspace.db_path, "ghost", "ghost-alias")
    monkeypatch.chdir(workspace.root)

    result = CliRunner().invoke(app, ["repair"])
    assert result.exit_code == 1
    assert "非交互环境" in result.output or "--yes" in result.output

    # 孤儿应仍存在（未清理）
    reg = Registry(workspace.db_path)
    reg.open()
    try:
        assert reg.find_orphan_rows()
    finally:
        reg.close()


def test_registry_check_clean_when_no_orphans(workspace: Workspace, monkeypatch) -> None:
    """无孤儿时 ``check`` 报告干净、退出 0。"""
    from local_webpage_access.cli.registry import app

    monkeypatch.chdir(workspace.root)
    result = CliRunner().invoke(app, ["check"])
    assert result.exit_code == 0, result.output
    assert "无孤儿数据" in result.output
