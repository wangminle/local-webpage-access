# local-webpage-access 实施计划合集（历史归档）

> 本文件是原 `design/plans/` 下 7 篇独立设计/实施计划的合并归档（2026-07-27～2026-08-03），现位于 `design/achievement/`。
> **核对（2026-08-06）**：**七份计划的主交付均已落地**（见下「完成度速查」）。Task 11 中**路径相对化 / CLI 解耦**已移植到 [`../plans/local-webpage-access-新增功能点2608.md`](../plans/local-webpage-access-新增功能点2608.md)（IMP-049 / IMP-050）。**跨盘 IMP-042.b 暂不开发、不迁入 2608 待办**（仍保留本节历史口径与预检 blocking 行为）。
> **补记（2026-08-06 晚）**：文件夹导入「识别成功却卡待识别 / 任意 HTML 不识别 / 管理页不自动部署」三道坎已按 BUG-443 收口（见文末新增块）；活文档方案以 2608 **IMP-047** 落地后修订为准。
> 日常运维与用户说明见 `docs/`；进行中的任务见根目录 `task-list.md` 与 2608。

### 完成度速查（2026-08-06）

| 计划块 | 结论 | 证据 / 残留 |
| --- | --- | --- |
| 2026-07-27 Bug 全量收口（BUG-293～320） | **已落地** | task-list 28 条均为已修复 |
| 2026-07-29 工作区迁移（IMP-042） | **主路径已落地** | DEV-089；同卷 `lwa workspace relocate` |
| └ Task 11（P2）路径相对化 / CLI 与工作区解耦 | **未实现 → 已迁 2608** | IMP-049 / IMP-050 |
| └ Task 11（P2）跨盘 042.b | **暂不开发**（不迁 2608 待办） | 历史口径仍见本节 |
| 2026-07-30 管理页表布局 | **已落地** | BUG-413/414；`table-layout` 非 fixed；名称两行截断 |
| 2026-08-03 七项待办（BUG-369/370/371/420/421/422 + DEV-094） | **已落地** | CHK-139；跟进 BUG-423～430 等亦已修 |
| 2026-08-06 文件夹导入三道坎（BUG-443） | **已落地** | 任意 `.html` 识别；识别成功→`stopped`；`import-from-dir` 自动 start；runtime 验收 3-src/4-output |

## 目录

