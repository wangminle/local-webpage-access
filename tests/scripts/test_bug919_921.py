"""BUG-919~921：runtime recreate 前救援、救援合并失败回滚、Django 迁移
纳入副作用识别与 requiresMigrations 契约。"""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from local_webpage_access.importer import build_manifest_from_detection
from local_webpage_access.models import (
    ContainerConfig,
    DatabaseConfig,
    DesiredState,
    EntryConfig,
    InstanceManifest,
    Kind,
    Runtime,
    ResourceProfile,
    ServingMode,
    Status,
)
from local_webpage_access.scanner import Scanner

from tests.scripts._helpers import make_container_manifest


# ---- BUG-919：runtime recreate 路径同样先救出可写层数据 ----------------------


def _write_flask_instance_app(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    pkg = root / "flaskr"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "import os\n"
        "from flask import Flask\n"
        "def create_app():\n"
        "    app = Flask(__name__, instance_relative_config=True)\n"
        "    app.config['DATABASE'] = os.path.join(app.instance_path, 'flaskr.sqlite')\n"
        "    return app\n",
        encoding="utf-8",
    )


def test_recreate_container_runtime_rescues_before_replace(
    workspace, registry, config, monkeypatch
) -> None:
    """BUG-919：只改 runtimeConfigHash 的 start/restart 走 force-recreate，
    旧容器可写层里的 instance 库必须先救出，再停机替换。"""
    from unittest.mock import MagicMock

    from local_webpage_access import hosting

    workspace.ensure_app_dirs("recreate")
    _write_flask_instance_app(workspace.app_current("recreate"))
    workspace.app_compose_path("recreate").write_text("services: {}\n")
    workspace.app_env_path("recreate").write_text("FOO=bar\n")

    manifest = InstanceManifest(
        id="recreate",
        name="recreate",
        version="1",
        kind=Kind.PYTHON,
        stack=["flask"],
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.SMALL,
        status=Status.RUNNING,
        desiredState=DesiredState.RUNNING,
        hasDatabase=True,
        database=DatabaseConfig(type="sqlite"),
        container=ContainerConfig(
            projectName="lwa-recreate",
            composePath=str(workspace.app_compose_path("recreate")),
            dockerfilePath=str(workspace.app_dockerfile_path("recreate")),
            hostPort=21101,
            internalPort=8000,
            containerId="cid-old",
            imageId="sha256:old",
        ),
    )
    manifest.save(workspace.app_manifest_path("recreate"))
    registry.upsert_from_manifest(manifest)

    order: list[str] = []

    class _FakeRuntime:
        def __init__(self, ws, reg):
            self.up_kwargs = {}

        def is_running(self, iid):
            return True

        def stop(self, iid):
            order.append("stop")

        def up(self, iid, *, force_recreate=False):
            order.append("up")
            self.up_kwargs = {"force_recreate": force_recreate}

        def container_id(self, iid):
            return "cid-new"

        def image_id(self, iid):
            return "sha256:old"

        def rescue_container_data(self, iid, host_data, candidates):
            order.append("rescue_data")
            return 0

        def rescue_bound_directory(self, iid, host_dir, container_path, **kw):
            order.append(f"rescue_instance:{container_path}")
            return 0

    fake = _FakeRuntime(workspace, registry)
    monkeypatch.setattr(hosting, "DockerRuntime", MagicMock())
    hosting.DockerRuntime.ensure_available = lambda: None
    hosting.DockerRuntime.return_value = fake
    monkeypatch.setattr(
        hosting,
        "_evaluate_container_verification",
        lambda *a, **k: {"overall_status": "passed", "liveness_passed": True},
    )

    hosting.recreate_container_runtime(workspace, config, registry, "recreate")
    assert order[:2] == ["rescue_data", "rescue_instance:/app/instance"]
    assert order[2:] == ["stop", "up"]


# ---- BUG-920：救援合并在失败时撤回本次写入 -----------------------------------


