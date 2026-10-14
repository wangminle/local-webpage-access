"""S3-01 有限回滚：rollback_instance 编排、D8 确认、revision CAS、daemon 自愈、CLI。

覆盖契约：
- 回滚到指定/默认上一可用版本：停当前容器 → 恢复快照生成物 → 目标镜像打回 latest
  → up → 验证 → lastOperation(rollback, succeeded) + revision +1 + 别名守卫；
- 回滚后验证失败：补偿恢复原生成物与原镜像并重新 up，抛 HostingError，操作记 failed；
- preStart/buildHooks 含迁移命令时未 --yes 抛 RollbackNeedsYes，零副作用；
- expected_revision 不符抛 revision_conflict；
- daemon 自愈 selfHealPreferLastGood：最近操作失败且容器缺失时优先拉回上一可用版本，
  失败回退 start_instance；
- CLI：lwa versions / lwa rollback 输出与退出码。
"""

from __future__ import annotations

import contextlib
import json
from unittest.mock import MagicMock

import pytest

from local_webpage_access import hosting, path_alias, rebuild_txn, rollback, versions
from local_webpage_access.errors import DockerError, HostingError, LifecycleError
from local_webpage_access.models import DesiredState, InstanceManifest, Status
from tests.scripts._helpers import make_container_manifest

COMPOSE_CURRENT = "services:\n  app:\n    image: current\n"
COMPOSE_OLD = "services:\n  app:\n    image: old\n"


@pytest.fixture()
def deployed(workspace, registry):
    """running 容器实例：当前构建 b2（img-current），历史可用 b1（img-old + 快照）。"""
    workspace.ensure_app_dirs("demo")
    manifest = make_container_manifest(
        "demo", status=Status.RUNNING, desiredState=DesiredState.RUNNING
    )
    manifest.appPath = str(workspace.app_current("demo"))
    manifest.container.containerId = "cur-container"
    manifest.container.imageId = "img-current"
    manifest.container.hostPort = 21001
    manifest.container.composePath = str(workspace.app_compose_path("demo"))
    manifest.container.dockerfilePath = str(workspace.app_dockerfile_path("demo"))
    manifest.save(workspace.app_manifest_path("demo"))
    registry.upsert_from_manifest(manifest)

    docker_dir = workspace.app_docker("demo")
    docker_dir.mkdir(parents=True, exist_ok=True)
    (docker_dir / "compose.yaml").write_text(COMPOSE_CURRENT, encoding="utf-8")
    (docker_dir / "Dockerfile").write_text("FROM x\n", encoding="utf-8")
    (docker_dir / ".env").write_text("A=1\n", encoding="utf-8")

    b1 = registry.add_build("demo", status="running")
    registry.finish_build(b1, status="success")
    snap = workspace.app_docker("demo") / "builds" / str(b1)
    snap.mkdir(parents=True)
    (snap / "compose.yaml").write_text(COMPOSE_OLD, encoding="utf-8")
    (snap / "Dockerfile").write_text("FROM x\n", encoding="utf-8")
    (snap / ".env").write_text("A=1\n", encoding="utf-8")
    registry.record_build_artifacts(
        b1,
        image_ref=f"lwa-demo-app:b{b1}",
        image_id="img-old",
        source_hash="hash-old",
        config_snapshot=str(snap),
    )
    b2 = registry.add_build("demo", status="running")
    registry.finish_build(b2, status="failed", error_summary="模拟新版本验证失败")
    return {"manifest": manifest, "b1": b1, "b2": b2}


@pytest.fixture()
def rt(monkeypatch):
    """mock DockerRuntime，记录 down/up 顺序与 tag 操作。"""
    runtime = MagicMock(name="runtime")
    order: list[str] = []
    tags: list[tuple[str, str]] = []
    runtime.container_id.return_value = "old-container"
    runtime.image_id.return_value = "img-old"
    runtime.down.side_effect = lambda *a, **k: order.append("down")
    runtime.up.side_effect = lambda *a, **k: order.append("up")
    monkeypatch.setattr(rollback, "DockerRuntime", MagicMock(return_value=runtime))
    monkeypatch.setattr(
        rebuild_txn, "tag_image", lambda src, dst: tags.append((src, dst))
    )
    monkeypatch.setattr(
        path_alias, "maybe_verify_alias_after_start", lambda *a, **k: True
    )
    monkeypatch.setattr("local_webpage_access.versions.image_exists", lambda ref: True)
    monkeypatch.setattr(
        hosting, "stop_or_abort", lambda *a, **k: order.append("stop")
    )
    return runtime, order, tags


def _verify(status: str):
    return {
        "overall_status": status,
        "liveness_passed": status != "failed",
        "mandatory_all_passed": status != "failed",
        "optional_warnings": [],
        "probe_notes": [],
        "observed_capabilities": [],
    }


# ---- 成功路径 -----------------------------------------------------------------


def test_rollback_to_previous_good(workspace, registry, config, deployed, rt, monkeypatch) -> None:
    runtime, order, tags = rt
    monkeypatch.setattr(
        hosting, "_evaluate_container_verification", lambda *a, **k: _verify("passed")
    )
    before = registry.get_revision("demo")

    manifest = rollback.rollback_instance(workspace, config, registry, "demo")

    assert order == ["stop", "up"]
    assert (f"lwa-demo-app:b{deployed['b1']}", "lwa-demo-app:latest") in tags
    # 快照生成物已恢复到 docker/
    assert (workspace.app_docker("demo") / "compose.yaml").read_text(encoding="utf-8") == COMPOSE_OLD
    assert manifest.status == Status.RUNNING
    assert manifest.container.imageId == "img-old"
    assert manifest.container.containerId == "old-container"
    op = registry.get_instance("demo")["last_operation_json"]
    op = json.loads(op)
    assert op["kind"] == "rollback" and op["status"] == "succeeded"
    assert op["toBuildId"] == deployed["b1"]
    assert registry.get_revision("demo") == before + 1
    events = registry.list_events("demo")
    assert any(e["event_type"] == "rollback" for e in events)


def test_rollback_explicit_to_build(workspace, registry, config, deployed, rt, monkeypatch) -> None:
    runtime, order, tags = rt
    monkeypatch.setattr(
        hosting, "_evaluate_container_verification", lambda *a, **k: _verify("passed")
    )
    manifest = rollback.rollback_instance(
        workspace, config, registry, "demo", to_build_id=deployed["b1"]
    )
    assert manifest.container.imageId == "img-old"


def test_rollback_uses_alias_guard(workspace, registry, config, deployed, rt, monkeypatch) -> None:
    runtime, order, tags = rt
    monkeypatch.setattr(
        hosting, "_evaluate_container_verification", lambda *a, **k: _verify("passed")
    )
    calls: list[str] = []

    def _guard(ws, cfg, reg, iid, manifest, **k):
        calls.append(iid)
        return True

    monkeypatch.setattr(path_alias, "maybe_verify_alias_after_start", _guard)
    rollback.rollback_instance(workspace, config, registry, "demo")
    assert calls == ["demo"]


