"""init_workspace 模块测试（WBS-03）。"""

from __future__ import annotations

from pathlib import Path

from local_web_access.config import load_config
from local_web_access.init_workspace import init_workspace
from local_web_access.paths import Workspace
from local_web_access.registry import Registry


def test_init_creates_full_layout(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    init_workspace(root)
    ws = Workspace(root)
    for directory in (
        ws.inbox,
        ws.apps,
        ws.registry_dir,
        ws.logs,
        ws.run,
        ws.templates,
        ws.skills,
        ws.static_sites,
        ws.manager,
    ):
        assert directory.is_dir(), f"{directory} 应被创建"


def test_init_writes_default_config(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    init_workspace(root)
    ws = Workspace(root)
    assert ws.config_path.is_file()
    cfg = load_config(ws)
    assert cfg.managerPort == 17800
    assert cfg.portPool.start == 18000


def test_init_creates_sqlite_db(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    init_workspace(root)
    ws = Workspace(root)
    assert ws.db_path.is_file()
    with Registry(ws.db_path) as reg:
        assert reg.total_count() == 0


def test_init_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    init_workspace(root)
    ws = Workspace(root)

    # 模拟已有实例
    with Registry(ws.db_path) as reg:
        from tests._helpers import make_static_manifest

        reg.upsert_from_manifest(make_static_manifest("existing"))

    # 再次初始化
    init_workspace(root)

    # 实例数据应保留
    with Registry(ws.db_path) as reg:
        assert reg.instance_exists("existing")


def test_init_preserves_config_by_default(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir(parents=True)
    ws = Workspace(root)
    ws.ensure_workspace_dirs()
    # 预置自定义配置
    ws.config_path.write_text("managerPort: 17801\n", encoding="utf-8")

    init_workspace(root)
    cfg = load_config(ws)
    assert cfg.managerPort == 17801  # 未被覆盖


def test_init_force_overwrites_config(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir(parents=True)
    ws = Workspace(root)
    ws.ensure_workspace_dirs()
    ws.config_path.write_text("managerPort: 17801\n", encoding="utf-8")

    init_workspace(root, force=True)
    cfg = load_config(ws)
    assert cfg.managerPort == 17800  # 被默认覆盖


def test_init_copies_templates(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    init_workspace(root)
    ws = Workspace(root)
    # 至少应有静态网关模板
    assert any(ws.templates.rglob("*.tpl"))


def test_init_copies_skills(tmp_path: Path) -> None:
    """lwa init 应把 12 个内置 skills 复制到 skills/（WBS-24）。"""
    root = tmp_path / "ws"
    init_workspace(root)
    ws = Workspace(root)
    skill_docs = list(ws.skills.rglob("SKILL.md"))
    assert len(skill_docs) == 12
    # 索引 README 也应存在
    assert (ws.skills / "README.md").is_file()
    # 关键 skill 应在列
    names = {p.parent.name for p in skill_docs}
    for expected in (
        "detect-stack",
        "dockerize-node-app",
        "dockerize-python-app",
        "generate-compose",
        "fix-docker-build-failure",
        "diagnose-health-check",
    ):
        assert expected in names, f"缺少 skill：{expected}"


def test_cli_init_e2e(tmp_path: Path) -> None:
    """通过 CLI 直接调用 init 子命令做端到端验证。"""
    from typer.testing import CliRunner

    from local_web_access.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["init", "--workspace", str(tmp_path / "cli-ws")])
    assert result.exit_code == 0, result.output
    assert "已初始化工作区" in result.output
    assert (tmp_path / "cli-ws" / "local-web.yml").is_file()
    assert (tmp_path / "cli-ws" / "registry" / "local-web.db").is_file()
