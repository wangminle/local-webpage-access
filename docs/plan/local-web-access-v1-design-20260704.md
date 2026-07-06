# Local Webpage Access V1 设计说明

> 日期：2026-07-04  
> 状态：V1 设计稿  
> 基础文档：`docs/discussion/local-web-access-设计意见-20260703.md`  
> 参考吸收：`docs/discussion/local-web-access-方案-20260703.md`、`docs/discussion/local-web-access-proposal-20260703.md`

## 1. V1 目标

Local Webpage Access 是一个面向局域网小主机的本地网页部署基座，用来集中导入、运行、管理 AI coding 过程中产生的小网页、小工具和小型 Web 应用。

V1 的核心目标是建立一个可闭环运行的最小系统：

1. 用户把 zip 包放入固定入口目录。
2. 工具识别项目类型，生成实例元数据和运行配置。
3. 纯静态页面和可构建成静态产物的前端项目进入共享静态托管。
4. 需要后端进程的 Node/Python 项目进入 Docker Compose 托管。
5. SQLite 数据放入实例数据目录，避免升级代码时丢失。
6. 系统统一分配访问入口，避免端口冲突。
7. 管理页展示实例列表、状态、访问地址、技术栈、数据库、版本、日志和资源占用。
8. 支持实例级启动、停止、重启、日志查看和重新构建。

一句话定位：

**Local Webpage Access V1 是一个 Docker 优先、静态轻量托管、由确定性工具执行、大模型 skill 辅助识别与修复的本地小型 PaaS。**

运行环境假设：

1. **目标部署环境是 Linux 小主机**（如 4G/8G 的迷你主机、NAS、软路由旁挂机），默认原生安装 Docker 与 Docker Compose。第 13、14、16 节中的 `/proc`、绝对路径和 Compose 语法均以此为前提。
2. **Windows/macOS 仅作为开发与调试环境**。在这些平台上，Docker 通过 Docker Desktop/WSL2 运行，宿主机 `/proc` 不可直接读取，因此第 16.4 节的整机资源采集需要走降级逻辑（见该节说明）。
3. V1 不承诺在 Windows/macOS 上提供与 Linux 一致的整机资源指标，但核心闭环（导入、识别、静态托管、Compose 托管、管理页）应在开发机上可运行。

## 2. 关键设计结论

V1 采用以下结论作为实现基线：

1. **Docker Compose 是复杂实例默认运行层。**  
   Node/Python 后端、服务端渲染应用、带 SQLite 的全栈小应用，默认生成 Dockerfile 和 Compose 配置。

2. **共享静态托管是轻量网页默认运行层。**  
   纯 HTML 和构建后的 Vite/React/Vue SPA 不为每个实例常驻容器，而是放入共享静态服务。

3. **确定性工具负责落地执行。**  
   `lwa` CLI/服务负责解压、登记、分配端口、生成配置、调用 Docker、更新 SQLite registry、刷新静态网关和提供管理页。

4. **大模型 skill 负责复杂判断和修复，不负责长期保活。**  
   大模型用于识别陌生项目、补全 Dockerfile、修复 compose、分析构建/启动失败日志。最终执行仍交给 `lwa`。

5. **实例元数据是工具与 skill 的合同。**  
   每个实例都有 `local-web.json`，SQLite registry 作为全局查询和管理页数据源。

6. **V1 默认使用端口池直连，预留名字路由扩展。**  
   基础方案使用宿主机端口池暴露实例，简单直接、依赖少。后续可启用 Traefik/Caddy 按名字路由，例如 `demo.<lan-ip>.nip.io`。

7. **实例开关语义必须明确。**  
   关闭实例等于 `stop`，不是删除。Docker 实例用 `docker compose stop/start`；静态实例用启用/禁用静态路由模拟开关。

8. **资源策略是 V1 的基础能力，不是后补功能。**  
   V1 默认限制构建并发，设置容器资源上限，展示 CPU/内存/磁盘信息，避免 4G/8G 小主机被未知项目拖垮。

9. **参考成熟面板和自托管 PaaS，但保持 zip-first 差异化。**  
   参考 1Panel 的服务器管理体验，参考 Coolify/Dokploy 的自托管应用部署流程，参考 Dockge 的 Compose stack 管理，参考 Runtipi/CasaOS 的家庭服务器应用目录体验；但核心入口仍是“本地 zip 实例库 + AI 辅助识别部署”。

## 3. 非目标

V1 暂不解决以下问题：

1. 不追求完全无人值守处理任意未知 zip。
2. 不默认自动部署 Postgres、MySQL、Redis 等重型数据库。
3. 不实现多用户权限系统。
4. 不实现跨机器同步或集群调度。
5. 不引入 Prometheus、cAdvisor 等重型监控组件。
6. 不把管理页暴露到公网。
7. 不承诺未知 zip 的强安全沙箱能力。
8. 不做另一个宝塔、1Panel 或 Coolify 这类大而全服务器面板。

## 4. 总体架构

```text
用户 zip
  -> inbox/
  -> lwa import / daemon watcher
  -> 解压到 apps/<id>/
  -> 确定性扫描
  -> 生成 local-web.json 草稿
  -> 判断运行形态
      -> static/shared-static
      -> frontend-static build 后 shared-static
      -> backend-container docker-compose
      -> fullstack-sqlite docker-compose + data/
      -> uncertain/heavy pending
  -> 分配访问入口
  -> 生成静态网关配置或 Dockerfile/compose.yaml
  -> build/start/reload
  -> 健康检查
  -> 写入 SQLite registry
  -> 管理页展示
```

架构分层如下：

