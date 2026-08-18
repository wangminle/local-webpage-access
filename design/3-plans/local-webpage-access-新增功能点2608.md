# 新增功能点计划（202608）— 编号续接 IMP-043

> **状态（2026-08-11）**：本文件承接 [`../achievement/local-webpage-access-新增功能点2607.md`](../achievement/local-webpage-access-新增功能点2607.md)。**2607 范围内 IMP-025～028 / IMP-030～043 主路径均已落地**（见下「§0 上月收口」）。**8 月初已落地补记：IMP-044 / IMP-045**。**IMP-046 Token 7×24h 自动轮换已落地**（DEV-095）；**IMP-047 本机文件夹源导入与一键更新已落地**（DEV-096）。**IMP-051 管理页「选择文件夹」已落地**（DEV-097；仅 loopback）。**V0.7.1**：导入 UX 护栏（选根/dist、pending 勿冒充成功、错误码前缀剥离、`lwa update` 等导入空闲）与中文名 ID 回退等已收口。**IMP-052 / BUG-455 / BUG-456**：家庭图书 Agent 部署复盘后的 Python 启动推断与 manager off 跨工作区提示（见 §11）。**IMP-053**：已有 Runtime 复用提示（§11.5，DEV-099）。**IMP-055**：路径别名兼容性门禁与文档口径见 §12；2026-08-11 评审后明确为「入口 HTML 根绝对资源负向守卫」，不构成整体兼容证明。**IMP-056 / IMP-057 的 MVP、IMP-058 Gate-A 原战术范围与 Gate-B 已落地；Gate-A 修订后 SQLite 安全加固见 IMP-058.A.R01。Gate-C 已具备核心模型、成功谓词、状态机和模拟故障注入，但完整计划执行、事务回滚、副作用采集及真实 Docker 门控仍按 Scanner 文档续接 WBS 收口。** **后续 / 不着急：IMP-048 zip↔文件夹转换；IMP-049 / IMP-050（优先级：中，不与 046/047 抢档）。****新立项待实施：IMP-064 服务意图字段去污染（§16，P1；CHK-230 / CHK-232 已修订契约）。** **IMP-042.b 跨盘/跨机不纳入本文件、暂不开发**。候选仍含 IMP-029。
> **范围**：§0 为 2607 与实施计划合集核对；§1～§2 已落地补记（044/045）；**§4～§5 本月优先 046/047（含可执行 WBS）**；**§6 IMP-051 文件夹选择器（已落地）+ V0.7.1 导入护栏收口**；**§7 后续 048**；**§8 合集移植 049/050（优先级中 / 不着急）**；§9 其它候选；**§11 Agent 部署复盘与即时修复（含 IMP-053）**；**§12 路径别名 × 方案 B（IMP-055，含详细 WBS与评审边界）**；**§13 Scanner 多候选与实证校验（IMP-056～058 摘要）**。无 §3（原 042.b 已删除）。日常跟踪以 `task-list.md` 为准。

---

## 0. 上月（2607）收口核对

### 0.1 主路径结论

| 编号 | 主题 | 结论 | 跟踪 |
| --- | --- | --- | --- |
| IMP-025 | page 级访问次数 | **已落地** | DEV-068 |
| IMP-026 | 独立 IP 列表 + 本机标记 | **已落地** | DEV-069 |
| IMP-027 | 容器经 Caddy 别名真实访客 IP | **已落地** | DEV-070 |
| IMP-028 | 无别名直连端口静态站浏览量 | **已落地** | DEV-072 |
| IMP-030 | macOS / Linux（含 WSL）自启动 | **已落地** | DEV-073 |
| IMP-031 | Docker 国内源安装脚本 | **已落地** | DEV-074 |
| IMP-032 | setup/init `--default` / `--full` | **已落地** | DEV-075 |
| IMP-033 | Full Profile 权限与能力闭环 | **主路径已落地**；033.13 真机勾选属运维 | DEV-076 / DEV-078 |
| IMP-034 | 日志可观测性补强 | **已落地** | DEV-077 / DEV-079 |
| IMP-035 | 管理页安全删除二次确认 | **主路径已落地**；035.06 真机勾选属运维 | DEV-080 / DOC-052 |
| IMP-036 | 正式支持平台收敛 | **主路径已落地**；036.08 真机矩阵属运维；036.09 已完成 | DEV-081 / DEV-091 |
| IMP-037 | 网关后端原子切换 | **已落地** | DEV-082 |
| IMP-038 | 升级后访问复核闭环 | **已落地** | DEV-083 |
| IMP-039 | 进行中构建可控取消 | **已落地** | DEV-084 |
| IMP-040 | LAN 地址新鲜度与漂移自愈 | **已落地**（原 `update --pull` 方案已删除） | DEV-087；DEV-085 已关闭 |
| IMP-041 | 删除阶段日志 + 容器别名清理 | **已落地**（原 Vite 端口元数据方案已删除） | DEV-088；DEV-086 已关闭 |
| §23 | WSL2 实机排障反哺 | **已落地** | BUG-376～381 / DOC-077 |
| IMP-042 | LWA 工作区迁移（同卷） | **主路径已落地**；跨盘/跨机（042.b）**暂不开发、不入本文件** | DEV-089 |
| IMP-043 | 实例显示名（主页 `<title>`） | **已落地**；固定列宽设想已撤销 | DEV-090；BUG-413/414 |

### 0.2 不计入「未实现」的边界

| 项 | 说明 |
| --- | --- |
| 原 IMP-040 `update --pull` / 原 IMP-041 Vite 端口元数据 | 已从 2607 **范围删除**，对应 DEV-085/086 **已关闭**，不是欠账 |
| 033.13 / 035.06 / 036.08 | 清单已写入 `docs/acceptance-checklist.md`；**缺真机勾选**，非功能未实现 |
| IMP-042.b 跨盘/跨机迁移 | **暂不开发**；不写入本月 2608 待办（仍可在 2607/合集查阅历史口径） |
| IMP-029 | 仍在待改进记录，见 §9 |

### 0.3 核对依据（2026-08-06）

- 文首状态与各节声明 vs `task-list.md` DEV-068～091
- 源码触点抽查：`pageviews`、`autostart`、`cancel_build`、`gateway_switch`、`workspace_migrate`、`extract_html_title`、已有 `rotate_token` 等
- 不把「运维真机勾选未填」写成「功能未实现」

### 0.4 实施计划合集（20260804）核对

来源：[`../achievement/local-webpage-access-实施计划合集-20260804.md`](../achievement/local-webpage-access-实施计划合集-20260804.md)。

| 计划块 | 结论 |
| --- | --- |
| BUG-293～320 全量收口 | **已落地**（28/28 已修复） |
| IMP-042 工作区迁移主路径（Task 1～10） | **已落地**（DEV-089） |
| 管理页表布局恢复 V0.6.9 + 两行截断 | **已落地**（BUG-413/414） |
| 七项待办 BUG-369/370/371/420/421/422 + DEV-094 | **已落地**（CHK-139；后续加固 BUG-423～430 等亦已修） |
| 合集 Task 11（P2）：路径相对化 / CLI↔工作区解耦 | **未实现** → §8 IMP-049 / 050（**优先级：中 · 不着急**） |
| 合集 Task 11：跨盘 IMP-042.b | **暂不开发**；不迁入本文件待办 |

另：ADJ-037 决议的 **P4 盲目旧→新路径整树替换** 仍暂缓，由 IMP-045 的 P0/P1 + enable/recover 覆盖；不单独升格 IMP。

---

## 1. IMP-044 — CLI 对齐管理页：`lwa recover` / `lwa pageviews`（已落地补记）

> **状态**：**已落地**（2026-07-31，DEV-092；DOC-092）。

### 1.1 需求

1. `lwa recover <id>`：对齐管理页「恢复」路径。
2. `lwa pageviews [id]`：对齐 `/api/pageviews`；先惰性摄入再汇总/明细。

### 1.2 task-list 映射

| ID | 关系 |
| --- | --- |
| `IMP-044` / `DEV-092` / `CHK-136` | 本项 / 实现 / 审计来源 |

---

## 2. IMP-045 — 裸 `mv` 防复发：doctor 工作区路径一致性 + 启动/挂载自愈（已落地补记）

> **状态**：**已落地**（2026-08-03～08-04，DEV-094 + BUG-420～430 等）。

### 2.1 摘要

P0 网关启动前写主配置（BUG-420）；P1 doctor 路径一致性（DEV-094）；P2 挂载漂移 fail-closed（BUG-421 等）；P3 派生路径回写（BUG-422）；P4 不做盲目整树替换。

### 2.2 task-list 映射

| ID | 关系 |
| --- | --- |
| `IMP-045` / `DEV-094` / `BUG-420`～`430` | 本项 / doctor / 配套修复 |

---

## 4. IMP-046 - 管理页 Token 每 7×24 小时自动轮换（已落地）

> **状态**：**已落地**（2026-08-06，DEV-095；DOC-093）。全 12 个工作包（046.01～046.12）已实现并通过回归验证。
> **背景**：管理页 API token 现由 `ensure_token` 生成并持久化在 `run/manager-token.json`；已有手动 `rotate_token`（BUG-118），但**无周期自动轮换**。局域网经 `managerPort`（默认 **17800**）访问时依赖该 token；本机 loopback 免 token（IMP-003）必须保留。

### 4.1 需求

1. **LWA 管理面启动后**，以 **168 小时（7×24）** 为周期，**自动轮换**管理页 API token。
2. **本机 loopback 访问**（`127.0.0.0/8`、`::1` 等，与现 `_is_loopback_host` / IMP-003 一致）**继续免 token**，行为不变。
3. **其它机器经局域网地址访问 17800**（或配置的 `managerPort`）时，必须携带**当前有效** token；轮换后旧 token 立即失效。
4. 轮换应对运行中的 manager 进程生效（或有明确、可操作的生效路径），不能只改磁盘文件却让内存中的旧校验长期有效。
5. 运维可感知：CLI/文档能查到「当前 token / 颁发时间 / 下次轮换时间」；轮换后局域网用户如何取得新 token 有说明（例如本机打开管理页、或 `lwa manager token` 类命令——具体 CLI 面实施时定）。

### 4.2 关键决策（已拍板 + 实施默认）

| 编号 | 决策点 | 方案 |
| --- | --- | --- |
| **046.a** | 周期 | **固定 168h（7×24）**；配置项可预留（如 `managerTokenRotateHours`），默认 168。 |
| **046.b** | 本机免 token | **保留 IMP-003**：仅 loopback 免鉴权；LAN IP / 非 loopback **必须**有效 token。 |
| **046.c** | 计时起点 | 以 token 文件中的 **`createdAt`（颁发时刻）** 起算满 168h 后轮换；manager 重启不重置周期（除非文件丢失触发重新颁发）。 |
| **046.d** | 触发位置 | manager 进程内定时检查（启动时立即检查一次 + 周期 tick）；可与 `ensure_token` / 既有 `rotate_token` 复用写盘逻辑。 |
| **046.e** | 生效方式 | 轮换后 **内存与磁盘同时更新**；正在使用旧 token 的 LAN 客户端收到 401，需换新 token。本机 loopback 无感。 |
| **046.f** | 非目标（本期） | 不做多 token 并存宽限期；不做 OAuth/多用户；不改变业务实例页（仅管理面 17800）的鉴权模型。 |

### 4.3 现状触点

- `manager_api.ensure_token` / `rotate_token` / `_write_token`（已写 `createdAt`）；`_verify_token` → `read_token` **每次请求读盘**
- `require_token` + `_is_loopback_request`（IMP-003）
- `manager_service` 启动 / lifespan 后台线程位（可挂 tick）
- 前端 `app.js`：Bearer / 本机判定；LAN 存本地 token
- 文档：`docs/manager-page.md`、FAQ；CLI `manager` 子命令组

### 4.4 WBS（可执行）

> 规模：S≤0.5d · M≈0.5–1.5d · L≈1.5–3d。依赖只写硬前置。建议顺序：**A → B → C → D（可与 C 后并行文档）→ E**。

#### 阶段 A — Token 元数据与轮换判定（库内核）

| ID | 工作包 | 规模 | 触点 | 交付物 | 依赖 | 完成标准 |
| --- | --- | --- | --- | --- | --- | --- |
| **046.01** | Token 文件读写契约 | S | `manager_api.py` `_write_token` / `read_token` / `ensure_token` | 稳定字段：`token`、`createdAt`；兼容缺 `createdAt` 的旧文件（视为「刚颁发」或立即补写，方案在实现时二选一并测） | — | 旧文件可读；新写入含 ISO `createdAt`；权限仍 0o600 |
| **046.02** | 轮换判定纯函数 | S | 新建或同模块 `should_rotate_token(created_at, *, now, hours=168)` | 纯函数 + 单测（差 1s、正好 168h、缺 createdAt、非法时间） | 046.01 | 测注入时钟；无 IO |
| **046.03** | 轮换写盘 API | S | 扩展 `rotate_token` / 新增 `maybe_rotate_token(ws, *, hours, now=)` | 到期则换新 token 并刷新 `createdAt`；未到期返回当前 | 046.01–02 | 单测：未到期不改文件；到期改 token 且 `createdAt` 更新 |

#### 阶段 B — Manager 进程内自动轮换

| ID | 工作包 | 规模 | 触点 | 交付物 | 依赖 | 完成标准 |
| --- | --- | --- | --- | --- | --- | --- |
| **046.04** | 启动时检查一次 | S | `manager_service` / app lifespan 启动 | 管理页进程起来后立刻 `maybe_rotate` | 046.03 | 伪造过期 `createdAt` 后冷启动 → 新 token 落盘 |
| **046.05** | 后台 tick | M | lifespan 守护线程或等价定时器 | 默认每 **15～60min** 检查（常量可配）；daemon 线程不阻塞请求 | 046.03–04 | 时间注入或缩短 interval 的测/手工：跨 tick 后文件已轮换 |
| **046.06** | 生效语义核对 | S | `_verify_token`、前端缓存说明 | 确认鉴权读盘故**无需为鉴权重启**；文档删除过时「必须重启才生效」表述（若仍存在）；前端 LAN 旧 token → 401 | 046.04 | loopback 免 token 回归绿；LAN 旧 token 401、新 token 200 |

#### 阶段 C — CLI / 配置可观测

| ID | 工作包 | 规模 | 触点 | 交付物 | 依赖 | 完成标准 |
| --- | --- | --- | --- | --- | --- | --- |
| **046.07** | 配置项（可选） | S | `config.py` / `local-web.yml` | `managerTokenRotateHours` 默认 **168**；非法值拒绝或回落默认 | 046.02 | 配置加载测；默认不变时行为=168h |
| **046.08** | CLI 查询面 | M | `cli/manager.py`（命令名实施时定，如 `lwa manager token` / `status` 增补） | 打印/JSON：是否已颁发、`createdAt`、下次轮换时间、**本机可显示 token 明文**（LAN 文档说明如何取） | 046.01 | `--help` 与 JSON 契约测；非本机场景不在本期做远程取 token |

#### 阶段 D — 前端与文档

| ID | 工作包 | 规模 | 触点 | 交付物 | 依赖 | 完成标准 |
| --- | --- | --- | --- | --- | --- | --- |
| **046.09** | 管理页 401 UX | S | `manager_static/app.js` | LAN 401 时提示可能已轮换，引导本机打开或 CLI 取新 token；**不改变** loopback 免 token | 046.06 | 前端测或手工：本机无弹错误登录墙；LAN 401 文案可见 |
| **046.10** | 用户文档 | S | `manager-page.md` / FAQ / README 摘句 | 周期、本机免 token、如何取新 token、轮换后旧会话失效 | 046.07–09 | 文案与实现一致；无「必须重启才轮换」矛盾句 |

#### 阶段 E — 回归与收口

| ID | 工作包 | 规模 | 触点 | 交付物 | 依赖 | 完成标准 |
| --- | --- | --- | --- | --- | --- | --- |
| **046.11** | 自动化回归套件 | M | `tests/test_manager_api.py` 等 | 覆盖：loopback 免 token；LAN 鉴权；到期轮换；重启不重置未到期周期；tick/启动路径 | A–D | 定向 pytest 全绿 |
| **046.12** | 门禁与 task-list | S | DEV 条目 | `compileall` / 相关 pytest / `task-list` DEV 完成态 | 046.11 | DEV 关闭；2608 §4 状态改「已落地」 |

**推荐落地顺序（摘要）**

```text
046.01 → 046.02 → 046.03 → 046.04 → 046.05 → 046.06
                         ↘ 046.07 → 046.08
046.06 → 046.09 → 046.10 → 046.11 → 046.12
```

### 4.5 验收标准

- manager 在模拟时钟下满 168h（或缩短配置）后自动换新 token；LAN 旧 token → 401，新 token → 200。
- `http://127.0.0.1:17800/`（及同类 loopback）**全程无需** token。
- manager 中途重启：**未到期**不因重启重置 `createdAt` / 不无故换 token。
- CLI（或文档约定入口）能看到颁发时间与下次轮换；文档说明 LAN 取新 token 方式。
- 不做多 token 宽限期；不改业务实例页鉴权。

### 4.6 task-list 映射

| ID | 关系 |
| --- | --- |
| `IMP-046` | 本功能点 |
| `PLN-029` | 本月规划（优先） |
| `DEV-*` | 实施时按阶段 A–E 开开发项或一条总 DEV + 子备注 |

---

## 5. IMP-047 — 本机文件夹源导入 + 一键更新（已落地）

> **状态**：**已落地**（2026-08-06，DEV-096）。folder_source.py + importer.import_from_dir/update_from_dir + CLI --from-dir + manager API + 前端导入/更新/详情 + lwa-import-folder Skill + 隔离红线硬断言测试（42 用例全绿，全量 1631 passed）。
> **落地后修订（2026-08-06，BUG-443）**：补齐「识别 → 可启动 → 自动部署」闭环与任意 HTML 静态识别；见 §5.7 与合集文末块。
> **背景**：当前导入只接受 zip（inbox / `lwa import`）。开发中的源码常以本机目录形式存在；希望**关联**本机开发目录，由 LWA **复制**核心代码进入自身工作区实例目录后按与 zip **完全相同**的方式托管；并支持「点更新 → 再从关联目录同步一次」。

### 5.1 需求

