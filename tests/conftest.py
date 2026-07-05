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


@pytest.fixture()
def registry(workspace_root: Path):
    """打开一个临时 registry，测试结束自动关闭。"""
    from local_web_access.registry import Registry

    workspace_root.joinpath("registry").mkdir(parents=True, exist_ok=True)
    reg = Registry(workspace_root / "registry" / "local-web.db")
    reg.open()
    yield reg
    reg.close()


@pytest.fixture()
def config(workspace_root: Path):
    from local_web_access.config import Config, PortPool

    return Config(portPool=PortPool(start=21000, end=21050))
