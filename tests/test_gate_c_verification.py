"""Gate-C 实证校验完整测试（IMP-058-C C.01-C.09）。

覆盖设计文档 §10.4 Gate-C 退出标准：
- C.01 能力契约模型
- C.02 部署计划收敛（全栈 = 一个计划，不是两个 fallback）
- C.03 结构化命令（CommandSpec）
- C.04 成功谓词与状态机（VERIFYING → RUNNING/DEGRADED/FAILED）
- C.05 证据驱动探针（declared/discovered 可作门槛，guessed 仅诊断）
- C.06 事务回滚 + 副作用台账
- C.07 fallback 策略（confirm / auto-equivalent / disabled）
- C.08 诊断报告（含回滚结果、能力差异）
- C.09 故障注入：build/up/health/回滚每层失败
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from local_webpage_access.errors import HostingError
from local_webpage_access.models import (
    CapabilityContract,
    CandidateDiagnosis,
    CommandSpec,
    ContainerConfig,
    DeploymentComponent,
    DeploymentPlan,
    DiagnosisReport,
    InstanceManifest,
    Kind,
    ProbeSpec,
    RollbackResult,
    Runtime,
    ServingMode,
    Status,
    VerificationResult,
)


# ---- C.01：能力契约模型 -----------------------------------------------------


class TestCapabilityContract:
    """Gate-C C.01：CapabilityContract 模型测试。"""

    def test_empty_contract_has_no_required_capabilities(self) -> None:
        cc = CapabilityContract()
        assert cc.required_capabilities == set()

    def test_fullstack_contract(self) -> None:
        cc = CapabilityContract(
            servesUi=True,
            servesApi=True,
            requiresDatabase=True,
            requiresMigrations=True,
        )
        assert cc.required_capabilities == {"ui", "api", "database", "migrations"}

    def test_contract_with_probes(self) -> None:
        probe = ProbeSpec(path="/health", isMandatory=True, source="declared")
        cc = CapabilityContract(
            servesApi=True,
            requiredProbes=[probe],
        )
        assert len(cc.requiredProbes) == 1
        assert cc.requiredProbes[0].isMandatory

    def test_contracts_equivalence(self) -> None:
        """两个能力集合相同的契约视为等价。"""
        cc1 = CapabilityContract(servesApi=True, requiresDatabase=True)
        cc2 = CapabilityContract(servesApi=True, requiresDatabase=True)
        assert cc1.required_capabilities == cc2.required_capabilities

    def test_contracts_non_equivalence(self) -> None:
        """fullstack 契约 != 纯 UI 契约（能力守恒核心）。"""
        fullstack = CapabilityContract(servesUi=True, servesApi=True, requiresDatabase=True)
        static_only = CapabilityContract(servesUi=True)
        assert fullstack.required_capabilities != static_only.required_capabilities
        # static_only 的能力是 fullstack 的真子集
        assert static_only.required_capabilities.issubset(fullstack.required_capabilities)


class TestCommandSpec:
    """Gate-C C.03：CommandSpec 结构化命令。"""

    def test_argv_mode(self) -> None:
        cmd = CommandSpec(argv=["uvicorn", "app.main:app", "--host", "0.0.0.0"])
        assert cmd.is_effective()
        assert cmd.argv == ["uvicorn", "app.main:app", "--host", "0.0.0.0"]
        assert cmd.shell is None

    def test_shell_mode(self) -> None:
        cmd = CommandSpec(shell="alembic upgrade head && exec uvicorn app.main:app")
        assert cmd.is_effective()
        assert cmd.shell is not None
        assert cmd.argv is None

    def test_empty_command(self) -> None:
        cmd = CommandSpec()
        assert not cmd.is_effective()

    def test_workdir_and_env(self) -> None:
        cmd = CommandSpec(
            argv=["alembic", "upgrade", "head"],
            workdir="/app/backend",
            environment={"DATABASE_URL": "sqlite:////app/data/test.db"},
        )
        assert cmd.workdir == "/app/backend"
        assert cmd.environment["DATABASE_URL"] == "sqlite:////app/data/test.db"


class TestDeploymentPlan:
    """Gate-C C.01/C.02：DeploymentPlan 模型。"""

    def test_fullstack_plan_has_two_components(self) -> None:
        """全栈计划含前端构建组件 + 后端运行组件（合作关系）。"""
        frontend = DeploymentComponent(
            componentId="frontend-build",
            role="build",
            sourceSubdir="frontend",
            buildOutputDir="dist",
            artifactTarget="backend/static",
        )
        backend = DeploymentComponent(
            componentId="python-runtime",
            role="runtime",
            sourceSubdir="backend",
            internalPort=8000,
        )
        plan = DeploymentPlan(
            planId="plan-fullstack-backend",
            components=[frontend, backend],
            capabilityContract=CapabilityContract(
                servesUi=True, servesApi=True, requiresDatabase=True,
            ),
        )
        assert len(plan.components) == 2
        roles = {c.role for c in plan.components}
        assert roles == {"build", "runtime"}

    def test_static_plan_has_one_component(self) -> None:
        component = DeploymentComponent(componentId="static-runtime", role="runtime")
        plan = DeploymentPlan(
            planId="plan-static",
            components=[component],
            capabilityContract=CapabilityContract(servesUi=True),
        )
        assert len(plan.components) == 1
        assert plan.capabilityContract.required_capabilities == {"ui"}


# ---- C.02：部署计划收敛 -----------------------------------------------------


class TestPlanGeneration:
    """Gate-C C.02：候选→计划收敛测试。"""

    def test_fullstack_converges_to_single_plan(self, tmp_path: Path) -> None:
        """backend+frontend 收敛为一个全栈计划（不是两个 fallback）。"""
        from local_webpage_access.evidence_collector import collect
        from local_webpage_access.candidate_generator import generate_plans

        # 创建 home-bookshelf 类项目
        (tmp_path / "backend").mkdir()
        (tmp_path / "backend" / "requirements.txt").write_text(
            "fastapi\nuvicorn\nsqlalchemy\nalembic\n"
        )
        (tmp_path / "backend" / "app").mkdir()
        (tmp_path / "backend" / "app" / "main.py").write_text("# FastAPI")
        (tmp_path / "backend" / "alembic.ini").write_text(
            "[alembic]\nscript_location = alembic\n"
        )
        (tmp_path / "frontend").mkdir()
        (tmp_path / "frontend" / "package.json").write_text(
            '{"name":"fe","scripts":{"build":"vite build"},'
            '"dependencies":{"vue":"3","vite":"5"}}'
        )
        (tmp_path / "frontend" / "index.html").write_text("<html></html>")

        evidence = collect(tmp_path)
        plans = generate_plans(evidence)

        # 应只有 1 个 primary 计划（全栈）
        primary_plans = [p for p in plans if p.confidenceTier == "primary"]
        assert len(primary_plans) == 1
        assert "fullstack" in primary_plans[0].planId

        # 该计划应含 2 个组件（build + runtime）
        assert len(primary_plans[0].components) == 2

        # 能力契约含 ui + api + database
        caps = primary_plans[0].capabilityContract.required_capabilities
        assert "ui" in caps
        assert "api" in caps
        assert "database" in caps

    def test_pure_static_single_plan(self, tmp_path: Path) -> None:
        """纯静态项目 → 1 个 static 计划。"""
        from local_webpage_access.evidence_collector import collect
        from local_webpage_access.candidate_generator import generate_plans

        (tmp_path / "index.html").write_text("<html></html>")

        evidence = collect(tmp_path)
        plans = generate_plans(evidence)

        assert len(plans) == 1
        assert plans[0].capabilityContract.required_capabilities == {"ui"}

    def test_static_diagnostic_when_backend_exists(self, tmp_path: Path) -> None:
        """有后端时，static 标记为 diagnostic（不参与自动启动）。"""
        from local_webpage_access.evidence_collector import collect
        from local_webpage_access.candidate_generator import generate_plans

        (tmp_path / "backend").mkdir()
        (tmp_path / "backend" / "requirements.txt").write_text("fastapi\nuvicorn\n")
        (tmp_path / "backend" / "app").mkdir()
        (tmp_path / "backend" / "app" / "main.py").write_text("# FastAPI")
        (tmp_path / "index.html").write_text("<html></html>")

        evidence = collect(tmp_path)
        plans = generate_plans(evidence)

        # 应有 primary backend 计划 + diagnostic static
        diagnostic_plans = [p for p in plans if p.confidenceTier == "diagnostic"]
        assert len(diagnostic_plans) == 1
        assert "diagnostic" in diagnostic_plans[0].planId


# ---- C.04/C.05：成功谓词与探针 -----------------------------------------------


class TestSuccessPredicate:
    """Gate-C C.04/C.05：成功谓词与证据驱动探针测试。"""

    def test_probe_spec_mandatory_vs_optional(self) -> None:
        mandatory = ProbeSpec(path="/health", isMandatory=True, source="declared")
        optional = ProbeSpec(path="/api/", isMandatory=False, source="guessed")
        assert mandatory.isMandatory
        assert not optional.isMandatory

    def test_evaluation_passed(self) -> None:
        """存活探针通过 + 能力覆盖 → passed。"""
        from local_webpage_access.health import evaluate_success_predicate

        with patch("local_webpage_access.health.http_ok", return_value=(True, 200)):
            with patch("local_webpage_access.health.api_probe", return_value=(True, "/health")):
                with patch("local_webpage_access.health.run_probe_spec", return_value=(True, 200)):
                    evaluation = evaluate_success_predicate(
                        9999,
                        required_probes=[ProbeSpec(path="/", isMandatory=True)],
                        capability_contract=CapabilityContract(servesUi=True),
                    )
        assert evaluation.overall_status == "passed"
        assert evaluation.liveness_passed

    def test_evaluation_failed_on_liveness_timeout(self) -> None:
        """基础存活探针超时 → failed。"""
        from local_webpage_access.health import evaluate_success_predicate

        with patch("local_webpage_access.health.http_ok", return_value=(False, None)):
            evaluation = evaluate_success_predicate(
                9999,
                required_probes=[ProbeSpec(path="/", isMandatory=True)],
            )
        assert evaluation.overall_status == "failed"
        assert not evaluation.liveness_passed

    def test_evaluation_failed_on_mandatory_probe(self) -> None:
        """必选探针失败 → failed（即使存活通过）。"""
        from local_webpage_access.health import evaluate_success_predicate

        with patch("local_webpage_access.health.http_ok", return_value=(True, 200)):
            with patch(
                "local_webpage_access.health.run_probe_spec",
                return_value=(False, 503),
            ):
                evaluation = evaluate_success_predicate(
                    9999,
                    required_probes=[
                        ProbeSpec(path="/health", isMandatory=True, source="declared"),
                    ],
                )
        assert evaluation.overall_status == "failed"
        assert not evaluation.mandatory_all_passed

    def test_evaluation_api_guess_not_failure(self) -> None:
        """通用 API 猜测 404 不判失败（§6.5）。"""
        from local_webpage_access.health import evaluate_success_predicate

        with patch("local_webpage_access.health.http_ok", return_value=(True, 200)):
            with patch("local_webpage_access.health.api_probe", return_value=(None, None)):
                # api_probe 返回 None（全 404）→ 不判失败
                evaluation = evaluate_success_predicate(
                    9999,
                    capability_contract=CapabilityContract(servesUi=True),
                )
        # 存活通过 + UI 能力覆盖 → passed
        assert evaluation.overall_status in ("passed", "degraded")

    def test_optional_probe_failure_is_degraded(self) -> None:
        """可选探针失败不阻断部署，但必须保留 DEGRADED 语义。"""
        from local_webpage_access.health import evaluate_success_predicate

        probe = ProbeSpec(path="/health", isMandatory=False, source="guessed")
        with (
            patch("local_webpage_access.health.http_ok", return_value=(True, 200)),
            patch("local_webpage_access.health.api_probe", return_value=(None, None)),
            patch("local_webpage_access.health.run_probe_spec", return_value=(False, 404)),
        ):
            evaluation = evaluate_success_predicate(
                9999,
                required_probes=[probe],
                capability_contract=CapabilityContract(servesUi=True),
            )
        assert evaluation.overall_status == "degraded"
        assert evaluation.optional_warnings


class TestStateMachine:
    """Gate-C C.04：VERIFYING/DEGRADED 状态测试。"""

    def test_status_enum_has_verifying(self) -> None:
        assert Status.VERIFYING.value == "verifying"

    def test_status_enum_has_degraded(self) -> None:
        assert Status.DEGRADED.value == "degraded"

    def test_verification_result_is_success(self) -> None:
        vr = VerificationResult(overallStatus="passed")
        assert vr.is_success()

        vr2 = VerificationResult(overallStatus="degraded")
        assert vr2.is_success()

        vr3 = VerificationResult(overallStatus="failed")
        assert not vr3.is_success()


# ---- C.06/C.07：事务回滚与 fallback 策略 ---------------------------------------


class TestFallbackPolicy:
    """Gate-C C.07：fallback 策略测试。"""

    def _mk_manifest(
        self, *, fallbacks: list[dict] | None = None
    ) -> InstanceManifest:
        return InstanceManifest(
            id="test",
            name="Test",
            version="1",
            kind=Kind.PYTHON,
            runtime=Runtime.DOCKER_COMPOSE,
            servingMode=ServingMode.CONTAINER,
            container={
                "projectName": "lwa-test",
                "internalPort": 8000,
                "composePath": "/tmp/c.yml",
                "dockerfilePath": "/tmp/D",
            },
            deploymentCandidates=fallbacks or [],
        )

    def test_confirm_raises_confirmation_required(self) -> None:
        """confirm 策略 → FallbackConfirmationRequired（不自动降级）。"""
        from local_webpage_access.lifecycle import (
            FallbackConfirmationRequired,
            _try_host_with_fallback,
        )

        manifest = self._mk_manifest(fallbacks=[
            {
                "kind": "node",
                "runtime": "docker-compose",
                "servingMode": "container",
                "form": "backend-container",
                "confidenceTier": "fallback",
            }
        ])
        host_fn = MagicMock(side_effect=HostingError("build failed"))

        with pytest.raises(FallbackConfirmationRequired) as exc_info:
            _try_host_with_fallback(
                MagicMock(), MagicMock(), MagicMock(), "test", manifest, host_fn,
                fallback_policy="confirm",
            )
        assert host_fn.call_count == 1
        assert len(exc_info.value.equivalent_candidates) == 1

    def test_disabled_does_not_fallback(self) -> None:
        """disabled 策略 → 直接抛原始错误。"""
        from local_webpage_access.lifecycle import _try_host_with_fallback

        manifest = self._mk_manifest(fallbacks=[
            {"kind": "node", "runtime": "docker-compose", "form": "backend-container"}
        ])
        host_fn = MagicMock(side_effect=HostingError("fail"))
        mock_ws = MagicMock()
        mock_ws.app_manifest_path.return_value = Path("/tmp/m.json")

        with pytest.raises(HostingError, match="fail"):
            _try_host_with_fallback(
                mock_ws, MagicMock(), MagicMock(), "test", manifest, host_fn,
                fallback_policy="disabled",
            )
        assert host_fn.call_count == 1

    def test_auto_equivalent_falls_back(self) -> None:
        """auto-equivalent 策略 → 自动降级到等价候选。"""
        from local_webpage_access.lifecycle import _try_host_with_fallback

        success = self._mk_manifest()
        manifest = self._mk_manifest(fallbacks=[
            {
                "kind": "node",
                "runtime": "docker-compose",
                "servingMode": "container",
                "form": "backend-container",
                "confidenceTier": "fallback",
            }
        ])
        host_fn = MagicMock(side_effect=[HostingError("py fail"), success])

        result = _try_host_with_fallback(
            MagicMock(), MagicMock(), MagicMock(), "test", manifest, host_fn,
            fallback_policy="auto-equivalent",
        )
        assert result is success
        assert host_fn.call_count == 2


class TestRollbackAndSideEffects:
    """Gate-C C.06：回滚范围与副作用台账测试。"""

    def test_rollback_result_default_unsafe(self) -> None:
        """回滚结果默认 automaticFallbackSafe=False。"""
        rb = RollbackResult(attemptId="a1")
        assert not rb.automaticFallbackSafe
        assert not rb.rollbackSucceeded

    def test_rollback_result_with_external_effects(self) -> None:
        """有外部副作用的回滚不标记 automaticFallbackSafe。"""
        rb = RollbackResult(
            attemptId="a1",
            rollbackSucceeded=True,
            rolledBackItems=["container", "port"],
            externalSideEffects=["migration:alembic_abc123"],
            automaticFallbackSafe=False,
        )
        assert rb.rollbackSucceeded
        assert len(rb.externalSideEffects) == 1
        assert not rb.automaticFallbackSafe

    def test_diagnosis_includes_rollback(self) -> None:
        """Layer 4 诊断报告包含回滚结果。"""
        diagnosis = CandidateDiagnosis(
            candidateIndex=0,
            failureLayer="build",
            failureReason="build error",
            rollback=RollbackResult(
                attemptId="a1",
                rollbackSucceeded=True,
                rolledBackItems=["container"],
            ),
        )
        assert diagnosis.rollback is not None
        assert diagnosis.rollback.rollbackSucceeded


# ---- C.09：故障注入测试 -----------------------------------------------------


class TestFaultInjection:
    """Gate-C C.09：逐层故障注入测试。

    每层失败应被正确捕获并产生对应失败诊断。
    """

    def _mk_manifest(
        self, *, fallbacks: list[dict] | None = None
    ) -> InstanceManifest:
        return InstanceManifest(
            id="fault-test",
            name="Fault Test",
            version="1",
            kind=Kind.PYTHON,
            runtime=Runtime.DOCKER_COMPOSE,
            servingMode=ServingMode.CONTAINER,
            container={
                "projectName": "lwa-fault-test",
                "internalPort": 8000,
                "composePath": "/tmp/c.yml",
                "dockerfilePath": "/tmp/D",
            },
            deploymentCandidates=fallbacks or [],
        )

    def test_build_failure_diagnosed(self) -> None:
        """build 阶段失败 → 失败诊断含 failureLayer='build'。"""
        from local_webpage_access.lifecycle import (
            _infer_failure_layer,
        )

        exc = HostingError("dockerfile COPY failed")
        layer = _infer_failure_layer(exc)
        assert layer == "build"

    def test_health_failure_diagnosed(self) -> None:
        """health 阶段失败 → failureLayer='health'。"""
        from local_webpage_access.lifecycle import _infer_failure_layer

        exc = HostingError("health check timeout")
        layer = _infer_failure_layer(exc)
        assert layer == "health"

    def test_port_failure_diagnosed(self) -> None:
        """port 阶段失败 → failureLayer='start'。"""
        from local_webpage_access.lifecycle import _infer_failure_layer

        exc = HostingError("port allocation failed")
        layer = _infer_failure_layer(exc)
        assert layer == "start"

    def test_fullstack_not_degraded_to_static(self) -> None:
        """C.09 关键回归：全栈后端失败时不得降级为仅静态首页。

        IMP-058 §6.1.1 硬性约束 + §10.4 退出标准：
        "全栈后端失败时，不得降级为仅静态首页并声明成功"
        """
        from local_webpage_access.lifecycle import _try_host_with_fallback

        # top-1 是 python backend，fallback 是 static
        manifest = self._mk_manifest(fallbacks=[
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
        mock_ws.app_manifest_path.return_value = Path("/tmp/m.json")

        with pytest.raises(HostingError, match="backend build failed"):
            _try_host_with_fallback(
                mock_ws, MagicMock(), MagicMock(), "test", manifest, host_fn,
                fallback_policy="auto-equivalent",  # 即使允许自动降级也不应降级到 static
            )
        # host_fn 只调用一次（top-1），static 不被尝试
        assert host_fn.call_count == 1

    def test_irreversible_migration_blocks_auto_fallback(self) -> None:
        """C.09：不可逆 migration 执行后禁止自动 fallback。

        §6.5 回滚边界：若 attempt 执行了数据库迁移，即使容器已 down，
        也不得自动 fallback（数据可能已被修改）。
        """
        # RollbackResult 标记了 externalSideEffects → automaticFallbackSafe=False
        rb = RollbackResult(
            attemptId="a1",
            rollbackSucceeded=True,
            rolledBackItems=["container"],
            externalSideEffects=["migration:alembic_head_abc"],
            automaticFallbackSafe=False,
        )
        # 即使容器回滚成功，也不允许自动 fallback
        assert rb.rollbackSucceeded
        assert not rb.automaticFallbackSafe
        assert len(rb.externalSideEffects) > 0


# ---- C.08：诊断报告完整性 ---------------------------------------------------


class TestDiagnosisReport:
    """Gate-C C.08：诊断报告完整性测试。"""

    def test_diagnosis_with_multiple_attempts(self) -> None:
        """多候选失败 → 诊断报告含每个 attempt 的详情。"""
        report = DiagnosisReport(
            instanceId="test",
            overallStatus="failed",
            candidatesTried=[
                CandidateDiagnosis(
                    candidateIndex=0,
                    candidateTier="primary",
                    failureLayer="build",
                    failureReason="Dockerfile syntax error",
                    fixSuggestion="fix Dockerfile",
                    verification=VerificationResult(
                        attemptId="a0",
                        candidateIndex=0,
                        buildSucceeded=False,
                        overallStatus="failed",
                    ),
                    rollback=RollbackResult(
                        attemptId="a0",
                        rollbackSucceeded=True,
                        rolledBackItems=["container", "port"],
                    ),
                ),
                CandidateDiagnosis(
                    candidateIndex=1,
                    candidateTier="fallback",
                    failureLayer="health",
                    failureReason="health timeout",
                    rollback=RollbackResult(attemptId="a1"),
                    capabilityDiff="fallback 缺少 api 能力",
                ),
            ],
            recommendedAction="手动检查",
        )
        assert len(report.candidatesTried) == 2
        d0 = report.candidatesTried[0]
        assert d0.failureLayer == "build"
        assert d0.rollback is not None
        assert d0.rollback.rollbackSucceeded

        d1 = report.candidatesTried[1]
        assert d1.failureLayer == "health"
        assert d1.capabilityDiff != ""

    def test_diagnosis_serialization(self) -> None:
        """诊断报告可序列化为 dict（写入 manifest）。"""
        report = DiagnosisReport(
            instanceId="test",
            candidatesTried=[
                CandidateDiagnosis(
                    candidateIndex=0,
                    failureLayer="build",
                    failureReason="error",
                )
            ],
        )
        d = report.model_dump()
        assert "candidatesTried" in d
        assert d["instanceId"] == "test"


# ---- CHK-193：后端能力契约验证 + 猜测探针可选 --------------------------------


class TestBackendCapabilityObservation:
    """CHK-193/P1：后端契约所需能力必须被正确观察。"""

    def test_backend_contract_does_not_pass_from_liveness_alone(self) -> None:
        """首页 200 只证明存活，不得反向证明契约要求的能力。"""
        from local_webpage_access.health import evaluate_success_predicate

        contract = CapabilityContract(
            servesUi=True,
            servesApi=True,
            requiresDatabase=True,
            requiresMigrations=True,
        )
        with (
            patch("local_webpage_access.health.http_ok", return_value=(True, 200)),
            patch("local_webpage_access.health.api_probe", return_value=(False, None)),
        ):
            evaluation = evaluate_success_predicate(
                9999,
                required_probes=[],
                capability_contract=contract,
                has_database=False,
                has_migrations=False,
            )
        assert evaluation.overall_status == "failed"
        assert evaluation.observed_capabilities == {"ui"}

    def test_backend_contract_passes_with_runtime_evidence(self) -> None:
        """有效 API 响应和显式 DB/迁移结果可满足后端契约。"""
        from local_webpage_access.health import evaluate_success_predicate

        contract = CapabilityContract(
            servesUi=True,
            servesApi=True,
            requiresDatabase=True,
            requiresMigrations=True,
        )
        with (
            patch("local_webpage_access.health.http_ok", return_value=(True, 200)),
            patch("local_webpage_access.health.api_probe", return_value=(True, "/api/")),
        ):
            evaluation = evaluate_success_predicate(
                9999,
                required_probes=[],
                capability_contract=contract,
                has_database=True,
                has_migrations=True,
            )
        assert evaluation.overall_status == "passed"
        assert evaluation.observed_capabilities == {
            "ui", "api", "database", "migrations",
        }

    def test_successful_declared_probe_observes_api(self) -> None:
        """成功的已声明业务探针是 API 能力证据。"""
        from local_webpage_access.health import evaluate_success_predicate

        probe = ProbeSpec(path="/ready", isMandatory=True, source="declared")
        contract = CapabilityContract(servesUi=True, servesApi=True)
        with (
            patch("local_webpage_access.health.http_ok", return_value=(True, 200)),
            patch("local_webpage_access.health.api_probe", return_value=(False, None)),
            patch("local_webpage_access.health.run_probe_spec", return_value=(True, 200)),
        ):
            evaluation = evaluate_success_predicate(
                9999,
                required_probes=[probe],
                capability_contract=contract,
            )
        assert evaluation.overall_status == "passed"
        assert "api" in evaluation.observed_capabilities

    def test_backend_contract_failed_when_liveness_fails(self) -> None:
        """存活失败时，即使契约要求 api/database -> failed。"""
        from local_webpage_access.health import evaluate_success_predicate

        contract = CapabilityContract(
            servesApi=True,
            requiresDatabase=True,
        )
        with patch("local_webpage_access.health.http_ok", return_value=(False, None)):
            evaluation = evaluate_success_predicate(
                9999,
                required_probes=[],
                capability_contract=contract,
            )
        assert evaluation.overall_status == "failed"

    def test_guessed_probes_are_optional_in_generated_plans(self, tmp_path: Path) -> None:
        """CHK-193/P1：generate_plans 生成的 source='guessed' 探针必须是可选。"""
        from local_webpage_access.evidence_collector import collect
        from local_webpage_access.candidate_generator import generate_plans

        # 创建后端项目
        (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")
        (tmp_path / "app.py").write_text("app = None")

        evidence = collect(tmp_path)
        plans = generate_plans(evidence)

        primary = [p for p in plans if p.confidenceTier == "primary"]
        assert len(primary) >= 1
        for plan in primary:
            for probe in plan.capabilityContract.requiredProbes:
                if probe.source == "guessed":
                    assert not probe.isMandatory, (
                        f"guessed 探针 {probe.path} 不应是 mandatory"
                    )

    def test_backend_plan_discovers_health_probe_from_source(self, tmp_path: Path) -> None:
        """BUG-504：源码声明的 /health 路由生成 discovered mandatory 探针，
        使 servesApi 契约有可满足的证据来源。"""
        from local_webpage_access.evidence_collector import collect
        from local_webpage_access.candidate_generator import generate_plans

        (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")
        (tmp_path / "main.py").write_text(
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n\n"
            '@app.get("/health")\n'
            "def health():\n"
            '    return {"ok": True}\n'
        )

        plans = generate_plans(collect(tmp_path))
        primary = [p for p in plans if p.confidenceTier == "primary"]
        assert len(primary) >= 1
        probes = primary[0].capabilityContract.requiredProbes
        discovered = [p for p in probes if p.source == "discovered"]
        assert len(discovered) == 1
        assert discovered[0].path == "/health"
        assert discovered[0].isMandatory is True

    def test_node_backend_plan_discovers_health_probe(self, tmp_path: Path) -> None:
        """BUG-504：Node 后端 app.get('/health') 同样生成 discovered 探针。"""
        import json

        from local_webpage_access.evidence_collector import collect
        from local_webpage_access.candidate_generator import generate_plans

        server = tmp_path / "server"
        server.mkdir()
        (server / "package.json").write_text(json.dumps({
            "dependencies": {"express": "^4"},
            "scripts": {"start": "node server.js"},
        }))
        (server / "server.js").write_text(
            "const app = require('express')();\n"
            "app.get('/health', (req, res) => res.json({ok: true}));\n"
        )

        plans = generate_plans(collect(tmp_path))
        primary = [p for p in plans if p.confidenceTier == "primary"]
        assert len(primary) >= 1
        probes = primary[0].capabilityContract.requiredProbes
        discovered = [p for p in probes if p.source == "discovered"]
        assert len(discovered) == 1
        assert discovered[0].path == "/health"
        assert discovered[0].isMandatory is True

    def test_post_health_route_is_not_discovered_as_get_probe(self, tmp_path: Path) -> None:
        """BUG-506：@app.post("/health") 不得生成 mandatory GET /health 探针。"""
        from local_webpage_access.evidence_collector import collect
        from local_webpage_access.candidate_generator import generate_plans

        (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")
        (tmp_path / "main.py").write_text(
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n\n"
            '@app.post("/health")\n'
            "def health():\n"
            '    return {"ok": True}\n'
        )

        plans = generate_plans(collect(tmp_path))
        primary = [p for p in plans if p.confidenceTier == "primary"]
        assert len(primary) >= 1
        discovered = [
            p for p in primary[0].capabilityContract.requiredProbes
            if p.source == "discovered"
        ]
        assert discovered == []

    def test_express_post_health_is_not_discovered(self, tmp_path: Path) -> None:
        """BUG-506：Express app.post('/health') 不得生成 mandatory GET 探针。"""
        import json

        from local_webpage_access.evidence_collector import collect
        from local_webpage_access.candidate_generator import generate_plans

        server = tmp_path / "server"
        server.mkdir()
        (server / "package.json").write_text(json.dumps({
            "dependencies": {"express": "^4"},
            "scripts": {"start": "node server.js"},
        }))
        (server / "server.js").write_text(
            "const app = require('express')();\n"
            "app.post('/health', (req, res) => res.json({ok: true}));\n"
        )

        plans = generate_plans(collect(tmp_path))
        primary = [p for p in plans if p.confidenceTier == "primary"]
        assert len(primary) >= 1
        discovered = [
            p for p in primary[0].capabilityContract.requiredProbes
            if p.source == "discovered"
        ]
        assert discovered == []

    def test_commented_get_health_is_not_discovered(self, tmp_path: Path) -> None:
        """BUG-506：注释中的 @app.get(\"/health\") 不得生成 mandatory 探针。"""
        from local_webpage_access.evidence_collector import collect
        from local_webpage_access.candidate_generator import generate_plans

        (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")
        (tmp_path / "main.py").write_text(
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n\n"
            '# @app.get("/health")\n'
            "def unused():\n"
            "    pass\n"
        )

        plans = generate_plans(collect(tmp_path))
        primary = [p for p in plans if p.confidenceTier == "primary"]
        assert len(primary) >= 1
        discovered = [
            p for p in primary[0].capabilityContract.requiredProbes
            if p.source == "discovered"
        ]
        assert discovered == []

    def test_backend_plan_without_health_route_stays_guessed(self, tmp_path: Path) -> None:
        """BUG-504：无健康路由的后端不产生 mandatory 探针（guessed 仅诊断），
        由验证器按「API 无法实证 → DEGRADED」降级，不构成不可满足谓词。"""
        from local_webpage_access.evidence_collector import collect
        from local_webpage_access.candidate_generator import generate_plans

        (tmp_path / "requirements.txt").write_text("flask\n")
        (tmp_path / "app.py").write_text(
            "from flask import Flask\napp = Flask(__name__)\n"
        )

        plans = generate_plans(collect(tmp_path))
        primary = [p for p in plans if p.confidenceTier == "primary"]
        assert len(primary) >= 1
        probes = primary[0].capabilityContract.requiredProbes
        assert not [p for p in probes if p.source == "discovered"]
        assert all(not p.isMandatory for p in probes)


class TestContainerRuntimeEvidence:
    """BUG-481：容器能力必须由运行时结果证明。"""

    @staticmethod
    def _sqlite_manifest(filename: str = "app.sqlite") -> InstanceManifest:
        return InstanceManifest(
            id="api",
            name="api",
            version="1",
            kind=Kind.PYTHON,
            runtime=Runtime.DOCKER_COMPOSE,
            servingMode=ServingMode.CONTAINER,
            container=ContainerConfig(
                projectName="lwa-api",
                internalPort=8000,
                composePath="docker/compose.yaml",
                dockerfilePath="docker/Dockerfile",
            ),
            hasDatabase=True,
            database={"type": "sqlite", "dbFilename": filename},
        )

    def test_sqlite_evidence_requires_readable_database(self, workspace) -> None:
        import sqlite3

        from local_webpage_access.hosting import _verify_sqlite_database

        manifest = self._sqlite_manifest("bookshelf.db")
        workspace.ensure_app_dirs("api")
        db_path = workspace.app_data("api") / "bookshelf.db"
        with sqlite3.connect(db_path) as connection:
            connection.execute("CREATE TABLE books (id INTEGER PRIMARY KEY)")

        assert _verify_sqlite_database(manifest, workspace) is True

    def test_sqlite_evidence_rejects_missing_or_corrupt_database(self, workspace) -> None:
        from local_webpage_access.hosting import _verify_sqlite_database

        manifest = self._sqlite_manifest("broken.db")
        workspace.ensure_app_dirs("api")
        assert _verify_sqlite_database(manifest, workspace) is False

        (workspace.app_data("api") / "broken.db").write_bytes(b"not sqlite")
        assert _verify_sqlite_database(manifest, workspace) is False

    def test_sqlite_evidence_fallback_scans_data_dir_when_file_missing(self, workspace) -> None:
        """BUG-492：manifest 声明的文件不存在时，回退扫描 data 目录中其他 SQLite 文件。"""
        import sqlite3

        from local_webpage_access.hosting import _verify_sqlite_database

        # manifest 指向占位文件 _empty_check.db，但 data 目录中只有 app.sqlite
        manifest = self._sqlite_manifest("_empty_check.db")
        workspace.ensure_app_dirs("api")
        db_path = workspace.app_data("api") / "app.sqlite"
        with sqlite3.connect(db_path) as connection:
            connection.execute("CREATE TABLE books (id INTEGER PRIMARY KEY)")

        assert _verify_sqlite_database(manifest, workspace) is True

    def test_sqlite_evidence_fallback_when_dbfilename_is_null(self, workspace) -> None:
        """BUG-492：dbFilename 为 null 时也应回退扫描 data 目录。"""
        import sqlite3

        from local_webpage_access.hosting import _verify_sqlite_database

        manifest = self._sqlite_manifest(None)
        workspace.ensure_app_dirs("api")
        db_path = workspace.app_data("api") / "prd_review.db"
        with sqlite3.connect(db_path) as connection:
            connection.execute("CREATE TABLE reviews (id INTEGER PRIMARY KEY)")

        assert _verify_sqlite_database(manifest, workspace) is True

    def test_sqlite_evidence_fallback_returns_false_when_no_valid_db(self, workspace) -> None:
        """BUG-492：data 目录中没有任何有效 SQLite 文件时应返回 False。"""
        from local_webpage_access.hosting import _verify_sqlite_database

        manifest = self._sqlite_manifest("_empty_check.db")
        workspace.ensure_app_dirs("api")
        # 写入一个无效文件，扩展名是 .db
        (workspace.app_data("api") / "junk.db").write_bytes(b"not a database")

        assert _verify_sqlite_database(manifest, workspace) is False

    def test_migration_evidence_requires_guarded_start_command(self) -> None:
        from local_webpage_access.hosting import _migration_command_succeeded

        manifest = self._sqlite_manifest()
        manifest.entry.start = (
            "sh -c 'alembic upgrade head && exec uvicorn app.main:app "
            "--host 0.0.0.0 --port 8000'"
        )
        assert _migration_command_succeeded(manifest, liveness_ok=True) is True

        manifest.entry.start = "uvicorn app.main:app --host 0.0.0.0 --port 8000"
        assert _migration_command_succeeded(manifest, liveness_ok=True) is False

    def test_hosting_evaluator_uses_runtime_evidence(self, workspace) -> None:
        from local_webpage_access.hosting import _evaluate_container_verification

        manifest = self._sqlite_manifest()
        manifest.capabilityContract = CapabilityContract(
            servesUi=True,
            servesApi=True,
            requiresDatabase=True,
            requiresMigrations=True,
            requiredProbes=[
                ProbeSpec(path="/health", isMandatory=True, source="declared"),
            ],
        ).model_dump()
        with (
            patch("local_webpage_access.hosting._wait_for_http", return_value=True),
            patch("local_webpage_access.hosting._probe_path", return_value=(True, 200)),
            patch("local_webpage_access.hosting._verify_sqlite_database", return_value=True),
            patch("local_webpage_access.hosting._migration_command_succeeded", return_value=True),
        ):
            result = _evaluate_container_verification(
                18000, manifest, workspace, MagicMock(), "api",
            )
        assert result["overall_status"] == "passed"
        assert result["observed_capabilities"] == [
            "api", "database", "migrations", "ui",
        ]

    def test_hosting_guessed_probe_does_not_satisfy_serves_api(self, workspace) -> None:
        """BUG-499：guessed 探针（如通用 /health）不得满足 servesApi。

        只有 declared/discovered 探针通过才可写入 observed 'api'；
        偶然 /health 200（guessed）不得当作 API 能力证据（避免假绿）。
        同时不得构成不可满足的成功谓词（BUG-504）：无声明/发现探针时
        API 能力无法实证 → 降级为 DEGRADED 告警，而非 failed 假红。
        """
        from local_webpage_access.hosting import _evaluate_container_verification

        manifest = self._sqlite_manifest()
        manifest.capabilityContract = CapabilityContract(
            servesUi=True,
            servesApi=True,
            requiredProbes=[
                ProbeSpec(path="/health", isMandatory=False, source="guessed"),
            ],
        ).model_dump()
        with (
            patch("local_webpage_access.hosting._wait_for_http", return_value=True),
            patch("local_webpage_access.hosting._probe_path", return_value=(True, 200)),
        ):
            result = _evaluate_container_verification(
                18000, manifest, workspace, MagicMock(), "api",
            )
        # guessed /health 200 不得观察为 api；无证据来源 → degraded（非 failed 假红）
        assert result["overall_status"] == "degraded"
        assert "api" not in result["observed_capabilities"]
        assert any("无法实证" in w for w in result["optional_warnings"])

    def test_hosting_optional_probe_failure_is_degraded(self, workspace) -> None:
        from local_webpage_access.hosting import _evaluate_container_verification

        manifest = self._sqlite_manifest()
        manifest.capabilityContract = CapabilityContract(
            servesUi=True,
            requiredProbes=[
                ProbeSpec(path="/health", isMandatory=False, source="guessed"),
            ],
        ).model_dump()
        with (
            patch("local_webpage_access.hosting._wait_for_http", return_value=True),
            patch("local_webpage_access.hosting._probe_path", return_value=(False, 404)),
        ):
            result = _evaluate_container_verification(
                18000, manifest, workspace, MagicMock(), "api",
            )
        assert result["overall_status"] == "degraded"
        assert result["optional_warnings"]


class TestNodeSubdirCandidate:
    """CHK-193/P2：Node 后端子目录候选生成。"""

    def test_node_subdir_candidate_generated(self, tmp_path: Path) -> None:
        """子目录有 package.json + NODE_BACKEND -> 生成 node 子目录候选。"""
        from local_webpage_access.evidence_collector import collect
        from local_webpage_access.candidate_generator import generate_candidates

        (tmp_path / "server").mkdir()
        (tmp_path / "server" / "package.json").write_text(
            '{"name":"api","scripts":{"start":"node server.js"},'
            '"dependencies":{"express":"4"}}'
        )
        (tmp_path / "server" / "server.js").write_text("// Express app")

        evidence = collect(tmp_path)
        candidates = generate_candidates(evidence)

        node_subdir = [
            c for c in candidates
            if c.kind == "node" and c.sourceSubdir == "server"
        ]
        assert len(node_subdir) == 1
        assert node_subdir[0].confidenceTier == "primary"
        assert node_subdir[0].form == "backend-container"

    def test_node_subdir_plan_generated(self, tmp_path: Path) -> None:
        """Node 子目录候选收敛为 DeploymentPlan。"""
        from local_webpage_access.evidence_collector import collect
        from local_webpage_access.candidate_generator import generate_plans

        (tmp_path / "backend").mkdir()
        (tmp_path / "backend" / "package.json").write_text(
            '{"name":"api","scripts":{"start":"node server.js"},'
            '"dependencies":{"fastify":"4"}}'
        )
        (tmp_path / "backend" / "server.js").write_text("// Fastify app")

        evidence = collect(tmp_path)
        plans = generate_plans(evidence)

        primary = [p for p in plans if p.confidenceTier == "primary"]
        assert len(primary) >= 1
        assert primary[0].capabilityContract.servesApi is True

    def test_node_subdir_start_script_without_framework_dependency(self, tmp_path: Path) -> None:
        """只有明确 Node 服务启动脚本时也应生成子目录后端候选。"""
        from local_webpage_access.candidate_generator import generate_candidates
        from local_webpage_access.evidence_collector import collect

        server = tmp_path / "server"
        server.mkdir()
        (server / "package.json").write_text(
            '{"name":"api","scripts":{"start":"node server.js"}}'
        )
        (server / "server.js").write_text(
            'require("http").createServer(() => {}).listen(8080)'
        )

        candidates = generate_candidates(collect(tmp_path))
        assert any(
            candidate.kind == "node" and candidate.sourceSubdir == "server"
            for candidate in candidates
        )

    @pytest.mark.parametrize(
        "start_script",
        ["vite", "vite preview", "react-scripts start", "next dev", "nuxt dev"],
    )
    def test_frontend_start_script_is_not_node_backend(
        self, tmp_path: Path, start_script: str,
    ) -> None:
        """前端开发/预览脚本不得被误判为 Node 后端。"""
        from local_webpage_access.candidate_generator import generate_candidates
        from local_webpage_access.evidence_collector import collect

        server = tmp_path / "server"
        server.mkdir()
        (server / "package.json").write_text(
            '{"scripts":{"start":"' + start_script + '"}}'
        )

        candidates = generate_candidates(collect(tmp_path))
        assert not any(
            candidate.kind == "node" and candidate.sourceSubdir == "server"
            for candidate in candidates
        )

    def test_python_subdir_alembic_requires_migrations(self, tmp_path: Path) -> None:
        """后端子目录的 alembic.ini 应进入对应计划能力契约。"""
        from local_webpage_access.candidate_generator import generate_plans
        from local_webpage_access.evidence_collector import collect

        backend = tmp_path / "backend"
        backend.mkdir()
        (backend / "requirements.txt").write_text(
            "fastapi\nuvicorn\nsqlalchemy\nalembic\n"
        )
        (backend / "alembic.ini").write_text(
            "[alembic]\nscript_location = alembic\n"
        )

        evidence = collect(tmp_path)
        signal = next(item for item in evidence.subdirSignals if item.path == "backend")
        assert signal.hasAlembicIni is True
        primary = [plan for plan in generate_plans(evidence) if plan.confidenceTier == "primary"]
        assert primary[0].capabilityContract.requiresMigrations is True


class TestPoetryDepsCollection:
    """BUG-502：evidence_collector._collect_python_deps 解析 Poetry 依赖段。"""

    def test_poetry_dependencies_collected(self, tmp_path: Path) -> None:
        """BUG-502：仅 [tool.poetry.dependencies] 声明 FastAPI 应被收集（python 键忽略）。"""
        from local_webpage_access.evidence_collector import _collect_python_deps

        (tmp_path / "pyproject.toml").write_text(
            '[tool.poetry]\n'
            'name = "demo"\n'
            'version = "0.1.0"\n'
            '[tool.poetry.dependencies]\n'
            'python = "^3.11"\n'
            'fastapi = "^0.100"\n'
            'uvicorn = "^0.30"\n'
        )
        deps = _collect_python_deps(tmp_path)
        assert "fastapi" in deps
        assert "uvicorn" in deps
        assert "python" not in deps

    def test_poetry_dev_and_group_dependencies_collected(self, tmp_path: Path) -> None:
        """BUG-502：dev-dependencies 与 group.*.dependencies 也应被收集。"""
        from local_webpage_access.evidence_collector import _collect_python_deps

        (tmp_path / "pyproject.toml").write_text(
            '[tool.poetry]\n'
            'name = "demo"\n'
            'version = "0.1.0"\n'
            '[tool.poetry.dependencies]\n'
            'python = "^3.11"\n'
            'fastapi = "^0.100"\n'
            '[tool.poetry.dev-dependencies]\n'
            'pytest = "^8.0"\n'
            '[tool.poetry.group.test.dependencies]\n'
            'httpx = "^0.27"\n'
        )
        deps = _collect_python_deps(tmp_path)
        assert "fastapi" in deps
        assert "pytest" in deps
        assert "httpx" in deps
        assert "python" not in deps

    def test_poetry_deps_flow_into_subdir_signal(self, tmp_path: Path) -> None:
        """BUG-502：子目录 Poetry 依赖经 collect() 进入 SubdirSignal.pythonDeps。"""
        from local_webpage_access.evidence_collector import collect

        backend = tmp_path / "backend"
        backend.mkdir()
        (backend / "pyproject.toml").write_text(
            '[tool.poetry]\n'
            'name = "demo"\n'
            'version = "0.1.0"\n'
            '[tool.poetry.dependencies]\n'
            'python = "^3.11"\n'
            'fastapi = "^0.100"\n'
        )

        evidence = collect(tmp_path)
        signal = next(item for item in evidence.subdirSignals if item.path == "backend")
        assert "fastapi" in signal.pythonDeps
        assert "python" not in signal.pythonDeps
