"""scanner 模块测试（WBS-08）。"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from local_webpage_access.models import EntryConfig, Kind, ResourceProfile, Runtime, ServingMode
from local_webpage_access.paths import Workspace
from local_webpage_access.scanner import DetectionResult, Scanner, summarize


# ---- FileSummary -----------------------------------------------------------


def test_summarize_picks_up_key_files(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html></html>")
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"express": "^4.0.0"}, "scripts": {"start": "node ."}})
    )
    summary = summarize(tmp_path)
    assert summary.has_index_html is True
    assert summary.has_package_json is True
    assert "express" in {d.lower() for d in summary.node_deps}
    assert "start" in summary.node_scripts


def test_summarize_collects_sqlite_files(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "app.sqlite").write_bytes(b"")
    summary = summarize(tmp_path)
    assert any("app.sqlite" in f for f in summary.sqlite_files)


# ---- Static ---------------------------------------------------------------


def test_detect_static_html(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html></html>")
    (tmp_path / "style.css").write_text("body{}")
    result = Scanner().detect(tmp_path)
    assert result.kind == Kind.STATIC
    assert result.runtime == Runtime.SHARED_STATIC
    assert result.servingMode == ServingMode.SHARED_STATIC
    assert result.form == "static"
    assert result.resourceProfile == ResourceProfile.TINY
    assert result.confidence == "high"
    assert result.pending is False


def test_detect_static_with_subdir_index(tmp_path: Path) -> None:
    sub = tmp_path / "site"
    sub.mkdir()
    (sub / "index.html").write_text("<html></html>")
    result = Scanner().detect(tmp_path)
    assert result.kind == Kind.STATIC
    assert result.confidence == "high"


# ---- Node frontend --------------------------------------------------------


def test_detect_node_frontend_static(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"react": "^18.0.0", "react-dom": "^18.0.0"},
                "scripts": {"build": "vite build"},
            }
        )
    )
    result = Scanner().detect(tmp_path)
    assert result.kind == Kind.NODE
    assert result.runtime == Runtime.SHARED_STATIC
    assert result.form == "frontend-static"
    assert result.resourceProfile == ResourceProfile.TINY
    assert result.entry.build == "npm run build"
    assert result.confidence == "high"


def test_detect_node_uses_ci_when_lockfile_present(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"react": "^18.0.0"},
                "scripts": {"build": "vite build"},
            }
        )
    )
    (tmp_path / "package-lock.json").write_text("{}")
    result = Scanner().detect(tmp_path)
    assert result.entry.install == "npm ci"


def test_detect_node_uses_pnpm_when_pnpm_lock_present(tmp_path: Path) -> None:
    """BUG-041：pnpm 锁文件项目不得误判为 npm ci。"""
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"express": "^4.18.0"},
                "scripts": {"start": "node server.js"},
            }
        )
    )
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    result = Scanner().detect(tmp_path)
    assert result.entry.install == "corepack enable && pnpm install --frozen-lockfile"


def test_detect_node_uses_yarn_when_yarn_lock_present(tmp_path: Path) -> None:
    """BUG-041：yarn 锁文件项目不得误判为 npm ci。"""
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"express": "^4.18.0"},
                "scripts": {"start": "node server.js"},
            }
        )
    )
    (tmp_path / "yarn.lock").write_text("# yarn lockfile\n")
    result = Scanner().detect(tmp_path)
    assert result.entry.install == "corepack enable && yarn install --frozen-lockfile"


# ---- Node backend ---------------------------------------------------------


def test_detect_node_backend_container(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"express": "^4.18.0"},
                "scripts": {"start": "node server.js"},
            }
        )
    )
    result = Scanner().detect(tmp_path)
    assert result.kind == Kind.NODE
    assert result.runtime == Runtime.DOCKER_COMPOSE
    assert result.servingMode == ServingMode.CONTAINER
    assert result.form == "backend-container"
    assert result.internalPort == 3000
    assert result.confidence == "high"


def test_detect_node_backend_port_from_scripts_env(tmp_path: Path) -> None:
    """BUG-032：scripts 中 PORT=xxxx 应被识别为容器端口。"""
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"express": "^4.18.0"},
                "scripts": {"start": "PORT=8080 node server.js"},
            }
        )
    )
    result = Scanner().detect(tmp_path)
    assert result.internalPort == 8080


def test_detect_node_backend_port_from_scripts_flag(tmp_path: Path) -> None:
    """BUG-032：scripts 中 --port xxxx（及 --port=xxxx）应被识别。"""
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"express": "^4.18.0"},
                "scripts": {"start": "node server.js --port 4000"},
            }
        )
    )
    result = Scanner().detect(tmp_path)
    assert result.internalPort == 4000


def test_detect_node_port_prefers_start_over_vite_dev(tmp_path: Path) -> None:
    """BUG-322：express+vite 时不应被 dev/build 的 Vite 端口覆盖。"""
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"express": "^4.18.0", "vite": "^5.0.0"},
                "scripts": {
                    "start": "node server.js",
                    "dev": "vite --port 5173",
                    "build": "vite build --port 5173",
                },
            }
        )
    )
    result = Scanner().detect(tmp_path)
    assert result.internalPort == 3000


def test_detect_node_port_from_dev_when_no_start(tmp_path: Path) -> None:
    """BUG-322：无 start 时可读 dev 脚本端口。"""
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"express": "^4.18.0"},
                "scripts": {"dev": "PORT=4000 node server.js"},
            }
        )
    )
    result = Scanner().detect(tmp_path)
    assert result.internalPort == 4000


def test_detect_node_invalid_port_left_none(tmp_path: Path) -> None:
    """BUG-322：非法端口（如 99999）不得写入 internalPort。"""
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"express": "^4.18.0"},
                "scripts": {"start": "PORT=99999 node server.js"},
            }
        )
    )
    result = Scanner().detect(tmp_path)
    assert result.internalPort is None
    assert result.pending is True
    assert result.confidence == "low"
    assert any("端口非法" in n for n in result.notes)


def test_detect_node_unknown_pending(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"lodash": "^4.0.0"}, "scripts": {}})
    )
    result = Scanner().detect(tmp_path)
    assert result.pending is True
    assert result.confidence == "low"


# ---- Python ---------------------------------------------------------------


def test_detect_python_fastapi(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")
    result = Scanner().detect(tmp_path)
    assert result.kind == Kind.PYTHON
    assert result.runtime == Runtime.DOCKER_COMPOSE
    assert result.servingMode == ServingMode.CONTAINER
    assert "fastapi" in result.stack
    assert result.internalPort == 8000
    assert result.entry.start is not None
    assert "uvicorn" in result.entry.start
    assert result.confidence == "high"


def test_detect_python_fastapi_app_main_module(tmp_path: Path) -> None:
    """BUG-455：仅有 app/main.py 时应推断 app.main:app，不得硬编码 main:app。"""
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    result = Scanner().detect(tmp_path)
    assert result.entry is not None
    assert result.entry.start is not None
    assert "app.main:app" in result.entry.start
    assert "uvicorn main:app" not in result.entry.start


def test_detect_python_fastapi_alembic_prepend(tmp_path: Path) -> None:
    """IMP-052：存在 alembic.ini 时自动前置 alembic upgrade head。"""
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\nalembic\n")
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = alembic\n")
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    result = Scanner().detect(tmp_path)
    assert result.entry is not None
    start = result.entry.start or ""
    assert "alembic upgrade head" in start
    assert "app.main:app" in start
    assert any("alembic" in n.lower() for n in result.notes)


def test_detect_python_fastapi_root_main_without_alembic(tmp_path: Path) -> None:
    """根 main.py 且无 alembic.ini：保持 uvicorn main:app，不包 sh -c。"""
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")
    (tmp_path / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    result = Scanner().detect(tmp_path)
    assert result.entry is not None
    start = result.entry.start or ""
    assert start.startswith("uvicorn main:app")
    assert "alembic" not in start


def test_detect_python_flask_port(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("flask\n")
    result = Scanner().detect(tmp_path)
    assert result.internalPort == 5000
    assert "flask" in result.entry.start


def test_python_multi_framework_port_command_consistent() -> None:
    """BUG-181：flask+gunicorn 等多框架下 internalPort 与启动命令端口须一致，
    且与 matched（源自 set 迭代）顺序无关。"""
    from local_webpage_access.scanner import (
        _infer_python_port,
        _python_start_command,
        _select_python_framework,
    )

    for order in (["flask", "gunicorn"], ["gunicorn", "flask"]):
        assert _select_python_framework(order) == "flask"
        assert _infer_python_port(None, order) == 5000
        cmd = _python_start_command(order, None)
        assert cmd is not None
        assert "--port 5000" in cmd
        assert "flask" in cmd


@pytest.mark.parametrize(
    "framework,needle",
    [
        ("sanic", "sanic"),
        ("tornado", "python app.py"),
        ("starlette", "uvicorn"),
        ("gunicorn", "gunicorn"),
    ],
)
def test_python_secondary_frameworks_have_start_command(
    tmp_path: Path, framework: str, needle: str
) -> None:
    """PYTHON_WEB 内框架须有启动命令，避免高置信度却 CMD 为空。"""
    (tmp_path / "requirements.txt").write_text(f"{framework}\n")
    result = Scanner().detect(tmp_path)
    assert result.pending is False
    assert result.confidence == "high"
    assert result.entry is not None
    assert result.entry.start is not None
    assert needle in result.entry.start


def test_detect_python_streamlit_is_medium(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("streamlit\n")
    result = Scanner().detect(tmp_path)
    assert result.resourceProfile == ResourceProfile.MEDIUM
    assert result.internalPort == 8501


def test_detect_python_no_framework_pending(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("requests\nnumpy\n")
    result = Scanner().detect(tmp_path)
    assert result.pending is True
    assert result.confidence == "low"


# ---- issue#1：零依赖 stdlib HTTP 弱信号识别 ---------------------------------


def test_detect_stdlib_http_with_requirements(tmp_path: Path) -> None:
    """有 requirements.txt 但无框架，顶层 server.py 用 http.server：识别为 backend-container。"""
    (tmp_path / "requirements.txt").write_text("# 空依赖清单\n")
    (tmp_path / "server.py").write_text(
        "import http.server\nfrom socketserver import ThreadingMixIn\n\ndef main():\n    pass\n"
    )
    result = Scanner().detect(tmp_path)
    assert result.pending is False
    assert result.runtime == Runtime.DOCKER_COMPOSE
    assert result.form == "backend-container"
    assert result.confidence == "medium"
    assert result.entry.start == "python server.py"


def test_detect_stdlib_http_no_python_files(tmp_path: Path) -> None:
    """完全零工程文件（无 requirements/pyproject）：仅 server.py 也能路由到 Python 分支。"""
    (tmp_path / "server.py").write_text(
        "from http.server import HTTPServer, BaseHTTPRequestHandler\n"
        "\n"
        "HTTPServer(('0.0.0.0', 8000), BaseHTTPRequestHandler).serve_forever()\n"
    )
    result = Scanner().detect(tmp_path)
    assert result.pending is False
    assert result.kind == Kind.PYTHON
    assert result.form == "backend-container"
    # 零依赖：install 为 None，避免容器构建 pip install 落空
    assert result.entry.install is None
    assert result.entry.start == "python server.py"


def test_detect_stdlib_http_prefers_server_py_over_app_py(tmp_path: Path) -> None:
    """server.py / app.py / main.py 并存时按固定优先级取第一个命中的。"""
    (tmp_path / "requirements.txt").write_text("")
    (tmp_path / "app.py").write_text("import http.server\n")
    (tmp_path / "main.py").write_text("import socketserver\n")
    (tmp_path / "server.py").write_text("import http.server\n")
    result = Scanner().detect(tmp_path)
    assert result.entry.start == "python server.py"


def test_detect_stdlib_http_ignores_comment_only_import(tmp_path: Path) -> None:
    """注释里的 http.server 不算信号；无 import 仍 pending。"""
    (tmp_path / "requirements.txt").write_text("requests\n")
    (tmp_path / "server.py").write_text("# import http.server（注释，不算）\nimport os\n")
    result = Scanner().detect(tmp_path)
    assert result.pending is True


def test_detect_stdlib_http_no_false_positive_on_foreign_module(tmp_path: Path) -> None:
    """BUG-534：from mypkg import socketserver 不算 stdlib-http（AST 精确匹配）。"""
    (tmp_path / "requirements.txt").write_text("requests\n")
    (tmp_path / "server.py").write_text("from mypkg import socketserver\nimport mysocketserver\n")
    result = Scanner().detect(tmp_path)
    assert result.pending is True


def test_detect_stdlib_http_syntax_error_file_not_matched(tmp_path: Path) -> None:
    """BUG-534：语法错误文件解析失败，保守视为未命中。"""
    (tmp_path / "requirements.txt").write_text("requests\n")
    (tmp_path / "server.py").write_text("import http.server\ndef broken(:\n")
    result = Scanner().detect(tmp_path)
    assert result.pending is True


def test_detect_stdlib_http_with_sqlite_is_fullstack(tmp_path: Path) -> None:
    """CHK-225 低危：stdlib-http + SQLite 文件应升 fullstack-sqlite，不得硬编码 backend-container。"""
    (tmp_path / "server.py").write_text(
        "import http.server\nimport sqlite3\nhttp.server.test()\n"
    )
    (tmp_path / "app.db").write_bytes(b"")
    result = Scanner().detect(tmp_path)
    assert result.pending is False
    assert result.stack == ["stdlib-http"]
    assert result.hasDatabase is True
    assert result.form == "fullstack-sqlite"
    assert result.database is not None
    assert result.database.type == "sqlite"


def test_stdlib_http_yields_to_static_index(tmp_path: Path) -> None:
    """CHK-225：index.html + import http.server 预览脚本应保持 static 高置信度。

    stdlib 弱信号不得抢占更强的静态站信号（否则 high 置信度静态站被降级为
    medium 置信度容器应用，且零依赖场景构建会失败）。
    """
    (tmp_path / "index.html").write_text("<html></html>")
    (tmp_path / "server.py").write_text("import http.server\nhttp.server.test()\n")
    result = Scanner().detect(tmp_path)
    assert result.kind == Kind.STATIC
    assert result.form == "static"
    assert result.confidence == "high"
    assert "stdlib-http" not in (result.stack or [])


def test_detect_python_uv_lock_uses_uv_sync(tmp_path: Path) -> None:
    """issue #13：仅含 [[package]] 记录的有效 lock 才走 uv sync。"""
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
    (tmp_path / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "fastapi"\nversion = "0.115.0"\n'
    )
    result = Scanner().detect(tmp_path)
    assert result.entry.install == "uv sync"


