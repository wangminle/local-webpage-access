"""Caddy 语义校验测试辅助（PLN-052 S0-02 / CHK-373 P-1）。

背景：网关相关测试此前只做 ``"…" in text`` 字符串断言，N1（别名片段
``redir /<alias>/ permanent`` 被 Caddy 解析成路径匹配器）就是这样被测试
“锁成正确”的。本模块提供两层能力：

1. :func:`adapt_caddyfile` / :func:`adapt_caddyfile_text`：调用真实
   ``caddy adapt`` 把渲染出的 Caddyfile 转成 JSON 配置。PATH 上（或环境变量
   ``LWA_TEST_CADDY`` 指向的位置）没有 caddy 时 ``pytest.skip``，CI 中安装
   caddy 后自动启用。
2. :func:`simulate_request`：在 adapt 出的 JSON 上按 Caddy 路由语义做**最小
   模拟**，回答“某个请求路径最终由哪个 handler 响应”，供断言状态码、
   ``Location`` 与反代上游。只支持 LWA 生成物会用到的子集（``path``/``host``
   匹配器、``group`` 互斥、``subroute``、``rewrite.strip_path_prefix``、
   ``static_response``、``reverse_proxy``、``file_server``）；遇到不认识的
   匹配器直接报错，绝不静默放行——宁可让测试作者补模拟，也不制造假绿。

模拟器不发真实请求、不启动 Caddy、不碰 admin :2019（BUG-121）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

CADDY_ENV = "LWA_TEST_CADDY"
_ADAPT_TIMEOUT = 30


# ---- caddy 二进制定位与 adapt ---------------------------------------------


def caddy_binary() -> str | None:
    """返回用于 ``caddy adapt`` 的可执行文件路径；不可用时返回 ``None``。

    优先 ``LWA_TEST_CADDY``（只影响本辅助模块，不会让其他用例的
    ``detect_backend`` 改走 caddy 后端），其次 PATH 上的 ``caddy``。
    """
    override = os.environ.get(CADDY_ENV, "").strip()
    if override:
        return override if Path(override).is_file() else None
    return shutil.which("caddy")


requires_caddy = pytest.mark.skipif(
    caddy_binary() is None,
    reason=f"需要 caddy 可执行文件（PATH 或 {CADDY_ENV}）做 Caddyfile 语义校验",
)


def adapt_caddyfile(path: Path) -> dict[str, Any]:
    """对磁盘上的 Caddyfile 执行 ``caddy adapt``，返回 JSON 配置。

    adapt 失败（语法错误、import 目标缺失等）直接 ``pytest.fail`` 并附 stderr。
    """
    binary = caddy_binary()
    if binary is None:
        pytest.skip(f"需要 caddy 可执行文件（PATH 或 {CADDY_ENV}）")
    proc = subprocess.run(
        [binary, "adapt", "--config", str(path), "--adapter", "caddyfile"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_ADAPT_TIMEOUT,
        cwd=str(path.parent),
    )
    if proc.returncode != 0:
        pytest.fail(
            f"caddy adapt 失败（exit={proc.returncode}）：{path}\n"
            f"stderr:\n{proc.stderr.strip()}",
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - caddy 输出异常
        pytest.fail(f"caddy adapt 输出不是 JSON：{exc}\nstdout:\n{proc.stdout[:2000]}")


def adapt_caddyfile_text(text: str, directory: Path, name: str = "Caddyfile") -> dict[str, Any]:
    """把 Caddyfile 文本写入 ``directory/name`` 后 adapt。"""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return adapt_caddyfile(path)


# ---- 路由语义模拟 ----------------------------------------------------------


class UnsupportedCaddyConfig(AssertionError):
    """模拟器遇到不支持的匹配器/结构——需要补模拟，而不是忽略。"""


@dataclass(frozen=True)
class CaddyOutcome:
    """一次模拟请求的最终结果。

    ``kind``：
    - ``static_response``：``respond`` / ``redir`` 等，见 ``status``/``headers``；
    - ``reverse_proxy``：见 ``upstreams``，``path`` 为改写后转发给上游的路径；
    - ``file_server``：见 ``root``；
    - ``empty``：没有任何 handler 写响应——Caddy 此时返回 **200 空体**
      （N1 与 issue #21 的“静默空 200”即此类）。
    """

    kind: str
    status: int
    path: str
    headers: dict[str, list[str]] = field(default_factory=dict)
    upstreams: tuple[str, ...] = ()
    root: str | None = None
    server: str = ""

    @property
    def location(self) -> str | None:
        for key, values in self.headers.items():
            if key.lower() == "location" and values:
                return values[0]
        return None


@dataclass
class _Request:
    path: str
    host: str | None


_STOP = object()  # terminal 路由：本层后续路由不再执行，且尚无响应


def _port_of(listen: str) -> int | None:
    m = re.search(r":(\d+)(?:-\d+)?$", listen)
    return int(m.group(1)) if m else None


def _select_server(config: dict[str, Any], port: int | None) -> tuple[str, dict[str, Any]]:
    servers: dict[str, Any] = config.get("apps", {}).get("http", {}).get("servers", {}) or {}
    if not servers:
        raise UnsupportedCaddyConfig("配置中没有 apps.http.servers")
    if port is None:
        if len(servers) != 1:
            raise UnsupportedCaddyConfig(
                f"存在 {len(servers)} 个 server，必须用 port= 指定：{sorted(servers)}",
            )
        name = next(iter(servers))
        return name, servers[name]
    hits = [
        (name, srv)
        for name, srv in servers.items()
        if any(_port_of(str(addr)) == port for addr in srv.get("listen", []))
    ]
    if len(hits) != 1:
        raise UnsupportedCaddyConfig(f"端口 {port} 命中 {len(hits)} 个 server")
    return hits[0]


def caddy_path_match(pattern: str, path: str) -> bool:
    """Caddy ``path`` 匹配器语义（大小写不敏感）。

    - ``*``：任意；
    - ``*x*``：子串；``x*``：前缀；``*x``：后缀；
    - 中间含 ``*``：按段 glob，``*`` 不跨 ``/``；
    - 其余：精确匹配。
    """
    pat = pattern.lower()
    p = path.lower()
    if pat == "*":
        return True
    star_count = pat.count("*")
    if star_count == 0:
        return p == pat
    if pat.startswith("*") and pat.endswith("*") and star_count == 2:
        return pat[1:-1] in p
    if pat.endswith("*") and star_count == 1:
        return p.startswith(pat[:-1])
    if pat.startswith("*") and star_count == 1:
        return p.endswith(pat[1:])
    regex = "^" + "[^/]*".join(re.escape(part) for part in pat.split("*")) + "$"
    return re.match(regex, p) is not None


def _host_match(patterns: list[str], host: str | None) -> bool:
    if host is None:
        # 未指定 Host 时不按主机名过滤（调用方只关心路径语义）
        return True
    h = host.lower()
    for pat in patterns:
        pat_l = pat.lower()
        if pat_l == h:
            return True
        if pat_l.startswith("*.") and h.endswith(pat_l[1:]):
            return True
    return False


def _matcher_set_matches(matcher: dict[str, Any], req: _Request) -> bool:
    for key, value in matcher.items():
        if key == "path":
            if not any(caddy_path_match(str(pat), req.path) for pat in value):
                return False
        elif key == "host":
            if not _host_match([str(v) for v in value], req.host):
                return False
        else:
            raise UnsupportedCaddyConfig(f"模拟器不支持匹配器 {key!r}：{matcher}")
    return True


def _route_matches(route: dict[str, Any], req: _Request) -> bool:
    match_sets = route.get("match")
    if not match_sets:
        return True
    # 多个匹配器集合之间为 OR
    return any(_matcher_set_matches(m, req) for m in match_sets)


def _status_of(handler: dict[str, Any]) -> int:
    raw = handler.get("status_code", 200)
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise UnsupportedCaddyConfig(f"无法解析 status_code：{raw!r}") from exc


def _walk_routes(routes: list[dict[str, Any]], req: _Request, server: str) -> Any:
    """按顺序执行路由列表；返回 ``CaddyOutcome``、``_STOP`` 或 ``None``（落空）。"""
    matched_groups: set[str] = set()
    for route in routes:
        group = route.get("group")
        if group and group in matched_groups:
            continue
        if not _route_matches(route, req):
            continue
        if group:
            matched_groups.add(group)
        for handler in route.get("handle", []) or []:
            result = _run_handler(handler, req, server)
            if isinstance(result, CaddyOutcome):
                return result
        if route.get("terminal"):
            return _STOP
    return None


def _run_handler(handler: dict[str, Any], req: _Request, server: str) -> CaddyOutcome | None:
    name = handler.get("handler")
    if name == "subroute":
        result = _walk_routes(handler.get("routes", []) or [], req, server)
        return result if isinstance(result, CaddyOutcome) else None
    if name == "rewrite":
        prefix = handler.get("strip_path_prefix")
        if prefix and req.path.lower().startswith(str(prefix).lower()):
            req.path = req.path[len(prefix):] or "/"
            if not req.path.startswith("/"):
                req.path = "/" + req.path
        if handler.get("uri") or handler.get("path_regexp") or handler.get("uri_substring"):
            raise UnsupportedCaddyConfig(f"模拟器不支持该 rewrite：{handler}")
        return None
    if name == "static_response":
        return CaddyOutcome(
            kind="static_response",
            status=_status_of(handler),
            path=req.path,
            headers={k: list(v) for k, v in (handler.get("headers") or {}).items()},
            server=server,
        )
    if name == "reverse_proxy":
        dials = tuple(str(u.get("dial", "")) for u in handler.get("upstreams", []) or [])
        return CaddyOutcome(
            kind="reverse_proxy", status=0, path=req.path, upstreams=dials, server=server,
        )
    if name == "file_server":
        return CaddyOutcome(
            kind="file_server", status=0, path=req.path, root=handler.get("root"), server=server,
        )
    # headers / vars / encode / rate_limit 等不写响应的中间件：继续执行后续 handler
    return None


def simulate_request(
    config: dict[str, Any],
    path: str,
    *,
    port: int | None = None,
    host: str | None = None,
) -> CaddyOutcome:
    """模拟一次 GET ``path`` 请求在 adapt 后配置中的路由结果。

    ``port``：存在多个 server 时按监听端口选择；``host``：为 ``None`` 时忽略
    ``host`` 匹配器。没有 handler 写响应时返回 ``kind="empty", status=200``。
    """
    if not path.startswith("/"):
        raise ValueError(f"path 必须以 / 开头：{path!r}")
    name, server = _select_server(config, port)
    req = _Request(path=path, host=host)
    result = _walk_routes(server.get("routes", []) or [], req, name)
    if isinstance(result, CaddyOutcome):
        return result
    return CaddyOutcome(kind="empty", status=200, path=req.path, server=name)
