"""IMP-006：实例路径别名在线设置与清除（管理页 API / CLI 共用）。"""

from __future__ import annotations

import contextlib
import os
import threading
import time
import ssl
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

from local_webpage_access.config import Config
from local_webpage_access.errors import GatewayError, LifecycleError, RecognitionError
from local_webpage_access.file_lock import (
    ensure_lockable,
    release_exclusive,
    try_acquire_exclusive,
    write_lock_payload,
)
from local_webpage_access.logging import get_logger, now_iso
from local_webpage_access.models import (
    InstanceManifest,
    NetworkConfig,
    RouteMode,
    Runtime,
    StaticConfig,
)
from local_webpage_access.paths import Workspace, validate_path_alias
from local_webpage_access.ports import build_network_entry
from local_webpage_access.registry import Registry
from local_webpage_access.static_gateway import StaticGateway

log = get_logger("path_alias")

# BUG-167：工作区级别名锁，串行化「查唯一性 → 写 manifest/子表/Caddy」全流程。
_ALIAS_LOCK_TIMEOUT = 30.0
_alias_thread_lock = threading.RLock()


@dataclass(frozen=True)
class PathAliasResult:
    instance_id: str
    alias: str | None
    route_url: str | None
    alias_entry_enabled: bool
    gateway_reloaded: bool
    unchanged: bool
    html_verified: bool = False
    html_warnings: tuple[str, ...] = ()
    live_verified: bool = False
    compat_check_skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "instanceId": self.instance_id,
            "alias": self.alias,
            "routeUrl": self.route_url,
            "aliasEntryEnabled": self.alias_entry_enabled,
            "gatewayReloaded": self.gateway_reloaded,
            "unchanged": self.unchanged,
            "htmlVerified": self.html_verified,
            "htmlWarnings": list(self.html_warnings),
            "liveVerified": self.live_verified,
            "compatCheckSkipped": self.compat_check_skipped,
        }


def _current_alias(manifest: InstanceManifest) -> str | None:
    """读取当前别名（静态站点或容器实例，IMP-014 放开容器别名后两者共用）。"""
    static = manifest.static
    if static is not None and static.routeMode == RouteMode.NAME.value and static.routeHost:
        return static.routeHost
    container = manifest.container
    if (
        container is not None
        and container.routeMode == RouteMode.NAME.value
        and container.routeHost
    ):
        return container.routeHost
    return None


def reject_alias_if_absolute_spa_assets(
    *,
    html: str | None,
    alias: str,
    instance_id: str,
) -> tuple[str, ...]:
    """入口 HTML 含**加载型**绝对路径资源时拒绝设别名（IMP-023 / IMP-055 硬拦截）。

    路径别名 ``handle_path`` 会去前缀；浏览器仍按 ``/assets/...`` 打到统一入口根，
    常见结果是空 200 / 白屏。能证明会挂时明确失败，避免「设置成功但打不开」。
    ``html`` 为空（探不到入口）时不拦截--无法证明不可用。

    issue #10 修复口径：

    * 结构化解析 HTML 标签后按语义分类--``script src`` / stylesheet /
      modulepreload 等加载型资源硬拦截；导航链接、canonical、favicon 等
      提示型引用仅警告（本函数返回警告路径列表，不拦截）；
    * 按**路径段边界**豁免 ``/{alias}`` 与 ``/{alias}/...`` 前缀资源：
      按 ``--base=/{alias}/`` 正确构建的产物可以通过守卫（此前守卫收到
      ``alias`` 却不参与判断，推荐的修复方案过不了守卫本身）；
      ``/{alias}-other/...`` 前缀相同但路径段不同，不豁免；
    * 先完整分类、过滤，再截断展示样本（此前抽样上限 6 先截断后过滤，
      前 6 条正确、第 7 条错误时漏报）。

    .. note:: IMP-055 撤销 docker-compose 豁免

        此前 BUG-465 曾为 docker-compose 追加全局 ``/assets`` 回退路由并跳过本守卫。
        IMP-055 收敛该回退（多实例争抢 ``/assets`` 且管不住 ``/api`` 与 Router），
        恢复对所有 runtime 的硬拦截。应用侧须按显式 base path 方案改造（方案 B）。

    Returns:
        提示型警告路径列表（导航 / canonical / favicon 等绝对路径引用）；
        空列表表示无警告或未拦截（``html`` 为空）。
    """
    if not html:
        return ()
    from local_webpage_access.access import scan_absolute_spa_resources

    scan = scan_absolute_spa_resources(html, alias=alias)
    if scan.warn_paths:
        sample = ", ".join(scan.warn_paths[:3])
        more = "…" if len(scan.warn_paths) > 3 else ""
        log.warning(
            "入口 HTML 含提示型绝对路径引用（别名 /%s/ 下可能 404，不拦截）："
            "%s%s（实例 %s）",
            alias,
            sample,
            more,
            instance_id,
        )
    if not scan.load_paths:
        return scan.warn_paths
    sample = ", ".join(scan.load_paths[:3])
    more = "…" if len(scan.load_paths) > 3 else ""
    raise RecognitionError(
        f"入口 HTML 含未带别名前缀的加载型绝对路径资源（{sample}{more}），"
        f"设置路径别名 /{alias}/ 后浏览器会绕过别名加载这些资源，页面会白屏"
        f"（IMP-023 / IMP-055 / issue #10）。\n"
        f"解决方法（方案 B - 显式、可配置的 base path，选一）：\n"
        f"  1. 构建时设 --base=/{alias}/（Vite: vite build --base=/{alias}/），"
        f"产物资源路径形如 /{alias}/assets/…，可正常通过本守卫；"
        f"同步重建静态产物后重新设置别名；\n"
        f"  2. Vue Router 用 createWebHistory(import.meta.env.BASE_URL)，"
        f"前端 API 客户端从 BASE_URL 派生请求路径（如 /{alias}/api/v1）；\n"
        f"  3. 若无源码或无法重建（C 类），路径别名模型下无解，"
        f"请继续用 hostPort 端口直达。\n"
        f"注意：base: './' 可消除绝对资源路径但不推荐作为最终方案"
        f"（Router/API 仍需跟 BASE_URL）。",
        instance_id=instance_id,
    )


