"""BUG-1025：Tailscale HTTPS 主机覆盖及在线地址同步。"""

from __future__ import annotations

import ipaddress

import pytest

from local_webpage_access.access import sync_caddy_tls_bind
from local_webpage_access.config import Config
from local_webpage_access.static_gateway import StaticGateway
from tests.scripts._caddy import adapt_caddyfile_text, requires_caddy, simulate_request
from tests.scripts.test_bug944_tls_multi_san import _san_ips, requires_openssl

LAN = "192.168.1.50"
TS4 = "100.101.102.103"
TS6 = "fd7a:115c:a1e0::1234"


@pytest.fixture(autouse=True)
def isolated_network(monkeypatch):
    monkeypatch.setattr("local_webpage_access.ports.resolve_lan_ip", lambda _cfg: LAN)
    monkeypatch.setattr(
        "local_webpage_access.wsl_network.collect_wsl_network_snapshot", lambda **_kw: None
    )
    _tailscale(monkeypatch)


def _tailscale(monkeypatch, ipv4=None, ipv6=None):
    monkeypatch.setattr(
        "local_webpage_access.tailscale_network.probe_tailscale",
        lambda: {"available": bool(ipv4 or ipv6), "ipv4": ipv4, "ipv6": ipv6},
    )


def _config():
    return Config(staticGateway="caddy", gatewayTls="internal")


def _main(workspace, by_port):
    path = workspace.static_gateway / "Caddyfile"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            StaticGateway._tls_addresses(hosts, port) + " {\n\ttls internal\n}\n"
            for port, hosts in by_port.items()
        ),
        encoding="utf-8",
    )
    return path


def _online(monkeypatch):
    monkeypatch.setattr(StaticGateway, "detect_backend", lambda self: "caddy")
    monkeypatch.setattr(StaticGateway, "_admin_alive", lambda self: True)
    calls = []
    monkeypatch.setattr(StaticGateway, "reload_all", lambda self: calls.append("reload"))
    return calls


@pytest.mark.parametrize("ipv4,ipv6", [(TS4, None), (None, TS6), (TS4, TS6)])
def test_tls_render_covers_local_tailscale_on_both_origins(workspace, monkeypatch, ipv4, ipv6):
    _tailscale(monkeypatch, ipv4, ipv6)
    workspace.static_aliases.mkdir(parents=True, exist_ok=True)
    (workspace.static_aliases / "demo.conf").write_text("handle /demo {\n\trespond 200\n}\n")
    text = StaticGateway(workspace, _config())._assemble_main_config()
    for ip in filter(None, (ipv4, ipv6)):
        host = f"[{ip}]" if ":" in ip else ip
        for port in (8443, 9443):
            assert f"https://{host}:{port}" in text
    assert f"https://{LAN}:8443" in text
    assert "https://127.0.0.1:9443" in text


def test_no_tailscale_keeps_lan_and_loopback(workspace):
    assert StaticGateway(workspace, _config())._tls_hosts() == (["127.0.0.1", LAN], False)


def test_duplicate_tailscale_and_lan_only_appears_once(workspace, monkeypatch):
    _tailscale(monkeypatch, LAN)
    hosts, _ = StaticGateway(workspace, _config())._tls_hosts()
    assert hosts.count(LAN) == 1


@pytest.mark.parametrize("port", [8443, 9443])
def test_refresh_adds_missing_tailscale_even_when_lan_unchanged(workspace, monkeypatch, port):
    _tailscale(monkeypatch, TS4, TS6)
    _main(workspace, {port: ["127.0.0.1", LAN]})
    calls = _online(monkeypatch)
    assert sync_caddy_tls_bind(workspace, _config(), LAN) == (True, None)
    assert calls == ["reload"]


def test_refresh_with_current_tailscale_is_idempotent(workspace, monkeypatch):
    _tailscale(monkeypatch, TS4, TS6)
    hosts = ["127.0.0.1", LAN, TS4, TS6]
    _main(workspace, {8443: hosts, 9443: hosts})
    calls = _online(monkeypatch)
    assert sync_caddy_tls_bind(workspace, _config(), LAN) == (False, None)
    assert calls == []


def test_refresh_replaces_old_tailscale_address(workspace, monkeypatch):
    _tailscale(monkeypatch, TS4)
    _main(workspace, {8443: ["127.0.0.1", LAN, "100.111.112.113"]})
    calls = _online(monkeypatch)
    assert sync_caddy_tls_bind(workspace, _config(), LAN) == (True, None)
    assert calls == ["reload"]


def test_refresh_removes_disconnected_tailscale(workspace, monkeypatch):
    _main(workspace, {8443: ["127.0.0.1", LAN, TS4]})
    calls = _online(monkeypatch)
    assert sync_caddy_tls_bind(workspace, _config(), LAN) == (True, None)
    assert calls == ["reload"]


