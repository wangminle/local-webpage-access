# LWA 代码审查报告：hosting.py + lifecycle.py（Group 2）

审查范围：`src\local_webpage_access\hosting.py`（1905 行）、`src\local_webpage_access\lifecycle.py`（2423 行）。
对照模块：file_lock.py / ports.py / registry(dao).py / models.py / errors.py / docker_runtime.py / static_gateway.py / build_queue.py / build_process.py / paths.py / compose.py / daemon.py / path_alias.py / health.py / probe.py。
测试验证：`tests\test_hosting.py`（37 通过，2 POSIX 跳过）、`tests\test_host_container.py`（37 通过）、`tests\test_lifecycle.py`（**2 失败**：锁心跳相关，见发现 #2）。全部为只读审查 + 运行既有测试，未修改任何文件。

---

## 1) 每个文件一行总结

- **hosting.py**：静态托管 / 前端构建 / 容器托管与轻量启动的编排层（含 Gate-C 验证、端口分配-回滚、run_command 进程树管理）。**有疑点**：端口回滚成对性与 `_rollback_attempt` 跨模块契约冲突、up 后写盘失败的孤儿容器、`_enable_static` 成功后写失败的端口泄漏、`_probe_path` 语义冗余。
- **lifecycle.py**：生命周期编排（start/stop/restart/rebuild/remove/observe）+ 双层锁（RLock+文件锁+心跳）+ Gate-C fallback/回滚/指纹。**有疑点**：Windows 上 msvcrt 字节锁使心跳与陈旧判定整体失效（测试实证）、`_rollback_attempt` 端口释放条件破坏复用端口保留契约、心跳线程 join 竞态、fallback 循环异常吞噬面过窄等。

---

## 2) 发现清单

### [严重度: major] lifecycle.py:1017-1023（配合 hosting.py:1000-1029 / 1032-1064） `_rollback_attempt` 端口释放条件破坏「复用端口保留」契约（BUG-045 / BUG-182）

- **说明**：`_rollback_attempt` 第 2 步回滚端口时，条件为 `if not manifest.container or not manifest.container.containerId: allocator.release_instance(instance_id)`，即**只要 containerId 为空就释放该实例的全部端口登记**，完全不区分「本轮新分配」与「上一轮成功部署留下的复用登记」。而 hosting 侧 `_ensure_static_port` / `_ensure_container_port` / `_enable_static` / `host_container` 的失败回滚都严格遵守 BUG-182：`fresh=False`（复用）时**不得**释放，否则破坏 lanUrl 稳定性并可致跨实例内容混淆。`_rollback_attempt` 没有拿到 fresh 信息，与这个契约直接冲突。
- **触发条件与影响**：
  - 静态实例：已成功部署（端口已登记）→ 再次 `start_instance` → `_try_host_with_fallback` → `host_static` 抛 `HostingError`（如 restart 时 index.html 已被用户删除）→ rollback 把该实例的静态端口登记**整行删除**。下次 start 分配新端口 → lanUrl 漂移；删除瞬间该端口回到池中，可被其他实例抢占（正是 BUG-182 要防的跨实例混淆场景）。
  - 容器实例：`rebuild_instance`（直接走 host_container，不经 rollback）失败后，磁盘 manifest 的 containerId 已被清空（hosting.py:505 BUG-300）→ 用户随后 `start_instance` → `_is_deployed_container=False` → `_try_host_with_fallback` → host_container 复用旧端口（fresh=False，失败时正确保留）→ 再失败 → `_rollback_attempt` 看到 containerId 为空 → **把复用的旧端口登记删掉**。此路径未被任何测试覆盖（`test_host_container_keeps_reused_port_on_build_failure` 只直接调 host_container，不经 `_try_host_with_fallback`）。
- **证据**：代码推理链（manifest 对象传递：`_try_host_with_fallback` 持有的是 start 时加载的 manifest，其 containerId 在「失败重建后」场景下确为 None）；`release_instance_ports` 无条件 `DELETE FROM ports WHERE instance_id=?`（registry/dao.py:501）；BUG-045/182 的语义在 hosting.py 多处注释与 `test_host_container_keeps_reused_port_on_build_failure`（断言复用端口 build 失败后仍在 allocated_ports）中明确。
- **建议修法**：`_rollback_attempt` 增加「端口新鲜度」输入（如把 attempt 前是否存在该实例端口登记、本轮是否调用了 allocate 记入 snapshot/attempt 上下文），仅当本轮新分配才 `release_instance`；静态实例同理。或复用 hosting 侧已有的 `(port, fresh)` 语义，由 host_fn 失败时把 fresh 标志通过异常 context 带出。

