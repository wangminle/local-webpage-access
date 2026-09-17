"""``lwa mcp`` 顶层命令（AGC-W13）：MCP stdio 适配器。

由 ``cli/__init__.py`` 通过 :func:`register` 挂载为顶层命令
（``lwa mcp --workspace <绝对路径>``），不是子命令组。

约束：stdout 只承载 MCP JSON-RPC 帧；一切诊断（缺依赖、工作区校验失败、
业务错误）走 stderr。不隐式 init、不启动 manager。
"""

from __future__ import annotations

from pathlib import Path

import typer

from local_webpage_access.errors import LwaError
from local_webpage_access.paths import CONFIG_FILENAME

MCP_INSTALL_HINT = "pip install 'local-webpage-access[mcp]'"


def register(app: typer.Typer) -> None:
    """把 ``mcp`` 命令挂到根 app（由 ``cli/__init__.py`` 调用一次）。"""

    @app.command("mcp")
    def mcp_stdio(
        workspace: str = typer.Option(
            ...,
            "--workspace",
            help="LWA 工作区根目录的绝对路径（须已 lwa init；本命令不隐式初始化）",
        ),
    ) -> None:
        """以 stdio 传输启动 MCP 服务器，供本机 Agent 客户端接入（stdout 仅协议帧）。"""
        try:
            import mcp as _sdk_check  # noqa: F401
        except ImportError:
            typer.secho(
                f"缺少 MCP SDK 依赖，请先安装：{MCP_INSTALL_HINT}",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1) from None

        root = Path(workspace)
        if not root.is_absolute():
            typer.secho(
                f"--workspace 必须是绝对路径：{workspace!r}",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        if not root.is_dir() or not (root / CONFIG_FILENAME).is_file():
            typer.secho(
                f"工作区不存在或未初始化（缺 {CONFIG_FILENAME}）：{root}\n"
                "请先运行 `lwa init --workspace <路径>`；mcp 不会隐式初始化",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)

        from local_webpage_access.mcp.server import run_stdio_server
        from local_webpage_access.paths import Workspace

        try:
            run_stdio_server(Workspace(root))
        except LwaError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
