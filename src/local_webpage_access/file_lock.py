"""跨平台文件互斥锁（BUG-213）。

* POSIX：``fcntl.flock``（函数内惰性导入，避免 Windows 收集测试时
  ``import fcntl`` 直接 ``ModuleNotFoundError``）。
* Windows：``msvcrt.locking``。

持锁期间保持同一 inode 打开；**释放时不 unlink**——否则等待者可能锁住旧
inode，第三进程又创建并锁住新 inode，互斥失效。
"""

from __future__ import annotations

import contextlib
import os
import sys


def try_acquire_exclusive(fd: int) -> None:
    """非阻塞获取排他锁；已被占用时抛 :class:`BlockingIOError`。"""
    if sys.platform == "win32":
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError(*exc.args) from exc
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def release_exclusive(fd: int) -> None:
    """释放排他锁（幂等）。"""
    if sys.platform == "win32":
        import msvcrt

        with contextlib.suppress(OSError):
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)


def ensure_lockable(fd: int) -> None:
    """保证文件至少 1 字节，以便 Windows ``msvcrt.locking`` 可锁区域。

    仅在文件为空时写入占位字节，不覆盖已有持有者内容。
    """
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"\n")
        os.lseek(fd, 0, os.SEEK_SET)


def write_lock_payload(fd: int, payload: bytes) -> None:
    """在**已持锁**前提下写入 PID/心跳（保持长度 ≥ 1，避免破坏 Windows 区域锁）。"""
    if not payload:
        payload = b"0\n"
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, payload)
    os.ftruncate(fd, len(payload))
    os.fsync(fd)


__all__ = [
    "try_acquire_exclusive",
    "release_exclusive",
    "ensure_lockable",
    "write_lock_payload",
]
