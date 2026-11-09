"""工作区配置文件共享写锁。"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Iterator
from pathlib import Path

from local_webpage_access import file_lock
from local_webpage_access.errors import ConfigError

_SAVE_LOCK_TIMEOUT_S = 10.0
_SAVE_LOCK_INTERVAL_S = 0.05


@contextlib.contextmanager
def config_save_lock(path: Path) -> Iterator[None]:
    """配置保存互斥：保护「读-改-校验-替换」全程（BUG-1000）。

    锁文件与配置同目录、独立 inode，持锁期间不 unlink（见
    :mod:`local_webpage_access.file_lock` 约定）。flock 按 open-file-description
    互斥，同进程线程与跨进程由同一把锁覆盖。
    """
    lock_path = path.with_name(f".{path.name}.save-lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + _SAVE_LOCK_TIMEOUT_S
        while True:
            try:
                file_lock.try_acquire_exclusive(fd)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ConfigError(
                        "另一个配置保存操作进行中，请稍后重试"
                    ) from None
                time.sleep(_SAVE_LOCK_INTERVAL_S)
        file_lock.ensure_lockable(fd)
        try:
            yield
        finally:
            file_lock.release_exclusive(fd)
    finally:
        os.close(fd)
