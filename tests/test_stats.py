"""资源监控与统计测试（WBS-19）。

不依赖真实 Docker：镜像/容器指标用 monkeypatch subprocess.run 模拟；
/proc 在非 Linux 上自动降级为 None；目录大小用真实临时文件验证。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from local_web_access.paths import Workspace
from local_web_access.registry import Registry
from local_web_access.stats import (
    HostResources,
    InstanceResources,
    _parse_cpu,
    _parse_container_stats,
    _parse_meminfo_line,
    _parse_size,
    _parse_mem_usage,
    all_instance_resources,
    collect_and_store,
    host_resources,
    instance_resources,
)


# ---- fixtures --------------------------------------------------------------


@pytest.fixture()
def workspace(workspace_root: Path) -> Workspace:
    ws = Workspace(workspace_root)
    ws.ensure_workspace_dirs()
    return ws


@pytest.fixture()
def registry(workspace_root: Path) -> Registry:
    workspace_root.joinpath("registry").mkdir(parents=True, exist_ok=True)
    reg = Registry(workspace_root / "registry" / "local-web.db")
    reg.open()
    yield reg
    reg.close()


@pytest.fixture()
def config(workspace_root: Path):
    from local_web_access.config import Config, PortPool

    return Config(portPool=PortPool(start=21000, end=21050))


def _seed_container_instance(
    workspace: Workspace, registry: Registry, iid: str = "api"
) -> None:
    from local_web_access.models import (
        ContainerConfig,
        DesiredState,
        InstanceManifest,
        Kind,
        ResourceProfile,
        Runtime,
        ServingMode,
        Status,
    )

    workspace.ensure_app_dirs(iid)
    # 写一些文件让目录大小 > 0
    (workspace.app_source(iid) / "code.py").write_text("x" * 100)
    (workspace.app_current(iid) / "index.html").write_text("<html/>")
    pub = workspace.app_public(iid)
    pub.mkdir(parents=True, exist_ok=True)
    (pub / "a.txt").write_text("a" * 50)
    data = workspace.app_data(iid)
    data.mkdir(parents=True, exist_ok=True)
    (data / "app.sqlite").write_text("db" * 200)

    manifest = InstanceManifest(
        id=iid,
        name=iid,
        version="1",
        kind=Kind.PYTHON,
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.SMALL,
        status=Status.RUNNING,
        desiredState=DesiredState.RUNNING,
        container=ContainerConfig(
            projectName=f"lwa-{iid}",
            internalPort=8000,
            composePath="docker/compose.yaml",
            dockerfilePath="docker/Dockerfile",
            imageId="sha256:abc",
        ),
    )
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)


def _seed_static_instance(
    workspace: Workspace, registry: Registry, iid: str = "demo"
) -> None:
    from local_web_access.models import (
        DesiredState,
        InstanceManifest,
        Kind,
        ResourceProfile,
        Runtime,
        ServingMode,
        StaticConfig,
        Status,
    )

    workspace.ensure_app_dirs(iid)
    (workspace.app_current(iid) / "index.html").write_text("<html/>")
    pub = workspace.app_public(iid)
    pub.mkdir(parents=True, exist_ok=True)
    (pub / "index.html").write_text("<html>hi</html>")

    manifest = InstanceManifest(
        id=iid,
        name=iid,
        version="1",
        kind=Kind.STATIC,
        runtime=Runtime.SHARED_STATIC,
        servingMode=ServingMode.SHARED_STATIC,
        resourceProfile=ResourceProfile.TINY,
        status=Status.RUNNING,
        desiredState=DesiredState.RUNNING,
        static=StaticConfig(hostPort=21100),
    )
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)


# ---- 整机资源 ---------------------------------------------------------------


def test_host_resources_disk_always_available(workspace) -> None:
    """disk_usage 在所有平台可用；mem/load 在非 Linux 为 None。"""
    info = host_resources(root=workspace.root)
    assert isinstance(info, HostResources)
    assert info.disk_total_bytes is not None
    assert info.disk_total_bytes > 0
    assert info.disk_used_bytes is not None
    if sys.platform.startswith("linux"):
        assert info.mem_total_bytes is not None
        assert info.load_avg_1m is not None
    else:
        # 非 Linux：/proc 不可读 → None（降级）
        assert info.mem_total_bytes is None
        assert info.load_avg_1m is None


def test_host_resources_to_dict(workspace) -> None:
    info = host_resources(root=workspace.root)
    d = info.to_dict()
    assert "diskTotalBytes" in d
    assert "platform" in d


def test_parse_meminfo_line() -> None:
    assert _parse_meminfo_line("MemTotal:       16384000 kB") == 16384000 * 1024
    assert _parse_meminfo_line("MemAvailable:   8000 kB") == 8000 * 1024


def test_parse_size() -> None:
    assert _parse_size("100B") == 100
    assert _parse_size("1KiB") == 1024
    assert _parse_size("1.5MiB") == int(1.5 * 1024 * 1024)
    assert _parse_size("2GB") == 2_000_000_000
    assert _parse_size("1024") == 1024


def test_parse_mem_usage() -> None:
    assert _parse_mem_usage("12.5MiB / 512MiB") == int(12.5 * 1024 * 1024)
    assert _parse_mem_usage(None) is None
    assert _parse_mem_usage("") is None


def test_parse_cpu() -> None:
    assert _parse_cpu("0.45%") == 0.45
    assert _parse_cpu(None) is None
    assert _parse_cpu("n/a") is None


# ---- 容器 stats 解析 --------------------------------------------------------


def test_parse_container_stats_matches_instance() -> None:
    stdout = '{"Name": "lwa-api-app", "MemUsage": "12.5MiB / 512MiB", "CPUPerc": "0.45%"}\n'
    mem, cpu = _parse_container_stats(stdout, "api")
    assert mem == int(12.5 * 1024 * 1024)
    assert cpu == 0.45


def test_parse_container_stats_does_not_match_substring() -> None:
    """BUG-027：查询 api 不得误命中 lwa-api2。"""
    stdout = (
        '{"Name": "lwa-api2", "MemUsage": "99MiB / 512MiB", "CPUPerc": "9.9%"}\n'
        '{"Name": "lwa-api", "MemUsage": "12MiB / 512MiB", "CPUPerc": "0.4%"}\n'
    )
    mem, cpu = _parse_container_stats(stdout, "api")
    assert mem == 12 * 1024 * 1024
    assert cpu == 0.4


def test_parse_container_stats_substring_only_returns_none() -> None:
    """BUG-027：只有 lwa-api2 时，api 应无匹配。"""
    stdout = '{"Name": "lwa-api2-app", "MemUsage": "99MiB / 512MiB", "CPUPerc": "9.9%"}\n'
    mem, cpu = _parse_container_stats(stdout, "api")
    assert mem is None
    assert cpu is None


def test_parse_container_stats_no_match() -> None:
    stdout = '{"Name": "other-app", "MemUsage": "1MiB / 1MiB", "CPUPerc": "0.1%"}\n'
    mem, cpu = _parse_container_stats(stdout, "api")
    assert mem is None
    assert cpu is None


def test_parse_container_stats_empty() -> None:
    assert _parse_container_stats("", "api") == (None, None)


# ---- 实例资源 ---------------------------------------------------------------


def test_instance_resources_container_dirs(
    workspace, registry, config, monkeypatch
) -> None:
    """容器实例：source/public/data 大小被统计；镜像/容器指标默认探测（无 Docker→None）。"""
    # docker 子进程探测会失败（无 Docker）→ None，不抛
    _seed_container_instance(workspace, registry, "api")
    info = instance_resources(workspace, config, registry, "api")
    assert isinstance(info, InstanceResources)
    assert info.source_size_bytes is not None and info.source_size_bytes > 0
    assert info.public_size_bytes == 50
    assert info.data_size_bytes == 400


def test_instance_resources_static_no_container_metrics(
    workspace, registry, config
) -> None:
    """静态实例不采集镜像/容器指标。"""
    _seed_static_instance(workspace, registry, "demo")
    info = instance_resources(workspace, config, registry, "demo")
    assert info.image_size_bytes is None
    assert info.last_memory_bytes is None
    assert info.last_cpu_percent is None
    assert info.public_size_bytes is not None and info.public_size_bytes > 0


def test_instance_resources_collect_container_false(
    workspace, registry, config
) -> None:
    """collect_container=False 跳过 docker stats（批量场景）。"""
    _seed_container_instance(workspace, registry, "api")
    info = instance_resources(
        workspace, config, registry, "api", collect_container=False
    )
    assert info.last_memory_bytes is None
    assert info.last_cpu_percent is None


def test_instance_resources_with_mocked_docker(
    workspace, registry, config, monkeypatch
) -> None:
    """模拟 docker image inspect + docker stats 返回，验证镜像/容器指标采集。"""
    _seed_container_instance(workspace, registry, "api")

    class _FakeCompleted:
        def __init__(self, stdout="", returncode=0):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = returncode

    def fake_run(args, **kw):
        if "image" in args and "inspect" in args:
            return _FakeCompleted("12345678", 0)
        if "stats" in args:
            stats_json = '{"Name": "lwa-api-app", "MemUsage": "12.5MiB / 512MiB", "CPUPerc": "0.45%"}\n'
            return _FakeCompleted(stats_json, 0)
        return _FakeCompleted("", 1)

    monkeypatch.setattr("local_web_access.stats.subprocess.run", fake_run)

    info = instance_resources(workspace, config, registry, "api")
    assert info.image_size_bytes == 12345678
    assert info.last_memory_bytes == int(12.5 * 1024 * 1024)
    assert info.last_cpu_percent == 0.45


# ---- collect_and_store -----------------------------------------------------


def test_collect_and_store_writes_resources_table(
    workspace, registry, config
) -> None:
    _seed_container_instance(workspace, registry, "api")
    info = collect_and_store(workspace, config, registry, "api")
    assert info.data_size_bytes is not None
    row = registry.get_resources("api")
    assert row is not None
    assert row["data_size_bytes"] == info.data_size_bytes
    assert row["public_size_bytes"] == info.public_size_bytes


def test_all_instance_resources(workspace, registry, config) -> None:
    _seed_container_instance(workspace, registry, "api")
    _seed_static_instance(workspace, registry, "demo")
    infos = all_instance_resources(workspace, config, registry)
    assert {i.instance_id for i in infos} == {"api", "demo"}


def test_resource_collection_failure_returns_none(
    workspace, registry, config, monkeypatch
) -> None:
    """docker 子进程抛异常时不影响实例资源采集（降级为 None）。"""
    _seed_container_instance(workspace, registry, "api")

    def boom(*a, **kw):
        raise OSError("docker broken")

    monkeypatch.setattr("local_web_access.stats.subprocess.run", boom)
    info = instance_resources(workspace, config, registry, "api")
    # 目录大小仍可用；容器/镜像指标为 None
    assert info.source_size_bytes is not None
    assert info.image_size_bytes is None
    assert info.last_memory_bytes is None
