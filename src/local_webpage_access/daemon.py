"""Daemon 双模式与 Inbox Watcher（WBS-21）。

V1 的 daemon 是一个**确定性**的轻量轮询进程，不做复杂推理（设计 §11）：

* ``lwa daemon on``  —— 持久化开关为 ``enabled``，并以独立子进程启动 watcher；
* ``lwa daemon off`` —— 持久化开关为 ``disabled``，并通知 watcher 退出；
* ``lwa daemon status`` —— 报告 watcher 是否在运行（WBS-21.03/04）。

watcher 主循环（WBS-21.05~11）：

1. 周期性扫描 ``inbox/`` 下的 ``*.zip``（WBS-21.05）；
2. 跳过尚未写完的文件（mtime/size 在稳定窗口内未变化才处理，WBS-21.06）；
3. 调用 :class:`~local_webpage_access.importer.Importer` 自动导入（WBS-21.07）；
4. 对**可确定且非重型**的实例（tiny/small，非 pending）调用
   :func:`~local_webpage_access.lifecycle.start_instance` 自动启动（WBS-21.08）；
5. uncertain（识别为 pending）与 heavy/medium 项目保持 pending/stopped，
   等待人工或大模型 skill 介入（WBS-21.09，设计 §16.5）；
6. 全过程写入 ``logs/daemon.log``（WBS-21.10）；
7. 单实例 ``O_EXCL`` 文件锁确保同一工作区只有一个 watcher（WBS-21.11）。

systemd user service 安装说明见 ``docs/daemon-systemd.md``（WBS-21.12）。

**可测试性**：实际轮询循环 :func:`run_watcher` 接受注入的 ``stop_event``、
``poll_interval`` 与 ``process_fn``，单测无需启动真实子进程；子进程启停只覆盖
状态文件与锁的读写。
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from local_webpage_access.config import Config
from local_webpage_access.errors import LifecycleError, ZipImportError
from local_webpage_access.logging import get_logger, now_iso
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry

log = get_logger("daemon")

# ---- 常量 -------------------------------------------------------------------

DEFAULT_POLL_INTERVAL = 5.0  # 秒：inbox 扫描周期
DEFAULT_STABLE_SECONDS = 2.0  # 秒：文件 mtime/size 稳定窗口（防半写文件）
# DEV-042：周期自愈间隔——watcher 每隔该秒数跑一次 reconcile，恢复掉线的
# desired=running 实例（builtin 静态进程存活监管 + 容器轻量拉起）。
DEFAULT_SUPERVISE_INTERVAL = 60.0
STATE_FILENAME = "daemon.json"
LOCK_FILENAME = "daemon.lock"
START_LOCK_FILENAME = "daemon-start.lock"
LOG_FILENAME = "daemon.log"
DAEMON_START_TIMEOUT = 5.0
# 心跳超时回收（BUG-030）：watcher 每轮更新锁文件心跳时间戳，
# 超过此秒数未更新即视为 watcher 卡死，即使 PID 仍在也回收锁。
LOCK_HEARTBEAT_TIMEOUT = 60.0
_LOCK_HEARTBEAT_POLL_MULTIPLE = 4  # 心跳超时至少为 poll_interval 的 N 倍

# daemon 自动启动的资源档位白名单（其余 medium/heavy 不自动启动，设计 §16.5）
_AUTO_START_PROFILES = {"tiny", "small"}
_START_LOCK_MUTEX = threading.Lock()


# ---- 状态持久化（WBS-21.04）-------------------------------------------------


@dataclass
class DaemonState:
    """daemon 开关与运行态。"""

    enabled: bool = False
    pid: int | None = None
    started_at: str | None = None
    poll_interval: float = DEFAULT_POLL_INTERVAL

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def state_path(workspace: Workspace) -> Path:
    return workspace.run / STATE_FILENAME


def lock_path(workspace: Workspace) -> Path:
    return workspace.run / LOCK_FILENAME


def start_lock_path(workspace: Workspace) -> Path:
    return workspace.run / START_LOCK_FILENAME


def log_file_path(workspace: Workspace) -> Path:
    return workspace.logs / LOG_FILENAME


def read_state(workspace: Workspace) -> DaemonState | None:
    """读取持久化的 daemon 状态；不存在或损坏返回 ``None``。"""
    path = state_path(workspace)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return DaemonState(
            enabled=bool(data.get("enabled", False)),
            pid=int(data["pid"]) if data.get("pid") is not None else None,
            started_at=data.get("started_at"),
            poll_interval=float(data.get("poll_interval", DEFAULT_POLL_INTERVAL)),
        )
    except (TypeError, ValueError):
        return None


def write_state(workspace: Workspace, state: DaemonState) -> None:
    """写入 daemon 状态（WBS-21.04）。"""
    path = state_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def clear_state(workspace: Workspace) -> None:
    """清除状态文件（用于退出清理）。"""
    with contextlib.suppress(FileNotFoundError):
        state_path(workspace).unlink()


# ---- 进程存活探测 -----------------------------------------------------------


def is_pid_alive(pid: int) -> bool:
    """跨平台进程存活探测。复用 lifecycle 的实现逻辑。"""
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            SYNCHRONIZE = 0x00100000
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _terminate_pid(pid: int, *, timeout: float = 5.0) -> bool:
    """best-effort 终止进程，返回是否成功（或已不存在）。"""
    if pid <= 0:
        return True
    if not is_pid_alive(pid):
        return True
    try:
        if sys.platform == "win32":
            # Windows: taskkill 整个进程树
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        else:
            os.kill(pid, 15)  # SIGTERM
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if not is_pid_alive(pid):
                    return True
                time.sleep(0.1)
            os.kill(pid, 9)  # SIGKILL 兜底
    except (OSError, subprocess.SubprocessError):
        pass
    return not is_pid_alive(pid)


def is_running(workspace: Workspace, *, now_ts: float | None = None) -> bool:
    """watcher 是否在运行：状态 enabled、PID 存活、锁存在且心跳未超时。

    心跳超时判定见 :func:`_lock_is_stale`（BUG-030）。此前只检查 PID 存活，
    卡死的 watcher（PID 在但不再轮询）会被误判为"运行中"，导致 ``lwa daemon on``
    无法回收锁恢复服务。
    """
    state = read_state(workspace)
    if state is None or not state.enabled or not state.pid:
        return False
    if not is_pid_alive(state.pid):
        return False
    lock = lock_path(workspace)
    if not lock.is_file():
        return False
    stale_after = max(
        LOCK_HEARTBEAT_TIMEOUT,
        (state.poll_interval or DEFAULT_POLL_INTERVAL) * _LOCK_HEARTBEAT_POLL_MULTIPLE,
    )
    return not _lock_is_stale(lock, stale_after=stale_after, now_ts=now_ts)


# ---- 单实例锁（WBS-21.11）---------------------------------------------------


@contextlib.contextmanager
def daemon_lock(workspace: Workspace) -> Iterator[int]:
    """watcher 单实例文件锁（``O_CREAT | O_EXCL``）。

    持有进程 PID 写入锁文件；已存在的陈旧锁（PID 已死）会被回收。
    获取失败抛 :class:`OSError`（调用方据此判定"已有 daemon 在跑"）。
    """
    path = lock_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd: int | None = None
    try:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            if _lock_is_stale(path):
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            else:
                raise
        os.write(fd, f"{os.getpid()}\n{time.time():.3f}\n".encode())
        os.close(fd)
        fd = None
        yield os.getpid()
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(FileNotFoundError, PermissionError):
            path.unlink()


def _lock_is_stale(
    path: Path,
    *,
    stale_after: float = LOCK_HEARTBEAT_TIMEOUT,
    now_ts: float | None = None,
) -> bool:
    """锁是否陈旧（可回收）。

    陈旧的两种情形（BUG-030）：

    1. 持有进程已死（PID 探测失败）；
    2. 进程仍存活但**心跳超时**——watcher 可能卡死/死锁，PID 在但不再轮询更新心跳。

    此前只判 PID 存活，卡死的 watcher 会让锁永远无法回收，``lwa daemon on``
    无法恢复。锁文件第二行为心跳时间戳（获取时写入、watcher 每轮由
    :func:`touch_lock_heartbeat` 更新）。心跳缺失或格式损坏时保守判为陈旧。
    """
    try:
        content = path.read_text(encoding="utf-8").strip().splitlines()
        pid = int(content[0]) if content else 0
    except (OSError, ValueError):
        return True
    if not is_pid_alive(pid):
        return True
    if len(content) >= 2:
        try:
            heartbeat = float(content[1])
        except ValueError:
            return True
        now_ts = now_ts if now_ts is not None else time.time()
        age = now_ts - heartbeat
        if age > stale_after:
            log.warning(
                "daemon 锁 %s 持有进程 %d 存活但心跳超时（%.1fs > %.1fs），回收",
                path,
                pid,
                age,
                stale_after,
            )
            return True
    return False


def touch_lock_heartbeat(workspace: Workspace) -> None:
    """更新锁文件心跳时间戳（watcher 每轮调用）。

    用临时文件 + ``os.replace`` 原子替换，避免并发 ``_lock_is_stale`` 读到半写内容。
    锁文件不存在或不可读时为空操作（不应在持有锁时发生，但保守跳过）。
    """
    path = lock_path(workspace)
    try:
        content = path.read_text(encoding="utf-8").strip().splitlines()
    except OSError:
        return
    pid_line = content[0] if content else str(os.getpid())
    tmp = path.with_name(path.name + ".hb")
    try:
        tmp.write_text(f"{pid_line}\n{time.time():.3f}\n", encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


# ---- inbox 扫描与文件稳定性（WBS-21.05/06）--------------------------------


def scan_inbox(workspace: Workspace) -> list[Path]:
    """列出 inbox/ 下所有 ``*.zip``（不保证稳定）。"""
    inbox = workspace.inbox
    if not inbox.is_dir():
        return []
    return sorted(p for p in inbox.glob("*.zip") if p.is_file())


@dataclass
class _FileFingerprint:
    mtime: float
    size: int


def is_file_stable(
    path: Path,
    previous: _FileFingerprint | None,
    *,
    now_ts: float | None = None,
    stable_seconds: float = DEFAULT_STABLE_SECONDS,
) -> tuple[bool, _FileFingerprint]:
    """判断文件是否已写完（WBS-21.06）。

    连续两次观测的 ``mtime`` 与 ``size`` 都相同，且距上次观测已超过
    ``stable_seconds`` 时视为稳定。返回 ``(是否稳定, 当前指纹)``。
    """
    try:
        st = path.stat()
    except OSError:
        return False, _FileFingerprint(0.0, -1)
    current = _FileFingerprint(st.st_mtime, st.st_size)
    now_ts = now_ts if now_ts is not None else time.time()
    if previous is None:
        return False, current
    if current.mtime != previous.mtime or current.size != previous.size:
        return False, current
    # mtime 未变，但若文件刚出现仍可能未刷盘；要求距上次观测足够久
    stable = (now_ts - previous.mtime) >= stable_seconds or (
        now_ts - current.mtime
    ) >= stable_seconds
    return stable, current


# ---- 导入与自动启动决策（WBS-21.07/08/09）---------------------------------


def _archive_processed_marker(workspace: Workspace) -> Path:
    """已处理 zip 的标记文件路径。"""
    return workspace.run / "daemon-processed.json"


def load_processed_set(workspace: Workspace) -> set[str]:
    """加载已处理 zip 指纹集合（避免重复导入）。"""
    path = _archive_processed_marker(workspace)
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return {str(item) for item in data} if isinstance(data, list) else set()


def save_processed_set(workspace: Workspace, items: set[str]) -> None:
    path = _archive_processed_marker(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(sorted(items), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def processed_key(path: Path) -> str:
    """生成 daemon 去重 key：路径 + 文件大小 + mtime_ns。

    旧实现只记录路径，用户用同名新 zip 覆盖 inbox 文件后会被永久跳过。把文件
    指纹纳入 key 后，同一路径的新内容可以重新处理。
    """
    try:
        st = path.stat()
    except OSError:
        return str(path)
    return f"{path}|{st.st_size}|{st.st_mtime_ns}"


def _archive_processed_zip(workspace: Workspace, zip_path: Path) -> Path | None:
    """IMP-011：把处理完成的 zip 移入 ``inbox/processed/``（同名加时间戳）。

    物理移出 inbox 扫描视野，替代旧"留 inbox + 指纹表去重"机制，杜绝归档后
    按旧指纹重复导入。返回归档目标路径；源文件已不存在或移动失败时返回 None。
    """
    if not zip_path.is_file():
        return None
    dest_dir = workspace.inbox_processed
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = dest_dir / f"{zip_path.stem}-{stamp}{zip_path.suffix}"
    # 极端：同一秒归档两个同名 zip → 追加 pid 防覆盖
    if dest.exists():
        dest = dest_dir / f"{zip_path.stem}-{stamp}-{os.getpid()}{zip_path.suffix}"
    try:
        return shutil.move(str(zip_path), str(dest))
    except OSError:
        return None


def process_zip(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    zip_path: Path,
) -> dict[str, Any]:
    """处理单个 inbox zip：导入 + 按档位决定是否自动启动。

    返回处理摘要字典（``instance_id`` / ``action`` / ``note``）。
    任何异常都被捕获并记录，绝不向上冒泡打断 watcher 主循环。
    """
    from local_webpage_access.importer import Importer
    from local_webpage_access.lifecycle import start_instance

    summary: dict[str, Any] = {
        "zip": str(zip_path),
        "action": "skipped",
        "instance_id": None,
        "note": None,
    }
    try:
        importer = Importer(workspace, config, registry)
        # IMP-011：daemon 用 on_conflict="error"——slug 冲突时不再 silent 建 -2/-3，
        # 而是抛 ZipImportError（携带 instance_id），由下方专属分支记事件并归档。
        result = importer.import_zip(str(zip_path), on_conflict="error")
        iid = result.instance_id
        summary["instance_id"] = iid
        manifest = result.manifest

        if result.detection.pending:
            summary["action"] = "pending"
            summary["note"] = (
                f"未识别（{manifest.lastError or 'detection pending'}），"
                "已标记 pending，等待人工或 skill 介入"
            )
            log.info("daemon: %s → pending（%s）", iid, summary["note"])
            return summary

        profile = (
            manifest.resourceProfile.value
            if hasattr(manifest.resourceProfile, "value")
            else str(manifest.resourceProfile)
        )
        if profile not in _AUTO_START_PROFILES:
            summary["action"] = "pending"
            summary["note"] = (
                f"资源档位 {profile}，daemon 不自动启动，请人工确认后 lwa start"
            )
            log.info("daemon: %s → 不自动启动（%s）", iid, profile)
            return summary

        # 可确定且轻量：自动启动（WBS-21.08）
        start_instance(workspace, config, registry, iid)
        summary["action"] = "started"
        summary["note"] = f"已自动启动（profile={profile}）"
        log.info("daemon: %s → 自动启动（profile=%s）", iid, profile)
        return summary
    except ZipImportError as exc:
        # IMP-011：区分"slug 冲突"（携带 instance_id）与"zip 校验/解压失败"。
        if exc.context.get("instance_id"):
            conflict_id = exc.context["instance_id"]
            summary["action"] = "conflict"
            summary["instance_id"] = conflict_id
            summary["note"] = str(exc)
            # 对已存在的实例记事件，提示用户走 --update；写事件失败不影响归档
            with contextlib.suppress(Exception):
                registry.add_event(
                    conflict_id,
                    "import_conflict",
                    f"inbox 重复导入：{exc.message or str(exc)}",
                )
            log.warning(
                "daemon: %s 与已有实例 %s 冲突，跳过（提示 --update）",
                zip_path.name, conflict_id,
            )
            return summary
        # 无 instance_id → 校验/解压类失败，归入 failed（watcher 下轮重试）
        summary["action"] = "failed"
        summary["note"] = f"处理失败：{exc}"
        log.warning("daemon 处理 %s 失败：%s", zip_path.name, exc)
        return summary
    except Exception as exc:  # noqa: BLE001 — watcher 不能因单个 zip 崩溃
        summary["action"] = "failed"
        summary["note"] = f"处理失败：{exc}"
        log.exception("daemon 处理 %s 失败", zip_path)
        return summary


# ---- DEV-042：开机/守护自愈 reconcile ---------------------------------------


# 这些态不应被 reconcile 强拉（进行中或人工介入中），交给对应流程收尾
_RECONCILE_SKIP_STATUSES = {"pending", "queued", "building"}


def reconcile(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    *,
    restarter: Callable[[Workspace, Config, Registry, str], None] | None = None,
) -> list[str]:
    """DEV-042 / BUG-079：开机/守护自愈——恢复 ``desired=running`` 但实际未在跑的实例。

    扫描 registry，对期望运行却掉线的实例逐一调用 ``restarter``（默认
    :func:`~local_webpage_access.lifecycle.start_instance`，幂等——已 running 则无操作，
    builtin 静态进程死了会重新 spawn，容器走轻量 ``compose start``）。返回被尝试恢复的
    instance_id 列表（成功与否各实例独立处理，单实例失败不中断整体）。

    跳过条件：

    * ``desired_state≠running``（用户已停止，不该被拉起）；
    * ``status ∈ {pending, queued, building}``（过渡态，交给对应流程收尾）；
    * registry 标 ``running`` 且 :func:`~local_webpage_access.lifecycle.observe_status`
      仍判定为 running（含 BUG-079：陈旧 running 经观测验证后才恢复）；
    * Caddy 后端且网关被显式关闭（``run/gateway.json enabled=false``）——此时静态
      实例呈 ``gateway_down`` 是网关层决策，重启实例会触发 ``ensure_caddy_running``
      把 master 拉起，与用户的 ``lwa gateway off`` 冲突，故跳过。

    builtin 静态进程存活监管由本函数在 watcher 周期调用实现：进程死了 → observe 偏离 →
    下一轮 reconcile 重新 spawn。
    """
    from local_webpage_access.lifecycle import observe_status, start_instance

    restarter = restarter or start_instance

    # Caddy 后端：判断网关是否被显式关闭（避免与 lwa gateway off 冲突）
    caddy_gateway_off = False
    try:
        from local_webpage_access.static_gateway import StaticGateway

        if StaticGateway(workspace, config).detect_backend() == "caddy":
            from local_webpage_access.gateway_service import read_state as _read_gw

            gw = _read_gw(workspace)
            if gw is not None and not gw.enabled:
                caddy_gateway_off = True
    except Exception:  # noqa: BLE001 — 探测失败不影响主流程，按可恢复处理
        pass

    restarted: list[str] = []
    for row in registry.list_instances():
        if row.get("desired_state") != "running":
            continue
        iid = row["id"]
        runtime = row.get("runtime")
        registry_status = row.get("status")
        # pending/queued/building：过渡态，交给对应流程收尾，不 observe 不强拉
        if registry_status in _RECONCILE_SKIP_STATUSES:
            continue
        # BUG-079：registry 标 running 可能陈旧（宿主机重启/进程异常退出后未刷新），
        # observe 验证真实状态——若实际已掉线则按偏离处理；其余非 running 态
        # （stopped/failed/gateway_down/config_invalid）本就是已知未运行，直接恢复。
        actual_status = registry_status
        if registry_status == "running":
            try:
                actual_status = observe_status(workspace, config, registry, iid).value
            except Exception as exc:  # noqa: BLE001 — 观测失败保守视为仍 running，跳过
                log.debug("daemon reconcile: 观测 %s 失败，保守跳过：%s", iid, exc)
                continue
        if actual_status == "running":
            continue
        if caddy_gateway_off and runtime == "shared-static":
            log.debug("daemon reconcile: 网关被显式关闭，跳过 caddy 静态实例 %s", iid)
            continue
        try:
            restarter(workspace, config, registry, iid)
            restarted.append(iid)
            log.info(
                "daemon reconcile: 恢复实例 %s（%s → running）", iid, actual_status or "?"
            )
            with contextlib.suppress(Exception):
                registry.add_event(
                    iid,
                    "reconcile",
                    f"daemon 自愈：desired=running 但状态偏离（{actual_status}），已自动恢复",
                )
        except Exception as exc:  # noqa: BLE001 — 单实例恢复失败不中断
            log.warning("daemon reconcile: 恢复 %s 失败：%s", iid, exc)
    return restarted


# ---- watcher 主循环（WBS-21.05~10）-----------------------------------------


def run_watcher(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    *,
    stop_event: threading.Event | None = None,
    poll_interval: float | None = None,
    stable_seconds: float = DEFAULT_STABLE_SECONDS,
    process_fn: Callable[[Workspace, Config, Registry, Path], dict[str, Any]]
    | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    heartbeat: Callable[[], None] | None = None,
    supervise: Callable[[Workspace, Config, Registry], Any] | None = None,
    supervise_interval: float = DEFAULT_SUPERVISE_INTERVAL,
) -> None:
    """watcher 主循环（可注入 stop_event/process_fn/heartbeat/supervise，便于单测）。

    退出条件：``stop_event`` 被置位，或状态文件 ``enabled`` 被外部置为 False
    （``lwa daemon off`` 设置）。每轮起始调用 ``heartbeat``（若提供），刷新
    锁心跳以供 :func:`_lock_is_stale` 超时回收判定（BUG-030）。

    DEV-042：``supervise`` 每隔 ``supervise_interval`` 秒调用一次（默认
    :func:`reconcile`），周期恢复 ``desired=running`` 但掉线的实例，实现
    builtin 静态进程存活监管。
    """
    stop_event = stop_event or threading.Event()
    poll_interval = (
        poll_interval if poll_interval is not None else DEFAULT_POLL_INTERVAL
    )
    process_fn = process_fn or process_zip
    fingerprints: dict[str, _FileFingerprint] = {}

    log.info("daemon watcher 启动（poll=%.1fs）", poll_interval)
    last_supervise = clock()
    while not stop_event.is_set():
        # 每轮起始刷新心跳，让卡死可被 _lock_is_stale 探测
        if heartbeat is not None:
            heartbeat()
        # 外部 off：状态文件 enabled=False → 退出
        state = read_state(workspace)
        if state is not None and not state.enabled:
            log.info("daemon 状态为 disabled，退出 watcher")
            break

        # DEV-042：周期自愈——定期恢复 desired=running 但掉线的实例
        if supervise is not None and (clock() - last_supervise) >= supervise_interval:
            last_supervise = clock()
            try:
                supervise(workspace, config, registry)
            except Exception:  # noqa: BLE001 — 自愈失败不中断 watcher
                log.exception("daemon supervise（reconcile）失败")

        processed = load_processed_set(workspace)
        for zip_path in scan_inbox(workspace):
            key = processed_key(zip_path)
            if key in processed:
                continue
            previous = fingerprints.get(str(zip_path))
            stable, fp = is_file_stable(
                zip_path, previous, now_ts=clock(), stable_seconds=stable_seconds
            )
            fingerprints[str(zip_path)] = fp
            if not stable:
                log.debug("daemon: %s 尚未稳定，等待", zip_path.name)
                continue
            log.info("daemon: 处理 %s", zip_path.name)
            summary = process_fn(workspace, config, registry, zip_path)
            if isinstance(summary, dict) and summary.get("action") == "failed":
                log.warning("daemon: %s 处理失败，保留待下轮重试", zip_path.name)
            else:
                processed.add(key)
                processed.add(str(zip_path))
                save_processed_set(workspace, processed)
                # IMP-011：终态（started/pending/conflict）后把 zip 物理移入
                # inbox/processed/，从扫描视野移除，避免重复导入与 -2/-3 冗余。
                if _archive_processed_zip(workspace, zip_path) is not None:
                    log.info("daemon: %s 已归档至 inbox/processed/", zip_path.name)
            # 处理完成后从指纹表移除，避免无界增长
            fingerprints.pop(str(zip_path), None)

        if stop_event.wait(poll_interval):
            break

    log.info("daemon watcher 已停止")


# ---- 启停子进程（WBS-21.01/02）---------------------------------------------


def _spawn_watcher(workspace: Workspace, poll_interval: float) -> int:
    """以独立子进程启动 watcher，返回 PID。"""
    root = str(workspace.root)
    cmd = [
        sys.executable,
        "-m",
        "local_webpage_access.daemon",
        "--workspace",
        root,
        "--poll",
        str(poll_interval),
    ]
    popen_kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        popen_kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **popen_kwargs)  # noqa: S603
    return int(proc.pid)


@contextlib.contextmanager
def daemon_start_lock(workspace: Workspace, *, timeout: float = 5.0) -> Iterator[None]:
    """串行化 ``daemon on``，避免并发父进程各自 spawn watcher。"""
    with _START_LOCK_MUTEX:
        path = start_lock_path(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd: int | None = None
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.write(fd, f"{os.getpid()}\n".encode())
                break
            except FileExistsError:
                if _lock_is_stale(path):
                    with contextlib.suppress(FileNotFoundError):
                        path.unlink()
                    continue
                if time.monotonic() >= deadline:
                    raise LifecycleError("daemon 启动锁被占用，稍后重试")
                time.sleep(0.05)
        try:
            yield
        finally:
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
            with contextlib.suppress(FileNotFoundError, PermissionError):
                path.unlink()


def _lock_pid(path: Path) -> int | None:
    try:
        content = path.read_text(encoding="utf-8").strip().splitlines()
        return int(content[0]) if content else None
    except (OSError, ValueError):
        return None


def _live_watcher_lock_pid(workspace: Workspace) -> int | None:
    path = lock_path(workspace)
    if not path.is_file() or _lock_is_stale(path):
        return None
    pid = _lock_pid(path)
    return pid if pid and is_pid_alive(pid) else None


def _wait_for_watcher_start(
    workspace: Workspace, pid: int, *, timeout: float = DAEMON_START_TIMEOUT
) -> bool:
    """等待 watcher 子进程拿到运行锁；进程提前退出则返回 False。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_pid_alive(pid):
            return False
        lock_pid = _live_watcher_lock_pid(workspace)
        if lock_pid == pid:
            return True
        time.sleep(0.05)
    return False


