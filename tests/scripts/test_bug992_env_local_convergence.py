"""BUG-992：docker/.env.local（业务环境文件）的创建/修改/删除必须进入运行配置收敛。

``compose stop`` + ``compose start`` 不会重读 env_file——只有 ``up``（含
force-recreate）才把新环境注入容器。运行指纹 v3 起把 ``.env.local`` 字节纳入
runtimeConfigHash：变化时决策为 runtime_recreate（``up --force-recreate``，
不重建镜像、保留数据卷），未变化时保持轻量 start。哈希与 ``.env`` 同口径
按字节计算，日志只暴露字段名，不写明文值。
"""

from __future__ import annotations

import pytest

from local_webpage_access import lifecycle, rebuild_txn
from local_webpage_access.models import InstanceManifest
from tests.scripts.test_lifecycle import fake_runtime  # noqa: F401 — Docker 替身夹具
from tests.scripts.test_s2_04_convergence import _baseline


def _env_local(workspace, iid: str = "api"):
    return workspace.app_env_local_path(iid)


def _advance_baseline(workspace):
    """模拟 recreate 部署成功：把当前生成物（含 .env.local）登记为指纹基线。"""
    manifest = InstanceManifest.load(workspace.app_manifest_path("api"))
    manifest.deploymentFingerprints = lifecycle._compute_deployment_fingerprints(
        workspace, manifest
    )
    manifest.save(workspace.app_manifest_path("api"))


def _action(workspace, config, registry):
    manifest = InstanceManifest.load(workspace.app_manifest_path("api"))
    return lifecycle._deployed_action(workspace, config, registry, "api", manifest)


def test_unchanged_without_env_local_stays_none(workspace, registry, config):
    _baseline(workspace, registry, config)
    assert _action(workspace, config, registry) == ("none", [])


def test_env_local_create_triggers_runtime_recreate(workspace, registry, config):
    _baseline(workspace, registry, config)
    _env_local(workspace).write_text("CMD_DOMAIN=example.test:8080\n", encoding="utf-8")

    assert _action(workspace, config, registry) == ("runtime_recreate", ["runtimeConfigHash"])


def test_env_local_modify_triggers_runtime_recreate(workspace, registry, config):
    _baseline(workspace, registry, config)
    _env_local(workspace).write_text("CMD_DOMAIN=old.test:8080\n", encoding="utf-8")
    _advance_baseline(workspace)
    assert _action(workspace, config, registry) == ("none", [])

    _env_local(workspace).write_text(
        "CMD_DOMAIN=new.test:8080\nCMD_URL_ADDPORT=false\n", encoding="utf-8"
    )

    assert _action(workspace, config, registry) == ("runtime_recreate", ["runtimeConfigHash"])


def test_env_local_delete_triggers_runtime_recreate(workspace, registry, config):
    _baseline(workspace, registry, config)
    _env_local(workspace).write_text("CMD_DOMAIN=example.test:8080\n", encoding="utf-8")
    _advance_baseline(workspace)
    assert _action(workspace, config, registry) == ("none", [])

    _env_local(workspace).unlink()

    assert _action(workspace, config, registry) == ("runtime_recreate", ["runtimeConfigHash"])


def test_env_local_comment_only_change_still_recreates(workspace, registry, config):
    """与 .env 同口径按字节比较：仅注释/空白差异也触发 recreate（可预测、不猜测语义）。"""
    _baseline(workspace, registry, config)
    path = _env_local(workspace)
    path.write_text("# v1\nCMD_DOMAIN=example.test:8080\n", encoding="utf-8")
    _advance_baseline(workspace)

    path.write_text("# v2\nCMD_DOMAIN=example.test:8080\n", encoding="utf-8")

    assert _action(workspace, config, registry) == ("runtime_recreate", ["runtimeConfigHash"])


@pytest.mark.parametrize("operation", ["start_instance", "restart_instance", "recover_instance"])
def test_env_local_edit_recreates_without_image_rebuild(
    workspace, registry, config, fake_runtime, operation  # noqa: F811
):
    """端到端：修改 .env.local 后 start/restart/recover 走 up --force-recreate 注入
    新环境，不重建镜像；recreate 成功后基线推进到 v3，再次执行回到轻量路径。"""
    _baseline(workspace, registry, config)
    # BUG-998：成功部署还需记录可恢复的旧环境（此处为「无业务文件」）。
    rebuild_txn.save_last_good_env(workspace, "api")
    _env_local(workspace).write_text("CMD_DOMAIN=example.test:8080\n", encoding="utf-8")

    getattr(lifecycle, operation)(workspace, config, registry, "api")

    assert "up" in fake_runtime.calls
    assert "build" not in fake_runtime.calls
    fps = InstanceManifest.load(workspace.app_manifest_path("api")).deploymentFingerprints
    assert fps["runtimeConfigHashVersion"] == lifecycle._RUNTIME_CONFIG_HASH_VERSION
    assert fps["runtimeConfigHash"] == lifecycle._compute_deployment_fingerprints(
        workspace, InstanceManifest.load(workspace.app_manifest_path("api"))
    )["runtimeConfigHash"]

    fake_runtime.calls.clear()
    getattr(lifecycle, operation)(workspace, config, registry, "api")
    assert "up" not in fake_runtime.calls
    assert "build" not in fake_runtime.calls


def test_v2_baseline_never_silently_absorbs_env_local(workspace, registry, config):
    """反吸收洞：v2 基线（未记录 .env.local）即使其余指纹全部吻合，也不得把
    盘上已存在的 .env.local 吞进基线——必须走一次 recreate 让容器环境收敛。"""
    manifest = _baseline(workspace, registry, config)
    _env_local(workspace).write_text("CMD_DOMAIN=example.test:8080\n", encoding="utf-8")
    fps = dict(manifest.deploymentFingerprints)
    # v2 口径：compose 规范化 + .env，不含 .env.local。
    fps["runtimeConfigHash"] = lifecycle._compute_runtime_config_fingerprint(
        workspace.app_compose_path("api"), workspace.app_env_path("api")
    )
    fps["runtimeConfigHashVersion"] = 2
    manifest.deploymentFingerprints = fps
    manifest.save(workspace.app_manifest_path("api"))

    assert _action(workspace, config, registry) == ("runtime_recreate", ["runtimeConfigHash"])
    # 决策本身不推进基线（v3 基线由 recreate 部署成功后登记）。
    assert (
        InstanceManifest.load(workspace.app_manifest_path("api")).deploymentFingerprints == fps
    )
