"""访问地址刷新与可用性复核测试（建议 B/C/E/I，gateway-switch-access-review）。

覆盖：
* :func:`refresh_network_entries`（G1）—— LAN IP 变化后重算 lanUrl/routeUrl、
  漂移检测、保留 hostPort/别名、幂等。
* :func:`review_access`（G2/G5）—— 回环探活、lanUrl 漂移、IMP-023 空 200 子资源。
* :func:`maybe_rebuild_after_review`（G6）—— 默认只提示；``--rebuild-if-needed``
  仅对 IMP-023 命中实例调用 rebuild。
* 切换事务（建议 A）—— ``enable()`` 停活 builtin、``stop_all_builtin`` 清孤儿。
"""

from __future__ import annotations

import http.server
import socketserver
import threading
from pathlib import Path

import pytest

from local_webpage_access.access import (
    _collect_api_paths,
    _extract_api_paths,
    _extract_js_bundle_paths,
    _fetch_text,
    format_rebuild_advice,
    format_review_report,
    instance_still_has_imp023,
    instances_needing_rebuild,
    maybe_rebuild_after_review,
    refresh_network_entries,
    review_access,
)
from local_webpage_access import access as access_mod
from local_webpage_access.models import DesiredState, InstanceManifest, Status


# ---- 工具：构造静态实例 manifest --------------------------------------------


def _seed_static(
    workspace,
    registry,
    iid: str = "demo",
    *,
    host_port: int = 21000,
    lan_url: str | None = "http://10.0.0.99:21000",
    route_host: str | None = None,
    route_url: str | None = None,
):
    """种入一个 shared-static 实例（含 manifest + registry 行）。"""
    from local_webpage_access.models import (
        InstanceManifest,
        Kind,
        NetworkConfig,
        RouteMode,
        Runtime,
        ServingMode,
        StaticConfig,
        Status,
        DesiredState,
        ResourceProfile,
    )

    workspace.ensure_app_dirs(iid)
    manifest = InstanceManifest(
        id=iid,
        name=iid,
        version="1",
        kind=Kind.STATIC,
        runtime=Runtime.SHARED_STATIC,
        servingMode=ServingMode.SHARED_STATIC,
        resourceProfile=ResourceProfile.TINY,
        status=Status.RUNNING,
        desiredState=DesiredState.RUNNING,
        static=StaticConfig(
            root="public",
            hostPort=host_port,
            routeMode=(RouteMode.NAME.value if route_host else RouteMode.PORT.value),
            routeHost=route_host,
            enabled=True,
        ),
        network=NetworkConfig(
            hostPort=host_port,
            routeMode=(RouteMode.NAME.value if route_host else RouteMode.PORT.value),
            routeHost=route_host,
            routeUrl=route_url,
            lanUrl=lan_url,
        ),
    )
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)
    registry.upsert_static_site(
        iid,
        {
            "root": "public",
            "gateway": "caddy",
            "routeMode": (RouteMode.NAME.value if route_host else RouteMode.PORT.value),
            "hostPort": host_port,
            "routeHost": route_host,
            "enabled": True,
        },
    )
    return manifest


# ---- refresh_network_entries（G1）------------------------------------------


def test_refresh_rewrites_lanurl_on_drift(workspace, registry, config, monkeypatch):
    """LAN IP 变化后 refresh 重写 lanUrl 并报告漂移。"""
    _seed_static(workspace, registry, "demo", host_port=21000,
                 lan_url="http://10.0.0.99:21000")
    monkeypatch.setattr("local_webpage_access.access.resolve_lan_ip",
                        lambda cfg: "192.168.1.50")

    report = refresh_network_entries(workspace, config, registry)

    assert report.lan_ip == "192.168.1.50"
    assert len(report.refreshed) == 1
    item = report.refreshed[0]
    assert item.instance_id == "demo"
    assert item.drifted is True
    assert item.old_host == "10.0.0.99"
    assert item.new_host == "192.168.1.50"
    assert item.lan_url == "http://192.168.1.50:21000"

    # manifest 已落盘
    from local_webpage_access.models import InstanceManifest

    saved = InstanceManifest.load(workspace.app_manifest_path("demo"))
    assert saved.network.lanUrl == "http://192.168.1.50:21000"
    # hostPort 保留
    assert saved.static.hostPort == 21000


def test_refresh_preserves_path_alias_and_routeurl(workspace, registry, config, monkeypatch):
    """刷新保留 pathAlias，并按当前 LAN IP 重算 routeUrl。"""
    _seed_static(workspace, registry, "vp", host_port=21001,
                 lan_url="http://10.0.0.99:21001", route_host="voiceprint")
    monkeypatch.setattr("local_webpage_access.access.resolve_lan_ip",
                        lambda cfg: "192.168.1.50")

    report = refresh_network_entries(workspace, config, registry)
    item = report.refreshed[0]
    assert item.route_url == "http://192.168.1.50:8080/voiceprint/"

    from local_webpage_access.models import InstanceManifest

    saved = InstanceManifest.load(workspace.app_manifest_path("vp"))
    assert saved.static.routeHost == "voiceprint"  # 别名保留
    assert saved.network.routeUrl == "http://192.168.1.50:8080/voiceprint/"


def test_refresh_preserves_network_extra_fields(
    workspace, registry, config, monkeypatch
) -> None:
    """BUG-365：refresh 只更新地址字段，不得抹掉 NetworkConfig extra=allow 的扩展键。"""
    _seed_static(
        workspace,
        registry,
        "demo",
        host_port=21000,
        lan_url="http://10.0.0.99:21000",
    )
    from local_webpage_access.models import InstanceManifest, NetworkConfig

    manifest_path = workspace.app_manifest_path("demo")
    manifest = InstanceManifest.load(manifest_path)
    manifest.network = NetworkConfig(
        lanUrl=manifest.network.lanUrl if manifest.network else "",
        customMeta={"source": "skill"},
    )
    manifest.save(manifest_path)

    monkeypatch.setattr(
        "local_webpage_access.access.resolve_lan_ip", lambda cfg: "192.168.1.50"
    )
    refresh_network_entries(workspace, config, registry)

    saved = InstanceManifest.load(manifest_path)
    assert saved.network.lanUrl == "http://192.168.1.50:21000"
    assert saved.network.customMeta == {"source": "skill"}


