"""Monorepo 包分类器（IMP-057 Gate-1）。

检测 npm workspaces monorepo，对子包进行 6 值分类（PackageType），
并按选主决策表选出 primary package。

分类规则（优先级从高到低，§4.3）：

1. deps ∪ devDeps 含 ``electron`` -> ``electron_desktop``
2. deps ∪ devDeps ∩ NODE_FRONTEND 非空 且 无 NODE_BACKEND 且有 scripts.build
   -> ``frontend_build``
3. deps ∪ devDeps ∩ NODE_BACKEND 非空 或 scripts 含 "server" 或
   （scripts 含 "start" 且非步骤 2 的纯前端）-> ``web_server``
4. 有 main 或 exports -> ``library``
5. 否则 -> ``unknown``

选主决策表（§4.4）：

| 可部署候选 | 行为 |
|---|---|
| 0 个 web_server 且 0 个 frontend_build | pending |
| 恰好 1 个 web_server | 选该包 |
| ≥1 web_server + frontend_build | 只在 web_server 中选 |
| 恰好 1 个 frontend_build（无 web_server） | 选该包 |
| ≥2 个 web_server | pending |
| ≥2 个 frontend_build（无 web_server） | pending |
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from local_webpage_access.logging import get_logger
from local_webpage_access.models import PackageClassification, PackageType

log = get_logger("package_classifier")


@dataclass
class ClassificationResult:
    """分类结果。"""

    classifications: list[PackageClassification]
    primary: PackageClassification | None
    is_monorepo: bool
    notes: list[str]


def detect_workspaces(root: Path) -> list[str]:
    """检测 npm workspaces 并返回子包目录列表。

    支持：
    - 根 package.json 的 ``"workspaces"`` 字段（数组或 ``{ packages: [...] }``）
    - ``packages/*/package.json`` 作为弱信号（与 workspaces 取并集去重）

    返回相对于 ``root`` 的路径列表（如 ``["packages/webpage", "packages/core"]``）。
    非 monorepo 返回空列表。
    """
    pkg_path = root / "package.json"
    if not pkg_path.is_file():
        return []

    try:
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    if not isinstance(pkg, dict):
        return []

    # 解析 workspaces 字段
    ws = pkg.get("workspaces")
    patterns: list[str] = []
    if isinstance(ws, list):
        patterns = [str(p) for p in ws]
    elif isinstance(ws, dict) and isinstance(ws.get("packages"), list):
        patterns = [str(p) for p in ws["packages"]]

    # 展开通配符模式（如 "packages/*"）
    pkg_dirs: list[str] = []
    seen: set[str] = set()

    for pattern in patterns:
        if "*" in pattern:
            # glob 展开包目录
            parent = root / pattern.split("*")[0].rstrip("/")
            if parent.is_dir():
                for entry in sorted(parent.iterdir()):
                    if entry.is_dir() and (entry / "package.json").is_file():
                        rel = _rel_within_root(root, entry)
                        if rel is not None and rel not in seen:
                            seen.add(rel)
                            pkg_dirs.append(rel)
        else:
            # 直接路径
            target = root / pattern
            if target.is_dir() and (target / "package.json").is_file():
                rel = _rel_within_root(root, target)
                if rel is not None and rel not in seen:
                    seen.add(rel)
                    pkg_dirs.append(rel)

    # 弱信号：packages/*/package.json（与 workspaces 取并集）
    packages_dir = root / "packages"
    if packages_dir.is_dir():
        for entry in sorted(packages_dir.iterdir()):
            if entry.is_dir() and (entry / "package.json").is_file():
                rel = _rel_within_root(root, entry)
                if rel is not None and rel not in seen:
                    seen.add(rel)
                    pkg_dirs.append(rel)

    return sorted(pkg_dirs)


def _rel_within_root(root: Path, path: Path) -> str | None:
    """返回 ``path`` 相对 ``root`` 的规范化路径；越界（含 ``..``）返回 ``None``。

    Python 3.13 的 :meth:`Path.relative_to` 对 ``root/../shared`` 这类越界路径会
    返回 ``../shared`` 而非抛错，直接流入分类会把仓库外目录当子包（BUG-508）。
    """
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None
    if rel.is_absolute() or ".." in rel.parts:
        return None
    return rel.as_posix()


