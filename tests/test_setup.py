"""setup 模块与 lwa setup CLI 测试。"""

from __future__ import annotations

import subprocess
from typing import Sequence

import pytest
from typer.testing import CliRunner

from local_webpage_access.cli import app
from local_webpage_access.doctor import STATUS_FAIL, STATUS_OK, STATUS_SKIP, STATUS_WARN
from local_webpage_access.setup import (
    detect_platform,
    format_setup_report,
    render_setup_script,
    run_setup,
)


def _proc(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _runner_from_map(mapping: dict[tuple[str, ...], subprocess.CompletedProcess]):
    def runner(args: Sequence[str]):
        key = tuple(args)
        if key in mapping:
            return mapping[key]
        for prefix, result in mapping.items():
            if key[: len(prefix)] == prefix:
                return result
        return _proc(127, stderr="not found")

    return runner


def test_detect_platform_returns_known_value() -> None:
    assert detect_platform() in {"macos", "linux", "wsl", "windows", "unknown"}


def test_run_setup_all_ok_with_mocked_tools() -> None:
    runner = _runner_from_map(
        {
            ("docker", "version"): _proc(0, stdout="29.6.1\n"),
            ("docker", "compose", "version"): _proc(0, stdout="v5.2.0\n"),
            ("node", "--version"): _proc(0, stdout="v24.16.0\n"),
        }
    )
    report = run_setup(static_gateway="builtin", runner=runner)
    names = {i.name: i.status for i in report.items}
    assert names["python"] == STATUS_OK
    assert names["docker"] == STATUS_OK
    assert names["docker_compose"] == STATUS_OK
    assert names["caddy"] == STATUS_SKIP
    assert names["nodejs"] in (STATUS_OK, STATUS_WARN)


def test_run_setup_fails_when_docker_too_old() -> None:
    runner = _runner_from_map(
        {
            ("docker", "version"): _proc(0, stdout="27.0.0\n"),
            ("docker", "compose", "version"): _proc(0, stdout="v5.2.0\n"),
            ("node", "--version"): _proc(0, stdout="v24.0.0\n"),
        }
    )
    report = run_setup(static_gateway="builtin", runner=runner)
    docker = next(i for i in report.items if i.name == "docker")
    assert docker.status == STATUS_FAIL
    assert not report.ready


def test_run_setup_supported_compose_v2_is_ready_with_warning() -> None:
    runner = _runner_from_map(
        {
            ("docker", "version"): _proc(0, stdout="29.6.1\n"),
            ("docker", "compose", "version"): _proc(0, stdout="2.40.3\n"),
            ("node", "--version"): _proc(0, stdout="v24.16.0\n"),
        }
    )
    report = run_setup(static_gateway="builtin", runner=runner)
    compose = next(i for i in report.items if i.name == "docker_compose")
    assert compose.status == STATUS_WARN
    assert report.ready


def test_run_setup_default_caddy_missing_is_ready_with_warning(monkeypatch) -> None:
    runner = _runner_from_map(
        {
            ("docker", "version"): _proc(0, stdout="29.6.1\n"),
            ("docker", "compose", "version"): _proc(0, stdout="v5.2.0\n"),
            ("node", "--version"): _proc(0, stdout="v24.16.0\n"),
        }
    )
    monkeypatch.setattr("local_webpage_access.doctor.shutil.which", lambda _: None)

    report = run_setup(static_gateway="caddy", runner=runner)
    caddy = next(i for i in report.items if i.name == "caddy")

    assert caddy.status == STATUS_WARN
    assert report.ready


def test_format_setup_report_mentions_next_steps() -> None:
    runner = _runner_from_map(
        {
            ("docker", "version"): _proc(0, stdout="29.6.1\n"),
            ("docker", "compose", "version"): _proc(0, stdout="v5.2.0\n"),
            ("node", "--version"): _proc(0, stdout="v24.16.0\n"),
        }
    )
    report = run_setup(static_gateway="builtin", runner=runner)
    text = format_setup_report(report)
    assert "宿主机环境检测" in text
    assert "lwa init" in text


def test_render_setup_script_per_platform() -> None:
    assert "#!/usr/bin/env bash" in render_setup_script("macos")
    assert "Docker Engine" in render_setup_script("linux")
    assert "PowerShell" in render_setup_script("windows")


def test_cli_setup_command() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["setup", "--static-gateway", "builtin"])
    assert result.exit_code in (0, 1)
    assert "宿主机环境检测" in result.output or "python" in result.output.lower()


