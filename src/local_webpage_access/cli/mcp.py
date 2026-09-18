"""``lwa mcp`` 顶层命令（AGC-W13）：MCP stdio 适配器。

由 ``cli/__init__.py`` 通过 :func:`register` 挂载为顶层命令
（``lwa mcp --workspace <绝对路径>``），不是子命令组。

约束：stdout 只承载 MCP JSON-RPC 帧；一切诊断（缺依赖、工作区校验失败、
业务错误）走 stderr。不隐式 init、不启动 manager。

issue #40 问题 1：MCP 客户端（Claude Code / Cursor 等）默认不展示 stderr
的人类可读文本，服务器退出后只报「无响应」——fatal 退出前额外写一行
机器可解析的 JSON 诊断（``{"lwaMcpFatal": …}``），客户端日志可 grep 定位。
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from local_webpage_access.errors import LwaError
from local_webpage_access.paths import CONFIG_FILENAME

MCP_INSTALL_HINT = "pip install 'local-webpage-access[mcp]'"


def _sdk_available() -> bool:
    """MCP SDK（``[mcp]`` extra）是否已安装。"""
    try:
        import mcp as _sdk_check  # noqa: F401

        return True
    except ImportError:
        return False


def _mcp_fatal(code: str, message: str, *, hint: str | None = None) -> None:
    """fatal 退出前的双通道诊断：stderr 先写一行机器可解析 JSON，再写人类文本。

    JSON 形如 ``{"lwaMcpFatal": "<code>", "message": …, "hint": …}``——
    单行、无前缀，MCP 客户端日志里可直接 grep ``lwaMcpFatal``。
    """
    payload: dict[str, str] = {"lwaMcpFatal": code, "message": message}
    if hint:
        payload["hint"] = hint
    typer.secho(json.dumps(payload, ensure_ascii=False), err=True)
    typer.secho(message + (f"\n修复：{hint}" if hint else ""), fg=typer.colors.RED, err=True)


def register(app: typer.Typer) -> None:
    """把 ``mcp`` 命令挂到根 app（由 cli/__init__.py 调用一次）。"""

    @app.command("mcp")
    def mcp_stdio(
        workspace: str = typer.Option(
            ...,
            "--workspace",
            help="LWA 工作区根目录的绝对路径（须已 lwa init；本命令不隐式初始化）",
        ),
    ) -> None:
        """以 stdio 传输启动 MCP 服务器，供本机 Agent 客户端接入（stdout 仅协议帧）。"""
        if not _sdk_available():
            _mcp_fatal(
                "missing-dependency",
                "缺少 MCP SDK 依赖，无法启动 stdio 服务器",
                hint=MCP_INSTALL_HINT,
            )
            raise typer.Exit(code=1)

        root = Path(workspace)
        if not root.is_absolute():
            _mcp_fatal(
                "workspace-not-absolute",
                f"--workspace 必须是绝对路径：{workspace!r}",
                hint="使用 lwa agent connection-info 获取工作区绝对路径",
            )
            raise typer.Exit(code=1)
        if not root.is_dir() or not (root / CONFIG_FILENAME).is_file():
            _mcp_fatal(
                "workspace-invalid",
                f"工作区不存在或未初始化（缺 {CONFIG_FILENAME}）：{root}",
                hint="请先运行 `lwa init --workspace <路径>`；mcp 不会隐式初始化",
            )
            raise typer.Exit(code=1)

        from local_webpage_access.mcp.server import run_stdio_server
        from local_webpage_access.paths import Workspace

        try:
            run_stdio_server(Workspace(root))
        except LwaError as exc:
            _mcp_fatal("lwa-error", str(exc), hint=None)
            raise typer.Exit(code=1) from exc
