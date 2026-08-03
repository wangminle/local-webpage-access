"""``gateway_service`` 单测（IMP-010 / DEV-041，WBS 0.2 / 0.9）。

用可控的 ``StaticGateway`` 替身覆盖服务层逻辑：状态读写、启停、探活、降级。
真实 Caddy 子进程交互由 ``tests/test_static_gateway.py`` 的 ``caddy_start/stop``
单测覆盖，此处只验证服务编排与 ``run/gateway.json`` 状态。
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

import pytest

from local_webpage_access.config import Config
from local_webpage_access.errors import LifecycleError
from local_webpage_access.gateway_service import (
    GatewayState,
    gateway_start_lock,
    gateway_status,
    is_gateway_running,
    maybe_start_gateway,
    read_state,
    start_gateway,
    start_lock_path,
    state_path,
    stop_gateway,
    write_state,
)
from local_webpage_access.paths import Workspace


# ---- 状态读写 ---------------------------------------------------------------


def test_state_roundtrip(workspace: Workspace) -> None:
    write_state(workspace, GatewayState(enabled=True, pid=12345, started_at="t", port=8080))
    st = read_state(workspace)
    assert st is not None
    assert st.enabled is True
    assert st.pid == 12345
    assert st.port == 8080
    assert st.admin_port == 2019


def test_read_state_none_when_absent(workspace: Workspace) -> None:
    assert read_state(workspace) is None


def test_read_state_none_on_corrupt_json(workspace: Workspace) -> None:
    state_path(workspace).parent.mkdir(parents=True, exist_ok=True)
    state_path(workspace).write_text("{ not json", encoding="utf-8")
    assert read_state(workspace) is None


def test_read_state_none_on_non_dict(workspace: Workspace) -> None:
    state_path(workspace).parent.mkdir(parents=True, exist_ok=True)
    state_path(workspace).write_text("[1, 2, 3]", encoding="utf-8")
    assert read_state(workspace) is None


def test_read_state_handles_null_port(workspace: Workspace) -> None:
    """staticGatewayPort=None 时，state 的 port 也应为 None。"""
    state_path(workspace).parent.mkdir(parents=True, exist_ok=True)
    state_path(workspace).write_text(
        json.dumps(
            {"enabled": True, "pid": 7, "started_at": "t", "port": None, "admin_port": 2019}
        ),
        encoding="utf-8",
    )
    st = read_state(workspace)
    assert st is not None
    assert st.port is None
    assert st.pid == 7


# ---- StaticGateway 替身 -----------------------------------------------------


@pytest.fixture()
def fake_gateway(monkeypatch, workspace):
    """把 gateway_service 内的 StaticGateway 换成可控替身，返回共享状态字典。

    所有函数（start/stop/status/...）各自构造的 StaticGateway 都映射到同一个
    闭包状态，便于在用例里翻转 backend / admin / 启停成败。
    """
    state = {
        "backend": "caddy",
        "admin_alive": False,
        "start_ok": True,
        "stop_ok": True,
        "start_calls": 0,
        "stop_calls": 0,
        "sync_calls": 0,
        "write_main_calls": 0,
        "stop_builtin_calls": 0,
        "reload_calls": 0,
        "call_order": [],
        "stopped_builtin": [],
        "pid": 12345,
        "owner": "lwa_service_user",
        "workspace_match": True,
    }

    class _Fake:
        def __init__(self, ws: Workspace, cfg: Config) -> None:
            self.ws = ws
            self.cfg = cfg

        def detect_backend(self) -> str:
            return state["backend"]

        def _admin_alive(self, **kw) -> bool:
            return state["admin_alive"]

        def inspect_caddy_owner(self) -> dict:
            return {
                "owner": state["owner"],
                "workspace_match": state["workspace_match"],
                "pid": state["pid"],
            }

        def caddy_start(self) -> bool:
            state["start_calls"] += 1
            state["call_order"].append("caddy_start")
            if state["start_ok"]:
                state["admin_alive"] = True  # start 成功后 master 在线
                self.ws.run.mkdir(parents=True, exist_ok=True)
                (self.ws.run / "caddy.pid").write_text(str(state["pid"]))
            return state["start_ok"]

        def caddy_stop(self) -> bool:
            state["stop_calls"] += 1
            if state["stop_ok"]:
                state["admin_alive"] = False
                with contextlib.suppress(FileNotFoundError):
                    (self.ws.run / "caddy.pid").unlink()
            return state["stop_ok"]

        def caddy_pid_path(self) -> Path:
            return self.ws.run / "caddy.pid"

        def main_config_path(self) -> Path:
            return self.ws.static_gateway / "Caddyfile"

        def write_main_config(self) -> None:
            # BUG-420：start_gateway 在 caddy_start 前无条件落盘当前主配置。
            state["write_main_calls"] += 1
            state["call_order"].append("write_main_config")

        def _sync_main_config(self) -> None:
            # BUG-074 遗留：写盘+reload。冷启动已改用 write_main_config，保留计数便于回归。
            state["sync_calls"] += 1
            state["call_order"].append("_sync_main_config")

        def stop_all_builtin(self) -> list[str]:
            # I1 / G3：start_gateway 在 caddy_start **之前**调用（先停旧再拉新）。
            state["stop_builtin_calls"] += 1
            state["call_order"].append("stop_all_builtin")
            return list(state.get("stopped_builtin") or [])

        def reload_all(self) -> None:
            state["reload_calls"] += 1
            state["call_order"].append("reload_all")

    monkeypatch.setattr("local_webpage_access.gateway_service.StaticGateway", _Fake)
    return state


# ---- is_gateway_running -----------------------------------------------------


def test_is_gateway_running_true_when_caddy_admin_alive(
    workspace: Workspace, config: Config, fake_gateway
) -> None:
    fake_gateway["admin_alive"] = True
    assert is_gateway_running(workspace, config) is True


def test_is_gateway_running_false_when_admin_down(
    workspace: Workspace, config: Config, fake_gateway
) -> None:
    fake_gateway["admin_alive"] = False
    assert is_gateway_running(workspace, config) is False


def test_is_gateway_running_false_for_builtin_backend(
    workspace: Workspace, config: Config, fake_gateway
) -> None:
    fake_gateway["backend"] = "builtin"
    fake_gateway["admin_alive"] = True  # 即便 admin 在线，非 caddy 也视为未运行
    assert is_gateway_running(workspace, config) is False


# ---- start_gateway ----------------------------------------------------------


def test_start_gateway_writes_state_and_pid(
    workspace: Workspace, config: Config, fake_gateway
) -> None:
    fake_gateway["admin_alive"] = False
    pid = start_gateway(workspace, config)
    assert pid == 12345
    assert fake_gateway["start_calls"] == 1
    st = read_state(workspace)
    assert st is not None and st.enabled and st.pid == 12345


def test_start_gateway_refreshes_capability_cache_after_success(
    workspace: Workspace, config: Config, fake_gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """截图假红根因：lwa gateway on / start_gateway 成功后须写 capability-gateway.json。"""
    import local_webpage_access.capability as cap_mod
    from local_webpage_access.capability import CapabilityReport

    fake_gateway["admin_alive"] = False
    writes: list[str] = []
    real_write = cap_mod.write_capability_cache

    def fake_collect(**kwargs):  # noqa: ANN003
        return CapabilityReport(
            profile="full",
            overall="ready",
            gateway_access="ready",
            caddy_runtime="ready",
        )

    def fake_write(root, role, report):  # noqa: ANN001
        writes.append(f"{role}:{report.gateway_access}")
        return real_write(root, role, report)

    monkeypatch.setattr(cap_mod, "collect_capability_report", fake_collect)
    monkeypatch.setattr(cap_mod, "log_capability_probe", lambda *a, **k: None)
    monkeypatch.setattr(cap_mod, "write_capability_cache", fake_write)

    pid = start_gateway(workspace, config)
    assert pid == 12345
    assert writes == ["gateway:ready"]
    cache = workspace.root / "run" / "capability-gateway.json"
    assert cache.is_file()
    data = json.loads(cache.read_text(encoding="utf-8"))
    assert data["capabilities"]["gatewayAccess"] == "ready"


def test_start_gateway_refreshes_capability_when_already_running(
    workspace: Workspace, config: Config, fake_gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已在线路径同样刷新能力缓存，避免仅补写 gateway.json 仍假红。"""
    import local_webpage_access.capability as cap_mod
    from local_webpage_access.capability import CapabilityReport

    fake_gateway["admin_alive"] = True
    write_state(
        workspace,
        GatewayState(enabled=True, pid=4321, started_at="t", port=8080),
    )
    refreshed: list[str] = []

    def fake_collect(**kwargs):  # noqa: ANN003
        return CapabilityReport(gateway_access="ready", caddy_runtime="ready")

    def fake_write(root, role, report):  # noqa: ANN001
        refreshed.append(role)
        path = Path(root) / "run" / f"capability-{role}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        return path

    monkeypatch.setattr(cap_mod, "collect_capability_report", fake_collect)
    monkeypatch.setattr(cap_mod, "log_capability_probe", lambda *a, **k: None)
    monkeypatch.setattr(cap_mod, "write_capability_cache", fake_write)

    start_gateway(workspace, config)
    assert refreshed == ["gateway"]