def test_merge_tree_rolls_back_new_files_on_copy_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """主库复制成功后第二个文件失败（磁盘满/IO）：本次新写入全部撤回，
    重试时不会把半成品主库当成既有业务数据。"""
    from local_webpage_access import docker_runtime as dr

    staged = tmp_path / "staged"
    host = tmp_path / "host"
    staged.mkdir()
    host.mkdir()
    (staged / "app.sqlite").write_bytes(b"container-db")
    (staged / "payload.bin").write_bytes(b"payload")

    calls = {"n": 0}
    real_copyfile = dr.shutil.copyfile

    def flaky_copyfile(src, dst, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("No space left on device")
        return real_copyfile(src, dst, **kwargs)

    monkeypatch.setattr(dr.shutil, "copyfile", flaky_copyfile)
    with pytest.raises(OSError, match="No space left"):
        dr._merge_tree_keep_host(staged, host)
    assert calls["n"] == 2
    # 无论 rglob 先遍历哪个文件，本次写入都必须整体撤回
    assert not (host / "app.sqlite").exists()
    assert not (host / "payload.bin").exists()


def test_merge_tree_rolls_back_replaced_seed_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """被替换的是「与种子逐字节相同」的宿主副本时，回滚按种子恢复；
    既有宿主业务文件不受影响。"""
    from local_webpage_access import docker_runtime as dr

    seed = tmp_path / "seed"
    host = tmp_path / "host"
    staged = tmp_path / "staged"
    for d in (seed, host, staged):
        d.mkdir()
    (seed / "app.sqlite").write_bytes(b"seed-bytes")
    (host / "app.sqlite").write_bytes(b"seed-bytes")  # 与种子相同 → 可被救援替换
    (host / "keep.txt").write_text("host-owned", encoding="utf-8")
    (staged / "app.sqlite").write_bytes(b"real-container-db")
    (staged / "sidecar.bin").write_bytes(b"sidecar")

    calls = {"n": 0}
    real_copyfile = dr.shutil.copyfile

    def flaky_copyfile(src, dst, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("I/O error")
        return real_copyfile(src, dst, **kwargs)

    monkeypatch.setattr(dr.shutil, "copyfile", flaky_copyfile)
    with pytest.raises(OSError, match="I/O error"):
        dr._merge_tree_keep_host(staged, host, seed_dir=seed)
    # 全部复制成功之前不提交，失败时不再复制种子来恢复。
    assert calls["n"] == 2
    # 种子副本与宿主业务文件原样
    assert (host / "app.sqlite").read_bytes() == b"seed-bytes"
    assert (host / "keep.txt").read_text(encoding="utf-8") == "host-owned"
    assert not (host / "sidecar.bin").exists()


@pytest.mark.parametrize("failed_file", ["app.sqlite", "app.sqlite-wal"])
@pytest.mark.parametrize("with_seed", [False, True])
def test_merge_partial_sqlite_copy_failure_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_file: str, with_seed: bool
) -> None:
    """主库或 WAL 部分写入后报错，宿主原样保留；重试须找回已提交事务。"""
    from local_webpage_access import docker_runtime as dr

    staged, host, seed = (tmp_path / name for name in ("staged", "host", "seed"))
    for directory in (staged, host, seed):
        directory.mkdir()
    (host / "keep.txt").write_bytes(b"host-owned")
    with closing(sqlite3.connect(staged / "app.sqlite")) as db:
        db.execute("CREATE TABLE records(value TEXT)")
        db.commit()
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("INSERT INTO records VALUES ('committed-record')")
        db.commit()
        if with_seed:
            shutil.copyfile(staged / "app.sqlite", seed / "app.sqlite")
            shutil.copyfile(seed / "app.sqlite", host / "app.sqlite")
        original = {p.name: p.read_bytes() for p in host.iterdir()}
        real_copyfile = dr.shutil.copyfile
        real_rglob = Path.rglob

        def database_first(path, pattern):
            return iter(sorted(real_rglob(path, pattern), key=lambda p: p.as_posix()))

        def partial_copy(src, dst, **kwargs):
            if Path(src).parent == staged and Path(src).name == failed_file:
                Path(dst).write_bytes(Path(src).read_bytes()[:32])
                raise OSError("No space left after partial write")
            return real_copyfile(src, dst, **kwargs)

        with monkeypatch.context() as fault:
            fault.setattr(Path, "rglob", database_first)
            fault.setattr(dr.shutil, "copyfile", partial_copy)
            with pytest.raises(OSError, match="after partial write"):
                dr._merge_tree_keep_host(staged, host, seed_dir=seed)
        assert {p.name: p.read_bytes() for p in host.iterdir()} == original
        copied, conflicts = dr._merge_tree_keep_host(staged, host, seed_dir=seed)
        assert copied >= 2
        assert conflicts == []
        assert (host / "keep.txt").read_bytes() == b"host-owned"
        with closing(sqlite3.connect(host / "app.sqlite")) as recovered:
            assert recovered.execute("SELECT * FROM records").fetchall() == [
                ("committed-record",)
            ]


@pytest.mark.parametrize("failed_target", ["payload.bin", "zz.sqlite"])
@pytest.mark.parametrize("exception_type", [OSError, KeyboardInterrupt])
def test_merge_commit_failure_restores_originals_without_copying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_target: str, exception_type: type
) -> None:
    """提交失败时已替换的种子与新增文件均撤回，回滚无需再写文件内容。"""
    from local_webpage_access import docker_runtime as dr

    staged, host, seed = (tmp_path / name for name in ("staged", "host", "seed"))
    for directory in (staged, host, seed):
        directory.mkdir()
    for name in ("app.sqlite", "zz.sqlite"):
        (seed / name).write_bytes(b"source-seed")
        (host / name).write_bytes(b"source-seed")
        (staged / name).write_bytes(b"container-data")
    (staged / "payload.bin").write_bytes(b"new-file")
    (host / "keep.txt").write_bytes(b"host-owned")
    original = {p.name: p.read_bytes() for p in host.iterdir()}
    real_replace, real_copyfile, real_rglob = Path.replace, dr.shutil.copyfile, Path.rglob
    failed = False

    def failing_replace(path, target):
        nonlocal failed
        if not failed and Path(target) == host / failed_target:
            failed = True
            raise exception_type("commit failed")
        return real_replace(path, target)

    def no_copy_during_rollback(src, dst, **kwargs):
        assert not failed, "磁盘故障后回滚不能依赖重新复制文件内容"
        return real_copyfile(src, dst, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "replace", failing_replace)
        fault.setattr(dr.shutil, "copyfile", no_copy_during_rollback)
        fault.setattr(
            Path, "rglob", lambda path, pattern: iter(sorted(real_rglob(path, pattern)))
        )
        with pytest.raises(exception_type, match="commit failed"):
            dr._merge_tree_keep_host(staged, host, seed_dir=seed)
    assert {p.name: p.read_bytes() for p in host.iterdir()} == original
    assert dr._merge_tree_keep_host(staged, host, seed_dir=seed) == (3, [])
    assert (host / "app.sqlite").read_bytes() == b"container-data"
    assert (host / "zz.sqlite").read_bytes() == b"container-data"
    assert (host / "payload.bin").read_bytes() == b"new-file"
    assert (host / "keep.txt").read_bytes() == b"host-owned"


