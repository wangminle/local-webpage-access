"""生命周期编排测试（WBS-17）。

用 fake DockerRuntime / fake gateway 验证 start / stop / restart / rebuild /
remove / observe_status 的派发、desiredState 一致性、并发锁与数据保护。
不依赖真实 Docker。
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from local_web_access.errors import LifecycleError
from local_web_access.lifecycle import (
    _lock_is_stale,
    _pid_alive,
    instance_lock,
    observe_status,
    rebuild_instance,
    remove_instance,
    restart_instance,
    start_instance,
    stop_instance_op,
)
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
from local_web_access.paths import Workspace
from local_web_access.registry import Registry


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
    monkeypatch.setattr("local_web_access.hosting.DockerRuntime", _FakeRuntime)
    monkeypatch.setattr("local_web_access.docker_runtime.DockerRuntime", _FakeRuntime)
    monkeypatch.setattr("local_web_access.hosting._http_ok", lambda port, **kw: True)
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
    """锁被"活跃进程"持有且超时 → LifecycleError。"""
    import os
    import time as _t

    lock_path = workspace.run / "lifecycle-api.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # 当前 PID + 当前时间戳 → 锁被视为"活跃持有"，不会被回收，只能等超时
    lock_path.write_text(f"{os.getpid()}\n{_t.time():.3f}\n", encoding="utf-8")
    with pytest.raises(LifecycleError, match="超时"):
        with instance_lock(workspace, "api", timeout=0.3):
            pass


def test_instance_lock_reclaims_stale(workspace, monkeypatch) -> None:
    """陈旧锁（持有进程已死）应被回收，新操作可获取。"""
    lock_path = workspace.run / "lifecycle-api.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # 用一个绝对不存在的 PID
    lock_path.write_text("999999\n0.0\n", encoding="utf-8")
    monkeypatch.setattr("local_web_access.lifecycle._STALE_LOCK_SECONDS", 999999)
    with instance_lock(workspace, "api", timeout=2):
        pass  # 不抛异常即说明陈旧锁已被回收


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
    from local_web_access.errors import LwaError

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
    from local_web_access.models import Status as S

    m = InstanceManifest.load(workspace.app_manifest_path("api"))
    m.status = S.RUNNING
    m.save(workspace.app_manifest_path("api"))
    registry.update_status("api", S.RUNNING.value)

    fake_runtime._running = False
    observed = observe_status(workspace, config, registry, "api")
    assert observed == Status.STOPPED
    assert registry.get_instance("api")["status"] == "stopped"


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
