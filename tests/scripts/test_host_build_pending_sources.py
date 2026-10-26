"""宿主预构建配置、目录/Git 重试与 Agent 更新入口回归（不访问 Docker/公网）。"""

from __future__ import annotations

import shlex
from functools import partial
from pathlib import Path

import pytest

from local_webpage_access.agent.contracts import (
    ApplyDeploymentInput,
    PlanDeploymentInput,
    ServerDirectorySource,
)
from local_webpage_access.agent.operations import AgentWorker
from local_webpage_access.agent.service import AgentService
from local_webpage_access.errors import BuildError
from local_webpage_access.importer import Importer
from local_webpage_access.instance_settings import update_instance_settings
from local_webpage_access.lifecycle import rebuild_instance
from local_webpage_access.models import DesiredState, InstanceManifest, Status
from tests.scripts.test_agent_operations import _config, _ops, _run_worker_once, _service
from tests.scripts.test_bug1003_zip_hostbuild_retry import _container_manifest, _seed
from tests.scripts.test_git_source import GitEnv


@pytest.mark.parametrize(
    "old_fields,pending,changes,expected",
    [
        ({}, False, {"hostBuild": " echo new "}, True),
        ({}, False, {"preSync": " echo new "}, True),
        ({"hostBuild": "echo old"}, False, {"hostBuild": "echo new"}, True),
        ({"preSync": "echo old"}, False, {"preSync": "echo new"}, True),
        ({"hostBuild": "echo old"}, True, {"hostBuild": ""}, False),
        ({"preSync": "echo old"}, True, {"preSync": None}, False),
        ({"hostBuild": "echo old"}, True, {"hostBuild": " ", "preSync": ""}, False),
        ({"hostBuild": "echo same"}, False, {"hostBuild": " echo same "}, False),
        ({"preSync": "echo same"}, False, {"preSync": " echo same "}, False),
        ({"hostBuild": "echo same"}, True, {"hostBuild": " echo same "}, True),
        (
            {"hostBuild": "echo same"}, False,
            {"hostBuild": None, "preSync": " echo same "}, False,
        ),
        (
            {"preSync": "echo old"}, False,
            {"preSync": None, "hostBuild": "echo new"}, True,
        ),
        ({"hostBuild": "echo old"}, True, {"buildEnv": {"FOO": "bar"}}, True),
        ({"hostBuild": "echo old"}, False, {"buildEnv": {"FOO": "bar"}}, False),
    ],
)
def test_settings_track_effective_command_changes(
    workspace, registry, old_fields, pending, changes, expected
):
    manifest = _container_manifest("demo")
    for key, value in old_fields.items():
        setattr(manifest, key, value)
    manifest.hostBuildPending = pending
    _seed(workspace, registry, manifest)

    updated = update_instance_settings(workspace, registry, "demo", changes)

    assert updated.hostBuildPending is expected
    assert InstanceManifest.load(workspace.app_manifest_path("demo")).hostBuildPending is expected


def _python_source(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "requirements.txt").write_text("flask\n", encoding="utf-8")
    (root / "app.py").write_text("app = None\n", encoding="utf-8")
    return root


@pytest.fixture(params=["folder", "git"])
def source_instance(request, workspace, registry, config, tmp_path):
    importer = Importer(workspace, config, registry)
    if request.param == "folder":
        source = _python_source(tmp_path / "source")
        iid = importer.import_from_dir(source).instance_id
        update = partial(importer.update_from_dir, iid)

        def advance():
            (source / "revision.txt").write_text("new", encoding="utf-8")
    else:
        env = GitEnv(tmp_path)
        env.push_commit("backend/requirements.txt", "flask\n", "添加 Python 依赖")
        env.push_commit("backend/app.py", "app = None\n", "添加 Python 入口")
        iid = importer.import_from_git(
            "https://github.com/acme/mysite", subdir="backend", clone_url=str(env.remote)
        ).instance_id
        update = partial(importer.update_from_git, iid, clone_url=str(env.remote))

        def advance():
            env.push_commit("backend/revision.txt", "new", "更新源码")

    manifest = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert manifest.runtime.value == "docker-compose"
    manifest.desiredState = DesiredState.RUNNING
    manifest.status = Status.RUNNING
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)
    return iid, update, advance


def _snapshot(workspace, registry):
    # 生命周期锁会刷新心跳，SQLite shm 会因读取改变；比较应用字节和数据库内容。
    files = {
        str(path.relative_to(workspace.root)): path.read_bytes()
        for path in (workspace.root / "apps").rglob("*")
        if path.is_file()
    }
    return files, list(registry.conn.iterdump())


