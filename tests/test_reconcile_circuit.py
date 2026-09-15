"""issue #35 建议 5：实例构建连续失败熔断。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from local_webpage_access.models import (
    ContainerConfig,
    EntryConfig,
    InstanceManifest,
    Kind,
    ResourceProfile,
    Runtime,
    ServingMode,
)
from local_webpage_access.reconcile_circuit import (
    MANUAL_THRESHOLD,
    clear_circuit,
    is_blocked,
    record_failure,
    status_note,
)


def _manifest() -> InstanceManifest:
    return InstanceManifest(
        id="demo",
        name="demo",
        version="1",
        kind=Kind.PYTHON,
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.SMALL,
        container=ContainerConfig(
            projectName="lwa-demo",
            internalPort=8000,
            composePath="docker/compose.yaml",
            dockerfilePath="docker/Dockerfile",
        ),
        entry=EntryConfig(install="pip install .", start="uvicorn main:app"),
    )


def test_circuit_opens_to_hour_backoff_on_third_failure() -> None:
    manifest = _manifest()
    now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    record_failure(manifest, now=now)
    record_failure(manifest, now=now + timedelta(minutes=2))
    assert is_blocked(manifest, now=now + timedelta(minutes=2, seconds=121)) is False
    record_failure(manifest, now=now + timedelta(minutes=5))
    assert manifest.consecutiveReconcileFailures == 3
    assert is_blocked(manifest, now=now + timedelta(minutes=5, seconds=1)) is True
    assert is_blocked(manifest, now=now + timedelta(hours=2)) is False
    note = status_note(manifest, now=now + timedelta(minutes=5, seconds=1))
    assert note is not None
    assert "熔断" in note


def test_circuit_requires_manual_after_threshold() -> None:
    manifest = _manifest()
    now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    for i in range(MANUAL_THRESHOLD):
        record_failure(manifest, now=now + timedelta(minutes=i))
    assert manifest.reconcileCircuitManual is True
    assert is_blocked(manifest, now=now + timedelta(days=1)) is True
    note = status_note(manifest, now=now + timedelta(days=1))
    assert note is not None
    assert "人工" in note
    assert "lwa start" in note
    clear_circuit(manifest)
    assert is_blocked(manifest, now=now + timedelta(days=1)) is False
    assert manifest.consecutiveReconcileFailures == 0
    assert manifest.reconcileCircuitManual is False
