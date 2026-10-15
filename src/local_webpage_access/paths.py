"""路径管理：统一生成工作区各子目录与实例目录的路径。

所有模块都应通过 :class:`Workspace` 访问路径，避免在代码里硬编码目录名。
目录布局对应 V1 设计说明第 7 节。
"""

from __future__ import annotations

import os
import re
from contextvars import ContextVar, Token
from pathlib import Path

CONFIG_FILENAME = "local-web.yml"
REGISTRY_DB_FILENAME = "local-web.db"

# 合法实例 ID 即 importer.slugify 的输出：``[a-z0-9]+(-[a-z0-9]+)*``。
# 不含 ``.`` / ``/`` / ``\\``，杜绝 ``..``、绝对路径等穿越片段（BUG-025）。
# 评审-组7：`$` 会匹配结尾换行之前（"abc\n" 通过校验），改用 `\Z` 严格结尾
_INSTANCE_ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*\Z")

# IMP-006：路径别名 slug 与实例 ID 同形（小写字母 / 数字 / 连字符）。
_PATH_ALIAS_RE = _INSTANCE_ID_RE
_PATH_ALIAS_MAX_LEN = 63  # DNS 标签上限，路径别名同理

# IMP-006：保留字——会被网关入口、管理页 API 路由或工作区顶层目录占用。
# 即使别名入口（staticGatewayPort）与管理页（managerPort）端口不同，
# 仍拒绝这些别名，避免用户混淆并预留未来端口合并的余地。
_PATH_ALIAS_RESERVED = frozenset(
    {
        "api",
        "static-gateway",
        "inbox",
        "apps",
        "registry",
        "run",
        "manager",
        "logs",
        "skills",
        "templates",
        "health",
    }
)


def validate_instance_id(instance_id: str) -> str:
    """校验实例 ID 是安全 slug，拒绝路径穿越（BUG-025）。

    所有 ``app_*`` 路径与生命周期锁都从 ``instance_id`` 拼接而来；若放行
    ``..`` / ``/`` / 绝对路径，``shutil.rmtree(app_dir(".."))`` 会越界删除
    工作区根。合法 ID 由 :func:`importer.slugify` 生成，必匹配此正则。
    非法 ID 抛 :class:`PathError`。
    """
    from local_webpage_access.errors import PathError

    if not isinstance(instance_id, str) or not _INSTANCE_ID_RE.match(instance_id):
        raise PathError(
            f"非法实例 ID：{instance_id!r}（仅允许小写字母、数字与连字符）",
        )
    return instance_id


def validate_path_alias(
    alias: str,
    *,
    existing_aliases: set[str] | None = None,
) -> str:
    """校验路径别名 slug（IMP-006）。

    规则：
    - 格式：``^[a-z0-9]+(-[a-z0-9]+)*$``（与实例 ID 同形），长度 ≤ 63；
    - 保留字：见 :data:`_PATH_ALIAS_RESERVED`，拒绝以避免与网关入口、管理页
      API 路由、工作区顶层目录冲突；
    - 全局唯一：``existing_aliases`` 为已占用的别名集合（通常由 registry
      扫描得出），命中即拒。``existing_aliases`` 为 ``None`` 时跳过唯一性检查
      （调用方需自行保证）。

    校验通过返回原值；失败抛 :class:`PathError`。本函数为纯函数（无 I/O），
    唯一性数据由调用方注入，避免 paths ↔ registry 的循环依赖。
    """
    from local_webpage_access.errors import PathError

    if not isinstance(alias, str) or not _PATH_ALIAS_RE.match(alias):
        raise PathError(
            f"非法路径别名：{alias!r}（仅允许小写字母、数字与连字符）",
        )
    if len(alias) > _PATH_ALIAS_MAX_LEN:
        raise PathError(
            f"路径别名过长：{len(alias)} > {_PATH_ALIAS_MAX_LEN}",
        )
    if alias in _PATH_ALIAS_RESERVED:
        raise PathError(
            f"路径别名 {alias!r} 是保留字，请换一个",
        )
    if existing_aliases is not None and alias in existing_aliases:
        raise PathError(
            f"路径别名 {alias!r} 已被其他实例占用",
        )
    return alias