# ---- 失败与守卫 ----------------------------------------------------------------


def test_rollback_verification_failure_restores_previous(workspace, registry, config,
                                                         deployed, rt, monkeypatch) -> None:
    runtime, order, tags = rt
    runtime.image_id.return_value = "img-current"
    outcomes = [_verify("failed"), _verify("passed")]

    def _verify_seq(*a, **k):
        return outcomes.pop(0)

    monkeypatch.setattr(hosting, "_evaluate_container_verification", _verify_seq)
    with pytest.raises(HostingError) as excinfo:
        rollback.rollback_instance(workspace, config, registry, "demo")

    assert "已恢复原版本" in str(excinfo.value)
    assert order == ["stop", "up", "down", "up"]
    # 原生成物与原镜像恢复
    assert (workspace.app_docker("demo") / "compose.yaml").read_text(encoding="utf-8") == COMPOSE_CURRENT
    assert ("img-current", "lwa-demo-app:latest") in tags
    op = json.loads(registry.get_instance("demo")["last_operation_json"])
    assert op["kind"] == "rollback" and op["status"] == "failed"


def test_rollback_compensation_verification_failure_reports_unrecovered(
    workspace, registry, config, deployed, rt, monkeypatch
) -> None:
    """BUG-812：补偿 up 成功不代表服务存活——原版本验证不过时不得登记 running。"""
    runtime, order, tags = rt
    outcomes = [_verify("failed"), _verify("failed")]

    def _verify_seq(*a, **k):
        return outcomes.pop(0)

    monkeypatch.setattr(hosting, "_evaluate_container_verification", _verify_seq)
    with pytest.raises(HostingError) as excinfo:
        rollback.rollback_instance(workspace, config, registry, "demo")

    assert "未能恢复原版本" in str(excinfo.value)
    assert "验证未通过" in str(excinfo.value)
    assert order == ["stop", "up", "down", "up"]
    # 未确认恢复：不得谎报 running，身份不可信须清 containerId（BUG-300 语义）
    saved = InstanceManifest.load(workspace.app_manifest_path("demo"))
    assert saved.status == Status.FAILED
    assert saved.container is not None and saved.container.containerId is None
    assert registry.get_instance("demo")["status"] == Status.FAILED.value
    op = json.loads(registry.get_instance("demo")["last_operation_json"])
    assert op["kind"] == "rollback" and op["status"] == "failed"


def test_rollback_tag_failure_after_stop_recovers_original(
    workspace, registry, config, deployed, rt, monkeypatch
) -> None:
    """BUG-799：停机成功后、改写 latest 前的失败（docker tag 抛错）也必须补偿。

    旧代码以 switched（已恢复快照+打回 latest）为补偿门槛：此窗口失败会按
    「现场未被改写」收尾——容器已停无人拉起，且快照生成物已被恢复成旧版。
    """
    runtime, order, tags = rt
    runtime.image_id.return_value = "img-current"
    attempts = {"n": 0}

    def _tag_fail_first(src, dst):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise DockerError("docker tag 拒绝", instance_id="demo", action="tag")
        tags.append((src, dst))

    monkeypatch.setattr(rebuild_txn, "tag_image", _tag_fail_first)
    monkeypatch.setattr(
        hosting, "_evaluate_container_verification", lambda *a, **k: _verify("passed")
    )

    with pytest.raises(HostingError, match="已恢复原版本"):
        rollback.rollback_instance(workspace, config, registry, "demo")

    # 停机 → 补偿 down → 恢复原生成物 → 原镜像打回 latest → up 拉回原版本
    assert order == ["stop", "down", "up"]
    assert (workspace.app_docker("demo") / "compose.yaml").read_text(encoding="utf-8") == COMPOSE_CURRENT
    assert tags == [("img-current", "lwa-demo-app:latest")]
    op = json.loads(registry.get_instance("demo")["last_operation_json"])
    assert op["kind"] == "rollback" and op["status"] == "failed"


def test_rollback_missing_snapshot_after_stop_recovers_original(
    workspace, registry, config, deployed, rt, monkeypatch
) -> None:
    """BUG-799 同窗口：快照在选定后、恢复前消失（TOCTOU）——同样补偿拉回原版本。"""
    runtime, order, tags = rt
    runtime.image_id.return_value = "img-current"
    monkeypatch.setattr(versions, "restore_config_snapshot", lambda *a, **k: False)
    monkeypatch.setattr(
        hosting, "_evaluate_container_verification", lambda *a, **k: _verify("passed")
    )

    with pytest.raises(HostingError, match="已恢复原版本"):
        rollback.rollback_instance(workspace, config, registry, "demo")

    assert order == ["stop", "down", "up"]
    assert (workspace.app_docker("demo") / "compose.yaml").read_text(encoding="utf-8") == COMPOSE_CURRENT
    assert tags == [("img-current", "lwa-demo-app:latest")]


def test_rollback_requires_yes_when_prestart_migration(workspace, registry, config,
                                                       deployed, rt, monkeypatch) -> None:
    runtime, order, tags = rt
    manifest = deployed["manifest"]
    manifest.preStart = "alembic upgrade head"
    manifest.save(workspace.app_manifest_path("demo"))
    with pytest.raises(rollback.RollbackNeedsYes):
        rollback.rollback_instance(workspace, config, registry, "demo")
    assert order == [] and tags == []
    # 生成物未被改动
    assert (workspace.app_docker("demo") / "compose.yaml").read_text(encoding="utf-8") == COMPOSE_CURRENT


def test_rollback_build_hooks_migration_also_requires_yes(workspace, registry, config,
                                                          deployed, rt, monkeypatch) -> None:
    runtime, order, tags = rt
    manifest = deployed["manifest"]
    manifest.buildHooks = ["npx prisma migrate deploy"]
    manifest.save(workspace.app_manifest_path("demo"))
    with pytest.raises(rollback.RollbackNeedsYes):
        rollback.rollback_instance(workspace, config, registry, "demo", yes=False)


def test_rollback_yes_proceeds_with_migration_warning(workspace, registry, config,
                                                      deployed, rt, monkeypatch) -> None:
    runtime, order, tags = rt
    manifest = deployed["manifest"]
    manifest.preStart = "alembic upgrade head"
    manifest.save(workspace.app_manifest_path("demo"))
    monkeypatch.setattr(
        hosting, "_evaluate_container_verification", lambda *a, **k: _verify("passed")
    )
    rollback.rollback_instance(workspace, config, registry, "demo", yes=True)
    assert order == ["stop", "up"]


