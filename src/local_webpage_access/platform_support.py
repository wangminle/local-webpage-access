"""正式支持平台矩阵与运行时门禁（IMP-036）。

产品口径：仅正式支持 Linux 裸机（Ubuntu 22.04+ / Debian 12+）、WSL2 Linux、
macOS；Windows 原生 hard fail；架构仅 x86_64/amd64 与 arm64/aarch64。

**import 本模块不得 sys.exit**；门禁仅由 CLI / 服务入口显式调用
:func:`require_supported_platform`。
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from local_webpage_access.platform_detect import (
    PLATFORM_LINUX,
    PLATFORM_MACOS,
    PLATFORM_UNKNOWN,
    PLATFORM_WINDOWS,
    PLATFORM_WSL,
    detect_platform,
    systemd_available as _detect_systemd_available,
)
from local_webpage_access.version_requirements import version_ge

# ---- 产品基线（macOS 滚动下限由 release checklist 刷新）-----------------------

MIN_KERNEL_VERSION = "5.15"
MIN_GLIBC_VERSION = "2.35"
MIN_UBUNTU_VERSION = "22.04"
MIN_DEBIAN_VERSION = "12"
# 正式发布矩阵：版本 ↔ 代号一一对应（报告层与 install-*-linux.sh 共用）
SUPPORTED_UBUNTU_LTS: dict[str, str] = {
    "22.04": "jammy",
    "24.04": "noble",
    "26.04": "resolute",
}
SUPPORTED_DEBIAN_STABLE: dict[str, str] = {
    "12": "bookworm",
    "13": "trixie",
}
SUPPORTED_DEBIAN_MAJORS = frozenset(int(m) for m in SUPPORTED_DEBIAN_STABLE)
DEBIAN_UNSTABLE_CODENAMES = frozenset({"sid", "unstable", "testing", "rc-buggy"})
MIN_WSL_PACKAGE_VERSION = "2.1.5"
# 截至 2026-07：Docker Desktop「当前及前两版」→ macOS 14 Sonoma+
MACOS_MIN_MAJOR = 14
SUPPORTED_ARCHES = frozenset({"x86_64", "amd64", "aarch64", "arm64"})

_WINDOWS_ACTION = (
    "Windows 原生不受支持；请在 WSL2 的 Ubuntu 22.04+/Debian 12+ 中安装并运行 lwa"
)


def _normalize_ubuntu_series(version: str) -> str | None:
    parts = (version or "").strip().split(".")
    if len(parts) < 2:
        return None
    try:
        return f"{int(parts[0])}.{int(parts[1]):02d}"
    except ValueError:
        return None


def is_ubuntu_lts(version: str, codename: str | None = None) -> bool:
    """仅允许 :data:`SUPPORTED_UBUNTU_LTS`；若给定代号须与版本配对。"""
    series = _normalize_ubuntu_series(version)
    if series is None:
        return False
    expected = SUPPORTED_UBUNTU_LTS.get(series)
    if expected is None:
        return False
    code = (codename or "").strip().lower()
    if code and code != expected:
        return False
    return True


def is_debian_stable(version: str, codename: str | None = None) -> bool:
    """仅允许 :data:`SUPPORTED_DEBIAN_STABLE`；版本与代号须配对，并拒绝 sid/testing。"""
    code = (codename or "").strip().lower()
    if code in DEBIAN_UNSTABLE_CODENAMES:
        return False
    text = (version or "").strip()
    if not text:
        return False
    try:
        major = str(int(text.split(".")[0]))
    except ValueError:
        return False
    expected = SUPPORTED_DEBIAN_STABLE.get(major)
    if expected is None:
        return False
    if code and code != expected:
        return False
    return True


@dataclass
class PlatformSupportReport:
    """平台支持检测报告（doctor --json / 门禁共用）。"""

    platform: str
    distro_id: str | None = None
    distro_version: str | None = None
    kernel_version: str | None = None
    libc_version: str | None = None
    architecture: str = "unknown"
    wsl_version: str | None = None
    systemd_available: bool = False
    supported: bool = False
    reasons: list[str] = field(default_factory=list)
    action: str | None = None
    wsl_package_version: str | None = None
    systemd_pid1: bool = False
    docker_backend: str | None = None
    workspace_on_drvfs: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        """稳定 camelCase JSON；unsupported 时字段齐全不缺省。"""
        return {
            "platform": self.platform,
            "distroId": self.distro_id,
            "distroVersion": self.distro_version,
            "kernelVersion": self.kernel_version,
            "libcVersion": self.libc_version,
            "architecture": self.architecture,
            "wslVersion": self.wsl_version,
            "systemdAvailable": bool(self.systemd_available),
            "supported": bool(self.supported),
            "reasons": list(self.reasons),
            "action": self.action,
            "wslPackageVersion": self.wsl_package_version,
            "systemdPid1": bool(self.systemd_pid1),
            "dockerBackend": self.docker_backend,
            "workspaceOnDrvfs": self.workspace_on_drvfs,
        }


def _normalize_arch(raw: str | None) -> str:
    text = (raw or platform.machine() or "unknown").strip().lower()
    aliases = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }
    return aliases.get(text, text)


def _read_os_release() -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        with open("/etc/os-release", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                data[key] = val.strip().strip('"')
    except OSError:
        pass
    return data


def _detect_kernel_version() -> str | None:
    raw = platform.release() or ""
    match = re.match(r"(\d+\.\d+(?:\.\d+)?)", raw)
    return match.group(1) if match else (raw or None)


def _detect_libc_version() -> str | None:
    try:
        conf = os.confstr("CS_GNU_LIBC_VERSION")  # type: ignore[arg-type]
    except (AttributeError, ValueError, OSError):
        conf = None
    if conf:
        match = re.search(r"(\d+\.\d+(?:\.\d+)?)", conf)
        if match:
            return match.group(1)
    try:
        proc = subprocess.run(
            ["ldd", "--version"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        text = (proc.stdout or "") + (proc.stderr or "")
        first = text.splitlines()[0] if text else ""
        match = re.search(r"(\d+\.\d+(?:\.\d+)?)", first)
        if match:
            return match.group(1)
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    return None


def _detect_macos_major() -> str | None:
    raw = platform.mac_ver()[0] or ""
    if not raw:
        try:
            proc = subprocess.run(
                ["sw_vers", "-productVersion"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            raw = (proc.stdout or "").strip()
        except (OSError, subprocess.SubprocessError):
            return None
    match = re.match(r"(\d+)", raw)
    return match.group(1) if match else None


def systemd_is_pid1() -> bool:
    """systemd 是否为 PID 1（WSL 正式要求）。"""
    try:
        with open("/proc/1/comm", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip() == "systemd"
    except OSError:
        return False


def detect_wsl_kernel_kind() -> str | None:
    """返回 ``1`` / ``2`` / None（非 WSL）。"""
    if detect_platform() != PLATFORM_WSL:
        return None
    try:
        with open("/proc/version", encoding="utf-8", errors="replace") as fh:
            text = fh.read().lower()
    except OSError:
        text = ""
    release = (platform.release() or "").lower()
    blob = f"{text} {release}"
    if "wsl2" in blob or "microsoft-standard-wsl2" in blob:
        return "2"
    if "microsoft" in blob and "wsl2" not in blob:
        if os.environ.get("WSL_INTEROP") or os.path.isdir("/run/WSL"):
            return "2"
        return "1"
    if os.environ.get("WSL_INTEROP") or os.path.isdir("/run/WSL"):
        return "2"
    return "2"


def detect_wsl_package_version(
    runner: Callable[..., Any] = subprocess.run,
) -> str | None:
    """尝试读取 Windows 侧 WSL 包版本；失败返回 ``unknown``。"""
    if detect_platform() != PLATFORM_WSL:
        return None
    candidates: list[Sequence[str]] = [
        ("wsl.exe", "--version"),
        ("wsl.exe", "-v"),
    ]
    for args in candidates:
        try:
            proc = runner(
                list(args),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).replace("\x00", "")
        for line in text.splitlines():
            lower = line.lower()
            if "wsl" in lower and ("version" in lower or "版本" in line):
                match = re.search(r"(\d+\.\d+\.\d+(?:\.\d+)?)", line)
                if match:
                    return match.group(1)
        match = re.search(r"(\d+\.\d+\.\d+(?:\.\d+)?)", text)
        if match and proc.returncode == 0:
            return match.group(1)
    return "unknown"


def is_wsl_drvfs_path(path: Path | str) -> bool:
    """工作区是否落在 WSL ``/mnt/<drive>``（Windows 盘符挂载）。"""
    try:
        resolved = Path(path).resolve()
    except OSError:
        resolved = Path(path)
    parts = resolved.parts
    if len(parts) >= 3 and parts[0] == "/" and parts[1] == "mnt":
        drive = parts[2]
        return len(drive) == 1 and drive.isalpha()
    return False


def detect_wsl_docker_backend(
    runner: Callable[..., Any] = subprocess.run,
) -> str:
    """WSL Docker 后端：desktop / engine / conflict / none / unknown。"""
    if detect_platform() != PLATFORM_WSL:
        return "unknown"
    desktop_markers = [
        Path("/mnt/wsl/docker-desktop"),
        Path("/mnt/wsl/docker-desktop/shared-sockets"),
    ]
    has_desktop = any(p.exists() for p in desktop_markers)
    context_name = ""
    try:
        proc = runner(
            ["docker", "context", "show"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0:
            context_name = (proc.stdout or "").strip().lower()
    except (OSError, subprocess.SubprocessError):
        pass
    if context_name in {"desktop-linux", "docker-desktop"}:
        has_desktop = True

    has_engine = False
    try:
        proc = runner(
            ["systemctl", "is-active", "docker"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0 and "active" in (proc.stdout or ""):
            has_engine = True
    except (OSError, subprocess.SubprocessError):
        pass
    if Path("/var/run/docker.sock").exists() and not has_desktop:
        has_engine = True
    if has_desktop and has_engine:
        return "conflict"
    if has_desktop:
        return "desktop"
    if has_engine:
        return "engine"
    try:
        proc = runner(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if proc.returncode == 0:
            return "unknown"
    except (OSError, subprocess.SubprocessError):
        pass
    return "none"


def collect_platform_support_report(
    *,
    platform_name: str | None = None,
    distro_id: str | None = None,
    distro_version: str | None = None,
    distro_codename: str | None = None,
    kernel_version: str | None = None,
    libc_version: str | None = None,
    architecture: str | None = None,
    wsl_version: str | None = None,
    wsl_package_version: str | None = None,
    systemd_available: bool | None = None,
    systemd_pid1: bool | None = None,
    docker_backend: str | None = None,
    workspace_root: Path | str | None = None,
) -> PlatformSupportReport:
    """收集并判定平台支持；所有事实均可注入（单测不依赖真实宿主）。

    ``workspace_on_drvfs`` 仅在显式传入 ``workspace_root`` 时判定（BUG-260：
    不得用 cwd 污染全局 supported）。``/mnt/<drive>`` 不并入 unsupported reasons，
    由 :func:`assert_writable_workspace_allowed` 在 Full/autostart 写路径阻断。
    """

    plat = platform_name if platform_name is not None else detect_platform()
    arch = _normalize_arch(architecture)

    os_rel = _read_os_release() if plat in (PLATFORM_LINUX, PLATFORM_WSL) else {}
    if distro_id is None and os_rel:
        distro_id = (os_rel.get("ID") or "").lower() or None
    if distro_version is None and os_rel:
        distro_version = os_rel.get("VERSION_ID")
    if distro_codename is None and os_rel:
        distro_codename = (
            os_rel.get("VERSION_CODENAME") or os_rel.get("UBUNTU_CODENAME") or None
        )

    if kernel_version is None and plat != PLATFORM_WINDOWS:
        kernel_version = _detect_kernel_version()

    if libc_version is None and plat in (PLATFORM_LINUX, PLATFORM_WSL):
        libc_version = _detect_libc_version()

    if plat == PLATFORM_MACOS and distro_version is None:
        distro_version = _detect_macos_major()

    # 区分内核种类与包版本
    kernel_kind = wsl_version
    if kernel_kind is None and plat == PLATFORM_WSL:
        kernel_kind = detect_wsl_kernel_kind()
    if wsl_package_version is None and plat == PLATFORM_WSL:
        wsl_package_version = detect_wsl_package_version()

    if systemd_available is None:
        systemd_available = (
            _detect_systemd_available()
            if plat in (PLATFORM_LINUX, PLATFORM_WSL)
            else False
        )
    if systemd_pid1 is None:
        systemd_pid1 = (
            systemd_is_pid1() if plat in (PLATFORM_LINUX, PLATFORM_WSL) else False
        )

    if docker_backend is None and plat == PLATFORM_WSL:
        docker_backend = detect_wsl_docker_backend()

    # BUG-260：仅显式 workspace_root 才填 workspaceOnDrvfs；勿用 cwd 推断
    workspace_on_drvfs: bool | None = None
    if workspace_root is not None:
        workspace_on_drvfs = is_wsl_drvfs_path(workspace_root)

    display_wsl = None
    if plat == PLATFORM_WSL:
        display_wsl = wsl_package_version or kernel_kind

    report = PlatformSupportReport(
        platform=plat,
        distro_id=distro_id,
        distro_version=distro_version,
        kernel_version=kernel_version,
        libc_version=libc_version,
        architecture=arch,
        wsl_version=display_wsl,
        systemd_available=bool(systemd_available),
        supported=False,
        reasons=[],
        action=None,
        wsl_package_version=wsl_package_version,
        systemd_pid1=bool(systemd_pid1),
        docker_backend=docker_backend,
        workspace_on_drvfs=workspace_on_drvfs,
    )

    reasons: list[str] = []

    if plat == PLATFORM_WINDOWS:
        reasons.append("检测到 Windows 原生进程，正式支持范围不包含 Windows 原生")
        report.reasons = reasons
        report.action = _WINDOWS_ACTION
        return report

    if plat == PLATFORM_UNKNOWN:
        reasons.append("无法识别操作系统，无法证明满足正式支持矩阵")
        report.reasons = reasons
        report.action = "请在 Ubuntu 22.04+/Debian 12+/WSL2 或 macOS 14+ 上运行"
        return report

    if arch not in {"x86_64", "arm64"}:
        reasons.append(
            f"架构 {architecture or arch} 不在正式支持列表（仅 x86_64/amd64、arm64/aarch64）"
        )

    if plat == PLATFORM_MACOS:
        try:
            major = int(str(distro_version or "0").split(".")[0])
        except ValueError:
            major = 0
        if major < MACOS_MIN_MAJOR:
            reasons.append(
                f"macOS 版本 {distro_version or 'unknown'} 低于滚动下限 "
                f"{MACOS_MIN_MAJOR}（截至 2026-07 为 Sonoma+）"
            )
        report.supported = not reasons
        report.reasons = reasons
        report.action = (
            None
            if report.supported
            else f"请升级到 macOS {MACOS_MIN_MAJOR}+（Docker Desktop 当前及前两版策略）"
        )
        return report

    if plat not in (PLATFORM_LINUX, PLATFORM_WSL):
        reasons.append(f"平台 {plat} 不在正式支持矩阵")
        report.reasons = reasons
        report.action = "请使用 Ubuntu/Debian 裸机、WSL2 或 macOS"
        return report

    did = (distro_id or "").lower()
    dver = distro_version or ""
    dcode = (distro_codename or "").strip().lower() or None
    if did == "ubuntu":
        if not is_ubuntu_lts(dver, dcode):
            reasons.append(
                f"Ubuntu {dver or 'unknown'}"
                + (f"（{dcode}）" if dcode else "")
                + " 不在正式支持矩阵（仅 "
                + "/".join(
                    f"{ver}={code}" for ver, code in sorted(SUPPORTED_UBUNTU_LTS.items())
                )
                + "）"
            )
    elif did == "debian":
        if not is_debian_stable(dver, dcode):
            if dcode and dcode in DEBIAN_UNSTABLE_CODENAMES:
                reasons.append(
                    f"Debian 代号 {dcode} 不是 Stable（仅支持 "
                    + "/".join(
                        f"{maj}={code}"
                        for maj, code in sorted(SUPPORTED_DEBIAN_STABLE.items())
                    )
                    + "）"
                )
            else:
                reasons.append(
                    f"Debian {dver or 'unknown'}"
                    + (f"（{dcode}）" if dcode else "")
                    + " 不在正式支持矩阵或版本/代号不匹配（仅 "
                    + "/".join(
                        f"{maj}={code}"
                        for maj, code in sorted(SUPPORTED_DEBIAN_STABLE.items())
                    )
                    + "）"
                )
    else:
        reasons.append(
            f"发行版 {did or 'unknown'} 不在正式支持矩阵（仅 Ubuntu LTS / Debian Stable）"
        )

    if not kernel_version or not version_ge(kernel_version, MIN_KERNEL_VERSION):
        reasons.append(
            f"内核 {kernel_version or 'unknown'} 低于最低要求 {MIN_KERNEL_VERSION}"
        )

    if not libc_version or not version_ge(libc_version, MIN_GLIBC_VERSION):
        reasons.append(
            f"glibc {libc_version or 'unknown'} 低于最低要求 {MIN_GLIBC_VERSION}"
        )

    if plat == PLATFORM_LINUX:
        if not systemd_available:
            reasons.append("systemd 不可用（需要 systemctl 与 user manager）")
    elif plat == PLATFORM_WSL:
        if kernel_kind == "1":
            reasons.append("WSL1 不受支持；请升级到 WSL2")
        pkg = wsl_package_version
        if pkg is None or pkg == "unknown" or pkg == "":
            reasons.append(
                "无法确定 WSL 包版本（wslVersion=unknown）；"
                f"写操作 fail-closed，请在 Windows 侧执行 wsl --version"
                f"（需 ≥ {MIN_WSL_PACKAGE_VERSION}）"
            )
        elif not version_ge(pkg, MIN_WSL_PACKAGE_VERSION):
            reasons.append(
                f"WSL 包版本 {pkg} 低于最低要求 {MIN_WSL_PACKAGE_VERSION}"
            )
        if not systemd_pid1:
            reasons.append(
                "WSL 中 systemd 不是 PID 1；请在 /etc/wsl.conf 启用 "
                "[boot] systemd=true 后 wsl --shutdown"
            )
        elif not systemd_available:
            reasons.append("systemd user manager 不可用")
        if docker_backend == "conflict":
            reasons.append(
                "同时检测到 Docker Desktop WSL integration 与发行版内 Docker Engine，"
                "Full Profile 不得假绿"
            )
        # BUG-260：/mnt/<drive> 仅报告字段，不并入 unsupported reasons

    report.reasons = reasons
    report.supported = len(reasons) == 0
    if report.supported:
        report.action = None
    elif plat == PLATFORM_WSL:
        report.action = (
            "请使用 WSL2（包 ≥ "
            f"{MIN_WSL_PACKAGE_VERSION}）+ Ubuntu 22.04 LTS+/Debian 12+ Stable，"
            "启用 systemd；Full/autostart 工作区请放在 Linux 文件系统"
        )
    else:
        report.action = (
            "请使用 Ubuntu LTS（"
            + "/".join(sorted(SUPPORTED_UBUNTU_LTS))
            + "）或 Debian Stable（"
            + "/".join(sorted(SUPPORTED_DEBIAN_STABLE))
            + f"），内核 ≥ {MIN_KERNEL_VERSION}，glibc ≥ {MIN_GLIBC_VERSION}，并确保 systemd 可用"
        )
    return report


def require_supported_platform(
    *,
    report: PlatformSupportReport | None = None,
    workspace_root: Path | str | None = None,
    exit_code: int = 2,
    file=None,
) -> PlatformSupportReport:
    """不支持则向 stderr 打印中文原因并以非零码退出。"""
    file = sys.stderr if file is None else file
    # BUG-260：优先用显式 workspace_root；否则尝试定位已有工作区，勿用 cwd 猜 drvfs
    root = workspace_root
    if root is None and report is None:
        try:
            from local_webpage_access.paths import find_workspace_root

            root = find_workspace_root()
        except Exception:  # noqa: BLE001 — 门禁不得因定位失败崩溃
            root = None
    rep = report or collect_platform_support_report(workspace_root=root)
    if rep.supported:
        return rep
    action = rep.action or "当前平台不受支持"
    print(action, file=file)
    for reason in rep.reasons:
        print(f"  - {reason}", file=file)
    raise SystemExit(exit_code)


def assert_writable_workspace_allowed(
    workspace_root: Path | str,
    *,
    report: PlatformSupportReport | None = None,
) -> None:
    """Full/autostart 写路径：WSL ``/mnt/<drive>`` fail-closed。"""
    if not is_wsl_drvfs_path(workspace_root):
        return
    plat = report.platform if report is not None else detect_platform()
    if plat != PLATFORM_WSL:
        return
    raise SystemExit(
        "工作区位于 /mnt/<drive>（Windows 文件系统），Full/autostart 写路径已阻断；"
        "请将工作区迁移到 Linux 文件系统（如 ~/lwa）后重试"
    )


__all__ = [
    "DEBIAN_UNSTABLE_CODENAMES",
    "MACOS_MIN_MAJOR",
    "MIN_DEBIAN_VERSION",
    "MIN_GLIBC_VERSION",
    "MIN_KERNEL_VERSION",
    "MIN_UBUNTU_VERSION",
    "MIN_WSL_PACKAGE_VERSION",
    "SUPPORTED_ARCHES",
    "SUPPORTED_DEBIAN_MAJORS",
    "SUPPORTED_DEBIAN_STABLE",
    "SUPPORTED_UBUNTU_LTS",
    "PlatformSupportReport",
    "assert_writable_workspace_allowed",
    "collect_platform_support_report",
    "detect_wsl_docker_backend",
    "detect_wsl_kernel_kind",
    "detect_wsl_package_version",
    "is_debian_stable",
    "is_ubuntu_lts",
    "is_wsl_drvfs_path",
    "require_supported_platform",
    "systemd_is_pid1",
]
