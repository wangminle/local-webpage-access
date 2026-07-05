"""pytest 共享夹具。"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_web_access.paths import Workspace


@pytest.fixture()
def workspace_root(tmp_path: Path) -> Path:
    """返回一个空的工作区根目录。"""
    return tmp_path / "ws"


@pytest.fixture()
def workspace(workspace_root: Path) -> Workspace:
    """返回一个已创建顶层目录的 Workspace。"""
    ws = Workspace(workspace_root)
    ws.ensure_workspace_dirs()
    return ws