def _read_pkg_json(path: Path) -> dict:
    """读取 package.json，失败返回空 dict。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def classify_package(
    pkg_json: dict,
    rel_path: str,
) -> PackageClassification:
    """对单个子包进行分类（§4.3 规则）。

    Parameters
    ----------
    pkg_json
        子包的 ``package.json`` 解析结果。
    rel_path
        相对于项目根的路径（如 ``"packages/webpage"``）。
    """
    name = pkg_json.get("name") or rel_path
    deps = {k.lower() for k in (pkg_json.get("dependencies") or {}).keys()}
    dev_deps = {k.lower() for k in (pkg_json.get("devDependencies") or {}).keys()}
    # 延迟导入避免循环依赖（scanner -> package_classifier -> scanner）
    from local_webpage_access.scanner import NODE_BACKEND, NODE_FRONTEND

    all_deps = deps | dev_deps
    scripts = pkg_json.get("scripts") or {}
    script_names = set(scripts.keys())

    # 步骤 1：electron
    if "electron" in all_deps:
        return PackageClassification(
            name=name,
            path=rel_path,
            packageType=PackageType.ELECTRON_DESKTOP.value,
            isDeployable=False,
        )

    # 步骤 2：frontend_build
    is_frontend = bool(all_deps & NODE_FRONTEND)
    is_backend = bool(all_deps & NODE_BACKEND)
    has_build = "build" in script_names

    if is_frontend and not is_backend and has_build:
        return PackageClassification(
            name=name,
            path=rel_path,
            packageType=PackageType.FRONTEND_BUILD.value,
            isDeployable=True,
        )

    # 步骤 3：web_server
    has_server_script = any("server" in s for s in script_names)
    has_start_script = "start" in script_names
    if is_backend or has_server_script or (has_start_script and not (is_frontend and has_build)):
        return PackageClassification(
            name=name,
            path=rel_path,
            packageType=PackageType.WEB_SERVER.value,
            isDeployable=True,
        )

    # 步骤 4：library
    has_main = bool(pkg_json.get("main"))
    has_exports = bool(pkg_json.get("exports"))
    if has_main or has_exports:
        return PackageClassification(
            name=name,
            path=rel_path,
            packageType=PackageType.LIBRARY.value,
            isDeployable=False,
        )

    # 步骤 5：unknown
    return PackageClassification(
        name=name,
        path=rel_path,
        packageType=PackageType.UNKNOWN.value,
        isDeployable=False,
    )


def classify_and_select(root: Path) -> ClassificationResult:
    """检测 monorepo、分类所有子包、选主包。

    非 monorepo 返回空分类列表和 ``is_monorepo=False``。
    """
    pkg_dirs = detect_workspaces(root)
    if not pkg_dirs:
        return ClassificationResult(
            classifications=[],
            primary=None,
            is_monorepo=False,
            notes=[],
        )

    # 分类每个子包
    classifications: list[PackageClassification] = []
    for rel in pkg_dirs:
        pkg_json = _read_pkg_json(root / rel / "package.json")
        cls = classify_package(pkg_json, rel)
        classifications.append(cls)
        log.debug("分类 %s: %s (%s)", rel, cls.packageType, cls.name)

    # 选主（§4.4 决策表）
    web_servers = [c for c in classifications if c.packageType == PackageType.WEB_SERVER.value]
    frontend_builds = [
        c for c in classifications if c.packageType == PackageType.FRONTEND_BUILD.value
    ]

    notes: list[str] = []
    primary: PackageClassification | None = None

    # 分类摘要 note
    summary_parts = [f"{c.path}={c.packageType}" for c in classifications]
    notes.append(f"分类：{', '.join(summary_parts)}")

    if len(web_servers) == 1:
        primary = web_servers[0]
        notes.append(f"primary={primary.path} ({primary.name})")
    elif len(web_servers) >= 2:
        candidates = ", ".join(f"{c.path} ({c.name})" for c in web_servers)
        notes.append(
            f"发现 {len(web_servers)} 个 web_server 子包：{candidates}；标记 pending（后续可用 --package 指定）"
        )
    elif len(frontend_builds) == 1:
        primary = frontend_builds[0]
        notes.append(f"primary={primary.path} ({primary.name})")
    elif len(frontend_builds) >= 2:
        candidates = ", ".join(f"{c.path} ({c.name})" for c in frontend_builds)
        notes.append(
            f"发现 {len(frontend_builds)} 个 frontend_build 子包：{candidates}；标记 pending（后续可用 --package 指定）"
        )
    else:
        notes.append("monorepo 中未发现 Web 可部署子包")

    return ClassificationResult(
        classifications=classifications,
        primary=primary,
        is_monorepo=True,
        notes=notes,
    )


__all__ = [
    "ClassificationResult",
    "classify_package",
    "classify_and_select",
    "detect_workspaces",
]
