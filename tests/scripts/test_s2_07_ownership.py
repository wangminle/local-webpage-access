"""S2-07 显式三态判定回归（BUG-748 后半 / #56-1/2）。

覆盖：
* ``_gateway_holds_static_port`` / ``_static_port_is_own_live_site`` 的
  OURS / FOREIGN / UNKNOWN 各分支；
* 静态端口 UNKNOWN 时维持复用放行并写 ``port_ownership_unknown`` 事件；
* ``_port_2019_foreign`` 三态与 ``_check_caddy`` 的 UNKNOWN 文案
  （「无法确认 :<port> 归属」+ 探测异常列表，不再说「被外部占用」）。
"""

from __future__ import annotations

from local_webpage_access.hosting import (
    _ensure_static_port,
    _gateway_holds_static_port,
)
from local_webpage_access.ownership import Ownership
from local_webpage_access.static_gateway import StaticGateway


def _seed_caddy_static_site(workspace, config, registry, monkeypatch, port: int = 21000):
    """登记 enabled 静态站点 + 磁盘站点片段（绑 port），并把后端伪装成 caddy。"""
    from tests.scripts._helpers import make_static_manifest

    manifest = make_static_manifest("demo")
    manifest.save(workspace.app_manifest_path("demo"))
    registry.upsert_from_manifest(manifest)
    registry.upsert_static_site(
        "demo",
        {"root": "public", "gateway": "caddy", "hostPort": port, "enabled": True},
    )
    assert registry.allocate_port("demo", port)
    site = StaticGateway(workspace, config).site_config_path("demo")
    site.parent.mkdir(parents=True, exist_ok=True)
    site.write_text(f":{port} {{\n\troot * /tmp\n}}\n", encoding="utf-8")
    monkeypatch.setattr(StaticGateway, "detect_backend", lambda self: "caddy")
    return site


# ---- _gateway_holds_static_port：caddy 后端 -----------------------------------


def test_gateway_holds_static_port_caddy_foreign_when_admin_down(
    workspace, config, monkeypatch
) -> None:
    """本网关 admin 不在线 = 确证不持有（FOREIGN），维持 BUG-737 重新分配语义。"""
    monkeypatch.setattr(StaticGateway, "detect_backend", lambda self: "caddy")
    monkeypatch.setattr(StaticGateway, "_admin_alive", lambda self, **_k: False)

    assert (
        _gateway_holds_static_port(workspace, config, "demo", 21000)
        is Ownership.FOREIGN
    )


def test_gateway_holds_static_port_caddy_unknown_when_lsof_missing(
    workspace, config, monkeypatch
) -> None:
    """lsof 缺失（监听者身份不可见）→ UNKNOWN：旧代码此处默默放行，三态显式可见。"""
    monkeypatch.setattr(StaticGateway, "detect_backend", lambda self: "caddy")
    monkeypatch.setattr(StaticGateway, "_admin_alive", lambda self, **_k: True)
    site = StaticGateway(workspace, config).site_config_path("demo")
    site.parent.mkdir(parents=True, exist_ok=True)
    site.write_text(":21000 {\n\troot * /tmp\n}\n", encoding="utf-8")
    monkeypatch.setattr(
        "local_webpage_access.hosting._listen_process_names", lambda _p: None
    )

    assert (
        _gateway_holds_static_port(workspace, config, "demo", 21000)
        is Ownership.UNKNOWN
    )


def test_gateway_holds_static_port_caddy_ours_and_foreign_listener(
    workspace, config, monkeypatch
) -> None:
    """lsof 看清监听者：caddy → OURS；其他进程 → FOREIGN。"""
    monkeypatch.setattr(StaticGateway, "detect_backend", lambda self: "caddy")
    monkeypatch.setattr(StaticGateway, "_admin_alive", lambda self, **_k: True)
    site = StaticGateway(workspace, config).site_config_path("demo")
    site.parent.mkdir(parents=True, exist_ok=True)
    site.write_text(":21000 {\n\troot * /tmp\n}\n", encoding="utf-8")

    monkeypatch.setattr(
        "local_webpage_access.hosting._listen_process_names", lambda _p: ["caddy"]
    )
    assert (
        _gateway_holds_static_port(workspace, config, "demo", 21000) is Ownership.OURS
    )

    monkeypatch.setattr(
        "local_webpage_access.hosting._listen_process_names", lambda _p: ["python3"]
    )
    assert (
        _gateway_holds_static_port(workspace, config, "demo", 21000)
        is Ownership.FOREIGN
    )


