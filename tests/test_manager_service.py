"""管理页后台服务测试。"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from local_webpage_access.config import Config, PortPool
from local_webpage_access.errors import (
    LifecycleError,
    ManagerLockHeldError,
)
from local_webpage_access.manager_service import (
    MANAGER_LOCK_FAILURE_EXIT_CODE,
    ManagerState,
    manager_start_lock,
    manager_instance_lock,
    instance_lock_path,
    start_lock_path,
    health_matches_workspace,
    health_ok,
    is_running,
    log_file_path,
    maybe_start_manager,
    read_manager_log,
    read_state,
    start_manager,
    stop_manager,
    write_state,
    _lock_held_exit_code,
    _spawn_manager,
)
from local_webpage_access.paths import Workspace


def test_manager_enabled_default_true() -> None:
    assert Config().managerEnabled is True


def test_maybe_start_skips_when_disabled(workspace: Workspace) -> None:
    workspace.ensure_workspace_dirs()
    cfg = Config(managerEnabled=False, portPool=PortPool(start=21000, end=21050))
    assert maybe_start_manager(workspace, cfg) is None


def test_start_manager_rejects_when_disabled(workspace: Workspace) -> None:
    workspace.ensure_workspace_dirs()
    cfg = Config(managerEnabled=False, portPool=PortPool(start=21000, end=21050))
    with pytest.raises(LifecycleError, match="managerEnabled=false"):
        start_manager(workspace, cfg)


def test_is_running_uses_state_and_health(workspace: Workspace) -> None:
    workspace.ensure_workspace_dirs()
    cfg = Config(portPool=PortPool(start=21000, end=21050))
    write_state(
        workspace,
        ManagerState(enabled=True, pid=999999, host="0.0.0.0", port=cfg.managerPort),
    )
    with patch("local_webpage_access.manager_service.is_pid_alive", return_value=True):
        with patch(
            "local_webpage_access.manager_service.health_matches_workspace",
            return_value=True,
        ):
            assert is_running(workspace, cfg) is True
        with patch(
            "local_webpage_access.manager_service.health_matches_workspace",
            return_value=False,
        ):
            assert is_running(workspace, cfg) is False


def test_is_running_without_pid_when_health_matches(workspace: Workspace) -> None:
    workspace.ensure_workspace_dirs()
    cfg = Config(portPool=PortPool(start=21000, end=21050))
    write_state(
        workspace,
        ManagerState(enabled=True, pid=None, host="0.0.0.0", port=cfg.managerPort),
    )
    with patch(
        "local_webpage_access.manager_service.health_matches_workspace",
        return_value=True,
    ):
        assert is_running(workspace, cfg) is True
    with patch(
        "local_webpage_access.manager_service.health_matches_workspace",
        return_value=False,
    ):
        assert is_running(workspace, cfg) is False


def test_start_manager_rejects_foreign_workspace_on_port(workspace: Workspace) -> None:
    workspace.ensure_workspace_dirs()
    cfg = Config(portPool=PortPool(start=21000, end=21050))
    with patch("local_webpage_access.manager_service.is_running", return_value=False):
        with patch(
            "local_webpage_access.manager_service.health_matches_workspace",
            return_value=False,
        ):
            with patch("local_webpage_access.manager_service.health_ok", return_value=True):
                with pytest.raises(LifecycleError, match="已被其他工作区占用"):
                    start_manager(workspace, cfg)


def test_start_manager_recovers_state_for_own_workspace(workspace: Workspace) -> None:
    workspace.ensure_workspace_dirs()
    cfg = Config(portPool=PortPool(start=21000, end=21050))
    with patch("local_webpage_access.manager_service.is_running", return_value=False):
        with patch(
            "local_webpage_access.manager_service.health_matches_workspace",
            return_value=True,
        ):
            assert start_manager(workspace, cfg) == 0
    state = read_state(workspace)
    assert state is not None
    assert state.enabled is True
    assert state.pid is None


def test_start_manager_recovery_stores_discovered_pid(workspace: Workspace) -> None:
    """BUG-126：恢复本工作区健康服务时记录监听 PID，避免后续无法停止。"""
    workspace.ensure_workspace_dirs()
    cfg = Config(portPool=PortPool(start=21000, end=21050))
    with (
        patch("local_webpage_access.manager_service.is_running", return_value=False),
        patch(
            "local_webpage_access.manager_service.health_matches_workspace",
            return_value=True,
        ),
        patch(
            "local_webpage_access.manager_service.find_listening_pid",
            return_value=4242,
        ),
        patch(
            "local_webpage_access.manager_service.pid_cmdline_contains",
            return_value=True,
        ),
    ):
        assert start_manager(workspace, cfg) == 4242
    state = read_state(workspace)
    assert state is not None
    assert state.pid == 4242


def test_stop_manager_clears_enabled(workspace: Workspace) -> None:
    workspace.ensure_workspace_dirs()
    write_state(
        workspace,
        ManagerState(enabled=True, pid=999999, host="0.0.0.0", port=17800),
    )
    with patch("local_webpage_access.manager_service._terminate_pid", return_value=True):
        assert stop_manager(workspace) is True
    state = read_state(workspace)
    assert state is not None
    assert state.enabled is False


def test_foreign_manager_hint_when_port_healthy_but_local_stopped(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BUG-456：本工作区未运行但端口仍健康 → 提示可能是其他工作区占用。"""
    from local_webpage_access.manager_service import foreign_manager_hint

    workspace.ensure_workspace_dirs()
    config = Config(managerHost="127.0.0.1", managerPort=17800)
    monkeypatch.setattr(
        "local_webpage_access.manager_service.health_ok",
        lambda host, port: True,
    )
    monkeypatch.setattr(
        "local_webpage_access.manager_service.is_running",
        lambda ws, cfg: False,
    )
    tip = foreign_manager_hint(workspace, config)
    assert tip is not None
    assert "其他工作区" in tip or "managerPort" in tip


