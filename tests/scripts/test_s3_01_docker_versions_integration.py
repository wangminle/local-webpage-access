"""S3-01 真实 Docker 验证：版本留档原语与回滚机制（opt-in）。

设置 ``LWA_RUN_DOCKER_TESTS=1`` 启用；依赖本机已有 ``python:3.13-slim`` 基础镜像。
覆盖：
1. 构建后解析 ``<repo>:latest`` 镜像 ID、打 ``b<build_id>`` 标签、image_exists/磁盘占用；
2. 生成物快照（真实 Workspace）往返；
3. GC 真实删除超保留期的镜像标签（在用镜像不受影响）；
4. 回滚机制：down → 恢复快照 → 目标镜像打回 :latest → up → 旧内容恢复服务。
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


def _http_body(port: int) -> bytes:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as resp:
        return resp.read()


def _wait_serving(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if _http_body(port):
                return
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    raise AssertionError(f"端口 {port} 未在时限内开始服务")


def _write_project(root: Path, content: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text(content, encoding="utf-8")
    (root / "Dockerfile").write_text(
        textwrap.dedent(
            """\
            FROM python:3.13-slim
            WORKDIR /app
            COPY index.html .
            CMD ["python", "-m", "http.server", "8000"]
            """
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def stack(tmp_path: Path):
    """真实 compose 项目（v1 内容）+ 挂到 LWA 临时工作区的 docker/ 目录。"""
    from local_webpage_access.paths import Workspace

    project = f"lwa-s31-{uuid.uuid4().hex[:8]}"
    ctx = tmp_path / "ctx"
    _write_project(ctx, "<html>version-one</html>")
    port = _free_port()
    ws_root = tmp_path / "ws"
    ws = Workspace(ws_root)
    ws.ensure_workspace_dirs()
    ws.ensure_app_dirs("demo")
    docker_dir = ws.app_docker("demo")
    (docker_dir / "compose.yaml").write_text(
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
    (docker_dir / "Dockerfile").write_text("FROM python:3.13-slim\n", encoding="utf-8")
    (docker_dir / ".env").write_text("A=1\n", encoding="utf-8")
    yield {"project": project, "ctx": ctx, "port": port, "ws": ws}
    _run(["docker", "compose", "-f", str(docker_dir / "compose.yaml"), "down",
          "--remove-orphans"], timeout=120)
    _run(["docker", "image", "rm", "-f", f"{project}-app:latest",
          f"{project}-app:prev"], timeout=120)


def _compose(stack: dict, *args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    compose = stack["ws"].app_docker("demo") / "compose.yaml"
    return _run(["docker", "compose", "-f", str(compose), *args], timeout=timeout)


def test_tag_resolve_exists_and_disk_usage(stack: dict) -> None:
    from local_webpage_access import versions

    assert _compose(stack, "build").returncode == 0
    repo = f"{stack['project']}-app"
    image_id = versions.resolve_repo_image_id(repo)
    assert image_id, "构建后应能解析 <repo>:latest 的镜像 ID"

    versions.tag_build_image(image_id, f"{repo}:b1")
    assert versions.image_exists(f"{repo}:b1") is True
    assert versions.image_exists(f"{repo}:b9") is False
    assert versions.image_disk_usage_bytes([f"{repo}:b1"]) > 0


def test_snapshot_roundtrip_real_files(stack: dict) -> None:
    from local_webpage_access import versions

    ws = stack["ws"]
    snap = versions.snapshot_config(ws, "demo", 1)
    assert snap is not None and (snap / "compose.yaml").is_file()

    (ws.app_docker("demo") / "compose.yaml").write_text("# tampered\n", encoding="utf-8")
    assert versions.restore_config_snapshot(ws, "demo", 1) is True
    assert "name:" in (ws.app_docker("demo") / "compose.yaml").read_text(encoding="utf-8")


def test_gc_removes_real_tag_but_keeps_in_use(stack: dict) -> None:
    from local_webpage_access import versions
    from local_webpage_access.models import DesiredState, Kind, ResourceProfile, Runtime
    from local_webpage_access.models import ServingMode, Status
    from local_webpage_access.models import InstanceManifest, ContainerConfig
    from local_webpage_access.registry import Registry

    ws = stack["ws"]
    repo = f"{stack['project']}-app"
    assert _compose(stack, "build").returncode == 0
    image_v1 = versions.resolve_repo_image_id(repo)
    # 造一个真实存在的「新版本」镜像（v1 + 一层 ENV），与 v1 不同 image_id
    derived = subprocess.run(
        ["docker", "build", "-q", "-t", f"{repo}:derived", "-"],
        input=f"FROM {repo}:latest\nENV LWA_GC_DERIVED=1\n",
        capture_output=True, text=True, timeout=300, check=False,
    )
    assert derived.returncode == 0, derived.stderr
    image_v2 = versions.resolve_repo_image_id(f"{repo}:derived")
    assert image_v2 and image_v2 != image_v1

    reg = Registry(ws.root / "registry" / "local-web.db")
    reg.open()
    try:
        manifest = InstanceManifest(
            id="demo", name="Demo", version="1", kind=Kind.PYTHON,
            runtime=Runtime.DOCKER_COMPOSE, servingMode=ServingMode.CONTAINER,
            resourceProfile=ResourceProfile.SMALL,
            container=ContainerConfig(
                projectName=stack["project"], serviceName="app", internalPort=8000,
                composePath=str(ws.app_compose_path("demo")),
                dockerfilePath=str(ws.app_dockerfile_path("demo")),
            ),
            desiredState=DesiredState.RUNNING, status=Status.RUNNING,
        )
        reg.upsert_from_manifest(manifest)
        # 最旧的一行 = 早期版本（image_v1）；其后 4 行均为 image_v2（在用 + 上一可用）
        prev = reg.add_build("demo", status="running")
        reg.finish_build(prev, status="success")
        reg.record_build_artifacts(
            prev, image_ref=f"{repo}:bprev", image_id=image_v1,
            source_hash="h-prev", config_snapshot=None,
        )
        stale = []
        for i in range(4):  # retention=3：bprev 超出保留且不受保护，应被 GC
            bid = reg.add_build("demo", status="running")
            reg.finish_build(bid, status="success")
            reg.record_build_artifacts(
                bid, image_ref=f"{repo}:old{i}", image_id=image_v2,
                source_hash=f"h{i}", config_snapshot=None,
            )
            versions.tag_build_image(image_v2, f"{repo}:old{i}")
            stale.append(f"{repo}:old{i}")

        result = versions.gc_build_artifacts(ws, _config(), reg, "demo",
                                             in_use_image_id=image_v2)
        assert result["removed"] == [prev]
        assert versions.image_exists(f"{repo}:bprev") is False
        assert versions.image_exists(stale[-1]) is True
        # 在用镜像本身（latest 与 derived 指向的 image_v2）不受影响
        assert versions.image_exists(f"{repo}:latest") is True
        assert versions.image_exists(f"{repo}:derived") is True
    finally:
        reg.close()


def _config():
    from local_webpage_access.config import Config, PortPool

    return Config(staticGateway="builtin", portPool=PortPool(start=21000, end=21050),
                  imageRetention=3)


def test_rollback_mechanism_restores_old_content(stack: dict) -> None:
    """回滚机制全链：v1 服务 → 快照 → v2 构建 → down → 恢复快照 →
    v1 镜像打回 :latest → up → 旧内容恢复。"""
    from local_webpage_access import versions

    ws = stack["ws"]
    repo = f"{stack['project']}-app"

    assert _compose(stack, "build").returncode == 0
    assert _compose(stack, "up", "-d").returncode == 0
    _wait_serving(stack["port"])
    assert b"version-one" in _http_body(stack["port"])
    image_v1 = versions.resolve_repo_image_id(repo)

    # S3-01 留档：v1 快照 + b1 标签
    assert versions.snapshot_config(ws, "demo", 1) is not None
    versions.tag_build_image(image_v1, f"{repo}:b1")

    # v2 上线
    _write_project(stack["ctx"], "<html>version-two</html>")
    assert _compose(stack, "build").returncode == 0
    assert _compose(stack, "up", "-d").returncode == 0
    _wait_serving(stack["port"])
    assert b"version-two" in _http_body(stack["port"])

    # 回滚：停 → 恢复快照 → b1 打回 latest → up
    assert _compose(stack, "down").returncode == 0
    assert versions.restore_config_snapshot(ws, "demo", 1) is True
    versions.tag_build_image(f"{repo}:b1", f"{repo}:latest")
    assert _compose(stack, "up", "-d").returncode == 0
    _wait_serving(stack["port"])
    body = _http_body(stack["port"])
    assert b"version-one" in body
    assert b"version-two" not in body
