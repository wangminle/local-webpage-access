"""IMP-030 跨平台自启动（``lwa autostart``）单测。

覆盖：平台识别（含 WSL）、前台监管单元生成（macOS plist / systemd unit）、旧
detached 启动器识别、安装/启用/协调卸载、完备性检查、WSL 唤醒脚本、网关前台入口。
非 macOS 平台用 monkeypatch + 假 runner 验证调用序列，不依赖真实 launchctl/systemd。
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from local_webpage_access import autostart as asm


# ---- 平台识别 --------------------------------------------------------------


def test_detect_platform_returns_known_value() -> None:
    assert asm.detect_platform() in {"macos", "linux", "wsl", "windows", "unknown"}


def test_is_wsl_via_proc_version(monkeypatch) -> None:
    import local_webpage_access.platform_detect as pd

    monkeypatch.setattr(pd.platform, "system", lambda: "Linux")
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    # _read_proc_version 真实实现会 .lower()，故 mock 给小写内容
    monkeypatch.setattr(pd, "_read_proc_version", lambda: "linux version ... microsoft")
    assert pd.is_wsl() is True
    monkeypatch.setattr(pd, "_read_proc_version", lambda: "linux version 6.x generic")
    assert pd.is_wsl() is False


def test_wsl_distro_from_env(monkeypatch) -> None:
    import local_webpage_access.platform_detect as pd

    monkeypatch.setattr(pd, "is_wsl", lambda: True)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")
    assert pd.wsl_distro() == "Ubuntu-24.04"


# ---- 服务选择 --------------------------------------------------------------


def test_select_services_daemon_always() -> None:
    from local_webpage_access.config import Config

    # builtin 网关 + manager 关闭 → 仅 daemon
    services = asm.select_services(
        Config(staticGateway="builtin", managerEnabled=False), with_caddy=True
    )
    assert services == ["daemon"]


def test_select_services_manager_and_gateway() -> None:
    from local_webpage_access.config import Config

    services = asm.select_services(
        Config(staticGateway="caddy", managerEnabled=True), with_caddy=True
    )
    assert services == ["daemon", "manager", "gateway"]


def test_select_services_gateway_only_when_caddy_and_flag() -> None:
    from local_webpage_access.config import Config

    # with_caddy=False → 不含 gateway
    assert "gateway" not in asm.select_services(
        Config(staticGateway="caddy", managerEnabled=True), with_caddy=False
    )
    # staticGateway=builtin → 即使 with_caddy 也不含 gateway
    assert "gateway" not in asm.select_services(
        Config(staticGateway="builtin", managerEnabled=True), with_caddy=True
    )


# ---- 单元生成 --------------------------------------------------------------


def test_build_launchd_plist_foreground_keepalive_path(tmp_path) -> None:
    plist = asm.build_launchd_plist(
        "daemon", python_exe="/usr/local/bin/python3", workspace_root=tmp_path
    )
    assert plist["Label"] == "com.fenix.lwa.daemon"
    assert plist["RunAtLoad"] is True
    # 前台入口（--workspace），不是 detached `on`
    assert plist["ProgramArguments"] == [
        "/usr/local/bin/python3",
        "-m",
        "local_webpage_access.daemon",
        "--workspace",
        str(tmp_path),
    ]
    assert "KeepAlive" in plist  # BUG-138 修复：前台监管崩溃即拉起
    assert "PATH" in plist["EnvironmentVariables"]  # BUG-139 修复
    assert "/opt/homebrew/bin" in plist["EnvironmentVariables"]["PATH"]


def test_build_systemd_unit_foreground_restart(tmp_path) -> None:
    unit = asm.build_systemd_unit(
        "manager", python_exe="/usr/bin/python3", workspace_root=tmp_path
    )
    assert "Type=simple" in unit
    assert "Restart=on-failure" in unit
    assert "RestartSec=5" in unit
    assert "After=network-online.target lwa-daemon.service" in unit  # manager 依赖 daemon
    assert "--workspace" in unit
    assert " on" not in unit.split("ExecStart=", 1)[1].splitlines()[0]
    # daemon 单元的 After 不含 lwa-daemon
    daemon_unit = asm.build_systemd_unit(
        "daemon", python_exe="/usr/bin/python3", workspace_root=tmp_path
    )
    assert "After=network-online.target" in daemon_unit


def test_build_systemd_unit_quotes_path_with_spaces(tmp_path, monkeypatch) -> None:
    """BUG-174：systemd Environment=PATH 须整体加引号，否则含空格目录被截断。"""
    monkeypatch.setattr(
        asm, "_build_path_env", lambda *a, **k: "/usr/bin:/mnt/c/Program Files/app"
    )
    unit = asm.build_systemd_unit(
        "daemon", python_exe="/usr/bin/python3", workspace_root=tmp_path
    )
    env_lines = [ln for ln in unit.splitlines() if ln.startswith("Environment=")]
    assert env_lines, "unit 缺少 Environment= 行"
    line = env_lines[0].rstrip()
    # 整体加引号：Environment="PATH=..."
    assert line.startswith('Environment="PATH='), line
    assert line.endswith('"'), line
    # 含空格目录完整保留在引号内（未被 systemd 按空格截断）
    assert "/mnt/c/Program Files/app" in line


def test_is_legacy_detection() -> None:
    # 旧 detached 启动器
    assert asm.is_legacy_program_arguments(
        ["/py", "-m", "local_webpage_access", "daemon", "on"]
    )
    # 前台入口不是旧配置
    assert not asm.is_legacy_program_arguments(
        ["/py", "-m", "local_webpage_access.daemon", "--workspace", "/x"]
    )
    # systemd ExecStart 字符串
    assert asm.is_legacy_exec_start("/py -m local_webpage_access manager on")
    assert not asm.is_legacy_exec_start(
        "/py -m local_webpage_access.manager_service --workspace /x"
    )


# ---- 后端 render + legacy 读回 ---------------------------------------------


def test_mac_backend_render_roundtrip(tmp_path) -> None:
    backend = asm.MacLaunchdBackend()
    data = plistlib.loads(
        backend.render(
            "daemon", python_exe="/py", workspace_root=tmp_path, keep_alive=True
        )
    )
    assert data["ProgramArguments"][-2:] == ["--workspace", str(tmp_path)]


def test_systemd_backend_legacy_detect(tmp_path, monkeypatch) -> None:
    backend = asm.SystemdUserBackend()
    unit_path = tmp_path / "lwa-daemon.service"
    unit_path.write_text(
        "[Service]\nType=simple\nExecStart=/py -m local_webpage_access daemon on\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(backend, "unit_path", lambda name: unit_path)
    assert backend.is_legacy("daemon") is True


# ---- 后端选择 --------------------------------------------------------------


def test_select_backend_macos(monkeypatch) -> None:
    monkeypatch.setattr(asm, "detect_platform", lambda: "macos")
    assert isinstance(asm.select_backend(), asm.MacLaunchdBackend)


def test_select_backend_linux_systemd(monkeypatch) -> None:
    monkeypatch.setattr(asm, "detect_platform", lambda: "linux")
    monkeypatch.setattr(asm, "systemd_available", lambda: True)
    assert isinstance(asm.select_backend(), asm.SystemdUserBackend)


def test_select_backend_unsupported(monkeypatch) -> None:
    monkeypatch.setattr(asm, "detect_platform", lambda: "windows")
    with pytest.raises(asm.AutostartError):
        asm.select_backend()


def test_select_backend_linux_no_systemd(monkeypatch) -> None:
    monkeypatch.setattr(asm, "detect_platform", lambda: "linux")
    monkeypatch.setattr(asm, "systemd_available", lambda: False)
    with pytest.raises(asm.AutostartError):
        asm.select_backend()


# ---- 安装（macOS / systemd）-----------------------------------------------


def _fake_runner(record=None):
    """假 runner：默认成功，并返回 is-enabled/print/MainPID 所需的最小 stdout。

    跟踪 macOS/linux 的 disable 状态，使 disable 后 is_enabled 反映持久停用（BUG-152/153）。
    """
    disabled: set[str] = set()

    def _run(cmd, **kwargs):
        if record is not None:
            record.append(list(cmd))
        joined = " ".join(cmd)
        # 记录持久 disable
        if "disable-linger" in joined:
            pass
        elif len(cmd) >= 2 and cmd[0] == "launchctl" and cmd[1] == "disable":
            # launchctl disable gui/UID/label
            disabled.add(cmd[2].rsplit("/", 1)[-1] if len(cmd) > 2 else "")
        elif len(cmd) >= 2 and cmd[0] == "launchctl" and cmd[1] == "enable":
            label = cmd[2].rsplit("/", 1)[-1] if len(cmd) > 2 else ""
            disabled.discard(label)
        elif "systemctl" in joined and "disable" in joined and "--now" in joined:
            for part in cmd:
                if part.endswith(".service"):
                    disabled.add(part)
        elif "systemctl" in joined and "enable" in joined and "--now" in joined:
            for part in cmd:
                if part.endswith(".service"):
                    disabled.discard(part)

        stdout = ""
        rc = 0
        if "is-enabled" in joined:
            unit = next((p for p in cmd if p.endswith(".service")), "")
            if unit in disabled:
                rc, stdout = 1, "disabled"
            else:
                stdout = "enabled"
        elif "print-disabled" in joined:
            lines = "\n".join(f'\t"{lab}" => disabled' for lab in sorted(disabled) if lab)
            stdout = f"disabled services = {{\n{lines}\n}}\n"
        elif "print" in joined:
            label = cmd[-1].rsplit("/", 1)[-1] if cmd else ""
            if label in disabled:
                rc = 1  # 已 bootout / 未加载
            else:
                stdout = "pid = 1\ndisabled = false\n"
        elif "is-active" in joined:
            unit = next((p for p in cmd if p.endswith(".service")), "")
            if unit in disabled:
                rc, stdout = 3, "inactive"
            else:
                stdout = "active"
        elif "MainPID" in joined or ("show" in joined and "-p" in joined):
            unit = next((p for p in cmd if p.endswith(".service")), "")
            stdout = "0" if unit in disabled else "1"
        return CompletedProcess(args=cmd, returncode=rc, stdout=stdout, stderr="")

    return _run


def _make_ws(tmp_path) -> tuple[Path, object, object]:
    from local_webpage_access.config import Config
    from local_webpage_access.init_workspace import init_workspace
    from local_webpage_access.paths import Workspace

    root = tmp_path / "ws"
    init_workspace(root)
    ws = Workspace(root)
    ws.ensure_workspace_dirs()
    return root, ws, Config(staticGateway="caddy", managerEnabled=True)


def test_install_macos_writes_foreground_plists(tmp_path, monkeypatch) -> None:
    root, ws, config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "macos")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    result = asm.install(
        ws, config, with_caddy=True, enable=False, python_exe="/usr/local/bin/python3"
    )
    names = [u.name for u in result.written]
    assert names == ["daemon", "manager", "gateway"]
    daemon_plist = tmp_path / "home" / "Library" / "LaunchAgents" / "com.fenix.lwa.daemon.plist"
    assert daemon_plist.is_file()
    data = plistlib.loads(daemon_plist.read_bytes())
    assert data["ProgramArguments"][1:4] == ["-m", "local_webpage_access.daemon", "--workspace"]
    assert "KeepAlive" in data
    # --no-enable 只生成单元，不得预置 daemon enabled（BUG-160）
    from local_webpage_access import daemon as daemon_mod

    state = daemon_mod.read_state(ws)
    assert state is None or state.enabled is False


def test_install_systemd_enables_via_systemctl(tmp_path, monkeypatch) -> None:
    root, ws, config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "linux")
    monkeypatch.setattr(asm, "systemd_available", lambda: True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    record: list[list[str]] = []
    asm.install(
        ws, config, with_caddy=False, enable=True, runner=_fake_runner(record)
    )
    unit = tmp_path / "home" / ".config" / "systemd" / "user" / "lwa-daemon.service"
    assert unit.is_file()
    assert "--workspace" in unit.read_text(encoding="utf-8")
    # enable 应调用 daemon-reload + enable --now
    joined = [" ".join(c) for c in record]
    assert any("daemon-reload" in c for c in joined)
    assert any("enable" in c and "lwa-daemon.service" in c for c in joined)


# ---- 协调卸载（030.b）------------------------------------------------------


def test_coordinated_disable_when_loaded(tmp_path, monkeypatch) -> None:
    root, ws, config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "macos")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # 先写一个 plist（已安装）
    backend = asm.MacLaunchdBackend()
    plist = backend.render("daemon", python_exe="/py", workspace_root=root)
    backend.write_unit("daemon", plist)
    label = asm.launchd_label("daemon")
    state = {"disabled": False}

    def runner(cmd, **kwargs):
        joined = " ".join(cmd)
        if "bootout" in joined or (cmd[:2] == ["launchctl", "disable"]):
            state["disabled"] = True
        if "print-disabled" in joined:
            body = f'\t"{label}" => disabled\n' if state["disabled"] else ""
            return CompletedProcess(
                args=cmd, returncode=0,
                stdout=f"disabled services = {{\n{body}}}\n", stderr="",
            )
        if "print" in joined:
            if state["disabled"]:
                return CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")
            return CompletedProcess(
                args=cmd, returncode=0, stdout="pid = 1\ndisabled = false\n", stderr=""
            )
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    res = asm.coordinated_disable(ws, "daemon", runner=runner)
    assert res.note is not None and "停用" in res.note
    assert res.ok is True


def test_coordinated_disable_reports_failure(tmp_path, monkeypatch) -> None:
    """disable 真实失败时 coordinated_disable 如实报告 ok=False（BUG-148）。"""
    root, ws, _config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "macos")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    backend = asm.MacLaunchdBackend()
    backend.write_unit("daemon", backend.render("daemon", python_exe="/py", workspace_root=root))

    def runner(cmd, **kwargs):
        # launchctl print（is_loaded）返回 0 → 已加载；但 disable/bootout 返回非零 → 卸载失败
        rc = 0 if "print" in cmd else 1
        return CompletedProcess(args=cmd, returncode=rc, stdout="", stderr="boom")

    res = asm.coordinated_disable(ws, "daemon", runner=runner)
    assert res.ok is False
    assert res.note is not None and "失败" in res.note


def test_coordinated_disable_no_unit(tmp_path, monkeypatch) -> None:
    root, ws, _config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "macos")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    res = asm.coordinated_disable(ws, "daemon")
    assert res.note is None


# ---- coordinated_restart（BUG-191）-----------------------------------------


def test_coordinated_restart_managed_when_loaded(tmp_path, monkeypatch) -> None:
    """BUG-191：单元已加载/启用时 coordinated_restart 交监督器重启，managed=True。"""
    root, ws, _config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "macos")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    backend = asm.MacLaunchdBackend()
    backend.write_unit(
        "daemon", backend.render("daemon", python_exe="/py", workspace_root=root)
    )
    record: list[list[str]] = []
    res = asm.coordinated_restart(ws, "daemon", runner=_fake_runner(record))
    assert res.managed is True
    assert res.ok is True
    assert any(c[:3] == ["launchctl", "kickstart", "-k"] for c in record)


def test_coordinated_restart_no_unit(tmp_path, monkeypatch) -> None:
    """无单元文件时 managed=False（调用方按 stop+start 处理）。"""
    root, ws, _config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "macos")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    res = asm.coordinated_restart(ws, "daemon")
    assert res.managed is False
    assert res.note is None


def test_coordinated_restart_failure_falls_back(tmp_path, monkeypatch) -> None:
    """BUG-191：监督器重启失败时 managed=False 回退 stop+start，并如实报 ok=False。"""
    root, ws, _config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "macos")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    backend = asm.MacLaunchdBackend()
    backend.write_unit(
        "daemon", backend.render("daemon", python_exe="/py", workspace_root=root)
    )

    def runner(cmd, **kwargs):
        joined = " ".join(cmd)
        if "kickstart" in joined:
            return CompletedProcess(args=cmd, returncode=1, stdout="", stderr="boom")
        if "print-disabled" in joined:
            return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        if "print" in cmd:
            return CompletedProcess(
                args=cmd, returncode=0, stdout="pid = 1\ndisabled = false\n", stderr=""
            )
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    res = asm.coordinated_restart(ws, "daemon", runner=runner)
    assert res.managed is False
    assert res.ok is False
    assert res.note is not None and "失败" in res.note


def test_mac_launchd_restart_uses_kickstart() -> None:
    """BUG-191：MacLaunchdBackend.restart 用 launchctl kickstart -k（监督下单一进程）。"""
    backend = asm.MacLaunchdBackend()
    record: list[list[str]] = []
    _outcomes, ok = backend.restart("daemon", _fake_runner(record))
    assert ok is True
    kicks = [c for c in record if c[:3] == ["launchctl", "kickstart", "-k"]]
    assert len(kicks) == 1


def test_systemd_restart_uses_systemctl_restart(monkeypatch) -> None:
    """BUG-191：SystemdUserBackend.restart 用 systemctl --user restart。"""
    monkeypatch.setattr(asm, "detect_platform", lambda: "linux")
    backend = asm.SystemdUserBackend()
    record: list[list[str]] = []
    _outcomes, ok = backend.restart("daemon", _fake_runner(record))
    assert ok is True
    restarts = [c for c in record if c[:2] == ["systemctl", "--user"] and "restart" in c]
    assert len(restarts) == 1


# ---- 完备性检查 ------------------------------------------------------------


def test_run_check_legacy_unit_reports_fail(tmp_path, monkeypatch) -> None:
    root, ws, config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "macos")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # 写一个旧 detached plist
    backend = asm.MacLaunchdBackend()
    legacy = {
        "Label": "com.fenix.lwa.daemon",
        "ProgramArguments": ["/py", "-m", "local_webpage_access", "daemon", "on"],
    }
    backend.write_unit("daemon", plistlib.dumps(legacy, fmt=plistlib.FMT_XML))
    report = asm.run_check(ws, config, runner=_fake_runner())
    # 旧 detached 启动器 → 身份检查 fail（category=unit）
    units = [i for i in report.items if i.category == "unit" and i.name == "daemon"]
    assert units and units[0].status == "fail"
    assert "repair" in units[0].fix


def test_run_check_foreground_unit_form_ok(tmp_path, monkeypatch) -> None:
    root, ws, config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "macos")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    backend = asm.MacLaunchdBackend()
    for name in ("daemon", "manager"):
        backend.write_unit(
            name, backend.render(name, python_exe="/py", workspace_root=root)
        )
    report = asm.run_check(ws, config, runner=_fake_runner())
    units = [i for i in report.items if i.category == "unit" and i.name in ("daemon", "manager")]
    assert all(i.status == "ok" for i in units)


# ---- WSL -------------------------------------------------------------------


def test_render_wsl_windows_script() -> None:
    from local_webpage_access.config import Config
    from local_webpage_access.paths import Workspace

    script = asm.render_wsl_windows_script(Workspace("/tmp/x"), Config())
    assert "wsl.exe" in script
    # BUG-150：长驻保活，而非 /bin/true 立即退出
    assert "sleep infinity" in script
    assert "/bin/true" not in script
    # 同时拉起 daemon / manager / gateway（未安装时 2>/dev/null 忽略）
    assert "lwa-daemon.service" in script
    assert "lwa-manager.service" in script
    assert "lwa-gateway.service" in script


# ---- manifest / 已安装服务集合（BUG-149）-----------------------------------


def test_install_writes_manifest_and_enable_uses_it(tmp_path, monkeypatch) -> None:
    """install 不带 --with-caddy 时 manifest=[daemon,manager]，enable 只操作这两个。"""
    root, ws, config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "linux")
    monkeypatch.setattr(asm, "systemd_available", lambda: True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    asm.install(ws, config, with_caddy=False, enable=False)
    assert asm.read_manifest(ws) == ["daemon", "manager"]
    # 实际安装集合不含 gateway
    assert asm.installed_services(ws) == ["daemon", "manager"]
    record: list[list[str]] = []
    op = asm.enable(ws, config, runner=_fake_runner(record))
    # enable 只对 daemon/manager 调用 systemctl，不碰 gateway
    assert not any("lwa-gateway" in " ".join(c) for c in record)
    assert op.success is True


def test_enable_failure_returns_unsuccessful(tmp_path, monkeypatch) -> None:
    """systemctl enable 失败时 OpResult.success=False（BUG-148）。"""
    root, ws, config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "linux")
    monkeypatch.setattr(asm, "systemd_available", lambda: True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    asm.install(ws, config, with_caddy=False, enable=False)

    def runner(cmd, **kwargs):
        # systemctl enable --now 失败；is-active（is_loaded）也失败 → success False
        rc = 1
        return CompletedProcess(args=cmd, returncode=rc, stdout="", stderr="enable failed")

    op = asm.enable(ws, config, runner=runner)
    assert op.success is False


def test_check_workspace_mismatch_fails(tmp_path, monkeypatch) -> None:
    """单元 --workspace 与当前工作区不一致 → check fail（杜绝假绿，BUG-151）。"""
    root, ws, config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "macos")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    backend = asm.MacLaunchdBackend()
    # 写一个指向"别的"工作区的 daemon plist
    backend.write_unit(
        "daemon",
        backend.render("daemon", python_exe="/py", workspace_root=tmp_path / "other"),
    )
    report = asm.run_check(ws, config, runner=_fake_runner())
    units = [i for i in report.items if i.category == "unit" and i.name == "daemon"]
    assert units and units[0].status == "fail"
    assert "工作区" in units[0].message or "repair" in units[0].fix


def test_mac_disable_runs_launchctl_disable(tmp_path, monkeypatch) -> None:
    """macOS disable 调用 launchctl disable（持久化，BUG：disable 不持久）。"""
    monkeypatch.setattr(asm, "detect_platform", lambda: "macos")
    backend = asm.MacLaunchdBackend()
    record: list[list[str]] = []
    backend.disable("daemon", runner=_fake_runner(record))
    joined = [" ".join(c) for c in record]
    assert any("launchctl" in c and "disable" in c for c in joined)
    assert any("launchctl" in c and "bootout" in c for c in joined)


def test_uninstall_purge_linger_calls_disable_linger(tmp_path, monkeypatch) -> None:
    """uninstall --purge-linger 真正调用 loginctl disable-linger（BUG-149）。"""
    root, ws, config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "linux")
    monkeypatch.setattr(asm, "systemd_available", lambda: True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    asm.install(ws, config, with_caddy=False, enable=False)
    record: list[list[str]] = []
    asm.uninstall(ws, config, purge_linger=True, runner=_fake_runner(record))
    joined = [" ".join(c) for c in record]
    assert any("disable-linger" in c for c in joined)
    # manifest 已清除
    assert asm.read_manifest(ws) is None


def test_daemon_main_writes_own_pid(tmp_path, monkeypatch) -> None:
    """前台 daemon 入口抢锁后回写自身 pid（BUG-146），使 is_running 可识别。"""
    import local_webpage_access.daemon as dm

    root, ws, config = _make_ws(tmp_path)
    # 模拟 _main 抢锁后的 pid 回写逻辑（直接调用 write_state 验证字段）
    dm.write_state(
        ws, dm.DaemonState(enabled=True, pid=12345, started_at="2026-07-16T00:00:00", poll_interval=5.0)
    )
    state = dm.read_state(ws)
    assert state is not None and state.enabled is True and state.pid == 12345


# ---- 网关前台入口 ----------------------------------------------------------


def test_gateway_foreground_non_caddy_returns_2(tmp_path) -> None:
    from local_webpage_access.config import Config
    from local_webpage_access.gateway_service import run_gateway_foreground
    from local_webpage_access.paths import Workspace

    ws = Workspace(tmp_path)
    rc = run_gateway_foreground(ws, Config(staticGateway="builtin"))
    assert rc == 2


# ---- CLI -------------------------------------------------------------------


def test_cli_autostart_group_registered() -> None:
    from typer.testing import CliRunner

    from local_webpage_access.cli import app

    res = CliRunner().invoke(app, ["autostart", "--help"])
    assert res.exit_code == 0
    for cmd in ("install", "enable", "disable", "status", "check", "repair", "uninstall"):
        assert cmd in res.output


def test_cli_autostart_doctor_hints(tmp_path, monkeypatch) -> None:
    from local_webpage_access.init_workspace import init_workspace
    from typer.testing import CliRunner

    from local_webpage_access.cli import app

    init_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(app, ["autostart", "doctor-hints"])
    assert res.exit_code == 0
    assert "自启动" in res.output


# ---- BUG-142/143/144/146/147/149 失败路径覆盖（复审第二轮）-----------------


def test_enable_bootout_nonzero_not_failure(tmp_path, monkeypatch) -> None:
    """macOS 首次 enable 时 bootout 预期非零不应判为整体失败（BUG-142）。

    bootout 清理"可能不存在"的旧实例返回非零是预期行为；成败以执行后
    is_loaded + is_enabled 为准（bootstrap/enable 须成功）。
    """
    root, ws, config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "macos")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    asm.install(ws, config, with_caddy=False, enable=False)

    def runner(cmd, **kwargs):
        # 注意：pytest tmp 路径可能含 "bootout" 字样，必须按 argv 判定命令本身。
        if len(cmd) >= 2 and cmd[1] == "bootout":  # 旧实例不存在 → 预期非零
            return CompletedProcess(args=cmd, returncode=36, stdout="", stderr="no service")
        if len(cmd) >= 2 and cmd[1] == "print-disabled":
            return CompletedProcess(args=cmd, returncode=0, stdout="disabled services = {\n}\n", stderr="")
        if len(cmd) >= 2 and cmd[1] == "print":
            return CompletedProcess(
                args=cmd, returncode=0, stdout="pid = 1\ndisabled = false\n", stderr=""
            )
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    op = asm.enable(ws, config, runner=runner)
    assert op.success is True


def test_check_caddy_detects_foreign_2019(tmp_path, monkeypatch) -> None:
    """:2019 被外部进程占用（本工作区 gateway 无存活 pid）→ caddy 检查 fail（BUG-142）。"""
    root, ws, config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "macos")
    monkeypatch.setattr(
        asm.shutil, "which",
        lambda c: "/usr/local/bin/caddy" if c == "caddy" else None,
    )

    class _FakeSock:
        def __init__(self, *a, **k):
            pass

        def settimeout(self, t):
            pass

        def connect(self, addr):
            return None  # 模拟 :2019 可连（外部占用）

        def close(self):
            pass

    monkeypatch.setattr("socket.socket", lambda *a, **k: _FakeSock())
    item = asm._check_caddy(ws, config)
    assert item.status == "fail"
    assert "2019" in item.message


def test_check_caddy_no_conflict_when_ours(tmp_path, monkeypatch) -> None:
    """:2019 由本工作区存活 master 持有 → 不报外部冲突（BUG-142 反例）。"""
    from local_webpage_access import gateway_service as gs

    root, ws, config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "macos")
    monkeypatch.setattr(
        asm.shutil, "which",
        lambda c: "/usr/local/bin/caddy" if c == "caddy" else None,
    )
    # 本工作区 gateway 持有存活且身份匹配的 pid → _port_2019_foreign 返回 False
    gs.write_state(ws, gs.GatewayState(enabled=True, pid=999999))
    import local_webpage_access.daemon as dm

    monkeypatch.setattr(dm, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        dm, "pid_cmdline_contains",
        lambda pid, *needles: True,
    )
    item = asm._check_caddy(ws, config)
    assert item.status == "ok"


def test_check_docker_uses_runtime_field(tmp_path, monkeypatch) -> None:
    """_check_docker 按 runtime 字段判定容器实例（旧实现误用 kind，BUG-143）。"""
    from local_webpage_access.registry import Registry

    root, ws, _config = _make_ws(tmp_path)
    reg = Registry(ws.db_path)
    reg.open()
    try:
        reg.upsert_instance({
            "id": "dc1", "name": "dc", "version": "1",
            "kind": "python",              # 检测 kind，非 docker-compose
            "runtime": "docker-compose",   # 真实 runtime 字段
            "serving_mode": "container",
            "created_at": "2026-07-16T00:00:00",
            "updated_at": "2026-07-16T00:00:00",
        })
    finally:
        reg.close()
    # docker CLI 缺失 → 确定性 WARN；关键是"检测到了容器实例"，不再误报"无容器实例"
    monkeypatch.setattr(asm.shutil, "which", lambda c: None)
    item = asm._check_docker(ws)
    assert "无容器实例" not in item.message
    assert item.status == "warn"


def test_systemd_uninstall_reload_failure_fails(tmp_path, monkeypatch) -> None:
    """systemd uninstall 的 daemon-reload 失败应计入成败并收集其结果（BUG-144）。"""
    backend = asm.SystemdUserBackend()
    # 需有单元文件；disable 成功后才会删文件并 reload
    path = backend.unit_path("daemon")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[Service]\nExecStart=/py -m local_webpage_access.daemon --workspace /x\n", encoding="utf-8")

    def runner(cmd, **kwargs):
        joined = " ".join(cmd)
        if "daemon-reload" in joined:
            return CompletedProcess(args=cmd, returncode=1, stdout="", stderr="reload err")
        if "is-active" in joined:  # 未激活
            return CompletedProcess(args=cmd, returncode=3, stdout="", stderr="")
        if "is-enabled" in joined:  # 已持久停用
            return CompletedProcess(args=cmd, returncode=1, stdout="disabled", stderr="")
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    outcomes, ok = backend.uninstall("daemon", runner=runner)
    assert ok is False
    assert any("daemon-reload" in " ".join(o.cmd) for o in outcomes)


def test_migrate_detached_stop_failure_returns_false(tmp_path, monkeypatch) -> None:
    """detached daemon 在跑但 stop 失败 → 迁移返回 False（不再静默吞掉，BUG-146）。"""
    root, ws, config = _make_ws(tmp_path)
    import local_webpage_access.daemon as dm

    monkeypatch.setattr(dm, "is_running", lambda workspace: True)
    monkeypatch.setattr(dm, "stop_daemon", lambda workspace: False)
    assert asm._migrate_detached_for_supervision(ws, config, "daemon") is False
    # 无 detached 进程 → True
    monkeypatch.setattr(dm, "is_running", lambda workspace: False)
    assert asm._migrate_detached_for_supervision(ws, config, "daemon") is True
    # gateway 无需迁移 → 恒 True
    assert asm._migrate_detached_for_supervision(ws, config, "gateway") is True


def test_enable_migration_failure_makes_unsuccessful(tmp_path, monkeypatch) -> None:
    """迁移失败应使 enable 整体失败（BUG-146）。"""
    root, ws, config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "linux")
    monkeypatch.setattr(asm, "systemd_available", lambda: True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    asm.install(ws, config, with_caddy=False, enable=False)
    import local_webpage_access.daemon as dm

    monkeypatch.setattr(dm, "is_running", lambda workspace: True)
    monkeypatch.setattr(dm, "stop_daemon", lambda workspace: False)
    op = asm.enable(ws, config, runner=_fake_runner())  # enable 命令 rc0，但迁移失败
    assert op.success is False


def test_coordinated_disable_enabled_but_inactive(tmp_path, monkeypatch) -> None:
    """enabled 但 inactive 的单元也必须 disable（否则下次触发会拉回，BUG-147）。"""
    root, ws, _config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "linux")
    monkeypatch.setattr(asm, "systemd_available", lambda: True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    backend = asm.SystemdUserBackend()
    backend.write_unit("daemon", backend.render("daemon", python_exe="/py", workspace_root=root))
    record: list[list[str]] = []
    disabled = {"done": False}

    def runner(cmd, **kwargs):
        record.append(list(cmd))
        joined = " ".join(cmd)
        if "is-active" in joined:  # inactive
            return CompletedProcess(args=cmd, returncode=3, stdout="", stderr="")
        if "is-enabled" in joined:
            # 协调探测时仍 enabled；disable 命令之后变为 disabled（BUG-152）
            if disabled["done"]:
                return CompletedProcess(args=cmd, returncode=1, stdout="disabled", stderr="")
            return CompletedProcess(args=cmd, returncode=0, stdout="enabled", stderr="")
        if "disable" in joined and "--now" in joined:
            disabled["done"] = True
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    res = asm.coordinated_disable(ws, "daemon", runner=runner)
    assert res.note is not None  # 触发了 disable（旧实现会因 inactive 跳过）
    assert res.ok is True
    assert any("disable" in " ".join(c) for c in record)


def test_check_nothing_installed_is_fail(tmp_path, monkeypatch) -> None:
    """一个单元都没装 → overall=fail、exit≠0（非 warn/0，BUG-149）。"""
    root, ws, config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "macos")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    report = asm.run_check(ws, config, runner=_fake_runner())
    assert report.overall == "fail"
    assert any(
        i.category == "unit" and i.name == "install" and i.status == "fail"
        for i in report.items
    )
    assert report.exit_code != 0


def test_check_active_without_process_is_fail(tmp_path, monkeypatch) -> None:
    """单元 active 但无服务进程 → active 项 fail（非 warn，BUG-149）。"""
    root, ws, config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "macos")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    backend = asm.MacLaunchdBackend()
    for name in ("daemon", "manager"):
        backend.write_unit(name, backend.render(name, python_exe="/py", workspace_root=root))
    report = asm.run_check(ws, config, runner=_fake_runner())
    actives = [i for i in report.items if i.category == "active"]
    assert actives and all(i.status == "fail" for i in actives)


# ---- CLI 退出码（repair/install 启用失败不再假绿，BUG-147）-----------------


def _init_ws_for_cli(tmp_path, monkeypatch):
    from local_webpage_access.init_workspace import init_workspace

    init_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)


def test_cli_repair_enable_failure_exits_nonzero(tmp_path, monkeypatch) -> None:
    """repair 在 enable 真实失败时退出码非零、不打印"修复完成"（BUG-147）。"""
    from typer.testing import CliRunner

    from local_webpage_access.cli import app

    _init_ws_for_cli(tmp_path, monkeypatch)
    # 模拟 repair 返回 enable_ok=False（如迁移失败/bootstrap 未加载）
    monkeypatch.setattr(
        asm, "repair",
        lambda ws, config, **kw: (
            asm.InstallResult(
                platform="macos", services=["daemon"], enabled=True,
                enable_ok=False,
                enable_outcomes=[asm.CmdOutcome(["(migrate)", "daemon"], 1, "",
                                                 "detached daemon 未能停止")],
            ),
            ["重写单元"],
        ),
    )
    res = CliRunner().invoke(app, ["autostart", "repair"])
    assert res.exit_code == 1
    assert "修复完成" not in res.output


def test_cli_install_enable_failure_exits_nonzero(tmp_path, monkeypatch) -> None:
    """install 在 enable 真实失败（enable_ok=False）时退出码非零（BUG-147/142）。"""
    from typer.testing import CliRunner

    from local_webpage_access.cli import app

    _init_ws_for_cli(tmp_path, monkeypatch)
    monkeypatch.setattr(
        asm, "install",
        lambda ws, config, **kw: asm.InstallResult(
            platform="linux", services=["daemon", "manager"], enabled=True,
            enable_ok=False,
            enable_outcomes=[asm.CmdOutcome(["systemctl", "enable", "--now"], 1, "",
                                             "enable failed")],
        ),
    )
    res = CliRunner().invoke(app, ["autostart", "install"])
    assert res.exit_code == 1


def test_cli_daemon_off_blocks_when_disable_fails(tmp_path, monkeypatch) -> None:
    """daemon off 在自启动单元停用失败时阻断后续 stop（BUG-147）。"""
    from typer.testing import CliRunner

    from local_webpage_access.cli import app
    import local_webpage_access.cli.daemon as cli_daemon

    _init_ws_for_cli(tmp_path, monkeypatch)
    # 协调停用失败（ok=False）→ 不应调用 stop_daemon
    monkeypatch.setattr(
        cli_daemon, "coordinated_autostart_disable",
        lambda ws, name: ("⚠️ 停用失败", False),
    )
    stopped = {"called": False}
    import local_webpage_access.daemon as dm

    monkeypatch.setattr(dm, "stop_daemon", lambda ws: stopped.__setitem__("called", True) or True)
    res = CliRunner().invoke(app, ["daemon", "off"])
    assert res.exit_code == 1
    assert stopped["called"] is False  # 阻断了 stop


# ---- BUG-152～158 第三轮残留失败路径 ----------------------------------------


def test_systemd_enable_requires_enabled_and_command_ok(tmp_path, monkeypatch) -> None:
    """enable --now rc=1 即便 is-active 仍不得判成功（BUG-152）。"""
    backend = asm.SystemdUserBackend()

    def runner(cmd, **kwargs):
        joined = " ".join(cmd)
        if "enable" in joined and "--now" in joined:
            return CompletedProcess(args=cmd, returncode=1, stdout="", stderr="fail")
        if "is-active" in joined:
            return CompletedProcess(args=cmd, returncode=0, stdout="active", stderr="")
        if "is-enabled" in joined:
            return CompletedProcess(args=cmd, returncode=1, stdout="disabled", stderr="")
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    _outcomes, ok = backend.enable("daemon", runner=runner)
    assert ok is False


def test_systemd_disable_requires_not_enabled(tmp_path, monkeypatch) -> None:
    """disable --now 后仍 enabled → 不得判成功（BUG-152）。"""
    backend = asm.SystemdUserBackend()

    def runner(cmd, **kwargs):
        joined = " ".join(cmd)
        if "is-active" in joined:
            return CompletedProcess(args=cmd, returncode=3, stdout="inactive", stderr="")
        if "is-enabled" in joined:  # 仍 enabled
            return CompletedProcess(args=cmd, returncode=0, stdout="enabled", stderr="")
        if "disable" in joined:
            return CompletedProcess(args=cmd, returncode=1, stdout="", stderr="fail")
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    _outcomes, ok = backend.disable("daemon", runner=runner)
    assert ok is False


def test_macos_is_enabled_via_print_disabled_when_unloaded(tmp_path, monkeypatch) -> None:
    """未加载但未被持久 disable 的 LaunchAgent 应判 enabled（BUG-153）。"""
    backend = asm.MacLaunchdBackend()
    label = asm.launchd_label("daemon")

    def runner(cmd, **kwargs):
        joined = " ".join(cmd)
        if "print-disabled" in joined:
            # 不在 disabled 列表 → 默认启用
            return CompletedProcess(
                args=cmd, returncode=0,
                stdout="disabled services = {\n\t\"com.other\" => disabled\n}\n",
                stderr="",
            )
        if "print" in joined:
            return CompletedProcess(args=cmd, returncode=1, stdout="", stderr="not found")
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    assert backend.is_enabled("daemon", runner) is True

    def runner_disabled(cmd, **kwargs):
        joined = " ".join(cmd)
        if "print-disabled" in joined:
            return CompletedProcess(
                args=cmd, returncode=0,
                stdout=f'disabled services = {{\n\t"{label}" => disabled\n}}\n',
                stderr="",
            )
        if "print" in joined:
            return CompletedProcess(args=cmd, returncode=1, stdout="", stderr="not found")
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    assert backend.is_enabled("daemon", runner_disabled) is False


def test_coordinated_wrapper_exception_fail_closed(tmp_path, monkeypatch) -> None:
    """协调包装器遇异常须 ok=False，不得 fail-open 继续 stop（BUG-154）。"""
    from local_webpage_access.cli._common import coordinated_autostart_disable

    root, ws, _config = _make_ws(tmp_path)

    def boom(*_a, **_k):
        raise RuntimeError("launchctl exploded")

    monkeypatch.setattr(asm, "coordinated_disable", boom)
    note, ok = coordinated_autostart_disable(ws, "daemon")
    assert ok is False
    assert note is not None
    assert "异常" in note or "未知" in note


def test_check_missing_required_daemon_is_fail(tmp_path, monkeypatch) -> None:
    """只装 manager、缺 daemon → fail 而非 warn/exit0（BUG-155）。"""
    root, ws, config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "macos")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    backend = asm.MacLaunchdBackend()
    backend.write_unit(
        "manager",
        backend.render("manager", python_exe=sys.executable, workspace_root=root),
    )
    asm.write_manifest(ws, ["manager"])
    report = asm.run_check(ws, config, runner=_fake_runner())
    missing = [i for i in report.items if i.category == "unit" and i.name == "daemon"]
    assert missing and missing[0].status == "fail"
    assert report.overall == "fail"
    assert report.exit_code != 0


def test_check_per_unit_bad_python_and_missing_mainpid(tmp_path, monkeypatch) -> None:
    """逐单元解释器错误与 active 无 MainPID 应 fail（BUG-156）。"""
    root, ws, config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "macos")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    backend = asm.MacLaunchdBackend()
    # daemon 用当前解释器；manager 指向不存在的坏路径
    backend.write_unit(
        "daemon",
        backend.render("daemon", python_exe=sys.executable, workspace_root=root),
    )
    bad_py = str(tmp_path / "missing-python")
    backend.write_unit(
        "manager",
        backend.render("manager", python_exe=bad_py, workspace_root=root),
    )
    asm.write_manifest(ws, ["daemon", "manager"])

    def runner(cmd, **kwargs):
        joined = " ".join(cmd)
        if "print-disabled" in joined:
            return CompletedProcess(args=cmd, returncode=0, stdout="disabled services = {\n}\n", stderr="")
        if "print" in joined:
            # active 但无 pid= 行 → MainPID 缺失
            return CompletedProcess(args=cmd, returncode=0, stdout="disabled = false\n", stderr="")
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    import local_webpage_access.daemon as dm

    monkeypatch.setattr(dm, "is_running", lambda workspace: True)
    monkeypatch.setattr(
        "local_webpage_access.autostart._service_process_running",
        lambda ws, config, name: True,
    )
    report = asm.run_check(ws, config, runner=runner)
    interp = [i for i in report.items if i.category == "interpreter" and i.name == "manager"]
    assert interp and interp[0].status == "fail"
    actives = [i for i in report.items if i.category == "active"]
    assert actives and all(i.status == "fail" for i in actives)
    assert report.overall == "fail"


def test_check_unit_missing_path_env_fails(tmp_path, monkeypatch) -> None:
    """单元缺少 PATH 环境变量 → fail（BUG-156）。"""
    import plistlib

    root, ws, config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "macos")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    backend = asm.MacLaunchdBackend()
    raw = plistlib.loads(
        backend.render("daemon", python_exe=sys.executable, workspace_root=root)
    )
    del raw["EnvironmentVariables"]
    path = backend.unit_path("daemon")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(raw, fmt=plistlib.FMT_XML, sort_keys=False))
    asm.write_manifest(ws, ["daemon"])
    report = asm.run_check(ws, config, runner=_fake_runner())
    path_items = [i for i in report.items if i.category == "path" and i.name == "daemon"]
    assert path_items and path_items[0].status == "fail"


def test_port_2019_rejects_alive_but_wrong_identity(tmp_path, monkeypatch) -> None:
    """gateway.json PID 存活但非本工作区 Caddy → 仍视为外部占用（BUG-157）。"""
    from local_webpage_access import gateway_service as gs
    import local_webpage_access.daemon as dm

    root, ws, config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "macos")
    monkeypatch.setattr(
        asm.shutil, "which",
        lambda c: "/usr/local/bin/caddy" if c == "caddy" else None,
    )
    gs.write_state(ws, gs.GatewayState(enabled=True, pid=424242))
    monkeypatch.setattr(dm, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(dm, "pid_cmdline_contains", lambda pid, *needles: False)

    class _FakeSock:
        def settimeout(self, t):
            pass

        def connect(self, addr):
            return None

        def close(self):
            pass

    monkeypatch.setattr("socket.socket", lambda *a, **k: _FakeSock())
    assert asm._port_2019_foreign(ws) is True
    item = asm._check_caddy(ws, config)
    assert item.status == "fail"
    assert "2019" in item.message


def test_uninstall_keeps_manifest_and_unit_on_failure(tmp_path, monkeypatch) -> None:
    """uninstall 失败不得清 manifest / 不得在 disable 失败时删单元（BUG-158）。"""
    root, ws, config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "linux")
    monkeypatch.setattr(asm, "systemd_available", lambda: True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    asm.install(ws, config, with_caddy=False, enable=False)
    backend = asm.SystemdUserBackend()
    unit = backend.unit_path("daemon")
    assert unit.is_file()
    assert asm.installed_services(ws, backend) == ["daemon", "manager"] or \
        "daemon" in asm.installed_services(ws, backend)

    def runner(cmd, **kwargs):
        joined = " ".join(cmd)
        if "disable" in joined:
            return CompletedProcess(args=cmd, returncode=1, stdout="", stderr="busy")
        if "is-active" in joined:
            return CompletedProcess(args=cmd, returncode=0, stdout="active", stderr="")
        if "is-enabled" in joined:
            return CompletedProcess(args=cmd, returncode=0, stdout="enabled", stderr="")
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    op = asm.uninstall(ws, config, runner=runner)
    assert op.success is False
    assert unit.is_file()  # disable 失败不得删文件
    # manifest 保留，便于重试
    assert "daemon" in asm.installed_services(ws, backend)


# ---- BUG-159～165：组合状态 / 身份 / PATH ------------------------------------


def test_enable_skips_backend_when_migrate_fails(tmp_path, monkeypatch) -> None:
    """迁移失败后不得再调用 backend.enable（BUG-159）。"""
    root, ws, config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "linux")
    monkeypatch.setattr(asm, "systemd_available", lambda: True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    asm.install(ws, config, with_caddy=False, enable=False)

    monkeypatch.setattr(
        asm, "_migrate_detached_for_supervision",
        lambda *a, **k: False,
    )
    record: list[list[str]] = []
    op = asm.enable(ws, config, runner=_fake_runner(record))
    assert op.success is False
    joined = [" ".join(c) for c in record]
    assert not any("enable" in c and "--now" in c for c in joined)


def test_install_no_enable_does_not_set_daemon_enabled(tmp_path, monkeypatch) -> None:
    """install --no-enable 不得污染 daemon.json enabled（BUG-160）。"""
    root, ws, config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "macos")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    from local_webpage_access import daemon as daemon_mod

    assert daemon_mod.read_state(ws) is None
    asm.install(ws, config, with_caddy=False, enable=False)
    state = daemon_mod.read_state(ws)
    assert state is None or state.enabled is False


def test_systemd_uninstall_reload_failure_restores_unit_for_retry(
    tmp_path, monkeypatch
) -> None:
    """reload 失败须恢复 unit，第二次 uninstall 仍能重试 reload（BUG-161）。"""
    root, ws, config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "linux")
    monkeypatch.setattr(asm, "systemd_available", lambda: True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    asm.install(ws, config, with_caddy=False, enable=False)
    backend = asm.SystemdUserBackend()
    unit = backend.unit_path("daemon")
    assert unit.is_file()

    reload_calls = {"n": 0}

    def runner(cmd, **kwargs):
        joined = " ".join(cmd)
        if "daemon-reload" in joined:
            reload_calls["n"] += 1
            return CompletedProcess(args=cmd, returncode=1, stdout="", stderr="busy")
        if "is-active" in joined:
            return CompletedProcess(args=cmd, returncode=3, stdout="inactive", stderr="")
        if "is-enabled" in joined:
            return CompletedProcess(args=cmd, returncode=1, stdout="disabled", stderr="")
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    op1 = asm.uninstall(ws, config, runner=runner)
    assert op1.success is False
    assert unit.is_file()  # reload 失败须恢复，便于重试
    assert "daemon" in asm.installed_services(ws, backend)

    op2 = asm.uninstall(ws, config, runner=runner)
    assert op2.success is False
    assert reload_calls["n"] >= 2  # 第二次仍执行 reload


def test_check_active_rejects_alive_but_wrong_identity(tmp_path, monkeypatch) -> None:
    """MainPID 存活但 cmdline 非本工作区前台模块 → fail（BUG-162）。"""
    root, ws, config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "macos")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    backend = asm.MacLaunchdBackend()
    backend.write_unit(
        "daemon",
        backend.render("daemon", python_exe=sys.executable, workspace_root=root),
    )
    asm.write_manifest(ws, ["daemon"])

    import local_webpage_access.daemon as dm

    monkeypatch.setattr(dm, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        dm, "pid_cmdline_contains",
        lambda pid, *needles: False,  # 任意存活 PID，身份不对
    )
    monkeypatch.setattr(
        asm, "_service_process_running", lambda *a, **k: True
    )

    item = asm._check_active(ws, config, backend, "daemon", _fake_runner())
    assert item.status == "fail"
    assert "身份" in item.message or "MainPID" in item.message


def test_installed_services_includes_disk_orphans_outside_manifest(
    tmp_path, monkeypatch
) -> None:
    """BUG-168：manifest 存在时仍须检出磁盘上的孤儿单元。"""
    root, ws, config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "macos")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    backend = asm.MacLaunchdBackend()
    # 磁盘上有 daemon + gateway，manifest 仅记录 daemon
    for name in ("daemon", "gateway"):
        backend.write_unit(
            name,
            backend.render(name, python_exe=sys.executable, workspace_root=root),
        )
    asm.write_manifest(ws, ["daemon"])

    detected = asm.installed_services(ws, backend)
    assert "daemon" in detected
    assert "gateway" in detected, "孤儿 gateway 单元不得被 manifest 屏蔽"


def test_reinstall_removes_orphan_services(tmp_path, monkeypatch) -> None:
    """缩减服务集合的重复 install 须卸载被移除的单元（BUG-163）。"""
    root, ws, config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "macos")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    record: list[list[str]] = []
    asm.install(
        ws, config, with_caddy=True, enable=False, python_exe=sys.executable
    )
    backend = asm.MacLaunchdBackend()
    gw = backend.unit_path("gateway")
    assert gw.is_file()

    # 再不带 --with-caddy：manifest 应收窄，gateway 单元须被卸载
    asm.install(
        ws, config, with_caddy=False, enable=False,
        python_exe=sys.executable, runner=_fake_runner(record),
    )
    assert not gw.is_file()
    assert "gateway" not in (asm.read_manifest(ws) or [])
    assert "gateway" not in asm.installed_services(ws, backend)


def test_check_unit_path_rejects_useless_path(tmp_path, monkeypatch) -> None:
    """PATH=/definitely/missing 不得判 OK（BUG-164）。"""
    import plistlib

    root, ws, config = _make_ws(tmp_path)
    monkeypatch.setattr(asm, "detect_platform", lambda: "macos")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    backend = asm.MacLaunchdBackend()
    raw = plistlib.loads(
        backend.render("daemon", python_exe=sys.executable, workspace_root=root)
    )
    raw["EnvironmentVariables"] = {"PATH": "/definitely/missing"}
    path = backend.unit_path("daemon")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(raw, fmt=plistlib.FMT_XML, sort_keys=False))
    item = asm._check_unit_path_env("daemon", "macos")
    assert item.status == "fail"


def test_systemd_static_is_not_enabled(tmp_path, monkeypatch) -> None:
    """is-enabled=static 不得视为自启动已启用（BUG-165）。"""
    monkeypatch.setattr(asm, "detect_platform", lambda: "linux")
    monkeypatch.setattr(asm, "systemd_available", lambda: True)
    backend = asm.SystemdUserBackend()

    def runner(cmd, **kwargs):
        if "is-enabled" in " ".join(cmd):
            return CompletedProcess(args=cmd, returncode=0, stdout="static\n", stderr="")
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    assert backend.is_enabled("daemon", runner) is False