def test_cli_setup_script_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["setup", "--script"])
    assert result.exit_code == 0
    plat = detect_platform()
    if plat == "macos":
        assert "brew install" in result.output
    elif plat == "windows":
        assert "winget" in result.output


def test_cli_setup_json() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["setup", "--json", "--static-gateway", "builtin"])
    assert result.exit_code in (0, 1)
    assert '"platform"' in result.output
    assert '"items"' in result.output


# ---- OPS-022/025：launchd 开机自启 plist 生成 ---------------------------------


def test_generate_launchd_plists_macos(tmp_path, monkeypatch) -> None:
    """macOS 下生成 daemon + manager（+ caddy）plist，内容含正确的 Label/命令/RunAtLoad。"""
    import plistlib

    from local_webpage_access.config import Config, PortPool
    from local_webpage_access.init_workspace import init_workspace
    from local_webpage_access.setup import generate_launchd_plists
    from local_webpage_access.paths import Workspace

    monkeypatch.setattr("local_webpage_access.setup.detect_platform", lambda: "macos")
    root = tmp_path / "ws"
    init_workspace(root)
    ws = Workspace(root)
    ws.ensure_workspace_dirs()
    cfg = Config(
        staticGateway="caddy",
        portPool=PortPool(start=21000, end=21050),
        managerEnabled=True,
    )
    dest = tmp_path / "LaunchAgents"
    written = generate_launchd_plists(
        root, cfg, include_caddy=True, dest_dir=dest, python_exe="/usr/local/bin/python3"
    )
    names = {name for name, _ in written}
    assert names == {"daemon", "manager", "gateway"}
    # 校验 daemon plist 内容
    daemon_plist = dict(written)["daemon"]
    data = plistlib.loads(daemon_plist.read_bytes())
    assert data["Label"] == "com.fenix.lwa.daemon"
    assert data["RunAtLoad"] is True
    # IMP-030：前台入口（--workspace），不是 detached `on`
    assert data["ProgramArguments"] == [
        "/usr/local/bin/python3",
        "-m",
        "local_webpage_access.daemon",
        "--workspace",
        str(root),
    ]
    assert data["WorkingDirectory"] == str(root)
    # IMP-030：前台监管 + KeepAlive（崩溃即拉起，修复 BUG-138）+ PATH（BUG-139）
    assert "KeepAlive" in data
    assert "PATH" in data["EnvironmentVariables"]


def test_generate_launchd_plists_omits_manager_when_disabled(tmp_path, monkeypatch) -> None:
    """managerEnabled=false 时不生成 manager plist。"""
    from local_webpage_access.config import Config, PortPool
    from local_webpage_access.init_workspace import init_workspace
    from local_webpage_access.setup import generate_launchd_plists

    monkeypatch.setattr("local_webpage_access.setup.detect_platform", lambda: "macos")
    root = tmp_path / "ws"
    init_workspace(root)
    cfg = Config(
        portPool=PortPool(start=21000, end=21050), managerEnabled=False
    )
    written = generate_launchd_plists(
        root, cfg, dest_dir=tmp_path / "LA", python_exe="/usr/bin/python3"
    )
    assert {n for n, _ in written} == {"daemon"}


def test_generate_launchd_plists_rejects_non_macos(tmp_path, monkeypatch) -> None:
    """非 macOS 抛 LifecycleError。"""
    from local_webpage_access.config import Config, PortPool
    from local_webpage_access.errors import LifecycleError
    from local_webpage_access.init_workspace import init_workspace
    from local_webpage_access.setup import generate_launchd_plists

    monkeypatch.setattr("local_webpage_access.setup.detect_platform", lambda: "linux")
    root = tmp_path / "ws"
    init_workspace(root)
    cfg = Config(portPool=PortPool(start=21000, end=21050))
    with pytest.raises(LifecycleError):
        generate_launchd_plists(root, cfg, dest_dir=tmp_path / "LA")
