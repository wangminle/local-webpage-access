"""构建队列与并发限制（WBS-20 / DEV-047）。

默认构建并发为 1（设计 §16.2），保护 4G/8G 小主机免受并发构建 OOM。
并发数可通过 ``local-web.yml`` 的 ``buildConcurrency`` 配置（1~8）。

**跨进程互斥（DEV-047）**：``rebuild`` 可由 CLI / 管理页 / daemon 三个独立
进程触发，进程内 :class:`~threading.BoundedSemaphore` 只在单进程内生效，
跨进程并发上限形同虚设。改用 :class:`CrossProcessBuildGate`——基于 SQLite
（``run/build-locks.db``，WAL）的计数信号量，所有进程连同一 DB 文件，
``BEGIN IMMEDIATE`` + ``busy_timeout`` 串行化槽位分配。同实例并发仍由
:class:`~local_webpage_access.lifecycle.instance_lock` 在实例级兜底。

核心 :class:`BuildQueue`：
* 用 :class:`CrossProcessBuildGate` 跨进程限流（WBS-20.02/03/04 + DEV-047）；
* 拿不到立即槽位时把实例标记为 ``queued``（WBS-20.05）；
* 排队/开始/结束写入 events（WBS-20.06）；
* 等待槽位超时抛 :class:`LifecycleError`（WBS-20.07，与构建本身的超时分开）；
* :meth:`BuildQueue.cancel` 为取消预留接口（WBS-20.08）。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from local_webpage_access.config import Config
from local_webpage_access.errors import LifecycleError
from local_webpage_access.logging import get_logger
from local_webpage_access.models import Status
from local_webpage_access.registry import Registry

log = get_logger("build_queue")

# 排队等待槽位的默认超时（秒）；None 表示无限等待。
_DEFAULT_WAIT_TIMEOUT: float | None = 1800.0

# 跨进程闸门轮询间隔（秒）：拿不到槽位时多久重试一次。
_GATE_POLL_INTERVAL = 0.1

# 进程内单例（BUG-022）：rebuild_instance 此前每次 ``BuildQueue(config, registry)``
# 新建实例，每个实例自带独立信号量，并发上限形同虚设。单例让同一进程内所有
# rebuild 共享一个闸门。跨进程互斥由 CrossProcessBuildGate 在 DB 级保证。
_global_queue: "BuildQueue | None" = None
_global_queue_guard = threading.Lock()


def get_build_queue(config: Config, registry: Registry) -> "BuildQueue":
    """返回进程内共享的 :class:`BuildQueue` 单例（BUG-022）。

    每次 ``BuildQueue(config, registry)`` 都会新建一个独立闸门连接，于是
    ``rebuild_instance`` 里"每次新建队列"的写法会让本进程内的限流失效。
    单例保证同一进程内所有 rebuild 共享一个闸门。跨进程互斥由
    :class:`CrossProcessBuildGate` 的共享 DB 文件保证（DEV-047）。
    若 ``config.buildConcurrency`` 变化，按新并发数重建；每次调用同步
    当前 ``registry``（测试可能用不同 DB 实例）。
    """
    global _global_queue
    with _global_queue_guard:
        if _global_queue is None or _global_queue.concurrency != max(
            1, config.buildConcurrency
        ) or _global_queue._gate.db_path != _gate_db_path(registry):  # noqa: SLF001
            _close_global_queue()
            _global_queue = BuildQueue(config, registry)
        else:
            _global_queue.registry = registry
        return _global_queue


def _close_global_queue() -> None:
    global _global_queue
    if _global_queue is not None:
        _global_queue._gate.close()  # noqa: SLF001
        _global_queue = None


def _reset_global_queue() -> None:
    """丢弃进程内单例（测试隔离用）。"""
    with _global_queue_guard:
        _close_global_queue()


def _gate_db_path(registry: Registry) -> Path:
    """闸门 DB 紧邻 registry DB（同目录），保证测试按工作区隔离。"""
    return registry.db_path.parent / "build-locks.db"


@dataclass
class BuildTask:
    """构建任务描述（WBS-20.01）。"""

    instance_id: str
    status: str = "queued"  # queued / building / success / failed / cancelled
    queued_at: float | None = None
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None


# ---- 跨进程闸门（DEV-047）----------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """跨平台检查进程是否存活（用于回收崩溃残留的槽位）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x1000, False, pid)  # SYNCHRONIZE
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 别人的进程，保守视为存活
    except (OSError, OverflowError):
        return False
    return True


