"""IMP-069：LWA 运行环境的 Tailscale 只读探测及进程内共享快照。"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REFRESH_SECONDS = 30
STALE_SECONDS = 60
PROBE_TIMEOUT_SECONDS = 2


def find_tailscale() -> str | None:
    """兼容服务 PATH 与 macOS GUI / Windows 常规安装。"""
    found = shutil.which("tailscale")
    if found:
        return found
    candidates: list[Path] = []
    if sys.platform == "darwin":
        candidates = [
            Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale"),
            Path("/usr/local/bin/tailscale"),
            Path("/opt/homebrew/bin/tailscale"),
        ]
    elif sys.platform == "win32":
        candidates = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tailscale/tailscale.exe"
        ]
    return next((str(p) for p in candidates if p.is_file() and os.access(p, os.X_OK)), None)


def _unavailable(state: str, reason: str = "") -> dict[str, Any]:
    return {
        "available": False,
        "state": state,
        "reason": reason or state,
        "ipv4": None,
        "ipv6": None,
        "selectedIp": None,
    }


def probe_tailscale() -> dict[str, Any]:
    """每轮最多一个只读子进程；只输出本机字段，不泄漏 tailnet peers。"""
    executable = find_tailscale()
    if not executable:
        return _unavailable("not_installed")
    try:
        proc = subprocess.run(
            [executable, "status", "--json", "--peers=false"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
        if proc.returncode:
            return _unavailable("unavailable", "command_failed")
        data = json.loads(proc.stdout)
        if not isinstance(data, dict):
            return _unavailable("unknown", "invalid_status")
        state = data.get("BackendState")
        own = data.get("Self")
        if state != "Running" or not isinstance(own, dict) or own.get("Online") is not True:
            return _unavailable(str(state or "unknown"), "not_connected")
        addresses = data.get("TailscaleIPs") or own.get("TailscaleIPs") or []
        if not isinstance(addresses, list):
            return _unavailable("unknown", "invalid_addresses")
        ips: dict[int, str] = {}
        for value in addresses:
            if not isinstance(value, str):
                continue
            try:
                addr = ipaddress.ip_address(value)
            except ValueError:
                continue
            if not (
                addr.is_loopback or addr.is_unspecified or addr.is_multicast or addr.is_link_local
            ):
                ips.setdefault(addr.version, str(addr))
        if not ips:
            return _unavailable("Running", "no_address")
        return {
            "available": True,
            "state": "Running",
            "reason": "",
            "ipv4": ips.get(4),
            "ipv6": ips.get(6),
            "selectedIp": ips.get(4) or ips[6],
        }
    except subprocess.TimeoutExpired:
        return _unavailable("unavailable", "timeout")
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeError):
        return _unavailable("unknown", "probe_failed")


class NetworkContextCache:
    """只由后台任务更新；API 不触发探测，过期不暴露旧地址。"""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._stop = threading.Event()
        self._data = _unavailable("unknown")
        self._updated: float | None = None
        self._checked_at: str | None = None
        self.thread = threading.Thread(target=self._run, name="lwa-tailscale-probe", daemon=True)

    def refresh(self) -> None:
        # 防止同一缓存意外出现两个并发采集者。
        if not self._refresh_lock.acquire(blocking=False):
            return
        try:
            try:
                data = probe_tailscale()
            except Exception:  # 单轮异常不终结后台检测，也不继续提供旧 available。
                data = _unavailable("unknown", "probe_failed")
            with self._lock:
                self._data = data
                self._updated = self._clock()
                self._checked_at = datetime.now(timezone.utc).isoformat()
        finally:
            self._refresh_lock.release()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            age = None if self._updated is None else max(0, self._clock() - self._updated)
            data = dict(self._data)
            if age is not None and age >= STALE_SECONDS:
                data = _unavailable("stale")
            return {
                **data,
                "checkedAt": self._checked_at,
                "validForSeconds": 0 if age is None else max(0, STALE_SECONDS - age),
            }

    def _run(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            if self._stop.wait(REFRESH_SECONDS):
                break

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.thread.join(timeout=PROBE_TIMEOUT_SECONDS + 0.5)
