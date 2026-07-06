# Local Webpage Access —— 方案讨论稿

> 文件：`docs/discussion/local-web-access-proposal-20260703.md`
> 日期：2026-07-03
> 状态：方案待确认（4 个决策点待拍板）

---

## 一、需求复述

确认一下我对需求的理解，没偏差再看下面的方案。

- 一台局域网 Unix 机器（Ubuntu / WSL2 / macOS），上面托管一堆 AI 生成的小网页。
- 这些网页来源是 zip 包，丢进一个固定目录（"实例库"）。
- 工具要能：
  - 识别类型（纯静态 / Node / Python / 带数据库）；
  - 把它跑起来；
  - 按项目默认端口设计启动端口；
  - 自动检查端口是否与本机已启动端口冲突；
  - 保活。
- 工具本身是"外壳"，真正干活的是一套 **skills**，由在根目录里跑的大模型驱动。
- 另外要一个**管理一览页**，能看到：数量、是否带库、技术栈、端口、入库时间、版本、名字。
- 交付两样：①一套工具；②装好之后的管理一览页。

---

## 二、总体判断：分两层，别让 daemon 变聪明

关于"daemon 自动跑还是大模型驱动"这个纠结点，我的答案是 **两者都要，但分工严格**。这是整个方案最关键的一点。

### 1. 机械层（确定性，不需要 AI）

一个 CLI（暂名 `lwactl`）+ 可选的轻量守护进程，负责它**肯定会做**的事：

- 解压 zip 到实例目录、算 hash、登记入库时间；
- 在固定端口范围里分配端口、检查冲突（既查自己 index，也查机器上实际 `ss -tlnp` / `lsof`）；
- 生成 systemd unit / 反代配置、`systemctl enable --now`、reload 代理；
- 维护 `index.json` 和仪表盘数据。

### 2. 判断层（适应性，skill 驱动）

这层交给大模型，因为 AI 生成的项目结构千变万化，规则永远写不全：

- 这 zip 到底是静态 / Node / Python / 带不带库？入口在哪？默认端口写死在哪？装哪些依赖？健康检查打哪个 path？
- 部署失败了（端口被占、构建报错、缺依赖）怎么诊断怎么修。

### 3. 两层如何衔接（直接回答你的问题）

daemon **不试图变聪明**，它遇到"判断不了"的 zip，就把它标成 `pending` 丢在一边。你在根目录打开大模型时，它一进来自动看到 `AGENTS.md` + 一堆 skill + `pending` 列表，自然就被驱动去处理。**判断完之后，再把结果交给机械层的 CLI 去落地。**

这样 daemon 永远简单可靠，而真正脏的活全在 skill 里，且可读、可改、可复用。这跟"工具只是外壳，skill 来驱动"的思路完全一致，只是把"外壳"再切成"会自动跑的机械部分"和"等大模型来调的判断部分"，避免为了全自动化把一堆 if-else 塞进 daemon。

---

## 三、目录结构（建在本仓库根目录下）

```
local-webpage-access/                  # 固定根目录（即本仓库）
├── AGENTS.md                      # 告诉大模型：这是什么、有哪些 skill、lwactl 怎么用
├── README.md
├── lwactl                         # 主 CLI（Python）
├── lwactl_lib/                    # CLI 实现
├── skills/                        # 判断层：ZCode skills
│   ├── lwa-detect/                # 识别技术栈 + 入口 + 端口 + 是否带库
│   ├── lwa-deploy/                # 编排完整部署（detect→分端口→装依赖→起服务→写 index）
│   ├── lwa-port/                  # 端口分配 / 冲突检查
│   ├── lwa-lifecycle/             # start / stop / restart / remove
│   └── lwa-troubleshoot/          # 部署/运行失败时诊断修复
├── templates/                     # 可填充模板（机械层用）
│   ├── systemd-node.service.tmpl
│   ├── systemd-python.service.tmpl
│   ├── systemd-static.service.tmpl
│   ├── caddy-site.conf.tmpl
│   ├── manifest.schema.json
│   └── meta.json.example
├── inbox/                         # 丢 zip 的入口
├── instances/                     # 实例库：每个已部署项目一个目录
│   ├── index.json                 # 所有实例汇总索引（仪表盘数据源）
│   └── <slug>/
│       ├── manifest.json          # 该实例的全部元数据 + 运行信息
│       ├── src/                   # 解压出的源码
│       ├── runtime/               # node_modules / .venv（每实例隔离）
│       ├── logs/
│       └── state/                 # sqlite 等持久化数据
├── dashboard/                     # 管理一览页
│   ├── server.py                  # 读 index.json 的轻后端
│   └── public/                    # 单页前端
└── config/
    ├── lwactl.toml                # 端口范围、代理开关、路径
    └── ports.json                 # 已分配端口表（防冲突的真相源之一）
```

