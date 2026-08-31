"""Gate-C 实证校验降级测试（IMP-058-C C.06-C.09）。

验证 start_instance 降级流程：
- top-1 候选失败 → 回滚 → 按策略降级
- 能力守恒：后端不得降级到静态/前端（§6.1.1）
- fallback_policy 默认 confirm → 抛 FallbackConfirmationRequired
- fallback_policy=auto-equivalent → 自动降级
- 全部失败 → Layer 4 诊断报告（含回滚结果）
- 无 fallback 候选时直接抛出
- 成本控制：最多尝试 _MAX_FALLBACK_CANDIDATES 个候选
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from local_webpage_access.errors import HostingError
from local_webpage_access.models import InstanceManifest, Kind, Runtime, ServingMode


def _mk_container_manifest(
    *,
    instance_id: str = "test-instance",
    fallbacks: list[dict] | None = None,
) -> InstanceManifest:
    """构造一个 docker-compose 容器实例 manifest。"""
    manifest = InstanceManifest(
        id=instance_id,
        name="Test Instance",
        version="1",
        kind=Kind.PYTHON,
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        sourceZipPath="/tmp/test.zip",
        appPath="/tmp/current",
        container={
            "projectName": f"lwa-{instance_id}",
            "internalPort": 8000,
            "composePath": "/tmp/compose.yml",
            "dockerfilePath": "/tmp/Dockerfile",
        },
        deploymentCandidates=fallbacks or [],
    )
    return manifest


# ---- _try_host_with_fallback 测试 ------------------------------------------


def test_no_fallback_reraises_immediately() -> None:
    """无 fallback 候选时，top-1 失败直接抛出（不进入降级）。"""
    from local_webpage_access.lifecycle import _try_host_with_fallback

    manifest = _mk_container_manifest(fallbacks=[])
    host_fn = MagicMock(side_effect=HostingError("build failed"))
    mock_ws = MagicMock()
    mock_ws.app_manifest_path.return_value = Path("/tmp/manifest.json")

    with pytest.raises(HostingError, match="build failed"):
        _try_host_with_fallback(mock_ws, MagicMock(), MagicMock(), "test", manifest, host_fn)
    # host_fn 只被调用一次（top-1）
    assert host_fn.call_count == 1


def test_static_instance_no_fallback() -> None:
    """静态实例 top-1 失败不降级（前端 ↔ 后端不是等价替代）。"""
    from local_webpage_access.lifecycle import _try_host_with_fallback

    manifest = _mk_container_manifest()
    manifest.runtime = Runtime.SHARED_STATIC
    manifest.servingMode = ServingMode.SHARED_STATIC
    manifest.static = MagicMock()
    manifest.container = None
    manifest.deploymentCandidates = [{"kind": "static", "runtime": "shared_static"}]

    host_fn = MagicMock(side_effect=HostingError("static fail"))

    with pytest.raises(HostingError, match="static fail"):
        _try_host_with_fallback(MagicMock(), MagicMock(), MagicMock(), "test", manifest, host_fn)
    assert host_fn.call_count == 1


def test_fallback_success_after_top1_failure() -> None:
    """top-1 失败 → 回滚 → auto-equivalent 降级 → fallback 成功。"""
    from local_webpage_access.lifecycle import _try_host_with_fallback

    success_manifest = _mk_container_manifest()
    manifest = _mk_container_manifest(
        fallbacks=[
            {
                "kind": "node",
                "runtime": "docker-compose",
                "servingMode": "container",
                "confidenceTier": "fallback",
                "entry": {"install": "npm ci", "start": "node server.js"},
            }
        ]
    )

    call_count = [0]

    def mock_host(ws, cfg, reg, iid):
        call_count[0] += 1
        if call_count[0] == 1:
            raise HostingError("python build failed")
        return success_manifest

    host_fn = MagicMock(side_effect=mock_host)

    result = _try_host_with_fallback(
        MagicMock(),
        MagicMock(),
        MagicMock(),
        "test",
        manifest,
        host_fn,
        fallback_policy="auto-equivalent",
    )
    assert result is success_manifest
    assert host_fn.call_count == 2  # top-1 + 1 fallback


def test_confirm_policy_raises_confirmation_required() -> None:
    """Gate-C C.07：默认 confirm 策略 → 抛 FallbackConfirmationRequired（不自动降级）。"""
    from local_webpage_access.lifecycle import (
        FallbackConfirmationRequired,
        _try_host_with_fallback,
    )

    manifest = _mk_container_manifest(
        fallbacks=[
            {
                "kind": "node",
                "runtime": "docker-compose",
                "servingMode": "container",
                "confidenceTier": "fallback",
                "entry": {"install": "npm ci", "start": "node server.js"},
            }
        ]
    )

    host_fn = MagicMock(side_effect=HostingError("python build failed"))

    with pytest.raises(FallbackConfirmationRequired) as exc_info:
        _try_host_with_fallback(
            MagicMock(),
            MagicMock(),
            MagicMock(),
            "test",
            manifest,
            host_fn,
            fallback_policy="confirm",  # 默认策略
        )
    # host_fn 只调用一次（top-1），不自动尝试 fallback
    assert host_fn.call_count == 1
    # 异常包含等价候选信息
    assert exc_info.value.instance_id == "test"
    assert len(exc_info.value.equivalent_candidates) == 1


def test_disabled_policy_no_fallback() -> None:
    """Gate-C C.07：disabled 策略 → 不降级，直接抛出原始错误。"""
    from local_webpage_access.lifecycle import _try_host_with_fallback

    manifest = _mk_container_manifest(
        fallbacks=[
            {
                "kind": "node",
                "runtime": "docker-compose",
                "servingMode": "container",
                "confidenceTier": "fallback",
            }
        ]
    )
    host_fn = MagicMock(side_effect=HostingError("build failed"))
    mock_ws = MagicMock()
    mock_ws.app_manifest_path.return_value = Path("/tmp/manifest.json")

    with pytest.raises(HostingError, match="build failed"):
        _try_host_with_fallback(
            mock_ws,
            MagicMock(),
            MagicMock(),
            "test",
            manifest,
            host_fn,
            fallback_policy="disabled",
        )
    assert host_fn.call_count == 1


def test_all_candidates_fail_produces_diagnosis() -> None:
    """全部候选失败 → Layer 4 诊断报告写入 + HostingError。"""
    from local_webpage_access.lifecycle import _try_host_with_fallback

    manifest = _mk_container_manifest(
        fallbacks=[
            {
                "kind": "node",
                "runtime": "docker-compose",
                "servingMode": "container",
                "confidenceTier": "fallback",
                "entry": {"install": "npm ci", "start": "node server.js"},
            },
            {
                "kind": "python",
                "runtime": "docker-compose",
                "servingMode": "container",
                "confidenceTier": "fallback",
                "entry": {"install": "pip install -r requirements.txt", "start": "python app.py"},
            },
        ]
    )

    host_fn = MagicMock(side_effect=HostingError("always fails"))
    mock_ws = MagicMock()
    mock_ws.app_manifest_path.return_value = Path("/tmp/manifest.json")
    mock_reg = MagicMock()

    with pytest.raises(HostingError, match="所有候选均部署失败"):
        _try_host_with_fallback(
            mock_ws,
            MagicMock(),
            mock_reg,
            "test",
            manifest,
            host_fn,
            fallback_policy="auto-equivalent",
        )
    # top-1 + 2 fallbacks = 3 calls
    assert host_fn.call_count == 3
    # 诊断写入
    assert mock_reg.update_status.called
    assert mock_reg.add_event.called


def test_max_fallback_candidates_limit() -> None:
    """最多尝试 _MAX_FALLBACK_CANDIDATES 个候选。"""
    from local_webpage_access.lifecycle import (
        _MAX_FALLBACK_CANDIDATES,
        _try_host_with_fallback,
    )

    # 提供超过限制的 fallback 候选
    many_fallbacks = [
        {
            "kind": "node",
            "runtime": "docker-compose",
            "servingMode": "container",
            "confidenceTier": "fallback",
            "entry": {"start": f"node server{i}.js"},
        }
        for i in range(10)
    ]
    manifest = _mk_container_manifest(fallbacks=many_fallbacks)
    host_fn = MagicMock(side_effect=HostingError("fail"))
    mock_ws = MagicMock()
    mock_ws.app_manifest_path.return_value = Path("/tmp/manifest.json")
    mock_reg = MagicMock()

    with pytest.raises(HostingError):
        _try_host_with_fallback(
            mock_ws,
            MagicMock(),
            mock_reg,
            "test",
            manifest,
            host_fn,
            fallback_policy="auto-equivalent",
        )
    # top-1 + _MAX_FALLBACK_CANDIDATES
    assert host_fn.call_count == 1 + _MAX_FALLBACK_CANDIDATES


# ---- 能力守恒测试（IMP-058 §6.1.1） ------------------------------------------


def test_backend_rejects_static_fallback() -> None:
    """IMP-058 §6.1.1：后端候选失败后不得降级到静态候选。"""
    from local_webpage_access.lifecycle import _try_host_with_fallback

    # top-1 是 python 容器（backend），fallback 是 static（index.html 兜底候选）
    manifest = _mk_container_manifest(
        fallbacks=[
            {
                "kind": "static",
                "runtime": "shared_static",
                "servingMode": "shared_static",
                "form": "static",
                "confidenceTier": "fallback",
            }
        ]
    )
    host_fn = MagicMock(side_effect=HostingError("backend build failed"))
    mock_ws = MagicMock()
    mock_ws.app_manifest_path.return_value = Path("/tmp/manifest.json")

    with pytest.raises(HostingError, match="backend build failed"):
        _try_host_with_fallback(
            mock_ws,
            MagicMock(),
            MagicMock(),
            "test",
            manifest,
            host_fn,
            fallback_policy="auto-equivalent",
        )
    # host_fn 只调用一次（top-1），不尝试 static fallback
    assert host_fn.call_count == 1


def test_backend_allows_equivalent_backend_fallback() -> None:
    """IMP-058 §6.1.1：后端候选可降级到同族后端候选（python→node）。"""
    from local_webpage_access.lifecycle import _try_host_with_fallback

    success_manifest = _mk_container_manifest()
    # top-1 是 python 容器（backend），fallback 是 node 容器（同为 backend）
    manifest = _mk_container_manifest(
        fallbacks=[
            {
                "kind": "node",
                "runtime": "docker_compose",
                "servingMode": "container",
                "form": "backend-container",
                "confidenceTier": "fallback",
                "entry": {"install": "npm ci", "start": "node server.js"},
            }
        ]
    )
    host_fn = MagicMock(side_effect=[HostingError("python failed"), success_manifest])

    result = _try_host_with_fallback(
        MagicMock(),
        MagicMock(),
        MagicMock(),
        "test",
        manifest,
        host_fn,
        fallback_policy="auto-equivalent",
    )
    assert result is success_manifest
    # top-1 + 1 等价 backend fallback = 2 calls
    assert host_fn.call_count == 2


def test_capability_family_classification() -> None:
    """能力族分类辅助函数覆盖各 kind/servingMode 组合。"""
    from local_webpage_access.lifecycle import (
        _candidate_capability_family,
        _dict_capability_family,
        _capabilities_equivalent,
    )

    # manifest 版本
    backend_m = _mk_container_manifest()
    assert _candidate_capability_family(backend_m) == "backend"

    static_m = _mk_container_manifest()
    static_m.servingMode = ServingMode.SHARED_STATIC
    static_m.kind = Kind.STATIC
    assert _candidate_capability_family(static_m) == "static"

    # dict 版本（有 form）
    assert _dict_capability_family({"kind": "python", "form": "backend-container"}) == "backend"
    assert _dict_capability_family({"kind": "static", "form": "static"}) == "static"
    assert _dict_capability_family({"kind": "node", "form": "frontend-static"}) == "frontend"

    # dict 版本（无 form，用 servingMode 兜底）
    assert _dict_capability_family({"kind": "python", "servingMode": "container"}) == "backend"
    assert _dict_capability_family({"kind": "static", "servingMode": "shared_static"}) == "static"

    # 等价性
    assert _capabilities_equivalent("backend", "backend") is True
    assert _capabilities_equivalent("backend", "static") is False
    assert _capabilities_equivalent("backend", "frontend") is False
    assert _capabilities_equivalent("static", "frontend") is True


# ---- _apply_candidate_and_host 测试 ----------------------------------------


def test_apply_candidate_updates_manifest() -> None:
    """fallback 候选应用到 manifest 后字段正确。"""
    from local_webpage_access.lifecycle import _apply_candidate_and_host

    manifest = _mk_container_manifest()
    candidate = {
        "kind": "node",
        "runtime": "docker_compose",
        "servingMode": "container",
        "confidenceTier": "fallback",
        "entry": {"install": "npm ci", "start": "node server.js"},
        "sourceSubdir": "backend",
    }
    mock_ws = MagicMock()
    mock_ws.app_manifest_path.return_value = Path("/tmp/manifest.json")
    result_manifest = _mk_container_manifest()
    host_fn = MagicMock(return_value=result_manifest)

    result = _apply_candidate_and_host(
        mock_ws, MagicMock(), MagicMock(), "test", manifest, candidate, host_fn
    )
    assert result is result_manifest
    assert host_fn.call_count == 1


# ---- _write_diagnosis 测试 --------------------------------------------------


def test_write_diagnosis_updates_status_and_events() -> None:
    """诊断报告写入 manifest.lastError + registry.update_status + add_event。"""
    from local_webpage_access.lifecycle import _write_diagnosis
    from local_webpage_access.models import (
        CandidateDiagnosis,
        DiagnosisReport,
        VerificationResult,
    )

    report = DiagnosisReport(
        instanceId="test-inst",
        overallStatus="failed",
        candidatesTried=[
            CandidateDiagnosis(
                candidateIndex=0,
                candidateTier="primary",
                failureLayer="build",
                failureReason="Dockerfile syntax error",
                fixSuggestion="fix Dockerfile",
                verification=VerificationResult(
                    candidateIndex=0,
                    candidateTier="primary",
                    buildSucceeded=False,
                    error="Dockerfile syntax error",
                ),
            ),
        ],
        recommendedAction="fix Dockerfile",
    )
    mock_ws = MagicMock()
    mock_ws.app_manifest_path.return_value = Path("/tmp/manifest.json")
    mock_reg = MagicMock()

    _write_diagnosis(mock_ws, mock_reg, "test-inst", report)

    mock_reg.update_status.assert_called_once()
    args = mock_reg.update_status.call_args
    assert args[0][0] == "test-inst"
    assert args[1]["last_error"] is not None

    mock_reg.add_event.assert_called_once()
    event_args = mock_reg.add_event.call_args
    assert "diagnosis" in str(event_args)


# ---- api_probe 测试 (C.04) -------------------------------------------------


def test_api_probe_constants() -> None:
    """API 探测路径常量存在且非空。"""
    from local_webpage_access.health import _API_PROBE_PATHS

    assert len(_API_PROBE_PATHS) > 0
    assert "/health" in _API_PROBE_PATHS
    assert "/api/" in _API_PROBE_PATHS


# ---- 模型测试 (C.01) --------------------------------------------------------


def test_verification_result_creation() -> None:
    """VerificationResult 可正常创建和序列化。"""
    from local_webpage_access.models import VerificationResult

    vr = VerificationResult(
        candidateIndex=0,
        candidateTier="primary",
        buildSucceeded=True,
        healthCheckPassed=True,
        apiProbePassed=True,
        durationSeconds=45.2,
    )
    d = vr.model_dump()
    assert d["candidateIndex"] == 0
    assert d["buildSucceeded"] is True
    assert d["durationSeconds"] == 45.2


def test_diagnosis_report_creation() -> None:
    """DiagnosisReport 可正常创建。"""
    from local_webpage_access.models import (
        CandidateDiagnosis,
        DiagnosisReport,
    )

    report = DiagnosisReport(
        instanceId="test",
        overallStatus="failed",
        candidatesTried=[
            CandidateDiagnosis(
                candidateIndex=0,
                failureLayer="build",
                failureReason="error",
            ),
        ],
    )
    assert len(report.candidatesTried) == 1
    assert report.overallStatus == "failed"
    d = report.model_dump()
    assert "candidatesTried" in d


def test_candidate_diagnosis_with_verification() -> None:
    """CandidateDiagnosis 嵌套 VerificationResult。"""
    from local_webpage_access.models import (
        CandidateDiagnosis,
        VerificationResult,
    )

    cd = CandidateDiagnosis(
        candidateIndex=1,
        candidateTier="fallback",
        failureLayer="health",
        failureReason="timeout",
        verification=VerificationResult(
            candidateIndex=1,
            candidateTier="fallback",
            buildSucceeded=True,
            healthCheckPassed=False,
        ),
    )
    assert cd.verification is not None
    assert cd.verification.buildSucceeded is True
    assert cd.verification.healthCheckPassed is False


# ---- C.R01：部署计划执行测试 --------------------------------------------------


def _mk_plan_manifest(
    *,
    instance_id: str = "test-plan",
    plans: list[dict] | None = None,
    candidates: list[dict] | None = None,
    capability_contract: dict | None = None,
) -> InstanceManifest:
    """构造带 deploymentPlans 的 manifest（C.R01）。"""
    manifest = InstanceManifest(
        id=instance_id,
        name="Plan Instance",
        version="1",
        kind=Kind.PYTHON,
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        sourceZipPath="/tmp/test.zip",
        appPath="/tmp/current",
        container={
            "projectName": f"lwa-{instance_id}",
            "internalPort": 8000,
            "composePath": "/tmp/compose.yml",
            "dockerfilePath": "/tmp/Dockerfile",
        },
        deploymentPlans=plans or [],
        deploymentCandidates=candidates or [],
        capabilityContract=capability_contract,
    )
    return manifest


def _mk_backend_plan(
    *,
    plan_id: str = "plan-python-001",
    tier: str = "primary",
    kind: str = "python",
    contract: dict | None = None,
) -> dict:
    """构造一个 DeploymentPlan dict。"""
    if contract is None:
        contract = {
            "servesUi": False,
            "servesApi": True,
            "requiresDatabase": True,
            "requiresMigrations": False,
        }
    comp_id = f"{kind}-001"
    return {
        "planId": plan_id,
        "confidenceTier": tier,
        "capabilityContract": contract,
        "components": [
            {
                "componentId": comp_id,
                "role": "runtime",
                "sourceSubdir": None,
                "startCommand": {"shell": f"{kind} app.py"}
                if kind != "node"
                else {"shell": "node server.js"},
                "buildCommand": {"shell": "pip install -r requirements.txt"}
                if kind != "node"
                else {"shell": "npm ci"},
                "internalPort": 8000,
            }
        ],
    }


def test_get_fallback_plans_reads_deployment_plans() -> None:
    """C.R01：_get_fallback_plans 优先从 deploymentPlans 读取。"""
    from local_webpage_access.lifecycle import _get_fallback_plans

    primary = _mk_backend_plan(tier="primary")
    alt = _mk_backend_plan(plan_id="plan-node-001", tier="alternate", kind="node")
    manifest = _mk_plan_manifest(plans=[primary, alt])

    result = _get_fallback_plans(manifest)
    # primary 被过滤，只返回 alternate
    assert len(result) == 1
    assert result[0]["planId"] == "plan-node-001"


def test_get_fallback_plans_falls_back_to_candidates() -> None:
    """C.R01：无 deploymentPlans 时回退到 deploymentCandidates（向后兼容）。"""
    from local_webpage_access.lifecycle import _get_fallback_plans

    manifest = _mk_plan_manifest(
        plans=[],
        candidates=[{"kind": "node", "runtime": "docker-compose"}],
    )
    result = _get_fallback_plans(manifest)
    assert len(result) == 1
    assert result[0]["kind"] == "node"


def test_get_fallback_plans_empty_when_only_primary() -> None:
    """C.R01：只有 primary plan 时无可用 fallback。"""
    from local_webpage_access.lifecycle import _get_fallback_plans

    primary = _mk_backend_plan(tier="primary")
    manifest = _mk_plan_manifest(plans=[primary])
    result = _get_fallback_plans(manifest)
    assert result == []


def test_get_fallback_plans_filters_diagnostic() -> None:
    """C.R01：diagnostic tier 的计划不可降级。"""
    from local_webpage_access.lifecycle import _get_fallback_plans

    primary = _mk_backend_plan(tier="primary")
    diagnostic = _mk_backend_plan(plan_id="plan-static-001", tier="diagnostic", kind="static")
    manifest = _mk_plan_manifest(plans=[primary, diagnostic])
    result = _get_fallback_plans(manifest)
    assert result == []


def test_plan_to_candidate_dict_extracts_runtime_component() -> None:
    """C.R01：_plan_to_candidate_dict 正确提取 runtime 组件。"""
    from local_webpage_access.lifecycle import _plan_to_candidate_dict

    plan = _mk_backend_plan(kind="python")
    result = _plan_to_candidate_dict(plan)
    assert result["kind"] == "python"
    assert result["runtime"] == "docker-compose"
    assert result["servingMode"] == "container"
    assert "start" in result["entry"]
    assert "app.py" in result["entry"]["start"]


def test_plan_to_candidate_dict_node_kind() -> None:
    """C.R01：_plan_to_candidate_dict 正确识别 node 组件。"""
    from local_webpage_access.lifecycle import _plan_to_candidate_dict

    plan = _mk_backend_plan(kind="node")
    result = _plan_to_candidate_dict(plan)
    assert result["kind"] == "node"
    assert "server.js" in result["entry"]["start"]


def test_apply_plan_and_host_delegates_to_candidate_for_flat_dict() -> None:
    """C.R01：扁平候选（无 components）直接走 _apply_candidate_and_host。"""
    from local_webpage_access.lifecycle import _apply_plan_and_host

    manifest = _mk_plan_manifest()
    flat_candidate = {
        "kind": "node",
        "runtime": "docker-compose",
        "entry": {"start": "node server.js"},
    }
    result_manifest = _mk_plan_manifest()
    host_fn = MagicMock(return_value=result_manifest)
    mock_ws = MagicMock()
    mock_ws.app_manifest_path.return_value = Path("/tmp/manifest.json")

    result = _apply_plan_and_host(
        mock_ws, MagicMock(), MagicMock(), "test", manifest, flat_candidate, host_fn
    )
    assert result is result_manifest


def test_apply_plan_and_host_extracts_plan_and_updates_selected_plan_id() -> None:
    """C.R01：DeploymentPlan 通过 _apply_plan_and_host 应用后 selectedPlanId 更新。"""
    from local_webpage_access.lifecycle import _apply_plan_and_host

    manifest = _mk_plan_manifest()
    plan = _mk_backend_plan(plan_id="plan-node-001", tier="alternate", kind="node")
    result_manifest = _mk_plan_manifest()
    host_fn = MagicMock(return_value=result_manifest)
    mock_ws = MagicMock()
    mock_ws.app_manifest_path.return_value = Path("/tmp/manifest.json")

    result = _apply_plan_and_host(
        mock_ws, MagicMock(), MagicMock(), "test", manifest, plan, host_fn
    )
    assert result is result_manifest
    assert manifest.selectedPlanId == "plan-node-001"


def test_apply_plan_and_host_updates_capability_contract() -> None:
    """C.R01：应用新计划时 capabilityContract 更新为计划携带的契约。"""
    from local_webpage_access.lifecycle import _apply_plan_and_host

    manifest = _mk_plan_manifest()
    new_contract = {
        "servesUi": True,
        "servesApi": True,
        "requiresDatabase": False,
        "requiresMigrations": False,
    }
    plan = _mk_backend_plan(
        plan_id="plan-node-002",
        tier="alternate",
        kind="node",
        contract=new_contract,
    )
    result_manifest = _mk_plan_manifest()
    host_fn = MagicMock(return_value=result_manifest)
    mock_ws = MagicMock()
    mock_ws.app_manifest_path.return_value = Path("/tmp/manifest.json")

    _apply_plan_and_host(mock_ws, MagicMock(), MagicMock(), "test", manifest, plan, host_fn)
    assert manifest.capabilityContract == new_contract


def test_try_host_with_fallback_uses_deployment_plans() -> None:
    """C.R01：_try_host_with_fallback 优先使用 deploymentPlans 进行降级。"""
    from local_webpage_access.lifecycle import _try_host_with_fallback

    primary_contract = {
        "servesUi": False,
        "servesApi": True,
        "requiresDatabase": True,
        "requiresMigrations": False,
    }
    alt_contract = {
        "servesUi": False,
        "servesApi": True,
        "requiresDatabase": True,
        "requiresMigrations": False,
    }
    primary = _mk_backend_plan(tier="primary", contract=primary_contract)
    alt = _mk_backend_plan(
        plan_id="plan-node-001",
        tier="alternate",
        kind="node",
        contract=alt_contract,
    )
    manifest = _mk_plan_manifest(
        plans=[primary, alt],
        capability_contract=primary_contract,
    )

    success_manifest = _mk_plan_manifest()
    call_count = [0]

    def mock_host(ws, cfg, reg, iid):
        call_count[0] += 1
        if call_count[0] == 1:
            raise HostingError("python build failed")
        return success_manifest

    host_fn = MagicMock(side_effect=mock_host)
    result = _try_host_with_fallback(
        MagicMock(),
        MagicMock(),
        MagicMock(),
        "test",
        manifest,
        host_fn,
        fallback_policy="auto-equivalent",
    )
    assert result is success_manifest
    assert host_fn.call_count == 2  # top-1 + 1 fallback


# ---- C.R02：完整 CapabilityContract 对比测试 -----------------------------------


def test_contracts_equivalent_same_contract() -> None:
    """C.R02：相同契约 -> 等价。"""
    from local_webpage_access.lifecycle import _contracts_equivalent

    contract = {
        "servesApi": True,
        "requiresDatabase": True,
        "requiresMigrations": False,
        "servesUi": False,
    }
    assert _contracts_equivalent(contract, dict(contract)) is True


def test_contracts_equivalent_different_api() -> None:
    """C.R02：servesApi 不同 -> 不等价（API 能力不可丢失）。"""
    from local_webpage_access.lifecycle import _contracts_equivalent

    a = {
        "servesApi": True,
        "requiresDatabase": False,
        "requiresMigrations": False,
        "servesUi": False,
    }
    b = {
        "servesApi": False,
        "requiresDatabase": False,
        "requiresMigrations": False,
        "servesUi": False,
    }
    assert _contracts_equivalent(a, b) is False


def test_contracts_equivalent_different_database() -> None:
    """C.R02：requiresDatabase 不同 -> 不等价（数据库能力不可丢失）。"""
    from local_webpage_access.lifecycle import _contracts_equivalent

    a = {
        "servesApi": True,
        "requiresDatabase": True,
        "requiresMigrations": False,
        "servesUi": False,
    }
    b = {
        "servesApi": True,
        "requiresDatabase": False,
        "requiresMigrations": False,
        "servesUi": False,
    }
    assert _contracts_equivalent(a, b) is False


def test_contracts_equivalent_different_migrations() -> None:
    """C.R02：requiresMigrations 不同 -> 不等价。"""
    from local_webpage_access.lifecycle import _contracts_equivalent

    a = {"servesApi": True, "requiresDatabase": True, "requiresMigrations": True, "servesUi": False}
    b = {
        "servesApi": True,
        "requiresDatabase": True,
        "requiresMigrations": False,
        "servesUi": False,
    }
    assert _contracts_equivalent(a, b) is False


def test_contracts_equivalent_different_ui() -> None:
    """C.R02：servesUi 不同 -> 不等价。"""
    from local_webpage_access.lifecycle import _contracts_equivalent

    a = {
        "servesApi": False,
        "requiresDatabase": False,
        "requiresMigrations": False,
        "servesUi": True,
    }
    b = {
        "servesApi": False,
        "requiresDatabase": False,
        "requiresMigrations": False,
        "servesUi": False,
    }
    assert _contracts_equivalent(a, b) is False


def test_contracts_equivalent_empty_contract() -> None:
    """C.R02：空契约 -> 保守返回 False（调用方回退到 family 比较）。"""
    from local_webpage_access.lifecycle import _contracts_equivalent

    assert _contracts_equivalent({}, {}) is False
    assert _contracts_equivalent({"servesApi": True}, {}) is False


def test_contract_diff_describes_differences() -> None:
    """C.R02：_contract_diff 正确描述差异。"""
    from local_webpage_access.lifecycle import _contract_diff

    a = {
        "servesApi": True,
        "requiresDatabase": True,
        "requiresMigrations": False,
        "servesUi": False,
    }
    b = {
        "servesApi": True,
        "requiresDatabase": False,
        "requiresMigrations": False,
        "servesUi": True,
    }
    diff = _contract_diff(a, b)
    assert "requiresDatabase: True -> False" in diff
    assert "servesUi: False -> True" in diff
    assert "servesApi" not in diff  # 相同的字段不出现在 diff 中


def test_manifest_capability_contract_from_manifest_field() -> None:
    """C.R02：_manifest_capability_contract 优先使用 manifest.capabilityContract。"""
    from local_webpage_access.lifecycle import _manifest_capability_contract

    contract = {
        "servesApi": True,
        "requiresDatabase": True,
        "requiresMigrations": False,
        "servesUi": False,
    }
    manifest = _mk_plan_manifest(capability_contract=contract)
    result = _manifest_capability_contract(manifest)
    assert result == contract


def test_manifest_capability_contract_from_plans() -> None:
    """C.R02：无 capabilityContract 字段时从 deploymentPlans[0] 提取。"""
    from local_webpage_access.lifecycle import _manifest_capability_contract

    contract = {
        "servesApi": True,
        "requiresDatabase": False,
        "requiresMigrations": False,
        "servesUi": False,
    }
    primary = _mk_backend_plan(tier="primary", contract=contract)
    manifest = _mk_plan_manifest(plans=[primary], capability_contract=None)
    manifest.capabilityContract = None
    result = _manifest_capability_contract(manifest)
    assert result == contract


def test_cr02_rejects_database_loss_fallback() -> None:
    """C.R02：requiresDatabase=True 的计划不得降级到 requiresDatabase=False。"""
    from local_webpage_access.lifecycle import _try_host_with_fallback

    primary_contract = {
        "servesApi": True,
        "requiresDatabase": True,
        "requiresMigrations": False,
        "servesUi": False,
    }
    alt_contract = {
        "servesApi": True,
        "requiresDatabase": False,  # 丢失数据库能力
        "requiresMigrations": False,
        "servesUi": False,
    }
    primary = _mk_backend_plan(tier="primary", contract=primary_contract)
    alt = _mk_backend_plan(
        plan_id="plan-nodb-001",
        tier="alternate",
        kind="node",
        contract=alt_contract,
    )
    manifest = _mk_plan_manifest(
        plans=[primary, alt],
        capability_contract=primary_contract,
    )
    host_fn = MagicMock(side_effect=HostingError("build failed"))
    mock_ws = MagicMock()
    mock_ws.app_manifest_path.return_value = Path("/tmp/manifest.json")

    with pytest.raises(HostingError, match="build failed"):
        _try_host_with_fallback(
            mock_ws,
            MagicMock(),
            MagicMock(),
            "test",
            manifest,
            host_fn,
            fallback_policy="auto-equivalent",
        )
    # top-1 only -- alt 被能力守恒过滤
    assert host_fn.call_count == 1


def test_cr02_rejects_api_loss_fallback() -> None:
    """C.R02：servesApi=True 的计划不得降级到 servesApi=False。"""
    from local_webpage_access.lifecycle import _try_host_with_fallback

    primary_contract = {
        "servesApi": True,
        "requiresDatabase": False,
        "requiresMigrations": False,
        "servesUi": True,
    }
    alt_contract = {
        "servesApi": False,  # 丢失 API 能力
        "requiresDatabase": False,
        "requiresMigrations": False,
        "servesUi": True,
    }
    primary = _mk_backend_plan(tier="primary", contract=primary_contract)
    alt = _mk_backend_plan(
        plan_id="plan-static-001",
        tier="alternate",
        kind="static",
        contract=alt_contract,
    )
    manifest = _mk_plan_manifest(
        plans=[primary, alt],
        capability_contract=primary_contract,
    )
    host_fn = MagicMock(side_effect=HostingError("build failed"))
    mock_ws = MagicMock()
    mock_ws.app_manifest_path.return_value = Path("/tmp/manifest.json")

    with pytest.raises(HostingError, match="build failed"):
        _try_host_with_fallback(
            mock_ws,
            MagicMock(),
            MagicMock(),
            "test",
            manifest,
            host_fn,
            fallback_policy="auto-equivalent",
        )
    assert host_fn.call_count == 1


def test_cr02_allows_equivalent_contract_fallback() -> None:
    """C.R02：完整契约等价时允许降级（python+DB+API -> node+DB+API）。"""
    from local_webpage_access.lifecycle import _try_host_with_fallback

    primary_contract = {
        "servesApi": True,
        "requiresDatabase": True,
        "requiresMigrations": False,
        "servesUi": False,
    }
    alt_contract = {
        "servesApi": True,
        "requiresDatabase": True,
        "requiresMigrations": False,
        "servesUi": False,
    }
    primary = _mk_backend_plan(tier="primary", contract=primary_contract)
    alt = _mk_backend_plan(
        plan_id="plan-node-001",
        tier="alternate",
        kind="node",
        contract=alt_contract,
    )
    manifest = _mk_plan_manifest(
        plans=[primary, alt],
        capability_contract=primary_contract,
    )

    success_manifest = _mk_plan_manifest()
    call_count = [0]

    def mock_host(ws, cfg, reg, iid):
        call_count[0] += 1
        if call_count[0] == 1:
            raise HostingError("python build failed")
        return success_manifest

    host_fn = MagicMock(side_effect=mock_host)
    result = _try_host_with_fallback(
        MagicMock(),
        MagicMock(),
        MagicMock(),
        "test",
        manifest,
        host_fn,
        fallback_policy="auto-equivalent",
    )
    assert result is success_manifest
    assert host_fn.call_count == 2


def test_cr02_falls_back_to_family_when_no_contract() -> None:
    """C.R02：无 contract 时回退到 3-value family 比较（向后兼容）。"""
    from local_webpage_access.lifecycle import _try_host_with_fallback

    # 使用 deploymentCandidates（无 contract），验证 family 回退仍然工作
    manifest = _mk_container_manifest(
        fallbacks=[
            {
                "kind": "node",
                "runtime": "docker-compose",
                "servingMode": "container",
                "form": "backend-container",
                "confidenceTier": "fallback",
                "entry": {"start": "node server.js"},
            }
        ]
    )
    # 确保 capabilityContract 为 None
    manifest.capabilityContract = None
    manifest.deploymentPlans = []

    success_manifest = _mk_container_manifest()
    host_fn = MagicMock(side_effect=[HostingError("python failed"), success_manifest])
    result = _try_host_with_fallback(
        MagicMock(),
        MagicMock(),
        MagicMock(),
        "test",
        manifest,
        host_fn,
        fallback_policy="auto-equivalent",
    )
    assert result is success_manifest
    assert host_fn.call_count == 2


# ---- C.R04：完整回滚测试 -----------------------------------------------------


def test_snapshot_attempt_captures_manifest_fields(tmp_path: Path) -> None:
    """C.R04：_snapshot_attempt 捕获 manifest 关键字段。"""
    from local_webpage_access.lifecycle import _snapshot_attempt
    from local_webpage_access.paths import Workspace
    from local_webpage_access.models import (
        ContainerConfig,
        EntryConfig,
    )

    ws = Workspace(tmp_path)
    ws.ensure_app_dirs("test-snap")
    manifest = _mk_container_manifest(instance_id="test-snap")
    manifest.container = ContainerConfig(
        projectName="lwa-test-snap",
        internalPort=8000,
        composePath=str(ws.app_compose_path("test-snap")),
        dockerfilePath=str(ws.app_dockerfile_path("test-snap")),
    )
    manifest.entry = EntryConfig(start="python app.py")
    manifest.selectedPlanId = "plan-001"
    manifest.capabilityContract = {"servesApi": True, "requiresDatabase": True}

    snapshot = _snapshot_attempt(ws, MagicMock(), "test-snap", manifest)
    mf = snapshot["manifestFields"]
    assert mf["selectedPlanId"] == "plan-001"
    assert mf["kind"] == "python"
    assert mf["entry"]["start"] == "python app.py"
    assert mf["capabilityContract"]["servesApi"] is True


def test_snapshot_attempt_captures_files(tmp_path: Path) -> None:
    """C.R04：_snapshot_attempt 捕获生成文件内容。"""
    from local_webpage_access.lifecycle import _snapshot_attempt
    from local_webpage_access.paths import Workspace

    ws = Workspace(tmp_path)
    ws.ensure_app_dirs("test-files")
    compose_path = ws.app_compose_path("test-files")
    compose_path.parent.mkdir(parents=True, exist_ok=True)
    compose_path.write_text("version: '3'\nservices:\n  app:\n    build: .")
    ws.app_dockerfile_path("test-files").write_text("FROM python:3.12")

    manifest = _mk_container_manifest(instance_id="test-files")
    snapshot = _snapshot_attempt(ws, MagicMock(), "test-files", manifest)
    assert "compose.yaml" in snapshot["files"]
    assert "Dockerfile" in snapshot["files"]
    assert "version: '3'" in snapshot["files"]["compose.yaml"]
    assert ".env" not in snapshot["files"]


def test_restore_from_snapshot_restores_files(tmp_path: Path) -> None:
    """C.R04：_restore_from_snapshot 恢复生成文件内容。"""
    from local_webpage_access.lifecycle import _restore_from_snapshot
    from local_webpage_access.paths import Workspace

    ws = Workspace(tmp_path)
    ws.ensure_app_dirs("test-restore")
    compose_path = ws.app_compose_path("test-restore")
    compose_path.parent.mkdir(parents=True, exist_ok=True)
    compose_path.write_text("original compose")
    ws.app_dockerfile_path("test-restore").write_text("original Dockerfile")

    manifest = _mk_container_manifest(instance_id="test-restore")
    snapshot = {
        "manifestFields": {},
        "files": {
            "compose.yaml": "original compose",
            "Dockerfile": "original Dockerfile",
        },
    }

    compose_path.write_text("modified compose")
    ws.app_dockerfile_path("test-restore").write_text("modified Dockerfile")

    restored, residuals = _restore_from_snapshot(
        ws, MagicMock(), "test-restore", manifest, snapshot
    )
    assert "file:Dockerfile" in restored
    assert "file:compose.yaml" in restored
    assert compose_path.read_text() == "original compose"
    assert ws.app_dockerfile_path("test-restore").read_text() == "original Dockerfile"
    assert residuals == []


def test_restore_from_snapshot_removes_new_files(tmp_path: Path) -> None:
    """C.R04：快照中不存在的文件在恢复时被删除。"""
    from local_webpage_access.lifecycle import _restore_from_snapshot
    from local_webpage_access.paths import Workspace

    ws = Workspace(tmp_path)
    ws.ensure_app_dirs("test-remove")
    compose_path = ws.app_compose_path("test-remove")
    compose_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {"manifestFields": {}, "files": {}}

    env_path = ws.app_env_path("test-remove")
    env_path.write_text("DATABASE_URL=sqlite:///test.db")

    manifest = _mk_container_manifest(instance_id="test-remove")
    restored, residuals = _restore_from_snapshot(ws, MagicMock(), "test-remove", manifest, snapshot)
    assert "file:.env:removed" in restored
    assert not env_path.exists()


def test_restore_from_snapshot_restores_manifest_fields(tmp_path: Path) -> None:
    """C.R04：_restore_from_snapshot 恢复 manifest 关键字段。"""
    from local_webpage_access.lifecycle import _restore_from_snapshot
    from local_webpage_access.paths import Workspace
    from local_webpage_access.models import EntryConfig, Kind

    ws = Workspace(tmp_path)
    ws.ensure_app_dirs("test-mf")
    manifest = _mk_container_manifest(instance_id="test-mf")
    manifest.selectedPlanId = "plan-new"
    manifest.kind = Kind.NODE
    manifest.entry = EntryConfig(start="node server.js")

    snapshot = {
        "manifestFields": {
            "selectedPlanId": "plan-original",
            "kind": "python",
            "runtime": "docker-compose",
            "servingMode": "container",
            "entry": {"start": "python app.py"},
            "sourceSubdir": None,
            "container": None,
            "capabilityContract": {"servesApi": True},
        },
        "files": {},
    }

    restored, residuals = _restore_from_snapshot(ws, MagicMock(), "test-mf", manifest, snapshot)
    assert "manifest" in restored
    assert manifest.selectedPlanId == "plan-original"
    assert manifest.kind == Kind.PYTHON
    assert manifest.entry.start == "python app.py"
    assert manifest.capabilityContract == {"servesApi": True}


def test_snapshot_attempt_captures_alias_fragment_and_route(tmp_path: Path) -> None:
    """issue #21 纵深防御：快照须包含 aliases/<id>.conf 与 network.routeMode/routeHost。

    根因已由 aliasLiveVerifiedAt 处理；本项防止将来新回滚路径漏掉别名片段。
    """
    from local_webpage_access.lifecycle import _snapshot_attempt
    from local_webpage_access.models import ContainerConfig
    from local_webpage_access.paths import Workspace

    ws = Workspace(tmp_path)
    ws.ensure_app_dirs("prd-review")
    alias_path = ws.app_alias_config("prd-review")
    alias_path.parent.mkdir(parents=True, exist_ok=True)
    alias_body = "handle_path /prd-review/* {\n\treverse_proxy 127.0.0.1:18001\n}\n"
    alias_path.write_text(alias_body, encoding="utf-8")

    manifest = _mk_container_manifest(instance_id="prd-review")
    manifest.container = ContainerConfig(
        projectName="lwa-prd-review",
        internalPort=8000,
        composePath=str(ws.app_compose_path("prd-review")),
        dockerfilePath=str(ws.app_dockerfile_path("prd-review")),
        routeMode="name",
        routeHost="prd-review",
    )
    manifest.network.routeMode = "name"
    manifest.network.routeHost = "prd-review"

    snapshot = _snapshot_attempt(ws, MagicMock(), "prd-review", manifest)
    assert snapshot["files"]["alias.conf"] == alias_body
    net = snapshot["manifestFields"]["network"]
    assert net["routeMode"] == "name"
    assert net["routeHost"] == "prd-review"
    assert snapshot["manifestFields"]["container"]["routeMode"] == "name"
    assert snapshot["manifestFields"]["container"]["routeHost"] == "prd-review"


def test_restore_from_snapshot_restores_alias_fragment_and_route(tmp_path: Path) -> None:
    """issue #21：回滚须写回别名片段，并把 network/container 路由字段复原。"""
    from local_webpage_access.lifecycle import _restore_from_snapshot
    from local_webpage_access.models import ContainerConfig
    from local_webpage_access.paths import Workspace

    ws = Workspace(tmp_path)
    ws.ensure_app_dirs("prd-review")
    alias_path = ws.app_alias_config("prd-review")
    alias_path.parent.mkdir(parents=True, exist_ok=True)
    original = "handle_path /prd-review/* {\n\treverse_proxy 127.0.0.1:18001\n}\n"
    alias_path.write_text("corrupted alias", encoding="utf-8")

    manifest = _mk_container_manifest(instance_id="prd-review")
    manifest.container = ContainerConfig(
        projectName="lwa-prd-review",
        internalPort=8000,
        composePath=str(ws.app_compose_path("prd-review")),
        dockerfilePath=str(ws.app_dockerfile_path("prd-review")),
        routeMode="port",
        routeHost=None,
    )
    manifest.network.routeMode = "port"
    manifest.network.routeHost = None

    snapshot = {
        "manifestFields": {
            "container": {
                "projectName": "lwa-prd-review",
                "internalPort": 8000,
                "composePath": str(ws.app_compose_path("prd-review")),
                "dockerfilePath": str(ws.app_dockerfile_path("prd-review")),
                "routeMode": "name",
                "routeHost": "prd-review",
            },
            "network": {"routeMode": "name", "routeHost": "prd-review"},
        },
        "files": {"alias.conf": original},
    }
    restored, residuals = _restore_from_snapshot(
        ws, MagicMock(), "prd-review", manifest, snapshot
    )
    assert residuals == []
    assert "file:alias.conf" in restored
    assert alias_path.read_text(encoding="utf-8") == original
    assert manifest.network.routeMode == "name"
    assert manifest.network.routeHost == "prd-review"
    assert manifest.container is not None
    assert manifest.container.routeMode == "name"
    assert manifest.container.routeHost == "prd-review"


