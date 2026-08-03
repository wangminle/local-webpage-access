# Task List 七项待办收口 Implementation Plan

> **状态（V0.6.12）**：本计划已执行完毕；审查跟进的 BUG-423～427（down 失败中止、救援 fail-safe、compose config --images、Caddy 旧根归属、doctor 挂载 SKIP）亦已落地。历史步骤保留供审计。

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 逐项修复或验证关闭 `BUG-369/370/371/420/421/422` 与 `DEV-094`，并以自动化回归和全量门禁证明行为正确。

**Architecture:** 保持 gateway、Docker runtime、hosting、doctor 现有边界；仅在 Docker runtime 增加只读挂载观测，在 hosting 增加规范路径回写，供 doctor 以只读方式复用。每项独立进行 RED→GREEN→REFACTOR，高风险挂载重建路径保持 fail-safe。

**Tech Stack:** Python 3.12、pytest、Pydantic、SQLite、Docker Compose/Caddy CLI、ruff、mypy。

---

### Task 1: BUG-369 并发观测可信状态

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

### Task 2: BUG-370 原子 ID claim 验证关闭

**Files:**
- Test: `tests/test_importer.py:760-800`
- Verify: `src/local_webpage_access/importer.py:1008-1050`

**Step 1: Run existing regression**

Run: `python3 -m pytest tests/test_importer.py -k 'conflict_error_mode_is_atomic or conflict_error_mode or conflict_rename' -q`

Expected: PASS，证明 `on_conflict=error` 在 `mkdir` 竞态时抛错而不是创建 `-2`。

**Step 2: Strengthen only if coverage is incomplete**

若现有用例未确认原始 slug 由并发者保留，先新增失败断言，再做最小实现修正。若覆盖完整，不修改产品代码。

### Task 3: BUG-371 Compose 镜像 ID 兜底

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

### Task 4: BUG-420 Caddy 启动前主配置落盘

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

### Task 5: BUG-421 挂载漂移防护

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

### Task 6: BUG-422 派生路径回写

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

### Task 7: DEV-094 doctor 一致性检查

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

### Task 8: 集成验证与任务清单收口

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