def test_detect_python_empty_uv_lock_falls_back_to_requirements(tmp_path: Path) -> None:
    """issue #13：空壳 uv.lock（无包记录）+ 有效 requirements.txt → 回退 pip 分支并留 note。"""
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")
    (tmp_path / "pyproject.toml").write_text("version = 1\n")
    (tmp_path / "uv.lock").write_text(
        'version = 1\nrevision = "abc"\nrequires-python = ">=3.13"\n'
    )
    result = Scanner().detect(tmp_path)
    assert result.entry.install == "pip install -r requirements.txt"
    assert any("uv.lock" in n and "空壳" in n for n in result.notes)


def test_detect_python_empty_uv_lock_no_deps_install_none(tmp_path: Path) -> None:
    """issue #13：空壳 lock 不算依赖文件——stdlib 零依赖服务 install=None（CHK-225 路径）。"""
    (tmp_path / "uv.lock").write_text('version = 1\nrequires-python = ">=3.13"\n')
    (tmp_path / "server.py").write_text(
        "from http.server import HTTPServer, BaseHTTPRequestHandler\n"
        "HTTPServer(('0.0.0.0', 8000), BaseHTTPRequestHandler).serve_forever()\n"
    )
    result = Scanner().detect(tmp_path)
    assert result.entry.install is None
    assert result.entry.start == "python server.py"


