"""``build_process`` 工具单测（IMP-039 / BUG-273 回归）。"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from local_webpage_access.build_process import wait_with_cancel


def _spawn(prog: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", prog],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX 时序")
def test_wait_with_cancel_success_no_duplicate() -> None:
    """BUG-273：成功返回时 stdout 不应重复。

    communicate(timeout=) 超时时 TimeoutExpired.stdout 是累计 partial，重试
    成功时 communicate 返回全量（含 partial）；两边都 append 会翻倍。
    """
    prog = (
        "import sys,time;"
        "sys.stdout.write('AAA');sys.stdout.flush();"
        "time.sleep(0.3);"
        "sys.stdout.write('BBBB');sys.stdout.flush()"
    )
    proc = _spawn(prog)
    out = wait_with_cancel(proc, timeout=3.0, should_cancel=lambda: False, poll_interval=0.1)
    assert out == "AAABBBB", f"stdout 被重复收集：{out!r}"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX 时序")
def test_wait_with_cancel_returns_partial_on_cancel() -> None:
    """取消时返回已收集的 partial（进程仍在跑，至少跑过一次 communicate）。"""
    prog = "import sys,time;sys.stdout.write('PART');sys.stdout.flush();time.sleep(10)"
    proc = _spawn(prog)
    start = time.monotonic()

    def should_cancel() -> bool:
        # 让首次 communicate 有机会超时并收集 partial
        return time.monotonic() - start > 0.3

    out = wait_with_cancel(proc, timeout=5.0, should_cancel=should_cancel, poll_interval=0.1)
    proc.kill()
    assert "PART" in out, f"未收集到 partial：{out!r}"
