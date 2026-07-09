"""zip 校验、哈希与安全解压（DEV-045 / WBS-20260708 阶段5.2）。

从原 :mod:`local_webpage_access.importer` 抽出，使 :class:`Importer` 专注实例
目录与 registry 管理。本模块只做无状态的 zip 处理：

* :func:`validate_zip`      —— 校验 zip 存在、扩展名、可读性（含截断检测）；
* :func:`compute_zip_hash`  —— 流式 SHA256 摘要；
* :func:`safe_extract`      —— 剥离冗余成员 + 集中安全审计 + zip slip / symlink
  防护 + 单层根目录拍平（IMP-001 / BUG-049）。

这些函数不依赖工作区或 registry，仅依赖入参。
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import zipfile
from pathlib import Path

from local_webpage_access.errors import ZipImportError
from local_webpage_access.logging import get_logger
from local_webpage_access.security import (
    ZipSanitizeResult,
    audit_zip_members,
    has_critical,
    sanitize_zip_members,
)

log = get_logger("importer")

_HASH_CHUNK = 64 * 1024


def validate_zip(zip_path: Path) -> None:
    """校验 zip 文件存在、扩展名与可读性（含损坏 / 截断检测）。"""
    if not zip_path.is_file():
        raise ZipImportError(f"zip 文件不存在：{zip_path}", path=str(zip_path))
    if zip_path.suffix.lower() != ".zip":
        raise ZipImportError(
            f"仅支持 .zip 文件，得到 {zip_path.name}",
            path=str(zip_path),
        )
    if not zipfile.is_zipfile(zip_path):
        raise ZipImportError(f"不是合法的 zip 文件：{zip_path}", path=str(zip_path))
    # 损坏的 zip（如截断）会在 ZipFile 构造或读取时报错
    try:
        with zipfile.ZipFile(zip_path) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise ZipImportError(
                    f"zip 文件损坏，首个坏文件：{bad}",
                    path=str(zip_path),
                )
    except zipfile.BadZipFile as exc:
        raise ZipImportError(
            f"zip 文件无法读取：{exc}",
            path=str(zip_path),
        ) from exc


def compute_zip_hash(zip_path: Path) -> str:
    """流式计算 zip 文件的 SHA256 摘要。"""
    h = hashlib.sha256()
    with zip_path.open("rb") as fh:
        while True:
            chunk = fh.read(_HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def safe_extract(zip_path: Path, target: Path) -> ZipSanitizeResult:
    """带剥离 / zip slip / 符号链接防护与单层根目录拍平的解压（IMP-001 / BUG-049）。

    安全流程四步：
    1. **剥离分类（IMP-001）**：``sanitize_zip_members`` 把 ``node_modules/``、
       ``__pycache__/``、``.venv/``、``.git/``、``__MACOSX/``、``.DS_Store``
       等冗余成员分到 ``stripped``，**不落盘、不参与后续审计**。这些成员下的
       symlink（如 ``node_modules/.bin/*``，npm 正常产物）随之剥离，不再触发
       ``zip_symlink`` 拒绝；
    2. **集中安全审计（BUG-049）**：仅对保留成员运行 ``audit_zip_members``，
       critical 级（绝对路径 / 盘符 / 路径穿越 / 业务源码目录的恶意 symlink）拒绝；
    3. **路径穿越运行时校验**：每个保留成员 resolve 后必须落在 tmp_path 之内；
    4. **解压后深度防御**：仅解压保留成员；遍历 tmp_path，若出现未在
       external_attr 声明的 symlink（如 Windows 打包器产出的异常 zip），拒绝。

    返回 :class:`~local_webpage_access.security.ZipSanitizeResult` 供调用方
    输出剥离摘要与事件审计。
    """
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lwa-import-") as tmp:
        tmp_path = Path(tmp).resolve()

        with zipfile.ZipFile(zip_path) as zf:
            members = zf.infolist()
            names = [m.filename for m in members]
            modes = [
                (m.external_attr >> 16) & 0xFFFF if m.external_attr else 0
                for m in members
            ]

            # 1. 剥离分类（IMP-001）：冗余包 / 缓存不落盘、不审计
            sanitized = sanitize_zip_members(names, modes=modes)
            keep_members = [members[i] for i in sanitized.keep_indices]
            if sanitized.stripped_names:
                parts = ", ".join(
                    f"{rule}×{n}" for rule, n in sorted(
                        sanitized.categories.items(), key=lambda kv: -kv[1]
                    )
                )
                log.info(
                    "剥离冗余成员 %d 项（含 symlink %d）：%s",
                    len(sanitized.stripped_names),
                    sanitized.stripped_symlink_count,
                    parts,
                )

            # 2. 集中安全审计（BUG-049）：仅保留成员
            keep_names = [m.filename for m in keep_members]
            keep_modes = [
                (m.external_attr >> 16) & 0xFFFF if m.external_attr else 0
                for m in keep_members
            ]
            findings = audit_zip_members(keep_names, modes=keep_modes)
            for f in findings:
                if f.level == "critical":
                    log.warning("zip 成员审计 [%s] %s", f.code, f.message)
            if has_critical(findings):
                codes = ", ".join(
                    f.code for f in findings if f.level == "critical"
                )
                raise ZipImportError(
                    f"zip 成员安全审计未通过（{codes}）",
                    members=keep_names,
                )

            # 3. 运行时路径穿越校验：每个保留成员必须在 tmp_path 之内
            for member in keep_members:
                member_target = (tmp_path / member.filename).resolve()
                try:
                    member_target.relative_to(tmp_path)
                except ValueError:
                    raise ZipImportError(
                        f"检测到路径穿越（zip slip）：{member.filename}",
                        member=member.filename,
                    )
            # 4. 仅解压保留成员（剥离成员不落盘）
            for member in keep_members:
                zf.extract(member, tmp_path)

        # 解压后深度防御：扫描是否产生了 symlink（即使 external_attr
        # 未声明 S_IFLNK，也拒绝任何符号链接，杜绝 zip slip 变种）。
        # 此时 node_modules/.bin 等 symlink 已被剥离，命中即业务源码的恶意链接。
        for item in tmp_path.rglob("*"):
            if item.is_symlink():
                raise ZipImportError(
                    f"检测到符号链接（zip slip）：{item.relative_to(tmp_path)}",
                    member=str(item.relative_to(tmp_path)),
                )

        # 单层根目录拍平：tmp 下只有一个目录且无散落文件时，提升一层
        entries = list(tmp_path.iterdir())
        single_root = len(entries) == 1 and entries[0].is_dir()
        source_root = entries[0] if single_root else tmp_path

        for item in source_root.iterdir():
            dest = target / item.name
            if dest.exists():
                # 同名冲突时跳过（防御性）
                continue
            shutil.move(str(item), str(dest))

    return sanitized


__all__ = ["validate_zip", "compute_zip_hash", "safe_extract"]
