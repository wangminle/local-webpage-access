"""Node 包管理器与 workspace 上下文（CHK-420 / CHK-423 / CHK-424）。

按锁文件选择 npm/pnpm/yarn/bun，安装命令在 workspace 根执行。pending 路径
也要写出可见提示，避免 bun.lock 或官方 Dockerfile 被静默丢掉。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
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

# BUG-897：packageManager 的版本号来自导入的 package.json（外部内容），会被
# 拼进宿主 ``sh -c`` 命令与 Dockerfile ``RUN``——只接受纯版本字符，含其余
# 字符（空格/引号/;/$ 等）一律按未声明处理，杜绝命令注入。
PM_VERSION_RE = re.compile(r"[0-9A-Za-z._-]+")

# BUG-966：lockfileVersion <9 的旧锁与 corepack 默认拉取的最新 pnpm（10.x，
# 只认 9.x 锁）不兼容（ERR_PNPM_BROKEN_LOCKFILE）；无 packageManager 声明时
# 按锁版本钉到生成该锁的 pnpm 世代末版。注意不能统一钉 8：5.3/5.4 锁在
# pnpm 8 下 frozen 安装报 ERR_PNPM_FROZEN_LOCKFILE_WITH_OUTDATED_LOCKFILE
#（独立验收实测，CHK-485）。
# 映射依据（pnpm 各世代写出的 lockfileVersion）：5.x(≤5.3)→pnpm 5/6，
# 5.4→pnpm 7，6.0→pnpm 8，9.x→pnpm 9+（corepack 当前版可读，保持不钉）。
_PNPM_LOCK_PIN_V5 = "6.35.1"  # lockfileVersion 5.0~5.3 → pnpm 6 末版
_PNPM_LOCK_PIN_V5_4 = "7.33.7"  # lockfileVersion 5.4 → pnpm 7 末版
_PNPM_LOCK_PIN_V6 = "8.15.9"  # lockfileVersion 6.0 → pnpm 8 末版
_PNPM_LOCK_CURRENT_MAJOR = 9  # >=9：corepack 默认 pnpm 可读，保持现状


@dataclass(frozen=True)
class NodeToolchain:
    manager: str
    install_command: str
    lockfile: str | None
    workspace_root: Path
    workspaces: bool
    dockerfile: Path | None
    notes: tuple[str, ...]
    engines_node: str | None = None


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
    engines_node = read_node_engines(workspace_root)
    notes = _toolchain_notes(
        manager=manager,
        install_command=install,
        lockfile=lockfile,
        workspaces=workspaces,
        dockerfile=dockerfile,
        workspace_root=workspace_root,
        project_root=stop,
        engines_node=engines_node,
    )
    return NodeToolchain(
        manager=manager,
        install_command=install,
        lockfile=lockfile,
        workspace_root=workspace_root,
        workspaces=workspaces,
        dockerfile=dockerfile,
        notes=notes,
        engines_node=engines_node,
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
            if manager == "pnpm":
                command = _pnpm_install_command(root)
            return name, manager, command
    return None, "npm", "npm install"


_LOCKFILE_VERSION_RE = re.compile(
    r"^lockfileVersion:\s*[\"']?(\d+)(?:\.(\d+))?", re.MULTILINE
)


def _read_lockfile_version(path: Path) -> tuple[int, int] | None:
    """读锁文件头部的 ``lockfileVersion``（主, 次）版本；缺失/不可读返回 None。"""
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:2048]
    except OSError:
        return None
    m = _LOCKFILE_VERSION_RE.search(head)
    return (int(m.group(1)), int(m.group(2) or 0)) if m else None


def read_declared_package_manager(work_dir: Path) -> tuple[str, str] | None:
    """读取 ``work_dir/package.json`` 的 ``packageManager`` 字段（BUG-877）。

    返回 ``(包管理器名, 版本)``（形如 ``("pnpm", "11.0.1")``）；未声明、
    文件缺失或解析失败返回 None。Corepack 规范允许带哈希校验后缀
    （"pnpm@9.12.0+sha512...."）；CLI 参数 ``<pm>@<version>`` 只接受纯版本号，
    须剥离 ``"+sha..."`` 段（BUG-897 的版本字符白名单同步防注入）。

    宿主（hosting）与推断（本模块）共用此实现，避免两处 packageManager
    解析逻辑漂移（BUG-966）。
    """
    pkg_path = work_dir / "package.json"
    if not pkg_path.is_file():
        return None
    try:
        data = json.loads(pkg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    declared = str(data.get("packageManager") or "").strip()
    # 形如 "pnpm@11.0.1"（name@version）；两段都非空才生效
    if "@" not in declared:
        return None
    name, _, version = declared.rpartition("@")
    name = name.strip()
    version = version.split("+", 1)[0].strip()
    if not name or not version:
        return None
    if not PM_VERSION_RE.fullmatch(version):
        return None
    return name, version


def pnpm_lockfile_fallback_version(root: Path) -> str | None:
    """按 ``root/pnpm-lock.yaml`` 的 ``lockfileVersion`` 返回兜底钉版（BUG-966）。

    5.0~5.3 → pnpm 6 末版（6.35.1）、5.4 → pnpm 7 末版（7.33.7）、6.0 →
    pnpm 8 末版（8.15.9）；lockfileVersion >=9、锁文件不可读或版本未知（<5）
    返回 None（宁保持现状不误钉）。宿主构建段钉版透传复用本映射（评审 P1：
    无 ``packageManager`` 声明时 build 段也须与 install 同版本）。
    """
    lock_ver = _read_lockfile_version(root / "pnpm-lock.yaml")
    if lock_ver is None or lock_ver >= (_PNPM_LOCK_CURRENT_MAJOR, 0):
        return None
    if lock_ver[0] == 6:
        return _PNPM_LOCK_PIN_V6
    if lock_ver >= (5, 4):
        return _PNPM_LOCK_PIN_V5_4
    if lock_ver[0] == 5:
        return _PNPM_LOCK_PIN_V5
    # <5 的版本无可靠映射，不钉
    return None


def _pnpm_install_command(root: Path) -> str:
    """pnpm 安装命令的钉版生成（BUG-966 / BUG-984）。

    声明优先：workspace 根 package.json 声明了 ``packageManager: pnpm@X`` →
    钉 ``pnpm@X``。无声明时锁版本兜底：按 ``pnpm-lock.yaml`` 的
    ``lockfileVersion`` 精确映射生成该锁的 pnpm 世代末版——5.0~5.3 钉
    pnpm 6（6.35.1）、5.4 钉 pnpm 7（7.33.7）、6.0 钉 pnpm 8（8.15.9）；
    统一钉 8 会在 5.3/5.4 锁上报
    ERR_PNPM_FROZEN_LOCKFILE_WITH_OUTDATED_LOCKFILE（CHK-485 实测）。
    lockfileVersion >=9（pnpm 9+，corepack 默认拉最新 pnpm 10.x 只认 9.x
    锁）或锁文件不可读/版本未知（<5）→ 保持现状不钉。

    BUG-984：钉版形式保留 ``corepack enable &&`` 前缀——裸 ``corepack
    pnpm@X install`` 按需执行不生成 pnpm shim，生成容器下游的
    ``pnpm run build`` / CMD 裸 pnpm 会 exit 127；enable 恢复 shim 生成，
    与未钉版形式的镜像内不变量一致（宿主侧改写见 hosting）。
    """
    declared = read_declared_package_manager(root)
    if declared and declared[0].lower() == "pnpm":
        return f"corepack enable && corepack pnpm@{declared[1]} install --frozen-lockfile"
    pinned = pnpm_lockfile_fallback_version(root)
    if pinned:
        return f"corepack enable && corepack pnpm@{pinned} install --frozen-lockfile"
    return "corepack enable && pnpm install --frozen-lockfile"


# ---- BUG-967：engines.node 双向兼容 ------------------------------------------


def read_node_engines(root: Path) -> str | None:
    """读 ``root/package.json`` 的 ``engines.node`` 声明；缺失返回 None。"""
    pkg_path = root / "package.json"
    if not pkg_path.is_file():
        return None
    try:
        data = json.loads(pkg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    engines = data.get("engines")
    if not isinstance(engines, dict):
        return None
    node = engines.get("node")
    if not isinstance(node, str):
        return None
    node = node.strip()
    return node or None


_VERSION_TEXT_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?$")


def parse_node_version(text: str) -> tuple[int, int, int] | None:
    """解析 ``v24.19.0`` / ``24.19.0`` 为版本三元组；无法解析返回 None。"""
    m = _VERSION_TEXT_RE.match(text.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0))


def host_node_version() -> tuple[int, int, int] | None:
    """宿主 ``node --version``（timeout <=5s）；node 缺失或执行失败返回 None。"""
    exe = shutil.which("node")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_node_version((out.stdout or "").strip())


def _parse_partial_version(token: str) -> tuple[int | None, int | None, int | None] | None:
    """解析 range 分量：``22.x.x`` → ``(22, None, None)``；``*`` → 全通配。"""
    token = token.strip()
    if token in ("*", "x", "X"):
        return (None, None, None)
    parts = token.split(".")
    if not 1 <= len(parts) <= 3:
        return None
    nums: list[int | None] = []
    for part in parts:
        part = part.strip()
        if part in ("*", "x", "X"):
            nums.append(None)
        elif part.isdigit():
            nums.append(int(part))
        else:
            return None
    while len(nums) < 3:
        nums.append(None)
    return (nums[0], nums[1], nums[2])


def _caret_bounds(
    ver: tuple[int | None, int | None, int | None],
) -> tuple[tuple[int, int, int], tuple[int, int, int]] | None:
    """``^`` 范围的上下界（npm 语义：锁最左非零段，缺段按 0 补齐后同规则）。

    npm 展开：``^22.12`` := ``>=22.12.0 <23.0.0``（不是 ``<22.13.0``——
    CHK-485 实测误判 ``^22.12`` 对 22.15.0 为 False）；``^0.2`` :=
    ``>=0.2.0 <0.3.0``；``^0.0`` := ``>=0.0.0 <0.1.0``；``^22``/``^0``
    := ``>=22.0.0 <23.0.0`` / ``>=0.0.0 <1.0.0``；0.x 完整版本时只锁到
    下一 minor/patch（``^0.2.3`` := ``<0.3.0``，``^0.0.3`` := ``<0.0.4``）。
    """
    major, minor, patch = ver
    if major is None:
        return ((0, 0, 0), None)  # type: ignore[return-value]
    if minor is None:
        return ((major, 0, 0), (major + 1, 0, 0))
    if patch is None:
        # 缺 patch 段与写全 ``^major.minor.0`` 同义（npm semver）
        if major > 0:
            return ((major, minor, 0), (major + 1, 0, 0))
        return ((major, minor, 0), (0, minor + 1, 0))
    if major > 0:
        return ((major, minor, patch), (major + 1, 0, 0))
    if minor > 0:
        return ((major, minor, patch), (0, minor + 1, 0))
    return ((major, minor, patch), (0, 0, patch + 1))


def _tilde_bounds(
    ver: tuple[int | None, int | None, int | None],
) -> tuple[tuple[int, int, int], tuple[int, int, int]] | None:
    """``~`` 范围的上下界（npm 语义：``~1.2`` := >=1.2.0 <1.3.0）。"""
    major, minor, patch = ver
    if major is None:
        return ((0, 0, 0), None)  # type: ignore[return-value]
    if minor is None:
        return ((major, 0, 0), (major + 1, 0, 0))
    if patch is None:
        return ((major, minor, 0), (major, minor + 1, 0))
    return ((major, minor, patch), (major, minor + 1, 0))


def _comparator_bounds(
    op: str, ver: tuple[int | None, int | None, int | None]
) -> tuple[tuple[int, int, int], tuple[int, int, int]] | None:
    """比较符 ``>= > <= < =`` 的半开区间界（npm 对部分版本的展开规则）。

    带通配/缺段时按 npm 语义展开：``>1.2`` := >=1.3.0，``>1`` := >=2.0.0，
    ``<=1.2`` := <1.3.0，``<=1`` := <2.0.0，``<1.2`` := <1.2.0。
    """
    major, minor, patch = ver
    lower = (major or 0, minor or 0, patch or 0)
    if op in ("=", ""):
        if major is None:
            return ((0, 0, 0), None)  # type: ignore[return-value]
        if minor is None:
            return ((major, 0, 0), (major + 1, 0, 0))
        if patch is None:
            return ((major, minor, 0), (major, minor + 1, 0))
        return (lower, (major, minor, patch + 1))
    if op == ">=":
        return (lower, None)  # type: ignore[return-value]
    if op == ">":
        if minor is None:
            return ((major + 1 if major is not None else 1, 0, 0), None)  # type: ignore[return-value]
        if patch is None:
            return ((major or 0, minor + 1, 0), None)  # type: ignore[return-value]
        return ((major, minor, patch + 1), None)  # type: ignore[return-value]
    if op == "<":
        return ((0, 0, 0), lower)
    if op == "<=":
        if minor is None:
            return ((0, 0, 0), (major + 1 if major is not None else 1, 0, 0))
        if patch is None:
            return ((0, 0, 0), (major or 0, minor + 1, 0))
        return ((0, 0, 0), (major, minor, patch + 1))
    return None


def _interval_contains(
    bounds: tuple[tuple[int, int, int], tuple[int, int, int] | None],
    version: tuple[int, int, int],
) -> bool:
    low, high = bounds
    return version >= low and (high is None or version < high)


def _atom_bounds(
    atom: str,
) -> tuple[tuple[int, int, int], tuple[int, int, int] | None] | None:
    """单个空格分隔 range 分量的半开区间界 ``(low, high)``；无法解析返回 None。

    ``high=None`` 表示无上界。连字符范围（``18.0.0 - 22.x.x``）须整体一个
    原子传入（切分由 ``_RANGE_ATOM_RE`` 保证）。
    """
    atom = atom.strip()
    hyphen = re.match(r"^(.+?)\s+-\s+(.+)$", atom)
    if hyphen:
        left = _parse_partial_version(hyphen.group(1))
        right = _parse_partial_version(hyphen.group(2))
        if left is None or right is None or left[0] is None or right[0] is None:
            return None
        low = (left[0], left[1] or 0, left[2] or 0)
        if right[1] is None:
            high = (right[0] + 1, 0, 0)
        elif right[2] is None:
            high = (right[0], right[1] + 1, 0)
        else:
            high = (right[0], right[1], right[2] + 1)
        return (low, high)
    m = re.match(r"^(\^|~|>=|<=|>|<|=)?\s*([0-9.*xX]+)$", atom)
    if not m:
        return None
    op = m.group(1) or ""
    ver = _parse_partial_version(m.group(2))
    if ver is None:
        return None
    if op == "^":
        return _caret_bounds(ver)
    if op == "~":
        return _tilde_bounds(ver)
    return _comparator_bounds(op, ver)


def _eval_range_atom(atom: str, version: tuple[int, int, int]) -> bool | None:
    """求值单个空格分隔的 range 分量（AND 单元）；无法解析返回 None。"""
    bounds = _atom_bounds(atom)
    if bounds is None:
        return None
    return _interval_contains(bounds, version)


# range 分量切分：连字符范围 ``18.0.0 - 22.x.x`` 含空格，必须整体成一个原子，
# 不能按空格切成三段（BUG-967）。比较符与版本号之间允许空格（``>= 18.0.0``
# 是合法 npm 写法，评审 P2：此前切成 ``[">=", "18.0.0"]`` 后 ``">="`` 单独
# 不可解析，整个 range 被判未知）；尾部 ``\S+`` 兜底捕获残片使解析如实失败。
_RANGE_ATOM_RE = re.compile(r"\S+\s+-\s+\S+|(?:\^|~|>=|<=|>|<|=)\s*[0-9.*xX]+|\S+")


def node_version_in_range(
    range_text: str, version: tuple[int, int, int]
) -> bool | None:
    """求值 ``engines.node`` 的 npm semver 常见子集（BUG-967）。

    支持 ``||`` 分支、连字符范围（``18.0.0 - 22.x.x``）、比较符
    （``>= > <= < =``，可带 ``x``/``*`` 通配）、``^`` / ``~``、裸版本与
    通配（``22.x.x`` 等价 ``>=22.0.0 <23.0.0``），多段空格连接为 AND。
    无法解析的 range 返回 None（宁未知不误判）；不支持预发布号。
    """
    text = range_text.strip()
    if not text:
        return None
    for branch in text.split("||"):
        atoms = _RANGE_ATOM_RE.findall(branch)
        if not atoms:
            return None
        results = [_eval_range_atom(atom, version) for atom in atoms]
        if any(r is None for r in results):
            return None
        if all(results):
            return True
    return False


def _branch_bounds(
    branch: str,
) -> tuple[tuple[int, int, int], tuple[int, int, int] | None] | None:
    """单个 ``||`` 分支（AND 组合）的合并区间界；无法解析返回 None。"""
    atoms = _RANGE_ATOM_RE.findall(branch)
    if not atoms:
        return None
    low = (0, 0, 0)
    high: tuple[int, int, int] | None = None
    for atom in atoms:
        bounds = _atom_bounds(atom)
        if bounds is None:
            return None
        lo, hi = bounds
        low = max(low, lo)
        if hi is not None:
            high = hi if high is None else min(high, hi)
    return (low, high)


def node_range_major_intersection(
    range_text: str, major: int
) -> tuple[tuple[int, int, int], tuple[int, int, int] | None] | None | bool:
    """``engines.node`` range 与 Node major 线 ``[major.0.0, major+1.0.0)`` 的交集。

    返回 ``(low, high)``：线内有效半开区间，``high=None`` 表示交集延伸到
    线尾（该线的 moving tag 无需次版本钉版）；无交集返回 ``False``；range
    无法解析返回 ``None``（宁未知不误判）。多 ``||`` 分支取线内上界最高者。
    用于镜像选择时不以 ``major.0.0`` 代表整条线——``>=22.12.0 <23`` 与
    22 线有交集但 22.0.0 不满足（CHK-485 实测误判）。
    """
    text = range_text.strip()
    if not text:
        return None
    line_start = (major, 0, 0)
    line_end = (major + 1, 0, 0)
    best: tuple[tuple[int, int, int], tuple[int, int, int] | None] | None = None
    for branch in text.split("||"):
        bounds = _branch_bounds(branch)
        if bounds is None:
            return None
        low, high = bounds
        lo = max(low, line_start)
        hi = line_end if high is None else min(high, line_end)
        if lo >= hi:
            continue
        hi_eff = None if hi >= line_end else hi
        if best is None or (best[1] is not None and (hi_eff is None or hi_eff > best[1])):
            best = (lo, hi_eff)
    if best is None:
        return False
    return best


def engines_mismatch_direction(
    range_text: str, host: tuple[int, int, int]
) -> str | None:
    """宿主版本不在范围内时判定方向：项目目标比宿主旧 → ``"过新"``，反之 ``"过旧"``。

    以范围内可满足的邻近版本与宿主比较；方向不可判定返回 None。
    评审 P2 两处修正：先判宿主**同 major 线**——线内更高版本可满足则
    ``"过旧"``（宿主 22.2.0 对 ``^20.19.0 || >=22.12.0`` 是 22 线内过旧，
    不能因 20 线可满足而误报过新），线内更低版本可满足则 ``"过新"``；跨
    major 向上探测须同时验 ``(major, 0, 0)`` 与 ``(major, 99, 99)``——只验
    ``major.0.0`` 会漏 ``^22.12.0``（22.0.0 不满足）对老宿主的过旧判定。
    """
    if node_version_in_range(range_text, host) is not False:
        return None
    if node_version_in_range(range_text, (host[0], 99, 99)):
        return "过旧"
    if node_version_in_range(range_text, (host[0], 0, 0)):
        return "过新"
    for major in range(host[0] - 1, 8, -1):
        if node_version_in_range(range_text, (major, 99, 99)):
            return "过新"
    for major in range(host[0] + 1, 60):
        if node_version_in_range(range_text, (major, 0, 0)) or node_version_in_range(
            range_text, (major, 99, 99)
        ):
            return "过旧"
    return None


def _fmt_version(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


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
    engines_node: str | None = None,
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
    if engines_node:
        note = f"package.json 声明 engines.node：{engines_node}"
        host = host_node_version()
        if host is not None and node_version_in_range(engines_node, host) is False:
            direction = engines_mismatch_direction(engines_node, host) or "不兼容"
            note += (
                f"；宿主 Node {_fmt_version(host)} 不在该范围内（{direction}），"
                "宿主构建将阻断，请改用兼容 Node 或显式设置 nodeEnginesIgnore: true"
                "强制忽略（仅 yarn 1/pnpm 可控）；容器构建按 engines 自动选择镜像"
            )
        notes.append(note)
    return tuple(notes)


__all__ = [
    "NodeToolchain",
    "describe_import_toolchain",
    "engines_mismatch_direction",
    "host_node_version",
    "inspect_node_toolchain",
    "install_command_for",
    "node_range_major_intersection",
    "node_version_in_range",
    "parse_node_version",
    "pnpm_lockfile_fallback_version",
    "read_declared_package_manager",
    "read_node_engines",
    "workspace_contains",
]
