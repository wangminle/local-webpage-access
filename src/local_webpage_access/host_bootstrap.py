"""宿主机组件安装编排（IMP-031 / IMP-032）。

提供：
- 内置安装脚本定位（Docker / Caddy，macOS / Linux）
- Docker Engine 状态细分（missing / daemon_down / outdated / ok）
- ``--default`` 下可选询问安装 Docker
- ``--full`` 下检查并安装 Caddy + Docker Engine + Compose
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Callable, Literal, Sequence

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
    raise FileNotFoundError(
        f"当前平台 {p} 无内置安装脚本（本期仅 macOS / Linux / WSL）"
    )


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

    raise FileNotFoundError(
        f"找不到内置安装脚本 {name}；请从源码树运行或重新 pip install -e ."
    )


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
    if (
        not skip_distro_docker
        and (
            docker.status in ("missing", "outdated")
            or compose.status in ("missing", "outdated")
        )
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


def _default_subprocess_run(
    cmd: Sequence[str], **kwargs
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        check=False,
        text=True,
        capture_output=False,
        **kwargs,
    )


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
                "Full Profile 需要已初始化的工作区；请先执行 lwa init，再运行 "
                "lwa setup --full。"
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
                messages.append(
                    "非交互终端且未传 --yes：跳过自动安装。可手动执行：\n" + listing
                )
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
        run = runner or _default_subprocess_run
        for item in planned:
            messages.append(f"执行：bash {item.script}")
            result = run(["bash", str(item.script)])
            if result.returncode != 0:
                messages.append(f"脚本失败（exit {result.returncode}）：{item.script}")
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
        docker_after.status == "ok"
        and compose_after.status == "ok"
        and caddy_after.status == "ok"
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
                "session_refresh_required"
                if report.session_refresh_required
                else "unready"
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
        messages.append(
            "Full Profile 能力验收通过（CLI + manager + daemon + Caddy + gateway）。"
        )

    return FullBootstrapResult(
        ok=True,
        planned=planned,
        ran=ran,
        messages=messages,
        overall="ready",
        exit_code=0,
        service_user=service_user,
    )


def _try_start_backends_for_capability(
    workspace_root: Path, messages: list[str]
) -> None:
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


def _persist_full_config(
    workspace_root: Path, service_user: str, *, ready: bool
) -> None:
    """把 profile/serviceUser 写回 local-web.yml（平滑，不破坏其它字段）。"""
    try:
        from local_webpage_access.config import load_config
        from local_webpage_access.paths import Workspace

        ws = Workspace(workspace_root)
        cfg = load_config(ws)
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
            f"Docker Engine {state.version} < {MIN_DOCKER_VERSION}。"
            f"可手动升级：bash {script}"
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
                "未检测到 Docker Engine。是否执行内置安装脚本"
                f"（阿里云源，路径 {script}）？[y/N] "
            )
            if confirm is not None:
                do_install = confirm(prompt)
            else:
                do_install = input(prompt).strip().lower() in {"y", "yes"}
        else:
            messages.append(
                f"非交互终端：跳过 Docker 安装询问。需要时执行：bash {script}"
            )
            return DockerOfferResult(messages=messages)

    if not do_install:
        messages.append(f"已跳过 Docker 安装。需要时执行：bash {script}")
        return DockerOfferResult(messages=messages)

    run = runner or _default_subprocess_run
    messages.append(f"执行：bash {script}")
    result = run(["bash", str(script)])
    if result.returncode != 0:
        messages.append(f"Docker 安装脚本失败（exit {result.returncode}）")
        return DockerOfferResult(
            messages=messages, attempted=True, script_ok=False, recheck_ok=False
        )

    after = detect_docker_engine(runner=detect_runner)
    compose_after = detect_docker_compose(runner=detect_runner)
    recheck = after.status == "ok" and compose_after.status == "ok"
    if after.status == "daemon_down":
        messages.append(
            "安装脚本已完成，但 Docker daemon 未起：请启动 Desktop / dockerd 后再验。"
        )
        recheck = False
    elif recheck:
        messages.append(
            f"Docker 安装并复检通过：Engine {after.version}，"
            f"Compose {compose_after.version}。"
        )
    else:
        messages.append(
            f"安装脚本已执行，但复检未通过："
            f"docker={after.status} compose={compose_after.status}"
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
