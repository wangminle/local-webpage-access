"""实例 revision CAS 测试（AGC-W09，M1）。

验收场景（设计 §9.2 W09）：
1. CLI 与 manager 同时以同一 expectedRevision 提交内容变更，只成功一个；
2. 观测刷新（observe_status / update_status）不递增 revision；
3. rebuild / import --update 等配置/内容通道会递增。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from local_webpage_access.errors import RegistryError
from local_webpage_access.lifecycle import instance_lock, observe_status
from local_webpage_access.models import Status
from local_webpage_access.paths import Workspace
from tests._helpers import make_static_manifest


def _seed(workspace: Workspace, registry, instance_id: str = "demo") -> None:
    workspace.app_dir(instance_id).mkdir(parents=True, exist_ok=True)
    manifest = make_static_manifest(instance_id, status=Status.PENDING)
    manifest.save(workspace.app_manifest_path(instance_id))
    registry.upsert_from_manifest(manifest)


def test_fresh_instance_revision_defaults_to_one(workspace: Workspace, registry) -> None:
    _seed(workspace, registry)
    assert registry.get_revision("demo") == 1


def test_cli_and_manager_cas_only_one_succeeds(workspace: Workspace, registry) -> None:
    _seed(workspace, registry)
    expected = registry.get_revision("demo")
    assert expected == 1
    results: list[int | str] = []

    def _mutate() -> None:
        try:
            with instance_lock(workspace, "demo"):
                new_rev = registry.cas_increment_revision("demo", expected)
            results.append(new_rev)
        except RegistryError as exc:
            results.append(exc.code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: _mutate(), range(2)))

    assert results.count(2) == 1
    assert results.count("revision_conflict") == 1
    assert registry.get_revision("demo") == 2


def test_observe_status_does_not_bump_revision(
    workspace: Workspace, registry, config, monkeypatch
) -> None:
    _seed(workspace, registry)
    monkeypatch.setattr(
        "local_webpage_access.lifecycle._observe_static_status",
        lambda *_a, **_k: Status.STOPPED,
    )
    observe_status(workspace, config, registry, "demo")
    assert registry.get_revision("demo") == 1
    registry.update_status("demo", Status.RUNNING.value, last_observed_at="2026-09-15T12:00:00Z")
    assert registry.get_revision("demo") == 1
    manifest = make_static_manifest("demo", status=Status.RUNNING)
    registry.upsert_from_manifest(manifest)
    assert registry.get_revision("demo") == 1


def test_rebuild_bumps_revision(workspace: Workspace, registry, config, monkeypatch) -> None:
    from local_webpage_access.lifecycle import rebuild_instance
    from local_webpage_access.models import InstanceManifest

    _seed(workspace, registry)
    manifest = InstanceManifest.load(workspace.app_manifest_path("demo"))

    monkeypatch.setattr(
        "local_webpage_access.hosting.host_instance",
        lambda *_a, **_k: manifest,
    )

    class _Queue:
        def run(self, _iid, builder):
            return builder(_iid)

    monkeypatch.setattr(
        "local_webpage_access.build_queue.get_build_queue",
        lambda *_a, **_k: _Queue(),
    )
    monkeypatch.setattr(
        "local_webpage_access.path_alias.maybe_verify_alias_after_start",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "local_webpage_access.path_alias.maybe_restore_desired_alias_after_start",
        lambda *_a, **_k: False,
    )
    monkeypatch.setattr(
        "local_webpage_access.lifecycle.check_source_staleness",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "local_webpage_access.lifecycle._sync_alias_port",
        lambda *_a, **_k: None,
    )
    rebuild_instance(workspace, config, registry, "demo")
    assert registry.get_revision("demo") == 2


def test_cas_conflict_raises_agent_code(workspace: Workspace, registry) -> None:
    from local_webpage_access.agent.service import AgentServiceError, claim_content_revision

    _seed(workspace, registry)
    claim_content_revision(registry, "demo", expected=1)
    with pytest.raises(AgentServiceError) as exc:
        claim_content_revision(registry, "demo", expected=1)
    assert exc.value.code == "revision_conflict"
    assert registry.get_revision("demo") == 2
