"""多网卡发布地址及工作区手工策略回归。"""

from __future__ import annotations

import json
import subprocess

import pytest
from typer.testing import CliRunner

from local_webpage_access import access_workflow, lan_network, ports
from local_webpage_access.cli import app
from local_webpage_access.config import Config, load_config


@pytest.fixture(autouse=True)
def reset_candidates():
    lan_network._discover.cache_clear()
    yield
    lan_network._discover.cache_clear()


def test_linux_prefers_active_ethernet_in_interface_order(monkeypatch):
    def interface(name, address, *, kind="", active=True):
        return {
            "ifname": name,
            "link_type": "ether",
            "flags": ["UP", "BROADCAST", "LOWER_UP"] if active else ["UP", "NO-CARRIER"],
            "operstate": "UP" if active else "DOWN",
            "linkinfo": {"info_kind": kind},
            "addr_info": [{"family": "inet", "scope": "global", "local": address}],
        }

    interfaces = [
        interface("wlan0", "192.168.1.3"),
        interface("eth1", "192.168.1.10"),
        interface("eth0", "192.168.1.50"),
        interface("eth2", "192.168.1.4", active=False),
        interface("mihomo", "198.18.0.1", kind="tun"),
        interface("wg0", "10.5.0.2", kind="wireguard"),
    ]
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, json.dumps(interfaces)))
    assert ports.detect_lan_ip() == "192.168.1.50"


