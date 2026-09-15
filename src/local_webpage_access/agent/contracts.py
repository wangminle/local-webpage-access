"""Agent 契约：严格输入/输出模型、错误表、分页、工具元数据（AGC-W04，M1）。

本模块是 Agent 通道的**契约共源**：OpenAPI（W12）与 MCP tool schema（W13）
均从这里生成，禁止在任何一侧手抄漂移（设计 §8）。

冻结约束（守护测试 ``tests/test_agent_contracts.py``）：

- 所有模型 ``extra="forbid"`` + ``strict=True``：未知字段拒绝、bool/int 不隐式转换；
- 时间戳一律 RFC 3339 字符串（UTC 存储）；
- 错误码全集、HTTP 映射、retryable 语义冻结；
- 分页默认 ``limit=50``、上界 200；
- 操作状态机与 13 个工具名冻结。

契约版本独立于产品版本递增（``AGENT_CONTRACT_VERSION``）。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

#: Agent 契约版本（与产品版本独立，破坏性变更时递增）
AGENT_CONTRACT_VERSION = "1"

#: 部署计划有效期（秒），设计 §6.2 默认 30 分钟
PLAN_TTL_SECONDS = 1800


# ---- 基础模型 -----------------------------------------------------------------

class StrictModel(BaseModel):
    """Agent 契约模型基类。

    禁额外字段；bool/int 字段以 ``StrictBool``/``StrictInt`` 注解实现严格校验
    （``int 1`` 不当 bool、``"50"`` 不当 int）。枚举走 lax 路径：接受合法值
    字符串、拒绝未知值——线上契约的输入即 JSON 字符串。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)


def validate_rfc3339(value: str) -> str:
    """校验 RFC 3339 时间戳字符串（契约层所有时间字段的统一约束）。

    不仅核对字面形状，还解析日历与时区：拒绝 2026-02-30、+99:99 等非法值。
    """
    if not isinstance(value, str) or not _RFC3339_RE.fullmatch(value):
        raise ValueError(f"时间戳必须是 RFC 3339 格式: {value!r}")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"时间戳必须是 RFC 3339 格式: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"时间戳必须是 RFC 3339 格式: {value!r}")
    return value


Rfc3339Timestamp = Annotated[StrictStr, AfterValidator(validate_rfc3339)]


# ---- 错误表（冻结） -------------------------------------------------------------

class AgentErrorCode(str, Enum):
    """Agent 通道错误码全集（设计 §6.4）。"""

    unauthenticated = "unauthenticated"
    permission_denied = "permission_denied"
    revision_conflict = "revision_conflict"
    idempotency_conflict = "idempotency_conflict"
    source_not_allowed = "source_not_allowed"
    quota_exceeded = "quota_exceeded"
    capability_unavailable = "capability_unavailable"
    needs_input = "needs_input"
    manager_unavailable = "manager_unavailable"
    busy = "busy"
    build_failed = "build_failed"
    healthcheck_failed = "healthcheck_failed"
    interrupted = "interrupted"


@dataclass(frozen=True)
class ErrorSpec:
    """错误码语义：HTTP 状态映射与可重试性。

    ``http_status=None`` 表示操作级错误——不经 HTTP 状态码表达，
    体现在 operation 的 ``error`` 字段（如 build_failed）。
    """

    http_status: int | None
    retryable: bool
    description: str


