"""daemon 与 inbox watcher 测试（WBS-21）。"""

from __future__ import annotations

import contextlib
import subprocess
import threading
import time
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from local_webpage_access import daemon as daemon_mod
from local_webpage_access.config import Config, PortPool
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry


# ---- fixtures --------------------------------------------------------------


@pytest.fixture()
def workspace(workspace_root: Path) -> Workspace:
    ws = Workspace(workspace_root)
    ws.ensure_workspace_dirs()
    # 写入最小配置让 require_workspace / load_config 工作
    from local_webpage_access.config import example_config_text

    if not ws.config_path.is_file():
        # BUG-121：示例配置默认 caddy；测试工作区改为 builtin
        text = example_config_text().replace(
            "staticGateway: caddy", "staticGateway: builtin"
        )
        ws.config_path.write_text(text, encoding="utf-8")
    return ws


@pytest.fixture()
def registry(workspace_root: Path) -> Registry:
    workspace_root.joinpath("registry").mkdir(parents=True, exist_ok=True)
    reg = Registry(workspace_root / "registry" / "local-web.db")
    reg.open()
    yield reg
    reg.close()


@pytest.fixture()
def config(workspace_root: Path) -> Config:
    # BUG-121：强制 builtin，避免 process_zip/start 打到本机 :2019
    return Config(staticGateway="builtin", portPool=PortPool(start=21000, end=21050))


# ---- 状态持久化（WBS-21.04）------------------------------------------------


def test_read_state_returns_none_when_absent(workspace: Workspace) -> None:
    assert daemon_mod.read_state(workspace) is None


def test_write_then_read_state_roundtrip(workspace: Workspace) -> None:
    state = daemon_mod.DaemonState(
        enabled=True, pid=12345, started_at="2026-07-05T10:00:00", poll_interval=3.0
    )
    daemon_mod.write_state(workspace, state)
    got = daemon_mod.read_state(workspace)
    assert got is not None
    assert got.enabled is True
    assert got.pid == 12345
    assert got.poll_interval == 3.0
    assert got.started_at == "2026-07-05T10:00:00"


def test_read_state_tolerates_corrupt_json(workspace: Workspace) -> None:
    daemon_mod.state_path(workspace).write_text("not json", encoding="utf-8")
    assert daemon_mod.read_state(workspace) is None


def test_clear_state_removes_file(workspace: Workspace) -> None:
    daemon_mod.write_state(workspace, daemon_mod.DaemonState(enabled=True, pid=1))
    assert daemon_mod.state_path(workspace).is_file()
    daemon_mod.clear_state(workspace)
    assert not daemon_mod.state_path(workspace).exists()


# ---- inbox 扫描（WBS-21.05）------------------------------------------------


def test_scan_inbox_lists_zips_sorted(workspace: Workspace) -> None:
    inbox = workspace.inbox
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "b.zip").write_bytes(b"")
    (inbox / "a.zip").write_bytes(b"")
    (inbox / "not_zip.txt").write_text("x", encoding="utf-8")
    found = daemon_mod.scan_inbox(workspace)
    assert [p.name for p in found] == ["a.zip", "b.zip"]


def test_scan_inbox_empty_when_missing(workspace: Workspace) -> None:
    # inbox 存在但为空
    workspace.inbox.mkdir(parents=True, exist_ok=True)
    assert daemon_mod.scan_inbox(workspace) == []


# ---- 文件稳定性（WBS-21.06）------------------------------------------------


def test_is_file_stable_false_on_first_sight(workspace: Workspace) -> None:
    p = workspace.inbox / "x.zip"
    p.write_bytes(b"hello")
    stable, fp = daemon_mod.is_file_stable(p, None, now_ts=time.time())
    assert stable is False
    assert fp.size == 5


def test_is_file_stable_true_when_unchanged_across_window(workspace: Workspace) -> None:
    p = workspace.inbox / "x.zip"
    p.write_bytes(b"hello")
    ts = time.time()
    # 第一次观测
    _, fp = daemon_mod.is_file_stable(p, None, now_ts=ts)
    # 第二次：mtime/size 不变，且时间窗已过
    stable, _ = daemon_mod.is_file_stable(
        p, fp, now_ts=ts + daemon_mod.DEFAULT_STABLE_SECONDS + 0.1
    )
    assert stable is True


def test_is_file_stable_false_when_size_changes(workspace: Workspace) -> None:
    p = workspace.inbox / "x.zip"
    p.write_bytes(b"hello")
    _, fp = daemon_mod.is_file_stable(p, None, now_ts=time.time())
    p.write_bytes(b"hello world")  # 变大
    stable, _ = daemon_mod.is_file_stable(p, fp, now_ts=time.time() + 10)
    assert stable is False


# ---- 单实例锁（WBS-21.11）--------------------------------------------------


def test_daemon_lock_is_exclusive(workspace: Workspace) -> None:
    with daemon_mod.daemon_lock(workspace):
        # 第二次获取（同进程但锁文件已存在）应失败
        with pytest.raises(OSError):
            with daemon_mod.daemon_lock(workspace):
                pass


def test_daemon_lock_released_after_context(workspace: Workspace) -> None:
    path = daemon_mod.lock_path(workspace)
    with daemon_mod.daemon_lock(workspace):
        assert path.exists()
        inode = path.stat().st_ino
    # BUG-213：释放后保留锁文件，但可再次获取同一 inode
    assert path.exists()
    assert path.stat().st_ino == inode
    with daemon_mod.daemon_lock(workspace):
        assert path.stat().st_ino == inode