def test_refresh_ignores_stale_routehost_when_port_mode(
    workspace, registry, config, monkeypatch
) -> None:
    """BUG-109：routeMode=port 时残留 routeHost 不得被 refresh 写回别名模式。"""
    from local_webpage_access.access import _extract_host_port_alias
    from local_webpage_access.models import (
        ContainerConfig,
        DesiredState,
        InstanceManifest,
        Kind,
        NetworkConfig,
        ResourceProfile,
        RouteMode,
        Runtime,
        ServingMode,
        StaticConfig,
        Status,
    )

    iid = "ctr"
    workspace.ensure_app_dirs(iid)
    manifest = InstanceManifest(
        id=iid,
        name=iid,
        version="1",
        kind=Kind.NODE,
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.TINY,
        status=Status.RUNNING,
        desiredState=DesiredState.RUNNING,
        # 残留 static.routeHost，但容器与 network 均为 port 模式
        static=StaticConfig(
            root="public",
            routeMode=RouteMode.PORT.value,
            routeHost="stale-alias",
        ),
        container=ContainerConfig(
            projectName="ctr",
            composePath="compose.yml",
            dockerfilePath="Dockerfile",
            hostPort=21001,
            internalPort=3000,
            routeMode=RouteMode.PORT.value,
            routeHost=None,
        ),
        network=NetworkConfig(
            hostPort=21001,
            internalPort=3000,
            routeMode=RouteMode.PORT.value,
            routeHost=None,
            routeUrl=None,
            lanUrl="http://10.0.0.99:21001",
        ),
    )
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)

    # review 仍应看到残留别名（I2）；refresh 不得持久化
    _hp, review_alias, _ip = _extract_host_port_alias(manifest, for_review=True)
    assert review_alias == "stale-alias"
    _hp2, active_alias, _ip2 = _extract_host_port_alias(manifest, for_review=False)
    assert active_alias is None

    monkeypatch.setattr(
        "local_webpage_access.access.resolve_lan_ip", lambda cfg: "192.168.1.50"
    )
    report = refresh_network_entries(workspace, config, registry)
    assert report.refreshed[0].route_url is None

    saved = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert saved.network.routeMode == RouteMode.PORT.value
    assert saved.network.routeHost is None
    assert saved.network.routeUrl is None
    assert saved.network.lanUrl == "http://192.168.1.50:21001"
    # 磁盘残留 static.routeHost 可保留（未主动清理），但不得升格 network
    assert saved.static.routeHost == "stale-alias"


def test_refresh_is_idempotent_when_no_drift(workspace, registry, config, monkeypatch):
    """地址未漂移时刷新幂等，drifted_count=0。"""
    _seed_static(workspace, registry, "demo", host_port=21000,
                 lan_url="http://192.168.1.50:21000")
    monkeypatch.setattr("local_webpage_access.access.resolve_lan_ip",
                        lambda cfg: "192.168.1.50")

    report = refresh_network_entries(workspace, config, registry)
    assert report.drifted_count == 0
    assert report.refreshed[0].drifted is False


def test_refresh_skips_instance_without_hostport(workspace, registry, config, monkeypatch):
    """无 hostPort 的实例被跳过，不报错。"""
    from local_webpage_access.models import (
        InstanceManifest, Kind, Runtime, ServingMode, Status, DesiredState,
    )

    workspace.ensure_app_dirs("noport")
    manifest = InstanceManifest(
        id="noport", name="noport", version="1", kind=Kind.STATIC,
        runtime=Runtime.SHARED_STATIC, servingMode=ServingMode.SHARED_STATIC,
        status=Status.PENDING, desiredState=DesiredState.STOPPED,
    )
    manifest.save(workspace.app_manifest_path("noport"))
    registry.upsert_from_manifest(manifest)
    monkeypatch.setattr("local_webpage_access.access.resolve_lan_ip",
                        lambda cfg: "192.168.1.50")

    report = refresh_network_entries(workspace, config, registry)
    assert "noport" in report.skipped


def test_refresh_skips_write_when_lan_ip_unavailable(
    workspace, registry, config, monkeypatch
) -> None:
    """BUG-325：LAN IP 探测失败时不得把现有 lanUrl/routeUrl 写成 None。"""
    _seed_static(
        workspace,
        registry,
        "demo",
        host_port=21000,
        lan_url="http://10.0.0.99:21000",
        route_host="demo-alias",
        route_url="http://10.0.0.99:8080/demo-alias/",
    )
    from local_webpage_access.models import InstanceManifest

    monkeypatch.setattr(
        "local_webpage_access.access.resolve_lan_ip", lambda cfg: None
    )

    report = refresh_network_entries(workspace, config, registry)

    assert report.lan_ip is None
    assert report.refreshed == []
    assert "demo" in report.skipped
    saved = InstanceManifest.load(workspace.app_manifest_path("demo"))
    assert saved.network is not None
    assert saved.network.lanUrl == "http://10.0.0.99:21000"
    assert saved.network.routeUrl == "http://10.0.0.99:8080/demo-alias/"


# ---- review_access：lanUrl 漂移（G1/G5）------------------------------------


def test_review_detects_lan_url_stale(workspace, registry, config, monkeypatch):
    """lanUrl host 与当前 LAN IP 不一致 → lan_url_stale=True。"""
    _seed_static(workspace, registry, "demo", host_port=21000,
                 lan_url="http://10.0.0.99:21000")
    monkeypatch.setattr("local_webpage_access.access.resolve_lan_ip",
                        lambda cfg: "192.168.1.50")
    # 回环探活通过（服务在本机跑）
    monkeypatch.setattr(
        "local_webpage_access.access._http_get",
        lambda url, **kw: _probe_ok(url),
    )

    report = review_access(workspace, config, registry)
    rep = report.instances[0]
    assert rep.lan_url_stale is True
    assert rep.status == "warn"
    assert any("漂移" in f for f in rep.findings)


def test_review_skips_desired_stopped_instance(
    workspace, registry, config, monkeypatch
) -> None:
    """BUG-301：用户已停用的实例不应因端口不可达被判访问失败。"""
    _seed_static(
        workspace,
        registry,
        "demo",
        host_port=21000,
        lan_url="http://127.0.0.1:21000",
    )
    manifest = InstanceManifest.load(workspace.app_manifest_path("demo"))
    manifest.desiredState = DesiredState.STOPPED
    manifest.status = Status.STOPPED
    manifest.save(workspace.app_manifest_path("demo"))
    registry.upsert_from_manifest(manifest)
    monkeypatch.setattr(
        "local_webpage_access.access._http_get",
        lambda *a, **kw: pytest.fail("停用实例不应发起 HTTP 探测"),
    )

    report = review_access(workspace, config, registry)

    assert report.instances[0].status == "skip"
    assert any("已停用" in item for item in report.instances[0].findings)


def _probe_ok(url, status=200, length=1024):
    from local_webpage_access.access import UrlProbe

    return UrlProbe(url=url, status_code=status, content_length=length, ok=True)


# ---- review_access：IMP-023 空 200 子资源（E，真实 HTTP）-------------------


class _SpaHandler(http.server.BaseHTTPRequestHandler):
    """模拟 IMP-023 场景：别名入口 HTML 含绝对资源；绝对路径空 200，带前缀有实体。"""

    HTML = (
        b'<!doctype html><html><head>'
        b'<script type="module" src="/assets/app.js"></script>'
        b'</head><body>spa</body></html>'
    )

    def do_GET(self):  # noqa: N802
        # 真实静态服务器（http.server / Caddy）忽略 query；剥掉 __lwa_probe 等
        path = self.path.split("?", 1)[0]
        if path == "/" or path == "/alias/":
            self._send(200, self.HTML if path == "/alias/" else b"root")
        elif path == "/alias/assets/app.js":
            self._send(200, b"x" * 1200)  # 带前缀：有实体
        elif path == "/assets/app.js":
            self._send(200, b"")  # 绝对路径：空 200（IMP-023）
        else:
            self._send(404, b"nf")

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # 静默
        pass


