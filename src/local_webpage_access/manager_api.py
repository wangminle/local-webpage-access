"""管理页后端 API（WBS-22）。

基于 FastAPI，提供管理页所需的数据与操作接口：

* ``GET  /api/stats``            —— 顶部统计（WBS-22.05/06）
* ``GET  /api/instances``        —— 实例列表（WBS-22.03）
* ``GET  /api/instances/{id}``   —— 实例详情（WBS-22.04）
* ``GET  /api/instances/{id}/logs?category=&tail=``（WBS-22.07）
* ``GET  /api/instances/{id}/resources``
* ``POST /api/instances/{id}/{start|stop|restart|rebuild}``（WBS-22.08）
* ``POST /api/instances/{id}/recover``             —— 一键恢复（DEV-043）
* ``POST /api/instances/{id}/remove``              —— 移除实例（IMP-019）
* ``GET  /api/pending``          —— pending / 导入队列（WBS-22.09）
* ``GET  /api/port-pool``        —— 端口池占用（WBS-22.10）
* ``GET  /api/redundant``        —— 冗余实例列表（IMP-019 / WBS-22.13）
* ``POST /api/redundant/remove`` —— 批量移除冗余实例（IMP-019 / WBS-22.13）
* ``GET  /api/health``           —— 轻量存活（能力片段来自启动缓存，BUG-254）
* ``GET  /api/capability``       —— 鉴权能力报告（``?refresh=true`` 同步重探）

所有 ``/api/*`` 路由（除 ``/api/health`` 外）都要求 API token（WBS-22.12）。
静态资源（管理页前端）由 ``/`` 托管（WBS-22.02），前端实现在 WBS-23。

设计要点：

* **复用 lifecycle**：start/stop/restart/rebuild 直接调用
  :mod:`local_webpage_access.lifecycle` 的同名函数，确保管理页操作与 CLI 走同一套
  逻辑（验收标准 3）。
* **WAL + 单连接**：registry 在 manager 进程内打开一个连接，借助 SQLite WAL
  与 :func:`~local_webpage_access.registry.connection.transaction` 的连接级锁，
  跨请求线程安全共享（验收标准 3 的并发前提）。
* **错误格式**：统一 ``{"error": {"code": "...", "message": "..."}}``（WBS-22.11）。
"""

from __future__ import annotations

import hmac
import ipaddress
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from local_webpage_access.config import Config
from local_webpage_access.errors import (
    BuildError,
    ConfigError,
    DataNonemptyError,
    DirectoryPickerError,
    DockerError,
    FolderSourceError,
    GatewayError,
    GitSourceError,
    HostingError,
    LifecycleError,
    LwaError,
    PathError,
    PortError,
    RecognitionError,
    RegistryError,
    SchemaError,
    ZipImportError,
)
from local_webpage_access.logging import get_logger
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry

log = get_logger("manager")

# ---- 常量 -------------------------------------------------------------------

TOKEN_FILENAME = "manager-token.json"  # 相对 run/
MANAGER_STATIC_DIR = "manager_static"  # 相对包目录
# BUG-379：首次延后刷新（等 gateway 晚于 manager 就绪），之后周期刷新完整能力。
# 默认 300s 与 §23 / gateway 前台周期同量级，降低 Docker/Caddy 子进程开销。
_CAPABILITY_INITIAL_DELAY = 15.0
_CAPABILITY_REFRESH_INTERVAL = 300.0
_CAPABILITY_STOP_JOIN_TIMEOUT = 2.0
_CAPABILITY_ERROR_LOG_INTERVAL = 300.0


# ---- token 机制（WBS-22.12）-------------------------------------------------


def token_path(workspace: Workspace) -> Path:
    """token 文件路径：``run/manager-token.json``。"""
    return workspace.run / TOKEN_FILENAME


def ensure_token(workspace: Workspace) -> str:
    """读取或生成管理页 API token（幂等）。

    token 以明文存于 ``run/manager-token.json``，仅工作区所有者可读。
    首次调用生成一个 URL-safe 随机串；后续调用返回已存在的 token。
    """
    import json

    path = token_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            token = str(data.get("token", "")).strip()
            if token:
                return token
        except (OSError, ValueError):
            pass  # 损坏则重新生成
    return _write_token(workspace, secrets.token_urlsafe(24))


def rotate_token(workspace: Workspace) -> str:
    """轮换管理页 API token（BUG-118 / IMP-046）。

    覆盖写入新 token 并收紧权限。``_verify_token`` 每次请求读盘，故轮换后
    新 token 立即生效、旧 token 立即失效，无需重启管理页。
    """
    return _write_token(workspace, secrets.token_urlsafe(24))


# IMP-046：7×24h 自动轮换常量
TOKEN_ROTATE_HOURS_DEFAULT = 168
_TOKEN_ROTATE_TICK_INTERVAL = 1800.0  # 30 min


def _write_token(workspace: Workspace, token: str) -> str:
    import contextlib
    import json
    import os

    path = token_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps({"token": token, "createdAt": _now_iso()}, ensure_ascii=False, indent=2) + "\n"
    )
    # BUG-447：先写临时文件再 os.replace，避免 O_TRUNC 窗口并发读到空/残缺 → 401。
    # BUG-334：临时文件一步以 0o600 创建，避免 write_text 后 chmod 的权限窗口。
    tmp_path = path.with_name(f".manager-token.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with contextlib.suppress(OSError):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1
            fh.write(payload)
            fh.flush()
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
        os.replace(str(tmp_path), str(path))
        tmp_path = None  # type: ignore[assignment]
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return token


def read_token(workspace: Workspace) -> str | None:
    """读取已存在的 token；不存在返回 ``None``。"""
    import json

    path = token_path(workspace)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    token = str(data.get("token", "")).strip()
    return token or None


def read_token_metadata(workspace: Workspace) -> dict[str, str | None]:
    """IMP-046：读取 token 文件元数据（token + createdAt）。

    返回 ``{"token": str | None, "createdAt": str | None}``。
    文件不存在或损坏时两个字段均为 ``None``。
    旧文件缺少 ``createdAt`` 时该字段为 ``None``（调用方可据此补写）。
    """
    import json

    path = token_path(workspace)
    if not path.is_file():
        return {"token": None, "createdAt": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"token": None, "createdAt": None}
    token = str(data.get("token", "")).strip() or None
    created = data.get("createdAt")
    created_str = str(created).strip() if created else None
    return {"token": token, "createdAt": created_str}


def should_rotate_token(
    created_at: str | None, *, now: str, hours: int = TOKEN_ROTATE_HOURS_DEFAULT
) -> bool:
    """IMP-046：判定 token 是否到期需要轮换（纯函数，无 IO）。

    * ``created_at`` 为 ``None`` 或非法时间 -> ``True``（视为需补写/轮换）。
    * ``now`` 必须为合法 ISO 时间戳。
    * ``hours`` 默认 168（7×24）；差 1 秒未到期返回 ``False``。
    """
    from datetime import datetime

    if not created_at:
        return True
    try:
        created_dt = datetime.fromisoformat(created_at)
        now_dt = datetime.fromisoformat(now)
    except (ValueError, TypeError):
        return True
    # 确保-aware 比较：naive 时间补本地时区
    if created_dt.tzinfo is None:
        created_dt = created_dt.astimezone()
    if now_dt.tzinfo is None:
        now_dt = now_dt.astimezone()
    elapsed = now_dt - created_dt
    return elapsed.total_seconds() >= hours * 3600


def maybe_rotate_token(
    workspace: Workspace,
    *,
    hours: int = TOKEN_ROTATE_HOURS_DEFAULT,
    now: str | None = None,
) -> dict[str, Any]:
    """IMP-046：检查 token 是否到期，到期则轮换。

    返回 ``{"rotated": bool, "token": str, "createdAt": str | None}``。
    若 token 文件不存在，调用 :func:`ensure_token` 颁发。
    若 ``createdAt`` 缺失但 token 有效，补写 ``createdAt`` 且不视为轮换。

    ``now`` 用于测试注入；默认取当前时间。
    """
    meta = read_token_metadata(workspace)
    current_token = meta["token"]
    created_at = meta["createdAt"]
    now_str = now or _now_iso()

    # token 不存在 -> 颁发
    if not current_token:
        new_token = ensure_token(workspace)
        return {"rotated": True, "token": new_token, "createdAt": _now_iso()}

    # createdAt 缺失 -> 补写但不换 token（避免旧文件触发无谓轮换）
    if not created_at:
        _write_token(workspace, current_token)
        return {"rotated": False, "token": current_token, "createdAt": _now_iso()}

    # 到期检查
    if should_rotate_token(created_at, now=now_str, hours=hours):
        new_token = rotate_token(workspace)
        log.info("管理页 token 已自动轮换（上次颁发：%s）", created_at)
        return {"rotated": True, "token": new_token, "createdAt": _now_iso()}

    return {"rotated": False, "token": current_token, "createdAt": created_at}


def _verify_token(workspace: Workspace, candidate: str | None) -> bool:
    """常数时间比较 token。

    BUG-281：``hmac.compare_digest`` 对含非 ASCII 的 ``str`` 会抛 ``TypeError``；
    统一转为 UTF-8 bytes 再比较，非法/不可比候选一律视为未通过（调用方→401）。
    """
    expected = read_token(workspace)
    if not expected:
        return False
    if not candidate or not isinstance(candidate, str):
        return False
    try:
        left = expected.encode("utf-8")
        right = candidate.encode("utf-8")
    except (AttributeError, TypeError, UnicodeEncodeError):
        return False
    try:
        return hmac.compare_digest(left, right)
    except (TypeError, ValueError):
        return False


def _now_iso() -> str:
    from local_webpage_access.logging import now_iso

    return now_iso()


# ---- 错误响应（WBS-22.11）---------------------------------------------------

# 错误码 → HTTP 状态
_ERROR_STATUS: dict[str, int] = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "bad_request": status.HTTP_400_BAD_REQUEST,
    "conflict": status.HTTP_409_CONFLICT,
    "data_nonempty": status.HTTP_409_CONFLICT,  # IMP-035：purge 非空 data/
    "unauthorized": status.HTTP_401_UNAUTHORIZED,
    "loopback_required": status.HTTP_403_FORBIDDEN,  # IMP-051
    "cancelled": status.HTTP_400_BAD_REQUEST,  # IMP-051 pick-directory
    "unavailable": status.HTTP_400_BAD_REQUEST,
    "timeout": status.HTTP_400_BAD_REQUEST,
    "service_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "internal": status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def error_response(
    code: str, message: str, *, status_code: int | None = None, detail: Any = None
) -> JSONResponse:
    """构造统一错误 JSON 响应（WBS-22.11）。"""
    http_status = (
        status_code
        if status_code is not None
        else _ERROR_STATUS.get(code, status.HTTP_500_INTERNAL_SERVER_ERROR)
    )
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if detail is not None:
        body["error"]["detail"] = detail
    return JSONResponse(status_code=http_status, content=body)


