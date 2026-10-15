"""每工作区独立的 Caddy admin 端口分配与登记（BUG-783）。

背景：所有工作区的 Caddy master 默认把 admin API 绑在同一点
``127.0.0.1:2019``（Caddy 默认），``caddy reload`` 实质是向"谁在 :2019 上听"
推送配置（POST /load），而不是向本工作区的 master 推送。BUG-176 只保护了
POST /stop，配置加载路径无归属保护——已删除工作区的孤儿 daemon 曾借此把空
配置推进共享 :2019，清空真实工作区的网关运行配置（2026-10-13 S1 退出验收）。

方案（design/2-achievement/2026-10-15-S2-实施与交付记录.md#s2-07-显式三态判定与-caddy-admin-归属保护）：
每个工作区在首次需要 admin 时分配一个独立端口（20201–20500），登记到
``run/admin-port`` 并渲染进主 Caddyfile 的全局选项块（``admin 127.0.0.1:<port>``）。
升级前未分配的工作区一律回退 legacy :2019，行为与旧版完全一致；
``caddy_start`` 在拉起新 master 前会把仍在线的本工作区 legacy master 优雅停掉
（BUG-176 同款归属校验），实现运行中工作区的平滑迁移。

该改动在结构上阻断旧代码孤儿：真实工作区升级后 master 不再监听 :2019，
旧版本 daemon 向 :2019 的推送只会打到它自己的 master 或无人监听。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from local_webpage_access.paths import Workspace
from local_webpage_access.ports import is_port_listening

# legacy 行为兼容点：未分配的工作区（含全部存量测试）继续使用 :2019。
LEGACY_ADMIN_PORT = 2019
# 分配区间：避开 2019 与并行测试常用的 21000–21050 段。
_ADMIN_PORT_FIRST = 20201
_ADMIN_PORT_SPAN = 300
# 登记文件名（workspace.run 下）。
_ADMIN_PORT_FILE = "admin-port"


def admin_port_path(workspace: Workspace) -> Path:
    """admin 端口登记文件路径。"""
    return workspace.run / _ADMIN_PORT_FILE


def read_admin_port(workspace: Workspace) -> int | None:
    """读取本工作区已分配的 admin 端口；未分配/登记损坏返回 None（调用方回退 2019）。"""
    try:
        text = admin_port_path(workspace).read_text(encoding="utf-8").strip()
        port = int(text)
    except (OSError, ValueError):
        return None
    if port <= 0 or port > 65535:
        return None
    return port


def _candidate_ports(workspace: Workspace) -> list[int]:
    """确定性候选序列：按工作区根路径散列取起点，冲突时顺序后移。"""
    digest = hashlib.sha256(str(workspace.root.resolve()).encode("utf-8")).digest()
    start = _ADMIN_PORT_FIRST + digest[0] % _ADMIN_PORT_SPAN
    return [((start - _ADMIN_PORT_FIRST + i) % _ADMIN_PORT_SPAN) + _ADMIN_PORT_FIRST for i in range(_ADMIN_PORT_SPAN)]


def ensure_admin_port(workspace: Workspace) -> int:
    """分配（或读取）本工作区的 admin 端口并落盘登记。

    已登记则直读返回（跨重启稳定）。首次分配用 O_EXCL 原子创建防并发双分配；
    候选端口已有监听者（含其他工作区已分配的端口）时顺延。
    """
    existing = read_admin_port(workspace)
    if existing is not None:
        return existing
    path = admin_port_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    for port in _candidate_ports(workspace):
        if is_port_listening(port):
            continue
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # 并发分配已完成：尊重先写者。
            registered = read_admin_port(workspace)
            if registered is not None:
                return registered
            continue
        try:
            os.write(fd, f"{port}\n".encode("ascii"))
        finally:
            os.close(fd)
        return port
    raise RuntimeError(
        f"无法为工作区分配 Caddy admin 端口（{_ADMIN_PORT_FIRST}–"
        f"{_ADMIN_PORT_FIRST + _ADMIN_PORT_SPAN - 1} 均被占用）：{workspace.root}"
    )


def admin_base(workspace: Workspace) -> str:
    """本工作区 Caddy admin API 的 base URL（未分配时回退 legacy :2019）。"""
    port = read_admin_port(workspace) or LEGACY_ADMIN_PORT
    return f"http://127.0.0.1:{port}"


def admin_address(workspace: Workspace) -> str:
    """本工作区 Caddy admin 监听地址（``127.0.0.1:<port>``，供 --address 等使用）。"""
    port = read_admin_port(workspace) or LEGACY_ADMIN_PORT
    return f"127.0.0.1:{port}"
