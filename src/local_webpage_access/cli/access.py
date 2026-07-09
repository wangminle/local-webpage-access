"""access 子命令（gateway-switch-access-review 建议 B/C/I）：``lwa access refresh|review``。

* ``lwa access refresh`` —— 用当前 LAN IP 重算所有实例 ``lanUrl``/``routeUrl``
  并落盘（G1，DHCP/换网后地址漂移自愈）。
* ``lwa access review`` —— 对声明 URL 做真探活（入口 HTML + 抽样绝对路径子资源
  + 端口独占），避免「入口 200 ≠ 页面可渲染」（G2/G5）；G6 默认提示需 rebuild
  的实例，``--rebuild-if-needed`` 时对 IMP-023 命中实例自动重建。
"""

from __future__ import annotations

import typer

from local_webpage_access.cli._common import log, open_workspace_registry
from local_webpage_access.errors import LwaError

app = typer.Typer(help="访问地址刷新与可用性复核（G1/G2/G5/G6）")


@app.command("refresh")
def access_refresh() -> None:
    """刷新所有实例的 LAN 访问地址（G1）。

    用当前 ``resolve_lan_ip`` 重算 ``lanUrl``/``routeUrl``。DHCP 换网 / 重启网关
    后管理页链接失效时运行此命令即可自愈，无需逐实例 restart。
    """
    from local_webpage_access.access import refresh_network_entries

    try:
        ws, config, reg = open_workspace_registry()
        try:
            report = refresh_network_entries(ws, config, reg)
        finally:
            reg.close()
        typer.secho("访问地址刷新完成", fg=typer.colors.GREEN)
        typer.echo(f"  当前 LAN IP：{report.lan_ip or '(无)'}")
        typer.echo(f"  刷新实例：{len(report.refreshed)} 个（其中 {report.drifted_count} 个地址漂移）")
        for item in report.refreshed:
            if item.drifted:
                typer.secho(
                    f"    · {item.instance_id}：{item.old_host or '(空)'} → {item.new_host or '(无)'}",
                    fg=typer.colors.YELLOW,
                )
            else:
                typer.echo(f"    · {item.instance_id}：{item.new_host or '(无)'}（未漂移）")
        if report.skipped:
            typer.echo(f"  跳过：{', '.join(report.skipped)}（无 hostPort 或 manifest 缺失）")
        typer.echo("  下一步：lwa access review 复核可用性")
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command("review")
def access_review(
    json_output: bool = typer.Option(
        False, "--json", help="输出 JSON 报告（便于脚本解析）"
    ),
    rebuild_if_needed: bool = typer.Option(
        False,
        "--rebuild-if-needed",
        help="对检出 IMP-023 空 200 的实例自动 lwa rebuild（默认仅检查并提示）",
    ),
) -> None:
    """复核各实例声明访问 URL 的真实可用性（G2/G5/G6）。

    探测回环 + lanUrl + routeUrl，并对别名入口做 SPA 绝对路径子资源空 200 检测
    （IMP-023）。默认只提示建议 rebuild 的实例；加 ``--rebuild-if-needed`` 时对
    命中实例自动重建。
    """
    import json as json_mod

    from local_webpage_access.access import (
        format_review_report,
        maybe_rebuild_after_review,
        review_access,
    )

    try:
        ws, config, reg = open_workspace_registry()
        try:
            report = review_access(ws, config, reg)
            rebuild_report = maybe_rebuild_after_review(
                ws,
                config,
                reg,
                report,
                rebuild_if_needed=rebuild_if_needed,
            )
        finally:
            reg.close()
        if json_output:
            payload = report.to_dict()
            payload["rebuild"] = rebuild_report.to_dict()
            typer.echo(json_mod.dumps(payload, ensure_ascii=False, indent=2))
        else:
            typer.echo(
                format_review_report(report, rebuild_report=rebuild_report)
            )
        if report.has_failures or not rebuild_report.all_ok:
            raise typer.Exit(code=1)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
