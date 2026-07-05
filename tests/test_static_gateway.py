"""静态网关测试（WBS-09）。

builtin 模式会真实启动 ``python -m http.server`` 子进程，
因此 enable/disable/health_check 是端到端集成测试。
"""

from __future__ import annotations

import socket
import time
from pathlib import Path

import pytest

from local_web_access.config import Config
from local_web_access.errors import GatewayError
from local_web_access.paths import Workspace
from local_web_access.static_gateway import StaticGateway


@pytest.fixture()
def workspace(workspace_root: Path) -> Workspace:
    ws = Workspace(workspace_root)
    ws.ensure_workspace_dirs()
    return ws


@pytest.fixture()
def gateway(workspace: Workspace) -> StaticGateway:
    return StaticGateway(workspace, Config())


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---- 配置生成 --------------------------------------------------------------


def test_generate_site_config_writes_file(gateway: StaticGateway, workspace: Workspace) -> None:
    root = workspace.app_public("demo")
    root.mkdir(parents=True)
    path = gateway.generate_site_config("demo", 18001, root)
    assert path.is_file()
    content = path.read_text(encoding="utf-8")
    assert ":18001" in content
    assert "file_server" in content
    assert str(root).replace("\\", "/") in content


def test_remove_site_config(gateway: StaticGateway, workspace: Workspace) -> None:
    root = workspace.app_public("demo")
    root.mkdir(parents=True)
    gateway.generate_site_config("demo", 18001, root)
    assert gateway.site_config_path("demo").exists()
    gateway.remove_site_config("demo")
    assert not gateway.site_config_path("demo").exists()


def test_detect_backend(gateway: StaticGateway) -> None:
    # 测试环境通常没有 caddy，应是 builtin；有 caddy 时是 caddy
    backend = gateway.detect_backend()
    assert backend in ("caddy", "builtin")


# ---- 回归测试：BUG-003 ----------------------------------------------------
#
# BUG-003：detect_backend 此前只看 caddy 可执行文件，完全忽略 config.staticGateway。


def test_detect_backend_builtin_config_forces_builtin(
    workspace: Workspace, monkeypatch
) -> None:
    """BUG-003：staticGateway=builtin 时即使环境装了 caddy 也必须用 builtin。"""
    # 假装 caddy 存在
    monkeypatch.setattr("local_web_access.static_gateway.shutil.which", lambda name: "/usr/bin/caddy")
    gw = StaticGateway(workspace, Config(staticGateway="builtin"))
    assert gw.detect_backend() == "builtin"


def test_detect_backend_caddy_config_falls_back_when_missing(
    workspace: Workspace, monkeypatch
) -> None:
    """BUG-003：配置 caddy 但环境无 caddy → 降级 builtin，不报错。"""
    monkeypatch.setattr("local_web_access.static_gateway.shutil.which", lambda name: None)
    gw = StaticGateway(workspace, Config(staticGateway="caddy"))
    assert gw.detect_backend() == "builtin"


def test_detect_backend_caddy_config_uses_caddy_when_present(
    workspace: Workspace, monkeypatch
) -> None:
    """BUG-003：配置 caddy 且 caddy 存在 → caddy。"""
    monkeypatch.setattr("local_web_access.static_gateway.shutil.which", lambda name: "/usr/bin/caddy")
    gw = StaticGateway(workspace, Config(staticGateway="caddy"))
    assert gw.detect_backend() == "caddy"


# ---- builtin enable/disable/health（真实子进程）----------------------------


def test_enable_starts_builtin_and_health_passes(
    gateway: StaticGateway, workspace: Workspace
) -> None:
    public = workspace.app_public("demo")
    public.mkdir(parents=True)
    (public / "index.html").write_text("<html>hi</html>")

    port = _free_port()
    try:
        gateway.enable("demo", port, public, wait_health=True)
        assert gateway.is_enabled("demo") is True
        assert gateway.health_check(port) is True
        # pid 文件存在
        assert gateway._read_pid("demo") is not None
        # gateway.log 有内容
        log_file = workspace.app_logs("demo") / "gateway.log"
        assert log_file.is_file()
    finally:
        gateway.disable("demo")


def test_disable_stops_builtin(gateway: StaticGateway, workspace: Workspace) -> None:
    public = workspace.app_public("demo")
    public.mkdir(parents=True)
    (public / "index.html").write_text("<html>hi</html>")
    port = _free_port()
    gateway.enable("demo", port, public)
    try:
        assert gateway.health_check(port) is True
    finally:
        gateway.disable("demo")
    # disable 后端口不再服务
    assert gateway.health_check(port) is False
    assert gateway.is_enabled("demo") is False
    assert not gateway.site_config_path("demo").exists()


