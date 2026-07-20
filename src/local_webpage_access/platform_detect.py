"""跨平台识别（IMP-030）：区分 macOS / Linux / WSL / Windows。

集中 :func:`detect_platform`，供 :mod:`setup`、:mod:`autostart` 复用。WSL 识别
基于 ``/proc/version``、``WSL_INTEROP``、``/run/WSL`` 等启发式，不依赖外部包——
用于自启动在「纯 Linux」与「WSL」间给出不同指引（WSL 的发行版生命周期由
Windows 侧决定，需额外的唤醒步骤）。
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from typing import Any

PLATFORM_MACOS = "macos"
PLATFORM_LINUX = "linux"
PLATFORM_WSL = "wsl"
PLATFORM_WINDOWS = "windows"
PLATFORM_UNKNOWN = "unknown"

# /proc/version 中标识 WSL 内核的子串（小写匹配）。
_WSL_MARKERS = ("microsoft", "wsl")


def _read_proc_version() -> str:
    try:
        with open("/proc/version", encoding="utf-8", errors="replace") as fh:
            return fh.read().lower()
    except OSError:
        return ""


def is_wsl() -> bool:
    """是否运行在 WSL 中（Linux 内核且带 Microsoft 标记）。

    非 Linux 直接返回 False；优先看 ``WSL_INTEROP`` 与 ``/run/WSL``，再回退到
    ``/proc/version`` 内容匹配，避免误判普通 Linux。
    """
    if platform.system() != "Linux":
        return False
    if os.environ.get("WSL_INTEROP") or os.path.isdir("/run/WSL"):
        return True
    return any(marker in _read_proc_version() for marker in _WSL_MARKERS)


def wsl_distro() -> str | None:
    """WSL 发行版名；非 WSL 返回 None。"""
    if not is_wsl():
        return None
    name = os.environ.get("WSL_DISTRO_NAME")
    if name:
        return name
    try:
        with open("/etc/os-release", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "unknown"


def detect_platform() -> str:
    """返回 ``macos`` / ``linux`` / ``wsl`` / ``windows`` / ``unknown``。"""
    system = platform.system()
    if system == "Darwin":
        return PLATFORM_MACOS
    if system == "Linux":
        return PLATFORM_WSL if is_wsl() else PLATFORM_LINUX
    if system in ("Windows", "Microsoft"):
        return PLATFORM_WINDOWS
    return PLATFORM_UNKNOWN


def is_unix_like() -> bool:
    """是否 macOS / Linux / WSL（可走 launchd 或 systemd user 自启动）。"""
    return detect_platform() in (PLATFORM_MACOS, PLATFORM_LINUX, PLATFORM_WSL)


def systemd_available() -> bool:
    """systemd 是否可用（仅 Linux/WSL 有意义）。

    同时要求 ``systemctl`` 在 PATH 且 ``/run/systemd/system`` 存在（user manager
    运行的标志）；WSL 需 0.2s+ 的 systemd 支持且 ``/etc/wsl.conf`` 已启用。
    """
    if detect_platform() not in (PLATFORM_LINUX, PLATFORM_WSL):
        return False
    if not shutil.which("systemctl"):
        return False
    return os.path.isdir("/run/systemd/system")


def subprocess_hidden_kwargs() -> dict[str, Any]:
    """Windows 下给 ``subprocess.run/Popen`` 追加 ``CREATE_NO_WINDOW``。

    无控制台父进程（如 ``lwa daemon`` DETACHED）再拉起 ``powershell`` /
    ``taskkill`` 时，若缺此标志，Windows 会周期性弹出短暂可见黑窗（BUG-250）。
    非 Windows 返回空 dict，可直接 ``subprocess.run(..., **kwargs)``。
    """
    if sys.platform == "win32":
        # CREATE_NO_WINDOW 是 Windows 专属常量；非 Windows 测试宿主（伪造 win32）
        # 用字面量兜底，避免 AttributeError。
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


__all__ = [
    "PLATFORM_MACOS",
    "PLATFORM_LINUX",
    "PLATFORM_WSL",
    "PLATFORM_WINDOWS",
    "PLATFORM_UNKNOWN",
    "detect_platform",
    "is_wsl",
    "is_unix_like",
    "systemd_available",
    "subprocess_hidden_kwargs",
    "wsl_distro",
]
