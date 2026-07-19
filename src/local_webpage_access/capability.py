"""统一能力探测报告（IMP-033 CapabilityReport）。

setup / doctor / autostart check / manager ``/api/health`` 共用同一判定源，
避免「CLI 有权、manager 无权」时四套口径漂移。
"""

from __future__ import annotations

import getpass
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from local_webpage_access.logging import get_logger, now_iso

log = get_logger("capability")

ProfileName = Literal["default", "full"]
OverallState = Literal["ready", "degraded", "unready"]
ComponentState = Literal[
    "ready",
    "unavailable",
    "version_unsupported",
    "permission_denied",
    "daemon_unavailable",
    "timeout",
    "unknown",
    "admin_unavailable",
    "config_invalid",
    "port_conflict",
    "owner_mismatch",
    "workspace_access_denied",
    "read_denied",
    "write_denied",
]
CaddyOwner = Literal[
    "lwa_service_user", "system_caddy", "foreign_process", "unknown"
]

# 观测失败分类（与实例 runtimeAccess / observationError 对齐）
ObservationError = Literal[
    "permission_denied",
    "daemon_unavailable",
    "timeout",
    "parse_error",
    "unknown",
]


@dataclass
class CapabilityReport:
    """Full / Default 能力快照。"""

    profile: ProfileName = "default"
    overall: OverallState = "unready"
    service_user: str | None = None
    workspace_root: str | None = None
    docker_engine: ComponentState = "unknown"
    docker_compose: ComponentState = "unknown"
    docker_access: ComponentState = "unknown"
    caddy_binary: ComponentState = "unknown"
    caddy_runtime: ComponentState = "unknown"
    caddy_owner: CaddyOwner = "unknown"
    caddy_process_user: str | None = None
    caddy_workspace_access: ComponentState = "unknown"
    cli_docker_access: ComponentState = "unknown"
    manager_docker_access: ComponentState = "unknown"
    daemon_docker_access: ComponentState = "unknown"
    gateway_access: ComponentState = "unknown"
    session_refresh_required: bool = False
    action: str | None = None
    checked_at: str = field(default_factory=now_iso)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """对外 JSON（camelCase，对齐 §13.7.1）。"""
        return {
            "profile": self.profile,
            "overall": self.overall,
            "serviceUser": self.service_user,
            "workspaceRoot": self.workspace_root,
            "capabilities": {
                "dockerEngine": self.docker_engine,
                "dockerCompose": self.docker_compose,
                "dockerAccess": self.docker_access,
                "cliDockerAccess": self.cli_docker_access,
                "managerDockerAccess": self.manager_docker_access,
                "daemonDockerAccess": self.daemon_docker_access,
                "caddyBinary": self.caddy_binary,
                "caddyRuntime": self.caddy_runtime,
                "caddyOwner": self.caddy_owner,
                "caddyProcessUser": self.caddy_process_user,
                "caddyWorkspaceAccess": self.caddy_workspace_access,
                "gatewayAccess": self.gateway_access,
                "sessionRefreshRequired": self.session_refresh_required,
            },
            "action": self.action,
            "checkedAt": self.checked_at,
            "details": self.details,
        }

    def to_health_fragment(self) -> dict[str, Any]:
        """``/api/health`` 嵌入片段。"""
        body = self.to_dict()
        return {
            "profile": body["profile"],
            "overall": body["overall"],
            "serviceUser": body["serviceUser"],
            "capabilities": body["capabilities"],
            "action": body["action"],
        }


