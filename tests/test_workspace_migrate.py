"""DEV-089 / IMP-042：LWA 工作区迁移事务单测。"""

from __future__ import annotations

import errno
import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from local_webpage_access.errors import MigrateError
from local_webpage_access.init_workspace import init_workspace
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry
from local_webpage_access import workspace_migrate as wm


@pytest.fixture()
def ws_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """初始化最小工作区；禁止 init 拉起 manager。"""
    monkeypatch.setattr(
        "local_webpage_access.manager_service.maybe_start_manager",
        lambda *a, **k: None,
    )
    root = tmp_path / "old-ws"
    init_workspace(root, static_gateway="builtin")
    return root


# ---- Task 1: lock / journal / phases ---------------------------------------


def test_journal_atomic_roundtrip(ws_root: Path) -> None:
    ws = Workspace(ws_root)
    wm.write_journal(ws, {"phase": "preflight", "old": str(ws_root)})
    data = wm.read_journal(ws)
    assert data is not None
    assert data["phase"] == "preflight"
    assert "updated_at" in data
    assert (ws.run / wm.JOURNAL_NAME).is_file()


def test_migrate_lock_mutual_exclusion(ws_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = Workspace(ws_root)
    with wm.migrate_lock(ws) as holder:
        assert holder[0].is_file()
        # 伪造另一存活 PID
        monkeypatch.setattr(wm, "_pid_alive", lambda pid: True)
        holder[0].write_text("999999", encoding="utf-8")
        with pytest.raises(MigrateError, match="锁被占用"):
            with wm.migrate_lock(ws):
                pass
        # 恢复为本进程，允许退出清理
        holder[0].write_text(str(os.getpid()), encoding="utf-8")


def test_next_phase_and_illegal_transition() -> None:
    assert wm.next_phase("preflight") == "backup"
    assert wm.next_phase(wm.MigratePhase.VERIFY) == "complete"
    assert wm.next_phase("complete") is None
    wm.assert_phase_transition(None, "preflight")
    with pytest.raises(MigrateError, match="必须从 preflight"):
        wm.assert_phase_transition(None, "move")


# ---- Task 2: preflight -----------------------------------------------------


def test_preflight_not_workspace(tmp_path: Path) -> None:
    old = tmp_path / "nope"
    old.mkdir()
    report = wm.preflight_migrate(old, tmp_path / "new")
    assert not report.ok
    assert any(i.code == "not_workspace" for i in report.blocking)


def test_preflight_target_exists(ws_root: Path, tmp_path: Path) -> None:
    target = tmp_path / "exists"
    target.mkdir()
    report = wm.preflight_migrate(ws_root, target)
    assert not report.ok
    assert any(i.code == "target_exists" for i in report.blocking)


def test_preflight_cross_device(ws_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wm, "_same_device", lambda a, b: False)
    report = wm.preflight_migrate(ws_root, tmp_path / "new-ws")
    assert not report.ok
    assert any(i.code == "cross_device" for i in report.blocking)


def test_preflight_ok_same_device(ws_root: Path, tmp_path: Path) -> None:
    report = wm.preflight_migrate(ws_root, tmp_path / "new-ws")
    assert report.ok
    assert report.same_device is True


def test_preflight_wsl_drvfs(ws_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "local_webpage_access.platform_support.is_wsl_drvfs_path",
        lambda p: True,
    )
    report = wm.preflight_migrate(ws_root, tmp_path / "new-ws")
    assert not report.ok
    assert any(i.code == "wsl_drvfs" for i in report.blocking)


# ---- Task 3: backup + snapshot ---------------------------------------------


def test_capture_snapshot_and_backup(ws_root: Path) -> None:
    ws = Workspace(ws_root)
    reg = Registry(ws.db_path)
    reg.open()
    try:
        # 插入一条 running 意图
        reg._conn.execute(  # noqa: SLF001
            "INSERT INTO instances (id, name, version, kind, runtime, serving_mode, "
            "status, desired_state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "demo",
                "demo",
                "1",
                "static",
                "builtin",
                "static",
                "running",
                "running",
                "t",
                "t",
            ),
        )
        reg._conn.commit()  # noqa: SLF001
        snap = wm.capture_snapshot(ws, reg)
        assert "demo" in snap.restore_instance_ids
        assert snap.desired_states.get("demo") == "running"
        dest = wm.write_backup(ws, snap)
        assert (dest / "local-web.yml").is_file()
        assert (dest / "local-web.db").is_file()
        assert (dest / wm.SNAPSHOT_NAME).is_file()
    finally:
        reg.close()


