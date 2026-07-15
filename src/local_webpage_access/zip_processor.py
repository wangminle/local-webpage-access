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

# BUG-123：压缩炸弹防护上限（声明大小 / 成员数；在完整解压前拦截）
_MAX_ZIP_MEMBERS = 50_000
_MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
_MAX_SINGLE_MEMBER_BYTES = 512 * 1024 * 1024  # 512 MiB
_MAX_COMPRESSION_RATIO = 100.0
_MIN_RATIO_CHECK_BYTES = 1024 * 1024  # 小文本高度重复很常见，不单凭压缩比拒绝


def _assert_zip_bomb_safe(
    members: list[zipfile.ZipInfo], *, path: str | None = None
) -> None:
    """按声明元数据拒绝可疑压缩炸弹（BUG-123）。"""
    if len(members) > _MAX_ZIP_MEMBERS:
        raise ZipImportError(
            f"zip 成员数过多（{len(members)} > {_MAX_ZIP_MEMBERS}），疑似压缩炸弹",
            path=path,
        )
    total_uncompressed = 0
    for member in members:
        if member.is_dir():
            continue
        uncompressed = int(member.file_size or 0)
        compressed = int(member.compress_size or 0)
        if uncompressed > _MAX_SINGLE_MEMBER_BYTES:
            raise ZipImportError(
                f"zip 成员过大（{member.filename} 声明 "
                f"{uncompressed} 字节 > {_MAX_SINGLE_MEMBER_BYTES}），疑似压缩炸弹",
                path=path,
                member=member.filename,
            )
        if (
            compressed > 0
            and uncompressed >= _MIN_RATIO_CHECK_BYTES
            and uncompressed > compressed * _MAX_COMPRESSION_RATIO
        ):
            ratio = uncompressed / compressed
            raise ZipImportError(
                f"zip 压缩比过高（{member.filename} 约 {ratio:.0f}:1 > "
                f"{_MAX_COMPRESSION_RATIO:.0f}:1），疑似压缩炸弹",
                path=path,
                member=member.filename,
            )
        total_uncompressed += uncompressed
        if total_uncompressed > _MAX_UNCOMPRESSED_BYTES:
            raise ZipImportError(
                f"zip 解压总大小超限（>{_MAX_UNCOMPRESSED_BYTES} 字节），疑似压缩炸弹",
                path=path,
            )


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
            # BUG-123：先按声明大小拦截炸弹，再做 CRC 抽检（testzip 会解压）
            _assert_zip_bomb_safe(zf.infolist(), path=str(zip_path))
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
            # BUG-123：解压前再次按声明元数据拦截（validate_zip 后文件可能被替换）
            _assert_zip_bomb_safe(members, path=str(zip_path))
            names = [m.filename for m in members]
            modes = [
                (m.external_attr >> 16) & 0xFFFF if m.external_attr else 0
                for m in members
            ]

            # 1. 剥离分类（IMP-001）：冗余包 / 缓存不落盘、不审计
            sanitized = sanitize_zip_members(names, modes=modes)
            keep_members = [members[i] for i in sanitized.keep_indices]
            # 对保留成员再算一次上限（剥离后仍可能过大）
            _assert_zip_bomb_safe(keep_members, path=str(zip_path))
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
