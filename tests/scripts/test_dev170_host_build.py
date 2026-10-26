"""DEV-170 / issue #61 阶段二：宿主预构建 hostBuild/preSync。

覆盖：
- manifest 字段校验：换行/NUL 拒绝、hostBuild 与 preSync 互斥、仅容器实例；
- CLI ``configure --host-build/--pre-sync/--clear-host-build`` 落盘；
- 执行器：cwd=current/、buildEnv + 别名 base + 构建代理 + 包缓存注入、
  超时与失败语义；
- 失败保旧：命令失败发生在 host_container 之前，旧服务状态不变；
- 成功路径产物进入 current/，随后才调用 host_container；
- ``rebuild --sync`` 显式开启，普通 rebuild 不执行用户宿主命令。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from local_webpage_access import host_build as host_build_module
from local_webpage_access.cli import app
from local_webpage_access.config import BuildProxy
from local_webpage_access.errors import BuildError
from local_webpage_access.host_build import (
    configured_host_build,
    run_host_build,
)
from local_webpage_access.instance_settings import update_instance_settings
from local_webpage_access.lifecycle import rebuild_instance
from local_webpage_access.models import (
    ContainerConfig,
    DesiredState,
    InstanceManifest,
    Kind,
    ResourceProfile,
    Runtime,
    ServingMode,
    Status,
    StaticConfig,
)
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry


def _container_manifest(**overrides) -> InstanceManifest:
    kwargs: dict = dict(
        id="demo",
        name="demo",
        version="1",
        kind=Kind.PYTHON,
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.SMALL,
        container=ContainerConfig(
            projectName="demo",
            internalPort=8000,
            composePath="docker/compose.yaml",
            dockerfilePath="docker/Dockerfile",
        ),
        desiredState=DesiredState.STOPPED,
        status=Status.STOPPED,
    )
    kwargs.update(overrides)
    return InstanceManifest(**kwargs)


def _seed(workspace: Workspace, registry: Registry, manifest: InstanceManifest) -> None:
    workspace.ensure_app_dirs(manifest.id)
    current = workspace.app_current(manifest.id)
    current.mkdir(parents=True, exist_ok=True)
    (current / "manage.py").write_text("app=None\n", encoding="utf-8")
    manifest.appPath = str(current)
    manifest.save(workspace.app_manifest_path(manifest.id))
    registry.upsert_from_manifest(manifest)


def _cli_env(monkeypatch, workspace, config) -> None:
    def open_env():
        reg = Registry(workspace.db_path)
        reg.open()
        return workspace, config, reg

    monkeypatch.setattr("local_webpage_access.cli.configure.open_workspace_registry", open_env)


# ---- manifest 字段 ----------------------------------------------------------


@pytest.mark.parametrize("value", ["npm run build\necho pwned", "npm run build\r", "x\0y"])
def test_host_build_rejects_newline_and_nul(value: str) -> None:
    with pytest.raises(ValidationError):
        _container_manifest(hostBuild=value)
    with pytest.raises(ValidationError):
        _container_manifest(preSync=value)


def test_host_build_and_pre_sync_are_mutually_exclusive() -> None:
    with pytest.raises(ValidationError, match="只能配置一个"):
        _container_manifest(hostBuild="npm run build", preSync="npm run build")


def test_host_build_only_for_container_runtime() -> None:
    with pytest.raises(ValidationError, match="docker-compose"):
        InstanceManifest(
            id="web",
            name="web",
            version="1",
            kind=Kind.STATIC,
            runtime=Runtime.SHARED_STATIC,
            servingMode=ServingMode.SHARED_STATIC,
            static=StaticConfig(hostPort=21001),
            hostBuild="npm run build",
        )


def test_blank_host_build_normalizes_to_none() -> None:
    assert _container_manifest(hostBuild="   ").hostBuild is None
    assert configured_host_build(_container_manifest(hostBuild="  npm run build  ")) == (
        "npm run build"
    )


# ---- configure CLI ----------------------------------------------------------


def test_configure_cli_sets_and_clears_host_build(
    workspace, registry, config, monkeypatch
) -> None:
    _seed(workspace, registry, _container_manifest())
    _cli_env(monkeypatch, workspace, config)
    runner = CliRunner()

    result = runner.invoke(
        app, ["configure", "demo", "--host-build", "cd frontend && npm run build"]
    )
    assert result.exit_code == 0, result.output
    saved = InstanceManifest.load(workspace.app_manifest_path("demo"))
    assert saved.hostBuild == "cd frontend && npm run build"
    assert saved.preSync is None
    assert "hostBuild" in result.output

    result = runner.invoke(app, ["configure", "demo", "--pre-sync", "make frontend"])
    assert result.exit_code == 0, result.output
    saved = InstanceManifest.load(workspace.app_manifest_path("demo"))
    assert saved.hostBuild is None
    assert saved.preSync == "make frontend"

    result = runner.invoke(app, ["configure", "demo", "--clear-host-build"])
    assert result.exit_code == 0, result.output
    saved = InstanceManifest.load(workspace.app_manifest_path("demo"))
    assert saved.hostBuild is None and saved.preSync is None

    result = runner.invoke(
        app, ["configure", "demo", "--host-build", "a", "--pre-sync", "b"]
    )
    assert result.exit_code != 0
    assert "互斥" in result.output


def test_configure_cli_rejects_host_build_on_static(
    workspace, registry, config, monkeypatch
) -> None:
    static = InstanceManifest(
        id="web",
        name="web",
        version="1",
        kind=Kind.STATIC,
        runtime=Runtime.SHARED_STATIC,
        servingMode=ServingMode.SHARED_STATIC,
        static=StaticConfig(hostPort=21001),
        status=Status.STOPPED,
    )
    workspace.ensure_app_dirs("web")
    static.save(workspace.app_manifest_path("web"))
    registry.upsert_from_manifest(static)
    _cli_env(monkeypatch, workspace, config)

    result = CliRunner().invoke(app, ["configure", "web", "--host-build", "npm run build"])
    assert result.exit_code != 0
    assert "docker-compose" in result.output


def test_update_instance_settings_accepts_host_build(workspace, registry) -> None:
    _seed(workspace, registry, _container_manifest())
    updated = update_instance_settings(
        workspace, registry, "demo", {"hostBuild": "npm run build"}
    )
    assert updated.hostBuild == "npm run build"
    cleared = update_instance_settings(
        workspace, registry, "demo", {"hostBuild": None, "preSync": None}
    )
    assert cleared.hostBuild is None and cleared.preSync is None


# ---- 执行器 -----------------------------------------------------------------


def test_run_host_build_uses_current_root_env_and_proxy(
    workspace, registry, config, monkeypatch
) -> None:
    proxy_config = config.model_copy(
        update={"buildProxy": BuildProxy(enabled=True, url="http://127.0.0.1:7890")}
    )
    monkeypatch.setattr(
        host_build_module,
        "config_with_latest_build_proxy",
        lambda _workspace, cfg: cfg,
    )
    manifest = _container_manifest(
        hostBuild=(
            'printf "%s\\n" "$VITE_BASE" "$BASE_URL" "$PUBLIC_URL" '
            '"$HTTP_PROXY" "$npm_config_cache" > env.txt'
        ),
        buildBaseFromAlias=True,
    )
    manifest.container.routeMode = "name"
    manifest.container.routeHost = "funasr"
    _seed(workspace, registry, manifest)

    result = run_host_build(workspace, proxy_config, registry, manifest)

    assert result is not None
    assert result.cwd == workspace.app_current("demo")
    current = workspace.app_current("demo")
    assert (current / "env.txt").is_file()
    lines = (current / "env.txt").read_text(encoding="utf-8").splitlines()
    assert lines[:3] == ["/funasr/", "/funasr/", "/funasr/"]
    assert lines[3] == "http://127.0.0.1:7890"
    assert "pkg-cache" in lines[4]
    assert (workspace.app_logs("demo") / "build.log").is_file()

    events = [e for e in registry.list_events("demo") if e.get("event_type") == "host_build"]
    assert events
    assert any("宿主预构建完成" in e.get("message", "") for e in events)


def test_run_host_build_noop_when_not_configured(workspace, registry, config) -> None:
    manifest = _container_manifest()
    assert run_host_build(workspace, config, registry, manifest) is None


def test_run_host_build_failure_raises_with_log(
    workspace, registry, config
) -> None:
    manifest = _container_manifest(hostBuild="exit 9")
    _seed(workspace, registry, manifest)

    with pytest.raises(BuildError, match="exit 9"):
        run_host_build(workspace, config, registry, manifest)
    events = [e for e in registry.list_events("demo") if e.get("event_type") == "host_build_failed"]
    assert events


def test_run_host_build_timeout(
    workspace, registry, config, monkeypatch
) -> None:
    manifest = _container_manifest(hostBuild="sleep 2")
    _seed(workspace, registry, manifest)
    monkeypatch.setattr(host_build_module, "host_build_timeout", lambda *a, **k: 1)

    with pytest.raises(BuildError, match="命令超时"):
        run_host_build(workspace, config, registry, manifest)


def test_run_host_build_requires_current_dir(workspace, registry, config) -> None:
    manifest = _container_manifest(id="missing", hostBuild="echo ok")
    workspace.ensure_app_dirs("missing")
    # app_current 目录被移除后，执行器必须明确拒绝而不是在别处跑命令。
    current = workspace.app_current("missing")
    if current.exists():
        current.rmdir()
    with pytest.raises(BuildError, match="缺少源码目录"):
        run_host_build(workspace, config, registry, manifest)


# ---- rebuild 集成 -----------------------------------------------------------


def test_rebuild_with_host_build_runs_before_host_container(
    workspace, registry, config, monkeypatch
) -> None:
    seen: dict = {}

    def fake_host_container(ws, cfg, reg, iid, **kw):
        current = ws.app_current(iid)
        seen["artifact"] = (current / "frontend-built.txt").read_text(encoding="utf-8")
        manifest = InstanceManifest.load(ws.app_manifest_path(iid))
        manifest.status = Status.RUNNING
        manifest.save(ws.app_manifest_path(iid))
        return manifest

    monkeypatch.setattr("local_webpage_access.hosting.host_container", fake_host_container)
    manifest = _container_manifest(
        status=Status.RUNNING,
        hostBuild="echo fresh-frontend > frontend-built.txt",
    )
    _seed(workspace, registry, manifest)

    rebuilt = rebuild_instance(
        workspace, config, registry, "demo", host_build=True
    )

    assert rebuilt.status == Status.RUNNING
    assert seen["artifact"].strip() == "fresh-frontend"
    assert (workspace.app_current("demo") / "frontend-built.txt").is_file()


def test_rebuild_failure_keeps_old_service_running(
    workspace, registry, config, monkeypatch
) -> None:
    calls: list[str] = []

    def fake_host_container(ws, cfg, reg, iid, **kw):
        calls.append(iid)
        raise AssertionError("宿主预构建失败时不应进入 host_container")

    monkeypatch.setattr("local_webpage_access.hosting.host_container", fake_host_container)
    manifest = _container_manifest(
        status=Status.RUNNING,
        hostBuild="echo broken && exit 3",
    )
    _seed(workspace, registry, manifest)
    registry.update_status("demo", Status.RUNNING.value)

    with pytest.raises(BuildError):
        rebuild_instance(workspace, config, registry, "demo", host_build=True)

    assert calls == []
    row = registry.get_instance("demo")
    assert row is not None and row["status"] == Status.RUNNING.value
    reloaded = InstanceManifest.load(workspace.app_manifest_path("demo"))
    assert reloaded.status == Status.RUNNING


def test_rebuild_without_flag_does_not_run_host_build(
    workspace, registry, config, monkeypatch
) -> None:
    calls: list[str] = []

    def fake_host_container(ws, cfg, reg, iid, **kw):
        calls.append(iid)
        manifest = InstanceManifest.load(ws.app_manifest_path(iid))
        manifest.status = Status.RUNNING
        manifest.save(ws.app_manifest_path(iid))
        return manifest

    monkeypatch.setattr("local_webpage_access.hosting.host_container", fake_host_container)
    manifest = _container_manifest(
        status=Status.RUNNING,
        hostBuild="touch should-not-exist.txt",
    )
    _seed(workspace, registry, manifest)

    rebuild_instance(workspace, config, registry, "demo")

    assert calls == ["demo"]
    assert not (workspace.app_current("demo") / "should-not-exist.txt").exists()


def test_rebuild_cli_only_enables_host_build_with_sync(
    workspace, config, monkeypatch
) -> None:
    seen: list[bool] = []

    def fake_rebuild(ws, cfg, reg, iid, **kwargs):
        seen.append(bool(kwargs.get("host_build")))
        return None

    monkeypatch.setattr("local_webpage_access.lifecycle.rebuild_instance", fake_rebuild)
    monkeypatch.setattr(
        "local_webpage_access.cli.lifecycle._sync_source_before_rebuild",
        lambda *a, **k: None,
    )

    def open_env():
        reg = Registry(workspace.db_path)
        reg.open()
        return workspace, config, reg

    monkeypatch.setattr("local_webpage_access.cli.lifecycle.open_workspace_registry", open_env)
    runner = CliRunner()

    result = runner.invoke(app, ["rebuild", "demo", "--sync"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["rebuild", "demo"])
    assert result.exit_code == 0, result.output
    assert seen == [True, False]