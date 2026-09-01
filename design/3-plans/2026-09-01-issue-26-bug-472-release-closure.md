# Issue #26 与 BUG-472 发布收口实施计划

> **For Codex:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task.

**目标：**发布 Gateway 启动锁竞态修复，统一 BUG-472 的已定案文档口径，并评论、关闭 GitHub Issue #26。

**架构：**保留已通过红绿回归的 `RLock + thread-local depth + flock` 启动锁实现，由 `ensure_caddy_running` / `start_gateway` / `stop_gateway_internal` 共用临界区，并在 update 收尾检查单 Caddy master。BUG-472 不增加自定义 Dockerfile 运行时，仅将文档统一为“LWA 生成自己的 Dockerfile，发现项目自带文件时明确警告”。

**技术栈：** Python 3、pytest、ruff、mypy、Git/GitHub CLI、Markdown 任务台账。

---

### 任务 1：核验修复边界

**文件：**
- 检查：`src/local_webpage_access/gateway_service.py`
- 检查：`src/local_webpage_access/static_gateway.py`
- 检查：`src/local_webpage_access/updater.py`
- 测试：`tests/test_gateway_start_race.py`
- 测试：`tests/test_static_gateway.py`

1. 审查 #26 的未提交差异与回归覆盖。
2. 运行 Gateway 专项测试，确认跨线程顺序进入、可重入、fail-safe 与单 master 检查通过。
3. 运行 BUG-472/CHK-V06 预检测试，确认项目自带 Dockerfile 会告警但不改变 LWA 生成策略。

### 任务 2：统一文档和版本

**文件：**
- 修改：`design/2-achievement/Scanner架构设计分析-多候选与实证校验-20260811.md`（2026-09-01 归档迁入，原位于 design/3-plans）
- 修改：`design/3-plans/github-issues-quality-patterns-20260818.md`
- 修改：`design/2-achievement/锁心跳与Gateway启动锁一致性修复归档-20260901.md`
- 修改：`README.md`
- 修改：`docs/release-checklist.md`
- 修改：`pyproject.toml`
- 修改：`src/local_webpage_access/version_info.py`
- 修改：`src/local_webpage_access/cli/__init__.py`
- 修改：`src/local_webpage_access/skills/lwa-update-runtime/SKILL.md`
- 修改：`tests/test_version_info.py`

1. 将 BUG-472 改为“产品决策已完成，CHK-V06 告警即为最终行为”。
2. 将 #26 从“本地候选/待发布”改为 `V0.8.10` 已发布并关闭。
3. 统一包版本、fallback、CLI 文案、runtime skill 和版本回归为 `0.8.10`。

### 任务 3：发布门禁与台账

**文件：**
- 修改：`task-list.md`

1. 运行全量 pytest、ruff、mypy、`git diff --check` 与任务台账校验。
2. 新增文档收口和发布运维条目，记录测试、提交、推送及 Issue 处置。
3. 使用发布主题 `V0.8.10-Build3471-20260901` 提交并推送 `main`。

### 任务 4：GitHub Issue 收口

1. 在 Issue #26 评论中说明根因、修复层次、测试证据和发布版本。
2. 以 completed 理由关闭 Issue #26。
3. 重新查询 Issue 和远端 `main`，确认状态为 CLOSED 且发布提交可见。
