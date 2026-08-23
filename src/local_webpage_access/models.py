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
import os
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from local_webpage_access.errors import PathError, SchemaError
from local_webpage_access.logging import now_iso

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
    # IMP-039：进行中构建取消中间态 / 终态
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    # DEV-043 / BUG-071：Caddy 模式下区分"网关不可达"与"配置无效"，
    # 避免把 enabled 但 master 挂掉的实例误标普通 stopped（BUG-071 根因）。
    # * gateway_down：enabled ∧ Caddy master（admin :2019）不可达；
    # * config_invalid：master 在线但站点 hostPort 不通（路由/配置问题，BUG-069 类）。
    GATEWAY_DOWN = "gateway_down"
    CONFIG_INVALID = "config_invalid"
    # IMP-058 Gate-C §6.5：实证校验状态机。
    # * VERIFYING：build/up 成功但尚未通过必选探针——不允许被折叠为 running。
    # * DEGRADED：必选探针通过、可选探针失败——进程存活但能力不完整。
    VERIFYING = "verifying"
    DEGRADED = "degraded"


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
    # IMP-058 Gate-A CHK-V03：SQLite 源文件名（如 "bookshelf.db"）。
    # scanner 从项目目录扫描到的 SQLite 文件名；compose.generate_env 据此注入
    # 保留原文件名的 DATABASE_URL，避免把应用指向全新空库（BUG-474 数据丢失风险）。
    dbFilename: str | None = None


class DatabaseSignal(BaseModel):
    """应用数据库配置消费信号（A.R01 尽力解析）。

    采集自项目源码（config.py / settings.py 等），判断应用是否读取
    ``DATABASE_URL`` 环境变量，以及默认连接串的路径形态。

    A.R01 安全自动修正前提：只有 ``consumesDatabaseUrl=True`` 时，
    compose 才自动注入 ``DATABASE_URL``；否则保留原配置，标记 warning。
    """

    model_config = ConfigDict(extra="allow")

    consumesDatabaseUrl: bool = False  # 应用是否读取 DATABASE_URL 环境变量
    defaultUrl: str | None = (
        None  # config 中的默认 DATABASE_URL（如 "sqlite:///./data/bookshelf.db"）
    )
    isRelative: bool = False  # 默认 URL 是否相对路径
    dbFilename: str | None = None  # 从默认 URL 解析的数据库文件名
    sourcePath: str | None = None  # 解析来源文件相对路径（如 "config.py"）


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
    # IMP-014（WBS-20260708 阶段3.1）：容器实例路径别名，镜像 static 的 routeMode/routeHost，
    # 让 registry containers 表与别名统一入口（reverse_proxy hostPort）联动。
    routeMode: str = RouteMode.PORT.value
    routeHost: str | None = None
    # issue#1：额外 bind mount（``宿主路径:容器路径[:ro]`` 列表），渲染 compose 时
    # 合并进 volumes--手工改 compose.yaml 会在重生成时被抹掉，业务定制走这里。
    extraVolumes: list[str] = Field(default_factory=list)


class NetworkConfig(BaseModel):
    """访问入口配置。"""

    model_config = ConfigDict(extra="allow")

    host: str = "0.0.0.0"
    internalPort: int | None = None
    hostPort: int | None = None
    routeMode: str = RouteMode.PORT.value
    routeHost: str | None = None
    # IMP-006：路径别名统一入口 URL（``routeMode="name"`` 时填充）。
    routeUrl: str | None = None
    lanUrl: str | None = None
    healthUrl: str | None = None


class EntryConfig(BaseModel):
    """安装 / 构建 / 启动命令推断结果。"""

    model_config = ConfigDict(extra="allow")

    install: str | None = None
    build: str | None = None
    start: str | None = None
    buildOutputDir: str | None = (
        None  # 构建产物目录（相对项目根），monorepo 子包为 packages/<name>/dist
    )


# ---- Monorepo 包分类（IMP-057 Gate-1）---------------------------------------


class PackageType(str, Enum):
    """Monorepo 子包类型（6 值枚举，IMP-057 §4.2）。

    - ``electron_desktop``：含 ``electron`` 依赖，不可部署
    - ``library``：有 ``main``/``exports``，无后端框架，不可部署
    - ``web_server``：Node HTTP 服务 / SSR，可部署
    - ``frontend_build``：纯前端构建后静态托管，可部署
    - ``runtime_data``：无有效 package.json，不可部署
    - ``unknown``：无法归类
    """

    ELECTRON_DESKTOP = "electron_desktop"
    LIBRARY = "library"
    WEB_SERVER = "web_server"
    FRONTEND_BUILD = "frontend_build"
    RUNTIME_DATA = "runtime_data"
    UNKNOWN = "unknown"