def test_start_gateway_stops_builtin_before_caddy_start(
    workspace: Workspace, config: Config, fake_gateway
) -> None:
    """I1 / §4.1 / BUG-420：stop_all_builtin → write_main_config → caddy_start；清孤儿后 reload。"""
    fake_gateway["admin_alive"] = False
    fake_gateway["stopped_builtin"] = ["demo-static"]
    # 主 Caddyfile 已存在：启动前仍须 write_main_config，启动后因清过 builtin 再 reload
    workspace.static_gateway.mkdir(parents=True, exist_ok=True)
    (workspace.static_gateway / "Caddyfile").write_text(":2019 {}\n", encoding="utf-8")
    start_gateway(workspace, config)
    assert fake_gateway["stop_builtin_calls"] == 1
    assert fake_gateway["write_main_calls"] == 1
    assert fake_gateway["start_calls"] == 1
    assert fake_gateway["call_order"][:3] == [
        "stop_all_builtin",
        "write_main_config",
        "caddy_start",
    ]
    assert fake_gateway["reload_calls"] == 1


def test_start_gateway_writes_main_config_when_no_main(
    workspace: Workspace, config: Config, fake_gateway
) -> None:
    """BUG-420：无主 Caddyfile 时在 caddy_start 前落盘，不再依赖启动后 _sync_main_config。"""
    fake_gateway["admin_alive"] = False
    assert not (workspace.static_gateway / "Caddyfile").exists()
    start_gateway(workspace, config)
    assert fake_gateway["write_main_calls"] == 1
    assert fake_gateway["sync_calls"] == 0
    assert fake_gateway["call_order"].index("write_main_config") < fake_gateway[
        "call_order"
    ].index("caddy_start")


