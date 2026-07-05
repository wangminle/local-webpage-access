"""``lwa init`` 工作区初始化逻辑（WBS-03）。

创建目录结构、默认配置、默认模板，并初始化 SQLite registry。
支持幂等重复执行：已存在的实例和 registry 不会被破坏。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from local_web_access.config import example_config_text
from local_web_access.errors import LwaError
from local_web_access.logging import get_logger
from local_web_access.paths import Workspace
from local_web_access.registry import Registry

log = get_logger("init")

# 打包内置模板目录
_BUNDLED_TEMPLATES = Path(__file__).parent / "templates"


def init_workspace(root: Path, *, force: bool = False) -> str:
    """初始化工作区。

    Args:
        root: 工作区根目录。
        force: 为 True 时强制覆盖配置文件和模板。

    Returns:
        初始化摘要文本（供 CLI 输出）。
    """
    root.mkdir(parents=True, exist_ok=True)
    ws = Workspace(root)

    # 1. 创建所有顶层目录
    ws.ensure_workspace_dirs()

    # 2. 写入默认配置
    config_written = _write_default_config(ws, force=force)

    # 3. 复制默认模板（用于用户编辑）
    templates_written = _copy_default_templates(ws, force=force)

    # 4. 初始化 SQLite registry（幂等：已存在则只跑迁移）
    reg = Registry(ws.db_path)
    reg.open()
    try:
        db_version = _schema_version_safe(reg)
    finally:
        reg.close()

    log.info("工作区已初始化：%s", ws.root)
    return _format_summary(
        ws,
        config_written=config_written,
        templates_written=templates_written,
        db_version=db_version,
    )


def _write_default_config(ws: Workspace, *, force: bool) -> bool:
    if ws.config_path.exists() and not force:
        log.debug("配置文件已存在，跳过：%s", ws.config_path)
        return False
    ws.config_path.write_text(example_config_text(), encoding="utf-8")
    log.info("已写入默认配置：%s", ws.config_path)
    return True


def _copy_default_templates(ws: Workspace, *, force: bool) -> list[str]:
    written: list[str] = []
    if not _BUNDLED_TEMPLATES.is_dir():
        log.debug("内置模板目录不存在，跳过复制")
        return written

    for src in _BUNDLED_TEMPLATES.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(_BUNDLED_TEMPLATES)
        dst = ws.templates / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and not force:
            continue
        shutil.copy2(src, dst)
        written.append(str(rel).replace("\\", "/"))
    return written


def _schema_version_safe(reg: Registry) -> int:
    from local_web_access.registry.connection import CURRENT_SCHEMA_VERSION

    try:
        from local_web_access.registry.connection import get_schema_version

        return get_schema_version(reg.conn)
    except Exception:
        return CURRENT_SCHEMA_VERSION


def _format_summary(
    ws: Workspace,
    *,
    config_written: bool,
    templates_written: list[str],
    db_version: int,
) -> str:
    lines: list[str] = []
    lines.append("── 工作区目录 ──")
    lines.append(f"  根目录       {ws.root}")
    lines.append(f"  inbox/       {ws.inbox}")
    lines.append(f"  apps/        {ws.apps}")
    lines.append(f"  registry/    {ws.db_path}")
    lines.append(f"  static-gateway/  {ws.static_gateway}")
    lines.append("")
    lines.append("── 初始化结果 ──")
    lines.append(f"  配置文件     {'已写入' if config_written else '已存在（保留）'}  {ws.config_path}")
    if templates_written:
        lines.append(f"  默认模板     已复制 {len(templates_written)} 个文件")
    else:
        lines.append("  默认模板     已存在（保留）")
    lines.append(f"  SQLite       schema_version={db_version}  {ws.db_path}")
    return "\n".join(lines)


__all__ = ["init_workspace"]
