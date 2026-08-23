"""Docker Compose 模板与 ``.env`` 生成（WBS-13）。

为容器实例生成 Compose project，作为容器实例的管理单元。输出到
``apps/<id>/docker/compose.yaml`` 与 ``apps/<id>/docker/.env``。

设计要点（对应 V1 设计说明第 13 节）：

1. 构建上下文是实例目录 ``apps/<id>/``（``context: ..``），Dockerfile 在 ``docker/``。
2. 端口映射 ``${HOST_PORT}:${INTERNAL_PORT}`` 由 ``.env`` 插值，避免硬编码。
3. SQLite 项目挂载 ``../data``：默认 ``/app/data`` + ``DATABASE_URL``；若应用使用
   ``RUNTIME_ROOT`` / ``runtime_paths``，则挂载 ``../data:/app/runtime/data`` 并注入
   ``RUNTIME_ROOT=/app/runtime``（BUG-198）。
4. 资源限制使用 Compose legacy 顶层字段 ``mem_limit`` / ``cpus``（单机模式直接生效），
   ``local-web.json`` 的 ``resourceLimits.{memory,cpus}`` 在渲染时映射为这两个字段。
5. ``restart: unless-stopped`` 配合 ``desiredState`` 实现"开机自启但 stop 后不拉起"。
6. 顶层 ``name:`` 固定 Compose project name，避免依赖目录名推断。

compose.yaml 用字符串模板而非 ``yaml.safe_dump`` 渲染，保证 ``${}`` 插值与
``mem_limit``/``cpus`` 字段被 ``docker compose config`` 原样接受（YAML dumper
会对含 ``:`` ``{`` ``}`` 的值加引号，破坏 Compose 变量插值）。
"""

from __future__ import annotations

import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from local_webpage_access.errors import ConfigError, PathError
from local_webpage_access.logging import get_logger
from local_webpage_access.models import InstanceManifest
from local_webpage_access.paths import Workspace, resolve_source_workdir

log = get_logger("compose")

_SERVICE_NAME = "app"
_DATA_VOLUME_APP = "../data:/app/data"
_DATA_VOLUME_RUNTIME = "../data:/app/runtime/data"
# IMP-058 Gate-A CHK-V03：默认 SQLite 文件名（scanner 未扫描到源文件时的兜底）。
# 若 DatabaseConfig.dbFilename 存在，则用原文件名，避免把应用指向全新空库（BUG-474）。
_SQLITE_DEFAULT_DB_FILENAME = "app.sqlite"
# IMP-015：业务密钥可选注入文件（用户按 docker/.env.example 填写后放入 docker/.env.local）。
# 用 Compose env_file 的对象形式 required:false，缺失时不报错（WBS-20260708 阶段3.2 决策）。
_ENV_LOCAL_BLOCK = "      - path: .env.local\n        required: false\n"

# issue #11：LWA 管理键——每次重生成以新值覆盖（DATABASE_URL 走 BUG-491 特殊
# 保留逻辑）。旧 .env 中的其余键视为**业务键**，迁移到 .env.local 而不是被
# 整文件覆盖抹掉（.env 与 .env.local 同在 compose env_file 里，注入语义不变）。
_ENV_MANAGED_KEYS = frozenset(
    {"HOST_PORT", "INTERNAL_PORT", "MEMORY_LIMIT", "CPU_LIMIT", "DATABASE_URL"}
)

_COMPOSE_TEMPLATE = """\
# 由 lwa 自动生成，请勿手动编辑。
# 实例：{instance_id}（host_port={host_port}, internal_port={internal_port}）
# 端口/资源/DATABASE_URL 来自同目录 .env；业务密钥可放 .env.local（可选，缺失不报错）。
name: {project_name}
services:
  {service}:
    build:
      context: ..
      dockerfile: docker/Dockerfile
    container_name: lwa-{instance_id}
    ports:
      - "${{HOST_PORT}}:${{INTERNAL_PORT}}"
    env_file:
      - .env
{env_local_block}{extra_environment}{volumes_block}    mem_limit: ${{MEMORY_LIMIT:-{memory}}}
    cpus: "${{CPU_LIMIT:-{cpus}}}"
    restart: unless-stopped
"""


