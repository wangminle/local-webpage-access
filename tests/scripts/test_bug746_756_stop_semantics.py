"""BUG-746 / BUG-756（issue #54 / N3，WBS S2-01）：统一停机语义 stop_or_abort。

复现翻转来源（design/5-checkpoints/ 原脚本保持不动，断言缺陷存在）：
- 2026-10-11/issues47-60-independent-repro.py::test_n3
  原断言「restart 停机失败后仍误报已重启」；此处翻转：停机失败直接报错中止。
- 2026-10-11/issues47-60-repro.py::test_51_54[down_fails]
  原断言「重建 down 失败后仍继续 build 且 manifest 身份被清空」；此处翻转：
  down 失败即中止，build 不再执行，身份保持完整。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from local_webpage_access import hosting, lifecycle, path_alias, workspace_migrate
from local_webpage_access.errors import DockerError, HostingError
from local_webpage_access.models import Status
from tests.scripts._helpers import make_container_manifest


@pytest.fixture()
def deployed_container(workspace, registry):
    """已部署且在跑的容器实例（containerId/imageId 已落库）。"""
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
    return manifest


# ---- stop_or_abort 单元语义 -------------------------------------------------


def test_stop_or_abort_not_running_is_success() -> None:
    """「本来就没在跑」视为成功：不执行停机命令。"""
    rt = MagicMock()
    rt.is_running.return_value = False
    hosting.stop_or_abort(rt, "demo", purpose="重建")
    rt.down.assert_not_called()
    rt.stop.assert_not_called()


def test_stop_or_abort_down_failure_raises_with_purpose() -> None:
    """停机失败抛 HostingError，文案写明「实例仍在运行，<purpose> 已中止」。"""
    rt = MagicMock()
    rt.is_running.return_value = True
    rt.down.side_effect = DockerError("compose down failed", instance_id="demo", action="down")
    with pytest.raises(HostingError, match="实例仍在运行，重建 已中止"):
        hosting.stop_or_abort(rt, "demo", purpose="重建")


def test_stop_or_abort_stop_variant_used_when_down_false() -> None:
    rt = MagicMock()
    rt.is_running.return_value = True
    hosting.stop_or_abort(rt, "demo", purpose="重启", down=False)
    rt.stop.assert_called_once_with("demo")
    rt.down.assert_not_called()


def test_stop_or_abort_query_failure_aborts() -> None:
    """运行状态查询失败不得当作「没在跑」——fail-safe 中止（BUG-429 语义）。"""
    rt = MagicMock()
    rt.is_running.side_effect = DockerError("docker daemon 不可达", instance_id="demo")
    with pytest.raises(HostingError, match="运行状态无法确认，重建 已中止"):
        hosting.stop_or_abort(rt, "demo", purpose="重建")
    rt.down.assert_not_called()


def test_stop_or_abort_only_if_running_false_attempts_when_stopped() -> None:
    """漂移修复/迁移：已停止但仍存在的旧容器也必须执行 down。"""
    rt = MagicMock()
    rt.is_running.return_value = False
    hosting.stop_or_abort(rt, "demo", purpose="迁移", only_if_running=False)
    rt.down.assert_called_once_with("demo")


# ---- test_51_54[down_fails] 翻转：重建停机失败即中止且身份完整 ---------------


def test_rebuild_down_failure_aborts_and_preserves_identity(
    workspace, registry, config, deployed_container, monkeypatch
) -> None:
    """BUG-746/#54：重建 down 失败 → HostingError 中止，不 build，manifest 身份不被清空。"""
    rt = MagicMock()
    order: list[str] = []
    rt.is_running.return_value = True

    def down(*args, **kwargs):
        order.append("down")
        raise DockerError("模拟 down 失败", instance_id="demo", action="down")

    def build(*args, **kwargs):
        order.append("build")
        raise DockerError("模拟 build 失败", instance_id="demo")

    rt.down.side_effect = down
    rt.build.side_effect = build
    monkeypatch.setattr(hosting, "DockerRuntime", MagicMock(return_value=rt))
    monkeypatch.setattr(
        "local_webpage_access.container_identity.ensure_non_root_identity_ready", lambda *a: None
    )
    monkeypatch.setattr(hosting, "_rescue_container_data_before_rebuild", lambda *a: None)
    monkeypatch.setattr(hosting, "generate_dockerfile", lambda *a, **k: None)
    monkeypatch.setattr(hosting, "generate_compose", lambda *a, **k: None)
    monkeypatch.setattr(hosting, "generate_env", lambda *a, **k: None)
    monkeypatch.setattr(hosting, "_ensure_container_port", lambda *a, **k: (21001, False))

    # S2-03：本组用例针对「先停后建」的停机语义，显式选用 stop-first 策略
    deployed_container.container.rebuildStrategy = "stop-first"
    deployed_container.save(workspace.app_manifest_path("demo"))
    with pytest.raises(HostingError, match="实例仍在运行，重建 已中止"):
        hosting.host_container(workspace, config, registry, "demo")

    assert order == ["down"]
    rt.build.assert_not_called()
    rt.up.assert_not_called()
    saved = hosting._load_manifest(workspace, "demo")
    assert saved.container is not None
    assert saved.container.containerId == "old-container"
    assert saved.container.imageId == "old-image"
    # 停机中止不算构建失败：实例保持操作前观测状态，不被标 FAILED
    assert registry.get_instance("demo")["status"] == Status.RUNNING.value


