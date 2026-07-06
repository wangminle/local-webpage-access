"""管理页后台服务测试。"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from local_webpage_access.config import Config, PortPool
from local_webpage_access.errors import LifecycleError
from local_webpage_access.manager_service import (
    ManagerState,
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