| 层 | 组件 | 职责 | 是否依赖大模型 |
| --- | --- | --- | --- |
| 导入层 | `lwa import` / watcher | 接收 zip、解压、命名、版本化、建立实例目录 | 否 |
| 判断层 | 扫描器 + skill | 判断技术栈、入口、端口、数据库、运行形态 | 简单项目否，复杂项目是 |
| 配置层 | 模板渲染器 | 生成 `local-web.json`、Dockerfile、Compose、静态网关配置 | 否 |
| 运行层 | Docker Compose / 共享静态服务 | 启动、停止、重启、日志、保活 | 否 |
| 数据层 | SQLite registry + 实例目录 | 全局索引、实例元数据、数据目录 | 否 |
| 展示层 | FastAPI Hub | 管理页、状态、日志、资源、操作入口 | 否 |
| 修复层 | skill | Docker 化补全、构建失败诊断、启动失败诊断 | 是 |

## 5. 运行形态

V1 只定义四种主要运行形态。

| 运行形态 | 适用项目 | 默认托管方式 | 是否常驻容器 |
| --- | --- | --- | --- |
| `static` | 纯 HTML/CSS/JS | 共享静态托管 | 否 |
| `frontend-static` | Vite/React/Vue 等纯前端 SPA | 构建后共享静态托管 | 否 |
| `backend-container` | Express/FastAPI/Flask/Django/Streamlit/Gradio 等 | Docker Compose | 是 |
| `fullstack-sqlite` | 带 SQLite 的小型全栈应用 | Docker Compose + `data/` 挂载 | 是 |

对于 Postgres/MySQL/Redis、多服务 Compose、任务队列、浏览器自动化、大模型本地服务等重型项目，V1 只做识别和标记，默认进入 `pending` 或 `heavy`，不自动启动。

## 6. 路由与访问入口

### 6.1 V1 默认：端口池直连

V1 默认采用端口池方式，降低部署前置要求。

```text
管理页端口：17800
实例端口池：18000-19999
```

实例访问地址示例：

```text
http://192.168.1.20:18023
```

选择端口池作为 V1 默认，是因为：

1. 不依赖局域网 DNS、通配域名或 hosts 配置。
2. 问题定位直接，浏览器访问和健康检查都简单。
3. 与 `local-web.json`、SQLite registry 和管理页字段天然匹配。
4. 对第一版实现更稳，方便先跑通完整闭环。

端口分配规则：

1. 从配置的端口池中选择未登记端口。
2. 检查宿主机真实监听状态。
3. 容器内部端口和宿主机端口分开记录。
4. 静态实例也分配宿主机访问端口，由共享静态网关监听或转发。
5. 如果端口已被占用，自动顺延到下一个可用端口。

### 6.2 预留增强：按名字路由

另外两份讨论稿中 Traefik/Caddy 按名字路由的价值很高，V1 需要在模型上预留，但不作为默认硬依赖。

可选路线：

```text
demo.192.168.1.20.nip.io
hub.192.168.1.20.nip.io
```

未来启用后，可以由 Traefik 或 Caddy 根据实例名路由到对应容器或静态目录。其收益是：

1. 用户不需要记端口。
2. 容器实例不必逐个发布宿主机端口。
3. 管理页展示更可读的访问地址。
4. 端口冲突问题进一步弱化。

为此，V1 的元数据中保留 `routeMode`、`routeHost`、`lanUrl` 字段。默认 `routeMode=port`，后续可切换为 `name`。

## 7. 目录结构

V1 推荐目录结构：

```text
local-webpage-access/
  AGENTS.md
  local-web.yml
  inbox/
  apps/
  registry/
    local-web.db
  logs/
  manager/
  static-gateway/
    sites/
  skills/
    detect-stack/
    build-frontend-static/
    dockerize-node-app/
    dockerize-python-app/
    fix-docker-build-failure/
    fix-container-startup-failure/
  templates/
    static/
    node/
    python/
    fullstack-sqlite/
  run/
```

每个实例目录：

```text
apps/
  <id>/
    source/
      original.zip
    current/
    public/
    data/
    logs/
    docker/
      Dockerfile
      compose.yaml
      .env
    local-web.json
```

说明：

1. `source/original.zip` 保留原始导入包。
2. `current/` 是当前版本源码。
3. `public/` 是共享静态托管的产物目录。
4. `data/` 是 SQLite、上传文件、用户数据等持久化目录。
5. `docker/` 只存工具生成的运行配置，不污染项目源码。
6. `local-web.json` 是实例级元数据和运行配方。

## 8. 实例元数据

`local-web.json` 是实例目录内的真相文件，SQLite registry 是全局索引。V1 以 `local-web.json` 为合同，供 CLI、管理页、静态网关、Docker Compose 和大模型 skill 共同读取。

### 8.0 字段术语与取值表

为避免各模块对相邻概念理解不一致，V1 固定以下四个正交字段的职责与取值域。第 5 节的"运行形态"是这四个字段的组合结论，不作为独立存储字段。

| 字段 | 职责 | 取值域 | 说明 |
| --- | --- | --- | --- |
| `kind` | 项目技术族 | `static` / `node` / `python` | 描述源项目属于哪种技术栈，纯前端 SPA 归入 `node` |
| `runtime` | 底层运行机制 | `shared-static` / `docker-compose` | 决定由静态网关托管还是由 Docker Compose 运行 |
| `servingMode` | 对外服务方式 | `shared-static` / `container` | `runtime=shared-static` 恒为 `shared-static`；`runtime=docker-compose` 恒为 `container` |
| `resourceProfile` | 资源档位 | `tiny` / `small` / `medium` / `heavy` | 见第 16 节资源策略 |

派生关系：

| §5 运行形态 | `kind` | `runtime` | `servingMode` | 典型 `resourceProfile` |
| --- | --- | --- | --- | --- |
| `static` | `static` | `shared-static` | `shared-static` | `tiny` |
| `frontend-static` | `node` | `shared-static` | `shared-static` | `tiny` |
| `backend-container` | `node` / `python` | `docker-compose` | `container` | `small` / `medium` |
| `fullstack-sqlite` | `node` / `python` | `docker-compose` | `container` | `small` / `medium` |

