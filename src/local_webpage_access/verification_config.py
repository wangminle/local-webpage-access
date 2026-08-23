"""实例级验证探针覆盖层（CHK-252 第三批）。

用户显式配置的 ``verificationOverrides`` 持久化在 ``local-web.json``，
导入/重扫/升级不得覆盖。与 ``capabilityContract.requiredProbes`` 合并后供
Gate-C 验证使用。
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from local_webpage_access.errors import SchemaError
from local_webpage_access.models import (
    CapabilityContract,
    InstanceManifest,
    ProbeSpec,
    VerificationOverrides,
)

# 探针允许的 HTTP 方法（BUG-588 写入前结构校验）
_ALLOWED_PROBE_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})


def _normalize_probe(raw: dict[str, Any]) -> ProbeSpec:
    return ProbeSpec(
        path=str(raw.get("path") or "/"),
        method=str(raw.get("method") or "GET"),
        expectedStatus=int(raw.get("expectedStatus") or raw.get("expected_status") or 200),
        isMandatory=bool(raw.get("isMandatory", raw.get("is_mandatory", True))),
        source="declared",
        description=str(raw.get("description") or "用户显式配置探针"),
    )


def get_verification_overrides(manifest: InstanceManifest) -> dict[str, Any]:
    raw = getattr(manifest, "verificationOverrides", None)
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if hasattr(raw, "model_dump"):
        return raw.model_dump(mode="json")
    return {}


def validate_verification_overrides(overrides: dict[str, Any]) -> None:
    """写入前结构校验（BUG-588）：非法配置抛 :class:`SchemaError`，调用方不得持久化。

    先经 :class:`VerificationOverrides` 做类型校验（如 ``expectedStatus`` 必须为
    可转整数的值），再补充模型未覆盖的语义约束：path 以 ``/`` 开头、method 合法、
    ``expectedStatus`` 落在 100..599。
    """
    try:
        parsed = VerificationOverrides.model_validate(overrides)
    except ValidationError as exc:
        errors = exc.errors()
        first_msg = str(errors[0]["msg"]) if errors else str(exc)
        raise SchemaError(
            f"verificationOverrides 结构非法：{first_msg}",
        ) from exc
    for probe in parsed.probes:
        if not probe.path.startswith("/"):
            raise SchemaError(f"探针 path 必须以 / 开头：{probe.path!r}")
        if probe.method.upper() not in _ALLOWED_PROBE_METHODS:
            raise SchemaError(f"探针 method 非法：{probe.method!r}")
        if not 100 <= probe.expectedStatus <= 599:
            raise SchemaError(
                f"探针 expectedStatus 必须为 100..599 的整数：{probe.expectedStatus!r}"
            )


def set_verification_overrides(manifest: InstanceManifest, overrides: dict[str, Any] | None) -> None:
    if overrides:
        validate_verification_overrides(overrides)
    manifest.verificationOverrides = overrides if overrides else None


def effective_capability_contract(manifest: InstanceManifest) -> CapabilityContract:
    """合并持久化契约与用户覆盖，生成 Gate-C 验证用契约。"""
    from local_webpage_access.hosting import _load_capability_contract

    base = _load_capability_contract(manifest)
    overrides = get_verification_overrides(manifest)
    user_probes_raw = overrides.get("probes") or []
    disable_auto = bool(
        overrides.get("disableAutoProbes") or overrides.get("disableGuessedProbes")
    )

    probes: list[ProbeSpec] = []
    seen_paths: set[str] = set()

    for item in user_probes_raw:
        if not isinstance(item, dict):
            continue
        spec = _normalize_probe(item)
        probes.append(spec)
        seen_paths.add(spec.path)

    for spec in base.requiredProbes:
        if spec.path in seen_paths:
            continue
        # BUG-587：disableAutoProbes 同时关闭 guessed 与 discovered 自动探针
        if disable_auto and spec.source in ("guessed", "discovered"):
            continue
        if spec.source == "discovered":
            # CHK-252：静态扫描发现的端点默认仅诊断，不作 mandatory 门槛
            probes.append(spec.model_copy(update={"isMandatory": False}))
            continue
        probes.append(spec)

    return CapabilityContract(
        servesUi=base.servesUi,
        servesApi=base.servesApi,
        requiresDatabase=base.requiresDatabase,
        requiresMigrations=base.requiresMigrations,
        requiredProbes=probes,
    )


def overrides_to_dict(manifest: InstanceManifest) -> dict[str, Any]:
    ov = get_verification_overrides(manifest)
    base = manifest.capabilityContract if isinstance(manifest.capabilityContract, dict) else {}
    inherited = [
        p if isinstance(p, dict) else p.model_dump(mode="json")
        for p in (base.get("requiredProbes") or [])
    ]
    return {
        "overrides": ov,
        "inheritedProbes": inherited,
        "effectiveProbes": [
            p.model_dump(mode="json") for p in effective_capability_contract(manifest).requiredProbes
        ],
    }


__all__ = [
    "effective_capability_contract",
    "get_verification_overrides",
    "overrides_to_dict",
    "set_verification_overrides",
    "validate_verification_overrides",
]