#: 错误表快照（契约冻结；修改需过契约评审并同步守护测试）
ERROR_SPECS: dict[AgentErrorCode, ErrorSpec] = {
    AgentErrorCode.unauthenticated: ErrorSpec(401, False, "凭据无效或缺失"),
    AgentErrorCode.permission_denied: ErrorSpec(403, False, "主体无权执行该操作"),
    AgentErrorCode.revision_conflict: ErrorSpec(409, False, "实例 revision 与期望不符"),
    AgentErrorCode.idempotency_conflict: ErrorSpec(409, False, "幂等键复用但请求内容不同"),
    AgentErrorCode.source_not_allowed: ErrorSpec(400, False, "部署来源不在允许范围"),
    AgentErrorCode.quota_exceeded: ErrorSpec(429, True, "资源配额已满"),
    AgentErrorCode.capability_unavailable: ErrorSpec(409, False, "所需运行能力不可用"),
    AgentErrorCode.needs_input: ErrorSpec(422, False, "需要补充输入或人工确认"),
    AgentErrorCode.manager_unavailable: ErrorSpec(503, True, "manager 不在线"),
    AgentErrorCode.busy: ErrorSpec(429, True, "服务繁忙，稍后有界退避重试"),
    AgentErrorCode.build_failed: ErrorSpec(None, False, "构建失败（操作级）"),
    AgentErrorCode.healthcheck_failed: ErrorSpec(None, False, "健康检查失败（操作级）"),
    AgentErrorCode.interrupted: ErrorSpec(None, False, "结果未知，需核对后恢复（操作级）"),
}


class AgentError(StrictModel):
    """统一错误模型：沿用 LWA ``error.code/message/detail`` 风格并扩展（§6.4）。"""

    code: AgentErrorCode
    message: str
    detail: dict[str, Any] | None = None
    retryable: StrictBool = False
    operationId: str | None = None
    nextActions: list[str] = Field(default_factory=list)
    retryAfterMs: StrictInt | None = Field(default=None, ge=0)


# ---- 分页 ---------------------------------------------------------------------

DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200


class PageRequest(StrictModel):
    """分页请求：cursor 不透明，limit 默认 50、上界 200。"""

    cursor: str | None = None
    limit: StrictInt = Field(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT)


# ---- 操作（§6.3） ---------------------------------------------------------------

class OperationStatus(str, Enum):
    queued = "queued"
    running = "running"
    cancelling = "cancelling"
    cancel_failed = "cancel_failed"
    succeeded = "succeeded"
    failed = "failed"
    needs_input = "needs_input"
    interrupted = "interrupted"
    cancelled = "cancelled"


#: 状态机（§6.3 图）：终态无出边；needs_input 只能被取消，继续须新计划/新 operation
OPERATION_TRANSITIONS: dict[OperationStatus, frozenset[OperationStatus]] = {
    OperationStatus.queued: frozenset({OperationStatus.running, OperationStatus.cancelled}),
    OperationStatus.running: frozenset(
        {
            OperationStatus.succeeded,
            OperationStatus.failed,
            OperationStatus.needs_input,
            OperationStatus.interrupted,
            OperationStatus.cancelling,
        }
    ),
    OperationStatus.cancelling: frozenset(
        {OperationStatus.cancelled, OperationStatus.cancel_failed}
    ),
    OperationStatus.needs_input: frozenset({OperationStatus.cancelled}),
    OperationStatus.succeeded: frozenset(),
    OperationStatus.failed: frozenset(),
    OperationStatus.interrupted: frozenset(),
    OperationStatus.cancelled: frozenset(),
    OperationStatus.cancel_failed: frozenset(),
}


class OperationPhase(str, Enum):
    """操作执行阶段；与实例 running/stopped 状态分属两个概念（§6.3）。"""

    validate = "validate"
    import_ = "import"
    build = "build"
    start = "start"
    healthcheck = "healthcheck"


class OperationAction(str, Enum):
    deploy = "deploy"
    start = "start"
    stop = "stop"
    restart = "restart"
    rebuild = "rebuild"


class OperationRecord(StrictModel):
    """持久化操作记录（§6.3 必需字段，全量）。

    这是存储形状；对外返回一律经 ``OperationView`` 过滤
    （不含 principal secret 与绝对宿主路径）。
    """

    operationId: str
    principalId: str
    workspaceId: str
    action: OperationAction
    targetInstanceId: str | None = None
    requestHash: str = Field(min_length=64, max_length=64)
    idempotencyKey: str
    planId: str | None = None
    status: OperationStatus
    phase: OperationPhase | None = None
    createdAt: Rfc3339Timestamp
    updatedAt: Rfc3339Timestamp
    workerIdentity: str | None = None
    leaseUntil: Rfc3339Timestamp | None = None
    buildToken: str | None = None
    result: dict[str, Any] | None = None
    error: AgentError | None = None


