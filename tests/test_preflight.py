"""preflight 模块测试（IMP-058 Gate-A A.09）。

每项预检（CHK-V02/V03/V04/V06）含正例 + 反例。
"""

from __future__ import annotations

from pathlib import Path

from local_webpage_access.models import DatabaseConfig, EntryConfig, Kind, Runtime
from local_webpage_access.preflight import (
    AUTOFIXED,
    PASSED,
    REJECTED,
    check_and_fix,
)
from local_webpage_access.scanner import DetectionResult


def _mk_python_result(
    *,
    start: str | None = "uvicorn app.main:app --host 0.0.0.0 --port 8000",
    has_db: bool = False,
    db_conn: str | None = None,
    db_filename: str | None = None,
    pending: bool = False,
) -> DetectionResult:
    """构造一个 Python 项目的 DetectionResult。"""
    result = DetectionResult()
    result.kind = Kind.PYTHON
    result.runtime = Runtime.DOCKER_COMPOSE
    result.form = "backend-container"
    result.confidence = "high"
    result.entry = EntryConfig(
        install="pip install -r requirements.txt",
        build=None,
        start=start,
    )
    if has_db:
        result.hasDatabase = True
        result.database = DatabaseConfig(type="sqlite", dataDir="data")
        if db_conn:
            result.database.connectionString = db_conn
        if db_filename:
            result.database.dbFilename = db_filename
    if pending:
        result.pending = True
        result.confidence = "low"
    return result


def _ensure_requirements(source_dir: Path, name: str = "requirements.txt") -> None:
    """在 source_dir 下创建空的 requirements 文件（CHK-V01 存在性检查前置条件）。"""
    (source_dir / name).write_text("", encoding="utf-8")


# ---- CHK-V02：CMD shell 操作符安全 -------------------------------------------


def test_chk_v02_no_shell_operators(tmp_path: Path) -> None:
    """正例：简单命令不含 shell 操作符，预检通过。"""
    _ensure_requirements(tmp_path)
    result = _mk_python_result(start="uvicorn app.main:app --host 0.0.0.0 --port 8000")
    pre = check_and_fix(result, tmp_path)
    assert pre.status == PASSED
    v02 = next(c for c in pre.checks if c.check_id == "CHK-V02")
    assert v02.passed is True
    assert v02.autofixed is False
    assert result.entry.start == "uvicorn app.main:app --host 0.0.0.0 --port 8000"


def test_chk_v02_shell_operators_autofix(tmp_path: Path) -> None:
    """反例：含 && 的命令自动包裹 sh -c。"""
    _ensure_requirements(tmp_path)
    result = _mk_python_result(
        start="alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"
    )
    pre = check_and_fix(result, tmp_path)
    assert pre.status == AUTOFIXED
    v02 = next(c for c in pre.checks if c.check_id == "CHK-V02")
    assert v02.passed is True
    assert v02.autofixed is True
    assert result.entry.start.startswith("sh -c '")
    assert "alembic upgrade head && uvicorn" in result.entry.start


def test_chk_v02_already_wrapped_no_double_wrap(tmp_path: Path) -> None:
    """已用 sh -c 包裹的命令不重复包裹。"""
    _ensure_requirements(tmp_path)
    result = _mk_python_result(
        start='sh -c "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"'
    )
    pre = check_and_fix(result, tmp_path)
    v02 = next(c for c in pre.checks if c.check_id == "CHK-V02")
    assert v02.passed is True
    assert v02.autofixed is False
    # 不应双重包裹
    assert result.entry.start.count("sh -c") == 1


def test_chk_v02_pipe_operator(tmp_path: Path) -> None:
    """含 || 的命令也自动包裹。"""
    _ensure_requirements(tmp_path)
    result = _mk_python_result(start="python app.py || python fallback.py")
    pre = check_and_fix(result, tmp_path)
    v02 = next(c for c in pre.checks if c.check_id == "CHK-V02")
    assert v02.autofixed is True
    assert result.entry.start.startswith("sh -c '")


