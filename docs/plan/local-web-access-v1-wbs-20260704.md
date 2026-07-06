# Local Webpage Access V1 WBS 分解计划

> 日期：2026-07-04  
> 状态：V1 实施 WBS 草案  
> 基础文档：`docs/plan/local-web-access-v1-design-20260704.md`  
> 目标：把 V1 设计说明拆解为可执行、可验收、可排期的工作包。

## 1. WBS 使用说明

本 WBS 面向 Local Webpage Access V1 的 MVP 实施，覆盖从工程底座、CLI、实例导入、静态托管、Docker Compose 托管、管理页、daemon、skills 到端到端验证的完整闭环。

每个工作包包含：

1. **范围**：该工作包要解决的问题。
2. **任务**：可以继续拆成 issue 或开发任务的条目。
3. **交付物**：完成后仓库中应出现的代码、配置、文档或样例。
4. **依赖**：开始该工作包之前需要完成的前置项。
5. **验收标准**：判断工作包完成的客观条件。

优先级定义：

| 优先级 | 含义 |
| --- | --- |
| P0 | V1 MVP 必须完成，缺失则闭环不成立 |
| P1 | V1 建议完成，明显提升可用性或可维护性 |
| P2 | 可延后到 V1.1 或 V1.2 |

规模定义：

| 规模 | 含义 |
| --- | --- |
| S | 小任务，通常 0.5-1 天可完成 |
| M | 中等任务，通常 1-3 天 |
| L | 大任务，需要拆分或跨多个模块 |

## 2. V1 交付边界

### 2.1 V1 必须交付

1. `lwa init` 初始化目录结构、配置文件和 SQLite registry。
2. `lwa import` 导入 zip，并生成实例目录、`local-web.json` 和 registry 记录。
3. 支持纯静态 HTML 共享静态托管。
4. 支持 Vite/React/Vue 等纯前端项目构建后共享静态托管。
5. 支持小型 Node 后端 Docker Compose 托管。
6. 支持小型 Python 后端 Docker Compose 托管。
7. 支持 SQLite 数据目录挂载。
8. 支持端口池分配和宿主机端口冲突检查。
9. 支持 `start`、`stop`、`restart`、`rebuild`、`logs`、`status`、`stats`。
10. 支持 daemon on/off 和 inbox watcher。
11. 支持管理页实例列表、实例详情、打开、启停、日志和基础资源展示。
12. 支持 pending/failed/building/running/stopped 状态流转。
13. 支持四个样例 zip 的端到端验证：静态 HTML、纯前端 SPA、Node/Express 后端、FastAPI/Flask + SQLite。
14. 提供 V1 安装、使用、排障和架构文档。

### 2.2 V1 不交付

1. Traefik/nip.io 名字路由默认启用。
2. Postgres/MySQL/Redis 自动托管。
3. 多用户权限系统。
4. 公网域名和 SSL 证书全流程。
5. Git 仓库持续部署。
6. 通用 Docker 主机管理器。
7. 完整应用商店。
8. 多机器部署和实例迁移包。
9. Prometheus、cAdvisor 等重型监控。

## 3. 里程碑总览

| 里程碑 | 名称 | 目标 | 主要产出 | 优先级 |
| --- | --- | --- | --- | --- |
| M0 | 项目基线与约束确认 | 固化 V1 范围、命名、目录和技术选型 | WBS、配置草案、实现约束 | P0 |
| M1 | 工程底座 | 建立 Python CLI、配置、日志、测试框架 | `lwa` CLI 骨架、配置加载、日志 | P0 |
| M2 | 元数据与 Registry | 建立 `local-web.json` schema 和 SQLite registry | schema、DAO、迁移、状态模型 | P0 |
| M3 | 导入与识别 | 打通 zip 导入、实例命名、基础识别 | `lwa import`、scanner、pending 状态 | P0 |
| M4 | 端口与访问入口 | 实现端口池、健康检查、LAN URL 生成 | 端口分配器、冲突检查、URL 记录 | P0 |
| M5 | 静态托管路径 | 支持纯静态和构建后 SPA | 静态网关配置、前端构建流程 | P0 |
| M6 | Docker Compose 路径 | 支持 Node/Python 后端和 SQLite 应用 | Dockerfile/Compose 模板、运行封装 | P0 |
| M7 | 生命周期与资源 | 实现启停、日志、状态同步、资源统计 | lifecycle、logs、stats、events | P0 |
| M8 | Daemon 与队列 | 实现 inbox watcher、构建队列和双模式 | daemon on/off、任务锁、构建队列 | P1 |
| M9 | 管理页 | 实现本地 Hub 页面和操作入口 | FastAPI API、前端页面、实例详情 | P0 |
| M10 | 大模型 Skills | 编写识别、Docker 化、修复类 skills 文档 | skills 目录、流程说明、输入输出约束 | P1 |
| M11 | 安全与运维 | 加固默认行为、token、清理和诊断 | 认证、doctor、运维脚本、风险提示 | P1 |
| M12 | 样例与测试 | 完成单测、集成测试、端到端验证 | 样例 zip、测试报告、验收记录 | P0 |
| M13 | 文档与发布 | 形成 V1 可交付版本 | README、安装文档、发布清单 | P0 |

## 4. WBS 详细分解

### WBS-00 项目基线与实施准备

| 字段 | 内容 |
| --- | --- |
| 优先级 | P0 |
| 规模 | M |
| 目标 | 明确 V1 的范围、技术约束、默认路径和不做事项 |
| 依赖 | `local-web-access-v1-design-20260704.md` |

任务：

1. WBS-00.01 确认 V1 MVP 边界和延期项。
2. WBS-00.02 确认 CLI 名称为 `lwa`。
3. WBS-00.03 确认默认管理页端口 `17800`。
4. WBS-00.04 确认默认实例端口池 `18000-19999`。
5. WBS-00.05 确认 V1 默认 `routeMode=port`，名字路由只预留字段。
6. WBS-00.06 确认 Python 版本、包管理方式和项目结构。
7. WBS-00.07 确认 Docker 和 Docker Compose 为运行前置。
8. WBS-00.08 确认共享静态托管实现优先级：内置静态服务或 Caddy。
9. WBS-00.09 确认开发、测试、目标部署环境；明确目标部署 OS 为 Linux 小主机，Windows/macOS 仅作开发调试，并确认整机资源采集的跨平台降级策略（对应设计 §1、§16.4）。
10. WBS-00.10 建立 V1 风险清单。

交付物：

1. `docs/plan/local-web-access-v1-wbs-20260704.md`
2. V1 决策清单。
3. V1 风险清单。

验收标准：

1. WBS 与 V1 设计说明范围一致。
2. 所有延期项在文档中明确，不进入 V1 强制交付。
3. 后续任务能直接转成 issue 或迭代计划。

### WBS-01 工程结构与 Python 项目骨架

| 字段 | 内容 |
| --- | --- |
| 优先级 | P0 |
| 规模 | M |
| 目标 | 建立可运行、可测试、可扩展的 Python 工程底座 |
| 依赖 | WBS-00 |

任务：