说明：`runtime` 与 `servingMode` 目前一一对应，V1 保留两个字段是为了区分"由什么机制运行"与"以什么方式对外服务"，为后续引入非容器的常驻进程或名字路由预留演进空间；实现时若确认无需区分，可在 V1.1 合并。

示例：

```json
{
  "schemaVersion": 1,
  "id": "my-demo",
  "name": "My Demo",
  "version": "2026.07.04-1",
  "kind": "node",
  "stack": ["vite", "react"],
  "runtime": "shared-static",
  "servingMode": "shared-static",
  "resourceProfile": "tiny",
  "hasDatabase": false,
  "database": null,
  "desiredState": "running",
  "status": "running",
  "static": {
    "root": "public",
    "gateway": "caddy",
    "routeMode": "port",
    "gatewayConfigPath": "static-gateway/sites/my-demo.conf"
  },
  "container": null,
  "network": {
    "host": "0.0.0.0",
    "internalPort": null,
    "hostPort": 18023,
    "routeMode": "port",
    "routeHost": null,
    "lanUrl": "http://192.168.1.20:18023",
    "healthUrl": "http://127.0.0.1:18023"
  },
  "entry": {
    "install": "npm ci",
    "build": "npm run build",
    "start": null
  },
  "createdAt": "2026-07-04T10:00:00+08:00",
  "updatedAt": "2026-07-04T10:00:00+08:00",
  "lastStartedAt": "2026-07-04T10:05:00+08:00",
  "lastHealthCheckAt": "2026-07-04T10:06:00+08:00",
  "lastError": null
}
```

容器实例示例差异：

```json
{
  "runtime": "docker-compose",
  "servingMode": "container",
  "resourceProfile": "small",
  "desiredState": "running",
  "container": {
    "projectName": "lwa-my-api",
    "serviceName": "app",
    "image": "lwa/my-api:2026.07.04-1",
    "internalPort": 8000,
    "composePath": "docker/compose.yaml",
    "dockerfilePath": "docker/Dockerfile",
    "resourceLimits": {
      "memory": "512m",
      "cpus": "0.75"
    }
  },
  "network": {
    "host": "0.0.0.0",
    "internalPort": 8000,
    "hostPort": 18024,
    "routeMode": "port",
    "routeHost": null,
    "lanUrl": "http://192.168.1.20:18024",
    "healthUrl": "http://127.0.0.1:18024"
  }
}
```

### 8.1 `desiredState` 与 `status`

V1 明确区分用户期望状态和实际状态：

| 字段 | 含义 |
| --- | --- |
| `desiredState` | 用户想让实例处于什么状态：`running` / `stopped` |
| `status` | 当前观测到的状态：`pending` / `building` / `running` / `stopped` / `failed` |

这样可以避免一个常见问题：用户手动停止实例后，系统重启又自动把它拉起来。

Docker Compose 实例使用：

```yaml
restart: unless-stopped
```

实例关闭使用：

```bash
docker compose stop
```

而不是：

```bash
docker compose down
```

`down` 只用于卸载、重建或清理。

## 9. SQLite Registry

SQLite registry 是管理页和全局查询的数据源。`local-web.json` 是实例内备份和配置，SQLite 是全局索引和状态缓存。

V1 表结构建议：

```sql
instances
  id
  name
  version
  kind
  runtime
  serving_mode
  resource_profile
  stack_json
  has_database
  database_type
  desired_state
  status
  app_path
  source_zip_path
  created_at
  updated_at
  last_started_at
  last_health_check_at
  last_error

containers
  instance_id
  compose_project
  service_name
  image
  image_id
  container_id
  internal_port
  host_port
  route_mode
  route_host
  compose_path
  dockerfile_path
  memory_limit
  cpu_limit

static_sites
  instance_id
  root_path
  gateway
  route_mode
  host_port
  route_host
  gateway_config_path
  enabled

ports
  port
  instance_id
  status
  created_at

events
  id
  instance_id
  event_type
  message
  created_at

builds
  id
  instance_id
  status
  started_at
  finished_at
  log_path
  error_summary

resources
  instance_id
  source_size_bytes
  public_size_bytes
  data_size_bytes
  image_size_bytes
  last_memory_bytes
  last_cpu_percent
  updated_at
```

## 10. CLI 设计

V1 CLI 名称采用：

```bash
lwa
```

核心命令：

```bash
lwa init
lwa import inbox/demo.zip
lwa scan
lwa start <id>
lwa stop <id>
lwa restart <id>
lwa rebuild <id>
lwa logs <id>
lwa status [id]
lwa stats [id]
lwa doctor [id]
lwa remove <id>
lwa daemon on
lwa daemon off
lwa daemon status
lwa manager start
```

命令语义：

| 命令 | 作用 |
| --- | --- |
| `init` | 初始化目录结构、SQLite、默认配置、端口池 |
| `import` | 导入 zip，生成实例目录和初始元数据 |
| `scan` | 扫描 `inbox/` 和 pending 实例 |
| `start` | 启动容器实例或启用静态路由 |
| `stop` | 停止容器实例或禁用静态路由 |
| `restart` | 重启实例 |
| `rebuild` | 重新构建镜像或静态产物 |
| `logs` | 查看构建日志、容器日志或静态网关日志 |
| `status` | 查看实例状态 |
| `stats` | 查看资源占用 |
| `doctor` | 执行健康检查和常见问题诊断 |
| `remove` | 删除实例，默认保留或提示备份 `data/` |
| `daemon` | 控制是否自动监听导入目录 |
| `manager` | 启动管理页服务 |

## 11. Daemon 双模式

吸收讨论稿中的守护进程开关设计，V1 支持双模式：

