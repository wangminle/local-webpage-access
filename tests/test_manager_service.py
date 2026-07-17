"""管理页后台服务测试。"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from local_webpage_access.config import Config, PortPool
from local_webpage_access.errors import LifecycleError
from local_webpage_access.manager_service import (
    ManagerState,
    manager_start_lock,
    manager_instance_lock,
    instance_lock_path,
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
    """BUG-130：死 PID 或超龄的 manager 启动锁可立即回收。"""
    workspace.ensure_workspace_dirs()
    lock = workspace.run / "manager-start.lock"
    lock.write_text("999999\n", encoding="utf-8")
    old = time.time() - 120
    os.utime(lock, (old, old))

    with patch("local_webpage_access.manager_service.is_pid_alive", return_value=False):
        with manager_start_lock(workspace, timeout=0.1):
            assert lock.is_file()
    assert not lock.exists()


def test_manager_instance_lock_exclusive_when_live_holder(
    workspace: Workspace,
) -> None:
    """BUG-193：已有存活实例持锁时第二次获取抛 LifecycleError，且不删他人锁。"""
    workspace.ensure_workspace_dirs()
    lock = instance_lock_path(workspace)
    lock.parent.mkdir(parents=True, exist_ok=True)
    # 存活持有者 = 本进程 PID
    lock.write_text(f"{os.getpid()}\n", encoding="utf-8")
    with pytest.raises(LifecycleError):
        with manager_instance_lock(workspace):
            pass
    # 关键：失败获取不得删活跃实例的锁（BUG-173 同款）
    assert lock.exists()


def test_manager_instance_lock_reclaims_stale(workspace: Workspace) -> None:
    """BUG-193：持有进程已死 → 回收陈旧锁后正常获取。"""
    workspace.ensure_workspace_dirs()
    lock = instance_lock_path(workspace)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("999999\n", encoding="utf-8")  # 死 PID
    with patch("local_webpage_access.manager_service.is_pid_alive", return_value=False):
        with manager_instance_lock(workspace):
            assert lock.read_text(encoding="utf-8").strip() == str(os.getpid())
    assert not lock.exists()  # 退出后释放


def test_manager_instance_lock_released_on_exit(workspace: Workspace) -> None:
    """BUG-193：正常获取并退出后释放锁文件。"""
    workspace.ensure_workspace_dirs()
    lock = instance_lock_path(workspace)
    assert not lock.exists()
    with manager_instance_lock(workspace):
        assert lock.exists()
        assert lock.read_text(encoding="utf-8").strip() == str(os.getpid())
    assert not lock.exists()


def test_health_ok_false_on_closed_port() -> None:
    assert health_ok("127.0.0.1", 1, timeout=0.2) is False


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
        with patch(
            "local_webpage_access.manager_service.is_pid_alive", return_value=True
        ):
            assert health_matches_workspace(
                "0.0.0.0", cfg.managerPort, workspace.root, state=state
            )
        with patch(
            "local_webpage_access.manager_service.is_pid_alive", return_value=False
        ):
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
        with patch(
            "local_webpage_access.manager_service.is_pid_alive", return_value=True
        ):
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
        with patch(
            "local_webpage_access.manager_service.is_pid_alive", return_value=True
        ):
            assert is_running(workspace, cfg) is True
        with patch(
            "local_webpage_access.manager_service.is_pid_alive", return_value=False
        ):
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


def test_spawn_manager_rotates_manager_log_before_open(
    workspace: Workspace, monkeypatch
) -> None:
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

    def fake_setup_logging(level="INFO", log_dir=None, *, force=False):  # noqa: ANN001
        seen["log_dir"] = log_dir
        return None

    monkeypatch.setattr(
        sys,
        "argv",
        ["manager_service", "--workspace", str(workspace.root)],
    )
    monkeypatch.setattr(
        "local_webpage_access.logging.setup_logging", fake_setup_logging
    )
    with patch("local_webpage_access.manager_api.run_manager"):
        with patch("local_webpage_access.registry.Registry") as reg_cls:
            reg_cls.return_value.open.return_value = None
            reg_cls.return_value.close.return_value = None
            assert manager_service.run_service_main() == 0
    assert seen.get("log_dir") == workspace.logs
