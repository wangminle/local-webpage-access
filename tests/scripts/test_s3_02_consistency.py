"""S3-02 / DEV-158 / #57-5 P-2：副本一致性巡检（只读）。"""

from __future__ import annotations

import json
import os
import threading
from types import SimpleNamespace

import pytest

from local_webpage_access.config import Config
from local_webpage_access.consistency import (
    CONSISTENCY_EVENT_TYPE,
    collect_drifts,
    run_consistency_patrol,
)
from local_webpage_access.daemon import run_watcher
from local_webpage_access.doctor import STATUS_OK, STATUS_WARN, check_state_consistency
from local_webpage_access.models import (
    ContainerConfig,
    DesiredState,
    InstanceManifest,
    Kind,
    Runtime,
    ServingMode,
    Status,
    StaticConfig,
)


def _static(hostport: int = 18100, route_mode: str = "port", route_host: str | None = None):
    m = InstanceManifest(
        id="demo",
        name="demo",
        kind=Kind.STATIC,
        runtime=Runtime.SHARED_STATIC,
        version="1",
        servingMode="shared-static",
        static=StaticConfig(routeMode=route_mode, routeHost=route_host, hostPort=hostport),
    )
    return m


def _container(host_port: int = 18200, extra: list[str] | None = None, container_id=None):
    return InstanceManifest(
        id="demo",
        name="demo",
        kind=Kind.PYTHON,
        runtime=Runtime.DOCKER_COMPOSE,
        version="1",
        servingMode=ServingMode.CONTAINER,
        status=Status.RUNNING,
        container=ContainerConfig(
            projectName="demo",
            internalPort=8000,
            hostPort=host_port,
            composePath="docker/compose.yaml",
            dockerfilePath="docker/Dockerfile",
            extraVolumes=list(extra or []),
            containerId=container_id,
        ),
    )


def _seed(workspace, registry, manifest: InstanceManifest) -> None:
    workspace.ensure_app_dirs("demo")
    manifest.save(workspace.app_manifest_path("demo"))
    registry.upsert_from_manifest(manifest)


def _families(drifts) -> list[str]:
    return [d.family for d in drifts]


def test_clean_static_instance_has_no_drift(workspace, registry) -> None:
    _seed(workspace, registry, _static())
    assert collect_drifts(workspace, Config(staticGateway="builtin"), registry) == []


def test_missing_manifest_is_reported(workspace, registry) -> None:
    _seed(workspace, registry, _static())
    workspace.app_manifest_path("demo").unlink()
    drifts = collect_drifts(workspace, Config(staticGateway="builtin"), registry)
    assert _families(drifts) == ["manifest_registry"]
    assert "缺失" in drifts[0].message


def test_corrupt_manifest_is_reported(workspace, registry) -> None:
    _seed(workspace, registry, _static())
    workspace.app_manifest_path("demo").write_text("{not json", encoding="utf-8")
    drifts = collect_drifts(workspace, Config(staticGateway="builtin"), registry)
    assert any("读取失败" in d.message for d in drifts)


def test_status_mismatch_between_manifest_and_registry(workspace, registry) -> None:
    _seed(workspace, registry, _static())
    manifest = InstanceManifest.load(workspace.app_manifest_path("demo"))
    manifest.status = Status.RUNNING
    manifest.save(workspace.app_manifest_path("demo"))
    drifts = collect_drifts(workspace, Config(staticGateway="builtin"), registry)
    assert any("status 不一致" in d.message for d in drifts)


def test_container_host_port_mismatch(workspace, registry) -> None:
    _seed(workspace, registry, _container(host_port=18201))
    manifest = InstanceManifest.load(workspace.app_manifest_path("demo"))
    manifest.container.hostPort = 18200
    manifest.save(workspace.app_manifest_path("demo"))
    drifts = collect_drifts(workspace, Config(staticGateway="builtin"), registry)
    assert any("hostPort 不一致" in d.message for d in drifts)