class CrossProcessBuildGate:
    """基于 SQLite 的跨进程计数信号量（DEV-047）。

    用一张 ``build_slots`` 表（slot 为主键）实现 N 槽位互斥：acquire 时
    ``BEGIN IMMEDIATE`` 串行化分配，取最小空闲 slot 并 INSERT；release 删除该行。
    CLI / 管理页 / daemon 连同一 DB 文件，互斥天然跨进程生效。崩溃进程残留的
    槽位由 acquire 时按 pid 存活性回收（``_pid_alive``）。

    单进程内多线程通过 ``self._lock`` 串行访问持久连接；跨进程由 SQLite
    写锁（``busy_timeout`` 让等待者排队而非报错）串行。
    """

    def __init__(
        self,
        db_path: Path,
        concurrency: int,
        *,
        poll_interval: float = _GATE_POLL_INTERVAL,
    ) -> None:
        self.db_path = Path(db_path)
        self.concurrency = max(1, concurrency)
        self.poll_interval = poll_interval
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._closed = False
        self._init_schema()

    def _conn_or_open(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=30,
                isolation_level=None,  # 手动事务
                check_same_thread=False,
            )
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._conn = conn
        return self._conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._conn_or_open()
            conn.execute(
                "CREATE TABLE IF NOT EXISTS build_slots ("
                "slot INTEGER PRIMARY KEY, "
                "instance_id TEXT NOT NULL, "
                "pid INTEGER NOT NULL, "
                "acquired_at REAL NOT NULL)"
            )

    def _try_acquire(self, instance_id: str) -> int | None:
        """尝试立即获取一个槽位；无空闲返回 ``None``。"""
        with self._lock:
            conn = self._conn_or_open()
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._reclaim_dead(conn)
                taken = {row[0] for row in conn.execute("SELECT slot FROM build_slots")}
                slot = next(
                    (s for s in range(self.concurrency) if s not in taken), None
                )
                if slot is None:
                    conn.execute("ROLLBACK")
                    return None
                conn.execute(
                    "INSERT INTO build_slots(slot, instance_id, pid, acquired_at) "
                    "VALUES(?,?,?,?)",
                    (slot, instance_id, os.getpid(), time.time()),
                )
                conn.execute("COMMIT")
                return slot
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def _reclaim_dead(self, conn: sqlite3.Connection) -> None:
        """回收持有者进程已死的槽位（崩溃残留）。"""
        rows = conn.execute("SELECT slot, pid FROM build_slots").fetchall()
        for slot, pid in rows:
            if not _pid_alive(int(pid)):
                conn.execute("DELETE FROM build_slots WHERE slot=?", (int(slot),))

    def acquire(self, instance_id: str, timeout: float | None) -> int | None:
        """阻塞获取槽位，超时返回 ``None``；``timeout=None`` 无限等待。"""
        slot = self._try_acquire(instance_id)
        if slot is not None:
            return slot
        if timeout is not None and timeout <= 0:
            return None
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            slot = self._try_acquire(instance_id)
            if slot is not None:
                return slot
            if deadline is not None and time.monotonic() >= deadline:
                return None
            time.sleep(self.poll_interval)

    def release(self, slot: int | None) -> None:
        if slot is None:
            return
        with self._lock:
            conn = self._conn_or_open()
            conn.execute("DELETE FROM build_slots WHERE slot=?", (slot,))

    def active_slots(self) -> int:
        """当前已占用的槽位数（跨进程可见，DEBUG/观测用）。"""
        with self._lock:
            conn = self._conn_or_open()
            row = conn.execute("SELECT COUNT(*) FROM build_slots").fetchone()
            return int(row[0]) if row else 0

    def close(self) -> None:
        with self._lock:
            if self._conn is not None and not self._closed:
                try:
                    self._conn.close()
                finally:
                    self._closed = True


