"""构建队列测试（WBS-20）。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from local_web_access.build_queue import (
    BuildQueue,
    BuildTask,
    _reset_global_queue,
    get_build_queue,
)
from local_web_access.errors import LifecycleError
from local_web_access.models import Status
from local_web_access.paths import Workspace
from local_web_access.registry import Registry


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


@pytest.fixture(autouse=True)
def _isolate_global_queue():
    """每个用例独立的进程内单例（BUG-022 回归测试依赖）。"""
    _reset_global_queue()
    yield
    _reset_global_queue()


def _seed_instance(registry: Registry, iid: str = "api") -> None:
    from local_web_access.logging import now_iso

    registry.upsert_instance(
        {
            "id": iid,
            "name": iid,
            "version": "1",
            "kind": "python",
            "runtime": "docker-compose",
            "serving_mode": "container",
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
    )


# ---- 基础 -------------------------------------------------------------------


def test_build_queue_default_concurrency_from_config(registry, config) -> None:
    q = BuildQueue(config, registry)
    assert q.concurrency == config.buildConcurrency == 1


def test_build_queue_override_concurrency(registry, config) -> None:
    q = BuildQueue(config, registry, concurrency=3)
    assert q.concurrency == 3


def test_build_queue_clamps_below_one(registry, config) -> None:
    q = BuildQueue(config, registry, concurrency=0)
    assert q.concurrency == 1


def test_build_task_defaults() -> None:
    t = BuildTask(instance_id="api")
    assert t.status == "queued"
    assert t.queued_at is None


# ---- run：并发 1 串行化 ----------------------------------------------------


def test_run_serializes_with_concurrency_1(registry, config) -> None:
    """并发=1 时，两次构建必须串行执行，绝不重叠。"""
    _seed_instance(registry, "api")
    _seed_instance(registry, "api2")
    q = BuildQueue(config, registry, concurrency=1)

    overlap = {"n": 0, "max": 0, "cur": 0}
    guard = threading.Lock()

    def builder(iid):
        with guard:
            overlap["cur"] += 1
            overlap["max"] = max(overlap["max"], overlap["cur"])
        time.sleep(0.05)
        with guard:
            overlap["cur"] -= 1
        return iid

    threads = [
        threading.Thread(target=q.run, args=("api", builder)),
        threading.Thread(target=q.run, args=("api2", builder)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert overlap["max"] == 1  # 从未并发
    assert overlap["n"] == 0  # 无错误（这里 n 未记录，保持 0）


def test_separate_queue_instances_share_concurrency(registry, config) -> None:
    """BUG-022：每次新建 BuildQueue 也必须共享进程级构建槽位。"""
    _seed_instance(registry, "api")
    _seed_instance(registry, "api2")
    q1 = BuildQueue(config, registry, concurrency=1)
    q2 = BuildQueue(config, registry, concurrency=1)

    state = {"cur": 0, "max": 0}
    guard = threading.Lock()

    def builder(iid):
        with guard:
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
        time.sleep(0.1)
        with guard:
            state["cur"] -= 1
        return iid

    threads = [
        threading.Thread(target=q1.run, args=("api", builder)),
        threading.Thread(target=q2.run, args=("api2", builder)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert state["max"] == 1


def test_run_allows_parallel_with_concurrency_2(registry, config) -> None:
    """并发=2 时允许两个构建同时进行。"""
    _seed_instance(registry, "api")
    _seed_instance(registry, "api2")
    q = BuildQueue(config, registry, concurrency=2)

    state = {"cur": 0, "max": 0}
    guard = threading.Lock()
    barrier = threading.Barrier(2)

    def builder(iid):
        barrier.wait()  # 让两个线程同时进入
        with guard:
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
        time.sleep(0.1)
        with guard:
            state["cur"] -= 1
        return iid

    threads = [
        threading.Thread(target=q.run, args=("api", builder)),
        threading.Thread(target=q.run, args=("api2", builder)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert state["max"] == 2  # 两个构建并发执行


# ---- run：事件与状态 -------------------------------------------------------


def test_run_writes_build_events(registry, config) -> None:
    _seed_instance(registry, "api")
    q = BuildQueue(config, registry, concurrency=1)

    def builder(iid):
        return "ok"

    q.run("api", builder)
    events = registry.list_events("api")
    types = {e["event_type"] for e in events}
    assert "build_start" in types


def test_run_marks_queued_when_waiting(registry, config) -> None:
    """并发=1：第二个任务在等待期间实例状态应为 queued。"""
    _seed_instance(registry, "api")
    _seed_instance(registry, "api2")
    q = BuildQueue(config, registry, concurrency=1)

    first_started = threading.Event()
    second_checked = threading.Event()
    queued_seen = {"v": False}

    def slow_builder(iid):
        first_started.set()
        time.sleep(0.15)
        return iid

    def quick_builder(iid):
        # 第二个：此时第一个还在跑，应该处于 queued
        queued_seen["v"] = (
            registry.get_instance(iid)["status"] == Status.QUEUED.value
        )
        second_checked.set()
        return iid

    t1 = threading.Thread(target=q.run, args=("api", slow_builder))
    t2 = threading.Thread(target=q.run, args=("api2", quick_builder))
    t1.start()
    first_started.wait()
    t2.start()
    second_checked.wait()
    t1.join()
    t2.join()

    assert queued_seen["v"] is True
    # queued 事件被记录
    events = registry.list_events("api2")
    assert any(e["event_type"] == "build_queue" for e in events)


def test_run_failure_propagates_and_releases_slot(registry, config) -> None:
    """builder 抛异常 → run 重新抛出并释放槽位（后续构建仍可进行）。"""
    _seed_instance(registry, "api")
    _seed_instance(registry, "api2")
    q = BuildQueue(config, registry, concurrency=1)

    def fail_builder(iid):
        raise RuntimeError("构建炸了")

    def ok_builder(iid):
        return "recovered"

    with pytest.raises(RuntimeError, match="构建炸了"):
        q.run("api", fail_builder)

    # 槽位已释放，第二次构建能成功
    assert q.run("api2", ok_builder) == "recovered"
    assert q.in_flight() == 0


def test_in_flight_counts_active_build_not_semaphore_private_attr(registry, config) -> None:
    """BUG-031：in_flight 按任务生命周期统计，不依赖 Semaphore._value。

    构建进行中应返回 1，结束后回 0。这锁定新实现（计数 building 任务），
    防止回退到读取信号量私有属性 ``_value``。
    """
    import threading

    _seed_instance(registry, "api")
    q = BuildQueue(config, registry, concurrency=1)

    release = threading.Event()

    def blocking_builder(iid):
        release.wait(2.0)
        return iid

    t = threading.Thread(target=q.run, args=("api", blocking_builder))
    t.start()
    try:
        time.sleep(0.15)  # 等待拿到槽位并进入 building
        assert q.in_flight() == 1
    finally:
        release.set()
        t.join()
    assert q.in_flight() == 0


# ---- 超时 -------------------------------------------------------------------


def test_run_timeout_raises_lifecycle_error(registry, config) -> None:
    """排队等待超过 wait_timeout → LifecycleError。"""
    _seed_instance(registry, "api")
    _seed_instance(registry, "api2")
    q = BuildQueue(config, registry, concurrency=1)

    release = threading.Event()

    def blocking_builder(iid):
        release.wait(2.0)
        return iid

    def queued_builder(iid):
        return iid

    t1 = threading.Thread(target=q.run, args=("api", blocking_builder))
    t1.start()
    time.sleep(0.1)  # 让第一个拿到槽位

    with pytest.raises(LifecycleError, match="超时"):
        q.run("api2", queued_builder, wait_timeout=0.2)

    row = registry.get_instance("api2")
    assert row["status"] == Status.FAILED.value
    assert "排队超时" in row["last_error"]

    release.set()
    t1.join()


# ---- 回归测试：BUG-023 ----------------------------------------------------
#
# BUG-023：排队超时后 ``_mark_timeout`` 只记事件不改状态，实例永远卡在
#          ``queued``。修复后超时置 ``failed`` + ``last_error``，绝不残留 queued。


def test_queue_timeout_does_not_leave_status_queued(registry, config) -> None:
    """超时后状态必须离开 queued：先观测到 queued，再断言最终 failed + last_error。"""
    _seed_instance(registry, "api")
    _seed_instance(registry, "api2")
    q = BuildQueue(config, registry, concurrency=1)

    release = threading.Event()
    queued_seen = {"v": False}

    def blocking_builder(iid):
        # 期间 api2 应被标记为 queued
        time.sleep(0.15)
        queued_seen["v"] = (
            registry.get_instance("api2")["status"] == Status.QUEUED.value
        )
        release.wait(2.0)
        return iid

    def queued_builder(iid):
        return iid

    t1 = threading.Thread(target=q.run, args=("api", blocking_builder))
    t1.start()
    time.sleep(0.05)  # 让 api 抢到槽位

    with pytest.raises(LifecycleError, match="超时"):
        q.run("api2", queued_builder, wait_timeout=0.2)

    release.set()
    t1.join()

    # 排队期间确实进入过 queued
    assert queued_seen["v"] is True
    # 超时后已离开 queued，置 failed 并带 last_error
    row = registry.get_instance("api2")
    assert row["status"] == Status.FAILED.value
    assert row["status"] != Status.QUEUED.value
    assert row["last_error"] and "排队超时" in row["last_error"]


# ---- cancel / 状态查询 -----------------------------------------------------


def test_cancel_records_event(registry, config) -> None:
    _seed_instance(registry, "api")
    q = BuildQueue(config, registry, concurrency=1)
    q.cancel("api")
    events = registry.list_events("api")
    assert any(e["event_type"] == "build_cancel" for e in events)


def test_pending_lists_queued_tasks(registry, config) -> None:
    _seed_instance(registry, "api")
    _seed_instance(registry, "api2")
    q = BuildQueue(config, registry, concurrency=1)
    release = threading.Event()

    def blocking_builder(iid):
        release.wait(2.0)
        return iid

    def queued_builder(iid):
        return iid

    t1 = threading.Thread(target=q.run, args=("api", blocking_builder))
    t1.start()
    time.sleep(0.1)

    t2 = threading.Thread(target=q.run, args=("api2", queued_builder))
    t2.start()
    time.sleep(0.1)

    assert "api2" in q.pending()
    release.set()
    t1.join()
    t2.join()


# ---- 回归测试：BUG-022 ----------------------------------------------------
#
# BUG-022：``rebuild_instance`` 每次 ``BuildQueue(config, registry)`` 新建实例，
#          每个实例自带独立 BoundedSemaphore，``buildConcurrency=1`` 形同虚设：
#          两个并发 rebuild 各拿各的信号量，并行构建，小主机 OOM。修复后
#          ``get_build_queue`` 返回进程内单例，所有 rebuild 共享一个信号量。


def test_get_build_queue_returns_same_singleton(registry, config) -> None:
    """两次 get_build_queue 返回同一对象。"""
    q1 = get_build_queue(config, registry)
    q2 = get_build_queue(config, registry)
    assert q1 is q2


def test_get_build_queue_rebuilds_on_concurrency_change(registry, config) -> None:
    """buildConcurrency 变化时按新值重建单例。"""
    from local_web_access.config import Config, PortPool

    q1 = get_build_queue(config, registry)
    assert q1.concurrency == 1
    cfg2 = Config(buildConcurrency=3, portPool=PortPool(start=21000, end=21050))
    q2 = get_build_queue(cfg2, registry)
    assert q2 is not q1
    assert q2.concurrency == 3


def test_global_queue_serializes_across_separate_instances(registry, config) -> None:
    """BUG-022 核心：两个独立 get_build_queue 共享信号量，concurrency=1 串行。

    若回归（每次新建 BuildQueue），两个队列各自持有独立信号量，两个构建会
    并行 → overlap.max == 2。修复后共享单例信号量 → overlap.max == 1。
    """
    _seed_instance(registry, "api")
    _seed_instance(registry, "api2")
    # 模拟两次独立的 rebuild_instance 调用：各自通过工厂取队列
    q1 = get_build_queue(config, registry)
    q2 = get_build_queue(config, registry)
    assert q1 is q2  # 单例

    overlap = {"cur": 0, "max": 0}
    guard = threading.Lock()

    def builder(iid):
        with guard:
            overlap["cur"] += 1
            overlap["max"] = max(overlap["max"], overlap["cur"])
        time.sleep(0.1)
        with guard:
            overlap["cur"] -= 1
        return iid

    threads = [
        threading.Thread(target=q1.run, args=("api", builder)),
        threading.Thread(target=q2.run, args=("api2", builder)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert overlap["max"] == 1  # 共享信号量，绝不并发


def test_reset_global_queue_clears_singleton(registry, config) -> None:
    """_reset_global_queue 后下次 get_build_queue 返回新对象。"""
    q1 = get_build_queue(config, registry)
    _reset_global_queue()
    q2 = get_build_queue(config, registry)
    assert q1 is not q2
