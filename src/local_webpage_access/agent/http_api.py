"""Agent HTTP API（AGC-W12，M1）：``/api/agent/v1/*`` 严格新路由。

- 全部端点经 :func:`authenticate_local_owner`：仅回环 + 有效凭据（M1 本机 owner）；
- 错误按契约 ERROR_SPECS 映射 HTTP 状态与错误体，不走旧 ``_lwa_error_code``
  （那会把自定义 code 落成 500）；本模块注册的处理器在 MRO 上先于 LwaError 处理器；
- 受理类端点返回 202 + OperationAccepted；业务执行失败体现在 operation，
  不经 HTTP 状态表达（§6.4）；
- plan/apply 含 Git 克隆等有界 IO，一律在线程池执行，不占事件循环。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool

from local_webpage_access.agent.auth import AgentAuthError, authenticate_local_owner
from local_webpage_access.agent.contracts import (
    ERROR_SPECS,
    AccessUrlsResult,
    AgentErrorCode,
    ApplyDeploymentInput,
    CancelOperationInput,
    CancelResult,
    CapabilitiesResult,
    GetAccessUrlsInput,
    GetLogsInput,
    InstanceDetail,
    InstanceListResult,
    LifecycleInput,
    ListInstancesInput,
    LogsPage,
    OperationAction,
    OperationAccepted,
    OperationView,
    PlanDeploymentInput,
    PlanRecord,
    json_safe_issues,
)
from local_webpage_access.agent.operations import AgentOperationService
from local_webpage_access.agent.service import (
    AgentService,
    AgentServiceError,
    agent_exc_message,
)
from local_webpage_access.logging import get_logger

log = get_logger("agent.http_api")

AGENT_API_PREFIX = "/api/agent/v1"

#: BUG-709：POST body 路由装饰期的 openapi_extra 占位——route 注册完成后由
#: ``_body_models`` 统一注入自包含请求体 schema（见 register_agent_api 末尾）。
_BODY_PLACEHOLDER: dict[str, Any] = {}


class InvalidAgentRequest(Exception):
    """请求体/参数不符合契约（映射 422 needs_input）。"""

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail = detail


def agent_error_payload(code: AgentErrorCode, message: str, **extra: Any) -> dict[str, Any]:
    spec = ERROR_SPECS.get(code)
    body: dict[str, Any] = {
        "code": code.value,
        "message": message,
        "retryable": spec.retryable if spec else False,
    }
    detail = extra.pop("detail", None)
    if detail:
        body["detail"] = detail
    body.update(extra)
    return {"error": body}


def agent_error_response(exc: AgentServiceError | AgentAuthError) -> JSONResponse:
    """契约错误映射：ERROR_SPECS 查状态码；操作级 code（http_status=None）兜底 500。"""
    try:
        code = AgentErrorCode(str(exc.code))
    except ValueError:
        code = AgentErrorCode.needs_input
    spec = ERROR_SPECS.get(code)
    status = spec.http_status if spec and spec.http_status is not None else 500
    extra: dict[str, Any] = {}
    if exc.context:
        retry_after = exc.context.get("retryAfterMs")
        if retry_after is not None:
            extra["retryAfterMs"] = retry_after
    return JSONResponse(
        status_code=status,
        content=agent_error_payload(
            code,
            agent_exc_message(exc),
            detail=dict(exc.context) if exc.context else None,
            **extra,
        ),
    )


def invalid_request_response(exc: InvalidAgentRequest) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=agent_error_payload(
            AgentErrorCode.needs_input, str(exc), detail=exc.detail
        ),
    )


def register_agent_api(app: FastAPI) -> None:
    """注册 Agent v1 路由与异常映射（须在 SPA catch-all 之前调用）。"""
    from local_webpage_access.config import Config
    from local_webpage_access.paths import Workspace
    from local_webpage_access.registry import Registry

    workspace: Workspace = app.state.workspace
    config: Config = app.state.config
    registry: Registry = app.state.registry

    # ---- 异常映射（MRO 上先于既有 LwaError 处理器） ----------------------------

    @app.exception_handler(AgentAuthError)
    async def _handle_auth_error(request: Request, exc: AgentAuthError):  # noqa: ARG001
        return agent_error_response(exc)

    @app.exception_handler(AgentServiceError)
    async def _handle_service_error(request: Request, exc: AgentServiceError):  # noqa: ARG001
        return agent_error_response(exc)

    @app.exception_handler(InvalidAgentRequest)
    async def _handle_invalid(request: Request, exc: InvalidAgentRequest):  # noqa: ARG001
        return invalid_request_response(exc)

    # ---- 公共助手 ----------------------------------------------------------------

    def _principal(request: Request):
        return authenticate_local_owner(request, workspace)

    def _service(principal) -> AgentService:
        return AgentService(workspace, config, registry, principal)

    def _ops(principal) -> AgentOperationService:
        return AgentOperationService(workspace, config, registry, principal)

    async def _read_body(request: Request) -> Any:
        try:
            return await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise InvalidAgentRequest(f"请求体必须是合法 JSON: {exc}") from exc

    def _parse(model: type[BaseModel], payload: Any) -> Any:
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            # BUG-708：issues 的 ctx 可能带 ValueError 等对象，须 JSON 安全化，
            # 否则 JSONResponse 序列化失败把 422 升级成 500。
            raise InvalidAgentRequest(
                "请求参数不符合契约",
                detail={"issues": json_safe_issues(exc.errors(include_url=False))},
            ) from exc

    def _query_limit(request: Request) -> int | None:
        raw = request.query_params.get("limit")
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError as exc:
            raise InvalidAgentRequest("limit 必须是整数") from exc

    def _body_openapi(model: type[BaseModel]) -> dict[str, Any]:
        """BUG-709：手写 Request 解析（鉴权先行）路线下的 OpenAPI 请求体补全。

        schema 自包含（嵌套模型收进 ``$defs``，不引用组件表），运行时仍走
        ``_read_body``/``_parse`` 的契约 422，行为不变。
        """
        return {
            "required": True,
            "content": {"application/json": {"schema": model.model_json_schema()}},
        }

    # ---- 查询 ----------------------------------------------------------------

    @app.get(f"{AGENT_API_PREFIX}/capabilities", response_model=CapabilitiesResult)
    async def get_capabilities(request: Request):
        principal = _principal(request)
        return await run_in_threadpool(_service(principal).get_capabilities)

    @app.get(f"{AGENT_API_PREFIX}/instances", response_model=InstanceListResult)
    async def list_instances(request: Request):
        principal = _principal(request)
        payload: dict[str, Any] = {"cursor": request.query_params.get("cursor")}
        limit = _query_limit(request)
        if limit is not None:
            payload["limit"] = limit
        parsed = _parse(ListInstancesInput, payload)
        return await run_in_threadpool(lambda: _service(principal).list_instances(parsed))

    @app.get(
        f"{AGENT_API_PREFIX}/instances/{{instance_id}}", response_model=InstanceDetail
    )
    async def get_instance(request: Request, instance_id: str):
        principal = _principal(request)
        return await run_in_threadpool(
            lambda: _service(principal).get_instance(instance_id)
        )

    @app.get(
        f"{AGENT_API_PREFIX}/instances/{{instance_id}}/access-urls",
        response_model=AccessUrlsResult,
    )
    async def get_access_urls(request: Request, instance_id: str):
        principal = _principal(request)
        parsed = _parse(
            GetAccessUrlsInput,
            {
                "instanceId": instance_id,
                "perspective": request.query_params.get("perspective"),
            },
        )
        return await run_in_threadpool(
            lambda: _service(principal).get_access_urls(parsed)
        )

    @app.get(f"{AGENT_API_PREFIX}/logs", response_model=LogsPage)
    async def get_logs(request: Request):
        principal = _principal(request)
        payload: dict[str, Any] = {
            "instanceId": request.query_params.get("instanceId"),
            "operationId": request.query_params.get("operationId"),
            "category": request.query_params.get("category"),
            "cursor": request.query_params.get("cursor"),
        }
        limit = _query_limit(request)
        if limit is not None:
            payload["limit"] = limit
        parsed = _parse(GetLogsInput, payload)
        return await run_in_threadpool(lambda: _service(principal).get_logs(parsed))

    # ---- 计划与受理 ------------------------------------------------------------

    @app.post(
        f"{AGENT_API_PREFIX}/plans",
        response_model=PlanRecord,
        openapi_extra={"requestBody": _BODY_PLACEHOLDER},
    )
    async def plan_deployment(request: Request):
        principal = _principal(request)
        parsed = _parse(PlanDeploymentInput, await _read_body(request))
        return await run_in_threadpool(
            lambda: _service(principal).plan_deployment(parsed)
        )

    @app.post(
        f"{AGENT_API_PREFIX}/deployments",
        status_code=202,
        response_model=OperationAccepted,
        openapi_extra={"requestBody": _BODY_PLACEHOLDER},
    )
    async def apply_deployment(request: Request):
        principal = _principal(request)
        parsed = _parse(ApplyDeploymentInput, await _read_body(request))
        return await run_in_threadpool(lambda: _ops(principal).apply_deployment(parsed))

    # ---- 生命周期受理 ------------------------------------------------------------

    async def _lifecycle(request: Request, instance_id: str, action: OperationAction):
        principal = _principal(request)
        parsed = _parse(LifecycleInput, await _read_body(request))
        if parsed.instanceId != instance_id:
            raise InvalidAgentRequest(
                "路径与请求体的 instanceId 不一致",
                detail={"path": instance_id, "body": parsed.instanceId},
            )
        return await run_in_threadpool(
            lambda: _ops(principal).submit_lifecycle(action, parsed)
        )

    @app.post(
        f"{AGENT_API_PREFIX}/instances/{{instance_id}}/start",
        status_code=202,
        response_model=OperationAccepted,
        openapi_extra={"requestBody": _BODY_PLACEHOLDER},
    )
    async def start_instance(request: Request, instance_id: str):
        return await _lifecycle(request, instance_id, OperationAction.start)

    @app.post(
        f"{AGENT_API_PREFIX}/instances/{{instance_id}}/stop",
        status_code=202,
        response_model=OperationAccepted,
        openapi_extra={"requestBody": _BODY_PLACEHOLDER},
    )
    async def stop_instance(request: Request, instance_id: str):
        return await _lifecycle(request, instance_id, OperationAction.stop)

    @app.post(
        f"{AGENT_API_PREFIX}/instances/{{instance_id}}/restart",
        status_code=202,
        response_model=OperationAccepted,
        openapi_extra={"requestBody": _BODY_PLACEHOLDER},
    )
    async def restart_instance(request: Request, instance_id: str):
        return await _lifecycle(request, instance_id, OperationAction.restart)

    @app.post(
        f"{AGENT_API_PREFIX}/instances/{{instance_id}}/rebuild",
        status_code=202,
        response_model=OperationAccepted,
        openapi_extra={"requestBody": _BODY_PLACEHOLDER},
    )
    async def rebuild_instance(request: Request, instance_id: str):
        return await _lifecycle(request, instance_id, OperationAction.rebuild)

    # ---- 操作查询与取消 ----------------------------------------------------------

    @app.get(
        f"{AGENT_API_PREFIX}/operations/{{operation_id}}", response_model=OperationView
    )
    async def get_operation(request: Request, operation_id: str):
        principal = _principal(request)
        return await run_in_threadpool(
            lambda: _ops(principal).get_operation(operation_id)
        )

    @app.post(
        f"{AGENT_API_PREFIX}/operations/{{operation_id}}/cancel",
        response_model=CancelResult,
    )
    async def cancel_operation(request: Request, operation_id: str):
        principal = _principal(request)
        _parse(CancelOperationInput, {"operationId": operation_id})
        return await run_in_threadpool(
            lambda: _ops(principal).cancel_operation(operation_id)
        )

    # BUG-709：请求体 schema 按 route 实际模型注入（占位符替换为自包含 schema，
    # 与鉴权先行的手写解析共存，OpenAPI 才有 requestBody 可发布）。
    _body_models: dict[str, type[BaseModel]] = {
        f"{AGENT_API_PREFIX}/plans": PlanDeploymentInput,
        f"{AGENT_API_PREFIX}/deployments": ApplyDeploymentInput,
        f"{AGENT_API_PREFIX}/instances/{{instance_id}}/start": LifecycleInput,
        f"{AGENT_API_PREFIX}/instances/{{instance_id}}/stop": LifecycleInput,
        f"{AGENT_API_PREFIX}/instances/{{instance_id}}/restart": LifecycleInput,
        f"{AGENT_API_PREFIX}/instances/{{instance_id}}/rebuild": LifecycleInput,
    }
    from fastapi.routing import APIRoute

    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        model_cls = _body_models.get(route.path)
        if model_cls is None:
            continue
        extra = dict(route.openapi_extra or {})
        extra.pop("requestBody", None)
        extra["requestBody"] = _body_openapi(model_cls)
        route.openapi_extra = extra
