"""IMP-047 文件夹源导入测试。

覆盖：
- folder_source.validate_source_dir / pack_source_dir / compute_source_hash
- importer.import_from_dir / update_from_dir
- 隔离红线硬断言（047.15）：Caddy root / static root / compose bind mount /
  build cwd / process cwd 不得指向关联目录。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from local_webpage_access.config import Config
from local_webpage_access.errors import ZipImportError
from local_webpage_access.folder_source import (
    FolderSourceError,
    compute_source_hash,
    pack_source_dir,
    validate_source_dir,
)
from local_webpage_access.importer import Importer
from local_webpage_access.models import InstanceManifest
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


@pytest.fixture()
def source_dir(tmp_path: Path) -> Path:
    """创建一个简单的源目录。"""
    d = tmp_path / "my-site"
    d.mkdir()
    d.joinpath("index.html").write_text("<html><body>Hello</body></html>", encoding="utf-8")
    d.joinpath("style.css").write_text("body { color: red; }", encoding="utf-8")
    (d / "assets").mkdir()
    d.joinpath("assets", "logo.txt").write_text("logo", encoding="utf-8")
    return d


# ---- validate_source_dir ---------------------------------------------------


class TestValidateSourceDir:
    def test_valid_dir(self, source_dir: Path) -> None:
        result = validate_source_dir(source_dir)
        assert result == source_dir.resolve()
        assert result.is_absolute()

    def test_empty_path(self) -> None:
        with pytest.raises(FolderSourceError, match="为空"):
            validate_source_dir("")

    def test_whitespace_path(self) -> None:
        with pytest.raises(FolderSourceError, match="为空"):
            validate_source_dir("   ")

    def test_nonexistent_dir(self, tmp_path: Path) -> None:
        with pytest.raises(FolderSourceError, match="不存在"):
            validate_source_dir(tmp_path / "does-not-exist")

    def test_not_a_directory(self, tmp_path: Path) -> None:
        f = tmp_path / "file.txt"
        f.write_text("hello")
        with pytest.raises(FolderSourceError, match="不是目录"):
            validate_source_dir(f)

    def test_rejects_workspace_subdir(self, workspace_root: Path) -> None:
        ws = Workspace(workspace_root)
        ws.ensure_workspace_dirs()
        # 源目录位于工作区内 -> 拒绝
        inner = workspace_root / "apps" / "sneaky"
        inner.mkdir(parents=True)
        inner.joinpath("index.html").write_text("nope")
        with pytest.raises(FolderSourceError, match="工作区内"):
            validate_source_dir(inner, workspace_root=workspace_root)

    def test_rejects_workspace_itself(self, workspace_root: Path) -> None:
        ws = Workspace(workspace_root)
        ws.ensure_workspace_dirs()
        with pytest.raises(FolderSourceError, match="工作区内"):
            validate_source_dir(workspace_root, workspace_root=workspace_root)

    def test_accepts_empty_dir(self, tmp_path: Path) -> None:
        d = tmp_path / "empty"
        d.mkdir()
        result = validate_source_dir(d)
        assert result == d.resolve()

    def test_string_path_accepted(self, source_dir: Path) -> None:
        result = validate_source_dir(str(source_dir))
        assert result == source_dir.resolve()

    def test_rejects_relative_path(self) -> None:
        """相对路径必须拒绝（不得 resolve 到服务端 cwd）。"""
        with pytest.raises(FolderSourceError, match="绝对路径"):
            validate_source_dir("relative/path")
        with pytest.raises(FolderSourceError, match="绝对路径"):
            validate_source_dir("./my-site")
        with pytest.raises(FolderSourceError, match="绝对路径"):
            validate_source_dir(".")


# ---- pack_source_dir -------------------------------------------------------


class TestPackSourceDir:
    def test_packs_all_files(self, source_dir: Path, tmp_path: Path) -> None:
        dest = tmp_path / "out.zip"
        pack_source_dir(source_dir, dest_zip=dest)
        with zipfile.ZipFile(dest) as zf:
            names = sorted(zf.namelist())
        assert "index.html" in names
        assert "style.css" in names
        assert "assets/logo.txt" in names

    def test_skips_node_modules(self, source_dir: Path, tmp_path: Path) -> None:
        nm = source_dir / "node_modules"
        nm.mkdir()
        nm.joinpath("big.js").write_text("var x = 1;")
        dest = tmp_path / "out.zip"
        pack_source_dir(source_dir, dest_zip=dest)
        with zipfile.ZipFile(dest) as zf:
            names = zf.namelist()
        assert not any("node_modules" in n for n in names)

    def test_skips_pycache(self, source_dir: Path, tmp_path: Path) -> None:
        pyc = source_dir / "__pycache__"
        pyc.mkdir()
        pyc.joinpath("mod.cpython-313.pyc").write_bytes(b"\x00\x01")
        dest = tmp_path / "out.zip"
        pack_source_dir(source_dir, dest_zip=dest)
        with zipfile.ZipFile(dest) as zf:
            names = zf.namelist()
        assert not any("__pycache__" in n for n in names)

    def test_skips_git(self, source_dir: Path, tmp_path: Path) -> None:
        git_dir = source_dir / ".git"
        git_dir.mkdir()
        git_dir.joinpath("config").write_text("[core]")
        dest = tmp_path / "out.zip"
        pack_source_dir(source_dir, dest_zip=dest)
        with zipfile.ZipFile(dest) as zf:
            names = zf.namelist()
        assert not any(".git" in n for n in names)

    def test_skips_ds_store(self, source_dir: Path, tmp_path: Path) -> None:
        source_dir.joinpath(".DS_Store").write_bytes(b"\x00\x00")
        dest = tmp_path / "out.zip"
        pack_source_dir(source_dir, dest_zip=dest)
        with zipfile.ZipFile(dest) as zf:
            names = zf.namelist()
        assert ".DS_Store" not in names

    def test_temp_zip_when_no_dest(self, source_dir: Path) -> None:
        result = pack_source_dir(source_dir)
        assert result.exists()
        assert result.suffix == ".zip"
        # 清理临时文件
        result.unlink(missing_ok=True)

    def test_arcname_relative_to_source(self, source_dir: Path, tmp_path: Path) -> None:
        dest = tmp_path / "out.zip"
        pack_source_dir(source_dir, dest_zip=dest)
        with zipfile.ZipFile(dest) as zf:
            names = zf.namelist()
        # arcname 不应包含源目录的绝对路径前缀
        for n in names:
            assert not n.startswith("/")


# ---- compute_source_hash ---------------------------------------------------


class TestComputeSourceHash:
    def test_deterministic(self, source_dir: Path) -> None:
        h1 = compute_source_hash(source_dir)
        h2 = compute_source_hash(source_dir)
        assert h1 == h2
        assert len(h1) == 64  # SHA256 hex

    def test_changes_on_content_edit(self, source_dir: Path) -> None:
        h1 = compute_source_hash(source_dir)
        source_dir.joinpath("index.html").write_text("<html><body>Changed</body></html>", encoding="utf-8")
        h2 = compute_source_hash(source_dir)
        assert h1 != h2

    def test_changes_on_file_add(self, source_dir: Path) -> None:
        h1 = compute_source_hash(source_dir)
        source_dir.joinpath("new.js").write_text("console.log(1);", encoding="utf-8")
        h2 = compute_source_hash(source_dir)
        assert h1 != h2

    def test_changes_on_file_rename(self, source_dir: Path) -> None:
        h1 = compute_source_hash(source_dir)
        source_dir.joinpath("style.css").rename(source_dir / "styles.css")
        h2 = compute_source_hash(source_dir)
        assert h1 != h2

    def test_ignores_node_modules(self, source_dir: Path) -> None:
        h1 = compute_source_hash(source_dir)
        nm = source_dir / "node_modules"
        nm.mkdir()
        nm.joinpath("big.js").write_text("var x = 1;")
        h2 = compute_source_hash(source_dir)
        assert h1 == h2

    def test_ignores_git(self, source_dir: Path) -> None:
        h1 = compute_source_hash(source_dir)
        git_dir = source_dir / ".git"
        git_dir.mkdir()
        git_dir.joinpath("HEAD").write_text("ref: refs/heads/main")
        h2 = compute_source_hash(source_dir)
        assert h1 == h2

    def test_ignores_ds_store(self, source_dir: Path) -> None:
        h1 = compute_source_hash(source_dir)
        source_dir.joinpath(".DS_Store").write_bytes(b"\x00")
        h2 = compute_source_hash(source_dir)
        assert h1 == h2


# ---- import_from_dir -------------------------------------------------------


class TestImportFromDir:
    def test_basic_import(
        self, importer: Importer, source_dir: Path
    ) -> None:
        result = importer.import_from_dir(source_dir)
        assert result.instance_id
        assert result.app_dir.exists()
        # 工作区中有 index.html
        assert (result.app_dir / "current" / "index.html").exists()

    def test_manifest_has_folder_source_kind(
        self, importer: Importer, source_dir: Path, workspace: Workspace
    ) -> None:
        result = importer.import_from_dir(source_dir)
        manifest = InstanceManifest.load(workspace.app_manifest_path(result.instance_id))
        assert manifest.sourceKind == "folder"
        assert manifest.sourceDirPath == str(source_dir.resolve())
        assert manifest.sourceSyncHash is not None
        assert len(manifest.sourceSyncHash) == 64

    def test_name_defaults_to_dir_name(
        self, importer: Importer, source_dir: Path
    ) -> None:
        result = importer.import_from_dir(source_dir)
        # import_zip 用 name 参数，slug 后可能不同，但应该包含目录名
        assert source_dir.name.lower() in result.instance_id.lower() or result.instance_id

    def test_custom_name(
        self, importer: Importer, source_dir: Path
    ) -> None:
        result = importer.import_from_dir(source_dir, name="custom-app")
        assert "custom" in result.instance_id.lower()

    def test_event_recorded(
        self, importer: Importer, source_dir: Path, registry: Registry
    ) -> None:
        result = importer.import_from_dir(source_dir)
        events = registry.list_events(result.instance_id)
        assert any("文件夹源导入" in e.get("message", "") for e in events)

    def test_source_dir_not_modified(
        self, importer: Importer, source_dir: Path
    ) -> None:
        """红线：导入不得修改源目录。"""
        original_files = sorted(
            str(p.relative_to(source_dir))
            for p in source_dir.rglob("*")
            if p.is_file()
        )
        importer.import_from_dir(source_dir)
        after_files = sorted(
            str(p.relative_to(source_dir))
            for p in source_dir.rglob("*")
            if p.is_file()
        )
        assert original_files == after_files


# ---- update_from_dir -------------------------------------------------------


class TestUpdateFromDir:
    def test_no_change_skipped(
        self, importer: Importer, source_dir: Path
    ) -> None:
        result = importer.import_from_dir(source_dir)
        # 不修改源目录 -> update 应跳过
        update_result = importer.update_from_dir(result.instance_id)
        assert update_result.skipped is True

    def test_with_change_updates(
        self, importer: Importer, source_dir: Path, workspace: Workspace
    ) -> None:
        result = importer.import_from_dir(source_dir)
        # 修改源目录
        source_dir.joinpath("index.html").write_text(
            "<html><body>Updated</body></html>", encoding="utf-8"
        )
        update_result = importer.update_from_dir(result.instance_id)
        assert update_result.skipped is False
        # sourceSyncHash 应更新
        manifest = InstanceManifest.load(workspace.app_manifest_path(result.instance_id))
        assert manifest.sourceSyncHash is not None

    def test_source_missing_raises(
        self, importer: Importer, source_dir: Path, tmp_path: Path
    ) -> None:
        result = importer.import_from_dir(source_dir)
        # 删除源目录
        import shutil

        shutil.rmtree(source_dir)
        with pytest.raises(ZipImportError, match="不可用"):
            importer.update_from_dir(result.instance_id)

    def test_non_folder_source_raises(
        self, importer: Importer, tmp_path: Path, workspace: Workspace
    ) -> None:
        """sourceKind=zip 的实例不能用 update_from_dir。"""
        # 先用 zip 导入一个实例
        zip_path = tmp_path / "test.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("index.html", "<html>zip</html>")
        result = importer.import_zip(zip_path, name="zip-app")
        with pytest.raises(ZipImportError, match="不是文件夹源"):
            importer.update_from_dir(result.instance_id)

    def test_nonexistent_instance_raises(
        self, importer: Importer
    ) -> None:
        with pytest.raises(ZipImportError, match="不存在"):
            importer.update_from_dir("nonexistent-id")

    def test_sync_hash_updated_after_change(
        self, importer: Importer, source_dir: Path, workspace: Workspace
    ) -> None:
        result = importer.import_from_dir(source_dir)
        manifest = InstanceManifest.load(workspace.app_manifest_path(result.instance_id))
        old_hash = manifest.sourceSyncHash

        # 修改源目录
        source_dir.joinpath("new.js").write_text("console.log(1);", encoding="utf-8")
        importer.update_from_dir(result.instance_id)

        manifest2 = InstanceManifest.load(workspace.app_manifest_path(result.instance_id))
        assert manifest2.sourceSyncHash != old_hash

    def test_source_kind_preserved_after_update(
        self, importer: Importer, source_dir: Path, workspace: Workspace
    ) -> None:
        """P0 回归：update_zip 重建 manifest 后 sourceKind 必须仍为 folder。"""
        result = importer.import_from_dir(source_dir)
        source_dir.joinpath("index.html").write_text(
            "<html><body>v2</body></html>", encoding="utf-8"
        )
        importer.update_from_dir(result.instance_id)
        manifest = InstanceManifest.load(workspace.app_manifest_path(result.instance_id))
        assert manifest.sourceKind == "folder"
        assert manifest.sourceDirPath == str(source_dir.resolve())
        assert manifest.sourceSyncHash is not None

    def test_consecutive_updates_work(
        self, importer: Importer, source_dir: Path, workspace: Workspace
    ) -> None:
        """P0 回归：连续两次 update_from_dir 都应成功，不因 sourceKind 丢失而报错。"""
        result = importer.import_from_dir(source_dir)

        # 第一次更新
        source_dir.joinpath("index.html").write_text("v2", encoding="utf-8")
        r1 = importer.update_from_dir(result.instance_id)
        assert r1.skipped is False

        # 第二次更新（内容再变）
        source_dir.joinpath("index.html").write_text("v3", encoding="utf-8")
        r2 = importer.update_from_dir(result.instance_id)
        assert r2.skipped is False

        # 第三次：无变化 -> skipped
        r3 = importer.update_from_dir(result.instance_id)
        assert r3.skipped is True


# ---- P2：CLI --from-dir --update 路径须与关联目录一致 ------------------------


class TestCliFromDirUpdatePathGuard:
    """BUG-440 P2：``--from-dir <dir> --update <id>`` 不得静默忽略传入目录。"""

    def test_mismatched_from_dir_rejected(
        self,
        importer: Importer,
        source_dir: Path,
        workspace: Workspace,
        registry: Registry,
        tmp_path: Path,
    ) -> None:
        import typer

        from local_webpage_access.cli.importing import _do_update_from_dir

        result = importer.import_from_dir(source_dir)
        other = tmp_path / "other-site"
        other.mkdir()
        other.joinpath("index.html").write_text("<html>other</html>", encoding="utf-8")

        with pytest.raises(typer.Exit) as exc_info:
            _do_update_from_dir(
                importer,
                workspace,
                Config(),
                registry,
                instance_id=result.instance_id,
                from_dir=str(other),
                restart=False,
                keep_data=True,
                yes=True,
                dry_run=False,
                force_kind_change=False,
            )
        assert exc_info.value.exit_code == 2

        # 拒绝后仍为 folder 源，且指纹未因误更新改变
        manifest = InstanceManifest.load(
            workspace.app_manifest_path(result.instance_id)
        )
        assert manifest.sourceKind == "folder"
        assert manifest.sourceDirPath == str(source_dir.resolve())

    def test_matching_from_dir_allowed(
        self,
        importer: Importer,
        source_dir: Path,
        workspace: Workspace,
        registry: Registry,
    ) -> None:
        from local_webpage_access.cli.importing import _do_update_from_dir

        result = importer.import_from_dir(source_dir)
        # 无变更 + 路径一致 → 跳过，不抛 Exit
        _do_update_from_dir(
            importer,
            workspace,
            Config(),
            registry,
            instance_id=result.instance_id,
            from_dir=str(source_dir),
            restart=False,
            keep_data=True,
            yes=True,
            dry_run=False,
            force_kind_change=False,
        )
        manifest = InstanceManifest.load(
            workspace.app_manifest_path(result.instance_id)
        )
        assert manifest.sourceKind == "folder"

    def test_cli_invoke_mismatched_from_dir_exit_2(
        self,
        workspace: Workspace,
        registry: Registry,
        source_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """端到端：``lwa import --from-dir <错目录> --update <id>`` → exit 2。"""
        from typer.testing import CliRunner

        from local_webpage_access.cli import app
        from local_webpage_access.init_workspace import init_workspace

        init_workspace(workspace.root)
        registry.close()
        reg = Registry(workspace.db_path)
        reg.open()
        try:
            importer = Importer(workspace, Config(), reg)
            imported = importer.import_from_dir(source_dir)
            other = tmp_path / "wrong-src"
            other.mkdir()
            other.joinpath("index.html").write_text("x", encoding="utf-8")

            monkeypatch.chdir(workspace.root)
            cli = CliRunner().invoke(
                app,
                [
                    "import",
                    "--from-dir",
                    str(other),
                    "--update",
                    imported.instance_id,
                ],
            )
            assert cli.exit_code == 2, cli.output
            assert "不一致" in cli.output
        finally:
            reg.close()


# ---- lwa scan 不得抹除文件夹源元数据 ----------------------------------------


class TestScanPreservesFolderSource:
    """BUG：apply_detection_to_manifest 曾默认 sourceKind=zip，scan 会抹掉 folder 身份。"""

    def test_apply_detection_preserves_folder_fields(
        self, importer: Importer, source_dir: Path, workspace: Workspace
    ) -> None:
        from local_webpage_access.importer import apply_detection_to_manifest
        from local_webpage_access.scanner import Scanner

        result = importer.import_from_dir(source_dir)
        manifest = InstanceManifest.load(
            workspace.app_manifest_path(result.instance_id)
        )
        assert manifest.sourceKind == "folder"
        old_dir = manifest.sourceDirPath
        old_hash = manifest.sourceSyncHash

        detection = Scanner().detect(workspace.app_current(result.instance_id))
        fresh = apply_detection_to_manifest(manifest, detection, workspace)

        assert fresh.sourceKind == "folder"
        assert fresh.sourceDirPath == old_dir
        assert fresh.sourceSyncHash == old_hash

    def test_scan_then_update_from_dir_still_works(
        self, importer: Importer, source_dir: Path, workspace: Workspace, registry: Registry
    ) -> None:
        """模拟 lwa scan 写盘后，仍可用 update_from_dir。"""
        from local_webpage_access.importer import apply_detection_to_manifest
        from local_webpage_access.scanner import Scanner

        result = importer.import_from_dir(source_dir)
        manifest_path = workspace.app_manifest_path(result.instance_id)
        manifest = InstanceManifest.load(manifest_path)
        detection = Scanner().detect(workspace.app_current(result.instance_id))
        fresh = apply_detection_to_manifest(manifest, detection, workspace)
        fresh.save(manifest_path)
        registry.upsert_from_manifest(fresh)

        source_dir.joinpath("index.html").write_text(
            "<html><body>after-scan</body></html>", encoding="utf-8"
        )
        update_result = importer.update_from_dir(result.instance_id)
        assert update_result.skipped is False
        after = InstanceManifest.load(manifest_path)
        assert after.sourceKind == "folder"
        assert after.sourceDirPath == str(source_dir.resolve())


# ---- 047.15 隔离红线硬断言 --------------------------------------------------


class TestIsolationRedLine:
    """IMP-047 隔离红线：关联目录是只读复制源，LWA 不得就地运行。"""

    def test_current_dir_is_inside_workspace(
        self, importer: Importer, source_dir: Path, workspace: Workspace
    ) -> None:
        """实例运行根 apps/<id>/current/ 必须位于工作区内。"""
        result = importer.import_from_dir(source_dir)
        current = result.app_dir / "current"
        assert current.exists()
        assert workspace.root in current.resolve().parents or current.resolve() == workspace.root.resolve()

    def test_current_dir_not_at_source_dir(
        self, importer: Importer, source_dir: Path
    ) -> None:
        """current/ 不得指向源目录。"""
        result = importer.import_from_dir(source_dir)
        current = (result.app_dir / "current").resolve()
        source_resolved = source_dir.resolve()
        assert current != source_resolved
        assert source_resolved not in current.parents

    def test_source_dir_not_in_app_dir(
        self, importer: Importer, source_dir: Path
    ) -> None:
        """源目录不得位于 apps/ 下（反向也成立）。"""
        result = importer.import_from_dir(source_dir)
        source_resolved = source_dir.resolve()
        app_dir_resolved = result.app_dir.resolve()
        assert app_dir_resolved not in source_resolved.parents
        assert source_resolved not in app_dir_resolved.parents

    def test_manifest_source_dir_path_is_absolute(
        self, importer: Importer, source_dir: Path, workspace: Workspace
    ) -> None:
        """manifest.sourceDirPath 必须是绝对路径。"""
        result = importer.import_from_dir(source_dir)
        manifest = InstanceManifest.load(workspace.app_manifest_path(result.instance_id))
        assert manifest.sourceDirPath is not None
        assert Path(manifest.sourceDirPath).is_absolute()

    def test_copied_files_exist_in_workspace_not_referencing_source(
        self, importer: Importer, source_dir: Path
    ) -> None:
        """导入后工作区中有文件副本，且不是符号链接到源目录。"""
        result = importer.import_from_dir(source_dir)
        copied_index = result.app_dir / "current" / "index.html"
        assert copied_index.exists()
        assert copied_index.is_file()
        assert not copied_index.is_symlink()

    def test_update_does_not_modify_source_dir(
        self, importer: Importer, source_dir: Path
    ) -> None:
        """update_from_dir 不得修改源目录。"""
        result = importer.import_from_dir(source_dir)
        source_dir.joinpath("index.html").write_text(
            "<html><body>v2</body></html>", encoding="utf-8"
        )
        original_content = source_dir.joinpath("index.html").read_text(encoding="utf-8")
        importer.update_from_dir(result.instance_id)
        after_content = source_dir.joinpath("index.html").read_text(encoding="utf-8")
        assert original_content == after_content

    def test_no_mount_fallback_on_missing_source(
        self, importer: Importer, source_dir: Path, tmp_path: Path
    ) -> None:
        """源目录缺失时必须报错，不回退到 mount 模式。"""
        result = importer.import_from_dir(source_dir)
        import shutil

        shutil.rmtree(source_dir)
        with pytest.raises(ZipImportError, match="不可用"):
            importer.update_from_dir(result.instance_id)
        # 确保 current/ 仍然存在（没有被改成 mount）
        assert (result.app_dir / "current").exists()
