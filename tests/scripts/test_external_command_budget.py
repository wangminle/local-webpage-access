"""外部命令时间预算（PLN-052 S1-06 / #55）。

- autostart 默认 30s 超时，超时转为带排查提示的 AutostartError；
- 安装脚本流式执行、写日志、总时限到期后终止整个进程组；
- 需要 sudo 时先预认证，非交互环境提前拒绝；两个安装脚本开头调用 ensure_sudo。
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from local_webpage_access import autostart, host_bootstrap

_SCRIPTS = Path(host_bootstrap.__file__).parent / "scripts"


# ---- autostart 默认超时 -------------------------------------------------------


def test_autostart_runner_applies_default_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(autostart.subprocess, "run", fake_run)
    autostart._default_runner(["systemctl", "--user", "is-active", "x"], capture_output=True)
    assert captured["timeout"] == autostart._RUNNER_DEFAULT_TIMEOUT == 30


def test_autostart_runner_keeps_explicit_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(autostart.subprocess, "run", fake_run)
    autostart._default_runner(["launchctl", "print", "x"], timeout=5)
    assert captured["timeout"] == 5


def test_autostart_runner_timeout_becomes_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(autostart.subprocess, "run", fake_run)
    with pytest.raises(autostart.AutostartError, match="systemctl --user status"):
        autostart._default_runner(["systemctl", "--user", "is-active", "x"])


# ---- 安装脚本：流式输出与总时限 -------------------------------------------------


def test_script_runner_streams_to_log_and_kills_group_on_total_timeout(tmp_path: Path) -> None:
    script = tmp_path / "slow.sh"
    script.write_text("echo stage-one\nsleep 30\necho never-reached\n", encoding="utf-8")
    log = tmp_path / "logs" / "bootstrap.log"

    started = time.monotonic()
    result = host_bootstrap._default_subprocess_run(
        ["bash", str(script)], timeout=1, log_path=log
    )
    elapsed = time.monotonic() - started

    assert result.returncode != 0
    assert elapsed < 15, "总时限到期后应终止进程组，而不是等满 sleep 30"
    assert "stage-one" in log.read_text(encoding="utf-8")
    assert "超过总时限" in result.stdout
    assert "never-reached" not in result.stdout


def test_script_runner_success_returns_zero_and_output(tmp_path: Path) -> None:
    script = tmp_path / "ok.sh"
    script.write_text("echo hello\n", encoding="utf-8")
    result = host_bootstrap._default_subprocess_run(["bash", str(script)], timeout=20)
    assert result.returncode == 0
    assert "hello" in result.stdout


def test_bootstrap_log_path_lives_under_workspace_logs(tmp_path: Path) -> None:
    path = host_bootstrap._bootstrap_log_path(tmp_path)
    assert path.parent == tmp_path / "logs"
    assert path.name.startswith("bootstrap-") and path.suffix == ".log"


# ---- sudo 预认证 -------------------------------------------------------------


def _patch_non_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_bootstrap.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(host_bootstrap.shutil, "which", lambda name: "/usr/bin/sudo")


def test_sudo_preflight_root_needs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_bootstrap.os, "geteuid", lambda: 0)
    assert host_bootstrap._sudo_preflight() is None


def test_sudo_preflight_passwordless_is_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_non_root(monkeypatch)
    monkeypatch.setattr(
        host_bootstrap.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, b"", b""),
    )
    assert host_bootstrap._sudo_preflight() is None


def test_sudo_preflight_refuses_when_password_needed_non_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_non_root(monkeypatch)
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 1, b"", b"")

    monkeypatch.setattr(host_bootstrap.subprocess, "run", fake_run)
    monkeypatch.setattr(host_bootstrap, "_stdin_is_interactive", lambda: False)

    reason = host_bootstrap._sudo_preflight()

    assert reason is not None
    assert "非交互环境" in reason and "sudo -v" in reason
    assert calls == [["sudo", "-n", "true"]], "非交互时不得尝试 sudo -v（会挂起等待密码）"


def test_sudo_preflight_interactive_prompts_sudo_v(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_non_root(monkeypatch)
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        rc = 1 if cmd[1] == "-n" else 0
        return subprocess.CompletedProcess(cmd, rc, b"", b"")

    monkeypatch.setattr(host_bootstrap.subprocess, "run", fake_run)
    monkeypatch.setattr(host_bootstrap, "_stdin_is_interactive", lambda: True)

    assert host_bootstrap._sudo_preflight() is None
    assert ["sudo", "-v"] in calls


# ---- 安装脚本：开头完成 sudo 认证 ----------------------------------------------


@pytest.mark.parametrize("name", ["install-caddy-linux.sh", "install-docker-linux.sh"])
def test_install_script_authenticates_sudo_before_privileged_work(name: str) -> None:
    text = (_SCRIPTS / name).read_text(encoding="utf-8")
    assert "ensure_sudo() {" in text
    head = text[text.index("main() {") :].splitlines()[:6]
    assert any("ensure_sudo" in line for line in head), "ensure_sudo 应在 main 开头调用"


@pytest.mark.parametrize("name", ["install-caddy-linux.sh", "install-docker-linux.sh"])
def test_install_script_syntax_is_valid(name: str) -> None:
    result = subprocess.run(
        ["bash", "-n", str(_SCRIPTS / name)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
