"""BUG-923 / PLN-054：WSL 网络模式、地址范围与只读入口发现。"""

from __future__ import annotations

import pytest

from local_webpage_access.config import Config
from local_webpage_access.wsl_network import (
    EXPOSURE_CANDIDATE_UNVERIFIED,
    EXPOSURE_ENTRY_MISSING,
    EXPOSURE_EXTERNALLY_VERIFIED,
    EXPOSURE_UNKNOWN,
    MODE_CONSOMME,
    MODE_MIRRORED,
    MODE_NAT,
    MODE_UNKNOWN,
    REACH_LAN,
    REACH_WINDOWS_HOST,
    InteropOutputs,
    classify_windows_product,
    collect_wsl_network_snapshot,
    describe_instance_access,
    parse_netip_text,
    parse_netroute_aliases,
    parse_networking_mode_text,
    parse_portproxy_text,
    parse_windows_nt_version,
    parse_wslconfig_networking_mode,
    reset_wsl_network_cache,
    snapshot_fingerprint,
)
from local_webpage_access import wsl_network as wn

YI14_WSL_VERSION = """\
WSL version: 3.0.3.0
Kernel version: 6.18.54.1-1
WSLg version: 1.0.79
Windows version: 10.0.19045.6456
"""

YI14_NETIP = """\
172.22.144.1|vEthernet (WSL)|Manual
172.30.224.1|vEthernet (Default Switch)|Manual
169.254.61.31|以太网 3|WellKnown
2.0.0.1|以太网 3|Manual
169.254.7.228|蓝牙网络连接|WellKnown
10.180.105.74|WLAN|Dhcp
127.0.0.1|Loopback Pseudo-Interface 1|WellKnown
"""

YI14_NETROUTE = "WLAN|10.180.105.1|0\n"

YI14_PORTPROXY = """\
Listen on ipv4:             Connect to ipv4:

Address            Port        Address            Port
---------------    ----------  ---------------    ----------
0.0.0.0            23722       172.22.148.94      23722
0.0.0.0            24722       172.22.148.94      24722
"""

YI14_OUTPUTS = InteropOutputs(
    wsl_version_text=YI14_WSL_VERSION,
    wslinfo_text="nat\n",
    wslconfig_text="[wsl2]\nmemory=8GB\n",
    netip_text=YI14_NETIP,
    netroute_text=YI14_NETROUTE,
    portproxy_text=YI14_PORTPROXY,
)


def setup_function() -> None:
    reset_wsl_network_cache()


def test_classify_windows_product_uses_build_not_major() -> None:
    assert classify_windows_product("10.0.19045.6456") == "windows10"
    assert classify_windows_product("10.0.22621.1") == "windows11"
    assert classify_windows_product("10.0.26100.1") == "windows11"
    assert classify_windows_product("10.0") is None


def test_parse_windows_nt_version_from_wsl_version_text() -> None:
    assert parse_windows_nt_version(YI14_WSL_VERSION) == "10.0.19045.6456"


def test_parse_networking_mode_normalizes_aliases() -> None:
    assert parse_networking_mode_text("nat") == MODE_NAT
    assert parse_networking_mode_text("NAT") == MODE_NAT
    assert parse_networking_mode_text("mirrored") == MODE_MIRRORED
    assert parse_networking_mode_text("virtioproxy") == "consomme"
    assert parse_networking_mode_text("consomme") == "consomme"
    assert parse_networking_mode_text("") == MODE_UNKNOWN
    assert parse_networking_mode_text("future-mode-xyz") == "future-mode-xyz"


def test_parse_wslconfig_reads_declared_mode_only() -> None:
    text = "[wsl2]\nnetworkingMode=mirrored\ndnsTunneling=true\n"
    assert parse_wslconfig_networking_mode(text) == MODE_MIRRORED
    assert parse_wslconfig_networking_mode("[wsl2]\nmemory=8GB\n") is None


def test_host_lan_ip_prefers_default_route_physical_nic() -> None:
    nics = parse_netip_text(YI14_NETIP)
    aliases = parse_netroute_aliases(YI14_NETROUTE)
    from local_webpage_access.wsl_network import select_host_lan_ip

    ip = select_host_lan_ip(nics, aliases)
    assert ip == "10.180.105.74"


def test_parse_portproxy_splits_listen_and_connect() -> None:
    rules = parse_portproxy_text(YI14_PORTPROXY)
    assert [(r.listen_port, r.connect_address, r.connect_port) for r in rules] == [
        (23722, "172.22.148.94", 23722),
        (24722, "172.22.148.94", 24722),
    ]


def _yi14_snapshot(**overrides):  # noqa: ANN003
    kw = dict(
        platform="wsl",
        guest_ip="172.22.148.94",
        outputs=YI14_OUTPUTS,
        use_cache=False,
    )
    kw.update(overrides)
    return collect_wsl_network_snapshot(**kw)


def test_yi14_nat_snapshot_does_not_treat_guest_ip_as_lan() -> None:
    snap = _yi14_snapshot()
    assert snap is not None
    assert snap.windows_product == "windows10"
    assert snap.package_version == "3.0.3.0"
    assert snap.networking_mode == MODE_NAT
    assert snap.guest_ip == "172.22.148.94"
    assert snap.host_lan_ip == "10.180.105.74"
    assert snap.guest_reachability == REACH_WINDOWS_HOST
    assert snap.declared_networking_mode is None


def test_win10_declared_mirrored_stays_observed_nat() -> None:
    outputs = InteropOutputs(
        wsl_version_text=YI14_WSL_VERSION,
        wslinfo_text="nat\n",
        wslconfig_text="[wsl2]\nnetworkingMode=mirrored\n",
        netip_text=YI14_NETIP,
        netroute_text=YI14_NETROUTE,
        portproxy_text=YI14_PORTPROXY,
    )
    snap = _yi14_snapshot(outputs=outputs)
    assert snap is not None
    assert snap.declared_networking_mode == MODE_MIRRORED
    assert snap.networking_mode == MODE_NAT
    assert "mirrored" in (snap.networking_mode_reason or "").lower() or "回退" in (
        snap.networking_mode_reason or ""
    )


