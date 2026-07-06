"""构建队列与并发限制（WBS-20）。

V1 默认构建并发为 1（设计 §16.2），保护 4G/8G 小主机免受并发构建 OOM。
并发数可通过 ``local-web.yml`` 的 ``buildConcurrency`` 配置（1~8）。

核心是 :class:`BuildQueue`：
* 用信号量限流（WBS-20.02/03/04）；
* 拿不到立即槽位时把实例标记为 ``queued``（WBS-20.05）；
* 排队/开始/结束写入 events（WBS-20.06）；
* 等待槽位超时抛 :class:`BuildQueueError`（WBS-20.07，与构建本身的超时分开）；
* :meth:`BuildQueue.cancel` 为取消预留接口（WBS-20.08）。

注意：信号量是**进程内、同一 registry 共享**限流，适用于 daemon 多线程构建调度；
多个独立 CLI 进程之间的互斥由各自的
:class:`~local_webpage_access.lifecycle.instance_lock` 在实例级保证（同一实例不会并发构建）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from local_webpage_access.config import Config
from local_webpage_access.errors import LifecycleError
from local_webpage_access.logging import get_logger
from local_webpage_access.models import Status
from local_webpage_access.registry import Registry

log = get_logger("build_queue")

# 排队等待槽位的默认超时（秒）；None 表示无限等待。
_DEFAULT_WAIT_TIMEOUT: float | None = 1800.0

# 进程内单例（BUG-022）：rebuild_instance 此前每次 ``BuildQueue(config, registry)``
# 新建实例，每个实例自带独立信号量，并发上限形同虚设。单例让同一进程内所有
# rebuild 共享一个信号量。跨进程互斥由 lifecycle.instance_lock 在实例级保证。
_global_queue: "BuildQueue | None" = None
_global_queue_guard = threading.Lock()


def get_build_queue(config: Config, registry: Registry) -> "BuildQueue":
    """返回进程内共享的 :class:`BuildQueue` 单例（BUG-022）。

    每次 ``BuildQueue(config, registry)`` 都会新建一个独立
    :class:`~threading.BoundedSemaphore`，于是 ``rebuild_instance`` 里
    "每次新建队列" 的写法让 ``buildConcurrency=1`` 形同虚设：两个并发
    rebuild 各自拿到自己的信号量，并行构建，小主机 OOM。

    单例保证同一进程内所有 rebuild 共享一个信号量，真正生效并发上限。
    若 ``config.buildConcurrency`` 变化，按新并发数重建；每次调用同步
    当前 ``registry``（测试可能用不同 DB 实例）。
    """
    global _global_queue
    with _global_queue_guard:
        if _global_queue is None or _global_queue.concurrency != max(
            1, config.buildConcurrency
        ):
            _global_queue = BuildQueue(config, registry)
        else:
            _global_queue.registry = registry
        return _global_queue


def _reset_global_queue() -> None:
    """丢弃进程内单例（测试隔离用）。"""
    global _global_queue
    with _global_queue_guard:
        _global_queue = None


@dataclass
class BuildTask:
    """构建任务描述（WBS-20.01）。"""

    instance_id: str
    status: str = "queued"  # queued / building / success / failed / cancelled
    queued_at: float | None = None
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None


@dataclass
class _BuildQueueState:
    """同一 registry + concurrency 共享的队列状态（BUG-022）。"""

    sem: threading.BoundedSemaphore
    tasks: dict[str, BuildTask]
    guard: threading.Lock


_states: dict[tuple[str, int], _BuildQueueState] = {}
_states_guard = threading.Lock()


def _shared_state(registry: Registry, concurrency: int) -> _BuildQueueState:
    key = (str(registry.db_path.resolve()), concurrency)
    with _states_guard:
        state = _states.get(key)
        if state is None:
            state = _BuildQueueState(
                sem=threading.BoundedSemaphore(concurrency),
                tasks={},
                guard=threading.Lock(),
            )
            _states[key] = state
        return state


class BuildQueue:
    """构建并发限流器（WBS-20.02~07）。

    Args:
        config: 提供 ``buildConcurrency``。
        registry: 写 events 与 QUEUED 状态。
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
        state = _shared_state(registry, self.concurrency)
        self._sem = state.sem
        self._tasks = state.tasks
        self._guard = state.guard

    # ---- 核心 API ----------------------------------------------------------

    def run(
        self,
        instance_id: str,
        builder: Callable[[str], Any],
        *,
        wait_timeout: float | None = None,
    ) -> Any:
        """排队执行一次构建（WBS-20.02/05/06）。

        ``builder`` 是真正执行构建的回调（如 ``host_container``），接收
        ``instance_id``，返回构建产物。本方法阻塞直到获得槽位并完成构建，
        返回 ``builder`` 的返回值。

        * 立即获得槽位 → 直接执行；
        * 需等待 → 实例标记 ``queued`` 并记录事件，获得槽位后执行；
        * 等待超时 → 抛 :class:`LifecycleError`（WBS-20.07）。
        """
        task = self._register_task(instance_id)
        timeout = wait_timeout if wait_timeout is not None else self.wait_timeout

        if not self._sem.acquire(blocking=False):
            # 需要排队
            self._mark_queued(instance_id, task)
            acquired = self._sem.acquire(timeout=timeout)
            if not acquired:
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
            self._sem.release()

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
        """
        with self._guard:
            return sum(1 for t in self._tasks.values() if t.status == "building")

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


__all__ = [
    "BuildTask",
    "BuildQueue",
    "get_build_queue",
    "_reset_global_queue",
]