def test_restore_from_snapshot_removes_new_alias_fragment(tmp_path: Path) -> None:
    """issue #21：快照无别名片段时，attempt 新建的 aliases/<id>.conf 应删除。"""
    from local_webpage_access.lifecycle import _restore_from_snapshot
    from local_webpage_access.paths import Workspace

    ws = Workspace(tmp_path)
    ws.ensure_app_dirs("fresh")
    alias_path = ws.app_alias_config("fresh")
    alias_path.parent.mkdir(parents=True, exist_ok=True)
    alias_path.write_text("new deferred alias", encoding="utf-8")

    manifest = _mk_container_manifest(instance_id="fresh")
    snapshot = {"manifestFields": {}, "files": {}}
    restored, residuals = _restore_from_snapshot(
        ws, MagicMock(), "fresh", manifest, snapshot
    )
    assert residuals == []
    assert "file:alias.conf:removed" in restored
    assert not alias_path.exists()


def test_rollback_with_snapshot_restores_files(tmp_path: Path) -> None:
    """C.R04：_rollback_attempt 带 snapshot 时恢复生成文件。"""
    from local_webpage_access.lifecycle import _rollback_attempt
    from local_webpage_access.paths import Workspace

    ws = Workspace(tmp_path)
    ws.ensure_app_dirs("test-rb")
    compose_path = ws.app_compose_path("test-rb")
    compose_path.parent.mkdir(parents=True, exist_ok=True)
    compose_path.write_text("original")
    ws.app_dockerfile_path("test-rb").write_text("original Dockerfile")

    manifest = _mk_container_manifest(instance_id="test-rb")
    snapshot = {
        "manifestFields": {},
        "files": {
            "compose.yaml": "original",
            "Dockerfile": "original Dockerfile",
        },
    }

    compose_path.write_text("modified")
    ws.app_dockerfile_path("test-rb").write_text("modified Dockerfile")

    result = _rollback_attempt(
        ws,
        MagicMock(),
        MagicMock(),
        "test-rb",
        manifest,
        attempt_id="attempt-test-rb-0",
        snapshot=snapshot,
    )
    assert compose_path.read_text() == "original"
    assert ws.app_dockerfile_path("test-rb").read_text() == "original Dockerfile"
    assert result.snapshotData == snapshot
    assert "file:compose.yaml" in result.rolledBackItems
    assert "file:Dockerfile" in result.rolledBackItems


