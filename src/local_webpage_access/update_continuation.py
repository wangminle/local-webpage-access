"""update continuation 内部入口（IMP-063.08，非公开 CLI）。

由 :mod:`local_webpage_access.update_flow` 经
``python -m local_webpage_access.update_continuation`` 启动：

* 从 stdin 读 handoff v1 JSON（schemaVersion/oldHead/newHead/workspace/options/
  sourceUpdateResult + 继承 FD 号）；
* **在任何 Runtime 写入前**校验：协议版本、FD 已继承、当前代码 HEAD ==
  newHead（证明跑的是新代码）；
* 校验通过后重新 import 全部代码（新解释器天然如此），重读 Config/Registry
  并执行 Runtime 后半段（skills/templates/migrate/重启/access/doctor）；
* 最后一行 stdout 输出回执 JSON（含子报告），父进程合并。

缺继承 FD / 协议不兼容 / 代码 HEAD 不符 → 拒绝执行，exit 3。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HANDOFF_SCHEMA_VERSION = 1

#: 拒绝执行（缺 FD/协议不符/代码不符）的专用退出码
EXIT_REFUSED = 3


def _refuse(reason: str) -> None:
    print(f"continuation refused: {reason}", file=sys.stderr)
    raise SystemExit(EXIT_REFUSED)


def _fd_is_open(fd: int) -> bool:
    try:
        os.fstat(fd)
        return True
    except OSError:
        return False


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        _refuse(f"handoff JSON 无法解析：{exc}")
        return
    if not isinstance(payload, dict):
        _refuse("handoff 非对象")
        return
    if payload.get("schemaVersion") != HANDOFF_SCHEMA_VERSION:
        _refuse(f"handoff schemaVersion 不兼容：{payload.get('schemaVersion')}")
        return
    if os.environ.get("LWA_UPDATE_CONTINUATION") != "1":
        _refuse("缺少 continuation 环境标记（该入口仅供 lwa update 接力调用）")
        return

    repo_fd = int(payload.get("repoLockFd", -1))
    ws_fd = int(payload.get("workspaceLockFd", -1))
    if repo_fd < 0 or ws_fd < 0 or not _fd_is_open(repo_fd) or not _fd_is_open(ws_fd):
        _refuse(f"未继承更新锁 FD（repo={repo_fd}, workspace={ws_fd}）")
        return

    workspace = str(payload.get("workspace") or "")
    new_head = str(payload.get("newHead") or "")
    if not workspace or not new_head:
        _refuse("handoff 缺 workspace/newHead")
        return

    # 证明当前解释器加载的是新代码：进程内包位置对应的 repo HEAD 必须等于 newHead
    from local_webpage_access import update_source as us
    from local_webpage_access.version_info import _repo_root

    repo_root = _repo_root()
    code_head = ""
    if repo_root is not None:
        try:
            code_head = us._rev_parse_head(repo_root)
        except us.SourceUpdateError:
            code_head = ""
    if code_head != new_head:
        _refuse(f"当前代码 HEAD ({code_head[:12] or '?'}) 与快进目标 ({new_head[:12]}) 不符")
        return

    # ---- 校验通过：重建 options（白名单）并执行 Runtime 后半段 ----
    from local_webpage_access.updater import UpdateOptions, _run_runtime_phase

    raw_options = payload.get("options") or {}
    allowed = {f.name for f in UpdateOptions.__dataclass_fields__.values()}
    options = UpdateOptions(**{k: v for k, v in raw_options.items() if k in allowed})

    from local_webpage_access.config import load_config
    from local_webpage_access.paths import Workspace
    from local_webpage_access.registry import Registry
    from local_webpage_access.updater import UpdateReport
    from local_webpage_access.version_info import resolve_version

    ws = Workspace(Path(workspace))
    report = UpdateReport(
        workspace=str(ws.root),
        repo=str(repo_root) if repo_root else None,
        version_before=resolve_version(),
        version_after=resolve_version(),
    )
    reg = Registry(ws.db_path)
    reg.open()
    try:
        config = load_config(ws)
        _run_runtime_phase(ws, config, reg, options, report)
    finally:
        reg.close()
    report.version_after = resolve_version()

    # §15.1.8：配置迁移等升级关键失败在已快进上下文中附人工恢复链（不自动执行）
    migrate_step = report.step("migrateConfig")
    old_head = str(payload.get("oldHead") or "")
    if migrate_step is not None and migrate_step.status == "failed" and old_head:
        migrate_step.message = f"{migrate_step.message} {us.recovery_hint(old_head)}"

    ack = {
        "schemaVersion": HANDOFF_SCHEMA_VERSION,
        "ok": not report.has_failures,
        "codeHead": code_head,
        "report": report.to_dict(),
    }
    print(json.dumps(ack, ensure_ascii=False))
    raise SystemExit(0 if not report.has_failures else 1)


if __name__ == "__main__":
    main()