# ---- _gateway_holds_static_port：builtin 后端 ---------------------------------


def test_gateway_holds_static_port_builtin_branches(workspace, config, monkeypatch) -> None:
    """builtin：pid 死/命令行不符 → FOREIGN；lsof 缺失 → UNKNOWN；python 监听 → OURS。"""
    import local_webpage_access.daemon as dm

    monkeypatch.setattr(StaticGateway, "detect_backend", lambda self: "builtin")
    monkeypatch.setattr(StaticGateway, "_read_pid", lambda self, _i: 4242)
    monkeypatch.setattr(StaticGateway, "_pid_alive", staticmethod(lambda pid: True))
    monkeypatch.setattr(
        dm, "pid_cmdline_contains", lambda pid, *needles: "http.server" in needles
    )
    (workspace.app_public("demo")).mkdir(parents=True, exist_ok=True)

    # lsof 缺失：UNKNOWN（身份不可见，维持放行但可见）
    monkeypatch.setattr(
        "local_webpage_access.hosting._listen_process_names", lambda _p: None
    )
    assert (
        _gateway_holds_static_port(workspace, config, "demo", 21000)
        is Ownership.UNKNOWN
    )

    # 监听者为 python → OURS
    monkeypatch.setattr(
        "local_webpage_access.hosting._listen_process_names", lambda _p: ["python3.13"]
    )
    assert (
        _gateway_holds_static_port(workspace, config, "demo", 21000) is Ownership.OURS
    )

    # pid 已死 → FOREIGN
    monkeypatch.setattr(StaticGateway, "_pid_alive", staticmethod(lambda pid: False))
    assert (
        _gateway_holds_static_port(workspace, config, "demo", 21000)
        is Ownership.FOREIGN
    )


# ---- UNKNOWN 可见性：事件 + 复用放行 ------------------------------------------


def test_ensure_static_port_unknown_reuses_and_writes_event(
    workspace, config, registry, monkeypatch
) -> None:
    """S2-07：端口归属 UNKNOWN（缺少 lsof）时维持复用放行，并写事件保持可见。"""
    _seed_caddy_static_site(workspace, config, registry, monkeypatch)
    monkeypatch.setattr(StaticGateway, "_admin_alive", lambda self, **_k: True)
    monkeypatch.setattr(
        "local_webpage_access.hosting.is_port_listening", lambda _p: True
    )
    monkeypatch.setattr(
        "local_webpage_access.hosting._listen_process_names", lambda _p: None
    )

    port, fresh = _ensure_static_port(workspace, config, registry, "demo")

    assert fresh is False  # UNKNOWN 维持放行（复用），与旧「names None → True」一致
    assert port == 21000
    events = registry.list_events("demo")
    assert any(e["event_type"] == "port_ownership_unknown" for e in events)


def test_ensure_static_port_foreign_listener_reallocates(
    workspace, config, registry, monkeypatch
) -> None:
    """FOREIGN（Caddy 已停、外部进程占端口）→ 不复用、重新分配（BUG-737 语义不变）。"""
    _seed_caddy_static_site(workspace, config, registry, monkeypatch)
    monkeypatch.setattr(StaticGateway, "_admin_alive", lambda self, **_k: False)
    monkeypatch.setattr("local_webpage_access.hosting.is_port_listening", lambda _p: True)
    monkeypatch.setattr(
        "local_webpage_access.ports.is_port_in_use", lambda _p, **_k: False
    )

    port, fresh = _ensure_static_port(workspace, config, registry, "demo")

    assert fresh is True
    assert registry.port_owner(port) == "demo"
    events = registry.list_events("demo")
    assert not any(e["event_type"] == "port_ownership_unknown" for e in events)


# ---- _port_2019_foreign / _check_caddy 三态与文案 -----------------------------


