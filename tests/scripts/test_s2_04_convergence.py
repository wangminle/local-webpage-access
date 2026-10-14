"""S2-04 配置收敛：决策前按 manifest 重渲染（PLN-052 S2-04 / #48 #57-1 N4 / D1）。

覆盖：
- 手改生成物（compose 注入 privileged）→ 决策前被 manifest 覆盖，并记录事件（N4）；
- 无手改、manifest 也未变 → 不告警、决策为 none；
- extraVolumes 变化 → runtime_recreate，新挂载进入 compose（#48）；
- buildHooks 变化 → full_rebuild（D1）；
- restart 与 start 使用同一决策：runtime_recreate 时不走 compose stop + start。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from local_webpage_access import generated_convergence, hosting, lifecycle
from local_webpage_access.models import InstanceManifest
from tests.scripts.test_lifecycle import _seed_container, fake_runtime  # noqa: F401 — 夹具与种子复用


# 审计只允许家目录下的宿主挂载（其余绝对路径按敏感目录 critical 拒绝）
EXTRA_SRC = str(Path.home() / "lwa-s24-share")


def _baseline(workspace, registry, config, iid: str = "api") -> InstanceManifest:
    """已部署实例：先渲染一次，把渲染结果登记为指纹基线（即上次成功部署的状态）。"""
    manifest = _seed_container(workspace, registry, iid, deployed=True)
    # 真实部署成功后 manifest 的生成物路径为绝对路径（_refresh_manifest_workspace_paths）
    manifest.container.composePath = str(workspace.app_compose_path(iid))
    manifest.container.dockerfilePath = str(workspace.app_dockerfile_path(iid))
    generated_convergence.converge_generated_artifacts(workspace, config, manifest)
    manifest.deploymentFingerprints = lifecycle._compute_deployment_fingerprints(
        workspace, manifest
    )
    manifest.save(workspace.app_manifest_path(iid))
    return manifest


def _events(registry, iid: str) -> list[str]:
    return [e["message"] for e in registry.list_events(iid)]


def test_manual_compose_edit_is_overwritten_and_recorded(
    workspace, registry, config, fake_runtime  # noqa: F811
) -> None:
    _baseline(workspace, registry, config)
    compose = workspace.app_compose_path("api")
    compose.write_text(
        "services:\n  app:\n    privileged: true\n", encoding="utf-8"
    )  # 绕过审计的手改（N4）

    manifest = InstanceManifest.load(workspace.app_manifest_path("api"))
    action, changed = lifecycle._deployed_action(workspace, config, registry, "api", manifest)

    # 覆盖后盘上内容回到与上次部署一致的基线，无需 recreate
    assert "privileged" not in compose.read_text(encoding="utf-8")
    assert action == "none"
    assert any("手工修改" in msg for msg in _events(registry, "api"))


def test_unchanged_instance_has_no_action_and_no_warning(
    workspace, registry, config, fake_runtime  # noqa: F811
) -> None:
    _baseline(workspace, registry, config)
    manifest = InstanceManifest.load(workspace.app_manifest_path("api"))

    action, _ = lifecycle._deployed_action(workspace, config, registry, "api", manifest)

    assert action == "none"
    assert not any("手工修改" in msg for msg in _events(registry, "api"))


def test_extra_volume_change_recreates_with_new_mount(
    workspace, registry, config, fake_runtime  # noqa: F811
) -> None:
    _baseline(workspace, registry, config)
    manifest = InstanceManifest.load(workspace.app_manifest_path("api"))
    manifest.container.extraVolumes = [f"{EXTRA_SRC}:/data/share:ro"]
    manifest.save(workspace.app_manifest_path("api"))
    manifest = InstanceManifest.load(workspace.app_manifest_path("api"))

    action, changed = lifecycle._deployed_action(workspace, config, registry, "api", manifest)

    assert action == "runtime_recreate"
    assert "runtimeConfigHash" in changed
    assert f"{EXTRA_SRC}:/data/share:ro" in workspace.app_compose_path("api").read_text(
        encoding="utf-8"
    )


def test_build_hook_change_forces_full_rebuild(workspace, registry, config, fake_runtime) -> None:  # noqa: F811
    _baseline(workspace, registry, config)
    manifest = InstanceManifest.load(workspace.app_manifest_path("api"))
    manifest.buildHooks = ["echo s24-hook"]
    manifest.save(workspace.app_manifest_path("api"))
    manifest = InstanceManifest.load(workspace.app_manifest_path("api"))

    action, changed = lifecycle._deployed_action(workspace, config, registry, "api", manifest)

    assert action == "full_rebuild"
    assert "buildConfigHash" in changed


def test_full_rebuild_decision_restores_pre_operation_files(
    workspace, registry, config, fake_runtime  # noqa: F811
) -> None:
    """full_rebuild 时盘上回到旧状态，由 host_container 重新渲染并快照旧状态（补偿可精确回滚）。"""
    _baseline(workspace, registry, config)
    old_dockerfile = workspace.app_dockerfile_path("api").read_text(encoding="utf-8")
    manifest = InstanceManifest.load(workspace.app_manifest_path("api"))
    manifest.buildHooks = ["echo s24-hook"]
    manifest.save(workspace.app_manifest_path("api"))
    manifest = InstanceManifest.load(workspace.app_manifest_path("api"))

    action, _ = lifecycle._deployed_action(workspace, config, registry, "api", manifest)

    assert action == "full_rebuild"
    assert workspace.app_dockerfile_path("api").read_text(encoding="utf-8") == old_dockerfile


def test_restart_uses_recreate_decision_without_compose_stop(
    workspace, registry, config, fake_runtime, monkeypatch  # noqa: F811
) -> None:
    """restart 与 start 同一决策：runtime_recreate 时不先 compose stop，交给 recreate。"""
    _baseline(workspace, registry, config)
    manifest = InstanceManifest.load(workspace.app_manifest_path("api"))
    manifest.container.extraVolumes = [f"{EXTRA_SRC}:/data/share:ro"]
    manifest.save(workspace.app_manifest_path("api"))

    recreate = MagicMock(return_value=manifest)
    stop = MagicMock()
    monkeypatch.setattr(hosting, "recreate_container_runtime", recreate)
    monkeypatch.setattr(hosting, "stop_or_abort", stop)
    monkeypatch.setattr(lifecycle, "_sync_alias_port", lambda *a, **k: None)
    monkeypatch.setattr(
        "local_webpage_access.path_alias.maybe_verify_alias_after_start", lambda *a, **k: False
    )

    lifecycle.restart_instance(workspace, config, registry, "api")

    recreate.assert_called_once()
    stop.assert_not_called()


@pytest.mark.parametrize("dummy", [None])
def test_converge_without_host_port_reports_not_rendered(
    workspace, registry, config, dummy
) -> None:
    manifest = _seed_container(workspace, registry, "api", deployed=False)
    result = generated_convergence.converge_generated_artifacts(workspace, config, manifest)
    assert result.rendered is False
