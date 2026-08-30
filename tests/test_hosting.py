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
    from tests._helpers import make_container_manifest

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


# ---- 回归测试：BUG-603（issue #16）----------------------------------------
#
# BUG-603：daemon 进程触发的重建（reconcile 自动恢复 / inbox 处理）阶段
#          日志只落 daemon.log，lwa.log 看起来"构建卡死"。修复后 daemon
#          进程标记（LWA_DAEMON_PROCESS=1）存在时，lifecycle_stage 镜像进
#          lwa.log；CLI / manager 进程行为不变。


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
    from tests._helpers import make_container_manifest

    workspace.ensure_app_dirs("api")
    m = make_container_manifest("api")
    m.save(workspace.app_manifest_path("api"))
    registry.upsert_from_manifest(m)


def test_host_container_stages_mirrored_to_lwa_log_in_daemon_process(
    workspace: Workspace, registry: Registry, config: Config, monkeypatch
) -> None:
    """BUG-603：daemon 进程内 lifecycle_stage 全量镜像到 lwa.log。"""
    from local_webpage_access.hosting import host_container

    _seed_ok_container(workspace, registry)
    monkeypatch.setattr("local_webpage_access.hosting.DockerRuntime", _OkRuntime)
    monkeypatch.setattr(
        "local_webpage_access.hosting._evaluate_container_verification",
        lambda *a, **k: {"overall_status": "passed"},
    )
    monkeypatch.setenv("LWA_DAEMON_PROCESS", "1")

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
        assert f"lifecycle_stage instance=api stage={stage}" in text


def test_host_container_stages_not_mirrored_outside_daemon_process(
    workspace: Workspace, registry: Registry, config: Config, monkeypatch
) -> None:
    """BUG-603：无 daemon 标记（CLI/manager 进程）时不镜像，行为不变。"""
    from local_webpage_access.hosting import host_container

    _seed_ok_container(workspace, registry)
    monkeypatch.setattr("local_webpage_access.hosting.DockerRuntime", _OkRuntime)
    monkeypatch.setattr(
        "local_webpage_access.hosting._evaluate_container_verification",
        lambda *a, **k: {"overall_status": "passed"},
    )
    monkeypatch.delenv("LWA_DAEMON_PROCESS", raising=False)

    host_container(workspace, config, registry, "api")

    assert not (workspace.logs / "lwa.log").exists()


def test_host_container_keeps_reused_port_on_build_failure(
    workspace: Workspace, registry: Registry, config: Config, monkeypatch
) -> None:
    """复用旧端口时 build 失败不得释放端口登记（与静态网关 BUG-182 对称）。"""
    from local_webpage_access.hosting import host_container
    from tests._helpers import make_container_manifest

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
    assert "redir /voiceprint/ permanent" in text

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
