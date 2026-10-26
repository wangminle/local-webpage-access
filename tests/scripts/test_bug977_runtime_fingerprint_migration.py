"""BUG-977：运行指纹升级须验证旧基线，不能误重建或吞掉真实配置变化。

BUG-992 起运行指纹 v3 把 ``.env.local`` 字节纳入 runtimeConfigHash。v2 及更早
基线从未记录 ``.env.local``，无法证明运行中容器的环境与当前文件一致，因此
**不再静默迁移**：旧基线首次 start/restart/recover 收敛为一次轻量
runtime_recreate（``up --force-recreate``，不重建镜像、保留数据卷），recreate
部署成功后写入 v3 基线，之后回到轻量路径。真实配置变化、不可信基线与回滚
钉住的行为保持不变。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from local_webpage_access import lifecycle, rebuild_txn
from local_webpage_access.build_proxy import write_build_proxy
from local_webpage_access.config import BuildProxy
from local_webpage_access.models import InstanceManifest
from tests.scripts.test_lifecycle import fake_runtime  # noqa: F401 — Docker 替身夹具
from tests.scripts.test_s2_04_convergence import _baseline


def _legacy_baseline(workspace, registry, config, *, canonical=False):
    manifest = _baseline(workspace, registry, config)
    fps = dict(manifest.deploymentFingerprints)
    fps.pop("runtimeConfigHashVersion", None)
    if canonical:
        # BUG-977 与 BUG-992 之间短期存在的「未标版本的 v2 规范化指纹」：
        # 不传 env_local_path 即为 v2 口径（compose 规范化 + .env）。
        fps["runtimeConfigHash"] = lifecycle._compute_runtime_config_fingerprint(
            workspace.app_compose_path("api"), workspace.app_env_path("api")
        )
    else:
        # 独立复现 BUG-976 之前的字节算法，不调用待修复的生产实现。
        old = hashlib.sha256()
        for label, path in [
            ("compose", workspace.app_compose_path("api")),
            ("env", workspace.app_env_path("api")),
        ]:
            old.update(label.encode())
            old.update(b"\0")
            old.update(path.read_bytes())
        fps["runtimeConfigHash"] = old.hexdigest()
    fps["customMetadata"] = "preserve-me"
    manifest.deploymentFingerprints = fps
    manifest.save(workspace.app_manifest_path("api"))
    return manifest


@pytest.mark.parametrize("canonical", [False, True], ids=["old-raw", "untagged-normalized"])
@pytest.mark.parametrize("proxy", [None, "http://127.0.0.1:10808", "http://10.1.2.3:8080"])
def test_verified_legacy_baseline_converges_via_lightweight_recreate(
    workspace, registry, config, canonical, proxy
):
    manifest = _legacy_baseline(workspace, registry, config, canonical=canonical)
    old = dict(manifest.deploymentFingerprints)
    if proxy:
        write_build_proxy(workspace.config_path, BuildProxy(enabled=True, url=proxy))

    # BUG-992：旧基线未记录 .env.local，不能静默迁移（会把升级前对 .env.local
    # 的修改/删除吞进基线）。首次决策为一次轻量 runtime_recreate——不重建镜像；
    # 决策本身不推进基线，v3 基线由 recreate 部署成功后登记。
    assert lifecycle._deployed_action(workspace, config, registry, "api", manifest) == (
        "runtime_recreate",
        ["runtimeConfigHash"],
    )
    assert InstanceManifest.load(workspace.app_manifest_path("api")).deploymentFingerprints == old


@pytest.mark.parametrize("operation", ["start_instance", "restart_instance", "recover_instance"])
def test_legacy_upgrade_converges_once_then_lightweight(
    workspace, registry, config, fake_runtime, operation  # noqa: F811
):
    _legacy_baseline(workspace, registry, config)
    # BUG-998：真正执行替换需要可恢复环境；纯指纹决策仍不推进基线。
    rebuild_txn.save_last_good_env(workspace, "api")
    write_build_proxy(workspace.config_path, BuildProxy(enabled=True, url="http://10.1.2.3:8080"))

    # 首次：一次轻量 force-recreate 收敛容器环境（up），不重建镜像（无 build）。
    getattr(lifecycle, operation)(workspace, config, registry, "api")
    assert "up" in fake_runtime.calls
    assert "build" not in fake_runtime.calls
    fps = InstanceManifest.load(workspace.app_manifest_path("api")).deploymentFingerprints
    assert fps["runtimeConfigHashVersion"] == lifecycle._RUNTIME_CONFIG_HASH_VERSION

    # 第二次：基线已是 v3 且无变化 → 轻量路径，不再 recreate / build。
    fake_runtime.calls.clear()
    getattr(lifecycle, operation)(workspace, config, registry, "api")
    assert "up" not in fake_runtime.calls
    assert "build" not in fake_runtime.calls


@pytest.mark.parametrize("change", ["port", "volume", "memory"])
def test_legacy_migration_preserves_real_runtime_change(workspace, registry, config, change):
    manifest = _legacy_baseline(workspace, registry, config)
    old = dict(manifest.deploymentFingerprints)
    if change == "port":
        manifest.container.hostPort += 1
    elif change == "volume":
        manifest.container.extraVolumes = [f"{Path.home()}/lwa-bug977-share:/data/share:ro"]
    else:
        manifest.container.resourceLimits.memory = "768m"
    manifest.save(workspace.app_manifest_path("api"))

    assert lifecycle._deployed_action(workspace, config, registry, "api", manifest) == (
        "runtime_recreate", ["runtimeConfigHash"]
    )
    assert InstanceManifest.load(workspace.app_manifest_path("api")).deploymentFingerprints == old


@pytest.mark.parametrize("untrusted", ["hash", "unknown-version", "edited-env", "invalid-hash-type"])
def test_unverified_legacy_baseline_recreates_without_advancing(
    workspace, registry, config, untrusted
):
    manifest = _legacy_baseline(workspace, registry, config)
    if untrusted == "hash":
        manifest.deploymentFingerprints["runtimeConfigHash"] = "unknown-deployment"
    elif untrusted == "invalid-hash-type":
        manifest.deploymentFingerprints["runtimeConfigHash"] = {"invalid": "hash"}
    elif untrusted == "unknown-version":
        manifest.deploymentFingerprints["runtimeConfigHashVersion"] = 99
    else:
        env = workspace.app_env_path("api")
        env.write_text(env.read_text().replace("MEMORY_LIMIT=", "MEMORY_LIMIT=9"))
    manifest.save(workspace.app_manifest_path("api"))
    old = dict(manifest.deploymentFingerprints)

    assert lifecycle._deployed_action(workspace, config, registry, "api", manifest) == (
        "runtime_recreate", ["runtimeConfigHash"]
    )
    assert InstanceManifest.load(workspace.app_manifest_path("api")).deploymentFingerprints == old


def test_failed_render_does_not_advance_baseline(workspace, registry, config, monkeypatch):
    manifest = _legacy_baseline(workspace, registry, config)
    old = dict(manifest.deploymentFingerprints)

    def fail_render(*args):
        raise RuntimeError("render failed")

    monkeypatch.setattr(
        "local_webpage_access.generated_convergence.converge_generated_artifacts", fail_render
    )
    with pytest.raises(RuntimeError, match="render failed"):
        lifecycle._deployed_action(workspace, config, registry, "api", manifest)

    assert manifest.deploymentFingerprints == old
    assert InstanceManifest.load(workspace.app_manifest_path("api")).deploymentFingerprints == old


def test_legacy_migration_does_not_advance_baseline_before_rebuild(workspace, registry, config):
    manifest = _legacy_baseline(workspace, registry, config)
    old = dict(manifest.deploymentFingerprints)
    before = workspace.app_dockerfile_path("api").read_bytes()
    manifest.buildHooks = ["echo bug977-build-change"]
    manifest.save(workspace.app_manifest_path("api"))

    action, changed = lifecycle._deployed_action(workspace, config, registry, "api", manifest)

    assert action == "full_rebuild"
    assert "buildConfigHash" in changed
    assert workspace.app_dockerfile_path("api").read_bytes() == before
    assert InstanceManifest.load(workspace.app_manifest_path("api")).deploymentFingerprints == old


def test_legacy_migration_keeps_rollback_pin(workspace, registry, config):
    manifest = _legacy_baseline(workspace, registry, config)
    manifest.deploymentFingerprints["rollbackBuildId"] = 42
    manifest.save(workspace.app_manifest_path("api"))
    old = dict(manifest.deploymentFingerprints)
    write_build_proxy(workspace.config_path, BuildProxy(enabled=True, url="http://10.1.2.3:8080"))
    before = workspace.app_compose_path("api").read_bytes()

    assert lifecycle._deployed_action(workspace, config, registry, "api", manifest) == ("none", [])
    assert workspace.app_compose_path("api").read_bytes() == before
    assert InstanceManifest.load(workspace.app_manifest_path("api")).deploymentFingerprints == old
