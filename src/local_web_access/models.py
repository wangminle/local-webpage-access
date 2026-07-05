"""``local-web.json`` 实例元数据模型与读写。

对应 WBS-04 与 V1 设计说明第 8 节。

字段术语与取值表（设计 §8.0）：
- ``kind``：项目技术族 ``static`` / ``node`` / ``python``
- ``runtime``：底层运行机制 ``shared-static`` / ``docker-compose``
- ``servingMode``：对外服务方式 ``shared-static`` / ``container``
- ``resourceProfile``：资源档位 ``tiny`` / ``small`` / ``medium`` / ``heavy``
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from local_web_access.errors import SchemaError
from local_web_access.logging import now_iso

SCHEMA_VERSION = 1

# ---- 枚举（用 str + Enum 便于序列化）---------------------------------------


class Kind(str, Enum):
    STATIC = "static"
    NODE = "node"
    PYTHON = "python"


class Runtime(str, Enum):
    SHARED_STATIC = "shared-static"
    DOCKER_COMPOSE = "docker-compose"


class ServingMode(str, Enum):
    SHARED_STATIC = "shared-static"
    CONTAINER = "container"


class ResourceProfile(str, Enum):
    TINY = "tiny"
    SMALL = "small"
    MEDIUM = "medium"
    HEAVY = "heavy"


class DesiredState(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"


class Status(str, Enum):
    PENDING = "pending"
    BUILDING = "building"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"
    QUEUED = "queued"


class RouteMode(str, Enum):
    PORT = "port"
    NAME = "name"


# ---- 子模型 -----------------------------------------------------------------


class DatabaseConfig(BaseModel):
    """数据库描述。V1 主要支持 SQLite，其余只识别标记。"""

    model_config = ConfigDict(extra="allow")

    type: str  # sqlite / postgres / mysql / redis / unknown
    connectionString: str | None = None
    dataDir: str | None = None


class ResourceLimits(BaseModel):
    memory: str = "512m"
    cpus: str = "0.75"


class StaticConfig(BaseModel):
    """静态托管配置。"""

    model_config = ConfigDict(extra="allow")

    root: str = "public"
    gateway: str = "caddy"
    routeMode: str = RouteMode.PORT.value
    routeHost: str | None = None
    gatewayConfigPath: str | None = None
    hostPort: int | None = None
    enabled: bool = True


class ContainerConfig(BaseModel):
    """Docker Compose 容器配置。"""

    model_config = ConfigDict(extra="allow")

    projectName: str
    serviceName: str = "app"
    image: str | None = None
    imageId: str | None = None
    containerId: str | None = None
    internalPort: int
    hostPort: int | None = None
    composePath: str
    dockerfilePath: str
    resourceLimits: ResourceLimits = Field(default_factory=ResourceLimits)


class NetworkConfig(BaseModel):
    """访问入口配置。"""

    model_config = ConfigDict(extra="allow")

    host: str = "0.0.0.0"
    internalPort: int | None = None
    hostPort: int | None = None
    routeMode: str = RouteMode.PORT.value
    routeHost: str | None = None
    lanUrl: str | None = None
    healthUrl: str | None = None


class EntryConfig(BaseModel):
    """安装 / 构建 / 启动命令推断结果。"""

    model_config = ConfigDict(extra="allow")

    install: str | None = None
    build: str | None = None
    start: str | None = None


# ---- 主模型 -----------------------------------------------------------------


class InstanceManifest(BaseModel):
    """实例元数据合同，对应 ``local-web.json``。

    这是 CLI、管理页、静态网关、Docker Compose 和大模型 skill 共同读取的真相文件。
    """

    model_config = ConfigDict(extra="allow", use_enum_values=False)

    schemaVersion: int = SCHEMA_VERSION
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    kind: Kind
    stack: list[str] = Field(default_factory=list)
    runtime: Runtime
    servingMode: ServingMode
    resourceProfile: ResourceProfile = ResourceProfile.SMALL
    hasDatabase: bool = False
    database: DatabaseConfig | None = None
    desiredState: DesiredState = DesiredState.STOPPED
    status: Status = Status.PENDING
    static: StaticConfig | None = None
    container: ContainerConfig | None = None
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    entry: EntryConfig = Field(default_factory=EntryConfig)
    sourceZipPath: str | None = None
    appPath: str | None = None
    createdAt: str = Field(default_factory=now_iso)
    updatedAt: str = Field(default_factory=now_iso)
    lastStartedAt: str | None = None
    lastHealthCheckAt: str | None = None
    lastError: str | None = None

    @field_validator("kind", "runtime", "servingMode", "resourceProfile", "desiredState", "status")
    @classmethod
    def _coerce_enum(cls, v: Any) -> Any:
        return v

    @model_validator(mode="after")
    def _check_runtime_serving_consistency(self) -> InstanceManifest:
        """runtime 与 servingMode 一致性，以及 static/container 的存在性。"""
        rt = self.runtime.value if isinstance(self.runtime, Runtime) else self.runtime
        sm = (
            self.servingMode.value
            if isinstance(self.servingMode, ServingMode)
            else self.servingMode
        )

        if rt == Runtime.SHARED_STATIC.value:
            if sm != ServingMode.SHARED_STATIC.value:
                raise ValueError(
                    f"runtime=shared-static 时 servingMode 必须为 shared-static，得到 {sm!r}",
                )
            if self.container is not None:
                raise ValueError("runtime=shared-static 时不应有 container 配置")
        elif rt == Runtime.DOCKER_COMPOSE.value:
            if sm != ServingMode.CONTAINER.value:
                raise ValueError(
                    f"runtime=docker-compose 时 servingMode 必须为 container，得到 {sm!r}",
                )
            if self.container is None:
                raise ValueError("runtime=docker-compose 时必须有 container 配置")
        return self

    @model_validator(mode="after")
    def _check_database_consistency(self) -> InstanceManifest:
        if self.hasDatabase and self.database is None:
            raise ValueError("hasDatabase=true 时 database 不能为空")
        if not self.hasDatabase and self.database is not None:
            raise ValueError("database 非空时 hasDatabase 应为 true")
        return self

    # ---- 便捷方法 ----------------------------------------------------------

    def touch(self) -> None:
        """更新 updatedAt 时间戳。"""
        self.updatedAt = now_iso()

    def to_dict(self) -> dict[str, Any]:
        """序列化为可写入 JSON 的 dict，枚举转为值。"""
        return self.model_dump(mode="json")

    # ---- IO ----------------------------------------------------------------

    def save(self, path: Path) -> None:
        """写入 ``local-web.json``（美化格式）。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.to_dict()
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> InstanceManifest:
        """从 ``local-web.json`` 读取并校验。"""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SchemaError(
                f"local-web.json 解析失败：{path}",
                path=str(path),
            ) from exc
        except OSError as exc:
            raise SchemaError(f"local-web.json 读取失败：{path}", path=str(path)) from exc
        return cls.from_dict(raw, path=path)

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], *, path: Path | None = None
    ) -> InstanceManifest:
        try:
            return cls.model_validate(data)
        except ValueError as exc:
            raise SchemaError(
                f"local-web.json schema 校验失败：{exc}",
                path=str(path) if path else None,
            ) from exc


def migrate_manifest(data: dict[str, Any]) -> dict[str, Any]:
    """迁移预留：未来 schemaVersion 升级时在这里做向前迁移。

    V1 当前只有 schemaVersion=1，直接返回。迁移规则：
    - 迁移只增不删，保持向后兼容。
    - 每次升级 schemaVersion 时新增一个分支。
    """
    version = data.get("schemaVersion", 1)
    if version == SCHEMA_VERSION:
        return data
    # 未来：if version == 1: data = _migrate_1_to_2(data)
    raise SchemaError(f"不支持的 schemaVersion={version}，当前支持 {SCHEMA_VERSION}")


__all__ = [
    "SCHEMA_VERSION",
    "Kind",
    "Runtime",
    "ServingMode",
    "ResourceProfile",
    "DesiredState",
    "Status",
    "RouteMode",
    "DatabaseConfig",
    "ResourceLimits",
    "StaticConfig",
    "ContainerConfig",
    "NetworkConfig",
    "EntryConfig",
    "InstanceManifest",
    "migrate_manifest",
]