def uses_runtime_root(source_dir: Path | None, manifest: InstanceManifest) -> bool:
    """BUG-198：是否按 RUNTIME_ROOT / runtime/data 持久化。

    ``source_dir`` 为 ``None`` 时仅依据 manifest.database.dataDir 判断。
    """
    data_dir = (manifest.database.dataDir if manifest.database else None) or ""
    if data_dir.replace("\\", "/").startswith("runtime"):
        return True
    if source_dir is None:
        return False
    return (source_dir / "src" / "app" / "runtime_paths.py").is_file() or (
        source_dir / "app" / "runtime_paths.py"
    ).is_file()


def _uses_runtime_root(source_dir: Path, manifest: InstanceManifest) -> bool:
    """内部别名，保持既有调用点。"""
    return uses_runtime_root(source_dir, manifest)


def container_data_paths(source_dir: Path, manifest: InstanceManifest) -> list[str]:
    """容器内数据目录的候选路径（BUG-205 重建前数据迁移用）。

    以"新 compose 将挂载的目标"优先，再兜底另一种历史布局，覆盖 RUNTIME_ROOT 与
    非 RUNTIME_ROOT 两类既有容器——旧实例的库可能写在容器可写层（旧版未挂载
    data/ 或挂载路径不同），重建 down 前需把这些路径的内容救出到宿主 data/。
    """
    if _uses_runtime_root(source_dir, manifest):
        return ["/app/runtime/data", "/app/data"]
    return ["/app/data", "/app/runtime/data"]


def generate_compose(
    manifest: InstanceManifest,
    workspace: Workspace,
    *,
    host_port: int,
) -> Path:
    """渲染 ``docker/compose.yaml``（WBS-13.01~10）。

    Args:
        manifest: 实例元数据（需有 container 配置）。
        workspace: 工作区。
        host_port: 宿主机端口（写入注释，实际映射走 .env 插值）。

    Returns:
        写入的 compose.yaml 路径。
    """
    container = manifest.container
    if container is None:
        raise ValueError(f"实例 {manifest.id} 缺少 container 配置，无法生成 compose.yaml")
    limits = container.resourceLimits

    # 评审-组3：projectName 来自源项目元数据（用户可控），含 `: `/`#`/`[` 等
    # YAML 特殊字符会改变 compose 结构或直接非法；按 Compose project 名规则
    # 白名单校验，非法时回落实例 id（slug 恒合法）。
    project_name = container.projectName
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", project_name or ""):
        project_name = manifest.id

    source_dir = workspace.app_current(manifest.id)
    env_lines: list[str] = []
    # FastAPI 常见 src/ 布局：不重建镜像时也要能找到 main（与 Dockerfile ENV 对齐）。
    if (source_dir / "src" / "main.py").is_file():
        env_lines.append("      - PYTHONPATH=src")
    # issue#1 附加观察：统一注入 PORT 环境变量（= .env 的 INTERNAL_PORT），
    # 让按 PORT 约定监听的应用与探针端口天然对齐，无需手工对表。
    env_lines.append("      - PORT=${INTERNAL_PORT}")

    data_volume = _DATA_VOLUME_APP
    if _is_sqlite(manifest) and _uses_runtime_root(source_dir, manifest):
        data_volume = _DATA_VOLUME_RUNTIME
        env_lines.append("      - RUNTIME_ROOT=/app/runtime")

    extra_environment = ""
    if env_lines:
        extra_environment = "    environment:\n" + "\n".join(env_lines) + "\n"

    # issue#1 问题2：volumes 合并 manifest.container.extraVolumes（业务定制挂载）。
    # 手工编辑 compose.yaml 会在重生成时被抹掉；持久化出口是 local-web.json 的
    # container.extraVolumes，每次渲染原样合并。安全审计在写出前统一把关。
    volumes: list[str] = [data_volume]
    for extra in container.extraVolumes:
        entry = extra.strip()
        if not entry or "\n" in entry or "\r" in entry:
            raise ValueError(
                f"实例 {manifest.id} container.extraVolumes 含非法条目（空或换行）：{extra!r}"
            )
        volumes.append(entry)
    volumes_block = "    volumes:\n" + "".join(f"      - {v}\n" for v in volumes)

    content = _COMPOSE_TEMPLATE.format(
        project_name=project_name,
        instance_id=manifest.id,
        service=_SERVICE_NAME,
        host_port=host_port,
        internal_port=container.internalPort,
        volumes_block=volumes_block,
        memory=limits.memory,
        cpus=limits.cpus,
        env_local_block=_ENV_LOCAL_BLOCK,
        extra_environment=extra_environment,
    )
    # WBS-25.03/04/05：自检生成的 compose 是否含 critical 安全问题
    # （模板本身安全；此检查防止模板被改动或 skill 覆盖后引入风险）。
    from local_webpage_access.security import audit_compose, has_critical

    findings = audit_compose(content)
    if has_critical(findings):
        codes = ", ".join(f.code for f in findings if f.level == "critical")
        raise RuntimeError(f"生成的 compose.yaml 含 critical 安全问题（{codes}），已拒绝写出")
    for f in findings:
        log.warning("compose 安全审计 [%s] %s", f.code, f.message)

    out_path = workspace.app_compose_path(manifest.id)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    log.info("已生成 compose.yaml：%s", out_path)
    return out_path


