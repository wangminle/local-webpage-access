"""CHK-326 / issue #33：协调重启自有服务入口。"""

from __future__ import annotations

from typer.testing import CliRunner


def test_services_restart_reuses_updater(monkeypatch, workspace) -> None:
    from local_webpage_access.cli import app

    calls: list[str] = []

    monkeypatch.setattr(
        "local_webpage_access.cli.services.open_workspace_registry",
        lambda: (workspace, object(), _FakeReg()),
    )
    monkeypatch.setattr(
        "local_webpage_access.updater.restart_manager",
        lambda ws, config, *, reconcile=True: calls.append("manager") or {"ok": True},
    )
    monkeypatch.setattr(
        "local_webpage_access.updater.restart_daemon",
        lambda ws, config, *, reconcile=True: calls.append("daemon") or {"ok": True},
    )
    monkeypatch.setattr(
        "local_webpage_access.updater.restart_gateway",
        lambda ws, config, *, reconcile=True: calls.append("gateway") or {"ok": True},
    )
    result = CliRunner().invoke(app, ["services", "restart"])
    assert result.exit_code == 0, result.output
    assert calls == ["manager", "daemon", "gateway"]


def test_services_restart_does_not_call_skip_success(monkeypatch, workspace) -> None:
    from local_webpage_access.cli import app

    monkeypatch.setattr(
        "local_webpage_access.cli.services.open_workspace_registry",
        lambda: (workspace, object(), _FakeReg()),
    )
    monkeypatch.setattr(
        "local_webpage_access.updater.restart_manager",
        lambda *_a, **_k: {"wasRunning": False, "pid": None, "message": "管理页原本未运行，跳过重启"},
    )
    monkeypatch.setattr(
        "local_webpage_access.updater.restart_daemon",
        lambda *_a, **_k: {
            "wasRunning": False,
            "reconciled": False,
            "circuitBlocked": True,
            "pid": None,
            "message": "daemon 启动熔断",
        },
    )
    monkeypatch.setattr(
        "local_webpage_access.updater.restart_gateway",
        lambda *_a, **_k: {
            "wasRunning": False,
            "pid": None,
            "message": "staticGateway=builtin，无需重启 Caddy Gateway",
        },
    )
    result = CliRunner().invoke(app, ["services", "restart"])
    assert result.exit_code != 0
    assert "已协调重启" not in result.output
    assert "跳过" in result.output
    assert "熔断" in result.output
    assert "无需重启" in result.output


class _FakeReg:
    def close(self) -> None:
        return None
