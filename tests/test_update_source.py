"""IMP-063 ``lwa update`` 一键 GitHub 更新通道测试。

git 测试统一用**临时 bare remote + clone 夹具**，禁止依赖外网与真实 GitHub。
覆盖 063.01-06.12：目标解析、互斥锁、九态关系、SourceCheckReport v1、固定 OID
快进、skip-pip 门控、handoff 接力与 CLI check/dry-run 契约。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from local_webpage_access import update_source as us
from local_webpage_access.paths import Workspace
from local_webpage_access.update_flow import run_update_flow
from local_webpage_access.updater import UpdateOptions, UpdateReport, StepResult


# ---- git 夹具 ---------------------------------------------------------------


def _git(repo: Path, *args: str, check: bool = True) -> str:
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and res.returncode != 0:
        raise AssertionError(f"git {args} 失败：{res.stderr}")
    return res.stdout


def _commit(repo: Path, filename: str, content: str, subject: str) -> str:
    (repo / filename).write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--allow-empty", "-m", subject)
    return _git(repo, "rev-parse", "HEAD").strip()


class GitEnv:
    """bare remote + clone（remote 名 origin，分支 main）。"""

    def __init__(self, tmp: Path, *, clone_depth: int | None = None) -> None:
        self.remote = tmp / "remote.git"
        self.remote.mkdir(parents=True)
        _git(self.remote, "init", "--bare", "-b", "main")
        # 种子提交（先在工作区做好再推）——两个提交保证 depth=1 克隆真正截断
        seed = tmp / "seed"
        _git_run(["git", "init", "-b", "main", str(seed)])
        _configure(seed)
        _commit(seed, "pyproject.toml", "[project]\nname='local-webpage-access'\n", "init")
        _commit(seed, "README.md", "# t\n", "V0.7.9-Build1")
        _git(seed, "remote", "add", "origin", str(self.remote))
        _git(seed, "push", "origin", "main")
        depth_args = ["--depth", str(clone_depth), "--no-local"] if clone_depth else []
        self.repo = tmp / "repo"
        _git_run(["git", "clone", *depth_args, "-b", "main", str(self.remote), str(self.repo)])
        _configure(self.repo)

    def push_commit(self, filename: str, content: str, subject: str) -> str:
        oid = _commit(self.repo, filename, content, subject)
        _git(self.repo, "push", "origin", "main")
        return oid


def _git_run(cmd: list[str]) -> None:
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert res.returncode == 0, f"{cmd} 失败：{res.stderr}"


def _configure(repo: Path) -> None:
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "t")


@pytest.fixture()
def git_env(tmp_path: Path) -> GitEnv:
    return GitEnv(tmp_path)


# ---- 063.01 目标解析 ---------------------------------------------------------


def test_resolve_target_from_upstream(git_env: GitEnv) -> None:
    target = us.resolve_source_target(git_env.repo)
    assert target.remote == "origin"
    assert target.branch == "main"
    assert target.from_upstream is True
    assert target.head == _git(git_env.repo, "rev-parse", "HEAD").strip()


def test_resolve_target_no_upstream_requires_both(git_env: GitEnv) -> None:
    _git(git_env.repo, "branch", "--unset-upstream")
    with pytest.raises(us.SourceUpdateError, match="upstream") as ei:
        us.resolve_source_target(git_env.repo)
    assert ei.value.kind == "target_incomplete"
    with pytest.raises(us.SourceUpdateError) as ei2:
        us.resolve_source_target(git_env.repo, remote="origin")
    assert ei2.value.kind == "target_incomplete"
    with pytest.raises(us.SourceUpdateError) as ei3:
        us.resolve_source_target(git_env.repo, ref="main")
    assert ei3.value.kind == "target_incomplete"
    # 显式给全则正常（验收：不因缺 upstream 拒绝）
    target = us.resolve_source_target(git_env.repo, remote="origin", ref="main")
    assert target.remote == "origin" and target.branch == "main"


def test_resolve_target_rejects_sha_and_refs(git_env: GitEnv) -> None:
    head = _git(git_env.repo, "rev-parse", "HEAD").strip()
    with pytest.raises(us.SourceUpdateError) as ei:
        us.resolve_source_target(git_env.repo, remote="origin", ref=head)
    assert ei.value.kind == "invalid_ref"
    with pytest.raises(us.SourceUpdateError) as ei2:
        us.resolve_source_target(git_env.repo, remote="origin", ref="refs/heads/main")
    assert ei2.value.kind == "invalid_ref"


def test_resolve_target_not_git_repo(tmp_path: Path) -> None:
    with pytest.raises(us.SourceUpdateError) as ei:
        us.resolve_source_target(tmp_path)
    assert ei.value.kind == "not_a_git_repo"


def test_resolve_target_detached(git_env: GitEnv) -> None:
    head = _git(git_env.repo, "rev-parse", "HEAD").strip()
    _git(git_env.repo, "checkout", "-q", "--detach", head)
    with pytest.raises(us.SourceUpdateError) as ei:
        us.resolve_source_target(git_env.repo)
    assert ei.value.kind == "detached"


# ---- 063.02 互斥锁 -----------------------------------------------------------


def test_repo_lock_mutual_exclusion(git_env: GitEnv, tmp_path: Path) -> None:
    fd = us.acquire_repo_lock(git_env.repo, tmp_path)
    try:
        with pytest.raises(us.UpdateLockBusy) as ei:
            us.acquire_repo_lock(git_env.repo, tmp_path)
        assert ei.value.scope == "repo"
        assert ei.value.holder and ei.value.holder.get("pid") > 0
    finally:
        from local_webpage_access.file_lock import release_exclusive
        import os

        release_exclusive(fd)
        os.close(fd)


def test_update_locks_order_and_close(git_env: GitEnv, tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    locks = us.acquire_update_locks(git_env.repo, ws)
    try:
        assert locks.repo_fd >= 0 and locks.ws_fd >= 0
    finally:
        locks.close()
    # 关闭后可重新获取
    locks2 = us.acquire_update_locks(git_env.repo, ws)
    locks2.close()


# ---- 063.03 状态与九态关系 ------------------------------------------------------


def test_inspect_clean_equal(git_env: GitEnv) -> None:
    head = _git(git_env.repo, "rev-parse", "HEAD").strip()
    status = us.inspect_repo(git_env.repo, head)
    assert status.relation == "equal"
    assert status.dirty_files == []
    assert status.detached is False


def test_inspect_dirty_only_tracked(git_env: GitEnv) -> None:
    (git_env.repo / "pyproject.toml").write_text("modified", encoding="utf-8")
    (git_env.repo / "untracked.txt").write_text("x", encoding="utf-8")
    head = _git(git_env.repo, "rev-parse", "HEAD").strip()
    status = us.inspect_repo(git_env.repo, head)
    assert status.dirty is True
    assert "pyproject.toml" in status.dirty_files
    assert "untracked.txt" in status.untracked_files
    assert status.untracked_files not in status.dirty_files


def test_inspect_relations(git_env: GitEnv) -> None:
    head = _git(git_env.repo, "rev-parse", "HEAD").strip()
    candidate = git_env.push_commit("a.txt", "1", "V0.7.10-Build1")
    # 本地退回旧提交构造 behind
    _git(git_env.repo, "reset", "-q", "--hard", head)
    status = us.inspect_repo(git_env.repo, candidate)
    assert status.relation == "behind"
    assert status.behind_by == 1
    # behind 基础上本地再提交 → diverged
    _commit(git_env.repo, "b.txt", "1", "local")
    status2 = us.inspect_repo(git_env.repo, candidate)
    assert status2.relation == "diverged"
    # 本地先对齐远端再提交 → ahead
    _git(git_env.repo, "reset", "-q", "--hard", candidate)
    _commit(git_env.repo, "c.txt", "1", "local-only")
    status3 = us.inspect_repo(git_env.repo, candidate)
    assert status3.relation == "ahead"


def test_inspect_shallow_unknown_not_diverged(tmp_path: Path) -> None:
    """shallow 历史不足 → unknown（history_insufficient），不冒充 diverged。

    现代 git 在 fetch 时会自动加深；这里用"已知是浅克隆 + 无法证明祖先关系
    的候选"构造判定输入：两个 --is-ancestor 都失败且 shallow=True → unknown。
    """
    env = GitEnv(tmp_path, clone_depth=1)
    assert us.inspect_repo(env.repo).shallow is True
    unknown_candidate = "e" * 40  # 本地不存在的对象 → 祖先关系不可证
    status = us.inspect_repo(env.repo, unknown_candidate)
    assert status.shallow is True
    assert status.relation == "unknown"


# ---- 063.05 fetch + --check ----------------------------------------------------


def test_fetch_candidate_fixed_oid(git_env: GitEnv) -> None:
    git_env.push_commit("a.txt", "1", "V0.7.10-Build1")
    git_env.push_commit("b.txt", "2", "V0.7.11-Build2")
    target = us.resolve_source_target(git_env.repo)
    oid = us.fetch_candidate(git_env.repo, target)
    expected = _git(git_env.repo, "rev-parse", "refs/remotes/origin/main").strip()
    assert oid == expected


def test_fetch_candidate_invalid_branch(git_env: GitEnv) -> None:
    target = us.resolve_source_target(git_env.repo, remote="origin", ref="no-such")
    with pytest.raises(us.SourceUpdateError) as ei:
        us.fetch_candidate(git_env.repo, target)
    assert ei.value.kind == "invalid_ref"


def test_source_check_update_available(git_env: GitEnv) -> None:
    git_env.push_commit("a.txt", "1", "V0.7.10-Build100")
    git_env.push_commit("b.txt", "2", "V0.7.11-Build200")
    _commit(git_env.repo, "local.txt", "keep-uncommitted? no", "x")  # 推进 HEAD?
    # 上面 _commit 已推进本地 HEAD —— 为保持 behind 场景，重置回 origin/main 之前
    _git(git_env.repo, "reset", "-q", "--hard", "origin/main~1")
    report = us.run_source_check(git_env.repo)
    d = report.to_dict()
    assert d["status"] == "updateAvailable"
    assert d["relation"] == "behind"
    assert d["behindBy"] == 1
    assert d["behind"][0]["subject"] == "V0.7.11-Build200"
    assert d["target"]["version"] == "0.7.11"
    assert d["fresh"] is True
    assert d["schemaVersion"] == 1
    assert report.exit_code() == 0


def test_source_check_up_to_date(git_env: GitEnv) -> None:
    report = us.run_source_check(git_env.repo)
    assert report.status == "upToDate"
    assert report.relation == "equal"
    assert report.exit_code() == 0


def test_source_check_blocked_dirty(git_env: GitEnv) -> None:
    git_env.push_commit("a.txt", "1", "V0.7.10")
    (git_env.repo / "pyproject.toml").write_text("dirty", encoding="utf-8")
    report = us.run_source_check(git_env.repo)
    assert report.status == "blocked"
    kinds = [b["kind"] for b in report.blockers]
    assert "dirty" in kinds
    assert report.exit_code() == 0


def test_source_check_unavailable_bad_remote(git_env: GitEnv) -> None:
    _git(git_env.repo, "remote", "set-url", "origin", str(git_env.repo.parent / "missing.git"))
    report = us.run_source_check(git_env.repo)
    assert report.status == "unavailable"
    assert report.error and report.error["kind"] == "fetch_failed"
    assert report.exit_code() == 2


def test_source_check_invalid_params_exit1(tmp_path: Path) -> None:
    report = us.run_source_check(tmp_path)
    assert report.status == "blocked"
    assert report.error is not None
    assert report.exit_code() == 1


def test_behind_truncation_json_limit(git_env: GitEnv) -> None:
    original = _git(git_env.repo, "rev-parse", "HEAD").strip()
    for i in range(3):
        git_env.push_commit(f"f{i}.txt", "1", f"V0.7.9-Build{i}")
    _git(git_env.repo, "reset", "-q", "--hard", original)
    report = us.run_source_check(git_env.repo)
    assert report.status == "updateAvailable"
    assert report.behind_by == 3
    assert len(report.behind) == 3
    assert report.truncated is False


# ---- 063.06 固定 OID 快进 -------------------------------------------------------


def test_fast_forward_applies_candidate(git_env: GitEnv) -> None:
    candidate = git_env.push_commit("a.txt", "1", "V0.7.11-Build2888")
    _git(git_env.repo, "reset", "-q", "--hard", "origin/main~1")
    target = us.resolve_source_target(git_env.repo)
    fetched = us.fetch_candidate(git_env.repo, target)
    assert fetched == candidate
    status = us.inspect_repo(git_env.repo, fetched)
    result = us.apply_fast_forward(git_env.repo, target, fetched, status)
    assert result.new_head == candidate
    assert result.descriptor.version == "0.7.11"
    assert _git(git_env.repo, "rev-parse", "HEAD").strip() == candidate


def test_fast_forward_rejects_dirty(git_env: GitEnv) -> None:
    candidate = git_env.push_commit("a.txt", "1", "V0.7.11")
    _git(git_env.repo, "reset", "-q", "--hard", "origin/main~1")
    (git_env.repo / "pyproject.toml").write_text("dirty", encoding="utf-8")
    target = us.resolve_source_target(git_env.repo)
    status = us.inspect_repo(git_env.repo, candidate)
    with pytest.raises(us.SourceUpdateError) as ei:
        us.apply_fast_forward(git_env.repo, target, candidate, status)
    assert ei.value.kind == "dirty"
    # 工作树零改动（拒绝路径）
    assert "dirty" in (git_env.repo / "pyproject.toml").read_text()


def test_fast_forward_rejects_behind_plus_skip_pip(git_env: GitEnv) -> None:
    """behind + --skip-pip：快进前拒绝且工作树不变（§15.1.6）。"""
    candidate = git_env.push_commit("a.txt", "1", "V0.7.11")
    _git(git_env.repo, "reset", "-q", "--hard", "origin/main~1")
    target = us.resolve_source_target(git_env.repo)
    status = us.inspect_repo(git_env.repo, candidate)
    head_before = _git(git_env.repo, "rev-parse", "HEAD").strip()
    with pytest.raises(us.SourceUpdateError) as ei:
        us.apply_fast_forward(git_env.repo, target, candidate, status, skip_pip=True)
    assert ei.value.kind == "skip_pip_conflict"
    assert _git(git_env.repo, "rev-parse", "HEAD").strip() == head_before


def test_fast_forward_equal_is_noop(git_env: GitEnv) -> None:
    target = us.resolve_source_target(git_env.repo)
    head = _git(git_env.repo, "rev-parse", "HEAD").strip()
    status = us.inspect_repo(git_env.repo, head)
    result = us.apply_fast_forward(git_env.repo, target, head, status)
    assert result.old_head == result.new_head == head


def test_fast_forward_untracked_collision_diagnosable(git_env: GitEnv) -> None:
    candidate = git_env.push_commit("new-file.txt", "remote", "V0.7.11")
    _git(git_env.repo, "reset", "-q", "--hard", "origin/main~1")
    # 本地 untracked 同名文件与远端新增冲突（不阻断判定，由 ff 阶段拒绝）
    (git_env.repo / "new-file.txt").write_text("local-untracked", encoding="utf-8")
    target = us.resolve_source_target(git_env.repo)
    status = us.inspect_repo(git_env.repo, candidate)
    assert status.dirty is False  # untracked 不算脏
    with pytest.raises(us.SourceUpdateError) as ei:
        us.apply_fast_forward(git_env.repo, target, candidate, status)
    assert ei.value.kind == "ff_failed"
    assert (git_env.repo / "new-file.txt").read_text() == "local-untracked"


# ---- 063.07/08 bootstrap 编排与接力 ------------------------------------------------


def _make_workspace(tmp_path: Path) -> Path:
    from local_webpage_access.paths import Workspace

    ws = Workspace(tmp_path / "ws")
    ws.ensure_workspace_dirs()
    ws.config_path.write_text(
        "managerPort: 17801\n"
        "managerHost: 127.0.0.1\n"
        "managerEnabled: false\n"
        "portPool:\n"
        "  start: 21100\n"
        "  end: 21150\n"
        "staticGateway: builtin\n",
        encoding="utf-8",
    )
    return ws.root


def _flow_options(repo: Path, **kw) -> UpdateOptions:
    base = dict(
        dry_run=False,
        skip_pip=False,
        sync_skills=False,
        sync_templates=False,
        restart_manager=False,
        restart_daemon=False,
        restart_gateway=False,
        restart_instances=False,
        run_doctor=False,
        review_access=False,
        repo=str(repo),
        pull=True,
        remote=None,
        ref=None,
    )
    base.update(kw)
    return UpdateOptions(**base)


def test_flow_up_to_date_inline(git_env: GitEnv, tmp_path, monkeypatch) -> None:
    """已是最新：sourceUpdate skipped，Runtime 步骤照旧（HEAD 未变不接力）。"""
    from local_webpage_access import update_flow

    ws_root = _make_workspace(tmp_path)
    calls = []
    monkeypatch.setattr(
        update_flow, "run_pip_install", lambda repo: calls.append("pip") or "pip ok"
    )
    launched = []
    monkeypatch.setattr(
        update_flow,
        "_launch_continuation",
        lambda *a, **k: launched.append(1) or (None, "不应接力"),
    )
    report = run_update_flow(ws_root, _flow_options(git_env.repo))
    src = report.step("sourceUpdate")
    assert src is not None and src.status == "skipped"
    assert "已是最新" in src.message
    assert calls == ["pip"]
    assert launched == []  # 未改 HEAD 不接力


def test_flow_fetch_warning_degrades_offline(git_env: GitEnv, tmp_path, monkeypatch) -> None:
    """断网/代理失效：sourceUpdate warning，仍以本地代码完成全部步骤。"""
    from local_webpage_access import update_flow

    ws_root = _make_workspace(tmp_path)
    monkeypatch.setattr(update_flow, "run_pip_install", lambda repo: "pip ok")
    monkeypatch.setattr(
        update_flow.us,
        "fetch_candidate",
        lambda repo, target, **kw: (_ for _ in ()).throw(us.FetchError("网络不可达")),
    )
    report = run_update_flow(ws_root, _flow_options(git_env.repo))
    src = report.step("sourceUpdate")
    assert src.status == "warning"
    assert report.step("pip").status == "ok"
    assert not report.has_failures
    assert report.has_warnings


def test_flow_dirty_rejected_inline_continues(git_env: GitEnv, tmp_path, monkeypatch) -> None:
    """tracked 脏：拒绝快进、工作树不变；后续以本地代码继续但整体 failed。"""
    from local_webpage_access import update_flow

    ws_root = _make_workspace(tmp_path)
    git_env.push_commit("a.txt", "1", "V0.7.11")
    _git(git_env.repo, "reset", "-q", "--hard", "origin/main~1")  # 构造 behind
    # tracked 修改（勿弄脏 pyproject.toml——locate_repo 依赖其 TOML 可解析）
    (git_env.repo / "README.md").write_text("dirty", encoding="utf-8")
    monkeypatch.setattr(update_flow, "run_pip_install", lambda repo: "pip ok")
    report = run_update_flow(ws_root, _flow_options(git_env.repo))
    src = report.step("sourceUpdate")
    assert src.status == "failed"
    assert src.extra.get("errorKind") == "dirty"
    assert report.has_failures


def test_flow_skip_pip_conflict_before_ff(git_env: GitEnv, tmp_path, monkeypatch) -> None:
    """behind + --skip-pip：快进前拒绝、工作树不变。"""
    from local_webpage_access import update_flow

    ws_root = _make_workspace(tmp_path)
    git_env.push_commit("a.txt", "1", "V0.7.11")
    _git(git_env.repo, "reset", "-q", "--hard", "origin/main~1")
    head_before = _git(git_env.repo, "rev-parse", "HEAD").strip()
    monkeypatch.setattr(update_flow, "run_pip_install", lambda repo: pytest.fail("不应执行 pip"))
    report = run_update_flow(ws_root, _flow_options(git_env.repo, skip_pip=True))
    src = report.step("sourceUpdate")
    assert src.status == "failed"
    assert src.extra.get("errorKind") == "skip_pip_conflict"
    assert _git(git_env.repo, "rev-parse", "HEAD").strip() == head_before
    assert report.step("pip").status == "skipped"


def test_flow_head_changed_launches_continuation(git_env: GitEnv, tmp_path, monkeypatch) -> None:
    """HEAD 变化：pip 后必须交新解释器接力；旧进程不跑 Runtime 后半段。"""
    from local_webpage_access import update_flow

    ws_root = _make_workspace(tmp_path)
    candidate = git_env.push_commit("a.txt", "1", "V0.7.12-Build3000")
    _git(git_env.repo, "reset", "-q", "--hard", "origin/main~1")

    runtime_called = []
    monkeypatch.setattr(update_flow, "run_pip_install", lambda repo: "pip ok")

    def fake_runtime_phase(ws, config, registry, options, report):
        runtime_called.append("parent")
        report.steps.append(StepResult("syncSkills", "ok", "parent ran (BAD)"))

    monkeypatch.setattr(update_flow, "_run_runtime_phase", fake_runtime_phase)

    captured: dict = {}

    def fake_launch(ws, options, locks, *, old_head, new_head, timeout):
        captured.update(old_head=old_head, new_head=new_head)
        child = UpdateReport(
            workspace=str(ws.root),
            repo=None,
            version_before="0.7.11",
            version_after="0.7.12",
            steps=[StepResult("syncSkills", "ok", "child ran (GOOD)")],
        )
        return child, None

    monkeypatch.setattr(update_flow, "_launch_continuation", fake_launch)
    report = run_update_flow(ws_root, _flow_options(git_env.repo))
    assert runtime_called == []  # 旧进程绝不执行 Runtime 后半段
    assert captured.get("new_head") == candidate
    assert report.step("sourceUpdate").status == "ok"
    assert report.step("pip").status == "ok"
    assert any("child ran" in s.message for s in report.steps)
    assert report.version_after == "0.7.12"


def test_flow_pip_failure_after_ff_blocks_continuation(
    git_env: GitEnv, tmp_path, monkeypatch
) -> None:
    """快进后 pip 失败：阻断 continuation，附恢复链。"""
    from local_webpage_access import update_flow

    ws_root = _make_workspace(tmp_path)
    git_env.push_commit("a.txt", "1", "V0.7.12-Build3000")
    _git(git_env.repo, "reset", "-q", "--hard", "origin/main~1")
    monkeypatch.setattr(
        update_flow, "run_pip_install", lambda repo: (_ for _ in ()).throw(RuntimeError("pip boom"))
    )
    launched = []
    monkeypatch.setattr(
        update_flow,
        "_launch_continuation",
        lambda *a, **k: launched.append(1) or (None, "不应接力"),
    )
    report = run_update_flow(ws_root, _flow_options(git_env.repo))
    cont = report.step("continuation")
    assert cont is not None and cont.status == "failed"
    assert "git reset --keep" in cont.message
    assert launched == []


def test_flow_no_pull_keeps_old_semantics(git_env: GitEnv, tmp_path, monkeypatch) -> None:
    """--no-pull：步骤集合/语义与旧基线兼容，不联网。"""
    from local_webpage_access import update_flow

    ws_root = _make_workspace(tmp_path)
    fetches = []
    real_fetch = update_flow.us.fetch_candidate

    def spy(repo, target, **kw):
        fetches.append(1)
        return real_fetch(repo, target, **kw)

    monkeypatch.setattr(update_flow.us, "fetch_candidate", spy)
    monkeypatch.setattr(update_flow, "run_pip_install", lambda repo: "pip ok")
    from local_webpage_access import updater as updater_mod

    monkeypatch.setattr(updater_mod, "run_pip_install", lambda repo: "pip ok")
    report = run_update_flow(ws_root, _flow_options(git_env.repo, pull=False))
    assert report.step("sourceUpdate").status == "skipped"
    assert fetches == []
    assert report.step("pip").status == "ok"


def test_flow_dry_run_zero_write(git_env: GitEnv, tmp_path, monkeypatch) -> None:
    """dry-run：不联网、不 fetch、不取锁、零写入，标记 fresh=false。"""
    from local_webpage_access import update_flow

    ws_root = _make_workspace(tmp_path)
    git_env.push_commit("a.txt", "1", "V0.7.12-Build3000")  # 刷新 tracking ref 后不 reset
    _git(git_env.repo, "reset", "-q", "--hard", "origin/main~1")
    head_before = _git(git_env.repo, "rev-parse", "HEAD").strip()

    fetches = []
    monkeypatch.setattr(update_flow.us, "fetch_candidate", lambda *a, **k: fetches.append(1) or "")
    locks_taken = []
    monkeypatch.setattr(
        update_flow.us,
        "acquire_update_locks",
        lambda repo, ws: locks_taken.append(1) or us.UpdateLocks(-1, Path("/x"), -1, Path("/y")),
    )
    # BUG-530：dry-run 零写入——不创建/迁移 registry SQLite
    db_path = Workspace(ws_root).db_path
    assert not db_path.exists()
    report = run_update_flow(ws_root, _flow_options(git_env.repo, dry_run=True))
    assert fetches == [] and locks_taken == []
    src = report.step("sourceUpdate")
    assert src.status == "skipped"
    assert src.extra.get("fresh") is False
    assert not db_path.exists(), "dry-run 不得创建 registry DB"
    assert _git(git_env.repo, "rev-parse", "HEAD").strip() == head_before


def test_flow_dry_run_missing_tracking_ref(git_env: GitEnv, tmp_path, monkeypatch) -> None:
    """dry-run 缺缓存 ref：sourceUpdate=skipped + reason=tracking_ref_missing，退出 0。"""
    from local_webpage_access import update_flow

    ws_root = _make_workspace(tmp_path)
    # 显式 --remote/--ref + 删除远端跟踪 ref（@{upstream} 解析依赖该 ref，故用显式目标）
    _git(git_env.repo, "update-ref", "-d", "refs/remotes/origin/main")
    monkeypatch.setattr(update_flow, "run_pip_install", lambda repo: "pip ok")
    report = run_update_flow(
        ws_root, _flow_options(git_env.repo, dry_run=True, remote="origin", ref="main")
    )
    src = report.step("sourceUpdate")
    assert src.status == "skipped"
    assert src.extra.get("reason") == "tracking_ref_missing"
    assert src.extra.get("fresh") is False
    assert not report.has_failures


def test_flow_lock_busy_fail_fast(git_env: GitEnv, tmp_path, monkeypatch) -> None:
    """双 update 竞争：后者 fail-fast，不触碰 pip/进程/registry。"""
    from local_webpage_access import update_flow

    ws_root = _make_workspace(tmp_path)
    locks = us.acquire_update_locks(git_env.repo, ws_root)
    try:
        # 双层 mock：新编排层与 updater 内联层都不得执行 pip（CHK-223 加强）
        monkeypatch.setattr(
            update_flow, "run_pip_install", lambda repo: pytest.fail("锁忙时不得执行 pip")
        )
        from local_webpage_access import updater as updater_mod

        monkeypatch.setattr(
            updater_mod, "run_pip_install", lambda repo: pytest.fail("锁忙时不得执行 pip")
        )
        report = run_update_flow(ws_root, _flow_options(git_env.repo))
        src = report.step("sourceUpdate")
        assert src.status == "failed"
        assert "锁" in src.message
        # BUG-529：锁忙后不得进入 Runtime 后半段（无 pip/重启/同步步骤）
        runtime_steps = [s.name for s in report.steps if s.name not in ("sourceUpdate",)]
        assert runtime_steps == []
    finally:
        locks.close()


def test_flow_non_git_install_skips_source(git_env: GitEnv, tmp_path, monkeypatch) -> None:
    """非 git 安装：skipped + 迁移指引，不自动 clone。"""
    from local_webpage_access import update_flow

    ws_root = _make_workspace(tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "pyproject.toml").write_text(
        "[project]\nname='local-webpage-access'\n", encoding="utf-8"
    )
    monkeypatch.setattr(update_flow, "run_pip_install", lambda repo: "pip ok")
    from local_webpage_access import updater as updater_mod

    monkeypatch.setattr(updater_mod, "run_pip_install", lambda repo: "pip ok")
    report = run_update_flow(ws_root, _flow_options(plain))
    src = report.step("sourceUpdate")
    assert src.status == "skipped"
    assert "clone" in src.message
    assert report.step("pip").status == "ok"


# ---- 063.08 continuation 拒绝路径（真实子进程） ------------------------------------


def test_continuation_refuses_without_env_marker(tmp_path: Path) -> None:
    """缺协议/环境标记：在任何 Runtime 写入前拒绝（exit 3）。"""
    import os
    import sys

    payload = {
        "schemaVersion": 1,
        "oldHead": "0" * 40,
        "newHead": "1" * 40,
        "workspace": str(tmp_path),
        "options": {},
        "repoLockFd": 0,
        "workspaceLockFd": 1,
    }
    env = {k: v for k, v in os.environ.items() if k != "LWA_UPDATE_CONTINUATION"}
    res = subprocess.run(
        [sys.executable, "-m", "local_webpage_access.update_continuation"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert res.returncode == 3
    assert "refused" in (res.stderr or "")


def test_continuation_refuses_bad_schema(tmp_path: Path) -> None:
    import os
    import sys

    payload = {"schemaVersion": 999, "workspace": str(tmp_path)}
    env = {**os.environ, "LWA_UPDATE_CONTINUATION": "1"}
    res = subprocess.run(
        [sys.executable, "-m", "local_webpage_access.update_continuation"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert res.returncode == 3


# ---- 063.10 CLI check / 互斥 ----------------------------------------------------


def test_cli_check_and_dry_run_mutually_exclusive() -> None:
    from typer.testing import CliRunner

    from local_webpage_access.cli import app

    result = CliRunner().invoke(app, ["update", "--check", "--dry-run"])
    assert result.exit_code == 2
    assert "互斥" in result.output


def test_cli_check_without_workspace(git_env: GitEnv) -> None:
    """--check：无 workspace 可用（不 require_workspace），不改工作树。"""
    from typer.testing import CliRunner

    from local_webpage_access.cli import app

    git_env.push_commit("a.txt", "1", "V0.7.12-Build3000")
    _git(git_env.repo, "reset", "-q", "--hard", "origin/main~1")
    head_before = _git(git_env.repo, "rev-parse", "HEAD").strip()
    result = CliRunner().invoke(app, ["update", "--check", "--json", "--repo", str(git_env.repo)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "updateAvailable"
    assert payload["target"]["version"] == "0.7.12"
    # 不改工作树
    assert _git(git_env.repo, "rev-parse", "HEAD").strip() == head_before


def test_cli_check_unavailable_exit2(git_env: GitEnv) -> None:
    from typer.testing import CliRunner

    from local_webpage_access.cli import app

    _git(git_env.repo, "remote", "set-url", "origin", str(git_env.repo.parent / "missing.git"))
    result = CliRunner().invoke(app, ["update", "--check", "--repo", str(git_env.repo)])
    assert result.exit_code == 2


def test_cli_check_human_output(git_env: GitEnv) -> None:
    from typer.testing import CliRunner

    from local_webpage_access.cli import app

    result = CliRunner().invoke(app, ["update", "--check", "--repo", str(git_env.repo)])
    assert result.exit_code == 0
    assert "已是最新" in result.output


def test_launch_continuation_parses_report_on_failure_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BUG-531：Runtime 步骤失败时 continuation exit 1 但打印完整 JSON，
    父进程必须解析并返回子报告，而非退化为纯退出码错误丢失失败细节。"""
    from local_webpage_access import update_flow

    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    new_head = "f" * 40
    child = UpdateReport(
        workspace=str(ws_root),
        repo=None,
        version_before="0.8.0",
        version_after="0.8.0",
    )
    child.steps.append(StepResult("migrateConfig", "failed", "schema 迁移失败"))
    ack = {
        "schemaVersion": update_flow.HANDOFF_SCHEMA_VERSION,
        "ok": False,
        "codeHead": new_head,
        "report": child.to_dict(),
    }

    class _FakeProc:
        returncode = 1

        def communicate(self, input=None, timeout=None):  # noqa: A002
            return json.dumps(ack, ensure_ascii=False), ""

        def kill(self) -> None:
            return None

    monkeypatch.setattr(update_flow.subprocess, "Popen", lambda *a, **k: _FakeProc())

    locks = us.UpdateLocks(-1, tmp_path, -1, ws_root)
    options = UpdateOptions()
    report, error = update_flow._launch_continuation(
        Workspace(ws_root),
        options,
        locks,
        old_head="0" * 40,
        new_head=new_head,
        timeout=30,
    )
    assert error is None
    assert report is not None
    step = report.step("migrateConfig")
    assert step is not None and step.status == "failed"


