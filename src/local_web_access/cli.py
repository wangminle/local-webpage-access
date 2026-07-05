"""``lwa`` CLI 入口。

命令语义见 V1 设计说明第 10 节。Phase 0~2 先实现 init/version；
后续 import/scan/start/stop 等命令在各阶段逐步补充。
"""

from __future__ import annotations

import sys

import typer

from local_web_access import __version__
from local_web_access.errors import LwaError
from local_web_access.logging import get_logger, setup_logging

app = typer.Typer(
    name="lwa",
    help="Local Web Access — 面向局域网小主机的本地网页部署基座",
    no_args_is_help=True,
    add_completion=False,
)

log = get_logger("cli")


def _bootstrap(level: str = "INFO") -> None:
    """在每条命令执行前初始化日志（幂等）。"""
    setup_logging(level=level)  # type: ignore[arg-type]


@app.callback()
def main_callback(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="输出 DEBUG 级别日志"),
) -> None:
    """Local Web Access 命令行工具。"""
    _bootstrap("DEBUG" if verbose else "INFO")


@app.command()
def version() -> None:
    """显示版本号。"""
    typer.echo(__version__)


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
    """初始化 Local Web Access 工作区（目录 / 配置 / SQLite registry）。"""
    from pathlib import Path

    from local_web_access.init_workspace import init_workspace

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


def _open_workspace_registry():
    """定位工作区并打开 registry，返回 (workspace, config, registry)。"""
    from local_web_access.config import load_config
    from local_web_access.paths import require_workspace
    from local_web_access.registry import Registry

    ws = require_workspace()
    config = load_config(ws)
    reg = Registry(ws.db_path)
    reg.open()
    return ws, config, reg


@app.command("import")
def import_cmd(
    zip_path: str = typer.Argument(..., help="要导入的 zip 文件路径"),
    name: str = typer.Option(None, "--name", "-n", help="实例显示名称（默认从文件名推导）"),
) -> None:
    """导入一个 zip 包：解压、识别、登记实例。"""
    from pathlib import Path

    from local_web_access.importer import Importer

    try:
        ws, config, reg = _open_workspace_registry()
        try:
            importer = Importer(ws, config, reg)
            result = importer.import_zip(zip_path, name=name)
        finally:
            reg.close()

        typer.secho(f"已导入实例：{result.instance_id}", fg=typer.colors.GREEN)
        typer.echo(f"  名称：{result.manifest.name}")
        typer.echo(f"  形态：{result.detection.form}（置信度 {result.detection.confidence}）")
        typer.echo(f"  类型：{result.manifest.kind} / {result.manifest.runtime}")
        typer.echo(f"  目录：{result.app_dir}")
        typer.echo(f"  sha256：{result.zip_hash}")
        if result.detection.pending:
            typer.secho(
                f"  注意：{result.manifest.lastError}（已标记 pending，需人工或 skill 介入）",
                fg=typer.colors.YELLOW,
            )
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command()
def scan(
    instance_id: str = typer.Argument(None, help="要重新扫描的实例 ID（省略则扫所有 pending）"),
) -> None:
    """重新扫描实例（或所有 pending 实例），刷新运行形态识别。"""
    from local_web_access.importer import apply_detection_to_manifest
    from local_web_access.models import InstanceManifest, Status
    from local_web_access.scanner import Scanner

    try:
        ws, config, reg = _open_workspace_registry()
        try:
            scanner = Scanner()
            ids: list[str]
            if instance_id:
                ids = [instance_id]
            else:
                ids = [
                    row["id"]
                    for row in reg.list_instances()
                    if row["status"] == Status.PENDING.value
                ]

            if not ids:
                typer.echo("没有待扫描的实例。")
                return

            for iid in ids:
                current_dir = ws.app_current(iid)
                detection = scanner.detect(current_dir)
                manifest_path = ws.app_manifest_path(iid)
                if not manifest_path.is_file():
                    typer.secho(f"  {iid}：缺少 local-web.json，跳过", fg=typer.colors.YELLOW)
                    continue
                manifest = InstanceManifest.load(manifest_path)
                manifest = apply_detection_to_manifest(manifest, detection, ws)
                manifest.save(manifest_path)
                reg.upsert_from_manifest(manifest)
                reg.add_event(iid, "scan", f"重新扫描：{detection.form}（{detection.confidence}）")
                status_label = detection.form if not detection.pending else "pending"
                typer.echo(f"  {iid}：{status_label}（{detection.confidence}）")
        finally:
            reg.close()
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command()
def start(instance_id: str = typer.Argument(..., help="要启动的实例 ID")) -> None:
    """启动实例（Phase 2 支持静态 / 前端形态）。"""
    from local_web_access.hosting import host_instance

    try:
        ws, config, reg = _open_workspace_registry()
        try:
            manifest = host_instance(ws, config, reg, instance_id)
        finally:
            reg.close()
        typer.secho(f"已启动实例：{instance_id}", fg=typer.colors.GREEN)
        typer.echo(f"  形态：{manifest.runtime} / {manifest.servingMode}")
        if manifest.network.hostPort:
            typer.echo(f"  端口：{manifest.network.hostPort}")
        if manifest.network.lanUrl:
            typer.echo(f"  局域网：{manifest.network.lanUrl}")
        typer.echo(f"  健康：{manifest.network.healthUrl}")
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command()
def stop(instance_id: str = typer.Argument(..., help="要停止的实例 ID")) -> None:
    """停止实例（禁用静态路由 / 释放端口）。"""
    from local_web_access.hosting import stop_instance

    try:
        ws, config, reg = _open_workspace_registry()
        try:
            stop_instance(ws, config, reg, instance_id)
        finally:
            reg.close()
        typer.secho(f"已停止实例：{instance_id}", fg=typer.colors.GREEN)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command("list")
def list_cmd() -> None:
    """列出所有实例及其状态。"""
    try:
        ws, _config, reg = _open_workspace_registry()
        try:
            rows = reg.list_instances()
            # 构造 instance_id -> port 映射
            port_map: dict[str, int] = {}
            for port in reg.allocated_ports():
                port_map[reg.port_owner(port) or ""] = port
        finally:
            reg.close()
        if not rows:
            typer.echo("（暂无实例）")
            return
        typer.echo(f"{'ID':20} {'KIND':8} {'RUNTIME':16} {'STATUS':10} {'PORT':6} NAME")
        for row in rows:
            port = str(port_map.get(row["id"], "")) or "-"
            typer.echo(
                f"{row['id'][:20]:20} {row['kind']:8} {row['runtime']:16} "
                f"{row['status']:10} {port:6} {row['name']}"
            )
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


def run() -> None:
    """供 ``python -m local_web_access`` 或 entry point 调用。"""
    try:
        app()
    except LwaError as exc:
        # 兜底：子命令内部未捕获的业务异常
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        sys.exit(1)


if __name__ == "__main__":
    run()