def _fetch_entrypoint_html_for_alias_guard(
    *,
    workspace: Workspace,
    manifest: InstanceManifest,
    host_port: int | None,
) -> str | None:
    """best-effort 取入口 HTML，供设别名前的 IMP-023 守卫。

    优先 GET ``http://127.0.0.1:{hostPort}/``；静态站再尝试磁盘 ``index.html``。
    失败返回 ``None``（调用方不拦截）。
    """
    from local_webpage_access.access import _fetch_text

    if host_port is not None:
        html = _fetch_text(f"http://127.0.0.1:{host_port}/")
        if html:
            return html

    if manifest.runtime == Runtime.SHARED_STATIC:
        root = workspace.app_current(manifest.id)
        for candidate in (root / "index.html", root / "public" / "index.html"):
            if not candidate.is_file():
                continue
            try:
                return candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
    return None


def _gateway_port(config: Config) -> int | None:
    return config.staticGatewayPort


def _looks_like_html_body(body: bytes) -> bool:
    head = body[:256].lstrip().lower()
    return head.startswith(b"<!doctype") or head.startswith(b"<html")


def _collect_alias_live_probe_paths(html: str, alias: str, *, limit: int = 6) -> list[str]:
    """收集别名入口下应可达的资源路径（/{alias}/...）。"""
    from local_webpage_access.access import (
        _alias_exempt,
        _extract_js_bundle_paths,
        _normalize_script_src,
        scan_absolute_spa_resources,
    )

    paths: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        bare = path.split("?", 1)[0].split("#", 1)[0]
        if not bare or bare in seen:
            return
        seen.add(bare)
        paths.append(bare)

    scan = scan_absolute_spa_resources(html, alias=None)
    for p in scan.load_paths:
        if _alias_exempt(p, alias):
            add(p)

    for src in _extract_js_bundle_paths(html, limit=limit):
        # BUG-590：规范化前先记录是否相对路径——_normalize_script_src 恒返回
        # "/" 前缀，规范化后再判 startswith('/') 的 else 分支永不可达，
        # 相对资源会被当成绝对路径跳过、不参与别名活验证。
        is_relative = not src.strip().startswith("/")
        norm = _normalize_script_src(src)
        if is_relative:
            add(f"/{alias}/{norm.lstrip('/')}")
        elif _alias_exempt(norm, alias):
            add(norm)

    return paths[:limit]


def _http_probe_alias_resource(
    url: str, *, timeout: float = 3.0, ssl_context: ssl.SSLContext | None = None
) -> tuple[bool, int | None, str | None, bytes]:
    """GET 别名下资源；返回 (ok, status, content_type, body_prefix)。

    W08：https 入口提供验证上下文（错误证书按失败呈现，不降级）。
    """
    from local_webpage_access.probe import mark_probe_url, urlopen_direct

    req = urllib.request.Request(  # noqa: S310 — 本机 loopback 活验证
        mark_probe_url(url),
        headers={"User-Agent": "lwa-alias-live-verify"},
    )
    try:
        with urlopen_direct(req, timeout=timeout, ssl_context=ssl_context) as resp:
            code = int(getattr(resp, "status", None) or resp.getcode())
            ctype = resp.headers.get("Content-Type", "")
            body = resp.read(4096)
            ok = 200 <= code < 400
            return ok, code, ctype, body
    except urllib.error.HTTPError as exc:
        return False, exc.code, exc.headers.get("Content-Type"), b""
    except OSError:
        return False, None, None, b""


