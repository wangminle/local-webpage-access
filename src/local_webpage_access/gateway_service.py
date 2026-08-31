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
from local_webpage_access.file_lock import (
    ensure_lockable,
    release_exclusive,
    try_acquire_exclusive,
    write_lock_payload,
)
from local_webpage_access.logging import get_logger, now_iso
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry
from local_webpage_access.service_failures import (
    LastStartError,
    clear_start_failures,
    has_start_failures,
    parse_consecutive_failures,
    parse_last_start_error,
    record_start_failure,
)
from local_webpage_access.static_gateway import StaticGateway
from local_webpage_access.version_requirements import MIN_CADDY_VERSION

log = get_logger("gateway")

STATE_FILENAME = "gateway.json"
START_LOCK_FILENAME = "gateway-start.lock"
GATEWAY_START_LOCK_TIMEOUT = 5.0
# BUG-175：启动锁陈旧回收阈值——持锁进程被 SIGKILL 后锁文件残留，超过该秒数或
# holder pid 已死即回收，避免网关从此无法启动只能人工删文件（对齐 manager_start_lock）。
# 评审-组4：GATEWAY_START_LOCK_STALE_SECONDS 从未使用（flock 随进程死亡由内核释放），已删

# Caddy admin API 固定监听 IPv4 loopback（reload/stop 走它，BUG-068 显式 127.0.0.1）。
ADMIN_PORT = 2019
ENTRY_PORT_DEFAULT = 8080


@dataclass
class GatewayState:
    """Caddy 网关后台服务态。

    IMP-064：``enabled`` 仅表用户意图；启动失败写 ``last_start_error`` /
    ``consecutive_start_failures`` 观测字段，不改 ``enabled``。
    """

    enabled: bool = False
    pid: int | None = None
    started_at: str | None = None
    # staticGatewayPort：别名统一入口端口；无别名时该端口不被占用，故可为 None。
    port: int | None = ENTRY_PORT_DEFAULT
    admin_port: int = ADMIN_PORT
    last_start_error: LastStartError | None = None
    consecutive_start_failures: int = 0

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
            # IMP-064.01：旧文件缺字段读默认值，不做 schema 迁移
            last_start_error=parse_last_start_error(data),
            consecutive_start_failures=parse_consecutive_failures(data),
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
    if not gateway._admin_alive():
        return False
    owner = gateway.inspect_caddy_owner()
    return bool(owner.get("owner") == "lwa_service_user" and owner.get("workspace_match"))


@contextlib.contextmanager
def gateway_start_lock(
    workspace: Workspace, *, timeout: float = GATEWAY_START_LOCK_TIMEOUT
) -> Iterator[None]:
    """串行化 ``lwa gateway on``，避免并发 ``caddy start``；回收陈旧启动锁（BUG-175）。"""
    path = start_lock_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd: int | None = None
    deadline = time.monotonic() + timeout
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    ensure_lockable(fd)
    while True:
        try:
            try_acquire_exclusive(fd)
            write_lock_payload(fd, f"{os.getpid()}\n{time.time():.3f}\n".encode())
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise LifecycleError("网关启动锁被占用，稍后重试")
            time.sleep(0.05)
    try:
        yield
    finally:
        if fd is not None:
            release_exclusive(fd)
            with contextlib.suppress(OSError):
                os.close(fd)


