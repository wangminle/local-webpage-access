"""AGC-W13：MCP stdio 适配器测试。

- 真实子进程（``python -c`` 起 ``register(app)`` 的 typer app，PYTHONPATH 指向 src）：
  initialize、list_tools（13 工具）、manager 离线时 call_tool 返回 isError +
  manager_unavailable、stdout 每行都是合法 JSON-RPC（不混日志）；
- bridge 单元测试（monkeypatch ``urlopen_direct``/``is_running``/``read_token``）：
  token 头注入、capabilities 的 workspaceId 不匹配拒绝、LAN-only manager 拒绝。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from local_webpage_access.agent.contracts import TOOL_SPECS
from local_webpage_access.paths import Workspace

_SRC = Path(__file__).resolve().parents[1] / "src"

# 与真实 `lwa mcp --workspace <path>` 同构：根 typer app + register 挂载。
_CHILD_SCRIPT = """
import sys
import typer
from local_webpage_access.cli._common import bootstrap
from local_webpage_access.cli.mcp import register

bootstrap("WARNING")
app = typer.Typer()


@app.callback()
def _cb() -> None:
    pass  # 空 callback：防止单命令 app 折叠，保持 `mcp` 子命令名


register(app)
sys.argv = ["lwa", "mcp", "--workspace", sys.argv[1]]
app()
"""


@pytest.fixture()
def mcp_workspace(tmp_path: Path) -> Workspace:
    """已 init 形状（有 local-web.yml）但 manager 离线的临时工作区。"""
    root = tmp_path / "ws"
    root.mkdir()
    (root / "local-web.yml").write_text("managerPort: 17899\n", encoding="utf-8")
    ws = Workspace(root)
    ws.ensure_workspace_dirs()
    return ws


def _server_params(workspace: Workspace):
    from mcp.client.stdio import StdioServerParameters

    return StdioServerParameters(
        command=sys.executable,
        args=["-c", _CHILD_SCRIPT, str(workspace.root)],
        env={**os.environ, "PYTHONPATH": str(_SRC)},
    )


async def _with_session(workspace: Workspace, fn) -> Any:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async with stdio_client(_server_params(workspace)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)


# ---- 真实子进程 ---------------------------------------------------------------


def test_stdio_initialize_list_tools_and_resources(mcp_workspace: Workspace) -> None:
    async def go(session) -> dict[str, Any]:
        tools = await session.list_tools()
        resources = await session.list_resources()
        guide = await session.read_resource("lwa://guide/deploy")
        return {"tools": tools, "resources": resources, "guide": guide}

    out = asyncio.run(_with_session(mcp_workspace, go))

    tools = out["tools"].tools
    names = {t.name for t in tools}
    assert names == set(TOOL_SPECS)
    assert len(tools) == 13
    cap = next(t for t in tools if t.name == "lwa_get_capabilities")
    assert cap.input_schema["type"] == "object"
    # annotations 与契约共源（lwa_get_capabilities：readOnly=True，其余默认 False）
    assert cap.annotations is not None
    assert cap.annotations.read_only_hint is True
    assert cap.annotations.idempotent_hint is False
    apply_tool = next(t for t in tools if t.name == "lwa_apply_deployment")
    assert apply_tool.annotations is not None
    assert apply_tool.annotations.read_only_hint is False
    assert apply_tool.annotations.idempotent_hint is True

    uris = {str(r.uri) for r in out["resources"].resources}
    assert uris == {"lwa://guide/quickstart", "lwa://guide/deploy", "lwa://guide/troubleshooting"}
    guide_text = out["guide"].contents[0].text
    assert "幂等" in guide_text


def test_stdio_call_tool_manager_offline(mcp_workspace: Workspace) -> None:
    async def go(session):
        return await session.call_tool("lwa_get_capabilities", {})

    result = asyncio.run(_with_session(mcp_workspace, go))

    assert result.is_error is True
    text = result.content[0].text
    assert "manager_unavailable" in text
    payload = json.loads(text)
    assert payload["error"]["code"] == "manager_unavailable"
    assert "lwa manager on" in payload["error"]["message"]


def test_stdio_call_tool_invalid_arguments(mcp_workspace: Workspace) -> None:
    async def go(session):
        return await session.call_tool("lwa_get_instance", {"instanceId": 5})

    result = asyncio.run(_with_session(mcp_workspace, go))

    assert result.is_error is True
    payload = json.loads(result.content[0].text)
    assert payload["error"]["code"] == "needs_input"


def test_stdio_stdout_is_pure_jsonrpc(mcp_workspace: Workspace) -> None:
    """逐行解析子进程 stdout：每行都必须是合法 JSON-RPC（日志只能走 stderr）。"""
    env = {**os.environ, "PYTHONPATH": str(_SRC)}
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", _CHILD_SCRIPT, str(mcp_workspace.root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert proc.stdin is not None and proc.stdout is not None
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "purity-test", "version": "0"},
            },
        }
        proc.stdin.write(json.dumps(init_req) + "\n")
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n"
        )
        proc.stdin.flush()
        for expected_id in (1, 2):
            line = proc.stdout.readline()
            assert line, f"子进程 stdout 提前 EOF（id={expected_id}）"
            msg = json.loads(line)  # 非 JSON 行（日志混入）会在这里失败
            assert msg["jsonrpc"] == "2.0"
            assert msg["id"] == expected_id
            assert "result" in msg
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


# ---- bridge 单元测试（在线行为，monkeypatch HTTP 层） ----------------------------


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.status = 200

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


@pytest.fixture()
def online_bridge(monkeypatch: pytest.MonkeyPatch, mcp_workspace: Workspace):
    """manager 在线 + token 可读的 bridge；HTTP 出口替换为捕获式假实现。"""
    from local_webpage_access.mcp import client_bridge

    captured: dict[str, Any] = {}

    def fake_open(req, *, timeout=None):  # noqa: ANN001, ANN202
        captured["req"] = req
        captured["timeout"] = timeout
        return _FakeResponse(captured.get("payload") or {"ok": True})

    monkeypatch.setattr(client_bridge, "urlopen_direct", fake_open)
    monkeypatch.setattr(
        "local_webpage_access.manager_service.is_running", lambda ws, config: True
    )
    monkeypatch.setattr(
        "local_webpage_access.manager_api.read_token", lambda ws: "tok-123"
    )
    return client_bridge.AgentBridge(mcp_workspace), captured


def test_bridge_injects_bearer_token(online_bridge) -> None:
    bridge, captured = online_bridge
    result, error = bridge.call_tool("lwa_list_instances", {})
    assert error is None
    assert result == {"ok": True}
    req = captured["req"]
    assert req.headers["Authorization"] == "Bearer tok-123"
    assert req.full_url.startswith("http://127.0.0.1:17899/api/agent/v1/instances")


def test_bridge_rejects_workspace_id_mismatch(
    online_bridge, mcp_workspace: Workspace
) -> None:
    from local_webpage_access.registry import Registry

    reg = Registry(mcp_workspace.db_path)
    reg.open()
    local_id = reg.get_or_create_workspace_id()
    reg.close()

    bridge, captured = online_bridge
    captured["payload"] = {"workspaceId": "other-workspace"}
    result, error = bridge.call_tool("lwa_get_capabilities", {})
    assert result is None
    assert error is not None
    assert error["code"] == "permission_denied"
    assert "workspaceId" in error["message"]

    # 一致时放行
    captured["payload"] = {"workspaceId": local_id}
    result, error = bridge.call_tool("lwa_get_capabilities", {})
    assert error is None
    assert result == {"workspaceId": local_id}


def test_bridge_rejects_lan_only_manager(
    monkeypatch: pytest.MonkeyPatch, mcp_workspace: Workspace
) -> None:
    from local_webpage_access.config import Config
    from local_webpage_access.mcp import client_bridge

    monkeypatch.setattr(
        "local_webpage_access.config.load_config",
        lambda ws: Config(managerHost="192.168.1.10"),
    )
    bridge = client_bridge.AgentBridge(mcp_workspace)
    result, error = bridge.call_tool("lwa_get_capabilities", {})
    assert result is None
    assert error is not None
    assert error["code"] == "manager_unavailable"
    assert "LAN" in error["message"]


def test_bridge_manager_offline(mcp_workspace: Workspace) -> None:
    from local_webpage_access.mcp import client_bridge

    bridge = client_bridge.AgentBridge(mcp_workspace)
    result, error = bridge.call_tool("lwa_get_capabilities", {})
    assert result is None
    assert error is not None
    assert error["code"] == "manager_unavailable"
    assert "lwa manager on" in error["message"]


def test_uncaught_tool_exception_is_interrupted_not_busy(
    monkeypatch: pytest.MonkeyPatch, mcp_workspace: Workspace
) -> None:
    """CHK-345 P3：适配器内部未捕获异常不得标 busy（retryable），应标 interrupted。"""
    from mcp.types import CallToolRequestParams

    from local_webpage_access.mcp.client_bridge import AgentBridge
    from local_webpage_access.mcp.server import build_server

    def boom(self, name: str, arguments: dict[str, Any]):  # noqa: ARG001, ANN202
        raise RuntimeError("kaboom")

    monkeypatch.setattr(AgentBridge, "call_tool", boom)
    server = build_server(mcp_workspace)
    entry = server.get_request_handler("tools/call")
    assert entry is not None
    result = asyncio.run(
        entry.handler(None, CallToolRequestParams(name="lwa_get_capabilities", arguments={}))
    )
    assert result.is_error is True
    payload = json.loads(result.content[0].text)
    err = payload["error"]
    assert err["code"] == "interrupted"
    assert err["retryable"] is False
    assert "kaboom" in err["message"]
    assert "可稍后重试" not in err["message"]


def test_bridge_http_error_passthrough(
    monkeypatch: pytest.MonkeyPatch, mcp_workspace: Workspace
) -> None:
    from local_webpage_access.mcp import client_bridge

    body = json.dumps(
        {"error": {"code": "busy", "message": "服务繁忙", "retryable": True}}
    ).encode("utf-8")

    def fake_open(req, *, timeout=None):  # noqa: ANN001, ANN202
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many", hdrs=None, fp=io.BytesIO(body))

    monkeypatch.setattr(client_bridge, "urlopen_direct", fake_open)
    monkeypatch.setattr(
        "local_webpage_access.manager_service.is_running", lambda ws, config: True
    )
    monkeypatch.setattr("local_webpage_access.manager_api.read_token", lambda ws: "tok-123")

    bridge = client_bridge.AgentBridge(mcp_workspace)
    result, error = bridge.call_tool("lwa_get_capabilities", {})
    assert result is None
    assert error is not None
    assert error["code"] == "busy"
    assert error["retryable"] is True


def test_bridge_no_redirect_handler_refuses() -> None:
    """凭据保护：redirect_request 一律返回 None（urllib 把 3xx 变成 HTTPError）。"""
    from local_webpage_access.mcp.client_bridge import _NoRedirectHandler

    handler = _NoRedirectHandler()
    assert (
        handler.redirect_request(None, None, 302, "Found", {}, "http://evil.example/")
        is None
    )
