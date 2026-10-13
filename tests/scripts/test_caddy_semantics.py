"""Caddyfile 语义测试（PLN-052 S0-02 / CHK-373 P-1）。

两部分：

- 模拟器单测：手写 adapt 后的 JSON，验证 :mod:`tests.scripts._caddy` 的路由模拟语义，
  不需要 caddy，任何环境都会运行；
- 渲染语义测试：把 :class:`StaticGateway` 生成的主 Caddyfile 与别名片段交给
  真实 ``caddy adapt``，再断言路由语义。缺 caddy 时跳过（CI 安装 caddy 后启用）。

N1（别名无斜杠访问返回 200 空体）已由 S1-01 修复（``redir * /<alias>/ permanent``），
对应用例为普通断言；``test_adapt_redir_path_first_arg_is_matcher_n1_shape`` 固化 Caddy 的解析行为。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_webpage_access.config import Config
from local_webpage_access.paths import Workspace
from local_webpage_access.static_gateway import StaticGateway
from tests.scripts._caddy import (
    UnsupportedCaddyConfig,
    adapt_caddyfile,
    adapt_caddyfile_text,
    caddy_path_match,
    requires_caddy,
    simulate_request,
)

# ---- 模拟器单测（无需 caddy）-------------------------------------------------


def _server(routes: list[dict], listen: str = ":8080") -> dict:
    return {"apps": {"http": {"servers": {"srv0": {"listen": [listen], "routes": routes}}}}}


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("/demo", "/demo", True),
        ("/demo", "/demo/", False),
        ("/demo/*", "/demo/", True),
        ("/demo/*", "/demo/a/b.js", True),
        ("/demo/*", "/demo", False),
        ("/DEMO/*", "/demo/x", True),
        ("*.js", "/a/b.js", True),
        ("*api*", "/x/api/y", True),
        ("/a/*/c", "/a/b/c", True),
        ("/a/*/c", "/a/b/x/c", False),
        ("*", "/anything", True),
    ],
)
def test_caddy_path_match_semantics(pattern: str, path: str, expected: bool) -> None:
    assert caddy_path_match(pattern, path) is expected


def test_simulate_group_is_mutually_exclusive_and_falls_through_to_empty() -> None:
    """同组路由互斥：命中外层但内层落空时，兜底 404 不再执行 → 200 空体（N1 形态）。"""
    config = _server(
        [
            {
                "group": "g1",
                "match": [{"path": ["/demo"]}],
                "handle": [
                    {
                        "handler": "subroute",
                        "routes": [
                            {
                                "match": [{"path": ["/demo/"]}],
                                "handle": [{"handler": "static_response", "status_code": 302}],
                            },
                        ],
                    },
                ],
            },
            {"group": "g1", "handle": [{"handler": "static_response", "status_code": 404}]},
        ],
    )
    outcome = simulate_request(config, "/demo")
    assert outcome.kind == "empty"
    assert outcome.status == 200
    assert simulate_request(config, "/other").status == 404


def test_simulate_strip_prefix_then_reverse_proxy() -> None:
    config = _server(
        [
            {
                "match": [{"path": ["/demo/*"]}],
                "handle": [
                    {
                        "handler": "subroute",
                        "routes": [
                            {"handle": [{"handler": "rewrite", "strip_path_prefix": "/demo"}]},
                            {
                                "handle": [
                                    {
                                        "handler": "reverse_proxy",
                                        "upstreams": [{"dial": "127.0.0.1:18000"}],
                                    },
                                ],
                            },
                        ],
                    },
                ],
            },
        ],
    )
    outcome = simulate_request(config, "/demo/assets/app.js")
    assert outcome.kind == "reverse_proxy"
    assert outcome.upstreams == ("127.0.0.1:18000",)
    assert outcome.path == "/assets/app.js"
    assert simulate_request(config, "/demo/").path == "/"


def test_simulate_terminal_route_stops_following_routes() -> None:
    config = _server(
        [
            {"match": [{"host": ["a.local"]}], "terminal": True, "handle": []},
            {"handle": [{"handler": "static_response", "status_code": 404}]},
        ],
    )
    assert simulate_request(config, "/", host="a.local").kind == "empty"
    assert simulate_request(config, "/", host="b.local").status == 404


def test_simulate_selects_server_by_port() -> None:
    config = {
        "apps": {
            "http": {
                "servers": {
                    "srv0": {
                        "listen": [":8443"],
                        "routes": [{"handle": [{"handler": "static_response", "status_code": 404}]}],
                    },
                    "srv1": {
                        "listen": [":9443"],
                        "routes": [
                            {
                                "handle": [
                                    {
                                        "handler": "reverse_proxy",
                                        "upstreams": [{"dial": "127.0.0.1:17800"}],
                                    },
                                ],
                            },
                        ],
                    },
                },
            },
        },
    }
    assert simulate_request(config, "/", port=8443).status == 404
    assert simulate_request(config, "/", port=9443).upstreams == ("127.0.0.1:17800",)
    with pytest.raises(UnsupportedCaddyConfig):
        simulate_request(config, "/")


def test_simulate_rejects_unknown_matcher_instead_of_passing() -> None:
    config = _server(
        [
            {
                "match": [{"path_regexp": {"pattern": "^/x"}}],
                "handle": [{"handler": "static_response", "status_code": 200}],
            },
        ],
    )
    with pytest.raises(UnsupportedCaddyConfig):
        simulate_request(config, "/x")


# ---- 真实 caddy adapt：辅助自检 -----------------------------------------------


@requires_caddy
def test_adapt_redir_wildcard_permanent_is_301(tmp_path: Path) -> None:
    """正确写法 ``redir * /demo/ permanent``：301 到 ``/demo/``。"""
    config = adapt_caddyfile_text(
        ":8080 {\n\thandle /demo {\n\t\tredir * /demo/ permanent\n\t}\n"
        "\thandle {\n\t\trespond 404\n\t}\n}\n",
        tmp_path,
    )
    outcome = simulate_request(config, "/demo")
    assert (outcome.kind, outcome.status, outcome.location) == ("static_response", 301, "/demo/")


@requires_caddy
def test_adapt_redir_path_first_arg_is_matcher_n1_shape(tmp_path: Path) -> None:
    """CHK-373 N1 根因：``redir /demo/ permanent`` 的首参被解析成路径匹配器。

    这是对 Caddy 解析行为本身的固化（与 LWA 代码无关），用来证明模拟器能复现
    本机 runtime 实测到的 ``200 0B``。
    """
    config = adapt_caddyfile_text(
        ":8080 {\n\thandle /demo {\n\t\tredir /demo/ permanent\n\t}\n"
        "\thandle {\n\t\trespond 404\n\t}\n}\n",
        tmp_path,
    )
    outcome = simulate_request(config, "/demo")
    assert (outcome.kind, outcome.status) == ("empty", 200)


# ---- 真实 caddy adapt：LWA 渲染结果 --------------------------------------------


@pytest.fixture()
def gw_workspace(workspace_root: Path) -> Workspace:
    ws = Workspace(workspace_root)
    ws.ensure_workspace_dirs()
    return ws


def _render_main(ws: Workspace, config: Config, aliases: dict[str, int]) -> Path:
    gw = StaticGateway(ws, config)
    for alias, port in aliases.items():
        gw.generate_alias_config(f"inst-{alias}", alias, port)
    main = gw.main_config_path()
    main.parent.mkdir(parents=True, exist_ok=True)
    # 直接组装而非 write_main_config：后者在本机无 caddy 时按 builtin 早退
    main.write_text(gw._assemble_main_config(), encoding="utf-8")
    return main


@requires_caddy
def test_plain_gateway_alias_routes_semantics(gw_workspace: Workspace) -> None:
    config = Config(staticGateway="caddy", staticGatewayPort=8080)
    main = _render_main(gw_workspace, config, {"demo": 18000, "other-app": 18001})
    adapted = adapt_caddyfile(main)

    asset = simulate_request(adapted, "/demo/assets/app.js", port=8080)
    assert asset.kind == "reverse_proxy"
    assert asset.upstreams == ("127.0.0.1:18000",)
    assert asset.path == "/assets/app.js"

    index = simulate_request(adapted, "/other-app/", port=8080)
    assert index.upstreams == ("127.0.0.1:18001",)
    assert index.path == "/"

    # issue #21：未命中别名显式 404，不是静默空 200
    missing = simulate_request(adapted, "/nonexist", port=8080)
    assert (missing.kind, missing.status) == ("static_response", 404)


@requires_caddy
def test_plain_gateway_alias_without_trailing_slash_redirects(gw_workspace: Workspace) -> None:
    config = Config(staticGateway="caddy", staticGatewayPort=8080)
    main = _render_main(gw_workspace, config, {"demo": 18000})
    outcome = simulate_request(adapt_caddyfile(main), "/demo", port=8080)
    assert (outcome.kind, outcome.status, outcome.location) == ("static_response", 301, "/demo/")


@requires_caddy
def test_tls_gateway_alias_and_manager_origin_semantics(gw_workspace: Workspace) -> None:
    config = Config(
        staticGateway="caddy",
        gatewayTls="internal",
        lanIpStrategy="manual",
        manualLanIp="192.168.1.10",
    )
    main = _render_main(gw_workspace, config, {"demo": 18000})
    adapted = adapt_caddyfile(main)

    tls_port = config.gatewayTlsPort
    asset = simulate_request(adapted, "/demo/x.css", port=tls_port, host="192.168.1.10")
    assert asset.upstreams == ("127.0.0.1:18000",)
    assert asset.path == "/x.css"
    missing = simulate_request(adapted, "/nonexist", port=tls_port, host="127.0.0.1")
    assert missing.status == 404

    manager = simulate_request(adapted, "/", port=config.managerTlsPort, host="127.0.0.1")
    assert manager.kind == "reverse_proxy"
    assert manager.upstreams == (f"127.0.0.1:{config.managerPort}",)