def test_mirrored_when_guest_ip_matches_windows_lan() -> None:
    outputs = InteropOutputs(
        wsl_version_text="WSL version: 2.4.13.0\nWindows version: 10.0.22621.2428\n",
        wslinfo_text="mirrored\n",
        wslconfig_text="[wsl2]\nnetworkingMode=mirrored\n",
        netip_text="10.180.105.74|WLAN|Dhcp\n127.0.0.1|Loopback Pseudo-Interface 1|WellKnown\n",
        netroute_text="WLAN|10.180.105.1|0\n",
        portproxy_text="",
    )
    snap = collect_wsl_network_snapshot(
        platform="wsl",
        guest_ip="10.180.105.74",
        outputs=outputs,
        use_cache=False,
    )
    assert snap is not None
    assert snap.windows_product == "windows11"
    assert snap.networking_mode == MODE_MIRRORED
    assert snap.guest_reachability == REACH_LAN


def test_interop_timeout_keeps_unknown_and_does_not_claim_lan() -> None:
    outputs = InteropOutputs(
        errors={
            "wslinfo": "timeout",
            "netip": "timeout",
            "portproxy": "timeout",
        }
    )
    snap = collect_wsl_network_snapshot(
        platform="wsl",
        guest_ip="172.22.148.94",
        outputs=outputs,
        use_cache=False,
    )
    assert snap is not None
    assert snap.networking_mode == MODE_UNKNOWN
    assert snap.host_lan_ip is None
    assert snap.guest_reachability != REACH_LAN
    assert snap.capabilities["netip"].status == "unknown"


def test_non_wsl_snapshot_is_none() -> None:
    assert collect_wsl_network_snapshot(platform="macos", use_cache=False) is None


def test_mirrored_cli_candidate_does_not_claim_portproxy_or_external_verification() -> None:
    outputs = InteropOutputs(
        wsl_version_text="WSL version: 3.0.4.0\nWindows version: 10.0.28020.3262\n",
        wslinfo_text="mirrored\n",
        netip_text="192.0.2.10|WLAN|Dhcp\n",
        netroute_text="WLAN|192.0.2.1|0\n",
        portproxy_text="",
    )
    snap = collect_wsl_network_snapshot(
        platform="wsl", guest_ip="192.0.2.10", outputs=outputs, use_cache=False
    )
    assert snap is not None
    access = describe_instance_access(snap, host_port=18000, route_host=None, config=Config())
    assert access.lan_exposure == EXPOSURE_CANDIDATE_UNVERIFIED
    lines = wn.format_cli_network_lines(lan_url=None, route_url=None, access=access)
    candidate = next(value for label, value in lines if label == "局域网候选")
    assert "尚未从其他设备验证" in candidate
    assert "已发现转发" not in candidate


def test_nat_without_matching_forward_marks_lan_entry_missing() -> None:
    snap = _yi14_snapshot()
    access = describe_instance_access(
        snap,
        host_port=18000,
        route_host="hello-lwa",
        config=Config(),
    )
    assert access.guest_reachability == REACH_WINDOWS_HOST
    assert access.lan_exposure == EXPOSURE_ENTRY_MISSING
    assert access.host_lan_url is None
    assert "8080" in (access.guidance or "")
    assert "不会自动执行" in (access.guidance or "") or "不自动" in (access.guidance or "")
    assert "127.0.0.1" in (access.guidance or "")


def test_nat_portproxy_to_loopback_is_candidate_not_lan_verified() -> None:
    proxy = """\
0.0.0.0            8080       127.0.0.1         8080
0.0.0.0            23722      172.22.148.94     23722
"""
    outputs = InteropOutputs(
        wsl_version_text=YI14_WSL_VERSION,
        wslinfo_text="nat\n",
        wslconfig_text="[wsl2]\n",
        netip_text=YI14_NETIP,
        netroute_text=YI14_NETROUTE,
        portproxy_text=proxy,
    )
    snap = _yi14_snapshot(outputs=outputs)
    access = describe_instance_access(
        snap,
        host_port=18000,
        route_host="hello-lwa",
        config=Config(),
    )
    assert access.host_lan_url == "http://10.180.105.74:8080/hello-lwa/"
    assert access.lan_exposure == EXPOSURE_CANDIDATE_UNVERIFIED
    assert access.listen_port == 8080
    assert access.connect_port == 8080
    assert access.connect_address == "127.0.0.1"


def test_portproxy_to_guest_ip_flags_drift_risk() -> None:
    snap = _yi14_snapshot()
    assert any(r.targets_guest_dynamic_ip for r in snap.portproxy_rules)
    access = describe_instance_access(snap, host_port=18000, route_host=None, config=Config())
    assert access.dynamic_ip_forwarding is True


def test_portproxy_ignores_forward_to_unrelated_host() -> None:
    """BUG-928：只比监听端口会把 8080→其他主机:80 当成 LWA 入口。"""
    proxy = "0.0.0.0            8080       203.0.113.9       80\n"
    outputs = InteropOutputs(
        wsl_version_text=YI14_WSL_VERSION,
        wslinfo_text="nat\n",
        netip_text=YI14_NETIP,
        netroute_text=YI14_NETROUTE,
        portproxy_text=proxy,
    )
    snap = _yi14_snapshot(outputs=outputs)
    access = describe_instance_access(
        snap,
        host_port=18000,
        route_host="hello-lwa",
        config=Config(),
    )
    assert access.host_lan_url is None
    assert access.lan_exposure == EXPOSURE_ENTRY_MISSING


def test_portproxy_matches_connect_port_not_listen_port() -> None:
    """BUG-928：9000→127.0.0.1:8080 应视为网关入口，URL 用监听端口。"""
    proxy = "0.0.0.0            9000       127.0.0.1         8080\n"
    outputs = InteropOutputs(
        wsl_version_text=YI14_WSL_VERSION,
        wslinfo_text="nat\n",
        netip_text=YI14_NETIP,
        netroute_text=YI14_NETROUTE,
        portproxy_text=proxy,
    )
    snap = _yi14_snapshot(outputs=outputs)
    access = describe_instance_access(
        snap,
        host_port=18000,
        route_host="hello-lwa",
        config=Config(),
    )
    assert access.host_lan_url == "http://10.180.105.74:9000/hello-lwa/"
    assert access.listen_port == 9000
    assert access.connect_port == 8080
    assert access.connect_address == "127.0.0.1"


