"""IMP-069：本机组网探测与共享缓存。"""

from __future__ import annotations

import json
import subprocess
import threading

import pytest

from local_webpage_access import tailscale_network as ts


def test_probe_self_addresses_and_running_state(monkeypatch):
    monkeypatch.setattr(ts, "find_tailscale", lambda: "/bin/tailscale")
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(
            cmd,
            0,
            json.dumps(
                {
                    "BackendState": "Running",
                    "TailscaleIPs": ["fd7a:115c:a1e0::1", "100.101.102.103"],
                    "Self": {"Online": True},
                    "Peer": {"x": {"TailscaleIPs": ["100.1.1.1"]}},
                }
            ),
            "",
        )

    monkeypatch.setattr(ts.subprocess, "run", run)
    snap = ts.probe_tailscale()
    assert snap["available"] is True
    assert snap["selectedIp"] == "100.101.102.103"
    assert snap["ipv6"] == "fd7a:115c:a1e0::1"
    assert "Peer" not in snap
    assert calls[0][1]["timeout"] == 2
    assert "--json" in calls[0][0]


@pytest.mark.parametrize(
    "data",
    [
        {"BackendState": "Stopped", "Self": {"Online": True}, "TailscaleIPs": ["100.1.1.1"]},
        {"BackendState": "NeedsLogin"},
        {"BackendState": "Running", "Self": {"Online": False}, "TailscaleIPs": ["100.1.1.1"]},
        {"BackendState": "Running", "Self": {"Online": True}, "TailscaleIPs": []},
        {
            "BackendState": "Running",
            "Self": {"Online": True},
            "TailscaleIPs": ["invalid", "127.0.0.1"],
        },
        {"BackendState": "Running", "Self": None},
        [],
        None,
    ],
)
def test_unavailable_state_never_keeps_old_ip(monkeypatch, data):
    monkeypatch.setattr(ts, "find_tailscale", lambda: "tailscale")
    monkeypatch.setattr(
        ts.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, json.dumps(data), ""),
    )
    snap = ts.probe_tailscale()
    assert not snap["available"]
    assert snap["selectedIp"] is None


@pytest.mark.parametrize("error", [OSError("denied"), subprocess.TimeoutExpired("tailscale", 2)])
def test_probe_errors_are_unavailable(monkeypatch, error):
    monkeypatch.setattr(ts, "find_tailscale", lambda: "tailscale")

    def run(*a, **k):
        raise error

    monkeypatch.setattr(ts.subprocess, "run", run)
    assert not ts.probe_tailscale()["available"]


def test_missing_cli_and_invalid_json(monkeypatch):
    monkeypatch.setattr(ts, "find_tailscale", lambda: None)
    assert ts.probe_tailscale()["state"] == "not_installed"
    monkeypatch.setattr(ts, "find_tailscale", lambda: "tailscale")
    monkeypatch.setattr(
        ts.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, "{", "")
    )
    assert not ts.probe_tailscale()["available"]


def test_cache_read_never_probes_and_expires(monkeypatch):
    now = [0.0]
    cache = ts.NetworkContextCache(clock=lambda: now[0])
    assert not cache.snapshot()["available"]
    monkeypatch.setattr(
        ts,
        "probe_tailscale",
        lambda: {"available": True, "selectedIp": "100.1.1.1", "state": "Running"},
    )
    cache.refresh()
    now[0] = 59
    assert cache.snapshot()["validForSeconds"] == 1
    assert cache.snapshot()["available"]
    now[0] = 60
    assert not cache.snapshot()["available"]
    assert cache.snapshot()["selectedIp"] is None
    assert cache.snapshot()["state"] == "stale"


def test_background_initial_probe_and_shutdown(monkeypatch):
    called = threading.Event()

    def probe():
        called.set()
        return {"available": False, "state": "Stopped"}

    monkeypatch.setattr(ts, "probe_tailscale", probe)
    cache = ts.NetworkContextCache()
    cache.start()
    assert called.wait(1)
    cache.stop()
    assert not cache.thread.is_alive()
    assert ts.REFRESH_SECONDS == 30


def test_ipv6_only_and_command_failure(monkeypatch):
    monkeypatch.setattr(ts, "find_tailscale", lambda: "tailscale")
    data = {
        "BackendState": "Running",
        "Self": {"Online": True, "TailscaleIPs": ["fd7a:115c:a1e0::1"]},
    }
    monkeypatch.setattr(
        ts.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, json.dumps(data), ""),
    )
    assert ts.probe_tailscale()["selectedIp"] == "fd7a:115c:a1e0::1"
    monkeypatch.setattr(
        ts.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 1, json.dumps(data), "denied"),
    )
    assert not ts.probe_tailscale()["available"]


def test_background_cadence_recovers_and_invalidates_on_error(monkeypatch):
    results = iter(
        [
            {"available": False, "state": "Stopped"},
            {"available": True, "selectedIp": "100.1.1.2", "state": "Running"},
            RuntimeError("unexpected failure"),
        ]
    )
    snapshots, waits = [], []
    cache = ts.NetworkContextCache()

    def probe():
        value = next(results)
        if isinstance(value, Exception):
            raise value
        return value

    class Stop:
        def is_set(self):
            return False

        def wait(self, delay):
            waits.append(delay)
            snapshots.append(cache.snapshot())
            return len(waits) == 3

    monkeypatch.setattr(ts, "probe_tailscale", probe)
    cache._stop = Stop()
    cache._run()
    assert waits == [30, 30, 30]
    assert [s["available"] for s in snapshots] == [False, True, False]
    assert snapshots[-1]["selectedIp"] is None


def test_macos_gui_cli_discovery(monkeypatch):
    monkeypatch.setattr(ts.shutil, "which", lambda name: None)
    monkeypatch.setattr(ts.sys, "platform", "darwin")
    monkeypatch.setattr(
        ts.Path,
        "is_file",
        lambda p: str(p) == "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    )
    monkeypatch.setattr(ts.os, "access", lambda *a: True)
    assert ts.find_tailscale() == "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