def classify_docker_observation_error(text: str | None) -> ObservationError | None:
    """从异常/stderr 文本归类观测错误；无法识别返回 ``unknown``；空文本返回 None。"""
    from local_webpage_access.docker_runtime import is_docker_permission_error

    if text is None:
        return None
    blob = text.strip()
    if not blob:
        return None
    lower = blob.lower()
    if is_docker_permission_error(blob):
        return "permission_denied"
    if "超时" in blob or "timeout" in lower or "timed out" in lower:
        return "timeout"
    if (
        "cannot connect" in lower
        or "is the docker daemon running" in lower
        or "docker daemon" in lower
        or "连接被拒绝" in blob
        or "connection refused" in lower
        or "daemon_unavailable" in lower
    ):
        return "daemon_unavailable"
    if "json" in lower and ("decode" in lower or "parse" in lower):
        return "parse_error"
    return "unknown"


def probe_docker_access_state() -> ComponentState:
    """探测当前进程的 Docker 访问能力。"""
    from local_webpage_access.docker_runtime import (
        is_docker_permission_error,
        probe_docker_permission,
    )
    from local_webpage_access.errors import DockerError
    from local_webpage_access.docker_runtime import DockerRuntime

    perm = probe_docker_permission()
    if perm:
        return "permission_denied"
    try:
        DockerRuntime._ensure_version_requirements()
    except DockerError as exc:
        msg = str(exc)
        if is_docker_permission_error(msg):
            return "permission_denied"
        kind = classify_docker_observation_error(msg)
        if kind == "timeout":
            return "timeout"
        if kind == "daemon_unavailable":
            return "daemon_unavailable"
        if "不满足最低要求" in msg or "version" in msg.lower():
            return "version_unsupported"
        if "未找到" in msg or "不可用" in msg:
            return "unavailable"
        return "unknown"
    return "ready"


