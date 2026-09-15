"""Agent 发现入口测试（AGC-W03，M0）。

验收场景（设计 §9.2 W03）：
1. `/llms.txt`、`/agent-info.json`、`/agent-guide` 返回正确媒体类型；
2. 秘密零暴露：token、工作区绝对路径、实例 ID、用户名哨兵不出现在任何发现响应；
3. SPA catch-all 不吞发现路由（静态目录存在时 StaticFiles 挂载 "/" 也不影响）；
4. 未实现的 Agent API 返回 404，不被 SPA 吞成 HTML。

泄漏判定（BUG-652 修正后）为两级：响应与静态模板**共源相等**（markdown 逐字节、
JSON 结构）是强保证；敏感值子串断言只对不与静态文案合法重合的值生效——真实
用户名可能与固定文案重合（如 ``lwa`` 命中 llms.txt 的 "CLI：lwa"），故用户名
改用每测试唯一的哨兵值注入。
"""

from __future__ import annotations

import getpass
import json
import uuid
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from local_webpage_access.config import Config, PortPool
from local_webpage_access.manager_api import create_app, ensure_token
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry

LLMS_TXT_PATH = "/llms.txt"
AGENT_INFO_PATH = "/agent-info.json"
GUIDE_PATH = "/agent-guide"
DISCOVERY_PATHS = [LLMS_TXT_PATH, AGENT_INFO_PATH, GUIDE_PATH]


@pytest.fixture()
def discovery_env(workspace_root: Path):
    """带静态实例 + token 的 manager 环境（发现端点为公开路由，无需凭据）。"""
    from local_webpage_access.config import example_config_text

    ws = Workspace(workspace_root)
    ws.ensure_workspace_dirs()
    if not ws.config_path.is_file():
        ws.config_path.write_text(example_config_text(), encoding="utf-8")
    config = Config(staticGateway="builtin", portPool=PortPool(start=22000, end=22050))

    reg = Registry(ws.db_path)
    reg.open()

    from local_webpage_access.importer import Importer

    zip_path = ws.inbox / "static.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("index.html", "<h1>agent-discovery</h1>")
    importer = Importer(ws, config, reg)
    instance_id = importer.import_zip(str(zip_path)).instance_id

    token = ensure_token(ws)
    app = create_app(ws, config, reg, token=token)
    with TestClient(app) as client:
        yield {"ws": ws, "client": client, "token": token, "instance_id": instance_id}


# ---- 1. 媒体类型与内容形状 ------------------------------------------------------


def test_llms_txt_is_markdown_with_guide_link(discovery_env) -> None:
    resp = discovery_env["client"].get("/llms.txt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    body = resp.text
    assert body.startswith("# "), "llms.txt 应以 H1 标题开头"
    assert "(/agent-guide)" in body, "llms.txt 应链接指南路径"
    assert "http://" not in body and "https://" not in body, "只允许相对链接，不生成主机绝对地址"


def test_agent_info_json_fields(discovery_env) -> None:
    resp = discovery_env["client"].get("/agent-info.json")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    data = resp.json()
    assert data["product"] == "local-webpage-access"
    assert data["discoveryVersion"] == "1"
    assert data["guide"] == "/agent-guide"
    assert data["mcp"]["enabled"] is False, "MCP 未开放时必须如实返回 false"
    assert data["authentication"]["required"] is True
    assert isinstance(data["authentication"]["contact"], str) and data["authentication"]["contact"]


def test_agent_guide_served_as_markdown(discovery_env) -> None:
    resp = discovery_env["client"].get("/agent-guide")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "token" in resp.text.lower(), "指南应包含鉴权说明"
    assert len(resp.text) > 200, "包内指南不应为空壳"


# ---- 2. 秘密零暴露 --------------------------------------------------------------


def _expected_discovery_content() -> dict[str, str]:
    """三个发现端点的期望静态内容（与实现共源生成，作为泄漏判定基准）。"""
    from local_webpage_access.agent.discovery import (
        build_agent_info,
        build_llms_txt,
        load_guide_markdown,
    )

    return {
        LLMS_TXT_PATH: build_llms_txt(),
        AGENT_INFO_PATH: json.dumps(build_agent_info(), ensure_ascii=False),
        GUIDE_PATH: load_guide_markdown(),
    }


def _assert_discovery_leaks_nothing(client: TestClient, secrets: list[str]) -> None:
    """两级泄漏判定（BUG-652）。

    1. 共源相等：响应必须与静态模板一致（markdown 逐字节、JSON 结构）——
       任何动态数据（token/路径/用户名）混入都会使响应偏离模板；
    2. 子串断言：仅对不与静态文案合法重合的敏感值生效，防止如真实用户名
       ``lwa`` 命中固定文案 "CLI：lwa" 的误报。
    """
    expected = _expected_discovery_content()
    for path in DISCOVERY_PATHS:
        body = client.get(path).text
        if path == AGENT_INFO_PATH:
            assert json.loads(body) == json.loads(expected[path]), f"{path} 偏离静态模板"
        else:
            assert body == expected[path], f"{path} 偏离静态模板"
    for path in DISCOVERY_PATHS:
        body = client.get(path).text
        for secret in secrets:
            if not secret:
                continue
            if any(secret in exp for exp in expected.values()):
                continue  # 与静态文案合法重合，由第 1 级共源相等兜底
            assert secret not in body, f"{path} 泄漏敏感值：{secret[:24]}…"


def test_discovery_responses_leak_no_secrets(discovery_env, monkeypatch) -> None:
    """用户名用唯一哨兵注入（BUG-652）：若实现改为读取 getuser，哨兵即泄漏被抓。"""
    sentinel_user = f"lwa-sentinel-{uuid.uuid4().hex[:12]}"
    monkeypatch.setattr(getpass, "getuser", lambda: sentinel_user)
    _assert_discovery_leaks_nothing(
        discovery_env["client"],
        [
            sentinel_user,
            discovery_env["token"],
            str(discovery_env["ws"].root.resolve()),
            str(discovery_env["ws"].root),
            discovery_env["instance_id"],
        ],
    )


def test_secrecy_no_false_positive_on_colliding_username(discovery_env, monkeypatch) -> None:
    """BUG-652 回归：用户名与静态文案重合（评审复现值 ``lwa``）时不得误报泄漏。"""
    monkeypatch.setattr(getpass, "getuser", lambda: "lwa")
    _assert_discovery_leaks_nothing(
        discovery_env["client"],
        ["lwa", discovery_env["token"], str(discovery_env["ws"].root.resolve())],
    )


# ---- 3. 公开可达 + SPA 不吞路由 --------------------------------------------------


@pytest.mark.parametrize("path", DISCOVERY_PATHS)
def test_discovery_public_without_token(discovery_env, path: str) -> None:
    """TestClient 源地址非 loopback，等价 LAN 客户端：发现端点仍应公开可达。"""
    resp = discovery_env["client"].get(path)  # 不带任何凭据
    assert resp.status_code == 200
    assert not resp.headers["content-type"].startswith("text/html"), f"{path} 被 SPA 吞掉"


def test_spa_still_serves_root(discovery_env) -> None:
    """发现路由不影响既有静态首页（SPA 正常路径不被破坏）。"""
    resp = discovery_env["client"].get("/")
    assert resp.status_code == 200


def test_unimplemented_agent_api_returns_404(discovery_env) -> None:
    """/api/agent/v1 尚未实现（M1）：应 404，不得回退成 SPA HTML 造成假路由。"""
    resp = discovery_env["client"].get("/api/agent/v1/capabilities")
    assert resp.status_code == 404
    assert not resp.headers["content-type"].startswith("text/html")