def test_no_alias_does_not_prefer_shared_gateway_forward() -> None:
    """BUG-929：无路径别名时不得把共享网关 8080 当成该实例入口。"""
    proxy = """\
0.0.0.0            8080       127.0.0.1         8080
0.0.0.0            18000      127.0.0.1         18000
"""
    outputs = InteropOutputs(
        wsl_version_text=YI14_WSL_VERSION,
        wslinfo_text="nat\n",
        netip_text=YI14_NETIP,
        netroute_text=YI14_NETROUTE,
        portproxy_text=proxy,
    )
    snap = _yi14_snapshot(outputs=outputs)
    access = describe_instance_access(
        snap, host_port=18000, route_host=None, config=Config()
    )
    assert access.host_lan_url == "http://10.180.105.74:18000"
    assert access.listen_port == 18000
    assert access.connect_port == 18000


def test_guidance_without_alias_uses_instance_host_port() -> None:
    """无路径别名时建议转发实例 hostPort，不得默认网关 8080。"""
    snap = _yi14_snapshot()
    access = describe_instance_access(
        snap, host_port=18000, route_host=None, config=Config()
    )
    guidance = access.guidance or ""
    assert "18000" in guidance
    assert "listenport=18000" in guidance
    assert "8080" not in guidance


def test_mirrored_guidance_without_alias_uses_instance_port() -> None:
    outputs = InteropOutputs(
        wsl_version_text="WSL version: 2.4.13.0\nWindows version: 10.0.22621.2428\n",
        wslinfo_text="mirrored\n",
        wslconfig_text="[wsl2]\nnetworkingMode=mirrored\n",
        netip_text="10.180.105.74|WLAN|Dhcp\n127.0.0.1|Loopback Pseudo-Interface 1|WellKnown\n",
        netroute_text="WLAN|10.180.105.1|0\n",
    )
    snap = collect_wsl_network_snapshot(
        platform="wsl",
        guest_ip="10.180.105.74",
        outputs=outputs,
        use_cache=False,
    )
    access = describe_instance_access(
        snap, host_port=18000, route_host=None, config=Config()
    )
    guidance = access.guidance or ""
    assert "LocalPorts 18000" in guidance
    assert "8080" not in guidance


def test_portproxy_read_failure_is_unknown_not_entry_missing() -> None:
    """netsh 超时不得把「读不到转发」说成「没有入口」。"""
    outputs = InteropOutputs(
        wsl_version_text=YI14_WSL_VERSION,
        wslinfo_text="nat\n",
        netip_text=YI14_NETIP,
        netroute_text=YI14_NETROUTE,
        portproxy_text=None,
        errors={"portproxy": "timeout"},
    )
    snap = _yi14_snapshot(outputs=outputs)
    assert snap is not None
    assert snap.capabilities["portproxy"].status == "unknown"
    access = describe_instance_access(
        snap,
        host_port=18000,
        route_host="hello-lwa",
        config=Config(),
    )
    assert access.lan_exposure == EXPOSURE_UNKNOWN
    assert access.host_lan_url is None


def test_mirrored_firewall_example_uses_single_braces() -> None:
    """BUG-930：VMCreatorId 必须输出单层大括号，用户才能照抄。"""
    outputs = InteropOutputs(
        wsl_version_text="WSL version: 2.4.13.0\nWindows version: 10.0.22621.2428\n",
        wslinfo_text="mirrored\n",
        wslconfig_text="[wsl2]\nnetworkingMode=mirrored\n",
        netip_text="10.180.105.74|WLAN|Dhcp\n127.0.0.1|Loopback Pseudo-Interface 1|WellKnown\n",
        netroute_text="WLAN|10.180.105.1|0\n",
    )
    snap = collect_wsl_network_snapshot(
        platform="wsl",
        guest_ip="10.180.105.74",
        outputs=outputs,
        use_cache=False,
    )
    access = describe_instance_access(
        snap, host_port=18000, route_host="demo", config=Config()
    )
    guidance = access.guidance or ""
    assert "-VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}'" in guidance
    assert "{{40E0AC32" not in guidance


def test_https_candidate_uses_tls_port_not_plaintext_8080() -> None:
    proxy = "0.0.0.0            8443       127.0.0.1         8443\n"
    outputs = InteropOutputs(
        wsl_version_text=YI14_WSL_VERSION,
        wslinfo_text="nat\n",
        netip_text=YI14_NETIP,
        netroute_text=YI14_NETROUTE,
        portproxy_text=proxy,
    )
    snap = _yi14_snapshot(outputs=outputs)
    config = Config(gatewayTls="internal", staticGateway="caddy")
    access = describe_instance_access(
        snap,
        host_port=18000,
        route_host="hello-lwa",
        config=config,
    )
    assert access.host_lan_url == "https://10.180.105.74:8443/hello-lwa/"
    assert "8080" not in (access.host_lan_url or "")
    assert "https://127.0.0.1:8443/" in (access.guidance or "")


def test_direct_instance_port_stays_http_when_gateway_tls_enabled() -> None:
    """TLS 只在网关终止；无别名或只匹配 hostPort 时直连候选保持 HTTP。"""
    proxy = "0.0.0.0            18000      127.0.0.1         18000\n"
    outputs = InteropOutputs(
        wsl_version_text=YI14_WSL_VERSION,
        wslinfo_text="nat\n",
        netip_text=YI14_NETIP,
        netroute_text=YI14_NETROUTE,
        portproxy_text=proxy,
    )
    snap = _yi14_snapshot(outputs=outputs)
    config = Config(gatewayTls="internal", staticGateway="caddy")
    access = describe_instance_access(
        snap, host_port=18000, route_host=None, config=config
    )
    assert access.host_lan_url == "http://10.180.105.74:18000"
    guidance = access.guidance or ""
    assert "http://127.0.0.1:18000/" in guidance
    assert "https://127.0.0.1:18000" not in guidance
    assert "https://" not in (access.host_lan_url or "")


