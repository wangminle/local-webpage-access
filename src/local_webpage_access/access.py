"""访问地址刷新与可用性复核（G1 / G2 / G5）。

本模块回应 `gateway-switch-access-review` 复盘的三类能力缺口：

* :func:`refresh_network_entries`（G1）—— 用当前 ``resolve_lan_ip`` 重算所有
  实例 ``network.lanUrl``/``routeUrl`` 并落盘。DHCP 换网 / 重启网关后，管理页
  链接不再指向失效的旧 LAN IP。
* :func:`review_access`（G2 / G5）—— 对每个实例的声明 URL 做**真探活**（入口
  HTML + 抽样绝对路径子资源 + 端口独占 + LAN IP 一致性），返回结构化报告。
  避免「入口返回 200 ≠ 页面可渲染」（IMP-023 空 200）与「CLI 报 FAIL ≠ 真失败」
  两类**状态报告缺口**。
* :func:`instances_needing_rebuild` / :func:`maybe_rebuild_after_review`（G6）——
  从复核报告收集 IMP-023 命中实例；默认只提示，``--rebuild-if-needed`` 时可选
  自动 ``rebuild_instance``。

探测口径遵循复盘文档 §9：入口 OK ∧ 无空 200 子资源 ∧ 无端口双开/孤儿监听 ∧
lanUrl host 未过期，才允许对外宣称「可访问」。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

from local_webpage_access.config import Config
from local_webpage_access.logging import get_logger
from local_webpage_access.paths import Workspace
from local_webpage_access.ports import resolve_lan_ip
from local_webpage_access.probe import mark_probe_url
from local_webpage_access.registry import Registry

log = get_logger("access")

# 探测 HTTP 的默认超时（短）：声明 URL 不应在数秒内无响应。
_PROBE_TIMEOUT = 3.0
# 单实例最多抽样的 SPA 绝对路径资源数量（避免对大型入口做过多请求）。
_MAX_SUBRESOURCES = 6

# 匹配 HTML 中 src=/href= 引用的绝对路径资源（以单个 / 开头，非 // 协议相对）。
_ABS_RESOURCE_RE = re.compile(
    r"""(?:src|href)\s*=\s*["'](/[^/"'][^"']*)["']""", re.IGNORECASE
)


# ---- 数据结构 ---------------------------------------------------------------


@dataclass
class UrlProbe:
    """单次 HTTP GET 探测结果。"""

    url: str
    status_code: int | None = None
    content_length: int | None = None
    ok: bool = False
    note: str | None = None  # EMPTY_BODY / TIMEOUT / REFUSED / PARSE 等

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "url": self.url,
            "statusCode": self.status_code,
            "contentLength": self.content_length,
            "ok": self.ok,
        }
        if self.note:
            d["note"] = self.note
        return d


@dataclass
class SubresourceFinding:
    """SPA 绝对路径子资源的探测对照（IMP-023 空 200 检测）。"""

    path: str
    absolute: UrlProbe  # http://127.0.0.1:entry/assets/x.js（无别名前缀）
    prefixed: UrlProbe  # http://127.0.0.1:entry/alias/assets/x.js（带前缀）
    empty_200: bool = False  # 绝对路径返回 200 但 0 字节，带前缀有实体

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "absolute": self.absolute.to_dict(),
            "prefixed": self.prefixed.to_dict(),
            "empty200": self.empty_200,
        }


@dataclass
class PortListener:
    """端口监听者（lsof best-effort 探测）。"""

    port: int
    pids: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    ok: bool = True  # 是否符合当前后端预期

    def to_dict(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "pids": self.pids,
            "names": self.names,
            "ok": self.ok,
        }