def test_daemon_lock_failed_acquire_keeps_live_lock(workspace: Workspace) -> None:
    """BUG-173：获取失败（他人持活锁）时 finally 不得删除其锁文件。

    旧行为：finally 无条件 unlink，第二个 watcher 的获取失败会删掉活跃 watcher
    的锁，is_running 假阴性、后续再次获取成功 → 重复 watcher 并发扫 inbox。
    """
    import os
    import time as time_mod

    from local_webpage_access.file_lock import (
        ensure_lockable,
        release_exclusive,
        try_acquire_exclusive,
        write_lock_payload,
    )

    path = daemon_mod.lock_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR)
    ensure_lockable(fd)
    try_acquire_exclusive(fd)
    write_lock_payload(fd, f"{os.getpid()}\n{time_mod.time():.3f}\n".encode())
    try:
        assert path.exists()
        with pytest.raises(OSError):
            with daemon_mod.daemon_lock(workspace):
                pass
        # 关键：获取失败后锁文件必须仍在（BUG-173）
        assert path.exists()
    finally:
        release_exclusive(fd)
        os.close(fd)


# ---- 锁心跳超时回收（BUG-030）---------------------------------------------


def _write_lock(workspace: Workspace, pid: int, heartbeat_ts: float) -> None:
    """直接写一个给定 PID 与心跳时间戳的锁文件（绕过 daemon_lock）。"""
    path = daemon_mod.lock_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n{heartbeat_ts:.3f}\n", encoding="utf-8")


def test_lock_is_stale_when_heartbeat_expired() -> None:
    """BUG-030：PID 存活但心跳超时 → 锁判为陈旧。"""
    import os

    path = Path(".bug030-stale.lock")
    try:
        path.write_text(f"{os.getpid()}\n{time.time() - 9999:.3f}\n", encoding="utf-8")
        assert daemon_mod._lock_is_stale(path, stale_after=60.0) is True
    finally:
        path.unlink(missing_ok=True)


def test_lock_is_not_stale_with_fresh_heartbeat() -> None:
    """BUG-030：PID 存活且心跳新鲜 → 锁不陈旧。"""
    import os

    path = Path(".bug030-fresh.lock")
    try:
        path.write_text(f"{os.getpid()}\n{time.time():.3f}\n", encoding="utf-8")
        assert daemon_mod._lock_is_stale(path, stale_after=60.0) is False
    finally:
        path.unlink(missing_ok=True)


def test_lock_is_stale_single_line_uses_mtime() -> None:
    """单行锁文件（仅 PID）用 mtime 兜底：过期则陈旧。"""
    import os

    path = Path(".bug-single-line.lock")
    try:
        path.write_text(f"{os.getpid()}\n", encoding="utf-8")
        old = time.time() - 9999
        os.utime(path, (old, old))
        assert daemon_mod._lock_is_stale(path, stale_after=60.0) is True
        # 新鲜 mtime → 不陈旧
        now = time.time()
        os.utime(path, (now, now))
        assert daemon_mod._lock_is_stale(path, stale_after=60.0) is False
    finally:
        path.unlink(missing_ok=True)


def test_is_running_false_when_heartbeat_expired(workspace: Workspace) -> None:
    """BUG-030：watcher 卡死（PID 存活但心跳超时）→ is_running 返回 False。"""
    import os

    _write_lock(workspace, os.getpid(), time.time() - 9999)
    daemon_mod.write_state(
        workspace,
        daemon_mod.DaemonState(
            enabled=True, pid=os.getpid(), started_at="now", poll_interval=5.0
        ),
    )
    assert daemon_mod.is_running(workspace) is False


def test_daemon_lock_reclaims_stale_heartbeat(workspace: Workspace) -> None:
    """BUG-030：持有进程卡死时 daemon_lock 应能回收陈旧心跳锁。"""
    import os

    # 模拟一个卡死的 watcher：本进程 PID（存活）但心跳早已超时
    # 无人持文件锁时（仅残留内容）可直接获取
    _write_lock(workspace, os.getpid(), time.time() - 9999)
    with daemon_mod.daemon_lock(workspace):
        assert daemon_mod.lock_path(workspace).exists()


def test_touch_lock_heartbeat_updates_timestamp(workspace: Workspace) -> None:
    """BUG-030：touch_lock_heartbeat 刷新心跳并保留 PID。"""
    import os

    old_ts = time.time() - 9999
    _write_lock(workspace, os.getpid(), old_ts)
    daemon_mod.touch_lock_heartbeat(workspace)
    content = daemon_mod.lock_path(workspace).read_text(encoding="utf-8").splitlines()
    assert int(content[0]) == os.getpid()
    assert float(content[1]) > old_ts


# ---- process_zip 自动启动决策（WBS-21.07/08/09）---------------------------


def _make_zip(zip_path: Path, files: dict[str, str]) -> None:
    """生成一个含若干文件的 zip。"""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)


def test_process_zip_pending_for_unknown_project(
    workspace: Workspace, config: Config, registry: Registry
) -> None:
    """无法识别的项目导入后保持 pending，不被错误启动。"""
    zip_path = workspace.inbox / "unknown.zip"
    _make_zip(zip_path, {"readme.txt": "nothing to recognize here"})
    summary = daemon_mod.process_zip(workspace, config, registry, zip_path)
    assert summary["action"] == "pending"
    assert summary["instance_id"] is not None
    row = registry.get_instance(summary["instance_id"])
    assert row is not None
    assert row["status"] == "pending"


