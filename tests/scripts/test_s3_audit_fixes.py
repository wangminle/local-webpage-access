"""S3 完成度审查第二轮（CHK-389）缺陷回归。

- BUG-798（S3-02，P1）：孤儿片段漂移写事件违反 events 外键且签名提前缓存，
  同组漂移永久静默——巡检对未登记实例的事件须落 instance_id=NULL；
- BUG-800（S3-01，P2）：GC「上一可用版本永不删除」承诺未落实（success_rows[0]
  即当前构建），且 in_use 带 sha256: 前缀时保护分支失效；
- BUG-801（S3-01，P3）：版本面板 inUse 未归一化镜像 ID 前缀，恒 False；
- BUG-802（S3-01，P3）：GC 后 builds 行产物列残留，重复 rm / 逐行 inspect 随
  历史线性增长。
"""

from __future__ import annotations

import pytest

from local_webpage_access import versions
from local_webpage_access.config import Config
from local_webpage_access.consistency import (
    CONSISTENCY_EVENT_TYPE,
    run_consistency_patrol,
)
from local_webpage_access.models import Status
from tests.scripts._helpers import make_container_manifest

# ---- BUG-798：孤儿片段巡检 ------------------------------------------------------


def test_patrol_with_orphan_fragment_records_event_without_fk_crash(
    workspace, registry
) -> None:
    """孤儿片段（实例未登记）不触发外键异常；事件落 NULL 实例且按签名去重。"""
    config = Config(staticGateway="builtin")
    ghost = workspace.static_sites / "ghost.conf"
    ghost.parent.mkdir(parents=True, exist_ok=True)
    ghost.write_text("# ghost\n", encoding="utf-8")

    # 旧代码在第一次调用即抛 FOREIGN KEY constraint failed
    run_consistency_patrol(workspace, config, registry)
    run_consistency_patrol(workspace, config, registry)  # 同组漂移去重

    events = [
        e
        for e in registry.list_events(None, limit=50)
        if e["event_type"] == CONSISTENCY_EVENT_TYPE
    ]
    assert len(events) == 1
    assert events[0]["instance_id"] is None  # 未登记实例 → 全局事件
    assert "ghost.conf" in events[0]["message"]


# ---- 共用夹具 -------------------------------------------------------------------


@pytest.fixture()
def seeded(workspace, registry):
    """登记容器实例（manifest imageId 用生产格式：带 sha256: 前缀）。"""
    workspace.ensure_app_dirs("demo")
    manifest = make_container_manifest("demo", status=Status.RUNNING)
    manifest.container.imageId = "sha256:img-10"
    manifest.save(workspace.app_manifest_path("demo"))
    registry.upsert_from_manifest(manifest)
    return manifest


def _mk_build(registry, image_id: str, *, status: str = "success") -> int:
    bid = registry.add_build("demo", status="running")
    registry.finish_build(bid, status=status)
    registry.record_build_artifacts(
        bid, image_ref=f"lwa-demo-app:b{bid}", image_id=image_id,
        source_hash=None, config_snapshot=None,
    )
    return bid


# ---- BUG-800：GC 上一可用版本保护 + 镜像 ID 归一化 --------------------------------


def test_gc_protects_previous_good_at_retention_one(workspace, registry, seeded,
                                                    monkeypatch) -> None:
    """imageRetention=1 时上一可用版本仍受保护；in_use 带前缀不影响判定。"""
    config = Config(staticGateway="builtin", imageRetention=1)
    b9 = _mk_build(registry, "img-9")  # 旧：上一可用版本
    b10 = _mk_build(registry, "img-10")  # 新：当前构建（在用）
    rm_calls: list[str] = []
    monkeypatch.setattr(versions, "_image_rm", lambda ref: rm_calls.append(ref))
    monkeypatch.setattr(versions, "image_exists", lambda ref: False)

    # 生产格式：_gc_after_deploy 传入 manifest.container.imageId（带 sha256: 前缀）
    result = versions.gc_build_artifacts(
        workspace, config, registry, "demo", in_use_image_id="sha256:img-10"
    )

    assert result["removed"] == []
    assert result["kept"] == [b10, b9]
    assert rm_calls == []


