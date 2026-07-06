"""测试辅助工厂。"""

from __future__ import annotations

from local_webpage_access.models import (
    ContainerConfig,
    DesiredState,
    InstanceManifest,
    Kind,
    ResourceProfile,
    Runtime,
    ServingMode,
    StaticConfig,
    Status,
)


def make_static_manifest(mid: str = "demo", **overrides) -> InstanceManifest:
    defaults: dict = dict(
        id=mid,
        name=mid.replace("-", " ").title(),
        version="1",
        kind=Kind.STATIC,
        runtime=Runtime.SHARED_STATIC,
        servingMode=ServingMode.SHARED_STATIC,
        resourceProfile=ResourceProfile.TINY,
        static=StaticConfig(hostPort=18001),
        desiredState=DesiredState.STOPPED,
        status=Status.PENDING,
    )
    defaults.update(overrides)
    return InstanceManifest(**defaults)


def make_container_manifest(mid: str = "api", **overrides) -> InstanceManifest:
    defaults: dict = dict(
        id=mid,
        name=mid.replace("-", " ").title(),
        version="1",
        kind=Kind.PYTHON,
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.SMALL,
        container=ContainerConfig(
            projectName=f"lwa-{mid}",
            internalPort=8000,
            composePath="docker/compose.yaml",
            dockerfilePath="docker/Dockerfile",
        ),
        desiredState=DesiredState.STOPPED,
        status=Status.PENDING,
    )
    defaults.update(overrides)
    return InstanceManifest(**defaults)