def test_rollback_unknown_target(workspace, registry, config, deployed, rt) -> None:
    runtime, order, tags = rt
    with pytest.raises(HostingError):
        rollback.rollback_instance(workspace, config, registry, "demo", to_build_id=9999)
    with pytest.raises(HostingError):
        rollback.rollback_instance(workspace, config, registry, "demo", to_build_id=deployed["b2"])
    assert order == []


def test_rollback_revision_conflict(workspace, registry, config, deployed, rt) -> None:
    runtime, order, tags = rt
    with pytest.raises(LifecycleError) as excinfo:
        rollback.rollback_instance(
            workspace, config, registry, "demo", expected_revision=999
        )
    assert excinfo.value.code == "revision_conflict"
    assert order == []


def test_rollback_rejects_static_instance(workspace, registry, config, rt) -> None:
    from tests.scripts._helpers import make_static_manifest

    runtime, order, tags = rt
    workspace.ensure_app_dirs("stat")
    manifest = make_static_manifest("stat")
    manifest.save(workspace.app_manifest_path("stat"))
    registry.upsert_from_manifest(manifest)
    with pytest.raises(HostingError):
        rollback.rollback_instance(workspace, config, registry, "stat")
    assert order == [] and tags == []


# ---- daemon 自愈 ----------------------------------------------------------------


def _reconcile_with(workspace, registry, cfg, restarter):
    from local_webpage_access import daemon

    return daemon.reconcile(
        workspace, cfg, registry, restarter=restarter, monotonic=lambda: 10_000.0
    )


def _patch_observe_failed(monkeypatch) -> None:
    monkeypatch.setattr(
        "local_webpage_access.lifecycle.observe_status",
        lambda *a, **k: Status.FAILED,
    )


def _patch_container_missing(monkeypatch) -> MagicMock:
    """容器确实不存在：严格查询（含已退出容器）成功并返回 None。"""
    runtime = MagicMock(name="daemon-runtime")
    runtime.container_id.return_value = None
    runtime.container_id_strict.return_value = None
    monkeypatch.setattr(
        "local_webpage_access.docker_runtime.DockerRuntime",
        MagicMock(return_value=runtime),
    )
    return runtime


def test_daemon_self_heal_prefers_last_good(workspace, registry, config, deployed,
                                            rt, monkeypatch) -> None:
    runtime, order, tags = rt
    _patch_observe_failed(monkeypatch)
    probe = _patch_container_missing(monkeypatch)
    registry.update_last_operation(
        "demo",
        {"kind": "rebuild", "status": "failed", "buildId": deployed["b2"],
         "startedAt": "t", "source": "host_container"},
    )
    monkeypatch.setattr(
        hosting, "_evaluate_container_verification", lambda *a, **k: _verify("passed")
    )
    restarts: list[str] = []

    def _restarter(ws, cfg2, reg, iid):
        restarts.append(iid)

    cfg = config.model_copy(update={"selfHealPreferLastGood": True})
    restarted = _reconcile_with(workspace, registry, cfg, _restarter)
    assert restarted == ["demo"]
    assert restarts == []  # 未回退常规 start_instance
    # 走回滚：停 → up 旧版本
    assert order == ["stop", "up"]
    assert (f"lwa-demo-app:b{deployed['b1']}", "lwa-demo-app:latest") in tags
    probe.container_id_strict.assert_called_with("demo", all_containers=True)
    assert any(
        e["event_type"] == "rollback_self_heal"
        for e in registry.list_events("demo")
    )


def test_daemon_self_heal_rollback_fails_falls_back_to_restart(
    workspace, registry, config, deployed, rt, monkeypatch
) -> None:
    runtime, order, tags = rt
    _patch_observe_failed(monkeypatch)
    _patch_container_missing(monkeypatch)
    registry.update_last_operation(
        "demo",
        {"kind": "rebuild", "status": "failed", "buildId": deployed["b2"],
         "startedAt": "t", "source": "host_container"},
    )
    # 回滚验证必败 → 补偿后抛错 → 回退 start_instance
    monkeypatch.setattr(
        hosting, "_evaluate_container_verification", lambda *a, **k: _verify("failed")
    )
    restarts: list[str] = []

    def _restarter(ws, cfg2, reg, iid):
        restarts.append(iid)

    cfg = config.model_copy(update={"selfHealPreferLastGood": True})
    restarted = _reconcile_with(workspace, registry, cfg, _restarter)
    assert restarted == ["demo"]
    assert restarts == ["demo"]


def test_daemon_self_heal_disabled_falls_back_to_restart(workspace, registry, config,
                                                         deployed, rt, monkeypatch) -> None:
    runtime, order, tags = rt
    _patch_observe_failed(monkeypatch)
    _patch_container_missing(monkeypatch)
    registry.update_last_operation(
        "demo",
        {"kind": "rebuild", "status": "failed", "buildId": deployed["b2"],
         "startedAt": "t", "source": "host_container"},
    )
    restarts: list[str] = []

    def _restarter(ws, cfg2, reg, iid):
        restarts.append(iid)

    cfg = config.model_copy(update={"selfHealPreferLastGood": False})
    restarted = _reconcile_with(workspace, registry, cfg, _restarter)
    assert restarted == ["demo"]
    assert restarts == ["demo"]  # 回退到常规 start_instance
    assert order == []


def test_daemon_self_heal_skips_when_container_present(workspace, registry, config,
                                                       deployed, rt, monkeypatch) -> None:
    """容器仍在（如旧版本仍服务）→ 不触发回滚，走常规 start。"""
    runtime, order, tags = rt
    _patch_observe_failed(monkeypatch)
    present = MagicMock(name="daemon-runtime")
    present.container_id.return_value = "cur-container"
    present.container_id_strict.return_value = "cur-container"
    monkeypatch.setattr(
        "local_webpage_access.docker_runtime.DockerRuntime",
        MagicMock(return_value=present),
    )
    registry.update_last_operation(
        "demo",
        {"kind": "rebuild", "status": "failed", "buildId": deployed["b2"],
         "startedAt": "t", "source": "host_container"},
    )
    restarts: list[str] = []

    def _restarter(ws, cfg2, reg, iid):
        restarts.append(iid)

    cfg = config.model_copy(update={"selfHealPreferLastGood": True})
    restarted = _reconcile_with(workspace, registry, cfg, _restarter)
    assert restarted == ["demo"]
    assert restarts == ["demo"]
    assert order == []
    present.container_id_strict.assert_called_with("demo", all_containers=True)


def test_daemon_self_heal_skips_when_last_op_ok(workspace, registry, config, deployed,
                                                rt, monkeypatch) -> None:
    """最近一次操作不是失败 → 不触发回滚。"""
    runtime, order, tags = rt
    _patch_observe_failed(monkeypatch)
    _patch_container_missing(monkeypatch)
    registry.update_last_operation(
        "demo",
        {"kind": "stop", "status": "succeeded", "startedAt": "t", "source": "cli"},
    )
    restarts: list[str] = []

    def _restarter(ws, cfg2, reg, iid):
        restarts.append(iid)

    cfg = config.model_copy(update={"selfHealPreferLastGood": True})
    restarted = _reconcile_with(workspace, registry, cfg, _restarter)
    assert restarted == ["demo"]
    assert restarts == ["demo"]
    assert order == []


