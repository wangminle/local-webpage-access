"""zip 导入与实例目录管理（WBS-07）。

职责：
1. 校验 zip 文件存在且格式合法。
2. 计算 zip 的 SHA256 摘要。
3. 由文件名生成 instance id（slug），处理同名冲突。
4. 创建 ``apps/<id>/`` 完整目录结构。
5. 保存 ``source/original.zip``，安全解压到 ``current/``。
6. 防御 zip slip（路径穿越）。
7. 处理 zip 内单层根目录（自动拍平）。
8. 调用扫描器识别运行形态，写入初始 ``local-web.json``。
9. 在 registry 登记实例与导入事件。
10. 失败时清理半成品目录，或把实例标记为 failed。

对应 V1 设计说明第 9 节。
"""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from local_web_access.config import Config
from local_web_access.errors import ZipImportError
from local_web_access.logging import get_logger, now_iso
from local_web_access.models import (
    ContainerConfig,
    DesiredState,
    InstanceManifest,
    Kind,
    ResourceProfile,
    Runtime,
    ServingMode,
    StaticConfig,
    Status,
)
from local_web_access.paths import Workspace
from local_web_access.registry import Registry
from local_web_access.scanner import DetectionResult, Scanner

log = get_logger("importer")

_HASH_CHUNK = 64 * 1024
_MAX_SLUG_LEN = 40


@dataclass
class ImportResult:
    """导入结果。"""

    instance_id: str
    manifest: InstanceManifest
    detection: DetectionResult
    app_dir: Path
    zip_hash: str


# ---- slug 工具 --------------------------------------------------------------


def slugify(text: str) -> str:
    """把任意文本转成合法的 instance id slug。

    规则：小写 → 非字母数字替换为连字符 → 折叠连续连字符 → 去首尾连字符。
    结果为空时返回 ``"instance"``。
    """
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    s = re.sub(r"-{2,}", "-", s)
    if len(s) > _MAX_SLUG_LEN:
        s = s[:_MAX_SLUG_LEN].rstrip("-")
    return s or "instance"


def titleize(slug: str) -> str:
    """把 slug 转成人类可读名称：``my-demo`` → ``My Demo``。"""
    return " ".join(part.capitalize() for part in slug.split("-") if part) or "Instance"


# ---- Importer ---------------------------------------------------------------


