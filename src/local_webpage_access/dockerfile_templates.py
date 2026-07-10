"""Dockerfile 模板体系（WBS-12）。

为 Node / Python 后端项目生成可审查、可修复的 Dockerfile，输出到
``apps/<id>/docker/Dockerfile``。

设计要点（对应 V1 设计说明第 13 节）：

1. 构建上下文是实例目录 ``apps/<id>/``（由 Compose 的 ``context: ..`` 指定），
   因此 ``COPY`` 源都从 ``current/`` 起算，不污染项目源码。
2. ``docker/`` 只存工具生成的运行配置，``Dockerfile`` 由本模块统一渲染。
3. 内部端口、启动命令、环境变量、SQLite 数据目录约定都通过 manifest 推断。
4. 生成的 Dockerfile 带注释头，记录模板来源和关键参数，方便 skill 二次修复。
5. SQLite 项目通过 Compose 的 ``env_file`` 注入 ``DATABASE_URL=sqlite:////app/data/app.sqlite``，
   Dockerfile 只负责约定 ``/app/data`` 目录存在（WBS-12.09）。
"""

from __future__ import annotations

import json
import posixpath
import shlex
from pathlib import Path

from local_webpage_access.logging import get_logger
from local_webpage_access.models import InstanceManifest, Kind
from local_webpage_access.paths import Workspace

log = get_logger("dockerfile")

_NODE_IMAGE = "node:24-alpine"
# Python 全栈镜像内嵌 Node 官方二进制版本（与 _NODE_IMAGE major 对齐，OPS-001 / BUG-114）
_NODE_DIST_VERSION = "24.16.0"
_PYTHON_IMAGE = "python:3.13-slim"

# 启动命令缺省时的兜底（与 scanner 推断保持一致）
_NODE_DEFAULT_START = "node server.js"
_PYTHON_DEFAULT_START = "python app.py"

_HEADER = """\
# 由 lwa 自动生成，请勿手动编辑（如需修改请交给 dockerize skill）。
# 模板：dockerfile_templates.py（{kind}）
# 内部端口：{internal_port}
# 安装命令：{install}
# 启动命令：{start}
# 数据库：{database}
"""


def generate_dockerfile(manifest: InstanceManifest, workspace: Workspace) -> Path:
    """根据 manifest 渲染 Dockerfile 到 ``apps/<id>/docker/Dockerfile``（WBS-12.10）。

    Returns:
        写入的 Dockerfile 路径。
    """
    container = manifest.container
    if container is None:
        raise ValueError(f"实例 {manifest.id} 缺少 container 配置，无法生成 Dockerfile")
    internal_port = container.internalPort
    out_path = workspace.app_dockerfile_path(manifest.id)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # 构建上下文是 apps/<id>/，业务源码在 current/（IMP-016/017 据此探测
    # package.json / requirements-prod.txt，决定是否追加 Node 工具链与剥离 pytest）。
    source_dir = workspace.app_current(manifest.id)

    if manifest.kind == Kind.NODE:
        content = _render_node(manifest, internal_port)
    elif manifest.kind == Kind.PYTHON:
        content = _render_python(manifest, internal_port, source_dir)
    else:
        # 容器实例只可能是 node/python；兜底用通用 shell 启动
        content = _render_generic(manifest, internal_port)

    out_path.write_text(content, encoding="utf-8")
    log.info("已生成 Dockerfile：%s", out_path)
    return out_path


# ---- Node -------------------------------------------------------------------


def _render_node(manifest: InstanceManifest, internal_port: int) -> str:
    install = (manifest.entry.install or "npm install").strip()
    start = (manifest.entry.start or _NODE_DEFAULT_START).strip()
    header = _HEADER.format(
        kind="node",
        internal_port=internal_port,
        install=install,
        start=start,
        database=_database_label(manifest),
    )
    build_step = ""
    if manifest.entry.build:
        build_step = f"RUN {manifest.entry.build}\n"
    dependency_copy = _node_dependency_copy_block(install)

    lines = [
        header,
        f"FROM {_NODE_IMAGE}",
        "WORKDIR /app",
        "ENV NODE_ENV=production",
        dependency_copy,
        f"RUN {install}",
        "COPY current/ ./",
        build_step,
        "ENV HOST=0.0.0.0",
        f"ENV PORT={internal_port}",
        f"EXPOSE {internal_port}",
        f"CMD {_to_exec_form(start)}",
    ]
    return "\n".join(line for line in lines if line) + "\n"


# ---- Python -----------------------------------------------------------------