class OperationView(StrictModel):
    """``lwa_get_operation`` / apply 受理响应的对外视图（§6.3：不含敏感值）。"""

    operationId: str
    action: OperationAction
    status: OperationStatus
    phase: OperationPhase | None = None
    instanceId: str | None = None
    result: dict[str, Any] | None = None
    error: AgentError | None = None
    createdAt: Rfc3339Timestamp
    updatedAt: Rfc3339Timestamp


# ---- 部署源与计划（§6.2） ---------------------------------------------------------

class ServerDirectorySource(StrictModel):
    """本机受控目录源：仅本机 owner；路径须在 allowedSourceRoots 内（W06 校验）。"""

    type: Literal["server_directory"]
    path: str


class GitSource(StrictModel):
    """Git 源：复用现有 HTTPS github.com 限制（W08 解析 ref 与快照）。"""

    type: Literal["git"]
    url: str
    ref: str | None = None
    subdir: str | None = None


class ArtifactSource(StrictModel):
    """制品源：M2（W18/W19）接入；契约形状现在冻结。"""

    type: Literal["artifact"]
    artifactId: str


DeploySource = Annotated[
    Union[ServerDirectorySource, GitSource, ArtifactSource],
    Field(discriminator="type"),
]

ResourceProfileName = Literal["tiny", "small", "medium", "heavy"]


class DeployOptions(StrictModel):
    """部署选项（M1 最小集；后续按需冻结扩展）。"""

    resourceProfile: ResourceProfileName | None = None


def _plan_deployment_json_schema(schema: dict[str, Any]) -> None:
    """把 intent/target 跨字段规则写入 JSON Schema（BUG-656，供 OpenAPI/MCP 共源）。"""
    schema["allOf"] = [
        {
            "if": {
                "properties": {"intent": {"const": "update"}},
                "required": ["intent"],
            },
            "then": {
                "required": ["targetInstanceId", "expectedRevision"],
                "properties": {"displayName": {"type": "null"}},
            },
        },
        {
            "if": {
                "properties": {"intent": {"const": "create"}},
                "required": ["intent"],
            },
            "then": {
                "properties": {
                    "targetInstanceId": {"type": "null"},
                    "expectedRevision": {"type": "null"},
                }
            },
        },
    ]


class PlanDeploymentInput(StrictModel):
    """``lwa_plan_deployment`` 入参：intent 决定目标规则（§6.2）。"""

    model_config = ConfigDict(json_schema_extra=_plan_deployment_json_schema)

    source: DeploySource
    intent: Literal["create", "update"]
    targetInstanceId: str | None = None
    expectedRevision: StrictInt | None = Field(default=None, ge=1)
    displayName: str | None = None
    options: DeployOptions = Field(default_factory=DeployOptions)

    @model_validator(mode="after")
    def _check_intent_target(self) -> PlanDeploymentInput:
        if self.intent == "update":
            if not self.targetInstanceId or self.expectedRevision is None:
                raise ValueError("update 必须携带 targetInstanceId 与 expectedRevision")
            if self.displayName is not None:
                raise ValueError("update 的名称只是显示名，不接受 displayName")
        else:
            if self.targetInstanceId is not None or self.expectedRevision is not None:
                raise ValueError("create 不得绑定既有实例的 targetInstanceId/expectedRevision")
        return self


class PlanRisks(StrictModel):
    """部署计划风险摘要（§6.2：保留 data、资源档位、入口变更、可能停机与能力缺口）。"""

    keepData: StrictBool | None = None
    resourceProfile: ResourceProfileName | None = None
    entryChange: StrictBool = False
    possibleDowntime: StrictBool = False
    capabilityGaps: list[str] = Field(default_factory=list)


class PlanRecord(StrictModel):
    """持久化部署计划（§6.2：绑定 principal、sourceDigest、目标 revision 与策略版本）。"""

    planId: str
    principalId: str
    workspaceId: str
    intent: Literal["create", "update"]
    source: DeploySource
    sourceDigest: str
    targetInstanceId: str | None = None
    expectedRevision: StrictInt | None = Field(default=None, ge=1)
    displayName: str | None = None
    options: DeployOptions
    policyVersion: str
    requestHash: str
    createdAt: Rfc3339Timestamp
    expiresAt: Rfc3339Timestamp
    risks: PlanRisks = Field(default_factory=PlanRisks)
    requiredCapabilities: list[str] = Field(default_factory=list)


