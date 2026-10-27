"""宿主预构建的别名源码更新与 rebuild --sync 显式守卫选项回归。"""

from __future__ import annotations

import json
import pytest
from typer.testing import CliRunner

from local_webpage_access.errors import ZipImportError
from local_webpage_access.importer import Importer
from local_webpage_access.instance_settings import update_instance_settings
from local_webpage_access.models import InstanceManifest, Runtime
from tests.scripts.test_bug952_import_alias_guard import _write_zip
from tests.scripts.test_git_source import GitEnv

_SOURCE_INDEX = '<!doctype html><script type="module" src="/src/main.ts"></script>'
_BUILT_INDEX = '<!doctype html><script type="module" src="/assets/app.js"></script>'


def _files(revision="v1"):
    return {
        "backend/app.py": "from fastapi import FastAPI\napp = FastAPI()\n",
        "backend/requirements.txt": "fastapi\nuvicorn\n",
        "ui/index.html": _SOURCE_INDEX,
        "ui/src/main.ts": f"console.log('{revision}')\n",
        "ui/package.json": json.dumps({"scripts": {"build": "vite build"}}),
        "ui/vite.config.ts": "export default { base: process.env.VITE_BASE ?? '/' }\n",
    }


def _source_case(workspace, registry, config, tmp_path, monkeypatch, source_kind):
    monkeypatch.setattr(
        "local_webpage_access.static_gateway.StaticGateway.detect_backend", lambda self: "caddy"
    )
    importer = Importer(workspace, config, registry)
    source = tmp_path / "upstream"
    env = None
    if source_kind == "git":
        env = GitEnv(tmp_path / "git")
        source = env.seed
        (source / "index.html").unlink()
        (source / "about.html").unlink()
    source.mkdir(parents=True, exist_ok=True)

    def write(files):
        for rel, content in files.items():
            path = source / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        if env:
            env.push_commit("ui/src/main.ts", files["ui/src/main.ts"], "更新 Vite 源码")

    write(_files())
    if source_kind == "zip":
        imported = importer.import_zip(_write_zip(tmp_path, "old.zip", _files()), path_alias="demo")
    elif source_kind == "folder":
        imported = importer.import_from_dir(source, path_alias="demo")
    else:
        imported = importer.import_from_git(
            "https://github.com/acme/workbench", path_alias="demo", clone_url=str(env.remote)
        )
    iid = imported.instance_id
    assert imported.manifest.runtime == Runtime.DOCKER_COMPOSE
    assert imported.manifest.entry.build is None

    def update(files):
        write(files)
        if source_kind == "zip":
            return importer.update_zip(_write_zip(tmp_path, "new.zip", files), iid, restart=False)
        if source_kind == "folder":
            return importer.update_from_dir(iid, restart=False)
        return importer.update_from_git(iid, clone_url=str(env.remote), restart=False)

    return iid, write, update, env


@pytest.mark.parametrize("source_kind", ["zip", "folder", "git"])
@pytest.mark.parametrize("command_field", ["hostBuild", "preSync"])
@pytest.mark.parametrize("base_mode", ["follow", "manual"])
def test_host_build_alias_update_allows_proven_vite_source(
    workspace, registry, config, tmp_path, monkeypatch, source_kind, command_field, base_mode
):
    iid, _, update, _ = _source_case(
        workspace, registry, config, tmp_path, monkeypatch, source_kind
    )
    changes = {
        command_field: "cd ui && npm ci && npm run build:pack",
        "buildBaseFromAlias": base_mode == "follow",
        "buildEnv": {"VITE_BASE": "/demo/"} if base_mode == "manual" else None,
    }
    update_instance_settings(workspace, registry, iid, changes)

    result = update(_files("v2"))

    assert not result.skipped
    assert "v2" in (workspace.app_current(iid) / "ui/src/main.ts").read_text()
    actual = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert actual.entry.build is None
    assert getattr(actual, command_field) == changes[command_field]
    assert actual.hostBuildPending is True
    assert any("别名守卫豁免" in e["message"] for e in registry.list_events(iid))


