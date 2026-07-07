"""路径管理：统一生成工作区各子目录与实例目录的路径。

所有模块都应通过 :class:`Workspace` 访问路径，避免在代码里硬编码目录名。
目录布局对应 V1 设计说明第 7 节。
"""

from __future__ import annotations

import re
from pathlib import Path

CONFIG_FILENAME = "local-web.yml"
REGISTRY_DB_FILENAME = "local-web.db"

# 合法实例 ID 即 importer.slugify 的输出：``[a-z0-9]+(-[a-z0-9]+)*``。
# 不含 ``.`` / ``/`` / ``\\``，杜绝 ``..``、绝对路径等穿越片段（BUG-025）。
_INSTANCE_ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# IMP-006：路径别名 slug 与实例 ID 同形（小写字母 / 数字 / 连字符）。
_PATH_ALIAS_RE = _INSTANCE_ID_RE
_PATH_ALIAS_MAX_LEN = 63  # DNS 标签上限，路径别名同理

# IMP-006：保留字——会被网关入口、管理页 API 路由或工作区顶层目录占用。
# 即使别名入口（staticGatewayPort）与管理页（managerPort）端口不同，
# 仍拒绝这些别名，避免用户混淆并预留未来端口合并的余地。
_PATH_ALIAS_RESERVED = frozenset({
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
})


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

    返回工作区根目录；找不到时返回 ``None``。
    """
    current = (Path(start) if start else Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / CONFIG_FILENAME).is_file():
            return candidate
    return None


def require_workspace(start: Path | None = None) -> Workspace:
    """查找工作区根，找不到时抛 :class:`PathError`。"""
    from local_webpage_access.errors import PathError

    root = find_workspace_root(start)
    if root is None:
        raise PathError(
            "未找到工作区（缺少 local-web.yml）。请先在目标目录执行 `lwa init`。",
        )
    return Workspace(root)


__all__ = [
    "Workspace",
    "CONFIG_FILENAME",
    "REGISTRY_DB_FILENAME",
    "validate_instance_id",
    "validate_path_alias",
    "find_workspace_root",
    "require_workspace",
]