def test_start_gateway_writes_main_config_when_main_exists(
    workspace: Workspace, config: Config, fake_gateway
) -> None:
    """BUG-420：已有非空旧 Caddyfile 时仍须在 caddy_start 前 write_main_config（防旧路径）。"""
    fake_gateway["admin_alive"] = False
    main = workspace.static_gateway / "Caddyfile"
    main.parent.mkdir(parents=True, exist_ok=True)
    main.write_text("# stale old absolute paths\n")
    start_gateway(workspace, config)
    assert fake_gateway["write_main_calls"] == 1
    assert fake_gateway["sync_calls"] == 0
    assert fake_gateway["call_order"].index("write_main_config") < fake_gateway[
        "call_order"
    ].index("caddy_start")


def test_start_gateway_recovers_state_when_already_running(
    workspace: Workspace, config: Config, fake_gateway
) -> None:
    """BUG-073：网关已在线但 gateway.json 缺失 → 补写恢复态，不重复 caddy start。"""
    fake_gateway["admin_alive"] = True  # 已在线
    pid = start_gateway(workspace, config)
    assert fake_gateway["start_calls"] == 0  # 不重复 caddy start
    assert pid == 0  # 无 pidfile（caddy_start 未调用）→ 0
    st = read_state(workspace)
    assert st is not None and st.enabled is True  # BUG-073：补写恢复态