@dataclass
class InstanceAccessReport:
    """单实例访问可用性报告。"""

    instance_id: str
    runtime: str | None = None
    host_port: int | None = None
    path_alias: str | None = None
    lan_url: str | None = None
    route_url: str | None = None
    localhost_probe: UrlProbe | None = None
    lan_probe: UrlProbe | None = None
    route_probe: UrlProbe | None = None
    subresources: list[SubresourceFinding] = field(default_factory=list)
    lan_url_stale: bool = False
    port_listener: PortListener | None = None
    status: str = "ok"  # ok / warn / fail / skip
    findings: list[str] = field(default_factory=list)

    @property
    def needs_rebuild(self) -> bool:
        """G6：仅 IMP-023 空 200 触发「建议 / 可选自动 rebuild」。"""
        return any(s.empty_200 for s in self.subresources)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "instanceId": self.instance_id,
            "runtime": self.runtime,
            "hostPort": self.host_port,
            "pathAlias": self.path_alias,
            "lanUrl": self.lan_url,
            "routeUrl": self.route_url,
            "status": self.status,
            "findings": self.findings,
            "lanUrlStale": self.lan_url_stale,
            "needsRebuild": self.needs_rebuild,
        }
        if self.localhost_probe:
            d["localhostProbe"] = self.localhost_probe.to_dict()
        if self.lan_probe:
            d["lanProbe"] = self.lan_probe.to_dict()
        if self.route_probe:
            d["routeProbe"] = self.route_probe.to_dict()
        if self.subresources:
            d["subresources"] = [s.to_dict() for s in self.subresources]
        if self.port_listener:
            d["portListener"] = self.port_listener.to_dict()
        return d


@dataclass
class AccessReviewReport:
    """完整访问复核报告。"""

    lan_ip: str | None = None
    backend: str | None = None
    static_gateway_port: int | None = None
    instances: list[InstanceAccessReport] = field(default_factory=list)

    @property
    def overall(self) -> str:
        worst = "ok"
        order = {"ok": 0, "skip": 0, "warn": 1, "fail": 2}
        for rep in self.instances:
            if order.get(rep.status, 0) > order.get(worst, 0):
                worst = rep.status
        return worst

    @property
    def has_failures(self) -> bool:
        return any(r.status == "fail" for r in self.instances)

    @property
    def has_warnings(self) -> bool:
        return any(r.status == "warn" for r in self.instances)

    @property
    def needs_rebuild_ids(self) -> list[str]:
        """G6：报告中建议重建的实例 ID（稳定顺序）。"""
        return [r.instance_id for r in self.instances if r.needs_rebuild]

    def to_dict(self) -> dict[str, Any]:
        return {
            "lanIp": self.lan_ip,
            "backend": self.backend,
            "staticGatewayPort": self.static_gateway_port,
            "overall": self.overall,
            "needsRebuild": self.needs_rebuild_ids,
            "instances": [r.to_dict() for r in self.instances],
        }


@dataclass
class RebuildActionResult:
    """单实例可选自动 rebuild 的结果（G6）。

    ``ok``：rebuild 调用未抛异常。``still_imp023``：重建后复检仍检出空 200
    （常见原因：Vite ``base`` 仍为 ``/``，产物未变）——调用成功但问题未解决。
    """

    instance_id: str
    ok: bool
    error: str | None = None
    still_imp023: bool = False

    @property
    def resolved(self) -> bool:
        """重建成功且复检不再命中 IMP-023。"""
        return self.ok and not self.still_imp023

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "instanceId": self.instance_id,
            "ok": self.ok,
            "stillImp023": self.still_imp023,
            "resolved": self.resolved,
        }
        if self.error:
            d["error"] = self.error
        return d


@dataclass
class RebuildAfterReviewReport:
    """``--rebuild-if-needed`` 执行汇总（G6）。"""

    candidates: list[str] = field(default_factory=list)
    results: list[RebuildActionResult] = field(default_factory=list)
    skipped: bool = False  # True：未开开关，仅列出候选

    @property
    def all_ok(self) -> bool:
        """全部「真正解决」：rebuild 成功且复检不再命中 IMP-023。"""
        if self.skipped:
            return True
        return all(r.resolved for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": self.candidates,
            "skipped": self.skipped,
            "results": [r.to_dict() for r in self.results],
            "allOk": self.all_ok,
        }


@dataclass
class RefreshedInstance:
    """单实例地址刷新结果。"""

    instance_id: str
    old_host: str | None = None
    new_host: str | None = None
    lan_url: str | None = None
    route_url: str | None = None
    drifted: bool = False


@dataclass
class RefreshReport:
    """地址刷新汇总。"""

    lan_ip: str | None = None
    refreshed: list[RefreshedInstance] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def drifted_count(self) -> int:
        return sum(1 for r in self.refreshed if r.drifted)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lanIp": self.lan_ip,
            "driftedCount": self.drifted_count,
            "refreshed": [
                {
                    "instanceId": r.instance_id,
                    "oldHost": r.old_host,
                    "newHost": r.new_host,
                    "lanUrl": r.lan_url,
                    "routeUrl": r.route_url,
                    "drifted": r.drifted,
                }
                for r in self.refreshed
            ],
            "skipped": self.skipped,
        }


