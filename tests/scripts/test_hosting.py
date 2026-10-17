"""托管流程测试（WBS-10 / WBS-11）。

静态流程用真实 builtin 网关做端到端；
前端构建流程用 monkeypatch 模拟 npm 命令，避免依赖 Node 环境。
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from local_webpage_access.config import Config, PortPool
from local_webpage_access.errors import BuildError, DockerError, HostingError
from local_webpage_access.hosting import (
    build_and_host_frontend,
    find_build_output,
    find_index_html,
    host_instance,
    host_static,
    run_command,
    stop_instance,
    sync_dir,
    sync_static_to_public,
)
from local_webpage_access.importer import build_manifest_from_detection
from local_webpage_access.models import Kind, ResourceProfile, Runtime, ServingMode, Status
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry
from local_webpage_access.scanner import DetectionResult


# ---- fixtures --------------------------------------------------------------


@pytest.fixture()
def workspace(workspace_root: Path) -> Workspace:
    ws = Workspace(workspace_root)
    ws.ensure_workspace_dirs()
    return ws


@pytest.fixture()
def registry(workspace_root: Path) -> Registry:
    workspace_root.joinpath("registry").mkdir(parents=True, exist_ok=True)
    reg = Registry(workspace_root / "registry" / "local-web.db")
    reg.open()
    yield reg
    reg.close()


@pytest.fixture()
def config(workspace_root: Path) -> Config:
    # 强制 builtin：host_static 用例依赖真实 builtin 静态子进程（pid/health/port
    # 回滚）。默认 staticGateway=caddy 在装了 caddy 的机器上会走 reload 路径使
    # 这些用例非确定性失败。caddy 专属行为（如别名片段）由各用例自设 config 覆盖。
    return Config(staticGateway="builtin", portPool=PortPool(start=21000, end=21050))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _seed_static_instance(workspace: Workspace, registry: Registry, iid: str = "demo") -> None:
    """构造一个已导入的静态实例（current/ 含 index.html + manifest + registry 记录）。"""
    workspace.ensure_app_dirs(iid)
    current = workspace.app_current(iid)
    (current / "index.html").write_text("<html><body>hello</body></html>")
    (current / "style.css").write_text("body{}")
    detection = DetectionResult(
        kind=Kind.STATIC,
        runtime=Runtime.SHARED_STATIC,
        servingMode=ServingMode.SHARED_STATIC,
        resourceProfile=ResourceProfile.TINY,
        form="static",
        confidence="high",
    )
    manifest = build_manifest_from_detection(
        instance_id=iid,
        display_name="Demo",
        detection=detection,
        workspace=workspace,
    )
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)


def _seed_frontend_instance(workspace: Workspace, registry: Registry, iid: str = "spa") -> None:
    """构造一个已导入的前端实例（current/ 含 package.json，build 脚本存在）。"""
    workspace.ensure_app_dirs(iid)
    current = workspace.app_current(iid)
    (current / "package.json").write_text(
        '{"dependencies":{"react":"^18"},"scripts":{"build":"vite build"}}'
    )
    detection = DetectionResult(
        kind=Kind.NODE,
        runtime=Runtime.SHARED_STATIC,
        servingMode=ServingMode.SHARED_STATIC,
        resourceProfile=ResourceProfile.TINY,
        form="frontend-static",
        confidence="high",
        stack=["react"],
        entry={"install": "npm ci", "build": "npm run build", "start": None},
    )
    from local_webpage_access.models import EntryConfig

    detection.entry = EntryConfig(install="npm ci", build="npm run build")
    manifest = build_manifest_from_detection(
        instance_id=iid,
        display_name="Spa",
        detection=detection,
        workspace=workspace,
    )
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)


# ---- 辅助函数 --------------------------------------------------------------

# ---- find_index_html ------------------------------------------------------


def test_find_index_html_top_level(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("x")
    assert find_index_html(tmp_path) == tmp_path / "index.html"


def test_find_index_html_subdir(tmp_path: Path) -> None:
    sub = tmp_path / "site"
    sub.mkdir()
    (sub / "index.html").write_text("x")
    assert find_index_html(tmp_path) == sub / "index.html"


def test_find_index_html_missing(tmp_path: Path) -> None:
    assert find_index_html(tmp_path) is None


def test_find_index_html_accepts_arbitrary_html_name(tmp_path: Path) -> None:
    """无 index.html 时，顶层任意 .html 可作为入口。"""
    page = tmp_path / "kakeya-3d-chapters.html"
    page.write_text("<html></html>", encoding="utf-8")
    assert find_index_html(tmp_path) == page


# ---- find_build_output ----------------------------------------------------


def test_find_build_output_dist(tmp_path: Path) -> None:
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text("x")
    assert find_build_output(tmp_path) == tmp_path / "dist"


def test_find_build_output_empty_skipped(tmp_path: Path) -> None:
    (tmp_path / "dist").mkdir()  # 空目录
    assert find_build_output(tmp_path) is None


def test_find_build_output_none(tmp_path: Path) -> None:
    assert find_build_output(tmp_path) is None


def test_find_build_output_rejects_outside_hint(tmp_path: Path) -> None:
    """BUG-508：hint 指向仓库外目录时不得当作构建产物。"""
    project = tmp_path / "proj"
    outside = tmp_path / "shared" / "dist"
    project.mkdir()
    outside.mkdir(parents=True)
    (outside / "index.html").write_text("x", encoding="utf-8")
    assert find_build_output(project, hint="../shared/dist") is None


# ---- sync_dir / sync_static_to_public -------------------------------------


def test_sync_dir_copies_and_clears(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    (src / "a.txt").write_text("a")
    (src / "sub").mkdir()
    (src / "sub" / "b.txt").write_text("b")
    # dst 先有旧内容
    dst.mkdir()
    (dst / "old.txt").write_text("old")
    sync_dir(src, dst)
    assert (dst / "a.txt").read_text() == "a"
    assert (dst / "sub" / "b.txt").read_text() == "b"
    assert not (dst / "old.txt").exists()


def test_sync_static_to_public_skips_engineering_files(tmp_path: Path) -> None:
    current = tmp_path / "current"
    public = tmp_path / "public"
    current.mkdir()
    (current / "index.html").write_text("x")
    (current / "package.json").write_text("{}")
    (current / "node_modules").mkdir()
    sync_static_to_public(current, public)
    assert (public / "index.html").exists()
    assert not (public / "package.json").exists()
    assert not (public / "node_modules").exists()


# ---- run_command ----------------------------------------------------------


def test_run_command_success(tmp_path: Path) -> None:
    log = tmp_path / "out.log"
    # 跨平台简单命令：写一个文件
    import sys

    if sys.platform == "win32":
        cmd = "echo hello > result.txt"
    else:
        cmd = "echo hello > result.txt"
    run_command(cmd, cwd=tmp_path, log_path=log)
    assert (tmp_path / "result.txt").exists()
    assert log.is_file()


@pytest.mark.skipif(
    __import__("sys").platform == "win32",
    reason="POSIX shell 语义",
)
def test_run_command_stdout_not_duplicated(tmp_path: Path) -> None:
    """BUG-273：成功路径下 communicate 全量输出不应被二次 drain 翻倍。

    轮询取消循环里，首次 communicate 成功已返回全量 stdout；若再调一次
    communicate(timeout=5) 会重复返回同一份全量，导致构建日志翻倍。
    """
    log = tmp_path / "out.log"
    # 用 seq 5 输出 1..5：命令文本不含 "3"，输出含一个 "3"。
    # 若 stdout 被二次 drain 翻倍，"3" 会出现两次。
    run_command("seq 5", cwd=tmp_path, log_path=log)
    text = log.read_text(encoding="utf-8")
    assert text.count("3") == 1, f"stdout 被重复收集：\n{text}"


def test_run_command_rotates_log_before_append(tmp_path: Path, monkeypatch) -> None:
    """BUG-186：run_command 写日志须经 open_append（先滚动再追加）。"""
    from local_webpage_access import logs as logs_mod

    calls: list[Path] = []
    real = logs_mod.open_append

    def spy(path, **kwargs):
        calls.append(Path(path))
        return real(path, **kwargs)

    monkeypatch.setattr(logs_mod, "open_append", spy)
    log = tmp_path / "out.log"
    log.write_text("old", encoding="utf-8")
    run_command("echo hi", cwd=tmp_path, log_path=log)
    assert any(c == log or c.resolve() == log.resolve() for c in calls)


def test_run_command_failure(tmp_path: Path) -> None:
    log = tmp_path / "out.log"
    with pytest.raises(BuildError):
        run_command("exit 7", cwd=tmp_path, log_path=log)


def test_run_command_failure_message_points_to_build_log(tmp_path: Path) -> None:
    """BUG-875：BUILD_ERROR 消息须附 build.log 完整路径，便于定位细节日志。"""
    log = tmp_path / "apps" / "spa" / "logs" / "build.log"
    with pytest.raises(BuildError, match="详细日志见") as exc_info:
        run_command("exit 1", cwd=tmp_path, log_path=log)
    assert str(log) in str(exc_info.value)


def test_run_command_timeout_message_points_to_build_log(tmp_path: Path) -> None:
    """BUG-875：超时分支同样附日志路径。"""
    log = tmp_path / "build.log"
    with pytest.raises(BuildError, match="详细日志见"):
        run_command("exit 0", cwd=tmp_path, log_path=log, timeout=0)


# ---- BUG-860 / BUG-877：宿主安装命令工具链解析 -------------------------------


def _fake_which(monkeypatch, available: set[str]):
    """桩 hosting.shutil.which：available 集合内的可执行视为在 PATH。"""
    monkeypatch.setattr(
        "local_webpage_access.hosting.shutil.which",
        lambda name, path=None: f"/usr/bin/{name}" if name in available else None,
    )


def test_resolve_host_install_command_pm_on_path_drops_corepack_enable(
    tmp_path: Path, monkeypatch
) -> None:
    """BUG-860：pnpm 已在 PATH → 去掉 corepack enable 前缀直接执行（免 root）。"""
    from local_webpage_access.hosting import _resolve_host_install_command

    _fake_which(monkeypatch, {"pnpm", "corepack"})
    cmd = "corepack enable && pnpm install --frozen-lockfile"
    assert _resolve_host_install_command(cmd, tmp_path) == "pnpm install --frozen-lockfile"


def test_resolve_host_install_command_falls_back_to_corepack_exec(
    tmp_path: Path, monkeypatch
) -> None:
    """BUG-860：pnpm 不在 PATH 但 corepack 可用 → corepack pnpm（免 enable 形式）。"""
    from local_webpage_access.hosting import _resolve_host_install_command

    _fake_which(monkeypatch, {"corepack"})
    cmd = "corepack enable && pnpm install --frozen-lockfile"
    assert _resolve_host_install_command(cmd, tmp_path) == "corepack pnpm install --frozen-lockfile"


def test_resolve_host_install_command_keeps_original_when_nothing_available(
    tmp_path: Path, monkeypatch
) -> None:
    """BUG-860：corepack 与 pnpm 均不可用 → 原样执行（如实失败）。"""
    from local_webpage_access.hosting import _resolve_host_install_command

    _fake_which(monkeypatch, set())
    cmd = "corepack enable && yarn install --frozen-lockfile"
    assert _resolve_host_install_command(cmd, tmp_path) == cmd


def test_resolve_host_install_command_declared_package_manager_via_corepack(
    tmp_path: Path, monkeypatch
) -> None:
    """BUG-877：package.json 声明 pnpm@11.0.1 且 corepack 可用 → corepack 按需取声明版本。"""
    from local_webpage_access.hosting import _resolve_host_install_command

    _fake_which(monkeypatch, {"pnpm", "corepack"})
    (tmp_path / "package.json").write_text(
        '{"packageManager": "pnpm@11.0.1"}', encoding="utf-8"
    )
    cmd = "corepack enable && pnpm install --frozen-lockfile"
    assert (
        _resolve_host_install_command(cmd, tmp_path)
        == "corepack pnpm@11.0.1 install --frozen-lockfile"
    )


def test_resolve_host_install_command_declared_without_corepack_warns_and_uses_path(
    tmp_path: Path, monkeypatch
) -> None:
    """BUG-877：声明了版本但 corepack 不可用 → 退回 PATH 版本（告警留痕）。"""
    from local_webpage_access.hosting import _resolve_host_install_command

    _fake_which(monkeypatch, {"pnpm"})
    (tmp_path / "package.json").write_text(
        '{"packageManager": "pnpm@11.0.1"}', encoding="utf-8"
    )
    cmd = "corepack enable && pnpm install --frozen-lockfile"
    assert _resolve_host_install_command(cmd, tmp_path) == "pnpm install --frozen-lockfile"


def test_resolve_host_install_command_non_corepack_form_untouched(
    tmp_path: Path, monkeypatch
) -> None:
    """非 corepack enable && 形态（npm ci / npm install）原样返回。"""
    from local_webpage_access.hosting import _resolve_host_install_command

    _fake_which(monkeypatch, {"corepack"})
    assert _resolve_host_install_command("npm ci", tmp_path) == "npm ci"
    assert _resolve_host_install_command("npm install", tmp_path) == "npm install"


@pytest.mark.skipif(
    __import__("sys").platform == "win32",
    reason="POSIX 进程组语义",
)
def test_run_command_timeout_kills_child_tree(tmp_path: Path) -> None:
    """BUG-183：超时时杀整个进程树，不残留孙进程孤儿。"""
    import os
    import time

    log = tmp_path / "out.log"
    child_pid_file = tmp_path / "child.pid"
    # shell 启动后台 sleep 子进程、记录其 pid，然后 wait（阻塞直到超时触发）
    cmd = f"sleep 30 & echo $! > {child_pid_file}; wait"
    with pytest.raises(BuildError, match="超时"):
        run_command(cmd, cwd=tmp_path, log_path=log, timeout=2)
    time.sleep(0.5)  # 等 SIGKILL 生效
    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text().strip())
    alive = True
    try:
        os.kill(child_pid, 0)
    except (ProcessLookupError, PermissionError):
        alive = False
    assert not alive, f"孙进程 {child_pid} 超时后仍存活（孤儿，BUG-183 未修）"


# ---- WBS-10 纯静态托管（端到端）------------------------------------------


def test_host_static_end_to_end(workspace: Workspace, registry: Registry, config: Config) -> None:
    _seed_static_instance(workspace, registry, "demo")
    manifest = host_static(workspace, config, registry, "demo")

    # manifest 状态
    assert manifest.status == Status.RUNNING
    assert manifest.runtime == Runtime.SHARED_STATIC
    assert manifest.static is not None
    assert manifest.static.hostPort is not None
    assert manifest.static.gateway in ("caddy", "builtin")
    assert manifest.static.enabled is True

    # public/ 已同步
    assert (workspace.app_public("demo") / "index.html").is_file()

    # registry
    row = registry.get_instance("demo")
    assert row["status"] == "running"

    # 清理子进程
    stop_instance(workspace, config, registry, "demo")


def test_host_static_missing_index_html(
    workspace: Workspace, registry: Registry, config: Config
) -> None:
    _seed_static_instance(workspace, registry, "demo")
    # 删掉 index.html
    (workspace.app_current("demo") / "index.html").unlink()

    with pytest.raises(HostingError, match="HTML"):
        host_static(workspace, config, registry, "demo")

    row = registry.get_instance("demo")
    assert row["status"] == "failed"


def test_host_static_arbitrary_html_entry_serves_root(
    workspace: Workspace, registry: Registry, config: Config
) -> None:
    """仅有非 index 名 HTML 时也应托管，并使 GET / 可用（写入 public/index.html）。"""
    _seed_static_instance(workspace, registry, "chapters")
    current = workspace.app_current("chapters")
    # 换成非 index 入口
    (current / "index.html").unlink(missing_ok=True)
    (current / "kakeya-3d-chapters.html").write_text(
        "<html><body>chapters</body></html>", encoding="utf-8"
    )
    (current / "vendor").mkdir(exist_ok=True)
    (current / "vendor" / "a.js").write_text("1", encoding="utf-8")

    manifest = host_static(workspace, config, registry, "chapters")
    assert manifest.status == Status.RUNNING
    public = workspace.app_public("chapters")
    assert (public / "index.html").is_file()
    assert (public / "kakeya-3d-chapters.html").is_file()
    assert (public / "vendor" / "a.js").is_file()
    stop_instance(workspace, config, registry, "chapters")


def test_host_instance_dispatches_static(
    workspace: Workspace, registry: Registry, config: Config
) -> None:
    _seed_static_instance(workspace, registry, "demo")
    manifest = host_instance(workspace, config, registry, "demo")
    assert manifest.status == Status.RUNNING
    stop_instance(workspace, config, registry, "demo")


def test_stop_instance_disables_gateway(
    workspace: Workspace, registry: Registry, config: Config
) -> None:
    _seed_static_instance(workspace, registry, "demo")
    host_static(workspace, config, registry, "demo")
    ports = registry.allocated_ports()
    assert len(ports) == 1
    held_port = ports[0]

    manifest = stop_instance(workspace, config, registry, "demo")
    assert manifest.status == Status.STOPPED
    # BUG-045：静态实例 stop 后端口登记应保留（与容器路径一致），供 start 复用，
    # 避免端口被重新分配给其他实例而造成跨实例内容混淆。
    assert registry.allocated_ports() == [held_port]
    assert registry.port_owner(held_port) == "demo"
    row = registry.get_instance("demo")
    assert row["status"] == "stopped"


def test_stop_static_then_restart_reuses_port(
    workspace: Workspace, registry: Registry, config: Config
) -> None:
    """BUG-045 回归：静态实例 stop 后再 start 复用同一端口，lanUrl 稳定。"""
    _seed_static_instance(workspace, registry, "demo")
    first = host_static(workspace, config, registry, "demo")
    port = first.network.hostPort
    assert port is not None

    stop_instance(workspace, config, registry, "demo")
    # stop 后端口登记仍在
    assert port in registry.allocated_ports()

    second = host_static(workspace, config, registry, "demo")
    assert second.network.hostPort == port
    # BUG：泄漏兜底——第二次 start 又起了一个 http.server 子进程，必须 stop，
    # 否则跨用例累积孤儿进程会占满端口池（全量测试连跑即红）。
    stop_instance(workspace, config, registry, "demo")


def test_rehost_static_while_listening_reuses_port(
    workspace: Workspace, registry: Registry, config: Config
) -> None:
    """BUG-631：静态站点仍在监听时再次 host（rebuild 路径）必须复用原端口。"""
    _seed_static_instance(workspace, registry, "demo")
    first = host_static(workspace, config, registry, "demo")
    port = first.network.hostPort
    assert port is not None

    second = host_static(workspace, config, registry, "demo")
    assert second.network.hostPort == port
    assert registry.port_owner(port) == "demo"
    stop_instance(workspace, config, registry, "demo")


def test_rehost_static_reallocates_when_live_port_is_not_own_site(
    workspace: Workspace, registry: Registry, config: Config
) -> None:
    """端口上有监听者、但登记已不是本实例活跃站点时，不得把该端口当成可复用。"""
    _seed_static_instance(workspace, registry, "demo")
    first = host_static(workspace, config, registry, "demo")
    port = first.network.hostPort
    assert port is not None
    registry.set_static_enabled("demo", False)

    second = host_static(workspace, config, registry, "demo")
    assert second.network.hostPort != port
    stop_instance(workspace, config, registry, "demo")


def test_ensure_static_port_skips_foreign_listener_when_caddy_is_down(
    workspace: Workspace, registry: Registry, config: Config, monkeypatch
) -> None:
    """BUG-737：Caddy 已停、登记仍 enabled，外部进程占端口时不得复用。"""
    from local_webpage_access.hosting import _ensure_static_port
    from local_webpage_access.static_gateway import StaticGateway

    _seed_static_instance(workspace, registry, "demo")
    registry.upsert_static_site(
        "demo",
        {"root": "public", "gateway": "caddy", "hostPort": 21000, "enabled": True},
    )
    assert registry.allocate_port("demo", 21000)
    site = StaticGateway(workspace, config).site_config_path("demo")
    site.parent.mkdir(parents=True, exist_ok=True)
    site.write_text(":21000 {\n\troot * /tmp\n}\n", encoding="utf-8")
    monkeypatch.setattr("local_webpage_access.hosting.is_port_listening", lambda _p: True)
    monkeypatch.setattr("local_webpage_access.ports.is_port_in_use", lambda _p, **_k: False)
    monkeypatch.setattr(StaticGateway, "detect_backend", lambda self: "caddy")
    monkeypatch.setattr(StaticGateway, "_admin_alive", lambda self, **_k: False)

    port, fresh = _ensure_static_port(workspace, config, registry, "demo")

    assert fresh is True
    assert registry.port_owner(port) == "demo"


def test_ensure_static_port_reuses_when_caddy_still_holds_port(
    workspace: Workspace, registry: Registry, config: Config, monkeypatch
) -> None:
    """本网关仍在线且站点配置绑着该端口时，监听中的端口可以复用。"""
    from local_webpage_access.hosting import _ensure_static_port
    from local_webpage_access.static_gateway import StaticGateway

    _seed_static_instance(workspace, registry, "demo")
    registry.upsert_static_site(
        "demo",
        {"root": "public", "gateway": "caddy", "hostPort": 21000, "enabled": True},
    )
    assert registry.allocate_port("demo", 21000)
    site = StaticGateway(workspace, config).site_config_path("demo")
    site.parent.mkdir(parents=True, exist_ok=True)
    site.write_text(":21000 {\n\troot * /tmp\n}\n", encoding="utf-8")
    monkeypatch.setattr("local_webpage_access.hosting.is_port_listening", lambda _p: True)
    monkeypatch.setattr(StaticGateway, "detect_backend", lambda self: "caddy")
    monkeypatch.setattr(StaticGateway, "_admin_alive", lambda self, **_k: True)
    monkeypatch.setattr(
        "local_webpage_access.hosting._listen_process_names",
        lambda _p: ["caddy"],
    )

    port, fresh = _ensure_static_port(workspace, config, registry, "demo")

    assert fresh is False
    assert port == 21000


def test_stopped_static_port_not_reassigned(
    workspace: Workspace, registry: Registry, config: Config
) -> None:
    """BUG-045 回归：静态实例 stop 后保留的端口不会被分配给另一实例。"""
    _seed_static_instance(workspace, registry, "demo")
    _seed_static_instance(workspace, registry, "other")
    host_static(workspace, config, registry, "demo")
    demo_port = registry.allocated_ports()[0]

    stop_instance(workspace, config, registry, "demo")
    # demo 的端口仍登记在案，other 启动时不应抢到它
    host_static(workspace, config, registry, "other")
    ports = registry.allocated_ports()
    assert demo_port in ports
    assert registry.port_owner(demo_port) == "demo"
    other_port = next(p for p in ports if p != demo_port)
    assert registry.port_owner(other_port) == "other"
    # BUG：泄漏兜底——other 实例的 http.server 子进程仍在跑，必须 stop，
    # 否则跨用例累积孤儿进程会占满端口池（全量测试连跑即红）。
    stop_instance(workspace, config, registry, "other")


# ---- WBS-11 前端构建（mock npm）-------------------------------------------


def test_build_and_host_frontend_success(
    workspace: Workspace, registry: Registry, config: Config, monkeypatch
) -> None:
    _seed_frontend_instance(workspace, registry, "spa")

    # 模拟 npm ci + npm run build：执行时创建 dist/index.html
    def fake_run(cmd, *, cwd, log_path, **kw):
        if "build" in cmd:
            dist = Path(cwd) / "dist"
            dist.mkdir(exist_ok=True)
            (dist / "index.html").write_text("<html>built</html>")

        return _subprocess_completed(0)

    monkeypatch.setattr("local_webpage_access.hosting.run_command", fake_run)

    manifest = build_and_host_frontend(workspace, config, registry, "spa")
    assert manifest.status == Status.RUNNING
    assert (workspace.app_public("spa") / "index.html").is_file()

    # builds 表记录成功
    builds = registry.list_builds("spa")
    assert len(builds) == 1
    assert builds[0]["status"] == "success"

    # BUG-861：前端 SPA 站点生成的站点配置带 try_files history 路由回退
    from local_webpage_access.static_gateway import StaticGateway

    site_conf = StaticGateway(workspace, config).site_config_path("spa")
    assert "try_files {path} {path}/ /index.html" in site_conf.read_text(encoding="utf-8")

    stop_instance(workspace, config, registry, "spa")


def test_host_static_site_config_has_no_spa_fallback(
    workspace: Workspace, registry: Registry, config: Config
) -> None:
    """BUG-861：纯静态站（folder 导入、无 entry.build）保持裸 file_server，不注入回退。"""
    _seed_static_instance(workspace, registry, "plain")

    host_static(workspace, config, registry, "plain")

    from local_webpage_access.static_gateway import StaticGateway

    site_conf = StaticGateway(workspace, config).site_config_path("plain")
    assert "try_files" not in site_conf.read_text(encoding="utf-8")

    stop_instance(workspace, config, registry, "plain")


def test_build_and_host_frontend_resolves_corepack_install(
    workspace: Workspace, registry: Registry, config: Config, monkeypatch
) -> None:
    """BUG-860/877：宿主执行前安装命令经工具链解析（corepack enable 被改写）。"""
    from local_webpage_access.models import EntryConfig, InstanceManifest

    _seed_frontend_instance(workspace, registry, "spa")
    mpath = workspace.app_manifest_path("spa")
    manifest = InstanceManifest.load(mpath)
    manifest.entry = EntryConfig(
        install="corepack enable && pnpm install --frozen-lockfile",
        build="pnpm run build",
    )
    manifest.save(mpath)

    seen: list[str] = []

    def fake_run(cmd, *, cwd, log_path, env=None, **kw):
        seen.append(cmd)
        if "build" in cmd:
            dist = Path(cwd) / "dist"
            dist.mkdir(exist_ok=True)
            (dist / "index.html").write_text("<html>built</html>")
        return _subprocess_completed(0)

    monkeypatch.setattr("local_webpage_access.hosting.run_command", fake_run)
    # pnpm 在 PATH 且 corepack 可用：安装命令应去掉 corepack enable 前缀
    monkeypatch.setattr(
        "local_webpage_access.hosting.shutil.which",
        lambda name, path=None: f"/usr/bin/{name}" if name in ("pnpm", "corepack") else None,
    )

    build_and_host_frontend(workspace, config, registry, "spa")

    assert seen[0] == "pnpm install --frozen-lockfile"

    stop_instance(workspace, config, registry, "spa")


def test_build_and_host_frontend_injects_build_env(
    workspace: Workspace, registry: Registry, config: Config, monkeypatch
) -> None:
    """DEV-132：manifest.buildEnv 注入安装/构建命令环境（os.environ 为底合并）。"""
    from local_webpage_access.models import InstanceManifest

    _seed_frontend_instance(workspace, registry, "spa")
    mpath = workspace.app_manifest_path("spa")
    manifest = InstanceManifest.load(mpath)
    manifest.buildEnv = {"VITE_BASE": "/alias-demo/"}
    manifest.save(mpath)

    seen: dict[str, dict | None] = {}

    def fake_run(cmd, *, cwd, log_path, env=None, **kw):
        seen[cmd] = env
        if "build" in cmd:
            dist = Path(cwd) / "dist"
            dist.mkdir(exist_ok=True)
            (dist / "index.html").write_text("<html>built</html>")
        return _subprocess_completed(0)

    monkeypatch.setattr("local_webpage_access.hosting.run_command", fake_run)

    build_and_host_frontend(workspace, config, registry, "spa")

    import os

    assert len(seen) == 2  # install + build 均注入
    for cmd, env in seen.items():
        assert env is not None, f"{cmd} 未收到 env"
        assert env.get("VITE_BASE") == "/alias-demo/"
        # subprocess env 整体替换语义：须以 os.environ 为底，PATH 不得丢失
        assert env.get("PATH") == os.environ.get("PATH")

    stop_instance(workspace, config, registry, "spa")


def test_build_and_host_frontend_build_failure(
    workspace: Workspace, registry: Registry, config: Config, monkeypatch
) -> None:
    _seed_frontend_instance(workspace, registry, "spa")

    def fake_run(cmd, *, cwd, log_path, **kw):
        raise BuildError("npm run build 失败", command=cmd, exit_code=1)

    monkeypatch.setattr("local_webpage_access.hosting.run_command", fake_run)

    with pytest.raises(BuildError):
        build_and_host_frontend(workspace, config, registry, "spa")

    # 状态：failed
    row = registry.get_instance("spa")
    assert row["status"] == "failed"

    # builds 表记录失败 + error_summary
    builds = registry.list_builds("spa")
    assert builds[0]["status"] == "failed"
    assert builds[0]["error_summary"]

    # 事件记录
    events = registry.list_events("spa")
    assert any(e["event_type"] == "error" for e in events)


def test_build_and_host_frontend_no_artifact(
    workspace: Workspace, registry: Registry, config: Config, monkeypatch
) -> None:
    """构建成功但无产物目录 → BuildError。"""
    _seed_frontend_instance(workspace, registry, "spa")

    def fake_run(cmd, *, cwd, log_path, **kw):
        # 不创建 dist/
        return _subprocess_completed(0)

    monkeypatch.setattr("local_webpage_access.hosting.run_command", fake_run)

    with pytest.raises(BuildError, match="产物"):
        build_and_host_frontend(workspace, config, registry, "spa")

    builds = registry.list_builds("spa")
    assert builds[0]["status"] == "failed"


def test_build_and_host_frontend_subdir_uses_subdir_cwd(
    workspace: Workspace, registry: Registry, config: Config, monkeypatch
) -> None:
    """BUG-503：frontend/ 子目录实例的 install/build 必须在子目录执行，
    否则 npm ci / npm run build 在 current/ 根找不到子包 package.json。"""
    from local_webpage_access.models import EntryConfig

    workspace.ensure_app_dirs("spa-sub")
    current = workspace.app_current("spa-sub")
    fe = current / "frontend"
    fe.mkdir()
    (fe / "package.json").write_text(
        '{"dependencies":{"vue":"^3"},"scripts":{"build":"vite build"}}'
    )

    detection = DetectionResult(
        kind=Kind.NODE,
        runtime=Runtime.SHARED_STATIC,
        servingMode=ServingMode.SHARED_STATIC,
        resourceProfile=ResourceProfile.TINY,
        form="frontend-static",
        confidence="high",
        stack=["vue", "vite"],
        source_subdir="frontend",
    )
    detection.entry = EntryConfig(
        install="npm ci",
        build="npm run build",
        buildOutputDir="frontend/dist",
    )
    manifest = build_manifest_from_detection(
        instance_id="spa-sub",
        display_name="SpaSub",
        detection=detection,
        workspace=workspace,
    )
    manifest.save(workspace.app_manifest_path("spa-sub"))
    registry.upsert_from_manifest(manifest)

    cwds: list[Path] = []

    def fake_run(cmd, *, cwd, log_path, **kw):
        cwds.append(Path(cwd))
        if "build" in cmd:
            dist = Path(cwd) / "dist"
            dist.mkdir(exist_ok=True)
            (dist / "index.html").write_text("<html>built</html>")
        return _subprocess_completed(0)

    monkeypatch.setattr("local_webpage_access.hosting.run_command", fake_run)

    built = build_and_host_frontend(workspace, config, registry, "spa-sub")
    assert built.status == Status.RUNNING
    # install 与 build 都在 frontend/ 子目录执行
    assert cwds and all(cwd == fe for cwd in cwds)
    # 产物来自 frontend/dist，被同步到 public/
    assert (workspace.app_public("spa-sub") / "index.html").is_file()

    stop_instance(workspace, config, registry, "spa-sub")


def test_build_and_host_frontend_rejects_source_subdir_escape(
    workspace: Workspace, registry: Registry, config: Config, monkeypatch
) -> None:
    """BUG-507：被污染的 sourceSubdir=../outside 不得成为 npm cwd。"""
    from local_webpage_access.models import EntryConfig

    workspace.ensure_app_dirs("spa-escape")
    current = workspace.app_current("spa-escape")
    outside = current.parent / "outside"
    outside.mkdir()
    (outside / "package.json").write_text('{"scripts":{"build":"vite build"}}')
    fe = current / "frontend"
    fe.mkdir()
    (fe / "package.json").write_text(
        '{"dependencies":{"vue":"^3"},"scripts":{"build":"vite build"}}'
    )

    detection = DetectionResult(
        kind=Kind.NODE,
        runtime=Runtime.SHARED_STATIC,
        servingMode=ServingMode.SHARED_STATIC,
        resourceProfile=ResourceProfile.TINY,
        form="frontend-static",
        confidence="high",
        stack=["vue", "vite"],
        source_subdir="frontend",
    )
    detection.entry = EntryConfig(
        install="npm ci",
        build="npm run build",
        buildOutputDir="frontend/dist",
    )
    manifest = build_manifest_from_detection(
        instance_id="spa-escape",
        display_name="SpaEscape",
        detection=detection,
        workspace=workspace,
    )
    manifest.save(workspace.app_manifest_path("spa-escape"))
    registry.upsert_from_manifest(manifest)

    import local_webpage_access.hosting as hosting_mod

    real_load = hosting_mod._load_manifest

    def poisoned_load(ws, iid):
        loaded = real_load(ws, iid)
        loaded.sourceSubdir = "../outside"
        return loaded

    monkeypatch.setattr(hosting_mod, "_load_manifest", poisoned_load)

    cwds: list[Path] = []

    def fake_run(cmd, *, cwd, log_path, **kw):
        cwds.append(Path(cwd))
        return _subprocess_completed(0)

    monkeypatch.setattr("local_webpage_access.hosting.run_command", fake_run)

    with pytest.raises(BuildError):
        build_and_host_frontend(workspace, config, registry, "spa-escape")
    assert cwds == []
    assert not any(cwd.resolve() == outside.resolve() for cwd in cwds)


# ---- 辅助 ------------------------------------------------------------------


def _subprocess_completed(returncode: int):
    """构造一个假的 CompletedProcess。"""
    import subprocess

    return subprocess.CompletedProcess(args="cmd", returncode=returncode)


# ---- 回归测试：BUG-001/002/006 -------------------------------------------
#
# BUG-001：嵌套 index.html 未拍平，public/ 根目录缺少首页、健康检查误报成功
# BUG-002：对已运行的静态实例再次 start，旧进程成为孤儿、旧端口泄漏
# BUG-006：stop_instance 对容器实例静默无操作，CLI 仍报"已停止"


def _seed_nested_static_instance(
    workspace: Workspace, registry: Registry, iid: str = "demo"
) -> None:
    """构造一个 index.html 嵌套于子目录 site/ 的静态实例。"""
    workspace.ensure_app_dirs(iid)
    current = workspace.app_current(iid)
    (current / "site").mkdir()
    (current / "site" / "index.html").write_text("<html><body>nested</body></html>")
    (current / "site" / "style.css").write_text("body{}")
    detection = DetectionResult(
        kind=Kind.STATIC,
        runtime=Runtime.SHARED_STATIC,
        servingMode=ServingMode.SHARED_STATIC,
        resourceProfile=ResourceProfile.TINY,
        form="static",
        confidence="high",
    )
    manifest = build_manifest_from_detection(
        instance_id=iid,
        display_name="Demo",
        detection=detection,
        workspace=workspace,
    )
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)


def test_host_static_nested_index_flattened_to_public_root(
    workspace: Workspace, registry: Registry, config: Config
) -> None:
    """BUG-001：index.html 在 site/ 子目录时，应拍平到 public/index.html。"""
    _seed_nested_static_instance(workspace, registry, "demo")
    manifest = host_static(workspace, config, registry, "demo")
    assert manifest.status == Status.RUNNING

    public = workspace.app_public("demo")
    # index.html 已提升到 public/ 根（GET / 命中首页，而非目录列表）
    assert (public / "index.html").is_file()
    assert (public / "style.css").is_file()
    # 整个 current/ 被同步，原 site/ 子目录路径也保留（BUG-004 边界）
    assert (public / "site" / "index.html").is_file()

    stop_instance(workspace, config, registry, "demo")


def test_host_static_nested_index_preserves_root_sibling_resources(
    workspace: Workspace, registry: Registry, config: Config
) -> None:
    """BUG-013：嵌套 index + 根目录同级资源（shared.css/README.md）不应丢失。

    复现：current/site/index.html（入口）与 current/shared.css 同级存在于根。
    修复前只同步 site/，根目录 sibling 全部丢失。
    """
    _seed_nested_static_instance(workspace, registry, "demo")
    current = workspace.app_current("demo")
    # 在 current/ 根目录追加同级资源
    (current / "shared.css").write_text(".shared{}")
    (current / "README.md").write_text("# demo")

    manifest = host_static(workspace, config, registry, "demo")
    assert manifest.status == Status.RUNNING

    public = workspace.app_public("demo")
    # index 所在子目录内容提升到 public/ 根
    assert (public / "index.html").is_file()
    assert (public / "style.css").is_file()
    # 根目录同级资源保留（修复前会丢失）
    assert (public / "shared.css").is_file()
    assert (public / "README.md").is_file()
    # 原子目录路径下的资源也仍在（绝对路径引用可命中）
    assert (public / "site" / "style.css").is_file()

    stop_instance(workspace, config, registry, "demo")


def test_host_static_restart_kills_old_process(
    workspace: Workspace, registry: Registry, config: Config
) -> None:
    """BUG-002：再次 start 应停掉旧进程，不产生孤儿/端口泄漏。"""
    from local_webpage_access.static_gateway import StaticGateway

    _seed_static_instance(workspace, registry, "demo")
    host_static(workspace, config, registry, "demo")

    pid_path = workspace.run / "static-demo.pid"
    assert pid_path.is_file()
    old_pid = int(pid_path.read_text().strip())
    gw = StaticGateway(workspace, config)
    assert gw._pid_alive(old_pid)  # 旧进程确实在跑
    old_port_count = len(registry.allocated_ports())
    assert old_port_count == 1

    # 再次启动（重启用场景）
    manifest = host_static(workspace, config, registry, "demo")
    assert manifest.status == Status.RUNNING

    # 旧进程应已终止，没有孤儿
    assert not gw._pid_alive(old_pid)
    # 仍只有一个端口被占用（没有泄漏第二个端口）
    assert len(registry.allocated_ports()) == 1
    # 新进程在服务
    new_pid = int(pid_path.read_text().strip())
    assert new_pid != old_pid
    assert gw._pid_alive(new_pid)

    stop_instance(workspace, config, registry, "demo")


# ---- 回归测试：BUG-016 ----------------------------------------------------
#
# BUG-016：网关启用失败后已分配端口未回滚。_enable_static 在 gateway.enable
# 抛错时只往上传播异常，端口留在 registry；连续失败耗尽端口池。修复后失败
# 路径释放刚分配的端口。host_container 在 build/up 失败时同理释放实例端口。


def test_enable_static_releases_port_on_gateway_failure(
    workspace: Workspace, registry: Registry, config: Config, monkeypatch
) -> None:
    """BUG-016：gateway.enable 抛错时，_enable_static 应释放刚分配的端口。"""
    from local_webpage_access.hosting import _enable_static
    from local_webpage_access.static_gateway import StaticGateway

    _seed_static_instance(workspace, registry, "demo")
    public = workspace.app_public("demo")
    public.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest_from_detection(
        instance_id="demo",
        display_name="Demo",
        detection=DetectionResult(
            kind=Kind.STATIC,
            runtime=Runtime.SHARED_STATIC,
            servingMode=ServingMode.SHARED_STATIC,
            resourceProfile=ResourceProfile.TINY,
            form="static",
            confidence="high",
        ),
        workspace=workspace,
    )

    # 让 gateway.enable 模拟失败
    def _boom(self, *a, **kw):
        raise RuntimeError("gateway boom")

    monkeypatch.setattr(StaticGateway, "enable", _boom)

    with pytest.raises(RuntimeError, match="gateway boom"):
        _enable_static(workspace, config, registry, "demo", manifest, public)

    # 端口不应残留在 registry
    assert registry.allocated_ports() == []


def test_host_static_releases_port_when_health_check_fails(
    workspace: Workspace, registry: Registry, config: Config, monkeypatch
) -> None:
    """BUG-016 端到端：健康检查失败 → host_static 抛错 → 端口不残留。"""
    from local_webpage_access.static_gateway import StaticGateway

    _seed_static_instance(workspace, registry, "demo")

    # 让 health_check 恒失败，触发 enable 内部回滚 + 抛错
    monkeypatch.setattr(StaticGateway, "health_check", lambda self, port, **kw: False)

    with pytest.raises(Exception):
        host_static(workspace, config, registry, "demo")

    # 失败后端口不应残留
    assert registry.allocated_ports() == []


def test_host_container_releases_port_on_build_failure(
    workspace: Workspace, registry: Registry, config: Config, monkeypatch
) -> None:
    """BUG-016：host_container 在 build/up 阶段失败时应释放实例端口。"""
    from local_webpage_access.hosting import host_container
    from tests.scripts._helpers import make_container_manifest

    workspace.ensure_app_dirs("api")
    m = make_container_manifest("api")
    m.save(workspace.app_manifest_path("api"))
    registry.upsert_from_manifest(m)

    # 让 ensure_available 通过，但 build 阶段抛错
    class _FakeRuntime:
        ensure_available = staticmethod(lambda: None)

        def __init__(self, *a, **kw):
            pass

        def is_running(self, iid):
            return False

        def down(self, iid, **kw):
            pass

        def build(self, iid, **kw):
            raise DockerError("build boom")

    monkeypatch.setattr("local_webpage_access.hosting.DockerRuntime", _FakeRuntime)

    with pytest.raises(DockerError, match="build boom"):
        host_container(workspace, config, registry, "api")

    # 端口不应残留（build 失败前 _ensure_container_port 已分配）
    assert registry.allocated_ports() == []


# ---- 回归测试：BUG-603（issue #16）、S2-08（issue #59）-------------------------
#
# BUG-603：daemon 进程触发的重建（reconcile 自动恢复 / inbox 处理）阶段
#          日志只落 daemon.log，lwa.log 看起来"构建卡死"。修复后非 CLI
#          来源把 lifecycle_stage 镜像进 lwa.log，行首带 source=<来源>；
#          CLI 进程本就把阶段写进 lwa.log，不重复镜像。


class _OkRuntime:
    """build/up 全成功 + 可观测身份的假 runtime。"""

    ensure_available = staticmethod(lambda: None)

    def __init__(self, *a, **kw):
        pass

    def is_running(self, iid):
        return False

    def down(self, iid, **kw):
        pass

    def build(self, iid, **kw):
        return None

    def up(self, iid, **kw):
        return None

    def container_id(self, iid, **kw):
        return "fake-cid"

    def image_id(self, iid, **kw):
        return "fake-iid"


def _seed_ok_container(workspace: Workspace, registry: Registry) -> None:
    from tests.scripts._helpers import make_container_manifest

    workspace.ensure_app_dirs("api")
    m = make_container_manifest("api")
    m.save(workspace.app_manifest_path("api"))
    registry.upsert_from_manifest(m)


def test_host_container_stages_mirrored_to_lwa_log_in_daemon_process(
    workspace: Workspace, registry: Registry, config: Config, monkeypatch
) -> None:
    """BUG-603 / S2-08：非 CLI 来源 lifecycle_stage 全量镜像到 lwa.log，带 source=。"""
    from local_webpage_access.hosting import host_container

    _seed_ok_container(workspace, registry)
    monkeypatch.setattr("local_webpage_access.hosting.DockerRuntime", _OkRuntime)
    monkeypatch.setattr(
        "local_webpage_access.hosting._evaluate_container_verification",
        lambda *a, **k: {"overall_status": "passed"},
    )
    monkeypatch.setenv("LWA_DAEMON_PROCESS", "1")
    monkeypatch.delenv("LWA_OP_SOURCE", raising=False)

    host_container(workspace, config, registry, "api")

    text = (workspace.logs / "lwa.log").read_text(encoding="utf-8")
    for stage in (
        "host_start",
        "dockerfile_ready",
        "compose_ready",
        "compose_build_start",
        "compose_build_done",
        "compose_up_start",
        "compose_up_done",
    ):
        assert f"source=daemon lifecycle_stage instance=api stage={stage}" in text


def test_host_container_stages_not_mirrored_outside_daemon_process(
    workspace: Workspace, registry: Registry, config: Config, monkeypatch
) -> None:
    """S2-08：CLI 来源（默认）不重复镜像，lwa.log 不出现 source= 阶段行。"""
    from local_webpage_access.hosting import host_container

    _seed_ok_container(workspace, registry)
    monkeypatch.setattr("local_webpage_access.hosting.DockerRuntime", _OkRuntime)
    monkeypatch.setattr(
        "local_webpage_access.hosting._evaluate_container_verification",
        lambda *a, **k: {"overall_status": "passed"},
    )
    monkeypatch.delenv("LWA_DAEMON_PROCESS", raising=False)
    monkeypatch.delenv("LWA_OP_SOURCE", raising=False)

    host_container(workspace, config, registry, "api")

    assert not (workspace.logs / "lwa.log").exists()


def test_host_container_keeps_reused_port_on_build_failure(
    workspace: Workspace, registry: Registry, config: Config, monkeypatch
) -> None:
    """复用旧端口时 build 失败不得释放端口登记（与静态网关 BUG-182 对称）。"""
    from local_webpage_access.hosting import host_container
    from tests.scripts._helpers import make_container_manifest

    workspace.ensure_app_dirs("api")
    m = make_container_manifest("api")
    m.container.hostPort = 18080
    m.save(workspace.app_manifest_path("api"))
    registry.upsert_from_manifest(m)
    assert registry.allocate_port("api", 18080)

    class _FakeRuntime:
        ensure_available = staticmethod(lambda: None)

        def __init__(self, *a, **kw):
            pass

        def is_running(self, iid):
            return False

        def down(self, iid, **kw):
            pass

        def build(self, iid, **kw):
            raise DockerError("build boom")

    monkeypatch.setattr("local_webpage_access.hosting.DockerRuntime", _FakeRuntime)
    monkeypatch.setattr("local_webpage_access.hosting.is_port_listening", lambda _p: False)

    with pytest.raises(DockerError, match="build boom"):
        host_container(workspace, config, registry, "api")

    assert 18080 in registry.allocated_ports()
    row = registry.get_container("api")
    assert row is not None
    assert int(row["host_port"]) == 18080


# ---- IMP-006 路径别名端到端 ------------------------------------------------
#
# 覆盖 WBS 006.06 的"端口 + 路径并存"流程：带 path_alias 的静态实例经
# host_static 后，hostPort 仍正常分配（端口可达），同时 network.routeMode=name
# / routeUrl 写入（路径可达）。并验证 Caddy 模式下别名片段落盘、stop 后清理。


def _seed_static_instance_with_alias(
    workspace: Workspace, registry: Registry, iid: str, alias: str
) -> None:
    """构造一个带路径别名的已导入静态实例。"""
    workspace.ensure_app_dirs(iid)
    current = workspace.app_current(iid)
    (current / "index.html").write_text("<html><body>hello</body></html>")
    detection = DetectionResult(
        kind=Kind.STATIC,
        runtime=Runtime.SHARED_STATIC,
        servingMode=ServingMode.SHARED_STATIC,
        resourceProfile=ResourceProfile.TINY,
        form="static",
        confidence="high",
    )
    manifest = build_manifest_from_detection(
        instance_id=iid,
        display_name="Demo",
        detection=detection,
        workspace=workspace,
        path_alias=alias,
    )
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)


def test_host_static_with_alias_port_and_path_coexist(
    workspace: Workspace, registry: Registry, config: Config
) -> None:
    """IMP-006：带别名的静态实例托管后端口与路径并存（builtin 模式）。

    builtin 模式无统一入口，别名片段不落盘，但 manifest 层的 routeMode/routeUrl
    仍正确写入——这是后端无关的纯数据层断言。hostPort 照常分配保证端口可达。
    """
    # 固定 lanIp + staticGatewayPort，使 routeUrl 可确定断言
    config.lanIpStrategy = "manual"
    config.manualLanIp = "192.168.1.100"
    config.staticGatewayPort = 8080

    _seed_static_instance_with_alias(workspace, registry, "demo", "voiceprint")
    manifest = host_static(workspace, config, registry, "demo")

    # 端口侧：hostPort 已分配（端口可达）
    assert manifest.network.hostPort is not None
    assert manifest.network.hostPort in registry.allocated_ports()
    # 路径侧：routeMode=name，routeUrl 指向统一入口
    assert manifest.network.routeMode == "name"
    assert manifest.network.routeHost == "voiceprint"
    assert manifest.network.routeUrl == "http://192.168.1.100:8080/voiceprint/"
    # lanUrl 仍保留（端口直达）
    assert manifest.network.lanUrl is not None
    # static 配置保留别名
    assert manifest.static is not None
    assert manifest.static.routeMode == "name"
    assert manifest.static.routeHost == "voiceprint"

    stop_instance(workspace, config, registry, "demo")


def test_host_static_without_alias_keeps_port_mode(
    workspace: Workspace, registry: Registry, config: Config
) -> None:
    """IMP-006：不传别名时默认行为不变（routeMode=port，无 routeUrl）。"""
    _seed_static_instance(workspace, registry, "demo")
    manifest = host_static(workspace, config, registry, "demo")

    assert manifest.network.routeMode == "port"
    assert manifest.network.routeHost is None
    assert manifest.network.routeUrl is None
    assert manifest.network.hostPort is not None
    assert manifest.static is not None
    assert manifest.static.routeMode == "port"
    assert manifest.static.routeHost is None

    stop_instance(workspace, config, registry, "demo")


def test_host_static_alias_writes_caddy_fragment(
    workspace: Workspace, registry: Registry, config: Config, monkeypatch
) -> None:
    """IMP-006：Caddy 模式下 host_static 应写出别名片段，stop 后清理。

    monkeypatch detect_backend→caddy 并 stub reload_all（无真实 caddy 二进制），
    验证 generate_alias_config 的落盘内容：handle_path 去前缀 + reverse_proxy +
    无尾斜杠 301。
    """
    from local_webpage_access.static_gateway import StaticGateway

    config.staticGateway = "caddy"
    config.lanIpStrategy = "manual"
    config.manualLanIp = "192.168.1.100"
    config.staticGatewayPort = 8080

    _seed_static_instance_with_alias(workspace, registry, "demo", "voiceprint")

    # 探测不到真实 caddy 时 detect_backend 会降级 builtin；强制走 caddy 路径
    monkeypatch.setattr(StaticGateway, "detect_backend", lambda self: "caddy")
    monkeypatch.setattr(StaticGateway, "reload_all", lambda self: None)
    monkeypatch.setattr(StaticGateway, "_sync_main_config", lambda self: None)

    manifest = host_static(workspace, config, registry, "demo")

    # 别名片段已落盘
    fragment = workspace.app_alias_config("demo")
    assert fragment.is_file()
    text = fragment.read_text(encoding="utf-8")
    assert "handle_path /voiceprint/*" in text
    assert "reverse_proxy 127.0.0.1:" in text
    assert "handle /voiceprint" in text
    assert "redir * /voiceprint/ permanent" in text

    # manifest 仍正确
    assert manifest.network.routeMode == "name"
    assert manifest.network.routeUrl == "http://192.168.1.100:8080/voiceprint/"

    stop_instance(workspace, config, registry, "demo")
    # stop 后别名片段已清理（disable 在 caddy 路径删除片段 + reload）
    assert not fragment.exists()


# ---- BUG-422：静态派生路径回写 ---------------------------------------------


def test_derived_path_host_static_refreshes_stale_paths(
    workspace: Workspace, registry: Registry, config: Config
) -> None:
    """host_static / _enable_static 成功后刷新 appPath 与 gatewayConfigPath。"""
    from local_webpage_access.models import InstanceManifest

    _seed_static_instance(workspace, registry, "demo")
    manifest = InstanceManifest.load(workspace.app_manifest_path("demo"))
    external_zip = "/external/old.zip"
    manifest.appPath = "/old/workspace/apps/demo/current"
    manifest.sourceZipPath = external_zip
    if manifest.static is None:
        from local_webpage_access.models import StaticConfig

        manifest.static = StaticConfig(root="public", gateway="builtin")
    manifest.static.gatewayConfigPath = "/old/workspace/static-gateway/sites/demo.conf"
    manifest.save(workspace.app_manifest_path("demo"))
    registry.upsert_from_manifest(manifest)

    try:
        hosted = host_static(workspace, config, registry, "demo")
        expected_app = str(workspace.app_current("demo"))
        expected_gw = str(workspace.app_gateway_config("demo"))
        assert hosted.appPath == expected_app
        assert hosted.sourceZipPath == external_zip
        assert hosted.static is not None
        assert hosted.static.gatewayConfigPath == expected_gw

        reloaded = InstanceManifest.load(workspace.app_manifest_path("demo"))
        assert reloaded.appPath == expected_app
        assert reloaded.sourceZipPath == external_zip
        assert reloaded.static is not None
        assert reloaded.static.gatewayConfigPath == expected_gw

        row = registry.get_instance("demo")
        assert row is not None
        assert row["app_path"] == expected_app
        assert row["source_zip_path"] == external_zip
        srow = registry.get_static_site("demo")
        assert srow is not None
        assert srow["gateway_config_path"] == expected_gw
    finally:
        stop_instance(workspace, config, registry, "demo")


# ---- BUG-887：find_build_output 子包 hint 父目录回退 -------------------------


def test_find_build_output_hint_parent_fallback(tmp_path: Path) -> None:
    """BUG-887：hint 为 <pkg>/dist 而实际产物在 <pkg>/build（vite outDir、
    CRA/Docusaurus）时，先在子包目录内按常规候选名找，不误报未找到产物。"""
    pkg = tmp_path / "excalidraw-app"
    (pkg / "build").mkdir(parents=True)
    (pkg / "build" / "index.html").write_text("x", encoding="utf-8")
    assert find_build_output(tmp_path, hint="excalidraw-app/dist") == pkg / "build"


def test_find_build_output_hint_parent_priority_over_root(tmp_path: Path) -> None:
    """BUG-887：子包内的产物候选优先于根级同名目录（更具体的归属）。"""
    pkg = tmp_path / "web"
    (pkg / "out").mkdir(parents=True)
    (pkg / "out" / "index.html").write_text("x", encoding="utf-8")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text("x", encoding="utf-8")
    assert find_build_output(tmp_path, hint="web/dist") == pkg / "out"


def test_find_build_output_hint_parent_missing_falls_back_to_root(tmp_path: Path) -> None:
    """BUG-887：子包目录无任何产物候选时，仍回退根级常规扫描。"""
    (tmp_path / "web").mkdir()
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text("x", encoding="utf-8")
    assert find_build_output(tmp_path, hint="web/dist") == tmp_path / "dist"


def test_find_build_output_hint_parent_outside_not_scanned(tmp_path: Path) -> None:
    """BUG-887：越界 hint（../shared/dist）的父目录在仓库外，不得扫描其下候选。"""
    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "shared" / "build"
    outside.mkdir(parents=True)
    (outside / "index.html").write_text("x", encoding="utf-8")
    assert find_build_output(project, hint="../shared/dist") is None


# ---- BUG-889：宿主构建命令的 corepack 工具链解析 ------------------------------


def test_resolve_host_build_command_declared_version_via_corepack(
    tmp_path: Path, monkeypatch
) -> None:
    """BUG-889：build 与 install 同一工具链——声明 yarn@4.5.1 且 corepack 可用
    时，yarn 开头的 build 也走 corepack 固定版本（不再裸跑 127/版本漂移）。"""
    from local_webpage_access.hosting import _resolve_host_build_command

    _fake_which(monkeypatch, {"corepack"})
    (tmp_path / "package.json").write_text(
        '{"packageManager": "yarn@4.5.1"}', encoding="utf-8"
    )
    assert (
        _resolve_host_build_command("yarn workspace excalidraw-app run build", tmp_path)
        == "corepack yarn@4.5.1 workspace excalidraw-app run build"
    )


def test_resolve_host_build_command_pm_on_path_runs_as_is(
    tmp_path: Path, monkeypatch
) -> None:
    """BUG-889：PATH 已有 pnpm 且未声明版本 → build 原样执行。"""
    from local_webpage_access.hosting import _resolve_host_build_command

    _fake_which(monkeypatch, {"pnpm", "corepack"})
    cmd = "pnpm --filter web run build"
    assert _resolve_host_build_command(cmd, tmp_path) == cmd


def test_resolve_host_build_command_corepack_proxy_when_pm_missing(
    tmp_path: Path, monkeypatch
) -> None:
    """BUG-889：pnpm 不在 PATH 但 corepack 可用 → corepack pnpm 代理形式。"""
    from local_webpage_access.hosting import _resolve_host_build_command

    _fake_which(monkeypatch, {"corepack"})
    assert (
        _resolve_host_build_command("pnpm run build", tmp_path)
        == "corepack pnpm run build"
    )


def test_resolve_host_build_command_npm_and_non_pm_untouched(
    tmp_path: Path, monkeypatch
) -> None:
    """BUG-889：npm run build / 非包管理器命令不经 corepack 改写。"""
    from local_webpage_access.hosting import _resolve_host_build_command

    _fake_which(monkeypatch, {"corepack"})
    assert _resolve_host_build_command("npm run build", tmp_path) == "npm run build"
    assert _resolve_host_build_command("vite build", tmp_path) == "vite build"


def test_read_declared_package_manager_strips_sha_suffix(tmp_path: Path) -> None:
    """BUG-889：packageManager 的 +sha 校验后缀须剥离——corepack CLI 的
    <pm>@<version> 参数只接受纯版本号。"""
    from local_webpage_access.hosting import _read_declared_package_manager

    (tmp_path / "package.json").write_text(
        '{"packageManager": "pnpm@9.12.0+sha512.1234abcd"}', encoding="utf-8"
    )
    assert _read_declared_package_manager(tmp_path) == ("pnpm", "9.12.0")


def test_resolve_host_build_command_and_chain_segments_all_resolved(
    tmp_path: Path, monkeypatch
) -> None:
    """BUG-896：monorepo 拓扑构建是 ``&&`` 多段命令——每段 yarn/pnpm 都须
    走同一工具链解析，不能只改写第一段（第二段裸跑仍会 127/版本漂移）。"""
    from local_webpage_access.hosting import _resolve_host_build_command

    _fake_which(monkeypatch, {"corepack"})
    (tmp_path / "package.json").write_text(
        '{"packageManager": "yarn@4.5.1"}', encoding="utf-8"
    )
    cmd = (
        "yarn workspace @excalidraw/core run build"
        " && yarn workspace excalidraw-app run build"
    )
    assert _resolve_host_build_command(cmd, tmp_path) == (
        "corepack yarn@4.5.1 workspace @excalidraw/core run build"
        " && corepack yarn@4.5.1 workspace excalidraw-app run build"
    )


def test_resolve_host_build_command_mixed_chain_only_pm_segments(
    tmp_path: Path, monkeypatch
) -> None:
    """BUG-896 对照：非 yarn/pnpm 开头的分段（npm/echo）保持原文不改写。"""
    from local_webpage_access.hosting import _resolve_host_build_command

    _fake_which(monkeypatch, {"corepack"})
    cmd = "npm run build && echo done"
    assert _resolve_host_build_command(cmd, tmp_path) == cmd


def test_read_declared_package_manager_rejects_non_version_chars(
    tmp_path: Path, monkeypatch
) -> None:
    """BUG-897：packageManager 版本号含白名单外字符（如注入串）时按未声明
    处理——版本号会被拼进宿主 sh -c 与 Dockerfile RUN，不得原样放行。"""
    from local_webpage_access.hosting import (
        _read_declared_package_manager,
        _resolve_host_build_command,
    )

    (tmp_path / "package.json").write_text(
        '{"packageManager": "pnpm@9.12.0; touch /tmp/lwa-pwned"}', encoding="utf-8"
    )
    assert _read_declared_package_manager(tmp_path) is None
    _fake_which(monkeypatch, {"corepack"})
    assert _resolve_host_build_command("pnpm run build", tmp_path) == "corepack pnpm run build"


# ---- BUG-902：构建子进程的 yarn/pnpm corepack shim ---------------------------


def test_frontend_needs_pm_shims_when_install_rewritten_to_corepack(
    tmp_path: Path, monkeypatch
) -> None:
    """BUG-902：install 已改写为 corepack 直跑且宿主无 yarn/pnpm——build 脚本
    内部嵌套调用（build: "yarn run compile"）没有入口会 127，需要 shim。"""
    from local_webpage_access.hosting import _frontend_needs_pm_shims

    _fake_which(monkeypatch, {"corepack"})
    assert _frontend_needs_pm_shims(
        tmp_path, "corepack pnpm install --frozen-lockfile", "npm run build"
    )


def test_frontend_needs_pm_shims_via_declared_pm_or_lockfile(
    tmp_path: Path, monkeypatch
) -> None:
    """BUG-902：命令本身不含 corepack（npm ci + npm run build），但项目声明
    packageManager=yarn 或存在 yarn.lock/pnpm-lock.yaml 时嵌套调用同样可能
    发生——宿主缺 yarn/pnpm 即需要 shim。"""
    from local_webpage_access.hosting import _frontend_needs_pm_shims

    _fake_which(monkeypatch, {"corepack"})
    (tmp_path / "package.json").write_text(
        '{"packageManager": "yarn@4.5.1"}', encoding="utf-8"
    )
    assert _frontend_needs_pm_shims(tmp_path, "npm ci", "npm run build")

    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
    assert _frontend_needs_pm_shims(tmp_path, "npm ci", "npm run build")


def test_frontend_needs_pm_shims_uses_build_path_not_process_path(
    tmp_path: Path, monkeypatch
) -> None:
    """CHK-431：进程 PATH 有 yarn/pnpm 而构建 PATH 没有时，仍要补 shim。"""
    from local_webpage_access.hosting import _frontend_needs_pm_shims

    def which(name: str, path: str | None = None) -> str | None:
        if path is not None:
            return None
        if name in {"yarn", "pnpm"}:
            return f"/usr/bin/{name}"
        return None

    monkeypatch.setattr("local_webpage_access.hosting.shutil.which", which)
    assert _frontend_needs_pm_shims(
        tmp_path,
        "corepack pnpm install --frozen-lockfile",
        "npm run build",
        path="/opt/empty-build-bin",
    )


def test_frontend_needs_pm_shims_false_when_both_pm_on_path(
    tmp_path: Path, monkeypatch
) -> None:
    """BUG-902：yarn 与 pnpm 都已在 PATH 时嵌套调用本就可行，无需 shim。"""
    from local_webpage_access.hosting import _frontend_needs_pm_shims

    _fake_which(monkeypatch, {"yarn", "pnpm", "corepack"})
    assert not _frontend_needs_pm_shims(
        tmp_path, "corepack pnpm install --frozen-lockfile", "npm run build"
    )


def test_frontend_needs_pm_shims_false_for_plain_npm_project(
    tmp_path: Path, monkeypatch
) -> None:
    """BUG-902 对照：纯 npm 项目（无 packageManager/无 yarn·pnpm 锁文件、
    命令不经 corepack）不引入 shim。"""
    from local_webpage_access.hosting import _frontend_needs_pm_shims

    _fake_which(monkeypatch, {"corepack"})
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    assert not _frontend_needs_pm_shims(tmp_path, "npm ci", "npm run build")


def test_ensure_corepack_shim_dir_none_without_corepack(
    workspace: Workspace, monkeypatch
) -> None:
    """BUG-902：corepack 不可用时返回 None（按原链路执行，如实失败）。"""
    from local_webpage_access.hosting import _ensure_corepack_shim_dir

    _fake_which(monkeypatch, set())
    assert _ensure_corepack_shim_dir(workspace) is None


def test_ensure_corepack_shim_dir_creates_and_reuses(
    workspace: Workspace, tmp_path: Path, monkeypatch
) -> None:
    """BUG-902：corepack enable --install-directory 在工作区生成 yarn/pnpm
    shim（免 root），再次调用幂等复用同一目录。"""
    import os
    import stat

    from local_webpage_access.hosting import _ensure_corepack_shim_dir

    fake = tmp_path / "corepack"
    fake.write_text(
        "#!/bin/sh\n"
        '# fake corepack：corepack enable --install-directory <dir> yarn pnpm\n'
        'for n in yarn yarnpkg pnpm pnpx; do touch "$3/$n"; done\n',
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(
        "local_webpage_access.hosting.shutil.which",
        lambda name: str(fake) if name == "corepack" else None,
    )

    shim_dir = _ensure_corepack_shim_dir(workspace)
    assert shim_dir is not None
    assert (Path(shim_dir) / "yarn").is_file()
    assert (Path(shim_dir) / "pnpm").is_file()
    # 幂等复用（不重复执行 corepack enable）
    assert _ensure_corepack_shim_dir(workspace) == shim_dir
    assert os.pathsep not in shim_dir
