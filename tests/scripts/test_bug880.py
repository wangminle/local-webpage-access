"""BUG-880 回归：根 start 脚本显式指向的子包优先于 monorepo 选主决策表。

真实 excalidraw 仓库（tests/evals 冻结 old/new）：根 start 为
``yarn --cwd ./excalidraw-app start``，workspaces 含 examples/*——其中
examples/with-nextjs 是唯一 web_server，按 §4.4 选主表被误选为主包，
生成 backend-container + ``npm run start -w with-nextjs``，偏离根入口
声明的绘图主应用（独立复核 design/5-checkpoints/2026-10-16/
eval-findings-verification 复现确认）。
"""

from __future__ import annotations

import json
from pathlib import Path

from local_webpage_access.models import Kind, Runtime
from local_webpage_access.package_classifier import classify_and_select
from local_webpage_access.scanner import Scanner


def _write_package_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _make_excalidraw_style_repo(root: Path, *, root_start: str | None) -> None:
    """excalidraw 真实结构：绘图主应用（frontend_build）+ next 示例（唯一
    web_server）+ library 包。"""
    scripts = {"start": root_start} if root_start is not None else {}
    _write_package_json(
        root / "package.json",
        {
            "name": "excalidraw-monorepo",
            "private": True,
            "workspaces": ["excalidraw-app", "packages/*", "examples/*"],
            "scripts": scripts,
        },
    )
    _write_package_json(
        root / "excalidraw-app/package.json",
        {
            "name": "excalidraw-app",
            "dependencies": {"react": "^19.0.0", "vite": "^6.0.0"},
            "scripts": {"build": "vite build", "start": "vite preview"},
        },
    )
    _write_package_json(
        root / "examples/with-nextjs/package.json",
        {
            "name": "with-nextjs",
            "dependencies": {"next": "^15.0.0", "react": "^19.0.0"},
            "scripts": {"build": "next build", "start": "next start"},
        },
    )
    _write_package_json(
        root / "packages/excalidraw/package.json",
        {"name": "@excalidraw/excalidraw", "main": "dist/index.js"},
    )


def test_root_start_cwd_dir_overrides_single_web_server(tmp_path: Path) -> None:
    """BUG-880 主场景：根 start ``yarn --cwd ./excalidraw-app start`` 时
    选主不得被唯一 web_server 示例（examples/with-nextjs）劫持。"""
    _make_excalidraw_style_repo(root=tmp_path, root_start="yarn --cwd ./excalidraw-app start")
    result = classify_and_select(tmp_path)
    assert result.primary is not None
    assert result.primary.path == "excalidraw-app"
    assert any("根 start 脚本指定" in n for n in result.notes)


def test_root_start_override_end_to_end_scanner_shape(tmp_path: Path) -> None:
    """BUG-880：scanner 端到端——选主修正后整体形态为 frontend-static，
    build 锚定 excalidraw-app，不再是 backend-container + with-nextjs。"""
    _make_excalidraw_style_repo(root=tmp_path, root_start="yarn --cwd ./excalidraw-app start")
    result = Scanner().detect(tmp_path)
    assert result.pending is False
    assert result.kind == Kind.NODE
    assert result.runtime == Runtime.SHARED_STATIC
    assert result.form == "frontend-static"
    assert result.entry.build == "npm run build -w excalidraw-app"
    assert result.entry.buildOutputDir == "excalidraw-app/dist"
    assert "with-nextjs" not in (result.entry.build or "")


def test_root_start_yarn_workspace_name_form(tmp_path: Path) -> None:
    """``yarn workspace <name>`` 按包名匹配，同样优先于选主表。"""
    _make_excalidraw_style_repo(root=tmp_path, root_start="yarn workspace excalidraw-app start")
    result = classify_and_select(tmp_path)
    assert result.primary is not None
    assert result.primary.path == "excalidraw-app"


def test_root_start_flag_equals_form(tmp_path: Path) -> None:
    """``pnpm --dir=<dir>`` / ``npm --prefix=<dir>`` 等号写法同样识别。"""
    _make_excalidraw_style_repo(root=tmp_path, root_start="pnpm --dir=excalidraw-app start")
    result = classify_and_select(tmp_path)
    assert result.primary is not None
    assert result.primary.path == "excalidraw-app"


def test_no_root_start_keeps_decision_table(tmp_path: Path) -> None:
    """对照：根无 start 脚本时维持 §4.4 决策表（唯一 web_server 当选）。"""
    _make_excalidraw_style_repo(root=tmp_path, root_start=None)
    result = classify_and_select(tmp_path)
    assert result.primary is not None
    assert result.primary.path == "examples/with-nextjs"


def test_root_start_unrelated_script_keeps_decision_table(tmp_path: Path) -> None:
    """对照：根 start 未指向任何子包（普通脚本）时维持决策表。"""
    _make_excalidraw_style_repo(root=tmp_path, root_start="node server.js")
    result = classify_and_select(tmp_path)
    assert result.primary is not None
    assert result.primary.path == "examples/with-nextjs"