# ---- CLI ----------------------------------------------------------------------


@pytest.fixture()
def ws_config(workspace, config):
    """CLI 测试需要 local-web.yml 才能定位工作区。"""
    workspace.config_path.write_text(config.to_yaml(), encoding="utf-8")
    return workspace


def test_cli_versions_lists_builds(ws_config, registry, config, deployed, monkeypatch) -> None:
    from typer.testing import CliRunner

    from local_webpage_access.cli import app

    monkeypatch.chdir(ws_config.root)
    monkeypatch.setattr(
        "local_webpage_access.versions.image_exists", lambda ref: ref.endswith(f":b{deployed['b1']}")
    )
    monkeypatch.setattr(
        "local_webpage_access.versions.image_disk_usage_bytes", lambda refs: 42
    )
    result = CliRunner().invoke(app, ["versions", "demo"])
    assert result.exit_code == 0, result.output
    assert f"b{deployed['b1']}" in result.output
    assert "hash-old" in result.output
    assert "success" in result.output
    assert "占用" in result.output


def test_cli_versions_rejects_static(ws_config, registry, config, monkeypatch) -> None:
    from typer.testing import CliRunner

    from local_webpage_access.cli import app
    from tests.scripts._helpers import make_static_manifest

    ws_config.ensure_app_dirs("stat")
    manifest = make_static_manifest("stat")
    manifest.save(ws_config.app_manifest_path("stat"))
    registry.upsert_from_manifest(manifest)
    monkeypatch.chdir(ws_config.root)
    result = CliRunner().invoke(app, ["versions", "stat"])
    assert result.exit_code == 1


def test_cli_rollback_success(ws_config, registry, config, deployed, rt, monkeypatch) -> None:
    from typer.testing import CliRunner

    from local_webpage_access.cli import app

    runtime, order, tags = rt
    monkeypatch.chdir(ws_config.root)
    monkeypatch.setattr(
        hosting, "_evaluate_container_verification", lambda *a, **k: _verify("passed")
    )
    result = CliRunner().invoke(app, ["rollback", "demo", "--yes"])
    assert result.exit_code == 0, result.output
    assert "已回滚实例：demo" in result.output
    assert order == ["stop", "up"]


def test_cli_rollback_migration_requires_yes(ws_config, registry, config, deployed,
                                             rt, monkeypatch) -> None:
    from typer.testing import CliRunner

    from local_webpage_access.cli import app

    runtime, order, tags = rt
    manifest = deployed["manifest"]
    manifest.preStart = "alembic upgrade head"
    manifest.save(ws_config.app_manifest_path("demo"))
    monkeypatch.chdir(ws_config.root)
    result = CliRunner().invoke(app, ["rollback", "demo"])
    assert result.exit_code == 1
    assert "--yes" in result.output
    assert "数据库" in result.output  # D8 醒目提示
    assert order == []


def test_status_shows_retained_usage(ws_config, registry, config, deployed, monkeypatch) -> None:
    """lwa status <id> 对容器实例显示保留版本占用；无产物行时不显示。"""
    from typer.testing import CliRunner

    from local_webpage_access.cli import app

    monkeypatch.chdir(ws_config.root)
    monkeypatch.setattr(
        "local_webpage_access.versions.image_disk_usage_bytes", lambda refs: 1024
    )
    monkeypatch.setattr(
        "local_webpage_access.status.sync_status", lambda *a, **k: None
    )

    # 无成功产物行（b1 是旧成功行）——先清掉 versions 占用显示条件
    result = CliRunner().invoke(app, ["status", "demo"])
    assert result.exit_code == 0, result.output
    assert "保留版本" in result.output
    assert "1.0KiB" in result.output


# ---- hosting 集成：成功部署留档 + GC --------------------------------------------


def test_host_container_archives_build_and_runs_gc(workspace, registry, config,
                                                   deployed, monkeypatch) -> None:
    """S3-01：host_container 成功路径——build 后打 b<id> 标签 + 生成物快照 +
    builds 行产物信息；部署成功后以新镜像为 in-use 执行 GC。"""
    rt = MagicMock(name="runtime")
    rt.is_running.return_value = True
    # 观测到的容器镜像即新镜像（GC 的 in-use 依据）
    rt.image_id.return_value = "img-new"
    rt.container_id.return_value = "new-container"
    monkeypatch.setattr(hosting, "DockerRuntime", MagicMock(return_value=rt))
    monkeypatch.setattr(rebuild_txn, "tag_image", lambda *a, **k: None)
    monkeypatch.setattr(
        "local_webpage_access.container_identity.ensure_non_root_identity_ready",
        lambda *a: None,
    )
    monkeypatch.setattr(hosting, "_rescue_container_data_before_rebuild",
                        lambda *a, **k: None)
    monkeypatch.setattr(hosting, "generate_dockerfile", lambda *a, **k: None)
    monkeypatch.setattr(hosting, "generate_compose", lambda *a, **k: None)
    monkeypatch.setattr(hosting, "generate_env", lambda *a, **k: None)
    monkeypatch.setattr(hosting, "_ensure_container_port", lambda *a, **k: (21001, False))
    monkeypatch.setattr(hosting, "_wait_for_http", lambda *a, **k: True)
    monkeypatch.setattr(
        hosting, "_evaluate_container_verification",
        lambda *a, **k: {
            "overall_status": "passed", "liveness_passed": True,
            "mandatory_all_passed": True, "optional_warnings": [],
            "probe_notes": [], "observed_capabilities": [],
        },
    )
    monkeypatch.setattr(
        "local_webpage_access.versions.resolve_repo_image_id", lambda repo: "img-new"
    )
    gc_calls: list[dict] = []
    monkeypatch.setattr(
        "local_webpage_access.versions.gc_build_artifacts",
        lambda *a, **k: gc_calls.append(k) or {"kept": [], "removed": []},
    )
    manifest = InstanceManifest.load(workspace.app_manifest_path("demo"))
    manifest.preStart = "alembic upgrade head"
    manifest.save(workspace.app_manifest_path("demo"))

    hosting.host_container(workspace, config, registry, "demo")

    rows = {int(r["id"]): r for r in registry.list_builds("demo")}
    build_id = max(rows)
    assert rows[build_id]["image_ref"] == f"lwa-demo-app:b{build_id}"
    assert rows[build_id]["image_id"] == "img-new"
    assert rows[build_id]["source_hash"]  # 源码目录指纹已回写
    snap = workspace.app_docker("demo") / "builds" / str(build_id)
    assert (snap / "compose.yaml").is_file()
    assert (snap / "migration-commands.txt").read_text(encoding="utf-8") == "alembic upgrade head"
    # GC 以新镜像为 in-use 触发
    assert len(gc_calls) == 1
    assert gc_calls[0].get("in_use_image_id") == "img-new"