def test_rebuild_down_failure_finishes_build_row(
    workspace, registry, config, deployed_container, monkeypatch
) -> None:
    """停机中止时本轮 build 行如实收尾为 failed，不留 running 残行。"""
    rt = MagicMock()
    rt.is_running.return_value = True
    rt.down.side_effect = DockerError("模拟 down 失败", instance_id="demo", action="down")
    monkeypatch.setattr(hosting, "DockerRuntime", MagicMock(return_value=rt))
    monkeypatch.setattr(
        "local_webpage_access.container_identity.ensure_non_root_identity_ready", lambda *a: None
    )
    monkeypatch.setattr(hosting, "_rescue_container_data_before_rebuild", lambda *a: None)

    # S2-03：本组用例针对「先停后建」的停机语义，显式选用 stop-first 策略
    deployed_container.container.rebuildStrategy = "stop-first"
    deployed_container.save(workspace.app_manifest_path("demo"))
    with pytest.raises(HostingError, match="实例仍在运行，重建 已中止"):
        hosting.host_container(workspace, config, registry, "demo")

    latest = registry.list_builds("demo", limit=1)
    assert latest and latest[0]["status"] == "failed"


# ---- test_n3 翻转：restart 停机失败直接报错，不再误报「已重启」 ---------------


def test_restart_stop_failure_aborts_without_start(
    workspace, registry, config, deployed_container, monkeypatch
) -> None:
    """BUG-756/N3：停机失败直接报错中止，start 不再执行。"""
    start = MagicMock(return_value=deployed_container)
    # 本用例断言停机失败中止，不测 Docker 版本。完整重建会先走
    # DockerRuntime.ensure_available()；runner 上的 Engine 28 会在停机语义之前
    # 抛出版本错误。
    monkeypatch.setattr(
        hosting.DockerRuntime,
        "ensure_available",
        classmethod(lambda cls: None),
    )
    monkeypatch.setattr(hosting, "stop_or_abort", MagicMock(side_effect=HostingError("停机失败")))
    monkeypatch.setattr(hosting, "start_container", start)
    monkeypatch.setattr(lifecycle, "_sync_alias_port", lambda *a, **k: None)
    monkeypatch.setattr(path_alias, "maybe_verify_alias_after_start", lambda *a, **k: False)

    with pytest.raises(HostingError, match="停机失败"):
        lifecycle.restart_instance(workspace, config, registry, "demo")
    start.assert_not_called()


