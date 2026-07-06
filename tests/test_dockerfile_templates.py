"""Dockerfile 模板测试（WBS-12）。"""

from __future__ import annotations

from local_webpage_access.dockerfile_templates import generate_dockerfile
from local_webpage_access.models import (
    ContainerConfig,
    DatabaseConfig,
    EntryConfig,
    InstanceManifest,
    Kind,
    ResourceProfile,
    Runtime,
    ServingMode,
)
from local_webpage_access.paths import Workspace


def _mk_manifest(
    *,
    mid: str = "api",
    kind: Kind = Kind.PYTHON,
    stack: list[str] | None = None,
    install: str | None = None,
    build: str | None = None,
    start: str | None = None,
    internal_port: int = 8000,
    has_database: bool = False,
    database_type: str | None = None,
) -> InstanceManifest:
    kwargs: dict = dict(
        id=mid,
        name=mid,
        version="1",
        kind=kind,
        stack=stack or [],
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.SMALL,
        container=ContainerConfig(
            projectName=f"lwa-{mid}",
            internalPort=internal_port,
            composePath="docker/compose.yaml",
            dockerfilePath="docker/Dockerfile",
        ),
        entry=EntryConfig(install=install, build=build, start=start),
        hasDatabase=has_database,
        database=DatabaseConfig(type=database_type) if has_database and database_type else None,
    )
    return InstanceManifest(**kwargs)


# ---- Node -------------------------------------------------------------------


def test_node_dockerfile_uses_alpine_and_npm_ci(workspace: Workspace) -> None:
    m = _mk_manifest(
        kind=Kind.NODE,
        stack=["express"],
        install="npm ci",
        start="npm run start",
        internal_port=3000,
    )
    path = generate_dockerfile(m, workspace)
    assert path == workspace.app_dockerfile_path("api")
    content = path.read_text(encoding="utf-8")
    assert "FROM node:24-alpine" in content
    assert "COPY current/package*.json ./" in content
    assert "RUN npm ci" in content
    assert "COPY current/ ./" in content
    assert "ENV PORT=3000" in content
    assert "EXPOSE 3000" in content
    assert 'CMD ["npm", "run", "start"]' in content


