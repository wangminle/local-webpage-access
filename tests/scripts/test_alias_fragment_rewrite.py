"""存量别名片段重写测试（PLN-052 S1-01 / N1）。

旧版片段 ``redir /<alias>/ permanent`` 被 Caddy 解析成路径匹配器，无斜杠访问返回 200 空体。
``rewrite_alias_fragments`` 负责把磁盘上的旧片段改写为当前模板，并在网关在线时统一 reload。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_webpage_access.config import Config
from local_webpage_access.gateway_switch import rewrite_alias_fragments
from local_webpage_access.importer import build_manifest_from_detection
from local_webpage_access.models import (
    ContainerConfig,
    Kind,
    ResourceProfile,
    Runtime,
    ServingMode,
)
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry
from local_webpage_access.scanner import DetectionResult
from local_webpage_access.static_gateway import StaticGateway
from tests.scripts._helpers import make_container_manifest

STALE_FRAGMENT = (
    "handle /voiceprint {\n"
    "\tredir /voiceprint/ permanent\n"
    "}\n"
)


def _seed_alias_instance(
    workspace: Workspace, registry: Registry, iid: str, alias: str, host_port: int = 18000
) -> None:
    workspace.ensure_app_dirs(iid)
    (workspace.app_current(iid) / "index.html").write_text("<html><body>hi</body></html>")
    detection = DetectionResult(
        kind=Kind.STATIC,
        runtime=Runtime.SHARED_STATIC,
        servingMode=ServingMode.SHARED_STATIC,
        resourceProfile=ResourceProfile.TINY,
        form="static",
        confidence="high",
    )
    manifest = build_manifest_from_detection(
        instance_id=iid,
        display_name="Demo",
        detection=detection,
        workspace=workspace,
        path_alias=alias,
    )
    manifest.static.hostPort = host_port
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)


@pytest.fixture()
def caddy_config(workspace_root: Path) -> Config:
    return Config(staticGateway="caddy", staticGatewayPort=8080)


def test_rewrite_replaces_stale_redir_fragment(
    workspace: Workspace, registry: Registry, caddy_config: Config
) -> None:
    _seed_alias_instance(workspace, registry, "demo", "voiceprint")
    fragment = workspace.app_alias_config("demo")
    fragment.parent.mkdir(parents=True, exist_ok=True)
    fragment.write_text(
        "handle_path /voiceprint/* {\n\treverse_proxy 127.0.0.1:18000\n}\n" + STALE_FRAGMENT,
        encoding="utf-8",
    )

    rewritten = rewrite_alias_fragments(workspace, registry, caddy_config, reload_if_running=False)

    assert rewritten == ["demo"]
    text = fragment.read_text(encoding="utf-8")
    assert "redir * /voiceprint/ permanent" in text
    assert "redir /voiceprint/ permanent" not in text


def test_rewrite_leaves_current_fragment_untouched(
    workspace: Workspace, registry: Registry, caddy_config: Config
) -> None:
    _seed_alias_instance(workspace, registry, "demo", "voiceprint")
    gateway = StaticGateway(workspace, caddy_config)
    fragment = gateway.generate_alias_config("demo", "voiceprint", 18000)
    before = fragment.read_text(encoding="utf-8")

    rewritten = rewrite_alias_fragments(workspace, registry, caddy_config, reload_if_running=False)

    assert rewritten == []
    assert fragment.read_text(encoding="utf-8") == before


def test_rewrite_does_not_recreate_missing_fragment(
    workspace: Workspace, registry: Registry, caddy_config: Config
) -> None:
    _seed_alias_instance(workspace, registry, "demo", "voiceprint")
    fragment = workspace.app_alias_config("demo")
    assert not fragment.exists()

    rewritten = rewrite_alias_fragments(workspace, registry, caddy_config, reload_if_running=False)

    assert rewritten == []
    assert not fragment.exists()


def test_rewrite_reloads_once_only_when_gateway_running(
    workspace: Workspace,
    registry: Registry,
    caddy_config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_alias_instance(workspace, registry, "demo", "voiceprint")
    _seed_alias_instance(workspace, registry, "other", "other-app", host_port=18001)

    def write_stale() -> None:
        for iid, alias in (("demo", "voiceprint"), ("other", "other-app")):
            path = workspace.app_alias_config(iid)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(STALE_FRAGMENT.replace("voiceprint", alias), encoding="utf-8")

    reloads: list[int] = []
    monkeypatch.setattr(StaticGateway, "reload_all", lambda self: reloads.append(1))

    write_stale()
    monkeypatch.setattr(StaticGateway, "_admin_alive", lambda self, **kw: False)
    assert sorted(rewrite_alias_fragments(workspace, registry, caddy_config)) == ["demo", "other"]
    assert reloads == []

    write_stale()
    monkeypatch.setattr(StaticGateway, "_admin_alive", lambda self, **kw: True)
    assert sorted(rewrite_alias_fragments(workspace, registry, caddy_config)) == ["demo", "other"]
    assert reloads == [1]


# ---- BUG-762 / F01：容器实例存量别名片段同样进入重写 -------------------------


def _seed_container_alias_instance(
    workspace: Workspace, registry: Registry, iid: str, alias: str, host_port: int = 18000
) -> None:
    workspace.ensure_app_dirs(iid)
    manifest = make_container_manifest(
        iid,
        container=ContainerConfig(
            projectName=f"lwa-{iid}",
            internalPort=8000,
            hostPort=host_port,
            composePath="docker/compose.yaml",
            dockerfilePath="docker/Dockerfile",
            routeMode="name",
            routeHost=alias,
        ),
    )
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)


CONTAINER_STALE_FRAGMENT = (
    "handle_path /bookshelf/* {\n"
    "\treverse_proxy 127.0.0.1:18000\n}\n"
    "handle /bookshelf {\n"
    "\tredir /bookshelf/ permanent\n}\n"
)


def test_rewrite_replaces_stale_container_fragment(
    workspace: Workspace, registry: Registry, caddy_config: Config
) -> None:
    _seed_container_alias_instance(workspace, registry, "bookshelf", "bookshelf")
    fragment = workspace.app_alias_config("bookshelf")
    fragment.parent.mkdir(parents=True, exist_ok=True)
    fragment.write_text(CONTAINER_STALE_FRAGMENT, encoding="utf-8")

    rewritten = rewrite_alias_fragments(workspace, registry, caddy_config, reload_if_running=False)

    assert rewritten == ["bookshelf"]
    text = fragment.read_text(encoding="utf-8")
    assert "redir * /bookshelf/ permanent" in text
    assert "redir /bookshelf/ permanent" not in text
    assert "reverse_proxy 127.0.0.1:18000" in text


def test_rewrite_leaves_current_container_fragment_untouched(
    workspace: Workspace, registry: Registry, caddy_config: Config
) -> None:
    _seed_container_alias_instance(workspace, registry, "bookshelf", "bookshelf")
    gateway = StaticGateway(workspace, caddy_config)
    fragment = gateway.generate_alias_config("bookshelf", "bookshelf", 18000)
    before = fragment.read_text(encoding="utf-8")

    rewritten = rewrite_alias_fragments(workspace, registry, caddy_config, reload_if_running=False)

    assert rewritten == []
    assert fragment.read_text(encoding="utf-8") == before


def test_rewrite_does_not_recreate_missing_container_fragment(
    workspace: Workspace, registry: Registry, caddy_config: Config
) -> None:
    _seed_container_alias_instance(workspace, registry, "bookshelf", "bookshelf")
    fragment = workspace.app_alias_config("bookshelf")
    assert not fragment.exists()

    rewritten = rewrite_alias_fragments(workspace, registry, caddy_config, reload_if_running=False)

    assert rewritten == []
    assert not fragment.exists()


def test_rewrite_static_and_container_share_single_reload(
    workspace: Workspace,
    registry: Registry,
    caddy_config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_alias_instance(workspace, registry, "demo", "voiceprint")
    _seed_container_alias_instance(workspace, registry, "bookshelf", "bookshelf", host_port=18001)
    workspace.app_alias_config("demo").parent.mkdir(parents=True, exist_ok=True)
    workspace.app_alias_config("demo").write_text(STALE_FRAGMENT, encoding="utf-8")
    workspace.app_alias_config("bookshelf").write_text(
        CONTAINER_STALE_FRAGMENT.replace("18000", "18001"), encoding="utf-8"
    )

    reloads: list[int] = []
    monkeypatch.setattr(StaticGateway, "reload_all", lambda self: reloads.append(1))
    monkeypatch.setattr(StaticGateway, "_admin_alive", lambda self, **kw: True)

    rewritten = rewrite_alias_fragments(workspace, registry, caddy_config)

    assert sorted(rewritten) == ["bookshelf", "demo"]
    assert reloads == [1]
    assert "redir * /bookshelf/ permanent" in workspace.app_alias_config("bookshelf").read_text(
        encoding="utf-8"
    )
    assert "redir * /voiceprint/ permanent" in workspace.app_alias_config("demo").read_text(
        encoding="utf-8"
    )
