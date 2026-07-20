# Runtime 工作区说明

`lwa init` 会在你指定的目录（例如本仓库下的 `runtime/`）创建 **Runtime 工作区**——这是 lwa **实际运行时的项目文件夹**，所有导入的 zip、实例数据、日志、端口登记与管理页状态都落在这里。

> CLI 会自动向上查找包含 `local-web.yml` 的目录作为当前工作区。在 Runtime 目录内执行 `lwa` 命令即可，无需每次加 `-w`。

## 快速上手

```bash
cd runtime          # 进入工作区（示例路径）
lwa setup           # 可选：检查宿主机工具（default，无需工作区）
lwa init            # 首次：生成目录与配置（已 init 可跳过）
# Full：lwa init --full --yes，或先 init 再 lwa setup --full --yes
lwa doctor --profile full   # Full 环境建议
lwa manager on      # 管理页（默认 init 后自动拉起，端口 17800）
lwa import inbox/xxx.zip --name my-app
lwa start my-app
lwa list
```

管理页：本机 http://127.0.0.1:17800/（本机免 token）；局域网 http://\<LAN-IP\>:17800/（需 token；`/api/health` 带 token 才返回完整 capabilities）。

---

## 顶层目录一览

```
runtime/                      ← 工作区根（Runtime 根目录）
├── local-web.yml             ← 全局配置（端口池、管理页、静态网关、profile/serviceUser 等）
├── inbox/                    ← 待导入 zip 投放区（daemon 也会监听；成功后可归档到 processed/）
├── apps/                     ← 已导入实例（每个子目录一个实例）
├── registry/                 ← SQLite 全局索引（local-web.db）与构建闸门（build-locks.db）
├── static-gateway/           ← 静态网关配置（Caddy 站点/别名片段等）
├── logs/                     ← 工作区级日志：lwa.log / manager.log / daemon.log / gateway.log / static-access.log
├── run/                      ← token、daemon/manager/gateway 状态、pageviews.db、capability-*.json、full-setup-state.json
├── templates/                ← 用户可编辑的 Dockerfile/Compose 模板副本
└── skills/                   ← 从包内复制的大模型 Skill 文档
```

### `inbox/`

- **用途**：放置待导入的 `.zip`；`lwa import inbox/foo.zip` 或 daemon 自动导入。
- **注意**：zip 内若含 `node_modules` 等大目录，导入时会 **自动剥离**可重建依赖；仍保留 zip slip / 符号链接安全审计。
- **归档（IMP-011）**：daemon 导入成功（started/pending/conflict 终态）后会把 zip 物理移入 `inbox/processed/`（同名加时间戳），从扫描视野移除，避免重复导入与 `-2/-3` 冗余实例；slug 冲突时不再自动改名，而是记事件并提示用 `lwa import <zip> --update <slug>`。

### `apps/<instance-id>/`

每个导入成功的实例一个目录，`instance-id` 一般为 slug（如 `voiceprint-v3-demo`）。

| 路径 | 用途 |
| --- | --- |
| `source/original.zip` | 导入时保存的原始 zip 副本 |
| `current/` | 解压后的项目源码（扫描、构建输入） |
| `public/` | 静态托管根目录（纯静态或前端 build 产物同步于此） |
| `data/` | 持久化数据（如 SQLite bind mount 对应目录；导入时为空） |
| `logs/` | 实例分类日志：`build.log` `run.log` `gateway.log` `import.log` `scan.log` |
| `docker/` | 容器实例的 `Dockerfile`、`compose.yaml`、`.env` |
| `local-web.json` | 实例 manifest（名称、技术栈、端口、运行态等） |

### `registry/local-web.db`

- **用途**：SQLite 全局 registry（instances / ports / events / builds / resources 等）。
- **关系**：`local-web.json` 是实例目录内的真相文件；registry 是管理页与 `lwa list` 的查询索引。
- **同目录**：`registry/build-locks.db` 为构建队列跨进程闸门（DEV-047），CLI / 管理页 / daemon 共享。

### `static-gateway/`

- **用途**：共享静态网关配置。
- **Caddy 模式**：`sites/<id>.conf` 按实例生成；`aliases/<id>.conf` 为路径别名路由片段（IMP-006 / IMP-014），由主 Caddyfile 在 `staticGatewayPort` 统一入口 import；统一入口块开启 JSON access log（`logs/static-access.log`，IMP-024）。
- **builtin 模式**：每个静态实例仍占独立 hostPort，由内置 `http.server` 子进程服务；**不支持**路径别名统一入口。设置别名会被 **IMP-022 拦截报错**（仅清除别名允许）；访问请用 hostPort。