def test_artifact_drift_when_compose_edited_after_manifest(workspace, registry) -> None:
    from local_webpage_access.lifecycle import _compute_config_fingerprint

    m = _container()
    m.deploymentFingerprints = {"configHash": "stale-baseline"}
    _seed(workspace, registry, m)
    compose = workspace.app_compose_path("demo")
    compose.parent.mkdir(parents=True, exist_ok=True)
    compose.write_text("services: {}\n", encoding="utf-8")
    t = compose.stat().st_mtime
    os.utime(workspace.app_manifest_path("demo"), (t - 60, t - 60))

    drifts = collect_drifts(workspace, Config(staticGateway="builtin"), registry)
    assert any(d.family == "artifacts" for d in drifts)

    m.deploymentFingerprints = {
        "configHash": _compute_config_fingerprint(
            compose, workspace.app_dockerfile_path("demo"), workspace.app_env_path("demo")
        )
    }
    m.save(workspace.app_manifest_path("demo"))
    os.utime(workspace.app_manifest_path("demo"), (t + 60, t + 60))
    drifts = collect_drifts(workspace, Config(staticGateway="builtin"), registry)
    assert not [d for d in drifts if d.family == "artifacts"]


def test_pending_configure_is_not_reported_as_manual_edit(workspace, registry) -> None:
    """configure 刚改 manifest、尚未 restart：生成物旧于 manifest，属正常待生效，不算手改。"""
    m = _container()
    m.deploymentFingerprints = {"configHash": "older-render"}
    _seed(workspace, registry, m)
    compose = workspace.app_compose_path("demo")
    compose.parent.mkdir(parents=True, exist_ok=True)
    compose.write_text("services: {}\n", encoding="utf-8")
    dockerfile = workspace.app_dockerfile_path("demo")
    env = workspace.app_env_path("demo")
    dockerfile.write_text("FROM x\n", encoding="utf-8")
    env.write_text("A=1\n", encoding="utf-8")
    t = compose.stat().st_mtime
    os.utime(compose, (t - 120, t - 120))
    os.utime(dockerfile, (t - 120, t - 120))
    os.utime(env, (t - 120, t - 120))
    os.utime(workspace.app_manifest_path("demo"), (t, t))

    drifts = collect_drifts(workspace, Config(staticGateway="builtin"), registry)
    assert not [d for d in drifts if d.family == "artifacts"]


def _docker_runner(mounts, returncode: int = 0):
    calls: list[list[str]] = []

    def runner(cmd, **kwargs):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=returncode, stdout=json.dumps(mounts), stderr="")

    runner.calls = calls
    return runner


def test_mounts_check_flags_missing_extra_volume(workspace, registry) -> None:
    _seed(
        workspace,
        registry,
        _container(extra=["/mnt/lwa-a:/mnt/a:ro"], container_id="cid123"),
    )
    runner = _docker_runner([{"Destination": "/app/data"}])
    drifts = collect_drifts(
        workspace, Config(staticGateway="builtin"), registry, runner=runner
    )
    mount_drifts = [d for d in drifts if d.family == "mounts"]
    assert len(mount_drifts) == 1
    assert "/mnt/a" in mount_drifts[0].message
    assert runner.calls[0][:2] == ["docker", "inspect"]


def test_mounts_check_passes_when_destination_present(workspace, registry) -> None:
    _seed(
        workspace,
        registry,
        _container(extra=["/mnt/lwa-a:/mnt/a:ro"], container_id="cid123"),
    )
    runner = _docker_runner([{"Destination": "/mnt/a"}])
    drifts = collect_drifts(
        workspace, Config(staticGateway="builtin"), registry, runner=runner
    )
    assert not [d for d in drifts if d.family == "mounts"]


