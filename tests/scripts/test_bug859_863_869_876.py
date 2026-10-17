"""BUG-859 / BUG-863 / BUG-869 / BUG-876 回归：更新链的「人工确认优先」语义。

真实应用评测（11 个开源项目双机部署/升级）暴露的更新链缺陷：

- BUG-863（GH #94）：folder 源 ``import --from-dir --update`` 重扫后，用户手工
  修改过的 entry.install/start 被重置回检测值（buildEnv/buildHooks/systemDeps
  都保留，只有 entry 被清零）。
- BUG-876（GH #107）：update 重扫无视现有 manifest 的人工形态适配——误判
  pending / 误检形态会把更新拒绝（shape-guard）或整体覆盖 manifest（
  --force-kind-change 时 container/network/hostPort 全被清空）。
- BUG-859（GH #90）：folder 源更新消息「指纹」单位不一致——有更新时输出打包
  zip 内容指纹，同内容跳过时输出 sourceSyncHash，同一目录两个值不同。
- BUG-869（GH #100）：pending 实例没有官方 CLI 通道确认形态（只能手改
  manifest + 直接改 sqlite registry，configure 因 registry 未同步拒绝
  systemDeps，lwa scan 又会覆盖手改）。
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest

from local_webpage_access.config import Config
from local_webpage_access.importer import Importer
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
from local_webpage_access.scanner import DetectionResult


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
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for member, content in files.items():
            zf.writestr(member, content)
    return zip_path


def _container_zip(path: Path, app_body: str) -> Path:
    return _make_zip(
        path,
        {"requirements.txt": "fastapi\nuvicorn\n", "main.py": app_body},
    )


def _static_zip(path: Path, body: str) -> Path:
    return _make_zip(path, {"index.html": f"<html>{body}</html>"})


class _FakeScanner:
    """按用例预定返回 DetectionResult，模拟误判/未识别的重扫。"""

    def __init__(self, result: DetectionResult) -> None:
        self._result = result

    def detect(self, project_dir: Path) -> DetectionResult:  # noqa: ARG002
        return self._result


def _seed_deployed_container(
    workspace: Workspace, registry: Registry, iid: str, *, port: int = 21001
) -> None:
    """把已导入的容器实例伪装成「已部署」：身份/端口落库，status=running。"""
    mpath = workspace.app_manifest_path(iid)
    m = InstanceManifest.load(mpath)
    assert m.container is not None
    m.container.containerId = "old-container-id"
    m.container.imageId = "old-image"
    m.container.hostPort = port
    m.desiredState = DesiredState.RUNNING
    m.save(mpath)
    registry.upsert_from_manifest(m)


# ---- BUG-863：entry 人工修改保留 -------------------------------------------


def test_bug863_folder_update_preserves_user_modified_entry(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """folder 源 --update 重扫后：用户改过的 entry.install/start 不得重置回检测值。"""
    src = tmp_path / "pyapp"
    src.mkdir()
    (src / "requirements.txt").write_text("fastapi\nuvicorn\n", encoding="utf-8")
    (src / "main.py").write_text("app = 1\n", encoding="utf-8")

    iid = importer.import_from_dir(src).instance_id
    mpath = workspace.app_manifest_path(iid)
    m = InstanceManifest.load(mpath)
    assert m.entry.install  # 检测值非空，后续判定才有意义
    m.entry.install = "pip install -r requirements-prod.txt"
    m.entry.start = "uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2"
    m.buildEnv = {"PIP_INDEX_URL": "https://mirror.example.com/simple"}
    m.save(mpath)

    # 源目录变更触发重扫更新（形态一致：python/docker-compose → 常规采纳路径）
    (src / "main.py").write_text("app = 2\n", encoding="utf-8")
    result = importer.update_from_dir(iid, restart=False)
    assert result.rebuilt is True

    reloaded = InstanceManifest.load(mpath)
    assert reloaded.entry.install == "pip install -r requirements-prod.txt"
    assert reloaded.entry.start == (
        "uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2"
    )
    # 同族用户配置不受影响（DEV-132 语义不变）
    assert reloaded.buildEnv == {"PIP_INDEX_URL": "https://mirror.example.com/simple"}


def test_bug863_scan_merge_preserves_user_modified_entry(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """apply_detection_to_manifest（lwa scan / import --update 共用的合并点）同样保留。"""
    from local_webpage_access.importer import apply_detection_to_manifest
    from local_webpage_access.scanner import Scanner

    iid = importer.import_zip(_container_zip(tmp_path / "api.zip", "app=1\n")).instance_id
    mpath = workspace.app_manifest_path(iid)
    m = InstanceManifest.load(mpath)
    m.entry.install = "pip install -r requirements-prod.txt"
    m.save(mpath)

    detection = Scanner().detect(workspace.app_current(iid))
    merged = apply_detection_to_manifest(m, detection, workspace)
    assert merged.entry.install == "pip install -r requirements-prod.txt"


def test_bug863_legacy_db_url_start_still_migrates(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """issue #15 旧式 export DATABASE_URL 的 start 不属于「用户适配」：迁移优先。"""
    iid = importer.import_zip(_container_zip(tmp_path / "api.zip", "app=1\n")).instance_id
    mpath = workspace.app_manifest_path(iid)
    m = InstanceManifest.load(mpath)
    m.entry.start = (
        "sh -c 'export DATABASE_URL=sqlite:////app/data/bookshelf.db "
        "&& exec uvicorn main:app --host 0.0.0.0 --port 8000'"
    )
    m.save(mpath)

    importer.update_zip(_container_zip(tmp_path / "api-v2.zip", "app=2\n"), iid, restart=False)

    reloaded = InstanceManifest.load(mpath)
    assert "DATABASE_URL" not in (reloaded.entry.start or "")
    assert "DATABASE_URL=sqlite:////app/data/bookshelf.db" in workspace.app_env_path(
        iid
    ).read_text(encoding="utf-8")


