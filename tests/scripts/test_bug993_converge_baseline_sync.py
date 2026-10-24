"""BUG-993：converge 自身改写生成物（如仅 buildProxy 变化）时同步 configHash 基线。

背景（BUG-976 的相邻缺陷）：运行/构建指纹已排除 Compose build 段，因此仅修改
构建代理时 _deployed_action 决策为 none，盘上 compose.yaml 却被重渲染为新代理
内容——聚合 configHash 基线（manifest.deploymentFingerprints["configHash"]）停在
旧值。下一次再改代理并 start，before != stored 且 before != after，被误判为
「生成物被手工修改（manual_overwritten）」。

修复：converge 检测到「盘上原与基线一致、改写由本次重渲染自身造成」
（ConvergeResult.baseline_advanced）时，_deployed_action 在 action == "none"
分支把 configHash 基线跟进到当前渲染值并落盘。真手改（manual_overwritten）与
「盘=渲染值≠基线」的保守场景不动基线，手改检测语义不变。
"""

from __future__ import annotations

from local_webpage_access import lifecycle
from local_webpage_access.build_proxy import write_build_proxy
from local_webpage_access.config import BuildProxy
from local_webpage_access.models import InstanceManifest
from tests.scripts.test_s2_04_convergence import _baseline, _events

_PROXY_A = "http://10.1.2.3:8080"
_PROXY_B = "http://10.1.2.4:8080"


def _load(workspace) -> InstanceManifest:
    return InstanceManifest.load(workspace.app_manifest_path("api"))


def _set_proxy(workspace, url: str | None) -> None:
    if url is None:
        write_build_proxy(workspace.config_path, BuildProxy(enabled=False, url=""))
    else:
        write_build_proxy(workspace.config_path, BuildProxy(enabled=True, url=url))


def _assert_no_manual_event(registry) -> None:
    assert not any("手工修改" in msg for msg in _events(registry, "api"))


def test_repeated_build_proxy_changes_do_not_flag_manual_overwrite(workspace, registry, config):
    """改代理 → start（none，compose 重写）→ 再改代理 → start：不得误判手改。"""
    _baseline(workspace, registry, config)
    old_config_hash = _load(workspace).deploymentFingerprints["configHash"]

    # 第 1 次修改 buildProxy（代理 A）：决策 none，converge 重写 compose（H_0 → H_1）
    _set_proxy(workspace, _PROXY_A)
    manifest = _load(workspace)
    assert lifecycle._deployed_action(workspace, config, registry, "api", manifest) == (
        "none",
        [],
    )
    _proxy_a_hash = _load(workspace).deploymentFingerprints["configHash"]
    assert _proxy_a_hash != old_config_hash  # 基线已同步到含代理 A 的渲染值
    assert _PROXY_A in workspace.app_compose_path("api").read_text(encoding="utf-8")
    _assert_no_manual_event(registry)

    # 第 2 次修改 buildProxy（代理 B）：修复前此处 before=H_1、stored=H_0 → 误报手改
    _set_proxy(workspace, _PROXY_B)
    manifest = _load(workspace)
    assert lifecycle._deployed_action(workspace, config, registry, "api", manifest) == (
        "none",
        [],
    )
    _assert_no_manual_event(registry)
    reloaded = _load(workspace)
    assert _PROXY_B in workspace.app_compose_path("api").read_text(encoding="utf-8")
    assert reloaded.deploymentFingerprints["configHash"] != _proxy_a_hash
    assert (
        reloaded.deploymentFingerprints["configHash"]
        == (lifecycle._compute_deployment_fingerprints(workspace, reloaded)["configHash"])
    )

    # 第 3 次修改 buildProxy（关闭代理）：同样不得误判
    _set_proxy(workspace, None)
    manifest = _load(workspace)
    assert lifecycle._deployed_action(workspace, config, registry, "api", manifest) == (
        "none",
        [],
    )
    _assert_no_manual_event(registry)
    assert _load(workspace).deploymentFingerprints["configHash"] == old_config_hash

    # 回到与初始基线一致的内容后再来一次，仍是干净的 none
    manifest = _load(workspace)
    assert lifecycle._deployed_action(workspace, config, registry, "api", manifest) == (
        "none",
        [],
    )
    _assert_no_manual_event(registry)


def test_real_manual_edit_still_detected_after_baseline_sync(workspace, registry, config):
    """基线同步后真手改仍被覆盖并记录事件，且手改不推进基线。"""
    _baseline(workspace, registry, config)

    _set_proxy(workspace, _PROXY_A)
    manifest = _load(workspace)
    assert lifecycle._deployed_action(workspace, config, registry, "api", manifest) == (
        "none",
        [],
    )
    synced_hash = _load(workspace).deploymentFingerprints["configHash"]

    # 绕过审计的手改（N4）：与代理无关的 compose 内容篡改
    compose = workspace.app_compose_path("api")
    compose.write_text("services:\n  app:\n    privileged: true\n", encoding="utf-8")

    manifest = _load(workspace)
    action, _ = lifecycle._deployed_action(workspace, config, registry, "api", manifest)

    assert action == "none"  # 覆盖后盘上回到基线内容，无需 recreate
    assert "privileged" not in compose.read_text(encoding="utf-8")
    assert any("手工修改" in msg for msg in _events(registry, "api"))
    # 手改检测不推进基线：覆盖后盘上内容 == 基线渲染值，原基线仍然正确
    assert _load(workspace).deploymentFingerprints["configHash"] == synced_hash