def test_mounts_check_skipped_without_runner_or_on_docker_error(workspace, registry) -> None:
    _seed(
        workspace,
        registry,
        _container(extra=["/mnt/lwa-a:/mnt/a"], container_id="cid123"),
    )
    assert not [
        d
        for d in collect_drifts(workspace, Config(staticGateway="builtin"), registry)
        if d.family == "mounts"
    ]
    failing = _docker_runner([], returncode=1)
    assert not [
        d
        for d in collect_drifts(
            workspace, Config(staticGateway="builtin"), registry, runner=failing
        )
        if d.family == "mounts"
    ]


def _alias_static(alias: str = "first", hostport: int = 18100):
    m = _static(hostport=hostport, route_mode="name", route_host=alias)
    m.status = Status.RUNNING
    m.desiredState = DesiredState.RUNNING
    return m


def test_alias_fragment_clean_then_stale_then_missing(workspace, registry, monkeypatch) -> None:
    from local_webpage_access.static_gateway import StaticGateway

    config = Config(staticGateway="builtin")
    _seed(workspace, registry, _alias_static())
    frag = workspace.app_alias_config("demo")
    frag.parent.mkdir(parents=True, exist_ok=True)
    rendered = StaticGateway(workspace, config).render_alias_fragment(
        "demo", "first", 18100, runtime="shared-static"
    )
    # 实际后端 builtin：别名不走 Caddy 片段（enable 不生成、disable 清残留），
    # 片段陈旧或缺失都不构成漂移（BUG-815）
    frag.write_text("# stale\n", encoding="utf-8")
    assert not [d for d in collect_drifts(workspace, config, registry) if d.family == "alias"]
    frag.unlink()
    assert not [d for d in collect_drifts(workspace, config, registry) if d.family == "alias"]

    caddy = Config(staticGateway="caddy", staticGatewayPort=8080)
    monkeypatch.setattr(StaticGateway, "detect_backend", lambda self: "caddy")
    frag.write_text(rendered, encoding="utf-8")
    assert not [d for d in collect_drifts(workspace, caddy, registry) if d.family == "alias"]
    frag.write_text("# stale\n", encoding="utf-8")
    drifts = [d for d in collect_drifts(workspace, caddy, registry) if d.family == "alias"]
    assert drifts and "不一致" in drifts[0].message
    frag.unlink()
    drifts = [d for d in collect_drifts(workspace, caddy, registry) if d.family == "alias"]
    assert drifts and "缺少片段文件" in drifts[0].message


def test_alias_residue_for_instance_without_alias(workspace, registry, monkeypatch) -> None:
    from local_webpage_access.static_gateway import StaticGateway

    config = Config(staticGateway="caddy", staticGatewayPort=8080)
    monkeypatch.setattr(StaticGateway, "detect_backend", lambda self: "caddy")
    _seed(workspace, registry, _static())
    residue = workspace.static_aliases / "demo.conf"
    residue.parent.mkdir(parents=True, exist_ok=True)
    residue.write_text("# residue\n", encoding="utf-8")
    drifts = [d for d in collect_drifts(workspace, config, registry) if d.family == "alias"]
    assert drifts and "残留" in drifts[0].message


def test_orphan_fragment_without_registry_entry(workspace, registry) -> None:
    config = Config(staticGateway="builtin")
    _seed(workspace, registry, _static())
    ghost = workspace.static_sites / "ghost.conf"
    ghost.parent.mkdir(parents=True, exist_ok=True)
    ghost.write_text("# ghost\n", encoding="utf-8")
    drifts = [d for d in collect_drifts(workspace, config, registry) if d.family == "fragments"]
    assert len(drifts) == 1 and drifts[0].instance_id == "ghost"