```bash
lwa daemon on
```

表示自动监听 `inbox/`，发现 zip 后自动导入、识别、构建和启动可确定的实例。

```bash
lwa daemon off
```

表示不自动处理新 zip。用户可以手动运行 CLI，或者在项目根目录让大模型根据 skills 处理 pending 实例。

两种模式底层都调用同一套确定性代码。daemon 不做复杂推理，只做明确流程；遇到无法判断、构建失败、重型依赖或高风险配置时，标记为 `pending` 或 `failed`。

## 12. 项目识别策略

V1 先用确定性规则做初筛。

### 12.1 静态 HTML

识别依据：

1. 存在 `index.html`。
2. 不存在 `package.json`、`requirements.txt`、`pyproject.toml`、`manage.py`。
3. 文件主要是 HTML/CSS/JS/图片/字体。

处理方式：

1. 复制或链接到 `apps/<id>/public/`。
2. 生成静态网关配置。
3. 分配宿主机端口。
4. 健康检查 `index.html`。

### 12.2 纯前端项目

识别依据：

1. 存在 `package.json`。
2. 存在 `build` 脚本。
3. 依赖中出现 Vite、React、Vue、Svelte 等。
4. 未发现明确服务端入口。

处理方式：

1. 使用构建环境执行 `npm ci` 或 `npm install`。
2. 执行 `npm run build`。
3. 识别 `dist/`、`build/` 等产物目录。
4. 复制到 `public/`。
5. 进入共享静态托管。

如果构建失败，实例标记为 `build_failed`，交给 skill 诊断。

### 12.3 Node 后端

识别依据：

1. 存在 `package.json`。
2. 依赖中出现 Express、Fastify、Koa、Nest、Next、Nuxt 等。
3. scripts 中有 `start`、`dev`、`preview` 等。
4. 代码中出现端口监听逻辑。

处理方式：

1. 生成 Node Dockerfile。
2. 生成 Compose。
3. 默认内部端口优先取项目端口，否则按框架推断。
4. 宿主机端口由端口池分配。
5. 容器绑定 `0.0.0.0`。

### 12.4 Python Web 项目

识别依据：

1. 存在 `requirements.txt`、`pyproject.toml`、`uv.lock` 或 `Pipfile`。
2. 存在 `app.py`、`main.py`、`manage.py` 等。
3. 代码或依赖中出现 Flask、FastAPI、Django、Streamlit、Gradio。

处理方式：

1. 生成 Python Dockerfile。
2. 生成 Compose。
3. 按框架推断内部端口。
4. 对 SQLite 数据目录做挂载。

### 12.5 数据库识别

V1 优先支持 SQLite。

识别依据：

1. 项目中有 `.sqlite`、`.sqlite3`、`.db` 文件。
2. 依赖中出现 `sqlite3`、`better-sqlite3`、`sqlalchemy` 等。
3. 配置中出现 SQLite 连接串。

处理方式：

1. 数据统一迁移或初始化到 `apps/<id>/data/`。
2. Compose 挂载 `../data:/app/data`。
3. 环境变量中优先注入 `DATABASE_URL`。

Postgres/MySQL/Redis 在 V1 中只识别和标记，默认不自动启动。

## 13. Docker 与 Compose 模板

Node 后端基础模板：

```dockerfile
FROM node:22-alpine
WORKDIR /app
COPY current/package*.json ./
RUN npm ci || npm install
COPY current/ ./
ENV HOST=0.0.0.0
ENV PORT=3000
EXPOSE 3000
CMD ["npm", "run", "start"]
```

Python FastAPI 基础模板：

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY current/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY current/ ./
ENV HOST=0.0.0.0
ENV PORT=8000
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Compose 基础模板：

```yaml
services:
  app:
    build:
      context: ..
      dockerfile: docker/Dockerfile
    ports:
      - "${HOST_PORT}:${INTERNAL_PORT}"
    env_file:
      - .env
    volumes:
      - ../data:/app/data
    mem_limit: ${MEMORY_LIMIT:-512m}
    cpus: "${CPU_LIMIT:-0.75}"
    restart: unless-stopped
```

资源限制语法说明：

1. 这里使用 Compose 的 legacy 顶层字段 `mem_limit` / `cpus`。它们在单机 `docker compose`（非 Swarm）下会被直接应用，V1 以此作为默认写法，实现时需在目标环境实测限制确实生效。
2. Compose Spec 推荐的 `deploy.resources.limits.{memory,cpus}` 主要面向 Swarm 编排，单机模式下部分版本不生效，故 V1 不作为默认。
3. `local-web.json` 中的 `container.resourceLimits` 使用 `memory` / `cpus` 字段名，工具在渲染 Compose 时负责映射为 `mem_limit` / `cpus`；两处命名差异属于"元数据字段名"与"Compose 字段名"的映射，需在模板渲染层保持一致。

`.env` 示例：

```env
HOST_PORT=18024
INTERNAL_PORT=8000
MEMORY_LIMIT=512m
CPU_LIMIT=0.75
DATABASE_URL=sqlite:////app/data/app.sqlite
```

## 14. 共享静态托管

V1 支持 Caddy、Nginx 或内置 FastAPI StaticFiles 三种网关实现，但推荐优先选择 Caddy 或内置静态服务。

下例中的 `<WORKSPACE>` 表示工作区根目录（由 `local-web.yml` 配置解析得到，不写死为 `/opt/...`）。

静态站点配置示例：

```text
:18023 {
  root * <WORKSPACE>/apps/my-demo/public
  file_server
}
```

V1 默认每个静态站分配一个宿主机端口。这样可以避免路径路由下常见的资源路径问题，例如前端代码硬编码 `/assets/...`。

### 14.1 "共享"与"每实例独立端口"的实现约定

