"""IMP-031 / IMP-032：宿主机 Docker/Caddy 安装脚本定位与装配编排。"""

from __future__ import annotations

import subprocess
from pathlib import Path
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
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


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
    monkeypatch.setattr("local_webpage_access.host_bootstrap.shutil.which", lambda _: None)
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
    monkeypatch.setattr("local_webpage_access.host_bootstrap.shutil.which", lambda _: "/bin/docker")

    def runner(args):
        if "Server" in args[-1]:
            return _proc(0, "27.0.0\n")
        return _proc(0, "27.0.0\n")

    state = detect_docker_engine(runner=runner)
    assert state.status == "outdated"
    assert state.version == "27.0.0"
    assert should_offer_docker_install(state) is False  # 询问升级路径，不默认重装


def test_detect_docker_engine_ok(monkeypatch) -> None:
    monkeypatch.setattr("local_webpage_access.host_bootstrap.shutil.which", lambda _: "/bin/docker")

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


def test_run_full_bootstrap_requires_yes_without_tty(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.plan_full_install",
        lambda **_: [MagicMock(kind="docker", script=Path("/tmp/x.sh"), reason="missing")],
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
        workspace_root=tmp_path,
    )
    assert result.ok is False
    assert result.skipped_no_confirm is True
    assert calls == []


def test_run_full_bootstrap_yes_runs_scripts(monkeypatch, tmp_path: Path) -> None:
    from local_webpage_access.capability import CapabilityReport

    script = tmp_path / "install-docker-linux.sh"
    script.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.plan_full_install",
        lambda **_: [type("P", (), {"kind": "docker", "script": script, "reason": "missing"})()],
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
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap._try_start_backends_for_capability",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "local_webpage_access.capability.collect_capability_report",
        lambda **_: CapabilityReport(profile="full", overall="ready"),
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
        workspace_root=tmp_path,
    )
    assert result.ok is True
    assert ran == [script]


def test_run_full_bootstrap_requires_initialized_workspace(monkeypatch) -> None:
    """BUG-248：没有工作区时不得以 components-only 结果假报 Full ready。"""
    called = {"plan": False}

    def fake_plan(**kwargs):
        called["plan"] = True
        return []

    monkeypatch.setattr("local_webpage_access.host_bootstrap.plan_full_install", fake_plan)
    result = run_full_bootstrap(platform="linux", yes=True, workspace_root=None)
    assert result.ok is False
    assert result.overall == "unready"
    assert result.exit_code == 1
    assert called["plan"] is False
    assert any("先执行 lwa init" in msg for msg in result.messages)


def test_run_full_bootstrap_resume_reinstalls_missing_components(
    monkeypatch, tmp_path: Path
) -> None:
    """BUG-242：已记 components_installed 但组件缺失时 resume 必须重装。"""
    from local_webpage_access.capability import CapabilityReport, save_profile_state

    script = tmp_path / "install-caddy-linux.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    save_profile_state(
        tmp_path,
        {
            "profile": "full",
            "completedSteps": ["components_installed"],
        },
    )
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.plan_full_install",
        lambda **_: [type("P", (), {"kind": "caddy", "script": script, "reason": "missing"})()],
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
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap._try_start_backends_for_capability",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "local_webpage_access.capability.collect_capability_report",
        lambda **_: CapabilityReport(profile="full", overall="ready"),
    )
    ran: list[Path] = []
    result = run_full_bootstrap(
        platform="linux",
        yes=True,
        resume=True,
        workspace_root=tmp_path,
        runner=lambda cmd, **_: ran.append(Path(cmd[1])) or _proc(0),
    )
    assert result.ok is True
    assert ran == [script]


