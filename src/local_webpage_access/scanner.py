"""项目扫描与运行形态识别（WBS-08）。

用确定性规则识别 V1 支持的项目类型，推断技术栈、内部端口和安装/构建/启动命令。
无法判断时返回 ``confidence=low``，调用方应把实例标记为 pending。

对应 V1 设计说明第 5、12 节。
"""

from __future__ import annotations

import ast
import json
import re
import shlex
import tomllib  # Python 3.11+ 标准库（项目要求 >=3.13）
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from local_webpage_access.logging import get_logger
from local_webpage_access.node_toolchain import (
    describe_import_toolchain,
    inspect_node_toolchain,
    install_command_for,
)
from local_webpage_access.package_classifier import classify_and_select as _classify_monorepo
from local_webpage_access.preflight import check_and_fix as _preflight_check
from local_webpage_access.evidence_collector import collect as _collect_evidence
from local_webpage_access.candidate_generator import generate_candidates as _generate_candidates
from local_webpage_access.models import (
    DatabaseConfig,
    EntryConfig,
    Kind,
    ProjectEvidence,
    ResourceProfile,
    Runtime,
    ServingMode,
)

log = get_logger("scanner")

# ---- 框架特征常量 -----------------------------------------------------------

# BUG-866（#97）：astro 默认 output=static，是纯静态前端框架（astro examples/portfolio
# 即此类）；有 build 脚本即可按 frontend-static 部署。仅当声明 Node SSR adapter
# （@astrojs/node，见 NODE_BACKEND）时才应进后端容器形态。
NODE_FRONTEND = {
    "vite",
    "react",
    "react-dom",
    "vue",
    "@vitejs/plugin-react",
    "svelte",
    "preact",
    "astro",
}
# next/nuxt 不在 NODE_FRONTEND：二者生产形态是自带 Node 服务器的容器应用，
# 放入前端名单会把纯前端规则套到后端容器上（误判 frontend-static）。
NODE_BACKEND = {
    "express",
    "fastify",
    "koa",
    "@nestjs/core",
    "next",
    "nuxt",
    "@hono/node-server",
    "polka",
    "restana",
    # BUG-866（#97）：astro 的 Node SSR adapter——声明了它才说明项目是后端形态。
    "@astrojs/node",
}
PYTHON_WEB = {
    "flask": ("flask", 5000),
    "fastapi": ("fastapi", 8000),
    "uvicorn": ("uvicorn", 8000),
    "gunicorn": ("gunicorn", 8000),
    "django": ("django", 8000),
    "streamlit": ("streamlit", 8501),
    "gradio": ("gradio", 7860),
    "starlette": ("starlette", 8000),
    "sanic": ("sanic", 8000),
    "tornado": ("tornado", 8888),
}
# 选择主导框架的固定优先级（BUG-181）：_infer_python_port 与 _python_start_command
# 必须按同一优先级挑框架。否则 flask+gunicorn 等多框架项目会因 matched（源自 set
# 迭代）顺序随机，出现 internalPort 推断与启动命令端口不一致，部署出不可达实例。
_PYTHON_FRAMEWORK_PRIORITY = (
    "fastapi",
    "flask",
    "django",
    "streamlit",
    "gradio",
    "uvicorn",
    "gunicorn",
    "starlette",
    "sanic",
    "tornado",
)
# 真正需要外部服务的数据库客户端：命中即 pending（不自动启动，交人工/skills）。
HEAVY_DATABASES = {
    "psycopg2",
    "psycopg",
    "asyncpg",
    "pymysql",
    "mysqlclient",
    "aiomysql",
}
# BUG-868（#99）：内存缓存类依赖多为可选组件——uptime-kuma 声明 redis 但
# SQLite 单实例即可运行。单独出现时不标 pending，仅给 warning 级提示；
# 与真正重型依赖共存时仍随重型依赖走 pending。
OPTIONAL_CACHE_DATABASES = {"redis", "aioredis"}
SQLITE_MARKERS = {"sqlite3", "better-sqlite3", "sqlalchemy", "peewee", "tortoise-orm", "aiosqlite"}
SQLITE_FILE_EXT = (".sqlite", ".sqlite3", ".db")
# IMP-018（WBS-20260708 阶段2.3）：命中即把资源档位自动升 medium（已 medium/heavy 不降）。
# 这些库运行时常驻较重内存（向量库/张量/嵌入缓存），恒 512m 易 OOM（runtime §4.2-P8）。
HEAVY_RUNTIMES = {
    "lancedb",
    "pyarrow",
    "torch",
    "transformers",
    "tensorflow",
    "openai",
    "anthropic",
    "chromadb",
    "pymilvus",
}


class UvLockState(Enum):
    """uv.lock 内容状态（issue #13）。

    - VALID_NONEMPTY：TOML 合法且含 ``[[package]]`` 记录 → 走 ``uv sync``。
    - VALID_EMPTY：TOML 合法但无包记录（空壳）→ 回退其他依赖文件分支；
      ``uv sync --frozen`` 对空壳会静默装 0 个包且 exit 0，失败被推迟到
      运行期（uvicorn: not found），必须在导入期拦截。
    - INVALID / UNREADABLE：TOML 损坏 / 读取失败 → **不静默换源**，保持
      uv 路径并在 notes 里给出诊断，令错误在构建前可见、可归因。
    """

    MISSING = "missing"
    VALID_NONEMPTY = "valid_nonempty"
    VALID_EMPTY = "valid_empty"
    INVALID = "invalid"
    UNREADABLE = "unreadable"


# ---- 文件摘要 ---------------------------------------------------------------


@dataclass
class FileSummary:
    """项目目录的确定性事实摘要。"""

    root: Path
    workspace_root: Path | None = None  # 仓库根；summarize_package 时与 root（子包）不同
    top_files: set[str] = field(default_factory=set)  # 顶层文件名（小写）
    has_index_html: bool = False
    has_html: bool = False  # 任意 .html（含非 index 名）
    has_package_json: bool = False
    has_requirements_txt: bool = False
    has_requirements_prod: bool = False
    has_pyproject_toml: bool = False
    has_pipfile: bool = False
    has_uv_lock: bool = False
    # issue #13：uv.lock 内容状态（四态，见 UvLockState）。
    uv_lock_state: "UvLockState" = UvLockState.MISSING
    has_manage_py: bool = False
    has_alembic_ini: bool = False  # IMP-052：顶层 alembic.ini
    has_runtime_paths: bool = False  # BUG-198：src/app/runtime_paths.py 等
    node_deps: dict[str, str] = field(
        default_factory=dict
    )  # 包名 -> 版本（含 devDependencies，BUG-019）
    node_runtime_deps: dict[str, str] = field(default_factory=dict)
    node_scripts: dict[str, str] = field(default_factory=dict)
    python_deps: set[str] = field(default_factory=set)
    sqlite_files: list[str] = field(default_factory=list)
    total_files: int = 0


def summarize(root: Path) -> FileSummary:
    """收集项目目录的文件摘要（仅扫顶层 + 浅层，避免大目录过慢）。"""
    summary = FileSummary(root=root, workspace_root=root)

    # 顶层文件
    for entry in root.iterdir():
        if entry.is_file():
            name = entry.name.lower()
            summary.top_files.add(name)
            summary.total_files += 1
            if name.endswith(".html"):
                summary.has_html = True
            if name == "index.html":
                summary.has_index_html = True
            elif name == "package.json":
                summary.has_package_json = True
            elif name == "requirements.txt":
                summary.has_requirements_txt = True
            elif name == "requirements-prod.txt":
                # IMP-017：生产依赖分离文件（含依赖、剔除测试包），优先于 requirements.txt
                summary.has_requirements_prod = True
            elif name == "pyproject.toml":
                summary.has_pyproject_toml = True
            elif name == "pipfile":
                summary.has_pipfile = True
            elif name == "uv.lock":
                summary.has_uv_lock = True
                summary.uv_lock_state = _uv_lock_state(entry)
            elif name == "manage.py":
                summary.has_manage_py = True
            elif name == "alembic.ini":
                summary.has_alembic_ini = True
            if name.endswith(SQLITE_FILE_EXT):
                summary.sqlite_files.append(entry.name)

    # 深度受限的递归统计子目录文件（最多 3 层）；
    # 顶层文件已在上方循环统计，这里跳过 path.parent == root 的项避免重复计数
    for path in _walk(root, max_depth=3):
        if path.is_file() and path.parent != root:
            summary.total_files += 1
            if path.name.lower().endswith(SQLITE_FILE_EXT):
                rel = path.relative_to(root)
                summary.sqlite_files.append(str(rel).replace("\\", "/"))

    # BUG-349：只用相对项目根的显式路径判定 runtime_paths，避免工作区绝对路径
    # 含 "app" 段时全局误判。
    if (root / "src" / "app" / "runtime_paths.py").is_file() or (
        root / "app" / "runtime_paths.py"
    ).is_file():
        summary.has_runtime_paths = True

    if summary.has_package_json:
        pkg = _read_package_json(root / "package.json")
        # 合并 dependencies 与 devDependencies（BUG-019）：Vite / Svelte / 框架
        # 插件常放在 devDependencies，只看 dependencies 会把这类前端模板误判
        # 为 pending。版本以 dependencies 优先（dev 多为工具链，版本无关识别）。
        node_deps: dict[str, str] = {}
        node_deps.update(pkg.get("devDependencies", {}) or {})
        node_deps.update(pkg.get("dependencies", {}) or {})
        summary.node_deps = node_deps
        summary.node_runtime_deps = dict(pkg.get("dependencies", {}) or {})
        summary.node_scripts = pkg.get("scripts", {}) or {}

    summary.python_deps = _collect_python_deps(root, summary)

    return summary


def summarize_package(root: Path, package_path: str) -> FileSummary:
    """为主包子目录构造 FileSummary（IMP-057 §4.5）。

    deps/scripts 来自子包 ``package.json``；锁文件/包管理器仍看仓库根
    （install 在根执行）。
    """
    pkg_dir = root / package_path
    summary = FileSummary(root=pkg_dir, workspace_root=root)

    # 子包 package.json 的 deps/scripts
    pkg = _read_package_json(pkg_dir / "package.json")
    if pkg:
        summary.has_package_json = True
        node_deps: dict[str, str] = {}
        node_deps.update(pkg.get("devDependencies", {}) or {})
        node_deps.update(pkg.get("dependencies", {}) or {})
        summary.node_deps = node_deps
        summary.node_runtime_deps = dict(pkg.get("dependencies", {}) or {})
        summary.node_scripts = pkg.get("scripts", {}) or {}

    # 锁文件仍看仓库根（install 在根执行）
    root_files: set[str] = set()
    try:
        for entry in root.iterdir():
            if entry.is_file():
                root_files.add(entry.name.lower())
    except (PermissionError, OSError):
        pass
    summary.top_files = root_files  # _node_install_command 直接检查 top_files 中的锁文件名

    # SQLite 文件在子包目录中查找
    for path in _walk(pkg_dir, max_depth=3):
        if path.is_file() and path.name.lower().endswith(SQLITE_FILE_EXT):
            rel = path.relative_to(pkg_dir)
            summary.sqlite_files.append(str(rel).replace("\\", "/"))
            summary.total_files += 1

    # runtime_paths 检查（在子包目录中）
    if (pkg_dir / "src" / "app" / "runtime_paths.py").is_file() or (
        pkg_dir / "app" / "runtime_paths.py"
    ).is_file():
        summary.has_runtime_paths = True

    return summary


def _walk(root: Path, *, max_depth: int):
    """受限深度遍历，跳过常见大目录。"""
    skip = {"node_modules", ".git", ".venv", "venv", "__pycache__", "dist", "build", ".next"}
    stack = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        if depth >= max_depth:
            continue
        try:
            for entry in current.iterdir():
                if entry.is_dir():
                    if entry.name.lower() in skip:
                        continue
                    stack.append((entry, depth + 1))
                else:
                    yield entry
        except (PermissionError, OSError):
            continue


def _read_package_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _read_requirements(path: Path) -> set[str]:
    deps: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return deps
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        pkg = re.split(r"[<>=!\[ ]", line, maxsplit=1)[0].strip().lower()
        if pkg:
            deps.add(pkg)
    return deps