1. WBS-01.01 设计仓库源码目录，例如 `src/local_webpage_access/`。
2. WBS-01.02 建立 CLI 入口，例如 `lwa`。
3. WBS-01.03 选择 CLI 框架，优先 Typer 或 Click。
4. WBS-01.04 建立核心模块边界：config、paths、registry、importer、scanner、runtime、static_gateway、manager。
5. WBS-01.05 建立统一异常类型和错误码。
6. WBS-01.06 建立统一日志模块，支持全局日志和实例日志。
7. WBS-01.07 建立测试目录和基础测试运行方式。
8. WBS-01.08 建立代码格式化和静态检查配置。
9. WBS-01.09 建立本地开发启动说明。

交付物：

1. Python 包结构。
2. `lwa --help` 可运行。
3. 基础测试框架。
4. 日志工具和错误类型。

验收标准：

1. 在仓库根目录运行 `lwa --help` 能看到命令列表。
2. 基础单元测试可以执行。
3. 代码结构能容纳后续 registry、import、runtime 等模块。

### WBS-02 全局配置与路径管理

| 字段 | 内容 |
| --- | --- |
| 优先级 | P0 |
| 规模 | M |
| 目标 | 定义全局配置文件、默认目录和路径解析规则 |
| 依赖 | WBS-01 |

任务：

1. WBS-02.01 定义 `local-web.yml` 配置结构。
2. WBS-02.02 支持配置默认值。
3. WBS-02.03 支持从项目根目录定位工作区。
4. WBS-02.04 支持配置端口池范围。
5. WBS-02.05 支持配置管理页监听地址和端口。
6. WBS-02.06 支持配置静态网关类型。
7. WBS-02.07 支持配置构建并发。
8. WBS-02.08 支持配置默认资源限制。
9. WBS-02.09 支持配置局域网 IP 获取策略。
10. WBS-02.10 实现路径工具，统一生成 inbox、apps、registry、logs、run、templates、skills 等路径。

交付物：

1. `local-web.yml` 示例。
2. 配置加载模块。
3. 路径解析模块。
4. 配置校验测试。

验收标准：

1. 无配置文件时可以使用默认配置。
2. 配置文件字段错误时输出明确错误。
3. 所有模块都通过路径工具访问工作区目录。

### WBS-03 `lwa init` 初始化

| 字段 | 内容 |
| --- | --- |
| 优先级 | P0 |
| 规模 | M |
| 目标 | 初始化 Local Webpage Access 工作区 |
| 依赖 | WBS-01、WBS-02 |

任务：

1. WBS-03.01 创建 `inbox/`。
2. WBS-03.02 创建 `apps/`。
3. WBS-03.03 创建 `registry/`。
4. WBS-03.04 创建 `logs/`。
5. WBS-03.05 创建 `run/`。
6. WBS-03.06 创建 `static-gateway/sites/`。
7. WBS-03.07 创建 `templates/` 和默认模板。
8. WBS-03.08 创建 `skills/` 目录占位。
9. WBS-03.09 写入默认 `local-web.yml`。
10. WBS-03.10 初始化 SQLite 数据库。
11. WBS-03.11 支持重复执行幂等。
12. WBS-03.12 输出初始化摘要和下一步命令。

交付物：

1. `lwa init` 命令。
2. 初始化后的目录结构。
3. 默认配置文件。
4. 初始化测试。

验收标准：

1. 空目录执行 `lwa init` 后生成 V1 设计要求的目录。
2. 重复执行不会破坏已有实例和 registry。
3. 初始化失败时不会留下不一致的半成品状态。

### WBS-04 `local-web.json` Schema 与实例模型

| 字段 | 内容 |
| --- | --- |
| 优先级 | P0 |
| 规模 | M |
| 目标 | 固化实例元数据合同，供 CLI、管理页、runtime 和 skills 共享 |
| 依赖 | WBS-02 |

任务：

1. WBS-04.01 定义 `schemaVersion`。
2. WBS-04.02 定义实例基础字段：id、name、version、kind、stack。
3. WBS-04.03 定义运行字段：runtime、servingMode、resourceProfile。
4. WBS-04.04 定义数据库字段：hasDatabase、database。
5. WBS-04.05 定义状态字段：desiredState、status、lastError。
6. WBS-04.06 定义 static 字段。
7. WBS-04.07 定义 container 字段。
8. WBS-04.08 定义 network 字段。
9. WBS-04.09 定义 entry 字段。
10. WBS-04.10 定义时间字段。
11. WBS-04.11 实现 schema 校验。
12. WBS-04.12 实现实例模型读写。
13. WBS-04.13 实现实例模型迁移预留机制。

交付物：

1. `local-web.schema.json` 或等价 Python 模型。
2. `local-web.json` 示例。
3. 实例模型读写模块。
4. schema 校验测试。

验收标准：

1. 设计文档中的静态实例和容器实例示例可以通过校验。
2. 缺失关键字段时能输出明确错误。
3. 大模型 skill 可以把 `local-web.json` 当作稳定合同。

### WBS-05 SQLite Registry

| 字段 | 内容 |
| --- | --- |
| 优先级 | P0 |
| 规模 | L |
| 目标 | 建立全局实例索引和管理页数据源 |
| 依赖 | WBS-03、WBS-04 |

任务：

1. WBS-05.01 建立 SQLite 连接和事务封装。
2. WBS-05.02 实现 schema migrations。
3. WBS-05.03 创建 `instances` 表。
4. WBS-05.04 创建 `containers` 表。
5. WBS-05.05 创建 `static_sites` 表。
6. WBS-05.06 创建 `ports` 表。
7. WBS-05.07 创建 `events` 表。
8. WBS-05.08 创建 `builds` 表。
9. WBS-05.09 创建 `resources` 表。
10. WBS-05.10 实现实例增删改查。
11. WBS-05.11 实现状态更新。
12. WBS-05.12 实现端口登记和释放。
13. WBS-05.13 实现事件写入。
14. WBS-05.14 实现构建记录写入。
15. WBS-05.15 实现资源快照写入。
16. WBS-05.16 实现从 `local-web.json` 同步到 registry。
17. WBS-05.17 实现从 Docker/静态网关观测状态回写 registry。

交付物：

1. `registry/local-web.db` 初始化逻辑。
2. registry DAO 或 repository 层。
3. migration 文件或内置迁移。
4. registry 单元测试。

验收标准：

1. `lwa init` 后数据库表结构完整。
2. 导入实例后 registry 能查询到完整摘要。
3. 状态变更、端口占用、构建记录和事件能正确写入。
4. registry 与 `local-web.json` 不一致时能通过命令重新同步。

### WBS-06 端口池与访问入口

| 字段 | 内容 |
| --- | --- |
| 优先级 | P0 |
| 规模 | M |
| 目标 | 分配局域网可访问入口，避免宿主机端口冲突 |
| 依赖 | WBS-02、WBS-05 |

任务：

1. WBS-06.01 实现端口池配置读取。
2. WBS-06.02 实现 registry 已占用端口检查。
3. WBS-06.03 实现宿主机真实监听端口检查。
4. WBS-06.04 实现端口分配策略。
5. WBS-06.05 实现端口释放。
6. WBS-06.06 实现端口冲突错误处理。
7. WBS-06.07 实现局域网 IP 推断。
8. WBS-06.08 生成 `lanUrl`。
9. WBS-06.09 生成 `healthUrl`。
10. WBS-06.10 预留 `routeMode=name` 字段但不默认启用。