class Importer:
    """zip 导入器。"""

    def __init__(
        self,
        workspace: Workspace,
        config: Config,
        registry: Registry,
        *,
        scanner: Scanner | None = None,
    ) -> None:
        self.ws = workspace
        self.config = config
        self.registry = registry
        self.scanner = scanner or Scanner()

    # ---- 主入口 ------------------------------------------------------------

    def import_zip(self, zip_path: str | Path, *, name: str | None = None) -> ImportResult:
        """导入一个 zip 包，返回 :class:`ImportResult`。

        Args:
            zip_path: zip 文件路径。
            name: 可选的显示名称；不提供时从 zip 文件名推导。

        Raises:
            ZipImportError: zip 不存在、格式非法、路径穿越或解压失败。
        """
        src = Path(zip_path).resolve()
        self._validate_zip(src)
        zip_hash = self._compute_hash(src)

        base = name if name else src.stem
        slug = slugify(base)
        display_name = name if name else titleize(slug)
        instance_id = self._resolve_unique_id(slug)

        log.info("开始导入 %s → 实例 %s（sha256=%s）", src, instance_id, zip_hash[:12])

        # 创建目录结构
        self.ws.ensure_app_dirs(instance_id)
        app_dir = self.ws.app_dir(instance_id)

        try:
            # 保存原始 zip
            shutil.copy2(src, self.ws.app_original_zip(instance_id))

            # 安全解压到 current/
            current_dir = self.ws.app_current(instance_id)
            self._safe_extract(src, current_dir)

            # 扫描识别
            detection = self.scanner.detect(current_dir)

            # 构建 manifest
            manifest = self._build_manifest(
                instance_id, display_name, zip_hash, detection
            )
            manifest.save(self.ws.app_manifest_path(instance_id))

            # 登记 registry
            self.registry.upsert_from_manifest(
                manifest,
                app_path=str(current_dir),
                source_zip_path=str(self.ws.app_original_zip(instance_id)),
            )
            source_size = _dir_size(current_dir)
            # data/ 在导入时尚为空；data_size_bytes 记录 data/ 目录真实大小，
            # 不要把 zip 体积写进这一列（列语义为 data/ 目录，WBS-19.08）
            data_size = _dir_size(self.ws.app_data(instance_id))
            self.registry.upsert_resources(
                instance_id,
                source_size_bytes=source_size,
                data_size_bytes=data_size,
            )
            event_msg = (
                f"导入完成，sha256={zip_hash[:12]}，识别为 {detection.form}"
                if not detection.pending
                else f"导入完成，sha256={zip_hash[:12]}，未识别（pending）"
            )
            self.registry.add_event(instance_id, "import", event_msg)
            # WBS-25.09：未知 zip 来源风险提示（仅 pending 时）
            if detection.pending:
                from local_web_access.security import unknown_zip_risk_hint

                self.registry.add_event(
                    instance_id, "security", unknown_zip_risk_hint()
                )

            log.info("导入成功：%s（%s）", instance_id, detection.form)
            return ImportResult(
                instance_id=instance_id,
                manifest=manifest,
                detection=detection,
                app_dir=app_dir,
                zip_hash=zip_hash,
            )
        except Exception as exc:
            log.error("导入 %s 失败，清理半成品：%s", instance_id, exc)
            self._cleanup_failed(instance_id)
            if isinstance(exc, ZipImportError):
                raise
            raise ZipImportError(
                f"导入失败：{exc}",
                instance_id=instance_id,
            ) from exc

    # ---- 校验 ---------------------------------------------------------------

    def _validate_zip(self, zip_path: Path) -> None:
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

    def _compute_hash(self, zip_path: Path) -> str:
        h = hashlib.sha256()
        with zip_path.open("rb") as fh:
            while True:
                chunk = fh.read(_HASH_CHUNK)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    # ---- id 冲突处理 --------------------------------------------------------

    def _resolve_unique_id(self, base_slug: str) -> str:
        candidate = base_slug
        n = 2
        while self._id_taken(candidate):
            candidate = f"{base_slug}-{n}"
            n += 1
        return candidate

    def _id_taken(self, instance_id: str) -> bool:
        if self.registry.instance_exists(instance_id):
            return True
        return self.ws.app_dir(instance_id).exists()

    # ---- 安全解压 -----------------------------------------------------------

    def _safe_extract(self, zip_path: Path, target: Path) -> None:
        """带 zip slip 防护与单层根目录拍平的解压。"""
        target.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="lwa-import-") as tmp:
            tmp_path = Path(tmp).resolve()

            with zipfile.ZipFile(zip_path) as zf:
                # zip slip 检查：每个成员必须在 tmp_path 之内
                for member in zf.infolist():
                    member_target = (tmp_path / member.filename).resolve()
                    try:
                        member_target.relative_to(tmp_path)
                    except ValueError:
                        raise ZipImportError(
                            f"检测到路径穿越（zip slip）：{member.filename}",
                            member=member.filename,
                        )
                zf.extractall(tmp_path)

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

    # ---- manifest 构建 ------------------------------------------------------

    def _build_manifest(
        self,
        instance_id: str,
        display_name: str,
        zip_hash: str,
        detection: DetectionResult,
    ) -> InstanceManifest:
        return build_manifest_from_detection(
            instance_id=instance_id,
            display_name=display_name,
            detection=detection,
            workspace=self.ws,
            zip_hash=zip_hash,
        )

    # ---- 失败清理 -----------------------------------------------------------

    def _cleanup_failed(self, instance_id: str) -> None:
        app_dir = self.ws.app_dir(instance_id)
        if app_dir.exists():
            shutil.rmtree(app_dir, ignore_errors=True)
        try:
            if self.registry.instance_exists(instance_id):
                self.registry.delete_instance(instance_id)
        except Exception:  # noqa: BLE001 — 清理时不应再抛
            log.warning("清理 registry 记录 %s 失败", instance_id)


