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

import contextlib
import html as html_lib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from local_webpage_access.config import Config
from local_webpage_access.errors import LwaError, ZipImportError
from local_webpage_access.import_activity import import_activity_lock
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


def _coerce_host_port(value: object) -> int:
    """把 registry 行里的 host_port（object）收窄为 int，满足 mypy（CHK-115）。"""
    if isinstance(value, bool):
        raise TypeError(f"host_port 不能是 bool：{value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        return int(value.strip())
    raise TypeError(f"无法将 host_port 转为 int：{type(value).__name__}")


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


def slug_basis_for_id(*, name: str | None, path_stem: str) -> str:
    """选择用于生成 instance id 的原始文本。

    显示名可以是纯中文，但 id 必须是 ASCII slug。若 ``name`` 不含任何
    ``[a-zA-Z0-9]``（slugify 会落到万能回退 ``instance``），改用路径 stem
    （zip 文件名 / 文件夹名），避免多次中文导入全部撞在 ``instance`` 上。
    """
    if name and re.search(r"[a-zA-Z0-9]", name):
        return name
    stem = (path_stem or "").strip()
    if stem:
        return stem
    return name or "instance"


def titleize(slug: str) -> str:
    """把 slug 转成人类可读名称：``my-demo`` → ``My Demo``。"""
    return " ".join(part.capitalize() for part in slug.split("-") if part) or "Instance"


_TITLE_RE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_MAX_DISPLAY_NAME_LEN = 200


def extract_html_title(html: str) -> str | None:
    """从 HTML 文本提取 ``<title>`` 纯文本；缺失/空白返回 ``None``。

    ``<title>`` 为 RCDATA：须先去掉源码中的真实标签，再 ``html.unescape``，
    否则 ``&lt;Admin&gt;`` 会先变成 ``<Admin>`` 再被当标签删掉（BUG-436）。
    """
    match = _TITLE_RE.search(html)
    if match is None:
        return None
    raw = match.group(1)
    text = re.sub(r"<[^>]+>", "", raw)
    text = html_lib.unescape(text)
    text = " ".join(text.split())
    if not text:
        return None
    if len(text) > _MAX_DISPLAY_NAME_LEN:
        return text[:_MAX_DISPLAY_NAME_LEN].rstrip()
    return text


def find_homepage_index(project_dir: Path, *, max_depth: int = 3) -> Path | None:
    """定位主页 ``index.html``：优先根目录，否则取最浅一层。

    跳过 ``node_modules`` / ``.git`` / ``venv`` 等噪音目录，避免大仓扫描。
    受 hosting 支持的构建产物目录（``dist`` / ``build`` / ``out`` 等）会扫描
    （BUG-435），与 :func:`hosting.find_build_output` 对齐。
    """
    from local_webpage_access.hosting import find_build_output

    root = Path(project_dir)
    direct = root / "index.html"
    if direct.is_file():
        return direct
    # 预构建静态包常见只有 dist/index.html，优先按托管入口解析标题
    build_dir = find_build_output(root)
    if build_dir is not None:
        build_index = build_dir / "index.html"
        if build_index.is_file():
            return build_index
    skip = {
        "node_modules",
        ".git",
        ".svn",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
    }
    found: list[tuple[int, Path]] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        if depth >= max_depth:
            continue
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if name in skip or name.startswith("."):
                continue
            if entry.is_file() and name.lower() == "index.html":
                found.append((depth + 1, entry))
            elif entry.is_dir():
                stack.append((entry, depth + 1))
    if not found:
        return None
    found.sort(key=lambda item: (item[0], str(item[1])))
    return found[0][1]


def resolve_auto_display_name(project_dir: Path, *, slug: str) -> str:
    """自动显示名：主页 ``<title>`` 优先，否则 ``titleize(slug)``。"""
    index = find_homepage_index(project_dir)
    if index is not None:
        try:
            title = extract_html_title(
                index.read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            title = None
        if title:
            return title
    return titleize(slug)


def is_auto_titleized_name(
    name: str,
    instance_id: str,
    *,
    name_source: str | None = None,
) -> bool:
    """是否允许用主页 ``<title>`` 覆盖当前显示名。

    - ``nameSource=user``：永不覆盖
    - ``nameSource=html_title``：已是 title，无需再当「slug 美化」处理
    - ``nameSource=slug`` 或旧数据 ``None``：仅当 name 等于 ``titleize(id)`` 时允许
    """
    if name_source == "user":
        return False
    if name_source == "html_title":
        return False
    return bool(name) and name == titleize(instance_id)


def refresh_display_name_from_homepage(
    workspace: Workspace,
    registry: Registry,
    instance_id: str,
) -> str | None:
    """若当前名仍是可覆盖的自动美化名，且主页有 ``<title>``，则回写 manifest+registry。

    返回新名称；无需更新或无法解析时返回 ``None``。

    写回在 :func:`instance_lock` 内重新加载 manifest 后只改名称字段，避免与
    start/stop/update 并发时用陈旧整份对象覆盖 desiredState/状态/端口（BUG-434）。
    """
    from local_webpage_access.lifecycle import instance_lock

    row = registry.get_instance(instance_id)
    if row is None:
        return None
    current_name = str(row.get("name") or "")
    manifest_path = workspace.app_manifest_path(instance_id)
    name_source: str | None = None
    if manifest_path.is_file():
        try:
            preview = InstanceManifest.load(manifest_path)
            name_source = getattr(preview, "nameSource", None)
        except Exception:  # noqa: BLE001 — 回填失败不阻断列表
            log.warning("实例 %s 回填显示名时读取 manifest 失败", instance_id)
            return None
    else:
        return None
    if not is_auto_titleized_name(
        current_name, instance_id, name_source=name_source
    ):
        return None
    current_dir = workspace.app_current(instance_id)
    if not current_dir.is_dir():
        return None
    new_name = resolve_auto_display_name(current_dir, slug=instance_id)
    if new_name == current_name:
        return None

    with instance_lock(workspace, instance_id):
        try:
            manifest = InstanceManifest.load(manifest_path)
        except Exception:  # noqa: BLE001
            log.warning("实例 %s 回填显示名时锁内重读 manifest 失败", instance_id)
            return None
        fresh_source = getattr(manifest, "nameSource", None)
        if not is_auto_titleized_name(
            manifest.name, instance_id, name_source=fresh_source
        ):
            return None
        if manifest.name == new_name:
            return None
        manifest.name = new_name
        manifest.nameSource = "html_title"
        manifest.touch()
        manifest.save(manifest_path)
        # BUG-410：只改 instances.name；勿 upsert_from_manifest（manifest.hostPort
        # 常为空时会把 static_sites/containers 已登记端口清成 NULL）。
        registry.update_name(instance_id, new_name)
    return new_name


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
        id_basis: str | None = None,
    ) -> ImportResult:
        """导入一个 zip 包，返回 :class:`ImportResult`。

        Args:
            zip_path: zip 文件路径。
            name: 可选的显示名称；不提供时从 zip 文件名推导。
            path_alias: 可选的路径别名 slug（IMP-006 / IMP-014）。提供时校验格式、
                保留字与全局唯一性；对 ``shared-static`` 与 ``docker-compose``
                实例生效（容器在 import 预写 ``routeHost``，首次 start 生成别名
                片段）。其它 runtime 会拒绝。未提供时默认行为与 V1 完全一致。
            on_conflict: slug 冲突策略。``"rename"``（默认，daemon 友好）按
                ``-2`` / ``-3`` 自动改名新建；``"error"``（IMP-009 CLI）直接报错
                并建议改用 ``--update``，避免无脑新建历史误导入实例。
            id_basis: 可选的 instance id 候选原文（文件夹导入传真实目录名）。
                未提供时用 zip stem。纯中文 ``name`` 会回退到此字段，避免
                全部撞成 ``instance``。

        Raises:
            ZipImportError: zip 不存在、格式非法、路径穿越、解压失败、
                或 ``on_conflict="error"`` 时 slug 已被占用。
            PathError: 路径别名格式非法、命中保留字或已被占用。
        """
        with import_activity_lock(self.ws):
            return self._import_zip_locked(
                zip_path,
                name=name,
                path_alias=path_alias,
                on_conflict=on_conflict,
                id_basis=id_basis,
            )

    def _import_zip_locked(
        self,
        zip_path: str | Path,
        *,
        name: str | None = None,
        path_alias: str | None = None,
        on_conflict: str = "rename",
        id_basis: str | None = None,
    ) -> ImportResult:
        src = Path(zip_path).resolve()
        validate_zip(src)
        zip_hash = compute_zip_hash(src)

        base = slug_basis_for_id(name=name, path_stem=id_basis or src.stem)
        slug = slugify(base)
        # display_name：显式 --name 优先；否则解压后取主页 <title>（见下方）

        # IMP-006：路径别名在写盘前校验，避免半成品写入后才发现冲突。
        if path_alias is not None:
            existing = set(self.registry.list_route_hosts().keys())
            validate_path_alias(path_alias, existing_aliases=existing)
            log.info("路径别名 %s 已校验通过", path_alias)

        # BUG-127 / BUG-313：冲突检查与认领合并为 mkdir 原子 claim。
        # on_conflict=error 时不得在 TOCTOU 窗口里 silent 改名到 -2。
        instance_id = self._claim_unique_id(slug, on_conflict=on_conflict)
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

            # 显示名：CLI --name > 主页 <title> > titleize(slug)
            if name:
                display_name = name
                name_source = "user"
            else:
                index = find_homepage_index(current_dir)
                page_title = None
                if index is not None:
                    with contextlib.suppress(OSError):
                        page_title = extract_html_title(
                            index.read_text(encoding="utf-8", errors="replace")
                        )
                if page_title:
                    display_name = page_title
                    name_source = "html_title"
                else:
                    display_name = titleize(slug)
                    name_source = "slug"

            # IMP-006 / IMP-014：路径别名支持 shared-static 与 docker-compose。
            # 其它 runtime（若将来出现）仍明确拒绝，避免静默忽略。
            # 容器侧在 import 预写 container.routeHost，首次 start 时生成别名片段。
            if path_alias is not None and detection.runtime not in (
                Runtime.SHARED_STATIC,
                Runtime.DOCKER_COMPOSE,
            ):
                raise ZipImportError(
                    f"路径别名仅支持静态站点或 docker-compose 容器实例，"
                    f"该实例被识别为 {detection.form}（{detection.runtime}）；"
                    f"请去掉 --path-alias",
                    instance_id=instance_id,
                )

            # 构建 manifest
            manifest = self._build_manifest(
                instance_id,
                display_name,
                zip_hash,
                detection,
                path_alias=path_alias,
                name_source=name_source,
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
            # BUG-187：瞬时失败（IO/SQLite locked/扫描异常）不得携带 instance_id——
            # daemon process_zip 据 instance_id 是否存在区分"slug 冲突（归档）"与
            # "失败（下轮重试）"。携带 instance_id 会让瞬时失败被误判为冲突、永久归档
            # 并给已清理的实例记孤儿事件。这里只保留错误信息，由 daemon 走重试分支。
            raise ZipImportError(f"导入失败：{exc}") from exc
        except BaseException:
            # BUG-299：KeyboardInterrupt/SystemExit 等不走 Exception 分支；
            # 仍须清理已 claim 的实例目录，避免 slug 永久占用。
            log.error("导入 %s 被中断，清理半成品", instance_id)
            self._cleanup_failed(instance_id)
            raise

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
        with import_activity_lock(self.ws):
            return self._update_zip_locked(
                zip_path,
                instance_id,
                restart=restart,
                keep_data=keep_data,
                yes=yes,
                dry_run=dry_run,
                force_kind_change=force_kind_change,
            )

    def _update_zip_locked(
        self,
        zip_path: str | Path,
        instance_id: str,
        *,
        restart: bool = True,
        keep_data: bool = True,
        yes: bool = False,
        dry_run: bool = False,
        force_kind_change: bool = False,
    ) -> UpdateResult:
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

                # 重建 manifest：保留 id/createdAt/路径别名。
                # status/desiredState 由 apply_detection_to_manifest 决定
                # （pending→识别成功 → stopped；已在跑/已停则保留），
                # 勿再强制写回 old status（BUG-444）。
                manifest = apply_detection_to_manifest(
                    old_manifest, detection, self.ws
                )
                # 若旧名仍是 slug 美化名，且新包主页有 <title>，顺带刷新显示名
                if is_auto_titleized_name(
                    old_manifest.name,
                    instance_id,
                    name_source=getattr(old_manifest, "nameSource", None),
                ):
                    refreshed = resolve_auto_display_name(
                        current_dir, slug=instance_id
                    )
                    if refreshed != old_manifest.name:
                        manifest.name = refreshed
                        manifest.nameSource = "html_title"
                manifest.sourceZipHash = new_hash  # type: ignore[attr-defined]
                manifest.lastStartedAt = old_manifest.lastStartedAt
                manifest.lastHealthCheckAt = old_manifest.lastHealthCheckAt
                if old_manifest.static is not None and manifest.static is not None:
                    # BUG-321：更新源码不等于重新启用用户已停止的静态实例。
                    manifest.static.enabled = old_manifest.static.enabled
                # IMP-006：路径别名是用户/CLI 选择，不从 zip 推导，必须保留
                if (
                    old_manifest.static is not None
                    and old_manifest.static.routeMode == "name"
                    and old_manifest.static.routeHost
                    and manifest.static is not None
                ):
                    manifest.static.routeMode = "name"
                    manifest.static.routeHost = old_manifest.static.routeHost
                # BUG-385 / IMP-014：容器实例路径别名同理必须保留（与上方 static 对称）。
                # 否则容器实例 ``import --update`` 重建 manifest 时
                # ``container.routeHost`` 被清空，管理页别名消失；而网关 Caddy
                # 别名片段仍残留，造成 manifest/registry 与网关层不一致。
                if (
                    old_manifest.container is not None
                    and old_manifest.container.routeMode == "name"
                    and old_manifest.container.routeHost
                    and manifest.container is not None
                ):
                    manifest.container.routeMode = "name"
                    manifest.container.routeHost = old_manifest.container.routeHost
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

                # registry 同步
                self.registry.upsert_from_manifest(
                    manifest,
                    app_path=str(current_dir),
                    source_zip_path=str(orig_zip),
                )
                # BUG-348：registry 主同步成功后才执行不可逆数据清空；此前异常可完整回滚。
                if not keep_data:
                    persistent_data = self.ws.app_data(instance_id)
                    if persistent_data.exists():
                        shutil.rmtree(persistent_data, ignore_errors=True)
                    persistent_data.mkdir(parents=True, exist_ok=True)
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
                        old_host_port=old_host_port,
                        old_port_rows=old_port_rows,
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
            manifest is not None
            and manifest.runtime.value == "docker-compose"
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
            manifest=manifest,
            detection=detection,
            app_dir=app_dir,
            zip_hash=new_hash,
            prev_hash=old_hash,
            skipped=False,
            rebuilt=True,
            was_running=was_running,
            needs_restart=needs_restart,
            needs_rebuild=needs_rebuild,
            kind_changed=kind_changed,
            sanitized=sanitized,
        )

    # ---- 文件夹源导入与更新（IMP-047）---------------------------------------

    def import_from_dir(
        self,
        source_dir: str | Path,
        *,
        name: str | None = None,
        path_alias: str | None = None,
        on_conflict: str = "rename",
    ) -> ImportResult:
        """从本机文件夹源导入实例（IMP-047）。

        红线流程：
        1. :func:`~folder_source.validate_source_dir` 校验源目录。
        2. :func:`~folder_source.pack_source_dir` 把源目录**只读复制**为临时 zip。
        3. 复用 :meth:`import_zip` 完成解压、扫描、manifest、registry 登记。
        4. 写回 ``sourceKind="folder"`` + ``sourceDirPath`` + ``sourceSyncHash``。

        关联目录只是复制来源；运行根永远在 ``apps/<id>/current/``。
        """
        with import_activity_lock(self.ws):
            return self._import_from_dir_locked(
                source_dir,
                name=name,
                path_alias=path_alias,
                on_conflict=on_conflict,
            )

    def _import_from_dir_locked(
        self,
        source_dir: str | Path,
        *,
        name: str | None = None,
        path_alias: str | None = None,
        on_conflict: str = "rename",
    ) -> ImportResult:
        from local_webpage_access.folder_source import (
            compute_source_hash,
            pack_source_dir,
            validate_source_dir,
        )

        resolved_dir = validate_source_dir(
            source_dir, workspace_root=self.ws.root
        )
        sync_hash = compute_source_hash(resolved_dir)
        log.info(
            "文件夹源导入：%s（指纹 %s）", resolved_dir, sync_hash[:12]
        )

        # 打包为临时 zip 后复用 import_zip（已持锁，走 _locked）
        fd, tmp_zip_path = tempfile.mkstemp(
            suffix=".zip", prefix="lwa-folder-import-"
        )
        os.close(fd)
        tmp_zip = Path(tmp_zip_path)
        try:
            pack_source_dir(resolved_dir, dest_zip=tmp_zip)
            result = self._import_zip_locked(
                tmp_zip,
                name=name or resolved_dir.name,
                path_alias=path_alias,
                on_conflict=on_conflict,
                id_basis=resolved_dir.name,
            )
        finally:
            with contextlib.suppress(OSError):
                tmp_zip.unlink(missing_ok=True)

        # 写回文件夹源元数据
        manifest_path = self.ws.app_manifest_path(result.instance_id)
        manifest = InstanceManifest.load(manifest_path)
        manifest.sourceKind = "folder"
        manifest.sourceDirPath = str(resolved_dir)
        manifest.sourceSyncHash = sync_hash
        manifest.touch()
        manifest.save(manifest_path)

        self.registry.add_event(
            result.instance_id,
            "import",
            f"文件夹源导入：{resolved_dir}（指纹 {sync_hash[:12]}）",
        )

        log.info(
            "文件夹源导入成功：%s（源 %s）", result.instance_id, resolved_dir
        )
        return result

    def update_from_dir(
        self,
        instance_id: str,
        *,
        restart: bool = True,
        keep_data: bool = True,
        yes: bool = False,  # noqa: ARG002 - 交互确认由 CLI 层处理
        dry_run: bool = False,
        force_kind_change: bool = False,
    ) -> UpdateResult:
        """从关联文件夹源更新实例（IMP-047）。

        流程：
        1. 读 manifest 的 ``sourceDirPath``；缺失或非 folder 源 -> 报错。
        2. 校验源目录仍存在/可读；缺失 -> 明确错误（禁止挂载回退）。
        3. 计算当前源目录指纹，与 ``sourceSyncHash`` 比较：
           - 相同 -> ``skipped=True``（无需更新），不 rebuild / 不重启。
           - 不同 -> 打包为临时 zip，调用 :meth:`update_zip`。
        4. 更新成功后写回新的 ``sourceSyncHash``。

        Raises:
            ZipImportError: 实例不存在、非 folder 源、源目录缺失、更新失败。
        """
        with import_activity_lock(self.ws):
            return self._update_from_dir_locked(
                instance_id,
                restart=restart,
                keep_data=keep_data,
                yes=yes,
                dry_run=dry_run,
                force_kind_change=force_kind_change,
            )

    def _update_from_dir_locked(
        self,
        instance_id: str,
        *,
        restart: bool = True,
        keep_data: bool = True,
        yes: bool = False,
        dry_run: bool = False,
        force_kind_change: bool = False,
    ) -> UpdateResult:
        from local_webpage_access.folder_source import (
            compute_source_hash,
            pack_source_dir,
            validate_source_dir,
        )

        if not self.registry.instance_exists(instance_id):
            raise ZipImportError(
                f"实例 {instance_id} 不存在，无法更新",
                instance_id=instance_id,
            )

        manifest_path = self.ws.app_manifest_path(instance_id)
        if not manifest_path.is_file():
            raise ZipImportError(
                f"实例 {instance_id} 缺少 local-web.json，无法更新",
                instance_id=instance_id,
            )
        old_manifest = InstanceManifest.load(manifest_path)

        source_kind = getattr(old_manifest, "sourceKind", "zip")
        source_dir_str = getattr(old_manifest, "sourceDirPath", None)
        if source_kind != "folder" or not source_dir_str:
            raise ZipImportError(
                f"实例 {instance_id} 不是文件夹源实例（sourceKind={source_kind!r}），"
                f"无法用 update-from-dir 更新；请用 lwa import --update 加 zip。",
                instance_id=instance_id,
            )

        source_dir = Path(source_dir_str)
        try:
            validate_source_dir(source_dir, workspace_root=self.ws.root)
        except Exception as exc:
            # 源目录缺失/不可读 -> 明确错误，禁止挂载回退
            raise ZipImportError(
                f"关联源目录不可用：{exc}",
                instance_id=instance_id,
            ) from exc

        new_sync_hash = compute_source_hash(source_dir)
        old_sync_hash = getattr(old_manifest, "sourceSyncHash", None)

        # 无变更短路
        if new_sync_hash == old_sync_hash:
            log.info(
                "实例 %s 的文件夹源内容未变化（指纹 %s），跳过更新",
                instance_id,
                new_sync_hash[:12],
            )
            self.registry.add_event(
                instance_id,
                "update",
                f"文件夹源内容未变化（指纹 {new_sync_hash[:12]}），跳过更新",
            )
            return UpdateResult(
                instance_id=instance_id,
                manifest=old_manifest,
                detection=None,
                app_dir=self.ws.app_dir(instance_id),
                zip_hash=new_sync_hash,
                prev_hash=old_sync_hash,
                skipped=True,
                was_running=old_manifest.desiredState == DesiredState.RUNNING,
                needs_restart=False,
            )

        log.info(
            "实例 %s 文件夹源有变更（指纹 %s -> %s）",
            instance_id,
            (old_sync_hash[:12] if old_sync_hash else "∅"),
            new_sync_hash[:12],
        )

        # 打包为临时 zip 后复用 update_zip（已持锁，走 _locked）
        fd, tmp_zip_path = tempfile.mkstemp(
            suffix=".zip", prefix="lwa-folder-update-"
        )
        os.close(fd)
        tmp_zip = Path(tmp_zip_path)
        try:
            pack_source_dir(source_dir, dest_zip=tmp_zip)
            result = self._update_zip_locked(
                tmp_zip,
                instance_id,
                restart=restart,
                keep_data=keep_data,
                yes=yes,
                dry_run=dry_run,
                force_kind_change=force_kind_change,
            )
        finally:
            with contextlib.suppress(OSError):
                tmp_zip.unlink(missing_ok=True)

        # 写回文件夹源元数据（非 dry_run 且非 skipped 时）。
        # update_zip 重建 manifest 后 sourceKind 会回到默认 "zip"，
        # 必须同时恢复 sourceKind/sourceDirPath/sourceSyncHash，否则下一次
        # update_from_dir 会被判为非 folder 源而拒绝（P0 回归）。
        if not result.dry_run and not result.skipped:
            updated_manifest = InstanceManifest.load(manifest_path)
            updated_manifest.sourceKind = "folder"
            updated_manifest.sourceDirPath = str(source_dir)
            updated_manifest.sourceSyncHash = new_sync_hash
            updated_manifest.touch()
            updated_manifest.save(manifest_path)

        return result

    @staticmethod
    def _kind_changed(
        old: InstanceManifest, detection: DetectionResult
    ) -> bool:
        """新扫描结果与旧 manifest 的 kind/runtime 是否不一致。

        pending（未识别）视为可更新（沿用 static 草稿），不算形态变化；
        但旧实例为容器（docker-compose）时例外——pending 会把运行中容器改写成
        static 草稿、删 containers 登记却不停容器，造成孤儿（BUG-180）。此时判为
        形态变化，交由 force_kind_change 的拒绝/停机流程处理，避免静默孤儿化。
        """
        old_rt = (
            old.runtime.value if hasattr(old.runtime, "value") else old.runtime
        )
        if detection.pending or detection.kind is None:
            # 容器实例被 pending zip 改写为 static 草稿 = 跨形态（BUG-180）
            return old_rt == "docker-compose"
        old_kind = old.kind.value if hasattr(old.kind, "value") else old.kind
        if detection.runtime is None:
            return detection.kind != old_kind
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
        old_host_port: int | None,
        old_port_rows: tuple[dict[str, object] | None, dict[str, object] | None],
        orig_zip: Path,
        orig_zip_bak: Path,
        old_resources: dict[str, object] | None,
    ) -> None:
        """在 current/ 已换入后恢复旧源码与关键元数据（BUG-056 / BUG-323）。"""
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
            if old_host_port is not None:
                if old_manifest.static is not None:
                    old_manifest.static.hostPort = old_host_port
                elif old_manifest.container is not None:
                    old_manifest.container.hostPort = old_host_port
            self.registry.upsert_from_manifest(
                old_manifest,
                app_path=str(current_dir),
                source_zip_path=str(orig_zip),
            )
            # BUG-323：upsert 后显式回写停止前的端口子表快照，避免 host_port 被清空。
            old_static, old_container = old_port_rows
            if old_static and old_static.get("host_port") is not None:
                self.registry.upsert_static_site(
                    instance_id,
                    {
                        "root": old_static.get("root_path", "public"),
                        "gateway": old_static.get("gateway", "caddy"),
                        "routeMode": old_static.get("route_mode", "port"),
                        "hostPort": _coerce_host_port(old_static["host_port"]),
                        "routeHost": old_static.get("route_host"),
                        "gatewayConfigPath": old_static.get("gateway_config_path"),
                        "enabled": bool(old_static.get("enabled", 1)),
                    },
                )
            if old_container and old_container.get("host_port") is not None:
                self.registry.upsert_container(
                    instance_id,
                    {
                        "projectName": old_container.get("compose_project")
                        or f"lwa-{instance_id}",
                        "serviceName": old_container.get("service_name", "app"),
                        "image": old_container.get("image"),
                        "imageId": old_container.get("image_id"),
                        "containerId": old_container.get("container_id"),
                        "internalPort": old_container.get("internal_port"),
                        "hostPort": _coerce_host_port(old_container["host_port"]),
                        "routeMode": old_container.get("route_mode", "port"),
                        "routeHost": old_container.get("route_host"),
                        "composePath": old_container.get("compose_path"),
                        "dockerfilePath": old_container.get("dockerfile_path"),
                        "resourceLimits": {
                            "memory": old_container.get("memory_limit"),
                            "cpus": old_container.get("cpu_limit"),
                        },
                    },
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

    def _claim_unique_id(
        self, base_slug: str, *, on_conflict: str = "rename"
    ) -> str:
        """原子占用实例目录，避免并发导入同一 slug（BUG-127 / BUG-313）。"""
        candidate = base_slug
        n = 2

        def _conflict_error(*, concurrent: bool = False) -> ZipImportError:
            prefix = (
                f"实例 {base_slug} 已被并发创建"
                if concurrent
                else f"实例 {base_slug} 已存在"
            )
            return ZipImportError(
                f"{prefix}。请换一个含英文/数字的名称，或先删除该实例后再导入；"
                f"若要覆盖更新已有实例，可用「从源更新」或 "
                f"`lwa import --update {base_slug}`。",
                instance_id=base_slug,
            )

        while True:
            if self.registry.instance_exists(candidate):
                if on_conflict == "error":
                    raise _conflict_error()
                candidate = f"{base_slug}-{n}"
                n += 1
                continue
            try:
                self.ws.app_dir(candidate).mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                if on_conflict == "error":
                    raise _conflict_error(concurrent=True) from None
                candidate = f"{base_slug}-{n}"
                n += 1
                continue
            try:
                self.ws.ensure_app_dirs(candidate)
            except Exception:
                # ensure_app_dirs 失败时清理已占用的实例根目录，避免孤儿目录
                with contextlib.suppress(OSError):
                    shutil.rmtree(self.ws.app_dir(candidate), ignore_errors=True)
                raise
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
        name_source: str | None = None,
    ) -> InstanceManifest:
        return build_manifest_from_detection(
            instance_id=instance_id,
            display_name=display_name,
            detection=detection,
            workspace=self.ws,
            zip_hash=zip_hash,
            path_alias=path_alias,
            name_source=name_source,
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
    name_source: str | None = None,
) -> InstanceManifest:
    """根据扫描结果构造一个完整且 schema 一致的 :class:`InstanceManifest`。

    被 :class:`Importer` 导入流程与 ``lwa scan`` 重扫流程共用，
    确保 static ↔ container 配置始终与 runtime 匹配。

    ``path_alias`` 非 ``None`` 时（IMP-006 / IMP-014）写入对应配置的
    ``routeMode="name"`` + ``routeHost=<alias>``：静态进 ``static``，
    docker-compose 进 ``container``；其它 runtime 忽略该参数。
    """
    if detection.pending or detection.kind is None:
        # 未识别：以 static 草稿落盘，标记 pending
        kind = Kind.STATIC
        runtime = Runtime.SHARED_STATIC
        serving_mode = ServingMode.SHARED_STATIC
        resource_profile = ResourceProfile.TINY
        last_error = "; ".join(detection.notes) if detection.notes else "未识别项目类型"
        initial_status = Status.PENDING
    else:
        kind = detection.kind
        runtime = detection.runtime  # type: ignore[assignment]
        serving_mode = detection.servingMode  # type: ignore[assignment]
        resource_profile = detection.resourceProfile
        last_error = None
        # 识别成功：stopped（可启动）。勿再用 pending——管理页会把 pending
        # 当成「待识别」并禁用启动按钮，造成文件夹导入死胡同。
        initial_status = Status.STOPPED

    kwargs: dict = dict(
        id=instance_id,
        name=display_name,
        nameSource=name_source,
        version="1",
        kind=kind,
        runtime=runtime,
        servingMode=serving_mode,
        resourceProfile=resource_profile,
        stack=detection.stack,
        hasDatabase=detection.hasDatabase,
        database=detection.database,
        desiredState=DesiredState.STOPPED,
        status=initial_status,
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

        container_kwargs: dict = {
            "projectName": f"lwa-{instance_id}",
            "internalPort": detection.internalPort or 8000,
            "composePath": str(workspace.app_compose_path(instance_id)),
            "dockerfilePath": str(workspace.app_dockerfile_path(instance_id)),
            "resourceLimits": profile_to_limits(resource_profile),
        }
        if path_alias is not None:
            # IMP-014：导入期预写容器别名；首次 start 时生成 Caddy 片段。
            container_kwargs["routeMode"] = "name"
            container_kwargs["routeHost"] = path_alias
        kwargs["container"] = ContainerConfig(**container_kwargs)

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
    亦保留 IMP-047 文件夹源字段（``sourceKind`` / ``sourceDirPath`` /
    ``sourceSyncHash``）：``build_manifest_from_detection`` 默认
    ``sourceKind="zip"``，若不透传则 ``lwa scan`` 会静默抹掉文件夹源身份。
    """
    fresh = build_manifest_from_detection(
        instance_id=manifest.id,
        display_name=manifest.name,
        detection=detection,
        workspace=workspace,
        zip_hash=getattr(manifest, "sourceZipHash", None),
        name_source=getattr(manifest, "nameSource", None),
    )
    # 保留版本号与原始 zip 路径（重扫不应改变这些）
    fresh.version = manifest.version
    fresh.sourceZipPath = manifest.sourceZipPath
    fresh.appPath = manifest.appPath
    fresh.createdAt = manifest.createdAt
    # 生命周期：真·未识别 → pending；从 pending 识别成功 → stopped；
    # 已在跑/已停的实例重扫不得把 status 打回 pending。
    if detection.pending or detection.kind is None:
        fresh.status = Status.PENDING
        fresh.desiredState = DesiredState.STOPPED
    elif manifest.status == Status.PENDING:
        fresh.status = Status.STOPPED
        fresh.desiredState = DesiredState.STOPPED
    else:
        fresh.status = manifest.status
        fresh.desiredState = manifest.desiredState
    fresh.lastStartedAt = manifest.lastStartedAt
    fresh.lastHealthCheckAt = manifest.lastHealthCheckAt
    # IMP-047：文件夹源身份与同步指纹不得因 scan / update_zip 重建而丢失
    fresh.sourceKind = getattr(manifest, "sourceKind", "zip") or "zip"
    fresh.sourceDirPath = getattr(manifest, "sourceDirPath", None)
    fresh.sourceSyncHash = getattr(manifest, "sourceSyncHash", None)
    return fresh


__all__ = [
    "Importer",
    "ImportResult",
    "UpdateResult",
    "slugify",
    "slug_basis_for_id",
    "titleize",
    "extract_html_title",
    "find_homepage_index",
    "resolve_auto_display_name",
    "is_auto_titleized_name",
    "refresh_display_name_from_homepage",
    "build_manifest_from_detection",
    "apply_detection_to_manifest",
]
