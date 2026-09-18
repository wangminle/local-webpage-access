"""BUG-700（issue #42）：sourceSubdir 布局下前端自动构建目录与 COPY 前缀对齐。

前端目录探测扫仓库根（``current/frontend|web|client``），镜像内容物却由
``_copy_prefix`` 决定。前端子树在拷贝前缀之外时从未进入镜像，构建块接着
``WORKDIR /app/frontend && npm ci`` 只会命中空目录（EUSAGE）。三种位置关系：

- 前缀即前端目录（``sourceSubdir=frontend``）→ final_copy 已拷到 /app，构建
  目录用 /app；
- 前端在前缀之内（根布局）→ /app/<fe>，行为与 BUG-683/684 既有实现一致；
- 前端在前缀之外（``sourceSubdir=backend``）→ 显式 COPY 前端子树进镜像。
"""

from __future__ import annotations

import json

from tests._helpers import make_container_manifest

from local_webpage_access.models import ContainerConfig, Kind, ServingMode, Status
from local_webpage_access.paths import Workspace


def _seed(
    workspace: Workspace,
    registry,
    *,
    source_subdir: str | None,
) -> str:
    """按 sourceSubdir 布局种子化 Python 全栈实例，返回生成的 Dockerfile。"""
    from local_webpage_access.dockerfile_templates import generate_dockerfile

    iid = "bookshelf"
    workspace.ensure_app_dirs(iid)
    current = workspace.app_current(iid)
    app_dir = current / source_subdir if source_subdir else current
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "requirements.txt").write_text("flask\n", encoding="utf-8")
    (app_dir / "app.py").write_text("app=1\n", encoding="utf-8")
    # 前端子树固定在仓库根 current/frontend（_frontend_package_dir 的探测位置）
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


def test_frontend_outside_copy_prefix_gets_explicit_copy(workspace, registry) -> None:
    """BUG-700：sourceSubdir=backend 时前端在前缀之外，必须显式 COPY 进镜像。

    实战现场（home-bookshelf）：final_copy 只拷 current/backend/，前端子树
    不进镜像，npm ci 命中空目录报 EUSAGE。
    """
    docker = _seed(workspace, registry, source_subdir="backend")
    assert "COPY current/frontend/ /app/frontend/" in docker, "前端在前缀之外须显式 COPY"
    assert "WORKDIR /app/frontend" in docker
    assert "/app/frontend/dist" in docker, "dist 探测须用对齐后的构建目录"
    # 显式 COPY 必须在后端 final_copy 之后（依赖层分层顺序不破坏）
    assert docker.index("COPY current/backend/ ./") < docker.index(
        "COPY current/frontend/ /app/frontend/"
    )


def test_frontend_dir_equals_copy_prefix_builds_in_app(workspace, registry) -> None:
    """BUG-700：sourceSubdir 即前端目录时 final_copy 已拷到 /app，不得再进 /app/frontend。"""
    docker = _seed(workspace, registry, source_subdir="frontend")
    assert "COPY current/frontend/ /app/frontend/" not in docker, "final_copy 已覆盖，无需重复 COPY"
    assert "WORKDIR /app/frontend\n" not in docker, "构建目录应是 /app 而非不存在的 /app/frontend"
    assert "WORKDIR /app\n" in docker
    assert "/app/dist" in docker, "dist 探测用 /app/dist（final_copy 落点）"
    assert "RUN npm ci" in docker and "npm run build" in docker


def test_root_layout_frontend_build_unchanged(workspace, registry) -> None:
    """回归：根布局（无 sourceSubdir）行为与 BUG-683/684 既有实现一致。"""
    docker = _seed(workspace, registry, source_subdir=None)
    assert "COPY current/frontend/ /app/frontend/" not in docker, "根布局由 final_copy 覆盖"
    assert "WORKDIR /app/frontend" in docker
    assert "/app/frontend/dist" in docker
