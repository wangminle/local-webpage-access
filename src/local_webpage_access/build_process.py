"""构建子进程登记与可取消终止（IMP-039 / DEV-084）。

构建命令（npm / pip / docker compose）必须以独立进程组运行；取消时先 TERM
再 KILL 整棵树。进程内用登记表持有 ``Popen``；跨进程靠持久化的
worker_pid / pgid + 身份指纹，避免 PID 复用误杀无关进程。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable

from local_webpage_access.logging import get_logger

log = get_logger("build_process")

# 温和终止后等待秒数，再强制 KILL。
_TERM_GRACE_SECONDS = 5.0

# 线程局部：当前正在执行的构建实例（供 run_command / docker 执行器登记）。
_tls = threading.local()


@dataclass
class ActiveBuildProc:
    instance_id: str
    proc: subprocess.Popen
    identity: str
    registered_at: float


class BuildProcessHub:
    """进程内活动构建子进程登记表。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._procs: dict[str, ActiveBuildProc] = {}
        self._cancel_flags: dict[str, bool] = {}

    def request_cancel(self, instance_id: str) -> bool:
        """标记取消并尽力终止已登记子进程。返回是否找到本地进程。"""
        with self._lock:
            self._cancel_flags[instance_id] = True
            active = self._procs.get(instance_id)
        if active is None:
            return False
        kill_process_tree(active.proc, grace_seconds=_TERM_GRACE_SECONDS)
        return True

    def is_cancel_requested(self, instance_id: str) -> bool:
        with self._lock:
            return bool(self._cancel_flags.get(instance_id))

    def clear_cancel(self, instance_id: str) -> None:
        with self._lock:
            self._cancel_flags.pop(instance_id, None)

    def register(
        self, instance_id: str, proc: subprocess.Popen, *, identity: str = ""
    ) -> None:
        with self._lock:
            self._procs[instance_id] = ActiveBuildProc(
                instance_id=instance_id,
                proc=proc,
                identity=identity or _proc_identity(proc.pid),
                registered_at=time.time(),
            )

    def unregister(self, instance_id: str, proc: subprocess.Popen | None = None) -> None:
        with self._lock:
            active = self._procs.get(instance_id)
            if active is None:
                return
            if proc is not None and active.proc is not proc:
                return
            self._procs.pop(instance_id, None)

    def get(self, instance_id: str) -> ActiveBuildProc | None:
        with self._lock:
            return self._procs.get(instance_id)

    def clear(self, instance_id: str) -> None:
        with self._lock:
            self._procs.pop(instance_id, None)
            self._cancel_flags.pop(instance_id, None)


_hub = BuildProcessHub()


def get_build_process_hub() -> BuildProcessHub:
    return _hub


def enter_build_context(instance_id: str) -> None:
    _tls.instance_id = instance_id
    _hub.clear_cancel(instance_id)


def exit_build_context(instance_id: str | None = None) -> None:
    current = getattr(_tls, "instance_id", None)
    if instance_id is None or current == instance_id:
        _tls.instance_id = None
    if instance_id:
        _hub.clear(instance_id)


def current_build_instance_id() -> str | None:
    return getattr(_tls, "instance_id", None)


