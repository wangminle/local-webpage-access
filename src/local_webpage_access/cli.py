"""``lwa`` CLI 入口。

命令语义见 V1 设计说明第 10 节。Phase 0~2 先实现 init/version；
后续 import/scan/start/stop 等命令在各阶段逐步补充。
"""

from __future__ import annotations

import sys

import typer

from local_webpage_access import PRODUCT_NAME, __version__
from local_webpage_access.errors import LwaError
from local_webpage_access.logging import get_logger, setup_logging

app = typer.Typer(
    name="lwa",
    help=f"{PRODUCT_NAME} — 面向局域网小主机的本地网页部署基座",
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
    f"""{PRODUCT_NAME} 命令行工具。"""
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


def _open_workspace_registry():
    """定位工作区并打开 registry，返回 (workspace, config, registry)。"""
    from local_webpage_access.config import load_config
    from local_webpage_access.paths import require_workspace
    from local_webpage_access.registry import Registry

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
    from local_webpage_access.importer import Importer

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
    from local_webpage_access.importer import apply_detection_to_manifest
    from local_webpage_access.models import InstanceManifest, Status
    from local_webpage_access.scanner import Scanner

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
    """启动实例（静态 / 前端 / 容器统一入口）。"""
    from local_webpage_access.lifecycle import start_instance

    try:
        ws, config, reg = _open_workspace_registry()
        try:
            manifest = start_instance(ws, config, reg, instance_id)
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
    """停止实例（禁用静态路由 / 容器 compose stop，不删数据）。"""
    from local_webpage_access.lifecycle import stop_instance_op

    try:
        ws, config, reg = _open_workspace_registry()
        try:
            stop_instance_op(ws, config, reg, instance_id)
        finally:
            reg.close()
        typer.secho(f"已停止实例：{instance_id}", fg=typer.colors.GREEN)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command()
def restart(instance_id: str = typer.Argument(..., help="要重启的实例 ID")) -> None:
    """重启实例（先停再启，容器走轻量 compose start）。"""
    from local_webpage_access.lifecycle import restart_instance

    try:
        ws, config, reg = _open_workspace_registry()
        try:
            restart_instance(ws, config, reg, instance_id)
        finally:
            reg.close()
        typer.secho(f"已重启实例：{instance_id}", fg=typer.colors.GREEN)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command()
def rebuild(instance_id: str = typer.Argument(..., help="要重建的实例 ID")) -> None:
    """重建实例（强制重新构建镜像 / 产物，经构建队列限流）。"""
    from local_webpage_access.lifecycle import rebuild_instance

    try:
        ws, config, reg = _open_workspace_registry()
        try:
            rebuild_instance(ws, config, reg, instance_id)
        finally:
            reg.close()
        typer.secho(f"已重建实例：{instance_id}", fg=typer.colors.GREEN)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command()
def remove(
    instance_id: str = typer.Argument(..., help="要移除的实例 ID"),
    purge: bool = typer.Option(False, "--purge", help="同时删除 apps/<id>/ 磁盘文件"),
    force: bool = typer.Option(
        False, "--force", help="purge 时强制删除非空 data/（默认保护）"
    ),
) -> None:
    """移除实例（默认保留磁盘文件与 data/，仅删 registry 索引）。"""
    from local_webpage_access.lifecycle import remove_instance

    try:
        ws, config, reg = _open_workspace_registry()
        try:
            remove_instance(ws, config, reg, instance_id, purge=purge, force=force)
        finally:
            reg.close()
        if purge:
            typer.secho(f"已移除实例（含磁盘文件）：{instance_id}", fg=typer.colors.GREEN)
        else:
            typer.secho(
                f"已移除实例（保留磁盘文件）：{instance_id}", fg=typer.colors.GREEN
            )
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command()
def logs(
    instance_id: str = typer.Argument(..., help="实例 ID"),
    category: str = typer.Option(
        "run", "--category", "-c", help="日志分类：build/run/gateway/import/scan"
    ),
    tail: int = typer.Option(200, "--tail", "-n", help="显示最近 N 行"),
) -> None:
    """查看实例日志（默认 run，可选 build/gateway 等）。"""
    from local_webpage_access.logs import list_logs, read_log

    try:
        ws, _config, reg = _open_workspace_registry()
        try:
            text = read_log(ws, instance_id, category, tail=tail)
            if not text:
                available = [i.category for i in list_logs(ws, instance_id)]
                hint = f"（可用分类：{', '.join(available) or '无'}）" if available else ""
                typer.secho(
                    f"日志 {category}.log 不存在或为空{hint}", fg=typer.colors.YELLOW
                )
            else:
                typer.echo(text)
        finally:
            reg.close()
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command()
def status(
    instance_id: str = typer.Argument(None, help="实例 ID（省略则显示全部）"),
) -> None:
    """查看实例状态（省略 ID 时显示所有实例）。"""
    from local_webpage_access.status import all_statuses, instance_status, sync_status

    try:
        ws, config, reg = _open_workspace_registry()
        try:
            sync_status(ws, config, reg, instance_id)
            if instance_id:
                statuses = [instance_status(ws, config, reg, instance_id)]
            else:
                statuses = all_statuses(ws, config, reg)
        finally:
            reg.close()

        if not statuses:
            typer.echo("（暂无实例）")
            return
        typer.echo(
            f"{'ID':20} {'KIND':8} {'RUNTIME':16} {'STATUS':10} {'DESIRED':10} {'PORT':6} NAME"
        )
        for s in statuses:
            port = str(s.host_port) if s.host_port else "-"
            typer.echo(
                f"{s.id[:20]:20} {s.kind:8} {s.runtime:16} "
                f"{s.status:10} {s.desired_state:10} {port:6} {s.name}"
            )
            if s.last_error:
                typer.secho(f"  ↳ lastError: {s.last_error}", fg=typer.colors.RED)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command()
def stats(
    instance_id: str = typer.Argument(None, help="实例 ID（省略则显示全部+整机）"),
) -> None:
    """查看资源占用（整机 + 实例目录/容器资源）。"""
    from local_webpage_access.stats import (
        all_instance_resources,
        host_resources,
        instance_resources,
    )

    try:
        ws, config, reg = _open_workspace_registry()
        try:
            host = host_resources(root=ws.root)
            if instance_id:
                infos = [instance_resources(ws, config, reg, instance_id)]
            else:
                infos = all_instance_resources(ws, config, reg)
        finally:
            reg.close()

        # 整机
        typer.secho("== 整机 ==", fg=typer.colors.CYAN)
        if host.mem_total_bytes is not None:
            mem_used = host.mem_used_bytes or 0
            typer.echo(
                f"  内存：{_fmt_bytes(mem_used)} / {_fmt_bytes(host.mem_total_bytes)}"
            )
        else:
            typer.echo("  内存：（非 Linux，已跳过）")
        if host.load_avg_1m is not None:
            typer.echo(f"  负载：1m={host.load_avg_1m:.2f} 5m={host.load_avg_5m:.2f}")
        typer.echo(
            f"  磁盘：{_fmt_bytes(host.disk_used_bytes or 0)} / "
            f"{_fmt_bytes(host.disk_total_bytes or 0)}"
        )

        # 实例
        typer.secho("== 实例 ==", fg=typer.colors.CYAN)
        if not infos:
            typer.echo("（暂无实例）")
            return
        for info in infos:
            typer.echo(f"  {info.instance_id}")
            typer.echo(f"    源码：{_fmt_bytes(info.source_size_bytes)}")
            typer.echo(f"    public：{_fmt_bytes(info.public_size_bytes)}")
            typer.echo(f"    data：{_fmt_bytes(info.data_size_bytes)}")
            if info.image_size_bytes is not None:
                typer.echo(f"    镜像：{_fmt_bytes(info.image_size_bytes)}")
            if info.last_memory_bytes is not None:
                typer.echo(f"    容器内存：{_fmt_bytes(info.last_memory_bytes)}")
            if info.last_cpu_percent is not None:
                typer.echo(f"    容器CPU：{info.last_cpu_percent:.2f}%")
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


def _fmt_bytes(n: int | None) -> str:
    """字节数格式化为人类可读。"""
    if n is None:
        return "-"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PiB"


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


# ---- daemon 子命令（WBS-21）-------------------------------------------------

daemon_app = typer.Typer(help="控制 daemon 自动导入模式")


@daemon_app.command("on")
def daemon_on(
    poll: float = typer.Option(
        None,
        "--poll",
        "-p",
        help="inbox 扫描间隔（秒），默认 5",
    ),
) -> None:
    """开启 daemon：自动监听 inbox/，导入并启动可确定的轻量实例。"""
    from local_webpage_access import daemon as daemon_mod

    try:
        ws, config, _reg = _open_workspace_registry()
        # daemon 子进程会自行打开 registry；这里只读状态
        _reg.close()
        pid = daemon_mod.start_daemon(ws, config, poll_interval=poll)
        typer.secho(f"daemon 已启动（pid={pid}）", fg=typer.colors.GREEN)
        typer.echo(f"  监听目录：{ws.inbox}")
        typer.echo("  把 zip 放入 inbox/ 即可自动导入；uncertain/heavy 实例标记 pending。")
        typer.echo("  停止：lwa daemon off")
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@daemon_app.command("off")
def daemon_off() -> None:
    """关闭 daemon：停止自动监听 inbox/。"""
    from local_webpage_access import daemon as daemon_mod
    from local_webpage_access.paths import require_workspace

    try:
        ws = require_workspace()
        stopped = daemon_mod.stop_daemon(ws)
        if stopped:
            typer.secho("daemon 已停止", fg=typer.colors.GREEN)
        else:
            typer.secho(
                "daemon 已请求停止，但子进程未在超时内退出（可能需要手动 kill）",
                fg=typer.colors.YELLOW,
            )
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@daemon_app.command("status")
def daemon_status() -> None:
    """查看 daemon 运行状态。"""
    from local_webpage_access import daemon as daemon_mod
    from local_webpage_access.paths import require_workspace

    try:
        ws = require_workspace()
        info = daemon_mod.daemon_status(ws)
        if info["running"]:
            typer.secho(
                f"daemon 运行中（pid={info['pid']}, poll={info['pollInterval']}s）",
                fg=typer.colors.GREEN,
            )
        elif info["enabled"]:
            typer.secho(
                f"daemon 已标记开启但未检测到进程（pid={info['pid']}）",
                fg=typer.colors.YELLOW,
            )
        else:
            typer.echo("daemon 未开启（lwa daemon on 启动）")
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


app.add_typer(daemon_app, name="daemon")


# ---- manager 子命令（WBS-22.13）---------------------------------------------

manager_app = typer.Typer(help="控制管理页 HTTP 服务")


@manager_app.command("start")
def manager_start(
    host: str = typer.Option(
        None,
        "--host",
        help="监听地址（默认用配置 managerHost，通常是 0.0.0.0 即局域网可达）",
    ),
    port: int = typer.Option(
        None, "--port", help="监听端口（默认用配置 managerPort，通常是 17800）"
    ),
) -> None:
    """启动管理页 HTTP 服务（前台运行，Ctrl+C 退出）。"""
    from local_webpage_access.manager_api import ensure_token, run_manager
    from local_webpage_access.security import assert_no_critical, validate_manager_binding

    try:
        ws, config, _reg = _open_workspace_registry()
        _reg.close()  # manager 会自行打开 registry
        token = ensure_token(ws)
        bind_host = host or config.managerHost
        bind_port = port if port is not None else config.managerPort
        assert_no_critical(
            validate_manager_binding(bind_host, has_token=bool(token), port=bind_port)
        )
        typer.secho(
            f"管理页启动中：http://{bind_host}:{bind_port}", fg=typer.colors.GREEN
        )
        typer.echo(f"  API token：{token}")
        typer.echo("  未带 token 的 /api/* 请求将被拒绝（401）。")
        typer.echo("  把 zip 放进 inbox/ 或在此页面管理已导入实例。Ctrl+C 退出。")
        run_manager(ws, config, host=bind_host, port=bind_port)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


app.add_typer(manager_app, name="manager")


# ---- setup 子命令（宿主机环境）----------------------------------------------


@app.command("setup")
def setup_cmd(
    script: bool = typer.Option(
        False, "--script", help="输出当前平台的参考安装脚本（不自动执行）"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出 JSON 报告"),
    static_gateway: str = typer.Option(
        "caddy",
        "--static-gateway",
        help="预期静态网关（caddy 时 Caddy 为必需；builtin 时 Caddy 为可选）",
    ),
) -> None:
    """检测宿主机工具环境并给出安装指引（可在 ``lwa init`` 之前运行）。

    检查 Python、lwa 包、Docker、Compose、Caddy、Node；不依赖工作区。
    工作区就绪后用 ``lwa doctor`` 做完整诊断（含端口池与 registry）。
    """
    import json as json_mod

    from local_webpage_access.setup import (
        format_setup_report,
        render_setup_script,
        run_setup,
    )

    if script:
        typer.echo(render_setup_script())
        return

    report = run_setup(static_gateway=static_gateway)
    if json_output:
        typer.echo(
            json_mod.dumps(
                {
                    "platform": report.platform,
                    "ready": report.ready,
                    "items": [i.to_dict() for i in report.items],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        typer.echo(format_setup_report(report))
    if not report.ready:
        raise typer.Exit(code=1)


# ---- doctor 子命令（WBS-26）------------------------------------------------


@app.command("doctor")
def doctor_cmd(
    instance_id: str = typer.Argument(
        None, help="可选：对单个实例执行健康诊断"
    ),
    json_output: bool = typer.Option(
        False, "--json", help="输出 JSON 报告（便于脚本解析）"
    ),
) -> None:
    """诊断环境与实例问题（WBS-26）。

    检查 Python/Docker/Compose/端口/registry/磁盘/内存；提供 instance_id 时
    附加该实例的 manifest、状态、最近事件与日志诊断，并给出修复建议。
    """
    import json as json_mod

    from local_webpage_access.doctor import format_report, run_doctor

    try:
        ws, config, _reg = _open_workspace_registry()
        _reg.close()  # doctor 自行打开 registry
        report = run_doctor(ws, config, instance_id=instance_id)
        if json_output:
            typer.echo(
                json_mod.dumps(
                    {
                        "overall": report.overall,
                        "instance_id": report.instance_id,
                        "checks": [c.to_dict() for c in report.checks],
                        "instance_checks": [
                            c.to_dict() for c in report.instance_checks
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            typer.echo(format_report(report))
        if report.has_failures:
            raise typer.Exit(code=1)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


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
