"""issue #35 建议 3：构建失败分类诊断与探针日志摘要。"""

from __future__ import annotations

from local_webpage_access.build_diagnostics import (
    classify_build_failure,
    format_probe_failure,
)


def test_classify_ignores_compile_regex_in_docker_run_echo() -> None:
    """CHK-460：失败 RUN 的命令回显含 Failed building wheel 时，不得把网络失败当成 compile。"""
    text = (
        "#7 [5/9] RUN --mount=type=cache,target=/root/.cache/pip \\\n"
        "  ( pip install -r requirements.txt ) > /tmp/lwa-pip.log 2>&1; status=$?; "
        "cat /tmp/lwa-pip.log; if [ \"$status\" -ne 0 ]; then if grep -E -q "
        "'Failed building wheel|error: command|subprocess-exited-with-error|pcre.h'; "
        "then echo '依赖编译失败。这不是镜像源不可达'; fi; fi\n"
        "#7 0.412 WARNING: Retrying (Retry(total=4, connect=None, read=None, "
        "redirect=None, status=None)) after connection broken by "
        "'NewConnectionError': Failed to establish a new connection: "
        "[Errno 101] Network is unreachable\n"
        "#7 1.008 Failed to fetch http://mirrors.aliyun.com/debian/pool/main/u/uwsgi\n"
        "#7 ERROR: process \"/bin/sh -c ( pip install ) ; grep Failed building "
        "wheel\" did not complete successfully\n"
        "failed to solve: process did not complete successfully\n"
    )
    hint = classify_build_failure(text)
    assert hint is not None
    assert hint.kind != "compile"
    assert "这不是镜像源不可达" not in hint.summary
    assert hint.kind == "apt"


def test_classify_compile_failure_is_not_mirror() -> None:
    """BUG-947：uwsgi/gcc 编译失败不得归成镜像源不可达。"""
    text = (
        "#9 12.1 gcc -pthread -fPIC -c plugins/pcre/pcre_plugin.c\n"
        "#9 12.2 fatal error: pcre.h: No such file or directory\n"
        "#9 12.4 error: command '/usr/bin/gcc' failed with exit code 1\n"
        "#9 12.5 Failed building wheel for uwsgi\n"
        "#9 ERROR: process did not complete successfully\n"
    )
    hint = classify_build_failure(text)
    assert hint is not None
    assert hint.kind == "compile"
    assert hint.summary.startswith("依赖编译失败")
    assert "apt 拉取失败" not in hint.summary


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


def test_classify_docker_hub_token_is_registry_not_apt() -> None:
    """issue #36：failed to fetch anonymous token 不得归为 apt。"""
    text = (
        "failed to solve: failed to fetch anonymous token:\n"
        'Get "https://auth.docker.io/token?scope=repository%3Alibrary%2Fpython'
        '%3Apull&service=registry.docker.io": dial tcp 3.3.3.3:443: i/o timeout'
    )
    hint = classify_build_failure(text)
    assert hint is not None
    assert hint.kind == "registry"
    assert "apt" not in hint.summary.lower()
    assert "Docker Hub" in hint.summary or "registry" in hint.summary.lower()


def test_classify_apt_still_wins_on_real_apt_log() -> None:
    text = (
        "Err:4 http://mirrors.aliyun.com/debian trixie/main arm64 Packages\n"
        "  Connection failed [IP: 27.221.122.14 80]\n"
        "E: Unable to fetch some archives, maybe run apt-get update\n"
    )
    hint = classify_build_failure(text)
    assert hint is not None
    assert hint.kind == "apt"


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
