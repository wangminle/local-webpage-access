"""zip_processor 单元测试（DEV-045 / WBS-20260708 阶段5.2）。

直接覆盖从 importer 抽出的 :func:`validate_zip` / :func:`compute_zip_hash` /
:func:`safe_extract`，锁定抽取后的契约；端到端导入行为仍由 test_importer.py 覆盖。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from local_webpage_access.errors import ZipImportError
from local_webpage_access.zip_processor import (
    compute_zip_hash,
    safe_extract,
    validate_zip,
)


def _make_zip(zip_path: Path, files: dict[str, str]) -> Path:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for member, content in files.items():
            zf.writestr(member, content)
    return zip_path


# ---- validate_zip ----------------------------------------------------------


def test_validate_zip_accepts_valid(tmp_path: Path) -> None:
    zp = _make_zip(tmp_path / "ok.zip", {"index.html": "<h1>hi</h1>"})
    validate_zip(zp)  # 不抛


def test_validate_zip_rejects_missing(tmp_path: Path) -> None:
    with pytest.raises(ZipImportError):
        validate_zip(tmp_path / "nope.zip")


def test_validate_zip_rejects_non_zip(tmp_path: Path) -> None:
    p = tmp_path / "x.txt"
    p.write_text("not a zip")
    with pytest.raises(ZipImportError):
        validate_zip(p)


def test_validate_zip_rejects_wrong_suffix(tmp_path: Path) -> None:
    zp = _make_zip(tmp_path / "ok.zip", {"index.html": "x"})
    renamed = zp.rename(tmp_path / "ok.tar")
    with pytest.raises(ZipImportError):
        validate_zip(renamed)


def test_validate_zip_rejects_truncated(tmp_path: Path) -> None:
    """截断的 zip 应被 testzip / BadZipFile 捕获。"""
    zp = _make_zip(tmp_path / "trunc.zip", {"a.txt": "x" * 5000})
    data = zp.read_bytes()
    zp.write_bytes(data[: len(data) // 2])  # 砍掉一半
    with pytest.raises(ZipImportError):
        validate_zip(zp)


# ---- compute_zip_hash ------------------------------------------------------


def test_compute_zip_hash_is_deterministic(tmp_path: Path) -> None:
    zp = _make_zip(tmp_path / "a.zip", {"index.html": "<h1>hi</h1>"})
    assert compute_zip_hash(zp) == compute_zip_hash(zp)
    assert len(compute_zip_hash(zp)) == 64  # sha256 hex


def test_compute_zip_hash_differs_by_content(tmp_path: Path) -> None:
    z1 = _make_zip(tmp_path / "z1.zip", {"index.html": "a"})
    z2 = _make_zip(tmp_path / "z2.zip", {"index.html": "b"})
    assert compute_zip_hash(z1) != compute_zip_hash(z2)


# ---- safe_extract ----------------------------------------------------------


def test_safe_extract_flattens_single_root(tmp_path: Path) -> None:
    zp = _make_zip(tmp_path / "rooted.zip", {"proj/index.html": "<h1>x</h1>"})
    target = tmp_path / "current"
    safe_extract(zp, target)
    # 单层根目录被拍平：index.html 直接落在 target 下
    assert (target / "index.html").is_file()
    assert not (target / "proj").exists()


def test_safe_extract_keeps_flat_layout(tmp_path: Path) -> None:
    zp = _make_zip(tmp_path / "flat.zip", {"index.html": "x", "style.css": "y"})
    target = tmp_path / "current"
    safe_extract(zp, target)
    assert (target / "index.html").is_file()
    assert (target / "style.css").is_file()


def test_safe_extract_strips_redundant_members(tmp_path: Path) -> None:
    """IMP-001：node_modules / .DS_Store 等被剥离，不落盘。"""
    zp = _make_zip(
        tmp_path / "mixed.zip",
        {
            "index.html": "x",
            "node_modules/leftpad/index.js": "module.exports=()=>{}",
            ".DS_Store": "junk",
            "__MACOSX/index.html": "mac meta",
        },
    )
    target = tmp_path / "current"
    result = safe_extract(zp, target)
    assert (target / "index.html").is_file()
    assert not (target / "node_modules").exists()
    assert not (target / ".DS_Store").exists()
    assert not (target / "__MACOSX").exists()
    # 剥离摘要记录了被去掉的成员
    assert result.stripped_names
    assert "node_modules" in result.categories


def test_safe_extract_blocks_zip_slip(tmp_path: Path) -> None:
    """绝对路径 / 路径穿越成员被安全审计拒绝。"""
    zp = tmp_path / "evil.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("index.html", "ok")
        info = zipfile.ZipInfo("/etc/pwned")
        zf.writestr(info, "pwned")
    with pytest.raises(ZipImportError):
        safe_extract(zp, tmp_path / "current")
