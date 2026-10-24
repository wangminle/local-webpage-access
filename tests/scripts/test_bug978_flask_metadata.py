"""BUG-978：AppleDouble 等 OS 元数据文件不得破坏 Flask init-db 证明；
0 字节空库/无表库可安全重初始化（自愈），有表真库与不可解析文件一律保护。"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

from local_webpage_access.scanner import _flask_init_db_spec
from tests.scripts.test_bug931_936_937 import (
    _detect_start,
    _make_stub,
    _run_guard,
    _write_factory,
)

# macOS 写向非原生文件系统时产生的 AppleDouble 头（\x00\x05\x16\x07），
# 其二进制内容拼入全包文本会让 AST 解析失败、init-db 证明被静默放弃。
_APPLEDOUBLE = b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        \x00" * 8


def test_appledouble_files_do_not_break_init_db_spec(tmp_path: Path) -> None:
    """flaskr 包内混入 ._*.py 二进制元数据时，init-db 包装不得丢失。"""
    _write_factory(tmp_path, "os.path.join(app.instance_path, 'flaskr.sqlite')")
    pkg = tmp_path / "flaskr"
    (pkg / "._db.py").write_bytes(_APPLEDOUBLE)
    (pkg / ".__init__.py").write_bytes(_APPLEDOUBLE)
    spec = _flask_init_db_spec(tmp_path, "flaskr:create_app()")
    assert spec is not None
    init_cmd, db_path, _guards = spec
    assert init_cmd == "flask --app flaskr init-db"
    assert db_path == "/app/instance/flaskr.sqlite"
    start, _notes = _detect_start(tmp_path)
    assert "init-db" in start


def test_hidden_files_excluded_but_package_files_joined(tmp_path: Path) -> None:
    """干净包不受过滤影响：多文件拼接证明照常成立。"""
    _write_factory(tmp_path, "os.path.join(app.instance_path, 'flaskr.sqlite')")
    spec = _flask_init_db_spec(tmp_path, "flaskr:create_app()")
    assert spec is not None


def test_guard_contains_empty_db_probe(tmp_path: Path) -> None:
    """生成守卫须包含 0 字节快路与 sqlite_master 无表探测。"""
    _write_factory(tmp_path, "os.path.join(app.instance_path, 'flaskr.sqlite')")
    start, notes = _detect_start(tmp_path)
    assert "[ ! -s /app/instance/flaskr.sqlite ]" in start
    assert "select count(*) from sqlite_master" in start
    assert any("可证明为空" in note for note in notes)


def _seed_flaskr_data(db_path: Path) -> None:
    """按 flaskr 教程 schema 造「注册用户 + 发布文章」的已有数据。"""
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE user (id INTEGER PRIMARY KEY, username TEXT UNIQUE)")
    conn.execute("CREATE TABLE post (id INTEGER PRIMARY KEY, author_id INTEGER, title TEXT)")
    conn.execute("INSERT INTO user (username) VALUES ('alice')")
    conn.execute("INSERT INTO post (author_id, title) VALUES (1, '第一篇文章')")
    conn.commit()
    conn.close()


_SEEDER = """import os
import sqlite3

