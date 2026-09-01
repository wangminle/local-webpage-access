"""CHK-280 双 Caddy master 竞态回归测试。

现场复盘（2026-08-31，V0.8.9 升级）：update 重启编排先 restart_daemon 后
restart_gateway，新 daemon 的启动自愈在 stop→start 间隙观测 gateway_down，
经 ``ensure_caddy_running`` 无锁 ``caddy start``；网关监督器的
``start_gateway`` 同时持 ``gateway_start_lock`` 再跑一次 ``caddy start``。
Caddy SO_REUSEPORT 让两个 master 双双绑定 :8080/:2019 并共享 pidfile，
先退出者还删除共享 pidfile，监督器随后 11 次误判掉线。

本文件锁定修复契约：

1. ``gateway_start_lock`` 同线程可重入（reload 自愈链在持锁栈内再入不卡死）；
2. ``ensure_caddy_running`` admin 离线时持启动锁串行拉起；锁被他者
   （跨进程）持有时 fail-safe：不启动、复查后返回；
3. ``stop_gateway_internal`` 停止全程持启动锁（update 的 stop→start 间隙
   不再被并发 ensure 插入）；
4. update 收尾的单 master 断言能识别 lsof 多监听 PID；
5. ``caddy start`` 的 stderr 落盘 ``logs/caddy-runtime.log``（不再 DEVNULL）。
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from local_webpage_access.config import Config
from local_webpage_access.errors import LifecycleError
from local_webpage_access.file_lock import try_acquire_exclusive
from local_webpage_access.gateway_service import (
    gateway_start_lock,
    start_lock_path,
    stop_gateway_internal,
    write_state,
    GatewayState,
)
from local_webpage_access.paths import Workspace
from local_webpage_access.static_gateway import StaticGateway


# ---- 1. gateway_start_lock 可重入 ------------------------------------------------


def test_gateway_start_lock_reentrant_same_thread(workspace: Workspace) -> None:
    """同线程嵌套获取不阻塞（否则 start_gateway→reload 自愈链自死锁）。"""
    t0 = time.monotonic()
    with gateway_start_lock(workspace):
        with gateway_start_lock(workspace):
            pass
    assert time.monotonic() - t0 < 2.0


def test_gateway_start_lock_serializes_threads_without_timeout(
    workspace: Workspace,
) -> None:
    """不同线程应在进程锁上排队，持有者释放后等待者成功进入。"""
    holder_entered = threading.Event()
    events: list[str] = []
    errors: list[BaseException] = []

    def _holder() -> None:
        with gateway_start_lock(workspace, timeout=0.4):
            events.append("holder_acquired")
            holder_entered.set()
            time.sleep(0.1)
        events.append("holder_released")

    def _waiter() -> None:
        assert holder_entered.wait(timeout=1.0)
        try:
            with gateway_start_lock(workspace, timeout=0.4):
                events.append("waiter_acquired")
            events.append("waiter_released")
        except BaseException as exc:  # noqa: BLE001 - 线程异常需回传主测试
            errors.append(exc)

    holder = threading.Thread(target=_holder)
    waiter = threading.Thread(target=_waiter)
    holder.start()
    waiter.start()
    holder.join(timeout=2.0)
    waiter.join(timeout=2.0)

    assert not holder.is_alive()
    assert not waiter.is_alive()
    assert errors == []
    assert events == [
        "holder_acquired",
        "holder_released",
        "waiter_acquired",
        "waiter_released",
    ]


def test_gateway_start_lock_excludes_second_fd(workspace: Workspace) -> None:
    """同进程另一 fd（模拟他者进程）持锁时，等待 timeout 后抛 LifecycleError。"""
    path = start_lock_path(workspace)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try_acquire_exclusive(fd)
        t0 = time.monotonic()
        with pytest.raises(LifecycleError):
            with gateway_start_lock(workspace, timeout=0.3):
                pass
        assert time.monotonic() - t0 < 2.0
    finally:
        os.close(fd)


# ---- 2. ensure_caddy_running 持锁 / fail-safe ------------------------------------


class _EnsureProbe(StaticGateway):
    """替身：记录 caddy_start 调用，admin 探活/归属可控。"""

    def __init__(self, ws: Workspace, cfg: Config) -> None:
        super().__init__(ws, cfg)
        self.start_calls = 0
        self._alive = False

    def _admin_alive(self, **kw) -> bool:  # noqa: ARG002
        return self._alive

    def inspect_caddy_owner(self) -> dict:
        return {
            "owner": "lwa_service_user",
            "workspace_match": True,
            "pid": 4242,
            "admin_alive": self._alive,
        }

    def caddy_start(self) -> bool:
        self.start_calls += 1
        self._alive = True
        return True


def test_ensure_starts_master_under_lock_when_admin_down(workspace: Workspace) -> None:
    """admin 离线 + 锁空闲：持锁拉起。"""
    cfg = Config(staticGateway="caddy", staticGatewayPort=8080)
    gw = _EnsureProbe(workspace, cfg)
    assert gw.ensure_caddy_running() is True
    assert gw.start_calls == 1
    # 锁文件存在（外层获取-释放路径已执行）
    assert start_lock_path(workspace).is_file()


def test_ensure_fast_path_skips_lock_when_admin_healthy(workspace: Workspace) -> None:
    """admin 健康：无锁快路径——他者持启动锁也不影响本判定（稳态零锁竞争）。"""
    cfg = Config(staticGateway="caddy", staticGatewayPort=8080)
    gw = _EnsureProbe(workspace, cfg)
    gw._alive = True
    path = start_lock_path(workspace)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try_acquire_exclusive(fd)
        t0 = time.monotonic()
        assert gw.ensure_caddy_running() is True
        assert time.monotonic() - t0 < 1.0
        assert gw.start_calls == 0
    finally:
        os.close(fd)


def test_ensure_waits_then_claims_master_started_by_holder(workspace: Workspace) -> None:
    """锁被他者持有期间 master 被拉起：等待→复查→认领，不重复启动。

    模拟 update 竞态窗口：监督器持锁 caddy_start 中，daemon 自愈等锁；
    监督器完成后 daemon 复查 admin 已在线 → True 且零次 caddy_start。
    """
    cfg = Config(staticGateway="caddy", staticGatewayPort=8080)
    gw = _EnsureProbe(workspace, cfg)
    path = start_lock_path(workspace)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try_acquire_exclusive(fd)

    def _holder() -> None:
        time.sleep(0.3)
        gw._alive = True
        os.close(fd)

    threading.Thread(target=_holder, daemon=True).start()
    t0 = time.monotonic()
    assert gw.ensure_caddy_running() is True
    assert time.monotonic() - t0 < 5.0
    assert gw.start_calls == 0


def test_ensure_fails_safe_when_lock_busy_and_master_absent(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """锁长期被占 + master 缺失：绝不无锁拉起（返回 False 交上层重试）。"""
    import local_webpage_access.gateway_service as gs

    monkeypatch.setattr(gs, "GATEWAY_START_LOCK_TIMEOUT", 0.2)
    cfg = Config(staticGateway="caddy", staticGatewayPort=8080)
    gw = _EnsureProbe(workspace, cfg)
    path = start_lock_path(workspace)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try_acquire_exclusive(fd)
        t0 = time.monotonic()
        assert gw.ensure_caddy_running() is False
        assert gw.start_calls == 0
        assert time.monotonic() - t0 < 2.0
    finally:
        os.close(fd)


def test_ensure_reentrant_under_start_lock(workspace: Workspace) -> None:
    """start_gateway 持锁栈内再入（reload 自愈链）：可重入直通，无超时损耗。"""
    cfg = Config(staticGateway="caddy", staticGatewayPort=8080)
    gw = _EnsureProbe(workspace, cfg)
    with gateway_start_lock(workspace):
        t0 = time.monotonic()
        assert gw.ensure_caddy_running() is True
        assert gw.start_calls == 1
        assert time.monotonic() - t0 < 2.0


def test_ensure_reentrant_under_mutation_lock_uses_short_timeout(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """持配置锁（reload_all 栈内）时：启动锁等待上限压短（ABBA 有界化）。"""
    import local_webpage_access.static_gateway as sg

    monkeypatch.setattr(sg, "_ENSURE_START_LOCK_MUTATION_TIMEOUT", 0.2)
    cfg = Config(staticGateway="caddy", staticGatewayPort=8080)
    gw = _EnsureProbe(workspace, cfg)
    path = start_lock_path(workspace)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try_acquire_exclusive(fd)
        sg._gateway_mutation_state.depth = 1
        t0 = time.monotonic()
        assert gw.ensure_caddy_running() is False
        assert gw.start_calls == 0
        assert time.monotonic() - t0 < 2.0
    finally:
        sg._gateway_mutation_state.depth = 0
        os.close(fd)


# ---- 3. stop_gateway_internal 持锁 -----------------------------------------------


def test_stop_gateway_internal_waits_for_start_lock(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """内部停止与 ensure/start 串行：先取启动锁再 caddy_stop。"""
    import local_webpage_access.gateway_service as gs

    cfg = Config(staticGateway="caddy", staticGatewayPort=8080)
    events: list[str] = []
    real_lock = gs.gateway_start_lock

    def _tracking_lock(ws, *, timeout=None):  # noqa: ANN001, ARG001
        events.append("lock")
        return real_lock(ws, timeout=timeout)

    monkeypatch.setattr(gs, "gateway_start_lock", _tracking_lock)

    class _StopProbe:
        def __init__(self, ws, cfg) -> None:  # noqa: ANN001, ARG002
            pass

        @staticmethod
        def detect_backend() -> str:
            return "caddy"

        @staticmethod
        def caddy_stop() -> bool:
            events.append("stop")
            return True

    monkeypatch.setattr(gs, "StaticGateway", _StopProbe)
    write_state(workspace, GatewayState(enabled=True, pid=123, port=8080))
    assert stop_gateway_internal(workspace, cfg) is True
    assert events == ["lock", "stop"]
    state = gs.read_state(workspace)
    assert state is not None
    assert state.pid is None
    assert state.enabled is True  # IMP-064.03 契约保持


# ---- 4. update 收尾单 master 断言 -------------------------------------------------


def _step_by_pids(monkeypatch: pytest.MonkeyPatch, pids: list[str]):
    import local_webpage_access.updater as up

    monkeypatch.setattr(up, "_admin_master_pids", lambda port=2019: list(pids))
    cfg = Config(staticGateway="caddy", staticGatewayPort=8080)
    return up._check_single_caddy_master(cfg)


def test_caddy_master_check_ok_single(monkeypatch: pytest.MonkeyPatch) -> None:
    step = _step_by_pids(monkeypatch, ["70558"])
    assert step.name == "caddyMasterCheck"
    assert step.status == "ok"
    assert "70558" in step.message


def test_caddy_master_check_failed_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    """CHK-280 现场：70556/70558 双 master 必须当场报 failed（而非延迟显形）。"""
    step = _step_by_pids(monkeypatch, ["70556", "70558"])
    assert step.status == "failed"
    assert step.extra.get("masterPids") == ["70556", "70558"]
    assert "CHK-280" in step.message


def test_caddy_master_check_skipped_without_lsof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    step = _step_by_pids(monkeypatch, [])
    assert step.status == "skipped"


def test_caddy_master_check_skipped_non_caddy_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import local_webpage_access.updater as up

    monkeypatch.setattr(up, "_admin_master_pids", lambda port=2019: ["1"])
    cfg = Config(staticGateway="builtin", staticGatewayPort=8080)
    step = up._check_single_caddy_master(cfg)
    assert step.status == "skipped"


def test_admin_master_pids_dedupes(monkeypatch: pytest.MonkeyPatch) -> None:
    """lsof 输出多行（IPv4/IPv6 重复行）时按 PID 去重。"""
    import local_webpage_access.updater as up

    class _R:
        returncode = 0
        stdout = (
            "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
            "caddy 70558 fenix 6u IPv4 0x1234 0t0 TCP 127.0.0.1:2019 (LISTEN)\n"
            "caddy 70558 fenix 7u IPv6 0x1235 0t0 TCP 127.0.0.1:2019 (LISTEN)\n"
            "caddy 70561 fenix 6u IPv4 0x1236 0t0 TCP 127.0.0.1:2019 (LISTEN)\n"
        )
        stderr = ""

    monkeypatch.setattr(up.subprocess, "run", lambda *a, **kw: _R())
    monkeypatch.setattr(up.shutil, "which", lambda name: "/usr/sbin/lsof")
    assert up._admin_master_pids() == ["70558", "70561"]


# ---- 5. caddy stderr 落盘 ----------------------------------------------------------


def test_caddy_start_routes_stderr_to_runtime_log(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CHK-280 可观测性：stderr 指向 logs/caddy-runtime.log，stdout 保持 DEVNULL。"""
    import subprocess as sp

    import local_webpage_access.static_gateway as sg

    cfg = Config(staticGateway="caddy", staticGatewayPort=8080)
    gw = StaticGateway(workspace, cfg)

    monkeypatch.setattr(sg, "_refuse_caddy_admin_in_pytest", lambda action: None)
    monkeypatch.setattr(gw, "_admin_alive", lambda **kw: True)
    monkeypatch.setattr(gw, "_workspace_caddy_pid_alive", lambda: True)
    # 主配置已存在且非空 → 不走 bootstrap 分支
    main = gw.main_config_path()
    main.parent.mkdir(parents=True, exist_ok=True)
    main.write_text(":2019 { }\n", encoding="utf-8")

    captured: dict[str, object] = {}

    class _FakeProc:
        returncode = 0

        def poll(self) -> int:
            return 0

        def wait(self, timeout=None) -> int:  # noqa: ARG002
            return 0

    def _fake_popen(cmd, **kwargs):  # noqa: ANN001, ARG001
        captured.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(sg.subprocess, "Popen", _fake_popen)

    # 第一轮探活即 admin+pid 就绪 → early_ready → 返回 True
    assert gw.caddy_start() is True

    assert captured.get("stdout") is sp.DEVNULL
    stderr_opt = captured.get("stderr")
    assert stderr_opt is not None
    assert not isinstance(stderr_opt, int), "stderr 不得再是 DEVNULL 常量"
    assert getattr(stderr_opt, "name", "").endswith("caddy-runtime.log")
    assert (workspace.logs / "caddy-runtime.log").exists()
