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
6. 写出前调用 ``audit_dockerfile``：``ADD <url>`` / 下载后直接解释执行链
   为 critical，拒绝落盘（与 ``generate_compose`` 对称）。
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
from pathlib import Path

from local_webpage_access.config import BuildMirrors, Config, default_config
from local_webpage_access.logging import get_logger
from local_webpage_access.models import InstanceManifest, Kind
from local_webpage_access.paths import Workspace

log = get_logger("dockerfile")

_NODE_IMAGE = "node:24-alpine"
# Python 全栈镜像内嵌 Node 官方二进制版本（与 _NODE_IMAGE major 对齐，OPS-001 / BUG-114）
_NODE_DIST_VERSION = "24.16.0"
_PYTHON_IMAGE = "python:3.13-slim"
_OFFICIAL_NODE_DIST = "https://nodejs.org/dist"

# 启动命令缺省时的兜底（与 scanner 推断保持一致）
_NODE_DEFAULT_START = "node server.js"
_PYTHON_DEFAULT_START = "python app.py"


def _copy_prefix(manifest: InstanceManifest) -> str:
    """Dockerfile COPY 源路径前缀（Gate-B：子目录布局支持）。

    根目录 -> ``current/``；子目录 -> ``current/<subdir>/``。
    非法 ``sourceSubdir``（绝对路径 / ``..``）回退根目录，避免 COPY 越界（BUG-507）。
    """
    from local_webpage_access.errors import PathError
    from local_webpage_access.paths import validate_source_subdir

    subdir = getattr(manifest, "sourceSubdir", None)
    if not subdir:
        return "current/"
    try:
        sanitized = validate_source_subdir(subdir)
    except PathError:
        return "current/"
    if sanitized:
        return f"current/{sanitized}/"
    return "current/"


# IMP-054：Python 包 → 所需 apt 系统库映射。
# 这些包的 wheel 不含系统共享库，运行时 import 会 ImportError；LWA 在
# 生成 Dockerfile 时自动追加 apt-get install，避免 rebuild 后丢失手动装的包。
# 键为 PyPI 包名（小写，去 extras）；值为 apt 包名列表。
_PYTHON_APT_DEPS: dict[str, list[str]] = {
    "pyzbar": ["libzbar0"],
    "opencv-python": ["libgl1", "libglib2.0-0"],
    "opencv-contrib-python": ["libgl1", "libglib2.0-0"],
    "opencv-python-headless": ["libgl1", "libglib2.0-0"],
    "python-magic": ["libmagic1"],
    "weasyprint": ["libpango-1.0-0", "libpangoft2-1.0-0"],
    "pycairo": ["libcairo2"],
    "mysqlclient": ["default-libmysqlclient-dev", "pkg-config"],
    "psycopg2": ["libpq-dev", "pkg-config"],
    "python-ldap": ["libldap2-dev", "libsasl2-dev"],
    "xmlsec": ["libxml2", "libxmlsec1-dev", "libxmlsec1-openssl", "pkg-config"],
}

_HEADER = """\
# 由 lwa 自动生成，请勿手动编辑（如需修改请交给 dockerize skill）。
# 模板：dockerfile_templates.py（{kind}）
# 内部端口：{internal_port}
# 安装命令：{install}
# 启动命令：{start}
# 数据库：{database}
"""


