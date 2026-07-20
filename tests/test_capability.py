"""IMP-033 CapabilityReport 与观测错误分类单测。"""

from __future__ import annotations

import json
from pathlib import Path

from local_webpage_access.capability import (
    CapabilityReport,
    _compute_overall,
    classify_docker_observation_error,
    collect_capability_report,
    log_capability_probe,
    save_profile_state,
    write_capability_cache,
)


def test_classify_docker_observation_error_permission() -> None:
    assert (
        classify_docker_observation_error(
            "permission denied while trying to connect to docker.sock"
        )
        == "permission_denied"
    )


def test_classify_docker_observation_error_timeout() -> None:
    assert classify_docker_observation_error("命令超时（60s）：docker compose") == "timeout"


def test_classify_docker_observation_error_daemon() -> None:
    assert (
        classify_docker_observation_error(
            "Cannot connect to the Docker daemon. Is the docker daemon running?"
        )
        == "daemon_unavailable"
    )


def test_save_and_load_profile_state(tmp_path: Path) -> None:
    path = save_profile_state(
        tmp_path,
        {
            "profile": "full",
            "serviceUser": "fenix",
            "overall": "unready",
            "sessionRefreshRequired": True,
            "action": "lwa setup --full --resume",
        },
    )
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["profile"] == "full"
    assert data["sessionRefreshRequired"] is True


def test_capability_report_to_health_fragment() -> None:
    report = CapabilityReport(
        profile="full",
        overall="degraded",
        service_user="fenix",
        docker_access="permission_denied",
        manager_docker_access="permission_denied",
        session_refresh_required=True,
        action="refresh session",
    )
    frag = report.to_health_fragment()
    assert frag["profile"] == "full"
    assert frag["overall"] == "degraded"
    assert frag["capabilities"]["managerDockerAccess"] == "permission_denied"
    assert frag["capabilities"]["sessionRefreshRequired"] is True


def test_full_overall_requires_caddy_gateway_and_backends() -> None:
    """BUG-233：Full 在 Caddy/gateway/后台 Docker 未证明 ready 时不得 overall=ready。"""
    almost = CapabilityReport(
        profile="full",
        docker_engine="ready",
        docker_compose="ready",
        docker_access="ready",
        cli_docker_access="ready",
        manager_docker_access="ready",
        daemon_docker_access="ready",
        caddy_binary="ready",
        caddy_runtime="unknown",  # 旧逻辑曾允许 unknown → ready
        caddy_owner="unknown",
        caddy_workspace_access="unknown",
        gateway_access="unknown",
    )
    assert _compute_overall(almost) == "unready"

    ready = CapabilityReport(
        profile="full",
        docker_engine="ready",
        docker_compose="ready",
        docker_access="ready",
        cli_docker_access="ready",
        manager_docker_access="ready",
        daemon_docker_access="ready",
        caddy_binary="ready",
        caddy_runtime="ready",
        caddy_owner="lwa_service_user",
        caddy_workspace_access="ready",
        gateway_access="ready",
    )
    assert _compute_overall(ready) == "ready"

    owner_bad = CapabilityReport(
        profile="full",
        docker_engine="ready",
        docker_compose="ready",
        docker_access="ready",
        cli_docker_access="ready",
        manager_docker_access="ready",
        daemon_docker_access="ready",
        caddy_binary="ready",
        caddy_runtime="ready",
        caddy_owner="system_caddy",
        caddy_workspace_access="ready",
        gateway_access="ready",
    )
    assert _compute_overall(owner_bad) == "unready"


def test_write_capability_cache_and_merge(tmp_path: Path, monkeypatch) -> None:
    report = CapabilityReport(
        profile="full",
        overall="degraded",
        manager_docker_access="permission_denied",
        docker_access="permission_denied",
    )
    write_capability_cache(tmp_path, "manager", report)
    monkeypatch.setattr(
        "local_webpage_access.capability._backend_role_alive",
        lambda _root, role: role == "manager",
    )

    monkeypatch.setattr(
        "local_webpage_access.capability.probe_docker_access_state",
        lambda: "ready",
    )
    monkeypatch.setattr(
        "local_webpage_access.capability.probe_caddy_binary_state",
        lambda: "ready",
    )
    monkeypatch.setattr(
        "local_webpage_access.capability.probe_caddy_runtime_fields",
        lambda _root: ("ready", "lwa_service_user", "fenix", "ready"),
    )
    merged = collect_capability_report(
        workspace_root=tmp_path,
        profile="full",
        role="cli",
        include_backend_cached=True,
    )
    assert merged.cli_docker_access == "ready"
    assert merged.manager_docker_access == "permission_denied"