def verify_alias_live(
    config: Config,
    alias: str,
    *,
    entry_html: str | None,
    instance_id: str,
    workspace: Workspace | None = None,
) -> None:
    """别名入口与关键静态资源的活验证（CHK-252 第三批）。

    设置别名并 reload 后，通过统一网关端口请求 ``/{alias}/`` 及 HTML 中引用的
    JS/CSS；若资源返回 SPA HTML 兜底或不可达则失败。
    """
    from local_webpage_access.ports import entry_scheme, lan_entry_port

    port = lan_entry_port(config)
    if port is None:
        raise RecognitionError(
            "路径别名活验证需要启用别名入口端口"
            "（staticGatewayPort 或 gatewayTls 的 gatewayTlsPort）",
            instance_id=instance_id,
        )
    # W07/W08：TLS 模式下入口为 https 且探活走证书验证——错误证书即失败。
    scheme = entry_scheme(config)
    ssl_ctx: ssl.SSLContext | None = None
    if scheme == "https" and workspace is not None:
        from local_webpage_access.static_gateway import make_https_ssl_context

        ssl_ctx = make_https_ssl_context(workspace)
    base = f"{scheme}://127.0.0.1:{port}"
    entry_url = f"{base}/{alias}/"
    ok, code, ctype, body = _http_probe_alias_resource(entry_url, ssl_context=ssl_ctx)
    if not ok or not body:
        raise RecognitionError(
            f"别名入口 {entry_url} 活验证失败（HTTP {code}）",
            instance_id=instance_id,
        )
    if "html" not in (ctype or "").lower() and not _looks_like_html_body(body):
        raise RecognitionError(
            f"别名入口 {entry_url} 未返回 HTML（Content-Type={ctype!r}）",
            instance_id=instance_id,
        )
    live_html = body.decode("utf-8", "replace")
    # BUG-678：始终以本次别名 HTTP 响应做守卫与探针；传入的直达 HTML 只能加严，
    # 不得覆盖实际入口（直达正常、别名仍引用 /assets/... 时会误盖章）。
    reject_alias_if_absolute_spa_assets(
        html=live_html, alias=alias, instance_id=instance_id
    )
    if entry_html:
        reject_alias_if_absolute_spa_assets(
            html=entry_html, alias=alias, instance_id=instance_id
        )
    html = live_html
    for path in _collect_alias_live_probe_paths(html, alias):
        url = urljoin(base + "/", path.lstrip("/"))
        rok, rcode, rctype, rbody = _http_probe_alias_resource(url, ssl_context=ssl_ctx)
        if not rok:
            raise RecognitionError(
                f"别名资源 {path} 活验证失败（HTTP {rcode}，URL {url}）",
                instance_id=instance_id,
            )
        lower_path = path.lower()
        if lower_path.endswith((".js", ".mjs", ".css")) and _looks_like_html_body(rbody):
            raise RecognitionError(
                f"别名资源 {path} 返回了 HTML 而非静态文件（疑似 SPA 兜底），"
                f"页面在 /{alias}/ 下仍会白屏",
                instance_id=instance_id,
            )
        if lower_path.endswith((".js", ".mjs")) and rctype and "html" in rctype.lower():
            raise RecognitionError(
                f"别名资源 {path} Content-Type 为 {rctype!r}（期望 script）",
                instance_id=instance_id,
            )


def _alias_previously_verified(manifest: InstanceManifest, alias: str) -> bool:
    """issue #21：别名是否通过过至少一次活验证（标记匹配当前别名）。"""
    return bool(
        manifest.aliasLiveVerifiedAt
        and manifest.aliasLiveVerifiedFor == alias
    )


def _stamp_alias_guard(manifest: InstanceManifest, result: str) -> None:
    """记录别名内容守卫结果（passed / failed / skipped），不单独落盘。"""
    manifest.aliasGuardCheckedAt = now_iso()
    manifest.aliasGuardResult = result


def _persist_alias_guard(
    workspace: Workspace, instance_id: str, manifest: InstanceManifest, result: str
) -> None:
    _stamp_alias_guard(manifest, result)
    with contextlib.suppress(OSError):
        manifest.save(workspace.app_manifest_path(instance_id))


def maybe_verify_alias_after_start(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
    manifest: InstanceManifest,
    *,
    skip_compat_check: bool = False,
    alias_fragment_preexisting: bool | None = None,
) -> bool:
    """实例首次 start 后对已配置别名补跑活验证（导入期未 running 时 deferred）。

    issue #21：活验证失败时的处置按「别名是否已被验证过」分流——

    - **已验证别名**（``aliasLiveVerifiedFor`` 标记匹配，或本轮 start 前
      别名片段已在盘——存量实例无标记但已跑过完整部署周期的旁证）：
      保留别名元数据与片段，仅记 warning 与事件，不抛错、不清除。
      应用慢启动 / 入口临时 5xx 不应静默摧毁用户配置的入口。
    - **deferred 别名**（导入期设置、从未验证、片段系本轮生成）：维持
      BUG-586 收敛行为——删除片段、清 manifest/registry 并抛错。
    """
    alias = _current_alias(manifest)
    if not alias:
        return False
    if skip_compat_check:
        _persist_alias_guard(workspace, instance_id, manifest, "skipped")
        return False
    gateway = StaticGateway(workspace, config)
    if gateway.detect_backend() != "caddy":
        # BUG-679：本次未跑活验证，不得沿用旧 passed。
        _persist_alias_guard(workspace, instance_id, manifest, "skipped")
        return False
    host_port, _ = _resolve_host_port(manifest)
    html = _fetch_entrypoint_html_for_alias_guard(
        workspace=workspace, manifest=manifest, host_port=host_port
    )
    try:
        verify_alias_live(
            config, alias, entry_html=html, instance_id=instance_id, workspace=workspace
        )
    except RecognitionError as exc:
        hinted = _append_lwa_build_hint(exc, manifest, alias)
        _stamp_alias_guard(manifest, "failed")
        if _alias_previously_verified(manifest, alias) or alias_fragment_preexisting:
            log.warning(
                "实例 %s 别名 /%s/ 启动后活验证失败（%s）；"
                "别名此前已验证/已在用，保留别名配置，请检查应用入口",
                instance_id,
                alias,
                hinted,
            )
            registry.add_event(
                instance_id,
                "path-alias",
                f"别名 /{alias}/ 启动后活验证失败（已保留别名配置）：{str(hinted)[:200]}",
            )
            with contextlib.suppress(OSError):
                manifest.save(workspace.app_manifest_path(instance_id))
            return False
        _rollback_deferred_alias_after_failed_live_verify(
            workspace, config, registry, instance_id, manifest, host_port=host_port
        )
        raise hinted from exc
    except Exception as exc:  # noqa: BLE001
        if _alias_previously_verified(manifest, alias) or alias_fragment_preexisting:
            log.warning(
                "实例 %s 别名 /%s/ 启动后活验证异常（%s）；"
                "别名此前已验证/已在用，保留别名配置，请检查应用入口",
                instance_id,
                alias,
                exc,
            )
            registry.add_event(
                instance_id,
                "path-alias",
                f"别名 /{alias}/ 启动后活验证异常（已保留别名配置）：{str(exc)[:200]}",
            )
            _persist_alias_guard(workspace, instance_id, manifest, "failed")
            return False
        _rollback_deferred_alias_after_failed_live_verify(
            workspace, config, registry, instance_id, manifest, host_port=host_port
        )
        raise RecognitionError(
            f"别名 /{alias}/ 启动后活验证异常：{exc}",
            instance_id=instance_id,
        ) from exc
    # issue #21：验证通过即落标记，后续启动的活验证失败走「保留」分支。
    manifest.aliasLiveVerifiedAt = now_iso()
    manifest.aliasLiveVerifiedFor = alias
    _stamp_alias_guard(manifest, "passed")
    with contextlib.suppress(OSError):
        manifest.save(workspace.app_manifest_path(instance_id))
    registry.add_event(instance_id, "path-alias", f"别名 /{alias}/ 启动后活验证通过")
    return True