# ---- app 上下文 -------------------------------------------------------------


class _Ctx:
    """请求级别的共享上下文（从 ``app.state`` 取）。"""

    def __init__(self, app: FastAPI) -> None:
        self.workspace: Workspace = app.state.workspace
        self.config: Config = app.state.config
        self.registry: Registry = app.state.registry

    @classmethod
    def from_request(cls, request: Request) -> _Ctx:
        return cls(request.app)


# ---- token 依赖 -------------------------------------------------------------

_LOCALHOST_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _normalize_client_host(host: str) -> str:
    """剥除 IPv4-mapped IPv6 的 ``::ffff:`` 前缀（BUG-194）。

    双栈（``::``）监听下，IPv4 回环连接的 client.host 形如 ``::ffff:127.0.0.1``，
    直接比对 ``_LOCALHOST_HOSTS`` 永不命中；剥前缀后与 IPv4 回环统一判定。
    """
    if host.startswith("::ffff:"):
        return host[len("::ffff:") :]
    return host


def _is_loopback_host(host: str) -> bool:
    """host 是否为本机回环（含 IPv4-mapped IPv6 与整个 ``127.0.0.0/8``，BUG-194）。

    ``ipaddress`` 的 ``is_loopback`` 已正确识别 ``::ffff:127.0.0.1``、``::1``、
    ``127.x.x.x`` 全段；字面量 ``localhost`` 单独兜底。
    """
    if host in _LOCALHOST_HOSTS or _normalize_client_host(host) in _LOCALHOST_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_self_connection(request: Request, config: Config) -> bool:
    """请求是否为 manager 经自身绑定地址的自连（managerHost=LAN IP 时探活，BUG-194）。

    ``managerHost`` 配具体 LAN IP 时，manager_service 必须经该地址探活（绑定未覆盖
    回环）。本机连自己的 LAN IP 时连接源地址等于绑定地址；局域网他机源 IP 不同，
    故比对相等即可识别"manager 探自己"，安全地返回 workspaceRoot 做归属校验。
    通配绑定（``0.0.0.0``/``::``）由 :func:`_is_localhost_client` 覆盖，此处跳过。
    """
    client = request.client
    if client is None:
        return False
    bind = getattr(config, "managerHost", "") or ""
    if not bind or bind in {"0.0.0.0", "::"}:
        return False
    return _normalize_client_host(client.host) == _normalize_client_host(bind)


def _is_localhost_client(request: Request) -> bool:
    """请求是否来自本机 loopback（IMP-003：本机免 token）。"""
    client = request.client
    if client is None:
        return False
    return _is_loopback_host(client.host)


def require_token(request: Request) -> None:
    """校验 API token（WBS-22.12）。

    支持三种传递方式：``Authorization: Bearer <token>``、
    ``X-LWA-Token`` 头、``?token=`` 查询参数。

    ``?token=`` 为便利通道（管理页新标签等），会进入浏览器历史与可能的
    访问日志；日常请求优先使用 Header。详见 docs/manager-page.md。

    从 ``127.0.0.1`` / ``localhost`` / ``::1`` 访问时跳过鉴权（IMP-003），
    便于本机调试；局域网 IP 访问仍必须携带有效 token。
    """
    is_local = _is_localhost_client(request)
    request_method = getattr(request, "method", "GET")
    if not isinstance(request_method, str):
        request_method = "GET"
    if is_local and request_method.upper() in {"GET", "HEAD", "OPTIONS"}:
        return
    workspace: Workspace = request.app.state.workspace
    candidate = _extract_token(request)
    if _verify_token(workspace, candidate):
        return
    if is_local:
        fetch_site = request.headers.get("sec-fetch-site", "").lower()
        if fetch_site == "same-origin":
            return
        origin = request.headers.get("origin", "").rstrip("/")
        expected_origin = f"{request.url.scheme}://{request.url.netloc}".rstrip("/")
        if origin and origin == expected_origin:
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": {
                    "code": "csrf_forbidden",
                    "message": "回环地址的非同源写请求需要 API token",
                }
            },
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": {"code": "unauthorized", "message": "API token 无效或缺失"}},
    )


def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    x_token = request.headers.get("x-lwa-token")
    if x_token:
        return x_token.strip() or None
    qp = request.query_params.get("token")
    if qp:
        return qp.strip() or None
    return None


# ---- app 工厂 ---------------------------------------------------------------


