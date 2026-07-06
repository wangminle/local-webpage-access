# Local Webpage Access 工具设计意见：Docker 优先版 - 20260703

## 1. 背景

当前要解决的问题是：在一台局域网可访问的类 Linux 机器上，集中运行和管理由 Web coding 生成的小网页、小工具和小型 Web 应用。

这些项目的形态并不统一：

1. 纯静态 HTML，直接打开或用静态服务器即可访问。
2. Node.js 前端项目，例如 Vite、React、Vue、Next.js 等，需要安装 npm 依赖后运行。
3. Python Web 项目，例如 Flask、FastAPI、Django、Streamlit、Gradio 等，需要创建运行环境、安装依赖并启动服务。
4. 带本地数据库、文件存储或后端逻辑的全栈项目，可能使用 Node.js、Python、SQLite、Postgres、Redis 等。

用户希望有一个固定目录，例如 `local-webpage-access/`，每次把 zip 包放入实例库目录后，工具可以完成识别、解压、配置、运行，并在本地管理页中展示所有已安装实例。

经过进一步判断，最合理的底层运行方式应调整为：

**Docker / Docker Compose 优先。**

也就是说，每个导入的 zip 最终都应尽量被标准化成一个可管理的运行单元。需要长期后端进程或数据库依赖的实例，优先转成 Docker Compose 项目；纯静态 HTML 和可构建成静态产物的前端项目，优先进入共享静态托管池，不必每个页面都常驻一个容器。

## 2. 核心结论

推荐架构是：

**确定性工具负责导入、识别、Docker 化、端口分配、启动、停止、登记和管理页展示；大模型 skill 负责复杂项目的 Docker 化补全、启动失败诊断和配置修复。**

更具体地说：

1. `lwa` 是一个本地管理工具。
2. Docker / Docker Compose 是默认运行层，但不是每个 zip 都必须变成一个长期运行容器。
3. 纯静态 HTML 和纯前端 build 产物优先由共享静态服务托管。
4. Node/Python 后端、Next.js 服务端应用、带数据库应用，才默认生成独立 Docker Compose project。
5. 管理工具统一生成或维护 `Dockerfile`、`compose.yaml`、`.env`、`local-web.json`，也可以为静态托管器生成路由或站点配置。
6. 所有实例端口都通过宿主机端口池或共享静态托管入口映射出来，例如 `18000-19999`。
7. SQLite registry 记录所有实例的元数据。
8. 本地管理页读取 registry、静态托管状态和 Docker 状态，展示实例列表、运行状态、技术栈、端口、版本、日志等。
9. 大模型 skill 不是常驻运行时，而是导入和故障处理阶段的“部署工程师”。

一句话概括：

**这不是一个直接运行 npm/Python 项目的脚本集合，而是一个把 zip 包转成局域网可访问、可登记、可管理运行单元的本地小型 PaaS；Docker 优先，但静态资源要轻量托管。**

## 3. 为什么 Docker 优先更合理

### 3.1 依赖隔离

如果直接在宿主机运行，每个项目可能要求不同版本的 Node.js、Python、系统库、npm 包、pip 包。随着实例增多，宿主机会逐渐变成难以维护的混合运行环境。

Docker 的优势是：

1. 每个实例的依赖封装在自己的镜像里。
2. Node.js 版本、Python 版本、系统依赖互不干扰。
3. 删除实例时，可以连同容器、镜像、匿名卷一起清理。
4. 宿主机只需要稳定维护 Docker 和 `lwa` 自身。

这对“经常产生很多小网页”的场景尤其重要。项目数量一多，依赖隔离比单个项目的启动便利更重要。

### 3.2 启动方式统一

如果不使用 Docker，不同项目的启动方式会分裂成：

```bash
python -m http.server
npm run dev
npm run preview
uvicorn main:app
flask run
python manage.py runserver
streamlit run app.py
```

使用 Docker Compose 后，管理工具面对所有实例都可以统一成：

```bash
docker compose up -d
docker compose down
docker compose restart
docker compose logs
```

底层项目内部仍然可以不同，但工具的控制面统一了。这个统一性非常关键，因为管理页、日志、状态检查、重启、删除、备份都可以基于同一套命令实现。

### 3.3 端口映射天然适配

很多 Web 项目内部默认端口是 `3000`、`5173`、`8000`、`8501`。如果直接在宿主机运行，就会频繁冲突。

Docker 更适合处理这个问题：

```yaml
ports:
  - "18023:3000"
```

容器内部可以继续使用项目熟悉的端口，宿主机统一暴露到端口池。

这意味着：

1. 不需要强行修改所有项目内部默认端口。
2. 宿主机端口分配由 `lwa` 统一管理。
3. 管理页只展示宿主机访问地址，例如 `http://192.168.1.20:18023`。
4. 容器内部端口和宿主机端口可以分开记录。

### 3.4 数据库和持久化更清晰