def generate_dockerfile(
    manifest: InstanceManifest,
    workspace: Workspace,
    *,
    config: Config | None = None,
) -> Path:
    """根据 manifest 渲染 Dockerfile 到 ``apps/<id>/docker/Dockerfile``（WBS-12.10）。

    ``config.buildMirrors``（BUG-200）控制 pip/npm/Node/apt 镜像；默认启用国内源，
    每次 regenerate 仍带镜像（BUG-201），无需手改 Dockerfile。

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
    mirrors = (config or default_config()).buildMirrors.resolved()

    if manifest.kind == Kind.NODE:
        content = _render_node(manifest, internal_port, mirrors=mirrors, source_dir=source_dir)
    elif manifest.kind == Kind.PYTHON:
        content = _render_python(manifest, internal_port, source_dir, mirrors=mirrors)
    else:
        # 容器实例只可能是 node/python；兜底用通用 shell 启动
        content = _render_generic(manifest, internal_port)

    # issue #20：runAsNonRoot=True 时末尾生成 USER（紧邻 CMD 之前）。
    # UID:GID 取宿主 data/ 属主（container_identity 统一解析，Compose 侧
    # 同源），数字形式无需镜像内建用户；HOME 指向可写目录，避免应用在
    # 只读 /root 下落缓存文件失败。issue #22：bind mount 的 data/ 运行时
    # 属主由宿主决定，但镜像内非挂载目录仍须在 USER 前 chown。
    from local_webpage_access.container_identity import resolve_container_identity

    identity = resolve_container_identity(manifest, workspace)
    if identity is not None:
        content = _insert_user_before_cmd(
            content,
            identity.docker_user(),
            chown_tree=_non_root_chown_tree(manifest, source_dir),
        )

    # 与 generate_compose 对称：写出前审计；pipe_to_shell / add_remote_url 为
    # critical，拒绝落盘（防止模板改动或 entry.install/build 注入供应链风险）。
    from local_webpage_access.security import audit_dockerfile, has_critical

    findings = audit_dockerfile(content)
    if has_critical(findings):
        codes = ", ".join(f.code for f in findings if f.level == "critical")
        raise RuntimeError(f"生成的 Dockerfile 含 critical 安全问题（{codes}），已拒绝写出")
    for f in findings:
        # issue #20：按 finding 级别分流——no_user 定义为 info，统一 warning
        # 会把 legacy root 实例的每次 rebuild 都刷成告警噪音。
        emit = log.warning if f.level == "warn" else log.info
        emit("Dockerfile 安全审计 [%s] %s", f.code, f.message)

    out_path.write_text(content, encoding="utf-8")
    # BUG-117：构建上下文是 apps/<id>/，.dockerignore 与 Dockerfile 一并生成。
    # BUG-128：有 build 步骤才排除 dist/build（构建会重生成）；否则保留预构建产物。
    has_build = bool(manifest.entry and manifest.entry.build)
    generate_dockerignore(workspace, manifest.id, exclude_build_artifacts=has_build)
    log.info("已生成 Dockerfile：%s", out_path)
    return out_path


_DOCKERIGNORE_BASE = """\
# 由 lwa 自动生成，请勿手动编辑。
# 构建上下文为 apps/<id>/（compose context: ..）。
**/node_modules
**/.git
**/__pycache__
**/*.py[cod]
**/.venv
**/venv
**/.env
**/.env.*
**/.pytest_cache
**/.mypy_cache
**/.ruff_cache
**/.DS_Store
source/
"""

# 仅当 Dockerfile 含构建步骤时追加：构建期会重新生成产物。
# 无 build 命令时保留 dist/build，避免丢掉预构建入口（BUG-128）。
# issue#6：模式相对构建上下文根 apps/<id>/，只排除源码树根级
# ``current/dist`` / ``current/build``；嵌套产物目录（如 current/backend/dist、
# current/skills/dist 等运行时产物）不再排除，避免破坏镜像内构建产物。
_DOCKERIGNORE_BUILD_ARTIFACTS = """\
current/dist
current/build
"""


def generate_dockerignore(
    workspace: Workspace,
    instance_id: str,
    *,
    exclude_build_artifacts: bool = False,
) -> Path:
    """写入 ``apps/<id>/.dockerignore``（BUG-117 / BUG-128）。"""
    path = workspace.app_dir(instance_id) / ".dockerignore"
    path.parent.mkdir(parents=True, exist_ok=True)
    content = _DOCKERIGNORE_BASE
    if exclude_build_artifacts:
        content += _DOCKERIGNORE_BUILD_ARTIFACTS
    path.write_text(content, encoding="utf-8")
    return path


# ---- Node -------------------------------------------------------------------


def _render_node(
    manifest: InstanceManifest,
    internal_port: int,
    *,
    mirrors: BuildMirrors | None = None,
    source_dir: Path | None = None,
) -> str:
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
    # issue#7：构建钩子在依赖安装/构建层之后、CMD 之前逐条执行（WORKDIR=/app）。
    hooks_block = _build_hooks_block(manifest)
    cpfx = _copy_prefix(manifest)
    dependency_copy = _node_dependency_copy_block(install, source_dir=source_dir, copy_prefix=cpfx)
    install_run = _with_npm_registry(install, mirrors)

    # BUG-122：安装/构建阶段不可设 NODE_ENV=production，否则 npm 会 omit
    # devDependencies，导致 tsc/vite 等构建工具缺失。运行期再切 production。
    lines = [
        header,
        f"FROM {_NODE_IMAGE}",
        "WORKDIR /app",
        dependency_copy,
        f"RUN {install_run}",
        f"COPY {cpfx} ./",
        build_step,
        hooks_block,
        "ENV NODE_ENV=production",
        "ENV HOST=0.0.0.0",
        f"ENV PORT={internal_port}",
        f"EXPOSE {internal_port}",
        f"CMD {_cmd_line(manifest, start)}",
    ]
    return "\n".join(line for line in lines if line) + "\n"


# ---- Python -----------------------------------------------------------------


# issue #18：pip 下载韧性——每源 retries/timeout；主源硬故障 || 切下一段。
# 默认值与 BuildMirrors.pipRetries / pipTimeout 对齐（可配置覆盖）。
_PIP_RETRIES = 3
_PIP_TIMEOUT = 60
# issue #18：依赖安装失败时的可行动提示（写入 build 日志，指向换源出口）。
_PIP_FAIL_HINT = (
    "lwa: 依赖安装失败。可能是镜像源不可达或过慢——可在 local-web.yml 的 "
    "buildMirrors 中调整 pip / pipFallbacks / pipRetries / pipTimeout，"
    "或 enabled: false 走官方 PyPI，然后重试构建"
)


def _pip_retry_flags(mirrors: BuildMirrors | None) -> str:
    """issue #18：每段 pip 安装的 ``--retries`` / ``--timeout``。"""
    retries = _PIP_RETRIES
    timeout = _PIP_TIMEOUT
    if mirrors is not None:
        retries = mirrors.pipRetries
        timeout = mirrors.pipTimeout
    return f"--retries {retries} --timeout {timeout}"