def test_caddy_dangling_import_and_unimported_fragment(workspace, registry, monkeypatch) -> None:
    from local_webpage_access.static_gateway import StaticGateway

    config = Config(staticGateway="caddy", staticGatewayPort=8080)
    monkeypatch.setattr(StaticGateway, "detect_backend", lambda self: "caddy")
    _seed(workspace, registry, _alias_static())
    frag = workspace.app_alias_config("demo")
    frag.parent.mkdir(parents=True, exist_ok=True)
    frag.write_text(
        StaticGateway(workspace, config).render_alias_fragment(
            "demo", "first", 18100, runtime="shared-static"
        ),
        encoding="utf-8",
    )
    main = workspace.static_gateway / "Caddyfile"
    main.write_text(f"import `{workspace.root / 'missing-fragment.conf'}`\n", encoding="utf-8")

    drifts = collect_drifts(workspace, config, registry)
    caddy = [d for d in drifts if d.family == "caddy"]
    assert any("不存在" in d.message for d in caddy)
    assert any(d.instance_id == "demo" and "未被主 Caddyfile 引用" in d.message for d in caddy)

    main.write_text(f"import `{frag.as_posix()}`\n", encoding="utf-8")
    drifts = collect_drifts(workspace, config, registry)
    assert not [d for d in drifts if d.family == "caddy"]


def test_caddy_checks_skipped_for_builtin_gateway(workspace, registry) -> None:
    _seed(workspace, registry, _static())
    main = workspace.static_gateway / "Caddyfile"
    main.parent.mkdir(parents=True, exist_ok=True)
    main.write_text("import `/nope.conf`\n", encoding="utf-8")
    assert not [
        d for d in collect_drifts(workspace, Config(staticGateway="builtin"), registry)
        if d.family == "caddy"
    ]


def test_patrol_records_drift_event_once_per_signature(workspace, registry) -> None:
    config = Config(staticGateway="builtin")
    _seed(workspace, registry, _static())
    workspace.app_manifest_path("demo").unlink()

    run_consistency_patrol(workspace, config, registry)
    run_consistency_patrol(workspace, config, registry)

    events = [
        e for e in registry.list_events("demo", limit=50)
        if e["event_type"] == CONSISTENCY_EVENT_TYPE
    ]
    assert len(events) == 1
    assert events[0]["source"] == "daemon"


def test_doctor_check_state_consistency_ok_and_warn(workspace, registry) -> None:
    config = Config(staticGateway="builtin")
    _seed(workspace, registry, _static())
    assert check_state_consistency(workspace, config, registry).status == STATUS_OK

    workspace.app_manifest_path("demo").unlink()
    result = check_state_consistency(workspace, config, registry)
    assert result.status == STATUS_WARN
    assert "副本漂移" in result.message
    assert result.detail and "manifest/registry" in result.detail
    assert result.suggestion


def test_daemon_runs_consistency_check_on_interval(workspace, registry) -> None:
    import time

    config = Config(staticGateway="builtin")
    _seed(workspace, registry, _static())
    calls: list[float] = []
    stop = threading.Event()

    def check(ws, cfg, reg) -> None:
        calls.append(time.monotonic())
        if len(calls) >= 2:
            stop.set()

    run_watcher(
        workspace,
        config,
        registry,
        stop_event=stop,
        poll_interval=0.02,
        process_fn=lambda *a, **k: {"action": "skipped"},
        consistency_check=check,
        consistency_interval=0.2,
        capability_initial_delay=1e9,
    )
    assert len(calls) == 2
    assert calls[1] - calls[0] >= 0.15


@pytest.mark.parametrize("edited", ["dockerfile", "env"])
def test_artifact_drift_when_only_dockerfile_or_env_edited(workspace, registry, edited) -> None:
    """只手改 Dockerfile 或 .env（compose 未动）也必须报告漂移：
    判定取三个生成物中最新的 mtime，而非只看 compose。"""
    m = _container()
    m.deploymentFingerprints = {"configHash": "stale-baseline"}
    _seed(workspace, registry, m)
    compose = workspace.app_compose_path("demo")
    compose.parent.mkdir(parents=True, exist_ok=True)
    compose.write_text("services: {}\n", encoding="utf-8")
    dockerfile = workspace.app_dockerfile_path("demo")
    env = workspace.app_env_path("demo")
    dockerfile.write_text("FROM x\n", encoding="utf-8")
    env.write_text("A=1\n", encoding="utf-8")
    t = compose.stat().st_mtime
    manifest_mtime = t
    # compose 与未编辑的那个生成物都早于 manifest；被编辑的那个晚于 manifest
    for path in (compose, dockerfile, env):
        os.utime(path, (t - 120, t - 120))
    os.utime(workspace.app_manifest_path("demo"), (manifest_mtime, manifest_mtime))
    edited_path = dockerfile if edited == "dockerfile" else env
    os.utime(edited_path, (t + 120, t + 120))

    drifts = collect_drifts(workspace, Config(staticGateway="builtin"), registry)
    assert any(d.family == "artifacts" for d in drifts)