def create_app(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    *,
    token: str,
) -> FastAPI:
    """构建管理页 FastAPI 应用（WBS-22.01）。"""
    from local_webpage_access.capability import (
        collect_capability_report,
        log_capability_probe,
        read_capability_health_fragment,
        write_capability_cache,
    )
    from local_webpage_access.logging import setup_logging
    from local_webpage_access.version_info import bind_process_version

    setup_logging(level=config.logLevel)  # type: ignore[arg-type]
    # BUG-451：应用构建时固定版本；/api/health 与 OpenAPI version 共用，不随
    # 长驻进程内后续 git/元数据变化「静默变版」。
    app_version = bind_process_version()

    # create_app 内共享状态：供 lifespan / refresh 闭包使用（单飞锁等）。
    import threading as _threading

    app_state_holder: dict[str, Any] = {
        "refresh_cond": _threading.Condition(),
        "refresh_inflight": False,
        "refresh_result": None,
        "refresh_error": None,
        "last_error_log_at": float("-inf"),
    }

    def _refresh_capability_cache() -> dict[str, Any]:
        """后台探测并刷新 capability-manager.json / 内存片段（BUG-254）。

        BUG-271：``include_backend_cached=True``，与 CLI/doctor 及「完整能力报告/
        刷新」契约一致，合并 daemon/gateway 缓存字段；manager 自身仍以实时探测为准。

        BUG-379：进程内单飞——周期线程与 ``/api/capability?refresh=true`` 并发时
        只跑一次探测，等待方共享同一结果，避免交错写缓存。
        """
        lock: _threading.Condition = app_state_holder["refresh_cond"]
        with lock:
            if app_state_holder["refresh_inflight"]:
                while app_state_holder["refresh_inflight"]:
                    lock.wait(timeout=120.0)
                exc = app_state_holder.get("refresh_error")
                if exc is not None:
                    raise exc
                result = app_state_holder.get("refresh_result")
                if not isinstance(result, dict):
                    raise RuntimeError("capability refresh finished without result")
                return dict(result)
            app_state_holder["refresh_inflight"] = True
            app_state_holder["refresh_result"] = None
            app_state_holder["refresh_error"] = None
        try:
            report = collect_capability_report(
                workspace_root=workspace.root,
                role="manager",
                config_profile=getattr(config, "profile", None),
                include_backend_cached=True,
            )
            level = "WARNING" if report.docker_access == "permission_denied" else "INFO"
            log_capability_probe("manager", report, level=level)
            write_capability_cache(workspace.root, "manager", report)
            frag = report.to_health_fragment()
            with lock:
                app_state_holder["refresh_result"] = frag
            return frag
        except Exception as exc:
            with lock:
                app_state_holder["refresh_error"] = exc
            raise
        finally:
            with lock:
                app_state_holder["refresh_inflight"] = False
                lock.notify_all()

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # BUG-254：能力探测放到后台，保证 /api/health 立即可用、不阻塞 start 轮询。
        # BUG-379：启动后周期刷新完整能力；停止事件在 lifespan 退出时置位收尾。
        import time as _time

        stop_event = _threading.Event()
        app.state.capability_stop_event = stop_event

        def _mark_probe_failed(exc: BaseException, *, force_log: bool = False) -> None:
            """探测异常：不覆盖磁盘快照；Full/内存片段不得无限保留旧 ready。"""
            prev = getattr(app.state, "capability_fragment", None) or {}
            frag = dict(prev) if isinstance(prev, dict) else {}
            frag["overall"] = "unknown"
            frag["action"] = (
                "capability_probe_failed；请查 manager.log 或 GET /api/capability?refresh=true"
            )
            frag["probeError"] = str(exc)[:200]
            caps = dict(frag.get("capabilities") or {})
            # 保留上次字段供排障对照，但 overall 已 unknown，避免假绿。
            frag["capabilities"] = caps
            app.state.capability_fragment = frag
            now = _time.monotonic()
            if force_log or (
                now - float(app_state_holder["last_error_log_at"]) >= _CAPABILITY_ERROR_LOG_INTERVAL
            ):
                app_state_holder["last_error_log_at"] = now
                log.exception("manager 能力自检失败")

        def _bg_probe() -> None:
            delay = _CAPABILITY_INITIAL_DELAY
            while True:
                try:
                    frag = _refresh_capability_cache()
                    app.state.capability_fragment = frag
                except Exception as exc:  # noqa: BLE001 — 探测失败不阻断管理页
                    _mark_probe_failed(exc)
                # BUG-277：首次延后等待 gateway 就绪；之后按间隔周期刷新。
                if stop_event.wait(timeout=delay):
                    break
                delay = _CAPABILITY_REFRESH_INTERVAL

        probe_thread = _threading.Thread(target=_bg_probe, name="lwa-capability-probe", daemon=True)
        probe_thread.start()
        app.state.capability_probe_thread = probe_thread

        # IMP-046：Token 7×24h 自动轮换。
        # BUG-446：yield 前主线程同步 maybe_rotate，避免冷启动窗口打印/使用已失效 token。
        # 046.05：后台 tick 守护线程，默认每 30 min 检查一次。
        # 046.06：_verify_token 每次请求读盘，轮换后新 token 立即生效、旧 token 立即 401。
        rotate_hours = (
            getattr(config, "managerTokenRotateHours", TOKEN_ROTATE_HOURS_DEFAULT)
            or TOKEN_ROTATE_HOURS_DEFAULT
        )
        try:
            maybe_rotate_token(workspace, hours=rotate_hours)
        except Exception:  # noqa: BLE001 - 轮换失败不阻断管理页
            log.warning("启动时 token 轮换检查失败", exc_info=True)

        token_stop_event = _threading.Event()
        app.state.token_rotate_stop_event = token_stop_event

        def _bg_token_rotate() -> None:
            while True:
                if token_stop_event.wait(timeout=_TOKEN_ROTATE_TICK_INTERVAL):
                    break
                try:
                    maybe_rotate_token(workspace, hours=rotate_hours)
                except Exception:  # noqa: BLE001
                    log.warning("周期 token 轮换检查失败", exc_info=True)

        token_thread = _threading.Thread(
            target=_bg_token_rotate, name="lwa-token-rotate", daemon=True
        )
        token_thread.start()
        app.state.token_rotate_thread = token_thread

        try:
            yield
        finally:
            stop_event.set()
            probe_thread.join(timeout=_CAPABILITY_STOP_JOIN_TIMEOUT)
            token_stop_event.set()
            token_thread.join(timeout=2.0)
            with _suppress_close():
                registry.close()

    app = FastAPI(
        title="Local Webpage Access Manager",
        version=app_version,
        lifespan=_lifespan,
        # 统一错误响应由下方异常处理器实现
    )
    app.state.workspace = workspace
    app.state.config = config
    app.state.registry = registry
    app.state.token = token
    app.state.app_version = app_version  # BUG-451：供 /api/health 闭包外读取
    app.state.pageview_store = None  # IMP-024：懒加载的 PageviewStore 单例
    # BUG-254：health 只读缓存；优先已有 capability-manager.json，否则 unknown 占位
    app.state.capability_fragment = read_capability_health_fragment(workspace.root) or {
        "profile": getattr(config, "profile", None) or "default",
        "overall": "unknown",
        "serviceUser": None,
        "capabilities": {},
        "action": None,
    }
    app.state.refresh_capability_cache = _refresh_capability_cache

    # ---- 异常处理器：LwaError → 统一错误格式 ----
    @app.exception_handler(LwaError)
    async def _handle_lwa_error(request: Request, exc: LwaError):  # noqa: ARG001
        code = _lwa_error_code(exc)
        return error_response(code, exc.message or str(exc), detail=exc.context or None)

    @app.exception_handler(HTTPException)
    async def _handle_http_exception(request: Request, exc: HTTPException):  # noqa: ARG001
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        return error_response(
            "bad_request" if exc.status_code < 500 else "internal",
            str(exc.detail),
            status_code=exc.status_code,
        )

    _register_routes(app)

    # 静态资源（管理页前端，WBS-22.02 / WBS-23）
    _mount_static(app)
    return app


