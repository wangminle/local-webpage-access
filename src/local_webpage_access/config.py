"""全局配置：定义 ``local-web.yml`` 结构、默认值和加载逻辑。

对应 WBS-02。配置字段与 V1 设计说明第 6、13、16 节保持一致。
"""

from __future__ import annotations

import ipaddress
import math
import re
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

    @field_validator("memory")
    @classmethod
    def _validate_memory(cls, value: str) -> str:
        normalized = str(value).strip().lower()
        if not re.fullmatch(r"[1-9]\d*(?:[kmgt]i?b?|b)?", normalized):
            raise ValueError("memory 必须是正整数加可选单位，如 512m、1g")
        return normalized

    @field_validator("cpus")
    @classmethod
    def _validate_cpus(cls, value: str) -> str:
        normalized = str(value).strip()
        try:
            numeric = float(normalized)
        except ValueError as exc:
            raise ValueError("cpus 必须是正数") from exc
        if not math.isfinite(numeric) or numeric <= 0:
            raise ValueError("cpus 必须是有限正数")
        return normalized


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


# BUG-200：容器构建期国内镜像（与 install-docker 阿里云默认口径一致）。
_CHINA_PIP = "https://mirrors.aliyun.com/pypi/simple/"
_CHINA_NPM = "https://registry.npmmirror.com"
_CHINA_NODE_DIST = "https://mirrors.aliyun.com/nodejs-release"
_CHINA_APT = "mirrors.aliyun.com"
# issue #35：阿里主源失败后切清华，再回落官方。清华在国内大包场景实测更稳。
_CHINA_APT_FALLBACKS = ("mirrors.tuna.tsinghua.edu.cn", "deb.debian.org")
# issue #18：pip || 切源链——阿里（主源）→ 官方 PyPI → 腾讯云。
# extra-index-url 不能在主源慢但包存在时切走；硬故障才走下一段。
_OFFICIAL_PYPI = "https://pypi.org/simple"
_TENCENT_PIP = "https://mirrors.cloud.tencent.com/pypi/simple/"
_CHINA_PIP_FALLBACKS = (_OFFICIAL_PYPI, _TENCENT_PIP)


class BuildMirrors(BaseModel):
    """Dockerfile 构建依赖镜像源（BUG-200 / BUG-201）。

    面向国内小主机：默认 ``enabled=true``，注入 pip/npm/Node 发行包与 apt 镜像。
    海外环境可设 ``enabled: false`` 走官方源。字段非空时覆盖 preset 默认值。

    issue #18：``pipFallbacks`` 是主源失败后的 ``||`` 切源列表（china 默认
    官方 PyPI → 腾讯云）；``pipRetries`` / ``pipTimeout`` 写入每段 pip 命令。
    显式 ``pipFallbacks: []`` 关闭切源。``pipExtraIndex`` 保留解析兼容，
    默认不再注入 ``--extra-index-url``（同版本包不会因主源慢而切走）。

    issue #34 / #35：``aptFallbacks`` / ``aptRetries`` / ``aptTimeout`` 与 pip
    同构；china 默认清华 → 官方。显式 ``aptFallbacks: []`` 关闭切源。
    """

    enabled: bool = True
    preset: str = "china"  # china | none
    pip: str | None = None
    pipExtraIndex: str | None = None
    pipFallbacks: list[str] | None = None
    pipRetries: int = Field(default=3, ge=0, le=20)
    pipTimeout: int = Field(default=60, ge=1, le=600)
    npm: str | None = None
    nodeDistBase: str | None = None
    aptMirror: str | None = None
    # issue #34 / #35：apt 与 pip 对称。china 默认阿里主源，失败切清华再官方。
    # 重试/超时取较小值，让主源快速失败后切源，避免在坏 IP 上耗尽长时间重试。
    aptFallbacks: list[str] | None = None
    aptRetries: int = Field(default=2, ge=0, le=20)
    aptTimeout: int = Field(default=30, ge=1, le=600)

    @field_validator("preset")
    @classmethod
    def _validate_preset(cls, v: str) -> str:
        allowed = {"china", "none"}
        lower = (v or "none").lower()
        if lower not in allowed:
            raise ValueError(f"buildMirrors.preset 必须是 {allowed} 之一，得到 {v!r}")
        return lower

    def resolved(self) -> BuildMirrors:
        """返回已展开 URL 的副本（enabled=false 时各源为 None）。"""
        if not self.enabled or self.preset == "none":
            return BuildMirrors(
                enabled=False,
                preset="none",
                pip=None,
                pipExtraIndex=None,
                pipFallbacks=[],
                pipRetries=self.pipRetries,
                pipTimeout=self.pipTimeout,
                npm=None,
                nodeDistBase=None,
                aptMirror=None,
                aptFallbacks=[],
                aptRetries=self.aptRetries,
                aptTimeout=self.aptTimeout,
            )
        fallbacks = (
            [u.rstrip("/") for u in self.pipFallbacks]
            if self.pipFallbacks is not None
            else [u.rstrip("/") for u in _CHINA_PIP_FALLBACKS]
        )
        apt_fallbacks = (
            [h.strip() for h in self.aptFallbacks]
            if self.aptFallbacks is not None
            else list(_CHINA_APT_FALLBACKS)
        )
        return BuildMirrors(
            enabled=True,
            preset="china",
            pip=self.pip or _CHINA_PIP,
            pipExtraIndex=self.pipExtraIndex,
            pipFallbacks=fallbacks,
            pipRetries=self.pipRetries,
            pipTimeout=self.pipTimeout,
            npm=self.npm or _CHINA_NPM,
            nodeDistBase=(self.nodeDistBase or _CHINA_NODE_DIST).rstrip("/"),
            aptMirror=self.aptMirror or _CHINA_APT,
            aptFallbacks=apt_fallbacks,
            aptRetries=self.aptRetries,
            aptTimeout=self.aptTimeout,
        )


