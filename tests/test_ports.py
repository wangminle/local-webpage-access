"""ports 模块测试（WBS-06）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_web_access.config import Config, PortPool
from local_web_access.errors import PortError
from local_web_access.ports import (
    PortAllocator,
    build_health_url,
    build_lan_url,
    build_network_entry,
    detect_lan_ip,
    is_port_in_use,
    resolve_lan_ip,
)
from local_web_access.registry import Registry
from tests._helpers import make_static_manifest


@pytest.fixture()
def registry(workspace_root: Path) -> Registry:
    workspace_root.mkdir(parents=True, exist_ok=True)
    reg = Registry(workspace_root / "registry" / "local-web.db")
    reg.open()
    yield reg
    reg.close()


@pytest.fixture()
def allocator(registry: Registry) -> PortAllocator:
    cfg = Config(portPool=PortPool(start=20000, end=20020))
    return PortAllocator(cfg, registry)


# ---- is_port_in_use --------------------------------------------------------


def test_is_port_in_use_false_for_free_port() -> None:
    # 找一个肯定空闲的端口
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    free_port = s.getsockname()[1]
    s.close()
    assert is_port_in_use(free_port, host="127.0.0.1") is False


def test_is_port_in_use_true_for_listening_port() -> None:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert is_port_in_use(port, host="127.0.0.1") is True
    finally:
        s.close()


def test_is_port_in_use_detects_wildcard_listener() -> None:
    """BUG-002：0.0.0.0 监听的端口必须被识别为占用。

    端口分配器默认探测 host=0.0.0.0；修复前 Windows 下 SO_REUSEADDR 允许
    重复 bind，把已监听端口判为空闲（用户复现 is_port_in_use_while_listening=False）。
    """
    import socket

    s = socket.socket()
    s.bind(("0.0.0.0", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert is_port_in_use(port) is True  # 默认 host=0.0.0.0
    finally:
        s.close()


# ---- LAN IP ----------------------------------------------------------------


def test_detect_lan_ip_returns_str_or_none() -> None:
    ip = detect_lan_ip()
    # CI/离线时可能为 None，在线时应是 IPv4
    if ip is not None:
        assert ip.count(".") == 3


def test_resolve_lan_ip_manual() -> None:
    cfg = Config(lanIpStrategy="manual", manualLanIp="192.168.1.20")
    assert resolve_lan_ip(cfg) == "192.168.1.20"


# ---- URL 生成 --------------------------------------------------------------


def test_build_lan_url() -> None:
    assert build_lan_url("192.168.1.20", 18023) == "http://192.168.1.20:18023"


def test_build_lan_url_none_ip() -> None:
    assert build_lan_url(None, 18023) is None


def test_build_health_url() -> None:
    assert build_health_url(18023) == "http://127.0.0.1:18023"


def test_build_network_entry() -> None:
    cfg = Config(lanIpStrategy="manual", manualLanIp="192.168.1.20")
    net = build_network_entry(cfg, host_port=18024, internal_port=8000)
    assert net["host"] == "0.0.0.0"
    assert net["hostPort"] == 18024
    assert net["internalPort"] == 8000
    assert net["routeMode"] == "port"
    assert net["routeHost"] is None
    assert net["lanUrl"] == "http://192.168.1.20:18024"
    assert net["healthUrl"] == "http://127.0.0.1:18024"


def test_build_network_entry_uses_explicit_lan_ip() -> None:
    cfg = Config(lanIpStrategy="auto")
    # 显式传入 lan_ip 时优先使用
    net = build_network_entry(cfg, host_port=18024, lan_ip="10.0.0.5")
    assert net["lanUrl"] == "http://10.0.0.5:18024"


# ---- PortAllocator ---------------------------------------------------------


def test_allocate_returns_pool_port(allocator: PortAllocator, registry: Registry) -> None:
    registry.upsert_from_manifest(make_static_manifest("a"))
    port = allocator.allocate("a", probe_host=False)
    assert 20000 <= port <= 20020


def test_allocate_skips_allocated(allocator: PortAllocator, registry: Registry) -> None:
    registry.upsert_from_manifest(make_static_manifest("a"))
    registry.upsert_from_manifest(make_static_manifest("b"))
    p1 = allocator.allocate("a", probe_host=False)
    p2 = allocator.allocate("b", probe_host=False)
    assert p1 != p2
    assert {p1, p2}.issubset(set(range(20000, 20021)))


def test_allocate_respects_exclude(allocator: PortAllocator, registry: Registry) -> None:
    registry.upsert_from_manifest(make_static_manifest("a"))
    port = allocator.allocate("a", exclude=set(range(20000, 20015)), probe_host=False)
    assert port >= 20015


def test_allocate_registers_in_registry(allocator: PortAllocator, registry: Registry) -> None:
    registry.upsert_from_manifest(make_static_manifest("a"))
    port = allocator.allocate("a", probe_host=False)
    assert port in registry.allocated_ports()
    assert registry.port_owner(port) == "a"


def test_release_port(allocator: PortAllocator, registry: Registry) -> None:
    registry.upsert_from_manifest(make_static_manifest("a"))
    port = allocator.allocate("a", probe_host=False)
    allocator.release(port)
    assert port not in registry.allocated_ports()


def test_release_instance(allocator: PortAllocator, registry: Registry) -> None:
    registry.upsert_from_manifest(make_static_manifest("a"))
    allocator.allocate("a", probe_host=False)
    allocator.allocate("a", probe_host=False)  # 同实例多次
    allocator.release_instance("a")
    assert registry.allocated_ports() == []


def test_allocate_exhausted_raises(allocator: PortAllocator, registry: Registry) -> None:
    registry.upsert_from_manifest(make_static_manifest("a"))
    # 排除整个池子
    with pytest.raises(PortError):
        allocator.allocate("a", exclude=set(range(20000, 20021)), probe_host=False)


def test_allocate_skips_host_listening_port(registry: Registry) -> None:
    """真实监听的端口应被跳过。"""
    import socket

    s = socket.socket()
    s.bind(("0.0.0.0", 0))
    s.listen(1)
    busy_port = s.getsockname()[1]
    try:
        from local_web_access.config import PortPool

        # 池必须 ≥10 个端口（PortPool 校验），把 busy_port 放在池起点
        cfg = Config(portPool=PortPool(start=busy_port, end=busy_port + 10))
        registry.upsert_from_manifest(make_static_manifest("x"))
        alloc = PortAllocator(cfg, registry)
        port = alloc.allocate("x", probe_host=True)
        assert port != busy_port
        assert busy_port <= port <= busy_port + 10
    finally:
        s.close()