def start_daemon(
    workspace: Workspace,
    config: Config,
    *,
    poll_interval: float | None = None,
) -> int:
    """``lwa daemon on``：持久化 enabled 并以子进程启动 watcher（WBS-21.01）。

    若已有 watcher 在跑，直接返回其 PID。返回当前 watcher PID。
    """
    with daemon_start_lock(workspace):
        state = read_state(workspace)
        if state and state.enabled and state.pid and is_running(workspace):
            log.info("daemon 已在运行（pid=%s），不重复启动", state.pid)
            return int(state.pid)

        existing_pid = _live_watcher_lock_pid(workspace)
        if existing_pid is not None:
            poll = poll_interval if poll_interval is not None else DEFAULT_POLL_INTERVAL
            write_state(
                workspace,
                DaemonState(
                    enabled=True,
                    pid=existing_pid,
                    started_at=now_iso(),
                    poll_interval=poll,
                ),
            )
            log.info("daemon 已由运行锁恢复状态（pid=%s）", existing_pid)
            return existing_pid

        poll = poll_interval if poll_interval is not None else DEFAULT_POLL_INTERVAL
        pid = _spawn_watcher(workspace, poll)
        state = DaemonState(
            enabled=True,
            pid=pid,
            started_at=now_iso(),
            poll_interval=poll,
        )
        write_state(workspace, state)
        if not _wait_for_watcher_start(workspace, pid):
            state.enabled = False
            write_state(workspace, state)
            if is_pid_alive(pid):
                _terminate_pid(pid, timeout=1.0)
            raise LifecycleError(
                f"daemon 子进程启动失败或已退出（pid={pid}）",
                pid=pid,
            )
        log.info("daemon 已启动（pid=%s, poll=%.1fs）", pid, poll)
        return pid


