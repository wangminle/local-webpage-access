"""issue #35 建议 3：构建失败分类诊断与探针日志摘要。"""

from __future__ import annotations

from local_webpage_access.build_diagnostics import (
    classify_build_failure,
    format_probe_failure,
)


def test_classify_apt_fetch_failure() -> None:
    text = (
        "Err:170 http://mirrors.aliyun.com/debian trixie/main arm64 libllvm19\n"
        "  Connection failed [IP: 119.167.135.56 80]\n"
        "E: Unable to fetch some archives, maybe run apt-get update "
        "or try with --fix-missing?\n"
        "failed to solve: process did not complete successfully"
    )
    hint = classify_build_failure(text)
    assert hint is not None
    assert hint.kind == "apt"
    assert "镜像源不可达" in hint.summary
    assert "aptFallbacks" in hint.summary


def test_classify_oom_killed() -> None:
    text = (
        "#8 1247.3 Killed\n"
        "#8 ERROR: process did not complete successfully: cannot allocate memory\n"
        "failed to solve: ResourceExhausted: cannot allocate memory"
    )
    hint = classify_build_failure(text)
    assert hint is not None
    assert hint.kind == "oom"
    assert hint.confidence == "likely"
    assert "内存不足" in hint.summary
    assert "cannot allocate memory" in (hint.evidence or "")


def test_classify_killed_alone_is_uncertain() -> None:
    """CHK-326：单凭 Killed 不得断言 Docker VM 内存不足。"""
    hint = classify_build_failure("#8 1247.3 Killed\nfailed to solve: process killed")
    assert hint is not None
    assert hint.kind == "killed"
    assert hint.confidence == "uncertain"
    assert "内存不足" not in hint.summary
    assert "未确认" in hint.summary
    assert "Killed" in (hint.evidence or "")


def test_classify_disk_full() -> None:
    hint = classify_build_failure("write /var/cache/apt: No space left on device")
    assert hint is not None
    assert hint.kind == "disk"
    assert "磁盘不足" in hint.summary


def test_classify_unknown_returns_none() -> None:
    assert classify_build_failure("Syntax error: unexpected token") is None
    assert classify_build_failure("") is None
    assert classify_build_failure(None) is None


def test_format_probe_failure_includes_restart_hint_and_logs() -> None:
    msg = format_probe_failure(
        "基础存活探针超时（host_port=18006，30 次未响应）",
        container_state="restarting",
        restart_count=4,
        logs="PermissionError: [Errno 13] Permission denied: '/app/var/objects'\n",
    )
    assert "启动即崩" in msg
    assert "restarting" in msg
    assert "PermissionError" in msg
    assert "host_port=18006" in msg


def test_format_probe_failure_includes_inspect_evidence() -> None:
    msg = format_probe_failure(
        "基础存活探针超时",
        container_state="exited",
        restart_count=3,
        exit_code=137,
        oom_killed=True,
        logs="Killed",
    )
    assert "ExitCode=137" in msg
    assert "OOMKilled=true" in msg
    assert "原始证据" in msg


def test_format_probe_failure_omits_unknown_restart_count() -> None:
    """BUG-665：未知 RestartCount 不得写成 0。"""
    msg = format_probe_failure("基础存活探针超时", container_state="restarting")
    assert "RestartCount=0" not in msg
    assert "RestartCount=2" not in msg