带数据库的项目如果直接跑在宿主机上，数据文件、数据库服务、迁移脚本、环境变量会分散在不同位置。

Docker Compose 可以把它们显式写清楚：

```yaml
services:
  app:
    build: .
    volumes:
      - ./data:/app/data
    environment:
      - DATABASE_URL=sqlite:////app/data/app.sqlite

  redis:
    image: redis:7
    restart: unless-stopped
```

对于 SQLite：

1. 数据可以统一放在 `apps/<id>/data/`。
2. 升级代码时不覆盖数据。
3. 备份时只需要备份实例目录下的 `data/`。

对于 Postgres、MySQL、Redis：

1. 可以在 `compose.yaml` 中作为附属服务声明。
2. 可以通过命名 volume 或实例本地目录保存数据。
3. 管理页能明确标记“这是多服务实例”。

### 3.5 迁移和复现更容易

直接在宿主机运行时，迁移到另一台机器需要重新安装 Node、Python、系统依赖、数据库等。

Docker 化之后，迁移路径更简单：

```text
复制 apps/<id>/
复制 registry 或重新扫描
docker compose up -d
```

这符合这个工具的定位：局域网机器上的小应用陈列架。以后换机器、重装系统、从 WSL 迁移到 Ubuntu 主机，都会更可控。

### 3.6 和 systemd 的关系更清楚

如果每个实例都直接生成 systemd service，后续会有很多服务文件、环境变量和启动命令散落在用户级 systemd 里。

Docker 优先后，systemd 只需要负责两类东西：

1. Docker 自身。
2. `lwa` 管理器和管理页。

实例的保活交给 Docker Compose：

```yaml
restart: unless-stopped
```

这样职责更清楚：

```text
systemd / launchd
  -> 启动 Docker 和 lwa 管理器

Docker Compose
  -> 启动、停止、重启需要后端进程或数据库依赖的网页实例

lwa
  -> 生成配置、登记实例、管理端口、展示状态、维护共享静态托管
```

### 3.7 小主机上的资源判断

Docker 本身在原生 Linux 上并不算重。真正消耗资源的是应用进程、数据库、构建过程，以及 Docker Desktop 这类带虚拟机的运行环境。

对 4G 或 8G 小主机，可以这样判断：

| 环境 | 判断 |
| --- | --- |
| 原生 Ubuntu / Debian / Linux 小主机 | 比较适合，Docker 引擎开销可控 |
| Windows WSL2 + Docker | 可用，但会有 WSL2 和虚拟化额外占用 |
| macOS Docker Desktop | 明显更重，因为 Docker 运行在 Linux VM 中 |
| NAS / 小主机原生 Linux | 通常适合，但要控制数据库和构建并发 |

因此，4G 小主机可以跑 Docker，但架构必须克制；8G 会宽裕很多。对当前工具来说，最重要的不是“能不能用 Docker”，而是“不要把所有 zip 都变成长期运行容器”。

不同实例的大致资源压力如下：

| 实例类型 | 常驻资源压力 | 推荐处理 |
| --- | --- | --- |
| 纯静态 HTML | 很低 | 共享静态托管 |
| React / Vite / Vue 纯前端 | 构建时中等，运行时很低 | 构建后共享静态托管 |
| 小型 Node 后端 | 中等 | 独立 Compose，限制资源 |
| 小型 Python 后端 | 中等 | 独立 Compose，限制资源 |
| Next.js dev 模式 | 偏重 | 尽量生产构建，避免大量常驻 dev server |
| Postgres / MySQL | 偏重 | 默认需要确认，不建议每个应用一套 |
| Redis | 中等偏低，但仍是常驻服务 | 需要时再启用 |
| Docker build | 临时占用高 | 限制并发，避免同时构建多个项目 |

所以推荐原则是：

1. Docker 优先，但不是容器数量优先。
2. 静态 HTML 和纯前端 build 产物进入共享静态托管池。
3. 只有需要后端进程、服务端渲染、API、任务队列或数据库服务的项目，才常驻独立容器。
4. SQLite 优先，Postgres/MySQL/Redis 作为高级模式。
5. 默认限制构建并发，4G 机器建议一次只构建一个实例。
6. 管理页应展示资源占用和磁盘占用，避免小主机被未知实例拖垮。

## 4. Docker 不是全部答案

虽然 Docker 应该作为默认运行层，但它不能替代 `lwa` 工具本身。

Docker 不会自动解决这些问题：

1. zip 包如何命名、解压、版本化。
2. 如何判断项目是静态 HTML、Node、Python 还是全栈。
3. 如何选择基础镜像。
4. 如何判断容器内部端口。
5. 如何分配宿主机端口。
6. 如何判断是否带数据库。
7. 如何挂载数据目录。
8. 如何生成管理页里的名称、技术栈、版本、导入时间。
9. 如何处理硬编码 localhost、硬编码端口、缺少启动脚本等问题。
10. 如何把所有实例集中展示和管理。
11. 如何决定静态项目是否进入共享托管，而不是独立容器。
12. 如何限制构建并发、容器内存、磁盘占用和数据库数量。