def stop_daemon(workspace: Workspace) -> bool:
    """``lwa daemon off``：持久化 disabled 并终止 watcher（WBS-21.02）。

    返回是否成功终止（或本就未运行）。
    """
    state = read_state(workspace)
    if state is None:
        return True
    # 先把 enabled 置 False，watcher 下一轮也会自行退出
    state.enabled = False
    write_state(workspace, state)

    stopped = True
    if state.pid:
        stopped = _terminate_pid(state.pid)
    # 清理锁文件；状态文件保留为 disabled 供 status 查询
    with contextlib.suppress(FileNotFoundError, PermissionError):
        lock_path(workspace).unlink()
    log.info("daemon 已停止（pid=%s）", state.pid)
    return stopped


def daemon_status(workspace: Workspace) -> dict[str, Any]:
    """``lwa daemon status``：返回状态摘要（WBS-21.03）。"""
    state = read_state(workspace)
    running = is_running(workspace)
    return {
        "running": running,
        "enabled": bool(state and state.enabled),
        "pid": state.pid if state else None,
        "startedAt": state.started_at if state else None,
        "pollInterval": state.poll_interval if state else DEFAULT_POLL_INTERVAL,
    }


# ---- CLI 入口（``python -m local_webpage_access.daemon --workspace ...``）-------