def _restore_stopped_builtin(
    workspace: Workspace,
    registry: Registry | None,
    gateway: StaticGateway,
    stopped_iids: list[str],
) -> None:
    """Caddy 启动失败时把 start 前停掉的 builtin 静态服务尽力拉回（BUG-517/523）。

    ``stop_all_builtin`` 返回的 iid 含两类：正常追踪实例与 pid-less 孤儿。孤儿
    （iid 形如 ``pid-<n>`` 或 manifest/registry 无记录）无法可靠恢复，跳过。
    必须调用 ``_start_builtin`` 而非 ``enable``：后者按 ``detect_backend()``
    分支，caddy 二进制仍在时只 reload、不会拉起 http.server（BUG-523）。
    best-effort：任何失败都不掩盖原始 caddy 启动异常。
    """
    for iid in stopped_iids:
        if iid.startswith("pid-"):
            continue
        try:
            from local_webpage_access.models import InstanceManifest

            host_port: int | None = None
            path = workspace.app_manifest_path(iid)
            if path.is_file():
                try:
                    m = InstanceManifest.load(path)
                    if m.static is not None and m.static.hostPort is not None:
                        host_port = int(m.static.hostPort)
                except Exception:  # noqa: BLE001
                    host_port = None
            if host_port is None and registry is not None:
                row = registry.get_static_site(iid)
                if row is not None and row.get("host_port") is not None:
                    host_port = int(row["host_port"])
            if host_port is None:
                log.warning("无法恢复 builtin 静态服务 %s：缺少 hostPort", iid)
                continue
            public = workspace.app_public(iid)
            if not public.is_dir():
                alt = workspace.app_current(iid) / "public"
                public = alt if alt.is_dir() else public
            # BUG-523：不可调 enable()。此时 config.staticGateway 仍是 caddy，
            # detect_backend() 只要 PATH 里有 caddy 就走 Caddy 分支（写站点片段
            # + reload_all → 再次 caddy start），不会拉起 http.server。对照
            # gateway_switch._rollback 的 builtin 恢复，直接启动内置进程。
            gateway._start_builtin(iid, host_port, public)
            log.info("已恢复 builtin 静态服务 %s（port=%d）", iid, host_port)
        except Exception as exc:  # noqa: BLE001
            log.warning("恢复 builtin 静态服务 %s 失败（忽略）：%s", iid, exc)


