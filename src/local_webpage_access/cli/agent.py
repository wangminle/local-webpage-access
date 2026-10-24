"""Agent 协作子命令组（AGC-W14）：``lwa agent connection-info``。

面向本机 Agent 的接入引导：输出连接所需的最小信息（apiBase / workspaceId /
契约版本 / 产品版本）与配置自检（manager 在线性、回环可达性、授权源根目录）。

安全红线（设计 §7）：

- **绝不输出 token**——Agent 通道回环连接同样必须携带管理 token（仅 Header
  方式，见 agent/auth.py），token 也不应出现在 CLI 输出里（BUG-693）；
- 只读打开 registry（``open_readonly``），不隐式 init、不迁移 schema、不扫描磁盘。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from local_webpage_access.cli._common import log
from local_webpage_access.errors import LwaError

app = typer.Typer(help="Agent 协作（M1 本机）：连接信息与配置检查", no_args_is_help=True)


@app.callback()
def _callback() -> None:
    """保持子命令组形态（单命令时 Typer 不会塌缩）。"""


def _open_agent_workspace(workspace: str):
    """按显式 ``--workspace`` 打开工作区（只读 registry，绝不 init）。"""
    from local_webpage_access.config import load_config
    from local_webpage_access.paths import Workspace
    from local_webpage_access.registry import Registry

    root = Path(workspace).expanduser()
    if not root.is_absolute():
        raise LwaError(f"--workspace 必须是绝对路径，得到 {workspace!r}")
    ws = Workspace(root)
    if not ws.config_path.is_file():
        raise LwaError(
            f"{root} 不是 LWA 工作区（缺少 local-web.yml）。"
            f"请先执行 `lwa init -w {root}`。",
        )
    config = load_config(ws)
    reg = Registry(ws.db_path)
    reg.open_readonly()  # 数据库不存在时 RegistryError；不创建、不迁移
    return ws, config, reg


def _loopback_identity_trusted_hint() -> bool:
    """BUG-983：本机视角的回环信任自检，与 manager 启动判定同口径。

    WSL NAT 下回环 peer 可能是 Windows 回环转发，manager 会整体暂停 Agent M1
    通道（``permission_denied``）；此处用同一轻量探测（BUG-982）提前告知 Agent
    「为何 403」。非 WSL 恒可信；探测失败按不可信（与 manager fail-closed 一致）。
    """
    from local_webpage_access.platform_detect import PLATFORM_WSL, detect_platform

    try:
        if detect_platform() != PLATFORM_WSL:
            return True
        from local_webpage_access.wsl_network import (
            MODE_MIRRORED,
            probe_networking_mode_light,
        )

        return probe_networking_mode_light() == MODE_MIRRORED
    except Exception:  # noqa: BLE001 — 自检失败不阻断 connection-info 输出
        return False


def _collect_connection_info(workspace: str) -> dict[str, Any]:
    """汇总连接信息与配置自检结果。"""
    from local_webpage_access.agent.contracts import AGENT_CONTRACT_VERSION
    from local_webpage_access.agent.discovery import AGENT_API_BASE
    from local_webpage_access.agent.auth import LOOPBACK_HOSTS
    from local_webpage_access.manager_service import _fetch_health, _health_check_host
    from local_webpage_access.version_info import resolve_version

    ws, config, reg = _open_agent_workspace(workspace)
    try:
        workspace_id = reg.get_workspace_id()
    finally:
        reg.close()

    port = config.managerPort
    # Agent API 仅接受回环请求（authenticate_local_owner）；bind 通配/回环时可连，
    # 绑定到具体 LAN 地址时回环不可达，属不兼容配置。BUG-694：`localhost` 绑定
    # 同样回环可达，与 MCP bridge 共用 LOOPBACK_HOSTS 判定口径。
    bind_host = config.managerHost
    loopback_reachable = _health_check_host(bind_host) in LOOPBACK_HOSTS
    health = _fetch_health("127.0.0.1", port) if loopback_reachable else None

    return {
        "apiBase": f"http://127.0.0.1:{port}{AGENT_API_BASE}",
        "workspaceId": workspace_id,
        "contractVersion": AGENT_CONTRACT_VERSION,
        "productVersion": resolve_version(),
        "manager": {
            "online": health is not None,
            "bindHost": bind_host,
            "port": port,
            "loopbackReachable": loopback_reachable,
            "loopbackIdentityTrusted": _loopback_identity_trusted_hint(),
        },
        "agent": {
            "allowedSourceRoots": [str(p) for p in config.agent.allowedSourceRoots],
        },
        "workspace": str(ws.root),
    }


@app.command("connection-info")
def agent_connection_info(
    workspace: str = typer.Option(
        ...,
        "--workspace",
        "-w",
        help="LWA 工作区根目录（绝对路径）",
    ),
    as_json: bool = typer.Option(False, "--json", help="以 JSON 输出（供 Agent 消费）"),
) -> None:
    """输出本机 Agent 接入 LWA 所需的连接信息与配置自检。

    输出不含任何凭据：token 由管理员在本机用 ``lwa manager token`` 查看后提供
    （Agent API 回环连接同样要求 token）。
    """
    try:
        info = _collect_connection_info(workspace)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    if as_json:
        typer.echo(json.dumps(info, ensure_ascii=False, indent=2))
        return

    typer.echo(f"apiBase:         {info['apiBase']}")
    typer.echo(f"workspaceId:     {info['workspaceId'] or '(尚未生成，manager 首次启动时分配)'}")
    typer.echo(f"contractVersion: {info['contractVersion']}")
    typer.echo(f"productVersion:  {info['productVersion']}")
    mgr = info["manager"]
    typer.echo(
        "manager:         "
        + ("在线" if mgr["online"] else "离线")
        + f"（bind {mgr['bindHost']}:{mgr['port']}，"
        + ("回环可达" if mgr["loopbackReachable"] else "回环不可达——请把 managerHost 改为 0.0.0.0 或 127.0.0.1")
        + "）"
    )
    if not mgr["loopbackIdentityTrusted"]:
        typer.echo(
            "loopbackIdentityTrusted: false——WSL NAT（或网络模式探测失败）下回环无法证明"
            "本机身份，Agent M1 通道与 lwa mcp 均被 403 permission_denied 暂停；"
            "切换 WSL mirrored 网络后重启 manager 可恢复，管理 API 携带 token 仍可用"
        )
    roots = info["agent"]["allowedSourceRoots"]
    typer.echo(
        "allowedSourceRoots: " + (", ".join(roots) if roots else "(空——server_directory 源将全部被拒绝)")
    )


def register(root_app: typer.Typer) -> None:
    """把 ``lwa agent`` 子命令组挂到根 app。"""
    root_app.add_typer(app, name="agent")