def _resolve_host_port(manifest: InstanceManifest) -> tuple[int | None, int | None]:
    """解析实例对外 hostPort / internalPort（静态站点或容器实例共用）。"""
    host_port: int | None = None
    internal_port: int | None = None
    if manifest.static is not None and manifest.static.hostPort is not None:
        host_port = manifest.static.hostPort
    if manifest.container is not None and manifest.container.hostPort is not None:
        host_port = host_port or manifest.container.hostPort
        internal_port = manifest.container.internalPort
    if manifest.network is not None:
        host_port = host_port or manifest.network.hostPort
        internal_port = internal_port or manifest.network.internalPort
    return host_port, internal_port


def _apply_manifest_alias(
    manifest: InstanceManifest,
    config: Config,
    alias: str | None,
) -> None:
    """写入 manifest.static（静态站点）或 manifest.container（容器，IMP-014）
    与 manifest.network（不持久化）。"""
    new_mode = RouteMode.NAME.value if alias else RouteMode.PORT.value
    # issue #21：换别名 / 清除别名时，旧别名的活验证标记随之失效。
    if manifest.aliasLiveVerifiedFor != alias:
        manifest.aliasLiveVerifiedAt = None
        manifest.aliasLiveVerifiedFor = None
        manifest.aliasGuardCheckedAt = None
        manifest.aliasGuardResult = None
    if manifest.runtime == Runtime.DOCKER_COMPOSE:
        # IMP-014：容器别名写入 container.routeMode/routeHost，registry 容器表据此联动。
        if manifest.container is not None:
            manifest.container = manifest.container.model_copy(
                update={"routeMode": new_mode, "routeHost": alias}
            )
    else:
        static = manifest.static or StaticConfig()
        manifest.static = static.model_copy(
            update={
                "routeMode": new_mode,
                "routeHost": alias,
            }
        )

    host_port, internal_port = _resolve_host_port(manifest)
    if host_port is not None:
        entry = build_network_entry(
            config,
            host_port,
            internal_port=internal_port,
            path_alias=alias,
        )
        manifest.network = NetworkConfig(**entry)
        return

    # 评审-组8：无 hostPort 且 network 为 None 的最小化 manifest 此前直接
    # AttributeError（非 LwaError，CLI 打裸 traceback）
    if manifest.network is None:
        from local_webpage_access.models import NetworkConfig as _NC

        manifest.network = _NC()
    if alias is None:
        manifest.network = manifest.network.model_copy(
            update={
                "routeMode": RouteMode.PORT.value,
                "routeHost": None,
                "routeUrl": None,
            }
        )
    else:
        manifest.network = manifest.network.model_copy(
            update={
                "routeMode": RouteMode.NAME.value,
                "routeHost": alias,
                "routeUrl": None,
            }
        )


def _rollback_alias_config(
    gateway: StaticGateway,
    instance_id: str,
    *,
    previous_alias: str | None,
    host_port: int | None,
    had_fragment: bool,
    previous_fragment: str | None,
    runtime: str | None = None,
) -> None:
    """Caddy reload 失败后恢复别名片段文件到变更前状态。"""
    path = gateway.ws.app_alias_config(instance_id)
    if had_fragment and previous_fragment is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(previous_fragment, encoding="utf-8")
    elif path.exists():
        path.unlink()
    elif previous_alias and host_port is not None:
        gateway.generate_alias_config(instance_id, previous_alias, host_port, runtime=runtime)


def _apply_gateway_alias(
    workspace: Workspace,
    config: Config,
    instance_id: str,
    alias: str | None,
    host_port: int | None,
    *,
    previous_alias: str | None,
    runtime: str,
) -> tuple[bool, bool]:
    """运行中实例同步 Caddy 别名片段。返回 (alias_entry_enabled, gateway_reloaded)。

    须在 manifest/registry 落盘**之前**调用：reload 失败时回滚别名片段并抛
    :class:`GatewayError`，调用方不得持久化新别名。

    静态站点（``runtime=shared-static``）仅在 ``gateway.is_enabled`` 时同步，
    与既有行为一致；容器实例（``runtime=docker-compose``，IMP-014）由 Docker
    托管进程、不经过 StaticGateway.enable，因此无 ``is_enabled`` 语义，只要
    Caddy 后端在线且 ``host_port`` 已知即生成别名片段（reverse_proxy hostPort）。
    """
    gateway = StaticGateway(workspace, config)
    backend = gateway.detect_backend()
    if host_port is None:
        return False, False
    if runtime == Runtime.SHARED_STATIC.value and not gateway.is_enabled(instance_id):
        return False, False

    if backend == "caddy":
        fragment_path = gateway.ws.app_alias_config(instance_id)
        had_fragment = fragment_path.is_file()
        previous_fragment = fragment_path.read_text(encoding="utf-8") if had_fragment else None
        try:
            if alias:
                gateway.generate_alias_config(instance_id, alias, host_port, runtime=runtime)
            else:
                gateway.remove_alias_config(instance_id)
            gateway.reload_all()
        except GatewayError:
            _rollback_alias_config(
                gateway,
                instance_id,
                previous_alias=previous_alias,
                host_port=host_port,
                had_fragment=had_fragment,
                previous_fragment=previous_fragment,
                runtime=runtime,
            )
            raise
        return bool(alias), True

    if alias:
        log.warning(
            "实例 %s 配置了路径别名 %s，但当前静态后端为 %s，别名入口未启用（仅通过端口 %s 访问）",
            instance_id,
            alias,
            backend,
            host_port,
        )
    gateway.remove_alias_config(instance_id)
    return False, False


