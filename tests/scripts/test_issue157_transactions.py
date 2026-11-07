from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from local_webpage_access import manager_api
from local_webpage_access.config import Config
from local_webpage_access.errors import HostingError
from local_webpage_access.hosting import (
    _clear_container_identity,
    _compensate_build_first,
    _resolve_host_build_command,
    _split_shell_and_segments,
)
from local_webpage_access.models import (
    ContainerConfig,
    DesiredState,
    InstanceManifest,
    Kind,
    ResourceProfile,
    Runtime,
    ServingMode,
    Status,
)
from local_webpage_access.path_alias import set_instance_path_alias
from local_webpage_access.paths import Workspace
from local_webpage_access.static_gateway import StaticGateway, _replace_entry_certificate_files


def test_token_rotation_is_serialized(workspace: Workspace) -> None:
    workspace.run.mkdir(parents=True, exist_ok=True)
    (workspace.run / "manager-token.json").write_text(
        json.dumps({"token": "old", "createdAt": "2000-01-01T00:00:00"}), encoding="utf-8"
    )
    barrier = threading.Barrier(2)
    results = []

    def rotate():
        barrier.wait()
        results.append(manager_api.maybe_rotate_token(workspace))

    first = threading.Thread(target=rotate)
    second = threading.Thread(target=rotate)
    first.start()
    second.start()
    first.join()
    second.join()
    assert sorted(item["rotated"] for item in results) == [False, True]
    assert len({item["token"] for item in results}) == 1


def test_rollback_records_new_container_identity(
    workspace, registry, monkeypatch
) -> None:
    iid = "demo"
    manifest = InstanceManifest(
        id=iid,
        name=iid,
        version="1",
        kind=Kind.PYTHON,
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.TINY,
        status=Status.RUNNING,
        desiredState=DesiredState.RUNNING,
        container=ContainerConfig(
            projectName="demo",
            internalPort=8000,
            composePath="docker/compose.yaml",
            dockerfilePath="docker/Dockerfile",
            containerId="old-container",
            imageId="old-image",
        ),
    )
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)
    _clear_container_identity(workspace, manifest)

    class FakeRuntime:
        def down(self, _iid):
            return None

        def up(self, _iid):
            return None

        def container_id(self, _iid):
            return "new-container"

        def image_id(self, _iid):
            return "new-image"

    monkeypatch.setattr("local_webpage_access.hosting.rebuild_txn.restore_generated", lambda ws, iid: True)
    monkeypatch.setattr("local_webpage_access.hosting.rebuild_txn.restore_last_good_env", lambda ws, iid: True)
    monkeypatch.setattr("local_webpage_access.hosting.rebuild_txn.restore_previous_image_tag", lambda manifest: None)
    monkeypatch.setattr("local_webpage_access.hosting._wait_for_http", lambda port: True)
    with pytest.raises(HostingError):
        _compensate_build_first(workspace, registry, FakeRuntime(), manifest, 18000, 1, RuntimeError("boom"))
    saved = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert saved.container.containerId == "new-container"
    assert saved.container.imageId == "new-image"
    assert saved.status == Status.RUNNING


def test_alias_manifest_save_failure_rolls_back_gateway(workspace, registry, config, monkeypatch) -> None:
    iid = "demo"
    from tests.scripts.test_access import _seed_static

    _seed_static(workspace, registry, iid)
    rolled_back = []
    monkeypatch.setattr("local_webpage_access.path_alias.StaticGateway.detect_backend", lambda self: "caddy")
    monkeypatch.setattr(
        "local_webpage_access.path_alias._apply_gateway_alias",
        lambda *args, **kwargs: (True, True),
    )
    monkeypatch.setattr(
        "local_webpage_access.path_alias._rollback_alias_after_failed_live_verify",
        lambda *args, **kwargs: rolled_back.append(kwargs),
    )
    monkeypatch.setattr(
        "local_webpage_access.path_alias.InstanceManifest.save",
        lambda self, path: (_ for _ in ()).throw(OSError("read-only")),
    )
    with pytest.raises(OSError, match="read-only"):
        set_instance_path_alias(workspace, config, registry, iid, "books", skip_compat_check=True)
    assert len(rolled_back) == 1
    assert rolled_back[0]["previous_alias"] is None