def test_chk_v02_semicolon_operator(tmp_path: Path) -> None:
    """含 ; 的命令也自动包裹。"""
    _ensure_requirements(tmp_path)
    result = _mk_python_result(start="python init.py ; python app.py")
    pre = check_and_fix(result, tmp_path)
    v02 = next(c for c in pre.checks if c.check_id == "CHK-V02")
    assert v02.autofixed is True
    assert result.entry.start.startswith("sh -c '")


def test_chk_v02_bare_parentheses_operator(tmp_path: Path) -> None:
    """IMP-058 Gate-A：含裸 () 的命令也自动包裹（文档 §6.4 CHK-V02）。"""
    _ensure_requirements(tmp_path)
    result = _mk_python_result(start="docker-entrypoint.sh (alembic upgrade) && uvicorn app.main:app")
    pre = check_and_fix(result, tmp_path)
    v02 = next(c for c in pre.checks if c.check_id == "CHK-V02")
    assert v02.autofixed is True
    assert result.entry.start.startswith("sh -c '")


# ---- CHK-V03：数据库路径与 volume 一致性 -------------------------------------


def test_chk_v03_no_database(tmp_path: Path) -> None:
    """非 SQLite 项目跳过检查。"""
    _ensure_requirements(tmp_path)
    result = _mk_python_result(has_db=False)
    pre = check_and_fix(result, tmp_path)
    v03 = next(c for c in pre.checks if c.check_id == "CHK-V03")
    assert v03.passed is True
    assert v03.autofixed is False


def test_chk_v03_relative_db_path(tmp_path: Path) -> None:
    """SQLite 使用相对路径时标记修正。"""
    _ensure_requirements(tmp_path)
    result = _mk_python_result(
        has_db=True,
        db_conn="sqlite:///./data/bookshelf.db",
    )
    pre = check_and_fix(result, tmp_path)
    v03 = next(c for c in pre.checks if c.check_id == "CHK-V03")
    assert v03.passed is True
    assert v03.autofixed is True
    assert "DATABASE_URL" in v03.action


def test_chk_v03_absolute_db_path(tmp_path: Path) -> None:
    """SQLite 使用绝对路径时仍预防性注入（A.02 对所有 SQLite 注入）。"""
    _ensure_requirements(tmp_path)
    result = _mk_python_result(
        has_db=True,
        db_conn="sqlite:////app/data/bookshelf.db",
    )
    pre = check_and_fix(result, tmp_path)
    v03 = next(c for c in pre.checks if c.check_id == "CHK-V03")
    assert v03.passed is True
    assert v03.autofixed is True  # A.02 预防性注入


def test_chk_v03_sqlite_no_connection_string(tmp_path: Path) -> None:
    """SQLite 无 connectionString 时仍预防性注入。"""
    _ensure_requirements(tmp_path)
    result = _mk_python_result(has_db=True, db_conn=None)
    pre = check_and_fix(result, tmp_path)
    v03 = next(c for c in pre.checks if c.check_id == "CHK-V03")
    assert v03.passed is True
    assert v03.autofixed is True


def test_chk_v03_preserves_source_db_filename(tmp_path: Path) -> None:
    """IMP-058 Gate-A CHK-V03：注入的 DATABASE_URL 保留源 SQLite 文件名。"""
    _ensure_requirements(tmp_path)
    result = _mk_python_result(
        has_db=True,
        db_conn="sqlite:///./data/bookshelf.db",
        db_filename="bookshelf.db",
    )
    pre = check_and_fix(result, tmp_path)
    v03 = next(c for c in pre.checks if c.check_id == "CHK-V03")
    assert v03.passed is True
    assert v03.autofixed is True
    # 注入的 DATABASE_URL 应保留源文件名 bookshelf.db，而非硬编码 app.sqlite
    assert "bookshelf.db" in v03.action
    assert "app.sqlite" not in v03.action


# ---- CHK-V04：alembic script_location 可达性 ---------------------------------


def test_chk_v04_no_alembic_ini(tmp_path: Path) -> None:
    """无 alembic.ini 跳过检查。"""
    _ensure_requirements(tmp_path)
    result = _mk_python_result()
    pre = check_and_fix(result, tmp_path)
    v04 = next(c for c in pre.checks if c.check_id == "CHK-V04")
    assert v04.passed is True
    assert v04.autofixed is False


