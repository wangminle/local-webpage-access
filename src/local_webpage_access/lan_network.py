"""按宿主机接口选择要发布的 LAN 地址。"""

from __future__ import annotations

import ipaddress
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import SplitResult, urlsplit

from local_webpage_access.platform_detect import subprocess_hidden_kwargs

_BENCHMARK = ipaddress.IPv4Network("198.18.0.0/15")
_RANK = {"有线": 0, "Wi-Fi": 1, "其他": 2}


@dataclass(frozen=True)
class LanCandidate:
    interface: str
    address: str
    medium: str
    index: int = 0


def parse_lan_url(url: str | None) -> SplitResult | None:
    """解析落盘的 HTTP(S) 入口，畸形历史值返回 None。"""
    if not url:
        return None
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme in {"http", "https"} and parsed.hostname
            and parsed.username is None and (parsed.port is None or parsed.port > 0)
        ):
            return parsed
    except ValueError:
        pass
    return None


def lan_url_host(url: str | None) -> str | None:
    parsed = parse_lan_url(url)
    return parsed.hostname if parsed is not None else None


def is_benchmark_ip(value: str | None) -> bool:
    if not value:
        return False
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        address = address.ipv4_mapped
    return address is not None and address in _BENCHMARK


def usable_lan_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        address.is_loopback or address.is_unspecified or address.is_multicast
        or address.is_link_local or is_benchmark_ip(value) or str(address) == "255.255.255.255"
    )


def _run(args: list[str], *, timeout: float = 1.5) -> str | None:
    try:
        result = subprocess.run(
            args, capture_output=True, encoding="utf-8", errors="replace",
            check=False, timeout=timeout, **subprocess_hidden_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _rows(text: str | None) -> list[dict] | None:
    if text is None:
        return None
    try:
        rows = json.loads(text)
    except ValueError:
        return None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return None
    return rows


def _linux_candidates() -> list[LanCandidate] | None:
    rows = _rows(_run(["ip", "-j", "-d", "-4", "addr", "show", "up"]))
    if rows is None:
        return None
    candidates = []
    for row in rows:
        name = row.get("ifname", "")
        flags = set(row.get("flags", ()))
        kind = row.get("linkinfo", {}).get("info_kind", "")
        if (
            not name or "UP" not in flags or flags & {"NO-CARRIER", "POINTOPOINT", "LOOPBACK"}
            or row.get("operstate") in {"DOWN", "LOWERLAYERDOWN", "DORMANT"}
            or row.get("link_type") != "ether" or row.get("master")
            or kind in {"tun", "wireguard", "dummy", "vxlan", "geneve"}
            or name.startswith(("docker", "br-", "tailscale", "virbr"))
        ):
            continue
        if name.startswith("wl") or (Path("/sys/class/net") / name / "wireless").is_dir():
            medium = "Wi-Fi"
        elif kind in {"bridge", "veth", "vlan", "macvlan", "ipvlan"}:
            medium = "其他"
        else:
            medium = "有线"
        for info in row.get("addr_info", ()):
            if info.get("family") == "inet" and info.get("scope") == "global":
                if not (info.get("deprecated") or info.get("tentative") or info.get("dadfailed")):
                    candidates.append(LanCandidate(name, info.get("local", ""), medium))
    return candidates


def _macos_candidates() -> list[LanCandidate] | None:
    hardware = _run(["/usr/sbin/networksetup", "-listallhardwareports"])
    interfaces = _run(["/sbin/ifconfig", "-a"])
    if hardware is None or interfaces is None:
        return None
    media = {}
    for label, name in re.findall(r"^Hardware Port: (.+)\nDevice: (\S+)", hardware, re.M):
        label = label.casefold()
        if label in {"wi-fi", "airport"}:
            media[name] = "Wi-Fi"
        elif "ethernet" in label or label.endswith(" lan"):
            media[name] = "有线"
        else:
            media[name] = "其他"
    if not media:
        return None

    candidates = []
    for block in re.split(r"(?=^[^\s:]+: flags=)", interfaces, flags=re.M):
        name = block.partition(":")[0]
        if name not in media and not name.startswith(("bridge", "vlan", "bond")):
            continue
        flags = set(block.splitlines()[0].partition("<")[2].partition(">")[0].split(","))
        if "UP" not in flags or flags & {"POINTOPOINT", "LOOPBACK"}:
            continue
        if re.search(r"^\s+status: inactive\s*$", block, re.M):
            continue
        for address in re.findall(r"^\s+inet\s+(\S+)", block, re.M):
            candidates.append(LanCandidate(name, address, media.get(name, "其他")))
    return candidates


_WINDOWS_QUERY = """
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$addresses = @(Get-NetIPAddress -AddressFamily IPv4 | Where-Object AddressState -eq Preferred)
$result = @(foreach ($adapter in Get-NetAdapter -Physical | Where-Object Status -eq Up) {
    foreach ($address in $addresses | Where-Object InterfaceIndex -eq $adapter.ifIndex) {
        [pscustomobject]@{
            index = $adapter.ifIndex
            interface = $adapter.Name
            medium = [int]$adapter.NdisPhysicalMedium
            address = $address.IPAddress
        }
    }
})
ConvertTo-Json -InputObject $result -Compress
"""


def _windows_candidates() -> list[LanCandidate] | None:
    rows = _rows(_run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_QUERY], timeout=4.0))
    if rows is None:
        return None
    # MSFT_NetAdapter.NdisPhysicalMedium: 1/9 为无线 LAN，14 为 802.3。
    media = {1: "Wi-Fi", 9: "Wi-Fi", 14: "有线"}
    return [
        LanCandidate(row["interface"], row["address"], media.get(row["medium"], "其他"), row["index"])
        for row in rows
    ]


def _interface_order(name: str) -> tuple[str | int, ...]:
    return tuple(int(part) if part.isdecimal() else part.casefold() for part in re.split(r"(\d+)", name))


@lru_cache(maxsize=4)
def _discover(platform: str, _interval: int) -> tuple[LanCandidate, ...] | None:
    reader = {"linux": _linux_candidates, "darwin": _macos_candidates, "win32": _windows_candidates}.get(platform)
    candidates = reader() if reader else None
    if candidates is None:
        return None
    return tuple(sorted(
        (item for item in candidates if usable_lan_address(item.address)),
        key=lambda item: (_RANK[item.medium], item.index, _interface_order(item.interface)),
    ))


def lan_candidates() -> tuple[LanCandidate, ...] | None:
    """返回排序后的 IPv4 候选。None 表示枚举不可用，空元组表示无可用接口。"""
    return _discover(sys.platform, int(time.monotonic() // 3))