class PackageClassification(BaseModel):
    """Monorepo 子包分类结果（IMP-057 §7.3）。

    Gate-1 运行期使用；持久化到 manifest 为可选（非 MVP 验收项）。
    """

    model_config = ConfigDict(extra="allow")

    name: str
    path: str  # "packages/webpage"
    packageType: str  # PackageType 枚举值
    isDeployable: bool


# ---- Gate-2：兼容性预检（IMP-056）-----------------------------------------


class CompatibilityFinding(BaseModel):
    """兼容性预检结果（IMP-056 Gate-2）。

    仅展示分级，不阻断 import/start/alias。IMP-055 仍是唯一 enforce 真源。
    """

    model_config = ConfigDict(extra="allow")

    checkId: str  # "CHK-P03" / "CHK-P04"
    severity: str  # "critical" / "warning" / "info"（仅展示分级）
    title: str
    file: str | None = None  # 相对 current/
    line: int | None = None
    code: str | None = None
    impact: str
    fix: str


# ---- Gate-B：多候选识别数据模型（IMP-058）-----------------------------------


class SubdirSignal(BaseModel):
    """子目录的工程文件信号（Layer 0 采集）。"""

    model_config = ConfigDict(extra="allow")

    path: str  # 相对项目根的路径（如 "backend"）
    name: str  # "backend" / "frontend" / "server" ...
    hasRequirements: bool = False
    hasPyproject: bool = False
    hasPackageJson: bool = False
    hasManagePy: bool = False
    hasAlembicIni: bool = False
    hasIndexHtml: bool = False
    pythonDeps: list[str] = Field(default_factory=list)
    nodeDeps: dict[str, str] = Field(default_factory=dict)
    nodeScripts: dict[str, str] = Field(default_factory=dict)


class ProjectEvidence(BaseModel):
    """Layer 0 输出：项目的客观事实摘要（零解释）。"""

    model_config = ConfigDict(extra="allow")

    root: str  # 项目根路径
    rootFiles: list[str] = Field(default_factory=list)
    rootDirs: list[str] = Field(default_factory=list)
    subdirSignals: list[SubdirSignal] = Field(default_factory=list)
    pythonDeps: list[str] = Field(default_factory=list)
    nodeDeps: dict[str, str] = Field(default_factory=dict)
    nodeScripts: dict[str, str] = Field(default_factory=dict)
    workspaces: list[str] | None = None
    hasManagePy: bool = False
    hasAlembicIni: bool = False
    hasRuntimePaths: bool = False
    projectDockerfile: str | None = None
    projectCompose: str | None = None
    hasEnvExample: bool = False
    hasIndexHtml: bool = False
    hasHtml: bool = False
    buildOutputs: list[str] = Field(default_factory=list)
    hasPackageJson: bool = False
    sqliteFiles: list[str] = Field(default_factory=list)
    # A.R01：应用数据库配置消费信号（是否读取 DATABASE_URL、默认连接串形态）
    databaseConfig: DatabaseSignal | None = None


class DeploymentCandidate(BaseModel):
    """一个部署候选配置（Layer 1 输出）。"""

    model_config = ConfigDict(extra="allow")

    kind: str  # "static" / "node" / "python"
    runtime: str  # "shared_static" / "docker_compose"
    servingMode: str  # "shared_static" / "container"
    form: str  # static / frontend-static / backend-container / fullstack-sqlite
    resourceProfile: str = "small"
    entry: EntryConfig = Field(default_factory=EntryConfig)
    internalPort: int | None = None
    sourceSubdir: str | None = None  # 源码子目录（如 "backend"），None = 根目录
    confidenceTier: str = "primary"  # primary / alternate / fallback
    reasoning: list[str] = Field(default_factory=list)


# ---- Gate-C：部署拓扑模型（IMP-058-C C.01）---------------------------------

# 能力标识（IMP-058 §6.5 成功谓词中 capabilities_observed 的取值）
CAPABILITY_UI = "ui"
CAPABILITY_API = "api"
CAPABILITY_DATABASE = "database"
CAPABILITY_MIGRATIONS = "migrations"