def test_start_gateway_noop_when_already_running_and_state_present(
    workspace: Workspace, config: Config, fake_gateway
) -> None:
    """网关在线且服务态已存在 → 不重写、不重复启动。"""
    fake_gateway["admin_alive"] = True
    write_state(
        workspace,
        GatewayState(enabled=True, pid=4321, started_at="t", port=8080),
    )
    pid = start_gateway(workspace, config)
    assert fake_gateway["start_calls"] == 0
    assert pid == 4321  # 读 caddy.pid（替身未写则用既有 state.pid）
    # state 未被改写（started_at 不变）
    assert read_state(workspace).started_at == "t"


def test_start_gateway_rejects_foreign_admin_before_stopping_builtin(
    workspace: Workspace, config: Config, fake_gateway
) -> None:
    """BUG-302：外部进程占用 admin :2019 时不得停 builtin 或伪报启动成功。"""
    fake_gateway["admin_alive"] = True
    fake_gateway["owner"] = "foreign_process"
    fake_gateway["workspace_match"] = False

    with pytest.raises(LifecycleError, match="非本工作区"):
        start_gateway(workspace, config)

    assert fake_gateway["stop_builtin_calls"] == 0
    assert fake_gateway["start_calls"] == 0


def test_start_gateway_raises_on_caddy_start_failure(
    workspace: Workspace, config: Config, fake_gateway
) -> None:
    fake_gateway["admin_alive"] = False
    fake_gateway["start_ok"] = False
    with pytest.raises(LifecycleError):
        start_gateway(workspace, config)
    assert read_state(workspace) is None  # 失败不写服务态


def test_start_gateway_rejects_non_caddy_backend(
    workspace: Workspace, config: Config, fake_gateway
) -> None:
    fake_gateway["backend"] = "builtin"
    with pytest.raises(LifecycleError):
        start_gateway(workspace, config)
    assert fake_gateway["start_calls"] == 0


def test_start_gateway_creates_and_releases_lock(
    workspace: Workspace, config: Config, fake_gateway
) -> None:
    fake_gateway["admin_alive"] = False
    start_gateway(workspace, config)
    # inode 保留，锁已释放后可再次获取（避免 unlink 导致跨进程双锁）。
    assert start_lock_path(workspace).exists()
    with gateway_start_lock(workspace, timeout=0.1):
        pass


def test_start_gateway_records_switch_event_with_registry(
    workspace: Workspace, config: Config, fake_gateway, registry, monkeypatch
) -> None:
    """建议 F/A：传入 registry 时记录 gateway_backend_switch 事件并刷新地址。"""
    from local_webpage_access.access import RefreshReport

    fake_gateway["admin_alive"] = False
    refreshed = {"called": False}
    monkeypatch.setattr(
        "local_webpage_access.access_workflow.refresh_network_entries",
        lambda ws, cfg, reg: refreshed.__setitem__("called", True) or RefreshReport(),
    )
    start_gateway(workspace, config, registry=registry)
    assert refreshed["called"] is True
    events = registry.list_events(limit=5)
    switch_events = [e for e in events if e["event_type"] == "gateway_backend_switch"]
    assert switch_events, "应记录 gateway_backend_switch 事件"
    assert "backend=caddy" in switch_events[0]["message"]