_NODE_SVC_SOURCE = (
    "const http = require('node:http');\n"
    "http.createServer((req, res) => res.end('ok')).listen(3000);\n"
)


def _node_stdlib_dir(src: Path, entry_name: str) -> None:
    """零依赖 node-stdlib-http 服务（弱信号识别，start=node <入口文件>）。"""
    src.mkdir(parents=True, exist_ok=True)
    (src / "package.json").write_text(
        '{"name":"demo","version":"1.0.0","dependencies":{}}', encoding="utf-8"
    )
    (src / entry_name).write_text(_NODE_SVC_SOURCE, encoding="utf-8")


def test_bug863_auto_generated_entry_follows_redetection(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """BUG-863 修订：现值 == 上次检测快照的纯自动生成值必须随新检测更新。

    评审反例：零依赖 Node 服务入口从 server.js 改名为 app.js 后，独立扫描
    正确得到 node app.js，但旧规则把自动生成的 node server.js 也当人工适配
    永久保留——指向已不存在文件，下一次启动必败。
    """
    src = tmp_path / "nodesvc"
    _node_stdlib_dir(src, "server.js")
    iid = importer.import_from_dir(src).instance_id
    mpath = workspace.app_manifest_path(iid)
    m = InstanceManifest.load(mpath)
    assert m.entry.start == "node server.js"
    assert m.entry.detected is not None
    assert m.entry.detected["start"] == "node server.js"

    # 源码入口改名（server.js → app.js），重扫检测值随之变化
    (src / "server.js").unlink()
    (src / "app.js").write_text(_NODE_SVC_SOURCE, encoding="utf-8")
    result = importer.update_from_dir(iid, restart=False)
    assert result.rebuilt is True

    reloaded = InstanceManifest.load(mpath)
    assert reloaded.entry.start == "node app.js"
    assert reloaded.entry.detected is not None
    assert reloaded.entry.detected["start"] == "node app.js"


def test_bug863_user_override_survives_redetection_change(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """BUG-863 修订：现值 != 快照的人工覆盖在检测值变化时保留，且快照照常刷新，
    后续重扫依旧能识别出这是人工覆盖（不会只保留一次就被新检测值吞掉）。"""
    from local_webpage_access.importer import apply_detection_to_manifest
    from local_webpage_access.scanner import Scanner

    src = tmp_path / "nodesvc"
    _node_stdlib_dir(src, "server.js")
    iid = importer.import_from_dir(src).instance_id
    current = workspace.app_current(iid)
    mpath = workspace.app_manifest_path(iid)
    m = InstanceManifest.load(mpath)
    m.entry.start = "node custom.js"  # 用户手工覆盖
    m.save(mpath)

    (current / "server.js").unlink()
    (current / "app.js").write_text(_NODE_SVC_SOURCE, encoding="utf-8")
    detection2 = Scanner().detect(current)
    assert detection2.entry.start == "node app.js"
    merged = apply_detection_to_manifest(
        InstanceManifest.load(mpath), detection2, workspace
    )
    assert merged.entry.start == "node custom.js"
    assert merged.entry.detected is not None
    assert merged.entry.detected["start"] == "node app.js"

    # 检测值再次变化（新增优先级更高的 index.js）：覆盖仍然保留
    (current / "index.js").write_text(_NODE_SVC_SOURCE, encoding="utf-8")
    detection3 = Scanner().detect(current)
    assert detection3.entry.start == "node index.js"
    merged2 = apply_detection_to_manifest(merged, detection3, workspace)
    assert merged2.entry.start == "node custom.js"
    assert merged2.entry.detected is not None
    assert merged2.entry.detected["start"] == "node index.js"


def test_bug863_manifest_without_snapshot_preserves_conservatively(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """BUG-863 修订：存量 manifest 无 detected 快照时维持保守保留（已适配实例
    不退化）；本次合并写入快照后，后续重扫即可区分自动生成值。"""
    from local_webpage_access.importer import apply_detection_to_manifest
    from local_webpage_access.scanner import Scanner

    iid = importer.import_zip(_container_zip(tmp_path / "api.zip", "app=1\n")).instance_id
    mpath = workspace.app_manifest_path(iid)
    m = InstanceManifest.load(mpath)
    m.entry.install = "pip install -r requirements-prod.txt"
    m.entry.detected = None  # 模拟快照机制上线前的存量 manifest
    m.save(mpath)

    detection = Scanner().detect(workspace.app_current(iid))
    merged = apply_detection_to_manifest(
        InstanceManifest.load(mpath), detection, workspace
    )
    assert merged.entry.install == "pip install -r requirements-prod.txt"
    assert merged.entry.detected is not None
    assert merged.entry.detected["install"] == "pip install -r requirements.txt"


# ---- BUG-876：已确认形态不被重扫拒绝/覆盖 ----------------------------------


def test_bug876_pending_rescan_keeps_confirmed_container_shape(
    importer: Importer,
    workspace: Workspace,
    registry: Registry,
    tmp_path: Path,
) -> None:
    """实例 A：人工适配为容器、已部署；新源码重扫误判 pending（runtime=None）。

    旧行为：shape-guard 拒绝（BUG-180 口径 pending=跨形态）。新行为：不拒绝、
    不 pending 化、不清 container/network——保留既有形态，内容照常更新。
    """
    iid = importer.import_zip(_container_zip(tmp_path / "api.zip", "app=1\n")).instance_id
    _seed_deployed_container(workspace, registry, iid)

    fake = Importer(
        workspace,
        Config(),
        registry,
        scanner=_FakeScanner(
            DetectionResult(pending=True, confidence="low", notes=["无法识别项目类型"])
        ),
    )
    result = fake.update_zip(
        _container_zip(tmp_path / "api-v2.zip", "app=2\n"), iid, restart=False
    )

    assert result.rebuilt is True
    assert result.shape_disagreement is True

    reloaded = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert reloaded.kind == Kind.PYTHON
    assert reloaded.runtime == Runtime.DOCKER_COMPOSE
    assert reloaded.status != Status.PENDING  # 不得被误判 pending 打回
    assert reloaded.desiredState == DesiredState.RUNNING
    assert reloaded.container is not None
    assert reloaded.container.containerId == "old-container-id"
    assert reloaded.container.hostPort == 21001
    row = registry.get_container(iid)
    assert row is not None and row.get("host_port") == 21001
    assert (workspace.app_current(iid) / "main.py").read_text() == "app=2\n"
    # BUG-895：空检测（pending）的 entry/stack 不得冲掉已确认实例的检测派生
    # 字段——fresh.entry 全 None 时照搬会把 install/start 清空、下次启动必败。
    assert reloaded.entry.install == "pip install -r requirements.txt"
    assert reloaded.entry.start == "uvicorn main:app --host 0.0.0.0 --port 8000"
    assert reloaded.entry.detected is not None
    assert reloaded.entry.detected.get("start") == "uvicorn main:app --host 0.0.0.0 --port 8000"
    assert reloaded.stack == ["fastapi", "uvicorn"]


def test_bug876_misdetected_rescan_keeps_confirmed_static_shape(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """实例 B：人工适配为静态；重扫误检 NODE/DOCKER_COMPOSE——不得拒绝/覆盖。"""
    iid = importer.import_zip(_static_zip(tmp_path / "demo.zip", "v1")).instance_id
    assert InstanceManifest.load(workspace.app_manifest_path(iid)).status == Status.STOPPED

    fake = Importer(
        workspace,
        Config(),
        _registry_of(importer),  # 复用同一 registry 连接（SQLite 单写者）
        scanner=_FakeScanner(
            DetectionResult(
                kind=Kind.NODE,
                runtime=Runtime.DOCKER_COMPOSE,
                servingMode=ServingMode.CONTAINER,
                form="backend-container",
                confidence="medium",
            )
        ),
    )
    result = fake.update_zip(_static_zip(tmp_path / "demo-v2.zip", "v2"), iid, restart=False)

    assert result.shape_disagreement is True
    reloaded = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert reloaded.kind == Kind.STATIC
    assert reloaded.runtime == Runtime.SHARED_STATIC
    assert reloaded.status == Status.STOPPED
    assert "v2" in (workspace.app_current(iid) / "index.html").read_text()


def _registry_of(imp: Importer) -> Registry:
    return imp.registry


def test_bug876_dry_run_reports_kept_shape(
    importer: Importer, workspace: Workspace, registry: Registry, tmp_path: Path
) -> None:
    """--dry-run 与落盘路径同判定：已确认形态遇不一致重扫 → 不算「将变化」。"""
    iid = importer.import_zip(_container_zip(tmp_path / "api.zip", "app=1\n")).instance_id
    _seed_deployed_container(workspace, registry, iid)

    fake = Importer(
        workspace,
        Config(),
        registry,
        scanner=_FakeScanner(DetectionResult(pending=True, confidence="low")),
    )
    result = fake.update_zip(
        _container_zip(tmp_path / "api-v2.zip", "app=2\n"),
        iid,
        restart=False,
        dry_run=True,
    )
    assert result.dry_run is True
    assert result.kind_changed is False
    assert result.shape_disagreement is True


def test_bug876_force_kind_change_adopts_shape_and_preserves_hostport(
    importer: Importer,
    workspace: Workspace,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--force-kind-change 真正跨形态时：hostPort 登记必须保留（不得清空 container/network）。"""
    iid = importer.import_zip(_container_zip(tmp_path / "api.zip", "app=1\n")).instance_id
    _seed_deployed_container(workspace, registry, iid)
    monkeypatch.setattr(
        "local_webpage_access.hosting.stop_instance",
        lambda *a, **k: InstanceManifest.load(workspace.app_manifest_path(iid)),
    )

    result = importer.update_zip(
        _static_zip(tmp_path / "demo.zip", "v1"),
        iid,
        restart=False,
        force_kind_change=True,
    )

    assert result.rebuilt is True
    assert result.kind_changed is True
    assert result.shape_disagreement is False  # 显式迁移：采纳重扫形态

    reloaded = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert reloaded.kind == Kind.STATIC
    assert reloaded.runtime == Runtime.SHARED_STATIC
    assert reloaded.static is not None
    assert reloaded.static.hostPort == 21001  # 端口登记随形态迁移保留
    static_row = registry.get_static_site(iid)
    assert static_row is not None and static_row.get("host_port") == 21001
    assert registry.get_container(iid) is None  # 旧形态子表已换表删除


def test_bug876_force_kind_change_with_pending_detection_keeps_shape(
    importer: Importer,
    workspace: Workspace,
    registry: Registry,
    tmp_path: Path,
) -> None:
    """force 撞上 pending 检测时无形态可采纳：仍保留既有形态（旧行为会清空成 static 草稿 + pending）。"""
    iid = importer.import_zip(_container_zip(tmp_path / "api.zip", "app=1\n")).instance_id
    _seed_deployed_container(workspace, registry, iid)

    fake = Importer(
        workspace,
        Config(),
        registry,
        scanner=_FakeScanner(DetectionResult(pending=True, confidence="low")),
    )
    result = fake.update_zip(
        _container_zip(tmp_path / "api-v2.zip", "app=2\n"),
        iid,
        restart=False,
        force_kind_change=True,
    )

    assert result.shape_disagreement is True
    reloaded = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert reloaded.runtime == Runtime.DOCKER_COMPOSE
    assert reloaded.status != Status.PENDING
    assert reloaded.desiredState == DesiredState.RUNNING
    assert reloaded.container is not None and reloaded.container.hostPort == 21001


def test_bug901_force_kind_change_does_not_inherit_old_shape_entry(
    importer: Importer,
    workspace: Workspace,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BUG-901：跨形态迁移不得继承旧形态入口命令。

    存量 Node 容器（无 entry.detected 快照）--force-kind-change 更新为纯静态站
    时，旧的 npm ci / npm run build 残留会让 hosting._infer_form 按 entry.build
    分派到前端构建流程——新源码没有 package.json，部署必败。
    """
    iid = importer.import_zip(_container_zip(tmp_path / "api.zip", "app=1\n")).instance_id
    _seed_deployed_container(workspace, registry, iid)
    # 伪装成快照机制之前的存量 Node 容器（entry.detected=None，保守保留路径）
    mpath = workspace.app_manifest_path(iid)
    legacy = InstanceManifest.load(mpath)
    legacy.kind = Kind.NODE
    legacy.stack = ["node"]
    legacy.entry.install = "npm ci"
    legacy.entry.build = "npm run build"
    legacy.entry.start = "node server.js"
    legacy.entry.detected = None
    legacy.save(mpath)
    registry.upsert_from_manifest(legacy)
    monkeypatch.setattr(
        "local_webpage_access.hosting.stop_instance",
        lambda *a, **k: InstanceManifest.load(workspace.app_manifest_path(iid)),
    )

    result = importer.update_zip(
        _static_zip(tmp_path / "demo.zip", "v1"),
        iid,
        restart=False,
        force_kind_change=True,
    )

    assert result.kind_changed is True
    reloaded = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert reloaded.kind == Kind.STATIC
    assert reloaded.runtime == Runtime.SHARED_STATIC
    # 目标形态的检测入口：不残留旧 Node 形态的 install/build/start
    assert reloaded.entry.install is None
    assert reloaded.entry.build is None
    assert reloaded.entry.start is None or "server.js" not in reloaded.entry.start


# ---- BUG-859：folder 源更新消息指纹单位统一 --------------------------------


def test_bug859_updated_and_skipped_paths_share_fingerprint_unit(
    importer: Importer, workspace: Workspace, tmp_path: Path
) -> None:
    """有更新与跳过两条路径的 UpdateResult 指纹单位一致（以落库 sourceSyncHash 为准）。"""
    from local_webpage_access.folder_source import compute_source_hash

    src = tmp_path / "site"
    src.mkdir()
    (src / "index.html").write_text("v1", encoding="utf-8")
    iid = importer.import_from_dir(src).instance_id
    old_sync_hash = InstanceManifest.load(workspace.app_manifest_path(iid)).sourceSyncHash
    assert old_sync_hash

    (src / "index.html").write_text("v2", encoding="utf-8")
    updated = importer.update_from_dir(iid, restart=False)
    assert updated.rebuilt is True
    assert updated.zip_hash == compute_source_hash(src)
    assert updated.prev_hash == old_sync_hash

    skipped = importer.update_from_dir(iid, restart=False)
    assert skipped.skipped is True
    assert skipped.zip_hash == updated.zip_hash  # 同一目录：两路径指纹一致
    assert skipped.prev_hash == updated.zip_hash


def test_bug859_cli_messages_share_fingerprint_unit(monkeypatch, tmp_path: Path) -> None:
    """CLI 输出：「已更新」消息的打包指纹与「已跳过」消息的指纹必须相等。"""
    from typer.testing import CliRunner

    from local_webpage_access.cli import app
    from local_webpage_access.init_workspace import init_workspace

    root = tmp_path / "ws"
    init_workspace(root)
    src = tmp_path / "site"
    src.mkdir()
    (src / "index.html").write_text("v1", encoding="utf-8")
    monkeypatch.chdir(root)
    monkeypatch.setattr(
        "local_webpage_access.platform_support.require_supported_platform",
        lambda **kw: None,
    )
    runner = CliRunner()

    r = runner.invoke(app, ["import", "--from-dir", str(src), "--yes"])
    assert r.exit_code == 0, r.output

    (src / "index.html").write_text("v2", encoding="utf-8")
    r1 = runner.invoke(app, ["import", "--from-dir", str(src), "--update", "site", "--yes"])
    assert r1.exit_code == 0, r1.output
    m1 = re.search(r"指纹：([0-9a-f]{12}) -> ([0-9a-f]{12})", r1.output)
    assert m1, r1.output

    r2 = runner.invoke(app, ["import", "--from-dir", str(src), "--update", "site", "--yes"])
    assert r2.exit_code == 0, r2.output
    m2 = re.search(r"内容未变化（指纹 ([0-9a-f]{12})）", r2.output)
    assert m2, r2.output
    assert m2.group(1) == m1.group(2)  # 跳过消息的新指纹 == 更新消息的新指纹


# ---- BUG-869：pending 实例的官方形态确认通道 -------------------------------


def _cli_workspace(monkeypatch, tmp_path: Path):
    from typer.testing import CliRunner

    from local_webpage_access.init_workspace import init_workspace

    root = tmp_path / "ws"
    init_workspace(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(
        "local_webpage_access.platform_support.require_supported_platform",
        lambda **kw: None,
    )
    return root, CliRunner()


def _import_pending_zip(tmp_path: Path) -> Path:
    return _make_zip(tmp_path / "mystery.zip", {"notes.txt": "hello"})


def test_bug869_configure_confirms_pending_shape_syncs_manifest_and_registry(
    monkeypatch, tmp_path: Path
) -> None:
    """configure --kind/--runtime：manifest 与 registry 一致，systemDeps 通道打开。"""
    from local_webpage_access.cli import app

    root, runner = _cli_workspace(monkeypatch, tmp_path)
    zip_path = _import_pending_zip(tmp_path)
    r = runner.invoke(app, ["import", str(zip_path), "--yes"])
    assert r.exit_code == 0, r.output
    iid = "mystery"
    assert "已导入实例：mystery" in r.output

    r = runner.invoke(app, ["configure", iid, "--kind", "node", "--runtime", "docker-compose"])
    assert r.exit_code == 0, r.output
    assert "已确认实例 mystery 形态：node/docker-compose" in r.output
    assert "建议下一步" in r.output

    ws = Workspace(root)
    manifest = InstanceManifest.load(ws.app_manifest_path(iid))
    assert manifest.kind == Kind.NODE
    assert manifest.runtime == Runtime.DOCKER_COMPOSE
    assert manifest.servingMode == ServingMode.CONTAINER
    assert manifest.status == Status.STOPPED  # pending → stopped（形态已确认）
    assert manifest.container is not None

    reg = Registry(ws.db_path)
    reg.open()
    try:
        row = reg.get_instance(iid)
        assert row is not None
        assert row["kind"] == "node"
        assert row["runtime"] == "docker-compose"
        assert row["status"] == "stopped"
        assert reg.get_container(iid) is not None
    finally:
        reg.close()

    # 此前因 registry 未同步会拒绝 systemDeps：确认后通道应打开
    r = runner.invoke(app, ["configure", iid, "--system-deps", "ffmpeg"])
    assert r.exit_code == 0, r.output
    assert "ffmpeg" in r.output
    assert InstanceManifest.load(ws.app_manifest_path(iid)).systemDeps == ["ffmpeg"]


def test_bug869_scan_does_not_override_confirmed_shape(monkeypatch, tmp_path: Path) -> None:
    """configure 确认过形态的实例：lwa scan 重扫（仍 pending）不得覆盖 kind/runtime。"""
    from local_webpage_access.cli import app

    root, runner = _cli_workspace(monkeypatch, tmp_path)
    zip_path = _import_pending_zip(tmp_path)
    r = runner.invoke(app, ["import", str(zip_path), "--yes"])
    assert r.exit_code == 0, r.output
    iid = "mystery"

    r = runner.invoke(app, ["configure", iid, "--kind", "node", "--runtime", "docker-compose"])
    assert r.exit_code == 0, r.output

    r = runner.invoke(app, ["scan", iid])
    assert r.exit_code == 0, r.output
    assert "保留既有形态" in r.output

    manifest = InstanceManifest.load(Workspace(root).app_manifest_path(iid))
    assert manifest.kind == Kind.NODE
    assert manifest.runtime == Runtime.DOCKER_COMPOSE
    assert manifest.status == Status.STOPPED


def test_bug869_configure_shape_validation(monkeypatch, tmp_path: Path) -> None:
    """取值闭集、成对使用、仅 pending 实例——非法输入一律 exit 2。"""
    from local_webpage_access.cli import app

    _, runner = _cli_workspace(monkeypatch, tmp_path)
    zip_path = _import_pending_zip(tmp_path)
    r = runner.invoke(app, ["import", str(zip_path), "--yes"])
    assert r.exit_code == 0, r.output
    iid = "mystery"

    # 非法 kind
    r = runner.invoke(app, ["configure", iid, "--kind", "java", "--runtime", "docker-compose"])
    assert r.exit_code == 2
    assert "--kind 取值须为" in r.output

    # 非法 runtime
    r = runner.invoke(app, ["configure", iid, "--kind", "node", "--runtime", "podman"])
    assert r.exit_code == 2
    assert "--runtime 取值须为" in r.output

    # 不成对
    r = runner.invoke(app, ["configure", iid, "--kind", "node"])
    assert r.exit_code == 2
    assert "须同时使用" in r.output

    # 与其他配置项组合
    r = runner.invoke(
        app, ["configure", iid, "--kind", "node", "--runtime", "docker-compose", "--system-deps", "ffmpeg"]
    )
    assert r.exit_code == 2
    assert "单独使用" in r.output

    # 非 pending 实例拒绝
    static_zip = _static_zip(tmp_path / "demo.zip", "v1")
    r = runner.invoke(app, ["import", str(static_zip), "--yes"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["configure", "demo", "--kind", "python", "--runtime", "docker-compose"])
    assert r.exit_code == 2
    assert "仅用于确认 pending 实例" in r.output


def test_bug891_confirm_container_shape_sets_nonroot_and_limits(
    monkeypatch, tmp_path: Path
) -> None:
    """BUG-891：pending 确认容器形态时补齐 runAsNonRoot=True 与
    resourceLimits（此前 None 会被当作 legacy root 实例告警，配额恒默认）。"""
    from local_webpage_access.cli import app

    root, runner = _cli_workspace(monkeypatch, tmp_path)
    zip_path = _import_pending_zip(tmp_path)
    r = runner.invoke(app, ["import", str(zip_path), "--yes"])
    assert r.exit_code == 0, r.output

    r = runner.invoke(app, ["configure", "mystery", "--kind", "node", "--runtime", "docker-compose"])
    assert r.exit_code == 0, r.output

    manifest = InstanceManifest.load(Workspace(root).app_manifest_path("mystery"))
    assert manifest.container is not None
    assert manifest.container.runAsNonRoot is True
    # pending 草稿 resourceProfile=tiny → 128m/0.25
    assert manifest.container.resourceLimits is not None
    assert manifest.container.resourceLimits.memory == "128m"
    assert manifest.container.resourceLimits.cpus == "0.25"


def test_bug891_confirm_shape_preserves_import_time_path_alias(
    monkeypatch, tmp_path: Path
) -> None:
    """BUG-891：pending 实例携带的路径别名（记于 static.routeMode/routeHost）
    在确认形态后必须保留——容器目标不再把别名抹成 routeMode=port。"""
    from local_webpage_access.cli import app

    root, runner = _cli_workspace(monkeypatch, tmp_path)
    zip_path = _import_pending_zip(tmp_path)
    r = runner.invoke(app, ["import", str(zip_path), "--yes"])
    assert r.exit_code == 0, r.output
    ws = Workspace(root)
    # pending 实例携带别名（如更新重扫从已部署别名实例退回 pending，或手工
    # 配置）：播种 static.routeMode/routeHost 模拟该状态
    mpath = ws.app_manifest_path("mystery")
    before = InstanceManifest.load(mpath)
    assert before.static is not None
    before.static.routeMode = "name"
    before.static.routeHost = "magic"
    before.desiredAlias = "magic"
    before.save(mpath)

    # 容器目标：别名进入 container.routeMode/routeHost
    r = runner.invoke(app, ["configure", "mystery", "--kind", "node", "--runtime", "docker-compose"])
    assert r.exit_code == 0, r.output
    manifest = InstanceManifest.load(ws.app_manifest_path("mystery"))
    assert manifest.container is not None
    assert manifest.container.routeMode == "name"
    assert manifest.container.routeHost == "magic"
    assert manifest.static is None


def test_bug891_confirm_static_shape_preserves_path_alias(
    monkeypatch, tmp_path: Path
) -> None:
    """BUG-891：静态目标同样保留别名（重写 static 配置不清空 routeHost）。"""
    from local_webpage_access.cli import app

    root, runner = _cli_workspace(monkeypatch, tmp_path)
    zip_path = _import_pending_zip(tmp_path)
    r = runner.invoke(app, ["import", str(zip_path), "--yes"])
    assert r.exit_code == 0, r.output
    ws = Workspace(root)
    mpath = ws.app_manifest_path("mystery")
    before = InstanceManifest.load(mpath)
    assert before.static is not None
    before.static.routeMode = "name"
    before.static.routeHost = "magic"
    before.save(mpath)

    r = runner.invoke(app, ["configure", "mystery", "--kind", "static", "--runtime", "shared-static"])
    assert r.exit_code == 0, r.output
    manifest = InstanceManifest.load(Workspace(root).app_manifest_path("mystery"))
    assert manifest.static is not None
    assert manifest.static.routeMode == "name"
    assert manifest.static.routeHost == "magic"
    assert manifest.container is None