def test_detect_python_broken_uv_lock_no_silent_downgrade(tmp_path: Path) -> None:
    """issue #13：TOML 损坏的 lock 不静默降级——保持 uv sync 并在 notes 诊断。

    即使文本里残留 [[package]] 标记也不做字符串计数兜底（会把"损坏"与
    "空壳"两种故障混为一谈），损坏必须显式暴露给用户。
    """
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    (tmp_path / "uv.lock").write_text(
        "this is [not valid toml\n[[package]]\nname = fastapi\n"
    )
    result = Scanner().detect(tmp_path)
    assert result.entry.install == "uv sync"
    assert any("损坏" in n for n in result.notes)


def test_uv_lock_state_unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """issue #13：读取失败 → UNREADABLE（不静默换源），而非按空壳回退。"""
    from local_webpage_access.scanner import _uv_lock_state

    lock = tmp_path / "uv.lock"
    lock.write_text("version = 1\n")
    original_read = Path.read_text

    def _raise_for_lock(self: Path, *args: object, **kwargs: object) -> str:
        if self == lock:
            raise OSError("permission denied")
        return original_read(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", _raise_for_lock)
    assert _uv_lock_state(lock).name == "UNREADABLE"


# ---- SQLite + fullstack ---------------------------------------------------


def test_detect_fullstack_sqlite_python(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("fastapi\nsqlalchemy\n")
    (tmp_path / "app.db").write_bytes(b"")
    result = Scanner().detect(tmp_path)
    assert result.hasDatabase is True
    assert result.form == "fullstack-sqlite"
    assert result.database is not None
    assert result.database.type == "sqlite"


# ---- Heavy DB -------------------------------------------------------------


def test_detect_heavy_db_marks_pending(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("fastapi\npsycopg2\n")
    result = Scanner().detect(tmp_path)
    assert result.pending is True
    assert result.confidence == "medium"
    assert "psycopg2" in result.notes[0]
    assert result.kind == Kind.PYTHON


def test_detect_pipfile_only_heavy_db_fills_python_kind(tmp_path: Path) -> None:
    """Pipfile-only + heavy DB：pending 但仍应填 kind=python（_fill_language 须认 has_pipfile）。"""
    (tmp_path / "Pipfile").write_text('[packages]\nfastapi = "*"\npsycopg2 = "*"\n')
    result = Scanner().detect(tmp_path)
    assert result.pending is True
    assert result.kind == Kind.PYTHON
    assert "psycopg2" in result.notes[0]


# ---- Unknown --------------------------------------------------------------


def test_detect_unknown_marks_pending(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hello")
    result = Scanner().detect(tmp_path)
    assert result.pending is True
    assert result.confidence == "low"


def test_detect_arbitrary_html_as_static(tmp_path: Path) -> None:
    """任意可打开的 .html（不必叫 index.html）应识别为纯静态。"""
    (tmp_path / "kakeya-3d-chapters.html").write_text(
        "<html><body>ok</body></html>", encoding="utf-8"
    )
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "three.min.js").write_text("/* stub */", encoding="utf-8")
    result = Scanner().detect(tmp_path)
    assert result.pending is False
    assert result.form == "static"
    assert result.kind == Kind.STATIC
    assert result.runtime == Runtime.SHARED_STATIC
    assert result.confidence == "high"


def test_detect_missing_dir(tmp_path: Path) -> None:
    result = Scanner().detect(tmp_path / "does-not-exist")
    assert result.pending is True
    assert result.confidence == "low"


# ---- Django port ----------------------------------------------------------


def test_detect_python_django(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("django\n")
    result = Scanner().detect(tmp_path)
    assert result.internalPort == 8000
    assert "manage.py" in result.entry.start


# ---- 回归测试：BUG-004/005/008 -------------------------------------------
#
# BUG-004：summarize 把顶层文件重复计入 total_files / sqlite_files
# BUG-005：Pipfile 用 requirements 的行解析器解析（Pipfile 其实是 TOML）
# BUG-008：has_manage_py 采集了但从未用于 Django 识别


def test_summarize_does_not_double_count_top_level_files(tmp_path: Path) -> None:
    """BUG-004：3 个顶层文件 → total_files=3（不是 5），sqlite_files 不重复。"""
    (tmp_path / "index.html").write_text("x")
    (tmp_path / "style.css").write_text("x")
    (tmp_path / "data.db").write_bytes(b"")
    summary = summarize(tmp_path)
    assert summary.total_files == 3
    assert summary.sqlite_files == ["data.db"]


def test_summarize_counts_subdir_files_once(tmp_path: Path) -> None:
    """子目录文件应被统计且只统计一次。"""
    (tmp_path / "index.html").write_text("x")  # 顶层 1
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_text("x")  # 子目录 1
    (tmp_path / "sub" / "data.sqlite3").write_bytes(b"")  # 子目录 sqlite
    summary = summarize(tmp_path)
    assert summary.total_files == 3
    assert summary.sqlite_files == ["sub/data.sqlite3"]


def test_summarize_pipfile_parsed_as_toml(tmp_path: Path) -> None:
    """BUG-005：Pipfile 按 TOML 解析 [packages]，[[source]] 的键不当依赖。"""
    (tmp_path / "Pipfile").write_text(
        "[packages]\n"
        'flask = "*"\n\n'
        "[[source]]\n"
        'url = "https://pypi.org/simple"\n'
        "verify_ssl = true\n"
        'name = "pypi"\n'
    )
    summary = summarize(tmp_path)
    deps = {d.lower() for d in summary.python_deps}
    assert "flask" in deps
    # [[source]] 段的键不应被误当作依赖
    assert "name" not in deps
    assert "url" not in deps
    assert "verify_ssl" not in deps


def test_detect_python_pipfile_only_uses_pipenv_install(tmp_path: Path) -> None:
    """BUG-024：仅 Pipfile 的 Python Web 项目不应回退到 requirements.txt。"""
    (tmp_path / "Pipfile").write_text('[packages]\nfastapi = "*"\nuvicorn = "*"\n')
    result = Scanner().detect(tmp_path)
    assert result.kind == Kind.PYTHON
    assert result.runtime == Runtime.DOCKER_COMPOSE
    assert result.pending is False
    assert "fastapi" in result.stack
    assert result.entry.install == "pip install pipenv && pipenv install --system --skip-lock"


def test_detect_django_via_manage_py_without_dep(tmp_path: Path) -> None:
    """BUG-008：有 manage.py 但依赖里没列 django 时，也应识别为 Django。"""
    (tmp_path / "requirements.txt").write_text("requests\n")  # 无 django
    (tmp_path / "manage.py").write_text("#!/usr/bin/env python\n")
    result = Scanner().detect(tmp_path)
    assert result.kind == Kind.PYTHON
    assert "django" in result.stack
    assert result.pending is False
    assert result.confidence == "high"
    assert "manage.py" in (result.entry.start or "")


# ---- 回归测试：BUG-018 / BUG-019 -----------------------------------------
#
# BUG-018：Python 3.10 没有 tomllib 时 pyproject.toml 依赖被跳过，FastAPI 等
#          pyproject-only 项目被误判 pending。修复后 3.10 走 tomli 回退。
# BUG-019：package.json 只读 dependencies，vite/svelte 等放在 devDependencies
#          的前端模板识别失败。修复后合并 devDependencies。


def test_detect_python_fastapi_from_pyproject(tmp_path: Path) -> None:
    """BUG-018：仅 pyproject.toml 声明 fastapi 的项目应被识别（3.10 tomli 回退）。"""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndependencies = ["fastapi", "uvicorn"]\n'
    )
    result = Scanner().detect(tmp_path)
    assert result.kind == Kind.PYTHON
    assert "fastapi" in result.stack
    assert result.pending is False
    assert result.confidence == "high"
    assert result.runtime == Runtime.DOCKER_COMPOSE


def test_summarize_pyproject_deps_collected(tmp_path: Path) -> None:
    """BUG-018：summarize 应解析 pyproject.toml [project.dependencies]。"""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndependencies = ["fastapi>=0.100", "uvicorn[standard]"]\n'
    )
    summary = summarize(tmp_path)
    deps = {d.lower() for d in summary.python_deps}
    assert "fastapi" in deps
    assert "uvicorn" in deps


def test_detect_node_frontend_with_devdeps_only(tmp_path: Path) -> None:
    """BUG-019：vite 放在 devDependencies 的前端模板应识别为 frontend-static。"""
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                # 典型 Vite 模板：运行时无 dependencies，构建工具链全在 devDependencies
                "devDependencies": {"vite": "^5.0.0", "react": "^18.0.0"},
                "scripts": {"build": "vite build"},
            }
        )
    )
    result = Scanner().detect(tmp_path)
    assert result.kind == Kind.NODE
    assert result.form == "frontend-static"
    assert result.runtime == Runtime.SHARED_STATIC
    assert result.pending is False
    assert result.confidence == "high"


