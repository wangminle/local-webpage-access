"""统一日志模块，支持全局日志和实例日志。

- 全局日志：写入工作区 ``logs/lwa.log``，同时输出到控制台。
- 实例日志：写入 ``apps/<id>/logs/`` 下的分类日志（build/run/import 等）。

实现保持轻量，避免在小主机上引入额外依赖。
"""

from __future__ import annotations

import contextlib
import logging
import os
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from rich.console import Console
from rich.logging import RichHandler

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

_CONFIGURED = False
_FILE_LOG_MAX_BYTES = 10 * 1024 * 1024
_FILE_LOG_BACKUP_COUNT = 3


def setup_logging(
    level: _LogLevel = "INFO",
    log_dir: Path | None = None,
    *,
    log_filename: str = "lwa.log",
    force: bool = False,
) -> logging.Logger:
    """配置全局日志。

    多次调用默认幂等（除非 ``force=True``），避免重复添加 handler。
    当提供 ``log_dir`` 时，会额外写入 ``<log_dir>/<log_filename>``（权限 0600）。
    IMP-034：CLI→``lwa.log``，manager→``manager.log``，daemon→``daemon.log``。
    """
    global _CONFIGURED

    root = logging.getLogger("local_webpage_access")
    root.setLevel(level)

    if _CONFIGURED and not force:
        return root

    # 清理旧 handler（force 或首次配置时）；评审-组7：同时 close，
    # 避免长驻进程反复 force 重配累积打开的文件描述符。
    for handler in list(root.handlers):
        root.removeHandler(handler)
        with contextlib.suppress(Exception):
            handler.close()

    console = RichHandler(
        console=Console(stderr=True),
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
        log_path = log_dir / (log_filename or "lwa.log")
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=_FILE_LOG_MAX_BYTES,
            backupCount=_FILE_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
        file_handler.setLevel(level)
        root.addHandler(file_handler)
        secure_chmod(log_path)
        root.info("文件日志：%s", log_path)

    root.propagate = False
    _CONFIGURED = True
    return root


def secure_chmod(path: Path, mode: int = 0o600) -> None:
    """收紧文件权限（BUG-118）；Windows 上 chmod 可能无效，忽略错误。"""
    try:
        path.chmod(mode)
    except OSError:
        pass


def append_global_log(
    log_dir: Path,
    message: str,
    *,
    logger_name: str = "local_webpage_access.daemon",
    level: str = "INFO",
) -> None:
    """跨进程向 ``logs/lwa.log`` 追加一条运维可见记录（issue #16 / BUG-603）。

    ``lwa.log`` 的 RotatingFileHandler 属 CLI 进程，daemon 等长驻进程不得再挂
    第二个 rotating handler（双进程滚动会互相截断）。本函数用 **每次打开、
    O_APPEND 追加、随即关闭** 的裸写方式落一条与 ``_LOG_FORMAT`` 同格式的
    行：小行追加在 POSIX 上原子，CLI 侧滚动最坏让一行落入 ``lwa.log.1``，
    不损坏任何一侧。失败静默（面包屑丢失不影响主流程）。
    """
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "lwa.log"
        existed = log_path.exists()
        ts = datetime.now().strftime(_DATE_FORMAT)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{ts} {level:<8} {logger_name} {message}\n")
        if not existed:
            secure_chmod(log_path)
    except OSError:
        return


# ---- 操作来源（S2-08 / issue #59）--------------------------------------------
#
# ``LWA_OP_SOURCE`` 标记当前进程/线程里生命周期操作的触发方：
# cli（命令行，默认）/ manager（管理页）/ daemon（自愈 watcher）/ agent
# （Agent 通道，含 MCP stdio 转发的操作）。非 CLI 来源的阶段日志不落
# ``lwa.log``（各长驻进程有自己的日志文件），由 hosting 的镜像函数按
# 来源补一行 ``source=<来源> lifecycle_stage …``，避免运维盯主日志时把
# manager/daemon 触发的重建误判为「构建卡死」。

_OP_SOURCE_ENV = "LWA_OP_SOURCE"
# issue #16 / BUG-603 时代的 daemon 入口只设这个标记；作为来源回退兼容。
_DAEMON_MARKER_ENV = "LWA_DAEMON_PROCESS"
_OP_SOURCE_VALUES = ("cli", "manager", "daemon", "agent")
_OP_SOURCE_DEFAULT = "cli"
# AgentWorker 与 manager 同进程（线程级来源），env 无法区分，用 contextvar。
_op_source_context: ContextVar[str | None] = ContextVar("lwa_op_source", default=None)


def set_op_source(source: str) -> None:
    """入口设置本进程的操作来源（仅接受 cli/manager/daemon/agent）。

    服务子进程（manager/daemon）必须**强制**赋值：它们由 CLI fork/spawn，
    继承来的 env 里可能已是 ``cli``，setdefault 会失聪。
    """
    if source not in _OP_SOURCE_VALUES:
        raise ValueError(f"LWA_OP_SOURCE 只能是 {_OP_SOURCE_VALUES}，得到 {source!r}")
    os.environ[_OP_SOURCE_ENV] = source


def set_op_source_context(source: str):
    """线程/协程级来源覆盖（Agent 通道），返回 contextvar token 供还原。"""
    if source not in _OP_SOURCE_VALUES:
        raise ValueError(f"LWA_OP_SOURCE 只能是 {_OP_SOURCE_VALUES}，得到 {source!r}")
    return _op_source_context.set(source)


def reset_op_source_context(token) -> None:
    """还原 :func:`set_op_source_context` 的线程级来源覆盖。"""
    _op_source_context.reset(token)


def current_op_source() -> str:
    """当前操作来源：线程级覆盖 → 环境变量 → daemon 旧标记回退 → cli。"""
    context_source = _op_source_context.get()
    if context_source:
        return context_source
    env_source = os.environ.get(_OP_SOURCE_ENV)
    if env_source in _OP_SOURCE_VALUES:
        return env_source
    if os.environ.get(_DAEMON_MARKER_ENV) == "1":
        return "daemon"
    return _OP_SOURCE_DEFAULT


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
    "append_global_log",
    "set_op_source",
    "set_op_source_context",
    "reset_op_source_context",
    "current_op_source",
    "secure_chmod",
    "instance_log_dir",
    "write_instance_log",
    "now_iso",
    "now_compact",
    "utc_now",
]
