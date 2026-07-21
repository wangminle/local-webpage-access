"""构建队列测试（WBS-20）。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from local_webpage_access.build_queue import (
    BuildQueue,
    BuildTask,
    _reset_global_queue,
    get_build_queue,
)
from local_webpage_access.errors import LifecycleError
from local_webpage_access.models import Status
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry


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
    from local_webpage_access.config import Config, PortPool

    return Config(portPool=PortPool(start=21000, end=21050))


@pytest.fixture(autouse=True)
def _isolate_global_queue():
    """每个用例独立的进程内单例（BUG-022 回归测试依赖）。"""
    _reset_global_queue()
    yield
    _reset_global_queue()


def _seed_instance(registry: Registry, iid: str = "api") -> None:
    from local_webpage_access.logging import now_iso

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


def test_cancel_noop_when_no_active_task(registry, config) -> None:
    """无活动构建时不得假报已取消（IMP-039 §20.1）。"""
    from local_webpage_access.build_queue import CancelResult

    _seed_instance(registry, "api")
    q = BuildQueue(config, registry, concurrency=1)
    result = q.cancel("api")
    assert isinstance(result, CancelResult)
    assert result.outcome == "noop"


def test_cancel_skips_queued_builder(registry, config) -> None:
    """cancel 后获槽的排队任务不得再执行 builder。"""
    _seed_instance(registry, "api")
    _seed_instance(registry, "api2")
    q = BuildQueue(config, registry, concurrency=1)
    release = threading.Event()
    ran: list[str] = []
    errors: list[BaseException] = []

    def blocking_builder(iid):
        release.wait(2.0)
        ran.append(iid)
        return iid

    def queued_builder(iid):
        ran.append(iid)
        return iid

    def run_api2() -> None:
        try:
            q.run("api2", queued_builder, wait_timeout=2.0)
        except BaseException as exc:  # noqa: BLE001 — 收集线程异常
            errors.append(exc)

    t1 = threading.Thread(target=lambda: q.run("api", blocking_builder))
    t1.start()
    # 等 api 占住槽位
    deadline = time.time() + 2
    while time.time() < deadline and q.in_flight() < 1:
        time.sleep(0.01)

    t2 = threading.Thread(target=run_api2)
    t2.start()
    deadline = time.time() + 2
    while time.time() < deadline:
        # 确认已进入闸门等待（active_slots 被 api 占满且 api2 在 tasks 中）
        if q.global_in_flight() >= 1:
            with q._guard:
                task = q._tasks.get("api2")
                if task is not None:
                    break
        time.sleep(0.01)
    # 给 api2 一点时间走到 acquire 等待
    time.sleep(0.15)

    result = q.cancel("api2")
    assert result.outcome == "cancelled"
    release.set()
    t1.join(timeout=3)
    t2.join(timeout=3)
    assert "api2" not in ran
    with q._guard:
        assert q._tasks["api2"].status == "cancelled"
    assert len(errors) == 1
    assert isinstance(errors[0], LifecycleError)
    assert "已取消" in str(errors[0])
    # 槽位已释放，无泄漏
    assert q.global_in_flight() == 0


def test_cancel_building_kills_process_tree(registry, config) -> None:
    """进行中构建：cancel 须终止子进程树，任务落到 cancelled，槽位释放（039.01）。"""
    import os
    import sys

    if sys.platform == "win32":
        pytest.skip("进程树取消用例以 POSIX shell 为准")

    from local_webpage_access.errors import BuildCancelled
    from local_webpage_access.hosting import run_command

    _seed_instance(registry, "api")
    q = BuildQueue(config, registry, concurrency=1, wait_timeout=5.0)
    child_pid_file = Path(registry.db_path).parent / "cancel-child.pid"
    log_path = Path(registry.db_path).parent / "cancel-build.log"
    errors: list[BaseException] = []
    started = threading.Event()

    def builder(iid: str):
        # BuildQueue.run 会 enter_build_context；此处仅发信号并跑可杀命令
        started.set()
        cmd = f"sleep 60 & echo $! > {child_pid_file}; wait"
        run_command(cmd, cwd=Path(registry.db_path).parent, log_path=log_path, timeout=120)
        return iid

    def run_build() -> None:
        try:
            q.run("api", builder)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=run_build)
    t.start()
    assert started.wait(5.0)
    deadline = time.time() + 5
    while time.time() < deadline and not child_pid_file.exists():
        time.sleep(0.05)
    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text().strip())

    result = q.cancel("api", wait_timeout=15.0)
    assert result.outcome == "cancelled", result
    t.join(timeout=20)
    time.sleep(0.3)

    alive = True
    try:
        os.kill(child_pid, 0)
    except (ProcessLookupError, PermissionError):
        alive = False
    assert not alive, f"孙进程 {child_pid} 取消后仍存活"
    assert q.global_in_flight() == 0
    with q._guard:
        assert q._tasks["api"].status == "cancelled"
    assert errors, "builder 线程应因取消而退出"
    assert any(
        isinstance(e, (LifecycleError, BuildCancelled)) or "取消" in str(e) for e in errors
    )


def test_cancel_idempotent_and_does_not_mutate_completed(registry, config) -> None:
    """重复 cancel 返回相同终态；已成功任务不被改成 cancelled。"""
    _seed_instance(registry, "api")
    q = BuildQueue(config, registry, concurrency=1)
    assert q.run("api", lambda iid: iid) == "api"
    with q._guard:
        assert q._tasks["api"].status == "success"
    r1 = q.cancel("api")
    assert r1.outcome == "already_done"
    with q._guard:
        assert q._tasks["api"].status == "success"
    r2 = q.cancel("api")
    assert r2.outcome == "already_done"


def test_cancel_race_with_normal_completion(registry, config) -> None:
    """取消与正常完成竞态：已 success 的不得被改成 cancelled。"""
    _seed_instance(registry, "api")
    q = BuildQueue(config, registry, concurrency=1)
    done = threading.Event()
    errors: list[BaseException] = []

    def fast_builder(iid):
        done.set()
        time.sleep(0.05)
        return iid

    def run_build() -> None:
        try:
            q.run("api", fast_builder)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=run_build)
    t.start()
    assert done.wait(2.0)
    # 尽量在即将完成时取消
    result = q.cancel("api", wait_timeout=3.0)
    t.join(timeout=3)
    with q._guard:
        final = q._tasks["api"].status
    assert final in ("success", "cancelled")
    if final == "success":
        assert result.outcome in ("already_done", "cancelled", "noop")
        with q._guard:
            assert q._tasks["api"].status == "success"
    else:
        assert result.outcome == "cancelled"
        assert errors


def test_cancel_pid_reuse_does_not_signal_unrelated(registry, config, tmp_path) -> None:
    """管理进程重启后，不得对 PID 复用的无关进程发信号（039.01 / §20.3）。"""
    import os
    import subprocess
    import sys

    from local_webpage_access.build_queue import CrossProcessBuildGate

    if sys.platform == "win32":
        pytest.skip("PID 复用身份校验用例以 POSIX 为准")

    _seed_instance(registry, "api")
    # 无关长驻进程
    decoy = subprocess.Popen(
        ["sleep", "60"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        q = BuildQueue(config, registry, concurrency=1)
        # 伪造持久化任务：worker_pid=decoy，但身份故意写错
        gate = q._gate
        gate.upsert_build_task(
            instance_id="api",
            build_token="fake-token",
            status="building",
            owner_pid=os.getpid(),
            owner_identity="not-a-real-owner-identity-xxxxx",
            worker_pid=decoy.pid,
            worker_pgid=os.getpgid(decoy.pid),
            worker_identity="npm-install-fake-identity",
        )
        # 内存中无对应 building 任务 → 走跨进程回收路径
        result = q.cancel("api", wait_timeout=2.0)
        assert result.outcome in ("cancel_failed", "noop", "already_done")
        # decoy 必须仍存活
        try:
            os.kill(decoy.pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        assert alive, "不得误杀 PID 复用的无关进程"
    finally:
        decoy.kill()
        decoy.wait(timeout=5)


def test_cancel_persists_owner_and_cancel_request(registry, config) -> None:
    """跨进程任务持久化含 build token、owner PID、状态与取消请求（039.02）。"""
    import sys

    if sys.platform == "win32":
        pytest.skip("持久化取消用例依赖 POSIX run_command")

    from local_webpage_access.hosting import run_command

    _seed_instance(registry, "api")
    q = BuildQueue(config, registry, concurrency=1)
    started = threading.Event()
    log_path = Path(registry.db_path).parent / "persist-cancel.log"
    errors: list[BaseException] = []

    def builder(iid):
        started.set()
        run_command(
            "sleep 60",
            cwd=Path(registry.db_path).parent,
            log_path=log_path,
            timeout=120,
        )
        return iid

    def run_build() -> None:
        try:
            q.run("api", builder)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=run_build)
    t.start()
    assert started.wait(3.0)
    # 等进入 building 持久化
    deadline = time.time() + 3
    row = None
    while time.time() < deadline:
        row = q._gate.get_build_task("api")
        if row and row["status"] == "building":
            break
        time.sleep(0.05)
    assert row is not None
    assert row["status"] == "building"
    assert row["owner_pid"] > 0
    assert row["build_token"]
    assert row["owner_identity"]

    result = q.cancel("api", wait_timeout=10.0)
    assert result.outcome == "cancelled"
    t.join(timeout=15)
    row2 = q._gate.get_build_task("api")
    if row2 is not None:
        assert row2["status"] in ("cancelled", "success", "failed", "cancel_failed")
        assert row2["status"] != "building"
        assert row2.get("cancel_requested_at") is not None or row2["status"] == "cancelled"
    assert errors


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
    from local_webpage_access.config import Config, PortPool

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


# ---- DEV-047：跨进程互斥 ----------------------------------------------------
#
# 进程内 BoundedSemaphore 只在单进程生效；CLI/管理页/daemon 三个独立进程触发
# rebuild 时必须靠 CrossProcessBuildGate（共享 SQLite DB）串行化。


def test_cross_process_gate_serializes_two_processes(tmp_path) -> None:
    """两个独立进程连同一闸门 DB：一方持槽位时另一方拿不到（跨进程互斥）。"""
    import subprocess
    import sys

    from local_webpage_access.build_queue import CrossProcessBuildGate

    db = tmp_path / "build-locks.db"
    gate = CrossProcessBuildGate(db, 1)
    parent_slot = gate.acquire("parent", 2.0)
    assert parent_slot == 0

    # 子进程尝试以短超时获取 → 必须超时失败（父进程占着唯一槽位）
    child_script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from local_webpage_access.build_queue import CrossProcessBuildGate\n"
        f"db = Path({str(db)!r})\n"
        "g = CrossProcessBuildGate(db, 1)\n"
        "slot = g.acquire('child', 0.6)\n"
        "sys.stdout.write('SLOT=' + str(slot) + '\\n')\n"
        "if slot is not None:\n"
        "    g.release(slot)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", child_script],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert "SLOT=None" in result.stdout, (
        f"子进程不应在父进程持槽时获取到：{result.stdout!r} {result.stderr!r}"
    )

    # 父进程释放后，子进程应能获取
    gate.release(parent_slot)
    result2 = subprocess.run(
        [sys.executable, "-c", child_script],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert "SLOT=0" in result2.stdout, (
        f"释放后子进程应获取槽位 0：{result2.stdout!r} {result2.stderr!r}"
    )
    gate.close()


def test_cross_process_gate_reclaims_dead_holder_slot(tmp_path) -> None:
    """持有槽位的进程崩溃（未 release）后，槽位应被按 pid 存活性回收。"""
    import subprocess
    import sys

    from local_webpage_access.build_queue import CrossProcessBuildGate

    db = tmp_path / "build-locks.db"
    # 子进程获取槽位后立即退出（不 release）→ 模拟崩溃
    leak_script = (
        "from pathlib import Path\n"
        "from local_webpage_access.build_queue import CrossProcessBuildGate\n"
        f"g = CrossProcessBuildGate(Path({str(db)!r}), 1)\n"
        "g.acquire('leaker', 2.0)\n"
        # 进程退出，slot 行残留
    )
    subprocess.run(
        [sys.executable, "-c", leak_script], capture_output=True, timeout=15, check=True
    )

    gate = CrossProcessBuildGate(db, 1)
    # acquire 时应回收死进程的槽位并获取成功
    slot = gate.acquire("after", 2.0)
    assert slot == 0
    gate.release(slot)
    gate.close()


def test_cross_process_cancel_queued_skips_builder(registry, config) -> None:
    """BUG-278：另一进程取消 queued 后，owner 获槽不得再跑 builder / 写 success。"""
    _seed_instance(registry, "blocker")
    _seed_instance(registry, "target")

    q_owner = BuildQueue(config, registry, concurrency=1)
    q_cancel = BuildQueue(config, registry, concurrency=1)
    assert q_owner._gate is q_cancel._gate

    blocker_started = threading.Event()
    release_blocker = threading.Event()
    target_waiting = threading.Event()
    builder_calls: list[str] = []
    owner_errors: list[BaseException] = []

    def blocker_builder(iid: str) -> str:
        blocker_started.set()
        assert release_blocker.wait(timeout=20.0)
        return iid

    def target_builder(iid: str) -> str:
        builder_calls.append(iid)
        return iid

    def run_blocker() -> None:
        q_owner.run("blocker", blocker_builder)

    def run_target() -> None:
        # 等 blocker 占住槽位后再排队，保证 target 进入 wait
        assert blocker_started.wait(timeout=5.0)
        target_waiting.set()
        try:
            q_owner.run("target", target_builder, wait_timeout=15.0)
        except BaseException as exc:  # noqa: BLE001
            owner_errors.append(exc)

    t_block = threading.Thread(target=run_blocker, name="blocker")
    t_target = threading.Thread(target=run_target, name="target")
    t_block.start()
    t_target.start()

    assert target_waiting.wait(timeout=5.0)
    # 确认 target 已持久化为 queued
    deadline = time.time() + 3.0
    row = None
    while time.time() < deadline:
        row = q_owner._gate.get_build_task("target")
        if row and row["status"] == "queued":
            break
        time.sleep(0.05)
    assert row is not None and row["status"] == "queued"

    result = q_cancel.cancel("target", wait_timeout=2.0)
    assert result.outcome == "cancelled"
    assert result.previous_status == "queued"
    row_cancelled = q_owner._gate.get_build_task("target")
    assert row_cancelled is not None
    assert row_cancelled["status"] == "cancelled"

    release_blocker.set()
    t_block.join(timeout=10.0)
    t_target.join(timeout=15.0)

    assert builder_calls == [], f"跨进程取消后仍执行了 builder: {builder_calls}"
    assert owner_errors, "owner 应因取消抛出 LifecycleError"
    assert all(isinstance(e, LifecycleError) for e in owner_errors)
    final = q_owner._gate.get_build_task("target")
    assert final is None or final["status"] == "cancelled"