def test_process_zip_starts_determinable_static(
    workspace: Workspace, config: Config, registry: Registry
) -> None:
    """可确定的纯静态项目应被自动启动（tiny/small）。"""
    zip_path = workspace.inbox / "static.zip"
    _make_zip(zip_path, {"index.html": "<h1>hi</h1>"})
    summary = daemon_mod.process_zip(workspace, config, registry, zip_path)
    assert summary["action"] == "started"
    assert summary["instance_id"] is not None
    row = registry.get_instance(summary["instance_id"])
    assert row is not None
    assert row["status"] == "running"
    # BUG：泄漏兜底——process_zip 自动 start 的内置静态服务（http.server 子进程）
    # 在测试结束未停会成为孤儿，跨用例累积会占满端口池、使全量测试连跑即红。
    from local_webpage_access.lifecycle import stop_instance_op

    stop_instance_op(workspace, config, registry, summary["instance_id"])


def test_process_zip_does_not_auto_start_heavy(
    workspace: Workspace, config: Config, registry: Registry
) -> None:
    """heavy/medium 项目导入后不自动启动，等待人工确认。"""
    zip_path = workspace.inbox / "app.zip"
    _make_zip(zip_path, {"index.html": "<h1>x</h1>"})
    # 通过猴子补丁把识别后的 resourceProfile 改成 heavy，验证不自动启动分支
    real_process = daemon_mod.process_zip
    try:
        started: list[str] = []

        def fake_start(ws, cfg, reg, iid):
            started.append(iid)
            return None

        with patch("local_webpage_access.lifecycle.start_instance", side_effect=fake_start):
            # 让 importer 走静态识别，然后 manifest 改 heavy：直接对 process_zip
            # 注入一个把 profile 改 heavy 的 process_fn 不现实；这里改成检测 pending
            # 分支：用一个 index.html 项目但人为 patch resourceProfile
            import local_webpage_access.importer as importer_mod

            orig_build = importer_mod.build_manifest_from_detection

            def patched(*args, **kwargs):
                m = orig_build(*args, **kwargs)
                from local_webpage_access.models import ResourceProfile

                m.resourceProfile = ResourceProfile.HEAVY
                return m

            with patch.object(importer_mod, "build_manifest_from_detection", patched):
                summary = real_process(workspace, config, registry, zip_path)
        assert summary["action"] == "pending"
        assert "heavy" in (summary["note"] or "")
        assert started == []  # 未自动启动
    finally:
        pass


def test_process_zip_failure_does_not_raise(
    workspace: Workspace, config: Config, registry: Registry
) -> None:
    """损坏的 zip 应返回 failed 摘要，而非冒泡打断 watcher。"""
    zip_path = workspace.inbox / "broken.zip"
    zip_path.write_bytes(b"not a zip")
    summary = daemon_mod.process_zip(workspace, config, registry, zip_path)
    assert summary["action"] == "failed"
    assert summary["note"]


def test_process_zip_transient_failure_retries_not_conflict(
    workspace: Workspace, config: Config, registry: Registry, monkeypatch
) -> None:
    """BUG-187：claim 后的瞬时失败（IO/SQLite locked）应 action=failed（下轮重试），
    而非被误判为 slug 冲突（永久归档 + 给已清理实例记 import_conflict 孤儿事件）。"""
    import local_webpage_access.importer as importer_mod

    zip_path = workspace.inbox / "static.zip"
    _make_zip(zip_path, {"index.html": "<h1>hi</h1>"})

    def boom(*_a, **_k):
        raise OSError("模拟瞬时错误（IO/SQLite locked）")

    monkeypatch.setattr(importer_mod, "safe_extract", boom)
    summary = daemon_mod.process_zip(workspace, config, registry, zip_path)
    assert summary["action"] == "failed"
    assert summary["instance_id"] is None  # 未被误判为冲突


def test_process_zip_autostart_failure_archives_and_defers(
    workspace: Workspace, config: Config, registry: Registry, monkeypatch
) -> None:
    """BUG-188：导入成功但自动启动失败时以终态归档 zip（不重导致冲突），
    并置期望运行交自愈 reconcile 重试，而非停在 stopped 被孤儿。"""
    zip_path = workspace.inbox / "static.zip"
    _make_zip(zip_path, {"index.html": "<h1>hi</h1>"})

    def boom(*_a, **_k):
        raise RuntimeError("启动模拟失败")

    monkeypatch.setattr("local_webpage_access.lifecycle.start_instance", boom)
    summary = daemon_mod.process_zip(workspace, config, registry, zip_path)
    assert summary["action"] == "imported"  # 终态 → watcher 归档 zip（不重导）
    iid = summary["instance_id"]
    assert iid is not None
    row = registry.get_instance(iid)
    assert row is not None
    assert row["desired_state"] == "running"  # 供自愈重试


# ---- processed 集合 --------------------------------------------------------


def test_processed_set_roundtrip(workspace: Workspace) -> None:
    assert daemon_mod.load_processed_set(workspace) == set()
    daemon_mod.save_processed_set(workspace, {"/a/b.zip", "/c/d.zip"})
    got = daemon_mod.load_processed_set(workspace)
    assert got == {"/a/b.zip", "/c/d.zip"}


def test_processed_set_tolerates_corrupt(workspace: Workspace) -> None:
    daemon_mod._archive_processed_marker(workspace).write_text("garbage", encoding="utf-8")
    assert daemon_mod.load_processed_set(workspace) == set()