def _pip_index_sources(mirrors: BuildMirrors | None) -> list[str]:
    """issue #18：有序列表——主源 + pipFallbacks（去重、去空）。"""
    sources: list[str] = []
    if mirrors is None:
        return sources
    primary = (mirrors.pip or "").rstrip("/")
    if primary:
        sources.append(primary)
    for raw in getattr(mirrors, "pipFallbacks", None) or []:
        url = (raw or "").rstrip("/")
        if url and url not in sources:
            sources.append(url)
    return sources


def _join_index_attempts(attempts: list[str]) -> str:
    """多源用括号包每段，避免 ``&&`` / ``||`` 优先级把切源链拆乱。"""
    if len(attempts) <= 1:
        return attempts[0] if attempts else ""
    return " || ".join(f"( {a} )" for a in attempts)


def _pip_run(shell_cmd: str, *, mirrors: BuildMirrors | None = None) -> str:
    """把 pip/uv/pipenv 安装命令包成带 BuildKit cache mount 的 RUN（BUG-117）。

    有 cache mount 时去掉 ``--no-cache-dir``，让下载留在挂载缓存里跨构建复用。
    BUG-200：``mirrors.pip`` 非空时给 ``pip install`` 追加 ``-i``。
    BUG-207：仅给安装 uv/Pipenv 本体的 ``pip install`` 加镜像不够——实际解析项目
    依赖时 ``uv sync`` / ``pipenv install`` 仍访问官方 PyPI。故对 uv 段注入
    ``UV_DEFAULT_INDEX``、pipenv 段注入 ``PIPENV_PYPI_MIRROR``（uv / Pipenv 官方
    索引配置变量），使其依赖解析也走镜像。
    issue #18：每段恒注入 ``--retries/--timeout``；主源硬故障时 ``||`` 切
    ``pipFallbacks``（默认官方 → 腾讯）。不注入 ``--extra-index-url``——pip
    对同版本包仍走 ``-i`` 源，慢而不失败时 extra-index 不会切走。括号包裹
    不改变原命令的 ``&&`` 语义；失败时把换源出口打进 build 日志。
    """
    cmd = shell_cmd.replace("pip install --no-cache-dir", "pip install").strip()
    retry_flags = _pip_retry_flags(mirrors)
    sources = _pip_index_sources(mirrors)
    timeout = _PIP_TIMEOUT
    if mirrors is not None:
        timeout = mirrors.pipTimeout

    def _inject_segment(seg: str) -> str:
        seg = seg.strip()
        if not seg:
            return seg
        if seg.startswith("pip install"):
            flags = ""
            if "--retries" not in seg and "--timeout" not in seg:
                flags = f" {retry_flags}"
            if "-i " in seg or not sources:
                return f"{seg}{flags}"
            attempts = [f"{seg} -i {src}{flags}" for src in sources]
            return _join_index_attempts(attempts)
        if (seg.startswith("uv ") or seg == "uv") and "UV_DEFAULT_INDEX=" not in seg:
            if not sources:
                return f"UV_HTTP_TIMEOUT={timeout} {seg}"
            attempts = [
                f"UV_HTTP_TIMEOUT={timeout} UV_DEFAULT_INDEX={src} {seg}" for src in sources
            ]
            return _join_index_attempts(attempts)
        if seg.startswith("pipenv") and "PIPENV_PYPI_MIRROR=" not in seg:
            if not sources:
                return f"PIP_DEFAULT_TIMEOUT={timeout} {seg}"
            attempts = [
                f"PIP_DEFAULT_TIMEOUT={timeout} PIPENV_PYPI_MIRROR={src} {seg}"
                for src in sources
            ]
            return _join_index_attempts(attempts)
        return seg

    # 同时处理 ``&&`` / ``||`` / ``;`` 连接的多段命令（与 _with_npm_registry 对齐）
    def _split_inject(cmd_part: str, sep: str) -> str:
        parts = [_inject_segment(s) for s in cmd_part.split(sep)]
        return f" {sep} ".join(p for p in parts if p)

    and_parts: list[str] = []
    for and_seg in cmd.split("&&"):
        seg = and_seg
        if "||" in seg:
            seg = _split_inject(seg, "||")
        elif ";" in seg:
            seg = _split_inject(seg, ";")
        else:
            seg = _inject_segment(seg)
        and_parts.append(seg)
    cmd = " && ".join(p for p in and_parts if p)
    mounts = ["--mount=type=cache,target=/root/.cache/pip"]
    if "uv sync" in cmd or cmd.startswith("uv ") or "UV_DEFAULT_INDEX=" in cmd:
        mounts.append("--mount=type=cache,target=/root/.cache/uv")
    mount_prefix = " ".join(mounts)
    return (
        f"RUN {mount_prefix} \\\n"
        f"  ( {cmd} ) || ( echo '{_PIP_FAIL_HINT}' && exit 1 )"
    )


