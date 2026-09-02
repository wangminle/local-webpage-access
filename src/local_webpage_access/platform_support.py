"""正式支持平台矩阵与运行时门禁（IMP-036）。

产品口径：仅正式支持 Linux 裸机（Ubuntu 22.04+ / Debian 12+ / Fedora 43+）、
WSL2 Linux、macOS；Windows 原生 hard fail；架构仅 x86_64/amd64 与 arm64/aarch64。

**import 本模块不得 sys.exit**；门禁仅由 CLI / 服务入口显式调用
:func:`require_supported_platform`。
"""

from __future__ import annotations

import os
import platform
import re
import shutil
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
MIN_FEDORA_VERSION = "43"
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
# Fedora 无代号配对，按大版本滚动窗口：当前 + 前一稳定版（截至 2026-09 为
# 44 现行 / 43 前一）；新版本发布后随本矩阵滚动更新，未纳入前明确拒绝。
SUPPORTED_FEDORA_RELEASES = frozenset({43, 44})
FEDORA_RAWHIDE_CODENAMES = frozenset({"rawhide"})
MIN_WSL_PACKAGE_VERSION = "2.1.5"
# 截至 2026-07：Docker Desktop「当前及前两版」→ macOS 14 Sonoma+
MACOS_MIN_MAJOR = 14
SUPPORTED_ARCHES = frozenset({"x86_64", "amd64", "aarch64", "arm64"})