conn = sqlite3.connect(os.environ["DB_PATH"])
conn.execute("CREATE TABLE IF NOT EXISTS user (id INTEGER PRIMARY KEY, username TEXT UNIQUE)")
conn.execute(
    "CREATE TABLE IF NOT EXISTS post (id INTEGER PRIMARY KEY, author_id INTEGER, title TEXT)"
)
conn.execute("INSERT INTO user (username) VALUES ('alice')")
conn.execute("INSERT INTO post (author_id, title) VALUES (1, '第一篇文章')")
conn.commit()
"""


def _flask_stub_records_and_seeds(bin_dir: Path, name: str, seeder: Path) -> None:
    """init-db 存根：记录调用并执行 seeder 按 flaskr schema 建表写数据。"""
    seeder.write_text(_SEEDER, encoding="utf-8")
    script = (
        'echo "$@" >> "$MARKER"\n'
        '"$(command -v python || command -v python3)" "$SEEDER"\n'
        "exit 0\n"
    )
    _make_stub(bin_dir, name, script)


def _run_guard_with_db(
    start: str, *, instance_root: Path, bin_dir: Path, marker: Path, seeder: Path
) -> int:
    db_path = instance_root / "flaskr.sqlite"
    inner = start[len("sh -c '") : -1].replace("/app/instance", str(instance_root))
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "MARKER": str(marker),
        "DB_PATH": str(db_path),
        "SEEDER": str(seeder),
    }
    return subprocess.run(["sh", "-c", inner], env=env, capture_output=True, text=True).returncode


def test_guard_reinitializes_empty_db_and_preserves_data_across_restarts(
    tmp_path: Path,
) -> None:
    """0 字节空库自愈：init-db 重建表与数据；再次启动（升降级/restart）数据原样保留。"""
    _write_factory(tmp_path, "os.path.join(app.instance_path, 'flaskr.sqlite')")
    start, _notes = _detect_start(tmp_path)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "init-db.called"
    seeder = tmp_path / "seed.py"
    _flask_stub_records_and_seeds(bin_dir, "flask", seeder)
    _make_stub(bin_dir, "gunicorn", "exit 0\n")
    instance_root = tmp_path / "instance"
    instance_root.mkdir()
    db_path = instance_root / "flaskr.sqlite"
    db_path.write_bytes(b"")  # Flask 首次请求 connect 产生的 0 字节空库

    assert _run_guard_with_db(
        start, instance_root=instance_root, bin_dir=bin_dir, marker=marker, seeder=seeder
    ) == 0
    assert marker.exists(), "0 字节空库应允许 init-db 自愈"
    conn = sqlite3.connect(db_path)
    users = conn.execute("SELECT username FROM user").fetchall()
    posts = conn.execute("SELECT title FROM post").fetchall()
    conn.close()
    assert users == [("alice",)]
    assert posts == [("第一篇文章",)]

    # 模拟升降级/重建后再次启动：init-db 不得再执行，注册与文章数据原样保留。
    marker.unlink()
    assert _run_guard_with_db(
        start, instance_root=instance_root, bin_dir=bin_dir, marker=marker, seeder=seeder
    ) == 0
    assert not marker.exists(), "已有数据的真库不得再次 init-db"
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT username FROM user").fetchall() == [("alice",)]
    assert conn.execute("SELECT title FROM post").fetchall() == [("第一篇文章",)]
    conn.close()


def test_guard_reinitializes_tabless_sqlite_but_protects_corrupt_file(
    tmp_path: Path,
) -> None:
    """有效但无表的 sqlite（建库中断残留）可重初始化；不可解析文件按真库保护。"""
    _write_factory(tmp_path, "os.path.join(app.instance_path, 'flaskr.sqlite')")
    start, _notes = _detect_start(tmp_path)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "init-db.called"
    _make_stub(bin_dir, "flask", 'echo "$@" >> "$MARKER"\nexit 0\n')
    _make_stub(bin_dir, "gunicorn", "exit 0\n")
    instance_root = tmp_path / "instance"
    instance_root.mkdir()
    db_path = instance_root / "flaskr.sqlite"

    # 有效 sqlite 头但 sqlite_master 为空（建库中断）：允许重初始化。
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA user_version = 1")
    conn.close()
    assert db_path.stat().st_size > 0
    assert _run_guard(start, instance_root=instance_root, bin_dir=bin_dir, marker=marker) == 0
    assert marker.exists(), "无表 sqlite 应允许 init-db"
    marker.unlink()

    # 非 sqlite 垃圾内容：无法证明为空，按真库保护。
    db_path.write_bytes(b"not-a-database" * 16)
    assert _run_guard(start, instance_root=instance_root, bin_dir=bin_dir, marker=marker) == 0
    assert not marker.exists(), "不可解析文件不得 init-db"
    assert db_path.read_bytes() == b"not-a-database" * 16


def test_guard_still_skips_when_config_guard_present(tmp_path: Path) -> None:
    """可选 instance 配置文件存在时，即使库缺失也不得 init-db（既有守卫语义）。"""
    _write_factory(tmp_path, "os.path.join(app.instance_path, 'flaskr.sqlite')")
    pkg_init = tmp_path / "flaskr" / "__init__.py"
    pkg_init.write_text(
        pkg_init.read_text(encoding="utf-8").replace(
            "    db.init_app(app)\n",
            "    app.config.from_pyfile('config.py', silent=True)\n    db.init_app(app)\n",
        ),
        encoding="utf-8",
    )
    start, _notes = _detect_start(tmp_path)
    assert "init-db" in start

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "init-db.called"
    _make_stub(bin_dir, "flask", 'echo "$@" >> "$MARKER"\nexit 0\n')
    _make_stub(bin_dir, "gunicorn", "exit 0\n")
    instance_root = tmp_path / "instance"
    instance_root.mkdir()
    (instance_root / "config.py").write_text("DATABASE = '/elsewhere/x.sqlite'\n")

    assert _run_guard(start, instance_root=instance_root, bin_dir=bin_dir, marker=marker) == 0
    assert not marker.exists(), "config.py 存在时不得 init-db"
