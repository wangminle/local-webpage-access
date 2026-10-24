"""BUG-966 / BUG-967：pnpm 锁版本兜底 + Node engines 双向兼容。

- BUG-966：无 packageManager 声明且 pnpm-lock.yaml ``lockfileVersion`` <9 时，
  安装命令钉 ``corepack enable && corepack pnpm@8.15.9 install --frozen-lockfile``；宿主侧对钉版
  形式的 corepack 缺失/PATH 版本回退给出可操作提示。
- BUG-967：engines.node 与宿主 Node 失配（过新/过旧）时的受控忽略
  （yarn ``--ignore-engines``、pnpm ``--config.engine-strict=false``）、
  range 求值、容器侧 Node 基础镜像降级（node:22/20-alpine）。
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_webpage_access.node_toolchain import (
    engines_mismatch_direction,
    inspect_node_toolchain,
    node_version_in_range,
    parse_node_version,
    read_node_engines,
)


@contextlib.contextmanager
def _attach_caplog(caplog: pytest.LogCaptureFixture, logger_name: str):
    """lwa 根 logger ``propagate=False``——caplog handler 须直接挂到目标 logger。"""
    logger = logging.getLogger(logger_name)
    logger.addHandler(caplog.handler)
    try:
        yield logger
    finally:
        logger.removeHandler(caplog.handler)


# ---- BUG-966：pnpm 安装命令钉版生成 -------------------------------------------


def _write_pkg(root: Path, data: dict) -> None:
    (root / "package.json").write_text(json.dumps(data), encoding="utf-8")


def test_pnpm_lock_v6_pins_pnpm8(tmp_path: Path) -> None:
    """无 packageManager + lockfileVersion 6.0 → 钉 pnpm@8.15.9。"""
    _write_pkg(tmp_path, {"name": "it-tools", "scripts": {"build": "vite build"}})
    (tmp_path / "pnpm-lock.yaml").write_text(
        "lockfileVersion: '6.0'\n\npackages:\n  /foo@1.0.0: {}\n", encoding="utf-8"
    )
    tool = inspect_node_toolchain(tmp_path)
    assert tool.install_command == "corepack enable && corepack pnpm@8.15.9 install --frozen-lockfile"


def test_pnpm_lock_v9_keeps_corepack_enable_form(tmp_path: Path) -> None:
    """lockfileVersion 9.x → corepack 默认最新 pnpm 可读，保持现状。"""
    _write_pkg(tmp_path, {"name": "app"})
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    tool = inspect_node_toolchain(tmp_path)
    assert tool.install_command == "corepack enable && pnpm install --frozen-lockfile"


def test_pnpm_declared_package_manager_pins_declared_version(tmp_path: Path) -> None:
    """声明优先：packageManager=pnpm@8.15.3 钉声明版（BUG-877 逻辑下沉）。"""
    _write_pkg(
        tmp_path,
        {"name": "app", "packageManager": "pnpm@8.15.3"},
    )
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '6.0'\n", encoding="utf-8")
    tool = inspect_node_toolchain(tmp_path)
    assert tool.install_command == "corepack enable && corepack pnpm@8.15.3 install --frozen-lockfile"


def test_pnpm_declared_sha_suffix_stripped(tmp_path: Path) -> None:
    """corepack ``+sha512`` 哈希后缀须剥离后才进 ``<pm>@<ver>`` CLI 参数。"""
    _write_pkg(
        tmp_path,
        {"name": "app", "packageManager": "pnpm@9.12.0+sha512.abcdef"},
    )
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    tool = inspect_node_toolchain(tmp_path)
    assert tool.install_command == "corepack enable && corepack pnpm@9.12.0 install --frozen-lockfile"


def test_pnpm_unreadable_lockfile_keeps_corepack_enable_form(tmp_path: Path) -> None:
    """lockfileVersion 缺失/不可读 → 宁保持现状不误钉。"""
    _write_pkg(tmp_path, {"name": "app"})
    (tmp_path / "pnpm-lock.yaml").write_text("{}\n", encoding="utf-8")
    tool = inspect_node_toolchain(tmp_path)
    assert tool.install_command == "corepack enable && pnpm install --frozen-lockfile"


def test_yarn_and_npm_templates_unchanged(tmp_path: Path) -> None:
    """BUG-966：yarn/bun/npm 模板不受 pnpm 钉版逻辑影响。"""
    _write_pkg(tmp_path, {"name": "app"})
    (tmp_path / "yarn.lock").write_text("# yarn lockfile\n", encoding="utf-8")
    assert (
        inspect_node_toolchain(tmp_path).install_command
        == "corepack enable && yarn install --frozen-lockfile"
    )


# ---- BUG-966：宿主侧钉版形式的三分支解析 --------------------------------------


def _fake_which(monkeypatch: pytest.MonkeyPatch, available: set[str]) -> None:
    monkeypatch.setattr(
        "local_webpage_access.hosting.shutil.which",
        lambda name, path=None: f"/usr/bin/{name}" if name in available else None,
    )


def _fake_pm_version(monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    monkeypatch.setattr(
        "local_webpage_access.hosting.subprocess.run",
        lambda *a, **k: SimpleNamespace(stdout=f"{version}\n", stderr=""),
    )


def test_resolve_pinned_corepack_available_keeps_pinned(tmp_path: Path, monkeypatch) -> None:
    """钉版形式 + corepack 可用 → 原样（已钉版）。"""
    from local_webpage_access.hosting import _resolve_host_install_command

    _fake_which(monkeypatch, {"corepack"})
    cmd = "corepack pnpm@8.15.9 install --frozen-lockfile"
    assert _resolve_host_install_command(cmd, tmp_path) == cmd


def test_resolve_pinned_path_matching_major_drops_pin(tmp_path: Path, monkeypatch) -> None:
    """钉版形式 + 无 corepack + PATH pnpm major 一致 → 去钉版用 PATH。"""
    from local_webpage_access.hosting import _resolve_host_install_command

    _fake_which(monkeypatch, {"pnpm"})
    _fake_pm_version(monkeypatch, "8.15.3")
    cmd = "corepack pnpm@8.15.9 install --frozen-lockfile"
    assert _resolve_host_install_command(cmd, tmp_path) == "pnpm install --frozen-lockfile"


def test_resolve_pinned_path_mismatch_major_warns_and_keeps(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """钉版形式 + PATH 版本 major 不一致 → 保留钉版命令 + 可操作告警。"""
    from local_webpage_access.hosting import _resolve_host_install_command

    _fake_which(monkeypatch, {"pnpm"})
    _fake_pm_version(monkeypatch, "10.2.0")
    cmd = "corepack pnpm@8.15.9 install --frozen-lockfile"
    with _attach_caplog(caplog, "local_webpage_access.hosting"):
        assert _resolve_host_install_command(cmd, tmp_path) == cmd
    assert any(
        "不匹配" in r.getMessage() and "corepack" in r.getMessage() and "8.15.9" in r.getMessage()
        for r in caplog.records
    )


def test_resolve_pinned_nothing_available_actionable_warning(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """钉版形式 + corepack 与 pm 均缺失 → 原样 + 「npm i -g corepack」提示。"""
    from local_webpage_access.hosting import _resolve_host_install_command

    _fake_which(monkeypatch, set())
    cmd = "corepack pnpm@8.15.9 install --frozen-lockfile"
    with _attach_caplog(caplog, "local_webpage_access.hosting"):
        assert _resolve_host_install_command(cmd, tmp_path) == cmd
    assert any(
        "npm i -g corepack" in r.getMessage() and "不在 PATH" in r.getMessage()
        for r in caplog.records
    )


def test_resolve_corepack_enable_form_nothing_available_actionable_warning(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """旧 ``corepack enable &&`` 形式的「都不可用」告警同样升级为可操作提示。"""
    from local_webpage_access.hosting import _resolve_host_install_command

    _fake_which(monkeypatch, set())
    cmd = "corepack enable && yarn install --frozen-lockfile"
    with _attach_caplog(caplog, "local_webpage_access.hosting"):
        assert _resolve_host_install_command(cmd, tmp_path) == cmd
    assert any("npm i -g corepack" in r.getMessage() for r in caplog.records)


# ---- BUG-984：钉版保留 corepack enable（镜像内 pnpm shim 不变量）---------------


def test_pinned_install_command_keeps_corepack_enable(tmp_path: Path) -> None:
    """钉版命令必须含 ``corepack enable &&``——裸 ``corepack pnpm@X install``
    按需执行不生成 pnpm shim，生成容器下游 ``pnpm run build`` 会 exit 127。"""
    from local_webpage_access.node_toolchain import inspect_node_toolchain

    _write_pkg(tmp_path, {"name": "app"})
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '6.0'\n", encoding="utf-8")
    tool = inspect_node_toolchain(tmp_path)
    assert tool.install_command.startswith("corepack enable && corepack pnpm@8.15.9 ")


def test_resolve_enable_pinned_corepack_available_strips_enable(tmp_path: Path, monkeypatch) -> None:
    """enable+钉版形式 + corepack 可用 → 剥离全局 ``corepack enable``（普通用户
    写系统 Corepack bin 目录会 EACCES），保留钉版直跑；下游裸 pnpm 由 BUG-902
    工作区 shim 目录兜底。"""
    from local_webpage_access.hosting import _resolve_host_install_command

    _fake_which(monkeypatch, {"corepack"})
    cmd = "corepack enable && corepack pnpm@8.15.9 install --frozen-lockfile"
    assert (
        _resolve_host_install_command(cmd, tmp_path)
        == "corepack pnpm@8.15.9 install --frozen-lockfile"
    )


def test_resolve_enable_pinned_path_matching_major_drops_all(
    tmp_path: Path, monkeypatch
) -> None:
    """enable+钉版形式 + 无 corepack + PATH major 一致 → 去钉版去 enable 用 PATH。"""
    from local_webpage_access.hosting import _resolve_host_install_command

    _fake_which(monkeypatch, {"pnpm"})
    _fake_pm_version(monkeypatch, "8.15.3")
    cmd = "corepack enable && corepack pnpm@8.15.9 install --frozen-lockfile"
    assert _resolve_host_install_command(cmd, tmp_path) == "pnpm install --frozen-lockfile"


def test_resolve_enable_pinned_nothing_available_keeps_and_warns(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """enable+钉版形式 + 都不可用 → 原样 + 可操作告警。"""
    from local_webpage_access.hosting import _resolve_host_install_command

    _fake_which(monkeypatch, set())
    cmd = "corepack enable && corepack pnpm@8.15.9 install --frozen-lockfile"
    with _attach_caplog(caplog, "local_webpage_access.hosting"):
        assert _resolve_host_install_command(cmd, tmp_path) == cmd
    assert any("npm i -g corepack" in r.getMessage() for r in caplog.records)


def test_registry_injection_and_copy_block_with_enable_pinned(tmp_path: Path) -> None:
    """enable+钉版形式：--registry 注入版本段保留，锁文件 COPY 块识别不变。"""
    from local_webpage_access.dockerfile_templates import (
        _node_dependency_copy_block,
        _with_npm_registry,
    )

    cmd = "corepack enable && corepack pnpm@8.15.9 install --frozen-lockfile"
    assert (
        _with_npm_registry(cmd, _mirrors())
        == "corepack enable && corepack pnpm@8.15.9 install"
        " --registry=https://registry.npmmirror.com --frozen-lockfile"
    )
    block = _node_dependency_copy_block(cmd, source_dir=tmp_path)
    assert "pnpm-lock.yaml" in block


# ---- BUG-967：engines.node range 求值 -----------------------------------------


@pytest.mark.parametrize(
    ("range_text", "version", "expected"),
    [
        ("18.0.0 - 22.x.x", (24, 19, 0), False),
        ("18.0.0 - 22.x.x", (20, 0, 0), True),
        (">=18.17.0", (16, 0, 0), False),
        (">=18.17.0", (18, 17, 0), True),
        ("^20.0.0", (20, 9, 9), True),
        ("^20.0.0", (21, 0, 0), False),
        ("^0.2.3", (0, 2, 9), True),
        ("^0.2.3", (0, 3, 0), False),
        ("~20.1.0", (20, 1, 9), True),
        ("~20.1.0", (20, 2, 0), False),
        (">=16.0.0 || ^18.0.0", (17, 0, 0), True),
        (">=16.0.0 || ^18.0.0", (19, 0, 0), True),
        (">=16.0.0 || ^18.0.0", (15, 0, 0), False),
        ("22.x.x", (22, 99, 0), True),
        ("22.x.x", (23, 0, 0), False),
        (">=18.17.0 <21", (20, 99, 0), True),
        (">=18.17.0 <21", (21, 0, 0), False),
        ("*", (99, 0, 0), True),
    ],
)
def test_node_version_in_range(range_text, version, expected) -> None:
    assert node_version_in_range(range_text, version) is expected


@pytest.mark.parametrize(
    "range_text",
    ["garbage", "", ">=18.17.0-alpha", "node >= 18"],
)
def test_node_version_in_range_unparseable_returns_none(range_text) -> None:
    """无法解析的 range 宁返回 None 不误判（BUG-967）。"""
    assert node_version_in_range(range_text, (20, 0, 0)) is None


def test_parse_node_version() -> None:
    assert parse_node_version("v24.19.0") == (24, 19, 0)
    assert parse_node_version("24.19.0") == (24, 19, 0)
    assert parse_node_version("not-a-version") is None


def test_engines_mismatch_direction() -> None:
    assert engines_mismatch_direction("18.0.0 - 22.x.x", (24, 19, 0)) == "过新"
    assert engines_mismatch_direction(">=20.0.0", (18, 0, 0)) == "过旧"
    assert engines_mismatch_direction(">=16.0.0", (18, 0, 0)) is None


# ---- BUG-967：宿主 engines 受控忽略 --------------------------------------------


def _write_engines_pkg(root: Path, engines: str) -> None:
    _write_pkg(
        root,
        {"name": "excalidraw", "engines": {"node": engines}, "scripts": {"build": "vite build"}},
    )


def _fake_host_node(monkeypatch: pytest.MonkeyPatch, version: tuple[int, int, int]) -> None:
    monkeypatch.setattr(
        "local_webpage_access.node_toolchain.host_node_version", lambda: version
    )


def test_engines_mismatch_yarn_appends_ignore_engines(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """显式忽略（nodeEnginesIgnore）下宿主 Node 过新（Excalidraw 实案）→
    yarn install 追加 --ignore-engines + 告警。"""
    from local_webpage_access.hosting import _apply_engines_compat

    _write_engines_pkg(tmp_path, "18.0.0 - 22.x.x")
    _fake_host_node(monkeypatch, (24, 19, 0))
    with _attach_caplog(caplog, "local_webpage_access.hosting"):
        out = _apply_engines_compat(
            "corepack yarn@1.22.22 install --frozen-lockfile --ignore-scripts",
            tmp_path,
            ignore_engines=True,
        )
    assert out == (
        "corepack yarn@1.22.22 install --frozen-lockfile --ignore-scripts --ignore-engines"
    )
    assert any(
        "过新" in r.getMessage() and "18.0.0 - 22.x.x" in r.getMessage()
        for r in caplog.records
    )


def test_engines_mismatch_default_blocks_with_actionable_error(
    tmp_path: Path, monkeypatch
) -> None:
    """D3 边界：未显式选择时失配默认阻断（HostingError），提示兼容 Node 与
    nodeEnginesIgnore 开关，不默认忽略 engines（CHK-485）。"""
    from local_webpage_access.errors import HostingError
    from local_webpage_access.hosting import _apply_engines_compat

    _write_engines_pkg(tmp_path, "18.0.0 - 22.x.x")
    _fake_host_node(monkeypatch, (24, 19, 0))
    with pytest.raises(HostingError) as excinfo:
        _apply_engines_compat("yarn install --frozen-lockfile", tmp_path)
    message = str(excinfo.value)
    assert "nodeEnginesIgnore" in message
    assert "18.0.0 - 22.x.x" in message
    assert "24.19.0" in message
    assert "过新" in message


def test_engines_mismatch_yarn_too_old_direction(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """宿主 Node 过旧（Astro 方向）→ 告警文案标明「过旧」。"""
    from local_webpage_access.hosting import _apply_engines_compat

    _write_engines_pkg(tmp_path, ">=20.0.0")
    _fake_host_node(monkeypatch, (18, 0, 0))
    with _attach_caplog(caplog, "local_webpage_access.hosting"):
        out = _apply_engines_compat(
            "yarn install --frozen-lockfile", tmp_path, ignore_engines=True
        )
    assert out == "yarn install --frozen-lockfile --ignore-engines"
    assert any("过旧" in r.getMessage() for r in caplog.records)


def test_engines_mismatch_pinned_yarn_berry_not_ignored(tmp_path: Path, monkeypatch) -> None:
    """钉版 yarn major>=2（berry）不追加 --ignore-engines——支持不确定，如实失败。"""
    from local_webpage_access.hosting import _apply_engines_compat

    _write_engines_pkg(tmp_path, "18.0.0 - 22.x.x")
    _fake_host_node(monkeypatch, (24, 19, 0))
    cmd = "corepack yarn@4.1.1 install --immutable"
    assert _apply_engines_compat(cmd, tmp_path, ignore_engines=True) == cmd


def test_engines_mismatch_pnpm_engine_strict_disabled(tmp_path: Path, monkeypatch) -> None:
    """pnpm 仅在 .npmrc 声明 engine-strict=true 时追加 --config.engine-strict=false。"""
    from local_webpage_access.hosting import _apply_engines_compat

    _write_engines_pkg(tmp_path, "18.0.0 - 22.x.x")
    _fake_host_node(monkeypatch, (24, 19, 0))
    # 无 .npmrc：pnpm 默认不强制 engines，仅告警不追加
    cmd = "corepack pnpm@8.15.9 install --frozen-lockfile"
    assert _apply_engines_compat(cmd, tmp_path, ignore_engines=True) == cmd
    # .npmrc 声明 engine-strict=true（大小写/空格不敏感）→ 追加
    (tmp_path / ".npmrc").write_text("Engine-Strict = true\n", encoding="utf-8")
    assert (
        _apply_engines_compat(cmd, tmp_path, ignore_engines=True)
        == "corepack pnpm@8.15.9 install --frozen-lockfile --config.engine-strict=false"
    )


def test_engines_match_no_change(tmp_path: Path, monkeypatch) -> None:
    """宿主 Node 在范围内 → 命令原样。"""
    from local_webpage_access.hosting import _apply_engines_compat

    _write_engines_pkg(tmp_path, "18.0.0 - 22.x.x")
    _fake_host_node(monkeypatch, (20, 10, 0))
    cmd = "yarn install --frozen-lockfile"
    assert _apply_engines_compat(cmd, tmp_path) == cmd


def test_toolchain_notes_mention_engines_mismatch(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """导入提示（预检）：notes 里提示宿主 Node 不在 engines 范围内及方向。"""
    _write_engines_pkg(tmp_path, "18.0.0 - 22.x.x")
    _fake_host_node(monkeypatch, (24, 19, 0))
    with _attach_caplog(caplog, "local_webpage_access.node_toolchain"):
        tool = inspect_node_toolchain(tmp_path)
    assert any(
        "engines.node" in n and "24.19.0" in n and "过新" in n for n in tool.notes
    )


def test_read_node_engines(tmp_path: Path) -> None:
    assert read_node_engines(tmp_path) is None
    _write_engines_pkg(tmp_path, ">=18.0.0")
    assert read_node_engines(tmp_path) == ">=18.0.0"


# ---- BUG-966/967：容器路径 ----------------------------------------------------


def _mirrors(npm: str = "https://registry.npmmirror.com"):
    from local_webpage_access.config import BuildMirrors

    return BuildMirrors(npm=npm)


def test_with_npm_registry_keeps_pinned_pnpm_version() -> None:
    """``corepack pnpm@8.15.9 install`` 注入 --registry 后版本段保留。"""
    from local_webpage_access.dockerfile_templates import _with_npm_registry

    out = _with_npm_registry(
        "corepack pnpm@8.15.9 install --frozen-lockfile", _mirrors()
    )
    assert (
        out
        == "corepack pnpm@8.15.9 install --registry=https://registry.npmmirror.com --frozen-lockfile"
    )


def test_with_npm_registry_unpinned_forms_unchanged_behavior() -> None:
    from local_webpage_access.dockerfile_templates import _with_npm_registry

    assert (
        _with_npm_registry("corepack enable && pnpm install --frozen-lockfile", _mirrors())
        == "corepack enable && pnpm install --registry=https://registry.npmmirror.com --frozen-lockfile"
    )
    assert (
        _with_npm_registry("corepack enable && yarn install --frozen-lockfile", _mirrors())
        == "corepack enable && yarn install --registry=https://registry.npmmirror.com --frozen-lockfile"
    )
    assert (
        _with_npm_registry("corepack yarn@1.22.22 install --frozen-lockfile", _mirrors())
        == "corepack yarn@1.22.22 install --registry=https://registry.npmmirror.com --frozen-lockfile"
    )


def test_node_dependency_copy_block_matches_pinned_pnpm(tmp_path: Path) -> None:
    """钉版 pnpm 安装命令同样触发 pnpm-lock.yaml 拷贝块。"""
    from local_webpage_access.dockerfile_templates import _node_dependency_copy_block

    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    block = _node_dependency_copy_block(
        "corepack pnpm@8.15.9 install --frozen-lockfile", source_dir=tmp_path
    )
    assert "pnpm-lock.yaml" in block


def test_node_dependency_copy_block_matches_pinned_yarn(tmp_path: Path) -> None:
    from local_webpage_access.dockerfile_templates import _node_dependency_copy_block

    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    block = _node_dependency_copy_block(
        "corepack yarn@1.22.22 install --frozen-lockfile", source_dir=tmp_path
    )
    assert "yarn.lock" in block


def test_node_image_for_engines_capped_at_22(tmp_path: Path) -> None:
    """engines 上限 22（node:24 不在范围内）→ FROM 用 node:22-alpine。"""
    from local_webpage_access.dockerfile_templates import (
        _NODE_IMAGE,
        _node_image_for_engines,
    )

    assert _node_image_for_engines(tmp_path) == _NODE_IMAGE
    _write_engines_pkg(tmp_path, "18.0.0 - 22.x.x")
    assert _node_image_for_engines(tmp_path) == "node:22-alpine"


def test_node_image_for_engines_capped_at_20(tmp_path: Path) -> None:
    """22 也不在范围内而 20 在 → node:20-alpine。"""
    from local_webpage_access.dockerfile_templates import _node_image_for_engines

    _write_engines_pkg(tmp_path, ">=18.0.0 <21")
    assert _node_image_for_engines(tmp_path) == "node:20-alpine"


def test_node_image_for_engines_unparseable_keeps_default(tmp_path: Path) -> None:
    """engines 不可解析/20/22 均不满足 → 保持默认镜像（宁装不上不错装）。"""
    from local_webpage_access.dockerfile_templates import (
        _NODE_IMAGE,
        _node_image_for_engines,
    )

    _write_engines_pkg(tmp_path, "garbage")
    assert _node_image_for_engines(tmp_path) == _NODE_IMAGE
    _write_engines_pkg(tmp_path, "^24.0.0")
    assert _node_image_for_engines(tmp_path) == _NODE_IMAGE


# ---- BUG-966 修订（CHK-485）：lockfileVersion 精确钉版映射 --------------------


@pytest.mark.parametrize(
    ("lockfile_version", "expected_pin"),
    [
        ("5.1", "6.35.1"),   # pnpm 5/6 世代 → pnpm 6 末版
        ("5.3", "6.35.1"),
        ("5.4", "7.33.7"),   # pnpm 7 → pnpm 7 末版
        ("6.0", "8.15.9"),   # pnpm 8 → pnpm 8 末版
    ],
)
def test_pnpm_lock_exact_version_mapping(
    tmp_path: Path, lockfile_version: str, expected_pin: str
) -> None:
    """旧锁按 lockfileVersion 精确钉生成世代末版——统一钉 8 对 5.3/5.4 锁
    会 ERR_PNPM_FROZEN_LOCKFILE_WITH_OUTDATED_LOCKFILE（CHK-485 实测）。"""
    _write_pkg(tmp_path, {"name": "app"})
    (tmp_path / "pnpm-lock.yaml").write_text(
        f"lockfileVersion: '{lockfile_version}'\n", encoding="utf-8"
    )
    tool = inspect_node_toolchain(tmp_path)
    assert (
        tool.install_command
        == f"corepack enable && corepack pnpm@{expected_pin} install --frozen-lockfile"
    )


def test_pnpm_lock_unknown_major_not_pinned(tmp_path: Path) -> None:
    """lockfileVersion <5 无可靠映射 → 不钉（宁保持现状不误钉）。"""
    _write_pkg(tmp_path, {"name": "app"})
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '4.0'\n", encoding="utf-8")
    tool = inspect_node_toolchain(tmp_path)
    assert tool.install_command == "corepack enable && pnpm install --frozen-lockfile"


# ---- BUG-967 修订（CHK-485）：caret 短范围与 major 线交集 ----------------------


@pytest.mark.parametrize(
    ("range_text", "version", "expected"),
    [
        ("^22.12", (22, 15, 0), True),   # CHK-485 实测误判 False 的用例
        ("^22.12", (22, 12, 0), True),
        ("^22.12", (22, 11, 9), False),
        ("^22.12", (23, 0, 0), False),
        ("^0.2", (0, 2, 5), True),
        ("^0.2", (0, 3, 0), False),
        ("^0.0", (0, 0, 9), True),
        ("^0.0", (0, 1, 0), False),
        ("^0", (0, 9, 9), True),
        ("^0", (1, 0, 0), False),
        ("^22", (22, 99, 0), True),
        ("^22", (23, 0, 0), False),
    ],
)
def test_caret_short_range_npm_semantics(range_text, version, expected) -> None:
    """caret 缺段按 npm 语义展开（锁最左非零段），不再套 tilde 上界。"""
    assert node_version_in_range(range_text, version) is expected


def test_range_major_intersection() -> None:
    """major 线交集：上界延伸到线尾 → hi=None；线内有上界 → 返回线内区间。"""
    from local_webpage_access.node_toolchain import node_range_major_intersection

    # >=22.12.0 <23 与 22 线交集延伸到线尾（moving tag 即可）
    assert node_range_major_intersection(">=22.12.0 <23", 22) == ((22, 12, 0), None)
    # 与 24 线无交集
    assert node_range_major_intersection(">=22.12.0 <23", 24) is False
    # 线内有上界：<24.10.0 → 线内 [24.0.0, 24.10.0)
    assert node_range_major_intersection(">=24.0.0 <24.10.0", 24) == ((24, 0, 0), (24, 10, 0))
    # 连字符范围与 22 线交集延伸到线尾
    assert node_range_major_intersection("18.0.0 - 22.x.x", 22) == ((22, 0, 0), None)
    # 不可解析 → None（宁未知不误判）
    assert node_range_major_intersection("garbage", 22) is None


def test_node_image_exact_version_mismatch_pins_minor(tmp_path: Path) -> None:
    """``>=24.0.0 <24.10.0``：24.0.0 满足不代表默认 24.22.1 满足——按线内
    上界钉次版本 node:24.9-alpine（CHK-485 实测误判用例）。"""
    from local_webpage_access.dockerfile_templates import _node_image_for_engines

    _write_engines_pkg(tmp_path, ">=24.0.0 <24.10.0")
    assert _node_image_for_engines(tmp_path) == "node:24.9-alpine"


def test_node_image_range_inside_22_line_selects_22(tmp_path: Path) -> None:
    """``>=22.12.0 <23``：22.0.0 不满足不代表 22 线不满足——选 node:22-alpine
    （CHK-485 实测误判用例）。"""
    from local_webpage_access.dockerfile_templates import _node_image_for_engines

    _write_engines_pkg(tmp_path, ">=22.12.0 <23")
    assert _node_image_for_engines(tmp_path) == "node:22-alpine"


def test_node_image_minor_pin_within_22_line(tmp_path: Path) -> None:
    """交集在 22 线内有上界 → 钉线内最高可用次版本 node:22.4-alpine。"""
    from local_webpage_access.dockerfile_templates import _node_image_for_engines

    _write_engines_pkg(tmp_path, ">=22.0.0 <22.5.0")
    assert _node_image_for_engines(tmp_path) == "node:22.4-alpine"


def test_node_image_18_line_supported(tmp_path: Path) -> None:
    """候选线含 18：``^18`` → node:18-alpine。"""
    from local_webpage_access.dockerfile_templates import _node_image_for_engines

    _write_engines_pkg(tmp_path, "^18")
    assert _node_image_for_engines(tmp_path) == "node:18-alpine"


def test_node_image_no_compatible_line_keeps_default_and_warns(
    tmp_path: Path, caplog
) -> None:
    """所有候选线均无交集（>=14 <16）→ 保持默认镜像并告警，容器内如实失败。"""
    from local_webpage_access.dockerfile_templates import (
        _NODE_IMAGE,
        _node_image_for_engines,
    )

    _write_engines_pkg(tmp_path, ">=14.0.0 <16.0.0")
    with _attach_caplog(caplog, "local_webpage_access.dockerfile"):
        assert _node_image_for_engines(tmp_path) == _NODE_IMAGE
    assert any("engines.node" in r.getMessage() for r in caplog.records)


# ---- 评审回归：range 解析与方向判定边界（BUG-995）------------------------------


def test_range_operator_followed_by_space_parses() -> None:
    """``>= 18.0.0``（比较符后带空格）须正常解析——旧 _RANGE_ATOM_RE 会把
    ``>=`` 拆成独立 token，使整个 range 解析失败返回 None（校验静默失效）。"""
    from local_webpage_access.node_toolchain import node_range_major_intersection

    assert node_version_in_range(">= 18.0.0", (20, 0, 0)) is True
    assert node_version_in_range(">= 18.0.0 < 23.0.0", (24, 0, 0)) is False
    assert node_range_major_intersection(">= 18.0.0 < 23.0.0", 22) == ((22, 0, 0), None)


def test_engines_mismatch_direction_cross_major_minor_floor() -> None:
    """``^22.12.0`` + 宿主 20.10.0：跨 major 只探 (22,0,0) 会漏报——
    须同时探 (22,99,99)，判「过旧」。"""
    assert engines_mismatch_direction("^22.12.0", (20, 10, 0)) == "过旧"


def test_engines_mismatch_direction_same_line_below_floor() -> None:
    """``^20.19.0 || >=22.12.0`` + 宿主 22.2.0：宿主在 22 线内但低于次版本
    下界，是「过旧」——不能因 20 线可满足而误报「过新」。"""
    assert engines_mismatch_direction("^20.19.0 || >=22.12.0", (22, 2, 0)) == "过旧"


def test_engines_mismatch_direction_same_line_above_ceiling() -> None:
    """宿主在同 major 线内但高于上界（``>=22.0.0 <22.5.0`` + 22.9.0）→「过新」。"""
    assert engines_mismatch_direction(">=22.0.0 <22.5.0", (22, 9, 0)) == "过新"


def test_pnpm_lockfile_fallback_version_mapping(tmp_path: Path) -> None:
    """锁版本兜底映射：6.0→pnpm 8 末版、5.4→pnpm 7 末版、5.0~5.3→pnpm 6
    末版；>=9 / 缺失 / 未知版本 → None（不钉）。"""
    from local_webpage_access.node_toolchain import pnpm_lockfile_fallback_version

    assert pnpm_lockfile_fallback_version(tmp_path) is None
    for lock_ver, expected in (
        ("6.0", "8.15.9"),
        ("5.4", "7.33.7"),
        ("5.3", "6.35.1"),
        ("5.0", "6.35.1"),
    ):
        (tmp_path / "pnpm-lock.yaml").write_text(
            f"lockfileVersion: '{lock_ver}'\n", encoding="utf-8"
        )
        assert pnpm_lockfile_fallback_version(tmp_path) == expected
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    assert pnpm_lockfile_fallback_version(tmp_path) is None


# ---- 评审回归：monorepo workspace_root 回退（BUG-995）--------------------------


def test_engines_compat_falls_back_to_workspace_root(tmp_path: Path, monkeypatch) -> None:
    """monorepo：engines.node 声明在 workspace 根而非 sourceSubdir 子目录，
    _apply_engines_compat 须回退 workspace_root 判定（与 inspect_node_toolchain
    同口径），否则失配漏检。"""
    from local_webpage_access.errors import HostingError
    from local_webpage_access.hosting import _apply_engines_compat

    sub = tmp_path / "web"
    sub.mkdir()
    _write_pkg(tmp_path, {"name": "mono", "engines": {"node": "18.0.0 - 22.x.x"}})
    _fake_host_node(monkeypatch, (24, 19, 0))
    # 不传 workspace_root：子目录读不到 engines → 不阻断（旧行为）
    assert _apply_engines_compat("yarn install --frozen-lockfile", sub) == (
        "yarn install --frozen-lockfile"
    )
    # 传 workspace_root：回退读到根的 engines → 默认阻断
    with pytest.raises(HostingError):
        _apply_engines_compat(
            "yarn install --frozen-lockfile", sub, workspace_root=tmp_path
        )


def test_engines_compat_npmrc_engine_strict_at_workspace_root(
    tmp_path: Path, monkeypatch
) -> None:
    """monorepo：.npmrc engine-strict=true 声明在 workspace 根时，pnpm 失配
    忽略同样生效（子目录无 .npmrc 也要命中根的声明）。"""
    from local_webpage_access.hosting import _apply_engines_compat

    sub = tmp_path / "web"
    sub.mkdir()
    _write_pkg(tmp_path, {"name": "mono", "engines": {"node": "18.0.0 - 22.x.x"}})
    (tmp_path / ".npmrc").write_text("engine-strict=true\n", encoding="utf-8")
    _fake_host_node(monkeypatch, (24, 19, 0))
    cmd = "corepack pnpm@8.15.9 install --frozen-lockfile"
    assert _apply_engines_compat(
        cmd, sub, ignore_engines=True, workspace_root=tmp_path
    ) == (f"{cmd} --config.engine-strict=false")


# ---- 评审回归：build 段钉版透传（BUG-994）--------------------------------------


def test_build_segment_pinned_passthrough_beats_path(tmp_path: Path, monkeypatch) -> None:
    """仅靠 pnpm-lock.yaml 兜底钉版（无 packageManager 声明）时，install 已用
    corepack pnpm@8.15.9，build 段也须钉同一版本——不能退回 PATH 上的 pnpm 10。"""
    from local_webpage_access.hosting import _resolve_build_segment

    _write_pkg(tmp_path, {"name": "app"})
    _fake_which(monkeypatch, {"corepack", "pnpm"})
    assert (
        _resolve_build_segment("pnpm run build", tmp_path, pinned=("pnpm", "8.15.9"))
        == "corepack pnpm@8.15.9 run build"
    )


def test_build_segment_declared_package_manager_beats_pinned(
    tmp_path: Path, monkeypatch
) -> None:
    """目录有 packageManager 声明时声明优先于 install 钉版透传。"""
    from local_webpage_access.hosting import _resolve_build_segment

    _write_pkg(tmp_path, {"name": "app", "packageManager": "pnpm@9.1.0"})
    _fake_which(monkeypatch, {"corepack", "pnpm"})
    assert (
        _resolve_build_segment("pnpm run build", tmp_path, pinned=("pnpm", "8.15.9"))
        == "corepack pnpm@9.1.0 run build"
    )


def test_build_segment_pinned_without_corepack_falls_back_to_path(
    tmp_path: Path, monkeypatch
) -> None:
    """钉版透传需要 corepack；corepack 缺失而 PATH 有 pnpm 时回退原样。"""
    from local_webpage_access.hosting import _resolve_build_segment

    _write_pkg(tmp_path, {"name": "app"})
    _fake_which(monkeypatch, {"pnpm"})
    assert (
        _resolve_build_segment("pnpm run build", tmp_path, pinned=("pnpm", "8.15.9"))
        == "pnpm run build"
    )


def test_build_segment_no_pin_no_corepack_path_unchanged(
    tmp_path: Path, monkeypatch
) -> None:
    """无钉版透传、PATH 有 pnpm → 原样（既有行为不变）。"""
    from local_webpage_access.hosting import _resolve_build_segment

    _write_pkg(tmp_path, {"name": "app"})
    _fake_which(monkeypatch, {"pnpm"})
    assert _resolve_build_segment("pnpm run build", tmp_path) == "pnpm run build"