@pytest.fixture()
def spa_server():
    """启动一个模拟别名入口的真实 HTTP 服务（IMP-023 空 200 场景）。"""
    port = _free_port()
    httpd = socketserver.TCPServer(("127.0.0.1", port), _SpaHandler)
    httpd.allow_reuse_address = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_review_detects_spa_empty_200(workspace, registry, config, spa_server, monkeypatch):
    """E：别名入口绝对路径资源返回 200 但 0 字节 → IMP-023 风险（WARN）。"""
    port = spa_server
    # host_port 与 staticGatewayPort 都指向测试服务（同一端口的 `/` 返回 200）
    config.staticGatewayPort = port
    _seed_static(
        workspace, registry, "vp", host_port=port,
        lan_url=f"http://127.0.0.1:{port}",  # 同机，不漂移
        route_host="alias",
        route_url=f"http://127.0.0.1:{port}/alias/",
    )
    monkeypatch.setattr("local_webpage_access.access.resolve_lan_ip",
                        lambda cfg: "127.0.0.1")

    report = review_access(workspace, config, registry)
    rep = report.instances[0]
    assert rep.status == "warn"
    assert rep.subresources, "应检测到绝对路径子资源"
    empty = [s for s in rep.subresources if s.empty_200]
    assert empty, "应识别出 IMP-023 空 200 子资源"
    assert empty[0].path == "/assets/app.js"
    assert empty[0].absolute.content_length == 0
    assert empty[0].prefixed.content_length == 1200
    assert empty[0].alias_resource_mismatch is True
    assert any("IMP-023" in f for f in rep.findings)