def test_fingerprint_changes_when_guest_ip_or_proxy_changes() -> None:
    snap1 = _yi14_snapshot()
    proxy = "0.0.0.0            8080       127.0.0.1         8080\n"
    snap2 = _yi14_snapshot(
        outputs=InteropOutputs(
            wsl_version_text=YI14_WSL_VERSION,
            wslinfo_text="nat\n",
            netip_text=YI14_NETIP,
            netroute_text=YI14_NETROUTE,
            portproxy_text=proxy,
        )
    )
    assert snapshot_fingerprint(snap1) != snapshot_fingerprint(snap2)
    snap3 = _yi14_snapshot(guest_ip="172.22.148.100")
    assert snapshot_fingerprint(snap1) != snapshot_fingerprint(snap3)


def test_n10_2_wsl_package_2x_nat_same_as_3x() -> None:
    """N10-2 策略：Win10 + 包 2.x + NAT 与 3.x 同一套地址范围，不因包号升格。"""
    outputs = InteropOutputs(
        wsl_version_text="WSL version: 2.4.13.0\nWindows version: 10.0.19045.6456\n",
        wslinfo_text="nat\n",
        netip_text=YI14_NETIP,
        netroute_text=YI14_NETROUTE,
        portproxy_text="",
    )
    snap = collect_wsl_network_snapshot(
        platform="wsl",
        guest_ip="172.22.148.94",
        outputs=outputs,
        use_cache=False,
    )
    assert snap is not None
    assert snap.windows_product == "windows10"
    assert snap.package_version == "2.4.13.0"
    assert snap.networking_mode == MODE_NAT
    assert snap.guest_reachability == REACH_WINDOWS_HOST
    access = describe_instance_access(
        snap, host_port=18000, route_host="hello-lwa", config=Config()
    )
    assert access.lan_exposure == EXPOSURE_ENTRY_MISSING
    assert access.host_lan_url is None


def test_n11_nat_does_not_assume_mirrored() -> None:
    """N11-2/N11-3：Win11 未开 mirrored 时与 Win10 NAT 同策略。"""
    outputs = InteropOutputs(
        wsl_version_text="WSL version: 2.4.13.0\nWindows version: 10.0.22621.2428\n",
        wslinfo_text="nat\n",
        wslconfig_text="[wsl2]\nmemory=8GB\n",
        netip_text=YI14_NETIP,
        netroute_text=YI14_NETROUTE,
        portproxy_text="",
    )
    snap = collect_wsl_network_snapshot(
        platform="wsl",
        guest_ip="172.22.148.94",
        outputs=outputs,
        use_cache=False,
    )
    assert snap is not None
    assert snap.windows_product == "windows11"
    assert snap.networking_mode == MODE_NAT
    assert snap.guest_reachability == REACH_WINDOWS_HOST
    access = describe_instance_access(
        snap, host_port=18000, route_host="hello-lwa", config=Config()
    )
    assert access.lan_exposure == EXPOSURE_ENTRY_MISSING
    assert "mirrored" in (access.guidance or "")


def test_n11_3_package_3x_nat_same_as_2x() -> None:
    outputs = InteropOutputs(
        wsl_version_text="WSL version: 3.0.3.0\nWindows version: 10.0.26100.1\n",
        wslinfo_text="nat\n",
        netip_text=YI14_NETIP,
        netroute_text=YI14_NETROUTE,
        portproxy_text="",
    )
    snap = collect_wsl_network_snapshot(
        platform="wsl",
        guest_ip="172.22.148.94",
        outputs=outputs,
        use_cache=False,
    )
    assert snap is not None
    assert snap.package_version == "3.0.3.0"
    assert snap.windows_product == "windows11"
    assert snap.networking_mode == MODE_NAT
    assert snap.guest_reachability == REACH_WINDOWS_HOST


def test_m11_mirrored_without_portproxy_is_candidate_not_verified() -> None:
    """M11：mirrored 不要求 portproxy，也不得写成外部已验证。"""
    outputs = InteropOutputs(
        wsl_version_text="WSL version: 2.4.13.0\nWindows version: 10.0.22621.2428\n",
        wslinfo_text="mirrored\n",
        wslconfig_text="[wsl2]\nnetworkingMode=mirrored\n",
        netip_text="10.180.105.74|WLAN|Dhcp\n127.0.0.1|Loopback Pseudo-Interface 1|WellKnown\n",
        netroute_text="WLAN|10.180.105.1|0\n",
        portproxy_text="",
    )
    snap = collect_wsl_network_snapshot(
        platform="wsl",
        guest_ip="10.180.105.74",
        outputs=outputs,
        use_cache=False,
    )
    access = describe_instance_access(
        snap, host_port=18000, route_host="hello-lwa", config=Config()
    )
    assert snap is not None
    assert snap.networking_mode == MODE_MIRRORED
    assert snap.guest_reachability == REACH_LAN
    assert access.lan_exposure == EXPOSURE_CANDIDATE_UNVERIFIED
    assert access.host_lan_url == "http://10.180.105.74:8080/hello-lwa/"
    assert "portproxy" not in (access.guidance or "").lower()


def test_x10_m_does_not_claim_mirrored_enabled() -> None:
    outputs = InteropOutputs(
        wsl_version_text=YI14_WSL_VERSION,
        wslinfo_text="nat\n",
        wslconfig_text="[wsl2]\nnetworkingMode=mirrored\n",
        netip_text=YI14_NETIP,
        netroute_text=YI14_NETROUTE,
        portproxy_text="",
    )
    snap = _yi14_snapshot(outputs=outputs)
    assert snap is not None
    assert snap.declared_networking_mode == MODE_MIRRORED
    assert snap.networking_mode == MODE_NAT
    access = describe_instance_access(
        snap, host_port=18000, route_host="hello-lwa", config=Config()
    )
    guidance = access.guidance or ""
    assert "当前为 mirrored" not in guidance
    assert "不支持 mirrored" in (snap.networking_mode_reason or "")
    assert access.guest_reachability == REACH_WINDOWS_HOST