def test_rollback_releases_only_new_ports(workspace, registry, config, monkeypatch) -> None:
    """BUG-510：回滚只释放本轮新端口，复用端口必须保留。"""
    from local_webpage_access.lifecycle import _rollback_attempt

    monkeypatch.setattr(
        "local_webpage_access.docker_runtime.DockerRuntime.is_running",
        lambda self, iid: False,
    )
    iid = "rb-ports"
    workspace.ensure_app_dirs(iid)
    manifest = _mk_container_manifest(instance_id=iid)
    registry.upsert_from_manifest(manifest)
    reused, fresh = 21010, 21011
    assert registry.allocate_port(iid, reused)
    assert registry.allocate_port(iid, fresh)

    result = _rollback_attempt(
        workspace,
        config,
        registry,
        iid,
        manifest,
        attempt_id="attempt-rb-ports-0",
        snapshot={"manifestFields": {}, "files": {}, "ports": [reused]},
    )
    assert registry.port_owner(reused) == iid
    assert registry.port_owner(fresh) is None
    assert "port" in result.rolledBackItems


def test_rollback_without_snapshot_no_file_restore(tmp_path: Path) -> None:
    """C.R04：无 snapshot 时不尝试恢复文件（向后兼容）。"""
    from local_webpage_access.lifecycle import _rollback_attempt
    from local_webpage_access.paths import Workspace

    ws = Workspace(tmp_path)
    ws.ensure_app_dirs("test-nosnap")
    manifest = _mk_container_manifest(instance_id="test-nosnap")

    result = _rollback_attempt(
        ws,
        MagicMock(),
        MagicMock(),
        "test-nosnap",
        manifest,
        attempt_id="attempt-test-nosnap-0",
        snapshot=None,
    )
    assert not any(item.startswith("file:") for item in result.rolledBackItems)
    assert result.snapshotData is None