def _register_routes(app: FastAPI) -> None:
    """注册所有 ``/api/*`` 路由。"""
    from local_webpage_access.models import Status
    from local_webpage_access.status import (
        all_statuses,
        instance_status,
        sync_status,
    )
    from local_webpage_access.stats import (
        host_resources,
        instance_resources,
    )

    api = Depends(require_token)

    # ---- /api/health（无鉴权存活探测；能力细节对本机或已鉴权客户端开放）----
    @app.get("/api/health", tags=["health"])
    def health(request: Request) -> dict[str, Any]:
        ws: Workspace = app.state.workspace
        body: dict[str, Any] = {
            "ok": True,
            "version": getattr(app.state, "app_version", None) or _app_version(),
        }
        # BUG-169：workspaceRoot 仅回环可见；局域网免鉴权客户端不得窥探绝对路径。
        # BUG-194：manager 从本机探活需拿 workspaceRoot 做归属校验。除回环外，
        # managerHost 配 LAN IP / 双栈 :: 时，本机经该地址自连的 client.host 等于
        # 绑定地址（或呈 ::ffff:127.0.0.1），也算可信——仅本机自连能命中，局域网他机源
        # IP 不同，不会泄露。
        local_ok = _is_localhost_client(request) or _is_self_connection(request, app.state.config)
        if local_ok:
            body["workspaceRoot"] = str(ws.root.resolve())
        # BUG-236：已鉴权局域网客户端也应拿到完整 capabilities（供管理页降级 UI）
        auth_ok = local_ok or _verify_token(ws, _extract_token(request))
        # BUG-254：存活检查不得同步跑昂贵 Docker/Caddy 探测；只用启动/后台缓存。
        # BUG-277：廉价合并新鲜 gateway 缓存，纠偏 manager 早于 gateway 就绪的陈旧片段。
        from local_webpage_access.capability import overlay_gateway_access_from_cache

        frag = overlay_gateway_access_from_cache(
            getattr(app.state, "capability_fragment", None),
            ws.root,
        )
        if auth_ok:
            body.update(frag)
        else:
            body["profile"] = frag.get("profile")
            body["overall"] = frag.get("overall")
        return body

    # ---- /api/capability（鉴权；按需刷新完整能力报告）----
    @app.get("/api/capability", dependencies=[api], tags=["health"])
    def capability_report(refresh: bool = Query(False)) -> dict[str, Any]:
        """返回能力报告；``refresh=true`` 时同步重探并更新缓存。"""
        ws: Workspace = app.state.workspace
        cfg: Config = app.state.config
        if refresh:
            refresh_fn = getattr(app.state, "refresh_capability_cache", None)
            if callable(refresh_fn):
                try:
                    frag = refresh_fn()
                    app.state.capability_fragment = frag
                except Exception as exc:  # noqa: BLE001
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail={
                            "error": {
                                "code": "capability_probe_failed",
                                "message": f"能力探测失败：{exc}",
                            }
                        },
                    ) from exc
                return {"ok": True, **frag}
        frag = getattr(app.state, "capability_fragment", None)
        if frag:
            return {"ok": True, **frag}
        from local_webpage_access.capability import collect_capability_report

        report = collect_capability_report(
            workspace_root=ws.root,
            role="manager",
            config_profile=getattr(cfg, "profile", None),
        )
        return {"ok": True, **report.to_health_fragment()}

    # ---- 顶部统计（WBS-22.05/06）----
    @app.get("/api/stats", dependencies=[api], tags=["stats"])
    def get_stats() -> dict[str, Any]:
        ctx = _Ctx(app)
        reg = ctx.registry
        sync_status(ctx.workspace, ctx.config, ctx.registry)
        counts = reg.status_counts()
        # 类型分布
        rows = reg.list_instances()
        type_dist: dict[str, int] = {}
        db_count = 0
        for row in rows:
            kind = str(row.get("kind") or "unknown")
            type_dist[kind] = type_dist.get(kind, 0) + 1
            if row.get("has_database"):
                db_count += 1
        host = host_resources(root=ctx.workspace.root)
        return {
            "counts": {
                "total": len(rows),
                Status.RUNNING.value: counts.get(Status.RUNNING.value, 0),
                Status.STOPPED.value: counts.get(Status.STOPPED.value, 0),
                Status.PENDING.value: counts.get(Status.PENDING.value, 0),
                Status.FAILED.value: counts.get(Status.FAILED.value, 0),
                Status.BUILDING.value: counts.get(Status.BUILDING.value, 0),
                # DEV-043：可恢复的异常态也纳入统计（BUG-081）
                Status.GATEWAY_DOWN.value: counts.get(Status.GATEWAY_DOWN.value, 0),
                Status.CONFIG_INVALID.value: counts.get(Status.CONFIG_INVALID.value, 0),
            },
            "typeDistribution": type_dist,
            "databaseCount": db_count,
            "portPool": _port_pool_summary(ctx),
            "host": host.to_dict(),
        }

    # ---- 实例列表（WBS-22.03）----
    @app.get("/api/instances", dependencies=[api], tags=["instances"])
    def list_instances() -> dict[str, Any]:
        from local_webpage_access.access_workflow import maybe_throttled_lan_refresh
        from local_webpage_access.lifecycle import list_redundant_instances

        ctx = _Ctx(app)
        # 先观测回写，再取快照（状态尽量新鲜）
        sync_status(ctx.workspace, ctx.config, ctx.registry)
        # IMP-040 R2/R3：旁路漂移检测 + 节流落盘（读时合成由 status 负责）
        try:
            maybe_throttled_lan_refresh(ctx.workspace, ctx.config, ctx.registry)
        except Exception:  # noqa: BLE001 — 旁路失败不阻断列表
            log.debug("列表旁路 LAN refresh 失败", exc_info=True)
        statuses = all_statuses(ctx.workspace, ctx.config, ctx.registry)
        # IMP-019（WBS-22.13）：标注冗余实例（同 zip 指纹分组中非最早者），
        # 前端据此显示冗余徽章 / 黄色边框 / 行内删除。
        redundant_ids = {r["id"] for r in list_redundant_instances(ctx.workspace, ctx.registry)}
        items: list[dict[str, Any]] = []
        for snap in statuses:
            data = snap.to_dict()
            data["redundant"] = snap.id in redundant_ids
            items.append(data)
        return {"instances": items}

    # ---- IMP-040：显式刷新访问地址（薄封装 refresh_network_entries）----
    @app.post("/api/access/refresh", dependencies=[api], tags=["access"])
    def refresh_access_urls() -> dict[str, Any]:
        from local_webpage_access.access import refresh_network_entries

        ctx = _Ctx(app)
        report = refresh_network_entries(ctx.workspace, ctx.config, ctx.registry)
        return {"ok": True, **report.to_dict()}

    # ---- IMP-037：网关后端原子切换（薄封装 switch_gateway）----
    @app.post("/api/gateway/switch", dependencies=[api], tags=["gateway"])
    def switch_gateway_backend(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        from local_webpage_access.gateway_switch import switch_gateway

        ctx = _Ctx(app)
        backend = str(payload.get("backend") or payload.get("target") or "").strip()
        if not backend:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": {
                        "code": "GATEWAY_BACKEND_INVALID",
                        "message": "缺少 backend（caddy|builtin）",
                    }
                },
            )
        dry_run = bool(payload.get("dryRun", False))
        review = bool(payload.get("review", True))
        result = switch_gateway(
            ctx.workspace,
            ctx.config,
            ctx.registry,
            backend,
            dry_run=dry_run,
            review=review,
        )
        body = result.to_dict()
        if not result.ok:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": {
                        "code": "gateway_switch_failed",
                        "message": result.error or "网关切换失败",
                        "result": body,
                    }
                },
            )
        return body

    # ---- 实例详情（WBS-22.04）----
    @app.get("/api/instances/{instance_id}", dependencies=[api], tags=["instances"])
    def get_instance(instance_id: str) -> dict[str, Any]:
        ctx = _Ctx(app)
        _require_instance(ctx, instance_id)
        sync_status(ctx.workspace, ctx.config, ctx.registry, instance_id)
        snap = instance_status(ctx.workspace, ctx.config, ctx.registry, instance_id)
        manifest = _load_manifest_dict(ctx.workspace, instance_id)
        builds = ctx.registry.list_builds(instance_id, limit=20)
        events = ctx.registry.list_events(instance_id, limit=30)
        resources = ctx.registry.get_resources(instance_id)
        return {
            "instance": snap.to_dict(),
            "manifest": manifest,
            "builds": [_camelize_keys(row) for row in builds],
            "events": [_camelize_keys(row) for row in events],
            "resources": _camelize_keys(resources) if resources else None,
        }

    # ---- 日志（WBS-22.07）----
    @app.get(
        "/api/instances/{instance_id}/logs",
        dependencies=[api],
        tags=["instances"],
    )
    def get_logs(
        instance_id: str,
        category: str = Query("run", description="build/run/gateway/import/scan"),
        tail: int = Query(200, ge=1, le=5000),
    ) -> dict[str, Any]:
        ctx = _Ctx(app)
        _require_instance(ctx, instance_id)
        from local_webpage_access.logs import list_logs, read_log

        text = read_log(ctx.workspace, instance_id, category, tail=tail)
        return {
            "instanceId": instance_id,
            "category": category,
            "available": [i.category for i in list_logs(ctx.workspace, instance_id)],
            "content": text or "",
        }

    # ---- 资源（WBS-22.06）----
    @app.get(
        "/api/instances/{instance_id}/resources",
        dependencies=[api],
        tags=["instances"],
    )
    def get_resources(instance_id: str) -> dict[str, Any]:
        ctx = _Ctx(app)
        _require_instance(ctx, instance_id)
        info = instance_resources(ctx.workspace, ctx.config, ctx.registry, instance_id)
        return {"instanceId": instance_id, "resources": info.to_dict()}

    # ---- 操作（WBS-22.08）----
    def _docker_ops_blocked_reason(ctx: _Ctx, instance_id: str) -> str | None:
        """BUG-237：容器生命周期在 Docker 能力降级时服务端 fail-closed。

        返回阻断原因字符串；允许操作时返回 None。静态实例不受 Docker 权限影响。
        """
        row = ctx.registry.get_instance(instance_id) or {}
        runtime = str(row.get("runtime") or "")
        serving = str(row.get("serving_mode") or row.get("servingMode") or "")
        is_container = runtime in ("docker-compose", "container") or serving == "container"
        if not is_container:
            # 再看 registry runtime_access：若曾观测到权限失败，仍阻断容器类操作
            # 但静态站点不走此路径
            return None
        try:
            from local_webpage_access.capability import collect_capability_report

            report = collect_capability_report(
                workspace_root=ctx.workspace.root,
                role="manager",
                config_profile=getattr(ctx.config, "profile", None),
            )
            caps = report.to_dict()["capabilities"]
            if caps.get("sessionRefreshRequired"):
                return "sessionRefreshRequired：请重新登录后执行 lwa setup --full --resume"
            for key, label in (
                ("managerDockerAccess", "manager"),
                ("dockerAccess", "docker"),
            ):
                if caps.get(key) == "permission_denied":
                    return f"{label} 无 Docker 权限（{key}=permission_denied）"
        except Exception:  # noqa: BLE001 — 探测失败时 fail-closed
            return "无法确认 Docker 能力，拒绝容器操作"
        return None

    def _lifecycle_op(
        instance_id: str,
        op: Callable[..., Any],
        *,
        label: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        ctx = _Ctx(app)
        _require_instance(ctx, instance_id)
        # IMP-039：cancelling 期间禁止其它生命周期操作（取消本身走独立路由）
        row = ctx.registry.get_instance(instance_id)
        if row and str(row.get("status") or "") == Status.CANCELLING.value:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": {
                        "code": "cancelling",
                        "message": f"实例正在取消构建，暂时不能 {label}",
                    }
                },
            )
        blocked = _docker_ops_blocked_reason(ctx, instance_id)
        if blocked is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": {
                        "code": "capability_denied",
                        "message": f"拒绝 {label}：{blocked}",
                    }
                },
            )
        op(ctx.workspace, ctx.config, ctx.registry, instance_id, **kwargs)
        sync_status(ctx.workspace, ctx.config, ctx.registry, instance_id)
        snap = instance_status(ctx.workspace, ctx.config, ctx.registry, instance_id)
        return {"instanceId": instance_id, "action": label, "instance": snap.to_dict()}

    @app.post("/api/instances/{instance_id}/start", dependencies=[api], tags=["instances"])
    def start_op(
        instance_id: str,
        fallback_policy: str = Query(
            "confirm",
            description="降级策略：confirm（默认）/ auto-equivalent / disabled",
        ),
    ) -> dict[str, Any]:
        """启动实例。

        C.R03：当 top-1 候选失败且存在等价 fallback 时：

        - ``fallback_policy=confirm``（默认）：返回 ``pendingConfirmation`` 响应，
          前端展示后用户选择降级则调用 ``/confirm-fallback`` 重试。
        - ``fallback_policy=auto-equivalent``：自动降级（需显式指定）。
        - ``fallback_policy=disabled``：不降级，直接返回失败。
        """
        from local_webpage_access.lifecycle import (
            FallbackConfirmationRequired,
            start_instance,
        )

        try:
            return _lifecycle_op(
                instance_id,
                start_instance,
                label="start",
                fallback_policy=fallback_policy,
            )
        except FallbackConfirmationRequired as fcr:
            # C.R03：返回结构化待确认响应（非错误，而是需要用户决策）
            return {
                "instanceId": instance_id,
                "action": "start",
                "pendingConfirmation": True,
                "primaryFailure": fcr.primary_failure,
                "equivalentCandidates": fcr.equivalent_candidates,
                "hint": ("调用 /api/instances/{instance_id}/confirm-fallback 以确认降级到等价候选"),
            }

    @app.post(
        "/api/instances/{instance_id}/confirm-fallback",
        dependencies=[api],
        tags=["instances"],
    )
    def confirm_fallback_op(instance_id: str) -> dict[str, Any]:
        """C.R03：确认降级到等价 fallback 候选。

        用户在管理页确认后调用此端点，以 ``auto-equivalent`` 策略重试启动。
        幂等性：若实例已在 fallback 后运行，直接返回当前状态。
        """
        from local_webpage_access.lifecycle import start_instance

        return _lifecycle_op(
            instance_id,
            start_instance,
            label="start",
            fallback_policy="auto-equivalent",
        )

    @app.post("/api/instances/{instance_id}/stop", dependencies=[api], tags=["instances"])
    def stop_op(instance_id: str) -> dict[str, Any]:
        from local_webpage_access.lifecycle import stop_instance_op

        return _lifecycle_op(instance_id, stop_instance_op, label="stop")

    @app.post(
        "/api/instances/{instance_id}/restart",
        dependencies=[api],
        tags=["instances"],
    )
    def restart_op(instance_id: str) -> dict[str, Any]:
        from local_webpage_access.lifecycle import restart_instance

        return _lifecycle_op(instance_id, restart_instance, label="restart")

    @app.post(
        "/api/instances/{instance_id}/rebuild",
        dependencies=[api],
        tags=["instances"],
    )
    def rebuild_op(instance_id: str) -> dict[str, Any]:
        from local_webpage_access.lifecycle import rebuild_instance

        return _lifecycle_op(instance_id, rebuild_instance, label="rebuild")

    @app.post(
        "/api/instances/{instance_id}/cancel-build",
        dependencies=[api],
        tags=["instances"],
    )
    def cancel_build_op(instance_id: str) -> dict[str, Any]:
        """IMP-039：取消排队中或进行中的构建。

        返回结构化 ``outcome``（cancelled / cancel_failed / noop / already_done），
        不在仅收到请求时假报成功。
        """
        from local_webpage_access.lifecycle import cancel_build

        ctx = _Ctx(app)
        _require_instance(ctx, instance_id)
        result = cancel_build(ctx.workspace, ctx.config, ctx.registry, instance_id)
        sync_status(ctx.workspace, ctx.config, ctx.registry, instance_id)
        snap = instance_status(ctx.workspace, ctx.config, ctx.registry, instance_id)
        outcome = getattr(result, "outcome", "unknown")
        payload = {
            "instanceId": instance_id,
            "action": "cancel-build",
            "outcome": outcome,
            "message": getattr(result, "message", "") or "",
            "previousStatus": getattr(result, "previous_status", None),
            "instance": snap.to_dict(),
        }
        if outcome == "cancel_failed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": {
                        "code": "cancel_failed",
                        "message": payload["message"] or "构建取消失败",
                        "outcome": outcome,
                    }
                },
            )
        return payload

    @app.post(
        "/api/instances/{instance_id}/recover",
        dependencies=[api],
        tags=["instances"],
    )
    def recover_op(instance_id: str) -> dict[str, Any]:
        """DEV-043：恢复 gateway_down / config_invalid / 掉线实例（一键 recover）。

        静态实例先尝试拉起 Caddy master，再 restart 重新托管；容器实例等价 restart。
        """
        from local_webpage_access.lifecycle import recover_instance

        return _lifecycle_op(instance_id, recover_instance, label="recover")

    @app.post(
        "/api/instances/{instance_id}/remove",
        dependencies=[api],
        tags=["instances"],
    )
    def remove_op(
        instance_id: str,
        purge: bool = Query(False, description="同时删除实例磁盘数据"),
        force: bool = Query(False, description="强制移除（跳过 data/ 非空检查）"),
        payload: dict[str, Any] = Body(default={}),
    ) -> dict[str, Any]:
        """IMP-019：移除单个实例（管理页行内删除 / 冗余清理）。

        默认仅停服 + 清 registry，保留 ``apps/<id>/``；``purge=true`` 额外删磁盘，
        data/ 非空时须同时 ``force=true``。
        """
        from local_webpage_access.lifecycle import remove_instance

        ctx = _Ctx(app)
        _require_instance(ctx, instance_id)
        row = ctx.registry.get_instance(instance_id) or {}
        if row.get("status") == Status.CANCELLING.value:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": {
                        "code": "cancelling",
                        "message": "实例正在取消构建，暂时不能 remove",
                    }
                },
            )
        if (purge or force) and str(payload.get("confirmId") or "") != instance_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": {
                        "code": "confirmation_required",
                        "message": "彻底删除必须提交与实例 ID 完全相同的 confirmId",
                    }
                },
            )
        try:
            remove_instance(
                ctx.workspace,
                ctx.config,
                ctx.registry,
                instance_id,
                purge=purge,
                force=force,
            )
        except LwaError as exc:
            code = _lwa_error_code(exc)
            http_status = _ERROR_STATUS.get(code, status.HTTP_500_INTERNAL_SERVER_ERROR)
            # IMP-041：破坏性 API 审计（无 token）
            log.info(
                "audit remove instance=%s purge=%s force=%s status=%s code=%s",
                instance_id,
                str(purge).lower(),
                str(force).lower(),
                http_status,
                code,
            )
            raise
        log.info(
            "audit remove instance=%s purge=%s force=%s status=200 code=ok",
            instance_id,
            str(purge).lower(),
            str(force).lower(),
        )
        return {
            "instanceId": instance_id,
            "action": "remove",
            "purge": purge,
            "force": force,
        }

    @app.post(
        "/api/instances/{instance_id}/update",
        dependencies=[api],
        tags=["instances"],
    )
    def update_op(instance_id: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """IMP-009：用新 zip 原地更新实例（保留 id/hostPort/data/）。

        Body: ``{"zipPath": "inbox/foo.zip", "restart": true, "keepData": true,
        "forceKindChange": false}``。``zipPath`` 相对路径以 ``inbox/`` 为根。
        """
        from local_webpage_access.importer import Importer

        ctx = _Ctx(app)
        _require_instance(ctx, instance_id)
        row = ctx.registry.get_instance(instance_id) or {}
        if row.get("status") == Status.CANCELLING.value:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": {
                        "code": "cancelling",
                        "message": "实例正在取消构建，暂时不能 update",
                    }
                },
            )

        raw_zip = str(payload.get("zipPath") or "").strip()
        if not raw_zip:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": {
                        "code": "bad_request",
                        "message": "缺少 zipPath",
                    }
                },
            )
        zip_path = Path(raw_zip)
        if not zip_path.is_absolute():
            # 相对路径以 inbox/ 为根（管理页选择 inbox 内文件）
            zip_path = ctx.workspace.inbox / raw_zip
        zip_path = zip_path.resolve()
        inbox_root = ctx.workspace.inbox.resolve()
        try:
            zip_path.relative_to(inbox_root)
        except ValueError as exc:
            raise PathError(
                "zipPath 必须位于工作区 inbox/ 目录内",
                path=str(zip_path),
                inbox=str(inbox_root),
            ) from exc

        restart = bool(payload.get("restart", True))
        keep_data = bool(payload.get("keepData", True))
        force_kind_change = bool(payload.get("forceKindChange", False))

        importer = Importer(ctx.workspace, ctx.config, ctx.registry)
        result = importer.update_zip(
            zip_path,
            instance_id,
            restart=restart,
            keep_data=keep_data,
            yes=True,  # API 路径非交互
            dry_run=False,
            force_kind_change=force_kind_change,
        )

        restarted = False
        rebuilt_runtime = False
        if result.needs_rebuild:
            from local_webpage_access.lifecycle import rebuild_instance

            rebuild_instance(ctx.workspace, ctx.config, ctx.registry, instance_id)
            rebuilt_runtime = True
        elif result.needs_restart:
            from local_webpage_access.lifecycle import restart_instance

            restart_instance(ctx.workspace, ctx.config, ctx.registry, instance_id)
            restarted = True

        sync_status(ctx.workspace, ctx.config, ctx.registry, instance_id)
        snap = instance_status(ctx.workspace, ctx.config, ctx.registry, instance_id)
        return {
            "instanceId": instance_id,
            "action": "update",
            "skipped": result.skipped,
            "rebuilt": result.rebuilt,
            "restarted": restarted,
            "rebuiltRuntime": rebuilt_runtime,
            "needsRebuild": result.needs_rebuild,
            "prevHash": result.prev_hash,
            "zipHash": result.zip_hash,
            "instance": snap.to_dict(),
        }

    @app.post(
        "/api/instances/{instance_id}/update-from-dir",
        dependencies=[api],
        tags=["instances"],
    )
    def update_from_dir_op(
        instance_id: str, payload: dict[str, Any] = Body(default={})
    ) -> dict[str, Any]:
        """IMP-047：从关联文件夹源更新实例。

        Body: ``{"restart": true, "keepData": true, "forceKindChange": false}``。
        仅对 ``sourceKind=folder`` 的实例有效；源目录从 manifest 的
        ``sourceDirPath`` 读取。
        """
        from local_webpage_access.importer import Importer

        ctx = _Ctx(app)
        _require_instance(ctx, instance_id)
        row = ctx.registry.get_instance(instance_id) or {}
        if row.get("status") == Status.CANCELLING.value:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": {
                        "code": "cancelling",
                        "message": "实例正在取消构建，暂时不能 update",
                    }
                },
            )

        restart = bool(payload.get("restart", True))
        keep_data = bool(payload.get("keepData", True))
        force_kind_change = bool(payload.get("forceKindChange", False))

        importer = Importer(ctx.workspace, ctx.config, ctx.registry)
        result = importer.update_from_dir(
            instance_id,
            restart=restart,
            keep_data=keep_data,
            yes=True,
            dry_run=False,
            force_kind_change=force_kind_change,
        )

        restarted = False
        rebuilt_runtime = False
        if result.needs_rebuild:
            from local_webpage_access.lifecycle import rebuild_instance

            rebuild_instance(ctx.workspace, ctx.config, ctx.registry, instance_id)
            rebuilt_runtime = True
        elif result.needs_restart:
            from local_webpage_access.lifecycle import restart_instance

            restart_instance(ctx.workspace, ctx.config, ctx.registry, instance_id)
            restarted = True

        sync_status(ctx.workspace, ctx.config, ctx.registry, instance_id)
        snap = instance_status(ctx.workspace, ctx.config, ctx.registry, instance_id)
        return {
            "instanceId": instance_id,
            "action": "update-from-dir",
            "skipped": result.skipped,
            "rebuilt": result.rebuilt,
            "restarted": restarted,
            "rebuiltRuntime": rebuilt_runtime,
            "needsRebuild": result.needs_rebuild,
            "prevHash": result.prev_hash,
            "zipHash": result.zip_hash,
            "instance": snap.to_dict(),
        }

    @app.post(
        "/api/import-from-dir",
        dependencies=[api],
        tags=["instances"],
    )
    def import_from_dir_op(
        payload: dict[str, Any] = Body(default={}),
    ) -> dict[str, Any]:
        """IMP-047：从本机文件夹源导入新实例。

        Body: ``{"sourceDir": "/abs/path/to/src", "name": "可选名",
        "pathAlias": "可选别名"}``。
        """
        from local_webpage_access.importer import Importer

        ctx = _Ctx(app)

        source_dir = str(payload.get("sourceDir") or "").strip()
        if not source_dir:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": {
                        "code": "bad_request",
                        "message": "缺少 sourceDir",
                    }
                },
            )

        name = payload.get("name") or None
        path_alias = payload.get("pathAlias") or None
        if path_alias is not None:
            path_alias = str(path_alias).strip() or None

        importer = Importer(ctx.workspace, ctx.config, ctx.registry)
        try:
            result = importer.import_from_dir(
                source_dir,
                name=name,
                path_alias=path_alias,
                on_conflict="error",
            )
        except LwaError as exc:
            code = _lwa_error_code(exc)
            http_status = _ERROR_STATUS.get(code, status.HTTP_400_BAD_REQUEST)
            raise HTTPException(
                status_code=http_status,
                detail={"error": {"code": code, "message": exc.message or str(exc)}},
            ) from exc

        # 对齐 daemon process_zip：识别成功且档位轻量则自动部署
        from local_webpage_access.daemon import try_auto_start_after_import

        auto_start = try_auto_start_after_import(
            ctx.workspace,
            ctx.config,
            ctx.registry,
            result,
            log_prefix="import-from-dir",
        )

        sync_status(ctx.workspace, ctx.config, ctx.registry, result.instance_id)
        snap = instance_status(ctx.workspace, ctx.config, ctx.registry, result.instance_id)
        return {
            "instanceId": result.instance_id,
            "action": "import-from-dir",
            "autoStart": auto_start,
            "instance": snap.to_dict(),
        }

    @app.post(
        "/api/import-from-git",
        dependencies=[api],
        tags=["instances"],
    )
    def import_from_git_op(
        payload: dict[str, Any] = Body(default={}),
    ) -> dict[str, Any]:
        """IMP-065：从 GitHub 仓库一键导入新实例。

        Body: ``{"url": "https://github.com/<owner>/<repo>", "ref": "可选分支/标签",
        "subdir": "可选子目录", "name": "可选名", "pathAlias": "可选别名"}``。

        错误体 ``error.detail.kind`` 携带闭集 errorKind（``invalid_url`` /
        ``host_not_allowed`` / ``userinfo_forbidden`` / ``git_missing`` /
        ``remote_unreachable`` / ``ref_not_found`` / ``clone_timeout`` /
        ``size_exceeded``）；与 CLI 同契约。并发导入经 ``import_activity``
        闸门排队，每路独立 tempfile staging。**不限 loopback**（065.d）：
        URL 输入无宿主目录浏览攻击面，LAN 侧由 manager token 把关。
        """
        from local_webpage_access.importer import Importer

        ctx = _Ctx(app)

        url = str(payload.get("url") or "").strip()
        # 空 url 交给 parse_github_url → invalid_url；错误体走 LwaError 处理器
        # 的 error.detail.kind（禁止手工 HTTPException 把 kind 放错层）。

        ref = (str(payload.get("ref") or "").strip() or None) if payload.get("ref") else None
        subdir = (str(payload.get("subdir") or "").strip() or None) if payload.get("subdir") else None
        name = payload.get("name") or None
        path_alias = payload.get("pathAlias") or None
        if path_alias is not None:
            path_alias = str(path_alias).strip() or None

        importer = Importer(ctx.workspace, ctx.config, ctx.registry)
        # GitSourceError / ZipImportError 等经全局 LwaError 处理器出统一错误体
        # （error.detail.kind 为闭集 errorKind，前端据此出人话提示）
        result = importer.import_from_git(
            url,
            ref=ref,
            subdir=subdir,
            name=name,
            path_alias=path_alias,
            on_conflict="error",
        )

        # 对齐 daemon / import-from-dir：识别成功且档位轻量则自动部署（065.13）
        from local_webpage_access.daemon import try_auto_start_after_import

        auto_start = try_auto_start_after_import(
            ctx.workspace,
            ctx.config,
            ctx.registry,
            result,
            log_prefix="import-from-git",
        )

        sync_status(ctx.workspace, ctx.config, ctx.registry, result.instance_id)
        snap = instance_status(ctx.workspace, ctx.config, ctx.registry, result.instance_id)
        return {
            "instanceId": result.instance_id,
            "action": "import-from-git",
            "autoStart": auto_start,
            "instance": snap.to_dict(),
        }

    @app.post(
        "/api/instances/{instance_id}/update-from-git",
        dependencies=[api],
        tags=["instances"],
    )
    def update_from_git_op(
        instance_id: str, payload: dict[str, Any] = Body(default={})
    ) -> dict[str, Any]:
        """IMP-065：从 GitHub 远端更新 git 源实例。

        Body: ``{"url": "可选（须与 manifest 一致）", "restart": true,
        "keepData": true, "forceKindChange": false}``。仅对 ``sourceKind=git``
        的实例有效；无 ``url`` 时用 manifest 存储的仓库地址。远端 OID 未变时
        返回 ``skipped=true``（无需更新，零重建）。
        """
        from local_webpage_access.importer import Importer

        ctx = _Ctx(app)
        _require_instance(ctx, instance_id)
        row = ctx.registry.get_instance(instance_id) or {}
        if row.get("status") == Status.CANCELLING.value:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": {
                        "code": "cancelling",
                        "message": "实例正在取消构建，暂时不能 update",
                    }
                },
            )

        url = (str(payload.get("url") or "").strip() or None) if payload.get("url") else None
        restart = bool(payload.get("restart", True))
        keep_data = bool(payload.get("keepData", True))
        force_kind_change = bool(payload.get("forceKindChange", False))

        importer = Importer(ctx.workspace, ctx.config, ctx.registry)
        result = importer.update_from_git(
            instance_id,
            url=url,
            restart=restart,
            keep_data=keep_data,
            yes=True,
            dry_run=False,
            force_kind_change=force_kind_change,
        )

        restarted = False
        rebuilt_runtime = False
        if result.needs_rebuild:
            from local_webpage_access.lifecycle import rebuild_instance

            rebuild_instance(ctx.workspace, ctx.config, ctx.registry, instance_id)
            rebuilt_runtime = True
        elif result.needs_restart:
            from local_webpage_access.lifecycle import restart_instance

            restart_instance(ctx.workspace, ctx.config, ctx.registry, instance_id)
            restarted = True

        sync_status(ctx.workspace, ctx.config, ctx.registry, instance_id)
        snap = instance_status(ctx.workspace, ctx.config, ctx.registry, instance_id)
        return {
            "instanceId": instance_id,
            "action": "update-from-git",
            "skipped": result.skipped,
            "rebuilt": result.rebuilt,
            "restarted": restarted,
            "rebuiltRuntime": rebuilt_runtime,
            "needsRebuild": result.needs_rebuild,
            "prevHash": result.prev_hash,
            "zipHash": result.zip_hash,
            "instance": snap.to_dict(),
        }

    @app.post(
        "/api/pick-directory",
        dependencies=[api],
        tags=["instances"],
    )
    def pick_directory_op(request: Request) -> dict[str, str]:
        """IMP-051：在 LWA 宿主机上打开原生目录选择器。

        仅允许 **loopback** 客户端调用（与管理页按钮门禁一致）。局域网/远程
        即使持有 token 也返回 403 ``loopback_required``，避免对话框在无人值守
        的服务器屏幕上弹出。

        成功：``{"path": "/abs/..."}``。取消/无 GUI/超时：400 + 对应 code。
        """
        if not _is_localhost_client(request):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": {
                        "code": "loopback_required",
                        "message": (
                            "目录选择器仅在本机（127.0.0.1 / ::1）访问管理页时可用；"
                            "请粘贴 LWA 所在机器上的绝对路径，或改用本机地址打开管理页"
                        ),
                    }
                },
            )

        from local_webpage_access.directory_picker import pick_directory

        try:
            path = pick_directory()
        except DirectoryPickerError as exc:
            code = getattr(exc, "code", None) or "bad_request"
            if code not in {"cancelled", "unavailable", "timeout"}:
                code = "bad_request"
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": {
                        "code": code,
                        "message": exc.message or str(exc),
                    }
                },
            ) from exc

        return {"path": path}

    @app.patch(
        "/api/instances/{instance_id}/path-alias",
        dependencies=[api],
        tags=["instances"],
    )
    def path_alias_op(
        instance_id: str,
        payload: dict[str, Any] = Body(default={}),
    ) -> dict[str, Any]:
        """IMP-006：设置或清除路径别名。

        Body: ``{"alias": "my-slug"}`` 或 ``{"alias": null}`` 清除。
        """
        from local_webpage_access.path_alias import set_instance_path_alias

        ctx = _Ctx(app)
        _require_instance(ctx, instance_id)

        if "alias" not in payload:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": {
                        "code": "bad_request",
                        "message": "缺少 alias 字段",
                    }
                },
            )

        raw_alias = payload.get("alias")
        if raw_alias is not None and not isinstance(raw_alias, str):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": {
                        "code": "bad_request",
                        "message": "alias 必须为字符串或 null",
                    }
                },
            )

        alias = raw_alias.strip() if isinstance(raw_alias, str) else None
        if alias == "":
            alias = None

        result = set_instance_path_alias(
            ctx.workspace,
            ctx.config,
            ctx.registry,
            instance_id,
            alias,
        )

        sync_status(ctx.workspace, ctx.config, ctx.registry, instance_id)
        snap = instance_status(ctx.workspace, ctx.config, ctx.registry, instance_id)
        body = result.to_dict()
        body["action"] = "path-alias"
        body["instance"] = snap.to_dict()
        return body

    # ---- pending 列表（WBS-22.09）----
    @app.get("/api/pending", dependencies=[api], tags=["instances"])
    def list_pending() -> dict[str, Any]:
        ctx = _Ctx(app)
        sync_status(ctx.workspace, ctx.config, ctx.registry)
        statuses = all_statuses(ctx.workspace, ctx.config, ctx.registry)
        pending = [s.to_dict() for s in statuses if s.status == Status.PENDING.value]
        failed = [s.to_dict() for s in statuses if s.status == Status.FAILED.value]
        return {"pending": pending, "failed": failed}

    # ---- 端口池（WBS-22.10）----
    @app.get("/api/port-pool", dependencies=[api], tags=["port-pool"])
    def get_port_pool() -> dict[str, Any]:
        ctx = _Ctx(app)
        return {"portPool": _port_pool_summary(ctx)}

    # ---- 浏览量统计（IMP-024 / DEV-061）----
    @app.get("/api/pageviews", dependencies=[api], tags=["pageviews"])
    def get_pageviews() -> dict[str, Any]:
        """IMP-024：所有实例浏览量汇总。惰性摄入最新日志后返回。"""
        from local_webpage_access.pageviews import ingest_all

        ctx = _Ctx(app)
        store = _pageview_store(app)
        try:
            ingest_all(ctx.workspace, ctx.config, ctx.registry, store)
        except Exception as exc:  # noqa: BLE001 — 摄入失败不阻断，返回已聚合数据
            log.debug("浏览量摄入失败：%s", exc)
        return {"instances": store.summary()}

    @app.get(
        "/api/instances/{instance_id}/pageviews",
        dependencies=[api],
        tags=["pageviews"],
    )
    def get_instance_pageviews(
        instance_id: str, limit: int = Query(50, ge=1, le=500)
    ) -> dict[str, Any]:
        """IMP-024：单实例浏览量详情（按天分布 + 最近命中明细）。"""
        from local_webpage_access.pageviews import ingest_all

        ctx = _Ctx(app)
        _require_instance(ctx, instance_id)
        store = _pageview_store(app)
        try:
            ingest_all(ctx.workspace, ctx.config, ctx.registry, store)
        except Exception as exc:  # noqa: BLE001
            log.debug("浏览量摄入失败（%s）：%s", instance_id, exc)
        return store.detail(instance_id, limit=limit)

    # ---- 冗余实例（IMP-019 / WBS-22.13）----
    @app.get("/api/redundant", dependencies=[api], tags=["instances"])
    def list_redundant() -> dict[str, Any]:
        """IMP-019：列出冗余实例（同 zip 指纹分组中非最早者）。

        返回 ``{"instances": [...], "count": N}``，每项含
        ``id`` / ``name`` / ``sourceZipHash`` / ``createdAt``，按 createdAt 升序。
        """
        from local_webpage_access.lifecycle import list_redundant_instances

        ctx = _Ctx(app)
        redundant = list_redundant_instances(ctx.workspace, ctx.registry)
        return {
            "instances": [_camelize_keys(r) for r in redundant],
            "count": len(redundant),
        }

    @app.post("/api/redundant/remove", dependencies=[api], tags=["instances"])
    def remove_redundant_op(
        purge: bool = Query(False, description="同时删除实例磁盘数据"),
        force: bool = Query(False, description="强制移除（跳过 data/ 非空检查）"),
    ) -> dict[str, Any]:
        """IMP-019：批量移除冗余实例（保留每组最早者），返回被移除的 id 列表。"""
        from local_webpage_access.lifecycle import remove_redundant

        ctx = _Ctx(app)
        try:
            removed = remove_redundant(
                ctx.workspace,
                ctx.config,
                ctx.registry,
                purge=purge,
                force=force,
            )
        except LwaError as exc:
            code = _lwa_error_code(exc)
            http_status = _ERROR_STATUS.get(code, status.HTTP_500_INTERNAL_SERVER_ERROR)
            log.info(
                "audit remove-redundant purge=%s force=%s status=%s code=%s count=0",
                str(purge).lower(),
                str(force).lower(),
                http_status,
                code,
            )
            raise
        log.info(
            "audit remove-redundant purge=%s force=%s status=200 code=ok count=%s",
            str(purge).lower(),
            str(force).lower(),
            len(removed),
        )
        return {
            "action": "remove-redundant",
            "removed": removed,
            "count": len(removed),
        }


