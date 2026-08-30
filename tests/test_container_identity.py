"""容器运行身份测试（issue #20：非 root 容器 + 宿主 data/ UID/GID 对齐）。

覆盖：
- 统一身份解析（container_identity）三态语义与 data/ 属主对齐；
- Dockerfile 三类模板（node/python/generic）在 runAsNonRoot=True 时生成
  ``USER <uid>:<gid>``（CMD 之前）与 ``ENV HOME=/tmp``；
- Compose ``user:`` 防御层与 Dockerfile 使用同一解析结果（UID:GID 一致）；
- rebuild 前预检：data/ 属主为 root 或不可写时在停止旧容器前拒绝；
- 新导入实例显式 True；scan / import --update 保留三态；
- ``lwa migrate-user`` 显式迁移（通过检查才写盘 / root 属主拒绝 / --root）。
"""

from __future__ import annotations

import os
import stat as stat_module
from pathlib import Path

import pytest

from local_webpage_access.compose import generate_compose
from local_webpage_access.container_identity import (
    ContainerIdentity,
    ensure_non_root_identity_ready,
    resolve_container_identity,
)
from local_webpage_access.dockerfile_templates import generate_dockerfile
from local_webpage_access.errors import ConfigError
from local_webpage_access.models import (
    ContainerConfig,
    EntryConfig,
    InstanceManifest,
    Kind,
    ResourceProfile,
    Runtime,
    ServingMode,
)
from local_webpage_access.paths import Workspace


def _mk_manifest(
    *,
    mid: str = "api",
    kind: Kind = Kind.PYTHON,
    run_as_non_root: bool | None = None,
    install: str | None = None,
    start: str | None = None,
) -> InstanceManifest:
    return InstanceManifest(
        id=mid,
        name=mid,
        version="1",
        kind=kind,
        stack=[],
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.SMALL,
        container=ContainerConfig(
            projectName=f"lwa-{mid}",
            internalPort=8000,
            composePath="docker/compose.yaml",
            dockerfilePath="docker/Dockerfile",
            runAsNonRoot=run_as_non_root,
        ),
        entry=EntryConfig(install=install, start=start),
    )


def _ensure_data_dir(workspace: Workspace, mid: str = "api") -> Path:
    data_dir = workspace.app_data(mid)
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


# ---- 身份解析 ----------------------------------------------------------------


def test_resolve_identity_aligns_with_data_dir_owner(workspace: Workspace) -> None:
    """runAsNonRoot=True：身份取 data/ 目录宿主属主。"""
    data_dir = _ensure_data_dir(workspace)
    m = _mk_manifest(run_as_non_root=True)
    identity = resolve_container_identity(m, workspace)
    assert identity is not None
    st = data_dir.stat()
    assert identity == ContainerIdentity(uid=st.st_uid, gid=st.st_gid)


def test_resolve_identity_disabled_states_return_none(workspace: Workspace) -> None:
    """runAsNonRoot=None / False（legacy 与显式 root）不生成身份。"""
    for flag in (None, False):
        m = _mk_manifest(run_as_non_root=flag)
        assert resolve_container_identity(m, workspace) is None


def test_resolve_identity_creates_missing_data_dir(workspace: Workspace) -> None:
    """data/ 缺失时兜底创建（进程属主），Dockerfile/Compose 仍同源。"""
    m = _mk_manifest(run_as_non_root=True)
    identity = resolve_container_identity(m, workspace)
    assert identity is not None
    data_dir = workspace.app_data("api")
    assert data_dir.is_dir()
    st = data_dir.stat()
    assert identity.uid == st.st_uid


# ---- Dockerfile USER / Compose user -----------------------------------------


@pytest.mark.parametrize(
    ("kind", "install", "start"),
    [
        (Kind.PYTHON, "pip install -r requirements.txt", "uvicorn main:app"),
        (Kind.NODE, "npm ci", "node server.js"),
        # generic 兜底（kind 既非 python 也 node 的异常形态）
        (Kind.STATIC, None, "python -m http.server 8000"),
    ],
)
def test_dockerfile_templates_emit_user_directive(
    workspace: Workspace, kind: Kind, install: str | None, start: str | None
) -> None:
    """issue #20：三类模板 runAsNonRoot=True 时都在 CMD 前生成 USER + HOME。"""
    _ensure_data_dir(workspace)
    m = _mk_manifest(kind=kind, run_as_non_root=True, install=install, start=start)
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")

    lines = content.splitlines()
    user_idx = next(i for i, ln in enumerate(lines) if ln.startswith("USER "))
    cmd_idx = next(i for i, ln in enumerate(lines) if ln.startswith("CMD "))
    assert user_idx < cmd_idx, "USER 必须在 CMD 之前"
    assert lines[user_idx - 1] == "ENV HOME=/tmp"
    assert lines[user_idx].startswith("USER ") and ":root" not in lines[user_idx]

    from local_webpage_access.security import audit_dockerfile

    codes = [f.code for f in audit_dockerfile(content)]
    assert "no_user" not in codes
    assert "root_user" not in codes