### 几个关键设计点

- **`inbox/` 和 `instances/` 分开**：zip 先进 inbox，部署成功后才"毕业"进 instances。失败的、待处理的不会污染实例库，仪表盘也干净。你说的"实例库最好带个 index"，`instances/index.json` 就是那个 index。
- **每个实例的 `node_modules` / `.venv` 放在各自目录下**，互不污染，删一个实例 = 删一个目录。
- **持久化数据放 `state/`**：带库的项目（sqlite 等）数据在这里，升级版本时不会丢。

---

## 四、核心数据结构：manifest.json

每个实例一份，仪表盘基本就是把它平铺成表格：

```json
{
  "slug": "todo-app",
  "name": "Todo App",
  "version": "1.2.0",
  "source": { "zip": "todo-app-1.2.0.zip", "sha256": "...", "ingested_at": "2026-07-03T10:15:00+08:00" },

  "stack": {
    "category": "node",                 // static | node | python | container
    "has_database": true,
    "db": { "type": "sqlite", "engine": "better-sqlite3" },
    "frameworks": ["vite", "express"],
    "runtime": "node@20",
    "language": "typescript"
  },

  "runtime": {
    "port": 8123,
    "extra_ports": [],
    "workdir": "src",
    "install_command": "npm ci",
    "build_command": "npm run build",
    "start_command": "npm run start",
    "env": { "NODE_ENV": "production" },
    "health": { "path": "/api/health", "expect": 200 }
  },

  "supervisor": {
    "kind": "docker",                      // docker | caddy-file（分层方案，见 11.4/11.8.4）
    "container": "lwa-todo-app",           // 静态类无此字段，改用 root
    "restart_policy": "unless-stopped",    // 配合 desired_state，见 11.9.2
    "enabled": true
  },
  "proxy":     { "enabled": true, "route": "/todo-app", "upstream": "http://lwa-todo-app:3000" },

  "desired_state": "running",              // 用户想要的状态：running | stopped（开关基础，见 11.9）
  "status": { "phase": "running", "last_checked": "2026-07-03T10:20:00+08:00", "pid": 12345 }
}
```

这一份就覆盖了你列的全部 6 条信息（名字、版本、入库时间、是否带库、技术栈、端口），还附带运行状态、健康检查、反代路由、`desired_state`——一键启动/停止/查日志/开关都靠它。

> **静态站示例（`supervisor` 段差异）**：`kind` 为 `caddy-file`，无 `container`，多一个 `root` 字段指向 `instances/<slug>/src/`；`runtime` 段无 `install_command` / `build_command` / 容器内端口。其余字段（`desired_state`、`status`、`proxy`）与容器类一致。

---

## 五、Skills 清单（判断层）

| Skill | 触发场景 | 产出 |
|---|---|---|
| `lwa-detect` | "看看这个 zip 是什么类型的项目" | 填好 manifest 的 `stack` + `runtime` 字段（最核心、最脏的活） |
| `lwa-deploy` | "把这个新站部署起来" | 编排：detect → 分端口 → 装依赖 → 起 systemd → 写反代 → 更新 index |
| `lwa-port` | "给它分个端口，别冲突" | 在配置范围内选空闲端口，查 index + 实际占用 |
| `lwa-lifecycle` | "停掉/重启/删掉 xxx" | 调 `lwactl stop / restart / remove` |
| `lwa-troubleshoot` | "xxx 起不来 / 502" | 看日志、验端口、试健康检查，给出修法 |