def start_gateway(
    workspace: Workspace,
    config: Config,
    *,
    registry: Registry | None = None,
    source: str = "manual",
) -> int:
    """``lwa gateway on`` / ``lwa init`` 联动：启动 Caddy master 并写服务态。

    Caddy 由 ``caddy start --pidfile`` 自守护（:meth:`StaticGateway.caddy_start`
    已轮询 admin :2019 确认在线），成功后把 pid 写入 ``run/gateway.json``。
    已在运行则不重复启动。返回 master pid；admin 在线但读不到 pidfile 时返回 0。

    成功路径（含已在线）会写入 ``run/capability-gateway.json``，避免仅起 Caddy、
    无 gateway supervisor 时 Full Profile 因 ``gatewayAccess=unknown`` 假红。

    建议项 A/B/F（gateway-switch-access-review）：传入 ``registry`` 时额外执行切换
    事务收尾——停掉残留 builtin 静态进程、刷新各实例 LAN 访问地址、记录
    ``gateway_backend_switch`` 审计事件。``lwa init`` / ``maybe_start_gateway``
    不传 registry（失败不阻断），故不执行收尾。
    """
    gateway = StaticGateway(workspace, config)
    _require_caddy_backend(gateway)

    with gateway_start_lock(workspace):
        if gateway._admin_alive() and not is_gateway_running(workspace, config):
            owner = gateway.inspect_caddy_owner()
            # IMP-064.02：拒绝启动同样写失败观测（意图保持开），doctor 据此报
            # FAIL 并附原因，而不是「已按意图停用」。
            fail_state = read_state(workspace) or GatewayState(
                enabled=True,
                port=config.staticGatewayPort,
                admin_port=ADMIN_PORT,
            )
            fail_state.enabled = True
            record_start_failure(
                fail_state,
                f"Caddy admin :{ADMIN_PORT} 被非本工作区进程占用，启动被拒绝",
                source=source,
            )
            write_state(workspace, fail_state)
            raise LifecycleError(
                "Caddy admin :2019 已被非本工作区进程占用，拒绝停止 builtin "
                f"或认领外部网关（owner={owner.get('owner')}, pid={owner.get('pid')}）",
            )
        if is_gateway_running(workspace, config):
            state = read_state(workspace)
            # pid 优先取 live master 的 caddy.pid，缺失时回退服务态记录的最后 pid
            pid = _read_caddy_pid(gateway) or (state.pid if state else None)
            # BUG-073 / IMP-064.02（规则 9）：仅「状态文件缺失」可补写恢复态
            # enabled=True；enabled=False 且 Caddy 在线视为残留进程（IMP-060
            # residual WARN 同向），不把用户意图翻回开。
            if state is None:
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
                log.info("网关在线但服务态缺失，补写恢复态（pid=%s）", pid if pid else "?")
            elif not state.enabled:
                log.warning(
                    "网关 master 在线但 enabled=false（残留进程，不翻意图）；"
                    "如需启用请 lwa gateway on",
                )
            elif pid is not None and pid != state.pid:
                # issue #4：监督器接管后 gateway.json 可能仍停在旧裸进程记录
                # （旧 pid），不刷新会让中断时长估算/2019 冲突检查按陈旧记录虚报。
                # 检测到 live pid 与记录不一致 -> 刷新为当前 master 的事实。
                state.pid = pid
                state.started_at = now_iso()
                state.port = config.staticGatewayPort
                write_state(workspace, state)
                log.info(
                    "网关服务态陈旧（json pid=%s，live pid=%s），已刷新",
                    state.pid,
                    pid,
                )
            else:
                log.info("网关已在运行（pid=%s），不重复启动", pid if pid else "?")
                # IMP-064.06：已在运行早退清零连续失败计数。
                if state is not None and has_start_failures(state):
                    clear_start_failures(state)
                    write_state(workspace, state)
            # 即使网关已在线，也清理可能残留的 builtin 孤儿（含 pid-less 孤儿，
            # §2.7）+ 刷新地址（建议 A/B）。不重复 caddy start，但交接收尾必须执行。
            # BUG-420：已在线也不重启，但先落盘当前主配置再 reload，修复 mv 后陈旧路径。
            # I1：先停旧再 reload，避免 hostPort 仍被 Python 占用时站点半死。
            gateway.write_main_config()
            stopped_builtin = gateway.stop_all_builtin()
            if stopped_builtin:
                log.info("网关已在线：清理残留 builtin 静态服务 %s", ", ".join(stopped_builtin))
            try:
                gateway.reload_all()
            except Exception as exc:  # noqa: BLE001 — reload 失败不阻断已在线网关
                log.warning("已在线网关写盘后 reload 失败（不阻断）：%s", exc)
            _post_switch_finalize(
                workspace,
                config,
                registry,
                pid,
                started=False,
                stopped_builtin=stopped_builtin,
            )
            # 已在线路径也要刷新能力缓存：仅补写 gateway.json 不够，
            # 否则 Full Profile 会因缺 capability-gateway.json 假红（截图根因）。
            _refresh_gateway_capability(workspace, config)
            return int(pid) if pid else 0

        # I1 / §4.1：先停残留 builtin（释放 hostPort），再拉 Caddy——避免双开竞态。
        stopped_builtin = gateway.stop_all_builtin()
        if stopped_builtin:
            log.info(
                "切换到 Caddy：启动前已停止残留 builtin 静态服务 %s",
                ", ".join(stopped_builtin),
            )

        # BUG-420：caddy_start 前无条件按当前 workspace 组装落盘主配置，
        # 避免信任磁盘上可能含迁移前旧绝对路径的非空 Caddyfile（亦覆盖 BUG-074
        # 无主配置 bootstrap：启动时直接加载真实主配置，无需启动后再 sync）。
        gateway.write_main_config()

        # IMP-064.02：on 入口先断言意图（enabled=True）再 caddy_start——与
        # manager/daemon 对齐；此前首次 write_state 发生在启动成功之后，
        # `lwa gateway on` 失败会留下 enabled=false，doctor 误报「已按意图停用」。
        state = read_state(workspace)
        intent_state = GatewayState(
            enabled=True,
            pid=state.pid if state else None,
            started_at=state.started_at if state else None,
            port=config.staticGatewayPort,
            admin_port=ADMIN_PORT,
            last_start_error=state.last_start_error if state else None,
            consecutive_start_failures=(
                state.consecutive_start_failures if state else 0
            ),
        )
        write_state(workspace, intent_state)

        if not gateway.caddy_start():
            # BUG-517：Caddy 启动失败时把 start 前停掉的 builtin 拉回来，否则站点
            # 会持续下线且难自愈。best-effort，失败不掩盖原始 caddy 启动异常。
            _restore_stopped_builtin(workspace, registry, gateway, stopped_builtin)
            # IMP-064.02：失败只写失败观测 + 清 pid，绝不写 enabled=False。
            intent_state.pid = None
            record_start_failure(
                intent_state,
                "Caddy master 启动失败（admin :2019 不可达或非本工作区进程）",
                source=source,
            )
            write_state(workspace, intent_state)
            raise LifecycleError(
                "Caddy master 启动失败（admin :2019 不可达或非本工作区进程）；"
                "请检查 Caddyfile、PATH 中的 caddy，以及是否有测试孤儿占用 :2019",
            )
        if stopped_builtin:
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
        # IMP-064.02/064.06：启动成功清零失败计数。
        write_state(workspace, state)
        log.info(
            "网关已启动（pid=%s，admin=127.0.0.1:%d，entry=%s）",
            pid if pid else "?",
            ADMIN_PORT,
            config.staticGatewayPort,
        )
        _post_switch_finalize(
            workspace, config, registry, pid, started=True, stopped_builtin=stopped_builtin
        )
        # lwa gateway on / maybe_start_gateway 只起 Caddy 时也必须写能力缓存，
        # 不能依赖 gateway_service 前台监管进程才存在（截图假红）。
        _refresh_gateway_capability(workspace, config)
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
        from local_webpage_access.access_workflow import run_access_pass

        pass_result = run_access_pass(workspace, config, registry, review=False, dry_run=False)
        report = pass_result.refresh
        if pass_result.refresh_error:
            log.warning("切换后刷新访问地址失败（不阻断）：%s", pass_result.refresh_error)
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
            # IMP-064.06：用户级 off 重置失败观测。
            clear_start_failures(state)
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
            # IMP-064.06：用户级 off 重置失败观测。
            clear_start_failures(state)
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