# ---- 辅助 -------------------------------------------------------------------


def _pageview_store(app: FastAPI) -> Any:
    """IMP-024：懒加载并复用管理页进程内的 :class:`PageviewStore` 单例。"""
    store = app.state.pageview_store
    if store is None:
        from local_webpage_access.pageviews import PageviewStore

        ws: Workspace = app.state.workspace
        # BUG-363：与 clear_instance_pageviews 共用进程级单例，避免双连接锁冲突。
        store = PageviewStore.shared_for_workspace(ws)
        app.state.pageview_store = store
    return store


def _port_pool_summary(ctx: _Ctx) -> dict[str, Any]:
    """端口池占用摘要。"""
    pool = ctx.config.portPool
    allocated = ctx.registry.allocated_ports()
    owners = {port: ctx.registry.port_owner(port) for port in allocated}
    total_in_range = max(0, pool.end - pool.start + 1)
    return {
        "start": pool.start,
        "end": pool.end,
        "total": total_in_range,
        "allocated": len(allocated),
        "free": max(0, total_in_range - len(allocated)),
        "ports": [{"port": port, "instanceId": owners.get(port)} for port in allocated],
    }


def _require_instance(ctx: _Ctx, instance_id: str) -> None:
    """校验实例存在，否则抛 404。"""
    from local_webpage_access.paths import validate_instance_id

    validate_instance_id(instance_id)
    if ctx.registry.get_instance(instance_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "not_found", "message": f"实例 {instance_id} 不存在"}},
        )


