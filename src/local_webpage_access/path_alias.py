"""IMP-006：实例路径别名在线设置与清除（管理页 API / CLI 共用）。"""

from __future__ import annotations

import contextlib
import os
import threading
import time
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
from local_webpage_access.logging import get_logger
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
    url: str, *, timeout: float = 3.0
) -> tuple[bool, int | None, str | None, bytes]:
    """GET 别名下资源；返回 (ok, status, content_type, body_prefix)。"""
    from local_webpage_access.probe import mark_probe_url, urlopen_direct

    req = urllib.request.Request(  # noqa: S310 — 本机 loopback 活验证
        mark_probe_url(url),
        headers={"User-Agent": "lwa-alias-live-verify"},
    )
    try:
        with urlopen_direct(req, timeout=timeout) as resp:
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
) -> None:
    """别名入口与关键静态资源的活验证（CHK-252 第三批）。

    设置别名并 reload 后，通过统一网关端口请求 ``/{alias}/`` 及 HTML 中引用的
    JS/CSS；若资源返回 SPA HTML 兜底或不可达则失败。
    """
    port = _gateway_port(config)
    if port is None:
        raise RecognitionError(
            "路径别名活验证需要启用 staticGatewayPort（Caddy 统一入口端口）",
            instance_id=instance_id,
        )
    base = f"http://127.0.0.1:{port}"
    entry_url = f"{base}/{alias}/"
    ok, code, ctype, body = _http_probe_alias_resource(entry_url)
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
    html = entry_html or body.decode("utf-8", "replace")
    for path in _collect_alias_live_probe_paths(html, alias):
        url = urljoin(base + "/", path.lstrip("/"))
        rok, rcode, rctype, rbody = _http_probe_alias_resource(url)
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


def maybe_verify_alias_after_start(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
    manifest: InstanceManifest,
    *,
    skip_compat_check: bool = False,
) -> bool:
    """实例首次 start 后对已配置别名补跑活验证（导入期未 running 时 deferred）。"""
    alias = _current_alias(manifest)
    if not alias or skip_compat_check:
        return False
    gateway = StaticGateway(workspace, config)
    if gateway.detect_backend() != "caddy":
        return False
    host_port, _ = _resolve_host_port(manifest)
    html = _fetch_entrypoint_html_for_alias_guard(
        workspace=workspace, manifest=manifest, host_port=host_port
    )
    try:
        verify_alias_live(config, alias, entry_html=html, instance_id=instance_id)
    except RecognitionError:
        _rollback_deferred_alias_after_failed_live_verify(
            workspace, config, registry, instance_id, manifest, host_port=host_port
        )
        raise
    except Exception as exc:  # noqa: BLE001
        _rollback_deferred_alias_after_failed_live_verify(
            workspace, config, registry, instance_id, manifest, host_port=host_port
        )
        raise RecognitionError(
            f"别名 /{alias}/ 启动后活验证异常：{exc}",
            instance_id=instance_id,
        ) from exc
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
) -> PathAliasResult:
    """设置或清除实例的路径别名 slug（IMP-006 静态站点 / IMP-014 容器实例）。

    BUG-167：持工作区别名锁 + 实例生命周期锁，并在锁内重新校验唯一性，
    避免并发「先查后写」写入重复别名或丢失 manifest 更新。

    ``skip_compat_check=True`` 跳过别名入口活验证（审计事件仍会记录）。
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
            )


def _set_instance_path_alias_locked(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
    alias: str | None,
    *,
    skip_compat_check: bool = False,
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

    current = _current_alias(manifest)
    if alias == current:
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
    if alias is not None:
        html = _fetch_entrypoint_html_for_alias_guard(
            workspace=workspace, manifest=manifest, host_port=host_port
        )
        if html is not None:
            html_verified = True
            html_warnings = reject_alias_if_absolute_spa_assets(
                html=html, alias=alias, instance_id=instance_id
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
                raise

    _apply_manifest_alias(manifest, config, alias)
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
    _apply_manifest_alias(manifest, config, None)
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


__all__ = [
    "PathAliasResult",
    "maybe_verify_alias_after_start",
    "path_alias_lock",
    "reject_alias_if_absolute_spa_assets",
    "set_instance_path_alias",
    "verify_alias_live",
]