def test_rollback_residuals_on_manifest_restore_failure(tmp_path: Path) -> None:
    """C.R04：manifest 恢复失败时记录残留项，rollbackSucceeded=False。"""
    from local_webpage_access.lifecycle import _rollback_attempt
    from local_webpage_access.paths import Workspace

    ws = Workspace(tmp_path)
    ws.ensure_app_dirs("test-residual")

    manifest = _mk_container_manifest(instance_id="test-residual")
    manifest.capabilityContract = {"requiresMigrations": True}

    # 快照中 manifest 字段恢复会触发异常（无效 container 字段）
    snapshot = {
        "manifestFields": {
            "container": {"invalid": "data"},
        },
        "files": {},
    }

    result = _rollback_attempt(
        ws,
        MagicMock(),
        MagicMock(),
        "test-residual",
        manifest,
        attempt_id="attempt-test-residual-0",
        snapshot=snapshot,
    )
    assert len(result.residualItems) > 0
    assert result.rollbackSucceeded is False
    assert result.automaticFallbackSafe is False


# ---- C.R05：SideEffectRecord 测试 --------------------------------------------


def test_side_effect_record_creation() -> None:
    """C.R05：SideEffectRecord 可正常创建和序列化。"""
    from local_webpage_access.models import SideEffectRecord

    record = SideEffectRecord(
        kind="migration",
        description="Alembic 迁移执行",
        intent="容器启动时执行 alembic upgrade head",
        executedAt="2026-08-12T00:00:00Z",
        result="succeeded",
        compensationMethod="alembic downgrade",
        autoRecoverable=False,
    )
    d = record.model_dump()
    assert d["kind"] == "migration"
    assert d["autoRecoverable"] is False
    assert d["result"] == "succeeded"


