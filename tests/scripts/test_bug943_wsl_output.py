"""BUG-943：Windows GBK 命令输出须经 runner 保留转发规则与能力状态。"""

from types import SimpleNamespace

import pytest

from local_webpage_access import platform_support as ps
from local_webpage_access import wsl_network as wn
from local_webpage_access.config import Config


NETSH_TEXT = (
    "\r\n侦听 ipv4:                 连接到 ipv4:\r\n\r\n"
    "地址            端口        地址            端口\r\n"
    "--------------- ----------  --------------- ----------\r\n"
    "0.0.0.0         23722       172.22.148.94   23722\r\n"
    "0.0.0.0         24722       172.22.148.94   24722\r\n\r\n"
)
NETSH_GBK = NETSH_TEXT.encode("gbk")


def test_gbk_netsh_decodes_exactly_and_preserves_both_rules() -> None:
    assert NETSH_GBK.startswith(bytes.fromhex("0d0ad5ecccfd20697076343a"))
    decoded = ps._decode_wsl_cli_output(NETSH_GBK)
    assert decoded == NETSH_TEXT.replace("\r\n", "\n")
    assert [(r.listen_port, r.connect_port) for r in wn.parse_portproxy_text(decoded)] == [
        (23722, 23722),
        (24722, 24722),
    ]


@pytest.mark.parametrize(
    ("encoding", "prefix"),
    [
        ("utf-8", b""),
        ("utf-8-sig", b""),
        ("utf-16-le", b""),
        ("utf-16-le", b"\xff\xfe"),
        ("utf-16-be", b"\xfe\xff"),
    ],
)
@pytest.mark.parametrize("label", ["version", "版本"])
def test_wsl_unicode_outputs_remain_compatible(encoding: str, prefix: bytes, label: str) -> None:
    text = f"WSL {label}: 2.4.13.0\r\nWindows {label}: 10.0.19045.6456\r\n"
    raw = prefix + text.encode(encoding)
    assert ps._decode_wsl_cli_output(raw) == text.replace("\r\n", "\n")


@pytest.mark.parametrize("raw", [None, b"", "", "nat\x00\r\n"])
def test_empty_and_predecoded_outputs_remain_compatible(raw) -> None:
    assert ps._decode_wsl_cli_output(raw) == ("nat\n" if raw else "")


def _runner_snapshot(monkeypatch, portproxy: bytes, *, profile: bytes = b""):
    monkeypatch.setattr(ps, "_wsl_exe_candidates", lambda: ["wsl.exe"])
    monkeypatch.setattr(ps, "detect_wsl_kernel_kind", lambda: "2")

    def runner(args, **kwargs):
        assert kwargs["text"] is False
        if args == ["wsl.exe", "--version"]:
            out = "WSL 版本: 3.0.3.0\r\nWindows 版本: 10.0.19045.6456\r\n".encode(
                "utf-16-le"
            )
        elif args == ["wslinfo", "--networking-mode"]:
            out = b"nat\n"
        elif args[1:] == ["interface", "portproxy", "show", "all"]:
            out = portproxy
        elif "Get-NetIPAddress" in args[-1]:
            out = "10.180.105.74|无线网络|Dhcp\r\n".encode("utf-8")
        elif "Get-NetRoute" in args[-1]:
            out = "无线网络|10.180.105.1|0\r\n".encode("utf-8")
        elif "Get-NetConnectionProfile" in args[-1]:
            out = "无线网络|Private\r\n".encode("utf-8")
        elif "Get-NetTCPConnection" in args[-1]:
            out = b"0.0.0.0|8080\n"
        elif "Get-NetFirewallProfile" in args[-1]:
            out = b"Private|Block\n"
        elif "Get-NetFirewallHyperV" in args[-1]:
            out = b"S|WSL|Block\n"
        elif args[-1] == "Write-Output $env:USERPROFILE":
            out = profile
        else:
            pytest.fail(f"意外命令：{args}")
        return SimpleNamespace(returncode=0, stdout=out, stderr=b"")

    snapshot = wn.collect_wsl_network_snapshot(
        platform="wsl", guest_ip="172.22.148.94", runner=runner, use_cache=False
    )
    assert snapshot is not None
    assert snapshot.networking_mode == wn.MODE_NAT
    assert snapshot.package_version == "3.0.3.0"
    assert snapshot.host_lan_ip == "10.180.105.74"
    return snapshot