class ProbeSpec(BaseModel):
    """证据驱动的健康探针规格（IMP-058-C C.05）。

    区分两类探针：
    - ``is_mandatory=True``：项目声明或证据发现的可作门槛探针（如 ``/health`` 预期 2xx）。
      失败时判定部署失败（不写 RUNNING）。
    - ``is_mandatory=False``：通用猜测探针（如默认 ``/api/`` 路径）。404/401 不单独判失败，
      仅产生诊断信息。
    """

    model_config = ConfigDict(extra="allow")

    path: str  # "/health" / "/api/v1/status"
    method: str = "GET"
    expectedStatus: int = 200  # 预期状态码（2xx 视为通过）
    isMandatory: bool = False  # True = 可作成功门槛；False = 仅诊断
    source: str = "guessed"  # "declared" / "discovered" / "guessed"
    description: str = ""  # 人类可读说明


class ProbeOverride(BaseModel):
    """用户显式声明的就绪探针（第二批 CHK-252：``lwa probe set``）。

    只有用户显式声明的探针才可作为 mandatory 门槛——契约探针
    （declared/discovered/guessed）均为证据，静态扫描无法判断鉴权与
    返回语义，不构成失败条件。
    """

    model_config = ConfigDict(extra="allow")

    path: str
    method: str = "GET"
    expectedStatus: int = 200  # 预期状态码（精确匹配；2xx/3xx 同样视为通过）
    description: str = ""


class VerificationOverrides(BaseModel):
    """实例级验证覆盖层（第二批 CHK-252），持久化在 manifest 顶层。

    重导入/rebuild 只重写 ``capabilityContract``，本字段独立存在、
    更新和重扫不得覆盖（stable override layer）。
    """

    model_config = ConfigDict(extra="allow")

    # 用户显式声明的就绪探针（mandatory 门槛的唯一来源）。
    probes: list[ProbeOverride] = Field(default_factory=list)
    # 关闭自动探针（契约中的 guessed/discovered 全部不执行）。
    disableAutoProbes: bool = False


class CapabilityContract(BaseModel):
    """部署成功必须保留的业务能力契约（IMP-058 §6.5）。

    ``required_capabilities`` 由结构化标志派生，避免维护第二份能力真相源。

    能力守恒（§6.1.1 硬性约束）：``serves_api=True`` 的计划不得降级到
    ``serves_api=False`` 的计划。
    """

    model_config = ConfigDict(extra="allow")

    servesUi: bool = False
    servesApi: bool = False
    requiresDatabase: bool = False
    requiresMigrations: bool = False
    requiredProbes: list[ProbeSpec] = Field(default_factory=list)

    @property
    def required_capabilities(self) -> set[str]:
        """由结构化标志派生的能力集合。"""
        pairs = {
            CAPABILITY_UI: self.servesUi,
            CAPABILITY_API: self.servesApi,
            CAPABILITY_DATABASE: self.requiresDatabase,
            CAPABILITY_MIGRATIONS: self.requiresMigrations,
        }
        return {name for name, required in pairs.items() if required}


class CommandSpec(BaseModel):
    """结构化命令规格（IMP-058-C C.03）。

    替代 Gate-A 的字符串命令模型（``entry.start`` 字符串 + ``sh -c`` 包裹）。
    每个 CommandSpec 显式表达 argv/shell/workdir/env，不再用正则改写 shell。

    - ``argv`` 模式：``["uvicorn", "app.main:app", "--host", "0.0.0.0"]``
    - ``shell`` 模式：``"alembic upgrade head && exec uvicorn ..."``（不自动改写）
    """

    model_config = ConfigDict(extra="allow")

    argv: list[str] | None = None  # exec 形式
    shell: str | None = None  # shell 形式（当 argv 无法无损表达时使用）
    workdir: str = "/app"  # 容器内工作目录
    environment: dict[str, str] = Field(default_factory=dict)

    def is_effective(self) -> bool:
        """是否有有效命令。"""
        return bool(self.argv or self.shell)


