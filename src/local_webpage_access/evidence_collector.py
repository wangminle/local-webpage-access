"""Layer 0：证据收集（IMP-058 Gate-B）。

收集项目目录的所有客观事实（毫秒级，纯文件操作），输出 :class:`ProjectEvidence`。
不做任何解释或判断--那 是 Layer 1（candidate_generator）的职责。

与 :func:`scanner.summarize` 的关系：``summarize`` 返回 :class:`FileSummary`（根目录摘要），
``collect`` 返回 :class:`ProjectEvidence`（更丰富，含子目录探测）。Gate-B 阶段
``detect()`` 改为消费 ``ProjectEvidence``，但 ``summarize`` 仍保留供向后兼容。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from local_webpage_access.logging import get_logger
from local_webpage_access.models import DatabaseSignal, ProjectEvidence, SubdirSignal

log = get_logger("evidence_collector")

# 常见子目录名（A 类盲区修复核心）
COMMON_SUBDIRS = ("backend", "server", "api", "app", "frontend", "client", "web", "ui")

# 跳过的噪音目录
_SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "__pycache__", "dist", "build", ".next"}


def collect(root: Path) -> ProjectEvidence:
    """收集项目目录的所有客观事实（毫秒级，纯文件操作）。"""
    evidence = ProjectEvidence(root=str(root))

    # 1. 根目录顶层文件 + 子目录
    try:
        for entry in root.iterdir():
            name = entry.name.lower()
            if entry.is_file():
                evidence.rootFiles.append(name)
                if name == "index.html":
                    evidence.hasIndexHtml = True
                if name.endswith(".html"):
                    evidence.hasHtml = True
                if name == "package.json":
                    evidence.hasPackageJson = True
            elif entry.is_dir() and name not in _SKIP_DIRS:
                evidence.rootDirs.append(name)
    except (PermissionError, OSError):
        pass

    # 2. 子目录工程文件探测（A 类修复核心）
    for subdir_name in COMMON_SUBDIRS:
        subdir = root / subdir_name
        if subdir.is_dir():
            signal = _probe_subdir(subdir, subdir_name)
            if (
                signal.hasRequirements
                or signal.hasPackageJson
                or signal.hasPyproject
                or signal.hasManagePy
                or signal.hasIndexHtml
            ):
                evidence.subdirSignals.append(signal)

    # 3. 根目录依赖收集
    if evidence.hasPackageJson:
        pkg = _read_package_json(root / "package.json")
        if pkg:
            evidence.nodeDeps = _merge_node_deps(pkg)
            evidence.nodeScripts = pkg.get("scripts", {}) or {}
            workspaces = pkg.get("workspaces")
            if workspaces:
                if isinstance(workspaces, list):
                    evidence.workspaces = workspaces
                elif isinstance(workspaces, dict) and "packages" in workspaces:
                    evidence.workspaces = workspaces["packages"]

    evidence.pythonDeps = sorted(_collect_python_deps(root))

    # 4. 根目录其他信号
    evidence.hasManagePy = "manage.py" in evidence.rootFiles
    evidence.hasAlembicIni = "alembic.ini" in evidence.rootFiles
    evidence.hasRuntimePaths = (root / "src" / "app" / "runtime_paths.py").is_file() or (
        root / "app" / "runtime_paths.py"
    ).is_file()
    evidence.hasEnvExample = ".env.example" in evidence.rootFiles

    # 5. SQLite 文件
    for path in _walk(root, max_depth=3):
        if path.is_file() and path.name.lower().endswith((".sqlite", ".sqlite3", ".db")):
            rel = path.relative_to(root)
            evidence.sqliteFiles.append(str(rel).replace("\\", "/"))

    # 5.5 A.R01：数据库配置消费证据
    evidence.databaseConfig = _collect_database_config(root)

    # 6. 项目自带 Dockerfile/compose
    evidence.projectDockerfile = _find_dockerfile(root)
    evidence.projectCompose = _find_compose(root)

    # 7. 构建产物目录
    for name in ("dist", "build", "out", ".output", ".svelte-kit"):
        candidate = root / name
        if candidate.is_dir():
            try:
                if any(candidate.iterdir()):
                    evidence.buildOutputs.append(name)
            except (PermissionError, OSError):
                pass

    return evidence


# ---- 内部辅助 ----------------------------------------------------------------


def _probe_subdir(subdir: Path, name: str) -> SubdirSignal:
    """探测单个子目录的工程文件信号。"""
    signal = SubdirSignal(path=name, name=name)
    try:
        for entry in subdir.iterdir():
            fname = entry.name.lower()
            if entry.is_file():
                if fname == "requirements.txt":
                    signal.hasRequirements = True
                elif fname == "requirements-prod.txt":
                    signal.hasRequirements = True
                elif fname == "pyproject.toml":
                    signal.hasPyproject = True
                elif fname == "package.json":
                    signal.hasPackageJson = True
                elif fname == "manage.py":
                    signal.hasManagePy = True
                elif fname == "alembic.ini":
                    signal.hasAlembicIni = True
                elif fname == "index.html":
                    signal.hasIndexHtml = True
    except (PermissionError, OSError):
        pass

    # 收集子目录依赖
    if signal.hasRequirements or signal.hasPyproject:
        signal.pythonDeps = sorted(_collect_python_deps(subdir))

    if signal.hasPackageJson:
        pkg = _read_package_json(subdir / "package.json")
        if pkg:
            signal.nodeDeps = _merge_node_deps(pkg)
            signal.nodeScripts = pkg.get("scripts", {}) or {}

    return signal


def _read_package_json(path: Path) -> dict:
    """安全读取 package.json。"""
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _merge_node_deps(pkg: dict) -> dict[str, str]:
    """合并 dependencies + devDependencies（BUG-019）。"""
    deps: dict[str, str] = {}
    deps.update(pkg.get("devDependencies", {}) or {})
    deps.update(pkg.get("dependencies", {}) or {})
    return deps


def _collect_python_deps(directory: Path) -> set[str]:
    """收集目录的 Python 依赖（复用 scanner 逻辑的简化版）。"""
    deps: set[str] = set()
    req_files: list[Path] = []
    req_prod = directory / "requirements-prod.txt"
    req_txt = directory / "requirements.txt"
    if req_prod.is_file():
        req_files.append(req_prod)
    elif req_txt.is_file():
        req_files.append(req_txt)

    for req_file in req_files:
        try:
            for line in req_file.read_text("utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("-"):
                    continue
                # 去掉版本约束和环境标记
                name = re.split(r"[<>=!\[ ]", line, maxsplit=1)[0].strip().lower()
                if name:
                    deps.add(name)
        except (OSError, UnicodeDecodeError):
            pass

    pyproject = directory / "pyproject.toml"
    if pyproject.is_file():
        try:
            import tomllib

            with pyproject.open("rb") as fh:
                data = tomllib.load(fh)
            items = data.get("project", {}).get("dependencies", [])
            if isinstance(items, list):
                for item in items:
                    name = re.split(r"[<>=!\[ ]", str(item), maxsplit=1)[0].strip().lower()
                    if name:
                        deps.add(name)
            # BUG-502：Poetry 依赖声明在 [tool.poetry.dependencies]，
            # 仅读 PEP 621 会漏掉 Poetry-only 的 FastAPI 等（与 scanner 对齐）。
            poetry = data.get("tool", {}).get("poetry", {})
            if isinstance(poetry, dict):
                sections = [poetry.get("dependencies"), poetry.get("dev-dependencies")]
                groups = poetry.get("group", {})
                if isinstance(groups, dict):
                    for group in groups.values():
                        if isinstance(group, dict):
                            sections.append(group.get("dependencies"))
                for section in sections:
                    if not isinstance(section, dict):
                        continue
                    for key in section:
                        name = key.strip().lower()
                        if name and name != "python":
                            deps.add(name)
        except (OSError, Exception):
            pass

    return deps


def _walk(root: Path, *, max_depth: int):
    """受限深度遍历，跳过常见大目录。"""
    stack = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        if depth >= max_depth:
            continue
        try:
            for entry in current.iterdir():
                if entry.is_dir():
                    if entry.name.lower() not in _SKIP_DIRS:
                        stack.append((entry, depth + 1))
                else:
                    yield entry
        except (PermissionError, OSError):
            continue


def _find_dockerfile(root: Path) -> str | None:
    """查找项目自带 Dockerfile。"""
    # 根目录
    for name in ("Dockerfile", "dockerfile"):
        candidate = root / name
        if candidate.is_file():
            return candidate.name
    # 一层子目录
    try:
        for entry in root.iterdir():
            if entry.is_dir() and entry.name.lower() not in _SKIP_DIRS:
                for name in ("Dockerfile", "dockerfile"):
                    candidate = entry / name
                    if candidate.is_file():
                        return f"{entry.name}/{candidate.name}"
    except (PermissionError, OSError):
        pass
    return None


def _find_compose(root: Path) -> str | None:
    """查找项目自带 docker-compose.yml。"""
    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        candidate = root / name
        if candidate.is_file():
            return candidate.name
    return None


# ---- A.R01：数据库配置消费证据 -------------------------------------------------

# 匹配应用读取 DATABASE_URL 环境变量的常见模式
_DATABASE_URL_ENV_PATTERNS = [
    re.compile(r'os\.environ\.get\s*\(\s*["\']DATABASE_URL["\']'),
    re.compile(r'os\.getenv\s*\(\s*["\']DATABASE_URL["\']'),
    re.compile(r'os\.environ\s*\[\s*["\']DATABASE_URL["\']\s*\]'),
    re.compile(r'environ\.get\s*\(\s*["\']DATABASE_URL["\']'),
    re.compile(r'getenv\s*\(\s*["\']DATABASE_URL["\']'),
]

# 匹配 SQLAlchemy / Starlette 配置中引用 DATABASE_URL
_SQLALCHEMY_URL_PATTERNS = [
    re.compile(r"SQLALCHEMY_DATABASE_URI\s*=.*DATABASE_URL", re.IGNORECASE),
    re.compile(r"SQLALCHEMY_DATABASE_URL\s*=.*DATABASE_URL", re.IGNORECASE),
]

# 匹配 pydantic Settings 中的 database_url 字段
_PYDANTIC_DB_FIELD_RE = re.compile(
    r"database_url\s*[:=]\s*(?:str|Optional\[str\]|str\s*\|\s*None)", re.IGNORECASE
)

# 匹配 SQLite 默认连接串，提取文件名
_SQLITE_URL_RE = re.compile(
    r"sqlite:///(/?\.{0,2}/?[\w./-]+\.(?:sqlite|sqlite3|db))",
    re.IGNORECASE,
)

# 常见配置文件名（相对项目根或一级子目录）
_CONFIG_FILE_NAMES = (
    "config.py",
    "settings.py",
    "database.py",
    "db.py",
    "app/config.py",
    "app/settings.py",
    "app/database.py",
    "app/db.py",
    "core/config.py",
    "core/settings.py",
)


def _collect_database_config(root: Path) -> DatabaseSignal | None:
    """A.R01：扫描项目源码，采集数据库配置消费证据。

    判断应用是否读取 ``DATABASE_URL`` 环境变量，并尝试解析默认连接串
    的路径形态（相对/绝对）和文件名。仅做纯文件扫描，不执行代码。
    """
    signal = DatabaseSignal()

    # 收集候选配置文件：预设文件名 + 含 DATABASE_URL 字符串的 .py 文件
    candidate_files: list[Path] = []
    for name in _CONFIG_FILE_NAMES:
        p = root / name
        if p.is_file():
            candidate_files.append(p)

    # 也扫描一级子目录中的配置文件（如 backend/config.py）
    for subdir_name in COMMON_SUBDIRS:
        subdir = root / subdir_name
        if subdir.is_dir():
            for name in _CONFIG_FILE_NAMES:
                basename = Path(name).name
                p = subdir / basename
                if p.is_file() and p not in candidate_files:
                    candidate_files.append(p)

    # 扫描所有 .py 文件（受限深度），查找含 DATABASE_URL 的文件
    for path in _walk(root, max_depth=3):
        if not path.suffix == ".py":
            continue
        if path in candidate_files:
            continue
        try:
            text = path.read_text("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "DATABASE_URL" in text:
            candidate_files.append(path)

    found_consumption = False
    default_url: str | None = None
    source_path: str | None = None

    for config_file in candidate_files:
        try:
            text = config_file.read_text("utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        # 检查是否消费 DATABASE_URL
        if not found_consumption:
            for pattern in _DATABASE_URL_ENV_PATTERNS + _SQLALCHEMY_URL_PATTERNS:
                if pattern.search(text):
                    found_consumption = True
                    try:
                        source_path = str(config_file.relative_to(root)).replace("\\", "/")
                    except ValueError:
                        source_path = str(config_file)
                    break

            # pydantic Settings 的 database_url 字段也算消费
            if not found_consumption and _PYDANTIC_DB_FIELD_RE.search(text):
                found_consumption = True
                try:
                    source_path = str(config_file.relative_to(root)).replace("\\", "/")
                except ValueError:
                    source_path = str(config_file)

        # 尝试提取默认 SQLite 连接串
        if default_url is None:
            match = _SQLITE_URL_RE.search(text)
            if match:
                default_url = match.group(1)
                if source_path is None:
                    try:
                        source_path = str(config_file.relative_to(root)).replace("\\", "/")
                    except ValueError:
                        source_path = str(config_file)

    signal.consumesDatabaseUrl = found_consumption
    signal.defaultUrl = default_url
    signal.sourcePath = source_path

    if default_url:
        signal.isRelative = not default_url.startswith("/")
        # 提取文件名：取 basename
        # 形如 "./data/bookshelf.db" -> "bookshelf.db"
        # 形如 "/app/data/bookshelf.db" -> "bookshelf.db"
        filename = Path(default_url).name
        if filename and filename.lower().endswith((".sqlite", ".sqlite3", ".db")):
            signal.dbFilename = filename

    if not found_consumption and default_url is None:
        return None  # 无任何数据库配置信号

    return signal
