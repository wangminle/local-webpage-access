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
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator

from local_webpage_access.config import Config
from local_webpage_access.daemon import is_pid_alive, pid_cmdline_contains
from local_webpage_access.gateway_service import maybe_start_gateway
from local_webpage_access.errors import LifecycleError
from local_webpage_access.logging import get_logger, now_iso
from local_webpage_access.paths import Workspace

log = get_logger("manager")

STATE_FILENAME = "manager.json"
START_LOCK_FILENAME = "manager-start.lock"
LOG_FILENAME = "manager.log"
MANAGER_START_TIMEOUT = 15.0
MANAGER_START_LOCK_STALE_SECONDS = 60.0


@dataclass
class ManagerState:
    """管理页后台运行态。"""

    enabled: bool = False
    pid: int | None = None
    started_at: str | None = None
    host: str = "0.0.0.0"
    port: int = 17800

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def state_path(workspace: Workspace) -> Path:
    return workspace.run / STATE_FILENAME


def start_lock_path(workspace: Workspace) -> Path:
    return workspace.run / START_LOCK_FILENAME


def log_file_path(workspace: Workspace) -> Path:
    """管理页运行时日志路径（``logs/manager.log``）。"""
    return workspace.logs / LOG_FILENAME


def read_manager_log(workspace: Workspace, *, tail: int = 200) -> str:
    """读取管理页日志；``tail<=0`` 返回全文，文件不存在返回空串。"""
    path = log_file_path(workspace)
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if tail is None or tail <= 0:
        return text
    lines = text.splitlines()
    return "\n".join(lines[-tail:])


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
        )
    except (TypeError, ValueError):
        return None


def write_state(workspace: Workspace, state: ManagerState) -> None:
    path = state_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _health_check_host(bind_host: str) -> str:
    if bind_host in {"0.0.0.0", "::", ""}:
        return "127.0.0.1"
    if bind_host.startswith("::"):
        return "127.0.0.1"
    return bind_host


