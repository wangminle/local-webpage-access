# LWA 工作区迁移（workspace relocate）Implementation Plan

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

## 0. 范围边界（已拍板）

| 场景 | 首版（IMP-042 v1） | 后续 |
| --- | --- | --- |
| **同一卷（same filesystem）原子改名** — **macOS / Linux 裸机 / WSL 内 Linux 盘** | ✅ **唯一支持的自动路径**（`os.rename`；自启走 launchd 或 systemd user） | — |
| 跨磁盘 / 跨卷复制（不同 mount、需 copy+verify+swap） | ❌ 预检 **blocking** 并指向手册 | IMP-042.b |
| 跨机器迁移（镜像、LAN、token、用户） | ❌ 不在范围 | 另案 |
| WSL `/mnt/<drive>` → `~/…`（通常跨设备） | ❌ 自动拒绝（同跨盘）；若碰巧同设备则按同卷 rename | 随 042.b |
| Windows 原生进程 | ❌ | — |

**理由：** 「同卷 vs 跨卷 vs 跨机」的备份/停机/回滚模型差一个数量级，与 OS 品牌无关。macOS 与 Linux 在同卷 rename + 各自自启后端（launchd / systemd）上均可走同一状态机。

---

## 1. CLI 表面（唯一执行入口）

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

## 2. 状态机

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

## 3. 自动处理清单（事务必须覆盖）

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

## 4. Skill：`lwa-relocate-workspace`

**Files:** `src/local_webpage_access/skills/lwa-relocate-workspace/SKILL.md`（随 syncSkills 进工作区）

| 职责 | 不做 |
| --- | --- |
| 先跑 `relocate --dry-run --json` 并向用户解释停机范围、风险、预计动作 | 不直接 `sed` / `mv` / `rm` 状态文件 |
| 用户确认后调用 CLI（可加 `--yes`） | 不临场拼接第二套迁移脚本 |
| editable 仍指 OLD、WSL、跨盘拒绝、权限问题 → 针对性指导 | 不假装跨盘已支持 |
| 结束后汇总 journal + verify 为迁移报告 | — |

---

## 5. 方案对比（产品决策记录）

| 方案 | 评价 |
| --- | --- |
| 仅运维文档 | 成本低，易漏 registry/pageviews/gateway/容器；**不推荐**作唯一交付 |
| 仅 Skill | 友好但模型临场拼接，安全性/可重复性不足 |
| **CLI 事务 + Skill** | **推荐**：CLI 保一致性，Skill 管交互与排障 |

DOC-081 手册保留为：CLI 未就绪时的人工路径，以及 CLI 失败时的逃生舱。

---

## 6. 实现任务（bite-sized）

### Task 1: 状态机骨架、锁与 journal

**Files:**
- Create: `src/local_webpage_access/workspace_migrate.py`
- Create: `tests/test_workspace_migrate.py`

**Step 1:** 失败测试 — `acquire_migrate_lock` 互斥；journal 原子写读；非法 phase 转换拒绝。  
**Step 3:** `MigratePhase` 枚举、`MigrateJournal`、`with_migrate_lock`。  
**Step 5:** Commit `feat: workspace migrate lock and journal`

---

### Task 2: preflight（含同设备检测）

**Files:** 同上

**Step 1:** 测试 — `target_exists` / `cross_device` / `not_workspace` / `/mnt` WSL blocking；path holders 列表。  
**Step 3:** `preflight_migrate(old, new) -> PreflightReport`（`ok`, `blocking`, `warnings`, `planned`）。  
**Step 5:** Commit `feat: workspace migrate preflight`

---

### Task 3: backup + snapshot

**Files:** 同上

**Step 1:** 测试 — 快照含 `restore_instance_ids`、`autostart_enabled`、`pageview_hits`；备份目录含 registry/pageviews/yml/manifests。  
**Step 3:** `capture_snapshot` / `write_backup`；支持 `--snapshot-out`。  
**Step 5:** Commit `feat: workspace migrate backup and snapshot`

---

### Task 4: quiesce

**Files:** 同上 + fake runtime / autostart stubs

