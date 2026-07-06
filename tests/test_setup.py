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
    assert detect_platform() in {"macos", "linux", "windows", "unknown"}


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