def _main() -> int:
    """watcher 子进程入口：解析参数并运行 watcher 主循环。"""
    import argparse

    from local_webpage_access.logging import setup_logging

    parser = argparse.ArgumentParser(prog="lwa-daemon", description="lwa inbox watcher")
    parser.add_argument("--workspace", "-w", required=True, help="工作区根目录")
    parser.add_argument("--poll", type=float, default=DEFAULT_POLL_INTERVAL, help="轮询间隔（秒）")
    parser.add_argument("--log-level", default="INFO", help="日志级别")
    args = parser.parse_args()

    setup_logging(level=args.log_level.upper())  # type: ignore[arg-type]
    workspace = Workspace(Path(args.workspace).resolve())
    if not workspace.config_path.is_file():
        log.error("工作区未初始化：%s", workspace.root)
        return 2

    from local_webpage_access.config import load_config

    config = load_config(workspace)
    workspace.ensure_workspace_dirs()

    # 抢占单实例锁；已有 daemon 在跑则直接退出
    try:
        with daemon_lock(workspace):
            # 锁内启动，状态文件由 ``lwa daemon on`` 写好；watcher 读 enabled 决定运行
            reg = Registry(workspace.db_path)
            reg.open()
            try:
                # DEV-042：启动即自愈一次——恢复上次退出/重启期间掉线的
                # desired=running 实例（builtin 静态进程 + 容器）。
                try:
                    recovered = reconcile(workspace, config, reg)
                    if recovered:
                        log.info("daemon 启动自愈：恢复 %d 个实例 %s", len(recovered), recovered)
                except Exception:  # noqa: BLE001 — 自愈失败不阻断 watcher
                    log.exception("daemon 启动 reconcile 失败")
                run_watcher(
                    workspace,
                    config,
                    reg,
                    poll_interval=args.poll,
                    heartbeat=lambda: touch_lock_heartbeat(workspace),
                    supervise=reconcile,
                )
            finally:
                reg.close()
    except OSError:
        log.warning("已有 daemon 实例在运行，退出")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "DaemonState",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_STABLE_SECONDS",
    "DEFAULT_SUPERVISE_INTERVAL",
    "read_state",
    "write_state",
    "clear_state",
    "state_path",
    "lock_path",
    "start_lock_path",
    "log_file_path",
    "is_pid_alive",
    "is_running",
    "daemon_lock",
    "daemon_start_lock",
    "touch_lock_heartbeat",
    "LOCK_HEARTBEAT_TIMEOUT",
    "scan_inbox",
    "is_file_stable",
    "load_processed_set",
    "save_processed_set",
    "processed_key",
    "process_zip",
    "reconcile",
    "run_watcher",
    "start_daemon",
    "stop_daemon",
    "daemon_status",
]
