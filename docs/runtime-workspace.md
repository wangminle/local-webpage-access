# Runtime 工作区说明

`lwa init` 会在你指定的目录（例如本仓库下的 `runtime/`）创建 **Runtime 工作区**——这是 lwa **实际运行时的项目文件夹**，所有导入的 zip、实例数据、日志、端口登记与管理页状态都落在这里。

> CLI 会自动向上查找包含 `local-web.yml` 的目录作为当前工作区。在 Runtime 目录内执行 `lwa` 命令即可，无需每次加 `-w`。

## 快速上手

```bash
cd runtime          # 进入工作区（示例路径）
lwa setup           # 可选：检查宿主机工具
lwa init            # 首次：生成目录与配置（已 init 可跳过）
lwa manager on      # 管理页（默认 init 后自动拉起，端口 17800）
lwa import inbox/xxx.zip --name my-app
lwa start my-app
lwa list
```

管理页：本机 http://127.0.0.1:17800/（本机免 token）；局域网 http://\<LAN-IP\>:17800/（需 token）。

---

## 顶层目录一览

```
runtime/                      ← 工作区根（Runtime 根目录）
├── local-web.yml             ← 全局配置（端口池、管理页、静态网关等）
├── inbox/                    ← 待导入 zip 投放区（daemon 也会监听）
├── apps/                     ← 已导入实例（每个子目录一个实例）
├── registry/                 ← SQLite 全局索引（local-web.db）
├── static-gateway/           ← 静态网关配置（Caddy 站点片段等）
├── logs/                     ← 工作区级日志（如 daemon.log）
├── run/                      ← 运行态文件（管理页 token、daemon/manager 状态）
├── templates/                ← 用户可编辑的 Dockerfile/Compose 模板副本
└── skills/                   ← 从包内复制的大模型 Skill 文档
```

### `inbox/`

- **用途**：放置待导入的 `.zip`；`lwa import inbox/foo.zip` 或 daemon 自动导入。
- **注意**：zip 内若含 `node_modules` 等大目录，导入时会 **自动剥离**可重建依赖；仍保留 zip slip / 符号链接安全审计。

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

### `static-gateway/`

- **用途**：共享静态网关配置。
- **Caddy 模式**：`sites/<id>.conf` 按实例生成；`aliases/<id>.conf` 为路径别名路由片段（IMP-006），由主 Caddyfile 在 `staticGatewayPort` 统一入口 import。
- **builtin 模式**：每个静态实例仍占独立 hostPort，由内置 `http.server` 子进程服务；**不支持**路径别名统一入口（别名可登记，仅 hostPort 可达）。

### `logs/`

- **用途**：工作区级日志，例如 `daemon.log`（inbox 自动导入守护进程）。

### `run/`

- **用途**：运行态元数据，例如：
  - `manager-token.json` — 管理页 API token
  - `manager.json` — 管理页后台进程状态
  - `daemon.json` / `daemon.lock` — daemon 开关与 watcher 锁

### `templates/`、`skills/`

- **templates/**：`lwa init` 复制的 Dockerfile/Compose 模板，可手工定制。
- **skills/**：AI 助手协作用 SKILL.md，描述 import/start/排障等流程。

---

## 与端口、访问地址的关系

| 服务 | 默认端口 | 说明 |
| --- | --- | --- |
| 管理页 | 17800 | `local-web.yml` → `managerPort` |
| 路径别名统一入口 | 8080 | `staticGatewayPort`（Caddy 模式；与 managerPort 错开） |
| 实例 | 18000–19999 | `portPool` 内分配，每实例一个 hostPort |
| 实例端口访问 | `http://<LAN-IP>:<hostPort>/` | `lwa start` 后输出的 `lanUrl` |
| 实例路径访问 | `http://<LAN-IP>:<staticGatewayPort>/<alias>/` | 显式启用路径别名时（IMP-006，Caddy 模式） |

每个实例 hostPort **独立**，可同时 running（见 IMP-004）。路径别名（IMP-006，当前版本管理页已支持在线修改）为**可选**：未设置时行为与 V1 一致，仍只用 hostPort；设置后可通过统一入口 `/<slug>/` 访问，与 hostPort **并存**。

设置方式：

```bash
# 导入时指定
lwa import inbox/foo.zip --path-alias my-demo

# 导入后修改
lwa alias set my-demo new-slug
lwa alias clear my-demo

# 或在管理页实例列表 → 操作区「路径别名」
```

详见 [管理页说明](manager-page.md)。

---

## 本仓库中的 `runtime/` 示例

开发仓内的 `runtime/` 即一个真实工作区，当前可能包含：

- 实例 `demo-static`（18000）
- 实例 `voiceprint-v3-demo` / 声纹管理页面 V3 演示（18001）
- 管理页监听 17800

该目录通常 **不提交 Git**（已在 `.gitignore`），仅作本地运行与演示。

---

## 开发期：lwa 源码更新后如何重载

V0.4.0 起优先运行 `lwa update`。当前实现已包含管理页路径别名在线修改（IMP-006 WBS 006.07~006.10）。`lwa update` 会刷新安装、同步 skills、补齐配置并重启 manager/daemon。**改仓库代码后仅 `pip install -e .` 不够**——管理页、daemon 等后台子进程仍跑旧代码；下面命令仅作为手动兜底：

```bash
# 推荐：一条命令完成工作区热重载
lwa update

# 仓库根：刷新 editable 安装
pip install -e .

# 工作区：重启 lwa 自有服务
cd runtime
lwa manager off && lwa manager on
lwa daemon off && lwa daemon on    # 若启用了 daemon

# 可选：托管/import 逻辑变更时重启业务实例
lwa restart <instance-id>

lwa version && lwa doctor
```

AI 助手可参照 Skill **`lwa-update-runtime`**（`lwa init` 后会复制到工作区 `skills/`）。V0.4.0 起优先使用 `lwa update` 自动完成上述步骤；升级后请确认管理页 `/api/health` 的 `version` 与 `lwa version` 输出一致，且实例列表可见「路径别名」按钮。手动命令仅作为排障兜底。

---

## 相关文档

- [README](../README.md) — 安装与命令总览
- [管理页说明](manager-page.md)
