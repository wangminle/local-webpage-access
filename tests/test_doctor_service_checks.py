"""IMP-060 doctor 服务运行态与重启韧性检查测试。

覆盖 060.01（service_runtime_state FAIL/PASS 矩阵）、060.02（restart_resilience
四类 WARN）、060.03（接入 run_doctor 主报告 + 全绿环境零新增告警）。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Sequence

import pytest

from local_webpage_access import autostart as asm
from local_webpage_access.config import load_config
from local_webpage_access.doctor import (
    STATUS_FAIL,
    STATUS_OK,
    STATUS_WARN,
    check_restart_resilience,
    check_service_runtime_state,
    run_doctor,
)
from local_webpage_access.paths import Workspace


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


def _proc(returncode: int, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def _docker_runner(containers: dict[str, str] | None = None):
    """docker 子命令 runner：ps 列容器名，inspect 返回 restart policy。"""

    def runner(args: Sequence[str], **kwargs):  # noqa: ARG001
        if args[:3] == ["docker", "ps", "--no-trunc"]:
            names = "\n".join(containers or {})
            return _proc(0, names)
        if args[:2] == ["docker", "inspect"]:
            name = args[-1]
            return _proc(0, (containers or {}).get(name, "no"))
        return _proc(127)

    return runner


class _FakeBackend:
    """假自启后端：单元文件按 installed 集合存在。

    ``enabled`` 控制服务管理器层面的启用态（BUG-533：文件存在 ≠ enabled）；
    缺省与 installed 相同，保持既有用例行为。
    """

    def __init__(self, installed: set[str], base: Path, enabled: set[str] | None = None) -> None:
        self.installed = installed
        self.enabled_set = set(installed) if enabled is None else enabled
        self.base = base
        base.mkdir(parents=True, exist_ok=True)
        for name in installed:
            (base / f"{name}.unit").write_text("unit", encoding="utf-8")

    def unit_path(self, name: str) -> Path:
        return self.base / f"{name}.unit"

    def is_enabled(self, name: str, runner=None) -> bool:
        return name in self.enabled_set


# ---- 060.01 service_runtime_state -------------------------------------------


def test_runtime_state_enabled_not_running_fails(workspace, config, monkeypatch) -> None:
    _write_state(workspace, "daemon.json", {"enabled": True})
    _write_state(workspace, "manager.json", {"enabled": True})
    from local_webpage_access import daemon as daemon_mod
    from local_webpage_access import gateway_service, manager_service

    monkeypatch.setattr(daemon_mod, "is_running", lambda ws: False)
    monkeypatch.setattr(manager_service, "is_running", lambda ws, cfg: False)
    monkeypatch.setattr(gateway_service, "is_gateway_running", lambda ws, cfg: False)

    result = check_service_runtime_state(workspace, config)
    assert result.status == STATUS_FAIL
    assert "daemon" in result.message and "manager" in result.message
    assert "lwa daemon on" in (result.suggestion or "")
    assert "lwa manager on" in (result.suggestion or "")


def test_runtime_state_enabled_and_running_ok(workspace, config, monkeypatch) -> None:
    # CHK-224#3：disabled+运行中会判残留 WARN，故凡 monkeypatch 为运行中的
    # 服务都必须同时声明 enabled 意图，才是「enabled 且运行中 → OK」形态。
    _write_state(workspace, "daemon.json", {"enabled": True})
    _write_state(workspace, "manager.json", {"enabled": True})
    _write_state(workspace, "gateway.json", {"enabled": True})
    from local_webpage_access import daemon as daemon_mod
    from local_webpage_access import gateway_service, manager_service

    monkeypatch.setattr(daemon_mod, "is_running", lambda ws: True)
    monkeypatch.setattr(manager_service, "is_running", lambda ws, cfg: True)
    monkeypatch.setattr(gateway_service, "is_gateway_running", lambda ws, cfg: True)

    result = check_service_runtime_state(workspace, config)
    assert result.status == STATUS_OK
    assert "enabled 且运行中" in (result.detail or "")


def test_runtime_state_disabled_intent_passes(workspace, config, monkeypatch) -> None:
    """enabled=false → PASS + INFO「已按意图停用」，不误升 FAIL。"""
    from local_webpage_access import daemon as daemon_mod
    from local_webpage_access import gateway_service, manager_service

    monkeypatch.setattr(daemon_mod, "is_running", lambda ws: False)
    monkeypatch.setattr(manager_service, "is_running", lambda ws, cfg: False)
    monkeypatch.setattr(gateway_service, "is_gateway_running", lambda ws, cfg: False)

    result = check_service_runtime_state(workspace, config)
    assert result.status == STATUS_OK
    assert "已按意图停用" in (result.detail or "")


def test_runtime_state_gateway_not_applicable(workspace, monkeypatch) -> None:
    workspace.config_path.write_text(
        workspace.config_path.read_text(encoding="utf-8").replace(
            "staticGateway: caddy", "staticGateway: builtin"
        ),
        encoding="utf-8",
    )
    cfg = load_config(workspace)
    result = check_service_runtime_state(workspace, cfg)
    assert result.status == STATUS_OK
    assert "不适用" in (result.detail or "")


# ---- 060.02 restart_resilience ----------------------------------------------


def test_resilience_no_units_warns_with_full_command(
    workspace, config, monkeypatch, tmp_path
) -> None:
    """事故形态：enabled 服务 + 未装任何自启单元 → WARN + 完整修复命令。"""
    _write_state(workspace, "daemon.json", {"enabled": True})
    backend = _FakeBackend(set(), tmp_path / "units")
    monkeypatch.setattr(asm, "detect_platform", lambda: asm.PLATFORM_LINUX)
    monkeypatch.setattr(asm, "select_backend", lambda *a, **k: backend)
    monkeypatch.setattr(asm, "linger_enabled", lambda **k: False)

    result = check_restart_resilience(workspace, config, runner=_docker_runner())
    assert result.status == STATUS_WARN
    assert "lwa autostart install --with-caddy --linger" in (result.suggestion or "")


def test_resilience_gateway_unit_missing_warns(workspace, config, monkeypatch, tmp_path) -> None:
    """caddy 在用但 gateway 单元缺失（缺 --with-caddy 的形态）→ WARN。"""
    _write_state(workspace, "daemon.json", {"enabled": True})
    _write_state(workspace, "gateway.json", {"enabled": True})
    backend = _FakeBackend({"daemon"}, tmp_path / "units")
    monkeypatch.setattr(asm, "detect_platform", lambda: asm.PLATFORM_LINUX)
    monkeypatch.setattr(asm, "select_backend", lambda *a, **k: backend)
    monkeypatch.setattr(asm, "linger_enabled", lambda **k: True)

    result = check_restart_resilience(workspace, config, runner=_docker_runner())
    assert result.status == STATUS_WARN
    assert "--with-caddy" in (result.suggestion or "")
    # CHK-224#1：逐项差集文案，缺 gateway 单元时点名 gateway + 别名入口失效
    assert "gateway" in (result.detail or "")
    assert "别名入口会失效" in (result.detail or "")


def test_resilience_no_linger_warns(workspace, config, monkeypatch, tmp_path) -> None:
    _write_state(workspace, "daemon.json", {"enabled": True})
    backend = _FakeBackend({"daemon"}, tmp_path / "units")
    monkeypatch.setattr(asm, "detect_platform", lambda: asm.PLATFORM_LINUX)
    monkeypatch.setattr(asm, "select_backend", lambda *a, **k: backend)
    monkeypatch.setattr(asm, "linger_enabled", lambda **k: False)

    result = check_restart_resilience(workspace, config, runner=_docker_runner())
    assert result.status == STATUS_WARN
    assert "enable-linger" in (result.suggestion or "")


def test_resilience_container_policy_mismatch_warns(
    workspace, config, monkeypatch, tmp_path
) -> None:
    _write_state(workspace, "daemon.json", {"enabled": True})
    backend = _FakeBackend({"daemon"}, tmp_path / "units")
    monkeypatch.setattr(asm, "detect_platform", lambda: asm.PLATFORM_LINUX)
    monkeypatch.setattr(asm, "select_backend", lambda *a, **k: backend)
    monkeypatch.setattr(asm, "linger_enabled", lambda **k: True)

    runner = _docker_runner({"lwa-v1": "no", "lwa-ok": "unless-stopped"})
    result = check_restart_resilience(workspace, config, runner=runner)
    assert result.status == STATUS_WARN
    assert "lwa-v1" in (result.detail or "")


def test_resilience_all_green_no_warn(workspace, config, monkeypatch, tmp_path) -> None:
    """全绿环境：单元齐 + linger + 策略符合 → OK，不引入噪声。"""
    _write_state(workspace, "daemon.json", {"enabled": True})
    _write_state(workspace, "manager.json", {"enabled": True})
    _write_state(workspace, "gateway.json", {"enabled": True})
    backend = _FakeBackend({"daemon", "manager", "gateway"}, tmp_path / "units")
    monkeypatch.setattr(asm, "detect_platform", lambda: asm.PLATFORM_LINUX)
    monkeypatch.setattr(asm, "select_backend", lambda *a, **k: backend)
    monkeypatch.setattr(asm, "linger_enabled", lambda **k: True)

    runner = _docker_runner({"lwa-v1": "unless-stopped"})
    result = check_restart_resilience(workspace, config, runner=runner)
    assert result.status == STATUS_OK


def test_resilience_unit_installed_but_disabled_warns(
    workspace, config, monkeypatch, tmp_path
) -> None:
    """BUG-533：单元文件存在但未被服务管理器启用 → WARN，不得报 OK。"""
    _write_state(workspace, "daemon.json", {"enabled": True})
    _write_state(workspace, "manager.json", {"enabled": True})
    _write_state(workspace, "gateway.json", {"enabled": True})
    # 三份单元文件都在，但全部 disabled（实证事故形态）
    backend = _FakeBackend({"daemon", "manager", "gateway"}, tmp_path / "units", enabled=set())
    monkeypatch.setattr(asm, "detect_platform", lambda: asm.PLATFORM_LINUX)
    monkeypatch.setattr(asm, "select_backend", lambda *a, **k: backend)
    monkeypatch.setattr(asm, "linger_enabled", lambda **k: True)

    runner = _docker_runner({"lwa-v1": "unless-stopped"})
    result = check_restart_resilience(workspace, config, runner=runner)
    assert result.status == STATUS_WARN
    assert "未被服务管理器启用" in (result.detail or "")
    assert "lwa autostart enable" in (result.suggestion or "")


def test_resilience_bare_process_stays_warn_not_fail(
    workspace, config, monkeypatch, tmp_path
) -> None:
    """裸进程是合法模式：enabled + 无单元只 WARN，不升 FAIL。"""
    _write_state(workspace, "daemon.json", {"enabled": True})
    backend = _FakeBackend(set(), tmp_path / "units")
    monkeypatch.setattr(asm, "detect_platform", lambda: asm.PLATFORM_LINUX)
    monkeypatch.setattr(asm, "select_backend", lambda *a, **k: backend)

    result = check_restart_resilience(workspace, config, runner=_docker_runner())
    assert result.status == STATUS_WARN


# ---- 060.03 接入主报告 -------------------------------------------------------


def test_run_doctor_includes_new_checks(workspace, config, monkeypatch) -> None:
    """两项新检查进入默认 doctor 报告。"""
    monkeypatch.setattr(
        "local_webpage_access.doctor.check_service_runtime_state",
        lambda ws, cfg: type("R", (), {"name": "service_runtime_state", "status": STATUS_OK})(),
    )
    monkeypatch.setattr(
        "local_webpage_access.doctor.check_restart_resilience",
        lambda ws, cfg, **k: type("R", (), {"name": "restart_resilience", "status": STATUS_WARN})(),
    )
    report = run_doctor(workspace, config, runner=_docker_runner())
    names = [c.name for c in report.checks]
    assert "service_runtime_state" in names
    assert "restart_resilience" in names


# ---- CHK-224 复核修复 -------------------------------------------------------


def test_resilience_partial_install_warns_missing_manager(
    workspace, config, monkeypatch, tmp_path
) -> None:
    """CHK-224#1：daemon+manager enabled 但只装了 daemon 单元 → WARN（不误报 OK）。"""
    _write_state(workspace, "daemon.json", {"enabled": True})
    _write_state(workspace, "manager.json", {"enabled": True})
    backend = _FakeBackend({"daemon"}, tmp_path / "units")
    monkeypatch.setattr(asm, "detect_platform", lambda: asm.PLATFORM_LINUX)
    monkeypatch.setattr(asm, "select_backend", lambda *a, **k: backend)
    monkeypatch.setattr(asm, "linger_enabled", lambda **k: True)

    result = check_restart_resilience(workspace, config, runner=_docker_runner())
    assert result.status == STATUS_WARN
    assert "manager" in (result.detail or "")
    assert "缺自启单元" in (result.detail or "")
    assert "lwa autostart install --with-caddy --linger" in (result.suggestion or "")


