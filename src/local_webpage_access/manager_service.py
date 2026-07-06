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
from local_webpage_access.daemon import is_pid_alive
from local_webpage_access.errors import LifecycleError
from local_webpage_access.logging import get_logger, now_iso
from local_webpage_access.paths import Workspace

log = get_logger("manager")

STATE_FILENAME = "manager.json"
START_LOCK_FILENAME = "manager-start.lock"
MANAGER_START_TIMEOUT = 15.0


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
) -> bool:
    """端口上的管理页是否属于指定工作区（依赖 ``/api/health`` 的 ``workspaceRoot``）。"""
    data = _fetch_health(host, port, timeout=timeout)
    if not data or not data.get("ok"):
        return False
    remote = data.get("workspaceRoot")
    if not remote:
        return False
    try:
        return Path(str(remote)).resolve() == Path(workspace_root).resolve()
    except (OSError, ValueError):
        return False


def is_running(workspace: Workspace, config: Config) -> bool:
    """管理页后台进程是否在运行且健康。"""
    state = read_state(workspace)
    if state is None or not state.enabled:
        return False
    port = state.port or config.managerPort
    host = state.host or config.managerHost
    if not health_matches_workspace(host, port, workspace.root):
        return False
    if state.pid is None:
        return True
    return is_pid_alive(state.pid)


def _spawn_manager(workspace: Workspace) -> int:
    root = str(workspace.root)
    cmd = [
        sys.executable,
        "-m",
        "local_webpage_access.manager_service",
        "--workspace",
        root,
    ]
    popen_kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **popen_kwargs)  # noqa: S603
    return int(proc.pid)


def _terminate_pid(pid: int, *, timeout: float = 5.0) -> bool:
    if not is_pid_alive(pid):
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
    """串行化 ``manager on``，避免并发 spawn。"""
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
        if is_running(workspace, config):
            state = read_state(workspace)
            log.info("管理页已在运行（pid=%s），不重复启动", state.pid if state else "?")
            return int(state.pid) if state and state.pid else 0

        # 端口已被占用：仅当健康端点确认属于本工作区时才恢复状态
        if health_matches_workspace(bind_host, bind_port, workspace.root):
            state = ManagerState(
                enabled=True,
                pid=None,
                started_at=now_iso(),
                host=bind_host,
                port=bind_port,
            )
            write_state(workspace, state)
            log.info("管理页端口 %s 已有本工作区健康响应，恢复状态记录", bind_port)
            return 0

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
                _terminate_pid(pid, timeout=1.0)
            raise LifecycleError(
                f"管理页子进程启动失败或健康检查超时（pid={pid}，port={bind_port}）",
                pid=pid,
            )
        log.info("管理页已启动（pid=%s, port=%s）", pid, bind_port)
        return pid


def stop_manager(workspace: Workspace) -> bool:
    """``lwa manager off``：停止后台管理页。"""
    state = read_state(workspace)
    if state is None:
        return True
    stopped = True
    if state.pid:
        stopped = _terminate_pid(state.pid)
    if stopped:
        state.enabled = False
        write_state(workspace, state)
        log.info("管理页已停止（pid=%s）", state.pid)
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

    setup_logging(level=args.log_level.upper())  # type: ignore[arg-type]
    workspace = Workspace(Path(args.workspace).resolve())
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

    try:
        run_manager(workspace, config)
    except Exception:
        log.exception("管理页子进程异常退出")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run_service_main())


__all__ = [
    "ManagerState",
    "health_ok",
    "health_matches_workspace",
    "is_running",
    "start_manager",
    "stop_manager",
    "manager_status",
    "maybe_start_manager",
    "run_service_main",
]