每个 skill 内部都内置"常规业务流程"，并调用 `templates/` 里可填充的模板和 `lwactl` 子命令来落地。

---

## 六、一次部署的完整流程

1. 把 `todo-app-1.2.0.zip` 扔进 `inbox/`。
2. （可选）守护进程检测到新 zip，先做机械部分：解压、算 hash、登记 `ingested_at`，状态置 `pending`。
3. 在根目录开大模型，它看到 `AGENTS.md` + skills + `pending` 列表，触发 `lwa-deploy`。
4. `lwa-deploy` 先调 `lwa-detect`：读 `package.json` / `requirements.txt` / `docker-compose.yml`，判定 `category=node, has_database=true, port 想用 3000`。
5. 调 `lwa-port`：3000 被占 → 自动改派 8123。
6. 调 `lwactl install`：`npm ci` → `npm run build` → 填 systemd 模板 → `systemctl enable --now` → 生成 Caddy 路由 → reload。
7. 健康检查通过 → manifest 状态置 `running`，写进 `index.json`，实例"毕业"到 `instances/todo-app/`。
8. 仪表盘立刻多出一行。

---

## 七、端口管理

- `config/lwactl.toml` 里定一个范围，比如 `8100–8499`，专留给本工具。
- 分配时**双重检查**：① `ports.json` 里已分配的；② 机器实际监听的（`ss -tlnp` / `lsof -i`）。任一冲突就换下一个。
- 项目自己声明的端口（package.json scripts、app.py 里的 `port=`）只是"建议"，最终以分配到的为准，并写回启动命令 / 环境变量。

---

## 八、进程托管 & 反向代理（我的推荐）

- **进程托管：systemd 为主。** Ubuntu / WSL2 都支持（WSL 需开启 systemd），每个实例一个 unit，原生保活、开机自启、`journalctl` 看日志，零额外依赖。**macOS 上 skill 自动切到 launchd**（同一套 manifest，换模板）。不建议一上来用 PM2 或 Docker：PM2 多一层运行时依赖，Docker 对"AI 生成的小网页"过重且每个项目都要写 compose。
- **反向代理：Caddy。** 比 nginx 配置短一个数量级，reload 安全，LAN 里访问 `http://机器/todo-app` 比记一堆端口舒服，仪表盘也挂在根路径 `/`。不想用反代也行，`lwactl` 支持关掉 proxy 直接用端口访问。

---

## 九、管理一览页

单页应用，后端就一个读 `index.json`（必要时合并实时 `systemctl is-active`）的轻服务。表格列正好是那 6 条 + 运行状态：

| 名字 | 版本 | 类型 | 带库 | 技术栈 | 端口 | 路由 | 入库时间 | 状态 | 操作 |
|---|---|---|---|---|---|---|---|---|---|
| Todo App | 1.2.0 | node | ✅ sqlite | vite, express | 8123 | /todo-app | 07-03 10:15 | 🟢 running | 启停·日志·打开·删除 |

再加几个实用增强：按类型 / 带库筛选、总数统计卡、"打开站点"按钮（跳反代路由或端口）、点状态看最近日志、`pending / error` 高亮提醒你去根目录跑大模型处理。

---

## 十、待拍板的决策点

下面 4 个点我已给出推荐，定了就可以出详细实现计划。

| # | 决策点 | 推荐 | 备选 |
|---|---|---|---|
| 1 | 核心语言 | **Python 3**（几乎必装，胶水活顺手，仪表盘同栈） | Node（生态一致） |
| 2 | 进程托管 | **systemd（macOS 降级 launchd）** | PM2 / Docker |
| 3 | 反向代理 | **Caddy**（配置短、LAN 体验好） | nginx / 不上反代 |
| 4 | 自动化程度 | **混合**（daemon 做机械部分 + 标 pending，判断与落地走 skill） | 全自动 daemon / 纯手动 |