交付物：

1. 端口分配模块。
2. 端口检测模块。
3. URL 生成模块。
4. 端口池测试。

验收标准：

1. 被 registry 占用的端口不会再次分配。
2. 被其他进程占用的端口不会分配。
3. 端口耗尽时返回明确错误。
4. 实例 `network.hostPort`、`lanUrl`、`healthUrl` 写入正确。

### WBS-07 zip 导入与实例目录管理

| 字段 | 内容 |
| --- | --- |
| 优先级 | P0 |
| 规模 | L |
| 目标 | 把 zip 可靠地导入为标准实例目录 |
| 依赖 | WBS-03、WBS-04、WBS-05 |

任务：

1. WBS-07.01 实现 `lwa import <zip>`。
2. WBS-07.02 校验 zip 文件存在和格式。
3. WBS-07.03 计算 zip hash。
4. WBS-07.04 生成实例 id 和 slug。
5. WBS-07.05 处理同名实例和版本号。
6. WBS-07.06 创建 `apps/<id>/`。
7. WBS-07.07 保存 `source/original.zip`。
8. WBS-07.08 解压到 `current/`。
9. WBS-07.09 创建 `public/`、`data/`、`logs/`、`docker/`。
10. WBS-07.10 防止 zip slip 路径穿越。
11. WBS-07.11 处理 zip 内单层根目录。
12. WBS-07.12 写入初始 `local-web.json`。
13. WBS-07.13 写入 registry 初始记录。
14. WBS-07.14 导入失败时清理半成品或标记 failed。

交付物：

1. `lwa import` 命令。
2. 实例目录管理模块。
3. zip 解压安全逻辑。
4. 导入测试样例。

验收标准：

1. 导入 zip 后目录结构符合 V1 设计。
2. 原始 zip 被保留。
3. `local-web.json` 和 registry 均有初始记录。
4. 恶意路径 zip 不会写出实例目录。

### WBS-08 项目扫描与运行形态识别

| 字段 | 内容 |
| --- | --- |
| 优先级 | P0 |
| 规模 | L |
| 目标 | 用确定性规则识别 V1 支持的项目类型 |
| 依赖 | WBS-07 |

任务：

1. WBS-08.01 扫描文件树并生成项目摘要。
2. WBS-08.02 识别纯静态 HTML。
3. WBS-08.03 识别 Node 项目。
4. WBS-08.04 解析 `package.json`。
5. WBS-08.05 识别纯前端 SPA。
6. WBS-08.06 识别 Node 后端。
7. WBS-08.07 识别 Next/Nuxt 等偏重项目并标记 medium。
8. WBS-08.08 识别 Python 项目。
9. WBS-08.09 解析 `requirements.txt`、`pyproject.toml`、`uv.lock`。
10. WBS-08.10 识别 Flask/FastAPI/Django/Streamlit/Gradio。
11. WBS-08.11 识别 SQLite。
12. WBS-08.12 识别 Postgres/MySQL/Redis 并标记 heavy/pending。
13. WBS-08.13 推断内部端口。
14. WBS-08.14 推断安装、构建、启动命令。
15. WBS-08.15 更新 `kind`、`stack`、`runtime`、`servingMode`、`resourceProfile`。
16. WBS-08.16 无法识别时标记 pending。
17. WBS-08.17 实现 `lwa scan` CLI：扫描 `inbox/` 与 pending 实例并触发识别（对应设计 §10）。

交付物：

1. scanner 模块。
2. stack detector。
3. internal port detector。
4. 项目摘要输出。
5. 识别规则测试。

验收标准：

1. 纯 HTML 样例识别为 `static`。
2. Vite/React 样例识别为 `frontend-static`。
3. FastAPI/Flask 样例识别为 `backend-container` 或 `fullstack-sqlite`。
4. Postgres/MySQL/Redis 项目不会自动启动。
5. 无法判断项目进入 `pending`，不会错误部署。

### WBS-09 静态网关基础能力

| 字段 | 内容 |
| --- | --- |
| 优先级 | P0 |
| 规模 | M |
| 目标 | 提供共享静态托管能力 |
| 依赖 | WBS-06、WBS-07 |

任务：

1. WBS-09.01 选择 V1 默认静态网关实现。
2. WBS-09.02 设计静态站点配置模板。
3. WBS-09.03 生成 `static-gateway/sites/<id>.conf`。
4. WBS-09.04 支持每个静态站点独立 hostPort。
5. WBS-09.05 实现静态网关 reload。
6. WBS-09.06 实现 reload 失败回滚。
7. WBS-09.07 实现启用/禁用静态路由。
8. WBS-09.08 实现静态健康检查。
9. WBS-09.09 写入 `static_sites` 表。
10. WBS-09.10 记录静态网关事件日志。

交付物：

1. 静态网关模块。
2. 静态配置模板。
3. reload/enable/disable 命令封装。
4. 静态托管测试。

验收标准：

1. `apps/<id>/public/index.html` 可通过 `lanUrl` 访问。
2. 禁用静态实例后访问失败或返回明确禁用状态。
3. 启用后恢复访问。
4. reload 失败不会破坏已有可用站点。

### WBS-10 纯静态 HTML 托管流程

| 字段 | 内容 |
| --- | --- |
| 优先级 | P0 |
| 规模 | M |
| 目标 | 打通纯静态 HTML 导入到可访问的完整路径 |
| 依赖 | WBS-08、WBS-09 |

任务：

1. WBS-10.01 识别入口 `index.html`。
2. WBS-10.02 将静态源码复制或同步到 `public/`。
3. WBS-10.03 分配 hostPort。
4. WBS-10.04 生成静态网关配置。
5. WBS-10.05 更新 `local-web.json` static/network/status。
6. WBS-10.06 写入 registry。
7. WBS-10.07 执行健康检查。
8. WBS-10.08 失败时写入 error summary。

交付物：

1. 纯静态导入处理器。
2. 静态样例 zip。
3. 静态端到端测试。

验收标准：

1. 导入纯 HTML zip 后可直接打开。
2. 不生成长期运行容器。
3. 管理页显示 `runtime=shared-static`。

### WBS-11 纯前端 SPA 构建托管流程

| 字段 | 内容 |
| --- | --- |
| 优先级 | P0 |
| 规模 | L |
| 目标 | 支持 Vite/React/Vue 等纯前端项目构建后共享静态托管 |
| 依赖 | WBS-08、WBS-09 |

任务：

1. WBS-11.01 识别 `package.json` build script。
2. WBS-11.02 选择构建执行方式。
3. WBS-11.03 支持 `npm ci`。
4. WBS-11.04 在无 lockfile 时支持 `npm install`。
5. WBS-11.05 执行 `npm run build`。
6. WBS-11.06 捕获构建日志。
7. WBS-11.07 识别 `dist/`、`build/` 等产物目录。
8. WBS-11.08 复制构建产物到 `public/`。
9. WBS-11.09 生成静态网关配置。
10. WBS-11.10 执行健康检查。
11. WBS-11.11 构建失败时标记 `build_failed`。
12. WBS-11.12 写入 builds 表和 events 表。
13. WBS-11.13 将失败上下文整理给 skill 使用。