def test_full_manager_view_does_not_require_cli_cache() -> None:
    """BUG-239：manager 健康视角不因 cliDockerAccess=unknown 永久 unready。"""
    report = CapabilityReport(
        profile="full",
        docker_engine="ready",
        docker_compose="ready",
        docker_access="ready",
        cli_docker_access="unknown",
        manager_docker_access="ready",
        daemon_docker_access="ready",
        caddy_binary="ready",
        caddy_runtime="ready",
        caddy_owner="lwa_service_user",
        caddy_workspace_access="ready",
        gateway_access="ready",
        details={"role": "manager"},
    )
    assert _compute_overall(report) == "ready"


def test_live_manager_probe_is_not_overwritten_by_own_cache(
    tmp_path: Path, monkeypatch
) -> None:
    """BUG-246：manager 实时探测优先于旧 capability-manager.json。"""
    write_capability_cache(
        tmp_path,
        "manager",
        CapabilityReport(manager_docker_access="daemon_unavailable"),
    )
    write_capability_cache(
        tmp_path,
        "daemon",
        CapabilityReport(daemon_docker_access="ready"),
    )
    write_capability_cache(
        tmp_path,
        "gateway",
        CapabilityReport(
            gateway_access="ready",
            caddy_runtime="ready",
            caddy_owner="lwa_service_user",
            caddy_workspace_access="ready",
        ),
    )
    monkeypatch.setattr(
        "local_webpage_access.capability._backend_role_alive",
        lambda _root, role: role in ("daemon", "gateway"),
    )
    monkeypatch.setattr(
        "local_webpage_access.capability.probe_docker_access_state", lambda: "ready"
    )
    monkeypatch.setattr(
        "local_webpage_access.capability.probe_caddy_binary_state", lambda: "ready"
    )
    monkeypatch.setattr(
        "local_webpage_access.capability.probe_caddy_runtime_fields",
        lambda _root: ("ready", "lwa_service_user", "fenix", "ready"),
    )

    report = collect_capability_report(
        workspace_root=tmp_path,
        profile="full",
        role="manager",
        include_backend_cached=True,
    )
    assert report.manager_docker_access == "ready"
    assert report.overall == "ready"


def test_stale_gateway_cache_does_not_override_live_caddy(
    tmp_path: Path, monkeypatch
) -> None:
    """BUG-253：gateway 已停或缓存陈旧时，不得用旧 ready 覆盖 live admin_unavailable。"""
    write_capability_cache(
        tmp_path,
        "gateway",
        CapabilityReport(
            gateway_access="ready",
            caddy_runtime="ready",
            caddy_owner="lwa_service_user",
            caddy_workspace_access="ready",
        ),
    )
    # 对应服务未存活 → 拒绝合并
    monkeypatch.setattr(
        "local_webpage_access.capability._backend_role_alive",
        lambda _root, _role: False,
    )
    monkeypatch.setattr(
        "local_webpage_access.capability.probe_docker_access_state", lambda: "ready"
    )
    monkeypatch.setattr(
        "local_webpage_access.capability.probe_caddy_binary_state", lambda: "ready"
    )
    monkeypatch.setattr(
        "local_webpage_access.capability.probe_caddy_runtime_fields",
        lambda _root: ("admin_unavailable", "unknown", None, "ready"),
    )

    report = collect_capability_report(
        workspace_root=tmp_path,
        profile="full",
        role="cli",
        include_backend_cached=True,
    )
    assert report.caddy_runtime == "admin_unavailable"
    assert report.gateway_access == "unknown"


def test_live_caddy_not_overwritten_even_when_gateway_cache_alive(
    tmp_path: Path, monkeypatch
) -> None:
    """BUG-253：即使 gateway 缓存存活，也不得覆盖非 unknown 的实时 Caddy 结果。"""
    write_capability_cache(
        tmp_path,
        "gateway",
        CapabilityReport(
            gateway_access="ready",
            caddy_runtime="ready",
            caddy_owner="lwa_service_user",
            caddy_workspace_access="ready",
        ),
    )
    monkeypatch.setattr(
        "local_webpage_access.capability._backend_role_alive",
        lambda _root, role: role == "gateway",
    )
    monkeypatch.setattr(
        "local_webpage_access.capability.probe_docker_access_state", lambda: "ready"
    )
    monkeypatch.setattr(
        "local_webpage_access.capability.probe_caddy_binary_state", lambda: "ready"
    )
    monkeypatch.setattr(
        "local_webpage_access.capability.probe_caddy_runtime_fields",
        lambda _root: ("admin_unavailable", "unknown", None, "ready"),
    )

    report = collect_capability_report(
        workspace_root=tmp_path,
        profile="full",
        role="cli",
        include_backend_cached=True,
    )
    assert report.caddy_runtime == "admin_unavailable"
    assert report.caddy_owner == "unknown"
    assert report.gateway_access == "ready"


