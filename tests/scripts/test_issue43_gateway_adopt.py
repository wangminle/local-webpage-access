"""issue #43（BUG-729）：pidfile 丢失后的孤儿 master 认领回归。

CHK-280：重复 master 退出时 Caddy 删除共享 pidfile → 本工作区健康 master
被 inspect_caddy_owner 误判 system_caddy → gateway off/on 双向卡死成熔断
终局。修复：adopt_orphan_master 四重安全闸（admin 在线 / pidfile 缺失或死 /
holder 命令行含本工作区 Caddyfile 或 pidfile 路径 / 进程用户匹配）全过才
重建 pidfile；start_gateway 与 caddy_stop 均接入。
"""

from __future__ import annotations

import getpass

from local_webpage_access.config import Config
from local_webpage_access.paths import Workspace
from local_webpage_access.static_gateway import StaticGateway


def _gateway(workspace: Workspace) -> StaticGateway:
    return StaticGateway(workspace, Config(staticGateway="caddy"))


def _patch_orphan(
    monkeypatch,
    gateway: StaticGateway,
    *,
    pid: int = 4242,
    cmdline: str | None = None,
    user: str | None = None,
) -> None:
    """把 gateway 打成「admin 在线 + pidfile 丢失 + holder 可见」的孤儿现场。"""
    monkeypatch.setattr(gateway, "_admin_alive", lambda timeout=1.0: True)
    monkeypatch.setattr(gateway, "_workspace_caddy_pid_alive", lambda: False)
    monkeypatch.setattr(gateway, "_admin_holder_pid", lambda: pid)
    monkeypatch.setattr(gateway, "_pid_alive", lambda p: p == pid)
    monkeypatch.setattr(
        gateway, "_process_user_for_pid", lambda p: user if user else getpass.getuser()
    )
    main_conf = str(gateway.main_config_path())
    monkeypatch.setattr(
        StaticGateway,
        "_pid_cmdline",
        staticmethod(lambda p: cmdline if cmdline is not None else f"caddy run --config {main_conf} --adapter caddyfile"),
    )


def test_adopt_rebuilds_pidfile_for_owned_master(workspace: Workspace, monkeypatch) -> None:
    """认领成功：命令行含本工作区主 Caddyfile → 重建 pidfile 并返回 pid。"""
    gateway = _gateway(workspace)
    _patch_orphan(monkeypatch, gateway)

    pidfile = gateway.caddy_pid_path()
    assert not pidfile.is_file(), "前置：pidfile 丢失"

    adopted = gateway.adopt_orphan_master()
    assert adopted == 4242
    assert pidfile.read_text(encoding="utf-8") == "4242\n"


def test_adopt_accepts_bootstrap_and_pidfile_paths(workspace: Workspace, monkeypatch) -> None:
    """引导配置路径或 pidfile 路径命中命令行同样可认领（bootstrap 启动的 master）。"""
    gateway = _gateway(workspace)
    _patch_orphan(
        monkeypatch,
        gateway,
        cmdline=f"caddy run --config {gateway._bootstrap_config_path()} --adapter caddyfile",
    )
    assert gateway.adopt_orphan_master() == 4242

    gateway.caddy_pid_path().unlink()
    _patch_orphan(
        monkeypatch,
        gateway,
        cmdline=f"caddy run --pidfile {gateway.caddy_pid_path()}",
    )
    assert gateway.adopt_orphan_master() == 4242


def test_adopt_rejects_foreign_config(workspace: Workspace, monkeypatch) -> None:
    """安全闸 3：命令行不含本工作区路径（真外来 Caddy）→ 拒绝且不写 pidfile。"""
    gateway = _gateway(workspace)
    _patch_orphan(
        monkeypatch, gateway, cmdline="caddy run --config /etc/caddy/Caddyfile"
    )
    assert gateway.adopt_orphan_master() is None
    assert not gateway.caddy_pid_path().is_file()


def test_adopt_rejects_user_mismatch(workspace: Workspace, monkeypatch) -> None:
    """安全闸 4：进程用户与 serviceUser 不符 → 拒绝。"""
    gateway = _gateway(workspace)
    _patch_orphan(monkeypatch, gateway, user="someone_else")
    assert gateway.adopt_orphan_master() is None


def test_adopt_rejects_when_pidfile_healthy(workspace: Workspace, monkeypatch) -> None:
    """pidfile 健在（无需认领）→ 返回 None 且不覆写。"""
    gateway = _gateway(workspace)
    monkeypatch.setattr(gateway, "_admin_alive", lambda timeout=1.0: True)
    monkeypatch.setattr(gateway, "_workspace_caddy_pid_alive", lambda: True)
    assert gateway.adopt_orphan_master() is None


def test_start_gateway_adopts_instead_of_rejecting(workspace: Workspace, monkeypatch) -> None:
    """端到端：admin 在线 + 孤儿 master 场景下 `lwa gateway on` 走认领恢复。"""
    from local_webpage_access import gateway_service as gs

    gateway = _gateway(workspace)
    _patch_orphan(monkeypatch, gateway)

    rejected: list[str] = []
    monkeypatch.setattr(
        gs,
        "StaticGateway",
        lambda ws, cfg: gateway,
    )
    # is_gateway_running 判定：认领前 False（pidfile 缺失）→ 认领后 True
    monkeypatch.setattr(
        gs,
        "is_gateway_running",
        lambda ws, cfg: gateway.caddy_pid_path().is_file(),
    )
    # 已在线恢复路径的副作用打桩（reload/finalize/capability/TLS 验证）
    monkeypatch.setattr(gateway, "write_main_config", lambda: None)
    monkeypatch.setattr(gateway, "stop_all_builtin", lambda: [])
    monkeypatch.setattr(gateway, "reload_all", lambda: True)
    monkeypatch.setattr(gs, "_post_switch_finalize", lambda *a, **k: None)
    monkeypatch.setattr(gs, "_refresh_gateway_capability", lambda *a: None)
    monkeypatch.setattr(gs, "verify_tls_entries", lambda *a, **k: None)

    pid = gs.start_gateway(workspace, Config(staticGateway="caddy"))
    assert pid == 4242, "应认领孤儿 master 并按已在线路径返回其 pid"
    assert not rejected
    state = gs.read_state(workspace)
    assert state is not None and state.pid == 4242
    assert state.consecutive_start_failures == 0
