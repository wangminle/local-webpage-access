"""静态网关测试（WBS-09）。

builtin 模式会真实启动 ``python -m http.server`` 子进程，
因此 enable/disable/health_check 是端到端集成测试。
"""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path

import pytest

from local_webpage_access.config import Config
from local_webpage_access.errors import GatewayError
from local_webpage_access.paths import Workspace
from local_webpage_access.static_gateway import StaticGateway

# BUG-121：本模块单测会走真实 _reload_once/caddy_start（subprocess 已 mock），需放行。
pytestmark = pytest.mark.usefixtures("_allow_caddy_admin_for_unit_tests")


@pytest.fixture()
def _allow_caddy_admin_for_unit_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LWA_ALLOW_CADDY_ADMIN", "1")


@pytest.fixture()
def workspace(workspace_root: Path) -> Workspace:
    ws = Workspace(workspace_root)
    ws.ensure_workspace_dirs()
    return ws


@pytest.fixture()
def gateway(workspace: Workspace) -> StaticGateway:
    # 强制 builtin 后端：builtin enable/disable/health 用例依赖真实 http.server
    # 子进程与 pid/gateway.log。默认 Config() 的 staticGateway=caddy，在装了
    # caddy 的机器上 detect_backend() 会返回 caddy 走 reload 路径，使这些用例
    # 非确定性地失败。caddy 专属行为由各用例自建 Config 或 monkeypatch 覆盖。
    return StaticGateway(workspace, Config(staticGateway="builtin"))


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
    monkeypatch.setattr("local_webpage_access.static_gateway.shutil.which", lambda name: "/usr/bin/caddy")
    gw = StaticGateway(workspace, Config(staticGateway="builtin"))
    assert gw.detect_backend() == "builtin"


def test_detect_backend_caddy_config_falls_back_when_missing(
    workspace: Workspace, monkeypatch
) -> None:
    """BUG-003：配置 caddy 但环境无 caddy → 降级 builtin，不报错。"""
    monkeypatch.setattr("local_webpage_access.static_gateway.shutil.which", lambda name: None)
    gw = StaticGateway(workspace, Config(staticGateway="caddy"))
    assert gw.detect_backend() == "builtin"


def test_detect_backend_caddy_config_uses_caddy_when_present(
    workspace: Workspace, monkeypatch
) -> None:
    """BUG-003：配置 caddy 且 caddy 存在 → caddy。"""
    monkeypatch.setattr("local_webpage_access.static_gateway.shutil.which", lambda name: "/usr/bin/caddy")
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


# ---- BUG-078：is_enabled 在 Caddy 模式按站点配置判定 --------------------------


def _caddy_gateway(workspace: Workspace, monkeypatch) -> StaticGateway:
    """构造确定性的 Caddy 后端 gateway（不依赖机器是否装 caddy）。"""
    monkeypatch.setattr(
        "local_webpage_access.static_gateway.shutil.which",
        lambda name: "/usr/bin/caddy",
    )
    return StaticGateway(workspace, Config(staticGateway="caddy"))


def test_is_enabled_caddy_true_when_site_config_exists(
    workspace: Workspace, monkeypatch
) -> None:
    """BUG-078：Caddy 模式下站点配置存在即视为 enabled（无 per-instance pid）。"""
    gw = _caddy_gateway(workspace, monkeypatch)
    site = gw.site_config_path("demo")
    site.parent.mkdir(parents=True, exist_ok=True)
    site.write_text(f":21100 {{ root */public }}\n", encoding="utf-8")
    assert gw.is_enabled("demo") is True


def test_is_enabled_caddy_false_when_no_site_config(
    workspace: Workspace, monkeypatch
) -> None:
    """BUG-078：Caddy 模式下无站点配置 → 未启用。"""
    gw = _caddy_gateway(workspace, monkeypatch)
    assert gw.is_enabled("demo") is False


