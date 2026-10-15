"""S2 残留缺口回归（BUG-833~839 / 核查 G1~G7，2026-10-15 S0-S4 completion-audit）。

归档契约测试（design/5-checkpoints/2026-10-14/S2-completion-audit/
test_cas_alias_contract_gaps.py 的 G4×2 / G5 / G6）以期望行为形式迁入默认门禁；
G1/G2/G3/G7 按同一复现逻辑补充回归。全部只依赖临时工作区与 mock。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from local_webpage_access import hosting, lifecycle, manager_api, path_alias, rebuild_txn
from local_webpage_access.config import Config, example_config_text
from local_webpage_access.errors import BuildError, DockerError, HostingError, RecognitionError
from local_webpage_access.importer import Importer
from local_webpage_access.models import InstanceManifest, Status
from local_webpage_access.registry import Registry
from local_webpage_access.scanner import _synthesize_production_start
from tests.scripts._helpers import make_container_manifest
from tests.scripts.test_hosting import _seed_frontend_instance

OLD_COMPOSE = "services:\n  app:\n    image: old\n"
NEW_COMPOSE = "services:\n  app:\n    image: new\n"


# ---- G4 / BUG-836：纯 API 别名活验证接受任意非 5xx（D7） ----------------------


@pytest.mark.parametrize("code", [401, 404])
def test_api_only_accepts_non_5xx_entry(code: int, monkeypatch) -> None:
    cfg = Config(staticGateway="caddy", staticGatewayPort=8080)
    # 与真实 _http_probe_alias_resource HTTPError 分支返回值一致。
    monkeypatch.setattr(
        path_alias,
        "_http_probe_alias_resource",
        lambda *a, **k: (False, code, "application/json", b""),
    )
    result = path_alias.verify_alias_live(
        cfg, "demo", entry_html=None, instance_id="demo", serves_ui=False
    )
    assert result.api_only


@pytest.mark.parametrize(
    "probe",
    [(False, 503, "text/html", b""), (False, None, None, b"")],
    ids=["5xx", "conn-error"],
)
def test_api_only_still_fails_on_5xx_or_conn_error(probe, monkeypatch) -> None:
    """非 5xx 放行不得扩大到 5xx / 连接失败——那是后端确实不可用。"""
    cfg = Config(staticGateway="caddy", staticGatewayPort=8080)
    monkeypatch.setattr(
        path_alias, "_http_probe_alias_resource", lambda *a, **k: probe
    )
    with pytest.raises(RecognitionError):
        path_alias.verify_alias_live(
            cfg, "demo", entry_html=None, instance_id="demo", serves_ui=False
        )


# ---- G5 / G6 / BUG-837/838：manager CAS 契约（迁移自归档契约测试） ------------


@pytest.fixture()
def manager_env(tmp_path, monkeypatch):
    from local_webpage_access.paths import Workspace

    ws = Workspace(tmp_path / "ws")
    ws.ensure_workspace_dirs()
    ws.config_path.write_text(example_config_text(), encoding="utf-8")
    cfg = Config(staticGateway="builtin")
    reg = Registry(ws.db_path)
    reg.open()
    archive = ws.inbox / "demo.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("index.html", "<h1>demo</h1>")
    result = Importer(ws, cfg, reg).import_zip(archive)
    monkeypatch.setattr(
        "local_webpage_access.status.sync_status", lambda *a, **k: None
    )
    token = manager_api.ensure_token(ws)
    app = manager_api.create_app(ws, cfg, reg, token=token)
    with TestClient(app) as client:
        yield ws, cfg, reg, result.instance_id, client, {"Authorization": f"Bearer {token}"}
    reg.close()


def test_recover_rechecks_revision_inside_lock(manager_env, monkeypatch) -> None:
    """G5 / BUG-837：recover 锁外预检通过后、获锁前被并发更新 → 409 且不重启。"""
    ws, cfg, reg, iid, client, headers = manager_env
    restarted = []

    @contextmanager
    def lock_after_concurrent_update(*args, **kwargs):
        reg.cas_increment_revision(iid, 1)
        yield

    def fake_restart(*args, **kwargs):
        restarted.append(reg.get_revision(iid))
        return InstanceManifest.load(ws.app_manifest_path(iid))

    monkeypatch.setattr(lifecycle, "instance_lock", lock_after_concurrent_update)
    monkeypatch.setattr(lifecycle, "_restart_instance_locked", fake_restart)
    response = client.post(
        f"/api/instances/{iid}/recover?expectedRevision=1", headers=headers
    )
    assert (response.status_code, restarted) == (409, []), response.text
    assert response.json()["error"]["code"] == "revision_conflict"


def test_update_rebuild_must_not_adopt_intervening_revision(manager_env, monkeypatch) -> None:
    """G6 / BUG-838：update 提交后到 restart 持锁前被插队更新 → 409 且不重启。"""
    ws, cfg, reg, iid, client, headers = manager_env
    restarted = []

    def update_then_concurrent_update(self, *args, **kwargs):
        assert kwargs["expected_revision"] == 1
        reg.cas_increment_revision(iid, 1)  # 此请求更新提交 revision=2。
        reg.cas_increment_revision(iid, 2)  # 放锁后另一请求抢先更新为 revision=3。
        return SimpleNamespace(
            needs_rebuild=False,
            needs_restart=True,
            skipped=False,
            rebuilt=True,
            prev_hash="old",
            zip_hash="new",
        )

    def fake_restart(workspace, config, registry, instance_id, **kwargs):
        lifecycle._ensure_revision_locked(registry, instance_id, kwargs.get("expected_revision"))
        restarted.append(kwargs.get("expected_revision"))
        return InstanceManifest.load(ws.app_manifest_path(iid))

    monkeypatch.setattr(Importer, "update_zip", update_then_concurrent_update)
    monkeypatch.setattr(lifecycle, "restart_instance", fake_restart)
    response = client.post(
        f"/api/instances/{iid}/update",
        headers=headers,
        json={"zipPath": "demo.zip", "expectedRevision": 1},
    )
    assert (response.status_code, restarted) == (409, []), response.text
    assert response.json()["error"]["code"] == "revision_conflict"


# ---- G7 / BUG-839：Node 生产启动路径尊重 tsconfig 显式 rootDir ---------------


def _make_ts_package(root, *, root_dir: str | None) -> None:
    package = root / "packages/server"
    (package / "src").mkdir(parents=True)
    (package / "src/index.ts").write_text("console.log(1)", encoding="utf-8")
    opts: dict[str, str] = {"outDir": "dist"}
    if root_dir is not None:
        opts["rootDir"] = root_dir
    (package / "tsconfig.json").write_text(
        json.dumps({"compilerOptions": opts}), encoding="utf-8"
    )


def test_production_start_respects_explicit_root_dir_dot(tmp_path) -> None:
    """rootDir="." 时 tsc 保留 src/ 层级：入口应为 dist/src/index.js。"""
    _make_ts_package(tmp_path, root_dir=".")
    result = _synthesize_production_start(
        tmp_path, "packages/server", {"dev": "tsx src/index.ts"}
    )
    assert result == "node packages/server/dist/src/index.js"


def test_production_start_root_dir_src_strips_prefix(tmp_path) -> None:
    _make_ts_package(tmp_path, root_dir="src")
    result = _synthesize_production_start(
        tmp_path, "packages/server", {"dev": "tsx src/index.ts"}
    )
    assert result == "node packages/server/dist/index.js"


def test_production_start_without_root_dir_keeps_legacy(tmp_path) -> None:
    """未声明 rootDir：维持既有启发（src/ 前缀映射到 outDir 根）。"""
    _make_ts_package(tmp_path, root_dir=None)
    result = _synthesize_production_start(
        tmp_path, "packages/server", {"dev": "tsx src/index.ts"}
    )
    assert result == "node packages/server/dist/index.js"


# ---- G1 / BUG-833：前端重建失败不把仍在服务的实例标 FAILED -------------------


def test_frontend_rebuild_failure_keeps_serving_state(
    workspace, registry, config, monkeypatch
) -> None:
    _seed_frontend_instance(workspace, registry, "spa")
    manifest = hosting._load_manifest(workspace, "spa")
    manifest.status = Status.RUNNING
    manifest.save(workspace.app_manifest_path("spa"))
    registry.upsert_from_manifest(manifest)
    registry.update_status("spa", Status.RUNNING.value)
    public = workspace.app_public("spa")
    public.mkdir(parents=True, exist_ok=True)
    (public / "index.html").write_text("<h1>old</h1>", encoding="utf-8")

    def fake_run(cmd, *, cwd, log_path, **kw):
        raise BuildError("npm run build 编译失败", command=cmd, exit_code=1)

    monkeypatch.setattr("local_webpage_access.hosting.run_command", fake_run)

    with pytest.raises(BuildError):
        hosting.build_and_host_frontend(workspace, config, registry, "spa")

    # 实例状态保留观测值 running（先经历 BUILDING 后被恢复），不再误标 failed
    row = registry.get_instance("spa")
    assert row["status"] == Status.RUNNING.value
    op = json.loads(row["last_operation_json"])
    assert op["status"] == "failed"
    assert op["kind"] == "deploy"
    # manifest 同步操作态与 lastError；旧产物原样保留
    saved = hosting._load_manifest(workspace, "spa")
    assert saved.status == Status.RUNNING
    assert saved.lastError
    assert (public / "index.html").read_text(encoding="utf-8") == "<h1>old</h1>"
    assert any(
        "旧版本仍在服务" in (e.get("message") or "")
        for e in registry.list_events("spa")
        if e["event_type"] == "error"
    )


# ---- G2 / G3 / BUG-834/835：重建事务的补偿边界与快照新鲜度 --------------------


def test_frontend_sync_failure_does_not_claim_old_version_is_serving(
    workspace, registry, config, monkeypatch
) -> None:
    """复制中途失败、public 已清空时，必须如实报告部署失败。"""
    _seed_frontend_instance(workspace, registry, "spa")
    manifest = hosting._load_manifest(workspace, "spa")
    manifest.status = Status.RUNNING
    manifest.save(workspace.app_manifest_path("spa"))
    registry.upsert_from_manifest(manifest)
    public = workspace.app_public("spa")
    public.mkdir(parents=True, exist_ok=True)
    (public / "index.html").write_text("old", encoding="utf-8")
    dist = workspace.app_current("spa") / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("new", encoding="utf-8")
    monkeypatch.setattr(hosting, "run_command", lambda *a, **k: None)

    def fail_copy(*a, **k):
        raise OSError("disk full during sync")

    monkeypatch.setattr(hosting, "_copy_item", fail_copy)
    with pytest.raises(OSError, match="disk full during sync"):
        hosting.build_and_host_frontend(workspace, config, registry, "spa")

    assert not list(public.iterdir())
    assert registry.get_instance("spa")["status"] == Status.FAILED.value
    assert hosting._load_manifest(workspace, "spa").status == Status.FAILED
    assert not any(
        "旧版本仍在服务" in (event.get("message") or "")
        for event in registry.list_events("spa")
    )


@pytest.fixture()
def deployed(workspace, registry):
    workspace.ensure_app_dirs("demo")
    manifest = make_container_manifest("demo", status=Status.RUNNING)
    manifest.appPath = str(workspace.app_current("demo"))
    manifest.container.containerId = "old-container"
    manifest.container.imageId = "old-image"
    manifest.container.hostPort = 21001
    manifest.container.composePath = str(workspace.app_compose_path("demo"))
    manifest.container.dockerfilePath = str(workspace.app_dockerfile_path("demo"))
    manifest.save(workspace.app_manifest_path("demo"))
    registry.upsert_from_manifest(manifest)
    workspace.app_docker("demo").mkdir(parents=True, exist_ok=True)
    workspace.app_compose_path("demo").write_text(OLD_COMPOSE, encoding="utf-8")
    return manifest


@pytest.fixture()
def deploy_env(workspace, registry, config, deployed, monkeypatch):
    order: list[str] = []
    tags: list[tuple[str, str]] = []
    rt = MagicMock(name="runtime")
    rt.is_running.return_value = True
    rt.image_id.return_value = "old-image"
    rt.container_id.return_value = "new-container"

    def _rec(name, *, fail: bool = False, msg: str = ""):
        def _call(*a, **k):
            order.append(name)
            if fail:
                raise DockerError(msg or f"模拟 {name} 失败", instance_id="demo", action=name)

        return _call

    rt.build.side_effect = _rec("build")
    rt.down.side_effect = _rec("down")
    rt.up.side_effect = _rec("up")

    def _tag(src, dst):
        tags.append((src, dst))

    monkeypatch.setattr(hosting, "DockerRuntime", MagicMock(return_value=rt))
    monkeypatch.setattr(rebuild_txn, "tag_image", _tag)
    monkeypatch.setattr(
        "local_webpage_access.container_identity.ensure_non_root_identity_ready",
        lambda *a: None,
    )
    monkeypatch.setattr(
        "local_webpage_access.versions.resolve_repo_image_id", lambda repo: None
    )
    monkeypatch.setattr(
        hosting,
        "_rescue_container_data_before_rebuild",
        lambda *a, **k: order.append("rescue"),
    )

    def _write_new_compose(*a, **k):
        workspace.app_compose_path("demo").write_text(NEW_COMPOSE, encoding="utf-8")

    monkeypatch.setattr(hosting, "generate_dockerfile", lambda *a, **k: None)
    monkeypatch.setattr(hosting, "generate_compose", _write_new_compose)
    monkeypatch.setattr(hosting, "generate_env", lambda *a, **k: None)
    monkeypatch.setattr(hosting, "_ensure_container_port", lambda *a, **k: (21001, False))
    monkeypatch.setattr(hosting, "_wait_for_http", lambda *a, **k: True)
    monkeypatch.setattr(
        hosting,
        "_evaluate_container_verification",
        lambda *a, **k: {
            "overall_status": "passed",
            "liveness_passed": True,
            "mandatory_all_passed": True,
            "optional_warnings": [],
            "probe_notes": [],
            "observed_capabilities": ["ui"],
        },
    )
    return order, rt, tags


def test_verifying_commit_failure_enters_compensation(
    workspace, registry, config, deployed, deploy_env, monkeypatch
) -> None:
    """G2 / BUG-834：up 后 VERIFYING 落盘失败必须补偿回旧版本，不得裸抛。"""

    def _boom(ctx):
        raise OSError("VERIFYING 状态写盘失败（模拟磁盘错误）")

    monkeypatch.setattr(hosting, "_commit_verifying_state", _boom)
    order, rt, _ = deploy_env

    with pytest.raises(HostingError, match="已回滚到旧版本并恢复服务"):
        hosting.host_container(workspace, config, registry, "demo")

    # swap 的 down/up 之后进入补偿：down 新容器 → 恢复 .prev → up 旧版本
    assert order == ["build", "rescue", "down", "up", "down", "up"]
    assert workspace.app_compose_path("demo").read_text(encoding="utf-8") == OLD_COMPOSE
    row = registry.get_instance("demo")
    assert row["status"] == Status.RUNNING.value
    op = json.loads(row["last_operation_json"])
    assert op["status"] == "failed" and op["rollback"] == "restored"


def test_tag_failure_does_not_restore_stale_prev(
    workspace, registry, config, deployed, deploy_env, monkeypatch
) -> None:
    """G3 / BUG-835：保旧镜像 tag 失败时不得把上一轮陈旧 .prev 盖回当前 compose。"""
    order, rt, _ = deploy_env
    prev = workspace.app_docker("demo") / ".prev"
    prev.mkdir(parents=True, exist_ok=True)
    (prev / "compose.yaml").write_text("STALE PREVIOUS COMPOSE", encoding="utf-8")

    def _fail_tag(src, dst):
        raise DockerError("docker tag 失败（模拟）", instance_id="demo")

    monkeypatch.setattr(rebuild_txn, "tag_image", _fail_tag)

    with pytest.raises(HostingError, match="无法保留旧镜像"):
        hosting.host_container(workspace, config, registry, "demo")

    # 中止于 prepare：build/down/up 均未执行，旧容器未被动
    assert order == []
    assert workspace.app_compose_path("demo").read_text(encoding="utf-8") == OLD_COMPOSE
    # 快照先于 tag：.prev 已刷新为当前（旧）生成物，而非上一轮陈旧内容
    assert (prev / "compose.yaml").read_text(encoding="utf-8") == OLD_COMPOSE
    assert registry.get_instance("demo")["status"] == Status.RUNNING.value


def test_snapshot_failure_discards_stale_prev(workspace, monkeypatch) -> None:
    """G3 / BUG-835：快照中途失败须丢弃（含上一轮陈旧）.prev，恢复按缺失处理。"""
    docker_dir = workspace.app_docker("demo")
    docker_dir.mkdir(parents=True, exist_ok=True)
    (docker_dir / "compose.yaml").write_text("current", encoding="utf-8")
    prev = docker_dir / ".prev"
    prev.mkdir()
    (prev / "compose.yaml").write_text("stale", encoding="utf-8")

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("local_webpage_access.rebuild_txn.shutil.copy2", _boom)
    with pytest.raises(OSError, match="disk full"):
        rebuild_txn.snapshot_generated(workspace, "demo")

    assert not prev.exists()
    assert (docker_dir / "compose.yaml").read_text(encoding="utf-8") == "current"
    assert rebuild_txn.restore_generated(workspace, "demo") is False


@pytest.mark.parametrize("failure_at", ["mkdir", "rename"])
def test_snapshot_preparation_failure_keeps_current_compose(
    workspace, registry, config, deployed, deploy_env, monkeypatch, failure_at
) -> None:
    """快照目录创建或提交失败，完整部署收尾不得恢复上一轮陈旧配置。"""
    prev = rebuild_txn.prev_dir(workspace, "demo")
    prev.mkdir()
    (prev / "compose.yaml").write_text("stale", encoding="utf-8")
    original = getattr(Path, failure_at)

    def fail_staging(path, *args, **kwargs):
        if path.name.startswith(".prev.staging-"):
            raise OSError("snapshot preparation failed")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, failure_at, fail_staging)
    with pytest.raises(OSError, match="snapshot preparation failed"):
        hosting.host_container(workspace, config, registry, "demo")

    assert workspace.app_compose_path("demo").read_text(encoding="utf-8") == OLD_COMPOSE
    assert not prev.exists()
    assert not list(prev.parent.glob(".prev.staging-*"))
    assert registry.get_instance("demo")["status"] == Status.RUNNING.value
    assert deploy_env[0] == []