_WINDOWS_ACTION = (
    "Windows 原生不受支持；请在 WSL2 的 Ubuntu 22.04+/Debian 12+/Fedora 43+ 中安装并运行 lwa"
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


def is_fedora_release(version: str, codename: str | None = None) -> bool:
    """仅允许 :data:`SUPPORTED_FEDORA_RELEASES` 内的大版本；拒绝 Rawhide。

    Fedora 的 ``VERSION_CODENAME`` 在正式发布上通常为空，Rawhide 则为
    ``Rawhide``——代号非空且命中 :data:`FEDORA_RAWHIDE_CODENAMES` 直接拒绝，
    其余按 ``VERSION_ID`` 大版本对照滚动窗口判定。
    """
    code = (codename or "").strip().lower()
    if code in FEDORA_RAWHIDE_CODENAMES:
        return False
    text = (version or "").strip()
    if not text:
        return False
    try:
        major = int(text.split(".")[0])
    except ValueError:
        return False
    return major in SUPPORTED_FEDORA_RELEASES


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
        conf = os.confstr("CS_GNU_LIBC_VERSION")
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


def _wsl_exe_candidates() -> list[str]:
    """guest 内调用 Windows 侧 ``wsl.exe`` 的候选路径。

    BUG-291：``appendWindowsPath=false`` 时 PATH 常无 ``wsl.exe``，但 DrvFS
    绝对路径 ``/mnt/c/Windows/System32/wsl.exe`` 在 interop 仍开启时往往可执行。
    仅依赖 PATH 名会把本可探测的环境误判为 ``unknown``。
    """
    found: list[str] = []
    which = shutil.which("wsl.exe")
    if which:
        found.append(which)
    # 扫描已挂载的 Windows 盘符（通常是 c；偶发 d 等）
    mnt = Path("/mnt")
    if mnt.is_dir():
        try:
            drives = sorted(p.name for p in mnt.iterdir() if p.is_dir())
        except OSError:
            drives = []
        for drive in drives:
            if len(drive) != 1 or not drive.isalpha():
                continue
            # 必须基于 mnt 拼接：单测可把 Path("/mnt") stub 到临时目录，
            # 硬编码 "/mnt/..." 字符串会绕过 stub 去查真实宿主。
            abs_exe = mnt / drive / "Windows" / "System32" / "wsl.exe"
            key = str(abs_exe)
            if key not in found and abs_exe.is_file():
                found.append(key)
    # 无任何候选时仍保留 PATH 名，便于注入 runner / 后续 PATH 变化
    if not found:
        found.append("wsl.exe")
    return found


def detect_wsl_package_version(
    runner: Callable[..., Any] = subprocess.run,
) -> str | None:
    """尝试读取 Windows 侧 WSL 包版本；失败返回 ``unknown``。

    BUG-282：在 guest 内调用 ``wsl.exe`` 时默认常为 UTF-16LE；以 bytes 捕获并
    双解码，同时注入 ``WSL_UTF8=1`` / ``WSLENV`` 请求 UTF-8 输出。

    BUG-291：除 PATH 外回退 ``/mnt/<drive>/Windows/System32/wsl.exe``。
    """
    if detect_platform() != PLATFORM_WSL:
        return None
    candidates: list[Sequence[str]] = []
    for exe in _wsl_exe_candidates():
        candidates.append((exe, "--version"))
        candidates.append((exe, "-v"))
    env = os.environ.copy()
    env["WSL_UTF8"] = "1"
    wslenv = env.get("WSLENV", "") or ""
    if "WSL_UTF8" not in wslenv.split(":"):
        env["WSLENV"] = f"{wslenv}:WSL_UTF8" if wslenv else "WSL_UTF8"

    for args in candidates:
        try:
            proc = runner(
                list(args),
                capture_output=True,
                text=False,
                timeout=5,
                check=False,
                env=env,
            )
        except TypeError:
            # BUG-288：注入的 runner 可能不接受 env=；降级重试而非当成 interop 失败
            try:
                proc = runner(
                    list(args),
                    capture_output=True,
                    text=False,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError, TypeError, ValueError):
                continue
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        try:
            text = _decode_wsl_cli_output(getattr(proc, "stdout", None))
            err = _decode_wsl_cli_output(getattr(proc, "stderr", None))
        except Exception:  # noqa: BLE001 — 解码失败继续下一候选
            continue
        blob = f"{text}\n{err}"
        parsed = _parse_wsl_package_version_text(blob)
        if parsed:
            return parsed
        # 回退兼容无 "WSL version" 标题的输出；但必须排除内核/WSLg 等同版面的
        # 其它组件版本号，否则会把 Kernel 5.15.x 当成包版本而绕过 ≥2.1.5 门禁
        # （BUG-288）。
        if getattr(proc, "returncode", 1) == 0:
            fallback = _first_version_outside_known_components(blob)
            if fallback:
                return fallback
    return "unknown"


def _decode_wsl_cli_output(raw: bytes | str | None) -> str:
    """解码 ``wsl.exe`` stdout/stderr（UTF-8 / UTF-16LE / 含 NUL 的误解码）。"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    data = bytes(raw)
    if not data:
        return ""
    if data.startswith(b"\xff\xfe"):
        text = data.decode("utf-16-le", errors="replace")
    elif data.startswith(b"\xfe\xff"):
        text = data.decode("utf-16-be", errors="replace")
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-16-le", errors="replace")
        else:
            # UTF-16LE ASCII 无 BOM 时常被 utf-8「成功」解出夹 NUL
            if "\x00" in text:
                text = data.decode("utf-16-le", errors="replace")
    return text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")


# ``wsl --version`` 同版面还会列出内核 / WSLg / MSRDC / Direct3D / Windows 版本；
# 这些行的版本号绝不可当作 WSL 包版本（BUG-288）。
_WSL_OTHER_COMPONENT_MARKERS = (
    "wslg",
    "kernel",
    "内核",
    "msrdc",
    "direct3d",
    "directx",
    "windows",
)


def _is_other_wsl_component_line(lower: str, line: str) -> bool:
    """该行是否属于 wsl --version 里的非「WSL 包」组件。"""
    if "内核" in line:
        return True
    return any(marker in lower for marker in _WSL_OTHER_COMPONENT_MARKERS)


def _parse_wsl_package_version_text(text: str) -> str | None:
    """从 ``wsl --version`` 文本提取 WSL 包版本（仅认含 WSL/版本 的包版本行）。"""
    for line in (text or "").splitlines():
        lower = line.lower()
        if "wsl" in lower and ("version" in lower or "版本" in line):
            if _is_other_wsl_component_line(lower, line):
                continue
            match = re.search(r"(\d+\.\d+\.\d+(?:\.\d+)?)", line)
            if match:
                return match.group(1)
    return None


def _first_version_outside_known_components(text: str) -> str | None:
    """在排除内核 / WSLg 等组件行后取第一个版本号；全被排除则返回 None。"""
    for line in (text or "").splitlines():
        if _is_other_wsl_component_line(line.lower(), line):
            continue
        match = re.search(r"(\d+\.\d+\.\d+(?:\.\d+)?)", line)
        if match:
            return match.group(1)
    return None


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
        distro_codename = os_rel.get("VERSION_CODENAME") or os_rel.get("UBUNTU_CODENAME") or None

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
            _detect_systemd_available() if plat in (PLATFORM_LINUX, PLATFORM_WSL) else False
        )
    if systemd_pid1 is None:
        systemd_pid1 = systemd_is_pid1() if plat in (PLATFORM_LINUX, PLATFORM_WSL) else False

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
    wsl_pkg_unknown = False

    if plat == PLATFORM_WINDOWS:
        reasons.append("检测到 Windows 原生进程，正式支持范围不包含 Windows 原生")
        report.reasons = reasons
        report.action = _WINDOWS_ACTION
        return report

    if plat == PLATFORM_UNKNOWN:
        reasons.append("无法识别操作系统，无法证明满足正式支持矩阵")
        report.reasons = reasons
        report.action = "请在 Ubuntu 22.04+/Debian 12+/Fedora 43+/WSL2 或 macOS 14+ 上运行"
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
        report.action = "请使用 Ubuntu/Debian/Fedora 裸机、WSL2 或 macOS"
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
                + "/".join(f"{ver}={code}" for ver, code in sorted(SUPPORTED_UBUNTU_LTS.items()))
                + "）"
            )
    elif did == "debian":
        if not is_debian_stable(dver, dcode):
            if dcode and dcode in DEBIAN_UNSTABLE_CODENAMES:
                reasons.append(
                    f"Debian 代号 {dcode} 不是 Stable（仅支持 "
                    + "/".join(
                        f"{maj}={code}" for maj, code in sorted(SUPPORTED_DEBIAN_STABLE.items())
                    )
                    + "）"
                )
            else:
                reasons.append(
                    f"Debian {dver or 'unknown'}"
                    + (f"（{dcode}）" if dcode else "")
                    + " 不在正式支持矩阵或版本/代号不匹配（仅 "
                    + "/".join(
                        f"{maj}={code}" for maj, code in sorted(SUPPORTED_DEBIAN_STABLE.items())
                    )
                    + "）"
                )
    elif did == "fedora":
        if not is_fedora_release(dver, dcode):
            if dcode and dcode in FEDORA_RAWHIDE_CODENAMES:
                reasons.append("Fedora Rawhide 不是正式发布，不在支持矩阵")
            else:
                reasons.append(
                    f"Fedora {dver or 'unknown'}"
                    + (f"（{dcode}）" if dcode else "")
                    + " 不在正式支持矩阵（当前 "
                    + "/".join(str(m) for m in sorted(SUPPORTED_FEDORA_RELEASES))
                    + "；新版本发布后随矩阵滚动更新）"
                )
    else:
        reasons.append(
            f"发行版 {did or 'unknown'} 不在正式支持矩阵（仅 Ubuntu LTS / Debian Stable / Fedora）"
        )

    if not kernel_version or not version_ge(kernel_version, MIN_KERNEL_VERSION):
        reasons.append(f"内核 {kernel_version or 'unknown'} 低于最低要求 {MIN_KERNEL_VERSION}")

    if not libc_version or not version_ge(libc_version, MIN_GLIBC_VERSION):
        reasons.append(f"glibc {libc_version or 'unknown'} 低于最低要求 {MIN_GLIBC_VERSION}")

    if plat == PLATFORM_LINUX:
        if not systemd_available:
            reasons.append("systemd 不可用（需要 systemctl 与 user manager）")
    elif plat == PLATFORM_WSL:
        if kernel_kind == "1":
            reasons.append("WSL1 不受支持；请升级到 WSL2")
        pkg = wsl_package_version
        if pkg is None or pkg == "unknown" or pkg == "":
            wsl_pkg_unknown = True
            reasons.append(
                "无法确定 WSL 包版本（wslVersion=unknown）；"
                f"写操作 fail-closed，请在 Windows 侧执行 wsl --version"
                f"（需 ≥ {MIN_WSL_PACKAGE_VERSION}）"
            )
        elif not version_ge(pkg, MIN_WSL_PACKAGE_VERSION):
            reasons.append(f"WSL 包版本 {pkg} 低于最低要求 {MIN_WSL_PACKAGE_VERSION}")
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
        if wsl_pkg_unknown:
            # BUG-288：包版本 unknown 通常是 guest 内读不到 Windows 侧（interop
            # 被禁用），而非「版本过低」；别把用户引向无谓的升级。
            report.action = (
                "无法在 WSL 内读取 Windows 侧 WSL 包版本："
                "请在 Windows PowerShell 执行 wsl --version 确认 ≥ "
                f"{MIN_WSL_PACKAGE_VERSION}；并检查 /etc/wsl.conf 的 "
                "[interop] enabled=true（彻底关闭 interop 时连 "
                "/mnt/c/Windows/System32/wsl.exe 也无法执行）。"
                "若仅关掉 appendWindowsPath，本工具会回退绝对路径探测"
            )
        else:
            report.action = (
                "请使用 WSL2（包 ≥ "
                f"{MIN_WSL_PACKAGE_VERSION}）+ Ubuntu 22.04 LTS+/Debian 12+ Stable/Fedora 43+，"
                "启用 systemd；Full/autostart 工作区请放在 Linux 文件系统"
            )
    else:
        report.action = (
            "请使用 Ubuntu LTS（"
            + "/".join(sorted(SUPPORTED_UBUNTU_LTS))
            + "）、Debian Stable（"
            + "/".join(sorted(SUPPORTED_DEBIAN_STABLE))
            + "）或 Fedora（"
            + "/".join(str(m) for m in sorted(SUPPORTED_FEDORA_RELEASES))
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
    "FEDORA_RAWHIDE_CODENAMES",
    "MACOS_MIN_MAJOR",
    "MIN_DEBIAN_VERSION",
    "MIN_FEDORA_VERSION",
    "MIN_GLIBC_VERSION",
    "MIN_KERNEL_VERSION",
    "MIN_UBUNTU_VERSION",
    "MIN_WSL_PACKAGE_VERSION",
    "SUPPORTED_ARCHES",
    "SUPPORTED_DEBIAN_MAJORS",
    "SUPPORTED_DEBIAN_STABLE",
    "SUPPORTED_FEDORA_RELEASES",
    "SUPPORTED_UBUNTU_LTS",
    "PlatformSupportReport",
    "assert_writable_workspace_allowed",
    "collect_platform_support_report",
    "detect_wsl_docker_backend",
    "detect_wsl_kernel_kind",
    "detect_wsl_package_version",
    "is_debian_stable",
    "is_fedora_release",
    "is_ubuntu_lts",
    "is_wsl_drvfs_path",
    "require_supported_platform",
    "systemd_is_pid1",
]
