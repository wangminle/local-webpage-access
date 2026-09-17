"""Agent 持久操作受理与 worker 执行测试（AGC-W10，M1）。

验收场景（设计 §9.2 W10）：
1. 重复幂等键去重：同键重试返回同一 operationId，不产生第二行；
2. 同键不同载荷 → idempotency_conflict；
3. 队列上限 → busy + retryAfterMs；
4. worker 异步执行：受理立即返回 queued，执行不阻塞受理方；
5. plan 归属 / 过期 / revision 预检。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from local_webpage_access.agent.auth import M1_LOCAL_OWNER_SCOPES, Principal
from local_webpage_access.agent.contracts import (
    ApplyDeploymentInput,
    LifecycleInput,
    OperationAction,
    PlanDeploymentInput,
    ServerDirectorySource,
)
from local_webpage_access.agent.operations import AgentOperationService, AgentWorker
from local_webpage_access.agent.service import AgentService, AgentServiceError
from local_webpage_access.config import AgentConfig, Config, PortPool
from local_webpage_access.paths import Workspace
from tests._helpers import make_static_manifest
from tests.conftest import stop_workspace_test_builtins


def _principal(pid: str = "local-owner") -> Principal:
    return Principal(pid, "local_owner", M1_LOCAL_OWNER_SCOPES)


def _config(allowed: Path, **kwargs) -> Config:
    return Config(
        staticGateway="builtin",
        portPool=PortPool(start=21000, end=21050),
        agent=AgentConfig(allowedSourceRoots=[allowed], **kwargs),
    )


@pytest.fixture(autouse=True)
def _stop_agent_operation_builtins(workspace: Workspace) -> None:
    """worker 真实 start 的 builtin http.server 必须在用例结束时停掉。"""
    yield
    stop_workspace_test_builtins(workspace)


def _service(workspace: Workspace, config: Config, registry, *, now=None) -> AgentService:
    return AgentService(workspace, config, registry, _principal(), now=now)


def _ops(workspace: Workspace, config: Config, registry, *, now=None) -> AgentOperationService:
    return AgentOperationService(workspace, config, registry, _principal(), now=now)


def _static_src(root: Path) -> Path:
    src = root / "site"
    src.mkdir(parents=True)
    (src / "index.html").write_text("<h1>ops</h1>\n", encoding="utf-8")
    return src


def _plan(workspace: Workspace, config: Config, registry, src: Path, **kwargs):
    svc = _service(workspace, config, registry, now=kwargs.pop("now", None))
    return svc.plan_deployment(
        PlanDeploymentInput(
            source=ServerDirectorySource(type="server_directory", path=str(src)),
            intent="create",
            **kwargs,
        )
    )


def _run_worker_once(worker: AgentWorker):
    record = worker.claim_next()
    assert record is not None, "应有可认领的 queued 操作"
    worker._execute(record)
    return record


# ---- 幂等受理 -----------------------------------------------------------------


def test_apply_idempotent_replay_returns_same_operation(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    src = _static_src(tmp_path)
    plan = _plan(workspace, config, registry, src)
    ops = _ops(workspace, config, registry)

    first = ops.apply_deployment(ApplyDeploymentInput(planId=plan.planId, idempotencyKey="k-1"))
    second = ops.apply_deployment(ApplyDeploymentInput(planId=plan.planId, idempotencyKey="k-1"))

    assert first.operationId == second.operationId
    assert first.status.value == "queued"
    rows = registry.list_agent_operations()
    assert len(rows) == 1


def test_apply_same_key_different_plan_conflicts(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    src = _static_src(tmp_path)
    plan_a = _plan(workspace, config, registry, src, displayName="site-a")
    plan_b = _plan(workspace, config, registry, src, displayName="site-b")
    ops = _ops(workspace, config, registry)

    ops.apply_deployment(ApplyDeploymentInput(planId=plan_a.planId, idempotencyKey="k-1"))
    with pytest.raises(AgentServiceError) as exc:
        ops.apply_deployment(ApplyDeploymentInput(planId=plan_b.planId, idempotencyKey="k-1"))
    assert exc.value.code == "idempotency_conflict"


def test_apply_rejects_expired_plan(workspace: Workspace, registry, tmp_path: Path) -> None:
    created = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    config = _config(tmp_path)
    src = _static_src(tmp_path)
    plan = _plan(workspace, config, registry, src, now=lambda: created)
    stale_ops = _ops(
        workspace, config, registry, now=lambda: created + timedelta(seconds=1801)
    )
    with pytest.raises(AgentServiceError) as exc:
        stale_ops.apply_deployment(
            ApplyDeploymentInput(planId=plan.planId, idempotencyKey="k-1")
        )
    assert exc.value.code == "needs_input"


def test_apply_rejects_plan_of_other_principal(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    src = _static_src(tmp_path)
    plan = _plan(workspace, config, registry, src)
    other = AgentOperationService(
        workspace, config, registry, Principal("agent-b", "local_owner", M1_LOCAL_OWNER_SCOPES)
    )
    with pytest.raises(AgentServiceError) as exc:
        other.apply_deployment(ApplyDeploymentInput(planId=plan.planId, idempotencyKey="k-1"))
    assert exc.value.code == "permission_denied"


def test_queue_limit_returns_busy(workspace: Workspace, registry, tmp_path: Path) -> None:
    config = _config(tmp_path, maxPendingOperations=1)
    src = _static_src(tmp_path)
    ops = _ops(workspace, config, registry)
    plan_a = _plan(workspace, config, registry, src, displayName="site-a")
    ops.apply_deployment(ApplyDeploymentInput(planId=plan_a.planId, idempotencyKey="k-1"))

    plan_b = _plan(workspace, config, registry, src, displayName="site-b")
    with pytest.raises(AgentServiceError) as exc:
        ops.apply_deployment(ApplyDeploymentInput(planId=plan_b.planId, idempotencyKey="k-2"))
    assert exc.value.code == "busy"
    assert exc.value.context.get("retryAfterMs")


def test_cancelling_counts_toward_queue_limit(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    """CHK-345 P3：cancelling 计入 maxPendingOperations（与 queued/running 一样占名额）。"""
    from local_webpage_access.agent.operations import _iso

    config = _config(tmp_path, maxPendingOperations=1)
    src = _static_src(tmp_path)
    now = _iso(datetime.now(timezone.utc))
    registry.create_agent_operation(
        {
            "operation_id": "op_cancelling",
            "principal_id": "local-owner",
            "workspace_id": registry.get_or_create_workspace_id(),
            "action": "deploy",
            "target_instance_id": None,
            "request_hash": "ab" * 32,
            "idempotency_key": "cancelling-slot",
            "plan_id": None,
            "status": "cancelling",
            "phase": "build",
            "created_at": now,
            "updated_at": now,
        }
    )
    ops = _ops(workspace, config, registry)
    plan = _plan(workspace, config, registry, src, displayName="site-after-cancel")
    with pytest.raises(AgentServiceError) as exc:
        ops.apply_deployment(ApplyDeploymentInput(planId=plan.planId, idempotencyKey="k-new"))
    assert exc.value.code == "busy"


# ---- lifecycle 受理预检 ----------------------------------------------------------


def _seed_instance(workspace: Workspace, registry, instance_id: str = "demo") -> None:
    workspace.ensure_app_dirs(instance_id)
    manifest = make_static_manifest(instance_id)
    manifest.save(workspace.app_manifest_path(instance_id))
    registry.upsert_from_manifest(manifest)


def test_lifecycle_revision_preflight(workspace: Workspace, registry, tmp_path: Path) -> None:
    config = _config(tmp_path)
    _seed_instance(workspace, registry)
    ops = _ops(workspace, config, registry)

    with pytest.raises(AgentServiceError) as missing:
        ops.submit_lifecycle(
            OperationAction.start,
            LifecycleInput(instanceId="nope", expectedRevision=1, idempotencyKey="k-1"),
        )
    assert missing.value.code == "needs_input"

    with pytest.raises(AgentServiceError) as conflict:
        ops.submit_lifecycle(
            OperationAction.start,
            LifecycleInput(instanceId="demo", expectedRevision=9, idempotencyKey="k-2"),
        )
    assert conflict.value.code == "revision_conflict"

    accepted = ops.submit_lifecycle(
        OperationAction.stop,
        LifecycleInput(instanceId="demo", expectedRevision=1, idempotencyKey="k-3"),
    )
    assert accepted.status.value == "queued"
    assert accepted.instanceId == "demo"


# ---- worker 执行 -----------------------------------------------------------------


def test_acceptance_does_not_execute(workspace: Workspace, registry, tmp_path: Path) -> None:
    """受理立即返回 queued；无 worker 时任务不推进（worker 不阻塞受理方）。"""
    config = _config(tmp_path)
    src = _static_src(tmp_path)
    plan = _plan(workspace, config, registry, src, displayName="site-x")
    ops = _ops(workspace, config, registry)
    accepted = ops.apply_deployment(
        ApplyDeploymentInput(planId=plan.planId, idempotencyKey="k-1")
    )
    view = ops.get_operation(accepted.operationId)
    assert view.status.value == "queued"
    assert registry.list_instances() == []


def test_worker_executes_deploy_create_static(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    """端到端：plan→apply→worker 执行→succeeded，含 instanceId/revision/access。"""
    config = _config(tmp_path)
    src = _static_src(tmp_path)
    plan = _plan(workspace, config, registry, src, displayName="site-y")
    ops = _ops(workspace, config, registry)
    accepted = ops.apply_deployment(
        ApplyDeploymentInput(planId=plan.planId, idempotencyKey="k-1")
    )

    worker = AgentWorker(workspace, config, registry)
    _run_worker_once(worker)

    view = ops.get_operation(accepted.operationId)
    assert view.status.value == "succeeded", view.error
    assert view.instanceId == "site-y"
    result = view.result or {}
    assert result.get("revision") == 1
    assert result.get("access"), "结果应带访问地址"
    assert registry.get_instance("site-y") is not None


def test_worker_executes_lifecycle_stop(workspace: Workspace, registry, tmp_path: Path) -> None:
    config = _config(tmp_path)
    _seed_instance(workspace, registry)
    ops = _ops(workspace, config, registry)
    accepted = ops.submit_lifecycle(
        OperationAction.stop,
        LifecycleInput(instanceId="demo", expectedRevision=1, idempotencyKey="k-1"),
    )

    worker = AgentWorker(workspace, config, registry)
    _run_worker_once(worker)

    view = ops.get_operation(accepted.operationId)
    assert view.status.value == "succeeded", view.error
    row = registry.get_instance("demo")
    assert row is not None and row["desired_state"] == "stopped"


def test_worker_skips_revision_mismatch_at_execution(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    """受理后实例 revision 被其他通道递增 → 执行期 revision_conflict 失败。"""
    config = _config(tmp_path)
    _seed_instance(workspace, registry)
    ops = _ops(workspace, config, registry)
    accepted = ops.submit_lifecycle(
        OperationAction.stop,
        LifecycleInput(instanceId="demo", expectedRevision=1, idempotencyKey="k-1"),
    )
    registry.cas_increment_revision("demo", 1)

    worker = AgentWorker(workspace, config, registry)
    _run_worker_once(worker)

    view = ops.get_operation(accepted.operationId)
    assert view.status.value == "failed"
    assert view.error is not None and view.error.code.value == "revision_conflict"


def test_cancel_queued_operation(workspace: Workspace, registry, tmp_path: Path) -> None:
    config = _config(tmp_path)
    src = _static_src(tmp_path)
    plan = _plan(workspace, config, registry, src, displayName="site-z")
    ops = _ops(workspace, config, registry)
    accepted = ops.apply_deployment(
        ApplyDeploymentInput(planId=plan.planId, idempotencyKey="k-1")
    )

    result = ops.cancel_operation(accepted.operationId)
    assert result.status.value == "cancelled"

    worker = AgentWorker(workspace, config, registry)
    assert worker.claim_next() is None, "已取消操作不得再被认领"
    assert registry.list_instances() == []


# ---- BUG-688：queued 取消竞态 ----------------------------------------------------------


def test_cancel_queued_race_with_claim_reports_running(
    workspace: Workspace, registry, tmp_path: Path, monkeypatch
) -> None:
    """BUG-688：读到 queued 与 CAS 之间被 worker 认领 → 如实返回 running。

    修复前 CAS 返回值被丢弃、一律报 cancelled，库里却仍是 running，
    任务继续执行——违反「不假报已取消」。
    """
    config = _config(tmp_path)
    src = _static_src(tmp_path)
    plan = _plan(workspace, config, registry, src, displayName="site-race")
    ops = _ops(workspace, config, registry)
    accepted = ops.apply_deployment(
        ApplyDeploymentInput(planId=plan.planId, idempotencyKey="k-1")
    )
    worker = AgentWorker(workspace, config, registry)

    original_cas = registry.cas_agent_operation_status

    def claim_then_cas(*args, **kwargs):
        # 模拟竞态：cancel 已读到 queued，worker 在 CAS 前抢先认领
        assert worker.claim_next() is not None, "竞态窗口内应能认领"
        return original_cas(*args, **kwargs)

    monkeypatch.setattr(registry, "cas_agent_operation_status", claim_then_cas)

    result = ops.cancel_operation(accepted.operationId)
    assert result.status.value == "running", "CAS 未命中时不得假报 cancelled"
    row = registry.get_agent_operation(accepted.operationId)
    assert row is not None and row["status"] == "running"


# ---- BUG-689：队列满时的同键幂等重放 ---------------------------------------------------


def test_busy_queue_still_replays_same_idempotency_key(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    """BUG-689：maxPendingOperations=1 且队列已满时，同键重试返回原 operationId。

    修复前容量判定抢在幂等查找之前，同键重试得到 busy 而非重放。
    """
    config = _config(tmp_path, maxPendingOperations=1)
    src = _static_src(tmp_path)
    plan = _plan(workspace, config, registry, src, displayName="site-busy")
    ops = _ops(workspace, config, registry)

    first = ops.apply_deployment(
        ApplyDeploymentInput(planId=plan.planId, idempotencyKey="same-key")
    )
    second = ops.apply_deployment(
        ApplyDeploymentInput(planId=plan.planId, idempotencyKey="same-key")
    )
    assert second.operationId == first.operationId
    assert second.status.value == first.status.value == "queued"
    # 不同键仍应 busy（上限语义不变）
    plan_b = _plan(workspace, config, registry, src, displayName="site-busy-b")
    with pytest.raises(AgentServiceError) as exc:
        ops.apply_deployment(
            ApplyDeploymentInput(planId=plan_b.planId, idempotencyKey="other-key")
        )
    assert exc.value.code == "busy"


# ---- BUG-690：源身份写回与 git 目标拒绝 ------------------------------------------------


def test_plan_rejects_update_of_git_source_instance(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    """BUG-690：git 源实例的更新在计划期拒绝（update_zip 对 git 硬拒）。"""
    from local_webpage_access.models import InstanceManifest

    config = _config(tmp_path)
    src = _static_src(tmp_path)
    _seed_instance(workspace, registry, "git-site")
    manifest = InstanceManifest.load(workspace.app_manifest_path("git-site"))
    manifest.sourceKind = "git"
    manifest.sourceGitUrl = "https://github.com/example/repo"
    manifest.touch()
    manifest.save(workspace.app_manifest_path("git-site"))

    with pytest.raises(AgentServiceError) as exc:
        _service(workspace, config, registry).plan_deployment(
            PlanDeploymentInput(
                source=ServerDirectorySource(type="server_directory", path=str(src)),
                intent="update",
                targetInstanceId="git-site",
                expectedRevision=1,
            )
        )
    assert exc.value.code == "needs_input"
    assert "GitHub" in str(exc)


def test_worker_rejects_update_when_target_flipped_to_git(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    """BUG-690 双保险：计划受理后目标被切成 git 源，worker 执行期兜底拒绝。"""
    from local_webpage_access.models import InstanceManifest

    config = _config(tmp_path)
    src = _static_src(tmp_path)
    plan = _plan(workspace, config, registry, src, displayName="site-flip")
    ops = _ops(workspace, config, registry)
    ops.apply_deployment(ApplyDeploymentInput(planId=plan.planId, idempotencyKey="k-1"))
    worker = AgentWorker(workspace, config, registry)
    _run_worker_once(worker)
    assert registry.get_instance("site-flip") is not None

    update_plan = _service(workspace, config, registry).plan_deployment(
        PlanDeploymentInput(
            source=ServerDirectorySource(type="server_directory", path=str(src)),
            intent="update",
            targetInstanceId="site-flip",
            expectedRevision=1,
        )
    )
    accepted = ops.apply_deployment(
        ApplyDeploymentInput(planId=update_plan.planId, idempotencyKey="k-2")
    )
    # 模拟竞态：受理后、执行前目标实例被切为 git 源
    manifest = InstanceManifest.load(workspace.app_manifest_path("site-flip"))
    manifest.sourceKind = "git"
    manifest.sourceGitUrl = "https://github.com/example/repo"
    manifest.touch()
    manifest.save(workspace.app_manifest_path("site-flip"))

    _run_worker_once(worker)
    view = ops.get_operation(accepted.operationId)
    assert view.status.value == "needs_input", view.result
    assert view.error is not None and "GitHub" in view.error.message
    # CHK-345 P3：操作 error.message 用 LwaError.message，不带 [ZIP_IMPORT_ERROR]
    assert not view.error.message.startswith("["), view.error.message
    assert "ZIP_IMPORT_ERROR" not in view.error.message


def test_worker_deploy_folder_source_writes_back_folder_identity(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    """BUG-690：folder 源 create 部署后写回 folder 身份，不再是 zip。"""
    from local_webpage_access.models import InstanceManifest

    config = _config(tmp_path)
    src = _static_src(tmp_path)
    plan = _plan(workspace, config, registry, src, displayName="site-folder")
    ops = _ops(workspace, config, registry)
    accepted = ops.apply_deployment(
        ApplyDeploymentInput(planId=plan.planId, idempotencyKey="k-1")
    )
    worker = AgentWorker(workspace, config, registry)
    _run_worker_once(worker)
    assert ops.get_operation(accepted.operationId).status.value == "succeeded"

    from pathlib import Path as _P

    manifest = InstanceManifest.load(workspace.app_manifest_path("site-folder"))
    assert manifest.sourceKind == "folder"
    assert _P(str(manifest.sourceDirPath)).resolve() == src.resolve()
    assert manifest.sourceSyncHash, "folder 身份须带快照指纹（update-from-dir 短路依据）"


def test_worker_deploy_git_source_writes_back_git_identity(
    workspace: Workspace, registry, tmp_path: Path, monkeypatch
) -> None:
    """BUG-690：git 源 create 部署后写回 §17.2.1 git 身份（url/ref/refKind/commit）。"""
    import shutil as _shutil

    from local_webpage_access.agent.contracts import GitSource
    from local_webpage_access.agent.service import AgentService
    from local_webpage_access.models import InstanceManifest

    config = _config(tmp_path)
    src = _static_src(tmp_path)
    commit = "deadbeef" * 5

    def fake_snapshot_git(svc, source, dest):
        _shutil.copytree(src, dest)
        return f"{commit}:{'ab' * 16}", GitSource(
            type="git",
            url=source.url,
            ref="v1",
            subdir=None,
            commit=commit,
            refKind="tag",
        )

    monkeypatch.setattr(AgentService, "_snapshot_git", fake_snapshot_git)
    plan = _service(workspace, config, registry).plan_deployment(
        PlanDeploymentInput(
            source=GitSource(type="git", url="https://github.com/example/repo"),
            intent="create",
            displayName="site-git",
        )
    )
    ops = _ops(workspace, config, registry)
    accepted = ops.apply_deployment(
        ApplyDeploymentInput(planId=plan.planId, idempotencyKey="k-1")
    )
    worker = AgentWorker(workspace, config, registry)
    _run_worker_once(worker)
    assert ops.get_operation(accepted.operationId).status.value == "succeeded"

    manifest = InstanceManifest.load(workspace.app_manifest_path("site-git"))
    assert manifest.sourceKind == "git"
    assert manifest.sourceGitUrl == "https://github.com/example/repo"
    assert manifest.sourceGitRef == "v1"
    assert manifest.sourceGitRefKind == "tag"
    assert manifest.sourceGitCommit == commit
    assert manifest.sourceDirPath is None


# ---- BUG-695：get_logs 按类别定向读取 --------------------------------------------------


def _seed_logs(workspace: Workspace, registry, instance_id: str = "site-logs") -> None:
    import os as _os

    workspace.ensure_app_dirs(instance_id)
    manifest = make_static_manifest(instance_id)
    manifest.save(workspace.app_manifest_path(instance_id))
    registry.upsert_from_manifest(manifest)
    log_dir = workspace.app_logs(instance_id)
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "build.log").write_text("build-line-1\nbuild-line-2\n", encoding="utf-8")
    _os.utime(log_dir / "build.log", (_os.stat(log_dir / "build.log").st_atime - 100, _os.stat(log_dir / "build.log").st_mtime - 100))
    (log_dir / "run.log").write_text("run-line-1\n", encoding="utf-8")


def test_get_logs_category_build_reads_older_build_log(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    """BUG-695：category=build 定向读取，不再被 mtime 更新的 run 日志挡住。"""
    from local_webpage_access.agent.contracts import GetLogsInput

    config = _config(tmp_path)
    _seed_logs(workspace, registry)
    page = _service(workspace, config, registry).get_logs(
        GetLogsInput(instanceId="site-logs", category="build", limit=50)
    )
    assert "build-line-1" in page.lines and "build-line-2" in page.lines
    assert "run-line-1" not in page.lines


def test_get_logs_default_keeps_latest_behavior(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    """缺省 category 沿用「读 mtime 最新」的既有行为。"""
    from local_webpage_access.agent.contracts import GetLogsInput

    config = _config(tmp_path)
    _seed_logs(workspace, registry)
    page = _service(workspace, config, registry).get_logs(
        GetLogsInput(instanceId="site-logs", limit=50)
    )
    assert "run-line-1" in page.lines
    assert "build-line-1" not in page.lines


def test_get_logs_unknown_category_rejected(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    """未知类别 → needs_input（列出可选值），不是 5xx。"""
    from local_webpage_access.agent.contracts import GetLogsInput

    config = _config(tmp_path)
    _seed_logs(workspace, registry)
    with pytest.raises(AgentServiceError) as exc:
        _service(workspace, config, registry).get_logs(
            GetLogsInput(instanceId="site-logs", category="bogus", limit=50)
        )
    assert exc.value.code == "needs_input"


def test_get_logs_category_without_file_returns_empty(
    workspace: Workspace, registry, tmp_path: Path
) -> None:
    """类别合法但该实例没有对应日志文件 → 空页，不是错误。"""
    from local_webpage_access.agent.contracts import GetLogsInput

    config = _config(tmp_path)
    _seed_logs(workspace, registry)
    page = _service(workspace, config, registry).get_logs(
        GetLogsInput(instanceId="site-logs", category="scan", limit=50)
    )
    assert page.lines == [] and page.nextCursor is None
