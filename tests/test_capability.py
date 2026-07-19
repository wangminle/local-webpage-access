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


def test_log_capability_probe_does_not_raise(caplog) -> None:
    import logging

    caplog.set_level(logging.INFO)
    report = CapabilityReport(
        profile="default",
        overall="ready",
        docker_access="ready",
        action=None,
    )
    log_capability_probe("cli", report, level="INFO")
    assert any("capability probe role=cli" in r.message for r in caplog.records)
