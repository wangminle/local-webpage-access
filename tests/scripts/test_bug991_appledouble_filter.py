"""BUG-991：OS 元数据（AppleDouble ``._*`` / ``.DS_Store``）不得被当作真实代码或
SQLite 数据库——BUG-978 只修了 ``_flask_init_db_spec`` 一处，本文件覆盖其余路径：

- compose：instance-SQLite 候选扫描 / Flask 构造文件唯一性 / instance 种子拷贝 /
  data/ 目录非空库盘点（含 doctor 同口径函数）；
- scanner：``summarize`` 与 ``_walk`` 的文件统计与 ``sqlite_files[0]`` 库名推断；
- security / folder_source / lifecycle：zip 剥离、文件夹打包与两类内容指纹。

测试数据使用真实含 NUL 字节的 AppleDouble 头（与 test_bug978 同一写法）。"""

from __future__ import annotations

import zipfile
from pathlib import Path

from local_webpage_access.compose import (
    _find_instance_sqlite_py,
    _unique_flask_constructor_file,
    generate_compose,
    generate_env,
)
from local_webpage_access.doctor import _nonempty_sqlite_files_in
from local_webpage_access.folder_source import compute_source_hash, pack_source_dir
from local_webpage_access.lifecycle import _compute_source_fingerprint
from local_webpage_access.paths import Workspace
from local_webpage_access.scanner import summarize
from local_webpage_access.security import sanitize_zip_members
from tests.scripts.test_bug978_flask_metadata import _APPLEDOUBLE
from tests.scripts.test_bug931_936_937 import _write_factory
from tests.scripts.test_compose import _mk_manifest

# 含关键字的 AppleDouble 诱饵：既有元数据二进制头（NUL 字节让 ast.parse 失败），
# 又含 "instance_path" / ".sqlite" 关键字，能命中候选扫描的文本条件。
_APPLEDOUBLE_DECOY = _APPLEDOUBLE + b"\ninstance_path = '/x'\nDB = 'a.sqlite'\n"


# ---- compose：``._*.py`` 不得成为 instance-SQLite 候选 ------------------------


def test_find_instance_sqlite_py_ignores_appledouble_decoy(tmp_path: Path) -> None:
    """``._db.py`` 含关键字且排序靠前时不得抢占 ``min()`` 选中。"""
    (tmp_path / "db.py").write_text(
        "import os\n"
        "from flask import current_app\n"
        "p = os.path.join(current_app.instance_path, 'app.sqlite')\n",
        encoding="utf-8",
    )
    (tmp_path / "._db.py").write_bytes(_APPLEDOUBLE_DECOY)
    assert _find_instance_sqlite_py(tmp_path) == tmp_path / "db.py"


def test_find_instance_sqlite_py_returns_none_for_only_appledouble(tmp_path: Path) -> None:
    """目录里只有 ``._db.py`` 时视为没有候选，而不是选中元数据文件。"""
    (tmp_path / "._db.py").write_bytes(_APPLEDOUBLE_DECOY)
    assert _find_instance_sqlite_py(tmp_path) is None


def test_unique_flask_constructor_ignores_appledouble_py(tmp_path: Path) -> None:
    """``._app.py``（NUL 字节）不得干扰唯一 Flask 构造的证明。"""
    (tmp_path / "app.py").write_text("from flask import Flask\napp = Flask(__name__)\n")
    (tmp_path / "._app.py").write_bytes(_APPLEDOUBLE + b"\napp = Flask(__name__)\n")
    assert _unique_flask_constructor_file(tmp_path) == tmp_path / "app.py"


# ---- compose：instance 持久化挂载不得被 AppleDouble 静默弄丢 -------------------


def test_appledouble_py_does_not_break_instance_mount(workspace: Workspace) -> None:
    """混入 ``._*.py`` 元数据时 ``../data/instance:/app/instance`` 挂载必须保留。

    修复前：``._db.py`` 被 ``_find_instance_sqlite_py`` 选中，其 NUL 内容拼进
    证明文本导致 ``ast.parse`` 失败，挂载证明整体返回 None（静默丢失）。
    """
    root = workspace.app_current("api")
    _write_factory(root, "os.path.join(app.instance_path, 'flaskr.sqlite')")
    (root / "flaskr" / "._db.py").write_bytes(_APPLEDOUBLE_DECOY)
    (root / "._db.py").write_bytes(_APPLEDOUBLE_DECOY)
    manifest = _mk_manifest(has_database=True, database_type="sqlite")
    content = generate_compose(manifest, workspace, host_port=18000).read_text()
    assert "../data/instance:/app/instance" in content


