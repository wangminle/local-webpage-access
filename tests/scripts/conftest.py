"""pytest 共享夹具。"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from local_webpage_access.paths import Workspace


# ---- Docker 可用性判定（WBS-28.15）----------------------------------------
# 真实 Docker 集成测试需要 docker 守护进程；CI 与开发机可能不具备。
# 用 ``@pytest.mark.requires_docker`` 或 ``@requires_docker`` 守卫。


def _docker_available() -> bool:
    """宿主机是否存在 docker 命令（不保证守护进程运行）。"""
    return shutil.which("docker") is not None


requires_docker = pytest.mark.skipif(
    not _docker_available(),
    reason="需要 docker 命令（设置 PATH 或安装 Docker 后启用）",
)

requires_docker_daemon = pytest.mark.skipif(
    not _docker_available(),
    reason="需要 docker 守护进程",
)


# DEV-166：会真实调用 Docker 构建/重建、单例耗时 30s 以上的验收用例不进默认门禁，
# 与 ``test_docker_*_integration.py`` 一致，设置 ``LWA_RUN_DOCKER_TESTS=1`` 才执行。
requires_docker_opt_in = pytest.mark.skipif(
    os.environ.get("LWA_RUN_DOCKER_TESTS") != "1" or not _docker_available(),
    reason="设置 LWA_RUN_DOCKER_TESTS=1（且需 docker 命令）以启用真实 Docker 耗时用例",
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "requires_docker: 需要宿主机 docker 命令")
    config.addinivalue_line("markers", "xdist_group(name): pytest-xdist 并行分组（DEV-166）")


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool:  # noqa: ARG001
    """BUG-120：忽略同步冲突残留文件，避免收集阶段 ModuleNotFoundError。"""
    name = collection_path.name
    return "sync-conflict" in name


# ---- 通用夹具 --------------------------------------------------------------


@pytest.fixture()
def workspace_root(tmp_path: Path) -> Path:
    """返回一个空的工作区根目录。"""
    return tmp_path / "ws"


@pytest.fixture()
def workspace(workspace_root: Path) -> Workspace:
    """返回一个已创建顶层目录的 Workspace。"""
    ws = Workspace(workspace_root)
    ws.ensure_workspace_dirs()
    return ws


@pytest.fixture()
def registry(workspace_root: Path):
    """打开一个临时 registry，测试结束自动关闭。"""
    from local_webpage_access.registry import Registry

    workspace_root.joinpath("registry").mkdir(parents=True, exist_ok=True)
    reg = Registry(workspace_root / "registry" / "local-web.db")
    reg.open()
    yield reg
    reg.close()


@pytest.fixture()
def config(workspace_root: Path):
    from local_webpage_access.config import Config, PortPool

    # BUG-121：测试默认强制 builtin，避免本机有 caddy 时把临时 Caddyfile
    # 通过全局 admin :2019 reload 进生产 master。
    return Config(
        staticGateway="builtin",
        portPool=PortPool(start=21000, end=21050),
    )


# ---- 测试套件进程泄漏兜底网（BUG：测试不幂等 / BUG-450）-------------------
# 1) StaticGateway 可能留下 ``http.server`` 占住测试端口池；
#    原范围仅 [21000, 21050]。Agent 验收另用 22000/23000 段，operations 测试
#    曾走默认 18000 段——命令行带 pytest 工作区路径，须按路径回收，不得误杀正式实例。
# 2) ``init_workspace`` → ``maybe_start_manager`` 可能留下
#    ``manager_service`` / ``daemon``（工作区在 pytest 临时目录，常占 :17800/:17801），
#    旧泄漏网扫不到 → 曾存活十余天（OPS-071）。
# 会话起止清理，保证套件幂等；正式工作区路径不含 pytest 标记，不会被误杀。
# pytest 临时工作区路径特征（含跨 session 已删目录仍留在 cmdline 的孤儿）
_PYTEST_WS_MARK = re.compile(
    r"(?:[/\\]pytest-of-[^/\\ \"']+|/pytest-\d+|\\pytest-\d+)",
    re.IGNORECASE,
)
_LWA_SERVICE_WS = re.compile(
    r"local_webpage_access\.(?:manager_service|daemon)\b.*?"
    r"--workspace(?:\s+|=)([^\s\"']+)",
    re.IGNORECASE,
)


# DEV-166：pytest-xdist 并行（gate 默认 ``-n 3 --dist loadgroup``）。
# 多个文件真实占用共享的固定测试端口池（21000/22000/23000 段 http.server、
# 端口登记复用、doctor 占位探测等），分散到不同 worker 会互相抢端口。
# 曾试过按 worker 平移端口池，但大量用例断言字面端口（21000/21001…），平移会让
# 它们整片失败——因此改为：这批文件固定归入同一个 xdist 分组（同一 worker 串行），
# 其余每个文件独立成组（等价 ``--dist loadfile``，保留文件内顺序语义）。
_REAL_PORT_FILES = frozenset(
    {
        "test_agent_api",
        "test_agent_m1_acceptance",
        "test_agent_operations",
        "test_agent_recovery",
        "test_access",
        "test_bug704_chk349_agent_guards",
        "test_bug706_709_agent_m1_guards",
        "test_bug753_stage_observability",
        "test_bug783_admin_port",
        "test_caddy_semantics",
        "test_daemon",
        "test_doctor",
        "test_e2e_acceptance",
        "test_host_container",
        "test_hosting",
        "test_gateway_service",
        "test_gateway_start_race",
        "test_https_first_delivery",
        "test_issue43_gateway_adopt",
        "test_lifecycle",
        "test_manager_service",
        "test_ports",
        "test_s2_03_rebuild_txn",
        "test_s2_07_ownership",
        "test_s2_audit_p1_fixes",
        "test_s3_02_consistency",
        "test_static_gateway",
    }
)
_REAL_PORT_GROUP = "real-ports"


def _xdist_group_for(module_stem: str) -> str:
    """文件 → xdist 分组名：真实占端口的文件共用一组，其余按文件分组。"""
    return _REAL_PORT_GROUP if module_stem in _REAL_PORT_FILES else module_stem


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:  # noqa: ARG001
    for item in items:
        item.add_marker(pytest.mark.xdist_group(_xdist_group_for(item.path.stem)))


def _parse_pid_cmdline_lines(out: str) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        m_pid = re.match(r"(\d+)\t?(.*)$", line)
        if not m_pid:
            m_pid = re.match(r"(\d+)\s+(.*)$", line)
        if not m_pid:
            continue
        rows.append((int(m_pid.group(1)), m_pid.group(2)))
    return rows


def _pgrep_lf(pattern: str) -> str:
    """跨平台枚举命中 pattern 的进程命令行；失败返回空串。"""
    try:
        if os.name == "nt":
            from local_webpage_access.platform_detect import subprocess_hidden_kwargs

            # PowerShell -match 用单引号包正则，避免二次转义地狱
            ps = (
                "Get-CimInstance Win32_Process | "
                f"Where-Object {{ $_.CommandLine -match '{pattern}' }} | "
                'ForEach-Object { "$($_.ProcessId)`t$($_.CommandLine)" }'
            )
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True,
                text=True,
                timeout=20,
                **subprocess_hidden_kwargs(),
            )
            return proc.stdout or ""
        # -lf：Darwin 上 -af 只输出 PID、无命令行（复盘 §10.2-C1）
        proc = subprocess.run(
            ["pgrep", "-lf", pattern],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return proc.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _list_http_server_pids_on_test_ports() -> set[int]:
    """枚举测试泄漏的 ``http.server`` 进程 PID。

    必须同时含 ``http.server`` 与 pytest 临时工作区路径标记。
    端口池也可能被普通服务使用，不能作为测试归属证据；此约束同时保护
    跨会话陈旧回收和会话结束差集清理。
    """
    pids: set[int] = set()
    out = _pgrep_lf(r"http\.server" if os.name == "nt" else "http.server")
    for pid, cmdline in _parse_pid_cmdline_lines(out):
        if re.search(r"\bhttp\.server\b", cmdline) and _PYTEST_WS_MARK.search(cmdline):
            pids.add(pid)
    return pids


# DEV-166：被中断（Ctrl-C / kill / 超时）的旧会话没机会跑 teardown，会话级“起止差集”
# 又把它们算进启动快照，导致孤儿永远不被回收（实测残留 39 个、存活 1 天以上）。
# 单个测试不会跑几小时，因此“属于测试 + 存活超阈值”的 http.server 一定是陈旧孤儿。
_STALE_HTTP_SERVER_SECONDS = 3 * 3600
_ETIME_RE = re.compile(r"^(?:(?:(\d+)-)?(?:(\d+):)?(\d+):)(\d+)$")


def _parse_etime(text: str) -> int | None:
    """解析 ``ps -o etime=``（``[[dd-]hh:]mm:ss``）为秒；无法解析返回 ``None``。"""
    m = _ETIME_RE.match(text.strip())
    if not m:
        return None
    days, hours, minutes, seconds = (int(g) if g else 0 for g in m.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def _process_age_seconds(pid: int) -> int | None:
    """进程存活秒数（仅 POSIX；Windows / 查询失败返回 ``None``，调用方保守不杀）。"""
    if os.name == "nt":
        return None
    try:
        proc = subprocess.run(
            ["ps", "-o", "etime=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return _parse_etime(proc.stdout or "")


def _list_stale_http_server_pids(
    *, max_age_seconds: int = _STALE_HTTP_SERVER_SECONDS
) -> set[int]:
    """测试 ``http.server`` 中存活超过阈值的陈旧孤儿 PID（跨会话残留）。"""
    stale: set[int] = set()
    for pid in _list_http_server_pids_on_test_ports():
        age = _process_age_seconds(pid)
        if age is not None and age > max_age_seconds:
            stale.add(pid)
    return stale


def stop_workspace_test_builtins(workspace: Workspace) -> None:
    """停掉本工作区测试拉起的 builtin ``http.server``（夹具 teardown 用）。"""
    from local_webpage_access.config import Config
    from local_webpage_access.static_gateway import StaticGateway

    StaticGateway(workspace, Config(staticGateway="builtin")).stop_all_builtin()


def _list_lwa_service_pids_on_pytest_workspaces(
    *,
    only_under: str | os.PathLike[str] | None = None,
    include_orphans: bool = False,
) -> set[int]:
    """枚举 ``manager_service``/``daemon`` 且 ``--workspace`` 落在 pytest 临时目录的 PID。

    BUG-450：只按「pytest 工作区路径标记」归属，避免误杀正式 :17800 管理页。

    CHK-178/P2：避免清理并发 pytest 会话（含 xdist worker）仍在使用的服务进程。

    * ``only_under``：给出本会话临时根（``tmp_path_factory.getbasetemp()``）时，
      只返回工作区落在该根下的 PID（=本会话自己拉起的进程）。未给时按旧的
      「命中 pytest 临时标记即归属」行为枚举。
    * ``include_orphans``：额外纳入工作区目录已不存在（已删除）的进程。
      这些是跨 session 的真孤儿，清理恒安全——不会误伤仍活着的并发会话
      （其工作区目录此刻必然存在）。
    """
    pids: set[int] = set()
    pattern = (
        r"local_webpage_access\.(manager_service|daemon)"
        if os.name == "nt"
        else "local_webpage_access.(manager_service|daemon)"
    )
    only_under_norm = os.path.realpath(os.fspath(only_under)) if only_under else None
    out = _pgrep_lf(pattern)
    for pid, cmdline in _parse_pid_cmdline_lines(out):
        m = _LWA_SERVICE_WS.search(cmdline)
        if not m:
            continue
        ws = m.group(1)
        if only_under_norm is not None:
            try:
                ws_norm = os.path.realpath(ws)
            except (OSError, ValueError):
                ws_norm = ws
            under_this = ws_norm == only_under_norm or ws_norm.startswith(only_under_norm + os.sep)
            if not under_this:
                # 非本会话进程：仅在显式要求且确认其工作区已删除（孤儿）时纳入
                if include_orphans and _PYTEST_WS_MARK.search(ws) and not os.path.isdir(ws):
                    pids.add(pid)
                continue
            pids.add(pid)
            continue
        if _PYTEST_WS_MARK.search(ws):
            pids.add(pid)
    return pids


def _kill_pid(pid: int) -> None:
    try:
        if os.name == "nt":
            from local_webpage_access.platform_detect import subprocess_hidden_kwargs

            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                **subprocess_hidden_kwargs(),
            )
        else:
            import signal

            os.kill(pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        pass


@pytest.fixture(autouse=True, scope="session")
def _cleanup_orphan_test_processes(tmp_path_factory) -> None:
    """会话级 autouse：清理 http.server 与 pytest 临时工作区上的 manager/daemon 孤儿。

    * http.server：session 差集（启动快照 → 结束差集），避免误杀会话前已有进程。
    * manager/daemon：CHK-178/P2 前按 pytest 工作区路径**绝对清理**（起止各一次），
      会误杀并发 pytest 会话/xdist worker 仍在使用的服务进程。

      CHK-178/P2 后改为按**本会话临时根**过滤：起止只清理本会话拉起的进程；
      另在启动阶段额外清理工作区目录已被删除的真·孤儿（跨 session 历史残留，
      OPS-071），其工作区此刻必然不存在，绝不会误伤仍活着的并发会话。
    """
    session_basetemp = str(tmp_path_factory.getbasetemp())
    # 启动：本会话残留（极少见）+ 工作区已不存在的真孤儿
    own_pids = _list_lwa_service_pids_on_pytest_workspaces(only_under=session_basetemp)
    orphan_pids = (
        _list_lwa_service_pids_on_pytest_workspaces(
            only_under=session_basetemp, include_orphans=True
        )
        - own_pids
    )
    for pid in sorted(own_pids | orphan_pids):
        _kill_pid(pid)
    # DEV-166：先回收被中断旧会话遗留的陈旧 http.server，再取启动快照
    for pid in sorted(_list_stale_http_server_pids()):
        _kill_pid(pid)
    initial_http = _list_http_server_pids_on_test_ports()
    yield
    leaked_http = _list_http_server_pids_on_test_ports() - initial_http
    for pid in sorted(leaked_http):
        _kill_pid(pid)
    # 结束：只清理本会话自己拉起的进程，绝不触碰并发会话
    for pid in sorted(_list_lwa_service_pids_on_pytest_workspaces(only_under=session_basetemp)):
        _kill_pid(pid)


@pytest.fixture(autouse=True)
def _no_real_docker_tag(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """S2-03：单元测试不触发真实 ``docker tag``（先建后换会为旧镜像打 :prev 标签）。

    真实 Docker 集成测试（LWA_RUN_DOCKER_TESTS=1）保持真实行为；
    需要验证标签逻辑的用例自行 monkeypatch ``rebuild_txn.tag_image``。
    """
    if os.environ.get("LWA_RUN_DOCKER_TESTS") == "1":
        return
    from local_webpage_access import rebuild_txn

    monkeypatch.setattr(rebuild_txn, "tag_image", lambda source_ref, target_ref: None)
