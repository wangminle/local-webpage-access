"""日志读取与滚动测试（WBS-18.01/02/03/11）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_webpage_access.logs import (
    DEFAULT_MAX_BYTES,
    LogInfo,
    list_logs,
    log_path,
    read_log,
    rotate_all,
    rotate_log,
)
from local_webpage_access.paths import Workspace
from local_webpage_access.errors import PathError


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


def test_rotate_path_serializes_with_file_lock(workspace, monkeypatch) -> None:
    """BUG-355：rotate_path 在 check+shift+rename 期间持有 file_lock。"""
    import local_webpage_access.file_lock as fl
    import local_webpage_access.logs as logs_mod
    from local_webpage_access.logs import rotate_path

    path = log_path(workspace, "api", "build")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x" * 100, encoding="utf-8")

    acquired: list[int] = []
    real_acquire = fl.try_acquire_exclusive

    def tracking_acquire(fd: int) -> None:
        acquired.append(fd)
        return real_acquire(fd)

    monkeypatch.setattr(fl, "try_acquire_exclusive", tracking_acquire)
    if hasattr(logs_mod, "try_acquire_exclusive"):
        monkeypatch.setattr(logs_mod, "try_acquire_exclusive", tracking_acquire)

    assert rotate_path(path, max_bytes=50, keep=3) is True
    assert acquired, "rotate_path 应通过 file_lock 串行化 check+shift+rename"
    assert path.with_name("build.log.1").is_file()


def test_rotate_all_processes_all_categories(workspace) -> None:
    _write(workspace, "api", "build", "b" * 100)
    _write(workspace, "api", "run", "r" * 100)
    _write(workspace, "api", "gateway", "g")  # 小文件，不滚动
    rotated = rotate_all(workspace, "api", max_bytes=50, keep=2)
    assert set(rotated) == {"build", "run"}


def test_write_instance_log_rotates_over_threshold(workspace, monkeypatch) -> None:
    """BUG-186：write_instance_log 写入前对超阈值日志滚动（接线 rotate_path）。"""
    from local_webpage_access import logs as logs_mod
    from local_webpage_access.logging import write_instance_log

    # 用极小阈值覆盖 rotate_path，避免在测试里写 10MB
    real_rotate = logs_mod.rotate_path

    def small_rotate(path, *, max_bytes=50, keep=3):
        return real_rotate(path, max_bytes=max_bytes, keep=keep)

    # write_instance_log 懒导入 logs.rotate_path，patch 模块属性即生效
    monkeypatch.setattr(logs_mod, "rotate_path", small_rotate)

    # 先写一份超 50 字节的旧日志
    write_instance_log(workspace.apps, "api", "build", "x" * 200)
    build_path = log_path(workspace, "api", "build")
    assert build_path.is_file()

    # 再写：写入前应触发滚动（旧内容 → build.log.1，当前文件为本次新内容）
    write_instance_log(workspace.apps, "api", "build", "new line")
    rotated = build_path.with_name("build.log.1")
    assert rotated.is_file()
    assert ("x" * 200) in rotated.read_text(encoding="utf-8")
    current = build_path.read_text(encoding="utf-8")
    assert "new line" in current
    assert ("x" * 200) not in current


def test_open_append_rotates_then_writes(tmp_path: Path) -> None:
    """BUG-186：open_append 对超阈值文件先滚动再追加。"""
    from local_webpage_access import logs as logs_mod

    path = tmp_path / "build.log"
    path.write_text("x" * 200, encoding="utf-8")
    with logs_mod.open_append(path, max_bytes=50) as fh:
        fh.write("fresh\n")
    assert path.read_text(encoding="utf-8").endswith("fresh\n")
    assert ("x" * 200) in path.with_name("build.log.1").read_text(encoding="utf-8")
    assert ("x" * 200) not in path.read_text(encoding="utf-8")


def test_setup_logging_uses_rotating_file_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BUG-297/BUG-329：长跑 daemon/manager/gateway 日志必须由 handler 在线滚动。"""
    import logging.handlers

    import local_webpage_access.logging as logging_mod
    from local_webpage_access.logging import setup_logging

    monkeypatch.setattr(logging_mod, "_FILE_LOG_MAX_BYTES", 2048)
    monkeypatch.setattr(logging_mod, "_FILE_LOG_BACKUP_COUNT", 2)

    logger = setup_logging(
        log_dir=tmp_path,
        log_filename="daemon.log",
        force=True,
    )
    try:
        rotating = [
            handler
            for handler in logger.handlers
            if isinstance(handler, logging.handlers.RotatingFileHandler)
        ]
        assert rotating
        handler = rotating[0]
        assert handler.maxBytes == 2048
        assert handler.backupCount == 2
        # 长持 fd：写入超过阈值后应在线滚动，无需 reopen
        for _ in range(40):
            logger.info("x" * 120)
        handler.flush()
        assert (tmp_path / "daemon.log.1").is_file()
    finally:
        for handler in list(logger.handlers):
            handler.close()


def test_read_log_tail_avoids_full_read_text(workspace, monkeypatch) -> None:
    """BUG-186：tail>0 时不得 Path.read_text 全量读入。"""
    _write(workspace, "api", "build", "keep-me\n" + ("noise\n" * 5000) + "TAIL-A\nTAIL-B\n")
    path = log_path(workspace, "api", "build")

    real_read_text = Path.read_text

    def boom(self, *args, **kwargs):  # noqa: ANN001
        if self.resolve() == path.resolve():
            raise AssertionError("read_log(tail>0) 不应全量 read_text")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", boom)
    text = read_log(workspace, "api", "build", tail=2)
    assert text == "TAIL-A\nTAIL-B"


# ---- 回归测试：BUG-603（issue #16）----------------------------------------


def test_append_global_log_writes_formatted_line(workspace) -> None:
    """BUG-603：跨进程面包屑以 _LOG_FORMAT 同构格式追加到 lwa.log。"""
    from local_webpage_access.logging import append_global_log

    append_global_log(workspace.logs, "自愈成功：实例 x（failed → running）")
    append_global_log(workspace.logs, "自愈失败：实例 y", level="WARNING")

    lines = (workspace.logs / "lwa.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    # 格式：%(asctime)s %(levelname)-8s %(name)s %(message)s
    assert " INFO     local_webpage_access.daemon 自愈成功：实例 x（failed → running）" in lines[0]
    assert " WARNING  local_webpage_access.daemon 自愈失败：实例 y" in lines[1]
    # 首次创建时收紧权限（BUG-118 同口径）
    assert (workspace.logs / "lwa.log").stat().st_mode & 0o777 == 0o600


def test_append_global_log_swallows_errors(tmp_path, monkeypatch) -> None:
    """BUG-603：日志文件打不开时面包屑静默丢弃，不影响主流程。"""
    from local_webpage_access.logging import append_global_log

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("local_webpage_access.logging.Path.open", boom)
    append_global_log(tmp_path, "任何消息")  # 不抛异常即通过
