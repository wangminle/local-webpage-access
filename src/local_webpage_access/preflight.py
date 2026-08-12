"""Layer 2 静态预检（IMP-058 Gate-A）。

在 ``scanner.detect()`` 之后、manifest 构建之前，对检测结果执行毫秒级
配置可行性检查，自动修正可修正的问题，淘汰不可行的候选。

预检项：

- **CHK-V01**：COPY 源路径文件存在性（DEV-105）。验证 requirements 文件
  在构建上下文中存在；若在常见子目录（backend/server/api...）下找到且
  ``source_subdir`` 未设置，自动修正。
- **CHK-V02**：CMD shell 操作符安全（BUG-471）。``entry.start`` 含
  ``&&``/``||``/``;``/``$()``/```` ` `` ``/裸 ``()`` 时，若未以 ``sh -c`` 开头则
  自动包裹 ``sh -c '<原命令>'``。
- **CHK-V03**：数据库路径与 volume 一致性（BUG-474）。SQLite 项目使用
  相对 DB 路径时，标记风险；修正已由 ``compose.generate_env`` 的绝对路径
  ``DATABASE_URL`` 注入完成（A.02），此处仅记录修正事件。注入保留 scanner
  扫描到的源文件名（``DatabaseConfig.dbFilename``），避免硬编码 ``app.sqlite``
  把应用指向全新空库。
- **CHK-V04**：alembic ``script_location`` 可达性（BUG-474）。解析 alembic.ini
  的 ``script_location``，验证相对路径指向的迁移脚本目录存在；alembic.ini
  在子目录且 ``entry.start`` 含 ``alembic`` 时，在启动命令前加
  ``cd <subdir> &&``，alembic 后加 ``cd /app &&``。
- **CHK-V05**：entrypoint 脚本 COPY 完整性（home-bookshelf L3）。``entry.start``
  引用 ``.sh`` 脚本时，验证脚本在项目目录中存在（Dockerfile COPY 范围内）；
  不存在则 rejected。
- **CHK-V06**：项目自带 Dockerfile 检测（BUG-472）。仅警告，不自动修正。
"""

from __future__ import annotations

import configparser
import re
from dataclasses import dataclass, field
from pathlib import Path

from typing import TYPE_CHECKING

from local_webpage_access.dockerfile_templates import (
    _extract_requirements_file,
    _has_shell_operators,
)

if TYPE_CHECKING:
    from local_webpage_access.scanner import DetectionResult

# ---- 数据结构 ----------------------------------------------------------------

# 预检状态枚举
PASSED = "passed"
AUTOFIXED = "autofixed"
WARNED = "warned"
REJECTED = "rejected"


@dataclass
class CheckResult:
    """单项预检结果。"""

    check_id: str  # "CHK-V02" ~ "CHK-V06"
    passed: bool  # 检查是否通过（修正后）
    autofixed: bool  # 是否做了自动修正
    action: str | None  # 修正动作描述（如 "wrapped CMD with sh -c"）
    detail: str  # 详细信息