### [严重度: major] lifecycle.py:76-91,131-161 + file_lock.py:18-31（Windows 行为） 双层锁的心跳与陈旧判定在 Windows 上整体失效（测试实证）

- **说明**：Windows 上 `try_acquire_exclusive` 用 `msvcrt.locking(fd, LK_NBLCK, 1)` 锁定**字节 0**（`ensure_lockable` 保证文件 ≥1 字节）；而锁载荷（PID + 时间戳）也写在偏移 0 起。Windows 字节区域锁使**任何其他句柄（包括同进程、只读）**访问该区域都抛 PermissionError。于是：
  1. 心跳线程 `_touch_lock_heartbeat`（用 `open(lock_path, "r+")` 另开句柄）读取即失败 → `except OSError` 静默吞掉 → **心跳在 Windows 上从不刷新**（BUG-046 机制失效）；
  2. `_lock_is_stale`（lifecycle.py:180-200）`read_text` 抛 PermissionError → 返回 True → **持锁期间恒被判陈旧**（误导 daemon/诊断：daemon.py:383/1151/1178、path_alias.py:307 复用此函数）。
- **触发条件**：任何 Windows 上持锁时长 > 心跳间隔的实例操作；以及所有调用 `_lock_is_stale` 的诊断。
- **证据**：`python -m pytest tests/test_lifecycle.py` 在 Windows 上 2 个测试失败：
  - `test_instance_lock_heartbeat_refreshes_timestamp`：进入 instance_lock 后 `lock_path.read_text()` 直接 `PermissionError: [Errno 13] Permission denied`（锁文件不可读）；
  - `test_instance_lock_heartbeat_keeps_lock_fresh`：持锁且心跳运行时 `_lock_is_stale` 返回 True（断言 `assert ... is False` 失败）。
  即平台级根因，非测试偶发。
- **影响边界**：互斥本身仍有效（字节锁被持有）；失效的是心跳刷新与陈旧观测。daemon 本体为 POSIX-only（WSL2），故主影响为：Windows 上 BUG-046 心跳空转、陈旧判定失真、CI 在 Windows 上必红（父进程全量套件应同样失败）。
- **建议修法**：心跳与陈旧读取改用**持锁的同一 fd**（如 `os.pread(fd, ...)`，`instance_lock` 把 fd 传给心跳/`_lock_is_stale`）；或把载荷写到偏移 1 之后、只锁偏移 0 的 1 字节；或 Windows 改用 `LockFileEx` 锁一个独立小区域、载荷区与锁区分离。

### [严重度: minor] lifecycle.py:163-172 心跳线程 join 超时后仍可能写锁文件（释放后写入竞态）

- **说明**：`finally` 中顺序为 `heartbeat_stop.set()` → `join(timeout=5.0)` → `release_exclusive(fd)`。若心跳线程正阻塞在慢速文件系统上的 `_touch_lock_heartbeat` 写操作超过 5s，join 超时后锁已释放，线程稍后仍会把「本进程 PID + 新时间戳」写进已无人持有的锁文件 → 锁文件显示一个并不持有锁的持有者，`_lock_is_stale` 最长 1800s 内误判「有活进程持有」。
- **影响**：陈旧观测失真（同 #2 的观测面）；因陈旧判定已不用于抢锁，无并发破坏，但诊断/告警会误报。
- **建议修法**：join 不放行（去掉超时）或释放前检查线程是否仍在跑；`_touch_lock_heartbeat` 在写前检查 stop 标志。

### [严重度: minor] lifecycle.py:1040-1042,1091-1104 `_has_active_resources` 与 docstring 不符（只查容器，不查端口登记）

- **说明**：docstring 写「检查实例是否有活跃的容器或端口登记」，实现只调 `runtime.is_running()`。`rollback_ok = len(rolled_back)>0 or not _has_active_resources(...)` 在「rolled_back 为空且容器未运行」时返回 True，即使实例仍持有端口登记（如 containerId 存在导致端口未释放的场景）。
- **影响**：回滚成功判定虚高 → 可能放行 auto-equivalent 降级，而旧端口仍占池。实际影响有限（rolled_back 通常非空），属文档/实现不一致 + 判定偏乐观。
- **建议修法**：把 `registry.allocated_ports()` 归属检查并入 `_has_active_resources`，或修正 docstring。

### [严重度: minor] lifecycle.py:1000-1015 回滚容器 down 失败被 `is_running` 异常掩盖 → automaticFallbackSafe 可能虚高