def validate_source_subdir(source_subdir: str | None) -> str | None:
    """校验 ``sourceSubdir`` 是 ``current/`` 内的相对路径（BUG-507）。

    ``None`` / 空白视为根目录（返回 ``None``）。拒绝绝对路径、盘符、
    NUL、以及任何 ``..`` 段。通过则返回规范化的 POSIX 相对路径。
    """
    from local_webpage_access.errors import PathError

    if source_subdir is None:
        return None
    if not isinstance(source_subdir, str):
        raise PathError(f"非法 sourceSubdir：{source_subdir!r}")
    raw = source_subdir.strip()
    if not raw:
        return None
    if "\x00" in raw:
        raise PathError(f"非法 sourceSubdir：{source_subdir!r}")
    normalized = raw.replace("\\", "/")
    if Path(raw).is_absolute() or Path(normalized).is_absolute() or normalized.startswith("/"):
        raise PathError(
            f"sourceSubdir 必须是 current 内的相对路径，收到 {source_subdir!r}",
        )
    if len(normalized) >= 2 and normalized[1] == ":":
        raise PathError(
            f"sourceSubdir 必须是 current 内的相对路径，收到 {source_subdir!r}",
        )
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if not parts:
        return None
    if any(p == ".." for p in parts):
        raise PathError(
            f"sourceSubdir 不得包含 '..'，收到 {source_subdir!r}",
        )
    return "/".join(parts)


def frontend_artifact_dest(copy_prefix: str, source_dir: Path | None) -> str:
    """前端构建产物在镜像内的目标目录（#47 / PLN-052 S1-02）。

    产物写入后端代码目录下的 ``static/``；后端代码在镜像内的位置由拷贝前缀决定：

    - 展平布局（前缀 ``current/<sub>/``）：后端位于 ``/app`` → ``/app/static``；
    - 根布局且源码根下有 ``backend/``：后端位于 ``/app/backend`` → ``/app/backend/static``；
    - 其他（根布局无 ``backend/``）：``/app/static``。

    只推导路径，不删除目录：后端可能在 ``static/`` 中放有自己的文件，调用方用
    ``cp -a`` 覆盖同名文件，不做 ``rm -rf``。
    """
    prefix = (copy_prefix or "current/").rstrip("/")
    if prefix == "current" and source_dir is not None and (source_dir / "backend").is_dir():
        return "/app/backend/static"
    return "/app/static"


def resolve_source_workdir(base: Path, source_subdir: str | None) -> Path:
    """把 ``sourceSubdir`` 解析为 ``base`` 内的工作目录（BUG-507）。

    安全但目录不存在时回退 ``base``（与历史 hosting 行为一致）。
    绝对路径、``..``、符号链接逃逸抛 :class:`PathError`。
    """
    from local_webpage_access.errors import PathError

    base_resolved = Path(base).resolve()
    sanitized = validate_source_subdir(source_subdir)
    if sanitized is None:
        return base_resolved
    candidate = (base_resolved / sanitized).resolve()
    if not candidate.is_relative_to(base_resolved):
        raise PathError(
            f"sourceSubdir {source_subdir!r} 越出源码目录 {base_resolved}",
        )
    if candidate.is_dir():
        return candidate
    return base_resolved