def test_x_unk_preserves_future_mode_name() -> None:
    outputs = InteropOutputs(
        wsl_version_text=YI14_WSL_VERSION,
        wslinfo_text="future-mode-xyz\n",
        netip_text=YI14_NETIP,
        netroute_text=YI14_NETROUTE,
        portproxy_text="",
    )
    snap = _yi14_snapshot(outputs=outputs)
    assert snap is not None
    assert snap.networking_mode == "future-mode-xyz"
    assert snap.guest_reachability == REACH_WINDOWS_HOST
    access = describe_instance_access(
        snap, host_port=18000, route_host=None, config=Config()
    )
    assert "future-mode-xyz" in (access.guidance or "")
    assert access.lan_exposure != EXPOSURE_EXTERNALLY_VERIFIED


def test_consomme_mode_keeps_name_and_does_not_claim_lan() -> None:
    outputs = InteropOutputs(
        wsl_version_text=YI14_WSL_VERSION,
        wslinfo_text="consomme\n",
        netip_text=YI14_NETIP,
        netroute_text=YI14_NETROUTE,
        portproxy_text="",
    )
    snap = _yi14_snapshot(outputs=outputs)
    assert snap is not None
    assert snap.networking_mode == MODE_CONSOMME
    assert snap.guest_reachability == REACH_WINDOWS_HOST


def test_manual_lan_ip_used_as_candidate_when_windows_nics_unreadable() -> None:
    """interop 读不到网卡时，用户提供的 Windows IP 只作候选，不改 guest lanUrl。"""
    outputs = InteropOutputs(
        wsl_version_text=YI14_WSL_VERSION,
        wslinfo_text="nat\n",
        errors={"netip": "timeout", "netroute": "timeout"},
        portproxy_text="0.0.0.0            8080       127.0.0.1         8080\n",
    )
    snap = collect_wsl_network_snapshot(
        platform="wsl",
        guest_ip="172.22.148.94",
        outputs=outputs,
        use_cache=False,
    )
    assert snap is not None
    assert snap.host_lan_ip is None
    config = Config(lanIpStrategy="manual", manualLanIp="10.180.105.74")
    access = describe_instance_access(
        snap,
        host_port=18000,
        route_host="hello-lwa",
        config=config,
    )
    assert access.host_lan_url == "http://10.180.105.74:8080/hello-lwa/"
    assert access.lan_exposure == EXPOSURE_CANDIDATE_UNVERIFIED
    assert access.guest_reachability == REACH_WINDOWS_HOST


def test_parse_wslconfig_localhost_forwarding() -> None:
    assert wn.parse_wslconfig_localhost_forwarding(
        "[wsl2]\nlocalhostForwarding=false\n"
    ) is False
    assert wn.parse_wslconfig_localhost_forwarding(
        "[wsl2]\nlocalhostForwarding=true\n"
    ) is True
    assert wn.parse_wslconfig_localhost_forwarding("[wsl2]\nmemory=8GB\n") is None


def test_localhost_forwarding_disabled_warns_against_loopback_target() -> None:
    outputs = InteropOutputs(
        wsl_version_text=YI14_WSL_VERSION,
        wslinfo_text="nat\n",
        wslconfig_text="[wsl2]\nlocalhostForwarding=false\n",
        netip_text=YI14_NETIP,
        netroute_text=YI14_NETROUTE,
        portproxy_text="",
    )
    snap = _yi14_snapshot(outputs=outputs)
    assert snap is not None
    assert snap.localhost_forwarding is False
    access = describe_instance_access(
        snap, host_port=18000, route_host="hello-lwa", config=Config()
    )
    guidance = access.guidance or ""
    assert "localhostForwarding" in guidance
    assert "127.0.0.1" in guidance
    assert "不可用" in guidance or "关闭" in guidance or "false" in guidance.lower()


def test_public_network_profile_is_independent_capability() -> None:
    outputs = InteropOutputs(
        wsl_version_text=YI14_WSL_VERSION,
        wslinfo_text="nat\n",
        netip_text=YI14_NETIP,
        netroute_text=YI14_NETROUTE,
        portproxy_text="",
        netprofile_text="WLAN|Public\n",
    )
    snap = _yi14_snapshot(outputs=outputs)
    assert snap is not None
    assert snap.network_profiles == [("WLAN", "Public")]
    assert snap.capabilities["netprofile"].status == "available"
    access = describe_instance_access(
        snap, host_port=18000, route_host="hello-lwa", config=Config()
    )
    assert "Public" in (access.guidance or "")


def test_netprofile_read_failure_does_not_drop_portproxy() -> None:
    outputs = InteropOutputs(
        wsl_version_text=YI14_WSL_VERSION,
        wslinfo_text="nat\n",
        netip_text=YI14_NETIP,
        netroute_text=YI14_NETROUTE,
        portproxy_text="0.0.0.0            8080       127.0.0.1         8080\n",
        errors={"netprofile": "access-denied"},
    )
    snap = _yi14_snapshot(outputs=outputs)
    assert snap is not None
    assert snap.capabilities["netprofile"].status == "unknown"
    assert snap.capabilities["portproxy"].status == "available"
    access = describe_instance_access(
        snap, host_port=18000, route_host="hello-lwa", config=Config()
    )
    assert access.lan_exposure == EXPOSURE_CANDIDATE_UNVERIFIED


def test_multi_nic_records_selection_reason() -> None:
    netip = (
        "10.180.105.74|WLAN|Dhcp\n"
        "10.8.0.2|Ethernet 2|Dhcp\n"
        "127.0.0.1|Loopback Pseudo-Interface 1|WellKnown\n"
    )
    outputs = InteropOutputs(
        wsl_version_text=YI14_WSL_VERSION,
        wslinfo_text="nat\n",
        netip_text=netip,
        netroute_text="WLAN|10.180.105.1|0\n",
        portproxy_text="",
    )
    snap = _yi14_snapshot(outputs=outputs)
    assert snap is not None
    assert snap.host_lan_ip == "10.180.105.74"
    assert snap.host_lan_ip_reason is not None
    assert "WLAN" in snap.host_lan_ip_reason
    assert "Ethernet 2" in snap.host_lan_ip_reason


