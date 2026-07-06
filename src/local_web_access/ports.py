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

import socket
from typing import Iterable

from local_web_access.config import Config
from local_web_access.errors import PortError
from local_web_access.logging import get_logger
from local_web_access.registry import Registry

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


def is_port_listening(
    port: int, *, host: str = "127.0.0.1", timeout: float = 0.3
) -> bool:
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


def detect_lan_ip() -> str | None:
    """推断本机局域网 IP。

    通过 UDP socket "连接" 一个外部地址来获知出口网卡的本地地址，
    不真正发送数据包。离线或多网卡时可能返回 ``None``。
    """
    for target in _PROBE_TARGETS:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((target, 80))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            continue
        finally:
            sock.close()
    return None


def resolve_lan_ip(config: Config) -> str | None:
    """根据配置策略解析局域网 IP（auto 或 manual）。"""
    if config.lanIpStrategy == "manual":
        return config.manualLanIp
    return detect_lan_ip()


def build_lan_url(lan_ip: str | None, port: int) -> str | None:
    """生成局域网访问 URL。无法确定 IP 时返回 ``None``。"""
    if not lan_ip:
        return None
    return f"http://{lan_ip}:{port}"


def build_health_url(port: int, *, host: str = _HEALTH_HOST) -> str:
    """生成健康检查 URL（本机回环）。"""
    return f"http://{host}:{port}"


class PortAllocator:
    """端口池分配器。

    分配顺序：端口池范围内 → 跳过 registry 已登记 → 跳过宿主机已监听 → 跳过显式排除。
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
            probe_host: 是否探测宿主机监听状态（测试时可关闭）。

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
            if probe_host and is_port_in_use(port):
                log.debug("端口 %d 已被宿主机占用，跳过", port)
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


def build_network_entry(
    config: Config,
    host_port: int,
    *,
    internal_port: int | None = None,
    lan_ip: str | None = None,
) -> dict:
    """构造 ``local-web.json`` 的 ``network`` 字段。

    对应 WBS-06.07~09。
    """
    if lan_ip is None:
        lan_ip = resolve_lan_ip(config)
    return {
        "host": "0.0.0.0",
        "internalPort": internal_port,
        "hostPort": host_port,
        "routeMode": "port",
        "routeHost": None,
        "lanUrl": build_lan_url(lan_ip, host_port),
        "healthUrl": build_health_url(host_port),
    }


__all__ = [
    "PortAllocator",
    "is_port_in_use",
    "is_port_listening",
    "detect_lan_ip",
    "resolve_lan_ip",
    "build_lan_url",
    "build_health_url",
    "build_network_entry",
]
