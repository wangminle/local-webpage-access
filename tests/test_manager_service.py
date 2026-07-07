"""管理页后台服务测试。"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from local_webpage_access.config import Config, PortPool
from local_webpage_access.errors import LifecycleError
from local_webpage_access.manager_service import (
    ManagerState,
    health_matches_workspace,
    health_ok,
    is_running,
    maybe_start_manager,
    read_state,
    start_manager,
    stop_manager,
    write_state,
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
