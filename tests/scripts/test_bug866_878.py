"""BUG-866~878 检测/推断缺陷回归测试（真实应用评测发现）。

- BUG-866（#97）：astro 项目识别为 frontend-static；声明 @astrojs/node 才进后端
- BUG-867（#98）：Flask 工厂包（flaskr/ + create_app）推断 gunicorn 工厂命令
- BUG-868（#99）：可选 redis 依赖不再拦 pending（scanner 侧断言见 test_scanner.py）
- BUG-871（#102）：解包 yarn --cwd / yarn workspace / pnpm --dir / npm --prefix 后判 dev
- BUG-873（#104）：bun.lock / bun.lockb → bun install --frozen-lockfile
- BUG-878（#109）：go.mod 主服务 + web/ 前端不得单独当完整应用部署
- BUG-872（#103）：monorepo 多主包 pending 提示引用真实存在的 CLI 选项
"""

from __future__ import annotations

import json
from pathlib import Path

from local_webpage_access.models import Kind, Runtime, ServingMode
from local_webpage_access.scanner import Scanner, _is_dev_node_script


def _write_package_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


# ---- BUG-866（#97）：astro 前端识别 -------------------------------------------


def test_astro_project_detected_as_frontend_static(tmp_path: Path) -> None:
    """astro examples/portfolio 形态（astro 依赖 + build 脚本、无 SSR adapter）
    应识别为 frontend-static，而非 unknown/pending。"""
    _write_package_json(
        tmp_path / "package.json",
        {
            "name": "portfolio",
            "dependencies": {"astro": "^7.3.7"},
            "scripts": {"build": "astro build", "dev": "astro dev"},
        },
    )
    result = Scanner().detect(tmp_path)
    assert result.pending is False
    assert result.kind == Kind.NODE
    assert result.runtime == Runtime.SHARED_STATIC
    assert result.servingMode == ServingMode.SHARED_STATIC
    assert result.form == "frontend-static"
    assert result.confidence == "high"
    assert result.entry.build == "npm run build"


def test_astro_with_node_ssr_adapter_is_backend(tmp_path: Path) -> None:
    """BUG-866（#97）：声明 @astrojs/node SSR adapter 的 astro 项目是后端容器形态。"""
    _write_package_json(
        tmp_path / "package.json",
        {
            "name": "astro-ssr",
            "dependencies": {"astro": "^7.3.7", "@astrojs/node": "^9.0.0"},
            "scripts": {"build": "astro build", "start": "node dist/server/entry.mjs"},
        },
    )
    result = Scanner().detect(tmp_path)
    assert result.pending is False
    assert result.form == "backend-container"
    assert result.runtime == Runtime.DOCKER_COMPOSE
    assert result.internalPort is not None


# ---- BUG-867（#98）：Flask 工厂包启动命令推断 ---------------------------------


def test_flask_factory_package_infers_gunicorn_factory(tmp_path: Path) -> None:
    """flask 教程式目录（pyproject 项目、无 app.py、flaskr/__init__.py 含
    create_app）应推断 gunicorn 工厂命令，而不是指向不存在模块的 flask run。"""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "flaskr"\ndependencies = ["flask"]\n'
    )
    pkg = tmp_path / "flaskr"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "from flask import Flask\n\n\ndef create_app():\n    app = Flask(__name__)\n    return app\n"
    )
    result = Scanner().detect(tmp_path)
    assert result.pending is False
    assert result.confidence == "high"
    # CHK-V02：create_app() 的括号触发 sh -c 包裹（平台 CMD 安全惯例），
    # 内层命令应为 gunicorn 工厂形态而非 flask --app app dev server
    start = result.entry.start or ""
    assert "gunicorn --workers 1 --threads 4 -b 0.0.0.0:8000 flaskr:create_app()" in start
    assert "flask --app app run" not in start
    # BUG-181 口径：internalPort 与启动命令绑定端口一致
    assert result.internalPort == 8000


def test_flask_factory_multiple_candidates_falls_back(tmp_path: Path) -> None:
    """多个包目录都声明 create_app 时不猜测，保持现状（flask --app app 兜底）。"""
    (tmp_path / "requirements.txt").write_text("flask\n")
    for name in ("flaskr", "api"):
        pkg = tmp_path / name
        pkg.mkdir()
        (pkg / "__init__.py").write_text("def create_app():\n    pass\n")
    result = Scanner().detect(tmp_path)
    assert result.pending is False
    assert "flask --app app run" in (result.entry.start or "")


def test_flask_no_factory_keeps_existing_fallback(tmp_path: Path) -> None:
    """BUG-867 对照：无工厂、无入口文件时保持 flask --app app 现状。"""
    (tmp_path / "requirements.txt").write_text("flask\n")
    result = Scanner().detect(tmp_path)
    assert "flask --app app run --host 0.0.0.0 --port 5000" == result.entry.start
    assert result.internalPort == 5000