def test_gc_previous_good_skips_in_use_image(workspace, registry, seeded,
                                              monkeypatch) -> None:
    """in-use 是旁支镜像（不在构建行）时，上一可用=最新成功构建，仍受保护。"""
    config = Config(staticGateway="builtin", imageRetention=1)
    b10 = _mk_build(registry, "img-10")
    rm_calls: list[str] = []
    monkeypatch.setattr(versions, "_image_rm", lambda ref: rm_calls.append(ref))
    monkeypatch.setattr(versions, "image_exists", lambda ref: False)

    result = versions.gc_build_artifacts(
        workspace, config, registry, "demo", in_use_image_id="sha256:img-side"
    )

    assert result["removed"] == []
    assert result["kept"] == [b10]
    assert rm_calls == []


# ---- BUG-802：GC 后清空产物列 ----------------------------------------------------


def test_gc_clears_artifact_columns_for_removed_rows(workspace, registry, seeded,
                                                      monkeypatch) -> None:
    """镜像确认不在后置空产物列：旧行不再参与 GC 重试与版本列表；rm 失败时保留重试。"""
    config = Config(staticGateway="builtin", imageRetention=2)
    b1 = _mk_build(registry, "img-1")
    b2 = _mk_build(registry, "img-2")
    _mk_build(registry, "img-3")  # 最新构建，在用
    rm_calls: list[str] = []
    monkeypatch.setattr(versions, "_image_rm", lambda ref: rm_calls.append(ref))
    # 第一轮：rm 后镜像仍在（模拟 image rm 失败）→ 行保留，下轮重试
    monkeypatch.setattr(versions, "image_inspect_status", lambda ref: "present")

    first = versions.gc_build_artifacts(
        workspace, config, registry, "demo", in_use_image_id="img-3"
    )
    assert first["removed"] == [b1]
    rows = {r["id"]: r for r in registry.list_builds("demo")}
    assert rows[b1]["image_ref"] == f"lwa-demo-app:b{b1}"  # 未清空，等待重试

    # 第二轮：镜像已不在 → 置空产物列
    monkeypatch.setattr(versions, "image_inspect_status", lambda ref: "missing")
    second = versions.gc_build_artifacts(
        workspace, config, registry, "demo", in_use_image_id="img-3"
    )
    assert second["removed"] == [b1]
    assert rm_calls == [f"lwa-demo-app:b{b1}", f"lwa-demo-app:b{b1}"]  # 重试过一次
    rows = {r["id"]: r for r in registry.list_builds("demo")}
    assert rows[b1]["image_ref"] is None
    assert rows[b1]["image_id"] is None
    assert rows[b1]["config_snapshot"] is None
    assert rows[b2]["image_ref"] == f"lwa-demo-app:b{b2}"  # 保留行不动（上一可用）

    # 第三轮：已清行不再参与 GC
    third = versions.gc_build_artifacts(
        workspace, config, registry, "demo", in_use_image_id="img-3"
    )
    assert third["removed"] == []
    assert rm_calls == [f"lwa-demo-app:b{b1}", f"lwa-demo-app:b{b1}"]


# ---- BUG-801：版本面板 inUse 归一化 ----------------------------------------------


def test_versions_payload_inuse_normalizes_sha256_prefix(workspace, registry, seeded,
                                                          monkeypatch) -> None:
    """containers 行（带前缀）vs builds 行（去前缀）比较须归一化，「在用」正确显示。"""
    from local_webpage_access.manager_history_routes import versions_payload

    monkeypatch.setattr(
        "local_webpage_access.versions.list_versions",
        lambda reg, iid: [
            {"id": 3, "imageId": "img-10", "imagePresent": True, "status": "success"},
            {"id": 2, "imageId": "img-9", "imagePresent": True, "status": "success"},
        ],
    )
    monkeypatch.setattr(
        "local_webpage_access.versions.retained_disk_usage_bytes",
        lambda reg, iid: (2, 4096),
    )

    payload = versions_payload(workspace, None, registry, "demo")

    assert payload["versions"][0]["inUse"] is True
    assert payload["versions"][1]["inUse"] is False
