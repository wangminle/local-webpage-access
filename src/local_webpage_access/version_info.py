"""应用版本解析：优先从 Git 最新 commit 主题读取 ``V0.8.17-Build...`` 前缀。"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version as pkg_version
from pathlib import Path
from typing import Any

_VERSION_PREFIX = re.compile(r"^V(\d+\.\d+\.\d+)", re.IGNORECASE)
_PACKAGE_NAME = "local-webpage-access"
_FALLBACK_VERSION = "0.8.17"


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
    """返回 semver 字符串（如 ``0.8.17``），不含 ``V`` 前缀。"""
    git_ver = _version_from_git(_repo_root())
    if git_ver:
        return git_ver
    meta = _version_from_metadata()
    if meta:
        return meta
    return _FALLBACK_VERSION


def version_from_subject(subject: str | None) -> str | None:
    """从 commit 主题解析 ``V0.8.17-Build...`` 前缀（IMP-063）。

    主题不含 ``Vx.y.z`` 时返回 ``None``——不伪造版本号，报告降级为短 SHA。
    """
    if not subject:
        return None
    match = _VERSION_PREFIX.match(str(subject).strip())
    return match.group(1) if match else None


def display_version() -> str:
    """UI/CLI 展示用（如 ``V0.8.17``）。"""
    return f"V{resolve_version()}"


def bind_process_version() -> str:
    """进程/应用启动时解析一次版本并返回展示串（BUG-451）。

    清掉 ``resolve_version`` 的进程内缓存后再解析，保证 manager 新建进程或
    ``create_app`` 重建时读到当前 git/元数据，而不是继承旧 CLI 导入时的缓存。
    长驻进程内此后仍靠调用方闭包/常量固定该值，避免中途「静默变版」。
    """
    resolve_version.cache_clear()
    return display_version()


def bind_process_revision() -> str | None:
    """进程启动时记录的短提交标识；无 git 仓库时返回 ``None``。"""
    root = _repo_root()
    if root is None:
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
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
    rev = (result.stdout or "").strip()
    if not rev or any(c not in "0123456789abcdefABCDEF" for c in rev):
        return None
    return rev[:12] if len(rev) >= 12 else None


def revisions_equivalent(left: str | None, right: str | None) -> bool:
    """同一提交的短 hash 可能 12 或更长（``--short`` 为消歧会加长）。"""
    if not left or not right:
        return False
    if left == right:
        return True
    n = min(len(left), len(right))
    return n >= 12 and left[:n] == right[:n]


def fill_missing_bind_version(state: Any, path: Path) -> dict[str, Any]:
    """序列化状态；缺 bind 字段时只填进返回的 dict，不修改入参（BUG-667）。"""
    payload = dict(state.to_dict())
    missing_ver = not payload.get("bind_version")
    missing_rev = not payload.get("bind_revision")
    if not missing_ver and not missing_rev:
        return payload
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return payload
    if not isinstance(data, dict):
        return payload
    if missing_ver and data.get("bind_version"):
        payload["bind_version"] = str(data["bind_version"])
    if missing_rev and data.get("bind_revision"):
        payload["bind_revision"] = str(data["bind_revision"])
    return payload


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
    "bind_process_revision",
    "fill_missing_bind_version",
    "normalize_version_label",
    "revisions_equivalent",
    "version_from_subject",
]
