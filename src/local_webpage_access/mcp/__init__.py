"""MCP 适配器包（AGC-W13）。

把 13 个 Agent 工具以 MCP 协议（stdio 传输）暴露给本机 Agent 客户端；
业务转发走 :mod:`~local_webpage_access.mcp.client_bridge`（本机回环 HTTP →
manager 的 ``/api/agent/v1/*``），协议层在 :mod:`~local_webpage_access.mcp.server`。

**顶层 import 本包不要求安装 MCP SDK**（可选依赖 ``local-webpage-access[mcp]``）：
SDK 只在 ``server`` 模块内导入，且经下面的惰性属性访问延迟加载。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from local_webpage_access.mcp.client_bridge import AgentBridge
    from local_webpage_access.mcp.server import build_server, run_stdio_server

__all__ = ["AgentBridge", "build_server", "run_stdio_server"]


def __getattr__(name: str):  # noqa: ANN202 — 惰性加载，类型见 TYPE_CHECKING
    if name in {"build_server", "run_stdio_server"}:
        from local_webpage_access.mcp import server

        return getattr(server, name)
    if name == "AgentBridge":
        from local_webpage_access.mcp.client_bridge import AgentBridge

        return AgentBridge
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
