"""S3 复核修复回归：默认回滚选版、镜像占用统计、时间线容错、生成物 mtime 判定。"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from local_webpage_access import hosting, rollback, versions
from local_webpage_access.cli import app as cli_app
from local_webpage_access.config import Config, example_config_text
from local_webpage_access.consistency import collect_drifts
from local_webpage_access.lifecycle import _compute_config_fingerprint
from local_webpage_access.models import InstanceManifest, Kind, Runtime, Status, StaticConfig
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry
from tests.scripts._helpers import make_container_manifest
from tests.scripts import test_s3_01_rollback as s301

COMPOSE_CURRENT = s301.COMPOSE_CURRENT
deployed = s301.deployed
rt = s301.rt


def _mark_current_build_successful(workspace: Workspace, registry: Registry, b2: int) -> None:
    """把 deployed 夹具里的失败构建 b2 改为成功，并登记为当前在用镜像 img-current。"""
    registry.finish_build(b2, status="success")
    snap = workspace.app_docker("demo") / "builds" / str(b2)
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "compose.yaml").write_text(COMPOSE_CURRENT, encoding="utf-8")
    (snap / "Dockerfile").write_text("FROM x\n", encoding="utf-8")
    (snap / ".env").write_text("A=1\n", encoding="utf-8")
    registry.record_build_artifacts(
        b2,
        image_ref=f"lwa-demo-app:b{b2}",
        image_id="img-current",
        source_hash="hash-current",
        config_snapshot=str(snap),
    )


# ---- 1. 默认回滚不得选中当前在用版本 -----------------------------------------------


def test_select_rollback_target_excludes_in_use_image(workspace, registry, deployed):
    _mark_current_build_successful(workspace, registry, deployed["b2"])
    from unittest.mock import patch

    with patch("local_webpage_access.versions.image_exists", lambda ref: True):
        newest = rollback.select_rollback_target(workspace, registry, "demo")
        assert newest["id"] == deployed["b2"]

        previous = rollback.select_rollback_target(
            workspace, registry, "demo", exclude_image_id="img-current"
        )
        assert previous["id"] == deployed["b1"]

        normalized = rollback.select_rollback_target(
            workspace, registry, "demo", exclude_image_id="sha256:img-current"
        )
        assert normalized["id"] == deployed["b1"]


def test_default_rollback_targets_previous_not_current(
    workspace, registry, config, deployed, rt, monkeypatch
):
    runtime, order, tags = rt
    _mark_current_build_successful(workspace, registry, deployed["b2"])
    monkeypatch.setattr(
        hosting,
        "_evaluate_container_verification",
        lambda *a, **k: {
            "overall_status": "passed",
            "liveness_passed": True,
            "mandatory_all_passed": True,
            "optional_warnings": [],
            "probe_notes": [],
            "observed_capabilities": [],
        },
    )

    manifest = rollback.rollback_instance(workspace, config, registry, "demo")

    assert manifest.container.imageId == "img-old"
    assert (f"lwa-demo-app:b{deployed['b1']}", "lwa-demo-app:latest") in tags
    assert (f"lwa-demo-app:b{deployed['b2']}", "lwa-demo-app:latest") not in tags


def test_default_rollback_without_previous_version_is_refused(
    workspace, registry, config, deployed, rt, monkeypatch
):
    # 只有当前在用版本可用（b1 镜像已被清理）：默认回滚必须拒绝，而不是原地重部署
    _mark_current_build_successful(workspace, registry, deployed["b2"])
    monkeypatch.setattr(
        "local_webpage_access.versions.image_exists",
        lambda ref: ref != f"lwa-demo-app:b{deployed['b1']}",
    )
    from local_webpage_access.errors import HostingError

    with pytest.raises(HostingError, match="没有可回滚的留档版本"):
        rollback.rollback_instance(workspace, config, registry, "demo")


# ---- 2. 镜像占用统计不因单个缺失镜像归零 ----------------------------------------


def test_image_disk_usage_counts_present_images_when_one_is_missing(monkeypatch):
    stdout = json.dumps([{"Size": 100}, {"Size": 50}])

    def fake_docker(args, *, timeout=60):
        return subprocess.CompletedProcess(
            args=["docker", *args],
            returncode=1,  # 有一个引用不存在时 docker 以非零退出，但 stdout 仍有存在镜像
            stdout=stdout,
            stderr="Error: No such image: gone:b1",
        )

    monkeypatch.setattr(versions, "_docker", fake_docker)
    assert versions.image_disk_usage_bytes(["a:b1", "b:b2", "gone:b1"]) == 150


def test_image_disk_usage_zero_when_nothing_parses(monkeypatch):
    monkeypatch.setattr(
        versions,
        "_docker",
        lambda args, *, timeout=60: subprocess.CompletedProcess(args, 1, stdout="", stderr="x"),
    )
    assert versions.image_disk_usage_bytes(["gone:b1"]) == 0


# ---- 3. 时间线在 manifest 损坏时仍可输出 -------------------------------------------


def test_logs_timeline_survives_corrupt_manifest(workspace_root: Path, monkeypatch):
    ws = Workspace(workspace_root)
    ws.ensure_workspace_dirs()
    ws.config_path.write_text(example_config_text(), encoding="utf-8")
    config = Config(staticGateway="builtin", portPool={"start": 21000, "end": 21050})
    reg = Registry(ws.db_path)
    reg.open()
    ws.ensure_app_dirs("demo")
    m = InstanceManifest(
        id="demo",
        name="demo",
        kind=Kind.STATIC,
        runtime=Runtime.SHARED_STATIC,
        version="1",
        servingMode="shared-static",
        static=StaticConfig(routeMode="port"),
    )
    m.save(ws.app_manifest_path("demo"))
    reg.upsert_from_manifest(m)
    reg.add_event("demo", "start", "事件仍可见", source="cli")
    ws.app_manifest_path("demo").write_text("{broken", encoding="utf-8")

    def open_env():
        return ws, config, reg

    monkeypatch.setattr("local_webpage_access.cli.lifecycle.open_workspace_registry", open_env)
    result = CliRunner().invoke(cli_app, ["logs", "demo", "--timeline"])
    assert result.exit_code == 0, result.output
    assert "manifest 读取失败" in result.output
    assert "[cli] start" in result.output


# ---- 4. 生成物 mtime 判定覆盖 .env / Dockerfile -------------------------------------


def test_manual_env_edit_is_detected_even_when_compose_is_older(workspace, registry):
    workspace.ensure_app_dirs("demo")
    m = make_container_manifest("demo", status=Status.RUNNING)
    m.container.hostPort = 21001
    compose = workspace.app_compose_path("demo")
    dockerfile = workspace.app_dockerfile_path("demo")
    env = workspace.app_env_path("demo")
    compose.parent.mkdir(parents=True, exist_ok=True)
    compose.write_text("services: {}\n", encoding="utf-8")
    dockerfile.write_text("FROM x\n", encoding="utf-8")
    env.write_text("A=1\n", encoding="utf-8")
    m.deploymentFingerprints = {
        "configHash": _compute_config_fingerprint(compose, dockerfile, env)
    }
    m.save(workspace.app_manifest_path("demo"))
    registry.upsert_from_manifest(m)

    base = compose.stat().st_mtime
    os.utime(compose, (base - 300, base - 300))
    os.utime(dockerfile, (base - 300, base - 300))
    os.utime(workspace.app_manifest_path("demo"), (base - 120, base - 120))
    env.write_text("A=2\n", encoding="utf-8")  # 只手改 .env
    os.utime(env, (base, base))

    drifts = collect_drifts(workspace, Config(staticGateway="builtin"), registry)
    assert any(d.family == "artifacts" for d in drifts)