def _load_manifest_dict(workspace: Workspace, instance_id: str) -> dict[str, Any] | None:
    """读取实例 ``local-web.json`` 为字典；不存在返回 ``None``。"""
    path = workspace.app_manifest_path(instance_id)
    if not path.is_file():
        return None
    try:
        from local_webpage_access.models import InstanceManifest

        manifest = InstanceManifest.load(path)
        return manifest.to_dict()
    except Exception as exc:  # noqa: BLE001 — 详情不应因 manifest 损坏而 500
        log.warning("读取 manifest 失败（%s）：%s", instance_id, exc)
        return {"_error": str(exc)}


def _camelize_keys(row: dict[str, Any] | None) -> dict[str, Any]:
    """把 registry 的 snake_case 行转换为 API 的 camelCase 字段。"""
    if not row:
        return {}
    return {_snake_to_camel(str(k)): v for k, v in row.items()}


def _snake_to_camel(key: str) -> str:
    head, *tail = key.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


# LwaError 子类 → 错误码（进而由 _ERROR_STATUS 映射 HTTP 状态）。
# 此前 _lwa_error_code 用类名字符串匹配（"notfound"/"validation"/"conflict"），
# 但实际子类名（ConfigError/PortError/...）都不含这些子串，导致全部落到 "internal"
# → 500（BUG-033）。改为按异常类显式映射：客户端输入/配置错误 → 4xx，
# 服务端基础设施不可用 → 503，其余服务端处理失败 → 500。新增子类时不在此表
# 即默认 "internal"，不会再静默误判。
_LWA_ERROR_CODE_BY_CLASS: dict[type[LwaError], str] = {
    # 客户端输入 / 配置 / 数据问题 → 400
    ConfigError: "bad_request",
    SchemaError: "bad_request",
    PathError: "bad_request",
    ZipImportError: "bad_request",
    RecognitionError: "bad_request",
    FolderSourceError: "bad_request",
    GitSourceError: "bad_request",
    # 服务端依赖不可用（端口池耗尽 / Docker 不可用）→ 503
    PortError: "service_unavailable",
    DockerError: "service_unavailable",
    # 服务端处理失败 → 500
    RegistryError: "internal",
    GatewayError: "internal",
    BuildError: "internal",
    LifecycleError: "internal",
    HostingError: "internal",
}