def _read_pipfile(path: Path) -> set[str]:
    """读取 Pipfile（TOML 格式）中的依赖包名。

    Pipfile 不是 requirements 格式，按行解析会把 ``[[source]]`` 段的
    ``name``/``url``/``verify_ssl`` 等键误当作依赖，因此必须按 TOML 解析，
    只取 ``[packages]`` 与 ``[dev-packages]`` 段的键。
    """
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return set()
    deps: set[str] = set()
    for section in ("packages", "dev-packages"):
        items = data.get(section, {})
        if isinstance(items, dict):
            for key in items:
                name = key.strip().lower()
                if name:
                    deps.add(name)
    return deps


def _read_poetry_deps(data: dict) -> set[str]:
    """读取 Poetry 依赖（BUG-502）。

    Poetry 项目把依赖声明在 ``[tool.poetry.dependencies]``、``[tool.poetry.dev-dependencies]``
    以及 ``[tool.poetry.group.<name>.dependencies]``，而非 PEP 621 的
    ``[project.dependencies]``。仅解析 PEP 621 会把 Poetry-only 声明的
    FastAPI 等项目误判 pending。忽略 ``python`` 这一版本约束键。
    """
    deps: set[str] = set()
    poetry = data.get("tool", {}).get("poetry", {})
    if not isinstance(poetry, dict):
        return deps
    sections: list[object] = [poetry.get("dependencies"), poetry.get("dev-dependencies")]
    groups = poetry.get("group", {})
    if isinstance(groups, dict):
        for group in groups.values():
            if isinstance(group, dict):
                sections.append(group.get("dependencies"))
    for items in sections:
        if not isinstance(items, dict):
            continue
        for key in items:
            name = key.strip().lower()
            if name and name != "python":
                deps.add(name)
    return deps


def _collect_python_deps(root: Path, summary: FileSummary) -> set[str]:
    deps: set[str] = set()
    if summary.has_requirements_txt:
        deps |= _read_requirements(root / "requirements.txt")
    # IMP-017：requirements-prod.txt 是 requirements.txt 的生产子集（剔除测试包），
    # 同样参与依赖识别（重依赖/数据库探测需要看到其中包名）。
    if summary.has_requirements_prod:
        deps |= _read_requirements(root / "requirements-prod.txt")
    if summary.has_pipfile:
        deps |= _read_pipfile(root / "Pipfile")
    if summary.has_pyproject_toml:
        try:
            with (root / "pyproject.toml").open("rb") as fh:
                data = tomllib.load(fh)
            for section in ("dependencies", "dev-dependencies"):
                items = data.get("project", {}).get(section, [])
                if isinstance(items, list):
                    for item in items:
                        deps.add(re.split(r"[<>=!\[ ]", str(item), maxsplit=1)[0].strip().lower())
            # BUG-502：Poetry 项目把依赖声明在 [tool.poetry.dependencies]，
            # 仅读 PEP 621 project.dependencies 会漏掉 Poetry-only 的 FastAPI 等。
            deps |= _read_poetry_deps(data)
        except (OSError, tomllib.TOMLDecodeError):
            pass
    return deps


# ---- Monorepo workspace 构建/启动辅助（S2-10 / BUG-760）-----------------------


def _workspace_scripts(root: Path, rel_path: str) -> dict:
    """读取 workspace 子包的 scripts（缺失/损坏返回空 dict）。"""
    pkg = _read_package_json(root / rel_path / "package.json")
    scripts = pkg.get("scripts")
    return scripts if isinstance(scripts, dict) else {}


def _workspace_build_order(
    root: Path,
    classifications,
    primary,
) -> tuple[list[tuple[str, str]], bool]:
    """计算需要先构建的 workspace 列表（被依赖者在前）。

    只收录主包及其传递 workspace 依赖中**有 build 脚本**的子包；拓扑排序
    依据是各子包 ``dependencies`` 命中的 workspace 包名。图中成环时退化为
    路径字典序并返回 ``cycle=True``（调用方写 note，不静默出错）。

    返回 ``[(包名, 相对路径), ...]`` 与成环标记。
    """
    by_name = {c.name: c for c in classifications}
    relevant: set[str] = set()

    # 主包 + 传递 workspace 依赖（BFS）
    queue = [primary.name]
    seen = {primary.name}
    while queue:
        current = queue.pop()
        relevant.add(current)
        cls = by_name.get(current)
        if cls is None:
            continue
        pkg = _read_package_json(root / cls.path / "package.json")
        for dep in (pkg.get("dependencies") or {}):
            if dep in by_name and dep not in seen:
                seen.add(dep)
                queue.append(dep)

    # 边：依赖者 -> 被依赖者（Kahn 从被依赖者开始）
    edges: dict[str, set[str]] = {}
    for name in relevant:
        cls = by_name[name]
        pkg = _read_package_json(root / cls.path / "package.json")
        edges[name] = {d for d in (pkg.get("dependencies") or {}) if d in relevant and d != name}

    ordered: list[str] = []
    ready = sorted(n for n in relevant if not edges[n])
    cycle = False
    while ready:
        name = ready.pop(0)
        ordered.append(name)
        for other in sorted(relevant - set(ordered)):
            if name in edges[other]:
                edges[other].discard(name)
                if not edges[other]:
                    ready.append(other)
        ready.sort()
    if len(ordered) != len(relevant):
        cycle = True
        ordered = sorted(relevant)

    buildable = [
        (name, by_name[name].path)
        for name in ordered
        if "build" in _workspace_scripts(root, by_name[name].path)
    ]
    return buildable, cycle


def _workspace_build_command(build_order: list[tuple[str, str]], top_files: set[str]) -> str | None:
    """按包管理器把 build 序列合成一条 ``&&`` 连接命令。

    npm workspaces 用 ``npm run build -w <name>``；pnpm/yarn 项目使用各自
    的 workspace 语法。无任一子包需要构建时返回 ``None``。
    """
    if not build_order:
        return None
    steps: list[str] = []
    for name, _ in build_order:
        quoted = shlex.quote(str(name))
        if "pnpm-lock.yaml" in top_files:
            steps.append(f"pnpm --filter {quoted} run build")
        elif "yarn.lock" in top_files:
            steps.append(f"yarn workspace {quoted} run build")
        else:
            steps.append(f"npm run build -w {quoted}")
    return " && ".join(steps)


# dev 模式启动命令前缀（tsx/ts-node/nodemon 等在容器内需要 devDependencies，
# 且不是生产形态）；vite 前缀只匹配独立 vite 命令，避免误伤 "vite build"（
# 那是构建脚本，不会走到 start 判定）。
_DEV_NODE_SCRIPT_PREFIXES = ("tsx ", "ts-node ", "ts-node-dev ", "nodemon ")

# BUG-871（#102）：monorepo 根 start 脚本的包管理器包装——
# ``yarn --cwd ./excalidraw-app start`` / ``yarn workspace <name> run <script>`` /
# ``pnpm --dir <dir> ...`` / ``npm --prefix <dir> run ...``。只认归一化前缀会把
# 间接 vite/tsx dev server 误判为生产启动（excalidraw 实案：纯前端被误判
# backend-container），必须先解包再按目标脚本判定。
_PM_DIR_WRAP_RE = re.compile(r"^(?:yarn|pnpm|npm)\s+(?:--cwd|--dir|--prefix)\s+\S+\s+(.+)$")
_PM_WORKSPACE_WRAP_RE = re.compile(r"^yarn\s+workspace\s+\S+\s+(?:run\s+)?(.+)$")


def _unwrap_pm_script(command: str) -> str:
    """剥掉 yarn/pnpm/npm 的目录/workspace 包装，返回目标脚本命令。

    嵌套包装最多解 3 层（防异常输入死循环）；无法解包时原样返回。
    解包后残留的 ``run`` 连接词（``npm --prefix ./app run vite``）也一并剥掉。
    """
    current = command
    for _ in range(3):
        m = _PM_DIR_WRAP_RE.match(current) or _PM_WORKSPACE_WRAP_RE.match(current)
        if not m:
            break
        current = m.group(1).strip()
    if current.startswith("run "):
        current = current[4:].strip()
    return current


def _is_dev_node_script(script: str | None) -> bool:
    """start 脚本是否为 dev 模式（tsx/ts-node/nodemon/vite dev server）。"""
    normalized = " ".join((script or "").strip().lower().split())
    if not normalized:
        return False
    # BUG-871（#102）：先解包 yarn --cwd / yarn workspace / pnpm --dir /
    # npm --prefix 包装，再对目标脚本做 dev 前缀判定。
    normalized = _unwrap_pm_script(normalized)
    if not normalized:
        return False
    if normalized.startswith(_DEV_NODE_SCRIPT_PREFIXES):
        return True
    return normalized == "vite" or normalized.startswith("vite ")


def _dev_script_hint(script: str) -> str:
    return " ".join((script or "").split())[:60]


# BUG-788：runner 命令后紧跟的监听/调试标志，不是入口文件——
# ``tsx watch src/server.ts`` 曾把 ``watch`` 当成入口，错合成
# ``node packages/webpage/dist/watch``（实案：patent-disclosure-workflow-v1）。
_RUNNER_NON_ENTRY_TOKENS = frozenset({"watch", "inspect", "inspect-brk"})
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# 入口文件名含 watch/dev/hot 的多为开发辅助文件；生产入口优先 server/index/main。
_DEVISH_ENTRY_STEM_RE = re.compile(r"watch|dev|hot", re.IGNORECASE)
# 生产入口探测顺序：server 优先，其次 main/index（含 src/ 与包根两级）。
_PRODUCTION_ENTRY_CANDIDATES = (
    "src/server.ts",
    "src/server.js",
    "src/main.ts",
    "src/main.js",
    "src/index.ts",
    "src/index.js",
    "server.ts",
    "server.js",
    "main.ts",
    "main.js",
    "index.ts",
    "index.js",
)


def _entry_from_dev_script(script: str) -> str | None:
    """从 dev/start 脚本提取入口源文件，跳过 runner 标志与 env 赋值。

    逐个取 ``tsx|ts-node|ts-node-dev|nodemon`` 之后的 token：跳过 ``-`` 开头
    的选项、``watch``/``inspect`` 等 runner 标志与 ``NODE_ENV=...`` 形式 env
    赋值，首个普通 token 即入口文件。无有效 token 返回 ``None``。
    """
    m = re.search(r"\b(?:tsx|ts-node|ts-node-dev|nodemon)\s+(.+)$", script)
    if not m:
        return None
    for token in m.group(1).split():
        if token.startswith("-"):
            continue
        if token.lower() in _RUNNER_NON_ENTRY_TOKENS:
            continue
        if _ENV_ASSIGN_RE.match(token):
            continue
        return token
    return None


def _synthesize_production_start(
    root: Path, pkg_path: str, scripts: dict[str, str]
) -> str | None:
    """从 dev 脚本 + tsconfig 推断生产启动命令 ``node <pkg>/<outDir>/<entry>``。

    依据：
    1. dev/start 脚本中的入口源文件（``tsx src/index.ts`` -> ``src/index.ts``；
       ``tsx watch src/server.ts`` 的 ``watch`` 标志会被跳过，BUG-788）；
    2. ``tsconfig.json`` 的 ``compilerOptions.outDir``（缺省 ``dist``）；
    3. ``src/`` 前缀映射到 outDir 根（tsc 常见 rootDir 布局）；
    4. 入口名含 watch/dev/hot 时（如 ``tsx src/dev.ts``）优先回退到
       ``server/index/main`` 生产入口（BUG-788）。

    无法推断（无入口源文件且无 tsconfig）时返回 ``None``，由调用方回退。
    """
    entry_src: str | None = None
    for script_name in ("start", "dev", "server"):
        script = scripts.get(script_name)
        if not script:
            continue
        entry_src = _entry_from_dev_script(script)
        if entry_src:
            break
    pkg_dir = root / pkg_path
    if entry_src is None:
        for candidate in _PRODUCTION_ENTRY_CANDIDATES:
            if (pkg_dir / candidate).is_file():
                entry_src = candidate
                break
    if entry_src is None:
        return None

    tsconfig = _read_package_json(pkg_dir / "tsconfig.json")
    compiler_options = tsconfig.get("compilerOptions")
    out_dir = "dist"
    root_dir: str | None = None
    if isinstance(compiler_options, dict):
        if isinstance(compiler_options.get("outDir"), str):
            out_dir = compiler_options["outDir"].strip().strip("./").strip("/") or "dist"
        if isinstance(compiler_options.get("rootDir"), str):
            root_dir = compiler_options["rootDir"].strip().strip("./").strip("/") or ""

    def _to_out_module(src_rel: str) -> str:
        js = re.sub(r"\.(ts|mts|cts)$", ".js", src_rel.lstrip("./"))
        if root_dir is None:
            # 未声明 rootDir：tsc 默认公共源根多为 src/，沿用前缀剥离启发。
            if js.startswith("src/"):
                js = js[4:]
            return js
        # 显式 rootDir（BUG-839）：输出为 outDir 下相对 rootDir 的路径——
        # rootDir="." 保留 src/ 前缀；入口不在 rootDir 内时退回启发式。
        if root_dir:
            prefix = root_dir + "/"
            if js.startswith(prefix):
                return js[len(prefix):]
            if js.startswith("src/"):
                js = js[4:]
        return js

    module_js = _to_out_module(entry_src)
    if _DEVISH_ENTRY_STEM_RE.search(Path(module_js).stem):
        # 入口名含 watch/dev/hot：多为开发辅助文件，优先 server/index/main。
        for candidate in _PRODUCTION_ENTRY_CANDIDATES:
            if (pkg_dir / candidate).is_file():
                alt = _to_out_module(candidate)
                if alt != module_js:
                    return f"node {pkg_path}/{out_dir}/{alt}"
        # 无更优入口：沿用原推断（不返回 None，避免无谓回退 dev 模式）。
    return f"node {pkg_path}/{out_dir}/{module_js}"


