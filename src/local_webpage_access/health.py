"""实例健康检查（WBS-18.05 / 18.06 / 18.07）。

对运行中的实例做 HTTP 探测，结果写回 registry：
* 成功 → ``last_health_check_at``（WBS-18.06）；
* 失败 → ``last_error`` + ``error`` 事件（WBS-18.07）。

容器实例与静态实例都通过暴露的 ``hostPort`` 探测。容器实例可叠加
``docker compose ps`` 的进程级状态（:func:`check_health` 会先判端口、
再可选地校验容器运行态）。
"""

from __future__ import annotations

import urllib.error
from dataclasses import dataclass
from typing import TYPE_CHECKING

from local_webpage_access.config import Config
from local_webpage_access.logging import get_logger, now_iso
from local_webpage_access.models import InstanceManifest, Status
from local_webpage_access.paths import Workspace
from local_webpage_access.probe import mark_probe_url, urlopen_direct
from local_webpage_access.registry import Registry

if TYPE_CHECKING:
    from local_webpage_access.models import CapabilityContract, ProbeSpec

log = get_logger("health")

_DEFAULT_TIMEOUT = 2.0


@dataclass(frozen=True)
class HealthResult:
    """健康检查结果。"""

    ok: bool
    host_port: int | None
    status_code: int | None = None
    reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "hostPort": self.host_port,
            "statusCode": self.status_code,
            "reason": self.reason,
            "checkedAt": now_iso(),
        }


def http_ok(host_port: int, *, timeout: float = _DEFAULT_TIMEOUT) -> tuple[bool, int | None]:
    """单次 HTTP GET 健康探测。

    返回 ``(是否成功, HTTP 状态码)``。2xx/3xx 视为成功；连接失败/超时/4xx/5xx 失败。
    """
    url = mark_probe_url(f"http://127.0.0.1:{host_port}/")
    try:
        resp = urlopen_direct(url, timeout=timeout)
        code = getattr(resp, "status", None) or resp.getcode()
        return (200 <= int(code) < 400, int(code))
    except urllib.error.HTTPError as exc:
        # HTTPError 也是"得到了响应"，4xx 视为不健康，5xx 同样
        return (False, exc.code)
    except Exception as exc:  # noqa: BLE001 — 连接拒绝/超时统一为失败
        log.debug("健康探测失败（port=%s）：%s", host_port, exc)
        return (False, None)


# Gate-C C.04：API 路径探测候选（按常见程度排序）
_API_PROBE_PATHS = ("/health", "/api/", "/api/v1/", "/api/health", "/healthz")


def api_probe(
    host_port: int, *, timeout: float = _DEFAULT_TIMEOUT
) -> tuple[bool | None, str | None]:
    """Gate-C C.04/C.05：探测常见 API 路径，返回 ``(是否成功, 命中路径)``。

    逐个尝试 ``_API_PROBE_PATHS`` 中的路径，首个 2xx/3xx 即成功。
    全部 404 不视为失败（应用可能没有标准 API 健康端点）。
    返回 ``(None, None)`` 表示无法判定（连接拒绝等）。

    Gate-C C.05 限制：本函数是**通用猜测探针**，其结果只能产生诊断，
    不能单独判定部署失败（§6.5 成功谓词）。只有 :class:`ProbeSpec` 中
    ``isMandatory=True`` 且 ``source`` 为 ``"declared"`` 或 ``"discovered"``
    的探针才可作成功门槛。
    """
    for path in _API_PROBE_PATHS:
        url = mark_probe_url(f"http://127.0.0.1:{host_port}{path}")
        try:
            resp = urlopen_direct(url, timeout=timeout)
            code = getattr(resp, "status", None) or resp.getcode()
            if 200 <= int(code) < 400:
                return (True, path)
        except urllib.error.HTTPError as exc:
            # 404 = 该路径不存在，继续尝试下一个
            if exc.code == 404:
                continue
            # 其它 HTTP 错误也继续尝试
            continue
        except Exception:  # noqa: BLE001
            continue
    # 全部 404 或不可达 → None（无法判定）
    return (None, None)


# ---- Gate-C C.05：证据驱动探针 ----------------------------------------------


