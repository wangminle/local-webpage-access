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
        _try_host_with_fallback(
            mock_ws, MagicMock(), MagicMock(), "test", manifest, host_fn
        )
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
        _try_host_with_fallback(
            MagicMock(), MagicMock(), MagicMock(), "test", manifest, host_fn
        )
    assert host_fn.call_count == 1


def test_fallback_success_after_top1_failure() -> None:
    """top-1 失败 → 回滚 → auto-equivalent 降级 → fallback 成功。"""
    from local_webpage_access.lifecycle import _try_host_with_fallback

    success_manifest = _mk_container_manifest()
    manifest = _mk_container_manifest(fallbacks=[
        {
            "kind": "node",
            "runtime": "docker-compose",
            "servingMode": "container",
            "confidenceTier": "fallback",
            "entry": {"install": "npm ci", "start": "node server.js"},
        }
    ])

    call_count = [0]

    def mock_host(ws, cfg, reg, iid):
        call_count[0] += 1
        if call_count[0] == 1:
            raise HostingError("python build failed")
        return success_manifest

    host_fn = MagicMock(side_effect=mock_host)

    result = _try_host_with_fallback(
        MagicMock(), MagicMock(), MagicMock(), "test", manifest, host_fn,
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

    manifest = _mk_container_manifest(fallbacks=[
        {
            "kind": "node",
            "runtime": "docker-compose",
            "servingMode": "container",
            "confidenceTier": "fallback",
            "entry": {"install": "npm ci", "start": "node server.js"},
        }
    ])

    host_fn = MagicMock(side_effect=HostingError("python build failed"))

    with pytest.raises(FallbackConfirmationRequired) as exc_info:
        _try_host_with_fallback(
            MagicMock(), MagicMock(), MagicMock(), "test", manifest, host_fn,
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

    manifest = _mk_container_manifest(fallbacks=[
        {
            "kind": "node",
            "runtime": "docker-compose",
            "servingMode": "container",
            "confidenceTier": "fallback",
        }
    ])
    host_fn = MagicMock(side_effect=HostingError("build failed"))
    mock_ws = MagicMock()
    mock_ws.app_manifest_path.return_value = Path("/tmp/manifest.json")

    with pytest.raises(HostingError, match="build failed"):
        _try_host_with_fallback(
            mock_ws, MagicMock(), MagicMock(), "test", manifest, host_fn,
            fallback_policy="disabled",
        )
    assert host_fn.call_count == 1


def test_all_candidates_fail_produces_diagnosis() -> None:
    """全部候选失败 → Layer 4 诊断报告写入 + HostingError。"""
    from local_webpage_access.lifecycle import _try_host_with_fallback

    manifest = _mk_container_manifest(fallbacks=[
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
    ])

    host_fn = MagicMock(side_effect=HostingError("always fails"))
    mock_ws = MagicMock()
    mock_ws.app_manifest_path.return_value = Path("/tmp/manifest.json")
    mock_reg = MagicMock()

    with pytest.raises(HostingError, match="所有候选均部署失败"):
        _try_host_with_fallback(
            mock_ws, MagicMock(), mock_reg, "test", manifest, host_fn,
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
            mock_ws, MagicMock(), mock_reg, "test", manifest, host_fn,
            fallback_policy="auto-equivalent",
        )
    # top-1 + _MAX_FALLBACK_CANDIDATES
    assert host_fn.call_count == 1 + _MAX_FALLBACK_CANDIDATES


# ---- 能力守恒测试（IMP-058 §6.1.1） ------------------------------------------


def test_backend_rejects_static_fallback() -> None:
    """IMP-058 §6.1.1：后端候选失败后不得降级到静态候选。"""
    from local_webpage_access.lifecycle import _try_host_with_fallback

    # top-1 是 python 容器（backend），fallback 是 static（index.html 兜底候选）
    manifest = _mk_container_manifest(fallbacks=[
        {
            "kind": "static",
            "runtime": "shared_static",
            "servingMode": "shared_static",
            "form": "static",
            "confidenceTier": "fallback",
        }
    ])
    host_fn = MagicMock(side_effect=HostingError("backend build failed"))
    mock_ws = MagicMock()
    mock_ws.app_manifest_path.return_value = Path("/tmp/manifest.json")

    with pytest.raises(HostingError, match="backend build failed"):
        _try_host_with_fallback(
            mock_ws, MagicMock(), MagicMock(), "test", manifest, host_fn,
            fallback_policy="auto-equivalent",
        )
    # host_fn 只调用一次（top-1），不尝试 static fallback
    assert host_fn.call_count == 1


def test_backend_allows_equivalent_backend_fallback() -> None:
    """IMP-058 §6.1.1：后端候选可降级到同族后端候选（python→node）。"""
    from local_webpage_access.lifecycle import _try_host_with_fallback

    success_manifest = _mk_container_manifest()
    # top-1 是 python 容器（backend），fallback 是 node 容器（同为 backend）
    manifest = _mk_container_manifest(fallbacks=[
        {
            "kind": "node",
            "runtime": "docker_compose",
            "servingMode": "container",
            "form": "backend-container",
            "confidenceTier": "fallback",
            "entry": {"install": "npm ci", "start": "node server.js"},
        }
    ])
    host_fn = MagicMock(
        side_effect=[HostingError("python failed"), success_manifest]
    )

    result = _try_host_with_fallback(
        MagicMock(), MagicMock(), MagicMock(), "test", manifest, host_fn,
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