def probe_caddy_binary_state() -> ComponentState:
    """探测 Caddy 二进制是否可用且满足版本下限。"""
    import shutil
    import subprocess

    from local_webpage_access.version_requirements import (
        MIN_CADDY_VERSION,
        version_ge,
    )

    if not shutil.which("caddy"):
        return "unavailable"
    try:
        cp = subprocess.run(
            ["caddy", "version"],
            capture_output=True,
            text=True,
            timeout=5,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    version_line = ((cp.stdout or "") + (cp.stderr or "")).strip().splitlines()
    version = version_line[0] if version_line else ""
    if not version:
        return "unavailable"
    if not version_ge(version, MIN_CADDY_VERSION):
        return "version_unsupported"
    return "ready"


def probe_caddy_runtime_fields(
    workspace_root: Path,
) -> tuple[ComponentState, CaddyOwner, str | None, ComponentState]:
    """探测本工作区 Caddy runtime / owner / 工作区访问（BUG-233）。

    返回 ``(caddy_runtime, caddy_owner, caddy_process_user, caddy_workspace_access)``。
    """
    from local_webpage_access.config import load_config
    from local_webpage_access.paths import Workspace
    from local_webpage_access.static_gateway import StaticGateway

    ws = Workspace(Path(workspace_root))
    try:
        config = load_config(ws) if ws.config_path.is_file() else None
    except Exception:  # noqa: BLE001 — 配置损坏时仍尽量探测
        config = None
    if config is None:
        from local_webpage_access.config import Config

        config = Config()
    try:
        gw = StaticGateway(ws, config)
        access_err = gw.verify_workspace_caddy_access()
        workspace_access: ComponentState = (
            "ready" if access_err is None else access_err  # type: ignore[assignment]
        )
        if workspace_access not in (
            "ready",
            "read_denied",
            "write_denied",
            "workspace_access_denied",
            "unknown",
        ):
            workspace_access = "workspace_access_denied"
        owner_info = gw.inspect_caddy_owner()
        owner_raw = str(owner_info.get("owner") or "unknown")
        owner: CaddyOwner
        if owner_raw in (
            "lwa_service_user",
            "system_caddy",
            "foreign_process",
            "unknown",
        ):
            owner = owner_raw  # type: ignore[assignment]
        else:
            owner = "unknown"
        runtime_raw = str(owner_info.get("runtime") or "unknown")
        if not owner_info.get("admin_alive"):
            runtime: ComponentState = "admin_unavailable"
        elif runtime_raw in (
            "ready",
            "admin_unavailable",
            "config_invalid",
            "port_conflict",
            "owner_mismatch",
            "workspace_access_denied",
            "unknown",
        ):
            runtime = runtime_raw  # type: ignore[assignment]
        else:
            runtime = "unknown"
        if workspace_access != "ready" and runtime == "ready":
            runtime = workspace_access
        proc_user = owner_info.get("process_user")
        return (
            runtime,
            owner,
            str(proc_user) if proc_user is not None else None,
            workspace_access,
        )
    except Exception:  # noqa: BLE001 — 探测失败保持 unknown，由 overall 判定
        log.debug("probe_caddy_runtime_fields failed", exc_info=True)
        return "unknown", "unknown", None, "unknown"


def current_service_user() -> str:
    """当前有效运行身份（非临时 sudo 的 root 时优先 SUDO_USER）。"""
    sudo_user = (os.environ.get("SUDO_USER") or "").strip()
    if sudo_user and os.geteuid() == 0 if hasattr(os, "geteuid") else False:
        return sudo_user
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"


def load_profile_state(workspace_root: Path | None) -> dict[str, Any]:
    """读取工作区 ``run/full-setup-state.json``（若不存在返回空）。"""
    if workspace_root is None:
        return {}
    path = Path(workspace_root) / "run" / "full-setup-state.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_profile_state(workspace_root: Path, state: dict[str, Any]) -> Path:
    """持久化 Full Profile 安装/验收状态。"""
    run_dir = Path(workspace_root) / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "full-setup-state.json"
    payload = dict(state)
    payload.setdefault("updatedAt", now_iso())
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def resolve_profile_name(
    config_profile: str | None = None,
    workspace_root: Path | None = None,
) -> ProfileName:
    """从配置或 full-setup-state 解析档位。"""
    if config_profile in ("full", "default"):
        return config_profile  # type: ignore[return-value]
    state = load_profile_state(workspace_root)
    if state.get("profile") == "full":
        return "full"
    return "default"


def collect_capability_report(
    *,
    workspace_root: Path | None = None,
    profile: ProfileName | None = None,
    role: Literal["cli", "manager", "daemon", "gateway"] = "cli",
    config_profile: str | None = None,
    include_backend_cached: bool = True,
) -> CapabilityReport:
    """采集当前进程视角的能力报告。

    ``role`` 决定把本进程 Docker 探测结果写入哪一列；manager/daemon 真实上下文
    自检后应调用本函数并以自身 role 写入，再由 health/status 文件合并展示。
    """
    root = Path(workspace_root) if workspace_root else None
    state = load_profile_state(root)
    resolved = profile or resolve_profile_name(
        config_profile or state.get("profile"), root
    )
    if isinstance(state.get("serviceUser"), str) and state["serviceUser"]:
        service_user = state["serviceUser"]
    else:
        service_user = current_service_user()

    docker_access = probe_docker_access_state()
    # Engine/Compose：权限失败时不重复跑版本探测，直接映射
    if docker_access == "permission_denied":
        docker_engine: ComponentState = "ready"  # 二进制通常在，只是无权
        docker_compose: ComponentState = "ready"
        # 若完全不可达，仍标记 access
    elif docker_access == "unavailable":
        docker_engine = "unavailable"
        docker_compose = "unavailable"
    elif docker_access == "version_unsupported":
        docker_engine = "version_unsupported"
        docker_compose = "version_unsupported"
    elif docker_access == "daemon_unavailable":
        docker_engine = "daemon_unavailable"
        docker_compose = "unknown"
    else:
        docker_engine = "ready" if docker_access == "ready" else docker_access
        docker_compose = "ready" if docker_access == "ready" else docker_access

    caddy_binary = probe_caddy_binary_state()
    caddy_runtime: ComponentState = "unknown"
    caddy_owner: CaddyOwner = "unknown"
    caddy_process_user: str | None = None
    caddy_workspace_access: ComponentState = "unknown"
    if root is not None:
        (
            caddy_runtime,
            caddy_owner,
            caddy_process_user,
            caddy_workspace_access,
        ) = probe_caddy_runtime_fields(root)

    report = CapabilityReport(
        profile=resolved,
        service_user=str(service_user),
        workspace_root=str(root.resolve()) if root else None,
        docker_engine=docker_engine,
        docker_compose=docker_compose,
        docker_access=docker_access,
        caddy_binary=caddy_binary,
        caddy_runtime=caddy_runtime,
        caddy_owner=caddy_owner,
        caddy_process_user=caddy_process_user,
        caddy_workspace_access=caddy_workspace_access,
        session_refresh_required=bool(state.get("sessionRefreshRequired")),
        action=state.get("action"),
        details={"role": role, "stateFile": bool(state)},
    )

    if role == "cli":
        report.cli_docker_access = docker_access
    elif role == "manager":
        report.manager_docker_access = docker_access
    elif role == "daemon":
        report.daemon_docker_access = docker_access
    elif role == "gateway":
        # 网关进程自身：Docker 非其职责；gatewayAccess 由 Caddy 运行态决定
        report.gateway_access = (
            "ready" if caddy_runtime == "ready" else caddy_runtime
        )

    if include_backend_cached and root is not None:
        _merge_cached_backend_probes(report, root, current_role=role)

    report.overall = _compute_overall(report)
    if report.overall != "ready" and not report.action:
        report.action = _default_action(report)
    return report


def _merge_cached_backend_probes(
    report: CapabilityReport,
    root: Path,
    *,
    current_role: Literal["cli", "manager", "daemon", "gateway"] = "cli",
) -> None:
    """合并其他后台角色的能力缓存；当前角色的实时探测永远优先。"""
    cache_dir = root / "run"
    for role, attr in (
        ("manager", "manager_docker_access"),
        ("daemon", "daemon_docker_access"),
        ("gateway", "gateway_access"),
    ):
        if role == current_role:
            continue
        path = cache_dir / f"capability-{role}.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        caps = data.get("capabilities") if isinstance(data, dict) else None
        if not isinstance(caps, dict):
            continue
        if role == "manager" and caps.get("managerDockerAccess"):
            report.manager_docker_access = caps["managerDockerAccess"]
        elif role == "daemon" and caps.get("daemonDockerAccess"):
            report.daemon_docker_access = caps["daemonDockerAccess"]
        elif role == "gateway":
            if caps.get("gatewayAccess"):
                report.gateway_access = caps["gatewayAccess"]
            if caps.get("caddyRuntime"):
                report.caddy_runtime = caps["caddyRuntime"]
            if caps.get("caddyOwner"):
                report.caddy_owner = caps["caddyOwner"]
            if caps.get("caddyProcessUser") is not None:
                report.caddy_process_user = caps.get("caddyProcessUser")
            if caps.get("caddyWorkspaceAccess"):
                report.caddy_workspace_access = caps["caddyWorkspaceAccess"]


def write_capability_cache(
    workspace_root: Path,
    role: Literal["manager", "daemon", "gateway"],
    report: CapabilityReport,
) -> Path:
    """后台进程把自身探测结果落到 ``run/capability-<role>.json``。"""
    run_dir = Path(workspace_root) / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"capability-{role}.json"
    path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def log_capability_probe(
    role: str,
    report: CapabilityReport | dict[str, Any],
    *,
    level: str = "INFO",
) -> None:
    """结构化一行能力探测日志（IMP-034.05）。"""
    if isinstance(report, CapabilityReport):
        caps = report.to_dict()["capabilities"]
        overall = report.overall
        refresh = report.session_refresh_required
        hint = report.action or ""
    else:
        caps = report.get("capabilities") or report
        overall = report.get("overall", "unknown")
        refresh = bool(
            (caps or {}).get("sessionRefreshRequired")
            if isinstance(caps, dict)
            else report.get("sessionRefreshRequired")
        )
        hint = str(report.get("action") or "")
    docker_access = (
        caps.get("dockerAccess")
        or caps.get("cliDockerAccess")
        or caps.get("managerDockerAccess")
        or caps.get("daemonDockerAccess")
        or "unknown"
    )
    msg = (
        f"capability probe role={role} overall={overall} "
        f"dockerAccess={docker_access} "
        f"sessionRefreshRequired={str(refresh).lower()} "
        f"hint={hint or '-'}"
    )
    logger = log.warning if level.upper() == "WARNING" else log.info
    if level.upper() == "ERROR":
        logger = log.error
    logger("%s", msg)


def _compute_overall(report: CapabilityReport) -> OverallState:
    if report.profile == "default":
        # default：工作区级能力宽松；仅 CLI 完全不可用时 unready 由调用方决定
        bad = {
            report.docker_access,
            report.cli_docker_access,
        }
        if "permission_denied" in bad or report.session_refresh_required:
            return "degraded"
        return "ready"

    # full：任一强制项不满足 → unready / degraded（BUG-233：含 Caddy runtime/
    # owner/工作区访问与 gatewayAccess；unknown 不得伪装 ready）
    required = [
        report.docker_engine,
        report.docker_compose,
        report.docker_access,
        report.manager_docker_access,
        report.daemon_docker_access,
        report.caddy_binary,
        report.caddy_runtime,
        report.caddy_workspace_access,
        report.gateway_access,
    ]
    # CLI 是安装/doctor 的强制能力；后台服务的健康视角不应因没有 CLI 缓存而
    # 永久 unready。后台进程仍必须证明自身及其余后台角色能力。
    if report.details.get("role", "cli") == "cli":
        required.append(report.cli_docker_access)
    if report.session_refresh_required:
        return "unready"
    hard_fail = {
        "unavailable",
        "version_unsupported",
        "permission_denied",
        "daemon_unavailable",
        "owner_mismatch",
        "workspace_access_denied",
        "admin_unavailable",
        "config_invalid",
        "port_conflict",
        "read_denied",
        "write_denied",
    }
    if any(s in hard_fail for s in required):
        return "unready"
    if report.caddy_owner in ("system_caddy", "foreign_process"):
        return "unready"
    # Full 下 unknown / timeout / degraded 一律视为尚未证明 ready
    if any(s in {"timeout", "unknown", "degraded"} for s in required):
        return "unready"
    if report.caddy_owner != "lwa_service_user":
        # owner 仍为 unknown 时同样未闭环
        return "unready"
    if any(s != "ready" for s in required):
        return "degraded"
    return "ready"


def _default_action(report: CapabilityReport) -> str | None:
    if report.session_refresh_required:
        return "重新登录或重启后执行：lwa setup --full --resume"
    if "permission_denied" in {
        report.docker_access,
        report.cli_docker_access,
        report.manager_docker_access,
        report.daemon_docker_access,
    }:
        from local_webpage_access.docker_runtime import DOCKER_PERMISSION_HINT

        return DOCKER_PERMISSION_HINT
    if report.caddy_runtime == "owner_mismatch" or report.caddy_owner == "system_caddy":
        return "停用系统 caddy.service 后执行：lwa setup --full --resume"
    if report.profile == "full" and report.overall != "ready":
        return "执行：lwa doctor --profile full 与 lwa setup --full --resume"
    return None


__all__ = [
    "CapabilityReport",
    "ObservationError",
    "classify_docker_observation_error",
    "collect_capability_report",
    "current_service_user",
    "load_profile_state",
    "log_capability_probe",
    "probe_caddy_binary_state",
    "probe_caddy_runtime_fields",
    "probe_docker_access_state",
    "resolve_profile_name",
    "save_profile_state",
    "write_capability_cache",
]
