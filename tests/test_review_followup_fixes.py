"""#30 / DEV-132 / #31 后续修复的行为回归。"""

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner
from local_webpage_access.cli import app
from local_webpage_access.models import InstanceManifest, Kind, Runtime, Status, StaticConfig


def manifest(**kw):
    return InstanceManifest(
        id="demo",
        name="demo",
        kind=Kind.STATIC,
        runtime=Runtime.SHARED_STATIC,
        version="1",
        servingMode="shared-static",
        **kw,
    )


@pytest.mark.parametrize(
    "args", [["missing.zip"], ["--from-dir", "/missing"], ["--from-git", "https://github.com/a/b"]]
)
def test_new_import_dry_run_refused_before_workspace(args, monkeypatch):
    def forbidden():
        raise AssertionError("不应打开工作区或触发导入")

    monkeypatch.setattr("local_webpage_access.cli.importing.open_workspace_registry", forbidden)
    result = CliRunner().invoke(app, ["import", *args, "--dry-run"])
    assert result.exit_code == 2, result.output
    assert "--update" in result.output


@pytest.mark.parametrize(
    "env", [{"X": "a\nb"}, {"X": "a\rb"}, {"X": "a\0b"}, {"A=B": "x"}, {"": "x"}, {"A\nB": "x"}]
)
def test_invalid_build_env_rejected(env):
    with pytest.raises(ValidationError):
        manifest(buildEnv=env)


def test_build_env_follows_alias_without_mutating_explicit_settings():
    from local_webpage_access.instance_settings import effective_build_env

    m = manifest(
        buildEnv={"CUSTOM": "yes"},
        buildBaseFromAlias=True,
        static=StaticConfig(routeMode="name", routeHost="first"),
    )
    assert effective_build_env(m)["VITE_BASE"] == "/first/"
    m.static.routeHost = "second"
    assert effective_build_env(m)["VITE_BASE"] == "/second/"
    m.static.routeMode = "port"
    assert effective_build_env(m)["VITE_BASE"] == "/"
    assert m.buildEnv == {"CUSTOM": "yes"}


def test_configure_updates_and_preserves_settings(workspace, registry):
    from local_webpage_access.instance_settings import update_instance_settings

    workspace.ensure_app_dirs("demo")
    m = manifest(buildEnv={"OLD": "keep"})
    m.save(workspace.app_manifest_path("demo"))
    registry.upsert_from_manifest(m)
    updated = update_instance_settings(
        workspace,
        registry,
        "demo",
        {"buildEnv": {"NEW": "value"}, "redundancyAcknowledged": True, "buildBaseFromAlias": True},
    )
    assert updated.buildEnv == {"NEW": "value"}
    assert InstanceManifest.load(workspace.app_manifest_path("demo")).redundancyAcknowledged


@pytest.mark.parametrize(
    "state", ["running", "building", "queued", "cancelling", "verifying", "degraded"]
)
def test_redundant_running_guard_cannot_be_overridden(
    workspace, registry, config, monkeypatch, state
):
    from local_webpage_access.lifecycle import remove_redundant

    for iid, date in [("first", "2026-01-01"), ("demo", "2026-01-02")]:
        workspace.ensure_app_dirs(iid)
        m = manifest(status=Status(state) if iid == "demo" else Status.STOPPED)
        m.id = iid
        m.createdAt = date
        m.save(workspace.app_manifest_path(iid))
        workspace.app_original_zip(iid).write_bytes(b"same")
        registry.upsert_from_manifest(m)
    calls = []
    monkeypatch.setattr(
        "local_webpage_access.lifecycle.remove_instance", lambda *a, **k: calls.append(a)
    )
    out = remove_redundant(workspace, config, registry, allow_config_loss=True, force=True)
    assert out["removed"] == []
    assert out["skipped"][0]["id"] == "demo"
    assert calls == []


def test_acknowledged_instance_not_redundant(workspace, registry):
    from local_webpage_access.lifecycle import list_redundant_instances

    for iid, date in [("first", "2026-01-01"), ("demo", "2026-01-02")]:
        workspace.ensure_app_dirs(iid)
        m = manifest(redundancyAcknowledged=iid == "demo")
        m.id = iid
        m.createdAt = date
        m.save(workspace.app_manifest_path(iid))
        workspace.app_original_zip(iid).write_bytes(b"same")
        registry.upsert_from_manifest(m)
    assert list_redundant_instances(workspace, registry) == []


