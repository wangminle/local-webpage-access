"""项目扫描与运行形态识别（WBS-08）。

用确定性规则识别 V1 支持的项目类型，推断技术栈、内部端口和安装/构建/启动命令。
无法判断时返回 ``confidence=low``，调用方应把实例标记为 pending。

对应 V1 设计说明第 5、12 节。
"""

from __future__ import annotations

import json
import re
import tomllib  # Python 3.11+ 标准库（项目要求 >=3.13）
from dataclasses import dataclass, field
from pathlib import Path

from local_webpage_access.logging import get_logger
from local_webpage_access.models import (
    DatabaseConfig,
    EntryConfig,
    Kind,
    ResourceProfile,
    Runtime,
    ServingMode,
)

log = get_logger("scanner")

# ---- 框架特征常量 -----------------------------------------------------------

NODE_FRONTEND = {"vite", "react", "react-dom", "vue", "@vitejs/plugin-react", "svelte", "preact"}
NODE_BACKEND = {
    "express", "fastify", "koa", "@nestjs/core", "next", "nuxt",
    "@hono/node-server", "polka", "restana",
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
    "tornado": ("tornado", 8000),
}
# 选择主导框架的固定优先级（BUG-181）：_infer_python_port 与 _python_start_command
# 必须按同一优先级挑框架。否则 flask+gunicorn 等多框架项目会因 matched（源自 set
# 迭代）顺序随机，出现 internalPort 推断与启动命令端口不一致，部署出不可达实例。
_PYTHON_FRAMEWORK_PRIORITY = (
    "fastapi", "flask", "django", "streamlit", "gradio", "uvicorn",
    "gunicorn", "starlette", "sanic", "tornado",
)
HEAVY_DATABASES = {"psycopg2", "psycopg", "asyncpg", "pymysql", "mysqlclient", "redis", "aiomysql", "aioredis"}
SQLITE_MARKERS = {"sqlite3", "better-sqlite3", "sqlalchemy", "peewee", "tortoise-orm", "aiosqlite"}
SQLITE_FILE_EXT = (".sqlite", ".sqlite3", ".db")
# IMP-018（WBS-20260708 阶段2.3）：命中即把资源档位自动升 medium（已 medium/heavy 不降）。
# 这些库运行时常驻较重内存（向量库/张量/嵌入缓存），恒 512m 易 OOM（runtime §4.2-P8）。
HEAVY_RUNTIMES = {
    "lancedb", "pyarrow", "torch", "transformers", "tensorflow",
    "openai", "anthropic", "chromadb", "pymilvus",
}


# ---- 文件摘要 ---------------------------------------------------------------


@dataclass
class FileSummary:
    """项目目录的确定性事实摘要。"""

    root: Path
    top_files: set[str] = field(default_factory=set)  # 顶层文件名（小写）
    has_index_html: bool = False
    has_package_json: bool = False
    has_requirements_txt: bool = False
    has_requirements_prod: bool = False
    has_pyproject_toml: bool = False
    has_pipfile: bool = False
    has_uv_lock: bool = False
    has_manage_py: bool = False
    has_runtime_paths: bool = False  # BUG-198：src/app/runtime_paths.py 等
    node_deps: dict[str, str] = field(default_factory=dict)  # 包名 -> 版本（含 devDependencies，BUG-019）
    node_scripts: dict[str, str] = field(default_factory=dict)
    python_deps: set[str] = field(default_factory=set)
    sqlite_files: list[str] = field(default_factory=list)
    total_files: int = 0


def summarize(root: Path) -> FileSummary:
    """收集项目目录的文件摘要（仅扫顶层 + 浅层，避免大目录过慢）。"""
    summary = FileSummary(root=root)

    # 顶层文件
    for entry in root.iterdir():
        if entry.is_file():
            name = entry.name.lower()
            summary.top_files.add(name)
            summary.total_files += 1
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
            elif name == "manage.py":
                summary.has_manage_py = True
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
            if path.name == "runtime_paths.py" and "app" in path.parts:
                summary.has_runtime_paths = True

    if (
        (root / "src" / "app" / "runtime_paths.py").is_file()
        or (root / "app" / "runtime_paths.py").is_file()
    ):
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
        summary.node_scripts = pkg.get("scripts", {}) or {}

    summary.python_deps = _collect_python_deps(root, summary)

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
        except (OSError, tomllib.TOMLDecodeError):
            pass
    return deps