- **说明**：`container_was_running` 仅在 `runtime.is_running()` 返回 True 时置位；若 `is_running` 本身抛异常（Docker 权限不足/daemon 抖动，进入 broad `except`），`container_was_running=False` → 容器实际仍在跑却不记残留 → `rollback_ok` 可能为 True 且 `not container_was_running` → `automaticFallbackSafe=True`（若不需要迁移）→ 在 top-1 容器仍存活时自动降级到 fallback，两个容器争同一端口。
- **建议修法**：`is_running` 抛错时按「可能仍在运行」保守处理（视作残留/不安全），或先试 `down` 再判断。

### [严重度: minor] lifecycle.py:583-624 fallback 循环只捕获 (HostingError, DockerError)，`_apply_plan_and_host` 内的非业务异常会炸穿整条降级链

- **说明**：循环内 `_apply_plan_and_host` → `_apply_candidate_and_host` 会 `manifest.save()`（磁盘/DB 写）并重建 model；若抛出 OSError / sqlite3.Error / pydantic ValueError（如候选 runtime 与既有 container/static 配置冲突触发 models.py:634 `_check_runtime_serving_consistency`），将绕过 `except (HostingError, DockerError)` 直接上抛 → 剩余 fallback 候选不再尝试、Layer-4 诊断报告也不写。
- **触发条件**：fallback 候选数据异常（多由部署计划 schema 漂移引起）。
- **建议修法**：循环内对每候选套 `except Exception` 记录诊断并 continue（与 top-1 分支一致）。

### [严重度: minor] lifecycle.py:1651-1666 `cancel_build` 对「首次部署 start」的构建是 no-op（未注册队列、无 build_token）

- **说明**：`run_command` 的跨进程取消依赖 `current_build_token()` 非空才查持久化 `_gates`；而 build_token 只在 `build_queue.run` 的 `enter_build_context` 中设置。`start_instance` 首次部署路径（`_try_host_with_fallback` → `host_instance` → `host_container`/`build_and_host_frontend`）**不经 build_queue.run**，build_token 为 None → 跨进程 `lwa cancel` 只能命中进程内 hub，对另一进程发起的 start 构建完全无效（返回 noop）。
- **影响**：构建期间无法取消（小主机长构建只能等 600s 超时）。rebuild 路径不受影响。
- **建议修法**：start 路径也通过 build_queue.run 调度，或在 start 期间也持久化带 token 的 build_task。

### [严重度: minor] lifecycle.py:2122-2124 `_sync_alias_port` 只匹配片段中第一个 `127.0.0.1:PORT`

- **说明**：`re.search(r"127\.0\.0\.1:(\d+)", text)` 取首个匹配与当前 hostPort 比较；若别名片段含多个 reverse_proxy（如多端口/多实例合片），首个匹配可能不是本实例端口 → 误判「未漂移」跳过重写。
- **影响**：别名入口继续指向旧端口。低概率（片段通常单代理）。
- **建议修法**：按实例 hostPort 精确匹配（`127.0.0.1:<port>` 是否存在），而非取首个匹配比较。

### [严重度: minor] hosting.py:559-579 `host_container` 通用失败路径不 down 已 up 的容器（孤儿运行容器）

- **说明**：`runtime.up()` 成功后的失败点（manifest.save / registry.upsert / update_status 等 DB/磁盘写失败，hosting.py:622-628）进入通用 `except Exception`：只释放 fresh 端口 + `_mark_failed`，**不 `runtime.down()`**。此时容器在跑但实例标记 FAILED；且这类 sqlite3/OSError 不属于 `(HostingError, DockerError)`，`_try_host_with_fallback` 也不会兜底回滚 → 孤儿容器。
- **触发条件**：up 之后注册表/磁盘瞬时故障（低概率但真实）。
- **建议修法**：通用失败路径先 best-effort `runtime.down()`（与 `_liveness_failed_rollback` 对称）。

### [严重度: minor] hosting.py:183-204,271-287 `_enable_static` 成功之后、后续写失败时的端口泄漏

- **说明**：端口仅在 `gateway.enable` 抛错时于 `_enable_static` 内释放（fresh 时）。`_enable_static` 返回后（端口已登记、网关已启用），若 `manifest.save`/`registry.upsert_from_manifest`/`update_status` 抛错 → `_mark_failed` 后端口登记残留；重试时 `_ensure_static_port` 见旧端口仍在监听 → 分配第二个端口 → 一实例两端口登记（直到 remove 才清）。
- **建议修法**：host_static / build_and_host_frontend 失败路径在「enable 成功后」也按 fresh 释放，或让 `_enable_static` 返回 fresh 供外层异常路径使用。

### [严重度: minor] hosting.py:805-809 `start_container` 在 hostPort 缺失时新分配端口，后续验证失败不释放（幽灵端口）