# ---- Task 4: quiesce -------------------------------------------------------


def test_quiesce_keeps_daemon_processed(
    ws_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = Workspace(ws_root)
    processed = ws.run / "daemon-processed.json"
    processed.write_text('{"x": 1}\n', encoding="utf-8")
    cap = ws.run / "capability-manager.json"
    cap.write_text("{}", encoding="utf-8")

    stopped: list[str] = []
    monkeypatch.setattr(
        "local_webpage_access.lifecycle.stop_instance_op",
        lambda *a, **k: stopped.append(a[3] if len(a) > 3 else k.get("instance_id")),
    )
    monkeypatch.setattr(
        "local_webpage_access.autostart.disable",
        lambda *a, **k: MagicMock(success=True),
    )
    monkeypatch.setattr(
        "local_webpage_access.daemon.stop_daemon", lambda *a, **k: True
    )
    monkeypatch.setattr(
        "local_webpage_access.manager_service.stop_manager", lambda *a, **k: True
    )

    from local_webpage_access.config import load_config

    reg = Registry(ws.db_path)
    reg.open()
    try:
        snap = wm.MigrateSnapshot(restore_instance_ids=["demo"])
        actions = wm.quiesce_workspace(ws, load_config(ws), reg, snap)
    finally:
        reg.close()

    assert processed.is_file()
    assert "keep:daemon-processed.json" in actions
    assert "stop:demo" in actions
    assert stopped == ["demo"]


# ---- Task 5: move ----------------------------------------------------------


def test_move_workspace_same_volume(tmp_path: Path) -> None:
    old = tmp_path / "a"
    new = tmp_path / "b"
    old.mkdir()
    (old / "marker").write_text("ok", encoding="utf-8")
    wm.move_workspace_root(old, new)
    assert not old.exists()
    assert (new / "marker").read_text(encoding="utf-8") == "ok"


def test_move_cross_device_exdev(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = tmp_path / "a"
    new = tmp_path / "b"
    old.mkdir()

    def boom(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        raise OSError(errno.EXDEV, "cross-device")

    monkeypatch.setattr(os, "rename", boom)
    with pytest.raises(MigrateError, match="跨文件系统"):
        wm.move_workspace_root(old, new)


# ---- Task 6: rebind --------------------------------------------------------


def test_rewrite_manifest_prefix_and_clear_container_id(tmp_path: Path) -> None:
    old = "/old/ws"
    new = "/new/ws"
    mp = tmp_path / "local-web.json"
    mp.write_text(
        json.dumps(
            {
                "id": "demo",
                "appPath": f"{old}/apps/demo/current",
                "note": f"see {old}/apps elsewhere",  # 非路径字段不改
                "container": {
                    "containerId": "deadbeef",
                    "imageId": "img",
                    "composePath": f"{old}/apps/demo/docker-compose.yml",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    wm.rewrite_manifest_paths(mp, old, new)
    data = json.loads(mp.read_text(encoding="utf-8"))
    assert data["appPath"] == f"{new}/apps/demo/current"
    assert data["note"] == f"see {old}/apps elsewhere"
    assert data["container"]["composePath"] == f"{new}/apps/demo/docker-compose.yml"
    assert data["container"]["containerId"] is None
    assert data["container"]["imageId"] is None


def test_rewrite_registry_paths(ws_root: Path) -> None:
    ws = Workspace(ws_root)
    old = str(ws_root.resolve())
    new = str((ws_root.parent / "new-ws").resolve())
    conn = sqlite3.connect(ws.db_path)
    try:
        conn.execute(
            "INSERT INTO instances (id, name, version, kind, runtime, serving_mode, "
            "status, desired_state, app_path, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "x",
                "x",
                "1",
                "static",
                "docker",
                "container",
                "stopped",
                "stopped",
                f"{old}/apps/x/current",
                "t",
                "t",
            ),
        )
        conn.execute(
            "INSERT INTO containers (instance_id, compose_project, compose_path, "
            "container_id) VALUES (?, ?, ?, ?)",
            ("x", "x", f"{old}/apps/x/docker-compose.yml", "cid"),
        )
        conn.commit()
    finally:
        conn.close()

    wm.rewrite_registry_paths(ws.db_path, old, new)
    conn = sqlite3.connect(ws.db_path)
    try:
        app_path = conn.execute(
            "SELECT app_path FROM instances WHERE id='x'"
        ).fetchone()[0]
        compose, cid = conn.execute(
            "SELECT compose_path, container_id FROM containers WHERE instance_id='x'"
        ).fetchone()
    finally:
        conn.close()
    assert app_path.startswith(new)
    assert compose.startswith(new)
    assert cid is None


# ---- Task 7: regenerate -----------------------------------------------------


def test_regenerate_clears_capability_only(
    ws_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = Workspace(ws_root)
    processed = ws.run / "daemon-processed.json"
    processed.write_text("{}", encoding="utf-8")
    (ws.run / "capability-daemon.json").write_text("{}", encoding="utf-8")

    repaired: list[bool] = []
    synced: list[bool] = []

    monkeypatch.setattr(
        "local_webpage_access.autostart.repair",
        lambda *a, **k: (MagicMock(), repaired.append(True) or []),
    )

    class FakeGW:
        def __init__(self, *a, **k) -> None:
            pass

        def _sync_main_config(self) -> None:
            synced.append(True)

    monkeypatch.setattr(
        "local_webpage_access.static_gateway.StaticGateway", FakeGW
    )

    from local_webpage_access.config import load_config

    actions = wm.regenerate_after_move(ws, load_config(ws))
    assert processed.is_file()
    assert not (ws.run / "capability-daemon.json").exists()
    assert repaired and synced
    assert "autostart_repair" in actions
    assert "caddy_sync_main" in actions


# ---- Task 8–9: dry-run / full migrate / rollback ---------------------------


def test_run_migrate_dry_run_no_move(ws_root: Path, tmp_path: Path) -> None:
    new = tmp_path / "new-ws"
    result = wm.run_migrate(ws_root, new, dry_run=True)
    assert result.dry_run
    assert result.ok
    assert ws_root.is_dir()
    assert not new.exists()
    assert "move" in result.planned_actions


def test_run_migrate_happy_path(ws_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    new = tmp_path / "new-ws"
    old = str(ws_root.resolve())

    # 写入含旧路径的 manifest
    app = ws_root / "apps" / "demo"
    app.mkdir(parents=True)
    (app / "local-web.json").write_text(
        json.dumps(
            {
                "id": "demo",
                "appPath": f"{old}/apps/demo/current",
                "container": {"containerId": "abc", "composePath": f"{old}/c.yml"},
            }
        ),
        encoding="utf-8",
    )
    (ws_root / "run" / "daemon-processed.json").write_text("{}", encoding="utf-8")
    (ws_root / "run" / "capability-manager.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        "local_webpage_access.lifecycle.stop_instance_op", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "local_webpage_access.lifecycle.start_instance", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "local_webpage_access.autostart.disable",
        lambda *a, **k: MagicMock(success=True),
    )
    monkeypatch.setattr(
        "local_webpage_access.autostart.enable",
        lambda *a, **k: MagicMock(success=True),
    )
    monkeypatch.setattr(
        "local_webpage_access.autostart.repair",
        lambda *a, **k: (MagicMock(), ["rewrote"]),
    )
    monkeypatch.setattr(
        "local_webpage_access.autostart.installed_services", lambda *a, **k: []
    )
    monkeypatch.setattr(
        "local_webpage_access.daemon.stop_daemon", lambda *a, **k: True
    )
    monkeypatch.setattr(
        "local_webpage_access.manager_service.stop_manager", lambda *a, **k: True
    )

    class FakeGW:
        def __init__(self, *a, **k) -> None:
            pass

        def _sync_main_config(self) -> None:
            pass

    monkeypatch.setattr(
        "local_webpage_access.static_gateway.StaticGateway", FakeGW
    )
    monkeypatch.setattr(
        "local_webpage_access.autostart.run_check",
        lambda *a, **k: MagicMock(overall="ok", items=[]),
    )

    result = wm.run_migrate(ws_root, new, yes=True)
    assert result.ok, result.error
    assert result.phase == "complete"
    assert not Path(old).exists()
    assert new.is_dir()
    assert (new / "run" / "daemon-processed.json").is_file()
    assert not (new / "run" / "capability-manager.json").exists()
    data = json.loads((new / "apps" / "demo" / "local-web.json").read_text())
    assert data["appPath"].startswith(str(new.resolve()))
    assert data["container"]["containerId"] is None
    journal = json.loads((new / "run" / wm.JOURNAL_NAME).read_text())
    assert journal["phase"] == "complete"


def test_run_migrate_rollback(ws_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    new = tmp_path / "new-ws"
    monkeypatch.setattr(
        "local_webpage_access.lifecycle.stop_instance_op", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "local_webpage_access.lifecycle.start_instance", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "local_webpage_access.autostart.disable",
        lambda *a, **k: MagicMock(success=True),
    )
    monkeypatch.setattr(
        "local_webpage_access.autostart.enable",
        lambda *a, **k: MagicMock(success=True),
    )
    monkeypatch.setattr(
        "local_webpage_access.autostart.repair",
        lambda *a, **k: (MagicMock(), []),
    )
    monkeypatch.setattr(
        "local_webpage_access.autostart.installed_services", lambda *a, **k: []
    )
    monkeypatch.setattr(
        "local_webpage_access.daemon.stop_daemon", lambda *a, **k: True
    )
    monkeypatch.setattr(
        "local_webpage_access.manager_service.stop_manager", lambda *a, **k: True
    )

    class FakeGW:
        def __init__(self, *a, **k) -> None:
            pass

        def _sync_main_config(self) -> None:
            pass

    monkeypatch.setattr(
        "local_webpage_access.static_gateway.StaticGateway", FakeGW
    )
    monkeypatch.setattr(
        "local_webpage_access.autostart.run_check",
        lambda *a, **k: MagicMock(overall="ok", items=[]),
    )

    old = ws_root.resolve()
    result = wm.run_migrate(ws_root, new, yes=True)
    assert result.ok
    assert new.is_dir() and not old.exists()

    rb = wm.run_migrate(old, new, rollback=True)
    assert rb.ok
    assert old.is_dir()
    assert not new.exists()


def test_verify_detects_old_path_in_manifest(ws_root: Path) -> None:
    ws = Workspace(ws_root)
    old = str(ws_root.resolve())
    app = ws.apps / "demo"
    app.mkdir(parents=True)
    (app / "local-web.json").write_text(
        json.dumps({"appPath": f"{old}/apps/demo/current"}),
        encoding="utf-8",
    )
    ok, notes = wm.verify_migrate(ws, old, old, wm.MigrateSnapshot())
    assert not ok
    assert any("仍含旧路径" in n for n in notes)
