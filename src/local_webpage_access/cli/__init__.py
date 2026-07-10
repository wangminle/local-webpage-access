"""``lwa`` CLI 入口（DEV-044 拆分为 ``cli/`` 包）。

本模块仅保留根 ``app``、全局 callback、``version``/``init`` 核心命令与 ``run()``，
其余命令按功能域拆到子模块，由 :func:`_register_all` 统一挂载：

* :mod:`local_webpage_access.cli.importing` —— import / scan
* :mod:`local_webpage_access.cli.lifecycle`   —— start / stop / restart / rebuild / remove / logs
* :mod:`local_webpage_access.cli.status`      —— status / stats / list
* :mod:`local_webpage_access.cli.system`      —— setup / doctor / update
* :mod:`local_webpage_access.cli.alias`       —— ``lwa alias set/clear`` 子命令组
* :mod:`local_webpage_access.cli.daemon`      —— ``lwa daemon on/off/status`` 子命令组
* :mod:`local_webpage_access.cli.manager`     —— ``lwa manager on/off/status/start`` 子命令组
* :mod:`local_webpage_access.cli.gateway`     —— ``lwa gateway on/off/status`` 子命令组
* :mod:`local_webpage_access.cli.access`      —— ``lwa access refresh/review`` 子命令组

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
    verbose: bool = typer.Option(False, "--verbose", "-v", help="输出 DEBUG 级别日志"),
) -> None:
    f"""{PRODUCT_NAME} 命令行工具。"""
    bootstrap("DEBUG" if verbose else "INFO")


@app.command()
def version() -> None:
    """显示版本号（与 Git commit 主题 ``V0.5.2-Build...`` 对齐）。"""
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
) -> None:
    f"""初始化 {PRODUCT_NAME} 工作区（目录 / 配置 / SQLite registry）。"""
    from pathlib import Path

    from local_webpage_access.init_workspace import init_workspace

    try:
        ws = Path(workspace).resolve()
        summary = init_workspace(ws, force=force)
        typer.secho(f"已初始化工作区：{ws}", fg=typer.colors.GREEN)
        typer.echo(summary)
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
    from local_webpage_access.cli import alias, access, daemon, gateway, manager

    app.add_typer(alias.app, name="alias")
    app.add_typer(access.app, name="access")
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