# ---- 识别结果 ---------------------------------------------------------------


@dataclass
class DetectionResult:
    """扫描识别结果，供 importer 构建 manifest 使用。"""

    kind: Kind | None = None
    runtime: Runtime | None = None
    servingMode: ServingMode | None = None
    resourceProfile: ResourceProfile = ResourceProfile.SMALL
    form: str = (
        "unknown"  # static / frontend-static / backend-container / fullstack-sqlite / unknown
    )
    stack: list[str] = field(default_factory=list)
    hasDatabase: bool = False
    database: DatabaseConfig | None = None
    entry: EntryConfig = field(default_factory=EntryConfig)
    internalPort: int | None = None
    # BUG-788：internalPort 是弱信号兜底值（源码只读 process.env.PORT、无字面
    # 回退端口，实际端口由 compose 注入的环境决定）时为 True——update 场景
    # importer 侧应保留 manifest 既有 internalPort，而非用兜底值覆盖用户修正
    # （与 BUG-787「update 保留运维态」同向；importer 侧由另一变更处理）。
    internalPortGuessed: bool = False
    confidence: str = "low"  # high / medium / low
    pending: bool = False
    notes: list[str] = field(default_factory=list)
    # IMP-057 Gate-1：Monorepo 包分类结果
    classifications: list = field(default_factory=list)  # list[PackageClassification]
    primary_package: str | None = None  # 主包相对路径（如 "packages/webpage"）
    # IMP-058 Gate-B：多候选识别
    source_subdir: str | None = None  # 源码子目录（如 "backend"），None = 根目录
    evidence: ProjectEvidence | None = None  # Layer 0 输出
    candidates: list = field(default_factory=list)  # list[DeploymentCandidate]（Layer 1 输出）


# ---- 扫描器 -----------------------------------------------------------------


def _apply_preflight(result: DetectionResult, preflight) -> None:
    """把预检结果合并进 :class:`DetectionResult`，REJECTED 时置 pending（BUG-498）。

    预检拒绝意味着配置不可部署（如 COPY 源缺失、entrypoint 脚本缺失），
    必须让失败出声：置 pending 阻断导入，而非仅记录 notes 继续假装识别成功。
    """
    if preflight.notes:
        result.notes.extend(preflight.notes)
    if preflight.warnings:
        result.notes.extend(f"[预检警告] {w}" for w in preflight.warnings)
    if preflight.rejections:
        result.notes.extend(f"[预检拒绝] {r}" for r in preflight.rejections)
        result.pending = True
        result.confidence = "low"


