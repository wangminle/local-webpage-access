# Lock Heartbeat Consistency Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 消除锁心跳刷新期间的瞬时空载荷，并让内部读者对短暂不完整载荷做有界重试。

**Architecture:** 心跳写入统一复用 `write_lock_payload`，保持同一 inode 并将完整写入放在截断之前。`lifecycle` 使用一个私有读取器解析 PID/时间戳，对不完整载荷做少量立即重试，然后安全降级。

**Tech Stack:** Python 3.11+、pytest、POSIX/Windows 文件锁抽象、ruff、mypy。

---

### Task 1: 锁心跳一致写入与读取

**Files:**
- Modify: `tests/test_lifecycle.py`
- Modify: `src/local_webpage_access/lifecycle.py`
- Modify: `task-list.md`

**Step 1: Write the failing tests**

- 增加 `test_touch_lock_heartbeat_never_exposes_empty_payload`：monkeypatch `file_lock.os.ftruncate`，在真实截断边界读取路径，要求观测点必然发生且心跳载荷已包含 PID 和时间戳。旧实现未调用该写后截断路径，应稳定失败。
- 增加 `_read_lock_payload` 用例：首次不完整、第二次完整时返回有效元组；持续单行但 PID 合法时返回 `(pid, 0.0)` 供 mtime 兜底；持续无效时返回 `(0, 0.0)`。

**Step 2: Run tests to verify RED**

Run: `pytest -q tests/test_lifecycle.py -k 'heartbeat_never_truncates or read_lock_payload'`

Expected: FAIL，原因分别是旧写入顺序暴露空载荷、新读取器尚未实现。

**Step 3: Write minimal implementation**

- 新增 `_read_lock_payload(lock_path, attempts=3) -> tuple[int, float]`，只在载荷缺失、不完整或格式无效时重试，不引入长时间等待。
- `_lock_timeout_message` 和 `_lock_is_stale` 复用该读取器。
- `_touch_lock_heartbeat` 在同一打开文件上调用 `write_lock_payload(fh.fileno(), payload)`，取消先 `truncate`。

**Step 4: Run tests to verify GREEN**

Run: `pytest -q tests/test_lifecycle.py`

Expected: PASS。

**Step 5: Run repository gates**

Run: `pytest -q`

Run: `ruff check src tests && mypy src/local_webpage_access && git diff --check`

Expected: 全部通过；只有显式需要 Docker 环境的测试可跳过。

**Step 6: Synchronize the task ledger**

- 将 `BUG-606` 更新为已修复，记录实现与验证证据。
- 追加一条已完成检查事项，记录 RED/GREEN 与全量门禁。
- 运行 task-list CLI `check`。