def test_dockerfile_legacy_root_has_no_user(workspace: Workspace) -> None:
    """runAsNonRoot=None（legacy）：模板不变，审计 no_user 仍为 info。"""
    m = _mk_manifest(run_as_non_root=None, install="pip install -r requirements.txt")
    content = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    assert "\nUSER " not in content

    from local_webpage_access.security import audit_dockerfile

    finding = next(f for f in audit_dockerfile(content) if f.code == "no_user")
    assert finding.level == "info"


def test_compose_emits_user_line_matching_dockerfile(workspace: Workspace) -> None:
    """issue #20：Compose user 与 Dockerfile USER 使用同一解析结果（防漂移）。"""
    _ensure_data_dir(workspace)
    m = _mk_manifest(
        run_as_non_root=True,
        install="pip install -r requirements.txt",
        start="uvicorn main:app",
    )
    dockerfile = generate_dockerfile(m, workspace).read_text(encoding="utf-8")
    compose = generate_compose(m, workspace, host_port=18080).read_text(encoding="utf-8")

    df_user = next(
        ln.split(" ", 1)[1] for ln in dockerfile.splitlines() if ln.startswith("USER ")
    )
    assert f'user: "{df_user}"' in compose
    # 防御层不得出现在 legacy 实例的 compose 里
    legacy = _mk_manifest(run_as_non_root=None)
    legacy_compose = generate_compose(legacy, workspace, host_port=18080).read_text(
        encoding="utf-8"
    )
    assert "user:" not in legacy_compose


# ---- rebuild 前预检 -----------------------------------------------------------


