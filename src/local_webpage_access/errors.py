"""统一异常类型与错误码。

所有模块抛出的业务异常都应继承 :class:`LwaError`，并携带稳定的 ``code`` 字段，
便于 CLI 层统一格式化输出，也便于大模型 skill 根据错误码决定修复路径。
"""

from __future__ import annotations

from typing import Any


class LwaError(Exception):
    """所有 Local Webpage Access（`lwa`）业务异常的基类。

    每个异常子类都声明一个稳定的 ``code``，用于在 CLI、管理页和 skill 之间传递错误类型。
    """

    code: str = "LWA_ERROR"

    def __init__(self, message: str = "", *, code: str | None = None, **context: Any) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.message = message
        self.context: dict[str, Any] = dict(context)

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}" if self.message else f"[{self.code}]"


# ---- 配置 / 路径 -----------------------------------------------------------


class ConfigError(LwaError):
    """配置文件缺失、格式错误或字段非法。"""

    code = "CONFIG_ERROR"


class PathError(LwaError):
    """路径解析失败，例如工作区根定位失败。"""

    code = "PATH_ERROR"


# ---- Schema / 模型 ---------------------------------------------------------


class SchemaError(LwaError):
    """``local-web.json`` 不符合 schema。"""

    code = "SCHEMA_ERROR"


# ---- Registry --------------------------------------------------------------


class RegistryError(LwaError):
    """SQLite registry 读写、迁移或约束错误。"""

    code = "REGISTRY_ERROR"


# ---- 端口 ------------------------------------------------------------------


class PortError(LwaError):
    """端口池耗尽或端口冲突。"""

    code = "PORT_ERROR"


# ---- 导入 ------------------------------------------------------------------


class ZipImportError(LwaError):
    """zip 导入失败：文件损坏、路径穿越、解压失败等。"""

    code = "ZIP_IMPORT_ERROR"


# ---- 识别 ------------------------------------------------------------------


class RecognitionError(LwaError):
    """项目扫描/识别过程中的错误。"""

    code = "RECOGNITION_ERROR"


# ---- 静态网关 --------------------------------------------------------------


class GatewayError(LwaError):
    """静态网关配置生成、reload 或健康检查错误。"""

    code = "GATEWAY_ERROR"


# ---- 构建 / Docker ---------------------------------------------------------


class BuildError(LwaError):
    """前端构建失败。"""

    code = "BUILD_ERROR"


class DockerError(LwaError):
    """Docker / Docker Compose 不可用或命令执行失败。"""

    code = "DOCKER_ERROR"


# ---- 生命周期 --------------------------------------------------------------


class LifecycleError(LwaError):
    """实例启停、重启、重建过程中的错误。"""

    code = "LIFECYCLE_ERROR"


class HostingError(LwaError):
    """静态托管流程中的错误（缺少 index.html、形态不支持等）。"""

    code = "HOSTING_ERROR"


__all__ = [
    "LwaError",
    "ConfigError",
    "PathError",
    "SchemaError",
    "RegistryError",
    "PortError",
    "ZipImportError",
    "RecognitionError",
    "GatewayError",
    "BuildError",
    "DockerError",
    "LifecycleError",
    "HostingError",
]
