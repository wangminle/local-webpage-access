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
from local_webpage_access.daemon import is_pid_alive
from local_webpage_access.errors import LifecycleError
from local_webpage_access.logging import get_logger, now_iso
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry
from local_webpage_access.static_gateway import StaticGateway
from local_webpage_access.version_requirements import MIN_CADDY_VERSION

log = get_logger("gateway")

STATE_FILENAME = "gateway.json"
START_LOCK_FILENAME = "gateway-start.lock"
GATEWAY_START_LOCK_TIMEOUT = 5.0
# BUG-175：启动锁陈旧回收阈值——持锁进程被 SIGKILL 后锁文件残留，超过该秒数或
# holder pid 已死即回收，避免网关从此无法启动只能人工删文件（对齐 manager_start_lock）。
GATEWAY_START_LOCK_STALE_SECONDS = 60.0

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
    """串行化 ``lwa gateway on``，避免并发 ``caddy start``；回收陈旧启动锁（BUG-175）。"""
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
            # BUG-175：回收陈旧锁——holder pid 已死，或锁文件年龄超阈值
            # （持锁进程被 SIGKILL 后残留）。否则 SIGKILL 后锁永久残留，网关再无法启动。
            stale = False
            try:
                content = path.read_text(encoding="utf-8").strip().splitlines()
                holder_pid = int(content[0]) if content else 0
                stale = not is_pid_alive(holder_pid)
                if not stale:
                    stale = (
                        time.time() - path.stat().st_mtime
                        > GATEWAY_START_LOCK_STALE_SECONDS
                    )
            except (OSError, ValueError):
                stale = True
            if stale:
                with contextlib.suppress(FileNotFoundError, PermissionError):
                    path.unlink()
                continue
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