交付物：

1. 前端构建处理器。
2. 构建日志管理。
3. Vite/React 样例 zip。
4. 前端构建端到端测试。

验收标准：

1. 导入 Vite/React zip 后能构建并访问。
2. 构建阶段不产生长期运行 Node 容器。
3. 构建失败时可以在 CLI 和管理页看到错误摘要和日志。

### WBS-12 Dockerfile 模板体系

| 字段 | 内容 |
| --- | --- |
| 优先级 | P0 |
| 规模 | M |
| 目标 | 为 Node/Python 后端生成可审查、可修复的 Dockerfile |
| 依赖 | WBS-08 |

任务：

1. WBS-12.01 建立 Node Dockerfile 模板。
2. WBS-12.02 建立 Python FastAPI 模板。
3. WBS-12.03 建立 Python Flask 模板。
4. WBS-12.04 建立 Python Django 模板。
5. WBS-12.05 建立 Streamlit/Gradio 基础模板。
6. WBS-12.06 支持内部端口替换。
7. WBS-12.07 支持启动命令替换。
8. WBS-12.08 支持环境变量注入。
9. WBS-12.09 支持 SQLite 数据目录约定。
10. WBS-12.10 输出 Dockerfile 到 `apps/<id>/docker/Dockerfile`。
11. WBS-12.11 记录模板来源和生成摘要。

交付物：

1. Dockerfile 模板。
2. 模板渲染模块。
3. Dockerfile 生成测试。

验收标准：

1. Node/Express 样例能生成 Dockerfile。
2. FastAPI/Flask 样例能生成 Dockerfile。
3. 生成文件保存在 `docker/` 下，不污染 `current/`。

### WBS-13 Compose 模板与 `.env`

| 字段 | 内容 |
| --- | --- |
| 优先级 | P0 |
| 规模 | M |
| 目标 | 生成 Docker Compose project，作为容器实例的管理单元 |
| 依赖 | WBS-06、WBS-12 |

任务：

1. WBS-13.01 建立 Compose 基础模板。
2. WBS-13.02 支持 `${HOST_PORT}:${INTERNAL_PORT}` 映射。
3. WBS-13.03 支持 `env_file`。
4. WBS-13.04 支持 `../data:/app/data` 挂载。
5. WBS-13.05 支持 `mem_limit`。
6. WBS-13.06 支持 `cpus`。
7. WBS-13.07 支持 `restart: unless-stopped`。
8. WBS-13.08 生成 `.env`。
9. WBS-13.09 生成 projectName 和 serviceName。
10. WBS-13.10 输出 `docker/compose.yaml`。
11. WBS-13.11 写入 `containers` 表。

交付物：

1. Compose 模板。
2. `.env` 生成器。
3. Compose 渲染测试。

验收标准：

1. 生成的 Compose 能被 `docker compose config` 校验通过。
2. hostPort、internalPort、memory、cpu 等字段与 `local-web.json` 一致。
3. SQLite 项目默认挂载 `data/`。

### WBS-14 Docker Runtime 封装

| 字段 | 内容 |
| --- | --- |
| 优先级 | P0 |
| 规模 | L |
| 目标 | 封装 Docker Compose build/up/start/stop/restart/logs/stats |
| 依赖 | WBS-13 |

任务：

1. WBS-14.01 实现 Docker 可用性检查。
2. WBS-14.02 实现 `docker compose build` 封装。
3. WBS-14.03 实现 `docker compose up -d` 封装。
4. WBS-14.04 实现 `docker compose stop` 封装。
5. WBS-14.05 实现 `docker compose start` 封装。
6. WBS-14.06 实现 `docker compose restart` 封装。
7. WBS-14.07 实现 `docker compose down` 内部能力，但不作为 stop 默认。
8. WBS-14.08 实现 `docker compose logs` 封装。
9. WBS-14.09 实现 container id 查询。
10. WBS-14.10 实现 image id 查询。
11. WBS-14.11 实现 Docker 状态观测。
12. WBS-14.12 实现超时和失败处理。
13. WBS-14.13 将 stdout/stderr 写入实例日志。
14. WBS-14.14 将构建结果写入 builds 表。
15. WBS-14.15 将状态变化写入 events 表。

交付物：

1. Docker runtime 模块。
2. Docker 命令执行封装。
3. 构建和运行日志。
4. Docker 集成测试。

验收标准：

1. 容器实例可以 build 和 up。
2. stop 使用 `docker compose stop`，不会删除容器。
3. start 可以从 stopped 状态恢复。
4. logs 能返回最近日志。
5. Docker 不可用时输出明确前置条件错误。

### WBS-15 Node 后端托管流程

| 字段 | 内容 |
| --- | --- |
| 优先级 | P0 |
| 规模 | M |
| 目标 | 打通 Node 后端项目导入到 Docker Compose 运行 |
| 依赖 | WBS-08、WBS-12、WBS-13、WBS-14 |

任务：

1. WBS-15.01 识别 Node 后端入口。
2. WBS-15.02 推断内部端口。
3. WBS-15.03 生成 Dockerfile。
4. WBS-15.04 生成 Compose 和 `.env`。
5. WBS-15.05 执行 build。
6. WBS-15.06 执行 up。
7. WBS-15.07 执行健康检查。
8. WBS-15.08 更新 `local-web.json`。
9. WBS-15.09 更新 registry。
10. WBS-15.10 失败时提供诊断上下文。

交付物：

1. Node 后端处理器。
2. Node/Express 样例。
3. Node 端到端测试。

验收标准：

1. Node 后端样例可通过 `lanUrl` 访问。
2. Compose stack 名称、serviceName、端口映射记录正确。
3. 失败时能看到构建或启动日志。

### WBS-16 Python 后端与 SQLite 托管流程

| 字段 | 内容 |
| --- | --- |
| 优先级 | P0 |
| 规模 | L |
| 目标 | 打通 Python Web 和 SQLite 项目的 Docker Compose 托管 |
| 依赖 | WBS-08、WBS-12、WBS-13、WBS-14 |

任务：

1. WBS-16.01 识别 FastAPI 项目。
2. WBS-16.02 识别 Flask 项目。
3. WBS-16.03 识别 Django 项目。
4. WBS-16.04 识别 Streamlit/Gradio 项目并标记 medium。
5. WBS-16.05 推断 Python 启动命令。
6. WBS-16.06 推断内部端口。
7. WBS-16.07 识别 SQLite 文件或连接串。
8. WBS-16.08 规划 `data/` 挂载。
9. WBS-16.09 注入 `DATABASE_URL`。
10. WBS-16.10 生成 Dockerfile。
11. WBS-16.11 生成 Compose 和 `.env`。
12. WBS-16.12 执行 build/up。
13. WBS-16.13 执行健康检查。
14. WBS-16.14 更新元数据和 registry。
15. WBS-16.15 失败时输出可供 skill 修复的上下文。

