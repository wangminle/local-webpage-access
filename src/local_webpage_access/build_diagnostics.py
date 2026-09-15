"""构建失败分类与探针失败摘要（issue #35 建议 3 / CHK-326）。"""

from __future__ import annotations

import re
from dataclasses import dataclass

_APT_FETCH_RE = re.compile(
    r"Failed to fetch|Connection failed|Unable to fetch some archives",
    re.IGNORECASE,
)
_OOM_RE = re.compile(
    r"cannot allocate memory|ResourceExhausted|OOMKilled",
    re.IGNORECASE,
)
_KILLED_RE = re.compile(r"\bKilled\b")
_DISK_RE = re.compile(r"No space left on device", re.IGNORECASE)


@dataclass(frozen=True)
class FailureHint:
    """分类结果：保留原始证据，并区分 likely / uncertain。"""

    kind: str
    summary: str
    confidence: str = "likely"
    evidence: str = ""

    def __str__(self) -> str:
        return self.summary


def _clip_evidence(blob: str, limit: int = 400) -> str:
    text = blob.strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def classify_build_failure(text: str | None) -> FailureHint | None:
    """从构建输出识别可行动根因；未命中返回 ``None``。"""
    blob = (text or "").strip()
    if not blob:
        return None
    evidence = _clip_evidence(blob)
    if _APT_FETCH_RE.search(blob):
        return FailureHint(
            kind="apt",
            summary=(
                "镜像源不可达：apt 拉取失败（Failed to fetch / Connection failed）。"
                "可在 local-web.yml 配置 buildMirrors.aptFallbacks / aptRetries / aptTimeout，"
                "或把系统包装入 manifest.systemDeps 走内置切源链后重试构建。"
            ),
            confidence="likely",
            evidence=evidence,
        )
    if _OOM_RE.search(blob):
        return FailureHint(
            kind="oom",
            summary=(
                "内存不足：构建输出含 cannot allocate memory / ResourceExhausted / OOMKilled。"
                "请提高 Docker Desktop 内存上限，避免并发构建，或错开长 apt 下载。"
            ),
            confidence="likely",
            evidence=evidence,
        )
    if _DISK_RE.search(blob):
        return FailureHint(
            kind="disk",
            summary=(
                "磁盘不足（No space left on device）。"
                "可执行 docker builder prune 清理构建缓存后重试。"
            ),
            confidence="likely",
            evidence=evidence,
        )
    if _KILLED_RE.search(blob):
        return FailureHint(
            kind="killed",
            summary=(
                "进程被杀死（Killed），原因未确认：可能是 OOM、cgroup 限制或外部 kill，"
                "不能单凭该词断言 Docker VM OOM。请结合 ExitCode / OOMKilled 与日志尾部。"
            ),
            confidence="uncertain",
            evidence=evidence,
        )
    return None


def format_probe_failure(
    base_error: str,
    *,
    container_state: str | None = None,
    restart_count: int | None = None,
    logs: str | None = None,
    exit_code: int | None = None,
    oom_killed: bool | None = None,
    stage: str | None = None,
    build_log: str | None = None,
    limit: int = 1800,
) -> str:
    """探针失败文案：原超时信息 + 容器状态 + 原始证据。"""
    parts = [base_error.strip()]
    if stage:
        parts.append(f"失败阶段：{stage}")
    state = (container_state or "").strip().lower()
    # BUG-665：RestartCount 未知时不伪造数值，标「未知」。
    known_restarts = restart_count if isinstance(restart_count, int) else None
    count_txt = (
        f"RestartCount={known_restarts}" if known_restarts is not None else "RestartCount=未知"
    )
    frequent = state in {"restarting", "exited"} or (
        known_restarts is not None and known_restarts >= 2
    )
    if frequent:
        parts.append(
            f"容器反复重启（state={container_state or '?'}，{count_txt}），"
            "启动即崩，见容器日志。"
        )
    elif container_state:
        parts.append(f"容器状态：{container_state}（{count_txt}）")
    inspect_bits: list[str] = []
    if exit_code is not None:
        inspect_bits.append(f"ExitCode={exit_code}")
    if oom_killed is not None:
        inspect_bits.append(f"OOMKilled={'true' if oom_killed else 'false'}")
    if inspect_bits:
        parts.append("容器 inspect 原始证据：" + "，".join(inspect_bits))
    if build_log:
        parts.append(f"构建日志：{build_log}")
    tail = (logs or "").strip()
    if tail:
        clipped = tail[-800:]
        parts.append("容器日志尾部：\n" + clipped)
    return "\n".join(parts)[:limit]


__all__ = ["FailureHint", "classify_build_failure", "format_probe_failure"]