# ---- LAN URL 刷新（G1）-----------------------------------------------------


def _url_host(url: str | None) -> str | None:
    """从 URL 中提取 host 部分（用于漂移比对）。"""
    if not url:
        return None
    parsed = urlparse(url)
    return parsed.hostname


def _extract_host_port(manifest: Any) -> tuple[int | None, int | None]:
    """从 manifest 提取 (hostPort, internalPort)。"""
    host_port: int | None = None
    internal_port: int | None = None
    static = getattr(manifest, "static", None)
    container = getattr(manifest, "container", None)
    network = getattr(manifest, "network", None)

    if static is not None and getattr(static, "hostPort", None):
        host_port = static.hostPort
    elif container is not None and getattr(container, "hostPort", None):
        host_port = container.hostPort
        internal_port = getattr(container, "internalPort", None)
    elif network is not None and getattr(network, "hostPort", None):
        host_port = network.hostPort
        internal_port = getattr(network, "internalPort", None)
    return host_port, internal_port


def _extract_active_path_alias(manifest: Any) -> str | None:
    """提取**当前生效**的路径别名（须 ``routeMode=name``）。

    与 :func:`local_webpage_access.path_alias._current_alias` 对齐，供
    :func:`refresh_network_entries` 持久化使用（BUG-109）：不得把已切回
    ``routeMode=port`` 后磁盘残留的 ``routeHost`` 写回 ``routeMode=name``。
    """
    from local_webpage_access.models import RouteMode

    name = RouteMode.NAME.value
    static = getattr(manifest, "static", None)
    if (
        static is not None
        and getattr(static, "routeMode", None) == name
        and getattr(static, "routeHost", None)
    ):
        return static.routeHost
    container = getattr(manifest, "container", None)
    if (
        container is not None
        and getattr(container, "routeMode", None) == name
        and getattr(container, "routeHost", None)
    ):
        return container.routeHost
    network = getattr(manifest, "network", None)
    if (
        network is not None
        and getattr(network, "routeMode", None) == name
        and getattr(network, "routeHost", None)
    ):
        return network.routeHost
    return None


def _extract_path_alias_for_review(manifest: Any) -> str | None:
    """review 用别名：忽略 ``routeMode``，从任意段读 ``routeHost``。

    复盘 §10.3-I2：容器已 ``routeMode=port`` 但磁盘仍有别名片段 / 残留
    ``static.routeHost`` 时，仍须能检出 SPA 空 200。**仅**供
    :func:`review_access`，不得用于 refresh 持久化（见 BUG-109）。
    """
    network = getattr(manifest, "network", None)
    static = getattr(manifest, "static", None)
    container = getattr(manifest, "container", None)
    for section in (network, static, container):
        if section is None:
            continue
        rh = getattr(section, "routeHost", None)
        if rh:
            return rh
    return None


def _extract_host_port_alias(
    manifest: Any, *, for_review: bool = False
) -> tuple[int | None, str | None, int | None]:
    """从 manifest 提取 (hostPort, pathAlias, internalPort)。

    * ``for_review=False``（默认，refresh）：别名须 ``routeMode=name``（BUG-109）。
    * ``for_review=True``：忽略 routeMode，便于 IMP-023 漏检兜底（I2）。
    """
    host_port, internal_port = _extract_host_port(manifest)
    if for_review:
        path_alias = _extract_path_alias_for_review(manifest)
    else:
        path_alias = _extract_active_path_alias(manifest)
    return host_port, path_alias, internal_port


