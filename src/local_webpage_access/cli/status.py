"""状态查看命令：``lwa status`` / ``lwa stats`` / ``lwa list`` / ``lwa pageviews``。

DEV-044（WBS-20260708 阶段5.1）：从原 ``cli.py`` 按功能域拆出。
注意：本模块路径为 ``local_webpage_access.cli.status``，与数据层的
``local_webpage_access.status`` 不同（前者是 CLI 命令，后者是状态快照模型）。
"""

from __future__ import annotations

import typer

from local_webpage_access.cli._common import fmt_bytes, log, open_workspace_registry
from local_webpage_access.errors import LwaError


def status(
    instance_id: str = typer.Argument(None, help="实例 ID（省略则显示全部）"),
) -> None:
    """查看实例状态（省略 ID 时显示所有实例）。"""
    from local_webpage_access.status import all_statuses, instance_status, sync_status

    try:
        ws, config, reg = open_workspace_registry()
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
            # IMP-007：容器实例展示端口映射（internalPort→hostPort）
            if s.port_mapping_label:
                typer.echo(f"  ↳ 映射：{s.port_mapping_label}")
            # IMP-047：文件夹源标识
            if s.source_kind == "folder":
                typer.echo(f"  ↳ 来源：本机文件夹 {s.source_dir_path or ''}")
            # IMP-006：路径别名入口 URL
            if s.route_url:
                typer.secho(f"  ↳ 路径：{s.route_url}", fg=typer.colors.CYAN)
            if s.last_error:
                typer.secho(f"  ↳ lastError: {s.last_error}", fg=typer.colors.RED)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


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
        ws, config, reg = open_workspace_registry()
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
                f"  内存：{fmt_bytes(mem_used)} / {fmt_bytes(host.mem_total_bytes)}"
            )
        else:
            typer.echo("  内存：（非 Linux，已跳过）")
        if host.load_avg_1m is not None:
            typer.echo(f"  负载：1m={host.load_avg_1m:.2f} 5m={host.load_avg_5m:.2f}")
        typer.echo(
            f"  磁盘：{fmt_bytes(host.disk_used_bytes or 0)} / "
            f"{fmt_bytes(host.disk_total_bytes or 0)}"
        )

        # 实例
        typer.secho("== 实例 ==", fg=typer.colors.CYAN)
        if not infos:
            typer.echo("（暂无实例）")
            return
        for info in infos:
            typer.echo(f"  {info.instance_id}")
            typer.echo(f"    源码：{fmt_bytes(info.source_size_bytes)}")
            typer.echo(f"    public：{fmt_bytes(info.public_size_bytes)}")
            typer.echo(f"    data：{fmt_bytes(info.data_size_bytes)}")
            if info.image_size_bytes is not None:
                typer.echo(f"    镜像：{fmt_bytes(info.image_size_bytes)}")
            if info.last_memory_bytes is not None:
                typer.echo(f"    容器内存：{fmt_bytes(info.last_memory_bytes)}")
            if info.last_cpu_percent is not None:
                typer.echo(f"    容器CPU：{info.last_cpu_percent:.2f}%")
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


def list_cmd() -> None:
    """列出所有实例及其状态。"""
    from local_webpage_access.status import all_statuses

    try:
        ws, config, reg = open_workspace_registry()
        try:
            statuses = all_statuses(ws, config, reg)
        finally:
            reg.close()
        if not statuses:
            typer.echo("（暂无实例）")
            return
        typer.echo(f"{'ID':20} {'KIND':8} {'RUNTIME':16} {'STATUS':10} {'PORT':6} NAME")
        for s in statuses:
            port = str(s.host_port) if s.host_port else "-"
            typer.echo(
                f"{s.id[:20]:20} {s.kind:8} {s.runtime:16} "
                f"{s.status:10} {port:6} {s.name}"
            )
            # IMP-007：容器实例展示端口映射（internalPort→hostPort）
            if s.port_mapping_label:
                typer.echo(f"  ↳ 映射：{s.port_mapping_label}")
            # IMP-047：文件夹源标识
            if s.source_kind == "folder":
                typer.echo(f"  ↳ 来源：本机文件夹 {s.source_dir_path or ''}")
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


def pageviews(
    instance_id: str = typer.Argument(None, help="实例 ID（省略则显示全部汇总）"),
    limit: int = typer.Option(
        50, "--limit", "-n", help="单实例详情时最近命中行数（1–500）"
    ),
) -> None:
    """查看浏览量统计（对齐管理页 /api/pageviews；先惰性摄入日志再汇总）。"""
    from local_webpage_access.pageviews import PageviewStore, ingest_all

    if limit < 1 or limit > 500:
        typer.secho("--limit 须在 1–500", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    try:
        ws, config, reg = open_workspace_registry()
        try:
            if instance_id is not None and reg.get_instance(instance_id) is None:
                typer.secho(f"实例不存在：{instance_id}", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=1)
            store = PageviewStore.shared_for_workspace(ws)
            try:
                ingest_all(ws, config, reg, store)
            except Exception as exc:  # noqa: BLE001 — 摄入失败不阻断，返回已聚合数据
                log.debug("浏览量摄入失败：%s", exc)
            if instance_id:
                detail = store.detail(instance_id, limit=limit)
                typer.secho(f"== 浏览量：{instance_id} ==", fg=typer.colors.CYAN)
                typer.echo(f"  来源：{detail.get('source') or '-'}")
                by_day = detail.get("byDay") or []
                total_hits = sum(int(d.get("hits") or 0) for d in by_day)
                ip_list = detail.get("uniqueIpList") or []
                typer.echo(f"  命中（近天合计）：{total_hits}")
                typer.echo(f"  独立 IP：{len(ip_list)}")
                if by_day:
                    typer.secho("  -- 按天 --", fg=typer.colors.CYAN)
                    for d in by_day[:14]:
                        typer.echo(
                            f"    {d.get('day')}: hits={d.get('hits', 0)} "
                            f"uniqueIps={d.get('uniqueIps', 0)}"
                        )
                recent = detail.get("recent") or []
                if recent:
                    typer.secho("  -- 最近命中 --", fg=typer.colors.CYAN)
                    for r in recent[:limit]:
                        typer.echo(
                            f"    {r.get('ts')} {r.get('method')} {r.get('path')} "
                            f"{r.get('status')} {r.get('remote')}"
                        )
            else:
                summary = store.summary()
                if not summary:
                    typer.echo("（暂无浏览量数据）")
                    return
                typer.echo(
                    f"{'ID':24} {'HITS':8} {'UNIQUE_IP':10} {'SOURCE':10} LAST_SEEN"
                )
                for iid, row in sorted(summary.items()):
                    typer.echo(
                        f"{iid[:24]:24} {row.get('hits', 0):<8} "
                        f"{row.get('uniqueIps', 0):<10} "
                        f"{str(row.get('source') or '-'):10} "
                        f"{row.get('lastSeen') or '-'}"
                    )
        finally:
            reg.close()
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


def register(app: typer.Typer) -> None:
    """把本模块命令注册到根 app（保持顶层命令名不变）。"""
    app.command()(status)
    app.command()(stats)
    app.command("list")(list_cmd)
    app.command()(pageviews)
