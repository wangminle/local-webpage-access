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
import shlex
from pathlib import Path

from local_webpage_access.logging import get_logger
from local_webpage_access.models import InstanceManifest, Kind
from local_webpage_access.paths import Workspace

log = get_logger("dockerfile")

_NODE_IMAGE = "node:24-alpine"
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

    if manifest.kind == Kind.NODE:
        content = _render_node(manifest, internal_port)
    elif manifest.kind == Kind.PYTHON:
        content = _render_python(manifest, internal_port)
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


def _render_python(manifest: InstanceManifest, internal_port: int) -> str:
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
    else:
        # requirements.txt 路径（默认）
        req_copy = "COPY current/requirements.txt ./"
        install_block = (
            f"{req_copy}\n"
            f"RUN {install.replace('pip install', 'pip install --no-cache-dir', 1)}"
            + "\nCOPY current/ ./"
        )

    sqlite_mkdir = ""
    if _is_sqlite(manifest):
        sqlite_mkdir = "RUN mkdir -p /app/data\n"

    lines = [
        header,
        f"FROM {_PYTHON_IMAGE}",
        "WORKDIR /app",
        install_block,
        sqlite_mkdir,
        "ENV HOST=0.0.0.0",
        f"ENV PORT={internal_port}",
        f"EXPOSE {internal_port}",
        f"CMD {_to_exec_form(start)}",
    ]
    return "\n".join(line for line in lines if line) + "\n"


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
    """
    parts = shlex.split(shell_cmd)
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
