"""IMP-059 服务级期望态 reconcile 测试。

覆盖：

* 059.01 ``service_intent`` 意图判定（状态文件缺失 / enabled / 交叉校验不一致 /
  staticGateway 非 caddy）；
* 059.02 三态重启决策（running 重启 / enabled+停拉起 / disabled 跳过）；
* 059.03 拉起与监督器协调（autostart 在管走 systemctl/kickstart，不双进程）；
* 059.04 中断时长与报告字段（unexpectedDown / downSince）；
* 059.05 ``--no-reconcile`` 逃生舱回到纯观察态。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_webpage_access import updater as upd
from local_webpage_access.config import load_config
from local_webpage_access.paths import Workspace
from local_webpage_access.service_intent import (
    INTENT_DISABLED,
    INTENT_ENABLED,
    INTENT_NOT_APPLICABLE,
    estimate_down_since,
    format_down_duration,
    service_intent,
)


# ---- fixtures --------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path: Path) -> Workspace:
    ws = Workspace(tmp_path / "ws")
    ws.ensure_workspace_dirs()
    ws.config_path.write_text(
        "managerPort: 17800\n"
        "managerHost: 127.0.0.1\n"
        "managerEnabled: true\n"
        "portPool:\n"
        "  start: 21000\n"
        "  end: 21050\n"
        "staticGateway: caddy\n",
        encoding="utf-8",
    )
    return ws


@pytest.fixture()
def config(workspace: Workspace):
    return load_config(workspace)


def _write_state(ws: Workspace, filename: str, payload: dict) -> None:
    ws.run.mkdir(parents=True, exist_ok=True)
    (ws.run / filename).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ---- 059.01 意图判定纯函数 ---------------------------------------------------


def test_intent_all_missing_states_disabled(workspace, config) -> None:
    intent = service_intent(workspace, config)
    assert intent.daemon == INTENT_DISABLED
    assert intent.manager == INTENT_DISABLED
    assert intent.gateway == INTENT_DISABLED


def test_intent_enabled_flags(workspace, config) -> None:
    _write_state(workspace, "daemon.json", {"enabled": True})
    _write_state(workspace, "manager.json", {"enabled": True})
    _write_state(workspace, "gateway.json", {"enabled": True})
    intent = service_intent(workspace, config)
    assert intent.daemon == INTENT_ENABLED
    assert intent.manager == INTENT_ENABLED
    assert intent.gateway == INTENT_ENABLED


def test_intent_disabled_flags(workspace, config) -> None:
    _write_state(workspace, "daemon.json", {"enabled": False})
    _write_state(workspace, "manager.json", {"enabled": False})
    _write_state(workspace, "gateway.json", {"enabled": False})
    intent = service_intent(workspace, config)
    assert intent.daemon == INTENT_DISABLED
    assert intent.manager == INTENT_DISABLED
    assert intent.gateway == INTENT_DISABLED


def test_intent_manager_config_cross_check_disabled(workspace, config) -> None:
    """manager.json enabled=true 但 managerEnabled=false → disabled（交叉校验）。"""
    _write_state(workspace, "manager.json", {"enabled": True})
    ws2 = workspace
    ws2.config_path.write_text(
        ws2.config_path.read_text(encoding="utf-8").replace(
            "managerEnabled: true", "managerEnabled: false"
        ),
        encoding="utf-8",
    )
    cfg2 = load_config(ws2)
    intent = service_intent(ws2, cfg2)
    assert intent.manager == INTENT_DISABLED


def test_intent_gateway_not_applicable_builtin(workspace) -> None:
    workspace.config_path.write_text(
        workspace.config_path.read_text(encoding="utf-8").replace(
            "staticGateway: caddy", "staticGateway: builtin"
        ),
        encoding="utf-8",
    )
    cfg = load_config(workspace)
    _write_state(workspace, "gateway.json", {"enabled": True})
    intent = service_intent(workspace, cfg)
    assert intent.gateway == INTENT_NOT_APPLICABLE


def test_intent_corrupt_state_file_is_disabled(workspace, config) -> None:
    (workspace.run / "daemon.json").write_text("not-json{", encoding="utf-8")
    intent = service_intent(workspace, config)
    assert intent.daemon == INTENT_DISABLED


# ---- 059.04 中断时长 --------------------------------------------------------


def test_estimate_down_since_daemon_uses_lock_heartbeat(workspace) -> None:
    _write_state(workspace, "daemon.json", {"enabled": True, "started_at": None})
    (workspace.run / "daemon.lock").write_text("123\n1690000000.0\n", encoding="utf-8")
    assert estimate_down_since("daemon", workspace) == 1690000000.0


def test_estimate_down_since_manager_uses_started_at(workspace) -> None:
    _write_state(
        workspace,
        "manager.json",
        {"enabled": True, "started_at": "2026-08-17T03:00:00+00:00"},
    )
    ts = estimate_down_since("manager", workspace)
    assert ts is not None
    assert abs(ts - 1786935600.0) < 5  # 2026-08-17T03:00:00Z epoch


def test_estimate_down_since_missing_returns_none(workspace) -> None:
    assert estimate_down_since("manager", workspace) is None
    assert estimate_down_since("daemon", workspace) is None


def test_format_down_duration_humanizes() -> None:
    import time

    now = 1786935600.0
    assert format_down_duration(None) == ""
    assert "秒" in format_down_duration(now - 30, now=now)
    assert "分钟" in format_down_duration(now - 300, now=now)
    assert "小时" in format_down_duration(now - 3.5 * 3600, now=now)
    assert "天" in format_down_duration(now - 2 * 86400, now=now)
    text = format_down_duration(now - 3600, now=now)
    assert text.startswith("，中断约")
    # 未来时刻（时钟漂移）按 0 秒处理，不出现负数
    assert "秒" in format_down_duration(now + 60, now=now)
    assert time.time() > 0  # silence linter on unused import


# ---- 059.02/03 三态重启决策 --------------------------------------------------


@pytest.fixture()
def started_manager_state(workspace) -> None:
    """manager：enabled=true 但 is_running=False（进程已死的现场）。"""
    _write_state(
        workspace,
        "manager.json",
        {"enabled": True, "started_at": "2026-08-17T03:00:00+00:00"},
    )


def test_restart_manager_reconciles_enabled_but_down(
    workspace, config, started_manager_state, monkeypatch
) -> None:
    """enabled=true 且未运行 → 拉起 + unexpectedDown 标注（059.02/04）。"""
    monkeypatch.setattr("local_webpage_access.manager_service.is_running", lambda ws, cfg: False)
    monkeypatch.setattr(
        "local_webpage_access.manager_service.start_manager",
        lambda ws, cfg, **kw: 4321,
    )
    monkeypatch.setattr(upd, "verify_manager_version", lambda cfg, **kw: (True, "V0.7.11-test"))
    # 无 autostart 单元在管 → managed=False → detached start
    from local_webpage_access import autostart as asm

    monkeypatch.setattr(asm, "select_backend", lambda *a, **k: _NoUnitBackend())

    info = upd.restart_manager(workspace, config)
    assert info["wasRunning"] is False
    assert info["reconciled"] is True
    assert info["unexpectedDown"] is True
    assert info["downSince"] is not None
    assert "意外未运行" in info["message"]
    assert "已恢复" in info["message"]
    assert info["pid"] == 4321


def test_restart_manager_reconcile_via_autostart_managed(
    workspace, config, started_manager_state, monkeypatch
) -> None:
    """autostart 在管 → 走监督器 start（managed），不 detached spawn（059.03）。"""
    from local_webpage_access import autostart as asm

    monkeypatch.setattr("local_webpage_access.manager_service.is_running", lambda ws, cfg: False)
    spawned = []
    monkeypatch.setattr(
        "local_webpage_access.manager_service.start_manager",
        lambda ws, cfg: spawned.append("detached") or 999,
    )
    monkeypatch.setattr(upd, "verify_manager_version", lambda cfg, **kw: (True, "V0.7.11-test"))
    monkeypatch.setattr(asm, "select_backend", lambda *a, **k: _ManagedBackend())

    info = upd.restart_manager(workspace, config)
    assert info["reconciled"] is True
    assert "自启动单元拉起" in info["message"]
    assert spawned == []  # 不得出现双进程


def test_restart_manager_no_reconcile_keeps_skip(
    workspace, config, started_manager_state, monkeypatch
) -> None:
    """--no-reconcile：回到纯观察态，未运行一律跳过（059.05）。"""
    monkeypatch.setattr("local_webpage_access.manager_service.is_running", lambda ws, cfg: False)
    monkeypatch.setattr(
        "local_webpage_access.manager_service.start_manager",
        lambda ws, cfg: pytest.fail("no-reconcile 下不得拉起"),
    )
    info = upd.restart_manager(workspace, config, reconcile=False)
    assert info["wasRunning"] is False
    assert "跳过重启" in info["message"]
    assert "reconciled" not in info


def test_restart_manager_disabled_intent_skips(workspace, config, monkeypatch) -> None:
    """enabled=false（状态文件 enabled=false）→ 跳过，文案不变。"""
    _write_state(workspace, "manager.json", {"enabled": False})
    monkeypatch.setattr("local_webpage_access.manager_service.is_running", lambda ws, cfg: False)
    monkeypatch.setattr(
        "local_webpage_access.manager_service.start_manager",
        lambda ws, cfg: pytest.fail("disabled 意图下不得拉起"),
    )
    info = upd.restart_manager(workspace, config)
    assert info["wasRunning"] is False
    assert info["message"] == "管理页原本未运行，跳过重启"


def test_restart_daemon_reconciles_enabled_but_down(workspace, config, monkeypatch) -> None:
    _write_state(workspace, "daemon.json", {"enabled": True})
    (workspace.run / "daemon.lock").write_text("123\n1690000000.0\n", encoding="utf-8")
    from local_webpage_access import daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "is_running", lambda ws: False)
    monkeypatch.setattr(daemon_mod, "start_daemon", lambda ws, cfg, **kw: 777)
    from local_webpage_access import autostart as asm

    monkeypatch.setattr(asm, "select_backend", lambda *a, **k: _NoUnitBackend())

    info = upd.restart_daemon(workspace, config)
    assert info["reconciled"] is True
    assert info["unexpectedDown"] is True
    assert info["pid"] == 777
    assert "意外未运行" in info["message"]


def test_restart_daemon_no_reconcile_skips(workspace, config, monkeypatch) -> None:
    _write_state(workspace, "daemon.json", {"enabled": True})
    from local_webpage_access import daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "is_running", lambda ws: False)
    monkeypatch.setattr(daemon_mod, "start_daemon", lambda ws, cfg: pytest.fail("不得拉起"))
    info = upd.restart_daemon(workspace, config, reconcile=False)
    assert info["message"] == "daemon 原本未运行，跳过重启"


def test_restart_gateway_reconciles_enabled_but_down(workspace, config, monkeypatch) -> None:
    _write_state(
        workspace,
        "gateway.json",
        {"enabled": True, "started_at": "2026-08-17T03:00:00+00:00"},
    )
    monkeypatch.setattr(
        "local_webpage_access.gateway_service.is_gateway_running",
        lambda ws, cfg: False,
    )
    monkeypatch.setattr(
        "local_webpage_access.gateway_service.start_gateway",
        lambda ws, cfg, **kw: 555,
    )
    from local_webpage_access import autostart as asm

    monkeypatch.setattr(asm, "select_backend", lambda *a, **k: _NoUnitBackend())

    info = upd.restart_gateway(workspace, config)
    assert info["reconciled"] is True
    assert info["unexpectedDown"] is True
    assert info["pid"] == 555
    assert "意外未运行" in info["message"]


def test_restart_gateway_non_caddy_skips(workspace, monkeypatch) -> None:
    workspace.config_path.write_text(
        workspace.config_path.read_text(encoding="utf-8").replace(
            "staticGateway: caddy", "staticGateway: builtin"
        ),
        encoding="utf-8",
    )
    cfg = load_config(workspace)
    monkeypatch.setattr(
        "local_webpage_access.gateway_service.is_gateway_running",
        lambda ws, c: pytest.fail("staticGateway!=caddy 不该探测 gateway"),
    )
    info = upd.restart_gateway(workspace, cfg)
    assert info["message"].startswith("staticGateway=builtin")


# ---- 测试用后端 -------------------------------------------------------------


class _NoUnitBackend:
    """无单元文件的后端（autostart 未安装 → managed=False）。"""

    def unit_path(self, name: str) -> Path:
        return Path("/nonexistent") / f"{name}.unit"


class _ManagedBackend(_NoUnitBackend):
    """单元已启用且 start 成功的后端（managed=True）。"""

    def __init__(self) -> None:
        import tempfile

        self.started: list[str] = []
        fd, path = tempfile.mkstemp(suffix=".unit")
        import os

        os.close(fd)
        self._unit_file = Path(path)

    def unit_path(self, name: str) -> Path:
        return self._unit_file

    def is_loaded(self, name, runner) -> bool:  # noqa: ARG002
        return True

    def is_enabled(self, name, runner) -> bool:  # noqa: ARG002
        return True

    def start(self, name, runner):  # noqa: ARG002
        from local_webpage_access.autostart import CmdOutcome

        self.started.append(name)
        return [CmdOutcome(["fake", "start"], 0, "", "")], True


# ---- issue #4：gateway 陈旧 gateway.json 不得虚报中断时长 ---------------------


def _write_gateway_stale(ws: Workspace, stale_pid: int = 3836952) -> None:
    """模拟监督器接管场景：gateway.json 停在 8/10 裸进程记录，pid 已死。"""
    _write_state(
        ws,
        "gateway.json",
        {"enabled": True, "pid": stale_pid, "started_at": "2026-08-10T22:32:00+00:00"},
    )


def test_estimate_gateway_live_master_no_estimate(workspace, monkeypatch) -> None:
    """issue #4：caddy.pid 的 pid 存活（admin 瞬断）-> 不估算中断时长。"""
    import local_webpage_access.daemon as dm

    _write_gateway_stale(workspace)
    (workspace.run / "caddy.pid").write_text("12345", encoding="utf-8")
    monkeypatch.setattr(dm, "is_pid_alive", lambda pid: pid == 12345)
    monkeypatch.setattr(dm, "pid_cmdline_contains", lambda pid, *n: True)
    assert estimate_down_since("gateway", workspace) is None