class Scanner:
    """项目类型识别器（确定性规则）。"""

    def detect(self, project_dir: Path) -> DetectionResult:
        result = self._detect_impl(project_dir)
        # #61（检查部分 / DEV-164）：预构建前端产物时效性——统一收口（所有识别
        # 分支共用一次检查）；疑似陈旧才入扫描提示（可行动），「无法判断」由
        # doctor 汇总避免噪音。
        from local_webpage_access.frontend_staleness import (
            LEVEL_SUSPECTED,
            check_frontend_artifacts_staleness,
        )

        try:
            staleness = check_frontend_artifacts_staleness(project_dir)
        except Exception:  # noqa: BLE001 — 时效性检查不得阻断识别
            return result
        if staleness.level == LEVEL_SUSPECTED:
            result.notes.append(
                f"[预检警告] #61：{staleness.message}——{staleness.suggestion}"
            )
        return result

    def _detect_impl(self, project_dir: Path) -> DetectionResult:
        if not project_dir.is_dir():
            return DetectionResult(confidence="low", pending=True, notes=["项目目录不存在"])

        summary = summarize(project_dir)
        result = DetectionResult()

        # IMP-057 Gate-1：Monorepo 包分类。
        # 在常规识别前检测 npm workspaces monorepo，若存在则按主包摘要识别。
        mono = _classify_monorepo(project_dir)
        if mono.is_monorepo:
            result.classifications = mono.classifications
            result.notes.extend(mono.notes)
            if mono.primary is None:
                # 0 或 >=2 个可部署子包：pending
                result.pending = True
                result.confidence = "low"
                result.kind = Kind.NODE
                result.notes.extend(describe_import_toolchain(project_dir))
                # 仍执行预检（无害）
                preflight = _preflight_check(result, project_dir)
                _apply_preflight(result, preflight)
                return result
            # 有主包：用主包摘要替代根摘要
            result.primary_package = mono.primary.path
            summary = summarize_package(project_dir, mono.primary.path)
            # 检测 SQLite（基于主包摘要）
            self._detect_sqlite(summary, result)
            # 用主包摘要识别 Node 项目
            self._detect_node(summary, result)
            # IMP-057 §4.5：entry 使用 -w <name> 语义
            self._apply_workspace_entry(result, mono, project_dir, summary)
            # 预检
            preflight = _preflight_check(result, project_dir)
            _apply_preflight(result, preflight)
            return result

        # IMP-058 Gate-B：Layer 0+1 多候选识别。
        # 收集证据并生成候选列表，检测子目录布局（如 backend/+frontend/）。
        evidence = _collect_evidence(project_dir)
        result.evidence = evidence
        candidates = _generate_candidates(evidence)
        result.candidates = candidates

        # 子目录布局检测（DEV-105 / BUG-495 / BUG-496）：消费 top primary 候选。
        # 此前仅对 kind=="python" 的子目录候选提前返回，导致 frontend/ Vite 子目录
        # 被误判 static、server/ Express 子目录被判 pending。改为按候选 kind 路由。
        subdir_candidate = next(
            (c for c in candidates if c.sourceSubdir and c.confidenceTier == "primary"),
            None,
        )
        if subdir_candidate and subdir_candidate.sourceSubdir:
            # 用子目录作为工程根进行识别
            subdir = project_dir / subdir_candidate.sourceSubdir
            subdir_summary = summarize(subdir)
            subdir_summary.workspace_root = project_dir
            result.source_subdir = subdir_candidate.sourceSubdir

            # BUG-497：子目录重型数据库依赖也要标记 pending（此前只在根目录检查）。
            heavy = self._detect_heavy_db(subdir_summary)
            if heavy:
                result.pending = True
                result.confidence = "medium"
                result.notes.append(
                    f"检测到重型数据库依赖：{', '.join(sorted(heavy))}，标记 pending"
                )
                self._fill_language(subdir_summary, result)
                result.notes.append(f"[子目录识别] 源码在 {subdir_candidate.sourceSubdir}/ 子目录")
                return result

            # BUG-868（#99）：子目录的可选缓存依赖同样只提示不拦（与根目录口径一致）
            optional_db = self._detect_optional_db_deps(subdir_summary)
            if optional_db:
                result.notes.append(
                    f"[预检警告] 子目录检测到可选缓存/数据库依赖："
                    f"{', '.join(sorted(optional_db))}——通常为可选组件，运行失败时请人工确认"
                )

            self._detect_sqlite(subdir_summary, result)
            if subdir_candidate.kind == "python":
                # 检测 Python 项目
                self._detect_python(subdir_summary, result)
                # 修正 install 命令中的 requirements 路径（若子目录有 requirements.txt）
                if subdir_summary.has_requirements_txt and result.entry.install:
                    result.entry.install = "pip install -r requirements.txt"
                elif subdir_summary.has_requirements_prod and result.entry.install:
                    result.entry.install = "pip install -r requirements-prod.txt"
            elif subdir_candidate.kind == "node":
                # BUG-878（#109）：Go 主服务 + web/ 前端布局（memos 实案）——
                # 前端子目录单独部署不是完整服务，判 pending 并明确提示。
                if (
                    subdir_candidate.form == "frontend-static"
                    and _has_go_main_service(project_dir, summary)
                ):
                    result.pending = True
                    result.confidence = "low"
                    result.notes.append(_go_main_service_note())
                    result.notes.append(
                        f"[子目录识别] 源码在 {subdir_candidate.sourceSubdir}/ 子目录"
                    )
                    return result
                # BUG-495/BUG-496：Node 子目录（frontend 或 backend）也走子目录识别。
                self._detect_node(subdir_summary, result)
                if subdir_candidate.form == "frontend-static":
                    # 纯前端子目录：构建产物在 <subdir>/dist，供 hosting 定位
                    result.entry.buildOutputDir = f"{subdir_candidate.sourceSubdir}/dist"
            else:
                result.pending = True
                result.confidence = "low"
                result.notes.append(
                    f"子目录候选 kind={subdir_candidate.kind} 无法识别，标记 pending"
                )

            # 预检
            preflight = _preflight_check(result, project_dir)
            _apply_preflight(result, preflight)
            result.notes.append(f"[子目录识别] 源码在 {subdir_candidate.sourceSubdir}/ 子目录")
            return result

        # 1. 重型数据库优先判断（不自动启动）
        heavy = self._detect_heavy_db(summary)
        if heavy:
            result.pending = True
            result.confidence = "medium"
            result.notes.append(f"检测到重型数据库依赖：{heavy}，标记 pending")
            # 仍尝试识别语言族，方便 skill 处理
            self._fill_language(summary, result)
            return result

        # 1.5 可选缓存/数据库依赖（BUG-868，#99）：不拦 pending，仅 warning 提示
        optional_db = self._detect_optional_db_deps(summary)
        if optional_db:
            result.notes.append(
                f"[预检警告] 检测到可选缓存/数据库依赖：{', '.join(sorted(optional_db))}"
                "——通常为可选组件（未配置外部服务时应用多回退内置 SQLite）；"
                "运行失败时请人工确认是否需要外部服务"
            )

        # 2. 数据库（SQLite）
        self._detect_sqlite(summary, result)

        # 3. 识别主类型
        # IMP-013（WBS-20260708 阶段2.1）：判定顺序改为「真 Node → Python → static」。
        # 仅当 package.json 命中 NODE_FRONTEND/NODE_BACKEND 才视为真 Node 项目；
        # 辅助 package.json（如 pi-agent 仅含 dev 工具链）落到 Python 分支，
        # 避免 prd-workflow 这类"Python + 辅助 Node"项目被误判 pending/static。
        has_python_signal = (
            summary.has_pyproject_toml
            or summary.has_requirements_txt
            or summary.has_requirements_prod
            or summary.has_pipfile
        )
        # issue#1：零依赖 stdlib HTTP 服务（仅 server.py/app.py/main.py + http.server）
        # 无任何 Python 工程文件时路由到 Python 分支做弱信号识别。
        # CHK-225 高危2：stdlib 弱信号必须让位于更强的静态站信号--
        # index.html + 一行 import http.server 预览辅助脚本（极常见）应保持
        # high 置信度 static，而非降级为 medium 置信度容器应用。
        if summary.has_package_json and self._is_real_node(summary):
            self._detect_node(summary, result)
        elif has_python_signal:
            self._detect_python(summary, result)
        elif summary.has_package_json:
            # package.json 存在但既非真 Node 也无 Python 工程文件：仍按 Node 兜底尝试
            self._detect_node(summary, result)
        elif summary.has_index_html or summary.has_html:
            self._detect_static(summary, result)
        elif _stdlib_http_entry(summary) is not None:
            self._detect_python(summary, result)
        else:
            # 兜底：子目录 index.html，或任意可打开的 .html
            if _has_index_anywhere(project_dir) or _has_html_anywhere(project_dir):
                self._detect_static(summary, result)
            elif _has_go_main_service(project_dir, summary):
                # BUG-878（#109）：Go 主服务超出当前支持范围，明确提示而非笼统 unknown
                result.pending = True
                result.confidence = "low"
                result.notes.append(_go_main_service_note())
            else:
                result.pending = True
                result.confidence = "low"
                result.notes.append("无法识别项目类型，标记 pending")

        # IMP-058 Gate-A：Layer 2 静态预检（不 build，毫秒级检查配置可行性）。
        # 自动修正 shell 操作符、alembic cwd 等，修正事件写入 notes。
        # REJECTED 时置 pending（BUG-498：破损门禁不再静默放行）。
        preflight = _preflight_check(result, project_dir)
        _apply_preflight(result, preflight)

        return result

    # ---- Monorepo entry 编排（IMP-057 §4.5）---------------------------------

    def _apply_workspace_entry(
        self,
        result: DetectionResult,
        mono,
        project_dir: Path,
        summary: FileSummary,
    ) -> None:
        """将 entry.build/start 改为 workspace 语义（``-w <name>`` 或生产模式）。

        install 仍在仓库根执行（由 ``_node_install_command`` 决定）。
        若主包无 ``name`` 字段，使用路径作为 ``-w`` 参数。

        S2-10 / BUG-760：build 按 workspace 内部依赖拓扑排序，先构建被依赖的
        子包（如 ``core``）；start 优先生产模式——主包 start 脚本若是
        ``tsx``/``ts-node`` 等 dev 形态且可推断产物路径，改用
        ``node <pkg>/<outDir>/<entry>``，不再以 dev 模式启动。

        BUG-788：install 在仓库根执行，根/主包有 install 生命周期脚本时补
        ``--ignore-scripts``；start 合成会跳过 ``tsx watch`` 的 ``watch`` 标志
        并排除 watch/dev/hot 类入口文件（优先 server/index/main）。
        """
        primary = mono.primary
        pkg_name = primary.name
        workspace_arg = shlex.quote(str(pkg_name))
        scripts = summary.node_scripts

        # build：主包 + 传递 workspace 依赖中「有 build 脚本」者，按依赖拓扑序
        build_order, cycle = _workspace_build_order(project_dir, mono.classifications, primary)
        result.entry.build = _workspace_build_command(build_order, summary.top_files)
        if cycle:
            result.notes.append(
                "workspace 依赖成环，build 顺序退化为字典序；请人工核对构建顺序"
            )

        # start：web_server -> 生产模式优先；frontend-static 为 None（静态托管）
        if result.form == "frontend-static":
            result.entry.start = None
        else:
            chosen = "start" if "start" in scripts else ("server" if "server" in scripts else None)
            if chosen is not None and _is_dev_node_script(scripts[chosen]):
                synthesized = _synthesize_production_start(project_dir, primary.path, scripts)
                if synthesized is not None:
                    result.entry.start = synthesized
                    result.notes.append(
                        f"entry: start 脚本为 dev 模式（{_dev_script_hint(scripts[chosen])}），"
                        f"已改用生产模式启动 {synthesized}"
                    )
                else:
                    result.entry.start = f"npm run {chosen} -w {workspace_arg}"
                    result.notes.append(
                        f"entry: start 脚本为 dev 模式且无法推断构建产物路径，"
                        f"回退 npm run {chosen} -w {pkg_name}（容器内将安装 devDependencies）"
                    )
            elif chosen is not None:
                result.entry.start = f"npm run {chosen} -w {workspace_arg}"
            else:
                # 无 start/server 脚本：node-stdlib-http 弱信号直接以 node 启动入口
                node_stdlib = _node_stdlib_http_entry(summary)
                if node_stdlib is not None:
                    entry_file, _ = node_stdlib
                    result.entry.start = f"node {primary.path}/{entry_file}"
                # 否则保持 _detect_node 已设置的 start

        # BUG-788：install 在仓库根执行，根（或主包）声明 install 生命周期脚本
        # （如 ensure-electron-mirror.mjs，依赖宿主机环境）时容器内必败，
        # 补 --ignore-scripts。
        root_scripts = _read_package_json(project_dir / "package.json").get("scripts")
        root_scripts = root_scripts if isinstance(root_scripts, dict) else {}
        if _has_install_lifecycle_scripts(root_scripts) or _has_install_lifecycle_scripts(
            summary.node_scripts
        ):
            merged_scripts = {**summary.node_scripts, **root_scripts}
            guarded = _guard_install_scripts(result.entry.install, merged_scripts)
            if guarded != result.entry.install:
                result.entry.install = guarded
                result.notes.append(
                    "entry: 检测到 install 生命周期脚本（preinstall/install/postinstall/prepare），"
                    f"install 已补 --ignore-scripts：{guarded}"
                )

        # A.07: frontend_build 主包产物路径策略
        # 构建产物在子包目录下（packages/<name>/dist），需告诉 hosting 去哪找
        if result.form == "frontend-static" and primary.path:
            result.entry.buildOutputDir = f"{primary.path}/dist"
        built = ", ".join(name for name, _ in build_order) if build_order else "(无 build)"
        result.notes.append(f"entry: install@root, build[{built}], start -w {pkg_name}")

    # ---- 语言族填充 --------------------------------------------------------

    def _fill_language(self, summary: FileSummary, result: DetectionResult) -> None:
        if summary.has_package_json:
            result.kind = Kind.NODE
        elif (
            summary.has_pyproject_toml
            or summary.has_requirements_txt
            or summary.has_requirements_prod
            or summary.has_pipfile
        ):
            result.kind = Kind.PYTHON

    def _is_real_node(self, summary: FileSummary) -> bool:
        """IMP-013：package.json 是否代表真正的 Node 应用。

        仅当依赖命中 :data:`NODE_FRONTEND`（Vite/React/Vue …）或
        :data:`NODE_BACKEND`（Express/Nest/Next …）才视为真 Node 项目。仅含
        ``lodash``/``concurrently``/``husky`` 等辅助工具链的 package.json
        （如 pi-agent 这类 Python 项目的脚手架）返回 False，让识别落到 Python。
        """
        deps_lower = {d.lower() for d in summary.node_deps}
        return bool(deps_lower & NODE_FRONTEND or deps_lower & NODE_BACKEND)

    # ---- 重型数据库 --------------------------------------------------------

    def _detect_heavy_db(self, summary: FileSummary) -> set[str]:
        """真正需要外部服务的重型数据库依赖（命中即 pending）。

        BUG-868（#99）：redis/aioredis 这类可选内存缓存不再计入重型集合——
        它们单独出现时仅由 :meth:`_detect_optional_db_deps` 给 warning 提示，
        不再拦 pending（uptime-kuma 声明 redis 但 SQLite 单实例即可运行）。
        """
        found: set[str] = set()
        all_deps: set[str] = {d.lower() for d in summary.node_runtime_deps} | {
            d.lower() for d in summary.python_deps
        }
        for dep in all_deps:
            base = re.split(r"[<>=!\[ ]", dep, maxsplit=1)[0]
            if base in HEAVY_DATABASES:
                found.add(base)
        return found

    def _detect_optional_db_deps(self, summary: FileSummary) -> set[str]:
        """可选缓存/数据库依赖（BUG-868，#99）：单独出现不拦，仅 warning 提示。"""
        found: set[str] = set()
        all_deps: set[str] = {d.lower() for d in summary.node_runtime_deps} | {
            d.lower() for d in summary.python_deps
        }
        for dep in all_deps:
            base = re.split(r"[<>=!\[ ]", dep, maxsplit=1)[0]
            if base in OPTIONAL_CACHE_DATABASES:
                found.add(base)
        return found

    def _detect_heavy_deps(self, summary: FileSummary, result: DetectionResult) -> None:
        """IMP-018：命中重运行时依赖（lancedb/torch/openai …）自动升 medium。

        仅向上提升：已为 medium/heavy 的不降级（避免把 streamlit 这类本就 medium
        的项目误降回 small）。命中即记一条 note，便于 skill/管理页展示原因。
        """
        deps_lower = {d.lower().split("[")[0] for d in summary.python_deps}
        hit = deps_lower & HEAVY_RUNTIMES
        if not hit:
            return
        if result.resourceProfile in (ResourceProfile.TINY, ResourceProfile.SMALL):
            result.resourceProfile = ResourceProfile.MEDIUM
            result.notes.append(f"检测到重运行时依赖：{', '.join(sorted(hit))}，资源档位升 medium")

    # ---- SQLite ------------------------------------------------------------

    def _detect_sqlite(self, summary: FileSummary, result: DetectionResult) -> None:
        has_sqlite_dep = bool(SQLITE_MARKERS & {d.lower() for d in summary.python_deps}) or bool(
            SQLITE_MARKERS & {d.lower() for d in summary.node_deps}
        )
        orm_sqlite, orm_filename = _detect_declared_sqlite(summary.root)
        if summary.sqlite_files or has_sqlite_dep or orm_sqlite:
            result.hasDatabase = True
            # BUG-198：RUNTIME_ROOT 应用写 runtime/data，compose 据此挂载
            data_dir = "runtime/data" if summary.has_runtime_paths else "data"
            # IMP-058 Gate-A CHK-V03：记录源 SQLite 文件名，供 compose.generate_env
            # 注入保留原文件名的 DATABASE_URL（避免 BUG-474 硬编码 app.sqlite 把
            # 应用指向全新空库）。取第一个文件名；多个时用首个作为主库。
            db_filename = summary.sqlite_files[0] if summary.sqlite_files else orm_filename
            result.database = DatabaseConfig(
                type="sqlite",
                dataDir=data_dir,
                dbFilename=db_filename,
            )
            if summary.sqlite_files:
                result.notes.append(f"发现 SQLite 文件：{', '.join(summary.sqlite_files[:3])}")
            elif orm_sqlite:
                result.notes.append(
                    "源码声明 SQLite（Django ENGINE 或 Flask instance_path），"
                    "尚未生成库文件时仍记 hasDatabase"
                )
            if summary.has_runtime_paths:
                result.notes.append("检测到 runtime_paths，SQLite 持久化目录为 runtime/data")

    # ---- 静态 --------------------------------------------------------------

    def _detect_static(self, summary: FileSummary, result: DetectionResult) -> None:
        # 纯静态：有 index.html，没有后端工程文件
        backend_markers = (
            summary.has_package_json
            or summary.has_requirements_txt
            or summary.has_requirements_prod
            or summary.has_pyproject_toml
        )
        if backend_markers:
            return
        result.kind = Kind.STATIC
        result.runtime = Runtime.SHARED_STATIC
        result.servingMode = ServingMode.SHARED_STATIC
        result.form = "static"
        result.resourceProfile = ResourceProfile.TINY
        result.confidence = "high"

    # ---- Node --------------------------------------------------------------

    def _detect_node(self, summary: FileSummary, result: DetectionResult) -> None:
        deps_lower = {d.lower() for d in summary.node_deps}
        result.kind = Kind.NODE
        result.stack = [d for d in sorted(deps_lower) if d in (NODE_FRONTEND | NODE_BACKEND)]

        is_frontend = bool(deps_lower & NODE_FRONTEND)
        is_backend = bool(deps_lower & NODE_BACKEND)
        has_build = "build" in summary.node_scripts

        if is_backend:
            # Node 后端：进容器
            result.runtime = Runtime.DOCKER_COMPOSE
            result.servingMode = ServingMode.CONTAINER
            result.form = "fullstack-sqlite" if result.hasDatabase else "backend-container"
            result.resourceProfile = (
                ResourceProfile.MEDIUM
                if ("next" in deps_lower or "nuxt" in deps_lower)
                else ResourceProfile.SMALL
            )
            result.internalPort = _infer_node_port(summary)
            if result.internalPort is None:
                # BUG-509：优先脚本声明的端口非法（越出 1..65535）时，不得再按
                # high 置信度静默兜底 8000；置 pending 交人工确认（与 BUG-322 注释一致）。
                result.pending = True
                result.confidence = "low"
                result.notes.append(
                    "Node 后端声明的端口非法（越出 1..65535），标记 pending 待人工确认"
                )
            else:
                result.confidence = "high"
            result.entry = EntryConfig(
                install=_guard_install_scripts(_node_install_command(summary), summary.node_scripts),
                build="npm run build" if has_build else None,
                start="npm run start" if "start" in summary.node_scripts else "node server.js",
            )
        elif is_frontend and has_build:
            # 纯前端：构建后静态托管
            result.runtime = Runtime.SHARED_STATIC
            result.servingMode = ServingMode.SHARED_STATIC
            result.form = "frontend-static"
            result.resourceProfile = ResourceProfile.TINY
            result.entry = EntryConfig(
                install=_guard_install_scripts(_node_install_command(summary), summary.node_scripts),
                build="npm run build",
                start=None,
            )
            result.confidence = "high"
        else:
            # S2-10 / BUG-760：无框架依赖时识别 Node 内置 http 弱信号
            # （require('http') / import from 'node:http' + createServer + listen）。
            # BUG-541 同款原则：根目录静态证据（index.html/html）优先于弱信号，
            # 有静态证据时不抢判（维持原 pending 语义，交人工/skills 处理）。
            node_stdlib: tuple[str, int | None] | None = None
            if not is_frontend and not (summary.has_index_html or summary.has_html):
                node_stdlib = _node_stdlib_http_entry(summary)
            if node_stdlib is not None:
                entry_file, listen_port = node_stdlib
                result.runtime = Runtime.DOCKER_COMPOSE
                result.servingMode = ServingMode.CONTAINER
                result.form = "fullstack-sqlite" if result.hasDatabase else "backend-container"
                result.stack = ["node-stdlib-http"]
                result.resourceProfile = ResourceProfile.TINY
                result.internalPort = listen_port or 3000
                # BUG-788：源码仅读 process.env.PORT（无字面回退端口）时，
                # 端口完全由环境注入决定，兜底值 3000 不是可靠推断——标注
                # guessed 供 importer 在 update 场景保留 manifest 既有端口。
                result.internalPortGuessed = listen_port is None
                result.confidence = "medium"
                # 零依赖（仅空 dependencies）时 install 置 None，避免 npm install
                # 空跑；有依赖时仍按锁文件选择包管理器。
                install = _guard_install_scripts(
                    _node_install_command(summary) if summary.node_deps else None,
                    summary.node_scripts,
                )
                result.entry = EntryConfig(
                    install=install,
                    build="npm run build" if has_build else None,
                    start=f"node {entry_file}",
                )
                result.notes.append(
                    f"识别到零框架 Node 内置 http 服务（{entry_file} 使用 "
                    "node:http createServer），弱信号识别，监听端口请读 PORT "
                    f"环境变量（compose 已注入，默认 {result.internalPort}"
                    + (
                        "）；internalPort 为兜底推断，update 时请保留 manifest 既有端口"
                        if result.internalPortGuessed
                        else "）"
                    )
                )
            else:
                result.pending = True
                result.confidence = "low"
                result.notes.append("Node 项目缺少明确的 frontend/backend 特征，标记 pending")
        result.notes.extend(
            inspect_node_toolchain(
                summary.workspace_root or summary.root, workdir=summary.root
            ).notes
        )

    # ---- Python ------------------------------------------------------------

    def _detect_python(self, summary: FileSummary, result: DetectionResult) -> None:
        result.kind = Kind.PYTHON
        # issue #13：uv.lock 状态诊断进 notes，随导入摘要/manifest 透出，
        # 避免"为何没走/为何走了 uv sync"的排障成本。
        if summary.has_uv_lock:
            if summary.uv_lock_state is UvLockState.VALID_EMPTY:
                result.notes.append(
                    "检测到空壳 uv.lock（无 [[package]] 记录），已回退 pip 安装分支（issue #13）"
                )
            elif summary.uv_lock_state is UvLockState.INVALID:
                result.notes.append(
                    "uv.lock 无法解析（TOML 损坏），仍按 uv sync 安装；"
                    "若构建失败请优先核对 lock 文件（issue #13）"
                )
            elif summary.uv_lock_state is UvLockState.UNREADABLE:
                result.notes.append(
                    "uv.lock 读取失败，仍按 uv sync 安装；请确认文件权限（issue #13）"
                )
        matched: list[str] = []
        for dep in summary.python_deps:
            base = dep.lower().split("[")[0]
            if base in PYTHON_WEB:
                matched.append(PYTHON_WEB[base][0])
        # manage.py 是 Django 项目的强信号：依赖解析遗漏 django 时据此补识别，
        # 让 has_manage_py 这个已采集的信号真正参与识别
        if summary.has_manage_py and "django" not in matched:
            matched.append("django")
        result.stack = sorted(set(matched))
        result.internalPort = _infer_python_port(summary, matched)

        if matched:
            result.runtime = Runtime.DOCKER_COMPOSE
            result.servingMode = ServingMode.CONTAINER
            result.form = "fullstack-sqlite" if result.hasDatabase else "backend-container"
            heavy_frameworks = {"streamlit", "gradio"}
            result.resourceProfile = (
                ResourceProfile.MEDIUM if set(matched) & heavy_frameworks else ResourceProfile.SMALL
            )
            # IMP-018：重运行时依赖（lancedb/pyarrow/torch/openai …）自动升 medium。
            self._detect_heavy_deps(summary, result)
            start = _python_start_command(matched, summary, notes=result.notes)
            result.entry = EntryConfig(
                install=_python_install_command(summary),
                build=None,
                start=start,
            )
            if start is None:
                # 已识别框架但无启动模板：不得标高置信度，否则容器 CMD 为空必失败
                result.pending = True
                result.confidence = "medium"
                result.notes.append(
                    f"已识别框架 {sorted(set(matched))} 但缺少启动命令模板，标记 pending"
                )
            else:
                result.confidence = "high"
                if summary.has_alembic_ini and start and "alembic upgrade head" in start:
                    result.notes.append(
                        "检测到 alembic.ini，已在启动命令前自动执行 alembic upgrade head"
                    )
        else:
            # issue#1 问题1：无框架特征时先做 stdlib HTTP 弱信号识别
            # （http.server / socketserver 的零依赖服务），命中则不再 pending。
            stdlib_entry = _stdlib_http_entry(summary)
            if stdlib_entry is not None:
                result.runtime = Runtime.DOCKER_COMPOSE
                result.servingMode = ServingMode.CONTAINER
                result.form = "fullstack-sqlite" if result.hasDatabase else "backend-container"
                result.stack = ["stdlib-http"]
                result.resourceProfile = ResourceProfile.TINY
                result.confidence = "medium"
                # 零依赖：无 requirements/pyproject/pipfile 时 install 置 None，
                # 避免容器构建执行 pip install -r requirements.txt 落空失败。
                # issue #13：空壳 uv.lock 不算依赖文件，正确落 install=None
                # （CHK-225 零依赖路径），而不是 COPY 一个装不出任何包的 lock。
                has_dep_file = (
                    (summary.has_uv_lock and _uv_lock_usable(summary))
                    or summary.has_requirements_prod
                    or summary.has_requirements_txt
                    or summary.has_pyproject_toml
                    or summary.has_pipfile
                )
                result.entry = EntryConfig(
                    install=_python_install_command(summary) if has_dep_file else None,
                    build=None,
                    start=f"python {stdlib_entry}",
                )
                result.notes.append(
                    f"识别到零依赖 stdlib HTTP 服务（{stdlib_entry} 使用 http.server），"
                    "弱信号识别，监听端口请读 PORT 环境变量（compose 已注入，默认 8000）"
                )
            else:
                result.pending = True
                result.confidence = "low"
                result.notes.append("Python 项目缺少 Web 框架特征，标记 pending")


