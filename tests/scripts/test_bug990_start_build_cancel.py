"""BUG-990：首次 ``lwa start`` / full_rebuild 的构建必须可被 cancel-build 取消。

根因：start 路径在 ``_try_host_with_fallback`` 中裸调 ``host_fn``，不经
BuildQueue——``build_tasks`` 无记录、docker worker PID 不落库（build_token
仅在 ``queue.run`` 的 ``enter_build_context`` 中设置），进行中的首次构建对
cancel-build 不可见（返回 noop、停不掉 compose 进程，只能按 PID 手动 SIGINT）。

修复后 start/full_rebuild 的 top-1 与 fallback 构建均经 ``queue.run`` 登记
执行；取消抛 :class:`LifecycleError`，不进入 Gate-C 回滚/降级链（取消不是
候选失败，不得触发回滚或继续降级）。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from local_webpage_access import lifecycle
from local_webpage_access.build_queue import _reset_global_queue, get_build_queue
from local_webpage_access.errors import HostingError, LifecycleError
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry
from tests.scripts.test_lifecycle import (  # noqa: F401 — 夹具与种子函数
    _seed_container,
    fake_runtime,
)


@pytest.fixture()
def workspace(workspace_root: Path) -> Workspace:
    ws = Workspace(workspace_root)
    ws.ensure_workspace_dirs()
    return ws


@pytest.fixture()
def registry(workspace_root: Path) -> Registry:
    workspace_root.joinpath("registry").mkdir(parents=True, exist_ok=True)
    reg = Registry(workspace_root / "registry" / "local-web.db")
    reg.open()
    yield reg
    reg.close()


@pytest.fixture()
def config(workspace_root: Path):
    from local_webpage_access.config import Config, PortPool

    return Config(portPool=PortPool(start=21000, end=21050))


@pytest.fixture(autouse=True)
def _isolate_global_queue():
    """每个用例独立的进程内队列单例（与 test_build_queue.py 同口径）。"""
    _reset_global_queue()
    yield
    _reset_global_queue()


def _capture(errors: list, fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except BaseException as exc:  # noqa: BLE001 — 收集线程异常供主线程断言
        errors.append(exc)


def _wait_child_pid(pid_file: Path) -> int:
    deadline = time.time() + 5
    while time.time() < deadline and not pid_file.exists():
        time.sleep(0.05)
    assert pid_file.exists(), "构建子进程未启动"
    return int(pid_file.read_text().strip())


def _assert_pid_dead(pid: int) -> None:
    time.sleep(0.3)
    with pytest.raises((ProcessLookupError, PermissionError)):
        os.kill(pid, 0)


def _slow_build_host(workspace_root: Path, started: threading.Event):
    """模拟耗时 docker 构建：spawn 可被杀死的子进程树（与 run_command 同构）。"""
    from local_webpage_access.hosting import run_command
    from local_webpage_access.models import InstanceManifest

    child_pid_file = workspace_root / "bug990-child.pid"
    log_path = workspace_root / "logs" / "bug990-build.log"

    def _host(ws, cfg, reg, iid):
        started.set()
        run_command(
            f"sleep 60 & echo $! > {child_pid_file}; wait",
            cwd=workspace_root,
            log_path=log_path,
            timeout=120,
        )
        return InstanceManifest.load(ws.app_manifest_path(iid))

    return _host, child_pid_file


def _spy_rollback(monkeypatch) -> list:
    calls: list = []
    real_rollback = lifecycle._rollback_attempt

    def _spy(*args, **kwargs):
        calls.append(args)
        return real_rollback(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "_rollback_attempt", _spy)
    return calls


@pytest.mark.skipif(sys.platform == "win32", reason="进程树取消用例以 POSIX shell 为准")
def test_first_start_build_is_cancellable(
    workspace, registry, config, monkeypatch, fake_runtime, workspace_root  # noqa: F811
) -> None:
    """首次 start 构建进行中：cancel-build 返回 cancelled（非 noop）、进程树退出、
    build_tasks 收敛 cancelled、取消不触发 Gate-C 回滚，且不影响其他实例。"""
    _seed_container(workspace, registry, "api")  # 首次部署（未 deployed）
    _seed_container(workspace, registry, "api2")  # 无关实例：不得被误伤

    started = threading.Event()
    fake_host, child_pid_file = _slow_build_host(workspace_root, started)
    monkeypatch.setattr("local_webpage_access.hosting.host_instance", fake_host)
    rollback_calls = _spy_rollback(monkeypatch)

    errors: list = []
    t = threading.Thread(
        target=_capture,
        args=(errors, lifecycle.start_instance, workspace, config, registry, "api"),
    )
    t.start()
    assert started.wait(5.0), "start 未进入构建"
    child_pid = _wait_child_pid(child_pid_file)

    result = lifecycle.cancel_build(workspace, config, registry, "api")
    assert result.outcome == "cancelled", result

    t.join(timeout=20)
    assert not t.is_alive(), "取消后 start 线程未退出"
    assert len(errors) == 1
    assert isinstance(errors[0], LifecycleError), errors
    assert "已取消" in str(errors[0])

    _assert_pid_dead(child_pid)
    assert not rollback_calls, "取消不得触发 Gate-C 回滚/降级链"

    queue = get_build_queue(config, registry)
    with queue._guard:  # noqa: SLF001
        assert queue._tasks["api"].status == "cancelled"  # noqa: SLF001
        assert "api2" not in queue._tasks  # noqa: SLF001
    assert queue.global_in_flight() == 0

    noop = lifecycle.cancel_build(workspace, config, registry, "api2")
    assert noop.outcome == "noop"


@pytest.mark.skipif(sys.platform == "win32", reason="进程树取消用例以 POSIX shell 为准")
def test_cancel_during_fallback_attempt_propagates_without_rollback(
    workspace, registry, config, monkeypatch, fake_runtime, workspace_root  # noqa: F811
) -> None:
    """fallback 候选构建进行中取消：LifecycleError 必须穿透 fallback 循环的
    宽捕获直接上抛——不得视为候选失败而回滚或继续降级。"""
    manifest = _seed_container(workspace, registry, "api")
    manifest.deploymentCandidates = [
        {
            "kind": "node",
            "runtime": "docker-compose",
            "servingMode": "container",
            "confidenceTier": "fallback",
            "form": "backend-container",
            "entry": {"install": "npm ci", "start": "node server.js"},
        }
    ]

    started_fallback = threading.Event()
    calls = {"n": 0}
    from local_webpage_access.hosting import run_command

    child_pid_file = workspace_root / "bug990-fb-child.pid"
    log_path = workspace_root / "logs" / "bug990-fb-build.log"

    def _host(ws, cfg, reg, iid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise HostingError("top1 build failed")
        started_fallback.set()
        run_command(
            f"sleep 60 & echo $! > {child_pid_file}; wait",
            cwd=workspace_root,
            log_path=log_path,
            timeout=120,
        )
        return manifest

    rollback_calls = _spy_rollback(monkeypatch)

    errors: list = []
    t = threading.Thread(
        target=_capture,
        args=(
            errors,
            lifecycle._try_host_with_fallback,
            workspace,
            config,
            registry,
            "api",
            manifest,
            _host,
        ),
        kwargs={"fallback_policy": "auto-equivalent"},
    )
    t.start()
    assert started_fallback.wait(5.0), "未进入 fallback 候选构建"
    child_pid = _wait_child_pid(child_pid_file)

    result = lifecycle.cancel_build(workspace, config, registry, "api")
    assert result.outcome == "cancelled", result

    t.join(timeout=20)
    assert not t.is_alive(), "取消后降级链未退出"
    assert len(errors) == 1
    assert isinstance(errors[0], LifecycleError), errors
    assert "已取消" in str(errors[0])

    _assert_pid_dead(child_pid)
    # 仅 top-1 失败触发的 1 次回滚；被取消的 fallback attempt 不得再回滚
    assert len(rollback_calls) == 1

    queue = get_build_queue(config, registry)
    with queue._guard:  # noqa: SLF001
        assert queue._tasks["api"].status == "cancelled"  # noqa: SLF001
    assert queue.global_in_flight() == 0
