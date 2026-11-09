"""Dell 验收暴露的 LAN 生命周期、诊断和锁重试边界。"""

from __future__ import annotations

import pytest

from local_webpage_access import access, access_workflow, doctor, ports, status
from local_webpage_access.models import InstanceManifest
from local_webpage_access.static_gateway import StaticGateway
from tests.scripts.test_access import _seed_static

LAN = "192.168.1.14"
BAD = "http://[broken:8080/demo/"


def test_static_enable_keeps_legal_links_when_lan_is_unknown(workspace, registry, config, monkeypatch):
    from local_webpage_access.hosting import _enable_static

    manifest = _seed_static(
        workspace, registry, "demo", lan_url=f"http://{LAN}:21000",
        route_host="demo", route_url=f"http://{LAN}:8080/demo/",
    )
    monkeypatch.setattr(ports, "detect_lan_ip", lambda: None)
    monkeypatch.setattr(StaticGateway, "enable", lambda *a, **kw: None)
    monkeypatch.setattr(StaticGateway, "detect_backend", lambda self: "builtin")
    updated = _enable_static(workspace, config, registry, "demo", manifest, workspace.app_public("demo"))
    assert updated.network.lanUrl == f"http://{LAN}:21000"
    assert updated.network.routeUrl == f"http://{LAN}:8080/demo/"
    assert updated.network.healthUrl == "http://127.0.0.1:21000"


def test_status_marks_preserved_entry_for_review_when_lan_is_unknown(
    workspace, registry, config, monkeypatch,
):
    _seed_static(workspace, registry, "demo", lan_url=f"http://{LAN}:21000")
    monkeypatch.setattr(ports, "detect_lan_ip", lambda: None)
    monkeypatch.setattr("local_webpage_access.wsl_network.collect_wsl_network_snapshot", lambda: None)
    snap = status.instance_status(workspace, config, registry, "demo")
    assert snap.lan_url == f"http://{LAN}:21000"
    assert snap.lan_address_stale


@pytest.mark.parametrize("field", ["lanUrl", "routeUrl"])
def test_bad_url_is_reported_and_refresh_can_repair_it(
    workspace, registry, config, monkeypatch, field,
):
    manifest = _seed_static(
        workspace, registry, "demo", lan_url=f"http://{LAN}:21000",
        route_host="demo", route_url=f"http://{LAN}:8080/demo/",
    )
    setattr(manifest.network, field, BAD)
    path = workspace.app_manifest_path("demo")
    manifest.save(path)
    monkeypatch.setattr(ports, "detect_lan_ip", lambda: LAN)
    monkeypatch.setattr("local_webpage_access.wsl_network.collect_wsl_network_snapshot", lambda: None)
    monkeypatch.setattr(access, "_http_get", lambda url, **kw: access.UrlProbe(url=url, ok=True))

    result = doctor.check_lan_url_stale(workspace, config, registry)
    assert result.status == doctor.STATUS_WARN and field in (result.detail or "")
    assert access.review_access(workspace, config, registry).overall == "warn"
    assert access_workflow._persisted_lan_hosts_differ(workspace, registry, LAN)
    snap = status.instance_status(workspace, config, registry, "demo")
    assert snap.lan_address_stale and BAD not in (snap.lan_url, snap.route_url)
    assert len(access.refresh_network_entries(workspace, config, registry).refreshed) == 1
    saved = InstanceManifest.load(path)
    assert (saved.network.lanUrl, saved.network.routeUrl) == (
        f"http://{LAN}:21000", f"http://{LAN}:8080/demo/",
    )


def test_legacy_manual_benchmark_does_not_pass_doctor(workspace, registry, config, monkeypatch):
    _seed_static(
        workspace, registry, "demo", lan_url="http://198.18.0.1:21000",
        route_host="demo", route_url="http://198.18.0.1:8080/demo/",
    )
    config.lanIpStrategy = "manual"
    config.manualLanIp = "198.18.0.1"
    monkeypatch.setattr("local_webpage_access.wsl_network.collect_wsl_network_snapshot", lambda: None)
    result = doctor.check_lan_url_stale(workspace, config, registry)
    assert result.status == doctor.STATUS_WARN
    assert "基准测试" in result.message
    assert "lan set" in (result.suggestion or "")


def test_refresh_lock_retry_records_non_lock_skip_once(workspace, registry, config, monkeypatch):
    reports = iter((
        access.RefreshReport(lan_ip=LAN, skipped=["noport", "busy"], lock_skipped=["busy"]),
        access.RefreshReport(lan_ip=LAN, skipped=["busy"]),
    ))
    monkeypatch.setattr(access_workflow, "refresh_network_entries", lambda *a, **kw: next(reports))
    monkeypatch.setattr(access_workflow.time, "sleep", lambda _: None)
    result = access_workflow.refresh_with_lock_retry(workspace, config, registry)
    assert result.skipped == ["noport", "busy"]
    assert result.lock_skipped == []


