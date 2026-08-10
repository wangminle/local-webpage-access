"""importer 模块测试（WBS-07）。"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from local_webpage_access.config import Config
from local_webpage_access.errors import PathError, RecognitionError, ZipImportError
from local_webpage_access.importer import (
    Importer,
    extract_html_title,
    find_homepage_index,
    is_auto_titleized_name,
    refresh_display_name_from_homepage,
    resolve_auto_display_name,
    slugify,
    titleize,
)
from local_webpage_access.models import (
    DesiredState,
    InstanceManifest,
    Kind,
    Runtime,
    ServingMode,
    Status,
)
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


def test_slugify_cjk_only_falls_back_to_instance() -> None:
    """纯中文无法产生 ASCII slug，slugify 仍回退 instance。"""
    assert slugify("分布式唤醒示意图") == "instance"


def test_slug_basis_prefers_path_when_name_is_cjk() -> None:
    """BUG：纯中文 --name 不应把所有导入都抢成 id=instance。"""
    from local_webpage_access.importer import slug_basis_for_id

    assert slug_basis_for_id(name="分布式唤醒示意图", path_stem="multidevices-arbitration-simulator") == (
        "multidevices-arbitration-simulator"
    )
    assert slugify(slug_basis_for_id(name="分布式唤醒示意图", path_stem="src")) == "src"
    # 名称本身含 ASCII 时仍用名称
    assert slug_basis_for_id(name="Demo 演示", path_stem="src") == "Demo 演示"
    assert slugify(slug_basis_for_id(name="Demo 演示", path_stem="src")) == "demo"


def test_slugify_truncates() -> None:
    assert len(slugify("x" * 100)) <= 40


def test_titleize() -> None:
    assert titleize("my-cool-app") == "My Cool App"


def test_extract_html_title_basic() -> None:
    assert extract_html_title("<html><head><title>AI 评审</title></head></html>") == "AI 评审"


def test_extract_html_title_entities_and_whitespace() -> None:
    html = "<html><title>\n  Foo&amp;Bar  Demo\n</title></html>"
    assert extract_html_title(html) == "Foo&Bar Demo"


def test_extract_html_title_preserves_entity_angle_brackets() -> None:
    """CHK-152/BUG-436：须先去标签再 unescape，否则 &lt;Admin&gt; 会被当标签删掉。"""
    html = "<html><title>&lt;Admin&gt; Dashboard</title></html>"
    assert extract_html_title(html) == "<Admin> Dashboard"


def test_extract_html_title_missing_or_empty() -> None:
    assert extract_html_title("<html><body>no title</body></html>") is None
    assert extract_html_title("<html><title>  </title></html>") is None


def test_find_homepage_index_prefers_root(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<title>Root</title>", encoding="utf-8")
    nested = tmp_path / "site"
    nested.mkdir()
    (nested / "index.html").write_text("<title>Nested</title>", encoding="utf-8")
    assert find_homepage_index(tmp_path) == tmp_path / "index.html"


def test_find_homepage_index_nested_fallback(tmp_path: Path) -> None:
    nested = tmp_path / "public"
    nested.mkdir()
    (nested / "index.html").write_text("<title>Nested</title>", encoding="utf-8")
    assert find_homepage_index(tmp_path) == nested / "index.html"


def test_find_homepage_index_supports_dist_and_build(tmp_path: Path) -> None:
    """CHK-152/BUG-435：预构建包只有 dist/index.html 或 build/index.html 时须能解析标题。"""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<title>Dist App</title>", encoding="utf-8")
    assert find_homepage_index(tmp_path) == dist / "index.html"
    assert resolve_auto_display_name(tmp_path, slug="dist-app") == "Dist App"

    build_root = tmp_path / "build-only"
    build_root.mkdir()
    build = build_root / "build"
    build.mkdir()
    (build / "index.html").write_text("<title>Build App</title>", encoding="utf-8")
    assert find_homepage_index(build_root) == build / "index.html"
    assert resolve_auto_display_name(build_root, slug="build-app") == "Build App"


def test_resolve_auto_display_name_prefers_html_title(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text(
        "<html><title>Distributed Wake Demo</title></html>", encoding="utf-8"
    )
    assert resolve_auto_display_name(tmp_path, slug="distributed-wake-demo") == (
        "Distributed Wake Demo"
    )


def test_resolve_auto_display_name_falls_back_to_titleize(tmp_path: Path) -> None:
    assert resolve_auto_display_name(tmp_path, slug="my-cool-app") == "My Cool App"


def test_import_uses_html_title_as_display_name(
    importer: Importer, registry: Registry, tmp_path: Path
) -> None:
    zip_path = _make_zip(
        tmp_path / "ai-review-prd-v0-2-7.zip",
        {
            "index.html": (
                "<html><head><title>AI Review · PRD</title></head>"
                "<body>hi</body></html>"
            )
        },
    )
    result = importer.import_zip(zip_path)
    assert result.manifest.name == "AI Review · PRD"
    row = registry.get_instance(result.instance_id)
    assert row is not None
    assert row["name"] == "AI Review · PRD"


def test_import_explicit_name_overrides_html_title(
    importer: Importer, tmp_path: Path
) -> None:
    zip_path = _make_zip(
        tmp_path / "demo.zip",
        {"index.html": "<html><title>From HTML</title></html>"},
    )
    result = importer.import_zip(zip_path, name="My Custom Site")
    assert result.manifest.name == "My Custom Site"


def test_is_auto_titleized_name() -> None:
    assert is_auto_titleized_name("Ai Review Prd V0 2 7", "ai-review-prd-v0-2-7")
    assert is_auto_titleized_name(
        "Ai Review Prd V0 2 7", "ai-review-prd-v0-2-7", name_source="slug"
    )
    assert not is_auto_titleized_name(
        "Keep Me", "keep-me", name_source="user"
    )
    assert not is_auto_titleized_name("AI Review · PRD", "ai-review-prd-v0-2-7")
    assert not is_auto_titleized_name(
        "AI Review · PRD", "ai-review-prd-v0-2-7", name_source="html_title"
    )


def test_refresh_display_name_from_homepage_updates_auto_name(
    workspace: Workspace, registry: Registry, importer: Importer, tmp_path: Path
) -> None:
    zip_path = _make_zip(
        tmp_path / "demo-site.zip",
        {"index.html": "<html><body>no title yet</body></html>"},
    )
    result = importer.import_zip(zip_path)
    assert result.manifest.name == "Demo Site"
    current = workspace.app_current(result.instance_id)
    (current / "index.html").write_text(
        "<html><title>真实站点名</title></html>", encoding="utf-8"
    )
    new_name = refresh_display_name_from_homepage(
        workspace, registry, result.instance_id
    )
    assert new_name == "真实站点名"
    row = registry.get_instance(result.instance_id)
    assert row is not None and row["name"] == "真实站点名"
    loaded = InstanceManifest.load(workspace.app_manifest_path(result.instance_id))
    assert loaded.name == "真实站点名"


def test_refresh_display_name_preserves_registered_host_port(
    workspace: Workspace, registry: Registry, importer: Importer, tmp_path: Path
) -> None:
    """BUG-410：显示名回填不得清空只存在于 registry 的 host_port。"""
    zip_path = _make_zip(
        tmp_path / "port-demo.zip",
        {"index.html": "<html><body>no title</body></html>"},
    )
    result = importer.import_zip(zip_path)
    instance_id = result.instance_id
    # 模拟 hosting 已登记端口，但未回写到 manifest.hostPort
    registry.upsert_static_site(instance_id, {"hostPort": 21001})
    manifest = InstanceManifest.load(workspace.app_manifest_path(instance_id))
    assert manifest.static is not None
    manifest.static.hostPort = None
    manifest.save(workspace.app_manifest_path(instance_id))
    assert registry.get_static_site(instance_id)["host_port"] == 21001

    (workspace.app_current(instance_id) / "index.html").write_text(
        "<html><title>端口保留测试</title></html>", encoding="utf-8"
    )
    assert (
        refresh_display_name_from_homepage(workspace, registry, instance_id)
        == "端口保留测试"
    )
    site = registry.get_static_site(instance_id)
    assert site is not None
    assert site["host_port"] == 21001
    row = registry.get_instance(instance_id)
    assert row is not None and row["name"] == "端口保留测试"


def test_refresh_display_name_skips_user_custom_name(
    workspace: Workspace, registry: Registry, importer: Importer, tmp_path: Path
) -> None:
    zip_path = _make_zip(
        tmp_path / "demo.zip",
        {"index.html": "<html><title>HTML Title</title></html>"},
    )
    result = importer.import_zip(zip_path, name="Keep Me")
    assert (
        refresh_display_name_from_homepage(workspace, registry, result.instance_id)
        is None
    )
    row = registry.get_instance(result.instance_id)
    assert row is not None and row["name"] == "Keep Me"


def test_refresh_display_name_holds_lock_and_rereads_manifest(
    workspace: Workspace,
    registry: Registry,
    importer: Importer,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """CHK-152/BUG-434：回填须持 instance_lock 并在锁内重读，避免覆盖并发状态写入。"""
    import contextlib

    from local_webpage_access.models import DesiredState, Status

    zip_path = _make_zip(
        tmp_path / "lock-demo.zip",
        {"index.html": "<html><body>no title</body></html>"},
    )
    result = importer.import_zip(zip_path)
    instance_id = result.instance_id
    manifest_path = workspace.app_manifest_path(instance_id)
    (workspace.app_current(instance_id) / "index.html").write_text(
        "<html><title>锁内回填</title></html>", encoding="utf-8"
    )

    lock_held = False
    saved_under_lock = False
    original_save = InstanceManifest.save

    def checked_save(self, path):
        nonlocal saved_under_lock
        saved_under_lock = lock_held
        return original_save(self, path)

    import local_webpage_access.lifecycle as lifecycle_mod

    real_lock = lifecycle_mod.instance_lock

    @contextlib.contextmanager
    def tracked_lock(ws, iid, **kwargs):
        nonlocal lock_held
        with real_lock(ws, iid, **kwargs):
            # 模拟并发 start：锁内、回填重读之前，运行态已写入磁盘
            live = InstanceManifest.load(manifest_path)
            live.desiredState = DesiredState.RUNNING
            live.status = Status.RUNNING
            original_save(live, manifest_path)
            lock_held = True
            try:
                yield
            finally:
                lock_held = False

    monkeypatch.setattr(InstanceManifest, "save", checked_save)
    monkeypatch.setattr(lifecycle_mod, "instance_lock", tracked_lock)

    assert (
        refresh_display_name_from_homepage(workspace, registry, instance_id)
        == "锁内回填"
    )
    assert saved_under_lock is True
    loaded = InstanceManifest.load(manifest_path)
    assert loaded.name == "锁内回填"
    assert loaded.desiredState == DesiredState.RUNNING
    assert loaded.status == Status.RUNNING


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
    # 识别成功 → stopped（可启动）；仅 detection.pending 才 status=pending
    assert result.manifest.status == Status.STOPPED
    assert result.manifest.lastError is None

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
    assert row["status"] == "stopped"

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


def test_import_claim_retries_when_directory_wins_race(
    importer: Importer, workspace: Workspace, tmp_path: Path, monkeypatch
) -> None:
    """BUG-127：同 slug 目录被并发抢占后，原子 claim 自动尝试 -2。"""
    zip_path = _make_zip(tmp_path / "demo.zip", {"index.html": "<html></html>"})
    original_mkdir = Path.mkdir
    raced = False

    def racing_mkdir(path, *args, **kwargs):  # noqa: ANN001
        nonlocal raced
        if path == workspace.app_dir("demo") and kwargs.get("exist_ok") is False:
            if not raced:
                raced = True
                original_mkdir(path, parents=True, exist_ok=False)
                raise FileExistsError(path)
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)
    result = importer.import_zip(zip_path)

    assert result.instance_id == "demo-2"
    assert workspace.app_dir("demo").is_dir()
    assert workspace.app_manifest_path("demo-2").is_file()


def test_import_custom_name(importer: Importer, tmp_path: Path) -> None:
    zip_path = _make_zip(tmp_path / "demo.zip", {"index.html": "<html></html>"})
    result = importer.import_zip(zip_path, name="My Custom Site")
    assert result.instance_id == "my-custom-site"
    assert result.manifest.name == "My Custom Site"


# ---- zip slip 防护 ---------------------------------------------------------


def test_import_rejects_zip_slip(importer: Importer, tmp_path: Path) -> None:
    """构造包含路径穿越成员的 zip，应被拒绝。

    BUG-049 后 audit_zip_members 在解压前集中拦截，错误消息为
    「zip 成员安全审计未通过（zip_slip）」；运行时路径校验作为兜底，
    仍产出「检测到路径穿越（zip slip）」消息。
    """
    zip_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("index.html", "<html></html>")
        # 手动写入一个 ../escape.txt 成员
        info = zipfile.ZipInfo("../escape.txt")
        zf.writestr(info, "pwned")
    with pytest.raises(ZipImportError, match="zip_slip"):
        importer.import_zip(zip_path)


def test_import_rejects_zip_symlink(importer: Importer, tmp_path: Path) -> None:
    """构造包含符号链接成员的 zip，应被 audit_zip_members 拦截（BUG-049）。

    symlink 成员的 external_attr 高 16 位为 S_IFLNK 模式，audit_zip_members
    检测到后以 critical 拒绝，杜绝指向解压目录外的符号链接。
    """
    import stat as stat_mod

    zip_path = tmp_path / "symlink.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("index.html", "<html></html>")
        info = zipfile.ZipInfo("link.txt")
        # 设置 S_IFLNK 模式位（符号链接）
        info.external_attr = (stat_mod.S_IFLNK | 0o777) << 16
        zf.writestr(info, "/etc/passwd")
    with pytest.raises(ZipImportError, match="zip_symlink"):
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


def test_import_non_index_html_is_static_stopped(
    importer: Importer, tmp_path: Path
) -> None:
    """非 index 文件名的 HTML 包应识别为静态且落盘 stopped（非 pending）。"""
    zip_path = _make_zip(
        tmp_path / "chapters.zip",
        {"kakeya-3d-chapters.html": "<html><title>K</title></html>"},
    )
    result = importer.import_zip(zip_path)
    assert result.detection.pending is False
    assert result.detection.form == "static"
    assert result.manifest.status == Status.STOPPED
    assert result.manifest.kind == Kind.STATIC


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


# ---- IMP-001：zip 导入前自动剥离冗余包与缓存 ------------------------------


def _make_zip_with_symlink_members(
    zip_path: Path, plain: dict[str, str], symlinks: dict[str, str]
) -> Path:
    """创建含符号链接成员的 zip。

    plain: {成员路径: 文本内容}
    symlinks: {成员路径: symlink 目标}（external_attr 设为 S_IFLNK）
    """
    import stat as stat_mod

    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for member, content in plain.items():
            zf.writestr(member, content)
        for member, target in symlinks.items():
            info = zipfile.ZipInfo(member)
            info.external_attr = (stat_mod.S_IFLNK | 0o777) << 16
            zf.writestr(info, target)
    return zip_path


def test_import_strips_node_modules_with_bin_symlink(
    importer: Importer, workspace: Workspace, registry: Registry, tmp_path: Path
) -> None:
    """含 node_modules/.bin 符号链接的 zip 应一键导入成功（IMP-001）。

    剥离前：node_modules/.bin/vite 是 symlink → audit_zip_members 会判 zip_symlink
    拒绝。剥离后该成员不落盘、不参与审计，导入通过。
    """
    zip_path = _make_zip_with_symlink_members(
        tmp_path / "nodeapp.zip",
        plain={
            "package.json": '{"dependencies":{"vite":"^5.0.0"}}',
            "package-lock.json": '{"lockfileVersion":3}',
            "src/index.ts": "export const x = 1;",
            "node_modules/react/index.js": "module.exports={};",
        },
        symlinks={"node_modules/.bin/vite": "../react/bin/vite.js"},
    )
    result = importer.import_zip(zip_path)

    # 导入成功，sanitized 摘要存在且记录了剥离
    assert result.sanitized is not None
    assert len(result.sanitized.stripped_names) > 0
    assert "node_modules" in result.sanitized.categories
    assert result.sanitized.stripped_symlink_count >= 1

    # 关键保留文件落盘
    current = workspace.app_current(result.instance_id)
    assert (current / "package.json").is_file()
    assert (current / "package-lock.json").is_file()
    assert (current / "src" / "index.ts").is_file()
    # node_modules 不落盘
    assert not (current / "node_modules").exists()

    # security 事件记录了剥离摘要
    events = registry.list_events(result.instance_id)
    security_events = [e for e in events if e["event_type"] == "security"]
    strip_events = [e for e in security_events if "剥离" in e["message"]]
    assert strip_events, [e["message"] for e in events]


def test_import_preserves_lockfile_after_strip(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """剥离冗余包后 lockfile 与源码必须保留（IMP-001）。"""
    zip_path = _make_zip(
        tmp_path / "nodeapp.zip",
        {
            "package.json": '{"dependencies":{}}',
            "package-lock.json": '{"lockfileVersion":3}',
            "pnpm-lock.yaml": "lockfileVersion: '6.0'",
            "node_modules/react/index.js": "x",
            "__pycache__/app.pyc": "x",
            ".DS_Store": "x",
            "src/app.ts": "export {}",
        },
    )
    result = importer.import_zip(zip_path)
    current = workspace.app_current(result.instance_id)
    assert (current / "package-lock.json").is_file()
    assert (current / "pnpm-lock.yaml").is_file()
    assert (current / "src" / "app.ts").is_file()
    assert (current / "package.json").is_file()
    assert not (current / "node_modules").exists()
    assert not (current / "__pycache__").exists()


def test_import_still_rejects_source_symlink_after_strip(
    importer: Importer, tmp_path: Path
) -> None:
    """剥离后业务源码目录的恶意 symlink 仍被拒绝（IMP-001 不削弱安全）。

    src/evil → /etc/passwd 不属于可剥离段，保留后由 audit_zip_members 拒绝。
    """
    zip_path = _make_zip_with_symlink_members(
        tmp_path / "evil.zip",
        plain={"index.html": "<html></html>"},
        symlinks={"src/evil": "/etc/passwd"},
    )
    with pytest.raises(ZipImportError, match="zip_symlink"):
        importer.import_zip(zip_path)


def test_import_strip_regression_zip_slip(importer: Importer, tmp_path: Path) -> None:
    """IMP-001 剥离后，zip slip 仍被拒绝（回归不退化）。"""
    zip_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("index.html", "<html></html>")
        info = zipfile.ZipInfo("../escape.txt")
        zf.writestr(info, "pwned")
    with pytest.raises(ZipImportError, match="zip_slip"):
        importer.import_zip(zip_path)


def test_import_strip_regression_absolute_path(importer: Importer, tmp_path: Path) -> None:
    """IMP-001 剥离后，绝对路径成员仍被拒绝（回归不退化）。"""
    zip_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("index.html", "<html></html>")
        zf.writestr("/etc/passwd", "stolen")
    with pytest.raises(ZipImportError, match="zip_absolute_path"):
        importer.import_zip(zip_path)


def test_import_clean_zip_has_empty_sanitized(
    importer: Importer, tmp_path: Path
) -> None:
    """无冗余成员的 zip，sanitized 非空但 stripped_names 为空。"""
    zip_path = _make_zip(
        tmp_path / "clean.zip",
        {"index.html": "<html></html>", "style.css": "body{}"},
    )
    result = importer.import_zip(zip_path)
    assert result.sanitized is not None
    assert result.sanitized.stripped_names == ()


# ---- IMP-006：路径别名导入 -------------------------------------------------


def _make_static_zip(zip_path: Path, body: str = "hi") -> Path:
    return _make_zip(
        zip_path,
        {"index.html": f"<html><body>{body}</body></html>", "style.css": "body{}"},
    )


def test_import_with_path_alias_writes_route_mode(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """--path-alias 写入 static.routeMode=name + routeHost；默认行为保持 port。"""
    zip_path = _make_static_zip(tmp_path / "demo.zip")
    result = importer.import_zip(zip_path, path_alias="voiceprint-app-demo")

    static = result.manifest.static
    assert static is not None
    assert static.routeMode == "name"
    assert static.routeHost == "voiceprint-app-demo"
    # registry 静态站点行也应记录
    row = importer.registry.get_static_site(result.instance_id)
    assert row["route_mode"] == "name"
    assert row["route_host"] == "voiceprint-app-demo"


def test_import_without_path_alias_keeps_port_mode(
    importer: Importer, tmp_path: Path
) -> None:
    """不传别名时 routeMode 仍为 port（默认行为不变）。"""
    zip_path = _make_static_zip(tmp_path / "demo.zip")
    result = importer.import_zip(zip_path)
    static = result.manifest.static
    assert static is not None
    assert static.routeMode == "port"
    assert static.routeHost is None


def test_import_path_alias_rejects_reserved(
    importer: Importer, tmp_path: Path
) -> None:
    zip_path = _make_static_zip(tmp_path / "demo.zip")
    with pytest.raises(PathError):
        importer.import_zip(zip_path, path_alias="api")


def test_import_path_alias_rejects_bad_format(
    importer: Importer, tmp_path: Path
) -> None:
    zip_path = _make_static_zip(tmp_path / "demo.zip")
    with pytest.raises(PathError):
        importer.import_zip(zip_path, path_alias="Bad_Alias!")


def test_import_path_alias_rejects_duplicate(
    importer: Importer, registry: Registry, tmp_path: Path
) -> None:
    """两个实例不能用同一个别名。"""
    z1 = _make_static_zip(tmp_path / "a.zip")
    importer.import_zip(z1, path_alias="shared-alias")

    z2 = _make_static_zip(tmp_path / "b.zip")
    with pytest.raises(PathError):
        importer.import_zip(z2, path_alias="shared-alias")


def test_import_path_alias_accepts_container(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """IMP-014：容器实例 import 时可预写 container.routeHost（首启时生成别名片段）。"""
    zip_path = _make_zip(
        tmp_path / "api.zip",
        {"requirements.txt": "fastapi\nuvicorn\n"},
    )
    result = importer.import_zip(zip_path, path_alias="api-alias")
    assert result.manifest.runtime == Runtime.DOCKER_COMPOSE
    container = result.manifest.container
    assert container is not None
    assert container.routeMode == "name"
    assert container.routeHost == "api-alias"
    row = importer.registry.get_container(result.instance_id)
    assert row is not None
    assert row["route_mode"] == "name"
    assert row["route_host"] == "api-alias"
    assert workspace.app_dir(result.instance_id).exists()


def test_import_path_alias_validates_before_write(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """别名校验在写盘前完成，非法别名不留半成品目录。"""
    zip_path = _make_static_zip(tmp_path / "demo.zip")
    with pytest.raises(PathError):
        importer.import_zip(zip_path, path_alias="health")  # 保留字
    # 校验在 ensure_app_dirs 之前 → 不应创建 apps/demo/
    assert not workspace.app_dir("demo").exists()


# ---- IMP-009：实例 zip 原地更新 --------------------------------------------


def _set_desired_running(workspace: Workspace, instance_id: str) -> None:
    """直接改盘上 manifest 的 desiredState=running（模拟已启动实例）。"""
    from local_webpage_access.models import DesiredState, InstanceManifest

    path = workspace.app_manifest_path(instance_id)
    m = InstanceManifest.load(path)
    m.desiredState = DesiredState.RUNNING
    m.save(path)


def test_import_conflict_error_mode(importer: Importer, tmp_path: Path) -> None:
    """on_conflict='error'：slug 冲突不再 silent 建 -2，而是报错并建议 --update。"""
    zip_path = _make_static_zip(tmp_path / "demo.zip")
    importer.import_zip(zip_path)  # 建 demo
    with pytest.raises(ZipImportError, match="--update"):
        importer.import_zip(zip_path, on_conflict="error")


def test_import_conflict_error_mode_is_atomic(
    importer: Importer, workspace: Workspace, tmp_path: Path, monkeypatch
) -> None:
    """BUG-313/BUG-370：检查后目录被并发认领时 error 模式不能静默创建 -2。"""
    zip_path = _make_static_zip(tmp_path / "demo.zip")
    original_mkdir = Path.mkdir
    raced = False
    # 故意让预检永远通过，强制冲突只由原子 mkdir claim 裁决。
    monkeypatch.setattr(importer, "_id_taken", lambda _slug: False)

    def racing_mkdir(path, *args, **kwargs):  # noqa: ANN001
        nonlocal raced
        if path == workspace.app_dir("demo") and kwargs.get("exist_ok") is False:
            if not raced:
                raced = True
                original_mkdir(path, parents=True, exist_ok=False)
                raise FileExistsError(path)
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)

    with pytest.raises(ZipImportError, match="已被并发创建.*--update"):
        importer.import_zip(zip_path, on_conflict="error")
    # 并发者已占用原始 slug；error 模式不得改名另建，也不得清掉对方目录。
    assert workspace.app_dir("demo").exists()
    assert not workspace.app_dir("demo-2").exists()


def test_import_conflict_rename_still_default(importer: Importer, tmp_path: Path) -> None:
    """默认 on_conflict='rename' 仍走 -2（daemon 依赖，不能回归）。"""
    zip_path = _make_static_zip(tmp_path / "demo.zip")
    importer.import_zip(zip_path)
    r2 = importer.import_zip(zip_path)  # 默认 rename
    assert r2.instance_id == "demo-2"


def test_update_replaces_content_and_hash(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """更新后 current/ 内容为新版，sourceZipHash 刷新，id/目录不变。"""
    v1 = _make_static_zip(tmp_path / "demo.zip", "v1")
    r1 = importer.import_zip(v1)
    iid = r1.instance_id
    old_hash = r1.zip_hash

    v2 = _make_static_zip(tmp_path / "demo-v2.zip", "v2")
    result = importer.update_zip(v2, iid, restart=False)

    assert result.skipped is False
    assert result.rebuilt is True
    assert result.instance_id == iid
    assert result.zip_hash != old_hash
    assert result.prev_hash == old_hash
    body = (workspace.app_current(iid) / "index.html").read_text()
    assert "v2" in body

    from local_webpage_access.models import InstanceManifest

    m = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert getattr(m, "sourceZipHash", None) == result.zip_hash


def test_update_same_hash_skips(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """相同 hash 再次更新 → skipped，不 rebuild。"""
    zip_path = _make_static_zip(tmp_path / "demo.zip", "v1")
    r1 = importer.import_zip(zip_path)
    iid = r1.instance_id

    result = importer.update_zip(zip_path, iid, restart=False)
    assert result.skipped is True
    assert result.rebuilt is False
    assert result.needs_restart is False


def test_update_kind_change_rejected(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """新 zip 形态从 static → container：拒绝，current/ 原封不动。"""
    static_zip = _make_static_zip(tmp_path / "demo.zip", "v1")
    r1 = importer.import_zip(static_zip)
    iid = r1.instance_id
    original_body = (workspace.app_current(iid) / "index.html").read_text()

    container_zip = _make_zip(
        tmp_path / "demo-api.zip",
        {"requirements.txt": "fastapi\nuvicorn\n", "main.py": "print('hi')"},
    )
    with pytest.raises(ZipImportError, match="形态发生变化"):
        importer.update_zip(container_zip, iid, restart=False)

    assert (workspace.app_current(iid) / "index.html").read_text() == original_body


def test_update_kind_change_forced(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """--force-kind-change 允许跨形态更新。"""
    static_zip = _make_static_zip(tmp_path / "demo.zip", "v1")
    r1 = importer.import_zip(static_zip)
    iid = r1.instance_id

    container_zip = _make_zip(
        tmp_path / "demo-api.zip",
        {"requirements.txt": "fastapi\n", "main.py": "x = 1"},
    )
    result = importer.update_zip(
        container_zip, iid, restart=False, force_kind_change=True
    )
    assert result.rebuilt is True
    assert result.kind_changed is True


def test_update_force_kind_change_stops_old_runtime(
    importer: Importer,
    workspace: Workspace,
    registry: Registry,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """BUG-124：跨形态换表前先停止旧 runtime，并恢复原 desiredState。"""
    static_zip = _make_static_zip(tmp_path / "demo.zip", "v1")
    r1 = importer.import_zip(static_zip)
    iid = r1.instance_id
    _set_desired_running(workspace, iid)
    old_manifest = InstanceManifest.load(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(old_manifest)

    stopped: list[str] = []

    def fake_stop(ws, config, reg, instance_id):  # noqa: ANN001
        stopped.append(instance_id)
        manifest = InstanceManifest.load(ws.app_manifest_path(instance_id))
        manifest.desiredState = DesiredState.STOPPED
        manifest.save(ws.app_manifest_path(instance_id))
        return manifest

    monkeypatch.setattr("local_webpage_access.hosting.stop_instance", fake_stop)
    container_zip = _make_zip(
        tmp_path / "demo-api.zip",
        {"requirements.txt": "fastapi\n", "main.py": "x = 1"},
    )

    result = importer.update_zip(
        container_zip, iid, restart=False, force_kind_change=True
    )

    assert stopped == [iid]
    assert result.kind_changed is True
    assert result.manifest.desiredState == DesiredState.RUNNING


def test_update_failure_rolls_back(
    importer: Importer, workspace: Workspace, tmp_path: Path, monkeypatch
) -> None:
    """更新过程中扫描抛错 → current/ 与 manifest hash 回滚到更新前。"""
    v1 = _make_static_zip(tmp_path / "demo.zip", "v1")
    r1 = importer.import_zip(v1)
    iid = r1.instance_id
    original_body = (workspace.app_current(iid) / "index.html").read_text()

    from local_webpage_access.models import InstanceManifest

    original_hash = getattr(
        InstanceManifest.load(workspace.app_manifest_path(iid)), "sourceZipHash", None
    )

    # 让 scanner.detect 仅在更新路径（暂存区 .new）触发异常
    from local_webpage_access import importer as importer_mod

    original_detect = importer_mod.Scanner.detect

    def boom_detect(self, path):
        if ".new" in str(path):
            raise RuntimeError("scan boom")
        return original_detect(self, path)

    monkeypatch.setattr(importer_mod.Scanner, "detect", boom_detect)

    v2 = _make_static_zip(tmp_path / "demo-v2.zip", "v2")
    with pytest.raises(ZipImportError, match="scan boom"):
        importer.update_zip(v2, iid, restart=False)

    # current/ 内容未变
    assert (workspace.app_current(iid) / "index.html").read_text() == original_body
    # manifest hash 未变
    post_hash = getattr(
        InstanceManifest.load(workspace.app_manifest_path(iid)), "sourceZipHash", None
    )
    assert post_hash == original_hash
    # 暂存区 / 备份目录已清理
    parent = workspace.app_current(iid).parent
    assert not (parent / "current.new").exists()
    assert not (parent / "current.old").exists()


def test_update_failure_after_current_swap_rolls_back(
    importer: Importer, workspace: Workspace, tmp_path: Path, monkeypatch
) -> None:
    """current/ 换入后若 manifest 写入失败，也必须回滚到旧 current/。"""
    v1 = _make_static_zip(tmp_path / "demo.zip", "v1")
    r1 = importer.import_zip(v1)
    iid = r1.instance_id
    original_body = (workspace.app_current(iid) / "index.html").read_text()

    from local_webpage_access import importer as importer_mod
    from local_webpage_access.models import InstanceManifest

    original_hash = getattr(
        InstanceManifest.load(workspace.app_manifest_path(iid)), "sourceZipHash", None
    )
    original_save = importer_mod.InstanceManifest.save

    def fail_update_manifest_save(self, path):
        if getattr(self, "sourceZipHash", None) != original_hash:
            raise OSError("manifest write boom")
        return original_save(self, path)

    monkeypatch.setattr(
        importer_mod.InstanceManifest, "save", fail_update_manifest_save
    )

    v2 = _make_static_zip(tmp_path / "demo-v2.zip", "v2")
    with pytest.raises(ZipImportError, match="manifest write boom"):
        importer.update_zip(v2, iid, restart=False)

    assert (workspace.app_current(iid) / "index.html").read_text() == original_body
    post_hash = getattr(
        InstanceManifest.load(workspace.app_manifest_path(iid)), "sourceZipHash", None
    )
    assert post_hash == original_hash
    assert workspace.app_original_zip(iid).read_bytes() == v1.read_bytes()


def test_update_preserves_data(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """keep_data=True（默认）：data/ 内文件在更新后仍在。"""
    v1 = _make_static_zip(tmp_path / "demo.zip", "v1")
    r1 = importer.import_zip(v1)
    iid = r1.instance_id

    data_dir = workspace.app_data(iid)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "app.db").write_bytes(b"SQLite-format-3\x00seed")
    (data_dir / "uploads").mkdir()
    (data_dir / "uploads" / "logo.png").write_bytes(b"\x89PNG")

    v2 = _make_static_zip(tmp_path / "demo-v2.zip", "v2")
    importer.update_zip(v2, iid, restart=False)

    assert (data_dir / "app.db").read_bytes().startswith(b"SQLite-format-3")
    assert (data_dir / "uploads" / "logo.png").read_bytes() == b"\x89PNG"


def test_update_no_keep_data_clears_data(
    importer: Importer, workspace: Workspace, registry: Registry, tmp_path: Path
) -> None:
    """keep_data=False：data/ 被清空，registry 资源统计同步归零。"""
    v1 = _make_static_zip(tmp_path / "demo.zip", "v1")
    r1 = importer.import_zip(v1)
    iid = r1.instance_id
    data_dir = workspace.app_data(iid)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "app.db").write_bytes(b"old-data")

    v2 = _make_static_zip(tmp_path / "demo-v2.zip", "v2")
    importer.update_zip(v2, iid, restart=False, keep_data=False)

    assert not (data_dir / "app.db").exists()
    resources = registry.get_resources(iid)
    assert resources is not None
    assert resources["data_size_bytes"] == 0


def test_update_keep_data_false_preserves_data_when_registry_sync_fails(
    importer: Importer, workspace: Workspace, registry: Registry, tmp_path: Path, monkeypatch
) -> None:
    """BUG-348：keep_data=False 时 data 清空须在 registry 同步成功之后；同步失败应保留 data。"""
    v1 = _make_static_zip(tmp_path / "demo.zip", "v1")
    r1 = importer.import_zip(v1)
    iid = r1.instance_id
    data_dir = workspace.app_data(iid)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "app.db").write_bytes(b"keep-me")

    original = registry.upsert_from_manifest

    def boom(manifest, **kwargs):
        raise RuntimeError("registry sync boom")

    monkeypatch.setattr(registry, "upsert_from_manifest", boom)

    v2 = _make_static_zip(tmp_path / "demo-v2.zip", "v2")
    with pytest.raises(ZipImportError, match="registry sync boom"):
        importer.update_zip(v2, iid, restart=False, keep_data=False)

    assert (data_dir / "app.db").read_bytes() == b"keep-me"
    # 恢复以便后续清理；此处仅断言回滚路径未提前清空 data
    monkeypatch.setattr(registry, "upsert_from_manifest", original)


def test_update_preserves_path_alias(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """IMP-006 路径别名在更新后保留（别名是用户选择，不从 zip 推导）。"""
    v1 = _make_static_zip(tmp_path / "demo.zip", "v1")
    r1 = importer.import_zip(v1, path_alias="voiceprint")
    iid = r1.instance_id
    assert r1.manifest.static.routeMode == "name"
    assert r1.manifest.static.routeHost == "voiceprint"

    v2 = _make_static_zip(tmp_path / "demo-v2.zip", "v2")
    result = importer.update_zip(v2, iid, restart=False)
    assert result.manifest.static is not None
    assert result.manifest.static.routeMode == "name"
    assert result.manifest.static.routeHost == "voiceprint"


def test_update_preserves_container_path_alias(
    importer: Importer, workspace: Workspace, registry: Registry, tmp_path: Path
) -> None:
    """BUG-385：容器实例路径别名在 import --update 后必须保留。

    别名经 alias set / IMP-014 写入 container.routeHost，不从 zip 推导；
    更新重建 manifest 时若未对称保留，管理页 routeUrl 变空，但 Caddy 片段仍残留。
    """
    v1 = _make_zip(
        tmp_path / "api.zip",
        {"requirements.txt": "fastapi\nuvicorn\n", "main.py": "app=1\n"},
    )
    r1 = importer.import_zip(v1)
    iid = r1.instance_id
    assert r1.manifest.runtime == Runtime.DOCKER_COMPOSE

    mpath = workspace.app_manifest_path(iid)
    manifest = InstanceManifest.load(mpath)
    assert manifest.container is not None
    manifest.container.routeMode = "name"
    manifest.container.routeHost = "prd-review"
    manifest.save(mpath)
    registry.upsert_from_manifest(manifest)

    v2 = _make_zip(
        tmp_path / "api-v2.zip",
        {"requirements.txt": "fastapi\nuvicorn\n", "main.py": "app=2\n"},
    )
    result = importer.update_zip(v2, iid, restart=False)
    assert result.manifest.container is not None
    assert result.manifest.container.routeMode == "name"
    assert result.manifest.container.routeHost == "prd-review"

    reloaded = InstanceManifest.load(mpath)
    assert reloaded.container is not None
    assert reloaded.container.routeMode == "name"
    assert reloaded.container.routeHost == "prd-review"

    row = registry.get_container(iid)
    assert row is not None
    assert row.get("route_host") == "prd-review"
    assert row.get("route_mode") == "name"


def test_update_preserves_hostport_registration(
    importer: Importer, workspace: Workspace, registry: Registry, tmp_path: Path
) -> None:
    """更新不触碰 registry 的端口登记（hostPort 复用靠 hosting，登记必须不动）。"""
    v1 = _make_static_zip(tmp_path / "demo.zip", "v1")
    r1 = importer.import_zip(v1)
    iid = r1.instance_id

    registry.upsert_static_site(iid, {"hostPort": 18001})
    assert registry.get_static_site(iid)["host_port"] == 18001

    v2 = _make_static_zip(tmp_path / "demo-v2.zip", "v2")
    importer.update_zip(v2, iid, restart=False)

    assert registry.get_static_site(iid)["host_port"] == 18001


def test_update_preserves_static_enabled_and_timestamps(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """BUG-321：--update 必须保留 static.enabled / lastStartedAt / lastHealthCheckAt。"""
    v1 = _make_static_zip(tmp_path / "demo.zip", "v1")
    r1 = importer.import_zip(v1)
    iid = r1.instance_id

    manifest = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert manifest.static is not None
    manifest.static.enabled = False
    manifest.lastStartedAt = "2026-01-02T03:04:05Z"
    manifest.lastHealthCheckAt = "2026-01-02T03:05:06Z"
    manifest.desiredState = DesiredState.STOPPED
    manifest.status = Status.STOPPED
    manifest.save(workspace.app_manifest_path(iid))

    v2 = _make_static_zip(tmp_path / "demo-v2.zip", "v2")
    result = importer.update_zip(v2, iid, restart=False)

    assert result.manifest.static is not None
    assert result.manifest.static.enabled is False
    assert result.manifest.lastStartedAt == "2026-01-02T03:04:05Z"
    assert result.manifest.lastHealthCheckAt == "2026-01-02T03:05:06Z"
    reloaded = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert reloaded.static is not None
    assert reloaded.static.enabled is False
    assert reloaded.lastStartedAt == "2026-01-02T03:04:05Z"
    assert reloaded.lastHealthCheckAt == "2026-01-02T03:05:06Z"


def test_update_rollback_restores_port_registry_rows(
    importer: Importer, workspace: Workspace, registry: Registry, tmp_path: Path, monkeypatch
) -> None:
    """BUG-323：换入后失败回滚须恢复 static_sites/containers 的 host_port。"""
    v1 = _make_static_zip(tmp_path / "demo.zip", "v1")
    r1 = importer.import_zip(v1)
    iid = r1.instance_id
    registry.upsert_static_site(iid, {"hostPort": 18077})
    assert registry.get_static_site(iid)["host_port"] == 18077

    from local_webpage_access import importer as importer_mod

    original_hash = getattr(
        InstanceManifest.load(workspace.app_manifest_path(iid)), "sourceZipHash", None
    )
    original_save = importer_mod.InstanceManifest.save

    def fail_after_swap(self, path):
        if getattr(self, "sourceZipHash", None) != original_hash:
            # 模拟换入后 registry 已被新 manifest 清掉 hostPort 的中间态
            importer.registry.upsert_static_site(iid, {"hostPort": None})
            raise OSError("manifest write boom")
        return original_save(self, path)

    monkeypatch.setattr(importer_mod.InstanceManifest, "save", fail_after_swap)

    v2 = _make_static_zip(tmp_path / "demo-v2.zip", "v2")
    with pytest.raises(ZipImportError, match="manifest write boom"):
        importer.update_zip(v2, iid, restart=False)

    row = registry.get_static_site(iid)
    assert row is not None
    assert row["host_port"] == 18077


def test_update_force_kind_change_preserves_hostport_across_static_to_container(
    importer: Importer, workspace: Workspace, registry: Registry, tmp_path: Path
) -> None:
    """强制跨形态更新时，也从旧形态登记里迁移 hostPort。"""
    v1 = _make_static_zip(tmp_path / "demo.zip", "v1")
    r1 = importer.import_zip(v1)
    iid = r1.instance_id
    registry.upsert_static_site(iid, {"hostPort": 18001})

    container_zip = _make_zip(
        tmp_path / "demo-api.zip",
        {"requirements.txt": "fastapi\n", "main.py": "x = 1"},
    )
    result = importer.update_zip(
        container_zip, iid, restart=False, force_kind_change=True
    )

    assert result.manifest.container is not None
    assert result.manifest.container.hostPort == 18001
    row = registry.get_container(iid)
    assert row is not None
    assert row["host_port"] == 18001
    assert registry.get_static_site(iid) is None


def test_update_needs_restart_when_running(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """原 desiredState=running + restart=True → needs_restart=True（不实际重启）。"""
    v1 = _make_static_zip(tmp_path / "demo.zip", "v1")
    r1 = importer.import_zip(v1)
    iid = r1.instance_id
    _set_desired_running(workspace, iid)

    v2 = _make_static_zip(tmp_path / "demo-v2.zip", "v2")
    result = importer.update_zip(v2, iid, restart=True)
    assert result.was_running is True
    assert result.needs_restart is True
    assert result.needs_rebuild is False

    # restart=False（--no-restart）：即使原 running 也不要求重启
    v3 = _make_static_zip(tmp_path / "demo-v3.zip", "v3")
    result2 = importer.update_zip(v3, iid, restart=False)
    assert result2.was_running is True
    assert result2.needs_restart is False
    assert result2.needs_rebuild is False


def test_update_container_needs_rebuild_not_restart(
    importer: Importer, workspace: Workspace, registry: Registry, tmp_path: Path
) -> None:
    """DEV-067：容器 running 更新 → needs_rebuild=True，且作废 containerId。"""
    from local_webpage_access.models import InstanceManifest

    v1 = _make_zip(
        tmp_path / "api.zip",
        {"requirements.txt": "fastapi\nuvicorn\n", "main.py": "app=1\n"},
    )
    r1 = importer.import_zip(v1)
    iid = r1.instance_id
    # 模拟已部署：落库 containerId，使旧逻辑会走轻量 start
    mpath = workspace.app_manifest_path(iid)
    m = InstanceManifest.load(mpath)
    assert m.container is not None
    m.container.containerId = "old-container-id"
    m.container.imageId = "old-image-id"
    m.save(mpath)
    registry.upsert_from_manifest(m)
    _set_desired_running(workspace, iid)

    v2 = _make_zip(
        tmp_path / "api-v2.zip",
        {"requirements.txt": "fastapi\nuvicorn\n", "main.py": "app=2\n"},
    )
    result = importer.update_zip(v2, iid, restart=True)
    assert result.was_running is True
    assert result.needs_rebuild is True
    assert result.needs_restart is False
    assert result.manifest.container is not None
    assert result.manifest.container.containerId is None
    assert result.manifest.container.imageId is None
    # registry 同步后也不应再有旧 container_id（避免 start 误判已部署）
    row = registry.get_container(iid)
    assert row is not None
    assert row.get("container_id") in (None, "")

    # --no-restart：仍作废部署标记，但不要求立即 rebuild
    v3 = _make_zip(
        tmp_path / "api-v3.zip",
        {"requirements.txt": "fastapi\nuvicorn\n", "main.py": "app=3\n"},
    )
    # 再假装部署过
    m = InstanceManifest.load(mpath)
    assert m.container is not None
    m.container.containerId = "again"
    m.save(mpath)
    registry.upsert_from_manifest(m)
    result2 = importer.update_zip(v3, iid, restart=False)
    assert result2.needs_rebuild is False
    assert result2.needs_restart is False
    assert result2.manifest.container is not None
    assert result2.manifest.container.containerId is None


def test_update_dry_run_container_reports_needs_rebuild(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """BUG-113：dry-run 对 running 容器应预告 needs_rebuild（非 restart）。"""
    v1 = _make_zip(
        tmp_path / "api.zip",
        {"requirements.txt": "fastapi\nuvicorn\n", "main.py": "app=1\n"},
    )
    r1 = importer.import_zip(v1)
    iid = r1.instance_id
    _set_desired_running(workspace, iid)

    v2 = _make_zip(
        tmp_path / "api-v2.zip",
        {"requirements.txt": "fastapi\nuvicorn\n", "main.py": "app=2\n"},
    )
    result = importer.update_zip(v2, iid, restart=True, dry_run=True)
    assert result.dry_run is True
    assert result.was_running is True
    assert result.needs_rebuild is True
    assert result.needs_restart is False


def test_cli_import_update_dry_run_says_rebuild_for_container(
    workspace: Workspace,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BUG-113：CLI --dry-run 对 running 容器输出将 rebuild，而非 restart。"""
    from typer.testing import CliRunner

    from local_webpage_access.cli import app
    from local_webpage_access.config import Config
    from local_webpage_access.importer import Importer
    from local_webpage_access.init_workspace import init_workspace

    init_workspace(workspace.root)
    # init 会新建 registry；复用同一路径重新打开
    registry.close()
    reg = Registry(workspace.db_path)
    reg.open()
    try:
        importer = Importer(workspace, Config(), reg)
        v1 = _make_zip(
            tmp_path / "api.zip",
            {"requirements.txt": "fastapi\nuvicorn\n", "main.py": "app=1\n"},
        )
        r1 = importer.import_zip(v1)
        iid = r1.instance_id
        _set_desired_running(workspace, iid)

        v2 = _make_zip(
            tmp_path / "api-v2.zip",
            {"requirements.txt": "fastapi\nuvicorn\n", "main.py": "app=2\n"},
        )
        monkeypatch.chdir(workspace.root)
        result = CliRunner().invoke(
            app,
            ["import", str(v2), "--update", iid, "--dry-run"],
        )
        assert result.exit_code == 0, result.output
        assert "将 rebuild" in result.output
        assert "将 restart" not in result.output
    finally:
        reg.close()


