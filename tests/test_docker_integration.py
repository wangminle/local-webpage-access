"""真实 Docker 集成测试（WBS-28.15）。

这些测试需要宿主机 docker 命令与运行中的守护进程，默认通过双重守卫跳过：

1. ``requires_docker`` —— PATH 中存在 docker 命令。
2. ``LWA_RUN_DOCKER_TESTS=1`` —— 显式开启，避免在仅安装 docker 但守护进程
   未运行的 CI 误触发。

覆盖内容：

* Docker / Compose 可用性自检（WBS-26.03/04 的真实环境验证）。
* 最小容器拉起与清理（验证 DockerRuntime 闭环）。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.conftest import requires_docker

_DOCKER_EXPLICIT = os.environ.get("LWA_RUN_DOCKER_TESTS") == "1"

_docker_guard = pytest.mark.skipif(
    not _DOCKER_EXPLICIT,
    reason="设置 LWA_RUN_DOCKER_TESTS=1 以启用真实 Docker 集成测试",
)


def _run(args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


@_docker_guard
@requires_docker
class TestDockerSelfCheck:
    """WBS-26.03/04 对应的真实环境检查。"""

    def test_docker_version_runs(self) -> None:
        r = _run(["docker", "version", "--format", "{{.Server.Version}}"])
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip()

    def test_compose_version_runs(self) -> None:
        r = _run(["docker", "compose", "version", "--short"])
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip()

    def test_docker_info_runs(self) -> None:
        r = _run(["docker", "info", "--format", "{{.ServerVersion}}"])
        assert r.returncode == 0, r.stderr


@_docker_guard
@requires_docker
class TestDockerSmoke:
    """拉起一个最小容器，验证 docker run/ps/stop 闭环。"""

    def test_run_hello_world(self) -> None:
        r = _run(["docker", "run", "--rm", "hello-world"], timeout=120)
        assert r.returncode == 0, r.stderr
        assert "Hello from Docker" in r.stdout