def test_alias_manifest_save_failure_skips_rollback_when_gateway_untouched(
    workspace, registry, config, monkeypatch
) -> None:
    """网关未被改动（如静态实例已停止）时，manifest 保存失败不得回滚/reload_all 拉起 Caddy。"""
    iid = "demo"
    from tests.scripts.test_access import _seed_static

    _seed_static(workspace, registry, iid)
    reloads: list[int] = []
    rolled_back: list[dict] = []
    monkeypatch.setattr("local_webpage_access.path_alias.StaticGateway.detect_backend", lambda self: "caddy")
    monkeypatch.setattr(
        "local_webpage_access.path_alias._apply_gateway_alias",
        lambda *args, **kwargs: (False, False),
    )
    monkeypatch.setattr(
        "local_webpage_access.path_alias.StaticGateway.reload_all",
        lambda self: reloads.append(1),
    )
    monkeypatch.setattr(
        "local_webpage_access.path_alias._rollback_alias_after_failed_live_verify",
        lambda *args, **kwargs: rolled_back.append(kwargs),
    )
    monkeypatch.setattr(
        "local_webpage_access.path_alias.InstanceManifest.save",
        lambda self, path: (_ for _ in ()).throw(OSError("read-only")),
    )
    with pytest.raises(OSError, match="read-only"):
        set_instance_path_alias(workspace, config, registry, iid, "books", skip_compat_check=True)
    assert rolled_back == []
    assert reloads == []


def test_shell_and_split_ignores_quoted_operators(tmp_path: Path) -> None:
    command = 'echo "a && pnpm run build && b"'
    assert _split_shell_and_segments(command) == [command]
    assert _resolve_host_build_command(command, tmp_path) == command


def test_shell_and_split_restores_outer_quote_after_command_substitution(
    tmp_path: Path, monkeypatch
) -> None:
    command = 'echo "$(printf \'(\')" && pnpm run build'
    assert _split_shell_and_segments(command) == [
        'echo "$(printf \'(\')" ',
        " pnpm run build",
    ]
    monkeypatch.setattr(
        "local_webpage_access.hosting.shutil.which",
        lambda name, path=None: "/usr/bin/corepack" if name == "corepack" else None,
    )
    assert _resolve_host_build_command(
        command, tmp_path, pinned=("pnpm", "9.15.0")
    ) == 'echo "$(printf \'(\')" && corepack pnpm@9.15.0 run build'


def test_certificate_group_replace_restores_old_files(tmp_path: Path, monkeypatch) -> None:
    old_key, old_cert, old_meta = tmp_path / "key.pem", tmp_path / "cert.pem", tmp_path / "meta.json"
    for path, content in ((old_key, "old-key"), (old_cert, "old-cert"), (old_meta, "old-meta")):
        path.write_text(content, encoding="utf-8")
    new_key, new_cert, new_meta = tmp_path / "new-key", tmp_path / "new-cert", tmp_path / "new-meta"
    for path, content in ((new_key, "new-key"), (new_cert, "new-cert"), (new_meta, "new-meta")):
        path.write_text(content, encoding="utf-8")
    original_replace = __import__("os").replace

    def fail_second(src, dst):
        if Path(src).name == "new-cert":
            raise OSError("injected")
        return original_replace(src, dst)

    monkeypatch.setattr("local_webpage_access.static_gateway.os.replace", fail_second)
    with pytest.raises(OSError, match="injected"):
        _replace_entry_certificate_files(
            [(new_key, old_key), (new_cert, old_cert), (new_meta, old_meta)]
        )
    assert old_key.read_text(encoding="utf-8") == "old-key"
    assert old_cert.read_text(encoding="utf-8") == "old-cert"
    assert old_meta.read_text(encoding="utf-8") == "old-meta"


def test_stop_all_builtin_excludes_failed_stop(workspace: Workspace, monkeypatch) -> None:
    gateway = StaticGateway(workspace, Config())
    workspace.run.mkdir(parents=True, exist_ok=True)
    (workspace.run / "static-demo.pid").write_text("4321", encoding="utf-8")
    monkeypatch.setattr(gateway, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(gateway, "_stop_builtin", lambda iid: False)
    monkeypatch.setattr(gateway, "_enumerate_workspace_builtin_pids", lambda: [(4321, "demo")])
    monkeypatch.setattr(gateway, "_kill_process", lambda pid, **kwargs: False)
    assert gateway.stop_all_builtin() == []


def test_start_builtin_cleans_process_when_pid_write_fails(workspace: Workspace, monkeypatch) -> None:
    gateway = StaticGateway(workspace, Config())
    root = workspace.app_public("demo")
    root.mkdir(parents=True, exist_ok=True)

    class Proc:
        pid = 5432

    killed = []
    monkeypatch.setattr("local_webpage_access.static_gateway.subprocess.Popen", lambda *args, **kwargs: Proc())
    monkeypatch.setattr(gateway, "_write_pid", lambda iid, pid: (_ for _ in ()).throw(OSError("read-only")))
    monkeypatch.setattr(
        gateway,
        "_kill_process",
        lambda pid, **kwargs: killed.append((pid, kwargs)) or True,
    )
    with pytest.raises(OSError, match="read-only"):
        gateway._start_builtin("demo", 21000, root)
    assert killed and killed[0][0] == 5432
    assert "demo" not in gateway._procs
