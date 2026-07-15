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
    format_review_report,
    instance_still_has_imp023,
    instances_needing_rebuild,
    maybe_rebuild_after_review,
    refresh_network_entries,
    review_access,
)


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
    assert any("IMP-023" in f for f in rep.findings)


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