def test_side_effect_record_defaults() -> None:
    """C.R05：默认值--未知写入不可自动恢复。"""
    from local_webpage_access.models import SideEffectRecord

    record = SideEffectRecord(
        kind="unknown",
        description="未知副作用",
        intent="未知",
    )
    assert record.autoRecoverable is False  # 默认不可自动恢复
    assert record.result == "unknown"
    assert record.compensationMethod is None


def test_collect_side_effect_records_detects_migration() -> None:
    """C.R05：检测到 Alembic 迁移命令时生成副作用记录。"""
    from local_webpage_access.hosting import _collect_side_effect_records
    from local_webpage_access.models import EntryConfig

    manifest = _mk_container_manifest()
    manifest.entry = EntryConfig(
        start="alembic upgrade head && python -m uvicorn app:app --host 0.0.0.0 --port 8000",
    )

    records = _collect_side_effect_records(
        manifest,
        liveness_ok=True,
        verification_status="passed",
    )
    assert len(records) == 1
    assert records[0].kind == "migration"
    assert records[0].autoRecoverable is False
    assert "alembic" in records[0].description.lower()


def test_collect_side_effect_records_no_migration() -> None:
    """C.R05：无迁移命令时不生成副作用记录。"""
    from local_webpage_access.hosting import _collect_side_effect_records
    from local_webpage_access.models import EntryConfig

    manifest = _mk_container_manifest()
    manifest.entry = EntryConfig(start="python app.py")

    records = _collect_side_effect_records(
        manifest,
        liveness_ok=True,
        verification_status="passed",
    )
    assert len(records) == 0


