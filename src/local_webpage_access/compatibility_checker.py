"""兼容性预检（IMP-056 Gate-2）。

在导入后对源码进行静态扫描，提前告知路径别名兼容性风险。
**不阻断** import/start/alias；IMP-055 仍是唯一 enforce 真源。

MVP 规则：
  CHK-P03  SSR/服务端模板中绝对 API 路径、``const API = ''`` -> critical（展示用）
  CHK-P04  源码未见 base path / 代理前缀线索 -> warning

扫描范围：主包（或非 monorepo 的 source_dir）下 .ts/.js/.mjs/.cjs/.py。
排除 node_modules / dist / build / .git 等噪音目录。
"""

from __future__ import annotations

import re
from pathlib import Path

from local_webpage_access.logging import get_logger
from local_webpage_access.models import CompatibilityFinding

log = get_logger("compatibility_checker")

# 排除目录
_EXCLUDE_DIRS = {
    "node_modules", ".git", ".venv", "venv", "__pycache__",
    "dist", "build", ".next", ".svelte-kit", "out", ".output",
    "tests", "test", "__tests__", "spec", ".spec", "e2e",
    "packages/desktop",  # electron 子包
}

# 扫描的文件扩展名
_SCAN_EXTENSIONS = (".ts", ".js", ".mjs", ".cjs", ".py")

# CHK-P03 正则：fetch('/api/...'), axios.get('/api/...'), const API = '' 等
_P03_FETCH_API_RE = re.compile(
    r"""(?:fetch|axios\.\w+)\s*\(\s*['"`]/api/""",
    re.MULTILINE,
)
_P03_EMPTY_API_BASE_RE = re.compile(
    r"""(?:const|let|var)\s+(?:API|apiBase|API_BASE|api_base)\s*=\s*['"`]\s*['"`]""",
    re.MULTILINE,
)

# CHK-P04 关键字（大小写不敏感）
_P04_KEYWORDS = (
    "BASE_PATH",
    "BASE_URL",
    "X-Forwarded-Prefix",
    "x-forwarded-prefix",
    "SCRIPT_NAME",
    "basePath",
    "baseUrl",
)

# 扫描文件数上限（成本控制）
_MAX_FILES = 500


def check_compatibility(
    source_dir: Path,
    *,
    primary_subdir: str | None = None,
) -> list[CompatibilityFinding]:
    """对源码执行兼容性预检（CHK-P03/P04）。

    Args:
        source_dir: 项目根目录（``current/``）。
        primary_subdir: monorepo 主包子目录路径（如 ``packages/webpage``）。
            若非 None，扫描范围限定在该子目录；否则扫描整个 source_dir。

    Returns:
        兼容性发现列表（可能为空）。
    """
    scan_root = source_dir / primary_subdir if primary_subdir else source_dir
    if not scan_root.is_dir():
        return []

    findings: list[CompatibilityFinding] = []
    files_scanned = 0
    has_base_path_keyword = False

    for path in _walk_source(scan_root):
        if files_scanned >= _MAX_FILES:
            log.warning("兼容性预检扫描文件数达到上限 %d，跳过剩余文件", _MAX_FILES)
            break

        try:
            content = path.read_text("utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        files_scanned += 1
        rel_path = str(path.relative_to(source_dir)).replace("\\", "/")

        # CHK-P03: 绝对 API 路径
        for match in _P03_FETCH_API_RE.finditer(content):
            line_num = content[:match.start()].count("\n") + 1
            line_content = _get_line(content, line_num)
            findings.append(CompatibilityFinding(
                checkId="CHK-P03",
                severity="critical",
                title="SSR/服务端模板绝对 API 路径",
                file=rel_path,
                line=line_num,
                code=line_content.strip()[:200] if line_content else None,
                impact="路径别名下去除前缀后，fetch('/api/...') 会打到入口根而非后端 API",
                fix="支持 BASE_PATH（或等价），注入客户端常量，并在路由匹配前剥前缀",
            ))

        # CHK-P03: 空 API base
        for match in _P03_EMPTY_API_BASE_RE.finditer(content):
            line_num = content[:match.start()].count("\n") + 1
            line_content = _get_line(content, line_num)
            findings.append(CompatibilityFinding(
                checkId="CHK-P03",
                severity="critical",
                title="空 API base 常量",
                file=rel_path,
                line=line_num,
                code=line_content.strip()[:200] if line_content else None,
                impact="API base 为空字符串意味着所有请求走绝对根路径，别名下会失效",
                fix="将 API base 设为可配置的 BASE_PATH 或环境变量",
            ))

        # CHK-P04: 检测 base path 关键字
        if not has_base_path_keyword:
            for kw in _P04_KEYWORDS:
                if kw in content:
                    has_base_path_keyword = True
                    break

    # CHK-P04: 若未检出任何 base path 关键字 -> warning
    if not has_base_path_keyword and files_scanned > 0:
        findings.append(CompatibilityFinding(
            checkId="CHK-P04",
            severity="warning",
            title="未检出常见 base path / 代理前缀关键字",
            file=None,
            line=None,
            code=None,
            impact="路径别名时应用可能无法正确处理前缀",
            fix="引入 BASE_PATH / BASE_URL / X-Forwarded-Prefix 等机制；"
                "此为启发式：未检出常见关键字 ≠ 一定不兼容；"
                "设别名时仍以 IMP-055 运行时探测为准",
        ))

    return findings


# ---- 内部辅助 ----------------------------------------------------------------


def _walk_source(root: Path):
    """遍历源码文件，跳过排除目录。"""
    skip = _EXCLUDE_DIRS
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            for entry in sorted(current.iterdir(), key=lambda p: p.name):
                if entry.is_dir():
                    rel = str(entry.relative_to(root)).replace("\\", "/")
                    if rel.lower() not in skip and not any(
                        rel.lower().startswith(s + "/") for s in skip
                    ):
                        stack.append(entry)
                elif entry.is_file() and entry.suffix.lower() in _SCAN_EXTENSIONS:
                    yield entry
        except (PermissionError, OSError):
            continue


def _get_line(content: str, line_num: int) -> str | None:
    """获取指定行号的内容。"""
    lines = content.splitlines()
    if 1 <= line_num <= len(lines):
        return lines[line_num - 1]
    return None