# ---- 查询与结果模型（§6.1/§6.4） ---------------------------------------------------

class GetCapabilitiesInput(StrictModel):
    """``lwa_get_capabilities`` 无参数。"""


class CapabilitiesResult(StrictModel):
    """``lwa_get_capabilities`` 输出：不强制同步重探，带观测时间。"""

    workspaceId: str
    contractVersion: str
    inputTypes: list[str]
    runtime: dict[str, Any]
    observedAt: Rfc3339Timestamp


class InstanceSummary(StrictModel):
    instanceId: str
    name: str
    kind: str
    runtime: str
    status: str
    revision: StrictInt


class InstanceDetail(InstanceSummary):
    desiredState: str
    updatedAt: Rfc3339Timestamp
    recentOperation: OperationView | None = None


class InstanceListResult(StrictModel):
    instances: list[InstanceSummary]
    nextCursor: str | None = None


class GetInstanceInput(StrictModel):
    instanceId: str


class ApplyDeploymentInput(StrictModel):
    planId: str
    idempotencyKey: str


class OperationAccepted(StrictModel):
    """写工具受理响应：MCP 正常工具结果 / HTTP 202 的业务体。"""

    operationId: str
    status: OperationStatus
    instanceId: str | None = None


class GetOperationInput(StrictModel):
    operationId: str


class CancelOperationInput(StrictModel):
    operationId: str


class CancelResult(StrictModel):
    operationId: str
    status: OperationStatus


def _logs_input_json_schema(schema: dict[str, Any]) -> None:
    """把 instanceId/operationId 二选一写入 JSON Schema（BUG-656）。"""
    schema["oneOf"] = [
        {
            "required": ["instanceId"],
            "properties": {"operationId": {"type": "null"}},
        },
        {
            "required": ["operationId"],
            "properties": {"instanceId": {"type": "null"}},
        },
    ]


class GetLogsInput(PageRequest):
    """``lwa_get_logs``：instanceId 与 operationId 二选一（§6.1）。"""

    model_config = ConfigDict(json_schema_extra=_logs_input_json_schema)

    instanceId: str | None = None
    operationId: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> GetLogsInput:
        if bool(self.instanceId) == bool(self.operationId):
            raise ValueError("instanceId 与 operationId 必须二选一")
        return self


class LogsPage(StrictModel):
    lines: list[str]
    nextCursor: str | None = None
    truncated: StrictBool = False


class GetAccessUrlsInput(StrictModel):
    instanceId: str
    perspective: Literal["localhost", "lan"] | None = None


class AccessUrlEntry(StrictModel):
    """访问 URL：区分服务端观测与客户端可达（§6.4，不承诺远端实测成功）。"""

    url: str
    audience: Literal["localhost", "lan"]
    serverProbe: Literal["ok", "failed", "unknown"] | None = None
    clientReachability: Literal["ok", "failed", "unknown"] = "unknown"
    observedAt: Rfc3339Timestamp


class AccessUrlsResult(StrictModel):
    instanceId: str
    urls: list[AccessUrlEntry]


# ---- 规范化与请求摘要（§6.3） -------------------------------------------------------


