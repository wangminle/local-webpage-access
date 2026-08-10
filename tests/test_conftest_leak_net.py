"""BUG-450：泄漏兜底网对 manager_service / pytest 工作区路径的归属判定。"""

from __future__ import annotations

import tests.conftest as cf


def test_pytest_ws_mark_matches_common_temp_layouts() -> None:
    assert cf._PYTEST_WS_MARK.search("/tmp/pytest-of-user/pytest-12/test0/ws")
    assert cf._PYTEST_WS_MARK.search(r"C:\Users\x\AppData\Local\Temp\pytest-of-x\pytest-3\ws")
    assert cf._PYTEST_WS_MARK.search("/tmp/pytest-0/test_init_preserves0/ws")
    assert cf._PYTEST_WS_MARK.search("/var/folders/xx/pytest-of-me/pytest-1/ws") is not None


def test_pytest_ws_mark_rejects_production_workspace() -> None:
    assert cf._PYTEST_WS_MARK.search("/home/user/lwa-runtime") is None
    assert cf._PYTEST_WS_MARK.search("/opt/local-webpage-access") is None
    assert cf._PYTEST_WS_MARK.search("/tmp/my-app-workspace") is None


def test_lwa_service_ws_extracts_workspace_path() -> None:
    cmd = (
        "12345 /usr/bin/python -m local_webpage_access.manager_service "
        "--workspace /tmp/pytest-of-u/pytest-1/test0/ws"
    )
    m = cf._LWA_SERVICE_WS.search(cmd)
    assert m is not None
    assert m.group(1).endswith("/ws")


def test_list_helpers_accept_empty_process_table(monkeypatch) -> None:
    monkeypatch.setattr(cf, "_pgrep_lf", lambda pattern: "")
    assert cf._list_http_server_pids_on_test_ports() == set()
    assert cf._list_lwa_service_pids_on_pytest_workspaces() == set()


def test_list_lwa_services_only_pytest_workspaces(monkeypatch) -> None:
    out = (
        "111\tpython -m local_webpage_access.manager_service "
        "--workspace /home/user/prod-ws\n"
        "222\tpython -m local_webpage_access.manager_service "
        "--workspace /tmp/pytest-of-u/pytest-9/test_x/ws\n"
        "333\tpython -m local_webpage_access.daemon "
        "--workspace /tmp/pytest-of-u/pytest-9/test_y/ws\n"
    )
    monkeypatch.setattr(cf, "_pgrep_lf", lambda pattern: out)
    assert cf._list_lwa_service_pids_on_pytest_workspaces() == {222, 333}


# ---- CHK-178/P2：会话级过滤避免清理并发 pytest 会话 -----------------------


def _session_procs_output():
    """模拟两个并发 pytest 会话各自的 manager/daemon 命令行。"""
    return (
        "111\tpython -m local_webpage_access.manager_service "
        "--workspace /tmp/pytest-of-u/pytest-9/test_x/ws\n"
        "222\tpython -m local_webpage_access.daemon "
        "--workspace /tmp/pytest-of-u/pytest-9/test_y/ws\n"
        "333\tpython -m local_webpage_access.manager_service "
        "--workspace /tmp/pytest-of-u/pytest-10/test_z/ws\n"
    )


def test_only_under_returns_own_session_pids(monkeypatch) -> None:
    """only_under=本会话根时只返回本会话拉起的进程，不碰并发会话。"""
    monkeypatch.setattr(cf, "_pgrep_lf", lambda pattern: _session_procs_output())
    own_root = "/tmp/pytest-of-u/pytest-9"
    assert cf._list_lwa_service_pids_on_pytest_workspaces(
        only_under=own_root
    ) == {111, 222}


def test_only_under_excludes_concurrent_session(monkeypatch) -> None:
    """另一并发会话（pytest-10）的进程绝不被本会话（pytest-9）纳入清理。"""
    monkeypatch.setattr(cf, "_pgrep_lf", lambda pattern: _session_procs_output())
    own_root = "/tmp/pytest-of-u/pytest-9"
    pids = cf._list_lwa_service_pids_on_pytest_workspaces(only_under=own_root)
    assert 333 not in pids


def test_only_under_with_orphans_picks_dead_workspace(monkeypatch, tmp_path) -> None:
    """include_orphans 纳入工作区目录已不存在的真孤儿（跨 session 残留）。

    其工作区此刻必然不存在，绝不会误伤仍活着的并发会话。
    """
    # 另一「活跃会话」：workspace 不在本会话根子树，但目录确实存在
    other_alive_ws = tmp_path / "concurrent-session-ws"
    other_alive_ws.mkdir()
    own_root = str(tmp_path / "my-session")

    out = (
        f"111\tpython -m local_webpage_access.manager_service "
        f"--workspace {other_alive_ws}\n"
        "222\tpython -m local_webpage_access.daemon "
        "--workspace /tmp/pytest-of-u/pytest-8/gone-ws\n"
    )
    monkeypatch.setattr(cf, "_pgrep_lf", lambda pattern: out)
    # 不带 orphan：other_alive_ws 不在子树且存在 → 跳过；gone-ws 不在子树
    # 且默认不查孤儿 → 跳过
    pids_no_orphan = cf._list_lwa_service_pids_on_pytest_workspaces(
        only_under=own_root
    )
    assert pids_no_orphan == set()
    # 带 orphan：other_alive_ws 存在 → 仍跳过；gone-ws 目录不存在 → 纳入
    pids_with_orphan = cf._list_lwa_service_pids_on_pytest_workspaces(
        only_under=own_root, include_orphans=True
    )
    assert pids_with_orphan == {222}


def test_only_under_with_orphans_ignores_missing_production_workspace(
    monkeypatch, tmp_path
) -> None:
    """孤儿清理只接管 pytest 路径，不得终止目录已删除的正式服务。"""
    own_root = str(tmp_path / "my-session")
    out = (
        "111\tpython -m local_webpage_access.manager_service "
        "--workspace /opt/local-webpage-access/deleted-workspace\n"
        "222\tpython -m local_webpage_access.daemon "
        "--workspace /tmp/my-app-deleted-workspace\n"
    )
    monkeypatch.setattr(cf, "_pgrep_lf", lambda pattern: out)

    assert cf._list_lwa_service_pids_on_pytest_workspaces(
        only_under=own_root, include_orphans=True
    ) == set()


def test_only_under_prefix_does_not_cross_directory_boundary(monkeypatch, tmp_path) -> None:
    """前缀匹配不得越界：pytest-9* 不应被 pytest-9 的根吃掉（目录分隔）。"""
    sibling = tmp_path / "session-A-extra"
    sibling.mkdir()
    out = (
        f"111\tpython -m local_webpage_access.manager_service "
        f"--workspace {sibling}\n"
    )
    monkeypatch.setattr(cf, "_pgrep_lf", lambda pattern: out)
    own_root = str(tmp_path / "session-A")
    # own_root 不存在；sibling = tmp_path/session-A-extra，不以 own_root + sep 开头
    assert cf._list_lwa_service_pids_on_pytest_workspaces(only_under=own_root) == set()