# ---- BUG-871（#102）：包管理器包装解包后的 dev 判定 -----------------------------


def test_is_dev_node_script_unwraps_pm_dir_wrappers() -> None:
    """``yarn --cwd <dir> <script>`` / ``pnpm --dir`` / ``npm --prefix`` 解包后
    继续按目标脚本判定 dev 前缀（vite/tsx 等）。"""
    assert _is_dev_node_script("yarn --cwd ./excalidraw-app vite") is True
    assert _is_dev_node_script("pnpm --dir ./app tsx src/server.ts") is True
    assert _is_dev_node_script("npm --prefix ./app run nodemon server.js") is True
    # 解包后不是 dev 前缀的按生产处理
    assert _is_dev_node_script("yarn --cwd ./app node dist/server.js") is False
    assert _is_dev_node_script("yarn --cwd ./excalidraw-app start") is False
    # 未包装形态行为不变
    assert _is_dev_node_script("vite") is True
    assert _is_dev_node_script("tsx watch src/index.ts") is True
    assert _is_dev_node_script("node server.js") is False
    assert _is_dev_node_script(None) is False


def test_is_dev_node_script_unwraps_yarn_workspace() -> None:
    """``yarn workspace <name> run <script>`` 解包后按目标脚本判定。"""
    assert _is_dev_node_script("yarn workspace @app/web run vite") is True
    assert _is_dev_node_script("yarn workspace @app/web start") is False
    assert _is_dev_node_script("yarn workspace @app/api tsx src/server.ts") is True


def test_excalidraw_style_monorepo_is_frontend_static(tmp_path: Path) -> None:
    """excalidraw 式根 package.json（workspaces + 根 start 为
    ``yarn --cwd ./excalidraw-app start`` + 子包 vite）判为 frontend-static，
    而非 backend-container。"""
    _write_package_json(
        tmp_path / "package.json",
        {
            "name": "excalidraw-monorepo",
            "private": True,
            "workspaces": ["excalidraw-app"],
            "scripts": {"start": "yarn --cwd ./excalidraw-app start"},
        },
    )
    app = tmp_path / "excalidraw-app"
    app.mkdir()
    _write_package_json(
        app / "package.json",
        {
            "name": "excalidraw-app",
            "dependencies": {"react": "^19.0.0", "react-dom": "^19.0.0"},
            "devDependencies": {"vite": "^6.0.0"},
            "scripts": {"start": "vite", "build": "vite build"},
        },
    )
    result = Scanner().detect(tmp_path)
    assert result.pending is False
    assert result.form == "frontend-static"
    assert result.runtime == Runtime.SHARED_STATIC
    assert result.kind == Kind.NODE


# ---- BUG-873（#104）：bun 锁文件安装命令 --------------------------------------


def test_bun_lock_infers_bun_install(tmp_path: Path) -> None:
    """现行文本锁 bun.lock 应识别为 bun install --frozen-lockfile。"""
    _write_package_json(
        tmp_path / "package.json",
        {
            "dependencies": {"react": "^18.0.0"},
            "scripts": {"build": "vite build"},
        },
    )
    (tmp_path / "bun.lock").write_text('{"lockfileVersion": 1}\n', encoding="utf-8")
    result = Scanner().detect(tmp_path)
    assert result.entry.install == "bun install --frozen-lockfile"


def test_bun_lockb_infers_bun_install(tmp_path: Path) -> None:
    """旧二进制锁 bun.lockb 不再错映射为 npm ci。"""
    _write_package_json(
        tmp_path / "package.json",
        {
            "dependencies": {"react": "^18.0.0"},
            "scripts": {"build": "vite build"},
        },
    )
    (tmp_path / "bun.lockb").write_bytes(b"\x8b\xb8...")
    result = Scanner().detect(tmp_path)
    assert result.entry.install == "bun install --frozen-lockfile"


def test_candidate_generator_bun_lock_infers_bun_install(tmp_path: Path) -> None:
    """candidate_generator 侧同步：根候选 install 同样识别 bun.lock。"""
    from local_webpage_access.evidence_collector import collect
    from local_webpage_access.candidate_generator import generate_candidates

    _write_package_json(
        tmp_path / "package.json",
        {
            "dependencies": {"react": "^18.0.0"},
            "scripts": {"build": "vite build"},
        },
    )
    (tmp_path / "bun.lock").write_text('{"lockfileVersion": 1}\n', encoding="utf-8")
    candidates = generate_candidates(collect(tmp_path))
    frontend = next(c for c in candidates if c.form == "frontend-static")
    assert frontend.entry.install == "bun install --frozen-lockfile"


