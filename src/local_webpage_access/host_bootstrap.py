"""宿主机组件安装编排（IMP-031 / IMP-032）。

提供：
- 内置安装脚本定位（Docker / Caddy，macOS / Linux）
- Docker Engine 状态细分（missing / daemon_down / outdated / ok）
- ``--default`` 下可选询问安装 Docker
- ``--full`` 下检查并安装 Caddy + Docker Engine + Compose
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal, Sequence

if TYPE_CHECKING:
    from local_webpage_access.capability import CapabilityReport

from local_webpage_access.doctor import SubprocessRunner, _default_runner
from local_webpage_access.platform_detect import detect_platform
from local_webpage_access.version_requirements import (
    MIN_CADDY_VERSION,
    MIN_COMPOSE_VERSION,
    MIN_DOCKER_VERSION,
    version_ge,
)

BootstrapProfile = Literal["default", "full"]
InstallKind = Literal["docker", "caddy"]

_SCRIPT_NAMES: dict[tuple[InstallKind, str], str] = {
    ("docker", "macos"): "install-docker-macos.sh",
    ("docker", "linux"): "install-docker-linux.sh",
    ("caddy", "macos"): "install-caddy-macos.sh",
    ("caddy", "linux"): "install-caddy-linux.sh",
}


@dataclass(frozen=True)
class DockerEngineState:
    status: Literal["missing", "daemon_down", "outdated", "ok"]
    version: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class ComponentNeed:
    name: str
    status: Literal["missing", "outdated", "ok", "daemon_down"]
    version: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class InstallPlanItem:
    kind: InstallKind
    script: Path
    reason: str


@dataclass
class DockerOfferResult:
    """default 档 Docker 安装询问/执行结果（BUG-197）。"""

    messages: list[str]
    attempted: bool = False
    script_ok: bool | None = None  # None=未执行；True/False=脚本退出码
    recheck_ok: bool | None = None  # 执行后复检 Engine 是否 ok


@dataclass
class FullBootstrapResult:
    ok: bool
    planned: list[InstallPlanItem]
    ran: list[InstallPlanItem]
    messages: list[str]
    skipped_no_confirm: bool = False
    overall: str = "unready"  # ready | unready | session_refresh_required
    session_refresh_required: bool = False
    exit_code: int = 1
    service_user: str | None = None


def resolve_profile(*, default: bool, full: bool) -> BootstrapProfile:
    if default and full:
        raise ValueError("--default 与 --full 互斥，请只指定其中一个")
    if full:
        return "full"
    return "default"


def _script_platform(plat: str | None = None) -> str:
    p = plat or detect_platform()
    if p in ("linux", "wsl"):
        return "linux"
    if p == "macos":
        return "macos"
    raise FileNotFoundError(f"当前平台 {p} 无内置安装脚本（本期仅 macOS / Linux / WSL）")


def resolve_install_script(kind: InstallKind, plat: str | None = None) -> Path:
    """定位打包内脚本；失败时回退到 importlib.resources。"""
    script_plat = _script_platform(plat)
    name = _SCRIPT_NAMES[(kind, script_plat)]

    # 1) 与本模块同目录（editable / 常规 wheel 布局）
    here = Path(__file__).resolve().parent / "scripts" / name
    if here.is_file():
        return here

    # 2) importlib.resources（部分安装布局）
    try:
        candidate = resources.files("local_webpage_access").joinpath("scripts", name)
        if candidate.is_file():
            with resources.as_file(candidate) as real:
                path = Path(real)
                if path.is_file():
                    return path
    except (FileNotFoundError, ModuleNotFoundError, TypeError, AttributeError, OSError):
        pass

    raise FileNotFoundError(f"找不到内置安装脚本 {name}；请从源码树运行或重新 pip install -e .")


def detect_docker_engine(
    runner: SubprocessRunner = _default_runner,
) -> DockerEngineState:
    if shutil.which("docker") is None:
        return DockerEngineState(status="missing", detail="未找到 docker 命令")

    client = runner(["docker", "version", "--format", "{{.Client.Version}}"])
    server = runner(["docker", "version", "--format", "{{.Server.Version}}"])
    client_ver = (client.stdout or "").strip() or None
    server_ver = (server.stdout or "").strip() or None

    if server.returncode != 0 or not server_ver:
        if client.returncode == 0 and client_ver:
            return DockerEngineState(
                status="daemon_down",
                version=client_ver,
                detail=(server.stderr or "Docker daemon 不可达").strip()[:200],
            )
        return DockerEngineState(
            status="missing",
            detail=(server.stderr or client.stderr or "docker 不可用").strip()[:200],
        )

    if not version_ge(server_ver, MIN_DOCKER_VERSION):
        return DockerEngineState(status="outdated", version=server_ver)
    return DockerEngineState(status="ok", version=server_ver)


def detect_docker_compose(
    runner: SubprocessRunner = _default_runner,
) -> ComponentNeed:
    if shutil.which("docker") is None:
        return ComponentNeed(name="compose", status="missing", detail="无 docker 命令")
    result = runner(["docker", "compose", "version", "--short"])
    if result.returncode != 0:
        return ComponentNeed(
            name="compose",
            status="missing",
            detail=(result.stderr or "compose 不可用").strip()[:200],
        )
    ver = (result.stdout or "").strip().lstrip("v")
    if not version_ge(ver, MIN_COMPOSE_VERSION):
        return ComponentNeed(name="compose", status="outdated", version=ver)
    return ComponentNeed(name="compose", status="ok", version=ver)


def detect_caddy(runner: SubprocessRunner = _default_runner) -> ComponentNeed:
    if shutil.which("caddy") is None:
        return ComponentNeed(name="caddy", status="missing")
    result = runner(["caddy", "version"])
    if result.returncode != 0:
        return ComponentNeed(
            name="caddy",
            status="missing",
            detail=(result.stderr or "caddy version 失败").strip()[:200],
        )
    line = ((result.stdout or "") + (result.stderr or "")).strip().splitlines()
    raw = line[0] if line else ""
    if not version_ge(raw, MIN_CADDY_VERSION):
        return ComponentNeed(name="caddy", status="outdated", version=raw.strip())
    return ComponentNeed(name="caddy", status="ok", version=raw.strip())


def should_offer_docker_install(state: DockerEngineState) -> bool:
    """仅「未安装 / 命令不存在」进入 default 档询问安装。"""
    return state.status == "missing"


def plan_full_install(
    *,
    platform: str | None = None,
    runner: SubprocessRunner = _default_runner,
) -> list[InstallPlanItem]:
    """full 档：缺 Engine/过低 → docker 脚本；缺 Caddy/过低 → caddy 脚本。

    Compose 随 Docker 脚本一并安装；daemon_down 不重装，只在消息里提示启动。
    """
    plat = platform or detect_platform()
    items: list[InstallPlanItem] = []

    # IMP-036：WSL 已接 Docker Desktop 时复用 integration，不装发行版内 Engine
    skip_distro_docker = False
    if plat == "wsl":
        from local_webpage_access.platform_support import detect_wsl_docker_backend

        backend = detect_wsl_docker_backend()
        if backend == "desktop":
            skip_distro_docker = True
        elif backend == "conflict":
            # 冲突由 run_full_bootstrap 阻断；此处不追加安装计划
            skip_distro_docker = True

    docker = detect_docker_engine(runner=runner)
    compose = detect_docker_compose(runner=runner)
    if not skip_distro_docker and (
        docker.status in ("missing", "outdated") or compose.status in ("missing", "outdated")
    ):
        if docker.status != "daemon_down":
            reason_parts = []
            if docker.status in ("missing", "outdated"):
                reason_parts.append(f"docker={docker.status}")
            if compose.status in ("missing", "outdated"):
                reason_parts.append(f"compose={compose.status}")
            items.append(
                InstallPlanItem(
                    kind="docker",
                    script=resolve_install_script("docker", plat),
                    reason=",".join(reason_parts) or "docker",
                )
            )

    caddy = detect_caddy(runner=runner)
    if caddy.status in ("missing", "outdated"):
        items.append(
            InstallPlanItem(
                kind="caddy",
                script=resolve_install_script("caddy", plat),
                reason=f"caddy={caddy.status}",
            )
        )
    return items


# S1-06 / #55：安装脚本总时限；超时后先 SIGTERM 进程组，宽限期后 SIGKILL。
_SCRIPT_TOTAL_TIMEOUT = 1800
_SCRIPT_KILL_GRACE = 5.0


def _default_subprocess_run(
    cmd: Sequence[str],
    *,
    timeout: float | None = None,
    log_path: Path | None = None,
    **kwargs,
) -> subprocess.CompletedProcess[str]:
    """运行安装脚本：逐行回放到终端并写入 ``log_path``，总时限到期后终止整个进程组。

    输出同时汇总到 ``stdout`` 供失败时回流 messages；超时的返回码非零，且 stdout 末尾注明时限。
    读取与清理一律以进程组是否仍存在为准（leader 退出不代表子进程已收尾）；
    日志先于进程创建，初始化失败不会留下安装进程。
    """
    import contextlib
    import os
    import select
    import signal
    import threading
    import time

    limit = float(_SCRIPT_TOTAL_TIMEOUT if timeout is None else timeout)
    deadline = time.monotonic() + limit
    log_fh = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = log_path.open("a", encoding="utf-8")
    timed_out = threading.Event()
    proc: subprocess.Popen[str] | None = None
    timer: threading.Timer | None = None

    def _signal_group(sig: int) -> None:
        if proc is None:
            return
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(proc.pid, sig)

    def _group_alive() -> bool:
        if proc is None:
            return False
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            # EPERM：组仍存在（如已交由 launchd 回收的僵尸），只是当前无权发信号
            return True
        return True

    def _on_timeout() -> None:
        timed_out.set()
        _signal_group(signal.SIGTERM)
        grace_deadline = time.monotonic() + _SCRIPT_KILL_GRACE
        while time.monotonic() < grace_deadline and _group_alive():
            time.sleep(0.02)
        if _group_alive():
            _signal_group(signal.SIGKILL)

    try:
        proc = subprocess.Popen(  # noqa: S603 — 内置安装脚本，参数来自受控列表
            list(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
            **kwargs,
        )
        timer = threading.Timer(limit, _on_timeout)
        timer.daemon = True
        timer.start()
        assert proc.stdout is not None
        chunks: list[str] = []
        eof = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([proc.stdout], [], [], min(remaining, 0.1))
            if not ready:
                continue
            line = proc.stdout.readline()
            if not line:
                eof = True
                break
            chunks.append(line)
            sys.stdout.write(line)
            sys.stdout.flush()
            if log_fh is not None:
                log_fh.write(line)
                log_fh.flush()
        if eof:
            # stdout 提前 EOF 不代表工作完成（进程可能静默继续）：在剩余预算内
            # 等待自然收尾；到期仍未退出才走下方终止路径（超时定时器同时兜底）。
            while (proc.poll() is None or _group_alive()) and time.monotonic() < deadline:
                time.sleep(0.02)
        if time.monotonic() >= deadline:
            timed_out.set()
        if proc.poll() is None or _group_alive():
            # 读取到点或进程组仍有残留：TERM → 宽限 → KILL，按组是否仍存在判断
            _signal_group(signal.SIGTERM)
            grace_deadline = time.monotonic() + _SCRIPT_KILL_GRACE
            while time.monotonic() < grace_deadline:
                if proc.poll() is not None and not _group_alive():
                    break
                time.sleep(0.02)
            if _group_alive():
                _signal_group(signal.SIGKILL)
            reap_deadline = time.monotonic() + 5.0
            while (
                proc.poll() is None or _group_alive()
            ) and time.monotonic() < reap_deadline:
                time.sleep(0.02)
        returncode = proc.wait()
    finally:
        if timer is not None:
            timer.cancel()
        if proc is not None and _group_alive():
            # 异常路径按整个进程组是否存活收尾，leader 已退出不代表同组子进程已结束
            _signal_group(signal.SIGKILL)
            reap_deadline = time.monotonic() + 5.0
            while (proc.poll() is None or _group_alive()) and time.monotonic() < reap_deadline:
                time.sleep(0.02)
        if log_fh is not None:
            log_fh.close()
    output = "".join(chunks)
    if timed_out.is_set():
        note = f"安装脚本超过总时限 {limit:g}s，已终止进程组（可能卡在网络或 sudo 提示）。"
        output = f"{output}\n{note}" if output else note
        sys.stdout.write(note + "\n")
        sys.stdout.flush()
        returncode = returncode or 124
    return subprocess.CompletedProcess(list(cmd), returncode, stdout=output, stderr="")


def _bootstrap_log_path(workspace_root: Path) -> Path:
    """安装脚本输出日志：``<workspace>/logs/bootstrap-<时间戳>.log``（S1-06）。"""
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path(workspace_root) / "logs" / f"bootstrap-{stamp}.log"


_SUDO_INSTALL_SCRIPTS = frozenset(
    {
        "install-docker-linux.sh",
        "install-caddy-linux.sh",
    }
)


def _plan_requires_sudo(planned: Sequence[InstallPlanItem]) -> bool:
    """按安装项目计算 sudo 需求（BUG-768）：仅 Linux/WSL 内置脚本调用 sudo。

    macOS 脚本以普通用户执行 brew install/upgrade，不需要 sudo；
    不得因预检要求密码而提前拒绝本可无人值守完成的 macOS 安装。
    """
    return any(item.script.name in _SUDO_INSTALL_SCRIPTS for item in planned)


def _sudo_preflight() -> str | None:
    """非 root 时的 sudo 预认证（S1-06 / #55）。

    ``sudo -n true`` 通过即就绪；否则仅在交互终端下执行 ``sudo -v`` 让用户输入密码，
    非交互环境直接返回拒绝原因，避免安装脚本在中途卡在密码提示。
    返回 None 表示就绪，否则为应展示给用户的原因。
    """
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and geteuid() == 0:
        return None
    if shutil.which("sudo") is None:
        return "未找到 sudo，无法执行需要管理员权限的安装步骤"
    try:
        probe = subprocess.run(  # noqa: S603, S607
            ["sudo", "-n", "true"], capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"sudo 检测失败：{exc}"
    if probe.returncode == 0:
        return None
    if not _stdin_is_interactive():
        return (
            "安装需要 sudo 权限，但当前为非交互环境（sudo 需要密码）。"
            "请在交互终端执行 sudo -v 后重试：lwa setup --full --resume"
        )
    try:
        auth = subprocess.run(["sudo", "-v"], check=False)  # noqa: S603, S607
    except (OSError, subprocess.SubprocessError) as exc:
        return f"sudo 认证失败：{exc}"
    return None if auth.returncode == 0 else "sudo 认证失败，已中止安装"


def _script_failure_detail(result: subprocess.CompletedProcess[str], *, limit: int = 400) -> str:
    """从脚本结果提取可展示的失败摘要（优先 stderr）。"""
    blob = (result.stderr or result.stdout or "").strip()
    if not blob:
        return ""
    text = blob if len(blob) <= limit else blob[-limit:]
    one_line = " ".join(text.split())
    return f"：{one_line}"


def _stdin_is_interactive() -> bool:
    """Click/Typer 隔离环境下 ``sys.stdin.isatty()`` 仍可能为 True；优先问 Click。"""
    try:
        import click

        return bool(click.get_text_stream("stdin").isatty())
    except Exception:
        try:
            return bool(sys.stdin.isatty())
        except Exception:
            return False


def run_full_bootstrap(
    *,
    platform: str | None = None,
    yes: bool = False,
    resume: bool = False,
    workspace_root: Path | None = None,
    confirm: Callable[[str], bool] | None = None,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
    detect_runner: SubprocessRunner = _default_runner,
) -> FullBootstrapResult:
    """执行 full 装配：确认 → 跑脚本 → 复检 → 能力验收（IMP-033）。

    ``resume=True`` 时跳过已完成安装步骤，从能力复检继续。
    退出语义：ready→exit_code 0；session_refresh_required→2；unready→1。
    """
    from local_webpage_access.capability import (
        collect_capability_report,
        current_service_user,
        load_profile_state,
        probe_docker_access_state,
        save_profile_state,
    )

    plat = platform or detect_platform()
    service_user = current_service_user()

    # IMP-036 / BUG-260：仅 WSL + /mnt/<drive> 时 Full 写路径 fail-closed
    # （原生 Linux 上偶然的 /mnt/c 路径不得误挡）
    from local_webpage_access.platform_support import (
        PlatformSupportReport,
        assert_writable_workspace_allowed,
        collect_platform_support_report,
    )

    if workspace_root is not None:
        try:
            assert_writable_workspace_allowed(
                workspace_root,
                report=PlatformSupportReport(platform=plat),
            )
        except SystemExit as exc:
            msg = exc.code if isinstance(exc.code, str) else str(exc)
            return FullBootstrapResult(
                ok=False,
                planned=[],
                ran=[],
                messages=[
                    msg
                    or (
                        "工作区位于 /mnt/<drive>（Windows 文件系统），Full Profile 已阻断；"
                        "请迁移到 Linux 文件系统（如 ~/lwa）后再执行 lwa setup --full。"
                    )
                ],
                overall="unready",
                exit_code=1,
                service_user=service_user,
            )
    if plat == "wsl":
        ps = collect_platform_support_report(
            platform_name="wsl",
            workspace_root=workspace_root,
        )
        if ps.docker_backend == "conflict":
            return FullBootstrapResult(
                ok=False,
                planned=[],
                ran=[],
                messages=[
                    "同时检测到 Docker Desktop WSL integration 与发行版内 Docker Engine；"
                    "请只保留一套后再执行 lwa setup --full。"
                ],
                overall="unready",
                exit_code=1,
                service_user=service_user,
            )

    if workspace_root is None:
        return FullBootstrapResult(
            ok=False,
            planned=[],
            ran=[],
            messages=[
                "Full Profile 需要已初始化的工作区；请先执行 lwa init，再运行 lwa setup --full。"
            ],
            overall="unready",
            exit_code=1,
            service_user=service_user,
        )
    state = load_profile_state(workspace_root) if workspace_root else {}
    completed = set(state.get("completedSteps") or [])
    messages: list[str] = []

    if resume and state:
        messages.append(
            f"恢复 Full Profile 安装（serviceUser={state.get('serviceUser') or service_user}）…"
        )
        service_user = state.get("serviceUser") or service_user

    planned = plan_full_install(platform=plat, runner=detect_runner)
    docker = detect_docker_engine(runner=detect_runner)
    if docker.status == "daemon_down":
        messages.append(
            "检测到 Docker CLI 但 daemon 未运行：请先启动 Docker Desktop / "
            "`sudo systemctl start docker`，本期不会因此重装。"
        )

    ran: list[InstallPlanItem] = []
    if not planned:
        messages.append("Caddy / Docker Engine / Compose 均已达到最低要求，无需安装。")
        completed.add("components_installed")
    else:
        listing = "\n".join(f"  · {p.kind}: {p.script} ({p.reason})" for p in planned)
        prompt = (
            "将安装/升级以下组件（需管理员权限，可能调用 sudo / brew）：\n"
            f"{listing}\n是否继续？[y/N] "
        )
        confirmed = yes
        if not confirmed:
            if confirm is not None:
                confirmed = confirm(prompt)
            elif _stdin_is_interactive():
                confirmed = input(prompt).strip().lower() in {"y", "yes"}
            else:
                messages.append("非交互终端且未传 --yes：跳过自动安装。可手动执行：\n" + listing)
                return FullBootstrapResult(
                    ok=False,
                    planned=planned,
                    ran=[],
                    messages=messages,
                    skipped_no_confirm=True,
                    overall="unready",
                    exit_code=1,
                    service_user=service_user,
                )
        if not confirmed:
            messages.append("用户取消安装。")
            return FullBootstrapResult(
                ok=False,
                planned=planned,
                ran=[],
                messages=messages,
                skipped_no_confirm=True,
                overall="unready",
                exit_code=1,
                service_user=service_user,
            )
        if runner is None and _plan_requires_sudo(planned):
            # S1-06 / #55：真实执行前完成 sudo 预认证，非交互环境提前拒绝
            sudo_problem = _sudo_preflight()
            if sudo_problem is not None:
                messages.append(sudo_problem)
                return FullBootstrapResult(
                    ok=False,
                    planned=planned,
                    ran=ran,
                    messages=messages,
                    overall="unready",
                    exit_code=1,
                    service_user=service_user,
                )
        log_path = _bootstrap_log_path(workspace_root) if runner is None and workspace_root else None
        run = runner or (lambda cmd: _default_subprocess_run(cmd, log_path=log_path))
        for idx, item in enumerate(planned):
            messages.append(f"执行：bash {item.script}")
            result = run(["bash", str(item.script)])
            if result.returncode != 0:
                messages.append(
                    f"脚本失败（exit {result.returncode}）：{item.script}"
                    + _script_failure_detail(result)
                )
                done_names = ", ".join(i.script.name for i in ran) or "无"
                pending_names = ", ".join(i.script.name for i in planned[idx:])
                messages.append(f"已完成阶段：{done_names}；未完成阶段：{pending_names}")
                if workspace_root:
                    save_profile_state(
                        workspace_root,
                        {
                            "profile": "full",
                            "serviceUser": service_user,
                            "overall": "unready",
                            "sessionRefreshRequired": False,
                            "completedSteps": sorted(completed),
                            "action": "修复安装失败后重试：lwa setup --full --resume",
                        },
                    )
                return FullBootstrapResult(
                    ok=False,
                    planned=planned,
                    ran=ran,
                    messages=messages,
                    overall="unready",
                    exit_code=1,
                    service_user=service_user,
                )
            ran.append(item)
        completed.add("components_installed")

    # 版本复检
    docker_after = detect_docker_engine(runner=detect_runner)
    compose_after = detect_docker_compose(runner=detect_runner)
    caddy_after = detect_caddy(runner=detect_runner)
    components_ok = (
        docker_after.status == "ok" and compose_after.status == "ok" and caddy_after.status == "ok"
    )
    if docker_after.status == "daemon_down":
        messages.append("安装完成但 Docker daemon 未起，请启动后再验。")
        components_ok = False
    if not components_ok:
        # 复检已证明安装完成标记过期；清掉后 --resume 才能重新规划/安装。
        completed.discard("components_installed")
        completed.discard("components_verified")
        messages.append(
            f"复检未全绿：docker={docker_after.status} "
            f"compose={compose_after.status} caddy={caddy_after.status}"
        )
        if workspace_root:
            save_profile_state(
                workspace_root,
                {
                    "profile": "full",
                    "serviceUser": service_user,
                    "overall": "unready",
                    "sessionRefreshRequired": False,
                    "completedSteps": sorted(completed),
                    "action": "lwa setup --full --resume",
                },
            )
        return FullBootstrapResult(
            ok=False,
            planned=planned,
            ran=ran,
            messages=messages,
            overall="unready",
            exit_code=1,
            service_user=service_user,
        )
    messages.append("复检通过：Caddy / Docker Engine / Compose 均满足最低版本。")
    completed.add("components_verified")

    # 权限/能力验收（当前 CLI 进程）
    docker_access = probe_docker_access_state()
    if docker_access == "permission_denied":
        messages.append(
            "当前进程仍无 Docker 权限（常见于刚加入 docker 组）。"
            "请重新登录或 newgrp docker 后执行：lwa setup --full --resume"
        )
        if workspace_root:
            save_profile_state(
                workspace_root,
                {
                    "profile": "full",
                    "serviceUser": service_user,
                    "overall": "session_refresh_required",
                    "sessionRefreshRequired": True,
                    "completedSteps": sorted(completed),
                    "action": "重新登录后执行：lwa setup --full --resume",
                },
            )
            _persist_full_config(workspace_root, service_user, ready=False)
        return FullBootstrapResult(
            ok=False,
            planned=planned,
            ran=ran,
            messages=messages,
            overall="session_refresh_required",
            session_refresh_required=True,
            exit_code=2,
            service_user=service_user,
        )

    if workspace_root:
        # BUG-234：尽量拉起 gateway/manager/daemon，让后台以真实身份写能力缓存后再验收
        _try_start_backends_for_capability(workspace_root, messages)
        report = collect_capability_report(
            workspace_root=workspace_root,
            profile="full",
            role="cli",
            include_backend_cached=True,
        )
        if report.overall != "ready":
            # issue #29：CLI 视角 Docker 已 ready 而 manager/daemon 缓存仍
            # unavailable（macOS 重启后 Docker Desktop 晚于 LWA 就绪）时，主动触发
            # manager 即时重探并等 daemon 短退避探针收敛后复验一轮，避免复读
            # 「doctor + resume」建议造成永不收敛的死循环。
            report = _refresh_backend_capability_once(workspace_root, report, messages)
        if report.overall != "ready":
            messages.append(
                "能力验收未通过（Full 强制闭环）："
                f"overall={report.overall} "
                f"cliDocker={report.cli_docker_access} "
                f"managerDocker={report.manager_docker_access} "
                f"daemonDocker={report.daemon_docker_access} "
                f"caddyBinary={report.caddy_binary} "
                f"caddyRuntime={report.caddy_runtime} "
                f"caddyOwner={report.caddy_owner} "
                f"caddyWorkspace={report.caddy_workspace_access} "
                f"gatewayAccess={report.gateway_access}"
            )
            if report.action:
                messages.append(f"建议：{report.action}")
            save_profile_state(
                workspace_root,
                {
                    "profile": "full",
                    "serviceUser": service_user,
                    "overall": report.overall,
                    "sessionRefreshRequired": bool(report.session_refresh_required),
                    "completedSteps": sorted(completed),
                    "action": report.action or "lwa doctor --profile full",
                },
            )
            _persist_full_config(workspace_root, service_user, ready=False)
            exit_overall = (
                "session_refresh_required" if report.session_refresh_required else "unready"
            )
            return FullBootstrapResult(
                ok=False,
                planned=planned,
                ran=ran,
                messages=messages,
                overall=exit_overall,
                session_refresh_required=bool(report.session_refresh_required),
                exit_code=2 if report.session_refresh_required else 1,
                service_user=service_user,
            )
        completed.add("capability_closed_loop")
        save_profile_state(
            workspace_root,
            {
                "profile": "full",
                "serviceUser": service_user,
                "overall": "ready",
                "sessionRefreshRequired": False,
                "completedSteps": sorted(completed),
                "action": None,
            },
        )
        _persist_full_config(workspace_root, service_user, ready=True)
        messages.append("Full Profile 能力验收通过（CLI + manager + daemon + Caddy + gateway）。")

    return FullBootstrapResult(
        ok=True,
        planned=planned,
        ran=ran,
        messages=messages,
        overall="ready",
        exit_code=0,
        service_user=service_user,
    )


def _try_start_backends_for_capability(workspace_root: Path, messages: list[str]) -> None:
    """setup --full 验收前尽量启动后台，以便写入真实能力缓存（BUG-234/235）。"""
    import time

    from local_webpage_access.config import load_config
    from local_webpage_access.paths import Workspace

    ws = Workspace(Path(workspace_root))
    if not ws.config_path.is_file():
        messages.append("工作区尚无 local-web.yml，跳过后台能力闭环拉起。")
        return
    try:
        config = load_config(ws)
    except Exception as exc:  # noqa: BLE001
        messages.append(f"加载配置失败，跳过后台拉起：{exc}")
        return

    # gateway → manager → daemon（与 Full 启停顺序一致）
    try:
        from local_webpage_access.gateway_service import start_gateway

        start_gateway(ws, config)
        messages.append("已尝试启动 gateway（Caddy 监管）。")
    except Exception as exc:  # noqa: BLE001
        messages.append(f"启动 gateway 未成功（继续验收）：{exc}")

    try:
        from local_webpage_access.manager_service import start_manager

        if getattr(config, "managerEnabled", True):
            start_manager(ws, config)
            messages.append("已尝试启动 manager。")
    except Exception as exc:  # noqa: BLE001
        messages.append(f"启动 manager 未成功（继续验收）：{exc}")

    try:
        from local_webpage_access.daemon import start_daemon

        start_daemon(ws, config)
        messages.append("已尝试启动 daemon。")
    except Exception as exc:  # noqa: BLE001
        messages.append(f"启动 daemon 未成功（继续验收）：{exc}")

    # 给子进程写入 capability-*.json 的短窗口
    time.sleep(1.5)


# issue #29：等待 daemon 短退避探针把能力缓存刷出 Docker 未就绪的上限/轮询间隔。
_CAPABILITY_CONVERGE_WAIT_TIMEOUT = 90.0
_CAPABILITY_CONVERGE_WAIT_INTERVAL = 5.0


def _request_manager_capability_refresh(workspace_root: Path, messages: list[str]) -> bool:
    """请求本机 manager 立即重探能力（``GET /api/capability?refresh=true``）。

    issue #29：macOS 重启后 Docker Desktop 晚于 LWA 就绪时，manager/daemon 缓存
    仍是 unavailable；resume 验收应主动触发即时重探而非干等 300s 周期。本机
    loopback 的 GET 免 token（IMP-003）；带上 run/manager-token.json 仅为兼容
    非典型绑定。触发失败不致命——按现有缓存继续验收。
    """
    import json
    import urllib.request

    from local_webpage_access.config import load_config
    from local_webpage_access.manager_service import _health_check_host
    from local_webpage_access.paths import Workspace
    from local_webpage_access.ports import format_http_host
    from local_webpage_access.probe import urlopen_direct

    ws = Workspace(Path(workspace_root))
    if not ws.config_path.is_file():
        return False
    try:
        config = load_config(ws)
    except Exception:  # noqa: BLE001
        return False
    host = _health_check_host(str(getattr(config, "managerHost", "") or "0.0.0.0"))
    port = int(getattr(config, "managerPort", 0) or 17800)
    # BUG-628：IPv6 字面量须加方括号（managerHost=:: 时 _health_check_host 得
    # ::1，裸拼 host:port 因冒号歧义产生非法 URL，重探静默失败提前返回旧报告）。
    url = f"http://{format_http_host(host)}:{port}/api/capability?refresh=true"
    req = urllib.request.Request(url)
    try:
        token_data = json.loads((ws.run / "manager-token.json").read_text(encoding="utf-8"))
        token = str(token_data.get("token") or "").strip()
    except (OSError, ValueError):
        token = ""
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        # refresh=true 为同步探测，Docker 超时场景可达 10s+，给足上限。
        with urlopen_direct(req, timeout=30.0) as resp:  # noqa: S310
            if resp.status != 200:
                messages.append(f"触发 manager 能力重探失败（HTTP {resp.status}），按缓存验收。")
                return False
        messages.append("已触发本机 manager 即时能力重探（/api/capability?refresh=true）。")
        return True
    except Exception as exc:  # noqa: BLE001 — 触发失败不致命，按现有缓存验收
        messages.append(f"触发 manager 能力重探未成功（继续按缓存验收）：{exc}")
        return False


def _await_daemon_capability_ready(
    workspace_root: Path,
    *,
    timeout: float = _CAPABILITY_CONVERGE_WAIT_TIMEOUT,
    interval: float = _CAPABILITY_CONVERGE_WAIT_INTERVAL,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> bool:
    """等 daemon 短退避探针把 ``capability-daemon.json`` 刷出 Docker 未就绪。

    issue #29：配合 daemon/manager 的短退避快探（10s→20s→40s，封顶 60s），
    resume 验收给出一个有上界的收敛窗口，而不是立刻复述旧建议。
    """
    import json
    import time

    from local_webpage_access.capability import _cache_is_fresh, docker_state_pending

    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    path = Path(workspace_root) / "run" / "capability-daemon.json"
    deadline = clock() + max(0.0, timeout)
    while True:
        state: object = None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict) and _cache_is_fresh(data):
            caps = data.get("capabilities")
            if isinstance(caps, dict):
                state = caps.get("daemonDockerAccess")
        if state is not None and not docker_state_pending(state):
            return True
        if clock() >= deadline:
            return False
        sleep(interval)


def _refresh_backend_capability_once(
    workspace_root: Path,
    report: CapabilityReport,
    messages: list[str],
) -> CapabilityReport:
    """issue #29：后台 Docker 能力缓存陈旧时触发即时重探并复验一轮。

    仅在「CLI 视角 cliDockerAccess=ready 且 manager/daemon 缓存为
    unavailable/daemon_unavailable」时介入；其余失败原因（权限、owner、
    caddy 等）原样返回，由既有失败路径给出真实剩余阻塞原因。
    """
    from local_webpage_access.capability import (
        collect_capability_report,
        docker_state_pending,
    )

    if report.cli_docker_access != "ready":
        return report
    if not (
        docker_state_pending(report.manager_docker_access)
        or docker_state_pending(report.daemon_docker_access)
    ):
        return report
    if not _request_manager_capability_refresh(workspace_root, messages):
        return report
    if docker_state_pending(report.daemon_docker_access):
        if _await_daemon_capability_ready(workspace_root):
            messages.append("daemon 能力缓存已随短退避探针收敛。")
        else:
            messages.append("等待 daemon 能力缓存收敛超时，按最新状态复验。")
    return collect_capability_report(
        workspace_root=workspace_root,
        profile="full",
        role="cli",
        include_backend_cached=True,
    )


def _persist_full_config(workspace_root: Path, service_user: str, *, ready: bool) -> None:
    """把 profile/serviceUser 写回 local-web.yml（平滑，不破坏其它字段）。"""
    try:
        from local_webpage_access.config import load_config
        from local_webpage_access.paths import Workspace

        ws = Workspace(workspace_root)
        cfg = load_config(ws)
        # BUG-305：只有完整能力验收通过后才把运行配置提升为 full。
        # 安装失败/待刷新仍由 full-setup-state.json 记录恢复信息，不能让日常
        # builtin 路径提前进入严格模式而失去自愈入口。
        if ready:
            cfg.profile = "full"
        cfg.serviceUser = service_user
        if ready and cfg.staticGateway == "builtin":
            # Full ready 仍尊重用户显式 builtin；能力验收已在上层处理
            pass
        cfg.save(ws.config_path)
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger("local_webpage_access.host_bootstrap").warning(
            "写回 local-web.yml profile 失败：%s", exc
        )


def maybe_offer_docker_install(
    *,
    platform: str | None = None,
    install_docker: bool | None = None,
    confirm: Callable[[str], bool] | None = None,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
    detect_runner: SubprocessRunner = _default_runner,
) -> DockerOfferResult:
    """default 档：缺失 Docker 时询问（或按 flag）是否执行内置脚本。

    ``install_docker``：True 强制装；False 强制不装；None 则 TTY 询问 / 非 TTY 跳过。
    返回结构化结果供 CLI 决定退出码，并在成功安装后复检（BUG-197）。
    """
    messages: list[str] = []
    state = detect_docker_engine(runner=detect_runner)
    if state.status == "daemon_down":
        messages.append(
            f"Docker CLI 已安装（{state.version or '?'}）但 daemon 未运行，"
            "请启动 Docker 后再继续（不会自动重装）。"
        )
        return DockerOfferResult(messages=messages)
    if state.status == "outdated":
        script = resolve_install_script("docker", platform)
        messages.append(
            f"Docker Engine {state.version} < {MIN_DOCKER_VERSION}。可手动升级：bash {script}"
        )
        return DockerOfferResult(messages=messages)
    if not should_offer_docker_install(state):
        return DockerOfferResult(messages=messages)

    script = resolve_install_script("docker", platform)
    if install_docker is False:
        messages.append(f"已跳过 Docker 安装。需要时执行：bash {script}")
        return DockerOfferResult(messages=messages)

    do_install = install_docker is True
    if install_docker is None:
        if _stdin_is_interactive():
            prompt = (
                f"未检测到 Docker Engine。是否执行内置安装脚本（阿里云源，路径 {script}）？[y/N] "
            )
            if confirm is not None:
                do_install = confirm(prompt)
            else:
                do_install = input(prompt).strip().lower() in {"y", "yes"}
        else:
            messages.append(f"非交互终端：跳过 Docker 安装询问。需要时执行：bash {script}")
            return DockerOfferResult(messages=messages)

    if not do_install:
        messages.append(f"已跳过 Docker 安装。需要时执行：bash {script}")
        return DockerOfferResult(messages=messages)

    run = runner or _default_subprocess_run
    messages.append(f"执行：bash {script}")
    result = run(["bash", str(script)])
    if result.returncode != 0:
        messages.append(
            f"Docker 安装脚本失败（exit {result.returncode})" + _script_failure_detail(result)
        )
        return DockerOfferResult(
            messages=messages, attempted=True, script_ok=False, recheck_ok=False
        )

    after = detect_docker_engine(runner=detect_runner)
    compose_after = detect_docker_compose(runner=detect_runner)
    recheck = after.status == "ok" and compose_after.status == "ok"
    if after.status == "daemon_down":
        messages.append("安装脚本已完成，但 Docker daemon 未起：请启动 Desktop / dockerd 后再验。")
        recheck = False
    elif recheck:
        messages.append(
            f"Docker 安装并复检通过：Engine {after.version}，Compose {compose_after.version}。"
        )
    else:
        messages.append(
            f"安装脚本已执行，但复检未通过：docker={after.status} compose={compose_after.status}"
        )
    return DockerOfferResult(
        messages=messages,
        attempted=True,
        script_ok=True,
        recheck_ok=recheck,
    )


def format_script_catalog(*, full: bool = False, plat: str | None = None) -> str:
    """供 ``lwa setup --script`` / ``--script --full`` 打印路径与用法。"""
    lines: list[str] = []
    try:
        script_plat = _script_platform(plat)
    except FileNotFoundError as exc:
        return str(exc)

    docker = resolve_install_script("docker", script_plat)
    lines.append(f"# Docker Engine + Compose（{script_plat}）")
    lines.append("# 参考：https://docs.docker.com/engine/install/ubuntu/")
    lines.append(f"bash {docker}")
    lines.append("")
    if full:
        caddy = resolve_install_script("caddy", script_plat)
        lines.append(f"# Caddy（{script_plat}）")
        lines.append(f"bash {caddy}")
        lines.append("")
        lines.append("# 或一次装配：lwa setup --full --yes")
    return "\n".join(lines)


__all__ = [
    "BootstrapProfile",
    "ComponentNeed",
    "DockerEngineState",
    "DockerOfferResult",
    "FullBootstrapResult",
    "InstallPlanItem",
    "detect_caddy",
    "detect_docker_compose",
    "detect_docker_engine",
    "format_script_catalog",
    "maybe_offer_docker_install",
    "plan_full_install",
    "resolve_install_script",
    "resolve_profile",
    "run_full_bootstrap",
    "should_offer_docker_install",
]
