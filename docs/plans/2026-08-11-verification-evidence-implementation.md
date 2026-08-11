# Deployment Verification Evidence Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development to implement this plan task-by-task.

**Goal:** 用真实运行时证据验证 API、SQLite 和 Alembic 能力，补齐 Node 启动脚本型子目录识别，并清零本轮 ruff/mypy 门禁错误。

**Architecture:** 将“契约要求”与“已观测证据”分离：探针结果、SQLite 只读检查和受控迁移命令分别产生能力证据。Node 候选生成使用“已知后端依赖或保守的服务启动脚本”规则。

**Tech Stack:** Python 3.11+、Pydantic、pytest、sqlite3、ruff、mypy

---

### Task 1: 用证据驱动成功谓词

**Files:**
- Modify: `tests/test_gate_c_verification.py`
- Modify: `src/local_webpage_access/health.py`
- Modify: `src/local_webpage_access/hosting.py`

**Step 1: Write the failing tests**

- 契约要求 API/DB/迁移，但只有首页 200 时必须 failed。
- 有效 API 探针成功时可观测 `api`。
- `has_database=True` / `has_migrations=True` 时分别观测对应能力。

**Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_gate_c_verification.py::TestBackendCapabilityObservation`

Expected: FAIL because the current implementation derives all capabilities from the contract.

**Step 3: Write minimal implementation**

- 删除基于 `capability_contract` 布尔值补齐 `observed` 的代码。
- API 由真实成功的探针结果记录。
- DB/迁移仅使用调用方传入的已验证布尔值。
- failed 错误中列出未观测能力。

**Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_gate_c_verification.py::TestBackendCapabilityObservation`

Expected: PASS.

### Task 2: SQLite 和 Alembic 运行时证据

**Files:**
- Modify: `tests/test_gate_c_verification.py`
- Modify: `tests/test_compose.py`
- Modify: `src/local_webpage_access/hosting.py`

**Step 1: Write the failing tests**

- SQLite 目标文件存在且可只读查询时返回真。
- 文件缺失或损坏时返回假。
- `alembic upgrade head && exec <server>` 且服务存活时返回真；无受控顺序时返回假。

**Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_gate_c_verification.py -k 'sqlite_evidence or migration_evidence'`

Expected: FAIL because evidence helpers do not exist.

**Step 3: Write minimal implementation**

- 从 `manifest.database.dbFilename` 确定挂载目标 basename。
- 通过 `sqlite3.connect("file:...?mode=ro", uri=True)` 和 `PRAGMA schema_version` 验证。
- 检查启动命令中 Alembic 是否在 `&&` 的服务命令之前。
- 将结果传入容器成功谓词。

**Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_gate_c_verification.py -k 'sqlite_evidence or migration_evidence'`

Expected: PASS.

### Task 3: Node 启动脚本与子目录 Alembic

**Files:**
- Modify: `src/local_webpage_access/models.py`
- Modify: `src/local_webpage_access/evidence_collector.py`
- Modify: `src/local_webpage_access/candidate_generator.py`
- Modify: `tests/test_gate_c_verification.py`

**Step 1: Write the failing tests**

- `server/package.json` 只有 `start: node server.js` 时产生 primary Node backend 候选。
- Vite/React 前端 `start` 脚本不产生 backend 候选。
- `backend/alembic.ini` 使对应 Python 后端计划声明 `requiresMigrations=True`。

**Step 2: Run test to verify it fails**

Run: `pytest -q tests/test_gate_c_verification.py::TestNodeSubdirCandidate`

Expected: FAIL for start-script-only and subdirectory Alembic cases.

**Step 3: Write minimal implementation**

- `SubdirSignal` 增加 `hasAlembicIni`。
- 收集子目录 `alembic.ini`。
- 新增 `_is_node_backend_start_script()` 保守判断器。
- 生成候选和计划时同时消费依赖、脚本与 Alembic 证据。

**Step 4: Run test to verify it passes**

Run: `pytest -q tests/test_gate_c_verification.py::TestNodeSubdirCandidate`

Expected: PASS.

### Task 4: 清零 ruff 和 mypy 门禁

**Files:**
- Modify: the exact files reported by `ruff check src tests` and `mypy src/local_webpage_access`

**Step 1: Capture current failures**

Run: `ruff check src tests` and `mypy src/local_webpage_access`

Expected: 23 ruff errors and 7 mypy errors.

**Step 2: Apply minimal corrections**

- 删除未使用导入/变量和多余 f-string。
- 用 `TYPE_CHECKING` 或模块级导入解决前向类型名。
- 对可空字段做显式缩窄。
- 移除无效的 Pydantic 构造参数，修正实际字段映射。

**Step 3: Verify both gates**

Run: `ruff check src tests && mypy src/local_webpage_access`

Expected: both commands exit 0.

### Task 5: 完整回归与台账

**Files:**
- Modify: `task-list.md`

**Step 1: Run focused regression**

Run: `pytest -q tests/test_gate_c_verification.py tests/test_compose.py tests/test_preflight.py`

Expected: PASS.

**Step 2: Run full verification**

Run: `pytest -q && ruff check src tests && mypy src/local_webpage_access && git diff --check`

Expected: pytest has no failures; all static gates exit 0.

**Step 3: Synchronize task ledger**

- 将 `BUG-481` 和 `BUG-485` 更新为 `已修复`。
- 为新发现的子目录 Alembic 契约缺口和质量门禁修复增加记录。
- 运行 task-list CLI `check`。
