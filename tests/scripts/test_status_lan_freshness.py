"""IMP-040：status/DTO 读时合成 lanUrl（方案 A）。"""

from __future__ import annotations

import threading
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


def test_status_consumes_persisted_lan_observation(ws_reg, monkeypatch) -> None:
    from local_webpage_access.access import record_lan_observation
    from local_webpage_access.models import InstanceManifest
    from local_webpage_access.wsl_network import EXPOSURE_EXTERNALLY_VERIFIED
    from tests.scripts.test_wsl_network import YI14_NETIP, YI14_NETROUTE, YI14_WSL_VERSION
    from local_webpage_access.wsl_network import InteropOutputs, collect_wsl_network_snapshot

    ws, reg = ws_reg
    _seed(
        ws,
        reg,
        lan_url="http://172.22.148.94:21000",
        route_host="hello-lwa",
        route_url="http://172.22.148.94:8080/hello-lwa/",
    )
    outputs = InteropOutputs(
        wsl_version_text=YI14_WSL_VERSION,
        wslinfo_text="nat\n",
        netip_text=YI14_NETIP,
        netroute_text=YI14_NETROUTE,
        portproxy_text="0.0.0.0            8080       127.0.0.1         8080\n",
    )
    snap = collect_wsl_network_snapshot(
        platform="wsl", guest_ip="172.22.148.94", outputs=outputs, use_cache=False
    )
    monkeypatch.setattr(
        "local_webpage_access.wsl_network.collect_wsl_network_snapshot",
        lambda **_k: snap,
    )
    monkeypatch.setattr("local_webpage_access.ports.resolve_lan_ip", lambda cfg: "172.22.148.94")
    cfg = Config(
        lanIpStrategy="auto",
        staticGatewayPort=8080,
        portPool=PortPool(start=21000, end=21050),
    )
    record_lan_observation(
        ws,
        cfg,
        "demo",
        vantage="lan-client",
        protocol="http",
        evidence="content-match",
        status_code=200,
        url="http://10.180.105.74:8080/hello-lwa/",
    )
    saved = InstanceManifest.load(ws.app_manifest_path("demo"))
    assert saved.network.externalLanObservation["vantage"] == "lan-client"
    data = instance_status(ws, cfg, reg, "demo").to_dict()
    assert data["lanExposure"] == EXPOSURE_EXTERNALLY_VERIFIED
    assert data["lanObservation"]["protocol"] == "http"
    assert data["lanObservationStale"] is False


def test_status_invalidates_observation_after_network_change(ws_reg, monkeypatch) -> None:
    from local_webpage_access.access import record_lan_observation
    from local_webpage_access.wsl_network import EXPOSURE_CANDIDATE_UNVERIFIED
    from tests.scripts.test_wsl_network import YI14_NETIP, YI14_NETROUTE, YI14_WSL_VERSION
    from local_webpage_access.wsl_network import InteropOutputs, collect_wsl_network_snapshot

    ws, reg = ws_reg
    _seed(
        ws,
        reg,
        lan_url="http://172.22.148.94:21000",
        route_host="hello-lwa",
        route_url="http://172.22.148.94:8080/hello-lwa/",
    )
    outputs = InteropOutputs(
        wsl_version_text=YI14_WSL_VERSION,
        wslinfo_text="nat\n",
        netip_text=YI14_NETIP,
        netroute_text=YI14_NETROUTE,
        portproxy_text="0.0.0.0            8080       127.0.0.1         8080\n",
    )
    snap = collect_wsl_network_snapshot(
        platform="wsl", guest_ip="172.22.148.94", outputs=outputs, use_cache=False
    )
    monkeypatch.setattr(
        "local_webpage_access.wsl_network.collect_wsl_network_snapshot",
        lambda **_k: snap,
    )
    monkeypatch.setattr("local_webpage_access.ports.resolve_lan_ip", lambda cfg: "172.22.148.94")
    cfg = Config(
        lanIpStrategy="auto",
        staticGatewayPort=8080,
        portPool=PortPool(start=21000, end=21050),
    )
    record_lan_observation(
        ws,
        cfg,
        "demo",
        vantage="lan-client",
        protocol="http",
        evidence="page",
        status_code=200,
        url="http://10.180.105.74:8080/hello-lwa/",
    )
    changed = collect_wsl_network_snapshot(
        platform="wsl", guest_ip="172.22.148.100", outputs=outputs, use_cache=False
    )
    monkeypatch.setattr(
        "local_webpage_access.wsl_network.collect_wsl_network_snapshot",
        lambda **_k: changed,
    )
    data = instance_status(ws, cfg, reg, "demo").to_dict()
    assert data["lanExposure"] == EXPOSURE_CANDIDATE_UNVERIFIED
    assert data["lanObservationStale"] is True
    assert data["lanObservation"]["vantage"] == "lan-client"


def test_record_lan_observation_does_not_clobber_concurrent_build_env(
    ws_reg, monkeypatch
) -> None:
    """记录观测必须持实例锁再读改写，不能把并发 configure 的 buildEnv 覆盖掉。"""
    from local_webpage_access.access import record_lan_observation
    from local_webpage_access.instance_settings import update_instance_settings
    from local_webpage_access.models import InstanceManifest
    from tests.scripts.test_wsl_network import YI14_NETIP, YI14_NETROUTE, YI14_WSL_VERSION
    from local_webpage_access.wsl_network import InteropOutputs, collect_wsl_network_snapshot

    ws, reg = ws_reg
    _seed(
        ws,
        reg,
        lan_url="http://172.22.148.94:21000",
        route_host="hello-lwa",
        route_url="http://172.22.148.94:8080/hello-lwa/",
    )
    outputs = InteropOutputs(
        wsl_version_text=YI14_WSL_VERSION,
        wslinfo_text="nat\n",
        netip_text=YI14_NETIP,
        netroute_text=YI14_NETROUTE,
        portproxy_text="0.0.0.0            8080       127.0.0.1         8080\n",
    )
    snap = collect_wsl_network_snapshot(
        platform="wsl", guest_ip="172.22.148.94", outputs=outputs, use_cache=False
    )
    started = threading.Event()
    proceed = threading.Event()

    def _slow_collect(**_k):
        started.set()
        assert proceed.wait(timeout=5)
        return snap

    monkeypatch.setattr(
        "local_webpage_access.wsl_network.collect_wsl_network_snapshot",
        _slow_collect,
    )
    cfg = Config(
        lanIpStrategy="auto",
        staticGatewayPort=8080,
        portPool=PortPool(start=21000, end=21050),
    )
    errors: list[BaseException] = []

    def _record() -> None:
        try:
            record_lan_observation(
                ws,
                cfg,
                "demo",
                vantage="lan-client",
                protocol="http",
                evidence="content-match",
                status_code=200,
                url="http://10.180.105.74:8080/hello-lwa/",
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=_record)
    thread.start()
    assert started.wait(timeout=5)
    update_instance_settings(ws, reg, "demo", {"buildEnv": {"VITE_BASE": "/hello-lwa/"}})
    proceed.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []
    saved = InstanceManifest.load(ws.app_manifest_path("demo"))
    assert saved.buildEnv == {"VITE_BASE": "/hello-lwa/"}
    assert saved.network.externalLanObservation is not None
    assert saved.network.externalLanObservation["vantage"] == "lan-client"