def canonical_json(payload: Any) -> str:
    """规范化 JSON：排序键、紧凑分隔符。

    幂等请求摘要必须基于本形式（§6.3：不以当前时间等易变字段计算）。
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def request_hash(payload: Any) -> str:
    """请求摘要（SHA-256）：相同规范化 payload 必须得到相同摘要。"""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# ---- 工具元数据（§6.1，冻结） -------------------------------------------------------

@dataclass(frozen=True)
class ToolSpec:
    """工具注册项：annotations 只是提示，服务端授权不依赖提示（§6.1）。"""

    name: str
    title: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    external_interaction: bool = False


class ListInstancesInput(PageRequest):
    """``lwa_list_instances``：分页。"""


class LifecycleInput(StrictModel):
    """start/stop/restart/rebuild 共有形状（§6.1：独立工具便于逐项授权）。"""

    instanceId: str
    expectedRevision: StrictInt = Field(ge=1)
    idempotencyKey: str


#: 工具表（契约冻结；改动须过契约评审并同步守护测试快照）
TOOL_SPECS: dict[str, ToolSpec] = {
    "lwa_get_capabilities": ToolSpec(
        name="lwa_get_capabilities",
        title="获取能力",
        description="返回 workspaceId、契约版本、支持的输入类型与 Runtime 能力及观测时间",
        input_model=GetCapabilitiesInput,
        output_model=CapabilitiesResult,
        read_only=True,
    ),
    "lwa_list_instances": ToolSpec(
        name="lwa_list_instances",
        title="列出实例",
        description="分页返回授权范围内的实例摘要",
        input_model=ListInstancesInput,
        output_model=InstanceListResult,
        read_only=True,
    ),
    "lwa_get_instance": ToolSpec(
        name="lwa_get_instance",
        title="实例详情",
        description="返回实例 revision、观测态与最近操作",
        input_model=GetInstanceInput,
        output_model=InstanceDetail,
        read_only=True,
    ),
    "lwa_plan_deployment": ToolSpec(
        name="lwa_plan_deployment",
        title="生成部署计划",
        description="预检并生成部署计划（不修改实例；可能获取源和暂存）",
        input_model=PlanDeploymentInput,
        output_model=PlanRecord,
        read_only=False,
        external_interaction=True,
    ),
    "lwa_apply_deployment": ToolSpec(
        name="lwa_apply_deployment",
        title="执行部署",
        description="按计划受理部署，创建或更新实例",
        input_model=ApplyDeploymentInput,
        output_model=OperationAccepted,
        idempotent=True,
    ),
    "lwa_get_operation": ToolSpec(
        name="lwa_get_operation",
        title="查询操作",
        description="返回操作阶段、状态、目标实例、错误与建议",
        input_model=GetOperationInput,
        output_model=OperationView,
        read_only=True,
    ),
    "lwa_get_logs": ToolSpec(
        name="lwa_get_logs",
        title="读取日志",
        description="分页返回脱敏日志（需 logs:read）",
        input_model=GetLogsInput,
        output_model=LogsPage,
        read_only=True,
    ),
    "lwa_get_access_urls": ToolSpec(
        name="lwa_get_access_urls",
        title="访问地址",
        description="返回实例访问 URL、适用网络与观测证据（不承诺客户端可达）",
        input_model=GetAccessUrlsInput,
        output_model=AccessUrlsResult,
        read_only=True,
    ),
    "lwa_start_instance": ToolSpec(
        name="lwa_start_instance",
        title="启动实例",
        description="启动实例（校验 expectedRevision）",
        input_model=LifecycleInput,
        output_model=OperationAccepted,
        idempotent=True,
    ),
    "lwa_stop_instance": ToolSpec(
        name="lwa_stop_instance",
        title="停止实例",
        description="停止实例（校验 expectedRevision）",
        input_model=LifecycleInput,
        output_model=OperationAccepted,
        idempotent=True,
    ),
    "lwa_restart_instance": ToolSpec(
        name="lwa_restart_instance",
        title="重启实例",
        description="重启实例（校验 expectedRevision）",
        input_model=LifecycleInput,
        output_model=OperationAccepted,
        idempotent=True,
    ),
    "lwa_rebuild_instance": ToolSpec(
        name="lwa_rebuild_instance",
        title="重建实例",
        description="重新构建实例（不自动修改技术栈配置）",
        input_model=LifecycleInput,
        output_model=OperationAccepted,
        idempotent=True,
    ),
    "lwa_cancel_operation": ToolSpec(
        name="lwa_cancel_operation",
        title="取消操作",
        description="请求取消操作；不保证立即成功",
        input_model=CancelOperationInput,
        output_model=CancelResult,
        idempotent=True,
    ),
}

#: 冻结的工具名集合
AGENT_TOOL_NAMES = frozenset(TOOL_SPECS)