# ---- 识别结果 ---------------------------------------------------------------


@dataclass
class DetectionResult:
    """扫描识别结果，供 importer 构建 manifest 使用。"""

    kind: Kind | None = None
    runtime: Runtime | None = None
    servingMode: ServingMode | None = None
    resourceProfile: ResourceProfile = ResourceProfile.SMALL
    form: str = "unknown"  # static / frontend-static / backend-container / fullstack-sqlite / unknown
    stack: list[str] = field(default_factory=list)
    hasDatabase: bool = False
    database: DatabaseConfig | None = None
    entry: EntryConfig = field(default_factory=EntryConfig)
    internalPort: int | None = None
    confidence: str = "low"  # high / medium / low
    pending: bool = False
    notes: list[str] = field(default_factory=list)


# ---- 扫描器 -----------------------------------------------------------------


class Scanner:
    """项目类型识别器（确定性规则）。"""

    def detect(self, project_dir: Path) -> DetectionResult:
        if not project_dir.is_dir():
            return DetectionResult(confidence="low", pending=True, notes=["项目目录不存在"])

        summary = summarize(project_dir)
        result = DetectionResult()

        # 1. 重型数据库优先判断（不自动启动）
        heavy = self._detect_heavy_db(summary)
        if heavy:
            result.pending = True
            result.confidence = "medium"
            result.notes.append(f"检测到重型数据库依赖：{heavy}，标记 pending")
            # 仍尝试识别语言族，方便 skill 处理
            self._fill_language(summary, result)
            return result

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
        if summary.has_package_json and self._is_real_node(summary):
            self._detect_node(summary, result)
        elif has_python_signal:
            self._detect_python(summary, result)
        elif summary.has_package_json and not has_python_signal:
            # package.json 存在但既非真 Node 也无 Python 工程文件：仍按 Node 兜底尝试
            self._detect_node(summary, result)
        elif summary.has_index_html:
            self._detect_static(summary, result)
        else:
            # 兜底：如果有 index.html 在子目录，尝试当作静态
            if _has_index_anywhere(project_dir):
                self._detect_static(summary, result)
            else:
                result.pending = True
                result.confidence = "low"
                result.notes.append("无法识别项目类型，标记 pending")

        return result

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
        found: set[str] = set()
        all_deps: set[str] = {d.lower() for d in summary.node_deps} | {
            d.lower() for d in summary.python_deps
        }
        for dep in all_deps:
            base = re.split(r"[<>=!\[ ]", dep, maxsplit=1)[0]
            if base in HEAVY_DATABASES:
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
            result.notes.append(
                f"检测到重运行时依赖：{', '.join(sorted(hit))}，资源档位升 medium"
            )

    # ---- SQLite ------------------------------------------------------------

    def _detect_sqlite(self, summary: FileSummary, result: DetectionResult) -> None:
        has_sqlite_dep = bool(
            SQLITE_MARKERS & {d.lower() for d in summary.python_deps}
        ) or bool(
            SQLITE_MARKERS & {d.lower() for d in summary.node_deps}
        )
        if summary.sqlite_files or has_sqlite_dep:
            result.hasDatabase = True
            # BUG-198：RUNTIME_ROOT 应用写 runtime/data，compose 据此挂载
            data_dir = "runtime/data" if summary.has_runtime_paths else "data"
            result.database = DatabaseConfig(
                type="sqlite",
                dataDir=data_dir,
            )
            if summary.sqlite_files:
                result.notes.append(f"发现 SQLite 文件：{', '.join(summary.sqlite_files[:3])}")
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
            result.resourceProfile = ResourceProfile.MEDIUM if (
                "next" in deps_lower or "nuxt" in deps_lower
            ) else ResourceProfile.SMALL
            result.internalPort = _infer_node_port(summary)
            result.entry = EntryConfig(
                install=_node_install_command(summary),
                build="npm run build" if has_build else None,
                start="npm run start" if "start" in summary.node_scripts else "node server.js",
            )
            result.confidence = "high"
        elif is_frontend and has_build:
            # 纯前端：构建后静态托管
            result.runtime = Runtime.SHARED_STATIC
            result.servingMode = ServingMode.SHARED_STATIC
            result.form = "frontend-static"
            result.resourceProfile = ResourceProfile.TINY
            result.entry = EntryConfig(
                install=_node_install_command(summary),
                build="npm run build",
                start=None,
            )
            result.confidence = "high"
        else:
            # Node 项目但无法确定形态
            result.pending = True
            result.confidence = "low"
            result.notes.append("Node 项目缺少明确的 frontend/backend 特征，标记 pending")

    # ---- Python ------------------------------------------------------------

    def _detect_python(self, summary: FileSummary, result: DetectionResult) -> None:
        result.kind = Kind.PYTHON
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
            start = _python_start_command(matched, summary)
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
        else:
            result.pending = True
            result.confidence = "low"
            result.notes.append("Python 项目缺少 Web 框架特征，标记 pending")


