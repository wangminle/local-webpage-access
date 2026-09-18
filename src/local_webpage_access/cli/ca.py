"""``ca`` 子命令（HTTPS 首版交付 W03）：``lwa ca export``——根证书导出与信任指引。

Caddy internal CA 的根证书归属工作区（``run/caddy-data/caddy/pki/``），
本命令把它导出到用户指定位置并打印 SHA-256 指纹与各平台安装指引——
客户端**必须真正信任**该证书，禁止「自签 + 点穿浏览器告警」的用法
（等于把明文攻击升级为换证书 MITM，见 docs/https.md）。
"""

from __future__ import annotations

from pathlib import Path

import typer

from local_webpage_access.errors import LwaError

app = typer.Typer(help="内部 CA 根证书管理（gatewayTls=internal 时使用）")

_INSTALL_GUIDE = """\
各平台安装指引（安装后在浏览器/系统中完全信任）：
  macOS     双击 lwa-root-ca.crt → 钥匙串访问 → 系统 → 找到 "Caddy Local Authority"
            → 显示简介 → 信任 → 始终信任；Safari/Chrome 即生效
            （命令行：sudo security add-trusted-cert -d -r trustRoot
              -k /Library/Keychains/System.keychain lwa-root-ca.crt）
  Windows   双击 lwa-root-ca.crt → 安装证书 → 本地计算机 →
            将所有的证书都放入下列存储 → 受信任的根证书颁发机构
  Linux     sudo cp lwa-root-ca.crt /usr/local/share/ca-certificates/ &&
            sudo update-ca-certificates（Debian/Ubuntu）；
            Fedora/RHEL 放 /etc/pki/ca-trust/source/anchors/ 后
            sudo update-ca-trust
  Firefox   单独信任库：设置 → 隐私与安全 → 证书 → 导入 lwa-root-ca.crt，
            勾选「信任由此证书颁发机构来标识网站」
  iOS       设置 → 通用 → VPN与设备管理 → 安装；再到 设置 → 通用 → 关于本机 →
            证书信任设置 → 启用 Caddy Local Authority 的完全信任
  Android   设置 → 安全 → 加密与凭据 → 安装证书 → CA 证书（各厂商路径略有差异）
  curl/Agent 客户端：curl --cacert lwa-root-ca.crt https://…；
            Python: ssl context.load_verify_locations(cafile=…)
"""


@app.command("export")
def ca_export(
    out: Path = typer.Option(
        Path("lwa-root-ca.crt"),
        "--out",
        "-o",
        help="导出目标路径（默认当前目录 lwa-root-ca.crt）",
    ),
) -> None:
    """导出 Caddy 内部 CA 根证书并打印 SHA-256 指纹与安装指引。"""
    from local_webpage_access.paths import require_workspace
    from local_webpage_access.static_gateway import (
        caddy_root_cert_fingerprint,
        caddy_root_cert_path,
    )

    # BUG-715：导出只读磁盘证书文件，不碰 registry——避免库损坏/被锁
    # 无端阻断证书分发（应急场景恰恰最需要这份证书）。
    try:
        ws = require_workspace()
    except LwaError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    cert = caddy_root_cert_path(ws)
    if not cert.is_file():
        typer.secho(
            f"未找到内部 CA 根证书：{cert}\n"
            "根证书在网关首次以 gatewayTls=internal 启动并签发证书后生成——"
            "请先在 local-web.yml 配置 gatewayTls: internal 并执行 `lwa gateway on`。",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    target = out.expanduser().resolve()
    if target.is_dir():
        target = target / "lwa-root-ca.crt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(cert.read_bytes())
    fingerprint = caddy_root_cert_fingerprint(ws)

    typer.secho(f"已导出根证书：{target}", fg=typer.colors.GREEN)
    if fingerprint:
        typer.echo(f"SHA-256 指纹：{fingerprint}")
        typer.echo("请在客户端安装后核对指纹（防导出/传输环节被替换）。\n")
    typer.echo(_INSTALL_GUIDE)
    typer.echo(
        "安装并信任后访问 https://<LAN-IP>:<gatewayTlsPort>/<alias>/ 与"
        " https://<LAN-IP>:<managerTlsPort>/ 应无证书告警；"
        "未安装根证书时浏览器告警**不要点穿**——那不是安全连接。"
    )