def test_side_effects_auto_recoverable_all_recoverable() -> None:
    """C.R05：全部可恢复 -> True。"""
    from local_webpage_access.hosting import _side_effects_auto_recoverable
    from local_webpage_access.models import SideEffectRecord

    records = [
        SideEffectRecord(
            kind="hook", description="cache warm", intent="warm", autoRecoverable=True
        ),
    ]
    assert _side_effects_auto_recoverable(records) is True


def test_side_effects_auto_recoverable_has_non_recoverable() -> None:
    """C.R05：含不可恢复 -> False。"""
    from local_webpage_access.hosting import _side_effects_auto_recoverable
    from local_webpage_access.models import SideEffectRecord

    records = [
        SideEffectRecord(
            kind="hook", description="cache warm", intent="warm", autoRecoverable=True
        ),
        SideEffectRecord(
            kind="migration", description="alembic", intent="migrate", autoRecoverable=False
        ),
    ]
    assert _side_effects_auto_recoverable(records) is False


def test_side_effects_auto_recoverable_empty() -> None:
    """C.R05：空列表 -> True（无副作用）。"""
    from local_webpage_access.hosting import _side_effects_auto_recoverable

    assert _side_effects_auto_recoverable([]) is True


def test_rollback_with_non_recoverable_side_effects_unsafe(tmp_path: Path) -> None:
    """C.R05：有不可恢复的副作用时 automaticFallbackSafe=False。"""
    from local_webpage_access.lifecycle import _rollback_attempt
    from local_webpage_access.paths import Workspace

    ws = Workspace(tmp_path)
    ws.ensure_app_dirs("test-se")

    manifest = _mk_container_manifest(instance_id="test-se")
    # 不需要迁移的契约
    manifest.capabilityContract = {"requiresMigrations": False}

    se_records = [
        {
            "kind": "migration",
            "description": "Alembic 迁移已执行",
            "intent": "alembic upgrade head",
            "executedAt": "2026-08-12T00:00:00Z",
            "result": "succeeded",
            "autoRecoverable": False,
        }
    ]

    result = _rollback_attempt(
        ws,
        MagicMock(),
        MagicMock(),
        "test-se",
        manifest,
        attempt_id="attempt-test-se-0",
        snapshot=None,
        side_effect_records=se_records,
        side_effects_auto_recoverable=False,
    )
    # 副作用不可恢复 -> automaticFallbackSafe=False
    assert result.automaticFallbackSafe is False
    assert len(result.externalSideEffects) > 0
    assert "migration" in result.externalSideEffects[0]
    assert len(result.sideEffectRecords) == 1
    assert result.sideEffectRecords[0].kind == "migration"