# ---- 默认回滚目标不得是当前在用版本 ----------------------------------------------------


def _add_current_good_build(workspace, registry, *, image_id: str = "img-current") -> int:
    """当前在用版本对应的**成功**构建（最新一条，带镜像引用与完整快照）。"""
    build_id = registry.add_build("demo", status="running")
    registry.finish_build(build_id, status="success")
    snap = workspace.app_docker("demo") / "builds" / str(build_id)
    snap.mkdir(parents=True)
    (snap / "compose.yaml").write_text(COMPOSE_CURRENT, encoding="utf-8")
    (snap / "Dockerfile").write_text("FROM x\n", encoding="utf-8")
    (snap / ".env").write_text("A=1\n", encoding="utf-8")
    registry.record_build_artifacts(
        build_id,
        image_ref=f"lwa-demo-app:b{build_id}",
        image_id=image_id,
        source_hash=f"hash-{build_id}",
        config_snapshot=str(snap),
    )
    return build_id


def test_default_rollback_skips_the_version_currently_in_use(
    workspace, registry, config, deployed, rt, monkeypatch
) -> None:
    """当前版本自身的构建是最近一个成功构建：未指定 --to 时必须退到更早的 b1，
    而不是把当前版本当“上一可用版本”原地重部署。"""
    runtime, order, tags = rt
    monkeypatch.setattr(
        hosting, "_evaluate_container_verification", lambda *a, **k: _verify("passed")
    )
    current_build = _add_current_good_build(workspace, registry)

    manifest = rollback.rollback_instance(workspace, config, registry, "demo")

    assert (f"lwa-demo-app:b{deployed['b1']}", "lwa-demo-app:latest") in tags
    assert all(src != f"lwa-demo-app:b{current_build}" for src, _dst in tags)
    assert manifest.container.imageId == "img-old"
    op = json.loads(registry.get_instance("demo")["last_operation_json"])
    assert op["toBuildId"] == deployed["b1"]


def test_select_rollback_target_excludes_in_use_image_with_sha256_prefix(
    workspace, registry, deployed, monkeypatch
) -> None:
    monkeypatch.setattr("local_webpage_access.versions.image_exists", lambda ref: True)
    current_build = _add_current_good_build(workspace, registry)

    # 不排除：最新成功构建（当前版本自身）被选中——这正是需要 exclude 的原因
    assert rollback.select_rollback_target(workspace, registry, "demo")["id"] == current_build
    # 排除在用镜像（容忍 docker 的 sha256: 前缀写法）：选中更早的 b1
    for in_use in ("img-current", "sha256:img-current"):
        target = rollback.select_rollback_target(
            workspace, registry, "demo", exclude_image_id=in_use
        )
        assert target is not None and target["id"] == deployed["b1"]


def test_default_rollback_with_only_current_version_errors_without_side_effects(
    workspace, registry, config, deployed, rt, monkeypatch
) -> None:
    """没有比当前更早的可用版本：明确报错，不停容器、不改镜像。"""
    runtime, order, tags = rt
    monkeypatch.setattr(
        hosting, "_evaluate_container_verification", lambda *a, **k: _verify("passed")
    )
    # 去掉历史版本 b1 的镜像可用性，只剩当前版本自身
    _add_current_good_build(workspace, registry)
    monkeypatch.setattr(
        "local_webpage_access.versions.image_exists",
        lambda ref: not ref.endswith(f":b{deployed['b1']}"),
    )

    with pytest.raises(HostingError) as excinfo:
        rollback.rollback_instance(workspace, config, registry, "demo")

    assert "没有可回滚的留档版本" in str(excinfo.value)
    assert order == [] and tags == []


# ---- P1/P2：锁外提交、回滚钉住、补偿身份收敛 ------------------------------------------


def test_rollback_commit_inside_lock_does_not_overwrite_concurrent_stop(
    workspace, registry, config, deployed, rt, monkeypatch
) -> None:
    """回滚成功落盘必须在实例锁内。锁释放后并发 stop 写入 STOPPED，不得被锁外 save 盖回 RUNNING。"""
    from local_webpage_access.lifecycle import instance_lock as real_lock
    from local_webpage_access.models import InstanceManifest

    runtime, order, tags = rt
    monkeypatch.setattr(
        hosting, "_evaluate_container_verification", lambda *a, **k: _verify("passed")
    )

    @contextlib.contextmanager
    def wrapping_lock(ws, iid, **kwargs):
        with real_lock(ws, iid, **kwargs):
            yield
        stopped = InstanceManifest.load(ws.app_manifest_path(iid))
        stopped.status = Status.STOPPED
        stopped.desiredState = DesiredState.STOPPED
        stopped.save(ws.app_manifest_path(iid))
        registry.update_status(
            iid, Status.STOPPED.value, desired_state=DesiredState.STOPPED.value
        )

    monkeypatch.setattr("local_webpage_access.lifecycle.instance_lock", wrapping_lock)
    rollback.rollback_instance(workspace, config, registry, "demo")
    final = InstanceManifest.load(workspace.app_manifest_path("demo"))
    assert final.status == Status.STOPPED
    assert final.desiredState == DesiredState.STOPPED
    row = registry.get_instance("demo")
    assert row["status"] == Status.STOPPED.value


def test_rollback_pins_so_start_does_not_rerender_current_source(
    workspace, registry, config, deployed, rt, monkeypatch
) -> None:
    """回滚后 start/restart 不得按当前源码重渲染生成物（否则等于重新部署新版本）。"""
    from local_webpage_access.lifecycle import _deployed_action
    from local_webpage_access.models import InstanceManifest

    runtime, order, tags = rt
    monkeypatch.setattr(
        hosting, "_evaluate_container_verification", lambda *a, **k: _verify("passed")
    )
    rollback.rollback_instance(workspace, config, registry, "demo")
    manifest = InstanceManifest.load(workspace.app_manifest_path("demo"))
    fps = manifest.deploymentFingerprints or {}
    assert fps.get("rollbackBuildId") == deployed["b1"]
    assert (workspace.app_docker("demo") / "compose.yaml").read_text(encoding="utf-8") == COMPOSE_OLD

    called: list[int] = []

    def _boom(*_a, **_k):
        called.append(1)
        raise AssertionError("回滚钉住期间不得 converge 当前源码")

    monkeypatch.setattr(
        "local_webpage_access.generated_convergence.converge_generated_artifacts", _boom
    )
    action, changed = _deployed_action(workspace, config, registry, "demo", manifest)
    assert action == "none" and changed == []
    assert called == []
    assert (workspace.app_docker("demo") / "compose.yaml").read_text(encoding="utf-8") == COMPOSE_OLD


