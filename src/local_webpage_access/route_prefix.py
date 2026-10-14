"""S2-06（#50 后半 / BUG-742）：健康端点路由前缀合成。

纯静态正则解析、尽力而为：

- FastAPI：``X = APIRouter(prefix=...)`` 构造器前缀为路由本身的前缀；
  ``include_router`` 链（含中间层 APIRouter——router 挂进带前缀的
  router、再挂进 app）沿边任意深度累加。挂载点目标可以是模块点路径
  （``<module>.router``，模块点路径先按 ``<a.b.c>.py`` 相对扫描根、再
  相对入口目录解析，最后按文件名兜底）、同文件变量，或裸变量名——
  裸变量在全部扫描文件中唯一匹配到 APIRouter 定义时视为跨文件引用
  （``from app.api.router import api_router`` 后 include 的形态）。
- Node：``app.use('<prefix>', <router>)`` 为第 1 层；同文件
  ``<parent>.use('<prefix>', <router>)`` 链式挂载与 ``require('./x')`` /
  ``import x from './x'`` 跨文件挂载沿链任意深度累加。

能确定完整路径的合成结果标 ``high``；无法确定完整路径的（router 变量
未在扫描到的文件中解析到挂载点）仍以路由原文入探针，标 ``low`` 并给出
说明——S1-04 的探针内容校验兜底，拼错只会变成「返回 HTML」提示，
不会造成假绿。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Python 路由装饰器：@app.get("/health") / @router.head("/x")（含变量名）。
_PY_ROUTE_RE = re.compile(
    r"""(?<![\"'])@(\w+)\.(get|head)\s*\(\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
# app = FastAPI() —— 应用级变量，路由无前缀。
_PY_FASTAPI_RE = re.compile(r"(\w+)\s*=\s*FastAPI\s*\(")
# router = APIRouter(..., prefix="/api") —— 第 1 层前缀。
# BUG-792：参数须容忍一层嵌套括号（dependencies=[Depends(...)]），
# 否则 [^)]* 在首个 ) 截断、prefix 在其后时丢失并误标 high。
_PY_ARGS_RE = r"((?:[^()]|\([^()]*\))*)"
_PY_APIROUTER_RE = re.compile(r"(\w+)\s*=\s*APIRouter\(" + _PY_ARGS_RE + r"\)")
_PY_PREFIX_KW_RE = re.compile(r"prefix\s*=\s*[\"']([^\"']+)[\"']")
# <recv>.include_router(<module>.router, prefix="/api/v1") / 裸 include_router(...)。
# recv 为挂载父变量（FastAPI 应用或上层 APIRouter）；缺省视为 app。
_PY_INCLUDE_RE = re.compile(
    r"(?:(\w+)\s*\.\s*)?include_router\(\s*([\w.]+)\s*(?:,\s*" + _PY_ARGS_RE + r")?\)"
)

# Node 路由声明：app.get('/health') / router.head('/x') / r.get(...)。
# S2-06：变量名不再受限（任意标识符都可能是 Router 变量）；未解析到
# app.use 挂载点的变量最终以低置信度入探针，健康命名过滤兜底噪音。
_NODE_ROUTE_RE = re.compile(
    r"""(?<![\"'])\b(\w+)\s*\.\s*(get|head)\s*\(\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
# const router = express.Router() / const router = Router()
_NODE_ROUTER_DEF_RE = re.compile(r"\b(\w+)\s*=\s*(?:express\s*\.\s*)?Router\s*\(")
# app.use('/api', router) / sub.use('/v1', router)
_NODE_USE_RE = re.compile(
    r"""\b(\w+)\s*\.\s*use\s*\(\s*['"]([^'"]+)['"]\s*,\s*(\w+)"""
)
# const healthRouter = require('./routes/health')
_NODE_REQUIRE_RE = re.compile(
    r"""\b(\w+)\s*=\s*require\s*\(\s*['"](\.{1,2}/[^'"]+)['"]\s*\)"""
)
# import healthRouter from './routes/health'
_NODE_IMPORT_RE = re.compile(
    r"""\bimport\s+(\w+)\s+from\s*['"](\.{1,2}/[^'"]+)['"]"""
)

_NODE_EXTENSIONS = (".js", ".ts", ".mjs", ".cjs")


@dataclass(frozen=True)
class ComposedRoute:
    """一条合成后的路由声明。"""

    method: str  # GET / HEAD
    route: str  # 装饰器原文中的路径（健康命名判定用）
    path: str  # 合成后的完整路径（入探针）
    confidence: str  # "high" = 完整路径可确定；"low" = 挂载前缀未解析
    note: str = ""  # confidence == "low" 时的说明（写入探针 description）


def _join_paths(*parts: str) -> str:
    """把前缀与路由拼成规范路径：/a + /b//c -> /a/b/c；空段忽略。"""
    segments: list[str] = []
    for part in parts:
        if not part:
            continue
        segments.extend(seg for seg in part.split("/") if seg)
    return "/" + "/".join(segments)


def _parse_prefix_kw(args: str | None) -> str:
    if not args:
        return ""
    m = _PY_PREFIX_KW_RE.search(args)
    return m.group(1) if m else ""


def compose_python_routes(scan_root: Path, texts: dict[Path, str]) -> list[ComposedRoute]:
    """FastAPI 路由前缀合成（APIRouter 构造器前缀 + include_router 链）。

    ``texts`` 为已去注释的 {文件路径: 源码}（仅 ``.py``）。挂载链沿
    include_router 任意深度累加；任一环无法解析到 FastAPI 应用时整体
    降级为 ``confidence="low"``。
    """
    per_file: dict[Path, tuple[set[str], dict[str, str], list[tuple[str, str, str]], list[tuple[str, str, str]]]] = {}
    for path, text in texts.items():
        fastapi_vars = set(_PY_FASTAPI_RE.findall(text))
        router_prefixes = {
            var: _parse_prefix_kw(args) for var, args in _PY_APIROUTER_RE.findall(text)
        }
        includes = [
            (recv or "app", target, _parse_prefix_kw(args))
            for recv, target, args in _PY_INCLUDE_RE.findall(text)
        ]
        routes = [
            (var, method.upper(), route) for var, method, route in _PY_ROUTE_RE.findall(text)
        ]
        per_file[path] = (fastapi_vars, router_prefixes, includes, routes)

    by_stem: dict[str, list[Path]] = {}
    for path in texts:
        by_stem.setdefault(path.stem, []).append(path)

    def resolve_module(includer: Path, module: str) -> Path | None:
        rel = Path(*module.split(".")).with_suffix(".py")
        for candidate in (scan_root / rel, includer.parent / rel.name):
            if candidate in texts:
                return candidate
        stem = module.rsplit(".", 1)[-1]
        hits = by_stem.get(stem, [])
        return hits[0] if len(hits) == 1 else None

    # 挂载边：被 include 的 router (file, var) -> (父文件, 父变量, 挂载前缀)；
    # 父变量递归解析到 FastAPI 应用变量即到达挂载根。模块点路径带变量名时
    # 精确到变量（未命中再按文件级兜底）；裸变量名在全部扫描文件中唯一
    # 匹配到 APIRouter 定义时视为跨文件引用（BUG-786：from x import
    # api_router 后 include 的中间层形态）。
    mount_edges: dict[tuple[Path, str], tuple[Path, str, str]] = {}
    file_mount_edges: dict[Path, tuple[Path, str, str]] = {}
    for path, (_, router_prefixes, includes, _) in per_file.items():
        for parent, target, prefix in includes:
            if "." in target:
                module, var = target.rsplit(".", 1)
                resolved = resolve_module(path, module)
                if resolved is None:
                    continue
                if var in per_file[resolved][1]:
                    mount_edges.setdefault((resolved, var), (path, parent, prefix))
                else:
                    file_mount_edges.setdefault(resolved, (path, parent, prefix))
            elif target in router_prefixes:
                mount_edges.setdefault((path, target), (path, parent, prefix))
            else:
                holders = [f for f, (_, rps, _, _) in per_file.items() if target in rps]
                if len(holders) == 1:
                    mount_edges.setdefault((holders[0], target), (path, parent, prefix))

    def prefix_for(
        file: Path, var: str, seen: frozenset[tuple[Path, str]] = frozenset()
    ) -> tuple[str, bool]:
        """-> (累积前缀, 是否解析到 FastAPI 应用)；未解析返回已知前缀 + False。"""
        if (file, var) in seen:
            return "", False
        seen = seen | {(file, var)}
        fastapi_vars, router_prefixes = per_file[file][0], per_file[file][1]
        if var in fastapi_vars:
            return "", True
        ctor = router_prefixes.get(var)
        if ctor is None:
            return "", False
        edge = mount_edges.get((file, var)) or file_mount_edges.get(file)
        if edge is None:
            # 构造器前缀已知，但挂载点不在扫描到的 include_router 链中。
            return ctor, False
        parent_file, parent_var, mount_p = edge
        base, ok = prefix_for(parent_file, parent_var, seen)
        return _join_paths(base, mount_p, ctor), ok

    out: list[ComposedRoute] = []
    for path, (fastapi_vars, router_prefixes, _, routes) in per_file.items():
        for var, method, route in routes:
            if not route.startswith("/") or "{" in route or "<" in route:
                continue
            if var in fastapi_vars or var == "app":
                # 应用级路由无前缀。
                out.append(ComposedRoute(method, route, route, "high"))
                continue
            if var in router_prefixes:
                full, ok = prefix_for(path, var)
                if ok:
                    out.append(ComposedRoute(method, route, _join_paths(full, route), "high"))
                else:
                    note = (
                        f"路由前缀合成：router 变量 {var} 未在扫描到的 "
                        "include_router 中挂载，挂载前缀未知"
                    )
                    out.append(
                        ComposedRoute(method, route, _join_paths(full, route), "low", note)
                    )
                continue
            # 变量在本文件未定义（跨文件装饰器、Flask 蓝本等）：无法合成。
            note = f"路由前缀合成：装饰器变量 {var} 的挂载前缀无法静态解析"
            out.append(ComposedRoute(method, route, route, "low", note))
    return out


def compose_node_routes(scan_root: Path, texts: dict[Path, str]) -> list[ComposedRoute]:
    """Node/Express 路由前缀合成（app.use 第 1 层 + 链式/跨文件挂载，任意深度）。

    ``texts`` 为已去注释的 {文件路径: 源码}（``.js``/``.ts``/``.mjs``/``.cjs``）。
    """
    del scan_root  # 解析相对 require 以入口文件目录为基准，扫描根暂不参与
    per_file: dict[Path, tuple[list[str], list[tuple[str, str, str]], dict[str, str], list[tuple[str, str, str]]]] = {}
    for path, text in texts.items():
        router_defs = list(dict.fromkeys(_NODE_ROUTER_DEF_RE.findall(text)))
        uses = list(_NODE_USE_RE.findall(text))
        requires = {var: rel for var, rel in _NODE_REQUIRE_RE.findall(text)}
        requires.update({var: rel for var, rel in _NODE_IMPORT_RE.findall(text)})
        routes = [
            (var, method.upper(), route) for var, method, route in _NODE_ROUTE_RE.findall(text)
        ]
        per_file[path] = (router_defs, uses, requires, routes)

    def resolve_rel(includer: Path, rel: str) -> Path | None:
        base = includer.parent / rel
        candidates = [base.with_suffix(ext) for ext in _NODE_EXTENSIONS]
        candidates += [base / f"index{ext}" for ext in _NODE_EXTENSIONS]
        for candidate in candidates:
            if candidate in texts:
                return candidate
        return None

    # direct[(file, child_var)] = (parent_var, prefix)；parent_var=None 表示
    # 直接挂在 app 上（第 1 层解析完毕）。
    direct: dict[tuple[Path, str], tuple[str | None, str]] = {}
    for path, (router_defs, uses, requires, _) in per_file.items():
        for parent, prefix, child in uses:
            if parent != "app" and parent not in router_defs:
                continue
            if child in router_defs:
                direct[(path, child)] = (None if parent == "app" else parent, prefix)
                continue
            if child in requires:
                target = resolve_rel(path, requires[child])
                if target is not None and per_file[target][0]:
                    # 跨文件挂载：把被 require 模块的第 1 个 Router 变量视为导出路由。
                    direct[(target, per_file[target][0][0])] = (None, prefix)

    def prefix_for(
        file: Path, var: str, seen: frozenset[tuple[Path, str]] = frozenset()
    ) -> tuple[str, bool]:
        """-> (前缀, 是否解析到挂载点)；未解析返回 ("", False)。沿链任意深度展开。"""
        if var in ("app", "server"):
            return "", True
        if (file, var) in seen:
            return "", False
        seen = seen | {(file, var)}
        entry = direct.get((file, var))
        if entry is None:
            return "", False
        parent, prefix = entry
        if parent is None:
            return prefix, True
        parent_prefix, ok = prefix_for(file, parent, seen)
        if not ok:
            return "", False
        return _join_paths(parent_prefix, prefix), True

    out: list[ComposedRoute] = []
    for path, (_, _, _, routes) in per_file.items():
        for var, method, route in routes:
            if not route.startswith("/") or "{" in route or "<" in route:
                continue
            prefix, ok = prefix_for(path, var)
            if ok:
                out.append(ComposedRoute(method, route, _join_paths(prefix, route), "high"))
            else:
                note = (
                    f"路由前缀合成：Node 路由变量 {var} 的挂载前缀"
                    "无法静态解析（app.use 未匹配）"
                )
                out.append(ComposedRoute(method, route, route, "low", note))
    return out
