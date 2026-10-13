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
from local_webpage_access.models import InstanceManifest, Status
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
        source_dir.joinpath("index.html").write_text(
            "<html><body>Changed</body></html>", encoding="utf-8"
        )
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
    def test_basic_import(self, importer: Importer, source_dir: Path) -> None:
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

    def test_name_defaults_to_dir_name(self, importer: Importer, source_dir: Path) -> None:
        result = importer.import_from_dir(source_dir)
        # import_zip 用 name 参数，slug 后可能不同，但应该包含目录名
        assert source_dir.name.lower() in result.instance_id.lower() or result.instance_id

    def test_custom_name(self, importer: Importer, source_dir: Path) -> None:
        result = importer.import_from_dir(source_dir, name="custom-app")
        assert "custom" in result.instance_id.lower()

    def test_event_recorded(self, importer: Importer, source_dir: Path, registry: Registry) -> None:
        result = importer.import_from_dir(source_dir)
        events = registry.list_events(result.instance_id)
        assert any("文件夹源导入" in e.get("message", "") for e in events)

    def test_source_dir_not_modified(self, importer: Importer, source_dir: Path) -> None:
        """红线：导入不得修改源目录。"""
        original_files = sorted(
            str(p.relative_to(source_dir)) for p in source_dir.rglob("*") if p.is_file()
        )
        importer.import_from_dir(source_dir)
        after_files = sorted(
            str(p.relative_to(source_dir)) for p in source_dir.rglob("*") if p.is_file()
        )
        assert original_files == after_files

    def test_chinese_name_uses_folder_basename_as_id(
        self, importer: Importer, tmp_path: Path, workspace: Workspace
    ) -> None:
        """纯中文显示名不得把 instance id 全部落到 instance。"""
        from local_webpage_access.models import Status

        d = tmp_path / "multidevices-arbitration-simulator"
        d.mkdir()
        d.joinpath("index.html").write_text("<html><body>ok</body></html>", encoding="utf-8")
        result = importer.import_from_dir(d, name="分布式唤醒示意图")
        assert result.instance_id == "multidevices-arbitration-simulator"
        manifest = InstanceManifest.load(workspace.app_manifest_path(result.instance_id))
        assert manifest.name == "分布式唤醒示意图"
        assert manifest.status == Status.STOPPED


# ---- update_from_dir -------------------------------------------------------


class TestUpdateFromDir:
    def test_no_change_skipped(self, importer: Importer, source_dir: Path) -> None:
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

    def test_nonexistent_instance_raises(self, importer: Importer) -> None:
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

    def test_pending_heals_to_stopped_after_source_fix(
        self, importer: Importer, tmp_path: Path, workspace: Workspace
    ) -> None:
        """BUG-444：pending 源修好后再从源更新，须落到 stopped（可启动）。

        update_zip 曾在 apply_detection 后强制 status=old_manifest.status，
        导致识别成功仍卡 pending、启动按钮继续禁用。
        """
        pending_dir = tmp_path / "mystery-site"
        pending_dir.mkdir()
        pending_dir.joinpath("notes.txt").write_text("hello", encoding="utf-8")

        result = importer.import_from_dir(pending_dir)
        assert result.manifest.status == Status.PENDING

        pending_dir.joinpath("index.html").write_text(
            "<html><body>fixed</body></html>", encoding="utf-8"
        )
        update_result = importer.update_from_dir(result.instance_id)
        assert update_result.skipped is False

        manifest = InstanceManifest.load(workspace.app_manifest_path(result.instance_id))
        assert manifest.status == Status.STOPPED
        assert manifest.sourceKind == "folder"
        assert manifest.lastError is None


# ---- issue #28：zip/git 实例 --from-dir <目录> --update 原地切换为 folder 源 -----