def test_runner_gbk_ssh_rules_visible_but_web_entries_missing(monkeypatch) -> None:
    snapshot = _runner_snapshot(monkeypatch, NETSH_GBK)
    assert snapshot.capabilities["portproxy"].status == "available"
    assert snapshot.to_dict()["portproxyRules"] == [
        {
            "listenAddress": "0.0.0.0",
            "listenPort": port,
            "connectAddress": "172.22.148.94",
            "connectPort": port,
            "targetsGuestDynamicIp": True,
        }
        for port in (23722, 24722)
    ]
    for alias, expected_port in [(None, 18000), ("hello-lwa", 8080)]:
        access = wn.describe_instance_access(
            snapshot, host_port=18000, route_host=alias, config=Config()
        )
        assert access.lan_exposure == wn.EXPOSURE_ENTRY_MISSING
        assert access.expected_port == expected_port
        assert access.host_lan_url is None


@pytest.mark.parametrize(
    ("connect_port", "alias", "url"),
    [
        (18000, None, "http://10.180.105.74:19000"),
        (8080, "hello-lwa", "http://10.180.105.74:19000/hello-lwa/"),
    ],
)
def test_runner_gbk_web_rule_matches_connect_port(monkeypatch, connect_port, alias, url) -> None:
    raw = (NETSH_TEXT + f"0.0.0.0         19000       127.0.0.1      {connect_port}\r\n").encode(
        "gbk"
    )
    snapshot = _runner_snapshot(monkeypatch, raw)
    assert len(snapshot.portproxy_rules) == 3
    access = wn.describe_instance_access(
        snapshot, host_port=18000, route_host=alias, config=Config()
    )
    assert access.lan_exposure == wn.EXPOSURE_CANDIDATE_UNVERIFIED
    assert access.host_lan_url == url
    assert access.listen_port == 19000
    assert access.connect_port == connect_port
    assert access.connect_address == "127.0.0.1"


@pytest.mark.parametrize(
    "raw", [b"\x81", b"\x81\x30", b"\xff\xfe\x00", b"\xfe\xff\x00", b"W\x00S"]
)
def test_runner_undecodable_portproxy_is_unknown(monkeypatch, raw) -> None:
    snapshot = _runner_snapshot(monkeypatch, raw)
    cap = snapshot.capabilities["portproxy"]
    assert cap.status == "unknown"
    assert cap.reason == "decode-error"
    assert snapshot.portproxy_rules == []
    access = wn.describe_instance_access(
        snapshot, host_port=18000, route_host="hello-lwa", config=Config()
    )
    assert access.lan_exposure == wn.EXPOSURE_UNKNOWN
    assert access.host_lan_url is None


def test_runner_empty_portproxy_proves_web_entry_missing(monkeypatch) -> None:
    snapshot = _runner_snapshot(monkeypatch, b"")
    assert snapshot.capabilities["portproxy"].status == "available"
    assert snapshot.portproxy_rules == []
    access = wn.describe_instance_access(
        snapshot, host_port=18000, route_host=None, config=Config()
    )
    assert access.lan_exposure == wn.EXPOSURE_ENTRY_MISSING


def test_runner_undecodable_profile_does_not_abort_snapshot(monkeypatch) -> None:
    snapshot = _runner_snapshot(monkeypatch, NETSH_GBK, profile=b"\x81")
    assert snapshot.capabilities["wslconfig"].status == "unknown"
    assert snapshot.capabilities["wslconfig"].reason == "decode-error"
    assert len(snapshot.portproxy_rules) == 2
