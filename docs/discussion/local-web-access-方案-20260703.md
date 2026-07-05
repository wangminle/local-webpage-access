# Local Web Access 工具方案讨论（Docker-first 版）

> 日期：2026-07-03
> 状态：方案讨论稿（Docker-first + Traefik 按名字路由，含关键决策确认）

## 一、背景与目标

Web coding 过程中会不断产生各种"小网页"，形态各异：

1. **纯 HTML**：直接双击/托管即可用，无任何依赖。
2. **JS/Node 架构**：需要安装 npm 包才能运行。
3. **Python 架构**：带本地数据库等，前后端都较复杂（也有 JS 后端复杂的情况）。

希望有一台局域网机器（**主要是 Ubuntu / Windows WSL**）承载这些网页。目标是做一套工具，实现：

- **输入方式**：把网页打包成 zip，扔进一个固定目录（"实例库"），目录附带一个 index。
- **核心功能**：自动识别 zip 是纯 HTML / JS / Python 架构，把它跑起来；处理端口/路由；避免冲突。
- **形态**：工具本质是"一套 skills + 一层外壳"。skills 内置常规业务流程，可跑可填充的脚本把各端跑起来；在固定根目录下用大模型运行时能自动识别并调用这些 skills。
- **管理页**：一个本地管理一览页，一目了然看到：已安装多少个 local web page、哪些带数据库/哪些纯前端、技术栈、访问地址/端口、放入时间与版本号、名字。

最终交付两套东西：**(1) 一套工具；(2) 一套安装后的管理一览页。**

---

## 二、核心结论：Docker-first 的"混合驱动"架构

### 2.1 混合驱动（不变的骨架）

关于"自动检测还是大模型驱动"——结论是**两者都要，各管一段**：

- **识别 + 生成配方（一次性、易变、需要判断）→ 大模型 + skills。**
  每个 zip 结构都不同，判断类型、依赖、启动方式、监听端口，这类"脏活"交给 LLM。**产出物是一份 `manifest.json`（运行配方），并由它编译出 `Dockerfile` + `docker-compose.yml`。**

- **运行 + 保活（长期、稳定、无人值守）→ 确定性脚本 + Docker。**
  服务保活靠 Docker `restart: unless-stopped`，重启/崩溃自动拉起。这一层只读 manifest / compose 照着执行，不依赖大模型在线。

> 一句话：**大模型把未知 zip 翻译成一份标准配方（→ compose），Docker 照配方稳定地跑。`manifest.json` 是两层之间的合同。**

### 2.2 为什么改用 Docker-first

- **隔离**：每个网页自带环境，彻底告别宿主机上 venv / node_modules / Python 版本互相打架（最大价值）。
- **保活**：`restart: unless-stopped` 直接替代 systemd 保活，还自带资源限额。
- **一致性 & 干净卸载**：Ubuntu / WSL 上行为一致，`docker compose down` 即彻底清除，无残留。
- **端口冲突问题几乎消失**：见 2.3。

> 注意：Docker 换掉的只是"运行/保活层"，前面的识别、管理页、配方逻辑都不变。它只是把"配方的编译目标"从"install/run 命令 + systemd unit"换成"Dockerfile + compose"。

### 2.3 Traefik 按名字路由（关键收益）

引入一个**共享反向代理 Traefik**，配合 Docker labels 自动发现容器：

- 各实例容器**不再各自映射宿主机端口**，只挂到同一个 Docker 网络，Traefik 通过 label 自动路由。
- 因此**"宿主机端口冲突"问题基本自动消失**，对外只剩 Traefik 的 `:80` 和 Hub 管理页。
- 局域网访问用 **`nip.io` / `sslip.io` 通配 DNS**，无需在每台机器配 hosts / DNS，例如：
  - `sales-dashboard.192.168.1.50.nip.io` → 自动解析到 `192.168.1.50` → Traefik 按名字路由到对应容器。
  - 管理页：`hub.192.168.1.50.nip.io`。
- 好处：管理页里"点名字打开"变成稳定、可读的地址，而不是记一堆端口号。

### 2.4 分类型处理策略

