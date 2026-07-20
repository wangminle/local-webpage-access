"""``lwa`` CLI 入口（DEV-044 拆分为 ``cli/`` 包）。

本模块仅保留根 ``app``、全局 callback、``version``/``init`` 核心命令与 ``run()``，
其余命令按功能域拆到子模块，由 :func:`_register_all` 统一挂载：

* :mod:`local_webpage_access.cli.importing` —— import / scan
* :mod:`local_webpage_access.cli.lifecycle`   —— start / stop / restart / rebuild / remove / logs
* :mod:`local_webpage_access.cli.status`      —— status / stats / list
* :mod:`local_webpage_access.cli.system`      —— setup / doctor / update
* :mod:`local_webpage_access.cli.alias`       —— ``lwa alias set/clear`` 子命令组
* :mod:`local_webpage_access.cli.daemon`      —— ``lwa daemon on/off/status`` 子命令组
* :mod:`local_webpage_access.cli.manager`     —— ``lwa manager on/off/status/start/logs`` 子命令组
* :mod:`local_webpage_access.cli.gateway`     —— ``lwa gateway on/off/status`` 子命令组
* :mod:`local_webpage_access.cli.access`      —— ``lwa access refresh/review`` 子命令组
* :mod:`local_webpage_access.cli.autostart`   —— ``lwa autostart install/enable/.../check`` 子命令组（IMP-030）

命令语义见 V1 设计说明第 10 节。拆分前后 CLI 行为完全一致（验收：全量 pytest）。
"""

from __future__ import annotations

import sys

import typer

from local_webpage_access import PRODUCT_NAME
from local_webpage_access.cli._common import bootstrap, log
from local_webpage_access.errors import LwaError

app = typer.Typer(
    name="lwa",
    help=f"{PRODUCT_NAME} — 面向局域网小主机的本地网页部署基座",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main_callback(
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", "-v", help="输出 DEBUG 级别日志"),
) -> None:
    f"""{PRODUCT_NAME} 命令行工具。"""
    bootstrap("DEBUG" if verbose else "INFO")
    # IMP-036：除 help / version / 只读 doctor 外，统一平台门禁（写工作区前 fail-fast）
    sub = ctx.invoked_subcommand
    if sub in (None, "version", "doctor"):
        return
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        return
    from local_webpage_access.platform_support import require_supported_platform

    require_supported_platform()


@app.command()
def version() -> None:
    """显示版本号（与 Git commit 主题 ``V0.6.5-Build...`` 对齐）。"""
    from local_webpage_access.version_info import display_version

    typer.echo(display_version())


@app.command()
def init(
    workspace: str = typer.Option(
        ".",
        "--workspace",
        "-w",
        help="工作区根目录，默认当前目录",
    ),
    force: bool = typer.Option(False, "--force", help="已存在时仍重新生成配置和模板"),
    default_profile: bool = typer.Option(
        False,
        "--default",
        help="装配档位：仅初始化工作区（缺省；可询问安装 Docker）",
    ),
    full_profile: bool = typer.Option(
        False,
        "--full",
        help="装配档位：初始化后检查并安装 Caddy + Docker Engine + Compose",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="full 档跳过确认直接安装（非 TTY 时必须）"
    ),
    install_docker: bool = typer.Option(
        False,
        "--install-docker",
        help="default 档：强制执行内置 Docker 安装脚本",
    ),
    no_install_docker: bool = typer.Option(
        False,
        "--no-install-docker",
        help="default 档：跳过 Docker 安装询问",
    ),
    static_gateway: str | None = typer.Option(
        None,
        "--static-gateway",
        help="写入 local-web.yml 的 staticGateway（full 默认 caddy）",
    ),
) -> None:
    f"""初始化 {PRODUCT_NAME} 工作区（目录 / 配置 / SQLite registry）。"""
    from pathlib import Path

    from local_webpage_access.host_bootstrap import (
        maybe_offer_docker_install,
        resolve_profile,
        run_full_bootstrap,
    )
    from local_webpage_access.init_workspace import init_workspace

    try:
        profile = resolve_profile(default=default_profile, full=full_profile)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    if install_docker and no_install_docker:
        typer.secho(
            "--install-docker 与 --no-install-docker 互斥",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    gateway = static_gateway
    if gateway is None and profile == "full":
        gateway = "caddy"

    try:
        ws = Path(workspace).resolve()
        # BUG-260：Full 写路径须在 init_workspace 之前按 WSL+/mnt 门禁 fail-closed
        if profile == "full":
            from local_webpage_access.platform_support import (
                assert_writable_workspace_allowed,
            )

            try:
                assert_writable_workspace_allowed(ws)
            except SystemExit as exc:
                msg = exc.code if isinstance(exc.code, str) else str(exc)
                typer.secho(msg or "Full 写路径已阻断", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=1) from exc

        summary = init_workspace(ws, force=force, static_gateway=gateway)
        typer.secho(f"已初始化工作区：{ws}", fg=typer.colors.GREEN)
        typer.echo(summary)

        if profile == "full":
            typer.secho("\n── 完整装配（--full）──", fg=typer.colors.CYAN)
            # 必须显式传入工作区：init -w 可能与 cwd 不同；且 run_full_bootstrap
            # 在 workspace_root=None 时会直接 unready（BUG-251）。
            boot = run_full_bootstrap(yes=yes, workspace_root=ws)
            for msg in boot.messages:
                typer.echo(msg)
            if not boot.ok:
                raise typer.Exit(code=boot.exit_code or 1)
        else:
            flag: bool | None
            if install_docker:
                flag = True
            elif no_install_docker:
                flag = False
            else:
                flag = None
            offer = maybe_offer_docker_install(install_docker=flag)
            for msg in offer.messages:
                typer.echo(msg)
            # BUG-197：安装失败或复检失败时非零退出
            if offer.attempted and (
                offer.script_ok is False or offer.recheck_ok is False
            ):
                raise typer.Exit(code=1)

        typer.echo("\n下一步：把 zip 放入 inbox/ 后执行 `lwa import inbox/xxx.zip`")
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


def _register_all() -> None:
    """挂载各子模块命令 / 子命令组到根 app。"""
    # 顶层命令（保持 ``lwa <cmd>`` 形式不变）
    from local_webpage_access.cli import (
        importing,
        lifecycle,
        status as status_cmd,
        system,
    )

    importing.register(app)
    lifecycle.register(app)
    status_cmd.register(app)
    system.register(app)

    # 子命令组（保持 ``lwa <group> <sub>`` 形式不变）
    from local_webpage_access.cli import (
        alias,
        access,
        autostart,
        daemon,
        gateway,
        manager,
    )

    app.add_typer(alias.app, name="alias")
    app.add_typer(access.app, name="access")
    app.add_typer(autostart.app, name="autostart")
    app.add_typer(daemon.app, name="daemon")
    app.add_typer(manager.app, name="manager")
    app.add_typer(gateway.app, name="gateway")


_register_all()


def run() -> None:
    """供 ``python -m local_webpage_access`` 或 entry point 调用。"""
    try:
        app()
    except LwaError as exc:
        # 兜底：子命令内部未捕获的业务异常
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        sys.exit(1)


if __name__ == "__main__":
    run()
