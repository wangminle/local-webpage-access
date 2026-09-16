"""issue #39：容器实例构建期注入前端 base（buildEnv / 别名跟随）。"""

from __future__ import annotations

from local_webpage_access.compose import generate_compose, generate_env
from local_webpage_access.dockerfile_templates import generate_dockerfile
from local_webpage_access.instance_settings import update_instance_settings
from local_webpage_access.models import ContainerConfig, Kind, ServingMode, Status
from tests._helpers import make_container_manifest


def _seed_python_container(workspace, registry, iid: str = "funasr"):
    workspace.ensure_app_dirs(iid)
    (workspace.app_current(iid) / "requirements.txt").write_text("flask\n", encoding="utf-8")
    (workspace.app_current(iid) / "app.py").write_text("app=1\n", encoding="utf-8")
    manifest = make_container_manifest(
        iid,
        kind=Kind.PYTHON,
        servingMode=ServingMode.CONTAINER,
        status=Status.STOPPED,
        container=ContainerConfig(
            projectName=f"lwa-{iid}",
            internalPort=8000,
            composePath="docker/compose.yaml",
            dockerfilePath="docker/Dockerfile",
        ),
    )
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)
    return manifest


def test_container_accepts_build_env_settings(workspace, registry) -> None:
    _seed_python_container(workspace, registry)
    updated = update_instance_settings(
        workspace, registry, "funasr", {"buildEnv": {"VITE_BASE": "/funasr/"}}
    )
    assert updated.buildEnv == {"VITE_BASE": "/funasr/"}


def test_container_dockerfile_and_compose_receive_build_args(workspace, registry) -> None:
    manifest = _seed_python_container(workspace, registry)
    update_instance_settings(
        workspace, registry, "funasr", {"buildEnv": {"VITE_BASE": "/funasr/"}}
    )
    from local_webpage_access.models import InstanceManifest

    manifest = InstanceManifest.load(workspace.app_manifest_path("funasr"))
    docker = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert "ARG VITE_BASE=" in docker
    assert "ENV VITE_BASE=${VITE_BASE}" in docker
    compose = generate_compose(manifest, workspace, host_port=18006).read_text(encoding="utf-8")
    assert "args:" in compose
    assert "VITE_BASE:" in compose
    assert "/funasr/" in compose
    env = generate_env(manifest, workspace, host_port=18006).read_text(encoding="utf-8")
    assert "HOST_PORT=18006" in env
    assert "VITE_BASE=" not in env


def test_container_vite_guard_hint_mentions_configure(workspace, registry) -> None:
    from local_webpage_access.errors import RecognitionError
    from local_webpage_access.path_alias import _append_lwa_build_hint

    manifest = _seed_python_container(workspace, registry)
    manifest.stack = ["fastapi", "vite"]
    manifest.buildEnv = {"VITE_BASE": "/funasr/"}
    exc = RecognitionError("入口 HTML 含未带别名前缀的加载型绝对路径资源")
    hinted = _append_lwa_build_hint(exc, manifest, "funasr")
    assert "lwa configure" in hinted.message
    assert "VITE_BASE" in hinted.message