所以正确分工应该是：

```text
Docker / Compose
  负责需要容器化实例的运行时隔离、启动、网络、卷、日志基础能力

lwa
  负责导入、识别、生成配置、端口分配、registry、共享静态托管、管理页、资源策略

大模型 skills
  负责复杂识别、Dockerfile 修复、compose 修复、日志诊断
```

## 5. Docker 优先后的总体流程

推荐导入流程如下：

```text
用户放入 zip
  -> lwa import inbox/demo.zip
  -> 解压到 apps/<id>/current/
  -> 确定性扫描项目结构
  -> 判断技术栈、数据库、入口命令、内部端口、资源级别
  -> 生成 local-web.json
  -> 根据类型选择运行形态
      -> 纯静态 HTML：登记到共享静态托管器
      -> 纯前端 SPA：临时容器或本机工具链构建，产物登记到共享静态托管器
      -> Node/Python 后端：生成 Dockerfile + compose.yaml
      -> 带数据库全栈：生成 Dockerfile + compose.yaml + data 挂载
  -> 分配宿主机端口或共享静态访问入口
  -> 对 Compose 实例执行 docker compose build / up -d
  -> 对静态实例更新共享静态托管配置并 reload
  -> 健康检查或静态文件可访问性检查
  -> 写入 SQLite registry
  -> 管理页展示实例
```

对于不确定项目：

```text
导入 zip
  -> 扫描器无法确定启动方式
  -> 标记为 pending
  -> 大模型 skill 读取项目结构和日志
  -> 补全运行形态、Dockerfile / compose.yaml / 静态托管配置 / local-web.json
  -> 再由 lwa 执行托管配置更新或 Docker build/up
```

这里要注意：大模型负责“生成和修复配置”，最终执行仍然由确定性工具完成。

## 6. 推荐目录结构

建议项目根目录：

```text
local-webpage-access/
  inbox/                  # zip 包入口目录
  apps/                   # 已安装实例
  static-sites/           # 共享静态托管根目录或构建产物目录
  registry/               # 元数据与索引
    local-web.db          # SQLite 注册表
  logs/                   # 全局日志
  manager/                # 管理页后端与前端
  static-gateway/         # 共享静态服务配置，例如 Caddy/Nginx/内置静态服务
  skills/                 # 大模型流程说明
  templates/              # Dockerfile / compose / 静态托管模板
    static/
    node/
    python/
    fullstack/
  local-web.yml           # 全局配置
```

每个实例目录：

```text
apps/
  my-demo/
    source/
      original.zip        # 原始 zip 包备份
    current/              # 解压后的项目代码
    public/               # 静态托管产物，可由 current/ 直接复制或构建生成
    data/                 # 持久化数据
    logs/                 # 实例日志或导出的日志
    docker/
      Dockerfile          # 工具生成或修复后的 Dockerfile
      compose.yaml        # 工具生成或修复后的 Compose 文件
      .env                # 端口、路径、运行环境变量
    local-web.json        # 实例元数据
```

是否把 `Dockerfile` 和 `compose.yaml` 放在实例根目录或 `docker/` 下都可以。更推荐放在 `docker/` 下，以便区分原始项目代码和工具生成物。

对于纯静态实例，`docker/` 目录可以不存在，或者只保存曾经用于构建的临时 Dockerfile。长期运行态应记录为 `shared-static`，而不是 `docker-compose`。

## 7. 实例元数据设计

`local-web.json` 是单个实例的身份证，也是工具、管理页和大模型 skill 共享上下文的核心文件。

示例：

```json
{
  "id": "my-demo",
  "name": "My Demo",
  "version": "2026.07.03-1",
  "kind": "node",
  "stack": ["vite", "react"],
  "runtime": "docker-compose",
  "servingMode": "container",
  "resourceProfile": "small",
  "hasDatabase": false,
  "database": null,
  "static": null,
  "container": {
    "projectName": "lwa-my-demo",
    "serviceName": "app",
    "image": "lwa/my-demo:2026.07.03-1",
    "internalPort": 3000,
    "composePath": "docker/compose.yaml",
    "dockerfilePath": "docker/Dockerfile",
    "resourceLimits": {
      "memory": "512m",
      "cpus": "0.75"
    }
  },
  "network": {
    "host": "0.0.0.0",
    "hostPort": 18023,
    "lanUrl": "http://192.168.1.20:18023",
    "healthUrl": "http://127.0.0.1:18023"
  },
  "entry": {
    "install": "npm install",
    "start": "npm run dev -- --host 0.0.0.0"
  },
  "createdAt": "2026-07-03T10:00:00+08:00",
  "updatedAt": "2026-07-03T10:00:00+08:00",
  "status": "running"
}
```

这里要区分两个端口：