@pytest.mark.parametrize("rollback_error", [OSError, KeyboardInterrupt])
def test_merge_failed_rollback_preserves_backup_and_blocks_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rollback_error: type
) -> None:
    """恢复本身也失败时保留原件，下一次救援必须明确拒绝继续。"""
    from local_webpage_access import docker_runtime as dr
    from local_webpage_access.errors import DockerError

    staged, host, seed = (tmp_path / name for name in ("staged", "host", "seed"))
    for directory in (staged, host, seed):
        directory.mkdir()
    (staged / "app.sqlite").write_bytes(b"container-db")
    (host / "app.sqlite").write_bytes(b"seed-db")
    (seed / "app.sqlite").write_bytes(b"seed-db")
    real_replace = Path.replace
    commit_failed = False

    def fail_target_write(path, target):
        nonlocal commit_failed
        if Path(target) == host / "app.sqlite":
            if not commit_failed:
                commit_failed = True
                raise OSError("commit failed")
            raise rollback_error("restore failed")
        return real_replace(path, target)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "replace", fail_target_write)
        expected = DockerError if rollback_error is OSError else KeyboardInterrupt
        with pytest.raises(expected):
            dr._merge_tree_keep_host(staged, host, seed_dir=seed)
    transaction = host / ".lwa-rescue-pending"
    assert (transaction / "old" / "app.sqlite").read_bytes() == b"seed-db"
    assert (transaction / "new" / "app.sqlite").read_bytes() == b"container-db"
    with pytest.raises(DockerError, match="未完成事务"):
        dr._merge_tree_keep_host(staged, host, seed_dir=seed)
    assert (transaction / "old" / "app.sqlite").read_bytes() == b"seed-db"


def test_merge_backup_move_failure_keeps_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from local_webpage_access import docker_runtime as dr

    staged, host, seed = (tmp_path / name for name in ("staged", "host", "seed"))
    for directory in (staged, host, seed):
        directory.mkdir()
    (staged / "app.sqlite").write_bytes(b"container-db")
    (host / "app.sqlite").write_bytes(b"seed-db")
    (seed / "app.sqlite").write_bytes(b"seed-db")
    real_replace = Path.replace

    def fail_backup_move(path, target):
        if path == host / "app.sqlite":
            raise OSError("backup move failed")
        return real_replace(path, target)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "replace", fail_backup_move)
        with pytest.raises(OSError, match="backup move failed"):
            dr._merge_tree_keep_host(staged, host, seed_dir=seed)
    assert {p.name: p.read_bytes() for p in host.iterdir()} == {"app.sqlite": b"seed-db"}
    assert dr._merge_tree_keep_host(staged, host, seed_dir=seed) == (1, [])
    assert (host / "app.sqlite").read_bytes() == b"container-db"


