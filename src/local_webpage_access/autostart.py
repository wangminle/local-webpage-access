"""跨平台开机自启动（IMP-030）。

提供 ``lwa autostart`` 子命令组的核心能力：在 macOS（launchd LaunchAgent）与
Linux/WSL（systemd user unit）上**直接监管前台进程**（修复 BUG-138：旧方案把
``lwa daemon/manager on`` 这种"快速返回的 detached 启动器"当作 ``ExecStart``，
``Restart=on-failure`` 监管的是秒退的 CLI，而非真实 watcher/uvicorn）。

设计要点（见 ``design/plan/local-webpage-access-新增功能点2607.md`` §10）：

* 监管对象是前台模块入口 ``python -m local_webpage_access.<daemon|manager_service|
  gateway_service> --workspace <abs>``（030.a）；
* macOS plist 固化 ``EnvironmentVariables.PATH``（含 Homebrew）与可选 caddy 绝对
  路径，并对前台进程启用 ``KeepAlive``（修复 BUG-139）；
* ``lwa X off`` 与自启动 ``disable`` 协调（030.b）：off 前先卸载单元，避免
  KeepAlive/Restart 立刻把进程拉回；
* ``check`` 给出完备性清单（§10.5），任一 fail → 非零退出码；
* WSL 额外产出 Windows 唤醒脚本与待办清单（发行版生命周期由 Windows 决定）。

子进程调用（launchctl/systemctl/loginctl）经可注入 ``runner`` 执行，便于单测用
假命令验证调用序列。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from local_webpage_access.config import Config
from local_webpage_access.doctor import STATUS_FAIL, STATUS_OK, STATUS_WARN
from local_webpage_access.paths import Workspace
from local_webpage_access.platform_detect import (
    PLATFORM_LINUX,
    PLATFORM_MACOS,
    PLATFORM_WSL,
    detect_platform,
    systemd_available,
    wsl_distro,
)

# ---- 常量 -------------------------------------------------------------------

LAUNCHD_LABEL_PREFIX = "com.fenix.lwa"
SYSTEMD_UNIT_PREFIX = "lwa"
# 服务名 → 前台模块（python -m <module> --workspace <root>）。
SERVICE_MODULES: dict[str, str] = {
    "daemon": "local_webpage_access.daemon",
    "manager": "local_webpage_access.manager_service",
    "gateway": "local_webpage_access.gateway_service",
}
ALL_SERVICES = ("daemon", "manager", "gateway")

# launchd/systemd 环境里默认没有交互式 PATH，补上 Homebrew 与系统目录，保证
# gateway_service 能通过 shutil.which 找到 caddy（BUG-139）。
_DEFAULT_PATH_DIRS = (
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)

# 退出码：0 完备；1 配置/运行不完备；2 平台不支持/前置缺失。
EXIT_OK = 0
EXIT_INCOMPLETE = 1
EXIT_UNSUPPORTED = 2


class SubprocessRunner(Protocol):
    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess: ...


def _default_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """真实子进程执行（默认）。``capture`` 时捕获 stdout/stderr。"""
    kwargs.setdefault("text", True)
    return subprocess.run(cmd, **kwargs)  # noqa: S603


# ---- 服务选择 --------------------------------------------------------------


def select_services(config: Config, *, with_caddy: bool) -> list[str]:
    """根据配置决定要监管的服务列表（顺序：daemon → manager → gateway）。"""
    services = ["daemon"]
    if config.managerEnabled:
        services.append("manager")
    if with_caddy and config.staticGateway == "caddy":
        services.append("gateway")
    return services


def _build_path_env(extra_caddy_dir: str | None = None) -> str:
    """构造单元环境 PATH：去重保留默认目录 + 当前 PATH 中找到 caddy 的目录。"""
    seen: list[str] = []
    for d in _DEFAULT_PATH_DIRS:
        if d not in seen:
            seen.append(d)
    if extra_caddy_dir and extra_caddy_dir not in seen:
        seen.insert(0, extra_caddy_dir)
    # 追加当前进程 PATH 中已有、但不在默认集合里的目录（用户自装工具）。
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d and d not in seen:
            seen.append(d)
    return os.pathsep.join(seen)


def _resolve_caddy_dir() -> str | None:
    """caddy 所在目录（用于固化绝对路径依赖）；未找到返回 None。"""
    caddy = shutil.which("caddy")
    if caddy:
        return str(Path(caddy).resolve().parent)
    return None


# ---- 单元名 / 路径 ----------------------------------------------------------


def launchd_label(name: str) -> str:
    return f"{LAUNCHD_LABEL_PREFIX}.{name}"


def launchd_plist_path(name: str, dest_dir: Path | None = None) -> Path:
    dest = dest_dir or (Path.home() / "Library" / "LaunchAgents")
    return dest / f"{launchd_label(name)}.plist"


def systemd_unit_name(name: str) -> str:
    return f"{SYSTEMD_UNIT_PREFIX}-{name}.service"


def systemd_unit_path(name: str, dest_dir: Path | None = None) -> Path:
    dest = dest_dir or (Path.home() / ".config" / "systemd" / "user")
    return dest / systemd_unit_name(name)


# ---- 旧配置识别（030.g）----------------------------------------------------


def is_legacy_program_arguments(args: list[str]) -> bool:
    """旧 detached 启动器特征：末尾为 ``<service> on`` 且无 ``--workspace``。"""
    if "--workspace" in args:
        return False
    return len(args) >= 2 and args[-1] == "on" and args[-2] in ALL_SERVICES


def is_legacy_exec_start(exec_start: str) -> bool:
    """systemd ExecStart 单行字符串的旧配置识别。"""
    try:
        tokens = shlex.split(exec_start)
    except ValueError:
        return False
    return is_legacy_program_arguments(tokens)


# ---- 单元内容生成 -----------------------------------------------------------


def _foreground_program_arguments(
    name: str, python_exe: str, workspace_root: Path
) -> list[str]:
    """前台监管命令：``python -m <module> --workspace <root>``。"""
    return [
        python_exe,
        "-m",
        SERVICE_MODULES[name],
        "--workspace",
        str(workspace_root),
    ]


def build_launchd_plist(
    name: str,
    *,
    python_exe: str,
    workspace_root: Path,
    caddy_dir: str | None = None,
    keep_alive: bool = True,
) -> dict[str, Any]:
    """构造单个 LaunchAgent plist 字典（前台监管 + PATH + KeepAlive）。"""
    logs = workspace_root / "logs"
    plist: dict[str, Any] = {
        "Label": launchd_label(name),
        "ProgramArguments": _foreground_program_arguments(
            name, python_exe, workspace_root
        ),
        "WorkingDirectory": str(workspace_root),
        "EnvironmentVariables": {"PATH": _build_path_env(caddy_dir)},
        "RunAtLoad": True,
        "StandardOutPath": str(logs / f"launchd-{name}.out"),
        "StandardErrorPath": str(logs / f"launchd-{name}.err"),
    }
    if keep_alive:
        # 前台进程崩溃后由 launchd 拉起（修复 BUG-138）。与 ``lwa X off`` 的冲突由
        # ``coordinated_disable`` 先 bootout 单元解决（030.b）。
        plist["KeepAlive"] = {"SuccessfulExit": False}
        plist["ThrottleInterval"] = 10
    return plist


def build_systemd_unit(
    name: str,
    *,
    python_exe: str,
    workspace_root: Path,
    caddy_dir: str | None = None,
) -> str:
    """构造单个 systemd user unit 内容（Type=simple 前台 + Restart + PATH）。"""
    args = _foreground_program_arguments(name, python_exe, workspace_root)
    exec_start = " ".join(shlex.quote(a) for a in args)
    after = "network-online.target"
    if name == "manager":
        after += " lwa-daemon.service"
    lines = [
        "[Unit]",
        f"Description=lwa {name} (foreground, supervised)",
        f"After={after}",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={workspace_root}",
        f"Environment=PATH={_build_path_env(caddy_dir)}",
        f"ExecStart={exec_start}",
        "Restart=on-failure",
        "RestartSec=5",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


# ---- 后端抽象 --------------------------------------------------------------


class AutostartError(Exception):
    """自启动配置不可恢复错误（平台不支持 / 前置缺失）。"""


@dataclass
class CmdOutcome:
    """单条子命令执行结果（用于报告与测试断言）。"""

    cmd: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class AutostartBackend:
    """平台后端：生成单元文件 + enable/disable/uninstall/status。"""

    platform: str = ""

    def unit_path(self, name: str) -> Path:  # pragma: no cover - 抽象
        raise NotImplementedError

    def render(self, name: str, **kwargs: Any) -> str | bytes:  # pragma: no cover
        raise NotImplementedError

    def write_unit(self, name: str, content: str | bytes) -> Path:
        path = self.unit_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def is_legacy(self, name: str) -> bool:
        path = self.unit_path(name)
        if not path.is_file():
            return False
        return self._content_is_legacy(path)

    # 以下操作默认透传 runner；子类可覆盖命令构造。
    def enable(self, name: str, runner: SubprocessRunner) -> tuple[list[CmdOutcome], bool]:  # pragma: no cover
        raise NotImplementedError

    def disable(self, name: str, runner: SubprocessRunner) -> tuple[list[CmdOutcome], bool]:  # pragma: no cover
        raise NotImplementedError

    def uninstall(self, name: str, runner: SubprocessRunner) -> tuple[list[CmdOutcome], bool]:  # pragma: no cover
        raise NotImplementedError

    def is_loaded(self, name: str, runner: SubprocessRunner) -> bool:  # pragma: no cover
        raise NotImplementedError

    def is_enabled(self, name: str, runner: SubprocessRunner) -> bool:  # pragma: no cover
        """是否被服务管理器标记为启用（区别于 is_loaded 的"当前已加载/激活"）。"""
        raise NotImplementedError

    def main_pid(self, name: str, runner: SubprocessRunner) -> int | None:  # pragma: no cover
        """监管对象的主 PID（用于校验进程确实由本单元拉起）。"""
        raise NotImplementedError

    def _content_is_legacy(self, path: Path) -> bool:  # pragma: no cover
        raise NotImplementedError


class MacLaunchdBackend(AutostartBackend):
    platform = PLATFORM_MACOS

    def unit_path(self, name: str) -> Path:
        return launchd_plist_path(name)

    def render(self, name: str, **kwargs: Any) -> bytes:
        import plistlib

        plist = build_launchd_plist(name, **kwargs)
        return plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=False)

    def _content_is_legacy(self, path: Path) -> bool:
        import plistlib

        try:
            data = plistlib.loads(path.read_bytes())
        except Exception:  # noqa: BLE001
            return False
        return is_legacy_program_arguments(list(data.get("ProgramArguments", [])))

    def _domain(self) -> str:
        return f"gui/{os.getuid()}"

    def _target(self, name: str) -> str:
        return f"{self._domain()}/{launchd_label(name)}"

    def enable(self, name: str, runner: SubprocessRunner) -> tuple[list[CmdOutcome], bool]:
        path = self.unit_path(name)
        domain = self._domain()
        target = self._target(name)
        # 清除历史持久 disable，再清旧实例，最后 bootstrap（登录即随 LaunchAgent 加载）。
        ren = runner(["launchctl", "enable", target], capture_output=True)
        boot = runner(["launchctl", "bootout", target], capture_output=True)
        bsp = runner(["launchctl", "bootstrap", domain, str(path)], capture_output=True)
        outcomes = [
            CmdOutcome(["launchctl", "enable", target], ren.returncode,
                       ren.stdout or "", ren.stderr or ""),
            CmdOutcome(["launchctl", "bootout", target], boot.returncode,
                       boot.stdout or "", boot.stderr or ""),
            CmdOutcome(["launchctl", "bootstrap", domain, str(path)], bsp.returncode,
                       bsp.stdout or "", bsp.stderr or ""),
        ]
        # bootout 对"可能不存在的旧实例"非零属预期；成败看 enable+bootstrap 与
        # 执行后 loaded+enabled（BUG-152）。
        ok = (
            ren.returncode == 0
            and bsp.returncode == 0
            and self.is_loaded(name, runner)
            and self.is_enabled(name, runner)
        )
        return outcomes, ok

    def disable(self, name: str, runner: SubprocessRunner) -> tuple[list[CmdOutcome], bool]:
        target = self._target(name)
        # launchctl disable 持久化禁用（下次登录不再自动加载），bootout 立即停止。
        dis = runner(["launchctl", "disable", target], capture_output=True)
        res = runner(["launchctl", "bootout", target], capture_output=True)
        outcomes = [
            CmdOutcome(["launchctl", "disable", target], dis.returncode,
                       dis.stdout or "", dis.stderr or ""),
            CmdOutcome(["launchctl", "bootout", target], res.returncode,
                       res.stdout or "", res.stderr or ""),
        ]
        # bootout 对未加载单元非零可忽略；必须 disable 成功且最终未启用（BUG-152）。
        ok = dis.returncode == 0 and not self.is_enabled(name, runner)
        return outcomes, ok

    def uninstall(self, name: str, runner: SubprocessRunner) -> tuple[list[CmdOutcome], bool]:
        outcomes, disabled_ok = self.disable(name, runner)
        if not disabled_ok:
            # disable 失败则保留 plist，便于重试清理（BUG-158）。
            return outcomes, False
        target = self._target(name)
        # 先确认已 bootout，再清持久 disable 覆盖并删 plist。
        # 不可在 enable 清覆盖后再用 is_loaded 判定——假 runner / 探测噪声会假阳，
        # 导致文件已删却报失败，上层 install 又把孤儿写回（BUG-163）。
        if self.is_loaded(name, runner):
            return outcomes, False
        runner(["launchctl", "enable", target], capture_output=True)
        path = self.unit_path(name)
        if path.is_file():
            path.unlink()
        return outcomes, not path.is_file()

    def is_loaded(self, name: str, runner: SubprocessRunner) -> bool:
        res = runner(["launchctl", "print", self._target(name)], capture_output=True)
        return res.returncode == 0

    def is_enabled(self, name: str, runner: SubprocessRunner) -> bool:
        """已加载时看 print 的 disabled 字段；未加载时查 print-disabled（BUG-153）。"""
        import re

        target = self._target(name)
        res = runner(["launchctl", "print", target], capture_output=True)
        if res.returncode == 0:
            return "disabled = true" not in (res.stdout or "")
        # 未加载：读 gui/$UID 的持久 disable 覆盖表。
        domain = self._domain()
        dis = runner(["launchctl", "print-disabled", domain], capture_output=True)
        if dis.returncode != 0:
            # 无法探测时保守：有单元文件则视为仍可能启用。
            return self.unit_path(name).is_file()
        label = launchd_label(name)
        out = dis.stdout or ""
        # `"com.fenix.lwa.daemon" => true` 表示被持久 disable。
        m = re.search(
            rf'"{re.escape(label)}"\s*=>\s*(true|false)', out
        )
        if m:
            return m.group(1) == "false"
        # 未出现在 disabled 列表 → 未被持久 disable，视为仍启用（BUG-153）。
        return True

    def main_pid(self, name: str, runner: SubprocessRunner) -> int | None:
        import re

        res = runner(["launchctl", "print", self._target(name)], capture_output=True)
        m = re.search(r"pid\s*=\s*(\d+)", res.stdout or "")
        return int(m.group(1)) if m else None


class SystemdUserBackend(AutostartBackend):
    platform = PLATFORM_LINUX  # WSL 复用

    def unit_path(self, name: str) -> Path:
        return systemd_unit_path(name)

    def render(self, name: str, **kwargs: Any) -> str:
        return build_systemd_unit(name, **kwargs)

    def _content_is_legacy(self, path: Path) -> bool:
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("ExecStart="):
                return is_legacy_exec_start(line[len("ExecStart="):])
        return False

    def enable(self, name: str, runner: SubprocessRunner) -> tuple[list[CmdOutcome], bool]:
        unit = systemd_unit_name(name)
        reload_ = runner(["systemctl", "--user", "daemon-reload"], capture_output=True)
        enable = runner(
            ["systemctl", "--user", "enable", "--now", unit], capture_output=True
        )
        outcomes = [
            CmdOutcome(["systemctl", "--user", "daemon-reload"], reload_.returncode,
                       reload_.stdout or "", reload_.stderr or ""),
            CmdOutcome(["systemctl", "--user", "enable", "--now", unit], enable.returncode,
                       enable.stdout or "", enable.stderr or ""),
        ]
        # 须命令成功且最终既 active 又 enabled（BUG-152）；不可仅凭 is-active。
        ok = (
            reload_.returncode == 0
            and enable.returncode == 0
            and self.is_loaded(name, runner)
            and self.is_enabled(name, runner)
        )
        return outcomes, ok

    def disable(self, name: str, runner: SubprocessRunner) -> tuple[list[CmdOutcome], bool]:
        unit = systemd_unit_name(name)
        res = runner(
            ["systemctl", "--user", "disable", "--now", unit], capture_output=True
        )
        outcomes = [CmdOutcome(["systemctl", "--user", "disable", "--now", unit],
                               res.returncode, res.stdout or "", res.stderr or "")]
        # 须命令成功且最终既非 active 也非 enabled（BUG-152）。
        ok = (
            res.returncode == 0
            and not self.is_loaded(name, runner)
            and not self.is_enabled(name, runner)
        )
        return outcomes, ok

    def uninstall(self, name: str, runner: SubprocessRunner) -> tuple[list[CmdOutcome], bool]:
        outcomes, disabled_ok = self.disable(name, runner)
        if not disabled_ok:
            # disable 失败则保留 unit 文件，便于重试（BUG-158）。
            return outcomes, False
        path = self.unit_path(name)
        # reload 失败时须恢复 unit，否则 installed_services 过滤掉缺失文件，
        # 第二次 uninstall 不再执行 reload 却假报成功（BUG-161）。
        saved: bytes | None = None
        if path.is_file():
            saved = path.read_bytes()
            path.unlink()
        # 先删单元文件，再 daemon-reload 让 systemd 忘掉已删除的单元。
        # reload 结果必须收集并计入成败（BUG-144）。
        rel = runner(["systemctl", "--user", "daemon-reload"], capture_output=True)
        outcomes.append(CmdOutcome(
            ["systemctl", "--user", "daemon-reload"], rel.returncode,
            rel.stdout or "", rel.stderr or ""))
        if rel.returncode != 0:
            if saved is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(saved)
            return outcomes, False
        return outcomes, (
            not path.is_file()
            and not self.is_loaded(name, runner)
        )

    def is_loaded(self, name: str, runner: SubprocessRunner) -> bool:
        unit = systemd_unit_name(name)
        res = runner(["systemctl", "--user", "is-active", unit], capture_output=True)
        return res.returncode == 0

    def is_enabled(self, name: str, runner: SubprocessRunner) -> bool:
        unit = systemd_unit_name(name)
        res = runner(["systemctl", "--user", "is-enabled", unit], capture_output=True)
        out = (res.stdout or "").strip()
        # static 无 enable 链接，不能保证随 default.target 自启（BUG-165）。
        return res.returncode == 0 and out in ("enabled", "enabled-runtime")

    def main_pid(self, name: str, runner: SubprocessRunner) -> int | None:
        unit = systemd_unit_name(name)
        res = runner(
            ["systemctl", "--user", "show", unit, "--value", "-p", "MainPID"],
            capture_output=True,
        )
        try:
            pid = int((res.stdout or "0").strip())
        except ValueError:
            return None
        return pid or None


def select_backend(plat: str | None = None) -> AutostartBackend:
    """按平台选择后端；不支持的平台抛 :class:`AutostartError`。"""
    plat = plat or detect_platform()
    if plat == PLATFORM_MACOS:
        return MacLaunchdBackend()
    if plat in (PLATFORM_LINUX, PLATFORM_WSL):
        if not systemd_available():
            raise AutostartError(
                "当前 Linux/WSL 未检测到可用的 systemd user manager；"
                "请确认 systemd 已启用（WSL 需 /etc/wsl.conf 设置 [boot] systemd=true）"
            )
        return SystemdUserBackend()
    raise AutostartError(
        f"自启动暂不支持平台 {plat!r}（仅 macOS / Linux / WSL；Windows 请用任务计划程序，"
        "参考 docs/autostart.md）"
    )


# ---- 安装参数封装 ----------------------------------------------------------


@dataclass
class UnitGen:
    """单条单元生成结果。"""

    name: str
    path: Path
    legacy: bool = False


@dataclass
class InstallResult:
    platform: str
    services: list[str]
    written: list[UnitGen] = field(default_factory=list)
    legacy_detected: list[str] = field(default_factory=list)
    enabled: bool = False
    enable_ok: bool = True
    enable_outcomes: list[CmdOutcome] = field(default_factory=list)
    linger_attempted: bool = False
    linger_ok: bool = False
    wsl_windows_script: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class OpResult:
    """enable/disable/uninstall 的聚合结果（含真实成败，供 CLI 决定退出码）。"""

    outcomes: list[CmdOutcome] = field(default_factory=list)
    success: bool = True


# ---- 已安装服务清单（manifest）---------------------------------------------
# install 写入 run/autostart.json 记录实际安装的服务；enable/disable/uninstall/
# check 以"实际已安装"为准，而非按当前配置推导，避免配置漂移导致集合不一致（BUG-149）。
MANIFEST_FILENAME = "autostart.json"


def _manifest_path(ws: Workspace) -> Path:
    return ws.run / MANIFEST_FILENAME


def write_manifest(ws: Workspace, services: list[str]) -> None:
    import json

    p = _manifest_path(ws)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"version": 1, "services": list(services)}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )


def read_manifest(ws: Workspace) -> list[str] | None:
    import json

    p = _manifest_path(ws)
    if not p.is_file():
        return None
    try:
        return list(json.loads(p.read_text(encoding="utf-8")).get("services", []))
    except Exception:  # noqa: BLE001
        return None


def _clear_manifest(ws: Workspace) -> None:
    p = _manifest_path(ws)
    if p.is_file():
        p.unlink()


def installed_services(ws: Workspace, backend: AutostartBackend | None = None) -> list[str]:
    """实际已安装的服务：manifest 优先，缺失（旧版/手装）则扫描磁盘单元文件。"""
    if backend is None:
        backend = select_backend()
    mani = read_manifest(ws)
    candidates = mani if mani is not None else list(ALL_SERVICES)
    return [s for s in ALL_SERVICES if s in candidates and backend.unit_path(s).is_file()]


def _install_render_kwargs(
    name: str, python_exe: str, ws_root: Path, caddy_dir: str | None
) -> dict[str, Any]:
    if detect_platform() == PLATFORM_MACOS:
        return {
            "python_exe": python_exe,
            "workspace_root": ws_root,
            "caddy_dir": caddy_dir,
            "keep_alive": True,
        }
    return {
        "python_exe": python_exe,
        "workspace_root": ws_root,
        "caddy_dir": caddy_dir,
    }


def _prepare_daemon_for_supervision(ws: Workspace) -> None:
    """置 ``enabled=True`` 但**保留现有 pid**（BUG-146：不可覆盖为 0）。

    前台 ``_main()`` 读 ``run/daemon.json`` 的 ``enabled`` 决定是否运行 watcher。
    若已有 detached daemon 在跑（enabled=True 且有 pid），完全不动——避免把真实
    pid 覆盖成 0，破坏 ``is_running`` 与 ``lwa daemon off``。前台 ``_main()`` 抢到
    锁后会回写自身 pid（见 daemon._main）。受控迁移（停 detached、让监管进程接管）
    由 :func:`enable` 负责，而非安装期。
    """
    from local_webpage_access import daemon as daemon_mod

    try:
        prev = daemon_mod.read_state(ws)
        if prev and prev.enabled and prev.pid:
            return  # detached 已在跑：保留其 pid，不动
        poll = prev.poll_interval if prev else daemon_mod.DEFAULT_POLL_INTERVAL
        daemon_mod.write_state(
            ws,
            daemon_mod.DaemonState(
                enabled=True,
                poll_interval=poll,
                pid=prev.pid if prev else None,
            ),
        )
    except Exception:  # noqa: BLE001
        pass


def _migrate_detached_for_supervision(
    ws: Workspace, config: Config, name: str
) -> bool:
    """受控迁移（BUG-146/147）：停掉 detached 进程，让监管进程持锁/绑端口。

    仅在对应 detached 服务确实在跑时停止它——监管单元随后启动的前台入口会抢锁/
    绑端口并回写自身 pid。gateway 由 ``start_gateway`` 幂等管理，无需迁移（恒 True）。

    返回 ``False`` 表示存在 detached 进程但未能停止（旧锁/端口未释放），此时监管
    入口无法真正接管——调用方应判为 enable 失败，而非静默继续（BUG-146）。
    """
    if name not in ("daemon", "manager"):
        return True
    try:
        if name == "daemon":
            from local_webpage_access import daemon as daemon_mod

            if not daemon_mod.is_running(ws):  # type: ignore[attr-defined]
                return True
            return bool(daemon_mod.stop_daemon(ws))
        from local_webpage_access.manager_service import (
            manager_status,
            stop_manager,
        )

        st = manager_status(ws, config)
        if not (isinstance(st, dict) and st.get("running")):
            return True
        return bool(stop_manager(ws))
    except Exception:  # noqa: BLE001 — 探测/停止异常同样无法保证接管
        return False


def install(
    ws: Workspace,
    config: Config,
    *,
    with_caddy: bool = False,
    enable: bool = True,
    linger: bool = False,
    python_exe: str | None = None,
    runner: SubprocessRunner = _default_runner,
) -> InstallResult:
    """生成（并可选启用）自启动单元。"""
    plat = detect_platform()
    backend = select_backend(plat)
    services = select_services(config, with_caddy=with_caddy)
    py = python_exe or sys.executable
    caddy_dir = _resolve_caddy_dir() if config.staticGateway == "caddy" else None
    ws_root = ws.root

    written: list[UnitGen] = []
    legacy_detected: list[str] = []
    # 缩减服务集合时差量卸载被移除的单元，避免 manifest 外孤儿（BUG-163）。
    prev_installed = installed_services(ws, backend)
    orphans = [s for s in prev_installed if s not in services]
    orphan_outcomes: list[CmdOutcome] = []
    orphan_ok = True
    for name in orphans:
        outs, _ok = backend.uninstall(name, runner)
        orphan_outcomes.extend(outs)
        # 以单元文件是否仍在为准：文件已删即视为清理成功（BUG-163）。
        if backend.unit_path(name).is_file():
            orphan_ok = False
            if name not in services:
                services = list(services) + [name]

    for name in services:
        legacy = backend.is_legacy(name)
        if legacy:
            legacy_detected.append(name)
        kwargs = _install_render_kwargs(name, py, ws_root, caddy_dir)
        content = backend.render(name, **kwargs)
        path = backend.write_unit(name, content)
        written.append(UnitGen(name=name, path=path, legacy=legacy))

    (ws_root / "logs").mkdir(parents=True, exist_ok=True)
    # 记录实际安装的服务清单，供 enable/disable/uninstall/check 以"已安装"为准（BUG-149）。
    # 去重并保持 ALL_SERVICES 顺序。
    services = [s for s in ALL_SERVICES if s in services]
    write_manifest(ws, services)

    result = InstallResult(
        platform=plat,
        services=services,
        written=written,
        legacy_detected=legacy_detected,
    )
    result.enable_outcomes.extend(orphan_outcomes)
    if orphans and not orphan_ok:
        result.notes.append(
            "⚠️ 缩减安装时未能卸载旧服务："
            + ", ".join(s for s in orphans if s in services)
            + "；请手动 lwa autostart uninstall 后重试"
        )

    if enable:
        result.enabled = True
        all_ok = orphan_ok
        # 仅在真正启用时置 daemon enabled；--no-enable 不得污染运行意图（BUG-160）。
        if "daemon" in services:
            _prepare_daemon_for_supervision(ws)
        for name in services:
            # 受控迁移：停 detached 进程，让监管进程持锁/绑端口并回写自身 pid（BUG-146/147）。
            # 迁移失败（旧进程未停）必须计入失败，且不得再加载单元（BUG-146/159）。
            if not _migrate_detached_for_supervision(ws, config, name):
                all_ok = False
                result.enable_outcomes.append(CmdOutcome(
                    ["(migrate)", name], 1, "",
                    f"detached {name} 未能停止，监管进程可能无法持锁/绑端口"))
                result.notes.append(
                    f"⚠️ 迁移 detached {name} 失败（旧进程未停止）；"
                    "请手动停掉旧进程后重试 lwa autostart install/enable"
                )
                continue
            outs, ok = backend.enable(name, runner)
            result.enable_outcomes.extend(outs)
            all_ok = all_ok and ok
        result.enable_ok = all_ok
    elif not orphan_ok:
        result.enable_ok = False

    # WSL：生成 Windows 侧唤醒脚本（发行版不会随 Windows 开机自动起）。
    if plat == PLATFORM_WSL:
        result.wsl_windows_script = render_wsl_windows_script(ws, config)
        result.notes.append(
            "WSL：发行版不会随 Windows 开机自动启动，需在 Windows 侧注册登录任务"
            "（见下方脚本或 docs/autostart.md）运行本工具。"
        )

    if linger and plat in (PLATFORM_LINUX, PLATFORM_WSL):
        result.linger_attempted = True
        result.linger_ok = enable_linger(runner=runner)
        if not result.linger_ok:
            result.notes.append(
                "enable-linger 失败（可能需要 polkit/管理员）；登出后 user 服务会停止，"
                "请手动执行：sudo loginctl enable-linger $USER"
            )

    if caddy_dir is None and config.staticGateway == "caddy":
        result.notes.append(
            "未在 PATH 中找到 caddy；已写入 PATH 环境变量，但若 caddy 装在非标准目录，"
            "gateway 监管进程可能起不来（BUG-139）。请确认 caddy 可被监管环境的 PATH 命中。"
        )
    return result


# ---- enable / disable / uninstall / status --------------------------------


def enable(
    ws: Workspace, config: Config, *, runner: SubprocessRunner = _default_runner
) -> OpResult:
    """启用**实际已安装**的单元，并做受控迁移；返回含真实成败的 :class:`OpResult`。"""
    backend = select_backend()
    services = installed_services(ws, backend)
    outcomes: list[CmdOutcome] = []
    all_ok = True
    if "daemon" in services:
        _prepare_daemon_for_supervision(ws)
    for name in services:
        if not _migrate_detached_for_supervision(ws, config, name):
            all_ok = False
            outcomes.append(CmdOutcome(
                ["(migrate)", name], 1, "",
                f"detached {name} 未能停止，监管进程可能无法持锁/绑端口"))
            # 迁移失败不得再加载单元，否则监管入口抢锁失败且可能形成重启循环（BUG-159）。
            continue
        outs, ok = backend.enable(name, runner)
        outcomes.extend(outs)
        all_ok = all_ok and ok
    return OpResult(outcomes, all_ok and bool(services))


def disable(
    ws: Workspace, config: Config, *, runner: SubprocessRunner = _default_runner
) -> OpResult:
    """停用**实际已安装**的单元（持久 disable）。"""
    backend = select_backend()
    services = installed_services(ws, backend)
    outcomes: list[CmdOutcome] = []
    all_ok = True
    for name in services:
        outs, ok = backend.disable(name, runner)
        outcomes.extend(outs)
        all_ok = all_ok and ok
    return OpResult(outcomes, all_ok)


def uninstall(
    ws: Workspace,
    config: Config,
    *,
    purge_linger: bool = False,
    runner: SubprocessRunner = _default_runner,
) -> OpResult:
    """卸载**实际已安装**的单元（停服务 + 删单元 + 清 manifest）。

    仅在全部单元卸载成功后才清 manifest；失败时保留可重试状态（BUG-158）。
    """
    backend = select_backend()
    services = installed_services(ws, backend)
    outcomes: list[CmdOutcome] = []
    all_ok = True
    for name in services:
        outs, ok = backend.uninstall(name, runner)
        outcomes.extend(outs)
        all_ok = all_ok and ok
    if all_ok:
        _clear_manifest(ws)
    if purge_linger:
        all_ok = disable_linger(runner=runner) and all_ok
    return OpResult(outcomes, all_ok)


def is_service_loaded(
    name: str, *, runner: SubprocessRunner = _default_runner
) -> bool:
    """该服务单元当前是否已加载/激活（按当前平台后端判定）。"""
    try:
        backend = select_backend()
    except AutostartError:
        return False
    try:
        return backend.is_loaded(name, runner)
    except Exception:  # noqa: BLE001
        return False


@dataclass
class CoordinatedResult:
    """``lwa X off`` 协调结果。"""

    note: str | None = None
    ok: bool = True


def coordinated_disable(
    ws: Workspace, service_name: str, *, runner: SubprocessRunner = _default_runner
) -> CoordinatedResult:
    """``lwa X off`` 协调（030.b）：若该服务单元已加载，先卸载，避免被立刻拉回。

    返回 :class:`CoordinatedResult`：``note`` 为提示文本（有动作时），``ok`` 表示
    卸载是否真正成功——失败时调用方应警告（KeepAlive/Restart 可能立即拉回进程）。
    """
    try:
        backend = select_backend()
    except AutostartError:
        return CoordinatedResult()
    if not backend.unit_path(service_name).is_file():
        return CoordinatedResult()
    try:
        loaded = backend.is_loaded(service_name, runner)
    except Exception:  # noqa: BLE001
        loaded = False
    try:
        enabled = backend.is_enabled(service_name, runner)
    except Exception:  # noqa: BLE001
        enabled = False
    # enabled（即便当前 inactive）或 loaded 都会在下次触发/Restart 时把进程拉回，
    # 必须先 disable——只判 is_loaded 会漏掉"已启用但未激活"的单元（BUG-147）。
    if not (loaded or enabled):
        return CoordinatedResult()
    _outcomes, ok = backend.disable(service_name, runner)
    if ok:
        return CoordinatedResult(
            note=(
                f"已先停用 {service_name} 的自启动单元，避免 KeepAlive/Restart 在 off 后把进程拉回"
                "（重新自启：lwa autostart enable）"
            ),
            ok=True,
        )
    return CoordinatedResult(
        note=(
            f"⚠️ 停用 {service_name} 自启动单元失败，KeepAlive/Restart 可能立即把进程拉回；"
            "请先 `lwa autostart disable` 再停服"
        ),
        ok=False,
    )


# ---- linger ----------------------------------------------------------------


def enable_linger(*, runner: SubprocessRunner = _default_runner) -> bool:
    """``loginctl enable-linger $USER``（Linux/WSL）。失败返回 False。"""
    if detect_platform() not in (PLATFORM_LINUX, PLATFORM_WSL):
        return False
    user = os.environ.get("USER") or "root"
    try:
        res = runner(["loginctl", "enable-linger", user], capture_output=True)
        return res.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def linger_enabled(*, runner: SubprocessRunner = _default_runner) -> bool:
    """当前用户是否已 enable-linger。"""
    if detect_platform() not in (PLATFORM_LINUX, PLATFORM_WSL):
        return True  # 非 Linux 无此概念，视为满足
    user = os.environ.get("USER") or "root"
    try:
        res = runner(
            ["loginctl", "show-user", user, "--value", "-p", "Linger"],
            capture_output=True,
        )
        return res.returncode == 0 and (res.stdout or "").strip() == "yes"
    except Exception:  # noqa: BLE001
        return False


def disable_linger(*, runner: SubprocessRunner = _default_runner) -> bool:
    """``loginctl disable-linger $USER``（Linux/WSL）。失败返回 False。"""
    if detect_platform() not in (PLATFORM_LINUX, PLATFORM_WSL):
        return True  # 非 Linux 无此概念
    user = os.environ.get("USER") or "root"
    try:
        res = runner(["loginctl", "disable-linger", user], capture_output=True)
        return res.returncode == 0
    except Exception:  # noqa: BLE001
        return False


# ---- WSL Windows 唤醒脚本 --------------------------------------------------


def render_wsl_windows_script(ws: Workspace, config: Config) -> str:
    """生成 Windows 侧登录任务用的 PowerShell 脚本（唤醒并**保活**发行版）。

    WSL 发行版不随 Windows 开机自启，且空闲超时（``vmIdleTimeout``，默认 60s）会自动
    关机——单次 ``wsl.exe --exec /bin/true`` 只能短暂唤醒，无法保住 systemd 服务
    （BUG-150）。故脚本运行一个长驻命令（``sleep infinity``）占住发行版，使其不因空闲
    关机；任务计划程序停止/登出时该命令随任务进程结束，发行版才允许空闲关机。
    """
    distro = wsl_distro() or "Ubuntu"
    return (
        "# lwa WSL 保活脚本（Windows 任务计划程序 → 触发器「登录时」→ 操作「启动程序」\n"
        "#   程序: powershell.exe  参数: -NoProfile -ExecutionPolicy Bypass -File <本文件>）\n"
        "# WSL 发行版不随 Windows 开机自启，且空闲会自动关机；本脚本长驻以保活 systemd。\n"
        "# 停止保活：在任务计划程序里结束本任务（发行版随后可空闲关机）。\n"
        f"$distro = \"{distro}\"\n"
        "wsl.exe -d $distro -- bash -lc 'systemctl --user start lwa-daemon.service "
        "lwa-manager.service 2>/dev/null; exec sleep infinity'\n"
    )


# ---- 完备性检查（§10.5）----------------------------------------------------


@dataclass
class CheckItem:
    category: str
    name: str
    status: str  # ok / warn / fail
    message: str
    detail: str = ""
    fix: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "category": self.category,
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "detail": self.detail,
            "fix": self.fix,
        }


@dataclass
class CheckReport:
    platform: str
    items: list[CheckItem] = field(default_factory=list)

    @property
    def overall(self) -> str:
        if any(i.status == STATUS_FAIL for i in self.items):
            return STATUS_FAIL
        if any(i.status == STATUS_WARN for i in self.items):
            return STATUS_WARN
        return STATUS_OK

    @property
    def exit_code(self) -> int:
        if self.overall == STATUS_FAIL:
            return EXIT_INCOMPLETE
        if self.overall == STATUS_WARN:
            return EXIT_OK  # warn 不阻断
        return EXIT_OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "overall": self.overall,
            "items": [i.to_dict() for i in self.items],
        }


def _python_version_ok(python_exe: str) -> tuple[bool, str]:
    """单元内解释器是否 ≥3.13 且可 import lwa。"""
    if not Path(python_exe).exists() and not shutil.which(python_exe):
        return False, f"解释器路径不存在：{python_exe}"
    try:
        res = subprocess.run(
            [python_exe, "-c", "import sys;print(sys.version_info[:2])"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"执行解释器失败：{exc}"
    if res.returncode != 0:
        return False, "解释器无法执行"
    try:
        major, minor = eval(res.stdout.strip())  # noqa: S307 — 受控输入
    except Exception:  # noqa: BLE001
        return False, f"无法解析版本：{res.stdout!r}"
    if (major, minor) < (3, 13):
        return False, f"Python {major}.{minor} < 3.13"
    try:
        imp = subprocess.run(
            [python_exe, "-c", "import local_webpage_access"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"import 检查失败：{exc}"
    if imp.returncode != 0:
        return False, "该解释器无法 import local_webpage_access（lwa 未装到该环境）"
    return True, f"Python {major}.{minor}，可 import lwa"


def _extract_workspace_from_unit(name: str, plat: str) -> str | None:
    """从已写单元文件解析 --workspace 的值。"""
    try:
        backend = select_backend(plat)
        path = backend.unit_path(name)
        if not path.is_file():
            return None
        if plat == PLATFORM_MACOS:
            import plistlib

            data = plistlib.loads(path.read_bytes())
            args = list(data.get("ProgramArguments", []))
        else:
            args = shlex.split(
                _grep_exec_start(path) or ""
            )
        if "--workspace" in args:
            i = args.index("--workspace")
            if i + 1 < len(args):
                return args[i + 1]
    except Exception:  # noqa: BLE001
        return None
    return None


def _grep_exec_start(path: Path) -> str | None:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("ExecStart="):
            return line[len("ExecStart="):]
    return None


def _unit_python(name: str, plat: str) -> str | None:
    """从已写单元文件解析解释器路径。"""
    try:
        backend = select_backend(plat)
        path = backend.unit_path(name)
        if not path.is_file():
            return None
        if plat == PLATFORM_MACOS:
            import plistlib

            data = plistlib.loads(path.read_bytes())
            args = list(data.get("ProgramArguments", []))
            return args[0] if args else None
        es = _grep_exec_start(path)
        if es:
            return shlex.split(es)[0] if shlex.split(es) else None
    except Exception:  # noqa: BLE001
        return None
    return None


def _unit_args(name: str, plat: str) -> list[str]:
    """从已写单元文件解析完整命令参数（ProgramArguments / ExecStart）。"""
    try:
        backend = select_backend(plat)
        path = backend.unit_path(name)
        if not path.is_file():
            return []
        if plat == PLATFORM_MACOS:
            import plistlib

            return list(plistlib.loads(path.read_bytes()).get("ProgramArguments", []))
        es = _grep_exec_start(path)
        return shlex.split(es) if es else []
    except Exception:  # noqa: BLE001
        return []


def _check_unit_identity(ws: Workspace, backend: AutostartBackend, name: str) -> CheckItem:
    """单元身份：已安装 + 前台形态 + 工作区一致 + 模块正确（杜绝假绿，BUG-151）。"""
    path = backend.unit_path(name)
    if not path.is_file():
        return CheckItem("unit", name, STATUS_FAIL, f"{name} 单元未安装",
                         fix=f"lwa autostart install{' --with-caddy' if name == 'gateway' else ''}")
    plat = detect_platform()
    if backend.is_legacy(name):
        return CheckItem("unit", name, STATUS_FAIL,
                         f"{name} 是旧 detached 启动器（… {name} on），无崩溃恢复",
                         fix="lwa autostart repair")
    args = _unit_args(name, plat)
    # 工作区一致
    unit_ws = _extract_workspace_from_unit(name, plat)
    if unit_ws is None or Path(unit_ws).resolve() != Path(ws.root).resolve():
        return CheckItem("unit", name, STATUS_FAIL,
                         f"{name} 单元工作区({unit_ws}) ≠ 当前({ws.root})",
                         fix="lwa autostart repair（重写为当前工作区）")
    # 模块正确（前台入口）
    expected_mod = SERVICE_MODULES.get(name)
    if expected_mod and ("-m", expected_mod) not in zip(args, args[1:]):
        return CheckItem("unit", name, STATUS_FAIL,
                         f"{name} 单元非预期前台模块（期望 -m {expected_mod}）",
                         fix="lwa autostart repair")
    return CheckItem("unit", name, STATUS_OK, f"{name} 前台监管单元，工作区一致")


def _check_enabled(backend: AutostartBackend, name: str, runner: SubprocessRunner) -> CheckItem:
    """是否被服务管理器标记为启用（is-enabled / launchctl 非 disabled）。"""
    try:
        enabled = backend.is_enabled(name, runner)
    except Exception:  # noqa: BLE001
        enabled = False
    if enabled:
        return CheckItem("enabled", name, STATUS_OK, "已启用")
    return CheckItem("enabled", name, STATUS_FAIL, "未启用",
                     fix="lwa autostart enable")


def _service_process_running(ws: Workspace, config: Config, name: str) -> bool:
    """按服务状态判定前台进程是否在运行（daemon/manager/gateway）。"""
    if name == "daemon":
        from local_webpage_access import daemon as daemon_mod

        return bool(daemon_mod.is_running(ws))  # type: ignore[attr-defined]
    if name == "manager":
        from local_webpage_access.manager_service import manager_status

        st = manager_status(ws, config)
        return bool(st.get("running")) if isinstance(st, dict) else False
    from local_webpage_access.gateway_service import is_gateway_running

    return bool(is_gateway_running(ws, config))


def _check_active(
    ws: Workspace, config: Config, backend: AutostartBackend, name: str, runner: SubprocessRunner
) -> CheckItem:
    """运行态：已加载/激活 + MainPID 存活且身份正确 + 服务进程可探测。"""
    try:
        active = backend.is_loaded(name, runner)
    except Exception:  # noqa: BLE001
        active = False
    if not active:
        return CheckItem("active", name, STATUS_FAIL, "单元未激活（未加载/未运行）",
                         fix="lwa autostart enable，并查 logs/launchd-<name>.err 或 journalctl --user -u lwa-<name>")
    try:
        mpid = backend.main_pid(name, runner)
    except Exception:  # noqa: BLE001
        mpid = None
    # MainPID 校验：单元 active 则应有存活主进程
    from local_webpage_access.daemon import is_pid_alive, pid_cmdline_contains

    if mpid and not is_pid_alive(mpid):
        return CheckItem("active", name, STATUS_FAIL,
                         f"{name} 单元标记 active 但 MainPID {mpid} 已死",
                         fix="查启动日志；必要时 lwa autostart repair")
    # 监管单元 active 时必须有 MainPID（BUG-156）；空 PID 无法证明由本单元拉起。
    if not mpid:
        return CheckItem("active", name, STATUS_FAIL,
                         f"{name} 单元标记 active 但无 MainPID",
                         fix="查 logs/launchd-<name>.err 或 journalctl --user -u lwa-<name>；必要时 lwa autostart repair")
    # MainPID 身份：须为本工作区对应前台模块，杜绝任意存活 PID 假绿（BUG-162）。
    expected_mod = SERVICE_MODULES.get(name, "")
    if not pid_cmdline_contains(mpid, expected_mod, str(ws.root)):
        return CheckItem(
            "active", name, STATUS_FAIL,
            f"{name} MainPID {mpid} 身份不符（非本工作区前台模块 {expected_mod}）",
            fix="lwa autostart repair；确认无外部进程占用同名服务",
        )
    try:
        proc_running = _service_process_running(ws, config, name)
    except Exception as exc:  # noqa: BLE001
        return CheckItem("active", name, STATUS_WARN, f"单元 active，但进程探测失败：{exc}")
    if not proc_running:
        # 单元标记 active 但实际无服务进程 → 假绿，按验收标准判 fail（BUG-149）。
        return CheckItem("active", name, STATUS_FAIL,
                         f"{name} 单元标记 active 但服务进程未运行",
                         fix="查 logs/launchd-<name>.err 或 journalctl --user -u lwa-<name>；必要时 lwa autostart repair")
    pid_info = f"，MainPID={mpid}"
    return CheckItem("active", name, STATUS_OK, f"{name} 运行中{pid_info}")


def _check_unit_interpreter(name: str, plat: str) -> CheckItem:
    """逐单元校验绝对 Python ≥3.13 且可执行（BUG-156）。"""
    py = _unit_python(name, plat)
    if not py:
        return CheckItem(
            "interpreter", name, STATUS_FAIL,
            f"{name} 单元无法解析解释器路径",
            fix="lwa autostart repair",
        )
    ok, msg = _python_version_ok(py)
    return CheckItem(
        "interpreter", name, STATUS_OK if ok else STATUS_FAIL, f"{name}: {msg}",
        fix="用 ≥3.13 且已 pip install -e . 的解释器重跑 lwa autostart repair" if not ok else "",
    )


def _unit_path_env(name: str, plat: str) -> str | None:
    """从单元文件读取 PATH 环境变量；缺失返回 None。"""
    try:
        backend = select_backend(plat)
        path = backend.unit_path(name)
        if not path.is_file():
            return None
        if plat == PLATFORM_MACOS:
            import plistlib

            data = plistlib.loads(path.read_bytes())
            env = data.get("EnvironmentVariables") or {}
            val = env.get("PATH")
            return str(val) if val else None
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("Environment=PATH="):
                return line[len("Environment=PATH="):]
            if line.startswith("Environment=\"PATH="):
                return line.split("PATH=", 1)[-1].rstrip('"')
    except Exception:  # noqa: BLE001
        return None
    return None


def _check_unit_path_env(name: str, plat: str) -> CheckItem:
    """单元须固化可用 PATH（解释器目录/系统目录；gateway 须能解析 caddy）（BUG-164）。"""
    path_env = _unit_path_env(name, plat)
    if not path_env:
        return CheckItem(
            "path", name, STATUS_FAIL,
            f"{name} 单元缺少 PATH 环境变量",
            fix="lwa autostart repair",
        )
    dirs = [d for d in path_env.split(os.pathsep) if d]
    if not dirs:
        return CheckItem(
            "path", name, STATUS_FAIL,
            f"{name} 单元 PATH 为空",
            fix="lwa autostart repair",
        )
    existing = [d for d in dirs if Path(d).is_dir()]
    if not existing:
        return CheckItem(
            "path", name, STATUS_FAIL,
            f"{name} 单元 PATH 目录均不存在",
            fix="lwa autostart repair",
        )
    py = _unit_python(name, plat)
    if py:
        try:
            py_dir = str(Path(py).resolve().parent)
        except OSError:
            py_dir = str(Path(py).parent)
        has_py_dir = py_dir in dirs
        has_sys = any(d in ("/usr/bin", "/bin") and Path(d).is_dir() for d in dirs)
        if not (has_py_dir or has_sys):
            return CheckItem(
                "path", name, STATUS_FAIL,
                f"{name} 单元 PATH 既无解释器目录也无基础系统目录",
                fix="lwa autostart repair",
            )
    if name == "gateway":
        if shutil.which("caddy", path=path_env) is None:
            return CheckItem(
                "path", name, STATUS_FAIL,
                f"{name} 单元 PATH 无法解析 caddy",
                fix="安装 caddy 到单元 PATH 可见目录后 lwa autostart repair",
            )
    return CheckItem("path", name, STATUS_OK, f"{name} PATH 已固化且可用")


def run_check(
    ws: Workspace,
    config: Config,
    *,
    runner: SubprocessRunner = _default_runner,
) -> CheckReport:
    """完备性深检（§10.5）。"""
    plat = detect_platform()
    report = CheckReport(platform=plat)

    # 平台
    if plat not in (PLATFORM_MACOS, PLATFORM_LINUX, PLATFORM_WSL):
        report.items.append(CheckItem("platform", "platform", STATUS_FAIL,
                                      f"不支持的平台 {plat!r}",
                                      fix="仅 macOS / Linux / WSL 支持自启动"))
        return report
    if plat == PLATFORM_WSL and not systemd_available():
        report.items.append(CheckItem("platform", "systemd", STATUS_FAIL,
                                      "WSL 下 systemd 不可用",
                                      fix="/etc/wsl.conf 设置 [boot] systemd=true 后 wsl --shutdown 重启"))
        return report
    report.items.append(CheckItem("platform", "platform", STATUS_OK, plat))

    backend = select_backend(plat)
    installed = installed_services(ws, backend)

    # 工作区
    ws_ok = ws.config_path.is_file()
    report.items.append(CheckItem(
        "workspace", "workspace", STATUS_OK if ws_ok else STATUS_FAIL,
        f"{ws.root}（含 local-web.yml）" if ws_ok else f"工作区未初始化：{ws.root}",
        fix="lwa init" if not ws_ok else "",
    ))

    # 逐服务（以"实际已安装"为准）：单元身份 + 解释器 + PATH + 启用态 + 运行态
    expected = select_services(config, with_caddy=config.staticGateway == "caddy")
    required = ["daemon"]
    if config.managerEnabled:
        required.append("manager")
    if not installed:
        # 一个自启动单元都没装 → 自启动实质未配置，按验收标准判 fail（BUG-149），
        # 不再仅以 expected-but-missing 的 warn 退出 0。
        report.items.append(CheckItem(
            "unit", "install", STATUS_FAIL, "尚未安装任何自启动单元",
            fix="lwa autostart install"))
    else:
        for name in installed:
            report.items.append(_check_unit_identity(ws, backend, name))
            report.items.append(_check_unit_interpreter(name, plat))
            report.items.append(_check_unit_path_env(name, plat))
            report.items.append(_check_enabled(backend, name, runner))
            report.items.append(_check_active(ws, config, backend, name, runner))
        # 配置期望但未安装：daemon/manager 为必需 → fail；gateway 可选 → warn（BUG-155）。
        for name in expected:
            if name not in installed:
                status = STATUS_FAIL if name in required else STATUS_WARN
                report.items.append(CheckItem(
                    "unit", name, status,
                    f"{name} 未纳入自启（当前配置期望）",
                    fix=f"lwa autostart install{' --with-caddy' if name == 'gateway' else ''}"))

    # Caddy
    if config.staticGateway == "caddy":
        report.items.append(_check_caddy(ws, config))

    # linger（Linux/WSL）
    if plat in (PLATFORM_LINUX, PLATFORM_WSL):
        if linger_enabled(runner=runner):
            report.items.append(CheckItem("linger", "linger", STATUS_OK, "Linger=yes"))
        else:
            report.items.append(CheckItem(
                "linger", "linger", STATUS_WARN, "未 enable-linger，登出后 user 服务会停止",
                fix="sudo loginctl enable-linger $USER"))

    # WSL 待办
    if plat == PLATFORM_WSL:
        report.items.append(_check_wsl(ws))

    # Docker（有容器实例时）
    report.items.append(_check_docker(ws))

    return report


def _port_2019_foreign(ws: Workspace) -> bool:
    """:2019 是否被**非本工作区 gateway**的外部进程占用。

    本工作区 gateway 持有存活且命令行身份匹配的 pid（含 ``caddy`` + 工作区路径）
    则 :2019 是我们的；仅 PID 存活不够——复用/非 Caddy 进程会假绿（BUG-157）。
    否则若 :2019 仍可连，即为外部 Caddy/测试孤儿占用。
    """
    import socket

    try:
        from local_webpage_access.daemon import is_pid_alive, pid_cmdline_contains
        from local_webpage_access.gateway_service import read_state

        st = read_state(ws)
        if st and st.pid and is_pid_alive(st.pid):
            if pid_cmdline_contains(st.pid, "caddy", str(ws.root)):
                return False  # :2019 由本工作区存活 master 持有
            # PID 存活但身份不匹配 → 继续探端口，可能是陈旧复用
    except Exception:  # noqa: BLE001
        pass
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        sock.connect(("127.0.0.1", 2019))
        return True  # 有进程在听 :2019，且不是经验证的本工作区 gateway
    except OSError:
        return False
    finally:
        sock.close()


def _check_caddy(ws: Workspace, config: Config) -> CheckItem:
    """Caddy：二进制在 PATH + 无系统 caddy.service/:2019 外部冲突（杜绝 :2019 争用假绿）。"""
    caddy = shutil.which("caddy")
    if not caddy:
        return CheckItem(
            "caddy", "caddy_binary", STATUS_FAIL,
            "PATH 中找不到 caddy（BUG-139：监管环境 PATH 不全）",
            fix="brew install caddy 或把 caddy 路径加入 PATH 后 lwa autostart repair")
    # 系统 caddy.service 与 LWA gateway 争用 :2019（仅 Linux 可能存在发行版单元）
    if detect_platform() in (PLATFORM_LINUX, PLATFORM_WSL):
        try:
            res = subprocess.run(
                ["systemctl", "is-active", "caddy.service"],
                capture_output=True, text=True, timeout=5,
            )
            if res.returncode == 0:
                return CheckItem(
                    "caddy", "caddy_conflict", STATUS_FAIL,
                    "系统 caddy.service 正在运行，会与 LWA gateway 争用 :2019",
                    fix="sudo systemctl disable --now caddy.service（由 LWA 托管 Caddy）")
        except Exception:  # noqa: BLE001
            pass
    # 外部 Caddy/进程占用 :2019（本工作区 gateway 未持存活 pid 却探测到端口在线）
    if _port_2019_foreign(ws):
        return CheckItem(
            "caddy", "caddy_conflict_2019", STATUS_FAIL,
            "127.0.0.1:2019 已被外部 Caddy/进程占用，LWA gateway 无法监听",
            fix="停止占用 :2019 的进程（如 caddy stop / 关测试孤儿），或改其 admin 端口")
    return CheckItem("caddy", "caddy_binary", STATUS_OK, f"caddy: {caddy}，无系统服务/端口冲突")


def _check_wsl(ws: Workspace) -> CheckItem:
    distro = wsl_distro() or "unknown"
    on_mnt_c = str(ws.root).startswith("/mnt/c") or str(ws.root).startswith("/mnt/")
    notes = [f"发行版：{distro}"]
    if on_mnt_c:
        notes.append("工作区在 /mnt/×（Windows 文件系统），IO 较慢，建议放 ~/ 下")
    return CheckItem("wsl", "wsl", STATUS_WARN, "；".join(notes),
                     detail="WSL 发行版不随 Windows 开机自启；需 Windows 登录任务唤醒 + 网络可能变化（lwa access refresh）")


def _check_docker(ws: Workspace) -> CheckItem:
    """有容器实例时检查 Docker 引擎可达性（warn 级）。"""
    try:
        from local_webpage_access.registry import Registry

        reg = Registry(ws.db_path)
        reg.open()
        try:
            # 容器实例标识在 runtime 字段（"docker-compose"），非 kind（BUG-143）。
            has_container = any(
                (i.get("runtime") if isinstance(i, dict) else getattr(i, "runtime", ""))
                == "docker-compose"
                for i in reg.list_instances()
            )
        finally:
            reg.close()
    except Exception:  # noqa: BLE001
        return CheckItem("docker", "docker", STATUS_OK, "无 registry，跳过")
    if not has_container:
        return CheckItem("docker", "docker", STATUS_OK, "无容器实例，跳过")
    if not shutil.which("docker"):
        return CheckItem("docker", "docker", STATUS_WARN,
                         "有容器实例但 docker 命令缺失",
                         fix="安装 Docker Engine/Desktop")
    try:
        res = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        if res.returncode == 0:
            return CheckItem("docker", "docker", STATUS_OK, "Docker 引擎可达")
        return CheckItem("docker", "docker", STATUS_WARN,
                         "docker info 返回非零，引擎可能未启动",
                         fix="启动 Docker Desktop/Engine（建议设为登录/开机启动）")
    except Exception as exc:  # noqa: BLE001
        return CheckItem("docker", "docker", STATUS_WARN, f"探测 Docker 失败：{exc}")


# ---- repair ----------------------------------------------------------------


def repair(
    ws: Workspace,
    config: Config,
    *,
    with_caddy: bool = False,
    python_exe: str | None = None,
    runner: SubprocessRunner = _default_runner,
) -> tuple[InstallResult, list[str]]:
    """修复：重写失效路径、迁移旧启动器单元、重新 enable。返回 (结果, 修复说明)。"""
    actions: list[str] = []
    backend = select_backend()
    # 检测旧单元 → 记录迁移
    for name in select_services(config, with_caddy=with_caddy):
        if backend.is_legacy(name):
            actions.append(f"迁移旧 detached 启动器单元：{name}")
    result = install(
        ws, config, with_caddy=with_caddy, enable=True,
        python_exe=python_exe, runner=runner,
    )
    if not actions:
        actions.append("重写单元（固化当前解释器/工作区/Caddy 路径）并重新启用")
    return result, actions


# ---- doctor-hints ----------------------------------------------------------


def doctor_hints(ws: Workspace, config: Config) -> str:
    """输出人工待办（Docker Desktop 登录启动 / WSL 网络等），不自动改系统设置。"""
    plat = detect_platform()
    lines: list[str] = ["── 自启动人工待办 ──"]
    if plat == PLATFORM_MACOS:
        lines.append("· macOS 为用户登录触发型自启（LaunchAgent），非无人值守系统服务。")
        lines.append("· Docker Desktop：在设置里勾选“Start Docker Desktop when you sign in”。")
    elif plat in (PLATFORM_LINUX, PLATFORM_WSL):
        lines.append("· Linux/WSL 为 systemd user 服务；登出后需 linger 才保活："
                     "sudo loginctl enable-linger $USER")
        lines.append("· Docker Engine 建议设为开机自启：sudo systemctl enable docker")
        if plat == PLATFORM_WSL:
            lines.append("· WSL：发行版需 Windows 登录任务唤醒（lwa autostart install 已生成脚本）；"
                         "网络可能变化，IP 变更后执行 lwa access refresh。")
    else:
        lines.append(f"· 平台 {plat!r} 暂不支持自启动（参考 docs/autostart.md）。")
    if config.staticGateway == "caddy":
        lines.append("· Caddy 由 LWA 托管；切勿同时启用系统 caddy.service（争用 :2019）。")
    return "\n".join(lines)


__all__ = [
    "ALL_SERVICES",
    "AutostartBackend",
    "AutostartError",
    "CheckItem",
    "CheckReport",
    "CmdOutcome",
    "CoordinatedResult",
    "EXIT_INCOMPLETE",
    "EXIT_OK",
    "EXIT_UNSUPPORTED",
    "InstallResult",
    "MacLaunchdBackend",
    "OpResult",
    "SERVICE_MODULES",
    "SystemdUserBackend",
    "UnitGen",
    "build_launchd_plist",
    "build_systemd_unit",
    "coordinated_disable",
    "detect_platform",
    "disable",
    "disable_linger",
    "doctor_hints",
    "enable",
    "enable_linger",
    "install",
    "installed_services",
    "is_legacy_exec_start",
    "is_legacy_program_arguments",
    "is_service_loaded",
    "linger_enabled",
    "read_manifest",
    "render_wsl_windows_script",
    "repair",
    "run_check",
    "select_backend",
    "select_services",
    "uninstall",
    "write_manifest",
]
