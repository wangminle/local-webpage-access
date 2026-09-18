"""BUG-704 / CHK-349·350：Agent M1 执行前门禁回归。

- BUG-704（P1，CHK-351 独立复核确认）：Agent 计划的 ``expectedRevision`` 只在
  worker 锁外预检；``update_zip`` 自行重读当前 revision 作 CAS 基准。CLI/daemon
  在预检与锁之间完成更新（r1→r2）时，旧计划会静默覆盖较新内容且不报
  revision_conflict——违反设计 §6.3「实例 mutation 在锁内检查 expectedRevision」。
- CHK-349/350（P2）：Docker 能力缺口只在计划期记录（capabilityGaps），
  ``apply`` 受理与 worker 执行均不复验；缺 Docker 的容器计划可入队并产生
  导入副作用，而非按 §6.4 返回 ``capability_unavailable``。
"""

from __future__ import annotations

import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from local_webpage_access.agent.auth import M1_LOCAL_OWNER_SCOPES, Principal
from local_webpage_access.agent.contracts import (
    ApplyDeploymentInput,
    PlanDeploymentInput,
    ServerDirectorySource,
)
from local_webpage_access.agent.operations import AgentOperationService, AgentWorker
from local_webpage_access.agent.service import AgentService, AgentServiceError
from local_webpage_access.capability import CapabilityReport, write_capability_cache
from local_webpage_access.config import AgentConfig, Config, PortPool
from local_webpage_access.errors import ZipImportError
from local_webpage_access.importer import Importer
from local_webpage_access.paths import Workspace
from tests.conftest import stop_workspace_test_builtins


def _principal() -> Principal:
    return Principal("local-owner", "local_owner", M1_LOCAL_OWNER_SCOPES)


def _config(allowed: Path) -> Config:
    return Config(
        staticGateway="builtin",
        portPool=PortPool(start=21000, end=21050),
        agent=AgentConfig(allowedSourceRoots=[allowed]),
    )


def _service(workspace: Workspace, config: Config, registry) -> AgentService:
    return AgentService(workspace, config, registry, _principal())


def _ops(workspace: Workspace, config: Config, registry) -> AgentOperationService:
    return AgentOperationService(workspace, config, registry, _principal())


def _static_src(root: Path, marker: str) -> Path:
    src = root / f"site-{marker}"
    src.mkdir(parents=True)
    (src / "index.html").write_text(f"<h1>{marker}</h1>\n", encoding="utf-8")
    return src


def _zip_of(root: Path, name: str, marker: str) -> Path:
    path = root / f"{name}.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("index.html", f"<h1>{marker}</h1>\n")
    return path


def _cache_ready(workspace: Workspace) -> None:
    write_capability_cache(
        workspace.root,
        "manager",
        CapabilityReport(
            overall="ready",
            docker_engine="ready",
            docker_compose="ready",
            docker_access="ready",
            checked_at=datetime.now(timezone.utc).isoformat(),
        ),
    )


def _cache_no_docker(workspace: Workspace) -> None:
    write_capability_cache(
        workspace.root,
        "manager",
        CapabilityReport(
            overall="unready",
            docker_engine="unavailable",
            docker_compose="unavailable",
            docker_access="unavailable",
            checked_at=datetime.now(timezone.utc).isoformat(),
        ),
    )


@pytest.fixture(autouse=True)
def _stop_agent_operation_builtins(workspace: Workspace) -> None:
    yield
    stop_workspace_test_builtins(workspace)


# ---- BUG-704：锁内复核 expectedRevision ----------------------------------------


def test_update_zip_rejects_stale_expected_revision_inside_lock(
    workspace: Workspace, registry, tmp_path: Path, monkeypatch
) -> None:
    """BUG-704 核心：锁外预检后 CLI/daemon 的更新不得被旧期望值覆盖。

    用「进入实例锁时递增 revision」模拟竞态窗口内落地的外部更新：
    update_zip(expected_revision=r1) 必须在换入任何内容前拒绝。
    """
    config = _config(tmp_path)
    importer = Importer(workspace, config, registry)
    v1 = _zip_of(tmp_path, "v1", "v1")
    v2 = _zip_of(tmp_path, "v2", "v2-stale-plan")
    iid = importer.import_zip(str(v1)).instance_id
    assert registry.get_revision(iid) == 1

    import local_webpage_access.lifecycle as lifecycle

    real_lock = lifecycle.instance_lock

    @contextmanager
    def _bump_then_lock(ws, instance_id):
        current = registry.get_revision(instance_id)
        if current is not None:
            registry.cas_increment_revision(instance_id, current)
        with real_lock(ws, instance_id):
            yield

    monkeypatch.setattr(lifecycle, "instance_lock", _bump_then_lock)

    with pytest.raises(ZipImportError) as exc:
        importer.update_zip(v2, iid, restart=False, expected_revision=1)
    assert exc.value.code == "revision_conflict"
    # 旧计划内容未覆盖实例，外部更新的 revision（2）保持
    assert (workspace.app_current(iid) / "index.html").read_text(encoding="utf-8") == (
        "<h1>v1</h1>\n"
    )
    assert registry.get_revision(iid) == 2