def test_artifact_drift_when_dockerfile_or_env_deleted(workspace, registry) -> None:
    """删除 Dockerfile 或 .env（compose 未动、mtime 仍旧于 manifest）也必须报告漂移。"""
    m = _container()
    m.deploymentFingerprints = {"configHash": "stale-baseline"}
    _seed(workspace, registry, m)
    compose = workspace.app_compose_path("demo")
    compose.parent.mkdir(parents=True, exist_ok=True)
    compose.write_text("services: {}\n", encoding="utf-8")
    dockerfile = workspace.app_dockerfile_path("demo")
    env = workspace.app_env_path("demo")
    dockerfile.write_text("FROM x\n", encoding="utf-8")
    env.write_text("A=1\n", encoding="utf-8")
    t = compose.stat().st_mtime
    os.utime(compose, (t - 120, t - 120))
    os.utime(env, (t - 120, t - 120))
    os.utime(workspace.app_manifest_path("demo"), (t, t))
    dockerfile.unlink()

    drifts = collect_drifts(workspace, Config(staticGateway="builtin"), registry)
    assert any(d.family == "artifacts" for d in drifts)


def test_stopped_instance_missing_alias_fragment_is_not_drift(workspace, registry, monkeypatch) -> None:
    """正常 stop 会撤掉别名片段，不得报「缺少片段文件」。"""
    from local_webpage_access.static_gateway import StaticGateway

    config = Config(staticGateway="builtin")
    m = _alias_static()
    m.status = Status.STOPPED
    m.desiredState = DesiredState.STOPPED
    _seed(workspace, registry, m)
    assert not workspace.app_alias_config("demo").is_file()
    assert not [d for d in collect_drifts(workspace, config, registry) if d.family == "alias"]

    m.status = Status.RUNNING
    m.desiredState = DesiredState.RUNNING
    m.save(workspace.app_manifest_path("demo"))
    registry.upsert_from_manifest(m)
    # builtin 不生成 Caddy 片段，routeMode=name 只是保留的元数据
    assert not [d for d in collect_drifts(workspace, config, registry) if d.family == "alias"]
    caddy = Config(staticGateway="caddy", staticGatewayPort=8080)
    monkeypatch.setattr(StaticGateway, "detect_backend", lambda self: "caddy")
    drifts = [d for d in collect_drifts(workspace, caddy, registry) if d.family == "alias"]
    assert drifts and "缺少片段文件" in drifts[0].message


def test_degraded_builtin_backend_skips_alias_fragment_checks(workspace, registry, monkeypatch) -> None:
    """BUG-815：配置 caddy 但二进制缺失被降级 builtin 时，缺片段不是漂移。"""
    from local_webpage_access.static_gateway import StaticGateway

    config = Config(staticGateway="caddy", staticGatewayPort=8080)
    monkeypatch.setattr(StaticGateway, "detect_backend", lambda self: "builtin")
    _seed(workspace, registry, _alias_static())
    assert not workspace.app_alias_config("demo").is_file()

    assert not [d for d in collect_drifts(workspace, config, registry) if d.family == "alias"]
    assert not [d for d in collect_drifts(workspace, config, registry) if d.family == "caddy"]