1. **新增导入模式：本机文件夹源**（`sourceKind=folder`，名称实施时可微调）。
2. 用户（或 Agent）给出 **LWA 所在机器上的本地目录绝对路径**（关联的开发/源码文件夹）。
3. **硬约束：关联 ≠ 就地运行**（产品红线）
   - 关联目录**只是复制来源**，记录在元数据里供日后「更新」读取。
   - **禁止**把 Caddy root、builtin 静态根、Compose bind mount、构建工作目录、运行 cwd 指到用户关联目录。
   - LWA 必须把源目录中的**核心代码文件复制**进本工作区自己的实例树（与 zip 导入后一致，如 `apps/<id>/current/` 及既有 `source/original.zip` 等），再在**该副本**上识别、构建、启动、保活。
   - 运行方式、生命周期、端口、别名、data/ 与 zip 模式**同一套路径**；差别仅在「内容从哪来、如何触发更新」。
4. **导入动作**：从关联目录收集/复制核心文件（可经临时 zip 或等价打包，复用 IMP-001 剥离）→ 写入 LWA 实例目录 → 走既有 import 管线 → **管理页导入在识别成功且档位轻量时自动部署**（对齐 daemon；§5.7）。
5. **仅支持本机路径**：不支持跨机共享目录 / UNC / 远端 URL；路径须本机可 resolve 且为存在的目录。
6. **更新**：管理页「更新」/ CLI / Skill 从**同一关联路径**再复制一轮 → 走既有 update（对齐 IMP-009）。
   - 若相对上次已同步内容**无实质变动**（内容 hash 相同；若源目录是 git 仓库且 `git diff` / 工作区相对上次同步基线无变更，亦可作辅助信号）→ **提示「无需更新」**，不重建、不打断运行。
   - 有变动才执行复制 + update。
7. **管理页 / CLI / Skill 三端对齐**：
   - CLI：例如 `lwa import --from-dir /abs/path/to/src`（旗标名实施时定）；
   - Skill：指导 Agent 完成「关联目录导入 → 构建/启动 → 触发更新」；
   - 管理页：文件夹源实例提供「更新」；展示来源类型与关联路径；**导入成功后轻量实例应进入 running（或至少 stopped 可点启动）**，不得因误标 pending 禁用启动。
8. **展示**：基本信息与 zip 实例一致；**额外标明来源**（文件夹 vs ZIP）及关联路径（截断 + tooltip）。
9. **明确不做（本期）**：zip ↔ 文件夹**模式转换**（§7 IMP-048）；不在关联目录内 watchdog 热更新（仅用户/Agent 点更新）；不在关联目录内执行 `npm`/`docker`/`caddy`。
10. **路径输入 UX（IMP-051）**：管理页「从文件夹导入」对话框中，源目录路径不得只靠手打；须提供与常见桌面软件一致的「选择文件夹」入口（见 §6）。

### 5.2 关键决策（已拍板 + 实施默认）

| 编号 | 决策点 | 方案 |
| --- | --- | --- |
| **047.a** | 源范围 | **仅本机本地目录**；拒绝非目录、不存在路径；不实现跨机/SMB/HTTP 源。 |
| **047.b** | 运行隔离（红线） | **永远在 `apps/<id>/`（及 LWA 生成的 docker/gateway 配置）内运行**；关联路径只读复制源，写入仅发生在 LWA 工作区。 |
| **047.c** | 与 zip 管线关系 | 复制/打包 → **复用 `import_zip` / `update_zip`（或等价落盘到 current + original.zip）**；托管、构建、启停与 zip **零分叉**。 |
| **047.d** | 元数据 | `sourceKind`（`zip` \| `folder`）+ `sourceDirPath`（文件夹模式必填）；可另存「上次同步内容指纹」供无变更短路。 |
| **047.e** | 更新语义 | 从 `sourceDirPath` 再复制 → 与上次指纹比较；**相同则「无需更新」**；不同则走 update（data 策略对齐 zip / `--keep-data`）。git diff 作辅助：无 git 时以内容 hash 为准；有 git 且无 diff 且指纹未变 → 同样提示无需更新。 |
| **047.f** | 源目录漂移 | 关联路径不存在/不可读 → 明确错误；**禁止**回退到「直接挂载该目录运行」。 |
| **047.g** | Agent 路径 | Skill 写明：复制进 LWA、禁止在用户仓库内起服务；传本机路径、CLI 导入/更新/启动。 |
| **047.h** | 前端 | 标注来源类型与关联路径；「更新」；无变更时 Toast/文案「无需更新」。路径手输之外的「选择文件夹」见 **IMP-051**。 |
| **047.i** | 导入后状态（BUG-443） | 识别成功 → **`stopped`**；仅真·未识别 → `pending`。 |
| **047.j** | 管理页自动部署（BUG-443） | `POST /api/import-from-dir` 调用 `try_auto_start_after_import`（tiny/small）；响应含 `autoStart`。 |
| **047.k** | 静态入口（BUG-443） | 任意 `*.html` 可识别/托管；`index.html` 优先；保证 `public/index.html` 存在。 |

### 5.3 现状触点（复用）

- `importer.import_zip` / `update_zip`：解压 → 扫描 → 落盘 `apps/<id>/`；update 已有 **hash 相同跳过**（「包未变化」）
- IMP-001 剥离：`sanitize` / stripped_names
- `models.InstanceManifest`：`sourceZipPath` / `sourceZipHash`；缺 `sourceKind` / `sourceDirPath`
- CLI：`cli/importing.py`（仅 zip 路径）
- Skill：`skills/lwa-import-zip/SKILL.md`（仅 zip）
- 管理页：导入 / 更新 UI；实例详情字段

### 5.4 WBS（可执行）

> 规模：S≤0.5d · M≈0.5–1.5d · L≈1.5–3d。**红线贯穿全程**：关联目录只读复制源；运行根永远 ∈ `apps/<id>/`（及 LWA 生成配置）。建议顺序：**A → B → C → D∥E → F → G**。

#### 阶段 A — 目录 → 工作区副本（只读源）

| ID | 工作包 | 规模 | 触点 | 交付物 | 依赖 | 完成标准 |
| --- | --- | --- | --- | --- | --- | --- |
| **047.01** | 路径校验 | S | 新 helper（如 `folder_source.py`） | 本机绝对路径；必须存在且为目录；拒绝文件/相对歧义/不可读；错误码清晰 | — | 单测覆盖非法路径 |
| **047.02** | 目录打包/暂存 | M | helper + IMP-001 | 从源目录收集核心文件 → **临时 zip 或 stage 目录**；对源目录**只读**；剥离对齐 zip import | 047.01 | 源树无新增 LWA 产物；产出可被 `import_zip` 消费 |
| **047.03** | 内容指纹 | S | helper | 与 zip 同源语义的内容 hash（复用或对齐 `sourceZipHash` 算法）；供无变更短路 | 047.02 | 同内容同 hash；改一文件 hash 变 |

#### 阶段 B — 元数据与 importer API

| ID | 工作包 | 规模 | 触点 | 交付物 | 依赖 | 完成标准 |
| --- | --- | --- | --- | --- | --- | --- |
| **047.04** | Manifest / registry 字段 | M | `models.py`、migrate、序列化 | `sourceKind`: `zip` \| `folder`（旧实例默认 `zip`）；`sourceDirPath`（folder 必填）；可选 `sourceSyncHash` / 沿用 `sourceZipHash` | — | 旧 JSON 可加载；新字段往返 |
| **047.05** | `import_from_dir` | M | `importer.py` | 校验 → 打包 → 调 `import_zip`（或等价落盘）；写 `sourceKind=folder` + `sourceDirPath`；**断言** `appPath`/`current` ∈ workspace `apps/<id>` | 047.01–04 | 导入后运行根不在源目录；registry 字段正确 |
| **047.06** | `update_from_dir` | M | `importer.py` | 读 `sourceDirPath` → 再打包 → 指纹相同则 **`skipped` +「无需更新」**；不同则 `update_zip`；源缺失明确报错（禁止挂载回退） | 047.03–05 | 无变更不换盘不 rebuild；有变更 current 更新；源删报错 |
| **047.07** | git 辅助信号（可选） | S | helper | 若源是 git 且相对上次同步基线无 diff **且**指纹未变 → 同样「无需更新」；无 git 时仅靠 hash | 047.06 | 有/无 `.git` 两路径测；不以 git 单独覆盖「内容已变」 |

#### 阶段 C — CLI

| ID | 工作包 | 规模 | 触点 | 交付物 | 依赖 | 完成标准 |
| --- | --- | --- | --- | --- | --- | --- |
| **047.08** | `lwa import --from-dir` | M | `cli/importing.py` | 与 zip 互斥或分支；`--update <id>` 对 folder 源走 `update_from_dir`；help 写明「复制进工作区、非就地运行」 | 047.05–06 | CLI 测：import / update / skipped 文案；dry-run 若可复用则对齐 |
| **047.09** | list/show 展示 | S | CLI 列表/详情 | 标明 `sourceKind` 与关联路径（截断） | 047.04 | 输出可读、不泄露无关 secrets |

#### 阶段 D — API + 管理页

| ID | 工作包 | 规模 | 触点 | 交付物 | 依赖 | 完成标准 |
| --- | --- | --- | --- | --- | --- | --- |
| **047.10** | Manager API | M | `manager_api` 导入/更新路由 | 支持 folder 导入体（路径字段）；folder 更新端点/复用 update；返回 `skipped` + message；**导入成功后轻量档位自动 start（`autoStart`）** | 047.05–06 | API 契约测；错误码与 CLI 对齐；自动部署与 daemon 同规则 |
| **047.11** | 管理页导入 UX | M | `manager_static` | 选「本机文件夹」模式；填绝对路径；提交后走 API | 047.10 | 手工/前端测：导入成功且来源标注 |
| **047.12** | 详情与更新 UX | M | 同上 | 展示来源类型 + 关联路径（截断 + tooltip）；「更新」；无变更 Toast「无需更新」 | 047.10–11 | 与 zip 实例启停等行为一致；无变更不闪断运行 |

#### 阶段 E — Skill 与文档（可与 D 后半并行）

| ID | 工作包 | 规模 | 触点 | 交付物 | 依赖 | 完成标准 |
| --- | --- | --- | --- | --- | --- | --- |
| **047.13** | Skill | M | 新建 `lwa-import-folder` 或扩展 `lwa-import-zip` | Agent 流程：传本机路径 → CLI 导入 → 构建/启动 → 更新；**红线**：禁止在关联目录内 npm/docker/caddy | 047.08 | Skill 步骤可照做；明确与 zip skill 分工 |
| **047.14** | 用户/运维文档 | S | README、manager-page、operations、known-limitations | 文件夹源说明；与 zip 差异；不做 048 转换；不做 watchdog 热更 | 047.08–12 | 与实现一致 |

#### 阶段 F — 隔离与回归

| ID | 工作包 | 规模 | 触点 | 交付物 | 依赖 | 完成标准 |
| --- | --- | --- | --- | --- | --- | --- |
| **047.15** | 隔离硬断言测 | M | tests | 导入/更新后：Caddy root / compose bind / builtin 根 / 进程 cwd **均不**等于 `sourceDirPath`；写入仅落在 workspace | 047.05–06 | 失败即红线违规 |
| **047.16** | 端到端回归 | M | tests | 有变更更新；无变更短路；源缺失；与 zip 生命周期字段一致；不实现模式转换 | C–E | 定向 pytest 全绿 |
| **047.17** | 门禁与 task-list | S | DEV | compileall / pytest / DEV 完成；2608 §5 改「已落地」 | 047.15–16 | DEV 关闭 |

**推荐落地顺序（摘要）**

```text
047.01 → 047.02 → 047.03 → 047.04 → 047.05 → 047.06 → 047.07(可选)
                                      ↘ 047.08 → 047.09
047.06 → 047.10 → 047.11 → 047.12
047.08 → 047.13 → 047.14
047.15 → 047.16 → 047.17
```

### 5.5 验收标准

- `lwa import --from-dir <本机目录>` 后，实例 `current/` 与运行配置位于 **LWA 工作区**；进程/容器/静态根**不**指向关联目录。
- 管理页标注文件夹来源与关联路径；启停、端口、别名等与 zip 实例一致。
- 管理页文件夹导入：识别成功且 tiny/small → **自动 running**；响应含 `autoStart`；不得误标「待识别」并禁用启动。
- 仅含非 `index.html` 名的可打开 HTML 目录 → 识别为 static 并可 `GET /`。
- 改关联目录后再更新 → 工作区副本变新；LWA **不**往关联目录写运行产物。
- 无实质变动点更新 → **「无需更新」**，不 rebuild、不重启（除非显式 force，若做则另开子项）。
- Skill + CLI 可完成导入与更新；**不**做 zip↔文件夹转换（IMP-048）。

### 5.6 task-list 映射

| ID | 关系 |
| --- | --- |
| `IMP-047` | 本功能点 |
| `PLN-030` | 本月规划（优先） |
| `DEV-096` | 主路径落地 |
| `BUG-443` | 识别/状态/自动部署闭环补丁 |
| `OPS-097` | runtime 验收（3-src / 4-output） |
| 关联 | IMP-001（剥离）、IMP-009（update）、IMP-048（转换，后期） |

### 5.7 落地后修订（BUG-443，2026-08-06）

IMP-047 主路径落地后，管理页文件夹导入仍出现「待识别死胡同」：识别成功却写 `pending`、API 不自动 start、UI 禁用启动；纯 HTML 因非 `index.html` 文件名被判 unknown。

**修订方案（已实现）**：B 状态语义（成功→`stopped`）→ C `import-from-dir` 对齐 daemon 自动 start → A 任意 `.html` 识别与 `public/index.html` 兜底。详细流程图与验收记录见合集 [`../achievement/local-webpage-access-实施计划合集-20260804.md`](../achievement/local-webpage-access-实施计划合集-20260804.md)「2026-08-06 · 文件夹导入识别/部署死胡同收口」。

**runtime 验收源**：

- `.../multidevices-wakeup-demo-3d/3-src` → 实例 `v1`，`:18006` 200
- `.../20260730-三维挂谷猜想的3d动画/4-output` → 实例 `3d`，`:18002` 与别名 `/3d-kakeya-animation/` 200；仅 `kakeya-3d-chapters.html` 时 API 新建亦可 autoStart 200

### 5.8 V0.7.1 相关修补（指向 §6.8）

文件夹导入真机验收后追加的 **pending 自愈、中文名 ID、管理页 UX、`lwa update` 与导入互斥** 等，统一记入 **§6.8**（与 IMP-051 同属 047 体验闭环），避免本节再拆一条 IMP。

---

## 6. IMP-051 — 管理页「选择文件夹」按钮（已落地）

> **状态**：**已落地**（2026-08-06，`DEV-097` / `PLN-035` / `DOC-116`）。属 IMP-047 管理页导入对话框的体验补强。
> **一句话**：用户不应靠手敲绝对路径；源目录输入框右侧提供「选择文件夹」按钮，唤起本机常见的目录选择器（macOS 访达 / Ubuntu 文件管理器同类控件），选完回填路径。**仅 loopback 启用。**
> **实现计划**：[`docs/plans/2026-08-06-imp-051-pick-directory.md`](../../docs/plans/2026-08-06-imp-051-pick-directory.md)。

### 6.1 需求

1. **触点**：管理页「从文件夹导入」模态框（三字段：源目录路径 / 实例名称 / 路径别名）。
2. **控件位置**：第一个字段「源目录路径」输入框**右侧**增加按钮（文案：「选择文件夹」）；与输入框同一行，视觉上为常见「路径 + 浏览」组合。
3. **交互**：
   - 点击按钮 → 打开**本机目录选择对话框**（非文件多选；只选目录）。
   - 用户确认后，将所选目录的**绝对路径**写入「源目录路径」输入框（可继续手工改）。
   - 取消对话框 → 不改动现有输入。
4. **平台**：与 LWA 正式支持平台对齐——**macOS**（访达目录面板）与 **Ubuntu / Linux**（`zenity`/`kdialog` 等目录选择器）。
5. **仍保留手输**：高级用户 / 远程场景 / 无图形会话时可继续粘贴绝对路径；选择器是增强，不是唯一入口。
6. **语义不变**：选中的仍是 **LWA 宿主机上的本机路径**（IMP-047 红线：关联后复制进工作区，非就地运行）。选择器不得改成「把浏览器所在机器的目录上传当 zip」。

### 6.2 关键决策（已拍板）

| 编号 | 决策点 | 方案 |
| --- | --- | --- |
| **051.a** | 为何不「只靠浏览器 `<input type=file webkitdirectory>`」 | 纯网页拿不到宿主机真实绝对路径；LWA API 要的是宿主机 `sourceDir`。 |
| **051.b** | 实现 | 管理页 `POST /api/pick-directory` → manager 进程调原生对话框 → 返回 `{path}`。macOS：`osascript`/`choose folder`；Linux：`zenity --file-selection --directory` / `kdialog --getexistingdirectory`。 |
| **051.c** | 无显示器 / SSH 无 GUI / 对话框失败 | API：`cancelled` / `unavailable` / `timeout`（400）；前端 Toast；**不阻塞**手输。 |
| **051.d** | 从另一台电脑打开管理页 | **仅 loopback 启用按钮与 API**；非 loopback → 按钮禁用 + 文案提示手输；API 即使有 token 也 **403 `loopback_required`**（防远程弹服务器 GUI）。 |
| **051.e** | 权限 | 与其它管理 API 相同：需 manager token；只返回路径字符串，不扩大写权限。 |
| **051.f** | 不做 | 不在关联目录内打开；不改 CLI（仍 `--from-dir`）；不做跨机浏览远端盘符。 |

### 6.3 落地触点（已实现）

| 模块 | 路径 / 说明 |
| --- | --- |
| 选目录 helper | `directory_picker.py` + `DirectoryPickerError` |
| API | `POST /api/pick-directory`（`manager_api.py`）；loopback 门禁 + 错误码映射 |
| 前端 | `manager_static/app.js`：`pickFolder` / `canPickFolder` / `folderImport.picking`；`style.css`：`.path-with-browse` |
| 文档 | `docs/manager-page.md`（API + UI）；Skill `lwa-import-folder` |
| 测试 | `test_directory_picker.py`；`test_manager_api` pick 用例；`test_manager_static_app` |

### 6.4 WBS（已完成）

| ID | 工作包 | 状态 | 交付物 |
| --- | --- | --- | --- |
| **051.01** | 宿主机选目录 helper | ✅ | `directory_picker.py`；取消/无 GUI/超时错误码 |
| **051.02** | Manager API | ✅ | `POST /api/pick-directory`；loopback 403；鉴权 |
| **051.03** | 管理页按钮与回填 | ✅ | 路径行浏览按钮；LAN 禁用提示；picking 态 |
| **051.04** | 文案与文档 | ✅ | manager-page / Skill / 对话框 hint |
| **051.05** | 回归 / task-list | ✅ | DEV-097、PLN-035、DOC-114/116 |

### 6.5 验收标准（已满足）

