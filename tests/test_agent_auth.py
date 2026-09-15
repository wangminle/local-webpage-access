"""Agent 权限测试（AGC-W06，M1）。

验收场景（设计 §9.2 W06）：
1. 回环仍需凭据：Agent 通道不继承回环免 token（§7.3）；
2. LAN 拒绝：M1 仅允许本机接入，非回环客户端直接拒绝；
3. 越界源拒绝：server_directory 必须落在 allowedSourceRoots 真实路径内，
   相对路径与越界符号链接均拒绝；
4. 轮换：token 轮换后旧凭据立即失效（每次请求读盘语义）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.requests import Request

from local_webpage_access.config import Config
from local_webpage_access.manager_api import ensure_token, rotate_token
from local_webpage_access.paths import Workspace


def _request(
    host: str = "127.0.0.1",
    headers: dict[str, str] | None = None,
    method: str = "POST",
    query: str = "",
) -> Request:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": "/api/agent/v1/x",
        "raw_path": b"/api/agent/v1/x",
        "query_string": query.encode(),
        "headers": [
            (k.lower().encode("utf-8"), v.encode("utf-8")) for k, v in (headers or {}).items()
        ],
        "client": (host, 54321),
        "server": ("127.0.0.1", 17800),
    }
    return Request(scope)


@pytest.fixture()
def tokened_workspace(workspace_root: Path) -> Workspace:
    ws = Workspace(workspace_root)
    ws.ensure_workspace_dirs()
    ensure_token(ws)
    return ws


# ---- 1/2/4. 认证：回环仍需凭据、LAN 拒绝、轮换 ----------------------------------------


def test_loopback_requires_credentials(tokened_workspace: Workspace) -> None:
    """回环客户端无凭据也必须 401 语义（不继承管理页回环免 token）。"""
    from local_webpage_access.agent.auth import AgentAuthError, authenticate_local_owner

    with pytest.raises(AgentAuthError) as exc:
        authenticate_local_owner(_request(), tokened_workspace)
    assert exc.value.code == "unauthenticated"


def test_bearer_and_x_token_accepted(tokened_workspace: Workspace) -> None:
    from local_webpage_access.manager_api import read_token

    from local_webpage_access.agent.auth import authenticate_local_owner

    token = read_token(tokened_workspace)
    assert token
    via_bearer = authenticate_local_owner(
        _request(headers={"Authorization": f"Bearer {token}"}), tokened_workspace
    )
    via_header = authenticate_local_owner(
        _request(headers={"X-LWA-Token": token}), tokened_workspace
    )
    assert via_bearer.principal_id == via_header.principal_id == "local-owner"
    assert via_bearer.kind == "local_owner"
    assert "deploy:create" in via_bearer.scopes and "logs:read" in via_bearer.scopes


def test_query_token_not_accepted(tokened_workspace: Workspace) -> None:
    """§7.3：凭据只放 Header；``?token=`` 便利通道对 Agent API 不生效。"""
    from local_webpage_access.manager_api import read_token

    from local_webpage_access.agent.auth import AgentAuthError, authenticate_local_owner

    token = read_token(tokened_workspace)
    assert token
    with pytest.raises(AgentAuthError) as exc:
        authenticate_local_owner(
            _request(query=f"token={token}"), tokened_workspace
        )
    assert exc.value.code == "unauthenticated"


def test_lan_client_rejected_even_with_valid_token(tokened_workspace: Workspace) -> None:
    """M1 只允许本机接入：持有有效 token 的局域网客户端也被拒（permission_denied）。"""
    from local_webpage_access.manager_api import read_token

    from local_webpage_access.agent.auth import AgentAuthError, authenticate_local_owner

    token = read_token(tokened_workspace)
    assert token
    with pytest.raises(AgentAuthError) as exc:
        authenticate_local_owner(
            _request(host="192.168.1.23", headers={"Authorization": f"Bearer {token}"}),
            tokened_workspace,
        )
    assert exc.value.code == "permission_denied"


def test_dns_rebinding_host_rejected(tokened_workspace: Workspace) -> None:
    """回环源 + 非本机 Host 头（rebinding 场景）不算本机客户端。"""
    from local_webpage_access.manager_api import read_token

    from local_webpage_access.agent.auth import AgentAuthError, authenticate_local_owner

    token = read_token(tokened_workspace)
    assert token
    with pytest.raises(AgentAuthError) as exc:
        authenticate_local_owner(
            _request(headers={"Authorization": f"Bearer {token}", "Host": "evil.example"}),
            tokened_workspace,
        )
    assert exc.value.code == "permission_denied"


def test_token_rotation_invalidates_old(tokened_workspace: Workspace) -> None:
    from local_webpage_access.manager_api import read_token

    from local_webpage_access.agent.auth import AgentAuthError, authenticate_local_owner

    old = read_token(tokened_workspace)
    assert old
    new = rotate_token(tokened_workspace)
    assert new != old
    with pytest.raises(AgentAuthError) as exc:
        authenticate_local_owner(
            _request(headers={"Authorization": f"Bearer {old}"}), tokened_workspace
        )
    assert exc.value.code == "unauthenticated"
    principal = authenticate_local_owner(
        _request(headers={"Authorization": f"Bearer {new}"}), tokened_workspace
    )
    assert principal.principal_id == "local-owner"


def test_wrong_token_rejected(tokened_workspace: Workspace) -> None:
    from local_webpage_access.agent.auth import AgentAuthError, authenticate_local_owner

    with pytest.raises(AgentAuthError) as exc:
        authenticate_local_owner(
            _request(headers={"Authorization": "Bearer not-the-token"}), tokened_workspace
        )
    assert exc.value.code == "unauthenticated"


# ---- 3. 源根校验 ------------------------------------------------------------------


def test_source_root_inside_allowed(tmp_path: Path) -> None:
    from local_webpage_access.agent.auth import validate_source_root

    roots = [tmp_path / "projects"]
    roots[0].mkdir()
    src = roots[0] / "site"
    src.mkdir()
    assert validate_source_root(str(src), roots) == src.resolve()


def test_source_root_outside_rejected(tmp_path: Path) -> None:
    from local_webpage_access.agent.auth import AgentAuthError, validate_source_root

    roots = [tmp_path / "projects"]
    roots[0].mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(AgentAuthError) as exc:
        validate_source_root(str(outside), roots)
    assert exc.value.code == "source_not_allowed"


def test_source_root_relative_path_rejected(tmp_path: Path) -> None:
    from local_webpage_access.agent.auth import AgentAuthError, validate_source_root

    with pytest.raises(AgentAuthError):
        validate_source_root("projects/site", [tmp_path])


def test_empty_roots_reject_everything(tmp_path: Path) -> None:
    """安全默认：未配置 allowedSourceRoots 时 server_directory 全部拒绝。"""
    from local_webpage_access.agent.auth import AgentAuthError, validate_source_root

    with pytest.raises(AgentAuthError):
        validate_source_root(str(tmp_path), [])


def test_symlink_escape_rejected(tmp_path: Path) -> None:
    """根内符号链接指向根外：按真实路径判定，拒绝（§6.2）。"""
    from local_webpage_access.agent.auth import AgentAuthError, validate_source_root

    root = tmp_path / "projects"
    root.mkdir()
    secret = tmp_path / "secret"
    secret.mkdir()
    link = root / "shortcut"
    link.symlink_to(secret, target_is_directory=True)
    with pytest.raises(AgentAuthError):
        validate_source_root(str(link), [root])


def test_symlink_still_inside_allowed(tmp_path: Path) -> None:
    from local_webpage_access.agent.auth import validate_source_root

    root = tmp_path / "projects"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    link = root / "a" / "to-b"
    link.symlink_to(root / "b", target_is_directory=True)
    assert validate_source_root(str(link), [root]) == (root / "b").resolve()


# ---- allowedSourceRoots 配置面 -----------------------------------------------------


def test_config_agent_section_parses(tmp_path: Path) -> None:
    cfg = Config.model_validate(
        {"agent": {"allowedSourceRoots": [str(tmp_path / "src")]}}
    )
    assert cfg.agent.allowedSourceRoots == [tmp_path / "src"]


def test_config_rejects_relative_source_root() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Config.model_validate({"agent": {"allowedSourceRoots": ["relative/dir"]}})


def test_config_agent_section_optional() -> None:
    cfg = Config.model_validate({})
    assert cfg.agent.allowedSourceRoots == []


def test_config_with_source_roots_saves_and_reloads(tmp_path: Path, workspace: Workspace) -> None:
    """BUG-653：allowedSourceRoots 为 Path 时 to_yaml/save 必须可序列化并 roundtrip。"""
    from local_webpage_access.config import load_config

    root = tmp_path / "projects"
    root.mkdir()
    cfg = Config.model_validate({"agent": {"allowedSourceRoots": [str(root)]}})
    cfg.save(workspace.config_path)
    loaded = load_config(workspace)
    assert loaded.agent.allowedSourceRoots == [root]


def test_ipv6_bracketed_loopback_host_accepted(tokened_workspace: Workspace) -> None:
    """BUG-657：标准 IPv6 回环 Host: [::1]:17800 必须视为本机客户端。"""
    from local_webpage_access.manager_api import read_token

    from local_webpage_access.agent.auth import authenticate_local_owner

    token = read_token(tokened_workspace)
    assert token
    principal = authenticate_local_owner(
        _request(
            host="::1",
            headers={"Authorization": f"Bearer {token}", "Host": "[::1]:17800"},
        ),
        tokened_workspace,
    )
    assert principal.principal_id == "local-owner"