def test_merge_rejects_reserved_transaction_path(tmp_path: Path) -> None:
    from local_webpage_access.docker_runtime import _merge_tree_keep_host
    from local_webpage_access.errors import DockerError

    staged, host = tmp_path / "staged", tmp_path / "host"
    reserved = staged / ".lwa-rescue-pending"
    reserved.mkdir(parents=True)
    (reserved / "new-0").write_bytes(b"untrusted")
    with pytest.raises(DockerError, match="保留事务路径"):
        _merge_tree_keep_host(staged, host)
    assert not list(host.iterdir())


# ---- BUG-921：Django 迁移纳入副作用识别与迁移契约 ----------------------------


def test_django_migrate_produces_side_effect_record() -> None:
    """迁移成功而必选探针失败时，Django 迁移与 Alembic 一样阻断自动降级。"""
    from local_webpage_access.hosting import (
        _collect_side_effect_records,
        _migration_command_succeeded,
    )

    manifest = make_container_manifest("dj")
    manifest.entry = EntryConfig(
        start=(
            'sh -c "python manage.py migrate --noinput '
            '&& exec gunicorn -b 0.0.0.0:8000 mysite.wsgi:application"'
        ),
    )
    records = _collect_side_effect_records(
        manifest, liveness_ok=False, verification_status="failed"
    )
    assert len(records) == 1
    assert records[0].kind == "migration"
    assert records[0].autoRecoverable is False
    assert "django" in records[0].description.lower()
    # 存活（&& 后的 gunicorn 已执行）即迁移前置已成功
    assert _migration_command_succeeded(manifest, liveness_ok=True) is True
    assert _migration_command_succeeded(manifest, liveness_ok=False) is False


def test_alembic_side_effect_record_unchanged() -> None:
    """既有 Alembic 识别不回归。"""
    from local_webpage_access.hosting import _collect_side_effect_records

    manifest = make_container_manifest("al")
    manifest.entry = EntryConfig(
        start="alembic upgrade head && python -m uvicorn app:app --host 0.0.0.0 --port 8000",
    )
    records = _collect_side_effect_records(
        manifest, liveness_ok=True, verification_status="passed"
    )
    assert len(records) == 1
    assert "alembic" in records[0].description.lower()


def _write_django_with_migrations(root: Path) -> None:
    (root / "requirements.txt").write_text("django\n", encoding="utf-8")
    (root / "manage.py").write_text("import os\n", encoding="utf-8")
    project = root / "mysite"
    project.mkdir()
    (project / "__init__.py").write_text("", encoding="utf-8")
    (project / "wsgi.py").write_text(
        "import os\napplication = os.environ.get('WSGI')\n",
        encoding="utf-8",
    )
    mig = root / "bookmarks" / "migrations"
    mig.mkdir(parents=True)
    (mig / "__init__.py").write_text("", encoding="utf-8")


def test_django_migrate_marks_contract_requires_migrations(
    workspace, tmp_path: Path
) -> None:
    """Scanner→manifest 全链路：迁移包装命令必须把 requiresMigrations
    写进能力契约，否则迁移后失败仍可能被判定可安全自动降级。"""
    project = tmp_path / "project"
    project.mkdir()
    _write_django_with_migrations(project)
    detection = Scanner().detect(project)
    manifest = build_manifest_from_detection(
        instance_id="dj",
        display_name="dj",
        detection=detection,
        workspace=workspace,
        zip_hash=None,
        name_source=None,
    )
    start = manifest.entry.start or ""
    assert "manage.py migrate" in start
    contract = manifest.capabilityContract
    assert isinstance(contract, dict)
    assert contract.get("requiresMigrations") is True


def test_no_migration_command_leaves_contract_flag_unset(
    workspace, tmp_path: Path
) -> None:
    """无迁移前置的普通 Django 项目（无 migrations 包）不误标。"""
    project = tmp_path / "plain"
    project.mkdir()
    _write_django_with_migrations(project)
    shutil.rmtree(project / "bookmarks")
    detection = Scanner().detect(project)
    manifest = build_manifest_from_detection(
        instance_id="plain",
        display_name="plain",
        detection=detection,
        workspace=workspace,
        zip_hash=None,
        name_source=None,
    )
    start = manifest.entry.start or ""
    assert "migrate" not in start
    contract = manifest.capabilityContract
    assert isinstance(contract, dict)
    assert contract.get("requiresMigrations") is not True