1. `internalPort`：容器内部端口，例如 `3000`。
2. `hostPort`：宿主机暴露端口，例如 `18023`。

管理页面向用户展示 `hostPort` 和 `lanUrl`。

如果是共享静态托管实例，元数据可以变成：

```json
{
  "id": "static-demo",
  "name": "Static Demo",
  "version": "2026.07.03-1",
  "kind": "static",
  "stack": ["html", "css", "javascript"],
  "runtime": "shared-static",
  "servingMode": "shared-static",
  "resourceProfile": "tiny",
  "hasDatabase": false,
  "database": null,
  "static": {
    "root": "public",
    "gateway": "caddy",
    "routeMode": "port",
    "gatewayConfigPath": "static-gateway/sites/static-demo.conf"
  },
  "container": null,
  "network": {
    "host": "0.0.0.0",
    "hostPort": 18024,
    "lanUrl": "http://192.168.1.20:18024",
    "healthUrl": "http://127.0.0.1:18024"
  },
  "createdAt": "2026-07-03T10:00:00+08:00",
  "updatedAt": "2026-07-03T10:00:00+08:00",
  "status": "running"
}
```

`resourceProfile` 建议先用几个简单档位：

| 档位 | 适用对象 |
| --- | --- |
| `tiny` | 共享静态页面、纯前端静态产物 |
| `small` | 小型 Node/Python 后端 |
| `medium` | Next.js、Streamlit、Gradio、较重 Python 服务 |
| `heavy` | 带 Postgres/MySQL/Redis 或构建成本高的项目 |

## 8. 不同项目的 Docker 化策略

### 8.1 纯静态 HTML

识别依据：

1. 存在 `index.html`。
2. 不存在 `package.json`、`requirements.txt`、`pyproject.toml`、`manage.py`。
3. 资源主要是 HTML、CSS、JS、图片、字体。

推荐运行方式：

1. 优先使用共享静态托管器，例如 Caddy、Nginx 或 `lwa` 管理器内置静态服务。
2. 每个静态站点可以分配一个宿主机端口，也可以使用路径路由。
3. 第一版更建议“每站一个宿主机端口、共享一个静态服务进程”，避免很多前端项目因为绝对路径 `/assets/...` 在路径路由下失效。
4. 不建议每个纯静态 HTML 都生成一个长期运行容器；在 4G 小主机上，这种做法会浪费进程、端口配置和镜像空间。

共享静态托管的概念示例：

```text
static-gateway/
  sites/
    static-demo.conf

apps/static-demo/
  public/
    index.html
    assets/
```

如果选择 Caddy，可以生成类似配置：

```text
:18024 {
  root * /opt/local-webpage-access/apps/static-demo/public
  file_server
}
```

这种方式只需要一个共享静态服务进程，新增静态站点时 reload 配置即可。

### 8.2 Node.js 前端项目

识别依据：

1. 存在 `package.json`。
2. scripts 中存在 `dev`、`start`、`preview`、`build`。
3. 依赖中出现 Vite、React、Vue、Next.js、Nuxt 等。

这里要区分两类：

1. 纯前端 SPA：推荐 build 后交给共享静态托管器。
2. 有服务端能力的框架：例如 Next.js，需要 Node 服务运行。

纯前端 SPA 推荐使用“构建阶段容器化，运行阶段共享静态托管”的模式。构建阶段可以使用类似下面的 builder：

```dockerfile
FROM node:22-alpine AS build
WORKDIR /app
COPY current/package*.json ./
RUN npm install
COPY current/ ./
RUN npm run build
```

构建完成后，把 `dist/` 或 `build/` 导出到实例的 `public/` 目录，然后交给共享静态托管器。只有在用户明确需要独立容器时，才额外生成 nginx 静态容器。

推荐产物目录：

```text
apps/my-spa/
  current/
  public/
    index.html
    assets/
```

Vite 开发模式可作为备用，但不建议长期默认：

```dockerfile
FROM node:22-alpine
WORKDIR /app
COPY current/package*.json ./
RUN npm install
COPY current/ ./
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
```

推荐默认策略：

1. 如果能执行 `npm run build`，优先生成静态产物并交给共享静态托管器。
2. 构建阶段可以使用 Docker，构建完成后不保留 Node 容器常驻。
3. 如果没有 build 或构建失败，再退回 dev/preview 模式。
4. 长期运行 `npm run dev` 应标记为 `resourceProfile=medium`，不适合在 4G 机器上大量常驻。
5. 构建失败时标记 `build_failed`，交给大模型 skill 诊断。

### 8.3 Next.js / Express / Node 后端

识别依据：

1. `package.json` 中存在 `next`、`express`、`fastify`、`koa`、`nest` 等。
2. scripts 中存在 `start` 或 `dev`。
3. 项目存在 API 路由或服务端入口。

示例：