# ---- 辅助：端口与命令推断 ---------------------------------------------------


def _has_lockfile(summary: FileSummary) -> bool:
    return any(
        f in summary.top_files
        for f in (
            "package-lock.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            "bun.lock",
            "bun.lockb",
        )
    )


def _node_install_command(summary: FileSummary) -> str:
    """根据锁文件选择包管理器，避免 pnpm/yarn 项目误走 npm ci。

    summarize_package 的 ``root`` 是子包目录，锁文件在仓库根（workspace_root）。
    """
    repo = summary.workspace_root or summary.root
    return install_command_for(repo, workdir=summary.root)


# BUG-788（S2-10 实案 patent-disclosure-workflow-v1）：workspaces 根的
# install 生命周期脚本（如 ensure-electron-mirror.mjs）常依赖宿主机环境，
# 容器内 ``npm ci`` 执行必败（exit 1）；检测到这类脚本时给安装命令补
# ``--ignore-scripts``（构建与启动不依赖它们，scripts 由 entry.build/start 执行）。
#
# BUG-793 收窄：postinstall/preinstall 做**原生构建**（node-gyp、prebuild、
# electron-rebuild 等）时不得补——忽略脚本会让原生模块装不上（未编译即用），
# 宁可让安装在容器内如实失败，由用户手改 entry.install（BUG-787 保留语义，
# update 不覆盖）。其余生命周期脚本（镜像配置、husky 等忽略无害类）维持
# BUG-788 的补参保护。
_INSTALL_LIFECYCLE_SCRIPT_KEYS = frozenset({"preinstall", "install", "postinstall", "prepare"})
_NATIVE_BUILD_SCRIPT_RE = re.compile(
    r"node-gyp|node-pre-gyp|prebuild|electron-rebuild|install-app-deps|cmake", re.IGNORECASE
)


def _has_install_lifecycle_scripts(scripts: dict) -> bool:
    """package.json scripts 是否声明 install 生命周期脚本。"""
    return any(scripts.get(key) for key in _INSTALL_LIFECYCLE_SCRIPT_KEYS)


def _lifecycle_scripts_native_build(scripts: dict) -> bool:
    """install 生命周期脚本是否包含原生构建命令（忽略即装不起来）。"""
    return any(
        _NATIVE_BUILD_SCRIPT_RE.search(str(scripts.get(key) or ""))
        for key in _INSTALL_LIFECYCLE_SCRIPT_KEYS
    )


def _guard_install_scripts(command: str | None, scripts: dict) -> str | None:
    """有忽略无害的 install 生命周期脚本时给安装命令补 ``--ignore-scripts``。

    只对安装命令尾部追加（``corepack enable && pnpm install`` 形式也只影响
    最后的安装子命令）；已是 ``None`` 或已带该参数时原样返回；脚本含原生
    构建时不补（BUG-793）。
    """
    if not command or "--ignore-scripts" in command:
        return command
    if not _has_install_lifecycle_scripts(scripts):
        return command
    if _lifecycle_scripts_native_build(scripts):
        return command
    return f"{command} --ignore-scripts"


def _infer_node_port(summary: FileSummary) -> int | None:
    """推断 Node 后端容器内监听端口。

    优先级：
    1. ``package.json`` 优先脚本（start → dev）中显式端口；
    2. 非法端口（越出 1..65535）→ ``None``（留给 pending/人工确认，BUG-322）；
    3. 无显式端口 → 默认 ``3000``。

    不扫描全部 scripts，避免 express+vite 单仓把 Vite 的 ``--port`` 误当作
    后端 ``internalPort``（BUG-322）。
    """
    extracted = _extract_port_from_scripts(summary.node_scripts)
    if extracted is _INVALID_PORT:
        return None
    if isinstance(extracted, int):
        return extracted
    return 3000


# 哨兵：优先脚本显式声明了端口，但数值非法。
_INVALID_PORT = object()


def _extract_port_from_scripts(scripts: dict) -> int | None | object:
    """从 package.json 优先 scripts 解析端口。

    仅看 ``start`` / ``dev``（有 start 时不回落到 dev，避免 Vite 端口覆盖）。
    匹配 ``PORT=8080`` 与 ``--port 8080`` / ``--port=8080``；短旗 ``-p`` 不采纳。
    返回合法端口、``None``（未声明）或 ``_INVALID_PORT``（声明但非法）。
    """
    if not scripts:
        return None
    # BUG-322：优先 start；仅当无 start 时才读 dev。
    preferred_names = ("start",) if "start" in scripts else (("dev",) if "dev" in scripts else ())
    if not preferred_names:
        return None
    blob = " ".join(str(scripts[name]) for name in preferred_names)
    m = re.search(r"\bPORT=(\d+)\b", blob)
    if not m:
        m = re.search(r"--port[=\s]+(\d+)", blob)
    if not m:
        return None
    port = int(m.group(1))
    if 1 <= port <= 65535:
        return port
    return _INVALID_PORT