@pytest.mark.parametrize(
    "boundary",
    [
        "no-command",
        "wrong-base",
        "no-base",
        "prebuilt",
        "missing-module",
        "mixed-assets",
        "fake-source",
    ],
)
def test_host_build_does_not_bypass_unproven_or_broken_alias_input(
    workspace, registry, config, tmp_path, monkeypatch, boundary
):
    iid, _, update, _ = _source_case(workspace, registry, config, tmp_path, monkeypatch, "zip")
    changes = {"hostBuild": "cd ui && npm run build", "buildBaseFromAlias": True}
    files = _files("v2")
    if boundary == "no-command":
        changes["hostBuild"] = None
    elif boundary in {"wrong-base", "no-base"}:
        changes.update(
            buildBaseFromAlias=False,
            buildEnv={"VITE_BASE": "/wrong/"} if boundary == "wrong-base" else None,
        )
    elif boundary == "prebuilt":
        files["ui/index.html"] = _BUILT_INDEX
        files["ui/assets/app.js"] = "console.log('built')"
    elif boundary == "missing-module":
        files.pop("ui/src/main.ts")
    elif boundary == "mixed-assets":
        files["ui/index.html"] += '<link rel="stylesheet" href="/assets/app.css">'
        files["ui/assets/app.css"] = "body{}"
    else:
        files["ui/index.html"] = "<p>/src/main.ts</p>" + _BUILT_INDEX
        files["ui/assets/app.js"] = "console.log('built')"
    update_instance_settings(workspace, registry, iid, changes)
    before = workspace.app_manifest_path(iid).read_bytes()

    with pytest.raises(ZipImportError, match="别名守卫"):
        update(files)

    assert workspace.app_manifest_path(iid).read_bytes() == before
    assert "v1" in (workspace.app_current(iid) / "ui/src/main.ts").read_text()


@pytest.mark.parametrize("source_kind", ["folder", "git"])
@pytest.mark.parametrize("ignore_guard", [False, True])
def test_rebuild_sync_propagates_explicit_guard_choice_to_real_importer(
    workspace, registry, config, tmp_path, monkeypatch, source_kind, ignore_guard
):
    iid, write, _, env = _source_case(
        workspace, registry, config, tmp_path, monkeypatch, source_kind
    )
    update_instance_settings(workspace, registry, iid, {"hostBuild": "cd ui && npm run build"})
    files = _files("v2")
    files["ui/index.html"] = _BUILT_INDEX
    files["ui/assets/app.js"] = "console.log('new built output')"
    write(files)
    if env:
        original = Importer.update_from_git

        def local_git(self, instance_id, **kwargs):
            return original(self, instance_id, clone_url=str(env.remote), **kwargs)

        monkeypatch.setattr(Importer, "update_from_git", local_git)
    monkeypatch.setattr(
        "local_webpage_access.cli.lifecycle.open_workspace_registry",
        lambda: (workspace, config, registry),
    )
    monkeypatch.setattr(registry, "close", lambda: None)
    rebuilt = []

    def rebuild(ws, cfg, reg, instance_id, **kwargs):
        rebuilt.append(kwargs)
        return InstanceManifest.load(ws.app_manifest_path(instance_id))

    monkeypatch.setattr("local_webpage_access.lifecycle.rebuild_instance", rebuild)
    from local_webpage_access.cli import app

    args = ["rebuild", iid, "--sync"]
    if ignore_guard:
        args.append("--ignore-alias-guard")
    result = CliRunner().invoke(app, args)

    assert result.exit_code == (0 if ignore_guard else 1), result.output
    if ignore_guard:
        assert len(rebuilt) == 1
        assert rebuilt[0]["ignore_alias_guard"] is True
        assert rebuilt[0]["host_build"] is True
        assert (workspace.app_current(iid) / "ui/index.html").read_text() == _BUILT_INDEX
        assert any("--ignore-alias-guard 越过" in e["message"] for e in registry.list_events(iid))
    else:
        assert rebuilt == []
        assert (workspace.app_current(iid) / "ui/index.html").read_text() == _SOURCE_INDEX