```dockerfile
FROM node:22-alpine
WORKDIR /app
COPY current/package*.json ./
RUN npm install
COPY current/ ./
ENV HOST=0.0.0.0
ENV PORT=3000
EXPOSE 3000
CMD ["npm", "run", "start"]
```

如果只有 `dev`，第一版可以先跑：

```dockerfile
CMD ["npm", "run", "dev"]
```

后续再优化生产构建。

### 8.4 Python Web 项目

识别依据：

1. 存在 `requirements.txt`、`pyproject.toml`、`uv.lock`、`Pipfile`。
2. 存在 `app.py`、`main.py`、`manage.py`。
3. 代码中出现 Flask、FastAPI、Django、Streamlit、Gradio。

FastAPI 示例：

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

Flask 示例：

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY current/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY current/ ./
ENV FLASK_RUN_HOST=0.0.0.0
ENV FLASK_RUN_PORT=5000
EXPOSE 5000
CMD ["flask", "run"]
```

Django 示例：

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY current/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY current/ ./
EXPOSE 8000
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
```

### 8.5 带 SQLite 的项目

SQLite 是第一版最值得支持的数据库形态。

推荐规范：

```text
apps/<id>/data/
  app.sqlite
```

Compose 中挂载：

```yaml
services:
  app:
    volumes:
      - ../data:/app/data
    environment:
      - DATABASE_URL=sqlite:////app/data/app.sqlite
```

这样代码升级时不会覆盖数据库。

### 8.6 带 Postgres / MySQL / Redis 的项目

第一版可以识别并标记，第二阶段再完整托管。

完整托管时可以生成多服务 Compose：

```yaml
services:
  app:
    build:
      context: ..
      dockerfile: docker/Dockerfile
    depends_on:
      - db
    ports:
      - "${HOST_PORT}:3000"
    environment:
      - DATABASE_URL=postgres://app:app@db:5432/app
    restart: unless-stopped

  db:
    image: postgres:16
    environment:
      - POSTGRES_USER=app
      - POSTGRES_PASSWORD=app
      - POSTGRES_DB=app
    volumes:
      - ../data/postgres:/var/lib/postgresql/data
    restart: unless-stopped
```

需要注意：自动生成数据库密码、迁移命令、初始化顺序都比 SQLite 更复杂。因此第一版应优先支持 SQLite，其他数据库先进入“需要确认”或“高级模式”。

### 8.7 容器资源限制与构建并发

对 4G 或 8G 小主机，必须把资源策略放进默认设计里。

建议默认规则：

1. 同一时间只允许一个 `docker build` 任务运行，避免多个 npm/pip 构建同时占满内存。
2. 小型 Node/Python 后端默认加内存和 CPU 限额。
3. Next.js、Streamlit、Gradio、带数据库项目默认标记为 `medium` 或 `heavy`。
4. `heavy` 实例导入后先进入确认流程，不自动启动。
5. 管理页展示容器内存、CPU、镜像大小、数据目录大小。

Compose 资源限制示例：

```yaml
services:
  app:
    build:
      context: ..
      dockerfile: docker/Dockerfile
    ports:
      - "${HOST_PORT}:3000"
    mem_limit: 512m
    cpus: "0.75"
    restart: unless-stopped
```

这不是为了做精细调度，而是为了防止单个小工具在小主机上异常占用资源。

## 9. 端口管理策略

建议固定端口池：

```text
管理页端口：17800
实例端口池：18000-19999
```

Docker 优先后，端口策略变为：

1. 扫描项目内部默认端口，例如 `3000`、`5173`、`8000`、`8501`。
2. 对容器实例，内部端口尽量不改。
3. 对共享静态实例，可以没有容器内部端口，只记录共享静态服务的宿主机端口或路由。
4. 从宿主机端口池选择空闲端口。
5. 对 Compose 实例，在 Compose 中建立映射：

```yaml
ports:
  - "18023:3000"
```

6. 对共享静态实例，生成静态托管配置，例如 `:18024 -> apps/static-demo/public`。
7. 把 `internalPort=3000`、`hostPort=18023` 写入 `local-web.json` 和 SQLite registry；静态实例则可以记录 `internalPort=null`、`hostPort=18024`。

端口冲突检查应检查两类端口：

1. 宿主机端口是否已被其他进程占用。
2. registry 中是否已登记该端口。

如果容器启动后健康检查失败，再判断是否是应用内部硬编码端口、只绑定 `127.0.0.1`、未读取 `PORT` 环境变量等问题。

## 10. 运行与服务托管

Docker 优先后，实例级运行命令统一为：

```bash
lwa start my-demo
lwa stop my-demo
lwa restart my-demo
lwa logs my-demo
```

工具内部实际调用：

对 Docker Compose 实例：

```bash
docker compose -p lwa-my-demo -f apps/my-demo/docker/compose.yaml up -d
docker compose -p lwa-my-demo -f apps/my-demo/docker/compose.yaml down
docker compose -p lwa-my-demo -f apps/my-demo/docker/compose.yaml restart
docker compose -p lwa-my-demo -f apps/my-demo/docker/compose.yaml logs
```

