"""Agent 权限：本机 owner 认证与源根校验（AGC-W06，M1）。

M1 权限模型（设计 §7.2/§7.3）：

- 唯一主体是**本机 owner**——持有该工作区既有管理 token 的本机回环客户端；
- Agent 通道**不继承**管理页的回环免 token，也不接受 ``?token=`` 便利通道：
  凭据只放 ``Authorization: Bearer`` 或 ``X-LWA-Token`` 头；
- M1 网络门禁：非回环客户端一律 ``permission_denied``（远程主体属 M2）；
- token 每次请求读盘校验（复用 manager 的实现），轮换后旧凭据立即失效。

``server_directory`` 源必须落在管理员配置的 ``allowedSourceRoots`` 真实路径内
（``Path.resolve()`` 规范化，越界符号链接按真实路径判定拒绝）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from starlette.requests import Request

from local_webpage_access.errors import LwaError
from local_webpage_access.paths import Workspace

#: M1 本机 owner 的固定主体标识（M2 引入独立 Agent 身份后扩展）
LOCAL_OWNER_PRINCIPAL_ID = "local-owner"

#: M1 授权 scope 集（设计 §7.2；artifacts:write 属 M2，不在内）
M1_LOCAL_OWNER_SCOPES = frozenset(
    {
        "instances:read",
        "deploy:create",
        "deploy:update",
        "instances:operate",
        "logs:read",
    }
)


class AgentAuthError(LwaError):
    """Agent 通道鉴权失败；``code`` 取 Agent 契约错误码（unauthenticated 等）。"""


@dataclass(frozen=True)
class Principal:
    """已认证主体。clientInfo/name 只作诊断，不作可信身份（§7.2）。"""

    principal_id: str
    kind: str
    scopes: frozenset[str]


def _extract_agent_credentials(request: Request) -> str | None:
    """仅接受 Header 凭据（§7.3）：Bearer 或 X-LWA-Token；无 ``?token=`` 通道。"""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        candidate = auth[7:].strip()
        return candidate or None
    x_token = request.headers.get("x-lwa-token", "").strip()
    return x_token or None


def authenticate_local_owner(request: Request, workspace: Workspace) -> Principal:
    """认证本机 owner：网络门禁 + 凭据校验。

    - 非回环客户端（含 rebinding 场景的非常规 Host）→ ``permission_denied``；
    - 回环客户端必须携带有效管理 token → 失败 ``unauthenticated``。
    """
    from local_webpage_access.manager_api import (
        _host_header_is_local,
        _is_loopback_host,
        _verify_token,
    )

    client = request.client
    client_host = client.host if client is not None else ""
    is_local = _is_loopback_host(client_host) and _host_header_is_local(request)
    if not is_local:
        raise AgentAuthError(
            "M1 阶段 Agent 通道仅允许本机接入；远程主体将在 M2 开放",
            code="permission_denied",
        )
    candidate = _extract_agent_credentials(request)
    if not _verify_token(workspace, candidate):
        raise AgentAuthError(
            "Agent 通道凭据无效或缺失（回环不免 token；仅 Authorization/X-LWA-Token 头）",
            code="unauthenticated",
        )
    return Principal(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        kind="local_owner",
        scopes=M1_LOCAL_OWNER_SCOPES,
    )


def validate_source_root(path: str, allowed_roots: Sequence[Path]) -> Path:
    """校验 ``server_directory`` 源路径并返回规范化真实路径。

    规则（设计 §6.2）：必须绝对路径；真实路径（``resolve`` 跟随符号链接后）
    必须位于某个 allowedRoot 之内；空 allowedRoots 一律拒绝（安全默认）。
    """
    candidate = Path(path)
    if not candidate.is_absolute():
        raise AgentAuthError(
            f"server_directory 路径必须是绝对路径: {path!r}",
            code="source_not_allowed",
        )
    real = candidate.resolve()
    for root in allowed_roots:
        root_real = Path(root).resolve()
        if real == root_real or root_real in real.parents:
            return real
    raise AgentAuthError(
        "server_directory 路径不在 allowedSourceRoots 允许范围内",
        code="source_not_allowed",
        path=path,
    )