交付物：

1. Python 后端处理器。
2. FastAPI 或 Flask + SQLite 样例。
3. Python 端到端测试。

验收标准：

1. FastAPI/Flask 样例可通过 `lanUrl` 访问。
2. SQLite 数据保存在 `apps/<id>/data/`。
3. 重建容器后数据目录不被覆盖。
4. Streamlit/Gradio 默认标记为 medium，并遵守启动确认策略。

### WBS-17 生命周期命令

| 字段 | 内容 |
| --- | --- |
| 优先级 | P0 |
| 规模 | L |
| 目标 | 统一静态实例和容器实例的启停、重启、重建和状态命令 |
| 依赖 | WBS-09、WBS-14 |

任务：

1. WBS-17.01 实现 `lwa start <id>`。
2. WBS-17.02 实现 `lwa stop <id>`。
3. WBS-17.03 实现 `lwa restart <id>`。
4. WBS-17.04 实现 `lwa rebuild <id>`。
5. WBS-17.05 实现 `lwa remove <id>`。
6. WBS-17.06 实现 `desiredState` 更新。
7. WBS-17.07 实现 `status` 观测和回写。
8. WBS-17.08 静态实例 start/stop 映射为启用/禁用路由。
9. WBS-17.09 容器实例 start/stop 映射为 compose start/stop。
10. WBS-17.10 remove 默认提示或保留 `data/`。
11. WBS-17.11 所有生命周期动作写入 events。
12. WBS-17.12 实现并发操作锁。

交付物：

1. lifecycle 模块。
2. CLI 生命周期命令。
3. 生命周期测试。

验收标准：

1. 静态实例和容器实例都能 start/stop/restart。
2. `stop` 不删除容器和数据。
3. `desiredState` 与用户操作一致。
4. 管理页与 CLI 看到的状态一致。

### WBS-18 日志、状态与健康检查

| 字段 | 内容 |
| --- | --- |
| 优先级 | P0 |
| 规模 | M |
| 目标 | 让实例状态、日志和健康结果可观测 |
| 依赖 | WBS-17 |

任务：

1. WBS-18.01 实现 `lwa logs <id>`。
2. WBS-18.02 区分构建日志、运行日志、静态网关日志。
3. WBS-18.03 支持最近 N 行日志。
4. WBS-18.04 实现 `lwa status [id]`。
5. WBS-18.05 实现 HTTP 健康检查。
6. WBS-18.06 健康检查结果写入 `lastHealthCheckAt`。
7. WBS-18.07 失败时写入 `lastError`。
8. WBS-18.08 实现状态同步任务。
9. WBS-18.09 管理 pending/building/failed/running/stopped 状态流转。
10. WBS-18.10 将状态变化写入 events。
11. WBS-18.11 实现实例日志滚动（按大小或条数上限），避免小主机磁盘被日志占满（对应设计 §16.6）。

交付物：

1. logs 模块。
2. health check 模块。
3. status 模块。
4. 日志和状态测试。

验收标准：

1. 构建失败、启动失败、健康失败都能看到不同错误摘要。
2. 运行实例健康检查通过后状态为 running。
3. 停止实例状态为 stopped，且 desiredState 为 stopped。

### WBS-19 资源监控与统计

| 字段 | 内容 |
| --- | --- |
| 优先级 | P0 |
| 规模 | M |
| 目标 | 用轻量方式展示整机和实例资源占用 |
| 依赖 | WBS-05、WBS-14 |

任务：

1. WBS-19.01 实现 `lwa stats [id]`。
2. WBS-19.02 使用 `docker stats --no-stream` 获取容器资源。
3. WBS-19.03 读取 `/proc/meminfo`。
4. WBS-19.04 读取 `/proc/loadavg`。
5. WBS-19.05 获取磁盘占用。
6. WBS-19.06 统计实例源码目录大小。
7. WBS-19.07 统计 `public/` 大小。
8. WBS-19.08 统计 `data/` 大小。
9. WBS-19.09 统计镜像大小。
10. WBS-19.10 写入 resources 表。
11. WBS-19.11 在非 Linux 或 WSL 差异场景提供降级逻辑。

交付物：

1. stats 模块。
2. resources 写入逻辑。
3. stats CLI。
4. 资源统计测试。

验收标准：

1. 能显示整机内存、CPU/负载、磁盘概览。
2. 能显示容器 CPU 和内存。
3. 静态实例显示目录大小而不是容器资源。
4. 资源采集失败不会影响实例运行。

### WBS-20 构建队列与并发限制

| 字段 | 内容 |
| --- | --- |
| 优先级 | P1 |
| 规模 | M |
| 目标 | 保护 4G/8G 小主机，避免并发构建导致 OOM |
| 依赖 | WBS-11、WBS-14 |

任务：

1. WBS-20.01 实现构建任务模型。
2. WBS-20.02 实现构建锁。
3. WBS-20.03 默认构建并发为 1。
4. WBS-20.04 支持配置并发数。
5. WBS-20.05 记录构建排队状态。
6. WBS-20.06 构建开始和结束写入 events。
7. WBS-20.07 构建超时处理。
8. WBS-20.08 构建取消预留。

交付物：

1. build queue 模块。
2. 构建锁。
3. 排队状态显示。

验收标准：

1. 同时导入多个项目时不会并发构建超过配置值。
2. 排队中的实例状态为 building 或 queued。
3. 构建失败不会阻塞后续队列。

### WBS-21 Daemon 与 Inbox Watcher

| 字段 | 内容 |
| --- | --- |
| 优先级 | P1 |
| 规模 | L |
| 目标 | 支持 daemon on/off 双模式和自动导入 |
| 依赖 | WBS-07、WBS-20 |

任务：

1. WBS-21.01 实现 `lwa daemon on`。
2. WBS-21.02 实现 `lwa daemon off`。
3. WBS-21.03 实现 `lwa daemon status`。
4. WBS-21.04 保存 daemon 开关状态。
5. WBS-21.05 监听 `inbox/` 新 zip。
6. WBS-21.06 避免处理未写完文件。
7. WBS-21.07 自动调用 import。
8. WBS-21.08 自动处理可确定的 static/frontend/backend 项目。
9. WBS-21.09 对 uncertain/heavy 项目标记 pending。
10. WBS-21.10 记录 daemon 日志。
11. WBS-21.11 实现单实例处理锁。
12. WBS-21.12 预留 systemd user service 安装说明。

交付物：

1. daemon 模块。
2. inbox watcher。
3. daemon CLI。
4. daemon 日志。

验收标准：

1. daemon on 后把 zip 放进 inbox 可以自动导入。
2. daemon off 后不会自动处理新 zip。
3. 无法判断项目不会被错误启动，而是进入 pending。

### WBS-22 管理页后端 API

| 字段 | 内容 |
| --- | --- |
| 优先级 | P0 |
| 规模 | L |
| 目标 | 提供管理页数据和操作 API |
| 依赖 | WBS-05、WBS-17、WBS-18、WBS-19 |

任务：