def test_resilience_missing_daemon_unit_warns_even_if_gateway_present(
    workspace, config, monkeypatch, tmp_path
) -> None:
    """差集对任一 enabled 服务生效（反向：缺 daemon 单元也告警）。"""
    _write_state(workspace, "daemon.json", {"enabled": True})
    _write_state(workspace, "gateway.json", {"enabled": True})
    backend = _FakeBackend({"gateway"}, tmp_path / "units")
    monkeypatch.setattr(asm, "detect_platform", lambda: asm.PLATFORM_LINUX)
    monkeypatch.setattr(asm, "select_backend", lambda *a, **k: backend)
    monkeypatch.setattr(asm, "linger_enabled", lambda **k: True)

    result = check_restart_resilience(workspace, config, runner=_docker_runner())
    assert result.status == STATUS_WARN
    assert "daemon" in (result.detail or "")


def test_runtime_state_disabled_but_running_warns_residual(workspace, config, monkeypatch) -> None:
    """CHK-224#3：已停用但进程仍在（残留）→ WARN，不假绿。"""
    from local_webpage_access import daemon as daemon_mod
    from local_webpage_access import gateway_service, manager_service

    monkeypatch.setattr(daemon_mod, "is_running", lambda ws: True)  # 残留
    monkeypatch.setattr(manager_service, "is_running", lambda ws, cfg: False)
    monkeypatch.setattr(gateway_service, "is_gateway_running", lambda ws, cfg: False)

    result = check_service_runtime_state(workspace, config)
    assert result.status == STATUS_WARN
    assert "daemon" in result.message
    assert "残留" in result.message
    assert "lwa daemon off" in (result.suggestion or "")