def test_processed_key_changes_when_same_path_is_overwritten(
    workspace: Workspace,
) -> None:
    """BUG-038：同名新 zip 覆盖后应得到新的去重 key。"""
    zip_path = workspace.inbox / "same.zip"
    _make_zip(zip_path, {"index.html": "old"})
    first = daemon_mod.processed_key(zip_path)
    time.sleep(0.001)
    _make_zip(zip_path, {"index.html": "new content"})
    second = daemon_mod.processed_key(zip_path)
    assert first != second


# ---- watcher 主循环（WBS-21.05/06/07/10）-----------------------------------


def test_run_watcher_in_thread_processes_and_exits_on_stop(
    workspace: Workspace, config: Config, registry: Registry
) -> None:
    zip_path = workspace.inbox / "static.zip"
    _make_zip(zip_path, {"index.html": "<h1>hi</h1>"})
    daemon_mod.write_state(
        workspace, daemon_mod.DaemonState(enabled=True, pid=0, poll_interval=0.01)
    )

    processed: list[Path] = []
    done = threading.Event()

    def fake_process(ws, cfg, reg, p):
        processed.append(p)
        return {"action": "started"}

    stop = threading.Event()

    def runner():
        daemon_mod.run_watcher(
            workspace,
            config,
            registry,
            stop_event=stop,
            poll_interval=0.01,
            stable_seconds=0.0,
            process_fn=fake_process,
        )
        done.set()

    t = threading.Thread(target=runner)
    t.start()
    # 等待处理完成（最多 2s）
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not processed:
        time.sleep(0.02)
    assert processed, "watcher 未处理任何 zip"
    # 置 stop，watcher 应在 poll_interval 内退出
    stop.set()
    assert done.wait(2.0), "watcher 未在 stop 后退出"
    t.join(timeout=2.0)

    # processed 集合已落盘
    assert str(zip_path) in daemon_mod.load_processed_set(workspace)


def test_run_watcher_does_not_mark_failed_zip_processed(
    workspace: Workspace, config: Config, registry: Registry
) -> None:
    """BUG-034：处理失败的 zip 不应写入 processed，下一轮可自动重试。"""
    zip_path = workspace.inbox / "bad.zip"
    _make_zip(zip_path, {"index.html": "<h1>x</h1>"})
    daemon_mod.write_state(
        workspace, daemon_mod.DaemonState(enabled=True, pid=0, poll_interval=0.01)
    )

    stop = threading.Event()
    calls: list[Path] = []

    def fake_process(ws, cfg, reg, p):
        calls.append(p)
        stop.set()
        return {"action": "failed"}

    daemon_mod.run_watcher(
        workspace,
        config,
        registry,
        stop_event=stop,
        poll_interval=0.01,
        stable_seconds=0.0,
        process_fn=fake_process,
    )
    processed = daemon_mod.load_processed_set(workspace)
    assert calls == [zip_path]
    assert daemon_mod.processed_key(zip_path) not in processed
    assert str(zip_path) not in processed


def test_run_watcher_processes_overwritten_same_path_zip(
    workspace: Workspace, config: Config, registry: Registry
) -> None:
    """BUG-038：路径相同但指纹不同的 zip 应重新处理。"""
    zip_path = workspace.inbox / "same.zip"
    _make_zip(zip_path, {"index.html": "old"})
    old_key = daemon_mod.processed_key(zip_path)
    daemon_mod.save_processed_set(workspace, {old_key, str(zip_path)})
    time.sleep(0.001)
    _make_zip(zip_path, {"index.html": "new content is longer"})
    daemon_mod.write_state(
        workspace, daemon_mod.DaemonState(enabled=True, pid=0, poll_interval=0.01)
    )

    stop = threading.Event()
    processed_paths: list[Path] = []

    def fake_process(ws, cfg, reg, p):
        processed_paths.append(p)
        stop.set()
        return {"action": "started"}

    daemon_mod.run_watcher(
        workspace,
        config,
        registry,
        stop_event=stop,
        poll_interval=0.01,
        stable_seconds=0.0,
        process_fn=fake_process,
    )
    assert processed_paths == [zip_path]
    assert daemon_mod.processed_key(zip_path) in daemon_mod.load_processed_set(workspace)


def test_run_watcher_exits_when_disabled(
    workspace: Workspace, config: Config, registry: Registry
) -> None:
    """状态文件 enabled=False 时 watcher 立即退出，不处理任何 zip。"""
    daemon_mod.write_state(
        workspace, daemon_mod.DaemonState(enabled=False, pid=0, poll_interval=0.01)
    )
    zip_path = workspace.inbox / "x.zip"
    _make_zip(zip_path, {"index.html": "<h1>x</h1>"})

    processed: list[Path] = []

    def fake_process(ws, cfg, reg, p):
        processed.append(p)

    stop = threading.Event()
    daemon_mod.run_watcher(
        workspace,
        config,
        registry,
        stop_event=stop,
        poll_interval=0.01,
        stable_seconds=0.0,
        process_fn=fake_process,
    )
    assert processed == []


