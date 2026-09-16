"""管理页后台服务：默认随 ``lwa init`` 启动，可通过 ``managerEnabled: false`` 关闭。

* ``lwa manager on``  —— 后台启动 uvicorn 管理页；
* ``lwa manager off`` —— 停止后台管理页；
* ``lwa manager status`` —— 查询运行态；
* ``lwa manager start`` —— 前台启动（阻塞，见 :mod:`manager_api`）。

子进程入口：``python -m local_webpage_access.manager_service --workspace <root>``。
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
import urllib.error
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator

from local_webpage_access import file_lock
from local_webpage_access.config import Config
from local_webpage_access.daemon import is_pid_alive, pid_cmdline_contains
from local_webpage_access.gateway_service import maybe_start_gateway
from local_webpage_access.errors import (
    LifecycleError,
    ManagerLockFailureError,
    ManagerLockHeldError,
)
from local_webpage_access.logging import get_logger, now_iso
from local_webpage_access.paths import Workspace
from local_webpage_access.probe import urlopen_direct
from local_webpage_access.service_failures import (
    LastStartError,
    clear_start_failures,
    has_start_failures,
    parse_consecutive_failures,
    parse_last_start_error,
    record_start_failure,
)

log = get_logger("manager")

STATE_FILENAME = "manager.json"
START_LOCK_FILENAME = "manager-start.lock"
INSTANCE_LOCK_FILENAME = "manager.instance.lock"
LOG_FILENAME = "manager.log"
MANAGER_START_TIMEOUT = 15.0
# BUG-641：单实例锁被占用且确认不是健康重复实例时的退出码（监督器可见失败）。
MANAGER_LOCK_FAILURE_EXIT_CODE = 3
# BUG-645/646：锁交接的重试上限——旧版进程退出时会 unlink 锁文件，可能需要
# 丢弃当前（已无路径的）inode 重新竞争；正常 1～2 次内收敛。
_LOCK_TAKEOVER_ATTEMPTS = 5
# BUG-646：新协议锁记录第二行的标记。旧版（≤V0.8.13）只写单行 PID；新版
# 持内核锁并写 ``flock`` 标记，据此识别「疑似仍在临界区的旧版持有者」。
_LOCK_PROTOCOL_MARKER = "flock"
# BUG-651：遗留空锁记录的认领年龄阈值——与旧版自身的陈旧窗口（V0.8.13
# ``MANAGER_START_LOCK_STALE_SECONDS = 60``）同口径：从未写过内容的文件
# mtime 即创建时间，达到阈值仍为空视为创建者崩溃残留，可就地认领；未达
# 阈值拒入并保留空记录，后续调用随文件年龄增长自愈。
_EMPTY_LOCK_CLAIM_SECONDS = 60.0
# BUG-651：单实例锁遇遗留空记录时的观察预算（启动锁复用调用方 timeout）。
_EMPTY_LOCK_WATCH_SECONDS = 5.0


@dataclass
class ManagerState:
    """管理页后台运行态。

    IMP-064：``enabled`` 仅表用户意图；启动失败与进程退出只写
    ``last_start_error`` / ``consecutive_start_failures`` 观测字段，
    绝不把 ``enabled`` 改回 False（§16.2 写入契约）。
    """

    enabled: bool = False
    pid: int | None = None
    started_at: str | None = None
    host: str = "0.0.0.0"
    port: int = 17800
    last_start_error: LastStartError | None = None
    consecutive_start_failures: int = 0
    bind_version: str | None = None
    bind_revision: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def state_path(workspace: Workspace) -> Path:
    return workspace.run / STATE_FILENAME


def start_lock_path(workspace: Workspace) -> Path:
    return workspace.run / START_LOCK_FILENAME


def instance_lock_path(workspace: Workspace) -> Path:
    """管理页单实例锁路径（BUG-193）。"""
    return workspace.run / INSTANCE_LOCK_FILENAME


def log_file_path(workspace: Workspace) -> Path:
    """管理页运行时日志路径（``logs/manager.log``）。"""
    return workspace.logs / LOG_FILENAME


def read_manager_log(workspace: Workspace, *, tail: int = 200) -> str:
    """读取管理页日志；``tail<=0`` 返回全文，文件不存在返回空串。"""
    from local_webpage_access.logs import tail_text_file

    return tail_text_file(log_file_path(workspace), 0 if tail is None else tail)


def read_state(workspace: Workspace) -> ManagerState | None:
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
        return ManagerState(
            enabled=bool(data.get("enabled", False)),
            pid=int(data["pid"]) if data.get("pid") is not None else None,
            started_at=data.get("started_at"),
            host=str(data.get("host", "0.0.0.0")),
            port=int(data.get("port", 17800)),
            # IMP-064.01：旧文件缺字段读默认值（None/0），不做 schema 迁移
            last_start_error=parse_last_start_error(data),
            consecutive_start_failures=parse_consecutive_failures(data),
            bind_version=str(data["bind_version"]) if data.get("bind_version") else None,
            bind_revision=str(data["bind_revision"]) if data.get("bind_revision") else None,
        )
    except (TypeError, ValueError):
        return None


def _manager_bind_version() -> str:
    from local_webpage_access.version_info import bind_process_version

    return bind_process_version()


def _manager_bind_revision() -> str | None:
    from local_webpage_access.version_info import bind_process_revision

    return bind_process_revision()


def write_state(workspace: Workspace, state: ManagerState) -> None:
    from local_webpage_access.version_info import fill_missing_bind_version

    path = state_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = fill_missing_bind_version(state, path)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _health_check_host(bind_host: str) -> str:
    """把监听地址映射为可探测的客户端主机。

    - IPv4 通配 ``0.0.0.0`` / 空串 → ``127.0.0.1``
    - IPv6 通配 ``::`` → ``::1``（同族回环；勿改写成 IPv4）
    - 具体地址（含 ``::1``、LAN IPv6）原样保留
    """
    if bind_host in {"0.0.0.0", ""}:
        return "127.0.0.1"
    if bind_host == "::":
        return "::1"
    return bind_host


def _fetch_health(host: str, port: int, *, timeout: float = 1.0) -> dict[str, Any] | None:
    """``GET /api/health`` 解析 JSON；失败或非 200 时返回 ``None``。"""
    from local_webpage_access.ports import format_http_host

    probe = format_http_host(_health_check_host(host))
    url = f"http://{probe}:{port}/api/health"
    try:
        with urlopen_direct(url, timeout=timeout) as resp:  # noqa: S310
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, dict) else None
    except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError):
        return None


def health_ok(host: str, port: int, *, timeout: float = 1.0) -> bool:
    """``GET /api/health`` 是否返回 200 且 ``ok`` 为真。"""
    data = _fetch_health(host, port, timeout=timeout)
    return bool(data and data.get("ok"))


def health_matches_workspace(
    host: str,
    port: int,
    workspace_root: Path,
    *,
    timeout: float = 1.0,
    state: ManagerState | None = None,
) -> bool:
    """端口上的管理页是否属于指定工作区（依赖 ``/api/health`` 的 ``workspaceRoot``）。

    若 health 未带 ``workspaceRoot``（BUG-053 之前的管理页），在 ``state`` 表明
    本工作区已启用且记录的 ``pid`` 仍存活时，视为本工作区进程（BUG-065），以便
    ``lwa update`` 能重启旧版管理页。foreign 占用场景勿传可证明归属的 ``state``，
    仍会由 ``start_manager`` 的 ``health_ok`` 分支拒绝。
    """
    data = _fetch_health(host, port, timeout=timeout)
    if not data or not data.get("ok"):
        return False
    remote = data.get("workspaceRoot")
    if remote:
        try:
            return Path(str(remote)).resolve() == Path(workspace_root).resolve()
        except (OSError, ValueError):
            return False
    # BUG-065：旧版 health 无 workspaceRoot —— 仅当 state 证明 pid 仍存活
    if state is not None and state.enabled and state.pid is not None and is_pid_alive(state.pid):
        return True
    return False


def is_running(workspace: Workspace, config: Config) -> bool:
    """管理页后台进程是否在运行且健康。"""
    state = read_state(workspace)
    if state is None or not state.enabled:
        return False
    port = state.port or config.managerPort
    host = state.host or config.managerHost
    if not health_matches_workspace(host, port, workspace.root, state=state):
        return False
    if state.pid is None:
        return True
    return is_pid_alive(state.pid)


def _spawn_manager(workspace: Workspace) -> int:
    """以独立子进程启动管理页，stdout/stderr 追加到 ``logs/manager.log``。"""
    from local_webpage_access.logs import open_append

    root = str(workspace.root)
    cmd = [
        sys.executable,
        "-m",
        "local_webpage_access.manager_service",
        "--workspace",
        root,
    ]
    log_path = log_file_path(workspace)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # BUG-186：打开前按大小滚动
    log_fh = open_append(log_path)
    from local_webpage_access.logging import secure_chmod

    secure_chmod(log_path)
    popen_kwargs: dict[str, Any] = {
        "stdout": log_fh,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
    }
    # 036.09：正式支持不含 Windows 原生；DETACHED_PROCESS 启动路径已删除。
    if sys.platform == "win32":
        raise RuntimeError(
            "Windows 原生不受支持；无法启动 LWA manager，"
            "请在 WSL2 的 Ubuntu/Debian 中运行（IMP-036 / 036.09）"
        )
    popen_kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)  # noqa: S603
    finally:
        # 子进程已继承句柄，父进程关闭自己的副本，避免泄漏。
        log_fh.close()
    return int(proc.pid)


def find_listening_pid(port: int) -> int | None:
    """用 lsof 查找 TCP 监听进程 PID；不可用时返回 ``None``（BUG-126）。"""
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid > 0:
            return pid
    return None


def _manager_pid_matches(pid: int, workspace: Workspace) -> bool:
    return pid_cmdline_contains(
        pid,
        "local_webpage_access.manager_service",
        str(workspace.root),
    )


def _terminate_pid(
    pid: int,
    *,
    timeout: float = 5.0,
    workspace: Workspace | None = None,
) -> bool:
    if not is_pid_alive(pid):
        return True
    # BUG-125：仅凭 PID 发送信号可能误杀复用该 PID 的无关进程。
    if workspace is not None and not _manager_pid_matches(pid, workspace):
        log.warning("管理页 PID %s 身份不匹配，拒绝终止", pid)
        return True
    try:
        # 正式平台均为 POSIX；win32 分支仅保留可移植工具语义（036.09）。
        os.kill(pid, 15 if sys.platform != "win32" else 9)
    except OSError:
        return not is_pid_alive(pid)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_pid_alive(pid):
            return True
        time.sleep(0.05)
    with contextlib.suppress(OSError):
        os.kill(pid, 9)
    return not is_pid_alive(pid)


def _read_lock_payload(fd: int) -> tuple[int | None, bool]:
    """从 fd 读取锁记录（BUG-645）：返回 (持有者 PID, 是否新协议记录)。

    必须读 fd 而不是路径——旧版退出会 unlink 路径，他人重建后路径中的 PID
    与本 fd 无关；「flock 成功」不能证明路径里的 PID 属于旧版。新协议第二行
    为 :data:`_LOCK_PROTOCOL_MARKER`；旧版（≤V0.8.13）只写单行 PID，首行
    仍兼容旧版解析。
    """
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        data = os.read(fd, 128)
        os.lseek(fd, 0, os.SEEK_SET)
        lines = data.decode("utf-8", "replace").strip().splitlines()
        pid = int(lines[0]) if lines else None
        marked = len(lines) > 1 and lines[1].strip() == _LOCK_PROTOCOL_MARKER
        return pid, marked
    except (OSError, ValueError):
        return None, False


def _write_lock_payload(fd: int) -> None:
    """以新协议写入自身 PID（PID 行 + ``flock`` 标记行，首行兼容旧版解析）。"""
    file_lock.write_lock_payload(
        fd, f"{os.getpid()}\n{_LOCK_PROTOCOL_MARKER}\n".encode()
    )


def _open_lock_file(path: Path) -> tuple[int, bool]:
    """打开（必要时新建）锁文件，返回 (fd, 是否由本次调用新建)。

    BUG-651：先按既有文件打开，不存在才 O_EXCL 新建——调用方据此区分
    「自己新建的文件（可立即写入身份）」与「他人遗留的空记录（可能是旧版
    「已建文件、未写 PID」的创建窗口，提前写入会让旧版随后覆写记录并在
    退出时 unlink 新版持锁路径）」。权限/I/O 故障转为锁失败异常。
    """
    failure = "管理页锁文件无法打开（{path}）：{exc}"
    for _ in range(3):
        try:
            return os.open(str(path), os.O_RDWR), False
        except FileNotFoundError:
            try:
                return os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_RDWR), True
            except FileExistsError:
                # 与旧版 O_EXCL 创建竞争失败：按既有文件重开
                continue
            except OSError as exc:
                raise ManagerLockFailureError(
                    failure.format(path=path, exc=exc),
                    lock_path=str(path),
                ) from exc
        except OSError as exc:
            raise ManagerLockFailureError(
                failure.format(path=path, exc=exc),
                lock_path=str(path),
            ) from exc
    raise ManagerLockFailureError(
        f"管理页锁文件打开竞争失败（{path}），请重试",
        lock_path=str(path),
    )


def _lock_path_matches_fd(path: Path, fd: int) -> bool:
    """锁文件路径当前是否仍指向我们持有内核锁的 inode。

    旧版（≤V0.8.13）进程退出时会 unlink 锁文件；若路径已被删除或指向新
    inode，当前 fd 上的锁无法拦住后来者，必须换新 inode 重新竞争。
    """
    try:
        return path.stat().st_ino == os.fstat(fd).st_ino
    except OSError:
        return False


def _await_empty_lock_record(path: Path, fd: int, *, deadline: float) -> str:
    """BUG-651：限时观察遗留空锁记录的归宿。

    返回四种结局：``record``（出现 PID——旧版创建者恢复，交由旧协议判定）、
    ``replaced``（路径被回收或重建——旧版临界区已结束 / 他人重建，换文件
    重试）、``claim``（超时仍空且 inode 年龄达认领阈值——创建者崩溃残留，
    就地认领，死亡进程不会再 unlink）、``refuse``（超时仍空且未达阈值——
    拒入并保留空记录，后续调用随文件年龄增长自愈）。

    空记录只可能来自旧版「O_EXCL 建文件后、写 PID 前」的窗口（新版新建
    文件会立即写入身份）。旧版不持内核锁，flock 拦不住它：观察期内**绝不
    写入**——提前写入会让旧版随后覆写记录、退出时再 unlink 新版持锁路径，
    造成两版并存与互斥失效。因此本函数返回前不产生任何写动作，inode 的
    mtime（未写过内容时即创建时间）保持可信。
    """
    while time.monotonic() < deadline:
        if not _lock_path_matches_fd(path, fd):
            return "replaced"
        holder, _marked = _read_lock_payload(fd)
        if holder is not None:
            return "record"
        time.sleep(0.05)
    age = time.time() - os.fstat(fd).st_mtime
    return "claim" if age >= _EMPTY_LOCK_CLAIM_SECONDS else "refuse"


@contextlib.contextmanager
def manager_start_lock(workspace: Workspace, *, timeout: float = 5.0) -> Iterator[None]:
    """串行化 ``manager on``（BUG-130 / BUG-642 / BUG-646）。

    BUG-642：改用 :mod:`file_lock` 内核排他锁——不再依赖 O_EXCL 创建 + PID/
    文件年龄判定，消除「A 创建空文件未写 PID、B 判陈旧删除重建」的空窗竞争，
    也取消「持有者仍存活但文件超过 60 秒即被抢占」的按年龄强删。锁文件创建后
    长期保留（永不 unlink）；记录采用新协议（PID + ``flock`` 标记）。

    BUG-646 / BUG-648（旧版交接协议）：旧版启动流程不持内核锁，只写单行 PID。
    新版取到内核锁后若发现**旧协议记录且持有者仍存活**，判定旧版启动临界区
    可能未结束：在剩余 ``timeout`` 内等待其退出后重新竞争；超时则**保留原旧
    协议记录**并报告等待失败——绝不在旧临界区仍活跃时写入 flock 标记（否则
    下次调用跳过旧版检查并与旧版并存；旧版随后 unlink 还会破坏新版互斥）。
    绝不终止任意旧 CLI PID。

    BUG-650：旧版临界区的结束以**锁路径被旧版 finally unlink** 为准，不是其
    进程退出——旧 CLI 释放锁后仍会存活（等待健康检查、打印输出等），只盯
    PID 存活会把已释放的锁误报为占用直到超时。等待期间同时监测 PID 存活与
    路径 inode 指向，任一表明临界区结束即换文件重试。

    BUG-651：遗留**空记录**可能是旧版「已建文件、未写 PID」的创建窗口——
    旧版不持内核锁，flock 拦不住它，此时绝不提前写入新协议记录（旧版恢复
    后会覆写记录、退出时还会 unlink 新版持锁路径，两版并存且互斥失效）。
    新版新建文件**立即写入身份**（旧版 EEXIST 读到存活 PID 自行让路）；遇
    遗留空记录先经 :func:`_await_empty_lock_record` 限时观察其归宿，超时
    仍空则按 inode 年龄（≥60s，与旧版陈旧阈值同口径）判定崩溃残留后就地
    认领，未达阈值拒入并保留原记录。

    BUG-649：另一新版已持内核锁时，在 ``deadline`` 前重试获取，仅超时后报告
    占用——恢复并发 ``lwa manager on`` 的限时串行等待与幂等成功路径。
    """
    path = start_lock_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd: int | None = None
    deadline = time.monotonic() + timeout
    attempts = _LOCK_TAKEOVER_ATTEMPTS
    replaced_msg = (
        f"管理页启动锁 {path} 在交接期间被反复重建，无法稳定持有；"
        "请检查是否仍有旧版本进程在运行"
    )

    def recycle(current_fd: int) -> None:
        """丢弃已被替换的 inode（关 fd、扣减重试配额），耗尽则报错。"""
        nonlocal attempts
        with contextlib.suppress(OSError):
            os.close(current_fd)
        attempts -= 1
        if attempts <= 0:
            raise ManagerLockFailureError(replaced_msg, lock_path=str(path))

    try:
        while True:
            fd, created = _open_lock_file(path)
            if created:
                # BUG-651：新建即写身份——旧版 EEXIST 读到存活 PID 按占用让路；
                # 本进程崩溃也不会留下永空记录。文件仅本进程经 O_EXCL 取得，
                # 写入先于内核锁是安全的（他人只读不写，见 _await_empty_lock_record）。
                _write_lock_payload(fd)
            else:
                holder, marked = _read_lock_payload(fd)
                if holder is None:
                    # BUG-651：遗留空记录 → 限时观察，绝不提前写入
                    outcome = _await_empty_lock_record(path, fd, deadline=deadline)
                    if outcome == "replaced":
                        recycle(fd)
                        fd = None
                        continue
                    if outcome == "refuse":
                        with contextlib.suppress(OSError):
                            os.close(fd)
                        fd = None
                        raise ManagerLockHeldError(
                            "管理页启动锁记录为空，疑似旧版本启动流程正在创建"
                            "（尚未写入 PID）；已等待超时，稍后重试即可",
                            lock_path=str(path),
                            legacy=True,
                        )
                    if outcome == "record":
                        holder, marked = _read_lock_payload(fd)
                if holder is not None and holder > 0 and is_pid_alive(holder) and not marked:
                    # BUG-646/648/650：旧协议 + 存活持有者 → 等待旧临界区结束：
                    # 持有者进程退出，或锁路径被其 finally unlink。
                    while time.monotonic() < deadline:
                        if not _lock_path_matches_fd(path, fd):
                            break  # BUG-650：路径已回收/重建 → 旧临界区已结束
                        if not is_pid_alive(holder):
                            break
                        time.sleep(0.05)
                    if is_pid_alive(holder) and _lock_path_matches_fd(path, fd):
                        with contextlib.suppress(OSError):
                            os.close(fd)
                        fd = None
                        raise ManagerLockHeldError(
                            f"管理页启动锁疑似被旧版本启动流程持有（pid={holder}），"
                            "已等待超时；旧版流程退出后重试即可",
                            lock_path=str(path),
                            holder_pid=holder,
                            legacy=True,
                        )
                    recycle(fd)
                    fd = None
                    continue

            # 认领点：内核锁在记录判定后获取
            try:
                file_lock.ensure_lockable(fd)
                file_lock.try_acquire_exclusive(fd)
            except BlockingIOError as exc:
                # BUG-649：内核锁被占时限时重试，勿首次冲突即失败
                if time.monotonic() >= deadline:
                    holder, _marked = _read_lock_payload(fd)
                    raise ManagerLockHeldError(
                        "管理页启动锁被占用，稍后重试",
                        lock_path=str(path),
                        holder_pid=holder,
                    ) from exc
                with contextlib.suppress(OSError):
                    os.close(fd)
                fd = None
                time.sleep(0.05)
                continue
            except OSError as exc:
                raise ManagerLockFailureError(
                    f"管理页启动锁加锁失败（{path}）：{exc}",
                    lock_path=str(path),
                ) from exc

            # BUG-645 同源：认领后核验路径仍指向本 fd，再发布记录——路径可能
            # 已被旧版 unlink 或指向他人新持有的新 inode。
            if not _lock_path_matches_fd(path, fd):
                file_lock.release_exclusive(fd)
                recycle(fd)
                fd = None
                continue
            _write_lock_payload(fd)
            if not _lock_path_matches_fd(path, fd):
                # 写入后路径被旧版退出 unlink：换新 inode 重试。
                file_lock.release_exclusive(fd)
                recycle(fd)
                fd = None
                continue
            break
        yield
    finally:
        if fd is not None:
            file_lock.release_exclusive(fd)
            with contextlib.suppress(OSError):
                os.close(fd)


@contextlib.contextmanager
def manager_instance_lock(workspace: Workspace) -> Iterator[None]:
    """管理页单实例锁（BUG-193/641/642/645）：run_service_main 整个生命周期持有。

    与 :func:`manager_start_lock`（仅串行化 ``lwa manager on``、start 后即释放）
    不同，本锁由管理页子进程入口持有到退出，保证同一工作区只有一个 manager
    uvicorn 进程——避免两个 manager 并发启动互踩 manager.json（后写者覆盖先写者
    pid），导致 ``off`` 假报已停止而另一实例仍在端口上运行。

    互斥由 :mod:`file_lock` 内核排他锁保证，整个持有期间保持 fd 打开，退出只
    解锁并关闭，**永不 unlink**。内核锁在持有进程死亡时自动释放，因此死 PID、
    被无关进程复用的 PID 都不会再长期拒启；空记录限时观察后按 inode 年龄自愈
    认领（BUG-651，不再无条件直接接管）。记录从**本 fd** 读取且读前先核验路径
    仍指向本 fd（BUG-645：路径可能被旧版 unlink 或指向他人新持有的新 inode，
    绝不基于路径内容做终止决策）。

    旧版交接（BUG-645）：锁层**不终止任何进程**。若记录为旧协议（无标记）且
    PID 存活、身份核验为本工作区 manager——必为不持内核锁的旧版进程——抛
    :class:`ManagerLockHeldError`（``legacy=True``）拒绝进入：旧版停止交由
    升级/监督器协调层（``lwa update`` 协调重启 / ``lwa manager off``）执行，
    入口再按健康分类退出码（健康重复实例 exit 0，否则 exit 3 并给出指引）。
    PID 指向无关进程时仅覆盖记录，绝不发送信号。

    BUG-651：遗留空记录可能是旧版 manager「已建文件、未写 PID」的启动窗口
    （旧版单实例锁同样是 O_EXCL 创建后写单行 PID）。与 :func:`manager_start_lock`
    同口径：新建即写身份；遇遗留空记录经 :func:`_await_empty_lock_record`
    限时观察，超时仍空按 inode 年龄认领或拒入，绝不提前写入。
    """
    path = instance_lock_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd: int | None = None
    attempts = _LOCK_TAKEOVER_ATTEMPTS
    replaced_msg = (
        f"管理页单实例锁 {path} 在交接期间被反复重建，无法稳定持有；"
        "请检查是否仍有旧版本进程在运行"
    )

    def recycle(current_fd: int) -> None:
        """丢弃已被替换的 inode（关 fd、扣减重试配额），耗尽则报错。"""
        nonlocal attempts
        with contextlib.suppress(OSError):
            os.close(current_fd)
        attempts -= 1
        if attempts <= 0:
            raise ManagerLockFailureError(replaced_msg, lock_path=str(path))

    try:
        while True:
            fd, created = _open_lock_file(path)
            if created:
                # BUG-651：新建即写身份（同 manager_start_lock）。
                _write_lock_payload(fd)
            else:
                holder, marked = _read_lock_payload(fd)
                if holder is None:
                    # BUG-651：遗留空记录 → 限时观察，绝不提前写入
                    outcome = _await_empty_lock_record(
                        path, fd, deadline=time.monotonic() + _EMPTY_LOCK_WATCH_SECONDS
                    )
                    if outcome == "replaced":
                        recycle(fd)
                        fd = None
                        continue
                    if outcome == "refuse":
                        with contextlib.suppress(OSError):
                            os.close(fd)
                        fd = None
                        raise ManagerLockHeldError(
                            "管理页单实例锁记录为空，疑似旧版本管理页正在启动"
                            "（尚未写入 PID）；请重试 `lwa update` 由监督器协调"
                            "重启，或 `lwa manager off` 停止后再启动",
                            lock_path=str(path),
                            legacy=True,
                        )
                    if outcome == "record":
                        holder, marked = _read_lock_payload(fd)
                if (
                    holder is not None
                    and holder > 0
                    and holder != os.getpid()
                    and is_pid_alive(holder)
                    and not marked
                ):
                    if _manager_pid_matches(holder, workspace):
                        # 旧协议 + 存活 + 身份匹配 → 旧版 manager 持旧式锁。
                        # 锁层拒绝进入（不终止，BUG-645），交由入口健康分类
                        # 与协调层停止。
                        with contextlib.suppress(OSError):
                            os.close(fd)
                        fd = None
                        raise ManagerLockHeldError(
                            f"疑似旧版本管理页进程（pid={holder}）仍持有旧式单实例锁；"
                            "请重试 `lwa update` 由监督器协调重启，"
                            "或 `lwa manager off` 停止后再启动",
                            lock_path=str(path),
                            holder_pid=holder,
                            legacy=True,
                        )
                    log.info(
                        "锁文件记录的 pid=%s 不是本工作区管理页（PID 可能被复用），仅覆盖记录",
                        holder,
                    )

            # 认领点：内核锁在记录判定后获取（被占即拒——实例锁不重试等待）
            try:
                file_lock.ensure_lockable(fd)
                file_lock.try_acquire_exclusive(fd)
            except BlockingIOError as exc:
                holder, _marked = _read_lock_payload(fd)
                with contextlib.suppress(OSError):
                    os.close(fd)
                fd = None
                raise ManagerLockHeldError(
                    "管理页已有实例在运行",
                    lock_path=str(path),
                    holder_pid=holder,
                ) from exc
            except OSError as exc:
                raise ManagerLockFailureError(
                    f"管理页单实例锁加锁失败（{path}）：{exc}",
                    lock_path=str(path),
                ) from exc

            # BUG-645：认领后核验路径仍指向本 fd，再发布记录
            if not _lock_path_matches_fd(path, fd):
                file_lock.release_exclusive(fd)
                recycle(fd)
                fd = None
                continue
            _write_lock_payload(fd)
            if not _lock_path_matches_fd(path, fd):
                # 写入后路径被旧版退出 unlink：换新 inode 重试。
                file_lock.release_exclusive(fd)
                recycle(fd)
                fd = None
                continue
            break
        yield
    finally:
        if fd is not None:
            file_lock.release_exclusive(fd)
            with contextlib.suppress(OSError):
                os.close(fd)


def _wait_for_health(config: Config, *, timeout: float = MANAGER_START_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if health_ok(config.managerHost, config.managerPort, timeout=0.5):
            return True
        time.sleep(0.1)
    return False


def start_manager(workspace: Workspace, config: Config, *, source: str = "manual") -> int:
    """``lwa manager on`` / ``lwa init`` 自动启动：后台拉起管理页。

    ``source`` 标记启动来源（manual / update-restart / reconcile / autostart），
    仅写入失败观测 ``last_start_error.source``，不影响行为（IMP-064.02）。
    """
    if not config.managerEnabled:
        raise LifecycleError(
            "managerEnabled=false，管理页未启用；可在 local-web.yml 设为 true 后执行 lwa manager on",
        )

    # BUG-235：能力缓存改由子进程 run_service_main 以真实身份写入，父 CLI 不得冒充

    bind_host = config.managerHost
    bind_port = config.managerPort

    with manager_start_lock(workspace):
        state = read_state(workspace)
        if is_running(workspace, config):
            state = read_state(workspace)
            log.info("管理页已在运行（pid=%s），不重复启动", state.pid if state else "?")
            if state is not None:
                # IMP-064.06：已在运行早退也算启动成功，清零连续失败计数。
                if has_start_failures(state):
                    clear_start_failures(state)
                    write_state(workspace, state)
            return int(state.pid) if state and state.pid else 0

        # 端口已被占用：仅当健康端点确认属于本工作区时才恢复状态
        if health_matches_workspace(bind_host, bind_port, workspace.root, state=state):
            recovered_pid = find_listening_pid(bind_port)
            if recovered_pid is not None and not _manager_pid_matches(recovered_pid, workspace):
                recovered_pid = None
            state = ManagerState(
                enabled=True,
                pid=recovered_pid,
                started_at=now_iso(),
                host=bind_host,
                port=bind_port,
            )
            write_state(workspace, state)
            log.info("管理页端口 %s 已有本工作区健康响应，恢复状态记录", bind_port)
            return recovered_pid or 0

        # IMP-064.02：on 入口先断言意图（enabled=True）再执行启动——后续任何
        # 失败（端口冲突 / spawn / 健康超时）只写失败观测，意图保持开，
        # doctor 报 FAIL 而非「已按意图停用」。
        state = ManagerState(
            enabled=True,
            pid=state.pid if state else None,
            started_at=state.started_at if state else None,
            host=bind_host,
            port=bind_port,
            last_start_error=state.last_start_error if state else None,
            consecutive_start_failures=(
                state.consecutive_start_failures if state else 0
            ),
        )
        write_state(workspace, state)

        if health_ok(bind_host, bind_port):
            record_start_failure(
                state,
                f"管理页端口 {bind_port} 已被其他工作区占用，启动被拒绝",
                source=source,
            )
            write_state(workspace, state)
            raise LifecycleError(
                f"管理页端口 {bind_port} 已被其他工作区占用；"
                "请修改 local-web.yml 的 managerPort，或停止占用该端口的管理页",
            )

        try:
            pid = _spawn_manager(workspace)
        except Exception as exc:  # noqa: BLE001 — spawn 失败同样写失败观测
            state.pid = None
            record_start_failure(state, f"管理页子进程 spawn 失败：{exc}", source=source)
            write_state(workspace, state)
            raise
        state.pid = pid
        state.started_at = now_iso()
        write_state(workspace, state)
        if not _wait_for_health(config):
            # IMP-064.02：健康检查失败只写失败观测（lastStartError + 计数），
            # 杀残留进程、清 pid、抛原异常——绝不把 enabled 写回 False。
            state.pid = None
            record_start_failure(
                state,
                f"管理页子进程启动失败或健康检查超时（pid={pid}，port={bind_port}）",
                source=source,
            )
            write_state(workspace, state)
            if is_pid_alive(pid):
                _terminate_pid(pid, timeout=1.0, workspace=workspace)
            raise LifecycleError(
                f"管理页子进程启动失败或健康检查超时（pid={pid}，port={bind_port}）",
                pid=pid,
            )
        # IMP-064.02/064.06：启动成功清零失败计数。
        clear_start_failures(state)
        write_state(workspace, state)
        log.info("管理页已启动（pid=%s, port=%s）", pid, bind_port)
        # IMP-010 / DEV-041（WBS 0.8）：管理页成功启动后联动启动 Caddy 网关，
        # 使 :8080 别名入口随管理页一起就绪。maybe_start_gateway 已吞 LifecycleError
        # 并降级 builtin；此处仅兜底意外异常，绝不阻断管理页启动。
        # 注意 stop_manager 不联动停网关——业务入口优先，避免关管理页连带断别名。
        try:
            maybe_start_gateway(workspace, config)
        except LifecycleError:
            pass  # maybe_start_gateway 内部已记日志
        except Exception:  # noqa: BLE001 — 联动启网关不得拖垮管理页
            log.exception("联动启动 Caddy 网关时发生意外异常（已忽略）")
        return pid


def stop_manager(workspace: Workspace) -> bool:
    """``lwa manager off``：停止后台管理页（用户级——写 ``enabled=False``）。"""
    state = read_state(workspace)
    if state is None:
        return True
    pid = state.pid
    discovered = False
    if pid is None and state.enabled:
        pid = find_listening_pid(state.port)
        discovered = pid is not None

    stopped = True
    if pid:
        if is_pid_alive(pid) and not _manager_pid_matches(pid, workspace):
            if state.pid is not None:
                # 已记录 PID 身份不匹配，说明 PID 被复用；清理陈旧状态即可。
                log.warning("管理页 PID %s 身份不匹配，按陈旧状态清理", pid)
            else:
                # 端口监听者不是本工作区 manager，不能终止。
                pid = None
        else:
            stopped = _terminate_pid(pid, workspace=workspace)

    if (
        pid is None
        and state.enabled
        and health_matches_workspace(state.host, state.port, workspace.root, state=state)
    ):
        # BUG-126：健康端点仍属于本工作区但找不到可安全终止的 PID，不能假报成功。
        log.warning("管理页仍健康但未找到可安全终止的监听 PID（port=%s）", state.port)
        return False
    if stopped:
        state.enabled = False
        if discovered:
            state.pid = pid
        # IMP-064.06：用户级 off 重置失败观测（用户已明确接管该服务）。
        clear_start_failures(state)
        write_state(workspace, state)
        try:
            from local_webpage_access.capability import clear_capability_cache

            clear_capability_cache(workspace.root, "manager")
        except Exception:  # noqa: BLE001 — 清缓存失败不阻断停服
            pass
        log.info("管理页已停止（pid=%s）", pid or state.pid)
    else:
        log.warning("管理页停止失败，进程可能仍在运行（pid=%s）", state.pid)
    return stopped


def stop_manager_internal(workspace: Workspace) -> bool:
    """IMP-064.03：内部停止原语——终止进程但**不改用户意图**。

    供 ``updater.restart_manager`` 主序列与版本不一致二次停止使用：写盘只清
    ``pid``（保留 ``enabled`` 与失败观测），随后由 ``start_manager`` 的成败
    决定意图与失败记录。与用户级 :func:`stop_manager`（写 ``enabled=False``
    并重置失败记录）严格区分。
    """
    state = read_state(workspace)
    if state is None:
        return True
    pid = state.pid
    if pid is None and state.enabled:
        pid = find_listening_pid(state.port)

    stopped = True
    if pid:
        if is_pid_alive(pid) and not _manager_pid_matches(pid, workspace):
            if state.pid is not None:
                log.warning("管理页 PID %s 身份不匹配，按陈旧状态清理", pid)
            else:
                pid = None
        else:
            stopped = _terminate_pid(pid, workspace=workspace)

    if (
        pid is None
        and state.enabled
        and health_matches_workspace(state.host, state.port, workspace.root, state=state)
    ):
        log.warning("管理页仍健康但未找到可安全终止的监听 PID（port=%s）", state.port)
        return False
    if stopped:
        # 只清运行观测：pid=None，enabled 保持原值（064.03 核心契约）
        state.pid = None
        write_state(workspace, state)
        try:
            from local_webpage_access.capability import clear_capability_cache

            clear_capability_cache(workspace.root, "manager")
        except Exception:  # noqa: BLE001
            pass
        log.info("管理页已内部停止（意图保持 enabled=%s）", state.enabled)
    else:
        log.warning("管理页内部停止失败，进程可能仍在运行（pid=%s）", state.pid)
    return stopped


def foreign_manager_hint(workspace: Workspace, config: Config) -> str | None:
    """BUG-456：本工作区管理页未运行，但配置端口仍有健康响应时返回跨工作区提示。

    用于 ``lwa manager off`` 成功清理本工作区状态后，避免绿字「已停止」掩盖
    另一工作区仍占用同一 ``managerPort`` 的情况。
    """
    if is_running(workspace, config):
        return None
    if not health_ok(config.managerHost, config.managerPort):
        return None
    return (
        f"本工作区管理页未在运行，但端口 {config.managerPort} 仍有健康响应"
        f"（可能是其他工作区的管理页）。请到对应工作区执行 lwa manager off，"
        f"或修改 local-web.yml 的 managerPort"
    )


def existing_foreign_manager_hint(
    candidate_root: Path,
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
) -> str | None:
    """IMP-053：拟 ``lwa init`` 的目录若与本机已运行管理页不属于同一工作区，返回复用提示。

    家庭服务器默认一机一工作区；Agent 常另开 ``~/lwa-workspace`` 导致抢 17800、
    实例列表分裂。软提示不阻断——确需多工作区时可改端口后继续。
    """
    from local_webpage_access.config import MANAGER_PORT_DEFAULT

    bind_port = MANAGER_PORT_DEFAULT if port is None else port
    data = _fetch_health(host, bind_port, timeout=0.5)
    if not data or not data.get("ok"):
        return None
    remote = data.get("workspaceRoot")
    if not remote:
        return None
    try:
        remote_path = Path(str(remote)).resolve()
        candidate = Path(candidate_root).resolve()
    except (OSError, ValueError):
        return None
    if remote_path == candidate:
        return None
    return (
        f"本机管理页 :{bind_port} 已在运行，工作区为 {remote_path}。"
        f"通常只需一个工作区：请 cd 到该目录执行 lwa import / lwa start，"
        f"不要再 lwa init 新建第二套（默认会抢同一 managerPort）。"
        f"确需多工作区时，请为新区改 managerPort / staticGatewayPort / portPool。"
    )


def manager_status(workspace: Workspace, config: Config) -> dict[str, Any]:
    """``lwa manager status``：返回状态摘要。"""
    from local_webpage_access.service_failures import failure_note

    state = read_state(workspace)
    running = is_running(workspace, config)
    return {
        "running": running,
        "enabled": bool(state and state.enabled),
        "configured": config.managerEnabled,
        "pid": state.pid if state else None,
        "startedAt": state.started_at if state else None,
        "host": (state.host if state else None) or config.managerHost,
        "port": (state.port if state else None) or config.managerPort,
        # IMP-064.05：透出失败观测（无失败时为 None/0）
        "lastStartError": (
            state.last_start_error.to_dict() if state and state.last_start_error else None
        ),
        "consecutiveStartFailures": (
            state.consecutive_start_failures if state else 0
        ),
        "lastStartErrorNote": failure_note(state) if state else None,
    }


def maybe_start_manager(workspace: Workspace, config: Config) -> int | None:
    """``lwa init`` 调用：``managerEnabled`` 为 true 时后台启动，失败只记日志。"""
    if not config.managerEnabled:
        log.info("managerEnabled=false，跳过管理页自动启动")
        return None
    try:
        return start_manager(workspace, config)
    except LifecycleError as exc:
        log.warning("管理页自动启动失败：%s", exc)
        return None


def _lock_held_exit_code(workspace: Workspace, config: Config) -> int:
    """BUG-641：单实例锁被占用时的退出码分类。

    只有健康端点确认**本工作区**已有健康 manager（真实重复实例）才返回 0——
    退出 0 是为了不让监督器 KeepAlive 反复拉起第二个实例。否则返回
    :data:`MANAGER_LOCK_FAILURE_EXIT_CODE`，把「不健康持锁」暴露为启动失败
    而非静默离线；重试退避交由监督器既有策略，避免快速重试风暴。
    """
    state = read_state(workspace)
    if health_matches_workspace(
        config.managerHost, config.managerPort, workspace.root, state=state
    ):
        return 0
    return MANAGER_LOCK_FAILURE_EXIT_CODE


def run_service_main() -> int:
    """管理页子进程入口。"""
    import argparse

    from local_webpage_access.config import load_config
    from local_webpage_access.logging import setup_logging
    from local_webpage_access.manager_api import run_manager
    from local_webpage_access.registry import Registry

    parser = argparse.ArgumentParser(prog="lwa-manager", description="lwa manager service")
    parser.add_argument("--workspace", "-w", required=True, help="工作区根目录")
    parser.add_argument("--log-level", default="INFO", help="日志级别")
    args = parser.parse_args()

    # IMP-036：服务直入口平台门禁（防止绕过 CLI）
    from local_webpage_access.platform_support import require_supported_platform

    require_supported_platform()

    workspace = Workspace(Path(args.workspace).resolve())
    # BUG-116：写入 logs/lwa.log；uvicorn 等 stdout 由父进程重定向到 manager.log。
    setup_logging(
        level=args.log_level.upper(),
        log_dir=workspace.logs,
        log_filename="manager.log",
    )
    if not workspace.config_path.is_file():
        log.error("工作区未初始化：%s", workspace.root)
        return 2

    config = load_config(workspace)
    if not config.managerEnabled:
        log.error("managerEnabled=false，拒绝启动管理页子进程")
        return 2

    # BUG-235 / BUG-254：能力探测改到 lifespan 后台线程，避免阻塞 uvicorn 监听；
    # /api/health 只读启动缓存，不再同步跑 Docker/Caddy 探测。
    workspace.ensure_workspace_dirs()
    reg = Registry(workspace.db_path)
    reg.open()
    reg.close()

    # BUG-193：单实例锁——保证同一工作区只有一个 manager 进程。否则两个 manager
    # 并发启动会互踩 manager.json（后写者覆盖先写者 pid），导致 `off` 假报已停止
    # 而另一实例仍在端口上运行。BUG-641：锁被占用时只有确认已有本工作区健康
    # 实例才 exit 0（重复实例，避免监督器反复拉起第二实例）；不健康持锁、
    # 权限或 I/O 故障必须非零退出并给出排查信息，不得静默离线。
    try:
        with manager_instance_lock(workspace):
            # IMP-030/BUG-147：前台入口回写自身 pid 到 manager.json，使 manager_status /
            # `lwa manager off` 能识别前台监管进程（不再依赖 `lwa manager on` 事后补写）。
            write_state(
                workspace,
                ManagerState(
                    enabled=True,
                    pid=os.getpid(),
                    started_at=now_iso(),
                    host=config.managerHost,
                    port=config.managerPort,
                    bind_version=_manager_bind_version(),
                    bind_revision=_manager_bind_revision(),
                ),
            )
            try:
                run_manager(workspace, config)
            except Exception:
                log.exception("管理页子进程异常退出")
                return 1
            finally:
                # IMP-064.03b：子进程退出（SIGTERM / uvicorn 正常返回）只清运行
                # 观测（pid），**不得**写 enabled=False——监督器关机/重启后用户
                # 意图仍是开，update reconcile 与 doctor 据此恢复/告警。
                # pid 已被新进程覆盖时靠现有 pid 匹配跳过。
                st = read_state(workspace)
                if st is not None and st.pid == os.getpid():
                    st.pid = None
                    write_state(workspace, st)
                    try:
                        from local_webpage_access.capability import clear_capability_cache

                        clear_capability_cache(workspace.root, "manager")
                    except Exception:  # noqa: BLE001
                        pass
    except ManagerLockHeldError as exc:
        holder = exc.context.get("holder_pid")
        lock_path = exc.context.get("lock_path") or instance_lock_path(workspace)
        if _lock_held_exit_code(workspace, config) == 0:
            log.warning("已有本工作区健康管理页实例在运行（holder_pid=%s），退出", holder)
            return 0
        log.error(
            "管理页单实例锁被占用且未探得本工作区健康实例：%s（holder_pid=%s，"
            "锁文件=%s，运行日志=%s）",
            exc.message,
            holder,
            lock_path,
            log_file_path(workspace),
        )
        return MANAGER_LOCK_FAILURE_EXIT_CODE
    except ManagerLockFailureError as exc:
        log.error(
            "管理页单实例锁获取失败：%s；运行日志=%s",
            exc,
            log_file_path(workspace),
        )
        return MANAGER_LOCK_FAILURE_EXIT_CODE
    return 0


if __name__ == "__main__":
    raise SystemExit(run_service_main())


__all__ = [
    "ManagerState",
    "health_ok",
    "health_matches_workspace",
    "is_running",
    "log_file_path",
    "read_manager_log",
    "find_listening_pid",
    "start_manager",
    "stop_manager",
    "stop_manager_internal",
    "foreign_manager_hint",
    "existing_foreign_manager_hint",
    "manager_status",
    "maybe_start_manager",
    "run_service_main",
]