整体方案的核心是**"分两层"**——daemon 永远只做确定性机械活，判断与适配交给 skill 驱动的大模型。这点确认后，其余 4 项按上表推荐即可推进到详细实现计划。

---

## 十一、Docker 路线修订（2026-07-03）

> 本节是讨论的演进记录，**不覆盖**前文，而是修正第十节里的"进程托管"决策。前文的目录结构、manifest、skills、流程在 Docker 路线下仍然成立，仅在"托管"和"端口"两处有显著简化（文末给出对照）。

### 11.0 结论先行

**基本是的——对我们的场景，Docker 是更好的默认选择。** 唯一值得单独讨论的特例是纯静态 HTML 那一类（见 11.4）。下面逐条论证，论证针对的是"这套架构"具体在哪些点上变简单，而不是 Docker 的通用优点。

### 11.1 Docker 真正帮到我们的地方（针对这套架构）

**(1) 机械层从"三套模板"塌缩成"一条流水线"。**
第八节最大的复杂度藏在 `systemd-node / python / static` 三套模板里：每种类型 ExecStart 不一样、WorkingDirectory 不一样、venv 怎么激活、node_modules 放哪……这堆"按类型分叉"的机械逻辑，在 Docker 下全部消失——`lwactl` 只剩一件事：`docker build` + `docker run`。类型差异被推进了 Dockerfile 里，而 **Dockerfile 恰好是大模型最擅长生成的那类产物**（标准格式、可读、可审查）。这与"分两层"的思路是天作合：判断层产出一个 Dockerfile，机械层傻瓜式执行。

**(2) macOS 的特殊处理整个消失。**
第八节我写了"systemd 为主，macOS 上 skill 自动切 launchd"——这是一笔一直让我别扭的复杂度（同一套 manifest 要服务两套 supervisor 模板）。Docker 在 Ubuntu / WSL2 / macOS 上**是同一套东西**，这个分支彻底不用写了。仅此一点就值得换。

**(3) 端口冲突问题几乎被消灭——这是意外的大红包。**
如果 Caddy 和所有应用容器挂在**同一个 Docker 网络**上，Caddy 直接用容器名反代（`http://todo-app:3000`），应用容器**根本不需要把端口发布到宿主机**。于是全机器只有一个端口要保证空闲：Caddy 的 `:80`。第七节那一整套"端口范围 8100–8499、双重查 index + `ss -tlnp`"可以砍掉一大半，只剩"给想直连的应用选一个宿主端口"这种可选功能。架构一下子干净很多。

**(4) 隔离是免费送的。**
两个项目一个要 Node 18 一个要 Node 20、一个用 Python 3.10 一个用 3.12、一个要 Postgres 一个要 Redis——原生环境下这是配置地狱，Docker 下天经地义。对"AI 生成的项目版本飘忽"这个场景，这点比想象的值钱。

### 11.2 但要说清楚：检测问题没有消失，只是变了形

这点别被"Docker 全包"骗了。判断 zip 到底是 Vite（要 `npm run build` 再用 nginx serve `dist/`）还是 Express（直接 `node server.js`）、入口文件是哪个、健康检查打哪——**这套 `lwa-detect` 的脏活还在**。区别只是它的产出从"一份填满魔法命令的 manifest.json"变成了"一个 Dockerfile / docker-compose.yml"。后者是更标准、更好审、更好 diff 的产物，这是质量提升，不是省事。

换句话说，Docker 把判断层从"猜野生项目的启动方式"升级成了"生成标准的构建/运行描述"。判断层仍然承重，但产物更规范、更可维护。

### 11.3 几个要盯着的坑

- **Dockerfile 生成成了承重的技能。** 它写错就构建失败。但这正是 `lwa-troubleshoot` 的活，而且 LLM 改 Dockerfile 比反推"这个野生的 Express 到底想怎么启动"要顺手。
- **带数据库的一定要用 named volume，不能放进容器内存储。** 否则容器一重建数据全没。这个规则要硬写进 skill 模板里（属于判断层产出 Dockerfile 时的硬约束）。
- **镜像体积。** 20 个 Node 项目 ≈ 20GB。LAN 机器一般扛得住，但 skill 应该默认走 multi-stage + slim/alpine 基础镜像。
- **构建时间。** 与原生跑 `npm ci` 是同一个成本，不是额外开销。
- **WSL2 的 Docker Desktop 依赖。** Windows 侧要装 Docker Desktop；纯 Ubuntu / macOS 则原生。属于一次性环境前置，不进入工具本身的复杂度。