def test_run_full_bootstrap_rejects_without_backend_capability_loop(
    monkeypatch, tmp_path: Path
) -> None:
    """BUG-234：有工作区时不得仅凭 CLI Docker/Caddy 二进制假绿成功退出。"""
    from local_webpage_access.capability import CapabilityReport
    from local_webpage_access.config import example_config_text
    from local_webpage_access.paths import Workspace

    ws = Workspace(tmp_path)
    ws.ensure_workspace_dirs()
    ws.config_path.write_text(example_config_text(), encoding="utf-8")

    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.plan_full_install",
        lambda **_: [],
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
    monkeypatch.setattr(
        "local_webpage_access.capability.probe_docker_access_state",
        lambda: "ready",
    )
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap._try_start_backends_for_capability",
        lambda *_a, **_k: None,
    )
    # CLI ready，但 manager/daemon/gateway 仍 unknown → overall unready
    monkeypatch.setattr(
        "local_webpage_access.capability.collect_capability_report",
        lambda **kwargs: CapabilityReport(
            profile="full",
            overall="unready",
            cli_docker_access="ready",
            caddy_binary="ready",
            manager_docker_access="unknown",
            daemon_docker_access="unknown",
            gateway_access="unknown",
            action="启动 manager/daemon 后 resume",
        ),
    )

    result = run_full_bootstrap(
        platform="linux",
        yes=True,
        workspace_root=tmp_path,
        runner=lambda *a, **k: _proc(0),
    )
    assert result.ok is False
    assert result.exit_code == 1
    assert result.overall == "unready"
    assert any("强制闭环" in m or "能力验收未通过" in m for m in result.messages)
    from local_webpage_access.config import load_config

    assert load_config(ws).profile == "default"


def test_persist_full_config_only_writes_profile_when_ready(tmp_path: Path) -> None:
    """BUG-305：验收失败时不得把 profile 持久化为 full。"""
    from local_webpage_access.config import example_config_text, load_config
    from local_webpage_access.host_bootstrap import _persist_full_config
    from local_webpage_access.paths import Workspace

    ws = Workspace(tmp_path)
    ws.ensure_workspace_dirs()
    ws.config_path.write_text(example_config_text(), encoding="utf-8")
    assert load_config(ws).profile == "default"

    _persist_full_config(tmp_path, "lwa", ready=False)
    assert load_config(ws).profile == "default"

    _persist_full_config(tmp_path, "lwa", ready=True)
    assert load_config(ws).profile == "full"


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


def test_maybe_offer_script_failure_includes_stderr(monkeypatch, tmp_path: Path) -> None:
    """审查 L5：Docker 安装脚本失败须带回 stderr，便于排障。"""
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
        return _proc(1, stderr="apt-get: package not found")

    result = maybe_offer_docker_install(install_docker=True, runner=fake_run)
    joined = "\n".join(result.messages)
    assert "exit 1" in joined
    assert "apt-get: package not found" in joined


def test_run_full_bootstrap_script_failure_includes_stderr(monkeypatch, tmp_path: Path) -> None:
    """审查 L5：full bootstrap 脚本失败消息须含 stderr 摘要。"""
    script = tmp_path / "install-caddy-linux.sh"
    script.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.plan_full_install",
        lambda **_: [type("P", (), {"kind": "caddy", "script": script, "reason": "missing"})()],
    )
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap._stdin_is_interactive",
        lambda: False,
    )

    def fake_run(cmd, **kwargs):
        return _proc(7, stderr="caddy: unsupported distro")

    result = run_full_bootstrap(
        platform="linux",
        yes=True,
        confirm=None,
        runner=fake_run,
        workspace_root=tmp_path,
    )
    assert result.ok is False
    joined = "\n".join(result.messages)
    assert "脚本失败" in joined
    assert "exit 7" in joined
    assert "caddy: unsupported distro" in joined


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
        lambda **_: ComponentNeed(name="compose", status="ok", version=MIN_COMPOSE_VERSION),
    )
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.resolve_install_script",
        lambda *a, **k: script,
    )

    result = maybe_offer_docker_install(install_docker=True, runner=lambda *a, **k: _proc(0))
    assert result.attempted is True
    assert result.script_ok is True
    assert result.recheck_ok is True


# ---- issue #29：resume 验收遇后台 Docker 缓存未就绪时触发即时重探 -------------


def _patch_full_bootstrap_components_ok(monkeypatch) -> None:
    """公共 mock：组件全部就绪、无需安装、后台拉起短路。"""
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.plan_full_install",
        lambda **_: [],
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
    monkeypatch.setattr(
        "local_webpage_access.capability.probe_docker_access_state",
        lambda: "ready",
    )
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap._try_start_backends_for_capability",
        lambda *_a, **_k: None,
    )