def start_gateway(
    workspace: Workspace,
    config: Config,
    *,
    registry: Registry | None = None,
) -> int:
    """``lwa gateway on`` / ``lwa init`` 联动：启动 Caddy master 并写服务态。

    Caddy 由 ``caddy start --pidfile`` 自守护（:meth:`StaticGateway.caddy_start`
    已轮询 admin :2019 确认在线），成功后把 pid 写入 ``run/gateway.json``。
    已在运行则不重复启动。返回 master pid；admin 在线但读不到 pidfile 时返回 0。

    建议项 A/B/F（gateway-switch-access-review）：传入 ``registry`` 时额外执行切换
    事务收尾——停掉残留 builtin 静态进程、刷新各实例 LAN 访问地址、记录
    ``gateway_backend_switch`` 审计事件。``lwa init`` / ``maybe_start_gateway``
    不传 registry（失败不阻断），故不执行收尾。
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
            # 即使网关已在线，也清理可能残留的 builtin 孤儿（含 pid-less 孤儿，
            # §2.7）+ 刷新地址（建议 A/B）。不重复 caddy start，但交接收尾必须执行。
            # I1：先停旧再必要时 reload，避免 hostPort 仍被 Python 占用时站点半死。
            stopped_builtin = gateway.stop_all_builtin()
            if stopped_builtin:
                log.info("网关已在线：清理残留 builtin 静态服务 %s", ", ".join(stopped_builtin))
                try:
                    gateway.reload_all()
                except Exception as exc:  # noqa: BLE001 — reload 失败不阻断已在线网关
                    log.warning("清理 builtin 后 reload 失败（不阻断）：%s", exc)
            _post_switch_finalize(
                workspace, config, registry, pid, started=False,
                stopped_builtin=stopped_builtin,
            )
            return int(pid) if pid else 0

        # I1 / §4.1：先停残留 builtin（释放 hostPort），再拉 Caddy——避免双开竞态。
        stopped_builtin = gateway.stop_all_builtin()
        if stopped_builtin:
            log.info(
                "切换到 Caddy：启动前已停止残留 builtin 静态服务 %s",
                ", ".join(stopped_builtin),
            )

        if not gateway.caddy_start():
            raise LifecycleError(
                "Caddy master 启动失败（admin :2019 不可达或非本工作区进程）；"
                "请检查 Caddyfile、PATH 中的 caddy，以及是否有测试孤儿占用 :2019",
            )
        # BUG-074：caddy_start 无主 Caddyfile 时加载最小 bootstrap（仅保证 admin 在线），
        # 真实站点/别名片段不会自动加载。若磁盘上有 sites/aliases 片段但主配置缺失，
        # 启动后立即 _sync_main_config 按 disk 实际文件组装并 reload，使别名入口就绪。
        main = gateway.main_config_path()
        if not (main.is_file() and main.read_text(encoding="utf-8").strip()):
            gateway._sync_main_config()
        elif stopped_builtin:
            # 启动前清过占用 hostPort 的 builtin：再 reload 一次确保站点绑定生效。
            try:
                gateway.reload_all()
            except Exception as exc:  # noqa: BLE001
                log.warning("启动后 reload 失败（不阻断）：%s", exc)
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
        _post_switch_finalize(
            workspace, config, registry, pid, started=True, stopped_builtin=stopped_builtin
        )
        return int(pid) if pid else 0


def _post_switch_finalize(
    workspace: Workspace,
    config: Config,
    registry: Registry | None,
    pid: int | None,
    *,
    started: bool,
    stopped_builtin: list[str] | None = None,
) -> None:
    """切换事务收尾（建议 A/B/F）：停孤儿、刷新地址、记审计事件。

    无 registry 时（``lwa init`` / 自动启动）跳过——只保证 master 在线，不阻断。
    """
    if registry is None:
        return
    try:
        from local_webpage_access.access import refresh_network_entries

        report = refresh_network_entries(workspace, config, registry)
    except Exception as exc:  # noqa: BLE001 — 地址刷新失败不阻断网关启动
        log.warning("切换后刷新访问地址失败（不阻断）：%s", exc)
        report = None
    # F（建议 F）：审计事件——记录本次切换动作与收尾结果。
    try:
        parts = [
            f"backend=caddy pid={pid if pid else '?'}",
            f"started={'yes' if started else 'already-running'}",
        ]
        if stopped_builtin:
            parts.append(f"stopped_builtin={','.join(stopped_builtin)}")
        if report is not None:
            parts.append(f"lan_ip={report.lan_ip or 'none'}")
            parts.append(f"lan_drifted={report.drifted_count}")
        registry.add_event(None, "gateway_backend_switch", "；".join(parts))
    except Exception as exc:  # noqa: BLE001
        log.debug("记录 gateway_backend_switch 事件失败：%s", exc)


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
            try:
                from local_webpage_access.capability import clear_capability_cache

                clear_capability_cache(workspace.root, "gateway")
            except Exception:  # noqa: BLE001
                pass
            log.info("staticGateway=%s 且无 Caddy master 在线，已清理服务态", backend)
            return True
        log.info(
            "staticGateway=%s 但检测到 Caddy master 仍在运行（admin :2019），尝试停止",
            backend,
        )
        stopped = gateway.caddy_stop()
        if stopped:
            try:
                from local_webpage_access.capability import clear_capability_cache

                clear_capability_cache(workspace.root, "gateway")
            except Exception:  # noqa: BLE001
                pass
        else:
            log.warning("Caddy master 停止失败（admin :2019 仍可能在线）")
        return stopped

    stopped = gateway.caddy_stop()
    state = read_state(workspace)
    if stopped:
        if state is not None:
            state.enabled = False
            state.pid = None
            write_state(workspace, state)
        try:
            from local_webpage_access.capability import clear_capability_cache

            clear_capability_cache(workspace.root, "gateway")
        except Exception:  # noqa: BLE001
            pass
        log.info("网关已停止")
    else:
        log.warning("网关停止失败，Caddy master 可能仍在运行（admin :2019）")
    return stopped


def gateway_status(workspace: Workspace, config: Config) -> dict[str, Any]:
    """``lwa gateway status``：返回状态摘要。

    BUG-108：``running`` 以 admin :2019 是否在线为准，**不**要求
    ``staticGateway=caddy``。配置已切 builtin 但旧 master 仍占 :2019 时，
    必须报 ``running=True`` 并标 ``orphanMaster``，与 :func:`stop_gateway`
    （BUG-077）一致，避免 CLI 显示「未运行」掩盖端口占用。
    """
    gateway = StaticGateway(workspace, config)
    backend = gateway.detect_backend()
    state = read_state(workspace)
    admin_alive = gateway._admin_alive()
    running = admin_alive
    orphan_master = running and backend != "caddy"
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
        "orphanMaster": orphan_master,
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


def run_gateway_foreground(
    workspace: Workspace,
    config: Config,
    *,
    poll_interval: float = 10.0,
) -> int:
    """前台监管入口（IMP-030）：启动并持有 Caddy master，崩溃自愈，信号优雅退出。

    供 systemd/launchd 作为 ``Type=simple`` 的 ``ExecStart`` 监管（030.c：Caddy 由
    LWA 托管）。与 ``lwa gateway on`` 的 detached 启动不同：本函数前台常驻，周期
    确认 admin :2019 在线，掉线则重启 master；收到 SIGTERM/SIGINT 时停止 master 后
    退出（systemd ``Restart=on-failure`` 在异常退出时将其拉回）。
    """
    import signal
    import threading

    if config.staticGateway != "caddy":
        log.error(
            "run_gateway_foreground 仅在 staticGateway=caddy 时有意义（当前 %s）",
            config.staticGateway,
        )
        return 2

    stop_event = threading.Event()

    def _on_signal(signum: int, _frame: Any) -> None:
        log.info("gateway 前台进程收到信号 %s，准备退出", signum)
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, _on_signal)

    workspace.ensure_workspace_dirs()
    try:
        start_gateway(workspace, config)
    except LifecycleError as exc:
        log.error("网关首次启动失败：%s", exc)
        return 1

    log.info(
        "gateway 前台监管就绪（admin=127.0.0.1:%d），每 %ss 探活一次",
        ADMIN_PORT,
        poll_interval,
    )
    while not stop_event.is_set():
        # wait 既作轮询节拍又能在收到信号时立即唤醒。
        if stop_event.wait(timeout=poll_interval):
            break
        if not is_gateway_running(workspace, config):
            log.warning("Caddy master 掉线，尝试重启")
            with contextlib.suppress(LifecycleError):
                start_gateway(workspace, config)

    log.info("gateway 前台进程退出，停止 master")
    with contextlib.suppress(Exception):  # noqa: BLE001
        stop_gateway(workspace, config)
    return 0


def run_service_main() -> int:
    """网关前台监管子进程入口（``python -m local_webpage_access.gateway_service``）。"""
    import argparse

    from local_webpage_access.config import load_config
    from local_webpage_access.logging import setup_logging

    parser = argparse.ArgumentParser(
        prog="lwa-gateway", description="lwa gateway foreground supervisor (IMP-030)"
    )
    parser.add_argument("--workspace", "-w", required=True, help="工作区根目录")
    parser.add_argument("--poll", type=float, default=10.0, help="admin 探活间隔（秒）")
    parser.add_argument("--log-level", default="INFO", help="日志级别")
    args = parser.parse_args()

    # IMP-036：服务直入口平台门禁（防止绕过 CLI）
    from local_webpage_access.platform_support import require_supported_platform

    require_supported_platform()

    workspace = Workspace(Path(args.workspace).resolve())
    setup_logging(
        level=args.log_level.upper(),  # type: ignore[arg-type]
        log_dir=workspace.logs if workspace.config_path.is_file() else None,
        log_filename="gateway.log",
    )
    if not workspace.config_path.is_file():
        log.error("工作区未初始化：%s", workspace.root)
        return 2
    config = load_config(workspace)

    # BUG-233/235：gateway 进程写入 capability-gateway.json（含 Caddy runtime）
    try:
        from local_webpage_access.capability import (
            collect_capability_report,
            log_capability_probe,
            write_capability_cache,
        )

        report = collect_capability_report(
            workspace_root=workspace.root,
            role="gateway",
            config_profile=getattr(config, "profile", None),
            include_backend_cached=False,
        )
        level = "WARNING" if report.gateway_access != "ready" else "INFO"
        log_capability_probe("gateway", report, level=level)
        write_capability_cache(workspace.root, "gateway", report)
    except Exception:  # noqa: BLE001
        log.exception("gateway 能力自检失败")

    return run_gateway_foreground(workspace, config, poll_interval=args.poll)


if __name__ == "__main__":
    raise SystemExit(run_service_main())


__all__ = [
    "GatewayState",
    "ADMIN_PORT",
    "is_gateway_running",
    "start_gateway",
    "stop_gateway",
    "gateway_status",
    "maybe_start_gateway",
    "gateway_start_lock",
    "run_gateway_foreground",
    "run_service_main",
]
