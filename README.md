# Local Webpage Access (`lwa`)

面向局域网小主机的**本地网页部署基座**：把一个打包好的 `zip` 项目，一条命令导入、自动识别运行形态、分配端口并对局域网暴露访问入口。

- 纯静态 HTML → 经共享静态网关（Caddy 优先，内置 `http.server` 兜底）直接托管
- 纯前端 SPA（Vite / React / Vue / Svelte 等）→ 自动 `npm install` + `build`，托管构建产物
- Node / Python 后端、含 SQLite 的全栈项目 → 自动生成 Dockerfile + Docker Compose 并起容器

面向 4G/8G 内存的小主机设计：端口池隔离、构建并发限流、资源监控与日志治理，尽量做到"导入即用"。

V1 已完成全部功能（Phase 0~7），提供 CLI、管理页（HTTP API + 前端）、自动导入守护进程、安全审计与 `lwa doctor` 排障。详见文末[路线图](#路线图)。

## 特性

- **一键导入**：`lwa import xxx.zip` 完成解压、zip-slip 防护、sha256 校验、单层根目录拍平、实例登记；放入 `inbox/` 由 daemon 自动导入亦可。
- **运行形态自动识别**：扫描 `package.json` / `requirements.txt` / `pyproject.toml` / `Pipfile` / `manage.py` 等，判定 `static` / `node` / `python` 与是否含数据库；识别失败标记 `pending` 并写入风险提示事件。
- **端口池管理**：从配置端口池中分配，跳过 registry 已登记端口与宿主机实际监听端口，生成 `lanUrl` / `healthUrl`；静态与容器实例均可选**路径别名**（`/<slug>/` 统一入口，**需 Caddy**；builtin 下设置会被拦截）。
- **静态托管**：Caddy 可用时用 Caddy，否则降级到内置 `http.server`；支持嵌套 `index.html` 与同级资源。
- **容器托管**：按技术栈生成 Dockerfile（非 root、`EXPOSE` 内部端口）与 Compose（端口/资源限额/`restart: unless-stopped`/SQLite `data/` 持久化 bind mount）。
- **生命周期编排**：`start` / `stop` / `restart` / `rebuild` / `remove`，实例级双层锁（进程内 `RLock` + 跨进程文件锁 + 陈旧锁回收）串行化同一实例操作；容器复用已登记端口保证 `lanUrl` 稳定。
- **可观测性**：分类日志（build / run / gateway / import / scan）与按大小滚动、HTTP 健康检查、状态聚合、整机与实例级资源统计；管理页**浏览量统计**（Caddy 别名入口 JSON access log / builtin gateway.log / 容器日志尽力解析）。
- **构建队列**：跨进程闸门限流（默认并发 1，`registry/build-locks.db`），拿不到槽位即标记 `queued`，排队超时可控。
- **SQLite Registry**：七张表（instances / containers / static_sites / ports / events / builds / resources），外键级联、WAL 模式。
- **管理页（WBS-22/23）**：内置 HTTP API + Vue 单页前端，token 鉴权，覆盖实例列表 / 详情 / 日志 / 资源 / 生命周期 / 路径别名 / **浏览量** / **冗余清理** / pending 队列 / 端口池 / 统计。
- **自动导入守护进程（WBS-21）**：`lwa daemon on` 后监听 `inbox/`，自动导入并启动可确定的轻量实例。
- **安全审计（WBS-25）**：对生成的 Compose / Dockerfile / zip 成员做 critical/warn/info 分级审计，critical 问题拒绝写出；管理页绑定校验（LAN 绑定 + token）。
- **排障辅助（WBS-26）**：`lwa doctor` 检查 Python / Docker / Compose / 端口池 / registry / 磁盘 / 内存，并可对单个实例做深度诊断。
- **大模型 Skills（WBS-24）**：16 个 SKILL.md 覆盖环境初始化、导入、托管、容器、生命周期、自启动、排障等场景，供 AI 编程助手协作。

## 安装

需要 Python 3.13+，以及 **fastapi ≥ 0.138.0**、**uvicorn ≥ 0.45.0**（见 `pyproject.toml`）。托管容器实例需要 **Docker ≥ 29.0.0** 与 **Docker Compose ≥ 2.40.2**（推荐 ≥ 5.2.0）；静态网关优先使用 Caddy（Caddy 模式需 **Caddy ≥ 2.10.0**），无 Caddy 时自动使用内置服务。生成的容器基线镜像为 `node:24-alpine` 与 `python:3.13-slim`。运行 `lwa doctor` 可逐项校验上述版本。

```bash
# 克隆后在项目根目录
pip install -e .

# 开发依赖（测试）
pip install -e ".[dev]"
```

安装后提供 `lwa` 命令；也可用 `python -m local_webpage_access` 或 `python -m local_webpage_access.cli` 调用（实现位于 `cli/` 包，对用户透明）。

安装完成后建议运行 `lwa setup` 检测宿主机工具，再 `lwa doctor` 做完整自检（需先 `lwa init`），详见 [排障指南](docs/faq.md)。

## 快速开始

```bash
# 0.（首次）检测宿主机环境，按提示安装 Docker / Compose / Caddy / Node 等
lwa setup
# 可选：lwa setup --script   # 输出当前平台参考安装脚本（需人工审阅）

# 1. 在目标工作区目录初始化（生成 local-web.yml、目录结构、SQLite registry）
lwa init

# 2. 导入一个打包好的项目 zip
lwa import ./inbox/my-site.zip --name my-site
# 可选：路径别名（需 Caddy；http://<LAN-IP>:<staticGatewayPort>/<slug>/）
lwa import ./inbox/my-site.zip --path-alias my-site

# 2b. 同项目新版本：原地更新 zip（保留 id / 端口 / data / 路径别名）
# 容器实例会自动 rebuild 镜像；静态/前端走 restart。加 --no-restart 则只换源码。
lwa import ./inbox/my-site-v2.zip --update my-site

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

# 7.（可选）打开管理页、守护进程与 Caddy 网关
lwa manager on                # 后台启动管理页（默认 init 后通常已自动拉起）
lwa manager start             # 前台启动管理页，Ctrl+C 退出
lwa daemon on                 # 监听 inbox/，自动导入并启动轻量实例（含启动/周期自愈）
lwa gateway on                # staticGateway=caddy 时启动 Caddy master（:8080 路径别名入口）

# 7b.（可选）开机/登录自启 daemon + manager（+ 可选 gateway）：macOS launchd / Linux·WSL systemd
lwa autostart install         # 生成并启用前台监管单元（IMP-030），见 docs/autostart.md
# lwa autostart install --with-caddy   # staticGateway=caddy 时额外监管 gateway
# lwa autostart check                   # 完备性深检（解释器/单元/启用态/进程/Caddy/linger）
# lwa setup --autostart                 # 兼容旧入口，等价 lwa autostart install

# 8.（可选）环境/实例排障
lwa doctor                    # 全部环境检查
lwa doctor my-site            # 对单个实例深度诊断
lwa access review             # 复核访问地址可用性（别名白屏 / 空 200 自查）
```

管理页 token 在首次 `lwa manager on` 或 `lwa manager start` 时生成；也可在工作区 `run/` 下查看。详见 [管理页说明](docs/manager-page.md)。

## 命令参考

| 命令 | 说明 |
| --- | --- |
| `lwa init [-w DIR] [--force]` | 初始化工作区（目录 / 配置 / registry / skills），幂等 |
| `lwa update` | 升级 lwa 包并热重载工作区：同步 skills、补齐配置、重启 manager/daemon、可选 doctor / restart 实例 |
| `lwa import <zip> [-n NAME] [--path-alias SLUG] [--update ID]` | 导入 zip；可选路径别名；`--update` 原地升级（容器自动 rebuild，静态/前端 restart；`--no-restart` 仅换源码） |
| `lwa alias set <ID> <slug>` / `lwa alias clear <ID>` | 为静态或容器实例设置/清除路径别名（需 Caddy；与管理页/API 共用逻辑） |
| `lwa scan [ID]` | 重新扫描实例（省略 ID 则扫所有 `pending`） |
| `lwa start <ID>` | 启动实例（容器已部署走轻量 `compose start`，否则全量部署） |
| `lwa stop <ID>` | 停止实例（静态禁用网关+释放端口；容器 `compose stop`，不删数据） |
| `lwa restart <ID>` | 先停再启（容器走轻量 start，不重建镜像） |
| `lwa rebuild <ID>` | 强制重建镜像/产物，经构建队列限流 |
| `lwa remove <ID> [--purge] [--force]` | 移除实例；`--purge` 删磁盘文件，非空 `data/` 需 `--force` |
| `lwa remove --redundant [--purge]` | 批量清理冗余实例（按 `sourceZipHash` 去重保留最早者，IMP-012 / IMP-019） |
| `lwa logs <ID> [-c CATEGORY] [-n TAIL]` | 查看实例日志（build/run/gateway/import/scan） |
| `lwa status [ID]` | 查看实例状态（省略 ID 显示全部） |
| `lwa stats [ID]` | 查看资源占用（整机 + 实例目录/镜像/容器） |
| `lwa list` | 列出所有实例及端口 |
| `lwa setup [--script] [--json] [--autostart] [--with-caddy]` | 检测宿主机工具环境；`--autostart` 委托 `lwa autostart install`（OPS-025 / IMP-030） |
| `lwa doctor [ID] [--json]` | 诊断环境与实例问题；`--json` 输出机器可读报告，有 fail 时退出码 1 |
| `lwa manager on / off / status` | 后台启动 / 停止 / 查看管理页状态 |
| `lwa manager start` | 前台启动管理页 HTTP 服务（Ctrl+C 退出） |
| `lwa manager logs [-n TAIL]` | 查看管理页运行时日志（`logs/manager.log`） |
| `lwa daemon on / off / status` | 控制 inbox/ 自动导入守护进程（启动即自愈 + 周期 reconcile，DEV-042） |
| `lwa gateway on / off / status` | 控制 Caddy 网关 master（admin :2019 探活；切 builtin 后仍可关残留 master，BUG-077）；`on` 默认复核访问地址，`--rebuild-if-needed` 对 IMP-023 命中实例自动 rebuild（G6） |
| `lwa access refresh` | 用当前 LAN IP 重算所有实例 lanUrl/routeUrl（DHCP 换网、重启网关后地址漂移自愈，G1） |
| `lwa access review [--json] [--rebuild-if-needed]` | 复核各实例声明 URL 的真实可用性（回环 / lanUrl / routeUrl + SPA 绝对路径空 200 检测 IMP-023）；默认仅提示需 rebuild 的实例 |
| `lwa autostart install [--with-caddy] [--no-enable] [--linger]` | 生成开机/登录自启单元（默认启用；`--no-enable` 只生成、不改 daemon 运行意图；macOS launchd / Linux·WSL systemd 前台监管，IMP-030） |
| `lwa autostart enable / disable / status` | 加载 / 停用（持久 disable）/ 查看自启动单元与对应前台进程；迁移失败不会强行 enable |
| `lwa autostart check [--json]` | 完备性深检（解释器 / PATH 可用性 / 工作区 / 单元形态 / 启用态 / MainPID 身份 / 进程 / Caddy·:2019 / linger / WSL / Docker），fail 退出码 1 |
| `lwa autostart repair [--with-caddy]` | 重写失效路径、迁移旧 detached 启动器并重新启用 |
| `lwa autostart uninstall [--purge-linger]` | 停服务 + 删单元文件（不删工作区数据） |
| `lwa version` | 显示版本号 |

全局选项 `-v/--verbose` 输出 DEBUG 日志。各命令的参数细节可用 `lwa <command> --help` 查看。

## 配置（`local-web.yml`）

由 `lwa init` 生成，关键字段：

```yaml
managerPort: 17800          # 管理页端口（不能落在端口池内）
managerHost: 0.0.0.0
portPool:                   # 实例端口池
  start: 18000
  end: 19999
staticGateway: caddy        # caddy | nginx | builtin
staticGatewayPort: 8080    # 路径别名统一入口端口（Caddy 模式，需与 managerPort 错开）
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
├─ inbox/                   # 待导入的 zip（daemon 自动监听；成功后可归档到 processed/）
├─ apps/                    # 已导入实例
├─ registry/
│  ├─ local-web.db          # SQLite registry
│  └─ build-locks.db        # 构建队列跨进程闸门（DEV-047）
├─ static-gateway/sites/    # 静态站点网关配置
├─ static-gateway/aliases/  # 路径别名路由片段（Caddy 模式）
├─ run/                     # 运行期 PID / 锁 / 管理 token / pageviews.db
├─ logs/                    # 全局日志（含 Caddy 别名入口 static-access.log）
├─ templates/               # 用户可编辑模板副本
├─ manager/                 # 管理页静态资源与运行相关目录
├─ skills/                  # 16 个大模型协作 SKILL.md（WBS-24）
└─ apps/<id>/
   ├─ local-web.json        # 实例元数据（真相文件）
   ├─ source/               # 原始 zip 与解压快照
   ├─ current/              # 当前项目源码
   ├─ public/               # 静态/前端托管产物
   ├─ data/                 # 持久化数据（SQLite 等，bind mount 进容器）
   ├─ docker/               # 生成的 Dockerfile / compose.yaml / .env / .env.local
   └─ logs/                 # 分类日志
```

## 管理页

`lwa manager on` 可后台启动管理页，`lwa manager start` 可前台启动管理页（FastAPI + 单页前端），默认监听 `0.0.0.0:17800`。
首次启动会生成访问 token 并打印到终端，浏览器打开后输入 token 即可使用；本机 `127.0.0.1` / `localhost` / `::1` 访问 API 可免 token。
管理页覆盖实例列表、详情、日志查看、资源占用、start/stop/restart/rebuild/删除、
路径别名（静态与容器，需 Caddy）、**浏览量**列与详情、**冗余**徽章/筛选/批量清理、
pending/failed/可恢复异常队列、端口池与统计。Caddy 模式下实例状态除运行中/已停止外，
还会区分 **网关不可达**（`gateway_down`，master 离线）与 **配置无效**（`config_invalid`，
master 在线但站点端口不通）；二者在列表中高亮，可点 **恢复**（先尝试拉起 Caddy master
再 restart，等价 `POST /api/instances/{id}/recover`）。API 端点与鉴权细节见
[管理页说明](docs/manager-page.md)；日常运维见 [运维手册](docs/operations-playbook.md)。

## 自动导入守护进程

`lwa daemon on` 开启 inbox/ 自动监听：把 zip 放进 `inbox/`，daemon 会自动导入，
对可确定的轻量实例（纯静态、已能识别的前端）直接启动；启动时与每 60s 执行一次
`reconcile()`，恢复 `desired=running` 但进程/网关已掉线的实例（宿主机重启后 registry 仍标
running 也会先观测再拉起）。`lwa daemon off` 关闭，`lwa daemon status` 查看运行状态。

## 开发与测试

```bash
pip install -e ".[dev]"
python -m pytest            # 全量单元测试与集成测试（不依赖真实 Docker）
```

代码位于 `src/local_webpage_access/`，测试位于 `tests/`。容器相关测试通过替身（fake runtime）运行，无需本机安装 Docker；真实 Docker 集成测试需设置 `LWA_RUN_DOCKER_TESTS=1`，详见 [测试指南](docs/testing.md)。

样例项目夹具见 `tests/fixtures/`（6 个样例：静态 HTML、Vite/React、Node/Express、FastAPI+SQLite、构建失败、未识别 pending）。

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [docs/runtime-workspace.md](docs/runtime-workspace.md) | Runtime 工作区目录说明与使用引导 |
| [docs/operations-playbook.md](docs/operations-playbook.md) | 日常运维速查（网关选型、inbox 规范、冗余清理、容器别名、Caddy 排障） |
| [docs/manager-page.md](docs/manager-page.md) | 管理页 API 端点、鉴权与使用（WBS-30.06） |
| [docs/faq.md](docs/faq.md) | 常见问题与排障路径（WBS-30.09） |
| [docs/security-boundary.md](docs/security-boundary.md) | 安全边界与默认保护（WBS-30.10） |
| [docs/release-checklist.md](docs/release-checklist.md) | V1 发布清单（WBS-30.11） |
| [docs/known-limitations.md](docs/known-limitations.md) | V1 已知限制（WBS-30.12） |
| [docs/autostart.md](docs/autostart.md) | 开机自启（macOS launchd / Linux systemd / Windows 任务计划） |
| [docs/testing.md](docs/testing.md) | 测试体系与运行方式 |
| [docs/acceptance-checklist.md](docs/acceptance-checklist.md) | V1 端到端验收清单 |

## 路线图

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| Phase 0 | CLI 骨架 / 配置 / registry / schema | 已完成 |
| Phase 1 | zip 导入 / 形态识别 / 端口池 | 已完成 |
| Phase 2 | 静态网关 / 纯静态 / 前端构建托管 | 已完成 |
| Phase 3 | Dockerfile / Compose / Runtime / Node & Python 容器 | 已完成 |
| Phase 4 | 生命周期 / 日志 / 健康 / 资源 / 构建队列 | 已完成 |
| Phase 5 | 守护进程与管理页（API + 前端） | 已完成 |
| Phase 6 | Skills、安全与排障 | 已完成 |
| Phase 7 | 测试、验收与发布 | 已完成 |

任务跟踪见 `task-list.md`。

## 许可

MIT