- 管理页「从文件夹导入」：源路径右侧有「选择文件夹」；本机 `127.0.0.1` 可弹系统对话框并回填绝对路径。
- 局域网 IP 打开管理页：按钮禁用，须手输 LWA 机器路径；API 403 `loopback_required`。
- 取消 / 无 GUI：可继续手输；不 500。
- 选完导入行为与手输同一路径一致（复制进工作区，非就地运行）。

### 6.6 task-list 映射

| ID | 关系 |
| --- | --- |
| `IMP-051` | 本功能点 |
| `PLN-035` | 规划入账 → 已完成 |
| `DEV-097` | 实现 |
| `DOC-114` / `DOC-116` | 2608 入账与用户文档 |
| 关联 | IMP-047（对话框与 `sourceDir`）；不替代 IMP-048 |

### 6.7 落地实现摘要

**架构**：前端 `isLocalhostAccess()` 门禁 + 服务端 `_is_localhost_client` 双检；禁止浏览器 `webkitdirectory` 假路径。

**关键代码**：

1. `pick_directory()`：darwin → osascript；linux → zenity 优先否则 kdialog；规范化去尾 `/`（根除外）。
2. API 鉴权后先判 loopback，再调 picker；`DirectoryPickerError.code` → HTTP 业务码。
3. 前端：成功回填 `folderImport.sourceDir`；`cancelled` 静默；其它 Toast。

### 6.8 V0.7.1 收口 — 导入 UX 护栏与 046/047 回归修补（已落地）

> **状态**：**已落地**（2026-08-06；版本 **V0.7.1**）。不新开 IMP 号：属 046/047/051 真机验收后的缺陷与体验收口。
> **台账**：`BUG-444`～`448`、`ADJ-042`～`043`、`OPS-101`、`DOC-117`。

#### 6.8.1 问题与方案

| 编号 | 现象 | 方案（已实现） |
| --- | --- | --- |
| **BUG-444** | `update_zip` 在 `apply_detection` 后强制 `status=old`，pending 修好源再更新仍卡 pending | 去掉强制覆盖，保留 `apply_detection` 的 pending→stopped |
| **BUG-445** | 管理页 `pending` 时禁用「从源更新」，无法自愈 | folder 更新按钮改用 `updateBusy`（不含 pending）；启动仍禁用 |
| **BUG-446** | `manager on/start` 打印 token 早于 lifespan 轮换，冷启动可打印已失效 token | lifespan `yield` 前同步 `maybe_rotate`；CLI 打印前 rotate + `read_token` |
| **BUG-447** | `_write_token` 非原子 `O_TRUNC`，轮换窗口可读残缺 | temp(0o600) + `os.replace` |
| **BUG-448** | 纯中文名 `slugify` 全落到 `instance`，重导冲突 | `slug_basis_for_id`；`import_zip(..., id_basis=文件夹名)`；冲突文案去 CLI 腔 |
| **ADJ-042** | 识别失败（pending）管理页冒充成功 | `describeFolderImportOutcome`：error toast、对话框保持打开、提示改选根/`dist` |
| **BUG-449** | 同上函数误把「识别成功但档位 medium/heavy 不自动启动」（`auto.action=pending`、`status=stopped`）报成「未能识别/请删除」 | 仅 `status==="pending"` 才报未识别；档位高未自动启动 → 成功 toast「已导入 + 档位说明」，可手动启动 |
| **ADJ-043** | 失败后才提示勿选 `src/`；错误带 `[ZIP_IMPORT_ERROR]`；导入中 `lwa update` 打断请求 | ① 对话框**提前** hint；② API/`friendlyApiMessage` 剥错误码前缀；③ `import_activity` 锁 + `lwa update` 重启前等待空闲（约 180s，超时跳过重启） |

#### 6.8.2 实现触点

| 能力 | 模块 |
| --- | --- |
| 导入活动锁 | `import_activity.py`（`run/import.lock`，可重入）；`importer` 的 import/update（含 from_dir）持锁 |
| update 等空闲 | `updater.run_update`：重启 manager/daemon 前 `wait_until_import_idle` |
| 管理页文案 | `helpers.js`：`describeFolderImportOutcome` / `friendlyApiMessage`；`app.js` 对话框 hint + `apiFetch` |
| ID 回退 | `importer.slug_basis_for_id` / `import_from_dir` 传 `id_basis` |

#### 6.8.3 验收要点

- 只选 `src/` → pending：错误 toast，对话框不关，可改路径重试；删除 pending 实例后可再导。
- 识别成功但档位 medium/heavy（`autoStart.action=pending`、`status=stopped`）：**成功**提示「已导入 + 不自动启动说明」，**不得**引导删除（BUG-449）。
- 纯中文显示名 + 英文文件夹名 → 实例 ID 取文件夹 basename，不撞成万能 `instance`。
- pending 文件夹实例仍可点「从源更新」；识别成功后可启动。
- 导入进行中执行 `lwa update`：等待或跳过重启，不静默杀掉半成品导入。
- 管理页错误不再展示裸 `[ZIP_IMPORT_ERROR] …` 前缀（`error.code` 仍在 JSON）。

#### 6.8.4 task-list 映射

| ID | 关系 |
| --- | --- |
| `BUG-444`～`449` | 回归缺陷（含 medium 档误报未识别） |
| `ADJ-042` / `ADJ-043` | UX / 运维护栏 |
| `OPS-101` | 版本升至 V0.7.1 |
| `DOC-117` | 用户文档与 Skill 同步 |
| 关联 | IMP-046 / 047 / 051 |

---

## 7. IMP-048 — ZIP 包模式 ↔ 文件夹源模式自由转换（后续待办）

> **状态**：**后续待办 / 明确不在 IMP-046、IMP-047 同期实现**（2026-08-06 记入）。找到合适时间点再规划与开发。

### 7.1 需求（占位）

允许已存在实例在两种来源模式间切换，例如：

- zip 源实例 → 绑定本机文件夹，之后用文件夹更新；
- 文件夹源实例 → 改为仅保留 `original.zip` / inbox zip 更新路径，解除对源目录的依赖。

### 7.2 边界（现阶段）

- **不做**自动双向同步设计；**不做**与 047 抢同一迭代带宽。
- 047 落地时预留 `sourceKind` 字段即可，转换状态机、UI、数据迁移留待本项。

### 7.3 规划时必须回答

1. 转换是否保留 `data/`、别名、端口、desiredState。
2. 转换后「更新」入口如何切换且不误导。
3. 源目录删除后文件夹→zip 的强制降级是否自动。

### 7.4 task-list 映射

| ID | 关系 |
| --- | --- |
| `IMP-048` | 本功能点（后续） |
| `PLN-031` | 后续待办入账（见 task-list） |

---

## 8. 合集 Task 11 移植 — IMP-049 / IMP-050（优先级：中 · 不着急）

