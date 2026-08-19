"""构建队列与并发限制（WBS-20 / DEV-047 / IMP-039）。

默认构建并发为 1（设计 §16.2），保护 4G/8G 小主机免受并发构建 OOM。
并发数可通过 ``local-web.yml`` 的 ``buildConcurrency`` 配置（1~8）。

**跨进程互斥（DEV-047）**：``rebuild`` 可由 CLI / 管理页 / daemon 三个独立
进程触发，进程内 :class:`~threading.BoundedSemaphore` 只在单进程内生效，
跨进程并发上限形同虚设。改用 :class:`CrossProcessBuildGate`——基于 SQLite
（``run/build-locks.db``，WAL）的计数信号量，所有进程连同一 DB 文件，
``BEGIN IMMEDIATE`` + ``busy_timeout`` 串行化槽位分配。同实例并发仍由
:class:`~local_webpage_access.lifecycle.instance_lock` 在实例级兜底。

**构建取消（IMP-039）**：
* ``queued → cancelled``：获槽后跳过 builder；
* ``building → cancelling → cancelled|cancel_failed``：杀进程树，不假报成功；
* 持久化 build token / owner PID+identity / worker pid+pgid，防 PID 复用误杀。
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from local_webpage_access.build_process import (
    enter_build_context,
    exit_build_context,
    get_build_process_hub,
    kill_pid_tree_if_matches,
    owner_process_identity,
)
from local_webpage_access.config import Config
from local_webpage_access.errors import BuildCancelled, LifecycleError
from local_webpage_access.logging import get_logger
from local_webpage_access.models import Status
from local_webpage_access.registry import Registry

log = get_logger("build_queue")

# 排队等待槽位的默认超时（秒）；None 表示无限等待。
_DEFAULT_WAIT_TIMEOUT: float | None = 1800.0

# 跨进程闸门轮询间隔（秒）：拿不到槽位时多久重试一次。
_GATE_POLL_INTERVAL = 0.1

# 取消进行中构建的默认等待超时（秒）。
_DEFAULT_CANCEL_WAIT = 60.0

_BUILD_TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"queued", "building", "cancelled", "failed"}),
    "building": frozenset({"building", "cancelling", "cancelled", "success", "failed"}),
    "cancelling": frozenset({"cancelling", "cancelled", "cancel_failed"}),
    "success": frozenset({"success"}),
    "failed": frozenset({"failed"}),
    "cancelled": frozenset({"cancelled"}),
    "cancel_failed": frozenset({"cancel_failed"}),
}

# 进程内单例（BUG-022）
_global_queue: "BuildQueue | None" = None
_global_queue_guard = threading.Lock()
# BUG-361：闸门按 (db_path, concurrency) 缓存；切换 concurrency 时须淘汰旧项。
_gates: dict[tuple[str, int], "CrossProcessBuildGate"] = {}
_gates_guard = threading.Lock()


def get_build_queue(config: Config, registry: Registry) -> "BuildQueue":
    """返回进程内共享的 :class:`BuildQueue` 单例（BUG-022）。"""
    global _global_queue
    with _global_queue_guard:
        if (
            _global_queue is None
            or _global_queue.concurrency != max(1, config.buildConcurrency)
            or _global_queue._gate.db_path != _gate_db_path(registry)  # noqa: SLF001
        ):
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
    # 同步清空闸门缓存，避免测试间泄漏 closed gate / SQLite 句柄。
    with _gates_guard:
        for gate in list(_gates.values()):
            try:
                gate.close()
            except Exception:  # noqa: BLE001
                pass
        _gates.clear()


def _gate_db_path(registry: Registry) -> Path:
    """闸门 DB 紧邻 registry DB（同目录），保证测试按工作区隔离。"""
    return registry.db_path.parent / "build-locks.db"


@dataclass
class BuildTask:
    """构建任务描述（WBS-20.01 / IMP-039）。"""

    instance_id: str
    status: str = "queued"  # queued/building/cancelling/success/failed/cancelled/cancel_failed
    queued_at: float | None = None
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    build_token: str = field(default_factory=lambda: uuid.uuid4().hex)
    cancel_requested_at: float | None = None


@dataclass
class CancelResult:
    """``cancel`` / ``cancel_build`` 的结构化结果（不假报成功）。"""

    instance_id: str
    outcome: str  # cancelled | cancelling | cancel_failed | noop | already_done
    previous_status: str | None = None
    message: str = ""

    def __bool__(self) -> bool:
        return self.outcome in ("cancelled", "cancelling")


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
        return True
    except (OSError, OverflowError):
        return False
    return True


class CrossProcessBuildGate:
    """基于 SQLite 的跨进程计数信号量 + 构建任务持久化（DEV-047 / IMP-039）。"""

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
        self._pending_kills: list[tuple[int, int | None, str]] = []  # 评审-组3
        self._conn: sqlite3.Connection | None = None
        self._closed = False
        self._init_schema()

    def _conn_or_open(self) -> sqlite3.Connection:
        # BUG-362：close 后禁止重开，避免 _closed=True 时残留连接永不关闭。
        if self._closed:
            raise RuntimeError("CrossProcessBuildGate 已关闭，不能再打开连接")
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=30,
                isolation_level=None,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
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
            conn.execute(
                "CREATE TABLE IF NOT EXISTS build_tasks ("
                "instance_id TEXT PRIMARY KEY, "
                "build_token TEXT NOT NULL, "
                "status TEXT NOT NULL, "
                "owner_pid INTEGER NOT NULL, "
                "owner_identity TEXT, "
                "worker_pid INTEGER, "
                "worker_pgid INTEGER, "
                "worker_identity TEXT, "
                "cancel_requested_at REAL, "
                "updated_at REAL NOT NULL)"
            )

    def _try_acquire(self, instance_id: str) -> int | None:
        """尝试立即获取一个槽位；无空闲返回 ``None``。"""
        with self._lock:
            conn = self._conn_or_open()
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._reclaim_dead(conn)
                taken = {row[0] for row in conn.execute("SELECT slot FROM build_slots")}
                slot = next((s for s in range(self.concurrency) if s not in taken), None)
                if slot is None:
                    conn.execute("ROLLBACK")
                    return None
                conn.execute(
                    "INSERT INTO build_slots(slot, instance_id, pid, acquired_at) VALUES(?,?,?,?)",
                    (slot, instance_id, os.getpid(), time.time()),
                )
                conn.execute("COMMIT")
                self._drain_pending_kills()
                return slot
            except sqlite3.OperationalError:
                # 评审-组3：database is locked 等瞬态冲突不直接上抛（否则 run()
                # 把任务误标 failed）；回滚后交 acquire 轮询重试
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                self._drain_pending_kills()
                return None
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                self._drain_pending_kills()
                raise

    def _reclaim_dead(self, conn: sqlite3.Connection) -> None:
        """回收持有者进程已死的槽位（崩溃残留）。

        评审-组3：进程终止（TERM 5s + KILL 5s）**移出** BEGIN IMMEDIATE 写事务
        ——此前在事务内 kill，build-locks.db 写锁可被长期持有，其它进程
        busy_timeout 30s 后抛 OperationalError 并把任务误标 failed。现改为：
        事务内只收集/改库，进程杀放到事务外（下次 acquire 前统一执行）。
        """
        rows = conn.execute("SELECT slot, pid FROM build_slots").fetchall()
        for slot, pid in rows:
            if not _pid_alive(int(pid)):
                conn.execute("DELETE FROM build_slots WHERE slot=?", (int(slot),))
        # 同步收尾 owner 已死且仍卡在 building/cancelling/queued 的任务行
        task_rows = conn.execute(
            "SELECT instance_id, owner_pid, status, worker_pid, worker_pgid, "
            "worker_identity FROM build_tasks "
            "WHERE status IN ('queued','building','cancelling')"
        ).fetchall()
        now = time.time()
        for (
            iid,
            owner_pid,
            status,
            worker_pid,
            worker_pgid,
            worker_identity,
        ) in task_rows:
            if not _pid_alive(int(owner_pid)):
                if worker_pid is not None:
                    # 事务外异步终止：身份三元组已从库行取出，杀错/杀漏由
                    # expected_pgid/expected_identity 校验兜底
                    self._pending_kills.append(
                        (
                            int(worker_pid),
                            int(worker_pgid) if worker_pgid is not None else None,
                            str(worker_identity or ""),
                        )
                    )
                conn.execute(
                    "UPDATE build_tasks SET status=?, "
                    "cancel_requested_at=COALESCE(cancel_requested_at, ?), "
                    "worker_pid=NULL, worker_pgid=NULL, worker_identity=NULL, "
                    "updated_at=? WHERE instance_id=?",
                    ("cancelled", now, now, iid),
                )

    def _drain_pending_kills(self) -> None:
        """执行 _reclaim_dead 收集的进程终止（在写事务外调用）。"""
        while self._pending_kills:
            worker_pid, expected_pgid, expected_identity = self._pending_kills.pop(0)
            with contextlib.suppress(Exception):  # noqa: BLE001 — best-effort
                kill_pid_tree_if_matches(
                    worker_pid,
                    expected_pgid=expected_pgid,
                    expected_identity=expected_identity,
                )

    def acquire(
        self,
        instance_id: str,
        timeout: float | None,
        *,
        build_token: str | None = None,
    ) -> int | None:
        """阻塞获取槽位，超时返回 ``None``；``timeout=None`` 无限等待。"""
        slot = self._try_acquire(instance_id)
        if slot is not None:
            return slot
        if timeout is not None and timeout <= 0:
            return None
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            if build_token is not None and self.is_cancel_requested(
                instance_id, build_token=build_token
            ):
                return None
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
            conn.execute(
                "DELETE FROM build_slots WHERE slot=? AND pid=?",
                (slot, os.getpid()),
            )

    def release_instance_slots(self, instance_id: str) -> None:
        """取消收尾：释放本进程为该实例持有的槽位（防泄漏）。"""
        with self._lock:
            conn = self._conn_or_open()
            conn.execute(
                "DELETE FROM build_slots WHERE instance_id=? AND pid=?",
                (instance_id, os.getpid()),
            )

    def active_slots(self) -> int:
        with self._lock:
            conn = self._conn_or_open()
            row = conn.execute("SELECT COUNT(*) FROM build_slots").fetchone()
            return int(row[0]) if row else 0

    # ---- build_tasks 持久化（IMP-039）--------------------------------------

    def upsert_build_task(
        self,
        *,
        instance_id: str,
        build_token: str,
        status: str,
        owner_pid: int,
        owner_identity: str,
        worker_pid: int | None = None,
        worker_pgid: int | None = None,
        worker_identity: str | None = None,
        cancel_requested_at: float | None = None,
        replace_generation: bool = False,
    ) -> bool:
        with self._lock:
            conn = self._conn_or_open()
            now = time.time()
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM build_tasks WHERE instance_id=?",
                    (instance_id,),
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO build_tasks("
                        "instance_id, build_token, status, owner_pid, owner_identity, "
                        "worker_pid, worker_pgid, worker_identity, "
                        "cancel_requested_at, updated_at"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            instance_id,
                            build_token,
                            status,
                            owner_pid,
                            owner_identity,
                            worker_pid,
                            worker_pgid,
                            worker_identity,
                            cancel_requested_at,
                            now,
                        ),
                    )
                elif row["build_token"] != build_token:
                    if not replace_generation:
                        conn.execute("ROLLBACK")
                        return False
                    conn.execute(
                        "UPDATE build_tasks SET build_token=?, status=?, owner_pid=?, "
                        "owner_identity=?, worker_pid=?, worker_pgid=?, "
                        "worker_identity=?, cancel_requested_at=?, updated_at=? "
                        "WHERE instance_id=?",
                        (
                            build_token,
                            status,
                            owner_pid,
                            owner_identity,
                            worker_pid,
                            worker_pgid,
                            worker_identity,
                            cancel_requested_at,
                            now,
                            instance_id,
                        ),
                    )
                else:
                    allowed = _BUILD_TASK_TRANSITIONS.get(
                        str(row["status"]), frozenset({str(row["status"])})
                    )
                    if status not in allowed:
                        conn.execute("ROLLBACK")
                        return False
                    conn.execute(
                        "UPDATE build_tasks SET status=?, owner_pid=?, "
                        "owner_identity=?, worker_pid=COALESCE(?, worker_pid), "
                        "worker_pgid=COALESCE(?, worker_pgid), "
                        "worker_identity=COALESCE(?, worker_identity), "
                        "cancel_requested_at=COALESCE(?, cancel_requested_at), "
                        "updated_at=? WHERE instance_id=? AND build_token=?",
                        (
                            status,
                            owner_pid,
                            owner_identity,
                            worker_pid,
                            worker_pgid,
                            worker_identity,
                            cancel_requested_at,
                            now,
                            instance_id,
                            build_token,
                        ),
                    )
                conn.execute("COMMIT")
                return True
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def update_build_task(
        self,
        instance_id: str,
        *,
        build_token: str,
        status: str | None = None,
        worker_pid: int | None = None,
        worker_pgid: int | None = None,
        worker_identity: str | None = None,
        cancel_requested_at: float | None = None,
        clear_worker: bool = False,
    ) -> bool:
        with self._lock:
            conn = self._conn_or_open()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM build_tasks WHERE instance_id=? AND build_token=?",
                (instance_id, build_token),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return False
            data = dict(row)
            if status is not None:
                allowed = _BUILD_TASK_TRANSITIONS.get(
                    str(data["status"]), frozenset({str(data["status"])})
                )
                if status not in allowed:
                    conn.execute("ROLLBACK")
                    return False
                data["status"] = status
            if clear_worker:
                data["worker_pid"] = None
                data["worker_pgid"] = None
                data["worker_identity"] = None
            else:
                if worker_pid is not None:
                    data["worker_pid"] = worker_pid
                if worker_pgid is not None:
                    data["worker_pgid"] = worker_pgid
                if worker_identity is not None:
                    data["worker_identity"] = worker_identity
            if cancel_requested_at is not None:
                data["cancel_requested_at"] = cancel_requested_at
            data["updated_at"] = time.time()
            conn.execute(
                "UPDATE build_tasks SET status=?, worker_pid=?, worker_pgid=?, "
                "worker_identity=?, cancel_requested_at=?, updated_at=? "
                "WHERE instance_id=? AND build_token=?",
                (
                    data["status"],
                    data["worker_pid"],
                    data["worker_pgid"],
                    data["worker_identity"],
                    data["cancel_requested_at"],
                    data["updated_at"],
                    instance_id,
                    build_token,
                ),
            )
            conn.execute("COMMIT")
            return True

    def get_build_task(self, instance_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._conn_or_open()
            row = conn.execute(
                "SELECT * FROM build_tasks WHERE instance_id=?", (instance_id,)
            ).fetchone()
            return dict(row) if row is not None else None

    def clear_build_task(self, instance_id: str) -> None:
        with self._lock:
            conn = self._conn_or_open()
            conn.execute("DELETE FROM build_tasks WHERE instance_id=?", (instance_id,))

    def is_cancel_requested(self, instance_id: str, *, build_token: str) -> bool:
        row = self.get_build_task(instance_id)
        if row is None or row["build_token"] != build_token:
            return False
        # BUG-278：跨进程取消写 status=cancelled + cancel_requested_at，owner 必须认
        return row["status"] in ("cancelling", "cancelled") or (
            row.get("cancel_requested_at") is not None
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None


class BuildQueue:
    """构建并发限流器（WBS-20.02~07 / DEV-047 / IMP-039）。"""

    def __init__(
        self,
        config: Config,
        registry: Registry,
        *,
        concurrency: int | None = None,
        wait_timeout: float | None = _DEFAULT_WAIT_TIMEOUT,
    ) -> None:
        self.registry = registry
        self.concurrency = concurrency if concurrency is not None else config.buildConcurrency
        if self.concurrency < 1:
            self.concurrency = 1
        self.wait_timeout = wait_timeout
        self._gate = _shared_gate(registry, self.concurrency)
        self._tasks: dict[str, BuildTask] = {}
        self._guard = threading.Lock()
        self._finish_events: dict[str, threading.Event] = {}

    # ---- 核心 API ----------------------------------------------------------

    def run(
        self,
        instance_id: str,
        builder: Callable[[str], Any],
        *,
        wait_timeout: float | None = None,
    ) -> Any:
        """排队执行一次构建；支持排队取消与进行中取消（IMP-039）。"""
        task = self._register_task(instance_id)
        timeout = wait_timeout if wait_timeout is not None else self.wait_timeout
        finish_ev = threading.Event()
        with self._guard:
            self._finish_events[instance_id] = finish_ev

        self._persist_task(task, status="queued", replace_generation=True)

        slot: int | None = None
        try:
            slot = self._gate.acquire(instance_id, 0.0)
            if slot is None:
                with self._guard:
                    if self._cancel_pending(task, instance_id):
                        self._finish_task_local(task, "cancelled")
                        finish_ev.set()
                        raise LifecycleError(
                            f"实例 {instance_id} 构建已取消",
                            instance_id=instance_id,
                        )
                self._mark_queued(instance_id, task)
                slot = self._gate.acquire(instance_id, timeout, build_token=task.build_token)
                if slot is None:
                    with self._guard:
                        if self._cancel_pending(task, instance_id):
                            self._finish_task_local(task, "cancelled")
                            finish_ev.set()
                            raise LifecycleError(
                                f"实例 {instance_id} 构建已取消",
                                instance_id=instance_id,
                            )
                    self._mark_timeout(instance_id, task, timeout)
                    finish_ev.set()
                    raise LifecycleError(
                        f"实例 {instance_id} 构建排队超时（{timeout}s）",
                        instance_id=instance_id,
                    )

            with self._guard:
                # BUG-278：跨进程 cancel 只改持久化行，须合并 DB 与内存状态
                if self._cancel_pending(task, instance_id):
                    self.registry.add_event(instance_id, "build_cancel", "排队任务已取消，跳过构建")
                    log.info("实例 %s 构建已取消，跳过 builder", instance_id)
                    self._finish_task_local(task, "cancelled")
                    raise LifecycleError(
                        f"实例 {instance_id} 构建已取消",
                        instance_id=instance_id,
                    )
                task.status = "building"
            task.started_at = time.time()
            if self._persist_task(task, status="building") is not True:  # 评审-组3：持久化异常返回 None 也不得放行
                with self._guard:
                    self._cancel_pending(task, instance_id)
                    self._finish_task_local(task, "cancelled")
                raise LifecycleError(
                    f"实例 {instance_id} 构建已取消",
                    instance_id=instance_id,
                )
            self.registry.add_event(instance_id, "build_start", "获得构建槽位，开始构建")
            enter_build_context(instance_id, build_token=task.build_token)
            try:
                result = builder(instance_id)
            finally:
                exit_build_context(instance_id)
            with self._guard:
                if self._cancel_pending(task, instance_id) or task.status in (
                    "cancelling",
                    "cancelled",
                ):
                    # 竞态：取消已发起但 builder 仍跑完 → 保留 cancelled，不改 success
                    self._finish_task_local(task, "cancelled")
                    raise LifecycleError(
                        f"实例 {instance_id} 构建已取消",
                        instance_id=instance_id,
                    )
                task.status = "success"
                task.finished_at = time.time()
            if self._persist_task(task, status="success") is not True:  # 评审-组3：持久化异常返回 None 也不得放行
                with self._guard:
                    self._cancel_pending(task, instance_id)
                    self._finish_task_local(task, "cancelled")
                raise LifecycleError(
                    f"实例 {instance_id} 构建已取消",
                    instance_id=instance_id,
                )
            return result
        except BuildCancelled as exc:
            with self._guard:
                task.status = "cancelled"
                task.error = str(exc)
                task.finished_at = time.time()
            self._persist_task(task, status="cancelled")
            self._update_instance_cancelled(instance_id, str(exc))
            raise LifecycleError(
                f"实例 {instance_id} 构建已取消",
                instance_id=instance_id,
            ) from exc
        except Exception as exc:
            with self._guard:
                if task.status in ("cancelled", "cancelling"):
                    task.status = "cancelled"
                    task.error = str(exc)
                    task.finished_at = time.time()
                    self._persist_task(task, status="cancelled")
                    self._update_instance_cancelled(instance_id, str(exc))
                    raise LifecycleError(
                        f"实例 {instance_id} 构建已取消",
                        instance_id=instance_id,
                    ) from exc
                task.status = "failed"
                task.error = str(exc)
                task.finished_at = time.time()
                self._persist_task(task, status="failed")
            raise
        finally:
            self._gate.release(slot)
            # 取消路径可能已提前 release；再清一次本实例残留
            if task.status in ("cancelled", "cancel_failed"):
                self._gate.release_instance_slots(instance_id)
            finish_ev.set()
            with self._guard:
                self._finish_events.pop(instance_id, None)

    def cancel(
        self,
        instance_id: str,
        *,
        wait_timeout: float = _DEFAULT_CANCEL_WAIT,
    ) -> CancelResult:
        """幂等取消构建（IMP-039）。

        * queued → 立即 cancelled（builder 永不调用）；
        * building → cancelling，杀进程树，等待 cancelled|cancel_failed；
        * 已终态 → already_done（不篡改）；
        * 无任务 → noop（不假报成功）。
        """
        hub = get_build_process_hub()

        with self._guard:
            task = self._tasks.get(instance_id)
            if task is not None:
                prev = task.status
                if prev in ("success", "failed", "cancelled", "cancel_failed"):
                    outcome = "cancelled" if prev == "cancelled" else "already_done"
                    if prev == "cancel_failed":
                        outcome = "cancel_failed"
                    return CancelResult(
                        instance_id=instance_id,
                        outcome=outcome,
                        previous_status=prev,
                        message=f"任务已处于终态 {prev}",
                    )
                if prev == "cancelling":
                    # 重复请求：等待同一最终态
                    pass
                elif prev == "queued":
                    task.status = "cancelled"
                    task.cancel_requested_at = time.time()
                    task.finished_at = time.time()
                    self._persist_task(task, status="cancelled")
                    self.registry.add_event(instance_id, "build_cancel", "排队任务已取消")
                    log.info("实例 %s 排队构建已取消", instance_id)
                    try:
                        self.registry.update_status(
                            instance_id,
                            Status.CANCELLED.value,
                            last_error="构建已取消",
                        )
                    except Exception:  # noqa: BLE001
                        log.exception("更新 cancelled 状态失败")
                    return CancelResult(
                        instance_id=instance_id,
                        outcome="cancelled",
                        previous_status="queued",
                        message="排队任务已取消",
                    )
                elif prev == "building":
                    task.status = "cancelling"
                    task.cancel_requested_at = time.time()
                    self._persist_task(
                        task,
                        status="cancelling",
                        cancel_requested_at=task.cancel_requested_at,
                    )
                    try:
                        self.registry.update_status(instance_id, Status.CANCELLING.value)
                    except Exception:  # noqa: BLE001
                        log.exception("更新 cancelling 状态失败")
                    self.registry.add_event(instance_id, "build_cancel", "正在取消进行中的构建")
                finish_ev = self._finish_events.get(instance_id)
            else:
                finish_ev = None
                prev = None

        # 无内存任务：尝试跨进程持久化行
        if task is None:
            row = self._gate.get_build_task(instance_id)
            if row is None or row["status"] in (
                "success",
                "failed",
                "cancelled",
                "cancel_failed",
            ):
                if row and row["status"] == "cancelled":
                    return CancelResult(
                        instance_id=instance_id,
                        outcome="cancelled",
                        previous_status="cancelled",
                        message="任务已取消",
                    )
                if row and row["status"] in ("success", "failed", "cancel_failed"):
                    return CancelResult(
                        instance_id=instance_id,
                        outcome="already_done"
                        if row["status"] != "cancel_failed"
                        else "cancel_failed",
                        previous_status=row["status"],
                        message=f"无活动构建（{row['status']}）",
                    )
                self.registry.add_event(instance_id, "build_cancel", "无活动构建，忽略取消请求")
                return CancelResult(
                    instance_id=instance_id,
                    outcome="noop",
                    previous_status=None,
                    message="无活动构建",
                )
            prev = row["status"]
            if prev == "queued":
                self._gate.update_build_task(
                    instance_id,
                    build_token=str(row["build_token"]),
                    status="cancelled",
                    cancel_requested_at=time.time(),
                )
                self.registry.add_event(instance_id, "build_cancel", "排队任务已取消（跨进程）")
                return CancelResult(
                    instance_id=instance_id,
                    outcome="cancelled",
                    previous_status="queued",
                    message="排队任务已取消",
                )
            # building / cancelling：标记并尝试按身份杀 worker
            self._gate.update_build_task(
                instance_id,
                build_token=str(row["build_token"]),
                status="cancelling",
                cancel_requested_at=time.time(),
            )
            signaled = self._signal_persisted_worker(row)
            if not signaled and not _pid_alive(int(row["owner_pid"])):
                self._gate.update_build_task(
                    instance_id,
                    build_token=str(row["build_token"]),
                    status="cancelled",
                )
                self._update_instance_cancelled(instance_id, "owner 已退出，任务已回收")
                return CancelResult(
                    instance_id=instance_id,
                    outcome="cancelled",
                    previous_status=prev,
                    message="owner 已退出，已回收任务",
                )
            # 等待 owner 收尾（轮询 DB）
            return self._wait_persisted_cancel(
                instance_id, previous_status=prev, wait_timeout=wait_timeout
            )

        # 本进程：本地杀树 + 等 finish
        hub.request_cancel(instance_id)
        row = self._gate.get_build_task(instance_id)
        if row is not None:
            self._signal_persisted_worker(row)

        if finish_ev is not None:
            finish_ev.wait(timeout=wait_timeout)

        with self._guard:
            final = self._tasks.get(instance_id)
            status_now = final.status if final else None

        if status_now == "cancelled":
            return CancelResult(
                instance_id=instance_id,
                outcome="cancelled",
                previous_status=prev,
                message="构建已取消",
            )
        if status_now in ("success", "failed"):
            return CancelResult(
                instance_id=instance_id,
                outcome="already_done",
                previous_status=status_now,
                message=f"构建在取消前已结束为 {status_now}",
            )
        # 超时仍卡在 cancelling
        with self._guard:
            if final is not None and final.status == "cancelling":
                final.status = "cancel_failed"
                final.finished_at = time.time()
                final.error = "取消超时，进程可能仍在运行"
        if final is not None:
            self._gate.update_build_task(
                instance_id,
                build_token=final.build_token,
                status="cancel_failed",
            )
        self.registry.add_event(instance_id, "build_cancel", "取消失败：等待构建退出超时")
        try:
            self.registry.update_status(
                instance_id,
                Status.FAILED.value,
                last_error="构建取消失败（超时）",
            )
        except Exception:  # noqa: BLE001
            log.exception("更新 cancel_failed 状态失败")
        return CancelResult(
            instance_id=instance_id,
            outcome="cancel_failed",
            previous_status=prev,
            message="取消超时",
        )

    def cancel_build(
        self,
        instance_id: str,
        *,
        wait_timeout: float = _DEFAULT_CANCEL_WAIT,
    ) -> CancelResult:
        """``cancel`` 的别名（WBS 039.04 命名）。"""
        return self.cancel(instance_id, wait_timeout=wait_timeout)

    def in_flight(self) -> int:
        with self._guard:
            return sum(1 for t in self._tasks.values() if t.status in ("building", "cancelling"))

    def global_in_flight(self) -> int:
        return self._gate.active_slots()

    def pending(self) -> list[str]:
        with self._guard:
            return [iid for iid, t in self._tasks.items() if t.status == "queued"]

    # ---- 内部 ---------------------------------------------------------------

    def _cancel_pending(self, task: BuildTask, instance_id: str) -> bool:
        """内存或跨进程持久化是否已请求/完成取消（BUG-278）。

        调用方须已持有 ``self._guard``（或可接受短暂读竞态）；会把 DB 取消态
        回写到内存 ``task``，避免后续仍按 queued/building 收尾。
        """
        if task.status in ("cancelled", "cancelling"):
            return True
        if not self._gate.is_cancel_requested(instance_id, build_token=task.build_token):
            return False
        row = self._gate.get_build_task(instance_id)
        status = (row or {}).get("status")
        if status == "cancelling":
            task.status = "cancelling"
            task.cancel_requested_at = (row or {}).get("cancel_requested_at") or time.time()
        else:
            task.status = "cancelled"
            task.cancel_requested_at = (row or {}).get("cancel_requested_at") or time.time()
            task.finished_at = task.finished_at or time.time()
        return True

    def _register_task(self, instance_id: str) -> BuildTask:
        task = BuildTask(instance_id=instance_id, queued_at=time.time())
        with self._guard:
            self._tasks[instance_id] = task
        return task

    def _persist_task(
        self,
        task: BuildTask,
        *,
        status: str,
        cancel_requested_at: float | None = None,
        replace_generation: bool = False,
    ) -> bool | None:
        try:
            return self._gate.upsert_build_task(
                instance_id=task.instance_id,
                build_token=task.build_token,
                status=status,
                owner_pid=os.getpid(),
                owner_identity=owner_process_identity(),
                cancel_requested_at=cancel_requested_at
                if cancel_requested_at is not None
                else task.cancel_requested_at,
                replace_generation=replace_generation,
            )
        except Exception:  # noqa: BLE001
            log.exception("持久化 build_task 失败")
            return None

    def _finish_task_local(self, task: BuildTask, status: str) -> None:
        task.status = status
        task.finished_at = time.time()
        self._persist_task(task, status=status)

    def _update_instance_cancelled(self, instance_id: str, message: str) -> None:
        try:
            self.registry.update_status(
                instance_id,
                Status.CANCELLED.value,
                last_error=message[:500],
            )
            # 收尾仍为 running 的 builds 行
            latest = self.registry.list_builds(instance_id, limit=1)
            if latest and latest[0].get("status") == "running":
                self.registry.finish_build(
                    int(latest[0]["id"]),
                    status="cancelled",
                    error_summary=message[:500],
                )
        except Exception:  # noqa: BLE001
            log.exception("实例取消收尾失败")

    def _signal_persisted_worker(self, row: dict[str, Any]) -> bool:
        worker_pid = row.get("worker_pid")
        if worker_pid is None:
            return False
        return kill_pid_tree_if_matches(
            int(worker_pid),
            expected_pgid=int(row["worker_pgid"]) if row.get("worker_pgid") is not None else None,
            expected_identity=str(row.get("worker_identity") or ""),
        )

    def _wait_persisted_cancel(
        self,
        instance_id: str,
        *,
        previous_status: str | None,
        wait_timeout: float,
    ) -> CancelResult:
        deadline = time.monotonic() + wait_timeout
        while time.monotonic() < deadline:
            row = self._gate.get_build_task(instance_id)
            if row is None:
                return CancelResult(
                    instance_id=instance_id,
                    outcome="cancelled",
                    previous_status=previous_status,
                    message="任务已清理",
                )
            st = row["status"]
            if st == "cancelled":
                return CancelResult(
                    instance_id=instance_id,
                    outcome="cancelled",
                    previous_status=previous_status,
                    message="构建已取消",
                )
            if st in ("success", "failed"):
                return CancelResult(
                    instance_id=instance_id,
                    outcome="already_done",
                    previous_status=st,
                    message=f"构建已结束为 {st}",
                )
            if st == "cancel_failed":
                return CancelResult(
                    instance_id=instance_id,
                    outcome="cancel_failed",
                    previous_status=previous_status,
                    message="取消失败",
                )
            time.sleep(0.1)
        row = self._gate.get_build_task(instance_id)
        if row is not None:
            self._gate.update_build_task(
                instance_id,
                build_token=str(row["build_token"]),
                status="cancel_failed",
            )
        return CancelResult(
            instance_id=instance_id,
            outcome="cancel_failed",
            previous_status=previous_status,
            message="跨进程取消等待超时",
        )

    def _mark_queued(self, instance_id: str, task: BuildTask) -> None:
        with self._guard:
            if task.status == "cancelled":
                return
            task.status = "queued"
        self._persist_task(task, status="queued")
        try:
            self.registry.update_status(instance_id, Status.QUEUED.value)
            self.registry.add_event(instance_id, "build_queue", "构建排队等待槽位")
        except Exception:  # noqa: BLE001
            log.exception("标记 queued 失败")

    def _mark_timeout(self, instance_id: str, task: BuildTask, timeout: float | None) -> None:
        task.status = "failed"
        task.error = f"构建排队超时（{timeout}s）"
        self._persist_task(task, status="failed")
        try:
            self.registry.update_status(
                instance_id,
                Status.FAILED.value,
                last_error=task.error,
            )
            self.registry.add_event(instance_id, "build_queue", task.error)
        except Exception:  # noqa: BLE001
            log.exception("记录排队超时事件失败")


# ---- 闸门单例 ---------------------------------------------------------------


def _shared_gate(registry: Registry, concurrency: int) -> CrossProcessBuildGate:
    db_path = str(_gate_db_path(registry).resolve())
    key = (db_path, concurrency)
    with _gates_guard:
        # BUG-361：同 db_path 不同 concurrency 的旧 gate 永不淘汰 → SQLite 连接泄漏。
        stale_keys = [k for k in _gates if k[0] == db_path and k != key]
        for sk in stale_keys:
            old = _gates.pop(sk, None)
            if old is not None:
                try:
                    old.close()
                except Exception:  # noqa: BLE001
                    pass
        gate = _gates.get(key)
        if gate is None or gate._closed:  # noqa: SLF001
            gate = CrossProcessBuildGate(_gate_db_path(registry), concurrency)
            _gates[key] = gate
        return gate


__all__ = [
    "BuildTask",
    "BuildQueue",
    "CancelResult",
    "CrossProcessBuildGate",
    "get_build_queue",
    "_reset_global_queue",
]