@pytest.mark.parametrize(
    ("saved_route", "expected"),
    [
        (f"http://{LAN}/demo/", f"http://{LAN}/demo/"),
        (f"http://{LAN}/old/", None),
        ("http://198.18.0.1/demo/", None),
        (BAD, None),
    ],
)
def test_rebuild_preserves_only_matching_route_on_implicit_gateway_port(
    config, monkeypatch, saved_route, expected,
):
    from local_webpage_access.models import NetworkConfig

    config.staticGatewayPort = 80
    monkeypatch.setattr(ports, "detect_lan_ip", lambda: None)
    previous = NetworkConfig(
        lanUrl=f"http://{LAN}:21000", routeUrl=saved_route, routeMode="name", routeHost="demo"
    )
    result = ports.build_network_entry(config, 21001, path_alias="demo", previous=previous)
    assert result["lanUrl"] is None
    assert result["routeUrl"] == expected
    assert result["healthUrl"] == "http://127.0.0.1:21001"


def test_refresh_does_not_retry_other_lifecycle_errors(workspace, registry, config, monkeypatch):
    from local_webpage_access.errors import LifecycleError

    _seed_static(workspace, registry, "demo", lan_url=f"http://{LAN}:21000")
    monkeypatch.setattr(access, "resolve_lan_ip", lambda cfg: LAN)

    def reject_save(*args, **kwargs):
        raise LifecycleError("非锁竞争")

    monkeypatch.setattr(InstanceManifest, "save", reject_save)
    result = access_workflow.refresh_with_lock_retry(workspace, config, registry, timeout=0.001)
    assert result.skipped == ["demo"]
    assert result.lock_skipped == []
    assert result.lock_retries == 0


def test_retry_repairs_released_real_lock_without_repeating_tls_sync(
    workspace, registry, config, monkeypatch,
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from local_webpage_access.lifecycle import instance_lock

    _seed_static(workspace, registry, "busy")
    monkeypatch.setattr(access, "resolve_lan_ip", lambda cfg: LAN)
    tls_syncs = []
    monkeypatch.setattr(
        access, "sync_caddy_tls_bind", lambda ws, cfg, ip: tls_syncs.append(ip) or (False, None),
    )
    locked, release = Event(), Event()

    def hold_lock():
        with instance_lock(workspace, "busy"):
            locked.set()
            assert release.wait(5)

    with ThreadPoolExecutor(max_workers=1) as executor:
        holder = executor.submit(hold_lock)
        assert locked.wait(5)

        def unlock(_delay):
            release.set()
            holder.result(timeout=5)

        monkeypatch.setattr(access_workflow.time, "sleep", unlock)
        try:
            report = access_workflow.refresh_with_lock_retry(workspace, config, registry)
        finally:
            release.set()

    assert report.lock_retries == 1
    assert report.skipped == report.lock_skipped == []
    assert [item.instance_id for item in report.refreshed] == ["busy"]
    assert tls_syncs == [LAN]
    assert InstanceManifest.load(workspace.app_manifest_path("busy")).network.lanUrl == f"http://{LAN}:21000"


def test_retry_leaves_pending_instance_when_lan_policy_changes(
    workspace, registry, config, monkeypatch,
):
    from local_webpage_access.lan_policy import write_lan_policy

    workspace.config_path.write_text("lanIpStrategy: auto\nmanualLanIp: null\n", encoding="utf-8")
    calls = []
    first = access.RefreshReport(lan_ip=LAN, skipped=["busy"], lock_skipped=["busy"])

    def refresh(*args, **kwargs):
        calls.append(kwargs)
        return first if len(calls) == 1 else access.RefreshReport(lan_ip=LAN)

    monkeypatch.setattr(access_workflow, "refresh_network_entries", refresh)
    monkeypatch.setattr(
        access_workflow.time, "sleep", lambda _: write_lan_policy(workspace, "192.168.0.22"),
    )
    result = access_workflow.refresh_with_lock_retry(workspace, config, registry)
    assert calls == [{}]
    assert result.lan_reason == "policy_changed"
    assert result.lock_skipped == ["busy"]


def test_manual_benchmark_cannot_return_via_tls_fallback(workspace, config, monkeypatch):
    config.staticGateway = "caddy"
    config.gatewayTls = "internal"
    config.lanIpStrategy = "manual"
    config.manualLanIp = "198.18.0.1"
    monkeypatch.setattr("local_webpage_access.wsl_network.collect_wsl_network_snapshot", lambda: None)
    hosts, _ = StaticGateway(workspace, config)._tls_hosts()
    assert "198.18.0.1" not in hosts


def test_background_fills_missing_links_after_lan_recovers(workspace, registry, config, monkeypatch):
    _seed_static(workspace, registry, "demo", lan_url=None, route_host="demo", route_url=None)
    access_workflow.reset_lan_refresh_throttle_state()
    monkeypatch.setattr(access_workflow, "resolve_lan_ip", lambda cfg: LAN)
    monkeypatch.setattr(access, "resolve_lan_ip", lambda cfg: LAN)
    monkeypatch.setattr(ports, "resolve_lan_ip", lambda cfg: LAN)
    monkeypatch.setattr("local_webpage_access.wsl_network.collect_wsl_network_snapshot", lambda: None)
    assert doctor.check_lan_url_stale(workspace, config, registry).status == doctor.STATUS_SKIP
    try:
        report = access_workflow.maybe_throttled_lan_refresh(workspace, config, registry)
    finally:
        access_workflow.reset_lan_refresh_throttle_state()
    assert report is not None and len(report.refreshed) == 1
    network = InstanceManifest.load(workspace.app_manifest_path("demo")).network
    assert (network.lanUrl, network.routeUrl) == (f"http://{LAN}:21000", f"http://{LAN}:8080/demo/")
