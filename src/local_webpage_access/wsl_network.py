"""WSL 网络模式与访问范围（BUG-923 / PLN-054）。

只读发现：识别实际网络模式、guest / Windows 本机 / 局域网入口，不修改
Windows 网卡、转发、防火墙或网络模式。一次请求共享快照，失败标 unknown。
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

from local_webpage_access.config import Config
from local_webpage_access.logging import get_logger
from local_webpage_access.platform_detect import PLATFORM_WSL, detect_platform

log = get_logger("wsl_network")

MODE_NAT = "nat"
MODE_MIRRORED = "mirrored"
MODE_CONSOMME = "consomme"
MODE_UNKNOWN = "unknown"

REACH_LOOPBACK = "loopback"
REACH_WINDOWS_HOST = "windows-host"
REACH_LAN = "lan"
REACH_UNKNOWN = "unknown"

EXPOSURE_INTERNAL_OK = "internal-ok"
EXPOSURE_ENTRY_MISSING = "entry-missing"
EXPOSURE_CANDIDATE_UNVERIFIED = "candidate-unverified"
EXPOSURE_EXTERNALLY_VERIFIED = "externally-verified"
EXPOSURE_UNKNOWN = "unknown"

VANTAGE_GUEST = "guest"
VANTAGE_WINDOWS_HOST = "windows-host"
VANTAGE_LAN_CLIENT = "lan-client"
_VANTAGES = {VANTAGE_GUEST, VANTAGE_WINDOWS_HOST, VANTAGE_LAN_CLIENT}
_PROTOCOLS = {"http", "https"}
_EVIDENCE_SUCCESS = {"content-match", "page"}
_EVIDENCE_FAILURE = {"timeout", "connection-refused", "tls-error", "auth-401"}
_EVIDENCE = _EVIDENCE_SUCCESS | _EVIDENCE_FAILURE

_CACHE_TTL_SEC = 45.0
_CMD_TIMEOUT_SEC = 5.0
_WIN11_MIN_BUILD = 22000
_WIN11_22H2_BUILD = 22621

_MODE_ALIASES = {
    "nat": MODE_NAT,
    "mirrored": MODE_MIRRORED,
    "mirror": MODE_MIRRORED,
    "consomme": MODE_CONSOMME,
    "virtioproxy": MODE_CONSOMME,
    "virtio-proxy": MODE_CONSOMME,
}

_VSWITCH_ALIASES = ("vethernet (wsl)", "vethernet (default switch)")
_IP_PORT_ROW = re.compile(
    r"(?P<lip>\d+\.\d+\.\d+\.\d+)\s+(?P<lport>\d+)\s+"
    r"(?P<cip>\d+\.\d+\.\d+\.\d+)\s+(?P<cport>\d+)\s*$"
)

Runner = Callable[..., Any]

_cache_lock = threading.Lock()
_cache: tuple[float, WslNetworkSnapshot | None] | None = None


@dataclass
class CapabilityStatus:
    name: str
    status: str  # available | unavailable | unknown
    reason: str | None = None
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "status": self.status}
        if self.reason:
            d["reason"] = self.reason
        if self.source:
            d["source"] = self.source
        return d


@dataclass
class WindowsNic:
    ip: str
    alias: str
    prefix_origin: str

    def to_dict(self) -> dict[str, str]:
        return {"ip": self.ip, "alias": self.alias, "prefixOrigin": self.prefix_origin}


@dataclass
class PortProxyRule:
    listen_address: str
    listen_port: int
    connect_address: str
    connect_port: int
    targets_guest_dynamic_ip: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "listenAddress": self.listen_address,
            "listenPort": self.listen_port,
            "connectAddress": self.connect_address,
            "connectPort": self.connect_port,
            "targetsGuestDynamicIp": self.targets_guest_dynamic_ip,
        }


@dataclass
class TcpListen:
    address: str
    port: int

    def to_dict(self) -> dict[str, Any]:
        return {"address": self.address, "port": self.port}


@dataclass
class HyperVFirewall:
    default_inbound: str | None = None
    vm_name: str | None = None
    rules: list[tuple[str, str, str, str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "defaultInbound": self.default_inbound,
            "vmName": self.vm_name,
            "rules": [
                {
                    "displayName": name,
                    "direction": direction,
                    "action": action,
                    "protocol": protocol,
                    "localPorts": ports,
                }
                for name, direction, action, protocol, ports in self.rules
            ],
        }


@dataclass
class ExternalLanObservation:
    vantage: str
    protocol: str
    evidence: str
    observed_at: str
    fingerprint: str
    status_code: int | None = None
    url: str | None = None

    def is_success(self) -> bool:
        if self.evidence not in _EVIDENCE_SUCCESS or self.status_code is None:
            return False
        return 200 <= self.status_code < 300

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "vantage": self.vantage,
            "protocol": self.protocol,
            "evidence": self.evidence,
            "observedAt": self.observed_at,
            "fingerprint": self.fingerprint,
        }
        if self.status_code is not None:
            d["statusCode"] = self.status_code
        if self.url:
            d["url"] = self.url
        return d


@dataclass
class InteropOutputs:
    """测试注入的只读命令输出；生产路径由 runner 填充。"""

    wsl_version_text: str | None = None
    wslinfo_text: str | None = None
    wslconfig_text: str | None = None
    netip_text: str | None = None
    netroute_text: str | None = None
    portproxy_text: str | None = None
    netprofile_text: str | None = None
    tcp_listen_text: str | None = None
    firewall_text: str | None = None
    hyperv_firewall_text: str | None = None
    errors: dict[str, str] = field(default_factory=dict)


@dataclass
class WslNetworkSnapshot:
    architecture: str | None
    package_version: str | None
    windows_nt_version: str | None
    windows_product: str | None
    declared_networking_mode: str | None
    observed_networking_mode: str | None
    networking_mode: str
    networking_mode_reason: str | None
    guest_ip: str | None
    host_lan_ip: str | None
    guest_reachability: str
    nics: list[WindowsNic] = field(default_factory=list)
    default_route_aliases: list[str] = field(default_factory=list)
    portproxy_rules: list[PortProxyRule] = field(default_factory=list)
    capabilities: dict[str, CapabilityStatus] = field(default_factory=dict)
    captured_at: str | None = None
    localhost_forwarding: bool | None = None
    network_profiles: list[tuple[str, str]] = field(default_factory=list)
    host_lan_ip_reason: str | None = None
    tcp_listens: list[TcpListen] = field(default_factory=list)
    firewall_profiles: list[tuple[str, str]] = field(default_factory=list)
    hyperv_firewall: HyperVFirewall | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "architecture": self.architecture,
            "packageVersion": self.package_version,
            "windowsNtVersion": self.windows_nt_version,
            "windowsProduct": self.windows_product,
            "declaredNetworkingMode": self.declared_networking_mode,
            "observedNetworkingMode": self.observed_networking_mode,
            "networkingMode": self.networking_mode,
            "networkingModeReason": self.networking_mode_reason,
            "guestIp": self.guest_ip,
            "hostLanIp": self.host_lan_ip,
            "guestReachability": self.guest_reachability,
            "nics": [n.to_dict() for n in self.nics],
            "defaultRouteAliases": list(self.default_route_aliases),
            "portproxyRules": [r.to_dict() for r in self.portproxy_rules],
            "capabilities": {k: v.to_dict() for k, v in self.capabilities.items()},
            "capturedAt": self.captured_at,
            "localhostForwarding": self.localhost_forwarding,
            "networkProfiles": [
                {"alias": alias, "category": category}
                for alias, category in self.network_profiles
            ],
            "hostLanIpReason": self.host_lan_ip_reason,
            "tcpListens": [item.to_dict() for item in self.tcp_listens],
            "firewallProfiles": [
                {"name": name, "defaultInbound": action}
                for name, action in self.firewall_profiles
            ],
            "hypervFirewall": (
                self.hyperv_firewall.to_dict() if self.hyperv_firewall else None
            ),
        }


@dataclass
class InstanceAccessView:
    guest_reachability: str
    lan_exposure: str
    host_lan_url: str | None
    listen_port: int | None = None
    connect_port: int | None = None
    connect_address: str | None = None
    dynamic_ip_forwarding: bool = False
    guidance: str | None = None
    note: str | None = None
    # BUG-937：该实例入口在 Windows 侧应被转发/放行的端口——有路径别名取
    # 网关入口端口，否则取实例 hostPort；doctor 文案与 guidance 命令共用此值。
    expected_port: int | None = None
    windows_listening: bool | None = None
    observation: ExternalLanObservation | None = None
    observation_stale: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "guestReachability": self.guest_reachability,
            "lanExposure": self.lan_exposure,
            "hostLanUrl": self.host_lan_url,
            "listenPort": self.listen_port,
            "connectPort": self.connect_port,
            "connectAddress": self.connect_address,
            "dynamicIpForwarding": self.dynamic_ip_forwarding,
            "guidance": self.guidance,
            "note": self.note,
            "expectedPort": self.expected_port,
            "windowsListening": self.windows_listening,
            "lanObservation": self.observation.to_dict() if self.observation else None,
            "lanObservationStale": self.observation_stale,
        }


def reset_wsl_network_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def classify_windows_product(nt_version: str | None) -> str | None:
    """用 Build 区分 Win10/Win11，不用 NT major=10。"""
    parsed = _split_nt(nt_version)
    if parsed is None:
        return None
    _major, _minor, build, _rev = parsed
    if build >= _WIN11_MIN_BUILD:
        return "windows11"
    if build >= 10240:
        return "windows10"
    return None


def parse_windows_nt_version(text: str | None) -> str | None:
    if not text:
        return None
    for line in text.splitlines():
        lower = line.lower()
        if "windows" in lower and ("version" in lower or "版本" in line):
            match = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
            if match:
                return match.group(1)
    return None


def parse_networking_mode_text(text: str | None) -> str:
    raw = (text or "").strip().splitlines()
    if not raw:
        return MODE_UNKNOWN
    token = raw[0].strip().strip("\"'").lower()
    if not token:
        return MODE_UNKNOWN
    return _MODE_ALIASES.get(token, token)


def _parse_wslconfig_keys(text: str | None) -> dict[str, str]:
    keys: dict[str, str] = {}
    if not text:
        return keys
    in_wsl2 = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            in_wsl2 = line.lower() == "[wsl2]"
            continue
        if not in_wsl2 or "=" not in line:
            continue
        key, value = line.split("=", 1)
        keys[key.strip().lower()] = value.strip().strip("\"'")
    return keys


def parse_wslconfig_networking_mode(text: str | None) -> str | None:
    raw = _parse_wslconfig_keys(text).get("networkingmode")
    if not raw:
        return None
    parsed = parse_networking_mode_text(raw)
    return None if parsed == MODE_UNKNOWN else parsed


def parse_wslconfig_localhost_forwarding(text: str | None) -> bool | None:
    raw = _parse_wslconfig_keys(text).get("localhostforwarding")
    if raw is None:
        return None
    token = raw.lower()
    if token in {"true", "1", "yes"}:
        return True
    if token in {"false", "0", "no"}:
        return False
    return None


def parse_netip_text(text: str | None) -> list[WindowsNic]:
    nics: list[WindowsNic] = []
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        ip, alias, origin = parts[0], parts[1], parts[2]
        if _valid_ipv4(ip):
            nics.append(WindowsNic(ip=ip, alias=alias, prefix_origin=origin))
    return nics


def parse_netroute_aliases(text: str | None) -> list[str]:
    aliases: list[str] = []
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if parts and parts[0]:
            aliases.append(parts[0])
    return aliases


def parse_portproxy_text(text: str | None) -> list[PortProxyRule]:
    rules: list[PortProxyRule] = []
    for line in (text or "").splitlines():
        match = _IP_PORT_ROW.search(line.strip())
        if not match:
            continue
        rules.append(
            PortProxyRule(
                listen_address=match.group("lip"),
                listen_port=int(match.group("lport")),
                connect_address=match.group("cip"),
                connect_port=int(match.group("cport")),
            )
        )
    return rules


def parse_netprofile_text(text: str | None) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 2 and parts[0]:
            rows.append((parts[0], parts[1]))
    return rows


def parse_tcp_listen_text(text: str | None) -> list[TcpListen]:
    listens: list[TcpListen] = []
    seen: set[tuple[str, int]] = set()
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or not _valid_ipv4(parts[0]):
            continue
        try:
            port = int(parts[1])
        except ValueError:
            continue
        key = (parts[0], port)
        if key in seen:
            continue
        seen.add(key)
        listens.append(TcpListen(address=parts[0], port=port))
    return listens


def parse_firewall_profile_text(text: str | None) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 2 and parts[0]:
            rows.append((parts[0], parts[1]))
    return rows


def parse_hyperv_firewall_text(text: str | None) -> HyperVFirewall:
    result = HyperVFirewall()
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if not parts:
            continue
        kind = parts[0].upper()
        if kind == "S" and len(parts) >= 3:
            if result.vm_name is None:
                result.vm_name = parts[1] or None
                result.default_inbound = parts[2] or None
        elif kind == "R" and len(parts) >= 6:
            result.rules.append((parts[1], parts[2], parts[3], parts[4], parts[5]))
    return result


def build_lan_observation(
    *,
    snapshot: WslNetworkSnapshot | None,
    vantage: str,
    protocol: str,
    evidence: str,
    status_code: int | None = None,
    url: str | None = None,
    observed_at: str | None = None,
) -> ExternalLanObservation:
    """受约束的外部观测：位置/协议/证据/时间齐全，且取值来自固定枚举。"""
    if vantage not in _VANTAGES:
        raise ValueError("vantage 必须是 guest / windows-host / lan-client")
    if protocol not in _PROTOCOLS:
        raise ValueError("protocol 必须是 http 或 https")
    if evidence not in _EVIDENCE:
        raise ValueError("evidence 不在允许的响应证据集合中")
    if evidence in _EVIDENCE_SUCCESS and status_code is None:
        raise ValueError("成功响应证据必须提供 status")
    if snapshot is None:
        raise ValueError("缺少当前网络快照，无法记录外部观测")
    if url:
        _observation_url_identity(url, protocol)
    stamp = observed_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ExternalLanObservation(
        vantage=vantage,
        protocol=protocol,
        evidence=evidence,
        observed_at=stamp,
        fingerprint=snapshot_fingerprint(snapshot),
        status_code=status_code,
        url=url or None,
    )


def observation_from_dict(data: dict[str, Any] | None) -> ExternalLanObservation | None:
    if not isinstance(data, dict):
        return None
    vantage = str(data.get("vantage") or "")
    protocol = str(data.get("protocol") or "")
    evidence = str(data.get("evidence") or "")
    observed_at = str(data.get("observedAt") or data.get("observed_at") or "")
    fingerprint = str(data.get("fingerprint") or "")
    if not (vantage and protocol and evidence and observed_at and fingerprint):
        return None
    if vantage not in _VANTAGES or protocol not in _PROTOCOLS or evidence not in _EVIDENCE:
        return None
    raw_status = data.get("statusCode", data.get("status_code"))
    try:
        status_code = int(raw_status) if raw_status is not None else None
    except (TypeError, ValueError):
        return None
    url = data.get("url")
    return ExternalLanObservation(
        vantage=vantage,
        protocol=protocol,
        evidence=evidence,
        observed_at=observed_at,
        fingerprint=fingerprint,
        status_code=status_code,
        url=str(url) if url else None,
    )


def observation_from_network(network: Any) -> ExternalLanObservation | None:
    raw = getattr(network, "externalLanObservation", None) if network is not None else None
    return observation_from_dict(raw if isinstance(raw, dict) else None)


def select_host_lan_ip(nics: list[WindowsNic], default_route_aliases: list[str]) -> str | None:
    ip, _reason = _select_host_lan(nics, default_route_aliases)
    return ip


def _select_host_lan(
    nics: list[WindowsNic], default_route_aliases: list[str]
) -> tuple[str | None, str | None]:
    usable = [n for n in nics if _is_physical_lan_nic(n)]
    if not usable:
        return None, None
    route_set = {a.lower() for a in default_route_aliases}
    on_route = [n for n in usable if n.alias.lower() in route_set]
    pool = on_route or usable
    dhcp = [n for n in pool if n.prefix_origin.lower() == "dhcp"]
    chosen = dhcp[0] if dhcp else pool[0]
    if on_route:
        reason = f"默认路由网卡 {chosen.alias}（{chosen.ip}）"
    else:
        reason = f"物理网卡 {chosen.alias}（{chosen.ip}）"
    others = [n for n in usable if n.ip != chosen.ip]
    if others:
        reason += "；其他候选：" + "、".join(f"{n.alias} {n.ip}" for n in others)
    return chosen.ip, reason


def exposure_after_network_change(
    exposure: str, previous_fingerprint: str, current_fingerprint: str
) -> str:
    """网络/映射变化后，历史「外部已验证」失效为待验证候选。"""
    if (
        exposure == EXPOSURE_EXTERNALLY_VERIFIED
        and previous_fingerprint != current_fingerprint
    ):
        return EXPOSURE_CANDIDATE_UNVERIFIED
    return exposure


def snapshot_fingerprint(snapshot: WslNetworkSnapshot | None) -> str:
    if snapshot is None:
        return "none"
    rules = tuple(
        sorted(
            (r.listen_address, r.listen_port, r.connect_address, r.connect_port)
            for r in snapshot.portproxy_rules
        )
    )
    listens = tuple(sorted((item.address, item.port) for item in snapshot.tcp_listens))
    profiles = tuple(sorted(snapshot.firewall_profiles))
    hyperv = ""
    if snapshot.hyperv_firewall:
        hyperv = (
            f"{snapshot.hyperv_firewall.default_inbound}|"
            f"{tuple(sorted(snapshot.hyperv_firewall.rules))}"
        )
    return "|".join(
        [
            snapshot.networking_mode,
            snapshot.guest_ip or "",
            snapshot.host_lan_ip or "",
            repr(rules),
            f"listen:{_cap_status(snapshot, 'tcp_listen')}:{listens!r}",
            f"fw:{_cap_status(snapshot, 'firewall')}:{profiles!r}",
            f"hv:{_cap_status(snapshot, 'hyperv_firewall')}:{hyperv}",
        ]
    )


def _cap_status(snapshot: WslNetworkSnapshot, name: str) -> str:
    cap = snapshot.capabilities.get(name)
    return cap.status if cap is not None else "missing"


def collect_wsl_network_snapshot(
    *,
    platform: str | None = None,
    guest_ip: str | None = None,
    outputs: InteropOutputs | None = None,
    runner: Runner | None = None,
    use_cache: bool = True,
    now: float | None = None,
) -> WslNetworkSnapshot | None:
    global _cache
    plat = platform if platform is not None else detect_platform()
    if plat != PLATFORM_WSL:
        return None
    stamp = time.monotonic() if now is None else now
    if use_cache:
        with _cache_lock:
            if _cache and stamp - _cache[0] < _CACHE_TTL_SEC:
                return _cache[1]
    snap = _collect_uncached(
        guest_ip=guest_ip, outputs=outputs, runner=runner or subprocess.run
    )
    if use_cache:
        with _cache_lock:
            _cache = (stamp, snap)
    return snap


def describe_instance_access(
    snapshot: WslNetworkSnapshot | None,
    *,
    host_port: int | None,
    route_host: str | None,
    config: Config,
    observation: ExternalLanObservation | None = None,
) -> InstanceAccessView:
    if snapshot is None:
        return InstanceAccessView(
            guest_reachability=REACH_LAN,
            lan_exposure=EXPOSURE_UNKNOWN,
            host_lan_url=None,
            observation=observation,
        )
    from local_webpage_access.ports import (
        build_lan_url,
        build_route_url,
        lan_entry_port,
    )

    entry_port = lan_entry_port(config)
    host_lan_ip = _effective_host_lan_ip(snapshot, config)
    match = None
    if route_host and entry_port:
        match = _matching_forward(snapshot, entry_port)
    if match is None and host_port:
        match = _matching_forward(snapshot, host_port)
    dynamic = any(r.targets_guest_dynamic_ip for r in snapshot.portproxy_rules)
    host_lan_url = None
    listen_port = connect_port = None
    connect_address = None
    if match and host_lan_ip:
        listen_port = match.listen_port
        connect_port = match.connect_port
        connect_address = match.connect_address
        if route_host and connect_port == entry_port:
            host_lan_url = build_route_url(
                host_lan_ip, listen_port, route_host, config=config
            )
        else:
            host_lan_url = build_lan_url(host_lan_ip, listen_port)
        exposure = EXPOSURE_CANDIDATE_UNVERIFIED
    elif snapshot.networking_mode == MODE_MIRRORED and host_lan_ip:
        if route_host and entry_port:
            host_lan_url = build_route_url(
                host_lan_ip, entry_port, route_host, config=config
            )
        elif host_port:
            host_lan_url = build_lan_url(host_lan_ip, host_port)
        exposure = EXPOSURE_CANDIDATE_UNVERIFIED
    elif _portproxy_status_unknown(snapshot):
        exposure = EXPOSURE_UNKNOWN
    elif snapshot.host_lan_ip is None and snapshot.networking_mode == MODE_UNKNOWN:
        exposure = EXPOSURE_UNKNOWN
    else:
        exposure = EXPOSURE_ENTRY_MISSING
    note = _scope_note(snapshot)
    guidance = _guidance(
        snapshot, config, entry_port, host_port, route_host=route_host
    )
    expected_port = _guidance_port(
        entry_port=entry_port, host_port=host_port, route_host=route_host
    )
    observation_stale = False
    if observation is not None:
        current_fp = snapshot_fingerprint(snapshot)
        entry_mismatch = not _observation_matches_current_entry(
            observation, host_lan_url
        )
        observation_stale = observation.fingerprint != current_fp or entry_mismatch
        if (
            observation.vantage == VANTAGE_LAN_CLIENT
            and observation.is_success()
            and not observation_stale
            and exposure in {EXPOSURE_CANDIDATE_UNVERIFIED, EXPOSURE_EXTERNALLY_VERIFIED}
        ):
            exposure = EXPOSURE_EXTERNALLY_VERIFIED
        exposure = exposure_after_network_change(
            exposure, observation.fingerprint, current_fp
        )
    listen_check_port = listen_port if listen_port is not None else expected_port
    return InstanceAccessView(
        guest_reachability=snapshot.guest_reachability,
        lan_exposure=exposure,
        host_lan_url=host_lan_url,
        listen_port=listen_port,
        connect_port=connect_port,
        connect_address=connect_address,
        dynamic_ip_forwarding=dynamic,
        guidance=guidance,
        note=note,
        expected_port=expected_port,
        windows_listening=_windows_listening(snapshot, listen_check_port),
        observation=observation,
        observation_stale=observation_stale,
    )


def _effective_host_lan_ip(snapshot: WslNetworkSnapshot, config: Config) -> str | None:
    """发现的 Windows 局域网 IP；读不到时才用用户提供的 manualLanIp 作候选。"""
    if snapshot.host_lan_ip:
        return snapshot.host_lan_ip
    if config.lanIpStrategy == "manual" and config.manualLanIp:
        if config.manualLanIp != snapshot.guest_ip:
            return config.manualLanIp
    return None


def guest_url_caption(reachability: str) -> str:
    if reachability == REACH_WINDOWS_HOST:
        return "仅 Windows 宿主可达"
    if reachability == REACH_LAN:
        return "局域网"
    if reachability == REACH_LOOPBACK:
        return "本机"
    if reachability == REACH_UNKNOWN:
        return "范围未知"
    return "访问地址"


def format_cli_network_lines(
    *,
    lan_url: str | None,
    route_url: str | None,
    access: InstanceAccessView | None,
) -> list[tuple[str, str]]:
    """返回 (label, value) 供 CLI 打印。label 已按可达性改写。"""
    lines: list[tuple[str, str]] = []
    reach = access.guest_reachability if access else REACH_LAN
    caption = guest_url_caption(reach)
    if lan_url:
        if reach == REACH_LAN:
            lines.append(("局域网", lan_url))
        else:
            lines.append((caption, lan_url))
    if route_url:
        lines.append(("路径", route_url))
    if access and access.host_lan_url:
        if access.lan_exposure == EXPOSURE_EXTERNALLY_VERIFIED:
            shown = access.host_lan_url
            extra = "已从其他设备验证"
            if access.observation:
                obs = access.observation
                shown = obs.url or shown
                extra = (
                    f"已从其他设备验证：{obs.vantage} {obs.protocol} "
                    f"{obs.evidence} {obs.observed_at}"
                )
            lines.append(("局域网入口", f"{shown}（{extra}）"))
        elif access.observation_stale:
            lines.append(
                ("局域网候选", f"{access.host_lan_url}（观测已失效：网络或入口不匹配，或缺少有效 URL）")
            )
        else:
            lines.append(("局域网候选", f"{access.host_lan_url}（尚未从其他设备验证）"))
    elif access and access.lan_exposure == EXPOSURE_ENTRY_MISSING:
        lines.append(("局域网入口", "未发现（LWA 不会自动配置 Windows 转发）"))
    if access and access.dynamic_ip_forwarding:
        lines.append(("转发风险", "现有 portproxy 指向会漂移的 WSL guest IP"))
    return lines


def _collect_uncached(
    *,
    guest_ip: str | None,
    outputs: InteropOutputs | None,
    runner: Runner,
) -> WslNetworkSnapshot:
    from local_webpage_access.platform_support import (
        _parse_wsl_package_version_text,
        detect_wsl_kernel_kind,
    )
    from local_webpage_access.ports import detect_lan_ip

    caps: dict[str, CapabilityStatus] = {}
    data = outputs or _gather_outputs(runner, caps)
    _fill_capability_from_outputs(data, caps)

    version_text = data.wsl_version_text
    nt = parse_windows_nt_version(version_text)
    pkg = _parse_wsl_package_version_text(version_text or "") if version_text else None
    declared = parse_wslconfig_networking_mode(data.wslconfig_text)
    observed = None
    if data.wslinfo_text is not None and "wslinfo" not in data.errors:
        observed = parse_networking_mode_text(data.wslinfo_text)
        if observed == MODE_UNKNOWN:
            observed = None
    nics = parse_netip_text(data.netip_text)
    routes = parse_netroute_aliases(data.netroute_text)
    host_lan, host_lan_reason = _select_host_lan(nics, routes)
    ip = guest_ip if guest_ip is not None else detect_lan_ip()
    rules = parse_portproxy_text(data.portproxy_text)
    for rule in rules:
        rule.targets_guest_dynamic_ip = bool(ip and rule.connect_address == ip)
    mode, reason = _resolve_mode(declared, observed, ip, host_lan, nt)
    reach = _guest_reachability(mode, ip, host_lan)
    captured = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return WslNetworkSnapshot(
        architecture=detect_wsl_kernel_kind() or "2",
        package_version=pkg,
        windows_nt_version=nt,
        windows_product=classify_windows_product(nt),
        declared_networking_mode=declared,
        observed_networking_mode=observed,
        networking_mode=mode,
        networking_mode_reason=reason,
        guest_ip=ip,
        host_lan_ip=host_lan,
        guest_reachability=reach,
        nics=nics,
        default_route_aliases=routes,
        portproxy_rules=rules,
        capabilities=caps,
        captured_at=captured,
        localhost_forwarding=parse_wslconfig_localhost_forwarding(data.wslconfig_text),
        network_profiles=parse_netprofile_text(data.netprofile_text),
        host_lan_ip_reason=host_lan_reason,
        tcp_listens=parse_tcp_listen_text(data.tcp_listen_text),
        firewall_profiles=parse_firewall_profile_text(data.firewall_text),
        hyperv_firewall=(
            parse_hyperv_firewall_text(data.hyperv_firewall_text)
            if data.hyperv_firewall_text is not None
            else None
        ),
    )


def _gather_outputs(runner: Runner, caps: dict[str, CapabilityStatus]) -> InteropOutputs:
    from local_webpage_access.platform_support import _decode_wsl_cli_output, _wsl_exe_candidates

    errors: dict[str, str] = {}
    version_text = _run_first(
        runner,
        [(exe, "--version") for exe in _wsl_exe_candidates()],
        caps,
        "wsl_version",
        errors,
    )
    wslinfo_text = _run_first(
        runner, [("wslinfo", "--networking-mode")], caps, "wslinfo", errors
    )
    netip_text = _run_powershell(
        runner,
        "Get-NetIPAddress -AddressFamily IPv4 | ForEach-Object { "
        "'{0}|{1}|{2}' -f $_.IPAddress, $_.InterfaceAlias, $_.PrefixOrigin }",
        caps,
        "netip",
        errors,
    )
    netroute_text = _run_powershell(
        runner,
        "Get-NetRoute -DestinationPrefix '0.0.0.0/0' | ForEach-Object { "
        "'{0}|{1}|{2}' -f $_.InterfaceAlias, $_.NextHop, $_.RouteMetric }",
        caps,
        "netroute",
        errors,
    )
    portproxy_text = _run_first(
        runner,
        [
            (
                "/mnt/c/Windows/System32/netsh.exe",
                "interface",
                "portproxy",
                "show",
                "all",
            )
        ],
        caps,
        "portproxy",
        errors,
    )
    netprofile_text = _run_powershell(
        runner,
        "Get-NetConnectionProfile | ForEach-Object { "
        "'{0}|{1}' -f $_.InterfaceAlias, $_.NetworkCategory }",
        caps,
        "netprofile",
        errors,
    )
    tcp_listen_text = _run_powershell(
        runner,
        "Get-NetTCPConnection -State Listen -ErrorAction Stop | "
        "ForEach-Object { '{0}|{1}' -f $_.LocalAddress, $_.LocalPort }",
        caps,
        "tcp_listen",
        errors,
    )
    firewall_text = _run_powershell(
        runner,
        "Get-NetFirewallProfile -ErrorAction Stop | ForEach-Object { "
        "'{0}|{1}' -f $_.Name, $_.DefaultInboundAction }",
        caps,
        "firewall",
        errors,
    )
    hyperv_firewall_text = _run_powershell(
        runner,
        "Get-NetFirewallHyperVVMSetting -ErrorAction Stop | ForEach-Object { "
        "'S|{0}|{1}' -f $_.Name, $_.DefaultInboundAction }; "
        "Get-NetFirewallHyperVRule -ErrorAction Stop | ForEach-Object { "
        "'R|{0}|{1}|{2}|{3}|{4}' -f $_.DisplayName, $_.Direction, $_.Action, "
        "$_.Protocol, (($_.LocalPorts | ForEach-Object { $_ }) -join ',') }",
        caps,
        "hyperv_firewall",
        errors,
    )
    wslconfig_text, cfg_err = _read_wslconfig(runner)
    if cfg_err:
        errors["wslconfig"] = cfg_err
        caps["wslconfig"] = CapabilityStatus(
            "wslconfig", "unknown", reason=cfg_err, source="filesystem"
        )
    else:
        caps["wslconfig"] = CapabilityStatus("wslconfig", "available", source="filesystem")
    _ = _decode_wsl_cli_output  # 保持与现有解码实现同路径（经 _run_first）
    return InteropOutputs(
        wsl_version_text=version_text,
        wslinfo_text=wslinfo_text,
        wslconfig_text=wslconfig_text,
        netip_text=netip_text,
        netroute_text=netroute_text,
        portproxy_text=portproxy_text,
        netprofile_text=netprofile_text,
        tcp_listen_text=tcp_listen_text,
        firewall_text=firewall_text,
        hyperv_firewall_text=hyperv_firewall_text,
        errors=errors,
    )


def _fill_capability_from_outputs(
    data: InteropOutputs, caps: dict[str, CapabilityStatus]
) -> None:
    mapping = {
        "wslinfo": data.wslinfo_text,
        "netip": data.netip_text,
        "netroute": data.netroute_text,
        "portproxy": data.portproxy_text,
        "netprofile": data.netprofile_text,
        "tcp_listen": data.tcp_listen_text,
        "firewall": data.firewall_text,
        "hyperv_firewall": data.hyperv_firewall_text,
        "wsl_version": data.wsl_version_text,
        "wslconfig": data.wslconfig_text,
    }
    for name, text in mapping.items():
        if name in caps:
            continue
        err = data.errors.get(name)
        if err:
            caps[name] = CapabilityStatus(name, "unknown", reason=err, source="injected")
        elif text is None:
            caps[name] = CapabilityStatus(name, "unavailable", source="injected")
        else:
            caps[name] = CapabilityStatus(name, "available", source="injected")


def _run_first(
    runner: Runner,
    arg_sets: list[tuple[str, ...]],
    caps: dict[str, CapabilityStatus],
    name: str,
    errors: dict[str, str],
) -> str | None:
    from local_webpage_access.platform_support import _decode_wsl_cli_output

    last_err = "unavailable"
    for args in arg_sets:
        text, err = _run_capture(runner, list(args))
        if err:
            last_err = err
            continue
        try:
            decoded = _decode_wsl_cli_output(text)
        except UnicodeDecodeError:
            last_err = "decode-error"
            continue
        caps[name] = CapabilityStatus(name, "available", source=args[0])
        return decoded
    errors[name] = last_err
    caps[name] = CapabilityStatus(name, "unknown", reason=last_err, source="interop")
    return None


def _run_powershell(
    runner: Runner,
    command: str,
    caps: dict[str, CapabilityStatus],
    name: str,
    errors: dict[str, str],
) -> str | None:
    ps = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    args = [
        ps,
        "-NoProfile",
        "-Command",
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + command,
    ]
    return _run_first(runner, [tuple(args)], caps, name, errors)


def _run_capture(runner: Runner, args: list[str]) -> tuple[bytes | str | None, str | None]:
    try:
        proc = runner(
            args,
            capture_output=True,
            text=False,
            timeout=_CMD_TIMEOUT_SEC,
            check=False,
        )
    except FileNotFoundError:
        return None, "not-found"
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except TypeError:
        try:
            proc = runner(args, capture_output=True, text=False, timeout=_CMD_TIMEOUT_SEC)
        except (OSError, subprocess.SubprocessError, TypeError, ValueError) as exc:
            return None, type(exc).__name__.lower()
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return None, type(exc).__name__.lower()
    stdout = getattr(proc, "stdout", None)
    code = getattr(proc, "returncode", 1)
    if code not in (0, None):
        return stdout, f"exit-{code}"
    return stdout, None


def _read_wslconfig(runner: Runner) -> tuple[str | None, str | None]:
    from pathlib import Path

    from local_webpage_access.platform_support import _decode_wsl_cli_output

    ps = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    text, err = _run_capture(runner, [ps, "-NoProfile", "-Command", "Write-Output $env:USERPROFILE"])
    if err or not text:
        return None, err or "unavailable"
    try:
        win_path = _decode_wsl_cli_output(text).strip().splitlines()
    except UnicodeDecodeError:
        return None, "decode-error"
    if not win_path:
        return None, "unavailable"
    profile = win_path[0].strip().strip('"')
    # C:\Users\name → /mnt/c/Users/name/.wslconfig
    mnt = _windows_path_to_drvfs(profile)
    if mnt is None:
        return None, "path-unmapped"
    cfg = Path(mnt) / ".wslconfig"
    try:
        if not cfg.is_file():
            return "", None
        return cfg.read_text(encoding="utf-8", errors="replace"), None
    except OSError as exc:
        return None, type(exc).__name__.lower()


def _windows_path_to_drvfs(win_path: str) -> str | None:
    match = re.match(r"^([A-Za-z]):\\(.*)$", win_path.replace("/", "\\"))
    if not match:
        return None
    drive, rest = match.group(1).lower(), match.group(2).replace("\\", "/")
    return f"/mnt/{drive}/{rest}"


def _resolve_mode(
    declared: str | None,
    observed: str | None,
    guest_ip: str | None,
    host_lan_ip: str | None,
    nt_version: str | None,
) -> tuple[str, str | None]:
    if observed in {MODE_NAT, MODE_MIRRORED, MODE_CONSOMME}:
        if (
            declared == MODE_MIRRORED
            and observed == MODE_NAT
            and classify_windows_product(nt_version) == "windows10"
        ):
            return MODE_NAT, "Windows 10 不支持 mirrored，配置意图已回退为 NAT"
        if declared and declared != observed:
            return observed, f"配置意图 {declared}，实际观测为 {observed}"
        return observed, "wslinfo"
    if observed and observed not in {MODE_UNKNOWN, None}:
        return observed, "保留未识别的运行态模式名称"
    if guest_ip and host_lan_ip and guest_ip == host_lan_ip:
        return MODE_MIRRORED, "guest 地址与 Windows 局域网 IP 相同"
    if guest_ip and host_lan_ip and guest_ip != host_lan_ip:
        return MODE_NAT, "guest 地址与 Windows 局域网 IP 不同"
    return MODE_UNKNOWN, "未能读取 wslinfo，且无法从地址关系推断"


def _guest_reachability(mode: str, guest_ip: str | None, host_lan_ip: str | None) -> str:
    if mode == MODE_MIRRORED and guest_ip and host_lan_ip and guest_ip == host_lan_ip:
        return REACH_LAN
    if mode == MODE_NAT:
        return REACH_WINDOWS_HOST
    if guest_ip and host_lan_ip and guest_ip == host_lan_ip:
        return REACH_LAN
    if guest_ip and host_lan_ip and guest_ip != host_lan_ip:
        return REACH_WINDOWS_HOST
    return REACH_UNKNOWN


_LOOPBACK_CONNECT = {"127.0.0.1", "localhost", "::1"}


def _rule_targets_wsl_service(
    rule: PortProxyRule, snapshot: WslNetworkSnapshot, service_port: int
) -> bool:
    if rule.connect_port != service_port:
        return False
    connect = (rule.connect_address or "").strip().lower()
    if connect in _LOOPBACK_CONNECT:
        return True
    if snapshot.guest_ip and connect == snapshot.guest_ip:
        return True
    return False


def _matching_forward(snapshot: WslNetworkSnapshot, port: int) -> PortProxyRule | None:
    host = snapshot.host_lan_ip
    for rule in snapshot.portproxy_rules:
        if not _rule_targets_wsl_service(rule, snapshot, port):
            continue
        if rule.listen_address in {"0.0.0.0", "*", host or ""}:
            return rule
    return None


def _scope_note(snapshot: WslNetworkSnapshot) -> str:
    if snapshot.guest_reachability == REACH_WINDOWS_HOST:
        guest = snapshot.guest_ip or "?"
        host = snapshot.host_lan_ip or "未知"
        return (
            f"WSL {snapshot.networking_mode}：{guest} 仅 Windows 宿主可达，"
            f"不是局域网地址（Windows 局域网 IP {host}）"
        )
    if snapshot.guest_reachability == REACH_LAN:
        return f"WSL {snapshot.networking_mode}：guest 地址与 Windows 局域网地址一致"
    return f"WSL 网络范围未知（mode={snapshot.networking_mode}）"


def _guidance_port(
    *,
    entry_port: int | None,
    host_port: int | None,
    route_host: str | None,
) -> int | None:
    if route_host:
        return entry_port or host_port
    return host_port or entry_port


def _portproxy_status_unknown(snapshot: WslNetworkSnapshot) -> bool:
    cap = snapshot.capabilities.get("portproxy")
    return cap is not None and cap.status == "unknown"


def _observation_matches_current_entry(
    observation: ExternalLanObservation, host_lan_url: str | None
) -> bool:
    """成功观测必须对应正在展示的协议/端口/路径，不能只比网络指纹。"""
    if not host_lan_url or not observation.url:
        return False
    try:
        return _observation_url_identity(
            observation.url, observation.protocol
        ) == _observation_url_identity(host_lan_url, observation.protocol)
    except ValueError:
        # 历史坏记录保留作证据，读时失效，不能让 status/API/doctor 整体失败。
        return False


def _observation_url_identity(url: str, protocol: str) -> tuple[str, str, int, str]:
    """写入前严格验证；读取时由调用方把历史非法 URL 当作入口不匹配。"""
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        port = _url_port(parsed)
        if (
            scheme not in _PROTOCOLS
            or scheme != protocol
            or not host
            or not parsed.netloc
            or parsed.netloc.endswith(":")
            or "\\" in url
            or any(char.isspace() or ord(char) < 32 for char in url)
            or port is None
            or not 1 <= port <= 65535
        ):
            raise ValueError("invalid entry")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            ascii_host = host.rstrip(".").encode("idna").decode("ascii")
            if len(ascii_host) > 253 or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label, re.I)
                for label in ascii_host.split(".")
            ):
                raise ValueError("invalid host")
        return scheme, host.lower(), port, _normalize_entry_path(parsed.path)
    except ValueError as exc:
        raise ValueError("url 必须是协议一致且主机、端口有效的完整 HTTP(S) URL") from exc


def _url_port(parsed: Any) -> int | None:
    if parsed.port is not None:
        return int(parsed.port)
    if parsed.scheme == "https":
        return 443
    if parsed.scheme == "http":
        return 80
    return None


def _normalize_entry_path(path: str) -> str:
    text = path or "/"
    if not text.startswith("/"):
        text = "/" + text
    if not text.endswith("/"):
        text = text + "/"
    return text


def _windows_listening(snapshot: WslNetworkSnapshot, port: int | None) -> bool | None:
    cap = snapshot.capabilities.get("tcp_listen")
    if cap is None or cap.status != "available" or port is None:
        return None
    host = snapshot.host_lan_ip
    for item in snapshot.tcp_listens:
        if item.port != port:
            continue
        if item.address in {"0.0.0.0", "*", host or ""}:
            return True
    return False


def _direct_or_gateway_scheme(
    config: Config, port: int | None, entry_port: int | None
) -> str:
    """网关 TLS 入口用 https；实例直连端口仍是 HTTP。"""
    from local_webpage_access.config import tls_enabled

    if tls_enabled(config) and port is not None and port == entry_port:
        return "https"
    return "http"


def _guidance(
    snapshot: WslNetworkSnapshot,
    config: Config,
    entry_port: int | None,
    host_port: int | None,
    *,
    route_host: str | None = None,
) -> str | None:
    if snapshot.networking_mode == MODE_MIRRORED:
        port = _guidance_port(
            entry_port=entry_port, host_port=host_port, route_host=route_host
        )
        if not port:
            return None
        return (
            "当前为 mirrored。LWA 不会修改 Windows 防火墙。"
            "若其他设备仍无法访问，请在管理员 PowerShell 审阅后按实际端口添加 "
            "Hyper-V 防火墙规则，例如："
            f" New-NetFirewallHyperVRule -Name 'LWA-{port}' "
            f"-DisplayName 'LWA-{port}' -Direction Inbound "
            "-VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' "
            f"-Protocol TCP -LocalPorts {port} -Action Allow。"
            "不要把整段端口池或 Caddy admin 端口暴露到局域网。"
        )
    if snapshot.networking_mode not in {MODE_NAT, MODE_UNKNOWN}:
        return (
            f"当前网络模式为 {snapshot.networking_mode}。LWA 只读展示已发现的入口，"
            "不会自动修改 Windows 网络。"
        )
    port = _guidance_port(
        entry_port=entry_port, host_port=host_port, route_host=route_host
    )
    if not port:
        return None
    scheme = _direct_or_gateway_scheme(config, port, entry_port)
    loopback = f"{scheme}://127.0.0.1:{port}/"
    lines = [
        "当前为 WSL NAT。guest 地址仅 Windows 宿主可达。LWA 不会自动执行下列命令。",
        f"请先在 Windows 本机确认 {loopback} 可打开，再于管理员 PowerShell 审阅后自行配置：",
        (
            "netsh interface portproxy add v4tov4 "
            f"listenaddress=0.0.0.0 listenport={port} "
            f"connectaddress=127.0.0.1 connectport={port}"
        ),
        (
            "netsh advfirewall firewall add rule "
            f'name="LWA {port}" dir=in action=allow protocol=TCP localport={port}'
        ),
        "转发目标请用 127.0.0.1（依赖 localhost 转发），不要指向会随 WSL 重启变化的 guest IP。",
    ]
    if snapshot.localhost_forwarding is False:
        lines.append(
            "当前 .wslconfig 声明 localhostForwarding=false，指向 127.0.0.1 的 "
            "portproxy 不可用；LWA 不会修改该设置。"
        )
    if any(category.lower() == "public" for _alias, category in snapshot.network_profiles):
        lines.append(
            "当前 Windows 网络配置文件含 Public，入站可能被防火墙拦截。"
            "LWA 不会修改网络类别。"
        )
    parsed = _split_nt(snapshot.windows_nt_version)
    if snapshot.windows_product == "windows11" and parsed and parsed[2] >= _WIN11_22H2_BUILD:
        lines.append("也可评估 .wslconfig 中 networkingMode=mirrored（需 Windows 11 22H2+）。")
    return "\n".join(lines)


def _is_physical_lan_nic(nic: WindowsNic) -> bool:
    if not _valid_ipv4(nic.ip):
        return False
    addr = ipaddress.ip_address(nic.ip)
    if addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified:
        return False
    alias = nic.alias.lower()
    if any(token in alias for token in _VSWITCH_ALIASES):
        return False
    if "loopback" in alias:
        return False
    return True


def _valid_ipv4(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).version == 4
    except ValueError:
        return False


def _split_nt(nt_version: str | None) -> tuple[int, int, int, int] | None:
    if not nt_version:
        return None
    parts = nt_version.split(".")
    if len(parts) < 3:
        return None
    try:
        major = int(parts[0])
        minor = int(parts[1])
        build = int(parts[2])
        rev = int(parts[3]) if len(parts) > 3 else 0
    except ValueError:
        return None
    return major, minor, build, rev


__all__ = [
    "MODE_NAT",
    "MODE_MIRRORED",
    "MODE_CONSOMME",
    "MODE_UNKNOWN",
    "REACH_LOOPBACK",
    "REACH_WINDOWS_HOST",
    "REACH_LAN",
    "REACH_UNKNOWN",
    "EXPOSURE_INTERNAL_OK",
    "EXPOSURE_ENTRY_MISSING",
    "EXPOSURE_CANDIDATE_UNVERIFIED",
    "EXPOSURE_EXTERNALLY_VERIFIED",
    "EXPOSURE_UNKNOWN",
    "VANTAGE_GUEST",
    "VANTAGE_WINDOWS_HOST",
    "VANTAGE_LAN_CLIENT",
    "CapabilityStatus",
    "WindowsNic",
    "PortProxyRule",
    "TcpListen",
    "HyperVFirewall",
    "ExternalLanObservation",
    "InteropOutputs",
    "WslNetworkSnapshot",
    "InstanceAccessView",
    "reset_wsl_network_cache",
    "classify_windows_product",
    "parse_windows_nt_version",
    "parse_networking_mode_text",
    "parse_wslconfig_networking_mode",
    "parse_wslconfig_localhost_forwarding",
    "parse_netprofile_text",
    "parse_tcp_listen_text",
    "parse_firewall_profile_text",
    "parse_hyperv_firewall_text",
    "build_lan_observation",
    "observation_from_dict",
    "observation_from_network",
    "exposure_after_network_change",
    "parse_netip_text",
    "parse_netroute_aliases",
    "parse_portproxy_text",
    "select_host_lan_ip",
    "snapshot_fingerprint",
    "collect_wsl_network_snapshot",
    "describe_instance_access",
    "guest_url_caption",
    "format_cli_network_lines",
]