def test_launch_continuation_crash_without_json_keeps_exit_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BUG-531：continuation 真崩溃（无可解析 JSON）仍报退出码错误。"""
    from local_webpage_access import update_flow

    ws_root = tmp_path / "ws"
    ws_root.mkdir()

    class _FakeProc:
        returncode = 2

        def communicate(self, input=None, timeout=None):  # noqa: A002
            return "Traceback (most recent call last):\nboom\n", ""

        def kill(self) -> None:
            return None

    monkeypatch.setattr(update_flow.subprocess, "Popen", lambda *a, **k: _FakeProc())

    locks = us.UpdateLocks(-1, tmp_path, -1, ws_root)
    report, error = update_flow._launch_continuation(
        Workspace(ws_root),
        UpdateOptions(),
        locks,
        old_head="0" * 40,
        new_head="f" * 40,
        timeout=30,
    )
    assert report is None
    assert error is not None and "退出码 2" in error


# ---- BUG-536：behindBy 截断保留真实总数 ----------------------------------------


def test_source_check_behind_by_keeps_true_total(git_env: GitEnv, monkeypatch) -> None:
    """落后数超过 JSON 上限时 behindBy 仍为真实总数，列表截断 + truncated 标记。

    用 monkeypatch 缩小 BEHIND_JSON_LIMIT（等价于制造 >100 个落后提交），
    避免测试里造 101 个真实提交拖慢套件。
    """
    original = _git(git_env.repo, "rev-parse", "HEAD").strip()
    for i in range(5):
        git_env.push_commit(f"t{i}.txt", "1", f"V0.7.9-Build{i}")
    _git(git_env.repo, "reset", "-q", "--hard", original)
    monkeypatch.setattr(us, "BEHIND_JSON_LIMIT", 3)

    report = us.run_source_check(git_env.repo)
    d = report.to_dict()
    assert d["behindBy"] == 5  # rev-list 真实总数，不是截断条数
    assert len(d["behind"]) == 3
    assert d["truncated"] is True