def generate_env(
    manifest: InstanceManifest,
    workspace: Workspace,
    *,
    host_port: int,
) -> Path:
    """渲染 ``docker/.env``（WBS-13.08）。

    包含 ``HOST_PORT`` / ``INTERNAL_PORT`` / ``MEMORY_LIMIT`` / ``CPU_LIMIT``；
    SQLite 项目额外注入 ``DATABASE_URL``（BUG-474：绝对路径，cwd 无关，不再仅限非 RUNTIME_ROOT 布局）。
    """
    container = manifest.container
    if container is None:
        raise ValueError(f"实例 {manifest.id} 缺少 container 配置，无法生成 .env")
    limits = container.resourceLimits
    source_dir = workspace.app_current(manifest.id)

    lines = [
        "# 由 lwa 自动生成，请勿手动编辑。",
        f"HOST_PORT={host_port}",
        f"INTERNAL_PORT={container.internalPort}",
        f"MEMORY_LIMIT={limits.memory}",
        f"CPU_LIMIT={limits.cpus}",
    ]
    # BUG-491：提前解析 out_path，SQLite 分支需要读取已有 .env 中的 DATABASE_URL。
    out_path = workspace.app_env_path(manifest.id)
    # issue #11：先解析旧 .env（管理键重写、业务键迁移、无法解析的行备份），
    # 再覆盖写入，杜绝整文件覆盖吞掉用户手写的业务键。DATABASE_URL 仅在
    # SQLite 实例下算 LWA 管理键（BUG-491 保留逻辑）；非 SQLite 实例 LWA
    # 从不写它，视为用户业务键迁移到 .env.local。
    existing = _parse_existing_env(out_path, database_url_managed=_is_sqlite(manifest))

    if _is_sqlite(manifest):
        # A.R01：只有当证据表明应用消费 DATABASE_URL 时才自动注入。
        # 无消费证据时保留原配置，避免把不读取 DATABASE_URL 的应用指向新空库。
        db_config = getattr(manifest, "databaseConfig", None)
        consumes_db_url = (
            db_config is not None and db_config.get("consumesDatabaseUrl", False)
            if isinstance(db_config, dict)
            else False
        )

        # BUG-491：更新已有实例时，保留旧 .env 中的 DATABASE_URL，避免被源目录
        # 占位 SQLite 文件（如 _empty_check.db）覆盖指向空库导致数据丢失。
        preserved_db_url = existing.database_url

        if consumes_db_url:
            if preserved_db_url:
                # 保留已有 DATABASE_URL（用户或上一次部署已确认可用）
                log.info(
                    "保留已有 DATABASE_URL（实例 %s）：%s",
                    manifest.id,
                    preserved_db_url,
                )
                lines.append(f"DATABASE_URL={preserved_db_url}")
            else:
                # BUG-474: 所有 SQLite 项目都注入绝对路径 DATABASE_URL，避免相对路径在不同 cwd 下解析到不同库文件。
                # IMP-058 Gate-A CHK-V03：保留 scanner 扫描到的源 SQLite 文件名，避免把应用
                # 指向全新空库（原硬编码 app.sqlite 的数据丢失风险）。无源文件名时用默认兜底。
                raw_db_filename = (
                    manifest.database.dbFilename
                    if manifest.database and manifest.database.dbFilename
                    else _SQLITE_DEFAULT_DB_FILENAME
                )
                # CHK-192/P1：scanner 可能保存相对路径（如 "data/app.sqlite"），
                # 直接拼接到 /app/data/ 会导致路径重复（/app/data/data/app.sqlite）。
                # 只取 basename 作为容器内文件名。
                db_filename = Path(raw_db_filename).name
                lines.append(f"DATABASE_URL=sqlite:////app/data/{db_filename}")

                # CHK-192/P1：把源 SQLite 文件复制到宿主 data 目录（apps/<id>/data/），
                # 该目录通过 compose 挂载为 /app/data。若不复制，容器启动时指向空库，
                # 既有数据丢失。仅在源文件存在且目标不存在时复制（避免覆盖用户修改）。
                host_data_dir = workspace.app_data(manifest.id)
                host_data_dir.mkdir(parents=True, exist_ok=True)
                # CHK-193/P1：构造源 SQLite 文件路径时需包含 sourceSubdir。
                # 当 sourceSubdir="backend" 时，dbFilename 相对于 backend/，
                # 源文件在 current/backend/<dbFilename>，而非 current/<dbFilename>。
                source_subdir = getattr(manifest, "sourceSubdir", None)
                source_root = source_dir
                if source_subdir:
                    try:
                        source_root = resolve_source_workdir(source_dir, source_subdir)
                    except PathError:
                        source_root = source_dir
                source_db_path = source_root / raw_db_filename
                target_db_path = host_data_dir / db_filename
                if source_db_path.is_file() and not target_db_path.exists():
                    import shutil

                    try:
                        shutil.copy2(source_db_path, target_db_path)
                        log.info(
                            "已复制源 SQLite 文件 %s -> %s",
                            source_db_path,
                            target_db_path,
                        )
                    except OSError as exc:
                        log.warning("复制源 SQLite 文件失败（忽略）：%s", exc)
        else:
            # A.R01：无消费证据，不自动注入 DATABASE_URL
            log.warning(
                "A.R01：未确认应用消费 DATABASE_URL，跳过自动注入（实例 %s）",
                manifest.id,
            )
            lines.append("# A.R01: 未检测到应用消费 DATABASE_URL，未自动注入。")
            lines.append("# 如需注入，请在应用 config 中使用 os.getenv('DATABASE_URL')。")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # issue #11：先迁业务键再覆盖 .env——迁移中断时旧 .env 原样保留；覆盖
    # 中断时业务键已在 .env.local（两处冗余无害，下次迁移按同名冲突幂等收敛）。
    if existing.unparseable_lines:
        _backup_unparseable_env(out_path, existing.unparseable_lines)
    if existing.business_lines:
        _migrate_business_keys_to_env_local(out_path.parent, existing.business_lines)
    # issue #11：临时文件 + os.replace 原子写入（崩溃不留半写文件），权限 0600。
    _atomic_write_env(out_path, "\n".join(lines) + "\n")
    log.info("已生成 .env：%s", out_path)

    # IMP-015（WBS-20260708 阶段3.2）：业务 .env.example 复制为 docker/.env.example。
    # 用户据此填写 docker/.env.local（由 compose env_file 的 required:false 可选注入）。
    # 不覆盖已存在的 .env.example（避免吞掉用户改动）；不自动填密钥。
    import shutil

    source_env_example = workspace.app_current(manifest.id) / ".env.example"
    target_env_example = out_path.parent / ".env.example"
    if source_env_example.is_file() and not target_env_example.exists():
        try:
            shutil.copy2(source_env_example, target_env_example)
            log.info("已复制业务 .env.example → %s", target_env_example)
        except OSError as exc:
            log.warning("复制 .env.example 失败（忽略）：%s", exc)

    # BUG-199：缺 .env.local 时为 JWT_SECRET 等空密钥生成持久值，避免重建后 token 失效。
    # BUG-208：密钥检测须读"当前源" current/.env.example，而非首次导入时缓存的
    # docker/.env.example——后者在项目更新（新增 JWT_SECRET）后不会刷新（上方 copy
    # 仅在缺失时复制），导致检测读到旧缓存、.env.local 漏生成，重建后 token 失效。
    ensure_env_local_secrets(out_path.parent, source_env_example)
    return out_path