def _fetch_health(host: str, port: int, *, timeout: float = 1.0) -> dict[str, Any] | None:
    """``GET /api/health`` 解析 JSON；失败或非 200 时返回 ``None``。"""
    url = f"http://{_health_check_host(host)}:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
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
    if (
        state is not None
        and state.enabled
        and state.pid is not None
        and is_pid_alive(state.pid)
    ):
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
    log_fh = log_path.open("a", encoding="utf-8")
    from local_webpage_access.logging import secure_chmod

    secure_chmod(log_path)
    popen_kwargs: dict[str, Any] = {
        "stdout": log_fh,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
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
        os.kill(pid, 15 if sys.platform != "win32" else 9)
    except OSError:
        return not is_pid_alive(pid)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_pid_alive(pid):
            return True
        time.sleep(0.05)
    if sys.platform == "win32":
        with contextlib.suppress(OSError):
            os.kill(pid, 9)
    else:
        with contextlib.suppress(OSError):
            os.kill(pid, 9)
    return not is_pid_alive(pid)


@contextlib.contextmanager
def manager_start_lock(workspace: Workspace, *, timeout: float = 5.0) -> Iterator[None]:
    """串行化 ``manager on``，并回收陈旧启动锁（BUG-130）。"""
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
            stale = False
            try:
                content = path.read_text(encoding="utf-8").strip().splitlines()
                holder_pid = int(content[0]) if content else 0
                stale = not is_pid_alive(holder_pid)
                if not stale:
                    stale = (
                        time.time() - path.stat().st_mtime
                        > MANAGER_START_LOCK_STALE_SECONDS
                    )
            except (OSError, ValueError):
                stale = True
            if stale:
                with contextlib.suppress(FileNotFoundError, PermissionError):
                    path.unlink()
                continue
            if time.monotonic() >= deadline:
                raise LifecycleError("管理页启动锁被占用，稍后重试")
            time.sleep(0.05)
    try:
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(FileNotFoundError, PermissionError):
            path.unlink()


def _wait_for_health(config: Config, *, timeout: float = MANAGER_START_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if health_ok(config.managerHost, config.managerPort, timeout=0.5):
            return True
        time.sleep(0.1)
    return False


def start_manager(workspace: Workspace, config: Config) -> int:
    """``lwa manager on`` / ``lwa init`` 自动启动：后台拉起管理页。"""
    if not config.managerEnabled:
        raise LifecycleError(
            "managerEnabled=false，管理页未启用；可在 local-web.yml 设为 true 后执行 lwa manager on",
        )

    bind_host = config.managerHost
    bind_port = config.managerPort

    with manager_start_lock(workspace):
        state = read_state(workspace)
        if is_running(workspace, config):
            state = read_state(workspace)
            log.info("管理页已在运行（pid=%s），不重复启动", state.pid if state else "?")
            return int(state.pid) if state and state.pid else 0

        # 端口已被占用：仅当健康端点确认属于本工作区时才恢复状态
        if health_matches_workspace(bind_host, bind_port, workspace.root, state=state):
            recovered_pid = find_listening_pid(bind_port)
            if recovered_pid is not None and not _manager_pid_matches(
                recovered_pid, workspace
            ):
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

        if health_ok(bind_host, bind_port):
            raise LifecycleError(
                f"管理页端口 {bind_port} 已被其他工作区占用；"
                "请修改 local-web.yml 的 managerPort，或停止占用该端口的管理页",
            )

        pid = _spawn_manager(workspace)
        state = ManagerState(
            enabled=True,
            pid=pid,
            started_at=now_iso(),
            host=bind_host,
            port=bind_port,
        )
        write_state(workspace, state)
        if not _wait_for_health(config):
            state.enabled = False
            write_state(workspace, state)
            if is_pid_alive(pid):
                _terminate_pid(pid, timeout=1.0, workspace=workspace)
            raise LifecycleError(
                f"管理页子进程启动失败或健康检查超时（pid={pid}，port={bind_port}）",
                pid=pid,
            )
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
    """``lwa manager off``：停止后台管理页。"""
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

    if pid is None and state.enabled and health_matches_workspace(
        state.host, state.port, workspace.root, state=state
    ):
        # BUG-126：健康端点仍属于本工作区但找不到可确认身份的 PID，不能假报成功。
        log.warning("管理页仍健康但未找到可安全终止的监听 PID（port=%s）", state.port)
        return False
    if stopped:
        state.enabled = False
        if discovered:
            state.pid = pid
        write_state(workspace, state)
        log.info("管理页已停止（pid=%s）", pid or state.pid)
    else:
        log.warning("管理页停止失败，进程可能仍在运行（pid=%s）", state.pid)
    return stopped


def manager_status(workspace: Workspace, config: Config) -> dict[str, Any]:
    """``lwa manager status``：返回状态摘要。"""
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

    workspace = Workspace(Path(args.workspace).resolve())
    # BUG-116：写入 logs/lwa.log；uvicorn 等 stdout 由父进程重定向到 manager.log。
    setup_logging(level=args.log_level.upper(), log_dir=workspace.logs)  # type: ignore[arg-type]
    if not workspace.config_path.is_file():
        log.error("工作区未初始化：%s", workspace.root)
        return 2

    config = load_config(workspace)
    if not config.managerEnabled:
        log.error("managerEnabled=false，拒绝启动管理页子进程")
        return 2

    workspace.ensure_workspace_dirs()
    reg = Registry(workspace.db_path)
    reg.open()
    reg.close()

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
        ),
    )
    try:
        run_manager(workspace, config)
    except Exception:
        log.exception("管理页子进程异常退出")
        return 1
    finally:
        # 进程退出（含 uvicorn 收到 SIGTERM 正常返回）后标记未运行，避免状态残留。
        st = read_state(workspace)
        if st is not None and st.pid == os.getpid():
            write_state(
                workspace,
                ManagerState(
                    enabled=False,
                    pid=st.pid,
                    started_at=st.started_at,
                    host=st.host,
                    port=st.port,
                ),
            )
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
    "manager_status",
    "maybe_start_manager",
    "run_service_main",
]
