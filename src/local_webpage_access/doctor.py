"""``lwa doctor`` 环境与实例诊断（WBS-26）。

提供**只读**的环境健康检查与单实例排障报告。所有外部探测（Docker / 端口 /
进程）都通过可注入的 callable 完成，便于测试。

检查项（对应 WBS-26.02~11）：

* Python 版本（WBS-26.02）
* Docker 可用性（WBS-26.03）
* Docker Compose 可用性（WBS-26.04）
* 端口池可用性（WBS-26.05）
* SQLite registry（WBS-26.06）
* 静态网关（WBS-26.07）
* 磁盘空间（WBS-26.08）
* 内存与 swap（WBS-26.09）
* 单实例健康诊断（WBS-26.10）
* 修复建议（WBS-26.11，每条 failing 检查附 suggestion）
"""

from __future__ import annotations

import contextlib
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from local_webpage_access.config import Config
from local_webpage_access.logging import get_logger
from local_webpage_access.paths import Workspace
from local_webpage_access.ports import is_port_in_use
from local_webpage_access.registry import Registry
from local_webpage_access.version_requirements import (
    MIN_CADDY_VERSION,
    MIN_COMPOSE_VERSION,
    MIN_DOCKER_VERSION,
    MIN_FASTAPI_VERSION,
    MIN_UVICORN_VERSION,
    RECOMMENDED_COMPOSE_VERSION,
    installed_package_version,
    version_ge,
)

log = get_logger("doctor")

# ---- 结果数据结构 -----------------------------------------------------------

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"

# Caddy admin API 端口（与 gateway_service.ADMIN_PORT 一致，本地常量避免循环导入）。
ADMIN_DOCTOR_PORT = 2019

_ORDER = {STATUS_OK: 0, STATUS_SKIP: 1, STATUS_WARN: 2, STATUS_FAIL: 3}


@dataclass
class CheckResult:
    """单项检查结果。"""

    name: str
    status: str  # ok / warn / fail / skip
    message: str
    detail: str | None = None
    suggestion: str | None = None

    @property
    def passed(self) -> bool:
        return self.status in (STATUS_OK, STATUS_SKIP)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "message": self.message,
        }
        if self.detail:
            d["detail"] = self.detail
        if self.suggestion:
            d["suggestion"] = self.suggestion
        return d


@dataclass
class DoctorReport:
    """完整诊断报告。"""

    checks: list[CheckResult] = field(default_factory=list)
    instance_checks: list[CheckResult] = field(default_factory=list)
    instance_id: str | None = None
    # IMP-040：JSON 友好的 LAN 漂移摘要
    current_lan_ip: str | None = None
    drifted_instance_ids: list[str] = field(default_factory=list)
    # IMP-038：可选 access review（doctor --access）
    access_review: Any = None

    @property
    def overall(self) -> str:
        worst = STATUS_OK
        for c in self.checks + self.instance_checks:
            if _ORDER.get(c.status, 0) > _ORDER.get(worst, 0):
                worst = c.status
        return worst

    @property
    def has_failures(self) -> bool:
        return any(c.status == STATUS_FAIL for c in self.checks + self.instance_checks)

    def failures(self) -> list[CheckResult]:
        return [
            c
            for c in self.checks + self.instance_checks
            if c.status == STATUS_FAIL
        ]


# ---- 可注入的探测 callable 类型 --------------------------------------------

#: subprocess 运行器：接受 args 列表，返回 CompletedProcess（含 returncode/stdout/stderr）。
SubprocessRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]

#: 端口占用探测：接受端口号，返回 True 表示已被占用。
PortChecker = Callable[[int], bool]


