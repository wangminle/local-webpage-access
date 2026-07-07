"""全局配置：定义 ``local-web.yml`` 结构、默认值和加载逻辑。

对应 WBS-02。配置字段与 V1 设计说明第 6、13、16 节保持一致。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from local_webpage_access.errors import ConfigError
from local_webpage_access.paths import Workspace

MANAGER_PORT_DEFAULT = 17800
PORT_POOL_START_DEFAULT = 18000
PORT_POOL_END_DEFAULT = 19999
# IMP-006：路径别名统一入口端口默认值。8080 避开端口池（18000-19999）与
# 管理页（17800），且无需特权绑定（80 需要 root/cap_net_bind_service）。
# 该端口仅在存在已启用别名时才被 Caddy 占用。
STATIC_GATEWAY_PORT_DEFAULT = 8080


class PortPool(BaseModel):
    """端口池范围。"""

    start: int = Field(default=PORT_POOL_START_DEFAULT, ge=1, le=65535)
    end: int = Field(default=PORT_POOL_END_DEFAULT, ge=1, le=65535)

    @model_validator(mode="after")
    def _check_range(self) -> PortPool:
        if self.start > self.end:
            raise ValueError(f"端口池 start({self.start}) 不能大于 end({self.end})")
        if self.end - self.start < 10:
            raise ValueError("端口池范围过小，至少需要 10 个端口")
        return self

    def as_range(self) -> range:
        return range(self.start, self.end + 1)

    def __len__(self) -> int:
        return self.end - self.start + 1


class ResourceLimits(BaseModel):
    """默认容器资源限制。"""

    memory: str = "512m"
    cpus: str = "0.75"


class StaticRateLimit(BaseModel):
    """静态站点访问频率限制（IMP-005，内网防护）。

    默认关闭；仅在 Caddy 含 ``http.handlers.rate_limit`` 模块时生效。
    builtin 模式不支持限流。

    * ``rps`` —— 每客户端每秒平均请求数（稳态补充速率）；
    * ``burst`` —— 令牌桶容量（允许的瞬时突发请求数）。
    两者映射为 Caddy ``rate_limit`` 指令的 ``events=burst`` / ``window=burst/rps``。
    """

    enabled: bool = False
    rps: int = Field(default=3, ge=1, le=10000)
    burst: int = Field(default=6, ge=1, le=100000)


class Config(BaseModel):
    """``local-web.yml`` 的完整配置模型。"""

    managerPort: int = Field(default=MANAGER_PORT_DEFAULT, ge=1, le=65535)
    managerHost: str = "0.0.0.0"
    managerEnabled: bool = True
    portPool: PortPool = Field(default_factory=PortPool)
    staticGateway: str = "caddy"
    # IMP-006：路径别名统一入口端口。仅当存在已启用的别名时，Caddy 才会在该
    # 端口上监听并按 ``/<alias>/*`` 反向代理到各实例 hostPort；无别名时该端口
    # 不被占用。``None`` 表示彻底关闭别名入口（即便实例配置了 alias 也只走端口）。
    staticGatewayPort: int | None = Field(default=STATIC_GATEWAY_PORT_DEFAULT, ge=1, le=65535)
    buildConcurrency: int = Field(default=1, ge=1, le=8)
    defaultResourceLimits: ResourceLimits = Field(default_factory=ResourceLimits)
    staticRateLimit: StaticRateLimit = Field(default_factory=StaticRateLimit)
    lanIpStrategy: str = "auto"
    manualLanIp: str | None = None
    logLevel: str = "INFO"

    @field_validator("staticGateway")
    @classmethod
    def _validate_gateway(cls, v: str) -> str:
        allowed = {"caddy", "nginx", "builtin"}
        if v not in allowed:
            raise ValueError(f"staticGateway 必须是 {allowed} 之一，得到 {v!r}")
        return v

    @field_validator("logLevel")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"logLevel 必须是 {allowed} 之一，得到 {v!r}")
        return upper

    @field_validator("lanIpStrategy")
    @classmethod
    def _validate_lan_strategy(cls, v: str) -> str:
        allowed = {"auto", "manual"}
        if v not in allowed:
            raise ValueError(f"lanIpStrategy 必须是 {allowed} 之一，得到 {v!r}")
        return v

    @model_validator(mode="after")
    def _check_manager_port_not_in_pool(self) -> Config:
        if self.portPool.start <= self.managerPort <= self.portPool.end:
            raise ValueError(
                f"管理页端口 {self.managerPort} 不能落在端口池 "
                f"[{self.portPool.start}, {self.portPool.end}] 内",
            )
        if self.lanIpStrategy == "manual" and not self.manualLanIp:
            raise ValueError("lanIpStrategy=manual 时必须提供 manualLanIp")
        # IMP-006：别名入口端口不能与管理页或端口池冲突，否则 Caddy 会与
        # 已有监听者抢端口导致 reload 失败。
        if self.staticGatewayPort is not None:
            if self.staticGatewayPort == self.managerPort:
                raise ValueError(
                    f"staticGatewayPort({self.staticGatewayPort}) 不能与管理页端口"
                    f"({self.managerPort}) 相同",
                )
            if self.portPool.start <= self.staticGatewayPort <= self.portPool.end:
                raise ValueError(
                    f"staticGatewayPort({self.staticGatewayPort}) 不能落在端口池 "
                    f"[{self.portPool.start}, {self.portPool.end}] 内",
                )
        return self

    # ---- 加载 / 序列化 -----------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        try:
            return cls.model_validate(data)
        except ValueError as exc:
            raise ConfigError(f"配置校验失败：{exc}", raw=data) from exc

    @classmethod
    def from_file(cls, path: Path) -> Config:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"配置文件 YAML 解析失败：{path}", path=str(path)) from exc
        except OSError as exc:
            raise ConfigError(f"配置文件读取失败：{path}", path=str(path)) from exc
        if not isinstance(raw, dict):
            raise ConfigError(
                f"配置文件顶层必须是映射/字典，得到 {type(raw).__name__}",
                path=str(path),
            )
        return cls.from_dict(raw)

    def to_yaml(self) -> str:
        data = self.model_dump()
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_yaml(), encoding="utf-8")


def default_config() -> Config:
    """返回默认配置实例。"""
    return Config()


def load_config(workspace: Workspace) -> Config:
    """从工作区加载配置；配置文件不存在时返回默认配置。"""
    if workspace.config_path.is_file():
        return Config.from_file(workspace.config_path)
    return default_config()


def example_config_text() -> str:
    """返回用于写入 ``local-web.yml`` 的示例文本（带注释）。"""
    return CONFIG_EXAMPLE


CONFIG_EXAMPLE = """\
# Local Webpage Access 配置文件
# 由 lwa init 生成。字段含义见 docs/plan/local-webpage-access-v1-design-20260704.md。