def _alias_lock_path(workspace: Workspace):
    return workspace.run / "path-alias.lock"


@contextlib.contextmanager
def path_alias_lock(
    workspace: Workspace, *, timeout: float = _ALIAS_LOCK_TIMEOUT
) -> Iterator[None]:
    """工作区级路径别名互斥锁（BUG-167）。

    双层锁：进程内 ``RLock`` + 跨进程文件锁。须在 :func:`instance_lock` **之前**
    获取，避免与生命周期锁交叉死锁。
    """
    if not _alias_thread_lock.acquire(timeout=timeout):
        raise LifecycleError(f"路径别名锁等待超时（{timeout}s）")
    file_acquired = False
    fd: int | None = None
    lock_path = _alias_lock_path(workspace)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        ensure_lockable(fd)
        while True:
            try:
                try_acquire_exclusive(fd)
                write_lock_payload(fd, f"{os.getpid()}\n{time.time():.3f}\n".encode())
                file_acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LifecycleError(f"路径别名锁被占用，等待超时（{timeout}s）")
                time.sleep(0.05)
        yield
    finally:
        if file_acquired and fd is not None:
            release_exclusive(fd)
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        _alias_thread_lock.release()


def set_instance_path_alias(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
    alias: str | None,
    *,
    skip_compat_check: bool = False,
    require_desired_alias: str | None = None,
) -> PathAliasResult:
    """设置或清除实例的路径别名 slug（IMP-006 静态站点 / IMP-014 容器实例）。

    BUG-167：持工作区别名锁 + 实例生命周期锁，并在锁内重新校验唯一性，
    避免并发「先查后写」写入重复别名或丢失 manifest 更新。

    ``skip_compat_check=True`` 跳过别名入口活验证（审计事件仍会记录）。
    ``require_desired_alias``：锁内重读后若期望别名已变（用户并发 clear），则中止。
    """
    from local_webpage_access.lifecycle import instance_lock

    if alias is not None:
        alias = alias.strip() or None

    with path_alias_lock(workspace):
        with instance_lock(workspace, instance_id):
            return _set_instance_path_alias_locked(
                workspace,
                config,
                registry,
                instance_id,
                alias,
                skip_compat_check=skip_compat_check,
                require_desired_alias=require_desired_alias,
            )


def _enrich_alias_rejection_with_findings(
    exc: RecognitionError,
    manifest: InstanceManifest,
) -> RecognitionError:
    """C.03（IMP-056 后置包）：IMP-055 拒绝原因后附加关联的预检 finding。

    只取 CHK-P03（绝对 API 路径 / 空 base 常量——与别名入口失效同族）且
    file 非空的 finding，按「预检线索」标注，最多 3 条。无关联 finding 时
    原样返回原异常（错误文案零变化）。**禁止**据此单独拒绝 alias——主错误
    仍来自 IMP-055 硬守卫。
    """
    findings = getattr(manifest, "compatibilityFindings", None) or []
    related = [
        f
        for f in findings
        if getattr(f, "checkId", "") == "CHK-P03" and getattr(f, "file", None)
    ]
    if not related:
        return exc
    # LwaError.__str__ 自带 [CODE] 前缀；拼接须用原始 message，避免双重前缀。
    lines = [exc.message or str(exc), "", "—— 预检线索（advisory，导入期静态扫描，不改变上述拒绝原因）——"]
    for f in related[:3]:
        loc = f"{f.file}:{f.line}" if f.line else str(f.file)
        lines.append(f"  · {f.title}（{loc}）")
        lines.append(f"    修复建议：{f.fix}")
    if len(related) > 3:
        lines.append(f"  …另有 {len(related) - 3} 条，见 lwa doctor / 管理页")
    enriched = RecognitionError("\n".join(lines))
    enriched.context = dict(getattr(exc, "context", {}) or {})
    return enriched


