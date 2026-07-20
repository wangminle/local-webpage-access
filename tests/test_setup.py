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
    win = render_setup_script("windows")
    assert "WSL2" in win
    assert "不支持" in win or "原生" in win


def test_cli_setup_command() -> None:
    runner = CliRunner()
    result = runner.invoke(
        app, ["setup", "--static-gateway", "builtin", "--no-install-docker"]
    )
    assert result.exit_code in (0, 1)
    assert "宿主机环境检测" in result.output or "python" in result.output.lower()


def test_cli_setup_script_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["setup", "--script"])
    assert result.exit_code == 0
    assert "install-docker-" in result.output
    plat = detect_platform()
    if plat == "macos":
        assert "brew install" in result.output
    elif plat == "windows":
        # IMP-036：原生 Windows 应提示改用 WSL2（门禁也可能非零退出）
        assert (
            "WSL2" in result.output
            or "不支持" in result.output
            or result.exit_code != 0
        )


def test_cli_setup_rejects_default_and_full() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["setup", "--default", "--full"])
    assert result.exit_code == 2
    assert "互斥" in result.output


def test_cli_setup_full_without_yes_non_tty(monkeypatch) -> None:
    """非 TTY 的 --full 无 --yes 应跳过安装并以非零退出（若有待装项）。"""
    from local_webpage_access.host_bootstrap import FullBootstrapResult, InstallPlanItem
    from pathlib import Path

    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.run_full_bootstrap",
        lambda **kwargs: FullBootstrapResult(
            ok=False,
            planned=[
                InstallPlanItem(
                    kind="docker",
                    script=Path("/tmp/x.sh"),
                    reason="missing",
                )
            ],
            ran=[],
            messages=["非交互终端且未传 --yes"],
            skipped_no_confirm=True,
        ),
    )
    runner = CliRunner()
    result = runner.invoke(app, ["setup", "--full"])
    assert result.exit_code == 1
    assert "--yes" in result.output or "非交互" in result.output


def test_cli_setup_json() -> None:
    runner = CliRunner()
    result = runner.invoke(
        app, ["setup", "--json", "--static-gateway", "builtin", "--no-install-docker"]
    )
    assert result.exit_code in (0, 1)
    assert '"platform"' in result.output
    assert '"items"' in result.output


def test_cli_setup_full_json_stdout_is_pure_json(monkeypatch) -> None:
    """BUG-196：--full --json 的 stdout 必须可被 json.loads（不被标题/过程散文污染）。"""
    import json

    from local_webpage_access.host_bootstrap import FullBootstrapResult

    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.run_full_bootstrap",
        lambda **kwargs: FullBootstrapResult(
            ok=True, planned=[], ran=[], messages=["装配过程日志"]
        ),
    )
    runner = CliRunner()
    result = runner.invoke(
        app, ["setup", "--full", "--yes", "--json", "--static-gateway", "builtin"]
    )
    # 关键：stdout 以 JSON 对象开头（无「── 完整装配」等散文前缀）
    stripped = result.stdout.lstrip()
    assert stripped.startswith("{"), result.stdout[:200]
    data = json.loads(result.stdout)
    assert data["profile"] == "full"
    assert data["bootstrap"]["messages"] == ["装配过程日志"]
    assert "── 完整装配" not in result.stdout


def test_cli_setup_install_docker_failure_exits_nonzero(monkeypatch) -> None:
    """BUG-197：安装脚本失败时 setup 必须 exit 1。"""
    from local_webpage_access.host_bootstrap import DockerOfferResult
    from local_webpage_access.setup import SetupReport

    monkeypatch.setattr(
        "local_webpage_access.setup.run_setup",
        lambda **kwargs: SetupReport(platform="macos", items=[]),
    )
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.maybe_offer_docker_install",
        lambda **kwargs: DockerOfferResult(
            messages=["fail"],
            attempted=True,
            script_ok=False,
            recheck_ok=False,
        ),
    )
    runner = CliRunner()
    result = runner.invoke(app, ["setup", "--install-docker", "--static-gateway", "builtin"])
    assert result.exit_code == 1


def test_cli_setup_install_docker_success_rechecks(monkeypatch) -> None:
    """BUG-197：安装成功后复检，ready 用新报告。"""
    from local_webpage_access.doctor import STATUS_OK
    from local_webpage_access.host_bootstrap import DockerOfferResult
    from local_webpage_access.setup import SetupItem, SetupReport

    calls = {"n": 0}

    def fake_setup(**kwargs):
        calls["n"] += 1
        # 第一次 fail，复检 ok
        status = STATUS_OK if calls["n"] > 1 else "fail"
        return SetupReport(
            platform="macos",
            items=[
                SetupItem(
                    name="docker",
                    status=status,
                    message="x",
                    required="y",
                    install_hint="z",
                )
            ],
        )

    monkeypatch.setattr("local_webpage_access.setup.run_setup", fake_setup)
    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.maybe_offer_docker_install",
        lambda **kwargs: DockerOfferResult(
            messages=["installed"],
            attempted=True,
            script_ok=True,
            recheck_ok=True,
        ),
    )
    runner = CliRunner()
    result = runner.invoke(app, ["setup", "--install-docker", "--static-gateway", "builtin"])
    assert calls["n"] == 2
    assert result.exit_code == 0
    assert "安装后复检" in result.output


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