def test_chk_v04_alembic_at_root(tmp_path: Path) -> None:
    """alembic.ini 在顶层且 script_location 可达：cwd 一致，无需修正。"""
    _ensure_requirements(tmp_path)
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = alembic\n")
    (tmp_path / "alembic").mkdir()  # script_location 指向的目录存在
    result = _mk_python_result(
        start='sh -c "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"'
    )
    pre = check_and_fix(result, tmp_path)
    v04 = next(c for c in pre.checks if c.check_id == "CHK-V04")
    assert v04.passed is True
    assert v04.autofixed is False


def test_chk_v04_alembic_at_root_script_location_missing(tmp_path: Path) -> None:
    """IMP-058 Gate-A CHK-V04：顶层 alembic.ini 但 script_location 目录不存在 → 标记风险。"""
    _ensure_requirements(tmp_path)
    (tmp_path / "alembic.ini").write_text("[alembic]\nscript_location = migrations\n")
    # 不创建 migrations/ 目录
    result = _mk_python_result(
        start='sh -c "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"'
    )
    pre = check_and_fix(result, tmp_path)
    v04 = next(c for c in pre.checks if c.check_id == "CHK-V04")
    assert v04.passed is True  # 不淘汰候选
    assert v04.autofixed is False
    assert "script_location" in v04.detail
    assert "不存在" in v04.detail
    # 风险应收集到 warnings
    assert any("script_location" in w for w in pre.warnings)


def test_chk_v04_alembic_in_subdir_autofix(tmp_path: Path) -> None:
    """alembic.ini 在子目录：自动编排 cwd 序列。"""
    _ensure_requirements(tmp_path)
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "alembic.ini").write_text("[alembic]\nscript_location = alembic\n")
    result = _mk_python_result(
        start='sh -c "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"'
    )
    pre = check_and_fix(result, tmp_path)
    v04 = next(c for c in pre.checks if c.check_id == "CHK-V04")
    assert v04.passed is True
    assert v04.autofixed is True
    assert "cd backend" in result.entry.start
    assert "cd /app" in result.entry.start


def test_chk_v04_alembic_in_subdir_no_alembic_in_start(tmp_path: Path) -> None:
    """alembic.ini 在子目录但 start 不含 alembic：不修正。"""
    _ensure_requirements(tmp_path)
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "alembic.ini").write_text("[alembic]\nscript_location = alembic\n")
    result = _mk_python_result(start="uvicorn app.main:app --host 0.0.0.0 --port 8000")
    pre = check_and_fix(result, tmp_path)
    v04 = next(c for c in pre.checks if c.check_id == "CHK-V04")
    assert v04.passed is True
    assert v04.autofixed is False


# ---- CHK-V06：项目自带 Dockerfile 检测 ---------------------------------------


def test_chk_v06_no_project_dockerfile(tmp_path: Path) -> None:
    """无项目自带 Dockerfile：通过。"""
    _ensure_requirements(tmp_path)
    result = _mk_python_result()
    pre = check_and_fix(result, tmp_path)
    v06 = next(c for c in pre.checks if c.check_id == "CHK-V06")
    assert v06.passed is True
    assert v06.autofixed is False
    assert pre.status == PASSED


def test_chk_v06_project_dockerfile_at_root(tmp_path: Path) -> None:
    """检测到顶层 Dockerfile：标记 warning。"""
    _ensure_requirements(tmp_path)
    (tmp_path / "Dockerfile").write_text("FROM python:3.12\n")
    result = _mk_python_result()
    pre = check_and_fix(result, tmp_path)
    v06 = next(c for c in pre.checks if c.check_id == "CHK-V06")
    assert v06.passed is True
    assert v06.autofixed is False
    assert "检测到项目自带 Dockerfile" in v06.detail
    assert len(pre.warnings) >= 1


def test_chk_v06_project_dockerfile_in_subdir(tmp_path: Path) -> None:
    """检测到子目录 Dockerfile：标记 warning。"""
    _ensure_requirements(tmp_path)
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "Dockerfile").write_text("FROM python:3.12\n")
    result = _mk_python_result()
    pre = check_and_fix(result, tmp_path)
    v06 = next(c for c in pre.checks if c.check_id == "CHK-V06")
    assert v06.passed is True
    assert "backend/Dockerfile" in v06.detail