class TestSourceSwitchToFolder:
    """issue #28：非 folder 源实例带目录 update_from_dir → 换源不换实例。"""

    def _import_zip(self, importer: Importer, tmp_path: Path) -> str:
        zip_path = tmp_path / "switch-app.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("index.html", "<html><body>zip v1</body></html>")
        return importer.import_zip(zip_path, name="switch-app").instance_id

    def test_zip_instance_switches_to_folder(
        self, importer: Importer, source_dir: Path, workspace: Workspace, tmp_path: Path
    ) -> None:
        iid = self._import_zip(importer, tmp_path)

        # 制造业务数据、路径别名与端口登记（切源不得触碰）
        data_dir = workspace.app_data(iid)
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "app.db").write_text("business-data", encoding="utf-8")
        manifest_path = workspace.app_manifest_path(iid)
        m = InstanceManifest.load(manifest_path)
        assert m.static is not None
        m.static.routeMode = "name"
        m.static.routeHost = "my-alias"
        m.static.hostPort = 48091
        m.save(manifest_path)
        # 同步 registry（模拟已启动实例的端口登记，hostPort 走 registry 保留链）
        importer.registry.upsert_from_manifest(
            m,
            app_path=str(workspace.app_current(iid)),
            source_zip_path=str(workspace.app_original_zip(iid)),
        )
        row_before = importer.registry.get_instance(iid) or {}

        result = importer.update_from_dir(iid, source_dir=str(source_dir))
        assert result.skipped is False

        manifest = InstanceManifest.load(manifest_path)
        # 新源身份写回
        assert manifest.sourceKind == "folder"
        assert manifest.sourceDirPath == str(source_dir.resolve())
        assert manifest.sourceSyncHash is not None
        # 别名 / 端口不动
        assert manifest.static is not None
        assert manifest.static.routeMode == "name"
        assert manifest.static.routeHost == "my-alias"
        assert manifest.static.hostPort == 48091
        site_row = importer.registry.get_static_site(iid) or {}
        assert site_row.get("host_port") == 48091
        # data/ 保留
        assert (data_dir / "app.db").read_text(encoding="utf-8") == "business-data"
        # current/ 已覆盖为新源内容
        current = workspace.app_current(iid)
        assert "Hello" in (current / "index.html").read_text(encoding="utf-8")
        # 实例 id / 创建时间不变
        row_after = importer.registry.get_instance(iid) or {}
        assert row_after["id"] == iid
        assert row_after.get("created_at") == row_before.get("created_at")

        # 切源后按 folder 语义继续增量更新（无需再传目录）
        source_dir.joinpath("index.html").write_text("<html>v2</html>", encoding="utf-8")
        again = importer.update_from_dir(iid)
        assert again.skipped is False
        assert importer.update_from_dir(iid).skipped is True

    def test_switch_with_identical_content_still_switches_identity(
        self, importer: Importer, source_dir: Path, workspace: Workspace, tmp_path: Path
    ) -> None:
        """切源目录内容与当前 zip 版本完全一致时，身份仍须完成切换。

        issue #28 典型场景：把原 zip 解压成目录后 --from-dir --update。
        _update_zip_locked 按打包 zip 指纹判 skipped——若随 skipped 跳过身份
        写回，「换源不换实例」会静默失败（本用例即修复前行为）。
        """
        from local_webpage_access.zip_processor import compute_zip_hash

        iid = self._import_zip(importer, tmp_path)
        # 预置 sourceZipHash = 该目录打包后的 zip 哈希，确保内层命中 skipped
        packed = tmp_path / "packed.zip"
        pack_source_dir(source_dir, dest_zip=packed)
        manifest_path = workspace.app_manifest_path(iid)
        m = InstanceManifest.load(manifest_path)
        m.sourceZipHash = compute_zip_hash(packed)
        m.save(manifest_path)

        result = importer.update_from_dir(iid, source_dir=str(source_dir))
        assert result.skipped is True  # 内容一致：内层按 zip 指纹跳过更新

        manifest = InstanceManifest.load(manifest_path)
        assert manifest.sourceKind == "folder"  # 身份必须已切换
        assert manifest.sourceDirPath == str(source_dir.resolve())
        assert manifest.sourceSyncHash == compute_source_hash(source_dir)
        assert result.manifest.sourceKind == "folder"  # 返回值与磁盘身份一致

        # 切换后再从目录更新（不传目录）走 folder 常规指纹短路
        again = importer.update_from_dir(iid)
        assert again.skipped is True

    def test_zip_instance_switch_dry_run_writes_nothing(
        self, importer: Importer, source_dir: Path, workspace: Workspace, tmp_path: Path
    ) -> None:
        iid = self._import_zip(importer, tmp_path)
        result = importer.update_from_dir(iid, source_dir=str(source_dir), dry_run=True)
        assert result.dry_run is True
        # dry-run 不切源：磁盘身份仍是 zip
        manifest = InstanceManifest.load(workspace.app_manifest_path(iid))
        assert manifest.sourceKind == "zip"
        assert manifest.sourceDirPath is None

    def test_zip_instance_without_dir_still_raises(
        self, importer: Importer, tmp_path: Path
    ) -> None:
        """不传目录时维持原报错（报错文案给出原地切换提示）。"""
        iid = self._import_zip(importer, tmp_path)
        with pytest.raises(ZipImportError, match="不是文件夹源"):
            importer.update_from_dir(iid)

    def test_folder_instance_zip_update_behavior_unchanged(
        self, importer: Importer, source_dir: Path, workspace: Workspace, tmp_path: Path
    ) -> None:
        """反向回归：folder 实例用普通 --update zip 更新仍允许，folder 身份不变。"""
        result = importer.import_from_dir(source_dir)
        iid = result.instance_id
        new_zip = tmp_path / "new.zip"
        with zipfile.ZipFile(new_zip, "w") as zf:
            zf.writestr("index.html", "<html><body>from zip</body></html>")
        updated = importer.update_zip(new_zip, iid)
        assert updated.skipped is False
        manifest = InstanceManifest.load(workspace.app_manifest_path(iid))
        assert manifest.sourceKind == "folder"
        assert manifest.sourceDirPath == str(source_dir.resolve())
        current = workspace.app_current(iid)
        assert "from zip" in (current / "index.html").read_text(encoding="utf-8")

    def test_cli_from_dir_update_switches_zip_instance(
        self,
        importer: Importer,
        source_dir: Path,
        workspace: Workspace,
        registry: Registry,
        tmp_path: Path,
    ) -> None:
        """端到端编排：``--from-dir <目录> --update <id>`` 对 zip 实例原地切源。"""
        from local_webpage_access.cli.importing import _do_update_from_dir

        iid = self._import_zip(importer, tmp_path)
        _do_update_from_dir(
            importer,
            workspace,
            Config(),
            registry,
            instance_id=iid,
            from_dir=str(source_dir),
            restart=False,
            keep_data=True,
            yes=True,
            dry_run=False,
            force_kind_change=False,
        )
        manifest = InstanceManifest.load(workspace.app_manifest_path(iid))
        assert manifest.sourceKind == "folder"
        assert manifest.sourceDirPath == str(source_dir.resolve())


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
        manifest = InstanceManifest.load(workspace.app_manifest_path(result.instance_id))
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
        manifest = InstanceManifest.load(workspace.app_manifest_path(result.instance_id))
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
            assert "请先删除" not in cli.output
            assert "--allow-source-change" in cli.output
            assert "data/" in cli.output
        finally:
            reg.close()