def test_rollback_compensation_refreshes_identity_after_reup(
    workspace, registry, config, deployed, rt, monkeypatch
) -> None:
    """补偿 up 之后必须观测新 containerId/imageId 并写回，状态收敛为 RUNNING。"""
    from local_webpage_access.models import InstanceManifest

    runtime, order, tags = rt
    runtime.container_id.return_value = "compensated-cid"
    runtime.image_id.return_value = "img-current"
    # 首次（回滚主路径）验证失败触发补偿；补偿 up 后的实证验证（BUG-812）通过
    outcomes = [_verify("failed"), _verify("passed")]

    def _verify_seq(*a, **k):
        return outcomes.pop(0)

    monkeypatch.setattr(hosting, "_evaluate_container_verification", _verify_seq)
    with pytest.raises(HostingError, match="已恢复原版本"):
        rollback.rollback_instance(workspace, config, registry, "demo")

    manifest = InstanceManifest.load(workspace.app_manifest_path("demo"))
    assert manifest.container.containerId == "compensated-cid"
    assert manifest.container.imageId == "img-current"
    assert manifest.status == Status.RUNNING
    row = registry.get_instance("demo")
    assert row["status"] == Status.RUNNING.value
    container = registry.get_container("demo") or {}
    assert container.get("container_id") == "compensated-cid"


def test_rollback_rereads_state_inside_lock(
    workspace, registry, config, deployed, rt, monkeypatch
) -> None:
    """等锁期间写入的 manifest / 生成物必须在锁内重读，成功落盘不得盖回锁前副本。"""
    from local_webpage_access.lifecycle import instance_lock as real_lock
    from local_webpage_access.models import InstanceManifest

    runtime, order, tags = rt
    monkeypatch.setattr(
        hosting, "_evaluate_container_verification", lambda *a, **k: _verify("passed")
    )

    @contextlib.contextmanager
    def wrapping_lock(ws, iid, **kwargs):
        fresh = InstanceManifest.load(ws.app_manifest_path(iid))
        fresh.buildHooks = ["echo configured"]
        fresh.save(ws.app_manifest_path(iid))
        with real_lock(ws, iid, **kwargs):
            yield

    monkeypatch.setattr("local_webpage_access.lifecycle.instance_lock", wrapping_lock)
    rollback.rollback_instance(workspace, config, registry, "demo")
    final = InstanceManifest.load(workspace.app_manifest_path("demo"))
    assert final.buildHooks == ["echo configured"]


def test_rollback_compensation_uses_files_seen_inside_lock(
    workspace, registry, config, deployed, rt, monkeypatch
) -> None:
    """补偿必须恢复进锁时的现场，不得用锁前读到的旧生成物撤销期间已完成的回滚。"""
    from local_webpage_access.lifecycle import instance_lock as real_lock
    from local_webpage_access.models import InstanceManifest

    runtime, order, tags = rt
    # 首次（回滚主路径）验证失败触发补偿；补偿 up 后的实证验证（BUG-812）通过
    outcomes = [_verify("failed"), _verify("passed")]

    def _verify_seq(*a, **k):
        return outcomes.pop(0)

    monkeypatch.setattr(hosting, "_evaluate_container_verification", _verify_seq)
    newer = "services:\n  app:\n    image: concurrent\n"

    @contextlib.contextmanager
    def wrapping_lock(ws, iid, **kwargs):
        docker_dir = ws.app_docker(iid)
        (docker_dir / "compose.yaml").write_text(newer, encoding="utf-8")
        fresh = InstanceManifest.load(ws.app_manifest_path(iid))
        fresh.container.imageId = "img-concurrent"
        fresh.save(ws.app_manifest_path(iid))
        with real_lock(ws, iid, **kwargs):
            yield

    monkeypatch.setattr("local_webpage_access.lifecycle.instance_lock", wrapping_lock)
    runtime.image_id.return_value = "img-concurrent"
    with pytest.raises(HostingError, match="已恢复原版本"):
        rollback.rollback_instance(workspace, config, registry, "demo")
    assert (workspace.app_docker("demo") / "compose.yaml").read_text(encoding="utf-8") == newer
    assert ("img-concurrent", "lwa-demo-app:latest") in tags


def test_rollback_rechecks_migration_inside_lock(
    workspace, registry, config, deployed, rt, monkeypatch
) -> None:
    """等锁期间新增的迁移命令必须在锁内再次拦截，且尚未停机。"""
    from local_webpage_access.lifecycle import instance_lock as real_lock
    from local_webpage_access.models import InstanceManifest

    runtime, order, tags = rt

    @contextlib.contextmanager
    def wrapping_lock(ws, iid, **kwargs):
        fresh = InstanceManifest.load(ws.app_manifest_path(iid))
        fresh.buildHooks = ["alembic upgrade head"]
        fresh.save(ws.app_manifest_path(iid))
        with real_lock(ws, iid, **kwargs):
            yield

    monkeypatch.setattr("local_webpage_access.lifecycle.instance_lock", wrapping_lock)
    with pytest.raises(rollback.RollbackNeedsYes):
        rollback.rollback_instance(workspace, config, registry, "demo")
    assert order == []


def test_rollback_compensation_up_failure_does_not_claim_running(
    workspace, registry, config, deployed, rt, monkeypatch
) -> None:
    """补偿 up 失败时不得仍登记 running，也不得声称已恢复原版本。"""
    from local_webpage_access.models import InstanceManifest

    runtime, order, tags = rt
    calls = {"n": 0}

    def _up(*_a, **_k):
        calls["n"] += 1
        order.append("up")
        if calls["n"] >= 2:
            raise DockerError("up failed", instance_id="demo")

    runtime.up.side_effect = _up
    monkeypatch.setattr(
        hosting, "_evaluate_container_verification", lambda *a, **k: _verify("failed")
    )
    with pytest.raises(HostingError, match="未能恢复原版本") as excinfo:
        rollback.rollback_instance(workspace, config, registry, "demo")
    assert "已恢复原版本" not in str(excinfo.value)
    manifest = InstanceManifest.load(workspace.app_manifest_path("demo"))
    assert manifest.status == Status.FAILED
    assert manifest.container.containerId is None
    row = registry.get_instance("demo")
    assert row["status"] == Status.FAILED.value
    container = registry.get_container("demo") or {}
    assert not container.get("container_id")


