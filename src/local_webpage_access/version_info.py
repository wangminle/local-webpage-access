"""应用版本解析：优先从 Git 最新 commit 主题读取 ``V0.8.8-Build...`` 前缀。"""

from __future__ import annotations

import re
import subprocess
import tomllib
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version as pkg_version
from pathlib import Path

_VERSION_PREFIX = re.compile(r"^V(\d+\.\d+\.\d+)", re.IGNORECASE)
_PACKAGE_NAME = "local-webpage-access"
_FALLBACK_VERSION = "0.8.8"


def _is_lwa_repo(path: Path) -> bool:
    try:
        with (path / "pyproject.toml").open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return str(data.get("project", {}).get("name", "")).strip() == _PACKAGE_NAME


def _repo_root() -> Path | None:
    """editable 安装时定位仓库根（``src/local_webpage_access`` 的上两级）。"""
    here = Path(__file__).resolve().parent
    candidate = here.parent.parent
    if _is_lwa_repo(candidate):
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
            if root.is_dir() and _is_lwa_repo(root):
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
    """返回 semver 字符串（如 ``0.8.8``），不含 ``V`` 前缀。"""
    git_ver = _version_from_git(_repo_root())
    if git_ver:
        return git_ver
    meta = _version_from_metadata()
    if meta:
        return meta
    return _FALLBACK_VERSION


def version_from_subject(subject: str | None) -> str | None:
    """从 commit 主题解析 ``V0.8.8-Build...`` 前缀（IMP-063）。

    主题不含 ``Vx.y.z`` 时返回 ``None``——不伪造版本号，报告降级为短 SHA。
    """
    if not subject:
        return None
    match = _VERSION_PREFIX.match(str(subject).strip())
    return match.group(1) if match else None


def display_version() -> str:
    """UI/CLI 展示用（如 ``V0.8.8``）。"""
    return f"V{resolve_version()}"


def bind_process_version() -> str:
    """进程/应用启动时解析一次版本并返回展示串（BUG-451）。

    清掉 ``resolve_version`` 的进程内缓存后再解析，保证 manager 新建进程或
    ``create_app`` 重建时读到当前 git/元数据，而不是继承旧 CLI 导入时的缓存。
    长驻进程内此后仍靠调用方闭包/常量固定该值，避免中途「静默变版」。
    """
    resolve_version.cache_clear()
    return display_version()


def normalize_version_label(value: str | None) -> str | None:
    """比较用：去掉首尾空白与可选 ``V`` 前缀，空串视为缺失。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text[:1] in {"V", "v"} and len(text) > 1 and text[1].isdigit():
        text = text[1:]
    return text


__all__ = [
    "resolve_version",
    "display_version",
    "bind_process_version",
    "normalize_version_label",
    "version_from_subject",
]