对共享静态实例：

```text
更新 static-gateway 配置
reload Caddy/Nginx/内置静态服务
检查 http://127.0.0.1:<HOST_PORT> 是否可访问
```

管理器自身可以由 systemd 或 launchd 托管：

```text
systemd / launchd
  -> lwa manager
  -> shared static gateway
  -> Docker Compose projects
```

这里不建议为每个实例单独生成 systemd service。需要长期进程的实例交给 Compose 管理；静态实例交给共享静态网关管理。

## 11. 大模型 skill 的位置

大模型 skill 的重点应该从“怎么在宿主机跑起来”调整为“怎么把项目可靠地转换成合适的运行形态”。对后端项目是 Docker 化；对静态和纯前端项目是构建产物并登记到共享静态托管。

建议 skills 包括：

```text
skills/
  detect-stack/
  detect-internal-port/
  prepare-static-html/
  build-frontend-static/
  dockerize-node-app/
  dockerize-python-app/
  dockerize-fullstack-app/
  detect-database/
  generate-static-gateway-config/
  generate-compose/
  fix-docker-build-failure/
  fix-container-startup-failure/
  fix-port-binding/
  diagnose-health-check/
```

每个 skill 只处理一个明确问题：

1. 识别项目结构。
2. 判断内部端口。
3. 判断启动命令。
4. 判断是否可以转成共享静态托管。
5. 生成静态托管配置、Dockerfile 或 Compose。
6. 修复 build 失败。
7. 修复容器启动失败。
8. 修复端口绑定或 localhost 问题。
9. 更新 `local-web.json`。

大模型不应该直接承担：

1. 常驻服务保活。
2. 端口池最终分配。
3. Docker Compose project 的长期状态管理。
4. 共享静态网关的长期运行。
5. registry 的最终写入。

这些仍由 `lwa` 工具完成。

## 12. 管理一览页

管理页仍是第二个核心交付物。

默认地址：

```text
http://<局域网IP>:17800
```

首页应直接展示实例列表，不需要营销式首页。

核心字段：

| 字段 | 说明 |
| --- | --- |
| 名称 | 实例名称 |
| 状态 | running / stopped / failed / pending / building |
| 类型 | static / node / python / fullstack |
| 运行层 | shared-static / docker-compose |
| 资源档位 | tiny / small / medium / heavy |
| 技术栈 | Vite、React、FastAPI、Flask、SQLite 等 |
| 是否带数据库 | 是 / 否 |
| 数据库类型 | SQLite、Postgres、MySQL、Redis、未知 |
| 宿主机端口 | 例如 18023 |
| 容器内部端口 | 例如 3000；静态托管可为空 |
| 访问地址 | 局域网 URL |
| 版本 | zip 导入版本 |
| 导入时间 | 首次导入时间 |
| 更新时间 | 最近更新版本时间 |
| 镜像 | 当前实例镜像名或 image id；静态托管可为空 |
| Compose 项目 | 例如 `lwa-my-demo`；静态托管可为空 |
| 内存/CPU | 容器运行时资源占用；静态托管展示共享服务占用 |
| 磁盘占用 | 源码、构建产物、镜像、数据目录大小 |
| 操作 | 打开、停止、重启、查看日志、重新构建、重新识别、删除 |

建议管理页包含：

1. **实例总览**
   展示全部实例数量、运行数量、失败数量、端口占用、技术栈分布。

2. **实例详情**
   展示 `local-web.json`、Dockerfile、Compose 摘要、静态托管配置摘要、健康检查结果。

3. **日志视图**
   对 Compose 实例展示 `docker compose logs` 的最近输出；对静态实例展示网关 reload、访问检查和构建日志。

4. **构建记录**
   展示最近一次 build 是否成功、耗时、错误摘要。

5. **导入队列**
   展示 `inbox/` 中待处理、处理中、失败的 zip。

6. **端口视图**
   展示端口池占用情况。

7. **数据卷视图**
   展示每个实例是否有 `data/`，大小多少，是否可备份。

8. **资源视图**
   展示 Docker 容器内存、CPU、镜像大小、数据目录大小、构建队列和重型依赖数量。

## 13. 注册表设计

建议继续使用 SQLite 作为 registry。

核心表可以包括：

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
  gateway_config_path

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

`local-web.json` 与 SQLite 的关系：

1. `local-web.json` 是实例目录内的可读配置和备份。
2. SQLite 是全局查询、排序、筛选和管理页展示的数据源。
3. Docker 实际状态可以从 Docker API 或 CLI 查询，再同步回 SQLite。
4. 共享静态托管状态可以从网关配置和健康检查结果同步回 SQLite。

## 14. 推荐技术选型

虽然运行层改为 Docker，主工具仍建议使用 Python。

理由：

