"""issue #8：rebuild 源码陈旧检测 / ``--sync`` / doctor 源码新鲜度检查。

覆盖：
* folder 源：漂移检出 / 无漂移不报警 / 源目录丢失；
* git 源：远端 OID 前进报警（monkeypatch ls-remote 探测）、离线不阻断；
* rebuild_instance：警告经 ``out`` 透出 + 写 ``source_stale`` 事件，不阻断；
* CLI ``lwa rebuild --sync``：folder/git 走更新管线、zip 拒绝；
* doctor ``check_source_freshness``：WARN / SKIP / OK 矩阵。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from local_webpage_access.doctor import (
    STATUS_OK,
    STATUS_SKIP,
    STATUS_WARN,
    check_source_freshness,
)
from local_webpage_access.folder_source import compute_source_hash
from local_webpage_access.lifecycle import check_source_staleness, rebuild_instance
from local_webpage_access.models import (
    ContainerConfig,
    DesiredState,
    InstanceManifest,
    Kind,
    ResourceProfile,
    Runtime,
    ServingMode,
    Status,
)
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry

_STORED_OID = "a" * 40
_ADVANCED_OID = "b" * 40


def _make_manifest(iid: str = "api", **overrides) -> InstanceManifest:
    defaults: dict = dict(
        id=iid,
        name=iid,
        version="1",
        kind=Kind.PYTHON,
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.SMALL,
        container=ContainerConfig(
            projectName=f"lwa-{iid}",
            internalPort=8000,
            composePath="docker/compose.yaml",
            dockerfilePath="docker/Dockerfile",
        ),
        desiredState=DesiredState.STOPPED,
        status=Status.STOPPED,
    )
    defaults.update(overrides)
    return InstanceManifest(**defaults)


def _seed_instance(
    workspace: Workspace,
    registry: Registry,
    iid: str = "api",
    **manifest_overrides,
) -> InstanceManifest:
    """落盘 manifest + current/ + registry 行（rebuild 路径需要）。"""
    workspace.ensure_app_dirs(iid)
    current = workspace.app_current(iid)
    (current / "main.py").write_text("app=None")
    manifest_overrides.setdefault("appPath", str(current))
    manifest = _make_manifest(iid, **manifest_overrides)
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)
    return manifest


def _make_source_dir(tmp_path: Path, *, content: str = "v1") -> Path:
    src = tmp_path / "src-app"
    src.mkdir()
    (src / "main.py").write_text(content)
    return src


@pytest.fixture()
def stub_hosting(monkeypatch):
    """rebuild 的实际构建由 host_container/host_instance 完成；此处打桩隔离。"""

    def _host(ws, config, reg, iid, **kw):
        manifest = InstanceManifest.load(ws.app_manifest_path(iid))
        manifest.status = Status.RUNNING
        manifest.save(ws.app_manifest_path(iid))
        return manifest

    monkeypatch.setattr("local_webpage_access.hosting.host_container", _host)
    monkeypatch.setattr("local_webpage_access.hosting.host_instance", _host)


# ---- check_source_staleness：folder 源 --------------------------------------


def test_folder_source_drift_detected(workspace, config, tmp_path) -> None:
    """folder 源指纹 != 上次同步指纹 → 警告建议更新 / --sync。"""
    src = _make_source_dir(tmp_path)
    manifest = _make_manifest(
        sourceKind="folder",
        sourceDirPath=str(src),
        sourceSyncHash=compute_source_hash(src),
    )
    (src / "main.py").write_text("v2")  # 上次同步后源码已变更
    warning = check_source_staleness(workspace, config, manifest)
    assert warning is not None
    assert "不一致" in warning
    assert "rebuild --sync" in warning


def test_folder_source_no_drift_no_warning(workspace, config, tmp_path) -> None:
    """指纹一致 → 不报警。"""
    src = _make_source_dir(tmp_path)
    manifest = _make_manifest(
        sourceKind="folder",
        sourceDirPath=str(src),
        sourceSyncHash=compute_source_hash(src),
    )
    assert check_source_staleness(workspace, config, manifest) is None


def test_folder_source_missing_dir_warns(workspace, config, tmp_path) -> None:
    """关联源码目录已丢失 → 警告（不假装正常）。"""
    manifest = _make_manifest(
        sourceKind="folder",
        sourceDirPath=str(tmp_path / "gone"),
    )
    warning = check_source_staleness(workspace, config, manifest)
    assert warning is not None
    assert "已丢失" in warning


def test_folder_source_without_sync_hash_falls_back_to_current(
    workspace, config, tmp_path
) -> None:
    """旧 manifest 无 sourceSyncHash：退化比对 current/ 内容指纹。"""
    src = _make_source_dir(tmp_path)
    current = tmp_path / "current"
    current.mkdir()
    (current / "main.py").write_text("v1")
    manifest = _make_manifest(
        sourceKind="folder",
        sourceDirPath=str(src),
        sourceSyncHash=None,
        appPath=str(current),
    )
    assert check_source_staleness(workspace, config, manifest) is None
    (src / "main.py").write_text("v2")
    assert check_source_staleness(workspace, config, manifest) is not None


# ---- check_source_staleness：git 源 -----------------------------------------


def _git_manifest() -> InstanceManifest:
    return _make_manifest(
        sourceKind="git",
        sourceGitUrl="https://github.com/octo/demo",
        sourceGitRef="main",
        sourceGitRefKind="branch",
        sourceGitCommit=_STORED_OID,
    )


def test_git_source_oid_advanced_warns(workspace, config, monkeypatch) -> None:
    """远端 OID 前进 → 警告建议更新。"""
    monkeypatch.setattr(
        "local_webpage_access.git_source.probe_remote_commit",
        lambda url, *, ref, ref_kind, timeout=5: _ADVANCED_OID,
    )
    warning = check_source_staleness(workspace, config, _git_manifest())
    assert warning is not None
    assert "新提交" in warning
    assert "aaaaaaaa" in warning  # 旧 OID 短 SHA
    assert "bbbbbbbb" in warning  # 新 OID 短 SHA


def test_git_source_offline_no_warning_no_block(workspace, config, monkeypatch) -> None:
    """ls-remote 探测失败（离线）→ 返回 None，绝不变成 rebuild 阻断。"""
    from local_webpage_access.errors import GitSourceError

    def _boom(url, *, ref, ref_kind, timeout=5):
        raise GitSourceError("offline", kind="remote_unreachable")

    monkeypatch.setattr(
        "local_webpage_access.git_source.probe_remote_commit", _boom
    )
    assert check_source_staleness(workspace, config, _git_manifest()) is None


def test_git_source_same_oid_no_warning(workspace, config, monkeypatch) -> None:
    monkeypatch.setattr(
        "local_webpage_access.git_source.probe_remote_commit",
        lambda url, *, ref, ref_kind, timeout=5: _STORED_OID,
    )
    assert check_source_staleness(workspace, config, _git_manifest()) is None


def test_git_source_incomplete_identity_no_probe(workspace, config, monkeypatch) -> None:
    """身份字段不全（缺 ref）→ 不探测、不报警。"""
    called: list = []
    monkeypatch.setattr(
        "local_webpage_access.git_source.probe_remote_commit",
        lambda *a, **kw: called.append(1) or _ADVANCED_OID,
    )
    manifest = _make_manifest(
        sourceKind="git",
        sourceGitUrl="https://github.com/octo/demo",
        sourceGitCommit=_STORED_OID,
    )
    assert check_source_staleness(workspace, config, manifest) is None
    assert not called


def test_zip_source_returns_none(workspace, config) -> None:
    """zip / 无源实例不参与陈旧检测。"""
    assert check_source_staleness(workspace, config, _make_manifest()) is None


# ---- rebuild_instance：警告透出 + 事件，不阻断 -------------------------------


def test_rebuild_warns_via_out_and_event_but_still_rebuilds(
    workspace, registry, config, tmp_path, stub_hosting
) -> None:
    """folder 源漂移：out 收警告 + registry 写 source_stale 事件 + 重建照常。"""
    src = _make_source_dir(tmp_path)
    _seed_instance(
        workspace,
        registry,
        sourceKind="folder",
        sourceDirPath=str(src),
        sourceSyncHash=compute_source_hash(src),
    )
    (src / "main.py").write_text("v2")

    out: list[str] = []
    manifest = rebuild_instance(workspace, config, registry, "api", out=out)

    assert manifest.status == Status.RUNNING  # 重建未被警告阻断
    assert len(out) == 1 and "不一致" in out[0]
    events = registry.list_events("api")
    assert any(e["event_type"] == "source_stale" for e in events)


def test_rebuild_no_drift_no_warning(
    workspace, registry, config, tmp_path, stub_hosting
) -> None:
    src = _make_source_dir(tmp_path)
    _seed_instance(
        workspace,
        registry,
        sourceKind="folder",
        sourceDirPath=str(src),
        sourceSyncHash=compute_source_hash(src),
    )
    out: list[str] = []
    rebuild_instance(workspace, config, registry, "api", out=out)
    assert out == []
    assert not any(
        e["event_type"] == "source_stale" for e in registry.list_events("api")
    )


def test_rebuild_zip_source_no_warning(
    workspace, registry, config, stub_hosting
) -> None:
    """zip 源 rebuild 不受影响，且既有调用方式（无 out）兼容。"""
    _seed_instance(workspace, registry)
    manifest = rebuild_instance(workspace, config, registry, "api")
    assert manifest.status == Status.RUNNING
    assert not any(
        e["event_type"] == "source_stale" for e in registry.list_events("api")
    )


# ---- CLI：lwa rebuild [--sync] -----------------------------------------------

_SALT = "staleness-test"


def _patched_cli_env(monkeypatch, workspace, config, registry):
    """把 CLI 的工作区打开与真实重建替换为受控替身。"""
    monkeypatch.setattr(
        "local_webpage_access.cli.lifecycle.open_workspace_registry",
        lambda: (workspace, config, registry),
    )
    monkeypatch.setattr(
        "local_webpage_access.lifecycle.rebuild_instance",
        lambda ws, cfg, reg, iid, *, out=None: _make_manifest(iid),
    )
    # rebuild 的 finally 会 reg.close()；测试后还要断言，挡住 close
    monkeypatch.setattr(registry, "close", lambda: None)


def test_cli_rebuild_sync_folder_goes_through_update_from_dir(
    workspace, registry, config, tmp_path, monkeypatch
) -> None:
    """--sync + folder 源 → 调 Importer.update_from_dir（复用更新管线）。"""
    src = _make_source_dir(tmp_path)
    _seed_instance(
        workspace,
        registry,
        sourceKind="folder",
        sourceDirPath=str(src),
        sourceSyncHash=compute_source_hash(src),
    )
    _patched_cli_env(monkeypatch, workspace, config, registry)

    calls: list[dict] = []
    monkeypatch.setattr(
        "local_webpage_access.importer.Importer.update_from_dir",
        lambda self, iid, **kw: calls.append({"iid": iid, **kw})
        or SimpleNamespace(skipped=False),
    )

    from local_webpage_access.cli import app

    result = CliRunner().invoke(app, ["rebuild", "api", "--sync"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["iid"] == "api"
    assert calls[0]["restart"] is False  # 由随后的 rebuild 统一重建
    assert calls[0]["keep_data"] is True


def test_cli_rebuild_sync_git_goes_through_update_from_git(
    workspace, registry, config, monkeypatch
) -> None:
    """--sync + git 源 → 调 Importer.update_from_git。"""
    _seed_instance(
        workspace,
        registry,
        sourceKind="git",
        sourceGitUrl="https://github.com/octo/demo",
        sourceGitRef="main",
        sourceGitRefKind="branch",
        sourceGitCommit=_STORED_OID,
    )
    _patched_cli_env(monkeypatch, workspace, config, registry)

    calls: list[dict] = []
    monkeypatch.setattr(
        "local_webpage_access.importer.Importer.update_from_git",
        lambda self, iid, **kw: calls.append({"iid": iid, **kw})
        or SimpleNamespace(skipped=True),
    )

    from local_webpage_access.cli import app

    result = CliRunner().invoke(app, ["rebuild", "api", "--sync"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["iid"] == "api"
    assert "无变更" in result.output


def test_cli_rebuild_sync_zip_rejected(workspace, registry, config, monkeypatch) -> None:
    """zip / 无源 + --sync → 提示不支持并非零退出。"""
    _seed_instance(workspace, registry)  # sourceKind 默认 zip
    _patched_cli_env(monkeypatch, workspace, config, registry)

    from local_webpage_access.cli import app

    result = CliRunner().invoke(app, ["rebuild", "api", "--sync"])
    assert result.exit_code == 2
    assert "无上游源码" in result.output


def test_cli_rebuild_default_prints_staleness_warning(
    workspace, registry, config, tmp_path, monkeypatch
) -> None:
    """默认（无 --sync）：检测到陈旧打印醒目警告后仍重建（exit 0）。"""
    src = _make_source_dir(tmp_path)
    _seed_instance(
        workspace,
        registry,
        sourceKind="folder",
        sourceDirPath=str(src),
        sourceSyncHash=compute_source_hash(src),
    )
    (src / "main.py").write_text("v2")
    _patched_cli_env(monkeypatch, workspace, config, registry)

    def _fake_rebuild(ws, cfg, reg, iid, *, out=None):
        if out is not None:
            out.append("源码目录与 current/ 不一致（上次同步后源码已变更）")
        return _make_manifest(iid)

    monkeypatch.setattr(
        "local_webpage_access.lifecycle.rebuild_instance", _fake_rebuild
    )

    from local_webpage_access.cli import app

    result = CliRunner().invoke(app, ["rebuild", "api"])
    assert result.exit_code == 0, result.output
    assert "源码陈旧" in result.output
    assert "已重建实例：api" in result.output


# ---- doctor：check_source_freshness ------------------------------------------


def test_doctor_source_freshness_warn_on_drift(workspace, tmp_path) -> None:
    src = _make_source_dir(tmp_path)
    manifest = _make_manifest(
        sourceKind="folder",
        sourceDirPath=str(src),
        sourceSyncHash=compute_source_hash(src),
        appPath=str(workspace.app_current("api")),
    )
    manifest.save(workspace.app_manifest_path("api"))
    (src / "main.py").write_text("v2")

    result = check_source_freshness(workspace)
    assert result.name == "source_freshness"
    assert result.status == STATUS_WARN
    assert "api" in (result.detail or "")
    assert "--sync" in (result.suggestion or "")


def test_doctor_source_freshness_warn_on_missing_dir(workspace, tmp_path) -> None:
    manifest = _make_manifest(
        sourceKind="folder",
        sourceDirPath=str(tmp_path / "gone"),
    )
    manifest.save(workspace.app_manifest_path("api"))

    result = check_source_freshness(workspace)
    assert result.status == STATUS_WARN
    assert "源目录丢失" in (result.detail or "")


def test_doctor_source_freshness_ok_when_fresh(workspace, tmp_path) -> None:
    src = _make_source_dir(tmp_path)
    manifest = _make_manifest(
        sourceKind="folder",
        sourceDirPath=str(src),
        sourceSyncHash=compute_source_hash(src),
        appPath=str(workspace.app_current("api")),
    )
    manifest.save(workspace.app_manifest_path("api"))

    result = check_source_freshness(workspace)
    assert result.status == STATUS_OK


def test_doctor_source_freshness_skips_git(workspace) -> None:
    """git 源不触网：SKIP。"""
    _git_manifest().save(workspace.app_manifest_path("api"))

    result = check_source_freshness(workspace)
    assert result.status == STATUS_SKIP
    assert "git" in result.message


def test_doctor_source_freshness_zip_not_participating(workspace) -> None:
    """zip 源不参与 → OK（无 folder/git 源需检查）。"""
    _make_manifest().save(workspace.app_manifest_path("api"))

    result = check_source_freshness(workspace)
    assert result.status == STATUS_OK


def test_run_doctor_includes_source_freshness(workspace, config) -> None:
    """issue #8：源码新鲜度检查接入 run_doctor 报告。"""
    from local_webpage_access.doctor import run_doctor

    report = run_doctor(workspace, config)
    assert any(c.name == "source_freshness" for c in report.checks)
