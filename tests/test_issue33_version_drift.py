"""issue #33：运行中服务版本落后于当前代码。"""

from __future__ import annotations

from local_webpage_access.config import default_config
from local_webpage_access.doctor import STATUS_OK, STATUS_WARN, check_service_version_drift
from local_webpage_access.paths import Workspace


def test_version_drift_warns_when_manager_health_lags(workspace: Workspace, monkeypatch) -> None:
    from local_webpage_access import doctor as doctor_mod
    from local_webpage_access.manager_service import ManagerState
    from local_webpage_access.version_info import normalize_version_label

    cfg = default_config()
    monkeypatch.setattr(
        doctor_mod,
        "_service_observed_running",
        lambda name, ws, config: True if name == "manager" else False,
    )
    monkeypatch.setattr(
        "local_webpage_access.manager_service.read_state",
        lambda ws: ManagerState(enabled=True, pid=1, port=17800, bind_version="V0.8.12"),
    )
    monkeypatch.setattr(
        doctor_mod,
        "_read_manager_health_version",
        lambda ws, config: "V0.8.12",
    )
    monkeypatch.setattr(
        "local_webpage_access.version_info.display_version",
        lambda: "V0.8.14",
    )
    result = check_service_version_drift(workspace, cfg)
    assert result.status == STATUS_WARN
    assert "manager" in result.message
    assert "V0.8.12" in (result.detail or "")
    assert "V0.8.14" in result.message
    assert "运行版本" in (result.detail or "")
    assert "当前安装版本" in (result.detail or "")
    assert "lwa services restart" in (result.suggestion or "")
    assert normalize_version_label("V0.8.12") == "0.8.12"


def test_version_drift_ok_when_bound_matches_code(workspace: Workspace, monkeypatch) -> None:
    from local_webpage_access import doctor as doctor_mod
    from local_webpage_access.daemon import DaemonState
    from local_webpage_access.gateway_service import GatewayState
    from local_webpage_access.manager_service import ManagerState

    cfg = default_config()
    monkeypatch.setattr(
        doctor_mod,
        "_service_observed_running",
        lambda name, ws, config: True,
    )
    monkeypatch.setattr(
        doctor_mod,
        "_read_manager_health_version",
        lambda ws, config: "V0.8.14",
    )
    monkeypatch.setattr(
        "local_webpage_access.version_info.display_version",
        lambda: "V0.8.14",
    )
    monkeypatch.setattr(
        "local_webpage_access.version_info.bind_process_revision",
        lambda: "abc1234def56",
    )
    monkeypatch.setattr(
        "local_webpage_access.manager_service.read_state",
        lambda ws: ManagerState(
            enabled=True, pid=1, port=17800, bind_version="V0.8.14", bind_revision="abc1234def56"
        ),
    )
    monkeypatch.setattr(
        "local_webpage_access.daemon.read_state",
        lambda ws: DaemonState(
            enabled=True, pid=2, bind_version="V0.8.14", bind_revision="abc1234def56"
        ),
    )
    monkeypatch.setattr(
        "local_webpage_access.gateway_service.read_state",
        lambda ws: GatewayState(
            enabled=True, pid=3, bind_version="V0.8.14", bind_revision="abc1234def56"
        ),
    )
    result = check_service_version_drift(workspace, cfg)
    assert result.status == STATUS_OK
    assert "一致" in result.message


def test_version_drift_warns_when_bind_version_missing(workspace, monkeypatch) -> None:
    from local_webpage_access import doctor as doctor_mod
    from local_webpage_access.daemon import DaemonState

    cfg = default_config()
    monkeypatch.setattr(
        doctor_mod,
        "_service_observed_running",
        lambda name, ws, config: name == "daemon",
    )
    monkeypatch.setattr(
        "local_webpage_access.daemon.read_state",
        lambda ws: DaemonState(enabled=True, pid=2, bind_version=None),
    )
    monkeypatch.setattr(
        "local_webpage_access.version_info.display_version",
        lambda: "V0.8.14",
    )
    result = check_service_version_drift(workspace, cfg)
    assert result.status == STATUS_WARN
    assert "daemon" in result.message
    assert "未知" in result.message
    assert "不一致" not in result.message
    assert "lwa services restart" in (result.suggestion or "")


def test_same_version_different_revision_is_drift(workspace, monkeypatch) -> None:
    from local_webpage_access import doctor as doctor_mod
    from local_webpage_access.daemon import DaemonState

    cfg = default_config()
    monkeypatch.setattr(
        doctor_mod,
        "_service_observed_running",
        lambda name, ws, config: name == "daemon",
    )
    monkeypatch.setattr(
        "local_webpage_access.daemon.read_state",
        lambda ws: DaemonState(
            enabled=True, pid=2, bind_version="V0.8.14", bind_revision="oldrev000001"
        ),
    )
    monkeypatch.setattr(
        "local_webpage_access.version_info.display_version",
        lambda: "V0.8.14",
    )
    monkeypatch.setattr(
        "local_webpage_access.version_info.bind_process_revision",
        lambda: "newrev000002",
    )
    result = check_service_version_drift(workspace, cfg)
    assert result.status == STATUS_WARN
    assert "不一致" in result.message
    assert "oldrev000001" in (result.detail or "")
    assert "newrev000002" in (result.detail or result.message)


def test_gateway_drift_labeled_as_lwa_supervisor(workspace, monkeypatch) -> None:
    from local_webpage_access import doctor as doctor_mod
    from local_webpage_access.gateway_service import GatewayState

    cfg = default_config()
    monkeypatch.setattr(
        doctor_mod,
        "_service_observed_running",
        lambda name, ws, config: name == "gateway",
    )
    monkeypatch.setattr(
        "local_webpage_access.gateway_service.read_state",
        lambda ws: GatewayState(enabled=True, pid=3, bind_version="V0.8.12"),
    )
    monkeypatch.setattr(
        "local_webpage_access.version_info.display_version",
        lambda: "V0.8.14",
    )
    result = check_service_version_drift(workspace, cfg)
    assert result.status == STATUS_WARN
    assert "Caddy 二进制" not in result.message
    detail = result.detail or ""
    assert "非 Caddy" in detail
    assert "监管" in detail


def test_run_doctor_includes_service_version_drift(workspace) -> None:
    from local_webpage_access.doctor import run_doctor

    report = run_doctor(workspace, default_config())
    assert "service_version_drift" in [c.name for c in report.checks]
