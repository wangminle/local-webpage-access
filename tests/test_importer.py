"""importer 模块测试（WBS-07）。"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest

from local_webpage_access.config import Config
from local_webpage_access.errors import ZipImportError
from local_webpage_access.importer import Importer, slugify, titleize
from local_webpage_access.models import Kind, Runtime, ServingMode, Status
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry


# ---- fixtures --------------------------------------------------------------


@pytest.fixture()
def workspace(workspace_root: Path) -> Workspace:
    ws = Workspace(workspace_root)
    ws.ensure_workspace_dirs()
    return ws


@pytest.fixture()
def registry(workspace_root: Path) -> Registry:
    workspace_root.joinpath("registry").mkdir(parents=True, exist_ok=True)
    reg = Registry(workspace_root / "registry" / "local-web.db")
    reg.open()
    yield reg
    reg.close()


@pytest.fixture()
def importer(workspace: Workspace, registry: Registry) -> Importer:
    return Importer(workspace, Config(), registry)


def _make_zip(zip_path: Path, files: dict[str, str]) -> Path:
    """创建一个 zip，files 为 {成员路径: 内容}。"""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for member, content in files.items():
            zf.writestr(member, content)
    return zip_path


# ---- slug ------------------------------------------------------------------


def test_slugify_basic() -> None:
    assert slugify("My Cool App!") == "my-cool-app"


def test_slugify_collapses_hyphens() -> None:
    assert slugify("a---b") == "a-b"


def test_slugify_empty_fallback() -> None:
    assert slugify("!!!") == "instance"


def test_slugify_truncates() -> None:
    assert len(slugify("x" * 100)) <= 40


def test_titleize() -> None:
    assert titleize("my-cool-app") == "My Cool App"


# ---- 基础导入 --------------------------------------------------------------


def test_import_static_html(importer: Importer, workspace: Workspace, tmp_path: Path) -> None:
    zip_path = _make_zip(
        tmp_path / "demo.zip",
        {"index.html": "<html><body>hi</body></html>", "style.css": "body{}"},
    )
    result = importer.import_zip(zip_path)

    assert result.instance_id == "demo"
    assert result.detection.kind == Kind.STATIC
    assert result.manifest.kind == Kind.STATIC
    assert result.manifest.runtime == Runtime.SHARED_STATIC
    assert result.manifest.status == Status.PENDING

    # 目录结构
    assert workspace.app_original_zip("demo").is_file()
    assert (workspace.app_current("demo") / "index.html").is_file()
    assert (workspace.app_current("demo") / "style.css").is_file()
    assert workspace.app_public("demo").is_dir()
    assert workspace.app_data("demo").is_dir()
    assert workspace.app_docker("demo").is_dir()
    assert workspace.app_manifest_path("demo").is_file()

    # zip hash 落盘
    assert result.zip_hash
    assert len(result.zip_hash) == 64
    assert result.manifest.sourceZipHash == result.zip_hash


def test_import_writes_registry(importer: Importer, registry: Registry, tmp_path: Path) -> None:
    zip_path = _make_zip(tmp_path / "demo.zip", {"index.html": "<html></html>"})
    result = importer.import_zip(zip_path)
    row = registry.get_instance(result.instance_id)
    assert row is not None
    assert row["kind"] == "static"
    assert row["status"] == "pending"

    events = registry.list_events(result.instance_id)
    assert any(e["event_type"] == "import" for e in events)


# ---- 单层根目录拍平 --------------------------------------------------------


def test_import_flattens_single_root(importer: Importer, workspace: Workspace, tmp_path: Path) -> None:
    zip_path = _make_zip(
        tmp_path / "wrapped.zip",
        {
            "my-project/index.html": "<html></html>",
            "my-project/style.css": "body{}",
        },
    )
    result = importer.import_zip(zip_path)
    # 拍平后 current/ 直接包含 index.html
    assert (workspace.app_current(result.instance_id) / "index.html").is_file()
    assert not (workspace.app_current(result.instance_id) / "my-project").exists()


def test_import_no_flatten_when_multiple_roots(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    zip_path = _make_zip(
        tmp_path / "multi.zip",
        {
            "index.html": "<html></html>",
            "subdir/a.txt": "a",
        },
    )
    result = importer.import_zip(zip_path)
    assert (workspace.app_current(result.instance_id) / "index.html").is_file()
    assert (workspace.app_current(result.instance_id) / "subdir" / "a.txt").is_file()


# ---- 同名冲突 --------------------------------------------------------------


def test_import_same_name_appends_suffix(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    zip_path = _make_zip(tmp_path / "demo.zip", {"index.html": "<html></html>"})
    r1 = importer.import_zip(zip_path)
    r2 = importer.import_zip(zip_path)
    assert r1.instance_id == "demo"
    assert r2.instance_id == "demo-2"


def test_import_custom_name(importer: Importer, tmp_path: Path) -> None:
    zip_path = _make_zip(tmp_path / "demo.zip", {"index.html": "<html></html>"})
    result = importer.import_zip(zip_path, name="My Custom Site")
    assert result.instance_id == "my-custom-site"
    assert result.manifest.name == "My Custom Site"


# ---- zip slip 防护 ---------------------------------------------------------


def test_import_rejects_zip_slip(importer: Importer, tmp_path: Path) -> None:
    """构造包含路径穿越成员的 zip，应被拒绝。"""
    zip_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("index.html", "<html></html>")
        # 手动写入一个 ../escape.txt 成员
        info = zipfile.ZipInfo("../escape.txt")
        zf.writestr(info, "pwned")
    with pytest.raises(ZipImportError, match="路径穿越"):
        importer.import_zip(zip_path)


# ---- 错误处理 --------------------------------------------------------------

def test_import_missing_file(importer: Importer, tmp_path: Path) -> None:
    with pytest.raises(ZipImportError, match="不存在"):
        importer.import_zip(tmp_path / "nope.zip")


def test_import_not_zip(importer: Importer, tmp_path: Path) -> None:
    fake = tmp_path / "fake.zip"
    fake.write_text("not a zip")
    with pytest.raises(ZipImportError):
        importer.import_zip(fake)


def test_import_wrong_extension(importer: Importer, tmp_path: Path) -> None:
    tar_path = tmp_path / "demo.tar"
    tar_path.write_bytes(b"")
    with pytest.raises(ZipImportError, match="仅支持"):
        importer.import_zip(tar_path)


# ---- 清理 ------------------------------------------------------------------


def test_failed_import_cleans_up(
    importer: Importer, workspace: Workspace, registry: Registry, tmp_path: Path, monkeypatch
) -> None:
    zip_path = _make_zip(tmp_path / "demo.zip", {"index.html": "<html></html>"})

    # 让扫描器抛错来触发失败路径
    def boom(_dir: Path):
        raise RuntimeError("scan boom")

    monkeypatch.setattr(importer.scanner, "detect", boom)
    with pytest.raises(ZipImportError):
        importer.import_zip(zip_path)

    # 目录应被清理
    assert not workspace.app_dir("demo").exists()
    assert registry.get_instance("demo") is None


# ---- 不同项目类型 ----------------------------------------------------------


def test_import_node_frontend(importer: Importer, tmp_path: Path) -> None:
    import json

    zip_path = _make_zip(
        tmp_path / "spa.zip",
        {
            "package.json": json.dumps(
                {
                    "dependencies": {"react": "^18.0.0"},
                    "scripts": {"build": "vite build"},
                }
            ),
        },
    )
    result = importer.import_zip(zip_path)
    assert result.manifest.kind == Kind.NODE
    assert result.manifest.runtime == Runtime.SHARED_STATIC


def test_import_python_backend(importer: Importer, tmp_path: Path) -> None:
    zip_path = _make_zip(
        tmp_path / "api.zip",
        {"requirements.txt": "fastapi\nuvicorn\n"},
    )
    result = importer.import_zip(zip_path)
    assert result.manifest.kind == Kind.PYTHON
    assert result.manifest.runtime == Runtime.DOCKER_COMPOSE
    assert result.manifest.container is not None
    assert result.manifest.container.internalPort == 8000


def test_import_unrecognized_marks_pending(importer: Importer, tmp_path: Path) -> None:
    zip_path = _make_zip(tmp_path / "mystery.zip", {"notes.txt": "hello"})
    result = importer.import_zip(zip_path)
    assert result.manifest.status == Status.PENDING
    assert result.manifest.lastError is not None


# ---- 回归测试：BUG-003 ----------------------------------------------------
#
# BUG-003：导入时把 zip 文件大小写进 data_size_bytes（列语义应为 data/ 目录大小）


def test_import_data_size_is_data_dir_not_zip(
    importer: Importer, registry: Registry, tmp_path: Path
) -> None:
    """data_size_bytes 应记录 data/ 目录大小（导入时为 0），不是 zip 文件大小。"""
    # 制造一个明显大于 0 的 zip（填充内容）
    payload = "x" * 4096
    zip_path = _make_zip(tmp_path / "demo.zip", {"index.html": f"<html>{payload}</html>"})
    zip_size = zip_path.stat().st_size
    assert zip_size > 0

    result = importer.import_zip(zip_path)
    resources = registry.get_resources(result.instance_id)
    assert resources is not None
    # source_size_bytes 记录 current/ 解压后大小，应 > 0
    assert resources["source_size_bytes"] > 0
    # data/ 导入时尚为空 → data_size_bytes 应为 0，绝不能等于 zip 文件大小
    assert resources["data_size_bytes"] == 0
    assert resources["data_size_bytes"] != zip_size