def test_run_watcher_skips_unstable_file(
    workspace: Workspace, config: Config, registry: Registry
) -> None:
    """未通过稳定窗口的文件本轮不处理，下一轮再判断。"""
    zip_path = workspace.inbox / "growing.zip"
    _make_zip(zip_path, {"index.html": "<h1>x</h1>"})
    daemon_mod.write_state(
        workspace, daemon_mod.DaemonState(enabled=True, pid=0, poll_interval=0.01)
    )

    call_count = {"n": 0}

    def fake_process(ws, cfg, reg, p):
        call_count["n"] += 1
        return {"action": "started"}

    stop = threading.Event()
    done = threading.Event()

    def runner():
        daemon_mod.run_watcher(
            workspace,
            config,
            registry,
            stop_event=stop,
            poll_interval=0.01,
            stable_seconds=999.0,  # 永不视为稳定
            process_fn=fake_process,
        )
        done.set()

    t = threading.Thread(target=runner)
    t.start()
    time.sleep(0.2)  # 让 watcher 跑几轮
    stop.set()
    assert done.wait(2.0)
    t.join(timeout=2.0)
    assert call_count["n"] == 0  # 从未处理（一直不稳定）


# ---- is_running / status ---------------------------------------------------


def test_is_running_false_when_no_state(workspace: Workspace) -> None:
    assert daemon_mod.is_running(workspace) is False


def test_is_running_false_when_pid_dead(workspace: Workspace) -> None:
    daemon_mod.write_state(
        workspace, daemon_mod.DaemonState(enabled=True, pid=999999999)
    )
    assert daemon_mod.is_running(workspace) is False


def test_daemon_status_reports_disabled(workspace: Workspace) -> None:
    info = daemon_mod.daemon_status(workspace)
    assert info["running"] is False
    assert info["enabled"] is False


def test_start_daemon_serializes_concurrent_start(
    workspace: Workspace, config: Config, monkeypatch
) -> None:
    """BUG-033：并发 daemon on 不应 spawn 多个 watcher。"""
    pid = 43210
    spawn_calls: list[float] = []
    real_is_pid_alive = daemon_mod.is_pid_alive

    def fake_is_pid_alive(candidate: int) -> bool:
        if candidate == pid:
            return True
        return real_is_pid_alive(candidate)

    def fake_spawn(ws, poll):
        spawn_calls.append(time.time())
        daemon_mod.lock_path(ws).write_text(f"{pid}\n{time.time():.3f}\n", encoding="utf-8")
        time.sleep(0.1)
        return pid

    monkeypatch.setattr(daemon_mod, "is_pid_alive", fake_is_pid_alive)
    monkeypatch.setattr(daemon_mod, "_spawn_watcher", fake_spawn)

    results: list[int] = []
    errors: list[BaseException] = []

    def worker():
        try:
            results.append(daemon_mod.start_daemon(workspace, config, poll_interval=0.01))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join(timeout=2)
    t2.join(timeout=2)

    assert errors == []
    assert results == [pid, pid]
    assert len(spawn_calls) == 1


def test_start_daemon_rolls_back_state_when_child_exits(
    workspace: Workspace, config: Config, monkeypatch
) -> None:
    """BUG-036：watcher 子进程立即退出后 state 不应保持 enabled=True。"""
    pid = 54321
    real_is_pid_alive = daemon_mod.is_pid_alive

    def fake_is_pid_alive(candidate: int) -> bool:
        if candidate == pid:
            return False
        return real_is_pid_alive(candidate)

    monkeypatch.setattr(daemon_mod, "is_pid_alive", fake_is_pid_alive)
    monkeypatch.setattr(daemon_mod, "_spawn_watcher", lambda ws, poll: pid)

    from local_webpage_access.errors import LifecycleError

    with pytest.raises(LifecycleError):
        daemon_mod.start_daemon(workspace, config, poll_interval=0.01)
    state = daemon_mod.read_state(workspace)
    assert state is not None
    assert state.enabled is False
    assert state.pid == pid


# ---- stop_daemon clears enabled -------------------------------------------


def test_stop_daemon_sets_disabled(workspace: Workspace) -> None:
    daemon_mod.write_state(
        workspace,
        daemon_mod.DaemonState(enabled=True, pid=999999999, started_at="now"),
    )
    assert daemon_mod.stop_daemon(workspace) is True
    state = daemon_mod.read_state(workspace)
    assert state is not None
    assert state.enabled is False


def test_stop_daemon_noop_when_never_started(workspace: Workspace) -> None:
    assert daemon_mod.stop_daemon(workspace) is True


