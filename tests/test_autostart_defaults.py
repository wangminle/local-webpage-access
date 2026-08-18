"""IMP-061 自启安装默认化与首次引导测试。

覆盖：

* 061.01 缺省值反转（with_caddy=None 自动纳入 caddy；linger=None 默认尝试；
  旧命令 ``--with-caddy`` / ``--linger`` 行为不变）；
* 061.02 init/setup 首次引导（Linux systemd 询问；非 TTY 零阻塞；已装跳过；
  拒绝不安装）；
* 061.03 运行模式标注（systemd/launchd 监管 vs 裸进程）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_webpage_access import autostart as asm


@pytest.fixture()
def ws_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Linux + systemd 假环境 + 已初始化工作区（复用 test_autostart 夹具逻辑）。"""
    from local_webpage_access.config import Config
    from local_webpage_access.init_workspace import init_workspace
    from local_webpage_access.paths import Workspace
    from tests.test_autostart import _fake_runner

    root = tmp_path / "ws"
    init_workspace(root)
    ws = Workspace(root)
    monkeypatch.setattr(asm, "detect_platform", lambda: asm.PLATFORM_LINUX)
    monkeypatch.setattr(asm, "systemd_available", lambda: True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = Config(staticGateway="caddy", managerEnabled=True)
    return root, ws, config, _fake_runner()


# ---- 061.01 缺省值反转 -------------------------------------------------------


def test_resolve_with_caddy_tri_state() -> None:
    from local_webpage_access.config import Config

    cfg_caddy = Config(staticGateway="caddy")
    cfg_builtin = Config(staticGateway="builtin")
    # None → caddy 在用即纳入（缺省安全）
    assert asm.resolve_with_caddy(cfg_caddy, None) is True
    assert asm.resolve_with_caddy(cfg_builtin, None) is False
    # 显式旗标语义不变
    assert asm.resolve_with_caddy(cfg_caddy, True) is True
    assert asm.resolve_with_caddy(cfg_caddy, False) is False


def test_install_default_includes_gateway(ws_env, monkeypatch) -> None:
    """不带任何 flag 在 caddy 环境装出 gateway 单元（验收 2）。"""
    _root, ws, config, runner = ws_env
    result = asm.install(ws, config, with_caddy=None, enable=False, runner=runner)
    assert "gateway" in result.services


def test_install_no_with_caddy_excludes_gateway(ws_env) -> None:
    """`--no-with-caddy` 仍可显式排除（验收）。"""
    _root, ws, config, runner = ws_env
    result = asm.install(ws, config, with_caddy=False, enable=False, runner=runner)
    assert "gateway" not in result.services


def test_install_builtin_gateway_not_applicable(ws_env) -> None:
    from local_webpage_access.config import Config

    _root, ws, _config, runner = ws_env
    result = asm.install(
        ws, Config(staticGateway="builtin"), with_caddy=None, enable=False, runner=runner
    )
    assert "gateway" not in result.services


def test_install_old_with_caddy_flag_unchanged(ws_env) -> None:
    """旧命令 `--with-caddy` 行为不变（gateway 纳入）。"""
    _root, ws, config, runner = ws_env
    result = asm.install(ws, config, with_caddy=True, enable=False, runner=runner)
    assert "gateway" in result.services


def test_install_default_linger_attempted(ws_env) -> None:
    """linger=None：Linux 默认尝试 enable-linger（不再要求 --linger）。"""
    _root, ws, config, runner = ws_env
    result = asm.install(ws, config, with_caddy=False, enable=False, linger=None, runner=runner)
    assert result.linger_attempted is True


def test_install_no_linger_skips(ws_env) -> None:
    _root, ws, config, runner = ws_env
    result = asm.install(ws, config, with_caddy=False, enable=False, linger=False, runner=runner)
    assert result.linger_attempted is False


def test_install_old_linger_flag_unchanged(ws_env) -> None:
    _root, ws, config, runner = ws_env
    result = asm.install(ws, config, with_caddy=False, enable=False, linger=True, runner=runner)
    assert result.linger_attempted is True


# ---- 061.02 首次引导 ---------------------------------------------------------


def test_offer_skips_non_linux(tmp_path, monkeypatch) -> None:
    """WSL / macOS 行为不变：不引导（IMP-030 语义）。"""
    from local_webpage_access.config import Config
    from local_webpage_access.paths import Workspace

    ws = Workspace(tmp_path / "ws")
    for plat in (asm.PLATFORM_MACOS, asm.PLATFORM_WSL):
        monkeypatch.setattr(asm, "detect_platform", lambda p=plat: p)
        result = asm.maybe_offer_autostart_install(ws, Config())
        assert result.attempted is False
        assert result.messages == []


def test_offer_non_tty_prints_suggestion_zero_block(ws_env, monkeypatch) -> None:
    """非交互（管道/CI）：不挂起、不装，仅打印建议命令（验收 3）。"""
    _root, ws, config, _runner = ws_env
    monkeypatch.setattr(asm, "_stdin_is_interactive", lambda: False)
    result = asm.maybe_offer_autostart_install(ws, config)
    assert result.attempted is False
    assert any("lwa autostart install" in m for m in result.messages)
    assert any("非交互终端" in m for m in result.messages)


def test_offer_interactive_confirm_installs(ws_env, monkeypatch) -> None:
    """交互 TTY 确认 → 安装成功且 check 目标含 gateway 与 linger（验收 1 主路径）。"""
    _root, ws, config, runner = ws_env
    monkeypatch.setattr(asm, "_stdin_is_interactive", lambda: True)
    result = asm.maybe_offer_autostart_install(
        ws, config, confirm=lambda prompt: True, runner=runner
    )
    assert result.attempted is True
    assert result.ok is True
    assert set(result.services) >= {"daemon", "manager", "gateway"}


def test_offer_interactive_decline_keeps_bare(ws_env, monkeypatch) -> None:
    _root, ws, config, _runner = ws_env
    monkeypatch.setattr(asm, "_stdin_is_interactive", lambda: True)
    result = asm.maybe_offer_autostart_install(ws, config, confirm=lambda prompt: False)
    assert result.attempted is False
    assert any("已跳过" in m for m in result.messages)


def test_offer_skips_when_already_installed(ws_env, monkeypatch) -> None:
    _root, ws, config, runner = ws_env
    monkeypatch.setattr(asm, "_stdin_is_interactive", lambda: True)
    # 先装一份
    asm.install(ws, config, with_caddy=False, enable=False, runner=runner)
    result = asm.maybe_offer_autostart_install(
        ws, config, confirm=lambda prompt: pytest.fail("已装时不应再询问"), runner=runner
    )
    assert result.attempted is False
    assert any("已安装" in m for m in result.messages)


def test_offer_linux_no_systemd_skips(tmp_path, monkeypatch) -> None:
    from local_webpage_access.config import Config
    from local_webpage_access.paths import Workspace

    monkeypatch.setattr(asm, "detect_platform", lambda: asm.PLATFORM_LINUX)
    monkeypatch.setattr(asm, "systemd_available", lambda: False)
    ws = Workspace(tmp_path / "ws")
    result = asm.maybe_offer_autostart_install(ws, Config())
    assert result.attempted is False
    assert result.messages == []


# ---- 061.03 运行模式标注 -----------------------------------------------------


def test_supervision_mode_bare_without_units(ws_env, monkeypatch) -> None:
    monkeypatch.setattr(asm, "_stdin_is_interactive", lambda: False)
    assert asm.service_supervision_mode("daemon") == asm.SERVICE_MODE_BARE


def test_supervision_mode_systemd_when_managed(ws_env) -> None:
    _root, ws, _config, runner = ws_env
    backend = asm.select_backend()
    path = backend.unit_path("daemon")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[Service]\nType=simple\nExecStart=/py -m local_webpage_access.daemon --workspace /x\n",
        encoding="utf-8",
    )
    # runner 返回 is-active 成功即视为已加载（_fake_runner 默认）
    assert asm.service_supervision_mode("daemon", runner=runner) == asm.SERVICE_MODE_SYSTEMD


def test_supervision_mode_launchd_on_macos(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(asm, "detect_platform", lambda: asm.PLATFORM_MACOS)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    backend = asm.MacLaunchdBackend()
    backend.write_unit(
        "daemon",
        backend.render("daemon", python_exe="/py", workspace_root=tmp_path, keep_alive=True),
    )
    from tests.test_autostart import _fake_runner

    assert asm.service_supervision_mode("daemon", runner=_fake_runner()) == asm.SERVICE_MODE_LAUNCHD