def _select_python_framework(matched: list[str]) -> str | None:
    """按固定优先级挑出主导框架（消除 set 迭代顺序导致的端口/命令不一致，BUG-181）。"""
    lower = {m.lower() for m in matched}
    for fw in _PYTHON_FRAMEWORK_PRIORITY:
        if fw in lower:
            return fw
    return matched[0].lower() if matched else None


def _infer_python_port(summary: FileSummary | None, matched: list[str]) -> int:
    fw = _select_python_framework(matched)
    if fw and fw in PYTHON_WEB:
        # BUG-867（#98）：flask 工厂包形态按 8000 约定，与 _python_start_command
        # 的 gunicorn 绑定端口保持同一口径（BUG-181：端口与命令必须一致）。
        if fw == "flask" and summary is not None:
            target, _reason = _infer_flask_wsgi_target(summary)
            if target:
                return _flask_target_port(target)
        return PYTHON_WEB[fw][1]
    return 8000


# S2-10 / BUG-760：Node 内置 http 弱信号的入口候选文件（含 ts/mjs 变体）。
_NODE_HTTP_ENTRY_FILES = (
    "server.js",
    "index.js",
    "app.js",
    "main.js",
    "server.mjs",
    "index.mjs",
    "app.mjs",
    "main.mjs",
    "server.ts",
    "index.ts",
    "app.ts",
    "main.ts",
)
# require('http') / require("node:http")
_NODE_HTTP_REQUIRE_RE = re.compile(r"""\brequire\(\s*['"](?:node:)?http['"]\s*\)""")
# import ... from 'http' / 'node:http'（含具名、命名空间与默认导入）
_NODE_HTTP_IMPORT_RE = re.compile(
    r"""\bfrom\s*['"](?:node:)?http['"]"""
    r"""|\bimport\s*\(\s*['"](?:node:)?http['"]\s*\)"""
    r"""|\bimport\s*['"](?:node:)?http['"]"""
)
# listen(...) 实参中的第一个数字字面量（覆盖 listen(3000) 与
# listen(process.env.PORT || 3210) 两种常见写法）
_LISTEN_ARGS_PORT_RE = re.compile(r"\blisten\s*\(([^)]*)\)")
# const PORT = process.env.PORT || 3210（配合 listen(PORT) 使用）
_ENV_PORT_FALLBACK_RE = re.compile(r"process\.env\.PORT\s*(?:\|\||\?\?)\s*(\d+)")


def _imports_node_http(text: str) -> bool:
    """源码是否导入 Node 内置 http 模块（CommonJS 或 ESM）。

    不做字符串包含判断：``some-lib`` 等第三方包名中的 "http" 子串、
    ``require('http-server')`` 之类的近似包名均不命中（对齐 BUG-534 的
    AST 精确判定思路，Node 侧用正则锚定引号边界）。
    """
    return bool(_NODE_HTTP_REQUIRE_RE.search(text) or _NODE_HTTP_IMPORT_RE.search(text))


def _infer_node_listen_port(text: str) -> int | None:
    """从 createServer().listen(...) 或 PORT 环境变量回退推断监听端口。

    返回 ``1..65535`` 的端口；无法确定或非法时返回 ``None``（调用方兜底）。
    """
    m = _LISTEN_ARGS_PORT_RE.search(text)
    if m:
        num = re.search(r"\d+", m.group(1))
        if num:
            port = int(num.group(0))
            if 1 <= port <= 65535:
                return port
    m = _ENV_PORT_FALLBACK_RE.search(text)
    if m:
        port = int(m.group(1))
        if 1 <= port <= 65535:
            return port
    return None


def _node_stdlib_http_entry(summary: FileSummary) -> tuple[str, int | None] | None:
    """弱信号识别只用 Node 内置 http 的服务（S2-10 / BUG-760，对标 Python
    侧 issue#1 / BUG-541 的 ``_stdlib_http_entry``）。

    无框架依赖时，若包目录内存在 ``server.js`` / ``index.ts`` 等候选入口，
    且源码同时导入 ``node:http``（或 ``http``）并调用 ``createServer(...)``
    与 ``.listen(...)``，按 node-stdlib-http 处理。返回 ``(入口文件, 端口)``；
    端口无法从源码确定时为 ``None``（调用方兜底默认值）。未命中返回 ``None``。
    """
    for name in _NODE_HTTP_ENTRY_FILES:
        # Node/TS 项目惯例把入口放在 src/ 下，根目录与 src/ 同级探测
        for path in (summary.root / name, summary.root / "src" / name):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not _imports_node_http(text):
                continue
            if "createServer" not in text or ".listen" not in text:
                continue
            return str(path.relative_to(summary.root)), _infer_node_listen_port(text)
    return None


# issue#1：零依赖 stdlib HTTP 服务的入口候选文件，按此优先级探测。
_STDLIB_HTTP_ENTRY_FILES = ("server.py", "app.py", "main.py")


def _imports_stdlib_http(tree: ast.AST) -> bool:
    """AST 精确判定是否 import 了 ``http.server`` / ``socketserver``。

    BUG-534：只匹配模块名精确等值，``from mypkg import socketserver``
    （module=mypkg）与 ``import mysocketserver`` 都不算命中。
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name in ("http.server", "socketserver") for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module in ("http.server", "socketserver"):
                return True
    return False


def _stdlib_http_entry(summary: FileSummary) -> str | None:
    """弱信号识别零依赖 stdlib HTTP 服务（issue#1 问题 1）。

    无第三方 Web 框架时，若顶层存在 ``server.py`` / ``app.py`` / ``main.py``
    且源码 import 了 ``http.server`` 或 ``socketserver``，按 stdlib-http
    模板处理（``start=python <file>``，端口默认 8000，应用应读 ``PORT``
    环境变量对齐）。返回入口文件名；未命中返回 ``None``。

    用 :mod:`ast` 解析 import（BUG-534），不做字符串包含判断；解析失败
    （语法错误/非源码）视为未命中，保持保守。
    """
    for name in _STDLIB_HTTP_ENTRY_FILES:
        path = summary.root / name
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(content)
        except (OSError, ValueError, SyntaxError):
            continue
        if _imports_stdlib_http(tree):
            return name
    return None


def _uv_lock_state(path: Path) -> UvLockState:
    """判定 uv.lock 内容状态（issue #13）。

    只用 tomllib 结构化解析，不做字符串计数兜底——TOML 损坏时若靠
    ``[[package]]`` 标记字符串命中继续走 uv，会把"损坏 lock"与"空壳 lock"
    两种不同故障混为一谈；损坏必须显式暴露为 INVALID，由调用方保持 uv
    路径并给出诊断，而不是静默降级或静默放行。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return UvLockState.UNREADABLE
    try:
        packages = tomllib.loads(text).get("package")
    except tomllib.TOMLDecodeError:
        return UvLockState.INVALID
    if isinstance(packages, list) and packages:
        return UvLockState.VALID_NONEMPTY
    return UvLockState.VALID_EMPTY


def _uv_lock_usable(summary: FileSummary) -> bool:
    """uv.lock 是否作为安装源保留：仅空壳（VALID_EMPTY）回退其他依赖文件。

    INVALID / UNREADABLE 不静默换源（保持 uv 路径 + notes 诊断），
    避免"读不了就偷偷改用 pip"造成难以归因的版本漂移。
    """
    return summary.uv_lock_state is not UvLockState.VALID_EMPTY


def _python_install_command(summary: FileSummary) -> str:
    if summary.has_uv_lock and _uv_lock_usable(summary):
        return "uv sync"
    # IMP-017：优先 requirements-prod.txt（已剔除测试包的生产依赖清单）。
    if summary.has_requirements_prod:
        return "pip install -r requirements-prod.txt"
    if summary.has_requirements_txt:
        return "pip install -r requirements.txt"
    if summary.has_pyproject_toml:
        return "pip install ."
    if summary.has_pipfile:
        return "pip install pipenv && pipenv install --system --skip-lock"
    return "pip install -r requirements.txt"


def _python_start_command(
    matched: list[str],
    summary: FileSummary | None,
    notes: list[str] | None = None,
) -> str | None:
    fw = _select_python_framework(matched)
    if fw in ("fastapi", "uvicorn", "starlette"):
        # BUG-455 / IMP-052：按磁盘布局推断 ASGI 模块，勿硬编码 main:app。
        target = _infer_uvicorn_app_target(summary)
        cmd = f"uvicorn {target} --host 0.0.0.0 --port 8000"
        if summary is not None and summary.has_alembic_ini:
            # 容器 CMD 用 sh -c，保证迁移失败时不启动 uvicorn。
            cmd = f'sh -c "alembic upgrade head && exec {cmd}"'
        return cmd
    if fw == "flask":
        target, reason = _infer_flask_wsgi_target(summary)
        if target:
            _note_gunicorn_generated(notes)
            # BUG-867（#98）：工厂包形态（flaskr:create_app()）按内部端口约定
            # 8000 绑定；模块文件形态（app:app）沿用 flask 惯例 5000。
            cmd = _gunicorn_start_command(target, port=_flask_target_port(target))
            spec = (
                _flask_init_db_spec(summary.root, target, notes)
                if summary is not None and "create_app()" in target
                else None
            )
            if spec is not None:
                init_cmd, db_path, config_paths = spec
                if notes is not None:
                    notes.append(
                        "Flask 工厂含唯一 init-db；仅当 instance 库文件不存在时执行，"
                        "避免教程命令清空已有数据"
                    )
                # BUG-931：db_path 是源码证明的容器内库绝对路径（含子目录与
                # 自定义 instance_path），不再硬编码 /app/instance/<basename>。
                checks = [f"[ ! -f {db_path} ]"]
                # 可选配置文件一旦存在，默认 DATABASE 就不再是已证明的最终值。
                checks.extend(f"[ ! -e {path} ]" for path in config_paths)
                cmd = f"sh -c 'if {' && '.join(checks)}; then {init_cmd}; fi && exec {cmd}'"
            return cmd
        if "gunicorn" in matched:
            # 066.a：已声明生产服务器优先——入口不可证明时沿用既有 app:app 形态。
            return "gunicorn -b 0.0.0.0:5000 app:app"
        _note_wsgi_fallback(notes, "Flask", reason)
        return "flask --app app run --host 0.0.0.0 --port 5000"
    if fw == "django":
        target, reason = _infer_django_wsgi_target(summary)
        if target:
            _note_gunicorn_generated(notes)
            cmd = _gunicorn_start_command(target, port=8000)
            # 与 Alembic 相同：只包平台推断出的生产入口，且项目里确实有
            # migrations 包。没有 migrations 时不猜测，也不改用户手写的 start。
            if (
                summary is not None
                and (summary.root / "manage.py").is_file()
                and _django_has_migrations(summary.root)
            ):
                if notes is not None:
                    notes.append(
                        "Django 项目含 migrations，启动前执行 "
                        "python manage.py migrate --noinput；迁移失败则不启动 Gunicorn"
                    )
                cmd = f'sh -c "python manage.py migrate --noinput && exec {cmd}"'
            return cmd
        if "gunicorn" in matched:
            return "gunicorn -b 0.0.0.0:8000 app:app"
        _note_wsgi_fallback(notes, "Django", reason)
        return "python manage.py runserver 0.0.0.0:8000"
    if fw == "streamlit":
        return "streamlit run app.py --server.port 8501"
    if fw == "gradio":
        return "python app.py"
    if fw == "gunicorn":
        return "gunicorn -b 0.0.0.0:8000 app:app"
    if fw == "sanic":
        return "sanic app.app --host=0.0.0.0 --port=8000"
    if fw == "tornado":
        return "python app.py"
    return None


# ---- IMP-066：Flask/Django WSGI 入口推断与生产服务器模板（066.b~066.f） ------