def refresh_network_entries(
    workspace: Workspace, config: Config, registry: Registry
) -> RefreshReport:
    """用当前 LAN IP 重算所有实例的 ``network.lanUrl``/``routeUrl``（G1）。

    对每个有 ``hostPort`` 的实例：以当前 :func:`resolve_lan_ip` 重建
    :func:`build_network_entry`（保留 hostPort/internalPort/**生效中的** pathAlias），
    更新 ``manifest.network`` 并保存 ``local-web.json``。管理页 API 列表直接读
    manifest，故保存后链接立即反映新地址。返回漂移摘要。

    BUG-109：别名仅在 ``routeMode=name`` 时传入 ``build_network_entry``，避免把
    端口模式下残留的 ``routeHost`` 误写回 ``routeMode=name`` / ``routeUrl``。
    """
    from local_webpage_access.models import InstanceManifest, NetworkConfig
    from local_webpage_access.ports import build_network_entry

    lan_ip = resolve_lan_ip(config)
    report = RefreshReport(lan_ip=lan_ip)
    rows = registry.list_instances()
    for row in rows:
        iid = row["id"]
        manifest_path = workspace.app_manifest_path(iid)
        if not manifest_path.is_file():
            report.skipped.append(iid)
            continue
        try:
            manifest = InstanceManifest.load(manifest_path)
        except Exception as exc:  # noqa: BLE001 — 单个 manifest 损坏不阻断整体刷新
            log.warning("刷新地址：实例 %s manifest 读取失败，跳过：%s", iid, exc)
            report.skipped.append(iid)
            continue
        host_port, path_alias, internal_port = _extract_host_port_alias(
            manifest, for_review=False
        )
        if host_port is None:
            report.skipped.append(iid)
            continue
        old_lan = manifest.network.lanUrl if manifest.network else None
        old_host = _url_host(old_lan)
        entry = build_network_entry(
            config,
            host_port,
            internal_port=internal_port,
            path_alias=path_alias,
            lan_ip=lan_ip,
        )
        manifest.network = NetworkConfig(**entry)
        manifest.touch()
        try:
            manifest.save(manifest_path)
        except OSError as exc:
            log.warning("刷新地址：实例 %s manifest 写入失败：%s", iid, exc)
            report.skipped.append(iid)
            continue
        new_lan = entry["lanUrl"]
        new_host = _url_host(new_lan)
        report.refreshed.append(
            RefreshedInstance(
                instance_id=iid,
                old_host=old_host,
                new_host=new_host,
                lan_url=new_lan,
                route_url=entry.get("routeUrl"),
                drifted=(old_host != new_host),
            )
        )
        if old_host != new_host:
            registry.add_event(
                iid,
                "access",
                f"刷新访问地址：lanUrl {old_lan or '(空)'} → {new_lan or '(无 LAN IP)'}",
            )
            log.info("实例 %s 地址已刷新：%s → %s", iid, old_lan, new_lan)
    log.info(
        "地址刷新完成：当前 LAN IP=%s，刷新 %d 个实例（其中 %d 个地址漂移）",
        lan_ip or "(无)", len(report.refreshed), report.drifted_count,
    )
    return report


# ---- 访问可用性复核（G2 / G5）----------------------------------------------


