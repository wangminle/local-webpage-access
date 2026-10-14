"""S2-03 真实 Docker 验证：先建后换依赖的两个物理前提（opt-in）。

1. compose 对未指定 ``image:`` 的 build 服务，默认镜像名为 ``<project>-<service>``，
   因此 ``rebuild_txn.image_repo`` 的推导与之一致；
2. 新镜像构建失败时，正在运行的旧容器不受影响，仍然响应 HTTP。

设置 ``LWA_RUN_DOCKER_TESTS=1`` 启用；依赖本机已有 ``python:3.13-slim`` 基础镜像。
"""

from __future__ import annotations

import os
import socket
import subprocess
import textwrap
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

from tests.scripts.conftest import requires_docker

_DOCKER_EXPLICIT = os.environ.get("LWA_RUN_DOCKER_TESTS") == "1"
pytestmark = [
    pytest.mark.skipif(
        not _DOCKER_EXPLICIT,
        reason="设置 LWA_RUN_DOCKER_TESTS=1 以启用真实 Docker 集成测试",
    ),
    requires_docker,
]


def _run(args: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_status(port: int) -> int | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as resp:
            return int(resp.status)
    except Exception:  # noqa: BLE001
        return None


def _write_project(root: Path, *, build_ok: bool) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text("<html>served</html>", encoding="utf-8")
    dockerfile = textwrap.dedent(
        """\
        FROM python:3.13-slim
        WORKDIR /app
        COPY index.html .
        """
    )
    if not build_ok:
        dockerfile += "RUN false  # 模拟必败的 buildHook\n"
    dockerfile += 'CMD ["python", "-m", "http.server", "8000"]\n'
    (root / "Dockerfile").write_text(dockerfile, encoding="utf-8")


@pytest.fixture()
def stack(tmp_path: Path):
    project = f"lwa-s23-{uuid.uuid4().hex[:8]}"
    ctx = tmp_path / "ctx"
    _write_project(ctx, build_ok=True)
    port = _free_port()
    compose = tmp_path / "compose.yaml"
    compose.write_text(
        textwrap.dedent(
            f"""\
            name: {project}
            services:
              app:
                build:
                  context: {ctx}
                ports:
                  - "{port}:8000"
            """
        ),
        encoding="utf-8",
    )
    yield {"project": project, "ctx": ctx, "compose": compose, "port": port}
    _run(["docker", "compose", "-f", str(compose), "down", "--remove-orphans"], timeout=120)
    _run(["docker", "image", "rm", "-f", f"{project}-app:latest"], timeout=120)


def _compose(stack: dict, *args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    return _run(["docker", "compose", "-f", str(stack["compose"]), *args], timeout=timeout)


def test_default_image_name_matches_rebuild_txn_repo(stack: dict) -> None:
    from local_webpage_access.rebuild_txn import image_repo

    assert _compose(stack, "build").returncode == 0
    assert _compose(stack, "up", "-d").returncode == 0
    inspect = _run(["docker", "image", "inspect", f"{stack['project']}-app:latest"])
    assert inspect.returncode == 0, inspect.stderr
    # 与 rebuild_txn.image_repo 的推导（projectName-serviceName）一致
    assert image_repo(_fake_manifest(stack["project"])) == f"{stack['project']}-app"


def test_failed_build_leaves_running_container_serving(stack: dict) -> None:
    assert _compose(stack, "build").returncode == 0
    assert _compose(stack, "up", "-d").returncode == 0
    for _ in range(20):
        if _http_status(stack["port"]) == 200:
            break
        time.sleep(0.5)
    assert _http_status(stack["port"]) == 200

    # 新版本构建必败：旧容器必须仍在运行并继续服务
    _write_project(stack["ctx"], build_ok=False)
    failed = _compose(stack, "build")
    assert failed.returncode != 0

    assert _compose(stack, "ps", "-q").stdout.strip(), "旧容器应仍在运行"
    assert _http_status(stack["port"]) == 200


def test_compensation_restores_previous_version(stack: dict) -> None:
    """模拟「新版本 up 后验证失败」的补偿路径（与 hosting._compensate_build_first 同序）。

    旧版本在跑 → 保留 :prev 标签与生成物快照 → 新版本 build 成功后 up 但内容不对
    （此处等价于验证失败现场）→ 补偿：down 新容器 → 恢复快照 → :prev 打回 :latest
    → up 旧版本 → HTTP 验证旧内容恢复服务。
    """
    from local_webpage_access.rebuild_txn import restore_previous_image_tag, tag_image

    assert _compose(stack, "build").returncode == 0
    assert _compose(stack, "up", "-d").returncode == 0
    time.sleep(1)
    assert _http_status(stack["port"]) == 200

    manifest = _fake_manifest(stack["project"])
    prev_compose = stack["compose"].read_text(encoding="utf-8")

    # 先建后换的准备：旧镜像保留为 :prev，生成物记入快照（此处为内存副本）
    tag_image(f"{stack['project']}-app:latest", f"{stack['project']}-app:prev")

    # 新版本构建成功、up 后服务的是新内容——等价于「验证失败」的旧容器已停现场
    (stack["ctx"] / "index.html").write_text("<html>broken new</html>", encoding="utf-8")
    assert _compose(stack, "build").returncode == 0
    assert _compose(stack, "up", "-d").returncode == 0
    time.sleep(1)
    body = urllib.request.urlopen(f"http://127.0.0.1:{stack['port']}/", timeout=3).read()
    assert b"broken new" in body

    # 补偿：down 新容器 → 恢复快照 → :prev 打回 :latest → up 旧版本 → 验证
    assert _compose(stack, "down").returncode == 0
    stack["compose"].write_text(prev_compose, encoding="utf-8")
    restore_previous_image_tag(manifest)
    assert _compose(stack, "up", "-d").returncode == 0
    for _ in range(20):
        if _http_status(stack["port"]) == 200:
            break
        time.sleep(0.5)
    body = urllib.request.urlopen(f"http://127.0.0.1:{stack['port']}/", timeout=3).read()
    assert b"broken new" not in body
    assert b"served" in body


def _fake_manifest(project: str):
    from tests.scripts._helpers import make_container_manifest

    manifest = make_container_manifest("demo")
    manifest.container.projectName = project
    manifest.container.serviceName = "app"
    return manifest