def test_foreign_manager_hint_none_when_port_free(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from local_webpage_access.manager_service import foreign_manager_hint

    workspace.ensure_workspace_dirs()
    config = Config(managerHost="127.0.0.1", managerPort=17800)
    monkeypatch.setattr(
        "local_webpage_access.manager_service.health_ok",
        lambda host, port: False,
    )
    assert foreign_manager_hint(workspace, config) is None


def test_existing_foreign_manager_hint_when_other_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IMP-053：init 新目录前，若 :17800 已是另一工作区，须提示复用而非再建一套。"""
    from local_webpage_access.manager_service import existing_foreign_manager_hint

    other = (tmp_path / "existing-runtime").resolve()
    candidate = (tmp_path / "new-lwa-workspace").resolve()
    monkeypatch.setattr(
        "local_webpage_access.manager_service._fetch_health",
        lambda host, port, timeout=1.0: {
            "ok": True,
            "workspaceRoot": str(other),
        },
    )
    tip = existing_foreign_manager_hint(candidate)
    assert tip is not None
    assert str(other) in tip
    assert "一个工作区" in tip or "不要再" in tip


def test_existing_foreign_manager_hint_none_for_same_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from local_webpage_access.manager_service import existing_foreign_manager_hint

    root = (tmp_path / "runtime").resolve()
    monkeypatch.setattr(
        "local_webpage_access.manager_service._fetch_health",
        lambda host, port, timeout=1.0: {
            "ok": True,
            "workspaceRoot": str(root),
        },
    )
    assert existing_foreign_manager_hint(root) is None


def test_existing_foreign_manager_hint_none_when_no_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from local_webpage_access.manager_service import existing_foreign_manager_hint

    monkeypatch.setattr(
        "local_webpage_access.manager_service._fetch_health",
        lambda host, port, timeout=1.0: None,
    )
    assert existing_foreign_manager_hint(tmp_path / "anywhere") is None


def test_stop_manager_keeps_enabled_when_terminate_fails(workspace: Workspace) -> None:
    workspace.ensure_workspace_dirs()
    write_state(
        workspace,
        ManagerState(enabled=True, pid=999999, host="0.0.0.0", port=17800),
    )
    with patch("local_webpage_access.manager_service._terminate_pid", return_value=False):
        assert stop_manager(workspace) is False
    state = read_state(workspace)
    assert state is not None
    assert state.enabled is True


def test_stop_manager_without_pid_discovers_and_terminates(
    workspace: Workspace,
) -> None:
    """BUG-126：pid=None 仍应查监听进程并停止，不能直接假成功。"""
    workspace.ensure_workspace_dirs()
    write_state(
        workspace,
        ManagerState(enabled=True, pid=None, host="0.0.0.0", port=17800),
    )
    with (
        patch(
            "local_webpage_access.manager_service.find_listening_pid",
            return_value=4242,
        ),
        patch(
            "local_webpage_access.manager_service.pid_cmdline_contains",
            return_value=True,
        ),
        patch(
            "local_webpage_access.manager_service._terminate_pid",
            return_value=True,
        ) as terminate,
    ):
        assert stop_manager(workspace) is True
    terminate.assert_called_once()
    assert read_state(workspace).enabled is False


def test_stop_manager_refuses_foreign_reused_pid(workspace: Workspace) -> None:
    """BUG-125：PID 已复用为无关进程时清状态但绝不发送信号。"""
    workspace.ensure_workspace_dirs()
    write_state(
        workspace,
        ManagerState(enabled=True, pid=4242, host="0.0.0.0", port=17800),
    )
    with (
        patch("local_webpage_access.manager_service.is_pid_alive", return_value=True),
        patch(
            "local_webpage_access.manager_service.pid_cmdline_contains",
            return_value=False,
        ),
        patch("local_webpage_access.manager_service.os.kill") as kill,
    ):
        assert stop_manager(workspace) is True
    kill.assert_not_called()
    assert read_state(workspace).enabled is False


def test_manager_start_lock_recovers_stale_file(workspace: Workspace) -> None:
    """BUG-130/642：死 PID 或超龄残留的启动锁不再阻塞——内核锁空闲即可获取。"""
    workspace.ensure_workspace_dirs()
    lock = start_lock_path(workspace)
    lock.write_text("999999\n", encoding="utf-8")
    old = time.time() - 120
    os.utime(lock, (old, old))

    with patch("local_webpage_access.manager_service.is_pid_alive", return_value=False):
        with manager_start_lock(workspace, timeout=0.1):
            assert lock.is_file()
    # BUG-642：锁文件永不 unlink（保持同一 inode 供竞争者复用），且释放后可再取
    assert lock.exists()
    with manager_start_lock(workspace, timeout=0.1):
        pass


def test_manager_instance_lock_exclusive_when_live_holder(
    workspace: Workspace,
) -> None:
    """BUG-193/642：真实持锁期间第二次获取抛 ManagerLockHeldError，不覆盖持有记录。"""
    workspace.ensure_workspace_dirs()
    lock = instance_lock_path(workspace)
    with manager_instance_lock(workspace):
        holder_pid = lock.read_text(encoding="utf-8").splitlines()[0].strip()
        with pytest.raises(ManagerLockHeldError) as ei:
            with manager_instance_lock(workspace):
                pass
        assert ei.value.context["holder_pid"] == int(holder_pid)
        # 失败获取不得破坏持有者的锁文件与 PID 记录（BUG-173 同款）
        assert lock.read_text(encoding="utf-8").splitlines()[0].strip() == holder_pid
    assert lock.exists()  # 退出只解锁，不删除锁文件


def test_manager_instance_lock_does_not_steal_old_live_holder(
    workspace: Workspace,
) -> None:
    """BUG-309/642：文件年龄不参与判定——内核锁仍被持有时不可抢占。"""
    workspace.ensure_workspace_dirs()
    lock = instance_lock_path(workspace)
    with manager_instance_lock(workspace):
        old = time.time() - 3600
        os.utime(lock, (old, old))
        with pytest.raises(ManagerLockHeldError):
            with manager_instance_lock(workspace):
                pass
        assert lock.read_text(encoding="utf-8").splitlines()[0].strip() == str(os.getpid())


def test_manager_instance_lock_reclaims_stale(workspace: Workspace) -> None:
    """BUG-193/641：崩溃残留（死 PID）不再拒启——内核锁空闲即可获取并覆盖记录。"""
    workspace.ensure_workspace_dirs()
    lock = instance_lock_path(workspace)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("999999\n", encoding="utf-8")  # 死 PID
    with patch("local_webpage_access.manager_service.is_pid_alive", return_value=False):
        with manager_instance_lock(workspace):
            assert lock.read_text(encoding="utf-8").splitlines()[0].strip() == str(os.getpid())
    assert lock.exists()  # 永不 unlink


def test_manager_instance_lock_released_on_exit(workspace: Workspace) -> None:
    """BUG-193/642：退出只解锁不删文件；随后可正常再取锁。"""
    workspace.ensure_workspace_dirs()
    lock = instance_lock_path(workspace)
    assert not lock.exists()
    with manager_instance_lock(workspace):
        assert lock.exists()
        assert lock.read_text(encoding="utf-8").splitlines()[0].strip() == str(os.getpid())
    assert lock.exists()  # 文件保留（内核锁已释放）
    with manager_instance_lock(workspace):  # 再取成功
        pass


# ---- BUG-641/642：manager 锁内核化（flock）----------------------------------


def test_manager_instance_lock_ignores_unrelated_live_pid(
    workspace: Workspace,
) -> None:
    """BUG-641：锁文件 PID 被无关进程复用时不拒启、不发信号，仅覆盖记录。"""
    workspace.ensure_workspace_dirs()
    lock = instance_lock_path(workspace)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("4242\n", encoding="utf-8")
    with (
        patch("local_webpage_access.manager_service.is_pid_alive", return_value=True),
        patch(
            "local_webpage_access.manager_service._manager_pid_matches",
            return_value=False,
        ),
        patch("local_webpage_access.manager_service.os.kill") as kill,
    ):
        with manager_instance_lock(workspace):
            assert lock.read_text(encoding="utf-8").splitlines()[0].strip() == str(os.getpid())
    kill.assert_not_called()


def test_manager_instance_lock_refuses_legacy_holder_without_terminating(
    workspace: Workspace,
) -> None:
    """BUG-645：旧协议记录 + 存活旧版 manager → 锁层拒绝进入且不终止、不覆盖记录。"""
    workspace.ensure_workspace_dirs()
    lock = instance_lock_path(workspace)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("4242\n", encoding="utf-8")
    with (
        patch("local_webpage_access.manager_service.is_pid_alive", return_value=True),
        patch(
            "local_webpage_access.manager_service._manager_pid_matches",
            return_value=True,
        ),
        patch(
            "local_webpage_access.manager_service._terminate_pid",
            return_value=True,
        ) as terminate,
    ):
        with pytest.raises(ManagerLockHeldError) as ei:
            with manager_instance_lock(workspace):
                pass
    terminate.assert_not_called()  # 锁层绝不终止；旧版停止交协调层
    assert ei.value.context.get("legacy") is True
    assert ei.value.context["holder_pid"] == 4242
    assert "lwa update" in ei.value.message  # 恢复指引交协调层
    assert lock.read_text(encoding="utf-8").strip() == "4242"  # 未覆盖他人记录


def test_manager_instance_lock_never_terminates_on_swapped_path(
    workspace: Workspace,
) -> None:
    """BUG-645：锁住已被替换路径的旧 inode 时，不得按路径中的 PID 终止新持有者。"""
    workspace.ensure_workspace_dirs()
    from local_webpage_access import file_lock as fl

    lock = instance_lock_path(workspace)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("0\n", encoding="utf-8")
    original_acquire = fl.try_acquire_exclusive
    swapped: list[int] = []
    others: list[int] = []

    def replace_after_open(fd):
        if swapped:
            original_acquire(fd)
            return
        swapped.append(fd)
        # A 已打开旧 inode；旧版退出 unlink；B 在新 inode 上真正持有 flock 并写 PID。
        lock.unlink()
        other = os.open(str(lock), os.O_CREAT | os.O_RDWR)
        original_acquire(other)
        fl.write_lock_payload(other, b"4242\n")
        others.append(other)
        original_acquire(fd)  # A 对已无路径的旧 inode 加锁也成功

    with (
        patch(
            "local_webpage_access.file_lock.try_acquire_exclusive",
            side_effect=replace_after_open,
        ),
        patch("local_webpage_access.manager_service.is_pid_alive", return_value=True),
        patch(
            "local_webpage_access.manager_service._manager_pid_matches",
            return_value=True,
        ),
        patch(
            "local_webpage_access.manager_service._terminate_pid",
            return_value=False,
        ) as terminate,
    ):
        # inode 核验在解读记录之前：放弃旧 inode 后重试，撞上 B 的内核锁
        with pytest.raises(ManagerLockHeldError) as ei:
            with manager_instance_lock(workspace):
                pass
    terminate.assert_not_called()  # 绝不尝试终止新持有者 B（pid=4242）
    assert ei.value.context["holder_pid"] == 4242
    for fd in others:
        with contextlib.suppress(OSError):
            os.close(fd)


@pytest.mark.parametrize("content", ["", "\n", "garbage\n", "not-a-pid"])
def test_manager_instance_lock_refuses_fresh_degraded_lock_file(
    workspace: Workspace, content: str
) -> None:
    """BUG-651：新建的空/损坏记录可能是旧版「已建文件、未写 PID」的创建窗口
    （旧版不持内核锁，flock 拦不住它）——限时观察后拒入且**绝不写入**，不再
    直接接管：提前写入会让旧版随后覆写记录并在退出时 unlink 本版持锁路径，
    两版并存且互斥失效。"""
    workspace.ensure_workspace_dirs()
    lock = instance_lock_path(workspace)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(content, encoding="utf-8")
    with patch(
        "local_webpage_access.manager_service._EMPTY_LOCK_WATCH_SECONDS", 0.15
    ):
        with pytest.raises(ManagerLockHeldError) as ei:
            with manager_instance_lock(workspace):
                pass
    assert ei.value.context.get("legacy") is True
    # 拒入时不得写入：记录保持原样，不干扰可能的旧版创建者
    assert lock.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("content", ["", "\n", "garbage\n", "not-a-pid"])
def test_manager_instance_lock_claims_aged_degraded_lock_file(
    workspace: Workspace, content: str
) -> None:
    """BUG-651 自愈：空/损坏记录年龄达到旧版陈旧阈值（60s 同口径）后视为
    创建者崩溃残留，就地认领并升级为新协议记录（死亡进程不会再 unlink）。"""
    workspace.ensure_workspace_dirs()
    lock = instance_lock_path(workspace)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(content, encoding="utf-8")
    old = time.time() - 120
    os.utime(lock, (old, old))
    with patch(
        "local_webpage_access.manager_service._EMPTY_LOCK_WATCH_SECONDS", 0.15
    ):
        with manager_instance_lock(workspace):
            lines = lock.read_text(encoding="utf-8").splitlines()
            assert lines[0].strip() == str(os.getpid())
            assert lines[1].strip() == "flock"
    assert lock.exists()


def test_manager_start_lock_never_steals_live_holder_by_age(
    workspace: Workspace,
) -> None:
    """BUG-642：持有者仍存活时，锁文件超过原 60 秒阈值也不得被抢占。"""
    workspace.ensure_workspace_dirs()
    lock = start_lock_path(workspace)
    with manager_start_lock(workspace, timeout=0.1):
        old = time.time() - 3600
        os.utime(lock, (old, old))
        with pytest.raises(ManagerLockHeldError):
            with manager_start_lock(workspace, timeout=0.05):
                pass
        assert lock.read_text(encoding="utf-8").splitlines()[0].strip() == str(os.getpid())
    assert lock.exists()


def test_manager_start_lock_refuses_while_legacy_holder_alive(
    workspace: Workspace,
) -> None:
    """BUG-646 / BUG-648：旧协议记录 + 存活持有者 → 旧版临界区未结束前新版拒绝进入。

    BUG-648：超时拒入时**不得**把记录升级为新协议——否则下次调用跳过旧版
    检查，与仍在临界区的旧版并存；旧版随后 unlink 还会让其他新版经新 inode
    进入，破坏新版互斥。
    """
    workspace.ensure_workspace_dirs()
    lock = start_lock_path(workspace)
    lock.parent.mkdir(parents=True, exist_ok=True)
    # 与 V0.8.13 相同的旧协议：单行 PID、无 flock 标记、不持内核锁
    lock.write_text(f"{os.getpid()}\n", encoding="utf-8")
    entered: list[str] = []
    with pytest.raises(ManagerLockHeldError) as ei:
        with manager_start_lock(workspace, timeout=0.05):
            entered.append("first")
    assert not entered
    assert ei.value.context.get("legacy") is True
    assert ei.value.context.get("holder_pid") == os.getpid()
    # BUG-648：超时后仍保持旧协议记录，不得写入 flock 标记
    assert lock.read_text(encoding="utf-8").strip() == str(os.getpid())
    # 旧持有者仍存活时，再次尝试仍须拒入（不得因误升级而成功）
    with pytest.raises(ManagerLockHeldError) as ei2:
        with manager_start_lock(workspace, timeout=0.05):
            entered.append("second")
    assert not entered
    assert ei2.value.context.get("legacy") is True


def test_manager_start_lock_waits_for_kernel_holder_until_timeout(
    workspace: Workspace,
) -> None:
    """BUG-649：新版内核锁冲突时须在 timeout 内重试，不得首次 BlockingIOError 即失败。

    对比复现：持锁约 0.2s、等待预算 1s——父提交等待后成功进入，误改后立即
    ManagerLockHeldError，破坏并发 ``lwa manager on`` 的串行幂等路径。
    """
    import threading

    workspace.ensure_workspace_dirs()
    entered: list[str] = []
    holder_done = threading.Event()
    holder_started = threading.Event()

    def hold_briefly() -> None:
        with manager_start_lock(workspace, timeout=0.1):
            holder_started.set()
            entered.append("holder")
            time.sleep(0.2)
        holder_done.set()

    t = threading.Thread(target=hold_briefly)
    t.start()
    assert holder_started.wait(timeout=2.0)
    # 持锁方仍活着；等待预算 1s > 剩余持锁时间 → 应成功进入
    with manager_start_lock(workspace, timeout=1.0):
        entered.append("waiter")
    t.join(timeout=2.0)
    assert entered == ["holder", "waiter"]
    assert holder_done.is_set()


def test_manager_start_lock_takes_over_after_legacy_exits(
    workspace: Workspace,
) -> None:
    """BUG-646：旧版持有者在等待期内退出（finally unlink）→ 新版重试后正常进入。"""
    workspace.ensure_workspace_dirs()
    lock = start_lock_path(workspace)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(f"{os.getpid()}\n", encoding="utf-8")
    calls = {"n": 0}

    def flip_alive(pid):
        if calls["n"] == 0:
            calls["n"] += 1
            return True
        # 旧版退出：finally unlink 路径
        with contextlib.suppress(OSError):
            lock.unlink()
        return False

    with patch(
        "local_webpage_access.manager_service.is_pid_alive", side_effect=flip_alive
    ):
        with manager_start_lock(workspace, timeout=2.0):
            assert lock.exists()
            assert lock.read_text(encoding="utf-8").splitlines()[1].strip() == "flock"


def test_manager_start_lock_enters_when_legacy_released_but_process_alive(
    workspace: Workspace,
) -> None:
    """BUG-650：旧版临界区的结束以「锁路径被其 finally unlink」为准，不是进程退出。

    旧 CLI 释放锁后仍会存活（等待健康检查、打印输出等）——只盯 PID 存活会把
    已释放的锁误报为占用直到超时。复现：持有者进程始终存活，首次探测后其
    finally 已 unlink 锁路径；新版应立即换新文件进入，而非等到超时拒入。
    """
    workspace.ensure_workspace_dirs()
    lock = start_lock_path(workspace)
    lock.parent.mkdir(parents=True, exist_ok=True)
    # 与 V0.8.13 相同的旧协议记录：单行 PID、无 flock 标记、不持内核锁
    lock.write_text(f"{os.getpid()}\n", encoding="utf-8")
    calls = {"n": 0}

    def alive_but_released(pid):
        # 旧版持有者：进程始终存活；首次存活探测时其 finally unlink 锁路径
        calls["n"] += 1
        if calls["n"] == 1:
            with contextlib.suppress(OSError):
                lock.unlink()
        return True

    entered: list[str] = []
    started = time.monotonic()
    with patch(
        "local_webpage_access.manager_service.is_pid_alive",
        side_effect=alive_but_released,
    ):
        with manager_start_lock(workspace, timeout=2.0):
            entered.append("in")
    assert entered == ["in"]
    # 未耗尽等待预算（旧实现会等满 2s 后 ManagerLockHeldError）
    assert time.monotonic() - started < 1.0
    lines = lock.read_text(encoding="utf-8").splitlines()
    assert lines[0].strip() == str(os.getpid())
    assert lines[1].strip() == "flock"


def test_manager_start_lock_refuses_on_fresh_empty_record(
    workspace: Workspace,
) -> None:
    """BUG-651：新建空记录（年龄未达认领阈值）= 疑似旧版「已建未写 PID」的
    创建窗口——超时拒入且**绝不写入**新协议记录。"""
    workspace.ensure_workspace_dirs()
    lock = start_lock_path(workspace)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(b"")
    with pytest.raises(ManagerLockHeldError) as ei:
        with manager_start_lock(workspace, timeout=0.2):
            pass
    assert ei.value.context.get("legacy") is True
    # 拒入时不得写入：空记录保持原样，不干扰可能的旧版创建者
    assert lock.read_bytes() == b""


def test_manager_start_lock_claims_aged_empty_record(
    workspace: Workspace,
) -> None:
    """BUG-651 自愈：空记录年龄达到旧版陈旧阈值（60s 同口径）→ 创建者崩溃
    残留，就地认领并升级为新协议记录。"""
    workspace.ensure_workspace_dirs()
    lock = start_lock_path(workspace)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(b"")
    old = time.time() - 120
    os.utime(lock, (old, old))
    with manager_start_lock(workspace, timeout=0.2):
        lines = lock.read_text(encoding="utf-8").splitlines()
        assert lines[0].strip() == str(os.getpid())
        assert lines[1].strip() == "flock"
    assert lock.exists()


def test_manager_start_lock_watches_empty_record_until_legacy_finishes(
    workspace: Workspace,
) -> None:
    """BUG-651 全链路：空记录 → 旧版恢复写 PID → 按旧协议等待 → 旧版临界区
    结束（unlink）→ 新版换新文件进入。

    全程旧版「进程」存活（复用本测试进程 PID）；旧版写入/释放通过后台线程
    模拟。关键断言：新版进入时旧版临界区确已结束，且持有的是**新 inode**
    （旧实现会把新协议记录写进旧版创建的 inode，随后被旧版退出 unlink）。
    """
    import threading

    workspace.ensure_workspace_dirs()
    lock = start_lock_path(workspace)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(b"")  # 旧版已建文件、尚未写 PID
    original_ino = lock.stat().st_ino
    released = threading.Event()

    def legacy_creator() -> None:
        time.sleep(0.1)  # 旧版恢复：写单行 PID（旧协议）
        with open(lock, "w", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()}\n")
        time.sleep(0.15)  # 旧版临界区执行中
        with contextlib.suppress(OSError):
            lock.unlink()  # 旧版 finally：unlink 锁路径（进程仍存活）
        released.set()

    t = threading.Thread(target=legacy_creator)
    t.start()
    try:
        with manager_start_lock(workspace, timeout=3.0):
            assert released.is_set()  # 进入时旧临界区确已结束
            assert lock.stat().st_ino != original_ino  # 持有的是新 inode
            lines = lock.read_text(encoding="utf-8").splitlines()
            assert lines[0].strip() == str(os.getpid())
            assert lines[1].strip() == "flock"
    finally:
        t.join(timeout=2.0)


def test_lock_held_exit_code_classifies_by_health(workspace: Workspace) -> None:
    """BUG-641：锁被占用的退出码——健康重复实例 0，其余非零（暴露故障）。"""
    workspace.ensure_workspace_dirs()
    cfg = Config(portPool=PortPool(start=21000, end=21050))
    with patch(
        "local_webpage_access.manager_service.health_matches_workspace",
        return_value=True,
    ):
        assert _lock_held_exit_code(workspace, cfg) == 0
    with patch(
        "local_webpage_access.manager_service.health_matches_workspace",
        return_value=False,
    ):
        assert _lock_held_exit_code(workspace, cfg) == MANAGER_LOCK_FAILURE_EXIT_CODE


_LOCK_CHILD_SCRIPT = """
import sys, time
from pathlib import Path

from local_webpage_access.errors import ManagerLockHeldError
from local_webpage_access.manager_service import manager_instance_lock
from local_webpage_access.paths import Workspace

ws = Workspace(Path(sys.argv[1]))
try:
    with manager_instance_lock(ws):
        print("HELD", flush=True)
        time.sleep(float(sys.argv[2]))
    print("RELEASED", flush=True)
except ManagerLockHeldError:
    print("BLOCKED", flush=True)
"""


def test_manager_instance_lock_real_processes_compete(
    workspace: Workspace,
) -> None:
    """BUG-642 验收：两个真实子进程竞争同一把锁——持有期间第二实例被拒，
    持有者退出后可正常再取，锁文件始终保留。"""
    workspace.ensure_workspace_dirs()
    src = Path(__file__).resolve().parents[1] / "src"
    env = {**os.environ, "PYTHONPATH": str(src)}
    holder = subprocess.Popen(
        [sys.executable, "-c", _LOCK_CHILD_SCRIPT, str(workspace.root), "10"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "HELD"
        blocked = subprocess.run(
            [sys.executable, "-c", _LOCK_CHILD_SCRIPT, str(workspace.root), "0"],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        assert blocked.returncode == 0, blocked.stderr
        assert blocked.stdout.strip() == "BLOCKED"
    finally:
        holder.terminate()
        holder.wait(timeout=10)
    after = subprocess.run(
        [sys.executable, "-c", _LOCK_CHILD_SCRIPT, str(workspace.root), "0"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert after.returncode == 0, after.stderr
    assert after.stdout.splitlines()[:2] == ["HELD", "RELEASED"]
    assert instance_lock_path(workspace).exists()


def test_health_ok_false_on_closed_port() -> None:
    assert health_ok("127.0.0.1", 1, timeout=0.2) is False


def test_manager_health_ignores_env_http_proxy(monkeypatch) -> None:
    """BUG-380：manager_service._fetch_health 不受无效代理影响。"""
    import json
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps({"ok": True, "workspaceRoot": "/tmp/ws"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    port = 0
    server = HTTPServer(("127.0.0.1", 0), _H)
    port = server.server_address[1]
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.delenv("NO_PROXY", raising=False)
        assert health_ok("127.0.0.1", port, timeout=2.0) is True
    finally:
        server.shutdown()
        server.server_close()


def test_health_matches_workspace_legacy_without_workspace_root(
    workspace: Workspace,
) -> None:
    """BUG-065：旧版 health 无 workspaceRoot 时，state.pid 存活则视为本工作区。"""
    workspace.ensure_workspace_dirs()
    cfg = Config(portPool=PortPool(start=21000, end=21050))
    legacy_health = {"ok": True}
    state = ManagerState(enabled=True, pid=4242, host="0.0.0.0", port=cfg.managerPort)
    with patch(
        "local_webpage_access.manager_service._fetch_health",
        return_value=legacy_health,
    ):
        with patch("local_webpage_access.manager_service.is_pid_alive", return_value=True):
            assert health_matches_workspace("0.0.0.0", cfg.managerPort, workspace.root, state=state)
        with patch("local_webpage_access.manager_service.is_pid_alive", return_value=False):
            assert not health_matches_workspace(
                "0.0.0.0", cfg.managerPort, workspace.root, state=state
            )


def test_health_matches_workspace_rejects_foreign_root_even_with_state(
    workspace: Workspace,
) -> None:
    """workspaceRoot 指向其他工作区时，即使有 state 也不视为匹配。"""
    workspace.ensure_workspace_dirs()
    cfg = Config(portPool=PortPool(start=21000, end=21050))
    foreign_health = {"ok": True, "workspaceRoot": "/other/workspace"}
    state = ManagerState(enabled=True, pid=4242, host="0.0.0.0", port=cfg.managerPort)
    with patch(
        "local_webpage_access.manager_service._fetch_health",
        return_value=foreign_health,
    ):
        with patch("local_webpage_access.manager_service.is_pid_alive", return_value=True):
            assert not health_matches_workspace(
                "0.0.0.0", cfg.managerPort, workspace.root, state=state
            )


def test_is_running_legacy_manager_without_workspace_root(workspace: Workspace) -> None:
    """BUG-065：旧版管理页缺 workspaceRoot 时 is_running 仍为 True（update 可重启）。"""
    workspace.ensure_workspace_dirs()
    cfg = Config(portPool=PortPool(start=21000, end=21050))
    write_state(
        workspace,
        ManagerState(enabled=True, pid=4242, host="0.0.0.0", port=cfg.managerPort),
    )
    legacy_health = {"ok": True}
    with patch(
        "local_webpage_access.manager_service._fetch_health",
        return_value=legacy_health,
    ):
        with patch("local_webpage_access.manager_service.is_pid_alive", return_value=True):
            assert is_running(workspace, cfg) is True
        with patch("local_webpage_access.manager_service.is_pid_alive", return_value=False):
            assert is_running(workspace, cfg) is False


# ---- BUG-116：管理页运行时日志不得丢弃 ---------------------------------------


def test_log_file_path_is_workspace_manager_log(workspace: Workspace) -> None:
    """BUG-116：管理页日志落在 workspace logs/manager.log。"""
    assert log_file_path(workspace) == workspace.logs / "manager.log"


def test_spawn_manager_redirects_stdout_to_manager_log(workspace: Workspace) -> None:
    """BUG-116：子进程 stdout/stderr 写入 manager.log，而非 DEVNULL。"""
    import subprocess

    workspace.ensure_workspace_dirs()
    captured: dict = {}

    class FakeProc:
        pid = 424242

    def fake_popen(cmd, **kwargs):  # noqa: ANN001
        captured["kwargs"] = kwargs
        return FakeProc()

    with patch("local_webpage_access.manager_service.subprocess.Popen", side_effect=fake_popen):
        assert _spawn_manager(workspace) == 424242

    kwargs = captured["kwargs"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.STDOUT
    assert kwargs["stdout"] is not subprocess.DEVNULL
    # 句柄应指向 manager.log；父进程关闭自己的副本（避免泄漏）
    assert log_file_path(workspace).is_file()
    assert getattr(kwargs["stdout"], "closed", False) is True


def test_spawn_manager_rejects_windows_native(workspace: Workspace, monkeypatch) -> None:
    """036.09：Windows 原生不得 DETACHED_PROCESS 启动 manager。"""
    import local_webpage_access.manager_service as ms

    monkeypatch.setattr(ms.sys, "platform", "win32")
    with pytest.raises(RuntimeError, match="Windows 原生不受支持"):
        _spawn_manager(workspace)


def test_spawn_manager_rotates_manager_log_before_open(workspace: Workspace, monkeypatch) -> None:
    """BUG-186：_spawn_manager 须经 open_append 打开 manager.log。"""
    from local_webpage_access import logs as logs_mod

    workspace.ensure_workspace_dirs()
    calls: list[Path] = []
    real = logs_mod.open_append

    def spy(path, **kwargs):
        calls.append(Path(path))
        return real(path, **kwargs)

    monkeypatch.setattr(logs_mod, "open_append", spy)

    class FakeProc:
        pid = 424243

    monkeypatch.setattr(
        "local_webpage_access.manager_service.subprocess.Popen",
        lambda *a, **k: FakeProc(),
    )
    log_file_path(workspace).write_text("old-manager\n", encoding="utf-8")
    assert _spawn_manager(workspace) == 424243
    assert any(c.name == "manager.log" for c in calls)


def test_read_manager_log_tail(workspace: Workspace) -> None:
    """BUG-116：可读管理页日志尾部。"""
    workspace.ensure_workspace_dirs()
    assert read_manager_log(workspace, tail=10) == ""
    path = log_file_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(f"line{i}" for i in range(10)) + "\n", encoding="utf-8")
    assert read_manager_log(workspace, tail=3) == "line7\nline8\nline9"
    assert read_manager_log(workspace, tail=0).startswith("line0")
    assert read_manager_log(workspace, tail=50).count("line") == 10


def test_run_service_main_passes_log_dir(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BUG-116：子进程入口 setup_logging 必须传入 workspace.logs。"""
    from local_webpage_access import manager_service

    workspace.ensure_workspace_dirs()
    (workspace.root / "local-web.yml").write_text(
        "portPool:\n  start: 21000\n  end: 21050\nmanagerEnabled: true\n",
        encoding="utf-8",
    )
    seen: dict = {}

    def fake_setup_logging(level="INFO", log_dir=None, *, log_filename="lwa.log", force=False):  # noqa: ANN001
        seen["log_dir"] = log_dir
        return None

    monkeypatch.setattr(
        sys,
        "argv",
        ["manager_service", "--workspace", str(workspace.root)],
    )
    monkeypatch.setattr("local_webpage_access.logging.setup_logging", fake_setup_logging)
    with patch("local_webpage_access.manager_api.run_manager"):
        with patch("local_webpage_access.registry.Registry") as reg_cls:
            reg_cls.return_value.open.return_value = None
            reg_cls.return_value.close.return_value = None
            assert manager_service.run_service_main() == 0
    assert seen.get("log_dir") == workspace.logs


# ---- IPv6 managerHost 健康探测（CHK-175/176/177）---------------------------


def test_health_check_host_maps_wildcards_by_family() -> None:
    """通配绑定按地址族映射到对应回环；不得把 ::1/:: 一律改成 127.0.0.1。"""
    from local_webpage_access.manager_service import _health_check_host

    assert _health_check_host("0.0.0.0") == "127.0.0.1"
    assert _health_check_host("") == "127.0.0.1"
    assert _health_check_host("::") == "::1"
    assert _health_check_host("::1") == "::1"
    assert _health_check_host("127.0.0.1") == "127.0.0.1"
    assert _health_check_host("2001:db8::1") == "2001:db8::1"


def test_fetch_health_brackets_ipv6_in_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """探测 URL 对 IPv6 主机加 []，避免 host:port 歧义导致探活失真。"""
    from local_webpage_access import manager_service

    seen: dict[str, str] = {}

    class _Resp:
        status = 200

        def read(self) -> bytes:
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(url: str, timeout: float = 1.0):  # noqa: ARG001
        seen["url"] = url
        return _Resp()

    monkeypatch.setattr(manager_service, "urlopen_direct", fake_urlopen)

    assert manager_service._fetch_health("::1", 17800) == {"ok": True}
    assert seen["url"] == "http://[::1]:17800/api/health"

    assert manager_service._fetch_health("::", 17800) == {"ok": True}
    assert seen["url"] == "http://[::1]:17800/api/health"

    assert manager_service._fetch_health("2001:db8::a", 17800) == {"ok": True}
    assert seen["url"] == "http://[2001:db8::a]:17800/api/health"

    assert manager_service._fetch_health("0.0.0.0", 17800) == {"ok": True}
    assert seen["url"] == "http://127.0.0.1:17800/api/health"
