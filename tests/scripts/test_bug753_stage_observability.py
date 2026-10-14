"""S2-08 操作观测回归（BUG-753 / issue #59）。

翻转 design/5-checkpoints/2026-10-11/issues47-60-repro.py::test_59_manager_stage_not_mirrored：
阶段日志按操作来源镜像到 lwa.log（``source=<来源> lifecycle_stage …``），
非 CLI 进程（manager/daemon/agent）不再丢失阶段；CLI 来源不重复镜像。
静态站与前端构建部署路径补齐 host_start / sync_done / gateway_reload_done 等阶段记录。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from local_webpage_access import hosting
from local_webpage_access.hosting import _mirror_stage_to_global_log, host_static
from local_webpage_access.models import Runtime, ServingMode, Status
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry

from tests.scripts._helpers import make_container_manifest


class _OkRuntime:
    """build/up 全成功 + 可观测身份的假 runtime（与 test_hosting 同款）。"""

    ensure_available = staticmethod(lambda: None)

    def __init__(self, *a, **kw):
        pass

    def is_running(self, iid):
        return False

    def down(self, iid, **kw):
        pass

    def build(self, iid, **kw):
        return None

    def up(self, iid, **kw):
        return None

    def container_id(self, iid, **kw):
        return "fake-cid"

    def image_id(self, iid, **kw):
        return "fake-iid"


def _seed_ok_container(workspace: Workspace, registry: Registry) -> None:
    workspace.ensure_app_dirs("api")
    m = make_container_manifest("api")
    m.save(workspace.app_manifest_path("api"))
    registry.upsert_from_manifest(m)


def _host_ok_container(workspace, config, registry, monkeypatch) -> None:
    _seed_ok_container(workspace, registry)
    monkeypatch.setattr(hosting, "DockerRuntime", _OkRuntime)
    monkeypatch.setattr(
        hosting,
        "_evaluate_container_verification",
        lambda *a, **k: {"overall_status": "passed"},
    )
    hosting.host_container(workspace, config, registry, "api")


# ---- 翻转：test_59 -------------------------------------------------------------


def test_59_mirror_by_source(workspace, monkeypatch) -> None:
    """翻转 repro::test_59：默认（cli）不镜像；manager/daemon 来源镜像且带 source=。"""
    append = MagicMock()
    monkeypatch.setattr("local_webpage_access.logging.append_global_log", append)
    monkeypatch.delenv("LWA_DAEMON_PROCESS", raising=False)
    monkeypatch.delenv("LWA_OP_SOURCE", raising=False)

    # CLI（默认来源）：本就把阶段写进 lwa.log，不重复镜像
    _mirror_stage_to_global_log(workspace, "demo", "compose_build_start")
    append.assert_not_called()

    # manager 来源：镜像一行，格式带 source=manager
    monkeypatch.setenv("LWA_OP_SOURCE", "manager")
    _mirror_stage_to_global_log(workspace, "demo", "compose_build_start")
    append.assert_called_once()
    message = append.call_args.args[1]
    assert message.startswith(
        "source=manager lifecycle_stage instance=demo stage=compose_build_start"
    )

    # daemon 旧标记兼容：未设 LWA_OP_SOURCE 时按 daemon 镜像
    monkeypatch.delenv("LWA_OP_SOURCE", raising=False)
    monkeypatch.setenv("LWA_DAEMON_PROCESS", "1")
    append.reset_mock()
    _mirror_stage_to_global_log(workspace, "demo", "compose_build_start")
    append.assert_called_once()
    assert append.call_args.args[1].startswith("source=daemon ")


def test_manager_source_stage_written_to_lwa_log(workspace, registry, config, monkeypatch) -> None:
    """S2-08 新增验收：manager 来源的容器重建把阶段写入 lwa.log 且带 source=manager。"""
    monkeypatch.setenv("LWA_OP_SOURCE", "manager")
    _host_ok_container(workspace, config, registry, monkeypatch)

    text = (workspace.logs / "lwa.log").read_text(encoding="utf-8")
    for stage in (
        "host_start",
        "dockerfile_ready",
        "compose_ready",
        "compose_build_start",
        "compose_build_done",
        "compose_up_start",
        "compose_up_done",
    ):
        assert f"source=manager lifecycle_stage instance=api stage={stage}" in text


def test_cli_source_does_not_duplicate_mirror(workspace, registry, config, monkeypatch) -> None:
    """S2-08 新增验收：CLI 来源不重复镜像（lwa.log 无 source= 阶段行）。"""
    monkeypatch.setenv("LWA_OP_SOURCE", "cli")
    monkeypatch.delenv("LWA_DAEMON_PROCESS", raising=False)
    _host_ok_container(workspace, config, registry, monkeypatch)

    log_path = workspace.logs / "lwa.log"
    assert not log_path.exists() or "lifecycle_stage" not in log_path.read_text(encoding="utf-8")


# ---- S2-08 新增：静态与前端部署路径的阶段记录 ------------------------------------


def _seed_static_instance(workspace: Workspace, registry: Registry, iid: str = "demo") -> None:
    """构造一个已导入的静态实例（current/ 含 index.html + manifest + registry 记录）。"""
    workspace.ensure_app_dirs(iid)
    current = workspace.app_current(iid)
    (current / "index.html").write_text("<html><body>hello</body></html>", encoding="utf-8")
    manifest = make_container_manifest(
        iid,
        runtime=Runtime.SHARED_STATIC,
        servingMode=ServingMode.SHARED_STATIC,
        container=None,
        status=Status.STOPPED,
    )
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)


def _mock_gateway(monkeypatch, workspace: Workspace) -> MagicMock:
    """把 hosting.StaticGateway 换成假网关：caddy 后端、enable/reload 立即成功。"""
    gateway = MagicMock()
    gateway.detect_backend.return_value = "caddy"
    gateway.site_config_path.return_value = workspace.app_dir("demo") / "site.conf"
    gateway.health_check.return_value = True
    monkeypatch.setattr(hosting, "StaticGateway", lambda *a, **k: gateway)
    return gateway


def _stage_events(registry: Registry, iid: str) -> list[str]:
    return [
        row["message"]
        for row in registry.list_events(iid)
        if row["event_type"] == "lifecycle_stage"
    ]


def test_static_host_path_records_stages(workspace, registry, config, monkeypatch) -> None:
    """S2-08 新增验收：静态部署路径产生 host_start / sync_done / gateway_reload_done 阶段记录。"""
    monkeypatch.delenv("LWA_OP_SOURCE", raising=False)
    monkeypatch.delenv("LWA_DAEMON_PROCESS", raising=False)
    _seed_static_instance(workspace, registry, "demo")
    _mock_gateway(monkeypatch, workspace)

    manifest = host_static(workspace, config, registry, "demo")

    assert manifest.status == Status.RUNNING
    stages = _stage_events(registry, "demo")
    assert "stage=host_start" in stages
    assert "stage=sync_done" in stages
    assert "stage=gateway_reload_done" in stages


def test_frontend_build_path_records_stages(workspace, registry, config, monkeypatch) -> None:
    """S2-08 新增验收：前端构建部署路径补齐 install/build/sync/gateway 阶段记录。"""
    monkeypatch.delenv("LWA_OP_SOURCE", raising=False)
    monkeypatch.delenv("LWA_DAEMON_PROCESS", raising=False)
    workspace.ensure_app_dirs("spa")
    current = workspace.app_current("spa")
    (current / "package.json").write_text('{"scripts":{"build":"vite build"}}', encoding="utf-8")
    manifest = make_container_manifest(
        "spa",
        runtime=Runtime.SHARED_STATIC,
        servingMode=ServingMode.SHARED_STATIC,
        container=None,
        status=Status.STOPPED,
    )
    manifest.entry.install = "npm ci"
    manifest.entry.build = "npm run build"
    manifest.save(workspace.app_manifest_path("spa"))
    registry.upsert_from_manifest(manifest)
    _mock_gateway(monkeypatch, workspace)

    def _fake_run_command(cmd, cwd=None, log_path=None, env=None):
        (cwd / "dist").mkdir(exist_ok=True)
        (cwd / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")

    monkeypatch.setattr(hosting, "run_command", _fake_run_command)

    result = hosting.build_and_host_frontend(workspace, config, registry, "spa")

    assert result.status == Status.RUNNING
    stages = _stage_events(registry, "spa")
    assert "stage=host_start" in stages
    assert "stage=install_done" in stages
    assert "stage=build_done" in stages
    assert "stage=sync_done" in stages
    assert "stage=gateway_reload_done" in stages


def test_frontend_build_failure_still_records_early_stages(
    workspace, registry, config, monkeypatch
) -> None:
    """构建失败时 host_start 等早期阶段仍可观测（静态/前端路径不再静默）。"""
    import pytest

    from local_webpage_access.errors import BuildError

    monkeypatch.delenv("LWA_OP_SOURCE", raising=False)
    monkeypatch.delenv("LWA_DAEMON_PROCESS", raising=False)
    workspace.ensure_app_dirs("spa")
    current = workspace.app_current("spa")
    (current / "package.json").write_text('{"scripts":{"build":"vite build"}}', encoding="utf-8")
    manifest = make_container_manifest(
        "spa",
        runtime=Runtime.SHARED_STATIC,
        servingMode=ServingMode.SHARED_STATIC,
        container=None,
        status=Status.STOPPED,
    )
    manifest.entry.build = "npm run build"
    manifest.save(workspace.app_manifest_path("spa"))
    registry.upsert_from_manifest(manifest)

    def _failing_run_command(cmd, cwd=None, log_path=None, env=None):
        raise BuildError("模拟构建失败", instance_id="spa")

    monkeypatch.setattr(hosting, "run_command", _failing_run_command)

    with pytest.raises(BuildError, match="模拟构建失败"):
        hosting.build_and_host_frontend(workspace, config, registry, "spa")

    stages = _stage_events(registry, "spa")
    assert "stage=host_start" in stages
