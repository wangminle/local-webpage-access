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
    config.addinivalue_line(
        "markers", "requires_docker: 需要宿主机 docker 命令"
    )


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

    return Config(portPool=PortPool(start=21000, end=21050))


# ---- 测试套件进程泄漏兜底网（BUG：测试不幂等）------------------------------
# 部分用例会通过 StaticGateway 启动真实 ``python -m http.server`` 子进程，
# 若用例自身漏了 stop teardown（或 stop 失败），进程会泄漏为孤儿并占住
# 端口池 [21000, 21050]，导致后续用例 ``PortError: 端口池已耗尽``，全量
# 测试连跑第二遍即转红。此 fixture 在会话结束时扫描并清理这类孤儿进程，
# 作为最后一道防泄漏网，保证测试套件幂等可重复运行。
_TEST_PORT_POOL_START = 21000
_TEST_PORT_POOL_END = 21050


def _list_http_server_pids_on_test_ports() -> set[int]:
    """枚举本机监听测试端口池且命令行命中 ``http.server`` 的进程 PID。

    仅匹配同时满足以下两条的进程，避免误杀：
    1. 命令行包含 ``http.server``；
    2. 通过 ``http.server <port>`` 启动且端口落在 ``[21000, 21050]``。
    """
    pids: set[int] = set()
    port_pat = re.compile(r"http\.server\s+(\d+)\b")
    try:
        if os.name == "nt":
            ps = (
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.CommandLine -match 'http\\.server' } | "
                "For-Each-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"
            )
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True,
                text=True,
                timeout=20,
            )
            out = proc.stdout or ""
        else:
            proc = subprocess.run(
                ["pgrep", "-af", "http.server"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            out = proc.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return pids

    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        m_pid = re.match(r"(\d+)\t?(.*)$", line)
        if not m_pid:
            # pgrep 输出形如 "12345 /usr/bin/python -m http.server 21000"
            m_pid = re.match(r"(\d+)\s+(.*)$", line)
        if not m_pid:
            continue
        pid = int(m_pid.group(1))
        cmdline = m_pid.group(2)
        mport = port_pat.search(cmdline)
        if mport and _TEST_PORT_POOL_START <= int(mport.group(1)) <= _TEST_PORT_POOL_END:
            pids.add(pid)
    return pids


def _kill_pid(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
            )
        else:
            import signal

            os.kill(pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        pass


@pytest.fixture(autouse=True, scope="session")
def _cleanup_orphan_http_servers() -> None:
    """会话级 autouse：结束时清理本次测试套件产生的 http.server 孤儿进程。

    策略：在 session 启动时快照已有进程集合，结束时再次枚举，差集中（会话
    期间新出现且仍存活）即为泄漏的孤儿。即便个别用例补好了 teardown，这里
    也能兜住 stop 失败 / 进程残留的边界情形。
    """
    initial = _list_http_server_pids_on_test_ports()
    yield
    leaked = _list_http_server_pids_on_test_ports() - initial
    for pid in sorted(leaked):
        _kill_pid(pid)