def stop_gateway_internal(workspace: Workspace, config: Config) -> bool:
    """IMP-064.03：内部停止原语——停 Caddy master 但**不改用户意图**。

    供 ``updater.restart_gateway`` 主序列与 ``run_gateway_foreground`` 退出
    使用（后者此前调用户级 :func:`stop_gateway`，监督器 SIGTERM / 关机后会把
    用户意图翻成关，属 CHK-232 遗漏 1）。写盘只清 ``pid``。
    """
    gateway = StaticGateway(workspace, config)
    stopped = gateway.caddy_stop()
    state = read_state(workspace)
    if stopped:
        if state is not None:
            # 只清运行观测：pid=None，enabled 保持原值（064.03 核心契约）
            state.pid = None
            write_state(workspace, state)
        try:
            from local_webpage_access.capability import clear_capability_cache

            clear_capability_cache(workspace.root, "gateway")
        except Exception:  # noqa: BLE001
            pass
        log.info("网关已内部停止（意图保持 enabled=%s）", state.enabled if state else "?")
    else:
        log.warning("网关内部停止失败，Caddy master 可能仍在运行（admin :2019）")
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
    # 评审-组4：backend==caddy 但 :2019 是其它工作区/外部 master 时，此前也
    # 报 running 且无提示（掩盖端口被抢）；补 foreignMaster 标注。
    foreign_master = False
    if running and not orphan_master:
        with contextlib.suppress(Exception):
            owner = gateway.inspect_caddy_owner()
            foreign_master = not bool(owner.get("workspace_match"))
    pid = state.pid if state else None
    if running and pid is None:
        # 服务态缺失但 master 在线：补读 caddy.pid 便于展示。
        pid = _read_caddy_pid(gateway)
    configured_port = config.staticGatewayPort
    from local_webpage_access.service_failures import failure_note

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
        "foreignMaster": foreign_master,
        # IMP-064.05：透出失败观测
        "lastStartError": (
            state.last_start_error.to_dict() if state and state.last_start_error else None
        ),
        "consecutiveStartFailures": (
            state.consecutive_start_failures if state else 0
        ),
        "lastStartErrorNote": failure_note(state) if state else None,
    }