def test_apply_gateway_alias_reloads_when_caddy_site_enabled(
    workspace: Workspace, monkeypatch
) -> None:
    """BUG-078 回归：Caddy 静态站点 enabled 时，在线改别名应触发 reload_all。

    修复前 is_enabled() 仅查 pid，Caddy 站点恒判未启用 → _apply_gateway_alias
    提前 return，generate_alias_config/reload_all 不被调用。
    """
    from local_webpage_access.path_alias import _apply_gateway_alias

    gw = _caddy_gateway(workspace, monkeypatch)
    # 站点配置存在 → is_enabled True
    site = gw.site_config_path("demo")
    site.parent.mkdir(parents=True, exist_ok=True)
    site.write_text(":21100 {}\n", encoding="utf-8")

    calls: dict[str, int] = {"reload": 0, "gen_alias": 0}

    monkeypatch.setattr(
        StaticGateway, "reload_all", lambda self: calls.__setitem__("reload", calls["reload"] + 1)
    )
    monkeypatch.setattr(
        StaticGateway,
        "generate_alias_config",
        lambda self, iid, alias, hp: calls.__setitem__("gen_alias", calls["gen_alias"] + 1),
    )

    alias_enabled, reloaded = _apply_gateway_alias(
        workspace,
        Config(staticGateway="caddy"),
        "demo",
        "myapp",
        21100,
        previous_alias=None,
        runtime="shared-static",
    )
    assert alias_enabled is True
    assert reloaded is True
    assert calls["gen_alias"] == 1
    assert calls["reload"] == 1


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


# ---- 回归测试：BUG-014 / BUG-020 -----------------------------------------
#
# BUG-014：_assemble_main_config 此前输出 ``admin off`` 全局块，首次加载后
#          Caddy admin 端点被关闭，后续 enable/disable 的 reload 全部失败。
# BUG-020：import / root 路径未做 Caddyfile 引用，含空格的工作区路径被拆词。


def test_assemble_main_config_has_no_admin_off(gateway: StaticGateway, workspace: Workspace) -> None:
    """BUG-014：主 Caddyfile 不得包含 ``admin off``，否则后续 reload 失败。"""
    root = workspace.app_public("demo")
    root.mkdir(parents=True)
    gateway.generate_site_config("demo", 18001, root)

    content = gateway._assemble_main_config()
    assert "admin off" not in content
    assert "admin" not in content.lower().split()


def test_assemble_main_config_quotes_import_paths(
    gateway: StaticGateway, workspace: Workspace, monkeypatch
) -> None:
    """BUG-020：import 路径用反引号引用，含空格也不被拆词。"""
    # 让站点配置落在含空格的路径下（workspace_root 由 tmp_path 派生，本身可能含空格）
    root = workspace.app_public("demo")
    root.mkdir(parents=True)
    site_conf = gateway.generate_site_config("demo", 18001, root)
    assert site_conf.is_file()

    content = gateway._assemble_main_config()
    # 每行 import 的路径都被反引号包裹
    import_line = [ln for ln in content.splitlines() if ln.startswith("import ")][0]
    rest = import_line[len("import "):]
    assert rest.startswith("`") and rest.endswith("`")
    # 引号内的路径等于站点配置的 posix 路径
    assert rest.strip("`") == site_conf.as_posix()


def test_generate_site_config_quotes_root_path(gateway: StaticGateway, workspace: Workspace) -> None:
    """BUG-020：站点配置 root 路径必须被反引号引用。"""
    root = workspace.app_public("demo")
    root.mkdir(parents=True)
    gateway.generate_site_config("demo", 18001, root)
    content = gateway.site_config_path("demo").read_text(encoding="utf-8")
    # root 行形如 ``root * `.../public` ``，反引号成对出现
    assert content.count("`") >= 2
    root_line = [ln for ln in content.splitlines() if ln.lstrip().startswith("root")][0]
    assert "`" in root_line


def test_caddy_quote_wraps_in_backticks() -> None:
    """BUG-020：普通路径用反引号包裹。"""
    from local_webpage_access.static_gateway import _caddy_quote

    assert _caddy_quote("/var/www/demo") == "`/var/www/demo`"
    assert _caddy_quote("C:/Users/foo bar/site") == "`C:/Users/foo bar/site`"


def test_caddy_quote_falls_back_for_backtick_path() -> None:
    """BUG-020：路径本身含反引号时回退到双引号 + 转义。"""
    from local_webpage_access.static_gateway import _caddy_quote

    quoted = _caddy_quote('/tmp/`whoami`/site')
    assert quoted.startswith('"') and quoted.endswith('"')
    # 内部反引号原样保留（Caddyfile 双引号字符串不把反引号当特殊字符）
    assert "`whoami`" in quoted


# ---- 回归测试：BUG-015 ----------------------------------------------------
#
# BUG-015：_kill_process 此前不校验进程是否真的退出、_stop_builtin 无条件清 PID；
#          Windows 上 taskkill 返回非零可能只是"进程已退出"，旧 http.server
#          可能仍在占端口/锁 gateway.log。修复后 _kill_process 返回 bool，
#          失败时 _stop_builtin 保留 PID 文件。


