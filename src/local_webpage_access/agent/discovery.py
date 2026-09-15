"""Agent 发现入口（AGC-W03，M0）。

提供三个**公开、无鉴权**的只读端点（设计 §5.1 产品发现层）：

- ``GET /llms.txt``      —— 社区提案的 llmstxt.org 文档索引格式，链接精简指南；
- ``GET /agent-info.json`` —— LWA 自定义机器可读元信息（产品、契约版本、入口、鉴权要求）；
- ``GET /agent-guide``   —— 包内精简 Agent 指南（与正式文档共用的模板，M0 版）。

安全边界（设计 §5.1 / §7.3）：

- 响应只含静态元信息与包内模板，**不读** workspace、registry、token、主机信息；
- 链接一律站点相对路径（以 ``/`` 开头），不信任 Host / X-Forwarded-* 生成绝对地址；
- MCP 未开放时必须如实返回 ``enabled: false``（不夸大能力）。

这些端点不进入 ``/api`` 命名空间，也不复用回环免 token 规则：它们本来就没有秘密。
"""

from __future__ import annotations

from importlib import resources
from typing import Any

from fastapi import FastAPI, Response

#: 产品标识（保持与 pyproject ``name`` 一致）
PRODUCT = "local-webpage-access"

#: 发现契约版本（独立于产品版本递增，设计 §8）
DISCOVERY_VERSION = "1"

LLMS_TXT_ROUTE = "/llms.txt"
AGENT_INFO_ROUTE = "/agent-info.json"
GUIDE_ROUTE = "/agent-guide"

#: Agent 专用 HTTP API 基址。None = 尚未开放（M1 落地 ``/api/agent/v1`` 后改值）。
AGENT_API_BASE: str | None = None


def build_agent_info() -> dict[str, Any]:
    """构造 ``/agent-info.json`` 响应体（纯静态，无实例/主机数据）。"""
    return {
        "product": PRODUCT,
        "discoveryVersion": DISCOVERY_VERSION,
        "guide": GUIDE_ROUTE,
        "apiBase": AGENT_API_BASE,
        "mcp": {"enabled": False},
        "authentication": {"required": True, "contact": "LWA 工作区管理员"},
    }


def build_llms_txt() -> str:
    """构造 ``/llms.txt``（llmstxt.org 提案格式：H1 标题 + 摘要 + 链接列表）。"""
    lines = [
        "# Local Webpage Access (LWA)",
        "",
        "> 局域网小主机本地网页部署基座（CLI：lwa）。本文件面向 LLM Agent，"
        "提供接入 LWA 所需的最小文档索引。",
        "",
        "- [Agent 接入快速指南](/agent-guide): 鉴权方式、部署输入限制、现有 API 与操作红线",
        "- [机器可读发现信息](/agent-info.json): 产品、发现契约版本、入口与鉴权要求",
        "",
    ]
    return "\n".join(lines)


def load_guide_markdown() -> str:
    """读取包内精简指南（``agent/guides/quickstart.md``，随包分发）。"""
    guide = resources.files("local_webpage_access.agent.guides") / "quickstart.md"
    return guide.read_text(encoding="utf-8")


def register_agent_discovery(app: FastAPI) -> None:
    """在 manager 应用上注册发现路由。

    必须在 SPA catch-all / 静态挂载**之前**调用（设计 §4.1）；
    由 :func:`local_webpage_access.manager_api.create_app` 统一挂载。
    """
    @app.get(LLMS_TXT_ROUTE, tags=["agent-discovery"], summary="llms.txt 文档索引（公开）")
    def llms_txt() -> Response:
        return Response(build_llms_txt(), media_type="text/markdown; charset=utf-8")

    @app.get(AGENT_INFO_ROUTE, tags=["agent-discovery"], summary="Agent 发现元信息（公开）")
    def agent_info() -> dict[str, Any]:
        return build_agent_info()

    @app.get(GUIDE_ROUTE, tags=["agent-discovery"], summary="Agent 接入快速指南（公开，Markdown）")
    def agent_guide() -> Response:
        return Response(load_guide_markdown(), media_type="text/markdown; charset=utf-8")