> **来源**：[`实施计划合集` · 工作区迁移 Task 11](../achievement/local-webpage-access-实施计划合集-20260804.md#2026-07-29-工作区迁移workspace-relocate实施计划)（原标 P2）。**主路径 Task 1～10 已落地**；本节约两项为合集迁入本文件的残留。**跨盘 IMP-042.b 不纳入本文件、暂不开发。**
> **状态**：**后续待办**。
> **优先级：中（不着急）** — 属减债/生产加固，边际收益低于本月主线；**先做 IMP-046 / IMP-047 / IMP-051**，本两项不排期抢档、不阻塞发布。真痛（频繁绝对路径残留 / 多工作区正式部署）再开干。

### 8.1 IMP-049 — 工作区派生路径相对化写入

> **优先级：中 · 不着急**（原合集 P2）。

| 编号 | 决策点（待拍板） | 倾向 |
| --- | --- | --- |
| **049.a** | 哪些字段相对化 | 优先 `appPath` / compose / dockerfile / gateway 片段内可生成路径；外部 `sourceZipPath` 可保持绝对 |
| **049.b** | 与 IMP-045 | 相对化是治本；doctor/漂移自愈仍保留作诊断 |
| **049.c** | 迁移 | 旧绝对路径实例：读取时兼容，写回时逐步相对化 |

**目标**：manifest / registry / 生成配置尽可能写**相对工作区根**的路径（或可重算字段），减少 `workspace relocate` 与裸 mv 后的 rebind 面。

**验收（草案）**：新导入实例关键派生路径可相对工作区根重算；同卷 relocate 后需改写的绝对路径条目显著减少。

| ID | 关系 |
| --- | --- |
| `IMP-049` | 本功能点 |
| 合集 Task 11 第 1 条 | 路径相对化写入 |
| `PLN-032` | 规划入账（优先级：中） |

### 8.2 IMP-050 — 生产 CLI 与工作区解耦安装

> **优先级：中 · 不着急**（原合集 P2；多工作区/正式部署时再优先于 049 亦可）。

| 编号 | 决策点（待拍板） | 倾向 |
| --- | --- | --- |
| **050.a** | 推荐布局 | 全局或用户级 venv + `lwa --workspace` / 环境变量指向工作区 |
| **050.b** | 与 autostart | 单元内 Python 绝对路径可指向解耦安装；工作区仅作 `--workspace` 参数 |
| **050.c** | 与 editable 开发 | 开发机仍可用 editable；生产文档与 `setup` 脚本走解耦路径 |

**目标**：安装/升级 `lwa` CLI（pip / venv / 入口脚本）与**具体工作区目录**解耦，避免「工具装在工作区树内 / editable 死钉旧路径」导致 relocate 后难用。

**验收（草案）**：按文档安装后，移动/relocate 工作区无需重装 CLI；`autostart repair` 只改 workspace 参数与生成配置。

| ID | 关系 |
| --- | --- |
| `IMP-050` | 本功能点 |
| 合集 Task 11 第 2 条 | 生产 CLI 与工作区解耦安装脚本 |
| `PLN-033` | 规划入账（优先级：中） |

---

## 9. 其它候选与追加模板

### 9.1 从待改进记录带入

| 候选 | 来源 | 优先级 | 备注 |
| --- | --- | --- | --- |
| **IMP-029** 资源采集接入周期任务 | [`待改进功能点记录-20260706.md`](../achievement/待改进功能点记录-20260706.md) | P2 | 不与 046/047/051 抢档；另排期时展开 |

### 9.2 运维验收（非新功能）

| 项 | 文档 |
| --- | --- |
| 033.13 / 035.06 / 036.08 | `docs/acceptance-checklist.md` |

### 9.3 追加模板

```markdown
## N. IMP-0XX — <一句话标题>

> **状态**：待规划 / 规划中 / 开发中 / 已落地（DEV-0xx）。

### N.1 需求
### N.2 关键决策
### N.3 现状触点
### N.4 WBS（可执行）
### N.5 验收标准
### N.6 task-list 映射
```

---

## 10. 编号与文档约定

| 约定 | 说明 |
| --- | --- |
| 月度文件 | `design/plans/local-webpage-access-新增功能点YYMM.md`（本文件；进行中权威位置） |
| IMP 号 | 全局递增不复用；046/047/**051 已落地**（含 §6.8 V0.7.1 收口）；048=后续；049/050=**优先级中 / 不着急**；042.b 不在本文件；**059/060/063 已落地（§14/§15，2026-08-17 事故复盘 + 当日实现）**；061 已落地（P1）；062=P2 占位（doctor 消费方，探测能力复用 063 `--check`） |
| 与 task-list | 规划 `PLN-`；开发 `DEV-`；文档 `DOC-` |
| 与 2607 | 7 月账本在 `design/achievement/`；本月只改本文件 |
| achievement | **暂不入账**：本月进行中的 2608 只放 `design/plans/`；收口归档后再迁入 `achievement/`（若需要） |
| 实施计划合集 | 历史已落地计划归档；未做项以本文件为准 |

---

## 11. Agent 部署复盘（家庭图书 / 2026-08-09）→ IMP-052 / BUG-455 / BUG-456

> **触发**：Agent 将 `home-bookshelf` 的 `backend/` 导入 lwa，出现多重启停、管理页看不到实例、`uvicorn main:app` 误判、缺 alembic 迁移致 API 500、两工作区抢 17800。
> **依据**：`~/lwa-workspace/logs/lwa.log`（04:42～04:48 三次 `host_start` + 17800 占用错误）与原工作区 `runtime/logs/lwa.log`（04:49 导入 `backend`）；`Scanner().detect(backend)` 实测 `entry.start = uvicorn main:app ...`（源仅有 `app/main.py` + `alembic.ini`）。

### 11.1 结论矩阵（缺陷 vs Agent）

| Claim | 判定 | 说明 |
| --- | --- | --- |
| FastAPI 启动猜成 `uvicorn main:app` | **设计缺陷 BUG-455** | `_python_start_command` 硬编码，不探测 `app/main.py` / 根 `main.py`；`src/main.py` 仅靠 Dockerfile `PYTHONPATH` 半修 |
| 不自动跑 `alembic upgrade head` | **产品缺口 → IMP-052** | V1 未承诺 ORM 迁移；但对 fullstack-sqlite + `alembic.ini` 会静默 500（表不存在），应自动前置迁移 |
| 管理页导入后需重启才见实例 | **Agent 误解** | `GET /api/instances` 每次读 registry；根因是 CLI 导入进了另一工作区 |
| 实例 ID = 文件夹名 `backend` | **设计行为** | `id_basis=目录名`；无 `--id`；ASCII `--name` 可驱动 ID；纯中文名回退 basename |
| 多重 `host_start` / 反复 recreate | **Agent 误用** | 改 start/alembic 后多次 `lwa start`；另有一次 `running→stopped` 后重拉 |
| 两工作区抢 17800；`manager off` 假停 | **部分缺陷 BUG-456** | 同工作区 stop 已加固；`state is None` 时直接成功，CLI 打「已停止」，而端口上仍是**另一工作区**管理页——易诱使 Agent 去 `kill` |
| 忽略项目自带 Dockerfile/entrypoint | **有意设计** | `docker/` 由模板统一生成；不复用源码 ENTRYPOINT（安全审计边界） |

### 11.2 IMP-052 — Python 启动命令推断增强（含 Alembic）

> **状态**：已落地（2026-08-09，DEV-098；BUG-455）。

**需求**

1. 探测入口优先级：`app/main.py` → `app.main:app`；`src/main.py` → `main:app`（保留现有 PYTHONPATH=src）；根 `main.py` → `main:app`；否则回退 `main:app`。
2. 顶层存在 `alembic.ini` 时，将 start 包成 `sh -c "alembic upgrade head && exec <uvicorn…>"`，并在 `notes` 提示已自动前置迁移。
3. 不自动复用源码 Dockerfile；用户仍可用手工 `entry.start` 覆盖。

**验收**：对仅有 `app/main.py`+`alembic.ini` 的 FastAPI 夹具，`Scanner.detect` 产出含 `app.main:app` 与 `alembic upgrade head`；回归 `test_scanner` / 相关 Dockerfile 用例全绿。

| ID | 关系 |
| --- | --- |
| `IMP-052` / `BUG-455` / `DEV-098` | 本项 / 入口硬编码缺陷 / 实现 |
| `PLN-036` | 规划入账 |

### 11.3 BUG-456 — `manager off` 跨工作区假停提示

> **状态**：已落地（2026-08-09，DEV-098）。

**需求**：`stop_manager` / `lwa manager off` 在本工作区已停（或无 state）时，若配置 `managerPort` 上仍有健康响应，CLI 不得仅绿字「管理页已停止」；须黄字提示可能为其他工作区占用，指引到对应工作区 `off` 或改 `managerPort`。

| ID | 关系 |
| --- | --- |
| `BUG-456` / `DEV-098` | 本项 / 与 IMP-052 同批 |

### 11.4 明确不修（本轮）

- 自动「合并」多工作区实例列表（与一工作区一心智冲突；见 IMP-050 解耦后再议）
- 新增 `--id`（体验增强，非本次故障根因；`--name` ASCII 已够）
- 复用源码 Dockerfile / ENTRYPOINT

### 11.5 IMP-053 — 已有 Runtime 时提示复用（防 Agent 另开第二套）

> **状态**：已落地（2026-08-09，DEV-099）。

**需求**：探测本机默认管理页（`:17800` `/api/health` → `workspaceRoot`）；`lwa init` 到**不同**目录时黄字软提示「请复用已有工作区」；skills（README / import-folder / setup-host）写明先 curl 再操作，禁止默认再 `mkdir ~/lwa-workspace && lwa init`。不硬拦多工作区。

| ID | 关系 |
| --- | --- |
| `IMP-053` / `DEV-099` / `PLN-037` | 本项 / 实现 / 规划 |

---

## 12. IMP-055 — 路径别名兼容性门禁（承接方案 B / 应用显式 base path）

> **状态**：主体于 2026-08-09 落地；2026-08-10 经 CHK-182～186 多轮复核补齐 BUG-467～469、别名感知 bundle URL、MIME 校验和负向样本；2026-08-11 经 CHK-190 / DOC-129 收紧门禁承诺与证据边界。详细时间线与最终测试矩阵见 [`路径别名兼容性问题发现与修复完整复盘-20260810.md`](./路径别名兼容性问题发现与修复完整复盘-20260810.md)。
> **关联**：CHK-180（别名链路诊断）、CHK-181（home-bookshelf 子路径方案评审）、BUG-465（容器 `/assets` 回退）、BUG-466 / DEV-101（设别名硬拦绝对资源）、BUG-467～469、prd-review vs home-bookshelf 对比。

### 12.1 结论：方案 B 谁改什么

**方案 B（应用侧显式、可配置的 base path）** 是应用具备路径别名兼容能力的基础方案；是否完整可用仍须以运行后 review 和真实 E2E 验收：

- Vite / 构建：`vite build --base=/<alias>/`（或等价可配置基址）
- Vue Router：`createWebHistory(import.meta.env.BASE_URL)`
- 前端 API 客户端：从 `import.meta.env.BASE_URL` 派生（如 `/home-bookshelf/api/v1`）
- **后端 HTTP 路由**在 Caddy `handle_path` 去前缀模型下**通常保持** `/api`、`/assets`，不必改成「相对路径」
- CLI / skills：可配置服务根 / API 前缀，而非笼统「相对地址」

| 责任方 | 是否立即改 | 做什么 |
| --- | --- | --- |
| **应用作者**（prd-review / home-bookshelf 等） | **是（已沟通）** | 按上表改前端资源/路由/API 基址；固定别名后明确「别名 URL 为正式 Web 入口，hostPort 直连前端可能不完整」 |
| **LWA 本仓** | **是，立即着手（本 IMP）** | **不改**业务应用源码；做平台门禁、探测增强、文档/skill 口径、收敛 BUG-465 假成功 |
| **LWA** | **否** | 不在网关长期用全局 `/assets` 回退冒充「别名已兼容」；不替无源码应用自动改包 |

对照三类结果（会话共识）：

| 类 | 含义 | 例 | LWA 动作 |
| --- | --- | --- | --- |
| **A** | 具备别名候选条件（资源等已相对或已带正确 base） | prd-review **页面壳**（`./js`…） | 允许设别名并进入运行后验收；仍可提示 API 若为绝对根路径 |
| **B** | 现不可用，显式 base path 后具备重新验收条件 | home-bookshelf（Vite `/assets` + `/api/v1`） | 命中入口 HTML 负向证据时硬失败并指向改造步骤；HTML 无法验证时标记未验证，运行后 review/E2E |
| **C** | 路径别名模型下无解（无源码/硬编码/要双入口全完整等） | 无法重建的绝对根 SPA | 已证实不兼容时拒绝并建议 hostPort/未来主机名；证据不足时不得假称兼容，标记未验证 |

**prd-review vs home-bookshelf（已核实）**：二者皆为 docker-compose；差别在 HTML——前者资源相对（A·壳），后者绝对（B）。二者 API 目前都偏绝对根路径；prd「能用别名」主要指不白屏，不等于 API 层已完美。

### 12.2 关键决策（已拍板）

| # | 决策 |
| --- | --- |
| **055.a** | 文案统一为「**显式、可配置的 base path**」，禁止把方案 B 泛称为「改成相对地址」；文档中 `base: './'` 仅作次优/补充说明，**最终推荐** `--base=/<alias>/` + Router/API 跟 `BASE_URL` |
| **055.b** | **恢复** docker-compose 的 IMP-023/BUG-466 硬拦截：能证明入口 HTML 含绝对资源则设别名失败；**撤销**「因 BUG-465 回退而对 docker-compose 跳过守卫」 |
| **055.c** | BUG-465 全局 `/assets`/`favicon` 回退：**降级为遗留/可选或默认关闭**，不得作为长期通用方案（多实例争抢 `/assets`；且管不住 `/api` 与 Router） |
| **055.d** | 设别名失败提示须含：改 `--base=/<alias>/`、Router/API 跟 `BASE_URL`、同步构建产物、或继续用 hostPort；对无源码点明属 C |
| **055.e** | `lwa access review` 增强：除静态子资源外，抽样绝对 `/api` 与「带别名前缀 API」对照；overall 不得在 API 根空 200 时假绿 |
| **055.f** | 本 IMP **不包含** 主机名别名实现（可另开候选）；文档可写「无源码时优先 hostPort / 未来主机名」 |
| **055.g** | **不**在本仓直接改 home-bookshelf / prd-review 源码（已交作者）；LWA 只改平台与文档 |

### 12.3 实施前基线触点（历史）

下表保存 IMP-055 开工前的基线，用于解释 WBS 为什么这样拆分；它不是 2026-08-11 的当前实现状态。当前能力和边界以 §12.8 及完整复盘为准。

| 模块 | 路径 | 当时状态 |
| --- | --- | --- |
| 设别名守卫 | `path_alias.py` `reject_alias_if_absolute_spa_assets` / `_fetch_entrypoint_html_for_alias_guard` | 有硬拦，但 **`runtime != DOCKER_COMPOSE` 才执行**（被 BUG-465 豁免） |
| 别名片段 | `static_gateway.py` `generate_alias_config` | docker-compose 追加 `@*_spa_assets` → `/assets/*` 等 |
| CLI 提示 | `cli/alias.py` | 成功后残余风险 cyan 提示；失败走 `RecognitionError` |
| 访问复核 | `access.py` `_check_subresources` / `review_access` | 主查绝对静态资源；**未**系统对照 `/api` |
| 文档 | `docs/known-limitations.md` / `docs/faq.md` | 已有 IMP-023；口径仍偏「相对 base / `./`」；与 055.a 未完全对齐 |
| Skills | `lwa-import-zip` 等 | 有 SPA 白屏提示；需改为显式 base path |

### 12.4 历史实施 WBS（主体已完成）

> 本节保留 2026-08-09 的原始任务拆分和依赖，便于追溯实施过程；主体已由 DEV-103、BUG-467～469 及后续复核收口，不应再按“当前待执行清单”理解。规模：S≤0.5d · M≈0.5–1.5d · L≈1.5–3d；原建议顺序为 **A → B → C → D → E**。

#### 阶段 A — 口径与失败文案（文档先行、与代码同批）

| ID | 工作包 | 规模 | 触点 | 交付物 | 依赖 | 完成标准 |
| --- | --- | --- | --- | --- | --- | --- |
| **055.01** | 三类结果写入计划/限制说明 | S | 本 §12；`docs/known-limitations.md` | A/B/C 表 + 「显式 base path」定义；标明方案 B 属应用、LWA 做门禁 | — | 读者能区分 prd（A·壳）与 bookshelf（B） |
| **055.02** | FAQ / 别名白屏条修订 | S | `docs/faq.md` | 推荐 `--base=/<alias>/` + Router/API；`./` 降为不推荐最终方案；固定 base 与 hostPort 取舍 | 055.01 | 无「只改相对地址即可」误导句 |
| **055.03** | Skill 提示对齐 | S | `skills/lwa-import-zip`、`lwa-build-frontend-static`、相关 README | 白屏规避改为显式 base path；勿写「前后端都改相对」 | 055.01 | skill 与 FAQ 同口径 |

#### 阶段 B — 设别名硬门禁（撤销 docker-compose 豁免）

| ID | 工作包 | 规模 | 触点 | 交付物 | 依赖 | 完成标准 |
| --- | --- | --- | --- | --- | --- | --- |
| **055.04** | 恢复容器绝对资源硬拦 | M | `path_alias.py` | 设别名（`alias is not None`）对 **SHARED_STATIC 与 DOCKER_COMPOSE** 均跑守卫；删除「BUG-465 故跳过」分支 | — | `test_path_alias_spa_guard`：容器含 `/assets` HTML → `RecognitionError`；不写 conf、不 reload |
| **055.05** | 失败文案升级 | S | `reject_alias_if_absolute_spa_assets` | 文案含：`--base=/<alias>/`、Router `BASE_URL`、API 派生、同步 static、hostPort 兜底；避免只提 `base: './'` | 055.04 | 单测 `match` 关键短语；CLI/管理页透出同一 `RecognitionError` |
| **055.06** | 探不到 HTML 时的行为 | S | `_fetch_entrypoint_html_for_alias_guard` + CLI | 仍不硬拦（无法证明）；成功路径 cyan/管理页提示「未验证入口 HTML」 | 055.04 | 既有 FakeGW/无监听测例仍绿 |

#### 阶段 C — 收敛 BUG-465 回退

| ID | 工作包 | 规模 | 触点 | 交付物 | 依赖 | 完成标准 |
| --- | --- | --- | --- | --- | --- | --- |
| **055.07** | 默认关闭 SPA 根路径回退 | M | `static_gateway.generate_alias_config` | 默认**不再**为 docker-compose 追加 `/assets/*` handle；可选：`config` 显式 `aliasSpaAssetFallback: true` 才开启（若保留逃生舱） | 055.04 | 新生成 conf 无 `@*_spa_assets`（默认）；单测锁片段内容 |
| **055.08** | 既有 aliases 片段迁移说明 | S | docs / `lwa gateway on` 或 alias set 重写路径 | 文档：旧回退片段在下次 `alias set`/rebuild 别名时消失；多实例争抢风险说明 | 055.07 | known-limitations 记「回退非长期方案」 |
| **055.09** | 回归测试调整 | S | `tests/test_static_gateway.py`、`test_path_alias_spa_guard.py`、lifecycle mocks | 删除/改写「容器因回退而允许绝对资源」断言；保留 handle_path 去前缀基线测 | 055.04–07 | 定向 pytest 全绿 |

#### 阶段 D — access review 增强（防假绿）

| ID | 工作包 | 规模 | 触点 | 交付物 | 依赖 | 完成标准 |
| --- | --- | --- | --- | --- | --- | --- |
| **055.10** | 绝对 API 对照探测 | M | `access.py` | 从入口 HTML/已知模式抽取或探测 `/api...`：根路径 vs `/<alias>/api...`；根空 200/失败且前缀成功 → finding + 降 overall | 055.01 | 单测：模拟 bookshelf 形态 → 非 overall=ok |
| **055.11** | 报告文案与 CLI | S | `access` 格式化 / `cli/access.py` | 提示属方案 B：改 API base；与静态 IMP-023 finding 区分 | 055.10 | `--json` 含稳定字段；人工可读一句原因 |
| **055.12** | （可选）深层路由抽样 | S | `access.py` | best-effort：`/<alias>/` 与一假路径刷新是否仍 HTML；做不到则文档标明未覆盖 | 055.10 | 有测或明确「未做」写在 known-limitations |

#### 阶段 E — 收口与台账

| ID | 工作包 | 规模 | 触点 | 交付物 | 依赖 | 完成标准 |
| --- | --- | --- | --- | --- | --- | --- |
| **055.13** | 管理页设别名错误展示 | S | `manager_static` / path-alias API | 硬拦错误完整展示（含改造建议），无「已保存」假成功 | 055.05 | API 测或前端静态断言 |
| **055.14** | 全量回归 + task-list | M | pytest / `task-list.md` | DEV/BUG/DOC 状态同步；§12 状态改「已落地」 | A–D | 全量或约定子集绿；摘要无漂移 |
| **055.15** | 对外说明（可选） | S | README 摘句 / operations-playbook | 一小节：路径别名前提 = 应用显式 base path；LWA 只门禁不代改包 | 055.01–03 | 与 FAQ 无矛盾 |

### 12.5 验收标准

1. 对运行中的 **docker-compose** 实例，入口 HTML 含 `/assets/...` 时，`lwa alias set` / 管理页设别名 **失败**，且文案指向显式 `--base=/<alias>/`（不再因 BUG-465 豁免而成功）。
2. 默认新生成的别名 conf **不含** 全局 `/assets` 回退（除非显式开启逃生舱）。
3. `lwa access review` 对「静态 OK、绝对 API 打到入口根空 200」类实例 **不得 overall=ok**。
4. 文档/skill 口径为「显式可配置 base path」，不再主推「整仓改相对地址」。
5. **不**修改 home-bookshelf / prd-review 业务仓；作者侧改造不在本 IMP 完成定义内。

### 12.6 task-list 映射

| ID | 关系 |
| --- | --- |
| `IMP-055` | 本功能点 |
| `PLN-038` | 本规划入账 |
| `CHK-180` / `CHK-181` | 诊断与方案评审来源 |
| `BUG-466` / `DEV-101` | 硬拦已有基础；本 IMP 撤销容器豁免并升级文案 |
| `BUG-465` / `DEV-102` | 回退实现；本 IMP 阶段 C 收敛 |
| `BUG-467`～`BUG-469` | 首轮落地后的 API 漏报、聚合/建议错误、导入门禁绕过；多轮复核与最终修复见完整复盘 |
| `CHK-182`～`CHK-186` / `TST-003` | 代码复核、实机正向 E2E、负向样本与最终 URL/MIME 回归矩阵 |
| `DEV-103` | 编码落地跟踪 |

### 12.7 明确不做（本 IMP）

- 在 LWA 内自动重写第三方前端构建 base
- 主机名别名产品化（另开 IMP）
- 强制应用同时支持「别名完整 + hostPort 根路径前端完整」
- 代 home-bookshelf / prd-review 合入业务 PR

### 12.8 2026-08-11 评审后的证据边界

设置前门禁的准确定位是「**入口 HTML 根绝对资源负向守卫**」，不能简称为“子路径兼容性检查通过”。结论按证据强度分层：

| 结果 | 可以说明 | 不能说明 |
| --- | --- | --- |
| 守卫命中根绝对 `src` / `href` | 已发现确定的不兼容证据，应阻止保存 | 项目修正后的全部业务一定可用 |
| 守卫未拒绝 | 已取得的入口 HTML 中未发现该类问题，或入口 HTML 无法验证 | 外链 bundle API、Router、深层路由、懒加载、WebSocket、写操作兼容 |
| `access review overall=ok` | 当前有界抽样未发现已知错位 | 所有 JS 数据流、角色、异常分支和业务写操作正确 |
| 真实 E2E 通过 | 已执行的 URL、资源、API 与路由场景可用 | 未执行场景同样可用 |

补充约束：

- 默认猜测的 `/api/`、`/api/v1/` 返回 401/404 只作诊断，不能单独判失败；只有从真实产物发现且带方法/预期状态的探针才可提高证据等级。
- import 阶段读取的解压目录 HTML 可能与构建或模板渲染后的生产入口不同，运行后必须复核真实入口。
- 复现实证时应记录 LWA commit/diff、Docker/Caddy 版本、项目样本 hash、实际 routeUrl 与验证时间；历史 pytest 数量只是现场快照。

---

## 13. IMP-058 — Scanner 多候选与实证校验架构摘要

> **状态（2026-08-11）**：Gate-A 原战术范围、IMP-057 Monorepo 分类、Gate-B 多候选识别和 IMP-056 兼容性预检的 MVP 已实现；Gate-A 修订后 SQLite 安全加固见 Scanner 文档 A.R01。修订版 Gate-C 已进入实施后收口：部署计划/能力契约模型、成功谓词、状态机、证据分级探针和模拟故障注入已有实现；生命周期改用完整计划、能力契约精确等价、用户确认闭环、完整事务回滚、实际副作用采集和真实 Docker 门控仍未满足最终退出标准。
>
> **完整设计**：[`Scanner架构设计分析-多候选与实证校验-20260811.md`](./Scanner架构设计分析-多候选与实证校验-20260811.md)；前置规则设计见 [`导入预检与Monorepo识别增强-20260810.md`](./导入预检与Monorepo识别增强-20260810.md)。

### 13.1 为什么要从即时判定升级

旧 scanner 遇到第一个高置信信号就立即产出单套配置，容易把“文件存在”直接推导成“完整部署方案”。在 `backend/ + frontend/`、npm workspaces、迁移命令、SQLite 相对路径和项目自带 Dockerfile 等场景中，识别、构建、运行和验证之间缺少可追溯的证据链，最终只能在容器启动末端暴露错误。

新范式保留快速静态识别，但把推理拆为 Layer 0～4 五层流水线：

| 层 | 职责 | 核心产物 |
| --- | --- | --- |
| Layer 0 | 收集文件布局、依赖、脚本、Dockerfile、数据库等客观事实 | `ProjectEvidence` |
| Layer 1 | 当前从证据生成识别候选；Gate-C 再将组件与能力收敛为可执行计划 | `DeploymentCandidate[]`（当前识别证据）→ `DeploymentPlan[]`（运行时目标真源） |
| Layer 2 | build 前检查 COPY、命令、cwd、迁移和数据库路径 | `PreflightResult` |
| Layer 3 | 实际 build、start、必选探针并按事务切换等价计划 | `VerificationResult` |
| Layer 4 | 所有计划失败或需人工决策时，输出逐 attempt 诊断 | `DiagnosisReport` |

### 13.2 已定案的架构约束

1. **组件不等于候选**：frontend build 与 backend API 是同一 fullstack 计划中的协作组件，不得互相 fallback；static 只能作为诊断线索，不能把全栈项目降级为“能打开首页”的静态壳并宣告成功。
2. **替代计划必须能力等价**：计划需声明 UI、API、数据库、迁移和必选探针等 `CapabilityContract`。只有能力契约完全一致，才有资格进入 fallback 选择。
3. **成功必须有统一谓词**：build 成功、start 成功、全部 required probes 通过、观察到的能力覆盖能力契约，四项必须同时成立。首页 HTTP 200 不能替代 API/DB 证明。
4. **fallback 默认确认**：默认由 CLI/管理页展示等价计划并等待用户确认；只有启用 `auto-equivalent`、上一 attempt 回滚成功且没有不可验证的外部副作用时，才可自动切换。
5. **回滚必须声明范围**：容器、端口、manifest 和生成文件可由基础设施事务恢复；数据库 migration、消息发布和外部 API 写入不因 `docker down` 自动恢复。无法用隔离环境、事务或快照证明恢复时，必须停止自动 fallback。
6. **探针分级**：存活探针必选；项目声明或可靠发现的 readiness/API/DB 探针才可作为成功门槛；猜测 API 路径的 401/404 只产生诊断。
7. **战术修正不是终态模型**：Gate-A 的 `sh -c` 包装和 SQLite 环境变量注入只处理有界形态；长期应使用结构化 `CommandSpec`、明确 workdir/env/pre_start，并验证应用确实消费目标数据库配置。
8. **缓存结论须靠指纹实证**：不能笼统归因“CMD 不参与缓存”。首次或 source/plan/generated-config/image 指纹变化后的 start 必须重新验证。

### 13.3 当前实现记录与修订后口径

| 条目 | 已有记录 | 2026-08-11 修订后口径 |
| --- | --- | --- |
| DEV-106 / Gate-A | Layer 2 静态预检原战术范围已实现 | 保留为战术护栏；字符串 shell 仅处理有界形态，SQLite 自动修正须按 Scanner A.R01 证明配置消费点、原文件名和持久卷 |
| DEV-107 / IMP-057 | Monorepo 包分类和主包选择已实现 | 作为 Layer 1 的规则集，不取代完整部署拓扑 |
| DEV-108 / Gate-B | 证据收集、子目录探测、扁平候选已实现 | `deploymentCandidates` 仅是过渡期识别证据，不是可直接自动 fallback 的列表 |
| DEV-109 / IMP-056 | 兼容性预检已实现 | 与 IMP-055 硬门禁分层，不把 advisory 结果冒充运行成功 |
| DEV-110 / 初版 Gate-C | 候选降级、诊断和 API 探测已有实现记录 | 仅作历史实现基础，不代表修订架构最终验收 |
| 修订版 Gate-C | 计划/组件/契约模型、VERIFYING/DEGRADED/FAILED、证据探针、attempt 诊断与模拟故障注入已有实现 | **仍为进行中**：运行时必须从扁平候选迁移到完整计划；等价性必须比较完整契约；回滚和副作用必须产生真实证据；真实 Docker 门控不得跳过 |

### 13.4 修订版 Gate-C 完成门槛

- 用 `DeploymentPlan / DeploymentComponent / CapabilityContract` 收敛 Gate-B 扁平候选，禁止 fullstack → static。
- 状态机遵循 `VERIFYING → RUNNING / DEGRADED / FAILED`；必选探针失败不得提前写 `RUNNING`。
- 每次尝试具有独立 attempt ID；Prepare、Execute、Verify、Commit、Rollback 均保留证据。
- 非等价计划、回滚失败或 migration/外部写入无法验证恢复时，不触发自动 fallback。
- 故障注入至少覆盖 build、up、required/optional probe、rollback 和不可逆 migration；真实 Docker 门控验证指纹与容器状态。

### 13.5 后续 Agent 的 WBS 入口

本月文档只承担范围导航，不复制详细开发台账。后续实现统一从完整 Scanner 文档的 **C.R01～C.R07** 开始：

1. C.R01：生命周期改为以 `DeploymentPlan[]` 为唯一执行真源。
2. C.R02：以完整 `CapabilityContract` 比较替代计划等价性。
3. C.R03：补 CLI/管理页确认与幂等重试闭环。
4. C.R04～C.R05：完成 manifest/生成文件回滚与真实副作用台账。
5. C.R06：补 source/plan/generated-config/image 四类指纹。
6. C.R07：完成真实 Docker 故障注入门控。

Gate-A 的修订后 SQLite 安全加固从 **IMP-058.A.R01** 续接，可与 C.R01/C.R02 并行，但未验收前不得把「对所有 SQLite 统一注入」宣称为通用安全策略。

IMP-056/057 的后置体验与 workspace 生态扩展，则从《导入预检与 Monorepo 识别增强》§9 可选包 C 继续，不扩张已完成 MVP 的定义。

---

## 14. Ubuntu 实机 update 事故复盘（2026-08-17）→ IMP-059 / IMP-060 / IMP-061 / IMP-062

> **触发**：家庭服务器（Ubuntu，`~/lwa-workspace`，V0.7.9）执行 `lwa update` 升级 V0.7.11 时，发现 manager 与 caddy 进程在当日 11:18 机器重启后已丢失约半天（9080 别名入口失效、无人察觉）；update 因「原本未运行，跳过重启」未将它们拉起，accessReview FAIL 才暴露。
> **依据**：现场 update/doctor/autostart 输出、`lwa autostart install --with-caddy` 修复后全绿（0 FAIL 0 WARN）、以及 2026-08-17 对当前源码的逐点核验（下表行号均为 V0.7.11 工作树实测）。
> **复盘记录**：`CHK-219`；规划入账 `PLN-040`。

### 14.1 事故时间线（现场事实）

| 时间 | 事件 | 结果 |
| --- | --- | --- |
| 08-17 11:18 | 机器重启（以 mihomo systemd 服务启动时间为证） | docker 3 实例靠 `restart: unless-stopped` 自愈（home-bookshelf 直连 18002 全程 200）；mihomo 靠 systemd 自愈；**manager / caddy 为裸进程，无自启，就此消失** |
| 11:18 → 晚间 | 9080 别名入口失效 | **约半天无人察觉**：无监管拉起、doctor 默认不查服务运行态、无任何通知机制 |
| 晚间 | 用户主动 `git pull`（走 mihomo 代理）+ `lwa update` | 版本升至 V0.7.11 成功；但 manager/gateway 报「原本未运行，跳过重启」→ accessReview FAIL |
| 随后 | 人工排查 + `lwa autostart install --with-caddy` | systemd user 单元拉起 daemon/manager/gateway 三服务；别名 200 / API 200，doctor 全绿 |
| 结论 | 该机从未安装过 autostart（可选功能，init/setup 不引导） | 裸进程模式在机器重启后**必然**复现此故障 |

### 14.2 根因矩阵（缺陷 vs 代码定位）

| # | 缺陷 | 代码定位 | 定性 |
| --- | --- | --- | --- |
| 1 | 自有服务默认 detached 裸进程、无监管：机器重启或崩溃后无人拉起 | `daemon.py:1109-1134` `_spawn_watcher`、`manager_service.py:196-232` `_spawn_manager` 均 `Popen(start_new_session=True)` 后即返回；caddy 仅 `caddy start` 自守护。唯一的前台监管入口 `gateway_service.py:513-585`（每 10s 探活）只被 autostart 单元使用 | **设计盲区**：监管被设计为可选外挂，但缺省路径毫无韧性 |
| 2 | 故障不可见：服务断了半天，唯一发现方式是用户恰好跑 update | `doctor.py:1613-1645` 15 项检查中**无**「manager/daemon 是否在运行」（仅 caddy_health 碰网关）、**无**「是否具备自启/重启韧性」；`autostart run_check`（`autostart.py:1542-1623`）与 doctor-hints 存在但**未接入** `run_doctor` 主报告；全仓库无通知机制 | **设计盲区**：可观测性只覆盖环境与实例，不覆盖自有服务 |
| 3 | update 观察态误判：「用户主动停」与「意外死亡」被当成同一种状态 | `updater.py:481-483`（manager）/ `545-547`（daemon）/ `586-591`（gateway）仅看运行时 `is_running`，**忽略** `run/daemon.json`、`run/manager.json`、`run/gateway.json` 持久化的 `enabled=true` 意图 | **模型缺口**：update 只有观察态，没有期望态 |
| 4 | autostart 可选且缺省不全：三个「要记得加的参数」缺一即有洞 | `autostart.py:91-98` `select_services`：gateway 单元需 `--with-caddy`（本次缺的正是 caddy）；`autostart.py:765-771` `install(linger=False)` 默认不 linger（需 `--linger`）；`lwa init` 仅 `maybe_start_manager`，不提示 autostart | **缺省值不安全**：合法但脆弱的模式成为默认 |
| 5 | （对照组）实例级有完整期望态自愈，服务级没有 | registry `desired_state`（`registry/dao.py:177/240/264-266`）+ daemon `reconcile` 每 60s（`daemon.py:64-66`、`754-925`）+ compose 模板固定 `restart: unless-stopped`（`compose.py:65`）——正是三个容器全活的原因 | **同构反差**：对「别人的进程」有期望态管理，对「自己的进程」没有 |

### 14.3 IMP-059 — update 服务级期望态 reconcile：enabled 但未运行的自有服务自动拉起（P0）

> **状态**：**已落地**（2026-08-17，DEV-118；059.01-06 全部完成）。**一句话**：update 的重启段从「was_running 才重启」升级为三态 reconcile，使 `lwa update` 顺带成为故障恢复点。实现触点：`service_intent.py`（意图判定纯函数 + 中断时长估算）、`updater.restart_manager/daemon/gateway`（三态决策 + unexpectedDown/downSince 字段）、`autostart.coordinated_start`（监督器拉起，无双进程）、CLI `--no-reconcile`。测试：`tests/test_service_intent.py`（18 用例）。

**需求**

1. 对 daemon / manager / gateway 三服务，重启决策由观察态单值改为三态：
   - **运行中** → 重启（现行为不变，保留 BUG-191 监管器协调 `coordinated_autostart_restart`（`cli/_common.py:75-91`）与 BUG-451 版本一致性校验）；
   - **enabled=true 且未运行** → **拉起**，报告中明确标注「意外未运行（上次心跳/启动于 X 前），已恢复」，与正常重启区分；
   - **enabled=false**（gateway 另含 `staticGateway!=caddy`） → 跳过，文案不变。
2. 中断时长估算：读状态文件 `started_at` / daemon 锁心跳时间与当前时间之差；不可得时只说「意外未运行，已恢复」。
3. 拉起复用现有 `start_manager` / `start_daemon` / `start_gateway`；autostart 在管时优先 `systemctl --user start`（与现有协调逻辑同一通道），不产生双进程。

**关键决策（拍板默认）**

- **意图来源就用现有持久化字段**（`run/*.json` 的 `enabled` + `config.managerEnabled/staticGateway` 交叉校验），不新增配置项——数据早已存在，缺的只是消费方。
- 语义与实例级 `desired_state` reconcile 对齐：服务级与实例级同构，降低理解成本。
- reconcile **默认开启**（update 的语义就是「收敛到应有状态」），提供 `--no-reconcile` 逃生舱供排障。
- 不掩盖事实：拉起动作必须在 update 报告里可见（含中断时长），避免「悄悄修好」造成第二次不可见。

**现状触点**：`updater.py:481/545/586` 三处判断；`manager_service.py:182-193`、`daemon.py:364-383`、`gateway_service.py:135-150` 的 `is_running`（已含 enabled 语义，可直接复用拆分）；`registry/dao.py` desired_state（先例）。

**WBS（可执行）**

| ID | 工作包 | 规模 | 触点 | 交付物 | 依赖 | 完成标准 |
| --- | --- | --- | --- | --- | --- | --- |
| **059.01** | 意图判定纯函数 | S | 新增 `service_intent(ws, config) -> {daemon,manager,gateway: enabled|disabled|n.a.}` | 纯函数 + 单测（enabled 文件缺失、config 交叉不一致、staticGateway 非 caddy） | — | 无 IO 副作用；三服务全矩阵覆盖 |
| **059.02** | 三态重启决策 | S | `updater.restart_manager/daemon/gateway` | 决策改为 `intent × is_running`：running→重启 / enabled+停→拉起 / disabled→跳过 | 059.01 | 现有「原本未运行」文案仅出现在 disabled 分支 |
| **059.03** | 拉起与监管器协调 | M | `start_*` / `coordinated_autostart_restart` | enabled+停时：autostart 在管走 `systemctl --user start`，否则 detached start；含 BUG-451 版本校验 | 059.02 | autostart 在管场景不出现双进程/双 watcher |
| **059.04** | 中断时长与报告 | S | 状态文件 `started_at` / 锁心跳 | 「意外未运行（中断约 X），已恢复」文案 + JSON 字段 `unexpectedDown: true, downSince` | 059.02 | kill 服务后 update 输出含该标注；正常重启不出现 |
| **059.05** | `--no-reconcile` 逃生舱 | S | `cli/system.py` update 选项 | flag 透传；help 说明仅排障用 | 059.02 | flag 下行为回到纯观察态 |
| **059.06** | 回归与收口 | M | `tests/test_updater*.py` | 场景回归：三态 × 三服务 × autostart 在管/不在管 | 059.01-05 | 定向 pytest 全绿；DEV 关闭；本节改「已落地」 |

**验收标准**

- 模拟 enabled=true 且进程已死（kill 后）→ `lwa update` 拉起并在输出标注「意外未运行，已恢复」；accessReview 由 FAIL 转绿。
- enabled=false → 仍跳过（文案不变）；`staticGateway!=caddy` 时 gateway 仍跳过。
- autostart 在管 + enabled 未运行 → 走 `systemctl --user start`，无双进程；版本校验仍生效。
- 全量 pytest 通过。

| ID | 关系 |
| --- | --- |
| `IMP-059` / `PLN-040` | 本功能点 / 规划入账 |
| `DEV-*` | 实施时按 059.01-06 开发项 |

### 14.4 IMP-060 — doctor 增设服务运行态与重启韧性检查（P0）

> **状态**：**已落地**（2026-08-17，DEV-118；060.01-04 全部完成）。

> **落地后修订（2026-08-18，CHK-224）**：060.02 场景 2 由「只查 gateway 单元缺失」泛化为
> **enabled_services 与 installed_services 逐项差集**（daemon/manager/gateway 任一缺失均 WARN，
> 部分安装不再误报 OK）；`service_runtime_state` 新增**反向不一致**检查（已停用但进程残留 → WARN，
> 建议 `lwa X off`，不假绿）；另按 CHK-223 增补单元「已装未启用」检查（BUG-533）。**一句话**：让「服务断了」和「重启后会断」在 `lwa doctor` 里自己浮出水面。实现触点：`doctor.check_service_runtime_state`（FAIL 级）与 `check_restart_resilience`（四类 WARN：无单元 / 任一 enabled 服务缺自启单元（逐项差集）/ 无 linger / 容器 restart 策略不符，复用 `autostart.linger_enabled`；内部探测不接收注入 runner，见 CHK-225 高③④/BUG-544）；已接入 `run_doctor` 主报告与 `--json`。测试：`tests/test_doctor_service_checks.py`（19 用例，含 CHK-224 差集/残留与 CHK-225 runner 解耦回归）。

**需求**

1. 新检查项 **`service_runtime_state`**（FAIL 级）：对 daemon/manager/gateway 比对意图（enabled）与观测（is_running）：
   - enabled=true 且未运行 → **FAIL**，建议文含 `lwa manager on` / `lwa daemon on` / `lwa autostart check`；
   - 其余（enabled 且运行 / enabled=false）→ PASS（enabled=false 时附 INFO「已按意图停用」）。
2. 新检查项 **`restart_resilience`**（WARN 级）：评估「机器重启后能否自动恢复」：
   - 存在 enabled 服务但未装任何 autostart 单元 → WARN「机器重启后服务不会自动恢复，建议 `lwa autostart install --with-caddy --linger`」；
   - `staticGateway=caddy` 且网关在用、但 gateway 单元缺失（本次事故形态）→ WARN，指明 `--with-caddy`；
   - 已装单元但未 linger → WARN（复用 `autostart run_check` 既有判定，`autostart.py:1607-1614`）；
   - running 容器 restart policy 与模板期望 `unless-stopped`（`compose.py:65`）不符 → WARN 列实例 ID。
3. 实现上**复用** `autostart.run_check` / doctor-hints 的既有逻辑并入 `run_doctor` 主报告（当前二者互不感知）。

**关键决策（拍板默认）**

- 分级原则：**enabled 未运行 = FAIL**（当前就是故障）；**无自启/无 linger = WARN**（裸进程是合法模式，但脆弱——重启后会变成 FAIL）。
- 保持 doctor 本地、快速、直连（BUG-380 原则，`probe.py:23-24`）：韧性检查只读本地文件与 `systemctl --user show`，**不做网络探测**，不拖慢常规 doctor。
- 修复建议文案直接给本次事故的实证修复命令 `lwa autostart install --with-caddy --linger`。

**现状触点**：`doctor.py:1613-1645` 检查列表；`autostart.py:1542-1623` run_check；`autostart.py:91-98/765-771`（单元缺省）；`compose.py:65`；`CheckResult` 模型与 `--json` 契约。

**WBS（可执行）**

| ID | 工作包 | 规模 | 触点 | 交付物 | 依赖 | 完成标准 |
| --- | --- | --- | --- | --- | --- | --- |
| **060.01** | `service_runtime_state` 检查 | S | `doctor.py` 新增 `check_service_runtime_state` | 复用 059.01 意图函数与各 `is_running` | 059.01 | 三服务 × 三态单测全绿；FAIL 文案含恢复命令 |
| **060.02** | `restart_resilience` 检查 | M | `doctor.py` 新增，复用 `autostart.run_check` | 四类 WARN 场景（无单元/gateway 缺/无 linger/restart policy 不符） | — | 每类有单测；裸进程合法模式不误升 FAIL |
| **060.03** | 接入主报告 | S | `doctor.py:1613-1645` 列表 | 两项进入默认检查与 `--json` 契约；skills/FAQ 同步 | 060.01-02 | `lwa doctor` 输出新两项；JSON schema 更新有测试 |
| **060.04** | 回归与收口 | M | `tests/test_doctor.py` | kill 服务、卸 autostart、关 linger 三现场模拟回归 | 060.01-03 | 定向 pytest 全绿；DEV 关闭；本节改「已落地」 |

**验收标准**

- kill manager 后 `lwa doctor` → `service_runtime_state` FAIL 且建议含恢复命令。
- 未装 autostart、caddy 在用 → `restart_resilience` WARN 并给出 `--with-caddy --linger` 完整命令。
- 全绿环境（autostart 齐 + linger + 服务运行）doctor 输出**零新增 FAIL/WARN**（不引入噪声）。

| ID | 关系 |
| --- | --- |
| `IMP-060` / `PLN-040` | 本功能点 / 规划入账 |
| `DEV-*` | 实施时按 060.01-04 开发项 |

### 14.5 IMP-061 — 自启安装默认化与首次引导（P1）

> **状态**：**已落地**（2026-08-17，DEV-118；061.01-04 全部完成）。**一句话**：把「缺省不安全」反转成「缺省安全、显式退出」。实现触点：`autostart.resolve_with_caddy`（三态）+ `install(with_caddy=None, linger=None)` 缺省反转（双 flag 兼容，旧命令行为不变）；`maybe_offer_autostart_install`（init/setup 收尾 Linux systemd TTY 引导、非 TTY 零阻塞）；`service_supervision_mode` + `lwa autostart status --json`（services[].mode）+ `lwa status` 运行模式段；docs/autostart.md、README 同步。测试：`tests/test_autostart_defaults.py`（17 用例）。

**需求**

1. `lwa init` / `lwa setup` 收尾检测到 systemd（Linux）时**交互询问**是否安装自启（仿 IMP-031 Docker 安装询问模式；非交互 TTY 不阻塞，改打印后续命令提示）。
2. `autostart install` 缺省值调整：
   - `staticGateway=caddy` 时 gateway 单元**默认纳入**（`with_caddy` 缺省反转为 True，保留 `--no-with-caddy` 显式退出）；
   - linger **默认尝试**，失败降级 WARN 不失败（不再要求 `--linger`，保留 `--no-linger`）。
3. `lwa autostart status` 与 `lwa status` 标注每个服务的运行模式：「systemd 监管」/「裸进程（重启后不自动恢复）」。
4. WSL / macOS 行为不变（LaunchAgent 登录触发语义，见 IMP-030）。

**关键决策（拍板默认）**

- 反转 `with_caddy` 缺省是**行为变化**：以 typer 双 flag（`--with-caddy/--no-with-caddy`）平滑兼容，skills 与 `docs/autostart.md` 同步更新。
- **不强制安装**（保留临时/开发用途的裸进程模式）：引导 + IMP-060 的 WARN 收敛，不做硬拦。
- 交互询问仅出现在 TTY；Agent/脚本场景零阻塞（与 IMP-031 同一模式，避免破坏自动化）。

**现状触点**：`autostart.py:91-98`（select_services）、`autostart.py:765-771`（install 缺省）、`cli/autostart.py:30-47`（CLI flag）、`init_workspace.py:67-73`（init 不提示 autostart）、`cli/system.py:109-152`（setup --autostart）。

**WBS（可执行）**

| ID | 工作包 | 规模 | 触点 | 交付物 | 依赖 | 完成标准 |
| --- | --- | --- | --- | --- | --- | --- |
| **061.01** | 缺省值反转 | S | `cli/autostart.py` / `autostart.py` | with_caddy 默认 True（caddy 在用时）、linger 默认尝试 | — | 双 flag 兼容；旧命令 `--with-caddy --linger` 行为不变 |
| **061.02** | init/setup 首次引导 | M | `init_workspace.py` / `cli/system.py` | TTY 交互询问；非交互打印建议命令 | 061.01 | 自动化路径无阻塞；交互路径可跳过 |
| **061.03** | 运行模式标注 | S | `autostart status` / `lwa status` | 「systemd 监管 / 裸进程（重启后不自动恢复）」标注 | — | 两命令输出与 `--json` 均含 mode 字段 |
| **061.04** | 文档与回归 | M | `docs/autostart.md` / skills / `tests/` | 文档同步 + 缺省行为回归 | 061.01-03 | 定向 pytest 全绿；DEV 关闭；本节改「已落地」 |

**验收标准**

- 全新 Ubuntu + systemd 环境 `lwa init`（交互）→ 引导安装后 `lwa autostart check` 全 PASS 含 gateway 单元与 linger。
- `lwa autostart install` 不带任何 flag 在 caddy 环境装出 gateway 单元；`--no-with-caddy` 仍可排除。
- 非交互（管道/CI）下 init/setup 不挂起、不装，仅打印建议。

| ID | 关系 |
| --- | --- |
| `IMP-061` / `PLN-040` | 本功能点 / 规划入账 |
| `DEV-*` | 实施时按 061.01-04 开发项 |

### 14.6 IMP-062 — 版本滞后可发现性（P2 · 不着急，占位）

> **状态**：占位（2026-08-17）。事故机落后两个版本（V0.7.9 → V0.7.11）无人知晓，直到用户主动 update 才发现。

**需求（占位）**：`lwa update` 结束时或 `doctor --profile full` 中做**可选**远端版本检查（`git ls-remote` / GitHub API），落后即提示「有新版本可 update」；离线/失败静默 SKIP 不告警。

**规划时必须回答**

1. 触发频率与缓存策略（避免每次命令都联网；建议仅 update 结束时 + 24h 缓存）。
2. 仓库地址可配置（fork / 私有镜像场景）。
3. 代理语义：远端检查**尊重** `https_proxy` 环境变量（本次 git pull 需走 mihomo 代理的实证）；内部本机/LAN 探测继续直连（BUG-380 原则不变，二者边界要写清）。
4. ~~`update` 文档补「源码更新需自行 git pull（可走代理）」运维提示~~——**2026-08-17 当日反转**：本条原依据「IMP-040 `--pull` 已删除不重开」，现该决策已被推翻，源码拉取与远端版本探测整体升级为 **IMP-063（§15，`lwa update` 一键 GitHub 更新通道）**；本 IMP 保留为 doctor 侧消费方，探测能力复用 IMP-063 的 `--check`。

### 14.7 明确不做（本轮）

- **不给 lwa 自带进程监管守护**（supervisor 化）：监管交给 systemd / LaunchAgent，lwa 保持「被监管者 + 协调者」定位（BUG-191 已确立此边界，不推翻）。
- **不做 update 自动 git pull**：IMP-040 `--pull` 已于 2026-07-20 从计划删除（DEV-085 关闭）；人工 pull 是预期流程。**【2026-08-17 更新：本条当日晚间被用户决策反转，重开为 IMP-063（§15）；「不做」范围收缩为 15.8 所列（不 clone、不自动回滚、不代操作非 ff merge/stash 等）。】**
- **不做通知推送**（webhook/邮件/IM）：先靠 IMP-059/060 把可见性补齐；通知渠道待有真实需求再议（家庭单人场景，doctor/CLI 输出足够）。
- **不强制消除裸进程模式**：保留开发/临时用途，靠 IMP-060 WARN + IMP-061 引导收敛。
- **不合并三个 systemd 单元为单一 supervisor 单元**：分单元与现有 `coordinated_autostart_restart` 协调机制匹配，收益不明确，不动。

---

## 15. IMP-063 — `lwa update` 一键 GitHub 更新通道（fetch → 安全拉取 → 升级）

> **状态**：**已落地**（2026-08-17，DEV-119；063.01-12 完成，063.13 实机「需代理环境一键 V0.7.x→V0.7.y」待下一发布周期真机执行）。**一句话**：把「人工 `git pull`（可能要走代理）+ `lwa update`」两步收敛为 `lwa update` 一步，并让远端版本探测成为 update 的内置能力。实现触点：`update_source.py`（目标解析/双锁/九态/SourceCheckReport v1/固定 OID fetch/ff-only/恢复链）、`update_flow.py`（bootstrap 编排 + handoff v1 + pass_fds 锁继承）、`update_continuation.py`（新解释器 Runtime 后半段，缺 FD/协议/代码不符在任何写入前拒绝 exit 3）、`cli/system.py`（`--check/--no-pull/--remote/--ref` 与互斥分流）、`updater`（warning/hasWarnings、Runtime 后半段抽取共用）。测试：`tests/test_update_source.py`（41 用例，全部基于临时 bare remote 夹具，零外网）；全量 2143 passed。
> **落地后修订（2026-08-18，CHK-223/224/225/226）**：BUG-529～536 与 CHK-225 关联项（BUG-540～544）全部收口——锁忙不再进 Runtime 后半段且 **--no-pull / 非 git 路径全程持 workspace 锁**（§15.1.9，`acquire_workspace_only_lock`）；dry-run 不落 Registry；continuation 非零退出仍回传完整子报告（恢复链仅限真崩溃/超时/refused）；behindBy 改用 rev-list 真实总数；--check 对非 git 树结构化拒绝且不创建 .git/；doctor 韧性检查与单参注入 runner 解耦。
> **重开说明**：原 IMP-040 `update --pull` 曾于 2026-07-20 以「价值偏低 / 非刚需」从 2607 删除（DEV-085 关闭，编号已复用于 LAN 新鲜度）。**§14 事故实证推翻该判断**：事故机落后两个版本无人知晓（版本滞后不可见）、更新需人工记忆「先 pull 再 update」且 pull 需处理代理环境（TLS 直连失败、走 mihomo 才通）——这些摩擦与盲区正应由工具吸收，本 IMP 以新编号 063 重开，非恢复旧案。
> **规划入账**：`PLN-041`。

### 15.1 需求

1. **`sourceUpdate` 默认前置**：`lwa update` 在 pip 之前完成源码探测与安全快进；`--no-pull` 保留「不联网、仅用本地代码刷新 Runtime」的逃生舱。
2. **目标解析只信 upstream，不硬编码 `origin`**：
   - 缺省用 `@{upstream}` 解析当前分支的真实 `remote + refs/heads/<branch>`；
   - 用两个无歧义参数覆盖：`--remote <name>` 与 `--ref <branch>`；任一缺省值可从 upstream 继承；
   - **无 upstream 时必须同时给出 `--remote` 和 `--ref`**，显式目标可正常更新，不因缺 upstream 拒绝；只给其中一个则 `failed(target_incomplete)`；
   - MVP **不接受 tag/任意 commit**，避免「把当前分支快进到任意对象」的语义不清；remote/ref 不存在均结构化报错。
3. **单次 fetch，固定候选 OID**：执行 `git fetch --no-tags <remote> +refs/heads/<branch>:refs/remotes/<remote>/<branch>` 后立即从该 remote-tracking ref 记录唯一 `candidateHead`；展示、祖先关系判定和最终快进均针对该 OID，不再用会二次 fetch 的 `git pull`；通过时执行 `git merge --ff-only <candidateHead>`。
4. **安全边界（快进前置条件）**：
   - tracked 文件有本地修改 → `failed`，列出文件并提示人工 commit/stash；
   - HEAD 领先或与 candidate 分叉 → `failed`，不 merge/rebase/强制重置；detached HEAD → `failed`；
   - untracked 文件不做全量阻断；若与远端新增同名文件冲突，由 ff 阶段安全拒绝并转为可操作文案；
   - shallow clone 无法证明祖先关系 → `failed(history_insufficient)`，提示 deepen/unshallow，不把 unknown 冒充 diverged；
   - fetch 断网/代理/凭据/超时 → `warning`，不改工作树，继续用本地代码完成 Runtime 刷新；
   - 非 git 安装 → `skipped`，提示迁移到 clone + editable 安装；`.git` 存在但 git 不可执行 → `failed(git_unavailable)`。
5. **两阶段自更新，禁止新旧代码混跑**：旧进程只执行「目标解析 → fetch/快进 → `pip install -e .`」；只要 HEAD 发生变化，后续 skills/config/重启/review/doctor 必须交给**新 Python 解释器进程**。接力协议携带 `schemaVersion/oldHead/newHead/workspace/options/sourceUpdateResult`，新进程强制 `noPull + skipPip`防递归，最终合并为一份报告。
6. **关键依赖门控**：
   - 快进后 pip 或新进程接力失败时，停止源码依赖的后续步骤，不套用原「每步互不中断」语义；进入新进程后，各 Runtime 步骤仍沿用既有独立失败模型；
   - `--skip-pip` **只允许用于本轮不改 HEAD 的路径**（`--no-pull`、已是最新或 fetch warning）；若探测到 behind 且同时传入 `--skip-pip`，在快进前 `failed(skip_pip_conflict)`，工作树不变，提示移除该 flag 或显式 `--no-pull --skip-pip`。
7. **`--check` 与 `--dry-run` 分层**：
   - `lwa update --check [--repo ...]`：在加载 workspace/config/registry 之前走独立路径，**不要求已 init 工作区**；会 fetch，因而只承诺「不改工作树与 Runtime」，允许刷新 `.git` 远端跟踪元数据；
   - `lwa update --dry-run`：保持既有严格零写入承诺，**不联网、不 fetch、不取锁**，仅根据已有 tracking ref 预览，报告标 `fresh=false`；缺缓存 ref 时 `sourceUpdate=skipped`、整体退出 0，`extra={fresh:false,relation:"unknown",candidateHead:null,reason:"tracking_ref_missing"}`，人类文案明示「无法在零写入模式确定远端版本」；
   - `--check` 与 `--dry-run` 互斥。
8. **恢复辅助不冒充环境回滚**：仅在本轮已快进且 pip/接力/配置迁移/版本一致性等升级关键步骤失败时，报告 `oldHead` 与恢复指引；doctor/accessReview 等业务诊断失败不默认建议退代码。先要求复查 `git status`，工作树仍干净时才给经 shell 安全转义的 `git reset --keep <oldHead>` 建议，并明示重跑 pip/update 的完整恢复链；**不自动执行**。
9. **并发互斥**：可变更的 update 全程取 workspace 锁，源码阶段（含会 fetch 的 `--check`）另取 git common-dir 下的 repo 锁；完整 update 的锁顺序固定为 `repo → workspace`，忙时 fail-fast 并显示持有者。父进程通过 POSIX `pass_fds` 把已持有的 repo/workspace 锁 FD 传给 continuation，父子均不重新取锁，父进程等待子进程结束；任一进程存活时锁都不释放。continuation 入口不注册为公开 CLI，且缺少继承 FD/协议信息时在任何 Runtime 写入前拒绝。

### 15.2 关键决策（拍板默认）

- **默认拉取**：与 IMP-061「缺省安全、显式退出」同一哲学——update 的语义就是「升到最新」；风险由 ff-only + dirty 拒绝 + fetch 失败降级三重护栏兜住，而不是把风险转移给「用户记得加 flag」。
- **只信已固定候选的 fast-forward**：fetch 一次后固定 OID，仅执行 `git merge --ff-only <candidateHead>`；任何需要非 ff merge/rebase/reset 的状态都交给用户。
- **新进程接力是正确性门槛**：当前进程一旦更改了自身源码，不得继续执行依赖新 schema/新函数的后续步骤；这是 BUG-357「pip 后旧 `Config` 类仍驻留内存」的同类边界，不能只靠 `resolve_version.cache_clear()` 解决。
- **git 环境零托管**：代理与凭据全部复用 git 自身机制（`https_proxy` 环境变量实证有效；私有仓库由用户自配 credential helper / SSH remote），lwa **不内置**代理配置、不管理凭据、不存 token。
- **fetch 失败是降级不是故障**：家庭服务器可能长期内网/代理不稳，update 必须离线可用（与 IMP-062「检查失败静默 SKIP」同口径）。
- **dirty 判定口径**：仅 tracked 修改拒绝（`git status --porcelain` 排除 `??` 前缀）；避免「工作区里一个无关临时文件就永远升不了级」。
- **不做 tag/release 通道**：当前版本体系在 commit 主题（`V0.7.11-Build2888-20260814`），无 tag 规范；`--ref` 在 MVP 只解析远端分支，tag 化发布体系成熟后再议。

### 15.3 编排协议与报告契约

**两阶段时序**：

```text
旧进程（bootstrap，不加载 Config/Registry）
  repo/目标解析 → repo/workspace 锁 → fetch 固定 OID → 状态门禁
  → merge --ff-only <OID> → pip install -e . → 启动新解释器
                                                ↓ stdin/stdout handoff v1 + pass_fds 锁继承
新进程（continuation，重新 import 全部代码）
  重读 Config/Registry → skills/templates → migrate → 重启 → access → doctor
  → 回传子报告 → 旧进程合并最终输出与退出码
```

**StepResult 状态**：扩展为 `ok | warning | failed | skipped | pending`；`warning` 在人类报告用 `!`，计入 `hasWarnings` 但不计入 `hasFailures`。update 只有 `failed` 退出 1；纯 warning 退出 0。dirty/diverged/detached/target-incomplete/skip-pip-conflict/git-unavailable 属于 failed；远端不可达属于 warning；非 git 安装与 dry-run 缺缓存 ref 属于 skipped。

**`SourceCheckReport` v1**：`--check --json` 不复用必填 workspace 的 `UpdateReport`，独立输出：

```json
{
  "schemaVersion": 1,
  "repo": "/abs/repo",
  "status": "upToDate|updateAvailable|blocked|unavailable",
  "current": {"head": "...", "version": "0.7.9", "subject": "..."},
  "target": {"remote": "origin", "branch": "main", "head": "...", "version": "0.7.11", "subject": "..."},
  "relation": "equal|behind|ahead|diverged|unknown",
  "aheadBy": 0,
  "behindBy": 2,
  "behind": [{"head": "...", "subject": "..."}],
  "blockers": [],
  "truncated": false,
  "fresh": true,
  "checkedAt": "2026-08-17T23:00:00+08:00",
  "error": null
}
```

- commit 主题不含 `Vx.y.z` 时 `version=null`，人类报告降级为短 SHA + subject，不伪造版本号。
- 人类 behind 列表最多 20 条；JSON 最多 100 条并用 `truncated=true` 表示截断。
- `blockers` 承载 dirty/detached/ahead/diverged/history-insufficient 等已成功探测但不宜快进的原因；`error` 仅承载 `{kind,message,action}` 形式的探测失败。
- `--check` 退出码：0=成功完成探测（含 updateAvailable/blocked），1=本地仓库/参数不合法，2=远端不可达。IMP-062/doctor 消费时把 2 映射为 SKIP，不把网络问题升为 doctor FAIL。
- `UpdateReport` 保留 `versionBefore/versionAfter`兼容字段，`sourceUpdate.extra` 另携带 `oldHead/candidateHead/newHead/remote/branch/relation/aheadBy/behindBy/fresh`；显式 `--repo` 时版本对比以目标 repo 的 commit descriptor 为准。

### 15.4 现状触点（复用）

| 触点 | 位置 | 复用点 |
| --- | --- | --- |
| 源码根识别 | `updater.py:125-162` `locate_repo`（`--repo` > editable > git 根） | 直接复用；新增「是否 git 克隆」判定（`.git` 存在） |
| 编排骨架 | `updater.py:675` `run_update`（每步独立 StepResult）；dry-run 分支 `721-761` | bootstrap 置于加载 Config/Registry 之前；新增 handoff 关键门控，新进程内复用原后半段 |
| 版本解析 | `version_info.py:49-88` `resolve_version`（git log commit 主题 > metadata > 兜底）；`run_update:771` 已有 `cache_clear()` 先例 | 当前/候选均从固定 commit OID 取 subject 并尝试解析版本；不受 remote ref 后续漂移影响 |
| pip 安装 | `updater.py:168-197` `run_pip_install`（subprocess、超时、错误不吞） | 风格与超时模式照搬给 fetch/ff；pip 成功后用 `sys.executable -m local_webpage_access` 启动 continuation |
| CLI flag 全集 | `cli/system.py:465-509`（`--repo/--dry-run/--skip-pip/--no-*` 族） | 新 flag：`--no-pull`、`--remote`、`--ref`、`--check`；`--check` 必须在 `require_workspace` 前分流；behind + skip-pip 在快进前拒绝 |
| 报告契约 | `updater.py:75-119` `StepResult/UpdateReport` | 增 `warning/hasWarnings`；`sourceUpdate.extra`；独立 `SourceCheckReport` v1；handoff v1 |
| 新旧进程边界 | `updater.py:379-430` `run_migrate_config_defaults`（BUG-357） | 既有子进程先例证明旧类不可继续执行新 schema；本 IMP 升级为整个 Runtime 后半段接力 |
| 安装口径 | `README.md:42`（clone + `pip install -e .`）；`docs/release-checklist.md:58`（release zip 为辅通道） | 文档以克隆安装为主通道，zip 安装提示迁移 |

### 15.5 WBS（可执行）

> 规模：S≤0.5d · M≈0.5–1.5d · L≈1.5–3d。建议顺序：**A → B → C → D → E（文档可与 D 后半段并行）→ F**。git 测试统一用临时 bare remote + clone/worktree 夹具，禁止依赖外网与真实 GitHub。

#### 阶段 A — git 状态与探测（纯逻辑层）

| ID | 工作包 | 规模 | 触点 | 交付物 | 依赖 | 完成标准 |
| --- | --- | --- | --- | --- | --- | --- |
| **063.01** | 仓库与目标解析 | M | 新建 `update_source.py`：`resolve_source_target(repo, remote, ref)` | 识别 git/link-worktree/无 git；解析 HEAD/upstream；`--remote/--ref` 独立继承与无 upstream 全显式规则；拒绝 tag/commit | — | 全程无网络；返回结构化 target/errorKind |
| **063.02** | repo/workspace 互斥锁 | M | 新 update lock helper | git common-dir repo 锁 + workspace 锁；跨进程固定顺序、持有者信息、陈旧判定；FD 可继承 | — | 双 update 竞争时一个 fail-fast；check 取 repo 锁；`--dry-run` 不建锁 |
| **063.03** | 本地状态与关系判定 | M | `inspect_repo(repo, candidate=None)` | tracked/untracked/detached/shallow；有 candidate 时计算 equal/behind/ahead/diverged/unknown | 063.01 | 九态矩阵；shallow 历史不足不误判分叉 |
| **063.04** | 状态与报告基础契约 | M | `StepResult/UpdateReport` + 新 `SourceCheckReport` | `warning/hasWarnings`；source extra；check JSON v1、blockers/error、behind 截断、退出码；dry-run 缺 ref 契约 | — | 纯模型/格式化/序列化测试通过，后续无需自定义临时报告 |
| **063.05** | 远端探测与 `--check` | M | `fetch_candidate` + CLI 早期分流 | 取 repo 锁；显式 refspec 刷新 tracking ref 后固定 candidate OID；无 workspace 输出 `SourceCheckReport` | 063.01–04 | 不改工作树；JSON/退出码契约测试；remote 失败可判型 |

#### 阶段 B — 安全拉取

| ID | 工作包 | 规模 | 触点 | 交付物 | 依赖 | 完成标准 |
| --- | --- | --- | --- | --- | --- | --- |
| **063.06** | 固定 OID 快进 | M | `apply_fast_forward(repo, candidate, options)` | 起点门禁通过后 `git merge --ff-only <OID>`；behind + skip-pip 快进前拒绝；返回 old/new/descriptor；untracked 同名冲突可诊断 | 063.02–05 | 应用 OID 与预览 OID 严格一致；所有拒绝路径工作树不变 |

#### 阶段 C — 新进程接力

| ID | 工作包 | 规模 | 触点 | 交付物 | 依赖 | 完成标准 |
| --- | --- | --- | --- | --- | --- | --- |
| **063.07** | bootstrap/pip 门控 | M | CLI update 前置分流 + `update_source.py` | Config/Registry 加载前完成 sourceUpdate；快进后 pip 失败即阻断 continuation；未改 HEAD 时保留 skip-pip 旧语义 | 063.05–06 | 旧进程未加载运行态 schema；pip 失败不执行后半段 |
| **063.08** | handoff v1 与新进程 continuation | L | 内部 continuation 入口 + 原始子报告 | `sys.executable -m local_webpage_access` 非公开入口；stdin/stdout JSON；`pass_fds` 继承两锁；参数白名单/防递归；新解释器重读 Config/Registry 并返回 Runtime 子报告 | 063.02、04、07 | 后半段 import newHead 代码；缺 FD/协议不兼容/子进程崩溃在写入前可诊断；父死子活时锁仍有效 |

#### 阶段 D — 编排、CLI 与恢复指引

| ID | 工作包 | 规模 | 触点 | 交付物 | 依赖 | 完成标准 |
| --- | --- | --- | --- | --- | --- | --- |
| **063.09** | source/Runtime 报告整合 | M | `run_update/format_report` + 父子报告合并 | source step/extra + Runtime 子报告；warning 图标；dirty 等 failed 本地继续；快进后 pip/handoff 失败门控 | 063.04、07–08 | 单一最终报告的状态、版本、退出码与 JSON 一致 |
| **063.10** | flag/模式分流、dry-run 与恢复 | M | `cli/system.py` + 报告收尾 | `--no-pull/--remote/--ref/--check`；check 无 workspace；dry-run 缺 ref 的 skipped/exit0/extra 契约；check/dry-run 互斥；关键失败恢复链 | 063.05、07、09 | `--no-pull` 与旧基线兼容；skip-pip 组合全矩阵；doctor/accessReview 失败不误建议退代码 |

#### 阶段 E — 文档与 skills

| ID | 工作包 | 规模 | 触点 | 交付物 | 依赖 | 完成标准 |
| --- | --- | --- | --- | --- | --- | --- |
| **063.11** | 用户文档与 skills | M | `README.md` / FAQ / operations / `lwa-update-runtime` | 一键更新；check/dry-run 副作用边界；`--remote/--ref`；skip-pip 限制；代理/凭据；离线降级；非 git 迁移；恢复链 | 063.09–10 | 用户文档与 CLI/JSON/退出码一致；Skill 不再要求人工先 pull |

#### 阶段 F — 回归与收口

| ID | 工作包 | 规模 | 触点 | 交付物 | 依赖 | 完成标准 |
| --- | --- | --- | --- | --- | --- | --- |
| **063.12** | 自动化场景矩阵 | L | `tests/test_update_source.py` + `tests/test_updater*.py` + CLI 测试 | bare remote/clone/linked worktree/shallow；九态×fetch 成败×固定 OID；handoff 新旧代码证明；双 update 竞争；check/dry-run/JSON/退出码 | 063.01–11 | 全绿且不触外网；包含路径空格与超长 behind 截断 |
| **063.13** | 收口 | S | task-list / 本文档 / 实机 | DEV 关闭；§15 改「已落地」；需代理环境一键 V0.7.x→V0.7.y；无 workspace 实测 `--check` | 063.12 | 实机一次成功；新进程版本、source report、Runtime 报告一致 |

### 15.6 验收标准

- **主路径（事故场景复现）**：克隆安装、落后两个版本、需代理的环境——一条 `lwa update` 完成 fetch → 固定 OID 快进 → pip → **新进程接力** → 同步/重启/复核/doctor，报告含 `sourceUpdate ok（V0.7.9 → V0.7.11）`，全程无人工 git 操作。
- 已是最新：`sourceUpdate skipped（已是最新）`，其余步骤照旧。
- tracked 脏 / 分叉 / detached / 无 upstream 且未全量显式给出 remote+ref / shallow 历史不足：拒绝快进、工作树零改动、结构化 errorKind + 下一步指引；后续步骤以本地代码继续但整体退出 1。
- 无 upstream 但显式给出 `--remote <name> --ref <branch>`：正常探测/快进；不因本地未设 upstream 拒绝。
- 断网或代理失效：`sourceUpdate warning`，update 仍以本地代码完成全部步骤（离线可用）。
- `--no-pull`：步骤集合/状态语义/退出码与 V0.7.11 兼容（版本/时序/文案除外）；`--check`：无 workspace 可用、不改工作树与 Runtime；`--dry-run`：不联网/不 fetch/不取锁/零写入并标记 `fresh=false`。
- 快进后 pip 或 handoff 失败：不由旧进程执行 Runtime 后半段；恢复指引只在升级关键失败中出现。
- behind + `--skip-pip`：在快进前拒绝且工作树不变；`--no-pull --skip-pip`、已最新 + `--skip-pip`、fetch warning + `--skip-pip` 均保留旧语义。
- 两个 update 并发：仅一个获锁执行，另一个快速失败且不触碰 pip/进程/registry；父进程中途死亡时，continuation 继承的 FD 仍保持锁到子进程结束。
- 非 git 安装：`skipped` + 迁移指引，不自动 clone。
- 全量 pytest 通过，git 相关测试不依赖外网。

### 15.7 实施参数（集中常量，实机可调）

1. **fetch 超时缺省**：60s 起步还是 30s？（跨境代理慢网络 vs 卡死体验；可实施时按实机调，常量集中定义。）
2. **behind 截断**：人类 20、JSON 100，集中常量；须保留 `behindBy` 总数。
3. **锁等待**：默认 fail-fast，只读取持有者 PID/开始时间/工作区；暂不增 `--wait`。
4. **`--check` 缓存**：本 IMP 每次实查；24h 调度与缓存由 IMP-062 doctor 消费层负责，不污染 source check 原子能力。

### 15.8 明确不做（本 IMP）

- 不做自动 `git clone` 安装与非 git 安装的自动迁移（只提示）。
- 不做自动回滚（仅在关键升级失败时给 `status → reset --keep → pip/update` 人工恢复链）。
- 不做非 ff merge/rebase/reset/stash 代操作；不做 `--force` 类破坏性 flag。
- 不接受 tag/任意 commit 作为 MVP `--ref`；仅支持远端分支。
- 不内置代理与凭据管理（git 生态已有成熟机制，§14 事故实证环境变量代理即可用）。
- 不做 GitHub API / Release 附件通道（仅 git 协议；pack-release-zip 辅通道维持现状）。

| ID | 关系 |
| --- | --- |
| `IMP-063` / `PLN-041` | 本功能点 / 规划入账 |
| `DEV-*` | 实施时按 063.01-13 开发项 |
| 原 IMP-040（已删）/ `DEV-085` | 历史前案（2026-07-20 删除）；本 IMP 重开依据见 §15 引言 |
| `IMP-062`（§14.6） | 版本滞后 doctor 消费方，探测能力由本 IMP `--check` 提供 |

---

## 16. IMP-064 - 服务意图字段去污染：`enabled` 只表用户意图，启动失败进独立字段（P1 · 待实施）

> **状态**：**待实施**（2026-08-18 立项；CHK-230 / CHK-232 修订契约）。承接 CHK-225 设计裁决项「update 重启失败把 enabled=False 留盘、之后不再自愈」。
> **一句话**：`run/*.json` 的 `enabled` 回归纯用户意图；失败与进程退出只更新运行观测与 `lastStartError`；熔断只挡 IMP-059 的自动拉起，不挡手动 `on`、不改监督器。

### 16.1 背景与问题

`enabled` 同时承担了「用户意图」与「启动成功 / 进程还在」两个职责，而 IMP-059/060 恰好以意图字段为安全网。失败路径一旦把 `enabled` 写成 false，安全网失明。

**已核实的污染路径（2026-08-18 工作树，行号随改动会漂，实施以符号名为准）：**

| 路径 | 文件 | 行为 | 三服务是否同构 |
| --- | --- | --- | --- |
| `start_*` 健康检查 / 子进程握手失败 | `manager_service.py` `start_manager`（先写 `enabled=True` 再失败写回 False）；`daemon.py` `start_daemon` 同构 | 意图被改成关 | **gateway 不同构（反方向）**：`start_gateway` 在 `caddy_start` 失败时直接抛错（约 318–325 行），失败发生在首次 `write_state` **之前**，`enabled` 仍是 False——用户已 `on`，doctor 却报「已按意图停用」 |
| 用户级 `stop_*` | `stop_manager` / `stop_daemon` / `stop_gateway` | 成功停止即写 `enabled=False` | 同构。`stop_daemon` **先**写 False，再靠 watcher 读到 disabled 自退 |
| `updater.restart_*` 无自启动托管 | `updater.py` `restart_manager/daemon/gateway` | 走用户级 `stop_*` → `start_*`；另有版本不一致时的**二次** `stop_manager`（约 571 / 624 行） | 同构结构；manager 多一条版本校验 stop |
| 管理页子进程退出 | `manager_service.run_service_main` 的 `finally`（约 651–664 行） | SIGTERM / uvicorn 正常返回后，若 `state.pid == os.getpid()` 则写 **`enabled=False`** | daemon `_main` 退出**不**翻 `enabled` |
| 网关前台监管退出 | `run_gateway_foreground`（约 579–581 行）调用用户级 `stop_gateway`（约 446 行写 False） | 监督器 SIGTERM / 机器关机 → 优雅退出 → **`enabled=False`** | **CHK-232**：与 manager `finally` 同类污染；初版「gateway 无对等 finally」不成立，064.03b 必须覆盖 |
| `maybe_start_gateway` 联动 | 只看 `backend==caddy`（约 499–504 行），**不看** `enabled`；`start_manager`（约 451 行）无条件调用 | 用户已 `gateway off` 时，`lwa manager on` 仍会拉起 Caddy | 叠加 `start_gateway` BUG-073（约 267 行）：在线但 `enabled=False` 时补写 True |

后果：

- **stop 成功 + start 失败**（含内部杀进程后子进程 `finally` 抢写）后，`enabled=false` 留盘；
- IMP-059 `service_intent` 看到 false → 永久跳过拉起（「原本未运行」）；
- IMP-060 `check_service_runtime_state` 看到 false → 「已按意图停用」，**PASS 假绿**；
- 无法区分「用户主动 off」与「上次 update 重启失败遗留」，除非当次看到报错并手动 `lwa <svc> on`。

CHK-230：只改 `start_*` 失败分支、或只加「不写状态文件的 `_stop_process`」，**不够**——manager 子进程 `finally` 会把内部停止原语架空。
CHK-232：gateway 前台退出走用户级 `stop_gateway`、`maybe_start_gateway` 不查意图、gateway `on` 失败不落意图——三条与 manager/daemon 对称缺口，须写入规则与 064.03b / 064.02 / 064.06。

### 16.2 字段定义（`run/{manager,daemon,gateway}.json`）

```json
{
  "enabled": true,
  "pid": null,
  "lastStartError": {
    "message": "管理页子进程启动失败或健康检查超时（port=8443）",
    "at": "2026-08-18T04:12:33Z",
    "source": "update-restart"
  },
  "consecutiveStartFailures": 2
}
```

| 字段 | 语义 | 写入方 |
| --- | --- | --- |
| `enabled` | **仅**用户意图（「我要它开/关」）；旧文件兼容读 | 见下方写入契约；**禁止**失败路径与进程退出写 False |
| `pid` / `started_at` 等 | 运行观测 | `start_*` 成功、监督器入口回写自身 pid、内部停止清 pid |
| `lastStartError` | 最近一次启动失败的观测；`source` 取 `manual` / `update-restart` / `reconcile` / `autostart` | 任意 `start_*` 失败路径 |
| `consecutiveStartFailures` | 连续启动失败计数；**启动成功清零**（含已在运行早退），用户级 `off` 时重置 | `start_*` 失败路径 + `on` 成功 / 早退 + `off` |

**`enabled` 写入契约（CHK-230：初版「只允许 on/off 写」过严，会与现码冲突）：**

| 动作 | 允许 | 禁止 |
| --- | --- | --- |
| 写 `enabled=True` | 用户级 `lwa <svc> on`（三服务 `on` 入口**先断言 True 再启动**，见规则 1）；监督器前台入口回写自身 pid（`run_service_main` / `daemon._main` / `run_gateway_foreground` 启动成功后）；`autostart._prepare_*` 等安装期「置意图为开」 | 用「失败后再写 True」补救被污染的意图；**`maybe_start_gateway` / `start_manager` 联动**；BUG-073 在 `enabled=False` 且 Caddy 在线时补写 True |
| 写 `enabled=False` | **仅**用户级 `off`（`lwa <svc> off` → `stop_*`；`autostart disable` 成功后连带的 `stop_*`） | `start_*` 失败；内部停止原语；manager `finally`；**gateway 前台退出**；update restart / reconcile；版本不一致二次 stop |

旧状态文件无新字段：读侧默认 `lastStartError=None`、计数 0，**不做 schema 迁移**。

**存量污染（明示，不自动翻回）：** 本 IMP 落地前已写成 `enabled=false` 且无 `lastStartError` 的文件，与真·用户 off **无法区分**，064 **不会**自动改回 true。恢复靠用户 `lwa <svc> on`；FAQ / known-limitations 写清。禁止用启发式（「有 pid 残留就当失败」）翻意图。

### 16.3 业务规则

1. **on 入口先断言意图，start 失败不把意图改回关**：三服务用户级 `on`（及直接调用的 `start_*` 作为 on 实现）**先写 `enabled=True` 再执行启动**。健康检查超时 / 子进程握手失败 / `caddy_start` 失败——杀残留进程、**清 pid**、写 `lastStartError`、计数 +1、抛原异常；**不写 `enabled=False`**。gateway 现状是失败发生在首次 `write_state` 之前（CHK-232 遗漏 3），须与 manager/daemon 对齐，否则 `lwa gateway on` 失败后 doctor 仍说「已按意图停用」。
2. **内部停止 ≠ 用户级 stop**：三服务各有内部原语（如 `_stop_process`）——发信号结束进程（daemon/gateway 含停 Caddy master），写盘**只更新运行观测**（`pid=null`，保留 `enabled`）。**禁止**「完全不写状态文件」：留下 stale pid 会让 `is_running` / 子进程 `finally` 的 pid 匹配判断出错。
3. **内部停止的调用方必须换干净**：`updater.restart_*` 的主序列 **以及** 版本不一致时的二次 `stop_manager` / `stop_daemon` / `stop_gateway` 全部改内部原语。用户级 `off` 仍走 `stop_*`（写 `enabled=False` 并重置失败记录）。
4. **子进程 / 前台监管退出不得写意图（064.03b，否则规则 2 被架空）**：
   - manager：`run_service_main` 的 `finally` 在 `state.pid == os.getpid()` 时只清运行观测（pid / 能力缓存），**不得**写 `enabled=False`。
   - daemon：`_main` 现状不翻 `enabled`，加回归锁住、禁止向 manager 看齐写 False。内部停止必须 SIGTERM，不能再靠先写 False 赶 watcher。
   - gateway（CHK-232 遗漏 1）：`run_gateway_foreground` 退出**不得**调用户级 `stop_gateway`。改为内部停止（可先内联 `caddy_stop` + 清 pid、保留 enabled；064.03 再抽成共用原语）。监督器 SIGTERM / 关机后意图仍为开，与「用户没 off」一致。
   - pid 已被新进程覆盖时，旧进程 `finally` 仍靠现有 pid 匹配跳过。
5. **熔断只挡 IMP-059 自动拉起**：`updater.restart_*` 在 **reconcile 拉起前**（`enabled=true` 且未运行）判定 `consecutiveStartFailures >= 3` 且 `lastStartError.at` 距今 ≤ 24h → 跳过自动拉起，报告「连续失败 N 次，已暂停自动拉起，请 `lwa <svc> on`」。冷却过期（>24h）允许再试一次（计数保留；若再失败则按新 `at` 重新熔断）。**禁止**把熔断塞进 `start_*` 本身，否则手动 `on` 会被误伤。**不约束** systemd/launchd KeepAlive/JobRestart。**明示（CHK-232 次要）**：手动 `lwa <svc> on` 失败同样计入 `consecutiveStartFailures`（失败就是失败）；熔断只决定 update 是否自动拉起，不挡住这次手动 `on`。
6. **doctor 消费**：`enabled=true` 且未运行 → FAIL，文案追加 `lastStartError.message`（若有）并在熔断时明示。`enabled=false` → 「已按意图停用」（去污染后该判定才语义真实；存量污染仍可能假绿，见 16.2）。
7. **status 暴露**：`lwa status` / `--json` 与管理页服务面板透出 `lastStartError` 与 `consecutiveStartFailures`。
8. **三服务同步改、不假装已经同构**：manager 改 `start_*` + `stop_*` 调用方 + `finally`；daemon 改 `start_*` + 内部 SIGTERM 停止；gateway 改 `on` 先断言、`start_gateway` 失败不写 False、前台退出走内部停止、联动查意图。gateway 保留 `staticGateway != caddy` 的 n.a.。
9. **联动启动不得翻他人意图（CHK-232 遗漏 2）**：`maybe_start_gateway`（含 `start_manager` / `lwa init` 调用）在 `backend==caddy` 之后还须查 `service_intent`：gateway 为 disabled / n.a. 则**跳过**，不调 `start_gateway`。`start_gateway` 已在线分支（BUG-073）仅允许在**状态文件缺失**（`state is None`）时补写恢复态；**`enabled=False` 且 Caddy 在线视为残留进程，不把意图翻回 True**（与 IMP-060 residual WARN 同向）。
10. **「on 成功清零」含已在运行早退（CHK-232 次要）**：`start_*` 在「已在运行」早退（如 `manager_service.py` 约 400–403 行）也须将 `consecutiveStartFailures` 清零。否则 064.06 只覆盖「本次新拉起成功」，熔断计数会在健康重启场景残留。

### 16.4 现状触点

- `manager_service.py`：`start_manager` 失败写 False；约 400–403 行已在运行早退；约 451 行无条件 `maybe_start_gateway`；`stop_manager` 写 False；`run_service_main` 入口写 True、`finally` 写 False。
- `daemon.py`（**不是** `daemon_service.py`）：`start_daemon` 失败写 False；`stop_daemon` 先写 False；`_main` 抢锁后写 True，退出不翻 `enabled`；watcher 以 `enabled=False` 为外部 off 退出条件——内部停止必须 SIGTERM。
- `gateway_service.py`：`stop_gateway` 写 False；`start_gateway` 失败在首次 `write_state` 之前（on 不落意图）；BUG-073 已在线且 `enabled=False` 时补写 True；`maybe_start_gateway` 只看 backend；`run_gateway_foreground` 退出调用户级 `stop_gateway`。
- `updater.py` `restart_manager/daemon/gateway`：无托管时 `stop_*`→`start_*`；manager 版本不一致二次 `stop_manager`。
- `autostart.py` `_prepare_daemon_for_supervision`：安装期写 `enabled=True`（合法，属意图断言）。
- `cli/manager.py`·daemon/gateway：`on` → `start_*`，`off` → `coordinated_autostart_disable` + `stop_*`。
- `service_intent.py`、`doctor.py` `check_service_runtime_state`：消费方。
- 测试：`tests/test_service_intent.py`、`tests/test_doctor_service_checks.py`、`tests/test_updater*.py`、`tests/test_manager_service.py`、`tests/test_daemon.py`、`tests/test_gateway_service.py`。

### 16.5 WBS（可执行）

| ID | 工作包 | 规模 | 触点 | 交付物 | 依赖 | 完成标准 |
| --- | --- | --- | --- | --- | --- | --- |
| **064.01** | 状态模型扩展 | S | 三服务 state dataclass + 读写 | `lastStartError` / `consecutiveStartFailures`；旧文件容错读 | — | 缺字段读为默认值，不迁移 |
| **064.02** | start 失败去污染 + on 先断言（×3） | M | `start_manager` / `start_daemon` / `start_gateway` | 不写 `enabled=False`；清 pid；写失败记录；**三服务 on/`start_*` 入口先断言 True**；BUG-073 仅 `state is None` 可补写 True | 064.01 | `caddy_start` 失败后 `enabled` 为 true（与 manager/daemon 对齐）；`enabled=False` 且 Caddy 在线时调用 `start_gateway` 不把意图翻 True |
| **064.03b** | 子进程 / 前台退出去污染 | M | `run_service_main` `finally`；`run_gateway_foreground` 退出；核验 `daemon._main` | manager `finally` 只清观测；gateway 退出改内部停止（可先内联 caddy_stop+清 pid）；daemon 加回归禁止写 False | 064.01 | 对运行中 manager **或** gateway 前台发 SIGTERM 后 `enabled` 仍为 true、pid 已清 |
| **064.03** | 内部停止原语（×3） | M | 新增 `_stop_process`（或等价）；`updater.restart_*` 主序列 **与** 版本不一致二次 stop 全部换用 | 写盘只清 pid、不改 `enabled`；用户级 `off` 仍走 `stop_*`；抽合 064.03b 的 gateway 内联停止 | 064.01、064.03b | 内部 stop+start 失败后 `enabled` 未变；二次 stop 路径同样不写 False |
| **064.04** | 熔断器 | M | 纯函数 + **仅** `updater` reconcile 拉起前 | `>=3 次且 24h 内` 跳过自动拉起并明示；冷却过期放行一次；`on` 成功清零。**不**接入 `start_*`，**不**改 KeepAlive。手动 on 失败计入计数但不挡本次 on | 064.02 | 熔断/冷却/恢复 +「手动 on 不被熔断挡住」+「手动 on 失败会 +1」有单测 |
| **064.05** | doctor / status 消费 | S | `check_service_runtime_state`、status/--json | FAIL 含失败原因与熔断；JSON 透出新字段 | 064.01 | doctor 输出含 `lastStartError.message` |
| **064.06** | on/off / 联动语义收口 | S | 三服务 CLI、`stop_*`、`maybe_start_gateway`、`start_*` 已在运行早退 | `off` 重置失败记录；`on` 成功与**已在运行早退**均清零；联动前查 `service_intent`，disabled/n.a. 跳过 | 064.02 | `gateway off` 后 `lwa manager on` 不把 gateway 翻回 True；早退后 `consecutiveStartFailures == 0` |
| **064.07** | 文档同步 | S | FAQ / known-limitations / runtime-workspace / operations-playbook | 写入契约、熔断范围、**存量污染需手动 on**、失败自愈路径 | 064.01–06、064.03b | release-checklist 检查项更新 |
| **064.08** | 回归与收口 | M | tests/ | 失败→自愈 / manager `finally` 与 gateway 前台退出不翻意图 / 联动不翻 gateway off / gateway on 失败仍 enabled=true / 熔断 / 冷却 / 手动恢复 / 版本不一致二次 stop / 已在运行早退清零 | 064.01–07、064.03b | 定向 + 全量 pytest 全绿；本节改「已落地」 |

### 16.6 验收标准

- 模拟内部停止成功 + `start_*` 健康检查失败 → `enabled` 保持 `true`，带 `lastStartError`，pid 已清；下一次 `lwa update` reconcile（未熔断时）自动拉起并标注「意外未运行，已恢复」。
- 对 manager：给运行中的管理页发 SIGTERM，在新进程未能起来前读状态文件 → `enabled` 仍为 true（覆盖 `finally` 旧行为）。
- 对 gateway：给运行中的前台监管发 SIGTERM → Caddy 已停、pid 已清、`enabled` 仍为 true（覆盖 `run_gateway_foreground` 调用户级 `stop_gateway` 的旧行为）。
- `lwa gateway on` 在 `caddy_start` 失败后 → `enabled` 为 true 且带 `lastStartError`（与 manager/daemon 对齐）；doctor 报 FAIL 而非「已按意图停用」。
- `lwa gateway off` 后执行 `lwa manager on` → gateway `enabled` 仍为 false，Caddy 不被联动拉起。
- `updater` 版本不一致触发的二次 stop 之后 start 再失败 → `enabled` 仍为 true。
- 连续 3 次 **reconcile 拉起**失败（24h 内）→ 第 4 次 update **跳过自动拉起**并明示熔断与 `lwa <svc> on`；同窗口内 `lwa <svc> on` 仍可调用 `start_*` 且不被熔断拦截（失败会计入计数）；`on` 成功或已在运行早退后计数清零。
- 监督器 KeepAlive 在熔断状态下仍可按既有语义拉起（本 IMP 不改单元文件 / 不插入熔断）。
- `lwa doctor`：`enabled=true` 未运行 → FAIL 且文案含上次失败原因；用户主动 `off` → 「已按意图停用」。
- 旧状态文件（无新字段）读写正常；**已是 `enabled=false` 且无失败记录的存量不自动翻回**；全量 pytest 通过。

### 16.7 明确不做（本 IMP）

- 不做定时自动重试调度（拉起时机仍限 update / doctor 等既有触发点，不新增后台重试循环）。
- 不做指数退避（固定阈值 3 次 + 24h 冷却窗口，参数集中常量、实机可调）。
- 不改 autostart 监督器（KeepAlive / JobRestart）自身的重启语义；熔断不约束监督器。
- 不把熔断判断放进 `start_*`（避免挡住手动 `on`）。
- 不把 BUG-073「Caddy 在线即补写 `enabled=True`」保留为普遍规则（仅状态文件缺失可恢复）。
- `maybe_start_gateway` 不在「用户未表达 gateway on」时写意图或拉起。
- 不引入状态机框架；不做状态文件版本迁移；**不自动翻回存量 `enabled=false`**。
- 不用「失败路径再写 `enabled=True`」替代本 IMP（会把真 off 与失败搅在一起）。

| ID | 关系 |
| --- | --- |
| `IMP-064` / `PLN-042` | 本功能点 / 规划入账 |
| `DEV-*` | 实施时按 064.01–08 + **064.03b** 开发项 |
| `IMP-059`（§14.3）/ `IMP-060`（§14.4） | 失明问题的受害方；本 IMP 落地后其安全网在失败场景生效 |
| CHK-225 设计裁决项（2026-08-18） | 立项来源 |
| CHK-230（2026-08-18） | 设计复核：补 `finally`、收紧写入契约、修正触点、明示存量不翻回 |
| CHK-232（2026-08-18） | 再复核：gateway 前台退出、`maybe_start_gateway` 联动、gateway on 失败意图落点；次要（早退清零、手动 on 计入熔断） |

---

## 变更日志

| 日期 | 变更 |
| --- | --- |
| 2026-08-18 | **§16 IMP-064 再修订（CHK-232）**：①064.03b 覆盖 `run_gateway_foreground` 退出（不得调用户级 `stop_gateway`）；②`maybe_start_gateway` / `start_manager` 联动前查 `service_intent`，BUG-073 仅 `state is None` 可补写 True；③三服务 `on` 入口统一先断言 `enabled=True`（gateway 失败不再落成「已按意图停用」）。次要：已在运行早退清零计数；手动 on 失败计入熔断但不挡本次 on。规则扩为 10 条；064.02/03b/04/06/08 与验收同步。 |
| 2026-08-18 | **§16 IMP-064 契约修订（CHK-230）**：补 064.03b（`run_service_main` `finally` 不得写 `enabled=False`，且须先于 064.03 完成）；`enabled` 写入契约改为 False 仅用户级 off、True 允许 on/start 成功断言/监督器入口/autostart 安装期；内部停止改为「写盘只清 pid」而非「完全不写状态」；触点更正 `daemon.py`（删除不存在的 `daemon_service.py`）、标明 gateway `start_*` 失败与 manager `finally` 不同构；熔断仅挡 reconcile、不进 `start_*`、不约束 KeepAlive；明示存量 `enabled=false` 不自动翻回。WBS 现为 064.01–08 + 064.03b。 |
| 2026-08-18 | **§16 立项 IMP-064 服务意图字段去污染（P1，PLN-042）**：承接 CHK-225 设计裁决项（update 重启失败把 enabled=False 留盘、IMP-059/060 失明）。方案：`enabled` 回归纯用户意图，`lastStartError`/`consecutiveStartFailures` 观测字段承载失败事实；重启路径改内部停止原语不动意图；3 次/24h 熔断防 boot loop；doctor FAIL 文案带失败原因。WBS 064.01-08，待实施。 |
| 2026-08-18 | **CHK-225/226/227 复审收口 + V0.8.1**：CHK-225 审查发现 5 高 5 中若干低，其中 6 项为 CHK-223/224 已修项的旧线索（BUG-533/529+收口/531/538/537），4 项新修--BUG-540（stdlib install=None 被模板兜底回 pip 层、构建必失败）、BUG-541（stdlib 弱信号抢占 static 降级）、BUG-542（家目录挂载 normpath 绕过）、BUG-543（非 git 目录误建 .git/lwa-update.lock）；BUG-544（restart_resilience 注入 runner 签名不匹配致容器策略死代码，移除 runner 注入解耦）。CHK-226/227 核验 BUG-529~539 均已落地。版本提升 **V0.8.1**（OPS-114，10 处修改）；README/FAQ/testing.md 补 stdlib 弱信号与识别优先级口径。 |
| 2026-08-18 | **CHK-223/224 复审收口**：BUG-529～534 由复审会话修复、BUG-535 随 V0.8.0 版本提升（OPS-113）关闭、BUG-536（behindBy 截断）本会话修复；CHK-224 三项（restart_resilience 逐项差集 / init 引导异常兜底 / service_runtime_state 反向不一致）落地为 BUG-537～539 并修复；新增回归 16 例，全量 2159 passed。 |
| 2026-08-17 | **§14/§15 全量落地（DEV-118/119）**：IMP-059 update 三态 reconcile（service_intent.py + coordinated_start + --no-reconcile）；IMP-060 doctor service_runtime_state/restart_resilience 两检查入主报告；IMP-061 autostart 缺省反转（with_caddy/linger 三态）+ init/setup 首次引导 + 运行模式标注（status/autostart status --json）；IMP-063 一键 GitHub 更新通道（update_source/update_flow/update_continuation + --check/--dry-run/--no-pull/--remote/--ref + handoff v1 新解释器接力 + repo/workspace 双锁 + SourceCheckReport v1 + skip-pip 门控 + 恢复链）；README/FAQ/autostart.md/lwa-update-runtime skill 同步；新增测试 87 例（059:18 + 060:11 + 061:17 + 063:41），全量 2143 passed。063.13 实机验收（需代理环境一键升级）待下一发布周期。 |
| 2026-08-17 | **§15 IMP-063 `lwa update` 一键 GitHub 更新通道（P0，准备开工）**：事故实证重开原 IMP-040 后，根据 CHK-220 将方案加固为「upstream/显式 remote+ref 解析 → 单次 fetch 固定 OID → ff-only 快进 → pip → 新解释器 handoff → Runtime 后半段」；拍板 check/dry-run 副作用边界、warning/独立 SourceCheckReport v1/退出码、`--skip-pip` 冲突门禁、repo+workspace 锁 FD 继承、关键失败恢复链；WBS 扩为 063.01-13 并消除依赖环；`PLN-041`。 |
| 2026-08-17 | **§14 Ubuntu 实机 update 事故复盘**：机器重启后 manager/caddy 裸进程丢失约半天不可见，update 因「原本未运行」观察态误判不拉起；根因矩阵五项（服务无监管 / doctor 不查服务运行态与韧性 / update 无期望态 / autostart 缺省不全 / 实例级有期望态而服务级没有的对照）；入账 **IMP-059**（update 服务级期望态 reconcile，P0）、**IMP-060**（doctor 服务运行态+重启韧性检查，P0）、**IMP-061**（自启默认化与首次引导，P1）、**IMP-062**（版本滞后可发现性，P2 占位）；含 WBS 059.01-06 / 060.01-04 / 061.01-04 与明确不做清单；`PLN-040`、`CHK-219`。 |
| 2026-08-11 | **四文档参照体系补强**：§13 同步 IMP-056/057 MVP 与 IMP-058 Gate-C 实施后状态；不复制 task-list 待办，改为指向 Scanner C.R01～C.R07 和预检/Monorepo 可选包 C，供后续 Agent 继续实施。 |
| 2026-08-11 | **§12.8 + §13 评审摘要**：路径别名门禁收紧为入口 HTML 根绝对资源负向守卫，补证据/快照边界；新增 IMP-058 Scanner 五层流水线、组件/替代计划、能力契约、成功谓词、等价 fallback 与副作用回滚摘要；明确 DEV-110 为评审前初版实现记录，修订版 Gate-C 由 DEV-111 重新承接。 |
| 2026-08-10 | **§12 IMP-055 复盘补链**：补充 BUG-467～469、多轮复核、真实 prd-review/home-bookshelf E2E、重复别名前缀与最终 URL/MIME/JSON 404 修复；详细过程迁入独立复盘文档。 |
| 2026-08-09 | **§12 IMP-055**：路径别名兼容性门禁（承接方案 B）；职责边界（应用改 base path / LWA 做门禁）；详细 WBS 055.01～15；撤销 docker-compose 对 BUG-466 豁免、收敛 BUG-465；`PLN-038`。 |
| 2026-08-09 | **§11.5 IMP-053**：已有 Runtime 复用提示（init 软警告 + skills）；DEV-099。 |
| 2026-08-09 | **§11 Agent 部署复盘**：入账 IMP-052 / BUG-455 / BUG-456 / PLN-036；区分缺陷与 Agent 误解；**已落地 DEV-098**（入口推断 + alembic 前置 + manager off 跨工作区黄字提示）。 |
| 2026-08-06 | 建档。核对 2607；补记 IMP-044 / IMP-045；承接 IMP-042.b 与 IMP-029 候选。 |
| 2026-08-06 | **入账本月待办 IMP-046（Token 7×24h 自动轮换）、IMP-047（本机文件夹源导入+更新）；后续待办 IMP-048（zip↔文件夹转换）。** |
| 2026-08-06 | **IMP-047 补强**：关联目录仅作只读复制源；运行必须在 LWA `apps/<id>/` 内，与 zip 同管线；禁止就地运行关联文件夹；更新无变更（内容指纹 / 可选 git diff）提示「无需更新」。 |
| 2026-08-06 | **权威位置改为 `design/plans/`**（用户挪移）；同步修正 task-list / 2607 链接。 |
| 2026-08-06 | 明确 **暂不进入 `achievement/`**：删除 achievement 下 2608 短链；进行中文档仅保留在 plans。 |
| 2026-08-06 | **核对实施计划合集**：主路径均已落地；移植 Task 11 中路径相对化/CLI 解耦 → §8 IMP-049·050；合集文首同步。 |
| 2026-08-06 | **删除 §3 IMP-042.b**：跨盘/跨机暂不开发、不纳入 2608 待办；仅在 §0.2 边界说明。 |
| 2026-08-06 | 确认本月先做 **046/047**；将 **049/050** 标为 **优先级：中 · 不着急**（不抢档）。 |
| 2026-08-06 | **拆解 IMP-046 / IMP-047 可执行 WBS**：§4.4（046.01～12，阶段 A–E）、§5.4（047.01～17，阶段 A–F）；含依赖、交付物、完成标准与落地顺序。 |
| 2026-08-06 | **IMP-047 落地后修订（BUG-443）**：§5.2 增 047.i/j/k；§5.5/§5.7 补自动部署与任意 HTML；合集文末归档方案与 runtime 验收（3-src / 4-output）。 |
| 2026-08-06 | **入账 IMP-051**：管理页「从文件夹导入」源路径右侧「选择文件夹」按钮（macOS 访达 / Ubuntu 文件选择器）；§6 全文；优先于 048/049/050；`PLN-035`。 |
| 2026-08-06 | **IMP-051 落地**：§6 决策/WBS/触点改为已实现；链到 `docs/plans/2026-08-06-imp-051-pick-directory.md`；`DEV-097`。 |
| 2026-08-06 | **V0.7.1 收口记入 §6.8**：BUG-444～448、ADJ-042/043（pending UX、错误前缀、`import_activity` + update 等空闲、中文 ID）；§5.8 交叉引用；§7 小节编号修正；用户文档见 `DOC-117`。 |
| 2026-08-06 | **§6.8 补 BUG-449**：medium/heavy「不自动启动」不得误报「未能识别」；`describeFolderImportOutcome` 仅以 `status===pending` 判未识别。 |
