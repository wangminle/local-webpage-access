from __future__ import annotations

import logging
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from local_webpage_access import build_process
from local_webpage_access.atomic_io import atomic_write_text
from local_webpage_access.autostart import MacLaunchdBackend
from local_webpage_access.config import Config
from local_webpage_access.doctor import STATUS_OK, check_port_contention
from local_webpage_access.gateway_service import GatewayState, read_state, start_gateway, stop_gateway, write_state
from local_webpage_access.import_activity import import_activity_lock
from local_webpage_access.lifecycle import _observe_static_status
from local_webpage_access.models import (
    DesiredState,
    InstanceManifest,
    Kind,
    ResourceProfile,
    Runtime,
    ServingMode,
    Status,
)
from local_webpage_access.paths import Workspace
from local_webpage_access.static_gateway import StaticGateway
from tests.scripts.test_gateway_service import fake_gateway as _fake_gateway_fixture  # noqa: F401,F811


def test_permission_error_still_calls_single_process_kill(monkeypatch) -> None:
    class Proc:
        pid = 4321
        killed = False

        def poll(self):
            return None

        def kill(self):
            self.killed = True

    proc = Proc()
    monkeypatch.setattr(build_process.os, "getpgid", lambda pid: 99)
    monkeypatch.setattr(build_process.os, "killpg", lambda *args: (_ for _ in ()).throw(PermissionError("denied")))
    monkeypatch.setattr(build_process.sys, "platform", "linux")
    build_process.kill_process_tree(proc, grace_seconds=0)
    assert proc.killed is True


def test_launchd_disable_requires_final_unloaded(monkeypatch) -> None:
    backend = MacLaunchdBackend()
    monkeypatch.setattr(backend, "is_enabled", lambda name, runner: False)
    monkeypatch.setattr(backend, "is_loaded", lambda name, runner: True)
    def runner(cmd, **kwargs):
        return CompletedProcess(cmd, 0, "", "")

    _outcomes, ok = backend.disable("manager", runner)
    assert ok is False


def test_import_activity_closes_fd_when_lock_setup_fails(workspace: Workspace, monkeypatch) -> None:
    closed: list[int] = []
    monkeypatch.setattr(
        "local_webpage_access.import_activity.ensure_lockable",
        lambda fd: (_ for _ in ()).throw(OSError("read-only")),
    )
    monkeypatch.setattr(
        "local_webpage_access.import_activity.os.close",
        lambda fd: closed.append(fd),
    )
    with pytest.raises(OSError, match="read-only"):
        with import_activity_lock(workspace):
            pass
    assert len(closed) == 1


def test_static_status_falls_back_to_http_when_pid_read_fails(workspace, registry, config, monkeypatch) -> None:
    iid = "demo"
    manifest = InstanceManifest(
        id=iid,
        name=iid,
        version="1",
        kind=Kind.STATIC,
        runtime=Runtime.SHARED_STATIC,
        servingMode=ServingMode.SHARED_STATIC,
        resourceProfile=ResourceProfile.TINY,
        status=Status.RUNNING,
        desiredState=DesiredState.RUNNING,
    )
    registry.upsert_from_manifest(manifest)
    registry.upsert_static_site(iid, {"enabled": True, "hostPort": 18000})
    pid_path = workspace.run / f"static-{iid}.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("123", encoding="utf-8")
    original_read = Path.read_text

    def fail_pid_read(self, *args, **kwargs):
        if self == pid_path:
            raise PermissionError("denied")
        return original_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_pid_read)
    monkeypatch.setattr(StaticGateway, "health_check", lambda self, port: True)
    assert _observe_static_status(workspace, config, registry, iid) == Status.RUNNING


def test_gateway_log_keeps_previous_pid(workspace, config, _fake_gateway_fixture, caplog) -> None:  # noqa: F811
    write_state(workspace, GatewayState(enabled=True, pid=123, started_at="old", port=8080))
    _fake_gateway_fixture["pid"] = 999
    _fake_gateway_fixture["admin_alive"] = True
    workspace.run.mkdir(parents=True, exist_ok=True)
    (workspace.run / "caddy.pid").write_text("999", encoding="utf-8")
    with caplog.at_level(logging.INFO):
        start_gateway(workspace, config)
    assert "json pid=123，live pid=999" in caplog.text


def test_doctor_allows_empty_entry_port_set(workspace, monkeypatch) -> None:
    config = Config(staticGateway="caddy", staticGatewayPort=None, gatewayTls="off")
    gateway = StaticGateway(workspace, config)
    pid_path = gateway.caddy_pid_path()
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("123", encoding="utf-8")
    monkeypatch.setattr("local_webpage_access.doctor._list_listeners", lambda port: [("caddy", "123")])
    monkeypatch.setattr(StaticGateway, "_admin_alive", lambda self: True)
    result = check_port_contention(workspace, config)
    assert result.status == STATUS_OK


def test_stop_gateway_failure_preserves_pid(workspace, config, _fake_gateway_fixture) -> None:  # noqa: F811
    write_state(workspace, GatewayState(enabled=True, pid=12345, started_at="old", port=8080))
    _fake_gateway_fixture["backend"] = "builtin"
    _fake_gateway_fixture["admin_alive"] = True
    _fake_gateway_fixture["stop_ok"] = False
    assert stop_gateway(workspace, config) is False
    state = read_state(workspace)
    assert state is not None
    assert state.enabled is False
    assert state.pid == 12345


def test_atomic_write_failure_preserves_previous_file(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "state.json"
    path.write_text("old", encoding="utf-8")
    monkeypatch.setattr("local_webpage_access.atomic_io.os.replace", lambda *args: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(OSError, match="full"):
        atomic_write_text(path, "new")
    assert path.read_text(encoding="utf-8") == "old"
    monkeypatch.undo()
    atomic_write_text(path, "new")
    assert path.read_text(encoding="utf-8") == "new"
