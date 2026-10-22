"""BUG-944：WSL NAT 多地址 HTTPS 入口对无 SNI 客户端的证书选择。

实机现象（Debian WSL，V0.9.11）：TLS 站点块按地址列出 ``https://127.0.0.1``、
guest IP、Windows 局域网 IP 并 ``tls internal``，Caddy 为每个地址各签一张单 SAN
证书。浏览器 / Python / curl 按 IP 访问时**不发 SNI**，certmagic 对无 SNI 连接按
**连接的本地地址**选证书；经 Windows portproxy 转发到 guest 后本地地址只会是
guest 或回环，客户端按 Windows IP 校验时拿到的永远是回环或 guest 证书。显式
``openssl -servername <WindowsIP>`` 成功只说明那张证书在磁盘上，不说明无 SNI
客户端能选中它。

修复口径：存在不属于 guest 的入口地址（WSL 下的 Windows 局域网 IP）时，不再逐
地址 ``tls internal``，而是用工作区 Caddy internal CA 的 root 直接签一张覆盖全部
入口地址的多 SAN 证书交给 Caddy（``tls <cert> <key>``）——无论无 SNI 连接按本地
地址选中哪个名字，拿到的都是同一张覆盖 Windows IP 的证书。非 WSL（回环与 LAN IP
都是本机地址，按本地地址选证书本就正确）保持逐地址 ``tls internal`` 不变。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import ssl
import subprocess
import time
from pathlib import Path

import pytest

from local_webpage_access.config import Config, PortPool
from local_webpage_access.paths import Workspace
from local_webpage_access.static_gateway import (
    StaticGateway,
    _caddy_quote,
    caddy_root_cert_fingerprint,
    caddy_root_cert_path,
    caddy_spawn_env,
    entry_tls_cert_paths,
    entry_tls_meta_path,
)
from tests.scripts._caddy import caddy_binary, requires_caddy

GUEST_IP = "172.22.148.94"
WINDOWS_IP = "10.180.105.74"
PLAIN_LAN_IP = "192.168.1.50"

_OPENSSL = shutil.which("openssl")
requires_openssl = pytest.mark.skipif(
    _OPENSSL is None, reason="需要 openssl 可执行文件签发多 SAN 入口证书"
)
_E2E_ENV = "LWA_RUN_CADDY_TLS_E2E"


def _tls_config(**overrides) -> Config:
    base = dict(
        staticGateway="caddy",
        staticGatewayPort=8080,
        gatewayTls="internal",
        portPool=PortPool(start=21000, end=21050),
    )
    base.update(overrides)
    return Config(**base)


def _with_alias(ws: Workspace) -> None:
    ws.static_aliases.mkdir(parents=True, exist_ok=True)
    (ws.static_aliases / "demo.conf").write_text(
        "handle_path /demo/* {\n\treverse_proxy 127.0.0.1:18001\n}\n", encoding="utf-8"
    )


def _wsl_nat(monkeypatch: pytest.MonkeyPatch, *, lan_ip: str | None = GUEST_IP) -> None:
    """N10-3 实机快照：guest 172.22.148.94，Windows 局域网 IP 10.180.105.74。"""
    from tests.scripts.test_wsl_network import _yi14_snapshot

    snap = _yi14_snapshot()
    assert snap is not None and snap.host_lan_ip == WINDOWS_IP
    monkeypatch.setattr(
        "local_webpage_access.wsl_network.collect_wsl_network_snapshot", lambda **_k: snap
    )
    monkeypatch.setattr("local_webpage_access.ports.resolve_lan_ip", lambda _cfg: lan_ip)


def _no_wsl(monkeypatch: pytest.MonkeyPatch, *, lan_ip: str | None = PLAIN_LAN_IP) -> None:
    monkeypatch.setattr(
        "local_webpage_access.wsl_network.collect_wsl_network_snapshot", lambda **_k: None
    )
    monkeypatch.setattr("local_webpage_access.ports.resolve_lan_ip", lambda _cfg: lan_ip)


def _render(ws: Workspace, config: Config | None = None) -> str:
    config = config or _tls_config()
    _with_alias(ws)
    return StaticGateway(ws, config)._assemble_main_config()


def _openssl(*args: str) -> subprocess.CompletedProcess[str]:
    assert _OPENSSL is not None
    return subprocess.run(
        [_OPENSSL, *args], capture_output=True, text=True, encoding="utf-8", timeout=30
    )


def _cert_text(path: Path) -> str:
    proc = _openssl("x509", "-in", str(path), "-noout", "-text")
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _san_ips(path: Path) -> set[str]:
    return set(re.findall(r"IP Address:([0-9A-Fa-f.:]+)", _cert_text(path)))


def _manual_tls_directive(ws: Workspace) -> str:
    cert, key = entry_tls_cert_paths(ws)
    return f"tls {_caddy_quote(cert.as_posix())} {_caddy_quote(key.as_posix())}"


# ---- 渲染口径 ------------------------------------------------------------------


@requires_openssl
def test_wsl_entry_uses_one_certificate_covering_windows_ip(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WSL NAT：别名入口与管理面都改用同一张覆盖回环 / guest / Windows IP 的证书。"""
    _wsl_nat(monkeypatch)
    text = _render(workspace)

    assert "tls internal" not in text, "逐地址 tls internal 会让无 SNI 客户端拿到回环/guest 证书"
    assert text.count(_manual_tls_directive(workspace)) == 2, text
    cert, key = entry_tls_cert_paths(workspace)
    assert cert.is_file() and key.is_file()
    assert key.stat().st_mode & 0o777 == 0o600
    assert _san_ips(cert) == {"127.0.0.1", GUEST_IP, WINDOWS_IP}
    # 站点地址与 bind 语义不变：Windows IP 仍只进 SAN，不让 Caddy 去绑定它
    assert (
        f"https://127.0.0.1:8443, https://{GUEST_IP}:8443, https://{WINDOWS_IP}:8443 {{" in text
    )
    assert f"https://127.0.0.1:9443, https://{GUEST_IP}:9443, https://{WINDOWS_IP}:9443 {{" in text
    assert "\tbind 0.0.0.0" in text


