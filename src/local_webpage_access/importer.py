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

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from local_webpage_access.config import Config
from local_webpage_access.errors import LwaError, ZipImportError
from local_webpage_access.logging import get_logger
from local_webpage_access.security import ZipSanitizeResult
from local_webpage_access.models import (
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
from local_webpage_access.paths import Workspace, validate_path_alias
from local_webpage_access.registry import Registry
from local_webpage_access.scanner import DetectionResult, Scanner
from local_webpage_access.zip_processor import (
    compute_zip_hash,
    safe_extract,
    validate_zip,
)

log = get_logger("importer")

_MAX_SLUG_LEN = 40


@dataclass
class ImportResult:
    """导入结果。"""

    instance_id: str
    manifest: InstanceManifest
    detection: DetectionResult
    app_dir: Path
    zip_hash: str
    # IMP-001：剥离摘要。None 表示未经过剥离阶段（如 update_zip 内部复用解压）；
    # 否则为 :class:`~local_webpage_access.security.ZipSanitizeResult`。
    sanitized: ZipSanitizeResult | None = None


@dataclass
class UpdateResult:
    """``update_zip`` 结果（IMP-009）。

    ``skipped`` 与 ``rebuilt`` 互斥：hash 未变化时 ``skipped=True``；
    ``dry_run=True`` 时不落盘，但会按 runtime 预填 ``needs_rebuild`` /
    ``needs_restart``，供 CLI 预告实际更新后的动作。

    ``needs_restart`` 表示调用方（CLI / 管理页）应在更新后调用
    :func:`local_webpage_access.lifecycle.restart_instance`：当且仅当
    ``restart=True``、更新前 ``desiredState=running``、实际发生了替换，
    **且不是容器实例**（静态 / 前端同步 public 即可）。

    ``needs_rebuild``（DEV-067 / BUG-112）：容器实例源码已换，旧镜像失效。
    当且仅当 ``restart=True``、更新前 running、runtime=docker-compose。
    调用方须走 :func:`~local_webpage_access.lifecycle.rebuild_instance`
    （``compose build``），**禁止**轻量 ``restart``（那不会重建镜像，
    会造成「源码已新、镜像仍旧」假绿）。

    update_zip 本身不启动 / 重启 / 重建进程（保持纯数据层、便于测试）；
    但对容器会清空 ``containerId``/``imageId``，使后续 ``lwa start``
    也不会误走轻量 start。端口复用由 hosting 在 rebuild/start 时完成。
    """

    instance_id: str
    manifest: InstanceManifest
    detection: DetectionResult | None
    app_dir: Path
    zip_hash: str
    prev_hash: str | None
    skipped: bool = False
    rebuilt: bool = False
    dry_run: bool = False
    was_running: bool = False
    needs_restart: bool = False
    needs_rebuild: bool = False
    kind_changed: bool = False
    sanitized: ZipSanitizeResult | None = None


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

    def import_zip(
        self,
        zip_path: str | Path,
        *,
        name: str | None = None,
        path_alias: str | None = None,
        on_conflict: str = "rename",
    ) -> ImportResult:
        """导入一个 zip 包，返回 :class:`ImportResult`。

        Args:
            zip_path: zip 文件路径。
            name: 可选的显示名称；不提供时从 zip 文件名推导。
            path_alias: 可选的路径别名 slug（IMP-006）。提供时校验格式、
                保留字与全局唯一性；仅对识别为 ``shared-static`` 的实例生效，
                容器实例会拒绝并报错。未提供时默认行为与 V1 完全一致。
            on_conflict: slug 冲突策略。``"rename"``（默认，daemon 友好）按
                ``-2`` / ``-3`` 自动改名新建；``"error"``（IMP-009 CLI）直接报错
                并建议改用 ``--update``，避免无脑新建历史误导入实例。

        Raises:
            ZipImportError: zip 不存在、格式非法、路径穿越、解压失败、
                或 ``on_conflict="error"`` 时 slug 已被占用。
            PathError: 路径别名格式非法、命中保留字或已被占用。
        """
        src = Path(zip_path).resolve()
        validate_zip(src)
        zip_hash = compute_zip_hash(src)

        base = name if name else src.stem
        slug = slugify(base)
        display_name = name if name else titleize(slug)

        # IMP-009：CLI 路径下 slug 冲突不再 silent 建 -2，提示用 --update。
        # daemon 路径（默认 rename）保持原自动改名行为，避免 watcher 误报错。
        if on_conflict == "error" and self._id_taken(slug):
            raise ZipImportError(
                f"实例 {slug} 已存在。如需更新该实例，请使用："
                f"lwa import <zip> --update {slug}；"
                f"如需另建新实例，请用 --name 指定不同的名称。",
                instance_id=slug,
            )
        # IMP-006：路径别名在写盘前校验，避免半成品写入后才发现冲突。
        if path_alias is not None:
            existing = set(self.registry.list_route_hosts().keys())
            validate_path_alias(path_alias, existing_aliases=existing)
            log.info("路径别名 %s 已校验通过", path_alias)

        # BUG-127：目录本身作为原子 claim。仅先查再 mkdir 会让并发导入拿到同一 slug。
        instance_id = self._claim_unique_id(slug)
        if path_alias is not None:
            log.info("路径别名 %s 将写入实例 %s", path_alias, instance_id)

        log.info("开始导入 %s → 实例 %s（sha256=%s）", src, instance_id, zip_hash[:12])

        app_dir = self.ws.app_dir(instance_id)

        try:
            # 保存原始 zip
            shutil.copy2(src, self.ws.app_original_zip(instance_id))

            # 安全解压到 current/（IMP-001：先剥离冗余成员，再审计与解压）
            current_dir = self.ws.app_current(instance_id)
            sanitized = safe_extract(src, current_dir)

            # 扫描识别
            detection = self.scanner.detect(current_dir)

            # IMP-006：路径别名当前仅支持静态实例；容器实例的别名路由需要容器
            # 托管路径额外生成 reverse_proxy 片段，V1 暂不支持，明确拒绝而非静默忽略。
            if path_alias is not None and detection.runtime != Runtime.SHARED_STATIC:
                raise ZipImportError(
                    f"路径别名仅支持静态站点，该实例被识别为 {detection.form}（"
                    f"{detection.runtime}）；请去掉 --path-alias 或仅对静态站点使用",
                    instance_id=instance_id,
                )

            # 构建 manifest
            manifest = self._build_manifest(
                instance_id, display_name, zip_hash, detection, path_alias=path_alias
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
            # IMP-015：检测到业务 .env.example 时登记事件，提示用户在部署后填写密钥。
            if (current_dir / ".env.example").is_file():
                self.registry.add_event(
                    instance_id,
                    "env_example_detected",
                    "检测到 .env.example：部署后复制为 docker/.env.example，"
                    "业务密钥请填入 docker/.env.local（compose 自动注入）",
                )
            # IMP-001：剥离摘要登记为可审计事件（仅当实际剥离了成员时）
            if sanitized is not None and sanitized.stripped_names:
                parts = ", ".join(
                    f"{rule}×{n}" for rule, n in sorted(
                        sanitized.categories.items(), key=lambda kv: -kv[1]
                    )
                )
                self.registry.add_event(
                    instance_id,
                    "security",
                    (
                        f"剥离冗余成员 {len(sanitized.stripped_names)} 项"
                        f"（含 symlink {sanitized.stripped_symlink_count}）：{parts}"
                    ),
                )
            # WBS-25.09：未知 zip 来源风险提示（仅 pending 时）
            if detection.pending:
                from local_webpage_access.security import unknown_zip_risk_hint

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
                sanitized=sanitized,
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

    # ---- 原地更新（IMP-009）-------------------------------------------------

    def update_zip(
        self,
        zip_path: str | Path,
        instance_id: str,
        *,
        restart: bool = True,
        keep_data: bool = True,
        yes: bool = False,  # noqa: ARG002 — 交互确认由 CLI 层处理；数据层非交互
        dry_run: bool = False,
        force_kind_change: bool = False,
    ) -> UpdateResult:
        """用新 zip 原地更新已存在的实例（IMP-009）。

        在保留 ``instance_id`` / ``hostPort``（端口登记不动）/ ``data/`` /
        ``desiredState`` / IMP-006 路径别名的前提下，覆盖 ``current/`` 业务源码、
        刷新 ``sourceZipHash`` 与扫描结果，让用户感知为「同一网页更新了」。

        流程：
        1. 校验 zip 与目标实例存在；
        2. 计算新 hash 与 ``sourceZipHash`` 比较 —— 相同则跳过（``skipped=True``）；
        3. ``dry_run`` 时仅解压到系统临时目录、扫描、报告差异，不触碰工作区；
        4. 持 :func:`~local_webpage_access.lifecycle.instance_lock` 期间：
           - 解压到 ``current.new/`` 暂存区（current/ 原封不动）；
           - 重新扫描；kind/runtime 变化时拒绝（除非 ``force_kind_change``）；
           - ``data/`` 位于 ``current/`` 外，默认保留；``keep_data=False`` 时清空；
           - 备份 ``original.zip`` → ``original.zip.bak``；
           - 原子换入（rename current → current.old、staging → current、删 old），
             失败回滚；
           - 重建 manifest（保留 id/createdAt/desiredState/status/路径别名），
             刷 ``sourceZipHash`` / ``updatedAt``，registry 同步 + 事件。

        本方法不启动 / 重启 / 重建进程；``needs_rebuild=True`` 时由调用方执行
        :func:`lifecycle.rebuild_instance`，``needs_restart=True`` 时执行
        :func:`lifecycle.restart_instance`。hostPort 由 hosting 在重启时复用。

        Raises:
            ZipImportError: zip 非法 / 实例不存在 / 形态变化被拒绝 / 解压失败。
        """
        src = Path(zip_path).resolve()
        validate_zip(src)
        new_hash = compute_zip_hash(src)

        if not self.registry.instance_exists(instance_id):
            raise ZipImportError(
                f"实例 {instance_id} 不存在，无法更新；如需新建请去掉 --update",
                instance_id=instance_id,
            )

        manifest_path = self.ws.app_manifest_path(instance_id)
        if not manifest_path.is_file():
            raise ZipImportError(
                f"实例 {instance_id} 缺少 local-web.json，无法更新",
                instance_id=instance_id,
            )
        old_manifest = InstanceManifest.load(manifest_path)
        old_hash = getattr(old_manifest, "sourceZipHash", None)
        was_running = old_manifest.desiredState == DesiredState.RUNNING
        app_dir = self.ws.app_dir(instance_id)

        # 2. hash 未变化 → 跳过
        if new_hash == old_hash:
            log.info(
                "实例 %s 的 zip 未变化（sha256=%s），跳过更新", instance_id, new_hash[:12]
            )
            self.registry.add_event(
                instance_id,
                "update",
                f"zip 未变化（sha256={new_hash[:12]}），跳过更新",
            )
            return UpdateResult(
                instance_id=instance_id,
                manifest=old_manifest,
                detection=None,
                app_dir=app_dir,
                zip_hash=new_hash,
                prev_hash=old_hash,
                skipped=True,
                was_running=was_running,
                needs_restart=False,
            )

        # 3. dry-run：解压到系统临时目录、扫描、报告，不写工作区
        if dry_run:
            detection = None
            sanitized: ZipSanitizeResult | None = None
            kind_changed = False
            with tempfile.TemporaryDirectory(prefix="lwa-update-dryrun-") as tmp:
                staging_tmp = Path(tmp)
                sanitized = safe_extract(src, staging_tmp)
                detection = self.scanner.detect(staging_tmp)
                kind_changed = self._kind_changed(old_manifest, detection)
            # 预告实际更新后的动作（与落盘路径一致：容器 rebuild，静态/前端 restart）
            is_container = old_manifest.runtime.value == "docker-compose"
            dry_needs_rebuild = bool(restart and was_running and is_container)
            dry_needs_restart = bool(restart and was_running and not is_container)
            log.info(
                "实例 %s dry-run：sha256 %s → %s，形态变化=%s，needs_rebuild=%s",
                instance_id,
                (old_hash[:12] if old_hash else "∅"),
                new_hash[:12],
                kind_changed,
                dry_needs_rebuild,
            )
            return UpdateResult(
                instance_id=instance_id,
                manifest=old_manifest,
                detection=detection,
                app_dir=app_dir,
                zip_hash=new_hash,
                prev_hash=old_hash,
                skipped=False,
                rebuilt=False,
                dry_run=True,
                was_running=was_running,
                needs_restart=dry_needs_restart,
                needs_rebuild=dry_needs_rebuild,
                kind_changed=kind_changed,
                sanitized=sanitized,
            )

        # 4. 持锁执行原子换入
        from local_webpage_access.lifecycle import instance_lock

        with instance_lock(self.ws, instance_id):
            current_dir = self.ws.app_current(instance_id)
            parent = current_dir.parent
            staging = parent / f"{current_dir.name}.new"
            old_current = parent / f"{current_dir.name}.old"
            manifest_snapshot = manifest_path.read_bytes()
            old_resources = self.registry.get_resources(instance_id)
            old_port_rows = (
                self.registry.get_static_site(instance_id),
                self.registry.get_container(instance_id),
            )
            old_host_port = next(
                (
                    int(row["host_port"])
                    for row in old_port_rows
                    if row and row.get("host_port")
                ),
                None,
            )
            current_swapped = False

            # 清理可能残留的暂存区
            for stale in (staging, old_current):
                if stale.exists():
                    shutil.rmtree(stale, ignore_errors=True)

            try:
                # 解压到暂存区 + 重扫
                sanitized = safe_extract(src, staging)
                detection = self.scanner.detect(staging)
                kind_changed = self._kind_changed(old_manifest, detection)

                # kind/runtime 变化拒绝（首版）
                if not force_kind_change and kind_changed:
                    raise ZipImportError(
                        f"新 zip 的形态发生变化（"
                        f"{old_manifest.kind.value}/{old_manifest.runtime.value}"
                        f" → {detection.kind}/{detection.runtime}），"
                        f"首版不支持跨形态原地更新；请改用普通 import 新建实例，"
                        f"或加 --force-kind-change 确认强制迁移",
                        instance_id=instance_id,
                    )

                # BUG-124：跨形态 upsert 会删除旧 static_sites/containers 子表，
                # 必须在换表前按旧 manifest 停掉 runtime。即使 desired=stopped 也尝试，
                # 以清理历史遗留的存活进程；业务停止失败只警告，不阻断从未托管实例更新。
                if force_kind_change and kind_changed:
                    try:
                        from local_webpage_access.hosting import stop_instance

                        stop_instance(self.ws, self.config, self.registry, instance_id)
                    except LwaError as exc:
                        log.warning(
                            "实例 %s 跨形态更新前停止旧 runtime 失败（继续更新）：%s",
                            instance_id,
                            exc,
                        )

                # 备份 original.zip
                orig_zip = self.ws.app_original_zip(instance_id)
                if orig_zip.exists():
                    shutil.copy2(orig_zip, orig_zip.with_suffix(".zip.bak"))

                # 原子换入：current → old、staging → current；失败回滚
                os.replace(str(current_dir), str(old_current))
                try:
                    os.replace(str(staging), str(current_dir))
                except OSError:
                    # 回滚 current/
                    shutil.rmtree(current_dir, ignore_errors=True)
                    os.replace(str(old_current), str(current_dir))
                    raise
                current_swapped = True

                # 重建 manifest：保留 id/createdAt/desiredState/status/路径别名
                manifest = apply_detection_to_manifest(
                    old_manifest, detection, self.ws
                )
                manifest.sourceZipHash = new_hash  # type: ignore[attr-defined]
                manifest.desiredState = old_manifest.desiredState
                manifest.status = old_manifest.status
                # IMP-006：路径别名是用户/CLI 选择，不从 zip 推导，必须保留
                if (
                    old_manifest.static is not None
                    and old_manifest.static.routeMode == "name"
                    and old_manifest.static.routeHost
                    and manifest.static is not None
                ):
                    manifest.static.routeMode = "name"
                    manifest.static.routeHost = old_manifest.static.routeHost
                # 保留端口登记：从旧 registry 行读 hostPort 写回 manifest，
                # 避免 upsert_from_manifest 用 manifest 的空 hostPort 清零登记
                # （hosting 重启时靠 static_sites/containers 表复用端口）
                self._preserve_hostport(manifest, instance_id)
                # BUG-124：stop_instance 会按旧 manifest 回写 registry；当端口只存在
                # 于 registry、尚未同步到 manifest 时会被清空，因此用停止前快照兜底。
                if old_host_port is not None:
                    if manifest.static is not None:
                        manifest.static.hostPort = old_host_port
                    elif manifest.container is not None:
                        manifest.container.hostPort = old_host_port
                # DEV-067 / BUG-112：容器源码已换 → 作废旧部署标记，避免
                # restart/start 走轻量 compose start 继续跑旧镜像。
                if (
                    manifest.runtime.value == "docker-compose"
                    and manifest.container is not None
                ):
                    manifest.container.containerId = None
                    manifest.container.imageId = None
                manifest.touch()
                manifest.save(manifest_path)

                # 覆盖 original.zip（备份已在上面完成）
                shutil.copy2(src, orig_zip)

                # keep_data=False：清空持久 data/（apps/<id>/data/，在 current/ 之外，
                # 默认不动；仅在用户显式 --no-keep-data 时清空，作为「重置数据」语义）。
                # 必须早于资源统计写入，否则管理页会继续显示清空前的 data/ 大小。
                if not keep_data:
                    persistent_data = self.ws.app_data(instance_id)
                    if persistent_data.exists():
                        shutil.rmtree(persistent_data, ignore_errors=True)
                    persistent_data.mkdir(parents=True, exist_ok=True)

                # registry 同步
                self.registry.upsert_from_manifest(
                    manifest,
                    app_path=str(current_dir),
                    source_zip_path=str(orig_zip),
                )
                self.registry.upsert_resources(
                    instance_id,
                    source_size_bytes=_dir_size(current_dir),
                    data_size_bytes=_dir_size(self.ws.app_data(instance_id)),
                )
                event_msg = (
                    f"zip 已更新（sha256 "
                    f"{(old_hash[:12] if old_hash else '∅')} → {new_hash[:12]}"
                    f"），识别为 {detection.form}"
                )
                self.registry.add_event(instance_id, "update", event_msg)
                if sanitized is not None and sanitized.stripped_names:
                    parts = ", ".join(
                        f"{rule}×{n}"
                        for rule, n in sorted(
                            sanitized.categories.items(), key=lambda kv: -kv[1]
                        )
                    )
                    self.registry.add_event(
                        instance_id,
                        "security",
                        f"更新剥离冗余成员 {len(sanitized.stripped_names)} 项"
                        f"（含 symlink {sanitized.stripped_symlink_count}）：{parts}",
                    )
            except ZipImportError:
                # 已是规范错误（形态变化 / zip 非法等），原样抛出；finally 清理暂存区
                raise
            except Exception as exc:
                if current_swapped:
                    self._rollback_swapped_current(
                        instance_id=instance_id,
                        current_dir=current_dir,
                        old_current=old_current,
                        manifest_path=manifest_path,
                        manifest_snapshot=manifest_snapshot,
                        old_manifest=old_manifest,
                        orig_zip=orig_zip,
                        orig_zip_bak=orig_zip.with_suffix(".zip.bak"),
                        old_resources=old_resources,
                    )
                # 失败时清理暂存区；current/ 已通过原子换入保护未被破坏
                # （换入前异常 current/ 原封未动；换入后异常也已回滚）
                log.error("更新实例 %s 失败：%s", instance_id, exc)
                raise ZipImportError(
                    f"更新失败：{exc}", instance_id=instance_id
                ) from exc
            finally:
                for stale in (staging, old_current):
                    if stale.exists():
                        shutil.rmtree(stale, ignore_errors=True)

        # 容器必须 rebuild 镜像；静态/前端只需 restart 同步 public。
        is_container = (
            manifest is not None  # type: ignore[possibly-undefined]
            and manifest.runtime.value == "docker-compose"  # type: ignore[possibly-undefined]
        )
        needs_rebuild = bool(restart and was_running and is_container)
        needs_restart = bool(restart and was_running and not is_container)
        log.info(
            "实例 %s 更新成功（sha256 %s → %s，needs_rebuild=%s，needs_restart=%s）",
            instance_id,
            (old_hash[:12] if old_hash else "∅"),
            new_hash[:12],
            needs_rebuild,
            needs_restart,
        )
        return UpdateResult(
            instance_id=instance_id,
            manifest=manifest,  # type: ignore[possibly-undefined]
            detection=detection,  # type: ignore[possibly-undefined]
            app_dir=app_dir,
            zip_hash=new_hash,
            prev_hash=old_hash,
            skipped=False,
            rebuilt=True,
            was_running=was_running,
            needs_restart=needs_restart,
            needs_rebuild=needs_rebuild,
            kind_changed=kind_changed,  # type: ignore[possibly-undefined]
            sanitized=sanitized,  # type: ignore[possibly-undefined]
        )

    @staticmethod
    def _kind_changed(
        old: InstanceManifest, detection: DetectionResult
    ) -> bool:
        """新扫描结果与旧 manifest 的 kind/runtime 是否不一致。

        pending（未识别）视为可更新（沿用 static 草稿），不算形态变化。
        """
        if detection.pending or detection.kind is None:
            return False
        old_kind = old.kind.value if hasattr(old.kind, "value") else old.kind
        old_rt = (
            old.runtime.value if hasattr(old.runtime, "value") else old.runtime
        )
        new_rt = (
            detection.runtime.value
            if hasattr(detection.runtime, "value")
            else detection.runtime
        )
        return detection.kind != old_kind or new_rt != old_rt

    def _preserve_hostport(
        self, manifest: InstanceManifest, instance_id: str
    ) -> None:
        """把 registry 中已登记的 hostPort 回填到 manifest（IMP-009）。

        ``apply_detection_to_manifest`` 重建出的 manifest 其 static/container
        的 hostPort 为空（hosting 尚未跑），若直接 upsert_from_manifest 会用空值
        清零 registry 的端口登记，破坏重启时的端口复用。这里从旧 registry 行读
        回 hostPort 写入 manifest，使 upsert 保持登记不变。``force_kind_change``
        跨形态迁移时，旧端口可能在另一张子表中，因此先查新形态对应表，再回退
        到旧形态表。
        """
        if manifest.static is not None:
            rows = (
                self.registry.get_static_site(instance_id),
                self.registry.get_container(instance_id),
            )
            for row in rows:
                if row and row.get("host_port"):
                    manifest.static.hostPort = int(row["host_port"])
                    return
        elif manifest.container is not None:
            rows = (
                self.registry.get_container(instance_id),
                self.registry.get_static_site(instance_id),
            )
            for row in rows:
                if row and row.get("host_port"):
                    manifest.container.hostPort = int(row["host_port"])
                    return

    def _rollback_swapped_current(
        self,
        *,
        instance_id: str,
        current_dir: Path,
        old_current: Path,
        manifest_path: Path,
        manifest_snapshot: bytes,
        old_manifest: InstanceManifest,
        orig_zip: Path,
        orig_zip_bak: Path,
        old_resources: dict[str, object] | None,
    ) -> None:
        """在 current/ 已换入后恢复旧源码与关键元数据（BUG-056）。"""
        try:
            shutil.rmtree(current_dir, ignore_errors=True)
            if old_current.exists():
                os.replace(str(old_current), str(current_dir))
        except OSError as rollback_exc:
            log.error("回滚实例 %s 的 current/ 失败：%s", instance_id, rollback_exc)

        try:
            manifest_path.write_bytes(manifest_snapshot)
        except OSError as rollback_exc:
            log.error("回滚实例 %s 的 manifest 失败：%s", instance_id, rollback_exc)

        try:
            if orig_zip_bak.is_file():
                shutil.copy2(orig_zip_bak, orig_zip)
        except OSError as rollback_exc:
            log.error("回滚实例 %s 的 original.zip 失败：%s", instance_id, rollback_exc)

        try:
            self.registry.upsert_from_manifest(
                old_manifest,
                app_path=str(current_dir),
                source_zip_path=str(orig_zip),
            )
            if old_resources is not None:
                self.registry.upsert_resources(
                    instance_id,
                    source_size_bytes=old_resources.get("source_size_bytes"),  # type: ignore[arg-type]
                    public_size_bytes=old_resources.get("public_size_bytes"),  # type: ignore[arg-type]
                    data_size_bytes=old_resources.get("data_size_bytes"),  # type: ignore[arg-type]
                    image_size_bytes=old_resources.get("image_size_bytes"),  # type: ignore[arg-type]
                    last_memory_bytes=old_resources.get("last_memory_bytes"),  # type: ignore[arg-type]
                    last_cpu_percent=old_resources.get("last_cpu_percent"),  # type: ignore[arg-type]
                )
        except Exception as rollback_exc:  # noqa: BLE001 — 回滚失败应记录原始错误继续抛出
            log.error("回滚实例 %s 的 registry 失败：%s", instance_id, rollback_exc)

    # ---- id 冲突处理 --------------------------------------------------------

    def _claim_unique_id(self, base_slug: str) -> str:
        """原子占用实例目录，避免并发导入同一 slug（BUG-127）。"""
        candidate = base_slug
        n = 2
        while True:
            if self.registry.instance_exists(candidate):
                candidate = f"{base_slug}-{n}"
                n += 1
                continue
            try:
                self.ws.app_dir(candidate).mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                candidate = f"{base_slug}-{n}"
                n += 1
                continue
            self.ws.ensure_app_dirs(candidate)
            return candidate

    def _resolve_unique_id(self, base_slug: str) -> str:
        """兼容旧调用：仅解析可用 ID，不占用目录。"""
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

    # ---- manifest 构建 ------------------------------------------------------

    def _build_manifest(
        self,
        instance_id: str,
        display_name: str,
        zip_hash: str,
        detection: DetectionResult,
        *,
        path_alias: str | None = None,
    ) -> InstanceManifest:
        return build_manifest_from_detection(
            instance_id=instance_id,
            display_name=display_name,
            detection=detection,
            workspace=self.ws,
            zip_hash=zip_hash,
            path_alias=path_alias,
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
    path_alias: str | None = None,
) -> InstanceManifest:
    """根据扫描结果构造一个完整且 schema 一致的 :class:`InstanceManifest`。

    被 :class:`Importer` 导入流程与 ``lwa scan`` 重扫流程共用，
    确保 static ↔ container 配置始终与 runtime 匹配。

    ``path_alias`` 非 ``None`` 时（IMP-006）写入静态配置的 ``routeMode="name"``
    + ``routeHost=<alias>``；仅对 ``shared-static`` runtime 有意义。
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
        static_kwargs: dict = {}
        if path_alias is not None:
            # IMP-006：路径别名写入 static.routeMode/routeHost。
            static_kwargs["routeMode"] = "name"
            static_kwargs["routeHost"] = path_alias
        kwargs["static"] = StaticConfig(**static_kwargs)
    elif runtime == Runtime.DOCKER_COMPOSE:
        # IMP-018（WBS-20260708 阶段2.4）：resourceProfile → mem/cpus 映射注入
        # container.resourceLimits，compose 的 ${MEMORY_LIMIT}/${CPU_LIMIT} 据此生效，
        # 不再恒为默认 512m（runtime §4.2-P8）。
        from local_webpage_access.resource_profiles import profile_to_limits

        kwargs["container"] = ContainerConfig(
            projectName=f"lwa-{instance_id}",
            internalPort=detection.internalPort or 8000,
            composePath=str(workspace.app_compose_path(instance_id)),
            dockerfilePath=str(workspace.app_dockerfile_path(instance_id)),
            resourceLimits=profile_to_limits(resource_profile),
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
    "UpdateResult",
    "slugify",
    "titleize",
    "build_manifest_from_detection",
    "apply_detection_to_manifest",
]
