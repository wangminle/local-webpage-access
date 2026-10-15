"""BUG-783 回归：Caddy admin 按工作区独立分配端口（POST /load 归属保护）。

证据：design/5-checkpoints/2026-10-13/S1-exit-acceptance/README.md 新发现 1——
多工作区共享 Caddy admin :2019，孤儿 daemon 向共享 admin 推空配置，
清空真实工作区网关运行配置；BUG-176 只保护了 POST /stop。

方案（design/2-achievement/2026-10-15-S2-实施与交付记录.md#s2-07-显式三态判定与-caddy-admin-归属保护）：
每工作区运行时分配独立 admin 端口（20201–20500，登记 run/admin-port），
主 Caddyfile 渲染 ``admin 127.0.0.1:<port>`` 指令；升级后首次 start 把仍在线的
本工作区 legacy :2019 旧 master 优雅停掉（平滑迁移）。旧代码孤儿再向 :2019 推送
只会打到它自己的 master 或无人监听，结构上阻断跨工作区串刷。
"""

from __future__ import annotations

from local_webpage_access.admin_port import (
    LEGACY_ADMIN_PORT,
    _candidate_ports,
    admin_address,
    admin_base,
    ensure_admin_port,
    read_admin_port,
)
from local_webpage_access.static_gateway import StaticGateway


# ---- 分配与登记 --------------------------------------------------------------


def test_admin_port_assigned_deterministically_and_persisted(workspace) -> None:
    """分配确定性（同一工作区稳定）且登记落盘，跨调用/重启不变。"""
    first = ensure_admin_port(workspace)
    assert first == ensure_admin_port(workspace)
    assert read_admin_port(workspace) == first
    assert 20201 <= first <= 20500
    # 登记文件物化
    assert (workspace.run / "admin-port").read_text(encoding="utf-8").strip() == str(first)


def test_admin_port_unassigned_falls_back_to_legacy(workspace) -> None:
    """未分配的工作区（含全部存量行为）回退 legacy :2019。"""
    assert read_admin_port(workspace) is None
    assert admin_base(workspace) == f"http://127.0.0.1:{LEGACY_ADMIN_PORT}"
    assert admin_address(workspace) == f"127.0.0.1:{LEGACY_ADMIN_PORT}"


def test_admin_port_skips_listening_candidate(workspace, monkeypatch) -> None:
    """候选端口已被占用（其他工作区/外部进程）时顺序顺延。"""
    first_candidate = _candidate_ports(workspace)[0]
    monkeypatch.setattr(
        "local_webpage_access.admin_port.is_port_listening",
        lambda port: port == first_candidate,
    )

    assigned = ensure_admin_port(workspace)

    assert assigned != first_candidate
    assert 20201 <= assigned <= 20500


def test_admin_port_two_workspaces_use_distinct_ports(workspace, tmp_path, monkeypatch) -> None:
    """不同工作区绝不共用 admin 端口：先分配者监听其端口时，后分配者顺延。"""
    from local_webpage_access.paths import Workspace

    other_root = tmp_path / "ws-other"
    other = Workspace(other_root)
    other.ensure_workspace_dirs()

    first = ensure_admin_port(workspace)
    # 模拟先分配者的 master 已在监听其端口（运行时常态）
    monkeypatch.setattr(
        "local_webpage_access.admin_port.is_port_listening",
        lambda port: port == first,
    )
    second = ensure_admin_port(other)

    assert second != first


# ---- 主 Caddyfile / bootstrap 渲染 admin 指令 ---------------------------------


def test_bootstrap_config_text_renders_admin_directive() -> None:
    """bootstrap 最小配置带 admin 指令，保证冷启动 master 绑对端口。"""
    from local_webpage_access.static_gateway import _bootstrap_config_text

    assert "admin 127.0.0.1:20333" in _bootstrap_config_text(20333)
    # 未分配（legacy）时不引入任何 admin 指令，行为与旧版一致
    assert "admin 127.0.0.1" not in _bootstrap_config_text(None)


def test_admin_urls_follow_assigned_port(workspace, config) -> None:
    """BUG-783：reload/stop/探活的 admin URL 全部指向分配端口（结构上孤儿推 :2019 够不到）。"""
    gateway = StaticGateway(workspace, config)
    assigned = ensure_admin_port(workspace)

    assert assigned != LEGACY_ADMIN_PORT
    assert gateway._admin_base_url() == f"http://127.0.0.1:{assigned}"
    assert gateway._admin_config_url() == f"http://127.0.0.1:{assigned}/config/"
    assert gateway._admin_stop_url() == f"http://127.0.0.1:{assigned}/stop"


