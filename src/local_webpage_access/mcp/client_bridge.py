"""AgentBridge：把 13 个 Agent 工具调用转发到本机 manager 的 ``/api/agent/v1/*``（AGC-W13）。

安全约束（M1 本机 owner，stdio 模式）：

- **仅回环基址**：``_health_check_host(config.managerHost)`` 解析后必须仍是回环
  （127.0.0.1 / ::1 / localhost）；manager 只绑定具体 LAN IP 时直接拒绝——
  绝不把 owner token 发往明文 LAN 地址；
- **禁跨域重定向**：urllib 默认 follow redirect 会把 ``Authorization`` 头带去
  重定向目标——自定义 opener 让 3xx 直接成为错误；
- **绕过环境代理**（BUG-380 惯例）：``ProxyHandler({})``；
- token 每次调用经 ``read_token`` 重读（轮换友好）；manager 离线返回
  ``manager_unavailable`` 业务错误并附 ``lwa manager on`` 指引，绝不隐式启动任何进程；
- ``lwa_get_capabilities`` 的 ``workspaceId`` 与本工作区 registry 比对
  （只读打开，读不到则跳过），不一致即拒绝——防止误连其他工作区的 manager。
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from pydantic import ValidationError

from local_webpage_access.agent.contracts import ERROR_SPECS, TOOL_SPECS, AgentErrorCode
from local_webpage_access.agent.auth import LOOPBACK_HOSTS as _LOOPBACK_HOSTS
from local_webpage_access.errors import LwaError
from local_webpage_access.logging import get_logger
from local_webpage_access.paths import Workspace

log = get_logger("mcp.bridge")

AGENT_API_PREFIX = "/api/agent/v1"

_DEFAULT_TIMEOUT = 30.0
# plan 可能含 Git 克隆（服务端超时 180s），客户端留足余量
_PLAN_TIMEOUT = 200.0


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """禁止任何重定向跟随：避免 ``Authorization`` 凭据被带往重定向目标。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN202, ARG002
        return None


# BUG-380 同源：忽略环境代理；另加禁重定向（凭据保护，见模块 docstring）。
_DIRECT_NO_REDIRECT_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _NoRedirectHandler(),
)


def urlopen_direct(url: str | urllib.request.Request, *, timeout: float | None = None) -> Any:
    """直连且禁止重定向的 ``urlopen``（本模块 HTTP 唯一出口，测试可 monkeypatch）。"""
    return _DIRECT_NO_REDIRECT_OPENER.open(url, timeout=timeout)


def _error(
    code: AgentErrorCode, message: str, *, detail: dict[str, Any] | None = None
) -> dict[str, Any]:
    """构造契约形状的业务错误 dict（不含外层 ``{"error": ...}`` 包裹）。"""
    spec = ERROR_SPECS.get(code)
    body: dict[str, Any] = {
        "code": code.value,
        "message": message,
        "retryable": spec.retryable if spec else False,
    }
    if detail:
        body["detail"] = detail
    return body


@dataclass(frozen=True)
class _Route:
    """工具 → HTTP 端点映射。``path`` 中 ``{instanceId}``/``{operationId}`` 由入参填充。"""

    method: str
    path: str
    query: tuple[str, ...] = ()
    body: bool = False
    timeout: float = _DEFAULT_TIMEOUT


_ROUTES: dict[str, _Route] = {
    "lwa_get_capabilities": _Route("GET", "/capabilities"),
    "lwa_list_instances": _Route("GET", "/instances", query=("cursor", "limit")),
    "lwa_get_instance": _Route("GET", "/instances/{instanceId}"),
    "lwa_get_access_urls": _Route(
        "GET", "/instances/{instanceId}/access-urls", query=("perspective",)
    ),
    "lwa_get_logs": _Route(
        "GET",
        "/logs",
        query=("instanceId", "operationId", "category", "cursor", "limit"),
    ),
    "lwa_plan_deployment": _Route("POST", "/plans", body=True, timeout=_PLAN_TIMEOUT),
    "lwa_apply_deployment": _Route("POST", "/deployments", body=True),
    "lwa_start_instance": _Route("POST", "/instances/{instanceId}/start", body=True),
    "lwa_stop_instance": _Route("POST", "/instances/{instanceId}/stop", body=True),
    "lwa_restart_instance": _Route("POST", "/instances/{instanceId}/restart", body=True),
    "lwa_rebuild_instance": _Route("POST", "/instances/{instanceId}/rebuild", body=True),
    "lwa_get_operation": _Route("GET", "/operations/{operationId}"),
    "lwa_cancel_operation": _Route("POST", "/operations/{operationId}/cancel", body=True),
}


