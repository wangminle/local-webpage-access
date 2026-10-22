"""BUG-951 / issue #141：旧版产物拷贝 buildHook 与模板保护冲突时须告警，不得默默删后端 static。"""

from __future__ import annotations

import pytest

from local_webpage_access.errors import DestructiveBuildHooksError
from local_webpage_access.models import ContainerConfig, Kind, ServingMode, Status
from tests.scripts._helpers import make_container_manifest

_LEGACY_HOOK = (
    "rm -rf /app/static && mkdir -p /app/static && cp -a /app/frontend/dist/. /app/static/"
)


def _seed_python_container(workspace, registry, *, hooks: list[str], iid: str = "bookshelf"):
    workspace.ensure_app_dirs(iid)
    current = workspace.app_current(iid)
    backend = current / "backend"
    backend.mkdir(parents=True, exist_ok=True)
    (backend / "app.py").write_text("app=1\n", encoding="utf-8")
    (backend / "requirements.txt").write_text("flask\n", encoding="utf-8")
    (backend / "static").mkdir(exist_ok=True)
    (backend / "static" / "skills").mkdir(exist_ok=True)
    (backend / "static" / "skills" / "index.json").write_text("[]\n", encoding="utf-8")
    manifest = make_container_manifest(
        iid,
        kind=Kind.PYTHON,
        servingMode=ServingMode.CONTAINER,
        status=Status.STOPPED,
        sourceSubdir="backend",
        buildHooks=hooks,
        container=ContainerConfig(
            projectName=f"lwa-{iid}",
            internalPort=8000,
            composePath="docker/compose.yaml",
            dockerfilePath="docker/Dockerfile",
        ),
    )
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)
    return manifest


def test_legacy_rm_rf_static_hook_is_detected(workspace, registry) -> None:
    from local_webpage_access.dockerfile_templates import destructive_artifact_copy_hooks

    manifest = _seed_python_container(workspace, registry, hooks=[_LEGACY_HOOK])
    hits = destructive_artifact_copy_hooks(manifest, workspace)
    assert hits == [_LEGACY_HOOK]


def test_unrelated_rm_hook_is_not_flagged(workspace, registry) -> None:
    from local_webpage_access.dockerfile_templates import destructive_artifact_copy_hooks

    manifest = _seed_python_container(
        workspace, registry, hooks=["rm -rf /tmp/cache", "python scripts/build_skills_bundle.py"]
    )
    assert destructive_artifact_copy_hooks(manifest, workspace) == []


def test_rm_temp_then_copy_static_is_not_flagged(workspace, registry) -> None:
    """rm 的操作数是临时目录、static 只出现在 cp 时，不得整段误拦。"""
    from local_webpage_access.dockerfile_templates import destructive_artifact_copy_hooks

    manifest = _seed_python_container(
        workspace,
        registry,
        hooks=[
            "rm -rf /tmp/build-cache && cp -a /app/frontend/dist/. /app/static/"
        ],
    )
    assert destructive_artifact_copy_hooks(manifest, workspace) == []


def test_generate_dockerfile_still_renders_legacy_hook(workspace, registry) -> None:
    """不得擅自删除用户钩子：模板仍原样渲染，由重建门禁告警。"""
    from local_webpage_access.dockerfile_templates import generate_dockerfile

    manifest = _seed_python_container(workspace, registry, hooks=[_LEGACY_HOOK])
    docker = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert f"RUN {_LEGACY_HOOK}" in docker
    assert "mkdir -p /app/static && cp -a /app/frontend/dist/. /app/static/" in docker


def test_rebuild_refuses_legacy_hook_without_force(workspace, registry, config) -> None:
    from local_webpage_access.lifecycle import rebuild_instance

    _seed_python_container(workspace, registry, hooks=[_LEGACY_HOOK])
    with pytest.raises(DestructiveBuildHooksError, match="clear-build-hooks") as ei:
        rebuild_instance(workspace, config, registry, "bookshelf")
    assert ei.value.code == "destructive_build_hooks"
    assert "--force" in str(ei.value)