1. WBS-22.01 建立 FastAPI manager 服务。
2. WBS-22.02 实现管理页静态资源托管。
3. WBS-22.03 实现实例列表 API。
4. WBS-22.04 实现实例详情 API。
5. WBS-22.05 实现顶部统计 API。
6. WBS-22.06 实现资源统计 API。
7. WBS-22.07 实现日志 API。
8. WBS-22.08 实现 start/stop/restart/rebuild 操作 API。
9. WBS-22.09 实现导入队列或 pending 列表 API。
10. WBS-22.10 实现端口池占用 API。
11. WBS-22.11 实现错误响应格式。
12. WBS-22.12 实现 API token 验证。
13. WBS-22.13 实现 `lwa manager start`。

交付物：

1. FastAPI manager。
2. API 路由。
3. API token 机制。
4. API 测试。

验收标准：

1. `http://<LAN IP>:17800` 可打开管理页。
2. API 能返回实例列表、详情、日志和资源。
3. 管理页操作最终调用同一套 lifecycle 逻辑。
4. 未授权请求被拒绝。

### WBS-23 管理页前端

| 字段 | 内容 |
| --- | --- |
| 优先级 | P0 |
| 规模 | L |
| 目标 | 提供本地 Hub 管理体验 |
| 依赖 | WBS-22 |

任务：

1. WBS-23.01 设计页面信息架构。
2. WBS-23.02 实现顶部统计区。
3. WBS-23.03 实现实例列表。
4. WBS-23.04 展示名称、状态、类型、运行层、技术栈、数据库、端口、访问地址、资源、版本、时间。
5. WBS-23.05 实现打开实例按钮。
6. WBS-23.06 实现 start/stop/restart 操作。
7. WBS-23.07 实现日志查看。
8. WBS-23.08 实现实例详情页或详情抽屉。
9. WBS-23.09 展示 `local-web.json` 摘要。
10. WBS-23.10 展示 Dockerfile/Compose/静态配置摘要。
11. WBS-23.11 展示构建记录和健康检查。
12. WBS-23.12 实现 pending/failed 高亮。
13. WBS-23.13 实现 10-30 秒低频轮询资源数据。
14. WBS-23.14 兼容小屏幕基础布局。
15. WBS-23.15 采用克制的工具型 UI，不做营销首页。

交付物：

1. 管理页前端。
2. 实例列表页。
3. 实例详情视图。
4. 操作反馈和错误提示。

验收标准：

1. 首页直接展示实例，不是 landing page。
2. 用户可以从页面打开、启动、停止、重启实例。
3. failed/pending 实例能清楚看到原因和日志入口。
4. 资源数据和状态刷新不会明显增加小主机负担。

### WBS-24 大模型 Skills 文档

| 字段 | 内容 |
| --- | --- |
| 优先级 | P1 |
| 规模 | L |
| 目标 | 编写 skills，用于复杂识别、Docker 化补全和失败修复 |
| 依赖 | WBS-04、WBS-08、WBS-12、WBS-13、WBS-18 |

任务：

1. WBS-24.01 编写 `detect-stack` skill。
2. WBS-24.02 编写 `detect-internal-port` skill。
3. WBS-24.03 编写 `build-frontend-static` skill。
4. WBS-24.04 编写 `dockerize-node-app` skill。
5. WBS-24.05 编写 `dockerize-python-app` skill。
6. WBS-24.06 编写 `dockerize-fullstack-sqlite` skill。
7. WBS-24.07 编写 `generate-static-gateway-config` skill。
8. WBS-24.08 编写 `generate-compose` skill。
9. WBS-24.09 编写 `fix-docker-build-failure` skill。
10. WBS-24.10 编写 `fix-container-startup-failure` skill。
11. WBS-24.11 编写 `fix-port-binding` skill。
12. WBS-24.12 编写 `diagnose-health-check` skill。
13. WBS-24.13 为每个 skill 定义输入、输出、可修改文件和禁止事项。
14. WBS-24.14 编写 skill 使用示例。
15. WBS-24.15 编写 pending 实例处理流程。

交付物：

1. `skills/` 目录。
2. 各 skill 的 `SKILL.md` 或流程文档。
3. skill 输入输出规范。
4. skill 使用示例。

验收标准：

1. 每个 skill 都明确只生成/修复配置，不直接承担长期运行。
2. skill 输出能回到 `local-web.json`、Dockerfile、Compose 或静态配置。
3. pending/failed 实例有明确的大模型处理入口。

### WBS-25 安全、权限与默认保护

| 字段 | 内容 |
| --- | --- |
| 优先级 | P1 |
| 规模 | M |
| 目标 | 固化 V1 默认安全边界，降低误操作风险 |
| 依赖 | WBS-22 |

任务：

1. WBS-25.01 管理页 token 或密码保护。
2. WBS-25.02 默认只绑定局域网或本机可配置地址。
3. WBS-25.03 Docker Compose 模板不使用 privileged。
4. WBS-25.04 不挂载 Docker socket 到实例容器。
5. WBS-25.05 实例只挂载自己的 `data/`。
6. WBS-25.06 heavy 项目默认不自动启动。
7. WBS-25.07 remove 操作增加确认和数据保护。
8. WBS-25.08 记录构建和启动计划。
9. WBS-25.09 明确未知 zip 风险提示。
10. WBS-25.10 检查 zip slip 和路径穿越。

交付物：

1. 管理页认证。
2. 安全检查模块。
3. 危险操作保护。
4. 安全文档。

验收标准：

1. 未授权不能操作管理页 API。
2. 实例容器不会获得 Docker socket 或宿主机敏感目录。
3. heavy 项目不会静默自动启动。

### WBS-26 `lwa doctor` 与排障辅助

| 字段 | 内容 |
| --- | --- |
| 优先级 | P1 |
| 规模 | M |
| 目标 | 提供环境和实例问题诊断入口 |
| 依赖 | WBS-18、WBS-19 |

任务：

1. WBS-26.01 实现 `lwa doctor`。
2. WBS-26.02 检查 Python 版本。
3. WBS-26.03 检查 Docker 可用性。
4. WBS-26.04 检查 Docker Compose 可用性。
5. WBS-26.05 检查端口池可用性。
6. WBS-26.06 检查 SQLite registry。
7. WBS-26.07 检查静态网关。
8. WBS-26.08 检查磁盘空间。
9. WBS-26.09 检查内存和 swap。
10. WBS-26.10 对单实例执行健康诊断。
11. WBS-26.11 输出修复建议。

交付物：

1. doctor 命令。
2. 环境检查项。
3. 实例诊断报告。

验收标准：

1. Docker 未安装时 doctor 能明确指出。
2. 端口冲突时 doctor 能定位。
3. 实例失败时 doctor 能列出日志、健康检查和最近错误。

### WBS-27 样例项目与测试夹具

| 字段 | 内容 |
| --- | --- |
| 优先级 | P0 |
| 规模 | M |
| 目标 | 准备 V1 验收所需样例 zip |
| 依赖 | WBS-07 |

任务：

