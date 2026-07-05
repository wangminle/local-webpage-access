"""scanner 模块测试（WBS-08）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_web_access.models import Kind, ResourceProfile, Runtime, ServingMode
from local_web_access.scanner import FileSummary, Scanner, summarize


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
