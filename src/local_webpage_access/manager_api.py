"""管理页后端 API（WBS-22）。

基于 FastAPI，提供管理页所需的数据与操作接口：

* ``GET  /api/stats``            —— 顶部统计（WBS-22.05/06）
* ``GET  /api/instances``        —— 实例列表（WBS-22.03）
* ``GET  /api/instances/{id}``   —— 实例详情（WBS-22.04）
* ``GET  /api/instances/{id}/logs?category=&tail=``（WBS-22.07）
* ``GET  /api/instances/{id}/resources``
* ``POST /api/instances/{id}/{start|stop|restart|rebuild}``（WBS-22.08）
* ``GET  /api/pending``          —— pending / 导入队列（WBS-22.09）
* ``GET  /api/port-pool``        —— 端口池占用（WBS-22.10）

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
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from local_webpage_access.config import Config
from local_webpage_access.errors import (
    BuildError,
    ConfigError,
    DockerError,
    GatewayError,
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
    token = secrets.token_urlsafe(24)
    path.write_text(
        json.dumps(
            {"token": token, "createdAt": _now_iso()}, ensure_ascii=False, indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        # 收紧权限：仅所有者可读
        path.chmod(0o600)
    except OSError:
        pass  # Windows 上 chmod 无意义，忽略
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


def _verify_token(workspace: Workspace, candidate: str | None) -> bool:
    """常数时间比较 token。"""
    expected = read_token(workspace)
    if not expected:
        return False
    if not candidate:
        return False
    return hmac.compare_digest(expected, candidate)


def _now_iso() -> str:
    from local_webpage_access.logging import now_iso

    return now_iso()


# ---- 错误响应（WBS-22.11）---------------------------------------------------

# 错误码 → HTTP 状态
_ERROR_STATUS: dict[str, int] = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "bad_request": status.HTTP_400_BAD_REQUEST,
    "conflict": status.HTTP_409_CONFLICT,
    "unauthorized": status.HTTP_401_UNAUTHORIZED,
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
        self.workspace: Workspace = app.state.workspace  # type: ignore[attr-defined]
        self.config: Config = app.state.config  # type: ignore[attr-defined]
        self.registry: Registry = app.state.registry  # type: ignore[attr-defined]

    @classmethod
    def from_request(cls, request: Request) -> _Ctx:
        return cls(request.app)


# ---- token 依赖 -------------------------------------------------------------

_LOCALHOST_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _is_localhost_client(request: Request) -> bool:
    """请求是否来自本机 loopback（IMP-003：本机免 token）。"""
    client = request.client
    if client is None:
        return False
    return client.host in _LOCALHOST_HOSTS


def require_token(request: Request) -> None:
    """校验 API token（WBS-22.12）。

    支持三种传递方式：``Authorization: Bearer <token>``、
    ``X-LWA-Token`` 头、``?token=`` 查询参数。

    从 ``127.0.0.1`` / ``localhost`` / ``::1`` 访问时跳过鉴权（IMP-003），
    便于本机调试；局域网 IP 访问仍必须携带有效 token。
    """
    if _is_localhost_client(request):
        return
    workspace: Workspace = request.app.state.workspace
    candidate = _extract_token(request)
    if not _verify_token(workspace, candidate):
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
    from local_webpage_access.logging import setup_logging

    setup_logging(level=config.logLevel)  # type: ignore[arg-type]

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        try:
            yield
        finally:
            with _suppress_close():
                registry.close()

    app = FastAPI(
        title="Local Webpage Access Manager",
        version=_app_version(),
        lifespan=_lifespan,
        # 统一错误响应由下方异常处理器实现
    )
    app.state.workspace = workspace
    app.state.config = config
    app.state.registry = registry
    app.state.token = token

    # ---- 异常处理器：LwaError → 统一错误格式 ----
    @app.exception_handler(LwaError)
    async def _handle_lwa_error(request: Request, exc: LwaError):  # noqa: ARG001
        code = _lwa_error_code(exc)
        return error_response(code, str(exc), detail=exc.context or None)

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

    # ---- /api/health（无鉴权，供存活探测）----
    @app.get("/api/health", tags=["health"])
    def health() -> dict[str, Any]:
        ws: Workspace = app.state.workspace
        return {
            "ok": True,
            "version": _app_version(),
            "workspaceRoot": str(ws.root.resolve()),
        }

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
            },
            "typeDistribution": type_dist,
            "databaseCount": db_count,
            "portPool": _port_pool_summary(ctx),
            "host": host.to_dict(),
        }

    # ---- 实例列表（WBS-22.03）----
    @app.get("/api/instances", dependencies=[api], tags=["instances"])
    def list_instances() -> dict[str, Any]:
        ctx = _Ctx(app)
        # 先观测回写，再取快照（状态尽量新鲜）
        sync_status(ctx.workspace, ctx.config, ctx.registry)
        statuses = all_statuses(ctx.workspace, ctx.config, ctx.registry)
        return {"instances": [s.to_dict() for s in statuses]}

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
        info = instance_resources(
            ctx.workspace, ctx.config, ctx.registry, instance_id
        )
        return {"instanceId": instance_id, "resources": info.to_dict()}

    # ---- 操作（WBS-22.08）----
    def _lifecycle_op(
        instance_id: str,
        op: Callable[[Workspace, Config, Registry, str], Any],
        *,
        label: str,
    ) -> dict[str, Any]:
        ctx = _Ctx(app)
        _require_instance(ctx, instance_id)
        op(ctx.workspace, ctx.config, ctx.registry, instance_id)
        sync_status(ctx.workspace, ctx.config, ctx.registry, instance_id)
        snap = instance_status(ctx.workspace, ctx.config, ctx.registry, instance_id)
        return {"instanceId": instance_id, "action": label, "instance": snap.to_dict()}

    @app.post("/api/instances/{instance_id}/start", dependencies=[api], tags=["instances"])
    def start_op(instance_id: str) -> dict[str, Any]:
        from local_webpage_access.lifecycle import start_instance

        return _lifecycle_op(instance_id, start_instance, label="start")

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


# ---- 辅助 -------------------------------------------------------------------


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
        "ports": [
            {"port": port, "instanceId": owners.get(port)} for port in allocated
        ],
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
    """根据异常类推断错误码（BUG-033）。

    用显式类映射取代类名字符串匹配；未知子类默认 ``internal``。
    """
    for cls, code in _LWA_ERROR_CODE_BY_CLASS.items():
        if isinstance(exc, cls):
            return code
    return "internal"


def _app_version() -> str:
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
    "<p>管理页前端尚未部署（WBS-23）。API 已就绪，可用 <code>lwa manager start</code> "
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
        log.info("管理页 token：%s", token)
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
    "MANAGER_STATIC_DIR",
    "token_path",
    "ensure_token",
    "read_token",
    "create_app",
    "run_manager",
    "require_token",
    "_is_localhost_client",
]