def test_rollback_with_recoverable_side_effects_can_be_safe(tmp_path: Path) -> None:
    """C.R05：全部可恢复的副作用不阻止 automaticFallbackSafe。"""
    from local_webpage_access.lifecycle import _rollback_attempt
    from local_webpage_access.paths import Workspace

    ws = Workspace(tmp_path)
    ws.ensure_app_dirs("test-se-safe")

    manifest = _mk_container_manifest(instance_id="test-se-safe")
    manifest.capabilityContract = {"requiresMigrations": False}

    se_records = [
        {
            "kind": "hook",
            "description": "缓存预热",
            "intent": "warm cache",
            "executedAt": "2026-08-12T00:00:00Z",
            "result": "succeeded",
            "autoRecoverable": True,
        }
    ]

    result = _rollback_attempt(
        ws,
        MagicMock(),
        MagicMock(),
        "test-se-safe",
        manifest,
        attempt_id="attempt-test-se-safe-0",
        snapshot=None,
        side_effect_records=se_records,
        side_effects_auto_recoverable=True,
    )
    # 副作用可恢复 -> 不影响 automaticFallbackSafe
    assert result.automaticFallbackSafe is True
    assert len(result.sideEffectRecords) == 1
    assert result.sideEffectRecords[0].autoRecoverable is True


def test_rollback_no_side_effects_safe_by_default(tmp_path: Path) -> None:
    """C.R05：无副作用时默认安全。"""
    from local_webpage_access.lifecycle import _rollback_attempt
    from local_webpage_access.paths import Workspace

    ws = Workspace(tmp_path)
    ws.ensure_app_dirs("test-no-se")

    manifest = _mk_container_manifest(instance_id="test-no-se")
    manifest.capabilityContract = {"requiresMigrations": False}

    result = _rollback_attempt(
        ws,
        MagicMock(),
        MagicMock(),
        "test-no-se",
        manifest,
        attempt_id="attempt-test-no-se-0",
        snapshot=None,
        side_effect_records=None,
        side_effects_auto_recoverable=True,
    )
    assert result.automaticFallbackSafe is True
    assert result.externalSideEffects == []
    assert result.sideEffectRecords == []


# ---- C.R03：confirm 策略与 FallbackConfirmationRequired -----------------------


def test_fallback_confirmation_required_attributes():
    """C.R03：FallbackConfirmationRequired 携带等价候选列表。"""
    from local_webpage_access.lifecycle import FallbackConfirmationRequired

    exc = FallbackConfirmationRequired(
        "test-cr03",
        primary_failure="build failed: Dockerfile error",
        equivalent_candidates=[
            {"index": 2, "kind": "python", "confidenceTier": "fallback"},
            {"index": 3, "kind": "python", "confidenceTier": "alternate"},
        ],
    )
    assert exc.instance_id == "test-cr03"
    assert "build failed" in exc.primary_failure
    assert len(exc.equivalent_candidates) == 2
    assert exc.equivalent_candidates[0]["index"] == 2
    assert exc.equivalent_candidates[1]["kind"] == "python"


def test_fallback_confirmation_required_empty_candidates():
    """C.R03：无等价候选时 equivalent_candidates 默认空列表。"""
    from local_webpage_access.lifecycle import FallbackConfirmationRequired

    exc = FallbackConfirmationRequired("test-empty", primary_failure="oops")
    assert exc.equivalent_candidates == []
    assert "0 个等价" in str(exc)


def test_confirm_policy_raises_fallback_confirmation():
    """C.R03：confirm 策略 + top-1 失败 + 有等价候选 -> 抛 FallbackConfirmationRequired。"""
    from local_webpage_access.lifecycle import (
        FallbackConfirmationRequired,
        _try_host_with_fallback,
    )

    manifest = _mk_container_manifest(
        instance_id="test-confirm",
        fallbacks=[
            {
                "index": 0,
                "kind": "python",
                "form": "backend-container",
                "confidenceTier": "primary",
            },
            {
                "index": 1,
                "kind": "python",
                "form": "backend-container",
                "confidenceTier": "fallback",
            },
        ],
    )

    call_count = [0]

    def failing_host(ws, cfg, reg, iid):
        call_count[0] += 1
        raise HostingError("build failed", instance_id=iid)

    ws = MagicMock()
    ws.app_compose_path = MagicMock(return_value=Path("/tmp/nonexistent-compose.yaml"))
    ws.app_dockerfile_path = MagicMock(return_value=Path("/tmp/nonexistent-Dockerfile"))
    ws.app_env_path = MagicMock(return_value=Path("/tmp/nonexistent.env"))
    ws.app_manifest_path = MagicMock(return_value=Path("/tmp/nonexistent-manifest.json"))

    with pytest.raises(FallbackConfirmationRequired) as exc_info:
        _try_host_with_fallback(
            ws,
            MagicMock(),
            MagicMock(),
            "test-confirm",
            manifest,
            failing_host,
            fallback_policy="confirm",
        )

    assert exc_info.value.instance_id == "test-confirm"
    assert len(exc_info.value.equivalent_candidates) >= 1
    # top-1 只调用一次（confirm 策略不自动降级）
    assert call_count[0] == 1


def test_disabled_policy_does_not_raise_confirmation():
    """C.R03：disabled 策略不抛 FallbackConfirmationRequired，直接抛原始异常。"""
    from local_webpage_access.lifecycle import _try_host_with_fallback

    manifest = _mk_container_manifest(
        instance_id="test-disabled",
        fallbacks=[
            {
                "index": 0,
                "kind": "python",
                "form": "backend-container",
                "confidenceTier": "primary",
            },
            {
                "index": 1,
                "kind": "python",
                "form": "backend-container",
                "confidenceTier": "fallback",
            },
        ],
    )

    def failing_host(ws, cfg, reg, iid):
        raise HostingError("build failed", instance_id=iid)

    ws = MagicMock()
    ws.app_compose_path = MagicMock(return_value=Path("/tmp/nonexistent-compose.yaml"))
    ws.app_dockerfile_path = MagicMock(return_value=Path("/tmp/nonexistent-Dockerfile"))
    ws.app_env_path = MagicMock(return_value=Path("/tmp/nonexistent.env"))
    ws.app_manifest_path = MagicMock(return_value=Path("/tmp/nonexistent-manifest.json"))

    # disabled 策略应直接抛出 HostingError，不抛 FallbackConfirmationRequired
    with pytest.raises(HostingError):
        _try_host_with_fallback(
            ws,
            MagicMock(),
            MagicMock(),
            "test-disabled",
            manifest,
            failing_host,
            fallback_policy="disabled",
        )