def test_estimate_gateway_pid_mismatch_discards_stale_json(workspace, monkeypatch) -> None:
    """issue #4：pidfile pid 与 json pid 不一致（换过 master）-> 丢弃陈旧 started_at。"""
    import local_webpage_access.daemon as dm

    _write_gateway_stale(workspace)
    (workspace.run / "caddy.pid").write_text("24680", encoding="utf-8")
    monkeypatch.setattr(dm, "is_pid_alive", lambda pid: False)
    # 监督器在管（systemd 时间戳不可得时）-> None，而非按 8/10 记录虚报 7.6 天
    monkeypatch.setattr(
        "local_webpage_access.autostart.service_supervision_mode",
        lambda name, **k: "systemd 监管",
    )
    assert estimate_down_since("gateway", workspace) is None


def test_estimate_gateway_systemd_inactive_timestamp(workspace, monkeypatch) -> None:
    """issue #4：systemd 在管且单元 inactive -> 用 InactiveEnterTimestamp 作中断起点。"""
    import local_webpage_access.autostart as asm

    _write_gateway_stale(workspace)
    # 无 pidfile：监督器路径
    from datetime import datetime

    class _FakeRes:
        returncode = 0
        stdout = "inactive\nTue 2026-08-18 06:00:00 UTC\n"

    def fake_run(cmd, **kwargs):
        assert "systemctl" in cmd and "InactiveEnterTimestamp" in " ".join(cmd)
        return _FakeRes()

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(
        asm, "service_supervision_mode", lambda name, **k: asm.SERVICE_MODE_SYSTEMD
    )
    ts = estimate_down_since("gateway", workspace)
    assert ts is not None
    # 本地时区解释的 2026-08-18 06:00:00（naive astimezone），与直接换算一致
    expected = datetime.strptime("Tue 2026-08-18 06:00:00", "%a %Y-%m-%d %H:%M:%S")
    assert abs(ts - expected.astimezone().timestamp()) < 5


def test_estimate_gateway_bare_uses_started_at(workspace, monkeypatch) -> None:
    """裸进程模式（无监督器、无 pidfile）：沿用 json.started_at（原语义不变）。"""
    import local_webpage_access.autostart as asm

    _write_gateway_stale(workspace)
    monkeypatch.setattr(asm, "service_supervision_mode", lambda name, **k: asm.SERVICE_MODE_BARE)
    ts = estimate_down_since("gateway", workspace)
    # 2026-08-10T22:32:00+00:00 对应 epoch
    assert ts is not None and abs(ts - 1786401120.0) < 5