### 11.4 纯静态 HTML 怎么办？（分层方案）

> 本节的推荐在 **11.8（资源占用）讨论后被修订**：原"统一进 Docker"的路线改为**分层方案**。本节保留两路线对比，最终推荐见 11.8 表格。

一个 `index.html` 怎么托管，有两条路：

- **统一路线**：静态也进 Docker（nginx/alpine 容器伺候），代价是多几 MB 镜像；好处是仪表盘、生命周期、反代规则对所有实例完全一致，`lwactl` 不用写 `if static`。
- **分层路线（推荐，见 11.8 修订）**：静态站点直接由 Caddy 的 `file_server` 伺候（Caddy 已经开着），**零额外容器、~0 内存**；Node / Python / 带库的进 Docker。

为什么改推荐分层：纯静态站恰恰是"原生明显更轻"的那一类特例。你会有很多纯静态小网页，每个塞进 nginx 容器都要多一个容器、多一份镜像，数量一多差距累积明显。而静态/非静态只分两支，远比 11.1(1) 里说的 node/python/static 三套 systemd 模板简单——非静态那一支仍然是统一的 `docker build/run`。

**最终选择：分层方案**——静态走 Caddy 直服，其余容器化。详见 11.8 表格。

### 11.5 决策点更新

这一问实际是在改第十节的 **决策点 #2（进程托管）**。其余三项不受影响，且 Caddy + Docker 是绝配（11.1(3) 正依赖于此）。

| # | 决策点 | 原推荐（第十节） | **修订后** |
|---|---|---|---|
| 1 | 核心语言 | Python 3 | Python 3（不变） |
| 2 | 进程托管 | systemd（macOS 降级 launchd） | **Docker：Caddy + 应用容器共享同一 Docker 网络** |
| 3 | 反向代理 | Caddy | Caddy（不变，且与 Docker 配合后更简化） |
| 4 | 自动化程度 | 混合（daemon 机械 + skill 判断） | 混合（不变） |

### 11.6 对前文两个章节的具体影响（修订清单）

为避免前后文打架，明确标出 Docker 路线下哪些章节需简化：

- **第七节「端口管理」大幅简化**：
  - 不再有"端口范围 8100–8499 + 双重检查"的默认机制；默认是"不发布宿主端口，走 Caddy 容器名反代"。
  - `config/ports.json` 退化为"可选直连端口登记表"——只给少数需要宿主直连（如调试）的实例分配。
  - 唯一需要保证空闲的宿主端口是 Caddy 的 `:80`（及可选的 `:443`、仪表盘端口）。
- **第八节「进程托管 & 反向代理」**：
  - "进程托管"段落改为：Caddy + 应用全部容器化，挂在同一 `lwa-net` Docker 网络上；不再有 systemd / launchd / PM2。
  - Caddy 段落：反代上游从 `http://127.0.0.1:<port>` 改为 `http://<container-name>:<内部端口>`。
- **第四节 manifest** 调整多处字段语义（含分层方案对静态站的处理，及 11.9 新增的 `desired_state`）：
  - `runtime.port` → 记录**容器内端口**（应用监听的那一个）。
  - 新增 `runtime.host_port`（可选，留空表示不发布宿主端口）。
  - `supervisor.kind` 改为枚举：
    - `docker`（Node / Python / 带库类，`container` 字段记录容器名）；
    - `caddy-file`（纯静态类，`root` 字段记录实例 `src/` 的绝对路径，由 Caddy `file_server` 直服，无容器）。
  - `runtime.workdir` 含义不变；`install_command` / `build_command` 不再在宿主机执行，而是写进 Dockerfile（容器类）；静态类无此字段。
  - **新增 `desired_state`**（取值 `running` / `stopped`，见 11.9）：持久化"用户想要的状态"，与"实际状态"分开，是开关功能的基础。容器类用 `--restart=unless-stopped` 配合；静态类用 Caddy 路由的注释/移除来模拟。