# ---- BUG-878（#109）：Go 主服务拦截 ------------------------------------------


def _make_go_web_project(root: Path) -> None:
    """memos 式布局：根 go.mod + cmd/<name>/main.go + web/ React 前端。"""
    (root / "go.mod").write_text("module example.com/memos\n\ngo 1.26.2\n", encoding="utf-8")
    cmd = root / "cmd" / "memos"
    cmd.mkdir(parents=True)
    (cmd / "main.go").write_text("package main\n\nfunc main() {}\n", encoding="utf-8")
    web = root / "web"
    web.mkdir()
    _write_package_json(
        web / "package.json",
        {
            "name": "memos-web",
            "dependencies": {"react": "^19.0.0", "react-dom": "^19.0.0"},
            "devDependencies": {"vite": "^6.0.0"},
            "scripts": {"build": "vite build", "dev": "vite"},
        },
    )


def test_go_mod_with_web_frontend_pending(tmp_path: Path) -> None:
    """memos 实案：go.mod 主服务 + web/ 前端不得识别为 frontend-static 部署，
    应判 pending 且提示含 Go。"""
    _make_go_web_project(tmp_path)
    result = Scanner().detect(tmp_path)
    assert result.pending is True
    assert result.confidence == "low"
    assert result.form == "unknown"
    assert any("Go" in n for n in result.notes)


def test_go_mod_only_project_pending_with_go_hint(tmp_path: Path) -> None:
    """仅 Go 主服务（无前端子目录候选）也应在兜底分支给出明确 Go 提示。"""
    (tmp_path / "go.mod").write_text("module example.com/svc\n\ngo 1.26.2\n", encoding="utf-8")
    (tmp_path / "main.go").write_text("package main\n\nfunc main() {}\n", encoding="utf-8")
    result = Scanner().detect(tmp_path)
    assert result.pending is True
    assert any("Go" in n for n in result.notes)


def test_pure_vite_frontend_unaffected_by_go_rule(tmp_path: Path) -> None:
    """BUG-878 对照：无 go.mod 的纯 vite 前端不受影响。"""
    _write_package_json(
        tmp_path / "package.json",
        {
            "dependencies": {"react": "^18.0.0", "react-dom": "^18.0.0"},
            "devDependencies": {"vite": "^6.0.0"},
            "scripts": {"build": "vite build"},
        },
    )
    result = Scanner().detect(tmp_path)
    assert result.pending is False
    assert result.form == "frontend-static"


def test_python_backend_subdir_unaffected_by_go_rule(tmp_path: Path) -> None:
    """BUG-878 对照：前后端分离且后端为 Python 的项目（fastapi-template 式）
    不受 Go 规则影响——backend/ 的 Python 候选仍是主服务。"""
    (tmp_path / "go.mod").write_text("module example.com/mixed\n\ngo 1.26.2\n", encoding="utf-8")
    (tmp_path / "main.go").write_text("package main\n\nfunc main() {}\n", encoding="utf-8")
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "requirements.txt").write_text("flask\n", encoding="utf-8")
    (backend / "app.py").write_text("app = None\n", encoding="utf-8")
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    _write_package_json(
        frontend / "package.json",
        {
            "name": "frontend",
            "dependencies": {"react": "^18.0.0"},
            "devDependencies": {"vite": "^6.0.0"},
            "scripts": {"build": "vite build"},
        },
    )
    result = Scanner().detect(tmp_path)
    assert result.pending is False
    assert result.kind == Kind.PYTHON
    assert result.source_subdir == "backend"
    assert not any("Go" in n for n in result.notes)


# ---- BUG-872（#103）：monorepo pending 提示引用真实 CLI 选项 --------------------


def test_classifier_pending_hint_uses_real_cli_option(tmp_path: Path) -> None:
    """多 web_server 子包的 pending 提示不得引用不存在的 --package，
    应引导真实可行的 --from-git <repo> --subdir <子包路径>。"""
    from local_webpage_access.package_classifier import classify_and_select

    _write_package_json(
        tmp_path / "package.json",
        {"name": "mono", "private": True, "workspaces": ["packages/*"]},
    )
    for name in ("api1", "api2"):
        pkg = tmp_path / "packages" / name
        pkg.mkdir(parents=True)
        _write_package_json(
            pkg / "package.json",
            {
                "name": f"@app/{name}",
                "dependencies": {"express": "^4.0.0"},
                "scripts": {"start": "node server.js"},
            },
        )
    result = classify_and_select(tmp_path)
    assert result.primary is None
    hint = next(n for n in result.notes if "pending" in n)
    assert "--package" not in hint
    assert "--subdir" in hint
    assert "--from-git" in hint
