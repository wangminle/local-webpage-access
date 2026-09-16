"""构建失败分类与探针失败摘要（issue #35 建议 3 / CHK-326）。"""

from __future__ import annotations

import re
from dataclasses import dataclass

_REGISTRY_RE = re.compile(
    r"auth\.docker\.io|registry-1\.docker\.io|registry\.docker\.io|"
    r"failed to fetch anonymous token",
    re.IGNORECASE,
)
_FAIL_MARK_RE = re.compile(
    r"(?:^|\n)(?:#\d+\s+ERROR:|ERROR:|failed to solve|E: )",
    re.IGNORECASE,
)
# 收窄：不得把 Docker Hub「failed to fetch anonymous token」误判为 apt（issue #36）。
_APT_FETCH_RE = re.compile(
    r"Unable to fetch some archives|Err:\d+|Failed to fetch \S+://",
    re.IGNORECASE,
)
_OOM_RE = re.compile(
    r"cannot allocate memory|ResourceExhausted|OOMKilled",
    re.IGNORECASE,
)
_KILLED_RE = re.compile(r"\bKilled\b", re.IGNORECASE)
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


def _failure_region(blob: str) -> str:
    """BUG-682/687：取失败步骤上下文，避免成功 registry 日志抢归因。

    ``failed to solve`` 常只是摘要行，证据在其前；优先从最后一次
    ``#N ERROR:`` / ``ERROR:`` / ``E: `` 截取。真实失败特征（如 ``Killed``）
    常出现在**同一步骤**的 ERROR 行之前——按步骤号保留该步骤此前的有界
    前文；无步骤编号时保留有界前文。其它步骤的日志（如成功的 registry
    拉取）仍被排除，维持 BUG-682 的收窄意图。
    """
    matches = list(_FAIL_MARK_RE.finditer(blob))
    if not matches:
        return blob
    error_marks = [
        m
        for m in matches
        if re.search(r"(?:#\d+\s+ERROR:|ERROR:|E: )", m.group(0), re.IGNORECASE)
    ]
    if not error_marks:
        start = matches[-1].start()
        prefix_lines = blob[:start].splitlines()
        keep = "\n".join(prefix_lines[-8:])
        return f"{keep}\n{blob[start:]}" if keep else blob[start:]
    last_error = error_marks[-1]
    start = last_error.start()
    prefix = blob[:start]
    step_m = re.search(r"#(\d+)\s+ERROR:", last_error.group(0))
    if step_m:
        step_tag = f"#{step_m.group(1)} "
        step_lines = [ln for ln in prefix.splitlines() if ln.startswith(step_tag)]
    else:
        step_lines = prefix.splitlines()
    keep = "\n".join(step_lines[-8:])
    return f"{keep}\n{blob[start:]}" if keep else blob[start:]


def classify_build_failure(text: str | None) -> FailureHint | None:
    """从构建输出识别可行动根因；未命中返回 ``None``。"""
    blob = (text or "").strip()
    if not blob:
        return None
    region = _failure_region(blob)
    evidence = _clip_evidence(region)
    if _APT_FETCH_RE.search(region):
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
    if _OOM_RE.search(region):
        return FailureHint(
            kind="oom",
            summary=(
                "内存不足：构建输出含 cannot allocate memory / ResourceExhausted / OOMKilled。"
                "请提高 Docker Desktop 内存上限，避免并发构建，或错开长 apt 下载。"
            ),
            confidence="likely",
            evidence=evidence,
        )
    if _DISK_RE.search(region):
        return FailureHint(
            kind="disk",
            summary=(
                "磁盘不足（No space left on device）。"
                "可执行 docker builder prune 清理构建缓存后重试。"
            ),
            confidence="likely",
            evidence=evidence,
        )
    if _KILLED_RE.search(region):
        return FailureHint(
            kind="killed",
            summary=(
                "进程被杀死（Killed），原因未确认：可能是 OOM、cgroup 限制或外部 kill，"
                "不能单凭该词断言 Docker VM OOM。请结合 ExitCode / OOMKilled 与日志尾部。"
            ),
            confidence="uncertain",
            evidence=evidence,
        )
    if _REGISTRY_RE.search(region):
        return FailureHint(
            kind="registry",
            summary=(
                "镜像仓库不可达：拉取 Docker Hub / registry token 失败"
                "（auth.docker.io / failed to fetch anonymous token）。"
                "请检查本机到 Docker Hub 的网络、代理或镜像加速，然后重试构建。"
            ),
            confidence="likely",
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