| 类型 | 识别特征 | 运行方式 |
|---|---|---|
| 纯 HTML | 只有 html/css/js，无 package.json / requirements | **不单独起容器**，由一个**共享静态服务器容器**（Caddy/nginx）统一托管，Traefik 按名字路由到对应静态目录 |
| Node/JS | 有 `package.json` | 生成 Dockerfile（node 镜像）→ 每实例一个 compose，Traefik label 路由 |
| Python | 有 `requirements.txt` / `pyproject` | 生成 Dockerfile（python 镜像）→ 每实例一个 compose；带库则在 compose 里加 db service（或用 SQLite 卷） |

> 纯 HTML 走共享静态容器，避免"为一个静态页起一个容器"的浪费；同时仍能被 Traefik 按名字统一路由。

---

## 三、关键决策（已确认）

| 决策点 | 结论 |
|---|---|
| 承载环境 | **Ubuntu / Windows WSL 为主** → Docker-first |
| 运行/保活层 | **Docker + docker compose，`restart: unless-stopped`** |
| 反向代理 | **引入 Traefik，按名字（nip.io 通配域名）路由**，弱化端口概念 |
| 触发方式 | **守护进程开关双模式**：开启守护 → 自动检查自动运行；关闭守护 → 等待大模型或 CLI 手动触发 |
| 管理页技术 | **小型 FastAPI Hub**（总览 + 单实例开关 + 资源监控 + 日志 + 统一入口） |
| 确定性层语言 | **Python** |

### 双模式触发细节

```text
lwa daemon on    →  watcher 监听 instances/_incoming/，检测到新 zip 自动跑完整流程（build + up）
lwa daemon off   →  静默等待，由你在根目录用大模型跑 skill，或用 lwa CLI 手动触发
```

两种模式底层执行的是**同一套确定性代码**，大模型只是"另一个能按同样流程调用它的调用者"。

---

## 四、目录结构（固定根目录 = `local-web-access`）

```text
local-web-access/                # 大模型在这个根目录运行，能读到 skills
├── AGENTS.md                    # 告诉 LLM：项目是什么、有哪些 skill、标准流程
├── skills/                      # 一套 skills（工具的"大脑"）
│   ├── ingest-zip/              # 解包、清洗，落到 instances/<slug>/
│   ├── detect-stack/            # 识别 static / node / python + 是否带数据库
│   ├── make-manifest/           # 生成/校验 manifest.json（核心配方）
│   ├── make-compose/            # 由 manifest 生成 Dockerfile + docker-compose.yml + Traefik labels
│   └── troubleshoot/            # 起不来时的排障流程（看容器日志等）
├── bin/                         # 确定性 CLI（不需要 LLM 也能跑）
│   └── lwa                      # scan / build / up / start / stop / down / status / stats / logs / daemon / dashboard
├── proxy/                       # 共享反向代理 Traefik（自身也是一个 compose 服务）
│   └── docker-compose.yml
├── static-server/               # 共享静态服务器容器（承载纯 HTML 实例）
│   └── docker-compose.yml
├── instances/                   # "实例库"
│   ├── _incoming/               # ← 把 zip 扔这里
│   ├── <slug>/                  # 每个网页一个目录
│   │   ├── app/                 # 解包后的代码
│   │   ├── Dockerfile           # 类型 2/3 由 skill 生成
│   │   ├── docker-compose.yml   # 由 manifest 生成，含 Traefik labels
│   │   ├── manifest.json        # 该实例的元数据 + 运行配方
│   │   └── logs/
│   └── registry.json            # 全局索引（dashboard 的数据源）
├── dashboard/                   # 管理一览页（FastAPI Hub + 前端）
└── run/                         # 守护进程状态、锁
```

所有实例容器 + Traefik + static-server 共用一个 Docker 网络（如 `lwa-net`）。

> 你提到"实例库最好附带一个 index"——这个 index 就是 `instances/registry.json`（机器可读的全局索引），管理页则是它的人类可读视图。

---

## 五、`manifest.json`（系统中枢，Dashboard 信息全部来源）

