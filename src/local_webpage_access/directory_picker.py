"""IMP-051：在 LWA 宿主机上调起原生目录选择对话框。

管理页「选择文件夹」经 Manager API 调用本模块；返回的是**宿主机** POSIX
绝对路径（供 ``import-from-dir`` 使用），不是浏览器所在机器的路径。

平台：
- macOS：``osascript`` + ``choose folder``
- Linux：优先 ``zenity --file-selection --directory``，否则 ``kdialog``
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from collections.abc import Callable
from typing import Any

from local_webpage_access.errors import DirectoryPickerError

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SEC = 120
_PROMPT = "选择要导入到 Local Webpage Access 的文件夹"


Runner = Callable[..., subprocess.CompletedProcess[str]]


def pick_directory(
    *,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    runner: Runner | None = None,
) -> str:
    """打开宿主机目录选择器并返回绝对路径。

    ``runner`` 默认 ``subprocess.run``；测试可注入。
    """
    run = runner or _default_runner
    if sys.platform == "darwin":
        return _pick_macos(run, timeout=timeout)
    if sys.platform.startswith("linux"):
        return _pick_linux(run, timeout=timeout)
    raise DirectoryPickerError(
        f"当前平台不支持图形目录选择器（{sys.platform}），请手动粘贴绝对路径",
        code="unavailable",
    )


def _default_runner(
    cmd: list[str],
    *,
    timeout: float,
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - 固定 argv，无 shell
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        **kwargs,
    )


def _normalize_path(raw: str) -> str:
    path = raw.strip().rstrip("\n\r")
    if not path:
        raise DirectoryPickerError("未选择目录", code="cancelled")
    # POSIX path of choose folder 常带尾斜杠；根目录保留 /
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    if not path.startswith("/"):
        raise DirectoryPickerError(
            f"选择器返回了非绝对路径：{path}",
            code="unavailable",
        )
    return path


def _pick_macos(run: Runner, *, timeout: float) -> str:
    script = f'POSIX path of (choose folder with prompt "{_PROMPT}")'
    cmd = ["osascript", "-e", script]
    try:
        result = run(cmd, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise DirectoryPickerError(
            "选择文件夹超时，请重试或手动粘贴绝对路径",
            code="timeout",
        ) from exc
    except FileNotFoundError as exc:
        raise DirectoryPickerError(
            "找不到 osascript，无法打开访达目录选择器",
            code="unavailable",
        ) from exc

    err = (result.stderr or "") + (result.stdout or "")
    if result.returncode != 0:
        # osascript 取消：returncode=1，stderr 含 User canceled / (-128)
        if "canceled" in err.lower() or "cancelled" in err.lower() or "(-128)" in err:
            raise DirectoryPickerError("已取消选择", code="cancelled")
        log.warning("osascript choose folder 失败 rc=%s err=%s", result.returncode, err.strip())
        raise DirectoryPickerError(
            "无法打开目录选择器，请手动粘贴绝对路径",
            code="unavailable",
        )
    return _normalize_path(result.stdout or "")


def _pick_linux(run: Runner, *, timeout: float) -> str:
    zenity = shutil.which("zenity")
    kdialog = shutil.which("kdialog")
    if zenity:
        cmd = [
            zenity,
            "--file-selection",
            "--directory",
            f"--title={_PROMPT}",
        ]
    elif kdialog:
        cmd = [kdialog, "--getexistingdirectory", "/"]
    else:
        raise DirectoryPickerError(
            "未找到 zenity/kdialog，无法打开目录选择器；请手动粘贴绝对路径",
            code="unavailable",
        )

    try:
        result = run(cmd, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise DirectoryPickerError(
            "选择文件夹超时，请重试或手动粘贴绝对路径",
            code="timeout",
        ) from exc
    except FileNotFoundError as exc:
        raise DirectoryPickerError(
            "目录选择器可执行文件不可用，请手动粘贴绝对路径",
            code="unavailable",
        ) from exc

    if result.returncode != 0:
        # zenity/kdialog：取消通常为 1，空 stdout
        raise DirectoryPickerError("已取消选择", code="cancelled")
    return _normalize_path(result.stdout or "")


__all__ = [
    "DEFAULT_TIMEOUT_SEC",
    "pick_directory",
]