def maybe_start_gateway(workspace: Workspace, config: Config) -> int | None:
    """``lwa manager on`` / reconcile 联动：caddy 后端且 gateway 意图为开时启动。

    IMP-064（规则 9 / CHK-232 遗漏 2）：联动前查 ``service_intent``——gateway
    为 disabled / n.a.（含状态文件缺失=未表达意图）则**跳过**，不调
    ``start_gateway``。否则 ``lwa manager on`` 会把用户主动 ``gateway off``
    的意图翻回开。首次启用请显式 ``lwa gateway on``（或安装 autostart，安装
    期会置意图为开）。

    启动失败不抛（业务可降级 builtin 静态服务继续工作），仅记 warning 供排障。
    返回 pid（0 也算成功），跳过/失败返回 ``None``。
    """
    gateway = StaticGateway(workspace, config)
    if gateway.detect_backend() != "caddy":
        log.info("staticGateway=%s，跳过网关自动启动", config.staticGateway)
        return None
    state = read_state(workspace)
    if state is None or not state.enabled:
        log.info(
            "gateway 意图为 disabled（run/gateway.json enabled=%s），联动启动跳过；"
            "如需启用请 lwa gateway on",
            bool(state and state.enabled),
        )
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

    # BUG-270：必须在 Caddy 启动成功后再采能力；启动前探测恒为 admin_unavailable，
    # 会把 capability-gateway.json 冻成假红，且 probe 日志恒 WARNING。
    # start_gateway 成功路径已刷新一次；此处再刷保证前台入口语义稳定（幂等）。
    _refresh_gateway_capability(workspace, config)

    log.info(
        "gateway 前台监管就绪（admin=127.0.0.1:%d），每 %ss 探活一次",
        ADMIN_PORT,
        poll_interval,
    )
    # 进程常驻期间周期刷新能力快照（默认约 5 分钟），覆盖权限/网络等静默变化。
    polls_since_capability_refresh = 0
    capability_refresh_every_polls = max(1, int(300 / max(poll_interval, 0.1)))
    while not stop_event.is_set():
        # wait 既作轮询节拍又能在收到信号时立即唤醒。
        if stop_event.wait(timeout=poll_interval):
            break
        if not is_gateway_running(workspace, config):
            log.warning("Caddy master 掉线，尝试重启")
            with contextlib.suppress(LifecycleError):
                start_gateway(workspace, config)
                # start_gateway 已刷新；重置周期计数
                polls_since_capability_refresh = 0
            continue
        polls_since_capability_refresh += 1
        if polls_since_capability_refresh >= capability_refresh_every_polls:
            _refresh_gateway_capability(workspace, config)
            polls_since_capability_refresh = 0

    log.info("gateway 前台进程退出，停止 master")
    # IMP-064.03b：前台监管退出（监督器 SIGTERM / 关机）改走内部停止——
    # 停 master、清 pid，但保留 enabled=True。此前调用户级 stop_gateway 会把
    # 用户意图翻成关，监督器拉回后意图/观测矛盾（CHK-232 遗漏 1）。
    with contextlib.suppress(Exception):  # noqa: BLE001
        stop_gateway_internal(workspace, config)
    return 0


def _refresh_gateway_capability(workspace: Workspace, config: Config) -> None:
    """Caddy 已在线后采集并写入 capability-gateway.json（BUG-270）。"""
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
            # BUG-406：须合并 manager/daemon 存活缓存；False 会使 Full overall 因
            # peer Docker=unknown 永久假红（每 5 分钟刷新也只是重复假红）。
            include_backend_cached=True,
        )
        level = "WARNING" if report.gateway_access != "ready" else "INFO"
        log_capability_probe("gateway", report, level=level)
        write_capability_cache(workspace.root, "gateway", report)
    except Exception:  # noqa: BLE001
        log.exception("gateway 能力自检失败")


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
        level=args.log_level.upper(),
        log_dir=workspace.logs if workspace.config_path.is_file() else None,
        log_filename="gateway.log",
    )
    if not workspace.config_path.is_file():
        log.error("工作区未初始化：%s", workspace.root)
        return 2
    config = load_config(workspace)

    # BUG-270：能力探测移入 run_gateway_foreground（start_gateway 成功之后），
    # 此处不再提前 collect，避免缓存长期误报 admin_unavailable。
    return run_gateway_foreground(workspace, config, poll_interval=args.poll)


if __name__ == "__main__":
    raise SystemExit(run_service_main())


__all__ = [
    "GatewayState",
    "ADMIN_PORT",
    "is_gateway_running",
    "start_gateway",
    "stop_gateway",
    "stop_gateway_internal",
    "gateway_status",
    "maybe_start_gateway",
    "gateway_start_lock",
    "run_gateway_foreground",
    "run_service_main",
]