class Workspace:
    """Local Webpage Access 工作区路径解析器。

    工作区根目录是 ``lwa init`` 创建的目录，包含 inbox/、apps/、registry/ 等。
    """

    def __init__(self, root: Path) -> None:
        self.root: Path = Path(root).resolve()

    # ---- 顶层目录 ----------------------------------------------------------

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_FILENAME

    @property
    def inbox(self) -> Path:
        return self.root / "inbox"

    @property
    def inbox_processed(self) -> Path:
        """IMP-011：daemon 处理成功的 zip 归档目录（``inbox/processed/``）。

        import 成功后 zip 移入此处（同名加时间戳），从扫描视野中物理移除，
        替代旧"留在 inbox + 指纹表去重"机制，杜绝归档后按旧指纹重复导入。
        位于 ``inbox/`` 下，但 :func:`scan_inbox` 用非递归 glob 不会扫到它。
        """
        return self.inbox / "processed"

    @property
    def inbox_failed(self) -> Path:
        """BUG-297：daemon 连续失败超阈值的 zip 死信目录（``inbox/failed/``）。

        物理移出扫描视野，避免损坏 zip 每 poll 无限重试撑爆日志。
        """
        return self.inbox / "failed"

    @property
    def apps(self) -> Path:
        return self.root / "apps"

    @property
    def registry_dir(self) -> Path:
        return self.root / "registry"

    @property
    def db_path(self) -> Path:
        return self.registry_dir / REGISTRY_DB_FILENAME

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def run(self) -> Path:
        return self.root / "run"

    @property
    def templates(self) -> Path:
        return self.root / "templates"

    @property
    def skills(self) -> Path:
        return self.root / "skills"

    @property
    def static_gateway(self) -> Path:
        return self.root / "static-gateway"

    @property
    def static_sites(self) -> Path:
        return self.static_gateway / "sites"

    @property
    def static_aliases(self) -> Path:
        """IMP-006：路径别名路由片段目录（``static-gateway/aliases/``）。

        每个有 path-alias 的实例对应一个 ``<id>.conf`` 片段，由主 Caddyfile
        的统一入口块 ``import`` 进去。无别名时该目录可为空。
        """
        return self.static_gateway / "aliases"

    @property
    def manager(self) -> Path:
        return self.root / "manager"

    # ---- 实例目录 ----------------------------------------------------------

    def app_dir(self, instance_id: str) -> Path:
        validate_instance_id(instance_id)
        return self.apps / instance_id

    def app_source(self, instance_id: str) -> Path:
        return self.app_dir(instance_id) / "source"

    def app_original_zip(self, instance_id: str) -> Path:
        return self.app_source(instance_id) / "original.zip"

    def app_current(self, instance_id: str) -> Path:
        return self.app_dir(instance_id) / "current"

    def app_public(self, instance_id: str) -> Path:
        return self.app_dir(instance_id) / "public"

    def app_data(self, instance_id: str) -> Path:
        return self.app_dir(instance_id) / "data"

    def app_logs(self, instance_id: str) -> Path:
        return self.app_dir(instance_id) / "logs"

    def app_docker(self, instance_id: str) -> Path:
        return self.app_dir(instance_id) / "docker"

    def app_compose_path(self, instance_id: str) -> Path:
        return self.app_docker(instance_id) / "compose.yaml"

    def app_dockerfile_path(self, instance_id: str) -> Path:
        return self.app_docker(instance_id) / "Dockerfile"

    def app_env_path(self, instance_id: str) -> Path:
        return self.app_docker(instance_id) / ".env"

    def app_manifest_path(self, instance_id: str) -> Path:
        return self.app_dir(instance_id) / "local-web.json"

    def app_gateway_config(self, instance_id: str) -> Path:
        validate_instance_id(instance_id)
        return self.static_sites / f"{instance_id}.conf"

    def app_alias_config(self, instance_id: str) -> Path:
        """IMP-006：实例路径别名路由片段路径（``static-gateway/aliases/<id>.conf``）。"""
        validate_instance_id(instance_id)
        return self.static_aliases / f"{instance_id}.conf"

    # ---- 创建目录 ----------------------------------------------------------

    def ensure_workspace_dirs(self) -> None:
        """创建所有顶层工作区目录（幂等）。"""
        for directory in (
            self.inbox,
            self.inbox_processed,
            self.inbox_failed,
            self.apps,
            self.registry_dir,
            self.logs,
            self.run,
            self.templates,
            self.skills,
            self.static_sites,
            self.manager,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def ensure_app_dirs(self, instance_id: str) -> None:
        """创建单个实例的完整目录结构（幂等）。"""
        for directory in (
            self.app_source(instance_id),
            self.app_current(instance_id),
            self.app_public(instance_id),
            self.app_data(instance_id),
            self.app_logs(instance_id),
            self.app_docker(instance_id),
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def __repr__(self) -> str:
        return f"Workspace(root={self.root!s})"


def find_workspace_root(start: Path | None = None) -> Path | None:
    """从 ``start``（默认当前目录）向上查找包含 ``local-web.yml`` 的目录。

    返回工作区根目录；找不到时返回 ``None``。不读取 ``--workspace``、
    ``LWA_WORKSPACE`` 或已保存的默认工作区。
    """
    current = (Path(start) if start else Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / CONFIG_FILENAME).is_file():
            return candidate
    return None


# CLI 入口在执行子命令前设置。库代码与测试默认关闭，避免误用用户家目录里的默认工作区。
_explicit_workspace: ContextVar[Path | None] = ContextVar("lwa_explicit_workspace", default=None)
_use_saved_default: ContextVar[bool] = ContextVar("lwa_use_saved_default", default=False)
ENV_WORKSPACE = "LWA_WORKSPACE"


def push_cli_workspace(explicit: str | None) -> tuple[Token[Path | None], Token[bool]]:
    """记录本次 CLI 调用的显式工作区，并允许回退到已保存的默认值。"""
    path = None
    if explicit is not None and explicit.strip():
        path = Path(explicit).expanduser().resolve()
    return _explicit_workspace.set(path), _use_saved_default.set(True)


def pop_cli_workspace(tokens: tuple[Token[Path | None], Token[bool]]) -> None:
    """子命令结束后恢复工作区选择，避免污染同一进程里的后续调用。"""
    explicit_token, default_token = tokens
    _explicit_workspace.reset(explicit_token)
    _use_saved_default.reset(default_token)


def default_workspace_config_path() -> Path:
    """用户级默认工作区文件（不放在某个 runtime 里面）。"""
    raw = os.environ.get("XDG_CONFIG_HOME")
    base = Path(raw).expanduser() if raw else Path.home() / ".config"
    return base / "lwa" / "default-workspace"


def read_saved_default_workspace() -> tuple[Path | None, str]:
    """返回 ``(路径, 来源)``。``LWA_WORKSPACE`` 优先于配置文件。"""
    env = os.environ.get(ENV_WORKSPACE, "").strip()
    if env:
        return Path(env).expanduser().resolve(), ENV_WORKSPACE
    path = default_workspace_config_path()
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None, ""
    if not text:
        return None, ""
    return Path(text).expanduser().resolve(), str(path)


def write_default_workspace(root: Path) -> Path:
    """把默认工作区写成绝对路径。调用方须先确认该目录已初始化。"""
    dest = default_workspace_config_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(str(root.expanduser().resolve()) + "\n", encoding="utf-8")
    dest.chmod(0o600)
    return dest


def rebind_saved_default_workspace(old: Path, new: Path) -> bool:
    """迁移/回滚时只同步文件中恰好指向旧根的默认值，不受环境变量覆盖影响。"""
    path = default_workspace_config_path()
    try:
        saved = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return False
    if not saved or Path(saved).expanduser().resolve() != old.expanduser().resolve():
        return False
    write_default_workspace(new)
    return True


def clear_default_workspace() -> bool:
    """删除已保存的默认工作区文件。不存在时返回 False。"""
    path = default_workspace_config_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def locate_workspace_root(start: Path | None = None) -> Path | None:
    """定位工作区：显式路径、当前目录向上、再是 CLI 默认值。

    ``start`` 非空时只从该路径向上查找（``lwa update --workspace`` 的既有语义）。
    """
    if start is None:
        explicit = _explicit_workspace.get()
        if explicit is not None:
            return explicit if (explicit / CONFIG_FILENAME).is_file() else None
    found = find_workspace_root(start)
    if found is not None:
        return found
    if start is None and _use_saved_default.get():
        saved, _source = read_saved_default_workspace()
        if saved is not None and (saved / CONFIG_FILENAME).is_file():
            return saved
    return None


def _cli_missing_workspace_message() -> str:
    return (
        "未找到工作区：当前目录及其上级没有 local-web.yml，也没有可用的默认工作区。\n"
        "已有工作区请执行：lwa --workspace <工作区绝对路径> <命令>\n"
        "或先记住它：lwa workspace use <工作区绝对路径>\n"
        f"也可以设置环境变量 {ENV_WORKSPACE}。\n"
        "不要在项目目录里执行 lwa init，那会新建另一套工作区。"
    )


def require_workspace(start: Path | None = None) -> Workspace:
    """查找工作区根，找不到时抛 :class:`PathError`。"""
    from local_webpage_access.errors import PathError

    if start is None:
        explicit = _explicit_workspace.get()
        if explicit is not None:
            if not (explicit / CONFIG_FILENAME).is_file():
                raise PathError(
                    f"--workspace 指向的目录不是已初始化工作区（缺少 {CONFIG_FILENAME}）："
                    f"{explicit}。\n"
                    "请改成已有工作区的绝对路径。不要对这个路径执行 lwa init，除非确实要新建一套。"
                )
            return Workspace(explicit)
    found = find_workspace_root(start)
    if found is not None:
        return Workspace(found)
    if start is not None:
        raise PathError(
            f"从 {Path(start).expanduser().resolve()} 向上未找到 {CONFIG_FILENAME}。"
        )
    if _use_saved_default.get():
        saved, source = read_saved_default_workspace()
        if saved is not None:
            if (saved / CONFIG_FILENAME).is_file():
                return Workspace(saved)
            raise PathError(
                f"{source} 不可用（缺少 {CONFIG_FILENAME}）：{saved}。"
                "请重新执行 lwa workspace use <工作区绝对路径>。"
            )
        raise PathError(_cli_missing_workspace_message())
    raise PathError(f"未找到工作区（缺少 {CONFIG_FILENAME}）。")


__all__ = [
    "Workspace",
    "CONFIG_FILENAME",
    "REGISTRY_DB_FILENAME",
    "validate_instance_id",
    "validate_path_alias",
    "validate_source_subdir",
    "resolve_source_workdir",
    "find_workspace_root",
    "locate_workspace_root",
    "require_workspace",
    "push_cli_workspace",
    "pop_cli_workspace",
    "read_saved_default_workspace",
    "write_default_workspace",
    "clear_default_workspace",
    "default_workspace_config_path",
    "ENV_WORKSPACE",
]