def test_summarize_merges_devdependencies(tmp_path: Path) -> None:
    """BUG-019：node_deps 应同时包含 dependencies 与 devDependencies。"""
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"react": "^18.0.0"},
                "devDependencies": {"vite": "^5.0.0"},
            }
        )
    )
    summary = summarize(tmp_path)
    deps = {d.lower() for d in summary.node_deps}
    assert "react" in deps
    assert "vite" in deps


# ---- IMP-013：辅助 package.json 优先 Python --------------------------------


def test_detect_prefers_python_when_package_json_is_auxiliary(tmp_path: Path) -> None:
    """IMP-013：package.json 仅含辅助工具（非框架）+ requirements.txt → 识别为
    Python/docker-compose，而非误判 pending 或 static（prd-workflow 类）。"""
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "prd-workflow",
                "devDependencies": {"concurrently": "^8.0.0", "husky": "^9.0.0"},
                "scripts": {"dev": "concurrently ..."},
            }
        )
    )
    result = Scanner().detect(tmp_path)
    assert result.kind == Kind.PYTHON
    assert result.runtime == Runtime.DOCKER_COMPOSE
    assert result.servingMode == ServingMode.CONTAINER
    assert "fastapi" in result.stack
    assert result.pending is False
    assert result.confidence == "high"


def test_detect_real_node_still_wins_over_python(tmp_path: Path) -> None:
    """IMP-013：真 Node（命中 NODE_BACKEND）即使同时有 Python 工程文件也优先 Node。"""
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"express": "^4.0.0"}, "scripts": {"start": "node ."}})
    )
    result = Scanner().detect(tmp_path)
    assert result.kind == Kind.NODE
    assert result.runtime == Runtime.DOCKER_COMPOSE


# ---- IMP-018：重依赖自动升 medium -----------------------------------------


def test_detect_heavy_deps_upgrade_profile(tmp_path: Path) -> None:
    """IMP-018：命中 lancedb/pyarrow/torch/openai 等重运行时依赖 → 自动升 medium。"""
    (tmp_path / "requirements.txt").write_text("fastapi\nlancedb\n")
    result = Scanner().detect(tmp_path)
    assert result.resourceProfile == ResourceProfile.MEDIUM
    assert result.pending is False
    assert any("lancedb" in n for n in result.notes)


def test_detect_heavy_deps_does_not_downgrade(tmp_path: Path) -> None:
    """IMP-018：已 medium（streamlit）不因重依赖判定而降级（仅向上提升）。"""
    (tmp_path / "requirements.txt").write_text("streamlit\nlancedb\n")
    result = Scanner().detect(tmp_path)
    assert result.resourceProfile == ResourceProfile.MEDIUM


