"""paths 模块测试（WBS-02）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_web_access.errors import PathError
from local_web_access.paths import (
    Workspace,
    find_workspace_root,
    require_workspace,
    validate_instance_id,
)


def test_workspace_top_level_dirs(workspace: Workspace) -> None:
    for attr in ("inbox", "apps", "registry_dir", "logs", "run", "templates", "skills"):
        path: Path = getattr(workspace, attr)
        assert path.is_dir(), f"{attr} 应为目录"
        assert path.parent == workspace.root


def test_db_path_under_registry(workspace: Workspace) -> None:
    assert workspace.db_path == workspace.registry_dir / "local-web.db"


def test_static_sites_under_gateway(workspace: Workspace) -> None:
    assert workspace.static_sites == workspace.static_gateway / "sites"


def test_app_dir_layout(workspace: Workspace) -> None:
    wid = "my-demo"
    assert workspace.app_dir(wid) == workspace.apps / wid
    assert workspace.app_original_zip(wid) == workspace.apps / wid / "source" / "original.zip"
    assert workspace.app_current(wid) == workspace.apps / wid / "current"
    assert workspace.app_public(wid) == workspace.apps / wid / "public"
    assert workspace.app_data(wid) == workspace.apps / wid / "data"
    assert workspace.app_logs(wid) == workspace.apps / wid / "logs"
    assert workspace.app_compose_path(wid) == workspace.apps / wid / "docker" / "compose.yaml"
    assert workspace.app_dockerfile_path(wid) == workspace.apps / wid / "docker" / "Dockerfile"
    assert workspace.app_manifest_path(wid) == workspace.apps / wid / "local-web.json"
    assert workspace.app_gateway_config(wid) == workspace.static_sites / "my-demo.conf"


def test_ensure_app_dirs_creates_all(workspace: Workspace) -> None:
    workspace.ensure_app_dirs("demo")
    for fn in (
        workspace.app_source,
        workspace.app_current,
        workspace.app_public,
        workspace.app_data,
        workspace.app_logs,
        workspace.app_docker,
    ):
        assert fn("demo").is_dir()


def test_ensure_workspace_dirs_idempotent(workspace: Workspace) -> None:
    # 重复调用不应报错
    workspace.ensure_workspace_dirs()
    workspace.ensure_app_dirs("x")
    workspace.ensure_app_dirs("x")


def test_find_workspace_root(tmp_path: Path) -> None:
    ws_root = tmp_path / "ws"
    ws = Workspace(ws_root)
    ws.ensure_workspace_dirs()
    (ws_root / "local-web.yml").write_text("managerPort: 17800\n", encoding="utf-8")

    nested = ws_root / "apps" / "demo" / "current"
    nested.mkdir(parents=True)
    assert find_workspace_root(nested) == ws_root
    assert find_workspace_root(ws_root) == ws_root


def test_find_workspace_root_not_found(tmp_path: Path) -> None:
    assert find_workspace_root(tmp_path) is None


def test_require_workspace_raises(tmp_path: Path) -> None:
    with pytest.raises(PathError):
        require_workspace(tmp_path)


def test_workspace_resolves_to_absolute(tmp_path: Path) -> None:
    ws = Workspace(tmp_path / "relative" / ".." / "relative")
    assert ws.root.is_absolute()


# ---- 回归测试：BUG-025 ----------------------------------------------------
#
# BUG-025：``app_dir`` / 锁路径直接用 ``instance_id`` 拼接，``..`` / ``/`` 等
#          片段会让 ``shutil.rmtree(app_dir(".."))`` 越界删工作区根。修复后
#          ``validate_instance_id`` 在所有路径 sink 前拦截非法 ID。


@pytest.mark.parametrize("iid", ["demo", "my-app", "a", "inst-1", "abc123"])
def test_validate_instance_id_accepts_slug(iid: str) -> None:
    assert validate_instance_id(iid) == iid


@pytest.mark.parametrize(
    "iid",
    [
        "..",
        ".",
    "../secret",
        "a/b",
        "a\\b",
        ".hidden",
        "trailing.",
        "double..dot",
        "UPPER",
        "with space",
        "",
        "emoji-😄",
    ],
)
def test_validate_instance_id_rejects_traversal(iid: str) -> None:
    with pytest.raises(PathError):
        validate_instance_id(iid)


@pytest.mark.parametrize(
    "sink",
    ["app_dir", "app_gateway_config", "app_data", "app_manifest_path"],
)
def test_app_sinks_reject_traversal_id(workspace: Workspace, sink: str) -> None:
    """BUG-025：所有路径 sink 遇非法 ID 必抛 PathError，绝不拼接越界路径。"""
    with pytest.raises(PathError):
        getattr(workspace, sink)("..")


def test_app_dir_valid_id_stays_under_apps(workspace: Workspace) -> None:
    """BUG-025：合法 ID 解析后必须落在 apps/ 之内。"""
    resolved = workspace.app_dir("demo").resolve()
    assert resolved.is_relative_to(workspace.apps.resolve())
