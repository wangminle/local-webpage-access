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
