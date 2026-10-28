"""DEV-171 / issue #153：按项目 engines 选择备选宿主 Node。"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from local_webpage_access.config import Config
from local_webpage_access.errors import BuildError
from local_webpage_access.host_build import run_host_build
from local_webpage_access.hosting import build_and_host_frontend
from local_webpage_access.importer import build_manifest_from_detection
from local_webpage_access.models import (
    ContainerConfig,
    DesiredState,
    EntryConfig,
    InstanceManifest,
    Kind,
    ResourceProfile,
    Runtime,
    ServingMode,
    Status,
)
from local_webpage_access.node_toolchain import select_project_node
from local_webpage_access.scanner import DetectionResult


def _node(root: Path, version: str) -> Path:
    executable = root / "bin" / "node"
    executable.parent.mkdir(parents=True)
    executable.write_text(f'#!/bin/sh\necho "v{version}"\n', encoding="utf-8")
    executable.chmod(0o755)
    return executable


def _engines(root: Path, range_text: str = "18.0.0 - 22.x.x") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(
        json.dumps({"name": "demo", "engines": {"node": range_text}}),
        encoding="utf-8",
    )


def _container_manifest(command: str) -> InstanceManifest:
    return InstanceManifest(
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
        hostBuild=command,
    )


def _seed_frontend(workspace, registry) -> None:
    workspace.ensure_app_dirs("spa")
    current = workspace.app_current("spa")
    _engines(current)
    detection = DetectionResult(
        kind=Kind.NODE,
        runtime=Runtime.SHARED_STATIC,
        servingMode=ServingMode.SHARED_STATIC,
        resourceProfile=ResourceProfile.TINY,
        form="frontend-static",
        confidence="high",
        stack=["react"],
        entry={"install": "npm ci", "build": "npm run build", "start": None},
    )
    detection.entry = EntryConfig(install="npm ci", build="npm run build")
    manifest = build_manifest_from_detection(
        instance_id="spa",
        display_name="Spa",
        detection=detection,
        workspace=workspace,
    )
    manifest.save(workspace.app_manifest_path("spa"))
    registry.upsert_from_manifest(manifest)


def test_config_fallback_paths_must_be_absolute(tmp_path: Path) -> None:
    node = _node(tmp_path / "node22", "22.22.0")
    assert Config(nodeFallbackPaths=[str(node)]).nodeFallbackPaths == [str(node)]
    with pytest.raises(ValidationError, match="绝对路径"):
        Config(nodeFallbackPaths=["relative/node"])
    with pytest.raises(ValidationError, match="文件名必须为 node"):
        Config(nodeFallbackPaths=[str(node.with_name("node22"))])


def test_default_compatible_node_does_not_switch(tmp_path: Path) -> None:
    _engines(tmp_path, ">=20.0.0")
    default = _node(tmp_path / "node24", "24.22.0")
    fallback = _node(tmp_path / "node22", "22.22.0")
    env = {"PATH": f"{default.parent}{os.pathsep}{os.environ['PATH']}"}
    original = env["PATH"]

    selected = select_project_node(tmp_path, env, [str(fallback)])

    assert selected is None
    assert env["PATH"] == original


def test_mismatch_selects_highest_compatible_node(tmp_path: Path) -> None:
    _engines(tmp_path)
    default = _node(tmp_path / "node24", "24.22.0")
    node20 = _node(tmp_path / "node20", "20.20.0")
    node22 = _node(tmp_path / "node22", "22.22.0")
    missing = tmp_path / "missing" / "node"
    env = {"PATH": f"{default.parent}{os.pathsep}{os.environ['PATH']}"}

    selected = select_project_node(tmp_path, env, [str(node20), str(missing), str(node22)])

    assert selected is not None
    assert selected.executable == node22
    assert selected.version == (22, 22, 0)
    assert env["PATH"].split(os.pathsep)[0] == str(node22.parent)


def test_no_compatible_fallback_keeps_path_unchanged(tmp_path: Path) -> None:
    _engines(tmp_path)
    default = _node(tmp_path / "node24", "24.22.0")
    node24 = _node(tmp_path / "other24", "24.21.0")
    env = {"PATH": f"{default.parent}{os.pathsep}{os.environ['PATH']}"}
    original = env["PATH"]

    assert select_project_node(tmp_path, env, [str(node24)]) is None
    assert env["PATH"] == original


def test_host_build_executes_with_selected_node(workspace, registry, tmp_path: Path) -> None:
    current = workspace.app_current("demo")
    _engines(current)
    default = _node(tmp_path / "node24", "24.22.0")
    fallback = _node(tmp_path / "node22", "22.22.0")
    manifest = _container_manifest("node --version > selected-node.txt")
    manifest.appPath = str(current)
    manifest.buildEnv = {"PATH": f"{default.parent}{os.pathsep}{os.environ['PATH']}"}
    manifest.save(workspace.app_manifest_path("demo"))
    registry.upsert_from_manifest(manifest)
    config = Config(staticGateway="builtin", nodeFallbackPaths=[str(fallback)])

    result = run_host_build(workspace, config, registry, manifest)

    assert result is not None
    assert (current / "selected-node.txt").read_text(encoding="utf-8").strip() == "v22.22.0"


@pytest.mark.parametrize("ignore_engines", [False, True])
def test_host_build_without_compatible_fallback_requires_explicit_ignore(
    workspace, registry, tmp_path: Path, ignore_engines: bool
) -> None:
    current = workspace.app_current("demo")
    _engines(current)
    default = _node(tmp_path / "node24", "24.22.0")
    incompatible = _node(tmp_path / "other24", "24.21.0")
    manifest = _container_manifest("node --version > selected-node.txt")
    manifest.appPath = str(current)
    manifest.buildEnv = {"PATH": f"{default.parent}{os.pathsep}{os.environ['PATH']}"}
    manifest.save(workspace.app_manifest_path("demo"))
    registry.upsert_from_manifest(manifest)
    config = Config(
        staticGateway="builtin",
        nodeFallbackPaths=[str(tmp_path / "missing" / "node"), str(incompatible)],
        nodeEnginesIgnore=ignore_engines,
    )

    if ignore_engines:
        assert run_host_build(workspace, config, registry, manifest) is not None
        assert (current / "selected-node.txt").read_text().strip() == "v24.22.0"
    else:
        original_manifest = workspace.app_manifest_path("demo").read_bytes()
        with pytest.raises(BuildError, match="nodeFallbackPaths"):
            run_host_build(workspace, config, registry, manifest)
        assert not (current / "selected-node.txt").exists()
        assert workspace.app_manifest_path("demo").read_bytes() == original_manifest


def test_frontend_install_and_build_share_selected_node(
    workspace, registry, tmp_path: Path, monkeypatch
) -> None:
    _seed_frontend(workspace, registry)
    default = _node(tmp_path / "node24", "24.22.0")
    fallback = _node(tmp_path / "node22", "22.22.0")
    frontend = InstanceManifest.load(workspace.app_manifest_path("spa"))
    frontend.buildEnv = {"PATH": f"{default.parent}{os.pathsep}{os.environ['PATH']}"}
    frontend.save(workspace.app_manifest_path("spa"))
    config = Config(staticGateway="builtin", nodeFallbackPaths=[str(fallback)])
    monkeypatch.setattr(
        "local_webpage_access.node_toolchain.host_node_version",
        lambda path=None: (24, 22, 0),
    )
    seen: list[dict] = []

    def fake_run(command, *, cwd, log_path, env=None, **kwargs):
        assert env is not None
        seen.append(env)
        if "build" in command:
            dist = Path(cwd) / "dist"
            dist.mkdir()
            (dist / "index.html").write_text("built", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("local_webpage_access.hosting.run_command", fake_run)
    try:
        build_and_host_frontend(workspace, config, registry, "spa")
        assert len(seen) == 2
        assert all(env["PATH"].split(os.pathsep)[0] == str(fallback.parent) for env in seen)
    finally:
        from local_webpage_access.hosting import stop_instance

        stop_instance(workspace, config, registry, "spa")


def test_corepack_command_resolution_uses_selected_build_path(
    tmp_path: Path, monkeypatch
) -> None:
    from local_webpage_access.hosting import (
        _resolve_host_build_command,
        _resolve_host_install_command,
    )

    selected_path = str(tmp_path / "node22" / "bin")
    calls: list[tuple[str, str | None]] = []

    def fake_which(name: str, path: str | None = None) -> str:
        calls.append((name, path))
        return f"/tool/{name}"

    monkeypatch.setattr("local_webpage_access.hosting.shutil.which", fake_which)

    assert _resolve_host_install_command(
        "corepack enable && pnpm install --frozen-lockfile",
        tmp_path,
        path=selected_path,
    ) == "pnpm install --frozen-lockfile"
    assert _resolve_host_build_command(
        "pnpm run build", tmp_path, path=selected_path
    ) == "pnpm run build"
    assert calls
    assert all(path == selected_path for _, path in calls)


def test_corepack_shim_uses_selected_build_path(workspace, tmp_path: Path, monkeypatch) -> None:
    from local_webpage_access.hosting import _ensure_corepack_shim_dir

    selected = _node(tmp_path / "node22", "22.22.0").parent / "corepack"
    selected.write_text("#!/bin/sh\n", encoding="utf-8")
    selected.chmod(0o755)
    default = tmp_path / "node24" / "bin" / "corepack"
    selected_path = str(selected.parent)
    executed: list[str] = []
    environments: list[dict | None] = []

    monkeypatch.setattr(
        "local_webpage_access.hosting.shutil.which",
        lambda name, path=None: str(selected if path == selected_path else default),
    )

    def fake_run(command, **kwargs):
        executed.append(command[0])
        environments.append(kwargs.get("env"))
        shim_dir = Path(command[command.index("--install-directory") + 1])
        shim_dir.mkdir(parents=True, exist_ok=True)
        (shim_dir / "yarn").touch()
        (shim_dir / "pnpm").touch()
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("local_webpage_access.hosting.subprocess.run", fake_run)

    assert _ensure_corepack_shim_dir(workspace, path=selected_path) is not None
    assert executed == [str(selected)]
    assert environments[0] is not None
    assert environments[0]["PATH"] == selected_path


@pytest.mark.parametrize("engines_location", ["root", "subdir"])
def test_host_build_reads_source_subdir_and_workspace_engines(
    workspace, registry, tmp_path: Path, engines_location: str
) -> None:
    current = workspace.app_current("demo")
    frontend = current / "frontend"
    frontend.mkdir(parents=True)
    (current / "package.json").write_text('{"name":"root"}', encoding="utf-8")
    if engines_location == "root":
        _engines(current)
    else:
        _engines(frontend)
    default = _node(tmp_path / "node24", "24.22.0")
    fallback = _node(tmp_path / "node22", "22.22.0")
    manifest = _container_manifest("node --version > selected-node.txt")
    manifest.sourceSubdir = "frontend"
    manifest.appPath = str(current)
    manifest.buildEnv = {"PATH": f"{default.parent}{os.pathsep}{os.environ['PATH']}"}
    manifest.save(workspace.app_manifest_path("demo"))
    registry.upsert_from_manifest(manifest)
    config = Config(staticGateway="builtin", nodeFallbackPaths=[str(fallback)])

    assert run_host_build(workspace, config, registry, manifest) is not None
    assert (current / "selected-node.txt").read_text(encoding="utf-8").strip() == "v22.22.0"


def test_pm_version_probe_executes_with_selected_node(tmp_path: Path) -> None:
    from local_webpage_access.hosting import _pm_path_version

    node22 = _node(tmp_path / "node22", "22.22.0")
    node22.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo v22.22.0; else echo 8.15.9; fi\n',
        encoding="utf-8",
    )
    node24 = _node(tmp_path / "node24", "24.22.0")
    node24.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo v24.22.0; else exit 1; fi\n',
        encoding="utf-8",
    )
    pnpm = node22.parent / "pnpm"
    pnpm.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    pnpm.chmod(0o755)
    path = f"{node22.parent}{os.pathsep}{node24.parent}{os.pathsep}{os.environ['PATH']}"

    assert _pm_path_version("pnpm", path=path) == (8, 15, 9)


def test_corepack_shim_cache_isolated_by_executable(workspace, tmp_path: Path, monkeypatch) -> None:
    from local_webpage_access.hosting import _ensure_corepack_shim_dir

    corepack22 = _node(tmp_path / "node22", "22.22.0").parent / "corepack"
    corepack18 = _node(tmp_path / "node18", "18.20.0").parent / "corepack"
    paths = {str(corepack22.parent): str(corepack22), str(corepack18.parent): str(corepack18)}
    monkeypatch.setattr(
        "local_webpage_access.hosting.shutil.which",
        lambda name, path=None: paths[path],
    )

    def fake_run(command, **kwargs):
        shim_dir = Path(command[command.index("--install-directory") + 1])
        shim_dir.mkdir(parents=True, exist_ok=True)
        (shim_dir / "yarn").touch()
        (shim_dir / "pnpm").touch()
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("local_webpage_access.hosting.subprocess.run", fake_run)

    shim22 = _ensure_corepack_shim_dir(workspace, path=str(corepack22.parent))
    shim18 = _ensure_corepack_shim_dir(workspace, path=str(corepack18.parent))

    assert shim22 is not None and shim18 is not None
    assert shim22 != shim18
    assert (Path(shim22) / "pnpm").is_file()
    assert (Path(shim18) / "pnpm").is_file()
    assert _ensure_corepack_shim_dir(workspace, path=str(corepack22.parent)) == shim22


def test_selector_rejects_node_with_unusable_filename(tmp_path: Path) -> None:
    _engines(tmp_path)
    default = _node(tmp_path / "node24", "24.22.0")
    fallback = _node(tmp_path / "node22", "22.22.0").with_name("node22")
    env = {"PATH": f"{default.parent}{os.pathsep}{os.environ['PATH']}"}

    with pytest.raises(ValueError, match="文件名必须为 node"):
        select_project_node(tmp_path, env, [str(fallback)])
