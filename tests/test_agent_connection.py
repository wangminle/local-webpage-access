"""``lwa agent connection-info`` CLI 测试（AGC-W14）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from local_webpage_access.init_workspace import init_workspace
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "ws"
    init_workspace(root)
    return Workspace(root)


def _assign_workspace_id(ws: Workspace) -> str:
    reg = Registry(ws.db_path)
    reg.open()
    try:
        return reg.get_or_create_workspace_id()
    finally:
        reg.close()


def _invoke(args: list[str]):
    from local_webpage_access.cli.agent import app

    return CliRunner().invoke(app, args)


def test_connection_info_json_matches_workspace_id(workspace: Workspace) -> None:
    """JSON 输出包含 apiBase/workspaceId/契约与产品版本，且无 token 字段。"""
    expected = _assign_workspace_id(workspace)
    result = _invoke(["connection-info", "--workspace", str(workspace.root), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["workspaceId"] == expected
    assert data["apiBase"].startswith("http://127.0.0.1:")
    assert data["apiBase"].endswith("/api/agent/v1")
    assert data["contractVersion"] == "1"
    assert data["productVersion"]
    assert isinstance(data["manager"]["online"], bool)
    assert isinstance(data["agent"]["allowedSourceRoots"], list)
    # 安全红线：输出任何层级都不得包含 token / secret
    assert "token" not in result.stdout.lower()
    assert "secret" not in result.stdout.lower()


def test_connection_info_distinct_workspace_ids(tmp_path: Path) -> None:
    """两个工作区各自返回自己的 workspaceId（R01 不误连判据）。"""
    ws_a = Workspace(tmp_path / "a")
    ws_b = Workspace(tmp_path / "b")
    init_workspace(ws_a.root)
    init_workspace(ws_b.root)
    id_a = _assign_workspace_id(ws_a)
    id_b = _assign_workspace_id(ws_b)
    assert id_a != id_b

    out_a = _invoke(["connection-info", "-w", str(ws_a.root), "--json"])
    out_b = _invoke(["connection-info", "-w", str(ws_b.root), "--json"])
    assert out_a.exit_code == 0 and out_b.exit_code == 0
    assert json.loads(out_a.stdout)["workspaceId"] == id_a
    assert json.loads(out_b.stdout)["workspaceId"] == id_b


def test_connection_info_workspace_id_not_yet_assigned(workspace: Workspace) -> None:
    """registry 尚无 workspace_id 时输出 null 而非隐式生成。"""
    result = _invoke(["connection-info", "-w", str(workspace.root), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["workspaceId"] is None


def test_connection_info_rejects_non_workspace(tmp_path: Path) -> None:
    """不存在/未初始化的目录报错退出，且不隐式 init。"""
    target = tmp_path / "not-a-ws"
    target.mkdir()
    result = _invoke(["connection-info", "-w", str(target)])
    assert result.exit_code == 1
    assert not (target / "local-web.yml").exists()
    assert not (target / "registry").exists()


def test_connection_info_rejects_relative_path() -> None:
    """--workspace 必须是绝对路径。"""
    result = _invoke(["connection-info", "-w", "relative/dir"])
    assert result.exit_code == 1


# ---- BUG-694：localhost 绑定的回环判定口径 ---------------------------------------------


def test_connection_info_localhost_bind_counts_loopback(
    workspace: Workspace, monkeypatch
) -> None:
    """BUG-694：managerHost=localhost 同样回环可达（与 MCP bridge 口径一致）。

    修复前 CLI 集合只有 127.0.0.1/::1，localhost 绑定被误判回环不可达。
    """
    import local_webpage_access.config as config_mod
    from local_webpage_access import manager_service
    from local_webpage_access.config import AgentConfig, Config

    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda ws: Config(
            staticGateway="builtin",
            managerHost="localhost",
            agent=AgentConfig(allowedSourceRoots=[ws.root]),
        ),
    )
    monkeypatch.setattr(manager_service, "_fetch_health", lambda *a, **k: None)

    result = _invoke(["connection-info", "--workspace", str(workspace.root), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["manager"]["loopbackReachable"] is True
    assert data["manager"]["bindHost"] == "localhost"


def test_loopback_host_set_shared_between_cli_and_mcp_bridge() -> None:
    """BUG-694：CLI 与 MCP bridge 共用 agent.auth.LOOPBACK_HOSTS，口径锁定。"""
    from local_webpage_access.agent.auth import LOOPBACK_HOSTS
    from local_webpage_access.mcp import client_bridge

    assert client_bridge._LOOPBACK_HOSTS is LOOPBACK_HOSTS
    assert LOOPBACK_HOSTS == frozenset({"127.0.0.1", "::1", "localhost"})
