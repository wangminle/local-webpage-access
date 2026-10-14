"""S3-05 / DEV-161：lwa configure --extra-volume 与 settings 接口的 extraVolumes 写入。"""

import pytest
from typer.testing import CliRunner

from local_webpage_access.cli import app
from local_webpage_access.instance_settings import update_instance_settings
from local_webpage_access.models import (
    ContainerConfig,
    InstanceManifest,
    Kind,
    Runtime,
    ServingMode,
    Status,
    StaticConfig,
)
from local_webpage_access.registry import Registry


def _container_manifest(**kw) -> InstanceManifest:
    return InstanceManifest(
        id="demo",
        name="demo",
        kind=Kind.PYTHON,
        runtime=Runtime.DOCKER_COMPOSE,
        version="1",
        servingMode=ServingMode.CONTAINER,
        status=Status.STOPPED,
        container=ContainerConfig(
            projectName="demo",
            internalPort=8000,
            composePath="docker/compose.yaml",
            dockerfilePath="docker/Dockerfile",
        ),
        **kw,
    )


def _seed(workspace, registry, manifest: InstanceManifest) -> None:
    workspace.ensure_app_dirs("demo")
    manifest.save(workspace.app_manifest_path("demo"))
    registry.upsert_from_manifest(manifest)


def test_settings_persist_extra_volumes_into_container(workspace, registry):
    _seed(workspace, registry, _container_manifest())

    updated = update_instance_settings(
        workspace,
        registry,
        "demo",
        {"extraVolumes": ["/mnt/lwa-share:/mnt/share:ro"]},
    )

    assert updated.container.extraVolumes == ["/mnt/lwa-share:/mnt/share:ro"]
    saved = InstanceManifest.load(workspace.app_manifest_path("demo"))
    assert saved.container.extraVolumes == ["/mnt/lwa-share:/mnt/share:ro"]
    assert saved.container.composePath == "docker/compose.yaml"
    events = registry.list_events("demo", limit=5)
    assert any("extraVolumes" in e["message"] for e in events)


def test_settings_clear_extra_volumes(workspace, registry):
    m = _container_manifest()
    m.container.extraVolumes = ["/mnt/lwa-share:/mnt/share"]
    _seed(workspace, registry, m)

    updated = update_instance_settings(workspace, registry, "demo", {"extraVolumes": []})

    assert updated.container.extraVolumes == []


@pytest.mark.parametrize(
    "entry",
    [
        "relative/path:/mnt/x",
        "/mnt/lwa-share",
        "/mnt/lwa-share:mnt/x",
        "/mnt/lwa-share:/mnt/x:bogus",
    ],
)
def test_settings_reject_malformed_volume(workspace, registry, entry):
    _seed(workspace, registry, _container_manifest())

    with pytest.raises(ValueError):
        update_instance_settings(workspace, registry, "demo", {"extraVolumes": [entry]})

    saved = InstanceManifest.load(workspace.app_manifest_path("demo"))
    assert saved.container.extraVolumes == []


def test_settings_reject_sensitive_host_mount_by_audit(workspace, registry):
    _seed(workspace, registry, _container_manifest())

    with pytest.raises(ValueError, match="安全审计"):
        update_instance_settings(workspace, registry, "demo", {"extraVolumes": ["/etc:/mnt/etc"]})


def test_settings_reject_extra_volumes_for_static_instance(workspace, registry):
    m = InstanceManifest(
        id="demo",
        name="demo",
        kind=Kind.STATIC,
        runtime=Runtime.SHARED_STATIC,
        version="1",
        servingMode="shared-static",
        static=StaticConfig(routeMode="port"),
    )
    _seed(workspace, registry, m)

    with pytest.raises(ValueError, match="容器实例"):
        update_instance_settings(
            workspace, registry, "demo", {"extraVolumes": ["/mnt/lwa-share:/mnt/share"]}
        )


def test_settings_reject_non_string_list(workspace, registry):
    _seed(workspace, registry, _container_manifest())

    with pytest.raises(ValueError, match="字符串列表"):
        update_instance_settings(workspace, registry, "demo", {"extraVolumes": "/srv:/mnt"})


def _cli_env(monkeypatch, workspace, config):
    def open_env():
        reg = Registry(workspace.db_path)
        reg.open()
        return workspace, config, reg

    monkeypatch.setattr("local_webpage_access.cli.configure.open_workspace_registry", open_env)


def test_configure_cli_extra_volume_repeatable_and_clear(workspace, registry, config, monkeypatch):
    _seed(workspace, registry, _container_manifest())
    _cli_env(monkeypatch, workspace, config)
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "configure",
            "demo",
            "--extra-volume",
            "/mnt/lwa-a:/mnt/a:ro",
            "--extra-volume",
            "/mnt/lwa-b:/mnt/b",
        ],
    )
    assert result.exit_code == 0, result.output
    saved = InstanceManifest.load(workspace.app_manifest_path("demo"))
    assert saved.container.extraVolumes == ["/mnt/lwa-a:/mnt/a:ro", "/mnt/lwa-b:/mnt/b"]
    assert "lwa restart" in result.output

    result = runner.invoke(app, ["configure", "demo", "--clear-extra-volumes"])
    assert result.exit_code == 0, result.output
    saved = InstanceManifest.load(workspace.app_manifest_path("demo"))
    assert saved.container.extraVolumes == []


def test_configure_cli_rejects_conflicting_flags(workspace, registry, config, monkeypatch):
    _seed(workspace, registry, _container_manifest())
    _cli_env(monkeypatch, workspace, config)

    result = CliRunner().invoke(
        app,
        [
            "configure",
            "demo",
            "--extra-volume",
            "/mnt/lwa-a:/mnt/a",
            "--clear-extra-volumes",
        ],
    )
    assert result.exit_code == 2
    assert "互斥" in result.output


def test_configure_cli_rejects_sensitive_mount(workspace, registry, config, monkeypatch):
    _seed(workspace, registry, _container_manifest())
    _cli_env(monkeypatch, workspace, config)

    result = CliRunner().invoke(
        app, ["configure", "demo", "--extra-volume", "/etc:/mnt/etc"]
    )
    assert result.exit_code == 2
    saved = InstanceManifest.load(workspace.app_manifest_path("demo"))
    assert saved.container.extraVolumes == []
