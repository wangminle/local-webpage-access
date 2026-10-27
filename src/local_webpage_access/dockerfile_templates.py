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

import contextlib
import json
import posixpath
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from local_webpage_access.config import BuildMirrors, Config, default_config
from local_webpage_access.logging import get_logger
from local_webpage_access.models import InstanceManifest, Kind
from local_webpage_access.paths import Workspace, frontend_artifact_dest

log = get_logger("dockerfile")

_NODE_IMAGE = "node:24.22.1-alpine"
# Python 全栈镜像内嵌 Node 官方二进制版本（与 _NODE_IMAGE major 对齐，OPS-001 / BUG-114）
_NODE_DIST_VERSION = "24.22.1"
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
    # BUG-947：uwsgi 2.0.31 在没有 gcc 时把版本探测成 None 再 .split，构建崩溃。
    # Debian 13 基础镜像没有 libpcre3-dev，不把整个 apt 安装拖失败。
    "uwsgi": ["gcc", "g++", "make", "libc6-dev"],
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

    django_static: DjangoStaticRecipe | None = None
    if manifest.kind == Kind.NODE:
        content = _render_node(manifest, internal_port, mirrors=mirrors, source_dir=source_dir)
    elif manifest.kind == Kind.PYTHON:
        # BUG-997：Django 根 package.json 的前端构建 / collectstatic / 生产静态
        # 托管配方。只在契约完整时启用（manage.py + STATIC_ROOT + npm 锁 +
        # build 脚本），不猜测项目自定义输出目录。
        django_static = _detect_django_static_recipe(
            manifest, source_dir, copy_prefix=_copy_prefix(manifest)
        )
        content = _render_python(
            manifest,
            internal_port,
            source_dir,
            mirrors=mirrors,
            django_static=django_static,
        )
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
    # BUG-969：不在 Dockerfile 显式声明代理 ARG。代理经 compose build.args
    # 以 Docker 内置代理构建参数传入，默认排除在镜像历史与缓存之外；显式
    # ARG 会把代理地址与凭据留在 docker history 里。

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

    # helper 与 Dockerfile 共用审计边界；切换为 Node/通用形态也须在审计
    # 成功后才清理旧 helper，critical 拒绝时保留现有生成物。
    _sync_django_static_helper(workspace, manifest.id, django_static)
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


def _bun_global_install_line(
    install: str, source_dir: Path | None, copy_prefix: str
) -> str:
    """bun 安装命令的容器工具链层（评审 P2 / BUG-873 补齐）。

    node:24.22.1-alpine 基础镜像不含 bun——install 推断为 ``bun install
    --frozen-lockfile`` 时构建必在 RUN 处失败。经 ``npm install -g bun``
    安装（npm 包按平台自动选择 glibc/musl 与架构二进制），版本取工程根
    package.json ``packageManager`` 的 ``bun@<ver>``（未声明装最新）。
    ``+sha...`` 校验后缀会剥掉，与宿主 Corepack 解析一致。
    非 bun 安装返回空串（渲染逐字不变）。
    """
    if "bun install" not in install:
        return ""
    spec = "bun"
    if source_dir is not None:
        pkg_path = _node_lifecycle_root(source_dir, copy_prefix) / "package.json"
        try:
            pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pkg = {}
        pm = str(pkg.get("packageManager") or "")
        # BUG-893：Corepack 允许 "bun@1.2.3+sha512..."；npm install -g bun@<ver> 只接受纯版本号。
        ver = pm[len("bun@") :].split("+", 1)[0].strip() if pm.startswith("bun@") else ""
        # BUG-897：版本号来自导入的 package.json，直接拼进 RUN——只接受纯版本
        # 字符（与 node_toolchain.PM_VERSION_RE 同步），否则按未声明装最新 bun。
        if ver and re.fullmatch(r"[0-9A-Za-z._-]+", ver):
            spec = f"bun@{ver}"
    return f"RUN npm install -g {spec}"


def _is_yarn_berry_project(project_root: Path) -> bool:
    """Yarn berry（2+）项目：仓库自带 .yarn/releases 或 yarn.lock 含 __metadata（BUG-987）。"""
    if (project_root / ".yarn" / "releases").is_dir():
        return True
    lock = project_root / "yarn.lock"
    if lock.is_file():
        try:
            return "__metadata" in lock.read_text(encoding="utf-8", errors="ignore")[:4096]
        except OSError:
            return False
    return False


# Docker Hub major.minor moving tag 的"保证存在"下限：各 LTS 线已发布的最高
# 次版本（评审 P2：宽上界如 ``<=22.99.0`` 会把 hi 解析成次版本 99，拼出
# node:22.99-alpine 这类不存在的 tag，构建拉取直接 404）。表值是保守下限——
# 线上若有更新次版本，moving tag 依然可用；解析次版本超过表值时不拼钉版，
# 由调用方回退整条线 moving tag（真实最新次版本多半低于该上界；万一越界，
# 容器内 engines 校验诚实失败，好过一个必然 404 的 tag）。
_KNOWN_LATEST_NODE_MINOR = {24: 22, 22: 21, 20: 19, 18: 20}


def _minor_pin_tag(major: int, hi: tuple[int, int, int]) -> str | None:
    """线内上界 ``hi``（半开区间）对应的最高可用次版本 moving tag。

    上界为 ``<X.Y.0`` → 最高可用次版本 Y-1；``<X.Y.Z``（Z>0）→ Y。
    解析次版本超出该线已知发布的最高次版本时返回 None（评审 P2，见
    ``_KNOWN_LATEST_NODE_MINOR``），调用方回退整条线 moving tag。
    """
    minor = hi[1] - 1 if hi[2] == 0 else hi[1]
    if minor < 0 or minor > _KNOWN_LATEST_NODE_MINOR.get(major, -1):
        return None
    return f"node:{major}.{minor}-alpine"


def _engines_compat_image(engines: str, default_major: int) -> str | None:
    """默认镜像精确版本不满足 ``engines`` 时，按区间交集选兼容镜像标签（BUG-967）。

    依次考察默认 major 线与 22/20/18 线：交集在线内有上界时钉线内最高可用
    次版本标签（``>=24.0.0 <24.10.0`` → ``node:24.9-alpine``；``>=22.0.0
    <22.5.0`` → ``node:22.4-alpine``）；交集延伸到线尾时用整条线的 moving
    tag（``>=22.12.0 <23`` → ``node:22-alpine``）。默认 major 线交集延伸到
    线尾但精确默认版本不满足（下界高于默认版本，如 ``>24.22.1``）时该线
    moving tag 与默认同版本，跳过。均无可选返回 None。

    次版本标签（``node:24.9-alpine``）是 Docker Hub 的 major.minor moving
    tag：上界带补丁段（如 ``<22.5.3``）时解析到的最新补丁可能越界，此时
    容器内 engines 校验如实失败（诚实失败，不错装）；补丁级上界属稀有
    场景，不为它放弃 minor 钉版覆盖的主流成功案例。解析次版本超出该线
    已知发布的最高次版本时（``_KNOWN_LATEST_NODE_MINOR``，如宽上界
    ``<=22.99.0``）不拼必然 404 的 tag，回退整条线 moving tag（评审 P2）。
    """
    from local_webpage_access.node_toolchain import node_range_major_intersection

    for major in (default_major, 22, 20, 18):
        inter = node_range_major_intersection(engines, major)
        if inter is None or inter is False:
            continue
        _lo, hi = inter
        if hi is not None:
            tag = _minor_pin_tag(major, hi)
            if tag is not None:
                return tag
            # 上界超出已知次版本：默认 major 线的 moving tag 与默认镜像同
            # 版本（本就不满足 engines），跳过；其余线回退 moving tag。
            if major == default_major:
                continue
        elif major == default_major:
            # 默认 major 线交集延伸到线尾但精确默认版本不满足（下界高于
            # 默认版本）：moving tag 与默认同版本，跳过。
            continue
        return f"node:{major}-alpine"
    return None


def _node_image_for_engines(source_dir: Path | None, copy_prefix: str = "current/") -> str:
    """按 ``engines.node`` 选择 Node 基础镜像（BUG-967）。

    判定一律用真实完整版本/区间交集，不再用 ``major.0.0`` 代表整条线
    （CHK-485 实测两类误判：``>=22.12.0 <23`` 因 22.0.0 不满足而错回默认
    24 镜像；``>=24.0.0 <24.10.0`` 因 24.0.0 满足而错选实际不满足的
    24.22.1）。规则：

    1. 默认镜像 ``_NODE_IMAGE`` 的**精确版本**（如 24.22.1）满足 → 默认镜像；
    2. 不满足但与默认 major 线有交集 → 线内次版本钉版（见
       :func:`_engines_compat_image`）；
    3. 与 22/20/18 线有交集 → 对应 moving tag 或次版本钉版；
    4. 均不满足 → 保持默认镜像并告警，容器内 engines 校验如实失败；
       engines 缺失/不可解析同样保持默认（宁未知不误判）。

    BUG-987：Yarn berry 在 Node 24 镜像内 fetch 确定性崩溃
    （``The onCancel handler was attached after the promise settled``，
    HedgeDoc 实案：同源码/锁/yarn 4.13.0/镜像源/网络，node:24 两次复现、
    node:22 fetch 通过）。berry 项目选中默认镜像且 engines 未排除 22 时
    优先 node:22-alpine（上游 yarn/Node 24 兼容问题，产品侧规避）；
    与 22 线的交集在线内有上界时不盲用 moving tag，按上界钉线内最高
    可用次版本标签（评审 P2，同 :func:`_engines_compat_image` 规则）。
    """
    if source_dir is None:
        return _NODE_IMAGE
    from local_webpage_access.node_toolchain import (
        node_range_major_intersection,
        node_version_in_range,
        parse_node_version,
        read_node_engines,
    )

    project_root = _node_lifecycle_root(source_dir, copy_prefix)
    engines = read_node_engines(project_root)
    default_ver = parse_node_version(_NODE_IMAGE.split(":", 1)[1].split("-", 1)[0])
    if default_ver is None:
        return _NODE_IMAGE
    chosen = _NODE_IMAGE
    if engines and node_version_in_range(engines, default_ver) is False:
        picked = _engines_compat_image(engines, default_ver[0])
        if picked is not None:
            chosen = picked
        else:
            log.warning(
                "engines.node（%s）与默认镜像 %s 及候选 22/20/18 线均无交集；"
                "保持默认镜像，容器内 engines 校验将如实失败",
                engines,
                _NODE_IMAGE,
            )
    if chosen == _NODE_IMAGE and _is_yarn_berry_project(project_root):
        inter22 = node_range_major_intersection(engines, 22) if engines else None
        if not engines:
            return "node:22-alpine"
        if inter22:  # 与 22 线有交集的区间（None=不可解析 / False=无交集 → 保持 chosen）
            # 评审 P2：交集在线内有上界时 moving tag node:22-alpine 可能越界
            # （如 >=24 || >=22.0.0 <22.5.0：默认镜像满足但 berry 在 24 必崩，
            # 22.5+ 又违反声明）——与 _engines_compat_image 同规则钉线内次版本；
            # 解析次版本超出已知发布范围时回退 moving tag（不拼 404 tag）。
            _lo, hi = inter22
            if hi is not None:
                tag = _minor_pin_tag(22, hi)
                if tag is not None:
                    return tag
            return "node:22-alpine"
    return chosen


