# 管理页实例表布局修复 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 完整恢复 V0.6.9 的实例表列宽规则，唯一新增名称最多两行省略。

**Architecture:** 保留现有 HTML 结构和横向滚动容器，只修改实例表相关 CSS。用静态资源回归测试锁定布局契约，再通过浏览器检查真实渲染结果。

**Tech Stack:** CSS、Vue 3 无构建静态前端、pytest、Node.js、Playwright

---

### Task 1: 建立布局回归测试

**Files:**
- Modify: `tests/test_manager_static_app.py`

1. 新增 CSS 布局契约测试。
2. 运行测试并确认因 V0.6.10 的固定表格布局和单行名称规则而失败。

### Task 2: 最小化修复布局

**Files:**
- Modify: `src/local_webpage_access/manager_static/style.css`

1. 删除整表固定布局及名称表头专用类。
2. 恢复名称单元格 `max-width: 220px` 和操作列 `min-width: 200px`。
3. 仅将名称按钮改为最多两行截断。
4. 运行管理页前端测试。

### Task 3: 验证与任务同步

**Files:**
- Modify: `task-list.md`

1. 运行前端测试、全量测试、Ruff 和 mypy。
2. 用 Chromium 验证实际布局。
3. 使用任务清单 CLI 登记修复并校验摘要。