"共享静态托管"指所有静态站由同一套网关进程/服务托管，而不是每个静态站起一个常驻容器；它并不意味着共用一个端口。三种实现处理多端口的方式不同：

1. **Caddy（推荐）**：单进程可同时监听多个端口，每个静态站是一个独立的 site block（`:<hostPort>`），reload 即可增删站点。多端口天然支持。
2. **Nginx**：同理，单进程多 `server { listen <hostPort>; }` 块，reload 生效。
3. **内置 FastAPI StaticFiles（兜底）**：单个 ASGI 应用默认只监听一个端口。V1 若采用内置实现，采取"每个静态站一个轻量子服务/独立监听端口"的方式，由 `lwa` 统一管理这些监听的启停；因实现与资源成本更高，内置实现仅作为无 Caddy/Nginx 时的兜底，不作为默认推荐。

因此 V1 默认推荐 Caddy 作为静态网关，以最小成本满足"共享进程 + 每实例独立端口"。

未来如果启用名字路由，可切换为：

```text
my-demo.192.168.1.20.nip.io {
  root * <WORKSPACE>/apps/my-demo/public
  file_server
}
```

## 15. 管理页

管理页默认地址：

```text
http://<局域网 IP>:17800
```

首页直接展示实例列表，不做营销首页。

### 15.1 顶部统计

1. 实例总数。
2. 运行中、已停止、失败、pending 数量。
3. 静态、Node、Python、全栈类型分布。
4. 带数据库实例数量。
5. 端口池占用情况。
6. 整机 CPU、内存、磁盘概览。

### 15.2 实例表格

| 字段 | 说明 |
| --- | --- |
| 名称 | 实例名称 |
| 状态 | pending / building / running / stopped / failed |
| 期望状态 | desiredState |
| 运行形态 | static / frontend-static / backend-container / fullstack-sqlite（对应 §5 与 §8.0） |
| 技术族 | `kind`：static / node / python |
| 运行层 | shared-static / docker-compose |
| 技术栈 | React、Vite、FastAPI、Flask、SQLite 等 |
| 数据库 | 无 / SQLite / Postgres / MySQL / Redis / 未知 |
| 访问地址 | `lanUrl` |
| 宿主机端口 | `hostPort` |
| 内部端口 | `internalPort` |
| 版本 | zip 导入版本 |
| 导入时间 | createdAt |
| 最近更新 | updatedAt |
| 资源 | CPU、内存、磁盘 |
| 操作 | 打开、启动、停止、重启、日志、重建、删除 |

### 15.3 实例详情

详情页展示：

1. `local-web.json` 摘要。
2. Dockerfile 摘要。
3. Compose 摘要。
4. 静态网关配置摘要。
5. 最近健康检查结果。
6. 最近构建记录。
7. 最近日志。
8. 数据目录大小和备份提示。

### 15.4 管理页参考原则

管理页需要吸收成熟项目的优点，但保持本工具的窄边界。

1. 参考 1Panel 的服务器面板体验：顶部展示整机资源、实例状态、磁盘占用和常用操作入口，让用户一眼判断当前机器还能不能再运行新实例。
2. 参考 Coolify/Dokploy 的部署体验：每个实例详情页保留构建记录、运行日志、环境变量、访问入口和健康检查结果，让“导入 -> 构建 -> 启动 -> 验证”的过程可追踪。
3. 参考 Dockge 的 Compose stack 管理：对 Docker Compose 实例展示 stack 名称、compose 文件路径、服务列表、容器状态和 compose logs，并提供重新构建、启动、停止、重启操作。
4. 参考 Runtipi/CasaOS 的家庭服务器体验：实例列表应像一个轻量应用目录，展示名称、图标或首字母标识、类型标签、资源档位、开关状态和打开入口，适合非专业运维用户在小主机上日常使用。
5. 不把管理页扩展成完整服务器控制面板：V1 不管理系统软件源、防火墙、数据库实例市场、计划任务、SSL 证书全流程或多站点传统虚拟主机。

## 16. 资源策略

V1 面向 4G/8G 小主机，默认保守。

### 16.1 资源档位

| 档位 | 适用对象 | 默认行为 |
| --- | --- | --- |
| `tiny` | 纯静态、构建后 SPA | 自动托管 |
| `small` | 小型 Node/Python 后端 | 自动构建和启动，加资源限制 |
| `medium` | Next.js、Streamlit、Gradio、较重 Python 服务 | 可导入，启动前提示 |
| `heavy` | 多服务数据库、大模型服务、复杂后台 | 只导入，不自动启动 |

### 16.2 构建并发

V1 默认同一时间只允许一个构建任务。

原因：

1. npm/pip 构建会产生瞬时内存尖峰。
2. 4G 机器容易在并发构建时 OOM。
3. 构建队列比并发失败更容易理解和恢复。

### 16.3 内存与 CPU 限制

小型容器默认：

```yaml
mem_limit: 512m
cpus: "0.75"
```

较重实例可提高到：

```yaml
mem_limit: 1024m
cpus: "1.5"
```

### 16.4 监控方式

V1 不引入 Prometheus/cAdvisor。

资源数据来源：

1. 容器资源：`docker stats --no-stream`。
2. 整机内存：`/proc/meminfo`。
3. 整机负载：`/proc/loadavg`。
4. 磁盘占用：实例目录统计和 Docker 镜像大小。

跨平台降级：`docker stats` 与目录/镜像大小在 Linux、Windows、macOS 上一致可用；`/proc/meminfo` 和 `/proc/loadavg` 仅在 Linux 可用。在非 Linux 开发环境（Windows/macOS）中，整机内存与负载指标可跳过或标记为"不可用"，不影响容器资源、目录大小和实例运行。资源采集实现应对宿主机指标做能力探测，探测失败时降级而非报错。

管理页采用按需拉取和 10-30 秒低频轮询。

### 16.5 小主机建议

4G 机器：