_APK_REPO_HOST = "dl-cdn.alpinelinux.org"
_APK_MIRROR_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+(:[0-9]{1,5})?$")


def _apk_add_block(system_pkgs: list[str], mirrors: BuildMirrors | None) -> str:
    """alpine 系统依赖安装层（BUG-988）。

    官方 ``dl-cdn.alpinelinux.org`` 在慢网络宿主上确定性装不完（实测 39 包
    30 分钟仅 11 个、两次撞构建超时；阿里镜像同批 7m22s）。resolved 镜像
    配置带 ``apkMirror`` 时在同一 RUN 内先把 ``/etc/apk/repositories`` 的
    仓库主机替换为镜像主机再安装——不额外加层。主机名做字符白名单校验，
    非法值不注入（宁慢不错）。无系统依赖时返回空串（渲染逐字不变）。
    """
    if not system_pkgs:
        return ""
    cmd = f"apk add --no-cache {' '.join(system_pkgs)}"
    host = (mirrors.apkMirror or "").strip() if mirrors else ""
    if host and host != _APK_REPO_HOST and _APK_MIRROR_HOST_RE.fullmatch(host):
        cmd = f"sed -i 's|{_APK_REPO_HOST}|{host}|g' /etc/apk/repositories && {cmd}"
    return f"RUN {cmd}\n"


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
    hooks_block = _build_hooks_block(manifest, mirrors=mirrors)
    sqlite_mkdir = ""
    if _is_sqlite(manifest):
        # Python 模板在 chown 前建数据目录。Node 非 root 同样 chown 该路径，
        # 源码未自带目录时 chown 会失败（BUG-906）。
        if _uses_runtime_root_layout(manifest, source_dir):
            sqlite_mkdir = "RUN mkdir -p /app/runtime/data\n"
        else:
            sqlite_mkdir = "RUN mkdir -p /app/data\n"
    cpfx = _copy_prefix(manifest)
    node_image = _node_image_for_engines(source_dir, cpfx)
    dependency_copy = _node_dependency_copy_block(install, source_dir=source_dir, copy_prefix=cpfx)
    install_run = _with_npm_registry(install, mirrors)
    # DOC-201（V0.9.7）：Node(alpine) 同样消费 systemDeps——node-gyp 等原生构建
    # 在 npm install 阶段就需要 python3/make/g++，必须先于依赖安装层；alpine
    # 用 apk。BUG-988：apk 层支持 buildMirrors.apkMirror 仓库主机替换（慢网络
    # 宿主官方 CDN 确定性超时）。为空时渲染保持逐字不变。
    system_pkgs = [str(p).strip() for p in (manifest.systemDeps or []) if str(p).strip()]
    # BUG-948：yarn/npm 的 git 依赖（如 github.com/...git）需要镜像内的 git。
    # 评审 P3：与 native 探测同口径走 _node_lifecycle_root——sourceSubdir
    # monorepo 的 git 依赖声明在子目录 package.json 时不能漏检。
    if (
        _node_source_needs_git(_node_lifecycle_root(source_dir, cpfx) if source_dir else None)
        and "git" not in system_pkgs
    ):
        system_pkgs.append("git")
    # BUG-986：原生模块（sqlite3 等）的 node-gyp 构建需要 python3/make/g++。
    if _node_source_needs_native_toolchain(_node_lifecycle_root(source_dir, cpfx) if source_dir else None):
        for pkg in ("python3", "make", "g++"):
            if pkg not in system_pkgs:
                system_pkgs.append(pkg)
    apk_block = _apk_add_block(system_pkgs, mirrors)
    # 评审 P2：bun 安装命令需先在镜像内补齐 bun 工具链（node:24.22.1-alpine 不含 bun）
    bun_toolchain = _bun_global_install_line(install, source_dir, cpfx)

    # BUG-122：安装/构建阶段不可设 NODE_ENV=production，否则 npm 会 omit
    # devDependencies，导致 tsc/vite 等构建工具缺失。运行期再切 production。
    lines = [
        header,
        f"FROM {node_image}",
        _build_env_arg_block(manifest),
        "WORKDIR /app",
        apk_block,
        bun_toolchain,
        dependency_copy,
        f"RUN {install_run}",
        f"COPY {cpfx} ./",
        build_step,
        hooks_block,
        sqlite_mkdir,
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
    "lwa: 依赖安装失败。若日志是超时、连接失败或找不到版本，可在 local-web.yml 的 "
    "buildMirrors 中调整 pip / pipFallbacks / pipRetries / pipTimeout，"
    "或 enabled: false 走官方 PyPI；若是编译错误（缺少 gcc 或系统库），换源不会解决"
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


_PNPM_YARN_INSTALL_RE = re.compile(r"\b(pnpm|yarn)(@[0-9A-Za-z._-]+)?\s+install\b")


def _is_pm_install(install: str, pm: str) -> bool:
    """安装命令是否为 ``<pm> install``（BUG-966：兼容钉版 ``pnpm@8.15.9 install``）。"""
    m = re.search(rf"\b{pm}(?:@[0-9A-Za-z._-]+)?\s+install\b", install)
    return m is not None


def _with_npm_registry(cmd: str, mirrors: BuildMirrors | None) -> str:
    """给 npm/pnpm/yarn/bun 安装命令追加 registry（BUG-200）。

    BUG-966：pnpm/yarn 兼容钉版形式（``corepack pnpm@8.15.9 install ...``），
    注入 ``--registry`` 时保留版本段。
    """
    if not mirrors or not mirrors.npm:
        return cmd
    registry = mirrors.npm.rstrip("/")

    def _one(segment: str) -> str:
        seg = segment.strip()
        if "--registry" in seg:
            return seg
        if "bun install" in seg:
            return seg.replace("bun install", f"bun install --registry={registry}", 1)
        m = _PNPM_YARN_INSTALL_RE.search(seg)
        if m:
            return f"{seg[:m.end()]} --registry={registry}{seg[m.end():]}"
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
    """在 apt-get 前切换 Debian 源（可选）。保留给单源路径与既有测试。"""
    if not mirrors or not mirrors.aptMirror:
        return ""
    host = _validate_apt_mirror_host(mirrors.aptMirror)
    return (
        f"sed -i 's/deb.debian.org/{host}/g' "
        "/etc/apt/sources.list.d/debian.sources 2>/dev/null || "
        f"sed -i 's/deb.debian.org/{host}/g' /etc/apt/sources.list || true; \\\n  "
    )


_APT_RETRIES = 2
_APT_TIMEOUT = 30
_APT_FAIL_HINT = (
    "lwa: apt 安装失败。可能是镜像源不可达或过慢——可在 local-web.yml 的 "
    "buildMirrors 中调整 aptMirror / aptFallbacks / aptRetries / aptTimeout / aptBudget，"
    "或把系统包装入 systemDeps 后重试"
)
_APT_CACHE_MOUNTS = (
    "--mount=type=cache,target=/var/cache/apt,sharing=locked "
    "--mount=type=cache,target=/var/lib/apt,sharing=locked"
)


def _apt_retry_opts(mirrors: BuildMirrors | None) -> str:
    retries = _APT_RETRIES
    timeout = _APT_TIMEOUT
    if mirrors is not None:
        retries = mirrors.aptRetries
        timeout = mirrors.aptTimeout
    return f"-o Acquire::Retries={retries} -o Acquire::http::Timeout={timeout}"


def _apt_budget(mirrors: BuildMirrors | None) -> int:
    """DEV-146：每个源候选的总时间预算（秒）；0 / 未配置 = 不限。"""
    return int(getattr(mirrors, "aptBudget", 0) or 0)


def _apt_budget_hint(budget: int) -> str:
    return (
        f"lwa: apt 在 {budget}s 总时间预算内未完成下载，放弃当前源并尝试下一个"
        "（可调 buildMirrors.aptBudget）"
    )


def _apt_body(install_args: str, mirrors: BuildMirrors | None) -> str:
    """单个源候选内执行的 ``apt-get update`` + ``install``。

    设置 ``aptBudget`` 后，网络阶段（update + ``--download-only``）整体放进
    ``timeout``，到点连同子进程一并终止并让 ``||`` 切下一个源；真正的 dpkg
    安装在下载完成后离线执行、不受预算约束——因此预算只会打断下载，
    不会把 dpkg 留在半安装状态。
    """
    opts = _apt_retry_opts(mirrors)
    budget = _apt_budget(mirrors)
    base = f"apt-get {opts} install -y --no-install-recommends"
    update = f"apt-get {opts} update"
    if budget <= 0:
        return f"{update} && {base} {install_args}"
    fetch = f"{update} && {base} --download-only {install_args}"
    hint = _apt_budget_hint(budget)
    # 124 = timeout 到点终止；137 = 宽限 10s 后被 KILL。其他失败码（源 503、
    # 解析失败等）原样透传，不误报“预算超时”。
    return (
        f"( timeout -k 10 {budget} sh -c {shlex.quote(fetch)} "
        f"|| ( rc=$?; if [ $rc -eq 124 ] || [ $rc -eq 137 ]; then "
        f"echo '{hint}' >&2; fi; exit $rc ) ) && "
        f"{base} {install_args}"
    )


def _apt_hosts(mirrors: BuildMirrors | None) -> list[str]:
    """主源 + aptFallbacks（去重、校验 hostname）。无配置时走官方源。"""
    hosts: list[str] = []
    if mirrors and mirrors.aptMirror:
        hosts.append(_validate_apt_mirror_host(mirrors.aptMirror))
    for raw in getattr(mirrors, "aptFallbacks", None) or []:
        host = _validate_apt_mirror_host(str(raw))
        if host not in hosts:
            hosts.append(host)
    return hosts or ["deb.debian.org"]


def _apt_switch_prefix(host: str) -> str:
    """从不可变官方源备份恢复后再切到 ``host``（官方源则只恢复）。"""
    host = _validate_apt_mirror_host(host)
    restore = (
        "cp /tmp/lwa-official-debian.sources /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; "
        "cp /tmp/lwa-official-sources.list /etc/apt/sources.list 2>/dev/null || true; "
    )
    if host == "deb.debian.org":
        return restore
    return (
        restore
        + f"sed -i 's/deb.debian.org/{host}/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || "
        f"sed -i 's/deb.debian.org/{host}/g' /etc/apt/sources.list || true; "
    )


def _render_apt_run(body: str, mirrors: BuildMirrors | None) -> str:
    """带 cache mount、快速失败重试与 || 切源的 apt RUN（issue #34 / #35）。

    官方源快照只写一次（``/tmp/lwa-official-*``），后续层不得用已切镜像覆盖。
    """
    hosts = _apt_hosts(mirrors)
    attempts = [f"( {_apt_switch_prefix(host)}{body} )" for host in hosts]
    chain = " || ".join(attempts) if len(attempts) > 1 else attempts[0]
    return (
        f"RUN {_APT_CACHE_MOUNTS} \\\n"
        "  set -eux; \\\n"
        "  rm -f /etc/apt/apt.conf.d/docker-clean; \\\n"
        '  echo \'Binary::apt::APT::Keep-Downloaded-Packages "true";\''
        " > /etc/apt/apt.conf.d/keep-cache; \\\n"
        "  if [ ! -f /tmp/lwa-official-debian.sources ]; then \\\n"
        "    cp /etc/apt/sources.list.d/debian.sources /tmp/lwa-official-debian.sources 2>/dev/null || true; \\\n"
        "    cp /etc/apt/sources.list /tmp/lwa-official-sources.list 2>/dev/null || true; \\\n"
        "  fi; \\\n"
        f"  ( {chain} ) || ( echo '{_APT_FAIL_HINT}' && exit 1 ); \\\n"
        "  cp /tmp/lwa-official-debian.sources /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \\\n"
        "  cp /tmp/lwa-official-sources.list /etc/apt/sources.list 2>/dev/null || true\n"
    )


def _render_apt_deps_block(apt_deps: list[str], mirrors: BuildMirrors | None) -> str:
    """渲染 apt-get install 系统依赖的 RUN 指令（IMP-054 / issue #35）。

    带 BuildKit apt cache mount、Acquire 重试/超时、aptFallbacks 切源。
    不再 ``rm -rf /var/lib/apt/lists``——lists 在 cache mount 内跨构建复用。
    """
    if not apt_deps:
        return ""
    pkgs = " ".join(apt_deps)
    body = _apt_body(f"--fix-missing {pkgs}", mirrors)
    return _render_apt_run(body, mirrors)


def _collect_apt_packages(
    manifest: InstanceManifest,
    source_dir: Path | None,
    install: str,
) -> list[str]:
    """自动探测 + ``systemDeps`` 去重合并。"""
    pkgs = _detect_apt_deps(source_dir, install)
    seen = set(pkgs)
    for name in getattr(manifest, "systemDeps", None) or []:
        item = str(name).strip()
        if item and item not in seen:
            seen.add(item)
            pkgs.append(item)
    return pkgs


_PYPROJECT_DEPS_EXTRACT = (
    "python -c 'import pathlib,tomllib;"
    "d=tomllib.loads(pathlib.Path(\"pyproject.toml\").read_text());"
    "pathlib.Path(\"/tmp/lwa-pyproject-deps.txt\").write_text("
    "chr(10).join((d.get(\"project\") or {}).get(\"dependencies\") or []))'"
)


_UNSAFE_PYPROJECT_DEP_RE = re.compile(
    r"""(?ix)
    @\s*(file:|https?://|git\+|ssh://|git@)
    | \bpath\s*=
    """
)


def _pyproject_split_safe(source_dir: Path | None) -> bool:
    """CHK-326：仅在 PEP 621 依赖清单完整且无路径/git/动态依赖时拆层。"""
    if source_dir is None:
        return False
    path = source_dir / "pyproject.toml"
    if not path.is_file():
        return False
    try:
        import tomllib

        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — 解析失败则不拆层
        return False
    project = data.get("project")
    if not isinstance(project, dict):
        return False
    dynamic = project.get("dynamic") or []
    if isinstance(dynamic, list) and "dependencies" in dynamic:
        return False
    deps = project.get("dependencies") or []
    if not isinstance(deps, list) or not deps:
        return False
    for item in deps:
        text = str(item)
        if _UNSAFE_PYPROJECT_DEP_RE.search(text) or text.strip().startswith((".", "/")):
            return False
    return True


def _is_plain_pip_install_dot(install: str) -> bool:
    """CHK-326 / BUG-660：仅精确 ``pip install .`` 才拆层，保留 extras 与后续命令。"""
    return install.strip() == "pip install ."


def _pip_dot_layers(
    cpfx: str,
    install: str,
    source_dir: Path | None,
    mirrors: BuildMirrors | None,
) -> tuple[str, bool]:
    """``pip install .``：清单完整且可拆时先装依赖，否则整包 COPY。"""
    if not _pyproject_split_safe(source_dir):
        return f"COPY {cpfx} ./\n" + _pip_run(install, mirrors=mirrors) + "\n", True
    deps_block = (
        f"COPY {cpfx}pyproject.toml ./\n"
        + _pip_run(
            f"{_PYPROJECT_DEPS_EXTRACT} && pip install -r /tmp/lwa-pyproject-deps.txt",
            mirrors=mirrors,
        )
        + "\n"
    )
    return deps_block, False


def _node_dist_base(mirrors: BuildMirrors | None) -> str:
    if mirrors and mirrors.nodeDistBase:
        return mirrors.nodeDistBase.rstrip("/")
    return _OFFICIAL_NODE_DIST


_NPMMIRROR_NODE_DIST = "https://npmmirror.com/mirrors/node"


def _node_dist_bases(mirrors: BuildMirrors | None) -> list[str]:
    """主 nodeDistBase + 国内/官方兜底，供 tarball 下载（BUG-670）。"""
    bases: list[str] = []
    primary = _node_dist_base(mirrors)
    if primary:
        bases.append(primary.rstrip("/"))
    extras: tuple[str, ...]
    if mirrors and mirrors.enabled and getattr(mirrors, "preset", "") == "china":
        extras = (_NPMMIRROR_NODE_DIST, _OFFICIAL_NODE_DIST)
    else:
        extras = (_OFFICIAL_NODE_DIST,)
    for extra in extras:
        extra = extra.rstrip("/")
        if extra not in bases:
            bases.append(extra)
    return bases or [_OFFICIAL_NODE_DIST]


def _render_node_tarball_run(mirrors: BuildMirrors | None) -> str:
    """与 apt 切源链分离：每次尝试使用不同的 Node 发行包 URL。"""
    curls = []
    for base in _node_dist_bases(mirrors):
        url = (
            f"{base}/v{_NODE_DIST_VERSION}/"
            f"node-v{_NODE_DIST_VERSION}-linux-${{NODE_ARCH}}.tar.xz"
        )
        curls.append(f'curl -fsSL "{url}"')
    chain = " || ".join(curls)
    hint = (
        "Node 发行包下载失败，请在 local-web.yml 调整 buildMirrors.nodeDistBase"
        "（与 aptMirror 无关）"
    )
    return (
        "RUN set -eux; \\\n"
        '  ARCH="$(dpkg --print-architecture)"; \\\n'
        '  case "$ARCH" in amd64) NODE_ARCH=x64;; arm64) NODE_ARCH=arm64;;'
        ' *) echo "unsupported arch: $ARCH" >&2; exit 1;; esac; \\\n'
        f"  ( {chain} ) | tar -xJ -C /usr/local --strip-components=1 || "
        f"( echo '{hint}' && exit 1 ); \\\n"
        "  node -v && npm -v\n"
    )


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


def _python_packages_from_project_files(source_dir: Path) -> set[str]:
    """从 pyproject 依赖和 uv.lock 的包名收集 Python 发行包（BUG-947）。"""
    names: set[str] = set()
    pyproject = source_dir / "pyproject.toml"
    if pyproject.is_file():
        try:
            import tomllib

            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            data = {}
        for dep in (data.get("project") or {}).get("dependencies") or []:
            if isinstance(dep, str):
                names |= _parse_requirements_packages(dep)
    lock = source_dir / "uv.lock"
    if lock.is_file():
        try:
            text = lock.read_text(encoding="utf-8")
        except OSError:
            text = ""
        for match in re.finditer(r'(?m)^name = "([^"]+)"', text):
            pkg = re.sub(r"[-_.]+", "-", match.group(1).strip().lower())
            if pkg:
                names.add(pkg)
    return names


def _detect_apt_deps(source_dir: Path | None, install: str) -> list[str]:
    """扫描依赖声明，返回需要 apt-get install 的系统包列表（IMP-054）。

    读取 install 命令中指定的 requirements 文件（默认 requirements.txt），
    并合并 pyproject / uv.lock 中的包名。对照 ``_PYTHON_APT_DEPS`` 去重后返回。
    """
    if source_dir is None:
        return []
    packages: set[str] = set()
    req_file = _extract_requirements_file(install)
    req_path = source_dir / req_file
    if req_path.is_file():
        try:
            packages |= _parse_requirements_packages(req_path.read_text(encoding="utf-8"))
        except OSError:
            pass
    packages |= _python_packages_from_project_files(source_dir)
    apt_deps: list[str] = []
    seen: set[str] = set()
    for pkg in sorted(packages):
        for apt in _PYTHON_APT_DEPS.get(pkg, ()):
            if apt not in seen:
                seen.add(apt)
                apt_deps.append(apt)
    return apt_deps


def _render_python(
    manifest: InstanceManifest,
    internal_port: int,
    source_dir: Path | None = None,
    *,
    mirrors: BuildMirrors | None = None,
    django_static: DjangoStaticRecipe | None = None,
) -> str:
    install = (manifest.entry.install or "").strip()
    start = (manifest.entry.start or _PYTHON_DEFAULT_START).strip()

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
        # --no-install-project 只装依赖、不构建项目本体；完整源码在 final_copy 拷入后，
        # 由下方 project_install 的第二次 uv sync 把项目本体（含控制台入口）装进
        # 同一 venv（BUG-900），运行期 uv run --no-sync 才有完整环境可用——否则
        # src/ 包布局项目启动即 ModuleNotFoundError。
        deps_block = (
            f"COPY {cpfx}uv.lock {cpfx}pyproject.toml ./\n"
            + _pip_run(
                "pip install uv && uv sync --frozen --no-dev --no-install-project",
                mirrors=mirrors,
            )
            + "\n"
        )
        run_prefix = "uv run --no-sync --no-dev "
        if not start.startswith("uv run"):
            start = run_prefix + start
    elif _is_plain_pip_install_dot(install):
        deps_block, needs_early_full_copy = _pip_dot_layers(
            cpfx, install, source_dir, mirrors
        )
    elif install.startswith("pip install ."):
        deps_block = f"COPY {cpfx} ./\n" + _pip_run(install, mirrors=mirrors) + "\n"
        needs_early_full_copy = True
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

    # BUG-997：Django 静态包装接管 Gunicorn 目标（保留迁移等前置链原样）。
    if django_static is not None and django_static.serve_static:
        start = _rewrite_static_wsgi_target(start, django_static.wsgi_module)

    header = _HEADER.format(
        kind="python",
        internal_port=internal_port,
        install=install or "（零依赖，跳过依赖安装）",
        start=start,
        database=_database_label(manifest),
    )

    # IMP-066（066.b）：CMD 使用 LWA 生成的 Gunicorn 生产命令时，平台分层补装
    # 运行时依赖（项目依赖声明 gunicorn 则尊重项目版本，不重复安装）；
    # uv 流程装进 uv venv（uv pip）。不回写用户 requirements/pyproject。
    gunicorn_block = _gunicorn_platform_block(start, source_dir, mirrors, uses_uv=uses_uv)
    # BUG-903：uv sync 会把锁文件未声明的包卸掉。平台 Gunicorn 若装在
    # 最终同步之前，会被这次 sync 清掉。uv 路径改到项目同步之后再补装；
    # pip 路径仍留在源码 COPY 之前，避免源码改动打掉该层。
    gunicorn_after_sync = ""
    if uses_uv:
        gunicorn_after_sync = gunicorn_block
        gunicorn_block = ""

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
    package_root = (
        _django_build_root(source_dir, cpfx)
        if django_static is not None
        else source_dir
    )
    frontend_pkg = _frontend_package_dir(source_dir)
    if package_root is not None and (
        (package_root / "package.json").is_file() or frontend_pkg
    ):
        node_apt = _apt_body("ca-certificates curl xz-utils", mirrors)
        node_toolchain = _render_apt_run(node_apt, mirrors) + _render_node_tarball_run(mirrors)
        if (package_root / "package.json").is_file():
            # BUG-997：Django 前端构建可能依赖 devDependencies（如 esbuild 之外的
            # 构建插件）。配方命中时必须完整安装，构建后再 prune 掉 devDeps；
            # 其他 Python 全栈项目保持原有的生产依赖安装，不扩大镜像。
            npm_install = _with_npm_registry(
                "npm ci || npm install"
                if django_static is not None
                else "npm ci --omit=dev || npm install --omit=dev",
                mirrors,
            )
            if needs_early_full_copy:
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

    # IMP-054 / issue #35：探测 + systemDeps，放在 COPY current/ 之前。
    apt_deps_block = _render_apt_deps_block(
        _collect_apt_packages(manifest, source_dir, install), mirrors
    )

    # 分层顺序：系统库 -> Node 工具链（最稳）-> Python 依赖 -> npm 依赖 -> 完整源码。
    final_copy = "" if needs_early_full_copy else f"COPY {cpfx} ./\n"
    project_install = ""
    if _is_plain_pip_install_dot(install) and not needs_early_full_copy:
        project_install = _pip_run("pip install . --no-deps", mirrors=mirrors) + "\n"
    elif uses_uv:
        # BUG-900：依赖层 --no-install-project 跳过了项目本体；完整源码就位后
        # 再 sync 一次把项目包（与控制台入口脚本）装进 venv。依赖已装的部分
        # uv 命中缓存不会重复下载，本层只增量构建/安装项目本体。
        project_install = _pip_run("uv sync --frozen --no-dev", mirrors=mirrors) + "\n"
    # issue#7：构建钩子在依赖安装层之后、CMD 之前逐条执行（WORKDIR=/app，
    # 源码已由 final_copy / 早期整包 COPY 就位）。含 apt-get 的钩子注入镜像与重试。
    hooks_block = _build_hooks_block(manifest, mirrors=mirrors)
    frontend_build = _frontend_build_block(
        manifest, source_dir, mirrors=mirrors, copy_prefix=cpfx
    )
    # BUG-997：静态配方在用户 buildHooks 之后执行——钩子可先改源码/产物，
    # 再由平台统一 npm run build + collectstatic，最后 prune 掉 devDeps。
    django_static_block = (
        _django_static_block(django_static, mirrors=mirrors, uses_uv=uses_uv)
        if django_static is not None
        else _django_static_skip_note(manifest, source_dir, copy_prefix=cpfx)
    )

    lines = [
        header,
        f"FROM {_PYTHON_IMAGE}",
        _build_env_arg_block(manifest),
        "WORKDIR /app",
        apt_deps_block,
        node_toolchain,
        deps_block,
        gunicorn_block,
        npm_block,
        final_copy,
        project_install,
        gunicorn_after_sync,
        frontend_build,
        sqlite_mkdir,
        hooks_block,
        django_static_block,
        pythonpath_env,
        "ENV HOST=0.0.0.0",
        f"ENV PORT={internal_port}",
        f"EXPOSE {internal_port}",
        f"CMD {_cmd_line(manifest, start)}",
    ]
    return "\n".join(line for line in lines if line) + "\n"


# PEP 621 引号包名，或 Poetry 等 ``gunicorn = ...`` 表项。描述/注释里的单词不算。
_PYPROJECT_GUNICORN_RE = re.compile(
    r"""(?im)(?:^\s*gunicorn\s*=|["']gunicorn(?=$|["'\s,<>=!~\[]))"""
)


def _project_declares_gunicorn(source_dir: Path | None) -> bool:
    """项目依赖是否已声明 gunicorn（requirements 行锚定 / pyproject 依赖声明）。"""
    if source_dir is None:
        return False
    req_re = re.compile(r"^\s*gunicorn\b", re.IGNORECASE | re.MULTILINE)
    for name in ("requirements.txt", "requirements-prod.txt"):
        text = _read_source_text(source_dir / name)
        if text and req_re.search(text):
            return True
    pyproject = _read_source_text(source_dir / "pyproject.toml")
    return bool(pyproject) and bool(_PYPROJECT_GUNICORN_RE.search(pyproject))


def _read_source_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _strip_uv_run(cmd: str) -> str:
    """剥掉 ``uv run`` 及平台注入的 ``--no-sync --no-dev --frozen`` 标志。"""
    stripped = cmd.strip()
    if not stripped.startswith("uv run"):
        return stripped
    rest = stripped[len("uv run") :].strip()
    for flag in ("--no-sync", "--no-dev", "--frozen"):
        if rest.startswith(flag):
            rest = rest[len(flag) :].strip()
    return rest


# BUG-909：平台迁移包装形如 ``<pre> && exec <start>``（Django migrate、
# Alembic、preStart）。判定平台依赖必须看最终 exec 启动的命令，否则
# 迁移包装后首段是 python/alembic，Django 迁移路径会漏装平台 Gunicorn。
_EXEC_CHAIN_RE = re.compile(r"&&\s*exec\s+")


def _gunicorn_launch_command(start: str, *, uses_uv: bool) -> str:
    """去掉平台加上的 ``uv run``、``sh -c`` 与迁移 ``&& exec`` 包裹，得到实际启动命令。

    工厂目标 ``app:create_app()`` 含括号，预检会写成 ``sh -c 'gunicorn ...'``。
    uv 流程还会在外层再加 ``uv run``；Django 迁移路径内层是
    ``python manage.py migrate --noinput && exec gunicorn ...``。是否补装
    Gunicorn 必须看最终 exec 启动的内层命令。
    """
    cmd = _strip_uv_run(start) if uses_uv else start.strip()
    try:
        parts = shlex.split(cmd)
    except ValueError:
        parts = None
    if parts is not None and len(parts) >= 3 and parts[0] == "sh" and parts[1] == "-c":
        cmd = parts[2].strip()
        if uses_uv:
            cmd = _strip_uv_run(cmd)
    return _EXEC_CHAIN_RE.split(cmd)[-1].strip()


def _gunicorn_platform_block(
    start: str, source_dir: Path | None, mirrors: BuildMirrors | None, *, uses_uv: bool
) -> str:
    """IMP-066（066.b）：Gunicorn 平台运行时依赖的分层安装块（不需要时为空）。"""
    cmd = _gunicorn_launch_command(start, uses_uv=uses_uv)
    if not cmd.startswith("gunicorn"):
        return ""
    if _project_declares_gunicorn(source_dir):
        return ""
    from local_webpage_access.scanner import GUNICORN_PLATFORM_SPEC

    if uses_uv:
        # uv venv 由 uv sync 创建；与项目依赖安装共用镜像回退链、超时和缓存。
        return (
            _pip_run(
                f'uv pip install "gunicorn{GUNICORN_PLATFORM_SPEC}"',
                mirrors=mirrors,
            )
            + "\n"
        )
    return (
        _pip_run(
            f'pip install --no-cache-dir "gunicorn{GUNICORN_PLATFORM_SPEC}"',
            mirrors=mirrors,
        )
        + "\n"
    )


def _frontend_package_dir(source_dir: Path | None) -> str | None:
    """Python 全栈常见前端子目录。"""
    if source_dir is None:
        return None
    for name in ("frontend", "web", "client"):
        if (source_dir / name / "package.json").is_file():
            return name
    return None


def _frontend_package_json(fe_dir: Path) -> dict:
    """安全解析前端 ``package.json``；缺失/损坏返回空 dict。"""
    import json

    try:
        data = json.loads((fe_dir / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _frontend_build_block(
    manifest: InstanceManifest,
    source_dir: Path | None,
    *,
    mirrors: BuildMirrors | None,
    copy_prefix: str = "current/",
) -> str:
    """BUG-683/684：契约完整（npm 锁文件 + build 脚本）才自动前端构建。

    无锁文件 / pnpm·yarn / 无 build 脚本 → 跳过并留注释说明，前端构建交给
    manifest ``buildHooks`` 显式执行（钩子在本块之后、CMD 之前，可完整替代）；
    自定义输出目录（默认假定 ``dist`` → ``backend/static``）同样走 buildHooks。

    BUG-700（issue #42）：构建目录必须与 COPY 前缀对齐。前端目录探测扫的是
    仓库根，而镜像内容物由 ``copy_prefix`` 决定，二者有三种位置关系——只有
    根布局下 final_copy 会顺带把前端子树带进镜像：

    - 前缀即前端目录（如 ``sourceSubdir=frontend``）→ final_copy 已拷到
      ``/app``，构建目录用 ``/app``；
    - 前端在前缀之内（根布局）→ 沿用 ``/app/<fe>``；
    - 前端在前缀之外（如 ``sourceSubdir=backend``）→ 显式 ``COPY`` 前端子树，
      否则 ``/app/<fe>`` 是空目录，``npm ci`` 必败（EUSAGE 无锁文件）。
    """
    del manifest  # 签名与其它渲染块对齐，构建参数已由 ARG/ENV 注入。
    fe = _frontend_package_dir(source_dir)
    if not fe or source_dir is None:
        return ""
    fe_dir = source_dir / fe
    raw_scripts = _frontend_package_json(fe_dir).get("scripts")
    scripts = raw_scripts if isinstance(raw_scripts, dict) else {}
    has_build_script = bool(scripts.get("build"))
    has_npm_lock = (fe_dir / "package-lock.json").is_file() or (
        fe_dir / "npm-shrinkwrap.json"
    ).is_file()
    other_lock = (fe_dir / "pnpm-lock.yaml").is_file() or (fe_dir / "yarn.lock").is_file()
    if not has_build_script or not has_npm_lock or other_lock:
        reasons: list[str] = []
        if not has_build_script:
            reasons.append("package.json 无 build 脚本")
        if other_lock:
            reasons.append("检测到 pnpm/yarn 锁文件（暂只支持 npm）")
        elif not has_npm_lock:
            reasons.append("缺少 package-lock.json（npm ci 需要）")
        return (
            "# 前端自动构建跳过：" + "；".join(reasons)
            + "。需要构建请用 manifest buildHooks 显式执行（见 docs/faq.md）。\n"
        )
    prefix_dir = (copy_prefix or "current/").rstrip("/")
    if prefix_dir == f"current/{fe}":
        # 前缀即前端目录：final_copy / 早期整包 COPY 已把子树拷到 /app
        workdir = "/app"
        copy_fe = ""
    else:
        workdir = f"/app/{fe}"
        if prefix_dir != "current":
            # 前端在拷贝前缀之外：显式带进镜像（构建上下文仍含整个 current/）
            copy_fe = f"COPY current/{fe}/ /app/{fe}/\n"
        else:
            copy_fe = ""
    npm_build = _with_npm_registry("npm ci && npm run build", mirrors)
    # #47 / S1-02：产物目录按后端实际位置推导；echo 出目标便于从 build.log 确认。
    dest = frontend_artifact_dest(prefix_dir, source_dir)
    copy_dist = (
        f"RUN if [ -d {workdir}/dist ]; then "
        f"mkdir -p {dest} && cp -a {workdir}/dist/. {dest}/ "
        f"&& echo \"frontend artifacts -> {dest}\"; "
        "fi"
    )
    return f"{copy_fe}WORKDIR {workdir}\nRUN {npm_build}\n{copy_dist}\nWORKDIR /app\n"


# ---- BUG-997：Django + 根 package.json 静态资源配方 --------------------------

@dataclass(frozen=True)
class DjangoStaticRecipe:
    """Django 项目前端构建 / collectstatic / 生产静态托管的已证明契约。

    只有下列证据同时成立才会生成：``manage.py``、可从 ``manage.py`` 解析出的
    settings 模块、该 settings 树里声明的 ``STATIC_ROOT``、根 ``package.json``
    的 ``build`` 脚本、npm 锁文件（package-lock / npm-shrinkwrap）。缺任一项
    都不猜，留注释并交 ``buildHooks`` 显式处理。
    """

    settings_module: str
    wsgi_module: str
    serve_static: bool


_DJANGO_SETTINGS_SETDEFAULT_RE = re.compile(
    r"""setdefault\(\s*["']DJANGO_SETTINGS_MODULE["']\s*,\s*["']([A-Za-z_][\w.]*)["']\s*\)"""
)
_DJANGO_SETTINGS_ASSIGN_RE = re.compile(
    r"""DJANGO_SETTINGS_MODULE\s*=\s*["']([A-Za-z_][\w.]*)["']"""
)
_DJANGO_STATIC_ROOT_RE = re.compile(r"(?m)^\s*STATIC_ROOT\s*=")
_SETTINGS_MAX_FILES = 40


def _detect_django_static_recipe(
    manifest: InstanceManifest,
    source_dir: Path | None,
    *,
    copy_prefix: str,
) -> DjangoStaticRecipe | None:
    """返回完整契约对应的配方；项目不适用或契约不全时返回 ``None``。"""
    recipe, _reason = _probe_django_static(manifest, source_dir, copy_prefix=copy_prefix)
    return recipe


def _django_static_skip_note(
    manifest: InstanceManifest,
    source_dir: Path | None,
    *,
    copy_prefix: str,
) -> str:
    """项目像「Django + 根前端构建」但契约不全时，在 Dockerfile 留可操作注释。"""
    recipe, reason = _probe_django_static(manifest, source_dir, copy_prefix=copy_prefix)
    if recipe is not None or not reason:
        return ""
    return (
        "# BUG-997：Django 静态资源自动配方跳过：" + reason
        + "。请用 manifest buildHooks 显式构建/收集，或补齐静态资源契约"
        "（见 docs/known-limitations.md）。\n"
    )


def _django_build_root(source_dir: Path | None, copy_prefix: str) -> Path | None:
    """镜像内业务源码根对应的宿主目录（与 COPY 前缀对齐）。"""
    if source_dir is None:
        return None
    prefix = (copy_prefix or "current/").strip("/")
    if prefix == "current":
        root = source_dir
    elif prefix.startswith("current/"):
        root = source_dir / prefix[len("current/") :]
    else:
        return None
    return root if root.is_dir() else None


def _probe_django_static(
    manifest: InstanceManifest,
    source_dir: Path | None,
    *,
    copy_prefix: str,
) -> tuple[DjangoStaticRecipe | None, str | None]:
    """契约探测并区分「不适用」``(None, None)`` 与「契约不全」``(None, reason)``。"""
    root = _django_build_root(source_dir, copy_prefix)
    if root is None or not (root / "manage.py").is_file():
        return None, None
    pkg_path = root / "package.json"
    if not pkg_path.is_file():
        return None, None
    try:
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "根 package.json 无法解析"
    scripts = pkg.get("scripts") if isinstance(pkg, dict) else None
    build_script = scripts.get("build") if isinstance(scripts, dict) else None
    if not isinstance(build_script, str) or not build_script.strip():
        # 没有前端 build 脚本：静态资源不需要预构建，保持原有行为不加注释。
        return None, None
    if (root / "pnpm-lock.yaml").is_file() or (root / "yarn.lock").is_file():
        return None, "检测到 pnpm/yarn 锁文件（自动配方暂只支持 npm）"
    if not (
        (root / "package-lock.json").is_file()
        or (root / "npm-shrinkwrap.json").is_file()
    ):
        return None, "根 package.json 有 build 脚本但缺少 package-lock.json/npm-shrinkwrap.json"

    manage_py = _read_source_text(root / "manage.py")
    settings_match = _DJANGO_SETTINGS_SETDEFAULT_RE.search(manage_py) or (
        _DJANGO_SETTINGS_ASSIGN_RE.search(manage_py)
    )
    if settings_match is None:
        return None, "无法从 manage.py 识别 DJANGO_SETTINGS_MODULE"
    settings_module = settings_match.group(1)
    settings_files = _settings_module_files(root, settings_module)
    if not settings_files or not any(
        _DJANGO_STATIC_ROOT_RE.search(_read_source_text(path)) for path in settings_files
    ):
        return None, f"settings 模块 {settings_module} 未声明 STATIC_ROOT"

    from local_webpage_access.scanner import infer_django_wsgi_target

    target, wsgi_reason = infer_django_wsgi_target(root)
    if not target:
        # BUG-1002：WSGI 入口不唯一/缺失时整体跳过——交付约定是「唯一 wsgi 才
        # 启用」。此前只把 serve_static 置 False 却仍执行 npm run build 与
        # collectstatic，既跑了本应保守跳过的构建，又留下无静态托管的半成品。
        return None, f"{wsgi_reason or 'WSGI 入口无法唯一确定'}（整体跳过，不执行 npm build / collectstatic）"
    wsgi_module = target.split(":", 1)[0]
    serve_static = f"{wsgi_module}:application" in (manifest.entry.start or "")
    return (
        DjangoStaticRecipe(
            settings_module=settings_module,
            wsgi_module=wsgi_module,
            serve_static=serve_static,
        ),
        None,
    )


_SETTINGS_STAR_IMPORT_RE = re.compile(
    r"(?m)^\s*from\s+(\.[\w.]*|[A-Za-z_][\w.]*)\s+import\s+\*"
)


def _settings_module_files(root: Path, module: str) -> list[Path]:
    """返回所选 settings 模块**实际可达**的文件。

    不能把 ``settings/`` 目录下所有 ``*.py`` 都算进去：``prod.py`` 可能声明
    ``STATIC_ROOT`` 但当前选择的是 ``dev`` 链，``collectstatic`` 仍会失败。
    这里沿 ``from X import *`` 递归（Django 常见的 base/dev/prod 写法），
    只接受该链上的文件。
    """
    files: list[Path] = []
    seen: set[str] = set()
    queue = [module]
    while queue and len(files) < _SETTINGS_MAX_FILES:
        name = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        base = root.joinpath(*name.split("."))
        module_file = base.with_suffix(".py")
        init_file = base / "__init__.py"
        path = module_file if module_file.is_file() else None
        is_package = path is None and init_file.is_file()
        if is_package:
            path = init_file
        if path is None:
            continue
        files.append(path)
        text = _read_source_text(path)
        anchor = name if is_package else name.rpartition(".")[0]
        for target in _settings_star_import_targets(text, anchor):
            if target not in seen:
                queue.append(target)
    return files


def _settings_star_import_targets(text: str, anchor: str) -> list[str]:
    """把 settings 文件里的星号导入解析成绝对模块名（尽力而为）。"""
    targets: list[str] = []
    for match in _SETTINGS_STAR_IMPORT_RE.finditer(text):
        raw = match.group(1)
        if not raw.startswith("."):
            targets.append(raw)
            continue
        dots = len(raw) - len(raw.lstrip("."))
        rest = raw.lstrip(".")
        parts = anchor.split(".") if anchor else []
        # 一个点表示当前包；``..`` 向上一级。
        ascend = dots - 1
        if ascend:
            parts = parts[:-ascend] if ascend <= len(parts) else []
        if rest:
            parts = [*parts, *rest.split(".")]
        if parts:
            targets.append(".".join(parts))
    return targets


def _django_static_block(
    recipe: DjangoStaticRecipe, *, mirrors: BuildMirrors | None, uses_uv: bool = False
) -> str:
    """Dockerfile 层：前端构建 → collectstatic → 静态 WSGI 包装 → 裁剪 devDeps。

    BUG-997（复核重开）：uv 项目的依赖经 ``uv sync`` 装在项目 venv 里，镜像的
    系统 ``python`` 没有 Django；collectstatic 必须用与 CMD 同一口径的
    ``uv run --no-sync --no-dev`` 进入该 venv（本层位于项目本体 sync 之后）。
    """
    python_prefix = "uv run --no-sync --no-dev " if uses_uv else ""
    lines = [
        "# BUG-997：Django 静态资源配方（manage.py + STATIC_ROOT + npm 锁 + build 脚本均已证明）。",
        f"RUN {_with_npm_registry('npm run build', mirrors)}",
        "RUN DJANGO_SETTINGS_MODULE="
        f"{recipe.settings_module} {python_prefix}python manage.py collectstatic --noinput",
    ]
    if recipe.serve_static:
        lines.append("COPY docker/lwa_static_wsgi.py /app/lwa_static_wsgi.py")
    lines.append("RUN npm prune --omit=dev --no-audit --no-fund")
    return "\n".join(lines) + "\n"


def _rewrite_static_wsgi_target(start: str, wsgi_module: str) -> str:
    """把 Gunicorn 目标改为平台生成的静态包装模块；目标不在命令中则原样返回。"""
    if not wsgi_module:
        return start
    # 只替换目标本身，不吃掉命令里已有的引号。曾用 ``["']?`` 包裹，结果把
    # ``sh -c "... module:application"`` 的收尾双引号一并吞掉，生成的 CMD
    # 少一个引号（字符串在 Dockerfile 中直接截断）。
    pattern = re.compile(
        rf"(?<![\w.]){re.escape(wsgi_module)}:application(?![\w.])"
    )
    if not pattern.search(start):
        return start
    return pattern.sub("lwa_static_wsgi:application", start)


_DJANGO_STATIC_WSGI_TEMPLATE = '''\
"""由 lwa 自动生成（BUG-997），请勿手动编辑。

Django 的生产 WSGI 服务器（Gunicorn）不提供静态资源；本模块在原应用外
增加 ``STATIC_ROOT`` 只读服务，URL 前缀与 Django ``STATIC_URL`` 一致。
路径经 ``realpath`` 归一化并做目录包含校验，拒绝穿越与符号链接逃逸。
"""

from __future__ import annotations

import importlib
import mimetypes
import os
import posixpath
import stat
from email.utils import formatdate
from urllib.parse import unquote, urlsplit
from wsgiref.util import FileWrapper

_APP_MODULE = __APP_MODULE__

# 先加载项目自己的 WSGI 模块，确保 Django settings 与自定义初始化都已执行。
_application = importlib.import_module(_APP_MODULE).application

from django.conf import settings  # noqa: E402

# BUG-1001：空/相对 STATIC_ROOT 不得先经 realpath——``os.path.realpath("")`` 会
# 返回工作目录（/app），后面的「未配置则不托管」判断随之失效，/static/ 就能读到
# 应用目录里的任意文件。先验证原始值非空且为绝对路径，再归一化；不合法时静态
# 托管整体停用，请求原样交回 Django 应用。
_STATIC_ROOT_RAW = str(getattr(settings, "STATIC_ROOT", "") or "").strip()
_STATIC_ROOT = (
    os.path.realpath(_STATIC_ROOT_RAW)
    if _STATIC_ROOT_RAW and os.path.isabs(_STATIC_ROOT_RAW)
    else ""
)
_STATIC_URL = str(getattr(settings, "STATIC_URL", "") or "").strip()
try:
    _STATIC_URL_PARTS = urlsplit(_STATIC_URL)
except ValueError:
    _STATIC_URL = ""
else:
    # 空值、外部 URL 与根路径不得接管动态路由；点段/重复斜杠归一化后
    # 落到根路径的前缀也保守禁用，但不改写其他本地前缀的匹配口径。
    if (
        not _STATIC_URL
        or _STATIC_URL_PARTS.scheme
        or _STATIC_URL_PARTS.netloc
        or _STATIC_URL.startswith("//")
        or posixpath.normpath("/" + _STATIC_URL.lstrip("/")) == "/"
    ):
        _STATIC_URL = ""
if _STATIC_URL:
    if not _STATIC_URL.startswith("/"):
        _STATIC_URL = "/" + _STATIC_URL
    if not _STATIC_URL.endswith("/"):
        _STATIC_URL += "/"


def _static_file(path_info: str) -> tuple[str, os.stat_result] | None:
    if not _STATIC_ROOT or not _STATIC_URL:
        return None
    if not path_info.startswith(_STATIC_URL):
        return None
    rel = unquote(path_info[len(_STATIC_URL) :])
    if not rel or rel.endswith("/") or "\\x00" in rel:
        return None
    rel = rel.replace("\\\\", "/")
    if rel.startswith("/"):
        return None
    normalized = posixpath.normpath(rel)
    if normalized == ".." or normalized.startswith("../"):
        return None
    full = os.path.realpath(os.path.join(_STATIC_ROOT, normalized))
    if full != _STATIC_ROOT and not full.startswith(_STATIC_ROOT + os.sep):
        return None
    try:
        info = os.stat(full)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return full, info


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET").upper()
    if method not in ("GET", "HEAD"):
        return _application(environ, start_response)
    found = _static_file(environ.get("PATH_INFO", ""))
    if found is None:
        return _application(environ, start_response)
    full, info = found
    content_type = mimetypes.guess_type(full)[0] or "application/octet-stream"
    headers = [
        ("Content-Type", content_type),
        ("Content-Length", str(info.st_size)),
        ("Last-Modified", formatdate(info.st_mtime, usegmt=True)),
    ]
    start_response("200 OK", headers)
    if method == "HEAD":
        return [b""]
    handle = open(full, "rb")
    file_wrapper = environ.get("wsgi.file_wrapper")
    if file_wrapper is not None:
        return file_wrapper(handle, 8192)
    return FileWrapper(handle, 8192)
'''


def _render_django_static_wsgi(wsgi_module: str) -> str:
    return _DJANGO_STATIC_WSGI_TEMPLATE.replace(
        "__APP_MODULE__", json.dumps(wsgi_module)
    )


def _sync_django_static_helper(
    workspace: Workspace, instance_id: str, recipe: DjangoStaticRecipe | None
) -> None:
    """写入/清理 ``docker/lwa_static_wsgi.py``（生成物，不属于用户文件）。"""
    path = workspace.app_docker(instance_id) / "lwa_static_wsgi.py"
    if recipe is not None and recipe.serve_static and recipe.wsgi_module:
        path.write_text(
            _render_django_static_wsgi(recipe.wsgi_module), encoding="utf-8"
        )
    else:
        path.unlink(missing_ok=True)


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
        _build_env_arg_block(manifest),
        "WORKDIR /app",
        f"COPY {_copy_prefix(manifest)} ./",
        # issue#7：与 node/python 渲染器对齐，支持构建钩子与启动前命令。
        _build_hooks_block(manifest, mirrors=None),
        f"EXPOSE {internal_port}",
        f"CMD {_cmd_line(manifest, start)}",
    ]
    return "\n".join(line for line in lines if line) + "\n"


def _build_env_arg_block(manifest: InstanceManifest) -> str:
    """issue #39：把 effective_build_env 声明为 Docker ARG + ENV（值由 compose build.args 注入）。"""
    from local_webpage_access.instance_settings import effective_build_env

    env = effective_build_env(manifest)
    if not env:
        return ""
    lines: list[str] = []
    for key in env:
        lines.append(f"ARG {key}=")
        lines.append(f"ENV {key}=${{{key}}}")
    return "\n".join(lines) + "\n"


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


def _build_hooks_block(
    manifest: InstanceManifest, *, mirrors: BuildMirrors | None = None
) -> str:
    """issue#7 / CHK-326：把 ``manifest.buildHooks`` 逐条原样渲染为 ``RUN <hook>``。

    不因字符串含 ``apt-get`` 自动改写。系统包请用 ``systemDeps``。
    """
    del mirrors  # 保留签名兼容 generate_dockerfile 调用
    chunks: list[str] = []
    for hook in manifest.buildHooks:
        text = hook.strip()
        if not text:
            continue
        chunks.append(f"RUN {text}\n")
    return "".join(chunks)


def artifact_dest_for_manifest(manifest: InstanceManifest, workspace: Workspace) -> str:
    """当前实例前端产物在镜像内的落点（与模板 COPY 前缀一致）。"""
    source = workspace.app_current(manifest.id)
    source_dir = source if source.is_dir() else None
    return frontend_artifact_dest(_copy_prefix(manifest), source_dir)


def destructive_artifact_copy_hooks(
    manifest: InstanceManifest, workspace: Workspace
) -> list[str]:
    """检查直接 rm 命令的操作数；仅分词，不执行或展开用户 shell。"""
    hooks = getattr(manifest, "buildHooks", None) or []
    if not hooks:
        return []
    dests = {"/app/static", "/app/backend/static"}
    with contextlib.suppress(Exception):
        dest = artifact_dest_for_manifest(manifest, workspace)
        dests.update({dest, dest.rstrip("/")})
    hits: list[str] = []
    for hook in hooks:
        text = str(hook).strip()
        if not text:
            continue
        if _rm_operands_hit_dests(text, dests):
            hits.append(hook)
    return hits


# 保留原始引号以区分字面量 ">" 与重定向；单词内容仍交给 shlex 解码。
_SHELL_TOKEN_RE = re.compile(
    r"(?P<comment>\#[^\n]*)|"
    r"""(?P<word>(?:[^\s;&|<>()'"\\]|\\[\s\S]|"(?:\\[\s\S]|[^"\\])*"|'[^']*')+)"""
    r"|(?P<operator>&>>?|<<<|<<-?|[<>]&|<>|>\||>>|&&|\|\||\|&|[;&|<>()\n])"
)


def _rm_operands_hit_dests(text: str, dests: set[str]) -> bool:
    """仅当某条 ``rm -rf/-f`` 的操作数命中产物目录时为破坏性钩子。"""
    dest_norm = {posixpath.normpath(item) for item in dests if item}
    if not dest_norm:
        return False
    tokens = list(_SHELL_TOKEN_RE.finditer(text))
    end = 0
    for match in tokens:
        if text[end:match.start()].strip():
            return False  # 未闭合引号等语法不能当作删除操作数。
        end = match.end()
    if text[end:].strip():
        return False
    commands: list[list[str]] = []
    command: list[str] = []
    redirect_target = False
    for index, match in enumerate(tokens):
        token = match.group()
        if match.lastgroup == "comment":
            break
        operator = match.lastgroup == "operator"
        if redirect_target:
            redirect_target = False
            continue
        if operator and ("<" in token or ">" in token):
            redirect_target = True
        elif operator:
            commands.append(command)
            command = []
        else:
            # 2>log 中的 2 是 fd；与空格分隔的数字文件名不同。
            if token.isdecimal() and index + 1 < len(tokens):
                following = tokens[index + 1]
                if following.start() == match.end() and following.lastgroup == "operator":
                    if "<" in following.group() or ">" in following.group():
                        continue
            command.extend(shlex.split(token))
    commands.append(command)
    for words in commands:
        if not words or words[0] not in {"rm", "/bin/rm", "/usr/bin/rm"}:
            continue
        destructive = False
        options = True
        operands: list[str] = []
        for token in words[1:]:
            if options and token == "--":
                options = False
                continue
            if options and token.startswith("-"):
                destructive |= token in {"--force", "--recursive"} or (
                    not token.startswith("--") and any(char in "rRfF" for char in token[1:])
                )
                continue
            operands.append(posixpath.normpath(token))
        if not destructive:
            continue
        for operand in operands:
            if operand in dest_norm or any(
                operand.startswith(dest + "/") for dest in dest_norm
            ):
                return True
    return False


def destructive_artifact_hooks_message(instance_id: str, hooks: list[str]) -> str:
    preview = "; ".join(item.strip() for item in hooks[:2])
    extra = " …" if len(hooks) > 2 else ""
    return (
        f"实例 {instance_id} 的 buildHooks 含会删除前端产物目录的命令"
        f"（V0.9.2 起模板自行把产物落到 static/ 且不 rm -rf，该钩子会删掉同目录下"
        f"应用自有文件）：{preview}{extra}。"
        f"建议 `lwa configure {instance_id} --clear-build-hooks` 后重建；"
        f"确认仍要执行该钩子时用 `lwa rebuild {instance_id} --force`。"
    )


def refuse_destructive_artifact_hooks(
    manifest: InstanceManifest,
    workspace: Workspace,
    *,
    force: bool = False,
    registry: object | None = None,
) -> None:
    """重建/生成镜像前审查破坏性产物钩子。不擅自删除用户钩子。"""
    hits = destructive_artifact_copy_hooks(manifest, workspace)
    if not hits:
        return
    message = destructive_artifact_hooks_message(manifest.id, hits)
    if registry is not None:
        add_event = getattr(registry, "add_event", None)
        if callable(add_event):
            with contextlib.suppress(Exception):
                add_event(manifest.id, "build_hooks", message)
    if force:
        log.warning("%s（--force 继续）", message)
        return
    from local_webpage_access.errors import DestructiveBuildHooksError

    raise DestructiveBuildHooksError(message, instance_id=manifest.id)


def _cmd_line(manifest: InstanceManifest, start: str) -> str:
    """issue#7：渲染 CMD。声明了 ``preStart`` 时走 shell 形式
    ``sh -c "<preStart> && exec <start>"``（启动前命令失败则容器退出，``exec``
    保证主进程仍是 PID 1、信号传递正常）；未声明时保持 ``_to_exec_form``
    原逻辑（含 BUG-471 的 shell 操作符处理）不变。
    """
    pre_start = (manifest.preStart or "").strip()
    if pre_start:
        # BUG-904：preStart 走自己的 sh -c，不会进入 _to_exec_form。
        # 存量裸工厂目标和已包一层 sh -c 的内层脚本都要先补引号。
        body = _quote_start_for_shell(start)
        return json.dumps(["sh", "-c", f"{pre_start} && exec {body}"])
    return _to_exec_form(start)


# 已落盘的旧 entry.start 可能仍是裸 ``flaskr:create_app()``。渲染 CMD 时补上
# 双引号，避免容器 sh 把括号当成语法（BUG-904）。先吃掉已有引号片段，
# 避免把 ``"flaskr:create_app()"`` 再拆开包一层。
# BUG-911：只匹配带冒号的 Gunicorn 工厂目标 ``module:factory()``。裸
# ``name()`` 是合法 shell 函数定义（如 ``start() { exec node server.js; }``），
# 给它加引号会把可用的自定义入口改成 sh 语法错误。
_BARE_CALL_RE = re.compile(
    r"""(?P<quoted>"(?:\\.|[^"\\])*"|'[^']*'|\\.)|(?P<call>(?<![\w\\])[A-Za-z_][\w.]*:[A-Za-z_][\w.]*\(\))"""
)


def _quote_factory_tokens(script: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        call = match.group("call")
        if call is None:
            return match.group(0)
        return f'"{call}"'

    return _BARE_CALL_RE.sub(_replace, script)


_SH_C_WRAPPER_RE = re.compile(
    r"""^(?P<head>sh\s+-c\s+)(?P<script>'[^']*'|"(?:\\.|[^"\\])*")(?P<tail>(?:\s.*)?)$""",
    re.DOTALL,
)


def _quote_bare_calls(script: str) -> str:
    """修复实际启动命令，不把普通参数中出现的 sh -c 文本当成包装层。"""
    return _quote_start_for_shell(script)


def _quote_start_for_shell(start: str) -> str:
    """给将要嵌进 ``sh -c`` 的启动命令补上工厂引号，含已有的 ``sh -c`` 内层。"""
    stripped = start.strip()
    command = _strip_uv_run(stripped)
    prefix = stripped[:len(stripped) - len(command)]
    match = _SH_C_WRAPPER_RE.fullmatch(command)
    if match is not None:
        # 只解开命令位置上的包装层。用 shell 转义重建变动的脚本参数，
        # 避免双引号外层与新补的工厂双引号嵌套冲突。
        inner = shlex.split(match["script"])[0]
        fixed = _quote_start_for_shell(inner)
        if fixed != inner:
            return prefix + match["head"] + shlex.quote(fixed) + match["tail"]
    return _quote_factory_tokens(stripped)


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
            if len(parts) >= 3 and parts[0] == "sh" and parts[1] == "-c":
                parts = [parts[0], parts[1], _quote_bare_calls(parts[2]), *parts[3:]]
                return "[" + ", ".join(json.dumps(p) for p in parts) + "]"
        except ValueError:
            pass
        return json.dumps(["sh", "-c", _quote_bare_calls(shell_cmd)])
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


def _node_workspace_patterns(root: Path) -> list[str]:
    """工程根声明的 workspaces glob 列表（package.json ``workspaces``）。"""
    try:
        pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    workspaces = pkg.get("workspaces") if isinstance(pkg, dict) else None
    if not workspaces:
        return []
    return list(
        workspaces if isinstance(workspaces, list) else workspaces.get("packages", [])
    )


def _pnpm_workspace_patterns(root: Path) -> list[str]:
    """从 pnpm-workspace.yaml 提取 ``packages`` glob（行解析，避免 YAML 依赖）。

    pnpm 只认 pnpm-workspace.yaml 的 workspace 声明（不读 package.json
    ``workspaces``），monorepo 子包定位必须以此为准。
    """
    path = root / "pnpm-workspace.yaml"
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    patterns: list[str] = []
    in_packages = False
    for line in lines:
        stripped = line.strip()
        if not in_packages:
            if re.match(r"^packages\s*:", stripped):
                in_packages = True
            continue
        m = re.match(r"^-\s*['\"]?([^'\"]+?)['\"]?\s*$", stripped)
        if m:
            patterns.append(m.group(1))
        elif stripped and not stripped.startswith("#"):
            break
    return patterns


def _node_workspace_package_copies(
    root: Path, copy_prefix: str, patterns: list[str]
) -> list[str]:
    """workspaces monorepo 子包 package.json 的 COPY 行（npm/pnpm/yarn/bun 共用）。

    ``npm ci --workspace=...`` / ``pnpm install --frozen-lockfile`` /
    ``yarn install --frozen-lockfile`` / ``bun install --frozen-lockfile``
    解析 workspaces 时所有子包的 package.json 必须在场，否则 lock 解析失败
    或子包依赖缺失。glob 逐条展开写出，保证仅复制声明文件而非源码。
    """
    lines: list[str] = []
    for pattern in patterns:
        for pkg_dir in sorted(root.glob(pattern)):
            if (pkg_dir / "package.json").is_file():
                rel = pkg_dir.relative_to(root)
                lines.append(
                    f"COPY {copy_prefix}{posixpath.join(str(rel), 'package.json')} {posixpath.join(str(rel), 'package.json')}"
                )
    return lines


_NODE_GIT_DEP_RE = re.compile(
    r"git\+|github:|gitlab:|bitbucket:|git@|github\.com/\S+\.git",
    re.IGNORECASE,
)

# BUG-986：已知含 node-gyp 原生构建的依赖（构建失败即 install 失败）。
_NODE_NATIVE_PACKAGES = (
    "sqlite3", "better-sqlite3", "bcrypt", "node-sass", "canvas",
    "grpc", "deasync", "leveldown", "farmhash", "cpu-features",
)
_NODE_NATIVE_DEP_RE = re.compile(
    r"(?<![\w-])(?:" + "|".join(_NODE_NATIVE_PACKAGES) + r")@"
    r"|\"node_modules/(?:" + "|".join(_NODE_NATIVE_PACKAGES) + r")\""
    r"|\"(?:" + "|".join(_NODE_NATIVE_PACKAGES) + r")\":"
    # 评审 P3：pnpm v5 锁文件的包键是斜杠分隔（``/sqlite3/5.1.7:``），
    # 不匹配 @ 形式；行首缩进后 ``/name/数字`` 才是锁键，避免误伤路径文本。
    r"|(?<![\w])/(?:" + "|".join(_NODE_NATIVE_PACKAGES) + r")/\d"
)


def _node_source_needs_native_toolchain(source_dir: Path | None) -> bool:
    """依赖声明/锁文件含已知原生模块时，install 阶段需要 node-gyp 工具链（BUG-986）。

    HedgeDoc 实案：node:22-alpine 内 yarn install 走到 sqlite3@5.1.7 的
    node-gyp 构建，镜像 apk 层只有 git（无 python3/make/g++）而失败。
    原生依赖可能是传递依赖，项目 scripts 不含任何原生构建命令也会触发。
    """
    if source_dir is None:
        return False
    for name in (
        "package.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "package-lock.json",
        "bun.lock",
    ):
        path = source_dir / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _NODE_NATIVE_DEP_RE.search(text):
            return True
    return False


def _node_source_needs_git(source_dir: Path | None) -> bool:
    """锁文件或 package.json 含 git 依赖时，安装阶段需要 git（BUG-948）。"""
    if source_dir is None:
        return False
    for name in (
        "package.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "package-lock.json",
        "bun.lock",
    ):
        path = source_dir / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _NODE_GIT_DEP_RE.search(text):
            return True
    return False


def _node_dependency_copy_block(
    install: str, *, source_dir: Path | None = None, copy_prefix: str = "current/"
) -> str:
    """复制与包管理器匹配的依赖声明文件。

    对于 workspaces monorepo，各包管理器分支都会追加子包 package.json 的
    COPY 行（见 :func:`_node_workspace_package_copies`）；pnpm 的 workspace
    声明取自 pnpm-workspace.yaml，其余取 package.json ``workspaces``。

    BUG-874：yarn/pnpm 分支额外携带工程根实际存在的包管理器配置文件——
    Yarn Berry 的 .yarnrc.yml 与 .yarn/（releases/patches/plugins）、Yarn 1
    的 .yarnrc、pnpm 的 pnpm-workspace.yaml；各分支均携带 .npmrc（registry/
    peer 策略属构建配置）。文件不存在时不追加，渲染保持逐字不变。

    BUG-888：pnpm/yarn/bun 分支不再提前返回而跳过 workspaces 子包拷贝与
    BUG-850 生命周期脚本前置拷贝（此前这两件事仅 npm 分支生效）；npm
    workspaces 的 glob/relative_to 统一按 COPY 前缀对应的工程根
    （:func:`_node_lifecycle_root`）解析，sourceSubdir 工程不再路径错位。
    """
    if "bun install" in install:
        # bun.lock（现行文本锁）/ bun.lockb（旧二进制锁）：scanner 仅在锁文件
        # 存在时才推断 bun install，故 bun.lock* glob 必有所指。
        lines = [f"COPY {copy_prefix}package.json {copy_prefix}bun.lock* ./"]
        if source_dir is not None:
            root = _node_lifecycle_root(source_dir, copy_prefix)
            lines.extend(
                _node_workspace_package_copies(root, copy_prefix, _node_workspace_patterns(root))
            )
            # bunfig.toml 是 bun 的构建配置（registry/install 行为）
            if (root / "bunfig.toml").is_file():
                lines.append(f"COPY {copy_prefix}bunfig.toml ./")
            if (root / ".npmrc").is_file():
                lines.append(f"COPY {copy_prefix}.npmrc ./")
        for copy_line in _node_install_script_prereq_copies(
            source_dir, copy_prefix, install=install
        ):
            if copy_line not in lines:
                lines.append(copy_line)
        return "\n".join(lines)
    if _is_pm_install(install, "pnpm"):
        lines = [f"COPY {copy_prefix}package.json {copy_prefix}pnpm-lock.yaml ./"]
        if source_dir is not None:
            root = _node_lifecycle_root(source_dir, copy_prefix)
            # pnpm workspaces 需要 workspace 声明文件在场
            if (root / "pnpm-workspace.yaml").is_file():
                lines.append(f"COPY {copy_prefix}pnpm-workspace.yaml ./")
            # pnpm 只认 pnpm-workspace.yaml 的 packages；缺失时兜底 package.json
            patterns = _pnpm_workspace_patterns(root) or _node_workspace_patterns(root)
            lines.extend(_node_workspace_package_copies(root, copy_prefix, patterns))
            if (root / ".npmrc").is_file():
                lines.append(f"COPY {copy_prefix}.npmrc ./")
        for copy_line in _node_install_script_prereq_copies(
            source_dir, copy_prefix, install=install
        ):
            if copy_line not in lines:
                lines.append(copy_line)
        return "\n".join(lines)
    if _is_pm_install(install, "yarn"):
        lines = [f"COPY {copy_prefix}package.json {copy_prefix}yarn.lock ./"]
        if source_dir is not None:
            root = _node_lifecycle_root(source_dir, copy_prefix)
            # BUG-874：Yarn Berry（2+）的 .yarnrc.yml 声明 yarnPath/patches/nodeLinker，
            # .yarn/ 含 releases/plugins/patches（patch: 协议依赖必需）——缺了 install 必败
            # （如 hedgedoc 的 passport patch 报 YN0001 ENOENT）。
            if (root / ".yarnrc.yml").is_file():
                lines.append(f"COPY {copy_prefix}.yarnrc.yml ./")
            if (root / ".yarn").is_dir():
                lines.append(f"COPY {copy_prefix}.yarn/ .yarn/")
            # Yarn 1 的 .yarnrc（registry/mirror 等配置）
            if (root / ".yarnrc").is_file():
                lines.append(f"COPY {copy_prefix}.yarnrc ./")
            lines.extend(
                _node_workspace_package_copies(root, copy_prefix, _node_workspace_patterns(root))
            )
            if (root / ".npmrc").is_file():
                lines.append(f"COPY {copy_prefix}.npmrc ./")
        for copy_line in _node_install_script_prereq_copies(
            source_dir, copy_prefix, install=install
        ):
            if copy_line not in lines:
                lines.append(copy_line)
        return "\n".join(lines)

    lines = [f"COPY {copy_prefix}package*.json ./"]

    if source_dir is not None:
        root = _node_lifecycle_root(source_dir, copy_prefix)
        # 检测 npm workspaces monorepo，追加子包 package.json
        lines.extend(
            _node_workspace_package_copies(root, copy_prefix, _node_workspace_patterns(root))
        )

        # .npmrc 是构建配置（registry/peer 策略），与声明文件同属依赖层
        if (root / ".npmrc").is_file():
            lines.append(f"COPY {copy_prefix}.npmrc ./")

    # BUG-850：安装生命周期脚本引用的项目内文件（如根级 `preinstall: sh probe.sh`
    # 或 `node scripts/ensure-mirror.mjs`）必须随依赖层先于 `npm ci` 复制，
    # 否则 preinstall 在容器内因文件缺失必败。提取脚本命令中的路径 token，
    # 在与 COPY 前缀相同的工程根下解析为真实存在的文件/目录时，按其**顶层路径
    # 组件**追加 COPY。npm 脚本名和已 --ignore-scripts 的安装不复制。
    for copy_line in _node_install_script_prereq_copies(
        source_dir, copy_prefix, install=install
    ):
        if copy_line not in lines:
            lines.append(copy_line)

    return "\n".join(lines)


# npm 安装生命周期（与 scanner._INSTALL_LIFECYCLE_SCRIPT_KEYS 对应）
_NODE_LIFECYCLE_KEYS = ("preinstall", "install", "postinstall", "prepare")
_SHELL_OPERATOR_SPLIT_RE = re.compile(r"&&|\|\||;|\|")
# 已由依赖层复制的声明文件，不重复拷贝
_NODE_DECLARATION_FILES = {"package.json", "package-lock.json"}
# 包管理器的 ``run <脚本名>`` 参数不是路径；node/npx 的普通参数仍可能是文件。
_NODE_PACKAGE_BINS = {"npm", "pnpm", "yarn", "bun"}


def _node_lifecycle_root(source_dir: Path, copy_prefix: str) -> Path:
    """COPY 前缀对应的工程根：``current/`` → source_dir，``current/server/`` → source_dir/server。"""
    rest = copy_prefix[len("current/") :] if copy_prefix.startswith("current/") else copy_prefix
    rest = rest.strip("/")
    return source_dir.joinpath(rest) if rest else source_dir


def _node_install_script_prereq_copies(
    source_dir: Path | None, copy_prefix: str, *, install: str = ""
) -> list[str]:
    """BUG-850：安装生命周期脚本引用的本地文件 → COPY 行。

    读取和路径存在性检查都用与 COPY 前缀相同的工程根（``sourceSubdir`` 下的
    package.json，而不是仓库根的辅助 package.json）。只接受可解析为该工程内
    真实存在的文件/目录 token；引用不存在路径时不追加。目录按顶层组件整拷；
    node_modules、package*.json 与隐藏路径不在此机制内。

    ``npm run <名称>`` 的名称是脚本，不是文件。安装命令含 ``--ignore-scripts``
    时这些脚本不会执行，不再为它们复制。
    """
    if source_dir is None or "--ignore-scripts" in install:
        return []
    project_root = _node_lifecycle_root(source_dir, copy_prefix)
    root_pkg = project_root / "package.json"
    if not root_pkg.is_file():
        return []
    try:
        pkg = json.loads(root_pkg.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    scripts = pkg.get("scripts") if isinstance(pkg, dict) else None
    if not isinstance(scripts, dict):
        return []
    commands = [str(scripts.get(key) or "") for key in _NODE_LIFECYCLE_KEYS]
    if not any(commands):
        return []
    copies: list[str] = []
    seen_top: set[str] = set()
    for command in commands:
        for chunk in _SHELL_OPERATOR_SPLIT_RE.split(command):
            tokens: list[str] = []
            for raw in chunk.split():
                token = raw.strip("'\"")
                if token.startswith("./"):
                    token = token[2:]
                elif token.startswith(("-", "/", "$", "http://", "https://")):
                    continue
                if not token or any(c in token for c in "*?$"):
                    continue
                tokens.append(token)
            index = 0
            while index < len(tokens):
                token = tokens[index]
                if token in _NODE_PACKAGE_BINS:
                    if index + 2 < len(tokens) and tokens[index + 1] in ("run", "run-script"):
                        index += 3
                        continue
                    index += 1
                    continue
                candidate = project_root / token
                if not candidate.exists():
                    index += 1
                    continue
                try:
                    rel = candidate.resolve().relative_to(project_root.resolve())
                except ValueError:
                    index += 1
                    continue  # 越出工程根（如绝对路径）不拷
                if not rel.parts:
                    index += 1
                    continue
                top = rel.parts[0]
                if (
                    top in seen_top
                    or top in _NODE_DECLARATION_FILES
                    or top == "node_modules"
                    or top.startswith(".")
                ):
                    index += 1
                    continue
                seen_top.add(top)
                if (project_root / top).is_dir():
                    copies.append(f"COPY {copy_prefix}{top}/ {top}/")
                else:
                    copies.append(f"COPY {copy_prefix}{top} {top}")
                index += 1
    return copies


__all__ = ["generate_dockerfile", "generate_dockerignore"]
