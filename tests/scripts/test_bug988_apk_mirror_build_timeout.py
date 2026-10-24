"""BUG-988：apk 镜像源注入 + 容器构建超时工作区可配。

背景（yi14-wsl-debian 实测）：官方 dl-cdn.alpinelinux.org 慢网络下 39 包
30 分钟仅装 11 个、两次撞 docker_runtime._BUILD_TIMEOUT=1800；阿里云镜像
同批 7m22s。修复：buildMirrors.apkMirror（china 预置默认阿里）在 apk 层
同一 RUN 内替换仓库主机；Config.buildTimeout 供 docker compose build 超时
取值（显式 timeout 参数优先）。
"""

from pathlib import Path

import pytest

from local_webpage_access.config import BuildMirrors, Config
from local_webpage_access.dockerfile_templates import _apk_add_block


def _china() -> BuildMirrors:
    return BuildMirrors().resolved()


def _disabled() -> BuildMirrors:
    return BuildMirrors(enabled=False, preset="none").resolved()


def test_apk_block_no_mirrors_plain() -> None:
    assert _apk_add_block(["git"], None) == "RUN apk add --no-cache git\n"


def test_apk_block_china_preset_injects_aliyun() -> None:
    out = _apk_add_block(["git", "python3", "make", "g++"], _china())
    assert out == (
        "RUN sed -i 's|dl-cdn.alpinelinux.org|mirrors.aliyun.com|g' "
        "/etc/apk/repositories && apk add --no-cache git python3 make g++\n"
    )


def test_apk_block_disabled_preset_plain() -> None:
    assert _apk_add_block(["git"], _disabled()) == "RUN apk add --no-cache git\n"


def test_apk_block_custom_mirror_host_port() -> None:
    mirrors = BuildMirrors(apkMirror="192.168.1.10:8081").resolved()
    out = _apk_add_block(["git"], mirrors)
    assert "s|dl-cdn.alpinelinux.org|192.168.1.10:8081|g" in out


def test_apk_block_invalid_mirror_not_injected() -> None:
    """非法主机（含 shell 元字符/URL）不注入，保持官方源（宁慢不错）。"""
    for bad in ("evil.com && rm -rf /", "https://mirrors.aliyun.com", "a b.com"):
        mirrors = BuildMirrors(apkMirror=bad).resolved()
        assert _apk_add_block(["git"], mirrors) == "RUN apk add --no-cache git\n"


def test_apk_block_empty_pkgs_empty_string() -> None:
    assert _apk_add_block([], _china()) == ""


def _native_source(root: Path) -> None:
    (root / "yarn.lock").write_text(
        '__metadata:\n  version: 8\n"sqlite3@npm:5.1.7":\n', encoding="utf-8"
    )
    (root / "package.json").write_text('{"name": "demo"}', encoding="utf-8")


def test_render_node_apk_mirror_with_native_toolchain(tmp_path: Path) -> None:
    """端到端：原生依赖触发 python3/make/g++ 且 china 预置时 sed 前缀同层。"""
    from local_webpage_access.dockerfile_templates import _render_node
    from tests.scripts.test_dockerfile_templates import _mk_manifest

    _native_source(tmp_path)
    rendered = _render_node(_mk_manifest(), 3000, mirrors=_china(), source_dir=tmp_path)
    assert (
        "RUN sed -i 's|dl-cdn.alpinelinux.org|mirrors.aliyun.com|g' "
        "/etc/apk/repositories && apk add --no-cache python3 make g++\n"
    ) in rendered


def test_render_node_apk_plain_when_mirrors_disabled(tmp_path: Path) -> None:
    from local_webpage_access.dockerfile_templates import _render_node
    from tests.scripts.test_dockerfile_templates import _mk_manifest

    _native_source(tmp_path)
    rendered = _render_node(_mk_manifest(), 3000, mirrors=_disabled(), source_dir=tmp_path)
    assert "RUN apk add --no-cache python3 make g++\n" in rendered
    assert "sed -i" not in rendered


def test_config_build_timeout_default_and_custom() -> None:
    assert Config().buildTimeout == 1800
    assert Config(buildTimeout=7200).buildTimeout == 7200
    with pytest.raises(Exception):
        Config(buildTimeout=30)  # 下限 60


class _FakeResult:
    ok = True
    returncode = 0
    stdout = ""
    stderr = ""


def _runtime_with_capture(workspace, monkeypatch: pytest.MonkeyPatch):
    from local_webpage_access import docker_runtime
    from local_webpage_access.docker_runtime import DockerRuntime

    captured: dict = {}

    def fake_execute(args, cwd=None, log_path=None, timeout=None, env=None):
        captured["timeout"] = timeout
        return _FakeResult()

    monkeypatch.setattr(docker_runtime, "_execute", fake_execute)
    return DockerRuntime(workspace), captured


def test_build_timeout_from_workspace_config(workspace, monkeypatch) -> None:
    """timeout=None 时取工作区配置 buildTimeout。"""
    (workspace.root / "local-web.yml").write_text("buildTimeout: 7200\n", encoding="utf-8")
    rt, captured = _runtime_with_capture(workspace, monkeypatch)
    rt.build("inst1")
    assert captured["timeout"] == 7200


def test_build_timeout_default_1800(workspace, monkeypatch) -> None:
    rt, captured = _runtime_with_capture(workspace, monkeypatch)
    rt.build("inst1")
    assert captured["timeout"] == 1800


def test_build_timeout_explicit_param_wins(workspace, monkeypatch) -> None:
    (workspace.root / "local-web.yml").write_text("buildTimeout: 7200\n", encoding="utf-8")
    rt, captured = _runtime_with_capture(workspace, monkeypatch)
    rt.build("inst1", timeout=60)
    assert captured["timeout"] == 60