def test_start_gateway_without_registry_skips_finalize(
    workspace: Workspace, config: Config, fake_gateway, monkeypatch
) -> None:
    """无 registry（lwa init / 自动启动）时不执行收尾、不刷新地址。"""
    fake_gateway["admin_alive"] = False
    called = {"n": 0}
    monkeypatch.setattr(
        "local_webpage_access.access_workflow.refresh_network_entries",
        lambda *a, **kw: called.__setitem__("n", called["n"] + 1),
    )
    start_gateway(workspace, config)  # 不传 registry
    assert called["n"] == 0


# ---- stop_gateway -----------------------------------------------------------


def test_stop_gateway_clears_state(
    workspace: Workspace, config: Config, fake_gateway
) -> None:
    write_state(workspace, GatewayState(enabled=True, pid=12345, started_at="t", port=8080))
    fake_gateway["stop_ok"] = True
    assert stop_gateway(workspace, config) is True
    assert fake_gateway["stop_calls"] == 1
    st = read_state(workspace)
    assert st is not None and st.enabled is False and st.pid is None


def test_stop_gateway_reports_failure(
    workspace: Workspace, config: Config, fake_gateway
) -> None:
    write_state(workspace, GatewayState(enabled=True, pid=12345, started_at="t", port=8080))
    fake_gateway["stop_ok"] = False
    assert stop_gateway(workspace, config) is False
    st = read_state(workspace)
    assert st is not None and st.enabled is True  # 停失败保留原状态


def test_stop_gateway_builtin_clears_stale_state(
    workspace: Workspace, config: Config, fake_gateway
) -> None:
    write_state(workspace, GatewayState(enabled=True, pid=12345, started_at="t", port=8080))
    fake_gateway["backend"] = "builtin"
    assert stop_gateway(workspace, config) is True
    assert fake_gateway["stop_calls"] == 0  # 非 caddy 不调 caddy_stop
    st = read_state(workspace)
    assert st is not None and st.enabled is False


def test_stop_gateway_builtin_still_stops_alive_master(
    workspace: Workspace, config: Config, fake_gateway
) -> None:
    """BUG-077：staticGateway=builtin 但 admin :2019 仍在线（旧 master 残留）时，
    lwa gateway off 仍要 caddy_stop 关掉，兑现 cli 注释承诺。"""
    write_state(workspace, GatewayState(enabled=True, pid=12345, started_at="t", port=8080))
    fake_gateway["backend"] = "builtin"
    fake_gateway["admin_alive"] = True  # 旧 master 仍在跑
    fake_gateway["stop_ok"] = True
    assert stop_gateway(workspace, config) is True
    assert fake_gateway["stop_calls"] == 1  # 关掉残留 master
    st = read_state(workspace)
    assert st is not None and st.enabled is False


# ---- gateway_status ---------------------------------------------------------


def test_gateway_status_running_caddy(
    workspace: Workspace, config: Config, fake_gateway
) -> None:
    fake_gateway["admin_alive"] = True
    fake_gateway["pid"] = 999
    # 写一个 caddy.pid，status 应补读
    workspace.run.mkdir(parents=True, exist_ok=True)
    (workspace.run / "caddy.pid").write_text("999")
    st = gateway_status(workspace, config)
    assert st["running"] is True
    assert st["backend"] == "caddy"
    assert st["pid"] == 999
    assert st["adminPort"] == 2019
    assert st["port"] == config.staticGatewayPort


def test_gateway_status_not_running_no_state(
    workspace: Workspace, config: Config, fake_gateway
) -> None:
    fake_gateway["admin_alive"] = False
    st = gateway_status(workspace, config)
    assert st["running"] is False
    assert st["enabled"] is False
    assert st["pid"] is None


def test_gateway_status_builtin_backend(
    workspace: Workspace, config: Config, fake_gateway
) -> None:
    fake_gateway["backend"] = "builtin"
    fake_gateway["admin_alive"] = False
    st = gateway_status(workspace, config)
    assert st["running"] is False
    assert st["backend"] == "builtin"
    assert st.get("orphanMaster") is False


