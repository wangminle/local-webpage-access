"""BUG-766/767/768：安装进程组超时收尾、日志初始化泄漏与 sudo 预检平台限定。

- BUG-766：父进程退出、忽略 SIGTERM 的子进程持 stdout 时，runner 按进程组收尾并在总预算内返回；
- BUG-767：日志路径初始化失败时不得先启动安装进程；
- BUG-768：sudo 预认证仅覆盖确需 sudo 的 Linux/WSL 安装项，macOS brew 路径不预检。
- 补充：stdout 提前 EOF 不代表工作完成，runner 须在剩余预算内等待自然收尾；
  超时消息对亚秒预算保持精度（不显示「0s」）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from local_webpage_access import host_bootstrap
from local_webpage_access.host_bootstrap import (
    DockerEngineState,
    InstallPlanItem,
    run_full_bootstrap,
)
from local_webpage_access.version_requirements import MIN_DOCKER_VERSION


def _proc(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ---- BUG-766：忽略 SIGTERM 的子进程持 stdout 时不得永久挂起 -------------------


def test_script_runner_reaps_group_held_by_term_ignoring_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """父进程响应 TERM 退出、子进程忽略 TERM 并持有 stdout 时，
    runner 仍须按进程组是否仍存在强制收尾，并在总预算内返回。"""
    monkeypatch.setattr(host_bootstrap, "_SCRIPT_KILL_GRACE", 0.1)
    pidfile = tmp_path / "child.pid"
    child = (
        "import signal,time,os,pathlib;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()));"
        "time.sleep(30)"
    )
    leader = (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{child!r}]);"
        "time.sleep(30)"
    )
    started = time.monotonic()
    result = host_bootstrap._default_subprocess_run([sys.executable, "-c", leader], timeout=0.5)
    elapsed = time.monotonic() - started

    assert elapsed < 10, "总时限到期后应强制收尾进程组，而不是永久挂起在 stdout EOF"
    assert result.returncode != 0
    assert "超过总时限" in result.stdout
    child_pid = int(pidfile.read_text(encoding="utf-8"))

    def _child_alive() -> bool:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return False
        return True

    deadline = time.monotonic() + 5
    while _child_alive():
        if time.monotonic() >= deadline:
            pytest.fail("返回后子进程仍在，进程组未收尾")
        time.sleep(0.05)


def test_script_runner_success_path_unchanged(tmp_path: Path) -> None:
    """自然完成的脚本仍按退出码与输出返回，不触发强制收尾。"""
    script = tmp_path / "ok.sh"
    script.write_text("echo hello\n", encoding="utf-8")
    result = host_bootstrap._default_subprocess_run(["bash", str(script)], timeout=20)
    assert result.returncode == 0
    assert "hello" in result.stdout


def test_script_runner_early_eof_waits_for_natural_completion(tmp_path: Path) -> None:
    """stdout 提前 EOF（进程关闭输出但继续静默工作）时，runner 须在剩余预算内
    等待自然完成，不得提前终止尚未到期的进程组。"""
    marker = tmp_path / "done"
    script = (
        "import os,time,pathlib;"
        "os.close(1);"
        "time.sleep(0.5);"
        f"pathlib.Path({str(marker)!r}).write_text('done', encoding='utf-8')"
    )
    started = time.monotonic()
    result = host_bootstrap._default_subprocess_run([sys.executable, "-c", script], timeout=10)
    elapsed = time.monotonic() - started
    assert result.returncode == 0
    assert elapsed < 10
    assert marker.read_text(encoding="utf-8") == "done"


def test_script_runner_timeout_message_keeps_subsecond_precision(tmp_path: Path) -> None:
    """亚秒级超时的提示消息不得因 ``:.0f`` 格式化成「0s」（生产预算 1800s 不受影响）。"""
    result = host_bootstrap._default_subprocess_run(
        [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.2
    )
    assert result.returncode != 0
    assert "超过总时限 0.2s" in result.stdout


# ---- BUG-767：日志初始化失败不得泄漏已启动的安装进程 ---------------------------


def test_script_runner_log_init_failure_starts_no_installer(tmp_path: Path) -> None:
    """``logs`` 被同名普通文件占用时 mkdir/open 失败，安装进程不得已启动。"""
    logs_blocker = tmp_path / "logs"
    logs_blocker.write_text("occupied", encoding="utf-8")
    pidfile = tmp_path / "installer.pid"
    script = (
        "import os,time,pathlib;"
        f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()));"
        "time.sleep(30)"
    )
    with pytest.raises(OSError):
        host_bootstrap._default_subprocess_run(
            [sys.executable, "-c", script],
            timeout=5,
            log_path=logs_blocker / "bootstrap.log",
        )
    time.sleep(0.5)
    assert not pidfile.exists(), "日志初始化失败时不得残留安装进程"


def test_script_runner_log_write_failure_reaps_whole_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BUG-778：日志运行中写入失败（ENOSPC）走异常路径时，须按进程组是否存活收尾；
    leader 已退出、同组子进程继续输出也不得泄漏。"""
    real_open = Path.open
    log_path = tmp_path / "failing.log"

    class _FailingLog:
        def __init__(self, fh):  # noqa: ANN001
            self.fh = fh

        def write(self, value):  # noqa: ANN001, ANN202
            raise OSError(28, "No space left on device")

        def flush(self) -> None:
            self.fh.flush()

        def close(self) -> None:
            self.fh.close()

    def failing_open(path, *a, **kw):  # noqa: ANN001, ANN002, ANN202
        fh = real_open(path, *a, **kw)
        return _FailingLog(fh) if path == log_path else fh

    leader_pidfile = tmp_path / "leader.pid"
    child_pidfile = tmp_path / "child.pid"
    child_code = (
        "import os,time,pathlib;"
        f"pathlib.Path({str(child_pidfile)!r}).write_text(str(os.getpid()));"
        'time.sleep(.4); print("child output",flush=True); time.sleep(30)'
    )
    leader_code = (
        "import os,subprocess,sys,pathlib;"
        f"pathlib.Path({str(leader_pidfile)!r}).write_text(str(os.getpid()));"
        f"subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
    )
    monkeypatch.setattr(Path, "open", failing_open)
    with pytest.raises(OSError, match="No space left"):
        host_bootstrap._default_subprocess_run(
            [sys.executable, "-c", leader_code], timeout=10, log_path=log_path
        )
    child_pid = int(child_pidfile.read_text(encoding="utf-8"))
    pgid = int(leader_pidfile.read_text(encoding="utf-8"))

    def _child_alive() -> bool:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return False
        return True

    deadline = time.monotonic() + 5
    while _child_alive():
        if time.monotonic() >= deadline:
            pytest.fail("日志写入异常返回后同组子进程仍未被清理")
        time.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


