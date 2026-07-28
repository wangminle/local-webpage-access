# Pending Bug Remediation Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use executing-plans to implement this plan task-by-task.

**Goal:** 核实并收口 `task-list.md` 中 BUG-293～BUG-320 全部 28 条待修复代码 Bug。

**Architecture:** 按共享状态、安全边界、daemon、生命周期、时区五批推进。每条先以失败测试确认，再做最小实现；共享原语先于调用方，避免重复返工。

**Tech Stack:** Python 3.11+、SQLite、FastAPI/Typer、pytest、Ruff、Mypy。

---

### Task 1: 构建队列取消、CAS 与死进程回收（BUG-293、307、308）

**Files:**
- Modify: `src/local_webpage_access/build_queue.py`
- Test: `tests/test_build_queue.py`

**Steps:**
1. 为排队取消立即中断等待、旧 token 禁止覆盖新代际、终态不被竞态改写、死 owner 的孤儿 worker 被安全终止分别写失败测试。
2. 运行新增用例，确认各自因现实现缺陷失败。
3. 为 `acquire` 增加带 token 的取消探测；所有任务更新增加 token/CAS 条件；回收前按 pid/pgid/identity 安全终止 worker。
4. 运行新增用例与 `tests/test_build_queue.py`。

### Task 2: SQLite 迁移与 manager 单实例锁（BUG-294、309）

**Files:**
- Modify: `src/local_webpage_access/registry/connection.py`
- Modify: `src/local_webpage_access/manager_service.py`
- Test: `tests/test_registry.py`
- Test: `tests/test_manager_service.py`

**Steps:**
1. 写双连接并发迁移和“锁持有者存活超过 60 秒仍不可抢锁”失败测试。
2. 在迁移写事务内重新读取版本并串行升级；manager 锁只按 pid/身份存活判断，不按 mtime 偷锁。
3. 运行两模块定向测试。

### Task 3: API/路径/仓库/CLI 安全校验（BUG-295、304、306、310、314～317）

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

### Task 4: daemon 退出、重试、启动、信号、稳定窗口、退避与导入原子性（BUG-296～299、311～313）

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

### Task 5: 生命周期与网关恢复（BUG-300～302、305、318～320）

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

### Task 6: 浏览量 UTC 规范化（BUG-303）

**Files:**
- Modify: `src/local_webpage_access/pageviews.py`
- Test: `tests/test_pageviews.py`

**Steps:**
1. 写带时区偏移和本地 naive 时间的分桶、last_seen 排序失败测试。
2. 所有解析结果在入库前转换为 UTC ISO8601，按 UTC 日期分桶并用规范化值比较。
3. 运行 pageviews 定向回归。

### Task 7: 全量验证与任务清单同步

**Files:**
- Modify: `task-list.md`

**Steps:**
1. 运行全部相关模块测试和完整 `python3 -m pytest -q`。
2. 运行 `python3 -m ruff check src tests`、`python3 -m mypy src/local_webpage_access`、`git diff --check`。
3. 逐条依据测试证据更新 BUG-293～320 状态、完成时间和备注。
4. 用 task-list CLI 重算摘要并执行 `check`。