def _patch_socket_connect(monkeypatch, exc: Exception | None):
    """把 socket.socket 替换为 connect 恒抛 exc（None 表示可连）的假货。"""

    class _FakeSock:
        def settimeout(self, t):
            pass

        def connect(self, addr):
            if exc is not None:
                raise exc

        def close(self):
            pass

    monkeypatch.setattr("socket.socket", lambda *a, **k: _FakeSock())


def _caddy_check_env(tmp_path, monkeypatch):
    from local_webpage_access import autostart as asm

    root = tmp_path / "ws"
    root.mkdir()
    from local_webpage_access.paths import Workspace

    ws = Workspace(root)
    ws.ensure_workspace_dirs()
    monkeypatch.setattr(asm, "detect_platform", lambda: "macos")
    monkeypatch.setattr(
        asm.shutil,
        "which",
        lambda c: "/usr/local/bin/caddy" if c == "caddy" else None,
    )
    from local_webpage_access.config import Config

    return asm, ws, Config()


def test_port_2019_unknown_blocks_with_probe_details(tmp_path, monkeypatch) -> None:
    """admin 端口 UNKNOWN：仍阻断，文案「无法确认归属」+ 列出探测异常，不说「占用」。"""
    import local_webpage_access.daemon as dm

    asm, ws, config = _caddy_check_env(tmp_path, monkeypatch)
    # 无任何本工作区证据；连通性探测抛非拒绝类异常 → UNKNOWN
    monkeypatch.setattr(dm, "is_pid_alive", lambda pid: False)
    monkeypatch.setattr(
        "local_webpage_access.static_gateway.StaticGateway.inspect_caddy_owner",
        lambda self: {"admin_alive": False},
    )
    _patch_socket_connect(monkeypatch, OSError("network unreachable"))

    ownership, errors = asm._port_2019_probe(ws, config)
    assert ownership is Ownership.UNKNOWN
    assert errors  # 各探测异常必须收集上屏

    item = asm._check_caddy(ws, config)
    assert item.status == "fail"
    assert "无法确认" in item.message
    assert "2019" in item.message
    assert "占用" not in item.message
    assert errors[0] in item.message


def test_port_2019_refused_is_ours(tmp_path, monkeypatch) -> None:
    """连接明确被拒 = 端口空闲 → OURS（不属于外部），不阻断。"""
    asm, ws, config = _caddy_check_env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "local_webpage_access.static_gateway.StaticGateway.inspect_caddy_owner",
        lambda self: {"admin_alive": False},
    )

    _patch_socket_connect(monkeypatch, ConnectionRefusedError())

    assert asm._port_2019_foreign(ws, config) is Ownership.OURS


def test_check_caddy_unknown_uses_assigned_admin_port(tmp_path, monkeypatch) -> None:
    """BUG-783：已分配独立 admin 端口的工作区，UNKNOWN 文案按分配端口表述。"""
    import local_webpage_access.daemon as dm
    from local_webpage_access.admin_port import ensure_admin_port

    asm, ws, config = _caddy_check_env(tmp_path, monkeypatch)
    assigned = ensure_admin_port(ws)
    assert assigned != 2019
    monkeypatch.setattr(dm, "is_pid_alive", lambda pid: False)
    monkeypatch.setattr(
        "local_webpage_access.static_gateway.StaticGateway.inspect_caddy_owner",
        lambda self: {"admin_alive": False},
    )
    _patch_socket_connect(monkeypatch, OSError("timeout"))

    item = asm._check_caddy(ws, config)
    assert item.status == "fail"
    assert f"无法确认 :{assigned} 归属" in item.message


def test_check_caddy_foreign_message_keeps_occupied_wording(tmp_path, monkeypatch) -> None:
    """FOREIGN 保留原有「已被外部 Caddy/进程占用」文案（确证占用时才说占用）。"""
    asm, ws, config = _caddy_check_env(tmp_path, monkeypatch)
    _patch_socket_connect(monkeypatch, None)  # 端口可连、无本工作区证据 → FOREIGN

    item = asm._check_caddy(ws, config)
    assert item.status == "fail"
    assert "占用" in item.message
    assert "2019" in item.message
