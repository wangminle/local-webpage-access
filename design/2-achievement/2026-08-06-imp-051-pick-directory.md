# IMP-051 管理页「选择文件夹」Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在「从文件夹导入」对话框为源路径提供宿主机原生目录选择器；仅 loopback 启用。

**Architecture:** 前端仅在 `isLocalhostAccess()` 时启用按钮；点击后 `POST /api/pick-directory`（需鉴权且服务端再验 loopback）。manager 进程在宿主机调起 macOS `osascript` / Linux `zenity|kdialog`，返回绝对路径回填输入框。取消/超时/无 GUI 为业务错误，手输始终可用。

**Tech Stack:** Python 3 + FastAPI、subprocess、Vue2 管理页静态资源、pytest。

**定稿约束（产品）：** 仅 loopback 启用按钮；非 loopback 禁用并提示粘贴 LWA 机器绝对路径；禁止浏览器 `webkitdirectory` 假路径。

---

### Task 1: DirectoryPickerError + pick_directory helper

**Files:**
- Create: `src/local_webpage_access/directory_picker.py`
- Modify: `src/local_webpage_access/errors.py`
- Test: `tests/test_directory_picker.py`

**Step 1:** 写失败测试：`pick_directory` mock 子进程成功返回绝对路径；取消 → `DirectoryPickerError(code=cancelled)`；无工具 → `unavailable`；超时 → `timeout`。

**Step 2:** 实现 `pick_directory(*, timeout=120, runner=subprocess.run)`：darwin→osascript；linux→zenity 优先否则 kdialog；规范化去尾 `/`（根除外）。

**Step 3:** 测试转绿。

---

### Task 2: Manager API `POST /api/pick-directory`

**Files:**
- Modify: `src/local_webpage_access/manager_api.py`
- Modify: `src/local_webpage_access/errors.py`（若未在 Task1）
- Test: `tests/test_manager_api.py`

**Step 1:** 测试：loopback + mock picker → 200 `{path}`；非 loopback → 403 `loopback_required`；cancelled → 400；未鉴权 LAN → 401。

**Step 2:** 端点：`dependencies=[api]`；先 `_is_localhost_client` 否则 403；调用 `pick_directory`；映射 `DirectoryPickerError`。

**Step 3:** 测试转绿。

---

### Task 3: 管理页 UI

**Files:**
- Modify: `manager_static/app.js`、`style.css`
- Test: `tests/test_manager_static_app.py`

**Step 1:** 测试模板含「选择文件夹」、`pickFolder`/`isLocalhostAccess` 相关结构；loopback 启用、非 loopback 禁用文案。

**Step 2:** 路径行改为 input+button；`folderImport.picking`；hint 更新；`pickFolder` 调 API 回填。

**Step 3:** 测试转绿。

---

### Task 4: 文档与台账

**Files:**
- Modify: `docs/manager-page.md`、`design/plans/...2608.md` §6 状态、`task-list.md`（PLN-035、DEV-*）

**Step 1:** 文档写清 loopback-only 与 API。
**Step 2:** 关闭 PLN-035；新增 DEV 已完成。

---

### Task 5: 回归

Run: `pytest tests/test_directory_picker.py tests/test_manager_api.py tests/test_manager_static_app.py tests/test_folder_source.py -q`