def _http_get(url: str, *, timeout: float = _PROBE_TIMEOUT) -> UrlProbe:
    """对 ``url`` 做 GET，返回 :class:`UrlProbe`（不抛异常）。"""
    probe = UrlProbe(url=url)
    # 实际请求带 __lwa_probe=1（不计入浏览量），UrlProbe 仍展示干净 URL
    req = urllib.request.Request(
        mark_probe_url(url), headers={"User-Agent": "lwa-access-review"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            probe.status_code = resp.status
            probe.content_length = _content_length(resp.headers) or len(body)
            probe.ok = 200 <= resp.status < 300
            return probe
    except urllib.error.HTTPError as exc:
        # 4xx/5xx 仍拿到响应头
        probe.status_code = exc.code
        probe.content_length = _content_length(exc.headers)
        probe.note = f"HTTP {exc.code}"
        probe.ok = 200 <= exc.code < 300
        return probe
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        probe.note = "REFUSED" if "refused" in str(reason).lower() else "UNREACHABLE"
        return probe
    except (TimeoutError, OSError) as exc:
        probe.note = "TIMEOUT" if "timed out" in str(exc).lower() else "UNREACHABLE"
        return probe
    except Exception:  # noqa: BLE001
        probe.note = "ERROR"
        return probe


def _content_length(headers: Any) -> int | None:
    """从响应头读 Content-Length（容忍缺失/非法）。"""
    if headers is None:
        return None
    try:
        val = headers.get("Content-Length")
    except Exception:  # noqa: BLE001
        return None
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _extract_absolute_resources(html: str, *, limit: int = _MAX_SUBRESOURCES) -> list[str]:
    """从入口 HTML 抽样绝对路径资源（以 ``/`` 开头、非协议相对 ``//``）。"""
    seen: set[str] = set()
    out: list[str] = []
    for match in _ABS_RESOURCE_RE.finditer(html):
        path = match.group(1)
        if path in seen:
            continue
        seen.add(path)
        out.append(path)
        if len(out) >= limit:
            break
    return out


def _detect_backend(gateway) -> str:
    """静默探测静态后端（不触发 detect_backend 的 warning 日志污染）。"""
    import shutil

    if gateway.config.staticGateway != "caddy":
        return "builtin"
    return "caddy" if shutil.which("caddy") else "builtin"


def _probe_listeners(ports: list[int]) -> dict[int, PortListener]:
    """best-effort：用 lsof 探测各端口监听进程（POSIX；Windows 返回空 pids）。

    返回 ``{port: PortListener}``；lsof 不可用时返回空 pids 的占位项（``ok=True``）。
    """
    result: dict[int, PortListener] = {p: PortListener(port=p) for p in ports}
    if shutil.which("lsof") is None:
        return result
    for port in ports:
        listener = result[port]
        try:
            proc = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            continue
        for line in proc.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 2:
                continue
            listener.pids.append(parts[1])
            listener.names.append(parts[0])
    return result


def review_access(
    workspace: Workspace, config: Config, registry: Registry
) -> AccessReviewReport:
    """对每个实例的声明 URL 做真探活，返回结构化报告（G2 / G5）。

    逐实例探测（遵循复盘 §4.5 / §9）：

    1. **回环对照**：``http://127.0.0.1:<hostPort>/`` 必须通——区分「服务死了」
       与「仅 LAN URL 陈旧」。
    2. **LAN 探活**：声明 ``lanUrl`` 发 GET；其 host 与当前 :func:`resolve_lan_ip`
       不一致则标 ``lan_url_stale``（G1 漂移）。
    3. **别名入口**：声明 ``routeUrl`` 发 GET；解析入口 HTML 的绝对路径 ``src``/``href``，
       对照 ``127.0.0.1:entry + 绝对路径``（无别名前缀）与 ``routeUrl + 路径``（带前缀）。
       前者 200 但 0 字节、后者有实体 → **IMP-023 空 200 风险**（WARN）。
    4. **端口监听**：best-effort lsof 列出 hostPort 监听者，供人工核对 builtin+caddy 双开。

    回环不通即 FAIL；LAN/别名问题为 WARN。
    """
    from local_webpage_access.static_gateway import StaticGateway

    lan_ip = resolve_lan_ip(config)
    gateway = StaticGateway(workspace, config)
    backend = _detect_backend(gateway)
    report = AccessReviewReport(
        lan_ip=lan_ip,
        backend=backend,
        static_gateway_port=config.staticGatewayPort,
    )
    rows = registry.list_instances()
    for row in rows:
        report.instances.append(
            _review_instance(workspace, config, registry, row, lan_ip, gateway, backend)
        )
    return report


def _review_instance(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    row: dict[str, Any],
    lan_ip: str | None,
    gateway,
    backend: str,
) -> InstanceAccessReport:
    """复核单个实例。"""
    from local_webpage_access.models import InstanceManifest

    iid = row["id"]
    rep = InstanceAccessReport(
        instance_id=iid,
        runtime=row.get("runtime"),
    )
    manifest_path = workspace.app_manifest_path(iid)
    if not manifest_path.is_file():
        rep.status = "skip"
        rep.findings.append("无 manifest，跳过访问复核")
        return rep
    try:
        manifest = InstanceManifest.load(manifest_path)
    except Exception as exc:  # noqa: BLE001
        rep.status = "skip"
        rep.findings.append(f"manifest 读取失败：{exc}")
        return rep
    host_port, path_alias, _internal = _extract_host_port_alias(
        manifest, for_review=True
    )
    rep.host_port = host_port
    rep.path_alias = path_alias
    if manifest.network:
        rep.lan_url = manifest.network.lanUrl
        rep.route_url = manifest.network.routeUrl

    if host_port is None:
        rep.status = "skip"
        rep.findings.append("无 hostPort，跳过访问复核")
        return rep

    # 1. 回环对照（权威信号）：127.0.0.1:hostPort 必须通。
    localhost_probe = _http_get(f"http://127.0.0.1:{host_port}/")
    rep.localhost_probe = localhost_probe
    if not localhost_probe.ok:
        rep.findings.append(
            f"回环 127.0.0.1:{host_port}/ 不可达（{localhost_probe.note or '非 2xx'}）"
            "——服务未运行或网关未就绪"
        )
        rep.status = "fail"
        _fill_port_listener(rep, host_port)
        return rep

    # 2. LAN URL 探活 + 漂移检测。
    if rep.lan_url:
        lan_host = _url_host(rep.lan_url)
        if lan_ip and lan_host and lan_host not in (lan_ip, "127.0.0.1"):
            rep.lan_url_stale = True
            rep.findings.append(
                f"lanUrl host={lan_host} 与当前 LAN IP={lan_ip} 不一致（地址漂移，"
                "运行 `lwa access refresh` 刷新）"
            )
        rep.lan_probe = _http_get(rep.lan_url)
        if not rep.lan_probe.ok and not rep.lan_url_stale:
            rep.findings.append(
                f"lanUrl {rep.lan_url} 探活失败（{rep.lan_probe.note or '非 2xx'}）"
            )

    # 3. 别名入口 + SPA 子资源空 200 检测（IMP-023）。
    # I2：routeUrl 为空但仍有 path_alias（或磁盘别名元数据）时，用回环合成入口探测。
    entry_port = config.staticGatewayPort
    route_target = rep.route_url
    if not route_target and path_alias and entry_port is not None:
        route_target = f"http://127.0.0.1:{entry_port}/{path_alias}/"
    if route_target and path_alias:
        route_probe = _http_get(route_target)
        rep.route_probe = route_probe
        if not rep.route_url:
            # 合成探测：便于报告展示实际检查的 URL
            rep.route_url = route_target
            rep.findings.append(
                f"network.routeUrl 为空，已用别名元数据合成探测 {route_target}"
            )
        if not route_probe.ok:
            rep.findings.append(
                f"routeUrl {route_target} 探活失败（{route_probe.note or '非 2xx'}）"
            )
        elif route_probe.content_length and route_probe.content_length > 0:
            _check_subresources(rep, config, path_alias, route_probe)

    _fill_port_listener(rep, host_port)

    if rep.status != "fail":
        rep.status = "warn" if rep.findings else "ok"
    return rep


def _fill_port_listener(rep: InstanceAccessReport, host_port: int) -> None:
    """best-effort 填充 hostPort 监听者信息。"""
    listeners = _probe_listeners([host_port])
    listener = listeners.get(host_port)
    if listener and (listener.pids or listener.names):
        rep.port_listener = listener
        names = sorted(set(listener.names))
        if len(names) > 1:
            rep.findings.append(
                f"hostPort {host_port} 有多个监听进程（{', '.join(names)}）"
                "——疑似 builtin + caddy 双开，运行 `lwa gateway off` 再 `lwa gateway on`"
            )
            if rep.status == "ok":
                rep.status = "warn"


def _check_subresources(
    rep: InstanceAccessReport,
    config: Config,
    path_alias: str,
    route_probe: UrlProbe,
) -> None:
    """解析别名入口 HTML，对照绝对路径 vs 带前缀子资源（IMP-023 空 200）。"""
    entry_port = config.staticGatewayPort
    if entry_port is None:
        return
    # 重新拉一份 HTML 文本用于解析（route_probe 只存了 length）。
    html = _fetch_text(f"http://127.0.0.1:{entry_port}/{path_alias}/")
    if not html:
        return
    resources = _extract_absolute_resources(html)
    for path in resources:
        absolute = _http_get(f"http://127.0.0.1:{entry_port}{path}")
        prefixed = _http_get(f"http://127.0.0.1:{entry_port}/{path_alias}{path}")
        empty_200 = (
            absolute.ok
            and (absolute.content_length == 0)
            and prefixed.ok
            and (prefixed.content_length or 0) > 0
        )
        finding = SubresourceFinding(
            path=path, absolute=absolute, prefixed=prefixed, empty_200=empty_200
        )
        rep.subresources.append(finding)
        if empty_200:
            rep.findings.append(
                f"IMP-023 风险：绝对路径 {path} 在别名入口根返回 200 但 0 字节，"
                f"带前缀 /{path_alias}{path} 有实体（{prefixed.content_length} 字节）"
                "——SPA 需构建时设相对 base（Vite: base: './'）"
            )
            if rep.status == "ok":
                rep.status = "warn"


def _fetch_text(url: str, *, timeout: float = _PROBE_TIMEOUT) -> str | None:
    """GET url 返回响应正文文本；失败返回 None。

    BUG-179：带 ``__lwa_probe=1`` 探针标记，避免 access review / gateway on /
    rebuild 复检拉取别名入口 HTML 时被 pageviews 计为真实浏览（与 _http_get 一致）。
    """
    req = urllib.request.Request(
        mark_probe_url(url), headers={"User-Agent": "lwa-access-review"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if not (200 <= resp.status < 300):
                return None
            return resp.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None


def instances_needing_rebuild(report: AccessReviewReport) -> list[str]:
    """G6：从复核报告收集建议 rebuild 的实例 ID（仅 IMP-023 空 200）。"""
    return list(report.needs_rebuild_ids)


def _alias_for_instance(
    report: AccessReviewReport, instance_id: str
) -> str | None:
    """从复核报告取实例路径别名（用于 rebuild 后复检）。"""
    for rep in report.instances:
        if rep.instance_id == instance_id:
            if rep.path_alias:
                return rep.path_alias
            if rep.route_url:
                path = urlparse(rep.route_url).path.strip("/")
                if path:
                    return path.split("/")[0]
            return None
    return None


def instance_still_has_imp023(
    config: Config,
    *,
    path_alias: str,
) -> bool:
    """rebuild 后简要复检：别名入口 HTML 是否仍存在绝对路径空 200。

    与 :func:`_check_subresources` 同口径；拉不到 HTML / 无绝对路径资源 → False
    （无法证明仍坏，不当作 still_imp023）。
    """
    entry_port = config.staticGatewayPort
    if entry_port is None or not path_alias:
        return False
    html = _fetch_text(f"http://127.0.0.1:{entry_port}/{path_alias}/")
    if not html:
        return False
    resources = _extract_absolute_resources(html)
    if not resources:
        return False
    for path in resources:
        absolute = _http_get(f"http://127.0.0.1:{entry_port}{path}")
        prefixed = _http_get(f"http://127.0.0.1:{entry_port}/{path_alias}{path}")
        if (
            absolute.ok
            and (absolute.content_length == 0)
            and prefixed.ok
            and (prefixed.content_length or 0) > 0
        ):
            return True
    return False


def maybe_rebuild_after_review(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    report: AccessReviewReport,
    *,
    rebuild_if_needed: bool = False,
    rebuild_fn: Callable[..., Any] | None = None,
) -> RebuildAfterReviewReport:
    """G6：按复核结果决定是否自动 rebuild，并在成功后复检 IMP-023。

    * ``rebuild_if_needed=False``（默认）：只返回候选列表，``skipped=True``，不执行重建。
    * ``rebuild_if_needed=True``：对候选依次调用 ``rebuild_instance``（或注入的
      ``rebuild_fn``）；调用成功后再对该实例别名入口做空 200 复检——仍命中则
      ``still_imp023=True``（避免「rebuild 成功但产物未变」假绿）。
    """
    candidates = instances_needing_rebuild(report)
    out = RebuildAfterReviewReport(candidates=candidates)
    if not rebuild_if_needed:
        out.skipped = True
        return out
    if not candidates:
        return out
    if rebuild_fn is None:
        from local_webpage_access.lifecycle import rebuild_instance

        rebuild_fn = rebuild_instance
    for iid in candidates:
        try:
            rebuild_fn(workspace, config, registry, iid)
        except Exception as exc:  # noqa: BLE001 — 单实例失败不阻断其余
            out.results.append(
                RebuildActionResult(instance_id=iid, ok=False, error=str(exc))
            )
            log.warning("G6：自动 rebuild %s 失败：%s", iid, exc)
            continue
        alias = _alias_for_instance(report, iid)
        still = False
        if alias:
            try:
                still = instance_still_has_imp023(config, path_alias=alias)
            except Exception as exc:  # noqa: BLE001 — 复检失败不掩盖 rebuild 成功
                log.warning("G6：rebuild 后复检 %s 失败（不阻断）：%s", iid, exc)
                still = False
        out.results.append(
            RebuildActionResult(instance_id=iid, ok=True, still_imp023=still)
        )
        if still:
            log.warning(
                "G6：已 rebuild %s 但 IMP-023 仍命中（需固化 Vite base: './'）",
                iid,
            )
        else:
            log.info("G6：因 IMP-023 已自动 rebuild %s，复检通过", iid)
    return out


def format_rebuild_advice(
    report: AccessReviewReport,
    *,
    rebuild_report: RebuildAfterReviewReport | None = None,
) -> str:
    """渲染 G6「建议重建 / 自动重建结果」段。"""
    lines: list[str] = []
    candidates = instances_needing_rebuild(report)
    if not candidates and not (rebuild_report and rebuild_report.results):
        return ""
    lines.append("── 构建兼容（G6 / IMP-023）──")
    if rebuild_report is not None and not rebuild_report.skipped:
        if not rebuild_report.candidates:
            lines.append("  无需 rebuild（未检出 IMP-023 空 200）")
        for r in rebuild_report.results:
            if not r.ok:
                lines.append(
                    f"  [FAIL] 自动 rebuild {r.instance_id} 失败："
                    f"{r.error or '未知错误'}"
                )
            elif r.still_imp023:
                lines.append(
                    f"  [WARN] rebuild {r.instance_id} 完成，但 IMP-023 仍命中"
                    "——需固化 Vite base: './'（或等价）后再次 rebuild；"
                    "仅重跑构建不会改绝对路径产物"
                )
            else:
                lines.append(
                    f"  [OK  ] 已自动 rebuild {r.instance_id}，复检通过"
                )
        return "\n".join(lines)
    lines.append(
        f"  建议 rebuild {len(candidates)} 个实例（别名下 SPA 绝对路径空 200）："
    )
    for iid in candidates:
        lines.append(f"    · {iid}  →  lwa rebuild {iid}")
    lines.append(
        "  仅检查不重建；需要自动重建时加 --rebuild-if-needed"
        "（请先固化 Vite base: './' 等构建配置，否则 rebuild 后可能仍空 200）"
    )
    return "\n".join(lines)


def format_review_report(
    report: AccessReviewReport,
    *,
    rebuild_report: RebuildAfterReviewReport | None = None,
) -> str:
    """把访问复核报告渲染为人类可读文本。"""
    lines: list[str] = []
    lines.append("── 访问地址可用性复核 ──")
    lines.append(
        f"  当前 LAN IP：{report.lan_ip or '(无)'}"
        f"  后端：{report.backend}  别名入口端口：{report.static_gateway_port or '—'}"
    )
    if not report.instances:
        lines.append("  （无实例）")
    for rep in report.instances:
        tag = rep.status.upper()
        rebuild_mark = " ⚠需rebuild" if rep.needs_rebuild else ""
        lines.append(
            f"  [{tag:4}] {rep.instance_id}（runtime={rep.runtime or '—'}）"
            f"{rebuild_mark}"
        )
        if rep.host_port:
            lp = rep.localhost_probe
            lines.append(
                f"           回环 :{rep.host_port} → "
                f"{_probe_brief(lp) if lp else '—'}"
            )
        if rep.lan_url:
            stale = " ⚠ 地址漂移" if rep.lan_url_stale else ""
            lines.append(
                f"           lanUrl {rep.lan_url}{stale} → "
                f"{_probe_brief(rep.lan_probe) if rep.lan_probe else '未探'}"
            )
        if rep.route_url:
            lines.append(
                f"           routeUrl {rep.route_url} → "
                f"{_probe_brief(rep.route_probe) if rep.route_probe else '未探'}"
            )
        for sub in rep.subresources:
            if sub.empty_200:
                lines.append(
                    f"           ⚠ {sub.path}：绝对路径空 200，"
                    f"带前缀 {sub.prefixed.content_length} 字节（IMP-023）"
                )
        if rep.port_listener and rep.port_listener.names:
            lines.append(
                f"           监听进程：{', '.join(sorted(set(rep.port_listener.names)))}"
            )
        for f in rep.findings:
            lines.append(f"           · {f}")
    lines.append("")
    n_fail = sum(1 for r in report.instances if r.status == "fail")
    n_warn = sum(1 for r in report.instances if r.status == "warn")
    lines.append(f"总体：{report.overall.upper()}（{n_fail} 失败，{n_warn} 警告）")
    advice = format_rebuild_advice(report, rebuild_report=rebuild_report)
    if advice:
        lines.append("")
        lines.append(advice)
    return "\n".join(lines)


def _probe_brief(probe: UrlProbe | None) -> str:
    if probe is None:
        return "—"
    if probe.status_code is not None:
        size = probe.content_length if probe.content_length is not None else "?"
        return f"{probe.status_code}（{size}B）"
    return probe.note or "失败"


__all__ = [
    "UrlProbe",
    "SubresourceFinding",
    "PortListener",
    "InstanceAccessReport",
    "AccessReviewReport",
    "RebuildActionResult",
    "RebuildAfterReviewReport",
    "RefreshedInstance",
    "RefreshReport",
    "refresh_network_entries",
    "review_access",
    "instances_needing_rebuild",
    "instance_still_has_imp023",
    "maybe_rebuild_after_review",
    "format_rebuild_advice",
    "format_review_report",
]
