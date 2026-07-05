# Local Web Access (`lwa`)

面向局域网小主机的**本地网页部署基座**：把一个打包好的 `zip` 项目，一条命令导入、自动识别运行形态、分配端口并对局域网暴露访问入口。

- 纯静态 HTML → 经共享静态网关（Caddy 优先，内置 `http.server` 兜底）直接托管
- 纯前端 SPA（Vite / React / Vue / Svelte 等）→ 自动 `npm install` + `build`，托管构建产物
- Node / Python 后端、含 SQLite 的全栈项目 → 自动生成 Dockerfile + Docker Compose 并起容器

面向 4G/8G 内存的小主机设计：端口池隔离、构建并发限流、资源监控与日志治理，尽量做到"导入即用"。

> 项目当前处于活跃开发中（Phase 0~4 已完成，见文末[路线图](#路线图)）。管理页与守护进程（Phase 5+）尚未实现，现阶段通过 `lwa` 命令行使用。

## 特性

- **一键导入**：`lwa import xxx.zip` 完成解压、zip-slip 防护、sha256 校验、单层根目录拍平、实例登记。
- **运行形态自动识别**：扫描 `package.json` / `requirements.txt` / `pyproject.toml` / `Pipfile` / `manage.py` 等，判定 `static` / `node` / `python` 与是否含数据库。
- **端口池管理**：从配置端口池中分配，跳过 registry 已登记端口与宿主机实际监听端口，生成 `lanUrl` / `healthUrl`。
- **静态托管**：Caddy 可用时用 Caddy，否则降级到内置 `http.server`；支持嵌套 `index.html` 与同级资源。
- **容器托管**：按技术栈生成 Dockerfile（非 root、`EXPOSE` 内部端口）与 Compose（端口/资源限额/`restart: unless-stopped`/SQLite `data/` 持久化 bind mount）。
- **生命周期编排**：`start` / `stop` / `restart` / `rebuild` / `remove`，双层锁（进程内 `RLock` + 跨进程文件锁 + 陈旧锁回收）串行化同一实例操作；容器复用已登记端口保证 `lanUrl` 稳定。
- **可观测性**：分类日志（build / run / gateway / import / scan）与按大小滚动、HTTP 健康检查、状态聚合、整机与实例级资源统计。
- **构建队列**：信号量限流（默认并发 1），拿不到槽位即标记 `queued`，排队超时可控。
- **SQLite Registry**：七张表（instances / containers / static_sites / ports / events / builds / resources），外键级联、WAL 模式。

## 安装

需要 Python 3.13+。托管容器实例需要 Docker（最新稳定版）+ `docker compose`；生成的容器基线镜像为 `node:24-alpine` 与 `python:3.13-slim`。静态托管在无 Caddy 时自动使用内置服务。

```bash
# 克隆后在项目根目录
pip install -e .

# 开发依赖（测试）
pip install -e ".[dev]"
```

安装后提供 `lwa` 命令；也可用 `python -m local_web_access` 调用。

## 快速开始

```bash
# 1. 在目标工作区目录初始化（生成 local-web.yml、目录结构、SQLite registry）
lwa init

# 2. 导入一个打包好的项目 zip
lwa import ./inbox/my-site.zip --name my-site

# 3.（可选）对被标记为 pending 的实例重新识别
lwa scan

# 4. 启动实例（静态 / 前端 / 容器统一入口）
lwa start my-site

# 5. 查看状态、日志、资源
lwa status
lwa logs my-site --category run --tail 200
lwa stats

# 6. 停止 / 重启 / 重建 / 移除
lwa stop my-site
lwa restart my-site
lwa rebuild my-site
lwa remove my-site            # 默认保留 apps/<id>/ 磁盘文件，仅删 registry 索引
lwa remove my-site --purge --force   # 连同磁盘文件与非空 data/ 一起删除
```

## 命令参考

| 命令 | 说明 |
| --- | --- |
| `lwa init [-w DIR] [--force]` | 初始化工作区（目录 / 配置 / registry），幂等 |
| `lwa import <zip> [-n NAME]` | 导入 zip：解压、识别、登记实例 |
| `lwa scan [ID]` | 重新扫描实例（省略 ID 则扫所有 `pending`） |
| `lwa start <ID>` | 启动实例（容器已部署走轻量 `compose start`，否则全量部署） |
| `lwa stop <ID>` | 停止实例（静态禁用网关+释放端口；容器 `compose stop`，不删数据） |
| `lwa restart <ID>` | 先停再启（容器走轻量 start，不重建镜像） |
| `lwa rebuild <ID>` | 强制重建镜像/产物，经构建队列限流 |
| `lwa remove <ID> [--purge] [--force]` | 移除实例；`--purge` 删磁盘文件，非空 `data/` 需 `--force` |
| `lwa logs <ID> [-c CATEGORY] [-n TAIL]` | 查看实例日志（build/run/gateway/import/scan） |
| `lwa status [ID]` | 查看实例状态（省略 ID 显示全部） |
| `lwa stats [ID]` | 查看资源占用（整机 + 实例目录/镜像/容器） |
| `lwa list` | 列出所有实例及端口 |
| `lwa version` | 显示版本号 |

全局选项 `-v/--verbose` 输出 DEBUG 日志。

## 配置（`local-web.yml`）

由 `lwa init` 生成，关键字段：

```yaml
managerPort: 17800          # 管理页端口（不能落在端口池内）
managerHost: 0.0.0.0
portPool:                   # 实例端口池
  start: 18000
  end: 19999
staticGateway: caddy        # caddy | nginx | builtin
buildConcurrency: 1         # 构建并发数（小主机建议保持 1）
defaultResourceLimits:
  memory: 512m
  cpus: "0.75"
lanIpStrategy: auto         # auto（自动探测）| manual
manualLanIp: null
logLevel: INFO
```

## 工作区目录布局

```
<workspace>/
├─ local-web.yml            # 全局配置
├─ inbox/                   # 待导入的 zip
├─ registry/local-web.db    # SQLite registry
├─ static-gateway/sites/    # 静态站点网关配置
├─ run/                     # 运行期 PID / 锁文件
├─ logs/                    # 全局日志
└─ apps/<id>/
   ├─ local-web.json        # 实例元数据（真相文件）
   ├─ source/               # 原始 zip 与解压快照
   ├─ current/              # 当前项目源码
   ├─ public/               # 静态/前端托管产物
   ├─ data/                 # 持久化数据（SQLite 等，bind mount 进容器）
   ├─ docker/               # 生成的 Dockerfile / compose.yaml / .env
   └─ logs/                 # 分类日志
```

## 开发与测试

```bash
pip install -e ".[dev]"
python -m pytest            # 全量单元测试（全部 hermetic，不依赖真实 Docker）
```

代码位于 `src/local_web_access/`，测试位于 `tests/`。容器相关测试通过替身（fake runtime）运行，无需本机安装 Docker。

## 路线图

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| Phase 0 | CLI 骨架 / 配置 / registry / schema | 已完成 |
| Phase 1 | zip 导入 / 形态识别 / 端口池 | 已完成 |
| Phase 2 | 静态网关 / 纯静态 / 前端构建托管 | 已完成 |
| Phase 3 | Dockerfile / Compose / Runtime / Node & Python 容器 | 已完成 |
| Phase 4 | 生命周期 / 日志 / 健康 / 资源 / 构建队列 | 已完成 |
| Phase 5 | 守护进程与管理页（API + 前端） | 规划中 |
| Phase 6 | Skills、安全与排障 | 规划中 |
| Phase 7 | 测试、验收与发布 | 规划中 |

详细设计见 `docs/plan/local-web-access-v1-design-20260704.md` 与 `docs/plan/local-web-access-v1-wbs-20260704.md`；任务跟踪见 `task-list.md`。

## 许可

MIT
