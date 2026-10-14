"""BUG-763~765 / BUG-776~777：迁移停机安全门回归（复审 F02/F03/F04 与 CHK-380）。

故障注入手法与 design/5-checkpoints/2026-10-13-S00-S01-review/repro-migration.py、
design/5-checkpoints/2026-10-13-BUG762-774-recheck/migration-repro.py 相同：
``_terminate_pid=False`` 模拟 daemon 终止失败且 PID 仍存活；``_execute`` 返回
rc=1 模拟 Docker 查询失败；回滚侧注入 ``quiesce_workspace`` 失败结果；真实休眠
子进程 + ``read_pid_cmdline=None`` 模拟命令行不可读（归属无法确认）。
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from local_webpage_access import daemon
from local_webpage_access import docker_runtime as dr
from local_webpage_access import workspace_migrate as wm
from local_webpage_access.errors import MigrateError
from local_webpage_access.init_workspace import init_workspace
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry


@pytest.fixture()
def ws_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(
        "local_webpage_access.manager_service.maybe_start_manager",
        lambda *a, **k: None,
    )
    root = tmp_path / "old-ws"
    init_workspace(root, static_gateway="builtin")
    return root


@pytest.fixture(autouse=True)
def _guard_real_autostart_units(monkeypatch: pytest.MonkeyPatch) -> None:
    """BUG-781 安全围栏：本文件任何测试不得改写宿主机真实自启动单元。

    快照经 ``autostart.installed_services`` 读到真实已装服务时，走完迁移的
    用例会在 regenerate 阶段对 pytest 临时目录执行真实 ``autostart.repair``，
    把 ``~/Library/LaunchAgents`` 里生产单元的 ``--workspace`` 重绑到临时路径
    （2026-10-13 曾致 runtime 工作区服务错绑中断 8.8 小时）。CI/未装服务的
    机器上此缺陷不可见，故按文件级围栏兜底；测试内的显式 patch 可覆盖本围栏。
    """

    def _forbid(*args: object, **kwargs: object) -> None:
        raise AssertionError("测试不得触碰真实自启动单元（BUG-781 围栏）")

    monkeypatch.setattr("local_webpage_access.autostart.installed_services", lambda *a, **k: [])
    monkeypatch.setattr("local_webpage_access.autostart.repair", _forbid)
    monkeypatch.setattr("local_webpage_access.autostart.enable", _forbid)
    monkeypatch.setattr("local_webpage_access.autostart.install", _forbid)


def _patch_services_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("local_webpage_access.autostart.disable", lambda *a, **k: None)
    monkeypatch.setattr("local_webpage_access.manager_service.stop_manager", lambda *a, **k: True)
    monkeypatch.setattr("local_webpage_access.gateway_service.stop_gateway", lambda *a, **k: True)
    monkeypatch.setattr("local_webpage_access.daemon.is_running", lambda *a, **k: False)
    monkeypatch.setattr("local_webpage_access.manager_service.is_running", lambda *a, **k: False)
    monkeypatch.setattr(
        "local_webpage_access.gateway_service.is_gateway_running", lambda *a, **k: False
    )


def _patch_migrate_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    """move 之后阶段的副作用补丁（regenerate/restore/verify）。"""
    monkeypatch.setattr("local_webpage_access.lifecycle.start_instance", lambda *a, **k: None)
    monkeypatch.setattr(
        "local_webpage_access.autostart.repair",
        lambda *a, **k: (MagicMock(success=True), []),
    )
    monkeypatch.setattr("local_webpage_access.autostart.installed_services", lambda *a, **k: [])
    monkeypatch.setattr(
        "local_webpage_access.autostart.run_check",
        lambda *a, **k: MagicMock(overall="ok", items=[]),
    )

    class FakeGW:
        def __init__(self, *a, **k) -> None:
            pass

        def _sync_main_config(self) -> None:
            pass

    monkeypatch.setattr("local_webpage_access.static_gateway.StaticGateway", FakeGW)
    monkeypatch.setattr(wm, "verify_migrate", lambda *a, **k: (True, []))


def _inject_live_daemon(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """daemon 终止失败（_terminate_pid=False）且 PID 存活、归属本工作区。"""
    cmdline = f"python local_webpage_access.daemon {Workspace(root).root}"
    monkeypatch.setattr(daemon, "is_pid_alive", lambda *a, **k: True)
    monkeypatch.setattr(daemon, "read_pid_cmdline", lambda pid: cmdline)
    monkeypatch.setattr(daemon, "pid_cmdline_contains", lambda *a, **k: True)
    monkeypatch.setattr(daemon, "_terminate_pid", lambda *a, **k: False)


def _spawn_dummy_daemon(ws: Workspace) -> subprocess.Popen:
    """真实休眠子进程，argv 含 daemon 与工作区归属特征（复现脚本手法）。"""
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(120)",
            "local_webpage_access.daemon",
            str(ws.root),
        ]
    )
    daemon.write_state(ws, daemon.DaemonState(enabled=True, pid=child.pid))
    return child


# ---- BUG-763（F02）：daemon 终止失败、PID 仍活时迁移不得进入 MOVE -----------


def test_quiesce_records_daemon_terminate_failure(
    ws_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = Workspace(ws_root)
    daemon.write_state(ws, daemon.DaemonState(enabled=True, pid=999991))
    _patch_services_stopped(monkeypatch)
    _inject_live_daemon(monkeypatch, ws_root)

    from local_webpage_access.config import load_config

    reg = Registry(ws.db_path)
    reg.open()
    try:
        result = wm.quiesce_workspace(ws, load_config(ws), reg, wm.MigrateSnapshot())
    finally:
        reg.close()

    assert any("stop:daemon" in f for f in result.failures)
    assert any("daemon 仍在运行" in f for f in result.failures)


def test_run_migrate_aborts_when_daemon_terminate_fails(
    ws_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = Workspace(ws_root)
    daemon.write_state(ws, daemon.DaemonState(enabled=True, pid=999991))
    _patch_services_stopped(monkeypatch)
    _inject_live_daemon(monkeypatch, ws_root)
    moved: list[tuple[Path, Path]] = []
    monkeypatch.setattr(wm, "move_workspace_root", lambda o, n: moved.append((o, n)))

    result = wm.run_migrate(ws_root, tmp_path / "new-ws", yes=True)

    assert result.ok is False
    assert result.phase == wm.MigratePhase.QUIESCE.value
    assert "迁移前停机未完成" in (result.error or "")
    assert moved == []
    assert ws_root.is_dir()
    journal = wm.read_journal(Workspace(ws_root))
    assert journal is not None and journal["phase"] == wm.MigratePhase.QUIESCE.value


def test_run_migrate_resume_reblocks_when_daemon_still_alive(
    ws_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """resume 重跑 QUIESCE：停机仍失败时必须再次中止，不得带着活进程进 MOVE。"""
    ws = Workspace(ws_root)
    daemon.write_state(ws, daemon.DaemonState(enabled=True, pid=999991))
    wm.write_journal(
        ws,
        {
            "phase": "quiesce",
            "old": str(ws_root.resolve()),
            "new": str((tmp_path / "new-ws").resolve()),
            "snapshot": wm.MigrateSnapshot(captured_at="2026-10-13T00:00:00Z").to_dict(),
        },
    )
    _patch_services_stopped(monkeypatch)
    _inject_live_daemon(monkeypatch, ws_root)
    moved: list[tuple[Path, Path]] = []
    monkeypatch.setattr(wm, "move_workspace_root", lambda o, n: moved.append((o, n)))

    result = wm.run_migrate(ws_root, tmp_path / "new-ws", yes=True, resume=True)

    assert result.ok is False
    assert result.phase == wm.MigratePhase.QUIESCE.value
    assert moved == []
    assert ws_root.is_dir()


def test_run_migrate_force_quiesce_bypasses_daemon_failure(
    ws_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = Workspace(ws_root)
    daemon.write_state(ws, daemon.DaemonState(enabled=True, pid=999991))
    _patch_services_stopped(monkeypatch)
    _inject_live_daemon(monkeypatch, ws_root)
    moved: list[tuple[Path, Path]] = []
    monkeypatch.setattr(wm, "move_workspace_root", lambda o, n: moved.append((o, n)))

    result = wm.run_migrate(ws_root, tmp_path / "new-ws", yes=True, force_quiesce=True)

    assert len(moved) == 1
    assert "迁移前停机未完成" not in (result.error or "")


def test_quiesce_passes_when_daemon_terminates(
    ws_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """对照：终止成功（_terminate_pid=True）时停机步骤不再误报失败。"""
    ws = Workspace(ws_root)
    daemon.write_state(ws, daemon.DaemonState(enabled=True, pid=999991))
    _patch_services_stopped(monkeypatch)
    terminated: list[int] = []
    cmdline = f"python local_webpage_access.daemon {ws.root}"

    def _fake_terminate(pid: int, **k: object) -> bool:
        terminated.append(pid)
        return True

    monkeypatch.setattr(daemon, "_terminate_pid", _fake_terminate)
    monkeypatch.setattr(daemon, "is_pid_alive", lambda pid: pid not in terminated)
    monkeypatch.setattr(
        daemon,
        "read_pid_cmdline",
        lambda pid: None if pid in terminated else cmdline,
    )
    monkeypatch.setattr(
        daemon, "pid_cmdline_contains", lambda pid, *n: pid not in terminated
    )

    from local_webpage_access.config import load_config

    reg = Registry(ws.db_path)
    reg.open()
    try:
        result = wm.quiesce_workspace(ws, load_config(ws), reg, wm.MigrateSnapshot())
    finally:
        reg.close()

    assert result.failures == []


# ---- BUG-764（F03）：Docker 查询失败不得被当成"容器已停止" -------------------


def _make_compose_instance(ws: Workspace) -> None:
    compose = ws.app_compose_path("demo")
    compose.parent.mkdir(parents=True, exist_ok=True)
    compose.write_text("services: {}\n", encoding="utf-8")
    conn = sqlite3.connect(ws.db_path)
    try:
        conn.execute(
            "INSERT INTO instances (id, name, version, kind, runtime, serving_mode, "
            "status, desired_state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("demo", "demo", "1", "app", "docker", "container", "running", "running", "t", "t"),
        )
        conn.commit()
    finally:
        conn.close()


_FAILED_PS = SimpleNamespace(
    ok=False,
    returncode=1,
    stdout="",
    stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
    "Is the docker daemon running?",
)


def test_verify_quiesced_blocks_when_docker_query_fails(
    ws_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = Workspace(ws_root)
    _make_compose_instance(ws)
    registry = MagicMock()
    registry.list_instances.return_value = [{"id": "demo"}]
    monkeypatch.setattr(dr, "_execute", lambda *a, **k: _FAILED_PS)

    from local_webpage_access.config import load_config

    result = wm.QuiesceResult()
    wm._verify_quiesced(ws, load_config(ws), registry, result)

    assert result.failures
    assert any("无法确认已停止" in f for f in result.failures)


def test_verify_quiesced_passes_when_no_running_container(
    ws_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """对照：查询成功且无运行容器（ps -q 空输出）时不计失败。"""
    ws = Workspace(ws_root)
    _make_compose_instance(ws)
    registry = MagicMock()
    registry.list_instances.return_value = [{"id": "demo"}]
    monkeypatch.setattr(
        dr, "_execute", lambda *a, **k: SimpleNamespace(ok=True, returncode=0, stdout="", stderr="")
    )
    _patch_services_stopped(monkeypatch)

    from local_webpage_access.config import load_config

    result = wm.QuiesceResult()
    wm._verify_quiesced(ws, load_config(ws), registry, result)

    assert result.failures == []


def test_run_migrate_aborts_when_docker_query_fails(
    ws_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = Workspace(ws_root)
    _make_compose_instance(ws)
    _patch_services_stopped(monkeypatch)
    monkeypatch.setattr(wm, "_compose_down", lambda *a, **k: None)
    monkeypatch.setattr(dr, "_execute", lambda *a, **k: _FAILED_PS)
    moved: list[tuple[Path, Path]] = []
    monkeypatch.setattr(wm, "move_workspace_root", lambda o, n: moved.append((o, n)))

    result = wm.run_migrate(ws_root, tmp_path / "new-ws", yes=True)

    assert result.ok is False
    assert result.phase == wm.MigratePhase.QUIESCE.value
    assert "无法确认已停止" in (result.error or "")
    assert moved == []
    assert ws_root.is_dir()


# ---- BUG-765（F04）：回滚同样受停机安全门约束，显式 force 才允许搬移 --------


def _rollback_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    old = tmp_path / "old-ws"
    new = tmp_path / "new-ws"
    monkeypatch.setattr(
        "local_webpage_access.manager_service.maybe_start_manager",
        lambda *a, **k: None,
    )
    init_workspace(new, static_gateway="builtin")
    wm.write_journal(
        Workspace(new),
        {
            "old": str(old),
            "new": str(new),
            "phase": "complete",
            "snapshot": wm.MigrateSnapshot().to_dict(),
        },
    )
    return old, new


def test_rollback_aborts_when_quiesce_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new = _rollback_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        wm,
        "quiesce_workspace",
        lambda *a, **k: wm.QuiesceResult(failures=["实例 demo：容器仍在运行"]),
    )
    moved: list[tuple[Path, Path]] = []
    monkeypatch.setattr(wm, "move_workspace_root", lambda o, n: moved.append((o, n)))

    with pytest.raises(MigrateError, match="回滚前停机未完成"):
        wm.run_migrate(old, new, rollback=True)

    assert moved == []
    assert new.is_dir() and not old.exists()
    journal = wm.read_journal(Workspace(new))
    assert journal is not None and journal["phase"] == "complete"


def test_rollback_aborts_when_quiesce_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new = _rollback_fixture(tmp_path, monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("停机步骤崩溃")

    monkeypatch.setattr(wm, "quiesce_workspace", _boom)
    moved: list[tuple[Path, Path]] = []
    monkeypatch.setattr(wm, "move_workspace_root", lambda o, n: moved.append((o, n)))

    with pytest.raises(MigrateError, match="回滚前停机未完成"):
        wm.run_migrate(old, new, rollback=True)

    assert moved == []
    assert new.is_dir() and not old.exists()


def test_rollback_force_quiesce_bypasses_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new = _rollback_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        wm,
        "quiesce_workspace",
        lambda *a, **k: wm.QuiesceResult(failures=["实例 demo：容器仍在运行"]),
    )
    moved: list[tuple[Path, Path]] = []
    monkeypatch.setattr(wm, "move_workspace_root", lambda o, n: moved.append((o, n)))

    result = wm.run_migrate(old, new, rollback=True, force_quiesce=True)

    assert len(moved) == 1
    assert result.ok
    assert result.phase == "rollback"
    journal = wm.read_journal(Workspace(old))
    assert journal is not None and journal["phase"] == "rollback"
    assert json.loads((old / "run" / wm.JOURNAL_NAME).read_text(encoding="utf-8"))


# ---- BUG-776：命令行查询失败 = 归属无法确认，必须阻断且不得误杀/清 PID -------


def test_run_migrate_blocks_when_daemon_cmdline_unreadable(
    ws_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实休眠子进程作 daemon，仅注入 read_pid_cmdline=None：迁移阻断、
    不终止未知进程、PID 不被静默清除。"""
    ws = Workspace(ws_root)
    child = _spawn_dummy_daemon(ws)
    try:
        _patch_services_stopped(monkeypatch)
        monkeypatch.setattr(daemon, "read_pid_cmdline", lambda pid: None)
        attempted: list[int] = []
        monkeypatch.setattr(
            daemon, "_terminate_pid", lambda pid, **k: attempted.append(pid) or False
        )
        moved: list[tuple[Path, Path]] = []
        monkeypatch.setattr(wm, "move_workspace_root", lambda o, n: moved.append((o, n)))

        result = wm.run_migrate(ws_root, tmp_path / "new-ws", yes=True)

        assert result.ok is False
        assert result.phase == wm.MigratePhase.QUIESCE.value
        assert "无法确认" in (result.error or "")
        assert moved == []
        assert attempted == []
        assert child.poll() is None
        state = daemon.read_state(ws)
        assert state is not None and state.pid == child.pid
        assert ws_root.is_dir()
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_quiesce_foreign_daemon_pid_does_not_block(
    ws_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """对照：PID 存活但命令行明确属于其他进程（可读的 foreign）不阻断迁移。"""
    import os

    ws = Workspace(ws_root)
    daemon.write_state(ws, daemon.DaemonState(enabled=True, pid=os.getpid()))
    _patch_services_stopped(monkeypatch)

    from local_webpage_access.config import load_config

    reg = Registry(ws.db_path)
    reg.open()
    try:
        result = wm.quiesce_workspace(ws, load_config(ws), reg, wm.MigrateSnapshot())
    finally:
        reg.close()

    assert result.failures == []


# ---- BUG-777：MOVE 阶段 journal 的 resume 必须重新过停机安全门 ---------------


def _write_move_journal(ws: Workspace, new: Path) -> None:
    wm.write_journal(
        ws,
        {
            "old": str(ws.root),
            "new": str(new),
            "phase": "move",
            "moving": True,
            "snapshot": wm.MigrateSnapshot(captured_at="2026-10-13T00:00:00Z").to_dict(),
        },
    )


def test_resume_move_requiesces_and_blocks_when_daemon_alive(
    ws_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rename 前中断留下 phase=move journal；恢复后旧目录 daemon 复活，
    resume 必须重新停机校验，失败即阻断不搬移。"""
    ws = Workspace(ws_root)
    new = tmp_path / "new-ws"
    child = _spawn_dummy_daemon(ws)
    try:
        _write_move_journal(ws, new)
        _patch_services_stopped(monkeypatch)
        attempted: list[int] = []
        monkeypatch.setattr(
            daemon, "_terminate_pid", lambda pid, **k: attempted.append(pid) or False
        )
        moved: list[tuple[Path, Path]] = []
        monkeypatch.setattr(wm, "move_workspace_root", lambda o, n: moved.append((o, n)))

        result = wm.run_migrate(ws_root, new, yes=True, resume=True)

        assert attempted == [child.pid]
        assert moved == []
        assert result.ok is False
        assert result.phase == wm.MigratePhase.MOVE.value
        assert "停机未完成" in (result.error or "")
        assert child.poll() is None
        state = daemon.read_state(ws)
        assert state is not None and state.pid == child.pid
        assert ws_root.is_dir() and not new.exists()
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_resume_move_force_quiesce_proceeds(
    ws_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = Workspace(ws_root)
    new = tmp_path / "new-ws"
    child = _spawn_dummy_daemon(ws)
    try:
        _write_move_journal(ws, new)
        _patch_services_stopped(monkeypatch)
        _patch_migrate_tail(monkeypatch)
        monkeypatch.setattr(daemon, "_terminate_pid", lambda *a, **k: False)
        moved: list[tuple[Path, Path]] = []
        monkeypatch.setattr(wm, "move_workspace_root", lambda o, n: moved.append((o, n)))

        result = wm.run_migrate(ws_root, new, yes=True, resume=True, force_quiesce=True)

        assert len(moved) == 1
        assert "停机未完成" not in (result.error or "")
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_resume_move_after_rename_skips_requiesce(
    ws_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目录已搬到 NEW 的 MOVE journal resume：不再搬移也不再停机，继续后续阶段。"""
    ws = Workspace(ws_root)
    new = tmp_path / "new-ws"
    child = _spawn_dummy_daemon(ws)
    try:
        _write_move_journal(ws, new)
        ws_root.rename(new)
        ws_new = Workspace(new)
        _patch_services_stopped(monkeypatch)
        _patch_migrate_tail(monkeypatch)
        quiesce_calls: list[bool] = []
        original = wm.quiesce_workspace

        def _count_quiesce(*a: object, **k: object):
            quiesce_calls.append(True)
            return original(*a, **k)

        monkeypatch.setattr(wm, "quiesce_workspace", _count_quiesce)
        moved: list[tuple[Path, Path]] = []
        monkeypatch.setattr(wm, "move_workspace_root", lambda o, n: moved.append((o, n)))

        result = wm.run_migrate(ws_root, new, yes=True, resume=True)

        assert moved == []
        assert quiesce_calls == []
        assert result.ok, result.error
        assert result.phase == wm.MigratePhase.COMPLETE.value
        assert new.is_dir() and not ws_root.exists()
        journal = wm.read_journal(ws_new)
        assert journal is not None and journal["phase"] == "complete"
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_resume_move_requiesce_succeeds_and_moves(
    ws_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """对照：MOVE journal resume 时旧目录服务确已停止 → 重新校验通过后完成搬移。"""
    ws = Workspace(ws_root)
    new = tmp_path / "new-ws"
    _write_move_journal(ws, new)
    _patch_services_stopped(monkeypatch)
    _patch_migrate_tail(monkeypatch)

    result = wm.run_migrate(ws_root, new, yes=True, resume=True)

    assert result.ok, result.error
    assert result.phase == wm.MigratePhase.COMPLETE.value
    assert new.is_dir() and not ws_root.exists()
    journal = wm.read_journal(Workspace(new))
    assert journal is not None and journal["phase"] == "complete"
