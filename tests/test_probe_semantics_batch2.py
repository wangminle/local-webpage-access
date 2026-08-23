"""第二批 CHK-252：探针三层语义 / discovered 降可选 / 状态与验证结论分离。

直接针对 :func:`local_webpage_access.hosting._evaluate_container_verification`
（部署验证的真实现），覆盖：
- 可达性：任意 HTTP 响应即存活（``_http_ok`` 含 401/404/5xx）；
- 就绪性：只有用户显式声明（verificationOverrides → declared+mandatory）的
  探针按期望状态通过才算就绪，失败 => failed；
- 能力证据：契约探针与能力覆盖只产生告警/备注，不再判 failed；
- guessed 404 中性丢弃；401/403 受保护不降级；5xx 告警。
"""

from __future__ import annotations

import urllib.error
from unittest.mock import MagicMock


from local_webpage_access import hosting
from local_webpage_access.models import (
    ContainerConfig,
    InstanceManifest,
    Kind,
    ResourceProfile,
    Runtime,
    ServingMode,
    Status,
)


def _manifest(
    *,
    probes: list[dict] | None = None,
    contract: dict | None = None,
    overrides: dict | None = None,
) -> InstanceManifest:
    if contract is None:
        contract = {
            "servesUi": True,
            "servesApi": False,
            "requiresDatabase": False,
            "requiresMigrations": False,
            "requiredProbes": probes or [],
        }
    m = InstanceManifest(
        id="batch2",
        name="batch2",
        version="1",
        kind=Kind.PYTHON,
        stack=[],
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.SMALL,
        status=Status.STOPPED,
        container=ContainerConfig(
            projectName="batch2",
            internalPort=8000,
            hostPort=19999,
            composePath="/tmp/batch2/compose.yaml",
            dockerfilePath="/tmp/batch2/Dockerfile",
        ),
    )
    m.capabilityContract = contract
    if overrides is not None:
        m.verificationOverrides = overrides
    return m


def _evaluate(
    manifest: InstanceManifest,
    *,
    probe_results: dict[str, tuple[bool, int | None]],
    liveness: bool = True,
) -> dict:
    """在 monkeypatch 掉网络探测后运行真实验证函数（用后恢复）。"""
    def _fake_probe_path(host_port, path, **kwargs):
        return probe_results.get(path, (True, 200))

    orig_wait = hosting._wait_for_http
    orig_probe = hosting._probe_path
    hosting._wait_for_http = lambda *a, **k: liveness
    hosting._probe_path = _fake_probe_path
    try:
        return hosting._evaluate_container_verification(
            19999, manifest, MagicMock(), MagicMock(), manifest.id
        )
    finally:
        hosting._wait_for_http = orig_wait
        hosting._probe_path = orig_probe


class TestReachability:
    """可达性层：收到任意 HTTP 状态即存活。"""

    def test_http_ok_accepts_401(self, monkeypatch) -> None:
        def _raise(url, timeout=2.0):
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

        monkeypatch.setattr(hosting, "urlopen_direct", _raise)
        assert hosting._http_ok(19999) is True

    def test_http_ok_accepts_404(self, monkeypatch) -> None:
        def _raise(url, timeout=2.0):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        monkeypatch.setattr(hosting, "urlopen_direct", _raise)
        assert hosting._http_ok(19999) is True

    def test_http_ok_rejects_connection_refused(self, monkeypatch) -> None:
        def _raise(url, timeout=2.0):
            raise OSError("connection refused")

        monkeypatch.setattr(hosting, "urlopen_direct", _raise)
        assert hosting._http_ok(19999) is False


class TestGuessedProbeSemantics:
    """guessed 探针：404 中性 / 401-403 受保护不降级 / 5xx 告警。"""

    def test_guessed_404_is_neutral(self) -> None:
        m = _manifest(
            probes=[{"path": "/health", "isMandatory": False, "source": "guessed"}]
        )
        result = _evaluate(m, probe_results={"/health": (False, 404)})
        assert result["overall_status"] == "passed"
        assert not result["optional_warnings"]

    def test_guessed_401_is_protected_note_not_warning(self) -> None:
        m = _manifest(
            probes=[{"path": "/health", "isMandatory": False, "source": "guessed"}]
        )
        result = _evaluate(m, probe_results={"/health": (False, 401)})
        assert result["overall_status"] == "passed"
        assert not result["optional_warnings"]
        assert any("受保护" in note for note in result["probe_notes"])

    def test_guessed_500_warns_degraded(self) -> None:
        m = _manifest(
            probes=[{"path": "/health", "isMandatory": False, "source": "guessed"}]
        )
        result = _evaluate(m, probe_results={"/health": (False, 500)})
        assert result["overall_status"] == "degraded"
        assert any("/health" in w for w in result["optional_warnings"])