def test_seed_instance_dir_skips_appledouble_and_ds_store(workspace: Workspace) -> None:
    """源码 instance/ 里的 ``._*`` / ``.DS_Store`` 不得种进持久化目录。"""
    root = workspace.app_current("api")
    _write_factory(root, "os.path.join(app.instance_path, 'flaskr.sqlite')")
    src_instance = root / "instance"
    src_instance.mkdir(parents=True)
    (src_instance / "flaskr.sqlite").write_bytes(b"source database")
    (src_instance / "._flaskr.sqlite").write_bytes(_APPLEDOUBLE)
    (src_instance / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1\x00")
    manifest = _mk_manifest(has_database=True, database_type="sqlite")
    generate_compose(manifest, workspace, host_port=18000)
    persisted = workspace.app_data("api") / "instance"
    assert (persisted / "flaskr.sqlite").read_bytes() == b"source database"
    assert not (persisted / "._flaskr.sqlite").exists()
    assert not (persisted / ".DS_Store").exists()


# ---- compose / doctor：data/ 目录非空库盘点 ------------------------------------


def test_env_db_url_ignores_appledouble_db_files(workspace: Workspace) -> None:
    """``._bookshelf.db``（AppleDouble）不得把唯一候选决策误判成 fail-closed。"""
    manifest = _mk_manifest(has_database=True, database_type="sqlite")
    workspace.ensure_app_dirs("api")
    data_dir = workspace.app_data("api")
    (data_dir / "app.sqlite").write_bytes(b"")  # 崩掉容器留下的 0 字节空库
    (data_dir / "bookshelf.db").write_bytes(b"x" * 1024)  # 真实数据
    (data_dir / "._bookshelf.db").write_bytes(_APPLEDOUBLE)  # 非空 AppleDouble
    text = generate_env(manifest, workspace, host_port=18000).read_text(encoding="utf-8")
    assert "DATABASE_URL=sqlite:////app/data/bookshelf.db" in text


def test_env_db_url_ignores_only_appledouble_db_file(workspace: Workspace) -> None:
    """data/ 只有 ``._x.db`` 时按零候选处理（维持默认名），不得选为元数据库。"""
    manifest = _mk_manifest(has_database=True, database_type="sqlite")
    workspace.ensure_app_dirs("api")
    (workspace.app_data("api") / "._app.db").write_bytes(_APPLEDOUBLE)
    text = generate_env(manifest, workspace, host_port=18000).read_text(encoding="utf-8")
    assert "DATABASE_URL=sqlite:////app/data/app.sqlite" in text


def test_doctor_nonempty_sqlite_files_ignores_appledouble(tmp_path: Path) -> None:
    """doctor 的盘点函数与 compose 同口径：``._*`` 不进候选列表。"""
    (tmp_path / "bookshelf.db").write_bytes(b"x" * 100)
    (tmp_path / "._bookshelf.db").write_bytes(_APPLEDOUBLE)
    names = [p.name for p in _nonempty_sqlite_files_in(tmp_path)]
    assert names == ["bookshelf.db"]


# ---- scanner：summarize / _walk 统计与 sqlite_files 推断 -----------------------


def test_summarize_ignores_appledouble_and_ds_store(tmp_path: Path) -> None:
    """顶层与子目录的 ``._*`` / ``.DS_Store`` 不计入统计，也不进 sqlite_files。"""
    (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "app.sqlite").write_bytes(b"x" * 100)
    (tmp_path / "._app.sqlite").write_bytes(_APPLEDOUBLE)
    (tmp_path / ".DS_Store").write_bytes(b"\x00" * 10)
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "data.db").write_bytes(b"y" * 100)
    (sub / "._data.db").write_bytes(_APPLEDOUBLE)
    (sub / ".DS_Store").write_bytes(b"\x00" * 10)

    summary = summarize(tmp_path)
    # sqlite_files[0] 必须是真库（BUG-991：不得被排序靠前的 ``._app.sqlite`` 抢占）。
    assert summary.sqlite_files == ["app.sqlite", "pkg/data.db"]
    assert "._app.sqlite" not in summary.top_files
    assert ".ds_store" not in summary.top_files
    assert summary.total_files == 3  # app.py + app.sqlite + pkg/data.db


# ---- security / folder_source / lifecycle：导入与指纹 --------------------------


def test_sanitize_zip_members_strips_appledouble() -> None:
    """zip 剥离：``._*`` 成员与 ``.DS_Store`` 一并剥离（任意层级）。"""
    result = sanitize_zip_members(["app.py", "._app.py", ".DS_Store", "pkg/._db.py"])
    kept = [name for i, name in enumerate(["app.py", "._app.py", ".DS_Store", "pkg/._db.py"])
            if i in result.keep_indices]
    assert kept == ["app.py"]
    assert result.categories.get("._*") == 2


def test_folder_source_pack_skips_appledouble(tmp_path: Path) -> None:
    """文件夹打包：``._*`` / ``.DS_Store`` 不进 zip。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (src / "._app.py").write_bytes(_APPLEDOUBLE)
    (src / ".DS_Store").write_bytes(b"\x00" * 10)
    dest = tmp_path / "out.zip"
    pack_source_dir(src, dest_zip=dest)
    with zipfile.ZipFile(dest) as zf:
        assert zf.namelist() == ["app.py"]


def test_source_hash_ignores_appledouble_changes(tmp_path: Path) -> None:
    """元数据文件的增删不得引起内容指纹抖动（无谓重部署）。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hi')\n", encoding="utf-8")
    before = compute_source_hash(src)
    fingerprint_before = _compute_source_fingerprint(str(src))
    (src / "._app.py").write_bytes(_APPLEDOUBLE)
    (src / ".DS_Store").write_bytes(b"\x00" * 10)
    assert compute_source_hash(src) == before
    assert _compute_source_fingerprint(str(src)) == fingerprint_before
    (src / "._app.py").unlink()
    (src / ".DS_Store").unlink()
    assert compute_source_hash(src) == before
    assert _compute_source_fingerprint(str(src)) == fingerprint_before