### 11.7 修订后的部署流程（对应第六节）

**容器类（Node / Python / 带库）：**

1. 把 `todo-app-1.2.0.zip` 扔进 `inbox/`。
2. （可选）守护进程做机械部分：解压、算 hash、登记 `ingested_at`，状态置 `pending`。
3. 在根目录开大模型，看到 `AGENTS.md` + skills + `pending`，触发 `lwa-deploy`。
4. `lwa-deploy` 调 `lwa-detect`：读 `package.json` 等，判定 `category=node, has_database=true`，**产出一个 Dockerfile**（multi-stage，依赖装进镜像，DB 用 named volume）。
5. 调 `lwactl install`：`docker build` → `docker run -d --network lwa-net --restart unless-stopped --name lwa-todo-app`（不发布宿主端口）→ 生成 Caddy 路由 `http://lwa-todo-app:3000` → reload Caddy。
6. 健康检查（从 Caddy 侧或容器内打）通过 → manifest 状态置 `running`、`desired_state=running`，写进 `index.json`，实例"毕业"到 `instances/todo-app/`。
7. 仪表盘立刻多出一行。

**静态类（纯 HTML）：**

1–4. 同上，但 `lwa-detect` 判定 `category=static`，**不产出 Dockerfile**，manifest 里 `supervisor.kind=caddy-file`、`root=<实例 src 绝对路径>`。
5'. 调 `lwactl install`：在 Caddyfile 追加一条 `file_server` 站点块（root 指向 `instances/<slug>/src/`）→ reload Caddy。
6'. 健康检查（HTTP 200）通过 → 同样置 `running`、`desired_state=running`，写进 `index.json`。

注意：与第六节相比，步骤里"分端口"被删掉了，"装依赖/起 systemd"被"build + run"或"加 Caddy 块"取代。流程更短、更线性。

### 11.8 资源占用与 4G/8G 小机器的可行性

讨论中提出的核心顾虑："我的机器就是 4G 或 8G 的小主机，跑得动 Docker 吗？" 结论：**4G 能跑，但需要开 swap；8G 非常宽裕。** 论证如下。

#### 11.8.1 关键澄清：Docker 开销是"固定 daemon"，不是"每容器一份"

很多人以为"每个容器都像小虚拟机一样有额外开销"，**这是错的**。容器就是带边界的普通进程，CPU/内存开销几乎为零。Docker 的真实成本结构是：

- **`dockerd` + `containerd` 守护进程**：固定 ~100–150MB RSS，**不管跑 1 个还是 50 个容器都这么多**。
- **每个容器本身**：约等于它里面那个程序的开销。nginx 伺候静态站 ~5–10MB，Node Express ~80–150MB。容器化**不**让它变重。
- 对比原生 systemd：省下的就是那 ~100–150MB daemon 钱，app 自己该吃多少还是多少。

所以"Docker 太重"在内存维度其实是：**为整台机器多付一次固定的 ~150MB，换掉所有按类型分叉的复杂度。**

#### 11.8.2 一台 4G 机器的内存预算

| 项目 | 占用 |
|---|---|
| Ubuntu Server（无桌面） | ~400MB |
| Docker daemon（固定） | ~150MB |
| Caddy | ~30MB |
| **固定开销合计** | **~580MB** |
| 剩余给所有 app | **~3.4GB** |

一个小 Node app 跑起来 ~100MB，3.4GB 够**同时跑二十几个**才到顶。实际上"一堆小网页"平时不会同时全开着——Docker 反而比 systemd 更适合"按需启停"，停掉的容器是**零**内存。8G 机器就更加宽裕。

**真正会吃内存的不是 Docker，是同时运行的应用本身**——这点容器化和原生一模一样。

#### 11.8.3 真正要小心的两个点（非 Docker 本身的锅）

