"""IMP-031 / IMP-032：宿主机 Docker/Caddy 安装脚本定位与装配编排。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable
from unittest.mock import MagicMock

import pytest

from local_webpage_access.host_bootstrap import (
    ComponentNeed,
    DockerEngineState,
    DockerOfferResult,
    detect_caddy,
    detect_docker_compose,
    detect_docker_engine,
    plan_full_install,
    resolve_install_script,
    resolve_profile,
    run_full_bootstrap,
    should_offer_docker_install,
    maybe_offer_docker_install,
)
from local_webpage_access.version_requirements import (
    MIN_CADDY_VERSION,
    MIN_COMPOSE_VERSION,
    MIN_DOCKER_VERSION,
)


def _proc(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_resolve_install_script_docker_linux() -> None:
    path = resolve_install_script("docker", "linux")
    assert path.is_file()
    assert path.name == "install-docker-linux.sh"


def test_resolve_install_script_docker_macos() -> None:
    path = resolve_install_script("docker", "macos")
    assert path.is_file()
    assert path.name == "install-docker-macos.sh"


def test_resolve_install_script_caddy() -> None:
    assert resolve_install_script("caddy", "linux").name == "install-caddy-linux.sh"
    assert resolve_install_script("caddy", "macos").name == "install-caddy-macos.sh"


def test_resolve_install_script_wsl_uses_linux() -> None:
    assert resolve_install_script("docker", "wsl").name == "install-docker-linux.sh"


def test_resolve_profile_default_when_neither() -> None:
    assert resolve_profile(default=False, full=False) == "default"


def test_resolve_profile_rejects_both() -> None:
    with pytest.raises(ValueError, match="互斥"):
        resolve_profile(default=True, full=True)


def test_resolve_profile_full() -> None:
    assert resolve_profile(default=False, full=True) == "full"


def test_detect_docker_engine_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.shutil.which", lambda _: None
    )
    state = detect_docker_engine(runner=lambda _: _proc(127))
    assert state.status == "missing"
    assert should_offer_docker_install(state) is True


def test_detect_docker_engine_daemon_down(monkeypatch) -> None:
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.shutil.which", lambda cmd: "/usr/bin/docker"
    )

    def runner(args):
        if args[-1] == "{{.Client.Version}}":
            return _proc(0, "29.1.0\n")
        return _proc(1, stderr="Cannot connect to the Docker daemon")

    state = detect_docker_engine(runner=runner)
    assert state.status == "daemon_down"
    assert should_offer_docker_install(state) is False


def test_detect_docker_engine_outdated(monkeypatch) -> None:
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.shutil.which", lambda _: "/bin/docker"
    )

    def runner(args):
        if "Server" in args[-1]:
            return _proc(0, "27.0.0\n")
        return _proc(0, "27.0.0\n")

    state = detect_docker_engine(runner=runner)
    assert state.status == "outdated"
    assert state.version == "27.0.0"
    assert should_offer_docker_install(state) is False  # 询问升级路径，不默认重装


def test_detect_docker_engine_ok(monkeypatch) -> None:
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.shutil.which", lambda _: "/bin/docker"
    )

    def runner(args):
        return _proc(0, f"{MIN_DOCKER_VERSION}\n")

    state = detect_docker_engine(runner=runner)
    assert state.status == "ok"
    assert should_offer_docker_install(state) is False


def test_detect_compose_and_caddy(monkeypatch) -> None:
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.shutil.which",
        lambda cmd: "/bin/caddy" if cmd == "caddy" else "/bin/docker",
    )

    def runner(args):
        if args[:3] == ["docker", "compose", "version"]:
            return _proc(0, f"{MIN_COMPOSE_VERSION}\n")
        if args[0] == "caddy":
            return _proc(0, f"v{MIN_CADDY_VERSION}\n")
        return _proc(127)

    assert detect_docker_compose(runner=runner).status == "ok"
    assert detect_caddy(runner=runner).status == "ok"


def test_plan_full_install_lists_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.detect_docker_engine",
        lambda **_: DockerEngineState(status="missing"),
    )
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.detect_docker_compose",
        lambda **_: ComponentNeed(name="compose", status="missing"),
    )
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.detect_caddy",
        lambda **_: ComponentNeed(name="caddy", status="missing"),
    )
    plan = plan_full_install(platform="linux")
    kinds = [p.kind for p in plan]
    assert "docker" in kinds
    assert "caddy" in kinds


def test_run_full_bootstrap_requires_yes_without_tty(monkeypatch) -> None:
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.plan_full_install",
        lambda **_: [
            MagicMock(kind="docker", script=Path("/tmp/x.sh"), reason="missing")
        ],
    )
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap._stdin_is_interactive",
        lambda: False,
    )
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _proc(0)

    result = run_full_bootstrap(
        platform="linux",
        yes=False,
        confirm=None,
        runner=fake_run,
    )
    assert result.ok is False
    assert result.skipped_no_confirm is True
    assert calls == []


def test_run_full_bootstrap_yes_runs_scripts(monkeypatch, tmp_path: Path) -> None:
    script = tmp_path / "install-docker-linux.sh"
    script.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.plan_full_install",
        lambda **_: [
            type("P", (), {"kind": "docker", "script": script, "reason": "missing"})()
        ],
    )
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.detect_docker_engine",
        lambda **_: DockerEngineState(status="ok", version=MIN_DOCKER_VERSION),
    )
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.detect_docker_compose",
        lambda **_: ComponentNeed(name="compose", status="ok", version=MIN_COMPOSE_VERSION),
    )
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.detect_caddy",
        lambda **_: ComponentNeed(name="caddy", status="ok", version=MIN_CADDY_VERSION),
    )
    ran: list[Path] = []

    def fake_run(cmd, **kwargs):
        ran.append(Path(cmd[1]))
        return _proc(0)

    result = run_full_bootstrap(
        platform="linux",
        yes=True,
        confirm=lambda _msg: False,
        runner=fake_run,
    )
    assert result.ok is True
    assert ran == [script]


def test_maybe_offer_propagates_script_failure(monkeypatch, tmp_path: Path) -> None:
    """BUG-197：脚本非零退出时 script_ok=False。"""
    script = tmp_path / "install-docker-linux.sh"
    script.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.detect_docker_engine",
        lambda **_: DockerEngineState(status="missing"),
    )
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.resolve_install_script",
        lambda *a, **k: script,
    )
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap._stdin_is_interactive",
        lambda: False,
    )

    def fake_run(cmd, **kwargs):
        return _proc(1)

    result = maybe_offer_docker_install(install_docker=True, runner=fake_run)
    assert isinstance(result, DockerOfferResult)
    assert result.attempted is True
    assert result.script_ok is False
    assert result.recheck_ok is False


def test_maybe_offer_rechecks_after_success(monkeypatch, tmp_path: Path) -> None:
    """BUG-197：脚本成功后复检 Engine/Compose。"""
    script = tmp_path / "install-docker-linux.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    states = iter(
        [
            DockerEngineState(status="missing"),
            DockerEngineState(status="ok", version=MIN_DOCKER_VERSION),
        ]
    )
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.detect_docker_engine",
        lambda **_: next(states),
    )
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.detect_docker_compose",
        lambda **_: ComponentNeed(
            name="compose", status="ok", version=MIN_COMPOSE_VERSION
        ),
    )
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.resolve_install_script",
        lambda *a, **k: script,
    )

    result = maybe_offer_docker_install(
        install_docker=True, runner=lambda *a, **k: _proc(0)
    )
    assert result.attempted is True
    assert result.script_ok is True
    assert result.recheck_ok is True