def test_runtime_state_disabled_not_running_still_ok(workspace, config, monkeypatch) -> None:
    """disabled 且确实未运行 → 维持 OK（不因新增观测引入噪声）。"""
    from local_webpage_access import daemon as daemon_mod
    from local_webpage_access import gateway_service, manager_service

    monkeypatch.setattr(daemon_mod, "is_running", lambda ws: False)
    monkeypatch.setattr(manager_service, "is_running", lambda ws, cfg: False)
    monkeypatch.setattr(gateway_service, "is_gateway_running", lambda ws, cfg: False)

    result = check_service_runtime_state(workspace, config)
    assert result.status == STATUS_OK
    assert "已按意图停用" in (result.detail or "")


def test_runtime_state_fail_takes_precedence_over_residual(workspace, config, monkeypatch) -> None:
    """同时存在 enabled 未运行与残留进程：FAIL 优先（更严重的故障先报）。"""
    _write_state(workspace, "manager.json", {"enabled": True})
    from local_webpage_access import daemon as daemon_mod
    from local_webpage_access import gateway_service, manager_service

    monkeypatch.setattr(daemon_mod, "is_running", lambda ws: True)  # daemon 残留
    monkeypatch.setattr(manager_service, "is_running", lambda ws, cfg: False)  # enabled 未运行
    monkeypatch.setattr(gateway_service, "is_gateway_running", lambda ws, cfg: False)

    result = check_service_runtime_state(workspace, config)
    assert result.status == STATUS_FAIL
    assert "manager" in result.message
    assert "残留" in (result.detail or "")