# ---- BUG-082：仅 requirements-prod.txt 也应识别为 Python ------------------


def test_detect_requirements_prod_only_is_python(tmp_path: Path) -> None:
    """BUG-082：目录仅含 requirements-prod.txt（无 requirements.txt/pyproject/Pipfile）
    时应识别为 Python，而非误判 pending。"""
    (tmp_path / "requirements-prod.txt").write_text("fastapi\nuvicorn\n")
    result = Scanner().detect(tmp_path)
    assert result.kind == Kind.PYTHON
    assert result.runtime == Runtime.DOCKER_COMPOSE
    assert result.servingMode == ServingMode.CONTAINER
    assert "fastapi" in result.stack
    assert result.pending is False
    # 安装命令优先 prod 清单
    assert result.entry.install == "pip install -r requirements-prod.txt"


def test_detect_requirements_prod_not_treated_as_static(tmp_path: Path) -> None:
    """BUG-082：requirements-prod.txt + index.html 不应判为纯静态（仍是 Python 信号）。"""
    (tmp_path / "requirements-prod.txt").write_text("flask\n")
    (tmp_path / "index.html").write_text("<html></html>")
    result = Scanner().detect(tmp_path)
    assert result.kind == Kind.PYTHON
    assert result.pending is False


# ---- BUG-349 / BUG-350 / BUG-352 -------------------------------------------


def test_runtime_paths_not_false_positive_when_workspace_path_contains_app(
    tmp_path: Path,
) -> None:
    """BUG-349：工作区绝对路径含 app 段时，不得把任意 runtime_paths.py 判为 RUNTIME_ROOT。"""
    root = tmp_path / "app" / "workspace" / "proj"
    root.mkdir(parents=True)
    (root / "lib").mkdir()
    (root / "lib" / "runtime_paths.py").write_text("ROOT = '.'\n", encoding="utf-8")
    (root / "requirements.txt").write_text("fastapi\n", encoding="utf-8")

    summary = summarize(root)
    assert summary.has_runtime_paths is False


def test_runtime_paths_detects_explicit_layouts(tmp_path: Path) -> None:
    """BUG-349 / BUG-198：仅 src/app 或 app 下的 runtime_paths.py 才算命中。"""
    for rel in (("src", "app", "runtime_paths.py"), ("app", "runtime_paths.py")):
        root = tmp_path / "-".join(rel[:-1])
        target = root.joinpath(*rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x=1\n", encoding="utf-8")
        (root / "requirements.txt").write_text("fastapi\nsqlalchemy\n", encoding="utf-8")
        assert summarize(root).has_runtime_paths is True


def test_heavy_db_in_devdependencies_does_not_mark_pending(tmp_path: Path) -> None:
    """BUG-350：重型库只看 dependencies；devDependencies 里的 redis 不应让纯前端 pending。"""
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "devDependencies": {
                    "vite": "^5.0.0",
                    "react": "^18.0.0",
                    "redis": "^4.6.0",
                },
                "scripts": {"build": "vite build"},
            }
        ),
        encoding="utf-8",
    )
    result = Scanner().detect(tmp_path)
    assert result.pending is False
    assert result.form == "frontend-static"
    assert not any("重型数据库" in n or "redis" in n for n in result.notes)


def test_heavy_db_in_dependencies_still_marks_pending(tmp_path: Path) -> None:
    """BUG-350 对照：dependencies 命中 redis 仍应 pending。"""
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"express": "^4.0.0", "redis": "^4.6.0"},
                "scripts": {"start": "node server.js"},
            }
        ),
        encoding="utf-8",
    )
    result = Scanner().detect(tmp_path)
    assert result.pending is True
    assert any("redis" in n for n in result.notes)


def test_detect_tornado_infers_port_8888(tmp_path: Path) -> None:
    """BUG-352：tornado 惯例内部端口为 8888，不得误推 8000。"""
    (tmp_path / "requirements.txt").write_text("tornado\n", encoding="utf-8")
    result = Scanner().detect(tmp_path)
    assert result.pending is False
    assert result.internalPort == 8888
    assert "tornado" in result.stack


# ---- IMP-058 Gate-A 回归测试 ------------------------------------------------
#
# A.10：现有项目类型经预检后不产生非预期修正或警告。


def test_preflight_static_project_no_issues(tmp_path: Path) -> None:
    """纯静态项目预检不修正、不警告。"""
    (tmp_path / "index.html").write_text("<html></html>")
    result = Scanner().detect(tmp_path)
    assert result.pending is False
    # 预检不应产生修正/警告 notes
    preflight_notes = [n for n in result.notes if n.startswith("[预检")]
    assert len(preflight_notes) == 0


def test_preflight_root_flask_no_issues(tmp_path: Path) -> None:
    """根目录 Flask 项目预检不修正、不警告。"""
    (tmp_path / "requirements.txt").write_text("flask\n")
    result = Scanner().detect(tmp_path)
    assert result.pending is False
    preflight_notes = [n for n in result.notes if n.startswith("[预检")]
    assert len(preflight_notes) == 0


def test_preflight_root_fastapi_no_issues(tmp_path: Path) -> None:
    """根目录 FastAPI 项目（无 alembic、无 Dockerfile）预检不修正、不警告。"""
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")
    (tmp_path / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    result = Scanner().detect(tmp_path)
    assert result.pending is False
    preflight_notes = [n for n in result.notes if n.startswith("[预检")]
    assert len(preflight_notes) == 0


def test_preflight_root_vite_no_issues(tmp_path: Path) -> None:
    """根目录 Vite 前端项目预检不修正、不警告。"""
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"react": "^18.0.0", "react-dom": "^18.0.0"},
                "scripts": {"build": "vite build"},
            }
        )
    )
    result = Scanner().detect(tmp_path)
    assert result.pending is False
    preflight_notes = [n for n in result.notes if n.startswith("[预检")]
    assert len(preflight_notes) == 0


def test_preflight_fastapi_alembic_autofix_note(tmp_path: Path) -> None:
    """FastAPI + alembic 项目：scanner 已包 sh -c，预检不重复修正。"""
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\nalembic\n")
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = alembic\n")
    result = Scanner().detect(tmp_path)
    assert result.pending is False
    # scanner 已用 sh -c 包裹，预检不应重复包裹（无 CHK-V02 修正）
    v02_notes = [n for n in result.notes if "[预检修正] CHK-V02" in n]
    assert len(v02_notes) == 0


def test_preflight_sqlite_autofix_note(tmp_path: Path) -> None:
    """A.R01：SQLite 项目有 DATABASE_URL 消费证据时预检标记修正。"""
    (tmp_path / "requirements.txt").write_text("fastapi\nsqlalchemy\n")
    (tmp_path / "app.db").write_bytes(b"")
    # A.R01：应用源码中读取 DATABASE_URL 环境变量
    (tmp_path / "config.py").write_text(
        "import os\nDATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///./app.db')\n",
        encoding="utf-8",
    )
    result = Scanner().detect(tmp_path)
    assert result.hasDatabase is True
    # CHK-V03 应标记修正（A.R01 确认消费后注入）
    v03_notes = [n for n in result.notes if "[预检修正] CHK-V03" in n]
    assert len(v03_notes) == 1


