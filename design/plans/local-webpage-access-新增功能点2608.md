# 新增功能点计划（202608）— 编号续接 IMP-043

> **状态（2026-08-06）**：本文件承接 [`../achievement/local-webpage-access-新增功能点2607.md`](../achievement/local-webpage-access-新增功能点2607.md)。**2607 范围内 IMP-025～028 / IMP-030～043 主路径均已落地**（见下「§0 上月收口」）。**8 月初已落地补记：IMP-044 / IMP-045**。**IMP-046 Token 7×24h 自动轮换已落地**（DEV-095）；**IMP-047 本机文件夹源导入与一键更新已落地**（DEV-096）。**IMP-051 管理页「选择文件夹」已落地**（DEV-097；仅 loopback）。**V0.7.1**：导入 UX 护栏（选根/dist、pending 勿冒充成功、错误码前缀剥离、`lwa update` 等导入空闲）与中文名 ID 回退等已收口。 **后续 / 不着急：IMP-048 zip↔文件夹转换；IMP-049 / IMP-050（优先级：中，不与 046/047 抢档）。** **IMP-042.b 跨盘/跨机不纳入本文件、暂不开发**。候选仍含 IMP-029。
> **范围**：§0 为 2607 与实施计划合集核对；§1～§2 已落地补记（044/045）；**§4～§5 本月优先 046/047（含可执行 WBS）**；**§6 IMP-051 文件夹选择器（已落地）+ V0.7.1 导入护栏收口**；**§7 后续 048**；**§8 合集移植 049/050（优先级中 / 不着急）**；§9 其它候选。无 §3（原 042.b 已删除）。日常跟踪以 `task-list.md` 为准。

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
> **实现计划**：[`docs/plans/2026-08-06-imp-051-pick-directory.md`](../../../docs/plans/2026-08-06-imp-051-pick-directory.md)。

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
| IMP 号 | 全局递增不复用；046/047/**051 已落地**（含 §6.8 V0.7.1 收口）；048=后续；049/050=**优先级中 / 不着急**；042.b 不在本文件 |
| 与 task-list | 规划 `PLN-`；开发 `DEV-`；文档 `DOC-` |
| 与 2607 | 7 月账本在 `design/achievement/`；本月只改本文件 |
| achievement | **暂不入账**：本月进行中的 2608 只放 `design/plans/`；收口归档后再迁入 `achievement/`（若需要） |
| 实施计划合集 | 历史已落地计划归档；未做项以本文件为准 |

---

## 变更日志

| 日期 | 变更 |
| --- | --- |
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