# ---- BUG-768：sudo 预认证按安装项目限定平台 ------------------------------------


def _patch_full_install_env(
    monkeypatch: pytest.MonkeyPatch,
    plan: list[InstallPlanItem],
) -> tuple[list, list]:
    monkeypatch.setattr(host_bootstrap, "plan_full_install", lambda **_: plan)
    monkeypatch.setattr(
        host_bootstrap,
        "detect_docker_engine",
        lambda **_: DockerEngineState("ok", MIN_DOCKER_VERSION),
    )
    sudo_calls: list = []
    script_calls: list = []
    monkeypatch.setattr(
        host_bootstrap,
        "_sudo_preflight",
        lambda: sudo_calls.append(1) and None,
    )
    return sudo_calls, script_calls


def test_macos_caddy_only_plan_skips_sudo_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """macOS Caddy 脚本用普通用户 brew 安装，不得被 sudo 预检提前拒绝。"""
    plan = [
        InstallPlanItem(
            "caddy",
            host_bootstrap.resolve_install_script("caddy", "macos"),
            "caddy=missing",
        )
    ]
    sudo_calls, script_calls = _patch_full_install_env(monkeypatch, plan)
    monkeypatch.setattr(
        host_bootstrap,
        "_default_subprocess_run",
        lambda cmd, **_: script_calls.append(cmd) or _proc(1, stderr="install failed"),
    )
    result = run_full_bootstrap(platform="macos", yes=True, workspace_root=tmp_path)
    assert sudo_calls == []
    assert len(script_calls) == 1
    assert result.ok is False
    assert any("脚本失败" in m for m in result.messages)


def test_linux_plan_still_preflights_sudo_and_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Linux 安装脚本确需 sudo：预检拒绝时不得执行安装脚本。"""
    plan = [
        InstallPlanItem(
            "docker",
            host_bootstrap.resolve_install_script("docker", "linux"),
            "docker=missing",
        )
    ]
    sudo_calls: list = []
    script_calls: list = []
    monkeypatch.setattr(host_bootstrap, "plan_full_install", lambda **_: plan)
    monkeypatch.setattr(
        host_bootstrap,
        "detect_docker_engine",
        lambda **_: DockerEngineState("missing"),
    )
    def fake_preflight() -> str:
        sudo_calls.append(1)
        return "安装需要 sudo 权限，但当前为非交互环境"

    monkeypatch.setattr(host_bootstrap, "_sudo_preflight", fake_preflight)
    monkeypatch.setattr(
        host_bootstrap,
        "_default_subprocess_run",
        lambda cmd, **_: script_calls.append(cmd) or _proc(0),
    )
    result = run_full_bootstrap(platform="linux", yes=True, workspace_root=tmp_path)
    assert len(sudo_calls) == 1
    assert script_calls == []
    assert result.ok is False
    assert any("sudo" in m for m in result.messages)


def test_linux_plan_runs_script_after_sudo_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Linux 安装脚本预检通过后正常执行。"""
    plan = [
        InstallPlanItem(
            "caddy",
            host_bootstrap.resolve_install_script("caddy", "linux"),
            "caddy=outdated",
        )
    ]
    sudo_calls, script_calls = _patch_full_install_env(monkeypatch, plan)
    monkeypatch.setattr(
        host_bootstrap,
        "_default_subprocess_run",
        lambda cmd, **_: script_calls.append(cmd) or _proc(1, stderr="install failed"),
    )
    result = run_full_bootstrap(platform="linux", yes=True, workspace_root=tmp_path)
    assert len(sudo_calls) == 1
    assert len(script_calls) == 1
    assert result.ok is False