1. 建议开启 2-4G swap。
2. 默认构建并发为 1。
3. 不自动启动 `medium` 和 `heavy` 实例。
4. 优先使用共享静态托管和 SQLite。
5. 鼓励停止闲置实例。

8G 机器：

1. 可以同时运行多个小型 Node/Python 容器。
2. 构建并发仍默认 1，可配置为 2。
3. 可运行少量数据库容器，但需明确登记资源和数据目录。

### 16.6 日志与磁盘治理

日志和构建产物会随时间累积，在小主机上可能悄悄吃满磁盘，V1 需要基础的常驻治理（备份/恢复仍留到 V1.2）：

1. 实例构建日志、运行日志集中在 `apps/<id>/logs/` 和全局 `logs/`，按大小或条数做滚动，单实例日志保留上限可配置（如单文件 10MB、保留最近 N 个）。
2. `docker compose logs` 属于 Docker 自身日志，V1 通过按需拉取展示，不长期复制；如需限制，建议在文档中提示配置 Docker 日志驱动的 `max-size` / `max-file`。
3. `builds`、`events`、`resources` 表会持续增长，V1 至少提供按实例/按时间清理旧记录的入口（可延后到 V1.1 自动化，V1 先保证不无界写入）。
4. 磁盘接近阈值时，管理页和 `lwa doctor` 应给出提示。

## 17. 安全边界

V1 的安全假设是：zip 主要来自用户自己的 AI coding 产物，来源基本可信。

仍需明确以下风险：

1. `docker build` 会执行 Dockerfile 中的命令。
2. `npm install` 可能执行 postinstall。
3. `pip install` 可能执行构建脚本。
4. 容器挂载宿主机目录会扩大风险面。
5. 管理页暴露公网会有明显风险。

V1 默认安全策略：

1. 管理页只面向局域网。
2. 管理页设置 token 或密码。
3. 实例容器只挂载自己的 `data/`。
4. 不挂载 Docker socket 到实例容器。
5. 不使用 privileged 容器。
6. `heavy` 和多服务数据库实例不自动启动。
7. 构建和启动前记录计划与日志。

## 18. 大模型 Skills

V1 的 skills 不承担运行时职责，只承担判断、生成和修复职责。

推荐 skills：

```text
skills/
  detect-stack/
  detect-internal-port/
  build-frontend-static/
  dockerize-node-app/
  dockerize-python-app/
  dockerize-fullstack-sqlite/
  generate-static-gateway-config/
  generate-compose/
  fix-docker-build-failure/
  fix-container-startup-failure/
  fix-port-binding/
  diagnose-health-check/
```

skill 的输入：

1. 项目目录结构。
2. 初始 `local-web.json`。
3. 构建日志。
4. 启动日志。
5. 健康检查结果。

skill 的输出：

1. 修改后的 `local-web.json`。
2. Dockerfile。
3. Compose。
4. 静态网关配置。
5. 诊断说明。

最终执行仍由 `lwa` 完成。

## 19. V1 MVP 范围

V1 必须完成：

1. `lwa init` 初始化目录结构和 SQLite。
2. `lwa import` 导入 zip。
3. 识别纯静态 HTML。
4. 识别并构建纯前端 SPA。
5. 识别小型 Node 后端。
6. 识别小型 Python 后端。
7. 支持 SQLite 数据目录挂载。
8. 生成 `local-web.json`。
9. 写入 SQLite registry。
10. 分配端口池访问入口。
11. 生成静态网关配置。
12. 生成 Dockerfile 和 Compose。
13. 支持 build/start/stop/restart/logs/status。
14. 支持 daemon on/off。
15. 支持管理页实例列表。
16. 支持管理页打开实例、启动、停止、重启、查看日志。
17. 支持基础资源监控。
18. 支持 pending/failed 状态和错误摘要。

V1 可以暂缓：

1. Traefik/nip.io 名字路由。
2. Postgres/MySQL/Redis 自动托管。
3. 备份恢复。
4. 多版本回滚。
5. 多用户权限。
6. 跨机器迁移包。
7. 自动清理 Docker 镜像。

## 20. 实施顺序

1. 建立目录结构和配置文件。
2. 实现 SQLite registry。
3. 实现 `local-web.json` schema。
4. 实现端口池。
5. 实现 zip 导入和实例命名。
6. 实现静态 HTML 托管。
7. 实现纯前端构建后托管。
8. 实现 Node/Python Dockerfile 模板。
9. 实现 Compose 模板和 `.env`。
10. 实现 Docker build/up/stop/start/logs 封装。
11. 实现 daemon watcher。
12. 实现管理页后端。
13. 实现管理页前端表格和操作。
14. 实现资源统计接口。
15. 实现失败诊断入口。
16. 补充 skills 文档。
17. 使用四个样例 zip 做端到端验证。

## 21. V1 验证样例

V1 至少需要准备四个样例包：

1. 纯静态 HTML。
2. Vite/React 纯前端项目。
3. Node/Express 后端项目（无数据库）。
4. FastAPI 或 Flask + SQLite 项目。

前三个覆盖三条主要托管路径（共享静态、构建后静态、后端容器），第四个额外覆盖 SQLite 数据目录挂载。Node 后端样例用于验证 `backend-container` 路径与 Node Dockerfile/Compose 模板，避免 Node 路径只在单元测试层被覆盖。

每个样例都需要验证：

1. zip 导入成功。
2. 实例目录结构正确。
3. `local-web.json` 内容正确。
4. SQLite registry 写入正确。
5. 访问地址可打开。
6. 健康检查通过。
7. 管理页展示正确。
8. start/stop/restart 行为正确。
9. logs 可查看。
10. 资源数据可显示。

## 22. V1 后续演进

V1.1：

