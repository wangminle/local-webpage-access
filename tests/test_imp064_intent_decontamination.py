"""IMP-064 回归：服务意图字段去污染（§16 验收标准）。

核心契约：``run/{manager,daemon,gateway}.json`` 的 ``enabled`` 仅表用户意图；
启动失败写 ``last_start_error`` / ``consecutive_start_failures`` 观测；
熔断只拦 update 的 reconcile 自动拉起；子进程/前台退出不翻意图。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from local_webpage_access.config import Config
from local_webpage_access.paths import Workspace
from local_webpage_access.service_failures import (
    LastStartError,
    clear_start_failures,
    failure_note,
    parse_consecutive_failures,
    parse_last_start_error,
    record_start_failure,
    start_failure_circuit_open,
)


@pytest.fixture()
def ws(tmp_path: Path) -> Workspace:
    w = Workspace(tmp_path / "ws")
    w.ensure_workspace_dirs()
    return w


@pytest.fixture()
def cfg() -> Config:
    from local_webpage_access.config import PortPool

    return Config(staticGateway="builtin", portPool=PortPool(start=21000, end=21050))


# ---- 064.01：状态模型扩展 ----------------------------------------------------


def test_parse_legacy_state_without_failure_fields() -> None:
    """旧状态文件（无新字段）读默认值，不做 schema 迁移。"""
    data = {"enabled": True, "pid": 123, "started_at": "t"}
    assert parse_last_start_error(data) is None
    assert parse_consecutive_failures(data) == 0

    # 损坏字段容错
    bad = {
        "last_start_error": "not-a-dict",
        "consecutive_start_failures": "x",
    }
    assert parse_last_start_error(bad) is None
    assert parse_consecutive_failures(bad) == 0


def test_state_roundtrip_with_failure_fields(ws: Workspace) -> None:
    """失败观测写入 / 读回状态文件往返一致。"""
    from local_webpage_access.manager_service import ManagerState, read_state, write_state

    state = ManagerState(enabled=True, pid=None, host="0.0.0.0", port=17800)
    record_start_failure(state, "健康检查超时", source="update-restart")
    write_state(ws, state)

    loaded = read_state(ws)
    assert loaded is not None
    assert loaded.enabled is True
    assert loaded.consecutive_start_failures == 1
    assert loaded.last_start_error is not None
    assert loaded.last_start_error.message == "健康检查超时"
    assert loaded.last_start_error.source == "update-restart"
    # JSON 序列化包含两个观测字段
    raw = json.loads((ws.run / "manager.json").read_text(encoding="utf-8"))
    assert "last_start_error" in raw
    assert "consecutive_start_failures" in raw


def test_record_and_clear_start_failures() -> None:
    state = LastStartErrorHolder()
    record_start_failure(state, "失败 A")
    record_start_failure(state, "失败 B", source="reconcile")
    assert state.consecutive_start_failures == 2
    assert state.last_start_error.message == "失败 B"
    assert failure_note(state) is not None and "连续失败 2 次" in failure_note(state)
    clear_start_failures(state)
    assert state.consecutive_start_failures == 0
    assert state.last_start_error is None
    assert failure_note(state) is None


class LastStartErrorHolder:
    """最小状态对象（record/clear 只依赖两个字段）。"""

    def __init__(self) -> None:
        self.last_start_error: LastStartError | None = None
        self.consecutive_start_failures = 0


# ---- 064.04：熔断器（纯函数）-------------------------------------------------


def _state_with_failures(count: int, at: str, source: str = "reconcile") -> LastStartErrorHolder:
    s = LastStartErrorHolder()
    s.consecutive_start_failures = count
    s.last_start_error = LastStartError(message="启动失败", at=at, source=source)
    return s


def test_circuit_opens_at_threshold_within_window() -> None:
    """连续 ≥3 次且 24h 内 → 熔断。"""
    import time

    now = time.time()
    assert start_failure_circuit_open(_state_with_failures(3, _iso(now))) is True
    assert start_failure_circuit_open(_state_with_failures(5, _iso(now))) is True
    # 阈值之下不熔断
    assert start_failure_circuit_open(_state_with_failures(2, _iso(now))) is False


def test_circuit_cools_down_after_window() -> None:
    """最近一次失败距今 >24h → 放行再试一次（计数保留）。"""
    import time

    now = time.time()
    old = now - 24 * 3600 - 60
    assert start_failure_circuit_open(_state_with_failures(3, _iso(old))) is False
    # 窗口边缘（恰好 24h 内）仍熔断
    assert start_failure_circuit_open(_state_with_failures(3, _iso(now - 3600))) is True


def test_circuit_no_failure_record_never_opens() -> None:
    s = LastStartErrorHolder()
    s.consecutive_start_failures = 10  # 计数但无 last_error（异常现场）
    assert start_failure_circuit_open(s) is False


def _iso(ts: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# ---- 064.02/03/06：三服务行为 ----------------------------------------------


def test_manager_start_failure_keeps_intent_and_records(
    ws: Workspace, cfg: Config, monkeypatch
) -> None:
    """验收：start 失败（健康超时）→ enabled=true + lastStartError + pid 已清。"""
    import local_webpage_access.manager_service as ms

    monkeypatch.setattr(ms, "_spawn_manager", lambda ws_: 424242)
    monkeypatch.setattr(ms, "_wait_for_health", lambda config, **kw: False)
    monkeypatch.setattr(ms, "is_pid_alive", lambda pid: False)
    monkeypatch.setattr(ms, "maybe_start_gateway", lambda *a, **k: None)
    # 隔离本机可能运行的真实 manager/端口占用
    monkeypatch.setattr(ms, "health_matches_workspace", lambda *a, **k: False)
    monkeypatch.setattr(ms, "health_ok", lambda *a, **k: False)

    from local_webpage_access.errors import LifecycleError

    with pytest.raises(LifecycleError):
        ms.start_manager(ws, cfg)

    state = ms.read_state(ws)
    assert state is not None
    assert state.enabled is True  # 意图未被污染
    assert state.pid is None  # pid 已清
    assert state.last_start_error is not None
    assert "健康检查超时" in state.last_start_error.message
    assert state.consecutive_start_failures == 1

    # 第二次失败 → 计数累积
    with pytest.raises(LifecycleError):
        ms.start_manager(ws, cfg)
    state = ms.read_state(ws)
    assert state is not None
    assert state.consecutive_start_failures == 2
    assert state.enabled is True


def test_manager_early_exit_clears_failure_count(
    ws: Workspace, cfg: Config, monkeypatch
) -> None:
    """验收（规则 10）：已在运行早退清零 consecutiveStartFailures。"""
    import local_webpage_access.manager_service as ms

    ms.write_state(
        ws,
        ms.ManagerState(
            enabled=True,
            pid=777,
            host="0.0.0.0",
            port=cfg.managerPort,
        ),
    )
    state = ms.read_state(ws)
    assert state is not None
    record_start_failure(state, "旧失败")
    ms.write_state(ws, state)

    monkeypatch.setattr(ms, "is_running", lambda ws_, cfg_: True)
    assert ms.start_manager(ws, cfg) == 777
    state = ms.read_state(ws)
    assert state is not None
    assert state.consecutive_start_failures == 0
    assert state.last_start_error is None


def test_manager_off_resets_failures(ws: Workspace, cfg: Config, monkeypatch) -> None:
    """验收（规则 3/064.06）：用户级 off 写 enabled=False 并重置失败记录。"""
    import local_webpage_access.manager_service as ms

    state = ms.ManagerState(enabled=True, pid=None, host="0.0.0.0", port=cfg.managerPort)
    record_start_failure(state, "失败")
    ms.write_state(ws, state)

    assert ms.stop_manager(ws) is True
    state = ms.read_state(ws)
    assert state is not None
    assert state.enabled is False
    assert state.consecutive_start_failures == 0
    assert state.last_start_error is None


def test_stop_manager_internal_keeps_intent(ws: Workspace, cfg: Config, monkeypatch) -> None:
    """验收（064.03）：内部停止只清 pid，不改 enabled、不重置失败记录。"""
    import local_webpage_access.manager_service as ms

    state = ms.ManagerState(enabled=True, pid=None, host="0.0.0.0", port=cfg.managerPort)
    record_start_failure(state, "失败")
    ms.write_state(ws, state)

    monkeypatch.setattr(ms, "find_listening_pid", lambda port: None)
    monkeypatch.setattr(
        ms,
        "health_matches_workspace",
        lambda *a, **k: False,
    )
    assert ms.stop_manager_internal(ws) is True
    state = ms.read_state(ws)
    assert state is not None
    assert state.enabled is True  # 意图保持
    assert state.consecutive_start_failures == 1  # 失败记录保持（由 start 成败决定）
    assert state.pid is None


def test_gateway_residual_online_does_not_flip_intent(
    ws: Workspace, cfg: Config, monkeypatch
) -> None:
    """验收（规则 9）：enabled=False 且 Caddy 在线 → 残留进程，不把意图翻回 True。"""
    import local_webpage_access.gateway_service as gs
    import local_webpage_access.static_gateway as sg

    monkeypatch.setattr(sg.StaticGateway, "detect_backend", lambda self: "caddy")
    monkeypatch.setattr(gs, "is_gateway_running", lambda ws_, cfg_: True)
    monkeypatch.setattr(gs, "_read_caddy_pid", lambda gw: 999)
    monkeypatch.setattr(sg.StaticGateway, "write_main_config", lambda self: None)
    monkeypatch.setattr(sg.StaticGateway, "stop_all_builtin", lambda self: [])
    monkeypatch.setattr(sg.StaticGateway, "reload_all", lambda self: None)
    monkeypatch.setattr(gs, "_post_switch_finalize", lambda *a, **k: None)
    monkeypatch.setattr(gs, "_refresh_gateway_capability", lambda *a, **k: None)

    gs.write_state(ws, gs.GatewayState(enabled=False, pid=None))
    gs.start_gateway(ws, cfg)

    state = gs.read_state(ws)
    assert state is not None
    assert state.enabled is False  # 残留进程不翻意图


def test_gateway_off_then_manager_on_does_not_relink(
    ws: Workspace, cfg: Config, monkeypatch
) -> None:
    """验收：`gateway off` 后 `manager on` 联动不把 gateway 翻回 True。"""
    import local_webpage_access.gateway_service as gs
    import local_webpage_access.static_gateway as sg

    monkeypatch.setattr(sg.StaticGateway, "detect_backend", lambda self: "caddy")
    gs.write_state(ws, gs.GatewayState(enabled=False))
    # 状态文件缺失（从未 on）同样跳过
    (ws.run / "gateway.json").unlink()

    started: list[str] = []
    monkeypatch.setattr(gs, "start_gateway", lambda *a, **k: started.append("x") or 0)
    assert gs.maybe_start_gateway(ws, cfg) is None
    assert started == []


def test_manager_subprocess_exit_keeps_intent(ws: Workspace, cfg: Config, monkeypatch) -> None:
    """验收（064.03b）：run_service_main 的 finally 只清 pid，不写 enabled=False。"""
    import local_webpage_access.manager_service as ms

    # 构造最小可运行现场
    from local_webpage_access.config import example_config_text

    ws.config_path.write_text(example_config_text(), encoding="utf-8")
    monkeypatch.setattr(
        "local_webpage_access.platform_support.require_supported_platform", lambda: None
    )
    monkeypatch.setattr(
        "local_webpage_access.manager_api.run_manager", lambda workspace, config: None
    )

    # 预置「监督器在管」状态：enabled=True + 旧 pid（非本进程）
    ms.write_state(
        ws,
        ms.ManagerState(enabled=True, pid=999999, host="127.0.0.1", port=cfg.managerPort),
    )

    argv = sys.argv
    try:
        sys.argv = ["lwa-manager", "--workspace", str(ws.root)]
        rc = ms.run_service_main()
    finally:
        sys.argv = argv
    assert rc == 0

    state = ms.read_state(ws)
    assert state is not None
    assert state.enabled is True  # SIGTERM/正常退出不翻意图
    assert state.pid is None  # 观测已清


def test_gateway_foreground_exit_keeps_intent(ws: Workspace, monkeypatch) -> None:
    """验收（064.03b）：前台监管退出走内部停止，enabled 保持 true、pid 已清。"""
    import local_webpage_access.gateway_service as gs
    import threading

    cfg = Config(staticGateway="caddy")
    monkeypatch.setattr(gs, "start_gateway", lambda *a, **k: 4242)
    monkeypatch.setattr(gs, "is_gateway_running", lambda *a, **k: True)
    monkeypatch.setattr(gs, "_refresh_gateway_capability", lambda *a, **k: None)

    # 真实 stop_gateway_internal：patch StaticGateway.caddy_stop → True
    import local_webpage_access.static_gateway as sg

    monkeypatch.setattr(sg.StaticGateway, "caddy_stop", lambda self: True)
    gs.write_state(ws, gs.GatewayState(enabled=True, pid=4242))

    real_event = threading.Event

    class _ImmediateStop(real_event):
        def wait(self, timeout=None):  # noqa: ANN001
            return True

    monkeypatch.setattr(threading, "Event", _ImmediateStop)
    assert gs.run_gateway_foreground(ws, cfg, poll_interval=0.01) == 0

    state = gs.read_state(ws)
    assert state is not None
    assert state.enabled is True  # 前台退出不翻意图
    assert state.pid is None


# ---- 064.04：updater 熔断 ----------------------------------------------------


def test_update_reconcile_blocked_by_circuit(ws: Workspace, cfg: Config, monkeypatch) -> None:
    """验收：连续 3 次 reconcile 拉起失败（24h 内）→ update 跳过自动拉起并明示。"""
    import local_webpage_access.manager_service as ms
    import local_webpage_access.updater as upd

    # enabled=true、未运行、连续失败 3 次（最近一次刚刚）
    state = ms.ManagerState(enabled=True, pid=None, host="0.0.0.0", port=cfg.managerPort)
    for _ in range(3):
        record_start_failure(state, "健康检查超时", source="reconcile")
    ms.write_state(ws, state)

    monkeypatch.setattr(ms, "is_running", lambda ws_, cfg_: False)
    started: list[int] = []
    monkeypatch.setattr(
        ms, "start_manager", lambda ws_, cfg_, **kw: started.append(1) or 1
    )
    from tests.test_service_intent import _NoUnitBackend  # noqa: PLC0415
    from local_webpage_access import autostart as asm

    monkeypatch.setattr(asm, "select_backend", lambda *a, **k: _NoUnitBackend())

    info = upd.restart_manager(ws, cfg)
    assert info.get("circuitBlocked") is True
    assert "熔断" in info["message"]
    assert "lwa manager on" in info["message"]
    assert started == []  # 未自动拉起


def test_update_reconcile_allowed_after_cooldown(ws: Workspace, cfg: Config, monkeypatch) -> None:
    """验收：冷却过期（>24h）允许再试一次。"""
    import time
    from datetime import datetime, timezone

    import local_webpage_access.manager_service as ms
    import local_webpage_access.updater as upd

    old_at = datetime.fromtimestamp(
        time.time() - 24 * 3600 - 300, tz=timezone.utc
    ).isoformat()
    state = ms.ManagerState(enabled=True, pid=None, host="0.0.0.0", port=cfg.managerPort)
    state.consecutive_start_failures = 3
    state.last_start_error = LastStartError(
        message="旧失败", at=old_at, source="reconcile"
    )
    ms.write_state(ws, state)

    monkeypatch.setattr(ms, "is_running", lambda ws_, cfg_: False)
    monkeypatch.setattr(
        upd, "verify_manager_version", lambda cfg_, **kw: (True, "Vtest")
    )
    monkeypatch.setattr(ms, "start_manager", lambda ws_, cfg_, **kw: 1234)
    from tests.test_service_intent import _NoUnitBackend  # noqa: PLC0415
    from local_webpage_access import autostart as asm

    monkeypatch.setattr(asm, "select_backend", lambda *a, **k: _NoUnitBackend())

    info = upd.restart_manager(ws, cfg)
    assert info.get("unexpectedDown") is True
    assert "已恢复" in info["message"]


def test_manual_on_not_blocked_by_circuit(ws: Workspace, cfg: Config, monkeypatch) -> None:
    """验收：熔断不挡手动 `lwa manager on`（熔断只在 updater reconcile 层）。"""
    import local_webpage_access.manager_service as ms

    state = ms.ManagerState(enabled=True, pid=None, host="0.0.0.0", port=cfg.managerPort)
    for _ in range(5):
        record_start_failure(state, "失败", source="reconcile")
    ms.write_state(ws, state)

    monkeypatch.setattr(ms, "_spawn_manager", lambda ws_: 555)
    monkeypatch.setattr(ms, "_wait_for_health", lambda config, **kw: True)
    monkeypatch.setattr(ms, "maybe_start_gateway", lambda *a, **k: None)
    monkeypatch.setattr(ms, "health_matches_workspace", lambda *a, **k: False)
    monkeypatch.setattr(ms, "health_ok", lambda *a, **k: False)

    # 手动 on：start_manager 本身无熔断判定
    assert ms.start_manager(ws, cfg) == 555
    state = ms.read_state(ws)
    assert state is not None
    # 手动 on 成功清零（熔断解除）
    assert state.consecutive_start_failures == 0


# ---- 064.05：doctor 消费 -----------------------------------------------------


def test_doctor_fail_includes_last_start_error(ws: Workspace, cfg: Config) -> None:
    """验收：enabled=true 未运行 → FAIL 文案含上次失败原因。"""
    import local_webpage_access.manager_service as ms
    from local_webpage_access.doctor import check_service_runtime_state

    state = ms.ManagerState(enabled=True, pid=None, host="0.0.0.0", port=cfg.managerPort)
    record_start_failure(state, "健康检查超时（port=17800）", source="update-restart")
    ms.write_state(ws, state)

    result = check_service_runtime_state(ws, cfg)
    assert result.status == "fail"
    assert "健康检查超时" in result.message
    assert "上次启动失败" in result.detail


def test_doctor_fail_marks_circuit(ws: Workspace, cfg: Config) -> None:
    """验收：熔断状态在 doctor FAIL 中明示。"""
    import local_webpage_access.manager_service as ms
    from local_webpage_access.doctor import check_service_runtime_state

    state = ms.ManagerState(enabled=True, pid=None, host="0.0.0.0", port=cfg.managerPort)
    for _ in range(3):
        record_start_failure(state, "反复失败", source="reconcile")
    ms.write_state(ws, state)

    result = check_service_runtime_state(ws, cfg)
    assert result.status == "fail"
    assert "熔断" in result.message


def test_doctor_pass_when_user_off(ws: Workspace, cfg: Config) -> None:
    """验收：用户主动 off →「已按意图停用」PASS（去污染后语义真实）。"""
    from local_webpage_access.doctor import check_service_runtime_state

    result = check_service_runtime_state(ws, cfg)
    assert result.status == "ok"
    assert "已按意图停用" in result.detail


# ---- 064.05：status 透出 -----------------------------------------------------


def test_status_exposes_failure_fields(ws: Workspace, cfg: Config) -> None:
    """验收：status 摘要透出 lastStartError / consecutiveStartFailures。"""
    import local_webpage_access.daemon as dm
    import local_webpage_access.manager_service as ms

    state = ms.ManagerState(enabled=True, pid=None, host="0.0.0.0", port=cfg.managerPort)
    record_start_failure(state, "失败原因 X", source="manual")
    ms.write_state(ws, state)

    status = ms.manager_status(ws, cfg)
    assert status["consecutiveStartFailures"] == 1
    assert status["lastStartError"] is not None
    assert status["lastStartError"]["message"] == "失败原因 X"
    assert "失败原因 X" in (status["lastStartErrorNote"] or "")

    # daemon 无失败 → 零值
    dstatus = dm.daemon_status(ws)
    assert dstatus["consecutiveStartFailures"] == 0
    assert dstatus["lastStartError"] is None