def test_browser_redundant_policy():
    import subprocess

    subprocess.run(
        [
            "node",
            "-e",
            """
      const assert = require('assert');
      const h = require('./src/local_webpage_access/manager_static/helpers.js');
      assert.deepEqual(h.redundantRemovalReasons({skipReasons:['运行中'],configLossReasons:['别名']},true),['运行中']);
      assert.deepEqual(h.redundantRemovalReasons({configLossReasons:['别名']},false),['别名']);
      assert.deepEqual(h.redundantRemovalReasons({configLossReasons:['别名']},true),[]);
    """,
        ],
        check=True,
    )


def test_invalid_settings_do_not_change_manifest(workspace, registry):
    from local_webpage_access.instance_settings import update_instance_settings

    workspace.ensure_app_dirs("demo")
    m = manifest()
    m.save(workspace.app_manifest_path("demo"))
    registry.upsert_from_manifest(m)
    before = workspace.app_manifest_path("demo").read_bytes()
    for changes in [
        {"buildEnv": {"X": "a\nb"}},
        {"redundancyAcknowledged": "false"},
        {"unknown": True},
    ]:
        with pytest.raises(ValueError):
            update_instance_settings(workspace, registry, "demo", changes)
        assert workspace.app_manifest_path("demo").read_bytes() == before


def test_container_build_env_refused_but_ack_allowed(workspace, registry):
    from local_webpage_access.instance_settings import update_instance_settings

    workspace.ensure_app_dirs("demo")
    m = manifest()
    from local_webpage_access.models import ContainerConfig, ServingMode

    m.runtime = Runtime.DOCKER_COMPOSE
    m.servingMode = ServingMode.CONTAINER
    m.container = ContainerConfig(
        projectName="demo",
        internalPort=8000,
        composePath="docker/compose.yml",
        dockerfilePath="docker/Dockerfile",
    )
    m.save(workspace.app_manifest_path("demo"))
    registry.upsert_from_manifest(m)
    with pytest.raises(ValueError, match="容器"):
        update_instance_settings(workspace, registry, "demo", {"buildEnv": {"X": "yes"}})
    assert update_instance_settings(
        workspace, registry, "demo", {"redundancyAcknowledged": True}
    ).redundancyAcknowledged


def test_remove_redundant_rechecks_guard_under_lock(workspace, registry, config, monkeypatch):
    from local_webpage_access import lifecycle

    for iid, date in [("first", "2026-01-01"), ("demo", "2026-01-02")]:
        workspace.ensure_app_dirs(iid)
        m = manifest(status=Status.STOPPED)
        m.id = iid
        m.createdAt = date
        m.save(workspace.app_manifest_path(iid))
        workspace.app_original_zip(iid).write_bytes(b"same")
        registry.upsert_from_manifest(m)
    original = lifecycle.remove_instance

    def concurrent_edit(*args, **kwargs):
        m = InstanceManifest.load(workspace.app_manifest_path("demo"))
        m.buildEnv = {"X": "new"}
        m.save(workspace.app_manifest_path("demo"))
        return original(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "remove_instance", concurrent_edit)
    outcome = lifecycle.remove_redundant(workspace, config, registry)
    assert outcome["removed"] == []
    assert "buildEnv" in outcome["skipped"][0]["reasons"][0]
    assert registry.instance_exists("demo")


def test_configure_cli_persists_and_shows_settings(workspace, registry, config, monkeypatch):
    from local_webpage_access.registry import Registry

    workspace.ensure_app_dirs("demo")
    m = manifest()
    m.save(workspace.app_manifest_path("demo"))
    registry.upsert_from_manifest(m)

    def open_env():
        reg = Registry(workspace.db_path)
        reg.open()
        return workspace, config, reg

    monkeypatch.setattr("local_webpage_access.cli.configure.open_workspace_registry", open_env)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "configure",
            "demo",
            "--build-env",
            "X=a=b",
            "--follow-alias-base",
            "--acknowledge-redundancy",
        ],
    )
    assert result.exit_code == 0, result.output
    saved = InstanceManifest.load(workspace.app_manifest_path("demo"))
    assert saved.buildEnv == {"X": "a=b"}
    assert saved.buildBaseFromAlias and saved.redundancyAcknowledged
    result = runner.invoke(
        app,
        [
            "configure",
            "demo",
            "--clear-build-env",
            "--no-follow-alias-base",
            "--no-acknowledge-redundancy",
        ],
    )
    assert result.exit_code == 0, result.output
    saved = InstanceManifest.load(workspace.app_manifest_path("demo"))
    assert saved.buildEnv is None
    assert not saved.buildBaseFromAlias and not saved.redundancyAcknowledged