# 管理页监听端口（不应落在端口池范围内）
managerPort: 17800
managerHost: 0.0.0.0

# 是否在 lwa init 后自动后台启动管理页（false 时需手动 lwa manager on）
managerEnabled: true

# 实例端口池
portPool:
  start: 18000
  end: 19999

# 静态网关实现：caddy | nginx | builtin
staticGateway: caddy

# 路径别名统一入口端口（IMP-006）。仅当存在已启用的 --path-alias 时，Caddy
# 才在此端口监听并按 /<alias>/ 反向代理到各实例 hostPort。设为 null 关闭别名入口。
staticGatewayPort: 8080

# 构建并发数（小主机建议保持 1）
buildConcurrency: 1

# 容器默认资源限制
defaultResourceLimits:
  memory: 512m
  cpus: "0.75"

# 静态站点访问频率限制（IMP-005，内网防护；默认关闭）
# 仅在 Caddy 含 http.handlers.rate_limit 模块时生效；builtin 模式不支持。
staticRateLimit:
  enabled: false
  rps: 3        # 每客户端每秒平均请求数
  burst: 6      # 令牌桶容量（瞬时突发上限）

# 局域网 IP 获取策略：auto（自动探测）| manual（手动指定）
lanIpStrategy: auto
manualLanIp: null

# 日志级别
logLevel: INFO
"""


__all__ = [
    "Config",
    "PortPool",
    "ResourceLimits",
    "StaticRateLimit",
    "default_config",
    "load_config",
    "example_config_text",
    "CONFIG_EXAMPLE",
    "MANAGER_PORT_DEFAULT",
    "PORT_POOL_START_DEFAULT",
    "PORT_POOL_END_DEFAULT",
    "STATIC_GATEWAY_PORT_DEFAULT",
]
