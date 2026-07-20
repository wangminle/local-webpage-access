"""init_workspace 模块测试（WBS-03）。"""

from __future__ import annotations

from pathlib import Path

from local_webpage_access.config import load_config
from local_webpage_access.init_workspace import init_workspace
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry


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


def test_init_static_gateway_override(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    init_workspace(root, static_gateway="builtin")
    cfg = load_config(Workspace(root))
    assert cfg.staticGateway == "builtin"


def test_init_full_default_gateway_caddy(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    init_workspace(root, static_gateway="caddy")
    cfg = load_config(Workspace(root))
    assert cfg.staticGateway == "caddy"


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
    """lwa init 应把内置 skills 复制到 skills/（WBS-24 + lwa-setup-host-environment 等）。"""
    root = tmp_path / "ws"
    init_workspace(root)
    ws = Workspace(root)
    skill_docs = list(ws.skills.rglob("SKILL.md"))
    assert len(skill_docs) == 17  # IMP-038 新增 lwa-review-access-urls
    # 索引 README 也应存在
    assert (ws.skills / "README.md").is_file()
    # 关键 skill 应在列
    names = {p.parent.name for p in skill_docs}
    for expected in (
        "lwa-detect-stack",
        "lwa-dockerize-node-app",
        "lwa-dockerize-python-app",
        "lwa-generate-compose",
        "lwa-fix-docker-build-failure",
        "lwa-diagnose-health-check",
        "lwa-setup-host-environment",
        "lwa-setup-autostart",
        "lwa-update-runtime",
        "lwa-import-zip",
        "lwa-review-access-urls",
    ):
        assert expected in names, f"缺少 skill：{expected}"


def test_cli_init_e2e(tmp_path: Path) -> None:
    """通过 CLI 直接调用 init 子命令做端到端验证。"""
    from typer.testing import CliRunner

    from local_webpage_access.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["init", "--workspace", str(tmp_path / "cli-ws")])
    assert result.exit_code == 0, result.output
    assert "已初始化工作区" in result.output
    assert (tmp_path / "cli-ws" / "local-web.yml").is_file()
    assert (tmp_path / "cli-ws" / "registry" / "local-web.db").is_file()


def test_cli_init_full_passes_workspace_root(tmp_path: Path, monkeypatch) -> None:
    """BUG-251：init --full 必须把新工作区传给 run_full_bootstrap（含 -w 异于 cwd）。"""
    from typer.testing import CliRunner

    from local_webpage_access.cli import app
    from local_webpage_access.host_bootstrap import FullBootstrapResult

    captured: dict = {}

    def _fake_boot(**kwargs):
        captured.update(kwargs)
        return FullBootstrapResult(
            ok=False,
            planned=[],
            ran=[],
            messages=["session refresh needed"],
            overall="session_refresh_required",
            exit_code=2,
        )

    monkeypatch.setattr(
        "local_webpage_access.host_bootstrap.run_full_bootstrap",
        _fake_boot,
    )
    ws = tmp_path / "full-ws"
    runner = CliRunner()
    result = runner.invoke(
        app, ["init", "--workspace", str(ws), "--full", "--yes"]
    )
    assert result.exit_code == 2, result.output
    assert captured.get("workspace_root") == ws.resolve()
    assert captured.get("yes") is True