class AgentConfig(BaseModel):
    """Agent 协作配置（AGC-W06，M1）。

    ``allowedSourceRoots``：管理员显式授权给 ``server_directory`` 部署源的
    受控目录根（必须绝对路径）。默认为空 = 全部拒绝（安全默认，§6.2）。
    """

    allowedSourceRoots: list[Path] = Field(default_factory=list)

    # AGC-W10：本 workspace 待执行操作上限（queued + running + cancelling 均占名额），
    # 超限受理返回 busy。cancelling 仍占用执行器，计入上限避免在取消窗口继续灌入。
    maxPendingOperations: int = Field(default=32, ge=1, le=1024)

    @field_validator("allowedSourceRoots")
    @classmethod
    def _validate_absolute(cls, v: list[Path]) -> list[Path]:
        for item in v:
            if not item.is_absolute():
                raise ValueError(
                    f"agent.allowedSourceRoots 必须是绝对路径，得到 {str(item)!r}"
                )
        return v


class Config(BaseModel):
    """``local-web.yml`` 的完整配置模型。"""

    managerPort: int = Field(default=MANAGER_PORT_DEFAULT, ge=1, le=65535)
    managerHost: str = "0.0.0.0"
    managerEnabled: bool = True
    # IMP-046：管理页 API token 自动轮换周期（小时），默认 168h（7×24）。
    # 到期后 manager 进程内后台线程自动换新 token；旧 token 立即失效。
    managerTokenRotateHours: int = Field(default=168, ge=1, le=8760)
    portPool: PortPool = Field(default_factory=PortPool)
    staticGateway: str = "caddy"
    # IMP-006：路径别名统一入口端口。仅当存在已启用的别名时，Caddy 才会在该
    # 端口上监听并按 ``/<alias>/*`` 反向代理到各实例 hostPort；无别名时该端口
    # 不被占用。``None`` 表示彻底关闭别名入口（即便实例配置了 alias 也只走端口）。
    staticGatewayPort: int | None = Field(default=STATIC_GATEWAY_PORT_DEFAULT, ge=1, le=65535)
    buildConcurrency: int = Field(default=1, ge=1, le=8)
    defaultResourceLimits: ResourceLimits = Field(default_factory=ResourceLimits)
    staticRateLimit: StaticRateLimit = Field(default_factory=StaticRateLimit)
    buildMirrors: BuildMirrors = Field(default_factory=BuildMirrors)
    lanIpStrategy: str = "auto"
    manualLanIp: str | None = None
    # ---- HTTPS 首版交付（2026-09-18 WBS / CHK-352）---------------------------
    # gatewayTls：别名入口与管理的传输加密。仅 staticGateway=caddy 支持
    # （internal = Caddy 内嵌 CA 自动签发，根证书经 `lwa ca export` 分发）。
    gatewayTls: str = "off"
    # HTTPS 别名入口端口（gatewayTls=internal 时生效；证书 SAN 覆盖回环+LAN IP）
    gatewayTlsPort: int = Field(default=8443, ge=1, le=65535)
    # 管理面独立 HTTPS origin 端口（独立端口而非同源 /manager/，CHK-352 修订 H1-B5）
    managerTlsPort: int = Field(default=9443, ge=1, le=65535)
    # TLS 开启后的明文入口端口：None=默认关闭（推荐）；设为端口号可保留明文
    # （非安全边界——IP 访问无 HSTS，重定向可被在径剥离，见 docs/https.md）
    gatewayPlainPort: int | None = Field(default=None, ge=1, le=65535)
    # 实例直连绑定地址：0.0.0.0（默认，兼容现状）/ 127.0.0.1（收敛——
    # LAN 仅剩网关入口；影响 builtin --bind、Caddy 站点块、Docker 端口发布）
    instanceBindHost: str = "0.0.0.0"
    logLevel: str = "INFO"
    # AGC-W06：Agent 协作（M1 仅本机 owner；默认空源根=拒绝 server_directory）
    agent: AgentConfig = Field(default_factory=AgentConfig)
    # IMP-033：安装档位与运行身份（缺省 default；full 由 setup --full 写入）
    profile: str = "default"
    serviceUser: str | None = None

    @field_validator("staticGateway")
    @classmethod
    def _validate_gateway(cls, v: str) -> str:
        allowed = {"caddy", "nginx", "builtin"}
        if v not in allowed:
            raise ValueError(f"staticGateway 必须是 {allowed} 之一，得到 {v!r}")
        return v

    @field_validator("profile")
    @classmethod
    def _validate_profile(cls, v: str) -> str:
        allowed = {"default", "full"}
        lower = (v or "default").lower()
        if lower not in allowed:
            raise ValueError(f"profile 必须是 {allowed} 之一，得到 {v!r}")
        return lower

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

    @field_validator("manualLanIp")
    @classmethod
    def _validate_manual_lan_ip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return str(ipaddress.ip_address(value.strip()))
        except ValueError as exc:
            raise ValueError(f"manualLanIp 不是合法 IP 地址：{value!r}") from exc

    @field_validator("gatewayTls", mode="before")
    @classmethod
    def _validate_gateway_tls(cls, v: Any) -> str:
        # YAML 1.1 陷阱：未加引号的 ``off`` 会被解析成布尔 False——
        # 手写配置几乎必然踩中，按语义归一（False=off，True=internal）。
        if isinstance(v, bool):
            v = "internal" if v else "off"
        allowed = {"off", "internal"}
        if v not in allowed:
            raise ValueError(f"gatewayTls 必须是 {allowed} 之一，得到 {v!r}")
        return v

    @field_validator("instanceBindHost")
    @classmethod
    def _validate_instance_bind_host(cls, v: str) -> str:
        # CHK-353/BUG-719：仅允许 IPv4 通配或 IPv4 回环——别名反代上游、
        # 健康探测与 access 复核固定走 127.0.0.1，::/::1 绑定会让全部回环
        # 上游与探活失效（IPv6-only 收敛暂不支持，待上游链路整体 v6 化）。
        try:
            normalized = str(ipaddress.ip_address(v.strip()))
        except ValueError as exc:
            raise ValueError(
                f"instanceBindHost 必须是合法 IP 地址，得到 {v!r}"
            ) from exc
        allowed = {"0.0.0.0", "127.0.0.1"}
        if normalized not in allowed:
            raise ValueError(
                f"instanceBindHost 仅支持 0.0.0.0（通配）或 127.0.0.1（回环收敛），"
                f"得到 {normalized!r}——网关别名反代/健康探测固定走 IPv4 回环，"
                "绑定其他地址（含 ::/::1）会使上游与探活失效"
            )
        return normalized

    @model_validator(mode="after")
    def _check_tls_compatibility(self) -> Config:
        # 支持矩阵（D3）：builtin 是 http.server，无 TLS 能力；nginx 在白名单
        # 但无实现分支。TLS 仅 caddy。
        if self.gatewayTls == "internal" and self.staticGateway != "caddy":
            raise ValueError(
                f"gatewayTls=internal 仅支持 staticGateway=caddy"
                f"（当前 {self.staticGateway!r}）；builtin 网关不支持 TLS，"
                "请切换网关或保持 gatewayTls=off",
            )
        return self

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
        # HTTPS 首版：TLS 端口互斥且不与管理口/入口口/端口池冲突（W01）
        tls_ports = {
            "gatewayTlsPort": self.gatewayTlsPort,
            "managerTlsPort": self.managerTlsPort,
        }
        occupied = {"managerPort": self.managerPort}
        if self.staticGatewayPort is not None:
            occupied["staticGatewayPort"] = self.staticGatewayPort
        for plain_name, plain_port in occupied.items():
            for tls_name, tls_port in tls_ports.items():
                if plain_port == tls_port:
                    raise ValueError(
                        f"{tls_name}({tls_port}) 不能与 {plain_name}({plain_port}) 相同",
                    )
        if self.gatewayTlsPort == self.managerTlsPort:
            raise ValueError(
                f"gatewayTlsPort 与 managerTlsPort 不能相同（{self.gatewayTlsPort}）",
            )
        for name, port in tls_ports.items():
            if self.portPool.start <= port <= self.portPool.end:
                raise ValueError(
                    f"{name}({port}) 不能落在端口池 "
                    f"[{self.portPool.start}, {self.portPool.end}] 内",
                )
        if self.gatewayTls == "internal" and self.gatewayPlainPort is not None:
            plain = self.gatewayPlainPort
            # BUG-722：与 staticGatewayPort 同规格查全——撞 managerPort 时
            # Caddy 抢不到端口，落端口池则与实例直连冲突。
            if plain == self.managerPort:
                raise ValueError(
                    f"gatewayPlainPort({plain}) 不能与管理页端口"
                    f"({self.managerPort}) 相同",
                )
            if plain in tls_ports.values():
                raise ValueError(
                    f"gatewayPlainPort({plain}) 不能与 TLS 端口相同",
                )
            if self.portPool.start <= plain <= self.portPool.end:
                raise ValueError(
                    f"gatewayPlainPort({plain}) 不能落在端口池 "
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
        # 评审-组7：未知顶层键此前被静默忽略并回退默认值（拼写错误难发现）。
        # 保持向后兼容不 fail，但记录 warning 提示。
        known = set(cls.model_fields.keys())
        unknown = [k for k in raw if k not in known]
        if unknown:
            import logging

            logging.getLogger("local_webpage_access.config").warning(
                "配置文件含未知键（已忽略）：%s；已知键见 example_config_text",
                ", ".join(sorted(unknown)),
            )
        return cls.from_dict(raw)

    def to_yaml(self) -> str:
        # BUG-653：mode="json" 把 Path 等不可 YAML 表示的类型转成原生标量，
        # 避免 allowedSourceRoots 配置后 save/网关切换写回失败。
        data = self.model_dump(mode="json")
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_yaml(), encoding="utf-8")