def _default_runner(args: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    """默认 subprocess 运行器：捕获输出，不在终端回显。"""
    try:
        return subprocess.run(  # type: ignore[call-overload]
            list(args),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except FileNotFoundError as exc:
        # 命令不存在 → 返回一个非零结果，由检查项解释
        return subprocess.CompletedProcess(
            args=list(args), returncode=127, stdout="", stderr=str(exc)
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=list(args), returncode=124, stdout="", stderr="timeout"
        )


def _default_port_in_use(port: int) -> bool:
    """默认端口占用探测：委托给 :func:`is_port_in_use`（独占 bind，无 SO_REUSEADDR）。

    此前本函数自行 ``setsockopt(SO_REUSEADDR)``，在 Windows 上允许多个套接字
    绑定同一端口，会把"已有进程监听"误判为"空闲"（BUG-002 的回归，BUG-029）。
    直接复用端口分配器使用的探测实现，保证 doctor 与分配器口径一致。
    """
    return is_port_in_use(port)


# ---- 环境检查（WBS-26.02~09）-----------------------------------------------


def check_python_version() -> CheckResult:
    """WBS-26.02：Python 版本 ≥ 3.13。"""
    info = sys.version_info
    current = f"{info.major}.{info.minor}.{info.micro}"
    if (info.major, info.minor) >= (3, 13):
        return CheckResult(
            "python_version", STATUS_OK, f"Python {current}（满足 ≥ 3.13）"
        )
    return CheckResult(
        "python_version",
        STATUS_FAIL,
        f"Python {current} 不满足最低要求 ≥ 3.13",
        suggestion="安装 Python 3.13+ 后重试",
    )


def check_docker(runner: SubprocessRunner = _default_runner) -> CheckResult:
    """WBS-26.03：Docker 守护进程可用，且 server 版本 ≥ 29.0.0。"""
    result = runner(["docker", "version", "--format", "{{.Server.Version}}"])
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        blob = f"{stderr}\n{stdout}"
        from local_webpage_access.docker_runtime import (
            DOCKER_PERMISSION_HINT,
            is_docker_permission_error,
        )

        # BUG-230：区分 sock 权限与引擎未启动，避免只写笼统「不可用」
        if is_docker_permission_error(blob):
            return CheckResult(
                "docker",
                STATUS_FAIL,
                "Docker 权限不足（无法访问 docker.sock）",
                detail=(stderr or stdout)[:200] or None,
                suggestion=DOCKER_PERMISSION_HINT,
            )
        return CheckResult(
            "docker",
            STATUS_FAIL,
            "Docker 不可用",
            detail=stderr[:200] or None,
            suggestion=(
                "安装 Docker 并启动 dockerd，或确认当前用户在 docker 组中；"
                "刚 usermod -aG docker 后须 newgrp/重登，并重启 lwa manager/daemon"
            ),
        )
    version = (result.stdout or "").strip()
    if not version_ge(version, MIN_DOCKER_VERSION):
        return CheckResult(
            "docker",
            STATUS_FAIL,
            f"Docker server {version} 不满足最低要求 ≥ {MIN_DOCKER_VERSION}",
            suggestion=f"升级 Docker 至 {MIN_DOCKER_VERSION} 或更高版本",
        )
    return CheckResult(
        "docker", STATUS_OK, f"Docker 可用（server {version}，≥ {MIN_DOCKER_VERSION}）"
    )


def check_docker_compose(runner: SubprocessRunner = _default_runner) -> CheckResult:
    """WBS-26.04：Docker Compose 插件可用，并区分最低线与推荐线。"""
    result = runner(["docker", "compose", "version", "--short"])
    if result.returncode != 0:
        # 回退尝试独立 compose 二进制（v1）
        result_v1 = runner(["docker-compose", "version", "--short"])
        if result_v1.returncode == 0:
            return CheckResult(
                "docker_compose",
                STATUS_FAIL,
                f"检测到 docker-compose v1（{(result_v1.stdout or '').strip()}），"
                "不满足最低要求",
                suggestion=(
                    f"升级到 `docker compose` 插件，版本需 ≥ {MIN_COMPOSE_VERSION}"
                ),
            )
        return CheckResult(
            "docker_compose",
            STATUS_FAIL,
            "Docker Compose 不可用",
            suggestion="安装 Docker Compose 插件（`docker compose`）",
        )
    compose_version = (result.stdout or "").strip()
    if not version_ge(compose_version, MIN_COMPOSE_VERSION):
        return CheckResult(
            "docker_compose",
            STATUS_FAIL,
            f"Docker Compose {compose_version} 不满足最低要求 ≥ {MIN_COMPOSE_VERSION}",
            suggestion=f"升级 Docker Compose 至 {MIN_COMPOSE_VERSION} 或更高版本",
        )
    if not version_ge(compose_version, RECOMMENDED_COMPOSE_VERSION):
        return CheckResult(
            "docker_compose",
            STATUS_WARN,
            f"Docker Compose {compose_version} 可用，但低于推荐版本 ≥ {RECOMMENDED_COMPOSE_VERSION}",
            suggestion=f"建议升级 Docker Compose 至 {RECOMMENDED_COMPOSE_VERSION} 或更高版本",
        )
    return CheckResult(
        "docker_compose",
        STATUS_OK,
        f"Docker Compose 可用（{compose_version}，≥ {RECOMMENDED_COMPOSE_VERSION}）",
    )


def check_caddy(
    config: Config, runner: SubprocessRunner = _default_runner
) -> CheckResult:
    """Caddy 版本检查：缺失时与运行时一致，降级 builtin 并告警。"""
    if config.staticGateway != "caddy":
        return CheckResult(
            "caddy",
            STATUS_SKIP,
            f"staticGateway={config.staticGateway}，跳过 Caddy 版本检查",
        )
    if not shutil.which("caddy"):
        return CheckResult(
            "caddy",
            STATUS_WARN,
            "配置 staticGateway=caddy 但未找到 caddy，可降级 builtin 静态服务",
            suggestion=(
                f"如需 Caddy 模式，安装 Caddy ≥ {MIN_CADDY_VERSION} 并加入 PATH；"
                "否则将使用内置静态服务"
            ),
        )
    result = runner(["caddy", "version"])
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        return CheckResult(
            "caddy",
            STATUS_FAIL,
            "无法获取 Caddy 版本",
            detail=stderr[:200] or None,
            suggestion=f"确认 caddy 可执行且版本 ≥ {MIN_CADDY_VERSION}",
        )
    version_line = ((result.stdout or "") + (result.stderr or "")).strip().splitlines()
    version = version_line[0] if version_line else ""
    if not version_ge(version, MIN_CADDY_VERSION):
        return CheckResult(
            "caddy",
            STATUS_FAIL,
            f"Caddy {version.strip()} 不满足最低要求 ≥ {MIN_CADDY_VERSION}",
            suggestion=f"升级 Caddy 至 ≥ {MIN_CADDY_VERSION}",
        )
    return CheckResult(
        "caddy",
        STATUS_OK,
        f"Caddy 可用（{version.strip()}，≥ {MIN_CADDY_VERSION}）",
    )


def check_python_packages() -> CheckResult:
    """已安装的 fastapi / uvicorn 是否满足最低版本。"""
    issues: list[str] = []
    fastapi_ver = installed_package_version("fastapi")
    if fastapi_ver is None:
        issues.append("fastapi 未安装")
    elif not version_ge(fastapi_ver, MIN_FASTAPI_VERSION):
        issues.append(f"fastapi {fastapi_ver} < {MIN_FASTAPI_VERSION}")
    uvicorn_ver = installed_package_version("uvicorn")
    if uvicorn_ver is None:
        issues.append("uvicorn 未安装")
    elif not version_ge(uvicorn_ver, MIN_UVICORN_VERSION):
        issues.append(f"uvicorn {uvicorn_ver} < {MIN_UVICORN_VERSION}")
    if issues:
        return CheckResult(
            "python_packages",
            STATUS_FAIL,
            "Python 依赖版本不满足最低要求",
            detail="；".join(issues),
            suggestion=(
                f"运行 `pip install -U 'fastapi>={MIN_FASTAPI_VERSION}' "
                f"'uvicorn>={MIN_UVICORN_VERSION}'` 或 `pip install -e .`"
            ),
        )
    return CheckResult(
        "python_packages",
        STATUS_OK,
        (
            f"fastapi {fastapi_ver}（≥ {MIN_FASTAPI_VERSION}），"
            f"uvicorn {uvicorn_ver}（≥ {MIN_UVICORN_VERSION}）"
        ),
    )


def check_port_pool(
    config: Config,
    port_in_use: PortChecker = _default_port_in_use,
    *,
    allocated_ports: set[int] | None = None,
    exclude_ports: set[int] | None = None,
) -> CheckResult:
    """WBS-26.05：端口池可用性（排除 lwa 合法自用端口）。

    抽样检查池首尾；池范围很小（≤32）时全量检查。

    建议项 H（gateway-switch-access-review）：排除 lwa **合法自用**端口——
    ``managerPort``（管理页）、``staticGatewayPort``（Caddy 别名入口）、registry
    已分配的 hostPort。这些端口被 lwa 自身监听是预期状态，报为冲突会干扰切换后
    巡检（OPS-005 / OPS-030 / OPS-031 均有误报记录）。仅当端口池范围内的**外部**
    占用（非 lwa 进程）才判 FAIL。
    """
    allocated = allocated_ports or set()
    exclude = exclude_ports or set()
    # 合法自用端口：管理端口始终自用；别名入口端口由 caddy 网关自用。
    self_ports: set[int] = {config.managerPort}
    if config.staticGatewayPort is not None:
        self_ports.add(config.staticGatewayPort)
    skip = allocated | exclude | self_ports

    conflicts: list[int] = []
    start = config.portPool.start
    end = config.portPool.end
    span = end - start + 1
    # 大范围抽样，小范围全量
    if span <= 32:
        candidates: list[int] = list(range(start, end + 1))
    else:
        candidates = [start, end, start + 1, end - 1, (start + end) // 2]
    for port in candidates:
        if port in skip:
            continue
        if port_in_use(port):
            conflicts.append(port)
    if conflicts:
        return CheckResult(
            "port_pool",
            STATUS_FAIL,
            f"端口池 {start}-{end} 存在外部占用",
            detail="被占用端口：" + ", ".join(str(p) for p in sorted(set(conflicts))),
            suggestion="修改 local-web.yml 的 portPool，或停止占用这些端口的外部进程",
        )
    self_summary = ", ".join(str(p) for p in sorted(self_ports | allocated))
    return CheckResult(
        "port_pool",
        STATUS_OK,
        f"端口池 {start}-{end}（抽样）无外部占用；已排除自用端口 {self_summary}",
    )


def check_registry(ws: Workspace) -> CheckResult:
    """WBS-26.06：SQLite registry 可读写，schema 版本正确。"""
    if not ws.db_path.is_file():
        return CheckResult(
            "registry",
            STATUS_FAIL,
            f"registry 数据库不存在：{ws.db_path}",
            suggestion="运行 `lwa init` 初始化工作区",
        )
    try:
        from local_webpage_access.registry.connection import (
            CURRENT_SCHEMA_VERSION,
            get_schema_version,
        )

        reg = Registry(ws.db_path)
        reg.open()
        try:
            version = get_schema_version(reg.conn)
            count = reg.total_count()
        finally:
            reg.close()
        if version != CURRENT_SCHEMA_VERSION:
            return CheckResult(
                "registry",
                STATUS_WARN,
                f"registry schema 版本 {version}，当前代码期望 {CURRENT_SCHEMA_VERSION}",
                suggestion="运行 `lwa init`（幂等）以应用迁移",
            )
        return CheckResult(
            "registry",
            STATUS_OK,
            f"registry 可用（schema v{version}，{count} 个实例）",
        )
    except Exception as exc:
        return CheckResult(
            "registry",
            STATUS_FAIL,
            f"registry 访问失败：{exc}",
            suggestion="若数据库损坏，备份后删除并重新 `lwa init`",
        )


def check_static_gateway(ws: Workspace) -> CheckResult:
    """WBS-26.07：静态网关目录与模板就绪。"""
    if not ws.static_gateway.is_dir():
        return CheckResult(
            "static_gateway",
            STATUS_WARN,
            f"静态网关目录不存在：{ws.static_gateway}",
            suggestion="运行 `lwa init` 创建（不影响容器实例）",
        )
    return CheckResult(
        "static_gateway", STATUS_OK, f"静态网关目录就绪（{ws.static_gateway}）"
    )


def _pid_alive_local(pid: int) -> bool:
    """跨平台 pid 存活探测（不依赖 psutil）。

    BUG-178：Windows 上 ``os.kill(pid, 0)`` 走 TerminateProcess 会真的杀掉进程，
    只读诊断（check_caddy_health 的 caddy.pid 存活探测）会误杀运行中的 Caddy
    master。改用 ``OpenProcess(SYNCHRONIZE)``，与 lifecycle/static_gateway 的
    ``is_pid_alive`` 实现一致；Unix 侧 ``os.kill(pid, 0)`` 仍是安全探针。
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        SYNCHRONIZE = 0x00100000
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def check_caddy_health(
    ws: Workspace,
    config: Config,
    *,
    runner: SubprocessRunner = _default_runner,
    registry: Registry | None = None,
) -> CheckResult:
    """IMP-020：Caddy 模式下的 master/admin/配置/可达性健康探针。

    仅 ``staticGateway=caddy`` 时探测（其他后端跳过）：

    1. admin :2019 是否在线（master 是否在跑）——不在线 FAIL，提示 ``lwa gateway on``；
    2. 主 Caddyfile ``caddy validate`` 是否通过——失败 FAIL，提示悬空 import（BUG-069）；
    3. ``run/caddy.pid`` 是否指向已死进程——stale 给 WARN（BUG-070）；
    4. master 在线时（提供 registry）：别名入口 ``:8080`` 与各 enabled 站点 hostPort
       可达性——不可达 WARN，提示 reload/核对站点配置。

    master 在线、配置有效、入口与站点均可达时返回 OK。
    """
    if config.staticGateway != "caddy":
        return CheckResult(
            "caddy_health",
            STATUS_SKIP,
            f"staticGateway={config.staticGateway}，跳过 Caddy 健康探针",
        )
    from local_webpage_access.static_gateway import StaticGateway

    # 不调 gateway.detect_backend()：caddy 缺失时它会 log.warning，经 RichHandler
    # 写入 stdout，污染 `lwa doctor --json` 的输出导致 JSON 不可解析（BUG-075）。
    # 此处静默判定 caddy 是否在 PATH，结果与 detect_backend 的 caddy/builtin 分支一致。
    if not shutil.which("caddy"):
        return CheckResult(
            "caddy_health",
            STATUS_WARN,
            "配置 staticGateway=caddy 但未找到 caddy，已降级 builtin",
            suggestion=f"安装 Caddy ≥ {MIN_CADDY_VERSION} 并加入 PATH 后执行 lwa gateway on",
        )
    gateway = StaticGateway(ws, config)

    findings: list[str] = []
    admin_ok = gateway._admin_alive()
    if not admin_ok:
        findings.append("admin :2019 不可达（master 未运行）")

    validate_ok = True
    main = gateway.main_config_path()
    if main.is_file():
        result = runner(
            ["caddy", "validate", "--config", str(main), "--adapter", "caddyfile"]
        )
        validate_ok = result.returncode == 0
        if not validate_ok:
            stderr = (result.stderr or "").strip().splitlines()
            findings.append(
                "主 Caddyfile validate 失败（可能悬空 import）"
                + (f"：{stderr[0][:160]}" if stderr else "")
            )

    stale_pid = False
    pid_path = gateway.caddy_pid_path()
    if pid_path.is_file():
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pid = None
        if pid is not None and not _pid_alive_local(pid):
            stale_pid = True

    # IMP-020：master 在线时探测别名入口 :8080 与各 enabled 站点 hostPort
    entry_unreachable = False
    site_unreachable: list[str] = []
    if admin_ok and registry is not None:
        # 别名入口 :staticGatewayPort——仅当存在路径别名时才应监听（无别名时该端口空闲）
        try:
            aliases = registry.list_route_hosts()
        except Exception:  # noqa: BLE001 — registry 不可用则跳过入口/站点探测
            aliases = {}
        entry_port = config.staticGatewayPort
        if aliases and entry_port is not None:
            # BUG-080：入口根路径 / 无路由（仅 /<alias>/ 有），必须探别名子路径，
            # 否则恒 404 误报 WARN。取任一别名的 /<alias>/ 探测。
            probe_alias = next(iter(aliases))
            if not gateway.health_check(
                int(entry_port), path=f"/{probe_alias}/"
            ):
                entry_unreachable = True
                findings.append(
                    f"别名入口 :{entry_port}/{probe_alias}/ 不可达"
                    f"（已配置 {len(aliases)} 个别名，入口未就绪或 reload 未生效）"
                )
        # 各 enabled 静态站点 hostPort
        try:
            rows = registry.list_instances()
        except Exception:  # noqa: BLE001
            rows = []
        for row in rows:
            if row.get("runtime") != "shared-static":
                continue
            iid = row["id"]
            site = registry.get_static_site(iid)
            if not site or not site.get("enabled"):
                continue
            hp = site.get("host_port")
            if hp and not gateway.health_check(int(hp)):
                site_unreachable.append(f"{iid}:{hp}")
        if site_unreachable:
            preview = ", ".join(site_unreachable[:5])
            more = f" 等 {len(site_unreachable)} 个" if len(site_unreachable) > 5 else ""
            findings.append(f"enabled 站点 hostPort 不可达：{preview}{more}")

    if not admin_ok:
        return CheckResult(
            "caddy_health",
            STATUS_FAIL,
            "；".join(findings),
            suggestion="执行 `lwa gateway on` 启动 Caddy master；"
            "若反复失败，检查 static-gateway/sites 与主 Caddyfile 是否含悬空 import（BUG-069）",
        )
    if not validate_ok:
        return CheckResult(
            "caddy_health",
            STATUS_FAIL,
            "；".join(findings),
            suggestion="主 Caddyfile 非法：执行 `lwa gateway off` 再 `lwa gateway on`，"
            "或核对 sites/ 与主配置 import 一致性",
        )
    if entry_unreachable or site_unreachable:
        return CheckResult(
            "caddy_health",
            STATUS_WARN,
            "；".join(findings),
            suggestion="master 在线但部分入口/站点不可达：执行 `lwa gateway off` 再 "
            "`lwa gateway on`，或对不可达实例 `lwa restart <id>` 触发 reload",
        )
    if stale_pid:
        return CheckResult(
            "caddy_health",
            STATUS_WARN,
            "Caddy master 在线、配置有效，但 run/caddy.pid 指向已死进程（stale）",
            suggestion="执行 `lwa gateway off` 后 `lwa gateway on` 清理 stale pid（BUG-070）",
        )
    return CheckResult(
        "caddy_health",
        STATUS_OK,
        "Caddy master 在线（admin :2019），主配置 validate 通过",
    )


# ---- 建议项 F：切换交接与地址漂移诊断（gateway-switch-access-review）----------


def _list_listeners(port: int) -> list[tuple[str, str]]:
    """best-effort：用 lsof 列出端口监听者 ``[(name, pid_str), ...]``。

    POSIX 上 lsof 可用；Windows / 无 lsof 时返回空列表（调用方据此 SKIP）。
    """
    if shutil.which("lsof") is None:
        return []
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=4,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    listeners: list[tuple[str, str]] = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            listeners.append((parts[0], parts[1]))
    return listeners


def check_lan_url_stale(
    ws: Workspace, config: Config, registry: Registry
) -> CheckResult:
    """建议 F / G1：检测实例 lanUrl 是否指向失效（漂移）的 LAN IP。

    换 Wi-Fi / DHCP 续约后本机 LAN IP 变化，但各实例 ``local-web.json`` 的
    ``lanUrl`` 仅在 start/enable 时写入，不会自愈 → 管理页链接失效。本检查比对
    各实例 lanUrl host 与当前 :func:`resolve_lan_ip`（及 127.0.0.1），漂移则 WARN，
    提示 ``lwa access refresh``。
    """
    from local_webpage_access.ports import resolve_lan_ip

    lan_ip = resolve_lan_ip(config)
    drifted_ids, skipped = _collect_lan_drifted_ids(ws, registry, lan_ip)
    drifted = [f"{iid}({host})" for iid, host in drifted_ids]
    if drifted:
        return CheckResult(
            "lan_url_stale",
            STATUS_WARN,
            f"{len(drifted)} 个实例的 lanUrl 指向非当前 LAN IP（{lan_ip}）",
            detail="漂移实例：" + ", ".join(drifted[:8]),
            suggestion="运行 `lwa access refresh` 用当前 LAN IP 刷新所有实例访问地址",
        )
    return CheckResult(
        "lan_url_stale",
        STATUS_OK,
        f"实例 lanUrl 与当前 LAN IP（{lan_ip or '127.0.0.1'}）一致" + (
            "" if not skipped else f"（{skipped} 个 manifest 跳过）"
        ),
    )


def _collect_lan_drifted_ids(
    ws: Workspace, registry: Registry, lan_ip: str | None
) -> tuple[list[tuple[str, str]], int]:
    """返回 ([(instance_id, host), ...], skipped_count)。"""
    drifted: list[tuple[str, str]] = []
    skipped = 0
    for row in registry.list_instances():
        iid = row["id"]
        manifest_path = ws.app_manifest_path(iid)
        if not manifest_path.is_file():
            continue
        try:
            from local_webpage_access.models import InstanceManifest

            manifest = InstanceManifest.load(manifest_path)
        except Exception:  # noqa: BLE001
            skipped += 1
            continue
        lan_url = manifest.network.lanUrl if manifest.network else None
        if not lan_url:
            continue
        host = _url_host(lan_url)
        if lan_ip and host and host not in (lan_ip, "127.0.0.1"):
            drifted.append((iid, host))
    return drifted, skipped


def check_backend_handoff(
    ws: Workspace, config: Config, registry: Registry
) -> CheckResult:
    """建议 F / G3：检测 builtin 与 caddy 在同一 hostPort 上双开（切换残留）。

    切换 builtin↔caddy 时若旧进程未停干净（建议 A 前的现场已观察到），同一
    hostPort 会同时被 Python ``http.server`` 与 Caddy 监听，行为不确定、排障极难。
    用 lsof 检查每个 enabled 静态站点的 hostPort，发现双开则 FAIL。无 lsof 时 SKIP。
    """
    double: list[str] = []
    probed = 0
    for row in registry.list_instances():
        if row.get("runtime") != "shared-static":
            continue
        iid = row["id"]
        site = registry.get_static_site(iid)
        if not site or not site.get("enabled"):
            continue
        hp = site.get("host_port")
        if not hp:
            continue
        listeners = _list_listeners(int(hp))
        if not listeners:
            continue
        probed += 1
        names = {name.lower() for name, _ in listeners}
        has_caddy = any("caddy" in n for n in names)
        has_python = any(
            "python" in n or "http.server" in n for n in names
        )
        if has_caddy and has_python:
            double.append(
                f"{iid}:{hp}（{', '.join(sorted(names))}）"
            )
    if probed == 0:
        return CheckResult(
            "backend_handoff",
            STATUS_SKIP,
            "无 enabled 静态站点 hostPort 可探（或 lsof 不可用）",
        )
    if double:
        return CheckResult(
            "backend_handoff",
            STATUS_FAIL,
            f"{len(double)} 个 hostPort 上 builtin + caddy 双开（切换未彻底交接）",
            detail="双开端口：" + ", ".join(double),
            suggestion="运行 `lwa gateway off` 再 `lwa gateway on`，统一停掉残留进程后重启网关",
        )
    return CheckResult(
        "backend_handoff",
        STATUS_OK,
        f"已探 {probed} 个 enabled 静态 hostPort，未发现 builtin+caddy 双开",
    )


def check_port_contention(
    ws: Workspace, config: Config, *, registry: Registry | None = None
) -> CheckResult:
    """建议 F / §2.7：检测关键端口上的**非预期**监听者（测试/外部孤儿）。

    陈旧监听不只来自 builtin↔caddy 切换，也可能来自 pytest 泄漏的真实 Caddy
    占 ``:2019``（现场 pid 75224，见复盘 §2.7）。本检查断言关键端口上的监听者
    符合当前后端与工作区预期：

    * ``:2019``（admin）：若有监听者但**非** ``run/caddy.pid`` 所记 master，判 FAIL（孤儿）；
    * ``:staticGatewayPort``（别名入口）：caddy 后端下应仅 caddy 监听。

    无 lsof 时 SKIP（无法判定监听者身份）。

    仅在 ``staticGateway=caddy`` 时检查——:2019 / 别名入口都是 caddy 的端口，
    builtin 模式工作区不占用它们，其上的陈旧监听不属本工作区 concern（避免 builtin
    工作区因机器上残留的测试 caddy 而 doctor FAIL）。builtin+caddy 双开由
    :func:`check_backend_handoff` 负责。
    """
    from local_webpage_access.static_gateway import StaticGateway

    if config.staticGateway != "caddy":
        return CheckResult(
            "port_contention",
            STATUS_SKIP,
            f"staticGateway={config.staticGateway}，:2019/别名入口非本工作区占用，跳过",
        )
    findings: list[str] = []
    probed = 0
    gateway = StaticGateway(ws, config)
    # :2019 admin
    admin_listeners = _list_listeners(ADMIN_DOCTOR_PORT)
    caddy_pid = None
    pid_path = gateway.caddy_pid_path()
    if pid_path.is_file():
        try:
            caddy_pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            caddy_pid = None
    if admin_listeners:
        probed += 1
        non_self = [
            (name, pid) for name, pid in admin_listeners
            if caddy_pid is None or pid != str(caddy_pid)
        ]
        if non_self and not gateway._admin_alive():
            # :2019 被占但非本工作区 caddy master（admin 不可达说明不是健康 master）
            findings.append(
                f":2019 被 {len(non_self)} 个非预期进程占用"
                f"（{', '.join(sorted({n for n, _ in non_self}))}）——疑似测试/外部孤儿"
            )
        elif non_self:
            findings.append(
                f":2019 存在非本工作区 caddy.pid 记录的监听者"
                f"（{', '.join(sorted({n for n, _ in non_self}))}）"
            )
    # :staticGatewayPort（别名入口）：caddy 后端下应仅 caddy 监听。
    # 只要存在非 caddy 监听者即 FAIL（含 caddy+python 混合），不得因「有 caddy」放行。
    entry_port = config.staticGatewayPort
    if entry_port is not None:
        entry_listeners = _list_listeners(int(entry_port))
        if entry_listeners:
            probed += 1
            non_caddy = sorted(
                {
                    name.lower()
                    for name, _ in entry_listeners
                    if "caddy" not in name.lower()
                }
            )
            if non_caddy:
                findings.append(
                    f":{entry_port}（别名入口）存在非 caddy 监听者"
                    f"（{', '.join(non_caddy)}）"
                )
    if probed == 0:
        return CheckResult(
            "port_contention",
            STATUS_SKIP,
            "关键端口无监听者或 lsof 不可用，跳过",
        )
    if findings:
        return CheckResult(
            "port_contention",
            STATUS_FAIL,
            "；".join(findings),
            suggestion="确认监听者来源：测试泄漏用 pkill 清理；外部进程改用其他端口；"
            "本工作区用 `lwa gateway off` 再 `lwa gateway on`",
        )
    return CheckResult(
        "port_contention",
        STATUS_OK,
        f"关键端口（:2019{f'/:{entry_port}' if entry_port else ''}）监听者符合预期",
    )


def _url_host(url: str) -> str | None:
    """从 URL 提取 host（供 lan_url_stale 比对）。"""
    from urllib.parse import urlparse

    return urlparse(url).hostname


def check_disk_space(ws: Workspace, *, min_gb: float = 1.0) -> CheckResult:
    """WBS-26.08：工作区所在磁盘剩余空间。"""
    try:
        usage = shutil.disk_usage(str(ws.root))
    except OSError as exc:
        return CheckResult(
            "disk_space",
            STATUS_SKIP,
            f"无法获取磁盘信息：{exc}",
        )
    free_gb = usage.free / (1024 ** 3)
    if free_gb < min_gb:
        return CheckResult(
            "disk_space",
            STATUS_FAIL,
            f"磁盘剩余 {free_gb:.2f} GB，低于阈值 {min_gb} GB",
            detail=f"total={usage.total / 1024**3:.1f}GB used={usage.used / 1024**3:.1f}GB",
            suggestion="清理工作区 inbox/ 与 logs/，或迁移工作区到更大磁盘",
        )
    if free_gb < min_gb * 3:
        return CheckResult(
            "disk_space",
            STATUS_WARN,
            f"磁盘剩余 {free_gb:.2f} GB，接近阈值",
            suggestion="关注磁盘占用增长",
        )
    return CheckResult(
        "disk_space", STATUS_OK, f"磁盘剩余 {free_gb:.2f} GB（充足）"
    )


def check_memory() -> CheckResult:
    """WBS-26.09：内存与 swap（跨平台尽力检测）。"""
    try:
        import psutil  # type: ignore

        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        total_gb = mem.total / 1024**3
        avail_gb = mem.available / 1024**3
        if avail_gb < 0.2:
            return CheckResult(
                "memory",
                STATUS_FAIL,
                f"可用内存仅 {avail_gb:.2f} GB",
                detail=f"total={total_gb:.1f}GB swap={swap.total / 1024**3:.1f}GB",
                suggestion="停止部分实例或增加 swap",
            )
        return CheckResult(
            "memory",
            STATUS_OK,
            f"内存可用 {avail_gb:.1f} / {total_gb:.1f} GB",
        )
    except ImportError:
        pass

    # 回退：Linux /proc/meminfo
    if sys.platform.startswith("linux"):
        try:
            info: dict[str, int] = {}
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                key, _, rest = line.partition(":")
                info[key.strip()] = int(rest.strip().split()[0]) * 1024
            avail = info.get("MemAvailable", 0)
            total = info.get("MemTotal", 0)
            avail_gb = avail / 1024**3
            total_gb = total / 1024**3
            if avail_gb < 0.2:
                return CheckResult(
                    "memory",
                    STATUS_FAIL,
                    f"可用内存仅 {avail_gb:.2f} GB（/proc/meminfo）",
                    suggestion="停止部分实例或增加 swap",
                )
            return CheckResult(
                "memory",
                STATUS_OK,
                f"内存可用 {avail_gb:.1f} / {total_gb:.1f} GB（/proc/meminfo）",
            )
        except OSError:
            pass

    return CheckResult(
        "memory",
        STATUS_SKIP,
        f"无法检测内存（{platform.system()} 无 psutil）",
        suggestion="pip install psutil 以启用内存检查",
    )


# ---- 单实例诊断（WBS-26.10/11）---------------------------------------------


def diagnose_instance(
    ws: Workspace, registry: Registry, instance_id: str
) -> list[CheckResult]:
    """WBS-26.10：对单个实例执行健康诊断，返回检查项列表。"""
    from local_webpage_access.models import InstanceManifest
    from local_webpage_access.paths import validate_instance_id

    results: list[CheckResult] = []

    # 0. id 合法性（BUG-025）
    try:
        validate_instance_id(instance_id)
    except Exception as exc:
        results.append(
            CheckResult(
                f"instance:{instance_id}",
                STATUS_FAIL,
                f"实例 id 非法：{exc}",
                suggestion="实例 id 仅允许小写字母、数字、短横线",
            )
        )
        return results

    # 1. registry 中存在
    if not registry.instance_exists(instance_id):
        results.append(
            CheckResult(
                f"instance:{instance_id}",
                STATUS_FAIL,
                f"实例 {instance_id} 不在 registry",
                suggestion="确认 id 正确，或运行 `lwa list` 查看全部实例",
            )
        )
        return results

    # 2. manifest 文件
    manifest_path = ws.app_manifest_path(instance_id)
    if not manifest_path.is_file():
        results.append(
            CheckResult(
                f"instance:{instance_id}:manifest",
                STATUS_FAIL,
                f"manifest 缺失：{manifest_path}",
                suggestion="manifest 丢失，建议 remove 后重新导入",
            )
        )
    else:
        try:
            manifest = InstanceManifest.load(manifest_path)
            results.append(
                CheckResult(
                    f"instance:{instance_id}:manifest",
                    STATUS_OK,
                    f"manifest 完整（kind={manifest.kind}）",
                )
            )
        except Exception as exc:
            results.append(
                CheckResult(
                    f"instance:{instance_id}:manifest",
                    STATUS_FAIL,
                    f"manifest 解析失败：{exc}",
                    suggestion=f"检查 {manifest_path} 是否为合法 JSON",
                )
            )

    # 3. 实例目录
    app_dir = ws.app_dir(instance_id)
    if not app_dir.is_dir():
        results.append(
            CheckResult(
                f"instance:{instance_id}:files",
                STATUS_FAIL,
                f"实例目录缺失：{app_dir}",
                suggestion="文件丢失，建议 remove 后重新导入",
            )
        )
    else:
        results.append(
            CheckResult(
                f"instance:{instance_id}:files",
                STATUS_OK,
                f"实例目录就绪（{app_dir}）",
            )
        )

    # 4. 状态与最近错误
    status_row = registry.get_instance(instance_id)
    if status_row:
        status = status_row.get("status") or "?"
        last_error = status_row.get("last_error")
        desired = status_row.get("desired_state") or "?"
        if status == "failed":
            results.append(
                CheckResult(
                    f"instance:{instance_id}:status",
                    STATUS_FAIL,
                    f"实例状态 failed（期望 {desired}）",
                    detail=last_error or None,
                    suggestion="查看 logs/ 下的 run.log 与 build 日志；"
                    "可调用对应 skill 排障后 `lwa restart`",
                )
            )
        elif status == "pending":
            results.append(
                CheckResult(
                    f"instance:{instance_id}:status",
                    STATUS_WARN,
                    "实例 pending（未识别或未启动）",
                    suggestion="确认来源可信后 `lwa start`，或用 skill 补全配置",
                )
            )
        else:
            results.append(
                CheckResult(
                    f"instance:{instance_id}:status",
                    STATUS_OK,
                    f"实例状态 {status}（期望 {desired}）",
                )
            )

    # 5. 最近事件
    events = registry.list_events(instance_id, limit=5)
    if events:
        recent = events[0]
        results.append(
            CheckResult(
                f"instance:{instance_id}:events",
                STATUS_OK,
                f"最近事件：[{recent['event_type']}] {recent['message'][:80]}",
            )
        )

    # 6. 日志文件存在性
    run_log = ws.app_logs(instance_id) / "run.log"
    if run_log.is_file():
        size = run_log.stat().st_size
        results.append(
            CheckResult(
                f"instance:{instance_id}:logs",
                STATUS_OK,
                f"运行日志存在（{size} 字节）：{run_log}",
            )
        )
    else:
        results.append(
            CheckResult(
                f"instance:{instance_id}:logs",
                STATUS_WARN,
                f"未找到运行日志：{run_log}",
                suggestion="实例可能从未启动；运行 `lwa start {instance_id}`",
            )
        )

    return results


# ---- 聚合入口（WBS-26.01/11）-----------------------------------------------


def run_doctor(
    ws: Workspace,
    config: Config,
    *,
    instance_id: str | None = None,
    access_review: bool = False,
    runner: SubprocessRunner = _default_runner,
    port_in_use: PortChecker = _default_port_in_use,
) -> DoctorReport:
    """运行全部环境检查；若提供 instance_id 则附加实例诊断。

    ``access_review=True``（``lwa doctor --access``）时复用
    :func:`local_webpage_access.access.review_access`，不重写探测逻辑。
    """
    report = DoctorReport()
    allocated_ports = _allocated_ports_for_workspace(ws)
    # IMP-020：打开一个 registry 供 caddy 健康探针探测站点/别名入口可达性；
    # 打开失败不阻断整体诊断（check_caddy_health 内部对 None registry 安全降级）。
    caddy_probe_registry: Registry | None = None
    try:
        caddy_probe_registry = Registry(ws.db_path)
        caddy_probe_registry.open()
    except Exception:  # noqa: BLE001
        caddy_probe_registry = None
    try:
        from local_webpage_access.ports import resolve_lan_ip

        report.current_lan_ip = resolve_lan_ip(config)
        if caddy_probe_registry is not None:
            drifted_pairs, _skipped = _collect_lan_drifted_ids(
                ws, caddy_probe_registry, report.current_lan_ip
            )
            report.drifted_instance_ids = [iid for iid, _host in drifted_pairs]

        report.checks = [
            check_python_version(),
            check_python_packages(),
            check_docker(runner=runner),
            check_docker_compose(runner=runner),
            check_caddy(config, runner=runner),
            check_port_pool(
                config, port_in_use=port_in_use, allocated_ports=allocated_ports
            ),
            check_registry(ws),
            check_static_gateway(ws),
            check_caddy_health(
                ws, config, runner=runner, registry=caddy_probe_registry
            ),
            check_lan_url_stale(
                ws, config, caddy_probe_registry
            ) if caddy_probe_registry is not None
            else CheckResult(
                "lan_url_stale", STATUS_SKIP, "registry 不可用，跳过 lanUrl 漂移检测"
            ),
            check_backend_handoff(
                ws, config, caddy_probe_registry
            ) if caddy_probe_registry is not None
            else CheckResult(
                "backend_handoff", STATUS_SKIP, "registry 不可用，跳过后端交接检测"
            ),
            check_port_contention(ws, config, registry=caddy_probe_registry),
            check_disk_space(ws),
            check_memory(),
        ]
        if access_review and caddy_probe_registry is not None:
            from local_webpage_access.access_workflow import review_access

            try:
                report.access_review = review_access(
                    ws, config, caddy_probe_registry
                )
            except Exception as exc:  # noqa: BLE001
                report.checks.append(
                    CheckResult(
                        "access_review",
                        STATUS_FAIL,
                        f"访问复核失败：{exc}",
                        suggestion="手动运行 `lwa access review`",
                    )
                )
    finally:
        if caddy_probe_registry is not None:
            with contextlib.suppress(Exception):
                caddy_probe_registry.close()
    if instance_id:
        report.instance_id = instance_id
        try:
            reg = Registry(ws.db_path)
            reg.open()
            try:
                report.instance_checks = diagnose_instance(ws, reg, instance_id)
            finally:
                reg.close()
        except Exception as exc:
            report.instance_checks = [
                CheckResult(
                    f"instance:{instance_id}",
                    STATUS_FAIL,
                    f"实例诊断失败：{exc}",
                )
            ]
    return report


def _allocated_ports_for_workspace(ws: Workspace) -> set[int]:
    if not ws.db_path.is_file():
        return set()
    try:
        reg = Registry(ws.db_path)
        reg.open()
        try:
            return set(reg.allocated_ports())
        finally:
            reg.close()
    except Exception:
        return set()


def format_report(report: DoctorReport) -> str:
    """把报告渲染成人类可读文本（供 CLI 输出）。"""
    lines: list[str] = []
    lines.append("── 环境检查 ──")
    for c in report.checks:
        lines.append(f"  [{c.status.upper():4}] {c.name}: {c.message}")
        if c.detail:
            lines.append(f"           详情：{c.detail}")
        if c.suggestion:
            lines.append(f"           建议：{c.suggestion}")
    if report.instance_id:
        lines.append("")
        lines.append(f"── 实例诊断：{report.instance_id} ──")
        for c in report.instance_checks:
            lines.append(f"  [{c.status.upper():4}] {c.message}")
            if c.detail:
                lines.append(f"           详情：{c.detail}")
            if c.suggestion:
                lines.append(f"           建议：{c.suggestion}")
    lines.append("")
    n_fail = len([c for c in report.checks + report.instance_checks if c.status == STATUS_FAIL])
    n_warn = len([c for c in report.checks + report.instance_checks if c.status == STATUS_WARN])
    summary = f"总体：{report.overall.upper()}（{n_fail} 失败，{n_warn} 警告）"
    lines.append(summary)
    return "\n".join(lines)


__all__ = [
    "STATUS_OK",
    "STATUS_WARN",
    "STATUS_FAIL",
    "STATUS_SKIP",
    "CheckResult",
    "DoctorReport",
    "SubprocessRunner",
    "PortChecker",
    "check_python_version",
    "check_python_packages",
    "check_docker",
    "check_docker_compose",
    "check_caddy",
    "check_port_pool",
    "check_registry",
    "check_static_gateway",
    "check_caddy_health",
    "check_lan_url_stale",
    "check_backend_handoff",
    "check_port_contention",
    "check_disk_space",
    "check_memory",
    "diagnose_instance",
    "run_doctor",
    "format_report",
]