1. WBS-27.01 准备纯静态 HTML 样例。
2. WBS-27.02 准备 Vite/React 纯前端样例。
3. WBS-27.03 准备 Node/Express 后端样例（无数据库）。
4. WBS-27.04 准备 FastAPI + SQLite 样例。
5. WBS-27.05 准备构建失败样例。
6. WBS-27.06 准备无法识别 pending 样例。
7. WBS-27.07 打包为 zip。
8. WBS-27.08 编写样例说明。

交付物：

1. `examples/` 或 `tests/fixtures/`。
2. 四个核心样例 zip（静态 HTML、Vite/React、Node/Express、FastAPI+SQLite）。
3. 失败和 pending 样例。

验收标准：

1. 四个核心样例可以稳定复现 V1 核心路径（含 Node 后端 `backend-container` 路径）。
2. 失败样例能触发 failed 状态。
3. pending 样例不会被错误部署。

### WBS-28 单元测试与集成测试

| 字段 | 内容 |
| --- | --- |
| 优先级 | P0 |
| 规模 | L |
| 目标 | 为关键模块建立自动化测试 |
| 依赖 | WBS-01 到 WBS-19 |

任务：

1. WBS-28.01 配置测试运行命令。
2. WBS-28.02 测试配置加载。
3. WBS-28.03 测试路径解析。
4. WBS-28.04 测试 schema 校验。
5. WBS-28.05 测试 registry DAO。
6. WBS-28.06 测试端口分配。
7. WBS-28.07 测试 zip 导入。
8. WBS-28.08 测试项目识别。
9. WBS-28.09 测试静态配置生成。
10. WBS-28.10 测试 Dockerfile 生成。
11. WBS-28.11 测试 Compose 生成。
12. WBS-28.12 测试生命周期状态流转。
13. WBS-28.13 测试资源统计解析。
14. WBS-28.14 测试管理页 API。
15. WBS-28.15 对 Docker 相关测试设置可跳过条件。

交付物：

1. 单元测试。
2. 集成测试。
3. 测试夹具。
4. 测试运行文档。

验收标准：

1. 非 Docker 单测可在本机稳定执行。
2. Docker 集成测试在具备 Docker 环境时可执行。
3. 核心路径有覆盖，失败路径有基本覆盖。

### WBS-29 端到端验收

| 字段 | 内容 |
| --- | --- |
| 优先级 | P0 |
| 规模 | L |
| 目标 | 用真实样例验证 V1 完整闭环 |
| 依赖 | WBS-10、WBS-11、WBS-15、WBS-16、WBS-22、WBS-23 |

任务：

1. WBS-29.01 在干净工作区执行 `lwa init`。
2. WBS-29.02 导入纯静态 HTML zip。
3. WBS-29.03 验证静态实例目录结构。
4. WBS-29.04 验证静态实例可访问。
5. WBS-29.05 导入 Vite/React zip。
6. WBS-29.06 验证前端构建产物。
7. WBS-29.07 验证前端实例可访问。
8. WBS-29.08 导入 Node/Express 后端 zip。
9. WBS-29.09 验证 Node 后端 Docker build/up 与可访问性。
10. WBS-29.10 导入 FastAPI/Flask + SQLite zip。
11. WBS-29.11 验证 Docker build/up。
12. WBS-29.12 验证 SQLite 数据目录挂载。
13. WBS-29.13 验证 start/stop/restart。
14. WBS-29.14 验证 logs/status/stats。
15. WBS-29.15 验证管理页实例列表。
16. WBS-29.16 验证管理页操作按钮。
17. WBS-29.17 验证 failed/pending 展示。
18. WBS-29.18 记录验收结果和问题清单。

交付物：

1. E2E 验收脚本或手工验收清单。
2. 验收记录。
3. 问题清单。

验收标准：

1. 四个核心样例全部能完成导入、运行和展示。
2. 停止和重启不会丢数据。
3. 管理页展示与 CLI 状态一致。
4. 失败路径可解释、可排障。

### WBS-30 文档与发布准备

| 字段 | 内容 |
| --- | --- |
| 优先级 | P0 |
| 规模 | M |
| 目标 | 形成 V1 可交付资料 |
| 依赖 | WBS-29 |

任务：

1. WBS-30.01 编写 README。
2. WBS-30.02 编写安装前置条件。
3. WBS-30.03 编写快速开始。
4. WBS-30.04 编写 CLI 命令说明。
5. WBS-30.05 编写目录结构说明。
6. WBS-30.06 编写管理页说明。
7. WBS-30.07 编写实例导入说明。
8. WBS-30.08 编写 Docker 和静态托管说明。
9. WBS-30.09 编写常见问题。
10. WBS-30.10 编写安全边界说明。
11. WBS-30.11 编写 V1 发布清单。
12. WBS-30.12 编写 V1 已知限制。

交付物：

1. README。
2. 安装文档。
3. 使用文档。
4. 排障文档。
5. 发布清单。

验收标准：

1. 新用户可以按文档完成 init、import 和打开管理页。
2. 常见失败能在文档中找到排查路径。
3. 文档明确 V1 不支持的范围。

## 5. 推荐实施顺序

### Phase 0：准备与底座

目标：让项目有可运行的 CLI、配置、目录和 registry。

任务：

1. WBS-00 项目基线与实施准备
2. WBS-01 工程结构与 Python 项目骨架
3. WBS-02 全局配置与路径管理
4. WBS-03 `lwa init` 初始化
5. WBS-04 `local-web.json` Schema 与实例模型
6. WBS-05 SQLite Registry

阶段验收：

1. `lwa --help` 可运行。
2. `lwa init` 可初始化工作区。
3. SQLite registry 可创建。
4. `local-web.json` schema 可校验。

### Phase 1：导入、识别与端口

目标：让 zip 进入标准实例目录，并能识别基本类型。

任务：

1. WBS-06 端口池与访问入口
2. WBS-07 zip 导入与实例目录管理
3. WBS-08 项目扫描与运行形态识别

阶段验收：

1. 导入 zip 后生成完整实例目录。
2. 四个核心样例能分别识别为 static、frontend-static、backend-container（Node）、fullstack-sqlite（Python+SQLite）。
3. hostPort、lanUrl、healthUrl 生成正确。

### Phase 2：静态路径闭环

目标：先跑通最轻的静态和纯前端场景。

任务：

1. WBS-09 静态网关基础能力
2. WBS-10 纯静态 HTML 托管流程
3. WBS-11 纯前端 SPA 构建托管流程

阶段验收：

1. 纯 HTML zip 导入后可访问。
2. Vite/React zip 构建后可访问。
3. 两类实例均进入 shared-static，不产生长期容器。

### Phase 3：Docker Compose 路径闭环

目标：跑通 Node/Python 后端和 SQLite 全栈应用。

任务：

1. WBS-12 Dockerfile 模板体系
2. WBS-13 Compose 模板与 `.env`
3. WBS-14 Docker Runtime 封装
4. WBS-15 Node 后端托管流程
5. WBS-16 Python 后端与 SQLite 托管流程

阶段验收：

1. Node 后端样例可通过 Docker Compose 运行。
2. FastAPI/Flask + SQLite 样例可运行。
3. stop/start/rebuild 不破坏数据目录。

### Phase 4：生命周期、日志、资源与队列

目标：让实例可管理、可观测，并适配小主机。

任务：

