"""BUG-985：buildBaseFromAlias 只注入 VITE_BASE，对读取 BASE_URL（IT-Tools）
或 PUBLIC_URL（CRA）的前端项目，别名 base 注入静默无效、产物仍绝对路径，
别名守卫内容级失败。修复后 effective_build_env 同值派生三个常见键。"""

from __future__ import annotations

from local_webpage_access.instance_settings import effective_build_env
from tests.scripts.test_review_followup_fixes import manifest
from local_webpage_access.models import StaticConfig


def test_alias_base_injected_into_all_common_keys() -> None:
    m = manifest(
        buildBaseFromAlias=True,
        static=StaticConfig(routeMode="name", routeHost="it-tools"),
    )
    env = effective_build_env(m)
    assert env["VITE_BASE"] == "/it-tools/"
    assert env["BASE_URL"] == "/it-tools/"
    assert env["PUBLIC_URL"] == "/it-tools/"


def test_alias_base_keys_follow_alias_and_root_fallback() -> None:
    m = manifest(
        buildBaseFromAlias=True,
        static=StaticConfig(routeMode="name", routeHost="first"),
    )
    assert effective_build_env(m)["BASE_URL"] == "/first/"
    m.static.routeHost = "second"
    assert effective_build_env(m)["BASE_URL"] == "/second/"
    m.static.routeMode = "port"
    env = effective_build_env(m)
    assert env["VITE_BASE"] == env["BASE_URL"] == env["PUBLIC_URL"] == "/"


def test_derived_keys_still_override_manual_and_env_not_mutated() -> None:
    """BUG-640 语义不变：跟随开启时派生值覆盖手动值；manifest.buildEnv 不被改写。"""
    m = manifest(
        buildEnv={"CUSTOM": "yes", "VITE_BASE": "/manual/", "BASE_URL": "/manual/"},
        buildBaseFromAlias=True,
        static=StaticConfig(routeMode="name", routeHost="it-tools"),
    )
    env = effective_build_env(m)
    assert env["VITE_BASE"] == env["BASE_URL"] == env["PUBLIC_URL"] == "/it-tools/"
    assert env["CUSTOM"] == "yes"
    assert m.buildEnv == {"CUSTOM": "yes", "VITE_BASE": "/manual/", "BASE_URL": "/manual/"}


def test_follow_disabled_leaves_keys_untouched() -> None:
    m = manifest(
        buildEnv={"BASE_URL": "/manual/"},
        static=StaticConfig(routeMode="name", routeHost="it-tools"),
    )
    env = effective_build_env(m)
    assert env["BASE_URL"] == "/manual/"
    assert "VITE_BASE" not in env
    assert "PUBLIC_URL" not in env