def test_stopped_container_residual_fragment_is_not_drift(workspace, registry, monkeypatch) -> None:
    """容器 stop 有意保留别名片段（不动网关，start 即恢复）——不得按残留误报。"""
    from local_webpage_access.static_gateway import StaticGateway

    config = Config(staticGateway="caddy", staticGatewayPort=8080)
    monkeypatch.setattr(StaticGateway, "detect_backend", lambda self: "caddy")
    m = _container()
    m.container.routeMode = "name"
    m.container.routeHost = "first"
    m.status = Status.STOPPED
    m.desiredState = DesiredState.STOPPED
    _seed(workspace, registry, m)
    frag = workspace.app_alias_config("demo")
    frag.parent.mkdir(parents=True, exist_ok=True)
    frag.write_text("# kept by container stop\n", encoding="utf-8")

    drifts = [d for d in collect_drifts(workspace, config, registry) if d.family == "alias"]
    assert drifts == []


def test_stopped_static_residual_fragment_reports_accurate_message(workspace, registry, monkeypatch) -> None:
    """静态 stop 会撤掉片段：残留是真漂移，但文案不得说「已无路径别名」。"""
    from local_webpage_access.static_gateway import StaticGateway

    config = Config(staticGateway="caddy", staticGatewayPort=8080)
    monkeypatch.setattr(StaticGateway, "detect_backend", lambda self: "caddy")
    m = _alias_static()
    m.status = Status.STOPPED
    m.desiredState = DesiredState.STOPPED
    _seed(workspace, registry, m)
    frag = workspace.app_alias_config("demo")
    frag.parent.mkdir(parents=True, exist_ok=True)
    frag.write_text("# residual after stop\n", encoding="utf-8")

    drifts = [d for d in collect_drifts(workspace, config, registry) if d.family == "alias"]
    assert len(drifts) == 1
    assert "已停止" in drifts[0].message and "已无路径别名" not in drifts[0].message
    assert "lwa stop" in (drifts[0].suggestion or "")


def test_building_static_residual_fragment_is_not_drift(workspace, registry, monkeypatch) -> None:
    """BUG-825：host_static 开始登记 building、旧片段正常存在——不得判为停止残留。"""
    from local_webpage_access.static_gateway import StaticGateway

    config = Config(staticGateway="caddy", staticGatewayPort=8080)
    monkeypatch.setattr(StaticGateway, "detect_backend", lambda self: "caddy")
    m = _alias_static()
    m.status = Status.BUILDING
    _seed(workspace, registry, m)
    registry.update_status("demo", Status.BUILDING.value)
    frag = workspace.app_alias_config("demo")
    frag.parent.mkdir(parents=True, exist_ok=True)
    frag.write_text("# fragment while rebuilding\n", encoding="utf-8")

    drifts = [d for d in collect_drifts(workspace, config, registry) if d.family == "alias"]
    assert drifts == []


def test_no_alias_instance_residual_fragment_keeps_original_message(workspace, registry, monkeypatch) -> None:
    """别名已移除的实例残留片段：保留原「已无路径别名」判定与文案。"""
    from local_webpage_access.static_gateway import StaticGateway

    config = Config(staticGateway="caddy", staticGatewayPort=8080)
    monkeypatch.setattr(StaticGateway, "detect_backend", lambda self: "caddy")
    _seed(workspace, registry, _alias_static())
    manifest = InstanceManifest.load(workspace.app_manifest_path("demo"))
    manifest.static.routeMode = "port"
    manifest.static.routeHost = None
    manifest.save(workspace.app_manifest_path("demo"))
    registry.upsert_from_manifest(manifest)
    frag = workspace.app_alias_config("demo")
    frag.parent.mkdir(parents=True, exist_ok=True)
    frag.write_text("# stale\n", encoding="utf-8")

    drifts = [d for d in collect_drifts(workspace, config, registry) if d.family == "alias"]
    assert len(drifts) == 1 and "已无路径别名" in drifts[0].message