# ---- 辅助：端口与命令推断 ---------------------------------------------------


def _has_lockfile(summary: FileSummary) -> bool:
    return any(
        f in summary.top_files
        for f in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml")
    )


def _node_install_command(summary: FileSummary) -> str:
    """根据锁文件选择包管理器，避免 pnpm/yarn 项目误走 npm ci。"""
    files = summary.top_files
    if "pnpm-lock.yaml" in files:
        return "corepack enable && pnpm install --frozen-lockfile"
    if "yarn.lock" in files:
        return "corepack enable && yarn install --frozen-lockfile"
    if "package-lock.json" in files:
        return "npm ci"
    return "npm install"


def _infer_node_port(summary: FileSummary) -> int:
    """推断 Node 后端容器内监听端口。

    优先级：
    1. ``package.json`` scripts 中显式配置的端口（``PORT=8080``、``--port 8080``）；
    2. Node 生态通用默认 ``3000``（Next/Nuxt/Nest 等 meta-framework 与多数
       Express/Fastify 样例均默认 3000）。

    此前本函数用 if 链"区分" next/nuxt/nest，但所有分支都返回 3000（BUG-032，
    死代码）。Next/Nuxt/Nest 确实都默认 3000，无需分支；真正缺的是从用户脚本
    里读取显式端口。raw Node 后端的端口由应用代码决定，无法静态可靠推断，
    只在 scripts 显式声明时才采纳，否则回退默认，由 compose 端口映射兜底。
    """
    port = _extract_port_from_scripts(summary.node_scripts)
    if port is not None:
        return port
    return 3000


def _extract_port_from_scripts(scripts: dict) -> int | None:
    """从 package.json scripts 文本尽力解析端口。

    仅匹配无歧义的两种写法：``PORT=8080``（环境变量内联）与 ``--port 8080``
    （含 ``--port=8080``）。短旗 ``-p`` 含义过多（pid/print 等）不采纳。
    """
    if not scripts:
        return None
    blob = " ".join(str(v) for v in scripts.values())
    m = re.search(r"\bPORT=(\d{2,5})\b", blob)
    if m:
        return int(m.group(1))
    m = re.search(r"--port[=\s]+(\d{2,5})", blob)
    if m:
        return int(m.group(1))
    return None


def _select_python_framework(matched: list[str]) -> str | None:
    """按固定优先级挑出主导框架（消除 set 迭代顺序导致的端口/命令不一致，BUG-181）。"""
    lower = {m.lower() for m in matched}
    for fw in _PYTHON_FRAMEWORK_PRIORITY:
        if fw in lower:
            return fw
    return matched[0].lower() if matched else None


def _infer_python_port(summary: FileSummary, matched: list[str]) -> int:
    fw = _select_python_framework(matched)
    if fw and fw in PYTHON_WEB:
        return PYTHON_WEB[fw][1]
    return 8000


def _python_install_command(summary: FileSummary) -> str:
    if summary.has_uv_lock:
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


def _python_start_command(matched: list[str], summary: FileSummary) -> str | None:
    fw = _select_python_framework(matched)
    if fw in ("fastapi", "uvicorn", "starlette"):
        # starlette 应用通常经 uvicorn 托管
        return 'uvicorn main:app --host 0.0.0.0 --port 8000'
    if fw == "flask":
        return 'flask --app app run --host 0.0.0.0 --port 5000'
    if fw == "django":
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


def _has_index_anywhere(root: Path, *, max_depth: int = 2) -> bool:
    for path in _walk(root, max_depth=max_depth):
        if path.is_file() and path.name.lower() == "index.html":
            return True
    return False


__all__ = [
    "Scanner",
    "FileSummary",
    "DetectionResult",
    "summarize",
]