def test_refresh_does_not_start_offline_gateway(workspace, monkeypatch):
    _tailscale(monkeypatch, TS4)
    _main(workspace, {8443: ["127.0.0.1", LAN]})
    calls = _online(monkeypatch)
    monkeypatch.setattr(StaticGateway, "_admin_alive", lambda self: False)
    assert sync_caddy_tls_bind(workspace, _config(), LAN) == (False, None)
    assert calls == []


def test_refresh_preserves_wsl_extra_host_without_reload_loop(workspace, monkeypatch):
    _tailscale(monkeypatch, TS4)
    monkeypatch.setattr(StaticGateway, "_wsl_tls_extra_ip", lambda self: "192.168.1.60")
    hosts = ["127.0.0.1", LAN, "192.168.1.60", TS4]
    _main(workspace, {8443: hosts, 9443: hosts})
    calls = _online(monkeypatch)
    assert sync_caddy_tls_bind(workspace, _config(), LAN) == (False, None)
    assert calls == []


def test_failed_lan_probe_preserves_last_trusted_tls_hosts(workspace, monkeypatch):
    _tailscale(monkeypatch, TS4)
    monkeypatch.setattr("local_webpage_access.ports.resolve_lan_ip", lambda _cfg: None)
    path = _main(workspace, {8443: ["127.0.0.1", LAN, TS4]})
    before = path.read_bytes()
    calls = _online(monkeypatch)
    assert sync_caddy_tls_bind(workspace, _config(), None) == (False, None)
    assert calls == []
    assert path.read_bytes() == before


def test_wsl_explicit_bind_also_listens_on_ipv6(workspace, monkeypatch):
    _tailscale(monkeypatch, TS4, TS6)
    monkeypatch.setattr(StaticGateway, "_wsl_tls_extra_ip", lambda self: "192.168.1.60")
    monkeypatch.setattr(StaticGateway, "_tls_directive", lambda self, hosts, foreign: "tls internal")
    text = "\n".join(StaticGateway(workspace, _config())._tls_blocks([]))
    assert "bind 0.0.0.0 [::]" in text


@requires_caddy
def test_real_caddy_adapt_covers_tailscale_certificate_and_routes(workspace, monkeypatch):
    _tailscale(monkeypatch, TS4, TS6)
    workspace.static_aliases.mkdir(parents=True, exist_ok=True)
    (workspace.static_aliases / "demo.conf").write_text("handle /demo {\n\trespond 200\n}\n")
    text = StaticGateway(workspace, _config())._assemble_main_config()
    adapted = adapt_caddyfile_text(text, workspace.static_gateway)
    policies = adapted["apps"]["tls"]["automation"]["policies"]
    subjects = {subject for p in policies for subject in p["subjects"]}
    assert {TS4, TS6, LAN, "127.0.0.1"} <= subjects
    for host in (TS4, TS6, LAN, "127.0.0.1"):
        assert simulate_request(adapted, "/demo", host=host, port=8443).status == 200
        assert simulate_request(adapted, "/absent", host=host, port=8443).status == 404
        result = simulate_request(adapted, "/", host=host, port=9443)
        assert result.kind == "reverse_proxy"
        assert result.upstreams == ("127.0.0.1:17800",)


def test_refresh_network_entries_preserves_tls_config_when_lan_probe_fails(
    workspace, registry, monkeypatch
):
    from local_webpage_access.access import refresh_network_entries

    _tailscale(monkeypatch, TS4)
    monkeypatch.setattr("local_webpage_access.access.resolve_lan_ip", lambda _cfg: None)
    monkeypatch.setattr("local_webpage_access.ports.resolve_lan_ip", lambda _cfg: None)
    path = _main(workspace, {8443: ["127.0.0.1", LAN, TS4]})
    before = path.read_bytes()
    calls = _online(monkeypatch)
    result = refresh_network_entries(workspace, _config(), registry)
    assert result.lan_ip is None
    assert result.refreshed == []
    assert result.caddy_reloaded is False
    assert calls == []
    assert path.read_bytes() == before


def test_render_without_lan_still_covers_tailscale(workspace, monkeypatch):
    _tailscale(monkeypatch, TS4)
    monkeypatch.setattr("local_webpage_access.ports.resolve_lan_ip", lambda _cfg: None)
    hosts, _ = StaticGateway(workspace, _config())._tls_hosts()
    assert hosts == ["127.0.0.1", TS4]


def test_repeated_sync_uses_one_tailscale_probe(workspace, monkeypatch):
    from local_webpage_access import tailscale_network as ts

    probes = []

    def probe():
        probes.append(True)
        return {"available": True, "ipv4": TS4, "ipv6": None}

    monkeypatch.setattr(ts, "probe_tailscale", probe)
    hosts = ["127.0.0.1", LAN, TS4]
    _main(workspace, {8443: hosts, 9443: hosts})
    calls = _online(monkeypatch)
    for _ in range(3):
        assert sync_caddy_tls_bind(workspace, _config(), LAN) == (False, None)
    assert len(probes) == 1
    assert calls == []


