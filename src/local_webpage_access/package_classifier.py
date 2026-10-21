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
from local_webpage_access.node_toolchain import workspace_contains

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

    # 明确声明了成员范围时，弱信号不得重新纳入被排除的独立项目。
    # 与工具链选择共用判断，避免 scanner 编排成 npm -w，而 install 已选子目录。
    if patterns:
        pkg_dirs = [rel for rel in pkg_dirs if workspace_contains(root, root / rel)]
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


# BUG-881：目录/过滤选项按包管理器分别登记——只有命令首 token 是对应包管理器
# 时，这些选项才表示「定位子包」；普通命令（node server.js --dir app、
# vite --prefix out 等）的同名参数与包管理器无关，不得据此选主。
_PM_DIR_FLAGS = {
    "yarn": ("--cwd",),
    "pnpm": ("--dir", "-C"),
    "npm": ("--prefix",),
}
# 按包名定位子包的选项（pnpm --filter/-F、npm --workspace/-w；yarn 用
# ``yarn workspace <name>`` 子命令形式，在扫描循环里单独识别）。
_PM_NAME_FLAGS = {
    "pnpm": ("--filter", "-F"),
    "npm": ("--workspace", "-w"),
}


def _root_start_target_package(
    root: Path, classifications: list[PackageClassification]
) -> PackageClassification | None:
    """根 package.json 的 start 脚本显式指向的可部署子包（BUG-880）。

    真实 monorepo 常在 examples/ 放可部署示例——按 §4.4「唯一 web_server
    优先」会误选示例，偏离根入口 ``yarn --cwd ./excalidraw-app start`` 声明
    的主应用。识别形式（首 token 必须是对应包管理器，BUG-881）：

    - ``yarn --cwd <dir>`` / ``pnpm --dir <dir>``（或 ``-C``）/
      ``npm --prefix <dir>``（``--flag <dir>`` 与 ``--flag=<dir>`` 两种写法）；
    - ``yarn workspace <name>``；
    - ``pnpm --filter <name>``（或 ``-F``）/ ``npm --workspace <name>``
      （或 ``-w``，``--flag=<name>`` 亦可；值也接受子包相对路径）。

    只扫描 ``--`` 参数转发分隔符之前的 token——其后属于目标脚本自身的
    参数，与包管理器定位无关（BUG-881）。

    仅当目标唯一且可部署（web_server / frontend_build）时返回该包，
    否则返回 None（回落 §4.4 决策表）。
    """
    pkg = _read_pkg_json(root / "package.json")
    scripts = pkg.get("scripts")
    start = scripts.get("start") if isinstance(scripts, dict) else None
    if not isinstance(start, str):
        return None
    tokens = start.split()
    if not tokens or tokens[0] not in _PM_DIR_FLAGS:
        # BUG-881：非包管理器命令（node server.js --dir app 等）不作选主提示。
        return None
    pm = tokens[0]
    dir_flags = _PM_DIR_FLAGS[pm]
    name_flags = _PM_NAME_FLAGS.get(pm, ())

    target_dir: str | None = None
    target_name: str | None = None
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            # BUG-881：`--` 是包管理器的参数转发分隔符，其后 token 原样传给
            # 目标脚本（``npm run serve -- --prefix app`` 实测应用收到该参数），
            # 不再是包管理器自己的定位选项，到此停止扫描。
            break
        if tok in dir_flags and i + 1 < len(tokens):
            target_dir = tokens[i + 1]
            break
        if any(tok.startswith(f + "=") for f in dir_flags):
            target_dir = tok.split("=", 1)[1]
            break
        if tok in name_flags and i + 1 < len(tokens):
            target_name = tokens[i + 1]
            break
        if any(tok.startswith(f + "=") for f in name_flags):
            target_name = tok.split("=", 1)[1]
            break
        if pm == "yarn" and tok == "workspace" and i + 1 < len(tokens):
            target_name = tokens[i + 1]
            break
        i += 1

    def _norm_dir(value: str) -> str:
        norm = value.strip().strip("\"'")
        while norm.startswith("./"):
            norm = norm[2:]
        return norm.rstrip("/")

    matches: list[PackageClassification] = []
    if target_dir is not None:
        norm = _norm_dir(target_dir)
        if norm and ".." not in norm.split("/"):
            matches = [c for c in classifications if c.path == norm]
    elif target_name is not None:
        name = target_name.strip().strip("\"'")
        matches = [c for c in classifications if c.name == name]
        if not matches:
            # pnpm --filter / npm -w 也接受相对路径（如 ./excalidraw-app）
            norm = _norm_dir(name)
            if norm and ".." not in norm.split("/"):
                matches = [c for c in classifications if c.path == norm]
    deployable = [c for c in matches if c.isDeployable]
    return deployable[0] if len(deployable) == 1 else None


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

    # BUG-880：根 start 脚本显式指向某可部署子包时优先于 §4.4 决策表——
    # 真实 monorepo 的 examples/ 常含可部署示例（唯一 web_server），按表
    # 会误选示例而偏离根入口（yarn --cwd 等）声明的主应用。
    hinted = _root_start_target_package(root, classifications)
    if hinted is not None:
        notes.append(f"primary={hinted.path} ({hinted.name})（根 start 脚本指定，优先于选主表）")
        return ClassificationResult(
            classifications=classifications,
            primary=hinted,
            is_monorepo=True,
            notes=notes,
        )

    if len(web_servers) == 1:
        primary = web_servers[0]
        notes.append(f"primary={primary.path} ({primary.name})")
    elif len(web_servers) >= 2:
        candidates = ", ".join(f"{c.path} ({c.name})" for c in web_servers)
        notes.append(
            f"发现 {len(web_servers)} 个 web_server 子包：{candidates}；"
            "标记 pending（可拆分导入：git 源用 --from-git <repo> "
            f"--subdir <子包路径> 单独导入其中一个，如 --subdir {web_servers[0].path}）"
        )
    elif len(frontend_builds) == 1:
        primary = frontend_builds[0]
        notes.append(f"primary={primary.path} ({primary.name})")
    elif len(frontend_builds) >= 2:
        candidates = ", ".join(f"{c.path} ({c.name})" for c in frontend_builds)
        notes.append(
            f"发现 {len(frontend_builds)} 个 frontend_build 子包：{candidates}；"
            "标记 pending（可拆分导入：git 源用 --from-git <repo> "
            f"--subdir <子包路径> 单独导入其中一个，如 --subdir {frontend_builds[0].path}）"
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