class DeploymentComponent(BaseModel):
    """部署计划内的协作组件（IMP-058-C C.01）。

    组件之间是**合作关系**（如 frontend-build + python-container），不是 fallback 关系。
    全栈项目的前端构建组件和后端运行组件共同构成一个 :class:`DeploymentPlan`。
    """

    model_config = ConfigDict(extra="allow")

    componentId: str  # "frontend-build" / "python-container"
    role: str  # "build" / "runtime" / "build-and-runtime"
    sourceSubdir: str | None = None
    buildCommand: CommandSpec | None = None
    startCommand: CommandSpec | None = None
    preStart: list[CommandSpec] = Field(default_factory=list)  # 如 alembic migrate
    buildOutputDir: str | None = None  # 构建产物目录（如 "dist"）
    artifactTarget: str | None = None  # 产物传递目标（如 "backend/static"）
    internalPort: int | None = None


class DeploymentPlan(BaseModel):
    """一套完整部署拓扑（IMP-058-C C.01）。

    内部组件是合作关系，不是 fallback 关系。只有能力契约完全等价的多个计划，
    才可以互相降级（§6.1.1）。
    """

    model_config = ConfigDict(extra="allow")

    planId: str
    components: list[DeploymentComponent] = Field(default_factory=list)
    capabilityContract: CapabilityContract = Field(default_factory=CapabilityContract)
    confidenceTier: str = "primary"  # primary / alternate / fallback
    reasoning: list[str] = Field(default_factory=list)
    evidenceRefs: list[str] = Field(default_factory=list)


class RollbackResult(BaseModel):
    """Gate-C C.06/C.R04：单次 attempt 的回滚结果。

    ``rollback_succeeded`` 只表示已声明的回滚步骤全部成功，不得笼统解释为
    "系统没有任何副作用"。若 attempt 执行了数据库迁移或外部写入，
    必须通过 ``externalSideEffects`` 单独记录。

    C.R04 扩展：
    - ``residualItems`` 记录未能恢复的残留项（供人工处置）
    - ``snapshotData`` 存储 Prepare 阶段的快照内容（manifest 字段 + 文件 hash）
    """

    model_config = ConfigDict(extra="allow")

    attemptId: str = ""
    rollbackSucceeded: bool = False
    rolledBackItems: list[str] = Field(
        default_factory=list
    )  # ["container", "port", "files", "manifest"]
    externalSideEffects: list[str] = Field(default_factory=list)  # ["migration:alembic_head_xxx"]
    automaticFallbackSafe: bool = False  # 仅当副作用可丢弃/已回滚/已快照恢复
    # C.R04：残留项（未能恢复，需人工处置）
    residualItems: list[str] = Field(default_factory=list)
    # C.R04：Prepare 阶段快照数据（manifest 关键字段 + 生成文件内容/hash）
    snapshotData: dict | None = None
    # C.R05：结构化副作用记录
    sideEffectRecords: list["SideEffectRecord"] = Field(default_factory=list)


class SideEffectRecord(BaseModel):
    """C.R05：外部副作用记录（pre_start/migration/hooks）。

    执行前记录意图，执行后记录结果、补偿方式和恢复证据。
    未知写入默认不可自动恢复（``autoRecoverable=False``）。
    """

    model_config = ConfigDict(extra="allow")

    kind: str  # "migration" / "pre_start" / "hook" / "unknown"
    description: str  # 人可读描述
    intent: str  # 执行前的意图声明
    executedAt: str = ""  # ISO timestamp
    result: str = "unknown"  # "succeeded" / "failed" / "unknown"
    compensationMethod: str | None = None  # 补偿方式描述
    recoveryEvidence: str | None = None  # 恢复证据
    autoRecoverable: bool = False  # 是否可自动恢复


# ---- Gate-C：实证校验模型（IMP-058）---------------------------------------


class VerificationResult(BaseModel):
    """Layer 3 输出：单个候选的实证校验结果（IMP-058 §6.5）。

    由 start_instance 降级流程在尝试每个候选后产出。

    成功谓词（§6.5）::

        plan_succeeded =
            build_succeeded
            AND start_succeeded
            AND all(required_probe.passed)
            AND capabilities_observed >= capability_contract.required_capabilities
    """

    model_config = ConfigDict(extra="allow")

    attemptId: str = ""
    planId: str | None = None
    candidateIndex: int = 0  # 候选在列表中的位置（0=top-1），兼容 Gate-B
    candidateTier: str = "primary"  # primary / alternate / fallback
    buildSucceeded: bool = False
    startSucceeded: bool = False
    healthCheckPassed: bool = False
    requiredProbesPassed: bool = False
    optionalProbeWarnings: list[str] = Field(default_factory=list)
    observedCapabilities: list[str] = Field(default_factory=list)
    externalSideEffects: list[str] = Field(default_factory=list)
    automaticFallbackSafe: bool = False
    overallStatus: str = "failed"  # "passed" / "degraded" / "failed"
    # 兼容字段（旧代码读取）
    apiProbePassed: bool | None = None  # None = 未探测
    error: str | None = None
    buildLogPath: str | None = None
    durationSeconds: float = 0.0

    def is_success(self) -> bool:
        """是否通过成功谓词（passed 或 degraded 都算成功部署）。"""
        return self.overallStatus in ("passed", "degraded")