def test_kill_process_returns_true_for_already_dead_pid(gateway: StaticGateway) -> None:
    """BUG-015：对一个绝不存在的 PID，_kill_process 应判为已退出（True）。"""
    # PID 0xFFFFFFFF 几乎不可能存活；_pid_alive 会返回 False → _wait_for_exit 立即 True
    assert gateway._kill_process(0xFFFFFFFE) is True


def test_stop_builtin_keeps_pid_when_kill_fails(
    gateway: StaticGateway, workspace: Workspace, monkeypatch
) -> None:
    """BUG-015：_kill_process 失败时 _stop_builtin 不得清除 PID 文件。"""
    # 预置一个 PID 文件
    gateway._write_pid("demo", 0xFFFFFFFE)
    assert gateway._read_pid("demo") == 0xFFFFFFFE

    # 让 _kill_process 模拟"无法终止"（进程一直存活）；接受 proc= 以匹配新签名
    monkeypatch.setattr(gateway, "_kill_process", lambda pid, **kw: False)

    gateway._stop_builtin("demo")

    # PID 文件应保留，便于人工排查或重试
    assert gateway._read_pid("demo") == 0xFFFFFFFE


def test_stop_builtin_clears_pid_when_kill_succeeds(
    gateway: StaticGateway, workspace: Workspace, monkeypatch
) -> None:
    """BUG-015：_kill_process 成功时 _stop_builtin 清除 PID（正常路径回归保护）。"""
    gateway._write_pid("demo", 12345)
    monkeypatch.setattr(gateway, "_kill_process", lambda pid, **kw: True)

    gateway._stop_builtin("demo")

    assert gateway._read_pid("demo") is None


# ---- IMP-005：Caddy 静态频率限制 ------------------------------------------


def _caddy_present(workspace, monkeypatch, *, modules: str = "") -> StaticGateway:
    """构造一个 Caddy 后端可用、模块清单可控的 gateway。

    ``modules`` 为 ``caddy list-modules`` 的模拟 stdout；含
    ``http.handlers.rate_limit`` 即视为具备限流能力。
    """
    monkeypatch.setattr(
        "local_webpage_access.static_gateway.shutil.which", lambda name: "/usr/bin/caddy"
    )

    import subprocess as sp

    class _FakeResult:
        returncode = 0
        stdout = modules.encode("utf-8")
        stderr = b""

    def fake_run(cmd, **kw):
        return _FakeResult()

    monkeypatch.setattr("local_webpage_access.static_gateway.subprocess.run", fake_run)
    return StaticGateway(workspace, Config(staticGateway="caddy"))


def test_static_rate_limit_defaults() -> None:
    from local_webpage_access.config import StaticRateLimit

    rl = StaticRateLimit()
    assert rl.enabled is False
    assert rl.rps == 3
    assert rl.burst == 6


def test_static_rate_limit_rejects_invalid_ranges() -> None:
    import pytest as _pytest

    from local_webpage_access.config import StaticRateLimit

    with _pytest.raises(Exception):
        StaticRateLimit(rps=0)
    with _pytest.raises(Exception):
        StaticRateLimit(burst=0)


def test_rate_limit_directive_disabled_returns_empty(gateway: StaticGateway) -> None:
    """未启用时限流指令为空串（默认配置）。"""
    assert gateway._rate_limit_directive("demo") == ""


def test_rate_limit_directive_builtin_returns_empty(
    workspace: Workspace, monkeypatch
) -> None:
    """builtin 后端不支持限流，指令为空。"""
    from local_webpage_access.config import StaticRateLimit

    gw = StaticGateway(
        workspace,
        Config(staticGateway="builtin", staticRateLimit=StaticRateLimit(enabled=True)),
    )
    assert gw._rate_limit_directive("demo") == ""


def test_rate_limit_directive_caddy_without_module_warns(
    workspace: Workspace, monkeypatch
) -> None:
    """Caddy 后端但无 rate_limit 模块 → 指令为空（站点仍可访问）。"""
    gw = _caddy_present(workspace, monkeypatch, modules="http.handlers.file_server\n")
    from local_webpage_access.config import StaticRateLimit

    gw.config = Config(
        staticGateway="caddy", staticRateLimit=StaticRateLimit(enabled=True)
    )
    # 重新探测前清缓存
    gw._supports_rate_limit = None
    assert gw._rate_limit_directive("demo") == ""