def test_non_wsl_keeps_per_address_tls_internal(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非 WSL：回环与 LAN IP 都是本机地址，按本地地址选证书本就正确，保持不变。"""
    _no_wsl(monkeypatch)
    text = _render(workspace)
    assert text.count("\ttls internal") == 2
    assert "run/tls/entry" not in text
    cert, key = entry_tls_cert_paths(workspace)
    assert not cert.exists() and not key.exists()
    assert not caddy_root_cert_path(workspace).exists(), "非 WSL 不自举 CA，仍由 Caddy 首签时生成"


@requires_openssl
def test_wsl_without_resolvable_lan_ip_still_covers_loopback_and_windows_ip(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """portproxy 以 127.0.0.1 为目标时本地地址是回环——回环名字也必须落在同一张证书上。"""
    _wsl_nat(monkeypatch, lan_ip=None)
    text = _render(workspace)
    assert "tls internal" not in text
    cert, _key = entry_tls_cert_paths(workspace)
    assert _san_ips(cert) == {"127.0.0.1", WINDOWS_IP}
    assert f"https://127.0.0.1:8443, https://{WINDOWS_IP}:8443 {{" in text


# ---- 证书链与扩展 ----------------------------------------------------------------


@requires_openssl
def test_entry_cert_bootstraps_workspace_root_and_verifies(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全新工作区没有 Caddy CA：按 Caddy 的 pki 路径自举 root，叶证书由该 root 直签。"""
    _wsl_nat(monkeypatch)
    assert caddy_root_cert_fingerprint(workspace) is None
    _render(workspace)

    root = caddy_root_cert_path(workspace)
    root_key = root.with_name("root.key")
    assert root.is_file() and root_key.is_file()
    assert root_key.stat().st_mode & 0o777 == 0o600
    assert root.parent.stat().st_mode & 0o777 == 0o700
    assert "CA:TRUE" in _cert_text(root)
    assert caddy_root_cert_fingerprint(workspace) is not None, "lwa ca export 口径不变"

    cert, _key = entry_tls_cert_paths(workspace)
    proc = _openssl("verify", "-CAfile", str(root), str(cert))
    assert proc.returncode == 0, proc.stdout + proc.stderr


@requires_openssl
def test_entry_cert_reuses_existing_root(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已有 CA（Caddy 此前首签生成）时直接用它签，不重建 root——否则客户端已信任的指纹会失效。"""
    _wsl_nat(monkeypatch)
    _render(workspace)
    fingerprint = caddy_root_cert_fingerprint(workspace)
    root = caddy_root_cert_path(workspace)
    before = root.read_bytes()

    cert, _key = entry_tls_cert_paths(workspace)
    cert.unlink()
    _render(workspace)
    assert root.read_bytes() == before
    assert caddy_root_cert_fingerprint(workspace) == fingerprint
    proc = _openssl("verify", "-CAfile", str(root), str(cert))
    assert proc.returncode == 0, proc.stdout + proc.stderr


@requires_openssl
def test_entry_cert_has_server_auth_and_is_not_ca(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """iOS 13+/macOS 10.15+ 要求：SAN 在场、EKU 含 serverAuth、有效期不超过 825 天。"""
    _wsl_nat(monkeypatch)
    _render(workspace)
    cert, _key = entry_tls_cert_paths(workspace)
    text = _cert_text(cert)
    assert "TLS Web Server Authentication" in text
    assert "CA:FALSE" in text
    meta = json.loads(entry_tls_meta_path(workspace).read_text(encoding="utf-8"))
    assert sorted(meta["sans"]) == sorted(["127.0.0.1", GUEST_IP, WINDOWS_IP])
    proc = _openssl("x509", "-in", str(cert), "-noout", "-checkend", str(800 * 86400))
    assert proc.returncode == 0, "有效期应覆盖至少 800 天（避免依赖频繁 reload 续签）"
    proc = _openssl("x509", "-in", str(cert), "-noout", "-checkend", str(826 * 86400))
    assert proc.returncode != 0, "有效期不得超过 825 天"


# ---- 重签与复用 ------------------------------------------------------------------


@requires_openssl
def test_entry_cert_reused_when_sans_unchanged_and_regenerated_when_changed(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wsl_nat(monkeypatch)
    _render(workspace)
    cert, key = entry_tls_cert_paths(workspace)
    cert_before, key_before = cert.read_bytes(), key.read_bytes()

    _render(workspace)
    assert cert.read_bytes() == cert_before and key.read_bytes() == key_before, "SAN 未变不重签"

    # guest IP 漂移（WSL 重启常见）→ SAN 集合变化 → 重签，旧地址不再残留
    _wsl_nat(monkeypatch, lan_ip="172.30.0.7")
    text = _render(workspace)
    assert cert.read_bytes() != cert_before
    assert _san_ips(cert) == {"127.0.0.1", "172.30.0.7", WINDOWS_IP}
    assert "https://172.30.0.7:8443" in text


@requires_openssl
def test_entry_cert_regenerated_when_root_changes(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """root 被替换（指纹变化）后旧叶证书成了孤儿，必须重签。"""
    _wsl_nat(monkeypatch)
    _render(workspace)
    cert, _key = entry_tls_cert_paths(workspace)
    before = cert.read_bytes()

    root = caddy_root_cert_path(workspace)
    root.unlink()
    root.with_name("root.key").unlink()
    _render(workspace)
    assert cert.read_bytes() != before
    proc = _openssl("verify", "-CAfile", str(root), str(cert))
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---- 退化路径 --------------------------------------------------------------------


def test_missing_openssl_falls_back_to_tls_internal_with_warning(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _wsl_nat(monkeypatch)
    monkeypatch.setattr("local_webpage_access.static_gateway._openssl_binary", lambda: None)
    with caplog.at_level(logging.WARNING, logger="local_webpage_access"):
        text = _render(workspace)
    assert text.count("\ttls internal") == 2, "签不出多 SAN 证书时退回旧行为，不让网关起不来"
    assert any("BUG-944" in rec.getMessage() for rec in caplog.records)
    cert, _key = entry_tls_cert_paths(workspace)
    assert not cert.exists()


def test_signing_failure_falls_back_without_partial_files(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """openssl 存在但执行失败：不留半成品证书/密钥，不抛异常。"""
    _wsl_nat(monkeypatch)
    fake = workspace.root / "fake-openssl"
    fake.write_text("#!/bin/sh\necho 'boom' >&2\nexit 3\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setattr(
        "local_webpage_access.static_gateway._openssl_binary", lambda: str(fake)
    )
    with caplog.at_level(logging.WARNING, logger="local_webpage_access"):
        text = _render(workspace)
    assert text.count("\ttls internal") == 2
    cert, key = entry_tls_cert_paths(workspace)
    assert not cert.exists() and not key.exists()
    assert not entry_tls_meta_path(workspace).exists()
    assert any("boom" in rec.getMessage() for rec in caplog.records)


# ---- 重签后强制 reload ------------------------------------------------------------


@requires_openssl
def test_reload_forces_when_certificate_renewed_but_caddyfile_unchanged(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只换证书文件、Caddyfile 文本不变时 Caddy 会判「配置未变化」跳过 reload，
    须 ``caddy reload --force``；证书复用时不加，避免无谓重载。"""
    import local_webpage_access.static_gateway as sg

    _wsl_nat(monkeypatch)
    monkeypatch.setenv("LWA_ALLOW_CADDY_ADMIN", "1")
    monkeypatch.setattr(
        "local_webpage_access.static_gateway.admin_address", lambda _ws: "127.0.0.1:2100"
    )
    gateway = StaticGateway(workspace, _tls_config())
    _with_alias(workspace)

    def reload_cmd() -> list[str]:
        calls: list[list[str]] = []

        def fake_run(cmd, **_kw):  # noqa: ANN001
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

        with monkeypatch.context() as patch:
            patch.setattr(sg.subprocess, "run", fake_run)
            ok, _stderr = gateway._reload_once()
        assert ok and len(calls) == 1
        return calls[0]

    first = gateway._assemble_main_config()
    assert gateway._entry_cert_renewed is True, "首次渲染签发 → 需要强制 reload"
    assert "--force" in reload_cmd()

    second = gateway._assemble_main_config()
    assert second == first
    assert gateway._entry_cert_renewed is False, "证书复用 → 普通 reload"
    assert "--force" not in reload_cmd()


# ---- Caddy 语义 ------------------------------------------------------------------


@requires_openssl
@requires_caddy
def test_caddy_validate_loads_manual_entry_certificate(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``caddy validate`` 真实加载 ``tls <cert> <key>``：PEM 可解析且覆盖全部站点名。"""
    _wsl_nat(monkeypatch)
    text = _render(workspace)
    path = workspace.static_gateway / "Caddyfile"
    path.write_text(text, encoding="utf-8")
    binary = caddy_binary()
    assert binary is not None
    proc = subprocess.run(
        [binary, "validate", "--config", str(path), "--adapter", "caddyfile"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        env=caddy_spawn_env(workspace),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---- 真实 Caddy 无 SNI 握手（默认跳过；LWA_RUN_CADDY_TLS_E2E=1 启用）--------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_listening(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise AssertionError(f"Caddy 未在 {timeout}s 内监听 127.0.0.1:{port}")


def _wait_file(path: Path, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size > 0:
            return
        time.sleep(0.1)
    raise AssertionError(f"{path} 未在 {timeout}s 内生成")


def _handshake_without_sni(port: int, expected_ip: str, cafile: Path) -> None:
    """按 IP 字面量校验证书。CPython 对 IP 字面量不发 SNI，与浏览器 / curl 一致。"""
    context = ssl.create_default_context(cafile=str(cafile))
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        with context.wrap_socket(raw, server_hostname=expected_ip) as tls:
            tls.getpeercert()


def _wait_tls_ready(port: int, expected_ip: str, cafile: Path, timeout: float = 20.0) -> None:
    """等到 Caddy 真能完成握手：``tls internal`` 在启动后异步签发，首批连接会收到
    ``tlsv1 alert internal error``。只对这类非校验错误重试，校验失败立即抛出。"""
    deadline = time.monotonic() + timeout
    while True:
        try:
            _handshake_without_sni(port, expected_ip, cafile)
            return
        except ssl.SSLCertVerificationError:
            raise
        except (ssl.SSLError, OSError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


def _legacy_caddyfile(text: str, ws: Workspace) -> str:
    """把修复后的 ``tls <cert> <key>`` 还原成修复前的逐地址 ``tls internal``。"""
    return text.replace(_manual_tls_directive(ws), "tls internal")


requires_e2e = pytest.mark.skipif(
    os.environ.get(_E2E_ENV) != "1", reason=f"真实 Caddy TLS 握手端到端：设置 {_E2E_ENV}=1 启用"
)


class _CaddyRun:
    """在隔离工作区里拉起一个真实 Caddy（独立 admin 端口与 XDG 目录，BUG-121）。"""

    def __init__(self, ws: Workspace, text: str) -> None:
        binary = caddy_binary()
        assert binary is not None
        self.path = ws.static_gateway / "Caddyfile"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, encoding="utf-8")
        self.proc = subprocess.Popen(
            [binary, "run", "--config", str(self.path), "--adapter", "caddyfile"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=caddy_spawn_env(ws),
        )

    def stop(self) -> str:
        self.proc.terminate()
        try:
            out, _ = self.proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            out, _ = self.proc.communicate()
        return (out or b"").decode("utf-8", "replace")


def _e2e_setup(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> tuple[Config, int, int]:
    from local_webpage_access.admin_port import ensure_admin_port

    ensure_admin_port(ws)
    tls_port, manager_tls = _free_port(), _free_port()
    config = _tls_config(gatewayTlsPort=tls_port, managerTlsPort=manager_tls)
    _wsl_nat(monkeypatch)
    return config, tls_port, manager_tls


@requires_e2e
@requires_openssl
@requires_caddy
def test_real_caddy_existing_ca_then_multi_san(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """存量部署：Caddy 已用 tls internal 建好 CA。

    阶段一（修复前行为）复现 BUG-944：按本地地址选中回环证书，按 Windows IP 校验
    失败；阶段二用 Caddy 自己的 root 直签多 SAN 证书，三个地址在两个端口都通过。
    """
    config, tls_port, manager_tls = _e2e_setup(workspace, monkeypatch)
    fixed = _render(workspace, config)
    assert "tls internal" not in fixed
    root = caddy_root_cert_path(workspace)
    # 清掉自举产物，让阶段一的 Caddy 像老部署一样自己生成 CA
    shutil.rmtree(root.parents[2], ignore_errors=True)
    cert, _key = entry_tls_cert_paths(workspace)
    cert.unlink()

    legacy = _CaddyRun(workspace, _legacy_caddyfile(fixed, workspace))
    try:
        _wait_listening(manager_tls)
        _wait_file(root)
        _wait_tls_ready(manager_tls, "127.0.0.1", root)
        with pytest.raises(ssl.SSLCertVerificationError, match=re.escape(WINDOWS_IP)):
            _handshake_without_sni(manager_tls, WINDOWS_IP, root)
    finally:
        legacy_log = legacy.stop()
    assert root.is_file(), legacy_log

    fingerprint = caddy_root_cert_fingerprint(workspace)
    fixed = _render(workspace, config)
    assert caddy_root_cert_fingerprint(workspace) == fingerprint, "沿用 Caddy 的 root"
    run = _CaddyRun(workspace, fixed)
    try:
        _wait_listening(manager_tls)
        _wait_listening(tls_port)
        _wait_tls_ready(manager_tls, "127.0.0.1", root)
        for ip in ("127.0.0.1", GUEST_IP, WINDOWS_IP):
            _handshake_without_sni(manager_tls, ip, root)
            _handshake_without_sni(tls_port, ip, root)
    finally:
        run.stop()


@requires_e2e
@requires_openssl
@requires_caddy
def test_real_caddy_adopts_bootstrapped_root(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全新 WSL 工作区：LWA 先自举 root；之后若配置回到 tls internal，Caddy 必须沿用该 root。"""
    config, _tls_port, manager_tls = _e2e_setup(workspace, monkeypatch)
    fixed = _render(workspace, config)
    root = caddy_root_cert_path(workspace)
    fingerprint = caddy_root_cert_fingerprint(workspace)
    assert fingerprint is not None

    run = _CaddyRun(workspace, _legacy_caddyfile(fixed, workspace))
    try:
        _wait_listening(manager_tls)
        _wait_tls_ready(manager_tls, "127.0.0.1", root)
    finally:
        run.stop()
    assert caddy_root_cert_fingerprint(workspace) == fingerprint, "Caddy 不得重建已自举的 root"