def test_ensure_ready_rejects_root_owned_data_dir(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """runAsNonRoot=True 而 data/ 属主 root：停止旧容器前直接拒绝 + chown 指引。"""
    _ensure_data_dir(workspace)
    m = _mk_manifest(run_as_non_root=True)

    real_stat = os.stat

    def fake_stat(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        st = real_stat(path, *args, **kwargs)
        if str(path) == str(workspace.app_data("api")):
            # os.stat_result 由 10 元组构造：把 uid/gid 位换成 root(0)
            values = list(st)
            values[4] = 0  # st_uid
            values[5] = 0  # st_gid
            return os.stat_result(tuple(values))
        return st

    monkeypatch.setattr(os, "stat", fake_stat)
    with pytest.raises(ConfigError, match="chown"):
        ensure_non_root_identity_ready(m, workspace)


def test_ensure_ready_rejects_unwritable_data_dir(workspace: Workspace) -> None:
    """属主存在但目录无属主写权限（SQLite 建不了 -wal/-shm）同样拒绝。"""
    data_dir = _ensure_data_dir(workspace)
    m = _mk_manifest(run_as_non_root=True)
    os.chmod(data_dir, stat_module.S_IMODE(data_dir.stat().st_mode) & ~0o200)
    try:
        with pytest.raises(ConfigError, match="写权限"):
            ensure_non_root_identity_ready(m, workspace)
    finally:
        os.chmod(data_dir, 0o755)


def test_ensure_ready_legacy_warns_and_passes(
    workspace: Workspace, caplog: pytest.LogCaptureFixture
) -> None:
    """runAsNonRoot=None：legacy root 放行但留 WARNING（显式迁移提示）。"""
    import logging

    from local_webpage_access.container_identity import log as ci_log

    m = _mk_manifest(run_as_non_root=None)
    ci_log.addHandler(caplog.handler)
    orig_level = ci_log.level
    ci_log.setLevel(logging.WARNING)
    try:
        identity = ensure_non_root_identity_ready(m, workspace)
    finally:
        ci_log.removeHandler(caplog.handler)
        ci_log.setLevel(orig_level)

    assert identity is None
    assert any(
        "migrate-user" in r.getMessage()
        for r in caplog.records
        if r.levelname == "WARNING"
    )


def test_ensure_ready_explicit_false_logs_info(
    workspace: Workspace, caplog: pytest.LogCaptureFixture
) -> None:
    """runAsNonRoot=False（用户显式选择 root）：INFO 留痕放行。"""
    import logging

    from local_webpage_access.container_identity import log as ci_log

    m = _mk_manifest(run_as_non_root=False)
    ci_log.addHandler(caplog.handler)
    orig_level = ci_log.level
    ci_log.setLevel(logging.INFO)
    try:
        identity = ensure_non_root_identity_ready(m, workspace)
    finally:
        ci_log.removeHandler(caplog.handler)
        ci_log.setLevel(orig_level)

    assert identity is None
    assert any(
        "显式选择" in r.getMessage() and r.levelname == "INFO" for r in caplog.records
    )


# ---- 导入 / 更新保留三态 -------------------------------------------------------


def _make_zip(zip_path: Path, files: dict[str, str]) -> Path:
    import zipfile

    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for member, content in files.items():
            zf.writestr(member, content)
    return zip_path


@pytest.fixture()
def importer_ws_reg(workspace_root: Path):
    from local_webpage_access.config import Config
    from local_webpage_access.importer import Importer
    from local_webpage_access.registry import Registry

    ws = Workspace(workspace_root)
    ws.ensure_workspace_dirs()
    workspace_root.joinpath("registry").mkdir(parents=True, exist_ok=True)
    reg = Registry(workspace_root / "registry" / "local-web.db")
    reg.open()
    yield Importer(ws, Config(), reg), ws, reg
    reg.close()


def test_new_container_import_sets_run_as_non_root_true(
    importer_ws_reg, tmp_path: Path
) -> None:
    """issue #20：新导入容器实例显式 runAsNonRoot=True。"""
    importer, ws, _reg = importer_ws_reg
    zip_path = _make_zip(
        tmp_path / "api.zip",
        {"requirements.txt": "fastapi\nuvicorn\n", "main.py": "app = None\n"},
    )
    result = importer.import_zip(zip_path)
    assert result.manifest.runtime == Runtime.DOCKER_COMPOSE
    assert result.manifest.container is not None
    assert result.manifest.container.runAsNonRoot is True
    # 落盘一致
    saved = InstanceManifest.load(ws.app_manifest_path(result.instance_id))
    assert saved.container is not None
    assert saved.container.runAsNonRoot is True


def test_update_zip_preserves_run_as_non_root_states(
    importer_ws_reg, tmp_path: Path
) -> None:
    """issue #20：import --update 重建 manifest 不得静默切换运行身份三态。"""
    importer, ws, _reg = importer_ws_reg
    v1 = _make_zip(
        tmp_path / "api.zip",
        {"requirements.txt": "fastapi\nuvicorn\n", "main.py": "app = 1\n"},
    )
    iid = importer.import_zip(v1).instance_id
    manifest_path = ws.app_manifest_path(iid)

    v2 = _make_zip(
        tmp_path / "api-v2.zip",
        {"requirements.txt": "fastapi\nuvicorn\n", "main.py": "app = 2\n"},
    )

    # True（新导入默认）经更新保留
    r = importer.update_zip(v2, iid, restart=False)
    assert r.manifest.container is not None
    assert r.manifest.container.runAsNonRoot is True

    # legacy None：手工去掉字段语义（模拟旧 manifest），更新后保持 None 不突变
    m = InstanceManifest.load(manifest_path)
    assert m.container is not None
    m.container.runAsNonRoot = None
    m.save(manifest_path)
    r2 = importer.update_zip(v1, iid, restart=False)
    assert r2.manifest.container is not None
    assert r2.manifest.container.runAsNonRoot is None

    # 显式 False 同样保留
    m2 = InstanceManifest.load(manifest_path)
    assert m2.container is not None
    m2.container.runAsNonRoot = False
    m2.save(manifest_path)
    r3 = importer.update_zip(v2, iid, restart=False)
    assert r3.manifest.container is not None
    assert r3.manifest.container.runAsNonRoot is False


def test_apply_detection_preserves_run_as_non_root(
    importer_ws_reg, tmp_path: Path
) -> None:
    """issue #20：lwa scan（apply_detection_to_manifest）同样透传三态。"""
    from local_webpage_access.importer import apply_detection_to_manifest
    from local_webpage_access.scanner import Scanner

    importer, ws, _reg = importer_ws_reg
    zip_path = _make_zip(
        tmp_path / "api.zip",
        {"requirements.txt": "fastapi\nuvicorn\n", "main.py": "app = None\n"},
    )
    r1 = importer.import_zip(zip_path)
    iid = r1.instance_id
    manifest_path = ws.app_manifest_path(iid)

    for flag in (None, True, False):
        m = InstanceManifest.load(manifest_path)
        assert m.container is not None
        m.container.runAsNonRoot = flag
        m.save(manifest_path)
        detection = Scanner().detect(ws.app_current(iid))
        fresh = apply_detection_to_manifest(m, detection, ws)
        assert fresh.container is not None
        assert fresh.container.runAsNonRoot is flag


# ---- lwa migrate-user ---------------------------------------------------------


def test_migrate_user_command_roundtrip(
    importer_ws_reg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """migrate-user：通过预检写 True；--root 显式选择 False。"""
    from local_webpage_access.cli import migrate_user as cli_migrate

    importer, ws, _reg = importer_ws_reg
    zip_path = _make_zip(
        tmp_path / "api.zip",
        {"requirements.txt": "fastapi\nuvicorn\n", "main.py": "app = None\n"},
    )
    iid = importer.import_zip(zip_path).instance_id
    manifest_path = ws.app_manifest_path(iid)

    # 模拟 legacy 实例：字段为 None
    m = InstanceManifest.load(manifest_path)
    assert m.container is not None
    m.container.runAsNonRoot = None
    m.save(manifest_path)

    monkeypatch.setattr(
        "local_webpage_access.cli.migrate_user.open_workspace_registry",
        lambda: (ws, type("C", (), {})(), _reopen_registry(ws)),
    )
    cli_migrate.migrate_user(iid, root=False)
    saved = InstanceManifest.load(manifest_path)
    assert saved.container is not None
    assert saved.container.runAsNonRoot is True

    cli_migrate.migrate_user(iid, root=True)
    saved2 = InstanceManifest.load(manifest_path)
    assert saved2.container is not None
    assert saved2.container.runAsNonRoot is False


def test_migrate_user_rejects_root_owned_data(
    importer_ws_reg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """data/ 属主 root：迁移拒绝、manifest 不落盘。"""
    from local_webpage_access.cli import migrate_user as cli_migrate

    importer, ws, _reg = importer_ws_reg
    zip_path = _make_zip(
        tmp_path / "api.zip",
        {"requirements.txt": "fastapi\nuvicorn\n", "main.py": "app = None\n"},
    )
    iid = importer.import_zip(zip_path).instance_id
    manifest_path = ws.app_manifest_path(iid)
    m = InstanceManifest.load(manifest_path)
    assert m.container is not None
    m.container.runAsNonRoot = None
    m.save(manifest_path)

    monkeypatch.setattr(
        "local_webpage_access.cli.migrate_user.open_workspace_registry",
        lambda: (ws, type("C", (), {})(), _reopen_registry(ws)),
    )

    def fake_ensure(manifest, workspace):  # noqa: ANN001
        raise ConfigError("属主为 root(0)……请 chown", instance_id=manifest.id)

    monkeypatch.setattr(
        "local_webpage_access.container_identity.ensure_non_root_identity_ready",
        fake_ensure,
    )
    import typer

    with pytest.raises(typer.Exit):
        cli_migrate.migrate_user(iid, root=False)
    saved = InstanceManifest.load(manifest_path)
    assert saved.container is not None
    assert saved.container.runAsNonRoot is None, "预检失败不得写盘"


def test_migrate_user_rejects_non_container(
    importer_ws_reg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """静态实例无 container 配置，命令直接拒绝。"""
    from local_webpage_access.cli import migrate_user as cli_migrate

    importer, ws, _reg = importer_ws_reg
    static_zip = _make_zip(
        tmp_path / "demo.zip",
        {"index.html": "<html><body>hi</body></html>"},
    )
    iid = importer.import_zip(static_zip).instance_id

    monkeypatch.setattr(
        "local_webpage_access.cli.migrate_user.open_workspace_registry",
        lambda: (ws, type("C", (), {})(), _reopen_registry(ws)),
    )
    import typer

    with pytest.raises(typer.Exit):
        cli_migrate.migrate_user(iid, root=False)


def _reopen_registry(ws: Workspace):  # noqa: ANN202
    """CLI 命令内部会 reg.close()；测试补一个独立打开的 registry。"""
    from local_webpage_access.registry import Registry

    reg = Registry(ws.db_path)
    reg.open()
    return reg


def test_migrate_user_locks_instance(
    importer_ws_reg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BUG-167 纪律：manifest 读-改-写须持实例锁（回归：调用点在锁内）。"""
    from local_webpage_access.cli import migrate_user as cli_migrate
    from local_webpage_access.lifecycle import instance_lock

    importer, ws, _reg = importer_ws_reg
    zip_path = _make_zip(
        tmp_path / "api.zip",
        {"requirements.txt": "fastapi\nuvicorn\n", "main.py": "app = None\n"},
    )
    iid = importer.import_zip(zip_path).instance_id

    monkeypatch.setattr(
        "local_webpage_access.cli.migrate_user.open_workspace_registry",
        lambda: (ws, type("C", (), {})(), _reopen_registry(ws)),
    )
    lock_calls: list[str] = []
    orig_lock = instance_lock

    def spy_lock(*a, **kw):  # noqa: ANN002, ANN003
        lock_calls.append("acquired")
        return orig_lock(*a, **kw)

    monkeypatch.setattr("local_webpage_access.cli.migrate_user.instance_lock", spy_lock)
    cli_migrate.migrate_user(iid, root=False)
    assert lock_calls, "migrate-user 必须持实例锁写 manifest"