def test_rate_limit_directive_injected_when_capable(
    workspace: Workspace, monkeypatch
) -> None:
    """Caddy + rate_limit 模块 → 注入指令，令牌桶参数正确。"""
    from local_webpage_access.config import StaticRateLimit

    gw = _caddy_present(
        workspace,
        monkeypatch,
        modules="http.handlers.file_server\nhttp.handlers.rate_limit\n",
    )
    gw.config = Config(
        staticGateway="caddy",
        staticRateLimit=StaticRateLimit(enabled=True, rps=3, burst=6),
    )
    gw._supports_rate_limit = None
    directive = gw._rate_limit_directive("demo")
    assert "rate_limit" in directive
    assert "zone lwa_demo" in directive
    # rps=3, burst=6 → events=6, window=2s
    assert "events 6" in directive
    assert "window 2s" in directive
    # {remote_host} 是 Caddy 占位符，作为字面值保留
    assert "{remote_host}" in directive


def test_rate_limit_window_fractional_uses_millis(
    workspace: Workspace, monkeypatch
) -> None:
    """burst < rps 时窗口小于 1 秒，用毫秒表示（如 rps=10, burst=5 → 500ms）。"""
    from local_webpage_access.config import StaticRateLimit

    gw = _caddy_present(
        workspace,
        monkeypatch,
        modules="http.handlers.rate_limit\n",
    )
    gw.config = Config(
        staticGateway="caddy",
        staticRateLimit=StaticRateLimit(enabled=True, rps=10, burst=5),
    )
    gw._supports_rate_limit = None
    directive = gw._rate_limit_directive("demo")
    assert "events 5" in directive
    assert "window 500ms" in directive


def test_rate_limit_directive_generated_into_site_config(
    workspace: Workspace, monkeypatch
) -> None:
    """generate_site_config 把指令写入站点 .conf。"""
    from local_webpage_access.config import StaticRateLimit

    gw = _caddy_present(
        workspace,
        monkeypatch,
        modules="http.handlers.rate_limit\n",
    )
    gw.config = Config(
        staticGateway="caddy",
        staticRateLimit=StaticRateLimit(enabled=True, rps=3, burst=6),
    )
    gw._supports_rate_limit = None
    root = workspace.app_public("demo")
    root.mkdir(parents=True)
    path = gw.generate_site_config("demo", 18001, root)
    content = path.read_text(encoding="utf-8")
    assert "rate_limit" in content
    assert "zone lwa_demo" in content


def test_rate_limit_not_injected_when_disabled_in_site_config(
    gateway: StaticGateway, workspace: Workspace
) -> None:
    """默认配置（限流关闭）生成的 .conf 不含 rate_limit 指令。"""
    root = workspace.app_public("demo")
    root.mkdir(parents=True)
    path = gateway.generate_site_config("demo", 18001, root)
    content = path.read_text(encoding="utf-8")
    # 检查指令语法（避免与测试路径名中的 rate_limit 子串混淆）
    assert "rate_limit {" not in content
    assert "zone lwa_" not in content
    assert "file_server" in content


def test_supports_rate_limit_caches_result(
    workspace: Workspace, monkeypatch
) -> None:
    """supports_rate_limit 在实例生命周期内缓存，不重复探测。"""
    call_count = {"n": 0}

    import subprocess as sp

    class _FakeResult:
        returncode = 0
        stdout = b"http.handlers.rate_limit\n"
        stderr = b""

    def fake_run(cmd, **kw):
        call_count["n"] += 1
        return _FakeResult()

    monkeypatch.setattr(
        "local_webpage_access.static_gateway.shutil.which", lambda name: "/usr/bin/caddy"
    )
    monkeypatch.setattr("local_webpage_access.static_gateway.subprocess.run", fake_run)
    gw = StaticGateway(workspace, Config(staticGateway="caddy"))
    assert gw.supports_rate_limit() is True
    assert gw.supports_rate_limit() is True
    assert call_count["n"] == 1  # 第二次命中缓存


# ---- IMP-006：路径别名路由片段与统一入口 -----------------------------------


def test_generate_alias_config_writes_strip_prefix_route(
    gateway: StaticGateway, workspace: Workspace
) -> None:
    """别名片段含 handle_path（去前缀）+ handle（无尾斜杠 301）。"""
    path = gateway.generate_alias_config("demo", "voiceprint-app-demo", 18001)
    assert path.is_file()
    assert path == workspace.app_alias_config("demo")
    content = path.read_text(encoding="utf-8")
    # 去前缀反向代理到本机 hostPort
    assert "handle_path /voiceprint-app-demo/* {" in content
    assert "reverse_proxy 127.0.0.1:18001" in content
    # 无尾斜杠 → 301 到 /voiceprint-app-demo/
    assert "handle /voiceprint-app-demo {" in content
    assert "redir /voiceprint-app-demo/ permanent" in content


