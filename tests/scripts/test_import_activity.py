"""导入活动锁：防止 lwa update 重启打断进行中的导入。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from local_webpage_access.errors import LwaError
from local_webpage_access.import_activity import (
    import_activity_lock,
    wait_until_import_idle,
)
from local_webpage_access.paths import Workspace


@pytest.fixture()
def workspace(tmp_path: Path) -> Workspace:
    ws = Workspace(tmp_path / "ws")
    ws.ensure_workspace_dirs()
    return ws


def test_wait_until_import_idle_when_no_holder(workspace: Workspace) -> None:
    waited = wait_until_import_idle(workspace, timeout=1.0)
    assert waited < 0.05


def test_wait_until_import_idle_blocks_while_held(workspace: Workspace) -> None:
    released = threading.Event()
    held = threading.Event()

    def holder() -> None:
        with import_activity_lock(workspace, timeout=5.0):
            held.set()
            assert released.wait(timeout=5.0)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert held.wait(timeout=2.0)

    started = time.monotonic()
    err: list[BaseException] = []

    def waiter() -> None:
        try:
            wait_until_import_idle(workspace, timeout=0.35, poll=0.05)
        except BaseException as exc:  # noqa: BLE001
            err.append(exc)

    tw = threading.Thread(target=waiter, daemon=True)
    tw.start()
    tw.join(timeout=2.0)
    assert tw.is_alive() is False
    assert err and isinstance(err[0], LwaError)
    assert "导入" in str(err[0])
    assert time.monotonic() - started >= 0.3

    released.set()
    t.join(timeout=2.0)


def test_import_activity_lock_is_reentrant(workspace: Workspace) -> None:
    with import_activity_lock(workspace, timeout=2.0):
        with import_activity_lock(workspace, timeout=2.0):
            pass
    wait_until_import_idle(workspace, timeout=1.0)


def test_wait_succeeds_after_holder_releases(workspace: Workspace) -> None:
    held = threading.Event()

    def holder() -> None:
        with import_activity_lock(workspace, timeout=5.0):
            held.set()
            time.sleep(0.2)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert held.wait(timeout=2.0)
    waited = wait_until_import_idle(workspace, timeout=3.0, poll=0.05)
    assert waited >= 0.0
    t.join(timeout=2.0)