def _append_lwa_build_hint(
    exc: RecognitionError,
    manifest: InstanceManifest,
    alias: str,
) -> RecognitionError:
    """DEV-132：Vite 前端实例的别名拒绝附加 LWA 侧最短解法。

    守卫方案 B 指向应用侧改造（改源码 / 本地重建）；对 shared-static 且
    stack 检出 Vite 的实例，LWA 自身即可完成「构建期注入 base」——
    manifest ``buildEnv`` 或 ``entry.build`` 注入 + rebuild；
    环境变量方式要求项目读取 VITE_BASE。仅提示，不改变拒绝判定；非 Vite 原样返回
    （错误文案零变化）。

    BUG-640：``buildBaseFromAlias`` 已开启时，构建期 ``effective_build_env()``
    会用当前已保存别名推导 ``VITE_BASE`` 并覆盖手动值——直接
    ``configure --build-env`` + rebuild 无法迁移到新 base，修复指引须先
    ``--no-follow-alias-base`` 关闭跟随，完成目标 base 构建与别名变更后再
    重新开启，否则指引本身会陷入重复失败。
    """
    if manifest.runtime not in (Runtime.SHARED_STATIC, Runtime.DOCKER_COMPOSE):
        return exc
    stack_lower = {s.lower() for s in manifest.stack}
    if "vite" not in stack_lower:
        return exc
    from local_webpage_access.models import Kind

    # LwaError.__str__ 自带 [CODE] 前缀；拼接须用原始 message。
    if manifest.runtime == Runtime.DOCKER_COMPOSE and manifest.kind == Kind.PYTHON:
        # BUG-685：开启跟随时 effective_build_env 仍用当前别名（无别名则 /）
        # 覆盖手动 VITE_BASE——直接 configure + rebuild 迁移不到目标 base，
        # 须复用「先关跟随 → 设目标 base → 重建切别名 → 恢复跟随」的顺序。
        if manifest.buildBaseFromAlias:
            steps = [
                f"  1. lwa configure {manifest.id} --no-follow-alias-base"
                f" --build-env VITE_BASE=/{alias}/"
                "（当前已开启别名跟随，手动 VITE_BASE 会被当前别名或 / 覆盖——"
                "先关跟随再设目标 base；buildEnv 整组替换，其他变量需一并传入）",
                f"  2. lwa rebuild {manifest.id}（执行前端构建并复制产物）",
                f"  3. 重新设置别名 /{alias}/ 即可通过本守卫",
                f"  4. lwa configure {manifest.id} --follow-alias-base"
                "（迁移完成后重新开启跟随，后续构建按新别名推导 base）",
            ]
        else:
            steps = [
                f"  1. lwa configure {manifest.id} --build-env VITE_BASE=/{alias}/",
                "     （可选再加 --build-hook 'cd frontend && npm ci && npm run build'）",
                f"  2. lwa rebuild {manifest.id} 后重新设置别名 /{alias}/",
            ]
        lines = [
            exc.message or str(exc),
            "",
            "—— Python 容器前端（Dockerfile 不会执行 manifest 里的构建命令字段）——",
            "  仅注入 VITE_BASE 不会改写已编译进镜像的静态产物。",
            "  需要真实的前端构建及产物复制：源码须含 frontend/（或 web/、client/）"
            "且具备 npm 锁文件与 build 脚本，LWA 会在 COPY 之后执行 "
            "npm ci && npm run build；其余情况用 buildHooks 自定义。",
            *steps,
            "  预编译 backend/static 不会因环境变量自行带上别名前缀。",
        ]
        enriched = RecognitionError("\n".join(lines))
        enriched.context = dict(getattr(exc, "context", {}) or {})
        return enriched
    if manifest.buildBaseFromAlias:
        lines = [
            exc.message or str(exc),
            "",
            "—— Vite 构建配置（当前已开启别名跟随，手动 VITE_BASE 会被当前别名覆盖）——",
            f"  按顺序迁移到 /{alias}/：",
            f"  1. lwa configure {manifest.id} --no-follow-alias-base"
            f" --build-env VITE_BASE=/{alias}/"
            "（先关跟随再设目标 base；buildEnv 整组替换，其他变量需一并传入；"
            "要求 vite.config 读 process.env.VITE_BASE）",
            f"  2. lwa rebuild {manifest.id}（产物资源将带 /{alias}/ 前缀）",
            f"  3. 重新设置别名 /{alias}/ 即可通过本守卫",
            f"  4. lwa configure {manifest.id} --follow-alias-base"
            "（迁移完成后重新开启跟随，后续构建按新别名推导 base）",
        ]
    else:
        lines = [
            exc.message or str(exc),
            "",
            "—— Vite 构建配置（环境变量方式需要项目配置配合）——",
            f"  选择下列方式后执行 lwa rebuild {manifest.id}：",
            f"  · lwa configure {manifest.id} --build-env VITE_BASE=/{alias}/"
            "（buildEnv 整组替换，其他变量需一并传入；要求 vite.config 读 process.env.VITE_BASE）",
            f'  · 或改 "entry": {{"build": "npm run build -- --base=/{alias}/"}}'
            "（仅适用于 build 脚本直接调用 Vite；entry.build 重扫时不保留）",
            f"  rebuild 后产物资源将带 /{alias}/ 前缀，重新设置别名即可通过本守卫。",
        ]
    enriched = RecognitionError("\n".join(lines))
    enriched.context = dict(getattr(exc, "context", {}) or {})
    return enriched