# 066.b：平台选定的 Gunicorn 版本范围（分层安装，不回写用户依赖文件）。
GUNICORN_PLATFORM_SPEC = ">=21,<23"
# BUG-921：平台在启动命令里内嵌的迁移前置标记。副作用识别（C.R05）、
# requiresMigrations 契约与迁移成功判定共用这一份，避免口径漂移。
MIGRATION_START_MARKERS = ("alembic upgrade", "manage.py migrate", "init-db")


def start_command_wraps_migration(start: str | None) -> bool:
    """启动命令是否内嵌迁移前置（Alembic ``alembic upgrade`` / Django
    ``manage.py migrate`` 的 ``&& exec`` 守卫包装）。"""
    lowered = (start or "").lower()
    return any(marker in lowered for marker in MIGRATION_START_MARKERS)

_FLASK_APP_ASSIGN_RE = re.compile(r"^\s*app\s*=\s*Flask\(", re.MULTILINE)
_CREATE_APP_RE = re.compile(r"^\s*(async\s+)?def\s+create_app\s*\(", re.MULTILINE)
_DJANGO_WSGI_APP_RE = re.compile(r"^\s*application\s*=", re.MULTILINE)
_WSGI_SKIP_DIRS = frozenset(
    {"venv", ".venv", "env", "node_modules", "site-packages", "dist", "build"}
)
_DEV_SERVER_START_RE = re.compile(
    r"(^|[\s/])("
    r"flask\s+--app|flask\s+run|python\s+-m\s+flask"
    r"|manage\.py\s+runserver"
    r")"
)


def _read_text_if_exists(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _find_flask_factory_candidates(root: Path) -> list[str]:
    """BUG-867（#98）：在根级 Python 包目录的 ``__init__.py`` 中探测 Flask 工厂。

    覆盖 flask 教程式布局（pyproject 项目、根无 app.py、``flaskr/__init__.py``
    内 ``def create_app``）。跳过 venv/site-packages/dist 等目录、隐藏目录与
    tests 目录（测试目录里的工厂不是生产入口）。返回 ``<pkg>:create_app()``
    候选列表。
    """
    candidates: list[str] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return candidates
    for entry in entries:
        if not entry.is_dir():
            continue
        name = entry.name
        if name.startswith(".") or name in _WSGI_SKIP_DIRS or name in ("test", "tests"):
            continue
        if not (entry / "__init__.py").is_file():
            continue
        text = _read_text_if_exists(entry / "__init__.py")
        if text and _CREATE_APP_RE.search(text):
            candidates.append(f"{name}:create_app()")
    return candidates


def _infer_flask_wsgi_target(summary: FileSummary | None) -> tuple[str, str | None]:
    """066.c：唯一可证明的 Flask WSGI 目标 ``module:app`` / 工厂 ``module:create_app()``。

    只接受根 ``app.py`` / ``main.py`` / ``wsgi.py`` / ``app/__init__.py`` 中可证明
    的入口（``app = Flask(...)`` 或 ``def create_app``）；多候选不猜。
    根文件无候选时，回退到根级包目录的工厂探测（BUG-867，#98：flask 教程式
    ``flaskr/`` 工厂包布局，根无 app.py 时不再硬编码不存在的 ``app`` 模块）。
    返回 ``(target, fallback_reason)``，二者互斥。
    """
    if summary is None:
        return "", "扫描摘要缺失，无法推断 Flask WSGI 入口"
    root = summary.root
    candidates: list[str] = []
    for fname, module in (("app.py", "app"), ("main.py", "main"), ("wsgi.py", "wsgi")):
        text = _read_text_if_exists(root / fname)
        if not text:
            continue
        if _FLASK_APP_ASSIGN_RE.search(text):
            candidates.append(f"{module}:app")
        elif _CREATE_APP_RE.search(text):
            candidates.append(f"{module}:create_app()")
    pkg_text = _read_text_if_exists(root / "app" / "__init__.py")
    if pkg_text:
        if _FLASK_APP_ASSIGN_RE.search(pkg_text):
            candidates.append("app:app")
        elif _CREATE_APP_RE.search(pkg_text):
            candidates.append("app:create_app()")
    if len(candidates) == 1:
        return candidates[0], None
    if len(candidates) > 1:
        return "", f"Flask WSGI 入口存在多个候选（{'、'.join(candidates)}），不猜测"
    # BUG-867（#98）：根级包目录工厂（如 flaskr/__init__.py 的 create_app）
    factories = _find_flask_factory_candidates(root)
    if len(factories) == 1:
        return factories[0], None
    if len(factories) > 1:
        return "", f"Flask 工厂入口存在多个候选（{'、'.join(factories)}），不猜测"
    return "", "未能从源码证明唯一的 Flask WSGI 入口"


def _flask_target_port(target: str) -> int:
    """Flask 启动命令端口：工厂包形态按内部端口约定 8000（BUG-867，#98），
    模块文件形态沿用 flask 惯例 5000。与 _infer_python_port 保持同一口径。"""
    return 8000 if "create_app()" in target else 5000


def _infer_django_wsgi_target(summary: FileSummary | None) -> tuple[str, str | None]:
    """066.d：``manage.py`` 根 + 唯一 ``*/wsgi.py`` → ``<project>.wsgi:application``。

    多个 wsgi.py 或缺失时返回 fallback 原因（不猜）。
    """
    if summary is None or not (summary.root / "manage.py").is_file():
        return "", "未找到 manage.py，无法推断 Django WSGI 入口"
    modules: list[str] = []
    for path in _walk(summary.root, max_depth=2):
        if not path.is_file() or path.name != "wsgi.py":
            continue
        rel = path.relative_to(summary.root)
        if any(part in _WSGI_SKIP_DIRS for part in rel.parts[:-1]):
            continue
        text = _read_text_if_exists(path)
        if text and _DJANGO_WSGI_APP_RE.search(text):
            modules.append(".".join(rel.with_suffix("").parts))
    if len(modules) == 1:
        return f"{modules[0]}:application", None
    if not modules:
        return "", "未找到含 application 的 wsgi.py"
    return "", f"Django wsgi.py 存在多个候选（{'、'.join(modules)}），不猜测"


# 预检用单引号包裹整条 ``sh -c``。目标若含 ``()`` 等元字符，必须用双引号，
# 否则内层 sh 会把 ``create_app()`` 当成语法（BUG-904）。
_GUNICORN_TARGET_META_RE = re.compile(r"""[()$`"\\;&|<> \t']""")


def _quote_gunicorn_target(target: str) -> str:
    if not _GUNICORN_TARGET_META_RE.search(target):
        return target
    escaped = (
        target.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    )
    return f'"{escaped}"'


def _gunicorn_start_command(target: str, *, port: int) -> str:
    # 066.e 低配参数：单 worker、小线程数（家庭服务器预算内），不按 CPU 放大。
    quoted = _quote_gunicorn_target(target)
    return f"gunicorn --workers 1 --threads 4 -b 0.0.0.0:{port} {quoted}"


def _django_has_migrations(root: Path) -> bool:
    """存在应用级 ``migrations/__init__.py`` 才视为可自动迁移。"""
    for path in _walk(root, max_depth=4):
        if path.name != "__init__.py" or path.parent.name != "migrations":
            continue
        parts = path.relative_to(root).parts
        if any(part in _WSGI_SKIP_DIRS or part in ("test", "tests") for part in parts):
            continue
        return True
    return False


_CLICK_INIT_DB_RE = re.compile(r"""@click\.command\(\s*['"]init-db['"]\s*\)""")
_DJANGO_SQLITE_ENGINE_RE = re.compile(r"django\.db\.backends\.sqlite3")
_FLASK_INSTANCE_SQLITE_RE = re.compile(
    r"instance_path.*?['\"]([^'\"]+\.(?:sqlite3?|db))['\"]",
    re.DOTALL | re.IGNORECASE,
)
_SQLITE_NAME_RE = re.compile(r"['\"]([^'\"]+\.(?:sqlite3?|db))['\"]", re.IGNORECASE)


def _detect_declared_sqlite(root: Path) -> tuple[bool, str | None]:
    """CHK-421：Django ENGINE / Flask instance_path 声明 SQLite 即记 hasDatabase。"""
    filename: str | None = None
    found = False
    for path in _walk(root, max_depth=4):
        if not path.is_file() or path.suffix != ".py":
            continue
        parts = path.relative_to(root).parts
        if any(part in _WSGI_SKIP_DIRS or part in ("test", "tests") for part in parts):
            continue
        text = _read_text_if_exists(path)
        if not text:
            continue
        if _DJANGO_SQLITE_ENGINE_RE.search(text):
            found = True
            names = _SQLITE_NAME_RE.findall(text)
            if names:
                filename = Path(names[-1]).name
            break
        if _FLASK_INSTANCE_SQLITE_RE.search(text):
            found = True
            filename = _flask_instance_sqlite_relpath(text)
            break
    return found, filename


def _safe_instance_sqlite_relpath(raw: str) -> str | None:
    """Flask instance 下的相对库路径；含 ``..`` / 绝对路径则视为不确定。"""
    # 容器使用 POSIX 路径；反斜杠是文件名字符，不能擅自改成目录分隔符。
    text = raw or ""
    if not text or text.startswith("/") or re.match(r"^[A-Za-z]:/", text):
        return None
    while text.startswith("./"):
        text = text[2:]
    parts = [p for p in text.split("/") if p and p != "."]
    if not parts or any(p == ".." for p in parts):
        return None
    if not all(re.fullmatch(r"[A-Za-z0-9._-]+", p) for p in parts):
        return None
    if not re.search(r"\.(?:sqlite3?|db)$", parts[-1], re.IGNORECASE):
        return None
    return "/".join(parts)


class _FactoryDefaultBranches(ast.NodeTransformer):
    """只剪除工厂默认参数能证明不会执行的 None 分支；不执行用户代码。"""

    def __init__(self) -> None:
        self.defaults: dict[str, ast.expr] = {}

    def visit_FunctionDef(self, node: ast.FunctionDef):
        previous = self.defaults
        self.defaults = {}
        if node.name == "create_app":
            args = node.args.posonlyargs + node.args.args
            self.defaults = dict(zip(
                (arg.arg for arg in args[len(args) - len(node.args.defaults):]),
                node.args.defaults,
            ))
            # 参数一旦被重赋值，就不能再用签名默认值推断控制流。
            for item in ast.walk(node):
                if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store):
                    self.defaults.pop(item.id, None)
        result = self.generic_visit(node)
        self.defaults = previous
        return result

    def visit_If(self, node: ast.If):
        test = node.test
        if (
            isinstance(test, ast.Compare) and len(test.ops) == 1
            and isinstance(test.left, ast.Name) and test.left.id in self.defaults
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value is None
            and isinstance(test.ops[0], (ast.Is, ast.IsNot))
            and isinstance(self.defaults[test.left.id], ast.Constant)
        ):
            is_none = self.defaults[test.left.id].value is None
            take_body = is_none if isinstance(test.ops[0], ast.Is) else not is_none
            chosen = ast.Module(body=node.body if take_body else node.orelse, type_ignores=[])
            return self.generic_visit(chosen).body
        return self.generic_visit(node)


def _flask_source_tree(text: str) -> ast.Module | None:
    try:
        return _FactoryDefaultBranches().visit(ast.parse(text))
    except (SyntaxError, ValueError):
        return None


def _flask_app_constructor(tree: ast.Module) -> tuple[str, ast.Call] | None:
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and (
        isinstance(node.func, ast.Name) and node.func.id == "Flask"
        or isinstance(node.func, ast.Attribute) and node.func.attr == "Flask"
    )]
    if len(calls) != 1:
        return None
    call = calls[0]
    assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                   and node.value is call and len(node.targets) == 1
                   and isinstance(node.targets[0], ast.Name)]
    if len(assignments) != 1:
        return None
    target = assignments[0].targets[0]
    if any(isinstance(node, ast.Name) and node.id == target.id
           and isinstance(node.ctx, ast.Store) and node is not target for node in ast.walk(tree)):
        return None
    return target.id, call


def _app_attribute(node: ast.AST, owner: str, attribute: str) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == attribute
            and isinstance(node.value, ast.Name) and node.value.id == owner)