def test_rebuild_force_allows_legacy_hook_and_records_event(
    workspace, registry, config, monkeypatch
) -> None:
    from local_webpage_access import hosting
    from local_webpage_access.lifecycle import rebuild_instance

    manifest = _seed_python_container(workspace, registry, hooks=[_LEGACY_HOOK])
    monkeypatch.setattr(
        hosting, "host_container", lambda *a, **k: manifest
    )
    rebuilt = rebuild_instance(
        workspace, config, registry, "bookshelf", force_hooks=True
    )
    assert rebuilt.id == "bookshelf"
    events = registry.list_events("bookshelf")
    assert any("产物目录" in (e.get("message") or "") for e in events)


def test_doctor_warns_on_legacy_artifact_hook(workspace, registry) -> None:
    from local_webpage_access.doctor import STATUS_WARN, check_build_hooks_artifact_conflict

    _seed_python_container(workspace, registry, hooks=[_LEGACY_HOOK])
    result = check_build_hooks_artifact_conflict(workspace, registry)
    assert result.status == STATUS_WARN
    assert "clear-build-hooks" in (result.suggestion or "")
    assert "bookshelf" in result.message


def test_cli_rebuild_force_forwards_force_hooks(
    workspace, registry, config, monkeypatch
) -> None:
    from typer.testing import CliRunner

    from local_webpage_access.cli import app

    captured: dict[str, object] = {}
    manifest = _seed_python_container(workspace, registry, hooks=[_LEGACY_HOOK])
    monkeypatch.setattr(
        "local_webpage_access.cli.lifecycle.open_workspace_registry",
        lambda: (workspace, config, registry),
    )
    monkeypatch.setattr(registry, "close", lambda: None)

    def _fake_rebuild(
        ws, cfg, reg, iid, *, out=None, ignore_alias_guard=False, force_hooks=False
    ):
        captured["force_hooks"] = force_hooks
        return manifest

    monkeypatch.setattr(
        "local_webpage_access.lifecycle.rebuild_instance", _fake_rebuild
    )
    result = CliRunner().invoke(app, ["rebuild", "bookshelf", "--force"])
    assert result.exit_code == 0, result.output
    assert captured.get("force_hooks") is True


def test_destructive_build_hooks_maps_to_http_409() -> None:
    """管理 API 将 DestructiveBuildHooksError 映射为 409，而不是通用 lifecycle internal。"""
    from local_webpage_access.manager_api import _ERROR_STATUS, _lwa_error_code

    exc = DestructiveBuildHooksError("hook would wipe static", instance_id="bookshelf")
    assert _lwa_error_code(exc) == "destructive_build_hooks"
    assert _ERROR_STATUS["destructive_build_hooks"] == 409
    from local_webpage_access.errors import LifecycleError

    assert _lwa_error_code(LifecycleError("other lifecycle")) == "internal"


@pytest.mark.parametrize("hook", [
    "rm -rf /tmp/build-cache > /app/static/cleanup.log",
    "rm -rf /tmp/build-cache 2>>/app/static/cleanup.log",
    "rm -rf /tmp/build-cache | tee /app/static/cleanup.log",
    "rm -rf /tmp/build-cache &> /app/static/cleanup.log",
    'echo "step; rm -rf /app/static"',
    'rm -rf "/tmp/cache && cp -a /app/static/"',
    "rm -rf /app/static/../cache",
    "rm -rf /tmp/cache > /app/static/log # don't fail",
])
def test_non_destructive_shell_targets_are_not_rm_operands(workspace, registry, hook) -> None:
    from local_webpage_access.dockerfile_templates import destructive_artifact_copy_hooks

    manifest = _seed_python_container(workspace, registry, hooks=[hook])
    assert destructive_artifact_copy_hooks(manifest, workspace) == []


@pytest.mark.parametrize("hook", [
    "rm -rf /tmp/cache > /tmp/log /app/static",
    "rm -rf /app/static | tee /tmp/log",
    "echo ready | rm -rf /app/static",
    'rm -rf ">" /app/static',
    'rm -rf /app/"sta"tic',
    "echo ready;>/tmp/log rm -rf /app/static",
    "rm -rf /app/./static",
    "rm -rf /app/static # don't keep old assets",
    "rm -rf /app/static 2>/tmp/log && cp -a /app/frontend/dist/. /app/static/",
])
def test_actual_static_delete_remains_blocked_with_shell_operators(
    workspace, registry, hook
) -> None:
    from local_webpage_access.dockerfile_templates import destructive_artifact_copy_hooks

    manifest = _seed_python_container(workspace, registry, hooks=[hook])
    assert destructive_artifact_copy_hooks(manifest, workspace) == [hook]