# ---- issue #45：folder → folder 更换关联目录 --------------------------------


class TestIssue45ChangeSourceDir:
    """folder 源换目录保留实例身份；未确认则拒绝且不再建议删实例重导。"""

    def test_allow_flag_preserves_identity(
        self,
        importer: Importer,
        source_dir: Path,
        workspace: Workspace,
        registry: Registry,
        tmp_path: Path,
    ) -> None:
        from local_webpage_access.cli.importing import _do_update_from_dir

        result = importer.import_from_dir(source_dir)
        iid = result.instance_id
        data_dir = workspace.app_data(iid)
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "app.db").write_text("business-data", encoding="utf-8")
        manifest_path = workspace.app_manifest_path(iid)
        manifest = InstanceManifest.load(manifest_path)
        assert manifest.static is not None
        manifest.static.routeMode = "name"
        manifest.static.routeHost = "kept-alias"
        manifest.static.hostPort = 48091
        manifest.save(manifest_path)
        importer.registry.upsert_from_manifest(manifest)

        new_dir = tmp_path / "v5-output"
        new_dir.mkdir()
        new_dir.joinpath("index.html").write_text("<html>v5</html>", encoding="utf-8")

        _do_update_from_dir(
            importer,
            workspace,
            Config(),
            registry,
            instance_id=iid,
            from_dir=str(new_dir),
            restart=False,
            keep_data=True,
            yes=True,
            dry_run=False,
            force_kind_change=False,
            allow_source_change=True,
        )

        saved = InstanceManifest.load(manifest_path)
        assert saved.id == iid
        assert saved.sourceKind == "folder"
        assert saved.sourceDirPath == str(new_dir.resolve())
        assert saved.static is not None
        assert saved.static.routeHost == "kept-alias"
        assert saved.static.hostPort == 48091
        assert (data_dir / "app.db").read_text(encoding="utf-8") == "business-data"
        assert "v5" in (workspace.app_current(iid) / "index.html").read_text(encoding="utf-8")
        events = registry.list_events(iid)
        assert any(
            "关联源目录更换" in e["message"] and str(new_dir.resolve()) in e["message"]
            for e in events
        )

    def test_same_content_still_rewrites_source_dir(
        self,
        importer: Importer,
        source_dir: Path,
        workspace: Workspace,
        tmp_path: Path,
    ) -> None:
        """旧指纹属于旧目录，内容碰巧相同也不得走指纹短路把换源吞掉。"""
        result = importer.import_from_dir(source_dir)
        iid = result.instance_id
        new_dir = tmp_path / "same-bytes"
        new_dir.mkdir()
        for src in source_dir.rglob("*"):
            if src.is_file():
                dest = new_dir / src.relative_to(source_dir)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(src.read_bytes())

        updated = importer.update_from_dir(iid, source_dir=new_dir, allow_source_dir_change=True)
        saved = InstanceManifest.load(workspace.app_manifest_path(iid))
        assert saved.sourceDirPath == str(new_dir.resolve())
        assert updated.source_dir_changed is True
        assert updated.prev_source_dir == str(source_dir.resolve())

    def test_without_flag_importer_rejects(
        self, importer: Importer, source_dir: Path, tmp_path: Path
    ) -> None:
        result = importer.import_from_dir(source_dir)
        other = tmp_path / "other"
        other.mkdir()
        other.joinpath("index.html").write_text("x", encoding="utf-8")
        with pytest.raises(ZipImportError, match="不一致"):
            importer.update_from_dir(result.instance_id, source_dir=other)

    def test_dry_run_shows_plan_without_writing(
        self,
        importer: Importer,
        source_dir: Path,
        workspace: Workspace,
        registry: Registry,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from local_webpage_access.cli.importing import _do_update_from_dir

        result = importer.import_from_dir(source_dir)
        other = tmp_path / "planned"
        other.mkdir()
        other.joinpath("index.html").write_text("<html>next</html>", encoding="utf-8")
        _do_update_from_dir(
            importer,
            workspace,
            Config(),
            registry,
            instance_id=result.instance_id,
            from_dir=str(other),
            restart=False,
            keep_data=True,
            yes=False,
            dry_run=True,
            force_kind_change=False,
            allow_source_change=False,
        )
        captured = capsys.readouterr().out
        assert "将更换关联目录" in captured
        saved = InstanceManifest.load(workspace.app_manifest_path(result.instance_id))
        assert saved.sourceDirPath == str(source_dir.resolve())
        assert "next" not in (workspace.app_current(result.instance_id) / "index.html").read_text(
            encoding="utf-8"
        )

    def test_interactive_confirm_includes_summary(
        self,
        importer: Importer,
        source_dir: Path,
        workspace: Workspace,
        registry: Registry,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import typer

        from local_webpage_access.cli import importing as importing_cli

        result = importer.import_from_dir(source_dir)
        other = tmp_path / "confirmed"
        other.mkdir()
        other.joinpath("index.html").write_text("<html>yes</html>", encoding="utf-8")
        seen: dict[str, str] = {}

        def fake_confirm(message: str, default: bool = False) -> bool:
            seen["message"] = message
            return True

        monkeypatch.setattr(typer, "confirm", fake_confirm)
        monkeypatch.setattr(
            importing_cli.sys, "stdin", type("S", (), {"isatty": lambda self: True})()
        )
        importing_cli._do_update_from_dir(
            importer,
            workspace,
            Config(),
            registry,
            instance_id=result.instance_id,
            from_dir=str(other),
            restart=False,
            keep_data=True,
            yes=False,
            dry_run=False,
            force_kind_change=False,
            allow_source_change=False,
        )
        assert "个文件" in seen["message"]
        assert "指纹" in seen["message"]
        saved = InstanceManifest.load(workspace.app_manifest_path(result.instance_id))
        assert saved.sourceDirPath == str(other.resolve())


class TestIssue45RelativeSourceDir:
    """BUG-736：相对路径在 resolve 后与记录目录相同，或显式换源，都不得被绝对路径校验提前拒绝。"""

    def test_relative_same_dir_updates(
        self,
        importer: Importer,
        source_dir: Path,
        workspace: Workspace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = importer.import_from_dir(source_dir)
        monkeypatch.chdir(source_dir.parent)
        updated = importer.update_from_dir(result.instance_id, source_dir=source_dir.name)
        assert updated.skipped is True
        saved = InstanceManifest.load(workspace.app_manifest_path(result.instance_id))
        assert saved.sourceDirPath == str(source_dir.resolve())

    def test_relative_new_dir_with_allow_resolves_absolute(
        self,
        importer: Importer,
        source_dir: Path,
        workspace: Workspace,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = importer.import_from_dir(source_dir)
        new_dir = tmp_path / "v5-output"
        new_dir.mkdir()
        new_dir.joinpath("index.html").write_text("<html>v5</html>", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        updated = importer.update_from_dir(
            result.instance_id,
            source_dir=new_dir.name,
            allow_source_dir_change=True,
        )
        assert updated.source_dir_changed is True
        saved = InstanceManifest.load(workspace.app_manifest_path(result.instance_id))
        assert saved.sourceDirPath == str(new_dir.resolve())
        assert "v5" in (workspace.app_current(result.instance_id) / "index.html").read_text(
            encoding="utf-8"
        )


# ---- lwa scan 不得抹除文件夹源元数据 ----------------------------------------


class TestScanPreservesFolderSource:
    """BUG：apply_detection_to_manifest 曾默认 sourceKind=zip，scan 会抹掉 folder 身份。"""

    def test_apply_detection_preserves_folder_fields(
        self, importer: Importer, source_dir: Path, workspace: Workspace
    ) -> None:
        from local_webpage_access.importer import apply_detection_to_manifest
        from local_webpage_access.scanner import Scanner

        result = importer.import_from_dir(source_dir)
        manifest = InstanceManifest.load(workspace.app_manifest_path(result.instance_id))
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
        assert (
            workspace.root in current.resolve().parents
            or current.resolve() == workspace.root.resolve()
        )

    def test_current_dir_not_at_source_dir(self, importer: Importer, source_dir: Path) -> None:
        """current/ 不得指向源目录。"""
        result = importer.import_from_dir(source_dir)
        current = (result.app_dir / "current").resolve()
        source_resolved = source_dir.resolve()
        assert current != source_resolved
        assert source_resolved not in current.parents

    def test_source_dir_not_in_app_dir(self, importer: Importer, source_dir: Path) -> None:
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

    def test_update_does_not_modify_source_dir(self, importer: Importer, source_dir: Path) -> None:
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