def test_cli_sets_workspace_policy_and_restores_auto(workspace, monkeypatch):
    workspace.config_path.write_text(
        "# 保留已有注释\nstaticGateway: builtin\nlanIpStrategy: auto # 保留行尾\nmanualLanIp: null\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ports, "detect_lan_ip", lambda: "192.168.1.50")
    runner = CliRunner()
    args = ["--workspace", str(workspace.root), "access", "lan"]
    before = workspace.config_path.read_bytes()
    rejected = runner.invoke(app, [*args, "set", "198.18.0.1"])
    assert rejected.exit_code == 2
    assert workspace.config_path.read_bytes() == before

    set_result = runner.invoke(app, [*args, "set", "192.168.1.10"])
    assert set_result.exit_code == 0, set_result.output
    saved = load_config(workspace)
    assert (saved.lanIpStrategy, saved.manualLanIp) == ("manual", "192.168.1.10")
    assert "# 保留已有注释" in workspace.config_path.read_text()
    assert "# 保留行尾" in workspace.config_path.read_text()

    show_result = runner.invoke(app, [*args, "show"])
    assert show_result.exit_code == 0, show_result.output
    assert "192.168.1.10" in show_result.output

    auto_result = runner.invoke(app, [*args, "auto"])
    assert auto_result.exit_code == 0, auto_result.output
    saved = load_config(workspace)
    assert (saved.lanIpStrategy, saved.manualLanIp) == ("auto", None)
    assert "192.168.1.50" in auto_result.output


def test_background_uses_new_manual_policy_with_old_config(workspace, registry, config, monkeypatch):
    access_workflow.reset_lan_refresh_throttle_state()
    workspace.config_path.write_text(
        "staticGateway: builtin\nlanIpStrategy: manual\nmanualLanIp: 192.168.1.10\n",
        encoding="utf-8",
    )
    assert isinstance(config, Config) and config.lanIpStrategy == "auto"
    monkeypatch.setattr(ports, "detect_lan_ip", lambda: "192.168.1.50")
    synced = []
    monkeypatch.setattr(
        "local_webpage_access.access.sync_caddy_tls_bind",
        lambda ws, cfg, ip: synced.append(ip) or (False, None),
    )
    assert access_workflow.maybe_throttled_lan_refresh(workspace, config, registry) is None
    assert synced == ["192.168.1.10"]


def test_macos_uses_hardware_type_over_interface_number(monkeypatch):
    hardware = "Hardware Port: Wi-Fi\nDevice: en0\n\nHardware Port: Ethernet\nDevice: en5\n"
    interfaces = (
        "en0: flags=1<UP,BROADCAST>\n\tinet 192.168.1.10\n\tstatus: active\n"
        "en5: flags=1<UP,BROADCAST>\n\tinet 192.168.1.50\n\tstatus: active\n"
        "utun0: flags=1<UP,POINTOPOINT>\n\tinet 198.18.0.1\n"
    )
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(
        lan_network, "_run", lambda command, **kw: hardware if "networksetup" in command[0] else interfaces
    )
    assert [(c.interface, c.address) for c in lan_network.lan_candidates()] == [
        ("en5", "192.168.1.50"),
        ("en0", "192.168.1.10"),
    ]


def test_windows_uses_interface_index_within_media_type(monkeypatch):
    rows = [
        {"interface": "Wireless", "medium": 9, "index": 1, "address": "192.168.1.3"},
        {"interface": "Ethernet A", "medium": 14, "index": 8, "address": "192.168.1.10"},
        {"interface": "Ethernet B", "medium": 14, "index": 3, "address": "192.168.1.50"},
    ]
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(lan_network, "_run", lambda *a, **kw: json.dumps(rows))
    assert ports.detect_lan_ip() == "192.168.1.50"


@pytest.mark.parametrize("address", ["198.18.0.1", "172.19.0.1", "100.64.1.2"])
def test_unavailable_interface_probe_rejects_unverified_udp_address(monkeypatch, address):
    class UdpSocket:
        def connect(self, _target):
            pass

        def getsockname(self):
            return address, 12345

        def close(self):
            pass

    monkeypatch.setattr(ports, "lan_candidates", lambda: None)
    monkeypatch.setattr(ports.socket, "socket", lambda *_args: UdpSocket())
    resolution = ports.detect_lan_resolution()
    assert ports.detect_lan_ip() is None
    assert resolution.ip is None
    if address != "198.18.0.1":
        assert resolution.source == "udp"
        assert resolution.reason == "source_unverified"
        assert resolution.probe_address == address


def test_enumerated_empty_result_differs_from_unavailable_probe(monkeypatch):
    monkeypatch.setattr(ports, "lan_candidates", lambda: ())
    resolution = ports.detect_lan_resolution()
    assert resolution.ip is None
    assert resolution.reason == "no_candidates"


def test_manual_resolution_keeps_explicit_priority(config):
    config.lanIpStrategy = "manual"
    config.manualLanIp = "192.168.1.14"
    resolution = ports.resolve_lan_resolution(config)
    assert resolution.ip == "192.168.1.14"
    assert resolution.source == "manual"
    assert resolution.reason is None


@pytest.mark.parametrize("kind", ["bridge", "vlan", "veth"])
def test_linux_accepts_verified_bridge_vlan_and_container_interfaces(monkeypatch, kind):
    rows = [
        {
            "ifname": "mihomo",
            "link_type": "ether",
            "flags": ["UP"],
            "operstate": "UP",
            "linkinfo": {"info_kind": "tun"},
            "addr_info": [{"family": "inet", "scope": "global", "local": "10.8.0.1"}],
        },
        {
            "ifname": f"lan-{kind}",
            "link_type": "ether",
            "flags": ["UP", "LOWER_UP"],
            "operstate": "UP",
            "linkinfo": {"info_kind": kind},
            "addr_info": [{"family": "inet", "scope": "global", "local": "192.168.1.20"}],
        },
    ]
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(lan_network, "_run", lambda *a, **kw: json.dumps(rows))
    resolution = ports.detect_lan_resolution()
    assert resolution.ip == "192.168.1.20"
    assert resolution.interface == f"lan-{kind}"
    assert resolution.source == "interface"


def test_linux_only_tunnel_has_no_automatic_candidate(monkeypatch):
    rows = [
        {
            "ifname": "mihomo",
            "link_type": "ether",
            "flags": ["UP"],
            "operstate": "UP",
            "linkinfo": {"info_kind": "tun"},
            "addr_info": [{"family": "inet", "scope": "global", "local": "198.18.0.1"}],
        }
    ]
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(lan_network, "_run", lambda *a, **kw: json.dumps(rows))
    assert ports.detect_lan_resolution().reason == "no_candidates"