# ---- CHK-V01：COPY 源路径文件存在性 ------------------------------------------


def test_chk_v01_root_requirements_exists(tmp_path: Path) -> None:
    """正例：requirements.txt 在根目录存在，通过。"""
    _ensure_requirements(tmp_path)
    result = _mk_python_result()
    pre = check_and_fix(result, tmp_path)
    v01 = next(c for c in pre.checks if c.check_id == "CHK-V01")
    assert v01.passed is True
    assert v01.autofixed is False


def test_chk_v01_subdir_autofix(tmp_path: Path) -> None:
    """反例：requirements.txt 在子目录但 source_subdir 未设 → 自动修正。"""
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    result = _mk_python_result()
    assert result.source_subdir is None
    pre = check_and_fix(result, tmp_path)
    v01 = next(c for c in pre.checks if c.check_id == "CHK-V01")
    assert v01.passed is True
    assert v01.autofixed is True
    assert result.source_subdir == "backend"
    assert "backend" in v01.action


def test_chk_v01_file_not_found_rejected(tmp_path: Path) -> None:
    """反例：requirements.txt 完全不存在 → rejected。"""
    result = _mk_python_result()
    pre = check_and_fix(result, tmp_path)
    v01 = next(c for c in pre.checks if c.check_id == "CHK-V01")
    assert v01.passed is False
    assert v01.autofixed is False
    assert pre.status == REJECTED


def test_chk_v01_non_pip_skipped(tmp_path: Path) -> None:
    """非 pip install 候选跳过 CHK-V01。"""
    result = _mk_python_result()
    result.entry.install = None
    pre = check_and_fix(result, tmp_path)
    v01 = next(c for c in pre.checks if c.check_id == "CHK-V01")
    assert v01.passed is True
    assert v01.autofixed is False
    assert "跳过" in v01.detail


# ---- CHK-V05：entrypoint 脚本 COPY 完整性 ------------------------------------


def test_chk_v05_no_script_reference(tmp_path: Path) -> None:
    """正例：启动命令不引用 .sh 脚本 → 跳过。"""
    _ensure_requirements(tmp_path)
    result = _mk_python_result(start="uvicorn app.main:app --host 0.0.0.0 --port 8000")
    pre = check_and_fix(result, tmp_path)
    v05 = next(c for c in pre.checks if c.check_id == "CHK-V05")
    assert v05.passed is True
    assert v05.autofixed is False
    assert "跳过" in v05.detail


def test_chk_v05_script_exists_at_root(tmp_path: Path) -> None:
    """正例：entry.start 引用的 .sh 脚本在根目录存在 → 通过。"""
    _ensure_requirements(tmp_path)
    (tmp_path / "docker-entrypoint.sh").write_text("#!/bin/sh\nalembic upgrade head\n", encoding="utf-8")
    result = _mk_python_result(start="docker-entrypoint.sh && uvicorn app.main:app")
    pre = check_and_fix(result, tmp_path)
    v05 = next(c for c in pre.checks if c.check_id == "CHK-V05")
    assert v05.passed is True
    assert v05.autofixed is False
    assert "docker-entrypoint.sh" in v05.detail


def test_chk_v05_script_exists_in_subdir(tmp_path: Path) -> None:
    """正例：entry.start 引用的 .sh 脚本在子目录存在 → 通过。"""
    _ensure_requirements(tmp_path)
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "entrypoint.sh").write_text("#!/bin/sh\nexec uvicorn app.main:app\n", encoding="utf-8")
    result = _mk_python_result(start="backend/entrypoint.sh")
    pre = check_and_fix(result, tmp_path)
    v05 = next(c for c in pre.checks if c.check_id == "CHK-V05")
    assert v05.passed is True
    assert "entrypoint.sh" in v05.detail


def test_chk_v05_script_not_found_rejected(tmp_path: Path) -> None:
    """反例：entry.start 引用的 .sh 脚本不存在 → rejected。"""
    _ensure_requirements(tmp_path)
    result = _mk_python_result(start="docker-entrypoint.sh alembic upgrade head && uvicorn app.main:app")
    pre = check_and_fix(result, tmp_path)
    v05 = next(c for c in pre.checks if c.check_id == "CHK-V05")
    assert v05.passed is False
    assert v05.autofixed is False
    assert "docker-entrypoint.sh" in v05.detail
    assert pre.status == REJECTED