def _write_minimal_workspace(tmp_path: Path) -> None:
    from local_webpage_access.config import example_config_text
    from local_webpage_access.paths import Workspace

    ws = Workspace(tmp_path)
    ws.ensure_workspace_dirs()
    ws.config_path.write_text(example_config_text(), encoding="utf-8")


def test_run_full_bootstrap_resume_triggers_manager_refresh(
    monkeypatch, tmp_path: Path
) -> None:
    """issue #29：CLI ready 而 manager/daemon 缓存 daemon_unavailable 时，
    resume 须触发 manager 即时重探并等 daemon 快探收敛后复验为 ready。"""
    from local_webpage_access.capability import CapabilityReport, load_profile_state

    _write_minimal_workspace(tmp_path)
    _patch_full_bootstrap_components_ok(monkeypatch)

    reports = iter(
        [
            CapabilityReport(
                profile="full",
                overall="unready",
                cli_docker_access="ready",
                manager_docker_access="daemon_unavailable",
                daemon_docker_access="daemon_unavailable",
                caddy_binary="ready",
                caddy_runtime="ready",
                caddy_owner="lwa_service_user",
                caddy_workspace_access="ready",
                gateway_access="ready",
                action="执行：lwa doctor --profile full 与 lwa setup --full --resume",
            ),
            CapabilityReport(
                profile="full",
                overall="ready",
                cli_docker_access="ready",
                manager_docker_access="ready",
                daemon_docker_access="ready",
                caddy_binary="ready",
                caddy_runtime="ready",
                caddy_owner="lwa_service_user",
                caddy_workspace_access="ready",
                gateway_access="ready",
            ),
        ]
    )
    monkeypatch.setattr(
        "local_webpage_access.capability.collect_capability_report",
        lambda **_: next(reports),
    )
    called: dict[str, bool] = {"refresh": False, "await": False}

    def fake_refresh(root, messages):  # noqa: ANN001, ANN202
        called["refresh"] = True
        messages.append("已触发本机 manager 即时能力重探（/api/capability?refresh=true）。")
        return True

    def fake_await(root, **kwargs):  # noqa: ANN001, ANN202
        called["await"] = True
        return True

    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap._request_manager_capability_refresh",
        fake_refresh,
    )
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap._await_daemon_capability_ready",
        fake_await,
    )

    result = run_full_bootstrap(
        platform="linux",
        yes=True,
        resume=True,
        workspace_root=tmp_path,
        runner=lambda *a, **k: _proc(0),
    )
    assert result.ok is True
    assert result.exit_code == 0
    assert result.overall == "ready"
    assert called["refresh"] is True
    assert called["await"] is True
    assert any("即时能力重探" in m for m in result.messages)
    # 验收通过后持久化状态不得残留旧建议文案
    assert load_profile_state(tmp_path).get("action") is None


def test_run_full_bootstrap_resume_refresh_failure_keeps_real_blocker(
    monkeypatch, tmp_path: Path
) -> None:
    """issue #29：manager 重探触发失败时不空转，按原缓存给出真实失败原因。"""
    from local_webpage_access.capability import CapabilityReport

    _write_minimal_workspace(tmp_path)
    _patch_full_bootstrap_components_ok(monkeypatch)
    monkeypatch.setattr(
        "local_webpage_access.capability.collect_capability_report",
        lambda **_: CapabilityReport(
            profile="full",
            overall="unready",
            cli_docker_access="ready",
            manager_docker_access="daemon_unavailable",
            daemon_docker_access="daemon_unavailable",
            caddy_binary="ready",
            caddy_runtime="ready",
            caddy_owner="lwa_service_user",
            caddy_workspace_access="ready",
            gateway_access="ready",
            action="执行：lwa doctor --profile full 与 lwa setup --full --resume",
        ),
    )

    def fake_refresh(root, messages):  # noqa: ANN001, ANN202
        messages.append("触发 manager 能力重探未成功（继续按缓存验收）：connection refused")
        return False

    def no_await(root, **kwargs):  # noqa: ANN001, ANN202
        raise AssertionError("manager 重探未触发时不应等待 daemon 缓存")

    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap._request_manager_capability_refresh",
        fake_refresh,
    )
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap._await_daemon_capability_ready",
        no_await,
    )

    result = run_full_bootstrap(
        platform="linux",
        yes=True,
        resume=True,
        workspace_root=tmp_path,
        runner=lambda *a, **k: _proc(0),
    )
    assert result.ok is False
    assert result.exit_code == 1
    assert any("触发 manager 能力重探未成功" in m for m in result.messages)
    # 失败明细仍列出真实阻塞字段（daemonDocker=daemon_unavailable）
    assert any("daemonDocker=daemon_unavailable" in m for m in result.messages)


