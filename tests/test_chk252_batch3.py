"""CHK-252 第三批：别名活验证、指纹拆分、verificationOverrides。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from local_webpage_access.errors import RecognitionError
from local_webpage_access.models import (
    ContainerConfig,
    DesiredState,
    InstanceManifest,
    Kind,
    ResourceProfile,
    Runtime,
    ServingMode,
    StaticConfig,
    Status,
)
from local_webpage_access.path_alias import set_instance_path_alias, verify_alias_live
from local_webpage_access.verification_config import (
    effective_capability_contract,
    get_verification_overrides,
    set_verification_overrides,
)


def test_fingerprint_change_action_runtime_only() -> None:
    from local_webpage_access.lifecycle import _fingerprint_change_action

    stored = {
        "sourceHash": "a",
        "planHash": "b",
        "buildConfigHash": "build1",
        "runtimeConfigHash": "run1",
        "configHash": "c",
        "imageId": "d",
    }
    current = dict(stored)
    current["runtimeConfigHash"] = "run2"
    action, fields = _fingerprint_change_action(stored, current)
    assert action == "runtime_recreate"
    assert fields == ["runtimeConfigHash"]


def test_fingerprint_change_action_build_change() -> None:
    from local_webpage_access.lifecycle import _fingerprint_change_action

    stored = {
        "sourceHash": "a",
        "planHash": "b",
        "buildConfigHash": "build1",
        "runtimeConfigHash": "run1",
        "configHash": "c",
        "imageId": "d",
    }
    current = dict(stored)
    current["buildConfigHash"] = "build2"
    action, fields = _fingerprint_change_action(stored, current)
    assert action == "full_rebuild"
    assert "buildConfigHash" in fields


def test_compute_deployment_fingerprints_includes_split_hashes() -> None:
    from local_webpage_access.lifecycle import _compute_deployment_fingerprints

    manifest = InstanceManifest(
        id="fp",
        name="fp",
        version="1",
        kind=Kind.PYTHON,
        stack=[],
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.SMALL,
        status=Status.STOPPED,
        desiredState=DesiredState.STOPPED,
        container=ContainerConfig(
            projectName="lwa-fp",
            composePath="/tmp/compose.yaml",
            dockerfilePath="/tmp/Dockerfile",
            hostPort=18080,
            internalPort=8000,
        ),
    )
    ws = MagicMock()
    ws.app_env_path = MagicMock(return_value=Path("/tmp/nonexistent.env"))
    fps = _compute_deployment_fingerprints(ws, manifest)
    assert "buildConfigHash" in fps
    assert "runtimeConfigHash" in fps


def _minimal_container() -> ContainerConfig:
    return ContainerConfig(
        projectName="lwa-test",
        composePath="/tmp/compose.yaml",
        dockerfilePath="/tmp/Dockerfile",
        hostPort=18080,
        internalPort=8000,
    )


def test_discovered_probe_not_mandatory_by_default() -> None:
    manifest = InstanceManifest(
        id="probe",
        name="probe",
        version="1",
        kind=Kind.PYTHON,
        stack=[],
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.SMALL,
        status=Status.STOPPED,
        desiredState=DesiredState.STOPPED,
        container=_minimal_container(),
        capabilityContract={
            "requiredProbes": [
                {
                    "path": "/health",
                    "method": "GET",
                    "expectedStatus": 200,
                    "isMandatory": True,
                    "source": "discovered",
                }
            ]
        },
    )
    contract = effective_capability_contract(manifest)
    assert contract.requiredProbes[0].isMandatory is False


def test_user_probe_is_mandatory() -> None:
    manifest = InstanceManifest(
        id="probe2",
        name="probe2",
        version="1",
        kind=Kind.PYTHON,
        stack=[],
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.SMALL,
        status=Status.STOPPED,
        desiredState=DesiredState.STOPPED,
        container=_minimal_container(),
        verificationOverrides={
            "probes": [{"path": "/ready", "expectedStatus": 200}],
        },
    )
    contract = effective_capability_contract(manifest)
    assert any(p.path == "/ready" and p.isMandatory for p in contract.requiredProbes)


def test_verify_alias_live_rejects_html_for_js(
    config, monkeypatch
) -> None:
    from local_webpage_access import path_alias

    html = '<script src="/demo/assets/app.js"></script>'

    def fake_probe(url, *, timeout=3.0):
        if url.endswith("/demo/"):
            return True, 200, "text/html", b"<!doctype html><html></html>"
        if "app.js" in url:
            return True, 200, "text/html", b"<!doctype html><html></html>"
        return True, 200, "application/javascript", b"console.log(1)"

    monkeypatch.setattr(path_alias, "_http_probe_alias_resource", fake_probe)
    config.staticGatewayPort = 8080
    with pytest.raises(RecognitionError, match="HTML 而非静态文件"):
        verify_alias_live(config, "demo", entry_html=html, instance_id="x")


def test_set_alias_skip_compat_check_skips_live_verify(
    workspace, registry, config, monkeypatch
) -> None:
    from local_webpage_access import path_alias

    workspace.ensure_app_dirs("alias-skip")
    (workspace.app_current("alias-skip") / "index.html").write_text(
        '<script src="./assets/app.js"></script>'
    )
    manifest = InstanceManifest(
        id="alias-skip",
        name="alias-skip",
        version="1",
        kind=Kind.STATIC,
        stack=[],
        runtime=Runtime.SHARED_STATIC,
        servingMode=ServingMode.SHARED_STATIC,
        resourceProfile=ResourceProfile.SMALL,
        status=Status.RUNNING,
        desiredState=DesiredState.RUNNING,
        static=StaticConfig(hostPort=21100, enabled=True),
    )
    manifest.save(workspace.app_manifest_path("alias-skip"))
    registry.upsert_from_manifest(manifest)

    class _FakeGW:
        def __init__(self, ws, cfg):
            self.ws = ws

        def detect_backend(self):
            return "caddy"

        def is_enabled(self, iid):
            return True

        def generate_alias_config(self, iid, alias, hp, **kwargs):
            pass

        def reload_all(self):
            pass

        def remove_alias_config(self, iid):
            pass

    monkeypatch.setattr(path_alias, "StaticGateway", _FakeGW)
    monkeypatch.setattr(
        path_alias,
        "_fetch_entrypoint_html_for_alias_guard",
        lambda **kwargs: '<script src="./assets/app.js"></script>',
    )
    live_called = {"n": 0}

    def boom(*args, **kwargs):
        live_called["n"] += 1
        raise RecognitionError("should not run")

    monkeypatch.setattr(path_alias, "verify_alias_live", boom)

    result = set_instance_path_alias(
        workspace,
        config,
        registry,
        "alias-skip",
        "my-slug",
        skip_compat_check=True,
    )
    assert live_called["n"] == 0
    assert result.compat_check_skipped is True
    reloaded = InstanceManifest.load(workspace.app_manifest_path("alias-skip"))
    assert reloaded.static is not None
    assert reloaded.static.routeHost == "my-slug"


def test_verification_overrides_roundtrip() -> None:
    manifest = InstanceManifest(
        id="ov",
        name="ov",
        version="1",
        kind=Kind.PYTHON,
        stack=[],
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.SMALL,
        status=Status.STOPPED,
        desiredState=DesiredState.STOPPED,
        container=_minimal_container(),
    )
    set_verification_overrides(
        manifest,
        {"disableAutoProbes": True, "probes": [{"path": "/health", "expectedStatus": 204}]},
    )
    ov = get_verification_overrides(manifest)
    assert ov["disableAutoProbes"] is True
    assert ov["probes"][0]["path"] == "/health"


def test_recreate_container_runtime_force_recreate(
    workspace, registry, config, monkeypatch
) -> None:
    from local_webpage_access import hosting

    workspace.ensure_app_dirs("recreate")
    compose = workspace.app_compose_path("recreate")
    env = workspace.app_env_path("recreate")
    compose.write_text("services: {}\n")
    env.write_text("FOO=bar\n")

    manifest = InstanceManifest(
        id="recreate",
        name="recreate",
        version="1",
        kind=Kind.PYTHON,
        stack=[],
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.SMALL,
        status=Status.RUNNING,
        desiredState=DesiredState.RUNNING,
        container=ContainerConfig(
            projectName="lwa-recreate",
            composePath=str(compose),
            dockerfilePath=str(workspace.app_dockerfile_path("recreate")),
            hostPort=21101,
            internalPort=8000,
            containerId="cid-old",
            imageId="sha256:old",
        ),
    )
    manifest.save(workspace.app_manifest_path("recreate"))
    registry.upsert_from_manifest(manifest)

    class _FakeRuntime:
        def __init__(self, ws, reg):
            self.up_kwargs = {}

        def is_running(self, iid):
            return True

        def stop(self, iid):
            pass

        def up(self, iid, *, force_recreate=False):
            self.up_kwargs = {"force_recreate": force_recreate}

        def container_id(self, iid):
            return "cid-new"

        def image_id(self, iid):
            return "sha256:old"

    fake = _FakeRuntime(workspace, registry)
    monkeypatch.setattr(hosting, "DockerRuntime", MagicMock())
    hosting.DockerRuntime.ensure_available = lambda: None
    hosting.DockerRuntime.return_value = fake
    monkeypatch.setattr(
        hosting,
        "_evaluate_container_verification",
        lambda *a, **k: {"overall_status": "passed", "liveness_passed": True},
    )

    out = hosting.recreate_container_runtime(workspace, config, registry, "recreate")
    assert fake.up_kwargs.get("force_recreate") is True
    assert out.container is not None
    assert out.container.containerId == "cid-new"


def test_fingerprint_change_action_real_env_change_runtime_recreate(workspace) -> None:
    """BUG-583：真实修改 .env → 重算完整指纹 → 决策必须是 runtime_recreate。

    真实场景下 .env 变化会同时改写 runtimeConfigHash 与聚合 configHash，
    新格式指纹决策必须只看拆分后的 hash 字段，否则永远落入 full_rebuild。
    """
    from local_webpage_access.lifecycle import (
        _compute_deployment_fingerprints,
        _fingerprint_change_action,
    )

    workspace.ensure_app_dirs("fp-real")
    compose = workspace.app_compose_path("fp-real")
    env = workspace.app_env_path("fp-real")
    dockerfile = workspace.app_dockerfile_path("fp-real")
    compose.write_text("services:\n  app:\n    image: img\n")
    env.write_text("FOO=bar\n")
    dockerfile.write_text("FROM python:3.12\n")

    manifest = InstanceManifest(
        id="fp-real",
        name="fp-real",
        version="1",
        kind=Kind.PYTHON,
        stack=[],
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.SMALL,
        status=Status.RUNNING,
        desiredState=DesiredState.RUNNING,
        container=ContainerConfig(
            projectName="lwa-fp-real",
            composePath=str(compose),
            dockerfilePath=str(dockerfile),
            hostPort=18080,
            internalPort=8000,
            imageId="sha256:img",
        ),
    )

    stored = _compute_deployment_fingerprints(workspace, manifest)
    env.write_text("FOO=baz\n")
    current = _compute_deployment_fingerprints(workspace, manifest)
    # 前置断言：真实指纹计算里 runtime 与聚合 configHash 会同时变化
    assert current["runtimeConfigHash"] != stored["runtimeConfigHash"]
    assert current["configHash"] != stored["configHash"]
    action, fields = _fingerprint_change_action(stored, current)
    assert action == "runtime_recreate"
    assert fields == ["runtimeConfigHash"]


def test_fingerprint_change_action_real_compose_change_runtime_recreate(workspace) -> None:
    """BUG-583：真实修改 Compose runtime 段 → 决策必须是 runtime_recreate。"""
    from local_webpage_access.lifecycle import (
        _compute_deployment_fingerprints,
        _fingerprint_change_action,
    )

    workspace.ensure_app_dirs("fp-real2")
    compose = workspace.app_compose_path("fp-real2")
    env = workspace.app_env_path("fp-real2")
    dockerfile = workspace.app_dockerfile_path("fp-real2")
    compose.write_text("services:\n  app:\n    image: img\n")
    env.write_text("FOO=bar\n")
    dockerfile.write_text("FROM python:3.12\n")

    manifest = InstanceManifest(
        id="fp-real2",
        name="fp-real2",
        version="1",
        kind=Kind.PYTHON,
        stack=[],
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.SMALL,
        status=Status.RUNNING,
        desiredState=DesiredState.RUNNING,
        container=ContainerConfig(
            projectName="lwa-fp-real2",
            composePath=str(compose),
            dockerfilePath=str(dockerfile),
            hostPort=18081,
            internalPort=8000,
            imageId="sha256:img",
        ),
    )

    stored = _compute_deployment_fingerprints(workspace, manifest)
    compose.write_text("services:\n  app:\n    image: img\n    mem_limit: 256m\n")
    current = _compute_deployment_fingerprints(workspace, manifest)
    assert current["runtimeConfigHash"] != stored["runtimeConfigHash"]
    assert current["configHash"] != stored["configHash"]
    assert current["buildConfigHash"] == stored["buildConfigHash"]
    action, fields = _fingerprint_change_action(stored, current)
    assert action == "runtime_recreate"
    assert fields == ["runtimeConfigHash"]


def _setup_recreate_instance(workspace, registry, instance_id: str) -> None:
    workspace.ensure_app_dirs(instance_id)
    compose = workspace.app_compose_path(instance_id)
    env = workspace.app_env_path(instance_id)
    compose.write_text("services: {}\n")
    env.write_text("FOO=bar\n")
    manifest = InstanceManifest(
        id=instance_id,
        name=instance_id,
        version="1",
        kind=Kind.PYTHON,
        stack=[],
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.SMALL,
        status=Status.RUNNING,
        desiredState=DesiredState.RUNNING,
        container=ContainerConfig(
            projectName=f"lwa-{instance_id}",
            composePath=str(compose),
            dockerfilePath=str(workspace.app_dockerfile_path(instance_id)),
            hostPort=21101,
            internalPort=8000,
            containerId="cid-old",
            imageId="sha256:old",
        ),
    )
    manifest.save(workspace.app_manifest_path(instance_id))
    registry.upsert_from_manifest(manifest)


def _patch_fake_runtime(monkeypatch, hosting, fake) -> None:
    monkeypatch.setattr(hosting, "DockerRuntime", MagicMock())
    hosting.DockerRuntime.ensure_available = lambda: None
    hosting.DockerRuntime.return_value = fake


def test_recreate_container_runtime_up_failure_rolls_back(
    workspace, registry, config, monkeypatch
) -> None:
    """BUG-589：compose up 失败 → 清理新容器、写 FAILED 落盘、记录诊断事件。"""
    from local_webpage_access import hosting
    from local_webpage_access.errors import DockerError

    _setup_recreate_instance(workspace, registry, "recreate-up-fail")

    class _FakeRuntime:
        def __init__(self, ws, reg):
            self.down_calls = 0

        def is_running(self, iid):
            return True

        def stop(self, iid):
            pass

        def up(self, iid, *, force_recreate=False):
            raise DockerError("compose up 爆炸")

        def down(self, iid):
            self.down_calls += 1

        def container_id(self, iid):
            return None

        def image_id(self, iid):
            return "sha256:old"

    fake = _FakeRuntime(workspace, registry)
    _patch_fake_runtime(monkeypatch, hosting, fake)

    with pytest.raises(DockerError, match="compose up 爆炸"):
        hosting.recreate_container_runtime(workspace, config, registry, "recreate-up-fail")

    assert fake.down_calls == 1
    reloaded = InstanceManifest.load(workspace.app_manifest_path("recreate-up-fail"))
    assert reloaded.status == Status.FAILED
    assert reloaded.lastError and "compose up 爆炸" in reloaded.lastError
    assert reloaded.container is not None
    assert reloaded.container.containerId is None
    assert reloaded.container.imageId is None
    row = registry.get_instance("recreate-up-fail")
    assert row is not None and row["status"] == Status.FAILED.value
    events = registry.list_events("recreate-up-fail")
    assert any("runtime recreate 失败已回滚" in e["message"] for e in events)


def test_recreate_container_runtime_probe_failure_rolls_back(
    workspace, registry, config, monkeypatch
) -> None:
    """BUG-589：mandatory 探针失败 → 清理新容器、写 FAILED 落盘、记录诊断事件。"""
    from local_webpage_access import hosting
    from local_webpage_access.errors import HostingError

    _setup_recreate_instance(workspace, registry, "recreate-probe-fail")

    class _FakeRuntime:
        def __init__(self, ws, reg):
            self.down_calls = 0

        def is_running(self, iid):
            return True

        def stop(self, iid):
            pass

        def up(self, iid, *, force_recreate=False):
            pass

        def down(self, iid):
            self.down_calls += 1

        def container_id(self, iid):
            return "cid-new"

        def image_id(self, iid):
            return "sha256:old"

    fake = _FakeRuntime(workspace, registry)
    _patch_fake_runtime(monkeypatch, hosting, fake)
    monkeypatch.setattr(
        hosting,
        "_evaluate_container_verification",
        lambda *a, **k: {
            "overall_status": "failed",
            "liveness_passed": False,
            "error": "必选探针未通过",
        },
    )

    with pytest.raises(HostingError, match="必选探针未通过"):
        hosting.recreate_container_runtime(workspace, config, registry, "recreate-probe-fail")

    assert fake.down_calls == 1
    reloaded = InstanceManifest.load(workspace.app_manifest_path("recreate-probe-fail"))
    assert reloaded.status == Status.FAILED
    assert reloaded.lastError and "必选探针未通过" in reloaded.lastError
    assert reloaded.container is not None
    assert reloaded.container.containerId is None
    assert reloaded.container.imageId is None
    row = registry.get_instance("recreate-probe-fail")
    assert row is not None and row["status"] == Status.FAILED.value
    events = registry.list_events("recreate-probe-fail")
    assert any("runtime recreate 失败已回滚" in e["message"] for e in events)


# ---- BUG-586 / BUG-590：别名活验证回滚与相对资源探测 -----------------------


def _static_alias_manifest(
    instance_id: str, host_port: int, alias: str | None = None
) -> InstanceManifest:
    return InstanceManifest(
        id=instance_id,
        name=instance_id,
        version="1",
        kind=Kind.STATIC,
        stack=[],
        runtime=Runtime.SHARED_STATIC,
        servingMode=ServingMode.SHARED_STATIC,
        resourceProfile=ResourceProfile.SMALL,
        status=Status.RUNNING,
        desiredState=DesiredState.RUNNING,
        static=StaticConfig(
            hostPort=host_port,
            enabled=True,
            routeMode="name" if alias else "port",
            routeHost=alias,
        ),
    )


class _CaddyWritingFakeGW:
    """Caddy 替身：generate_alias_config 真实落盘片段，reload 为空操作。"""

    def __init__(self, ws, cfg):
        self.ws = ws

    def detect_backend(self):
        return "caddy"

    def is_enabled(self, iid):
        return True

    def generate_alias_config(self, iid, alias, hp, **kwargs):
        p = self.ws.app_alias_config(iid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# alias {alias}\n", encoding="utf-8")

    def reload_all(self):
        pass

    def remove_alias_config(self, iid):
        p = self.ws.app_alias_config(iid)
        if p.is_file():
            p.unlink()


def test_set_alias_live_verify_failure_rolls_back_to_previous_fragment(
    workspace, registry, config, monkeypatch
) -> None:
    """BUG-586：活验证失败回滚须恢复「变更前」片段，而不是刚写入的新片段。"""
    from local_webpage_access import path_alias

    iid = "alias-rollback"
    workspace.ensure_app_dirs(iid)
    (workspace.app_current(iid) / "index.html").write_text("<h1>x</h1>")
    manifest = _static_alias_manifest(iid, 21102, alias="old-alias")
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)

    fragment = workspace.app_alias_config(iid)
    fragment.parent.mkdir(parents=True, exist_ok=True)
    fragment.write_text("OLD_ALIAS\n", encoding="utf-8")

    monkeypatch.setattr(path_alias, "StaticGateway", _CaddyWritingFakeGW)
    monkeypatch.setattr(
        path_alias, "_fetch_entrypoint_html_for_alias_guard", lambda **kwargs: "<h1>x</h1>"
    )

    def boom(*args, **kwargs):
        raise RecognitionError("live verify failed")

    monkeypatch.setattr(path_alias, "verify_alias_live", boom)

    with pytest.raises(RecognitionError, match="live verify failed"):
        set_instance_path_alias(workspace, config, registry, iid, "new-alias")

    assert fragment.read_text(encoding="utf-8") == "OLD_ALIAS\n"
    reloaded = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert reloaded.static is not None
    assert reloaded.static.routeHost == "old-alias"
    site = registry.get_static_site(iid)
    assert site is not None
    assert site["route_host"] == "old-alias"


def test_deferred_alias_verify_failure_rolls_back_state(
    workspace, registry, config, monkeypatch
) -> None:
    """BUG-586：首次 start 的 deferred 活验证失败须回滚片段 + manifest + registry。"""
    from local_webpage_access import path_alias

    iid = "alias-deferred"
    workspace.ensure_app_dirs(iid)
    manifest = _static_alias_manifest(iid, 21103, alias="deferred-alias")
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)

    fragment = workspace.app_alias_config(iid)
    fragment.parent.mkdir(parents=True, exist_ok=True)
    fragment.write_text("# alias deferred-alias\n", encoding="utf-8")

    monkeypatch.setattr(path_alias, "StaticGateway", _CaddyWritingFakeGW)
    monkeypatch.setattr(
        path_alias, "_fetch_entrypoint_html_for_alias_guard", lambda **kwargs: None
    )

    def boom(*args, **kwargs):
        raise RecognitionError("deferred live verify failed")

    monkeypatch.setattr(path_alias, "verify_alias_live", boom)

    with pytest.raises(RecognitionError, match="deferred live verify failed"):
        path_alias.maybe_verify_alias_after_start(
            workspace, config, registry, iid, manifest
        )

    assert not fragment.exists()
    reloaded = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert reloaded.static is not None
    assert reloaded.static.routeMode == "port"
    assert reloaded.static.routeHost is None
    site = registry.get_static_site(iid)
    assert site is not None
    assert site["route_host"] is None


def test_collect_alias_live_probe_paths_includes_relative_script() -> None:
    """BUG-590：相对 script 资源解析为 /{alias}/… 参与活验证。"""
    from local_webpage_access.path_alias import _collect_alias_live_probe_paths

    html = '<script src="assets/app.js"></script>'
    assert _collect_alias_live_probe_paths(html, "demo") == ["/demo/assets/app.js"]
    html_dot = '<script src="./assets/app.js"></script>'
    assert _collect_alias_live_probe_paths(html_dot, "demo") == ["/demo/assets/app.js"]


def test_verify_alias_live_probes_relative_script_under_alias(config, monkeypatch) -> None:
    """BUG-590：HTML 含 <script src="assets/app.js"> 时活验证请求 /{alias}/assets/app.js。"""
    from local_webpage_access import path_alias

    probed: list[str] = []

    def fake_probe(url, *, timeout=3.0):
        probed.append(url)
        if url.endswith("/demo/"):
            return True, 200, "text/html", b"<!doctype html><html></html>"
        return True, 200, "application/javascript", b"console.log(1)"

    monkeypatch.setattr(path_alias, "_http_probe_alias_resource", fake_probe)
    config.staticGatewayPort = 8080
    verify_alias_live(
        config,
        "demo",
        entry_html='<script src="assets/app.js"></script>',
        instance_id="x",
    )
    assert "http://127.0.0.1:8080/demo/assets/app.js" in probed