- **说明**：`if not host_port:` 分支新分配并写回 `manifest.container.hostPort`，但该分支之后的失败路径（观测失败 / Gate-C 必选探针失败）都不释放该端口（轻量 start 无 `_liveness_failed_rollback` 的 fresh 处理）。失败后端口登记永久挂在实例名下，端口实际空闲 → 池容量浪费 + 下次 start 复用同一端口。
- **建议修法**：记录本次是否新分配，失败时释放（与 host_container 对称）。

### [严重度: minor] hosting.py:1385-1400 `_probe_path` 的 expected_status 语义：期望 404/401 的探针也会被任意 2xx/3xx 判为通过

- **说明**：第 3 个分支 `if 200 <= code_int < 400: return (True, ...)` 使第 2 分支（`not expected_status` 时才 2xx 通过）形同虚设：只要响应是 2xx/3xx，无论 expectedStatus 是什么都通过；`expected_status` 只在「非 2xx/3xx 精确匹配」时生效（如期望 404 命中 404）。docstring 明确写了「等于 expected_status 或同为 2xx/3xx」，属**有意的宽松语义**，但会让「期望 404」的探针在 200 时假绿。标注为语义存疑（设计权衡，非确定 bug）。
- **建议修法**：若探针意图是「期望状态码精确匹配」，应去掉第 3 分支；否则在文档中明确 expected_status 仅对非 2xx/3xx 生效。

### [严重度: minor] hosting.py:1615-1629 `sync_dir` 对 dst 为普通文件的异常状态无防护

- **说明**：`if dst.exists(): shutil.rmtree(dst)` — 若 `public/` 被外部破坏成普通文件，`rmtree` 抛 `NotADirectoryError`（OSError 子类）→ 托管失败且 `_mark_failed`，无自愈。属防御性缺口。
- **建议修法**：`dst.is_dir() and rmtree(...) else dst.unlink(missing_ok=True)`。

### [严重度: minor] hosting.py:1184-1186 `_collect_side_effect_records` 冗余三元

- **说明**：`"succeeded" if migration_succeeded else ("unknown" if liveness_ok else "unknown")` 两个分支同为 "unknown"，恒等于 `"succeeded" if migration_succeeded else "unknown"`。无功能影响，代码冗余。
- **建议修法**：化简。

### [严重度: minor] lifecycle.py:1441-1444 `_apply_candidate_and_host` 无法把 sourceSubdir 清回 None

- **说明**：`if candidate_subdir:` 仅在候选带子目录时覆盖；fallback 候选无 sourceSubdir（None）时保留上一候选/旧值。回滚路径有 snapshot 兜底，但若 fallback 成功，错误保留的旧子目录可能让后续 build 在错误目录执行。
- **建议修法**：改为 `if "sourceSubdir" in candidate_dict:` 显式赋值（含 None）。

---

## 3) 经核对排除的疑似项（避免误报）

- `run_command` 的 `stdout_data = chunk` 覆盖而非累加：`Popen.communicate` 的文档与 BUG-273 注释确认「超时重试不丢输出、最终一次返回全量」，**非 bug**（`test_run_command_stdout_not_duplicated` 通过佐证）。
- `_ensure_static_port` / `_ensure_container_port` 的「检查监听→登记」TOCTOU：由 `registry.allocate_port` 的并发安全语义（INSERT OR IGNORE + 归属校验，BUG-017）覆盖，**非 bug**。
- `host_container` 验证失败路径 `_liveness_failed_rollback` 后 manifest 仍保留 containerId：后续 start 走轻量路径由 BUG-382（外部 down 自愈 up）兜底，**设计成立**。
- `stop_instance` 不释放端口（静态/容器）与 `remove_instance` 级联删 ports 行：与 BUG-045 语义一致，**非 bug**。
- `_wait_for_http` 固定 30 次 × 1s：与 `_CONTAINER_HEALTH_ATTEMPTS/DELAY` 注释一致，**非 bug**。
- `build_queue` 的 cancel（queued/building/cancelling 状态机、PID+identity+pgid 三重校验、槽位回收）：实现严谨，**未发现问题**（唯一缺口见上面 cancel_build 对 start 路径的 no-op）。

---

## 4) 汇总

- 文件级：**hosting.py 有疑点（7 条 minor）**；**lifecycle.py 有疑点（2 条 major + 8 条 minor）**；两文件间存在 1 条跨模块契约冲突（major，见 #1）。
- 测试现状：`test_hosting.py`、`test_host_container.py` 全绿；`test_lifecycle.py` 在 Windows 上 2 个锁心跳测试必失败（对应 major #2，属平台级真实 bug，非测试问题）。
- 未发现 critical 级问题；无遗漏的裸 except 吞错导致数据损坏的路径（多数 broad except 有明确注释且 best-effort 合理）。
