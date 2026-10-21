"""Node 包管理器与 workspace 上下文（CHK-420 / CHK-423 / CHK-424）。

按锁文件选择 npm/pnpm/yarn/bun，安装命令在 workspace 根执行。pending 路径
也要写出可见提示，避免 bun.lock 或官方 Dockerfile 被静默丢掉。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

from local_webpage_access.logging import get_logger

log = get_logger("node_toolchain")

_LOCK_ORDER: tuple[tuple[str, str, str], ...] = (
    ("pnpm-lock.yaml", "pnpm", "corepack enable && pnpm install --frozen-lockfile"),
    ("yarn.lock", "yarn", "corepack enable && yarn install --frozen-lockfile"),
    ("bun.lock", "bun", "bun install --frozen-lockfile"),
    ("bun.lockb", "bun", "bun install --frozen-lockfile"),
    ("package-lock.json", "npm", "npm ci"),
)


@dataclass(frozen=True)
class NodeToolchain:
    manager: str
    install_command: str
    lockfile: str | None
    workspace_root: Path
    workspaces: bool
    dockerfile: Path | None
    notes: tuple[str, ...]


def inspect_node_toolchain(root: Path, *, workdir: Path | None = None) -> NodeToolchain:
    """从 workdir 向上找到锁文件 / workspace 根，再选包管理器。"""
    start = (workdir or root).resolve()
    stop = root.resolve()
    workspace_root = _find_workspace_root(start, stop)
    lockfile, manager, install = _lockfile_choice(workspace_root)
    if lockfile is None:
        manager, install = "npm", "npm install"
    workspaces = _has_workspaces(workspace_root)
    dockerfile = _find_dockerfile(stop)
    notes = _toolchain_notes(
        manager=manager,
        install_command=install,
        lockfile=lockfile,
        workspaces=workspaces,
        dockerfile=dockerfile,
        workspace_root=workspace_root,
        project_root=stop,
    )
    return NodeToolchain(
        manager=manager,
        install_command=install,
        lockfile=lockfile,
        workspace_root=workspace_root,
        workspaces=workspaces,
        dockerfile=dockerfile,
        notes=notes,
    )


def install_command_for(root: Path, *, workdir: Path | None = None) -> str:
    return inspect_node_toolchain(root, workdir=workdir).install_command


def describe_import_toolchain(root: Path) -> list[str]:
    """import 输出用：即使 pending 也列出锁文件、bun、Dockerfile。"""
    if not root.is_dir():
        return []
    tool = inspect_node_toolchain(root)
    if tool.lockfile is None and not tool.dockerfile and not (root / "package.json").is_file():
        return []
    return list(tool.notes)


def _find_workspace_root(start: Path, stop: Path) -> Path:
    """从 workdir 走到仓库根，找真正的安装根。

    BUG-933：子包残留的 npm 锁文件不得盖过仓库根的 pnpm/yarn workspace——
    声明了 workspaces（package.json ``workspaces`` / pnpm-workspace.yaml）的
    最外层目录优先。BUG-936：反过来，仓库根**没有**声明 workspaces 时，它的
    锁文件也不能盖过独立子目录自己的锁文件：根 package.json 与 frontend/ 只是
    两个并列的独立项目，根锁文件不管子目录依赖。因此以离 workdir 最近的
    package.json 目录为「独立项目边界」，只接受边界内的锁文件；边界外的
    锁文件一律忽略。都没有则回退仓库根。
    """
    current = start.resolve()
    stop = stop.resolve()
    marked: Path | None = None
    nearest_locked: Path | None = None
    project_dir: Path | None = None
    while True:
        try:
            current.relative_to(stop)
        except ValueError:
            break
        if nearest_locked is None and _lockfile_choice(current)[0] is not None:
            nearest_locked = current
        if project_dir is None and (current / "package.json").is_file():
            project_dir = current
        # 声明 workspace 并不代表拥有每个后代；只提升实际包含的包。
        if _has_workspaces(current) and workspace_contains(current, project_dir or start):
            marked = current
        if current == stop:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    if marked is not None:
        return marked
    if project_dir is not None:
        if nearest_locked is not None and nearest_locked.is_relative_to(project_dir):
            return nearest_locked
        return project_dir
    return nearest_locked or stop


def workspace_contains(root: Path, project: Path) -> bool:
    """包必须匹配 workspace 包含模式，且不匹配任何排除模式。"""
    if root == project:
        return True
    try:
        relative = project.relative_to(root).as_posix()
    except ValueError:
        return False
    try:
        pnpm = root / "pnpm-workspace.yaml"
        if pnpm.is_file():
            data = yaml.safe_load(pnpm.read_text(encoding="utf-8"))
            patterns = data.get("packages", []) if isinstance(data, dict) else []
        else:
            data = json.loads((root / "package.json").read_text(encoding="utf-8"))
            patterns = data.get("workspaces", []) if isinstance(data, dict) else []
            if isinstance(patterns, dict):
                patterns = patterns.get("packages", [])
    except (OSError, ValueError, yaml.YAMLError):
        return False
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        return False
    path = PurePosixPath(relative)

    def matches(pattern: str) -> bool:
        # workspace 常用 {web,api}；PurePath 的 glob 自身不展开大括号。
        alternatives = re.search(r"\{([^{}]*,[^{}]*)\}", pattern)
        if alternatives:
            return any(matches(pattern[:alternatives.start()] + choice
                               + pattern[alternatives.end():])
                       for choice in alternatives.group(1).split(","))
        return path.full_match(pattern)

    included = excluded = False
    for pattern in patterns:
        negative = pattern.startswith("!")
        glob = pattern[1:] if negative else pattern
        # 按目录组件匹配：* 不跨目录，** 可以跨目录；排除规则始终优先。
        if glob and not glob.startswith("/") and ".." not in glob.split("/"):
            if matches(glob.removeprefix("./").rstrip("/")):
                if negative:
                    excluded = True
                else:
                    included = True
    return included and not excluded


def _lockfile_choice(root: Path) -> tuple[str | None, str, str]:
    for name, manager, command in _LOCK_ORDER:
        if (root / name).is_file():
            return name, manager, command
    return None, "npm", "npm install"


def _has_workspaces(root: Path) -> bool:
    if (root / "pnpm-workspace.yaml").is_file():
        return True
    pkg = root / "package.json"
    if not pkg.is_file():
        return False
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    workspaces = data.get("workspaces")
    return bool(workspaces)


def _find_dockerfile(root: Path) -> Path | None:
    for name in ("Dockerfile", "dockerfile", "Dockerfile.prod"):
        path = root / name
        if path.is_file():
            return path
    return None


def _toolchain_notes(
    *,
    manager: str,
    install_command: str,
    lockfile: str | None,
    workspaces: bool,
    dockerfile: Path | None,
    workspace_root: Path,
    project_root: Path,
) -> tuple[str, ...]:
    notes: list[str] = []
    if lockfile:
        extra = ""
        if workspace_root.resolve() != project_root.resolve():
            extra = f"，workspace 根 {workspace_root.name}"
        notes.append(
            f"包管理器：{manager}（锁文件 {lockfile}{extra}）。安装应使用：{install_command}"
        )
    elif (project_root / "package.json").is_file():
        notes.append("未发现 lockfile，安装将使用 npm install")
    if workspaces:
        notes.append(
            "检测到 npm/pnpm/yarn workspace：install 在仓库根执行，"
            "不要在子包单独用 npm 安装 workspace:* 协议"
        )
    if manager == "bun":
        notes.append(
            "检测到 bun.lock/bun.lockb。当前宿主若无 bun，应拒绝并说明工具链缺口，"
            "不要改走静态 HTML 路径后提示检查 Dockerfile"
        )
    if dockerfile is not None:
        notes.append(
            f"源码含 {dockerfile.name}。若识别为 pending/unknown/STATIC，"
            "失败原因是形态/工具链未选容器路径，不是 Dockerfile 语法错误"
        )
    return tuple(notes)


__all__ = [
    "NodeToolchain",
    "describe_import_toolchain",
    "inspect_node_toolchain",
    "install_command_for",
    "workspace_contains",
]