def test_preflight_sqlite_no_consumption_warning(tmp_path: Path) -> None:
    """A.R01：SQLite 项目无 DATABASE_URL 消费证据时预检标记警告，不注入。"""
    (tmp_path / "requirements.txt").write_text("fastapi\nsqlalchemy\n")
    (tmp_path / "app.db").write_bytes(b"")
    # 不写 config.py / 不读取 DATABASE_URL
    result = Scanner().detect(tmp_path)
    assert result.hasDatabase is True
    # CHK-V03 不应标记修正（A.R01 无消费证据）
    v03_notes = [n for n in result.notes if "[预检修正] CHK-V03" in n]
    assert len(v03_notes) == 0
    # 应标记警告
    v03_warnings = [n for n in result.notes if "[预检警告]" in n and "A.R01" in n]
    assert len(v03_warnings) == 1


def test_preflight_project_dockerfile_warning(tmp_path: Path) -> None:
    """项目自带 Dockerfile：预检标记警告。"""
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")
    (tmp_path / "Dockerfile").write_text("FROM python:3.12\n")
    result = Scanner().detect(tmp_path)
    warning_notes = [n for n in result.notes if "[预检警告]" in n]
    assert len(warning_notes) == 1
    assert "Dockerfile" in warning_notes[0]


# ---- IMP-057 Gate-1：Monorepo 包分类测试 -----------------------------------


def test_monorepo_npm_workspaces_web_server(tmp_path: Path) -> None:
    """npm workspaces monorepo：单 web_server -> 自动识别为主包。"""
    # 根 package.json with workspaces
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "monorepo-root",
                "private": True,
                "workspaces": ["packages/*"],
            }
        )
    )
    (tmp_path / "package-lock.json").write_text("{}")

    # webpage 包（web_server）
    webpage = tmp_path / "packages" / "webpage"
    webpage.mkdir(parents=True)
    (webpage / "package.json").write_text(
        json.dumps(
            {
                "name": "@app/webpage",
                "dependencies": {"express": "^4.18.0"},
                "scripts": {"start": "node server.js"},
            }
        )
    )

    # core 包（library）
    core = tmp_path / "packages" / "core"
    core.mkdir(parents=True)
    (core / "package.json").write_text(
        json.dumps(
            {
                "name": "@app/core",
                "main": "index.js",
            }
        )
    )

    # desktop 包（electron_desktop）
    desktop = tmp_path / "packages" / "desktop"
    desktop.mkdir(parents=True)
    (desktop / "package.json").write_text(
        json.dumps(
            {
                "name": "@app/desktop",
                "devDependencies": {"electron": "^30.0.0"},
            }
        )
    )

    result = Scanner().detect(tmp_path)
    assert result.pending is False
    assert result.kind == Kind.NODE
    assert result.runtime == Runtime.DOCKER_COMPOSE
    assert result.primary_package == "packages/webpage"
    assert len(result.classifications) == 3
    # entry uses -w
    assert result.entry.start is not None
    assert "-w @app/webpage" in result.entry.start
    assert result.entry.install == "npm ci"  # root lockfile


def test_detect_workspaces_skips_parent_escape(tmp_path: Path) -> None:
    """BUG-508：workspaces 指向仓库外目录时不得进入分类结果。"""
    from local_webpage_access.package_classifier import detect_workspaces

    root = tmp_path / "repo"
    shared = tmp_path / "shared"
    root.mkdir()
    shared.mkdir()
    (shared / "package.json").write_text("{}", encoding="utf-8")
    (root / "package.json").write_text(
        json.dumps({"private": True, "workspaces": ["../shared"]}),
        encoding="utf-8",
    )
    assert detect_workspaces(root) == []


def test_monorepo_no_deployable_packages(tmp_path: Path) -> None:
    """monorepo 无可部署子包 -> pending。"""
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "mono",
                "workspaces": ["packages/*"],
            }
        )
    )
    # 仅 library 和 electron
    core = tmp_path / "packages" / "core"
    core.mkdir(parents=True)
    (core / "package.json").write_text(json.dumps({"name": "@app/core", "main": "index.js"}))
    desktop = tmp_path / "packages" / "desktop"
    desktop.mkdir(parents=True)
    (desktop / "package.json").write_text(
        json.dumps({"name": "@app/desktop", "devDependencies": {"electron": "^30"}})
    )

    result = Scanner().detect(tmp_path)
    assert result.pending is True
    assert result.primary_package is None


def test_monorepo_two_web_servers_pending(tmp_path: Path) -> None:
    """两个 web_server -> pending。"""
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "mono", "workspaces": ["packages/*"]})
    )
    for name in ("api1", "api2"):
        pkg = tmp_path / "packages" / name
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text(
            json.dumps(
                {
                    "name": f"@app/{name}",
                    "dependencies": {"express": "^4.18.0"},
                    "scripts": {"start": "node server.js"},
                }
            )
        )

    result = Scanner().detect(tmp_path)
    assert result.pending is True
    assert any("2 个 web_server" in n for n in result.notes)


def test_monorepo_frontend_build_only(tmp_path: Path) -> None:
    """单 frontend_build（无 web_server）-> 选为 primary。"""
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "mono", "workspaces": ["packages/*"]})
    )
    web = tmp_path / "packages" / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text(
        json.dumps(
            {
                "name": "@app/web",
                "dependencies": {"react": "^18.0.0", "react-dom": "^18.0.0"},
                "scripts": {"build": "vite build"},
            }
        )
    )

    result = Scanner().detect(tmp_path)
    assert result.pending is False
    assert result.primary_package == "packages/web"
    assert result.form == "frontend-static"
    assert result.entry.build is not None
    assert "-w @app/web" in result.entry.build
    assert result.entry.buildOutputDir == "packages/web/dist"


def test_monorepo_workspace_name_is_one_shell_argument(tmp_path: Path) -> None:
    """package.json name 不能逃逸 npm ``-w`` 参数并注入 shell 命令。"""
    malicious_name = "web; id > /tmp/lwa-workspace-injection"
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "mono", "workspaces": ["packages/*"]})
    )
    web = tmp_path / "packages" / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text(
        json.dumps(
            {
                "name": malicious_name,
                "dependencies": {"react": "^18.0.0", "react-dom": "^18.0.0"},
                "scripts": {"build": "vite build"},
            }
        )
    )

    result = Scanner().detect(tmp_path)

    assert result.entry.build is not None
    assert shlex.split(result.entry.build) == ["npm", "run", "build", "-w", malicious_name]


