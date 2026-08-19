"""IMP-038/040：共享 access_workflow 编排与节流 refresh。"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from local_webpage_access.access import RefreshReport
from local_webpage_access.config import Config, PortPool
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry


def _seed(
    workspace: Workspace,
    registry: Registry,
    iid: str = "demo",
    *,
    host_port: int = 21000,
    lan_url: str,
):
    from local_webpage_access.models import (
        DesiredState,
        InstanceManifest,
        Kind,
        NetworkConfig,
        ResourceProfile,
        Runtime,
        ServingMode,
        StaticConfig,
        Status,
    )

    workspace.ensure_app_dirs(iid)
    manifest = InstanceManifest(
        id=iid,
        name=iid,
        version="1",
        kind=Kind.STATIC,
        runtime=Runtime.SHARED_STATIC,
        servingMode=ServingMode.SHARED_STATIC,
        resourceProfile=ResourceProfile.TINY,
        status=Status.RUNNING,
        desiredState=DesiredState.RUNNING,
        static=StaticConfig(root="public", hostPort=host_port, enabled=True),
        network=NetworkConfig(hostPort=host_port, lanUrl=lan_url),
    )
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)
    registry.allocate_port(iid, host_port)


@pytest.fixture()
def env(tmp_path: Path):
    root = tmp_path / "ws"
    ws = Workspace(root)
    ws.ensure_workspace_dirs()
    reg = Registry(ws.db_path)
    reg.open()
    cfg = Config(lanIpStrategy="auto", portPool=PortPool(start=21000, end=21050))
    yield ws, cfg, reg
    reg.close()


def test_run_access_pass_dry_run_skips(env) -> None:
    from local_webpage_access.access_workflow import run_access_pass

    ws, cfg, reg = env
    result = run_access_pass(ws, cfg, reg, review=True, dry_run=True)
    assert result.skipped is True
    assert result.refresh is None
    assert result.review is None


def test_throttled_refresh_writes_once_within_window(env, monkeypatch) -> None:
    """040.01：漂移后首次落盘；窗口内二次调用不重复全量写。"""
    from local_webpage_access import access_workflow as aw

    ws, cfg, reg = env
    _seed(ws, reg, lan_url="http://10.0.0.99:21000")
    monkeypatch.setattr("local_webpage_access.ports.resolve_lan_ip", lambda c: "192.168.1.50")
    monkeypatch.setattr("local_webpage_access.access.resolve_lan_ip", lambda c: "192.168.1.50")
    aw.reset_lan_refresh_throttle_state()

    calls = {"n": 0}
    real = aw.refresh_network_entries

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(aw, "refresh_network_entries", counting)

    r1 = aw.maybe_throttled_lan_refresh(ws, cfg, reg, min_interval=60.0)
    assert r1 is not None
    assert calls["n"] == 1
    r2 = aw.maybe_throttled_lan_refresh(ws, cfg, reg, min_interval=60.0)
    assert r2 is None
    assert calls["n"] == 1


def test_throttled_refresh_single_flight(env, monkeypatch) -> None:
    from local_webpage_access import access_workflow as aw

    ws, cfg, reg = env
    _seed(ws, reg, lan_url="http://10.0.0.99:21000")
    monkeypatch.setattr("local_webpage_access.ports.resolve_lan_ip", lambda c: "192.168.1.50")
    monkeypatch.setattr("local_webpage_access.access.resolve_lan_ip", lambda c: "192.168.1.50")
    aw.reset_lan_refresh_throttle_state()

    started = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    def slow_refresh(*a, **k):
        calls["n"] += 1
        started.set()
        release.wait(timeout=2)
        return RefreshReport(lan_ip="192.168.1.50")

    monkeypatch.setattr(aw, "refresh_network_entries", slow_refresh)

    results: list = []

    def worker():
        results.append(aw.maybe_throttled_lan_refresh(ws, cfg, reg, min_interval=60.0))

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    assert started.wait(timeout=1)
    t2.start()
    release.set()
    t1.join(timeout=2)
    t2.join(timeout=2)
    assert calls["n"] == 1
    assert sum(1 for r in results if r is not None) == 1


def test_manual_strategy_does_not_auto_refresh(env, monkeypatch) -> None:
    from local_webpage_access import access_workflow as aw

    ws, cfg, reg = env
    cfg.lanIpStrategy = "manual"
    cfg.manualLanIp = "192.168.9.9"
    _seed(ws, reg, lan_url="http://10.0.0.99:21000")
    monkeypatch.setattr("local_webpage_access.ports.detect_lan_ip", lambda: "192.168.1.50")
    aw.reset_lan_refresh_throttle_state()
    calls = {"n": 0}
    monkeypatch.setattr(
        aw,
        "refresh_network_entries",
        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1),
    )
    assert aw.maybe_throttled_lan_refresh(ws, cfg, reg) is None
    assert calls["n"] == 0


# ---- DEV-114：access review 防抖重试 ------------------------------------------


class TestReviewAccessDebounce:
    """update 重启后的启动窗口误报 FAIL：0.5/1/2/4s 防抖重试。"""

    @staticmethod
    def _report(*statuses: str):
        from local_webpage_access.access import AccessReviewReport, InstanceAccessReport

        return AccessReviewReport(
            instances=[
                InstanceAccessReport(instance_id=f"i{n}", status=s)
                for n, s in enumerate(statuses)
            ]
        )

    @pytest.fixture()
    def deb_env(self, env, monkeypatch: pytest.MonkeyPatch):
        """env + 记录 sleep 的防抖环境（真实 sleep 被替换为记录器）。"""
        import local_webpage_access.access_workflow as aw

        sleeps: list[float] = []
        monkeypatch.setattr(aw.time, "sleep", lambda s: sleeps.append(s))
        ws, cfg, reg = env
        return ws, cfg, reg, aw, sleeps

    def test_default_schedule_is_user_specified(self) -> None:
        from local_webpage_access.access_workflow import ACCESS_REVIEW_DEBOUNCE_DELAYS

        assert ACCESS_REVIEW_DEBOUNCE_DELAYS == (0.5, 1.0, 2.0, 4.0)

    def test_first_pass_ok_no_retry(self, deb_env) -> None:
        ws, cfg, reg, aw, sleeps = deb_env
        calls: list[int] = []
        monkeypatch_holder = pytest.MonkeyPatch()
        monkeypatch_holder.setattr(
            aw, "review_access", lambda *a, **k: (calls.append(1), self._report("ok"))[1]
        )
        try:
            result = aw.review_access_with_debounce(ws, cfg, reg)
        finally:
            monkeypatch_holder.undo()
        assert result.attempts == 1
        assert result.passed_on_attempt == 1
        assert result.review_error is None
        assert sleeps == []  # 通过即返回，不等待
        assert len(calls) == 1

    def test_fail_fail_then_ok_passes_on_third_attempt(self, deb_env) -> None:
        ws, cfg, reg, aw, sleeps = deb_env
        outcomes = iter(
            [self._report("fail"), self._report("fail"), self._report("ok")]
        )
        holder = pytest.MonkeyPatch()
        holder.setattr(aw, "review_access", lambda *a, **k: next(outcomes))
        try:
            result = aw.review_access_with_debounce(ws, cfg, reg)
        finally:
            holder.undo()
        assert result.attempts == 3
        assert result.passed_on_attempt == 3
        assert sleeps == [0.5, 1.0]  # 第 2、3 次前的防抖间隔
        assert result.review_error is None

    def test_all_fail_returns_last_after_full_schedule(self, deb_env) -> None:
        ws, cfg, reg, aw, sleeps = deb_env
        n = [0]

        def always_fail(*a, **k):
            n[0] += 1
            return self._report("fail")

        holder = pytest.MonkeyPatch()
        holder.setattr(aw, "review_access", always_fail)
        try:
            result = aw.review_access_with_debounce(ws, cfg, reg)
        finally:
            holder.undo()
        assert result.attempts == 5  # 1 次立即 + 4 次防抖
        assert result.passed_on_attempt is None
        assert result.review is not None and result.review.has_failures
        assert sleeps == [0.5, 1.0, 2.0, 4.0]

    def test_probe_error_is_retryable_then_passes(self, deb_env) -> None:
        ws, cfg, reg, aw, sleeps = deb_env
        state = [0]

        def flaky(*a, **k):
            state[0] += 1
            if state[0] == 1:
                raise RuntimeError("probe refused（启动窗口）")
            return self._report("ok")

        holder = pytest.MonkeyPatch()
        holder.setattr(aw, "review_access", flaky)
        try:
            result = aw.review_access_with_debounce(ws, cfg, reg)
        finally:
            holder.undo()
        assert result.passed_on_attempt == 2
        assert result.review_error is None  # 通过后不携带旧的异常

    def test_warn_is_real_finding_no_retry(self, deb_env) -> None:
        """WARN（LAN 漂移/别名错位）不是启动噪声，不重试洗白。"""
        ws, cfg, reg, aw, sleeps = deb_env
        holder = pytest.MonkeyPatch()
        holder.setattr(aw, "review_access", lambda *a, **k: self._report("warn"))
        try:
            result = aw.review_access_with_debounce(ws, cfg, reg)
        finally:
            holder.undo()
        assert result.attempts == 1
        assert result.passed_on_attempt == 1
        assert sleeps == []

    def test_custom_delays_injected(self, deb_env) -> None:
        ws, cfg, reg, aw, sleeps = deb_env
        outcomes = iter([self._report("fail"), self._report("fail")])
        holder = pytest.MonkeyPatch()
        holder.setattr(aw, "review_access", lambda *a, **k: next(outcomes))
        try:
            result = aw.review_access_with_debounce(
                ws, cfg, reg, delays=(0.0, 0.0, 0.0)
            )
        finally:
            holder.undo()
        assert result.attempts == 4
        assert sleeps == [0.0, 0.0, 0.0]