def test_update_zip_expected_revision_match_proceeds(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    """BUG-704 对照：锁内 revision 与期望一致时正常更新并按期望值 CAS。"""
    config = _config(tmp_path)
    importer = Importer(workspace, config, registry)
    v1 = _zip_of(tmp_path, "b1", "b1")
    v2 = _zip_of(tmp_path, "b2", "b2")
    iid = importer.import_zip(str(v1)).instance_id

    result = importer.update_zip(v2, iid, restart=False, expected_revision=1)
    assert result.skipped is False
    assert (workspace.app_current(iid) / "index.html").read_text(encoding="utf-8") == (
        "<h1>b2</h1>\n"
    )
    assert registry.get_revision(iid) == 2


def test_worker_update_lock_conflict_reports_revision_conflict(
    workspace: Workspace, registry, tmp_path: Path, monkeypatch
) -> None:
    """BUG-704 端到端：锁内冲突以 revision_conflict（非 needs_input）上报。"""
    config = _config(tmp_path)
    importer = Importer(workspace, config, registry)
    v1 = _zip_of(tmp_path, "w1", "w1")
    iid = importer.import_zip(str(v1), name="update-race").instance_id

    src = _static_src(tmp_path, "w2-newer-plan")
    update_plan = _service(workspace, config, registry).plan_deployment(
        PlanDeploymentInput(
            source=ServerDirectorySource(type="server_directory", path=str(src)),
            intent="update",
            targetInstanceId=iid,
            expectedRevision=1,
        )
    )
    ops = _ops(workspace, config, registry)
    accepted = ops.apply_deployment(
        ApplyDeploymentInput(planId=update_plan.planId, idempotencyKey="k-1")
    )

    import local_webpage_access.lifecycle as lifecycle

    real_lock = lifecycle.instance_lock

    @contextmanager
    def _bump_then_lock(ws, instance_id):
        current = registry.get_revision(instance_id)
        if current is not None:
            registry.cas_increment_revision(instance_id, current)
        with real_lock(ws, instance_id):
            yield

    monkeypatch.setattr(lifecycle, "instance_lock", _bump_then_lock)

    worker = AgentWorker(workspace, config, registry)
    record = worker.claim_next()
    assert record is not None
    worker._execute(record)

    view = ops.get_operation(accepted.operationId)
    assert view.status.value == "failed", view.result
    assert view.error is not None and view.error.code.value == "revision_conflict"
    # 实例内容仍是 v1——旧计划未覆盖
    assert (workspace.app_current(iid) / "index.html").read_text(encoding="utf-8") == (
        "<h1>w1</h1>\n"
    )


# ---- CHK-349/350：apply 与 worker 复验能力缺口 ---------------------------------


def _flask_src(root: Path) -> Path:
    src = root / "api"
    src.mkdir(parents=True)
    (src / "requirements.txt").write_text("flask==3.0.0\n", encoding="utf-8")
    (src / "app.py").write_text(
        "from flask import Flask\napp = Flask(__name__)\n",
        encoding="utf-8",
    )
    return src


def test_apply_rejects_plan_with_capability_gap(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    """CHK-349/350：计划期已记录 Docker 缺口 → apply 受理即拒绝（§6.4）。"""
    _cache_no_docker(workspace)
    config = _config(tmp_path)
    src = _flask_src(tmp_path)
    plan = _service(workspace, config, registry).plan_deployment(
        PlanDeploymentInput(
            source=ServerDirectorySource(type="server_directory", path=str(src)),
            intent="create",
        )
    )
    assert "docker" in plan.risks.capabilityGaps, "前置：计划期确实记录了缺口"

    with pytest.raises(AgentServiceError) as exc:
        _ops(workspace, config, registry).apply_deployment(
            ApplyDeploymentInput(planId=plan.planId, idempotencyKey="k-1")
        )
    assert exc.value.code == "capability_unavailable"


def test_worker_aborts_before_import_when_capability_lost(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    """CHK-349/350：apply 与执行之间 Docker 失效 → 副作用前终止，无导入残留。"""
    _cache_ready(workspace)
    config = _config(tmp_path)
    src = _flask_src(tmp_path)
    plan = _service(workspace, config, registry).plan_deployment(
        PlanDeploymentInput(
            source=ServerDirectorySource(type="server_directory", path=str(src)),
            intent="create",
            displayName="cap-lost",
        )
    )
    assert plan.risks.capabilityGaps == [], "前置：计划期 Docker 可用"
    ops = _ops(workspace, config, registry)
    accepted = ops.apply_deployment(
        ApplyDeploymentInput(planId=plan.planId, idempotencyKey="k-1")
    )

    # 模拟 apply 与 worker 执行之间 Docker 掉线
    _cache_no_docker(workspace)

    worker = AgentWorker(workspace, config, registry)
    record = worker.claim_next()
    assert record is not None
    worker._execute(record)

    view = ops.get_operation(accepted.operationId)
    assert view.status.value == "failed", view.result
    assert view.error is not None and view.error.code.value == "capability_unavailable"
    assert registry.list_instances() == [], "缺口必须在导入副作用之前终止"
