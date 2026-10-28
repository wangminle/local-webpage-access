"""#61（检查部分）：前后端分离布局的前端产物时效性分级检查（DEV-164 / CHK-407）。

只告警、**不自动构建**；与别名资源检查（base 配错）相互独立——本模块只回答
「预构建产物是否可能落后于前端源码」。分级证据（CHK-407 拍板）：

- ``suspected``（疑似陈旧）：同仓布局（前端源目录 + 预构建 ``static/`` 产物
  均在导入树内），前端源码 mtime 晚于产物——只能提示疑似，不用哈希比新旧；
- ``clean``：产物不早于源码；
- ``unknown``（无法判断）：预构建产物存在但前端源码不在导入树内（跨仓关联
  不明），或导入拷贝使两侧 mtime 不可区分；
- ``confirmed``（确证陈旧）：需要**构建时记录的源码指纹**比对——当前部署
  流程尚未记录该指纹，本级别为预留通路，当前不会产出。
  hostBuild/preSync 已支持，但尚未向本检查提供构建期源码指纹。

统一供扫描（DetectionResult.notes）、``lwa rebuild --sync`` 提示与
``lwa doctor``（``frontend_artifacts_staleness`` 检查）共用。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# 前端源目录命名约定（与 dockerfile_templates._frontend_package_dir 一致）
_FRONTEND_DIR_NAMES = ("frontend", "web", "client")
# 预构建产物目录：后端直接伺服的静态产物（非 LWA 构建输出——那在 public/）
_ARTIFACT_DIR_NAME = "static"
_ARTIFACT_ENTRY = "index.html"
# mtime 采样的排除项与大目录上限。遍历时直接剪枝，不进入这些目录。
_SKIP_DIRS = {"node_modules", ".git", ".venv", "venv"}
_MAX_FILES = 2000
# 导入拷贝可能带来的秒级 mtime 抖动容差
_MTIME_TOLERANCE_SEC = 1.0

LEVEL_CLEAN = "clean"
LEVEL_SUSPECTED = "suspected"
LEVEL_UNKNOWN = "unknown"
LEVEL_CONFIRMED = "confirmed"


@dataclass(frozen=True)
class FrontendStaleness:
    """一次时效性检查的分级结论。``level=None`` 表示布局不适用（无预构建产物）。"""

    level: str | None
    message: str
    suggestion: str = ""


def _newest_mtime(root: Path) -> float | None:
    """目录内（排除依赖目录）最新文件的 mtime；空/超限返回 None。

    在目录层级剪掉 ``node_modules`` 等大目录，避免先递归读完再丢弃。
    """
    newest: float | None = None
    count = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
        for name in filenames:
            if name in _SKIP_DIRS:
                continue
            try:
                mtime = (Path(dirpath) / name).stat().st_mtime
            except OSError:
                continue
            if newest is None or mtime > newest:
                newest = mtime
            count += 1
            if count >= _MAX_FILES:
                return None
    return newest


def _find_frontend_dirs(root: Path) -> list[Path]:
    out = []
    for name in _FRONTEND_DIR_NAMES:
        candidate = root / name
        if (candidate / "package.json").is_file():
            out.append(candidate)
    return out


def _find_artifact_dirs(root: Path, frontend_dirs: list[Path]) -> list[Path]:
    """深度 ≤2、名为 static/ 且含 index.html 的预构建产物目录（不在前端目录内）。"""
    out: list[Path] = []
    for path in root.glob(f"*/{_ARTIFACT_DIR_NAME}/{_ARTIFACT_ENTRY}"):
        out.append(path.parent)
    top = root / _ARTIFACT_DIR_NAME / _ARTIFACT_ENTRY
    if top.is_file():
        out.append(top.parent)
    return [
        d
        for d in out
        if not any(fd == d or fd in d.parents for fd in frontend_dirs)
    ]


def check_frontend_artifacts_staleness(current_dir: Path) -> FrontendStaleness:
    """对一份 ``current/`` 源码做前端产物时效性分级检查（只读，不构建）。"""
    frontend_dirs = _find_frontend_dirs(current_dir)
    artifact_dirs = _find_artifact_dirs(current_dir, frontend_dirs)
    if not artifact_dirs:
        return FrontendStaleness(None, "无预构建 static/ 产物，布局不适用")

    if not frontend_dirs:
        return FrontendStaleness(
            LEVEL_UNKNOWN,
            "存在预构建 static/ 产物，但前端源码不在导入树内"
            "（跨仓关联或未随仓分发），无法判定产物新旧",
            "如产物由外部前端仓构建，请在其变更后重新构建并覆盖 static/ 后 "
            "执行 lwa rebuild --sync（issue #61 预构建机制规划中）",
        )

    source_newest: float | None = None
    for fd in frontend_dirs:
        mtime = _newest_mtime(fd)
        if mtime is not None and (source_newest is None or mtime > source_newest):
            source_newest = mtime
    artifact_newest: float | None = None
    for ad in artifact_dirs:
        mtime = _newest_mtime(ad)
        if mtime is not None and (artifact_newest is None or mtime > artifact_newest):
            artifact_newest = mtime

    if source_newest is None or artifact_newest is None:
        return FrontendStaleness(
            LEVEL_UNKNOWN,
            "前端源/产物目录过大或不可读，mtime 采样不完整，无法判定产物新旧",
        )
    if source_newest - artifact_newest > _MTIME_TOLERANCE_SEC:
        return FrontendStaleness(
            LEVEL_SUSPECTED,
            "前端源码比预构建 static/ 产物新——疑似陈旧（mtime 证据，仅提示疑似）",
            "在前端目录重新构建并覆盖 static/ 产物后执行 lwa rebuild --sync；"
            "base 配置问题另见别名资源检查（与本检查无关）",
        )
    if artifact_newest - source_newest > _MTIME_TOLERANCE_SEC:
        return FrontendStaleness(LEVEL_CLEAN, "预构建产物不早于前端源码")
    return FrontendStaleness(
        LEVEL_UNKNOWN,
        "前端源码与产物 mtime 不可区分（导入拷贝可能使时间失真），无法判定",
    )
