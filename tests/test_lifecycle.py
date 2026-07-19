"""生命周期编排测试（WBS-17）。

用 fake DockerRuntime / fake gateway 验证 start / stop / restart / rebuild /
remove / observe_status 的派发、desiredState 一致性、并发锁与数据保护。
不依赖真实 Docker。
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from local_webpage_access.errors import LifecycleError
from local_webpage_access.lifecycle import (
    _lock_is_stale,
    _pid_alive,
    instance_lock,
    list_redundant_instances,
    observe_status,
    rebuild_instance,
    remove_instance,
    remove_redundant,
    restart_instance,
    start_instance,
    stop_instance_op,
)
from local_webpage_access.models import (
    ContainerConfig,
    DesiredState,
    InstanceManifest,
    Kind,
    ResourceProfile,
    Runtime,
    ServingMode,
    Status,
)
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry


# ---- 复用 fixtures ----------------------------------------------------------


@pytest.fixture()
def workspace(workspace_root: Path) -> Workspace:
    ws = Workspace(workspace_root)
    ws.ensure_workspace_dirs()
    return ws


class _FakeRuntime:
    """记录调用的 DockerRuntime 替身。"""

    calls: list[str] = []
    _running = False

    def __init__(self, workspace=None, registry=None) -> None:
        self.workspace = workspace
        self.registry = registry

    @classmethod
    def ensure_available(cls) -> None:
        return None

    @classmethod
    def is_available(cls) -> bool:
        return True

    def is_running(self, iid):
        return type(self)._running

    def start(self, iid, **kw):
        type(self).calls.append("start")
        type(self)._running = True

    def stop(self, iid, **kw):
        type(self).calls.append("stop")
        type(self)._running = False

    def down(self, iid, **kw):
        type(self).calls.append("down")
        type(self)._running = False

    def build(self, iid, *, build_id=None, **kw):
        type(self).calls.append("build")
        if build_id is not None and self.registry is not None:
            self.registry.finish_build(build_id, status="success")

    def up(self, iid, **kw):
        type(self).calls.append("up")
        type(self)._running = True

    def container_id(self, iid):
        return "cid-x"

    def image_id(self, iid):
        return "sha256:img"

    def status(self, iid):
        return None


@pytest.fixture()
def fake_runtime(monkeypatch):
    _FakeRuntime.calls = []
    _FakeRuntime._running = False
    # hosting 在模块顶层导入了 DockerRuntime；lifecycle 用局部 import 解析到
    # docker_runtime.DockerRuntime。两处都要替换。
    monkeypatch.setattr("local_webpage_access.hosting.DockerRuntime", _FakeRuntime)
    monkeypatch.setattr("local_webpage_access.docker_runtime.DockerRuntime", _FakeRuntime)
    monkeypatch.setattr("local_webpage_access.hosting._http_ok", lambda port, **kw: True)
    return _FakeRuntime


def _seed_container(
    workspace: Workspace,
    registry: Registry,
    iid: str = "api",
    *,
    deployed: bool = False,
) -> InstanceManifest:
    """构造容器实例；deployed=True 时模拟已部署（有 containerId、hostPort）。"""
    workspace.ensure_app_dirs(iid)
    current = workspace.app_current(iid)
    (current / "main.py").write_text("app=None")
    (current / "requirements.txt").write_text("fastapi")
    manifest = InstanceManifest(
        id=iid,
        name=iid,
        version="1",
        kind=Kind.PYTHON,
        stack=["fastapi"],
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.SMALL,
        container=ContainerConfig(
            projectName=f"lwa-{iid}",
            internalPort=8000,
            composePath="docker/compose.yaml",
            dockerfilePath="docker/Dockerfile",
            containerId="cid-x" if deployed else None,
            hostPort=21000 if deployed else None,
        ),
        status=Status.STOPPED if deployed else Status.PENDING,
        desiredState=DesiredState.STOPPED,
    )
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)
    return manifest


def _seed_static(
    workspace: Workspace, registry: Registry, iid: str = "demo"
) -> InstanceManifest:
    workspace.ensure_app_dirs(iid)
    (workspace.app_current(iid) / "index.html").write_text("<html>hi</html>")
    from tests._helpers import make_static_manifest

    manifest = make_static_manifest(iid)
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)
    return manifest


# ---- 并发锁（WBS-17.12）----------------------------------------------------


def test_instance_lock_serializes_concurrent_ops(workspace) -> None:
    """两线程争用同一实例锁：第二个必须等第一个释放。"""
    order: list[str] = []
    barrier = threading.Barrier(2)

    def worker(tag: str) -> None:
        barrier.wait()
        with instance_lock(workspace, "api", timeout=10):
            order.append(f"{tag}-in")
            # 持锁期间短暂休眠，让另一线程必然排队
            for _ in range(1000):
                pass
            order.append(f"{tag}-out")

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    # 两个 in/out 互不交错（任一线程的 in 必须早于自己的 out，且与另一线程不交叉）
    joined = "".join(order)
    assert ("-in" in joined) and ("-out" in joined)
    # a-in 必须在 b-in 之前或之后，但不能在 a-in..a-out 之间出现 b-in
    assert order[0].endswith("-in")
    assert order[1].endswith("-out")
    assert order[2].endswith("-in")
    assert order[3].endswith("-out")


def test_instance_lock_timeout_raises(workspace) -> None:
    """锁被其他持有者占用且超时 → LifecycleError。"""
    import os
    import time as time_mod

    from local_webpage_access.file_lock import (
        ensure_lockable,
        release_exclusive,
        try_acquire_exclusive,
        write_lock_payload,
    )

    lock_path = workspace.run / "lifecycle-api.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # 真正持有跨平台文件锁，才能阻塞同路径的 instance_lock
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    ensure_lockable(fd)
    try_acquire_exclusive(fd)
    write_lock_payload(fd, f"{os.getpid()}\n{time_mod.time():.3f}\n".encode())
    try:
        with pytest.raises(LifecycleError, match="超时"):
            with instance_lock(workspace, "api", timeout=0.3):
                pass
    finally:
        release_exclusive(fd)
        os.close(fd)


def test_instance_lock_does_not_unlink_on_release(workspace) -> None:
    """BUG-213：释放后保留锁文件 inode，避免新旧 inode 并行持锁。"""
    lock_path = workspace.run / "lifecycle-keep.lock"
    with instance_lock(workspace, "keep", timeout=2):
        assert lock_path.exists()
        inode = lock_path.stat().st_ino
    assert lock_path.exists()
    assert lock_path.stat().st_ino == inode
    with instance_lock(workspace, "keep", timeout=2):
        assert lock_path.stat().st_ino == inode


def test_instance_lock_reclaims_stale(workspace, monkeypatch) -> None:
    """无文件锁持有者时，残留锁文件可被立即获取（进程崩溃后内核已释锁）。"""
    lock_path = workspace.run / "lifecycle-api.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # 用一个绝对不存在的 PID（仅残留文件内容，无人持锁）
    lock_path.write_text("999999\n0.0\n", encoding="utf-8")
    monkeypatch.setattr("local_webpage_access.lifecycle._STALE_LOCK_SECONDS", 999999)
    with instance_lock(workspace, "api", timeout=2):
        pass  # 不抛异常即说明可获取


def test_lock_is_stale_dead_pid(tmp_path: Path) -> None:
    p = tmp_path / "stale.lock"
    p.write_text("999999\n0.0\n", encoding="utf-8")
    assert _lock_is_stale(p) is True


def test_lock_is_stale_corrupt(tmp_path: Path) -> None:
    p = tmp_path / "bad.lock"
    p.write_text("not-a-pid\n", encoding="utf-8")
    assert _lock_is_stale(p) is True


def test_pid_alive_current_process() -> None:
    import os

    assert _pid_alive(os.getpid()) is True
    assert _pid_alive(0) is False
    assert _pid_alive(999999) is False


# ---- start -----------------------------------------------------------------


def test_start_undeployed_container_goes_full_deploy(
    workspace, registry, config, fake_runtime
) -> None:
    """未部署容器（无 containerId）→ 全量 host_container（build+up）。"""
    _seed_container(workspace, registry, "api", deployed=False)
    manifest = start_instance(workspace, config, registry, "api")
    assert manifest.status == Status.RUNNING
    assert manifest.desiredState == DesiredState.RUNNING
    assert "build" in fake_runtime.calls
    assert "up" in fake_runtime.calls
    assert "start" not in fake_runtime.calls  # 没走轻量 start


def test_start_deployed_container_uses_light_start(
    workspace, registry, config, fake_runtime
) -> None:
    """已部署容器（有 containerId）→ 轻量 compose start，不 build。"""
    _seed_container(workspace, registry, "api", deployed=True)
    manifest = start_instance(workspace, config, registry, "api")
    assert manifest.status == Status.RUNNING
    assert "start" in fake_runtime.calls
    assert "build" not in fake_runtime.calls
    assert "up" not in fake_runtime.calls


def test_start_static_dispatches_to_host_static(
    workspace, registry, config, fake_runtime
) -> None:
    """静态实例 start → host_instance 的静态分支（不碰 Docker）。"""
    _seed_static(workspace, registry, "demo")
    manifest = start_instance(workspace, config, registry, "demo")
    assert manifest.status == Status.RUNNING
    assert fake_runtime.calls == []  # 静态不调用 DockerRuntime
    # BUG：泄漏兜底——host_static 真起了一个 http.server 子进程，必须 stop
    # 掉，否则端口池在跨用例累积下会耗尽（导致全量测试连跑即红）。
    stop_instance_op(workspace, config, registry, "demo")


# ---- stop ------------------------------------------------------------------


def test_stop_container_marks_stopped_and_keeps_port(
    workspace, registry, config, fake_runtime
) -> None:
    _seed_container(workspace, registry, "api", deployed=True)
    registry.allocate_port("api", 21000)
    stop_instance_op(workspace, config, registry, "api")
    row = registry.get_instance("api")
    assert row["status"] == "stopped"
    assert row["desired_state"] == "stopped"
    assert "stop" in fake_runtime.calls
    # 端口保留
    assert 21000 in registry.allocated_ports()


# ---- restart ---------------------------------------------------------------


def test_restart_container_stop_then_light_start(
    workspace, registry, config, fake_runtime
) -> None:
    _seed_container(workspace, registry, "api", deployed=True)
    registry.allocate_port("api", 21000)
    fake_runtime._running = True  # 模拟在跑
    manifest = restart_instance(workspace, config, registry, "api")
    assert manifest.status == Status.RUNNING
    # 先 stop 再 start，不 build
    assert fake_runtime.calls.index("stop") < fake_runtime.calls.index("start")
    assert "build" not in fake_runtime.calls


# ---- rebuild ---------------------------------------------------------------


def test_rebuild_container_forces_full_rebuild(
    workspace, registry, config, fake_runtime
) -> None:
    """rebuild → host_container：down 旧 + build + up。"""
    _seed_container(workspace, registry, "api", deployed=True)
    fake_runtime._running = True
    rebuild_instance(workspace, config, registry, "api")
    assert "down" in fake_runtime.calls
    assert "build" in fake_runtime.calls
    assert "up" in fake_runtime.calls


def test_rebuild_writes_build_queue_events(
    workspace, registry, config, fake_runtime
) -> None:
    """rebuild 通过 BuildQueue 调度，应写入 build_start 事件（WBS-20.06）。"""
    _seed_container(workspace, registry, "api", deployed=False)
    rebuild_instance(workspace, config, registry, "api")
    events = registry.list_events("api")
    assert any(e["event_type"] == "build_start" for e in events)


# ---- remove ----------------------------------------------------------------


def test_remove_default_keeps_files_and_data(
    workspace, registry, config, fake_runtime
) -> None:
    _seed_container(workspace, registry, "api", deployed=True)
    data_dir = workspace.app_data("api")
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "app.sqlite").write_text("data")

    remove_instance(workspace, config, registry, "api")

    # registry 记录已删
    assert registry.get_instance("api") is None
    assert registry.get_container("api") is None
    # 磁盘文件 + data 保留
    assert workspace.app_dir("api").is_dir()
    assert (data_dir / "app.sqlite").is_file()


def test_remove_clears_pageviews(workspace, registry, config, fake_runtime) -> None:
    """BUG-090：删除实例应清空其浏览量，避免残留 / 同 ID 复用串数据。"""
    from local_webpage_access.pageviews import AccessHit, PageviewStore

    _seed_container(workspace, registry, "api", deployed=True)
    store = PageviewStore.for_workspace(workspace)
    store.record_hits(
        "api", "container", [AccessHit("2026-07-09T10:00:00+08:00", "GET", "/", 200, "1.1.1.1")]
    )
    assert store.summary().get("api", {}).get("hits") == 1
    store.close()

    remove_instance(workspace, config, registry, "api")

    store2 = PageviewStore.for_workspace(workspace)
    assert store2.summary().get("api") is None  # 浏览量已随实例删除清空
    store2.close()


def test_remove_purge_deletes_files(
    workspace, registry, config, fake_runtime
) -> None:
    _seed_container(workspace, registry, "api", deployed=False)
    remove_instance(workspace, config, registry, "api", purge=True, force=True)
    assert registry.get_instance("api") is None
    assert not workspace.app_dir("api").exists()


def test_remove_purge_protects_nonempty_data(
    workspace, registry, config, fake_runtime
) -> None:
    """purge 但 data/ 非空且未 force → LifecycleError。"""
    _seed_container(workspace, registry, "api", deployed=False)
    data_dir = workspace.app_data("api")
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "app.sqlite").write_text("data")

    with pytest.raises(LifecycleError, match="data/"):
        remove_instance(workspace, config, registry, "api", purge=True, force=False)
    # 实例仍在（保护生效）
    assert registry.get_instance("api") is not None


def test_remove_purge_force_deletes_nonempty_data(
    workspace, registry, config, fake_runtime
) -> None:
    _seed_container(workspace, registry, "api", deployed=False)
    data_dir = workspace.app_data("api")
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "app.sqlite").write_text("data")

    remove_instance(workspace, config, registry, "api", purge=True, force=True)
    assert registry.get_instance("api") is None
    assert not workspace.app_dir("api").exists()


def test_remove_container_calls_compose_down(
    workspace, registry, config, fake_runtime
) -> None:
    _seed_container(workspace, registry, "api", deployed=True)
    remove_instance(workspace, config, registry, "api")
    assert "stop" in fake_runtime.calls
    assert "down" in fake_runtime.calls


# ---- 回归测试：BUG-025 ----------------------------------------------------
#
# BUG-025：``lwa remove .. --purge --force`` 会 ``shutil.rmtree(app_dir(".."))``，
#          越界删除工作区根。修复后非法 ID 在入口（instance_lock / app_dir）
#          即被拦截，rmtree 永不执行。


def test_remove_traversal_id_rejected_before_delete(
    workspace, registry, config, fake_runtime
) -> None:
    """``..`` 不得越界删除：调用前在工作区根留哨兵，必须原样保留。"""
    from local_webpage_access.errors import LwaError

    sentinel = workspace.root / "DO-NOT-DELETE.txt"
    sentinel.write_text("workspace root sentinel")
    # 预置 apps/ 下一个真实实例，证明合法路径不受影响
    _seed_static(workspace, registry, "demo")

    with pytest.raises(LwaError):
        remove_instance(workspace, config, registry, "..", purge=True, force=True)

    # 工作区根哨兵仍在；合法实例目录也仍在
    assert sentinel.is_file()
    assert workspace.app_dir("demo").is_dir()


def test_remove_purge_valid_id_confined_to_apps(
    workspace, registry, config, fake_runtime
) -> None:
    """合法 purge 只删 apps/<id>/，不动 apps/ 同级与工作区根。"""
    sentinel = workspace.root / "DO-NOT-DELETE.txt"
    sentinel.write_text("workspace root sentinel")
    sibling = workspace.apps / "keep-me"
    sibling.mkdir(parents=True)
    (sibling / "f.txt").write_text("kept")

    _seed_static(workspace, registry, "demo")
    remove_instance(workspace, config, registry, "demo", purge=True, force=True)

    assert not workspace.app_dir("demo").exists()
    assert sentinel.is_file()
    assert (sibling / "f.txt").is_file()


# ---- observe_status --------------------------------------------------------


def test_observe_status_container_running(
    workspace, registry, config, fake_runtime
) -> None:
    _seed_container(workspace, registry, "api", deployed=True)
    # manifest 落 stopped，但容器在跑 → observe 改写 running
    fake_runtime._running = True
    observed = observe_status(workspace, config, registry, "api")
    assert observed == Status.RUNNING
    assert registry.get_instance("api")["status"] == "running"


def test_observe_status_container_stopped(
    workspace, registry, config, fake_runtime
) -> None:
    _seed_container(workspace, registry, "api", deployed=True)
    # manifest 落 running，容器实际没跑 → 改写 stopped
    from local_webpage_access.models import Status as S

    m = InstanceManifest.load(workspace.app_manifest_path("api"))
    m.status = S.RUNNING
    m.save(workspace.app_manifest_path("api"))
    registry.update_status("api", S.RUNNING.value)

    fake_runtime._running = False
    observed = observe_status(workspace, config, registry, "api")
    assert observed == Status.STOPPED
    assert registry.get_instance("api")["status"] == "stopped"


def test_observe_container_docker_permission_preserves_running(
    workspace, registry, config, monkeypatch
) -> None:
    """BUG-230：docker.sock 权限不足时不得把 running 误标为 stopped。"""
    from local_webpage_access.errors import DockerError
    from local_webpage_access.models import Status as S

    _seed_container(workspace, registry, "api", deployed=True)
    m = InstanceManifest.load(workspace.app_manifest_path("api"))
    m.status = S.RUNNING
    m.save(workspace.app_manifest_path("api"))
    registry.update_status("api", S.RUNNING.value)

    class _PermDeniedRuntime:
        def __init__(self, *a, **k) -> None:
            pass

        def is_running(self, iid):
            raise DockerError(
                "Docker 权限不足（无法访问 docker.sock）：请执行 `newgrp docker`"
            )

    monkeypatch.setattr(
        "local_webpage_access.docker_runtime.DockerRuntime", _PermDeniedRuntime
    )
    monkeypatch.setattr(
        "local_webpage_access.health.http_ok", lambda port, **kw: (False, None)
    )

    observed = observe_status(workspace, config, registry, "api")
    assert observed == Status.RUNNING
    row = registry.get_instance("api")
    assert row["status"] == "running"
    assert row["last_error"] and "权限" in row["last_error"]
    assert "newgrp" in row["last_error"] or "manager" in row["last_error"]
    assert row.get("observed_state") == "unknown"
    assert row.get("observation_error") == "permission_denied"
    assert row.get("runtime_access") == "permission_denied"
    assert row.get("last_trusted_state") == "running"


def test_observe_container_docker_permission_http_fallback_running(
    workspace, registry, config, monkeypatch
) -> None:
    """BUG-230：Docker 权限不足但 hostPort HTTP 可达时按 running。"""
    from local_webpage_access.errors import DockerError
    from local_webpage_access.models import Status as S

    _seed_container(workspace, registry, "api", deployed=True)
    m = InstanceManifest.load(workspace.app_manifest_path("api"))
    m.status = S.STOPPED
    m.save(workspace.app_manifest_path("api"))
    registry.update_status("api", S.STOPPED.value)

    class _PermDeniedRuntime:
        def __init__(self, *a, **k) -> None:
            pass

        def is_running(self, iid):
            raise DockerError(
                "Docker 权限不足（无法访问 docker.sock）：请执行 `newgrp docker`"
            )

    monkeypatch.setattr(
        "local_webpage_access.docker_runtime.DockerRuntime", _PermDeniedRuntime
    )
    monkeypatch.setattr(
        "local_webpage_access.health.http_ok", lambda port, **kw: (True, 200)
    )

    observed = observe_status(workspace, config, registry, "api")
    assert observed == Status.RUNNING
    assert registry.get_instance("api")["status"] == "running"


def test_observe_container_programming_error_preserves_trusted_state(
    workspace, registry, config, monkeypatch
) -> None:
    """BUG-240：TypeError 等编程异常不得伪装成干净 stopped。"""
    from local_webpage_access.models import Status as S

    _seed_container(workspace, registry, "api", deployed=True)
    registry.update_status("api", S.RUNNING.value)

    class _BrokenRuntime:
        def __init__(self, *a, **k) -> None:
            pass

        def is_running(self, iid):
            raise TypeError("programming bug")

    monkeypatch.setattr(
        "local_webpage_access.docker_runtime.DockerRuntime", _BrokenRuntime
    )
    observed = observe_status(workspace, config, registry, "api")
    row = registry.get_instance("api")
    assert observed == Status.RUNNING
    assert row["status"] == "running"
    assert row["observed_state"] == "unknown"
    assert row["observation_error"] == "unknown"
    assert row["runtime_access"] == "unknown"


def test_observe_status_no_change_no_event(
    workspace, registry, config, fake_runtime
) -> None:
    """观测状态与现状一致时不写 status_change 事件。"""
    _seed_container(workspace, registry, "api", deployed=True)
    fake_runtime._running = False  # 与 manifest stopped 一致
    events_before = registry.list_events("api")
    observe_status(workspace, config, registry, "api")
    events_after = registry.list_events("api")
    assert len(events_after) == len(events_before)


def test_observe_static_status_uses_health_when_pid_missing(
    workspace, registry, config, monkeypatch
) -> None:
    """BUG-052：PID 文件缺失但 HTTP 仍可用时，观测应为 running。"""
    from local_webpage_access.lifecycle import _observe_static_status
    from local_webpage_access.models import Status
    from tests._helpers import make_static_manifest

    workspace.ensure_app_dirs("demo")
    m = make_static_manifest("demo")
    m.status = Status.RUNNING
    m.static.enabled = True
    m.static.hostPort = 21100
    m.save(workspace.app_manifest_path("demo"))
    registry.upsert_from_manifest(m)
    registry.set_static_enabled("demo", True)

    monkeypatch.setattr(
        "local_webpage_access.static_gateway.StaticGateway.health_check",
        lambda self, port, **kw: port == 21100,
    )

    observed = _observe_static_status(workspace, config, registry, "demo")
    assert observed == Status.RUNNING


# ---- DEV-043 / BUG-071：状态模型区分 gateway_down / config_invalid ------------


def _patch_gateway_for_observe(
    monkeypatch,
    *,
    backend: str = "caddy",
    admin_alive: bool = False,
    health_ok: bool = False,
    pid_alive: bool = False,
) -> None:
    """桩 StaticGateway 探测方法，供 _observe_static_status 的 Caddy 区分测试。"""
    from local_webpage_access import static_gateway

    class _FakeGW:
        def __init__(self, *a, **kw):
            pass

        def detect_backend(self):
            return backend

        def _admin_alive(self, **kw):
            return admin_alive

        def health_check(self, port, **kw):
            return health_ok

        def _pid_alive(self, pid):
            return pid_alive

    monkeypatch.setattr(static_gateway, "StaticGateway", _FakeGW)


def _seed_enabled_static(workspace, registry, iid="demo", host_port=21100):
    from local_webpage_access.models import StaticConfig
    from tests._helpers import make_static_manifest

    workspace.ensure_app_dirs(iid)
    m = make_static_manifest(
        iid,
        desiredState=DesiredState.RUNNING,
        status=Status.RUNNING,
        static=StaticConfig(hostPort=host_port),
    )
    m.static.enabled = True
    m.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(m)
    registry.set_static_enabled(iid, True)
    return m


def test_observe_distinguishes_gateway_down(workspace, registry, config, monkeypatch) -> None:
    """BUG-071/DEV-043：Caddy master 挂掉时 enabled 实例标 gateway_down，不再误标 stopped。"""
    from local_webpage_access.lifecycle import _observe_static_status

    _seed_enabled_static(workspace, registry, "demo", host_port=21100)
    _patch_gateway_for_observe(
        monkeypatch, backend="caddy", admin_alive=False, health_ok=False
    )
    assert (
        _observe_static_status(workspace, config, registry, "demo")
        == Status.GATEWAY_DOWN
    )


def test_observe_config_invalid_when_master_up_but_site_unreachable(
    workspace, registry, config, monkeypatch
) -> None:
    """DEV-043：master 在线但站点 hostPort 不通 → config_invalid（路由/配置问题）。"""
    from local_webpage_access.lifecycle import _observe_static_status

    _seed_enabled_static(workspace, registry, "demo", host_port=21100)
    _patch_gateway_for_observe(
        monkeypatch, backend="caddy", admin_alive=True, health_ok=False
    )
    assert (
        _observe_static_status(workspace, config, registry, "demo")
        == Status.CONFIG_INVALID
    )


def test_observe_health_first_keeps_running_even_if_admin_down(
    workspace, registry, config, monkeypatch
) -> None:
    """健康优先：即便 admin 探测失败，只要 hostPort HTTP 可达仍判 running（向后兼容）。"""
    from local_webpage_access.lifecycle import _observe_static_status

    _seed_enabled_static(workspace, registry, "demo", host_port=21100)
    _patch_gateway_for_observe(
        monkeypatch, backend="caddy", admin_alive=False, health_ok=True
    )
    assert (
        _observe_static_status(workspace, config, registry, "demo") == Status.RUNNING
    )


def test_recover_instance_brings_up_gateway_then_restarts(
    workspace, registry, config, monkeypatch
) -> None:
    """DEV-043 recover：Caddy 静态 + master 离线时，先 maybe_start_gateway 再 restart。"""
    from local_webpage_access import gateway_service, lifecycle
    from local_webpage_access import static_gateway as sg

    _seed_enabled_static(workspace, registry, "demo", host_port=21100)

    calls: list[str] = []

    class _FakeGW:
        def __init__(self, *a, **kw):
            pass

        def detect_backend(self):
            return "caddy"

        def _admin_alive(self, **kw):
            return False

    monkeypatch.setattr(sg, "StaticGateway", _FakeGW)
    monkeypatch.setattr(
        gateway_service,
        "maybe_start_gateway",
        lambda *a, **kw: calls.append("gateway"),
    )
    monkeypatch.setattr(
        lifecycle,
        "_restart_instance_locked",
        lambda *a, **kw: calls.append("restart") or _seed_enabled_static(
            workspace, registry, "demo"
        ),
    )

    lifecycle.recover_instance(workspace, config, registry, "demo")
    assert calls == ["gateway", "restart"]


def test_recover_instance_skips_gateway_when_admin_already_up(
    workspace, registry, config, monkeypatch
) -> None:
    """recover：master 已在线时不重复拉网关，直接 restart。"""
    from local_webpage_access import gateway_service, lifecycle
    from local_webpage_access import static_gateway as sg

    _seed_enabled_static(workspace, registry, "demo", host_port=21100)
    calls: list[str] = []

    class _FakeGW:
        def __init__(self, *a, **kw):
            pass

        def detect_backend(self):
            return "caddy"

        def _admin_alive(self, **kw):
            return True

    monkeypatch.setattr(sg, "StaticGateway", _FakeGW)
    monkeypatch.setattr(
        gateway_service, "maybe_start_gateway", lambda *a, **kw: calls.append("gateway")
    )
    monkeypatch.setattr(
        lifecycle, "_restart_instance_locked", lambda *a, **kw: calls.append("restart")
    )
    lifecycle.recover_instance(workspace, config, registry, "demo")
    assert calls == ["restart"]


# ---- 回归测试：BUG-046 ----------------------------------------------------
#
# BUG-046：``instance_lock`` 在长耗时操作期间不刷新锁文件时间戳，
#          超过 ``_STALE_LOCK_SECONDS``（30 分钟）后会被另一进程误回收，
#          导致跨进程并发操作同一实例。修复后用后台心跳线程周期性刷新。


def test_instance_lock_heartbeat_refreshes_timestamp(workspace) -> None:
    """持锁期间心跳线程应刷新锁文件时间戳（BUG-046）。

    构造一个持锁 2 个心跳间隔的场景，验证锁文件第二行（时间戳）在持锁期间
    被刷新过（不是初始值）。
    """
    import os
    import time as _t

    from local_webpage_access import lifecycle

    # 把心跳间隔压到很短，让测试快速验证刷新
    monkeypatch_interval = 0.05
    original = lifecycle._LOCK_HEARTBEAT_INTERVAL
    lifecycle._LOCK_HEARTBEAT_INTERVAL = monkeypatch_interval
    try:
        lock_path = workspace.run / "lifecycle-hb.lock"
        with instance_lock(workspace, "hb"):
            ts_initial = lock_path.read_text(encoding="utf-8").strip().splitlines()[1]
            # 等待至少 2 个心跳间隔，让后台线程刷新
            _t.sleep(monkeypatch_interval * 4)
            ts_after = lock_path.read_text(encoding="utf-8").strip().splitlines()[1]
            # 时间戳应被刷新（增大）
            assert float(ts_after) > float(ts_initial)
            # 锁文件 PID 应仍是当前进程
            pid_line = lock_path.read_text(encoding="utf-8").strip().splitlines()[0]
            assert int(pid_line) == os.getpid()
    finally:
        lifecycle._LOCK_HEARTBEAT_INTERVAL = original


def test_instance_lock_heartbeat_keeps_lock_fresh(workspace, monkeypatch) -> None:
    """持锁超过 stale 阈值后锁仍不应被判为陈旧（BUG-046 核心）。

    通过缩短 ``_STALE_LOCK_SECONDS`` + ``_LOCK_HEARTBEAT_INTERVAL``，模拟
    长耗时 rebuild 场景：持锁时间 > stale 阈值，但因心跳刷新，
    ``_lock_is_stale`` 返回 False。
    """
    import time as _t

    from local_webpage_access import lifecycle

    monkeypatch.setattr(lifecycle, "_STALE_LOCK_SECONDS", 0.5)
    monkeypatch.setattr(lifecycle, "_LOCK_HEARTBEAT_INTERVAL", 0.1)
    lock_path = workspace.run / "lifecycle-long.lock"
    with instance_lock(workspace, "long", timeout=1):
        # 持锁 1s（> stale 0.5s），心跳每 0.1s 刷新一次
        _t.sleep(1.0)
        # 锁不应被判为 stale（因为心跳在持续刷新）
        assert lifecycle._lock_is_stale(lock_path) is False


# ---- 回归测试：BUG-047 ----------------------------------------------------
#
# BUG-047：``remove_instance`` 先写 remove 事件再删除实例，但 events 表
#          ``ON DELETE CASCADE`` 会把事件一起删掉，审计链断裂。修复后
#          remove 事件以 orphan event（instance_id=NULL）写入，不受级联影响。


def test_remove_keeps_audit_event_as_orphan(
    workspace, registry, config, fake_runtime
) -> None:
    """remove 后 remove 事件应作为孤儿事件保留（BUG-047）。"""
    _seed_container(workspace, registry, "api", deployed=True)
    remove_instance(workspace, config, registry, "api")

    # 实例已删
    assert registry.get_instance("api") is None
    # 但 remove 事件仍在（作为 instance_id=NULL 的孤儿事件）
    all_events = registry.list_events(None)
    remove_events = [e for e in all_events if e["event_type"] == "remove"]
    assert len(remove_events) >= 1
    # 孤儿事件的 instance_id 为 NULL
    assert remove_events[-1]["instance_id"] is None
    # message 中保留了实例 ID 文本，便于追溯
    assert "api" in remove_events[-1]["message"]


# ---- IMP-012：冗余实例批量清理 --------------------------------------------


def _seed_redundant(workspace, registry, iid: str, zip_bytes: bytes, created_at: str):
    """构造一个带 original.zip 的实例，并固定 created_at 用于冗余排序判定。"""
    _seed_container(workspace, registry, iid)
    zip_path = workspace.app_original_zip(iid)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    zip_path.write_bytes(zip_bytes)
    row = registry.get_instance(iid)
    row["created_at"] = created_at
    registry.upsert_instance(row)
    return row


_SAME_ZIP = b"identical-zip-bytes-for-grouping"
_OTHER_ZIP = b"different-content"


def test_list_redundant_keeps_oldest(workspace, registry, config) -> None:
    """IMP-012：同指纹分组保留 createdAt 最早者，其余为冗余。"""
    _seed_redundant(workspace, registry, "oldest", _SAME_ZIP, "2026-07-01T10:00:00")
    _seed_redundant(workspace, registry, "newer", _SAME_ZIP, "2026-07-02T10:00:00")
    _seed_redundant(workspace, registry, "unique", _OTHER_ZIP, "2026-07-03T10:00:00")

    redundant = list_redundant_instances(workspace, registry)
    ids = [r["id"] for r in redundant]
    assert ids == ["newer"]  # oldest 保留；unique 唯一不参与
    assert redundant[0]["sourceZipHash"]  # 带指纹


def test_redundant_ignores_empty_hash(workspace, registry, config) -> None:
    """IMP-012：无 original.zip 的实例不参与分组（空 hash 不参与）。"""
    _seed_redundant(workspace, registry, "a", _SAME_ZIP, "2026-07-01T10:00:00")
    _seed_redundant(workspace, registry, "b", _SAME_ZIP, "2026-07-02T10:00:00")
    # c 无 original.zip
    _seed_container(workspace, registry, "c")
    assert not workspace.app_original_zip("c").exists()

    redundant = list_redundant_instances(workspace, registry)
    assert [r["id"] for r in redundant] == ["b"]  # c 被排除


def test_remove_redundant_keeps_oldest_and_purges(workspace, registry, config) -> None:
    """IMP-012：remove_redundant 移除冗余、保留最早者并清理磁盘（purge）。"""
    _seed_redundant(workspace, registry, "keep", _SAME_ZIP, "2026-07-01T10:00:00")
    _seed_redundant(workspace, registry, "drop", _SAME_ZIP, "2026-07-02T10:00:00")

    removed = remove_redundant(workspace, config, registry, purge=True, force=True)
    assert removed == ["drop"]
    assert registry.get_instance("drop") is None
    assert registry.get_instance("keep") is not None  # 最早者保留
    assert not workspace.app_dir("drop").exists()  # purge 删了磁盘


def test_remove_redundant_none_when_all_unique(workspace, registry, config) -> None:
    """IMP-012：所有实例指纹唯一时无冗余可删。"""
    _seed_redundant(workspace, registry, "a", b"aaa", "2026-07-01T10:00:00")
    _seed_redundant(workspace, registry, "b", b"bbb", "2026-07-02T10:00:00")
    assert list_redundant_instances(workspace, registry) == []
    assert remove_redundant(workspace, config, registry) == []



# ---- IMP-014：容器实例路径别名 --------------------------------------------


def test_container_path_alias(workspace, registry, config, monkeypatch) -> None:
    """IMP-014：容器实例可设路径别名；_apply_gateway_alias 对容器跳过 is_enabled 守卫。"""
    from local_webpage_access import path_alias
    from local_webpage_access.path_alias import set_instance_path_alias

    _seed_container(workspace, registry, "api", deployed=True)  # hostPort=21000
    calls = {"gen": 0, "reload": 0}

    class _FakeGW:
        def __init__(self, ws, cfg):
            self.ws = ws

        def detect_backend(self):
            return "caddy"

        def is_enabled(self, iid):
            return False  # 容器无 static site conf；验证容器路径不依赖此守卫

        def generate_alias_config(self, iid, alias, hp):
            calls["gen"] += 1
            p = self.ws.app_alias_config(iid)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"reverse_proxy 127.0.0.1:{hp}\n", encoding="utf-8")

        def reload_all(self):
            calls["reload"] += 1

        def remove_alias_config(self, iid):
            p = self.ws.app_alias_config(iid)
            if p.is_file():
                p.unlink()

    monkeypatch.setattr(path_alias, "StaticGateway", _FakeGW)

    result = set_instance_path_alias(workspace, config, registry, "api", "api-alias")
    assert result.alias == "api-alias"
    assert result.gateway_reloaded is True  # 容器别名触发了 reload
    assert calls["gen"] == 1
    assert calls["reload"] == 1

    reloaded = InstanceManifest.load(workspace.app_manifest_path("api"))
    assert reloaded.container.routeMode == "name"
    assert reloaded.container.routeHost == "api-alias"
    # registry 容器表也记录了别名（IMP-014）
    crow = registry.get_container("api")
    assert crow["route_host"] == "api-alias"
    assert crow["route_mode"] == "name"


# ---- IMP-021：端口漂移同步别名片段 ----------------------------------------


def test_sync_alias_port_rewrites_when_drifted(
    workspace, registry, config, monkeypatch
) -> None:
    """IMP-021：别名片段端口与当前 hostPort 不一致 → 重写 + reload。"""
    from local_webpage_access import static_gateway as sg
    from local_webpage_access.lifecycle import _sync_alias_port

    manifest = _seed_container(workspace, registry, "api", deployed=True)
    manifest.container.routeMode = "name"
    manifest.container.routeHost = "api-alias"
    manifest.container.hostPort = 21001  # 模拟漂移后的新端口
    manifest.save(workspace.app_manifest_path("api"))

    # 旧别名片段仍指向 21000
    alias_conf = workspace.app_alias_config("api")
    alias_conf.parent.mkdir(parents=True, exist_ok=True)
    alias_conf.write_text("reverse_proxy 127.0.0.1:21000\n", encoding="utf-8")

    calls = {"reload": 0, "gen": 0}

    class _FakeGW:
        def __init__(self, ws, cfg):
            self.ws = ws

        def detect_backend(self):
            return "caddy"

        def generate_alias_config(self, iid, alias, hp):
            calls["gen"] += 1
            p = self.ws.app_alias_config(iid)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"reverse_proxy 127.0.0.1:{hp}\n", encoding="utf-8")

        def reload_all(self):
            calls["reload"] += 1

    monkeypatch.setattr(sg, "StaticGateway", _FakeGW)

    assert _sync_alias_port(workspace, config, "api", manifest) is True
    new_conf = workspace.app_alias_config("api").read_text(encoding="utf-8")
    assert "127.0.0.1:21001" in new_conf
    assert "127.0.0.1:21000" not in new_conf
    assert calls["gen"] == 1
    assert calls["reload"] == 1


def test_sync_alias_port_noop_when_unchanged(
    workspace, registry, config, monkeypatch
) -> None:
    """IMP-021：别名片段端口与 hostPort 一致 → 不重写、不 reload。"""
    from local_webpage_access import static_gateway as sg
    from local_webpage_access.lifecycle import _sync_alias_port

    manifest = _seed_container(workspace, registry, "api", deployed=True)
    manifest.container.routeMode = "name"
    manifest.container.routeHost = "api-alias"
    manifest.save(workspace.app_manifest_path("api"))

    host_port = manifest.container.hostPort  # 21000
    alias_conf = workspace.app_alias_config("api")
    alias_conf.parent.mkdir(parents=True, exist_ok=True)
    alias_conf.write_text(f"reverse_proxy 127.0.0.1:{host_port}\n", encoding="utf-8")

    class _FakeGW:
        def __init__(self, ws, cfg):
            pass

        def detect_backend(self):
            return "caddy"

        def generate_alias_config(self, *a, **kw):
            raise AssertionError("端口未漂移不应重写别名片段")

        def reload_all(self):
            raise AssertionError("端口未漂移不应 reload")

    monkeypatch.setattr(sg, "StaticGateway", _FakeGW)
    assert _sync_alias_port(workspace, config, "api", manifest) is False


def test_rebuild_syncs_drifted_alias_port(
    workspace, registry, config, fake_runtime, monkeypatch
) -> None:
    """IMP-021：rebuild（host_container 重新分配端口）后别名片段跟随漂移。"""
    from local_webpage_access import hosting
    from local_webpage_access import static_gateway as sg

    manifest = _seed_container(workspace, registry, "api", deployed=True)
    manifest.container.routeMode = "name"
    manifest.container.routeHost = "api-alias"
    manifest.save(workspace.app_manifest_path("api"))
    # 旧别名片段指向 21000（漂移前）
    alias_conf = workspace.app_alias_config("api")
    alias_conf.parent.mkdir(parents=True, exist_ok=True)
    alias_conf.write_text("reverse_proxy 127.0.0.1:21000\n", encoding="utf-8")

    class _FakeGW:
        def __init__(self, ws, cfg):
            self.ws = ws

        def detect_backend(self):
            return "caddy"

        def generate_alias_config(self, iid, alias, hp):
            p = self.ws.app_alias_config(iid)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"reverse_proxy 127.0.0.1:{hp}\n", encoding="utf-8")

        def reload_all(self):
            pass

    monkeypatch.setattr(sg, "StaticGateway", _FakeGW)
    # 模拟端口漂移：rebuild 时 host_container 分配到新端口 21001
    monkeypatch.setattr(
        hosting, "_ensure_container_port", lambda *a, **kw: (21001, True)
    )

    rebuild_instance(workspace, config, registry, "api")

    new_conf = workspace.app_alias_config("api").read_text(encoding="utf-8")
    assert "127.0.0.1:21001" in new_conf
    assert "127.0.0.1:21000" not in new_conf


# ---- BUG-084：容器别名在 start/restart 后保留 + 首启生成片段 ---------------


def _caddy_gateway_fake(monkeypatch, record):
    """注册一个记录 generate/reload 的 Caddy StaticGateway 替身。"""
    from local_webpage_access import static_gateway as sg

    class _FakeGW:
        def __init__(self, ws, cfg):
            self.ws = ws

        def detect_backend(self):
            return "caddy"

        def generate_alias_config(self, iid, alias, hp):
            record["gen"].append((alias, hp))
            p = self.ws.app_alias_config(iid)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"reverse_proxy 127.0.0.1:{hp}\n", encoding="utf-8")

        def reload_all(self):
            record["reload"] += 1

    monkeypatch.setattr(sg, "StaticGateway", _FakeGW)


def test_start_container_preserves_alias_in_network(
    workspace, registry, config, fake_runtime, monkeypatch
) -> None:
    """BUG-084：已部署容器 restart（轻量 start）后 network 仍保留别名，状态/API 可见。"""
    record = {"gen": [], "reload": 0}
    _caddy_gateway_fake(monkeypatch, record)

    manifest = _seed_container(workspace, registry, "api", deployed=True)
    manifest.container.routeMode = "name"
    manifest.container.routeHost = "api-alias"
    manifest.save(workspace.app_manifest_path("api"))
    registry.upsert_container("api", manifest.container.model_dump())

    fake_runtime._running = True
    result = restart_instance(workspace, config, registry, "api")
    # BUG-084：network 保留别名（不再被 build_network_entry 覆盖为 port）
    assert result.network.routeMode == "name"
    assert result.network.routeHost == "api-alias"
    assert result.network.routeUrl is not None
    # 状态层 _resolve_route 经 network 也能读到别名
    from local_webpage_access.status import _resolve_route

    alias, _url = _resolve_route(workspace, "api")
    assert alias == "api-alias"


def test_start_generates_alias_fragment_when_set_before_deploy(
    workspace, registry, config, fake_runtime, monkeypatch
) -> None:
    """BUG-084：别名在首次启动前设置（无 hostPort）→ start 时生成别名片段。"""
    record = {"gen": [], "reload": 0}
    _caddy_gateway_fake(monkeypatch, record)

    manifest = _seed_container(workspace, registry, "api", deployed=False)  # 无 hostPort
    manifest.container.routeMode = "name"
    manifest.container.routeHost = "api-alias"
    manifest.save(workspace.app_manifest_path("api"))
    registry.upsert_container("api", manifest.container.model_dump())

    result = start_instance(workspace, config, registry, "api")  # 首次部署
    # network 保留别名
    assert result.network.routeMode == "name"
    assert result.network.routeHost == "api-alias"
    # 别名片段已生成（start_instance 收尾 _sync_alias_port 触发）
    assert record["gen"], "首次启动应生成别名片段"
    assert workspace.app_alias_config("api").is_file()


# ---- IMP-022：builtin 后端设别名被拦截 ------------------------------------


def test_alias_set_blocks_builtin(workspace, registry, config, monkeypatch) -> None:
    """IMP-022：builtin 后端设置别名应明确报错，不再无声写元数据。"""
    from local_webpage_access import path_alias
    from local_webpage_access.errors import RecognitionError
    from local_webpage_access.path_alias import set_instance_path_alias

    _seed_container(workspace, registry, "api", deployed=True)

    class _FakeGW:
        def __init__(self, ws, cfg):
            pass

        def detect_backend(self):
            return "builtin"

    monkeypatch.setattr(path_alias, "StaticGateway", _FakeGW)

    with pytest.raises(RecognitionError):
        set_instance_path_alias(workspace, config, registry, "api", "api-alias")
    # 别名未写入 manifest（未无声落盘）
    reloaded = InstanceManifest.load(workspace.app_manifest_path("api"))
    assert reloaded.container.routeHost is None
    assert reloaded.container.routeMode == "port"


def test_alias_clear_allows_builtin(workspace, registry, config, monkeypatch) -> None:
    """IMP-022：清除别名（alias=None）在 builtin 下仍允许（清除恒安全）。"""
    from local_webpage_access import path_alias
    from local_webpage_access.path_alias import set_instance_path_alias

    manifest = _seed_container(workspace, registry, "api", deployed=True)
    manifest.container.routeMode = "name"
    manifest.container.routeHost = "api-alias"
    manifest.save(workspace.app_manifest_path("api"))
    registry.upsert_container("api", manifest.container.model_dump())

    class _FakeGW:
        def __init__(self, ws, cfg):
            pass

        def detect_backend(self):
            return "builtin"

        def remove_alias_config(self, iid):
            pass

    monkeypatch.setattr(path_alias, "StaticGateway", _FakeGW)

    result = set_instance_path_alias(workspace, config, registry, "api", None)
    assert result.alias is None
    reloaded = InstanceManifest.load(workspace.app_manifest_path("api"))
    assert reloaded.container.routeHost is None
    assert reloaded.container.routeMode == "port"


# ---- BUG-167：路径别名并发唯一性 --------------------------------------------


def test_concurrent_path_alias_rejects_duplicate(
    workspace, registry, config, monkeypatch
) -> None:
    """BUG-167：双线程同时设同一别名时，仅一方成功，另一方报冲突。"""
    from local_webpage_access import path_alias
    from local_webpage_access.errors import PathError
    from local_webpage_access.path_alias import set_instance_path_alias

    _seed_static(workspace, registry, "one")
    _seed_static(workspace, registry, "two")

    class _FakeGW:
        def __init__(self, ws, cfg):
            self.ws = ws

        def detect_backend(self):
            return "caddy"

        def is_enabled(self, iid):
            return True

        def generate_alias_config(self, iid, alias, hp):
            p = self.ws.app_alias_config(iid)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"reverse_proxy 127.0.0.1:{hp}\n", encoding="utf-8")

        def reload_all(self):
            pass

        def remove_alias_config(self, iid):
            p = self.ws.app_alias_config(iid)
            if p.is_file():
                p.unlink()

    monkeypatch.setattr(path_alias, "StaticGateway", _FakeGW)

    # 拉长网关写入窗口，放大「先查后写」竞态（修复前双双成功）。
    real_apply = path_alias._apply_gateway_alias

    def slow_apply(*args, **kwargs):
        import time as _t

        _t.sleep(0.05)
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(path_alias, "_apply_gateway_alias", slow_apply)

    successes: list[str] = []
    errors: list[str] = []
    barrier = threading.Barrier(2)

    def worker(iid: str) -> None:
        barrier.wait()
        try:
            set_instance_path_alias(
                workspace, config, registry, iid, "same-alias"
            )
            successes.append(iid)
        except PathError:
            errors.append(iid)

    t1 = threading.Thread(target=worker, args=("one",))
    t2 = threading.Thread(target=worker, args=("two",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(successes) == 1, successes
    assert len(errors) == 1, errors
    hosts = registry.list_route_hosts()
    assert hosts.get("same-alias") == successes[0]
    assert list(hosts.values()).count(successes[0]) == 1