def _render_python(
    manifest: InstanceManifest, internal_port: int, source_dir: Path | None = None
) -> str:
    install = (manifest.entry.install or "pip install -r requirements.txt").strip()
    start = (manifest.entry.start or _PYTHON_DEFAULT_START).strip()
    header = _HEADER.format(
        kind="python",
        internal_port=internal_port,
        install=install,
        start=start,
        database=_database_label(manifest),
    )

    uses_uv = install.startswith("uv sync") or "uv sync" in install
    if uses_uv:
        # uv 需要先安装到镜像，再用 uv sync 装依赖；启动用 uv run 进入虚拟环境
        install_block = (
            "COPY current/uv.lock current/pyproject.toml ./\n"
            "RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev\n"
            "COPY current/ ./"
        )
        run_prefix = "uv run "
        if not start.startswith("uv run"):
            start = run_prefix + start
    elif install.startswith("pip install ."):
        # pyproject 项目：先拷贝整个项目再装
        install_block = (
            "COPY current/ ./\n"
            f"RUN {install.replace('pip install', 'pip install --no-cache-dir', 1)}"
        )
    elif "pipenv" in install:
        install_cmd = install
        if install_cmd.startswith("pipenv "):
            install_cmd = f"pip install pipenv && {install_cmd}"
        install_block = (
            "COPY current/Pipfile* ./\n"
            f"RUN {install_cmd.replace('pip install', 'pip install --no-cache-dir', 1)}\n"
            "COPY current/ ./"
        )
    elif install.startswith("pip install -r"):
        # requirements 路径（默认 / requirements-prod.txt，IMP-017）。
        # 从 install 命令解析目标文件名，使 COPY 与 RUN 始终一致。
        req_file = _extract_requirements_file(install)
        # BUG-083：COPY 目标必须保留 requirements 文件的相对路径，否则嵌套路径
        # （如 requirements/prod.txt）会被平铺到工作目录根，pip 按原路径安装时找不到。
        # 显式 mkdir 父目录，保证 COPY 落点与 ``-r <req_file>`` 一致（不依赖 Docker
        # 对 dest 父目录的隐式创建行为，跨版本可预期）。
        req_dir = posixpath.dirname(req_file)
        copy_lines = [f"RUN mkdir -p {req_dir}"] if req_dir else []
        copy_lines.append(f"COPY current/{req_file} {req_file}")
        req_copy = "\n".join(copy_lines)
        if req_file == "requirements.txt":
            # IMP-017：无独立生产清单时，构建期就地剔除 pytest*（pytest/pytest-cov/
            # pytest-xdist 等含版本号或 extras 的行），让镜像不含测试包。
            # python:3.13-slim（Debian）自带 GNU sed，-E 用扩展正则。
            strip_step = (
                f"RUN sed -i -E '/^pytest([-_]|[<>=!~]|$)/d' {req_file}\n"
            )
            install_run = f"RUN pip install --no-cache-dir -r {req_file}"
        else:
            # requirements-prod.txt 已是生产子集，无需剥离。
            strip_step = ""
            install_run = f"RUN pip install --no-cache-dir -r {req_file}"
        install_block = f"{req_copy}\n{strip_step}{install_run}\nCOPY current/ ./"
    else:
        # 兜底（无法解析的 install）：按 requirements.txt 处理
        install_block = (
            "COPY current/requirements.txt ./\n"
            f"RUN {install.replace('pip install', 'pip install --no-cache-dir', 1)}"
            + "\nCOPY current/ ./"
        )

    # IMP-016（WBS-20260708 阶段2.5）：Python 全栈镜像含 Node 运行时。
    # 源码含 package.json（如 Pi Agent 这类 Python + 辅助 Node 项目）时，追加
    # Node.js/npm 与 Node 依赖安装，base 仍为 python:3.13-slim。
    #
    # 注意：不要用 Debian 的 ``apt install nodejs npm``——``npm`` 元包会拉入
    # webpack/terser 等约 300+ 依赖，在 Docker Desktop 默认内存下易 OOM
    #（cannot allocate memory）。改用官方 Node 二进制 tarball（含 npm）。
    node_block = ""
    if source_dir is not None and (source_dir / "package.json").is_file():
        node_block = (
            "RUN set -eux; \\\n"
            "  apt-get update; \\\n"
            "  apt-get install -y --no-install-recommends ca-certificates curl xz-utils; \\\n"
            "  ARCH=\"$(dpkg --print-architecture)\"; \\\n"
            "  case \"$ARCH\" in amd64) NODE_ARCH=x64;; arm64) NODE_ARCH=arm64;;"
            " *) echo \"unsupported arch: $ARCH\" >&2; exit 1;; esac; \\\n"
            "  curl -fsSL"
            f" \"https://nodejs.org/dist/v{_NODE_DIST_VERSION}/"
            f"node-v{_NODE_DIST_VERSION}-linux-${{NODE_ARCH}}.tar.xz\""
            " | tar -xJ -C /usr/local --strip-components=1; \\\n"
            "  rm -rf /var/lib/apt/lists/*; \\\n"
            "  node -v && npm -v\n"
            "COPY current/package*.json ./\n"
            "RUN npm ci --omit=dev || npm install --omit=dev\n"
        )

    sqlite_mkdir = ""
    if _is_sqlite(manifest):
        sqlite_mkdir = "RUN mkdir -p /app/data\n"

    # 常见 FastAPI 布局：入口在 src/main.py（如 start.sh 用 PYTHONPATH=src）。
    # exec 形式 CMD 无法携带 ``VAR=val`` 前缀，因此用 ENV 注入。
    pythonpath_env = ""
    if source_dir is not None and (source_dir / "src" / "main.py").is_file():
        pythonpath_env = "ENV PYTHONPATH=src\n"

    lines = [
        header,
        f"FROM {_PYTHON_IMAGE}",
        "WORKDIR /app",
        install_block,
        node_block,
        sqlite_mkdir,
        pythonpath_env,
        "ENV HOST=0.0.0.0",
        f"ENV PORT={internal_port}",
        f"EXPOSE {internal_port}",
        f"CMD {_to_exec_form(start)}",
    ]
    return "\n".join(line for line in lines if line) + "\n"