1. WBS-17 生命周期命令
2. WBS-18 日志、状态与健康检查
3. WBS-19 资源监控与统计
4. WBS-20 构建队列与并发限制

阶段验收：

1. CLI 能统一管理静态和容器实例。
2. 状态流转可追踪。
3. 日志、健康检查和资源信息可查看。
4. 构建并发默认限制为 1。

### Phase 5：自动化与管理页

目标：让工具具备日常可用的本地管理体验。

任务：

1. WBS-21 Daemon 与 Inbox Watcher
2. WBS-22 管理页后端 API
3. WBS-23 管理页前端

阶段验收：

1. daemon on/off 行为正确。
2. 管理页能展示实例和整机状态。
3. 管理页能执行打开、启动、停止、重启、日志查看。

### Phase 6：Skills、安全与排障

目标：补齐 AI 辅助修复入口和默认安全保护。

任务：

1. WBS-24 大模型 Skills 文档
2. WBS-25 安全、权限与默认保护
3. WBS-26 `lwa doctor` 与排障辅助

阶段验收：

1. pending/failed 实例有 skill 处理路径。
2. 管理页 API 有 token 保护。
3. doctor 能诊断 Docker、端口、静态网关、实例健康。

### Phase 7：测试、验收与发布

目标：形成可交付的 V1。

任务：

1. WBS-27 样例项目与测试夹具
2. WBS-28 单元测试与集成测试
3. WBS-29 端到端验收
4. WBS-30 文档与发布准备

阶段验收：

1. 四个核心样例通过端到端验收。
2. CLI、管理页、registry 状态一致。
3. README 和安装文档可支持新用户使用。

## 6. 关键依赖关系

```text
WBS-00
  -> WBS-01
  -> WBS-02
  -> WBS-03
  -> WBS-04
  -> WBS-05
  -> WBS-06
  -> WBS-07
  -> WBS-08

WBS-08
  -> WBS-09 -> WBS-10 -> WBS-11
  -> WBS-12 -> WBS-13 -> WBS-14 -> WBS-15/WBS-16

WBS-09 + WBS-14
  -> WBS-17
  -> WBS-18
  -> WBS-19
  -> WBS-20

WBS-17 + WBS-18 + WBS-19
  -> WBS-22
  -> WBS-23

WBS-07 + WBS-20
  -> WBS-21

WBS-18
  -> WBS-24
  -> WBS-26

WBS-10 + WBS-11 + WBS-15 + WBS-16 + WBS-23
  -> WBS-29
  -> WBS-30
```

## 7. 验收总清单

V1 总体验收需要满足：

1. 在干净目录执行 `lwa init` 成功。
2. `local-web.yml`、目录结构和 SQLite registry 正确创建。
3. 导入纯静态 HTML zip 后可通过局域网 URL 访问。
4. 导入 Vite/React zip 后能构建为静态产物并访问。
5. 导入 FastAPI/Flask + SQLite zip 后能通过 Docker Compose 运行。
6. 每个实例都有正确的 `local-web.json`。
7. SQLite registry 能查询到所有实例。
8. 端口池不会分配冲突端口。
9. `start`、`stop`、`restart`、`rebuild`、`logs`、`status`、`stats` 可用。
10. `stop` 不等于 `down`，不会删除容器或数据。
11. 静态实例不产生长期容器。
12. 容器实例有 Compose project、serviceName、internalPort、hostPort 记录。
13. SQLite 数据保存在 `apps/<id>/data/`。
14. 构建失败会进入 failed，并保存日志和错误摘要。
15. 无法识别或 heavy 项目进入 pending，不自动启动。
16. daemon on 后会自动处理 inbox zip。
17. daemon off 后不会自动处理 inbox zip。
18. 管理页能展示实例列表、状态、资源、日志和操作。
19. 管理页 API 有基础 token 保护。
20. `lwa doctor` 能检查 Docker、端口、registry 和实例健康。
21. 四个核心样例通过端到端验证。
22. README、安装文档、使用文档和排障文档完整。

## 8. 主要风险与缓解

| 风险 | 影响 | 缓解措施 |
| --- | --- | --- |
| 项目识别规则不完整 | 导入失败或错误部署 | 简单规则先行，无法判断进入 pending，由 skill 修复 |
| npm/pip 构建内存峰值高 | 4G 小主机 OOM | 默认构建并发 1，建议 swap，medium/heavy 不自动启动 |
| Docker 环境差异 | 容器启动失败 | `lwa doctor` 检查 Docker/Compose 版本和权限 |
| 静态网关 reload 失败 | 影响已有站点 | 配置生成后先校验，reload 失败回滚 |
| 端口冲突 | 实例不可访问 | registry + 宿主机监听双重检查 |
| SQLite 数据被覆盖 | 用户数据丢失 | 数据目录固定在 `data/`，rebuild 不覆盖，remove 默认保护 |
| 管理页越做越大 | 偏离工具定位 | 首页围绕实例库，不做通用服务器面板 |
| 大模型 skill 边界失控 | 行为不可预测 | skill 只生成/修复配置，最终执行交给 `lwa` |
| 未知 zip 安全风险 | 执行不可信代码 | 默认可信来源，heavy/pending 不自动启动，限制挂载和权限 |
| 日志/记录/镜像无界增长 | 小主机磁盘被占满 | 日志按大小滚动，registry 记录可清理，`lwa doctor` 与管理页在磁盘接近阈值时提示 |

## 9. V1 之后的延后工作

以下内容不进入 V1 WBS，可作为 V1.1/V1.2/V2：

1. Traefik/Caddy + nip.io/sslip.io 名字路由。
2. Postgres/MySQL/Redis 多服务 Compose。
3. 实例备份和恢复。
4. 版本升级和回滚。
5. 镜像和构建缓存清理。
6. 闲置实例自动停止。
7. 模板/配方目录。
8. Git 仓库导入。
9. 公网域名和证书管理。
10. 多机器部署。
11. 实例迁移包。
12. 多用户权限和审计。

## 10. 建议的最小开发切片

如果希望尽快看到可运行成果，建议按以下切片推进：

1. **切片 A：静态 HTML 最小闭环**  
   `init -> import -> detect static -> allocate port -> static gateway -> health check -> registry -> open URL`

2. **切片 B：管理页只读版**  
   `registry -> FastAPI API -> 实例列表 -> 打开 URL`

3. **切片 C：前端构建闭环**  
   `detect frontend -> npm build -> public -> static gateway -> build logs`

4. **切片 D：Docker 后端闭环**  
   `detect backend -> Dockerfile -> compose -> build/up -> logs/status`

5. **切片 E：生命周期闭环**  
   `start/stop/restart/rebuild -> desiredState/status -> events -> 管理页操作`

6. **切片 F：资源与 daemon**  
   `stats -> build queue -> daemon watcher -> pending/failed`

7. **切片 G：skills 与验收**  
   `skills docs -> failed diagnosis -> examples -> E2E -> release docs`

这个切片顺序能在早期就验证核心产品方向：Local Webpage Access 不是通用服务器面板，而是围绕“本地 zip 实例库 + AI 辅助识别部署”的轻量部署基座。
