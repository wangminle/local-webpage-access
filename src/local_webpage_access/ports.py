"""端口池与访问入口（WBS-06）。

职责：
1. 从配置读取端口池范围。
2. 排除 registry 已登记端口。
3. 探测宿主机真实监听端口（跨平台，用 socket bind 探测）。
4. 分配/释放端口。
5. 推断局域网 IP，生成 ``lanUrl`` 与 ``healthUrl``。

对应 V1 设计说明第 6 节。
"""

from __future__ import annotations

import ipaddress
import socket
import subprocess
from dataclasses import dataclass
from typing import Iterable

from local_webpage_access.config import Config
from local_webpage_access.errors import PortError
from local_webpage_access.lan_network import (
    is_benchmark_ip,
    lan_candidates,
    parse_lan_url,
    usable_lan_address,
)
from local_webpage_access.logging import get_logger
from local_webpage_access.models import NetworkConfig
from local_webpage_access.registry import Registry

log = get_logger("ports")

_HEALTH_HOST = "127.0.0.1"
_PROBE_TARGETS = ("8.8.8.8", "1.1.1.1", "114.114.114.114")


def is_port_in_use(port: int, *, host: str = "0.0.0.0") -> bool:
    """探测端口是否被占用（跨平台，尝试独占 bind）。

    不设置 ``SO_REUSEADDR``：在 Windows 上 ``SO_REUSEADDR`` 允许多个套接字绑定
    同一端口，会把"已有进程监听"误判为"空闲"（BUG-002）。独占 bind 真实反映
    "此刻能否绑定该端口"，正是端口分配器需要的信息。bind 成功说明空闲，
    失败说明被占用或权限不足。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return False
    except OSError:
        return True
    finally:
        sock.close()


def is_port_listening(port: int, *, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    """端口上是否有进程正在监听（可接受连接）。

    用 ``connect`` 探测：连得上说明有活跃监听者；连不上（如 ECONNREFUSED）
    说明无监听者。与 :func:`is_port_in_use`（独占 bind）的关键区别在于
    TIME_WAIT 残留——刚被停止的服务，其 health-check 连接会在端口上留下
    TIME_WAIT，让 bind 失败但 *不影响* connect。因此**端口复用判定**（BUG-045）
    应使用本函数：stop 后端口无活跃监听者即可复用，不被 TIME_WAIT 误判占用。

    分配器 :meth:`PortAllocator.allocate` 仍用 :func:`is_port_in_use`，因为那里
    需要"此刻能否真正绑定"的严格语义（要避开 TIME_WAIT）。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


@dataclass(frozen=True)
class LanResolution:
    """LAN 地址选择结果；没有可信地址时也保留可解释的来源与原因。"""

    ip: str | None = None
    interface: str | None = None
    source: str = "none"
    reason: str | None = None
    probe_address: str | None = None


def _udp_source_address() -> str | None:
    for target in _PROBE_TARGETS:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((target, 80))
            address = sock.getsockname()[0]
            if usable_lan_address(address):
                return address
        except OSError:
            continue
        finally:
            sock.close()
    return None


def detect_lan_resolution() -> LanResolution:
    """按接口证据选择地址；UDP 源地址在枚举失败时仅作诊断线索。"""
    candidates = lan_candidates()
    if candidates is not None:
        if candidates:
            selected = candidates[0]
            return LanResolution(
                ip=selected.address,
                interface=selected.interface,
                source="interface",
            )
        return LanResolution(source="auto", reason="no_candidates")
    probe_address = _udp_source_address()
    if probe_address:
        return LanResolution(
            source="udp",
            reason="source_unverified",
            probe_address=probe_address,
        )
    return LanResolution(source="none", reason="enumeration_unavailable")


def detect_lan_ip() -> str | None:
    """按有线、Wi-Fi 和接口顺序选择可发布的 LAN IPv4。"""
    return detect_lan_resolution().ip


