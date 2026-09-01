# GitHub Issue 代码质量共性分析报告

> 盘点时间：2026-08-18；2026-09-01 复评
> 数据来源：[GitHub wangminle/local-webpage-access issues #1–#5](https://github.com/wangminle/local-webpage-access/issues)（2026-08-17 至 2026-08-18）  
> 关联台账：[[CHK-233]] · 可视化原版见 `canvases/github-issues-quality-patterns.canvas.tsx`

> **当前状态（2026-08-31）**：原分析样本 #1～#5 已全部关闭，BUG-549～552 已随 V0.8.2 收口。本文的质量规律继续有效，但原开放状态和修复顺序只保留为历史现场，不再作为当前待办。

## 摘要

仓库在 48 小时内新增 5 条 GitHub Issue，全部来自实机运维（Ubuntu systemd + macOS launchd），版本窗口 V0.7.11 → V0.8.1。它们不是散点 bug，而是同一层设计裂缝的不同切口：**状态文件被当成运行实况、运维命令不幂等、重启后立刻自检**。

| 指标 | 数值 |
| --- | --- |
| 新增 Issue | 5 |
| 仍开放 | 0（原样本 #1～#5） |
| 已关闭 | 5（#1～#5；#1 于 V0.8.0 修复，#2～#5 于 V0.8.2 收口） |
| 落在自启/更新路径 | 4/5 |

**一句话结论：** 大家真正有意见的，不是「测试不够」或「风格不统一」，而是：进程监管层上线后，多套真相源（json / pidfile / 监督器 / admin 探活）没有统一；运维命令对已达目标态仍会动进程；重启后的瞬态被当成真实失败。

> 作者均为 wangminle（仓库维护者的实机反馈，尚无外部贡献者）。

---

## Issue 清单

| # | 状态 | 标题 | 平台 | 质量焦点 |
| --- | --- | --- | --- | --- |
| [#1](https://github.com/wangminle/local-webpage-access/issues/1) | closed | stdlib 服务：识别无覆盖、挂载被抹、失败后撞锁 | Ubuntu | 封闭世界 + 生成物无出口 + 锁/自愈 |
| [#2](https://github.com/wangminle/local-webpage-access/issues/2) | closed | autostart enable：launchctl error 5，gateway 下线 | macOS | 竞态 + 失败不自愈 |
| [#3](https://github.com/wangminle/local-webpage-access/issues/3) | closed | autostart enable：已监管进程被非必要 stop | WSL2 systemd | 不幂等 + 能力未复用 |
| [#4](https://github.com/wangminle/local-webpage-access/issues/4) | closed | update 虚报 Gateway 中断 7.6 天；check 误杀自家 caddy | Ubuntu systemd | 陈旧 json 当观测 |
| [#5](https://github.com/wangminle/local-webpage-access/issues/5) | closed | update 内置 doctor/accessReview 落在重启窗口，瞬时 FAIL | Ubuntu systemd | 无就绪等待 + 误报 |

---

## 2026-09-01 风险增量与当前状态

以下三项不是重新打开 #1～#5，而是说明本文质量规律仍会在新路径复现；#18/#21/#26 均已完成并保留为回归约束：

| Issue | 当前状态与分级 | 对本文规律的增量 | 落地要求 |
| --- | --- | --- | --- |
| [#21](https://github.com/wangminle/local-webpage-access/issues/21) | **已完成（V0.8.9）** | liveness 失败回滚曾丢失 `routeMode=name`、路径别名及网关片段，命中 L3「失败 fail-safe」和 L5「意图/观测/持久化分离」 | 已补 manifest 别名元数据、网关片段快照与回滚回归；继续作为回归约束 |
| [#18](https://github.com/wangminle/local-webpage-access/issues/18) | **已完成（V0.8.9）** | 生成 Dockerfile 曾把单一镜像源硬编码为唯一通路，命中 L6「生成物要有用户出口」 | 已补来源链、超时、有限重试、降级与诊断；继续作为生成物可靠性回归约束 |
| [#26](https://github.com/wangminle/local-webpage-access/issues/26) | **已完成（V0.8.10）** | 同进程多线程启动竞争时，进程互斥锁与 `flock` 生命周期错位导致持有线程无法释放，继续命中锁与自愈边界 | 进程内可重入锁覆盖完整临界区，嵌套同线程只由最外层持有 `flock`，不同线程共享同一超时预算排队；update 收尾额外检查单 master |

当前优先级：保持 #18/#21/#26 的回归门禁；IMP-064 已作为 L5 的结构性修复落地，不再用更多状态字段补丁替代意图/观测分离。

---

## 主题重叠：5 条 Issue 打中 4 条规律

“陈旧状态当观测”被 2/5 个 Issue 直接触及，其余三条主要规律各被 3/5 个 Issue 触及，说明不是孤立缺陷，而是同一设计习惯在不同命令上的重复。

| 规律 | 触及 Issue 数 | 计入 Issue |
| --- | ---: | --- |
| 陈旧状态当观测 | 2 | #3、#4 |
| 命令不幂等 / 失败留残局 | 3 | #1（挂载抹除）、#2、#3 |
| 重启竞态无就绪等待 | 3 | #2、#3、#5 |
| 已有探活能力未接线 | 3 | #3、#4、#5 |
| 扫描器/模板封闭世界 | 1 | #1 |

---

## 共性一：状态文件被当成运行实况

这是最强信号。代码里已经有「不要信 pid 文件」的注释（`is_gateway_running` 以 admin 为准，BUG-070），但新加的中断时长、端口占用、是否需要迁移，又回去读 `gateway.json` / `state.enabled`。

### issue #4：gateway.json 永久陈旧

- 监督器 8/17 接管后从不回写 json。文件仍停在 8/10 裸进程：pid=3836952，started_at=2026-08-10T22:32。
- `estimate_down_since` 把这份 started_at 当「中断上界」，算出 7.6 天。systemd ActiveEnterTimestamp 证明监督器未中断。
- **同根因**让 autostart check 报 `caddy_conflict_2019`：json pid 已死，真实 caddy 在 `run/caddy.pid`，检查只信 json，提示用户去杀自家网关。

### issue #3：is_running 不看监管模式

- `_migrate_detached_for_supervision` 应用 `daemon.is_running()`：enabled ∧ pid 存活 ∧ 锁 ∧ 心跳。systemd 监管进程全部满足，于是被 stop。
- `service_supervision_mode()` 已能区分 systemd / launchd / 裸进程，目前只给 status 展示用，迁移路径没复用。
- 设计注释写「已有 detached 才停」；实现停的是「任何在跑的」。

---

## 共性二：运维命令对已正确状态仍会动刀

enable / start / update 被当成「再做一遍流程」，而不是「收敛到目标态」。目标已达成时仍 stop、仍重生成、失败时还不把服务拉回来。

| 命令 | 用户预期 | 实际 | Issue |
| --- | --- | --- | --- |
| autostart enable（已 enable） | 幂等，零副作用 | stop daemon/manager，2–3s 中断 + 能力探针 25–30s 假红 | #3 |
| autostart enable（macOS 首次） | 失败则保持原服务 | bootstrap error 5 后 gateway 单元未加载且进程已停 | #2 |
| lwa start（compose 手改过） | 保留 extra bind mount | 模板重生成，静默抹掉 /workspace 挂载；HTTP 探针仍绿 | #1 |
| lwa update 收尾自检 | 反映真实健康 | restart 后立刻 doctor，瞬时 FAIL；手动再跑即 OK | #5 |

---

## 共性三：探活能力已经写过，新路径没有接到

不是「不会做观测」，是「新功能另起一套文件口径」。同一仓库里，实例探针有 30 次重试，gateway 有 admin 探活和 `inspect_caddy_owner`，自启有监管模式标注——更新和 enable 都没用上。

| 已有能力 | 本该用在 | 实际用了什么 | 后果 |
| --- | --- | --- | --- |
| `service_supervision_mode()` | enable 是否需要迁移裸进程 | `is_running()`（enabled+pid+锁） | #3 误停监管进程 |
| `inspect_caddy_owner` / `run/caddy.pid` / admin :2019 | 中断时长、2019 是否外部占用 | `gateway.json` 的 pid 与 started_at | #4 虚报 7.6 天 + check 假红 |
| 实例 Gate-C 30 次重试 | update 重启后的 doctor/accessReview | stop→start 后立即探测一次 | #5 更新报告 FAIL，手动即 OK |

---

## 共性四：失败路径不 fail-safe，成功路径又太早报完成

### 失败留下线窗口

- **#2：** bootout 与 bootstrap 之间的 launchctl error 5 是瞬态（原样重试即过），但 enable 收尾已经把 gateway 进程停了，没有兜底 `gateway on`。用户看到「已启用（失败）」，别名入口 :8080 实际下线。
- **#1：** 探针失败后锁仍被 daemon 自愈占用约 30s，用户 `restart` 得到「正在被其他操作占用」，像死锁。根因是 desired 仍 running，下一 tick 全量重建。

### 成功报得太早

- **#5：** update 逐步打勾 restart* 后立刻 accessReview/doctor。服务还在绑定 admin 端口、加载 Caddyfile。对比同一产品里部署探针会重试 30 次。
- **#3 叠加 BUG-412：** enable 误重启后 capability probe 要 25–30s 才 ready，期间 access review / doctor 再红一轮。

---

## #1 的另一面：识别层仍是封闭世界

与后四条不同，#1 打在导入/模板层，但质量意见同类：系统只认自己生成的形状。

| 维度 | 说明 |
| --- | --- |
| 无覆盖入口 | PYTHON_WEB 白名单 miss → pending；`--path-alias` 又拒绝 pending。用户只能伪造 tornado 依赖绕过。 |
| 生成物即唯一真相 | compose 注释「请勿手改」，`.env.local` 又鼓励业务定制。最常见的挂载没有 extraVolumes 出口，`lwa start` 静默覆盖。 |
| 启发式警告有效（正向） | CHK-P04「绝对 fetch('/api/...') 在路径别名下会 404」被实机验证为对。该类警告应保留并加码，不要在误报压力下砍掉。 |

> #1 已在 V0.8.0（4b61c9b）修复：stdlib AST 识别、extraVolumes、PORT 注入、锁消息与 reconcile 退避。详见 issue 回复与 [[BUG-528]]。

---

## 可写入后续约定的规律

按「以后写自启 / 更新 / doctor / 状态估算时先对照」来排，而不是再开一轮风格审查。

| # | 规律 | 落地检查 | 针对 Issue |
| --- | --- | --- | --- |
| L1 | 观测优先于状态文件 | 「是否在跑 / 中断多久 / 谁占用端口」必须以 live probe（admin、pidfile、supervision mode、proc）为准；json 只缓存，且与 live 不一致时丢弃估算 | #3、#4 |
| L2 | 运维命令必须幂等 | enable/install/update 对已达目标态零副作用；先问 supervision_mode，再决定是否 stop | #3 |
| L3 | 失败 fail-safe，成功等就绪 | bootstrap/start 失败要兜底拉回或明确恢复命令；restart 与 doctor 之间必须 wait-ready（复用实例探针重试），禁止单次探测定终身 | #2、#5 |
| L4 | 新检查先复用已有探活 | 加 doctor/check/estimate 前先搜 `inspect_*` / `is_*_running` / `service_supervision_mode`；禁止再造一套文件口径 | #3、#4、#5 |
| L5 | 意图、观测、持久化三分离 | enabled ≠ running ≠ json.pid ≠ 监督器 ActiveEnterTimestamp。混用处就是下一条虚报 | #4 及 IMP-064 |
| L6 | 生成物要有用户出口 | 模板重生成必须合并 extraVolumes 等定制，或检测手改后 warning+备份；探针绿不能掩盖应用内挂载丢失 | #1 |
| L7 | 区分瞬态 FAIL 与持续 FAIL | 文案和退出码分层：刚重启的失败标「可能未就绪，请稍后复检」，不要让脚本按 FAIL 回滚代码 | #5、#4 |
| L8 | 失败后的锁与自愈要退避 | 刚失败实例不要立刻全量重建；锁超时必须带持有者 PID 与心跳，避免用户当死锁 | #1 |

---

## 不该从这批 Issue 得出的结论

### 样本边界

- 5 条全部是维护者自己的实机报告，不是外部用户投票。结论对「自启监管 + 一键 update」这条新主路径有效，不宜外推到前端 UI 或扫描器全貌。
- #1 已在 V0.8.0 关闭；#2–#5 集中爆发在 V0.8.1 启用监督器之后的同一天下午，属于同一发布波次的回归簇。

### 质量画像

- ruff/mypy/pytest 全绿挡不住这批问题：它们是集成语义（监管 vs 裸进程、文件 vs 探活、重启 vs 就绪），单测 mock 文件状态会把缺陷测成绿。
- 后续优先补的是：真实监督器路径的幂等测试、陈旧 json 与活 pid 不一致的 doctor 用例、update 后 wait-ready 再自检。而不是再加静态检查规则。

---

## 历史修复顺序（已完成）

> 2026-08-18 补记：四项已全部修复并经红绿验证收口于 **V0.8.2**（BUG-549~552 / CHK-234/235），详见 2608 计划 changelog。

1. **先修 #4** — 陈旧 json 同时毒化 update 文案和 autostart check，持续假红且可能误杀网关。
2. **再修 #3** — enable 幂等，顺带减少 #5 的重启窗口。
3. **#2** — 加 error 5 短重试 + 失败兜底拉起（台账 [[BUG-549]]）。
4. **#5** — 在 restart 与 doctor 之间接入就绪等待。

四条已修完，这批原始「监管层观测」裂缝已收口；后续同类问题按“2026-08-31 当前风险增量”继续追踪。

---

## 关联

| 类型 | 条目 |
| --- | --- |
| 台账 | [[CHK-233]]、[[BUG-528]]（#1）、[[BUG-549]]（#2） |
| 设计 | IMP-064（服务意图字段去污染）、[[CHK-230]]、[[CHK-232]] |
| 可视化 | `canvases/github-issues-quality-patterns.canvas.tsx` |