class CandidateDiagnosis(BaseModel):
    """单个候选的诊断信息（IMP-058 §6.6）。"""

    model_config = ConfigDict(extra="allow")

    attemptId: str = ""
    candidateIndex: int
    candidateTier: str = "primary"
    planId: str | None = None
    failureLayer: str  # "preflight" / "build" / "start" / "health" / "api" / "none"
    failureReason: str = ""
    fixSuggestion: str = ""
    verification: VerificationResult | None = None
    rollback: RollbackResult | None = None
    capabilityDiff: str = ""  # 能力差异描述（如 "fallback 缺少 api 能力")


class DiagnosisReport(BaseModel):
    """Layer 4 输出：收敛诊断（IMP-058 §6.6）。

    全部候选失败（或 Layer 2 后零可行候选）时产出。
    """

    model_config = ConfigDict(extra="allow")

    instanceId: str
    overallStatus: str = "failed"  # "pending" / "failed"
    candidatesTried: list[CandidateDiagnosis] = Field(default_factory=list)
    recommendedAction: str = ""
    notes: list[str] = Field(default_factory=list)


# ---- 主模型 -----------------------------------------------------------------


class InstanceManifest(BaseModel):
    """实例元数据合同，对应 ``local-web.json``。

    这是 CLI、管理页、静态网关、Docker Compose 和大模型 skill 共同读取的真相文件。
    """

    model_config = ConfigDict(extra="allow", use_enum_values=False)

    schemaVersion: int = SCHEMA_VERSION
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    # IMP-043：显示名来源。user=用户 ``--name``；html_title=主页 <title>；slug=titleize(id)。
    # 缺省 None 表示升级前旧实例——仅当 name==titleize(id) 时才允许 title 回填。
    nameSource: str | None = None
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
    # issue#7：构建钩子与启动前命令（持久化在 manifest，rebuild 重生成 Dockerfile
    # 时保留，不再被抹掉）。buildHooks 在依赖安装层之后逐条生成 RUN；preStart
    # 使 CMD 变为 ``sh -c "<preStart> && exec <start>"``。均拒绝换行符（防注入）。
    buildHooks: list[str] = Field(default_factory=list)
    preStart: str | None = None
    # IMP-047：来源类型与关联路径。旧实例默认 "zip"（迁移时由 from_dict extra 兜底）。
    sourceKind: str = "zip"
    sourceDirPath: str | None = None
    # IMP-047：文件夹源上次同步的内容指纹（compute_source_hash），供无变更短路。
    sourceSyncHash: str | None = None
    # IMP-065（§17.2.1）：git 源身份。sourceKind="git" 时 sourceDirPath 恒为 None
    # （staging 一次性可弃，禁止写成本机路径）；URL 存规范化后的
    # https://github.com/<owner>/<repo>；ref 存克隆当时解析出的真实分支/tag 名
    # （禁止存 "HEAD"）；commit 存完整 OID（短 SHA 仅展示）。
    sourceGitUrl: str | None = None
    sourceGitRef: str | None = None
    sourceGitRefKind: str | None = None  # "branch" | "tag"
    sourceGitCommit: str | None = None
    # git 源打包根（仓库相对子目录；None = 整仓打包）。与 scanner 的
    # sourceSubdir（识别/构建上下文）是两个字段：本字段决定 update 重打包范围，
    # 使更新后的 current/ 与导入时结构一致。
    sourceGitSubdir: str | None = None
    # sourceZipHash 通过 extra="allow" 存储（未显式声明），保持向后兼容。
    sourceZipPath: str | None = None
    appPath: str | None = None
    createdAt: str = Field(default_factory=now_iso)
    updatedAt: str = Field(default_factory=now_iso)
    lastStartedAt: str | None = None
    lastHealthCheckAt: str | None = None
    lastError: str | None = None

    # ---- Gate-B 新字段（IMP-058）----
    deploymentCandidates: list[dict] = Field(default_factory=list)
    preflightSummary: str | None = None
    sourceSubdir: str | None = None  # 源码子目录（如 "backend"），None = 根目录
    # ---- Gate-C 新字段（IMP-058-C）----
    # 部署计划列表与选中计划（C.01/C.06/C.07）
    deploymentPlans: list[dict] = Field(default_factory=list)
    selectedPlanId: str | None = None
    selectedPlanHash: str | None = None
    # 验证摘要（C.04/C.08）：必选/可选探针结果、观测到的能力与 attempt ID
    verificationSummary: dict | None = None
    # 能力契约快照（C.01/C.07）：该实例声明的能力集合，用于降级时等价性校验
    capabilityContract: dict | None = None
    # 第二批 CHK-252：用户级验证覆盖层（显式就绪探针 + 关闭自动探针）。
    # 顶层独立字段——rebuild/重扫重写 capabilityContract 时不受影响。
    verificationOverrides: VerificationOverrides | dict | None = None
    # build 变化 → 强制镜像重建；仅 runtime 变化 → compose recreate；全未变 → 轻量 start
    deploymentFingerprints: dict | None = None
    # ---- Gate-2 新字段（IMP-056）----
    compatibilityFindings: list[CompatibilityFinding] = Field(default_factory=list)
    # A.R01：数据库配置消费信号（从 evidence 传递，compose 据此决定是否注入 DATABASE_URL）
    databaseConfig: dict | None = None

    @field_validator("kind", "runtime", "servingMode", "resourceProfile", "desiredState", "status")
    @classmethod
    def _coerce_enum(cls, v: Any) -> Any:
        return v

    @field_validator("buildHooks", "preStart")
    @classmethod
    def _check_hooks_no_newline(cls, v: Any) -> Any:
        """issue#7：buildHooks/preStart 会内插进生成的 Dockerfile（RUN / CMD），
        拒绝换行符，防止手改 local-web.json 注入额外 Dockerfile 指令。"""
        values = v if isinstance(v, list) else [v]
        for item in values:
            if isinstance(item, str) and ("\n" in item or "\r" in item):
                raise ValueError(f"buildHooks/preStart 不允许包含换行符：{item!r}")
        return v

    @field_validator("sourceSubdir")
    @classmethod
    def _check_source_subdir(cls, v: Any) -> Any:
        """BUG-507：写入/加载入口拒绝绝对路径与 ``..`` 穿越。"""
        from local_webpage_access.paths import validate_source_subdir

        if v is None:
            return None
        try:
            return validate_source_subdir(v)
        except PathError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("sourceGitSubdir")
    @classmethod
    def _check_source_git_subdir(cls, v: Any) -> Any:
        """IMP-065（BUG-557）：git 源打包根与 sourceSubdir 同规——拒绝绝对
        路径与 ``..`` 穿越；否则手改 local-web.json 可让 update 打到仓库外。"""
        from local_webpage_access.paths import validate_source_subdir

        if v is None:
            return None
        try:
            return validate_source_subdir(v)
        except PathError as exc:
            raise ValueError(str(exc)) from exc

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
        """写入 ``local-web.json``（美化格式）。

        评审-组7：临时文件 + ``os.replace`` 原子替换——中途崩溃不再留下
        损坏的 manifest（后续 load 抛 SchemaError 会让实例不可读）。
        """
        data = self.to_dict()
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        else:
            # 非 Path（测试桩 / PathLike 以外）：保持直写
            path.write_text(text, encoding="utf-8")

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
    def from_dict(cls, data: dict[str, Any], *, path: Path | None = None) -> InstanceManifest:
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
    "DatabaseSignal",
    "ResourceLimits",
    "StaticConfig",
    "ContainerConfig",
    "NetworkConfig",
    "EntryConfig",
    "InstanceManifest",
    "migrate_manifest",
    # IMP-058-C Gate-C
    "CAPABILITY_UI",
    "CAPABILITY_API",
    "CAPABILITY_DATABASE",
    "CAPABILITY_MIGRATIONS",
    "ProbeSpec",
    "CapabilityContract",
    "CommandSpec",
    "DeploymentComponent",
    "DeploymentPlan",
    "RollbackResult",
    "SideEffectRecord",
    "VerificationResult",
    "CandidateDiagnosis",
    "DiagnosisReport",
]
