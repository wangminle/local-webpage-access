"""init_workspace 模块测试（WBS-03）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from local_webpage_access.config import load_config
from local_webpage_access.init_workspace import init_workspace
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry


@pytest.fixture(autouse=True)
def _stub_maybe_start_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """BUG-450：init 单元测试不拉起真实 manager（避免 :17801 等端口孤儿）。

    ``init_workspace`` 默认 ``maybe_start_manager``；本模块只断言布局/配置/skills，
    会话级泄漏网是第二道兜底，这里从根上禁止 spawn。
    """
    monkeypatch.setattr(
        "local_webpage_access.manager_service.maybe_start_manager",
        lambda *a, **k: None,
    )


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
    assert len(skill_docs) == 20  # V0.8 IMP-065 新增 lwa-import-git
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
        "lwa-relocate-workspace",
    ):
        assert expected in names, f"缺少 skill：{expected}"


def test_bundled_skills_have_discoverable_frontmatter() -> None:
    """内置 Skill 必须提供 Agent Skills 可发现的最小 YAML 元数据。"""
    skills_root = Path(__file__).parents[1] / "src/local_webpage_access/skills"
    skill_docs = sorted(skills_root.glob("lwa-*/SKILL.md"))
    assert len(skill_docs) == 20

    for skill_doc in skill_docs:
        text = skill_doc.read_text(encoding="utf-8")
        assert text.startswith("---\n"), f"{skill_doc.parent.name} 缺少 YAML frontmatter"
        _, frontmatter, body = text.split("---", 2)
        metadata = yaml.safe_load(frontmatter)
        assert set(metadata) == {"name", "description"}
        assert metadata["name"] == skill_doc.parent.name
        assert isinstance(metadata["description"], str)
        assert metadata["description"].strip()
        assert body.lstrip().startswith(f"# {skill_doc.parent.name}\n")


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="IMP-036：CLI init 正式支持不含 Windows 原生",
)
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


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="IMP-036：CLI init 正式支持不含 Windows 原生",
)
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
    result = runner.invoke(app, ["init", "--workspace", str(ws), "--full", "--yes"])
    assert result.exit_code == 2, result.output
    assert captured.get("workspace_root") == ws.resolve()
    assert captured.get("yes") is True


# ---- CHK-224#2：init 收尾自启引导异常兜底 ------------------------------------


def test_cli_init_autostart_offer_exception_does_not_break_init(
    tmp_path: Path, monkeypatch
) -> None:
    """引导探测抛任意异常（AutostartError/OSError）不阻断 init 主流程。"""
    from typer.testing import CliRunner

    from local_webpage_access import autostart as asm
    from local_webpage_access.cli import app

    def boom(*args, **kwargs):
        raise asm.AutostartError("平台异常：unit 目录不可写")

    monkeypatch.setattr(asm, "maybe_offer_autostart_install", boom)
    ws = tmp_path / "offer-broken-ws"
    result = CliRunner().invoke(app, ["init", "--workspace", str(ws)])
    assert result.exit_code == 0, result.output
    assert "已初始化工作区" in result.output
    assert (ws / "local-web.yml").is_file()


def test_cli_init_autostart_offer_generic_exception_swallowed(tmp_path: Path, monkeypatch) -> None:
    """非 LwaError 的普通异常（如 OSError）同样兜底，不冒栈中断。"""
    from typer.testing import CliRunner

    from local_webpage_access import autostart as asm
    from local_webpage_access.cli import app

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(asm, "maybe_offer_autostart_install", boom)
    ws = tmp_path / "offer-oserror-ws"
    result = CliRunner().invoke(app, ["init", "--workspace", str(ws)])
    assert result.exit_code == 0, result.output


def test_cli_init_autostart_offer_failed_install_exits_nonzero(tmp_path: Path, monkeypatch) -> None:
    """引导已执行但启用失败（attempted=True, ok=False）仍非零退出且不被吞。"""
    from typer.testing import CliRunner

    from local_webpage_access import autostart as asm
    from local_webpage_access.autostart import AutostartOfferResult
    from local_webpage_access.cli import app

    offer = AutostartOfferResult(messages=["执行：lwa autostart install"], attempted=True, ok=False)
    monkeypatch.setattr(asm, "maybe_offer_autostart_install", lambda *a, **k: offer)
    ws = tmp_path / "offer-failed-ws"
    result = CliRunner().invoke(app, ["init", "--workspace", str(ws)])
    assert result.exit_code == 1
    assert "autostart check" in result.output
