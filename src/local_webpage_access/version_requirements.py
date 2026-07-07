"""运行时与 Python 包版本下限（集中定义，供 doctor / docker_runtime 复用）。"""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, version as pkg_version

MIN_DOCKER_VERSION = "29.0.0"
MIN_COMPOSE_VERSION = "2.40.2"
RECOMMENDED_COMPOSE_VERSION = "5.2.0"
MIN_CADDY_VERSION = "2.11.2"
MIN_FASTAPI_VERSION = "0.138.0"
MIN_UVICORN_VERSION = "0.45.0"
MIN_NODE_VERSION = "24.0.0"  # 前端 SPA 构建基线（OPS-001）

_VERSION_PREFIX = re.compile(r"^[vV]?(\d+(?:\.\d+)*)")


def parse_version_string(raw: str) -> tuple[int, ...]:
    """从版本字符串提取前导数字段（忽略后缀与构建元数据）。"""
    text = (raw or "").strip()
    match = _VERSION_PREFIX.match(text)
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def _compare(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    width = max(len(a), len(b))
    a_pad = a + (0,) * (width - len(a))
    b_pad = b + (0,) * (width - len(b))
    if a_pad < b_pad:
        return -1
    if a_pad > b_pad:
        return 1
    return 0


def version_ge(raw: str, minimum: str) -> bool:
    """``raw`` 版本是否 ≥ ``minimum``。"""
    parsed = parse_version_string(raw)
    required = parse_version_string(minimum)
    if not parsed or not required:
        return False
    return _compare(parsed, required) >= 0


def version_gt(raw: str, minimum: str) -> bool:
    """``raw`` 版本是否 > ``minimum``。"""
    parsed = parse_version_string(raw)
    required = parse_version_string(minimum)
    if not parsed or not required:
        return False
    return _compare(parsed, required) > 0


def installed_package_version(name: str) -> str | None:
    """读取已安装包版本；未安装返回 None。"""
    try:
        return pkg_version(name)
    except PackageNotFoundError:
        return None


__all__ = [
    "MIN_CADDY_VERSION",
    "MIN_COMPOSE_VERSION",
    "MIN_DOCKER_VERSION",
    "MIN_FASTAPI_VERSION",
    "MIN_NODE_VERSION",
    "MIN_UVICORN_VERSION",
    "RECOMMENDED_COMPOSE_VERSION",
    "installed_package_version",
    "parse_version_string",
    "version_ge",
    "version_gt",
]
