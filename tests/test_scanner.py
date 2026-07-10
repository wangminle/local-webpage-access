"""scanner 模块测试（WBS-08）。"""

from __future__ import annotations

import json
from pathlib import Path

from local_webpage_access.models import Kind, ResourceProfile, Runtime, ServingMode
from local_webpage_access.scanner import Scanner, summarize


# ---- FileSummary -----------------------------------------------------------


def test_summarize_picks_up_key_files(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html></html>")
    (tmp_path / "package.json").write_text(
        json.dumps(
            {"dependencies": {"express": "^4.0.0"}, "scripts": {"start": "node ."}}
        )
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


def test_detect_python_flask_port(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("flask\n")
    result = Scanner().detect(tmp_path)
    assert result.internalPort == 5000
    assert "flask" in result.entry.start


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


def test_detect_python_uv_lock_uses_uv_sync(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    (tmp_path / "uv.lock").write_text("")
    result = Scanner().detect(tmp_path)
    assert result.entry.install == "uv sync"


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
    (tmp_path / "Pipfile").write_text(
        '[packages]\n'
        'fastapi = "*"\n'
        'psycopg2 = "*"\n'
    )
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
        '[packages]\n'
        'flask = "*"\n\n'
        '[[source]]\n'
        'url = "https://pypi.org/simple"\n'
        'verify_ssl = true\n'
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
    (tmp_path / "Pipfile").write_text(
        '[packages]\n'
        'fastapi = "*"\n'
        'uvicorn = "*"\n'
    )
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
        '[project]\n'
        'name = "demo"\n'
        'dependencies = ["fastapi", "uvicorn"]\n'
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
        '[project]\n'
        'name = "demo"\n'
        'dependencies = ["fastapi>=0.100", "uvicorn[standard]"]\n'
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