def test_exposure_after_network_change_invalidates_external_verification() -> None:
    previous = snapshot_fingerprint(_yi14_snapshot())
    changed = snapshot_fingerprint(_yi14_snapshot(guest_ip="172.22.148.100"))
    assert (
        wn.exposure_after_network_change(
            EXPOSURE_EXTERNALLY_VERIFIED, previous, previous
        )
        == EXPOSURE_EXTERNALLY_VERIFIED
    )
    assert (
        wn.exposure_after_network_change(
            EXPOSURE_EXTERNALLY_VERIFIED, previous, changed
        )
        == EXPOSURE_CANDIDATE_UNVERIFIED
    )
    assert (
        wn.exposure_after_network_change(
            EXPOSURE_ENTRY_MISSING, previous, changed
        )
        == EXPOSURE_ENTRY_MISSING
    )


def test_expected_port_is_in_access_view_dict() -> None:
    snap = _yi14_snapshot()
    access = describe_instance_access(
        snap, host_port=18000, route_host=None, config=Config()
    )
    assert access.to_dict()["expectedPort"] == 18000


def test_parse_tcp_listen_and_firewall_texts() -> None:
    listens = wn.parse_tcp_listen_text(
        "0.0.0.0|8080\n127.0.0.1|18000\n::|8443\n10.180.105.74|23722\n"
    )
    assert [(item.address, item.port) for item in listens] == [
        ("0.0.0.0", 8080),
        ("127.0.0.1", 18000),
        ("10.180.105.74", 23722),
    ]
    profiles = wn.parse_firewall_profile_text("Domain|NotConfigured\nPrivate|Block\nPublic|Block\n")
    assert profiles == [
        ("Domain", "NotConfigured"),
        ("Private", "Block"),
        ("Public", "Block"),
    ]
    hyperv = wn.parse_hyperv_firewall_text(
        "S|WSL|Block\nR|WSL-In-TCP-22|Inbound|Allow|TCP|22\n"
    )
    assert hyperv.default_inbound == "Block"
    assert hyperv.rules == [("WSL-In-TCP-22", "Inbound", "Allow", "TCP", "22")]


def test_tcp_listen_and_firewall_failures_are_independent() -> None:
    """网卡/portproxy 可读时，监听或防火墙失败只把对应能力标 unknown。"""
    outputs = InteropOutputs(
        wsl_version_text=YI14_WSL_VERSION,
        wslinfo_text="nat\n",
        netip_text=YI14_NETIP,
        netroute_text=YI14_NETROUTE,
        portproxy_text=YI14_PORTPROXY,
        errors={"tcp_listen": "timeout", "firewall": "exit-1"},
    )
    snap = _yi14_snapshot(outputs=outputs)
    assert snap is not None
    assert snap.capabilities["portproxy"].status == "available"
    assert snap.capabilities["tcp_listen"].status == "unknown"
    assert snap.capabilities["tcp_listen"].reason == "timeout"
    assert snap.capabilities["firewall"].status == "unknown"
    assert snap.capabilities["firewall"].reason == "exit-1"
    assert snap.capabilities["hyperv_firewall"].status == "unavailable"
    assert snap.tcp_listens == []
    assert snap.firewall_profiles == []


def test_snapshot_keeps_listen_and_firewall_evidence() -> None:
    outputs = InteropOutputs(
        wsl_version_text=YI14_WSL_VERSION,
        wslinfo_text="nat\n",
        netip_text=YI14_NETIP,
        netroute_text=YI14_NETROUTE,
        portproxy_text="0.0.0.0            8080       127.0.0.1         8080\n",
        tcp_listen_text="0.0.0.0|8080\n127.0.0.1|18000\n",
        firewall_text="Domain|NotConfigured\nPrivate|Block\nPublic|Block\n",
        hyperv_firewall_text="S|WSL|Block\nR|LWA-8080|Inbound|Allow|TCP|8080\n",
    )
    snap = _yi14_snapshot(outputs=outputs)
    assert snap is not None
    assert [(item.address, item.port) for item in snap.tcp_listens] == [
        ("0.0.0.0", 8080),
        ("127.0.0.1", 18000),
    ]
    assert snap.firewall_profiles == [
        ("Domain", "NotConfigured"),
        ("Private", "Block"),
        ("Public", "Block"),
    ]
    assert snap.hyperv_firewall is not None
    assert snap.hyperv_firewall.default_inbound == "Block"
    payload = snap.to_dict()
    assert payload["tcpListens"][0] == {"address": "0.0.0.0", "port": 8080}
    assert payload["firewallProfiles"][2] == {"name": "Public", "defaultInbound": "Block"}
    assert payload["hypervFirewall"]["defaultInbound"] == "Block"
    access = describe_instance_access(
        snap, host_port=18000, route_host="hello-lwa", config=Config()
    )
    assert access.windows_listening is True
    assert access.to_dict()["windowsListening"] is True


def test_fingerprint_ignores_listen_and_firewall_order() -> None:
    base = dict(
        wsl_version_text=YI14_WSL_VERSION,
        wslinfo_text="nat\n",
        netip_text=YI14_NETIP,
        netroute_text=YI14_NETROUTE,
        portproxy_text="0.0.0.0            8080       127.0.0.1         8080\n",
    )
    snap_a = _yi14_snapshot(
        outputs=InteropOutputs(
            **base,
            tcp_listen_text="0.0.0.0|8080\n127.0.0.1|18000\n",
            firewall_text="Private|Block\nPublic|Allow\n",
            hyperv_firewall_text="S|WSL|Block\nR|B|Inbound|Allow|TCP|22\nR|A|Inbound|Allow|TCP|80\n",
        )
    )
    snap_b = _yi14_snapshot(
        outputs=InteropOutputs(
            **base,
            tcp_listen_text="127.0.0.1|18000\n0.0.0.0|8080\n",
            firewall_text="Public|Allow\nPrivate|Block\n",
            hyperv_firewall_text="S|WSL|Block\nR|A|Inbound|Allow|TCP|80\nR|B|Inbound|Allow|TCP|22\n",
        )
    )
    assert snapshot_fingerprint(snap_a) == snapshot_fingerprint(snap_b)


