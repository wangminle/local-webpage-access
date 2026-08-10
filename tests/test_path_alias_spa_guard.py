"""设路径别名时拦截 IMP-023（入口 HTML 含绝对路径资源）。"""

from __future__ import annotations

import pytest

from local_webpage_access.errors import RecognitionError
from local_webpage_access.models import DesiredState, InstanceManifest, Kind, Runtime, Status
from local_webpage_access.path_alias import (
    reject_alias_if_absolute_spa_assets,
    set_instance_path_alias,
)


def test_reject_alias_if_absolute_spa_assets_raises() -> None:
    html = (
        "<!DOCTYPE html><html><head>"
        '<script src="/assets/index-abc.js"></script>'
        '<link href="/assets/index-abc.css" rel="stylesheet">'
        "</head><body></body></html>"
    )
    with pytest.raises(RecognitionError, match="绝对路径资源") as ei:
        reject_alias_if_absolute_spa_assets(
            html=html, alias="home-bookshelf", instance_id="backend"
        )
    assert "IMP-023" in str(ei.value) or "白屏" in str(ei.value)
    assert "/assets/" in str(ei.value)


def test_reject_alias_if_absolute_spa_assets_allows_relative() -> None:
    html = (
        "<!DOCTYPE html><html><head>"
        '<script src="./assets/index-abc.js"></script>'
        '<link href="assets/index-abc.css" rel="stylesheet">'
        "</head><body></body></html>"
    )
    reject_alias_if_absolute_spa_assets(
        html=html, alias="ok-site", instance_id="demo"
    )


def test_reject_alias_if_absolute_spa_assets_skips_empty_html() -> None:
    reject_alias_if_absolute_spa_assets(html=None, alias="x", instance_id="y")
    reject_alias_if_absolute_spa_assets(html="", alias="x", instance_id="y")


def test_set_alias_rejects_when_entrypoint_has_absolute_assets(
    workspace, registry, config, monkeypatch
) -> None:
    """shared-static 设别名前探活入口 HTML：含 /assets/… 则失败，不写别名、不 reload。"""
    from local_webpage_access import path_alias
    from local_webpage_access.models import (
        ResourceProfile,
        ServingMode,
        StaticConfig,
    )

    workspace.ensure_app_dirs("spa")
    (workspace.app_current("spa") / "index.html").write_text(
        '<script src="/assets/app.js"></script>'
    )
    manifest = InstanceManifest(
        id="spa",
        name="spa",
        version="1",
        kind=Kind.STATIC,
        stack=[],
        runtime=Runtime.SHARED_STATIC,
        servingMode=ServingMode.SHARED_STATIC,
        resourceProfile=ResourceProfile.SMALL,
        status=Status.RUNNING,
        desiredState=DesiredState.RUNNING,
        static=StaticConfig(
            hostPort=21099,
            enabled=True,
        ),
    )
    manifest.save(workspace.app_manifest_path("spa"))
    registry.upsert_from_manifest(manifest)

    calls = {"gen": 0, "reload": 0}

    class _FakeGW:
        def __init__(self, ws, cfg):
            self.ws = ws

        def detect_backend(self):
            return "caddy"

        def is_enabled(self, iid):
            return True

        def generate_alias_config(self, iid, alias, hp, **kwargs):
            calls["gen"] += 1

        def reload_all(self):
            calls["reload"] += 1

        def remove_alias_config(self, iid):
            pass

    monkeypatch.setattr(path_alias, "StaticGateway", _FakeGW)
    monkeypatch.setattr(
        path_alias,
        "_fetch_entrypoint_html_for_alias_guard",
        lambda **kwargs: (
            '<script src="/assets/app.js"></script>'
            '<link href="/assets/app.css" rel="stylesheet">'
        ),
    )

    with pytest.raises(RecognitionError, match="绝对路径资源"):
        set_instance_path_alias(workspace, config, registry, "spa", "home-bookshelf")

    assert calls["gen"] == 0
    assert calls["reload"] == 0
    reloaded = InstanceManifest.load(workspace.app_manifest_path("spa"))
    assert reloaded.static is not None
    assert reloaded.static.routeHost is None


def test_set_alias_blocks_absolute_assets_for_docker_compose(
    workspace, registry, config, monkeypatch
) -> None:
    """IMP-055：docker-compose 实例含绝对路径资源时同样硬拦截（撤销 BUG-465 豁免）。"""
    from local_webpage_access import path_alias
    from local_webpage_access.models import (
        ContainerConfig,
        ResourceProfile,
        ServingMode,
    )

    workspace.ensure_app_dirs("spa-dc")
    (workspace.app_current("spa-dc") / "main.py").write_text("app=None")
    manifest = InstanceManifest(
        id="spa-dc",
        name="spa-dc",
        version="1",
        kind=Kind.PYTHON,
        stack=["fastapi"],
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.SMALL,
        status=Status.RUNNING,
        desiredState=DesiredState.RUNNING,
        container=ContainerConfig(
            projectName="lwa-spa-dc",
            composePath="docker/compose.yaml",
            dockerfilePath="docker/Dockerfile",
            hostPort=21098,
            internalPort=8000,
            containerId="cid-spa-dc",
        ),
    )
    manifest.save(workspace.app_manifest_path("spa-dc"))
    registry.upsert_from_manifest(manifest)

    calls = {"gen": 0, "reload": 0}

    class _FakeGW:
        def __init__(self, ws, cfg):
            self.ws = ws

        def detect_backend(self):
            return "caddy"

        def is_enabled(self, iid):
            return False

        def generate_alias_config(self, iid, alias, hp, *, runtime=None, **kw):
            calls["gen"] += 1

        def reload_all(self):
            calls["reload"] += 1

        def remove_alias_config(self, iid):
            pass

    monkeypatch.setattr(path_alias, "StaticGateway", _FakeGW)
    monkeypatch.setattr(
        path_alias,
        "_fetch_entrypoint_html_for_alias_guard",
        lambda **kwargs: (
            '<script src="/assets/app.js"></script>'
            '<link href="/assets/app.css" rel="stylesheet">'
        ),
    )

    # IMP-055：docker-compose 同样被硬拦截，不再豁免
    with pytest.raises(RecognitionError, match="绝对路径资源"):
        set_instance_path_alias(workspace, config, registry, "spa-dc", "my-app")

    assert calls["gen"] == 0
    assert calls["reload"] == 0