- [2026-07-27 · 待修复 Bug 全量收口设计](#2026-07-27-待修复-bug-全量收口设计)
- [2026-07-27 · 待修复 Bug 全量收口实施计划](#2026-07-27-待修复-bug-全量收口实施计划)
- [2026-07-29 · 工作区迁移（workspace relocate）实施计划](#2026-07-29-工作区迁移workspace-relocate实施计划)
- [2026-07-30 · 管理页实例表布局修复设计](#2026-07-30-管理页实例表布局修复设计)
- [2026-07-30 · 管理页实例表布局修复实施计划](#2026-07-30-管理页实例表布局修复实施计划)
- [2026-08-03 · Task List 七项待办收口设计](#2026-08-03-task-list-七项待办收口设计)
- [2026-08-03 · Task List 七项待办收口实施计划](#2026-08-03-task-list-七项待办收口实施计划)
- [2026-08-06 · 文件夹导入识别/部署死胡同收口（BUG-443）](#2026-08-06-文件夹导入识别部署死胡同收口bug-443)

---

## 2026-07-27 · 待修复 Bug 全量收口设计

<a id="2026-07-27-待修复-bug-全量收口设计"></a>

> 原文件：`2026-07-27-pending-bug-remediation-design.md`

### 范围

处理 `task-list.md` 中全部 28 条待修复代码 Bug：BUG-293～BUG-320。每条外部审查结论先用当前代码和失败测试确认；无法复现或结论不成立时记录证据并关闭，不做臆测式修改。

### 方案

采用依赖优先的五批修复顺序：

1. 共享状态与并发基础：BUG-293、294、307、308、309。
2. 安全边界与输入校验：BUG-295、304、306、310、314、315、316、317。
3. daemon 可靠性：BUG-296、297、298、299、311、312、313。
4. 生命周期与网关：BUG-300、301、302、305、318、319、320。
5. 浏览量时区：BUG-303。

构建队列先建立 token/CAS 与可取消等待语义，其他跨进程状态修复在其上实现。安全批次统一采用服务端强制校验，不依赖前端约束。daemon 批次把退出、重试、退避和信号处理作为同一运行循环的可靠性边界。生命周期批次优先保全用户数据和可恢复性。浏览量统一把所有输入规范化为 UTC 后再分桶、持久化和比较。

### 测试与完成标准

每条 Bug 均执行：

1. 写最小回归测试。
2. 运行测试并确认因目标缺陷失败。
3. 实施最小修复。
4. 运行定向测试确认通过。
5. 每批运行相关模块回归。

全部完成后运行全量 `pytest`、Ruff、Mypy、`git diff --check` 和任务清单 CLI 校验。只有具备新鲜验证证据的条目才更新为“已修复”；真实 Docker 测试若因环境开关跳过，必须在备注中明确。

### 工作区约束

当前工作区包含大量既有未提交成果，且后续修复必须建立在这些成果之上。因此不创建脱离当前状态的新 worktree，不覆盖无关差异，不提交、不部署。

---

## 2026-07-27 · 待修复 Bug 全量收口实施计划

<a id="2026-07-27-待修复-bug-全量收口实施计划"></a>

> 原文件：`2026-07-27-pending-bug-remediation.md`

> **For Codex:** REQUIRED SUB-SKILL: Use executing-plans to implement this plan task-by-task.

**Goal:** 核实并收口 `task-list.md` 中 BUG-293～BUG-320 全部 28 条待修复代码 Bug。

**Architecture:** 按共享状态、安全边界、daemon、生命周期、时区五批推进。每条先以失败测试确认，再做最小实现；共享原语先于调用方，避免重复返工。

**Tech Stack:** Python 3.11+、SQLite、FastAPI/Typer、pytest、Ruff、Mypy。

---

#### Task 1: 构建队列取消、CAS 与死进程回收（BUG-293、307、308）

**Files:**
- Modify: `src/local_webpage_access/build_queue.py`
- Test: `tests/test_build_queue.py`

**Steps:**
1. 为排队取消立即中断等待、旧 token 禁止覆盖新代际、终态不被竞态改写、死 owner 的孤儿 worker 被安全终止分别写失败测试。
2. 运行新增用例，确认各自因现实现缺陷失败。
3. 为 `acquire` 增加带 token 的取消探测；所有任务更新增加 token/CAS 条件；回收前按 pid/pgid/identity 安全终止 worker。
4. 运行新增用例与 `tests/test_build_queue.py`。

#### Task 2: SQLite 迁移与 manager 单实例锁（BUG-294、309）

**Files:**
- Modify: `src/local_webpage_access/registry/connection.py`
- Modify: `src/local_webpage_access/manager_service.py`
- Test: `tests/test_registry.py`
- Test: `tests/test_manager_service.py`

**Steps:**
1. 写双连接并发迁移和“锁持有者存活超过 60 秒仍不可抢锁”失败测试。
2. 在迁移写事务内重新读取版本并串行升级；manager 锁只按 pid/身份存活判断，不按 mtime 偷锁。
3. 运行两模块定向测试。

#### Task 3: API/路径/仓库/CLI 安全校验（BUG-295、304、306、310、314～317）

**Files:**
- Modify: `src/local_webpage_access/manager_api.py`
- Modify: `src/local_webpage_access/security.py`
- Modify: `src/local_webpage_access/updater.py`
- Modify: `src/local_webpage_access/version_info.py`
- Modify: `src/local_webpage_access/cli/daemon.py`
- Modify: `src/local_webpage_access/cli/__init__.py`
- Modify: `src/local_webpage_access/cli/manager.py`
- Modify: `src/local_webpage_access/registry/connection.py`
- Test: corresponding `tests/test_*.py`

**Steps:**
1. 分别写跨站回环破坏性请求、相对 mount 穿越、伪项目根、缺失 confirmId、非法 poll/staticGateway/port、Registry 原生异常的失败测试。
2. 服务端对浏览器非 GET 强制同源 Fetch Metadata/Origin 约束并保留 token 客户端通路；mount 先规范化再判敏感目录；源码根校验 `project.name`；purge/force 校验 confirmId；Typer 参数使用范围/枚举；Registry 异常包装为领域错误。
3. 运行安全、API、CLI、registry、updater/version 定向回归。

#### Task 4: daemon 退出、重试、启动、信号、稳定窗口、退避与导入原子性（BUG-296～299、311～313）

**Files:**
- Modify: `src/local_webpage_access/daemon.py`
- Modify: `src/local_webpage_access/logging.py`
- Modify: `src/local_webpage_access/importer.py`
- Test: `tests/test_daemon.py`
- Test: `tests/test_logs.py`
- Test: `tests/test_importer.py`

**Steps:**
1. 为每项运行循环缺陷写失败测试，确认错误退出码、无限重试、超时误杀、SIGTERM 清理、慢拷贝误判、无退避、error 模式竞态。
2. 区分锁占用与运行期 OSError；失败指纹隔离并限制重试；启用滚动日志；抢锁前移或延长有条件等待；信号设置 stop_event 并等待当前导入收尾；稳定判定至少跨两个轮询；reconcile 指数退避；把 error 模式冲突判断并入原子 claim。
3. 运行 daemon/log/importer 定向回归。

#### Task 5: 生命周期与网关恢复（BUG-300～302、305、318～320）

**Files:**
- Modify: `src/local_webpage_access/hosting.py`
- Modify: `src/local_webpage_access/access.py`
- Modify: `src/local_webpage_access/gateway_service.py`
- Modify: `src/local_webpage_access/host_bootstrap.py`
- Modify: `src/local_webpage_access/docker_runtime.py`
- Modify: `src/local_webpage_access/lifecycle.py`
- Test: corresponding `tests/test_*.py`

**Steps:**
1. 写重建失败后可 start、停止实例不参与在线审查、外来 Caddy 不冒充本工作区、失败 setup 不持久化 full、停止容器可救援、manifest 缺失仍清理、observe_status 串行化的失败测试。
2. 失败重建清除失效 containerId；审查按 desired_state 过滤；Caddy 健康检查绑定工作区所有权；仅 ready 时持久化 full；容器查询支持 `--all`；remove 从 registry 降级清理；observe_status 使用实例锁并做受控字段更新。
3. 运行 hosting/access/gateway/bootstrap/docker/lifecycle 定向回归。

#### Task 6: 浏览量 UTC 规范化（BUG-303）

**Files:**
- Modify: `src/local_webpage_access/pageviews.py`
- Test: `tests/test_pageviews.py`

**Steps:**
1. 写带时区偏移和本地 naive 时间的分桶、last_seen 排序失败测试。
2. 所有解析结果在入库前转换为 UTC ISO8601，按 UTC 日期分桶并用规范化值比较。
3. 运行 pageviews 定向回归。

#### Task 7: 全量验证与任务清单同步

**Files:**
- Modify: `task-list.md`

**Steps:**
1. 运行全部相关模块测试和完整 `python3 -m pytest -q`。
2. 运行 `python3 -m ruff check src tests`、`python3 -m mypy src/local_webpage_access`、`git diff --check`。
3. 逐条依据测试证据更新 BUG-293～320 状态、完成时间和备注。
4. 用 task-list CLI 重算摘要并执行 `check`。

---

## 2026-07-29 · 工作区迁移（workspace relocate）实施计划

<a id="2026-07-29-工作区迁移workspace-relocate实施计划"></a>

> 原文件：`2026-07-29-workspace-relocate.md`

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 落地 **LWA 工作区迁移** 一等能力：底层迁移事务 + 唯一执行入口 `lwa workspace relocate` + 交互 Skill `lwa-relocate-workspace`。文件夹改名只是同盘原子 rename 的最简场景。

**Architecture:**
- **事务内核**（`workspace_migrate.py` 或 `workspace_relocate.py`）：确定性状态机、迁移锁、可恢复 journal、备份、回滚钩子；只编排现有 lifecycle / hosting / autostart / static_gateway / registry / pageviews，不内嵌 `sed`/`mv` 脚本语义给 Agent。
- **CLI**：唯一执行入口；支持 `--dry-run` / `--json` / `--resume` / `--verify` / `--rollback`。
- **Skill**：读 dry-run JSON、解释风险、调用 CLI、处理特殊环境（editable / WSL / 跨盘拒绝）、出迁移报告；**禁止**直接 `sed`、`mv` 或删除状态文件。

**Tech Stack:** Python 3.13、Typer、SQLite、DockerRuntime、AutostartBackend、pytest。

**产品编号：** IMP-042（见 `design/plan/local-webpage-access-新增功能点2607.md` §24）。
**跟踪：** DEV-089；人工过渡契约 DOC-081（`docs/workspace-rename.md`）。
**前置已落地：** BUG-382 / BUG-383 / BUG-384。

---

### 0. 范围边界（已拍板）

| 场景 | 首版（IMP-042 v1） | 后续 |
| --- | --- | --- |
| **同一卷（same filesystem）原子改名** — **macOS / Linux 裸机 / WSL 内 Linux 盘** | ✅ **唯一支持的自动路径**（`os.rename`；自启走 launchd 或 systemd user） | — |
| 跨磁盘 / 跨卷复制（不同 mount、需 copy+verify+swap） | ❌ 预检 **blocking** 并指向手册 | IMP-042.b |
| 跨机器迁移（镜像、LAN、token、用户） | ❌ 不在范围 | 另案 |
| WSL `/mnt/<drive>` → `~/…`（通常跨设备） | ❌ 自动拒绝（同跨盘）；若碰巧同设备则按同卷 rename | 随 042.b |
| Windows 原生进程 | ❌ | — |

**理由：** 「同卷 vs 跨卷 vs 跨机」的备份/停机/回滚模型差一个数量级，与 OS 品牌无关。macOS 与 Linux 在同卷 rename + 各自自启后端（launchd / systemd）上均可走同一状态机。

---

### 1. CLI 表面（唯一执行入口）

```bash
# 预演（推荐先跑）
lwa workspace relocate /abs/new/path --dry-run
lwa workspace relocate /abs/new/path --dry-run --json

# 执行（默认 OLD=当前工作区根；非 TTY 需 --yes）
lwa workspace relocate /abs/new/path
lwa workspace relocate /abs/new/path --yes

# 失败后续跑 / 仅验收 / 回滚
lwa workspace relocate --resume
lwa workspace relocate --verify
lwa workspace relocate --rollback
```

| 开关 | 语义 |
| --- | --- |
| `--dry-run` | 跑 preflight + 计划动作，**零副作用**（可写只读报告到 stdout） |
| `--json` | 机器可读：blocking/warnings/planned_phases/snapshot 摘要 |
| `--yes` | 跳过确认 |
| `--from OLD` | 显式旧根（默认当前 workspace） |
| `--snapshot-out PATH` | 把快照额外写到工作区外（防单点） |
| `--resume` | 读 `run/workspace-migrate-journal.json` 从失败 phase 继续 |
| `--verify` | 不搬迁，只跑验收不变量 |
| `--rollback` | 在 journal 允许时回滚到备份/旧路径（v1：同盘且备份完整时） |

---

### 2. 状态机

```text
preflight
→ backup
→ quiesce
→ move
→ rebind
→ regenerate
→ restore
→ verify
→ complete
```

| Phase | 职责 | journal 关键字段 |
| --- | --- | --- |
| **preflight** | 目标不存在、同设备、非 `/mnt` 写禁、路径 holders、editable 告警、锁可获取 | `old`, `new`, `device_ids` |
| **backup** | 备份 registry、pageviews、`local-web.yml`、各 manifest、自启 unit 副本；记录 desiredState / autostart enabled | `backup_dir`, `snapshot` |
| **quiesce** | 暂停 daemon/manager/gateway；stop 业务实例；容器 `compose down`；持迁移锁 | `stopped_ids`, `downed_ids` |
| **move** | 同文件系统原子改名 OLD→NEW | `moved_at` |
| **rebind** | 结构化改写 manifest + registry（及 builds.log_path）；清陈旧 containerId 以便 start 走 up -d | `rewritten` |
| **regenerate** | 重生主 Caddyfile（`_sync_main_config`）；sites/aliases 绝对路径已在 rebind 边界安全改写；`autostart repair`（仅迁前已装；preserve gateway）；只清 capability 缓存 | `units`, `caddy` |
| **restore** | enable 自启（若有）；未装自启则按快照拉回 detached daemon/manager；只 start 快照中 running 意图的实例 | `restored_ids` |
| **verify** | autostart check、pageviews 不变量、扫描 manifest + 主 Caddyfile + sites/aliases 无 OLD 残留 | `verify_report` |
| **complete** | 释锁；保留 journal 供审计 | `completed_at` |

失败时 journal `phase` 停在失败步 + `error`；`--resume` 从下一步安全点继续；不可恢复则 `--rollback` 或 DOC-081 人工回滚。

---

### 3. 自动处理清单（事务必须覆盖）

- [ ] 迁移锁 + 可恢复 journal（`run/workspace-migrate-journal.json`，原子写）
- [ ] 记录原 `desiredState` 与自启动 enabled 集合（daemon/manager/gateway）
- [ ] quiesce：停 daemon/manager/gateway，避免迁中写入
- [ ] 备份 registry、pageviews、manifest、配置、unit 文件
- [ ] 同文件系统原子改名；跨设备 **fail-closed**
- [ ] 结构化更新 manifest + registry 内部路径（禁止整树 sed）
- [ ] pageviews：依赖 BUG-383 稳定游标；迁前基线 + 迁后对账（不重复累计）
- [ ] 清陈旧容器身份；`lwa start` → compose `up -d`（BUG-382），非默认整镜像 rebuild
- [ ] 重新生成主 Caddyfile；sites/aliases 片段在 rebind 阶段边界安全改写绝对路径
- [ ] 重写 daemon、manager、gateway 三个自启动单元（仅迁前已装时 `repair` + preserve）
- [ ] 只清可重建缓存（capability-*）；**不删** `daemon-processed.json`
- [ ] 仅恢复迁前 running 意图实例；未装自启时拉回迁前 running 的 detached 控制面
- [ ] 验收：`autostart check`、manifest/Caddyfile/sites/aliases 无 OLD、统计不变量

---

### 4. Skill：`lwa-relocate-workspace`

**Files:** `src/local_webpage_access/skills/lwa-relocate-workspace/SKILL.md`（随 syncSkills 进工作区）

| 职责 | 不做 |
| --- | --- |
| 先跑 `relocate --dry-run --json` 并向用户解释停机范围、风险、预计动作 | 不直接 `sed` / `mv` / `rm` 状态文件 |
| 用户确认后调用 CLI（可加 `--yes`） | 不临场拼接第二套迁移脚本 |
| editable 仍指 OLD、WSL、跨盘拒绝、权限问题 → 针对性指导 | 不假装跨盘已支持 |
| 结束后汇总 journal + verify 为迁移报告 | — |

---

### 5. 方案对比（产品决策记录）

| 方案 | 评价 |
| --- | --- |
| 仅运维文档 | 成本低，易漏 registry/pageviews/gateway/容器；**不推荐**作唯一交付 |
| 仅 Skill | 友好但模型临场拼接，安全性/可重复性不足 |
| **CLI 事务 + Skill** | **推荐**：CLI 保一致性，Skill 管交互与排障 |

DOC-081 手册保留为：CLI 未就绪时的人工路径，以及 CLI 失败时的逃生舱。

---

### 6. 实现任务（bite-sized）

#### Task 1: 状态机骨架、锁与 journal

**Files:**
- Create: `src/local_webpage_access/workspace_migrate.py`
- Create: `tests/test_workspace_migrate.py`

**Step 1:** 失败测试 — `acquire_migrate_lock` 互斥；journal 原子写读；非法 phase 转换拒绝。
**Step 3:** `MigratePhase` 枚举、`MigrateJournal`、`with_migrate_lock`。
**Step 5:** Commit `feat: workspace migrate lock and journal`

---

#### Task 2: preflight（含同设备检测）

**Files:** 同上

**Step 1:** 测试 — `target_exists` / `cross_device` / `not_workspace` / `/mnt` WSL blocking；path holders 列表。
**Step 3:** `preflight_migrate(old, new) -> PreflightReport`（`ok`, `blocking`, `warnings`, `planned`）。
**Step 5:** Commit `feat: workspace migrate preflight`

---

#### Task 3: backup + snapshot

**Files:** 同上

**Step 1:** 测试 — 快照含 `restore_instance_ids`、`autostart_enabled`、`pageview_hits`；备份目录含 registry/pageviews/yml/manifests。
**Step 3:** `capture_snapshot` / `write_backup`；支持 `--snapshot-out`。
**Step 5:** Commit `feat: workspace migrate backup and snapshot`

---

#### Task 4: quiesce

**Files:** 同上 + fake runtime / autostart stubs

**Step 1:** 测试 — 只 stop 快照 running；compose down 容器实例；autostart disable；**不**删 daemon-processed。
**Step 3:** `quiesce_workspace(...)`。
**Step 5:** Commit `feat: workspace migrate quiesce`

---

#### Task 5: move（同盘原子）

**Step 1:** 同盘 rename 成功；跨设备 mock `EXDEV` → RelocateError。
**Step 3:** `move_workspace_root`。
**Step 5:** Commit `feat: workspace migrate atomic move`

---

#### Task 6: rebind（manifest + registry + 清容器身份）

**Step 1:** 前缀替换；非前缀子串不动；containerId 清空以便 BUG-382 up -d。
**Step 3:** 结构化 JSON 原子写 + 参数化 SQL。
**Step 5:** Commit `feat: workspace migrate path rebind`

---

#### Task 7: regenerate（Caddy + autostart 三单元）

**Step 1:** 断言调用 `StaticGateway._sync_main_config`（或公开 regenerate API）与 `autostart.repair(preserve_installed=True)`；只删 capability-*.json。
**Step 3:** 实现 regenerate phase。
**Step 5:** Commit `feat: workspace migrate regenerate caddy and autostart`

---

#### Task 8: restore + verify

**Step 1:** 只 start 快照 ID；verify 检查 Mounts 前缀、无关键配置 OLD、pageviews 未翻倍、可选 autostart check / capability。
**Step 3:** `restore_instances` / `verify_migrate`。
**Step 5:** Commit `feat: workspace migrate restore and verify`

---

#### Task 9: 顶层编排 + CLI

**Files:**
- Modify: `workspace_migrate.py` — `run_migrate(...)`
- Create: `src/local_webpage_access/cli/workspace.py`
- Modify: `cli/__init__.py` 注册 `workspace` 组

**Step 1:** dry-run 不 move；resume 从 journal 继续；rollback 在测试夹具上恢复。
**Step 3:** Typer 子命令对齐 §1。
**Step 5:** Commit `feat: add lwa workspace relocate CLI`

---

#### Task 10: Skill + 文档同步

**Files:**
- Create: `skills/lwa-relocate-workspace/SKILL.md`
- Modify: `docs/workspace-rename.md`（标题改为 LWA 工作区迁移；优先 CLI）
- Modify: README / playbook / faq 交叉链
- Modify: `design/plan/...2607.md` §24 状态随落地更新

**Step 5:** Commit `docs: add lwa-relocate-workspace skill and point manual to CLI`

---

#### Task 11（P2，可另 PR）— **未在本合集迭代落地**

> **2026-08-06**：下列三项未实现，已移植至 [`../plans/local-webpage-access-新增功能点2608.md`](../plans/local-webpage-access-新增功能点2608.md)。

- 路径相对化写入（减少未来 rebind 面）→ **IMP-049**（2608 §7）
- 生产 CLI 与工作区解耦安装脚本 → **IMP-050**（2608 §7）
- **IMP-042.b** 跨磁盘 copy 迁移 → **暂不开发**（不迁入 2608 待办；预检仍 blocking）

---

### 7. 测试与门禁

| 门禁 | 命令 |
| --- | --- |
| 单元 | `pytest tests/test_workspace_migrate.py -q` |
| 回归 | `pytest tests/test_lifecycle.py tests/test_autostart.py tests/test_pageviews.py tests/test_host_container.py -q` |
| Lint | `ruff check src/local_webpage_access/workspace_migrate.py src/local_webpage_access/cli/workspace.py` |
| 手工 | 副本工作区：`--dry-run --json` → 真迁 → `--verify`；故意跨盘应被拒绝 |

---

### 8. 风险

| 风险 | 缓解 |
| --- | --- |
| quiesce 后 move 前断电 | journal + 区外 `--snapshot-out`；rollback / DOC-081 |
| start 自愈失败 | 明确错误；提示 rebuild；不 purge data |
| repair 卸 gateway | 强制 preserve_installed（BUG-384） |
| pageviews 误判 | BUG-383 + 基线对账阈值 |
| editable 仍指 OLD | preflight warn；Skill 指导重装；不阻塞同盘 rename 本身 |

---

### 9. task-list 对照

| ID | 角色 |
| --- | --- |
| IMP-042 | 功能点（§24） |
| PLN-027 | 本计划（已规划） |
| DEV-089 | 实现主项 |
| DOC-081 | 人工手册 / 逃生舱 |
| BUG-382/383/384 | 迁移依赖的运行时修复 |

---

## 2026-07-30 · 管理页实例表布局修复设计

<a id="2026-07-30-管理页实例表布局修复设计"></a>

> 原文件：`2026-07-30-manager-table-layout-design.md`

### 背景

V0.6.10 为抑制名称列宽度变化，在整个实例表启用了 `table-layout: fixed`。由于表中只有名称列和操作列设置了宽度，其余列被平均压缩；单元格内容仍保持不换行，最终导致访问地址、端口、更新时间和操作区互相覆盖。

### 最终确认方案

- 表格、表头与全部列宽规则恢复 V0.6.9 实现，让 13 列都根据实际内容协调分配空间。
- 名称列不固定宽度，恢复 V0.6.9 的 `max-width: 220px`。
- 操作列恢复为仅设置 `min-width: 200px`，不保留 V0.6.10 的 240px 偏好宽度。
- 名称最多显示两行；超过两行时在第二行末尾显示省略号。
- 保留表格容器的横向滚动能力，空间不足时不挤压内容到相邻列。
- 不调整现有颜色、字号、按钮、数据结构与交互行为。

### 验收标准

1. `table.instances` 不再使用 `table-layout: fixed`。
2. 名称表头没有专用固定宽度类，名称单元格仅保留 V0.6.9 的 `max-width: 220px`。
3. 操作列仅保留 V0.6.9 的 `min-width: 200px`。
4. 唯一新增行为是名称按钮使用两行截断。
5. 管理页前端回归测试通过，并在实际页面中确认各列没有互相覆盖。

---

## 2026-07-30 · 管理页实例表布局修复实施计划

<a id="2026-07-30-管理页实例表布局修复实施计划"></a>

> 原文件：`2026-07-30-manager-table-layout.md`

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 完整恢复 V0.6.9 的实例表列宽规则，唯一新增名称最多两行省略。

**Architecture:** 保留现有 HTML 结构和横向滚动容器，只修改实例表相关 CSS。用静态资源回归测试锁定布局契约，再通过浏览器检查真实渲染结果。

**Tech Stack:** CSS、Vue 3 无构建静态前端、pytest、Node.js、Playwright

---

#### Task 1: 建立布局回归测试

**Files:**
- Modify: `tests/test_manager_static_app.py`

1. 新增 CSS 布局契约测试。
2. 运行测试并确认因 V0.6.10 的固定表格布局和单行名称规则而失败。

#### Task 2: 最小化修复布局

**Files:**
- Modify: `src/local_webpage_access/manager_static/style.css`

1. 删除整表固定布局及名称表头专用类。
2. 恢复名称单元格 `max-width: 220px` 和操作列 `min-width: 200px`。
3. 仅将名称按钮改为最多两行截断。
4. 运行管理页前端测试。

#### Task 3: 验证与任务同步

**Files:**
- Modify: `task-list.md`

1. 运行前端测试、全量测试、Ruff 和 mypy。
2. 用 Chromium 验证实际布局。
3. 使用任务清单 CLI 登记修复并校验摘要。

---

## 2026-08-03 · Task List 七项待办收口设计

<a id="2026-08-03-task-list-七项待办收口设计"></a>

> 原文件：`2026-08-03-tasklist-seven-items-design.md`

> **状态（V0.6.12）**：七项（BUG-369/370/371/420/421/422 + DEV-094）及审查跟进 BUG-423～427 已落地；详见同目录 implementation 计划与 `task-list.md`。

### 目标与范围

一次性核实并收口 `BUG-369`、`BUG-370`、`BUG-371`、`BUG-420`、`BUG-421`、`BUG-422` 与 `DEV-094`。对已被后续代码修复的历史项，以回归证据关闭，不重复改动；对仍存在的问题，按 TDD 逐项修复。

### 总体方案

采用“按风险分阶段、共享少量基础能力”的方案：

1. 先复核三个历史低优先项，区分真实遗留与任务状态陈旧。
2. 修复网关启动时序，保证 Caddy 首次加载当前工作区配置。
3. 建立只读的 Docker 挂载观测与规范路径计算能力，供容器生命周期和 doctor 共用。
4. 修复挂载漂移时的数据保护与容器重建流程。
5. 统一回写 manifest/registry 中可确定的工作区派生路径。
6. 将同一口径接入 doctor，在运维阶段主动暴露裸 `mv` 残留。

### 逐项设计

#### BUG-369：并发观测的可信状态

`_observe_container_status()` 在入口读取 `last_trusted_state`，异常发生时内层函数仍使用该快照。改为在记录 unknown 前重读 registry，优先保留并发更新后的 `last_trusted_state/status`。新增回归模拟观测过程中另一写者更新状态。

#### BUG-370：导入 ID 原子认领

当前 `_claim_unique_id(..., on_conflict="error")` 已将 registry 检查与 `mkdir(exist_ok=False)` 原子认领合并，并发失败直接抛错，对应 `BUG-313` 回归已存在。本项不重复修改产品代码，通过定向并发回归确认后关闭。

#### BUG-371：镜像 ID 兜底查询

有容器时仍优先 `docker inspect` 读取真实镜像 ID。无容器时不再拼接 `<project>-<service>` 假设镜像名，改用当前 Compose 文件的 `docker compose images -q <service>` 查询，自然支持顶层 `name`、自定义 `projectName`、显式 `image` 和 BuildKit。

#### BUG-420：Caddy 首次加载的配置

在 `StaticGateway` 中抽出“按磁盘片段组装并原子落盘主 Caddyfile”的纯写入方法，不 reload、不自愈启动。`start_gateway()` 在 `caddy_start()` 前无条件调用。`_sync_main_config()` 复用该方法后再 reload。保留已在线 master 的不重启语义。

#### BUG-421：Docker bind mount 漂移与数据保护

`DockerRuntime` 新增只读挂载观测，从容器 `inspect` 返回 bind mount 的 source/destination。`start_container()` 在“已运行直接返回”与“已停止直接 start”之前检查 LWA 管理的 SQLite data mount。

若 source 与当前 `workspace.app_data(instance_id)` 不同：

1. 调用现有 SQLite 数据救援；
2. `down` 删除旧容器；
3. 清空陈旧容器身份；
4. 使用当前 Compose `up -d` 显式重建；
5. 观测并回写新身份。

挂载观测失败时不执行破坏性操作，返回可诊断错误；无 SQLite data mount 的容器保持原行为。

#### BUG-422：manifest/registry 派生路径一致性

提供小型路径同步 helper，在容器成功托管/重建与静态站点成功启用后，回写：

- `manifest.appPath`
- `manifest.container.composePath`
- `manifest.container.dockerfilePath`
- `manifest.static.gatewayConfigPath`

完成后与 manifest 一起 upsert 到 registry。`sourceZipPath` 允许指向工作区外部，不做无条件改写。

#### DEV-094：doctor 工作区一致性检查

doctor 新增单一聚合检查，检查：

- 活跃 manifest/registry 的可确定派生路径是否等于当前 workspace 规范值；
- 主 Caddyfile 和 sites 片段中引用的本地路径是否存在；
- Docker 可用时，LWA 管理的 SQLite data mount 是否指向当前 data 目录。

历史 builds/events 和合法外部 `sourceZipPath` 不触发 WARN。诊断输出实例、字段、实际值、期望值和修复建议。

### 错误处理与安全边界

- 不对未知旧根路径执行全文盲替换。
- 容器挂载状态未知时不自动 down/recreate。
- 破坏性重建前复用现有 SQLite 数据救援保护。
- doctor 只读，仅诊断不自动修改。
- 不改写合法的外部源 ZIP 路径。

### 验证策略

每项遵循红-绿-重构：先写最小失败回归，确认失败原因，再实施最小修复。最终执行：

- 七项相关定向测试；
- 容器、网关、doctor、导入、生命周期相关套件；
- 全量 pytest；
- ruff 与 mypy；
- 语法/构建检查；
- Docker 环境可用时执行真实挂载回归。

验证通过后逐条更新 `task-list.md` 的完成时间、状态、修改文件和测试结果。

---

## 2026-08-03 · Task List 七项待办收口实施计划

<a id="2026-08-03-task-list-七项待办收口实施计划"></a>

> 原文件：`2026-08-03-tasklist-seven-items-implementation.md`

> **状态（V0.6.12）**：本计划已执行完毕；审查跟进的 BUG-423～427（down 失败中止、救援 fail-safe、compose config --images、Caddy 旧根归属、doctor 挂载 SKIP）亦已落地。历史步骤保留供审计。

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 逐项修复或验证关闭 `BUG-369/370/371/420/421/422` 与 `DEV-094`，并以自动化回归和全量门禁证明行为正确。

**Architecture:** 保持 gateway、Docker runtime、hosting、doctor 现有边界；仅在 Docker runtime 增加只读挂载观测，在 hosting 增加规范路径回写，供 doctor 以只读方式复用。每项独立进行 RED→GREEN→REFACTOR，高风险挂载重建路径保持 fail-safe。

**Tech Stack:** Python 3.12、pytest、Pydantic、SQLite、Docker Compose/Caddy CLI、ruff、mypy。

---

#### Task 1: BUG-369 并发观测可信状态

**Files:**
- Modify: `src/local_webpage_access/lifecycle.py:990-1053`
- Test: `tests/test_lifecycle.py`

**Step 1: Write the failing test**

新增用例：首次读到 `last_trusted_state=stopped`，在 `DockerRuntime.is_running()` 抛错前模拟并发写者将 registry 更新为 `running`；断言 unknown 记录保留 `running`。

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_lifecycle.py -k trusted_state_refresh -q`

Expected: FAIL，实际 `last_trusted_state` 被旧快照写回 `stopped`。

**Step 3: Write minimal implementation**

在 `_record_unknown()` 内重读 `registry.get_instance(instance_id)`，优先取最新 `last_trusted_state`，其次取最新 `status`，最后回退入口值。日志与 event 使用同一最新值。

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_lifecycle.py -k 'trusted_state_refresh or observation' -q`

#### Task 2: BUG-370 原子 ID claim 验证关闭

**Files:**
- Test: `tests/test_importer.py:760-800`
- Verify: `src/local_webpage_access/importer.py:1008-1050`

**Step 1: Run existing regression**

Run: `python3 -m pytest tests/test_importer.py -k 'conflict_error_mode_is_atomic or conflict_error_mode or conflict_rename' -q`

Expected: PASS，证明 `on_conflict=error` 在 `mkdir` 竞态时抛错而不是创建 `-2`。

**Step 2: Strengthen only if coverage is incomplete**

若现有用例未确认原始 slug 由并发者保留，先新增失败断言，再做最小实现修正。若覆盖完整，不修改产品代码。

#### Task 3: BUG-371 Compose 镜像 ID 兜底

**Files:**
- Modify: `src/local_webpage_access/docker_runtime.py:774-799`
- Test: `tests/test_docker_runtime.py:483-506`

**Step 1: Write the failing test**

新增用例：无容器、Compose 顶层使用自定义 project/image 时，期望执行 `docker compose ... images -q app`，而不是 `docker images -q lwa-api-app`。

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_docker_runtime.py -k image_id -q`

Expected: FAIL，调用仍是 `docker images -q`。

**Step 3: Write minimal implementation**

无容器时用 `_compose_cmd(instance_id, "images", "-q", service)`查询镜像 ID，保留查询失败/空输出返回 `None`。

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_docker_runtime.py -k image_id -q`

#### Task 4: BUG-420 Caddy 启动前主配置落盘

**Files:**
- Modify: `src/local_webpage_access/static_gateway.py:816-835`
- Modify: `src/local_webpage_access/gateway_service.py:252-277`
- Test: `tests/test_gateway_service.py:273-309`
- Test: `tests/test_static_gateway.py:1125-1145`

**Step 1: Write the failing tests**

1. 已有非空旧 Caddyfile 时，`start_gateway()` 必须在 `caddy_start` 前调用纯写入方法。
2. 纯写入方法只原子写文件，不调用 reload/self-heal。

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_gateway_service.py tests/test_static_gateway.py -k 'start_gateway and main_config or write_main_config' -q`

**Step 3: Write minimal implementation**

新增 `StaticGateway.write_main_config()`；`_sync_main_config()` 调用它后 reload；`start_gateway()` 在 `caddy_start()` 前调用它，删除“仅缺主配置才 sync”分支。builtin 清理后仍保留必要 reload。

**Step 4: Run focused suites**

Run: `python3 -m pytest tests/test_gateway_service.py tests/test_static_gateway.py -q`

#### Task 5: BUG-421 挂载漂移防护

**Files:**
- Modify: `src/local_webpage_access/docker_runtime.py`
- Modify: `src/local_webpage_access/hosting.py:295-324,515-630`
- Test: `tests/test_docker_runtime.py`
- Test: `tests/test_host_container.py`

**Step 1: Write failing runtime tests**

新增 `DockerRuntime.bind_mounts(instance_id, all_containers=True)` 的期望 API 测试：解析 inspect JSON 的 bind source/destination；无容器返回空；inspect 失败抛 `DockerError`。

Run: `python3 -m pytest tests/test_docker_runtime.py -k bind_mount -q`

Expected: FAIL，API 不存在。

**Step 2: Implement minimal read-only observation**

以 `docker inspect <cid> --format '{{json .Mounts}}'` 读取并归一化 `Type/Source/Destination`。

**Step 3: Write failing hosting tests**

1. running + mount 一致→保持跳过 start。
2. stopped + mount 一致→保持 `compose start`。
3. running/stopped + SQLite data mount 漂移→先 rescue，再 down/up，不调用 start。
4. inspect 失败→不 down/up，抛可诊断 `HostingError`。
5. 非 SQLite 或无管理 data mount→保持原行为。

Run: `python3 -m pytest tests/test_host_container.py -k mount_drift -q`

Expected: FAIL，旧路径仍直接 return/start。

**Step 4: Implement drift recovery**

在任何早返回前获取既有 container id 和挂载。仅对 manifest 声明 SQLite 的 data destination 比较 source。漂移时复用 `_rescue_container_data_before_rebuild()`，再 down、清 ID、up。

**Step 5: Run focused suites**

Run: `python3 -m pytest tests/test_docker_runtime.py tests/test_host_container.py -q`

#### Task 6: BUG-422 派生路径回写

**Files:**
- Modify: `src/local_webpage_access/hosting.py`
- Test: `tests/test_host_container.py`
- Test: static hosting tests selected by `rg '_enable_static|host_static' tests`

**Step 1: Write failing tests**

使用含旧绝对路径的 manifest，成功 host/start 后断言 manifest 与 registry 的 `appPath/composePath/dockerfilePath/gatewayConfigPath` 等于当前 workspace 规范值，同时外部 `sourceZipPath` 不变。

Run: `python3 -m pytest tests/test_host_container.py -k derived_path -q`

Expected: FAIL，陈旧字段被原样保留。

**Step 2: Implement minimal helper**

新增 `_refresh_manifest_workspace_paths(workspace, manifest)`，在成功保存/upsert 前调用；静态配置在 `_enable_static` 已生成当前 `gatewayConfigPath`，helper 作防御性一致化。

**Step 3: Run hosting tests**

Run: `python3 -m pytest tests/test_host_container.py tests/test_hosting.py -q`

#### Task 7: DEV-094 doctor 一致性检查

**Files:**
- Modify: `src/local_webpage_access/doctor.py`
- Modify: `src/local_webpage_access/docker_runtime.py` only if a read-only helper export is needed
- Test: `tests/test_doctor.py`

**Step 1: Write failing tests**

1. 当前规范路径→OK。
2. manifest/registry 派生字段陈旧→WARN，detail 包含实例、字段、实际/期望。
3. Caddy 片段引用不存在本地路径→WARN。
4. SQLite data mount 漂移→WARN。
5. 外部 `sourceZipPath` 与历史 builds/events→不告警。
6. Docker 不可用/观测失败→SKIP 挂载部分，仍返回其他路径检查结果。

Run: `python3 -m pytest tests/test_doctor.py -k workspace_consistency -q`

Expected: FAIL，检查函数尚不存在。

**Step 2: Implement read-only check**

新增 `check_workspace_path_consistency()` 并接入 `run_doctor()`。不复用 `_list_path_holders()`，而是对活跃字段生成当前期望值。Caddy 只解析 LWA 生成的 import/root/output file 本地路径。Docker 检查使用 Task 5 只读 API。

**Step 3: Run doctor suite**

Run: `python3 -m pytest tests/test_doctor.py -q`

#### Task 8: 集成验证与任务清单收口

**Files:**
- Modify: `task-list.md`

**Step 1: Run targeted suites**

Run: `python3 -m pytest tests/test_lifecycle.py tests/test_importer.py tests/test_docker_runtime.py tests/test_host_container.py tests/test_gateway_service.py tests/test_static_gateway.py tests/test_doctor.py -q`

**Step 2: Run static gates**

Run: `python3 -m compileall -q src/local_webpage_access tests`

Run: `python3 -m ruff check src tests`

Run: `python3 -m mypy src/local_webpage_access`

**Step 3: Run full test suite**

Run: `python3 -m pytest -q`

Expected: 0 failed；环境门禁测试只允许项目既定的 skip。

**Step 4: Run real Docker tests when available**

查找项目现有 Docker marker/用例并执行；若本机 Docker 不可用，记录明确原因而不伪报。

**Step 5: Update task list**

用 task-list CLI 逐条将七项更新为已修复/已完成，备注记录根因、文件、RED/GREEN 回归与全量门禁结果；重算统计摘要并执行 `check`。

**Step 6: Review final diff**

Run: `git diff --check`

Run: `git status --short`

确认不覆盖用户先前的 `task-list.md` 记录，且只修改计划内文件。

---

## 2026-08-06 · 文件夹导入识别/部署死胡同收口（BUG-443）

<a id="2026-08-06-文件夹导入识别部署死胡同收口bug-443"></a>

> 补记入本合集（非原 `design/plans/` 七篇之一）。活文档对应修订见 [`../plans/local-webpage-access-新增功能点2608.md`](../plans/local-webpage-access-新增功能点2608.md) **§5 IMP-047 落地后修订**。task-list：`BUG-443` / `OPS-097`。

### 问题与证据

管理页「本机文件夹」导入后，用户连续导入多个目录均停在「待识别」，启停按钮灰掉，无法走到可访问 URL。对照日志与 registry：

| 实例 | 源目录 | 识别结果 | 卡点 |
| --- | --- | --- | --- |
| `3d` / `4-output` | 仅有 `kakeya-3d-chapters.html`（无 `index.html`） | `unknown` → pending | Scanner / hosting **只认文件名 `index.html`** |
| `3-scripts` | 同上 + `vendor/` + 构建脚本 | `unknown` → pending | 同上 |
| `v1` / `3-src` | Vite 前端，已有 `dist/` | **`frontend-static` 成功** | 仍写 `status=pending`；`/api/import-from-dir` **不调用** `start_instance`；管理页把 `pending` 算进 `inProgress` → **禁用「启动」**（死胡同） |

对照：daemon 收 zip 在识别成功且 `resourceProfile ∈ {tiny,small}` 时会自动 `start_instance`；文件夹导入 API 缺对称一步。

### 方案（B → C → A）

1. **B · 状态语义**
   - `build_manifest_from_detection`：仅 `detection.pending`（或 `kind is None`）→ `status=pending`；识别成功 → **`status=stopped`**（可启动，文案「已停止」而非「待识别」）。
   - `apply_detection_to_manifest`：重扫时保留已 running/stopped 的生命周期；从真 pending 识别成功 → 升为 `stopped`。
   - 管理页：`stopped` 可点启动；真 `pending` 仍禁用启动（避免误部署完全无法识别的包）。

2. **C · 文件夹导入自动部署**
   - 抽出 `daemon.try_auto_start_after_import`（与 `process_zip` 同规则：非 pending + tiny/small → `start_instance`；启动失败置期望 running 待自愈）。
   - `POST /api/import-from-dir` 在导入成功后调用，响应增加 `autoStart: {action, note}`。

3. **A · 任意可打开 HTML 即静态**
   - Scanner：顶层/浅层存在任意 `*.html` 且无 Node/Python 工程信号 → `static`（不强制 `index.html`）。
   - `hosting.find_index_html`：`index.html` 优先，否则任意顶层/一层 `.html`。
   - 同步到 `public/` 后若无 `index.html`，将入口页复制为 `public/index.html`，保证 `GET /` 可开。

### 明确不做（本期）

- 不对「仅有 `.txt` / 无 HTML」的目录强行托管。
- CLI `lwa import --from-dir` 仍可不自动 start（与历史 CLI 习惯一致）；管理页导入对齐 daemon。若后续要 CLI 对称，另开子项。
- 不做「试探性临时 http.server 探活」式识别（可选后续）。

### 触点文件

- `scanner.py`（`has_html` / `_has_html_anywhere`）
- `importer.py`（`build_manifest_from_detection` / `apply_detection_to_manifest`）
- `hosting.py`（`find_index_html` / `_ensure_public_index`）
- `daemon.py`（`try_auto_start_after_import` / `AUTO_START_PROFILES`）
- `manager_api.py`（`import-from-dir` 自动 start）
- 回归：`test_scanner` / `test_importer` / `test_hosting` / `test_manager_api` / `test_daemon` / `test_manager_static_app`

### 验收（2026-08-06 runtime）

| 步骤 | 结果 |
| --- | --- |
| `4-output` 仅留 `kakeya-3d-chapters.html` → detect | `static` / high / 非 pending |
| 管理页 API 自该目录新建导入 | `autoStart.action=started`，端口可 `HTTP 200` |
| `3-src` 关联实例 `v1` restart | `http://127.0.0.1:18006/` → 200 |
| `4-output` 关联实例 `3d` update/restart + 别名 | `18002/` 与 `/3d-kakeya-animation/` → 200 |
| 定向 pytest（scanner/importer/hosting/daemon/folder/manager 切片） | 全绿 |

### 与 IMP-047 关系

IMP-047（文件夹源复制进工作区）主路径已在 DEV-096 落地；本块是其**部署闭环与识别容错**补丁，不改「关联≠就地运行」红线。方案描述以 2608 §5「落地后修订」为权威。

---
