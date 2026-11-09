"""access 子命令（gateway-switch-access-review 建议 B/C/I）：``lwa access refresh|review``。

* ``lwa access refresh`` —— 用当前 LAN IP 重算所有实例 ``lanUrl``/``routeUrl``
  并落盘（G1，DHCP/换网后地址漂移自愈）。
* ``lwa access review`` —— 对声明 URL 做真探活（入口 HTML + 抽样绝对路径子资源
  + 端口独占），避免「入口 200 ≠ 页面可渲染」（G2/G5）；G6 默认提示需 rebuild
  的实例，``--rebuild-if-needed`` 时对 IMP-023 命中实例自动重建。
* ``lwa access record-observation`` —— 记录一次受约束的外部观测（位置/协议/
  响应/时间）。guest 或 Windows 本机成功不能升格为局域网已验证。
"""

from __future__ import annotations

import ipaddress

import typer

from local_webpage_access.cli._common import log, open_workspace_registry
from local_webpage_access.errors import LwaError

app = typer.Typer(help="访问地址刷新与可用性复核（G1/G2/G5/G6）")
lan_app = typer.Typer(help="查看或修改当前工作区的 LAN 地址策略", no_args_is_help=True)
app.add_typer(lan_app, name="lan")


@lan_app.command("show")
def lan_show() -> None:
    """查看当前策略、选中地址和自动候选。"""
    from local_webpage_access.config import load_config
    from local_webpage_access.lan_network import lan_candidates
    from local_webpage_access.paths import require_workspace
    from local_webpage_access.ports import (
        lan_reason_text,
        resolve_lan_ip,
        resolve_lan_resolution,
    )

    try:
        config = load_config(require_workspace())
    except LwaError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    lan_ip = resolve_lan_ip(config)
    typer.echo(f"LAN 策略：{config.lanIpStrategy}")
    typer.echo(f"当前 LAN IP：{lan_ip or '(无)'}")
    candidates = lan_candidates()
    if candidates is None:
        typer.echo("系统接口枚举不可用")
    elif not candidates:
        typer.echo("无可用自动候选")
    else:
        typer.echo("自动候选（按优先级）：")
        for candidate in candidates:
            typer.echo(f"  {candidate.interface}  {candidate.medium}  {candidate.address}")
    if lan_ip is None:
        resolution = resolve_lan_resolution(config)
        reason = lan_reason_text(resolution.reason, resolution.probe_address)
        if reason:
            typer.secho(f"自动地址状态：{reason}", fg=typer.colors.YELLOW)
        typer.echo("可运行 lwa access lan set <IP> 固定可信地址；已有链接已保留，需复核。")


@lan_app.command("set")
def lan_set(address: str = typer.Argument(..., help="固定使用的 LAN IP 地址")) -> None:
    """固定 LAN 地址并刷新实例入口。"""
    from local_webpage_access.lan_network import usable_lan_address

    if not usable_lan_address(address):
        raise typer.BadParameter("请输入可发布的 IPv4 或 IPv6 地址")
    _apply_lan_policy(str(ipaddress.ip_address(address)))


@lan_app.command("auto")
def lan_auto() -> None:
    """恢复自动选择并刷新实例入口。"""
    _apply_lan_policy(None)


