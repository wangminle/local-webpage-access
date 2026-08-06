"""导入活动互斥：避免 ``lwa update`` 重启打断进行中的导入。

跨进程文件锁 ``run/import.lock`` + 同进程可重入（文件夹导入会嵌套
``import_zip`` / ``update_zip``）。updater 在重启 manager/daemon 前调用
:func:`wait_until_import_idle`。
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING

from local_webpage_access.errors import LwaError
from local_webpage_access.file_lock import (
    ensure_lockable,
    release_exclusive,
    try_acquire_exclusive,
    write_lock_payload,
)
from local_webpage_access.logging import get_logger

if TYPE_CHECKING:
    from local_webpage_access.paths import Workspace

log = get_logger("import_activity")

LOCK_FILENAME = "import.lock"
DEFAULT_ACQUIRE_TIMEOUT = 600.0
DEFAULT_IDLE_WAIT = 180.0

_thread_lock = threading.RLock()
_local = threading.local()


def _lock_path(workspace: Workspace):
    return workspace.run / LOCK_FILENAME


@contextlib.contextmanager
def import_activity_lock(
    workspace: Workspace, *, timeout: float = DEFAULT_ACQUIRE_TIMEOUT
) -> Iterator[None]:
    """持有导入活动锁（同线程可重入）。"""
    with _thread_lock:
        depth = int(getattr(_local, "depth", 0) or 0)
        fd: int | None = None
        file_acquired = False
        if depth == 0:
            lock_path = _lock_path(workspace)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            deadline = time.monotonic() + timeout
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
            ensure_lockable(fd)
            while True:
                try:
                    try_acquire_exclusive(fd)
                    write_lock_payload(
                        fd, f"{os.getpid()}\n{time.time():.3f}\n".encode()
                    )
                    file_acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        with contextlib.suppress(OSError):
                            os.close(fd)
                        raise LwaError(
                            f"导入活动锁被占用，等待超时（{timeout:g}s）",
                            code="IMPORT_BUSY",
                        )
                    time.sleep(0.05)
            _local.fd = fd
            _local.file_acquired = file_acquired
        _local.depth = depth + 1
        try:
            yield
        finally:
            _local.depth = int(getattr(_local, "depth", 1)) - 1
            if _local.depth <= 0:
                held_fd = getattr(_local, "fd", None)
                if getattr(_local, "file_acquired", False) and held_fd is not None:
                    release_exclusive(held_fd)
                if held_fd is not None:
                    with contextlib.suppress(OSError):
                        os.close(held_fd)
                _local.fd = None
                _local.file_acquired = False
                _local.depth = 0


def wait_until_import_idle(
    workspace: Workspace,
    *,
    timeout: float = DEFAULT_IDLE_WAIT,
    poll: float = 0.25,
) -> float:
    """等到无导入持锁；返回已等待秒数。超时抛 ``IMPORT_BUSY``。"""
    lock_path = _lock_path(workspace)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    logged = False
    while True:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            ensure_lockable(fd)
            try:
                try_acquire_exclusive(fd)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LwaError(
                        "仍有导入进行中，请待导入完成后再执行 lwa update"
                        f"（已等待 {timeout:g}s）",
                        code="IMPORT_BUSY",
                    )
                if not logged:
                    log.info("检测到导入进行中，等待其完成后再重启服务…")
                    logged = True
                time.sleep(poll)
                continue
            # 空闲：立刻释放探测锁
            release_exclusive(fd)
            return max(0.0, time.monotonic() - started)
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)


__all__ = [
    "LOCK_FILENAME",
    "DEFAULT_IDLE_WAIT",
    "import_activity_lock",
    "wait_until_import_idle",
]