```json
{
  "name": "销售看板",
  "slug": "sales-dashboard",
  "type": "python",                    // static | node | python
  "stack": ["FastAPI", "SQLite"],
  "has_database": true,
  "db_type": "sqlite",
  "internal_port": 8000,               // 容器内监听端口（供 Traefik label 使用，非宿主机端口）
  "route": "sales-dashboard.192.168.1.50.nip.io",
  "image": "lwa/sales-dashboard:1.0.0", // 或 build: ./app
  "restart": "unless-stopped",
  "version": "1.0.0",
  "added_at": "2026-07-03T22:40:00+08:00",
  "source_zip": "sales-dashboard-v1.zip",
  "status": "running"                  // running | stopped | error
}
```

- 端口只记录**容器内监听端口**，供生成 Traefik label 用；宿主机不再逐个映射端口。
- `registry.json` 为全局汇总（聚合各实例 manifest 关键字段），是 Dashboard 唯一数据源；运行状态可再叠加 `docker compose ls` / `docker ps` 实时校正。

---

## 六、分层职责

| 层 | 用什么 | 干什么 | 依赖大模型？ |
|---|---|---|---|
| skills 层 | Markdown skill | 识别未知 zip、生成/修正 manifest、生成 compose、排障 | 是（一次性） |
| 确定性层 `bin/lwa` | Python | 解包、生成 compose、`build/up/start/stop/down/stats/logs`、写 registry | 否 |
| 保活层 | Docker `restart: unless-stopped` | 崩溃/重启自动拉起 | 否 |
| 路由层 | Traefik + nip.io | 按名字统一路由，消解端口冲突 | 否 |
| 展示层 | FastAPI Hub | 总览表 + 单实例开关(start/stop) + 资源监控 + 日志 + 统一入口 | 否 |

> 可选：用一个 systemd unit 确保 Docker daemon 与 `lwa daemon` watcher 开机自启；Docker 内部的实例保活交给 restart policy。

---

## 七、管理一览页（第二套交付物）

一个 **Hub 页面**（`hub.<lan-ip>.nip.io`），数据源 `registry.json` + docker 实时状态。

**顶部汇总卡片**（对应原始需求"已安装多少个 / 哪些带库"）：

- 已安装实例总数、运行中 / 已停止数量
- 带数据库实例数 vs 纯前端实例数
- 各技术路线分布（static / node / python）
- 整机资源占用条（内存 / CPU / 磁盘，见 §8.2）

**表格列**（逐实例明细）：

- 名字 / 类型 / 技术栈 / 是否带库 / 访问地址（route）/ 加入时间 / 版本 / 状态（运行中/停止）
- **实时资源**：内存用量/上限、CPU%（见 §8.2）
- 操作：**开关（start ⇄ stop）** / 打开（跳 route）/ 日志（docker logs）

---

## 八、按需启停 + 资源监控（小主机核心能力）

### 8.1 单实例开关（start ⇄ stop，不是 down）

管理页每个实例一行一个开关，用于"暂时退出内存、随时秒级拉回"。

| 动作 | 效果 | 用途 |
|---|---|---|
| `docker compose stop` | 容器保留，仅退出内存；数据卷/配置/DB 数据都在，秒级重启 | ✅ 开关"关" |
| `docker compose start` | 从已停止容器快速拉起 | ✅ 开关"开" |
| `docker compose down` | 删除容器（仅卸载/清理时用） | 卸载 |

> **两个层级的开关要分清**：
> - **全局守护开关** `lwa daemon on/off`：管"是否自动发现新 zip 并自动部署"。
> - **单实例开关**（管理页每行）：管"这个已部署实例现在跑不跑、占不占内存"。

> **边界情况**：纯 HTML 实例由共享静态容器托管，无独立容器，其"开关"实为"挂/摘 Traefik 路由"，不涉及内存释放（本身几乎不占内存）。

### 8.2 资源监控

Docker 原生提供数据，无需额外造轮子：

- **单容器**：`docker stats --no-stream` → CPU%、内存用量/上限、网络 I/O。Hub 定时轮询展示。
- **整机**：宿主机总内存/CPU/磁盘，配合每容器 `mem_limit` 一起看，一眼判断"还能不能再起一个"。
- 页面形态：每行显示该实例实时内存/CPU；顶部显示整机总占用条。