def test_remove_alias_config_idempotent(gateway: StaticGateway) -> None:
    """删除不存在的别名片段不应报错。"""
    gateway.remove_alias_config("never-existed")  # 不抛


def test_assemble_main_config_emits_alias_entry_block(
    gateway: StaticGateway, workspace: Workspace
) -> None:
    """存在别名片段且端口已配置时，主 Caddyfile 追加统一入口块。"""
    root = workspace.app_public("demo")
    root.mkdir(parents=True)
    gateway.generate_site_config("demo", 18001, root)
    gateway.generate_alias_config("demo", "voiceprint", 18001)

    content = gateway._assemble_main_config()
    # 站点 import 仍在
    assert any(ln.startswith("import ") for ln in content.splitlines())
    # 统一入口块
    assert ":8080 {" in content  # 默认 staticGatewayPort=8080
    assert "# IMP-006 路径别名统一入口" in content
    # 别名片段被 import 进块（缩进一级）
    alias_conf = workspace.app_alias_config("demo").as_posix()
    assert f"\timport `{alias_conf}`" in content


def test_assemble_main_config_no_alias_block_without_fragments(
    gateway: StaticGateway, workspace: Workspace
) -> None:
    """无别名片段时不追加统一入口块（端口不被占用）。"""
    root = workspace.app_public("demo")
    root.mkdir(parents=True)
    gateway.generate_site_config("demo", 18001, root)

    content = gateway._assemble_main_config()
    assert ":8080" not in content
    assert "IMP-006" not in content


def test_assemble_main_config_no_alias_block_when_port_none(
    workspace: Workspace, monkeypatch
) -> None:
    """staticGatewayPort=None 时即便有别名片段也不注入块（入口关闭）。"""
    gw = StaticGateway(workspace, Config(staticGatewayPort=None))
    root = workspace.app_public("demo")
    root.mkdir(parents=True)
    gw.generate_site_config("demo", 18001, root)
    gw.generate_alias_config("demo", "voiceprint", 18001)

    content = gw._assemble_main_config()
    assert ":8080" not in content
    assert "IMP-006" not in content


def test_assemble_main_config_alias_block_uses_configured_port(
    workspace: Workspace
) -> None:
    """统一入口端口跟随 config.staticGatewayPort。"""
    gw = StaticGateway(workspace, Config(staticGatewayPort=9090))
    root = workspace.app_public("demo")
    root.mkdir(parents=True)
    gw.generate_alias_config("demo", "voiceprint", 18001)

    content = gw._assemble_main_config()
    assert ":9090 {" in content
    assert ":8080" not in content


def test_disable_removes_alias_fragment(gateway: StaticGateway, workspace: Workspace, monkeypatch) -> None:
    """disable 同时清理站点配置与别名片段（builtin 模式也清）。"""
    monkeypatch.setattr(gateway, "detect_backend", lambda: "builtin")
    monkeypatch.setattr(gateway, "_stop_builtin", lambda iid: None)
    # 预置别名片段
    gateway.generate_alias_config("demo", "voiceprint", 18001)
    assert workspace.app_alias_config("demo").exists()

    gateway.disable("demo")
    assert not workspace.app_alias_config("demo").exists()
    assert not gateway.site_config_path("demo").exists()


# ---- 回归测试：BUG-069（悬空 import 死锁）---------------------------------
#
# BUG-069：enable/disable 删除 site/alias 片段后，主 Caddyfile 若仍 import 已删
# 文件，caddy validate/start 失败 → "恢复→失败→回滚"死锁。修复：删片段后由
# _sync_main_config 按磁盘实际文件无条件重组主 Caddyfile。