@dataclass
class _ExistingEnv:
    """旧 ``.env`` 的解析结果（issue #11）。

    日志只报告**键名/行号**，绝不打印值。
    """

    database_url: str | None = None
    # (键名, 原始行)：迁移到 .env.local 时保留原格式（引号、内嵌注释等）。
    business_lines: list[tuple[str, str]] = field(default_factory=list)
    # (行号, 原始行)：无法按 KEY=VALUE 解析的行，备份后告警，绝不静默丢弃。
    unparseable_lines: list[tuple[int, str]] = field(default_factory=list)


def _parse_existing_env(env_path: Path, *, database_url_managed: bool = True) -> _ExistingEnv:
    """把已有 ``.env`` 拆成管理键 / 业务键 / 无法解析的行（issue #11）。

    ``database_url_managed=False``（非 SQLite 实例）时 ``DATABASE_URL`` 按业务键处理。
    BUG-585：文件不存在按空处理；存在但读取失败抛 :class:`ConfigError`（fail-closed）。
    """
    existing = _ExistingEnv()
    if not env_path.is_file():
        return existing
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError as exc:
        # BUG-585：fail-closed——旧 .env 存在但读不出（权限/瞬时 I/O）时绝不能
        # 按"无旧配置"继续，否则随后的覆盖写入会静默吞掉业务键。
        raise ConfigError(f"读取旧 .env 失败，已中止重生成以保留原文件：{exc}") from exc
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, sep, value = line.partition("=")
        if not sep or not key.strip():
            existing.unparseable_lines.append((lineno, raw))
            continue
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            # 键名含空格等非法字符：docker compose 解析 env_file 时可能整个
            # 报错，绝不能原样迁入 .env.local--归入坏行，走备份+告警。
            existing.unparseable_lines.append((lineno, raw))
            continue
        if key == "DATABASE_URL" and database_url_managed:
            # BUG-491：SQLite 分支特殊保留逻辑的输入。
            existing.database_url = value
        elif key not in _ENV_MANAGED_KEYS or key == "DATABASE_URL":
            existing.business_lines.append((key, raw))
    return existing