def test_gateway_status_exposes_orphan_master_when_builtin(
    workspace: Workspace, config: Config, fake_gateway
) -> None:
    """BUG-108：配置已切 builtin 但 admin :2019 仍在线 → running + orphanMaster。"""
    fake_gateway["backend"] = "builtin"
    fake_gateway["admin_alive"] = True
    fake_gateway["pid"] = 75224
    workspace.run.mkdir(parents=True, exist_ok=True)
    (workspace.run / "caddy.pid").write_text("75224")
    st = gateway_status(workspace, config)
    assert st["running"] is True
    assert st["backend"] == "builtin"
    assert st["orphanMaster"] is True
    assert st["pid"] == 75224


# ---- maybe_start_gateway ----------------------------------------------------


def test_maybe_start_gateway_caddy_success(
    workspace: Workspace, config: Config, fake_gateway
) -> None:
    fake_gateway["admin_alive"] = False
    assert maybe_start_gateway(workspace, config) == 12345
    assert fake_gateway["start_calls"] == 1


def test_maybe_start_gateway_skips_non_caddy(
    workspace: Workspace, config: Config, fake_gateway
) -> None:
    fake_gateway["backend"] = "builtin"
    assert maybe_start_gateway(workspace, config) is None
    assert fake_gateway["start_calls"] == 0


def test_maybe_start_gateway_swallows_failure(
    workspace: Workspace, config: Config, fake_gateway
) -> None:
    """caddy 后端但启动失败时不抛，仅返回 None（降级 builtin 不阻断）。"""
    fake_gateway["admin_alive"] = False
    fake_gateway["start_ok"] = False
    assert maybe_start_gateway(workspace, config) is None  # 不抛 LifecycleError


def test_maybe_start_gateway_noop_when_already_running(
    workspace: Workspace, config: Config, fake_gateway
) -> None:
    fake_gateway["admin_alive"] = True
    assert maybe_start_gateway(workspace, config) == 0
    assert fake_gateway["start_calls"] == 0


# ---- gateway_start_lock -----------------------------------------------------


def test_gateway_start_lock_serializes(workspace: Workspace, monkeypatch) -> None:
    """底层文件锁被占用时应抛 LifecycleError。"""
    from local_webpage_access.file_lock import ensure_lockable, try_acquire_exclusive

    lock = start_lock_path(workspace)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR)
    ensure_lockable(fd)
    try_acquire_exclusive(fd)
    try:
        monkeypatch.setattr("time.sleep", lambda *_: None)
        with pytest.raises(LifecycleError):
            with gateway_start_lock(workspace, timeout=0.0):
                pass
    finally:
        os.close(fd)


def test_gateway_start_lock_cleans_up_on_success(workspace: Workspace) -> None:
    with gateway_start_lock(workspace):
        assert start_lock_path(workspace).exists()
    assert start_lock_path(workspace).exists()
    with gateway_start_lock(workspace, timeout=0.1):
        pass


def test_gateway_start_lock_ignores_stale_payload(workspace: Workspace) -> None:
    """BUG-327：陈旧内容不影响 OS 锁获取，也不再按年龄偷锁。"""
    workspace.ensure_workspace_dirs()
    lock = start_lock_path(workspace)
    lock.write_text("999999\n", encoding="utf-8")
    old = time.time() - 120
    os.utime(lock, (old, old))
    with gateway_start_lock(workspace, timeout=0.1):
        assert lock.is_file()
    assert lock.exists()


