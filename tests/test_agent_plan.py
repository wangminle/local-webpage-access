"""Agent 部署计划测试（AGC-W08，M1）。

验收场景（设计 §9.2 W08）：
1. plan 不启动实例、不执行用户 build；
2. 源快照：计划后改源不影响已存 digest / 快照内容；
3. TTL：expiresAt = createdAt + 30 分钟；过期后不可再用于 apply 预检；
4. 能力缺口：容器源在 Docker 不可用时写入 capabilityGaps。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from local_webpage_access.agent.auth import M1_LOCAL_OWNER_SCOPES, Principal
from local_webpage_access.agent.contracts import (
    PLAN_TTL_SECONDS,
    ArtifactSource,
    GitSource,
    PlanDeploymentInput,
    ServerDirectorySource,
)
from local_webpage_access.capability import CapabilityReport, write_capability_cache
from local_webpage_access.config import AgentConfig, Config
from local_webpage_access.git_source import CloneResult
from local_webpage_access.models import Status
from local_webpage_access.paths import Workspace
from tests._helpers import make_static_manifest


def _principal() -> Principal:
    return Principal("local-owner", "local_owner", M1_LOCAL_OWNER_SCOPES)


def _service(workspace: Workspace, registry, *, allowed: Path, now=None):
    from local_webpage_access.agent.service import AgentService

    config = Config(agent=AgentConfig(allowedSourceRoots=[allowed]))
    return AgentService(workspace, config, registry, _principal(), now=now)


def _static_src(root: Path) -> Path:
    src = root / "site"
    src.mkdir(parents=True)
    (src / "index.html").write_text("<h1>hello</h1>\n", encoding="utf-8")
    return src


def _flask_src(root: Path) -> Path:
    src = root / "api"
    src.mkdir(parents=True)
    (src / "requirements.txt").write_text("flask==3.0.0\n", encoding="utf-8")
    (src / "app.py").write_text(
        "from flask import Flask\napp = Flask(__name__)\n",
        encoding="utf-8",
    )
    return src


# ---- 1. 不启动 / 不 build ------------------------------------------------------


def test_plan_does_not_start_or_build(
    workspace: Workspace, registry, tmp_path: Path, monkeypatch
) -> None:
    src = _static_src(tmp_path)
    calls: list[str] = []

    def _track(name: str):
        def _inner(*_a, **_k):
            calls.append(name)
            raise AssertionError(f"plan 不得调用 {name}")

        return _inner

    monkeypatch.setattr("local_webpage_access.lifecycle.start_instance", _track("start"))
    monkeypatch.setattr("local_webpage_access.hosting.host_instance", _track("host"))
    monkeypatch.setattr("local_webpage_access.hosting.host_container", _track("container"))
    monkeypatch.setattr(
        "local_webpage_access.importer.Importer.import_from_dir", _track("import_dir")
    )

    svc = _service(workspace, registry, allowed=tmp_path)
    plan = svc.plan_deployment(
        PlanDeploymentInput(
            source=ServerDirectorySource(type="server_directory", path=str(src)),
            intent="create",
            displayName="demo-site",
        )
    )
    assert plan.intent == "create"
    assert plan.sourceDigest
    assert calls == []
    assert registry.list_instances() == []
    snap = svc.plan_snapshot_path(plan.planId)
    assert snap.is_dir()
    assert (snap / "index.html").read_text(encoding="utf-8") == "<h1>hello</h1>\n"


def test_plan_snapshot_survives_source_change(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    src = _static_src(tmp_path)
    svc = _service(workspace, registry, allowed=tmp_path)
    plan = svc.plan_deployment(
        PlanDeploymentInput(
            source=ServerDirectorySource(type="server_directory", path=str(src)),
            intent="create",
        )
    )
    digest = plan.sourceDigest
    (src / "index.html").write_text("<h1>changed</h1>\n", encoding="utf-8")
    stored = registry.get_agent_plan(plan.planId)
    assert stored is not None
    assert stored["source_digest"] == digest
    snap = svc.plan_snapshot_path(plan.planId)
    assert (snap / "index.html").read_text(encoding="utf-8") == "<h1>hello</h1>\n"


def test_plan_ttl_and_expiry_check(workspace: Workspace, registry, tmp_path: Path) -> None:
    from local_webpage_access.agent.service import AgentServiceError

    src = _static_src(tmp_path)
    created = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    svc = _service(workspace, registry, allowed=tmp_path, now=lambda: created)
    plan = svc.plan_deployment(
        PlanDeploymentInput(
            source=ServerDirectorySource(type="server_directory", path=str(src)),
            intent="create",
        )
    )
    assert plan.createdAt == "2026-09-15T12:00:00Z"
    assert plan.expiresAt == "2026-09-15T12:30:00Z"
    delta = datetime.fromisoformat(plan.expiresAt.replace("Z", "+00:00")) - datetime.fromisoformat(
        plan.createdAt.replace("Z", "+00:00")
    )
    assert delta == timedelta(seconds=PLAN_TTL_SECONDS)
    svc.assert_plan_fresh(plan)

    expired_svc = _service(
        workspace, registry, allowed=tmp_path, now=lambda: created + timedelta(seconds=1801)
    )
    with pytest.raises(AgentServiceError) as exc:
        expired_svc.assert_plan_fresh(plan)
    assert exc.value.code == "needs_input"


def test_plan_records_docker_capability_gap(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    src = _flask_src(tmp_path)
    write_capability_cache(
        workspace.root,
        "manager",
        CapabilityReport(
            overall="unready",
            docker_engine="unavailable",
            docker_compose="unavailable",
            checked_at="2026-09-15T12:00:00Z",
        ),
    )
    svc = _service(workspace, registry, allowed=tmp_path)
    plan = svc.plan_deployment(
        PlanDeploymentInput(
            source=ServerDirectorySource(type="server_directory", path=str(src)),
            intent="create",
        )
    )
    assert "docker" in plan.requiredCapabilities
    assert "docker" in plan.risks.capabilityGaps


def test_plan_rejects_artifact_and_out_of_root(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    from local_webpage_access.agent.service import AgentServiceError

    svc = _service(workspace, registry, allowed=tmp_path)
    with pytest.raises(AgentServiceError) as artifact:
        svc.plan_deployment(
            PlanDeploymentInput(
                source=ArtifactSource(type="artifact", artifactId="art_1"),
                intent="create",
            )
        )
    assert artifact.value.code == "source_not_allowed"

    outside = Path("/tmp/lwa-not-allowed-source")
    with pytest.raises(AgentServiceError) as denied:
        svc.plan_deployment(
            PlanDeploymentInput(
                source=ServerDirectorySource(type="server_directory", path=str(outside)),
                intent="create",
            )
        )
    assert denied.value.code == "source_not_allowed"


def test_plan_git_uses_snapshot_without_network_by_injection(
    workspace: Workspace, registry, tmp_path: Path, monkeypatch
) -> None:
    staged = tmp_path / "clone"
    staged.mkdir()
    (staged / "index.html").write_text("from-git\n", encoding="utf-8")

    @contextmanager
    def fake_clone(*_a, **_k):
        yield CloneResult(
            commit="a" * 40,
            ref="main",
            ref_kind="branch",
            directory=staged,
        )

    monkeypatch.setattr("local_webpage_access.git_source.stage_git_clone", fake_clone)
    svc = _service(workspace, registry, allowed=tmp_path)
    plan = svc.plan_deployment(
        PlanDeploymentInput(
            source=GitSource(type="git", url="https://github.com/octocat/Hello-World.git"),
            intent="create",
        )
    )
    assert plan.sourceDigest.startswith("a" * 40)
    assert (svc.plan_snapshot_path(plan.planId) / "index.html").read_text(
        encoding="utf-8"
    ) == "from-git\n"


def test_plan_git_valid_subdir_accepted(
    workspace: Workspace, registry, tmp_path: Path, monkeypatch
) -> None:
    """BUG-674：macOS tempfile 根含 /var 符号链接，未 resolve 的包含检查会误杀合法 subdir。"""
    staged = tmp_path / "clone"
    (staged / "app").mkdir(parents=True)
    (staged / "app" / "index.html").write_text("from-subdir\n", encoding="utf-8")
    (staged / "root-only.txt").write_text("outside-subdir\n", encoding="utf-8")

    @contextmanager
    def fake_clone(*_a, **_k):
        yield CloneResult(
            commit="b" * 40,
            ref="main",
            ref_kind="branch",
            directory=staged,
        )

    monkeypatch.setattr("local_webpage_access.git_source.stage_git_clone", fake_clone)
    svc = _service(workspace, registry, allowed=tmp_path)
    plan = svc.plan_deployment(
        PlanDeploymentInput(
            source=GitSource(
                type="git",
                url="https://github.com/octocat/Hello-World.git",
                subdir="app",
            ),
            intent="create",
        )
    )
    snap = svc.plan_snapshot_path(plan.planId)
    assert (snap / "index.html").read_text(encoding="utf-8") == "from-subdir\n"
    assert not (snap / "root-only.txt").exists()
    assert plan.sourceDigest.startswith("b" * 40)


def test_plan_git_subdir_escape_rejected(
    workspace: Workspace, registry, tmp_path: Path, monkeypatch
) -> None:
    from local_webpage_access.agent.service import AgentServiceError

    staged = tmp_path / "clone"
    staged.mkdir()

    @contextmanager
    def fake_clone(*_a, **_k):
        yield CloneResult(
            commit="c" * 40,
            ref="main",
            ref_kind="branch",
            directory=staged,
        )

    monkeypatch.setattr("local_webpage_access.git_source.stage_git_clone", fake_clone)
    svc = _service(workspace, registry, allowed=tmp_path)
    for subdir in ("../outside", "/etc", ".."):
        with pytest.raises(AgentServiceError) as exc:
            svc.plan_deployment(
                PlanDeploymentInput(
                    source=GitSource(
                        type="git",
                        url="https://github.com/octocat/Hello-World.git",
                        subdir=subdir,
                    ),
                    intent="create",
                )
            )
        assert exc.value.code == "source_not_allowed", subdir


def test_plan_update_requires_existing_instance(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    from local_webpage_access.agent.service import AgentServiceError

    src = _static_src(tmp_path)
    svc = _service(workspace, registry, allowed=tmp_path)
    with pytest.raises(AgentServiceError) as missing:
        svc.plan_deployment(
            PlanDeploymentInput(
                source=ServerDirectorySource(type="server_directory", path=str(src)),
                intent="update",
                targetInstanceId="nope",
                expectedRevision=1,
            )
        )
    assert missing.value.code == "needs_input"

    workspace.app_dir("demo").mkdir(parents=True)
    manifest = make_static_manifest("demo", status=Status.STOPPED)
    manifest.save(workspace.app_manifest_path("demo"))
    registry.upsert_from_manifest(manifest)
    with pytest.raises(AgentServiceError) as conflict:
        svc.plan_deployment(
            PlanDeploymentInput(
                source=ServerDirectorySource(type="server_directory", path=str(src)),
                intent="update",
                targetInstanceId="demo",
                expectedRevision=9,
            )
        )
    assert conflict.value.code == "revision_conflict"
    plan = svc.plan_deployment(
        PlanDeploymentInput(
            source=ServerDirectorySource(type="server_directory", path=str(src)),
            intent="update",
            targetInstanceId="demo",
            expectedRevision=1,
        )
    )
    assert plan.risks.possibleDowntime is True
    assert plan.expectedRevision == 1
