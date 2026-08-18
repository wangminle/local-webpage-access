"""``lwa update`` 两阶段编排（IMP-063 bootstrap + continuation 接力）。

时序（§15.3）::

    旧进程（bootstrap，不加载 Config/Registry）
      repo/目标解析 → repo/workspace 锁 → fetch 固定 OID → 状态门禁
      → merge --ff-only <OID> → pip install -e . → 启动新解释器
                                                    ↓ stdin handoff v1 + pass_fds 锁继承
    新进程（continuation，重新 import 全部代码）
      重读 Config/Registry → skills/templates → migrate → 重启 → access → doctor
      → 回传子报告 → 旧进程合并最终输出与退出码

正确性门槛：旧进程一旦更改了自身源码（HEAD 变化），Runtime 后半段必须交给
新解释器（BUG-357 同类边界）；HEAD 未变化时旧进程内联执行后半段。
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from local_webpage_access import update_source as us
from local_webpage_access.logging import get_logger
from local_webpage_access.paths import Workspace
from local_webpage_access.updater import (
    StepResult,
    UpdateOptions,
    UpdateReport,
    _run_runtime_phase,
    locate_repo,
    run_pip_install,
)
from local_webpage_access.version_info import resolve_version

log = get_logger("update_flow")

#: continuation 模块（非公开 CLI；``python -m local_webpage_access.update_continuation``）
CONTINUATION_MODULE = "local_webpage_access.update_continuation"

#: handoff 协议版本
HANDOFF_SCHEMA_VERSION = 1

#: continuation 总超时（doctor/review 较慢，给足窗口；可实施时按实机调）
CONTINUATION_TIMEOUT = 1800


def _source_extra(
    *,
    old_head: str | None,
    candidate: str | None,
    new_head: str | None,
    target: us.SourceTarget | None,
    status: us.RepoStatus | None,
    fresh: bool,
) -> dict[str, Any]:
    """``sourceUpdate.extra`` 契约（§15.3）。"""
    return {
        "oldHead": old_head,
        "candidateHead": candidate,
        "newHead": new_head,
        "remote": target.remote if target else None,
        "branch": target.branch if target else None,
        "relation": status.relation if status else "unknown",
        "aheadBy": status.ahead_by if status else 0,
        "behindBy": status.behind_by if status else 0,
        "fresh": fresh,
    }


def _version_note(desc: us.CommitDescriptor | None) -> str:
    if desc is None:
        return ""
    if desc.version:
        return f"V{desc.version}"
    return desc.head[:12]


def _dry_run_source_step(repo: Path | None, options: UpdateOptions) -> StepResult:
    """dry-run 源码预览：零写入（不联网、不 fetch、不取锁），标 fresh=false。"""
    if repo is None:
        return StepResult(
            "sourceUpdate",
            "skipped",
            "[dry-run] 未识别到 lwa 源码根，跳过源码更新预览",
        )
    if not us.is_git_repo(repo):
        return StepResult(
            "sourceUpdate",
            "skipped",
            "[dry-run] 非 git 克隆安装，源码更新不适用（迁移到 clone 安装后可用）",
        )
    try:
        target = us.resolve_source_target(repo, options.remote, options.ref)
    except us.SourceUpdateError as exc:
        return StepResult(
            "sourceUpdate",
            "skipped",
            f"[dry-run] {exc.message}",
            extra={"errorKind": exc.kind},
        )
    candidate = us.cached_candidate(repo, target)
    if candidate is None:
        return StepResult(
            "sourceUpdate",
            "skipped",
            "[dry-run] 缓存中无远端跟踪 ref，无法在零写入模式确定远端版本",
            extra=_source_extra(
                old_head=target.head,
                candidate=None,
                new_head=None,
                target=target,
                status=None,
                fresh=False,
            )
            | {"reason": "tracking_ref_missing"},
        )
    status = us.inspect_repo(repo, candidate)
    return StepResult(
        "sourceUpdate",
        "skipped",
        f"[dry-run] 基于缓存 ref 预览：relation={status.relation}"
        f"（behind {status.behind_by}）；零写入模式不 fetch，数据可能陈旧（fresh=false）",
        extra=_source_extra(
            old_head=status.head,
            candidate=candidate,
            new_head=None,
            target=target,
            status=status,
            fresh=False,
        ),
    )


def _inline_tail(
    workspace_root: Path,
    options: UpdateOptions,
    prepend: list[StepResult],
    report: UpdateReport,
) -> UpdateReport:
    """HEAD 未变化/不适用源码阶段的内联路径：旧进程直接跑完整后半段。"""
    from local_webpage_access.config import load_config
    from local_webpage_access.registry import Registry
    from local_webpage_access.updater import run_update

    ws = Workspace(workspace_root)
    config = load_config(ws)
    if options.dry_run:
        # BUG-530：dry-run 零写入保证——打开 Registry 会创建 DB 文件或执行
        # schema 迁移。run_update 的 dry-run 分支只输出计划、不使用 registry。
        sub = run_update(ws, config, None, options=options)
    else:
        reg = Registry(ws.db_path)
        reg.open()
        try:
            sub = run_update(ws, config, reg, options=options)
        finally:
            reg.close()
    sub.steps[0:0] = prepend
    sub.repo = report.repo
    return sub


def run_update_flow(
    workspace_root: Path,
    options: UpdateOptions,
    *,
    continuation_timeout: int = CONTINUATION_TIMEOUT,
) -> UpdateReport:
    """两阶段 update 编排入口（CLI 直接调用；不在加载 Config/Registry 之前）。

    返回合并后的最终报告：bootstrap 源码步骤 + （continuation 或内联的）
    Runtime 步骤。
    """
    ws = Workspace(workspace_root)
    version_before = resolve_version()
    report = UpdateReport(
        workspace=str(ws.root),
        repo=None,
        version_before=version_before,
        version_after=version_before,
    )

    # ---- 1. 识别 repo（不加载 Config/Registry）----
    repo: Path | None = None
    repo_error: str | None = None
    try:
        repo = locate_repo(options.repo)
    except FileNotFoundError as exc:
        repo_error = str(exc)
    report.repo = str(repo) if repo else options.repo

    # ---- dry-run：零写入预览，不取锁不 fetch ----
    if options.dry_run:
        report.steps.append(_dry_run_source_step(repo, options))
        return _inline_tail(workspace_root, options, report.steps, report)

    # ---- --no-pull / 非 git 安装：源码阶段 skipped，旧路径内联 ----
    if not options.pull:
        report.steps.append(
            StepResult(
                "sourceUpdate",
                "skipped",
                "--no-pull：不联网，仅用本地代码刷新 Runtime",
            )
        )
        return _inline_tail(workspace_root, options, report.steps, report)
    if repo is None:
        if repo_error:
            report.steps.append(StepResult("sourceUpdate", "failed", repo_error))
        else:
            report.steps.append(
                StepResult(
                    "sourceUpdate",
                    "skipped",
                    "未识别到 lwa 源码根，跳过源码更新（如已手动装过可用 --skip-pip）",
                )
            )
        return _inline_tail(workspace_root, options, report.steps, report)
    if not us.git_available():
        report.steps.append(
            StepResult(
                "sourceUpdate",
                "failed",
                "git 不可执行，无法做源码更新（.git 存在）",
                extra={"errorKind": "git_unavailable"},
            )
        )
        return _inline_tail(workspace_root, options, report.steps, report)
    if not us.is_git_repo(repo):
        report.steps.append(
            StepResult(
                "sourceUpdate",
                "skipped",
                "非 git 克隆安装（无 .git）：源码更新不适用；"
                "建议迁移到 clone + pip install -e . 安装",
            )
        )
        return _inline_tail(workspace_root, options, report.steps, report)

    # ---- git 安装：锁 → 目标解析 → fetch → 门禁 → 快进 ----
    locks: us.UpdateLocks | None = None
    try:
        try:
            locks = us.acquire_update_locks(repo, ws.root)
        except us.UpdateLockBusy as exc:
            # BUG-529：锁忙说明已有 update 持锁执行中（可能正在 pip/迁移/重启
            # 服务），此时绝不能再跑 Runtime 后半段，否则与持锁更新并发。
            # 立即失败返回，不进 _inline_tail。
            report.steps.append(
                StepResult(
                    "sourceUpdate",
                    "failed",
                    f"更新锁被占用（{exc.scope}），已有 update 在执行：{exc}",
                )
            )
            report.version_after = resolve_version()
            return report

        target: us.SourceTarget | None = None
        status: us.RepoStatus | None = None
        candidate: str | None = None
        old_head: str | None = None
        new_head: str | None = None
        head_changed = False

        try:
            target = us.resolve_source_target(repo, options.remote, options.ref)
            candidate = us.fetch_candidate(repo, target)
            status = us.inspect_repo(repo, candidate)
            ff = us.apply_fast_forward(repo, target, candidate, status, skip_pip=options.skip_pip)
            old_head, new_head = ff.old_head, ff.new_head
            head_changed = old_head != new_head
            if head_changed:
                before = _version_note(us.describe_commit(repo, old_head))
                after = _version_note(ff.descriptor)
                report.steps.append(
                    StepResult(
                        "sourceUpdate",
                        "ok",
                        f"已快进 {target.remote}/{target.branch}：{before} → {after}",
                        extra=_source_extra(
                            old_head=old_head,
                            candidate=candidate,
                            new_head=new_head,
                            target=target,
                            status=status,
                            fresh=True,
                        ),
                    )
                )
            else:
                report.steps.append(
                    StepResult(
                        "sourceUpdate",
                        "skipped",
                        f"已是最新（{target.remote}/{target.branch} @ "
                        f"{_version_note(ff.descriptor)}）",
                        extra=_source_extra(
                            old_head=old_head,
                            candidate=candidate,
                            new_head=new_head,
                            target=target,
                            status=status,
                            fresh=True,
                        ),
                    )
                )
        except us.FetchError as exc:
            # fetch 失败是降级不是故障（§15.2）：不改工作树，本地代码继续
            report.steps.append(
                StepResult(
                    "sourceUpdate",
                    "warning",
                    f"git fetch 失败（{exc.message}）：不改工作树，"
                    "以本地代码继续；检查网络/代理后重试可拉取新版本",
                    extra={"errorKind": "fetch_failed"},
                )
            )
        except us.SourceUpdateError as exc:
            report.steps.append(
                StepResult(
                    "sourceUpdate",
                    "failed",
                    f"[{exc.kind}] {exc.message}" + (f"；建议：{exc.action}" if exc.action else ""),
                    extra={"errorKind": exc.kind},
                )
            )

        # ---- pip（父进程执行；skip_pip 语义见 §15.1.6）----
        # behind + skip_pip 已在快进前被 apply_fast_forward 拒绝；
        # 到这里的 skip_pip 只可能伴随「未改 HEAD」路径（--no-pull 除外，此分支
        # 仅 git 安装可达），保留旧语义。
        if options.skip_pip:
            report.steps.append(StepResult("pip", "skipped", "已通过 --skip-pip 跳过"))
        else:
            try:
                summary = run_pip_install(repo)
                report.steps.append(StepResult("pip", "ok", summary))
                resolve_version.cache_clear()
            except Exception as exc:  # noqa: BLE001
                report.steps.append(StepResult("pip", "failed", str(exc)))
                if head_changed:
                    # 快进后 pip 失败：阻断 continuation，给恢复链（§15.1.8）
                    report.steps.append(
                        StepResult(
                            "continuation",
                            "failed",
                            "快进后 pip 安装失败，已停止 Runtime 后半段。"
                            + us.recovery_hint(old_head),
                        )
                    )
                    report.version_after = resolve_version()
                    return report
                # 未改 HEAD 的 pip 失败：沿用旧「每步独立失败」语义，后半段继续

        # ---- Runtime 后半段：内联 or continuation ----
        if not head_changed:
            config = None
            from local_webpage_access.config import load_config
            from local_webpage_access.registry import Registry

            config = load_config(ws)
            reg = Registry(ws.db_path)
            reg.open()
            try:
                inline_options = dataclasses.replace(options, skip_pip=True)
                _run_runtime_phase(ws, config, reg, inline_options, report)
            finally:
                reg.close()
            report.version_after = resolve_version()
            return report

        # HEAD 已变：新解释器接力（禁止新旧代码混跑）
        child_report, child_error = _launch_continuation(
            ws,
            options,
            locks,
            old_head=old_head or "",
            new_head=new_head or "",
            timeout=continuation_timeout,
        )
        if child_error is not None:
            report.steps.append(
                StepResult(
                    "continuation",
                    "failed",
                    f"{child_error} " + us.recovery_hint(old_head),
                )
            )
            report.version_after = resolve_version()
            return report
        if child_report is not None:
            report.steps.extend(child_report.steps)
            report.manager_url = child_report.manager_url
            report.doctor_status = child_report.doctor_status
            report.version_after = child_report.version_after
        return report
    finally:
        if locks is not None:
            locks.close()


# ---- 063.08 handoff v1 与 continuation 启动 -----------------------------------


def _sanitize_options(options: UpdateOptions) -> dict[str, Any]:
    """handoff 选项白名单（仅 UpdateOptions 字段；continuation 强制 noPull+skipPip）。"""
    return {
        "dry_run": False,
        "skip_pip": True,  # 防递归：pip 已在 bootstrap 完成
        "sync_skills": options.sync_skills,
        "sync_templates": options.sync_templates,
        "restart_manager": options.restart_manager,
        "restart_daemon": options.restart_daemon,
        "restart_gateway": options.restart_gateway,
        "restart_instances": options.restart_instances,
        "run_doctor": options.run_doctor,
        "review_access": options.review_access,
        "repo": options.repo,
        "reconcile_services": options.reconcile_services,
        "pull": False,  # 防递归：源码已在 bootstrap 完成
        "remote": None,
        "ref": None,
    }


def _launch_continuation(
    ws: Workspace,
    options: UpdateOptions,
    locks: us.UpdateLocks,
    *,
    old_head: str,
    new_head: str,
    timeout: int,
) -> tuple[UpdateReport | None, str | None]:
    """启动新解释器执行 Runtime 后半段，返回 (子报告, 错误信息)。"""
    payload = {
        "schemaVersion": HANDOFF_SCHEMA_VERSION,
        "oldHead": old_head,
        "newHead": new_head,
        "workspace": str(ws.root),
        "options": _sanitize_options(options),
        "sourceUpdateResult": {"oldHead": old_head, "newHead": new_head},
        "repoLockFd": locks.repo_fd,
        "workspaceLockFd": locks.ws_fd,
    }
    import os

    pass_fds = tuple(fd for fd in locks.fds if fd >= 0)
    cmd = [sys.executable, "-m", CONTINUATION_MODULE]
    log.info("启动 continuation（%s，pass_fds=%s）", " ".join(cmd), pass_fds)
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,  # 子进程日志直接透传终端/logs
            text=True,
            pass_fds=pass_fds,
            close_fds=True,
            env={**os.environ, "LWA_UPDATE_CONTINUATION": "1"},
        )
    except OSError as exc:
        return None, f"无法启动 continuation 子进程：{exc}"
    try:
        out, _err = proc.communicate(json.dumps(payload, ensure_ascii=False), timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return None, f"continuation 超时（{timeout}s）被终止"
    if proc.returncode != 0:
        # BUG-531：Runtime 阶段步骤失败时 continuation 仍会打印完整 JSON
        # 子报告后 exit 1。先尝试解析回传，把失败步骤与恢复提示带回来；
        # 解析失败（真崩溃）才退化为纯退出码错误。
        line = _last_json_line(out or "")
        if line is not None:
            try:
                ack = json.loads(line)
            except json.JSONDecodeError:
                ack = None
            if (
                isinstance(ack, dict)
                and ack.get("codeHead") == new_head
                and isinstance(ack.get("report"), dict)
            ):
                return UpdateReport.from_dict(ack["report"]), None
        tail = (out or "").strip().splitlines()[-3:]
        return None, (
            f"continuation 退出码 {proc.returncode}" + (f"：{' | '.join(tail)}" if tail else "")
        )
    line = _last_json_line(out or "")
    if line is None:
        return None, "continuation 未返回可解析的子报告 JSON"
    try:
        ack = json.loads(line)
    except json.JSONDecodeError as exc:
        return None, f"continuation 子报告解析失败：{exc}"
    if ack.get("codeHead") != new_head:
        return None, (
            f"continuation 运行代码 ({str(ack.get('codeHead'))[:12]}) "
            f"与快进目标 ({new_head[:12]}) 不一致"
        )
    report_data = ack.get("report")
    if not isinstance(report_data, dict):
        return None, "continuation 子报告缺少 report 字段"
    return UpdateReport.from_dict(report_data), None


def _last_json_line(text: str) -> str | None:
    """从子进程 stdout 取最后一个可解析的 JSON 行（协议回传通道）。"""
    for line in reversed((text or "").strip().splitlines()):
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            return stripped
    return None


__all__ = [
    "run_update_flow",
    "_launch_continuation",
    "HANDOFF_SCHEMA_VERSION",
    "CONTINUATION_TIMEOUT",
]