def test_fingerprint_changes_when_listen_or_firewall_changes() -> None:
    base = InteropOutputs(
        wsl_version_text=YI14_WSL_VERSION,
        wslinfo_text="nat\n",
        netip_text=YI14_NETIP,
        netroute_text=YI14_NETROUTE,
        portproxy_text="0.0.0.0            8080       127.0.0.1         8080\n",
        tcp_listen_text="0.0.0.0|8080\n",
        firewall_text="Private|Block\n",
        hyperv_firewall_text="S|WSL|Block\n",
    )
    snap1 = _yi14_snapshot(outputs=base)
    snap2 = _yi14_snapshot(
        outputs=InteropOutputs(
            **{
                **base.__dict__,
                "tcp_listen_text": "0.0.0.0|18000\n",
            }
        )
    )
    snap3 = _yi14_snapshot(
        outputs=InteropOutputs(
            **{
                **base.__dict__,
                "hyperv_firewall_text": "S|WSL|Allow\n",
            }
        )
    )
    assert snapshot_fingerprint(snap1) != snapshot_fingerprint(snap2)
    assert snapshot_fingerprint(snap1) != snapshot_fingerprint(snap3)


def test_build_lan_observation_rejects_unconstrained_fields() -> None:
    snap = _yi14_snapshot()
    with pytest.raises(ValueError, match="vantage"):
        wn.build_lan_observation(
            snapshot=snap,
            vantage="loopback",
            protocol="https",
            evidence="content-match",
            status_code=200,
        )
    with pytest.raises(ValueError, match="protocol"):
        wn.build_lan_observation(
            snapshot=snap,
            vantage="lan-client",
            protocol="tcp",
            evidence="content-match",
            status_code=200,
        )
    with pytest.raises(ValueError, match="evidence"):
        wn.build_lan_observation(
            snapshot=snap,
            vantage="lan-client",
            protocol="https",
            evidence="looks-good",
            status_code=200,
        )
    with pytest.raises(ValueError, match="status"):
        wn.build_lan_observation(
            snapshot=snap,
            vantage="lan-client",
            protocol="https",
            evidence="content-match",
        )


def test_windows_listening_uses_portproxy_listen_port() -> None:
    outputs = InteropOutputs(
        wsl_version_text=YI14_WSL_VERSION,
        wslinfo_text="nat\n",
        netip_text=YI14_NETIP,
        netroute_text=YI14_NETROUTE,
        portproxy_text="0.0.0.0            18080      127.0.0.1         8080\n",
        tcp_listen_text="0.0.0.0|18080\n",
    )
    snap = _yi14_snapshot(outputs=outputs)
    access = describe_instance_access(
        snap, host_port=8080, route_host=None, config=Config()
    )
    assert access.listen_port == 18080
    assert access.connect_port == 8080
    assert access.host_lan_url == "http://10.180.105.74:18080"
    assert access.windows_listening is True


def test_http_observation_does_not_verify_https_entry() -> None:
    outputs = InteropOutputs(
        wsl_version_text="WSL version: 2.4.13.0\nWindows version: 10.0.22621.2428\n",
        wslinfo_text="mirrored\n",
        wslconfig_text="[wsl2]\nnetworkingMode=mirrored\n",
        netip_text="10.180.105.74|WLAN|Dhcp\n127.0.0.1|Loopback Pseudo-Interface 1|WellKnown\n",
        netroute_text="WLAN|10.180.105.1|0\n",
        portproxy_text="",
    )
    snap = collect_wsl_network_snapshot(
        platform="wsl",
        guest_ip="10.180.105.74",
        outputs=outputs,
        use_cache=False,
    )
    observation = wn.build_lan_observation(
        snapshot=snap,
        vantage="lan-client",
        protocol="http",
        evidence="content-match",
        status_code=200,
        url="http://10.180.105.74:8080/hello-lwa/",
    )
    http_access = describe_instance_access(
        snap,
        host_port=18000,
        route_host="hello-lwa",
        config=Config(staticGatewayPort=8080),
        observation=observation,
    )
    assert http_access.lan_exposure == EXPOSURE_EXTERNALLY_VERIFIED
    assert http_access.observation_stale is False
    https_access = describe_instance_access(
        snap,
        host_port=18000,
        route_host="hello-lwa",
        config=Config(
            staticGateway="caddy",
            gatewayTls="internal",
            staticGatewayPort=8080,
            gatewayTlsPort=8443,
        ),
        observation=observation,
    )
    assert https_access.host_lan_url == "https://10.180.105.74:8443/hello-lwa/"
    assert https_access.lan_exposure == EXPOSURE_CANDIDATE_UNVERIFIED
    assert https_access.observation_stale is True


def test_lan_client_observation_marks_externally_verified() -> None:
    proxy = "0.0.0.0            8080       127.0.0.1         8080\n"
    outputs = InteropOutputs(
        wsl_version_text=YI14_WSL_VERSION,
        wslinfo_text="nat\n",
        netip_text=YI14_NETIP,
        netroute_text=YI14_NETROUTE,
        portproxy_text=proxy,
    )
    snap = _yi14_snapshot(outputs=outputs)
    observation = wn.build_lan_observation(
        snapshot=snap,
        vantage="lan-client",
        protocol="http",
        evidence="content-match",
        status_code=200,
        url="http://10.180.105.74:8080/hello-lwa/",
        observed_at="2026-10-22T02:00:00Z",
    )
    access = describe_instance_access(
        snap,
        host_port=18000,
        route_host="hello-lwa",
        config=Config(),
        observation=observation,
    )
    assert access.lan_exposure == EXPOSURE_EXTERNALLY_VERIFIED
    assert access.observation_stale is False
    payload = access.to_dict()
    assert payload["lanExposure"] == EXPOSURE_EXTERNALLY_VERIFIED
    assert payload["lanObservation"]["vantage"] == "lan-client"
    assert payload["lanObservation"]["protocol"] == "http"
    assert payload["lanObservation"]["evidence"] == "content-match"
    assert payload["lanObservation"]["observedAt"] == "2026-10-22T02:00:00Z"
    lines = wn.format_cli_network_lines(lan_url=None, route_url=None, access=access)
    verified = next(value for label, value in lines if label == "局域网入口")
    assert "http://10.180.105.74:8080/hello-lwa/" in verified
    assert "lan-client" in verified
    assert "尚未从其他设备验证" not in verified


