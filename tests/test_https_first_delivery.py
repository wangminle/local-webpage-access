"""HTTPS 首版交付回归（2026-09-18 WBS，CHK-352）。

覆盖 W01 配置校验、W02 CA 归属与指纹、W04 Caddyfile TLS 块与明文抑制、
W06 端口收敛、W07 URL scheme、W09 反代 XFF 鉴权、W10 doctor TLS 检查。
真机 Caddy TLS 端到端验收按 docs/https.md §7 人工清单执行（两台设备）。
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from local_webpage_access.config import Config, PortPool, tls_enabled
from local_webpage_access.paths import Workspace
from local_webpage_access.static_gateway import StaticGateway

# 一个最小可用 PEM 根证书（内容随意，指纹按 DER 摘要——仅验证计算路径）
_FAKE_CERT_B64 = b"MA0GC2CGSAGG+hMBBAMCAQI="  # 无效 DER，仅用于指纹测试


def _tls_config(**overrides) -> Config:
    base = dict(
        staticGateway="caddy",
        staticGatewayPort=8080,
        gatewayTls="internal",
        portPool=PortPool(start=21000, end=21050),
    )
    base.update(overrides)
    return Config(**base)


# ---- W01：配置校验 --------------------------------------------------------------


def test_gateway_tls_defaults_off_keeps_current_behavior() -> None:
    config = Config()
    assert config.gatewayTls == "off"
    assert config.gatewayPlainPort is None
    assert config.instanceBindHost == "0.0.0.0"
    assert tls_enabled(config) is False


def test_gateway_tls_rejects_builtin_backend() -> None:
    with pytest.raises(ValueError, match="仅支持 staticGateway=caddy"):
        Config(staticGateway="builtin", gatewayTls="internal")


def test_gateway_tls_port_conflicts_rejected() -> None:
    with pytest.raises(ValueError, match="不能与 managerPort"):
        _tls_config(managerPort=8443)
    with pytest.raises(ValueError, match="gatewayTlsPort 与 managerTlsPort"):
        _tls_config(gatewayTlsPort=9443, managerTlsPort=9443)
    with pytest.raises(ValueError, match="不能落在端口池"):
        _tls_config(gatewayTlsPort=21010)


def test_instance_bind_host_must_be_ip() -> None:
    with pytest.raises(ValueError, match="合法 IP"):
        Config(instanceBindHost="example.com")


def test_instance_bind_host_rejects_non_loopback_unicast() -> None:
    """CHK-353：网关上游固定回环——LAN IP 绑定会使别名反代/探活全部失效。"""
    with pytest.raises(ValueError, match="仅支持 0.0.0.0"):
        Config(instanceBindHost="192.168.1.5")
    assert Config(instanceBindHost="127.0.0.1").instanceBindHost == "127.0.0.1"
    # BUG-719：IPv6 通配/回环不再接受——上游与探活固定 IPv4 回环
    with pytest.raises(ValueError, match="仅支持 0.0.0.0"):
        Config(instanceBindHost="::1")
    with pytest.raises(ValueError, match="仅支持 0.0.0.0"):
        Config(instanceBindHost="::")


# ---- W02：CA 归属与指纹 ---------------------------------------------------------


def test_root_cert_paths_belong_to_workspace(workspace: Workspace) -> None:
    from local_webpage_access.static_gateway import (
        caddy_root_cert_path,
        caddy_spawn_env,
    )

    ws = workspace
    cert = caddy_root_cert_path(ws)
    assert cert.is_relative_to(ws.run / "caddy-data" / "caddy")

    env = caddy_spawn_env(ws)
    assert env["XDG_DATA_HOME"] == str(ws.run / "caddy-data")
    assert (ws.run / "caddy-data").stat().st_mode & 0o777 == 0o700


def test_root_cert_fingerprint_sha256_of_der(workspace: Workspace) -> None:
    from local_webpage_access.static_gateway import (
        caddy_root_cert_fingerprint,
        caddy_root_cert_path,
    )

    ws = workspace
    assert caddy_root_cert_fingerprint(ws) is None  # 未签发

    der = base64.b64decode(_FAKE_CERT_B64)
    cert = caddy_root_cert_path(ws)
    cert.parent.mkdir(parents=True, exist_ok=True)
    cert.write_bytes(
        b"-----BEGIN CERTIFICATE-----\n"
        + base64.encodebytes(der)
        + b"-----END CERTIFICATE-----\n"
    )
    digest = hashlib.sha256(der).hexdigest().upper()
    expected = ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))
    assert caddy_root_cert_fingerprint(ws) == expected


# ---- W04：Caddyfile TLS 块 ------------------------------------------------------


def _gateway_with_aliases(ws: Workspace, config: Config) -> None:
    aliases = ws.static_aliases
    aliases.mkdir(parents=True, exist_ok=True)
    (aliases / "demo.conf").write_text(
        'handle_path /demo/* { reverse_proxy 127.0.0.1:18001 }',
        encoding="utf-8",
    )


def test_main_config_emits_tls_blocks_and_suppresses_plain(workspace: Workspace) -> None:
    ws = workspace
    config = _tls_config()
    _gateway_with_aliases(ws, config)

    text = StaticGateway(ws, config)._assemble_main_config()

    assert "tls internal" in text
    assert f"https://127.0.0.1:{config.gatewayTlsPort}" in text
    # 管理面独立 origin：独立端口反代回环 manager
    assert f"reverse_proxy 127.0.0.1:{config.managerPort}" in text
    assert f"https://127.0.0.1:{config.managerTlsPort}" in text
    # 明文 staticGatewayPort 块被抑制
    assert "\n:8080 {" not in text
    assert "# IMP-006 路径别名统一入口（端口 8080" not in text


def test_main_config_plain_port_optional(workspace: Workspace) -> None:
    ws = workspace
    config = _tls_config(gatewayPlainPort=8080)
    _gateway_with_aliases(ws, config)

    text = StaticGateway(ws, config)._assemble_main_config()
    assert ":8080 {" in text, "gatewayPlainPort 显式保留时明文块在场"


def test_site_config_binds_instance_host(workspace: Workspace, tmp_path: Path) -> None:
    ws = workspace
    site_root = tmp_path / "site"
    site_root.mkdir()

    converged = StaticGateway(ws, _tls_config(instanceBindHost="127.0.0.1"))
    path = converged.generate_site_config("demo", 18001, site_root)
    assert "bind 127.0.0.1" in path.read_text(encoding="utf-8")

    default = StaticGateway(ws, Config())
    path = default.generate_site_config("demo2", 18002, site_root)
    assert "bind " not in path.read_text(encoding="utf-8"), "默认通配不 bind"


# ---- W06：compose 端口发布 ------------------------------------------------------


def test_compose_publish_host_ip_when_converged(workspace, registry, tmp_path: Path) -> None:
    from local_webpage_access.compose import generate_compose
    from local_webpage_access.models import ContainerConfig, Kind, ServingMode, Status
    from tests._helpers import make_container_manifest

    manifest = make_container_manifest(
        "tlsdemo",
        kind=Kind.PYTHON,
        servingMode=ServingMode.CONTAINER,
        status=Status.STOPPED,
        container=ContainerConfig(
            projectName="lwa-tlsdemo",
            internalPort=8000,
            composePath="docker/compose.yaml",
            dockerfilePath="docker/Dockerfile",
        ),
    )
    manifest.save(workspace.app_manifest_path("tlsdemo"))
    workspace.ensure_app_dirs("tlsdemo")

    config = Config(instanceBindHost="127.0.0.1")
    text = generate_compose(manifest, workspace, host_port=18006, config=config).read_text(
        encoding="utf-8"
    )
    assert '"127.0.0.1:${HOST_PORT}:${INTERNAL_PORT}"' in text

    plain = generate_compose(manifest, workspace, host_port=18006).read_text(encoding="utf-8")
    assert '"${HOST_PORT}:${INTERNAL_PORT}"' in plain, "缺省 config 保持通配发布"


# ---- W07：URL scheme ------------------------------------------------------------


def test_url_builders_follow_tls() -> None:
    from local_webpage_access.ports import (
        build_lan_url,
        build_route_url,
        lan_entry_port,
        manager_entry_url,
    )

    tls = _tls_config()
    plain = Config()

    assert build_lan_url("10.0.0.5", 18001) == "http://10.0.0.5:18001"
    assert build_lan_url("10.0.0.5", 8443, config=tls) == "https://10.0.0.5:8443"
    assert lan_entry_port(plain) == 8080
    assert lan_entry_port(tls) == 8443
    assert build_route_url("10.0.0.5", 8443, "demo", config=tls) == "https://10.0.0.5:8443/demo/"
    assert build_route_url("10.0.0.5", 443, "demo", config=tls) == "https://10.0.0.5/demo/"
    assert manager_entry_url(plain, "10.0.0.5") == "http://10.0.0.5:17800/"
    assert manager_entry_url(tls, "10.0.0.5") == "https://10.0.0.5:9443/"


def test_network_entry_records_bind_and_tls_route() -> None:
    from local_webpage_access.ports import build_network_entry

    tls = _tls_config(instanceBindHost="127.0.0.1")
    entry = build_network_entry(tls, 18001, path_alias="demo", lan_ip="10.0.0.5")
    assert entry["host"] == "127.0.0.1"
    assert entry["routeUrl"] == "https://10.0.0.5:8443/demo/"
    # BUG-713：lanUrl 保持直连明文语义（实例直连口不走 TLS）
    assert entry["lanUrl"] == "http://10.0.0.5:18001"

    plain = Config()
    entry = build_network_entry(plain, 18001, path_alias="demo", lan_ip="10.0.0.5")
    assert entry["host"] == "0.0.0.0"
    assert entry["routeUrl"] == "http://10.0.0.5:8080/demo/"


# ---- W09：反代 XFF 鉴权 ---------------------------------------------------------


def _agent_app(workspace_root: Path, *, tls: bool):
    from local_webpage_access.manager_api import create_app, ensure_token
    from local_webpage_access.registry import Registry

    ws = Workspace(workspace_root)
    ws.ensure_workspace_dirs()
    config = _tls_config() if tls else Config()
    reg = Registry(ws.db_path)
    reg.open()
    token = ensure_token(ws)
    app = create_app(ws, config, reg, token=token)
    return app, token, reg


def test_xff_chain_blocks_lan_impersonation_via_proxy(workspace_root: Path) -> None:
    """W09：经反代（peer=127.0.0.1）的 LAN 客户端伪造 Host 不得免鉴权。"""
    app, _token, reg = _agent_app(workspace_root, tls=True)
    client = TestClient(app, base_url="http://127.0.0.1:17800", client=("127.0.0.1", 50000))

    # 直连本机（无 XFF）：回环 GET 免 token
    resp = client.get("/api/stats", headers={"Host": "127.0.0.1:17800"})
    assert resp.status_code == 200

    # 经反代的 LAN 客户端：XFF 链含非回环地址 → 必须带 token
    resp = client.get(
        "/api/stats",
        headers={"Host": "127.0.0.1:17800", "X-Forwarded-For": "192.168.1.50"},
    )
    assert resp.status_code == 401, "LAN 客户端经反代伪造 Host 不得免鉴权"

    # 本机客户端经反代（XFF 链全回环）→ 仍按本机免 token
    resp = client.get(
        "/api/stats",
        headers={"Host": "127.0.0.1:17800", "X-Forwarded-For": "127.0.0.1"},
    )
    assert resp.status_code == 200
    reg.close()


def test_no_xff_parsing_when_tls_off(workspace_root: Path) -> None:
    """W09 对照：非 TLS 部署（无反代）不解析 XFF——维持既有语义。"""
    app, _token, reg = _agent_app(workspace_root, tls=False)
    client = TestClient(app, base_url="http://127.0.0.1:17800", client=("127.0.0.1", 50000))
    resp = client.get(
        "/api/stats",
        headers={"Host": "127.0.0.1:17800", "X-Forwarded-For": "192.168.1.50"},
    )
    assert resp.status_code == 200, "TLS 关闭时 XFF 不参与本机判定（直连语义）"
    reg.close()


# ---- W10：doctor TLS 检查 -------------------------------------------------------


def test_doctor_tls_check_states(workspace: Workspace) -> None:
    from local_webpage_access.doctor import check_gateway_tls

    off = check_gateway_tls(workspace, Config())
    assert off.status == "skip" and "gatewayTls=off" in off.message

    on = check_gateway_tls(workspace, _tls_config())
    assert on.status == "fail", "根证书缺失（未签发）应为 FAIL"
    assert "根证书缺失" in on.message


# ---- BUG-710：Caddyfile global 块 -----------------------------------------------


def test_tls_caddyfile_starts_with_global_block(workspace: Workspace) -> None:
    """BUG-710：TLS 模式首块必须是 global 选项——禁 sudo 装根证书与 :80 重定向。"""
    config = _tls_config()
    _gateway_with_aliases(workspace, config)
    text = StaticGateway(workspace, config)._assemble_main_config()
    assert text.startswith("{"), "global 块必须是 Caddyfile 首块"
    header = text[: text.index("}") + 1]
    assert "skip_install_trust" in header, "禁止 Caddy 自动 sudo 安装根证书"
    assert "auto_https disable_redirects" in header, "禁止额外监听明文 :80"


# ---- BUG-711：TLS 验证语义（502=TLS 层健康；证书错立即失败）----------------------


def test_tls_verify_treats_502_as_healthy(workspace: Workspace, monkeypatch) -> None:
    """BUG-711：启动顺序 gateway→manager——反代 502 证明 TLS 层正常，不算失败。"""
    import urllib.error

    import local_webpage_access.probe as probe_mod

    def fake_urlopen(url, timeout=None, ssl_context=None):
        raise urllib.error.HTTPError(
            str(url), 502, "Bad Gateway", hdrs=None, fp=None  # type: ignore[arg-type]
        )

    monkeypatch.setattr(probe_mod, "urlopen_direct", fake_urlopen)
    from local_webpage_access.gateway_service import verify_tls_entries

    assert verify_tls_entries(workspace, _tls_config()) is None


def test_tls_verify_cert_error_fails_immediately(workspace: Workspace, monkeypatch) -> None:
    """BUG-711 对照：根证书在场时证书校验失败立即报错（不重试、不降级）。"""
    import ssl
    import urllib.error

    import local_webpage_access.probe as probe_mod
    from local_webpage_access.static_gateway import caddy_root_cert_path

    cert = caddy_root_cert_path(workspace)
    cert.parent.mkdir(parents=True, exist_ok=True)
    cert.write_bytes(
        b"-----BEGIN CERTIFICATE-----\nMA0GC2CGSAGG+hMBBAMCAQI=\n-----END CERTIFICATE-----\n"
    )
    calls = {"n": 0}

    def fake_urlopen(url, timeout=None, ssl_context=None):
        calls["n"] += 1
        raise urllib.error.URLError(ssl.SSLCertVerificationError("cert verify failed"))

    monkeypatch.setattr(probe_mod, "urlopen_direct", fake_urlopen)
    from local_webpage_access.gateway_service import verify_tls_entries

    error = verify_tls_entries(workspace, _tls_config())
    assert error is not None and "证书验证失败" in error
    assert calls["n"] == 1, "真证书错误不得重试"


# ---- BUG-712：G6 复检 TLS 适配 ---------------------------------------------------


def test_imp023_recheck_probes_https_entry(workspace: Workspace, monkeypatch) -> None:
    """BUG-712：TLS 模式下复检走 https 入口 + 证书验证，不再恒 False。"""
    import local_webpage_access.access as access_mod

    config = _tls_config()
    seen: dict[str, object] = {}

    def fake_fetch_text(url, *, timeout=5.0, ssl_context=None):
        seen["entry_url"] = url
        seen["entry_ctx"] = ssl_context
        return '<html><script src="/assets/app.js"></script></html>'

    def fake_http_get(url, *, timeout=5.0, ssl_context=None):
        # IMP-023 语义：绝对路径打到入口根失败，带别名前缀成功 → mismatch
        seen.setdefault("probe_urls", []).append(url)
        from local_webpage_access.access import UrlProbe

        probe = UrlProbe(url=url)
        prefixed = "/demo/" in url
        probe.status_code = 200 if prefixed else 404
        probe.ok = prefixed
        probe.content_length = 128 if prefixed else 0
        return probe

    monkeypatch.setattr(access_mod, "_fetch_text", fake_fetch_text)
    monkeypatch.setattr(access_mod, "_http_get", fake_http_get)

    still = access_mod.instance_still_has_imp023(
        config, path_alias="demo", workspace=workspace
    )
    assert seen["entry_url"] == "https://127.0.0.1:8443/demo/"
    assert seen["entry_ctx"] is not None, "https 探测必须带证书验证上下文"
    assert all(str(u).startswith("https://") for u in seen["probe_urls"])  # type: ignore[union-attr]
    assert still is True


# ---- CHK-356 复审修复回归 --------------------------------------------------------


def test_tls_verify_cert_error_message_not_truncated(workspace: Workspace, monkeypatch) -> None:
    """修复1：证书错误消息完整——含排查指引，不以逗号截断结尾。"""
    import ssl
    import urllib.error

    import local_webpage_access.probe as probe_mod
    from local_webpage_access.static_gateway import caddy_root_cert_path

    # 根证书在场（真证书错误路径：立即失败，不重试）
    cert = caddy_root_cert_path(workspace)
    cert.parent.mkdir(parents=True, exist_ok=True)
    cert.write_bytes(
        b"-----BEGIN CERTIFICATE-----\nMA0GC2CGSAGG+hMBBAMCAQI=\n-----END CERTIFICATE-----\n"
    )

    def fake_urlopen(url, timeout=None, ssl_context=None):
        raise urllib.error.URLError(ssl.SSLCertVerificationError("cert verify failed"))

    monkeypatch.setattr(probe_mod, "urlopen_direct", fake_urlopen)
    from local_webpage_access.gateway_service import verify_tls_entries

    error = verify_tls_entries(workspace, _tls_config())
    assert error is not None
    assert "证书验证失败" in error
    assert error.rstrip().endswith("`lwa gateway on`"), "消息不得以逗号截断"
    assert "run/caddy-data" in error


def test_tls_verify_first_start_race_recovers(workspace: Workspace, monkeypatch) -> None:
    """修复4：首启竞态——根证书未落盘时的证书错误按可重试，落盘后通过。"""
    import ssl
    import urllib.error

    import local_webpage_access.gateway_service as gs
    import local_webpage_access.probe as probe_mod
    from local_webpage_access.static_gateway import caddy_root_cert_path

    calls = {"n": 0}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(url, timeout=None, ssl_context=None):
        calls["n"] += 1
        if calls["n"] <= 2:
            # 前两次：Caddy 尚未写出 root.crt，上下文只有系统 CA → 验证失败
            raise urllib.error.URLError(ssl.SSLCertVerificationError("not trusted yet"))
        return _Resp()

    monkeypatch.setattr(probe_mod, "urlopen_direct", fake_urlopen)
    sleeps: list[float] = []
    monkeypatch.setattr(gs.time, "sleep", lambda s: sleeps.append(s))

    # 根证书在第三次调用前落盘（竞态时序：Caddy 启动加载配置时才生成 CA）
    cert = caddy_root_cert_path(workspace)

    def controlled(url, timeout=None, ssl_context=None):
        if calls["n"] == 2 and not cert.is_file():
            cert.parent.mkdir(parents=True, exist_ok=True)
            cert.write_bytes(
                b"-----BEGIN CERTIFICATE-----\nMA0=\n-----END CERTIFICATE-----\n"
            )
        return fake_urlopen(url, timeout, ssl_context)

    monkeypatch.setattr(probe_mod, "urlopen_direct", controlled)
    from local_webpage_access.gateway_service import verify_tls_entries

    assert verify_tls_entries(workspace, _tls_config()) is None, "竞态应恢复为健康"
    assert calls["n"] == 3


def test_gateway_plain_port_full_conflict_validation() -> None:
    """修复5：gatewayPlainPort 撞 managerPort / TLS 端口 / 落端口池均拒绝。"""
    base = dict(staticGateway="caddy", gatewayTls="internal", portPool=PortPool(start=21000, end=21050))
    with pytest.raises(ValueError, match="不能与管理页端口"):
        Config(**base, gatewayPlainPort=8090, managerPort=8090)
    with pytest.raises(ValueError, match="不能与 TLS 端口"):
        Config(**base, gatewayPlainPort=8443)
    with pytest.raises(ValueError, match="不能落在端口池"):
        Config(**base, gatewayPlainPort=21010)
    assert Config(**base, gatewayPlainPort=8090).gatewayPlainPort == 8090


def test_doctor_caddy_health_probes_tls_entry(workspace: Workspace, registry, monkeypatch) -> None:
    """修复2：TLS 模式下 caddy_health 探测 https 入口（端口/scheme/证书上下文）。"""
    import shutil as shutil_mod

    import local_webpage_access.doctor as doctor_mod
    from local_webpage_access.static_gateway import StaticGateway

    config = _tls_config()
    captured: dict[str, object] = {}

    def fake_health_check(self, host_port, *, timeout=5.0, path="/", scheme="http", ssl_context=None):
        captured["port"] = host_port
        captured["scheme"] = scheme
        captured["path"] = path
        captured["ctx"] = ssl_context
        return True

    monkeypatch.setattr(StaticGateway, "health_check", fake_health_check)
    monkeypatch.setattr(StaticGateway, "_admin_alive", lambda self: True)
    monkeypatch.setattr(
        shutil_mod, "which", lambda name: "/usr/bin/caddy" if name == "caddy" else None
    )
    monkeypatch.setattr(registry, "list_route_hosts", lambda: {"demo": 18001})
    monkeypatch.setattr(registry, "list_instances", lambda: [])

    result = doctor_mod.check_caddy_health(workspace, config, registry=registry)
    assert captured["port"] == 8443, "TLS 模式入口端口应为 gatewayTlsPort"
    assert captured["scheme"] == "https"
    assert captured["path"] == "/demo/"
    assert captured["ctx"] is not None, "https 探活必须带证书验证上下文"
    assert result.status in ("ok", "warn", "fail"), result.message