def _atomic_write_env(path: Path, content: str, *, mode: int = 0o600) -> None:
    """issue #11：临时文件 + ``os.replace`` 原子写入；默认 0600（含端口/密钥类值）。"""
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    tmp.chmod(mode)
    os.replace(tmp, path)


def _backup_unparseable_env(env_path: Path, unparseable_lines: list[tuple[int, str]]) -> None:
    """issue #11：覆盖前把含无法解析行的旧 ``.env`` 整体备份，告警只报行号。

    BUG-585：备份失败抛 :class:`ConfigError`（fail-closed），绝不无备份覆盖。
    """
    if not env_path.is_file():
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = env_path.with_name(f".env.lwa-backup-{stamp}")
    try:
        backup.write_text(env_path.read_text(encoding="utf-8"), encoding="utf-8")
        backup.chmod(0o600)
        log.warning(
            "旧 .env 第 %s 行无法按 KEY=VALUE 解析，不会进入新 .env/.env.local，"
            "已整体备份到 %s，请人工检查（issue #11）",
            "、".join(str(no) for no, _ in unparseable_lines),
            backup,
        )
    except OSError as exc:
        # BUG-585：fail-closed——备份失败就中止重生成，绝不在没有备份的
        # 情况下覆盖含无法解析行的旧 .env。
        raise ConfigError(f"备份旧 .env 失败，已中止重生成以保留原文件：{exc}") from exc