def test_monorepo_vite_not_web_server(tmp_path: Path) -> None:
    """Vite 包不应因 scripts.dev 被误判为 web_server。"""
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "mono", "workspaces": ["packages/*"]})
    )
    vite_pkg = tmp_path / "packages" / "frontend"
    vite_pkg.mkdir(parents=True)
    (vite_pkg / "package.json").write_text(
        json.dumps(
            {
                "name": "@app/frontend",
                "dependencies": {"react": "^18.0.0", "react-dom": "^18.0.0"},
                "devDependencies": {"vite": "^5.0.0"},
                "scripts": {"dev": "vite", "build": "vite build"},
            }
        )
    )

    result = Scanner().detect(tmp_path)
    assert result.pending is False
    # 应为 frontend_build，不是 web_server
    cls = next(c for c in result.classifications if c.path == "packages/frontend")
    assert cls.packageType == "frontend_build"


def test_monorepo_non_monorepo_no_change(tmp_path: Path) -> None:
    """非 monorepo 项目不受影响。"""
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"express": "^4.18.0"},
                "scripts": {"start": "node server.js"},
            }
        )
    )
    result = Scanner().detect(tmp_path)
    assert result.pending is False
    assert result.primary_package is None
    assert len(result.classifications) == 0
    assert result.entry.start == "npm run start"


def test_monorepo_pure_electron_pending(tmp_path: Path) -> None:
    """纯 electron monorepo -> pending（无可部署包）。"""
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "mono", "workspaces": ["packages/*"]})
    )
    desktop = tmp_path / "packages" / "desktop"
    desktop.mkdir(parents=True)
    (desktop / "package.json").write_text(
        json.dumps(
            {
                "name": "@app/desktop",
                "devDependencies": {"electron": "^30.0.0"},
            }
        )
    )

    result = Scanner().detect(tmp_path)
    assert result.pending is True
    assert any("未发现 Web 可部署" in n for n in result.notes)


def test_monorepo_web_server_and_frontend_selects_web_server(tmp_path: Path) -> None:
    """web_server + frontend_build 共存时选 web_server。"""
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "mono", "workspaces": ["packages/*"]})
    )
    # web_server 包
    api = tmp_path / "packages" / "api"
    api.mkdir(parents=True)
    (api / "package.json").write_text(
        json.dumps(
            {
                "name": "@app/api",
                "dependencies": {"express": "^4.18.0"},
                "scripts": {"start": "node server.js"},
            }
        )
    )
    # frontend_build 包
    web = tmp_path / "packages" / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text(
        json.dumps(
            {
                "name": "@app/web",
                "dependencies": {"react": "^18.0.0"},
                "scripts": {"build": "vite build"},
            }
        )
    )

    result = Scanner().detect(tmp_path)
    assert result.pending is False
    assert result.primary_package == "packages/api"
    assert result.runtime == Runtime.DOCKER_COMPOSE


# ---- IMP-058 Gate-B：子目录布局识别 + 多候选测试 ----------------------------


def test_subdir_backend_frontend_layout(tmp_path: Path) -> None:
    """backend/+frontend/ 布局自动识别为 python 候选（DEV-105 / home-bookshelf 场景）。"""
    # backend 子目录：FastAPI + SQLite + alembic
    backend = tmp_path / "backend"
    backend.mkdir(parents=True)
    (backend / "requirements.txt").write_text("fastapi\nuvicorn\nsqlalchemy\nalembic\n")
    (backend / "app").mkdir()
    (backend / "app" / "__init__.py").write_text("")
    (backend / "app" / "main.py").write_text("app = None  # FastAPI app")

    # frontend 子目录：Vue + Vite
    frontend = tmp_path / "frontend"
    frontend.mkdir(parents=True)
    (frontend / "package.json").write_text(
        json.dumps(
            {
                "name": "frontend",
                "dependencies": {"vue": "^3.0.0"},
                "devDependencies": {"vite": "^5.0.0"},
                "scripts": {"build": "vite build", "dev": "vite"},
            }
        )
    )

    result = Scanner().detect(tmp_path)
    assert result.pending is False
    assert result.kind == Kind.PYTHON
    assert result.runtime == Runtime.DOCKER_COMPOSE
    assert result.source_subdir == "backend"
    assert result.form == "fullstack-sqlite"
    assert result.hasDatabase is True
    # 预检应检测到子目录
    assert any("[子目录识别]" in n for n in result.notes)
    # 候选列表应有 python primary + frontend alternate + static fallback
    assert len(result.candidates) >= 2


def test_subdir_backend_only_python(tmp_path: Path) -> None:
    """仅 backend/ 子目录有 Python 项目 -> 识别为 python 候选。"""
    backend = tmp_path / "backend"
    backend.mkdir(parents=True)
    (backend / "requirements.txt").write_text("flask\n")
    (backend / "app.py").write_text("app = None")

    result = Scanner().detect(tmp_path)
    assert result.pending is False
    assert result.kind == Kind.PYTHON
    assert result.source_subdir == "backend"


def test_subdir_server_python(tmp_path: Path) -> None:
    """server/ 子目录有 Python Web 框架 -> 识别为 python 候选。"""
    server = tmp_path / "server"
    server.mkdir(parents=True)
    (server / "requirements.txt").write_text("fastapi\nuvicorn\n")
    (server / "app").mkdir()
    (server / "app" / "__init__.py").write_text("")
    (server / "app" / "main.py").write_text("app = None")

    result = Scanner().detect(tmp_path)
    assert result.pending is False
    assert result.kind == Kind.PYTHON
    assert result.source_subdir == "server"


def test_subdir_non_web_python_not_selected(tmp_path: Path) -> None:
    """子目录有 Python 依赖但无 Web 框架 -> 不作为子目录候选，回退根目录。"""
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "requirements.txt").write_text("requests\nbeautifulsoup4\n")

    # 根目录有 index.html -> static
    (tmp_path / "index.html").write_text("<html></html>")

    result = Scanner().detect(tmp_path)
    # 无 Web 框架，不触发子目录识别 -> 回退 static
    assert result.kind == Kind.STATIC


def test_subdir_root_python_still_works(tmp_path: Path) -> None:
    """根目录有 requirements.txt + FastAPI -> 不触发子目录识别（根目录优先）。"""
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "main.py").write_text("app = None")

    result = Scanner().detect(tmp_path)
    assert result.pending is False
    assert result.kind == Kind.PYTHON
    assert result.source_subdir is None  # 根目录，无子目录


def test_subdir_evidence_collected(tmp_path: Path) -> None:
    """detect() 结果包含 ProjectEvidence。"""
    (tmp_path / "index.html").write_text("<html></html>")

    result = Scanner().detect(tmp_path)
    assert result.evidence is not None
    assert "index.html" in result.evidence.rootFiles


def test_subdir_candidates_generated(tmp_path: Path) -> None:
    """detect() 结果包含候选列表。"""
    (tmp_path / "requirements.txt").write_text("flask\n")
    (tmp_path / "app.py").write_text("app = None")

    result = Scanner().detect(tmp_path)
    assert len(result.candidates) >= 1
    # top-1 候选应为 python primary
    top = result.candidates[0]
    assert top.kind == "python"
    assert top.confidenceTier == "primary"


