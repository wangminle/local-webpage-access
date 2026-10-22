"""IMP-040：status/DTO 读时合成 lanUrl（方案 A）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_webpage_access.config import Config, PortPool
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry
from local_webpage_access.status import instance_status


def _seed(
    workspace: Workspace,
    registry: Registry,
    iid: str = "demo",
    *,
    host_port: int = 21000,
    lan_url: str,
    route_host: str | None = None,
    route_url: str | None = None,
):
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
    # 本用例模拟原生 LAN，避免宿主 WSL 快照覆盖 mock IP。
    monkeypatch.setattr(
        "local_webpage_access.wsl_network.collect_wsl_network_snapshot", lambda: None
    )
    ws, reg = ws_reg
    _seed(ws, reg, lan_url="http://10.0.0.99:21000")
    monkeypatch.setattr("local_webpage_access.ports.resolve_lan_ip", lambda cfg: "192.168.1.50")
    cfg = Config(lanIpStrategy="auto", portPool=PortPool(start=21000, end=21050))
    snap = instance_status(ws, cfg, reg, "demo")
    data = snap.to_dict()
    assert data["lanUrl"] == "http://192.168.1.50:21000"
    assert data["currentLanIp"] == "192.168.1.50"
    assert data["persistedLanIp"] == "10.0.0.99"
    assert data["lanAddressStale"] is True
    assert data["lanUrlSource"] in ("live", "auto")


def test_status_synthesizes_route_url_host_on_drift(ws_reg, monkeypatch) -> None:
    # 本用例模拟原生 LAN，避免宿主 WSL 快照覆盖 mock IP。
    monkeypatch.setattr(
        "local_webpage_access.wsl_network.collect_wsl_network_snapshot", lambda: None
    )
    ws, reg = ws_reg
    _seed(
        ws,
        reg,
        lan_url="http://10.0.0.99:21000",
        route_host="demo",
        route_url="http://10.0.0.99:8080/demo/",
    )
    monkeypatch.setattr("local_webpage_access.ports.resolve_lan_ip", lambda cfg: "192.168.1.50")
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
    # 本用例模拟原生 LAN，避免宿主 WSL 快照覆盖手动 IP。
    monkeypatch.setattr(
        "local_webpage_access.wsl_network.collect_wsl_network_snapshot", lambda: None
    )
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


def test_status_wsl_nat_does_not_advertise_guest_ip_as_lan(ws_reg, monkeypatch) -> None:
    """BUG-923：DTO 保留 guest lanUrl，但标明仅宿主可达且无局域网入口。"""
    from tests.scripts.test_wsl_network import _yi14_snapshot

    ws, reg = ws_reg
    _seed(ws, reg, lan_url="http://172.22.148.94:21000", route_host="hello-lwa")
    monkeypatch.setattr("local_webpage_access.ports.resolve_lan_ip", lambda cfg: "172.22.148.94")
    monkeypatch.setattr(
        "local_webpage_access.wsl_network.collect_wsl_network_snapshot",
        lambda **_k: _yi14_snapshot(),
    )
    cfg = Config(
        lanIpStrategy="auto",
        staticGatewayPort=8080,
        portPool=PortPool(start=21000, end=21050),
    )
    data = instance_status(ws, cfg, reg, "demo").to_dict()
    assert data["lanUrl"] == "http://172.22.148.94:21000"
    assert data["lanReachability"] == "windows-host"
    assert data["lanExposure"] == "entry-missing"
    assert data["hostLanIp"] == "10.180.105.74"
    assert data["hostLanUrl"] is None
    assert data["wslNetworkMode"] == "nat"
    assert data["lanAddressStale"] is False


def test_status_wsl_nat_manual_ip_does_not_replace_guest_lan_url(
    ws_reg, monkeypatch
) -> None:
    """草案 §3.3：不能把 resolve_lan_ip / manualLanIp 直接换成 Windows IP。"""
    from tests.scripts.test_wsl_network import _yi14_snapshot

    ws, reg = ws_reg
    _seed(ws, reg, lan_url="http://172.22.148.94:21000", route_host="hello-lwa")
    monkeypatch.setattr(
        "local_webpage_access.wsl_network.collect_wsl_network_snapshot",
        lambda **_k: _yi14_snapshot(),
    )
    cfg = Config(
        lanIpStrategy="manual",
        manualLanIp="10.180.105.74",
        staticGatewayPort=8080,
        portPool=PortPool(start=21000, end=21050),
    )
    data = instance_status(ws, cfg, reg, "demo").to_dict()
    assert data["lanUrl"] == "http://172.22.148.94:21000"
    assert data["lanReachability"] == "windows-host"
    assert data["hostLanIp"] == "10.180.105.74"