def _lwa_error_code(exc: LwaError) -> str:
    """根据异常类推断错误码（BUG-033 / IMP-035）。

    用显式类映射取代类名字符串匹配；未知子类默认 ``internal``。
    ``DataNonemptyError`` / ``code=data_nonempty`` 优先于通用 LifecycleError→internal。
    """
    if isinstance(exc, DataNonemptyError) or getattr(exc, "code", None) == "data_nonempty":
        return "data_nonempty"
    for cls, code in _LWA_ERROR_CODE_BY_CLASS.items():
        if isinstance(exc, cls):
            return code
    return "internal"


def _app_version() -> str:
    """遗留辅助：返回当前展示版本（新代码路径用 create_app 闭包固定值）。"""
    from local_webpage_access.version_info import display_version

    return display_version()


class _suppress_close:
    """suppress close errors during shutdown."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


def _mount_static(app: FastAPI) -> None:
    """挂载管理页静态资源（WBS-22.02）。

    静态目录位于包内 ``manager_static/``。若不存在则注册一个占位首页，
    指引用户调用 CLI（前端在 WBS-23 完善）。
    """
    static_dir = Path(__file__).parent / MANAGER_STATIC_DIR
    if static_dir.is_dir() and (static_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
        return

    @app.get("/", include_in_schema=False)
    def index_placeholder() -> HTMLResponse:
        return HTMLResponse(_PLACEHOLDER_HTML)

    @app.get("/{full_path:path}", include_in_schema=False)
    def catch_all(full_path: str) -> HTMLResponse:
        # 前端为单页应用时由 / 处理；未部署前端时回退占位页
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404)
        return HTMLResponse(_PLACEHOLDER_HTML)


_PLACEHOLDER_HTML = (
    "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
    "<title>Local Webpage Access</title></head>"
    "<body style='font-family:system-ui;padding:2rem;line-height:1.6'>"
    "<h1>Local Webpage Access Manager</h1>"
    "<p>管理页前端尚未部署（WBS-23）。API 已就绪，可用 <code>lwa manager on</code> "
    "查看 token 并访问 <code>/api/stats</code>、<code>/api/instances</code> 等接口。</p>"
    "</body></html>"
)


# ---- 运行入口（WBS-22.13）---------------------------------------------------


def run_manager(
    workspace: Workspace,
    config: Config,
    *,
    host: str | None = None,
    port: int | None = None,
) -> None:
    """``lwa manager start``：打开 registry、确保 token、启动 uvicorn（阻塞）。

    返回前关闭 registry。Ctrl+C 由 uvicorn 处理后正常退出。
    """
    import uvicorn

    bind_host = host or config.managerHost
    bind_port = port if port is not None else config.managerPort

    reg = Registry(workspace.db_path)
    reg.open()
    try:
        token = ensure_token(workspace)
        from local_webpage_access.security import assert_no_critical, validate_manager_binding

        assert_no_critical(
            validate_manager_binding(bind_host, has_token=bool(token), port=bind_port)
        )
        # BUG-118：禁止把完整 token 写入可被他人读取的日志文件；CLI 仍会向终端打印。
        log.info("管理页已就绪（API token 仅通过 CLI 输出，不写入日志）")
        app = create_app(workspace, config, reg, token=token)
        config_obj = uvicorn.Config(
            app,
            host=bind_host,
            port=bind_port,
            log_level="info",
            access_log=False,
        )
        server = uvicorn.Server(config_obj)
        server.run()
    finally:
        reg.close()


__all__ = [
    "TOKEN_FILENAME",
    "TOKEN_ROTATE_HOURS_DEFAULT",
    "MANAGER_STATIC_DIR",
    "token_path",
    "ensure_token",
    "rotate_token",
    "read_token",
    "read_token_metadata",
    "should_rotate_token",
    "maybe_rotate_token",
    "create_app",
    "run_manager",
    "require_token",
    "_is_localhost_client",
]