def test_rollback_syncs_fingerprints_so_change_action_is_not_full_rebuild(
    workspace, registry, config, deployed, rt, monkeypatch
) -> None:
    """回滚必须把 deploymentFingerprints 同步成留档基线；否则 _fingerprint_change_action
    会因 imageId 不一致返回 full_rebuild，普通 restart 把当前源码再部署回来。"""
    from local_webpage_access.lifecycle import (
        _compute_deployment_fingerprints,
        _fingerprint_change_action,
    )
    from local_webpage_access.models import InstanceManifest

    runtime, order, tags = rt
    monkeypatch.setattr(
        hosting, "_evaluate_container_verification", lambda *a, **k: _verify("passed")
    )
    before = InstanceManifest.load(workspace.app_manifest_path("demo"))
    before.deploymentFingerprints = {
        "sourceHash": "new-src",
        "planHash": "new-plan",
        "configHash": "new-cfg",
        "buildConfigHash": "new-build",
        "runtimeConfigHash": "new-rt",
        "imageId": "img-current",
    }
    before.save(workspace.app_manifest_path("demo"))

    rollback.rollback_instance(workspace, config, registry, "demo")
    manifest = InstanceManifest.load(workspace.app_manifest_path("demo"))
    fps = manifest.deploymentFingerprints or {}
    assert fps.get("imageId") == "img-old"
    assert fps.get("rollbackBuildId") == deployed["b1"]
    current = _compute_deployment_fingerprints(workspace, manifest)
    action, changed = _fingerprint_change_action(fps, current)
    assert action == "none", changed


def test_rollback_rewrites_restored_host_port_before_verify(
    workspace, registry, config, deployed, rt, monkeypatch
) -> None:
    """快照 .env 的 HOST_PORT 可能是历史分配；验证与登记必须用当前 hostPort。"""
    from local_webpage_access.models import InstanceManifest

    runtime, order, tags = rt
    snap = workspace.app_docker("demo") / "builds" / str(deployed["b1"])
    (snap / ".env").write_text("HOST_PORT=18000\nA=1\n", encoding="utf-8")
    seen: list[int] = []

    def _capture(port, *a, **k):
        seen.append(port)
        return _verify("passed")

    monkeypatch.setattr(hosting, "_evaluate_container_verification", _capture)
    rollback.rollback_instance(workspace, config, registry, "demo")
    env_text = (workspace.app_docker("demo") / ".env").read_text(encoding="utf-8")
    assert "HOST_PORT=21001" in env_text
    assert "HOST_PORT=18000" not in env_text
    assert seen == [21001]
    manifest = InstanceManifest.load(workspace.app_manifest_path("demo"))
    assert manifest.container.hostPort == 21001


def _host_mocks(monkeypatch, registry, *, running: bool, image_id: str, verify_failed: bool):
    """host_container 的 Docker/生成器替身。build 成功时把 builds 行标成 success。"""
    rt = MagicMock(name="runtime")
    rt.is_running.return_value = running
    rt.image_id.return_value = image_id
    rt.container_id.return_value = "new-container"
    tags: list[tuple[str, str]] = []

    def _build(iid, *, build_id=None, **_k):
        if build_id is not None:
            registry.finish_build(build_id, status="success")

    rt.build.side_effect = _build
    rt.status.return_value = None
    rt.inspect_state.return_value = {}
    rt.logs.return_value = ""
    monkeypatch.setattr(hosting, "DockerRuntime", MagicMock(return_value=rt))
    monkeypatch.setattr(rebuild_txn, "tag_image", lambda src, dst: tags.append((src, dst)))
    monkeypatch.setattr(
        "local_webpage_access.container_identity.ensure_non_root_identity_ready",
        lambda *a: None,
    )
    monkeypatch.setattr(hosting, "_rescue_container_data_before_rebuild", lambda *a, **k: None)
    monkeypatch.setattr(hosting, "generate_dockerfile", lambda *a, **k: None)
    monkeypatch.setattr(hosting, "generate_compose", lambda *a, **k: None)
    monkeypatch.setattr(hosting, "generate_env", lambda *a, **k: None)
    monkeypatch.setattr(hosting, "_ensure_container_port", lambda *a, **k: (21001, False))
    monkeypatch.setattr(hosting, "_wait_for_http", lambda *a, **k: True)
    monkeypatch.setattr(
        "local_webpage_access.versions.resolve_repo_image_id", lambda repo: image_id
    )
    monkeypatch.setattr("local_webpage_access.versions.image_exists", lambda ref: True)
    status = "failed" if verify_failed else "passed"
    monkeypatch.setattr(
        hosting, "_evaluate_container_verification", lambda *a, **k: _verify(status)
    )
    return rt, tags


def test_failed_probe_does_not_keep_build_as_rollback_target(
    workspace, registry, config, deployed, monkeypatch
) -> None:
    """镜像构建成功但必选探针失败：该版本不得成为默认回滚目标。

    imageRetention=1 的后续成功部署必须保护真正可用的旧版本，而不是这次失败版本。
    """
    from local_webpage_access.models import InstanceManifest

    manifest = InstanceManifest.load(workspace.app_manifest_path("demo"))
    manifest.container.rebuildStrategy = "stop-first"
    manifest.save(workspace.app_manifest_path("demo"))
    _rt, tags = _host_mocks(
        monkeypatch, registry, running=False, image_id="img-failed", verify_failed=True
    )

    with pytest.raises(HostingError, match="必选探针未通过"):
        hosting.host_container(workspace, config, registry, "demo")

    rows = {int(row["id"]): row for row in registry.list_builds("demo")}
    failed_id = max(rows)
    assert failed_id != deployed["b1"]
    assert not rows[failed_id].get("image_ref")
    assert not any(str(dst).endswith(f":b{failed_id}") for _src, dst in tags)
    target = rollback.select_rollback_target(
        workspace, registry, "demo", exclude_image_id="img-current"
    )
    assert target is not None and target["id"] == deployed["b1"]

    good = _add_current_good_build(workspace, registry, image_id="img-c")
    monkeypatch.setattr("local_webpage_access.versions._image_rm", lambda ref: None)
    monkeypatch.setattr(
        "local_webpage_access.versions.image_inspect_status", lambda ref: "missing"
    )
    cfg = config.model_copy(update={"imageRetention": 1})
    result = versions.gc_build_artifacts(
        workspace, cfg, registry, "demo", in_use_image_id="img-c"
    )
    assert deployed["b1"] in result["kept"]
    assert deployed["b1"] not in result["removed"]
    assert good in result["kept"]
    kept_row = next(row for row in registry.list_builds("demo") if int(row["id"]) == deployed["b1"])
    assert kept_row["image_ref"]


def test_build_first_probe_failure_revokes_archived_eligibility(
    workspace, registry, config, deployed, monkeypatch
) -> None:
    """先建后换在探针失败并补偿回旧版本后，失败构建不再保有可回滚资格。"""
    _host_mocks(
        monkeypatch, registry, running=True, image_id="img-failed", verify_failed=True
    )
    with pytest.raises(HostingError, match="已回滚到旧版本并恢复服务"):
        hosting.host_container(workspace, config, registry, "demo")

    rows = {int(row["id"]): row for row in registry.list_builds("demo")}
    failed_id = max(rows)
    assert not rows[failed_id].get("image_ref")
    target = rollback.select_rollback_target(workspace, registry, "demo")
    assert target is not None and target["id"] == deployed["b1"]


