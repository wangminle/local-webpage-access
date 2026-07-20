"""IMP-038/040：共享 access_workflow 编排与节流 refresh。"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from local_webpage_access.access import RefreshReport
from local_webpage_access.config import Config, PortPool
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry


def _seed(workspace: Workspace, registry: Registry, iid: str = "demo", *, host_port: int = 21000, lan_url: str):
    from local_webpage_access.models import (
        DesiredState,
        InstanceManifest,
        Kind,
        NetworkConfig,
        ResourceProfile,
        Runtime,
        ServingMode,
        StaticConfig,
        Status,
    )

    workspace.ensure_app_dirs(iid)
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
        static=StaticConfig(root="public", hostPort=host_port, enabled=True),
        network=NetworkConfig(hostPort=host_port, lanUrl=lan_url),
    )
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)
    registry.allocate_port(iid, host_port)


@pytest.fixture()
def env(tmp_path: Path):
    root = tmp_path / "ws"
    ws = Workspace(root)
    ws.ensure_workspace_dirs()
    reg = Registry(ws.db_path)
    reg.open()
    cfg = Config(lanIpStrategy="auto", portPool=PortPool(start=21000, end=21050))
    yield ws, cfg, reg
    reg.close()


def test_run_access_pass_dry_run_skips(env) -> None:
    from local_webpage_access.access_workflow import run_access_pass

    ws, cfg, reg = env
    result = run_access_pass(ws, cfg, reg, review=True, dry_run=True)
    assert result.skipped is True
    assert result.refresh is None
    assert result.review is None


def test_throttled_refresh_writes_once_within_window(env, monkeypatch) -> None:
    """040.01：漂移后首次落盘；窗口内二次调用不重复全量写。"""
    from local_webpage_access import access_workflow as aw

    ws, cfg, reg = env
    _seed(ws, reg, lan_url="http://10.0.0.99:21000")
    monkeypatch.setattr(
        "local_webpage_access.ports.resolve_lan_ip", lambda c: "192.168.1.50"
    )
    monkeypatch.setattr(
        "local_webpage_access.access.resolve_lan_ip", lambda c: "192.168.1.50"
    )
    aw.reset_lan_refresh_throttle_state()

    calls = {"n": 0}
    real = aw.refresh_network_entries

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(aw, "refresh_network_entries", counting)

    r1 = aw.maybe_throttled_lan_refresh(ws, cfg, reg, min_interval=60.0)
    assert r1 is not None
    assert calls["n"] == 1
    r2 = aw.maybe_throttled_lan_refresh(ws, cfg, reg, min_interval=60.0)
    assert r2 is None
    assert calls["n"] == 1


def test_throttled_refresh_single_flight(env, monkeypatch) -> None:
    from local_webpage_access import access_workflow as aw

    ws, cfg, reg = env
    _seed(ws, reg, lan_url="http://10.0.0.99:21000")
    monkeypatch.setattr(
        "local_webpage_access.ports.resolve_lan_ip", lambda c: "192.168.1.50"
    )
    monkeypatch.setattr(
        "local_webpage_access.access.resolve_lan_ip", lambda c: "192.168.1.50"
    )
    aw.reset_lan_refresh_throttle_state()

    started = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    def slow_refresh(*a, **k):
        calls["n"] += 1
        started.set()
        release.wait(timeout=2)
        return RefreshReport(lan_ip="192.168.1.50")

    monkeypatch.setattr(aw, "refresh_network_entries", slow_refresh)

    results: list = []

    def worker():
        results.append(aw.maybe_throttled_lan_refresh(ws, cfg, reg, min_interval=60.0))

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    assert started.wait(timeout=1)
    t2.start()
    release.set()
    t1.join(timeout=2)
    t2.join(timeout=2)
    assert calls["n"] == 1
    assert sum(1 for r in results if r is not None) == 1


def test_manual_strategy_does_not_auto_refresh(env, monkeypatch) -> None:
    from local_webpage_access import access_workflow as aw

    ws, cfg, reg = env
    cfg.lanIpStrategy = "manual"
    cfg.manualLanIp = "192.168.9.9"
    _seed(ws, reg, lan_url="http://10.0.0.99:21000")
    monkeypatch.setattr(
        "local_webpage_access.ports.detect_lan_ip", lambda: "192.168.1.50"
    )
    aw.reset_lan_refresh_throttle_state()
    calls = {"n": 0}
    monkeypatch.setattr(
        aw,
        "refresh_network_entries",
        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1),
    )
    assert aw.maybe_throttled_lan_refresh(ws, cfg, reg) is None
    assert calls["n"] == 0
