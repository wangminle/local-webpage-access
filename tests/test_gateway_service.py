"""``gateway_service`` 单测（IMP-010 / DEV-041，WBS 0.2 / 0.9）。

用可控的 ``StaticGateway`` 替身覆盖服务层逻辑：状态读写、启停、探活、降级。
真实 Caddy 子进程交互由 ``tests/test_static_gateway.py`` 的 ``caddy_start/stop``
单测覆盖，此处只验证服务编排与 ``run/gateway.json`` 状态。
"""

from __future__ import annotations

import contextlib
import json
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
        "stop_builtin_calls": 0,
        "reload_calls": 0,
        "call_order": [],
        "stopped_builtin": [],
        "pid": 12345,
    }

    class _Fake:
        def __init__(self, ws: Workspace, cfg: Config) -> None:
            self.ws = ws
            self.cfg = cfg

        def detect_backend(self) -> str:
            return state["backend"]

        def _admin_alive(self, **kw) -> bool:
            return state["admin_alive"]

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

        def _sync_main_config(self) -> None:
            # BUG-074：start_gateway 在无主 Caddyfile 时应调此方法加载真实站点。
            state["sync_calls"] += 1

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


def test_start_gateway_stops_builtin_before_caddy_start(
    workspace: Workspace, config: Config, fake_gateway
) -> None:
    """I1 / §4.1：先 stop_all_builtin，再 caddy_start；清过孤儿且主配置已存在则 reload。"""
    fake_gateway["admin_alive"] = False
    fake_gateway["stopped_builtin"] = ["demo-static"]
    # 主 Caddyfile 已存在时走 reload 分支（无主配置时走 _sync_main_config）
    workspace.static_gateway.mkdir(parents=True, exist_ok=True)
    (workspace.static_gateway / "Caddyfile").write_text(":2019 {}\n", encoding="utf-8")
    start_gateway(workspace, config)
    assert fake_gateway["stop_builtin_calls"] == 1
    assert fake_gateway["start_calls"] == 1
    assert fake_gateway["call_order"][:2] == ["stop_all_builtin", "caddy_start"]
    assert fake_gateway["reload_calls"] == 1


def test_start_gateway_syncs_main_config_when_no_main(
    workspace: Workspace, config: Config, fake_gateway
) -> None:
    """BUG-074：caddy_start 用 bootstrap（无主 Caddyfile）后应 sync 真实站点配置。"""
    fake_gateway["admin_alive"] = False
    assert not (workspace.static_gateway / "Caddyfile").exists()
    start_gateway(workspace, config)
    assert fake_gateway["sync_calls"] == 1


def test_start_gateway_skips_sync_when_main_exists(
    workspace: Workspace, config: Config, fake_gateway
) -> None:
    """主 Caddyfile 已存在且非空时 caddy_start 已加载它，无需再 sync。"""
    fake_gateway["admin_alive"] = False
    main = workspace.static_gateway / "Caddyfile"
    main.parent.mkdir(parents=True, exist_ok=True)
    main.write_text("# real config\n")
    start_gateway(workspace, config)
    assert fake_gateway["sync_calls"] == 0


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
    # 启动结束后锁文件应被清理（即便成功路径）
    assert not start_lock_path(workspace).exists()


def test_start_gateway_records_switch_event_with_registry(
    workspace: Workspace, config: Config, fake_gateway, registry, monkeypatch
) -> None:
    """建议 F/A：传入 registry 时记录 gateway_backend_switch 事件并刷新地址。"""
    from local_webpage_access.access import RefreshReport

    fake_gateway["admin_alive"] = False
    refreshed = {"called": False}
    monkeypatch.setattr(
        "local_webpage_access.access.refresh_network_entries",
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
        "local_webpage_access.access.refresh_network_entries",
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
    """锁文件被占用时应抛 LifecycleError。"""
    lock = start_lock_path(workspace)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("0\n", encoding="utf-8")  # 模拟他人持锁
    # 缩短超时避免拖慢测试
    monkeypatch.setattr("time.sleep", lambda *_: None)
    with pytest.raises(LifecycleError):
        with gateway_start_lock(workspace, timeout=0.0):
            pass


def test_gateway_start_lock_cleans_up_on_success(workspace: Workspace) -> None:
    with gateway_start_lock(workspace):
        assert start_lock_path(workspace).exists()
    assert not start_lock_path(workspace).exists()