def _with_npm_registry(cmd: str, mirrors: BuildMirrors | None) -> str:
    """给 npm/pnpm/yarn 安装命令追加 registry（BUG-200）。"""
    if not mirrors or not mirrors.npm:
        return cmd
    registry = mirrors.npm.rstrip("/")

    def _one(segment: str) -> str:
        seg = segment.strip()
        if "--registry" in seg:
            return seg
        if "pnpm install" in seg:
            return seg.replace("pnpm install", f"pnpm install --registry={registry}", 1)
        if "yarn install" in seg:
            return seg.replace("yarn install", f"yarn install --registry={registry}", 1)
        for token in ("npm ci", "npm install"):
            if token in seg:
                return seg.replace(token, f"{token} --registry={registry}", 1)
        return seg

    if "||" in cmd:
        return " || ".join(_one(part) for part in cmd.split("||"))
    return _one(cmd)


_APT_MIRROR_HOST_RE = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)


def _validate_apt_mirror_host(host: str) -> str:
    """校验 aptMirror 为安全 hostname（拒绝 shell 元字符注入）。"""
    host = host.strip()
    if not host or not _APT_MIRROR_HOST_RE.fullmatch(host):
        raise ValueError(f"非法 aptMirror：{host!r}（仅允许 hostname，如 mirrors.aliyun.com）")
    return host


def _apt_mirror_prefix(mirrors: BuildMirrors | None) -> str:
    """在 apt-get 前切换 Debian 源（可选）。"""
    if not mirrors or not mirrors.aptMirror:
        return ""
    host = _validate_apt_mirror_host(mirrors.aptMirror)
    return (
        f"sed -i 's/deb.debian.org/{host}/g' "
        "/etc/apt/sources.list.d/debian.sources 2>/dev/null || "
        f"sed -i 's/deb.debian.org/{host}/g' /etc/apt/sources.list || true; \\\n  "
    )


def _node_dist_base(mirrors: BuildMirrors | None) -> str:
    if mirrors and mirrors.nodeDistBase:
        return mirrors.nodeDistBase.rstrip("/")
    return _OFFICIAL_NODE_DIST


def _parse_requirements_packages(req_text: str) -> set[str]:
    """从 requirements.txt 文本提取包名集合（小写、去 extras / 版本约束）。

    仅取每行第一个 token，忽略注释 / ``-r`` / ``-e`` / URL 行。
    """
    names: set[str] = set()
    for line in req_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        # 去掉环境标记与注释
        for sep in (";", "#"):
            idx = line.find(sep)
            if idx != -1:
                line = line[:idx].strip()
        if not line:
            continue
        # 取 ``package[extras]==1.0`` 的 package 部分
        pkg = re.split(r"[\[<>=!~\[\] ]", line, maxsplit=1)[0].strip().lower()
        # PEP 503：发行包名中的连字符、下划线、点号等价，映射键统一用 ``-``。
        pkg = re.sub(r"[-_.]+", "-", pkg)
        if pkg and not pkg.startswith(("http://", "https://", "git+", "file:")):
            names.add(pkg)
    return names


def _detect_apt_deps(source_dir: Path | None, install: str) -> list[str]:
    """扫描 requirements 文件，返回需要 apt-get install 的系统包列表（IMP-054）。

    读取 install 命令中指定的 requirements 文件（默认 requirements.txt），
    对照 ``_PYTHON_APT_DEPS`` 映射，合并去重后返回。
    """
    if source_dir is None:
        return []
    req_file = _extract_requirements_file(install)
    req_path = source_dir / req_file
    if not req_path.is_file():
        return []
    try:
        text = req_path.read_text(encoding="utf-8")
    except OSError:
        return []
    packages = _parse_requirements_packages(text)
    apt_deps: list[str] = []
    seen: set[str] = set()
    for pkg in sorted(packages):
        for apt in _PYTHON_APT_DEPS.get(pkg, ()):
            if apt not in seen:
                seen.add(apt)
                apt_deps.append(apt)
    return apt_deps