def test_update_dry_run_no_writes(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """--dry-run：不写 current/、不生成 .bak、manifest hash 不变。"""
    v1 = _make_static_zip(tmp_path / "demo.zip", "v1")
    r1 = importer.import_zip(v1)
    iid = r1.instance_id
    original_body = (workspace.app_current(iid) / "index.html").read_text()

    from local_webpage_access.models import InstanceManifest

    pre_hash = getattr(
        InstanceManifest.load(workspace.app_manifest_path(iid)), "sourceZipHash", None
    )

    v2 = _make_static_zip(tmp_path / "demo-v2.zip", "v2")
    result = importer.update_zip(v2, iid, restart=False, dry_run=True)
    assert result.dry_run is True
    assert result.rebuilt is False
    assert result.kind_changed is False

    assert (workspace.app_current(iid) / "index.html").read_text() == original_body
    post_hash = getattr(
        InstanceManifest.load(workspace.app_manifest_path(iid)), "sourceZipHash", None
    )
    assert post_hash == pre_hash
    assert not workspace.app_original_zip(iid).with_suffix(".zip.bak").exists()


def test_update_backs_up_original_zip(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """更新时备份 original.zip → original.zip.bak。"""
    v1 = _make_static_zip(tmp_path / "demo.zip", "v1")
    r1 = importer.import_zip(v1)
    iid = r1.instance_id
    bak = workspace.app_original_zip(iid).with_suffix(".zip.bak")
    assert not bak.exists()

    v2 = _make_static_zip(tmp_path / "demo-v2.zip", "v2")
    importer.update_zip(v2, iid, restart=False)
    assert bak.is_file()
    # .bak 内容是旧 zip（v1）的二进制
    assert bak.read_bytes() == v1.read_bytes()


def test_update_nonexistent_instance_errors(importer: Importer, tmp_path: Path) -> None:
    """更新不存在的实例 → 报错并建议去掉 --update。"""
    zip_path = _make_static_zip(tmp_path / "demo.zip", "v1")
    with pytest.raises(ZipImportError, match="不存在"):
        importer.update_zip(zip_path, "no-such-id", restart=False)


def test_update_event_recorded(
    importer: Importer, workspace: Workspace, registry: Registry, tmp_path: Path
) -> None:
    """更新成功后 registry 写入 update 事件。"""
    v1 = _make_static_zip(tmp_path / "demo.zip", "v1")
    r1 = importer.import_zip(v1)
    iid = r1.instance_id

    v2 = _make_static_zip(tmp_path / "demo-v2.zip", "v2")
    importer.update_zip(v2, iid, restart=False)

    events = registry.list_events(iid)
    update_events = [e for e in events if e.get("event_type") == "update"]
    assert update_events, "应至少记录一条 update 事件"
    msg = update_events[-1].get("message", "")
    assert "sha256" in msg or "已更新" in msg


def test_kind_changed_helper() -> None:
    """_kind_changed：pending 不算变化；kind/runtime 不同算变化。"""
    from local_webpage_access.importer import Importer
    from local_webpage_access.models import InstanceManifest, Kind, Runtime
    from local_webpage_access.scanner import DetectionResult

    m = InstanceManifest(
        id="x",
        name="x",
        version="1",
        kind=Kind.STATIC,
        runtime=Runtime.SHARED_STATIC,
        servingMode=ServingMode.SHARED_STATIC,
    )
    # pending（未识别）→ 不算变化
    pending_dr = DetectionResult(form="未知", pending=True, notes=["未识别"])
    assert Importer._kind_changed(m, pending_dr) is False

    # static → static：不变
    same_dr = DetectionResult(
        form="静态站点",
        kind=Kind.STATIC,
        runtime=Runtime.SHARED_STATIC,
        servingMode=ServingMode.SHARED_STATIC,
    )
    assert Importer._kind_changed(m, same_dr) is False

    # static → docker（python）：变化
    diff_dr = DetectionResult(
        form="后端容器",
        kind=Kind.PYTHON,
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
    )
    assert Importer._kind_changed(m, diff_dr) is True

    # BUG-180：容器实例 + pending（未识别）zip → 判为形态变化。
    # 否则 pending 草稿会把运行中容器改写成 static、删 containers 登记却不停容器，孤儿化。
    from local_webpage_access.models import ContainerConfig

    container_m = InstanceManifest(
        id="c",
        name="c",
        version="1",
        kind=Kind.PYTHON,
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        container=ContainerConfig(
            projectName="c",
            internalPort=5000,
            composePath="docker/compose.yaml",
            dockerfilePath="docker/Dockerfile",
        ),
    )
    assert Importer._kind_changed(container_m, pending_dr) is True


# ---- IMP-018：build_manifest 注入资源档位限制 ------------------------------


def test_build_manifest_injects_profile_limits(workspace: Workspace) -> None:
    """IMP-018：medium 档位 → container.resourceLimits = 1g/1.5。"""
    from local_webpage_access.importer import build_manifest_from_detection
    from local_webpage_access.models import EntryConfig, ResourceProfile
    from local_webpage_access.scanner import DetectionResult

    detection = DetectionResult(
        kind=Kind.PYTHON,
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.MEDIUM,
        internalPort=8000,
        entry=EntryConfig(install="pip install -r requirements.txt", start="uvicorn main:app"),
        confidence="high",
    )
    manifest = build_manifest_from_detection(
        instance_id="api",
        display_name="api",
        detection=detection,
        workspace=workspace,
    )
    assert manifest.container is not None
    assert manifest.container.resourceLimits.memory == "1g"
    assert manifest.container.resourceLimits.cpus == "1.5"


def test_build_manifest_small_profile_uses_small_limits(workspace: Workspace) -> None:
    """IMP-018：small 档位 → 256m/0.5（不再是恒定默认 512m）。"""
    from local_webpage_access.importer import build_manifest_from_detection
    from local_webpage_access.models import EntryConfig, ResourceProfile
    from local_webpage_access.scanner import DetectionResult

    detection = DetectionResult(
        kind=Kind.PYTHON,
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.SMALL,
        internalPort=8000,
        entry=EntryConfig(install="pip install -r requirements.txt", start="uvicorn main:app"),
        confidence="high",
    )
    manifest = build_manifest_from_detection(
        instance_id="api",
        display_name="api",
        detection=detection,
        workspace=workspace,
    )
    assert manifest.container.resourceLimits.memory == "256m"


# ---- IMP-015：导入检测 .env.example 登记事件 ------------------------------


def test_import_env_example_records_event(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """IMP-015：zip 含 .env.example → 导入后登记 env_example_detected 事件。"""
    zip_path = _make_zip(
        tmp_path / "demo.zip",
        {
            "requirements.txt": "fastapi\n",
            ".env.example": "API_KEY=changeme\n",
        },
    )
    result = importer.import_zip(zip_path)
    events = importer.registry.list_events(result.instance_id)
    types = [e.get("event_type") for e in events]
    assert "env_example_detected" in types


# ---- CHK-178：路径别名与 builtin 后端 / 重扫保留 --------------------------


@pytest.fixture()
def builtin_importer(
    workspace: Workspace, registry: Registry
) -> Importer:
    """强制 builtin 后端的导入器（模拟 Caddy 不可用降级）。"""
    return Importer(workspace, Config(staticGateway="builtin"), registry)


def test_import_path_alias_rejected_on_builtin_backend(
    builtin_importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """CHK-178/P2：builtin 后端无统一别名入口，导入期应拒绝 --path-alias。

    否则写出 routeMode=name/routeHost 但实际访问不到（CLI 还会展示假 URL）。
    与运行时 set_instance_path_alias 的 builtin 拦截（path_alias.py）对称。
    """
    zip_path = _make_static_zip(tmp_path / "demo.zip")
    with pytest.raises(RecognitionError, match="Caddy"):
        builtin_importer.import_zip(zip_path, path_alias="voiceprint")
    # 拒绝在写盘前 → 不应留下半成品实例目录
    assert not workspace.app_dir("demo").exists()


def test_import_path_alias_rejected_on_builtin_container(
    builtin_importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """CHK-178/P2：容器实例在 builtin 后端同样拒绝 --path-alias。"""
    zip_path = _make_zip(
        tmp_path / "api.zip",
        {"requirements.txt": "fastapi\nuvicorn\n"},
    )
    with pytest.raises(RecognitionError, match="Caddy"):
        builtin_importer.import_zip(zip_path, path_alias="api-alias")
    assert not workspace.app_dir("api").exists()


def test_rescan_preserves_static_path_alias(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """CHK-178/P2：``lwa scan`` 重扫静态实例不得清空已配置的路径别名。

    复现：import --path-alias → apply_detection_to_manifest 重建 manifest 时
    未透传 path_alias，routeMode/name + routeHost 被重置为 port/None。
    """
    from local_webpage_access.importer import apply_detection_to_manifest
    from local_webpage_access.scanner import Scanner

    zip_path = _make_static_zip(tmp_path / "demo.zip")
    r1 = importer.import_zip(zip_path, path_alias="voiceprint")
    iid = r1.instance_id
    assert r1.manifest.static.routeMode == "name"
    assert r1.manifest.static.routeHost == "voiceprint"

    # 模拟 lwa scan：重新扫描并应用
    scanner = Scanner()
    current_dir = workspace.app_current(iid)
    detection = scanner.detect(current_dir)
    manifest = InstanceManifest.load(workspace.app_manifest_path(iid))
    fresh = apply_detection_to_manifest(manifest, detection, workspace)
    assert fresh.static is not None
    assert fresh.static.routeMode == "name"
    assert fresh.static.routeHost == "voiceprint"


def test_rescan_preserves_container_path_alias(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """CHK-178/P2：``lwa scan`` 重扫容器实例同样保留路径别名（IMP-014）。"""
    from local_webpage_access.importer import apply_detection_to_manifest
    from local_webpage_access.scanner import Scanner

    zip_path = _make_zip(
        tmp_path / "api.zip",
        {"requirements.txt": "fastapi\nuvicorn\n", "main.py": "app=1\n"},
    )
    r1 = importer.import_zip(zip_path, path_alias="prd-review")
    iid = r1.instance_id
    assert r1.manifest.runtime == Runtime.DOCKER_COMPOSE
    assert r1.manifest.container.routeMode == "name"
    assert r1.manifest.container.routeHost == "prd-review"

    scanner = Scanner()
    current_dir = workspace.app_current(iid)
    detection = scanner.detect(current_dir)
    manifest = InstanceManifest.load(workspace.app_manifest_path(iid))
    fresh = apply_detection_to_manifest(manifest, detection, workspace)
    assert fresh.container is not None
    assert fresh.container.routeMode == "name"
    assert fresh.container.routeHost == "prd-review"


# ---- BUG-469：import --path-alias 不得绕过绝对 SPA 资源硬守卫 --------------


def test_import_path_alias_rejects_absolute_spa_assets(
    importer: Importer, workspace: Workspace, tmp_path: Path, monkeypatch
) -> None:
    """BUG-469：import --path-alias 须执行与 set_instance_path_alias 同等的硬守卫。

    入口 HTML 含 ``/assets/...`` 时不得写出 routeMode=name/routeHost，
    否则用户可绕过设别名门禁，首次启动仍生成不可用别名路由。
    """
    monkeypatch.setattr(
        "local_webpage_access.static_gateway.StaticGateway.detect_backend",
        lambda self: "caddy",
    )
    zip_path = _make_zip(
        tmp_path / "spa.zip",
        {
            "index.html": (
                "<!doctype html><html><head>"
                '<script type="module" src="/assets/app.js"></script>'
                "</head><body>spa</body></html>"
            ),
            "assets/app.js": "console.log(1);",
        },
    )
    with pytest.raises(RecognitionError, match="绝对路径资源|白屏|IMP-023"):
        importer.import_zip(zip_path, path_alias="broken-spa")
    assert not workspace.app_dir("spa").exists()


def test_import_path_alias_allows_relative_spa_assets(
    importer: Importer, workspace: Workspace, tmp_path: Path, monkeypatch
) -> None:
    """相对资源路径的 SPA 仍允许 import --path-alias。"""
    monkeypatch.setattr(
        "local_webpage_access.static_gateway.StaticGateway.detect_backend",
        lambda self: "caddy",
    )
    zip_path = _make_zip(
        tmp_path / "ok-spa.zip",
        {
            "index.html": (
                "<!doctype html><html><head>"
                '<script type="module" src="./assets/app.js"></script>'
                "</head><body>spa</body></html>"
            ),
            "assets/app.js": "console.log(1);",
        },
    )
    result = importer.import_zip(zip_path, path_alias="ok-spa")
    assert result.manifest.static is not None
    assert result.manifest.static.routeMode == "name"
    assert result.manifest.static.routeHost == "ok-spa"