1. Traefik 或 Caddy 按名字路由。
2. nip.io / sslip.io 通配域名支持。
3. 一键切换端口路由和名字路由。
4. 更完整的 Dockerfile 修复 skill。
5. 静态网关配置自动 reload 和回滚。

V1.2：

1. Postgres/MySQL/Redis 多服务 Compose。
2. 数据目录备份和恢复。
3. 实例版本升级和回滚。
4. 镜像和构建缓存清理。
5. 闲置实例自动停止。

V2：

1. 多机器部署。
2. 实例迁移包。
3. 更强的安全隔离。
4. 多用户权限和审计。
5. 更完整的资源治理。

## 23. 竞品与参考项目

Local Webpage Access 和宝塔面板、1Panel、Coolify、Dockge、Runtipi 等项目处在相邻赛道，但产品入口和目标边界不同。

我们的需求更像是 1Panel、Coolify、Dokploy、Dockge、Runtipi、CasaOS 的交叉点，但多了一个很特殊的入口：

**把 AI 生成的小网页 zip 丢进本地实例库，由工具自动识别、生成运行配置并在局域网托管。**

### 23.1 赛道判断

| 项目 | 调研时 star 约数 | 核心定位 | 和 Local Webpage Access 的关系 |
| --- | ---: | --- | --- |
| [Coolify](https://github.com/coollabsio/coolify) | 57.8k | 自托管 PaaS，部署应用、静态站、数据库和服务 | 很接近，可参考应用部署生命周期、构建日志、环境变量、反向代理和健康检查 |
| [Portainer](https://github.com/portainer/portainer) | 37.9k | Docker/Kubernetes 管理面板 | 可参考容器状态、日志、资源和操作入口，但 V1 不做通用 Docker 管理面板 |
| [1Panel](https://github.com/1Panel-dev/1Panel) | 36.1k | 现代服务器运维面板，覆盖网站、Docker、应用和主机管理 | 可参考服务器管理页体验，但不扩展到完整 VPS 面板 |
| [CasaOS](https://github.com/IceWhaleTech/CasaOS) | 36.2k | 家庭服务器系统和应用体验 | 可参考轻量应用目录、小主机友好界面和家庭服务器使用心智 |
| [Dokploy](https://github.com/Dokploy/dokploy) | 35.3k | 自托管 PaaS，类似轻量 Vercel/Netlify/Heroku | 可参考部署流水线、路由、日志、应用状态和服务编排 |
| [Dokku](https://github.com/dokku/dokku) | 32.0k | Docker-powered mini-Heroku，偏 git push 部署 | 可参考小型 PaaS 边界，但入口不同，Local Webpage Access 不是 git push-first |
| [Nginx Proxy Manager](https://github.com/NginxProxyManager/nginx-proxy-manager) | 33.5k | 反向代理、域名和证书管理 | 可参考代理配置 UI，但 V1 只做本地访问入口，不做完整证书平台 |
| [Dockge](https://github.com/louislam/dockge) | 23.7k | Docker Compose stack 管理 | 和 Compose 层高度相关，可参考 stack 生命周期、compose 文件管理和日志体验 |
| [CapRover](https://github.com/caprover/caprover) | 15.1k | Docker + nginx 的自托管 PaaS | 可参考一键部署和应用路由，但 V1 不做多租户 PaaS |
| [Umbrel](https://github.com/getumbrel/umbrel) | 11.6k | 个人服务器 OS 和应用生态 | 可参考个人服务的安装体验和应用卡片 |
| [Komodo](https://github.com/moghtech/komodo) | 11.5k | 多服务器容器、Compose stack 和部署管理 | 可参考多实例状态和部署历史，V1 暂不做多服务器 |
| [Runtipi](https://github.com/runtipi/runtipi) | 9.5k | 家庭服务器应用商店，Docker app 一键安装 | 可参考应用目录、安装/卸载体验和小主机使用路径 |
| [YunoHost](https://github.com/YunoHost/yunohost) | 2.9k | 自托管应用系统，偏传统应用打包安装 | 可参考自托管应用生命周期，但技术路线不同 |
| [aaPanel/BaoTa](https://github.com/aaPanel/BaoTa) | 4.5k | 宝塔 Linux 面板，传统服务器运维面板 | 管理页思路相似，但不是 zip 导入、AI 识别、Docker Compose-first 的产品 |

star 数只作为调研时的热度参考，不作为功能优先级依据。V1 的功能优先级以本地 zip 实例库闭环为准。

### 23.2 需要吸收的核心优势

#### 1Panel / 宝塔 / aaPanel

可借鉴：

1. 主机概览、资源占用、磁盘占用、服务状态集中展示。
2. 网站/应用/容器/日志的统一管理入口。
3. 面向非专业运维用户的低门槛操作体验。

不照搬：

1. 不做完整服务器运维面板。
2. 不管理 LNMP/LAMP、系统防火墙、软件商店、计划任务、传统数据库面板。
3. 不把 Local Webpage Access 扩展成通用 VPS 管理工具。

落到本设计：

1. 管理页必须有整机资源总览和实例状态总览。
2. 每个实例必须能打开、启动、停止、重启、查看日志。
3. 所有危险操作都要有明确状态和错误摘要。

#### Coolify / Dokploy / CapRover / Dokku

可借鉴：

1. 应用部署生命周期：构建、启动、健康检查、路由、回滚或重建。
2. 环境变量、构建日志、运行日志、域名/访问入口集中管理。
3. 静态站、后端应用、数据库服务使用不同部署策略。

不照搬：

1. V1 不做 GitHub/GitLab 代码仓库持续部署。
2. V1 不做团队协作、多租户、远程集群或公网 PaaS。
3. V1 不默认引入复杂数据库和证书全自动化。

落到本设计：

1. `lwa import` 之后要形成可追踪的部署记录。
2. 构建日志、健康检查和访问入口必须进入实例详情页。
3. Dockerfile、Compose 和静态托管配置都应保存在实例目录中，方便排障和二次修改。

#### Dockge / Portainer / Komodo

可借鉴：

1. Compose stack 是一个清晰的管理单元。
2. stack 的状态、服务列表、compose 文件、日志和操作应该放在同一个视图。
3. 容器资源数据可以直接来自 Docker，无需额外引入重型监控系统。

不照搬：

1. V1 不做通用 Docker 主机管理器。
2. V1 不允许用户在界面里任意管理非 Local Webpage Access 创建的容器。
3. V1 不把 Docker Compose 编辑器作为第一优先级，先保证自动生成和可读。

落到本设计：

1. 每个 Docker 实例必须记录 `composePath`、`projectName`、`serviceName`。
2. 管理页需要展示 Compose stack 摘要和 `docker compose logs`。
3. `lwa` 的 start/stop/restart/logs/rebuild 都应围绕实例对应的 Compose project 执行。

#### Runtipi / CasaOS / Umbrel

可借鉴：

1. 家庭服务器和小主机场景下，用户更关心“装了什么、开着什么、占多少资源、点哪里打开”。
2. 应用目录式体验比传统服务器术语更适合日常使用。
3. 应用卡片、标签、状态、开关和一键打开能显著降低使用门槛。

不照搬：

1. V1 不做固定应用商店。
2. V1 的入口不是安装预置应用，而是导入用户自己的 zip。
3. V1 不把家庭服务器 OS、文件管理、媒体服务生态作为目标。

落到本设计：

1. 管理页实例列表要兼顾表格密度和应用目录可读性。
2. 每个实例要有名称、类型、技术栈、资源档位和运行状态。
3. 后续可以增加“模板/配方目录”，但 V1 首先服务 zip 导入。

#### Nginx Proxy Manager

可借鉴：

1. 访问入口、反向代理、路由和端口映射需要可视化。
2. 路由配置应能生成、查看、重载并在失败时回滚。

不照搬：

1. V1 不做完整公网域名和证书管理。
2. V1 默认仍以局域网端口池直连为主。

落到本设计：

1. V1 保留 `routeMode`、`routeHost`、`lanUrl` 字段。
2. 后续可启用 Traefik/Caddy + nip.io/sslip.io 按名字路由。
3. 静态网关配置需要有生成路径、启用状态和 reload 日志。

### 23.3 我们的差异化边界

Local Webpage Access 不应该被设计成“另一个 1Panel/宝塔”，也不应该一开始就复制 Coolify 的完整 PaaS 能力。

核心差异化是：

1. **zip-first 输入。**  
   用户把 AI coding 产出的 zip 放进实例库，而不是先接 Git 仓库、应用市场或手写 Compose。

2. **AI 辅助识别部署。**  
   大模型 skill 读取陌生项目结构，判断技术栈、入口、端口、数据库和运行形态，并在失败时修复 Dockerfile/Compose。

3. **静态轻量托管。**  
   纯静态和纯前端构建产物进入共享静态托管，避免每个小页面一个常驻容器。

4. **后端项目 Compose 化。**  
   Node/Python 后端和 SQLite 全栈应用标准化成 Docker Compose project，便于启停、日志、迁移和重建。

5. **小主机资源克制。**  
   默认构建并发为 1，限制容器资源，轻量采集监控，不引入重型组件。

6. **本地管理页围绕实例库组织。**  
   管理页首先回答：导入了哪些 zip，跑起来哪些，失败哪些，访问地址是什么，占了多少资源。

### 23.4 对 V1 的设计约束

根据以上参考项目，V1 需要坚持以下约束：

1. 管理页首页优先展示实例，而不是系统运维菜单。
2. 每个实例必须有明确生命周期：`pending`、`building`、`running`、`stopped`、`failed`。
3. 每个实例必须保留构建记录、运行日志和健康检查结果。
4. Compose 实例必须被当作 stack 管理，而不是孤立容器。
5. 静态实例必须保持轻量，不因为统一性强行容器化。
6. 资源监控必须轻量，优先用 Docker CLI 和系统 `/proc` 信息。
7. 路由能力要可演进：V1 端口池直连，后续升级到名字路由。
8. 所有复杂识别和修复都应通过 skill 增强，而不是把 daemon 写成复杂规则引擎。

最终定位可以写成：

**参考 1Panel 的服务器管理体验，参考 Coolify/Dokploy 的自托管应用部署思路，参考 Dockge 的 Compose stack 管理，参考 Runtipi/CasaOS 的家庭服务器应用目录体验，但核心入口是“本地 zip 实例库 + AI 辅助识别部署”。**

## 24. 结论

V1 不追求“大而全”，而是先把本地网页部署基座的核心闭环做扎实：

**zip 导入 -> 项目识别 -> 运行形态选择 -> 静态托管或 Docker Compose -> 统一登记 -> 管理页展示 -> 实例启停和日志 -> 资源可见。**

相较三份讨论稿，V1 的最终取舍是：

1. 保留“设计意见”中的 Docker 优先、共享静态托管、SQLite registry、端口池和管理页完整设计。
2. 吸收“方案”中的混合驱动、manifest 合同、daemon 双模式、实例开关、资源监控思想。
3. 吸收“proposal”中的 `desiredState`、`unless-stopped`、`stop` 不等于 `down`、轻量监控和小主机资源纪律。
4. 对 Traefik/nip.io 按名字路由采取“预留字段、后续启用”的策略，避免 V1 被额外网络依赖阻塞。
5. 吸收 1Panel、Coolify、Dokploy、Dockge、Runtipi、CasaOS 等项目的成熟体验，但不复制它们的大而全边界。

这样 V1 的实现路径清晰、依赖克制、适合 4G/8G 小主机，也方便后续逐步增强到名字路由、多服务数据库、备份恢复和跨机器迁移。