def test_windows_host_observation_does_not_claim_external_verification() -> None:
    proxy = "0.0.0.0            8080       127.0.0.1         8080\n"
    outputs = InteropOutputs(
        wsl_version_text=YI14_WSL_VERSION,
        wslinfo_text="nat\n",
        netip_text=YI14_NETIP,
        netroute_text=YI14_NETROUTE,
        portproxy_text=proxy,
    )
    snap = _yi14_snapshot(outputs=outputs)
    observation = wn.build_lan_observation(
        snapshot=snap,
        vantage="windows-host",
        protocol="http",
        evidence="page",
        status_code=200,
        url="http://10.180.105.74:8080/hello-lwa/",
    )
    access = describe_instance_access(
        snap,
        host_port=18000,
        route_host="hello-lwa",
        config=Config(),
        observation=observation,
    )
    assert access.lan_exposure == EXPOSURE_CANDIDATE_UNVERIFIED
    assert access.observation is not None
    assert access.observation.vantage == "windows-host"


def test_describe_instance_access_invalidates_stale_external_observation() -> None:
    proxy = "0.0.0.0            8080       127.0.0.1         8080\n"
    outputs = InteropOutputs(
        wsl_version_text=YI14_WSL_VERSION,
        wslinfo_text="nat\n",
        netip_text=YI14_NETIP,
        netroute_text=YI14_NETROUTE,
        portproxy_text=proxy,
    )
    snap = _yi14_snapshot(outputs=outputs)
    observation = wn.build_lan_observation(
        snapshot=snap,
        vantage="lan-client",
        protocol="http",
        evidence="content-match",
        status_code=200,
        url="http://10.180.105.74:8080/hello-lwa/",
    )
    changed = _yi14_snapshot(outputs=outputs, guest_ip="172.22.148.100")
    access = describe_instance_access(
        changed,
        host_port=18000,
        route_host="hello-lwa",
        config=Config(),
        observation=observation,
    )
    assert access.lan_exposure == EXPOSURE_CANDIDATE_UNVERIFIED
    assert access.observation_stale is True
    lines = wn.format_cli_network_lines(lan_url=None, route_url=None, access=access)
    candidate = next(value for label, value in lines if label == "局域网候选")
    assert "观测已失效" in candidate


@pytest.mark.parametrize("url", [
    None,
    "http://192.0.2.123:8080/hello-lwa/",
    "http://10.180.105.74:9090/hello-lwa/",
    "http://10.180.105.74:8080/other/",
    "http://10.180.105.74:8080/hello-lwa;other",
])
def test_observation_must_identify_the_full_current_entry(url) -> None:
    snap = _yi14_snapshot(outputs=InteropOutputs(
        **{**YI14_OUTPUTS.__dict__,
           "portproxy_text": "0.0.0.0 8080 127.0.0.1 8080\n"}
    ))
    observation = wn.build_lan_observation(
        snapshot=snap, vantage="lan-client", protocol="http",
        evidence="page", status_code=200, url=url,
    )
    access = describe_instance_access(
        snap, host_port=18000, route_host="hello-lwa", config=Config(),
        observation=observation,
    )
    assert access.lan_exposure == EXPOSURE_CANDIDATE_UNVERIFIED
    assert access.observation_stale is True
    assert access.observation == observation


@pytest.mark.parametrize("url", [
    "http://10.180.105.74:bad/hello-lwa/",
    "http://[invalid/hello-lwa/",
    "http://10.180.105.74:99999/hello-lwa/",
    "/hello-lwa/",
    "https://10.180.105.74:8080/hello-lwa/",
    "http://10.180.105.74:0/hello-lwa/",
    "http://10.180.105.74:/hello-lwa/",
    "http://10.180.105.74:8080/hello\nlwa/",
    "http://host^invalid/hello-lwa/",
    "http://unrelated.invalid\\@10.180.105.74:8080/hello-lwa/",
])
def test_build_observation_rejects_invalid_or_inconsistent_url(url) -> None:
    with pytest.raises(ValueError, match="URL|url"):
        wn.build_lan_observation(
            snapshot=_yi14_snapshot(), vantage="lan-client", protocol="http",
            evidence="page", status_code=200, url=url,
        )


@pytest.mark.parametrize("url", [
    "http://10.180.105.74:bad/hello-lwa/", "http://[invalid/hello-lwa/",
])
def test_legacy_invalid_observation_is_retained_but_stale(url) -> None:
    snap = _yi14_snapshot(outputs=InteropOutputs(
        **{**YI14_OUTPUTS.__dict__,
           "portproxy_text": "0.0.0.0 8080 127.0.0.1 8080\n"}
    ))
    observation = wn.observation_from_dict({
        "vantage": "lan-client", "protocol": "http", "evidence": "page",
        "statusCode": 200, "observedAt": "2026-10-22T04:00:00Z",
        "fingerprint": snapshot_fingerprint(snap), "url": url,
    })
    access = describe_instance_access(
        snap, host_port=18000, route_host="hello-lwa", config=Config(),
        observation=observation,
    )
    assert access.lan_exposure == EXPOSURE_CANDIDATE_UNVERIFIED
    assert access.observation_stale is True
    assert access.observation.url == url


def test_missing_url_cannot_verify_changed_port_and_alias() -> None:
    snap = _yi14_snapshot()
    snap.networking_mode = MODE_MIRRORED
    snap.portproxy_rules = []
    observation = wn.build_lan_observation(
        snapshot=snap, vantage="lan-client", protocol="http",
        evidence="page", status_code=200,
    )
    for port, alias in [(8080, "before"), (9090, "after")]:
        access = describe_instance_access(
            snap, host_port=18000, route_host=alias,
            config=Config(staticGatewayPort=port), observation=observation,
        )
        assert access.lan_exposure == EXPOSURE_CANDIDATE_UNVERIFIED
        assert access.observation_stale is True


def test_equivalent_default_port_still_accepts_matching_observation() -> None:
    snap = _yi14_snapshot()
    snap.networking_mode = MODE_MIRRORED
    snap.portproxy_rules = []
    observation = wn.build_lan_observation(
        snapshot=snap, vantage="lan-client", protocol="http",
        evidence="page", status_code=200,
        url="http://10.180.105.74/hello-lwa",
    )
    access = describe_instance_access(
        snap, host_port=18000, route_host="hello-lwa",
        config=Config(staticGatewayPort=80), observation=observation,
    )
    assert access.lan_exposure == EXPOSURE_EXTERNALLY_VERIFIED
    assert access.observation_stale is False