def test_run_full_bootstrap_no_refresh_when_cli_docker_not_ready(
    monkeypatch, tmp_path: Path
) -> None:
    """issue #29：CLI 视角 Docker 也未就绪时不触发 manager 重探（真阻塞，非缓存陈旧）。"""
    from local_webpage_access.capability import CapabilityReport

    _write_minimal_workspace(tmp_path)
    _patch_full_bootstrap_components_ok(monkeypatch)
    monkeypatch.setattr(
        "local_webpage_access.capability.probe_docker_access_state",
        lambda: "daemon_unavailable",
    )
    monkeypatch.setattr(
        "local_webpage_access.capability.collect_capability_report",
        lambda **_: CapabilityReport(
            profile="full",
            overall="unready",
            cli_docker_access="daemon_unavailable",
            manager_docker_access="daemon_unavailable",
            daemon_docker_access="daemon_unavailable",
            action="执行：lwa doctor --profile full 与 lwa setup --full --resume",
        ),
    )

    def no_refresh(root, messages):  # noqa: ANN001, ANN202
        raise AssertionError("CLI Docker 未就绪时不应触发 manager 重探")

    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap._request_manager_capability_refresh",
        no_refresh,
    )

    result = run_full_bootstrap(
        platform="linux",
        yes=True,
        resume=True,
        workspace_root=tmp_path,
        runner=lambda *a, **k: _proc(0),
    )
    assert result.ok is False
    assert result.exit_code == 1


def test_await_daemon_capability_ready(tmp_path: Path) -> None:
    """issue #29：daemon 缓存就绪立即返回；仍 pending 且有界超时后返回 False。"""
    from local_webpage_access.capability import CapabilityReport, write_capability_cache
    from local_webpage_access.host_bootstrap import _await_daemon_capability_ready

    write_capability_cache(
        tmp_path,
        "daemon",
        CapabilityReport(daemon_docker_access="ready", details={"role": "daemon"}),
    )
    assert _await_daemon_capability_ready(tmp_path, timeout=5.0, interval=0.01) is True

    write_capability_cache(
        tmp_path,
        "daemon",
        CapabilityReport(
            daemon_docker_access="daemon_unavailable", details={"role": "daemon"}
        ),
    )
    assert _await_daemon_capability_ready(tmp_path, timeout=0.0, interval=0.01) is False


def test_request_manager_capability_refresh_brackets_ipv6_host(
    tmp_path: Path, monkeypatch
) -> None:
    """BUG-628：managerHost=:: 时重探 URL 必须是 http://[::1]:port/...，不能是 ::1 裸字面。"""
    from types import SimpleNamespace

    from local_webpage_access.host_bootstrap import _request_manager_capability_refresh
    from local_webpage_access.paths import Workspace

    ws = Workspace(tmp_path)
    ws.ensure_workspace_dirs()
    ws.config_path.write_text("profile: full\n", encoding="utf-8")

    captured: dict[str, str] = {}

    def fake_urlopen(req, timeout=None):  # noqa: ANN001, ANN202
        captured["url"] = req.full_url
        raise AssertionError("stop-after-capture")

    monkeypatch.setattr(
        "local_webpage_access.config.load_config",
        lambda _ws: SimpleNamespace(managerHost="::", managerPort=17800),
    )
    monkeypatch.setattr("local_webpage_access.probe.urlopen_direct", fake_urlopen)

    messages: list[str] = []
    assert _request_manager_capability_refresh(tmp_path, messages) is False
    assert "url" in captured
    assert captured["url"].startswith("http://[::1]:17800/api/capability?refresh=true")
    assert "http://::1:" not in captured["url"]
