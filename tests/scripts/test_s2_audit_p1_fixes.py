"""S2 完成度审计 P1 缺口的回归（网关侧）。

证据：design/5-checkpoints/2026-10-14/S2-completion-audit/README.md 缺陷 5/6
（复现脚本 repro_admin_isolation.py）。S2-03 侧的 P1-1（身份写盘失败补偿）回归
在 ``test_s2_03_rebuild_txn.py``（复用其事务夹具）。

- P1-2（S2-07 / BUG-783）：legacy :2019 归属判定不能只凭本工作区 pidfile 存活
  + 命令行含工作区路径——该 PID 还必须是 :2019 的实际监听者，否则迁移/清理
  会向别的工作区持有的 :2019 发 POST /stop；
- P1-3（BUG-783）：删除片段/失败清理路径 ``_sync_main_config`` 同受独立端口
  分配与归属保护，不得再向共享 :2019 推送重组配置。
"""

from __future__ import annotations

import subprocess

from local_webpage_access.admin_port import (
    LEGACY_ADMIN_PORT,
    ensure_admin_port,
    read_admin_port,
)
from local_webpage_access.config import Config
from local_webpage_access.static_gateway import StaticGateway

# ---- P1-2（S2-07 / BUG-783）：legacy :2019 归属必须核对监听者 ------------------


def _legacy_holder_scenario(gateway: StaticGateway, monkeypatch, *, holder_pid: int) -> list[str]:
    """构造审计 F2 现场：pidfile 指向存活 caddy（12345），:2019 实际由 holder 持有。"""
    stops: list[str] = []
    gateway.caddy_pid_path().write_text("12345\n", encoding="utf-8")
    monkeypatch.setattr(gateway, "_legacy_admin_alive", lambda **_k: True)
    monkeypatch.setattr(gateway, "_pid_alive", lambda _pid: True)
    # pidfile PID 命令行含本工作区路径——旧代码据此误判 :2019 归属
    monkeypatch.setattr(
        "local_webpage_access.static_gateway.pid_cmdline_contains", lambda *a, **k: True
    )
    monkeypatch.setattr(gateway, "_admin_holder_pid", lambda port=None: holder_pid)
    monkeypatch.setattr(
        gateway, "_pid_cmdline", lambda _pid: "caddy run --config /other-workspace/Caddyfile"
    )
    monkeypatch.setattr(
        gateway, "_post_admin_stop", lambda stop_url, _config_url: stops.append(stop_url) or True
    )
    return stops


def test_migration_does_not_stop_foreign_2019_when_pidfile_pid_alive(
    workspace, config, monkeypatch
) -> None:
    """本工作区 master 存活（已迁独立端口/优雅退出中）但 :2019 归别人 → 不得 POST /stop。"""
    gateway = StaticGateway(workspace, config)
    ensure_admin_port(workspace)
    stops = _legacy_holder_scenario(gateway, monkeypatch, holder_pid=54321)

    gateway._stop_legacy_master_for_migration()

    assert stops == []


def test_migration_stops_legacy_when_pidfile_pid_is_the_holder(
    workspace, config, monkeypatch
) -> None:
    """正向：pidfile PID 就是 :2019 监听者（升级前的本工作区旧 master）→ 允许迁移停机。"""
    gateway = StaticGateway(workspace, config)
    ensure_admin_port(workspace)
    stops = _legacy_holder_scenario(gateway, monkeypatch, holder_pid=12345)

    gateway._stop_legacy_master_for_migration()

    assert stops == [f"http://127.0.0.1:{LEGACY_ADMIN_PORT}/stop"]


def test_caddy_stop_does_not_stop_foreign_2019_when_pidfile_pid_alive(
    workspace, config, monkeypatch
) -> None:
    """``gateway off`` 的迁移收尾同样不得误停别人持有的 :2019。"""
    gateway = StaticGateway(workspace, config)
    ensure_admin_port(workspace)
    stops = _legacy_holder_scenario(gateway, monkeypatch, holder_pid=54321)
    monkeypatch.setattr(StaticGateway, "_admin_alive", lambda self, **_k: False)

    stopped = gateway.caddy_stop()

    assert stopped is True
    assert stops == []


# ---- P1-3（BUG-783）：_sync_main_config 纳入分配与归属保护 ---------------------


def _caddy_gateway(workspace) -> StaticGateway:
    return StaticGateway(workspace, Config(staticGateway="caddy"))


def test_sync_main_config_targets_assigned_port_not_shared_2019(
    workspace, monkeypatch
) -> None:
    """未登记端口的工作区走清理路径：先分配独立端口，reload 目标不再是共享 :2019。"""
    gateway = _caddy_gateway(workspace)
    monkeypatch.setattr(gateway, "detect_backend", lambda: "caddy")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "local_webpage_access.static_gateway._refuse_caddy_admin_in_pytest",
        lambda _action: None,
    )
    monkeypatch.setattr(gateway, "_admin_alive", lambda **_k: False)

    def _fake_run(cmd, **_kwargs):
        commands.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr("local_webpage_access.static_gateway.subprocess.run", _fake_run)

    gateway._sync_main_config()

    assigned = read_admin_port(workspace)
    assert assigned is not None and assigned != LEGACY_ADMIN_PORT
    addresses = [cmd[cmd.index("--address") + 1] for cmd in commands if "--address" in cmd]
    assert addresses == [f"127.0.0.1:{assigned}"]


def test_sync_main_config_skips_reload_when_owner_mismatch(workspace, monkeypatch) -> None:
    """admin 在线但归属不匹配 → 只写盘不 reload（fail-closed，磁盘配置待下次加载）。"""
    gateway = _caddy_gateway(workspace)
    monkeypatch.setattr(gateway, "detect_backend", lambda: "caddy")
    reloads: list[int] = []
    monkeypatch.setattr(gateway, "_admin_alive", lambda **_k: True)
    monkeypatch.setattr(
        gateway,
        "inspect_caddy_owner",
        lambda: {
            "owner": "system_caddy",
            "process_user": "root",
            "pid": 4242,
            "workspace_match": False,
            "admin_alive": True,
            "runtime": "owner_mismatch",
        },
    )
    monkeypatch.setattr(
        gateway, "_reload_with_self_heal", lambda: reloads.append(1) or (True, "")
    )

    gateway._sync_main_config()

    assert read_admin_port(workspace) is not None  # 端口分配照常物化
    assert reloads == []
    main = gateway.main_config_path()
    assert main.is_file()
    assert "import" not in main.read_text(encoding="utf-8")  # BUG-069 语义保留


def test_sync_main_config_reloads_when_owner_matches(workspace, monkeypatch) -> None:
    """归属可证时清理路径照常 reload（保护不得把正常路径一并关死）。"""
    gateway = _caddy_gateway(workspace)
    monkeypatch.setattr(gateway, "detect_backend", lambda: "caddy")
    reloads: list[int] = []
    monkeypatch.setattr(gateway, "_admin_alive", lambda **_k: True)
    monkeypatch.setattr(
        gateway,
        "inspect_caddy_owner",
        lambda: {
            "owner": "lwa_service_user",
            "process_user": "me",
            "pid": 4242,
            "workspace_match": True,
            "admin_alive": True,
            "runtime": "ready",
        },
    )
    monkeypatch.setattr(
        gateway, "_reload_with_self_heal", lambda: reloads.append(1) or (True, "")
    )

    gateway._sync_main_config()

    assert reloads == [1]
