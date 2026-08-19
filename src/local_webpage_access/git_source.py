"""GitHub 源导入辅助（IMP-065）。

职责：
1. URL 解析与规范化（§17.2.2）：``urlsplit`` 权威段取 hostname，精确匹配
   allowlist；拒绝 userinfo / query / fragment / 网页段路径 / 非 https。
2. 一次性浅克隆（§17.3 065.a/m）到**工作区外**系统临时目录：空 template +
   ``core.hooksPath=/dev/null`` + ``GIT_LFS_SKIP_SMUDGE=1``；成功后解析
   真实 ref（branch/tag，禁止存 ``HEAD``）与完整 commit OID。
3. ``git ls-remote`` 无变更探测（不落盘），按已存储 ref+kind 组
   ``refs/heads/…`` 或 ``refs/tags/…``。

红线（§17.1.3 / 17.7）：
- staging 是一次性可弃副本，禁止写成 ``sourceDirPath``；
- 不设 ``GIT_CONFIG_NOSYSTEM`` / 不清空 ``GIT_CONFIG_GLOBAL``——保留宿主机
  credential helper 与 ``http.proxy``（凭据与代理零托管）；
- 不递归 submodule、不解析 LFS 对象；
- git 测试统一用临时 bare remote 夹具（``clone_url`` 注入已解析 target），
  禁止外网与真实 GitHub。
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
import tempfile
import urllib.parse
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_webpage_access.errors import GitSourceError
from local_webpage_access.logging import get_logger

log = get_logger("git_source")

# ---- 实施参数（§17.3 集中常量，实机可调）------------------------------------

#: host allowlist（065.c）：精确相等匹配，挡 ``github.com.evil.tld`` /
#: ``www.github.com`` / ``gist.github.com``。MVP 仅 github.com；常量预留扩展。
ALLOWED_GIT_HOSTS: tuple[str, ...] = ("github.com",)

#: clone 超时（秒）→ ``clone_timeout``
CLONE_TIMEOUT = 180

#: clone 后源码树体积上限（字节）→ ``size_exceeded``。git 无 ``--max-size``，
#: 只能在克隆完成后 ``os.walk`` 合计，超限删除 staging（065.08）。
CLONE_SIZE_LIMIT = 2 * 1024 * 1024 * 1024  # 2 GiB

#: ls-remote 探测超时（秒）
LS_REMOTE_TIMEOUT = 30

#: errorKind 闭集（065.p）：未知失败不得降成 ``invalid_url``
ERROR_KINDS = frozenset(
    {
        "invalid_url",
        "host_not_allowed",
        "userinfo_forbidden",
        "git_missing",
        "remote_unreachable",
        "ref_not_found",
        "clone_timeout",
        "size_exceeded",
        "source_mismatch",
    }
)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")

#: GitHub 网页段（§17.2.2）：命中时提示改用仓库根地址 + ``--ref`` / ``--subdir``
_WEB_PATH_SEGMENTS = (
    "/tree/",
    "/blob/",
    "/releases",
    "/issues",
    "/pull/",
    "/archive",
    "/commit/",
    "/wiki/",
    "/forks/",
    "/settings/",
)

_REPO_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


def _git_error(kind: str, message: str, **context: Any) -> GitSourceError:
    """构造带闭集 errorKind 的 :class:`GitSourceError`。"""
    if kind not in ERROR_KINDS:  # pragma: no cover - 防御：新 kind 必须先进闭集
        raise AssertionError(f"未知 errorKind：{kind!r}（不在 ERROR_KINDS 闭集）")
    return GitSourceError(message, kind=kind, **context)


# ---- 065.01–03：URL 解析（零网络纯函数）--------------------------------------


@dataclass(frozen=True)
class ParsedGitTarget:
    """规范化后的 GitHub 仓库目标。

    ``url`` 恒为 ``https://github.com/<owner>/<repo>``（小写 host、剥离
    ``.git`` 与尾斜杠；owner/repo 保持 GitHub 大小写）。
    """

    url: str
    owner: str
    repo: str


def parse_github_url(
    raw: str,
    *,
    allowed_hosts: Sequence[str] = ALLOWED_GIT_HOSTS,
) -> ParsedGitTarget:
    """解析并规范化 GitHub 仓库地址（§17.2.2，零网络）。

    用 :func:`urllib.parse.urlsplit` 取权威段——**禁止**对原始字符串做
    ``startswith`` / ``endswith`` 前缀判断（webpack userinfo 绕过 allowlist、
    Windmill ``#@github.com`` 把校验 host 与 git 拨号 host 拆开）。

    Raises:
        GitSourceError: ``context["kind"]`` 为闭集 errorKind。
    """
    text = (raw or "").strip()
    if not text:
        raise _git_error("invalid_url", "仓库地址为空")

    # BUG-560（CHK-239 M1）：Python 3.11+ 的 urlsplit 对畸形 netloc（如
    # ``https://[github.com]/o/r``、``https://github.com]/o/r``）直接抛裸
    # ValueError——CLI 侧变 traceback、API 侧变 500，击穿「错误体必带闭集
    # errorKind」契约。在此拦截并转 invalid_url。
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError as exc:
        raise _git_error(
            "invalid_url",
            f"仓库地址格式非法：{text}（{exc}）",
        ) from exc
    if parts.scheme != "https":
        raise _git_error(
            "invalid_url",
            f"仅支持 https:// 的 GitHub 仓库地址（不支持 SSH / git@ / git:// / file://）：{text}",
        )
    if parts.username or parts.password:
        raise _git_error(
            "userinfo_forbidden",
            "仓库地址不能携带用户名/密码；私有仓凭据请配置在 LWA 宿主机的 "
            "git credential helper（不是浏览器所在机器）",
        )
    if parts.query or parts.fragment:
        raise _git_error(
            "invalid_url",
            "仓库地址不能包含 query（?）或 fragment（#）；请只传仓库根地址",
        )

    host = (parts.hostname or "").lower()
    if host not in allowed_hosts:
        raise _git_error(
            "host_not_allowed",
            f"仅支持 GitHub 仓库（github.com）：{host or '（空 host）'} 不在允许列表",
        )
    try:
        port = parts.port
    except ValueError as exc:
        raise _git_error("invalid_url", f"仓库地址端口非法：{text}") from exc
    if port is not None and port != 443:
        raise _git_error(
            "host_not_allowed",
            f"仅支持 443 端口（显式传了 :{port}）；请去掉端口号",
        )

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    # GitHub 禁止仓库名以 ``.git`` 结尾（创建时会自动剥离该后缀），因此这里
    # 只剥一次 clone URL 惯例后缀，不会把「真名以 .git 结尾的仓」静默指到错仓。
    if path.endswith(".git"):
        path = path[: -len(".git")]

    segments = [s for s in path.split("/") if s]
    if len(segments) != 2 or any(not _REPO_SEGMENT_RE.match(s) for s in segments):
        lowered = path.lower()
        for seg in _WEB_PATH_SEGMENTS:
            if seg in lowered:
                raise _git_error(
                    "invalid_url",
                    f"这是 GitHub 网页地址而不是仓库根地址：{text}。"
                    "请改用 https://github.com/<owner>/<repo>，分支/标签用 --ref、"
                    "子目录用 --subdir 指定",
                )
        raise _git_error(
            "invalid_url",
            f"仓库地址路径须为 /<owner>/<repo>（可带 .git 后缀）：{text}",
        )
    owner, repo = segments
    if owner in {".", ".."} or repo in {".", ".."}:
        raise _git_error("invalid_url", f"仓库 owner/repo 非法：{text}")

    return ParsedGitTarget(url=f"https://{host}/{owner}/{repo}", owner=owner, repo=repo)


# ---- 065.04：git 可执行探测 ---------------------------------------------------


def git_binary_available() -> bool:
    """git 可执行探测（与 ``update_source.git_available`` 同口径：``shutil.which``）。"""
    return shutil.which("git") is not None


def require_git_binary() -> None:
    """导入/更新入口缺 git 时 fail-fast（065.e）。"""
    if not git_binary_available():
        raise _git_error(
            "git_missing",
            "未找到 git 可执行文件：GitHub 源导入/更新需要宿主机安装 git"
            "（zip 与本机文件夹导入不受影响）",
        )


# ---- 065.06–09：一次性浅克隆护栏 ----------------------------------------------


@dataclass(frozen=True)
class CloneResult:
    """克隆结果：staging 内仓库工作树 + 解析出的身份。

    ``directory`` 指向 staging 中的仓库工作树（含 ``.git``，打包时由
    :func:`folder_source.pack_source_dir` 跳过）；随
    :func:`stage_git_clone` 上下文退出而删除。
    """

    commit: str  # 完整 OID（40 hex；sha256 仓库为 64 hex）
    ref: str  # 真实分支名或 tag 名（禁止 "HEAD"）
    ref_kind: str  # "branch" | "tag"
    directory: Path


def _clone_env() -> dict[str, str]:
    """git 子进程环境：LFS 跳过、禁终端提示、强制 C locale。

    红线：不设 ``GIT_CONFIG_NOSYSTEM``、不清空 ``GIT_CONFIG_GLOBAL``——
    保留宿主机 credential helper 与 ``http.proxy`` 配置（零托管）。
    ``GIT_TERMINAL_PROMPT=0``（BUG-554）：manager/daemon 无 TTY，HTTPS 401 时
    git 默认在终端等用户名直到超时（并占住 import_activity 锁）；禁终端提示
    让其快速失败。credential helper 是非交互的，不受此开关影响。
    ``LC_ALL=C`` / ``LANG=C``：``_classify_clone_failure`` 按英文 stderr 分类，
    非英文 locale 会把 ``repository not found`` 等误降成兜底 kind。
    """
    env = dict(os.environ)
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return env


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    """杀掉 git 子进程及其子孙（git-remote-https 等，BUG-558 / 065.08）。

    POSIX 下 Popen 以 ``start_new_session`` 建进程组（pgid == pid），
    ``killpg(SIGKILL)`` 整组连幼儿进程一起杀；Windows 退回 ``proc.kill()``。
    """
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:  # pragma: no cover - 正式平台为 macOS/Linux；Windows 兜底
            proc.kill()


def _run_git_no_prompt(
    argv: list[str], *, timeout: int
) -> subprocess.CompletedProcess[str]:
    """运行 git 子进程：stdin 关闭、禁终端提示、超时杀整个进程组。

    Raises:
        subprocess.TimeoutExpired: 超时（进程组已清杀后 re-raise，调用方
            转 ``clone_timeout`` / ``remote_unreachable``）。
    """
    popen_kwargs: dict[str, Any] = {}
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_clone_env(),
        **popen_kwargs,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        with contextlib.suppress(Exception):  # noqa: BLE001 - 尽力回收管道与尸体
            proc.communicate(timeout=5)
        raise
    returncode = proc.returncode if proc.returncode is not None else -1
    return subprocess.CompletedProcess(argv, returncode, stdout=out, stderr=err)


def _build_clone_argv(url: str, dest: Path, ref: str | None, template_dir: Path) -> list[str]:
    argv = [
        "git",
        "clone",
        "--depth",
        "1",
        "--single-branch",
        "--no-tags",
        f"--template={template_dir}",
        "-c",
        "core.hooksPath=/dev/null",
    ]
    if ref:
        argv += ["--branch", ref]
    argv += [url, str(dest)]
    return argv


def _classify_clone_failure(stderr: str, *, ref: str | None) -> GitSourceError:
    """把 clone 失败的 stderr 尾行映射到闭集 errorKind（065.p）。"""
    tail = (stderr or "").strip().splitlines()
    last = tail[-1].strip() if tail else ""
    lowered = last.lower()
    if ref and ("not found in upstream" in lowered or f"remote branch {ref.lower()}" in lowered):
        return _git_error(
            "ref_not_found",
            f"远端不存在分支/标签 {ref!r}；请核对 --ref",
            ref=ref,
        )
    if "could not resolve host" in lowered or "connection" in lowered or "timed out" in lowered:
        return _git_error("remote_unreachable", f"无法连接远端仓库：{last or '网络不可达'}")
    if (
        "repository not found" in lowered
        or "does not appear to be a git repository" in lowered
        or "could not read from remote repository" in lowered
        or "access denied" in lowered
        or "authentication failed" in lowered
        or "not found" in lowered
    ):
        return _git_error(
            "remote_unreachable",
            f"远端仓库不可达或不可读（私有仓需在 LWA 宿主机配置凭据）：{last}",
        )
    return _git_error("remote_unreachable", f"git clone 失败：{last or '未知错误'}")


def _tree_size_bytes(root: Path) -> int:
    """staging 体积合计（不跟随符号链接，跳过 symlink 本身）。

    口径与 :func:`folder_source.pack_source_dir` 一致：``.git`` 对象库**不计**
    （CHK-239 low-5）——它不进 zip 也不进实例树，计入只会让大历史浅克隆被
    误拒（size 上限保护的是入库内容）。
    """
    total = 0
    for dirpath, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d != ".git"]
        for fname in files:
            fpath = Path(dirpath) / fname
            try:
                if fpath.is_symlink() or not fpath.is_file():
                    continue
                total += fpath.stat().st_size
            except OSError:  # pragma: no cover - 竞态删除等
                continue
    return total


def _resolve_ref_and_commit(dest: Path, *, requested_ref: str | None) -> tuple[str, str, str]:
    """解析 HEAD 的真实 ref、ref 类型与完整 OID（065.k）。

    返回 ``(ref, ref_kind, commit)``：

    * ``symbolic-ref HEAD`` 成功 → 附着分支 → ``branch``；
    * 失败（detached）→ 用户显式给了 tag（``--branch <tag>`` 浅克隆 checkout
      到 commit）→ ``tag``；
    * 未显式给 ref 却 detached（GitHub 默认分支恒为 branch，实际不可达）→
      ``ref_not_found`` 兜底，不得存 ``HEAD``。
    """
    try:
        proc = _run_git_no_prompt(
            ["git", "-C", str(dest), "symbolic-ref", "-q", "HEAD"],
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise _git_error("remote_unreachable", f"解析克隆结果超时：{dest}") from exc
    if proc.returncode == 0 and proc.stdout.strip():
        full_ref = proc.stdout.strip()
        m = re.match(r"^refs/heads/(.+)$", full_ref)
        if m:
            ref, kind = m.group(1), "branch"
        else:  # pragma: no cover - refs/heads 之外的附着形态
            ref, kind = full_ref, "branch"
    elif requested_ref:
        ref, kind = requested_ref, "tag"
    else:
        raise _git_error(
            "ref_not_found",
            "克隆结果处于游离 HEAD 且未显式指定 --ref，无法解析默认分支",
        )

    try:
        proc = _run_git_no_prompt(
            ["git", "-C", str(dest), "rev-parse", "HEAD"],
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise _git_error("remote_unreachable", f"解析克隆结果超时：{dest}") from exc
    commit = proc.stdout.strip().lower()
    if proc.returncode != 0 or not _SHA_RE.match(commit):
        raise _git_error("remote_unreachable", f"无法解析克隆后的 commit OID：{dest}")
    return ref, kind, commit


@contextlib.contextmanager
def stage_git_clone(
    target: ParsedGitTarget,
    *,
    ref: str | None = None,
    clone_url: str | None = None,
    timeout: int = CLONE_TIMEOUT,
    size_limit_bytes: int = CLONE_SIZE_LIMIT,
) -> Iterator[CloneResult]:
    """一次性浅克隆到工作区外系统临时目录；无论成败，退出时删除 staging（065.07）。

    Args:
        target: 已通过 :func:`parse_github_url` 的规范化目标。
        ref: 可选分支/标签名；None 时跟远端 HEAD（默认分支）。
        clone_url: 实际克隆地址，默认 ``target.url``。仅供零外网测试注入
            本地 bare remote（065.05：clone 层接收已解析 target，两层分开，
            不构成对阶段 A 纯函数的绕过）。生产调用方不得传。
        timeout: clone 超时秒数。
        size_limit_bytes: 克隆后源码树体积上限。

    Raises:
        GitSourceError: ``git_missing`` / ``clone_timeout`` / ``size_exceeded`` /
            ``remote_unreachable`` / ``ref_not_found``。
    """
    require_git_binary()

    staging_root: Path | None = None
    template_dir: Path | None = None
    try:
        staging_root = Path(tempfile.mkdtemp(prefix="lwa-git-clone-"))
        template_dir = Path(tempfile.mkdtemp(prefix="lwa-git-template-"))
        dest = staging_root / (target.repo or "repo")
        argv = _build_clone_argv(clone_url or target.url, dest, ref, template_dir)
        log.info("浅克隆 %s（ref=%s）→ %s", target.url, ref or "远端 HEAD", dest)
        try:
            proc = _run_git_no_prompt(argv, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise _git_error(
                "clone_timeout",
                f"git clone 超时（>{timeout}s）：{target.url}",
            ) from exc
        if proc.returncode != 0:
            raise _classify_clone_failure(proc.stderr, ref=ref)

        size = _tree_size_bytes(dest)
        if size > size_limit_bytes:
            raise _git_error(
                "size_exceeded",
                f"仓库源码体积 {size / 1024 / 1024:.1f} MiB 超过上限 "
                f"{size_limit_bytes / 1024 / 1024:.1f} MiB，已取消导入",
            )

        resolved_ref, ref_kind, commit = _resolve_ref_and_commit(dest, requested_ref=ref)
        log.info(
            "克隆完成：%s@%s（%s，OID %s，浅克隆 depth=1）",
            target.url,
            resolved_ref,
            ref_kind,
            commit[:8],
        )
        yield CloneResult(
            commit=commit,
            ref=resolved_ref,
            ref_kind=ref_kind,
            directory=dest,
        )
    finally:
        if staging_root is not None:
            shutil.rmtree(staging_root, ignore_errors=True)
        if template_dir is not None:
            shutil.rmtree(template_dir, ignore_errors=True)


# ---- 065.15：ls-remote 无变更探测 ---------------------------------------------


def probe_remote_commit(
    url: str,
    *,
    ref: str,
    ref_kind: str,
    timeout: int = LS_REMOTE_TIMEOUT,
) -> str:
    """对已存储 ref+kind 做 ``git ls-remote`` 探测，返回远端 OID（不落盘）。

    tag 用 ``refs/tags/…``（annotated tag 取 ``^{}`` 剥离后的 commit OID，
    与 ``clone --branch <tag>`` checkout 的 commit 同口径）；branch 用
    ``refs/heads/…``。

    Raises:
        GitSourceError: ``remote_unreachable``（连不上/不可读）或
            ``ref_not_found``（远端已无该 ref）。
    """
    require_git_binary()
    pattern = f"refs/tags/{ref}" if ref_kind == "tag" else f"refs/heads/{ref}"
    # annotated tag 的 ref 指向 tag 对象而非 commit；ls-remote 精确模式不会
    # 自动附 peeled 行，必须显式加 `^{}` 查询（与 clone --branch <tag>
    # checkout 的 commit 同口径）
    patterns = [pattern] + ([f"{pattern}^{{}}"] if ref_kind == "tag" else [])
    try:
        proc = _run_git_no_prompt(["git", "ls-remote", url, *patterns], timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise _git_error(
            "remote_unreachable",
            f"git ls-remote 超时（>{timeout}s）：{url}",
        ) from exc
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        last = tail[-1].strip() if tail else ""
        raise _git_error("remote_unreachable", f"git ls-remote 失败：{last or url}")

    direct: str | None = None
    peeled: str | None = None
    for line in (proc.stdout or "").splitlines():
        sha, _, name = line.partition("\t")
        sha = sha.strip().lower()
        if name.strip() == pattern:
            direct = sha
        elif name.strip() == f"{pattern}^{{}}":
            peeled = sha
    oid = peeled or direct
    if not oid or not _SHA_RE.match(oid):
        raise _git_error(
            "ref_not_found",
            f"远端不存在 {pattern}（可能分支/标签已改名或删除）；"
            "如需更换请删除实例后重新导入",
            ref=ref,
            ref_kind=ref_kind,
        )
    return oid


__all__ = [
    "ALLOWED_GIT_HOSTS",
    "CLONE_TIMEOUT",
    "CLONE_SIZE_LIMIT",
    "LS_REMOTE_TIMEOUT",
    "ERROR_KINDS",
    "ParsedGitTarget",
    "CloneResult",
    "parse_github_url",
    "git_binary_available",
    "require_git_binary",
    "stage_git_clone",
    "probe_remote_commit",
]