def test_enable_failure_leaves_no_dangling_import(
    gateway: StaticGateway, workspace: Workspace, monkeypatch
) -> None:
    """BUG-069 核心：已启用实例再次 enable 且 reload 失败时，主 Caddyfile 不得残留悬空 import。

    场景：demo 此前已成功启用（main 含 ``import sites/demo.conf``，文件存在）；
    再次 enable 时 reload 失败 → enable catch 删片段 + ``_sync_main_config`` 重组。
    重组后主 Caddyfile 基于磁盘实际文件（demo.conf 已删）→ 不再 import 它。
    """
    monkeypatch.setattr(gateway, "detect_backend", lambda: "caddy")
    # 预置"已启用"状态
    root = workspace.app_public("demo")
    root.mkdir(parents=True)
    (root / "index.html").write_text("hi")
    gateway.generate_site_config("demo", 18001, root)
    main = gateway.main_config_path()
    main.parent.mkdir(parents=True, exist_ok=True)
    main.write_text(gateway._assemble_main_config(), encoding="utf-8")  # 含 import demo.conf
    assert "demo.conf" in main.read_text()

    # reload 全部失败（master 不可达）——_sync_main_config 仍会按实际文件重写主配置
    monkeypatch.setattr(gateway, "_admin_alive", lambda **kw: False)
    monkeypatch.setattr(gateway, "caddy_start", lambda: False)

    class _Fail:
        returncode = 1
        stderr = b"reload error"

    monkeypatch.setattr(
        "local_webpage_access.static_gateway.subprocess.run",
        lambda *a, **kw: _Fail(),
    )

    with pytest.raises(GatewayError):
        gateway.enable("demo", 18001, root)

    # 主 Caddyfile 不再 import 已删除的 demo.conf（无悬空 import）
    assert not gateway.site_config_path("demo").exists()
    assert "demo.conf" not in main.read_text()


def test_disable_leaves_no_dangling_import(
    gateway: StaticGateway, workspace: Workspace, monkeypatch
) -> None:
    """BUG-069：Caddy 模式 disable 后主 Caddyfile 不再 import 已删站点。"""
    monkeypatch.setattr(gateway, "detect_backend", lambda: "caddy")
    root = workspace.app_public("demo")
    root.mkdir(parents=True)
    gateway.generate_site_config("demo", 18001, root)
    main = gateway.main_config_path()
    main.parent.mkdir(parents=True, exist_ok=True)
    main.write_text(gateway._assemble_main_config(), encoding="utf-8")
    assert "demo.conf" in main.read_text()

    monkeypatch.setattr(gateway, "_admin_alive", lambda **kw: False)
    monkeypatch.setattr(gateway, "caddy_start", lambda: False)

    class _Fail:
        returncode = 1
        stderr = b"err"

    monkeypatch.setattr(
        "local_webpage_access.static_gateway.subprocess.run",
        lambda *a, **kw: _Fail(),
    )

    gateway.disable("demo")

    assert not gateway.site_config_path("demo").exists()
    assert "demo.conf" not in main.read_text()


def test_sync_main_config_writes_assembled_content(
    gateway: StaticGateway, workspace: Workspace, monkeypatch
) -> None:
    """_sync_main_config 按磁盘实际文件重写主 Caddyfile（BUG-069）。"""
    monkeypatch.setattr(gateway, "detect_backend", lambda: "caddy")
    monkeypatch.setattr(gateway, "_reload_with_self_heal", lambda: (True, ""))
    main = gateway.main_config_path()
    main.parent.mkdir(parents=True, exist_ok=True)
    main.write_text("# stale\nimport `/nope/missing.conf`\n", encoding="utf-8")

    gateway._sync_main_config()

    # 无任何 site/alias 片段 → 主配置为空组装（无悬空 import）
    content = main.read_text(encoding="utf-8")
    assert "missing.conf" not in content
    assert "import" not in content


# ---- 回归测试：IMP-010（Caddy master 生命周期 + reload 自愈）--------------

_DEAD_PID = 0xFFFFFFFE  # 几乎不可能存活的 pid，用于 stale pid 测试


def test_reload_with_self_heal_retries_after_failure(
    gateway: StaticGateway, monkeypatch
) -> None:
    """IMP-010/0.4：reload 首次失败、ensure 成功后应再 reload 一次并成功。"""
    monkeypatch.setattr(gateway, "detect_backend", lambda: "caddy")
    state = {"n": 0}

    def fake_reload_once():
        state["n"] += 1
        return (False, "err") if state["n"] == 1 else (True, "")

    monkeypatch.setattr(gateway, "_reload_once", fake_reload_once)
    monkeypatch.setattr(gateway, "ensure_caddy_running", lambda: True)

    ok, stderr = gateway._reload_with_self_heal()
    assert ok is True
    assert state["n"] == 2  # 失败一次后自愈再 reload 成功