def test_subdir_dockerfile_copy_prefix(tmp_path: Path, workspace: Workspace) -> None:
    """sourceSubdir 设置时 Dockerfile COPY 路径含子目录前缀。"""
    from local_webpage_access.importer import build_manifest_from_detection
    from local_webpage_access.dockerfile_templates import generate_dockerfile

    # 构造子目录检测结果
    detection = DetectionResult()
    detection.kind = Kind.PYTHON
    detection.runtime = Runtime.DOCKER_COMPOSE
    detection.servingMode = ServingMode.CONTAINER
    detection.form = "backend-container"
    detection.resourceProfile = ResourceProfile.SMALL
    detection.entry = EntryConfig(
        install="pip install -r requirements.txt",
        start="uvicorn app.main:app --host 0.0.0.0 --port 8000",
    )
    detection.internalPort = 8000
    detection.source_subdir = "backend"
    detection.confidence = "high"

    manifest = build_manifest_from_detection(
        instance_id="test-subdir",
        display_name="test-subdir",
        detection=detection,
        workspace=workspace,
    )

    # 创建源码目录
    src_dir = workspace.app_current("test-subdir")
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "backend").mkdir(exist_ok=True)
    (src_dir / "backend" / "requirements.txt").write_text("flask\n")
    (src_dir / "backend" / "app.py").write_text("app = None")

    path = generate_dockerfile(manifest, workspace)
    content = path.read_text("utf-8")

    # COPY 路径应含 backend/ 前缀
    assert "COPY current/backend/requirements.txt requirements.txt" in content
    assert "COPY current/backend/ ./" in content


def test_subdir_no_subdir_uses_root_copy(tmp_path: Path, workspace: Workspace) -> None:
    """sourceSubdir 为 None 时 COPY 路径不含子目录前缀。"""
    from local_webpage_access.importer import build_manifest_from_detection
    from local_webpage_access.dockerfile_templates import generate_dockerfile

    detection = DetectionResult()
    detection.kind = Kind.PYTHON
    detection.runtime = Runtime.DOCKER_COMPOSE
    detection.servingMode = ServingMode.CONTAINER
    detection.form = "backend-container"
    detection.resourceProfile = ResourceProfile.SMALL
    detection.entry = EntryConfig(
        install="pip install -r requirements.txt",
        start="uvicorn app.main:app --host 0.0.0.0 --port 8000",
    )
    detection.internalPort = 8000
    detection.confidence = "high"

    manifest = build_manifest_from_detection(
        instance_id="test-nosubdir",
        display_name="test-nosubdir",
        detection=detection,
        workspace=workspace,
    )

    src_dir = workspace.app_current("test-nosubdir")
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "requirements.txt").write_text("flask\n")
    (src_dir / "app.py").write_text("app = None")

    path = generate_dockerfile(manifest, workspace)
    content = path.read_text("utf-8")

    # COPY 路径不含子目录前缀
    assert "COPY current/requirements.txt requirements.txt" in content
    assert "COPY current/ ./" in content
    assert "current/backend/" not in content


# ---- IMP-058 五层流水线咬合回归（BUG-495/496/497/498/499/500/501/502）-------


def test_subdir_frontend_vite_recognized_as_frontend_static(tmp_path: Path) -> None:
    """BUG-495：仅 frontend/ 子目录的 Vite/Vue 不得误判为 static（此前缺 npm build）。"""
    frontend = tmp_path / "frontend"
    frontend.mkdir(parents=True)
    (frontend / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"vue": "^3.0.0"},
                "devDependencies": {"vite": "^5.0.0"},
                "scripts": {"build": "vite build"},
            }
        )
    )
    result = Scanner().detect(tmp_path)
    assert result.pending is False
    assert result.kind == Kind.NODE
    assert result.runtime == Runtime.SHARED_STATIC
    assert result.form == "frontend-static"
    assert result.source_subdir == "frontend"
    assert result.entry.build == "npm run build"
    assert result.entry.buildOutputDir == "frontend/dist"


def test_subdir_node_backend_recognized(tmp_path: Path) -> None:
    """BUG-496：server/ Express 子目录有 node primary 候选，detect() 不得判 pending。"""
    server = tmp_path / "server"
    server.mkdir(parents=True)
    (server / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"express": "^4.0.0"},
                "scripts": {"start": "node server.js"},
            }
        )
    )
    (server / "server.js").write_text("// Express app")
    result = Scanner().detect(tmp_path)
    assert result.pending is False
    assert result.kind == Kind.NODE
    assert result.runtime == Runtime.DOCKER_COMPOSE
    assert result.form == "backend-container"
    assert result.source_subdir == "server"


def test_subdir_python_heavy_db_pending(tmp_path: Path) -> None:
    """BUG-497：子目录 Python 含重型数据库依赖（psycopg2）也要标记 pending。"""
    backend = tmp_path / "backend"
    backend.mkdir(parents=True)
    (backend / "requirements.txt").write_text("fastapi\nuvicorn\npsycopg2\n")
    result = Scanner().detect(tmp_path)
    assert result.pending is True
    assert result.confidence == "medium"
    assert any("psycopg2" in n for n in result.notes)
    assert result.kind == Kind.PYTHON
    assert result.source_subdir == "backend"


def test_detect_python_poetry_dependencies(tmp_path: Path) -> None:
    """BUG-502：仅 [tool.poetry.dependencies] 声明 FastAPI 应识别（不 pending）。"""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.poetry]\n"
        'name = "demo"\n'
        'version = "0.1.0"\n'
        "[tool.poetry.dependencies]\n"
        'python = "^3.11"\n'
        'fastapi = "^0.100"\n'
        'uvicorn = "^0.30"\n'
    )
    result = Scanner().detect(tmp_path)
    assert result.kind == Kind.PYTHON
    assert "fastapi" in result.stack
    assert result.pending is False
    assert result.confidence == "high"
    assert result.runtime == Runtime.DOCKER_COMPOSE


def test_preflight_rejected_marks_pending(tmp_path: Path, monkeypatch) -> None:
    """BUG-498：预检 REJECTED（如缺失 COPY 源）应置 pending 阻断导入，不得静默放行。"""
    from local_webpage_access import scanner as scanner_mod
    from local_webpage_access.preflight import REJECTED, PreflightResult

    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")
    (tmp_path / "main.py").write_text("app = None")

    def fake_preflight(result, source_dir):
        return PreflightResult(
            status=REJECTED,
            rejections=["COPY 源路径 requirements.txt 不存在，build 将失败"],
        )

    monkeypatch.setattr(scanner_mod, "_preflight_check", fake_preflight)
    result = Scanner().detect(tmp_path)
    assert result.pending is True
    assert any("[预检拒绝]" in n for n in result.notes)
