"""``lwa update`` 源码阶段：git 探测、安全快进与 handoff（IMP-063）。

把"人工 ``git pull``（可能要走代理）+ ``lwa update``"收敛为一条命令。分层：

* :func:`resolve_source_target` —— 仓库/目标解析（只信 upstream，不硬编码 origin）；
* :class:`UpdateLocks` —— repo（git common-dir）+ workspace 双锁，固定顺序、FD 可继承；
* :func:`inspect_repo` —— 本地状态（tracked 脏/detached/shallow）与候选关系九态；
* :class:`SourceCheckReport` —— ``lwa update --check`` 独立 JSON 契约 v1；
* :func:`fetch_candidate` —— 单次 fetch 固定候选 OID；
* :func:`apply_fast_forward` —— 门禁通过后 ``git merge --ff-only <OID>``。

安全边界（§15.1/15.2）：

* 只做 fast-forward；dirty/detached/ahead/diverged/shallow-unknown 一律拒绝；
* fetch 失败是 **warning** 不是故障（离线可用），不改工作树；
* 代理与凭据全部复用 git 自身机制，本模块零托管；
* git 测试统一用临时 bare remote + clone 夹具，不触外网。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from local_webpage_access.logging import get_logger

log = get_logger("update_source")

# ---- 实施参数（§15.7：集中常量，实机可调）------------------------------------

FETCH_TIMEOUT = 60
GIT_TIMEOUT = 15
BEHIND_HUMAN_LIMIT = 20
BEHIND_JSON_LIMIT = 100
DIRTY_FILES_LIMIT = 20

#: 人类 behind 列表 20 条 / JSON 100 条（§15.7.2），须保留 behindBy 总数
SCHEMA_VERSION = 1

_SHA_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")


# ---- 异常 --------------------------------------------------------------------


class SourceUpdateError(Exception):
    """源码阶段结构化错误。

    ``kind`` 稳定供机器消费：``not_a_git_repo`` / ``git_unavailable`` /
    ``target_incomplete`` / ``invalid_ref`` / ``dirty`` / ``detached`` /
    ``ahead`` / ``diverged`` / ``history_insufficient`` / ``skip_pip_conflict`` /
    ``ff_failed`` / ``git_error``。
    """

    def __init__(self, kind: str, message: str, *, action: str | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.action = action

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "message": self.message, "action": self.action}


class FetchError(Exception):
    """``git fetch`` 失败（远端不可达/超时/凭据）——降级 warning，不是故障。"""

    def __init__(self, message: str, *, stderr: str = ""):
        super().__init__(message)
        self.message = message
        self.stderr = stderr


class UpdateLockBusy(Exception):
    """repo/workspace 更新锁已被其他进程持有（fail-fast，显示持有者）。"""

    def __init__(self, scope: str, holder: dict[str, Any] | None):
        super().__init__(f"{scope} 更新锁被占用：{holder or '（未知持有者）'}")
        self.scope = scope
        self.holder = holder


# ---- git 基础 ----------------------------------------------------------------


def _git(repo: Path, *args: str, timeout: int = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def git_available() -> bool:
    return shutil.which("git") is not None


def is_git_repo(repo: Path) -> bool:
    """``.git`` 存在（目录或 worktree 指针文件）才算 git 克隆安装。"""
    return (repo / ".git").exists()


def _git_common_dir(repo: Path) -> Path:
    """git common-dir（worktree 场景锁主仓库，不锁 worktree 私有目录）。"""
    try:
        res = _git(repo, "rev-parse", "--git-common-dir")
        if res.returncode == 0:
            p = Path((res.stdout or "").strip())
            if not p.is_absolute():
                p = repo / p
            return p.resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return (repo / ".git").resolve()


# ---- 063.01 仓库与目标解析 -----------------------------------------------------


@dataclass(frozen=True)
class SourceTarget:
    """解析后的更新目标（纯逻辑产物，无网络）。"""

    repo: Path
    remote: str
    branch: str
    from_upstream: bool
    head: str

    @property
    def tracking_ref(self) -> str:
        return f"refs/remotes/{self.remote}/{self.branch}"

    @property
    def refspec(self) -> str:
        return f"+refs/heads/{self.branch}:{self.tracking_ref}"


def resolve_source_target(
    repo: Path, remote: str | None = None, ref: str | None = None
) -> SourceTarget:
    """解析更新目标（§15.1.2：缺省 ``@{upstream}``，显式 ``--remote/--ref`` 覆盖）。

    * 缺省用 ``@{upstream}`` 解析真实 remote + 分支；
    * 无 upstream 时**必须**同时给出 ``--remote`` 与 ``--ref``（target_incomplete）；
    * MVP 只接受远端**分支**：拒绝 SHA / ``refs/*`` 路径（invalid_ref）；
    * detached HEAD / 非 git 仓库 / git 不可用 → 结构化错误。
    """
    if not git_available():
        raise SourceUpdateError(
            "git_unavailable",
            "git 不可执行，无法做源码更新",
            action="安装 git 后重试，或用 --no-pull 仅用本地代码刷新 Runtime",
        )
    if not is_git_repo(repo):
        raise SourceUpdateError(
            "not_a_git_repo",
            f"{repo} 不是 git 克隆（无 .git）",
            action="迁移到 clone + pip install -e . 安装后使用一键更新；"
            "本次可用 --no-pull 仅用本地代码刷新 Runtime",
        )
    head = _rev_parse_head(repo)
    if _is_detached(repo):
        raise SourceUpdateError(
            "detached",
            "HEAD 处于 detached 状态，拒绝快进",
            action="git switch <branch> 回到分支后再更新",
        )

    upstream_remote, upstream_branch = _resolve_upstream(repo)
    has_upstream = upstream_remote is not None

    if not has_upstream and remote is None and ref is None:
        raise SourceUpdateError(
            "target_incomplete",
            "当前分支未配置 upstream，且未显式给出 --remote 与 --ref",
            action="同时指定 --remote <name> --ref <branch>，或 git branch "
            "--set-upstream-to=<remote>/<branch>",
        )
    if not has_upstream and (remote is None or ref is None):
        missing = "--remote" if remote is None else "--ref"
        raise SourceUpdateError(
            "target_incomplete",
            f"无 upstream 时必须同时给出 --remote 与 --ref（缺 {missing}）",
            action=f"补齐 {missing} 后重试",
        )

    final_remote = remote or upstream_remote or ""
    final_branch = ref or upstream_branch or ""

    if _SHA_RE.match(final_branch):
        raise SourceUpdateError(
            "invalid_ref",
            f"--ref 只接受远端分支名，不接受 commit SHA：{final_branch}",
            action="传分支名（如 main）",
        )
    if final_branch.startswith("refs/") or final_branch.startswith("-"):
        raise SourceUpdateError(
            "invalid_ref",
            f"--ref 只接受分支名：{final_branch}",
            action="传分支名（如 main）",
        )
    # 远端名不得为空、不得像 flag（前导 -）、不得含 /（会与 refs/remotes/a/b 混淆）。
    # 括号必要：and 优先于 or，旧写法 ``startswith("-") or "/" in x and not x``
    # 使斜杠检查对非空字符串恒死。
    if (not final_remote) or final_remote.startswith("-") or "/" in final_remote:
        raise SourceUpdateError(
            "invalid_ref",
            f"--remote 非法：{final_remote or '(空)'}",
            action="传远端名（如 origin），不要带斜杠或前导 -",
        )

    return SourceTarget(
        repo=repo,
        remote=final_remote,
        branch=final_branch,
        from_upstream=remote is None and ref is None and has_upstream,
        head=head,
    )


def _rev_parse_head(repo: Path) -> str:
    try:
        res = _git(repo, "rev-parse", "HEAD")
    except (OSError, subprocess.SubprocessError) as exc:
        raise SourceUpdateError("git_error", f"git rev-parse 失败：{exc}") from exc
    if res.returncode != 0 or not (res.stdout or "").strip():
        raise SourceUpdateError(
            "git_error",
            f"无法解析 HEAD：{(res.stderr or '').strip()}",
        )
    return (res.stdout or "").strip().splitlines()[0]


def _is_detached(repo: Path) -> bool:
    try:
        res = _git(repo, "symbolic-ref", "-q", "HEAD")
        return res.returncode != 0
    except (OSError, subprocess.SubprocessError):
        return False


def _resolve_upstream(repo: Path) -> tuple[str | None, str | None]:
    """解析 ``@{upstream}`` → (remote, branch)；无 upstream 返回 (None, None)。"""
    try:
        res = _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    except (OSError, subprocess.SubprocessError):
        return None, None
    if res.returncode != 0:
        return None, None
    value = (res.stdout or "").strip()
    if not value or value == "@{upstream}" or "/" not in value:
        return None, None
    remote, _, branch = value.partition("/")
    if not remote or not branch:
        return None, None
    return remote, branch


# ---- 063.02 repo/workspace 互斥锁 ---------------------------------------------


REPO_LOCK_FILENAME = "lwa-update.lock"
WORKSPACE_LOCK_FILENAME = "update.lock"


def _holder_payload(workspace: Path | None) -> bytes:
    return (
        json.dumps(
            {
                "pid": os.getpid(),
                "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "workspace": str(workspace) if workspace else None,
            },
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


def _read_holder(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8").strip() or "null")
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


class UpdateLocks:
    """repo + workspace 双锁（§15.1.9）。

    * 锁顺序固定 ``repo → workspace``；
    * **不释放**语义：FD 由持有进程保持到退出（或显式 :meth:`close`）；
    * 可经 ``pass_fds`` 继承给 continuation，父子任一存活锁即有效；
    * 忙时 :class:`UpdateLockBusy` fail-fast 并附持有者信息。
    """

    def __init__(self, repo_fd: int, repo_path: Path, ws_fd: int, ws_path: Path):
        self.repo_fd = repo_fd
        self.repo_path = repo_path
        self.ws_fd = ws_fd
        self.ws_path = ws_path

    @property
    def fds(self) -> tuple[int, ...]:
        return (self.repo_fd, self.ws_fd)

    def close(self) -> None:
        from local_webpage_access.file_lock import release_exclusive

        for fd in (self.repo_fd, self.ws_fd):
            if fd < 0:
                continue  # 仅持部分锁（如 --check 只有 repo 锁）
            release_exclusive(fd)
            try:
                os.close(fd)
            except OSError:
                pass
        self.repo_fd = self.ws_fd = -1


def acquire_repo_lock(repo: Path, workspace: Path | None = None) -> int:
    """取 git common-dir 下的 repo 锁，返回可继承 FD。"""
    from local_webpage_access.file_lock import ensure_lockable, try_acquire_exclusive

    if not is_git_repo(repo):
        # CHK-225 中危7：非 git 目录绝不取 repo 锁。旧实现的 _git_common_dir
        # 回退 ``repo/.git`` 并 mkdir + O_CREAT，会在用户目录里留下
        # .git/lwa-update.lock，此后 is_git_repo() 恒 True 误导后续判定。
        raise SourceUpdateError(
            "not_a_git_repo",
            f"{repo} 不是 git 克隆（无 .git），不取 repo 锁",
            action="确认 --repo 指向 git 克隆的源码根",
        )
    path = _git_common_dir(repo) / REPO_LOCK_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try_acquire_exclusive(fd)
    except BlockingIOError:
        holder = _read_holder(path)
        os.close(fd)
        raise UpdateLockBusy("repo", holder) from None
    ensure_lockable(fd)
    os.lseek(fd, 0, os.SEEK_SET)
    payload = _holder_payload(workspace)
    os.write(fd, payload)
    os.ftruncate(fd, len(payload))
    return fd


def acquire_workspace_lock(workspace: Path) -> int:
    """取工作区 run/ 下的 workspace 锁，返回可继承 FD。"""
    from local_webpage_access.file_lock import ensure_lockable, try_acquire_exclusive

    path = workspace / "run" / WORKSPACE_LOCK_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try_acquire_exclusive(fd)
    except BlockingIOError:
        holder = _read_holder(path)
        os.close(fd)
        raise UpdateLockBusy("workspace", holder) from None
    ensure_lockable(fd)
    os.lseek(fd, 0, os.SEEK_SET)
    payload = _holder_payload(workspace)
    os.write(fd, payload)
    os.ftruncate(fd, len(payload))
    return fd


def acquire_update_locks(repo: Path, workspace: Path) -> UpdateLocks:
    """完整 update 锁序：repo → workspace（§15.1.9 固定顺序）。"""
    repo_fd = acquire_repo_lock(repo, workspace)
    try:
        ws_fd = acquire_workspace_lock(workspace)
    except Exception:
        from local_webpage_access.file_lock import release_exclusive

        release_exclusive(repo_fd)
        os.close(repo_fd)
        raise
    return UpdateLocks(
        repo_fd,
        _git_common_dir(repo) / REPO_LOCK_FILENAME,
        ws_fd,
        workspace / "run" / WORKSPACE_LOCK_FILENAME,
    )


def acquire_workspace_only_lock(workspace: Path) -> UpdateLocks:
    """仅取 workspace 锁（§15.1.9：可变更的 update 全程须持 workspace 锁）。

    用于无源码阶段的路径（``--no-pull`` / 非 git 安装 / 未识别 repo /
    git 不可用）——它们不改 ``.git``，无需 repo 锁，但 Runtime 变更仍须
    与其它 update 互斥。repo FD 置 -1（:class:`UpdateLocks` 已兼容）。
    """
    ws_fd = acquire_workspace_lock(workspace)
    return UpdateLocks(
        -1,
        Path("/nonexistent") / REPO_LOCK_FILENAME,
        ws_fd,
        workspace / "run" / WORKSPACE_LOCK_FILENAME,
    )


# ---- 063.03 本地状态与关系判定 --------------------------------------------------


@dataclass
class RepoStatus:
    """本地仓库状态 + 与固定候选的关系（九态）。"""

    head: str
    detached: bool = False
    dirty_files: list[str] = field(default_factory=list)
    untracked_files: list[str] = field(default_factory=list)
    shallow: bool = False
    relation: str = "unknown"  # equal | behind | ahead | diverged | unknown
    ahead_by: int = 0
    behind_by: int = 0

    @property
    def dirty(self) -> bool:
        return bool(self.dirty_files)


def inspect_repo(repo: Path, candidate: str | None = None) -> RepoStatus:
    """收集 tracked/untracked/detached/shallow 与候选关系（§15.1.4）。

    * dirty 只看 **tracked 修改**（``git status --porcelain`` 排除 ``??``）；
    * shallow 且无法证明祖先关系 → ``unknown``（history_insufficient），
      不冒充 diverged；
    * ``candidate=None`` 时 relation 恒 ``unknown``。
    """
    status = RepoStatus(head=_rev_parse_head(repo))
    try:
        res = _git(repo, "status", "--porcelain")
        if res.returncode == 0:
            for line in (res.stdout or "").splitlines():
                if not line.strip():
                    continue
                if line.startswith("??"):
                    status.untracked_files.append(line[3:].strip())
                else:
                    status.dirty_files.append(line[3:].strip())
    except (OSError, subprocess.SubprocessError):
        pass
    status.detached = _is_detached(repo)
    status.shallow = _is_shallow(repo)

    if candidate is None or candidate == status.head:
        status.relation = "equal" if candidate == status.head else "unknown"
        return status

    if status.head == candidate:
        status.relation = "equal"
        return status
    head_anc = _is_ancestor(repo, status.head, candidate)  # head ⊆ candidate → behind
    cand_anc = _is_ancestor(repo, candidate, status.head)  # candidate ⊆ head → ahead
    if head_anc:
        status.relation = "behind"
    elif cand_anc:
        status.relation = "ahead"
    elif status.shallow:
        # shallow 历史不足无法证明祖先关系：unknown，不冒充 diverged（§15.1.4）
        status.relation = "unknown"
    else:
        status.relation = "diverged"
    status.ahead_by, status.behind_by = _count_left_right(repo, status.head, candidate)
    return status


def _is_shallow(repo: Path) -> bool:
    try:
        res = _git(repo, "rev-parse", "--is-shallow-repository")
        return (res.stdout or "").strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def _is_ancestor(repo: Path, maybe: str, ancestor_of: str) -> bool:
    try:
        res = _git(repo, "merge-base", "--is-ancestor", maybe, ancestor_of)
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _count_left_right(repo: Path, head: str, candidate: str) -> tuple[int, int]:
    try:
        res = _git(repo, "rev-list", "--left-right", "--count", f"{head}...{candidate}")
        parts = (res.stdout or "").split()
        if len(parts) == 2:
            return int(parts[0]), int(parts[1])
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return 0, 0


# ---- 063.04 commit 描述与 SourceCheckReport v1 ---------------------------------


@dataclass(frozen=True)
class CommitDescriptor:
    head: str
    subject: str | None = None
    version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"head": self.head, "version": self.version, "subject": self.subject}


def describe_commit(repo: Path, oid: str) -> CommitDescriptor:
    """从固定 OID 取 subject 并尝试解析版本（不受 remote ref 后续漂移影响）。"""
    try:
        res = _git(repo, "log", "-1", "--format=%s", oid)
        subject = (res.stdout or "").strip() if res.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        subject = None
    from local_webpage_access.version_info import version_from_subject

    return CommitDescriptor(head=oid, subject=subject, version=version_from_subject(subject))


@dataclass
class BehindCommit:
    head: str
    subject: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"head": self.head, "subject": self.subject}


@dataclass
class SourceCheckReport:
    """``lwa update --check --json`` 独立契约 v1（不要求已 init 工作区）。"""

    repo: str
    status: str  # upToDate | updateAvailable | blocked | unavailable
    current: CommitDescriptor | None = None
    target: dict[str, Any] | None = None  # {remote, branch, head, version, subject}
    relation: str = "unknown"
    ahead_by: int = 0
    behind_by: int = 0
    behind: list[BehindCommit] = field(default_factory=list)
    blockers: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    fresh: bool = True
    checked_at: str = ""
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "repo": self.repo,
            "status": self.status,
            "current": self.current.to_dict() if self.current else None,
            "target": self.target,
            "relation": self.relation,
            "aheadBy": self.ahead_by,
            "behindBy": self.behind_by,
            "behind": [c.to_dict() for c in self.behind],
            "blockers": self.blockers,
            "truncated": self.truncated,
            "fresh": self.fresh,
            "checkedAt": self.checked_at,
            "error": self.error,
        }

    def exit_code(self) -> int:
        """0=探测完成（含 updateAvailable/blocked）；1=本地仓库/参数不合法；2=远端不可达。"""
        if self.status == "unavailable":
            return 2
        if self.error is not None and self.status == "blocked":
            return 1
        return 0


def _append_blockers_from_status(blockers: list[dict[str, Any]], status: RepoStatus) -> None:
    if status.detached:
        blockers.append({"kind": "detached", "message": "HEAD 处于 detached 状态"})
    if status.dirty:
        files = status.dirty_files[:DIRTY_FILES_LIMIT]
        blockers.append(
            {
                "kind": "dirty",
                "message": f"tracked 文件有本地修改（{len(status.dirty_files)} 个）",
                "files": files,
                "action": "git commit / git stash 后重试",
            }
        )
    if status.relation == "ahead":
        blockers.append(
            {
                "kind": "ahead",
                "message": f"HEAD 领先远端 {status.ahead_by} 个提交",
                "action": "先 git push 或确认本地提交，再更新",
            }
        )
    elif status.relation == "diverged":
        blockers.append(
            {
                "kind": "diverged",
                "message": f"与远端分叉（ahead {status.ahead_by} / behind {status.behind_by}）",
                "action": "人工 merge/rebase 后重试；lwa 不代操作非 ff 变更",
            }
        )
    elif status.relation == "unknown":
        blockers.append(
            {
                "kind": "history_insufficient",
                "message": "浅克隆历史不足，无法证明快进关系",
                "action": "git fetch --unshallow 或 --deepen 后重试",
            }
        )


def _collect_behind(repo: Path, head: str, candidate: str) -> tuple[list[BehindCommit], bool]:
    """取 behind 列表（JSON 上限 100 条 + truncated 标记）。

    BUG-536：``behindBy`` 总数由调用方使用 :func:`inspect_repo` 的
    ``rev-list --count``（``RepoStatus.behind_by``）提供；本函数**不得**用
    截断后的列表长度冒充总数（``--max-count=101`` 之外仍有提交时会把
    behindBy 钉死在 101，§15.7.2 要求保留真实总数）。
    """
    try:
        res = _git(
            repo,
            "rev-list",
            "--format=%s",
            f"{head}..{candidate}",
            f"--max-count={BEHIND_JSON_LIMIT + 1}",
        )
    except (OSError, subprocess.SubprocessError):
        return [], False
    commits: list[BehindCommit] = []
    oid = ""
    for line in (res.stdout or "").splitlines():
        if line.startswith("commit "):
            oid = line.split()[1]
        elif oid and not line.startswith((" ", "Author:", "Date:")):
            commits.append(BehindCommit(head=oid, subject=line.strip()))
            oid = ""
    truncated = len(commits) > BEHIND_JSON_LIMIT
    if truncated:
        commits = commits[:BEHIND_JSON_LIMIT]
    return commits, truncated


# ---- 063.05 fetch 固定候选 + --check -------------------------------------------


def fetch_candidate(repo: Path, target: SourceTarget, *, timeout: int = FETCH_TIMEOUT) -> str:
    """单次 fetch 固定候选 OID（§15.1.3）。

    ``git fetch --no-tags <remote> +refs/heads/<branch>:refs/remotes/<remote>/<branch>``
    后立即从 remote-tracking ref 读取唯一 ``candidateHead``；后续展示/判定/快进
    均针对该 OID，不再二次 fetch。远端分支不存在 → ``invalid_ref``（结构化）。
    """
    cmd = [
        "git",
        "-C",
        str(repo),
        "fetch",
        "--no-tags",
        target.remote,
        target.refspec,
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise FetchError(f"git fetch 超时（{timeout}s）") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise FetchError(f"git fetch 无法执行：{exc}") from exc
    if res.returncode != 0:
        stderr = (res.stderr or "").strip()
        tail = stderr.splitlines()[-1] if stderr else str(res.returncode)
        # 显式 refspec 拉不存在的远端分支：目标错误（invalid_ref），非网络故障
        if "couldn't find remote ref" in stderr:
            raise SourceUpdateError(
                "invalid_ref",
                f"远端不存在分支 {target.remote}/{target.branch}（{tail}）",
                action="检查 --remote/--ref 是否正确",
            )
        raise FetchError(f"git fetch 失败：{tail}", stderr=stderr)
    try:
        probe = _git(repo, "rev-parse", target.tracking_ref)
    except (OSError, subprocess.SubprocessError) as exc:
        raise FetchError(f"读取候选 ref 失败：{exc}") from exc
    if probe.returncode != 0 or not (probe.stdout or "").strip():
        raise SourceUpdateError(
            "invalid_ref",
            f"远端不存在分支 {target.remote}/{target.branch}",
            action="检查 --remote/--ref 是否正确",
        )
    return (probe.stdout or "").strip().splitlines()[0]


def run_source_check(
    repo: Path,
    *,
    remote: str | None = None,
    ref: str | None = None,
    locks: UpdateLocks | None = None,
) -> SourceCheckReport:
    """``lwa update --check`` 主路径：fetch 后产出 SourceCheckReport（不改工作树）。

    会 fetch（刷新 ``.git`` 远端跟踪元数据）——只承诺不改工作树与 Runtime。
    调用方负责 repo 锁（会 fetch 的 check 也须与 update 互斥）。
    """
    import datetime as dt

    def _report(status: str, **kw: Any) -> SourceCheckReport:
        return SourceCheckReport(
            repo=str(repo),
            status=status,
            checked_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            **kw,
        )

    try:
        target = resolve_source_target(repo, remote, ref)
    except SourceUpdateError as exc:
        return _report("blocked", error=exc.to_dict())

    try:
        candidate = fetch_candidate(repo, target)
    except FetchError as exc:
        return _report(
            "unavailable",
            relation="unknown",
            fresh=True,
            current=describe_commit(repo, target.head),
            target={"remote": target.remote, "branch": target.branch},
            error={
                "kind": "fetch_failed",
                "message": exc.message,
                "action": "检查网络/代理（https_proxy）与凭据后重试；"
                "本地代码不受影响，可用 --no-pull 仅刷新 Runtime",
            },
        )

    status = inspect_repo(repo, candidate)
    behind, truncated = _collect_behind(repo, status.head, candidate)
    current_desc = describe_commit(repo, status.head)
    candidate_desc = describe_commit(repo, candidate)

    blockers: list[dict[str, Any]] = []
    _append_blockers_from_status(blockers, status)

    if blockers:
        # 已成功探测但不宜快进（dirty/detached/ahead/diverged/history_insufficient）
        check_status = "blocked"
    elif status.relation == "equal":
        check_status = "upToDate"
    else:
        check_status = "updateAvailable"

    return _report(
        check_status,
        current=current_desc,
        target={
            "remote": target.remote,
            "branch": target.branch,
            "head": candidate_desc.head,
            "version": candidate_desc.version,
            "subject": candidate_desc.subject,
        },
        relation=status.relation,
        ahead_by=status.ahead_by,
        # BUG-536：behindBy 用 rev-list 真实计数（status.behind_by），不受
        # behind 列表 100 条截断影响
        behind_by=status.behind_by,
        behind=behind,
        blockers=blockers,
        truncated=truncated,
    )


def cached_candidate(repo: Path, target: SourceTarget) -> str | None:
    """读已有 remote-tracking ref（不联网；dry-run 缺缓存时调用方标 skipped）。"""
    try:
        res = _git(repo, "rev-parse", "--verify", "-q", target.tracking_ref)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    value = (res.stdout or "").strip()
    return value or None


# ---- 063.06 固定 OID 快进 ------------------------------------------------------


@dataclass
class FastForwardResult:
    old_head: str
    new_head: str
    descriptor: CommitDescriptor


def apply_fast_forward(
    repo: Path,
    target: SourceTarget,
    candidate: str,
    status: RepoStatus,
    *,
    skip_pip: bool = False,
) -> FastForwardResult:
    """门禁通过后 ``git merge --ff-only <candidateHead>``（§15.1.3/15.1.6）。

    拒绝路径（工作树零改动）：

    * detached / tracked 脏 / ahead / diverged / shallow unknown → 结构化错误
      （仅当确需快进时才检查——equal 时无快进，前置条件不适用）；
    * behind + ``--skip-pip`` → ``skip_pip_conflict``（快进**前**拒绝）。
    """
    if status.relation == "equal":
        # 已是最新：无操作（调用方按 up-to-date 报告）；无快进则不检查脏
        return FastForwardResult(status.head, status.head, describe_commit(repo, status.head))
    if status.detached:
        raise SourceUpdateError(
            "detached", "HEAD 处于 detached 状态，拒绝快进", action="git switch <branch> 后重试"
        )
    if status.dirty:
        files = "\n".join(f"  {f}" for f in status.dirty_files[:DIRTY_FILES_LIMIT])
        raise SourceUpdateError(
            "dirty",
            f"tracked 文件有本地修改，拒绝快进（{len(status.dirty_files)} 个）：\n{files}",
            action="git commit 或 git stash 后重试 lwa update",
        )
    if status.relation == "ahead":
        raise SourceUpdateError(
            "ahead",
            f"HEAD 领先远端 {status.ahead_by} 个提交，拒绝快进",
            action="先 git push 或回退本地提交",
        )
    if status.relation == "diverged":
        raise SourceUpdateError(
            "diverged",
            f"与远端分叉（ahead {status.ahead_by} / behind {status.behind_by}），拒绝快进",
            action="人工处理分叉后重试；lwa 不做 merge/rebase/reset",
        )
    if status.relation == "unknown":
        raise SourceUpdateError(
            "history_insufficient",
            "历史不足无法证明快进关系（浅克隆？）",
            action="git fetch --unshallow 或 --deepen 后重试",
        )
    # behind + skip-pip：快进前拒绝（工作树不变）
    if skip_pip:
        raise SourceUpdateError(
            "skip_pip_conflict",
            "检测到落后且传入了 --skip-pip：快进后源码已变，跳过 pip 会造成新旧代码混跑",
            action="移除 --skip-pip，或显式 --no-pull --skip-pip 仅用本地代码",
        )
    try:
        res = _git(repo, "merge", "--ff-only", candidate, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SourceUpdateError("ff_failed", f"git merge --ff-only 无法执行：{exc}") from exc
    if res.returncode != 0:
        stderr = (res.stderr or "").strip()
        hint = ""
        if "untracked working tree file" in stderr or "would be overwritten" in stderr:
            hint = "（untracked 文件与远端新增文件同名冲突：请移动/删除该文件后重试）"
        raise SourceUpdateError(
            "ff_failed",
            f"git merge --ff-only 失败{hint}：{stderr.splitlines()[-1] if stderr else ''}",
            action="处理上述文件后重试 lwa update",
        )
    new_head = _rev_parse_head(repo)
    if new_head != candidate:
        raise SourceUpdateError(
            "ff_failed",
            f"快进结果与固定候选不一致（{new_head[:12]} != {candidate[:12]}）",
            action="git status 检查工作树后重试",
        )
    return FastForwardResult(status.head, new_head, describe_commit(repo, new_head))


# ---- 恢复指引（§15.1.8）-------------------------------------------------------


def recovery_hint(
    old_head: str | None,
    *,
    pip_missing: bool = False,
    python_executable: str | None = None,
    repo_path: str | Path | None = None,
) -> str:
    """升级关键步骤失败时的**人工**恢复链（不自动执行）。

    先要求复查 ``git status``；工作树仍干净时才给经 shell 安全转义的
    ``git reset --keep`` 建议，并明示重跑 pip/update 的完整链。

    ``pip_missing=True``（issue #27：venv 缺 pip 模块）时给针对性链——通用链
    的「重跑 lwa update（或 pip install -e .）」依赖被缺失的 pip 本身，会把
    用户引入死循环。快进后竞态还可能让新源码在 CLI 启动期就缺依赖：须先
    ``ensurepip``，再用同一解释器对明确仓库路径 ``pip install -e``，最后才
    重跑 ``lwa update``。代码回退降级为「通常无需」。
    """
    if not old_head:
        return ""
    quoted = shlex.quote(old_head)
    if pip_missing:
        quoted_python = shlex.quote(python_executable or sys.executable)
        quoted_repo = shlex.quote(str(repo_path) if repo_path else ".")
        return (
            "升级关键步骤失败（当前解释器缺少 pip 模块）。恢复链（人工执行）："
            f"① 恢复 pip：`{quoted_python} -m ensurepip --upgrade`；"
            f"② 用同一解释器安装已快进源码：`{quoted_python} -m pip install -e {quoted_repo}`"
            "（避免新源码启动期依赖缺失导致 `lwa` 无法启动）；"
            "③ 再跑 `lwa update` 完成收尾（代码已快进，重跑会按「已是最新」"
            "直接进入环境刷新，无需回退）；"
            f"④ 如确需回退代码：`git reset --keep {quoted}`（恢复 pip 后通常无需）。"
        )
    return (
        "升级关键步骤失败。恢复链（人工执行）：① 复查 `git status` 确认工作树干净；"
        f"② 如需回退代码：`git reset --keep {quoted}`；"
        "③ 重跑 `lwa update`（或 pip install -e .）恢复运行环境。"
        "doctor/accessReview 等业务诊断失败无需退代码。"
    )


__all__ = [
    "FETCH_TIMEOUT",
    "SCHEMA_VERSION",
    "BEHIND_HUMAN_LIMIT",
    "BEHIND_JSON_LIMIT",
    "SourceUpdateError",
    "FetchError",
    "UpdateLockBusy",
    "SourceTarget",
    "resolve_source_target",
    "UpdateLocks",
    "acquire_repo_lock",
    "acquire_workspace_lock",
    "acquire_update_locks",
    "acquire_workspace_only_lock",
    "RepoStatus",
    "inspect_repo",
    "CommitDescriptor",
    "describe_commit",
    "SourceCheckReport",
    "BehindCommit",
    "fetch_candidate",
    "run_source_check",
    "cached_candidate",
    "FastForwardResult",
    "apply_fast_forward",
    "recovery_hint",
    "git_available",
    "is_git_repo",
]