def test_node_dockerfile_includes_build_step_when_present(workspace: Workspace) -> None:
    m = _mk_manifest(
        kind=Kind.NODE,
        stack=["next"],
        install="npm ci",
        build="npm run build",
        start="npm run start",
        internal_port=3000,
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "RUN npm run build" in content


def test_node_dockerfile_supports_pnpm_lockfile(workspace: Workspace) -> None:
    """BUG-041：pnpm 项目应复制 pnpm-lock.yaml 并运行 pnpm install。"""
    m = _mk_manifest(
        kind=Kind.NODE,
        stack=["express"],
        install="corepack enable && pnpm install --frozen-lockfile",
        start="npm run start",
        internal_port=3000,
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "COPY current/package.json current/pnpm-lock.yaml ./" in content
    assert "RUN corepack enable && pnpm install --frozen-lockfile" in content
    assert "COPY current/package*.json ./" not in content


def test_node_dockerfile_supports_yarn_lockfile(workspace: Workspace) -> None:
    """BUG-041：yarn 项目应复制 yarn.lock 并运行 yarn install。"""
    m = _mk_manifest(
        kind=Kind.NODE,
        stack=["express"],
        install="corepack enable && yarn install --frozen-lockfile",
        start="npm run start",
        internal_port=3000,
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "COPY current/package.json current/yarn.lock ./" in content
    assert "RUN corepack enable && yarn install --frozen-lockfile" in content
    assert "COPY current/package*.json ./" not in content


def test_node_dockerfile_default_start_when_missing(workspace: Workspace) -> None:
    m = _mk_manifest(kind=Kind.NODE, install="npm install", start=None)
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert 'CMD ["node", "server.js"]' in content


# ---- Python: FastAPI / Flask / Django / Streamlit / Gradio -------------------


def test_python_fastapi_dockerfile(workspace: Workspace) -> None:
    m = _mk_manifest(
        kind=Kind.PYTHON,
        stack=["fastapi"],
        install="pip install -r requirements.txt",
        start="uvicorn main:app --host 0.0.0.0 --port 8000",
        internal_port=8000,
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "FROM python:3.13-slim" in content
    assert "COPY current/requirements.txt ./" in content
    assert "RUN pip install --no-cache-dir -r requirements.txt" in content
    assert "ENV PORT=8000" in content
    assert '["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]' in content


def test_python_flask_dockerfile(workspace: Workspace) -> None:
    m = _mk_manifest(
        kind=Kind.PYTHON,
        stack=["flask"],
        install="pip install -r requirements.txt",
        start="flask --app app run --host 0.0.0.0 --port 5000",
        internal_port=5000,
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "EXPOSE 5000" in content
    assert '["flask", "--app", "app", "run", "--host", "0.0.0.0", "--port", "5000"]' in content


def test_python_django_dockerfile(workspace: Workspace) -> None:
    m = _mk_manifest(
        kind=Kind.PYTHON,
        stack=["django"],
        install="pip install -r requirements.txt",
        start="python manage.py runserver 0.0.0.0:8000",
        internal_port=8000,
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert '["python", "manage.py", "runserver", "0.0.0.0:8000"]' in content


def test_python_streamlit_dockerfile(workspace: Workspace) -> None:
    m = _mk_manifest(
        kind=Kind.PYTHON,
        stack=["streamlit"],
        install="pip install -r requirements.txt",
        start="streamlit run app.py --server.port 8501",
        internal_port=8501,
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "EXPOSE 8501" in content
    assert '["streamlit", "run", "app.py", "--server.port", "8501"]' in content


def test_python_pyproject_install_path(workspace: Workspace) -> None:
    m = _mk_manifest(
        kind=Kind.PYTHON,
        stack=["fastapi"],
        install="pip install .",
        start="uvicorn main:app --host 0.0.0.0 --port 8000",
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    # pyproject 路径：先拷整个项目再装
    assert "COPY current/ ./" in content
    assert "RUN pip install --no-cache-dir ." in content


def test_python_uv_sync_path(workspace: Workspace) -> None:
    m = _mk_manifest(
        kind=Kind.PYTHON,
        stack=["fastapi"],
        install="uv sync",
        start="uvicorn main:app --host 0.0.0.0 --port 8000",
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "pip install --no-cache-dir uv && uv sync --frozen --no-dev" in content
    # 启动命令自动包 uv run
    assert '"uv", "run", "uvicorn"' in content


def test_python_pipfile_install_path(workspace: Workspace) -> None:
    """BUG-024：Pipfile-only 项目应复制 Pipfile 并走 pipenv 安装。"""
    m = _mk_manifest(
        kind=Kind.PYTHON,
        stack=["fastapi"],
        install="pip install pipenv && pipenv install --system --skip-lock",
        start="uvicorn main:app --host 0.0.0.0 --port 8000",
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "COPY current/Pipfile* ./" in content
    assert "pip install --no-cache-dir pipenv" in content
    assert "pipenv install --system --skip-lock" in content
    assert "COPY current/requirements.txt ./" not in content


# ---- SQLite 数据目录约定 -----------------------------------------------------


def test_sqlite_project_creates_data_dir(workspace: Workspace) -> None:
    m = _mk_manifest(
        kind=Kind.PYTHON,
        stack=["fastapi"],
        install="pip install -r requirements.txt",
        start="uvicorn main:app --host 0.0.0.0 --port 8000",
        has_database=True,
        database_type="sqlite",
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "RUN mkdir -p /app/data" in content
    assert "数据库：sqlite" in content


def test_non_sqlite_project_skips_data_dir(workspace: Workspace) -> None:
    m = _mk_manifest(
        kind=Kind.PYTHON,
        stack=["fastapi"],
        install="pip install -r requirements.txt",
        start="uvicorn main:app --host 0.0.0.0 --port 8000",
        has_database=False,
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "/app/data" not in content
    assert "数据库：无" in content


# ---- 输出路径与头部 ----------------------------------------------------------


def test_dockerfile_written_to_docker_dir(workspace: Workspace) -> None:
    m = _mk_manifest()
    path = generate_dockerfile(m, workspace)
    assert path.is_file()
    assert path.parent == workspace.app_docker("api")


def test_dockerfile_header_records_summary(workspace: Workspace) -> None:
    m = _mk_manifest(install="pip install -r requirements.txt", start="uvicorn main:app")
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "由 lwa 自动生成" in content
    assert "内部端口：8000" in content
    assert "模板：dockerfile_templates.py" in content