def _render_apt_deps_block(apt_deps: list[str], mirrors: BuildMirrors | None) -> str:
    """渲染 apt-get install 系统依赖的 RUN 指令（IMP-054）。

    与 node_toolchain 的 apt 块对称：使用 ``_apt_mirror_prefix``，
    ``--no-install-recommends``，结束后 ``rm -rf /var/lib/apt/lists/*``。
    """
    if not apt_deps:
        return ""
    apt_prefix = _apt_mirror_prefix(mirrors)
    pkgs = " ".join(apt_deps)
    return (
        "RUN set -eux; \\\n"
        f"  {apt_prefix}"
        "apt-get update; \\\n"
        f"  apt-get install -y --no-install-recommends {pkgs}; \\\n"
        "  rm -rf /var/lib/apt/lists/*\n"
    )


def _render_python(
    manifest: InstanceManifest,
    internal_port: int,
    source_dir: Path | None = None,
    *,
    mirrors: BuildMirrors | None = None,
) -> str:
    install = (manifest.entry.install or "").strip()
    start = (manifest.entry.start or _PYTHON_DEFAULT_START).strip()
    header = _HEADER.format(
        kind="python",
        internal_port=internal_port,
        install=install or "（零依赖，跳过依赖安装）",
        start=start,
        database=_database_label(manifest),
    )

    cpfx = _copy_prefix(manifest)
    uses_uv = bool(install) and "uv sync" in install
    # ``pip install .`` 需要完整源码，无法把依赖层与源码层完全拆开。
    needs_early_full_copy = False

    if not install:
        # CHK-225 高危1：stdlib 零依赖项目（scanner 置 install=None）不落任何
        # 依赖层。旧的 "pip install -r requirements.txt" 兜底会 COPY 不存在的
        # requirements.txt，docker build 必失败，而 CHK-V01 又恰好豁免 install=None，
        # 端到端无拦截。
        deps_block = ""
    elif uses_uv:
        # BUG-185：uv sync 默认会构建并安装项目本体，需要完整源码；但依赖层只 COPY 了
        # uv.lock+pyproject.toml，带 [build-system] 的 packaged 项目必然构建失败。
        # --no-install-project 只装依赖、不构建项目本体，源码在后续 final_copy 拷入，
        # 运行时 uv run 直接从工作目录导入（main:app 等模块无需作为包安装）。
        deps_block = (
            f"COPY {cpfx}uv.lock {cpfx}pyproject.toml ./\n"
            + _pip_run(
                "pip install uv && uv sync --frozen --no-dev --no-install-project",
                mirrors=mirrors,
            )
            + "\n"
        )
        run_prefix = "uv run "
        if not start.startswith("uv run"):
            start = run_prefix + start
    elif install.startswith("pip install ."):
        needs_early_full_copy = True
        deps_block = f"COPY {cpfx} ./\n" + _pip_run(install, mirrors=mirrors) + "\n"
    elif "pipenv" in install:
        install_cmd = install
        if install_cmd.startswith("pipenv "):
            install_cmd = f"pip install pipenv && {install_cmd}"
        install_cmd = install_cmd.replace("pip install --no-cache-dir", "pip install")
        deps_block = f"COPY {cpfx}Pipfile* ./\n" + _pip_run(install_cmd, mirrors=mirrors) + "\n"
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
        copy_lines.append(f"COPY {cpfx}{req_file} {req_file}")
        req_copy = "\n".join(copy_lines)
        if req_file == "requirements.txt":
            # IMP-017：无独立生产清单时，构建期就地剔除 pytest*（pytest/pytest-cov/
            # pytest-xdist 等含版本号或 extras 的行），让镜像不含测试包。
            # python:3.13-slim（Debian）自带 GNU sed，-E 用扩展正则。
            # BUG-360：须匹配 pytest[extras]、前导空白；旧正则漏掉 extras 写法。
            strip_step = f"RUN sed -i -E '/^\\s*pytest([-_]|[<>=!~\\[]|$)/d' {req_file}\n"
        else:
            # requirements-prod.txt 已是生产子集，无需剥离。
            strip_step = ""
        deps_block = (
            f"{req_copy}\n{strip_step}{_pip_run(f'pip install -r {req_file}', mirrors=mirrors)}\n"
        )
    else:
        # 兜底（无法解析的 install）：按 requirements.txt 处理
        fallback = install.replace("pip install --no-cache-dir", "pip install")
        if "pip install" in fallback and "--no-cache-dir" not in fallback:
            pass
        deps_block = (
            f"COPY {cpfx}requirements.txt ./\n" + _pip_run(fallback, mirrors=mirrors) + "\n"
        )

    # IMP-016（WBS-20260708 阶段2.5）：Python 全栈镜像含 Node 运行时。
    # 源码含 package.json（如 Pi Agent 这类 Python + 辅助 Node 项目）时，追加
    # Node.js/npm 与 Node 依赖安装，base 仍为 python:3.13-slim。
    #
    # 注意：不要用 Debian 的 ``apt install nodejs npm``——``npm`` 元包会拉入
    # webpack/terser 等约 300+ 依赖，在 Docker Desktop 默认内存下易 OOM
    # （cannot allocate memory）。改用官方 Node 二进制 tarball（含 npm）。
    #
    # BUG-117：Node 工具链与 npm ci 必须在完整 ``COPY current/`` 之前，
    # 否则任意源码改动都会打掉 Node 下载层（约 30MB）与 npm 依赖层。
    node_toolchain = ""
    npm_block = ""
    if source_dir is not None and (source_dir / "package.json").is_file():
        node_base = _node_dist_base(mirrors)
        apt_prefix = _apt_mirror_prefix(mirrors)
        node_toolchain = (
            "RUN set -eux; \\\n"
            f"  {apt_prefix}"
            "apt-get update; \\\n"
            "  apt-get install -y --no-install-recommends ca-certificates curl xz-utils; \\\n"
            '  ARCH="$(dpkg --print-architecture)"; \\\n'
            '  case "$ARCH" in amd64) NODE_ARCH=x64;; arm64) NODE_ARCH=arm64;;'
            ' *) echo "unsupported arch: $ARCH" >&2; exit 1;; esac; \\\n'
            "  curl -fsSL"
            f' "{node_base}/v{_NODE_DIST_VERSION}/'
            f'node-v{_NODE_DIST_VERSION}-linux-${{NODE_ARCH}}.tar.xz"'
            " | tar -xJ -C /usr/local --strip-components=1; \\\n"
            "  rm -rf /var/lib/apt/lists/*; \\\n"
            "  node -v && npm -v\n"
        )
        npm_install = _with_npm_registry("npm ci --omit=dev || npm install --omit=dev", mirrors)
        if needs_early_full_copy:
            # 源码已整包拷入，只需 npm 安装。
            npm_block = f"RUN {npm_install}\n"
        else:
            npm_block = f"COPY {cpfx}package*.json ./\nRUN {npm_install}\n"

    sqlite_mkdir = ""
    if _is_sqlite(manifest):
        # BUG-198：RUNTIME_ROOT 型应用写 /app/runtime/data；其余仍用 /app/data
        if _uses_runtime_root_layout(manifest, source_dir):
            sqlite_mkdir = "RUN mkdir -p /app/runtime/data\n"
        else:
            sqlite_mkdir = "RUN mkdir -p /app/data\n"

    # 常见 FastAPI 布局：入口在 src/main.py（如 start.sh 用 PYTHONPATH=src）。
    # exec 形式 CMD 无法携带 ``VAR=val`` 前缀，因此用 ENV 注入。
    pythonpath_env = ""
    if source_dir is not None and (source_dir / "src" / "main.py").is_file():
        pythonpath_env = "ENV PYTHONPATH=src\n"

    # IMP-054：探测 requirements.txt 中需要系统库的 Python 包，自动追加 apt-get install。
    # 放在 WORKDIR 之后、pip 依赖层之前，确保系统库先于 Python 包安装。
    apt_deps_block = _render_apt_deps_block(_detect_apt_deps(source_dir, install), mirrors)

    # 分层顺序：系统库 -> Node 工具链（最稳）-> Python 依赖 -> npm 依赖 -> 完整源码。
    final_copy = "" if needs_early_full_copy else f"COPY {cpfx} ./\n"
    # issue#7：构建钩子在依赖安装层之后、CMD 之前逐条执行（WORKDIR=/app，
    # 源码已由 final_copy / 早期整包 COPY 就位）。
    hooks_block = _build_hooks_block(manifest)

    lines = [
        header,
        f"FROM {_PYTHON_IMAGE}",
        "WORKDIR /app",
        apt_deps_block,
        node_toolchain,
        deps_block,
        npm_block,
        final_copy,
        sqlite_mkdir,
        hooks_block,
        pythonpath_env,
        "ENV HOST=0.0.0.0",
        f"ENV PORT={internal_port}",
        f"EXPOSE {internal_port}",
        f"CMD {_cmd_line(manifest, start)}",
    ]
    return "\n".join(line for line in lines if line) + "\n"


