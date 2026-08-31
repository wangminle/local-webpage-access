"""IMP-062 回归：doctor 版本滞后提示（复用 ``lwa update --check`` + 24h 缓存）。

* 复用 update_source.run_source_check（不重复实现远端探测）；
* 24h 缓存命中不触网；所有状态都缓存（含 unavailable，离线环境不反复等超时）；
* updateAvailable → WARN（提示 lwa update）；upToDate → OK；
  unavailable / blocked / 非 git 安装 / 锁忙 → SKIP（网络与源码管理问题不升
  doctor FAIL）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_webpage_access.config import Config
from local_webpage_access.doctor import check_version_freshness
from local_webpage_access.paths import Workspace


@pytest.fixture(autouse=True)
def _allow_version_check_in_tests(monkeypatch):
    """绕过 pytest 门禁（本文件全部用例均用替身，不触网）。"""
    monkeypatch.setenv("LWA_ALLOW_VERSION_CHECK", "1")


@pytest.fixture()
def ws(tmp_path: Path) -> Workspace:
    w = Workspace(tmp_path / "ws")
    w.ensure_workspace_dirs()
    return w


@pytest.fixture()
def cfg() -> Config:
    from local_webpage_access.config import PortPool

    return Config(staticGateway="builtin", portPool=PortPool(start=21000, end=21050))


def _write_cache(ws: Workspace, payload: dict) -> None:
    path = ws.run / "version-check.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class _Report:
    """SourceCheckReport 替身（to_dict 契约子集）。"""

    def __init__(self, status: str, *, behind_by: int = 0, target: dict | None = None,
                 error: dict | None = None) -> None:
        self.status = status
        self.behind_by = behind_by
        self._target = target
        self._error = error

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "checkedAt": "2026-08-31T12:00:00+08:00",
            "behindBy": self.behind_by,
            "target": self._target,
            "error": self._error,
        }


def test_non_git_install_skips(ws, monkeypatch) -> None:
    """非 git 克隆安装（locate_repo 无果）→ SKIP。"""
    monkeypatch.setattr("local_webpage_access.updater.locate_repo", lambda: None)
    result = check_version_freshness(ws)
    assert result.status == "skip"
    assert "源码根" in result.message


def test_update_available_warns_and_suggests_update(ws, monkeypatch) -> None:
    """落后 → WARN + `lwa update` 建议；结论写缓存。"""
    import local_webpage_access.update_source as us

    monkeypatch.setattr("local_webpage_access.updater.locate_repo", lambda: Path("/repo"))
    captured: dict = {}

    class _Locks:
        def acquire(self, repo, workspace=None):
            captured["locked"] = True
            return 99

    monkeypatch.setattr(us, "acquire_repo_lock", _Locks().acquire)
    monkeypatch.setattr(
        us,
        "run_source_check",
        lambda repo, **kw: _Report(
            "updateAvailable",
            behind_by=2,
            target={"version": "V0.8.9-test", "branch": "main"},
        ),
    )

    result = check_version_freshness(ws)
    assert result.status == "warn"
    assert "落后 2 个提交" in result.message
    assert "V0.8.9-test" in result.message
    assert "lwa update" in (result.suggestion or "")
    assert captured.get("locked") is True  # 复用 repo 锁语义
    # 缓存已写
    cached = json.loads((ws.run / "version-check.json").read_text(encoding="utf-8"))
    assert cached["status"] == "updateAvailable"
    assert cached["behindBy"] == 2


def test_up_to_date_ok(ws, monkeypatch) -> None:
    import local_webpage_access.update_source as us

    monkeypatch.setattr("local_webpage_access.updater.locate_repo", lambda: Path("/repo"))
    monkeypatch.setattr(us, "acquire_repo_lock", lambda repo, workspace=None: 1)
    monkeypatch.setattr(us, "run_source_check", lambda repo, **kw: _Report("upToDate"))

    result = check_version_freshness(ws)
    assert result.status == "ok"


def test_unavailable_is_skip_not_fail(ws, monkeypatch) -> None:
    """网络失败 → SKIP（不产生 doctor FAIL），且结论入缓存。"""
    import local_webpage_access.update_source as us

    monkeypatch.setattr("local_webpage_access.updater.locate_repo", lambda: Path("/repo"))
    monkeypatch.setattr(us, "acquire_repo_lock", lambda repo, workspace=None: 1)
    monkeypatch.setattr(
        us,
        "run_source_check",
        lambda repo, **kw: _Report(
            "unavailable",
            error={"kind": "fetch_failed", "message": "connection timeout"},
        ),
    )

    result = check_version_freshness(ws)
    assert result.status == "skip"
    assert "不可达" in result.message
    cached = json.loads((ws.run / "version-check.json").read_text(encoding="utf-8"))
    assert cached["status"] == "unavailable"


def test_blocked_is_skip(ws, monkeypatch) -> None:
    import local_webpage_access.update_source as us

    monkeypatch.setattr("local_webpage_access.updater.locate_repo", lambda: Path("/repo"))
    monkeypatch.setattr(us, "acquire_repo_lock", lambda repo, workspace=None: 1)
    monkeypatch.setattr(
        us,
        "run_source_check",
        lambda repo, **kw: _Report(
            "blocked", error={"kind": "dirty", "message": "tracked 文件有本地修改"}
        ),
    )
    result = check_version_freshness(ws)
    assert result.status == "skip"
    assert "本地修改" in result.message


def test_lock_busy_skips(ws, monkeypatch) -> None:
    import local_webpage_access.update_source as us

    monkeypatch.setattr("local_webpage_access.updater.locate_repo", lambda: Path("/repo"))

    def busy(repo, workspace=None):
        raise us.UpdateLockBusy("repo", "holder")

    monkeypatch.setattr(us, "acquire_repo_lock", busy)
    result = check_version_freshness(ws)
    assert result.status == "skip"
    assert "更新锁被占用" in result.message


def test_cache_hit_avoids_network(ws, monkeypatch) -> None:
    """24h 内缓存命中 → 不定位 repo、不取锁、不 fetch。"""
    import local_webpage_access.update_source as us
    from local_webpage_access.logging import now_iso

    _write_cache(
        ws,
        {
            "status": "updateAvailable",
            "checkedAt": now_iso(),
            "behindBy": 7,
            "target": {"version": "V0.9.0-test"},
            "error": None,
        },
    )
    called: list[str] = []
    monkeypatch.setattr(
        "local_webpage_access.updater.locate_repo",
        lambda: called.append("locate") or Path("/repo"),
    )
    monkeypatch.setattr(us, "acquire_repo_lock", lambda repo, workspace=None: called.append("lock") or 1)
    monkeypatch.setattr(
        us, "run_source_check", lambda repo, **kw: called.append("fetch") or _Report("upToDate")
    )

    result = check_version_freshness(ws)
    assert result.status == "warn"
    assert "落后 7 个提交" in result.message
    assert "缓存" in result.message
    assert called == []  # 零网络/零锁


def test_stale_cache_expired_rechecks(ws, monkeypatch) -> None:
    """缓存超过 24h → 重新探测。"""
    import local_webpage_access.update_source as us

    _write_cache(
        ws,
        {
            "status": "upToDate",
            "checkedAt": "2026-08-01T00:00:00+08:00",  # 远超 24h
            "behindBy": 0,
        },
    )
    monkeypatch.setattr("local_webpage_access.updater.locate_repo", lambda: Path("/repo"))
    monkeypatch.setattr(us, "acquire_repo_lock", lambda repo, workspace=None: 1)
    monkeypatch.setattr(us, "run_source_check", lambda repo, **kw: _Report("updateAvailable", behind_by=1))

    result = check_version_freshness(ws)
    assert result.status == "warn"


def test_run_doctor_includes_version_freshness(ws, cfg, monkeypatch) -> None:
    """run_doctor 注册了该检查（默认检查列表）。"""
    monkeypatch.setattr(
        "local_webpage_access.updater.locate_repo", lambda: None
    )
    from local_webpage_access.doctor import run_doctor

    report = run_doctor(ws, cfg)
    names = [c.name for c in report.checks]
    assert "version_freshness" in names