### 8.3 小主机资源纪律（内置默认）

针对 4G/8G 小主机，以下作为默认策略内置：

1. **base 镜像一律 slim/alpine**，控制磁盘占用（镜像是磁盘开销，非内存）。
2. **每容器设内存上限** `mem_limit`（如 256m），防止单个应用跑飞导致整机 OOM。
3. **按需启停（懒运行）**：装了很多但同时在用的少，用 §8.1 开关压住内存峰值。
4. **定期 `docker system prune`** 清理无用镜像/缓存。

> 认知：原生 Docker Engine（Ubuntu/WSL）下 `dockerd+containerd` 空闲仅约 100–300MB，容器与宿主机共享内核、非虚拟机；真正吃内存的是应用本身。瓶颈是"同时运行多少个应用"，用按需启停 + 内存上限即可控制。

---

## 九、实现步骤（建议顺序）

1. **骨架**：根目录结构 + `AGENTS.md`（让大模型一进来就知道有哪些 skill、标准流程）+ `lwa-net` 网络。
2. **基础设施**：`proxy/`（Traefik compose）+ `static-server/`（共享静态容器）先跑通。
3. **确定性核心 `bin/lwa`**：`scan / build / up / start / stop / down / status / stats / logs` + manifest/registry 读写 + compose 生成（含 `mem_limit`）。
4. **守护进程 `lwa daemon`**：监听 `_incoming/` 的 watcher + on/off 开关。
5. **skills 集**：ingest-zip / detect-stack / make-manifest / make-compose / troubleshoot。
6. **FastAPI Hub + Dashboard 页**：先接单实例开关（start/stop），再接资源监控（stats + 整机指标）。
7. **端到端验证**：用一个纯 HTML、一个 Node、一个 Python(带 SQLite) 样例 zip 走通全流程（含 Traefik 路由、保活、开关、监控）。

---

## 十、待进一步确认的小项

- 目录命名（`local-web-access` 是否沿用）。
- 局域网 IP / 域名策略（nip.io 通配 vs 自建 DNS vs 端口直连兜底）。
- Traefik 是否同时启用 dashboard、是否要基础鉴权。
- 数据库实例的数据卷 / 备份策略。

---

## 附录：讨论演进记录

按讨论时间顺序记录关键决策的演变与论证，便于回溯"为什么是现在这样"。

1. **最初设想**：固定根目录 + skills 外壳；纠结"自动检测转 systemd" vs "大模型驱动"。
   → 结论：**混合驱动**——LLM 负责识别/生成配方（一次性），确定性脚本负责运行/保活（长期）。

2. **第一版运行层选型**：systemd user service 保活 + Python 确定性层 + 双模式守护开关 + FastAPI Hub。

3. **提出改用 Docker**：质疑"是不是每次做成 Docker 部署最合理"。
   → 结论：Docker 换的只是"运行/保活层"，混合驱动骨架不变。**第 2/3 类（Node/Python）用 Docker 收益最大，纯 HTML 走共享静态容器**。确认环境以 Ubuntu/WSL 为主 → 转为 **Docker-first**。

4. **引入 Traefik 反向代理**：按名字（nip.io 通配域名）路由。
   → 关键收益：容器不再逐个映射宿主机端口，**"端口冲突检测"问题基本自动消失**。

5. **Docker 资源担忧**（4G/8G 小主机能否跑动）。
   → 澄清：重的是 Docker **Desktop**（含虚拟机），而 Ubuntu/WSL 用的**原生 Docker Engine** 空闲仅约 100–300MB，容器共享内核非虚拟机；**真正吃内存的是应用本身**。8G 从容，4G 靠"资源纪律"可控。

6. **新增两大管理页能力**：
   - **单实例开关**（start ⇄ stop，非 down）——释放内存且秒级拉回、不丢数据；区别于全局守护开关。
   - **资源监控**——`docker stats` 每容器 CPU/内存 + 整机指标。
   - 配套内置**小主机资源纪律**：slim 镜像 / `mem_limit` / 按需启停 / 定期 prune。