def _uses_runtime_root_layout(manifest: InstanceManifest, source_dir: Path | None) -> bool:
    """BUG-198：应用是否用 RUNTIME_ROOT / runtime/data 落库。

    与 :func:`local_webpage_access.compose.uses_runtime_root` 共用同一判定。
    """
    from local_webpage_access.compose import uses_runtime_root

    return uses_runtime_root(source_dir, manifest)


def _extract_requirements_file(install: str) -> str:
    """从 ``pip install -r <file>`` 命令解析 requirements 文件名（IMP-017）。

    返回 ``requirements.txt`` / ``requirements-prod.txt`` 等；解析失败回退
    ``requirements.txt``。文件名仅含字母数字与连字符/点，直接内插 Dockerfile 安全。
    支持 ``-r file`` 与无空格的 ``-rfile``（BUG-526）；不以 ``--registry`` 中的
    ``-r`` 子串误匹配。
    """
    import re

    m = re.search(r"(?:^|\s)-r\s*([A-Za-z0-9_./-]+)", install)
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
        f"COPY {_copy_prefix(manifest)} ./",
        # issue#7：与 node/python 渲染器对齐，支持构建钩子与启动前命令。
        _build_hooks_block(manifest),
        f"EXPOSE {internal_port}",
        f"CMD {_cmd_line(manifest, start)}",
    ]
    return "\n".join(line for line in lines if line) + "\n"


