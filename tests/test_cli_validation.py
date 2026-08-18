"""CLI 参数在触及工作区前完成边界校验。"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests._helpers import make_static_manifest


def test_daemon_on_rejects_nonpositive_poll() -> None:
    """BUG-314：非正 poll 不能启动 CPU 空转 watcher。"""
    from local_webpage_access.cli.daemon import app

    for value in ("0", "-1"):
        result = CliRunner().invoke(app, ["on", "--poll", value])
        assert result.exit_code == 2
        assert "Invalid value" in result.output


def test_init_rejects_unknown_static_gateway(tmp_path) -> None:
    """BUG-315：非法 staticGateway 不得写入工作区配置。"""
    from local_webpage_access.cli import app

    workspace = tmp_path / "workspace"
    result = CliRunner().invoke(
        app,
        [
            "init",
            "--workspace",
            str(workspace),
            "--static-gateway",
            "invalid",
            "--no-install-docker",
        ],
    )

    assert result.exit_code == 2
    assert not (workspace / "local-web.yml").exists()


def test_manager_start_rejects_out_of_range_port() -> None:
    """BUG-317：非法端口必须由 CLI 参数层拒绝，不能进入 uvicorn。"""
    from local_webpage_access.cli.manager import app

    for value in ("0", "99999"):
        result = CliRunner().invoke(app, ["start", "--port", value])
        assert result.exit_code == 2
        assert "Invalid value" in result.output


def test_run_handles_broken_pipe_cleanly(monkeypatch) -> None:
    """BUG-340：管道截断时捕获 BrokenPipeError，干净退出（0 或 141）。"""
    import io
    import sys

    import local_webpage_access.cli as cli_mod

    def boom() -> None:
        raise BrokenPipeError()

    monkeypatch.setattr(cli_mod, "app", boom)
    # 用可关闭替身，避免 handler 关掉 pytest 的 capture 流
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    with pytest.raises(SystemExit) as excinfo:
        cli_mod.run()
    assert excinfo.value.code in (0, 141)


def test_import_update_rejects_path_alias(monkeypatch, tmp_path: Path) -> None:
    """BUG-342：--update 与 --path-alias 组合须显式拒绝，不得静默忽略。"""
    from local_webpage_access.cli import app
    from local_webpage_access.init_workspace import init_workspace

    root = tmp_path / "ws"
    init_workspace(root)
    zip_path = tmp_path / "site.zip"
    zip_path.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    monkeypatch.chdir(root)
    monkeypatch.setattr(
        "local_webpage_access.platform_support.require_supported_platform",
        lambda **kw: None,
    )

    result = CliRunner().invoke(
        app,
        [
            "import",
            str(zip_path),
            "--update",
            "demo",
            "--path-alias",
            "my-alias",
            "--yes",
        ],
    )
    assert result.exit_code == 2
    assert "--path-alias" in result.output
    assert "--update" in result.output


def test_doctor_rejects_illegal_profile(monkeypatch, tmp_path: Path) -> None:
    """BUG-343：--profile 非法值须 exit 2，不得静默降级。"""
    from local_webpage_access.cli import app
    from local_webpage_access.init_workspace import init_workspace

    root = tmp_path / "ws"
    init_workspace(root)
    monkeypatch.chdir(root)

    result = CliRunner().invoke(app, ["doctor", "--profile", "turbo"])
    assert result.exit_code == 2
    assert "profile" in result.output.lower() or "default" in result.output


def test_scan_holds_instance_lock_while_saving(monkeypatch, tmp_path: Path) -> None:
    """BUG-341：lwa scan 写 local-web.json 前必须持有 instance_lock。"""
    from local_webpage_access.cli import app
    from local_webpage_access.init_workspace import init_workspace
    from local_webpage_access.models import InstanceManifest
    from local_webpage_access.paths import Workspace
    from local_webpage_access.registry import Registry

    root = tmp_path / "ws"
    init_workspace(root)
    ws = Workspace(root)
    ws.ensure_app_dirs("demo")
    (ws.app_current("demo") / "index.html").write_text("<html></html>", encoding="utf-8")
    manifest = make_static_manifest("demo")
    manifest.save(ws.app_manifest_path("demo"))
    reg = Registry(ws.db_path)
    reg.open()
    reg.upsert_from_manifest(manifest)
    reg.close()

    lock_held = False
    saved_under_lock = False
    original_save = InstanceManifest.save

    def checked_save(self, path):
        nonlocal saved_under_lock
        saved_under_lock = lock_held
        return original_save(self, path)

    @contextlib.contextmanager
    def tracked_lock(*args, **kwargs):
        nonlocal lock_held
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    monkeypatch.chdir(root)
    monkeypatch.setattr(
        "local_webpage_access.platform_support.require_supported_platform",
        lambda **kw: None,
    )
    monkeypatch.setattr("local_webpage_access.lifecycle.instance_lock", tracked_lock)
    monkeypatch.setattr(InstanceManifest, "save", checked_save)

    result = CliRunner().invoke(app, ["scan", "demo"])
    assert result.exit_code == 0, result.output
    assert saved_under_lock, "scan 保存 manifest 时未持实例锁"


def test_cli_recover_calls_lifecycle(monkeypatch, tmp_path: Path) -> None:
    """CLI recover 应委托 lifecycle.recover_instance。"""
    from local_webpage_access.cli import app
    from local_webpage_access.init_workspace import init_workspace

    root = tmp_path / "ws"
    init_workspace(root)
    called: list[str] = []

    monkeypatch.chdir(root)
    monkeypatch.setattr(
        "local_webpage_access.platform_support.require_supported_platform",
        lambda **kw: None,
    )
    monkeypatch.setattr(
        "local_webpage_access.lifecycle.recover_instance",
        lambda ws, cfg, reg, iid: called.append(iid),
    )

    result = CliRunner().invoke(app, ["recover", "demo"])
    assert result.exit_code == 0, result.output
    assert called == ["demo"]
    assert "已恢复实例" in result.output


def test_cli_pageviews_summary_and_detail(monkeypatch, tmp_path: Path) -> None:
    """CLI pageviews 汇总与详情：对齐 store.summary/detail。"""
    from local_webpage_access.cli import app
    from local_webpage_access.init_workspace import init_workspace
    from local_webpage_access.pageviews import PageviewStore
    from local_webpage_access.paths import Workspace
    from local_webpage_access.registry import Registry

    root = tmp_path / "ws"
    init_workspace(root)
    ws = Workspace(root)
    reg = Registry(ws.db_path)
    reg.open()
    try:
        reg.upsert_from_manifest(make_static_manifest("demo"))
    finally:
        reg.close()

    store = PageviewStore.shared_for_workspace(ws)
    conn = store._conn_or_open()
    with store._lock:
        conn.execute(
            "INSERT INTO pageviews(instance_id, day, hits, unique_ips, last_seen, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("demo", "2026-07-30", 3, 1, "2026-07-30T12:00:00+00:00", "builtin"),
        )
        conn.execute(
            "INSERT INTO pageview_ip_stats(instance_id, remote, hits, last_seen) "
            "VALUES (?, ?, ?, ?)",
            ("demo", "127.0.0.1", 3, "2026-07-30T12:00:00+00:00"),
        )
        conn.commit()

    monkeypatch.chdir(root)
    monkeypatch.setattr(
        "local_webpage_access.platform_support.require_supported_platform",
        lambda **kw: None,
    )
    monkeypatch.setattr(
        "local_webpage_access.pageviews.ingest_all",
        lambda *a, **k: None,
    )

    summary = CliRunner().invoke(app, ["pageviews"])
    assert summary.exit_code == 0, summary.output
    assert "demo" in summary.output
    assert "3" in summary.output

    detail = CliRunner().invoke(app, ["pageviews", "demo"])
    assert detail.exit_code == 0, detail.output
    assert "浏览量：demo" in detail.output
    assert "builtin" in detail.output

    missing = CliRunner().invoke(app, ["pageviews", "no-such"])
    assert missing.exit_code == 1
    assert "不存在" in missing.output