class AgentBridge:
    """MCP 工具调用 → 本机 manager Agent HTTP API 的桥（每调用一次性、无连接缓存）。"""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self._config_cache: Any = None

    # ---- 工作区 / manager 状态 ------------------------------------------------

    def _config(self) -> Any:
        if self._config_cache is None:
            from local_webpage_access.config import load_config

            self._config_cache = load_config(self.workspace)
        return self._config_cache

    def _loopback_base_url(self) -> tuple[str | None, dict[str, Any] | None]:
        """回环基址；manager 只绑 LAN 地址时返回 ``manager_unavailable`` 错误。"""
        from local_webpage_access.manager_service import _health_check_host
        from local_webpage_access.ports import format_http_host

        config = self._config()
        probe_host = _health_check_host(config.managerHost)
        if probe_host not in _LOOPBACK_HOSTS:
            return None, _error(
                AgentErrorCode.manager_unavailable,
                "manager 只绑定了 LAN 地址"
                f"（managerHost={config.managerHost}），本机 stdio 模式需要回环可达；"
                "出于凭据安全不会把 owner token 发往明文 LAN 地址。请把 managerHost 改回 "
                "0.0.0.0/127.0.0.1 后 `lwa manager off && lwa manager on`",
                detail={"managerHost": config.managerHost},
            )
        return f"http://{format_http_host(probe_host)}:{config.managerPort}", None

    def _local_workspace_id(self) -> str | None:
        """本工作区 registry 的 workspaceId；只读打开，读不到（缺库/缺行）返回 None。"""
        if not self.workspace.db_path.is_file():
            return None
        from local_webpage_access.registry import Registry

        reg = Registry(self.workspace.db_path)
        try:
            reg.open_readonly()
        except LwaError:
            return None
        try:
            row = reg.conn.execute(
                "SELECT value FROM workspace_meta WHERE key = 'workspace_id'"
            ).fetchone()
            return str(row[0]) if row else None
        except (sqlite3.Error, LwaError):
            return None
        finally:
            reg.close()

    def _check_workspace_id(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """capabilities 的 workspaceId 与本工作区比对；不一致返回错误，一致/无从比对返回 None。"""
        remote = payload.get("workspaceId")
        local = self._local_workspace_id()
        if not remote or local is None:
            return None
        if str(remote) != local:
            return _error(
                AgentErrorCode.permission_denied,
                "manager 返回的 workspaceId 与本工作区 registry 不一致——可能连到了"
                "其他工作区的 manager，已拒绝继续；请核对 --workspace 指向的工作区",
                detail={"remoteWorkspaceId": str(remote), "localWorkspaceId": local},
            )
        return None

    # ---- HTTP -----------------------------------------------------------------

    def _map_http_error(self, exc: urllib.error.HTTPError) -> dict[str, Any]:
        """HTTP 错误 → 契约错误 dict：优先透传服务端 ``{"error": {...}}`` 体。"""
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            body = None
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict) and err.get("code") in {c.value for c in AgentErrorCode}:
                return dict(err)
        # 非契约错误体（含被拒的 3xx 重定向、5xx 网关页等）按状态码归类
        if exc.code in (401,):
            code = AgentErrorCode.unauthenticated
        elif exc.code in (403,):
            code = AgentErrorCode.permission_denied
        elif exc.code == 429:
            code = AgentErrorCode.busy
        elif exc.code >= 500:
            code = AgentErrorCode.manager_unavailable
        else:
            code = AgentErrorCode.needs_input
        return _error(code, f"manager HTTP {exc.code}: {exc.reason}")

    # ---- 入口 -----------------------------------------------------------------

    def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """执行工具调用：成功返回 ``(结果dict, None)``，失败返回 ``(None, 错误dict)``。"""
        from local_webpage_access.manager_api import read_token
        from local_webpage_access.manager_service import is_running

        spec = TOOL_SPECS.get(name)
        route = _ROUTES.get(name)
        if spec is None or route is None:
            return None, _error(
                AgentErrorCode.needs_input,
                f"未知工具: {name!r}",
                detail={"knownTools": sorted(TOOL_SPECS)},
            )
        try:
            parsed = spec.input_model.model_validate(arguments or {})
        except ValidationError as exc:
            return None, _error(
                AgentErrorCode.needs_input,
                "工具参数不符合契约",
                detail={"issues": exc.errors(include_url=False)},
            )
        payload = parsed.model_dump(mode="json")

        base_url, base_error = self._loopback_base_url()
        if base_error is not None:
            return None, base_error
        assert base_url is not None

        config = self._config()
        if not is_running(self.workspace, config):
            return None, _error(
                AgentErrorCode.manager_unavailable,
                "manager 不在线：请在工作区执行 `lwa manager on` 启动后重试"
                "（mcp 适配器不会隐式初始化或启动任何服务）",
            )
        token = read_token(self.workspace)
        if not token:
            return None, _error(
                AgentErrorCode.manager_unavailable,
                "manager token 未颁发或不可读；请执行 `lwa manager off && lwa manager on` 恢复",
            )

        path = route.path
        for key in ("instanceId", "operationId"):
            if "{" + key + "}" in path:
                path = path.replace("{" + key + "}", quote(str(payload[key]), safe=""))
        query_items = [(k, payload[k]) for k in route.query if payload.get(k) is not None]
        url = base_url + AGENT_API_PREFIX + path
        if query_items:
            url += "?" + urlencode(query_items)

        data: bytes | None = None
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        if route.body:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=route.method)

        try:
            with urlopen_direct(request, timeout=route.timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return None, self._map_http_error(exc)
        except (OSError, urllib.error.URLError, ValueError) as exc:
            return None, _error(
                AgentErrorCode.manager_unavailable,
                f"manager 请求失败（{type(exc).__name__}: {exc}）；请确认 `lwa manager status`",
            )
        if not isinstance(result, dict):
            return None, _error(
                AgentErrorCode.manager_unavailable,
                "manager 返回了非 JSON 对象的响应",
            )
        if name == "lwa_get_capabilities":
            mismatch = self._check_workspace_id(result)
            if mismatch is not None:
                return None, mismatch
        return result, None