def test_rollback_target_dockerfile_cmd_requires_yes_without_current_hooks(
    workspace, registry, config, deployed, rt
) -> None:
    """当前 manifest 已无迁移钩子，但目标快照 Dockerfile 的 CMD 仍含迁移。"""
    runtime, order, tags = rt
    snap = workspace.app_docker("demo") / "builds" / str(deployed["b1"])
    (snap / "Dockerfile").write_text(
        'FROM x\nCMD ["sh", "-c", "alembic upgrade head && exec uvicorn app:app"]\n',
        encoding="utf-8",
    )
    with pytest.raises(rollback.RollbackNeedsYes, match="目标构建"):
        rollback.rollback_instance(workspace, config, registry, "demo", yes=False)
    assert order == [] and tags == []


def test_rollback_archived_migration_commands_require_yes(
    workspace, registry, config, deployed, rt
) -> None:
    """显式留档的迁移命令即使不在当前 manifest，也要求确认。"""
    runtime, order, tags = rt
    snap = workspace.app_docker("demo") / "builds" / str(deployed["b1"])
    (snap / "migration-commands.txt").write_text("npx prisma migrate deploy\n", encoding="utf-8")
    with pytest.raises(rollback.RollbackNeedsYes):
        rollback.rollback_instance(workspace, config, registry, "demo")
    assert order == [] and tags == []


def test_rollback_yes_allows_target_migration_command(
    workspace, registry, config, deployed, rt, monkeypatch
) -> None:
    runtime, order, tags = rt
    snap = workspace.app_docker("demo") / "builds" / str(deployed["b1"])
    (snap / "migration-commands.txt").write_text("alembic upgrade head\n", encoding="utf-8")
    monkeypatch.setattr(
        hosting, "_evaluate_container_verification", lambda *a, **k: _verify("passed")
    )
    rollback.rollback_instance(workspace, config, registry, "demo", yes=True)
    assert order == ["stop", "up"]


def test_versions_payload_requires_confirm_for_target_migration(
    workspace, registry, config, deployed
) -> None:
    from local_webpage_access.manager_history_routes import versions_payload

    snap = workspace.app_docker("demo") / "builds" / str(deployed["b1"])
    (snap / "Dockerfile").write_text(
        'FROM x\nCMD ["sh", "-c", "alembic upgrade head && exec uvicorn"]\n',
        encoding="utf-8",
    )
    payload = versions_payload(workspace, config, registry, "demo")
    assert payload is not None
    assert payload["requiresConfirm"] is True
    matched = [item for item in payload["versions"] if item["id"] == deployed["b1"]]
    assert matched and matched[0]["hasMigration"] is True
    assert matched[0]["requiresConfirm"] is True


def test_compensation_retag_failure_does_not_claim_original_restored(
    workspace, registry, config, deployed, rt, monkeypatch
) -> None:
    """目标镜像已是 latest、补偿 retag 失败时，探针通过也不能宣称恢复了原版本。"""
    runtime, order, tags = rt
    runtime.image_id.return_value = "img-old"
    calls = {"tag": 0, "up": 0}

    def _tag(src, dst):
        calls["tag"] += 1
        if calls["tag"] >= 2:
            raise DockerError("原镜像 retag 失败", instance_id="demo", action="tag")
        tags.append((src, dst))

    def _up(*_a, **_k):
        calls["up"] += 1
        order.append("up")
        if calls["up"] == 1:
            raise DockerError("首次 up 暂时失败", instance_id="demo", action="up")

    runtime.up.side_effect = _up
    monkeypatch.setattr(rebuild_txn, "tag_image", _tag)
    monkeypatch.setattr(
        hosting, "_evaluate_container_verification", lambda *a, **k: _verify("passed")
    )
    with pytest.raises(HostingError, match="未能恢复原版本") as excinfo:
        rollback.rollback_instance(workspace, config, registry, "demo")
    assert "已恢复原版本" not in str(excinfo.value)
    saved = InstanceManifest.load(workspace.app_manifest_path("demo"))
    assert saved.status == Status.FAILED
    assert saved.container is not None
    assert saved.container.containerId is None
    assert saved.container.imageId == "img-current"
    assert registry.get_instance("demo")["status"] == Status.FAILED.value


def test_daemon_self_heal_skips_when_only_exited_container_remains(
    workspace, registry, config, deployed, rt, monkeypatch
) -> None:
    """已退出容器仍在：不得因运行中查询为空而自动回滚。"""
    runtime, order, tags = rt
    _patch_observe_failed(monkeypatch)
    probe = MagicMock(name="daemon-runtime")
    probe.container_id.return_value = None
    probe.container_id_strict.return_value = "exited-container"
    monkeypatch.setattr(
        "local_webpage_access.docker_runtime.DockerRuntime",
        MagicMock(return_value=probe),
    )
    registry.update_last_operation(
        "demo",
        {"kind": "rebuild", "status": "failed", "buildId": deployed["b2"],
         "startedAt": "t", "source": "host_container"},
    )
    restarts: list[str] = []

    def _restarter(ws, cfg2, reg, iid):
        restarts.append(iid)

    cfg = config.model_copy(update={"selfHealPreferLastGood": True})
    restarted = _reconcile_with(workspace, registry, cfg, _restarter)
    assert restarted == ["demo"]
    assert restarts == ["demo"]
    assert order == []
    probe.container_id_strict.assert_called_with("demo", all_containers=True)


def test_daemon_self_heal_skips_when_container_query_fails(
    workspace, registry, config, deployed, rt, monkeypatch
) -> None:
    """Docker 查询失败不是容器缺失，必须回退轻量恢复而不是自动回滚。"""
    runtime, order, tags = rt
    _patch_observe_failed(monkeypatch)
    probe = MagicMock(name="daemon-runtime")
    probe.container_id_strict.side_effect = DockerError("daemon 不可达", instance_id="demo")
    monkeypatch.setattr(
        "local_webpage_access.docker_runtime.DockerRuntime",
        MagicMock(return_value=probe),
    )
    registry.update_last_operation(
        "demo",
        {"kind": "rebuild", "status": "failed", "buildId": deployed["b2"],
         "startedAt": "t", "source": "host_container"},
    )
    restarts: list[str] = []

    def _restarter(ws, cfg2, reg, iid):
        restarts.append(iid)

    cfg = config.model_copy(update={"selfHealPreferLastGood": True})
    restarted = _reconcile_with(workspace, registry, cfg, _restarter)
    assert restarted == ["demo"]
    assert restarts == ["demo"]
    assert order == []
