"""托管流程测试（WBS-10 / WBS-11）。

静态流程用真实 builtin 网关做端到端；
前端构建流程用 monkeypatch 模拟 npm 命令，避免依赖 Node 环境。
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from local_web_access.config import Config, PortPool
from local_web_access.errors import BuildError, DockerError, HostingError
from local_web_access.hosting import (
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
from local_web_access.importer import build_manifest_from_detection
from local_web_access.models import Kind, ResourceProfile, Runtime, ServingMode, Status
from local_web_access.paths import Workspace
from local_web_access.registry import Registry
from local_web_access.scanner import DetectionResult


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
    return Config(portPool=PortPool(start=21000, end=21050))


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
    (current / "package.json").write_text('{"dependencies":{"react":"^18"},"scripts":{"build":"vite build"}}')
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
    from local_web_access.models import EntryConfig

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


def test_run_command_failure(tmp_path: Path) -> None:
    log = tmp_path / "out.log"
    with pytest.raises(BuildError):
        run_command("exit 7", cwd=tmp_path, log_path=log)


# ---- WBS-10 纯静态托管（端到端）------------------------------------------


def test_host_static_end_to_end(
    workspace: Workspace, registry: Registry, config: Config
) -> None:
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

    with pytest.raises(HostingError, match="index.html"):
        host_static(workspace, config, registry, "demo")

    row = registry.get_instance("demo")
    assert row["status"] == "failed"


def test_host_instance_dispatches_static(
    workspace: Workspace, registry: Registry, config: Config
) -> None:
    _seed_static_instance(workspace, registry, "demo")
    manifest = host_instance(workspace, config, registry, "demo")
    assert manifest.status == Status.RUNNING
    stop_instance(workspace, config, registry, "demo")


def test_host_instance_dispatches_container(
    workspace: Workspace, registry: Registry, config: Config, monkeypatch
) -> None:
    """Phase 3：host_instance 对 docker-compose 实例应派发到 host_container。

    强制 Docker 不可用，前置检查会抛 DockerError（而非旧的 HostingError），
    从而证明派发确实走进了 host_container 分支。
    """
    from tests._helpers import make_container_manifest

    def _unavailable():
        raise DockerError("Docker 不可用")

    monkeypatch.setattr(
        "local_web_access.hosting.DockerRuntime.ensure_available", staticmethod(_unavailable)
    )

    workspace.ensure_app_dirs("api")
    m = make_container_manifest("api")
    m.save(workspace.app_manifest_path("api"))
    registry.upsert_from_manifest(m)

    # Docker 不可用 → 前置检查抛 DockerError（证明已派发到 host_container）
    with pytest.raises(DockerError, match="不可用"):
        host_instance(workspace, config, registry, "api")


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
    # BUG-028：静态实例 stop 后端口登记应保留（与容器路径一致），供 start 复用，
    # 避免端口被重新分配给其他实例而造成跨实例内容混淆。
    assert registry.allocated_ports() == [held_port]
    assert registry.port_owner(held_port) == "demo"
    row = registry.get_instance("demo")
    assert row["status"] == "stopped"


def test_stop_static_then_restart_reuses_port(
    workspace: Workspace, registry: Registry, config: Config
) -> None:
    """BUG-028 回归：静态实例 stop 后再 start 复用同一端口，lanUrl 稳定。"""
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
    """BUG-028 回归：静态实例 stop 后保留的端口不会被分配给另一实例。"""
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
        from local_web_access.hosting import subprocess as _sp

        return _subprocess_completed(0)
    monkeypatch.setattr("local_web_access.hosting.run_command", fake_run)

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
    monkeypatch.setattr("local_web_access.hosting.run_command", fake_run)

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
    monkeypatch.setattr("local_web_access.hosting.run_command", fake_run)

    with pytest.raises(BuildError, match="产物"):
        build_and_host_frontend(workspace, config, registry, "spa")

    builds = registry.list_builds("spa")
    assert builds[0]["status"] == "failed"


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


def _seed_nested_static_instance(workspace: Workspace, registry: Registry, iid: str = "demo") -> None:
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
    from local_web_access.static_gateway import StaticGateway

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


def test_stop_instance_dispatches_container_runtime(
    workspace: Workspace, registry: Registry, config: Config, monkeypatch
) -> None:
    """Phase 3：stop 对 docker-compose 实例派发到 compose stop。

    BUG-006 原要求"对容器实例 stop 明确报错而非静默无操作"——Phase 3 起
    容器实例已支持 stop，故断言改为：确实调用了 docker compose stop。
    """
    from tests._helpers import make_container_manifest

    workspace.ensure_app_dirs("api")
    m = make_container_manifest("api")
    m.save(workspace.app_manifest_path("api"))
    registry.upsert_from_manifest(m)

    stopped = {"called": False}

    class _FakeRuntime:
        def __init__(self, *a, **kw):
            pass

        def stop(self, iid, **kw):
            stopped["called"] = True

    monkeypatch.setattr("local_web_access.hosting.DockerRuntime", _FakeRuntime)
    manifest = stop_instance(workspace, config, registry, "api")
    assert stopped["called"] is True
    assert manifest.status == Status.STOPPED
    row = registry.get_instance("api")
    assert row["status"] == "stopped"
    assert row["desired_state"] == "stopped"


# ---- 回归测试：BUG-016 ----------------------------------------------------
#
# BUG-016：网关启用失败后已分配端口未回滚。_enable_static 在 gateway.enable
# 抛错时只往上传播异常，端口留在 registry；连续失败耗尽端口池。修复后失败
# 路径释放刚分配的端口。host_container 在 build/up 失败时同理释放实例端口。


def test_enable_static_releases_port_on_gateway_failure(
    workspace: Workspace, registry: Registry, config: Config, monkeypatch
) -> None:
    """BUG-016：gateway.enable 抛错时，_enable_static 应释放刚分配的端口。"""
    from local_web_access.hosting import _enable_static
    from local_web_access.models import EntryConfig
    from local_web_access.static_gateway import StaticGateway

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
    from local_web_access.static_gateway import StaticGateway

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
    from local_web_access.hosting import host_container
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

    monkeypatch.setattr("local_web_access.hosting.DockerRuntime", _FakeRuntime)

    with pytest.raises(DockerError, match="build boom"):
        host_container(workspace, config, registry, "api")

    # 端口不应残留（build 失败前 _ensure_container_port 已分配）
    assert registry.allocated_ports() == []
