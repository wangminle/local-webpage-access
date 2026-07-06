"""日志读取与滚动测试（WBS-18.01/02/03/11）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_web_access.logs import (
    DEFAULT_MAX_BYTES,
    LogInfo,
    list_logs,
    log_path,
    read_log,
    rotate_all,
    rotate_log,
)
from local_web_access.paths import Workspace
from local_web_access.errors import PathError


@pytest.fixture()
def workspace(workspace_root: Path) -> Workspace:
    ws = Workspace(workspace_root)
    ws.ensure_workspace_dirs()
    return ws


def _write(workspace: Workspace, iid: str, category: str, content: str) -> None:
    p = log_path(workspace, iid, category)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ---- read_log --------------------------------------------------------------


def test_read_log_missing_returns_empty(workspace) -> None:
    assert read_log(workspace, "api", "build") == ""


def test_read_log_returns_full_when_no_tail(workspace) -> None:
    _write(workspace, "api", "build", "line1\nline2\nline3\n")
    assert read_log(workspace, "api", "build", tail=0) == "line1\nline2\nline3\n"


def test_read_log_returns_last_n_lines(workspace) -> None:
    _write(workspace, "api", "build", "\n".join(f"l{i}" for i in range(10)))
    text = read_log(workspace, "api", "build", tail=3)
    assert text == "l7\nl8\nl9"


def test_read_log_tail_larger_than_file(workspace) -> None:
    _write(workspace, "api", "run", "only\n")
    assert read_log(workspace, "api", "run", tail=100) == "only"


def test_read_log_rejects_invalid_category(workspace) -> None:
    """BUG-040：category 不得用于路径穿越。"""
    workspace.ensure_app_dirs("api")
    secret = workspace.logs / "secret.log"
    secret.write_text("secret", encoding="utf-8")
    with pytest.raises(PathError):
        read_log(workspace, "api", "../../logs/secret", tail=10)


# ---- list_logs -------------------------------------------------------------


def test_list_logs_empty(workspace) -> None:
    workspace.ensure_app_dirs("api")
    assert list_logs(workspace, "api") == []


def test_list_logs_returns_all_categories(workspace) -> None:
    workspace.ensure_app_dirs("api")
    _write(workspace, "api", "build", "x")
    _write(workspace, "api", "run", "y")
    _write(workspace, "api", "gateway", "z")
    infos = list_logs(workspace, "api")
    cats = {i.category for i in infos}
    assert cats == {"build", "run", "gateway"}
    for info in infos:
        assert isinstance(info, LogInfo)
        assert info.size > 0
        assert info.exists


# ---- rotate_log ------------------------------------------------------------


def test_rotate_log_under_threshold_no_op(workspace) -> None:
    _write(workspace, "api", "build", "small")
    rotated = rotate_log(workspace, "api", "build", max_bytes=DEFAULT_MAX_BYTES)
    assert rotated is False
    assert log_path(workspace, "api", "build").read_text() == "small"


def test_rotate_log_over_threshold_rotates(workspace) -> None:
    _write(workspace, "api", "build", "x" * 100)
    rotated = rotate_log(workspace, "api", "build", max_bytes=50, keep=3)
    assert rotated is True
    # 当前文件被改名，原路径不再存在
    assert not log_path(workspace, "api", "build").exists()
    # .log.1 存在
    rotated_path = log_path(workspace, "api", "build").with_name("build.log.1")
    assert rotated_path.is_file()
    assert rotated_path.stat().st_size == 100


def test_rotate_log_keeps_at_most_n_copies(workspace) -> None:
    """多次滚动后保留不超过 keep 份历史。"""
    log_dir = workspace.app_logs("api")
    log_dir.mkdir(parents=True, exist_ok=True)
    # 预置 .log.1 ~ .log.3
    for i in range(1, 4):
        (log_dir / f"build.log.{i}").write_text(f"old{i}")
    # 写一个超限的当前文件
    (log_dir / "build.log").write_text("x" * 100)

    rotate_log(workspace, "api", "build", max_bytes=50, keep=3)

    # 最旧的 .3 被删除（顺延后），现在存在 .1 .2 .3，但原始 .3 内容应消失
    assert (log_dir / "build.log.1").read_text() == "x" * 100  # 新滚动的当前
    assert (log_dir / "build.log.2").read_text() == "old1"
    assert (log_dir / "build.log.3").read_text() == "old2"
    # 不存在 .4
    assert not (log_dir / "build.log.4").exists()


def test_rotate_missing_log_no_op(workspace) -> None:
    assert rotate_log(workspace, "api", "build") is False


def test_rotate_all_processes_all_categories(workspace) -> None:
    _write(workspace, "api", "build", "b" * 100)
    _write(workspace, "api", "run", "r" * 100)
    _write(workspace, "api", "gateway", "g")  # 小文件，不滚动
    rotated = rotate_all(workspace, "api", max_bytes=50, keep=2)
    assert set(rotated) == {"build", "run"}
