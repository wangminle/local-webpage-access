"""BUG-760 / S2-10 / issue #66：Node 内置 http.createServer 识别 + monorepo 依赖包构建。

真实案例：patent-disclosure-workflow-v1 是 npm workspaces monorepo，
packages/server 用 ``node:http`` 的 ``createServer``（无 express/koa 等框架
依赖），packages/core 是被 server 依赖的 workspace 包。修复前 scanner 报
pending，且 workspace 入口合成漏掉 core 构建、start 落到 tsx dev 模式。

本文件夹具仿照该布局自建（真实源码不在仓库内）。
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from local_webpage_access.models import Kind, Runtime
from local_webpage_access.scanner import Scanner

# 仿 patent-disclosure-workflow-v1 的 server 入口（node:http + createServer）
_SERVER_INDEX_TS = """\
import { createServer } from "node:http";
import { handleRequest } from "@demo/core";

const PORT = Number(process.env.PORT || 3210);

const server = createServer((req, res) => {
  handleRequest(req, res);
});

server.listen(PORT, () => {
  console.log(`listening on ${PORT}`);
});
"""


def _write_pkg(path: Path, data: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "package.json").write_text(json.dumps(data), encoding="utf-8")


def _write_tsconfig(path: Path, out_dir: str = "dist") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "tsconfig.json").write_text(
        json.dumps({"compilerOptions": {"outDir": out_dir}}), encoding="utf-8"
    )


def _write_patent_like_monorepo(root: Path) -> None:
    """npm workspaces monorepo：core（被依赖）+ server（node:http 入口）。"""
    _write_pkg(
        root,
        {"name": "demo-monorepo", "private": True, "workspaces": ["packages/*"]},
    )
    (root / "package-lock.json").write_text("{}", encoding="utf-8")

    # packages/core：library，有 build 脚本，被 server 依赖
    core = root / "packages" / "core"
    _write_pkg(
        core,
        {
            "name": "@demo/core",
            "version": "1.0.0",
            "main": "dist/index.js",
            "scripts": {"build": "tsc"},
        },
    )
    _write_tsconfig(core)
    (core / "src").mkdir(parents=True)
    (core / "src" / "index.ts").write_text(
        "export function handleRequest(req: any, res: any): void { res.end('ok'); }\n",
        encoding="utf-8",
    )

    # packages/server：无框架依赖，node:http createServer；start 是 tsx dev 模式
    server = root / "packages" / "server"
    _write_pkg(
        server,
        {
            "name": "@demo/server",
            "version": "1.0.0",
            "dependencies": {"@demo/core": "*"},
            "scripts": {
                "build": "tsc",
                "dev": "tsx src/index.ts",
                "start": "tsx src/index.ts",
            },
        },
    )
    _write_tsconfig(server)
    (server / "src").mkdir(parents=True)
    (server / "src" / "index.ts").write_text(_SERVER_INDEX_TS, encoding="utf-8")


# ---- monorepo 识别 ----------------------------------------------------------


def test_monorepo_node_stdlib_http_no_longer_pending(tmp_path: Path) -> None:
    """patent 式 monorepo：node:http 服务应被识别，不再 pending（BUG-760）。"""
    _write_patent_like_monorepo(tmp_path)

    result = Scanner().detect(tmp_path)

    assert result.pending is False
    assert result.kind == Kind.NODE
    assert result.runtime == Runtime.DOCKER_COMPOSE
    assert result.form == "backend-container"
    assert result.primary_package == "packages/server"
    assert "node-stdlib-http" in result.stack
    # 端口来自 listen / PORT 环境变量推断（3210）
    assert result.internalPort == 3210


def test_monorepo_build_covers_dependent_workspaces_in_order(tmp_path: Path) -> None:
    """build 命令按依赖顺序覆盖被依赖 workspace：先 core 后 server。"""
    _write_patent_like_monorepo(tmp_path)

    result = Scanner().detect(tmp_path)

    assert result.entry.build is not None
    parts = result.entry.build.split(" && ")
    assert parts == [
        "npm run build -w @demo/core",
        "npm run build -w @demo/server",
    ]


def test_monorepo_start_uses_production_mode_not_tsx(tmp_path: Path) -> None:
    """start 脚本为 tsx dev 模式时，合成生产命令 node packages/server/dist/index.js。"""
    _write_patent_like_monorepo(tmp_path)

    result = Scanner().detect(tmp_path)

    assert result.entry.start == "node packages/server/dist/index.js"
    assert "tsx" not in (result.entry.start or "")


def test_monorepo_production_start_prefers_existing_node_start(tmp_path: Path) -> None:
    """start 已是生产模式（node dist/...）时保持 npm run start -w 语义。"""
    _write_patent_like_monorepo(tmp_path)
    server_pkg = tmp_path / "packages" / "server" / "package.json"
    pkg = json.loads(server_pkg.read_text(encoding="utf-8"))
    pkg["scripts"]["start"] = "node dist/index.js"
    server_pkg.write_text(json.dumps(pkg), encoding="utf-8")

    result = Scanner().detect(tmp_path)

    assert result.entry.start is not None
    assert shlex.split(result.entry.start) == ["npm", "run", "start", "-w", "@demo/server"]


def test_monorepo_transitive_dependency_build_order(tmp_path: Path) -> None:
    """三级依赖 util -> core -> server，build 顺序应为 util、core、server。"""
    _write_patent_like_monorepo(tmp_path)
    util = tmp_path / "packages" / "util"
    _write_pkg(
        util,
        {
            "name": "@demo/util",
            "version": "1.0.0",
            "main": "dist/index.js",
            "scripts": {"build": "tsc"},
        },
    )
    _write_tsconfig(util)
    core_pkg = tmp_path / "packages" / "core" / "package.json"
    pkg = json.loads(core_pkg.read_text(encoding="utf-8"))
    pkg["dependencies"] = {"@demo/util": "*"}
    core_pkg.write_text(json.dumps(pkg), encoding="utf-8")

    result = Scanner().detect(tmp_path)

    parts = (result.entry.build or "").split(" && ")
    assert parts == [
        "npm run build -w @demo/util",
        "npm run build -w @demo/core",
        "npm run build -w @demo/server",
    ]


# ---- 扁平项目识别 -----------------------------------------------------------


def _write_flat_node_http(
    root: Path, entry_name: str, source: str, *, with_deps: bool = False
) -> None:
    _write_pkg(
        root,
        {
            "name": "flat-node-http",
            "dependencies": {"lodash": "^4.0.0"} if with_deps else {},
            "scripts": {},
        },
    )
    (root / entry_name).write_text(source, encoding="utf-8")


_REQUIRE_HTTP_SOURCE = """\
const http = require('http');

