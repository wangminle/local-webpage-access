"""IMP-065 GitHub 源一键导入测试（阶段 A–H 定向回归矩阵）。

红线（§17.5）：git 测试统一用**临时 bare remote + clone 夹具**（对齐
``tests/test_update_source.py``），禁止依赖外网与真实 GitHub。clone 层通过
``clone_url`` 参数 / monkeypatch 注入已解析 target，不绕过阶段 A 的 URL
纯函数（065.05：两层分开测）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from local_webpage_access import git_source
from local_webpage_access.config import Config
from local_webpage_access.errors import GitSourceError, ZipImportError
from local_webpage_access.importer import Importer, apply_detection_to_manifest
from local_webpage_access.models import InstanceManifest
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry
from local_webpage_access.scanner import Scanner

# ---- git 夹具（零外网 bare remote）------------------------------------------


def _track_mkdtemp(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """记录 ``git_source.tempfile.mkdtemp`` 创建的目录，供失败后无残留断言。"""
    created: list[Path] = []
    real = git_source.tempfile.mkdtemp

    def wrapper(*args: object, **kwargs: object) -> str:
        path = Path(real(*args, **kwargs))
        created.append(path)
        return str(path)

    monkeypatch.setattr(git_source.tempfile, "mkdtemp", wrapper)
    return created




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


def _git_run(cmd: list[str]) -> None:
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert res.returncode == 0, f"{' '.join(cmd)} 失败：{res.stderr}"


def _configure(repo: Path) -> None:
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "lwa-test")


def _commit(repo: Path, filename: str, content: str, subject: str) -> str:
    target = repo / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--allow-empty", "-m", subject)
    return _git(repo, "rev-parse", "HEAD").strip()


class GitEnv:
    """临时 bare remote（默认分支 ``trunk``，验证真实默认分支名捕获）。

    两个种子提交 + 一个 annotated tag；``push_commit`` 推新提交供更新测试。
    """

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.remote = tmp / "remote.git"
        self.remote.mkdir(parents=True)
        _git(self.remote, "init", "--bare", "-b", "trunk")

        seed = tmp / "seed"
        _git_run(["git", "init", "-b", "trunk", str(seed)])
        _configure(seed)
        (seed / "index.html").write_text(
            "<html><head><title>Git Site</title></head><body>v1</body></html>",
            encoding="utf-8",
        )
        _git(seed, "add", "-A")
        _git(seed, "commit", "-m", "seed v1")
        # 第二个提交：保证 --depth 1 克隆真正截断历史
        (seed / "about.html").write_text("<html><body>about v1</body></html>", encoding="utf-8")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-m", "seed v1.1")
        self.head = _git(seed, "rev-parse", "HEAD").strip()
        _git(seed, "remote", "add", "origin", str(self.remote))
        _git(seed, "push", "origin", "trunk")
        # annotated tag：OID ≠ commit OID，验证 peeled 探测
        _git(seed, "tag", "-a", "v1.0", "-m", "release v1.0")
        _git(seed, "push", "origin", "v1.0")
        self.seed = seed

    def head_oid(self) -> str:
        return _git(self.seed, "rev-parse", "trunk").strip()

    def tag_commit_oid(self) -> str:
        return _git(self.seed, "rev-parse", "v1.0^{commit}").strip()

    def push_commit(self, filename: str, content: str, subject: str) -> str:
        oid = _commit(self.seed, filename, content, subject)
        _git(self.seed, "push", "origin", "trunk")
        return oid

    def push_empty_commit(self, subject: str) -> str:
        """推一个内容不变的空提交（CHK-239 HIGH-2：OID 前进但打包内容相同）。"""
        _git(self.seed, "commit", "--allow-empty", "-m", subject)
        _git(self.seed, "push", "origin", "trunk")
        return self.head_oid()


@pytest.fixture()
def git_env(tmp_path: Path) -> GitEnv:
    return GitEnv(tmp_path)


# ---- workspace / importer 夹具（对齐 test_folder_source.py）-----------------


@pytest.fixture()
def workspace(workspace_root: Path) -> Workspace:
    ws = Workspace(workspace_root)
    ws.ensure_workspace_dirs()
    return ws


@pytest.fixture()
def registry(workspace_root: Path) -> Registry:
    workspace_root.joinpath("registry").mkdir(parents=True, exist_ok=True)
    reg = Registry(workspace_root / "registry" / "local-web.db")
    reg.open()
    yield reg
    reg.close()


@pytest.fixture()
def importer(workspace: Workspace, registry: Registry) -> Importer:
    return Importer(workspace, Config(), registry)


def _patch_git_remote(monkeypatch: pytest.MonkeyPatch, env: GitEnv) -> None:
    """把 git_source 的物理克隆/探测端点注入本地 bare remote（零外网）。"""
    real_stage = git_source.stage_git_clone

    def fake_stage(target, *, ref=None, clone_url=None, **kwargs):  # noqa: ANN002, ANN003
        return real_stage(target, ref=ref, clone_url=str(env.remote), **kwargs)

    monkeypatch.setattr(git_source, "stage_git_clone", fake_stage)

    real_probe = git_source.probe_remote_commit

    def fake_probe(url, *, ref, ref_kind, **kwargs):  # noqa: ANN001, ANN003
        return real_probe(str(env.remote), ref=ref, ref_kind=ref_kind, **kwargs)

    monkeypatch.setattr(git_source, "probe_remote_commit", fake_probe)


def _kind_of(exc: pytest.ExceptionInfo[GitSourceError]) -> str:
    return exc.value.context.get("kind")


def test_skills_readme_git_row_not_loopback_limited() -> None:
    """065.d：git 源管理页不限 loopback；索引行不得照抄 folder 版口径。"""
    readme = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "local_webpage_access"
        / "skills"
        / "README.md"
    )
    git_rows = [
        line
        for line in readme.read_text(encoding="utf-8").splitlines()
        if "lwa-import-git" in line and line.lstrip().startswith("|")
    ]
    assert git_rows, "skills/README.md 缺少 lwa-import-git 索引行"
    row = git_rows[0]
    assert "仅 loopback" not in row
    assert "不限" in row


# ---- 阶段 A（065.01–04）：URL 规范化与拒绝矩阵 -------------------------------


class TestParseGithubUrl:
    def test_happy_path_preserves_owner_case(self) -> None:
        t = git_source.parse_github_url("https://github.com/Foo/Bar")
        assert t == git_source.ParsedGitTarget(
            url="https://github.com/Foo/Bar", owner="Foo", repo="Bar"
        )

    def test_git_suffix_and_trailing_slash_normalized(self) -> None:
        assert git_source.parse_github_url("https://github.com/Foo/Bar.git/").url == (
            "https://github.com/Foo/Bar"
        )
        assert git_source.parse_github_url("HTTPS://GITHUB.COM/Foo/Bar").url == (
            "https://github.com/Foo/Bar"
        )

    def test_explicit_443_allowed(self) -> None:
        assert git_source.parse_github_url("https://github.com:443/Foo/Bar").url == (
            "https://github.com/Foo/Bar"
        )

    @pytest.mark.parametrize(
        "raw",
        [
            "http://github.com/Foo/Bar",  # 非 https
            "ssh://git@github.com/Foo/Bar.git",
            "git://github.com/Foo/Bar.git",
            "file:///tmp/repo",
            "git@github.com:Foo/Bar.git",  # SCP 形态（无 scheme）
            "github.com/Foo/Bar",  # 无 scheme
        ],
    )
    def test_reject_non_https_scheme(self, raw: str) -> None:
        with pytest.raises(GitSourceError) as ei:
            git_source.parse_github_url(raw)
        assert _kind_of(ei) == "invalid_url"

    def test_reject_userinfo(self) -> None:
        with pytest.raises(GitSourceError) as ei:
            git_source.parse_github_url("https://user:pass@github.com/Foo/Bar")
        assert _kind_of(ei) == "userinfo_forbidden"

    @pytest.mark.parametrize(
        "raw",
        [
            "https://github.com/Foo/Bar?tab=readme",
            "https://github.com/Foo/Bar#readme",
            # Windmill 型：fragment 拆开校验 host 与拨号 host
            "https://github.com/Foo/Bar#@github.com",
        ],
    )
    def test_reject_query_and_fragment(self, raw: str) -> None:
        with pytest.raises(GitSourceError) as ei:
            git_source.parse_github_url(raw)
        assert _kind_of(ei) == "invalid_url"

    @pytest.mark.parametrize(
        "raw",
        [
            "https://www.github.com/Foo/Bar",
            "https://gist.github.com/Foo/Bar",
            "https://github.com.evil.tld/Foo/Bar",
            "https://gitlab.com/Foo/Bar",
            "https://127.0.0.1/Foo/Bar",
        ],
    )
    def test_reject_non_github_host(self, raw: str) -> None:
        with pytest.raises(GitSourceError) as ei:
            git_source.parse_github_url(raw)
        assert _kind_of(ei) == "host_not_allowed"

    def test_reject_userinfo_host_disguise(self) -> None:
        """webpack userinfo 型：``github.com@evil.com`` 的真实 host 是 evil.com。
        username 非空先命中 userinfo_forbidden，host 校验兜底——两种都必须拒绝。"""
        with pytest.raises(GitSourceError) as ei:
            git_source.parse_github_url("https://github.com@evil.com/Foo/Bar")
        assert _kind_of(ei) in {"userinfo_forbidden", "host_not_allowed"}

    def test_reject_non_443_port(self) -> None:
        with pytest.raises(GitSourceError) as ei:
            git_source.parse_github_url("https://github.com:8443/Foo/Bar")
        assert _kind_of(ei) == "host_not_allowed"

    @pytest.mark.parametrize(
        "raw",
        [
            "https://github.com/Foo/Bar/tree/main/src",
            "https://github.com/Foo/Bar/blob/main/README.md",
            "https://github.com/Foo/Bar/releases",
            "https://github.com/Foo/Bar/issues/1",
            "https://github.com/Foo/Bar/pull/2",
            "https://github.com/Foo/Bar/archive/refs/heads/main.zip",
        ],
    )
    def test_reject_web_path_segments(self, raw: str) -> None:
        with pytest.raises(GitSourceError) as ei:
            git_source.parse_github_url(raw)
        assert _kind_of(ei) == "invalid_url"
        # /tree/、/blob/ 等必须提示改用仓库根 + --ref / --subdir
        assert "--ref" in ei.value.message or "仓库根" in ei.value.message

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "https://github.com/",
            "https://github.com/onlyowner",
            "https://github.com/a/b/c",
        ],
    )
    def test_reject_bad_path_shape(self, raw: str) -> None:
        with pytest.raises(GitSourceError) as ei:
            git_source.parse_github_url(raw)
        assert _kind_of(ei) == "invalid_url"

    @pytest.mark.parametrize(
        "raw",
        [
            "https://[github.com]/owner/repo",
            "https://github.com]/owner/repo",
            "https://[github.com/owner/repo",
        ],
    )
    def test_malformed_netloc_returns_invalid_url_not_valueerror(self, raw: str) -> None:
        """BUG-560（CHK-239 M1）：畸形 netloc 不得抛裸 ValueError（CLI traceback /
        API 500），必须落闭集 invalid_url。"""
        with pytest.raises(GitSourceError) as ei:
            git_source.parse_github_url(raw)
        assert _kind_of(ei) == "invalid_url"

    def test_allowed_hosts_injection(self) -> None:
        """夹具可注入 host 列表（065.c：常量可测注入），但精确相等语义不变。"""
        t = git_source.parse_github_url(
            "https://gitea.local/Foo/Bar", allowed_hosts=("github.com", "gitea.local")
        )
        assert t.url == "https://gitea.local/Foo/Bar"

    def test_error_kinds_closed_set(self) -> None:
        """闭集自洽：解析层只会产出闭集内的 kind（065.p）。"""
        with pytest.raises(GitSourceError) as ei:
            git_source.parse_github_url("https://gitlab.com/a/b")
        assert _kind_of(ei) in git_source.ERROR_KINDS


# ---- 阶段 B（065.05–09）：clone 护栏 -----------------------------------------


class TestStageGitClone:
    def test_default_branch_real_name_captured(self, git_env: GitEnv) -> None:
        """默认分支夹具名非 main 时仍记下真名（065.k，禁止存 HEAD）。"""
        target = git_source.parse_github_url("https://github.com/acme/mysite")
        with git_source.stage_git_clone(target, clone_url=str(git_env.remote)) as clone:
            assert clone.ref == "trunk"
            assert clone.ref_kind == "branch"
            assert clone.commit == git_env.head_oid()
            assert len(clone.commit) == 40
            assert (clone.directory / "index.html").is_file()
            # staging 在工作区外（065.i）
            assert clone.directory != Path("/")

    def test_explicit_branch(self, git_env: GitEnv) -> None:
        target = git_source.parse_github_url("https://github.com/acme/mysite")
        with git_source.stage_git_clone(
            target, ref="trunk", clone_url=str(git_env.remote)
        ) as clone:
            assert (clone.ref, clone.ref_kind) == ("trunk", "branch")

    def test_tag_clone_records_kind_tag_and_peeled_commit(self, git_env: GitEnv) -> None:
        target = git_source.parse_github_url("https://github.com/acme/mysite")
        with git_source.stage_git_clone(
            target, ref="v1.0", clone_url=str(git_env.remote)
        ) as clone:
            assert clone.ref_kind == "tag"
            assert clone.ref == "v1.0"
            # checkout 的是 commit（peeled），不是 annotated tag 对象
            assert clone.commit == git_env.tag_commit_oid()

    def test_staging_removed_on_success_and_failure(
        self, git_env: GitEnv, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = git_source.parse_github_url("https://github.com/acme/mysite")
        seen: list[Path] = []
        with git_source.stage_git_clone(target, clone_url=str(git_env.remote)) as clone:
            seen.append(clone.directory)
            assert clone.directory.exists()
        assert not seen[0].exists()  # 成功后删除

        created = _track_mkdtemp(monkeypatch)
        bad = git_source.parse_github_url("https://github.com/acme/mysite")
        with pytest.raises(GitSourceError) as ei:
            with git_source.stage_git_clone(
                bad, clone_url=str(git_env.tmp / "not-exist.git")
            ):
                pass  # pragma: no cover - 不会到达
        assert _kind_of(ei) == "remote_unreachable"
        assert created, "失败路径必须实际创建过 staging/template"
        assert all(not p.exists() for p in created)

    def test_second_mkdtemp_failure_cleans_first(
        self, git_env: GitEnv, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """第二个 mkdtemp 失败时，第一个临时目录也必须清掉（不得泄漏）。"""
        real = git_source.tempfile.mkdtemp
        created: list[Path] = []
        n = {"c": 0}

        def wrapper(*args: object, **kwargs: object) -> str:
            n["c"] += 1
            if n["c"] >= 2:
                raise OSError("no space left on device")
            path = Path(real(*args, **kwargs))
            created.append(path)
            return str(path)

        monkeypatch.setattr(git_source.tempfile, "mkdtemp", wrapper)
        target = git_source.parse_github_url("https://github.com/acme/mysite")
        with pytest.raises(OSError, match="no space"):
            with git_source.stage_git_clone(target, clone_url=str(git_env.remote)):
                pass  # pragma: no cover
        assert created
        assert all(not p.exists() for p in created)

    def test_staging_outside_workspace_root(self, git_env: GitEnv, workspace: Workspace) -> None:
        target = git_source.parse_github_url("https://github.com/acme/mysite")
        with git_source.stage_git_clone(target, clone_url=str(git_env.remote)) as clone:
            resolved = clone.directory.resolve()
            ws_root = workspace.root.resolve()
            assert resolved != ws_root
            assert ws_root not in resolved.parents

    def test_concurrent_clones_use_distinct_staging(self, git_env: GitEnv) -> None:
        target = git_source.parse_github_url("https://github.com/acme/mysite")
        with git_source.stage_git_clone(target, clone_url=str(git_env.remote)) as c1:
            with git_source.stage_git_clone(target, clone_url=str(git_env.remote)) as c2:
                assert c1.directory != c2.directory  # 065.o：每路独立 tempfile

    def test_ref_not_found(self, git_env: GitEnv) -> None:
        target = git_source.parse_github_url("https://github.com/acme/mysite")
        with pytest.raises(GitSourceError) as ei:
            with git_source.stage_git_clone(
                target, ref="no-such-branch", clone_url=str(git_env.remote)
            ):
                pass  # pragma: no cover
        assert _kind_of(ei) == "ref_not_found"

    def test_clone_timeout_kills_and_cleans(
        self, git_env: GitEnv, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_runner = git_source._run_git_no_prompt
        staging_dirs: list[Path] = []

        def fake_runner(argv, *, timeout):  # noqa: ANN001
            if "clone" in argv:
                # argv 尾项是 dest；其父目录即 mkdtemp 出的 staging
                staging_dirs.append(Path(argv[-1]).parent)
                raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)
            return real_runner(argv, timeout=timeout)

        monkeypatch.setattr(git_source, "_run_git_no_prompt", fake_runner)
        target = git_source.parse_github_url("https://github.com/acme/mysite")
        with pytest.raises(GitSourceError) as ei:
            with git_source.stage_git_clone(target, clone_url=str(git_env.remote)):
                pass  # pragma: no cover
        assert _kind_of(ei) == "clone_timeout"
        assert all(not p.exists() for p in staging_dirs)  # 超时无残留

    def test_size_exceeded_deletes_staging(
        self, git_env: GitEnv, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        created = _track_mkdtemp(monkeypatch)
        target = git_source.parse_github_url("https://github.com/acme/mysite")
        with pytest.raises(GitSourceError) as ei:
            with git_source.stage_git_clone(
                target, clone_url=str(git_env.remote), size_limit_bytes=1
            ):
                pass  # pragma: no cover
        assert _kind_of(ei) == "size_exceeded"
        assert created
        assert all(not p.exists() for p in created)

    def test_git_missing_fail_fast(self, git_env: GitEnv, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(git_source.shutil, "which", lambda _name: None)
        target = git_source.parse_github_url("https://github.com/acme/mysite")
        with pytest.raises(GitSourceError) as ei:
            with git_source.stage_git_clone(target, clone_url=str(git_env.remote)):
                pass  # pragma: no cover
        assert _kind_of(ei) == "git_missing"

    def test_resolve_ref_timeout_maps_to_closed_kind(
        self, git_env: GitEnv, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_resolve_ref_and_commit`` 超时必须进闭集，不得裸漏 TimeoutExpired。"""
        real_runner = git_source._run_git_no_prompt

        def fake_runner(argv, *, timeout):  # noqa: ANN001
            if "symbolic-ref" in argv or "rev-parse" in argv:
                raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)
            return real_runner(argv, timeout=timeout)

        monkeypatch.setattr(git_source, "_run_git_no_prompt", fake_runner)
        target = git_source.parse_github_url("https://github.com/acme/mysite")
        with pytest.raises(GitSourceError) as ei:
            with git_source.stage_git_clone(target, clone_url=str(git_env.remote)):
                pass  # pragma: no cover
        assert _kind_of(ei) in {"clone_timeout", "remote_unreachable"}


