"""IMP-040：status/DTO 读时合成 lanUrl（方案 A）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_webpage_access.config import Config, PortPool
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry
from local_webpage_access.status import instance_status


def _seed(workspace: Workspace, registry: Registry, iid: str = "demo", *, host_port: int = 21000, lan_url: str, route_host: str | None = None, route_url: str | None = None):
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
    route_mode = "name" if route_host else "port"
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
        static=StaticConfig(
            root="public",
            hostPort=host_port,
            routeMode=route_mode,
            routeHost=route_host,
            enabled=True,
        ),
        network=NetworkConfig(
            hostPort=host_port,
            routeMode=route_mode,
            routeHost=route_host,
            routeUrl=route_url,
            lanUrl=lan_url,
        ),
    )
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)
    registry.allocate_port(iid, host_port)
    return manifest


@pytest.fixture()
def ws_reg(tmp_path: Path):
    root = tmp_path / "ws"
    ws = Workspace(root)
    ws.ensure_workspace_dirs()
    reg = Registry(ws.db_path)
    reg.open()
    yield ws, reg
    reg.close()


def test_status_synthesizes_live_lan_url_when_persisted_stale(ws_reg, monkeypatch) -> None:
    """旧落盘 IP + mock 新 IP → DTO lanUrl 已是新地址。"""
    ws, reg = ws_reg
    _seed(ws, reg, lan_url="http://10.0.0.99:21000")
    monkeypatch.setattr(
        "local_webpage_access.ports.resolve_lan_ip", lambda cfg: "192.168.1.50"
    )
    cfg = Config(lanIpStrategy="auto", portPool=PortPool(start=21000, end=21050))
    snap = instance_status(ws, cfg, reg, "demo")
    data = snap.to_dict()
    assert data["lanUrl"] == "http://192.168.1.50:21000"
    assert data["currentLanIp"] == "192.168.1.50"
    assert data["persistedLanIp"] == "10.0.0.99"
    assert data["lanAddressStale"] is True
    assert data["lanUrlSource"] in ("live", "auto")


def test_status_synthesizes_route_url_host_on_drift(ws_reg, monkeypatch) -> None:
    ws, reg = ws_reg
    _seed(
        ws,
        reg,
        lan_url="http://10.0.0.99:21000",
        route_host="demo",
        route_url="http://10.0.0.99:8080/demo/",
    )
    monkeypatch.setattr(
        "local_webpage_access.ports.resolve_lan_ip", lambda cfg: "192.168.1.50"
    )
    cfg = Config(
        lanIpStrategy="auto",
        staticGatewayPort=8080,
        portPool=PortPool(start=21000, end=21050),
    )
    snap = instance_status(ws, cfg, reg, "demo")
    assert snap.route_url == "http://192.168.1.50:8080/demo/"
    assert snap.route_host == "demo"


def test_manual_strategy_uses_manual_ip(ws_reg, monkeypatch) -> None:
    """manual：用配置 IP 合成；不盲信落盘，也不用 detect。"""
    ws, reg = ws_reg
    _seed(ws, reg, lan_url="http://10.0.0.99:21000")
    monkeypatch.setattr(
        "local_webpage_access.ports.detect_lan_ip",
        lambda: (_ for _ in ()).throw(AssertionError("detect 不应被调用")),
    )
    cfg = Config(
        lanIpStrategy="manual",
        manualLanIp="192.168.9.9",
        portPool=PortPool(start=21000, end=21050),
    )
    snap = instance_status(ws, cfg, reg, "demo")
    data = snap.to_dict()
    assert data["lanUrl"] == "http://192.168.9.9:21000"
    assert data["currentLanIp"] == "192.168.9.9"
    assert data["lanUrlSource"] == "manual"
    assert data["lanAddressStale"] is True