def _string_literal(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _instance_sqlite_expression(node: ast.expr, owner: str) -> str | None:
    """仅接受完整的 join、字符串加法、Path 除法或 f-string，不截取内部调用。"""
    parts: list[str | None]
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Attribute)
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "os" and node.func.value.attr == "path"
            and node.func.attr == "join" and node.args and not node.keywords
            and _app_attribute(node.args[0], owner, "instance_path")):
        parts = [_string_literal(arg) for arg in node.args[1:]]
        if not parts or any(part is None or part.startswith(("/", "\\")) for part in parts):
            return None
        raw = "/".join(parts)
    elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Div)):
        operator = type(node.op)
        leaves: list[ast.expr] = []

        def flatten(expr: ast.expr) -> None:
            if isinstance(expr, ast.BinOp) and isinstance(expr.op, operator):
                flatten(expr.left)
                flatten(expr.right)
            else:
                leaves.append(expr)

        flatten(node)
        first = leaves[0]
        parts = [_string_literal(piece) for piece in leaves[1:]]
        if any(part is None for part in parts):
            return None
        if operator is ast.Add and _app_attribute(first, owner, "instance_path"):
            joined = "".join(parts)
            if not joined.startswith("/"):
                return None
            raw = joined[1:]
        elif (operator is ast.Div and isinstance(first, ast.Call)
              and isinstance(first.func, ast.Name) and first.func.id == "Path"
              and len(first.args) == 1 and not first.keywords
              and _app_attribute(first.args[0], owner, "instance_path")):
            if any(part.startswith(("/", "\\")) for part in parts):
                return None
            raw = "/".join(parts)
        else:
            return None
    elif isinstance(node, ast.JoinedStr) and node.values:
        first, *rest = node.values
        if (not isinstance(first, ast.FormattedValue) or first.conversion != -1
                or first.format_spec is not None
                or not _app_attribute(first.value, owner, "instance_path")):
            return None
        parts = [_string_literal(piece) for piece in rest]
        if any(part is None for part in parts):
            return None
        joined = "".join(parts)
        if not joined.startswith("/"):
            return None
        raw = joined[1:]
    else:
        return None
    return _safe_instance_sqlite_relpath(raw)


def _flask_instance_sqlite_relpath(text: str) -> str | None:
    """从 DATABASE 的完整赋值和更新证明路径；动态覆盖或歧义一律返回 None。"""
    tree = _flask_source_tree(text)
    app = _flask_app_constructor(tree) if tree is not None else None
    if app is None:
        return None
    owner, _ctor = app
    values: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (isinstance(target, ast.Subscript)
                        and _app_attribute(target.value, owner, "config")
                        and _string_literal(target.slice) == "DATABASE"):
                    if isinstance(node, ast.AugAssign) or node.value is None:
                        return None
                    values.append(node.value)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and _app_attribute(node.func.value, owner, "config")
                and node.func.attr in {"from_mapping", "update"}):
            if node.args or any(kw.arg is None for kw in node.keywords):
                return None
            values.extend(kw.value for kw in node.keywords if kw.arg == "DATABASE")
    if not values:
        return None
    # 其他 instance SQLite 表达式也不得暗中指向第二个库。沿 AST 到完整表达式，
    # 而不是像旧正则那样停在 join(...) 或 f-string 的结尾。
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    for node in ast.walk(tree):
        if not _app_attribute(node, owner, "instance_path"):
            continue
        expr = node
        while isinstance(parents.get(expr), ast.expr):
            expr = parents[expr]
        if any(isinstance(item, ast.Constant) and isinstance(item.value, str)
               and re.search(r"\.(?:sqlite3?|db)\b", item.value, re.IGNORECASE)
               for item in ast.walk(expr)):
            values.append(expr)
    paths = {_instance_sqlite_expression(value, owner) for value in values}
    return paths.pop() if len(paths) == 1 and None not in paths else None


def _flask_instance_base(text: str, *, default_base: str = "/app/instance") -> str | None:
    """只在找到唯一构造且没有参数展开/后续路径修改时证明 instance 目录。"""
    tree = _flask_source_tree(text)
    app = _flask_app_constructor(tree) if tree is not None else None
    if app is None:
        return None
    owner, ctor = app
    if len(ctor.args) != 1 or isinstance(ctor.args[0], ast.Starred):
        return None
    if any(kw.arg is None for kw in ctor.keywords):
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            if _app_attribute(node, owner, "instance_path"):
                return None
    values = [kw.value for kw in ctor.keywords if kw.arg == "instance_path"]
    if not values:
        return default_base
    literal = _string_literal(values[0])
    if (literal is None or not re.fullmatch(r"/[A-Za-z0-9._/-]*", literal)
            or ".." in literal.split("/")):
        return None
    return "/" + literal.strip("/")


def _flask_configuration_guards(text: str, base: str) -> tuple[str, ...] | None:
    """可选 instance 配置仅在文件不存在时安全；其他动态配置加载不自动初始化。"""
    tree = _flask_source_tree(text)
    app = _flask_app_constructor(tree) if tree is not None else None
    if app is None:
        return None
    owner, ctor = app
    instance_relative = any(kw.arg == "instance_relative_config"
                            and isinstance(kw.value, ast.Constant) and kw.value.value is True
                            for kw in ctor.keywords)
    guards: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            if _app_attribute(node, owner, "config"):
                return None
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            if _app_attribute(node.value, owner, "config") and _string_literal(node.slice) is None:
                return None
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and _app_attribute(node.func.value, owner, "config")):
            continue
        if node.func.attr in {"from_mapping", "update", "get", "items", "keys", "values"}:
            continue
        if node.func.attr != "from_pyfile" or not instance_relative or len(node.args) != 1:
            return None
        filename = _string_literal(node.args[0])
        if (filename is None or not re.fullmatch(r"[A-Za-z0-9._/-]+", filename)
                or filename.startswith("/") or ".." in filename.split("/")):
            return None
        if not (len(node.keywords) == 1 and node.keywords[0].arg == "silent"
                and isinstance(node.keywords[0].value, ast.Constant)
                and node.keywords[0].value.value is True):
            return None
        guards.add(f"{base}/{filename}")
    return tuple(sorted(guards))


def _flask_init_db_spec(
    root: Path, target: str, notes: list[str] | None = None
) -> tuple[str, str, tuple[str, ...]] | None:
    """CHK-418：唯一 click init-db 才包装；库已存在则跳过，避免清空教程库。

    BUG-931：守卫检查的必须是源码能**完整证明**的实际库路径（见
    :func:`_flask_instance_sqlite_relpath` / :func:`_flask_instance_base`）。
    返回 ``(init 命令, 库绝对路径, 必须不存在的可选配置文件)``。
    """
    module = target.split(":", 1)[0]
    if not module:
        return None
    pkg = root / module.replace(".", "/")
    files: list[Path] = []
    if pkg.is_dir():
        files.extend(sorted(p for p in pkg.glob("*.py") if p.is_file()))
    elif (root / f"{module}.py").is_file():
        files.append(root / f"{module}.py")
    texts = [_read_text_if_exists(path) for path in files]
    hits = [text for text in texts if _CLICK_INIT_DB_RE.search(text)]
    if len(hits) != 1:
        return None
    text = "\n".join(texts)
    base = _flask_instance_base(text)
    relpath = _flask_instance_sqlite_relpath(text)
    guards = _flask_configuration_guards(text, base) if base else None
    if base is None or relpath is None or guards is None:
        if notes is not None and _FLASK_INSTANCE_SQLITE_RE.search(text):
            notes.append(
                "Flask 工厂含 init-db，但 instance 库路径无法从源码完整证明"
                "（动态路径、配置覆盖或构造参数不确定），未自动包装 init-db；"
                "请确认库路径后手动初始化"
            )
        return None
    return f"flask --app {module} init-db", f"{base}/{relpath}", guards


def _note_gunicorn_generated(notes: list[str] | None) -> None:
    if notes is None:
        return
    notes.append(
        "IMP-066：已生成 Gunicorn 生产启动命令（平台运行时依赖，构建时分层安装，"
        "不回写项目依赖文件）"
    )


def _note_wsgi_fallback(notes: list[str] | None, framework: str, reason: str | None) -> None:
    if notes is None:
        return
    notes.append(
        f"[预检警告] IMP-066：{reason or '入口不可证明'}，{framework} 使用开发服务器 "
        "fallback——仅建议内部快速验证；长期运行请在项目依赖声明 Gunicorn，"
        "或调整源码布局使 WSGI 入口可唯一推断"
    )


def dev_server_fallback_warning(start: str | None) -> str | None:
    """IMP-066（066.f）：开发服务器 fallback 的同源警告文案（doctor / 扫描共用）。"""
    if not start or not _DEV_SERVER_START_RE.search(start):
        return None
    return (
        "启动命令使用框架开发服务器（flask run / manage.py runserver）——"
        "仅建议内部快速验证；长期运行请在项目依赖声明 Gunicorn，"
        "或调整源码布局使 WSGI 入口可唯一推断（IMP-066）"
    )


def _infer_uvicorn_app_target(summary: FileSummary | None) -> str:
    """推断 uvicorn 的 ``module:attr`` 目标（BUG-455）。

    优先级：``app/main.py`` → ``app.main:app``；``src/main.py`` → ``main:app``
    （Dockerfile 已对 src 布局注入 ``PYTHONPATH=src``）；根 ``main.py`` →
    ``main:app``；根 ``app.py`` → ``app:app``；未知则回退 ``main:app``。
    """
    if summary is None:
        return "main:app"
    root = summary.root
    if (root / "app" / "main.py").is_file():
        return "app.main:app"
    if (root / "src" / "main.py").is_file():
        return "main:app"
    if (root / "main.py").is_file():
        return "main:app"
    if (root / "app.py").is_file():
        return "app:app"
    return "main:app"


def _has_index_anywhere(root: Path, *, max_depth: int = 2) -> bool:
    for path in _walk(root, max_depth=max_depth):
        if path.is_file() and path.name.lower() == "index.html":
            return True
    return False


# BUG-878（#109）：Go 主程序入口的 package main 声明（行首锚定，避免注释误命中）。
_GO_PACKAGE_MAIN_RE = re.compile(r"^\s*package\s+main\s*$", re.MULTILINE)


def _has_go_main_service(root: Path, summary: FileSummary) -> bool:
    """源码根是否存在 Go 主服务（BUG-878，#109）。

    memos 类项目（根 go.mod + main.go/cmd 入口 + web/ React 前端）曾被整体
    识别为纯前端并成功部署——前端能打开但无 Go 后端，不是完整服务。命中
    本函数时不得把前端子目录单独当完整应用部署，应判 pending 并明确提示。

    不误伤两类项目：
    - 纯前端/纯 Node/Python 项目（根本无 go.mod）；
    - 前后端分离且后端为 Node/Python 的项目（根有更强的主服务特征，
      如 fastapi-template 的 backend/ Python——走各自识别分支，不进本规则）。
    """
    if "go.mod" not in summary.top_files:
        return False
    node_deps = {d.lower() for d in summary.node_deps}
    if summary.has_package_json and (node_deps & NODE_FRONTEND or node_deps & NODE_BACKEND):
        return False
    py_bases = {d.lower().split("[")[0] for d in summary.python_deps}
    if (py_bases & set(PYTHON_WEB)) or summary.has_manage_py:
        return False
    # Go 入口信号：根 main.go，或浅层（≤3 层，覆盖 cmd/<name>/main.go 惯例）
    # 任一 .go 文件的 package main
    if (root / "main.go").is_file():
        return True
    for path in _walk(root, max_depth=3):
        if not path.is_file() or path.suffix.lower() != ".go":
            continue
        if _GO_PACKAGE_MAIN_RE.search(_read_text_if_exists(path)):
            return True
    return False


def _go_main_service_note() -> str:
    return (
        "检测到 Go 主服务（源码根存在 go.mod），超出当前支持范围——"
        "不得仅部署前端子目录充当完整服务（前端可访问但无 Go 后端），标记 pending"
    )


def _has_html_anywhere(root: Path, *, max_depth: int = 2) -> bool:
    """是否存在任意可打开的 ``*.html``（不要求文件名是 index.html）。"""
    for path in _walk(root, max_depth=max_depth):
        if path.is_file() and path.name.lower().endswith(".html"):
            return True
    return False


__all__ = [
    "Scanner",
    "FileSummary",
    "DetectionResult",
    "summarize",
    "dev_server_fallback_warning",
]