def lan_reason_text(reason: str | None, probe_address: str | None = None) -> str | None:
    if reason == "manual_benchmark":
        return "手工 LAN IP 位于基准测试网段，请设置可用于局域网的地址"
    if reason == "no_candidates":
        return "系统接口枚举成功，但没有可发布的 LAN 候选"
    if reason == "source_unverified":
        return (
            f"系统接口枚举不可用；UDP 源地址 {probe_address or '(未知)'} 的接口来源未验证，"
            "不能作为自动 LAN 地址"
        )
    if reason == "enumeration_unavailable":
        return "系统接口枚举不可用，且未获得可验证的 UDP 来源"
    return None


def _normalize_ip(value: str) -> str | None:
    """规范化 IP 地址字符串，供集合比较。

    - 去掉 zone-id（``fe80::1%eth0`` → ``fe80::1``）。
    - IPv4-mapped IPv6（``::ffff:1.2.3.4``）归一到 IPv4。
    - 非法输入返回 ``None``。
    """
    if not value:
        return None
    addr_str = value.split("%", 1)[0].strip()
    try:
        addr = ipaddress.ip_address(addr_str)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return str(addr)


def _tailscale_ips() -> set[str]:
    """尽力获取本机 Tailscale 地址（命令缺失/超时/异常均返回空集）。"""
    result: set[str] = set()
    # 与 tailscale_network 探测共用 CLI 发现：服务 PATH 缺失时仍能找到 macOS GUI /
    # Windows 常规安装，否则本机自己的 tailnet 地址会被浏览量统计误判为外部访客。
    from local_webpage_access.tailscale_network import find_tailscale

    executable = find_tailscale()
    if not executable:
        return result
    for family in ("-4", "-6"):
        try:
            proc = subprocess.run(  # noqa: S603 — 路径来自 find_tailscale，失败即降级
                [executable, "ip", family],
                capture_output=True,
                text=True,
                timeout=1.5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            continue
        for line in proc.stdout.splitlines():
            n = _normalize_ip(line.strip())
            if n:
                result.add(n)
    return result


def detect_local_ips() -> set[str]:
    """构造本机**实际持有**的地址集合（规范化后）。

    来源：loopback ∪ :func:`detect_lan_ip` ∪ hostname 解析地址 ∪ 可用时
    ``tailscale ip -4/-6`` 输出。任一来源失败静默降级。用于浏览量 IP 列表的
    「本机」标记——只标记本机真实地址，**不能**把整个 ``100.64.0.0/10`` 网段
    都判为本机（否则同一 tailnet 的其他节点也会被误标）。
    """
    found: set[str] = set()
    for loopback in ("127.0.0.1", "::1"):
        n = _normalize_ip(loopback)
        if n:
            found.add(n)
    lan = detect_lan_ip()
    if lan:
        n = _normalize_ip(lan)
        if n:
            found.add(n)
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None)
    except OSError:
        infos = []
    for info in infos:
        sockaddr = info[4]
        if sockaddr and sockaddr[0]:
            n = _normalize_ip(str(sockaddr[0]))
            if n:
                found.add(n)
    found |= _tailscale_ips()
    return found


def is_local_ip(ip: str, local_ips: set[str] | None = None) -> bool:
    """``ip`` 是否为本机实际持有的地址。

    ``local_ips`` 为 ``None`` 时即时调用 :func:`detect_local_ips`（会派生子进程，
    不要在循环里逐 IP 调用——先取一次集合再复用）。
    """
    n = _normalize_ip(ip)
    if n is None:
        return False
    if local_ips is None:
        local_ips = detect_local_ips()
    return n in local_ips


def resolve_lan_resolution(config: Config) -> LanResolution:
    """根据配置策略返回地址、来源和不可发布原因。"""
    if config.lanIpStrategy == "manual":
        if is_benchmark_ip(config.manualLanIp):
            return LanResolution(source="manual", reason="manual_benchmark")
        return LanResolution(ip=config.manualLanIp, source="manual")
    return detect_lan_resolution()


def resolve_lan_ip(config: Config) -> str | None:
    """根据配置策略解析局域网 IP（auto 或 manual）。"""
    if config.lanIpStrategy == "manual":
        return resolve_lan_resolution(config).ip
    return detect_lan_ip()