def tls_enabled(config: Config) -> bool:
    """HTTPS 首版交付（WBS W01/W07）：网关 TLS 是否生效。

    权威定义置于 config（避免 ports ↔ static_gateway 循环导入）——
    ``gatewayTls=internal`` 且网关为 caddy（支持矩阵 D3，Config 校验
    已拒绝 builtin+TLS 组合）。
    """
    return config.gatewayTls == "internal" and config.staticGateway == "caddy"


def default_config() -> Config:
    """返回默认配置实例。"""
    return Config()


def load_config(workspace: Workspace) -> Config:
    """从工作区加载配置；配置文件不存在时返回默认配置。"""
    if workspace.config_path.is_file():
        return Config.from_file(workspace.config_path)
    return default_config()


def example_config_text(*, static_gateway: str | None = None) -> str:
    """返回用于写入 ``local-web.yml`` 的示例文本（带注释）。

    ``static_gateway`` 若给出，则覆盖示例中的 ``staticGateway`` 行（IMP-032）。
    """
    text = CONFIG_EXAMPLE
    if static_gateway is not None:
        import re

        text = re.sub(
            r"(?m)^staticGateway:\s*\S+",
            f"staticGateway: {static_gateway}",
            text,
            count=1,
        )
    return text


CONFIG_EXAMPLE = """\
# Local Webpage Access 配置文件
# 由 lwa init 生成。字段含义见 docs/plan/local-webpage-access-v1-design-20260704.md。

# 管理页监听端口（不应落在端口池范围内）
managerPort: 17800
managerHost: 0.0.0.0

# 是否在 lwa init 后自动后台启动管理页（false 时需手动 lwa manager on）
managerEnabled: true

# 管理页 API token 自动轮换周期（小时），默认 168h=7 天（IMP-046）
# 到期后 manager 后台线程自动换新 token；旧 token 立即失效。
# 本机 loopback 访问免 token，不受影响。
managerTokenRotateHours: 168

# 实例端口池
portPool:
  start: 18000
  end: 19999

# 静态网关实现：caddy | nginx | builtin
staticGateway: caddy

# 路径别名统一入口端口（IMP-006）。仅当存在已启用的 --path-alias 时，Caddy
# 才在此端口监听并按 /<alias>/ 反向代理到各实例 hostPort。设为 null 关闭别名入口。
staticGatewayPort: 8080

# HTTPS 传输加密（仅 staticGateway=caddy 支持；详见 docs/https.md）。
# off：现状——全部明文 HTTP。
# internal：Caddy 内嵌 CA 自动签发——别名入口与管理面走 HTTPS，manager 收敛为
#   仅回环监听（managerHost 被强制 127.0.0.1），根证书用 `lwa ca export` 导出
#   并在客户端安装信任；未安装根证书的浏览器会得到证书告警（不要点穿告警）。
gatewayTls: off
gatewayTlsPort: 8443      # HTTPS 别名入口端口
managerTlsPort: 9443      # 管理面独立 HTTPS origin 端口（https://<LAN-IP>:9443/）
# TLS 开启后的明文入口端口：默认 null=关闭（推荐）。保留明文不是安全边界。
# gatewayPlainPort: 8080
# 实例直连绑定地址：0.0.0.0（默认，LAN 可直连 hostPort）/ 127.0.0.1（收敛——
# LAN 仅剩网关入口，Docker 发布与 builtin/Caddy 站点同步绑定回环；
# 仅支持这两个值，IPv6 回环暂不支持——别名反代/探活固定走 IPv4 回环）
instanceBindHost: 0.0.0.0

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

# 容器构建镜像源（BUG-200）：默认启用国内源；海外可设 enabled: false
# issue #18：pip 默认阿里 → 官方 → 腾讯，每源 retries 3；pipFallbacks: [] 关闭切源
buildMirrors:
  enabled: true
  preset: china
  # pip: https://mirrors.aliyun.com/pypi/simple/
  # pipFallbacks:
  #   - https://pypi.org/simple
  #   - https://mirrors.cloud.tencent.com/pypi/simple/
  # pipRetries: 3
  # pipTimeout: 60
  # npm: https://registry.npmmirror.com
  # nodeDistBase: https://mirrors.aliyun.com/nodejs-release
  # aptMirror: mirrors.aliyun.com
  # aptFallbacks:
  #   - mirrors.tuna.tsinghua.edu.cn
  #   - deb.debian.org
  # aptRetries: 2
  # aptTimeout: 30

# 局域网 IP 获取策略：auto（自动探测）| manual（手动指定）
lanIpStrategy: auto
manualLanIp: null

# 日志级别
logLevel: INFO
"""


__all__ = [
    "BuildMirrors",
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
    "tls_enabled",
]
