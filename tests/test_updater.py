"""``lwa update`` 工作区热重载测试（IMP-008 / WBS-008.05）。

全部 mock subprocess / manager / daemon / doctor，避免依赖真实 pip、
不启动真实管理页进程、不要求 Docker。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from local_webpage_access.config import Config, PortPool, load_config
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry
from local_webpage_access.updater import (
    UpdateOptions,
    UpdateReport,
    locate_repo,
    migrate_config_defaults,
    run_update,
    sync_skills,
)


# ---- fixtures --------------------------------------------------------------


@pytest.fixture()
def workspace_root(tmp_path: Path) -> Path:
    return tmp_path / "ws"


@pytest.fixture()
def workspace(workspace_root: Path) -> Workspace:
    ws = Workspace(workspace_root)
    ws.ensure_workspace_dirs()
    # 写一份最小配置，使 require_workspace 能识别
    ws.config_path.write_text(
        "managerPort: 17800\n"
        "managerHost: 127.0.0.1\n"
        "managerEnabled: false\n"
        "portPool:\n"
        "  start: 21000\n"
        "  end: 21050\n"
        "staticGateway: builtin\n",
        encoding="utf-8",
    )
    return ws


@pytest.fixture()
def config(workspace: Workspace) -> Config:
    return load_config(workspace)


@pytest.fixture()
def registry(workspace_root: Path) -> Registry:
    workspace_root.joinpath("registry").mkdir(parents=True, exist_ok=True)
    reg = Registry(workspace_root / "registry" / "local-web.db")
    reg.open()
    yield reg
    reg.close()


def _opts(**kw) -> UpdateOptions:
    """默认关闭 doctor/重启，单测按需开启，避免触发真实 subprocess。"""
    base = dict(
        dry_run=False,
        skip_pip=True,
        sync_skills=False,
        sync_templates=False,
        restart_manager=False,
        restart_daemon=False,
        restart_instances=False,
        run_doctor=False,
        repo=None,
    )
    base.update(kw)
    return UpdateOptions(**base)


# ---- locate_repo -----------------------------------------------------------


def test_locate_repo_explicit_valid(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert locate_repo(str(repo)) == repo.resolve()


def test_locate_repo_explicit_missing_pyproject(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="pyproject.toml"):
        locate_repo(str(tmp_path))


def test_locate_repo_falls_back_to_editable_install() -> None:
    """无 --repo 时应定位到本包的 editable 安装根（含 pyproject.toml）。"""
    repo = locate_repo(None)
    # 在 editable 安装的开发环境下应识别到；wheel 安装时可能为 None，跳过
    if repo is None:
        pytest.skip("非 editable 安装环境，git 兜底也未命中")
    assert (repo / "pyproject.toml").is_file()


# ---- migrate_config_defaults ----------------------------------------------


def test_migrate_config_adds_missing_keys(workspace: Workspace) -> None:
    """配置文件缺字段时补齐，原值保留，并生成 .bak。"""
    # 删掉 staticGatewayPort（IMP-006 引入的新字段），模拟旧配置
    raw = workspace.config_path.read_text(encoding="utf-8")
    assert "staticGatewayPort" not in raw

    cfg = load_config(workspace)
    missing, written = migrate_config_defaults(workspace, cfg)

    assert "staticGatewayPort" in missing
    assert written is True
    # 备份生成
    assert workspace.config_path.with_suffix(".yml.bak").is_file()
    # 写回后文件含新字段，且原 managerPort 不变
    new_raw = workspace.config_path.read_text(encoding="utf-8")
    assert "staticGatewayPort" in new_raw
    parsed = yaml.safe_load(new_raw)
    assert parsed["managerPort"] == 17800


def test_migrate_config_noop_when_complete(workspace: Workspace) -> None:
    """配置已含全部字段时不写盘、不备份。"""
    # 用 Config 默认值写一份完整配置
    full = Config(portPool=PortPool(start=21000, end=21050)).model_dump()
    workspace.config_path.write_text(
        yaml.safe_dump(full, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    cfg = load_config(workspace)
    missing, written = migrate_config_defaults(workspace, cfg)
    assert missing == []
    assert written is False
    assert not workspace.config_path.with_suffix(".yml.bak").exists()


def test_migrate_config_deep_merges_nested_dict(workspace: Workspace) -> None:
    """嵌套字段部分缺失时深层补齐：用户写的子键保留，缺失子键从默认补全（BUG-056）。

    旧 ``{**defaults, **existing}`` 浅合并会让 ``portPool: {start: ...}`` 整体覆盖
    默认的 ``{start, end}``，导致 ``end`` 在写回文件里丢失。深层合并应保留用户的
    ``start`` 并从默认补齐 ``end``。
    """
    partial = {
        "managerPort": 17800,
        "portPool": {"start": 19000},  # 缺 end；同时缺 staticGatewayPort 触发迁移
    }
    workspace.config_path.write_text(
        yaml.safe_dump(partial, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    cfg = load_config(workspace)
    missing, written = migrate_config_defaults(workspace, cfg)

    assert written is True
    assert "staticGatewayPort" in missing  # 触发迁移的顶层缺失键
    parsed = yaml.safe_load(workspace.config_path.read_text(encoding="utf-8"))
    # 用户的 start 保留，end 从默认补齐（深层合并，非整体覆盖）
    assert parsed["portPool"]["start"] == 19000
    assert parsed["portPool"]["end"] == Config().portPool.end


# ---- sync_skills -----------------------------------------------------------


def test_sync_skills_adds_new_files(workspace: Workspace, monkeypatch) -> None:
    """包内 skills 同步到工作区，新增文件计入 added。"""
    # 临时把 bundled skills 指向一个临时目录，造一个确定的新文件
    import local_webpage_access.updater as upd

    fake_bundled = workspace.root / "_fake_skills"
    fake_bundled.mkdir()
    (fake_bundled / "demo-skill").mkdir()
    (fake_bundled / "demo-skill" / "SKILL.md").write_text("hello")
    monkeypatch.setattr(upd, "_BUNDLED_SKILLS", fake_bundled)

    added, updated, skipped = sync_skills(workspace)
    assert "demo-skill/SKILL.md" in added
    assert (workspace.skills / "demo-skill" / "SKILL.md").read_text() == "hello"

    # 第二次同步：内容相同 → skipped
    added2, updated2, skipped2 = sync_skills(workspace)
    assert added2 == []
    assert "demo-skill/SKILL.md" in skipped2


def test_sync_skills_overwrites_stale(workspace: Workspace, monkeypatch) -> None:
    """force=True 时内容变化的文件被覆盖并计入 updated。"""
    import local_webpage_access.updater as upd

    fake_bundled = workspace.root / "_fake_skills"
    (fake_bundled / "s").mkdir(parents=True)
    (fake_bundled / "s" / "SKILL.md").write_text("v2")
    monkeypatch.setattr(upd, "_BUNDLED_SKILLS", fake_bundled)

    # 工作区已有旧版本
    (workspace.skills / "s").mkdir(parents=True)
    (workspace.skills / "s" / "SKILL.md").write_text("v1")

    added, updated, skipped = sync_skills(workspace)
    assert updated == ["s/SKILL.md"]
    assert (workspace.skills / "s" / "SKILL.md").read_text() == "v2"


def test_sync_skills_keeps_user_custom(workspace: Workspace, monkeypatch) -> None:
    """用户自建的自定义 skill 不在 bundled 中时不被删除。"""
    import local_webpage_access.updater as upd

    fake_bundled = workspace.root / "_fake_skills"
    (fake_bundled / "bundled").mkdir(parents=True)
    (fake_bundled / "bundled" / "SKILL.md").write_text("b")
    monkeypatch.setattr(upd, "_BUNDLED_SKILLS", fake_bundled)

    (workspace.skills / "my-custom").mkdir(parents=True)
    (workspace.skills / "my-custom" / "SKILL.md").write_text("mine")

    sync_skills(workspace)
    assert (workspace.skills / "my-custom" / "SKILL.md").read_text() == "mine"


# ---- run_update: dry-run ---------------------------------------------------


def test_dry_run_makes_no_changes(
    workspace: Workspace, config: Config, registry: Registry
) -> None:
    """--dry-run 不产生文件/进程变更，所有步骤 skipped。"""
    opts = _opts(
        dry_run=True,
        skip_pip=False,
        sync_skills=True,
        sync_templates=True,
        restart_manager=True,
        restart_daemon=True,
        restart_instances=True,
        run_doctor=True,
        repo=None,
    )
    report = run_update(workspace, config, registry, options=opts)

    # 全部步骤 skipped，无 ok/failed/pending
    assert all(s.status == "skipped" for s in report.steps)
    assert not report.has_failures
    # skills/ 目录没有新增（dry-run 不同步）
    # （workspace.skills 已由 fixture 创建为空目录）


# ---- run_update: pip -------------------------------------------------------


def test_pip_success_clears_version_cache(
    workspace: Workspace, config: Config, registry: Registry, monkeypatch
) -> None:
    """pip 成功后调用 resolve_version.cache_clear()，让 version_after 反映新代码。"""
    from local_webpage_access import version_info

    calls = {"pip": False, "cleared": False}

    def fake_pip(repo: Path) -> str:
        calls["pip"] = True
        return "Successfully installed local-webpage-access"

    monkeypatch.setattr("local_webpage_access.updater.run_pip_install", fake_pip)
    # 让 locate_repo 命中一个假源码根
    fake_repo = workspace.root / "repo"
    fake_repo.mkdir()
    (fake_repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    monkeypatch.setattr("local_webpage_access.updater.locate_repo", lambda explicit: fake_repo)

    # 替换 resolve_version 为带 cache_clear 的替身，验证 run_update 会调用它
    def fake_resolve() -> str:
        return "1.2.3"

    def fake_clear() -> None:
        calls["cleared"] = True

    fake_resolve.cache_clear = fake_clear  # type: ignore[attr-defined]
    monkeypatch.setattr(version_info, "resolve_version", fake_resolve)

    opts = _opts(skip_pip=False, run_doctor=False)
    report = run_update(workspace, config, registry, options=opts)

    assert calls["pip"] is True
    assert calls["cleared"] is True
    pip_step = report.step("pip")
    assert pip_step.status == "ok"
    assert report.version_before == "1.2.3"
    assert report.version_after == "1.2.3"


def test_pip_failure_does_not_block_other_steps(
    workspace: Workspace, config: Config, registry: Registry, monkeypatch
) -> None:
    """pip 失败标记 failed，但后续 sync_skills 仍执行。"""
    monkeypatch.setattr(
        "local_webpage_access.updater.run_pip_install",
        lambda repo: (_ for _ in ()).throw(RuntimeError("pip boom")),
    )
    fake_repo = workspace.root / "repo"
    fake_repo.mkdir()
    (fake_repo / "pyproject.toml").write_text("x")
    monkeypatch.setattr("local_webpage_access.updater.locate_repo", lambda e: fake_repo)

    # 造一个可同步的 skill
    import local_webpage_access.updater as upd

    fake_bundled = workspace.root / "_skills"
    (fake_bundled / "s").mkdir(parents=True)
    (fake_bundled / "s" / "SKILL.md").write_text("x")
    monkeypatch.setattr(upd, "_BUNDLED_SKILLS", fake_bundled)

    opts = _opts(skip_pip=False, sync_skills=True)
    report = run_update(workspace, config, registry, options=opts)

    assert report.step("pip").status == "failed"
    assert report.step("syncSkills").status == "ok"
    assert report.has_failures is True


# ---- run_update: manager/daemon restart -----------------------------------


def test_manager_restart_skipped_when_not_running(
    workspace: Workspace, config: Config, registry: Registry, monkeypatch
) -> None:
    """manager 原本未运行 → 步骤 skipped，不调用 start_manager。"""
    started = {"count": 0}

    def fake_start(ws, cfg):
        started["count"] += 1
        return 999

    monkeypatch.setattr(
        "local_webpage_access.manager_service.is_running", lambda ws, cfg: False
    )
    monkeypatch.setattr("local_webpage_access.manager_service.stop_manager", lambda ws: True)
    monkeypatch.setattr("local_webpage_access.manager_service.start_manager", fake_start)

    opts = _opts(restart_manager=True)
    report = run_update(workspace, config, registry, options=opts)

    step = report.step("restartManager")
    assert step.status == "skipped"
    assert started["count"] == 0


def test_manager_restart_runs_for_legacy_health_without_workspace_root(
    workspace: Workspace, config: Config, registry: Registry, monkeypatch
) -> None:
    """BUG-065：旧版 health 无 workspaceRoot 时 update 仍应 restart manager。"""
    from local_webpage_access.manager_service import ManagerState, write_state

    write_state(
        workspace,
        ManagerState(
            enabled=True, pid=4242, host="0.0.0.0", port=config.managerPort
        ),
    )
    calls = {"stop": 0, "start": 0}

    # 自启动不在管（managed=False）→ 走 stop+start 路径
    monkeypatch.setattr(
        "local_webpage_access.cli._common.coordinated_autostart_restart",
        lambda ws, name: (None, True, False),
    )
    monkeypatch.setattr(
        "local_webpage_access.manager_service._fetch_health",
        lambda *a, **k: {"ok": True},
    )
    monkeypatch.setattr(
        "local_webpage_access.manager_service.is_pid_alive", lambda pid: True
    )
    monkeypatch.setattr(
        "local_webpage_access.manager_service.stop_manager",
        lambda ws: calls.__setitem__("stop", calls["stop"] + 1) or True,
    )
    monkeypatch.setattr(
        "local_webpage_access.manager_service.start_manager",
        lambda ws, cfg: calls.__setitem__("start", calls["start"] + 1) or 888,
    )

    report = run_update(workspace, config, registry, options=_opts(restart_manager=True))

    step = report.step("restartManager")
    assert step.status == "ok"
    assert calls["stop"] == 1
    assert calls["start"] == 1


def test_manager_restart_failure_captured(
    workspace: Workspace, config: Config, registry: Registry, monkeypatch
) -> None:
    """manager 重启失败标记 failed，但不中断 doctor 等后续步骤。"""
    # 自启动不在管 → 走 stop+start 路径
    monkeypatch.setattr(
        "local_webpage_access.cli._common.coordinated_autostart_restart",
        lambda ws, name: (None, True, False),
    )
    monkeypatch.setattr(
        "local_webpage_access.manager_service.is_running", lambda ws, cfg: True
    )
    monkeypatch.setattr("local_webpage_access.manager_service.stop_manager", lambda ws: True)
    monkeypatch.setattr(
        "local_webpage_access.manager_service.start_manager",
        lambda ws, cfg: (_ for _ in ()).throw(RuntimeError("start boom")),
    )
    # doctor 也开，验证它仍然执行
    monkeypatch.setattr(
        "local_webpage_access.updater.run_doctor_check", lambda ws, cfg: "ok"
    )

    opts = _opts(restart_manager=True, run_doctor=True)
    report = run_update(workspace, config, registry, options=opts)

    mgr = report.step("restartManager")
    assert mgr.status == "failed"
    assert "pip 已更新" in mgr.message or "start boom" in mgr.message
    assert report.step("doctor").status == "ok"


def test_daemon_not_started_when_stopped(
    workspace: Workspace, config: Config, registry: Registry, monkeypatch
) -> None:
    """daemon 原本 stopped → 不被自动开启。"""
    started = {"count": 0}

    def fake_start(ws, cfg, **kw):
        started["count"] += 1
        return 1

    import local_webpage_access.daemon as dmod

    monkeypatch.setattr(dmod, "is_running", lambda ws: False)
    monkeypatch.setattr(dmod, "stop_daemon", lambda ws: True)
    monkeypatch.setattr(dmod, "start_daemon", fake_start)

    opts = _opts(restart_daemon=True)
    report = run_update(workspace, config, registry, options=opts)

    step = report.step("restartDaemon")
    assert step.status == "skipped"
    assert started["count"] == 0


def test_daemon_restart_via_autostart_when_managed(
    workspace: Workspace, config: Config, registry: Registry, monkeypatch
) -> None:
    """BUG-191：自启动在管 daemon 时重启走监督器（managed=True），不 stop+start
    detached，避免 KeepAlive/Restart 拉回与新 spawn 抢锁产生重复 watcher。"""
    import local_webpage_access.daemon as dmod

    calls = {"stop": 0, "start": 0}
    monkeypatch.setattr(dmod, "is_running", lambda ws: True)
    monkeypatch.setattr(dmod, "stop_daemon", lambda ws: calls.__setitem__("stop", calls["stop"] + 1) or True)
    monkeypatch.setattr(dmod, "start_daemon", lambda ws, cfg, **kw: calls.__setitem__("start", calls["start"] + 1) or 555)
    # 自启动接管重启 → managed=True
    monkeypatch.setattr(
        "local_webpage_access.cli._common.coordinated_autostart_restart",
        lambda ws, name: ("已通过自启动单元重启 daemon", True, True),
    )

    report = run_update(workspace, config, registry, options=_opts(restart_daemon=True))
    step = report.step("restartDaemon")
    assert step.status == "ok"
    assert calls["stop"] == 0  # 未走 stop
    assert calls["start"] == 0  # 未走 detached start


def test_daemon_restart_stop_failure_marks_failed(
    workspace: Workspace, config: Config, registry: Registry, monkeypatch
) -> None:
    """BUG-192：stop_daemon 返回 False（终止失败）时步骤标 failed，且不 start，
    不再把停止失败报成重启成功。"""
    import local_webpage_access.daemon as dmod

    started = {"n": 0}
    monkeypatch.setattr(dmod, "is_running", lambda ws: True)
    monkeypatch.setattr(dmod, "stop_daemon", lambda ws: False)  # 终止失败
    monkeypatch.setattr(dmod, "start_daemon", lambda ws, cfg, **kw: started.__setitem__("n", started["n"] + 1) or 555)
    monkeypatch.setattr(
        "local_webpage_access.cli._common.coordinated_autostart_restart",
        lambda ws, name: (None, True, False),
    )

    report = run_update(workspace, config, registry, options=_opts(restart_daemon=True))
    step = report.step("restartDaemon")
    assert step.status == "failed"
    assert started["n"] == 0  # 终止失败不得 start
    assert "停止失败" in step.message


# ---- run_update: restart-instances ----------------------------------------


def _seed_instance(registry: Registry, iid: str, status: str) -> None:
    """直接写一行 instances 记录（绕过完整 manifest，仅用于 restart 过滤测试）。"""
    from datetime import datetime

    now = datetime.now().isoformat()
    registry.upsert_instance({
        "id": iid,
        "name": iid,
        "version": "1",
        "kind": "static",
        "runtime": "shared-static",
        "serving_mode": "shared-static",
        "resource_profile": "tiny",
        "stack_json": "[]",
        "has_database": 0,
        "database_type": None,
        "database_json": None,
        "desired_state": "stopped",
        "status": status,
        "app_path": None,
        "source_zip_path": None,
        "created_at": now,
        "updated_at": now,
        "last_started_at": None,
        "last_health_check_at": None,
        "last_error": None,
    })


def test_restart_instances_skips_building(
    workspace: Workspace, config: Config, registry: Registry, monkeypatch
) -> None:
    """--restart-instances 跳过 building/queued/pending，仅处理可重启状态。"""
    restarted: list[str] = []

    def fake_restart(ws, cfg, reg, iid):
        restarted.append(iid)
        return None

    monkeypatch.setattr(
        "local_webpage_access.lifecycle.restart_instance", fake_restart
    )

    _seed_instance(registry, "running-1", "running")
    _seed_instance(registry, "stopped-1", "stopped")
    _seed_instance(registry, "building-1", "building")
    _seed_instance(registry, "queued-1", "queued")
    _seed_instance(registry, "pending-1", "pending")

    opts = _opts(restart_instances=True)
    report = run_update(workspace, config, registry, options=opts)

    step = report.step("restartInstances")
    assert step.status == "ok"
    assert set(restarted) == {"running-1", "stopped-1"}
    skipped = step.extra["skipped"]
    assert any("building-1" in s for s in skipped)
    assert any("queued-1" in s for s in skipped)
    assert any("pending-1" in s for s in skipped)


def test_restart_instances_continues_on_single_failure(
    workspace: Workspace, config: Config, registry: Registry, monkeypatch
) -> None:
    """单个实例重启失败不中断后续实例。"""

    def fake_restart(ws, cfg, reg, iid):
        if iid == "bad":
            raise RuntimeError("boom")
        return None

    monkeypatch.setattr(
        "local_webpage_access.lifecycle.restart_instance", fake_restart
    )
    _seed_instance(registry, "good", "running")
    _seed_instance(registry, "bad", "running")

    opts = _opts(restart_instances=True)
    report = run_update(workspace, config, registry, options=opts)

    step = report.step("restartInstances")
    assert step.status == "failed"  # 有失败
    assert "good" in step.extra["restarted"]
    assert "bad" in step.extra["failed"]


# ---- run_update: doctor + report ------------------------------------------


def test_doctor_step_runs(
    workspace: Workspace, config: Config, registry: Registry, monkeypatch
) -> None:
    monkeypatch.setattr(
        "local_webpage_access.updater.run_doctor_check", lambda ws, cfg: "warn"
    )
    opts = _opts(run_doctor=True)
    report = run_update(workspace, config, registry, options=opts)

    step = report.step("doctor")
    assert step.status == "ok"
    assert "WARN" in step.message
    assert report.doctor_status == "warn"


def test_report_to_dict_structure(
    workspace: Workspace, config: Config, registry: Registry, monkeypatch
) -> None:
    """JSON 输出草案字段齐全。"""
    opts = _opts(sync_skills=True, run_doctor=True)
    monkeypatch.setattr(
        "local_webpage_access.updater.run_doctor_check", lambda ws, cfg: "ok"
    )
    report = run_update(workspace, config, registry, options=opts)
    d = report.to_dict()

    assert d["workspace"] == str(workspace.root)
    assert "versionBefore" in d and "versionAfter" in d
    assert isinstance(d["steps"], list)
    assert all("name" in s and "status" in s for s in d["steps"])
    assert d["doctorStatus"] == "ok"


def test_format_report_renders(
    workspace: Workspace, config: Config, registry: Registry, monkeypatch
) -> None:
    from local_webpage_access.updater import format_report

    monkeypatch.setattr(
        "local_webpage_access.updater.run_doctor_check", lambda ws, cfg: "ok"
    )
    opts = _opts(run_doctor=True)
    report = run_update(workspace, config, registry, options=opts)
    text = format_report(report)
    assert "lwa update" in text
    assert "doctor" in text