def test_restart_not_running_still_restarts(
    workspace, registry, config, deployed_container, monkeypatch
) -> None:
    """「本来就没在跑」视为成功：停机命令不执行，restart 正常走轻量 start。"""
    # S2-04：已部署实例的生成物在盘上存在，否则 restart 会判为完整重建
    workspace.app_docker("demo").mkdir(parents=True, exist_ok=True)
    workspace.app_compose_path("demo").write_text("services: {}\n", encoding="utf-8")
    workspace.app_dockerfile_path("demo").write_text("FROM scratch\n", encoding="utf-8")
    rt = MagicMock()
    rt.is_running.return_value = False
    start = MagicMock(return_value=deployed_container)
    monkeypatch.setattr(
        "local_webpage_access.docker_runtime.DockerRuntime", MagicMock(return_value=rt)
    )
    monkeypatch.setattr(hosting, "start_container", start)
    monkeypatch.setattr(lifecycle, "_sync_alias_port", lambda *a, **k: None)
    monkeypatch.setattr(path_alias, "maybe_verify_alias_after_start", lambda *a, **k: False)

    out = lifecycle.restart_instance(workspace, config, registry, "demo")
    assert out is deployed_container
    rt.stop.assert_not_called()
    rt.down.assert_not_called()
    start.assert_called_once()


# ---- recreate（CHK-252）统一停机语义 -----------------------------------------


def test_recreate_stop_failure_aborts_and_preserves_identity(
    workspace, registry, config, deployed_container, monkeypatch
) -> None:
    """runtime recreate 停机失败 → HostingError 中止，manifest 身份不被清空。"""
    rt = MagicMock()
    rt.is_running.return_value = True
    rt.stop.side_effect = DockerError("模拟 stop 失败", instance_id="demo", action="stop")
    monkeypatch.setattr(hosting, "DockerRuntime", MagicMock(return_value=rt))
    monkeypatch.setattr(hosting, "_can_recreate_container_without_rebuild", lambda *a: True)

    with pytest.raises(HostingError, match="实例仍在运行，runtime recreate 已中止"):
        hosting.recreate_container_runtime(workspace, config, registry, "demo")

    rt.up.assert_not_called()
    saved = hosting._load_manifest(workspace, "demo")
    assert saved.container is not None
    assert saved.container.containerId == "old-container"


# ---- 迁移（workspace_migrate）与 S1-05 停机安全门整合 --------------------------


class _DownFailsRuntime:
    """停机必失败、查询仍报告容器存活（down 失败现场）的替身。"""

    def __init__(self, workspace=None, registry=None) -> None:
        self.workspace = workspace
        self.registry = registry

    @classmethod
    def ensure_available(cls) -> None:
        return None

    def is_running(self, iid: str) -> bool:
        return True

    def down(self, iid: str, **kw):
        raise DockerError("模拟 down 失败", instance_id=iid, action="down")

    def container_id_strict(self, iid: str, *, all_containers: bool = False) -> str | None:
        return "old-container"  # S1-05 后置校验视角：容器仍在运行


def test_migrate_quiesce_down_failure_blocks(
    workspace, registry, config, deployed_container, monkeypatch
) -> None:
    """迁移停机 down 失败 → HostingError 记入 errors，S1-05 后置校验继续阻断（D6）。"""
    workspace.app_compose_path("demo").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr("local_webpage_access.docker_runtime.DockerRuntime", _DownFailsRuntime)
    monkeypatch.setattr(lifecycle, "stop_instance_op", lambda *a, **k: None)
    monkeypatch.setattr("local_webpage_access.daemon.is_running", lambda *a, **k: False)
    monkeypatch.setattr("local_webpage_access.manager_service.is_running", lambda *a, **k: False)
    monkeypatch.setattr(
        "local_webpage_access.gateway_service.is_gateway_running", lambda *a, **k: False
    )
    monkeypatch.setattr("local_webpage_access.autostart.disable", lambda *a, **k: True)
    monkeypatch.setattr("local_webpage_access.daemon.stop_daemon", lambda *a, **k: True)
    monkeypatch.setattr("local_webpage_access.manager_service.stop_manager", lambda *a, **k: True)
    monkeypatch.setattr("local_webpage_access.gateway_service.stop_gateway", lambda *a, **k: True)

    snapshot = workspace_migrate.MigrateSnapshot(restore_instance_ids=["demo"])
    result = workspace_migrate.quiesce_workspace(workspace, config, registry, snapshot)

    assert any("迁移 已中止" in e for e in result.errors)
    # S1-05 后置校验（BUG-764 严格查询）确认容器仍在运行 → 迁移必须中止
    assert any("demo" in f and "仍在运行" in f for f in result.failures)