# ---- 辅助 --------------------------------------------------------------------


def _insert_user_before_cmd(
    content: str, docker_user: str, *, chown_tree: str | None = None
) -> str:
    """issue #20 / #22：把 chown（可选）+ ``ENV HOME=/tmp`` + ``USER`` 插在最后一条 CMD 之前。

    USER 影响其后所有 RUN/CMD/ENTRYPOINT 的执行身份；紧邻 CMD 放置可保证
    运行命令以非 root 执行，且不落在任何构建层（依赖安装需要 root）之后
    引发混淆。无 CMD（异常现场）时追加到文件末尾。

    issue #22：bind mount 的 ``data/`` 运行时属主由宿主决定，但镜像内非挂载
    目录（``/app/runtime`` 及兄弟子目录、WORKDIR ``/app``）构建期属主是
    root。``chown_tree`` 与 ``sqlite_mkdir`` 同源；无 sqlite 时回退 ``/app``。
    """
    lines = content.splitlines()
    user_lines = ["ENV HOME=/tmp", f"USER {docker_user}"]
    if chown_tree:
        user_lines.insert(0, f"RUN chown -R {docker_user} {chown_tree}")
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith("CMD "):
            lines[i:i] = user_lines
            return "\n".join(lines) + "\n"
    return content.rstrip("\n") + "\n" + "\n".join(user_lines) + "\n"


def _non_root_chown_tree(
    manifest: InstanceManifest, source_dir: Path | None
) -> str:
    """issue #22：非 root 镜像内需改属主的目录树，与 sqlite_mkdir 同源。

    RUNTIME_ROOT → ``/app/runtime``（覆盖 logs/secrets 等兄弟目录）；
    普通 sqlite → ``/app/data``；无 sqlite 时回退 ``/app``（三类模板都有 USER）。
    bind mount 覆盖 data/ 时再 chown 一次幂等无害。
    """
    if _is_sqlite(manifest):
        if _uses_runtime_root_layout(manifest, source_dir):
            return "/app/runtime"
        return "/app/data"
    return "/app"


# BUG-471：CMD/启动命令含 shell 操作符（&&、||、;、$()、``、裸 ()）时，
# shlex.split 会拆出无意义 token，生成的 exec 数组无法执行。
# 此类命令必须通过 ``sh -c`` 执行。
# IMP-058 Gate-A CHK-V02：文档 §6.4 明确要求检测裸 ``(`` ``)``（非 $() 形式），
# 如 ``docker-entrypoint.sh (alembic...)`` 会被 shlex 拆碎。
_SHELL_OPERATOR_RE = re.compile(r"&&|\|\||;|\$\(|`|\(|\)")


def _has_shell_operators(cmd: str) -> bool:
    """命令是否含 shell 操作符（需要通过 ``sh -c`` 执行）。"""
    return bool(_SHELL_OPERATOR_RE.search(cmd))


def _build_hooks_block(manifest: InstanceManifest) -> str:
    """issue#7：把 ``manifest.buildHooks`` 逐条渲染为 ``RUN <hook>``。

    钩子声明在 ``apps/<id>/local-web.json``，rebuild 重生成 Dockerfile 时保留
    （手工改 Dockerfile 会被抹掉）。换行符在 manifest 校验阶段已拒绝；含
    下载后直接解释执行的供应链风险指令会被
    ``audit_dockerfile`` 拒绝落盘。
    """
    return "".join(f"RUN {hook.strip()}\n" for hook in manifest.buildHooks if hook.strip())