class _Spa404Handler(http.server.BaseHTTPRequestHandler):
    """BUG-381：绝对路径 404、带前缀 200 —— 常见别名白屏（非空 200）。"""

    HTML = (
        b'<!doctype html><html><head>'
        b'<script type="module" src="/assets/app.js"></script>'
        b'</head><body>spa</body></html>'
    )

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/" or path == "/alias/":
            self._send(200, self.HTML if path == "/alias/" else b"root", "text/html")
        elif path == "/alias/assets/app.js":
            self._send(200, b"x" * 1200, "application/javascript")
        elif path == "/assets/app.js":
            self._send(404, b"nf", "text/plain")
        else:
            self._send(404, b"nf", "text/plain")

    def _send(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class _SpaMimeHandler(http.server.BaseHTTPRequestHandler):
    """BUG-381：绝对路径返回非空 HTML（错误 MIME），带前缀返回真实 JS。"""

    HTML = (
        b'<!doctype html><html><head>'
        b'<script type="module" src="/assets/app.js"></script>'
        b'</head><body>spa</body></html>'
    )

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/" or path == "/alias/":
            self._send(200, self.HTML if path == "/alias/" else b"root", "text/html")
        elif path == "/alias/assets/app.js":
            self._send(200, b"x" * 1200, "application/javascript")
        elif path == "/assets/app.js":
            self._send(200, b"<html>not found</html>", "text/html")
        else:
            self._send(404, b"nf", "text/plain")

    def _send(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture()
def spa_404_server():
    port = _free_port()
    httpd = socketserver.TCPServer(("127.0.0.1", port), _Spa404Handler)
    httpd.allow_reuse_address = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture()
def spa_mime_server():
    port = _free_port()
    httpd = socketserver.TCPServer(("127.0.0.1", port), _SpaMimeHandler)
    httpd.allow_reuse_address = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_review_detects_spa_absolute_404_mismatch(
    workspace, registry, config, spa_404_server, monkeypatch
):
    """BUG-381：绝对路径 404 + 带前缀 200 → alias_resource_mismatch / needsRebuild。"""
    port = spa_404_server
    config.staticGatewayPort = port
    _seed_static(
        workspace, registry, "vp404", host_port=port,
        lan_url=f"http://127.0.0.1:{port}",
        route_host="alias",
        route_url=f"http://127.0.0.1:{port}/alias/",
    )
    monkeypatch.setattr(
        "local_webpage_access.access.resolve_lan_ip", lambda cfg: "127.0.0.1"
    )

    report = review_access(workspace, config, registry)
    rep = report.instances[0]
    assert rep.status == "warn"
    mismatched = [s for s in rep.subresources if s.alias_resource_mismatch]
    assert mismatched, "应识别出别名资源不匹配"
    assert mismatched[0].path == "/assets/app.js"
    assert mismatched[0].empty_200 is False
    assert mismatched[0].absolute.status_code == 404
    assert mismatched[0].prefixed.ok is True
    assert rep.needs_rebuild is True
    assert "vp404" in instances_needing_rebuild(report)
    assert any("IMP-023" in f for f in rep.findings)


def test_alias_mismatch_ignores_connection_failures() -> None:
    """BUG-399：绝对路径 TIMEOUT/REFUSED 不得判为 IMP-023。"""
    from local_webpage_access.access import UrlProbe, _alias_resource_mismatch

    prefixed = UrlProbe(url="http://x/alias/a.js", ok=True, content_length=10)
    for note in ("TIMEOUT", "REFUSED", "UNREACHABLE"):
        absolute = UrlProbe(
            url="http://x/a.js", ok=False, status_code=None, note=note
        )
        empty, mismatch = _alias_resource_mismatch("/a.js", absolute, prefixed)
        assert empty is False
        assert mismatch is False, note
    # HTTP 404 仍应命中
    abs404 = UrlProbe(url="http://x/a.js", ok=False, status_code=404, note=None)
    _e, mismatch404 = _alias_resource_mismatch("/a.js", abs404, prefixed)
    assert mismatch404 is True


def test_review_detects_spa_wrong_mime_mismatch(
    workspace, registry, config, spa_mime_server, monkeypatch
) -> None:
    """BUG-381：绝对路径错误 MIME（text/html）+ 带前缀 JS → mismatch。"""
    port = spa_mime_server
    config.staticGatewayPort = port
    _seed_static(
        workspace, registry, "vpmime", host_port=port,
        lan_url=f"http://127.0.0.1:{port}",
        route_host="alias",
        route_url=f"http://127.0.0.1:{port}/alias/",
    )
    monkeypatch.setattr(
        "local_webpage_access.access.resolve_lan_ip", lambda cfg: "127.0.0.1"
    )

    report = review_access(workspace, config, registry)
    rep = report.instances[0]
    mismatched = [s for s in rep.subresources if s.alias_resource_mismatch]
    assert mismatched, "应识别出错误 MIME 不匹配"
    assert mismatched[0].empty_200 is False
    assert mismatched[0].absolute.ok is True
    assert (mismatched[0].absolute.content_length or 0) > 0
    assert rep.needs_rebuild is True
    assert any("IMP-023" in f for f in rep.findings)


def test_review_no_mismatch_when_both_absolute_and_prefixed_fail(
    workspace, registry, config, monkeypatch
):
    """BUG-381 矩阵：两边都失败 → 不误判成 alias mismatch。"""
    from local_webpage_access.access import UrlProbe

    config.staticGatewayPort = 18080
    _seed_static(
        workspace, registry, "bothfail", host_port=18080,
        lan_url="http://127.0.0.1:18080",
        route_host="alias",
        route_url="http://127.0.0.1:18080/alias/",
    )
    monkeypatch.setattr(
        "local_webpage_access.access.resolve_lan_ip", lambda cfg: "127.0.0.1"
    )
    monkeypatch.setattr(
        "local_webpage_access.access._fetch_text",
        lambda url, **kw: (
            '<script src="/assets/app.js"></script>' if "/alias/" in url else None
        ),
    )

    def fake_get(url, **kw):
        # 入口/回环通；绝对与带前缀资源均 404
        if url.rstrip("/").endswith("/alias") or url.endswith(":18080/"):
            return UrlProbe(url=url, status_code=200, content_length=10, ok=True)
        return UrlProbe(
            url=url, status_code=404, content_length=0, ok=False, note="HTTP 404"
        )

    monkeypatch.setattr("local_webpage_access.access._http_get", fake_get)
    report = review_access(workspace, config, registry)
    rep = next(r for r in report.instances if r.instance_id == "bothfail")
    mismatched = [s for s in rep.subresources if s.alias_resource_mismatch]
    assert mismatched == []
    assert rep.needs_rebuild is False


def test_review_no_mismatch_when_absolute_mime_correct(
    workspace, registry, config, spa_ok_server, monkeypatch
):
    """BUG-381 矩阵：绝对与带前缀均正确 MIME/非空 → 不告警。"""
    port = spa_ok_server
    config.staticGatewayPort = port
    _seed_static(
        workspace, registry, "okmime", host_port=port,
        lan_url=f"http://127.0.0.1:{port}",
        route_host="alias",
        route_url=f"http://127.0.0.1:{port}/alias/",
    )
    monkeypatch.setattr(
        "local_webpage_access.access.resolve_lan_ip", lambda cfg: "127.0.0.1"
    )
    report = review_access(workspace, config, registry)
    rep = report.instances[0]
    assert rep.subresources
    assert all(not s.alias_resource_mismatch for s in rep.subresources)
    assert rep.needs_rebuild is False


class _SpaOkHandler(http.server.BaseHTTPRequestHandler):
    HTML = (
        b'<!doctype html><html><head>'
        b'<script type="module" src="/assets/app.js"></script>'
        b'</head><body>spa</body></html>'
    )

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in {"/", "/alias/"}:
            body = self.HTML if path == "/alias/" else b"root"
            self._send(200, body, "text/html")
        elif path in {"/assets/app.js", "/alias/assets/app.js"}:
            self._send(200, b"x" * 800, "application/javascript")
        else:
            self._send(404, b"nf", "text/plain")

    def _send(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture()
def spa_ok_server():
    port = _free_port()
    httpd = socketserver.TCPServer(("127.0.0.1", port), _SpaOkHandler)
    httpd.allow_reuse_address = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_http_get_ignores_env_http_proxy(monkeypatch) -> None:
    """BUG-380：access._http_get 在无效代理下仍直连本机。"""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread

    from local_webpage_access.access import _http_get

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _H)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("https_proxy", "http://127.0.0.1:1")
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.delenv("NO_PROXY", raising=False)
        probe = _http_get(f"http://127.0.0.1:{port}/")
        assert probe.ok is True
        assert probe.status_code == 200
    finally:
        server.shutdown()
        server.server_close()


def test_review_synthesizes_route_when_route_url_missing(
    workspace, registry, config, spa_server, monkeypatch
):
    """I2：network.routeUrl 为空但仍有 static.routeHost → 合成别名入口并做空 200 检测。"""
    port = spa_server
    config.staticGatewayPort = port
    _seed_static(
        workspace, registry, "prd", host_port=port,
        lan_url=f"http://127.0.0.1:{port}",
        route_host="alias",
        route_url=None,  # 元数据漂移：有别名无 routeUrl
    )
    monkeypatch.setattr("local_webpage_access.access.resolve_lan_ip",
                        lambda cfg: "127.0.0.1")

    report = review_access(workspace, config, registry)
    rep = report.instances[0]
    assert rep.status == "warn"
    assert any("合成探测" in f for f in rep.findings)
    empty = [s for s in rep.subresources if s.empty_200]
    assert empty, "routeUrl 为空时仍应检出 IMP-023 空 200"
    assert any("IMP-023" in f for f in rep.findings)


# ---- G6：切换后 rebuild 兼容检查 -------------------------------------------


def test_instances_needing_rebuild_from_imp023(
    workspace, registry, config, spa_server, monkeypatch
) -> None:
    """G6：IMP-023 空 200 → needs_rebuild / needsRebuild 列表。"""
    port = spa_server
    config.staticGatewayPort = port
    _seed_static(
        workspace, registry, "vp", host_port=port,
        lan_url=f"http://127.0.0.1:{port}",
        route_host="alias",
        route_url=f"http://127.0.0.1:{port}/alias/",
    )
    monkeypatch.setattr(
        "local_webpage_access.access.resolve_lan_ip", lambda cfg: "127.0.0.1"
    )

    report = review_access(workspace, config, registry)
    assert instances_needing_rebuild(report) == ["vp"]
    assert report.instances[0].needs_rebuild is True
    assert report.to_dict()["needsRebuild"] == ["vp"]
    text = format_review_report(report)
    assert "建议 rebuild" in text
    assert "lwa rebuild vp" in text
    assert "--rebuild-if-needed" in text


def test_maybe_rebuild_skipped_without_flag(
    workspace, registry, config, spa_server, monkeypatch
) -> None:
    """G6：无 --rebuild-if-needed 时不调用 rebuild，仅列出候选。"""
    port = spa_server
    config.staticGatewayPort = port
    _seed_static(
        workspace, registry, "vp", host_port=port,
        lan_url=f"http://127.0.0.1:{port}",
        route_host="alias",
        route_url=f"http://127.0.0.1:{port}/alias/",
    )
    monkeypatch.setattr(
        "local_webpage_access.access.resolve_lan_ip", lambda cfg: "127.0.0.1"
    )
    report = review_access(workspace, config, registry)
    calls: list[str] = []

    def fake_rebuild(ws, cfg, reg, iid):
        calls.append(iid)

    out = maybe_rebuild_after_review(
        workspace, config, registry, report,
        rebuild_if_needed=False,
        rebuild_fn=fake_rebuild,
    )
    assert out.skipped is True
    assert out.candidates == ["vp"]
    assert calls == []
    assert out.all_ok is True


def test_maybe_rebuild_runs_when_flag_set(
    workspace, registry, config, spa_server, monkeypatch
) -> None:
    """G6：--rebuild-if-needed 时对 IMP-023 命中实例调用 rebuild。"""
    port = spa_server
    config.staticGatewayPort = port
    _seed_static(
        workspace, registry, "vp", host_port=port,
        lan_url=f"http://127.0.0.1:{port}",
        route_host="alias",
        route_url=f"http://127.0.0.1:{port}/alias/",
    )
    monkeypatch.setattr(
        "local_webpage_access.access.resolve_lan_ip", lambda cfg: "127.0.0.1"
    )
    # 复检桩为「已修复」，避免 spa_server 仍返回绝对路径导致 still_imp023。
    monkeypatch.setattr(
        "local_webpage_access.access.instance_still_has_imp023",
        lambda cfg, *, path_alias: False,
    )
    report = review_access(workspace, config, registry)
    calls: list[str] = []

    def fake_rebuild(ws, cfg, reg, iid):
        calls.append(iid)

    out = maybe_rebuild_after_review(
        workspace, config, registry, report,
        rebuild_if_needed=True,
        rebuild_fn=fake_rebuild,
    )
    assert out.skipped is False
    assert calls == ["vp"]
    assert out.results[0].ok is True
    assert out.results[0].still_imp023 is False
    assert out.all_ok is True
    text = format_review_report(report, rebuild_report=out)
    assert "已自动 rebuild vp" in text
    assert "复检通过" in text


def test_maybe_rebuild_still_imp023_when_assets_unchanged(
    workspace, registry, config, spa_server, monkeypatch
) -> None:
    """G6：rebuild 调用成功但产物仍绝对路径 → still_imp023，勿假绿。"""
    port = spa_server
    config.staticGatewayPort = port
    _seed_static(
        workspace, registry, "vp", host_port=port,
        lan_url=f"http://127.0.0.1:{port}",
        route_host="alias",
        route_url=f"http://127.0.0.1:{port}/alias/",
    )
    monkeypatch.setattr(
        "local_webpage_access.access.resolve_lan_ip", lambda cfg: "127.0.0.1"
    )
    report = review_access(workspace, config, registry)
    # spa_server 仍提供绝对 /assets → 复检应仍命中
    assert instance_still_has_imp023(config, path_alias="alias") is True

    out = maybe_rebuild_after_review(
        workspace, config, registry, report,
        rebuild_if_needed=True,
        rebuild_fn=lambda *a, **k: None,
    )
    assert out.results[0].ok is True
    assert out.results[0].still_imp023 is True
    assert out.results[0].resolved is False
    assert out.all_ok is False
    text = format_review_report(report, rebuild_report=out)
    assert "IMP-023 仍命中" in text
    assert "base: './'" in text
    assert "[WARN]" in text


def test_maybe_rebuild_ignores_non_imp023_warnings(
    workspace, registry, config, monkeypatch
) -> None:
    """G6：仅 LAN 漂移等 WARN 不进入 rebuild 候选。"""
    _seed_static(
        workspace, registry, "demo", host_port=21000,
        lan_url="http://10.0.0.99:21000",  # 与当前 IP 不一致 → 漂移
    )
    monkeypatch.setattr(
        "local_webpage_access.access.resolve_lan_ip", lambda cfg: "10.0.0.1"
    )
    # 回环不通时 status=fail，仍不应因漂移触发 rebuild；此处桩回环为通。
    monkeypatch.setattr(
        "local_webpage_access.access._http_get",
        lambda url, **kw: __import__(
            "local_webpage_access.access", fromlist=["UrlProbe"]
        ).UrlProbe(url=url, status_code=200, content_length=10, ok=True),
    )
    report = review_access(workspace, config, registry)
    assert any(r.lan_url_stale for r in report.instances)
    assert instances_needing_rebuild(report) == []
    calls: list[str] = []
    out = maybe_rebuild_after_review(
        workspace, config, registry, report,
        rebuild_if_needed=True,
        rebuild_fn=lambda *a, **k: calls.append(a[3]),
    )
    assert calls == []
    assert out.candidates == []


# ---- 切换事务：enable() 停活 builtin（建议 A）------------------------------


def test_enable_caddy_stops_live_builtin(workspace, config, monkeypatch):
    """G3：切换到 caddy 时，enable() 先停掉该实例仍存活的 builtin 进程。"""
    from local_webpage_access.static_gateway import StaticGateway

    gateway = StaticGateway(workspace, config)
    # 模拟一个「存活」的 builtin pid 文件（不真正起进程，用当前进程 pid 兜底判定）
    calls = {"stopped": []}
    monkeypatch.setattr(gateway, "_stop_builtin",
                        lambda iid: calls["stopped"].append(iid))
    monkeypatch.setattr(gateway, "_read_pid", lambda iid: 99999)
    monkeypatch.setattr(gateway, "_pid_alive", lambda pid: True)
    # enable 的其余依赖桩掉
    monkeypatch.setattr(gateway, "_clear_stale_static_pid", lambda iid: None)
    monkeypatch.setattr(gateway, "generate_site_config",
                        lambda iid, hp, root: Path("/tmp/x"))
    monkeypatch.setattr(gateway, "detect_backend", lambda: "caddy")
    monkeypatch.setattr(gateway, "reload_all", lambda: None)

    root = workspace.root / "public"
    root.mkdir(parents=True, exist_ok=True)
    gateway.enable("demo", 21000, root)

    assert calls["stopped"] == ["demo"], "enable 前应先停掉存活 builtin"


def test_stop_all_builtin_clears_live_and_dead(workspace, config, monkeypatch):
    """stop_all_builtin 停存活进程、清死 pid 文件，返回被停实例列表。"""
    from local_webpage_access.static_gateway import StaticGateway

    gateway = StaticGateway(workspace, config)
    # 两个 pid 文件：一个存活，一个死
    workspace.run.mkdir(parents=True, exist_ok=True)
    (workspace.run / "static-live.pid").write_text("111")
    (workspace.run / "static-dead.pid").write_text("222")

    stopped = []
    monkeypatch.setattr(gateway, "_read_pid", lambda iid: {"live": 111, "dead": 222}.get(iid))
    monkeypatch.setattr(gateway, "_pid_alive",
                        lambda pid: pid == 111)  # live 存活，dead 已死
    monkeypatch.setattr(gateway, "_stop_builtin", lambda iid: stopped.append(iid))
    monkeypatch.setattr(gateway, "_clear_stale_static_pid", lambda iid: None)
    # 无 pid-less 孤儿
    monkeypatch.setattr(gateway, "_enumerate_workspace_builtin_pids", lambda: [])

    result = gateway.stop_all_builtin()
    assert result == ["live"]
    assert stopped == ["live"]


def test_stop_all_builtin_kills_pid_less_orphans(workspace, config, monkeypatch):
    """§2.7：pid 文件已丢失的孤儿（PPID=1）靠 workspace 枚举捕获并杀掉。"""
    from local_webpage_access.static_gateway import StaticGateway

    gateway = StaticGateway(workspace, config)
    # 无 pid 文件，但枚举发现一个孤儿
    monkeypatch.setattr(gateway, "_read_pid", lambda iid: None)
    monkeypatch.setattr(gateway, "_enumerate_workspace_builtin_pids",
                        lambda: [(65599, "demo-static")])
    killed = []
    monkeypatch.setattr(gateway, "_kill_process",
                        lambda pid, proc=None, **kw: killed.append(pid) or True)

    result = gateway.stop_all_builtin()
    assert result == ["demo-static"]
    assert killed == [65599]


def test_enumerate_workspace_builtin_pids_parses_pgrep_lf(
    workspace, config, monkeypatch
):
    """§10.2-C1：解析 ``pgrep -lf`` 完整命令行；拒绝仅 PID 的 -af 形态。"""
    from local_webpage_access.static_gateway import StaticGateway

    gateway = StaticGateway(workspace, config)
    apps = str(workspace.apps)
    cmdline = (
        f"65599 /usr/bin/python -u -m http.server 18000 "
        f"--directory {apps}/demo-static/public --bind 0.0.0.0"
    )
    captured: dict = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = list(cmd)

        class _R:
            returncode = 0
            stdout = cmdline + "\n"
            stderr = ""

        return _R()

    monkeypatch.setattr(
        "local_webpage_access.static_gateway.subprocess.run", fake_run
    )
    found = gateway._enumerate_workspace_builtin_pids()
    assert captured["cmd"][:2] == ["pgrep", "-lf"]
    assert found == [(65599, "demo-static")]

    # Darwin -af 形态：只有 PID → 应被过滤（无 cmdline）
    def fake_af(cmd, **kw):
        class _R:
            returncode = 0
            stdout = "65599\n"
            stderr = ""

        return _R()

    monkeypatch.setattr(
        "local_webpage_access.static_gateway.subprocess.run", fake_af
    )
    assert gateway._enumerate_workspace_builtin_pids() == []


def test_fetch_text_marks_probe_param(monkeypatch) -> None:
    """BUG-179：_fetch_text 带 __lwa_probe=1，不被 pageviews 计为真实浏览。"""
    from local_webpage_access import access as access_mod

    captured = {}

    class _Resp:
        status = 200

        def read(self):
            return b"<html>alias entry</html>"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(access_mod, "urlopen_direct", fake_urlopen)
    text = _fetch_text("http://10.0.0.1:8080/my-app/")
    assert text == "<html>alias entry</html>"
    assert "__lwa_probe=" in captured["url"]
    assert "/my-app/" in captured["url"]


# ---- IMP-055 / BUG-467 / BUG-468：API 对照探测与报告聚合 -------------------


class _BookshelfApiHandler(http.server.BaseHTTPRequestHandler):
    """模拟 home-bookshelf：API 仅出现在 JS bundle，HTML 无 /api 字面量。

    - 入口 HTML 引用绝对 ``/assets/index-abc.js``（无 API 字面）
    - 绝对 ``/assets/...`` 空 200（无 spa_fallback）；带别名前缀才有真实 bundle
    - bundle 内含 ``"/api/v1/members"``
    - 默认兜底 ``/api/`` ``/api/v1/`` 带前缀端返回 404 JSON（非 2xx）→ 旧逻辑漏报
    - ``/api/v1/members``：根空 200，带前缀有 JSON → 应判 mismatch
    """

    HTML = (
        b"<!doctype html><html><head>"
        b'<script type="module" src="/assets/index-abc.js"></script>'
        b"</head><body>bookshelf</body></html>"
    )
    BUNDLE = (
        b'const u="/api/v1/members";'
        b'const b="/api/v1/books";'
        b"fetch(u);"
    )
    MEMBERS_JSON = b'[{"id":1,"name":"a"}]'
    NOT_FOUND_JSON = b'{"detail":"Not Found"}'

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in {"/", "/home-bookshelf", "/home-bookshelf/"}:
            body = self.HTML if "home-bookshelf" in path else b"root"
            self._send(200, body, "text/html")
        elif path == "/home-bookshelf/assets/index-abc.js":
            self._send(200, self.BUNDLE, "application/javascript")
        elif path == "/assets/index-abc.js":
            self._send(200, b"", "application/javascript")  # 空 200
        elif path == "/home-bookshelf/api/v1/members":
            self._send(200, self.MEMBERS_JSON, "application/json")
        elif path == "/api/v1/members":
            self._send(200, b"", "application/json")  # 根空 200
        elif path in {
            "/api/",
            "/api/v1/",
            "/api/v1",
            "/home-bookshelf/api/",
            "/home-bookshelf/api/v1/",
            "/home-bookshelf/api/v1",
        }:
            self._send(404, self.NOT_FOUND_JSON, "application/json")
        else:
            self._send(404, b"nf", "text/plain")

    def _send(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # noqa: ARG002
        pass


@pytest.fixture()
def bookshelf_api_server():
    """home-bookshelf 形态的真实 HTTP 服务（API 在 bundle 内）。"""
    port = _free_port()
    httpd = socketserver.TCPServer(("127.0.0.1", port), _BookshelfApiHandler)
    httpd.allow_reuse_address = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_extract_api_paths_preserves_concrete_endpoint() -> None:
    """BUG-467：具体端点路径不强制加尾斜杠（否则探成 /members/ → 404 漏报）。"""
    paths = _extract_api_paths('const API="/api/v1/members";')
    assert "/api/v1/members" in paths
    assert "/api/v1/members/" not in paths
    paths2 = _extract_api_paths('fetch("/api/v1/")')
    assert "/api/v1/" in paths2


def test_extract_js_bundle_paths_finds_script_srcs() -> None:
    html = (
        '<script src="./assets/index.js"></script>'
        '<script src="/vendor/runtime.js"></script>'
    )
    srcs = _extract_js_bundle_paths(html)
    assert "./assets/index.js" in srcs
    assert "/vendor/runtime.js" in srcs


def test_collect_api_paths_fetches_bundle_via_alias(bookshelf_api_server) -> None:
    """BUG-467：绝对 script 在入口根为空时，须经别名前缀拉取真实 bundle。"""
    port = bookshelf_api_server
    html = (
        "<!doctype html>"
        '<script type="module" src="/assets/index-abc.js"></script>'
    )
    # 不带别名：入口根空 bundle → 抽不到 API
    assert "/api/v1/members" not in _collect_api_paths(html, entry_port=port)
    # 带别名：从 /home-bookshelf/assets/... 取到真实 bundle
    paths = _collect_api_paths(
        html, entry_port=port, path_alias="home-bookshelf"
    )
    assert "/api/v1/members" in paths
    assert "/api/v1/books" in paths


def test_collect_api_paths_empty_html_returns_empty() -> None:
    assert _collect_api_paths(None, entry_port=8000, path_alias="x") == []
    assert _collect_api_paths("", entry_port=8000, path_alias="x") == []


def test_review_detects_api_mismatch_from_js_bundle(
    workspace, registry, config, bookshelf_api_server, monkeypatch
):
    """BUG-467 / WBS 055.10：API 仅在外链 bundle 时仍须检出 mismatch，不得 overall=ok。"""
    port = bookshelf_api_server
    config.staticGatewayPort = port
    _seed_static(
        workspace,
        registry,
        "home-bookshelf",
        host_port=port,
        lan_url=f"http://127.0.0.1:{port}",
        route_host="home-bookshelf",
        route_url=f"http://127.0.0.1:{port}/home-bookshelf/",
    )
    monkeypatch.setattr(
        "local_webpage_access.access.resolve_lan_ip", lambda cfg: "127.0.0.1"
    )

    report = review_access(workspace, config, registry)
    rep = report.instances[0]
    assert rep.has_api_mismatch is True
    mismatched = [f for f in rep.api_findings if f.mismatch]
    assert mismatched, "应从 JS bundle 抽出 /api/v1/members 并判 mismatch"
    assert any(f.path == "/api/v1/members" for f in mismatched)
    assert rep.status != "ok"
    assert any("IMP-055" in f and "API" in f for f in rep.findings)
    text = format_review_report(report)
    assert "API 错位" in text or "IMP-055" in text
    advice = format_rebuild_advice(report)
    assert "API 路径错位" in advice
    assert "home-bookshelf" in advice


def test_has_api_mismatch_ignores_non_mismatch_findings() -> None:
    """BUG-468：只要执行过 API 探测就会有 finding；聚合必须看 mismatch 标志。"""
    from local_webpage_access.access import (
        ApiPathFinding,
        InstanceAccessReport,
        UrlProbe,
    )

    rep = InstanceAccessReport(instance_id="x")
    rep.api_findings.append(
        ApiPathFinding(
            path="/api/",
            absolute=UrlProbe(url="a", status_code=404, ok=False, note="HTTP 404"),
            prefixed=UrlProbe(url="b", status_code=404, ok=False, note="HTTP 404"),
            mismatch=False,
        )
    )
    assert rep.has_api_mismatch is False
    rep.api_findings.append(
        ApiPathFinding(
            path="/api/v1/members",
            absolute=UrlProbe(
                url="c", status_code=200, ok=True, content_length=0
            ),
            prefixed=UrlProbe(
                url="d", status_code=200, ok=True, content_length=42
            ),
            mismatch=True,
        )
    )
    assert rep.has_api_mismatch is True


def test_format_rebuild_advice_api_only_mismatch() -> None:
    """BUG-468：无静态 rebuild 候选时，API-only mismatch 仍须输出建议段。"""
    from local_webpage_access.access import (
        AccessReviewReport,
        ApiPathFinding,
        InstanceAccessReport,
        UrlProbe,
    )

    rep = InstanceAccessReport(
        instance_id="api-only",
        status="warn",
        path_alias="demo",
    )
    rep.api_findings.append(
        ApiPathFinding(
            path="/api/v1/members",
            absolute=UrlProbe(
                url="a", status_code=200, ok=True, content_length=0
            ),
            prefixed=UrlProbe(
                url="b", status_code=200, ok=True, content_length=10
            ),
            mismatch=True,
        )
    )
    report = AccessReviewReport(
        lan_ip="10.0.0.1",
        backend="caddy",
        static_gateway_port=8080,
        instances=[rep],
    )
    text = format_rebuild_advice(report)
    assert text, "API-only mismatch 不得返回空建议"
    assert "API 路径错位" in text
    assert "api-only" in text
    assert "BASE_URL" in text


# ---- BUG-467 续：别名感知 script URL + JSON 404 高置信判定 ---------------


@pytest.mark.parametrize(
    "src,alias,backend,gateway",
    [
        ("/assets/app.js", "home-bookshelf", "/assets/app.js", "/home-bookshelf/assets/app.js"),
        ("./assets/app.js", "home-bookshelf", "/assets/app.js", "/home-bookshelf/assets/app.js"),
        (
            "/home-bookshelf/assets/app.js",
            "home-bookshelf",
            "/assets/app.js",
            "/home-bookshelf/assets/app.js",
        ),
    ],
)
def test_resolve_alias_aware_script_urls_matrix(src, alias, backend, gateway) -> None:
    """CHK-186：根绝对 / 相对 / 已带别名前缀三种 script src 解析矩阵。"""
    be, gw = access_mod._resolve_alias_aware_script_urls(src, path_alias=alias)
    assert be == backend
    assert gw == gateway


def test_resolve_alias_aware_script_urls_never_double_prefix() -> None:
    """防双前缀：已带别名的 src 不得再生成 /alias/alias/...。"""
    be, gw = access_mod._resolve_alias_aware_script_urls(
        "/home-bookshelf/assets/index.js", path_alias="home-bookshelf"
    )
    assert be == "/assets/index.js"
    assert gw == "/home-bookshelf/assets/index.js"
    assert "/home-bookshelf/home-bookshelf/" not in (gw or "")


class _PrefixedScriptBundleHandler(http.server.BaseHTTPRequestHandler):
    """script src 已带别名前缀；后端真实路径无前缀；错误双前缀返回 HTML SPA fallback。"""

    HTML = (
        b"<!doctype html><html><head>"
        b'<script type="module" src="/home-bookshelf/assets/index.js"></script>'
        b"</head><body>app</body></html>"
    )
    BUNDLE = b'const BASE="/api/v1";fetch(`${BASE}${"/members"}`);'
    SPA_HTML = b"<!doctype html><html><body>spa-fallback</body></html>"
    MEMBERS = b'[{"id":1}]'
    NOT_FOUND = b'{"detail":"Not Found"}'

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in {"/", "/home-bookshelf", "/home-bookshelf/"}:
            body = self.HTML if "home-bookshelf" in path else b"root"
            self._send(200, body, "text/html")
        elif path == "/assets/index.js":
            self._send(200, self.BUNDLE, "application/javascript")
        elif path == "/home-bookshelf/assets/index.js":
            self._send(200, self.BUNDLE, "application/javascript")
        elif path == "/home-bookshelf/home-bookshelf/assets/index.js":
            # 双前缀：SPA fallback 返回 HTML 200（掩盖探测失败）
            self._send(200, self.SPA_HTML, "text/html")
        elif path == "/api/v1":
            self._send(200, b"", "application/json")
        elif path == "/home-bookshelf/api/v1":
            self._send(404, self.NOT_FOUND, "application/json")
        elif path == "/home-bookshelf/api/v1/members":
            self._send(200, self.MEMBERS, "application/json")
        elif path == "/api/v1/members":
            self._send(200, b"", "application/json")
        elif path in {
            "/api/",
            "/api/v1/",
            "/home-bookshelf/api/",
            "/home-bookshelf/api/v1/",
        }:
            self._send(404, self.NOT_FOUND, "application/json")
        else:
            self._send(404, b"nf", "text/plain")

    def _send(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # noqa: ARG002
        pass


@pytest.fixture()
def prefixed_script_bundle_server():
    port = _free_port()
    httpd = socketserver.TCPServer(("127.0.0.1", port), _PrefixedScriptBundleHandler)
    httpd.allow_reuse_address = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_collect_api_paths_strips_alias_for_host_port(prefixed_script_bundle_server) -> None:
    """已带别名前缀的 script src：hostPort 须请求 /assets/...，不得双前缀。"""
    port = prefixed_script_bundle_server
    html = (
        "<!doctype html>"
        '<script type="module" src="/home-bookshelf/assets/index.js"></script>'
    )
    fetched: list[str] = []

    def tracking_fetch(url: str, **_kw):
        fetched.append(url)
        return access_mod._fetch_javascript(url)

    paths = _collect_api_paths(
        html,
        host_port=port,
        entry_port=port,
        path_alias="home-bookshelf",
        fetch_text=tracking_fetch,
    )
    assert "/api/v1" in paths
    assert f"http://127.0.0.1:{port}/assets/index.js" in fetched
    assert not any("/home-bookshelf/home-bookshelf/" in u for u in fetched)
    # 仅 hostPort 也能抽到（去前缀后命中真实 bundle）
    assert "/api/v1" in _collect_api_paths(
        html, host_port=port, path_alias="home-bookshelf"
    )


def test_collect_api_paths_ignores_html_spa_fallback_as_bundle() -> None:
    """Content-Type 为 text/html 的 SPA fallback 不得当 JS bundle 解析。"""
    html = '<script src="/assets/missing.js"></script>'

    # 模拟：错误路径返回 HTML 200（含误导性 /api 字面量）
    class _HtmlTrap(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b'<!doctype html><script>const x="/api/v1/trap";</script>'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # noqa: ARG002
            pass

    trap_port = _free_port()
    httpd = socketserver.TCPServer(("127.0.0.1", trap_port), _HtmlTrap)
    httpd.allow_reuse_address = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        paths = _collect_api_paths(
            html, host_port=trap_port, entry_port=trap_port, path_alias="x"
        )
        assert "/api/v1/trap" not in paths
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_review_detects_probable_api_mismatch_from_base_and_json_404(
    workspace, registry, config, prefixed_script_bundle_server, monkeypatch
):
    """旧版负向：bundle 仅有 BASE=/api/v1；根空 200 + 前缀 JSON 404 → 疑似错位。"""
    port = prefixed_script_bundle_server
    config.staticGatewayPort = port
    _seed_static(
        workspace,
        registry,
        "backend",
        host_port=port,
        lan_url=f"http://127.0.0.1:{port}",
        route_host="home-bookshelf",
        route_url=f"http://127.0.0.1:{port}/home-bookshelf/",
    )
    monkeypatch.setattr(
        "local_webpage_access.access.resolve_lan_ip", lambda cfg: "127.0.0.1"
    )

    report = review_access(workspace, config, registry)
    rep = report.instances[0]
    assert rep.has_api_mismatch is True
    mismatched = [f for f in rep.api_findings if f.mismatch]
    assert any(f.path == "/api/v1" for f in mismatched)
    assert rep.status == "warn"
    assert any("IMP-055" in f for f in rep.findings)


class _CompatiblePrefixedHandler(http.server.BaseHTTPRequestHandler):
    """新版正向：HTML / 静态 / API 均带别名前缀；无绝对 /api 字面量。"""

    HTML = (
        b"<!doctype html><html><head>"
        b'<script type="module" src="/home-bookshelf/assets/app.js"></script>'
        b"</head><body>ok</body></html>"
    )
    BUNDLE = b'const BASE="/home-bookshelf/api/v1";fetch(BASE+"/members");'
    MEMBERS = b'[{"id":1}]'
    NOT_FOUND = b'{"detail":"Not Found"}'

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in {"/", "/home-bookshelf", "/home-bookshelf/"}:
            body = self.HTML if "home-bookshelf" in path else b"root"
            self._send(200, body, "text/html")
        elif path in {"/assets/app.js", "/home-bookshelf/assets/app.js"}:
            self._send(200, self.BUNDLE, "application/javascript")
        elif path == "/home-bookshelf/api/v1/members":
            self._send(200, self.MEMBERS, "application/json")
        elif path in {
            "/api/",
            "/api/v1/",
            "/api/v1",
            "/home-bookshelf/api/",
            "/home-bookshelf/api/v1/",
            "/home-bookshelf/api/v1",
        }:
            self._send(404, self.NOT_FOUND, "application/json")
        else:
            self._send(404, b"nf", "text/plain")

    def _send(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # noqa: ARG002
        pass


@pytest.fixture()
def compatible_prefixed_server():
    port = _free_port()
    httpd = socketserver.TCPServer(("127.0.0.1", port), _CompatiblePrefixedHandler)
    httpd.allow_reuse_address = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_review_compatible_prefixed_app_overall_ok(
    workspace, registry, config, compatible_prefixed_server, monkeypatch
):
    """新版正向：资源与 API 均带别名前缀时 overall=ok，不得误报。"""
    port = compatible_prefixed_server
    config.staticGatewayPort = port
    _seed_static(
        workspace,
        registry,
        "backend",
        host_port=port,
        lan_url=f"http://127.0.0.1:{port}",
        route_host="home-bookshelf",
        route_url=f"http://127.0.0.1:{port}/home-bookshelf/",
    )
    monkeypatch.setattr(
        "local_webpage_access.access.resolve_lan_ip", lambda cfg: "127.0.0.1"
    )

    report = review_access(workspace, config, registry)
    rep = report.instances[0]
    assert rep.has_api_mismatch is False
    assert rep.status == "ok"
    assert report.overall == "ok"


class _NoApiHandler(http.server.BaseHTTPRequestHandler):
    """无 API 项目：默认 /api/ 探针 JSON 404，不得误报。"""

    HTML = b"<!doctype html><html><body><h1>static</h1></body></html>"
    NOT_FOUND = b'{"detail":"Not Found"}'

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in {"/", "/demo", "/demo/"}:
            body = self.HTML
            self._send(200, body, "text/html")
        elif path in {
            "/api/",
            "/api/v1/",
            "/demo/api/",
            "/demo/api/v1/",
        }:
            self._send(404, self.NOT_FOUND, "application/json")
        else:
            self._send(404, b"nf", "text/plain")

    def _send(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # noqa: ARG002
        pass


@pytest.fixture()
def no_api_server():
    port = _free_port()
    httpd = socketserver.TCPServer(("127.0.0.1", port), _NoApiHandler)
    httpd.allow_reuse_address = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_review_no_api_project_default_probes_no_false_positive(
    workspace, registry, config, no_api_server, monkeypatch
):
    """无 API 项目：默认 /api/、/api/v1/ 返回 JSON 404 仍不得告警。"""
    port = no_api_server
    config.staticGatewayPort = port
    _seed_static(
        workspace,
        registry,
        "static-only",
        host_port=port,
        lan_url=f"http://127.0.0.1:{port}",
        route_host="demo",
        route_url=f"http://127.0.0.1:{port}/demo/",
    )
    monkeypatch.setattr(
        "local_webpage_access.access.resolve_lan_ip", lambda cfg: "127.0.0.1"
    )

    report = review_access(workspace, config, registry)
    rep = report.instances[0]
    assert rep.has_api_mismatch is False
    assert rep.status == "ok"
