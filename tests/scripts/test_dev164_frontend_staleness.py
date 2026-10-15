"""#61（检查部分 / DEV-164）：前端产物时效性分级检查——统一模块与三处接入。"""

from __future__ import annotations

import os
import time

import pytest
from pathlib import Path

from local_webpage_access.frontend_staleness import (
    LEVEL_CLEAN,
    LEVEL_SUSPECTED,
    LEVEL_UNKNOWN,
    check_frontend_artifacts_staleness,
)
from tests.scripts._helpers import make_container_manifest


@pytest.fixture()
def env(tmp_path):
    from local_webpage_access.init_workspace import init_workspace
    from local_webpage_access.paths import Workspace
    from local_webpage_access.registry import Registry

    root = tmp_path / "ws"
    init_workspace(root)
    ws = Workspace(root)
    reg = Registry(ws.db_path)
    reg.open()
    yield ws, None, reg
    reg.close()


def _mk(path: Path, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_suspected_when_source_newer(tmp_path: Path) -> None:
    old, new = time.time() - 3600, time.time()
    _mk(tmp_path / "frontend/package.json", old)
    _mk(tmp_path / "frontend/src/App.vue", new)
    _mk(tmp_path / "backend/static/index.html", old)
    verdict = check_frontend_artifacts_staleness(tmp_path)
    assert verdict.level == LEVEL_SUSPECTED
    assert "疑似" in verdict.message and "base" in verdict.suggestion


def test_clean_when_artifact_newer(tmp_path: Path) -> None:
    old, new = time.time() - 3600, time.time()
    _mk(tmp_path / "frontend/package.json", old)
    _mk(tmp_path / "frontend/src/main.ts", old)
    _mk(tmp_path / "backend/static/index.html", new)
    assert check_frontend_artifacts_staleness(tmp_path).level == LEVEL_CLEAN


def test_unknown_without_frontend_source(tmp_path: Path) -> None:
    """backend 形态：产物在树内、前端源在树外（跨仓）→ 无法判断。"""
    _mk(tmp_path / "backend/static/index.html")
    verdict = check_frontend_artifacts_staleness(tmp_path)
    assert verdict.level == LEVEL_UNKNOWN
    assert "前端源码不在导入树内" in verdict.message


def test_not_applicable_without_static_artifacts(tmp_path: Path) -> None:
    _mk(tmp_path / "frontend/package.json")
    assert check_frontend_artifacts_staleness(tmp_path).level is None


def test_node_modules_does_not_affect_staleness(tmp_path: Path) -> None:
    """依赖目录里更新的文件不得把产物判成疑似陈旧；目录不可读也不得中断检查。"""
    old, new = time.time() - 3600, time.time()
    _mk(tmp_path / "frontend/package.json", old)
    _mk(tmp_path / "frontend/src/App.vue", old)
    _mk(tmp_path / "frontend/node_modules/pkg/index.js", new)
    _mk(tmp_path / "backend/static/index.html", new)
    assert check_frontend_artifacts_staleness(tmp_path).level == LEVEL_CLEAN

    blocked = tmp_path / "frontend" / "node_modules"
    blocked.chmod(0)
    try:
        assert check_frontend_artifacts_staleness(tmp_path).level == LEVEL_CLEAN
    finally:
        blocked.chmod(0o755)


def test_unknown_when_mtimes_indistinguishable(tmp_path: Path) -> None:
    now = time.time()
    _mk(tmp_path / "frontend/package.json", now)
    _mk(tmp_path / "frontend/src/a.ts", now)
    _mk(tmp_path / "backend/static/index.html", now)
    assert check_frontend_artifacts_staleness(tmp_path).level == LEVEL_UNKNOWN


def test_doctor_check_warns_for_suspected(env) -> None:
    from local_webpage_access.doctor import (
        STATUS_WARN,
        check_frontend_artifacts_staleness as doc_check,
    )

    ws, _config, reg = env
    manifest = make_container_manifest("staleness")
    manifest.save(ws.app_manifest_path("staleness"))
    reg.upsert_from_manifest(manifest)
    current = ws.app_current("staleness")
    old, new = time.time() - 3600, time.time()
    _mk(current / "frontend/package.json", old)
    _mk(current / "frontend/src/App.vue", new)
    _mk(current / "static/index.html", old)

    r = doc_check(ws, reg)
    assert r.status == STATUS_WARN and "staleness" in r.message and "#61" in (r.suggestion or "")


def test_doctor_check_unknown_is_informational_only(env) -> None:
    from local_webpage_access.doctor import (
        check_frontend_artifacts_staleness as doc_check,
    )

    ws, _config, reg = env
    manifest = make_container_manifest("crossrepo")
    manifest.save(ws.app_manifest_path("crossrepo"))
    reg.upsert_from_manifest(manifest)
    _mk(ws.app_current("crossrepo") / "backend/static/index.html")

    r = doc_check(ws, reg)
    assert r.status == "ok" and "无法判定" in r.message


def test_scanner_notes_suspected_only(tmp_path: Path) -> None:
    from local_webpage_access.scanner import Scanner

    old, new = time.time() - 3600, time.time()
    _mk(tmp_path / "frontend/package.json", old)
    _mk(tmp_path / "frontend/src/main.ts", new)
    _mk(tmp_path / "backend/static/index.html", old)
    (tmp_path / "backend/requirements.txt").write_text("flask\n", encoding="utf-8")
    (tmp_path / "backend/app.py").write_text(
        "from flask import Flask\napp = Flask(__name__)\n", encoding="utf-8"
    )
    result = Scanner().detect(tmp_path)
    assert any(
        n.startswith("[预检警告] #61") and "疑似" in n for n in result.notes
    )
