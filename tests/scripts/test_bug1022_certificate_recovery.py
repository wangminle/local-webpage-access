"""BUG-1022：入口证书组在异常和无清理进程退出后的事务恢复。"""

from __future__ import annotations

import json
import os
import shutil
import ssl
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from local_webpage_access import static_gateway as gateway
from local_webpage_access.config import Config
from local_webpage_access.file_lock import try_acquire_exclusive

NAMES = ("key.pem", "cert.pem", "meta.json")
OLD_SANS = ["127.0.0.1", "192.168.1.50"]
NEW_SANS = ["127.0.0.1", "192.168.1.51"]
requires_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="需要 openssl")


def test_certificate_flush_uses_writable_file_handles(tmp_path, monkeypatch):
    """Windows 的 FlushFileBuffers 要求可写句柄，在其他平台模拟此约束。"""
    replacements = []
    for name in NAMES:
        source, destination = tmp_path / f"{name}.new", tmp_path / name
        source.write_text(f"new-{name}")
        replacements.append((source, destination))
    original_fsync = os.fsync

    def require_writable(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            os.write(fd, b"")
        original_fsync(fd)

    monkeypatch.setattr(gateway.os, "fsync", require_writable)
    gateway._replace_entry_certificate_files(replacements)
    assert [dst.read_text() for _, dst in replacements] == [f"new-{name}" for name in NAMES]


def test_certificate_lock_is_reentrant_without_releasing_outer_lock(workspace, monkeypatch):
    monkeypatch.setattr(gateway, "_ENTRY_LOCK_TIMEOUT", 0.02)
    with gateway._entry_tls_lock(workspace):
        with gateway._entry_tls_lock(workspace):
            pass
        fd = os.open(gateway.entry_tls_dir(workspace) / ".lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                try_acquire_exclusive(fd)
        finally:
            os.close(fd)


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
def test_certificate_interrupt_restores_whole_group(tmp_path, monkeypatch, name, error_type):
    replacements = []
    for item in NAMES:
        destination, source = tmp_path / item, tmp_path / f"{item}.new"
        destination.write_text(f"old-{item}")
        source.write_text(f"new-{item}")
        replacements.append((source, destination))
    failure = error_type("certificate interrupted")
    original_replace = os.replace
    interrupted = False

    def interrupt_after_replace(src, dst):
        nonlocal interrupted
        original_replace(src, dst)
        if Path(src).name == f"{name}.new" and not interrupted:
            interrupted = True
            raise failure

    monkeypatch.setattr(gateway.os, "replace", interrupt_after_replace)
    with pytest.raises(error_type) as caught:
        gateway._replace_entry_certificate_files(replacements)
    assert caught.value is failure
    assert [dst.read_text() for _, dst in replacements] == [f"old-{item}" for item in NAMES]


def _snapshot(workspace):
    directory = gateway.entry_tls_dir(workspace)
    return {name: (directory / name).read_bytes() for name in NAMES}


def _check_material(workspace, sans):
    """真实解析证书与私钥，不能只依赖 meta 判断成功。"""
    cert, key = gateway.entry_tls_cert_paths(workspace)

    def openssl(*args):
        result = subprocess.run(
            [shutil.which("openssl"), *map(str, args)], capture_output=True, timeout=10
        )
        assert result.returncode == 0, result.stderr.decode()
        return result.stdout

    assert openssl("x509", "-in", cert, "-pubkey", "-noout") == openssl(
        "pkey", "-in", key, "-pubout"
    )
    openssl("verify", "-CAfile", gateway.caddy_root_cert_path(workspace), cert)
    meta = json.loads(gateway.entry_tls_meta_path(workspace).read_text())
    assert meta["sans"] == sorted(sans)
    assert meta["rootFingerprint"] == gateway.caddy_root_cert_fingerprint(workspace)
    decoded = ssl._ssl._test_decode_cert(str(cert))
    assert {value for kind, value in decoded["subjectAltName"] if kind == "IP Address"} == set(sans)
    if os.name != "nt":
        assert key.stat().st_mode & 0o777 == 0o600
    assert not (gateway.entry_tls_dir(workspace) / ".certificate-transaction.json").exists()


_CRASH_SIGNING = """
import os, sys
from pathlib import Path
from local_webpage_access import static_gateway as gateway
from local_webpage_access.paths import Workspace
ws = Workspace(Path(sys.argv[1]))
destination = gateway.entry_tls_dir(ws) / sys.argv[2]
original_replace = os.replace
original_unlink = Path.unlink
def crash_after_commit(path, *args, **kwargs):
    original_unlink(path, *args, **kwargs)
    if str(sys.argv[2]) == 'commit' and path.name == '.certificate-transaction.json':
        os._exit(73)
def crash_after_replace(src, dst):
    original_replace(src, dst)
    if Path(dst) == destination:
        os._exit(73)
gateway.os.replace = crash_after_replace
Path.unlink = crash_after_commit
gateway.ensure_entry_tls_cert(ws, ['127.0.0.1', '192.168.1.51'])
raise SystemExit('未触发预期中断点')
"""


def _crash_signing(workspace, name):
    env = dict(os.environ, PYTHONPATH=str(Path(gateway.__file__).parents[1]))
    result = subprocess.run(
        [sys.executable, "-c", _CRASH_SIGNING, str(workspace.root), name],
        env=env, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 73, result.stdout + result.stderr


@requires_openssl
@pytest.mark.parametrize("name", (*NAMES, ".certificate-transaction.json"))
def test_next_reader_recovers_after_process_exit(workspace, name):
    assert gateway.ensure_entry_tls_cert(workspace, OLD_SANS) is not None
    before = _snapshot(workspace)
    _crash_signing(workspace, name)

    journal = gateway.entry_tls_dir(workspace) / ".certificate-transaction.json"
    assert journal.is_file()
    if os.name != "nt":
        assert journal.stat().st_mode & 0o777 == 0o600

    # 强制退出不会执行 except/finally；恢复必须依赖磁盘，而非上个进程内的状态。
    material = gateway.ensure_entry_tls_cert(workspace, OLD_SANS)
    assert material is not None
    assert _snapshot(workspace) == before
    assert material.renewed, "恢复文件后也应强制 Caddy 重新加载"
    _check_material(workspace, OLD_SANS)


@requires_openssl
@pytest.mark.parametrize("name", NAMES)
def test_first_issuance_recovers_after_process_exit(workspace, name):
    _crash_signing(workspace, name)
    assert gateway.ensure_entry_tls_cert(workspace, OLD_SANS) is not None
    _check_material(workspace, OLD_SANS)


@requires_openssl
@pytest.mark.parametrize("name", NAMES)
def test_recovery_can_itself_be_interrupted(workspace, name):
    assert gateway.ensure_entry_tls_cert(workspace, OLD_SANS) is not None
    before = _snapshot(workspace)
    _crash_signing(workspace, "meta.json")
    _crash_signing(workspace, name)
    assert gateway.ensure_entry_tls_cert(workspace, OLD_SANS) is not None
    assert _snapshot(workspace) == before
    _check_material(workspace, OLD_SANS)


@requires_openssl
def test_recovery_failure_preserves_journal_for_retry(workspace, monkeypatch):
    assert gateway.ensure_entry_tls_cert(workspace, OLD_SANS) is not None
    before = _snapshot(workspace)
    _crash_signing(workspace, "cert.pem")
    original_replace = os.replace

    def fail_recovery(src, dst):
        if Path(dst).name == "key.pem":
            raise OSError("recovery unavailable")
        return original_replace(src, dst)

    with monkeypatch.context() as patch:
        patch.setattr(gateway.os, "replace", fail_recovery)
        assert gateway.ensure_entry_tls_cert(workspace, OLD_SANS) is None
    assert gateway.ensure_entry_tls_cert(workspace, OLD_SANS) is not None
    assert _snapshot(workspace) == before
    _check_material(workspace, OLD_SANS)


@requires_openssl
def test_recovery_runs_even_without_openssl(workspace, monkeypatch):
    assert gateway.ensure_entry_tls_cert(workspace, OLD_SANS) is not None
    before = _snapshot(workspace)
    _crash_signing(workspace, "key.pem")
    monkeypatch.setattr(gateway, "_openssl_binary", lambda: None)
    assert gateway.ensure_entry_tls_cert(workspace, OLD_SANS) is None
    assert _snapshot(workspace) == before


@requires_openssl
def test_successful_commit_is_not_rolled_back_on_next_read(workspace):
    assert gateway.ensure_entry_tls_cert(workspace, OLD_SANS) is not None
    old = _snapshot(workspace)
    assert gateway.ensure_entry_tls_cert(workspace, NEW_SANS) is not None
    committed = _snapshot(workspace)
    assert committed != old
    material = gateway.ensure_entry_tls_cert(workspace, NEW_SANS)
    assert material is not None and not material.renewed
    assert _snapshot(workspace) == committed
    _check_material(workspace, NEW_SANS)


@requires_openssl
def test_exit_after_commit_keeps_complete_new_generation(workspace):
    assert gateway.ensure_entry_tls_cert(workspace, OLD_SANS) is not None
    old = _snapshot(workspace)
    _crash_signing(workspace, "commit")
    committed = _snapshot(workspace)
    assert committed != old
    material = gateway.ensure_entry_tls_cert(workspace, NEW_SANS)
    assert material is not None and not material.renewed
    assert _snapshot(workspace) == committed
    _check_material(workspace, NEW_SANS)


@pytest.mark.parametrize("recovery_error", [OSError, SystemExit])
def test_interrupted_rollback_keeps_journal_and_original_exception(
    tmp_path, monkeypatch, recovery_error,
):
    replacements = []
    for name in NAMES:
        destination, source = tmp_path / name, tmp_path / f"{name}.new"
        destination.write_text(f"old-{name}")
        source.write_text(f"new-{name}")
        replacements.append((source, destination))
    failure = KeyboardInterrupt("original interruption")
    original_replace = os.replace
    interrupted = False

    def interrupt_both(src, dst):
        nonlocal interrupted
        if interrupted and Path(dst).name in NAMES:
            raise recovery_error("rollback unavailable")
        original_replace(src, dst)
        if Path(src).name == "key.pem.new":
            interrupted = True
            raise failure

    with monkeypatch.context() as patch:
        patch.setattr(gateway.os, "replace", interrupt_both)
        with pytest.raises(KeyboardInterrupt) as caught:
            gateway._replace_entry_certificate_files(replacements)
        assert caught.value is failure
    assert (tmp_path / ".certificate-transaction.json").is_file()
    assert gateway._recover_entry_certificate_files(tmp_path)
    assert [dst.read_text() for _, dst in replacements] == [f"old-{name}" for name in NAMES]


@requires_openssl
@pytest.mark.parametrize("content", ["{", '{"version":1,"files":{"../key.pem":null}}'])
def test_invalid_journal_is_retained_and_never_reuses_certificates(workspace, content):
    assert gateway.ensure_entry_tls_cert(workspace, OLD_SANS) is not None
    before = _snapshot(workspace)
    journal = gateway.entry_tls_dir(workspace) / ".certificate-transaction.json"
    journal.write_text(content)
    assert gateway.ensure_entry_tls_cert(workspace, OLD_SANS) is None
    assert journal.read_text() == content
    assert _snapshot(workspace) == before


@requires_openssl
@pytest.mark.parametrize("operation", ["start", "reload"])
@pytest.mark.parametrize("recovery_fails", [False, True])
def test_caddy_load_recovers_and_holds_certificate_lock(
    workspace, monkeypatch, operation, recovery_fails,
):
    assert gateway.ensure_entry_tls_cert(workspace, OLD_SANS) is not None
    before = _snapshot(workspace)
    _crash_signing(workspace, "key.pem")
    instance = gateway.StaticGateway(workspace, Config(staticGateway="caddy"))
    main = instance.main_config_path()
    main.parent.mkdir(parents=True, exist_ok=True)
    main.write_text("{\n admin 127.0.0.1:2019\n}\n")
    monkeypatch.setattr(gateway, "_refuse_caddy_admin_in_pytest", lambda action: None)
    monkeypatch.setattr(gateway, "ensure_admin_port", lambda ws: gateway.LEGACY_ADMIN_PORT)
    called = []

    def check_caddy_load(*args, **kwargs):
        called.append(args)
        assert not recovery_fails, "恢复失败时不能让 Caddy 读取混合文件"
        assert _snapshot(workspace) == before
        fd = os.open(gateway.entry_tls_dir(workspace) / ".lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                try_acquire_exclusive(fd)
        finally:
            os.close(fd)
        if operation == "start":
            raise OSError("测试阻止真实启动")
        assert "--force" in args[0]
        return subprocess.CompletedProcess(args[0], 0, b"", b"")

    if recovery_fails:
        original_replace = os.replace

        def fail_recovery(src, dst):
            if Path(dst).name == "key.pem":
                raise OSError("recovery unavailable")
            return original_replace(src, dst)

        monkeypatch.setattr(gateway.os, "replace", fail_recovery)
    if operation == "start":
        monkeypatch.setattr(gateway.subprocess, "Popen", check_caddy_load)
        assert instance.caddy_start() is False
    else:
        monkeypatch.setattr(gateway.subprocess, "run", check_caddy_load)
        ok, _ = instance._reload_once()
        assert ok is not recovery_fails
    assert bool(called) is not recovery_fails
