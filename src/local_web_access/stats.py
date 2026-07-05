"""资源监控与统计（WBS-19）。

提供：
* :func:`host_resources` —— 整机内存 / 负载 / 磁盘（WBS-19.03/04/05）；
* :func:`instance_resources` —— 单实例的目录大小、镜像大小、容器资源
  （WBS-19.06/07/08/09）；
* :func:`collect_and_store` —— 采集并写入 registry 的 resources 表（WBS-19.10）；
* :func:`all_instance_resources` —— 全部实例资源汇总。

跨平台降级（WBS-19.11，设计 §16.4）：``/proc/meminfo`` 与 ``/proc/loadavg``
仅在 Linux 可用，非 Linux 返回 ``None``；容器资源与目录大小在所有平台可用。
资源采集失败**绝不影响实例运行**：所有探测都在 try/except 内，失败返回 ``None``。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from local_web_access.config import Config
from local_web_access.logging import get_logger
from local_web_access.paths import Workspace
from local_web_access.registry import Registry

log = get_logger("stats")

_PROC_MEMINFO = Path("/proc/meminfo")
_PROC_LOADAVG = Path("/proc/loadavg")


# ---- 整机资源 ---------------------------------------------------------------


@dataclass
class HostResources:
    """整机资源快照。非 Linux 下主机指标为 ``None``。"""

    mem_total_bytes: int | None = None
    mem_available_bytes: int | None = None
    load_avg_1m: float | None = None
    load_avg_5m: float | None = None
    disk_total_bytes: int | None = None
    disk_used_bytes: int | None = None
    platform: str = field(default_factory=lambda: sys.platform)

    @property
    def mem_used_bytes(self) -> int | None:
        if self.mem_total_bytes is None or self.mem_available_bytes is None:
            return None
        return max(0, self.mem_total_bytes - self.mem_available_bytes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "memTotalBytes": self.mem_total_bytes,
            "memAvailableBytes": self.mem_available_bytes,
            "memUsedBytes": self.mem_used_bytes,
            "loadAvg1m": self.load_avg_1m,
            "loadAvg5m": self.load_avg_5m,
            "diskTotalBytes": self.disk_total_bytes,
            "diskUsedBytes": self.disk_used_bytes,
            "platform": self.platform,
        }


def host_resources(*, root: Path | None = None) -> HostResources:
    """采集整机资源（WBS-19.03/04/05）。

    ``root`` 指定磁盘占用的统计根（默认工作区根目录所在分区）。
    """
    info = HostResources()
    info.mem_total_bytes, info.mem_available_bytes = _read_meminfo()
    info.load_avg_1m, info.load_avg_5m = _read_loadavg()
    info.disk_total_bytes, info.disk_used_bytes = _disk_usage(root)
    return info


def _read_meminfo() -> tuple[int | None, int | None]:
    """从 /proc/meminfo 读取 MemTotal / MemAvailable（WBS-19.03）。"""
    if not _PROC_MEMINFO.is_file():
        return None, None
    try:
        total = available = None
        for line in _PROC_MEMINFO.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                total = _parse_meminfo_line(line)
            elif line.startswith("MemAvailable:"):
                available = _parse_meminfo_line(line)
        return total, available
    except Exception:  # noqa: BLE001
        return None, None


def _parse_meminfo_line(line: str) -> int | None:
    parts = line.split()
    if len(parts) >= 2:
        try:
            return int(parts[1]) * 1024  # kB → bytes
        except ValueError:
            return None
    return None


def _read_loadavg() -> tuple[float | None, float | None]:
    """从 /proc/loadavg 读取 1m / 5m 负载（WBS-19.04）。"""
    if not _PROC_LOADAVG.is_file():
        return None, None
    try:
        parts = _PROC_LOADAVG.read_text(encoding="utf-8").split()
        return float(parts[0]), float(parts[1])
    except Exception:  # noqa: BLE001
        return None, None


def _disk_usage(root: Path | None) -> tuple[int | None, int | None]:
    """获取 root 所在分区的总容量与已用（WBS-19.05）。"""
    target = str(root) if root else "."
    usage = shutil.disk_usage(target)
    return usage.total, usage.used


# ---- 实例资源 ---------------------------------------------------------------


@dataclass
class InstanceResources:
    """单实例资源快照。"""

    instance_id: str
    source_size_bytes: int | None = None
    public_size_bytes: int | None = None
    data_size_bytes: int | None = None
    image_size_bytes: int | None = None
    last_memory_bytes: int | None = None
    last_cpu_percent: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "instanceId": self.instance_id,
            "sourceSizeBytes": self.source_size_bytes,
            "publicSizeBytes": self.public_size_bytes,
            "dataSizeBytes": self.data_size_bytes,
            "imageSizeBytes": self.image_size_bytes,
            "lastMemoryBytes": self.last_memory_bytes,
            "lastCpuPercent": self.last_cpu_percent,
        }


def instance_resources(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
    *,
    collect_container: bool = True,
) -> InstanceResources:
    """采集单实例资源（WBS-19.06~09）。

    * 目录大小（source/public/data）对所有实例可用；
    * 镜像大小仅容器实例；
    * 容器 CPU/内存通过 ``docker stats --no-stream``（WBS-19.02），
      ``collect_container=False`` 可跳过（如批量采集时单独控制）。
    """
    info = InstanceResources(instance_id=instance_id)
    info.source_size_bytes = _dir_size(workspace.app_source(instance_id))
    info.public_size_bytes = _dir_size(workspace.app_public(instance_id))
    info.data_size_bytes = _dir_size(workspace.app_data(instance_id))

    row = registry.get_instance(instance_id)
    if row and row["runtime"] == "docker-compose":
        info.image_size_bytes = _image_size(instance_id, registry)
        if collect_container:
            mem, cpu = _container_stats(instance_id)
            info.last_memory_bytes = mem
            info.last_cpu_percent = cpu
    return info


def all_instance_resources(
    workspace: Workspace, config: Config, registry: Registry
) -> list[InstanceResources]:
    """全部实例资源汇总（静态实例不含容器指标）。"""
    return [
        instance_resources(workspace, config, registry, r["id"])
        for r in registry.list_instances()
    ]


def collect_and_store(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
) -> InstanceResources:
    """采集并写入 resources 表（WBS-19.10）。"""
    info = instance_resources(workspace, config, registry, instance_id)
    registry.upsert_resources(
        instance_id,
        source_size_bytes=info.source_size_bytes,
        public_size_bytes=info.public_size_bytes,
        data_size_bytes=info.data_size_bytes,
        image_size_bytes=info.image_size_bytes,
        last_memory_bytes=info.last_memory_bytes,
        last_cpu_percent=info.last_cpu_percent,
    )
    return info


# ---- 目录大小 ---------------------------------------------------------------


def _dir_size(path: Path) -> int | None:
    """递归统计目录大小（字节）；不存在返回 ``None``（WBS-19.06/07/08）。"""
    if not path.exists():
        return None
    try:
        total = 0
        for dirpath, _dirs, files in os.walk(path):
            for f in files:
                fp = Path(dirpath, f)
                try:
                    total += fp.stat().st_size
                except OSError:
                    continue
        return total
    except Exception:  # noqa: BLE001
        return None


# ---- 容器 / 镜像资源 --------------------------------------------------------


def _image_size(instance_id: str, registry: Registry) -> int | None:
    """容器镜像大小（WBS-19.09）：``docker image inspect <image> --format {{.Size}}``。"""
    row = registry.get_container(instance_id)
    if not row:
        return None
    image = row.get("image_id") or row.get("image")
    if not image:
        return None
    try:
        out = subprocess.run(
            ["docker", "image", "inspect", image, "--format", "{{.Size}}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if out.returncode == 0:
            return int(out.stdout.strip())
    except Exception:  # noqa: BLE001
        return None
    return None


def _container_stats(instance_id: str) -> tuple[int | None, float | None]:
    """容器 CPU/内存（WBS-19.02）：``docker stats --no-stream --format <json>``。

    返回 ``(memory_bytes, cpu_percent)``。失败返回 ``(None, None)``。
    """
    try:
        out = subprocess.run(
            [
                "docker", "stats", "--no-stream",
                "--format", "{{json .}}",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if out.returncode != 0:
            return None, None
        return _parse_container_stats(out.stdout, instance_id)
    except Exception:  # noqa: BLE001
        return None, None


def _parse_container_stats(stdout: str, instance_id: str) -> tuple[int | None, float | None]:
    """从 ``docker stats`` 输出中匹配实例对应行。

    Compose 容器名形如 ``lwa-<id>``（当前模板）或 ``lwa-<id>-app``（兼容早期/默认
    Compose 命名），必须精确匹配，避免 ``api`` 误命中 ``api2``。
    """
    candidates = {f"lwa-{instance_id}".lower(), f"lwa-{instance_id}-app".lower()}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = str(data.get("Name", "")).strip().lstrip("/").lower()
        if name in candidates:
            return _extract_mem_cpu(data)
    # 未匹配到具体实例
    return None, None


def _extract_mem_cpu(data: dict) -> tuple[int | None, float | None]:
    mem = _parse_mem_usage(data.get("MemUsage"))
    cpu = _parse_cpu(data.get("CPUPerc"))
    return mem, cpu


def _parse_mem_usage(value: Any) -> int | None:
    """``docker stats`` MemUsage 形如 ``"12.5MiB / 512MiB"``。"""
    if not value:
        return None
    try:
        used = str(value).split("/")[0].strip()
        return _parse_size(used)
    except Exception:  # noqa: BLE001
        return None


def _parse_cpu(value: Any) -> float | None:
    """``docker stats`` CPUPerc 形如 ``"0.45%"``。"""
    if value is None:
        return None
    try:
        return float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def _parse_size(text: str) -> int | None:
    """把 ``"12.5MiB"`` / ``"512MB"`` 等解析为字节数。"""
    text = text.strip()
    units = {
        "b": 1,
        "kb": 1000, "kib": 1024,
        "mb": 1_000_000, "mib": 1024 * 1024,
        "gb": 1_000_000_000, "gib": 1024 * 1024 * 1024,
        "tb": 1_000_000_000_000, "tib": 1024 ** 4,
    }
    import re

    m = re.match(r"^([\d.]+)\s*([a-zA-Z]+)$", text)
    if not m:
        try:
            return int(float(text))
        except ValueError:
            return None
    num = float(m.group(1))
    unit = m.group(2).lower()
    if unit not in units:
        return None
    return int(num * units[unit])


__all__ = [
    "HostResources",
    "InstanceResources",
    "host_resources",
    "instance_resources",
    "all_instance_resources",
    "collect_and_store",
]
