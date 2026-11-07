"""迁移恢复服务范围及反引号构建命令回归。"""

from __future__ import annotations

import functools
import os
import plistlib
import subprocess

import pytest

from local_webpage_access import autostart as asm, workspace_migrate as wm
from local_webpage_access.config import Config
from local_webpage_access.hosting import (
    _frontend_needs_pm_shims,
    _resolve_host_build_command,
    _split_shell_and_segments,
)
from local_webpage_access.paths import Workspace
from tests.scripts.test_autostart import _fake_runner


@pytest.mark.parametrize("platform", ["macos", "linux"])
@pytest.mark.parametrize("gateway,manager", [
    ("builtin", False), ("builtin", True), ("caddy", False), ("caddy", True),
])
def test_migration_and_rollback_keep_config_disabled_units_off(
    tmp_path, monkeypatch, platform, gateway, manager
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    monkeypatch.setattr(asm, "detect_platform", lambda: platform)
    monkeypatch.setattr(asm, "systemd_available", lambda: True)
    monkeypatch.setattr(asm, "_migrate_detached_for_supervision", lambda *a, **k: True)
    monkeypatch.setattr(asm, "_prepare_daemon_for_supervision", lambda *a, **k: None)
    monkeypatch.setattr(
        "local_webpage_access.static_gateway.StaticGateway._sync_main_config",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "local_webpage_access.gateway_service.maybe_start_gateway", lambda *a, **k: None
    )
    old, new = tmp_path / "old", tmp_path / "new"
    ws = Workspace(old)
    ws.ensure_workspace_dirs()
    asm.install(
        ws, Config(staticGateway="caddy", managerEnabled=True),
        with_caddy=True, enable=False, linger=False,
    )
    backend = asm.select_backend()
    # 模拟历史 UNKNOWN 单元；repair 必须保留、重写，配置关闭时保持停用。
    for name in ("manager", "gateway"):
        path = backend.unit_path(name)
        if platform == "macos":
            raw = plistlib.loads(path.read_bytes())
            del raw["WorkingDirectory"]
            path.write_bytes(plistlib.dumps(raw))
        else:
            path.write_text("\n".join(
                line for line in path.read_text().splitlines()
                if not line.startswith("WorkingDirectory=")
            ) + "\n", encoding="utf-8")

    record = []
    runner = _fake_runner(record)
    monkeypatch.setattr(asm, "repair", functools.partial(asm.repair, runner=runner))
    monkeypatch.setattr(asm, "enable", functools.partial(asm.enable, runner=runner))
    config = Config(staticGateway=gateway, managerEnabled=manager)
    snapshot = wm.MigrateSnapshot(
        autostart_installed=["daemon", "manager", "gateway"], workspace_root=str(old)
    )
    expected = {"daemon"}
    if manager:
        expected.add("manager")
    if gateway == "caddy":
        expected.add("gateway")
    for source, destination in ((old, new), (new, old)):
        source.rename(destination)
        ws = Workspace(destination)
        actions = wm.regenerate_after_move(ws, config, snapshot=snapshot, moved_from=source)
        assert "autostart_repair" in actions
        for name in asm.ALL_SERVICES:
            assert backend.is_enabled(name, runner) is (name in expected)
        # repair 已启用目标服务；先停用才能证明 restore 真正恢复了它们，
        # 避免 restore 变成空操作时测试仍然通过。
        for name in expected:
            _outcomes, ok = backend.disable(name, runner)
            assert ok
        record.clear()
        assert wm.restore_instances(ws, config, None, snapshot) == []
        for name in asm.ALL_SERVICES:
            assert asm.unit_points_to_workspace(backend, name, destination)
            assert backend.is_enabled(name, runner) is (name in expected)
            assert backend.is_loaded(name, runner) is (name in expected)
        for name in set(asm.ALL_SERVICES) - expected:
            assert not any(
                any(name in part for part in cmd) and "enable" in cmd
                for cmd in record
            )


@pytest.mark.parametrize("platform", ["macos", "linux"])
@pytest.mark.parametrize("restore_daemon", [False, True])
def test_restore_leaves_foreign_and_non_snapshot_units_untouched(
    tmp_path, monkeypatch, platform, restore_daemon
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    monkeypatch.setattr(asm, "detect_platform", lambda: platform)
    monkeypatch.setattr(asm, "systemd_available", lambda: True)
    monkeypatch.setattr(asm, "_migrate_detached_for_supervision", lambda *a, **k: True)
    monkeypatch.setattr(
        "local_webpage_access.gateway_service.maybe_start_gateway", lambda *a, **k: None
    )
    ws = Workspace(tmp_path / "ws")
    ws.ensure_workspace_dirs()
    backend = asm.select_backend()
    for name in asm.ALL_SERVICES:
        root = tmp_path / "other-workspace" if name == "gateway" else ws.root
        backend.write_unit(name, backend.render(name, python_exe="/py", workspace_root=root))
    original_gateway = backend.unit_path("gateway").read_bytes()
    record = []
    runner = _fake_runner(record)
    for name in ("daemon", "manager"):
        backend.disable(name, runner)
    record.clear()
    monkeypatch.setattr(asm, "enable", functools.partial(asm.enable, runner=runner))
    snapshot = wm.MigrateSnapshot(
        autostart_installed=["gateway", "daemon"] if restore_daemon else ["gateway"]
    )
    wm.restore_instances(ws, Config(staticGateway="caddy", managerEnabled=True), None, snapshot)
    assert backend.unit_path("gateway").read_bytes() == original_gateway
    assert not any(any(name in part for part in cmd) for cmd in record
                   for name in ("manager", "gateway"))
    assert backend.is_enabled("manager", runner) is False
    assert backend.is_enabled("daemon", runner) is restore_daemon


_BACKTICK_PREFIXES = [
    '''echo "`printf "'"`"''',
    '''echo `printf '"'`''',
    '''echo "$(printf '%s' "`printf "'"`")"''',
    '''echo "`printf '%s' "$(printf "'")"`"''',
    r'''echo "\`"''',
    '''echo '`literal && pnpm run nested`' ''',
    '''echo "`printf '%s' 'a && b'`"''',
    r'''echo "`printf '%s' \`printf x\``"''',
]


@pytest.mark.parametrize("prefix", _BACKTICK_PREFIXES)
def test_backtick_quotes_preserve_top_level_build_segments(tmp_path, monkeypatch, prefix) -> None:
    command = prefix + " && pnpm run build"
    assert _split_shell_and_segments(command) == [prefix + " ", " pnpm run build"]
    monkeypatch.setattr(
        "local_webpage_access.hosting.shutil.which",
        lambda name, path=None: "/fake/corepack" if name == "corepack" else None,
    )
    assert _resolve_host_build_command(command, tmp_path, pinned=("pnpm", "8.15.9")) == (
        prefix + " && corepack pnpm@8.15.9 run build"
    )


@pytest.mark.skipif(os.name == "nt", reason="需要 POSIX sh 执行反引号语法")
@pytest.mark.parametrize("prefix", _BACKTICK_PREFIXES)
def test_backtick_build_executes_pinned_pm_instead_of_global_pm(tmp_path, prefix) -> None:
    for name, body in (
        ("corepack", 'echo "PINNED $*"; test "$1" = pnpm@8.15.9'),
        ("pnpm", 'echo WRONG_GLOBAL_PM >&2; exit 42'),
        ("yarn", "exit 0"),
    ):
        tool = tmp_path / name
        tool.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        tool.chmod(0o755)
    command = prefix + " && pnpm run build"
    resolved = _resolve_host_build_command(
        command, tmp_path, pinned=("pnpm", "8.15.9"), path=str(tmp_path)
    )
    assert not _frontend_needs_pm_shims(
        tmp_path, "corepack pnpm@8.15.9 install", resolved, path=str(tmp_path)
    )
    result = subprocess.run(
        ["/bin/sh", "-c", resolved], capture_output=True, text=True,
        env={**os.environ, "PATH": str(tmp_path)}, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "PINNED pnpm@8.15.9 run build" in result.stdout
    assert "WRONG_GLOBAL_PM" not in result.stderr
