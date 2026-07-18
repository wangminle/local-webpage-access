"""Dockerfile 模板测试（WBS-12）。"""

from __future__ import annotations

import pytest

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
    assert "pnpm install" in content
    assert "--frozen-lockfile" in content
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
    assert "yarn install" in content
    assert "--frozen-lockfile" in content
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
    assert "COPY current/requirements.txt requirements.txt" in content
    assert "--mount=type=cache,target=/root/.cache/pip" in content
    assert "pip install -r requirements.txt" in content
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
    assert "pip install ." in content
    assert "--mount=type=cache,target=/root/.cache/pip" in content


def test_python_uv_sync_path(workspace: Workspace) -> None:
    m = _mk_manifest(
        kind=Kind.PYTHON,
        stack=["fastapi"],
        install="uv sync",
        start="uvicorn main:app --host 0.0.0.0 --port 8000",
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    # BUG-185：依赖层只 COPY lock+pyproject，uv sync 须 --no-install-project，
    # 否则带 [build-system] 的 packaged 项目构建本体时缺源码必然失败。
    assert "uv sync --frozen --no-dev --no-install-project" in content
    assert "pip install uv" in content
    assert "--mount=type=cache,target=/root/.cache/pip" in content
    assert "--mount=type=cache,target=/root/.cache/uv" in content
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
    assert "pip install pipenv" in content
    assert "pipenv install --system --skip-lock" in content
    assert "COPY current/requirements.txt ./" not in content


def test_python_uv_sync_injects_uv_default_index(workspace: Workspace) -> None:
    """BUG-207：uv sync 项目须注入 UV_DEFAULT_INDEX，否则依赖解析仍走官方 PyPI。"""
    workspace.ensure_app_dirs("api")
    m = _mk_manifest(install="uv sync", start="uvicorn main:app")
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    # uv 段带 UV_DEFAULT_INDEX=<pip 镜像>（uv 读该变量做依赖解析）
    assert "UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple uv sync" in content
    # 安装 uv 本体的 pip 段仍走 -i
    assert "pip install uv -i https://mirrors.aliyun.com/pypi/simple" in content
    # uv 缓存挂载保留
    assert "--mount=type=cache,target=/root/.cache/uv" in content


def test_python_pipenv_injects_pipenv_mirror(workspace: Workspace) -> None:
    """BUG-207：pipenv 项目须注入 PIPENV_PYPI_MIRROR，否则依赖解析仍走官方 PyPI。"""
    workspace.ensure_app_dirs("api")
    m = _mk_manifest(
        install="pipenv install --system --skip-lock",
        start="uvicorn main:app",
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "PIPENV_PYPI_MIRROR=https://mirrors.aliyun.com/pypi/simple pipenv install" in content
    # 安装 pipenv 本体的 pip 段仍走 -i
    assert "pip install pipenv -i https://mirrors.aliyun.com/pypi/simple" in content


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


def test_sqlite_runtime_paths_creates_runtime_data_dir(workspace: Workspace) -> None:
    """BUG-198：runtime_paths 布局 mkdir /app/runtime/data。"""
    workspace.ensure_app_dirs("api")
    rp = workspace.app_current("api") / "src" / "app"
    rp.mkdir(parents=True, exist_ok=True)
    (rp / "runtime_paths.py").write_text("x=1\n")
    m = _mk_manifest(
        kind=Kind.PYTHON,
        install="pip install -r requirements.txt",
        start="uvicorn main:app",
        has_database=True,
        database_type="sqlite",
    )
    m.database.dataDir = "runtime/data"
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "RUN mkdir -p /app/runtime/data" in content
    assert "RUN mkdir -p /app/data\n" not in content
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


# ---- IMP-016 / IMP-017：Python 全栈 Node + 生产依赖分离 --------------------


def test_dockerfile_python_with_node(workspace: Workspace) -> None:
    """IMP-016：Python 项目源码含 package.json → Dockerfile 追加 Node 官方二进制。"""
    workspace.ensure_app_dirs("api")
    (workspace.app_current("api") / "package.json").write_text(
        '{"name":"pi-agent","dependencies":{}}'
    )
    m = _mk_manifest(install="pip install -r requirements.txt", start="uvicorn main:app")
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    # base 仍为 python
    assert "FROM python:3.13-slim" in content
    # 追加 Node 工具链（默认国内 nodejs-release 镜像，BUG-200）
    assert "mirrors.aliyun.com/nodejs-release" in content
    # BUG-114：与纯 Node 基线 node:24-alpine 同一 major（非 v22）
    assert "/v24." in content
    assert "node-v24." in content
    assert "v22.19.0" not in content
    assert "npm ci" in content
    assert "--omit=dev" in content


def test_dockerfile_python_with_node_official_when_mirrors_disabled(
    workspace: Workspace,
) -> None:
    """BUG-200：buildMirrors.enabled=false 时回退官方 nodejs.org。"""
    from local_webpage_access.config import BuildMirrors, Config

    workspace.ensure_app_dirs("api")
    (workspace.app_current("api") / "package.json").write_text(
        '{"name":"pi-agent","dependencies":{}}'
    )
    m = _mk_manifest(install="pip install -r requirements.txt", start="uvicorn main:app")
    cfg = Config(buildMirrors=BuildMirrors(enabled=False, preset="none"))
    content = generate_dockerfile(m, workspace, config=cfg).read_text(encoding="utf-8")
    assert "nodejs.org/dist/v24." in content
    assert "mirrors.aliyun.com/nodejs-release" not in content
    assert "pip install -r requirements.txt" in content
    assert "-i https://mirrors.aliyun.com" not in content


def test_dockerfile_uses_china_mirrors_by_default(workspace: Workspace) -> None:
    """BUG-200 / BUG-201：默认注入 pip/npm 国内源，regenerate 仍带镜像。"""
    workspace.ensure_app_dirs("api")
    (workspace.app_current("api") / "package.json").write_text('{"name":"x"}')
    m = _mk_manifest(install="pip install -r requirements.txt", start="uvicorn main:app")
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "-i https://mirrors.aliyun.com/pypi/simple" in content
    assert "registry.npmmirror.com" in content
    # 再生成一次仍含国内源（不依赖手改 Dockerfile）
    content2 = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "-i https://mirrors.aliyun.com/pypi/simple" in content2
    assert "registry.npmmirror.com" in content2


def test_dockerfile_python_src_main_sets_pythonpath(workspace: Workspace) -> None:
    """src/main.py 布局 → Dockerfile 注入 PYTHONPATH=src；CMD 剥离 VAR= 前缀。"""
    workspace.ensure_app_dirs("api")
    src = workspace.app_current("api") / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "main.py").write_text("app = None\n")
    m = _mk_manifest(
        install="pip install -r requirements.txt",
        start="PYTHONPATH=src uvicorn main:app --host 0.0.0.0 --port 8000",
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "ENV PYTHONPATH=src" in content
    assert 'CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]' in content
    assert '"PYTHONPATH=src"' not in content


def test_dockerfile_python_without_node_omits_node_toolchain(
    workspace: Workspace,
) -> None:
    """IMP-016：无 package.json 的纯 Python 项目不加 Node（回归边界）。"""
    workspace.ensure_app_dirs("api")
    m = _mk_manifest(install="pip install -r requirements.txt", start="uvicorn main:app")
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "nodejs.org/dist" not in content
    assert "npm ci" not in content


def test_dockerfile_strips_pytest(workspace: Workspace) -> None:
    """IMP-017：仅 requirements.txt（无 prod 清单）→ 构建期 sed 剔除 pytest*。"""
    workspace.ensure_app_dirs("api")
    m = _mk_manifest(install="pip install -r requirements.txt", start="uvicorn main:app")
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "sed -i -E" in content
    assert "pytest" in content
    # 仍从 requirements.txt 安装（剥离后）
    assert "pip install -r requirements.txt" in content
    assert "--mount=type=cache,target=/root/.cache/pip" in content


def test_dockerfile_prefers_requirements_prod(workspace: Workspace) -> None:
    """IMP-017：requirements-prod.txt 路径 → 直接装 prod 清单，不剥离 pytest。"""
    workspace.ensure_app_dirs("api")
    (workspace.app_current("api") / "requirements-prod.txt").write_text("fastapi\n")
    m = _mk_manifest(
        install="pip install -r requirements-prod.txt", start="uvicorn main:app"
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "COPY current/requirements-prod.txt requirements-prod.txt" in content
    assert "pip install -r requirements-prod.txt" in content
    assert "--mount=type=cache,target=/root/.cache/pip" in content
    # prod 清单无需剥离
    assert "sed -i" not in content


# ---- BUG-083：嵌套 requirements 路径 COPY/RUN 一致 ------------------------


def test_dockerfile_nested_requirements_path_consistent(workspace: Workspace) -> None:
    """BUG-083：pip install -r requirements/prod.txt → COPY 保留嵌套路径 + mkdir 父目录。"""
    workspace.ensure_app_dirs("api")
    m = _mk_manifest(
        install="pip install -r requirements/prod.txt", start="uvicorn main:app"
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    # COPY 目标保留嵌套路径（不再平铺到 ./），且预先 mkdir 父目录
    assert "RUN mkdir -p requirements" in content
    assert "COPY current/requirements/prod.txt requirements/prod.txt" in content
    # RUN 安装路径与 COPY 落点一致
    assert "pip install -r requirements/prod.txt" in content
    assert "--mount=type=cache,target=/root/.cache/pip" in content
    # 不应出现平铺到根的旧写法（BUG-083 根因）
    assert "COPY current/requirements/prod.txt ./" not in content


def test_dockerfile_flat_requirements_no_mkdir(workspace: Workspace) -> None:
    """BUG-083：扁平 requirements.txt → COPY 到同名文件，无需 mkdir 父目录。"""
    workspace.ensure_app_dirs("api")
    m = _mk_manifest(
        install="pip install -r requirements.txt", start="uvicorn main:app"
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "COPY current/requirements.txt requirements.txt" in content
    # 扁平路径无父目录，不应插入 mkdir
    assert "mkdir -p requirements\n" not in content


# ---- BUG-117：Python 缓存分层 / pip cache mount / .dockerignore --------------


def test_python_node_toolchain_before_full_source_copy(workspace: Workspace) -> None:
    """BUG-117：Node 安装与 npm ci 必须在 COPY current/ ./ 之前，避免源码改动打掉缓存。"""
    workspace.ensure_app_dirs("api")
    (workspace.app_current("api") / "package.json").write_text(
        '{"name":"app","dependencies":{}}'
    )
    m = _mk_manifest(install="pip install -r requirements.txt", start="uvicorn main:app")
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    idx_node = content.find("nodejs-release")
    if idx_node < 0:
        idx_node = content.find("nodejs.org/dist")
    idx_npm = content.find("npm ci")
    # 取「完整源码拷贝」层：排除 package*.json / requirements 的局部 COPY
    idx_full = content.find("COPY current/ ./")
    assert idx_node >= 0
    assert idx_npm >= 0
    assert idx_full >= 0
    assert idx_node < idx_full
    assert idx_npm < idx_full


def test_python_pip_uses_buildkit_cache_mount(workspace: Workspace) -> None:
    """BUG-117：pip install 使用 BuildKit cache mount，避免每次全量重下。"""
    workspace.ensure_app_dirs("api")
    m = _mk_manifest(install="pip install -r requirements.txt", start="uvicorn main:app")
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "--mount=type=cache,target=/root/.cache/pip" in content
    assert "pip install -r requirements.txt" in content
    # 有 cache mount 时不再使用 --no-cache-dir（否则挂载缓存无效）
    assert "pip install --no-cache-dir" not in content


def test_generate_dockerfile_writes_dockerignore(workspace: Workspace) -> None:
    """BUG-117：构建上下文 apps/<id>/ 写入 .dockerignore，排除噪声目录。"""
    workspace.ensure_app_dirs("api")
    m = _mk_manifest(install="pip install -r requirements.txt", start="uvicorn main:app")
    generate_dockerfile(m, workspace)
    ignore = (workspace.app_dir("api") / ".dockerignore").read_text(encoding="utf-8")
    for pattern in ("**/node_modules", "**/.git", "**/__pycache__", "**/.env", "source/"):
        assert pattern in ignore


def test_node_production_env_only_after_install_and_build(
    workspace: Workspace,
) -> None:
    """BUG-122：开发依赖完成 install/build 后才切 NODE_ENV=production。"""
    workspace.ensure_app_dirs("api")
    m = _mk_manifest(
        kind=Kind.NODE,
        install="npm ci",
        build="npm run build",
        start="npm start",
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")

    env_index = content.index("ENV NODE_ENV=production")
    assert env_index > content.index("RUN npm ci")
    assert env_index > content.index("RUN npm run build")
    assert "ENV NODE_ENV=production" not in content[: content.index("RUN npm ci")]


def test_dockerignore_dist_only_for_build_entry(workspace: Workspace) -> None:
    """BUG-128：仅有 build 命令时排除宿主 dist/build 产物。"""
    workspace.ensure_app_dirs("with-build")
    with_build = _mk_manifest(
        mid="with-build",
        kind=Kind.NODE,
        install="npm ci",
        build="npm run build",
        start="npm start",
    )
    generate_dockerfile(with_build, workspace)
    ignore = (workspace.app_dir("with-build") / ".dockerignore").read_text()
    assert "**/dist" in ignore

    workspace.ensure_app_dirs("without-build")
    without_build = _mk_manifest(
        mid="without-build",
        kind=Kind.NODE,
        install="npm ci",
        start="npm start",
    )
    generate_dockerfile(without_build, workspace)
    ignore = (workspace.app_dir("without-build") / ".dockerignore").read_text()
    assert "**/dist" not in ignore


def test_pip_run_injects_mirror_for_semicolon_segments() -> None:
    """_pip_run 须同时处理 ``;`` 分隔段，不只 ``&&``。"""
    from local_webpage_access.config import BuildMirrors
    from local_webpage_access.dockerfile_templates import _pip_run

    mirrors = BuildMirrors(
        enabled=True,
        preset="china",
        pip="https://mirrors.aliyun.com/pypi/simple/",
    )
    out = _pip_run(
        "pip install pipenv ; pipenv install --system --skip-lock",
        mirrors=mirrors,
    )
    assert "pip install pipenv -i https://mirrors.aliyun.com/pypi/simple" in out
    assert "PIPENV_PYPI_MIRROR=https://mirrors.aliyun.com/pypi/simple" in out


def test_apt_mirror_rejects_shell_injection() -> None:
    from local_webpage_access.config import BuildMirrors
    from local_webpage_access.dockerfile_templates import _apt_mirror_prefix

    with pytest.raises(ValueError, match="非法 aptMirror"):
        _apt_mirror_prefix(
            BuildMirrors(enabled=True, aptMirror="evil.com/$(curl x)")
        )
    with pytest.raises(ValueError, match="非法 aptMirror"):
        _apt_mirror_prefix(BuildMirrors(enabled=True, aptMirror="a; rm -rf /"))
    ok = _apt_mirror_prefix(BuildMirrors(enabled=True, aptMirror="mirrors.aliyun.com"))
    assert "mirrors.aliyun.com" in ok