class TestDiscoveredProbeOptional:
    """discovered 探针降为可选证据：失败只告警、不回滚。"""

    def test_discovered_mandatory_in_old_manifest_500_degrades_not_fails(self) -> None:
        # 旧实例 manifest 里 discovered 探针带 isMandatory=True——
        # effective_capability_contract 应降级为可选证据。
        m = _manifest(
            probes=[{"path": "/health", "isMandatory": True, "source": "discovered"}]
        )
        result = _evaluate(m, probe_results={"/health": (False, 500)})
        assert result["overall_status"] == "degraded"
        assert result["mandatory_all_passed"] is True

    def test_discovered_401_note_not_failure(self) -> None:
        m = _manifest(
            probes=[{"path": "/health", "isMandatory": True, "source": "discovered"}]
        )
        result = _evaluate(m, probe_results={"/health": (False, 401)})
        assert result["overall_status"] == "passed"
        assert any("受保护" in note for note in result["probe_notes"])


class TestUserDeclaredProbeGate:
    """就绪门槛：只有用户显式声明的探针失败才 failed。"""

    def test_user_probe_500_fails(self) -> None:
        m = _manifest(
            overrides={
                "probes": [{"path": "/public-health", "expectedStatus": 200}],
            }
        )
        result = _evaluate(m, probe_results={"/public-health": (False, 500)})
        assert result["overall_status"] == "failed"
        assert result["mandatory_all_passed"] is False

    def test_user_probe_expected_401_passes(self) -> None:
        m = _manifest(
            overrides={
                "probes": [{"path": "/health", "expectedStatus": 401}],
            }
        )
        # 真实 _probe_path 支持 expectedStatus 精确匹配：期望 401 且返回 401 → 通过。
        result = _evaluate(m, probe_results={"/health": (True, 401)})
        assert result["overall_status"] == "passed"

    def test_contract_declared_probe_still_gates(self) -> None:
        # 契约内 declared+mandatory 探针（项目显式声明）仍可作为门槛。
        m = _manifest(
            probes=[{"path": "/ready", "isMandatory": True, "source": "declared"}]
        )
        result = _evaluate(m, probe_results={"/ready": (False, 503)})
        assert result["overall_status"] == "failed"

    def test_disable_auto_probes_skips_contract_probes(self) -> None:
        m = _manifest(
            probes=[{"path": "/health", "isMandatory": False, "source": "guessed"}],
            overrides={"probes": [], "disableAutoProbes": True},
        )
        result = _evaluate(m, probe_results={"/health": (False, 500)})
        assert result["overall_status"] == "passed"
        assert not result["optional_warnings"]


class TestCapabilityEvidenceNotFailure:
    """能力证据是证据不是门槛：未覆盖只告警。"""

    def test_missing_capabilities_warn_not_fail(self) -> None:
        m = _manifest(
            contract={
                "servesUi": True,
                "servesApi": True,
                "requiresDatabase": True,
                "requiresMigrations": False,
                "requiredProbes": [],
            }
        )
        result = _evaluate(m, probe_results={})
        assert result["overall_status"] == "degraded"
        assert any("能力证据未覆盖" in w for w in result["optional_warnings"])

    def test_api_probe_success_observes_api_capability(self) -> None:
        m = _manifest(
            contract={
                "servesUi": True,
                "servesApi": True,
                "requiresDatabase": False,
                "requiresMigrations": False,
                "requiredProbes": [
                    {"path": "/api/health", "isMandatory": False, "source": "discovered"}
                ],
            }
        )
        result = _evaluate(m, probe_results={"/api/health": (True, 200)})
        assert "api" in result["observed_capabilities"]


class TestStatusVerificationSeparation:
    """进程状态与验证结论分离：Status 不再写入 DEGRADED。"""

    def test_status_enum_degraded_retained_for_compat(self) -> None:
        # 枚举成员保留（旧 manifest 兼容 + UI 标签映射），但部署路径不再赋值。
        assert Status.DEGRADED.value == "degraded"

    def test_deploy_path_never_assigns_degraded(self) -> None:
        # 源码检查：hosting.py 中不再出现 Status.DEGRADED 赋值。
        import inspect

        source = inspect.getsource(hosting)
        assert "Status.DEGRADED" not in source


class TestDisableAutoProbesCoversDiscovered:
    """BUG-587：disableAutoProbes 同时关闭 guessed 与 discovered 自动探针。"""

    def test_effective_probes_keep_only_user_probes_when_auto_disabled(self) -> None:
        from local_webpage_access.verification_config import effective_capability_contract

        m = _manifest(
            probes=[
                {"path": "/health", "isMandatory": True, "source": "discovered"},
                {"path": "/api/", "isMandatory": False, "source": "guessed"},
            ],
            overrides={
                "probes": [{"path": "/ready", "expectedStatus": 200}],
                "disableAutoProbes": True,
            },
        )
        effective = effective_capability_contract(m)
        assert [p.path for p in effective.requiredProbes] == ["/ready"]

    def test_disable_auto_probes_also_skips_discovered_in_evaluation(self) -> None:
        m = _manifest(
            probes=[{"path": "/health", "isMandatory": True, "source": "discovered"}],
            overrides={"probes": [], "disableAutoProbes": True},
        )
        result = _evaluate(m, probe_results={"/health": (False, 500)})
        assert result["overall_status"] == "passed"
        assert not result["optional_warnings"]
