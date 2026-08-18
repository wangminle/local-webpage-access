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


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "requires_docker: 需要宿主机 docker 命令")


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
# 1) StaticGateway 可能留下 ``http.server`` 占住端口池 [21000, 21050]；
# 2) ``init_workspace`` → ``maybe_start_manager`` 可能留下
#    ``manager_service`` / ``daemon``（工作区在 pytest 临时目录，常占 :17800/:17801），
#    旧泄漏网扫不到 → 曾存活十余天（OPS-071）。
# 会话起止清理，保证套件幂等；正式工作区路径不含 pytest 标记，不会被误杀。
_TEST_PORT_POOL_START = 21000
_TEST_PORT_POOL_END = 21050
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
    """枚举本机监听测试端口池且命令行命中 ``http.server`` 的进程 PID。

    仅匹配同时满足以下两条的进程，避免误杀：
    1. 命令行包含 ``http.server``；
    2. 通过 ``http.server <port>`` 启动且端口落在 ``[21000, 21050]``。
    """
    pids: set[int] = set()
    port_pat = re.compile(r"http\.server\s+(\d+)\b")
    out = _pgrep_lf(r"http\.server" if os.name == "nt" else "http.server")
    for pid, cmdline in _parse_pid_cmdline_lines(out):
        mport = port_pat.search(cmdline)
        if mport and _TEST_PORT_POOL_START <= int(mport.group(1)) <= _TEST_PORT_POOL_END:
            pids.add(pid)
    return pids


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
    initial_http = _list_http_server_pids_on_test_ports()
    yield
    leaked_http = _list_http_server_pids_on_test_ports() - initial_http
    for pid in sorted(leaked_http):
        _kill_pid(pid)
    # 结束：只清理本会话自己拉起的进程，绝不触碰并发会话
    for pid in sorted(_list_lwa_service_pids_on_pytest_workspaces(only_under=session_basetemp)):
        _kill_pid(pid)