def _cmd_line(manifest: InstanceManifest, start: str) -> str:
    """issue#7：渲染 CMD。声明了 ``preStart`` 时走 shell 形式
    ``sh -c "<preStart> && exec <start>"``（启动前命令失败则容器退出，``exec``
    保证主进程仍是 PID 1、信号传递正常）；未声明时保持 ``_to_exec_form``
    原逻辑（含 BUG-471 的 shell 操作符处理）不变。
    """
    pre_start = (manifest.preStart or "").strip()
    if pre_start:
        return json.dumps(["sh", "-c", f"{pre_start} && exec {start}"])
    return _to_exec_form(start)


def _to_exec_form(shell_cmd: str) -> str:
    """把 shell 命令字符串转成 Dockerfile exec 形式 ``["a", "b"]``。

    信号传递和参数安全都优于 shell 形式；scanner 推断的启动命令都是简单
    空格分隔，``shlex.split`` 足够。无法解析时回退到 shell 形式。

    前缀 ``KEY=VAL``（如 ``PYTHONPATH=src``）会从 exec 参数中剥离——exec
    形式不会像 shell 那样设置环境变量；这类变量应通过 ``ENV`` / compose
    ``environment`` 注入（见 ``_render_python`` / ``compose.generate_compose``）。
    """
    if _has_shell_operators(shell_cmd):
        # BUG-471：含 shell 操作符的命令必须用 sh -c 执行。
        # 但若命令已经是 ``sh -c "..."`` 形式（scanner 的 alembic 路径），
        # shlex.split 能正确拆出 ``["sh", "-c", "..."]``，无需再包一层。
        try:
            parts = shlex.split(shell_cmd)
            if len(parts) >= 2 and parts[0] == "sh" and parts[1] == "-c":
                return "[" + ", ".join(json.dumps(p) for p in parts) + "]"
        except ValueError:
            pass
        return json.dumps(["sh", "-c", shell_cmd])
    # BUG-359：未闭合引号等无法解析时回退 shell 形式（与空 parts 路径一致）。
    try:
        parts = shlex.split(shell_cmd)
    except ValueError:
        return json.dumps(shell_cmd)
    while parts and "=" in parts[0] and not parts[0].startswith("-"):
        # 仅剥离 ``NAME=value`` 形态；保留含 ``=`` 的普通参数极少见，且
        # 启动命令首段几乎总是解释器名。
        key, _, _ = parts[0].partition("=")
        if not key.isidentifier():
            break
        parts = parts[1:]
    if parts:
        return "[" + ", ".join(json.dumps(p) for p in parts) + "]"
    return f"{json.dumps(shell_cmd)}"


def _is_sqlite(manifest: InstanceManifest) -> bool:
    return bool(manifest.hasDatabase and manifest.database and manifest.database.type == "sqlite")


def _database_label(manifest: InstanceManifest) -> str:
    if not manifest.hasDatabase or manifest.database is None:
        return "无"
    return manifest.database.type


def _node_dependency_copy_block(
    install: str, *, source_dir: Path | None = None, copy_prefix: str = "current/"
) -> str:
    """复制与包管理器匹配的依赖声明文件。

    对于 npm workspaces monorepo（root package.json 含 ``"workspaces"``），
    ``npm ci --workspace=...`` 需要所有子包的 package.json 在场，否则 lock
    解析失败。检测到 workspaces 时追加 ``COPY current/packages/*/package.json``
    行（glob 在 Docker COPY 中逐条写出，保证仅复制声明文件而非源码）。
    """
    if "pnpm install" in install:
        return f"COPY {copy_prefix}package.json current/pnpm-lock.yaml ./"
    if "yarn install" in install:
        return f"COPY {copy_prefix}package.json current/yarn.lock ./"

    lines = [f"COPY {copy_prefix}package*.json ./"]

    # 检测 npm workspaces monorepo，追加子包 package.json
    if source_dir is not None:
        root_pkg = source_dir / "package.json"
        if root_pkg.is_file():
            try:
                pkg = json.loads(root_pkg.read_text(encoding="utf-8"))
                workspaces = pkg.get("workspaces")
                if workspaces:
                    patterns = (
                        workspaces
                        if isinstance(workspaces, list)
                        else workspaces.get("packages", [])
                    )
                    for pattern in patterns:
                        # 将 glob（如 "packages/*"）展开为实际目录
                        for pkg_dir in sorted(source_dir.glob(pattern)):
                            if (pkg_dir / "package.json").is_file():
                                rel = pkg_dir.relative_to(source_dir)
                                lines.append(
                                    f"COPY current/{posixpath.join(str(rel), 'package.json')} {posixpath.join(str(rel), 'package.json')}"
                                )
            except (json.JSONDecodeError, OSError):
                pass

    return "\n".join(lines)


__all__ = ["generate_dockerfile", "generate_dockerignore"]
