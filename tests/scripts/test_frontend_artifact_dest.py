"""前端构建产物落点（#47 / PLN-052 S1-02）。

旧实现把产物写死为 ``/app/backend/static``。后端在镜像内的位置由拷贝前缀决定：
展平布局（``sourceSubdir=backend``）后端位于 ``/app``，根布局且有 ``backend/`` 时位于
``/app/backend``，其余情况位于 ``/app``。目标目录应为 ``<后端目录>/static``。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.scripts._helpers import make_container_manifest

from local_webpage_access.dockerfile_templates import generate_dockerfile
from local_webpage_access.models import ContainerConfig, Kind, ServingMode, Status
from local_webpage_access.paths import Workspace, frontend_artifact_dest


@pytest.mark.parametrize(
    ("copy_prefix", "has_backend_dir", "expected"),
    [
        ("current/backend/", False, "/app/static"),
        ("current/backend/", True, "/app/static"),
        ("current/", False, "/app/static"),
        ("current/", True, "/app/backend/static"),
        ("current/frontend/", True, "/app/static"),
    ],
)
def test_frontend_artifact_dest_follows_backend_location(
    tmp_path: Path, copy_prefix: str, has_backend_dir: bool, expected: str
) -> None:
    if has_backend_dir:
        (tmp_path / "backend").mkdir()
    assert frontend_artifact_dest(copy_prefix, tmp_path) == expected


def test_frontend_artifact_dest_without_source_dir_defaults_to_app() -> None:
    assert frontend_artifact_dest("current/", None) == "/app/static"


def _seed(workspace: Workspace, registry, *, source_subdir: str | None, backend_dir: bool) -> str:
    iid = "bookshelf"
    workspace.ensure_app_dirs(iid)
    current = workspace.app_current(iid)
    app_dir = current / source_subdir if source_subdir else (current / "backend" if backend_dir else current)
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "requirements.txt").write_text("flask\n", encoding="utf-8")
    (app_dir / "app.py").write_text("app=1\n", encoding="utf-8")
    frontend = current / "frontend"
    frontend.mkdir(parents=True, exist_ok=True)
    (frontend / "package.json").write_text(
        json.dumps({"scripts": {"build": "vite build"}}), encoding="utf-8"
    )
    (frontend / "package-lock.json").write_text('{"lockfileVersion":3}', encoding="utf-8")

    manifest = make_container_manifest(
        iid,
        kind=Kind.PYTHON,
        servingMode=ServingMode.CONTAINER,
        status=Status.STOPPED,
        sourceSubdir=source_subdir,
        container=ContainerConfig(
            projectName=f"lwa-{iid}",
            internalPort=8000,
            composePath="docker/compose.yaml",
            dockerfilePath="docker/Dockerfile",
        ),
    )
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)
    return generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")


def test_flattened_layout_copies_artifacts_to_app_static(workspace, registry) -> None:
    """sourceSubdir=backend：后端展平到 /app，产物应进 /app/static（旧实现写到 /app/backend/static）。"""
    docker = _seed(workspace, registry, source_subdir="backend", backend_dir=False)
    assert "mkdir -p /app/static && cp -a /app/frontend/dist/. /app/static/" in docker
    assert "/app/backend/static" not in docker
    assert 'echo "frontend artifacts -> /app/static"' in docker


def test_root_layout_with_backend_dir_copies_to_backend_static(workspace, registry) -> None:
    docker = _seed(workspace, registry, source_subdir=None, backend_dir=True)
    assert "cp -a /app/frontend/dist/. /app/backend/static/" in docker
    assert "/app/static" not in docker.replace("/app/backend/static", "")


def test_root_layout_without_backend_dir_copies_to_app_static(workspace, registry) -> None:
    docker = _seed(workspace, registry, source_subdir=None, backend_dir=False)
    assert "cp -a /app/frontend/dist/. /app/static/" in docker
    assert "/app/backend/static" not in docker


def test_artifact_copy_never_removes_existing_static(workspace, registry) -> None:
    """后端可能在 static/ 放有自身文件：产物拷贝只覆盖同名文件，不 rm -rf。"""
    docker = _seed(workspace, registry, source_subdir="backend", backend_dir=False)
    assert "rm -rf" not in docker
