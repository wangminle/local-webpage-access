"""MCP lowlevel Server（AGC-W13）：13 个 Agent 工具 + 3 个包内指南资源。

- 工具清单与 schema **契约共源**：一律从 :data:`agent.contracts.TOOL_SPECS` 生成
  （``input_model.model_json_schema()``），禁止手抄漂移；
- annotations 按 ToolSpec 真实标注映射（readOnly/destructive/idempotent/openWorld）；
- SDK 2.x 对 handler 未捕获异常只回通用错误文本——业务错误一律在
  ``on_call_tool`` 内捕获并以 ``isError=True`` 的工具结果返回（契约错误 JSON）；
- stdio 传输：stdout 只承载 JSON-RPC 帧，任何诊断都走 stderr（由日志层保证）。
"""

from __future__ import annotations

import asyncio
import importlib.resources
import json
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server

from local_webpage_access.agent.contracts import ERROR_SPECS, TOOL_SPECS, AgentErrorCode, ToolSpec
from local_webpage_access.logging import get_logger
from local_webpage_access.mcp.client_bridge import AgentBridge
from local_webpage_access.paths import Workspace

log = get_logger("mcp.server")

#: lwa:// 资源 URI → （包内 guides 文件名, 标题）
_GUIDE_RESOURCES: dict[str, tuple[str, str]] = {
    "lwa://guide/quickstart": ("quickstart.md", "Agent 接入快速指南"),
    "lwa://guide/deploy": ("deploy.md", "部署闭环指南（plan/apply/operation 与幂等键）"),
    "lwa://guide/troubleshooting": ("troubleshooting.md", "错误处置指南"),
}


def tool_to_mcp(spec: ToolSpec) -> types.Tool:
    """契约 ToolSpec → MCP Tool（schema 共源，annotations 按真实标注）。

    BUG-709（AGC-W04 / 设计 §6.1、§8）：输入**和输出** JSON Schema 都从
    TOOL_SPECS 共源发布——自动客户端不翻文档即可生成调用与解析代码。
    """
    return types.Tool(
        name=spec.name,
        title=spec.title,
        description=spec.description,
        input_schema=spec.input_model.model_json_schema(),
        output_schema=spec.output_model.model_json_schema(),
        annotations=types.ToolAnnotations(
            title=spec.title,
            read_only_hint=spec.read_only,
            destructive_hint=spec.destructive,
            idempotent_hint=spec.idempotent,
            open_world_hint=spec.external_interaction,
        ),
    )


def _result_text(payload: dict[str, Any]) -> types.TextContent:
    return types.TextContent(
        type="text", text=json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _error_result(error: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(
        content=[_result_text({"error": error})],
        is_error=True,
    )


def uncaught_tool_error(exc: Exception) -> dict[str, Any]:
    """适配器内部未捕获异常：契约码 ``interrupted``（不可重试），不伪装 busy。"""
    spec = ERROR_SPECS[AgentErrorCode.interrupted]
    return {
        "code": AgentErrorCode.interrupted.value,
        "message": f"MCP 适配器内部错误（{type(exc).__name__}: {exc}）",
        "retryable": spec.retryable,
    }


def _read_guide(filename: str) -> str:
    ref = importlib.resources.files("local_webpage_access.agent.guides").joinpath(filename)
    text = ref.read_text(encoding="utf-8")
    if "{{PRODUCT_VERSION}}" in text:
        # 与 /agent-guide 一致：版本占位符在分发时替换（H3-4）
        from local_webpage_access.version_info import display_version

        text = text.replace("{{PRODUCT_VERSION}}", display_version())
    return text


def build_server(workspace: Workspace) -> Server:
    """构造绑定指定工作区的 lowlevel MCP Server。"""
    from local_webpage_access.version_info import resolve_version

    bridge = AgentBridge(workspace)

    async def on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:  # noqa: ARG001
        return types.ListToolsResult(tools=[tool_to_mcp(spec) for spec in TOOL_SPECS.values()])

    async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:  # noqa: ARG001
        arguments = dict(params.arguments) if isinstance(params.arguments, dict) else {}
        try:
            result, error = bridge.call_tool(params.name, arguments)
        except Exception as exc:  # noqa: BLE001 — 适配器内部错误不得裸抛给 SDK
            log.exception("工具 %s 调用内部错误", params.name)
            return _error_result(uncaught_tool_error(exc))
        if error is not None:
            return _error_result(error)
        assert result is not None
        return types.CallToolResult(
            content=[_result_text(result)],
            structured_content=result,
        )

    async def on_list_resources(ctx: Any, params: Any) -> types.ListResourcesResult:  # noqa: ARG001
        return types.ListResourcesResult(
            resources=[
                types.Resource(
                    uri=uri,
                    name=filename.removesuffix(".md"),
                    title=title,
                    description=f"LWA Agent 指南：{title}",
                    mime_type="text/markdown",
                )
                for uri, (filename, title) in _GUIDE_RESOURCES.items()
            ]
        )

    async def on_read_resource(ctx: Any, params: types.ReadResourceRequestParams) -> types.ReadResourceResult:  # noqa: ARG001
        uri = str(params.uri)
        entry = _GUIDE_RESOURCES.get(uri)
        if entry is None:
            raise ValueError(f"未知资源: {uri!r}（可用: {sorted(_GUIDE_RESOURCES)}）")
        filename, _title = entry
        return types.ReadResourceResult(
            contents=[
                types.TextResourceContents(
                    uri=params.uri,
                    mime_type="text/markdown",
                    text=_read_guide(filename),
                )
            ]
        )

    return Server(
        "lwa-local-webpage-access",
        version=resolve_version(),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        on_list_resources=on_list_resources,
        on_read_resource=on_read_resource,
    )


async def _run_stdio(workspace: Workspace) -> None:
    from mcp.server.stdio import stdio_server

    server = build_server(workspace)
    async with stdio_server() as (read, write):
        # 业务错误已在 handler 内转为 isError 结果；此处用默认 raise_exceptions=False，
        # 意外异常由 SDK 转为通用错误文本而不是杀死适配器进程。
        await server.run(read, write, server.create_initialization_options())


def run_stdio_server(workspace: Workspace) -> None:
    """同步入口（``lwa mcp``）：asyncio.run + stdio_server，阻塞至客户端断开。"""
    asyncio.run(_run_stdio(workspace))