class BuildQueue:
    """构建并发限流器（WBS-20.02~07 / DEV-047 跨进程）。

    Args:
        config: 提供 ``buildConcurrency``。
        registry: 写 events 与 QUEUED 状态；其 db_path 决定闸门 DB 位置。
        concurrency: 显式覆盖并发数（主要用于测试）。
        wait_timeout: 等待槽位的超时秒数；``None`` 无限等待。
    """

    def __init__(
        self,
        config: Config,
        registry: Registry,
        *,
        concurrency: int | None = None,
        wait_timeout: float | None = _DEFAULT_WAIT_TIMEOUT,
    ) -> None:
        self.registry = registry
        self.concurrency = (
            concurrency if concurrency is not None else config.buildConcurrency
        )
        if self.concurrency < 1:
            self.concurrency = 1
        self.wait_timeout = wait_timeout
        self._gate = _shared_gate(registry, self.concurrency)
        # _tasks 仍为进程内视图：in_flight/pending 在本进程内统计，便于管理页展示。
        self._tasks: dict[str, BuildTask] = {}
        self._guard = threading.Lock()

    # ---- 核心 API ----------------------------------------------------------

    def run(
        self,
        instance_id: str,
        builder: Callable[[str], Any],
        *,
        wait_timeout: float | None = None,
    ) -> Any:
        """排队执行一次构建（WBS-20.02/05/06 + DEV-047 跨进程互斥）。

        ``builder`` 是真正执行构建的回调（如 ``host_container``），接收
        ``instance_id``，返回构建产物。本方法阻塞直到获得槽位并完成构建，
        返回 ``builder`` 的返回值。

        * 立即获得槽位 → 直接执行；
        * 需等待 → 实例标记 ``queued`` 并记录事件，获得槽位后执行；
        * 等待超时 → 抛 :class:`LifecycleError`（WBS-20.07）。

        槽位互斥跨进程生效（:class:`CrossProcessBuildGate`）。
        """
        task = self._register_task(instance_id)
        timeout = wait_timeout if wait_timeout is not None else self.wait_timeout

        slot = self._gate.acquire(instance_id, 0.0)
        if slot is None:
            # 需要排队
            self._mark_queued(instance_id, task)
            slot = self._gate.acquire(instance_id, timeout)
            if slot is None:
                self._mark_timeout(instance_id, task, timeout)
                raise LifecycleError(
                    f"实例 {instance_id} 构建排队超时（{timeout}s）",
                    instance_id=instance_id,
                )

        try:
            task.status = "building"
            task.started_at = time.time()
            self.registry.add_event(
                instance_id, "build_start", "获得构建槽位，开始构建"
            )
            result = builder(instance_id)
            task.status = "success"
            task.finished_at = time.time()
            return result
        except Exception as exc:
            task.status = "failed"
            task.error = str(exc)
            task.finished_at = time.time()
            raise
        finally:
            self._gate.release(slot)

    def cancel(self, instance_id: str) -> bool:
        """取消构建（WBS-20.08 预留）。

        V1 仅记录取消事件；**不抢占进行中的构建**（构建一旦开始应自然完成或超时）。
        排队中的任务可在下次获得槽位前被外部状态检查识别为 cancelled。
        """
        with self._guard:
            task = self._tasks.get(instance_id)
            if task and task.status == "queued":
                task.status = "cancelled"
        self.registry.add_event(
            instance_id, "build_cancel", "构建取消（V1 占位：不抢占进行中构建）"
        )
        log.info("实例 %s 构建取消（占位）", instance_id)
        return True

    def in_flight(self) -> int:
        """当前正在构建（已占用槽位）的任务数。

        按任务生命周期统计 ``building`` 状态的任务，而非读取信号量私有属性
        ``_value``（BUG-031）。``_value`` 是 CPython 实现细节，替代实现或未来
        版本可能不再暴露；且计数语义上"已获取槽位且正在构建"恰等于 status=building。
        跨进程总占用可用 :meth:`global_in_flight`。
        """
        with self._guard:
            return sum(1 for t in self._tasks.values() if t.status == "building")

    def global_in_flight(self) -> int:
        """跨进程当前已占用的构建槽位数（DEV-047）。"""
        return self._gate.active_slots()

    def pending(self) -> list[str]:
        """当前排队中的实例 ID 列表。"""
        with self._guard:
            return [
                iid for iid, t in self._tasks.items() if t.status == "queued"
            ]

    # ---- 内部 ---------------------------------------------------------------

    def _register_task(self, instance_id: str) -> BuildTask:
        task = BuildTask(instance_id=instance_id, queued_at=time.time())
        with self._guard:
            self._tasks[instance_id] = task
        return task

    def _mark_queued(self, instance_id: str, task: BuildTask) -> None:
        task.status = "queued"
        try:
            self.registry.update_status(instance_id, Status.QUEUED.value)
            self.registry.add_event(
                instance_id, "build_queue", "构建排队等待槽位"
            )
        except Exception:  # noqa: BLE001 — registry 写失败不影响调度
            log.exception("标记 queued 失败")

    def _mark_timeout(
        self, instance_id: str, task: BuildTask, timeout: float | None
    ) -> None:
        task.status = "failed"
        task.error = f"构建排队超时（{timeout}s）"
        try:
            # 排队超时直接置 failed 并写 last_error，避免实例永远卡在 queued
            # （BUG-023）；真实运行态可由 observe_status 后续观测校正。
            self.registry.update_status(
                instance_id,
                Status.FAILED.value,
                last_error=task.error,
            )
            self.registry.add_event(
                instance_id,
                "build_queue",
                task.error,
            )
        except Exception:  # noqa: BLE001
            log.exception("记录排队超时事件失败")


# ---- 闸门单例（同 DB + 同并发共享，跨 BuildQueue 实例复用连接）----------------

_gates: dict[tuple[str, int], CrossProcessBuildGate] = {}
_gates_guard = threading.Lock()


def _shared_gate(registry: Registry, concurrency: int) -> CrossProcessBuildGate:
    """同 db_path + 同并发的闸门单例（进程内复用连接；跨进程靠 DB 文件互斥）。"""
    key = (str(_gate_db_path(registry).resolve()), concurrency)
    with _gates_guard:
        gate = _gates.get(key)
        if gate is None or gate._closed:  # noqa: SLF001
            gate = CrossProcessBuildGate(_gate_db_path(registry), concurrency)
            _gates[key] = gate
        return gate


__all__ = [
    "BuildTask",
    "BuildQueue",
    "CrossProcessBuildGate",
    "get_build_queue",
    "_reset_global_queue",
]