def test_clear_capability_cache(tmp_path: Path) -> None:
    from local_webpage_access.capability import clear_capability_cache

    write_capability_cache(tmp_path, "gateway", CapabilityReport(gateway_access="ready"))
    path = tmp_path / "run" / "capability-gateway.json"
    assert path.is_file()
    clear_capability_cache(tmp_path, "gateway")
    assert not path.is_file()


def test_cache_is_fresh_rejects_future_checked_at() -> None:
    """BUG-258：未来 checkedAt 不得视为新鲜（可允许极小时钟容差）。"""
    from local_webpage_access.capability import _cache_is_fresh

    assert (
        _cache_is_fresh(
            {"checkedAt": "2999-01-01T00:00:00+00:00"},
            now_ts=0.0,
        )
        is False
    )


def test_cache_is_fresh_rejects_stale_and_accepts_recent() -> None:
    """BUG-257/258：过旧拒绝；近期 checkedAt 接受。"""
    from datetime import datetime, timezone

    from local_webpage_access.capability import (
        CAPABILITY_CACHE_MAX_AGE_SECONDS,
        _cache_is_fresh,
    )

    now = 1_700_000_000.0
    stale = datetime.fromtimestamp(
        now - CAPABILITY_CACHE_MAX_AGE_SECONDS - 10, tz=timezone.utc
    ).isoformat()
    recent = datetime.fromtimestamp(now - 60, tz=timezone.utc).isoformat()
    assert _cache_is_fresh({"checkedAt": stale}, now_ts=now) is False
    assert _cache_is_fresh({"checkedAt": recent}, now_ts=now) is True


def test_read_capability_health_fragment_rejects_stale_cache(tmp_path: Path) -> None:
    """BUG-257：/api/health 启动读缓存时拒绝过期 capability-manager.json。"""
    from local_webpage_access.capability import read_capability_health_fragment

    run = tmp_path / "run"
    run.mkdir()
    (run / "capability-manager.json").write_text(
        json.dumps(
            {
                "profile": "full",
                "overall": "ready",
                "checkedAt": "2000-01-01T00:00:00+00:00",
                "serviceUser": "fenix",
                "capabilities": {"dockerAccess": "ready"},
                "action": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert read_capability_health_fragment(tmp_path) is None


def test_backend_role_alive_rejects_unrelated_python_pid(
    tmp_path: Path, monkeypatch
) -> None:
    """BUG-256：PID 存活但命令行非 manager/本工作区时不得信任缓存。"""
    import os

    from local_webpage_access.capability import _backend_role_alive
    from local_webpage_access.manager_service import ManagerState, write_state
    from local_webpage_access.paths import Workspace

    ws = Workspace(tmp_path)
    write_state(
        ws,
        ManagerState(
            enabled=True,
            pid=os.getpid(),
            host="127.0.0.1",
            port=17800,
        ),
    )
    # 当前 pytest 进程不是 manager_service，应判为未存活
    assert _backend_role_alive(tmp_path, "manager") is False

    monkeypatch.setattr(
        "local_webpage_access.daemon.pid_cmdline_contains",
        lambda pid, *needles: True,
    )
    assert _backend_role_alive(tmp_path, "manager") is True


def test_log_capability_probe_does_not_raise(caplog) -> None:
    """不依赖全局 logging 配置：直接监听 capability 模块 logger（避免 suite 污染）。

    configure_logging 会把 ``local_webpage_access`` 父 logger 的 propagate 置 False，
    并跨测试残留；caplog 的 handler 挂在 root，故必须把 handler 直接加到 capability
    子 logger 上，记录才不会被 propagate=False 截断。
    """
    import logging

    from local_webpage_access import capability as capability_mod

    logger = capability_mod.log
    logger.addHandler(caplog.handler)
    orig_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        report = CapabilityReport(
            profile="default",
            overall="ready",
            docker_access="ready",
            action=None,
        )
        log_capability_probe("cli", report, level="INFO")
        assert any("capability probe role=cli" in r.message for r in caplog.records)
    finally:
        logger.removeHandler(caplog.handler)
        logger.setLevel(orig_level)
