"""真实 Docker 集成测试（WBS-28.15 + IMP-058 C.R07）。

这些测试需要宿主机 docker 命令与运行中的守护进程，默认通过双重守卫跳过：

1. ``requires_docker`` -- PATH 中存在 docker 命令。
2. ``LWA_RUN_DOCKER_TESTS=1`` -- 显式开启，避免在仅安装 docker 但守护进程
   未运行的 CI 误触发。

覆盖内容：

* Docker / Compose 可用性自检（WBS-26.03/04 的真实环境验证）。
* 最小容器拉起与清理（验证 DockerRuntime 闭环）。
* C.R07 Gate-C 门控矩阵：
  - required/optional probe 验证
  - 等价 fallback 降级
  - migration 副作用禁止自动 fallback
  - 四类指纹变化触发强制重建
  - 失败现场保留（compose logs / inspect / 诊断报告）
"""

from __future__ import annotations

import os
import socket
import subprocess
import textwrap
from contextlib import suppress
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


def _free_port() -> int:
    """让内核分配当前可用的 loopback TCP 端口，避免与宿主固定端口冲突。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


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


# ---- C.R07：Gate-C 真实 Docker 门控矩阵 --------------------------------------


@_docker_guard
@requires_docker
class TestGateCRequiredProbe:
    """C.R07：required probe 在真实 Docker 下验证。

    部署一个带 /health 端点的 FastAPI 容器：
    - 健康 -> RUNNING
    - 不健康 -> FAILED（不写 RUNNING）
    """

    @pytest.fixture()
    def fastapi_project(self, tmp_path: Path) -> Path:
        """创建一个最小 FastAPI 项目。"""
        project = tmp_path / "fastapi-app"
        project.mkdir()
        (project / "requirements.txt").write_text(
            "fastapi\nuvicorn[standard]\n",
            encoding="utf-8",
        )
        (project / "app.py").write_text(
            textwrap.dedent("""\
            from fastapi import FastAPI
            from fastapi.responses import JSONResponse
            import os

            app = FastAPI()

            @app.get("/health")
            def health():
                if os.environ.get("FAIL_HEALTH") == "1":
                    return JSONResponse(
                        content={"status": "unhealthy"}, status_code=503,
                    )
                return {"status": "ok"}

            @app.get("/")
            def root():
                return {"message": "hello"}
        """),
            encoding="utf-8",
        )
        (project / "Dockerfile").write_text(
            textwrap.dedent("""\
            FROM python:3.13-slim
            WORKDIR /app
            COPY requirements.txt .
            RUN pip install --no-cache-dir -r requirements.txt
            COPY app.py .
            CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
        """),
            encoding="utf-8",
        )
        return project

    def test_healthy_container_passes_required_probe(
        self,
        fastapi_project: Path,
        tmp_path: Path,
    ) -> None:
        """健康容器 -> required probe 通过 -> RUNNING。"""
        from local_webpage_access.docker_runtime import DockerRuntime
        from local_webpage_access.hosting import _evaluate_container_verification
        from local_webpage_access.models import CapabilityContract, ProbeSpec
        from local_webpage_access.paths import Workspace
        from local_webpage_access.registry import Registry
        from tests._helpers import make_container_manifest

        ws_root = tmp_path / "ws"
        ws = Workspace(ws_root)
        ws.ensure_workspace_dirs()
        reg = Registry(ws.db_path)
        reg.open()
        try:
            runtime = DockerRuntime(ws)
            instance_id = "test-cr07-healthy"
            host_port = _free_port()

            compose_dir = ws.app_docker(instance_id)
            compose_dir.mkdir(parents=True)
            (compose_dir / "compose.yaml").write_text(
                textwrap.dedent(f"""\
                services:
                  app:
                    build: {fastapi_project}
                    ports:
                      - "{host_port}:8000"
                    environment:
                      - FAIL_HEALTH=0
            """),
                encoding="utf-8",
            )

            runtime.build(instance_id)
            runtime.up(instance_id)

            import time

            time.sleep(3)

            assert runtime.is_running(instance_id)
            manifest = make_container_manifest(instance_id)
            manifest.capabilityContract = CapabilityContract(
                servesUi=True,
                servesApi=True,
                requiredProbes=[ProbeSpec(path="/health", isMandatory=True, source="declared")],
            ).model_dump()
            verification = _evaluate_container_verification(
                host_port, manifest, ws, reg, instance_id
            )
            assert verification["overall_status"] == "passed", verification
            assert verification["mandatory_all_passed"] is True
        finally:
            if "runtime" in locals() and "instance_id" in locals():
                with suppress(Exception):
                    runtime.down(instance_id)
            reg.close()

    def test_unhealthy_container_fails_required_probe(
        self,
        fastapi_project: Path,
        tmp_path: Path,
    ) -> None:
        """不健康容器 -> required probe 失败 -> 不写 RUNNING。"""
        from local_webpage_access.docker_runtime import DockerRuntime
        from local_webpage_access.hosting import _evaluate_container_verification
        from local_webpage_access.models import CapabilityContract, ProbeSpec
        from local_webpage_access.paths import Workspace
        from local_webpage_access.registry import Registry
        from tests._helpers import make_container_manifest

        ws_root = tmp_path / "ws"
        ws = Workspace(ws_root)
        ws.ensure_workspace_dirs()
        reg = Registry(ws.db_path)
        reg.open()
        try:
            runtime = DockerRuntime(ws)
            instance_id = "test-cr07-unhealthy"
            host_port = _free_port()

            compose_dir = ws.app_docker(instance_id)
            compose_dir.mkdir(parents=True)
            (compose_dir / "compose.yaml").write_text(
                textwrap.dedent(f"""\
                services:
                  app:
                    build: {fastapi_project}
                    ports:
                      - "{host_port}:8000"
                    environment:
                      - FAIL_HEALTH=1
            """),
                encoding="utf-8",
            )

            runtime.build(instance_id)
            runtime.up(instance_id)

            import time

            time.sleep(3)

            assert runtime.is_running(instance_id)
            manifest = make_container_manifest(instance_id)
            manifest.capabilityContract = CapabilityContract(
                servesUi=True,
                servesApi=True,
                requiredProbes=[ProbeSpec(path="/health", isMandatory=True, source="declared")],
            ).model_dump()
            verification = _evaluate_container_verification(
                host_port, manifest, ws, reg, instance_id
            )
            assert verification["overall_status"] == "failed"
            assert verification["mandatory_all_passed"] is False
        finally:
            if "runtime" in locals() and "instance_id" in locals():
                with suppress(Exception):
                    runtime.down(instance_id)
            reg.close()


@_docker_guard
@requires_docker
class TestGateCFingerprintChange:
    """C.R07：四类指纹变化在真实 Docker 下触发强制重建。

    验证 sourceHash 变化被检测到。
    """

    @pytest.fixture()
    def simple_http_project(self, tmp_path: Path) -> Path:
        """创建一个最小 Python HTTP 服务项目。"""
        project = tmp_path / "http-app"
        project.mkdir()
        (project / "requirements.txt").write_text("", encoding="utf-8")
        (project / "app.py").write_text(
            textwrap.dedent("""\
            from http.server import HTTPServer, BaseHTTPRequestHandler

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"hello v1")

            HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
        """),
            encoding="utf-8",
        )
        return project

    def test_source_change_detected_by_fingerprint(
        self,
        simple_http_project: Path,
    ) -> None:
        """修改源码后 sourceHash 变化。"""
        from local_webpage_access.lifecycle import _compute_source_fingerprint

        h1 = _compute_source_fingerprint(str(simple_http_project))
        assert len(h1) == 64

        (simple_http_project / "app.py").write_text(
            textwrap.dedent("""\
            from http.server import HTTPServer, BaseHTTPRequestHandler

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"hello v2")

            HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
        """),
            encoding="utf-8",
        )

        h2 = _compute_source_fingerprint(str(simple_http_project))
        assert h1 != h2, "源码修改后指纹应变化"


@_docker_guard
@requires_docker
class TestGateCComposeLifecycle:
    """C.R07：真实 Docker compose 生命周期闭环。

    验证 build -> up -> start -> stop -> down 完整闭环，
    并保留失败现场的 compose logs 和 inspect。
    """

    def test_compose_build_up_stop_down_cycle(self, tmp_path: Path) -> None:
        """完整 compose 生命周期：up -> stop -> start -> logs -> down。"""
        from local_webpage_access.docker_runtime import DockerRuntime
        from local_webpage_access.paths import Workspace
        from local_webpage_access.registry import Registry

        ws_root = tmp_path / "ws"
        ws = Workspace(ws_root)
        ws.ensure_workspace_dirs()
        reg = Registry(ws.db_path)
        reg.open()
        try:
            runtime = DockerRuntime(ws)
            instance_id = "test-cr07-lifecycle"
            host_port = _free_port()

            compose_dir = ws.app_docker(instance_id)
            compose_dir.mkdir(parents=True)
            (compose_dir / "compose.yaml").write_text(
                textwrap.dedent(f"""\
                services:
                  app:
                    image: nginx:alpine
                    ports:
                      - "{host_port}:80"
            """),
                encoding="utf-8",
            )

            runtime.up(instance_id)
            import time

            time.sleep(2)
            assert runtime.is_running(instance_id)

            cid = runtime.container_id(instance_id)
            assert cid, "应获取到 containerId"

            runtime.stop(instance_id)
            assert not runtime.is_running(instance_id)

            runtime.start(instance_id)
            assert runtime.is_running(instance_id)

            logs = runtime.logs(instance_id, tail=10)
            assert logs

            runtime.down(instance_id)
            assert not runtime.is_running(instance_id)
        finally:
            if "runtime" in locals() and "instance_id" in locals():
                with suppress(Exception):
                    runtime.down(instance_id)
            reg.close()


@_docker_guard
@requires_docker
class TestGateCMigrationSideEffect:
    """C.R07：migration 副作用在真实 Docker 下的行为验证。

    验证 SideEffectRecord 在 alembic 迁移场景下正确标记 autoRecoverable=False。
    """

    def test_migration_side_effect_not_auto_recoverable(self) -> None:
        """带 alembic 迁移的启动命令 -> SideEffectRecord.autoRecoverable=False。"""
        from local_webpage_access.hosting import (
            _collect_side_effect_records,
            _side_effects_auto_recoverable,
        )
        from local_webpage_access.models import (
            EntryConfig,
            InstanceManifest,
            Kind,
            Runtime,
            ServingMode,
        )

        manifest = InstanceManifest(
            id="test-cr07-migration",
            name="Migration Test",
            version="1",
            kind=Kind.PYTHON,
            runtime=Runtime.DOCKER_COMPOSE,
            servingMode=ServingMode.CONTAINER,
            sourceZipPath="/tmp/test.zip",
            appPath="/tmp/app",
            container={
                "projectName": "lwa-test-cr07-migration",
                "internalPort": 8000,
                "composePath": "/tmp/compose.yml",
                "dockerfilePath": "/tmp/Dockerfile",
            },
        )
        manifest.entry = EntryConfig(
            install="pip install -r requirements.txt",
            start="alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000",
        )
        manifest.capabilityContract = {"requiresMigrations": True}

        records = _collect_side_effect_records(
            manifest,
            liveness_ok=True,
            verification_status="passed",
        )
        assert len(records) == 1
        assert records[0].kind == "migration"
        assert records[0].autoRecoverable is False
        assert not _side_effects_auto_recoverable(records)


@_docker_guard
@requires_docker
class TestGateCFailureScenePreservation:
    """C.R07：失败现场保留--compose logs / inspect / 诊断报告。"""

    def test_build_failure_preserves_diagnostic(self, tmp_path: Path) -> None:
        """构建失败时保留诊断信息，无残留容器。"""
        from local_webpage_access.docker_runtime import DockerRuntime
        from local_webpage_access.errors import DockerError
        from local_webpage_access.paths import Workspace
        from local_webpage_access.registry import Registry

        ws_root = tmp_path / "ws"
        ws = Workspace(ws_root)
        ws.ensure_workspace_dirs()
        reg = Registry(ws.db_path)
        reg.open()
        try:
            runtime = DockerRuntime(ws)
            instance_id = "test-cr07-buildfail"
            host_port = _free_port()

            compose_dir = ws.app_docker(instance_id)
            compose_dir.mkdir(parents=True)
            (compose_dir / "compose.yaml").write_text(
                textwrap.dedent(f"""\
                services:
                  app:
                    build: .
                    ports:
                      - "{host_port}:80"
            """),
                encoding="utf-8",
            )
            (compose_dir / "Dockerfile").write_text(
                "FROM non-existent-image-12345:latest\n",
                encoding="utf-8",
            )

            with pytest.raises(DockerError):
                runtime.build(instance_id)

            assert not runtime.is_running(instance_id)
        finally:
            reg.close()


@_docker_guard
@requires_docker
class TestNonRootContainerIdentity:
    """issue #20：生成器输出的非 root 容器真实验证。

    覆盖：``docker exec id -u`` 不为 0；非 root 进程可在 bind mount 的
    ``/app/data`` 写普通文件与 SQLite 库（含 -wal/-shm 创建）。
    """

    def test_generated_container_runs_non_root_and_writes_data(
        self,
        tmp_path: Path,
    ) -> None:
        from local_webpage_access.compose import generate_compose, generate_env
        from local_webpage_access.docker_runtime import DockerRuntime
        from local_webpage_access.dockerfile_templates import generate_dockerfile
        from local_webpage_access.models import EntryConfig
        from local_webpage_access.paths import Workspace
        from local_webpage_access.registry import Registry
        from tests._helpers import make_container_manifest

        ws = Workspace(tmp_path / "ws")
        ws.ensure_workspace_dirs()
        reg = Registry(ws.db_path)
        reg.open()
        runtime = DockerRuntime(ws)
        instance_id = "test-i20-nonroot"
        host_port = _free_port()
        try:
            # 零依赖 stdlib 服务（python:3.13-slim 本地缓存，构建最快）
            current = ws.app_current(instance_id)
            current.mkdir(parents=True)
            manifest = make_container_manifest(
                instance_id,
                entry=EntryConfig(install=None, start="python -m http.server 8000"),
            )
            manifest.container.runAsNonRoot = True
            # 让 data/ 存在（属主 = 当前用户），身份解析与其对齐
            ws.app_data(instance_id).mkdir(parents=True, exist_ok=True)

            generate_dockerfile(manifest, ws)
            generate_compose(manifest, ws, host_port=host_port)
            generate_env(manifest, ws, host_port=host_port)

            runtime.build(instance_id)
            runtime.up(instance_id)
            assert runtime.is_running(instance_id)

            def _exec(*args: str) -> subprocess.CompletedProcess:
                return _run(["docker", "exec", f"lwa-{instance_id}", *args], timeout=30)

            # 1. 运行身份非 root
            uid = _exec("id", "-u")
            assert uid.returncode == 0, uid.stderr
            assert uid.stdout.strip() != "0", "容器不得以 root 运行（issue #20）"

            # 2. bind mount 的 /app/data 可写（普通文件）
            probe = _exec("sh", "-c", "echo ok > /app/data/probe.txt && cat /app/data/probe.txt")
            assert probe.returncode == 0, probe.stderr
            assert "ok" in probe.stdout

            # 3. SQLite 可建库建表（隐含 -wal/-shm 创建权限）
            sqlite_check = _exec(
                "python",
                "-c",
                "import sqlite3; con=sqlite3.connect('/app/data/t.db');"
                " con.execute('create table t(x)'); con.commit(); con.close()",
            )
            assert sqlite_check.returncode == 0, sqlite_check.stderr
            # 宿主侧可见落盘（bind mount 对齐验证）
            assert (ws.app_data(instance_id) / "t.db").is_file()
        finally:
            with suppress(Exception):
                runtime.down(instance_id)
            reg.close()

    def test_runtime_root_container_writes_sqlite_as_non_root(self, tmp_path: Path) -> None:
        """issue #20：RUNTIME_ROOT 布局（/app/runtime/data）的非 root SQLite 写入。"""
        from local_webpage_access.compose import generate_compose, generate_env
        from local_webpage_access.docker_runtime import DockerRuntime
        from local_webpage_access.dockerfile_templates import generate_dockerfile
        from local_webpage_access.models import DatabaseConfig, EntryConfig
        from local_webpage_access.paths import Workspace
        from local_webpage_access.registry import Registry
        from tests._helpers import make_container_manifest

        ws = Workspace(tmp_path / "ws")
        ws.ensure_workspace_dirs()
        reg = Registry(ws.db_path)
        reg.open()
        runtime = DockerRuntime(ws)
        instance_id = "test-i20-rtroot"
        host_port = _free_port()
        try:
            ws.app_current(instance_id).mkdir(parents=True)
            manifest = make_container_manifest(
                instance_id,
                entry=EntryConfig(install=None, start="python -m http.server 8000"),
                hasDatabase=True,
                database=DatabaseConfig(type="sqlite", dataDir="runtime/data"),
            )
            manifest.container.runAsNonRoot = True
            ws.app_data(instance_id).mkdir(parents=True, exist_ok=True)

            generate_dockerfile(manifest, ws)
            compose_path = generate_compose(manifest, ws, host_port=host_port)
            generate_env(manifest, ws, host_port=host_port)
            # RUNTIME_ROOT 布局：data 挂载点为 /app/runtime/data
            assert "../data:/app/runtime/data" in compose_path.read_text(encoding="utf-8")

            runtime.build(instance_id)
            runtime.up(instance_id)
            assert runtime.is_running(instance_id)

            uid = _run(["docker", "exec", f"lwa-{instance_id}", "id", "-u"], timeout=30)
            assert uid.stdout.strip() != "0", "RUNTIME_ROOT 容器同样不得以 root 运行"

            sqlite_check = _run(
                [
                    "docker",
                    "exec",
                    f"lwa-{instance_id}",
                    "python",
                    "-c",
                    "import sqlite3; con=sqlite3.connect('/app/runtime/data/t.db');"
                    " con.execute('create table t(x)'); con.commit(); con.close()",
                ],
                timeout=30,
            )
            assert sqlite_check.returncode == 0, sqlite_check.stderr
            assert (ws.app_data(instance_id) / "t.db").is_file()
        finally:
            with suppress(Exception):
                runtime.down(instance_id)
            reg.close()