**1. 构建时的瞬时尖峰。** `npm ci && npm run build` 一个 Vite 项目，可能瞬间冲到 800MB–1.5GB。这是 build 工具的脾气，原生跑也一样。**对策：开 2–4GB swap。** 在 4G 机器上务必配 swap，否则大项目构建可能 OOM。这是一行 `fallocate` + `swapon` 的事，应列为安装前置。

**2. 磁盘，不是内存。** 20 个 Node 镜像（multi-stage + slim 之后）≈ 5–10GB。比原生（共享全局 node_modules）确实更费盘。对策：skill 默认用 `node:XX-slim` / `python:XX-slim` + multi-stage，单镜像压到 150–300MB；带库的再算上数据卷。LAN 小主机若 64GB+ SSD/TF 卡够用；只有 32GB 得盯紧。静态站走 Caddy 直服（不分容器）正是省盘的关键（见 11.8.4）。

#### 11.8.4 最终修订：静态站改走分层方案

结合 11.8.1–11.8.3，**11.4 的推荐从"统一进 Docker"修订为分层方案**。纯静态站恰恰是"原生明显更轻"的特例——Caddy `file_server` 直服零额外容器、~0 内存、零镜像，数量多时省得明显。

| 应用类型 | 托管方式 | 理由 |
|---|---|---|
| 纯静态 HTML | **Caddy `file_server` 直服**（不进容器） | 最轻，Caddy 已在；数量多时省得明显 |
| Node / Python 应用 | Docker 容器 | 受益于运行时版本隔离，容器开销可忽略 |
| 带数据库的复杂应用 | Docker + named volume | 必须容器化以隔离数据和依赖 |

代价：机械层 `lwactl` 又得写一次"按类型分叉"。但只分静态/非静态两支，远比原 node/python/static 三套 systemd 模板简单——非静态那一支仍是统一的 `docker build/run`。

**机器规格对应建议：**

- **4G 机器**：能跑 Docker，开 swap；用分层方案；不用的 app 用开关（见 11.9）停掉省内存。
- **8G 机器**：非常宽裕；同样用分层方案；可更宽松地同时开多个 app。

不需要考虑 Podman 之类的替代——它们的"更轻"主要省在 rootless/daemonless 的运维差异上，对本场景没有实质收益，反而引入兼容性摩擦。

### 11.9 开关控制 + 资源监控

讨论中新增两个需求：①实例开关（管理页拨一下，启动/停止实例）；②资源监控（看内存/资源占用）。两者高度互补——开关直接服务"省内存"，监控让你"看得见余量"。实现分阶段。

#### 11.9.1 开关控制：先分清两个层次

**（A）实例级开关**——每个 app 一个开关，开启=运行，关闭=停掉省内存。**最有用、最该先做。** 即 `lwa-lifecycle` skill 的 start/stop，现在暴露成管理页按钮，并把状态写进 manifest 的 `desired_state`。

**（B）总闸**——一键停掉全部 / 停掉 Docker daemon。场景：机器要干别的事 / 要重启 / 出门省电。锦上添花，放后面。

**建议：实例级先做（Phase 1），总闸后做（Phase 3）。**

#### 11.9.2 开关最关键的坑：desired state + restart policy

**若用 Docker 默认的 `--restart=always`，机器一重启，手动关掉的容器会自己又起来**——与"我想让它关着省内存"的意图完全冲突。正确做法两件事，缺一不可：

1. **manifest 里加 `desired_state` 字段**（`running` / `stopped`），持久化"用户想要的状态"，与"容器实际状态"分开。开关拨一下就是改这个字段，再由 `lwactl` 执行 stop/start。
2. **容器用 `--restart=unless-stopped`** 而非 `always`。这个策略正好是想要的语义：**用户手动 stop 的，重启后不再自启；没手动停的，重启后恢复**。零额外逻辑解决冲突。

另外明确：开关关闭 = **`docker stop`**（容器配置全保留，再开是秒级），**不是** `docker rm`（要重新 run）。

静态站（Caddy 直服）的开关：没有容器可停，开关的作用是 **disable/enable 那条 Caddy 路由**（注释/移除后 reload），效果一样——访问不到、不再占资源。