1. zip 解压、文件扫描、模板渲染、SQLite、CLI 都很方便。
2. 管理页后端可以直接使用 FastAPI。
3. Python 调用 Docker CLI 或 Docker SDK 都可行。
4. 与大模型 skills 的文件读写、日志分析工作方式兼容。

推荐组合：

```text
CLI: Python + Typer 或 Click
管理后端: FastAPI
数据库: SQLite
前端: 简单 HTML / Jinja2 / 轻量 React
运行层: Docker Compose + 共享静态托管
共享静态托管: Caddy / Nginx / FastAPI StaticFiles
静态项目容器: 仅作为备用，不作为默认常驻形态
Node 基础镜像: node:22-alpine
Python 基础镜像: python:3.12-slim
管理器托管: systemd user service 或 launchd
```

第一版可以先通过 Docker CLI 调用实现，后续再考虑 Docker SDK。CLI 调用更透明，出错时日志也更接近用户熟悉的 Docker 输出。

如果目标机器是 4G 小主机，推荐优先使用原生 Ubuntu/Debian，而不是 macOS Docker Desktop 或 Windows Docker Desktop。Docker Desktop 带虚拟机开销，对小内存机器不如原生 Linux 稳定。

## 15. 和“本机直接运行”的对比

| 维度 | 本机直接运行 | Docker 优先 |
| --- | --- | --- |
| 依赖隔离 | 弱，容易污染宿主机 | 强，后端实例独立镜像；静态实例共享托管 |
| 端口冲突 | 需要改项目配置 | 用宿主机端口映射解决 |
| 数据库管理 | 容易分散 | Compose 和 volume 显式管理 |
| 迁移复现 | 依赖宿主机环境 | 复制目录后更容易复现 |
| 日志管理 | 各框架不同 | Compose 日志和静态网关日志统一进入管理页 |
| 服务保活 | 需要 systemd/launchd | Compose `restart: unless-stopped` + 共享静态网关 |
| 调试便利 | 简单项目更直接 | 多一层容器，但更标准 |
| 初始门槛 | 低 | 需要安装 Docker |
| 资源占用 | 简单静态页最低 | 需要轻量策略，避免所有实例常驻容器 |
| 安全边界 | 弱 | 比宿主机直接执行更好，但不是绝对安全 |

结论：

1. 如果只管理少量静态页，一个共享静态服务就足够，不需要每页一个容器。
2. 如果会长期积累很多 Node、Python、带数据库的小项目，Docker 明显更合理。
3. 对当前需求，项目数量和形态都会增长，因此 Docker 应作为默认路径。
4. 对 4G/8G 小主机，Docker 优先必须配合共享静态托管、SQLite 优先、构建限流和重型服务确认。
5. 本机直接运行可以保留为 fallback，不应作为主架构。

## 16. 小主机资源策略

如果目标机器只有 4G 或 8G 内存，系统默认策略应保守。

推荐按资源档位处理：

| 档位 | 自动行为 | 例子 |
| --- | --- | --- |
| `tiny` | 自动导入、自动托管、可自动启动 | 纯 HTML、已构建静态页面 |
| `small` | 可自动构建和启动，但加资源限制 | 小型 Flask/FastAPI/Express |
| `medium` | 可导入，启动前提示资源影响 | Next.js、Streamlit、Gradio、Vite dev server |
| `heavy` | 只导入和生成计划，不自动启动 | Postgres/MySQL、多服务 Compose、大模型相关服务 |

4G 机器建议：

1. 管理器 + 共享静态服务常驻。
2. 后端容器常驻数量控制在少量。
3. 默认一次只构建一个项目。
4. 优先 SQLite，不自动启动 Postgres/MySQL。
5. 不把 Next.js dev、Vite dev、Streamlit、Gradio 这类偏重进程大量常驻。
6. 管理页提供“停止闲置实例”和“清理构建缓存/镜像”的入口。

8G 机器建议：

1. 可以更放心地运行多个小型 Node/Python 容器。
2. 仍然不建议每个静态页一个容器。
3. 可以允许一两个数据库容器，但仍应登记磁盘和内存占用。
4. 构建并发可以从 1 提升到 2，但默认仍应保守。

真正应该避免的是：

```text
大量 Node dev server
+ 多个 Python 长驻服务
+ 多个 Postgres/MySQL
+ 同时 docker build
```

所以最终原则不是“Docker 越多越好”，而是：

**Docker 负责隔离复杂运行时，共享静态托管负责承载轻量网页。**

## 17. 安全与边界

Docker 能改善隔离，但不能把未知 zip 变成绝对安全。

需要明确：

1. `docker build` 会执行 Dockerfile 中的 `RUN` 命令。
2. `npm install` 可能触发 `postinstall` 脚本。
3. `pip install` 也会执行包构建逻辑。
4. 容器如果挂载了宿主机敏感目录，仍然可能造成破坏。
5. 管理页如果暴露到公网，会引入额外风险。

