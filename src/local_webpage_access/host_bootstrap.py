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

    docker = detect_docker_engine(runner=runner)
    compose = detect_docker_compose(runner=runner)
    if docker.status in ("missing", "outdated") or compose.status in (
        "missing",
        "outdated",
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
    confirm: Callable[[str], bool] | None = None,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
    detect_runner: SubprocessRunner = _default_runner,
) -> FullBootstrapResult:
    """执行 full 装配：确认 → 跑脚本 → 复检。"""
    plat = platform or detect_platform()
    planned = plan_full_install(platform=plat, runner=detect_runner)
    messages: list[str] = []

    docker = detect_docker_engine(runner=detect_runner)
    if docker.status == "daemon_down":
        messages.append(
            "检测到 Docker CLI 但 daemon 未运行：请先启动 Docker Desktop / "
            "`sudo systemctl start docker`，本期不会因此重装。"
        )

    if not planned:
        messages.append("Caddy / Docker Engine / Compose 均已达到最低要求，无需安装。")
        # daemon_down 仍算未完备
        ok = docker.status == "ok" and detect_caddy(runner=detect_runner).status == "ok"
        if docker.status == "daemon_down":
            ok = False
        return FullBootstrapResult(ok=ok, planned=[], ran=[], messages=messages)

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
            )

    if not confirmed:
        messages.append("用户取消安装。")
        return FullBootstrapResult(
            ok=False,
            planned=planned,
            ran=[],
            messages=messages,
            skipped_no_confirm=True,
        )

    run = runner or _default_subprocess_run
    ran: list[InstallPlanItem] = []
    for item in planned:
        messages.append(f"执行：bash {item.script}")
        result = run(["bash", str(item.script)])
        if result.returncode != 0:
            messages.append(f"脚本失败（exit {result.returncode}）：{item.script}")
            return FullBootstrapResult(
                ok=False, planned=planned, ran=ran, messages=messages
            )
        ran.append(item)

    # 复检
    docker_after = detect_docker_engine(runner=detect_runner)
    compose_after = detect_docker_compose(runner=detect_runner)
    caddy_after = detect_caddy(runner=detect_runner)
    ok = (
        docker_after.status == "ok"
        and compose_after.status in ("ok",)
        and caddy_after.status == "ok"
    )
    if docker_after.status == "daemon_down":
        messages.append("安装完成但 Docker daemon 未起，请启动后再验。")
        ok = False
    if not ok:
        messages.append(
            f"复检未全绿：docker={docker_after.status} "
            f"compose={compose_after.status} caddy={caddy_after.status}"
        )
    else:
        messages.append("复检通过：Caddy / Docker Engine / Compose 均满足最低版本。")
    return FullBootstrapResult(ok=ok, planned=planned, ran=ran, messages=messages)


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
    lines.append(f"# 参考：https://docs.docker.com/engine/install/ubuntu/")
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