def _apply_lan_policy(address: str | None) -> None:
    from local_webpage_access.access import refresh_network_entries
    from local_webpage_access.lan_policy import write_lan_policy

    try:
        workspace, _, registry = open_workspace_registry()
        try:
            config = write_lan_policy(workspace, address)
            typer.echo(f"已保存 LAN 策略：{config.lanIpStrategy}")
            report = refresh_network_entries(workspace, config, registry)
        finally:
            registry.close()
    except LwaError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"当前 LAN IP：{report.lan_ip or '(无)'}")
    typer.echo(f"已刷新 {len(report.refreshed)} 个实例")
    if report.lan_ip is None:
        from local_webpage_access.ports import lan_reason_text

        reason = lan_reason_text(report.lan_reason, report.lan_probe_address)
        typer.secho(
            f"未选到可用 LAN IP，已保留现有地址。{reason or ''}",
            fg=typer.colors.YELLOW,
        )
    elif report.skipped:
        typer.secho(
            f"以下实例本次未刷新：{', '.join(report.skipped)}。可运行 lwa access refresh 重试。",
            fg=typer.colors.YELLOW,
        )
    if report.caddy_reload_error:
        typer.secho(f"Caddy TLS 入口同步失败：{report.caddy_reload_error}", fg=typer.colors.YELLOW)
    elif report.caddy_reloaded:
        typer.echo("已同步在线 Caddy 的 TLS 入口。")



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
        if report.lan_ip is None:
            from local_webpage_access.ports import lan_reason_text

            reason = lan_reason_text(report.lan_reason, report.lan_probe_address)
            typer.secho(
                f"  {reason or '未解析到 LAN IP'}；已保留现有地址，未同步在线 Caddy",
                fg=typer.colors.YELLOW,
            )
        typer.echo(
            f"  刷新实例：{len(report.refreshed)} 个（其中 {report.drifted_count} 个地址漂移）"
        )
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
        if report.caddy_reloaded:
            typer.secho("  已按当前 LAN IP 重载 Caddy TLS 站点绑定", fg=typer.colors.GREEN)
        elif report.caddy_reload_error:
            typer.secho(
                f"  Caddy TLS 绑定未重载：{report.caddy_reload_error}（可手动 lwa gateway on）",
                fg=typer.colors.YELLOW,
            )
        typer.echo("  下一步：lwa access review 复核可用性")
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command("review")
def access_review(
    json_output: bool = typer.Option(False, "--json", help="输出 JSON 报告（便于脚本解析）"),
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
            typer.echo(format_review_report(report, rebuild_report=rebuild_report))
        if report.has_failures or not rebuild_report.all_ok:
            raise typer.Exit(code=1)
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


@app.command("record-observation")
def access_record_observation(
    instance_id: str = typer.Argument(..., help="实例 ID"),
    vantage: str = typer.Option(
        ...,
        "--vantage",
        help="观察位置：lan-client / windows-host / guest",
    ),
    protocol: str = typer.Option(..., "--protocol", help="http 或 https"),
    evidence: str = typer.Option(
        ...,
        "--evidence",
        help="响应证据：content-match / page / timeout / connection-refused / tls-error / auth-401",
    ),
    status_code: int | None = typer.Option(
        None, "--status", help="HTTP 状态码；成功证据必填"
    ),
    url: str | None = typer.Option(None, "--url", help="实际访问的完整 HTTP(S) URL；省略则仅留证，不升格为外部已验证"),
) -> None:
    """记录一次外部观测。LWA 不会自己从其他设备探测。"""
    from local_webpage_access.access import record_lan_observation

    try:
        ws, config, reg = open_workspace_registry()
        try:
            result = record_lan_observation(
                ws,
                config,
                instance_id,
                vantage=vantage,
                protocol=protocol,
                evidence=evidence,
                status_code=status_code,
                url=url,
            )
        finally:
            reg.close()
        typer.secho("已记录外部观测", fg=typer.colors.GREEN)
        typer.echo(f"  暴露状态：{result['lanExposure']}")
        obs = result.get("lanObservation") or {}
        typer.echo(
            "  证据："
            f"{obs.get('vantage')} {obs.get('protocol')} "
            f"{obs.get('evidence')} {obs.get('observedAt')}"
        )
        if result.get("lanObservationStale"):
            typer.secho("  观测 URL 缺失、无效或与当前网络/入口不匹配，不能作为外部已验证", fg=typer.colors.YELLOW)
        if result.get("hostLanUrl"):
            typer.echo(f"  候选：{result['hostLanUrl']}")
    except LwaError as exc:
        log.error(str(exc), extra=exc.context)
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