def test_gateway_start_lock_does_not_steal_live_holder_by_age(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BUG-327：活持锁者即使锁文件很旧也不可被年龄偷锁。"""
    from local_webpage_access.file_lock import (
        ensure_lockable,
        try_acquire_exclusive,
        write_lock_payload,
    )

    workspace.ensure_workspace_dirs()
    lock = start_lock_path(workspace)
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
    ensure_lockable(fd)
    try_acquire_exclusive(fd)
    write_lock_payload(fd, f"{os.getpid()}\n{time.time() - 3600:.3f}\n".encode())
    monkeypatch.setattr("time.sleep", lambda *_: None)
    try:
        with pytest.raises(LifecycleError, match="网关启动锁"):
            with gateway_start_lock(workspace, timeout=0.0):
                pass
    finally:
        os.close(fd)


def test_run_gateway_foreground_refreshes_capability_after_start(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BUG-270：Caddy 启动成功后才写入 capability 缓存，且 gatewayAccess=ready 记 INFO。"""
    import threading

    import local_webpage_access.capability as cap_mod
    from local_webpage_access.capability import CapabilityReport
    from local_webpage_access.gateway_service import run_gateway_foreground

    workspace.ensure_workspace_dirs()
    cfg = Config(staticGateway="caddy", staticGatewayPort=8080)
    probe_order: list[str] = []
    logged: list[tuple[str, str]] = []
    real_write = cap_mod.write_capability_cache

    def fake_start(ws, config):  # noqa: ANN001
        probe_order.append("start")
        return 4242

    def fake_collect(**kwargs):  # noqa: ANN003
        assert "start" in probe_order, "能力探测不得早于 start_gateway"
        probe_order.append("collect")
        return CapabilityReport(
            profile="default",
            overall="ready",
            gateway_access="ready",
            caddy_runtime="ready",
        )

    def fake_log_probe(role, report, *, level="INFO"):  # noqa: ANN001
        logged.append((role, level))

    def fake_write(root, role, report):  # noqa: ANN001
        probe_order.append(f"write:{role}:{report.gateway_access}")
        return real_write(root, role, report)

    monkeypatch.setattr(
        "local_webpage_access.gateway_service.start_gateway", fake_start
    )
    monkeypatch.setattr(
        "local_webpage_access.gateway_service.is_gateway_running", lambda *a, **k: True
    )
    monkeypatch.setattr(
        "local_webpage_access.gateway_service.stop_gateway", lambda *a, **k: None
    )
    monkeypatch.setattr(cap_mod, "collect_capability_report", fake_collect)
    monkeypatch.setattr(cap_mod, "log_capability_probe", fake_log_probe)
    monkeypatch.setattr(cap_mod, "write_capability_cache", fake_write)

    # 立刻触发 stop：首次 start + 能力刷新后进入 wait 即退出
    real_event = threading.Event

    class _ImmediateStop(real_event):
        def wait(self, timeout=None):  # noqa: ANN001
            return True

    monkeypatch.setattr("threading.Event", _ImmediateStop)

    rc = run_gateway_foreground(workspace, cfg, poll_interval=0.01)
    assert rc == 0
    assert probe_order == ["start", "collect", "write:gateway:ready"]
    assert logged == [("gateway", "INFO")]
    cache = workspace.root / "run" / "capability-gateway.json"
    assert cache.is_file()
    data = json.loads(cache.read_text(encoding="utf-8"))
    assert data["capabilities"]["gatewayAccess"] == "ready"


def test_refresh_gateway_capability_merges_backend_caches(
    workspace: Workspace, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BUG-406：gateway 写缓存须 include_backend_cached=True，否则 Full overall 永久假红。"""
    import local_webpage_access.capability as cap_mod
    from local_webpage_access.capability import CapabilityReport
    from local_webpage_access.gateway_service import _refresh_gateway_capability

    seen: dict[str, object] = {}

    def fake_collect(**kwargs):  # noqa: ANN003
        seen.update(kwargs)
        return CapabilityReport(
            profile="full",
            gateway_access="ready",
            caddy_runtime="ready",
            details={"role": "gateway"},
        )

    monkeypatch.setattr(cap_mod, "collect_capability_report", fake_collect)
    monkeypatch.setattr(cap_mod, "log_capability_probe", lambda *a, **k: None)
    monkeypatch.setattr(cap_mod, "write_capability_cache", lambda *a, **k: None)

    _refresh_gateway_capability(workspace, config)
    assert seen.get("include_backend_cached") is True
    assert seen.get("role") == "gateway"