def _migrate_business_keys_to_env_local(
    docker_dir: Path, business_lines: list[tuple[str, str]]
) -> None:
    """把旧 ``.env`` 的业务键迁入 ``.env.local``（issue #11）。

    ``.env`` 与 ``.env.local`` 同在 compose env_file 列表中，注入语义不变；
    ``.env.local`` 已有同名键时**现有值优先**，仅报告冲突。日志只打印键名。
    BUG-585：读/写 ``.env.local`` 失败抛 :class:`ConfigError`（fail-closed），
    文件不存在按空处理。
    """
    local_path = docker_dir / ".env.local"
    existing_text = ""
    if local_path.is_file():
        try:
            existing_text = local_path.read_text(encoding="utf-8")
        except OSError as exc:
            # BUG-585：fail-closed——读不出已有 .env.local 时中止整个重生成，
            # 否则同名键去重依据丢失，覆盖 .env 后业务键可能两边都找不到。
            raise ConfigError(f"读取 .env.local 失败，已中止重生成以保留原文件：{exc}") from exc
    existing_keys = set(
        re.findall(r"(?m)^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", existing_text)
    )
    append_lines: list[str] = []
    migrated: list[str] = []
    conflicts: list[str] = []
    for key, raw_line in business_lines:
        if key in existing_keys:
            conflicts.append(key)
            continue
        existing_keys.add(key)
        append_lines.append(raw_line)
        migrated.append(key)
    if conflicts:
        log.warning(
            "业务键与 .env.local 已有键冲突，以 .env.local 现有值为准：%s",
            "、".join(conflicts),
        )
    if not append_lines:
        return
    merged = existing_text
    if merged and not merged.endswith("\n"):
        merged += "\n"
    merged += "# 以下键由 lwa 从 .env 迁移（issue #11）。\n" + "\n".join(append_lines) + "\n"
    try:
        _atomic_write_env(local_path, merged)
    except OSError as exc:
        # BUG-585：fail-closed——.env.local 写不进去时中止，.env 尚未覆盖。
        raise ConfigError(f"写入 .env.local 失败，已中止重生成以保留原文件：{exc}") from exc
    log.info("已迁移业务键至 .env.local（键名）：%s", "、".join(migrated))


def ensure_env_local_secrets(docker_dir: Path, env_example: Path | None = None) -> Path | None:
    """为空的 ``JWT_SECRET`` 生成持久值并写入 ``.env.local``（BUG-199）。

    issue #11：``.env.local`` 已存在时不再直接跳过--若其中尚无
    ``JWT_SECRET``（如刚被业务键迁移创建），改为**追加**而不是覆盖/跳过；
    已有 ``JWT_SECRET``（迁移带入或此前生成）时仍不代填。

    fail-closed（同 BUG-585）：``.env.local``/``.env.example`` 存在但读取
    失败时抛 ``ConfigError`` 中止，而不是静默跳过让应用带空密钥运行。
    """
    local_path = docker_dir / ".env.local"
    existing_text = ""
    if local_path.is_file():
        try:
            existing_text = local_path.read_text(encoding="utf-8")
        except OSError as exc:
            # fail-closed（同 BUG-585）：读不出就无法判断 JWT_SECRET 是否已存在，
            # 静默跳过会让应用带空密钥运行；中止并保留文件，交由用户修复权限。
            raise ConfigError(f"读取 .env.local 失败，已中止密钥补齐以保留原文件：{exc}") from exc
        # 与迁移侧键识别同口径（容忍 export 前缀/缩进）：迁移保留原始行，
        # ``export JWT_SECRET=…`` 必须视为已有 JWT_SECRET，否则会追加第二条
        # 同名键（compose env_file 后值覆盖前值，用户迁移值被静顶掉）。
        if re.search(r"(?m)^\s*(?:export\s+)?JWT_SECRET\s*=", existing_text):
            return None
    example = env_example if env_example is not None else docker_dir / ".env.example"
    if not example.is_file():
        return None
    try:
        text = example.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"读取 .env.example 失败，已中止密钥补齐：{exc}") from exc
    if not re.search(r"(?m)^JWT_SECRET=", text):
        return None
    # 已在 example 里填了非空值则不代填
    m = re.search(r"(?m)^JWT_SECRET=(.*)$", text)
    if m and m.group(1).strip():
        return None
    jwt = secrets.token_hex(32)
    if existing_text:
        # issue #11：追加而非覆盖（保留迁移进来的业务键）。
        content = existing_text.rstrip("\n") + f"\nJWT_SECRET={jwt}\n"
    else:
        content = (
            "# 由 lwa 自动生成（BUG-199）；可按 .env.example 补充其它密钥。\n"
            f"JWT_SECRET={jwt}\n"
        )
    _atomic_write_env(local_path, content)
    log.info("已生成业务密钥文件：%s（含 JWT_SECRET）", local_path)
    return local_path


def service_name() -> str:
    """返回 Compose 服务名（固定为 ``app``）。"""
    return _SERVICE_NAME


def _is_sqlite(manifest: InstanceManifest) -> bool:
    return bool(manifest.hasDatabase and manifest.database and manifest.database.type == "sqlite")


__all__ = [
    "generate_compose",
    "generate_env",
    "ensure_env_local_secrets",
    "container_data_paths",
    "service_name",
    "uses_runtime_root",
]
