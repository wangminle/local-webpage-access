"""S3-07（DEV-146）真实 Docker 验证：aptBudget 让挂住的 apt 源在预算内被放弃并切源（opt-in）。

用一个只 accept、永不应答的容器充当两个“慢源”，渲染带 ``aptBudget`` 的 apt 层并真实
``docker build``：每个源候选应在预算附近被终止，整体在远小于 Retries x Timeout 的时间内
以 lwa 统一提示失败，而不是挂住数分钟。

设置 ``LWA_RUN_DOCKER_TESTS=1`` 启用；依赖本机已有 ``python:3.13-slim`` 基础镜像。
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from local_webpage_access.config import BuildMirrors
from local_webpage_access.dockerfile_templates import _render_apt_deps_block
from tests.scripts.conftest import requires_docker

_DOCKER_EXPLICIT = os.environ.get("LWA_RUN_DOCKER_TESTS") == "1"
pytestmark = [
    pytest.mark.skipif(
        not _DOCKER_EXPLICIT,
        reason="设置 LWA_RUN_DOCKER_TESTS=1 以启用真实 Docker 集成测试",
    ),
    requires_docker,
]

_SILENT_SERVER = (
    "import socket\n"
    "s = socket.socket()\n"
    "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
    "s.bind(('0.0.0.0', 80))\n"
    "s.listen(50)\n"
    "keep = []\n"
    "while True:\n"
    "    c, _ = s.accept()\n"
    "    keep.append(c)\n"
)


def _run(args: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


@pytest.fixture
def silent_mirror_ip():
    name = f"lwa-silent-{uuid.uuid4().hex[:8]}"
    started = _run(
        ["docker", "run", "-d", "--name", name, "python:3.13-slim", "python", "-c", _SILENT_SERVER]
    )
    assert started.returncode == 0, started.stderr
    try:
        ip = ""
        for _ in range(20):
            insp = _run(
                [
                    "docker",
                    "inspect",
                    "-f",
                    "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
                    name,
                ]
            )
            parts = insp.stdout.split()
            ip = parts[0] if parts else ""
            if ip:
                break
            time.sleep(0.5)
        assert ip, "取不到静默源容器 IP"
        time.sleep(1.5)  # 等监听就绪
        yield ip
    finally:
        _run(["docker", "rm", "-f", name])


def test_apt_budget_aborts_hanging_mirrors_and_switches(
    tmp_path: Path, silent_mirror_ip: str
) -> None:
    budget = 4
    mirrors = BuildMirrors(
        enabled=True,
        preset="china",
        aptMirror="slow1.test",
        aptFallbacks=["slow2.test"],
        aptBudget=budget,
    ).resolved()
    (tmp_path / "Dockerfile").write_text(
        "FROM python:3.13-slim\n" + _render_apt_deps_block(["ffmpeg"], mirrors),
        encoding="utf-8",
    )
    tag = f"lwa-apt-budget-{uuid.uuid4().hex[:8]}"
    started = time.monotonic()
    try:
        build = _run(
            [
                "docker",
                "build",
                "--no-cache",
                "--progress=plain",
                "--add-host",
                f"slow1.test:{silent_mirror_ip}",
                "--add-host",
                f"slow2.test:{silent_mirror_ip}",
                "-t",
                tag,
                str(tmp_path),
            ],
            timeout=180,
        )
    finally:
        _run(["docker", "rmi", "-f", tag])
    elapsed = time.monotonic() - started
    out = build.stdout + build.stderr

    assert build.returncode != 0
    # 两个源候选各被预算终止一次（回显行会出现 2 次以上：xtrace + 实际输出）
    assert out.count("总时间预算") >= 2
    assert "apt 安装失败" in out and "aptBudget" in out
    # 未设预算时 Retries=2 x Timeout=30 对挂住的源要拖 90s 以上；此处应远小于它
    assert elapsed < 75, f"预算未生效：构建耗时 {elapsed:.0f}s"