def format_http_host(host: str) -> str:
    """把主机名格式化为 URL 权威部分可用的形式。

    IPv6 字面量必须加方括号（``[::1]``），否则 ``host:port`` 会因冒号歧义
    变成非法 URL。已带方括号或非 IPv6（IPv4 / 主机名）原样返回。
    """
    raw = host.strip()
    if not raw:
        return raw
    if raw.startswith("[") and raw.endswith("]"):
        return raw
    try:
        if ipaddress.ip_address(raw).version == 6:
            return f"[{raw}]"
    except ValueError:
        pass
    return raw


def entry_scheme(config: Config | None = None) -> str:
    """HTTPS 首版交付（W07）：对外入口 URL 的 scheme。

    ``gatewayTls=internal`` → ``https``；否则 ``http``（现状）。
    """
    if config is not None:
        from local_webpage_access.config import tls_enabled

        if tls_enabled(config):
            return "https"
    return "http"


def lan_entry_port(config: Config) -> int | None:
    """W07：对外别名入口端口——TLS 开启时为 gatewayTlsPort，否则 staticGatewayPort。"""
    from local_webpage_access.config import tls_enabled

    if tls_enabled(config):
        return config.gatewayTlsPort
    return config.staticGatewayPort


def manager_entry_url(config: Config, lan_ip: str | None) -> str | None:
    """W07：管理面对外入口 URL。

    TLS 关闭（现状）：``http://<ip>:<managerPort>``（直连 manager）。
    TLS 开启：``https://<ip>:<managerTlsPort>``（Caddy 独立 origin 反代——
    manager 本体已收敛为仅回环监听）。
    """
    if not lan_ip:
        return None
    from local_webpage_access.config import tls_enabled

    if tls_enabled(config):
        return f"https://{format_http_host(lan_ip)}:{config.managerTlsPort}/"
    return f"http://{format_http_host(lan_ip)}:{config.managerPort}/"


def build_lan_url(
    lan_ip: str | None, port: int, *, config: Config | None = None
) -> str | None:
    """生成局域网访问 URL。无法确定 IP 时返回 ``None``。

    W07：提供 ``config`` 且 TLS 开启时合成 https（网关入口语义；实例
    直连口在收敛部署中仅回环可达）。
    """
    if not lan_ip:
        return None
    scheme = entry_scheme(config)
    return f"{scheme}://{format_http_host(lan_ip)}:{port}"


def build_health_url(port: int, *, host: str = _HEALTH_HOST) -> str:
    """生成健康检查 URL（本机回环——回环上游保持明文 HTTP，D5）。"""
    return f"http://{format_http_host(host)}:{port}"


def build_route_url(
    lan_ip: str | None,
    gateway_port: int | None,
    alias: str,
    *,
    config: Config | None = None,
) -> str | None:
    """生成路径别名的统一入口 URL（IMP-006）。

    形如 ``http://<lan_ip>:8080/<alias>/``（TLS 开启时
    ``https://<lan_ip>:8443/<alias>/``，W07）。``lan_ip`` 无法确定或
    ``gateway_port`` 为 ``None``（别名入口关闭）时返回 ``None``——此时只能
    通过 hostPort 访问。端口为 80（http）/443（https）时省略显式端口。
    """
    if not lan_ip or gateway_port is None:
        return None
    scheme = entry_scheme(config)
    default_port = 443 if scheme == "https" else 80
    host = format_http_host(lan_ip)
    port_part = "" if gateway_port == default_port else f":{gateway_port}"
    return f"{scheme}://{host}{port_part}/{alias}/"