def test_reload_with_self_heal_gives_up_when_start_fails(
    gateway: StaticGateway, monkeypatch
) -> None:
    """IMP-010/0.4：reload 失败且 master 无法拉起时放弃，返回 False。"""
    monkeypatch.setattr(gateway, "detect_backend", lambda: "caddy")
    monkeypatch.setattr(gateway, "_reload_once", lambda: (False, "err"))
    monkeypatch.setattr(gateway, "ensure_caddy_running", lambda: False)

    ok, stderr = gateway._reload_with_self_heal()
    assert ok is False


def test_reload_all_invokes_ensure_caddy_running(
    gateway: StaticGateway, workspace: Workspace, monkeypatch
) -> None:
    """IMP-010/0.3：reload_all 在 reload 前必须 ensure_caddy_running。"""
    monkeypatch.setattr(gateway, "detect_backend", lambda: "caddy")
    called = {"ensure": False}

    def fake_ensure():
        called["ensure"] = True
        return True

    monkeypatch.setattr(gateway, "ensure_caddy_running", fake_ensure)
    monkeypatch.setattr(gateway, "_reload_once", lambda: (True, ""))

    gateway.reload_all()
    assert called["ensure"] is True


def test_ensure_caddy_running_starts_when_admin_down(
    gateway: StaticGateway, monkeypatch
) -> None:
    """IMP-010/0.3：admin 不在线时调 caddy_start 拉起。"""
    monkeypatch.setattr(gateway, "_admin_alive", lambda **kw: False)
    monkeypatch.setattr(gateway, "caddy_start", lambda: True)
    assert gateway.ensure_caddy_running() is True


def test_ensure_caddy_running_returns_true_when_admin_up(
    gateway: StaticGateway, monkeypatch
) -> None:
    """IMP-010/0.3：admin 已在线时直接返回 True，不重复 start。"""
    started = {"n": 0}
    monkeypatch.setattr(gateway, "_admin_alive", lambda **kw: True)
    monkeypatch.setattr(gateway, "caddy_start", lambda: started.__setitem__("n", started["n"] + 1) or True)
    assert gateway.ensure_caddy_running() is True
    assert started["n"] == 0  # admin 在线，不触发 start


def test_ensure_caddy_running_returns_false_when_start_fails(
    gateway: StaticGateway, monkeypatch
) -> None:
    monkeypatch.setattr(gateway, "_admin_alive", lambda **kw: False)
    monkeypatch.setattr(gateway, "caddy_start", lambda: False)
    assert gateway.ensure_caddy_running() is False


def test_caddy_start_uses_main_when_present(
    gateway: StaticGateway, workspace: Workspace, monkeypatch
) -> None:
    """IMP-010：主 Caddyfile 存在且非空时，caddy start 用它（并带 --pidfile）。"""
    main = gateway.main_config_path()
    main.parent.mkdir(parents=True, exist_ok=True)
    main.write_text("# real config\n", encoding="utf-8")
    captured = {}

    class _OK:
        returncode = 0
        stderr = b""

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _OK()

    monkeypatch.setattr(
        "local_webpage_access.static_gateway.subprocess.run", fake_run
    )
    monkeypatch.setattr(gateway, "_admin_alive", lambda **kw: True)

    assert gateway.caddy_start() is True
    cmd_str = " ".join(captured["cmd"])
    assert str(main) in cmd_str
    assert "--pidfile" in cmd_str


def test_caddy_start_falls_back_to_bootstrap_when_no_main(
    gateway: StaticGateway, workspace: Workspace, monkeypatch
) -> None:
    """IMP-010：无主 Caddyfile时写最小 bootstrap 并用它启动。"""
    main = gateway.main_config_path()
    assert not main.exists()
    captured = {}

    class _OK:
        returncode = 0
        stderr = b""

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _OK()

    monkeypatch.setattr(
        "local_webpage_access.static_gateway.subprocess.run", fake_run
    )
    monkeypatch.setattr(gateway, "_admin_alive", lambda **kw: True)

    assert gateway.caddy_start() is True
    assert gateway._bootstrap_config_path().is_file()
    assert str(gateway._bootstrap_config_path()) in " ".join(captured["cmd"])


def test_caddy_start_admin_probe_when_cmd_fails_but_admin_alive(
    gateway: StaticGateway, monkeypatch
) -> None:
    """BUG-102：caddy start 非零退出但 admin + 本工作区 pidfile 就绪 → True。"""
    class _Fail:
        returncode = 1
        stderr = b"pingback timeout"

    monkeypatch.setattr(
        "local_webpage_access.static_gateway.subprocess.run",
        lambda *a, **kw: _Fail(),
    )
    monkeypatch.setattr(gateway, "_admin_alive", lambda **kw: True)
    monkeypatch.setattr(gateway, "_workspace_caddy_pid_alive", lambda: True)
    assert gateway.caddy_start() is True  # 假失败恢复