http.createServer((req, res) => {
  res.end('ok');
}).listen(8080);
"""


def test_flat_project_require_http_detected(tmp_path: Path) -> None:
    """扁平项目：require('http') + createServer + listen(8080) 可识别。"""
    _write_flat_node_http(tmp_path, "server.js", _REQUIRE_HTTP_SOURCE)

    result = Scanner().detect(tmp_path)

    assert result.pending is False
    assert result.kind == Kind.NODE
    assert result.form == "backend-container"
    assert "node-stdlib-http" in result.stack
    assert result.internalPort == 8080
    assert result.entry.start == "node server.js"
    # 零依赖：install 置 None，避免 npm install 落空（对齐 Python stdlib 路径）
    assert result.entry.build is None


def test_flat_project_esm_node_http_import_detected(tmp_path: Path) -> None:
    """ESM 形式 import { createServer } from 'node:http' 可识别。"""
    _write_flat_node_http(
        tmp_path,
        "index.mjs",
        "import { createServer } from 'node:http';\n"
        "createServer((q, s) => s.end('ok')).listen(process.env.PORT || 9090);\n",
    )

    result = Scanner().detect(tmp_path)

    assert result.pending is False
    assert result.internalPort == 9090
    assert result.entry.start == "node index.mjs"


def test_flat_project_zero_deps_no_install(tmp_path: Path) -> None:
    """无依赖时 install 为 None；有依赖时仍生成 npm install。"""
    _write_flat_node_http(tmp_path, "server.js", _REQUIRE_HTTP_SOURCE, with_deps=True)

    result = Scanner().detect(tmp_path)

    assert result.pending is False
    assert result.entry.install == "npm install"


def test_flat_project_createServer_without_listen_not_detected(tmp_path: Path) -> None:
    """只 createServer 不 listen 不算服务（保守，不猜）。"""
    _write_flat_node_http(
        tmp_path,
        "server.js",
        "const http = require('http');\nconst s = http.createServer();\n",
    )

    result = Scanner().detect(tmp_path)

    assert result.pending is True


def test_flat_project_listen_without_http_import_not_detected(tmp_path: Path) -> None:
    """listen/createServer 但无 node:http 依赖导入，不命中弱信号。"""
    _write_flat_node_http(
        tmp_path,
        "server.js",
        "const { createServer } = require('some-lib');\n"
        "createServer(() => {}).listen(8080);\n",
    )

    result = Scanner().detect(tmp_path)

    assert result.pending is True


def test_flat_project_frontend_deps_yield_to_frontend(tmp_path: Path) -> None:
    """有前端框架依赖的项目不因辅助 server.js 被弱信号抢判为后端。"""
    _write_flat_node_http(tmp_path, "server.js", _REQUIRE_HTTP_SOURCE)
    pkg = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
    pkg["dependencies"] = {"react": "^18.0.0"}
    pkg["scripts"] = {"build": "vite build", "dev": "vite"}
    (tmp_path / "package.json").write_text(json.dumps(pkg), encoding="utf-8")

    result = Scanner().detect(tmp_path)

    # 走 frontend-static 分支（前端无 build 以外的后端特征时不被弱信号抢判）
    assert "node-stdlib-http" not in result.stack


def test_stdlib_weak_signal_yields_to_static_evidence(tmp_path: Path) -> None:
    """静态站 + 辅助 node server.js（BUG-541 同款场景）：弱信号让位。

    根目录有 index.html 时，Node stdlib-http 弱信号不得把项目抢判为后端
    容器（对齐 BUG-541「静态证据优先于 stdlib 弱信号」的处置），维持原
    pending 语义交人工/skills 处理。
    """
    _write_flat_node_http(tmp_path, "server.js", _REQUIRE_HTTP_SOURCE)
    (tmp_path / "index.html").write_text("<html><body>hi</body></html>", encoding="utf-8")

    result = Scanner().detect(tmp_path)

    assert result.pending is True
    assert "node-stdlib-http" not in result.stack
