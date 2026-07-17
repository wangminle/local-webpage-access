"""统一日志模块，支持全局日志和实例日志。

- 全局日志：写入工作区 ``logs/lwa.log``，同时输出到控制台。
- 实例日志：写入 ``apps/<id>/logs/`` 下的分类日志（build/run/import 等）。

实现保持轻量，避免在小主机上引入额外依赖。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from rich.logging import RichHandler

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

_CONFIGURED = False


def setup_logging(
    level: _LogLevel = "INFO",
    log_dir: Path | None = None,
    *,
    force: bool = False,
) -> logging.Logger:
    """配置全局日志。

    多次调用默认幂等（除非 ``force=True``），避免重复添加 handler。
    当提供 ``log_dir`` 时，会额外写入 ``<log_dir>/lwa.log``（权限 0600）。
    """
    global _CONFIGURED

    root = logging.getLogger("local_webpage_access")
    root.setLevel(level)

    if _CONFIGURED and not force:
        return root

    # 清理旧 handler（force 或首次配置时）
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = RichHandler(
        show_time=True,
        show_level=True,
        show_path=False,
        rich_tracebacks=True,
        tracebacks_show_locals=False,
    )
    console.setLevel(level)
    root.addHandler(console)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "lwa.log"
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
        file_handler.setLevel(level)
        root.addHandler(file_handler)
        secure_chmod(log_path)

    root.propagate = False
    _CONFIGURED = True
    return root


def secure_chmod(path: Path, mode: int = 0o600) -> None:
    """收紧文件权限（BUG-118）；Windows 上 chmod 可能无效，忽略错误。"""
    try:
        path.chmod(mode)
    except OSError:
        pass


def get_logger(name: str) -> logging.Logger:
    """获取子模块 logger，自动归入 ``local_webpage_access`` 命名空间。"""
    if name.startswith("local_webpage_access"):
        return logging.getLogger(name)
    return logging.getLogger(f"local_webpage_access.{name}")


def instance_log_dir(apps_dir: Path, instance_id: str) -> Path:
    """返回实例日志目录 ``apps/<id>/logs/``，并确保目录存在。"""
    path = apps_dir / instance_id / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_instance_log(
    apps_dir: Path,
    instance_id: str,
    category: str,
    content: str,
    *,
    append: bool = True,
) -> Path:
    """把内容写入实例分类日志文件。

    ``category`` 例如 ``import``、``build``、``run``、``scan``。
    返回写入的日志文件路径。时间戳前缀便于排查。
    """
    log_dir = instance_log_dir(apps_dir, instance_id)
    path = log_dir / f"{category}.log"
    # BUG-186：写入前若当前文件已超阈值（默认 10MB）则先滚动，使本次写入落到新的
    # 当前文件——既治理无限增长，又保证当前文件总有近期内容供 read_log 读取。
    # 滚动失败不得影响日志写入主流程。
    try:
        from local_webpage_access.logs import rotate_path

        rotate_path(path)
    except Exception:  # noqa: BLE001
        pass
    ts = datetime.now().astimezone().strftime(_DATE_FORMAT)
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8") as fh:
        for line in content.splitlines() or [""]:
            fh.write(f"[{ts}] {line}\n")
    return path


def now_iso() -> str:
    """返回带本地时区的 ISO8601 时间戳，用于 registry 和元数据。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def now_compact() -> str:
    """返回 ``YYYY-MM-DD HH:MM`` 紧凑时间戳，用于事件记录。"""
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")


def utc_now() -> datetime:
    """返回当前 UTC 时间（aware），用于统一时间计算。"""
    return datetime.now(timezone.utc)


__all__ = [
    "setup_logging",
    "get_logger",
    "secure_chmod",
    "instance_log_dir",
    "write_instance_log",
    "now_iso",
    "now_compact",
    "utc_now",
]
