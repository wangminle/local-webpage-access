"""应用版本解析：优先从 Git 最新 commit 主题读取 ``V0.6.3-Build...`` 前缀。"""

from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version as pkg_version
from pathlib import Path

_VERSION_PREFIX = re.compile(r"^V(\d+\.\d+\.\d+)", re.IGNORECASE)
_PACKAGE_NAME = "local-webpage-access"
_FALLBACK_VERSION = "0.6.3"


def _repo_root() -> Path | None:
    """editable 安装时定位仓库根（``src/local_webpage_access`` 的上两级）。"""
    here = Path(__file__).resolve().parent
    candidate = here.parent.parent
    if (candidate / "pyproject.toml").is_file():
        return candidate
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode == 0:
            root = Path(result.stdout.strip())
            if root.is_dir():
                return root
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _version_from_git(root: Path | None) -> str | None:
    if root is None:
        return None
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            capture_output=True,
            text=True,
            timeout=2,
            cwd=root,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    subject = (result.stdout or "").strip()
    match = _VERSION_PREFIX.match(subject)
    if not match:
        return None
    return match.group(1)


def _version_from_metadata() -> str | None:
    try:
        return pkg_version(_PACKAGE_NAME)
    except PackageNotFoundError:
        return None


@lru_cache(maxsize=1)
def resolve_version() -> str:
    """返回 semver 字符串（如 ``0.6.3``），不含 ``V`` 前缀。"""
    git_ver = _version_from_git(_repo_root())
    if git_ver:
        return git_ver
    meta = _version_from_metadata()
    if meta:
        return meta
    return _FALLBACK_VERSION


def display_version() -> str:
    """UI/CLI 展示用（如 ``V0.6.3``）。"""
    return f"V{resolve_version()}"


__all__ = ["resolve_version", "display_version"]
