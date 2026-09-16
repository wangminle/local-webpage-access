"""Agent 查询与能力映射测试（AGC-W07，M1）。

验收场景（设计 §9.2 W07）：
1. 能力缓存命中：不调用昂贵全量探测，observedAt 取缓存 checkedAt；
2. 无缓存：runtime 为 unknown，仍不触发全量探测；
3. list 分页：cursor 不透明、limit 生效、nextCursor 在末页为空；
4. 访问 URL：带 observedAt，clientReachability 默认 unknown，不发探活请求。
"""

from __future__ import annotations

import pytest

from local_webpage_access.agent.auth import M1_LOCAL_OWNER_SCOPES, Principal
from local_webpage_access.agent.contracts import (
    AGENT_CONTRACT_VERSION,
    GetAccessUrlsInput,
    ListInstancesInput,
)
from local_webpage_access.capability import CapabilityReport, write_capability_cache
from local_webpage_access.config import Config
from local_webpage_access.models import NetworkConfig, Status
from local_webpage_access.paths import Workspace
from tests._helpers import make_static_manifest


def _principal() -> Principal:
    return Principal("local-owner", "local_owner", M1_LOCAL_OWNER_SCOPES)


def _service(workspace: Workspace, registry, *, config: Config | None = None):
    from local_webpage_access.agent.service import AgentService

    return AgentService(workspace, config or Config(), registry, _principal())


def _seed_instance(workspace: Workspace, registry, instance_id: str, **overrides) -> None:
    workspace.app_dir(instance_id).mkdir(parents=True, exist_ok=True)
    defaults = dict(
        status=Status.STOPPED,
        network=NetworkConfig(
            hostPort=21001,
            lanUrl="http://192.168.1.8:21001/",
            healthUrl="http://127.0.0.1:21001/",
        ),
    )
    defaults.update(overrides)
    manifest = make_static_manifest(instance_id, **defaults)
    manifest.save(workspace.app_manifest_path(instance_id))
    registry.upsert_from_manifest(manifest)


# ---- 1. 能力缓存 / 未知态，禁止全量探测 ----------------------------------------


def test_get_capabilities_uses_cache_without_probe(
    workspace: Workspace, registry, monkeypatch
) -> None:
    from local_webpage_access import capability as capability_mod

    def _boom(*_a, **_k):
        raise AssertionError("不得调用 collect_capability_report")

    monkeypatch.setattr(capability_mod, "collect_capability_report", _boom)

    checked = "2026-09-15T11:00:00Z"
    write_capability_cache(
        workspace.root,
        "manager",
        CapabilityReport(overall="ready", docker_engine="ready", checked_at=checked),
    )
    result = _service(workspace, registry).get_capabilities()
    assert result.workspaceId
    assert result.contractVersion == AGENT_CONTRACT_VERSION
    assert result.inputTypes == ["server_directory", "git"]
    assert "artifact" not in result.inputTypes
    assert result.runtime.get("overall") == "ready"
    assert result.observedAt == checked


def test_get_capabilities_unknown_without_cache_does_not_probe(
    workspace: Workspace, registry, monkeypatch
) -> None:
    from local_webpage_access import capability as capability_mod

    monkeypatch.setattr(
        capability_mod,
        "collect_capability_report",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("不得全量探测")),
    )
    result = _service(workspace, registry).get_capabilities()
    assert result.runtime.get("overall") == "unknown"
    assert result.observedAt


# ---- 2. 分页 -------------------------------------------------------------------


def test_list_instances_pagination(workspace: Workspace, registry) -> None:
    for iid in ("alpha", "bravo", "charlie"):
        _seed_instance(workspace, registry, iid)
    svc = _service(workspace, registry)
    page1 = svc.list_instances(ListInstancesInput(limit=2))
    assert [item.instanceId for item in page1.instances] == ["alpha", "bravo"]
    assert all(item.revision == 1 for item in page1.instances)
    assert page1.nextCursor
    page2 = svc.list_instances(ListInstancesInput(cursor=page1.nextCursor, limit=2))
    assert [item.instanceId for item in page2.instances] == ["charlie"]
    assert page2.nextCursor is None


def test_get_instance_includes_revision_and_missing_is_needs_input(
    workspace: Workspace, registry
) -> None:
    from local_webpage_access.agent.service import AgentServiceError

    _seed_instance(workspace, registry, "demo")
    svc = _service(workspace, registry)
    detail = svc.get_instance("demo")
    assert detail.instanceId == "demo"
    assert detail.revision == 1
    assert detail.desiredState == "stopped"
    with pytest.raises(AgentServiceError) as exc:
        svc.get_instance("missing")
    assert exc.value.code == "needs_input"


# ---- 3. 访问 URL 观测时间，不探活 ----------------------------------------------


def test_get_access_urls_observed_at_without_live_probe(
    workspace: Workspace, registry, monkeypatch
) -> None:
    import urllib.request

    def _boom(*_a, **_k):
        raise AssertionError("get_access_urls 不得发 HTTP 探活")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    _seed_instance(workspace, registry, "demo")
    result = _service(workspace, registry).get_access_urls(GetAccessUrlsInput(instanceId="demo"))
    assert result.instanceId == "demo"
    audiences = {entry.audience for entry in result.urls}
    assert audiences == {"localhost", "lan"}
    for entry in result.urls:
        assert entry.observedAt
        assert entry.clientReachability == "unknown"
        assert entry.serverProbe in (None, "unknown")
    lan = next(e for e in result.urls if e.audience == "lan")
    assert lan.url == "http://192.168.1.8:21001/"
    local = next(e for e in result.urls if e.audience == "localhost")
    assert "127.0.0.1:21001" in local.url