@dataclass
class PreflightResult:
    """Layer 2 输出：预检结果。"""

    status: str = PASSED  # "passed" / "autofixed" / "warned" / "rejected"
    checks: list[CheckResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # 供 DetectionResult.notes 追加


# ---- 预检实现 ----------------------------------------------------------------

# alembic 命令前缀检测：匹配 ``alembic upgrade head`` 或 ``alembic <sub>``
_ALEMBIC_CMD_RE = re.compile(r"\balembic\b")

# 子目录中的 alembic.ini 搜索深度
_ALEMBIC_SEARCH_DEPTH = 2


def _find_alembic_ini(source_dir: Path) -> Path | None:
    """在 source_dir 顶层和浅层子目录中查找 alembic.ini。

    返回相对于 ``source_dir`` 的路径。仅查找一级子目录（如 ``backend/``），
    避免深层遍历。
    """
    # 顶层
    top = source_dir / "alembic.ini"
    if top.is_file():
        return top

    # 一级子目录
    try:
        for entry in source_dir.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                sub = entry / "alembic.ini"
                if sub.is_file():
                    return sub
    except (PermissionError, OSError):
        pass
    return None


def _find_project_dockerfile(source_dir: Path) -> Path | None:
    """检测项目自带 Dockerfile（顶层或一级子目录）。"""
    top = source_dir / "Dockerfile"
    if top.is_file():
        return top
    try:
        for entry in source_dir.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                sub = entry / "Dockerfile"
                if sub.is_file():
                    return sub
    except (PermissionError, OSError):
        pass
    return None


# CHK-V01 子目录探测范围（与 evidence_collector.COMMON_SUBDIRS 对齐）。
_V01_COMMON_SUBDIRS = ("backend", "server", "api", "app", "frontend", "client", "web", "ui")

# CHK-V05：常见 entrypoint 脚本名（文档 §6.4）。
_ENTRYPOINT_SCRIPT_NAMES = (
    "docker-entrypoint.sh",
    "entrypoint.sh",
    "start.sh",
)
# 从 entry.start 提取脚本引用名的正则：匹配 .sh 结尾的 token。
_ENTRYPOINT_REF_RE = re.compile(r"([\w./-]+\.sh)\b")


def _check_copy_source(result: DetectionResult, source_dir: Path) -> CheckResult:
    """CHK-V01：COPY 源路径文件存在性（文档 §6.4）。

    验证 Dockerfile COPY 上下文中 requirements 文件存在。若 ``source_subdir``
    为 None 但文件在常见子目录下，自动修正 ``source_subdir``。

    仅对有 ``entry.install``（含 ``pip install -r``）的 Python 候选检查；
    其他类型（static/node）无 requirements COPY，跳过。
    """
    install = result.entry.install
    if not install or "pip install" not in install:
        return CheckResult(
            check_id="CHK-V01",
            passed=True,
            autofixed=False,
            action=None,
            detail="非 pip install 候选，跳过 COPY 源路径检查",
        )

    req_file = _extract_requirements_file(install)
    source_subdir = result.source_subdir

    # 已有 source_subdir：检查 source_dir/subdir/req_file
    if source_subdir:
        target = source_dir / source_subdir / req_file
        if target.is_file():
            return CheckResult(
                check_id="CHK-V01",
                passed=True,
                autofixed=False,
                action=None,
                detail=f"COPY 源路径 {source_subdir}/{req_file} 存在",
            )
        return CheckResult(
            check_id="CHK-V01",
            passed=False,
            autofixed=False,
            action=None,
            detail=f"COPY 源路径 {source_subdir}/{req_file} 不存在，build 将失败",
        )

    # 无 source_subdir：先检查根目录
    root_target = source_dir / req_file
    if root_target.is_file():
        return CheckResult(
            check_id="CHK-V01",
            passed=True,
            autofixed=False,
            action=None,
            detail=f"COPY 源路径 {req_file} 在根目录存在",
        )

    # 根目录没有：检查常见子目录，自动修正 source_subdir
    for subdir_name in _V01_COMMON_SUBDIRS:
        candidate = source_dir / subdir_name / req_file
        if candidate.is_file():
            result.source_subdir = subdir_name
            return CheckResult(
                check_id="CHK-V01",
                passed=True,
                autofixed=True,
                action=f"修正 source_subdir={subdir_name!r}（{req_file} 在子目录 {subdir_name}/ 下）",
                detail=f"COPY 源路径 {req_file} 在根目录不存在，但在 {subdir_name}/ 下找到，已自动修正 source_subdir",
            )

    # 根目录和常见子目录都没有：拒绝
    return CheckResult(
        check_id="CHK-V01",
        passed=False,
        autofixed=False,
        action=None,
        detail=(
            f"COPY 源路径 {req_file} 在根目录和常见子目录下均不存在，build 将失败"
        ),
    )


def _find_entrypoint_script(source_dir: Path, script_name: str) -> Path | None:
    """在 source_dir 顶层和一级子目录中查找 entrypoint 脚本。"""
    # 顶层
    top = source_dir / script_name
    if top.is_file():
        return top
    # 一级子目录
    try:
        for entry in source_dir.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                sub = entry / script_name
                if sub.is_file():
                    return sub
    except (PermissionError, OSError):
        pass
    return None


def _check_entrypoint_scripts(result: DetectionResult, source_dir: Path) -> CheckResult:
    """CHK-V05：entrypoint 脚本 COPY 完整性（文档 §6.4）。

    若 ``entry.start`` 引用了 ``.sh`` 脚本（如 ``docker-entrypoint.sh``），
    验证该脚本在项目目录中存在。Dockerfile 模板的 ``COPY current/ ./`` 会
    包含子目录内容，因此只要脚本在 source_dir 下即可被 COPY。

    - 脚本存在 → 通过
    - 脚本不存在但被引用 → rejected（build 会失败或运行时缺文件）
    - start 不引用 .sh 脚本 → 跳过
    """
    start = result.entry.start
    if not start:
        return CheckResult(
            check_id="CHK-V05",
            passed=True,
            autofixed=False,
            action=None,
            detail="entry.start 为空，跳过 entrypoint 脚本检查",
        )

    # 从 start 提取 .sh 脚本引用
    refs = _ENTRYPOINT_REF_RE.findall(start)
    if not refs:
        return CheckResult(
            check_id="CHK-V05",
            passed=True,
            autofixed=False,
            action=None,
            detail="启动命令未引用 .sh 脚本，跳过检查",
        )

    # 检查每个引用的脚本是否存在
    # 注意：sh -c 包裹后 start 可能含 cd/subshell，提取的 .sh 可能是路径的一部分
    missing = []
    found = []
    for ref in refs:
        # 取 basename（如 ./backend/entrypoint.sh → entrypoint.sh）
        script_name = Path(ref).name
        if script_name.startswith(".") or len(script_name) < 4:
            continue  # 跳过 .sh 之类的无效名
        path = _find_entrypoint_script(source_dir, script_name)
        if path is not None:
            found.append(script_name)
        else:
            missing.append(script_name)

    if missing:
        return CheckResult(
            check_id="CHK-V05",
            passed=False,
            autofixed=False,
            action=None,
            detail=(
                f"entry.start 引用的 entrypoint 脚本在项目中不存在：{', '.join(missing)}，"
                f"容器运行时将找不到该脚本"
            ),
        )

    if found:
        return CheckResult(
            check_id="CHK-V05",
            passed=True,
            autofixed=False,
            action=None,
            detail=f"entry.start 引用的 entrypoint 脚本存在：{', '.join(found)}",
        )

    # refs 非空但都被跳过（无效名）
    return CheckResult(
        check_id="CHK-V05",
        passed=True,
        autofixed=False,
        action=None,
        detail="启动命令引用的 .sh token 无有效脚本名，跳过检查",
    )


def _is_sh_c_wrapped(cmd: str) -> bool:
    """命令是否已经是 ``sh -c "..."`` 形式。"""
    stripped = cmd.strip()
    return stripped.startswith("sh -c ") or stripped.startswith("sh -c'")


def _check_cmd_safety(result: DetectionResult) -> CheckResult:
    """CHK-V02：CMD shell 操作符安全。

    若 ``entry.start`` 含 shell 操作符且未以 ``sh -c`` 开头，自动包裹。
    """
    start = result.entry.start
    if not start:
        return CheckResult(
            check_id="CHK-V02",
            passed=True,
            autofixed=False,
            action=None,
            detail="entry.start 为空，跳过检查",
        )

    if not _has_shell_operators(start):
        return CheckResult(
            check_id="CHK-V02",
            passed=True,
            autofixed=False,
            action=None,
            detail="启动命令不含 shell 操作符",
        )

    if _is_sh_c_wrapped(start):
        return CheckResult(
            check_id="CHK-V02",
            passed=True,
            autofixed=False,
            action=None,
            detail="启动命令已用 sh -c 包裹",
        )

    # 自动修正：包裹 sh -c
    new_start = f"sh -c '{start}'"
    result.entry.start = new_start
    return CheckResult(
        check_id="CHK-V02",
        passed=True,
        autofixed=True,
        action=f"包裹 sh -c：{start} -> {new_start}",
        detail="启动命令含 shell 操作符（&&/||/;/$()/`），已自动包裹 sh -c",
    )


def _check_db_path(result: DetectionResult) -> CheckResult:
    """CHK-V03：数据库路径与 volume 一致性（A.R01 安全自动修正）。

    SQLite 项目使用相对 DB 路径时标记风险。修正已由 ``compose.generate_env``
    的绝对路径 ``DATABASE_URL`` 注入完成（A.02），此处仅记录修正事件。

    A.R01 修订：只有当证据表明应用消费 ``DATABASE_URL`` 环境变量时，
    才标记为 autofixed；否则降级为 warned，compose 不自动注入。
    保留 scanner 扫描到的源文件名（``DatabaseConfig.dbFilename``），
    避免原硬编码 ``app.sqlite`` 把应用指向全新空库。
    """
    if not result.hasDatabase or not result.database or result.database.type != "sqlite":
        return CheckResult(
            check_id="CHK-V03",
            passed=True,
            autofixed=False,
            action=None,
            detail="非 SQLite 项目，跳过检查",
        )

    # 解析注入将使用的文件名：优先源文件名，否则默认兜底
    db_filename = (
        result.database.dbFilename
        if result.database.dbFilename
        else "app.sqlite"
    )

    # A.R01：检查应用是否消费 DATABASE_URL
    db_signal = None
    if result.evidence and result.evidence.databaseConfig:
        db_signal = result.evidence.databaseConfig

    conn = result.database.connectionString
    is_relative = conn and _is_relative_sqlite_path(conn)

    if db_signal and db_signal.consumesDatabaseUrl:
        # 应用确认消费 DATABASE_URL，安全自动注入
        detail_parts = [
            f"应用在 {db_signal.sourcePath or '源码'} 中读取 DATABASE_URL",
            f"注入绝对路径 DATABASE_URL=sqlite:////app/data/{db_filename}",
        ]
        if is_relative:
            detail_parts.append(f"检测到相对路径 SQLite 连接串（{conn}）")
        if db_signal.defaultUrl:
            detail_parts.append(f"默认连接串：{db_signal.defaultUrl}")

        return CheckResult(
            check_id="CHK-V03",
            passed=True,
            autofixed=True,
            action=f"compose.generate_env 已注入绝对路径 DATABASE_URL=sqlite:////app/data/{db_filename}",
            detail="；".join(detail_parts),
        )
    else:
        # A.R01：无消费证据，不自动注入，降级为 warning
        reason = "未在源码中找到读取 DATABASE_URL 的证据"
        if db_signal is None:
            reason = "未采集到数据库配置信号"
        elif not db_signal.consumesDatabaseUrl:
            reason = f"应用在 {db_signal.sourcePath or '已扫描文件'} 中未读取 DATABASE_URL 环境变量"

        return CheckResult(
            check_id="CHK-V03",
            passed=True,
            autofixed=False,
            action=None,
            detail=(
                f"⚠️ A.R01 安全检查：{reason}。"
                f"compose 将不自动注入 DATABASE_URL，保留应用原配置。"
                f"如需注入，请确认应用 config 中使用 os.getenv('DATABASE_URL')。"
            ),
        )


def _is_relative_sqlite_path(conn: str) -> bool:
    """判断 SQLite 连接串是否使用相对路径。

    绝对路径形式：``sqlite:////app/data/foo.db``（四个斜杠）。
    相对路径形式：``sqlite:///./data/foo.db``、``sqlite:///data/foo.db``。
    """
    # sqlite:/// 后面不是 / 开头即为相对路径
    # sqlite:////  是绝对路径
    if conn.startswith("sqlite:////"):
        return False
    if conn.startswith("sqlite:///"):
        return True
    return False


def _parse_alembic_script_location(alembic_ini: Path) -> str | None:
    """解析 alembic.ini 的 ``[alembic] script_location`` 值。

    返回 None 表示无法解析（文件缺失、格式错误或无该键）。
    """
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(alembic_ini, encoding="utf-8")
    except (configparser.Error, OSError, UnicodeDecodeError):
        return None
    if parser.has_option("alembic", "script_location"):
        return parser.get("alembic", "script_location").strip()
    return None


def _check_alembic_cwd(result: DetectionResult, source_dir: Path) -> CheckResult:
    """CHK-V04：alembic script_location 可达性。

    IMP-058 Gate-A CHK-V04（文档 §6.4）：解析 alembic.ini 的 ``script_location``，
    验证相对路径指向的迁移脚本目录是否存在。同时，若 alembic.ini 在子目录且
    ``entry.start`` 含 ``alembic``，在启动命令中编排 cwd 序列：
    ``cd <subdir> && <alembic ...> && cd /app && <原命令后续>``。

    若 start 已被 CHK-V02 包裹为 ``sh -c '...'``，则修改 sh -c 内的命令。
    """
    alembic_ini = _find_alembic_ini(source_dir)
    if alembic_ini is None:
        return CheckResult(
            check_id="CHK-V04",
            passed=True,
            autofixed=False,
            action=None,
            detail="未找到 alembic.ini，跳过检查",
        )

    # 判断 alembic.ini 是否在子目录
    try:
        rel = alembic_ini.relative_to(source_dir)
    except ValueError:
        return CheckResult(
            check_id="CHK-V04",
            passed=True,
            autofixed=False,
            action=None,
            detail="alembic.ini 不在项目目录内，跳过检查",
        )

    in_subdir = len(rel.parts) > 1
    subdir = rel.parts[0] if in_subdir else None  # 如 "backend"

    # 解析 script_location，验证相对路径可达性（文档 §6.4 CHK-V04 检查逻辑）
    script_location = _parse_alembic_script_location(alembic_ini)
    script_location_missing = False
    if script_location is not None:
        # script_location 相对 alembic.ini 所在目录解析（alembic 的默认行为）
        alembic_dir = alembic_ini.parent
        if not script_location.startswith("/"):
            # 相对路径：验证目录是否存在
            target = alembic_dir / script_location
            if not target.is_dir():
                script_location_missing = True

    # 场景 1：alembic.ini 在顶层、script_location 可达 → 无需修正
    if not in_subdir and not script_location_missing:
        return CheckResult(
            check_id="CHK-V04",
            passed=True,
            autofixed=False,
            action=None,
            detail="alembic.ini 在项目根目录，script_location 可达，cwd 一致",
        )

    # 场景 2：alembic.ini 在顶层但 script_location 指向不存在的目录 → 标记风险
    # （Gate-A 不自动修正路径配置，只 warning；不淘汰候选）
    if not in_subdir and script_location_missing:
        return CheckResult(
            check_id="CHK-V04",
            passed=True,
            autofixed=False,
            action=None,
            detail=(
                f"⚠️ alembic.ini 的 script_location={script_location!r} 指向的目录"
                f"在 {alembic_ini.parent.relative_to(source_dir) or '.'} 下不存在，"
                f"alembic upgrade head 可能失败"
            ),
        )

    # 场景 3：alembic.ini 在子目录 → 需要 cwd 编排（若 start 含 alembic）
    # 此时无论 script_location 是否可达，都需要确保 alembic 在正确 cwd 执行
    assert subdir is not None
    start = result.entry.start
    if not start or not _ALEMBIC_CMD_RE.search(start):
        # start 不含 alembic：若 script_location 也缺失，仍标记风险
        detail = f"alembic.ini 在子目录 {subdir}/ 但启动命令不含 alembic"
        if script_location_missing:
            detail += f"；且 script_location={script_location!r} 目录不存在"
        return CheckResult(
            check_id="CHK-V04",
            passed=True,
            autofixed=False,
            action=None,
            detail=detail,
        )
    # CHK-193/P1：当 sourceSubdir 匹配 alembic.ini 所在子目录时，
    # Dockerfile 使用 COPY current/<subdir>/ ./ 将子目录内容扁平复制到 /app/。
    # 容器内 alembic.ini 位于 /app/alembic.ini，alembic 可直接从 /app/ 执行，
    # 不需要 cd <subdir>。注入 cd 会导致容器内找不到 /app/<subdir> 而启动失败。
    manifest_subdir = getattr(result, "source_subdir", None)
    if manifest_subdir and manifest_subdir == subdir:
        detail = (
            f"alembic.ini 在子目录 {subdir}/，Dockerfile 扁平复制到 /app/，"
            f"无需 cwd 编排"
        )
        if script_location_missing:
            detail += f"；注意 script_location={script_location!r} 目录不存在，迁移可能仍会失败"
        return CheckResult(
            check_id="CHK-V04",
            passed=True,
            autofixed=False,
            action=None,
            detail=detail,
        )

    # 检查 start 是否已被 sh -c 包裹
    if _is_sh_c_wrapped(start):
        # 修改 sh -c 内的命令
        # 提取 sh -c '...' 或 sh -c "..." 中的内容
        inner = _extract_sh_c_inner(start)
        if inner is None:
            return CheckResult(
                check_id="CHK-V04",
                passed=True,
                autofixed=False,
                action=None,
                detail="无法解析 sh -c 内部命令，跳过 alembic cwd 编排",
            )
        new_inner = _inject_cwd_sequence(inner, subdir)
        result.entry.start = f"sh -c '{new_inner}'"
    else:
        # 直接编排
        new_start = _inject_cwd_sequence(start, subdir)
        result.entry.start = f"sh -c '{new_start}'"

    detail = f"alembic.ini 在子目录 {subdir}/，已在启动命令中编排 cwd 序列"
    if script_location_missing:
        detail += f"；注意 script_location={script_location!r} 目录不存在，迁移可能仍会失败"

    return CheckResult(
        check_id="CHK-V04",
        passed=True,
        autofixed=True,
        action=f"编排 cwd 序列：cd {subdir} && ... && cd /app && ...",
        detail=detail,
    )


def _extract_sh_c_inner(cmd: str) -> str | None:
    """从 ``sh -c '...'`` 或 ``sh -c "..."`` 中提取内部命令。"""
    stripped = cmd.strip()
    # sh -c '...'
    if stripped.startswith("sh -c '") and stripped.endswith("'"):
        return stripped[7:-1]
    # sh -c "..."
    if stripped.startswith('sh -c "') and stripped.endswith('"'):
        return stripped[7:-1]
    return None


def _inject_cwd_sequence(cmd: str, subdir: str) -> str:
    """在 alembic 命令前加 ``cd <subdir> &&``，alembic 后加 ``cd /app &&``。

    若命令含 ``&&``（如 ``alembic upgrade head && exec uvicorn ...``），
    在第一个 ``&&`` 前加 ``cd <subdir> &&``，在第一个 ``&&`` 后加 ``cd /app &&``。
    """
    # 找到 alembic ... && 的位置
    match = re.search(r"(alembic\s+\S+(?:\s+\S+)*)\s*&&", cmd)
    if match:
        before = cmd[: match.start()]
        alembic_part = match.group(1)
        after = cmd[match.end():]
        return f"{before}cd {subdir} && {alembic_part} && cd /app &&{after}"

    # 整个命令就是 alembic（无 && 后续）
    if _ALEMBIC_CMD_RE.search(cmd):
        return f"cd {subdir} && {cmd}"

    return cmd


def _check_project_dockerfile(result: DetectionResult, source_dir: Path) -> CheckResult:
    """CHK-V06：项目自带 Dockerfile 检测。

    检测到项目自带 Dockerfile 时标记 warning，不自动修正。
    """
    dockerfile = _find_project_dockerfile(source_dir)
    if dockerfile is None:
        return CheckResult(
            check_id="CHK-V06",
            passed=True,
            autofixed=False,
            action=None,
            detail="无项目自带 Dockerfile",
        )

    try:
        rel = dockerfile.relative_to(source_dir)
        rel_str = str(rel).replace("\\", "/")
    except ValueError:
        rel_str = str(dockerfile)

    return CheckResult(
        check_id="CHK-V06",
        passed=True,
        autofixed=False,
        action=None,
        detail=f"检测到项目自带 Dockerfile（{rel_str}），lwa 将生成自己的 Dockerfile",
    )


# ---- 主入口 ------------------------------------------------------------------


def check_and_fix(result: DetectionResult, source_dir: Path) -> PreflightResult:
    """对检测结果执行 Layer 2 静态预检。

    会就地修正 ``result``（如包裹 ``sh -c``、编排 cwd），返回预检结果摘要。

    Parameters
    ----------
    result
        ``scanner.detect()`` 的输出。
    source_dir
        项目源码目录（``current/``）。

    Returns
    -------
    PreflightResult
        预检结果，包含每项检查的详情、修正事件和警告。
    """
    preflight = PreflightResult()

    # pending 结果不执行预检（无可修正的候选）
    if result.pending:
        preflight.status = PASSED
        preflight.checks.append(
            CheckResult(
                check_id="SKIP",
                passed=True,
                autofixed=False,
                action=None,
                detail="检测结果为 pending，跳过预检",
            )
        )
        return preflight

    # CHK-V01：COPY 源路径文件存在性
    r_v01 = _check_copy_source(result, source_dir)
    preflight.checks.append(r_v01)

    # CHK-V02：CMD shell 操作符安全
    r_v02 = _check_cmd_safety(result)
    preflight.checks.append(r_v02)

    # CHK-V03：数据库路径与 volume 一致性
    r_v03 = _check_db_path(result)
    preflight.checks.append(r_v03)

    # CHK-V04：alembic script_location 可达性
    r_v04 = _check_alembic_cwd(result, source_dir)
    preflight.checks.append(r_v04)

    # CHK-V05：entrypoint 脚本 COPY 完整性
    r_v05 = _check_entrypoint_scripts(result, source_dir)
    preflight.checks.append(r_v05)

    # CHK-V06：项目自带 Dockerfile 检测
    r_v06 = _check_project_dockerfile(result, source_dir)
    preflight.checks.append(r_v06)

    # 汇总状态
    has_autofix = any(c.autofixed for c in preflight.checks)
    has_warning = r_v06.passed and not r_v06.autofixed and r_v06.detail.startswith("检测到项目自带 Dockerfile")
    has_rejection = any(not c.passed for c in preflight.checks)

    if has_rejection:
        preflight.status = REJECTED
        preflight.rejections.extend(
            c.detail for c in preflight.checks if not c.passed
        )
    elif has_autofix:
        preflight.status = AUTOFIXED
        preflight.notes.extend(
            f"[预检修正] {c.check_id}: {c.action}"
            for c in preflight.checks
            if c.autofixed and c.action
        )
    elif has_warning:
        preflight.status = WARNED
    else:
        preflight.status = PASSED

    # CHK-V06 的 warning 始终收集
    if r_v06.detail.startswith("检测到项目自带 Dockerfile"):
        preflight.warnings.append(r_v06.detail)

    # CHK-V03 的 A.R01 安全警告（无消费证据时不自动注入）
    if r_v03.detail.startswith("⚠️"):
        preflight.warnings.append(r_v03.detail)
        if preflight.status == PASSED:
            preflight.status = WARNED

    # CHK-V04 的 script_location 风险也收集为 warning（含 ⚠️ 前缀的 detail）
    for check in preflight.checks:
        if check.check_id == "CHK-V04" and check.detail.startswith("⚠️"):
            preflight.warnings.append(check.detail)
            # 若无其他 warning/autofix，把状态提升为 WARNED
            if preflight.status == PASSED:
                preflight.status = WARNED

    return preflight


__all__ = [
    "CheckResult",
    "PreflightResult",
    "check_and_fix",
    "PASSED",
    "AUTOFIXED",
    "WARNED",
    "REJECTED",
]
