"""安全、权限与默认保护（WBS-25）。

本模块是 V1 安全边界的**集中校验层**，固化设计 §17 的默认保护：

* :func:`audit_compose` —— 审计 Compose 文本：禁止 ``privileged``、禁止挂载
  Docker socket、只允许实例自己的 ``data/``（WBS-25.03/04/05）。
* :func:`audit_dockerfile` —— 审计 Dockerfile：检测 root 运行、远程 ADD、
  管道执行脚本等供应链风险。
* :func:`audit_zip_members` —— zip slip / 路径穿越防御纵深（WBS-25.10）。
  importer 已做一次拦截，此处对 skill 或外部产出的成员名再做校验。
* :func:`unknown_zip_risk_hint` —— 未知 zip 来源的标准风险提示（WBS-25.09）。
* :func:`validate_manager_binding` —— 管理页绑定地址安全性（WBS-25.02）。

校验只**判断与提示**，不直接执行任何破坏性操作；应用与否由调用方决定。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml

from local_web_access.errors import LwaError
from local_web_access.logging import get_logger

log = get_logger("security")

# ---- 数据结构 ---------------------------------------------------------------

LEVEL_CRITICAL = "critical"
LEVEL_WARN = "warn"
LEVEL_INFO = "info"


@dataclass(frozen=True)
class SecurityFinding:
    """单项安全发现。"""

    level: str  # critical / warn / info
    code: str  # 机器可读的发现码
    message: str  # 人类可读说明
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "level": self.level,
            "code": self.code,
            "message": self.message,
        }
        if self.detail:
            d["detail"] = self.detail
        return d


class SecurityError(LwaError):
    """存在 critical 级安全问题时抛出。"""

    def __init__(self, message: str, *, findings: list[SecurityFinding] | None = None) -> None:
        super().__init__(message)
        self.findings = findings or []


# ---- 敏感目录 / 危险能力 ----------------------------------------------------

# Docker socket 与同类逃逸入口
_DOCKER_SOCKET_PATHS = (
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/var/run/docker",
)

# 宿主敏感目录：禁止作为 bind mount 源
_HOST_SENSITIVE_DIRS = (
    "/", "/etc", "/var", "/usr", "/root", "/home", "/boot", "/proc", "/sys",
    "/dev", "/opt", "/srv", "/var/lib/docker",
)
_WINDOWS_HOST_SENSITIVE_DIRS = (
    "c:/", "c:/windows", "c:/users", "c:/program files", "c:/programdata",
)

# Compose 中允许的 host bind mount 源（实例自己的 data/，相对 compose 文件）
_DEFAULT_ALLOWED_HOST_MOUNTS = frozenset(
    {"./data", "../data", "./data/", "../data/"}
)

# 危险的 Linux capabilities
_DANGEROUS_CAPS = frozenset(
    {
        "SYS_ADMIN",
        "NET_ADMIN",
        "SYS_PTRACE",
        "SYS_MODULE",
        "DAC_READ_SEARCH",
        "DAC_OVERRIDE",
    }
)


# ---- Compose 审计（WBS-25.03/04/05）-----------------------------------------


def audit_compose(
    text: str,
    *,
    allowed_host_mounts: frozenset[str] = _DEFAULT_ALLOWED_HOST_MOUNTS,
) -> list[SecurityFinding]:
    """审计 Compose YAML 文本。

    检查项：
    * ``privileged: true``（WBS-25.03）→ critical
    * 挂载 Docker socket（WBS-25.04）→ critical
    * bind mount 宿主敏感目录（WBS-25.05）→ critical
    * 非 data/ 的 host bind mount → warn
    * ``network_mode: host`` → warn
    * 危险 ``cap_add`` → warn
    """
    findings: list[SecurityFinding] = []
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [
            SecurityFinding(
                LEVEL_CRITICAL,
                "invalid_yaml",
                f"Compose YAML 解析失败：{exc}",
            )
        ]
    if not isinstance(doc, dict):
        return [
            SecurityFinding(
                LEVEL_CRITICAL, "invalid_yaml", "Compose 不是合法的映射结构"
            )
        ]

    services = doc.get("services") or {}
    if not isinstance(services, dict):
        return [
            SecurityFinding(
                LEVEL_CRITICAL, "invalid_yaml", "services 段缺失或类型错误"
            )
        ]

    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        _audit_service(svc_name or "?", svc, allowed_host_mounts, findings)

    return findings


def _audit_service(
    name: str,
    svc: dict[str, Any],
    allowed_host_mounts: frozenset[str],
    out: list[SecurityFinding],
) -> None:
    if svc.get("privileged"):
        out.append(
            SecurityFinding(
                LEVEL_CRITICAL,
                "privileged",
                f"服务 {name} 启用了 privileged，违反安全边界",
            )
        )

    # cap_add
    caps = svc.get("cap_add") or []
    if isinstance(caps, list):
        for cap in caps:
            cap_upper = str(cap).upper()
            if cap_upper in _DANGEROUS_CAPS:
                out.append(
                    SecurityFinding(
                        LEVEL_WARN,
                        "dangerous_capability",
                        f"服务 {name} 添加了危险 capability：{cap}",
                    )
                )

    # network_mode
    if str(svc.get("network_mode", "")).lower() == "host":
        out.append(
            SecurityFinding(
                LEVEL_WARN,
                "host_network",
                f"服务 {name} 使用 host 网络模式，端口与宿主共享",
            )
        )

    # volumes
    volumes = svc.get("volumes") or []
    if isinstance(volumes, list):
        for vol in volumes:
            _audit_volume(name, vol, allowed_host_mounts, out)
    elif isinstance(volumes, dict):
        for target, vol_spec in volumes.items():
            _audit_volume(name, vol_spec, allowed_host_mounts, out, target=target)

    # user
    user = str(svc.get("user", "")).strip().lower()
    if user in ("root", "0"):
        out.append(
            SecurityFinding(
                LEVEL_WARN,
                "root_user",
                f"服务 {name} 以 root 运行，建议使用非 root 用户",
            )
        )


def _audit_volume(
    svc: str,
    vol: Any,
    allowed_host_mounts: frozenset[str],
    out: list[SecurityFinding],
    *,
    target: str | None = None,
) -> None:
    """审计单个 volume 条目（支持字符串和长格式）。"""
    src: str | None = None
    if isinstance(vol, str):
        if target is not None:
            # Compose dict 形式：volumes: {"/container/path": "./host/path"}
            src = vol
        else:
            # 短格式：source:target[:mode]；需兼容 Windows 盘符。
            src = _split_volume_source(vol)
    elif isinstance(vol, dict):
        vtype = str(vol.get("type", "")).lower()
        if vtype == "bind":
            src = str(vol.get("source", "") or "")
        elif vtype in ("", "volume", "tmpfs"):
            return  # 命名卷 / tmpfs 不涉及宿主路径
    else:
        return

    if not src:
        return

    # Docker socket
    src_norm = src.replace("\\", "/")
    src_lower = src_norm.lower()
    for sock in _DOCKER_SOCKET_PATHS:
        if src_norm == sock or src_norm.startswith(sock + "/"):
            out.append(
                SecurityFinding(
                    LEVEL_CRITICAL,
                    "docker_socket_mount",
                    f"服务 {svc} 挂载了 Docker socket：{src}",
                )
            )
            return

    is_windows_abs = _is_windows_abs_path(src_norm)

    # 命名卷（不以 /、. 或 Windows 盘符开头）允许
    if not src.startswith("/") and not src.startswith(".") and not is_windows_abs:
        return  # 命名卷

    # 宿主敏感目录
    src_abs = src_norm if src.startswith("/") else None
    if src_abs:
        for sens in _HOST_SENSITIVE_DIRS:
            if src_abs == sens or src_abs.startswith(sens.rstrip("/") + "/"):
                out.append(
                    SecurityFinding(
                        LEVEL_CRITICAL,
                        "host_sensitive_mount",
                        f"服务 {svc} 挂载了宿主敏感目录：{src}",
                    )
                )
                return
    if is_windows_abs:
        for sens in _WINDOWS_HOST_SENSITIVE_DIRS:
            if src_lower == sens or src_lower.startswith(sens.rstrip("/") + "/"):
                out.append(
                    SecurityFinding(
                        LEVEL_CRITICAL,
                        "host_sensitive_mount",
                        f"服务 {svc} 挂载了宿主敏感目录：{src}",
                    )
                )
                return

    # 相对路径：只允许实例自己的 data/
    if src.startswith("."):
        if src not in allowed_host_mounts:
            out.append(
                SecurityFinding(
                    LEVEL_WARN,
                    "unexpected_host_mount",
                    f"服务 {svc} 挂载了非 data/ 的宿主路径：{src}",
                    detail="实例只应挂载自己的 data/ 目录",
                )
            )

    if is_windows_abs:
        out.append(
            SecurityFinding(
                LEVEL_WARN,
                "unexpected_host_mount",
                f"服务 {svc} 挂载了非 data/ 的宿主路径：{src}",
                detail="实例只应挂载自己的 data/ 目录",
            )
        )


def _split_volume_source(vol: str) -> str | None:
    """解析 Compose 短格式 volume 的 source，兼容 Windows 盘符。"""
    if _is_windows_abs_path(vol.replace("\\", "/")):
        sep = vol.find(":", 2)
        return vol[:sep] if sep > 0 else vol
    parts = vol.split(":", 2)
    if len(parts) >= 2:
        return parts[0]
    return None


def _is_windows_abs_path(path: str) -> bool:
    return len(path) >= 3 and path[1] == ":" and path[0].isalpha() and path[2] == "/"


# ---- Dockerfile 审计 --------------------------------------------------------


def audit_dockerfile(text: str) -> list[SecurityFinding]:
    """审计 Dockerfile 文本。

    检查项：
    * ``USER root`` → warn
    * 无 ``USER`` 指令 → info（默认 root 运行）
    * ``ADD <url>`` → warn（应改用 COPY + 校验）
    * ``RUN ... | sh`` / ``| bash`` 管道执行 → warn（供应链风险）
    """
    findings: list[SecurityFinding] = []
    lines = text.splitlines()
    has_user = False
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        upper = line.upper()
        if upper.startswith("USER "):
            has_user = True
            user_val = line[5:].strip().lower()
            if user_val in ("root", "0"):
                findings.append(
                    SecurityFinding(
                        LEVEL_WARN,
                        "root_user",
                        "Dockerfile 显式以 root 运行",
                    )
                )
        elif upper.startswith("ADD ") and ("http://" in line or "https://" in line):
            findings.append(
                SecurityFinding(
                    LEVEL_WARN,
                    "add_remote_url",
                    "Dockerfile 使用 ADD 拉取远程地址，建议改用 COPY 并校验",
                    detail=line,
                )
            )
        elif upper.startswith("RUN "):
            lowered = line.lower()
            if ("| sh" in lowered or "| bash" in lowered) and (
                "curl" in lowered or "wget" in lowered
            ):
                findings.append(
                    SecurityFinding(
                        LEVEL_WARN,
                        "pipe_to_shell",
                        "Dockerfile 存在 curl|sh 类管道执行，存在供应链风险",
                        detail=line,
                    )
                )
    if not has_user:
        findings.append(
            SecurityFinding(
                LEVEL_INFO,
                "no_user",
                "Dockerfile 未声明 USER，默认以 root 运行",
            )
        )
    return findings


# ---- zip slip 防御纵深（WBS-25.10）------------------------------------------


def audit_zip_members(names: list[str]) -> list[SecurityFinding]:
    """审计 zip 成员名（zip slip / 路径穿越，WBS-25.10）。

    importer 已在解压时拦截，此函数用于对 skill 产出的成员名或二次校验场景。
    """
    findings: list[SecurityFinding] = []
    for name in names:
        norm = name.replace("\\", "/")
        # 绝对路径
        if norm.startswith("/"):
            findings.append(
                SecurityFinding(
                    LEVEL_CRITICAL,
                    "zip_absolute_path",
                    f"zip 成员使用了绝对路径：{name}",
                )
            )
            continue
        # 盘符（Windows）
        if len(norm) >= 2 and norm[1] == ":":
            findings.append(
                SecurityFinding(
                    LEVEL_CRITICAL,
                    "zip_drive_letter",
                    f"zip 成员使用了盘符路径：{name}",
                )
            )
            continue
        # 路径穿越
        depth = 0
        for part in norm.split("/"):
            if part == "..":
                depth -= 1
                if depth < 0:
                    findings.append(
                        SecurityFinding(
                            LEVEL_CRITICAL,
                            "zip_slip",
                            f"zip 成员包含路径穿越：{name}",
                        )
                    )
                    break
            elif part == "." or part == "":
                continue
            else:
                depth += 1
    return findings


# ---- 未知 zip 风险提示（WBS-25.09）------------------------------------------


def unknown_zip_risk_hint() -> str:
    """未知来源 zip 的标准风险提示文本（WBS-25.09）。

    用于 importer 把实例标记 pending 时写入事件日志，以及管理页展示。
    """
    return (
        "该 zip 来源未经信任确认。zip 内可能包含构建脚本（npm postinstall、"
        "pip install 脚本、Dockerfile RUN）、挂载声明或对外服务，存在供应链与"
        "运行时风险。请在确认来源可信后，再执行构建或启动；uncertain 实例默认"
        "保持 pending，不会自动构建或启动。"
    )


def trusted_zip_hint() -> str:
    """已通过识别、可确定运行形态的 zip 的提示（对比用）。"""
    return (
        "该 zip 已通过运行形态识别，可确定静态/容器托管方式。仍建议在首次启动前"
        "确认其来源可信。"
    )


# ---- 管理页绑定策略（WBS-25.02）---------------------------------------------


def validate_manager_binding(
    host: str, *, has_token: bool, port: int | None = None
) -> list[SecurityFinding]:
    """校验管理页绑定地址的安全性（WBS-25.02）。

    * 绑定到非回环地址（``0.0.0.0`` 等）且无 token → critical
    * 绑定到非回环地址 → info（提示局域网可达，符合设计，但需 token）
    """
    findings: list[SecurityFinding] = []
    is_loopback = host in ("127.0.0.1", "localhost", "::1")
    if not is_loopback:
        if not has_token:
            findings.append(
                SecurityFinding(
                    LEVEL_CRITICAL,
                    "lan_bind_no_token",
                    f"管理页绑定到 {host}（局域网可达）但未启用 API token，"
                    "任何同网段主机都可操作实例",
                )
            )
        else:
            findings.append(
                SecurityFinding(
                    LEVEL_INFO,
                    "lan_bind",
                    f"管理页绑定到 {host}，局域网可达；已启用 token 保护",
                    detail=f"端口 {port}" if port else None,
                )
            )
    return findings


# ---- 聚合校验 ---------------------------------------------------------------


def assert_no_critical(findings: list[SecurityFinding]) -> None:
    """若无 critical 级问题则通过，否则抛 :class:`SecurityError`。"""
    critical = [f for f in findings if f.level == LEVEL_CRITICAL]
    if critical:
        msg = "存在 critical 级安全问题：" + "; ".join(f.code for f in critical)
        raise SecurityError(msg, findings=findings)


def has_critical(findings: list[SecurityFinding]) -> bool:
    return any(f.level == LEVEL_CRITICAL for f in findings)


__all__ = [
    "LEVEL_CRITICAL",
    "LEVEL_WARN",
    "LEVEL_INFO",
    "SecurityFinding",
    "SecurityError",
    "audit_compose",
    "audit_dockerfile",
    "audit_zip_members",
    "unknown_zip_risk_hint",
    "trusted_zip_hint",
    "validate_manager_binding",
    "assert_no_critical",
    "has_critical",
]