# ---- 辅助 -------------------------------------------------------------------


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                continue
    return total


def build_manifest_from_detection(
    *,
    instance_id: str,
    display_name: str,
    detection: DetectionResult,
    workspace: Workspace,
    zip_hash: str | None = None,
) -> InstanceManifest:
    """根据扫描结果构造一个完整且 schema 一致的 :class:`InstanceManifest`。

    被 :class:`Importer` 导入流程与 ``lwa scan`` 重扫流程共用，
    确保 static ↔ container 配置始终与 runtime 匹配。
    """
    if detection.pending or detection.kind is None:
        # 未识别：以 static 草稿落盘，标记 pending
        kind = Kind.STATIC
        runtime = Runtime.SHARED_STATIC
        serving_mode = ServingMode.SHARED_STATIC
        resource_profile = ResourceProfile.TINY
        last_error = "; ".join(detection.notes) if detection.notes else "未识别项目类型"
    else:
        kind = detection.kind
        runtime = detection.runtime  # type: ignore[assignment]
        serving_mode = detection.servingMode  # type: ignore[assignment]
        resource_profile = detection.resourceProfile
        last_error = None

    kwargs: dict = dict(
        id=instance_id,
        name=display_name,
        version="1",
        kind=kind,
        runtime=runtime,
        servingMode=serving_mode,
        resourceProfile=resource_profile,
        stack=detection.stack,
        hasDatabase=detection.hasDatabase,
        database=detection.database,
        desiredState=DesiredState.STOPPED,
        status=Status.PENDING,
        entry=detection.entry,
        sourceZipPath=str(workspace.app_original_zip(instance_id)),
        appPath=str(workspace.app_current(instance_id)),
        lastError=last_error,
    )

    if runtime == Runtime.SHARED_STATIC:
        kwargs["static"] = StaticConfig()
    elif runtime == Runtime.DOCKER_COMPOSE:
        kwargs["container"] = ContainerConfig(
            projectName=f"lwa-{instance_id}",
            internalPort=detection.internalPort or 8000,
            composePath=str(workspace.app_compose_path(instance_id)),
            dockerfilePath=str(workspace.app_dockerfile_path(instance_id)),
        )

    manifest = InstanceManifest(**kwargs)
    if zip_hash:
        manifest.sourceZipHash = zip_hash  # type: ignore[attr-defined]
    manifest.network.internalPort = detection.internalPort
    manifest.touch()
    return manifest


def apply_detection_to_manifest(
    manifest: InstanceManifest,
    detection: DetectionResult,
    workspace: Workspace,
) -> InstanceManifest:
    """把新的扫描结果应用到已存在的 manifest（用于 ``lwa scan`` 重扫）。

    会正确处理 static ↔ container 配置的切换，保持 schema 一致性。
    保留 id/name/version/sourceZipPath/appPath 等既有字段。
    """
    fresh = build_manifest_from_detection(
        instance_id=manifest.id,
        display_name=manifest.name,
        detection=detection,
        workspace=workspace,
        zip_hash=getattr(manifest, "sourceZipHash", None),
    )
    # 保留版本号与原始 zip 路径（重扫不应改变这些）
    fresh.version = manifest.version
    fresh.sourceZipPath = manifest.sourceZipPath
    fresh.appPath = manifest.appPath
    fresh.createdAt = manifest.createdAt
    return fresh


__all__ = [
    "Importer",
    "ImportResult",
    "slugify",
    "titleize",
    "build_manifest_from_detection",
    "apply_detection_to_manifest",
]