第一版建议假设 zip 来源可信，也就是主要来自用户自己的 Web coding 产物。

安全策略建议：

1. 默认只监听局域网。
2. 管理页加 token 或密码。
3. 每个实例只挂载自己的 `data/` 目录。
4. 不挂载 Docker socket 到实例容器。
5. 不默认使用 `--privileged`。
6. 对外部数据库、Docker Compose 自带复杂服务的项目，进入确认流程。
7. 自动执行 build/up 前，在管理页或 CLI 中显示将要执行的计划。

## 18. 最小可行版本

MVP 应围绕“共享静态托管 + Docker Compose 后端容器”建立完整闭环。

第一版建议只做：

1. 初始化目录结构：

```bash
lwa init
```

2. 导入 zip：

```bash
lwa import inbox/demo.zip
```

3. 自动识别三类项目：

```text
static
frontend-static
backend-container
```

4. 静态 HTML 直接登记到共享静态托管器。
5. 纯前端项目优先构建成 `public/` 静态产物，再登记到共享静态托管器。
6. Node/Python 后端项目生成 Dockerfile、`compose.yaml` 和 `.env`。
7. 自动分配宿主机端口。
8. 生成 `local-web.json`。
9. 写入 SQLite registry。
10. 支持启动、停止、重启、日志：

```bash
lwa start demo
lwa stop demo
lwa restart demo
lwa logs demo
```

11. 管理页展示全部实例。
12. 管理页可以打开实例访问地址。
13. 管理页展示运行形态、Docker 构建状态、静态托管状态和资源档位。
14. 默认限制构建并发，适配 4G/8G 小主机。

MVP 暂时不强求：

1. 自动支持 Postgres、MySQL、Redis。
2. 自动运行复杂数据库迁移。
3. 自动修复所有硬编码端口。
4. 完全无人值守地执行未知 zip。
5. 多用户权限系统。
6. 跨机器同步。
7. 精细化资源调度。

## 19. 后续演进

第二阶段：

1. `lwa watch` 自动监听 `inbox/`。
2. 支持同名应用版本更新。
3. 支持 SQLite 数据目录自动识别和迁移保护。
4. 支持大模型 skill 自动诊断 pending 实例。
5. 支持重新生成静态托管配置、Dockerfile 和 Compose。
6. 支持管理页一键重建、一键重启、一键查看日志。
7. 支持资源占用统计、镜像大小统计和构建队列。

第三阶段：

1. 支持 Postgres、MySQL、Redis 多服务 Compose。
2. 支持备份和恢复实例。
3. 支持导出实例为可迁移包。
4. 支持 Docker 镜像清理和磁盘占用统计。
5. 支持健康检查和失败自动重启策略。
6. 支持多机器导入或远程部署。
7. 支持闲置实例自动停止或定时休眠。

## 20. 最终建议

最终建议采用以下路线：

1. `lwa` 使用 Python 实现 CLI 和管理页后端。
2. Docker Compose 作为复杂实例和后端实例的默认运行层。
3. 共享静态托管作为纯静态 HTML 和纯前端 build 产物的默认运行层。
4. 不把每个 zip 都粗暴变成长期运行容器。
5. 每个实例目录下保存源码、静态产物、数据、Docker 配置和元数据。
6. SQLite registry 作为全局索引。
7. 端口池负责统一分配宿主机访问端口。
8. 管理页提供实例总览、状态、端口、技术栈、数据库、版本、日志、资源占用和操作入口。
9. 大模型 skills 专注于运行形态判断、Docker 化、静态构建、配置修复和日志诊断。
10. 本机直接运行只作为 fallback，不作为主架构。

这样设计的结果是：用户仍然只需要把 zip 放进实例库，但系统内部会把它标准化成可管理、可重启、可迁移、可展示的运行单元。复杂项目享受 Docker 的隔离和可复现性，轻量网页走共享静态托管以节省资源。相比直接在宿主机运行 npm 或 Python 项目，这个混合方案更适合长期积累大量小网页和本地 Web 工具，也更适合 4G/8G 小主机。

## 21. 后续待办

1. 明确工具名称和命令名，例如 `lwa`。
2. 定义 Docker 优先、共享静态托管优先的目录结构。
3. 定义 `local-web.json` schema。
4. 设计 SQLite registry schema。
5. 实现 `init/import/start/stop/restart/logs/status` 核心命令。
6. 实现共享静态托管器配置生成和 reload。
7. 实现静态 HTML 和纯前端 build 产物托管。
8. 实现 Node/Python 后端 Dockerfile 模板。
9. 实现 Compose 模板和 `.env` 生成。
10. 实现端口池和端口冲突检测。
11. 实现 Docker build/up/down/logs 调用层。
12. 实现构建队列和默认构建并发限制。
13. 实现最简管理页。
14. 再补充大模型 skills 的运行形态判断、Docker 化和静态托管流程文档。