def test_auto_equivalent_policy_does_not_raise_confirmation():
    """C.R03：auto-equivalent 策略不抛 FallbackConfirmationRequired，自动尝试降级。"""
    from local_webpage_access.lifecycle import _try_host_with_fallback

    manifest = _mk_container_manifest(
        instance_id="test-auto",
        fallbacks=[
            {
                "index": 0,
                "kind": "python",
                "form": "backend-container",
                "confidenceTier": "primary",
            },
            {
                "index": 1,
                "kind": "python",
                "form": "backend-container",
                "confidenceTier": "fallback",
            },
        ],
    )

    call_count = [0]

    def first_fail_second_succeed(ws, cfg, reg, iid):
        call_count[0] += 1
        if call_count[0] == 1:
            raise HostingError("build failed", instance_id=iid)
        # 第二次成功
        manifest.selectedCandidateIndex = 1
        return manifest

    ws = MagicMock()
    ws.app_compose_path = MagicMock(return_value=Path("/tmp/nonexistent-compose.yaml"))
    ws.app_dockerfile_path = MagicMock(return_value=Path("/tmp/nonexistent-Dockerfile"))
    ws.app_env_path = MagicMock(return_value=Path("/tmp/nonexistent.env"))
    ws.app_manifest_path = MagicMock(return_value=Path("/tmp/nonexistent-manifest.json"))

    result = _try_host_with_fallback(
        ws,
        MagicMock(),
        MagicMock(),
        "test-auto",
        manifest,
        first_fail_second_succeed,
        fallback_policy="auto-equivalent",
    )
    # top-1 失败后自动降级到第二个候选并成功
    assert call_count[0] == 2
    assert result.selectedCandidateIndex == 1


# ---- C.R06：四类部署指纹 -----------------------------------------------------


def test_compute_source_fingerprint_basic():
    """C.R06：源码指纹基于文件路径+内容计算。"""
    import tempfile

    from local_webpage_access.lifecycle import _compute_source_fingerprint

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        (tmp / "app.py").write_text("print('hello')", encoding="utf-8")
        (tmp / "requirements.txt").write_text("flask", encoding="utf-8")
        h1 = _compute_source_fingerprint(str(tmp))
        assert len(h1) == 64  # SHA256 hex

        # 修改内容 -> hash 变化
        (tmp / "app.py").write_text("print('world')", encoding="utf-8")
        h2 = _compute_source_fingerprint(str(tmp))
        assert h1 != h2

        # 同内容 -> hash 不变
        (tmp / "app.py").write_text("print('hello')", encoding="utf-8")
        h3 = _compute_source_fingerprint(str(tmp))
        assert h1 == h3


def test_compute_source_fingerprint_empty_path():
    """C.R06：空路径或不存在目录返回空字符串。"""
    from local_webpage_access.lifecycle import _compute_source_fingerprint

    assert _compute_source_fingerprint(None) == ""
    assert _compute_source_fingerprint("") == ""
    assert _compute_source_fingerprint("/tmp/nonexistent-xyz-123") == ""


def test_compute_plan_fingerprint_stable():
    """C.R06：计划指纹对相同内容稳定，对不同内容变化。"""
    from local_webpage_access.lifecycle import _compute_plan_fingerprint

    plan = {"planId": "p1", "kind": "python", "components": []}
    h1 = _compute_plan_fingerprint(plan)
    h2 = _compute_plan_fingerprint({"components": [], "planId": "p1", "kind": "python"})
    assert h1 == h2  # key 顺序不影响

    plan2 = {"planId": "p2", "kind": "python", "components": []}
    h3 = _compute_plan_fingerprint(plan2)
    assert h1 != h3


def test_compute_plan_fingerprint_none():
    """C.R06：None 或空字典返回空字符串。"""
    from local_webpage_access.lifecycle import _compute_plan_fingerprint

    assert _compute_plan_fingerprint(None) == ""
    assert _compute_plan_fingerprint({}) == ""


def test_compute_config_fingerprint():
    """C.R06：配置指纹基于 compose/Dockerfile/.env 内容。"""
    import tempfile

    from local_webpage_access.lifecycle import _compute_config_fingerprint

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        compose = tmp / "compose.yaml"
        dockerfile = tmp / "Dockerfile"
        env = tmp / ".env"

        compose.write_text("services:\n  app:\n    image: test", encoding="utf-8")
        dockerfile.write_text("FROM python:3.12", encoding="utf-8")
        env.write_text("DEBUG=true", encoding="utf-8")

        h1 = _compute_config_fingerprint(compose, dockerfile, env)
        assert len(h1) == 64

        # 修改 Dockerfile -> hash 变化
        dockerfile.write_text("FROM python:3.13", encoding="utf-8")
        h2 = _compute_config_fingerprint(compose, dockerfile, env)
        assert h1 != h2


def test_compute_config_fingerprint_missing_files():
    """C.R06：缺失文件不报错，返回有效哈希。"""
    from local_webpage_access.lifecycle import _compute_config_fingerprint

    h = _compute_config_fingerprint(None, None, None)
    assert len(h) == 64  # 仍有哈希（全部 missing 标记）

    # missing 与存在文件应不同
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        compose = Path(tmpdir) / "compose.yaml"
        compose.write_text("services: {}", encoding="utf-8")
        h2 = _compute_config_fingerprint(compose, None, None)
        assert h != h2


def test_fingerprints_changed_no_stored():
    """C.R06：无存储指纹时不视为变化（向后兼容）。"""
    from local_webpage_access.lifecycle import _fingerprints_changed

    changed, fields = _fingerprints_changed(None, {"sourceHash": "abc"})
    assert changed is False
    assert fields == []


def test_fingerprints_changed_all_match():
    """C.R06：全部指纹匹配时不变化。"""
    from local_webpage_access.lifecycle import _fingerprints_changed

    stored = {"sourceHash": "a", "planHash": "b", "configHash": "c", "imageId": "d"}
    current = {"sourceHash": "a", "planHash": "b", "configHash": "c", "imageId": "d"}
    changed, fields = _fingerprints_changed(stored, current)
    assert changed is False
    assert fields == []


def test_fingerprints_changed_source():
    """C.R06：sourceHash 变化被检测。"""
    from local_webpage_access.lifecycle import _fingerprints_changed

    stored = {"sourceHash": "a", "planHash": "b", "configHash": "c", "imageId": "d"}
    current = {"sourceHash": "X", "planHash": "b", "configHash": "c", "imageId": "d"}
    changed, fields = _fingerprints_changed(stored, current)
    assert changed is True
    assert "sourceHash" in fields


def test_fingerprints_changed_image():
    """C.R06：imageId 变化被检测。"""
    from local_webpage_access.lifecycle import _fingerprints_changed

    stored = {"sourceHash": "a", "planHash": "b", "configHash": "c", "imageId": "d"}
    current = {"sourceHash": "a", "planHash": "b", "configHash": "c", "imageId": "Z"}
    changed, fields = _fingerprints_changed(stored, current)
    assert changed is True
    assert "imageId" in fields
    assert "sourceHash" not in fields


def test_fingerprints_changed_multiple():
    """C.R06：多个指纹变化都被检测。"""
    from local_webpage_access.lifecycle import _fingerprints_changed

    stored = {"sourceHash": "a", "planHash": "b", "configHash": "c", "imageId": "d"}
    current = {"sourceHash": "X", "planHash": "Y", "configHash": "c", "imageId": "d"}
    changed, fields = _fingerprints_changed(stored, current)
    assert changed is True
    assert set(fields) == {"sourceHash", "planHash"}


def test_compute_deployment_fingerprints_structure():
    """C.R06：_compute_deployment_fingerprints 返回四类指纹。"""
    from local_webpage_access.lifecycle import _compute_deployment_fingerprints

    manifest = _mk_container_manifest(instance_id="test-fp")
    manifest.appPath = None  # 无源码目录
    manifest.container.composePath = "/tmp/nonexistent-compose.yaml"
    manifest.container.dockerfilePath = "/tmp/nonexistent-Dockerfile"
    manifest.container.imageId = "sha256:abc123"

    ws = MagicMock()
    ws.app_env_path = MagicMock(return_value=Path("/tmp/nonexistent.env"))

    fps = _compute_deployment_fingerprints(ws, manifest)
    assert "sourceHash" in fps
    assert "planHash" in fps
    assert "configHash" in fps
    assert "buildConfigHash" in fps
    assert "runtimeConfigHash" in fps
    assert "imageId" in fps
    assert fps["sourceHash"] == ""  # appPath=None
    assert fps["imageId"] == "sha256:abc123"
    assert len(fps["configHash"]) == 64  # 有效哈希即使文件不存在