**Step 1:** 测试 — 只 stop 快照 running；compose down 容器实例；autostart disable；**不**删 daemon-processed。  
**Step 3:** `quiesce_workspace(...)`。  
**Step 5:** Commit `feat: workspace migrate quiesce`

---

### Task 5: move（同盘原子）

**Step 1:** 同盘 rename 成功；跨设备 mock `EXDEV` → RelocateError。  
**Step 3:** `move_workspace_root`。  
**Step 5:** Commit `feat: workspace migrate atomic move`

---

### Task 6: rebind（manifest + registry + 清容器身份）

**Step 1:** 前缀替换；非前缀子串不动；containerId 清空以便 BUG-382 up -d。  
**Step 3:** 结构化 JSON 原子写 + 参数化 SQL。  
**Step 5:** Commit `feat: workspace migrate path rebind`

---

### Task 7: regenerate（Caddy + autostart 三单元）

**Step 1:** 断言调用 `StaticGateway._sync_main_config`（或公开 regenerate API）与 `autostart.repair(preserve_installed=True)`；只删 capability-*.json。  
**Step 3:** 实现 regenerate phase。  
**Step 5:** Commit `feat: workspace migrate regenerate caddy and autostart`

---

### Task 8: restore + verify

**Step 1:** 只 start 快照 ID；verify 检查 Mounts 前缀、无关键配置 OLD、pageviews 未翻倍、可选 autostart check / capability。  
**Step 3:** `restore_instances` / `verify_migrate`。  
**Step 5:** Commit `feat: workspace migrate restore and verify`

---

### Task 9: 顶层编排 + CLI

**Files:**
- Modify: `workspace_migrate.py` — `run_migrate(...)`
- Create: `src/local_webpage_access/cli/workspace.py`
- Modify: `cli/__init__.py` 注册 `workspace` 组

**Step 1:** dry-run 不 move；resume 从 journal 继续；rollback 在测试夹具上恢复。  
**Step 3:** Typer 子命令对齐 §1。  
**Step 5:** Commit `feat: add lwa workspace relocate CLI`

---

### Task 10: Skill + 文档同步

**Files:**
- Create: `skills/lwa-relocate-workspace/SKILL.md`
- Modify: `docs/workspace-rename.md`（标题改为 LWA 工作区迁移；优先 CLI）
- Modify: README / playbook / faq 交叉链
- Modify: `design/plan/...2607.md` §24 状态随落地更新

**Step 5:** Commit `docs: add lwa-relocate-workspace skill and point manual to CLI`

---

### Task 11（P2，可另 PR）

- 路径相对化写入（减少未来 rebind 面）
- 生产 CLI 与工作区解耦安装脚本
- **IMP-042.b** 跨磁盘 copy 迁移（独立状态机与双倍磁盘预检）

---

## 7. 测试与门禁

| 门禁 | 命令 |
| --- | --- |
| 单元 | `pytest tests/test_workspace_migrate.py -q` |
| 回归 | `pytest tests/test_lifecycle.py tests/test_autostart.py tests/test_pageviews.py tests/test_host_container.py -q` |
| Lint | `ruff check src/local_webpage_access/workspace_migrate.py src/local_webpage_access/cli/workspace.py` |
| 手工 | 副本工作区：`--dry-run --json` → 真迁 → `--verify`；故意跨盘应被拒绝 |

---

## 8. 风险

| 风险 | 缓解 |
| --- | --- |
| quiesce 后 move 前断电 | journal + 区外 `--snapshot-out`；rollback / DOC-081 |
| start 自愈失败 | 明确错误；提示 rebuild；不 purge data |
| repair 卸 gateway | 强制 preserve_installed（BUG-384） |
| pageviews 误判 | BUG-383 + 基线对账阈值 |
| editable 仍指 OLD | preflight warn；Skill 指导重装；不阻塞同盘 rename 本身 |

---

## 9. task-list 对照

| ID | 角色 |
| --- | --- |
| IMP-042 | 功能点（§24） |
| PLN-027 | 本计划（已规划） |
| DEV-089 | 实现主项 |
| DOC-081 | 人工手册 / 逃生舱 |
| BUG-382/383/384 | 迁移依赖的运行时修复 |
