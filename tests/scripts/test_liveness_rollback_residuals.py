"""存活探针失败回滚的残留可见性（PLN-052 S1-05 / #53）。

回滚为尽力而为：任一步失败不得中断后续步骤，失败项须作为残留返回、写入事件并进入 lastError。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from local_webpage_access import hosting
from local_webpage_access.models import Status


class _BrokenRuntime:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def is_running(self, instance_id: str) -> bool:
        raise RuntimeError("docker 不可用")


def test_liveness_rollback_returns_and_records_residuals(monkeypatch) -> None:
    monkeypatch.setattr(hosting, "DockerRuntime", _BrokenRuntime)
    monkeypatch.setattr(hosting, "_container_probe_context", lambda *a, **k: {})

    manifest = MagicMock()
    registry = MagicMock()
    residuals = hosting._liveness_failed_rollback(
        MagicMock(), MagicMock(), registry, "demo", manifest, 18000, False, "探针失败"
    )

    assert any("停止容器" in item and "docker 不可用" in item for item in residuals)
    assert manifest.status == Status.FAILED
    assert "回滚残留" in manifest.lastError
    event_types = [c.args[1] for c in registry.add_event.call_args_list]
    assert "rollback_failed" in event_types


def test_liveness_rollback_clean_run_has_no_residuals(monkeypatch) -> None:
    class _IdleRuntime:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def is_running(self, instance_id: str) -> bool:
            return False

    monkeypatch.setattr(hosting, "DockerRuntime", _IdleRuntime)
    monkeypatch.setattr(hosting, "_container_probe_context", lambda *a, **k: {})

    manifest = MagicMock()
    registry = MagicMock()
    residuals = hosting._liveness_failed_rollback(
        MagicMock(), MagicMock(), registry, "demo", manifest, 18000, False, "探针失败"
    )

    assert residuals == []
    event_types = [c.args[1] for c in registry.add_event.call_args_list]
    assert "rollback_failed" not in event_types