def _set_instance_path_alias_locked(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
    alias: str | None,
    *,
    skip_compat_check: bool = False,
    require_desired_alias: str | None = None,
) -> PathAliasResult:
    """锁内实现：重新加载 manifest 后校验并落盘。"""
    mpath = workspace.app_manifest_path(instance_id)
    manifest = InstanceManifest.load(mpath)

    runtime = manifest.runtime
    if runtime not in (Runtime.SHARED_STATIC, Runtime.DOCKER_COMPOSE):
        raise RecognitionError(
            f"路径别名仅支持 shared-static / docker-compose 实例，当前为 {runtime.value}",
            instance_id=instance_id,
        )

    if require_desired_alias is not None:
        latest_desired = (getattr(manifest, "desiredAlias", None) or "").strip()
        if latest_desired != require_desired_alias.strip():
            route_url = manifest.network.routeUrl if manifest.network else None
            return PathAliasResult(
                instance_id=instance_id,
                alias=_current_alias(manifest),
                route_url=route_url,
                alias_entry_enabled=False,
                gateway_reloaded=False,
                unchanged=True,
                html_verified=True,
                live_verified=True,
            )

    current = _current_alias(manifest)
    if alias is None and getattr(manifest, "desiredAlias", None):
        manifest.desiredAlias = None
        with contextlib.suppress(Exception):
            manifest.save(mpath)
    if alias == current:
        if alias is not None and (getattr(manifest, "desiredAlias", None) or "").strip() != alias:
            manifest.desiredAlias = alias
            with contextlib.suppress(Exception):
                manifest.save(mpath)
        route_url = manifest.network.routeUrl if manifest.network else None
        return PathAliasResult(
            instance_id=instance_id,
            alias=current,
            route_url=route_url,
            alias_entry_enabled=False,
            gateway_reloaded=False,
            unchanged=True,
            html_verified=True,
            live_verified=True,
        )

    if alias is not None:
        # 锁内再查一次，消除 TOCTOU（BUG-167）
        existing = set(registry.list_route_hosts(exclude_instance=instance_id).keys())
        validate_path_alias(alias, existing_aliases=existing)
        # IMP-022（WBS-20260708 阶段4.1）：路径别名依赖 Caddy 统一入口
        # （:{staticGatewayPort} 的 import 块），builtin 多端口模式无统一入口，
        # 别名设置了也访问不到。显式拦截，不再无声写元数据造成「设置成功但访问失败」。
        # 清除别名（alias=None）在 builtin 下仍允许（清除恒安全）。
        backend = StaticGateway(workspace, config).detect_backend()
        if backend != "caddy":
            raise RecognitionError(
                f"路径别名需要 Caddy 网关统一入口，当前静态后端为 {backend}（无 "
                f":{config.staticGatewayPort} 别名入口）。请先 `lwa gateway on` 启用 "
                f"Caddy（或安装 caddy 可执行文件），或继续通过 hostPort 端口直达。",
                instance_id=instance_id,
            )

    host_port, _ = _resolve_host_port(manifest)

    # IMP-023 / IMP-055：设别名前检测入口 HTML 绝对路径资源；
    # 能证明会白屏则硬失败（对齐 IMP-022）。清除别名（alias=None）恒安全，跳过。
    # 探不到 HTML 时不拦截（无法证明），但成功路径提示「未验证入口 HTML」。
    # issue #10：结构化扫描按 /{alias}/ 前缀豁免；提示型引用只警告不拦截。
    html_verified = False
    html_warnings: tuple[str, ...] = ()
    html: str | None = None
    if alias is not None:
        html = _fetch_entrypoint_html_for_alias_guard(
            workspace=workspace, manifest=manifest, host_port=host_port
        )
        if html is not None:
            html_verified = True
            try:
                html_warnings = reject_alias_if_absolute_spa_assets(
                    html=html, alias=alias, instance_id=instance_id
                )
            except RecognitionError as exc:
                # C.03（IMP-056 后置包）：拒绝原因保持 IMP-055 原文，其后附加
                # 导入期预检的关联 finding 线索（file/line/fix）。无 finding
                # 时错误完全原样；findings 只提供线索，不参与拒绝判定。
                # DEV-132：Vite 实例再附加 LWA 侧解法（buildEnv / --base + rebuild）。
                enriched = _enrich_alias_rejection_with_findings(exc, manifest)
                raise _append_lwa_build_hint(enriched, manifest, alias) from exc
        else:
            log.warning(
                "实例 %s 设置别名 /%s/ 时探不到入口 HTML，守卫记为 skipped（不硬失败）",
                instance_id,
                alias,
            )

    # BUG-586：活验证失败回滚需恢复「变更前」片段，快照必须在新片段写入
    # 之前捕获；回滚时再读文件拿到的已是刚写入的新片段，恢复等于没恢复。
    fragment_path = workspace.app_alias_config(instance_id)
    had_fragment = fragment_path.is_file()
    previous_fragment = fragment_path.read_text(encoding="utf-8") if had_fragment else None

    # 运行中 + Caddy：先网关重载，成功后再活验证与落盘
    alias_entry_enabled, gateway_reloaded = _apply_gateway_alias(
        workspace,
        config,
        instance_id,
        alias,
        host_port,
        previous_alias=current,
        runtime=runtime.value,
    )

    live_verified = False
    compat_skipped = False
    if alias is not None and gateway_reloaded:
        if skip_compat_check:
            compat_skipped = True
            registry.add_event(
                instance_id,
                "path-alias",
                f"别名 /{alias}/ 活验证已跳过（--skip-compat-check）",
            )
        else:
            try:
                verify_alias_live(
                    config,
                    alias,
                    entry_html=html if html_verified else None,
                    instance_id=instance_id,
                    workspace=workspace,
                )
                live_verified = True
            except RecognitionError:
                _rollback_alias_after_failed_live_verify(
                    workspace,
                    config,
                    instance_id,
                    previous_alias=current,
                    host_port=host_port,
                    runtime=runtime.value,
                    had_fragment=had_fragment,
                    previous_fragment=previous_fragment,
                )
                if alias is not None:
                    manifest.desiredAlias = alias
                    with contextlib.suppress(Exception):
                        manifest.save(mpath)
                raise

    _apply_manifest_alias(manifest, config, alias)
    # issue #35：期望别名独立于当前 routeHost，活验证失败回滚后仍可自动补登记。
    if alias is not None:
        manifest.desiredAlias = alias
    else:
        manifest.desiredAlias = None
    # issue #21：运行中设置且活验证通过 → 落验证标记（须在 _apply_manifest_alias
    # 之后，否则新别名会被判为「标记不匹配」而清空）。
    if alias is not None and live_verified:
        manifest.aliasLiveVerifiedAt = now_iso()
        manifest.aliasLiveVerifiedFor = alias
    if alias is not None:
        if live_verified:
            _stamp_alias_guard(manifest, "passed")
        elif html is None:
            _stamp_alias_guard(manifest, "skipped")
        elif html_verified:
            _stamp_alias_guard(manifest, "passed")
    manifest.save(mpath)

    # 持久化别名到对应子表：静态站点 / 容器实例（IMP-014 容器别名落 containers 表）
    if runtime == Runtime.DOCKER_COMPOSE and manifest.container is not None:
        registry.upsert_container(instance_id, manifest.container.model_dump())
    else:
        static_dump = manifest.static.model_dump() if manifest.static else {}
        registry.upsert_static_site(instance_id, static_dump)
    registry.add_event(
        instance_id,
        "path-alias",
        f"路径别名：{current or '(无)'} → {alias or '(无)'}",
    )

    route_url = manifest.network.routeUrl if manifest.network else None

    return PathAliasResult(
        instance_id=instance_id,
        alias=alias,
        route_url=route_url,
        alias_entry_enabled=alias_entry_enabled,
        gateway_reloaded=gateway_reloaded,
        unchanged=False,
        html_verified=html_verified,
        html_warnings=html_warnings,
        live_verified=live_verified,
        compat_check_skipped=compat_skipped,
    )