### `logs/`

- **用途**：工作区级日志（IMP-034）：
  - `lwa.log` — CLI / 通用操作
  - `manager.log` / `daemon.log` / `gateway.log` — 各后台进程
  - `static-access.log` — Caddy 别名入口 JSON access log（浏览量）
- 排障对照见 [FAQ · 症状→日志](faq.md#症状--日志文件--命令imp-034)。

### `run/`

- **用途**：运行态元数据，例如：
  - `manager-token.json` — 管理页 API token
  - `manager.json` — 管理页后台进程状态；`manager-start.lock` 串行化 `manager on`，
    `manager.instance.lock` 为管理页运行态单实例锁（BUG-193，避免并发实例互踩状态）
  - `daemon.json` / `daemon.lock` — daemon 开关与 watcher 锁
  - `gateway.json` — Caddy 网关后台服务态（IMP-010）
  - `caddy.pid` — Caddy master pid（`caddy start --pidfile` 写入）
  - `pageviews.db` — 浏览量聚合与摄入游标（IMP-024）
  - `capability-manager.json` / `capability-daemon.json` / `capability-gateway.json` — 各进程真实身份写入的能力缓存（IMP-033；CLI 不得冒充）
  - `full-setup-state.json` — Full Profile 安装进度与 `sessionRefreshRequired` 等状态

## 网关与开机自启

- **Caddy 网关生命周期（IMP-010）**：`lwa gateway on/off/status` 管理 Caddy master
  （admin :2019 探活为存活信据）；`lwa manager on` 成功后会联动 `maybe_start_gateway`
  拉起网关。**default** 档失败可降级 builtin 不阻断业务；**Full** 档要求 Caddy 严格可用，不静默降级。
- **daemon 自愈（DEV-042 / IMP-033）**：watcher 启动时与每 `DEFAULT_SUPERVISE_INTERVAL`（60s）
  执行一次 `reconcile()`，恢复 `desired=running` 但状态偏离（stopped/failed/gateway_down
  等）的实例——builtin 静态进程死了重新 spawn、容器走轻量 start。观测失败
  （`permission_denied` / timeout / unknown）或 Full Profile `overall≠ready` 时**跳过容器自动纠正**。
  Caddy 后端且网关被显式关闭（`lwa gateway off`）时跳过 caddy 静态实例，避免与手动停止冲突。
- **开机自启（IMP-030）**：优先 `lwa autostart install [--with-caddy]`（见 [autostart.md](autostart.md)）；
  `lwa setup --autostart` 为兼容旧入口。macOS launchd / Linux·WSL systemd user unit。
  **Windows 原生不受支持**；仅可在 WSL2 内安装自启，Windows 宿主侧只需注册「登录时唤醒 WSL」任务（见 autostart.md）。

### `templates/`、`skills/`

- **templates/**：`lwa init` 复制的 Dockerfile/Compose 模板，可手工定制。
- **skills/**：AI 助手协作用 SKILL.md，描述 import/start/排障等流程。

---

## 容器实例：资源档位与业务密钥

### 资源档位（IMP-018）

scanner 推断的 `resourceProfile` 会映射为 Compose `mem_limit` / `cpus`，写入实例 `docker/.env`（`${MEMORY_LIMIT}` / `${CPU_LIMIT}`）：

| 档位 | 内存 | CPU | 适用 |
| --- | --- | --- | --- |
| `tiny` | 128m | 0.25 | 纯静态/极轻量（静态实例不走容器，映射仅作对称） |
| `small`（默认回退） | 256m | 0.5 | 普通 Web 后端（FastAPI/Flask/Django 基础栈） |
| `medium` | 1g | 1.5 | 含重依赖（lancedb/pyarrow/torch/openai …）或 streamlit/gradio |
| `heavy` | 2g | 3 | 预留高档位（scanner 不自动赋予，可由 skill 手动提升） |

- **自动升档**：scanner 命中重依赖（`lancedb / pyarrow / torch / transformers / tensorflow / openai / anthropic / chromadb / pymilvus`）会自动升 `medium`（只升不降）。
- **改档位**：编辑 `apps/<id>/local-web.json` 的 `resourceProfile` 后 `lwa rebuild <id>` 重新生成 compose（mem_limit 随档位生效）。
- **未知档位**回退 `small`，保证 Compose 始终拿到合法限制。

### 业务密钥与 `.env.local`（IMP-015）

容器实例的 `docker/.env` 由 lwa 生成（端口、`DATABASE_URL`、资源限制等基础设施变量）。**业务密钥**（API key、第三方 token）不要写进 `.env`，改用 `.env.local`：

1. 源码根有 `.env.example` → 导入时自动复制为 `docker/.env.example`（不覆盖）。
2. 按提示把密钥填入 `docker/.env.local`（**缺失不报错**——compose `env_file` 用 `required: false` 可选注入）。
3. compose 启动顺序：`.env`（基础设施）→ `.env.local`（业务密钥，可选）。

```bash
# 导入含 .env.example 的项目后
cp apps/<id>/docker/.env.example apps/<id>/docker/.env.local
$EDITOR apps/<id>/docker/.env.local   # 填 OPENAI_API_KEY 等
lwa restart <id>
```

`.env.local` 不进 git（含密钥），由用户自行保管与备份。

---

## 与端口、访问地址的关系

| 服务 | 默认端口 | 说明 |
| --- | --- | --- |
| 管理页 | 17800 | `local-web.yml` → `managerPort` |
| 路径别名统一入口 | 8080 | `staticGatewayPort`（Caddy 模式；与 managerPort 错开） |
| 实例 | 18000–19999 | `portPool` 内分配，每实例一个 hostPort |
| 实例端口访问 | `http://<LAN-IP>:<hostPort>/` | `lwa start` 后输出的 `lanUrl` |
| 实例路径访问 | `http://<LAN-IP>:<staticGatewayPort>/<alias>/` | 显式启用路径别名时（IMP-006，Caddy 模式） |

每个实例 hostPort **独立**，可同时 running（见 IMP-004）。路径别名（IMP-006 / IMP-014）为**可选**：未设置时行为与 V1 一致，仍只用 hostPort；设置后可通过统一入口 `/<slug>/` 访问，与 hostPort **并存**。**设置别名需要 Caddy**（IMP-022）；容器实例须先 `lwa start` 拿到 hostPort 再 `lwa alias set`。

设置方式：

```bash
# 导入时指定（仅静态/前端；容器请 start 后再设）
lwa import inbox/foo.zip --path-alias my-demo

# 导入后修改（静态或 docker-compose）
lwa alias set <id> new-slug
lwa alias clear <id>

# 或在管理页实例列表 → 操作区「路径别名」
```

详见 [管理页说明](manager-page.md)、[运维手册](operations-playbook.md)。

---

## 本仓库中的 `runtime/` 示例

开发仓内的 `runtime/` 即一个真实工作区，当前可能包含：

- 实例 `demo-static`（18000）
- 实例 `voiceprint-v3-demo` / 声纹管理页面 V3 演示（18001）
- 管理页监听 17800

该目录通常 **不提交 Git**（已在 `.gitignore`），仅作本地运行与演示。

---

## 开发期：lwa 源码更新后如何重载

V0.4.0 起优先运行 `lwa update`。当前实现已包含管理页路径别名在线修改（IMP-006 WBS 006.07~006.10）。`lwa update` 会刷新安装、同步 skills、补齐配置并重启 manager/daemon；**若开机自启单元在管，会走 `coordinated_restart`（监督器 `kickstart -k` / `systemctl restart`），保证单一进程**，勿再手搓 `off && on` 与 KeepAlive 抢锁。**改仓库代码后仅 `pip install -e .` 不够**——管理页、daemon 等后台子进程仍跑旧代码。

```bash
# 推荐：一条命令完成工作区热重载（已与自启协调）
lwa update

# ── 仅当 lwa update 失败时的手动兜底 ──
# 仓库根：刷新 editable 安装
pip install -e .

# 工作区：重启 lwa 自有服务
cd runtime
# 自启在管时：先停用再手搓 off/on，否则 KeepAlive/Restart 会立刻拉回旧进程
lwa autostart disable
lwa manager off && lwa manager on
lwa daemon off && lwa daemon on    # 若启用了 daemon
# 需要继续自启时再：lwa autostart enable

# 可选：托管/import 逻辑变更时重启业务实例
lwa restart <instance-id>

lwa version && lwa doctor
```

AI 助手可参照 Skill **`lwa-update-runtime`**（`lwa init` 后会复制到工作区 `skills/`）。V0.4.0 起优先使用 `lwa update` 自动完成上述步骤；升级后请确认管理页 `/api/health` 的 `version` 与 `lwa version` 输出一致，且实例列表可见「路径别名」按钮。手动命令仅作为排障兜底；细节见 [开机自启](autostart.md)「停服与自启的协调」。

---

## 相关文档

- [README](../README.md) — 安装与命令总览
- [运维手册](operations-playbook.md) — 网关选型、冗余清理、容器别名、浏览量、Caddy 排障
- [管理页说明](manager-page.md)