def test_stop_daemon_refuses_foreign_reused_pid(
    workspace: Workspace, monkeypatch
) -> None:
    """BUG-125：daemon PID 已复用时只清状态，不终止无关进程。"""
    daemon_mod.write_state(
        workspace,
        daemon_mod.DaemonState(enabled=True, pid=4242, started_at="now"),
    )
    monkeypatch.setattr(daemon_mod, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(daemon_mod, "pid_cmdline_contains", lambda pid, *needles: False)
    killed: list[int] = []
    monkeypatch.setattr(daemon_mod.os, "kill", lambda pid, sig: killed.append(pid))

    assert daemon_mod.stop_daemon(workspace) is True
    assert killed == []
    assert daemon_mod.read_state(workspace).enabled is False


def test_stop_daemon_keeps_lock_when_termination_fails(
    workspace: Workspace, monkeypatch
) -> None:
    """BUG-192：stop_daemon 终止失败（pid 仍存活）时不得删锁文件，否则其他 watcher
    会误判无主并发启动，叠加 stuck/重复进程产生无锁 watcher。"""
    import os

    daemon_mod.write_state(
        workspace,
        daemon_mod.DaemonState(enabled=True, pid=os.getpid(), started_at="now"),
    )
    # 持锁进程 = 本进程（身份匹配 → 走 _terminate_pid 分支）
    lock = daemon_mod.lock_path(workspace)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(f"{os.getpid()}\n{time.time():.3f}\n", encoding="utf-8")
    monkeypatch.setattr(daemon_mod, "pid_cmdline_contains", lambda *a, **k: True)
    monkeypatch.setattr(daemon_mod, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(daemon_mod, "_terminate_pid", lambda pid, **k: False)  # 终止失败

    stopped = daemon_mod.stop_daemon(workspace)
    assert stopped is False
    # 关键：终止失败时锁文件必须保留
    assert lock.exists()


def test_read_pid_cmdline_windows_powershell(monkeypatch) -> None:
    """BUG-177：Windows 上 read_pid_cmdline 用 PowerShell 读 CommandLine，不再恒 None。

    旧实现落到 ``ps`` 分支恒返回 None（Windows 无 ps），致 pid_cmdline_contains
    恒 False、停止 builtin 静态服务时误判身份不匹配而 orphan http.server。
    """
    monkeypatch.setattr(daemon_mod.sys, "platform", "win32")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout="C:\\py\\python.exe -m http.server --directory C:\\apps\\x\\public",
            stderr="",
        )

    monkeypatch.setattr(daemon_mod.subprocess, "run", fake_run)
    cmdline = daemon_mod.read_pid_cmdline(1234)
    assert cmdline is not None
    assert "http.server" in cmdline
    assert captured["cmd"][0] == "powershell"
    # 身份校验在 Windows 上现可命中（不再恒 False）
    assert daemon_mod.pid_cmdline_contains(1234, "http.server", "C:\\apps\\x\\public")


# ---- IMP-011：inbox 防污染（on_conflict=error + processed 归档）-------------


def test_process_zip_conflict_no_rename(
    workspace: Workspace, config: Config, registry: Registry
) -> None:
    """IMP-011：slug 已占用时 process_zip 应 action=conflict，不建 -2，记事件。"""
    from local_webpage_access.importer import Importer

    zip_path = workspace.inbox / "static.zip"
    _make_zip(zip_path, {"index.html": "<h1>hi</h1>"})
    # 先直接导入占住 slug "static"（不走 process_zip 的自动启动，不拉起服务）
    Importer(workspace, config, registry).import_zip(str(zip_path))
    assert registry.get_instance("static") is not None

    # 再次经 daemon process_zip 导入同名 → 冲突
    summary = daemon_mod.process_zip(workspace, config, registry, zip_path)
    assert summary["action"] == "conflict"
    assert summary["instance_id"] == "static"
    # 不应自动改名建冗余实例
    assert registry.get_instance("static-2") is None
    # 已存在的实例上应记录 import_conflict 事件（提示 --update）
    events = registry.list_events("static")
    assert any(e["event_type"] == "import_conflict" for e in events)


def test_archive_processed_zip_moves_with_timestamp(workspace: Workspace) -> None:
    """IMP-011：归档 helper 把 zip 移入 inbox/processed/ 并加时间戳。"""
    src = workspace.inbox / "app.zip"
    _make_zip(src, {"index.html": "x"})
    dest = daemon_mod._archive_processed_zip(workspace, src)
    assert dest is not None
    assert not src.exists()  # 已移走
    assert Path(dest).is_file()
    assert Path(dest).parent == workspace.inbox_processed
    assert "app-" in Path(dest).name  # 带时间戳前缀


def test_archive_processed_zip_handles_missing_source(workspace: Workspace) -> None:
    assert daemon_mod._archive_processed_zip(workspace, workspace.inbox / "nope.zip") is None


def test_scan_inbox_ignores_processed_subdir(workspace: Workspace) -> None:
    """IMP-011：inbox/processed/ 下的 zip 不被 scan_inbox 扫到。"""
    workspace.inbox.mkdir(parents=True, exist_ok=True)
    (workspace.inbox / "top.zip").write_bytes(b"")
    workspace.inbox_processed.mkdir(parents=True, exist_ok=True)
    (workspace.inbox_processed / "done.zip").write_bytes(b"")
    names = [p.name for p in daemon_mod.scan_inbox(workspace)]
    assert names == ["top.zip"]


def test_run_watcher_archives_terminal_zip(
    workspace: Workspace, config: Config, registry: Registry
) -> None:
    """IMP-011：watcher 对终态（非 failed）zip 移入 inbox/processed/。"""
    zip_path = workspace.inbox / "static.zip"
    _make_zip(zip_path, {"index.html": "x"})
    stop = threading.Event()

    def fake_process(ws, cfg, reg, zp):
        stop.set()  # 处理完即令 watcher 退出
        return {"action": "started", "instance_id": "static", "note": None, "zip": str(zp)}

    daemon_mod.run_watcher(
        workspace,
        config,
        registry,
        stop_event=stop,
        poll_interval=0.01,
        stable_seconds=0.0,
        process_fn=fake_process,
    )
    assert not zip_path.exists()  # 已从 inbox 移走
    archived = list(workspace.inbox_processed.glob("*.zip"))
    assert len(archived) == 1


def test_run_watcher_keeps_failed_zip_in_inbox(
    workspace: Workspace, config: Config, registry: Registry
) -> None:
    """IMP-011：failed 的 zip 保留在 inbox 等下轮重试，不归档。"""
    zip_path = workspace.inbox / "broken.zip"
    _make_zip(zip_path, {"index.html": "x"})
    stop = threading.Event()

    def fake_process(ws, cfg, reg, zp):
        stop.set()
        return {"action": "failed", "instance_id": None, "note": "boom", "zip": str(zp)}

    daemon_mod.run_watcher(
        workspace,
        config,
        registry,
        stop_event=stop,
        poll_interval=0.01,
        stable_seconds=0.0,
        process_fn=fake_process,
    )
    assert zip_path.exists()  # 仍在 inbox
    assert list(workspace.inbox_processed.glob("*.zip")) == []  # 未归档


# ---- DEV-042：reconcile 自愈 -------------------------------------------------


def _seed_instance(
    registry: Registry,
    workspace: Workspace,
    iid: str,
    *,
    runtime: str = "shared-static",
    desired: str = "running",
    status: str = "stopped",
) -> None:
    """在 registry 落一个实例（含 static_sites 行），用于 reconcile 测试。"""
    from local_webpage_access.models import (
        ContainerConfig,
        DesiredState,
        InstanceManifest,
        Kind,
        ResourceProfile,
        Runtime,
        ServingMode,
        StaticConfig,
        Status,
    )

    workspace.ensure_app_dirs(iid)
    if runtime == "shared-static":
        m = InstanceManifest(
            id=iid,
            name=iid,
            version="1",
            kind=Kind.STATIC,
            runtime=Runtime.SHARED_STATIC,
            servingMode=ServingMode.SHARED_STATIC,
            resourceProfile=ResourceProfile.TINY,
            desiredState=DesiredState(desired),
            status=Status(status),
            static=StaticConfig(hostPort=21100, enabled=True),
        )
    else:
        m = InstanceManifest(
            id=iid,
            name=iid,
            version="1",
            kind=Kind.PYTHON,
            runtime=Runtime.DOCKER_COMPOSE,
            servingMode=ServingMode.CONTAINER,
            resourceProfile=ResourceProfile.SMALL,
            desiredState=DesiredState(desired),
            status=Status(status),
            container=ContainerConfig(
                projectName=f"lwa-{iid}",
                internalPort=8000,
                composePath="docker/compose.yaml",
                dockerfilePath="docker/Dockerfile",
            ),
        )
    m.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(m)


def test_reconcile_restarts_desired_running_offline(
    workspace: Workspace, config: Config, registry: Registry
) -> None:
    """DEV-042：desired=running ∧ status≠running 的实例被逐一恢复。"""
    _seed_instance(registry, workspace, "a", desired="running", status="stopped")
    _seed_instance(registry, workspace, "b", desired="running", status="failed")
    _seed_instance(registry, workspace, "c", desired="stopped", status="stopped")
    restarted: list[str] = []
    daemon_mod.reconcile(
        workspace,
        config,
        registry,
        restarter=lambda ws, cfg, reg, iid: restarted.append(iid),
    )
    assert sorted(restarted) == ["a", "b"]
    # 成功恢复写 reconcile 事件
    assert registry.list_events("a", limit=1)[0]["event_type"] == "reconcile"


def test_reconcile_skips_in_progress_and_truly_running(
    workspace: Workspace, config: Config, registry: Registry, monkeypatch
) -> None:
    """pending/queued/有活跃构建的 building 跳过；running 经 observe 确认也跳过。"""
    from local_webpage_access.models import Status
    from local_webpage_access.logging import now_iso

    for iid, st in [
        ("p", "pending"),
        ("q", "queued"),
        ("bld", "building"),
        ("r", "running"),
    ]:
        _seed_instance(registry, workspace, iid, status=st)
    # 活跃构建中的 building 仍应跳过（BUG-166 边界）
    registry.add_build("bld", status="running", started_at=now_iso())
    # "r" 经 observe 确认真实运行中 → 不恢复
    monkeypatch.setattr(
        "local_webpage_access.lifecycle.observe_status",
        lambda ws, cfg, reg, iid: Status.RUNNING,
    )
    restarted: list[str] = []
    daemon_mod.reconcile(
        workspace,
        config,
        registry,
        restarter=lambda ws, cfg, reg, iid: restarted.append(iid),
    )
    assert restarted == []


def test_reconcile_recovers_orphan_building_without_active_build(
    workspace: Workspace, config: Config, registry: Registry, monkeypatch
) -> None:
    """BUG-166：无活跃 builds 的 building 不得跳过；observe 掉线后应恢复。"""
    from local_webpage_access.models import Status

    _seed_instance(registry, workspace, "demo", status="building")
    monkeypatch.setattr(
        "local_webpage_access.lifecycle.observe_status",
        lambda ws, cfg, reg, iid: Status.STOPPED,
    )
    restarted: list[str] = []
    daemon_mod.reconcile(
        workspace,
        config,
        registry,
        restarter=lambda ws, cfg, reg, iid: restarted.append(iid),
    )
    assert restarted == ["demo"]


def test_reconcile_recovers_stale_running(
    workspace: Workspace, config: Config, registry: Registry, monkeypatch
) -> None:
    """BUG-079：registry 标 running 但 observe 发现实际掉线 → 仍恢复。"""
    from local_webpage_access.models import Status

    _seed_instance(registry, workspace, "demo", status="running")
    monkeypatch.setattr(
        "local_webpage_access.lifecycle.observe_status",
        lambda ws, cfg, reg, iid: Status.STOPPED,
    )
    restarted: list[str] = []
    daemon_mod.reconcile(
        workspace,
        config,
        registry,
        restarter=lambda ws, cfg, reg, iid: restarted.append(iid),
    )
    assert restarted == ["demo"]


def test_reconcile_continues_on_individual_failure(
    workspace: Workspace, config: Config, registry: Registry
) -> None:
    """单实例恢复失败不中断整体。"""

    def flaky(ws, cfg, reg, iid):
        if iid == "boom":
            raise RuntimeError("simulated")
        restarted.append(iid)

    restarted: list[str] = []
    _seed_instance(registry, workspace, "boom", status="stopped")
    _seed_instance(registry, workspace, "ok", status="stopped")
    daemon_mod.reconcile(workspace, config, registry, restarter=flaky)
    assert restarted == ["ok"]


def test_reconcile_skips_caddy_static_when_gateway_disabled(
    workspace: Workspace, config: Config, registry: Registry, monkeypatch
) -> None:
    """Caddy 后端 + gateway.json enabled=false（用户 lwa gateway off）→ 跳过 caddy 静态。"""
    # builtin 实例应仍被恢复；caddy 静态被跳过
    _seed_instance(registry, workspace, "caddy-static", status="stopped")
    _seed_instance(
        registry, workspace, "container", runtime="docker-compose", status="stopped"
    )

    class _FakeGW:
        def __init__(self, *a, **kw):
            pass

        def detect_backend(self):
            return "caddy"

    monkeypatch.setattr(
        "local_webpage_access.static_gateway.StaticGateway", _FakeGW
    )
    # gateway.json enabled=false
    from local_webpage_access.gateway_service import GatewayState, write_state

    write_state(workspace, GatewayState(enabled=False))

    restarted: list[str] = []
    daemon_mod.reconcile(
        workspace,
        config,
        registry,
        restarter=lambda ws, cfg, reg, iid: restarted.append(iid),
    )
    assert restarted == ["container"]  # caddy-static 被跳过


def test_run_watcher_invokes_supervise_periodically(
    workspace: Workspace, config: Config, registry: Registry
) -> None:
    """DEV-042：watcher 主循环按 supervise_interval 周期调用 supervise 回调。"""
    import time as _time

    calls: list[float] = []
    clock = [_time.monotonic()]
    # 让 clock 随调用推进，确保越过 supervise_interval 触发一次后停止
    base = [_time.monotonic()]

    def fake_clock():
        base[0] += 100  # 每次读 clock 推进 100s，必然越过 supervise_interval
        return base[0]

    stop = threading.Event()

    def fake_supervise(ws, cfg, reg):
        calls.append(fake_clock())
        stop.set()

    daemon_mod.run_watcher(
        workspace,
        config,
        registry,
        stop_event=stop,
        poll_interval=0.01,
        stable_seconds=0.0,
        process_fn=lambda *a: {"action": "skipped"},
        clock=fake_clock,
        sleep=lambda *_a: None,
        supervise=fake_supervise,
        supervise_interval=60.0,
    )
    assert len(calls) >= 1


def test_run_watcher_heartbeat_thread_refreshes_during_long_round(
    workspace: Workspace, config: Config, registry: Registry
) -> None:
    """BUG-190：后台心跳线程在长轮次（单轮超 LOCK_HEARTBEAT_TIMEOUT）期间持续刷新，
    不仅每轮起始刷新一次——否则长构建期间锁被 _lock_is_stale 误判 stale、产生
    重复 watcher（对照 instance_lock 的独立心跳线程 BUG-046）。"""
    daemon_mod.write_state(
        workspace, daemon_mod.DaemonState(enabled=True, pid=0, poll_interval=0.01)
    )

    calls: list[float] = []

    def heartbeat() -> None:
        calls.append(time.monotonic())

    stop = threading.Event()
    # 主循环第一轮会阻塞在 stop_event.wait(poll_interval=5)；期间仅后台线程刷新。
    timer = threading.Timer(0.4, stop.set)
    timer.start()
    try:
        daemon_mod.run_watcher(
            workspace,
            config,
            registry,
            stop_event=stop,
            poll_interval=5.0,  # 长阻塞，靠 stop 退出
            heartbeat=heartbeat,
            heartbeat_interval=0.03,  # 30ms → 0.4s 内后台刷新 ~10 次
        )
    finally:
        timer.cancel()
        timer.join(timeout=1.0)

    # 若无后台线程，整个阻塞期间只有轮首那 1 次；有线程则 ≥3 次
    assert len(calls) >= 3, f"后台心跳未持续刷新（仅 {len(calls)} 次）"


def test_attach_daemon_log_handler_writes_daemon_log(workspace: Workspace) -> None:
    """BUG-189：attach_daemon_log_handler 给 root logger 追加 logs/daemon.log 的
    FileHandler，detached watcher 子进程（stdout/stderr=DEVNULL）日志不再丢失。"""
    import logging

    root = logging.getLogger()
    before = set(id(h) for h in root.handlers)
    try:
        daemon_mod.attach_daemon_log_handler(workspace)
        added = [h for h in root.handlers if id(h) not in before]
        assert any(
            isinstance(h, logging.FileHandler)
            and str(workspace.logs / daemon_mod.LOG_FILENAME) == h.baseFilename
            for h in added
        ), "未为 logs/daemon.log 追加 FileHandler"

        logging.getLogger("lwa.bug189.test").warning("BUG-189 marker line")
        for h in added:
            h.flush()
        content = (workspace.logs / daemon_mod.LOG_FILENAME).read_text(encoding="utf-8")
        assert "BUG-189 marker line" in content
    finally:
        # 清理：移除本测试追加的 handler，避免污染后续用例
        for h in list(root.handlers):
            if id(h) not in before:
                root.removeHandler(h)
                with contextlib.suppress(Exception):
                    h.close()