def run_probe_spec(
    host_port: int,
    spec: "ProbeSpec",
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> tuple[bool, int | None]:
    """Gate-C C.05：执行单个 :class:`ProbeSpec` 探针。

    返回 ``(passed, status_code)``。

    - ``passed=True``：状态码匹配 ``expectedStatus`` 或同为 2xx/3xx 范围。
    - ``passed=False``：状态码不匹配或连接失败。

    区分于 :func:`api_probe` 的通用猜测探针——``ProbeSpec`` 有明确的路径、
    方法和预期状态，可作为成功门槛。
    """
    path = spec.path or "/"
    url = mark_probe_url(f"http://127.0.0.1:{host_port}{path}")
    try:
        resp = urlopen_direct(url, timeout=timeout)
        code = getattr(resp, "status", None) or resp.getcode()
        code_int = int(code)
        if spec.expectedStatus and code_int == spec.expectedStatus:
            return (True, code_int)
        if not spec.expectedStatus and 200 <= code_int < 400:
            return (True, code_int)
        return (False, code_int)
    except urllib.error.HTTPError as exc:
        return (False, exc.code)
    except Exception:  # noqa: BLE001
        return (False, None)


def evaluate_success_predicate(
    host_port: int,
    *,
    required_probes: list["ProbeSpec"] | None = None,
    capability_contract: "CapabilityContract | None" = None,
    has_database: bool = False,
    has_migrations: bool = False,
    timeout: float = _DEFAULT_TIMEOUT,
) -> "ProbeEvaluation":
    """Gate-C C.04/C.05：评估成功谓词（§6.5）。

    成功谓词::

        plan_succeeded =
            liveness_passed
            AND all(required_probe.passed)
            AND capabilities_observed >= capability_contract.required_capabilities

    返回 :class:`ProbeEvaluation`，包含各探针结果和整体判定。

    **证据驱动语义**（§6.5）：
    - 基础存活探针（HTTP GET /）：必选，证明进程存活且端口可达。
    - ``required_probes`` 中的 mandatory 探针：必选。
    - 通用 API 猜测（:func:`api_probe`）：仅诊断，404/401 不判失败。
    - 数据库/迁移能力：由 ``has_database`` / ``has_migrations`` 传入，
      不从首页 200 推断（§6.5）。
    """
    # 基础存活探针
    liveness_ok, liveness_code = http_ok(host_port, timeout=timeout)

    # 必选探针
    mandatory_results: list[tuple[str, bool, int | None]] = []
    mandatory_all_passed = True
    successful_business_probe = False
    for spec in (required_probes or []):
        if not spec.isMandatory:
            continue
        passed, code = run_probe_spec(host_port, spec, timeout=timeout)
        mandatory_results.append((spec.path, passed, code))
        if passed:
            successful_business_probe = True
        if not passed:
            mandatory_all_passed = False

    # 通用 API 猜测（仅诊断）
    api_guess_ok, api_guess_path = api_probe(host_port, timeout=timeout)

    # 可选探针 warning 收集
    optional_warnings: list[str] = []
    for spec in (required_probes or []):
        if spec.isMandatory:
            continue
        passed, code = run_probe_spec(host_port, spec, timeout=timeout)
        if passed:
            successful_business_probe = True
        if not passed:
            optional_warnings.append(
                f"可选探针 {spec.path} 未通过（code={code}）"
            )

    # 观测到的能力
    observed: set[str] = set()
    if liveness_ok:
        observed.add("ui")  # 存活即服务可达（静态或前端）
        if api_guess_ok or successful_business_probe:
            observed.add("api")
        # BUG-481：契约只表达“要求什么”，不能当作“已证明什么”。
        # DB/迁移能力必须由调用方的实际检查结果显式传入。
        if has_database:
            observed.add("database")
        if has_migrations:
            observed.add("migrations")

    # 能力覆盖校验
    required = set()
    if capability_contract is not None:
        required = capability_contract.required_capabilities
    capabilities_covered = required.issubset(observed)

    # 总体判定
    if not liveness_ok or not mandatory_all_passed:
        overall = "failed"
    elif not capabilities_covered:
        # CHK-192/P1：能力未覆盖且无可选探针告警 -> failed（不得假报 passed）
        overall = "failed"
    elif optional_warnings:
        overall = "degraded"
    else:
        overall = "passed"

    return ProbeEvaluation(
        liveness_passed=liveness_ok,
        liveness_code=liveness_code,
        mandatory_results=mandatory_results,
        mandatory_all_passed=mandatory_all_passed,
        api_guess_ok=api_guess_ok,
        api_guess_path=api_guess_path,
        optional_warnings=optional_warnings,
        observed_capabilities=observed,
        required_capabilities=required,
        capabilities_covered=capabilities_covered,
        overall_status=overall,
    )


@dataclass(frozen=True)
class ProbeEvaluation:
    """Gate-C C.04：探针评估结果（成功谓词各维度）。"""

    liveness_passed: bool
    liveness_code: int | None
    mandatory_results: list[tuple[str, bool, int | None]]  # [(path, passed, code)]
    mandatory_all_passed: bool
    api_guess_ok: bool | None  # None = 未猜测
    api_guess_path: str | None
    optional_warnings: list[str]
    observed_capabilities: set[str]
    required_capabilities: set[str]
    capabilities_covered: bool
    overall_status: str  # "passed" / "degraded" / "failed"

    def to_verification_fields(self) -> dict:
        """转换为 :class:`VerificationResult` 可用字段。"""
        return {
            "healthCheckPassed": self.liveness_passed,
            "requiredProbesPassed": self.mandatory_all_passed,
            "optionalProbeWarnings": list(self.optional_warnings),
            "observedCapabilities": sorted(self.observed_capabilities),
        }


def check_health(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> HealthResult:
    """对实例执行 HTTP 健康检查并写回 registry（WBS-18.05/06/07）。

    * 无可用端口 → 返回失败，不写 last_error（实例未部署）；
    * 探测成功 → 写 ``last_health_check_at``，清 ``last_error``；
    * 探测失败 → 写 ``last_error`` 与 ``error`` 事件，但不直接改 status
      （status 由 :func:`lifecycle.observe_status` 基于进程态判定，避免健康
      抖动误报 failed）。
    """
    from local_webpage_access.hosting import _load_manifest

    manifest = _load_manifest(workspace, instance_id)
    host_port = _resolve_host_port(manifest, registry)
    if not host_port:
        result = HealthResult(ok=False, host_port=None, reason="实例尚未分配端口")
        registry.add_event(
            instance_id, "health_check", "健康检查跳过：无可用端口"
        )
        return result

    ok, code = http_ok(host_port, timeout=timeout)
    if ok:
        registry.record_health_check(instance_id)
        registry.update_status(instance_id, _current_status(manifest), last_error="")
        registry.add_event(
            instance_id, "health_check", f"健康检查通过（port={host_port}, code={code}）"
        )
        log.info("实例 %s 健康检查通过（port=%s）", instance_id, host_port)
    else:
        reason = f"健康检查失败（port={host_port}, code={code}）"
        registry.update_status(
            instance_id, _current_status(manifest), last_error=reason
        )
        registry.add_event(instance_id, "health_check", reason)
        log.warning("实例 %s %s", instance_id, reason)
    return HealthResult(ok=ok, host_port=host_port, status_code=code, reason=None if ok else reason)


def _resolve_host_port(manifest: InstanceManifest, registry: Registry) -> int | None:
    """从 manifest 或 registry 解析实例暴露的 host 端口。"""
    # 优先 manifest 的 network/container 字段
    port = None
    if manifest.network and manifest.network.hostPort:
        port = manifest.network.hostPort
    if not port and manifest.container and manifest.container.hostPort:
        port = manifest.container.hostPort
    if not port and manifest.static and manifest.static.hostPort:
        port = manifest.static.hostPort
    if port:
        return int(port)
    # 回退到 registry
    if manifest.runtime.value == "docker-compose":
        row = registry.get_container(manifest.id)
        if row and row.get("host_port"):
            return int(row["host_port"])
    else:
        row = registry.get_static_site(manifest.id)
        if row and row.get("host_port"):
            return int(row["host_port"])
    return None


def _current_status(manifest: InstanceManifest) -> str:
    val = manifest.status.value if isinstance(manifest.status, Status) else manifest.status
    return val or Status.PENDING.value


__all__ = [
    "HealthResult",
    "http_ok",
    "api_probe",
    "check_health",
    "run_probe_spec",
    "evaluate_success_predicate",
    "ProbeEvaluation",
]