# ---- 综合场景 ----------------------------------------------------------------


def test_pending_result_skips_preflight(tmp_path: Path) -> None:
    """pending 结果跳过预检。"""
    result = _mk_python_result(pending=True)
    pre = check_and_fix(result, tmp_path)
    assert pre.status == PASSED
    assert len(pre.checks) == 1
    assert pre.checks[0].check_id == "SKIP"


def test_combined_autofix_and_warning(tmp_path: Path) -> None:
    """同时有修正和警告：status=autofixed（修正优先于警告）。"""
    _ensure_requirements(tmp_path)
    (tmp_path / "Dockerfile").write_text("FROM python:3.12\n")
    result = _mk_python_result(
        start="alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000",
        has_db=True,
    )
    pre = check_and_fix(result, tmp_path)
    # 有修正 -> status=autofixed
    assert pre.status == AUTOFIXED
    # 同时有 warning
    assert len(pre.warnings) >= 1
    # 修正事件在 notes 中
    assert len(pre.notes) >= 1
    assert any("CHK-V02" in n for n in pre.notes)


def test_simple_project_no_issues(tmp_path: Path) -> None:
    """简单项目（无 shell 操作符、无 DB、无 alembic、无 Dockerfile）：全通过。"""
    _ensure_requirements(tmp_path)
    result = _mk_python_result()
    pre = check_and_fix(result, tmp_path)
    assert pre.status == PASSED
    assert all(c.passed for c in pre.checks)
    assert len(pre.warnings) == 0
    assert len(pre.notes) == 0


# ---- CHK-193/P1：sourceSubdir 匹配时不注入 cd --------------------------------


def test_chk_v04_subdir_matches_source_subdir_no_cd(tmp_path: Path) -> None:
    """CHK-193/P1：alembic.ini 子目录与 source_subdir 一致时，
    Dockerfile 扁平复制到 /app/，不需要 cd 编排。"""
    _ensure_requirements(tmp_path)
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "alembic.ini").write_text("[alembic]\nscript_location = alembic\n")
    (backend / "alembic").mkdir()
    result = _mk_python_result(
        start='sh -c "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"'
    )
    result.source_subdir = "backend"
    pre = check_and_fix(result, tmp_path)
    v04 = next(c for c in pre.checks if c.check_id == "CHK-V04")
    assert v04.passed is True
    assert v04.autofixed is False
    assert "cd backend" not in result.entry.start
    assert "扁平复制" in v04.detail


def test_chk_v04_subdir_no_source_subdir_still_injects_cd(tmp_path: Path) -> None:
    """source_subdir 未设置时（Dockerfile 保留目录结构），仍注入 cd。"""
    _ensure_requirements(tmp_path)
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "alembic.ini").write_text("[alembic]\nscript_location = alembic\n")
    (backend / "alembic").mkdir()
    result = _mk_python_result(
        start='sh -c "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"'
    )
    # source_subdir 未设置（默认 None）
    pre = check_and_fix(result, tmp_path)
    v04 = next(c for c in pre.checks if c.check_id == "CHK-V04")
    assert v04.passed is True
    assert v04.autofixed is True
    assert "cd backend" in result.entry.start


def test_chk_v04_subdir_mismatch_source_subdir_still_injects(tmp_path: Path) -> None:
    """source_subdir 与 alembic.ini 子目录不一致时，仍注入 cd。"""
    _ensure_requirements(tmp_path)
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "alembic.ini").write_text("[alembic]\nscript_location = alembic\n")
    (backend / "alembic").mkdir()
    result = _mk_python_result(
        start='sh -c "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"'
    )
    result.source_subdir = "other"  # 不匹配 alembic.ini 的 backend/
    pre = check_and_fix(result, tmp_path)
    v04 = next(c for c in pre.checks if c.check_id == "CHK-V04")
    assert v04.autofixed is True
    assert "cd backend" in result.entry.start
