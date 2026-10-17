"""工厂入口实际执行与 instance 救援的残留回归。"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from local_webpage_access.compose import _seed_instance_dir, generate_compose
from local_webpage_access.docker_runtime import ComposeResult, DockerRuntime
from local_webpage_access.dockerfile_templates import _cmd_line
from local_webpage_access.errors import DockerError, HostingError, PathError
from local_webpage_access.hosting import _rescue_container_data_before_rebuild
from tests.scripts.test_bug903_908 import _write_flask_instance_app
from tests.scripts.test_compose import _mk_manifest as _compose_manifest
from tests.scripts.test_dockerfile_templates import _mk_manifest


@pytest.mark.parametrize("uv", [False, True])
@pytest.mark.parametrize("pre_start", [None, "echo ready >/dev/null"])
@pytest.mark.parametrize("wrapper", [None, "single", "double"])
def test_factory_cmd_executes_through_all_shell_layers(
    tmp_path: Path, uv: bool, pre_start: str | None, wrapper: str | None
) -> None:
    """真正执行 CMD，捕获最终 gunicorn argv，不能只检查外层 sh -n。"""
    (tmp_path / "gunicorn").write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    (tmp_path / "gunicorn").chmod(0o755)
    (tmp_path / "uv").write_text(
        '#!/bin/sh\nshift\nwhile [ "${1#--}" != "$1" ]; do shift; done\nexec "$@"\n'
    )
    (tmp_path / "uv").chmod(0o755)
    start = "gunicorn flaskr:create_app()"
    if wrapper == "single":
        start = f"sh -c '{start}'"
    elif wrapper == "double":
        start = f'sh -c "{start}"'
    if uv:
        start = "uv run --no-sync --no-dev " + start
    manifest = _mk_manifest(start=start)
    manifest.preStart = pre_start
    argv = json.loads(_cmd_line(manifest, start))
    proc = subprocess.run(
        argv, capture_output=True, text=True, check=False,
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
    )
    assert proc.returncode == 0, (argv, proc.stderr)
    assert proc.stdout == "flaskr:create_app()\n"


@pytest.mark.parametrize("inner_quote", ["'", '"'])
def test_shell_text_inside_argument_is_not_rewritten(inner_quote: str) -> None:
    """普通参数里的 sh -c 文本不是可执行的包装层。"""
    payload = f"sh -c {inner_quote}gunicorn flaskr:create_app(){inner_quote}"
    start = f"printf '%s' {shlex.quote(payload)} && true"
    proc = subprocess.run(
        json.loads(_cmd_line(_mk_manifest(start=start), start)),
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == payload


def test_factory_text_inside_escaped_quotes_is_not_rewritten() -> None:
    start = r'''printf '%s' "\"flaskr:create_app()\"" && true'''
    proc = subprocess.run(
        json.loads(_cmd_line(_mk_manifest(start=start), start)),
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == '"flaskr:create_app()"'


def _write_database(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("create table records(value text)")
        db.execute("insert into records values (?)", (value,))


@pytest.mark.parametrize("host_value", [None, "host-data"])
def test_instance_rescue_prefers_live_database_over_source_seed(
    workspace, tmp_path: Path, monkeypatch, host_value: str | None
) -> None:
    current = workspace.app_current("api")
    _write_flask_instance_app(current)
    source_instance = current / "instance"
    _write_database(source_instance / "app.sqlite", "source-seed")
    host = workspace.app_data("api") / "instance"
    if host_value is not None:
        _write_database(host / "app.sqlite", host_value)
    manifest = _compose_manifest(has_database=True, database_type="sqlite")
    generate_compose(manifest, workspace, host_port=18000)
    old_instance = tmp_path / "old-instance"
    _write_database(old_instance / "app.sqlite", "live-container-data")

    def execute(args, **kwargs):
        assert args[:2] == ["docker", "cp"]
        shutil.copytree(old_instance, Path(args[-1]), dirs_exist_ok=True)
        return ComposeResult(args=args, returncode=0)

    runtime = DockerRuntime(workspace)
    monkeypatch.setattr(runtime, "container_id", lambda *a, **k: "old")
    monkeypatch.setattr(runtime, "container_id_strict", lambda *a, **k: "old")
    monkeypatch.setattr(runtime, "rescue_container_data", lambda *a, **k: 0)
    monkeypatch.setattr("local_webpage_access.docker_runtime._execute", execute)
    _rescue_container_data_before_rebuild(workspace, manifest, "api", runtime)
    with sqlite3.connect(host / "app.sqlite") as db:
        value = db.execute("select value from records").fetchone()[0]
    assert value == (host_value or "live-container-data")


@pytest.mark.parametrize("failure", ["query", "copy"])
def test_instance_rescue_failure_aborts_rebuild(workspace, monkeypatch, failure: str) -> None:
    """自动加挂载时，Docker 观测/拷贝故障不能被当成无数据继续删容器。"""
    _write_flask_instance_app(workspace.app_current("api"))
    manifest = _compose_manifest(has_database=True, database_type="sqlite")
    runtime = DockerRuntime(workspace)
    monkeypatch.setattr(runtime, "rescue_container_data", lambda *a, **k: 0)

    def query(*args, **kwargs):
        if failure == "query":
            raise DockerError("daemon unavailable")
        return "old"

    monkeypatch.setattr(runtime, "container_id_strict", query)
    monkeypatch.setattr(
        "local_webpage_access.docker_runtime._execute",
        lambda args, **kw: ComposeResult(args=args, returncode=1, stderr="permission denied"),
    )
    with pytest.raises(HostingError, match="救援"):
        _rescue_container_data_before_rebuild(workspace, manifest, "api", runtime)


@pytest.mark.parametrize("container_id", [None, "old"])
def test_instance_rescue_allows_absent_container_or_directory(
    workspace, monkeypatch, container_id: str | None
) -> None:
    runtime = DockerRuntime(workspace)
    monkeypatch.setattr(runtime, "container_id_strict", lambda *a, **k: container_id)
    monkeypatch.setattr(
        "local_webpage_access.docker_runtime._execute",
        lambda args, **kw: ComposeResult(
            args=args, returncode=1,
            stderr="Error response from daemon: Could not find the file /app/instance in container old",
        ),
    )
    assert runtime.rescue_bound_directory(
        "api", workspace.app_data("api") / "instance", "/app/instance"
    ) == 0


@pytest.mark.parametrize("operation", ["seed", "rescue"])
@pytest.mark.parametrize("link_type", ["root", "directory", "dangling-file"])
def test_instance_copy_does_not_follow_host_symlinks(
    tmp_path: Path, operation: str, link_type: str
) -> None:
    from local_webpage_access.docker_runtime import _merge_tree_keep_host

    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "app.sqlite").write_bytes(b"data")
    outside = tmp_path / "outside"
    outside.mkdir()
    host = tmp_path / "host"
    if link_type == "root":
        host.symlink_to(outside, target_is_directory=True)
    else:
        host.mkdir()
    if link_type == "directory":
        (host / "nested").symlink_to(outside, target_is_directory=True)
    elif link_type == "dangling-file":
        (host / "nested").mkdir()
        (host / "nested" / "app.sqlite").symlink_to(outside / "app.sqlite")
    with pytest.raises((PathError, DockerError)):
        if operation == "seed":
            _seed_instance_dir(host, source)
        else:
            _merge_tree_keep_host(source, host)
    assert not list(outside.rglob("*.sqlite"))


def test_instance_rescue_skips_explicit_volume_target(workspace) -> None:
    _write_flask_instance_app(workspace.app_current("api"))
    manifest = _compose_manifest(has_database=True, database_type="sqlite")
    manifest.container.extraVolumes = ["/custom/instance:/app/instance"]

    class Runtime:
        def rescue_container_data(self, *args, **kwargs):
            return 0

        def rescue_bound_directory(self, *args, **kwargs):
            pytest.fail("用户已显式配置 instance 挂载，不应向未使用的管理目录写入")

    _rescue_container_data_before_rebuild(workspace, manifest, "api", Runtime())


@pytest.mark.parametrize("sidecar", ["-wal", "-journal"])
def test_source_equal_database_with_host_transactions_is_not_overwritten(
    tmp_path: Path, sidecar: str
) -> None:
    """数据库主文件与种子相同，不代表 WAL/回滚日志里没有宿主事务。"""
    from local_webpage_access.docker_runtime import _merge_tree_keep_host

    source, host, staged = (tmp_path / name for name in ("source", "host", "staged"))
    for root in (source, host, staged):
        root.mkdir()
    (source / "app.sqlite").write_bytes(b"seed")
    (host / "app.sqlite").write_bytes(b"seed")
    (host / ("app.sqlite" + sidecar)).write_bytes(b"host-transaction")
    (staged / "app.sqlite").write_bytes(b"container")
    _merge_tree_keep_host(staged, host, seed_dir=source)
    assert (host / "app.sqlite").read_bytes() == b"seed"


@pytest.mark.parametrize("sidecar", ["-wal", "-shm", "-journal"])
def test_conflicting_database_does_not_receive_container_sidecars(
    tmp_path: Path, sidecar: str
) -> None:
    from local_webpage_access.docker_runtime import _merge_tree_keep_host

    staged, host = tmp_path / "staged", tmp_path / "host"
    staged.mkdir()
    host.mkdir()
    (host / "app.sqlite").write_bytes(b"host-database")
    (staged / "app.sqlite").write_bytes(b"container-database")
    (staged / ("app.sqlite" + sidecar)).write_bytes(b"container-transaction")
    _merge_tree_keep_host(staged, host)
    assert not (host / ("app.sqlite" + sidecar)).exists()