def _extract_requirements_file(install: str) -> str:
    """从 ``pip install -r <file>`` 命令解析 requirements 文件名（IMP-017）。

    返回 ``requirements.txt`` / ``requirements-prod.txt`` 等；解析失败回退
    ``requirements.txt``。文件名仅含字母数字与连字符/点，直接内插 Dockerfile 安全。
    """
    import re

    m = re.search(r"-r\s+([A-Za-z0-9_./-]+)", install)
    return m.group(1) if m else "requirements.txt"


# ---- 通用兜底 ----------------------------------------------------------------


def _render_generic(manifest: InstanceManifest, internal_port: int) -> str:
    start = (manifest.entry.start or "echo no start command").strip()
    header = _HEADER.format(
        kind=str(manifest.kind),
        internal_port=internal_port,
        install=manifest.entry.install or "(none)",
        start=start,
        database=_database_label(manifest),
    )
    lines = [
        header,
        f"FROM {_PYTHON_IMAGE}",
        "WORKDIR /app",
        "COPY current/ ./",
        f"EXPOSE {internal_port}",
        f"CMD {_to_exec_form(start)}",
    ]
    return "\n".join(lines) + "\n"


# ---- 辅助 --------------------------------------------------------------------


def _to_exec_form(shell_cmd: str) -> str:
    """把 shell 命令字符串转成 Dockerfile exec 形式 ``["a", "b"]``。

    信号传递和参数安全都优于 shell 形式；scanner 推断的启动命令都是简单
    空格分隔，``shlex.split`` 足够。无法解析时回退到 shell 形式。

    前缀 ``KEY=VAL``（如 ``PYTHONPATH=src``）会从 exec 参数中剥离——exec
    形式不会像 shell 那样设置环境变量；这类变量应通过 ``ENV`` / compose
    ``environment`` 注入（见 ``_render_python`` / ``compose.generate_compose``）。
    """
    parts = shlex.split(shell_cmd)
    while parts and "=" in parts[0] and not parts[0].startswith("-"):
        # 仅剥离 ``NAME=value`` 形态；保留含 ``=`` 的普通参数极少见，且
        # 启动命令首段几乎总是解释器名。
        key, _, _ = parts[0].partition("=")
        if not key.isidentifier():
            break
        parts = parts[1:]
    if parts:
        return "[" + ", ".join(json.dumps(p) for p in parts) + "]"
    return f'{json.dumps(shell_cmd)}'


def _is_sqlite(manifest: InstanceManifest) -> bool:
    return bool(
        manifest.hasDatabase
        and manifest.database
        and manifest.database.type == "sqlite"
    )


def _database_label(manifest: InstanceManifest) -> str:
    if not manifest.hasDatabase or manifest.database is None:
        return "无"
    return manifest.database.type


def _node_dependency_copy_block(install: str) -> str:
    """复制与包管理器匹配的依赖声明文件。"""
    if "pnpm install" in install:
        return "COPY current/package.json current/pnpm-lock.yaml ./"
    if "yarn install" in install:
        return "COPY current/package.json current/yarn.lock ./"
    return "COPY current/package*.json ./"


__all__ = ["generate_dockerfile"]