@pytest.mark.parametrize("reason", ["timeout", "probe_failed", "command_failed"])
def test_uncertain_tailscale_probe_does_not_delete_working_tls_hosts(
    workspace, monkeypatch, reason
):
    monkeypatch.setattr(
        "local_webpage_access.tailscale_network.probe_tailscale",
        lambda: {"available": False, "state": "unavailable", "reason": reason},
    )
    _main(workspace, {8443: ["127.0.0.1", LAN, TS4]})
    calls = _online(monkeypatch)
    assert sync_caddy_tls_bind(workspace, _config(), LAN) == (False, None)
    assert calls == []


def test_tls_snapshot_expires_and_distinguishes_uncertain_from_disconnected(monkeypatch):
    from local_webpage_access import tailscale_network as ts

    now = [0.0]
    results = iter(
        [
            {"available": True, "ipv4": TS4},
            {"available": False, "state": "unavailable", "reason": "timeout"},
            {"available": False, "state": "Stopped", "reason": "not_connected"},
        ]
    )
    monkeypatch.setattr(ts.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(ts, "probe_tailscale", lambda: next(results))
    first = ts.cached_tls_snapshot()
    first["ipv4"] = "100.111.112.113"
    now[0] = 29.9
    assert ts.cached_tls_snapshot()["ipv4"] == TS4
    now[0] = 30.0
    uncertain = ts.cached_tls_snapshot()
    assert uncertain["available"] is True
    assert uncertain["ipv4"] == TS4
    assert uncertain["reason"] == "timeout"
    now[0] = 60.0
    assert ts.cached_tls_snapshot()["available"] is False
    assert ts.cached_tls_snapshot().get("ipv4") is None


@pytest.mark.parametrize("has_tailscale", [False, True])
def test_uncertain_tailscale_preserves_names_while_healing_changed_lan(
    workspace, monkeypatch, has_tailscale
):
    from local_webpage_access.tailscale_network import _unavailable

    old_lan = "192.168.1.40"
    hosts = ["127.0.0.1", old_lan] + ([TS4] if has_tailscale else [])
    path = _main(workspace, {9443: hosts})
    monkeypatch.setattr(
        "local_webpage_access.tailscale_network.probe_tailscale",
        lambda: _unavailable("unavailable", "command_failed"),
    )
    calls = _online(monkeypatch)

    def reload(self):
        calls.append("reload")
        path.write_text(self._assemble_main_config(), encoding="utf-8")

    monkeypatch.setattr(StaticGateway, "reload_all", reload)
    assert sync_caddy_tls_bind(workspace, _config(), LAN) == (True, None)
    text = path.read_text()
    assert f"https://{LAN}:9443" in text
    assert f"https://{old_lan}:9443" in text
    if has_tailscale:
        assert f"https://{TS4}:9443" in text
    assert sync_caddy_tls_bind(workspace, _config(), LAN) == (False, None)
    assert calls == ["reload"]


def test_tls_snapshot_serializes_concurrent_probes(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from local_webpage_access import tailscale_network as ts

    entered = threading.Event()
    release = threading.Event()
    probes = []

    def probe():
        probes.append(True)
        entered.set()
        assert release.wait(3)
        return {"available": True, "ipv4": TS4}

    monkeypatch.setattr(ts, "probe_tailscale", probe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(ts.cached_tls_snapshot)
        assert entered.wait(3)
        two = pool.submit(ts.cached_tls_snapshot)
        release.set()
        assert one.result(timeout=3) == two.result(timeout=3)
    assert len(probes) == 1


@requires_openssl
@requires_caddy
def test_wsl_certificate_and_dual_stack_listener_cover_tailscale(workspace, monkeypatch):
    from local_webpage_access.static_gateway import entry_tls_cert_paths

    _tailscale(monkeypatch, TS4, TS6)
    extra = "192.168.1.60"
    monkeypatch.setattr(StaticGateway, "_wsl_tls_extra_ip", lambda self: extra)
    text = StaticGateway(workspace, _config())._assemble_main_config()
    cert, _key = entry_tls_cert_paths(workspace)
    assert {str(ipaddress.ip_address(v)) for v in _san_ips(cert)} == {
        "127.0.0.1", LAN, extra, TS4, TS6
    }
    adapted = adapt_caddyfile_text(text, workspace.static_gateway)
    listeners = {
        address
        for server in adapted["apps"]["http"]["servers"].values()
        for address in server["listen"]
    }
    assert "0.0.0.0:9443" in listeners
    assert "[::]:9443" in listeners


def test_control_plane_blip_keeps_tailscale_tls_hosts(workspace, monkeypatch):
    """Running 但 Self.Online=false（控制面抖动）按未知处理：不摘 Tailscale 主机、不 reload。"""
    from local_webpage_access import tailscale_network as ts

    monkeypatch.setattr(
        ts, "probe_tailscale", lambda: ts._unavailable("Running", "control_offline")
    )
    _main(workspace, {8443: ["127.0.0.1", LAN, TS4], 9443: ["127.0.0.1", LAN, TS4]})
    calls = _online(monkeypatch)
    assert sync_caddy_tls_bind(workspace, _config(), LAN) == (False, None)
    assert calls == []
