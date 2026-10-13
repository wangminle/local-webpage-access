"""Agent 契约测试（AGC-W04，M1）。

验收场景（设计 §9.2 W04）：
1. 严格校验：strict bool/enum、未知字段拒绝、时间戳 RFC 3339；
2. 错误表快照：错误码全集、HTTP 映射与 retryable 语义冻结（防漂移）；
3. 分页契约快照：默认/边界/nextCursor 语义冻结。

契约是 OpenAPI 与 MCP tool schema 的共源（设计 §8），本文件即"冻结"的守护测试：
任何契约变更必须显式修改本文件的快照断言并走契约评审。
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from local_webpage_access.agent import contracts as c


# ---- 1. 严格校验 --------------------------------------------------------------


def test_unknown_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        c.GetOperationInput.model_validate({"operationId": "op_1", "extra": 1})
    with pytest.raises(ValidationError):
        c.ListInstancesInput.model_validate({"limit": 10, "page": 2})


def test_strict_enum() -> None:
    # 枚举走 lax：合法值字符串可解析、未知值被拒
    assert c.OperationStatus("queued") is c.OperationStatus.queued
    with pytest.raises(ValueError):
        c.OperationStatus("paused")
    with pytest.raises(ValidationError):
        c.OperationRecord.model_validate({"status": "paused"})
    with pytest.raises(ValidationError):
        c.GetLogsInput.model_validate({"instanceId": "x", "limit": "50"})  # limit 必须 int


def test_strict_bool() -> None:
    from pydantic import StrictBool

    class BoolHolder(c.StrictModel):
        flag: StrictBool

    assert BoolHolder(flag=True).flag is True
    with pytest.raises(ValidationError):
        BoolHolder.model_validate({"flag": 1})  # int 不得当 bool
    with pytest.raises(ValidationError):
        BoolHolder.model_validate({"flag": "true"})


def _operation_record_payload(**over: object) -> dict:
    base: dict = {
        "operationId": "op_1",
        "principalId": "local-owner",
        "workspaceId": "ws_1",
        "action": "deploy",
        "requestHash": "h" * 64,
        "idempotencyKey": "idem-1",
        "status": "queued",
        "createdAt": "2026-09-15T04:00:00Z",
        "updatedAt": "2026-09-15T04:00:00Z",
    }
    base.update(over)
    return base


def _plan_record_payload(**over: object) -> dict:
    base: dict = {
        "planId": "plan_1",
        "principalId": "local-owner",
        "workspaceId": "ws_1",
        "intent": "create",
        "source": {"type": "git", "url": "https://github.com/o/r"},
        "sourceDigest": "d" * 64,
        "options": {},
        "policyVersion": "1",
        "requestHash": "h" * 64,
        "createdAt": "2026-09-15T04:00:00Z",
        "expiresAt": "2026-09-15T04:30:00Z",
    }
    base.update(over)
    return base


def test_timestamps_must_be_rfc3339() -> None:
    ok = c.OperationRecord.model_validate(_operation_record_payload())
    assert ok.createdAt.endswith("Z")
    with pytest.raises(ValidationError):
        ok.model_validate({**ok.model_dump(), "createdAt": "2026-09-15 04:00:00"})


def test_validate_rfc3339_rejects_impossible_dates_and_offsets() -> None:
    """BUG-655：格式匹配不等于合法日历/时区；+99:99 与 2 月 30 日必须拒绝。"""
    with pytest.raises(ValueError):
        c.validate_rfc3339("2026-99-99T99:99:99Z")
    with pytest.raises(ValueError):
        c.validate_rfc3339("2026-09-15T04:00:00+99:99")
    with pytest.raises(ValueError):
        c.validate_rfc3339("2026-02-30T00:00:00Z")
    assert c.validate_rfc3339("2026-09-15T04:00:00Z") == "2026-09-15T04:00:00Z"
    assert c.validate_rfc3339("2026-09-15T10:00:00+08:00") == "2026-09-15T10:00:00+08:00"


def test_all_timestamp_fields_reject_non_rfc3339() -> None:
    """BUG-655：OperationView / PlanRecord / 观测时间字段都必须走 RFC 3339。"""
    with pytest.raises(ValidationError):
        c.OperationView.model_validate(
            {
                "operationId": "op_1",
                "action": "deploy",
                "status": "queued",
                "createdAt": "not-a-date",
                "updatedAt": "2026-09-15T04:00:00Z",
            }
        )
    with pytest.raises(ValidationError):
        c.PlanRecord.model_validate(_plan_record_payload(createdAt="2026-99-99T99:99:99Z"))
    with pytest.raises(ValidationError):
        c.CapabilitiesResult.model_validate(
            {
                "workspaceId": "ws_1",
                "contractVersion": "1",
                "inputTypes": [],
                "runtime": {},
                "observedAt": "yesterday",
            }
        )
    with pytest.raises(ValidationError):
        c.AccessUrlEntry.model_validate(
            {
                "url": "http://127.0.0.1:18000/",
                "audience": "localhost",
                "observedAt": "2026-13-01T00:00:00Z",
            }
        )
    with pytest.raises(ValidationError):
        c.InstanceDetail.model_validate(
            {
                "instanceId": "site",
                "name": "site",
                "kind": "static",
                "runtime": "builtin",
                "status": "running",
                "revision": 1,
                "desiredState": "running",
                "updatedAt": "2026-09-15 04:00:00",
            }
        )


def test_plan_record_rejects_boolean_revision() -> None:
    """BUG-655：PlanRecord.expectedRevision 不得把 bool 当成 int。"""
    with pytest.raises(ValidationError):
        c.PlanRecord.model_validate(_plan_record_payload(intent="update", expectedRevision=True))


def test_source_union_discriminator() -> None:
    src = c.PlanDeploymentInput.model_validate(
        {"source": {"type": "git", "url": "https://github.com/o/r"}, "intent": "create"}
    )
    assert isinstance(src.source, c.GitSource)
    with pytest.raises(ValidationError):
        c.PlanDeploymentInput.model_validate(
            {"source": {"type": "svn", "url": "x"}, "intent": "create"}
        )
    with pytest.raises(ValidationError):
        # artifact 源在契约中冻结，M1 不开放（W19 接入），但形状必须稳定
        c.PlanDeploymentInput.model_validate(
            {"source": {"type": "artifact"}, "intent": "create"}
        )


def test_plan_intent_target_rules() -> None:
    with pytest.raises(ValidationError):
        # update 必须带 targetInstanceId + expectedRevision
        c.PlanDeploymentInput.model_validate(
            {"source": {"type": "git", "url": "https://github.com/o/r"}, "intent": "update"}
        )
    with pytest.raises(ValidationError):
        # create 不得绑定既有实例
        c.PlanDeploymentInput.model_validate(
            {
                "source": {"type": "git", "url": "https://github.com/o/r"},
                "intent": "create",
                "targetInstanceId": "exists",
            }
        )
    ok = c.PlanDeploymentInput.model_validate(
        {
            "source": {"type": "server_directory", "path": "/srv/src/site"},
            "intent": "update",
            "targetInstanceId": "site",
            "expectedRevision": 3,
        }
    )
    assert ok.expectedRevision == 3


def test_logs_input_exactly_one_target() -> None:
    with pytest.raises(ValidationError):
        c.GetLogsInput.model_validate({"instanceId": "a", "operationId": "op_1"})
    with pytest.raises(ValidationError):
        c.GetLogsInput.model_validate({})
    assert c.GetLogsInput.model_validate({"operationId": "op_1"}).operationId == "op_1"


def test_canonical_request_hash_stable() -> None:
    a = {"intent": "create", "source": {"type": "git", "url": "u", "ref": None, "subdir": None}}
    b = {"source": {"subdir": None, "ref": None, "url": "u", "type": "git"}, "intent": "create"}
    assert c.canonical_json(a) == c.canonical_json(b)
    assert c.request_hash(a) == c.request_hash(b)
    assert c.request_hash(a) != c.request_hash({**a, "intent": "update"})


# ---- 2. 错误表快照（冻结） ------------------------------------------------------


def test_error_codes_frozen() -> None:
    assert {e.value for e in c.AgentErrorCode} == {
        "unauthenticated",
        "permission_denied",
        "revision_conflict",
        "idempotency_conflict",
        "source_not_allowed",
        "quota_exceeded",
        "capability_unavailable",
        "needs_input",
        "manager_unavailable",
        "busy",
        "build_failed",
        "healthcheck_failed",
        "interrupted",
    }


def test_error_http_and_retryable_mapping_frozen() -> None:
    assert c.ERROR_SPECS["unauthenticated"].http_status == 401
    assert c.ERROR_SPECS["permission_denied"].http_status == 403
    assert c.ERROR_SPECS["revision_conflict"].http_status == 409
    assert c.ERROR_SPECS["idempotency_conflict"].http_status == 409
    assert c.ERROR_SPECS["manager_unavailable"].http_status == 503
    assert c.ERROR_SPECS["busy"].http_status == 429
    assert c.ERROR_SPECS["quota_exceeded"].http_status == 429
    # 操作级错误不经 HTTP 状态码表达（体现在 operation.error）
    assert c.ERROR_SPECS["build_failed"].http_status is None
    assert c.ERROR_SPECS["healthcheck_failed"].http_status is None
    assert c.ERROR_SPECS["interrupted"].http_status is None
    # 可重试语义冻结
    retryable = {k for k, v in c.ERROR_SPECS.items() if v.retryable}
    assert retryable == {"manager_unavailable", "busy", "quota_exceeded"}


def test_agent_error_model_shape() -> None:
    err = c.AgentError.model_validate(
        {
            "code": "revision_conflict",
            "message": "实例 revision 已变化",
            "detail": {"expected": 3, "actual": 4},
            "retryable": False,
            "operationId": "op_1",
            "nextActions": ["重新读取实例状态"],
        }
    )
    dumped = err.model_dump(exclude_none=True)
    assert dumped["code"] == "revision_conflict"
    assert dumped["detail"]["expected"] == 3
    assert "retryAfterMs" not in dumped


def test_agent_error_busy_carries_retry_after_ms() -> None:
    """BUG-654：busy / manager_unavailable 须能返回 retryAfterMs（§6.4）。"""
    assert "retryAfterMs" in c.AgentError.model_fields
    err = c.AgentError.model_validate(
        {
            "code": "busy",
            "message": "服务繁忙",
            "retryable": True,
            "retryAfterMs": 1500,
        }
    )
    assert err.retryAfterMs == 1500
    with pytest.raises(ValidationError):
        c.AgentError.model_validate(
            {"code": "busy", "message": "服务繁忙", "retryable": True, "retryAfterMs": True}
        )


# ---- 3. 分页契约快照 ------------------------------------------------------------


def test_pagination_contract_frozen() -> None:
    req = c.ListInstancesInput()
    assert req.limit == 50 and req.cursor is None
    assert c.ListInstancesInput(limit=1).limit == 1
    assert c.ListInstancesInput(limit=200).limit == 200
    with pytest.raises(ValidationError):
        c.ListInstancesInput(limit=0)
    with pytest.raises(ValidationError):
        c.ListInstancesInput(limit=201)
    page = c.InstanceListResult(instances=[], nextCursor=None)
    assert page.nextCursor is None


# ---- 4. 操作状态机与工具表冻结 ---------------------------------------------------


def test_operation_status_machine_frozen() -> None:
    assert {s.value for s in c.OperationStatus} == {
        "queued", "running", "cancelling", "cancel_failed",
        "succeeded", "failed", "needs_input", "interrupted", "cancelled",
    }
    t = c.OPERATION_TRANSITIONS
    assert t[c.OperationStatus.queued] == {c.OperationStatus.running, c.OperationStatus.cancelled}
    assert t[c.OperationStatus.running] == frozenset(
        {
            c.OperationStatus.succeeded,
            c.OperationStatus.failed,
            c.OperationStatus.needs_input,
            c.OperationStatus.interrupted,
            c.OperationStatus.cancelling,
        }
    )
    assert t[c.OperationStatus.cancelling] == {
        c.OperationStatus.cancelled,
        c.OperationStatus.cancel_failed,
    }
    assert t[c.OperationStatus.needs_input] == {c.OperationStatus.cancelled}
    for terminal in (
        c.OperationStatus.succeeded,
        c.OperationStatus.failed,
        c.OperationStatus.interrupted,
        c.OperationStatus.cancelled,
        c.OperationStatus.cancel_failed,
    ):
        assert t[terminal] == frozenset()


def test_operation_phases_frozen() -> None:
    assert {p.value for p in c.OperationPhase} == {"validate", "import", "build", "start", "healthcheck"}


def test_tool_names_frozen() -> None:
    assert set(c.TOOL_SPECS) == {
        "lwa_get_capabilities",
        "lwa_list_instances",
        "lwa_get_instance",
        "lwa_plan_deployment",
        "lwa_apply_deployment",
        "lwa_get_operation",
        "lwa_get_logs",
        "lwa_get_access_urls",
        "lwa_start_instance",
        "lwa_stop_instance",
        "lwa_restart_instance",
        "lwa_rebuild_instance",
        "lwa_cancel_operation",
    }


def test_tool_annotations_honest() -> None:
    read_only = {n for n, s in c.TOOL_SPECS.items() if s.read_only}
    assert read_only == {
        "lwa_get_capabilities",
        "lwa_list_instances",
        "lwa_get_instance",
        "lwa_get_operation",
        "lwa_get_logs",
        "lwa_get_access_urls",
    }
    # plan 可能获取源和暂存（设计 §6.1），不得标成只读
    assert not c.TOOL_SPECS["lwa_plan_deployment"].read_only
    # 带 idempotencyKey 的写工具声明幂等
    for name in ("lwa_apply_deployment", "lwa_start_instance", "lwa_stop_instance",
                 "lwa_restart_instance", "lwa_rebuild_instance"):
        assert c.TOOL_SPECS[name].idempotent
    for spec in c.TOOL_SPECS.values():
        assert spec.input_model is not None and spec.output_model is not None


def test_contract_version_frozen() -> None:
    assert c.AGENT_CONTRACT_VERSION == "1"
    assert c.PLAN_TTL_SECONDS == 1800


def test_plan_record_includes_risks_and_required_capabilities() -> None:
    """BUG-654：计划输出必须含风险与所需能力（§6.1/§6.2），否则 W08 无法从共源生成。"""
    assert "risks" in c.PlanRecord.model_fields
    assert "requiredCapabilities" in c.PlanRecord.model_fields
    plan = c.PlanRecord.model_validate(
        _plan_record_payload(
            risks={
                "keepData": True,
                "resourceProfile": "small",
                "entryChange": True,
                "possibleDowntime": True,
                "capabilityGaps": ["docker"],
            },
            requiredCapabilities=["docker"],
        )
    )
    assert plan.requiredCapabilities == ["docker"]
    assert plan.risks.possibleDowntime is True
    assert plan.risks.capabilityGaps == ["docker"]
    assert plan.risks.resourceProfile == "small"
    with pytest.raises(ValidationError):
        c.PlanRecord.model_validate(_plan_record_payload(risks={"unknown": True}))


def test_instance_detail_includes_recent_operation() -> None:
    """BUG-654：实例详情必须含最近 operation（§6.1）。"""
    assert "recentOperation" in c.InstanceDetail.model_fields
    detail = c.InstanceDetail.model_validate(
        {
            "instanceId": "site",
            "name": "site",
            "kind": "static",
            "runtime": "builtin",
            "status": "running",
            "revision": 3,
            "desiredState": "running",
            "updatedAt": "2026-09-15T04:00:00Z",
            "recentOperation": {
                "operationId": "op_1",
                "action": "deploy",
                "status": "succeeded",
                "createdAt": "2026-09-15T04:00:00Z",
                "updatedAt": "2026-09-15T04:01:00Z",
            },
        }
    )
    assert detail.recentOperation is not None
    assert detail.recentOperation.operationId == "op_1"
    empty = c.InstanceDetail.model_validate(
        {
            "instanceId": "site",
            "name": "site",
            "kind": "static",
            "runtime": "builtin",
            "status": "running",
            "revision": 1,
            "desiredState": "running",
            "updatedAt": "2026-09-15T04:00:00Z",
        }
    )
    assert empty.recentOperation is None


def test_plan_input_json_schema_encodes_intent_rules() -> None:
    """BUG-656：跨字段 intent 规则必须进入 JSON Schema，不能只靠运行时 validator。"""
    schema = c.PlanDeploymentInput.model_json_schema()
    top = {k: schema[k] for k in schema if k in ("allOf", "oneOf", "anyOf", "if", "then")}
    assert top, schema
    blob = json.dumps(top)
    assert "targetInstanceId" in blob
    assert "expectedRevision" in blob
    assert "update" in blob
    assert "create" in blob


def test_logs_input_json_schema_encodes_exactly_one_target() -> None:
    """BUG-656：instanceId / operationId 二选一必须出现在 JSON Schema。"""
    schema = c.GetLogsInput.model_json_schema()
    top = {k: schema[k] for k in schema if k in ("allOf", "oneOf", "anyOf", "if", "then")}
    assert top, schema
    blob = json.dumps(top)
    assert "instanceId" in blob
    assert "operationId" in blob
