"""S2-02 操作态与实例态分离回归（BUG-750 / issue #57-2、BUG-759 / N6，决策 D4）。

翻转 design/5-checkpoints/2026-10-11-issues47-60-repro.py::test_57_queue_status_only_updates_registry：
排队/超时/构建失败只写操作态 lastOperation 与 lastError，实例 status 仅由观测决定。
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from local_webpage_access import hosting
from local_webpage_access.build_queue import BuildQueue, BuildTask
from local_webpage_access.errors import LifecycleError
from local_webpage_access.models import InstanceManifest, Status
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry

from tests.scripts._helpers import make_container_manifest


def _seed_running_instance(ws: Workspace, reg: Registry, iid: str = "demo") -> None:
    """登记一个「旧容器仍在服务」的运行中实例（manifest + registry 一致）。"""
    ws.ensure_app_dirs(iid)
    manifest = make_container_manifest(iid, status=Status.RUNNING)
    manifest.appPath = str(ws.app_current(iid))
    manifest.container.containerId = "old-container"
    manifest.container.imageId = "old-image"
    manifest.container.hostPort = 21001
    manifest.container.composePath = str(ws.app_compose_path(iid))
    manifest.container.dockerfilePath = str(ws.app_dockerfile_path(iid))
    manifest.save(ws.app_manifest_path(iid))
    reg.upsert_from_manifest(manifest)


def _last_operation(reg: Registry, iid: str) -> dict:
    row = reg.get_instance(iid)
    raw = row.get("last_operation_json")
    return json.loads(raw) if raw else {}


# ---- 翻转：#57-2 -------------------------------------------------------------


def test_57_queue_status_only_updates_registry(workspace, registry) -> None:
    """排队与排队超时只写操作态：实例 status 与 manifest 均不被改写。"""
    _seed_running_instance(workspace, registry, "demo")
    q = object.__new__(BuildQueue)
    q.registry, q._guard = registry, threading.Lock()
    q._persist_task = lambda *a, **k: None
    task = BuildTask(instance_id="demo")

    q._mark_queued("demo", task)
    row = registry.get_instance("demo")
    # S2-02 / D4：status 保持观测值 running，操作态记录 queued
    assert row["status"] == Status.RUNNING.value
    op = _last_operation(registry, "demo")
    assert op["status"] == "queued"
    assert op["kind"] == "build"
    assert op["buildId"] == task.build_token
    assert InstanceManifest.load(workspace.app_manifest_path("demo")).status == Status.RUNNING

    q._mark_timeout("demo", task, 0.1)
    row = registry.get_instance("demo")
    # 排队超时（BUG-759 / N6）：仍在服务的实例不得被标 FAILED
    assert row["status"] == Status.RUNNING.value
    assert row["last_error"] and "排队超时" in row["last_error"]
    op = _last_operation(registry, "demo")
    assert op["status"] == "failed"
    assert "排队超时" in op["error"]
    assert op["finishedAt"]
    assert InstanceManifest.load(workspace.app_manifest_path("demo")).status == Status.RUNNING


def test_queue_timeout_keeps_running_instance_serving(workspace, registry, config) -> None:
    """排队超时后状态仍为 running 且 lastOperation=failed（S2-02 新增验收用例）。"""
    _seed_running_instance(workspace, registry, "api")
    _seed_running_instance(workspace, registry, "api2")
    q = BuildQueue(config, registry, concurrency=1)

    release = threading.Event()

    def blocking_builder(iid):
        release.wait(2.0)
        return iid

    def queued_builder(iid):
        return iid

    t1 = threading.Thread(target=q.run, args=("api", blocking_builder))
    t1.start()
    time.sleep(0.05)  # 让 api 抢到槽位

    with pytest.raises(LifecycleError, match="排队超时"):
        q.run("api2", queued_builder, wait_timeout=0.2)

    release.set()
    t1.join()

    row = registry.get_instance("api2")
    assert row["status"] == Status.RUNNING.value  # 观测态未被操作污染
    op = _last_operation(registry, "api2")
    assert op["status"] == "failed"
    assert op["kind"] == "build"
    assert "排队超时" in op["error"]
    # manifest 侧同样保持 running（不被排队超时改写）
    assert InstanceManifest.load(workspace.app_manifest_path("api2")).status == Status.RUNNING


# ---- registry 序列化 ---------------------------------------------------------


def test_update_last_operation_roundtrip(registry) -> None:
    """update_last_operation 写入/读取/清空；updated_at 不被 status 污染。"""
    from local_webpage_access.logging import now_iso

    registry.upsert_instance(
        {
            "id": "demo",
            "name": "demo",
            "version": "1",
            "kind": "python",
            "runtime": "docker-compose",
            "serving_mode": "container",
            "status": "running",
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
    )
    op = {
        "kind": "rebuild",
        "status": "failed",
        "buildId": 7,
        "startedAt": "2026-10-13T10:00:00+08:00",
        "finishedAt": "2026-10-13T10:05:00+08:00",
        "error": "boom",
        "source": "host_container",
    }
    registry.update_last_operation("demo", op)
    row = registry.get_instance("demo")
    assert row["status"] == "running"  # 写操作态不动实例 status
    assert json.loads(row["last_operation_json"]) == op
    # 清空
    registry.update_last_operation("demo", None)
    assert registry.get_instance("demo")["last_operation_json"] is None


def test_upsert_from_manifest_syncs_last_operation(workspace, registry) -> None:
    """manifest.lastOperation 经 upsert_from_manifest 同步进 registry 列。"""
    _seed_running_instance(workspace, registry, "demo")
    manifest = InstanceManifest.load(workspace.app_manifest_path("demo"))
    manifest.lastOperation = {
        "kind": "rebuild",
        "status": "failed",
        "buildId": 3,
        "finishedAt": "2026-10-13T11:00:00+08:00",
        "error": "构建炸了",
        "source": "host_container",
    }
    manifest.save(workspace.app_manifest_path("demo"))
    registry.upsert_from_manifest(manifest)
    assert _last_operation(registry, "demo") == manifest.lastOperation


# ---- 快照 / API / CLI 输出 ----------------------------------------------------


def test_instance_status_snapshot_exposes_last_operation(workspace, registry, config, monkeypatch) -> None:
    """status.py 快照与 to_dict() 暴露 lastOperation（manager API 经 to_dict 输出）。"""
    from local_webpage_access import status as status_mod

    _seed_running_instance(workspace, registry, "demo")
    op = {
        "kind": "rebuild",
        "status": "failed",
        "buildId": 9,
        "finishedAt": "2026-10-13T12:00:00+08:00",
        "error": "npm run build 失败",
        "source": "host_container",
    }
    registry.update_last_operation("demo", op)
    monkeypatch.setattr(
        status_mod,
        "_resolve_network_urls",
        lambda *a, **k: dict(
            lan_url=None,
            route_host=None,
            route_url=None,
            current_lan_ip=None,
            persisted_lan_ip=None,
            lan_address_stale=False,
            lan_url_source="test",
        ),
    )
    snap = status_mod.instance_status(workspace, config, registry, "demo")
    assert snap.status == Status.RUNNING.value  # 观测态独立
    assert snap.last_operation == op
    assert snap.to_dict()["lastOperation"] == op


def test_operation_note_cli_rendering() -> None:
    """CLI status 的「最近操作」行：failed/cancelled 渲染，succeeded/缺失不渲染。"""
    from local_webpage_access.cli.status import operation_note

    failed = SimpleNamespace(
        last_operation={
            "kind": "rebuild",
            "status": "failed",
            "finishedAt": "2026-10-13T12:00:00+08:00",
            "error": "npm run build 失败",
        }
    )
    assert operation_note(failed) == "最近一次重建失败（2026-10-13T12:00:00+08:00）：npm run build 失败"

    cancelled = SimpleNamespace(
        last_operation={"kind": "build", "status": "cancelled", "error": "构建已取消"}
    )
    assert operation_note(cancelled) == "最近一次构建已取消：构建已取消"

    assert operation_note(SimpleNamespace(last_operation={"kind": "build", "status": "succeeded"})) is None
    assert operation_note(SimpleNamespace(last_operation=None)) is None


# ---- hosting 操作态 ----------------------------------------------------------


def test_mark_failed_records_last_operation(workspace, registry) -> None:
    """hosting._mark_failed：failed 仍是诚实观测（未在服务），同时记录操作态。"""
    _seed_running_instance(workspace, registry, "demo")
    manifest = InstanceManifest.load(workspace.app_manifest_path("demo"))
    hosting._mark_failed(
        workspace,
        registry,
        "demo",
        manifest,
        RuntimeError("npm run build 失败"),
        operation_kind="rebuild",
        operation_source="host_container",
        build_id=11,
    )
    row = registry.get_instance("demo")
    assert row["status"] == Status.FAILED.value
    assert row["last_error"]
    op = _last_operation(registry, "demo")
    assert op["kind"] == "rebuild"
    assert op["status"] == "failed"
    assert op["buildId"] == 11
    assert op["source"] == "host_container"
    # manifest 镜像同步
    reloaded = InstanceManifest.load(workspace.app_manifest_path("demo"))
    assert reloaded.lastOperation["status"] == "failed"
    assert reloaded.status == Status.FAILED


def test_host_container_cancel_keeps_observed_status(
    workspace, registry, config, monkeypatch
) -> None:
    """BuildCancelled 是操作态：实例 status 保留观测值，不写 CANCELLED。"""
    from local_webpage_access.errors import BuildCancelled

    _seed_running_instance(workspace, registry, "demo")

    class _Runtime:
        def __init__(self, *a, **k):
            pass

        @staticmethod
        def ensure_available() -> None:  # pragma: no cover - 占位
            return None

        @staticmethod
        def rescue_container_data(*a, **k) -> int:
            return 0

        def build(self, *a, **k):
            raise BuildCancelled("构建已取消")

        def up(self, *a, **k) -> None:
            return None

    monkeypatch.setattr(hosting, "DockerRuntime", _Runtime)
    # 注意：该函数在 host_container 内做局部 import，须补丁来源模块
    monkeypatch.setattr(
        "local_webpage_access.container_identity.ensure_non_root_identity_ready",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(hosting, "_rescue_container_data_before_rebuild", lambda *a, **k: None)
    monkeypatch.setattr(hosting, "stop_or_abort", lambda *a, **k: None)
    monkeypatch.setattr(hosting, "generate_dockerfile", lambda *a, **k: None)
    monkeypatch.setattr(hosting, "_ensure_container_port", lambda *a, **k: (21001, False))
    monkeypatch.setattr(hosting, "generate_compose", lambda *a, **k: None)
    monkeypatch.setattr(hosting, "generate_env", lambda *a, **k: None)

    with pytest.raises(BuildCancelled):
        hosting.host_container(workspace, config, registry, "demo")

    row = registry.get_instance("demo")
    # 取消是操作态：实例 status 保留操作前观测值（build 进行中），不写 CANCELLED
    assert row["status"] == Status.BUILDING.value
    assert row["status"] != Status.CANCELLED.value
    op = _last_operation(registry, "demo")
    assert op["status"] == "cancelled"
    assert op["kind"] == "rebuild"  # 有旧容器 → 重建