def test_reload_once_targets_assigned_admin_port(workspace, config, monkeypatch) -> None:
    """``caddy reload --address`` 指向分配端口——POST /load 只可能落到本工作区 master。"""
    import subprocess

    gateway = StaticGateway(workspace, config)
    assigned = ensure_admin_port(workspace)
    captured: dict[str, list[str]] = {}

    monkeypatch.setattr(
        "local_webpage_access.static_gateway._refuse_caddy_admin_in_pytest",
        lambda action: None,
    )

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    ok, _stderr = gateway._reload_once()

    assert ok is True
    assert "--address" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--address") + 1] == (
        f"127.0.0.1:{assigned}"
    )


# ---- 平滑迁移：legacy :2019 旧 master 处理 -----------------------------------


def test_migration_stops_legacy_master_when_ours(workspace, config, monkeypatch) -> None:
    """升级后首次 start：legacy :2019 仍是本工作区旧 master → 先 POST /stop 再拉起。"""
    gateway = StaticGateway(workspace, config)
    ensure_admin_port(workspace)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(gateway, "_legacy_admin_alive", lambda **_k: True)
    monkeypatch.setattr(gateway, "_legacy_master_is_ours", lambda: True)
    monkeypatch.setattr(
        gateway,
        "_post_admin_stop",
        lambda stop_url, config_url: calls.append((stop_url, config_url)) or True,
    )

    gateway._stop_legacy_master_for_migration()

    assert calls == [
        (
            f"http://127.0.0.1:{LEGACY_ADMIN_PORT}/stop",
            f"http://127.0.0.1:{LEGACY_ADMIN_PORT}/config/",
        )
    ]


def test_migration_never_stops_foreign_legacy_master(workspace, config, monkeypatch) -> None:
    """legacy :2019 归其他工作区/外部 → 不停（BUG-176 语义），只告警。"""
    gateway = StaticGateway(workspace, config)
    ensure_admin_port(workspace)
    calls: list[str] = []
    monkeypatch.setattr(gateway, "_legacy_admin_alive", lambda **_k: True)
    monkeypatch.setattr(gateway, "_legacy_master_is_ours", lambda: False)
    monkeypatch.setattr(
        gateway,
        "_post_admin_stop",
        lambda stop_url, config_url: calls.append(stop_url) or True,
    )

    gateway._stop_legacy_master_for_migration()

    assert calls == []  # fail-closed：不替用户停外部 Caddy


def test_caddy_stop_also_stops_legacy_master(workspace, config, monkeypatch) -> None:
    """``lwa gateway off``：分配端口 + legacy 端口都尝试停（升级窗口内的旧 master 不残留）。"""
    gateway = StaticGateway(workspace, config)
    ensure_admin_port(workspace)
    calls: list[str] = []
    monkeypatch.setattr(StaticGateway, "_admin_alive", lambda self, **_k: False)
    monkeypatch.setattr(gateway, "_legacy_admin_alive", lambda **_k: True)
    monkeypatch.setattr(gateway, "_legacy_master_is_ours", lambda: True)
    monkeypatch.setattr(
        gateway,
        "_post_admin_stop",
        lambda stop_url, config_url: calls.append(stop_url) or True,
    )

    stopped = gateway.caddy_stop()

    assert stopped is True
    # admin（分配端口）不在线 → 无需 POST /stop；legacy 旧 master 被迁移收尾停掉
    assert calls == [f"http://127.0.0.1:{LEGACY_ADMIN_PORT}/stop"]


# ---- 旧代码孤儿的结构性阻断 ---------------------------------------------------


def test_orphan_pushing_legacy_port_cannot_reach_workspace_admin(
    workspace, config
) -> None:
    """升级后本工作区 admin 与 :2019 解绑：旧代码孤儿推 :2019 结构上够不到本网关。

    对应事故（S1 退出验收新发现 1）：孤儿 daemon POST /load 到共享 :2019 清空真实
    工作区配置。分配独立端口后，真实工作区的 master 不再监听 :2019。
    """
    gateway = StaticGateway(workspace, config)
    ensure_admin_port(workspace)

    assert str(LEGACY_ADMIN_PORT) not in gateway._admin_config_url()
    assert gateway._admin_config_url().startswith("http://127.0.0.1:20")
