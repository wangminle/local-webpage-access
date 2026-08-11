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
from local_webpage_access.models import ProjectEvidence, SubdirSignal

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
    evidence.hasRuntimePaths = (
        (root / "src" / "app" / "runtime_paths.py").is_file()
        or (root / "app" / "runtime_paths.py").is_file()
    )
    evidence.hasEnvExample = ".env.example" in evidence.rootFiles

    # 5. SQLite 文件
    for path in _walk(root, max_depth=3):
        if path.is_file() and path.name.lower().endswith((".sqlite", ".sqlite3", ".db")):
            rel = path.relative_to(root)
            evidence.sqliteFiles.append(str(rel).replace("\\", "/"))

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