#### 11.9.3 资源监控：分两层，用最轻方式

先想清楚"监控谁"，因为成本差很多：

- **整机总览**（CPU/内存/磁盘/负载还剩多少）——**对 4G 小机器最关键**，回答"我还能不能再塞一个站"。
- **每容器细分**（哪个 app 是大户）——回答"谁在吃内存、要不要关掉它"，锦上添花。

实现上**千万别上 cAdvisor / Prometheus 那套**（本身就是几百 MB 的内存大户，把要省的资源吃了）。正确做法是 CLI 自己采，几乎零成本：

- 每容器：`docker stats --no-stream --format json`，**一次调用拿全部容器**的 CPU/内存/网络，非常便宜。
- 整机：读 `/proc/meminfo`、`/proc/loadavg`、`df`，纯文本解析。

刷新策略：**按需拉取 + 低频轮询（10–30 秒），不做实时推送**。实时 WebSocket 流对小机器是额外负担，盯仪表盘本来就不是为了看实时跳动。打开页面拉一次、挂着就低频轮询，足够。

一个意外红利：监控能发现**11.8.3 提过的"构建时 OOM 尖峰"**。可顺手记一个"最近一次构建的峰值内存"，超了提示加 swap（Phase 3）。

#### 11.9.4 分阶段实施计划

| 阶段 | 内容 | 理由 |
|---|---|---|
| **Phase 1** | 实例级开关（`desired_state` + `unless-stopped` + 管理页开关按钮 + 静态站路由 enable/disable） | 直接服务"省内存"核心诉求，成本最低、价值最高 |
| **Phase 2** | 资源监控（整机总览 + 每容器 `docker stats`，按需拉取/低频轮询） | 看得见 4G 机器的余量，决定能不能再塞站 |
| **Phase 3** | 总闸、构建峰值监控、开关日志/审计 | 锦上添花 |

Phase 1 几乎不引入新组件（就是 lifecycle skill 的前端化 + 一个字段），Phase 2 才需要仪表盘后端加采集接口。与"一步一步做"的节奏完全吻合。

---

## 十二、待你确认（最终决策清单）

整体方案的核心仍是**"分两层"**（机械层 = `lwactl` + 守护进程；判断层 = skill 驱动的大模型）。经多轮讨论，机械层定型为 **Docker（Caddy + 应用容器共享 `lwa-net` 网络）+ 静态站 Caddy 直服** 的分层方案；判断层产出 Dockerfile（容器类）或 Caddyfile 块（静态类）。开关与监控作为配套，分三阶段实施。

请确认以下 6 项，我即可出详细实现计划（具体到文件、模板内容、`lwactl` 子命令清单）：

| # | 决策点 | 最终推荐 | 出处 |
|---|---|---|---|
| 1 | 核心语言 | **Python 3** | 第十节 |
| 2 | 进程托管 | **分层**：静态走 Caddy `file_server` 直服；Node/Python/带库走 Docker（`--network lwa-net` + `--restart unless-stopped`） | 11.5 + 11.8.4 |
| 3 | 反向代理 | **Caddy**（容器名反代 `http://<container>:<port>`，静态站 `file_server`） | 11.5 |
| 4 | 自动化程度 | **混合**（daemon 做机械部分 + 标 pending，判断与落地走 skill） | 第十节 |
| 5 | 资源前提 | **4G 机器需开 2–4GB swap**；skill 默认 multi-stage + slim 镜像控盘 | 11.8.3 |
| 6 | 开关与监控 | **Phase 1 实例级开关**（`desired_state` + `unless-stopped`）→ **Phase 2 资源监控**（`docker stats` + `/proc`，低频轮询）→ Phase 3 总闸/审计 | 11.9 |

**额外需你拍板的细节**（若同意上表推荐，下面默认采纳）：

- a. 开关关闭 = `docker stop`（保留配置，秒级再开），**非** `docker rm`。—— 11.9.2
- b. 监控**不上 cAdvisor/Prometheus**，由 `lwactl` 自己采，按需拉取 + 10–30s 低频轮询。—— 11.9.3

确认后推进到详细实现计划。
