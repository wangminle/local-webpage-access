"""实例日志读取与滚动（WBS-18.01 / 18.02 / 18.03 / 18.11）。

日志分类（WBS-18.02）：
* ``build`` —— 构建日志（``apps/<id>/logs/build.log``）；
* ``run`` —— 运行日志（``run.log``，docker compose up/start 输出）；
* ``gateway`` —— 静态网关日志（``gateway.log``）；
* ``import`` / ``scan`` —— 导入与重扫日志。

提供：
* :func:`read_log` 读取最近 N 行（WBS-18.03）；
* :func:`list_logs` 列出实例所有日志及大小；
* :func:`rotate_log` / :func:`rotate_all` 按大小滚动的日志治理
  （WBS-18.11，对应设计 §16.6：单文件 10MB、保留最近 N 份）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from local_webpage_access.errors import PathError
from local_webpage_access.paths import Workspace

# 已知日志分类（用于校验与文档）；list_logs 不局限于此，会列出实际存在的 .log
LOG_CATEGORIES = ("build", "run", "gateway", "import", "scan")

DEFAULT_TAIL = 200
# 设计 §16.6：单文件上限 10MB，保留最近 3 份
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_KEEP = 3


@dataclass(frozen=True)
class LogInfo:
    """单个日志文件的元信息。"""

    category: str
    path: Path
    size: int
    mtime: float

    @property
    def exists(self) -> bool:
        return self.path.is_file()


def log_path(workspace: Workspace, instance_id: str, category: str) -> Path:
    """返回实例某类日志文件路径（不保证存在）。"""
    validate_log_category(category)
    log_dir = workspace.app_logs(instance_id).resolve()
    path = (log_dir / f"{category}.log").resolve()
    try:
        path.relative_to(log_dir)
    except ValueError as exc:
        raise PathError(
            f"日志路径越界：{category!r}",
            instance_id=instance_id,
            category=category,
        ) from exc
    return path


def validate_log_category(category: str) -> str:
    """校验日志分类，拒绝路径穿越和非预期日志文件读取。"""
    if category not in LOG_CATEGORIES:
        raise PathError(
            f"非法日志分类：{category!r}（允许：{', '.join(LOG_CATEGORIES)}）",
            category=category,
        )
    return category


def read_log(
    workspace: Workspace,
    instance_id: str,
    category: str,
    *,
    tail: int = DEFAULT_TAIL,
) -> str:
    """读取实例某类日志（WBS-18.01 / 18.03）。

    ``tail`` 取最近 N 行；``tail=0`` 或负数返回全文。文件不存在返回空串。
    """
    path = log_path(workspace, instance_id, category)
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if tail is None or tail <= 0:
        return text
    lines = text.splitlines()
    return "\n".join(lines[-tail:])


def list_logs(workspace: Workspace, instance_id: str) -> list[LogInfo]:
    """列出实例所有 ``*.log`` 文件及大小（WBS-18.02）。"""
    log_dir = workspace.app_logs(instance_id)
    if not log_dir.is_dir():
        return []
    infos: list[LogInfo] = []
    for p in sorted(log_dir.glob("*.log")):
        try:
            st = p.stat()
        except OSError:
            continue
        infos.append(
            LogInfo(category=p.stem, path=p, size=st.st_size, mtime=st.st_mtime)
        )
    return infos


def rotate_log(
    workspace: Workspace,
    instance_id: str,
    category: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    keep: int = DEFAULT_KEEP,
) -> bool:
    """单类日志按大小滚动（WBS-18.11）。

    超过 ``max_bytes`` 时把当前文件改名为 ``<category>.log.1``，旧的 ``.1``
    顺延为 ``.2``，依此类推；保留最多 ``keep`` 份，最旧的删除。返回是否触发滚动。
    """
    if keep < 1:
        keep = 1
    path = log_path(workspace, instance_id, category)
    if not path.is_file():
        return False
    try:
        if path.stat().st_size <= max_bytes:
            return False
    except OSError:
        return False

    # 删除最旧的一份（.log.<keep>）
    path.with_name(f"{category}.log.{keep}").unlink(missing_ok=True)
    # .log.<keep-1> → .log.<keep>，..., .log.1 → .log.2
    for i in range(keep - 1, 0, -1):
        src = path.with_name(f"{category}.log.{i}")
        if src.exists():
            src.rename(path.with_name(f"{category}.log.{i + 1}"))
    # 当前 → .log.1
    path.rename(path.with_name(f"{category}.log.1"))
    return True


def rotate_all(
    workspace: Workspace,
    instance_id: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    keep: int = DEFAULT_KEEP,
) -> list[str]:
    """对实例所有日志执行滚动，返回触发滚动的分类列表。"""
    rotated: list[str] = []
    for info in list_logs(workspace, instance_id):
        if info.category not in LOG_CATEGORIES:
            continue
        if rotate_log(workspace, instance_id, info.category, max_bytes=max_bytes, keep=keep):
            rotated.append(info.category)
    return rotated


__all__ = [
    "LOG_CATEGORIES",
    "DEFAULT_TAIL",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_KEEP",
    "LogInfo",
    "validate_log_category",
    "log_path",
    "read_log",
    "list_logs",
    "rotate_log",
    "rotate_all",
]
