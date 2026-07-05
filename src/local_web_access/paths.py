"""路径管理：统一生成工作区各子目录与实例目录的路径。

所有模块都应通过 :class:`Workspace` 访问路径，避免在代码里硬编码目录名。
目录布局对应 V1 设计说明第 7 节。
"""

from __future__ import annotations

from pathlib import Path

CONFIG_FILENAME = "local-web.yml"
REGISTRY_DB_FILENAME = "local-web.db"


class Workspace:
    """Local Web Access 工作区路径解析器。

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
    def manager(self) -> Path:
        return self.root / "manager"

    # ---- 实例目录 ----------------------------------------------------------

    def app_dir(self, instance_id: str) -> Path:
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
        return self.static_sites / f"{instance_id}.conf"

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
    from local_web_access.errors import PathError

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
    "find_workspace_root",
    "require_workspace",
]