class TestCloneCommandGuards:
    """065.m：clone argv 与环境护栏。"""

    def test_argv_contains_shallow_and_hook_guards(self, tmp_path: Path) -> None:
        argv = git_source._build_clone_argv(
            "https://github.com/a/b", tmp_path / "dest", "dev", tmp_path / "tpl"
        )
        assert argv[:2] == ["git", "clone"]
        joined = " ".join(argv)
        assert "--depth 1" in joined
        assert "--single-branch" in joined
        assert "--no-tags" in joined
        assert "--template=" in joined
        assert "core.hooksPath=/dev/null" in joined
        assert "--branch dev" in joined
        assert "--recurse-submodules" not in joined

    def test_env_adds_lfs_skip_and_never_sets_nosystem(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
        monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
        env = git_source._clone_env()
        assert env["GIT_LFS_SKIP_SMUDGE"] == "1"
        assert env["LC_ALL"] == "C"
        assert env["LANG"] == "C"
        assert "GIT_CONFIG_NOSYSTEM" not in env  # 禁止设置（065.m）
        assert "GIT_CONFIG_GLOBAL" not in env  # 不清空用户 config

    def test_cloned_repo_has_no_active_hooks(
        self, git_env: GitEnv, tmp_path: Path
    ) -> None:
        """阳性对照：恶意 template 的 post-checkout 会被普通 clone 执行；
        ``stage_git_clone`` 空 template + hooksPath=/dev/null 不得触发。"""
        sentinel = tmp_path / "hook-fired"
        template = tmp_path / "evil-template"
        (template / "hooks").mkdir(parents=True)
        hook = template / "hooks" / "post-checkout"
        hook.write_text(
            f"#!/bin/sh\nprintf fired > '{sentinel}'\n",
            encoding="utf-8",
        )
        os.chmod(hook, 0o755)

        unguarded = tmp_path / "unguarded"
        env = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        }
        proc = subprocess.run(
            [
                "git",
                "clone",
                f"--template={template}",
                str(git_env.remote),
                str(unguarded),
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert proc.returncode == 0, proc.stderr
        assert sentinel.is_file(), "阳性对照：无护栏 clone 应执行 post-checkout"
        sentinel.unlink()

        target = git_source.parse_github_url("https://github.com/acme/mysite")
        with git_source.stage_git_clone(target, clone_url=str(git_env.remote)) as clone:
            hooks_dir = clone.directory / ".git" / "hooks"
            if hooks_dir.exists():
                active = [
                    p
                    for p in hooks_dir.iterdir()
                    if p.is_file() and p.name.endswith((".sample",)) is False
                ]
                assert active == []
        assert not sentinel.exists()


# ---- 阶段 D 前置（065.15）：ls-remote 探测 -----------------------------------


class TestProbeRemoteCommit:
    def test_branch_probe(self, git_env: GitEnv) -> None:
        oid = git_source.probe_remote_commit(
            str(git_env.remote), ref="trunk", ref_kind="branch"
        )
        assert oid == git_env.head_oid()

    def test_annotated_tag_probe_returns_peeled_commit(self, git_env: GitEnv) -> None:
        oid = git_source.probe_remote_commit(
            str(git_env.remote), ref="v1.0", ref_kind="tag"
        )
        assert oid == git_env.tag_commit_oid()  # peeled，非 tag 对象 OID

    def test_ref_not_found(self, git_env: GitEnv) -> None:
        with pytest.raises(GitSourceError) as ei:
            git_source.probe_remote_commit(
                str(git_env.remote), ref="gone", ref_kind="branch"
            )
        assert _kind_of(ei) == "ref_not_found"

    def test_remote_unreachable(self, tmp_path: Path) -> None:
        with pytest.raises(GitSourceError) as ei:
            git_source.probe_remote_commit(
                str(tmp_path / "missing.git"), ref="trunk", ref_kind="branch"
            )
        assert _kind_of(ei) == "remote_unreachable"


# ---- 阶段 C（065.10–14）：import_from_git ------------------------------------


class TestImportFromGit:
    def test_import_static_site_with_git_identity(
        self, git_env: GitEnv, importer: Importer, workspace: Workspace
    ) -> None:
        result = importer.import_from_git(
            "https://github.com/acme/mysite", clone_url=str(git_env.remote)
        )

        assert result.instance_id == "mysite"  # id_basis=repo 名
        assert result.detection.pending is False

        manifest = InstanceManifest.load(workspace.app_manifest_path(result.instance_id))
        assert manifest.sourceKind == "git"
        assert manifest.sourceDirPath is None  # 红线：staging 恒不落 sourceDirPath
        assert manifest.sourceGitUrl == "https://github.com/acme/mysite"
        assert manifest.sourceGitRef == "trunk"  # 真实默认分支名，非 HEAD
        assert manifest.sourceGitRefKind == "branch"
        assert manifest.sourceGitCommit == git_env.head_oid()
        assert manifest.sourceGitSubdir is None

        current = workspace.app_current(result.instance_id)
        assert (current / "index.html").is_file()

    def test_import_isolation_hard_assertions(
        self, git_env: GitEnv, importer: Importer, workspace: Workspace
    ) -> None:
        """065.14：实例树无 .git、无 symlink、运行根 ∈ apps/<id>。"""
        result = importer.import_from_git(
            "https://github.com/acme/mysite", clone_url=str(git_env.remote)
        )
        app_dir = workspace.app_dir(result.instance_id)
        assert app_dir.exists()
        assert workspace.root.resolve() in app_dir.resolve().parents

        for p in app_dir.rglob("*"):
            assert ".git" not in p.parts, f"实例树出现 .git：{p}"
            assert not p.is_symlink(), f"实例树出现 symlink：{p}"

    def test_import_with_subdir_packs_only_subdir(
        self, git_env: GitEnv, importer: Importer, workspace: Workspace
    ) -> None:
        # 造 monorepo：site/ 子目录才是要部署的静态站
        (git_env.seed / "site").mkdir(exist_ok=True)
        (git_env.seed / "site" / "index.html").write_text(
            "<html><body>subdir site</body></html>", encoding="utf-8"
        )
        (git_env.seed / "README.md").write_text("root readme", encoding="utf-8")
        _git(git_env.seed, "add", "-A")
        _git(git_env.seed, "commit", "-m", "add monorepo layout")
        _git(git_env.seed, "push", "origin", "trunk")

        result = importer.import_from_git(
            "https://github.com/acme/mysite",
            subdir="site",
            clone_url=str(git_env.remote),
        )
        current = workspace.app_current(result.instance_id)
        assert (current / "index.html").is_file()  # 子目录内容拍平到 current 根
        assert not (current / "README.md").exists()  # 仓库根文件未打入
        manifest = InstanceManifest.load(workspace.app_manifest_path(result.instance_id))
        assert manifest.sourceGitSubdir == "site"

    def test_import_subdir_missing(self, git_env: GitEnv, importer: Importer) -> None:
        with pytest.raises(ZipImportError):
            importer.import_from_git(
                "https://github.com/acme/mysite",
                subdir="nope",
                clone_url=str(git_env.remote),
            )

    def test_import_subdir_rejects_traversal(self, git_env: GitEnv, importer: Importer) -> None:
        with pytest.raises(ZipImportError):
            importer.import_from_git(
                "https://github.com/acme/mysite",
                subdir="../etc",
                clone_url=str(git_env.remote),
            )

    def test_import_bad_url_no_clone(self, git_env: GitEnv, importer: Importer) -> None:
        """URL 拒绝在 clone 之前（fail-fast，不触网）。"""
        with pytest.raises(GitSourceError) as ei:
            importer.import_from_git("https://gitlab.com/acme/mysite")
        assert _kind_of(ei) == "host_not_allowed"

    def test_import_no_halfway_instance_on_clone_failure(
        self, git_env: GitEnv, importer: Importer, workspace: Workspace
    ) -> None:
        with pytest.raises(GitSourceError):
            importer.import_from_git(
                "https://github.com/acme/mysite",
                clone_url=str(git_env.tmp / "missing.git"),
            )
        assert not list((workspace.root / "apps").glob("mysite*")) if (
            workspace.root / "apps"
        ).exists() else True

    def test_identity_writeback_failure_leaves_no_zip_instance(
        self,
        git_env: GitEnv,
        importer: Importer,
        workspace: Workspace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """zip 导入已成功、git 身份写回失败时不得留下 zip 形态半成品（CHK-240 / BUG-572）。"""
        from local_webpage_access import importer as importer_mod

        original_save = importer_mod.InstanceManifest.save

        def fail_git_identity_save(self, path):  # noqa: ANN001
            if getattr(self, "sourceKind", None) == "git":
                raise OSError("identity write boom")
            return original_save(self, path)

        monkeypatch.setattr(
            importer_mod.InstanceManifest, "save", fail_git_identity_save
        )

        with pytest.raises(ZipImportError, match="identity write boom"):
            importer.import_from_git(
                "https://github.com/acme/mysite",
                clone_url=str(git_env.remote),
            )

        apps = workspace.root / "apps"
        leftover = [p.name for p in apps.iterdir()] if apps.exists() else []
        assert leftover == []
        assert importer.registry.get_instance("mysite") is None

    def test_scan_preserves_git_identity(
        self, git_env: GitEnv, importer: Importer, workspace: Workspace
    ) -> None:
        """065.12：lwa scan 重扫不把 git 身份退回 zip/folder。"""
        result = importer.import_from_git(
            "https://github.com/acme/mysite", clone_url=str(git_env.remote)
        )
        manifest_path = workspace.app_manifest_path(result.instance_id)
        manifest = InstanceManifest.load(manifest_path)
        detection = Scanner().detect(workspace.app_current(result.instance_id))
        refreshed = apply_detection_to_manifest(manifest, detection, workspace)
        assert refreshed.sourceKind == "git"
        assert refreshed.sourceGitUrl == "https://github.com/acme/mysite"
        assert refreshed.sourceGitRef == "trunk"
        assert refreshed.sourceGitRefKind == "branch"
        assert refreshed.sourceGitCommit == git_env.head_oid()


# ---- 阶段 D（065.16–18）：update_from_git ------------------------------------


class TestUpdateFromGit:
    def _import(self, git_env: GitEnv, importer: Importer) -> str:
        result = importer.import_from_git(
            "https://github.com/acme/mysite", clone_url=str(git_env.remote)
        )
        return result.instance_id

    def test_no_change_short_circuit(
        self, git_env: GitEnv, importer: Importer, workspace: Workspace
    ) -> None:
        iid = self._import(git_env, importer)
        current = workspace.app_current(iid)
        index_before = (current / "index.html").read_bytes()
        mtime_dir_before = sorted(p.stat().st_mtime_ns for p in current.rglob("*"))

        result = importer.update_from_git(iid, clone_url=str(git_env.remote))
        assert result.skipped is True
        # 零重建：文件字节与树 mtime 均未变
        assert (current / "index.html").read_bytes() == index_before
        assert sorted(p.stat().st_mtime_ns for p in current.rglob("*")) == mtime_dir_before
        manifest = InstanceManifest.load(workspace.app_manifest_path(iid))
        assert manifest.sourceGitCommit == git_env.head_oid()

    def test_update_with_new_commit_preserves_identity(
        self, git_env: GitEnv, importer: Importer, workspace: Workspace
    ) -> None:
        iid = self._import(git_env, importer)
        row_before = importer.registry.get_instance(iid) or {}

        new_oid = git_env.push_commit(
            "index.html", "<html><body>v2</body></html>", "update to v2"
        )
        result = importer.update_from_git(iid, clone_url=str(git_env.remote))
        assert result.skipped is False

        current = workspace.app_current(iid)
        assert "v2" in (current / "index.html").read_text(encoding="utf-8")

        manifest = InstanceManifest.load(workspace.app_manifest_path(iid))
        assert manifest.sourceKind == "git"
        assert manifest.sourceGitUrl == "https://github.com/acme/mysite"
        assert manifest.sourceGitCommit == new_oid
        assert manifest.sourceGitRef == "trunk"
        # 返回的内存 manifest 必须与磁盘一致（不得留陈旧 commit/ref）
        assert result.manifest.sourceGitCommit == new_oid
        assert result.manifest.sourceGitRef == "trunk"
        assert result.manifest.sourceKind == "git"
        # id / 端口登记保留
        row_after = importer.registry.get_instance(iid) or {}
        assert row_after["id"] == iid
        assert row_after.get("created_at") == row_before.get("created_at")

    def test_dry_run_records_no_update_event(
        self, git_env: GitEnv, importer: Importer, workspace: Workspace
    ) -> None:
        """dry-run 更新不落盘，也不得记「已更新」事件（与 zip/文件夹路径一致）。"""
        iid = self._import(git_env, importer)
        old_oid = git_env.head_oid()
        git_env.push_commit("index.html", "<html><body>v2</body></html>", "v2")

        events_before = importer.registry.list_events(iid)
        result = importer.update_from_git(iid, clone_url=str(git_env.remote), dry_run=True)
        assert result.dry_run is True
        events_after = importer.registry.list_events(iid)
        new_events = [e for e in events_after if e not in events_before]
        assert all("git 源更新" not in (e.get("message") or "") for e in new_events)
        # 磁盘身份未动（仍是旧 commit）
        manifest = InstanceManifest.load(workspace.app_manifest_path(iid))
        assert manifest.sourceGitCommit == old_oid

    def test_dry_run_empty_commit_does_not_write_oid(
        self, git_env: GitEnv, importer: Importer, workspace: Workspace
    ) -> None:
        """远端 OID 前进但打包内容相同：dry-run 不得刷 sourceGitCommit、不得记事件。

        ``_update_zip_locked`` 在 hash 相同分支返回的 ``UpdateResult.dry_run``
        是默认 False；必须按 ``update_from_git`` 自己的 dry_run 形参判定。
        """
        iid = self._import(git_env, importer)
        old_oid = git_env.head_oid()
        new_oid = git_env.push_empty_commit("empty")
        assert new_oid != old_oid

        events_before = {e["id"] for e in importer.registry.list_events(iid)}
        result = importer.update_from_git(iid, clone_url=str(git_env.remote), dry_run=True)
        manifest = InstanceManifest.load(workspace.app_manifest_path(iid))
        assert manifest.sourceGitCommit == old_oid
        assert result.dry_run is True
        new_events = [
            e for e in importer.registry.list_events(iid) if e["id"] not in events_before
        ]
        assert new_events == []

    def test_same_content_new_oid_records_single_skip_event(
        self, git_env: GitEnv, importer: Importer, workspace: Workspace
    ) -> None:
        """非 dry-run：OID 前进、内容未变 → 刷新 commit，只记一条跳过类事件。"""
        iid = self._import(git_env, importer)
        new_oid = git_env.push_empty_commit("empty")

        events_before = {e["id"] for e in importer.registry.list_events(iid)}
        result = importer.update_from_git(iid, clone_url=str(git_env.remote))
        assert result.skipped is True
        manifest = InstanceManifest.load(workspace.app_manifest_path(iid))
        assert manifest.sourceGitCommit == new_oid
        new_msgs = [
            e.get("message") or ""
            for e in importer.registry.list_events(iid)
            if e["id"] not in events_before
        ]
        assert len(new_msgs) == 1
        assert "跳过" in new_msgs[0]
        assert "git 源更新" not in new_msgs[0]

    def test_clone_failure_keeps_current_running_content(
        self, git_env: GitEnv, importer: Importer, workspace: Workspace
    ) -> None:
        """远端推了新提交但 clone 失败：实例 current/ 不受影响。"""
        iid = self._import(git_env, importer)
        git_env.push_commit("index.html", "<html><body>v2</body></html>", "v2")
        current = workspace.app_current(iid)
        before = (current / "index.html").read_bytes()

        # 探测用真实 remote（发现有新提交），clone 指向坏路径
        real_stage = git_source.stage_git_clone

        import contextlib

        @contextlib.contextmanager
        def broken_stage(target, *, ref=None, clone_url=None, **kwargs):  # noqa: ANN001, ANN003
            with real_stage(
                target, ref=ref, clone_url=str(git_env.tmp / "missing.git"), **kwargs
            ) as clone:
                yield clone

        monkeypatch_direct = pytest.MonkeyPatch()
        monkeypatch_direct.setattr(git_source, "stage_git_clone", broken_stage)
        try:
            with pytest.raises(GitSourceError):
                importer.update_from_git(iid, clone_url=str(git_env.remote))
        finally:
            monkeypatch_direct.undo()
        assert (current / "index.html").read_bytes() == before

    def test_source_mismatch_rejected(
        self, git_env: GitEnv, importer: Importer
    ) -> None:
        iid = self._import(git_env, importer)
        with pytest.raises(GitSourceError) as ei:
            importer.update_from_git(
                iid,
                url="https://github.com/other/repo",
                clone_url=str(git_env.remote),
            )
        assert _kind_of(ei) == "source_mismatch"

    def test_matching_url_allowed(
        self, git_env: GitEnv, importer: Importer
    ) -> None:
        iid = self._import(git_env, importer)
        result = importer.update_from_git(
            iid,
            url="https://github.com/acme/mysite",
            clone_url=str(git_env.remote),
        )
        assert result.skipped is True

    def test_non_git_instance_rejected(
        self, git_env: GitEnv, importer: Importer, workspace: Workspace, tmp_path: Path
    ) -> None:
        # 先用 zip 导入一个普通实例
        zip_path = tmp_path / "plain.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("index.html", "<html></html>")
        result = importer.import_zip(zip_path)

        with pytest.raises(ZipImportError):
            importer.update_from_git(result.instance_id, clone_url=str(git_env.remote))

    def test_update_zip_rejects_git_instance(
        self, git_env: GitEnv, importer: Importer, tmp_path: Path
    ) -> None:
        """065.18：git 实例不能用 zip 覆盖更新（update_zip 公共入口护栏）。"""
        iid = self._import(git_env, importer)
        other_zip = tmp_path / "other.zip"
        with zipfile.ZipFile(other_zip, "w") as zf:
            zf.writestr("index.html", "<html>other</html></html>")
        with pytest.raises(ZipImportError):
            importer.update_zip(other_zip, iid)

    def test_update_from_dir_rejects_git_instance(
        self, git_env: GitEnv, importer: Importer
    ) -> None:
        iid = self._import(git_env, importer)
        with pytest.raises(ZipImportError) as ei:
            importer.update_from_dir(iid)
        assert "--from-git" in ei.value.message

    def test_update_with_subdir_repacks_same_root(
        self, git_env: GitEnv, importer: Importer, workspace: Workspace
    ) -> None:
        (git_env.seed / "site").mkdir(exist_ok=True)
        (git_env.seed / "site" / "index.html").write_text(
            "<html><body>sub v1</body></html>", encoding="utf-8"
        )
        _git(git_env.seed, "add", "-A")
        _git(git_env.seed, "commit", "-m", "subdir v1")
        _git(git_env.seed, "push", "origin", "trunk")

        result = importer.import_from_git(
            "https://github.com/acme/mysite",
            subdir="site",
            clone_url=str(git_env.remote),
        )
        iid = result.instance_id

        (git_env.seed / "site" / "index.html").write_text(
            "<html><body>sub v2</body></html>", encoding="utf-8"
        )
        _git(git_env.seed, "add", "-A")
        _git(git_env.seed, "commit", "-m", "subdir v2")
        _git(git_env.seed, "push", "origin", "trunk")

        updated = importer.update_from_git(iid, clone_url=str(git_env.remote))
        assert updated.skipped is False
        current = workspace.app_current(iid)
        assert "sub v2" in (current / "index.html").read_text(encoding="utf-8")
        manifest = InstanceManifest.load(workspace.app_manifest_path(iid))
        assert manifest.sourceGitSubdir == "site"  # 打包范围不漂移


# ---- 阶段 E（065.19–20）：CLI -------------------------------------------------


class TestCliImportGit:
    def _init_ws(self, workspace: Workspace) -> None:
        from local_webpage_access.init_workspace import init_workspace

        init_workspace(workspace.root)

    def test_three_source_mutual_exclusion(self, workspace: Workspace, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from local_webpage_access.cli import app

        self._init_ws(workspace)
        zip_path = tmp_path / "a.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("index.html", "<html></html>")

        runner = CliRunner()
        import os

        cwd = os.getcwd()
        os.chdir(workspace.root)
        try:
            # zip + --from-git → 拒绝
            r = runner.invoke(app, ["import", str(zip_path), "--from-git", "https://github.com/a/b"])
            assert r.exit_code == 2
            # --from-dir + --from-git → 拒绝
            r = runner.invoke(
                app, ["import", "--from-dir", "/tmp/x", "--from-git", "https://github.com/a/b"]
            )
            assert r.exit_code == 2
            # 什么都不给 → 拒绝
            r = runner.invoke(app, ["import"])
            assert r.exit_code == 2
            # --ref / --subdir 不配 --from-git → 拒绝
            r = runner.invoke(app, ["import", str(zip_path), "--ref", "main"])
            assert r.exit_code == 2
        finally:
            os.chdir(cwd)

    def test_import_and_update_e2e(
        self,
        git_env: GitEnv,
        workspace: Workspace,
        workspace_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from typer.testing import CliRunner

        from local_webpage_access.cli import app

        self._init_ws(workspace)
        _patch_git_remote(monkeypatch, git_env)

        runner = CliRunner()
        import os

        cwd = os.getcwd()
        os.chdir(workspace.root)
        try:
            r = runner.invoke(
                app, ["import", "--from-git", "https://github.com/acme/mysite"]
            )
            assert r.exit_code == 0, r.output
            assert "mysite" in r.output
            assert "GitHub" in r.output

            # 无变更更新 → 跳过
            r = runner.invoke(
                app,
                ["import", "--from-git", "https://github.com/acme/mysite", "--update", "mysite"],
            )
            assert r.exit_code == 0, r.output
            assert "无变更" in r.output or "跳过" in r.output

            # 有新提交 → 原地更新
            git_env.push_commit("index.html", "<html><body>v2</body></html>", "v2")
            r = runner.invoke(
                app,
                ["import", "--from-git", "https://github.com/acme/mysite", "--update", "mysite"],
            )
            assert r.exit_code == 0, r.output
            assert "已从 GitHub 源更新" in r.output

            # 换仓库 --update 同 id → 拒绝（CHK-239 low-1：exit 2 与 folder 预检同档）
            r = runner.invoke(
                app,
                ["import", "--from-git", "https://github.com/other/repo", "--update", "mysite"],
            )
            assert r.exit_code == 2
            assert "不一致" in r.output
        finally:
            os.chdir(cwd)

    def test_status_and_list_share_github_source_line(
        self,
        git_env: GitEnv,
        workspace: Workspace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """BUG-571：status 与 list 共用 GitHub 来源行，不得只改一边。"""
        from typer.testing import CliRunner

        from local_webpage_access.cli import app

        self._init_ws(workspace)
        _patch_git_remote(monkeypatch, git_env)
        runner = CliRunner()
        import os

        cwd = os.getcwd()
        os.chdir(workspace.root)
        try:
            r = runner.invoke(
                app, ["import", "--from-git", "https://github.com/acme/mysite"]
            )
            assert r.exit_code == 0, r.output
            status = runner.invoke(app, ["status", "mysite"])
            listing = runner.invoke(app, ["list"])
        finally:
            os.chdir(cwd)
        assert status.exit_code == 0, status.output
        assert listing.exit_code == 0, listing.output
        assert "来源：GitHub https://github.com/acme/mysite" in status.output
        assert "来源：GitHub https://github.com/acme/mysite" in listing.output
        status_src = [
            line.strip() for line in status.output.splitlines() if "来源：GitHub" in line
        ]
        list_src = [
            line.strip() for line in listing.output.splitlines() if "来源：GitHub" in line
        ]
        assert status_src
        assert status_src == list_src

    def test_git_source_label_formats_tag_and_subdir(self) -> None:
        """BUG-571：status/list 共用的 GitHub 来源文案（抽公共函数，避免两份拷贝漂移）。"""
        from types import SimpleNamespace

        from local_webpage_access.cli.status import git_source_label

        s = SimpleNamespace(
            source_git_url="https://github.com/acme/mysite",
            source_git_ref="v1.0",
            source_git_ref_kind="tag",
            source_git_commit="abcdef1234567890",
            source_git_subdir="site",
        )
        assert git_source_label(s) == (
            "GitHub https://github.com/acme/mysite tag v1.0@abcdef12 子目录 site"
        )

    def test_update_cli_prints_commit_oids_not_zip_hash(
        self,
        git_env: GitEnv,
        importer: Importer,
        workspace: Workspace,
        registry: Registry,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """成功更新须打印真实 git commit 短 SHA，不得把 zip sha256 标成「远端 OID」。

        skipped 路径的 zip_hash 碰巧等于 stored_commit；changed 路径的
        prev_hash/zip_hash 是 pack_source_dir 的内容指纹，拿去 GitHub 找不到。
        """
        from local_webpage_access.cli.importing import _do_update_from_git

        _patch_git_remote(monkeypatch, git_env)
        imported = importer.import_from_git(
            "https://github.com/acme/mysite",
            clone_url=str(git_env.remote),
        )
        iid = imported.instance_id
        old_oid = imported.manifest.sourceGitCommit
        assert old_oid and len(old_oid) >= 12
        new_oid = git_env.push_commit(
            "index.html", "<html><body>v2</body></html>", "v2"
        )

        kwargs = dict(
            instance_id=iid,
            url="https://github.com/acme/mysite",
            restart=False,
            keep_data=True,
            yes=True,
            force_kind_change=False,
        )

        _do_update_from_git(
            importer, workspace, Config(), registry, dry_run=True, **kwargs
        )
        dry_out = capsys.readouterr().out
        assert "内容指纹" in dry_out
        assert "远端 OID" not in dry_out

        _do_update_from_git(
            importer, workspace, Config(), registry, dry_run=False, **kwargs
        )
        out = capsys.readouterr().out
        assert f"远端 OID：{old_oid[:12]} -> {new_oid[:12]}" in out

    def test_from_dir_update_rejected_for_git_instance(
        self,
        git_env: GitEnv,
        workspace: Workspace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from typer.testing import CliRunner

        from local_webpage_access.cli import app

        self._init_ws(workspace)
        _patch_git_remote(monkeypatch, git_env)
        runner = CliRunner()
        import os

        cwd = os.getcwd()
        os.chdir(workspace.root)
        try:
            r = runner.invoke(app, ["import", "--from-git", "https://github.com/acme/mysite"])
            assert r.exit_code == 0, r.output
            r = runner.invoke(
                app,
                ["import", "--from-dir", "/tmp/whatever", "--update", "mysite"],
            )
            # CLI 层 BUG-440 预检：manifest 记录的是 git 源（无 sourceDirPath），
            # update_from_dir 拒绝（exit 1）或路径一致性预检拒绝（exit 2）均属正确拒绝
            assert r.exit_code in (1, 2)
        finally:
            os.chdir(cwd)


# ---- 阶段 F（065.21–22）：manager API ----------------------------------------


@pytest.fixture()
def manager_env(
    workspace_root: Path,
    workspace: Workspace,
    git_env: GitEnv,
    monkeypatch: pytest.MonkeyPatch,
):
    """管理页 API 测试环境（IMP-065；模块级供多个测试类复用）。"""
    from fastapi.testclient import TestClient

    from local_webpage_access.config import Config, example_config_text
    from local_webpage_access.manager_api import create_app, ensure_token
    from local_webpage_access.paths import Workspace as Ws
    from local_webpage_access.registry import Registry as Reg

    workspace_root.joinpath("registry").mkdir(parents=True, exist_ok=True)
    config_path = workspace_root / "local-web.yml"
    config_path.write_text(example_config_text(static_gateway="builtin"), encoding="utf-8")
    reg = Reg(workspace_root / "registry" / "local-web.db")
    reg.open()
    ws = Ws(workspace_root)
    token = ensure_token(ws)
    app = create_app(ws, config=Config(), registry=reg, token=token)
    _patch_git_remote(monkeypatch, git_env)
    with TestClient(app) as client:
        yield {
            "client": client,
            "token": token,
            "reg": reg,
            "ws": ws,
            "git_env": git_env,
        }
    reg.close()


class TestManagerApiGit:
    def test_import_from_git_endpoint(self, manager_env: dict) -> None:
        client = manager_env["client"]
        token = manager_env["token"]
        resp = client.post(
            "/api/import-from-git",
            json={"url": "https://github.com/acme/mysite"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["action"] == "import-from-git"
        assert data["instanceId"] == "mysite"
        assert "autoStart" in data
        inst = data["instance"]
        assert inst["sourceKind"] == "git"
        assert inst["sourceGitUrl"] == "https://github.com/acme/mysite"
        assert inst["sourceGitRef"] == "trunk"
        assert inst["sourceGitRefKind"] == "branch"  # CHK-239 low-3：refKind 透出
        assert inst["sourceGitCommit"]

    def test_import_from_git_requires_url(self, manager_env: dict) -> None:
        client = manager_env["client"]
        token = manager_env["token"]
        resp = client.post(
            "/api/import-from-git",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400
        body = resp.json()
        kind = (body.get("error", {}).get("detail") or {}).get("kind")
        assert kind == "invalid_url"
        assert "kind" not in (body.get("error") or {})

    def test_import_from_git_error_kind_in_detail(self, manager_env: dict) -> None:
        client = manager_env["client"]
        token = manager_env["token"]
        resp = client.post(
            "/api/import-from-git",
            json={"url": "https://gitlab.com/acme/mysite"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400
        body = resp.json()
        kind = (body.get("error", {}).get("detail") or {}).get("kind")
        assert kind == "host_not_allowed"

    def test_import_from_git_unauthorized_without_token(self, manager_env: dict) -> None:
        client = manager_env["client"]
        resp = client.post("/api/import-from-git", json={"url": "https://github.com/a/b"})
        assert resp.status_code == 401

    def test_update_from_git_endpoint_no_change(self, manager_env: dict) -> None:
        client = manager_env["client"]
        token = manager_env["token"]
        r1 = client.post(
            "/api/import-from-git",
            json={"url": "https://github.com/acme/mysite"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r1.status_code == 200
        iid = r1.json()["instanceId"]

        r2 = client.post(
            f"/api/instances/{iid}/update-from-git",
            json={"restart": True, "keepData": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r2.status_code == 200, r2.text
        data = r2.json()
        assert data["skipped"] is True
        assert data["action"] == "update-from-git"

    def test_update_from_git_endpoint_rebuild_on_change(self, manager_env: dict) -> None:
        client = manager_env["client"]
        token = manager_env["token"]
        git_env = manager_env["git_env"]
        r1 = client.post(
            "/api/import-from-git",
            json={"url": "https://github.com/acme/mysite"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r1.status_code == 200
        iid = r1.json()["instanceId"]

        git_env.push_commit("index.html", "<html><body>v2</body></html>", "v2")
        r2 = client.post(
            f"/api/instances/{iid}/update-from-git",
            json={"restart": False},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["skipped"] is False

    def test_update_from_git_rejects_zip_instance(self, manager_env: dict) -> None:
        client = manager_env["client"]
        token = manager_env["token"]
        ws = manager_env["ws"]
        reg = manager_env["reg"]

        zip_path = ws.inbox / "plain.zip"
        ws.inbox.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("index.html", "<html></html>")
        imp = Importer(ws, Config(), reg)
        result = imp.import_zip(zip_path)

        resp = client.post(
            f"/api/instances/{result.instance_id}/update-from-git",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400


# ---- 阶段 G（065.25）：doctor check_git ---------------------------------------


class TestDoctorCheckGit:
    def test_git_present_ok(self) -> None:
        from local_webpage_access.doctor import STATUS_OK, check_git

        result = check_git()
        if result.status == STATUS_OK:
            assert "git" in result.message.lower()
        else:
            # 本机无 git：WARN 合法，但不得 FAIL
            from local_webpage_access.doctor import STATUS_FAIL

            assert result.status != STATUS_FAIL

    def test_git_missing_warn_not_fail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from local_webpage_access import doctor
        from local_webpage_access.doctor import STATUS_WARN, check_git

        monkeypatch.setattr(doctor, "shutil", type("S", (), {"which": staticmethod(lambda _n: None)}))
        result = check_git()
        assert result.status == STATUS_WARN
        assert "GitHub" in (result.detail or "") or "GitHub" in result.message

    def test_run_doctor_includes_git_check(self, workspace: Workspace) -> None:
        from local_webpage_access.doctor import run_doctor

        report = run_doctor(workspace, Config())
        names = [c.name for c in report.checks]
        assert "git" in names

    def test_doctor_json_exposes_git(self, workspace: Workspace) -> None:
        report_data = {
            c.name: c.to_dict() for c in run_doctor(workspace, Config()).checks
        }
        assert "git" in report_data


def run_doctor(ws, config):  # pragma: no cover - 供上方用例复用的薄封装
    from local_webpage_access.doctor import run_doctor as _rd

    return _rd(ws, config)


# ---- 杂项：manifest 字段往返 ---------------------------------------------------


class TestManifestGitFields:
    def test_old_json_without_git_fields_loads(self, tmp_path: Path) -> None:
        """旧实例 JSON 无 git 字段可加载（默认 None，不做迁移）。"""
        payload = {
            "schemaVersion": 1,
            "id": "old",
            "name": "Old",
            "version": "v1",
            "kind": "static",
            "runtime": "shared-static",
            "servingMode": "shared-static",
            "status": "stopped",
            "desiredState": "stopped",
        }
        path = tmp_path / "local-web.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        manifest = InstanceManifest.load(path)
        assert manifest.sourceGitUrl is None
        assert manifest.sourceGitRefKind is None
        assert manifest.sourceGitCommit is None
        assert manifest.sourceGitSubdir is None

    def test_git_fields_roundtrip(self, tmp_path: Path) -> None:
        payload = {
            "schemaVersion": 1,
            "id": "site",
            "name": "Site",
            "version": "v1",
            "kind": "static",
            "runtime": "shared-static",
            "servingMode": "shared-static",
            "status": "stopped",
            "desiredState": "stopped",
            "sourceKind": "git",
            "sourceGitUrl": "https://github.com/acme/mysite",
            "sourceGitRef": "trunk",
            "sourceGitRefKind": "branch",
            "sourceGitCommit": "a" * 40,
            "sourceGitSubdir": "site",
        }
        path = tmp_path / "local-web.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        manifest = InstanceManifest.load(path)
        assert manifest.sourceKind == "git"
        assert manifest.sourceDirPath is None
        assert manifest.sourceGitCommit == "a" * 40
        manifest.save(path)
        reloaded = InstanceManifest.load(path)
        assert reloaded.sourceGitSubdir == "site"


# ---- CHK-238 回归：P1 无提示挂起 / 进程组超时杀 / P2 四项 ----------------------


class TestNoPromptAndProcessGroup:
    """BUG-553：私有仓 401 不得等终端输入堵满超时；BUG-558：超时杀整个进程组。"""

    def test_clone_env_disables_terminal_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GIT_TERMINAL_PROMPT", raising=False)
        env = git_source._clone_env()
        assert env["GIT_TERMINAL_PROMPT"] == "0"  # 401 快速失败，credential helper 不受影响
        assert env["GIT_LFS_SKIP_SMUDGE"] == "1"
        assert env["LC_ALL"] == "C"
        assert env["LANG"] == "C"
        assert "GIT_CONFIG_NOSYSTEM" not in env

    def test_run_git_no_prompt_closes_stdin_and_sets_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded: dict = {}

        class FakeProc:
            returncode = 0

            def communicate(self, timeout=None):  # noqa: ANN001, ANN202
                return "", ""

        def fake_popen(argv, **kwargs):  # noqa: ANN001, ANN003
            recorded.update(kwargs)
            return FakeProc()

        monkeypatch.setattr(git_source.subprocess, "Popen", fake_popen)
        res = git_source._run_git_no_prompt(["git", "x"], timeout=5)
        assert res.returncode == 0
        assert recorded["stdin"] == subprocess.DEVNULL
        assert recorded["env"]["GIT_TERMINAL_PROMPT"] == "0"
        if os.name == "posix":
            assert recorded["start_new_session"] is True

    @pytest.mark.skipif(os.name != "posix", reason="POSIX 进程组语义")
    def test_timeout_kills_grandchild_process_group(self, tmp_path: Path) -> None:
        """超时后孙进程（模拟 git-remote-https）须随进程组一起死亡，不留孤儿。"""
        pidfile = tmp_path / "child.pid"
        argv = [
            sys.executable,
            "-c",
            "import subprocess,time;"
            f"p=subprocess.Popen(['sleep','30']);"
            f"open({str(pidfile)!r},'w').write(str(p.pid));"
            "time.sleep(30)",
        ]
        with pytest.raises(subprocess.TimeoutExpired):
            git_source._run_git_no_prompt(argv, timeout=1)
        child = int(pidfile.read_text())

        def _dead(pid: int) -> bool:
            r = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
            )
            return r.returncode != 0 or r.stdout.strip().startswith("Z")

        deadline = time.time() + 10
        while time.time() < deadline and not _dead(child):
            time.sleep(0.2)
        assert _dead(child), f"孙进程 {child} 未随进程组被杀"


class TestUpdateGitDryRunAndEvents:
    """BUG-555：dry-run 不得写「git 源更新」事件（预演不进实例历史）。"""

    def _import(self, git_env: GitEnv, importer: Importer) -> str:
        result = importer.import_from_git(
            "https://github.com/acme/mysite", clone_url=str(git_env.remote)
        )
        return result.instance_id

    def test_dry_run_writes_no_update_event(self, git_env: GitEnv, importer: Importer) -> None:
        iid = self._import(git_env, importer)
        git_env.push_commit("index.html", "<html><body>v2</body></html>", "v2")
        events_before = len(importer.registry.list_events(iid))

        result = importer.update_from_git(iid, dry_run=True, clone_url=str(git_env.remote))
        assert result.dry_run is True
        assert len(importer.registry.list_events(iid)) == events_before

        # 对照：真实更新会记录 update 事件
        importer.update_from_git(iid, clone_url=str(git_env.remote))
        kinds = [e["event_type"] for e in importer.registry.list_events(iid)]
        assert "update" in kinds

    def test_real_update_syncs_manifest_commit_in_memory(
        self, git_env: GitEnv, importer: Importer
    ) -> None:
        """BUG-553 前置：更新后 result.manifest 携带新 commit，CLI 据此展示。"""
        iid = self._import(git_env, importer)
        new_oid = git_env.push_commit("index.html", "<html><body>v2</body></html>", "v2")
        result = importer.update_from_git(iid, clone_url=str(git_env.remote))
        assert result.manifest.sourceGitCommit == new_oid


class TestSourceGitSubdirValidator:
    """BUG-557：sourceGitSubdir 与 sourceSubdir 同规（拒绝绝对路径与 ``..``）。"""

    @pytest.mark.parametrize("bad", ["/etc", "../outside", "a/../../b", "C:\\x"])
    def test_manifest_load_rejects_traversal(self, tmp_path: Path, bad: str) -> None:
        from local_webpage_access.errors import SchemaError

        payload = {
            "schemaVersion": 1,
            "id": "site",
            "name": "Site",
            "version": "v1",
            "kind": "static",
            "runtime": "shared-static",
            "servingMode": "shared-static",
            "status": "stopped",
            "desiredState": "stopped",
            "sourceKind": "git",
            "sourceGitSubdir": bad,
        }
        path = tmp_path / "local-web.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(SchemaError):
            InstanceManifest.load(path)

    def test_pack_root_resolve_blocks_symlink_escape(
        self, git_env: GitEnv, importer: Importer, workspace: Workspace
    ) -> None:
        """staging 内 symlink 子目录指向仓库外：resolve 断言拦下。"""
        from local_webpage_access.importer import _resolve_git_pack_root

        with git_source.stage_git_clone(
            git_source.parse_github_url("https://github.com/acme/mysite"),
            clone_url=str(git_env.remote),
        ) as clone:
            outside = clone.directory.parent / "outside"
            outside.mkdir(exist_ok=True)
            link = clone.directory / "evil"
            link.symlink_to(outside, target_is_directory=True)
            with pytest.raises(ZipImportError):
                _resolve_git_pack_root(clone.directory, "evil")


class TestCliGuardsChk238:
    """BUG-556：--update 时 --ref/--subdir 必须显式拒绝，不得静默丢弃。"""

    def _init_ws(self, workspace: Workspace) -> None:
        from local_webpage_access.init_workspace import init_workspace

        init_workspace(workspace.root)

    def test_ref_or_subdir_with_update_rejected(
        self,
        git_env: GitEnv,
        workspace: Workspace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from typer.testing import CliRunner

        from local_webpage_access.cli import app

        self._init_ws(workspace)
        _patch_git_remote(monkeypatch, git_env)
        runner = CliRunner()
        cwd = os.getcwd()
        os.chdir(workspace.root)
        try:
            r = runner.invoke(app, ["import", "--from-git", "https://github.com/acme/mysite"])
            assert r.exit_code == 0, r.output

            r = runner.invoke(
                app,
                [
                    "import", "--from-git", "https://github.com/acme/mysite",
                    "--ref", "other", "--update", "mysite",
                ],
            )
            assert r.exit_code == 2
            assert "不能与 --from-git --update 同时使用" in r.output

            r = runner.invoke(
                app,
                [
                    "import", "--from-git", "https://github.com/acme/mysite",
                    "--subdir", "docs", "--update", "mysite",
                ],
            )
            assert r.exit_code == 2
        finally:
            os.chdir(cwd)

    def test_update_output_shows_commit_not_zip_hash(
        self,
        git_env: GitEnv,
        workspace: Workspace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """BUG-553：成功更新打印远端 commit（前后同单位），zip sha256 单独一行。"""
        from typer.testing import CliRunner

        from local_webpage_access.cli import app

        self._init_ws(workspace)
        _patch_git_remote(monkeypatch, git_env)
        runner = CliRunner()
        cwd = os.getcwd()
        os.chdir(workspace.root)
        try:
            r = runner.invoke(app, ["import", "--from-git", "https://github.com/acme/mysite"])
            assert r.exit_code == 0, r.output
            new_oid = git_env.push_commit("index.html", "<html><body>v2</body></html>", "v2")

            r = runner.invoke(
                app,
                ["import", "--from-git", "https://github.com/acme/mysite", "--update", "mysite"],
            )
            assert r.exit_code == 0, r.output
            assert "远端 OID：" in r.output
            assert new_oid[:12] in r.output
            assert "打包内容指纹：" in r.output
        finally:
            os.chdir(cwd)

    def test_dry_run_label_honest(
        self,
        git_env: GitEnv,
        workspace: Workspace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from typer.testing import CliRunner

        from local_webpage_access.cli import app

        self._init_ws(workspace)
        _patch_git_remote(monkeypatch, git_env)
        runner = CliRunner()
        cwd = os.getcwd()
        os.chdir(workspace.root)
        try:
            r = runner.invoke(app, ["import", "--from-git", "https://github.com/acme/mysite"])
            assert r.exit_code == 0, r.output
            git_env.push_commit("index.html", "<html><body>v2</body></html>", "v2")

            r = runner.invoke(
                app,
                [
                    "import", "--from-git", "https://github.com/acme/mysite",
                    "--update", "mysite", "--dry-run",
                ],
            )
            assert r.exit_code == 0, r.output
            assert "[dry-run] 实例 mysite：远端有新提交" in r.output
            assert "打包内容指纹：" in r.output
            assert "远端 OID" not in r.output
        finally:
            os.chdir(cwd)


# ---- CHK-239 回归：M1 / HIGH 测试缺口 / low 项 ------------------------------


class TestChk239Regressions:
    """二轮审查（CHK-239）修复项的回归锁。"""

    def test_import_result_manifest_in_memory_is_git(self, git_env: GitEnv, importer: Importer) -> None:
        """HIGH-1：ImportResult.manifest 必须是 git 身份的内存对象——
        删掉 importer 里 ``result.manifest = manifest`` 同步行，本用例转红
        （API 的 autoStart / CLI 展示拿的就是这个对象，不重新读盘）。"""
        result = importer.import_from_git(
            "https://github.com/acme/mysite", clone_url=str(git_env.remote)
        )
        # 全部断言打在返回对象上，不做磁盘 load
        assert result.manifest.sourceKind == "git"
        assert result.manifest.sourceGitUrl == "https://github.com/acme/mysite"
        assert result.manifest.sourceGitRef == "trunk"
        assert result.manifest.sourceGitRefKind == "branch"
        assert result.manifest.sourceGitCommit == git_env.head_oid()
        assert result.manifest.sourceDirPath is None

    def test_content_identical_commit_refreshes_oid_without_reclone(
        self,
        git_env: GitEnv,
        importer: Importer,
        workspace: Workspace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """HIGH-2（065.17 分支）：远端有新提交但打包内容不变 → update_zip 判
        skipped，仍须刷新 sourceGitCommit——否则每次更新都重复全量克隆。"""
        import contextlib

        real_stage = git_source.stage_git_clone
        clone_calls: list[int] = []

        @contextlib.contextmanager
        def counting_stage(target, *, ref=None, clone_url=None, **kwargs):  # noqa: ANN001, ANN003
            clone_calls.append(1)
            with real_stage(target, ref=ref, clone_url=clone_url, **kwargs) as clone:
                yield clone

        monkeypatch.setattr(git_source, "stage_git_clone", counting_stage)

        result = importer.import_from_git(
            "https://github.com/acme/mysite", clone_url=str(git_env.remote)
        )
        iid = result.instance_id
        clones_after_import = len(clone_calls)

        empty_oid = git_env.push_empty_commit("empty: OID 前进，内容不变")

        # 第一次更新：probe 发现新 OID → 克隆一次 → 打包内容相同 → update_zip
        # skipped，但 OID 必须已刷新
        updated = importer.update_from_git(iid, clone_url=str(git_env.remote))
        assert updated.skipped is True
        manifest = InstanceManifest.load(workspace.app_manifest_path(iid))
        assert manifest.sourceGitCommit == empty_oid
        assert len(clone_calls) == clones_after_import + 1  # 只克隆了一次

        # 第二次更新：OID 已对齐 → probe 短路，零克隆
        again = importer.update_from_git(iid, clone_url=str(git_env.remote))
        assert again.skipped is True
        assert len(clone_calls) == clones_after_import + 1  # 没有再克隆

    def test_tree_size_excludes_git_dir(self, tmp_path: Path) -> None:
        """CHK-239 low-5：体积口径与 pack_source_dir 一致，.git 对象库不计。"""
        root = tmp_path / "repo"
        (root / ".git" / "objects").mkdir(parents=True)
        (root / ".git" / "objects" / "pack.dat").write_bytes(b"x" * 10_000)
        (root / "index.html").write_text("<html></html>", encoding="utf-8")
        assert git_source._tree_size_bytes(root) == len("<html></html>")

    def test_api_malformed_url_is_400_not_500(self, manager_env: dict) -> None:
        """BUG-560 API 面：畸形 netloc 走结构化 400 + invalid_url，不是 500。"""
        client = manager_env["client"]
        token = manager_env["token"]
        resp = client.post(
            "/api/import-from-git",
            json={"url": "https://[github.com]/owner/repo"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        err = body.get("error", {})
        kind = (err.get("detail") or {}).get("kind") or err.get("kind")
        assert kind == "invalid_url"