def popen_new_session_kwargs() -> dict:
    """创建独立进程组/会话的 Popen kwargs（可被整树终止）。"""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_process_tree(
    proc: subprocess.Popen,
    *,
    grace_seconds: float = _TERM_GRACE_SECONDS,
) -> None:
    """先温和终止再强制终止完整进程树（IMP-039 / BUG-183）。"""
    if proc.poll() is not None:
        return
    pid = proc.pid
    try:
        if sys.platform == "win32":
            from local_webpage_access.platform_detect import subprocess_hidden_kwargs

            # Windows：taskkill /T 杀整树；先尝试无 /F，再强制。
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T"],
                capture_output=True,
                timeout=15,
                check=False,
                **subprocess_hidden_kwargs(),
            )
            try:
                proc.wait(timeout=grace_seconds)
                return
            except subprocess.TimeoutExpired:
                pass
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
                check=False,
                **subprocess_hidden_kwargs(),
            )
            with contextlib_suppress():
                proc.wait(timeout=5)
            return

        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            return
        for sig, wait_s in (
            (signal.SIGTERM, grace_seconds),
            (signal.SIGKILL, 5.0),
        ):
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                return
            except PermissionError:
                break
            try:
                proc.wait(timeout=wait_s)
                return
            except subprocess.TimeoutExpired:
                continue
    except Exception:  # noqa: BLE001 — best-effort
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def kill_pid_tree_if_matches(
    pid: int,
    *,
    expected_pgid: int | None,
    expected_identity: str,
    grace_seconds: float = _TERM_GRACE_SECONDS,
) -> bool:
    """跨进程取消：仅当 PID 存活且身份匹配时发信号。返回是否发出信号。"""
    if pid <= 0 or not _pid_alive(pid):
        return False
    identity = _proc_identity(pid)
    if expected_identity and expected_identity not in (identity or ""):
        log.warning(
            "跳过终止 PID %s：身份不匹配（期望含 %r，实际 %r）",
            pid,
            expected_identity,
            identity,
        )
        return False
    if expected_pgid is not None and sys.platform != "win32":
        try:
            actual_pgid = os.getpgid(pid)
        except ProcessLookupError:
            return False
        if actual_pgid != expected_pgid:
            log.warning(
                "跳过终止 PID %s：pgid 不匹配（期望 %s，实际 %s）",
                pid,
                expected_pgid,
                actual_pgid,
            )
            return False
        for sig, wait_s in (
            (signal.SIGTERM, grace_seconds),
            (signal.SIGKILL, 5.0),
        ):
            try:
                os.killpg(expected_pgid, sig)
            except ProcessLookupError:
                return True
            except PermissionError:
                return False
            deadline = time.monotonic() + wait_s
            while time.monotonic() < deadline:
                if not _pid_alive(pid):
                    return True
                time.sleep(0.05)
        return not _pid_alive(pid)

    # Windows 或无 pgid：按 PID 杀树
    if sys.platform == "win32":
        from local_webpage_access.platform_detect import subprocess_hidden_kwargs

        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=15,
            check=False,
            **subprocess_hidden_kwargs(),
        )
        return not _pid_alive(pid)

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    return not _pid_alive(pid)


def _proc_identity(pid: int) -> str:
    """进程身份指纹（cmdline）；失败返回空串。"""
    try:
        from local_webpage_access.daemon import read_pid_cmdline

        return (read_pid_cmdline(pid) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def owner_process_identity() -> str:
    """当前进程身份，用于持久化后防 PID 复用。"""
    return _proc_identity(os.getpid()) or f"pid:{os.getpid()}"


def worker_identity_token(cmdline: str) -> str:
    """从命令行提取稳定身份片段（用于跨进程比对）。"""
    text = (cmdline or "").strip()
    if not text:
        return ""
    # 取前 120 字符足够区分常见构建命令，又避免过长。
    return text[:120]


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x1000, False, pid)  # SYNCHRONIZE
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


class contextlib_suppress:
    """无依赖的 suppress 上下文（避免循环 import）。"""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return True


def wait_with_cancel(
    proc: subprocess.Popen,
    *,
    timeout: float,
    should_cancel: Callable[[], bool],
    poll_interval: float = 0.25,
) -> str:
    """``communicate`` 循环；取消或超时由调用方处理。

    返回已收集的 stdout 文本。若进程仍在跑且 ``should_cancel()`` 为真，
    调用方应杀树并抛取消异常；若超时同样由调用方处理。

    注意：``communicate(timeout=)`` 超时时 ``TimeoutExpired.stdout`` 是累计
    partial，而重试成功时 ``communicate()`` 返回的是【全量累计】输出（含该
    partial）。因此不能两边都 append，否则 partial 被重复计算（BUG-273）。
    这里只在成功时返回 communicate 的全量；取消/超时退出时返回最后一次 partial。
    """
    deadline = time.monotonic() + timeout
    last_partial = ""
    while True:
        if should_cancel():
            return last_partial
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return last_partial
        try:
            out, _ = proc.communicate(timeout=min(poll_interval, remaining))
            # 成功：communicate 返回全量累计输出（含此前 partial），直接返回。
            return out or ""
        except subprocess.TimeoutExpired as exc:
            if exc.stdout:
                last_partial = (
                    exc.stdout
                    if isinstance(exc.stdout, str)
                    else exc.stdout.decode(errors="replace")
                )
            continue


__all__ = [
    "ActiveBuildProc",
    "BuildProcessHub",
    "current_build_instance_id",
    "enter_build_context",
    "exit_build_context",
    "get_build_process_hub",
    "kill_pid_tree_if_matches",
    "kill_process_tree",
    "owner_process_identity",
    "popen_new_session_kwargs",
    "wait_with_cancel",
    "worker_identity_token",
]