@pytest.mark.parametrize("dry_run", [False, True])
def test_same_source_retries_after_real_host_build_failure(
    source_instance, workspace, registry, config, tmp_path, monkeypatch, dry_run
):
    iid, update, advance = source_instance
    ready = tmp_path / "dependency-ready"
    manifest = InstanceManifest.load(workspace.app_manifest_path(iid))
    manifest.hostBuild = f"test -f {shlex.quote(str(ready))} && printf built > artifact.txt"
    manifest.save(workspace.app_manifest_path(iid))
    advance()
    first = update(restart=True)
    assert first.skipped is False and first.needs_rebuild is True

    builds = []

    def host(ws, cfg, reg, target, **kwargs):
        builds.append((ws.app_current(target) / "artifact.txt").read_text())
        return InstanceManifest.load(ws.app_manifest_path(target))

    monkeypatch.setattr("local_webpage_access.hosting.host_container", host)
    with pytest.raises(BuildError):
        rebuild_instance(workspace, config, registry, iid, host_build=True)
    assert builds == []
    assert InstanceManifest.load(workspace.app_manifest_path(iid)).hostBuildPending is True

    if dry_run:
        before = _snapshot(workspace, registry)
        dry = update(dry_run=True)
        assert _snapshot(workspace, registry) == before
        assert dry.dry_run is True and dry.skipped is False and dry.needs_rebuild is True
        return

    ready.touch()
    retry = update(restart=True)
    assert retry.skipped is False and retry.needs_rebuild is True
    assert retry.manifest.hostBuildPending is True
    rebuild_instance(workspace, config, registry, iid, host_build=True)
    assert builds == ["built"]
    assert InstanceManifest.load(workspace.app_manifest_path(iid)).hostBuildPending is False
    assert update().skipped is True


@pytest.mark.parametrize("dry_run", [False, True])
def test_unchanged_source_without_pending_still_short_circuits(
    source_instance, workspace, registry, monkeypatch, dry_run
):
    _, update, _ = source_instance

    def unexpected(*args, **kwargs):
        pytest.fail("正常同内容/OID 更新应在打包与克隆前短路")

    monkeypatch.setattr("local_webpage_access.folder_source.pack_source_dir", unexpected)
    monkeypatch.setattr("local_webpage_access.git_source.stage_git_clone", unexpected)
    before = _snapshot(workspace, registry)
    result = update(dry_run=dry_run)
    if dry_run:
        assert _snapshot(workspace, registry) == before
    assert result.skipped is True and result.needs_rebuild is False
    assert result.dry_run is dry_run


def test_agent_update_runs_host_build_after_import(
    workspace, registry, tmp_path, monkeypatch
):
    config = _config(tmp_path)
    source = _python_source(tmp_path / "source")
    importer = Importer(workspace, config, registry)
    iid = importer.import_from_dir(source).instance_id
    manifest = InstanceManifest.load(workspace.app_manifest_path(iid))
    manifest.hostBuild = "cat revision.txt > artifact.txt"
    manifest.desiredState = DesiredState.RUNNING
    manifest.status = Status.RUNNING
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)
    (source / "revision.txt").write_text("new", encoding="utf-8")
    monkeypatch.setattr(AgentService, "capability_gaps_now", lambda *a: [])
    builds = []
    rebuild_options = []

    def rebuild(*args, **kwargs):
        rebuild_options.append(kwargs)
        return rebuild_instance(*args, **kwargs)

    def host(ws, cfg, reg, target, **kwargs):
        builds.append((ws.app_current(target) / "artifact.txt").read_text())
        return InstanceManifest.load(ws.app_manifest_path(target))

    monkeypatch.setattr("local_webpage_access.hosting.host_container", host)
    monkeypatch.setattr("local_webpage_access.lifecycle.rebuild_instance", rebuild)
    plan = _service(workspace, config, registry).plan_deployment(
        PlanDeploymentInput(
            source=ServerDirectorySource(type="server_directory", path=str(source)),
            intent="update", targetInstanceId=iid, expectedRevision=registry.get_revision(iid),
        )
    )
    ops = _ops(workspace, config, registry)
    accepted = ops.apply_deployment(
        ApplyDeploymentInput(planId=plan.planId, idempotencyKey="host-build-update")
    )
    _run_worker_once(AgentWorker(workspace, config, registry))
    view = ops.get_operation(accepted.operationId)
    assert view.status.value == "succeeded", view.error
    assert builds == ["new"]
    assert rebuild_options == [{"host_build": True}]
    assert InstanceManifest.load(workspace.app_manifest_path(iid)).hostBuildPending is False


def test_agent_create_retains_start_without_host_build_authorization(
    workspace, registry, tmp_path, monkeypatch
):
    config = _config(tmp_path)
    source = _python_source(tmp_path / "source")
    monkeypatch.setattr(AgentService, "capability_gaps_now", lambda *a: [])
    starts = []

    def start(ws, cfg, reg, iid, **kwargs):
        starts.append(kwargs)

    def unexpected(*args, **kwargs):
        pytest.fail("首次导入不应新增重建或宿主用户命令执行")

    monkeypatch.setattr("local_webpage_access.lifecycle.start_instance", start)
    monkeypatch.setattr("local_webpage_access.lifecycle.rebuild_instance", unexpected)
    monkeypatch.setattr("local_webpage_access.host_build.run_host_build", unexpected)
    plan = _service(workspace, config, registry).plan_deployment(
        PlanDeploymentInput(
            source=ServerDirectorySource(type="server_directory", path=str(source)),
            intent="create", displayName="agent-create",
        )
    )
    ops = _ops(workspace, config, registry)
    accepted = ops.apply_deployment(
        ApplyDeploymentInput(planId=plan.planId, idempotencyKey="host-build-create")
    )
    _run_worker_once(AgentWorker(workspace, config, registry))
    view = ops.get_operation(accepted.operationId)
    assert view.status.value == "succeeded", view.error
    assert starts == [{}]