class PortAllocator:
    """端口池分配器。

    分配顺序：端口池范围内 → 跳过 registry 已登记 → 跳过宿主机无法 bind 的端口
    → 跳过显式排除。
    """

    def __init__(self, config: Config, registry: Registry) -> None:
        self.config = config
        self.registry = registry

    def allocated_ports(self) -> set[int]:
        """registry 中已登记的端口集合。"""
        return set(self.registry.allocated_ports())

    def candidate_ports(self) -> Iterable[int]:
        return self.config.portPool.as_range()

    def allocate(
        self,
        instance_id: str,
        *,
        exclude: set[int] | None = None,
        probe_host: bool = True,
    ) -> int:
        """为实例分配一个可用端口。

        Args:
            instance_id: 实例 ID，用于登记。
            exclude: 额外排除的端口集合。
            probe_host: 是否探测宿主机 bind 可用性（测试时可关闭）。

        Returns:
            分配到的端口号。

        Raises:
            PortError: 端口池耗尽。
        """
        exclude = exclude or set()
        allocated = self.allocated_ports()

        for port in self.candidate_ports():
            if port in allocated or port in exclude:
                continue
            if probe_host:
                # 新分配必须以「此刻能否真正 bind」为准；connect 只能
                # 证明有无监听者，会漏掉 bind-only 占用并导致后续启动失败。
                if is_port_in_use(port):
                    log.debug("端口 %d 无法 bind，跳过", port)
                    continue
            # 并发安全登记：若端口在登记前被其他实例抢走（BUG-017），
            # allocate_port 返回 False，跳到下一个候选端口重试。
            if not self.registry.allocate_port(instance_id, port):
                log.debug("端口 %d 被其他实例抢先占用，跳过", port)
                allocated.add(port)
                continue
            log.info("为实例 %s 分配端口 %d", instance_id, port)
            return port

        raise PortError(
            f"端口池 [{self.config.portPool.start}, {self.config.portPool.end}] 已耗尽",
            pool_start=self.config.portPool.start,
            pool_end=self.config.portPool.end,
        )

    def release(self, port: int) -> None:
        """释放端口。"""
        self.registry.release_port(port)
        log.info("释放端口 %d", port)

    def release_instance(self, instance_id: str) -> None:
        """释放实例占用的所有端口。"""
        self.registry.release_instance_ports(instance_id)


def _keep_lan_url(
    url: str | None, *, scheme: str, port: int | None, path: str,
) -> str | None:
    parsed = parse_lan_url(url)
    if (
        port is not None and parsed is not None
        and not is_benchmark_ip(parsed.hostname)
        and parsed.scheme == scheme and (parsed.port or (443 if scheme == "https" else 80)) == port
        and (parsed.path or "/") == path
    ):
        return url
    return None


def build_network_entry(
    config: Config,
    host_port: int,
    *,
    internal_port: int | None = None,
    lan_ip: str | None = None,
    path_alias: str | None = None,
    previous: NetworkConfig | None = None,
) -> dict:
    """构造访问入口，探测失败时可保留仍匹配端口和别名的旧 URL。

    直连实例仍用 HTTP；路径别名采用当前网关协议和端口。
    """
    if lan_ip is None:
        lan_ip = resolve_lan_ip(config)
    entry_port = lan_entry_port(config)
    lan_url = build_lan_url(lan_ip, host_port)
    route_url = (
        build_route_url(lan_ip, entry_port, path_alias, config=config)
        if path_alias is not None else None
    )
    if lan_ip is None and previous is not None:
        lan_url = _keep_lan_url(previous.lanUrl, scheme="http", port=host_port, path="/")
        if path_alias is not None:
            route_url = _keep_lan_url(
                previous.routeUrl, scheme=entry_scheme(config), port=entry_port, path=f"/{path_alias}/"
            )
    return {
        "host": getattr(config, "instanceBindHost", "0.0.0.0") or "0.0.0.0",
        "internalPort": internal_port,
        "hostPort": host_port,
        "routeMode": "name" if path_alias is not None else "port",
        "routeHost": path_alias,
        "routeUrl": route_url,
        "lanUrl": lan_url,
        "healthUrl": build_health_url(host_port),
    }


__all__ = [
    "PortAllocator",
    "is_port_in_use",
    "is_port_listening",
    "detect_lan_ip",
    "resolve_lan_ip",
    "format_http_host",
    "build_lan_url",
    "build_health_url",
    "build_route_url",
    "build_network_entry",
    "entry_scheme",
    "lan_entry_port",
    "manager_entry_url",
]
