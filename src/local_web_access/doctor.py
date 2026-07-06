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

import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from local_web_access.config import Config
from local_web_access.logging import get_logger
from local_web_access.paths import Workspace
from local_web_access.ports import is_port_in_use
from local_web_access.registry import Registry

log = get_logger("doctor")

# ---- 结果数据结构 -----------------------------------------------------------

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"

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
    """WBS-26.03：Docker 守护进程可用。"""
    result = runner(["docker", "version", "--format", "{{.Server.Version}}"])
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        return CheckResult(
            "docker",
            STATUS_FAIL,
            "Docker 不可用",
            detail=stderr[:200] or None,
            suggestion="安装 Docker 并启动 dockerd，或确认当前用户在 docker 组中",
        )
    version = (result.stdout or "").strip()
    return CheckResult(
        "docker", STATUS_OK, f"Docker 可用（server {version}）"
    )


def check_docker_compose(runner: SubprocessRunner = _default_runner) -> CheckResult:
    """WBS-26.04：Docker Compose v2（`docker compose` 子命令）可用。"""
    result = runner(["docker", "compose", "version", "--short"])
    if result.returncode != 0:
        # 回退尝试独立 compose 二进制（v1）
        result_v1 = runner(["docker-compose", "version", "--short"])
        if result_v1.returncode == 0:
            return CheckResult(
                "docker_compose",
                STATUS_WARN,
                f"检测到 docker-compose v1（{(result_v1.stdout or '').strip()}），"
                "建议升级到 `docker compose` v2 插件",
            )
        return CheckResult(
            "docker_compose",
            STATUS_FAIL,
            "Docker Compose 不可用",
            suggestion="安装 Docker Compose v2 插件（`docker compose plugin`）",
        )
    return CheckResult(
        "docker_compose",
        STATUS_OK,
        f"Docker Compose 可用（{(result.stdout or '').strip()}）",
    )


def check_port_pool(
    config: Config,
    port_in_use: PortChecker = _default_port_in_use,
    *,
    allocated_ports: set[int] | None = None,
) -> CheckResult:
    """WBS-26.05：端口池与管理端口未被占用。

    抽样检查池首尾与 manager 端口；若池范围很小（≤32）则全量检查。
    """
    allocated_ports = allocated_ports or set()
    conflicts: list[int] = []
    start = config.portPool.start
    end = config.portPool.end
    span = end - start + 1
    # 大范围抽样，小范围全量
    if span <= 32:
        candidates = range(start, end + 1)
    else:
        candidates = [start, end, start + 1, end - 1, (start + end) // 2]
    for port in candidates:
        if port in allocated_ports:
            continue
        if port_in_use(port):
            conflicts.append(port)
    # 管理端口单独检查（可能与池范围不重叠）
    if port_in_use(config.managerPort) and config.managerPort not in conflicts:
        conflicts.append(config.managerPort)
    if conflicts:
        return CheckResult(
            "port_pool",
            STATUS_FAIL,
            f"端口池 {start}-{end} 或管理端口 {config.managerPort} 存在占用",
            detail="被占用端口：" + ", ".join(str(p) for p in sorted(set(conflicts))),
            suggestion="修改 local-web.yml 的 portPool 或 managerPort，"
            "或停止占用这些端口的进程",
        )
    return CheckResult(
        "port_pool",
        STATUS_OK,
        f"端口池 {start}-{end}（抽样）与管理端口 {config.managerPort} 可用",
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
        from local_web_access.registry.connection import (
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
    from local_web_access.models import InstanceManifest
    from local_web_access.paths import validate_instance_id

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
    runner: SubprocessRunner = _default_runner,
    port_in_use: PortChecker = _default_port_in_use,
) -> DoctorReport:
    """运行全部环境检查；若提供 instance_id 则附加实例诊断。"""
    report = DoctorReport()
    allocated_ports = _allocated_ports_for_workspace(ws)
    report.checks = [
        check_python_version(),
        check_docker(runner=runner),
        check_docker_compose(runner=runner),
        check_port_pool(
            config, port_in_use=port_in_use, allocated_ports=allocated_ports
        ),
        check_registry(ws),
        check_static_gateway(ws),
        check_disk_space(ws),
        check_memory(),
    ]
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
    "check_docker",
    "check_docker_compose",
    "check_port_pool",
    "check_registry",
    "check_static_gateway",
    "check_disk_space",
    "check_memory",
    "diagnose_instance",
    "run_doctor",
    "format_report",
]
