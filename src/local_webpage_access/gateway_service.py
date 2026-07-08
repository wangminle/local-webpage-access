"""Caddy 网关后台服务（IMP-010 / DEV-041）。

镜像 :mod:`manager_service` 的服务层模式，但 Caddy 通过原生
``caddy start --pidfile`` 自守护，故本模块不 spawn 子进程，而是委托
:class:`~local_webpage_access.static_gateway.StaticGateway` 的 ``caddy_start`` /
``caddy_stop`` 管理 master 生命周期，并用 ``run/gateway.json`` 记录服务态
（与 :mod:`manager_service` 的 ``manager.json`` 对称）。

* ``lwa gateway on``     —— 启动 Caddy master 并写服务态；
* ``lwa gateway off``    —— 停止 Caddy master；
* ``lwa gateway status`` —— 查询运行态。

仅在 ``staticGateway=caddy`` 且 Caddy 可用时有效；其他后端是空操作或报错。
:func:`maybe_start_gateway` 在 ``lwa init`` / ``lwa manager on`` 联动调用，
失败只记日志不阻断（可降级 builtin 静态服务）。

"运行中" 的判定以 Caddy admin API（``127.0.0.1:2019``）是否在线为准——这是
master 真实存活的可信信号；``run/caddy.pid`` 仅作 pid 记录，可能因崩溃残留
（由 :meth:`StaticGateway._clear_stale_caddy_pid` 清理，BUG-070）。
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator

from local_webpage_access.config import Config
from local_webpage_access.errors import LifecycleError
from local_webpage_access.logging import get_logger, now_iso
from local_webpage_access.paths import Workspace
from local_webpage_access.static_gateway import StaticGateway
from local_webpage_access.version_requirements import MIN_CADDY_VERSION

log = get_logger("gateway")

STATE_FILENAME = "gateway.json"
START_LOCK_FILENAME = "gateway-start.lock"
GATEWAY_START_LOCK_TIMEOUT = 5.0

# Caddy admin API 固定监听 IPv4 loopback（reload/stop 走它，BUG-068 显式 127.0.0.1）。
ADMIN_PORT = 2019
ENTRY_PORT_DEFAULT = 8080


@dataclass
class GatewayState:
    """Caddy 网关后台服务态。"""

    enabled: bool = False
    pid: int | None = None
    started_at: str | None = None
    # staticGatewayPort：别名统一入口端口；无别名时该端口不被占用，故可为 None。
    port: int | None = ENTRY_PORT_DEFAULT
    admin_port: int = ADMIN_PORT

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def state_path(workspace: Workspace) -> Path:
    return workspace.run / STATE_FILENAME


def start_lock_path(workspace: Workspace) -> Path:
    return workspace.run / START_LOCK_FILENAME


def read_state(workspace: Workspace) -> GatewayState | None:
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
        port = data.get("port")
        return GatewayState(
            enabled=bool(data.get("enabled", False)),
            pid=int(data["pid"]) if data.get("pid") is not None else None,
            started_at=data.get("started_at"),
            port=int(port) if port is not None else None,
            admin_port=int(data.get("admin_port", ADMIN_PORT)),
        )
    except (TypeError, ValueError):
        return None


def write_state(workspace: Workspace, state: GatewayState) -> None:
    path = state_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _read_caddy_pid(gateway: StaticGateway) -> int | None:
    """读取 ``run/caddy.pid``（``caddy start --pidfile`` 写入）；缺失/非法返回 None。"""
    path = gateway.caddy_pid_path()
    if not path.is_file():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def _require_caddy_backend(gateway: StaticGateway) -> None:
    """``lwa gateway on`` 前置：backend 必须解析为 caddy，否则无意义。"""
    if gateway.detect_backend() != "caddy":
        raise LifecycleError(
            "staticGateway 非 caddy，网关服务不适用；"
            f"请在 local-web.yml 设置 staticGateway: caddy 并安装 Caddy ≥ {MIN_CADDY_VERSION}",
        )


def is_gateway_running(workspace: Workspace, config: Config) -> bool:
    """Caddy master 是否在线（admin :2019 可达）。

    以 admin 探测为准而非 pid 文件：master 崩溃后 pid 文件会残留并指向已死
    进程，单看 pid 会误判（BUG-070）。backend 非 caddy 时恒为 False。
    """
    gateway = StaticGateway(workspace, config)
    if gateway.detect_backend() != "caddy":
        return False
    return gateway._admin_alive()


@contextlib.contextmanager
def gateway_start_lock(
    workspace: Workspace, *, timeout: float = GATEWAY_START_LOCK_TIMEOUT
) -> Iterator[None]:
    """串行化 ``lwa gateway on``，避免并发 ``caddy start``。"""
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
                raise LifecycleError("网关启动锁被占用，稍后重试")
            time.sleep(0.05)
    try:
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(FileNotFoundError, PermissionError):
            path.unlink()


def start_gateway(workspace: Workspace, config: Config) -> int:
    """``lwa gateway on`` / ``lwa init`` 联动：启动 Caddy master 并写服务态。

    Caddy 由 ``caddy start --pidfile`` 自守护（:meth:`StaticGateway.caddy_start`
    已轮询 admin :2019 确认在线），成功后把 pid 写入 ``run/gateway.json``。
    已在运行则不重复启动。返回 master pid；admin 在线但读不到 pidfile 时返回 0。
    """
    gateway = StaticGateway(workspace, config)
    _require_caddy_backend(gateway)

    with gateway_start_lock(workspace):
        if is_gateway_running(workspace, config):
            state = read_state(workspace)
            # pid 优先取 live master 的 caddy.pid，缺失时回退服务态记录的最后 pid
            pid = _read_caddy_pid(gateway) or (state.pid if state else None)
            # BUG-073：网关在线但服务态缺失/未启用（如 gateway.json 被删或来自旧版）
            # → 补写恢复态，避免 gateway status 出现 running=true ∧ enabled=false
            # 的不一致。不重复 caddy start。
            if state is None or not state.enabled:
                write_state(
                    workspace,
                    GatewayState(
                        enabled=True,
                        pid=pid,
                        started_at=now_iso(),
                        port=config.staticGatewayPort,
                        admin_port=ADMIN_PORT,
                    ),
                )
                log.info("网关已在线，补写服务态（pid=%s）", pid if pid else "?")
            else:
                log.info("网关已在运行（pid=%s），不重复启动", pid if pid else "?")
            return int(pid) if pid else 0

        if not gateway.caddy_start():
            raise LifecycleError(
                "Caddy master 启动失败（caddy start 返回非零）；请检查 Caddyfile 配置与 Caddy 日志",
            )
        # BUG-074：caddy_start 无主 Caddyfile 时加载最小 bootstrap（仅保证 admin 在线），
        # 真实站点/别名片段不会自动加载。若磁盘上有 sites/aliases 片段但主配置缺失，
        # 启动后立即 _sync_main_config 按 disk 实际文件组装并 reload，使别名入口就绪。
        main = gateway.main_config_path()
        if not (main.is_file() and main.read_text(encoding="utf-8").strip()):
            gateway._sync_main_config()
        pid = _read_caddy_pid(gateway)
        state = GatewayState(
            enabled=True,
            pid=pid,
            started_at=now_iso(),
            port=config.staticGatewayPort,
            admin_port=ADMIN_PORT,
        )
        write_state(workspace, state)
        log.info(
            "网关已启动（pid=%s，admin=127.0.0.1:%d，entry=%s）",
            pid if pid else "?", ADMIN_PORT, config.staticGatewayPort,
        )
        return int(pid) if pid else 0


def stop_gateway(workspace: Workspace, config: Config) -> bool:
    """``lwa gateway off``：停止 Caddy master 并清服务态。

    backend 非 caddy 时（如已切 builtin）：清理可能残留的 stale 态文件。但若
    admin :2019 仍在线（旧 master 还在跑——典型场景：刚把 staticGateway 从 caddy
    切到 builtin 但未关 master），仍要 :meth:`caddy_stop` 关掉，兑现
    ``cli.gateway_off`` "切 builtin 后也能关 master" 的承诺（BUG-077）。

    返回是否成功停止（master 真正退出；无 master 在线时返回 True）。
    """
    gateway = StaticGateway(workspace, config)
    backend = gateway.detect_backend()
    if backend != "caddy":
        # 先清服务态，避免 status 误报 enabled
        state = read_state(workspace)
        if state is not None:
            state.enabled = False
            state.pid = None
            write_state(workspace, state)
        # BUG-077：backend 非 caddy 但 admin 仍在线 → 仍有残留 master，需关停
        if not gateway._admin_alive():
            log.info("staticGateway=%s 且无 Caddy master 在线，已清理服务态", backend)
            return True
        log.info(
            "staticGateway=%s 但检测到 Caddy master 仍在运行（admin :2019），尝试停止",
            backend,
        )
        stopped = gateway.caddy_stop()
        if not stopped:
            log.warning("Caddy master 停止失败（admin :2019 仍可能在线）")
        return stopped

    stopped = gateway.caddy_stop()
    state = read_state(workspace)
    if stopped:
        if state is not None:
            state.enabled = False
            state.pid = None
            write_state(workspace, state)
        log.info("网关已停止")
    else:
        log.warning("网关停止失败，Caddy master 可能仍在运行（admin :2019）")
    return stopped


def gateway_status(workspace: Workspace, config: Config) -> dict[str, Any]:
    """``lwa gateway status``：返回状态摘要。"""
    gateway = StaticGateway(workspace, config)
    backend = gateway.detect_backend()
    state = read_state(workspace)
    running = backend == "caddy" and gateway._admin_alive()
    pid = state.pid if state else None
    if running and pid is None:
        # 服务态缺失但 master 在线：补读 caddy.pid 便于展示。
        pid = _read_caddy_pid(gateway)
    configured_port = config.staticGatewayPort
    return {
        "running": running,
        "enabled": bool(state and state.enabled),
        "backend": backend,
        "configured": config.staticGateway,
        "pid": pid,
        "startedAt": state.started_at if state else None,
        "port": (state.port if state and state.port is not None else configured_port),
        "adminPort": ADMIN_PORT,
    }


def maybe_start_gateway(workspace: Workspace, config: Config) -> int | None:
    """``lwa init`` / ``lwa manager on`` 联动：caddy 后端时启动网关，失败只记日志。

    非 caddy 后端跳过；启动失败不抛（业务可降级 builtin 静态服务继续工作），
    仅记 warning 供排障。返回 pid（0 也算成功），跳过/失败返回 None。
    """
    gateway = StaticGateway(workspace, config)
    if gateway.detect_backend() != "caddy":
        log.info("staticGateway=%s，跳过网关自动启动", config.staticGateway)
        return None
    try:
        return start_gateway(workspace, config)
    except LifecycleError as exc:
        log.warning("网关自动启动失败（已降级，不阻断）：%s", exc)
        return None


__all__ = [
    "GatewayState",
    "ADMIN_PORT",
    "is_gateway_running",
    "start_gateway",
    "stop_gateway",
    "gateway_status",
    "maybe_start_gateway",
    "gateway_start_lock",
]