def _rollback_alias_after_failed_live_verify(
    workspace: Workspace,
    config: Config,
    instance_id: str,
    *,
    previous_alias: str | None,
    host_port: int | None,
    runtime: str | None,
    had_fragment: bool,
    previous_fragment: str | None,
) -> None:
    """活验证失败后恢复别名片段到变更前状态。

    BUG-586：``had_fragment`` / ``previous_fragment`` 须为**写入新片段之前**
    捕获的快照；在此再读文件只能拿到刚写入的新片段，回滚无效。
    """
    gateway = StaticGateway(workspace, config)
    try:
        _rollback_alias_config(
            gateway,
            instance_id,
            previous_alias=previous_alias,
            host_port=host_port,
            had_fragment=had_fragment,
            previous_fragment=previous_fragment,
            runtime=runtime,
        )
        gateway.reload_all()
    except GatewayError as exc:
        log.warning("别名活验证失败后回滚 reload 失败：%s", exc)


def _rollback_deferred_alias_after_failed_live_verify(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
    manifest: InstanceManifest,
    *,
    host_port: int | None,
) -> None:
    """BUG-586：deferred 活验证失败后收敛三处状态（Caddy 片段 + manifest + registry）。

    deferred 别名是实例未 running 时设置的，失败后没有「上一个别名」可回退，
    统一收敛为清除别名：删除别名片段并 best-effort reload、manifest 别名清空、
    registry 对应子表同步。
    """
    gateway = StaticGateway(workspace, config)
    try:
        _rollback_alias_config(
            gateway,
            instance_id,
            previous_alias=None,
            host_port=host_port,
            had_fragment=False,
            previous_fragment=None,
            runtime=manifest.runtime.value,
        )
        gateway.reload_all()
    except GatewayError as exc:
        log.warning("别名 deferred 活验证失败后回滚 reload 失败：%s", exc)
    desired = (getattr(manifest, "desiredAlias", None) or "").strip() or _current_alias(manifest)
    _apply_manifest_alias(manifest, config, None)
    if desired:
        manifest.desiredAlias = desired
    manifest.save(workspace.app_manifest_path(instance_id))
    if manifest.runtime == Runtime.DOCKER_COMPOSE and manifest.container is not None:
        registry.upsert_container(instance_id, manifest.container.model_dump())
    else:
        static_dump = manifest.static.model_dump() if manifest.static else {}
        registry.upsert_static_site(instance_id, static_dump)
    registry.add_event(
        instance_id,
        "path-alias",
        "别名 deferred 活验证失败，已回滚别名设置（片段/manifest/registry）",
    )


def maybe_restore_desired_alias_after_start(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
    manifest: InstanceManifest,
) -> bool:
    """issue #35 建议 4：部署成功后若有期望别名但未登记，自动补登记。

    BUG-662：必须重读磁盘上的 ``desiredAlias``，避免解锁前快照把用户已 clear 的别名挂回。
    """
    try:
        latest = InstanceManifest.load(workspace.app_manifest_path(instance_id))
    except Exception:  # noqa: BLE001
        latest = manifest
    desired = (getattr(latest, "desiredAlias", None) or "").strip()
    if not desired:
        return False
    if _current_alias(latest) == desired:
        return False
    try:
        set_instance_path_alias(
            workspace,
            config,
            registry,
            instance_id,
            desired,
            require_desired_alias=desired,
        )
        refreshed = InstanceManifest.load(workspace.app_manifest_path(instance_id))
        manifest.container = refreshed.container
        manifest.static = refreshed.static
        manifest.network = refreshed.network
        manifest.desiredAlias = refreshed.desiredAlias
        manifest.aliasLiveVerifiedAt = refreshed.aliasLiveVerifiedAt
        manifest.aliasLiveVerifiedFor = refreshed.aliasLiveVerifiedFor
        manifest.aliasGuardCheckedAt = refreshed.aliasGuardCheckedAt
        manifest.aliasGuardResult = refreshed.aliasGuardResult
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("实例 %s 自动补登记别名 /%s/ 失败：%s", instance_id, desired, exc)
        return False


__all__ = [
    "PathAliasResult",
    "maybe_restore_desired_alias_after_start",
    "maybe_verify_alias_after_start",
    "path_alias_lock",
    "reject_alias_if_absolute_spa_assets",
    "set_instance_path_alias",
    "verify_alias_live",
]