def test_caddy_start_returns_false_when_cmd_fails_and_admin_down(
    gateway: StaticGateway, monkeypatch
) -> None:
    """BUG-102：caddy start 命令失败且 admin 始终不可达 → 真失败，返回 False。"""
    class _Fail:
        returncode = 1
        stderr = b"real error"

    monkeypatch.setattr(
        "local_webpage_access.static_gateway.subprocess.run",
        lambda *a, **kw: _Fail(),
    )
    monkeypatch.setattr(gateway, "_admin_alive", lambda **kw: False)
    assert gateway.caddy_start() is False


def test_caddy_start_recovers_on_timeout_exception(
    gateway: StaticGateway, monkeypatch
) -> None:
    """BUG-102：TimeoutExpired（pingback）但本工作区 admin+pidfile 就绪 → True。"""
    import subprocess as sp

    def _timeout(*a, **kw):
        raise sp.TimeoutExpired(cmd=a[0] if a else "caddy", timeout=20)

    monkeypatch.setattr(
        "local_webpage_access.static_gateway.subprocess.run", _timeout
    )
    monkeypatch.setattr(gateway, "_admin_alive", lambda **kw: True)
    monkeypatch.setattr(gateway, "_workspace_caddy_pid_alive", lambda: True)
    assert gateway.caddy_start() is True


def test_caddy_start_file_not_found_is_hard_fail(
    gateway: StaticGateway, monkeypatch
) -> None:
    """§10.2-C2：PATH 无 caddy（FileNotFoundError）立即失败，不认领孤儿 admin。"""

    def _missing(*a, **kw):
        raise FileNotFoundError("caddy")

    monkeypatch.setattr(
        "local_webpage_access.static_gateway.subprocess.run", _missing
    )
    monkeypatch.setattr(gateway, "_admin_alive", lambda **kw: True)
    monkeypatch.setattr(gateway, "_workspace_caddy_pid_alive", lambda: True)
    assert gateway.caddy_start() is False


def test_caddy_start_rejects_orphan_admin_without_workspace_pid(
    gateway: StaticGateway, monkeypatch
) -> None:
    """§10.2-C2：pingback 失败后 admin 在线但本工作区 pidfile 无效 → 不认领。"""
    class _Fail:
        returncode = 1
        stderr = b"pingback timeout"

    monkeypatch.setattr(
        "local_webpage_access.static_gateway.subprocess.run",
        lambda *a, **kw: _Fail(),
    )
    monkeypatch.setattr(gateway, "_admin_alive", lambda **kw: True)
    monkeypatch.setattr(gateway, "_workspace_caddy_pid_alive", lambda: False)
    assert gateway.caddy_start() is False

def test_caddy_stop_clears_pid_when_admin_down(
    gateway: StaticGateway, monkeypatch
) -> None:
    """IMP-010/BUG-070：admin 不在线时 caddy_stop 视为已停并清理 stale caddy pid。"""
    monkeypatch.setattr(gateway, "_admin_alive", lambda **kw: False)
    caddy_pid = gateway.caddy_pid_path()
    caddy_pid.parent.mkdir(parents=True, exist_ok=True)
    caddy_pid.write_text(str(_DEAD_PID), encoding="utf-8")
    assert gateway.caddy_stop() is True
    assert not caddy_pid.exists()


# ---- 回归测试：BUG-070（stale pid 清理）----------------------------------


def test_clear_stale_static_pid_removes_dead_pid(gateway: StaticGateway) -> None:
    gateway._write_pid("demo", _DEAD_PID)
    assert gateway._pid_path("demo").is_file()
    gateway._clear_stale_static_pid("demo")
    assert not gateway._pid_path("demo").exists()


def test_clear_stale_static_pid_keeps_alive_pid(gateway: StaticGateway) -> None:
    gateway._write_pid("demo", os.getpid())  # 当前进程存活
    gateway._clear_stale_static_pid("demo")
    assert gateway._read_pid("demo") == os.getpid()


def test_clear_stale_static_pid_noop_when_absent(gateway: StaticGateway) -> None:
    gateway._clear_stale_static_pid("never-existed")  # 不抛


def test_clear_stale_caddy_pid_removes_dead_pid(gateway: StaticGateway) -> None:
    path = gateway.caddy_pid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(_DEAD_PID), encoding="utf-8")
    gateway._clear_stale_caddy_pid()
    assert not path.exists()