def test_enable_rolls_back_on_missing_root(gateway: StaticGateway, workspace: Workspace) -> None:
    port = _free_port()
    with pytest.raises(Exception):
        gateway.enable("demo", port, workspace.app_public("demo") / "nope")
    # 不应残留 pid
    assert gateway._read_pid("demo") is None


def test_enable_health_check_rolls_back(
    gateway: StaticGateway, workspace: Workspace, monkeypatch
) -> None:
    """健康检查失败时应回滚（停进程 + 删配置）。"""
    public = workspace.app_public("demo")
    public.mkdir(parents=True)
    (public / "index.html").write_text("<html>hi</html>")
    port = _free_port()

    # 让 health_check 恒返回 False
    monkeypatch.setattr(gateway, "health_check", lambda *a, **kw: False)

    with pytest.raises(Exception, match="健康检查失败"):
        gateway.enable("demo", port, public, wait_health=True)

    # 回滚：进程已停、配置已删
    assert gateway._read_pid("demo") is None
    assert not gateway.site_config_path("demo").exists()


# ---- 健康检查逻辑 ----------------------------------------------------------


def test_health_check_false_for_dead_port(gateway: StaticGateway) -> None:
    port = _free_port()  # 没人监听
    assert gateway.health_check(port, timeout=1) is False


def test_health_check_true_for_real_server(gateway: StaticGateway, workspace: Workspace) -> None:
    public = workspace.app_public("hc")
    public.mkdir(parents=True)
    (public / "index.html").write_text("<html>ok</html>")
    port = _free_port()
    gateway.enable("hc", port, public)
    try:
        # 给服务一点启动时间
        time.sleep(0.3)
        assert gateway.health_check(port, timeout=3) is True
    finally:
        gateway.disable("hc")


# ---- 回归测试：BUG-007 ----------------------------------------------------
#
# BUG-007：reload_all 首次无旧配置且 reload 失败时，坏的新 Caddyfile 被留在原地


class _FakeReloadResult:
    returncode = 1
    stderr = b"parse error: invalid site address"


def test_reload_all_first_time_failure_deletes_broken_config(
    gateway: StaticGateway, monkeypatch
) -> None:
    """BUG-007：首次 reload 失败且无旧配置时，坏的 Caddyfile 应被删除而非残留。"""
    monkeypatch.setattr(gateway, "detect_backend", lambda: "caddy")
    main = gateway.main_config_path()
    assert not main.exists()  # 首次：没有旧配置

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: _FakeReloadResult())

    with pytest.raises(GatewayError, match="reload 失败"):
        gateway.reload_all()

    # 坏配置应被删除，不残留影响后续 reload
    assert not main.exists()


def test_reload_all_existing_failure_restores_previous(
    gateway: StaticGateway, monkeypatch
) -> None:
    """有旧配置时 reload 失败应恢复上一份内容（既有正确行为，回归保护）。"""
    monkeypatch.setattr(gateway, "detect_backend", lambda: "caddy")
    main = gateway.main_config_path()
    main.parent.mkdir(parents=True, exist_ok=True)
    main.write_text("# previous good config\n", encoding="utf-8")

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: _FakeReloadResult())

    with pytest.raises(GatewayError):
        gateway.reload_all()

    # 旧配置应被恢复
    assert main.read_text(encoding="utf-8") == "# previous good config\n"


# ---- 回归测试：BUG-006 ----------------------------------------------------
#
# BUG-006：_start_builtin 把 log_fh 传给 Popen 后从不关闭，父进程侧句柄
# 锁定 gateway.log，Windows 下 disable 后再删实例目录/gateway.log 报 PermissionError。


def test_start_builtin_does_not_keep_log_handle(
    gateway: StaticGateway, workspace: Workspace
) -> None:
    """BUG-006：enable/disable 后父进程不应再持有 gateway.log 句柄。

    修复前 log_fh 在成功路径上从不 close；disable 杀掉子进程后，父进程
    （pytest）侧句柄仍锁住 gateway.log，Windows 下 unlink 会 PermissionError。
    """
    public = workspace.app_public("demo")
    public.mkdir(parents=True)
    (public / "index.html").write_text("<html>hi</html>")
    port = _free_port()
    gateway.enable("demo", port, public)
    # 子进程在服务，gateway.log 已有写入
    assert (workspace.app_logs("demo") / "gateway.log").is_file()
    gateway.disable("demo")  # 杀掉子进程；子进程侧句柄随之释放

    log_file = workspace.app_logs("demo") / "gateway.log"
    # 此时唯一可能仍持有句柄的是父进程侧的 log_fh；修复后应已关闭，可删除
    log_file.unlink()
    assert not log_file.exists()
