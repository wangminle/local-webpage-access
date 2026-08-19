"""本机文件夹源导入辅助（IMP-047）。

职责：
1. 校验源目录路径（本机、存在、可读、非工作区内）。
2. 将源目录内容打包为临时 zip（只读源目录），复用 IMP-001 剥离。
3. 计算源目录的内容指纹（SHA256），供无变更短路。

红线：源目录**只读**，所有写入仅发生在 LWA 工作区。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from local_webpage_access.errors import FolderSourceError
from local_webpage_access.logging import get_logger

log = get_logger("folder_source")

# 与 IMP-001 剥离规则一致的跳过目录/文件名集合。
# 打包时跳过这些，与 zip 导入时剥离的成员对齐。
_SKIP_DIRS = {
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    ".git",
    ".svn",
    ".hg",
    "__MACOSX",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}
_SKIP_FILES = {".DS_Store", "Thumbs.db"}

#: 确定性 zip 时间戳（zip 纪元 1980-01-01）。若用源文件 mtime，同一内容在
#: 不同时刻打包会产生不同 zip 字节流 → compute_zip_hash 抖动，「打包内容
#: 未变」的 skipped 判定退化为 2 秒 DOS 时间窗内的运气（CHK-239）。
_ZIP_FIXED_DATE = (1980, 1, 1, 0, 0, 0)


def validate_source_dir(
    source_dir: str | Path,
    *,
    workspace_root: Path | None = None,
) -> Path:
    """校验源目录路径，返回 resolved 绝对路径。

    Args:
        source_dir: 用户提供的路径字符串或 Path。
        workspace_root: LWA 工作区根目录；如果源目录在工作区内则拒绝
            （防止把工作区自身文件当作源导入）。

    Raises:
        FolderSourceError: 路径不存在、不是目录、不可读、或位于工作区内。
    """
    raw = str(source_dir).strip()
    if not raw:
        raise FolderSourceError("源目录路径为空")

    p = Path(raw)
    # 拒绝相对路径歧义：必须在 resolve() 之前判断绝对性——resolve() 恒返回绝对
    # 路径，先 resolve 再判 is_absolute() 是死代码，会放行 './x'、'.' 等相对
    # 路径并解析到服务端 cwd（CHK-166 确认为 minor 缺陷）。
    if not p.is_absolute():
        raise FolderSourceError(
            f"源目录必须是绝对路径：{raw}",
            path=raw,
        )
    try:
        resolved = p.resolve()
    except OSError as exc:
        raise FolderSourceError(
            f"源目录路径无法解析：{raw}",
            path=raw,
        ) from exc

    if not resolved.exists():
        raise FolderSourceError(
            f"源目录不存在：{resolved}",
            path=str(resolved),
        )

    if not resolved.is_dir():
        raise FolderSourceError(
            f"源路径不是目录：{resolved}",
            path=str(resolved),
        )

    # 可读性检查
    try:
        next(resolved.iterdir())
    except PermissionError as exc:
        raise FolderSourceError(
            f"源目录不可读：{resolved}",
            path=str(resolved),
        ) from exc
    except StopIteration:
        pass  # 空目录允许

    # 红线：禁止把工作区自身作为源目录
    if workspace_root is not None:
        try:
            ws_resolved = workspace_root.resolve()
            if resolved == ws_resolved or ws_resolved in resolved.parents:
                raise FolderSourceError(
                    f"源目录不能位于 LWA 工作区内：{resolved}",
                    path=str(resolved),
                    workspace=str(ws_resolved),
                )
        except OSError:
            pass

    return resolved


def pack_source_dir(
    source_dir: Path,
    *,
    dest_zip: Path | None = None,
) -> Path:
    """把源目录内容打包为 zip（只读源目录），返回 zip 路径。

    打包规则：
    - 跳过 ``node_modules`` / ``__pycache__`` / ``.git`` / ``.venv`` 等
      与 IMP-001 剥离规则一致的冗余目录/文件。
    - 不跟随符号链接（记录但不打包 link 本身，防止安全风险）。
    - 如果 ``dest_zip`` 为 None，创建系统临时 zip 文件。

    Args:
        source_dir: 已校验的源目录绝对路径。
        dest_zip: 目标 zip 路径；None 时用临时文件。

    Returns:
        生成的 zip 文件路径。
    """
    if dest_zip is None:
        fd, tmp_path = tempfile.mkstemp(suffix=".zip", prefix="lwa-folder-")
        os.close(fd)
        dest_zip = Path(tmp_path)

    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    file_count = 0

    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(source_dir, followlinks=False):
            # 原地修改 dirs 以跳过冗余目录（os.walk 惯例）
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            root_path = Path(root)

            for fname in files:
                if fname in _SKIP_FILES:
                    continue
                fpath = root_path / fname
                # 跳过符号链接（安全）
                if fpath.is_symlink():
                    continue
                if not fpath.is_file():
                    continue
                # arcname 相对于源目录根。用固定时间戳的 ZipInfo 流式写入，
                # 保证「同内容 → 同 zip 字节流 → 同 hash」（大文件不进内存）。
                arcname = str(fpath.relative_to(source_dir))
                try:
                    zi = zipfile.ZipInfo(arcname, date_time=_ZIP_FIXED_DATE)
                    zi.compress_type = zipfile.ZIP_DEFLATED
                    zi.external_attr = (fpath.stat().st_mode & 0xFFFF) << 16
                    with fpath.open("rb") as src_fh, zf.open(zi, "w") as dst_fh:
                        shutil.copyfileobj(src_fh, dst_fh, length=1024 * 1024)
                    file_count += 1
                except OSError as exc:
                    log.warning("打包时跳过文件 %s：%s", fpath, exc)

    log.info("源目录 %s 已打包为 %s（%d 个文件）", source_dir, dest_zip, file_count)
    return dest_zip


def compute_source_hash(source_dir: Path) -> str:
    """计算源目录的内容指纹（SHA256）。

    指纹基于文件相对路径 + 文件内容的有序哈希，确保：
    - 同内容同 hash（跨平台稳定）。
    - 改一个文件 hash 变。
    - 文件顺序不影响 hash（先排序再哈希）。

    与 zip 的 ``compute_zip_hash`` 语义对齐但独立实现：
    zip hash 是对整个 zip 文件流的 SHA256；文件夹 hash 是对
    目录树的逻辑内容 SHA256。两者不互通，文件夹模式用自己的 hash
    做无变更短路比较（与上次同步时的 ``sourceSyncHash`` 比较）。

    Args:
        source_dir: 已校验的源目录绝对路径。

    Returns:
        64 字符十六进制 SHA256 字符串。
    """
    h = hashlib.sha256()

    for root, dirs, files in os.walk(source_dir, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        root_path = Path(root)

        for fname in sorted(files):
            if fname in _SKIP_FILES:
                continue
            fpath = root_path / fname
            if fpath.is_symlink() or not fpath.is_file():
                continue
            arcname = str(fpath.relative_to(source_dir))
            # 路径参与 hash（文件移动/重命名可感知）
            h.update(arcname.encode("utf-8"))
            h.update(b"\0")
            try:
                with fpath.open("rb") as fh:
                    while True:
                        chunk = fh.read(65536)
                        if not chunk:
                            break
                        h.update(chunk)
            except OSError as exc:
                log.warning("计算指纹时跳过文件 %s：%s", fpath, exc)
            h.update(b"\0")

    return h.hexdigest()


__all__ = [
    "FolderSourceError",
    "validate_source_dir",
    "pack_source_dir",
    "compute_source_hash",
]