def test_root_start_target_not_deployable_falls_back(tmp_path: Path) -> None:
    """根 start 指向不可部署子包（library）时不强行选主，回落决策表。"""
    _make_excalidraw_style_repo(
        root=tmp_path, root_start="yarn --cwd ./packages/excalidraw start"
    )
    result = classify_and_select(tmp_path)
    assert result.primary is not None
    assert result.primary.path == "examples/with-nextjs"


def test_root_start_points_outside_workspaces_keeps_table(tmp_path: Path) -> None:
    """根 start 指向 workspaces 之外（含越界 ..）时不得匹配，维持决策表。"""
    _make_excalidraw_style_repo(root=tmp_path, root_start="yarn --cwd ../evil start")
    result = classify_and_select(tmp_path)
    assert result.primary is not None
    assert result.primary.path == "examples/with-nextjs"


def test_root_start_pnpm_filter_name_form(tmp_path: Path) -> None:
    """BUG-892：``pnpm --filter <name>`` / ``-F`` 按包名定位主应用。"""
    _make_excalidraw_style_repo(
        root=tmp_path, root_start="pnpm --filter excalidraw-app start"
    )
    result = classify_and_select(tmp_path)
    assert result.primary is not None
    assert result.primary.path == "excalidraw-app"

    _make_excalidraw_style_repo(root=tmp_path, root_start="pnpm -F excalidraw-app run start")
    result = classify_and_select(tmp_path)
    assert result.primary is not None
    assert result.primary.path == "excalidraw-app"


def test_root_start_npm_workspace_forms(tmp_path: Path) -> None:
    """BUG-892：``npm -w <name>`` / ``--workspace=<name>`` 按包名定位主应用。"""
    _make_excalidraw_style_repo(
        root=tmp_path, root_start="npm run start -w excalidraw-app"
    )
    result = classify_and_select(tmp_path)
    assert result.primary is not None
    assert result.primary.path == "excalidraw-app"

    _make_excalidraw_style_repo(
        root=tmp_path, root_start="npm --workspace=excalidraw-app run start"
    )
    result = classify_and_select(tmp_path)
    assert result.primary is not None
    assert result.primary.path == "excalidraw-app"


def test_root_start_pnpm_filter_accepts_path_form(tmp_path: Path) -> None:
    """BUG-892：pnpm --filter 的值也接受子包相对路径（./excalidraw-app）。"""
    _make_excalidraw_style_repo(
        root=tmp_path, root_start="pnpm --filter ./excalidraw-app start"
    )
    result = classify_and_select(tmp_path)
    assert result.primary is not None
    assert result.primary.path == "excalidraw-app"


def test_non_pm_command_dir_flag_keeps_decision_table(tmp_path: Path) -> None:
    """BUG-881：普通命令的 --dir/--prefix 参数不是包管理器定位选项——
    ``node server.js --dir excalidraw-app`` 不得劫持选主（复核反例）。"""
    _make_excalidraw_style_repo(
        root=tmp_path, root_start="node server.js --dir excalidraw-app"
    )
    result = classify_and_select(tmp_path)
    assert result.primary is not None
    assert result.primary.path == "examples/with-nextjs"


def test_pm_command_with_other_pm_flag_keeps_decision_table(tmp_path: Path) -> None:
    """BUG-881：目录选项按包管理器归属——``yarn build --dir excalidraw-app``
    的 --dir 是 pnpm 选项（yarn 用 --cwd），不得作为定位提示。"""
    _make_excalidraw_style_repo(
        root=tmp_path, root_start="yarn build --dir excalidraw-app"
    )
    result = classify_and_select(tmp_path)
    assert result.primary is not None
    assert result.primary.path == "examples/with-nextjs"


def test_arg_forwarding_after_separator_keeps_decision_table(tmp_path: Path) -> None:
    """BUG-881 残留：``--`` 之后是转发给目标脚本的参数——
    ``npm run serve -- --prefix excalidraw-app`` 的 --prefix 属于应用
    （实测 npm 退出码 0、应用 argv 收到该参数），不得据此选主。"""
    for start in (
        "npm run serve -- --prefix excalidraw-app",
        "pnpm run serve -- --dir=excalidraw-app",
    ):
        _make_excalidraw_style_repo(root=tmp_path, root_start=start)
        result = classify_and_select(tmp_path)
        assert result.primary is not None
        assert result.primary.path == "examples/with-nextjs", start


def test_pm_dir_flag_before_separator_still_overrides(tmp_path: Path) -> None:
    """BUG-881 对照：定位选项在 ``--`` 之前仍是包管理器自身的——
    ``npm --prefix excalidraw-app run start -- --port 3000`` 照常选主。"""
    _make_excalidraw_style_repo(
        root=tmp_path,
        root_start="npm --prefix excalidraw-app run start -- --port 3000",
    )
    result = classify_and_select(tmp_path)
    assert result.primary is not None
    assert result.primary.path == "excalidraw-app"
