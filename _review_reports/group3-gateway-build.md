# 代码审阅报告：static_gateway / docker_runtime / compose / build_process / build_queue

审阅环境：Windows（Python 3.13.13，UTF-8 mode 开启），源码在 `src/local_webpage_access/`，测试单文件运行（未跑全量）。

## 0) 用户核心疑点的直接结论（mypy 报错行的运行时安全性）

mypy 报错的 6 处 POSIX/Windows 专属调用**全部位于平台守卫分支内，Windows 运行不会 AttributeError**：

| 位置 | 调用 | 守卫 |
|---|---|---|
| static_gateway.py:1431 | `subprocess.CREATE_NEW_PROCESS_GROUP` | `if os.name == "nt":` 内（Windows 才有该属性，POSIX 不执行） |
| static_gateway.py:1534/1535 | `os.getpgid` / `os.killpg` | `if os.name == "nt":` 的 `else` 分支（POSIX 才执行） |
| static_gateway.py:1655 | `ctypes.windll.kernel32` | `if os.name == "nt":` 内 |
| static_gateway.py:1669 | `os.waitpid` / `os.WNOHANG` | `os.name == "nt"` 之后的 POSIX 分支 |
| build_queue.py:154 | `ctypes.windll.kernel32` | `if os.name == "nt":` 内 |

- 1431 行的 `# type: ignore[attr-defined]` 是**用上的**（Windows stub 下该属性缺失）；1655 行同理。1534/1535/1669 与 build_queue:154 的报错是"Windows 平台 typeshed 缺属性"（mypy 按运行平台推断），不是"缺守卫"。
- build_process.py:128 还有一处同类调用（`subprocess.CREATE_NEW_PROCESS_GROUP`，无 type: ignore）——同样有 `sys.platform == "win32"` 守卫，运行时安全，但建议补 ignore 让 mypy 干净。
- 补充：static_gateway 的 `_pid_alive`/`_kill_process` 在 POSIX 分支对僵尸子进程做了 `waitpid` 回收（BUG-045 处理正确）。

## 1) 各文件一行总结

- **static_gateway.py**（职责：Caddy/builtin 双后端静态托管、站点配置渲染、builtin 子进程生命周期与健康检查、Caddy master 启停/reload/回滚、跨进程 gateway-config.lock 串行化）——有疑点：Linux 上 pgrep 孤儿枚举大概率失效；Windows 上非 ASCII 命令行经 `daemon.read_pid_cmdline` 会抛未捕获异常（实测复现）。
- **docker_runtime.py**（职责：docker compose 命令封装、流式日志+超时/取消、容器/镜像/挂载观测、数据救援）——基本健康，仅若干 minor（构建超时不收尾 builds 行等）。
- **compose.py**（职责：compose.yaml/.env 模板渲染、SQLite 数据迁移与密钥注入）——未发现明显逻辑错误，仅 1 个 minor（projectName 原样内插 YAML）。
- **build_process.py**（职责：构建子进程登记表、进程组 TERM→KILL 整树终止、跨进程 PID 身份校验）——有 2 个 minor（PermissionError 分支无兜底、僵尸误判）。
- **build_queue.py**（职责：SQLite 跨进程构建闸门 build-locks.db、队列状态机 queued/building/cancelling、跨进程取消协调）——状态机与 token 代际设计健壮，无逻辑性错误；有若干 minor（长事务持锁、锁冲突不重试、`is False` 判定）与 1 个 Windows 性能问题（owner_identity 每次 persist 都 spawn PowerShell）。

## 2) 发现清单

### static_gateway.py

**[major] static_gateway.py:1341-1365 `_enumerate_workspace_builtin_pids`：Linux 上 `pgrep -lf` 只输出 comm（进程名），孤儿枚举恒为空（疑似，需 Linux 实测确认）**
- 说明：`pgrep -l` 在 Linux（procps-ng）输出"pid + 进程名(comm，截断 15 字符)"，不含完整命令行；而 1354 行 `if apps_prefix not in line: continue` 依赖命令行包含工作区 `apps/` 绝对路径——Linux 上该行恒真，函数恒返回空列表。代码注释自述 Darwin 上 `-af` 只输出 PID 所以选 `-l`，但该选择牺牲了 Linux（macOS 的 `-l` 恰好输出完整命令行，与 Linux 语义不同）。
- 影响：`stop_all_builtin` 的"途径 2"（pid 文件丢失的 http.server 孤儿清理）在 Linux 上永不生效；崩溃/异常切换残留的无 pid 文件进程继续占端口。途径 1（扫描 `run/static-*.pid`）仍有效，故影响范围有限。
- 证据：procps `-l` 语义（文档知识，本机无 Linux 无法实测）+ 代码注释本身承认平台差异；`_iid_from_cmdline` 也依赖命令行，同样在 Linux 失配。
- 建议修法：按平台选择参数（Linux 用 `pgrep -af`，macOS 用 `pgrep -lf`），或在 Linux 改用 `/proc` 扫描。

**[major] static_gateway.py:1505-1512（调用链）→ daemon.py:284-322 `read_pid_cmdline`：Windows + 目标命令行含非 ASCII 时抛未捕获 AttributeError（已实测复现）**
- 说明：`_kill_process` 的 PID 身份校验走 `pid_cmdline_contains` → `read_pid_cmdline`。其 Windows 分支 `subprocess.run([powershell...], text=True)` 未指定编码（Python UTF-8 mode 下按 utf-8 解码），而 PowerShell 对含非 ASCII 的进程命令行按控制台代码页（如 GBK）输出 → 子进程 reader 线程 UnicodeDecodeError → `result.stdout` 为 None → 第 309 行 `result.stdout.strip()` 抛 `AttributeError: 'NoneType' object has no attribute 'strip'`；except 只捕获 `(FileNotFoundError, subprocess.SubprocessError, OSError)`，AttributeError 漏网向上传播。
- 影响：工作区路径含中文（本项目场景常见）时，Windows 上 `_stop_builtin`/`enable`/`disable` 会在 kill 前崩溃：builtin 进程不被终止、pid 文件不清，站点状态残留；API 调用方可能收到未包装异常。`build_process._proc_identity` 路径因外层 `except Exception` 兜底返回 ""，不受影响。
- 证据：本机实测——对 `subprocess.Popen([sys.executable, '-c', '...', 'zh-test-中'])` 调 `read_pid_cmdline(pid)` / `pid_cmdline_contains(pid, 'zh-test')`，两者均抛 AttributeError（reader 线程先报 UnicodeDecodeError）。
- 建议修法：`read_pid_cmdline` 增加 `errors="replace"`（或捕获 `(UnicodeDecodeError, AttributeError)` 返回 None），并给 `.strip()` 判空保护；`pid_cmdline_contains` 已对所有 needle 判空，返回 False 即安全拒绝终止。

**[minor] static_gateway.py:1533-1538 `_kill_process` POSIX：killpg 目标进程组的身份校验仅覆盖"无 Popen 句柄"路径**
- 说明：带 `proc=` 句柄时跳过 cmdline 身份校验直接 `os.getpgid(pid)` + `os.killpg(pgid, SIGTERM)`。本网关启动的 builtin 均 `start_new_session=True`（独立进程组），正常无风险；但若 pid 文件被人工写入指向同进程组内无关进程，会连网关自身所在组一起 TERM。
- 影响：低（需人工/外部干预才触发），属防御性缺口。
- 建议：killpg 前校验 `pgid != os.getpgrp()` 或始终做一次命令行身份校验。

**[minor] static_gateway.py:393-408 `generate_site_config`：用户模板含额外 `{}` 时 `str.format` 抛 ValueError/KeyError（非 GatewayError）**
- 说明：`template.format(...)` 对模板中的多余/缺失占位符直接抛异常，`enable` 的恢复路径虽能兜住，但错误类型与网关错误体系不一致，且异常信息不含实例上下文。
- 影响：仅在用户替换了 `templates/static/caddy_site.conf.tpl` 且含特殊字符时触发，低概率。
- 建议：format 外包 `try/except (KeyError, ValueError)` 转 GatewayError。

**[测试平台问题] tests/test_static_gateway.py::test_stop_builtin_refuses_foreign_reused_pid 在 Windows 失败（非源码 bug）**
- 说明：测试无条件 `monkeypatch.setattr("local_webpage_access.static_gateway.os.killpg", ...)`，Windows 的 `os` 模块没有 `killpg` 属性 → 打桩阶段即 AttributeError。
- 证据：`85 passed, 1 failed`，失败仅此一例，报错在 monkeypatch.py:94。
- 建议：该测试加 `@pytest.mark.skipif(sys.platform == "win32")` 或改用 patch 目标（如 `_kill_process` 的 POSIX 分支函数）。

### docker_runtime.py

**[minor] docker_runtime.py:536-554 `build()`：构建超时（DockerError）时不收尾 builds 行、不记事件**
- 说明：`except Exception` 分支只对 `BuildCancelled` 做 `finish_build(status="cancelled")` + 事件；`_execute_streaming` 超时抛的 DockerError 直接 re-raise，registry 的 builds 行停留在 running/building。
- 影响：30 分钟构建超时后，管理页该构建永久显示"进行中"。
- 证据：代码阅读（异常分支仅处理 BuildCancelled；非零退出的失败路径有收尾，超时路径没有）。
- 建议：DockerError 时也 `finish_build(status="failed"/"timeout")` 并记 error 事件。

**[minor] docker_runtime.py:280-284 `_execute_streaming`：Popen 只捕 FileNotFoundError，其余 OSError 原样上抛**
- 说明：`subprocess.Popen` 还可能抛 PermissionError 等 OSError（如 docker 二进制不可执行），会以裸 OSError 泄漏，违反"执行失败统一 DockerError"约定（`_execute_captured` 同样只捕 FileNotFoundError/TimeoutExpired，但 `subprocess.run` 的 OSError 也未捕——两类执行器都不一致）。
- 影响：调用方按 DockerError 兜底时会漏接。
- 建议：捕 `OSError` 一并转 DockerError。

**[minor] docker_runtime.py:295-308 reader 线程：进程不退出且 kill 失败时 readline 阻塞，靠 daemon 线程 + join(timeout=5) 兜底**
- 说明：`stdout.close()` 在 finally 中已尽力关闭；daemon 线程不会卡进程退出。设计上可接受（注释已说明），但多次取消场景 fd 释放依赖 close 成功。
- 影响：低；属防御性代码，不作为必须修复项。

**[minor] docker_runtime.py:1022-1042 `_extract_ports`：PublishedPort 为 0（随机端口）时端口不展示**
- 说明：`elif pub and tgt:` 对 `pub=0` 判 False，跳过。
- 影响：仅展示层，极小。

### compose.py

**[minor] compose.py:46-66 + 138-149：`project_name`/`instance_id`/`limits.memory` 等值原样内插 YAML**
- 说明：`name: {project_name}`、`container_name: lwa-{instance_id}`、`mem_limit: ...{memory}` 均未做 YAML 引号/字符校验。projectName 来自源项目元数据（用户可控），若含 `: `、`#`、`[` 等 YAML 特殊字符，生成的 compose.yaml 会被解析为不同结构或直接非法导致 `docker compose` 报错。
- 影响：仅影响该实例自身的 compose 文件（本机 docker 消费），无权限提升面；可能导致构建/启动失败。
- 证据：代码阅读（模板字符串直插 + 设计说明第 18-20 行明确"用字符串模板而非 safe_dump"——权衡可理解，但值侧无校验）。
- 建议：渲染前校验 projectName 匹配 `[a-zA-Z0-9_.-]+`（Compose project 名限制），或在值侧加引号包裹；`memory`/`cpus` 已由 ResourceLimits 默认工厂兜底非空。

### build_process.py

**[minor] build_process.py:173-187 `kill_process_tree` POSIX：`os.killpg` 抛 PermissionError 时 `break` 直接跳出，SIGKILL 与 `proc.kill()` 兜底都不执行**
- 说明：`except PermissionError: break` 后 for 循环结束、函数返回，进程可能残留；其他异常才走 `proc.kill()` 兜底。
- 影响：跨用户权限边界（罕见）时取消不彻底。
- 建议：PermissionError 分支改为 `break` 后仍执行一次 `proc.kill()` 兜底，或 fallthrough 到外层兜底。

**[minor] build_process.py:214-271 `kill_pid_tree_if_matches`：僵尸进程误判存活 + `os.getpgid` 的 PermissionError 未捕获**
- 说明：(a) SIGTERM 后轮询 `_pid_alive` 用 `os.kill(pid, 0)`，对"已退出但未回收"的僵尸子进程恒 True（本函数无 Popen 句柄、也不做 waitpid 回收）→ 超时后可能误报未杀死并补 SIGKILL；(b) 216 行 `os.getpgid(pid)` 只捕 ProcessLookupError，PermissionError 会向调用方（build_queue._reclaim_dead）传播。
- 影响：跨进程取消在极端时序下多打一次 SIGKILL 或向上抛一个未包装 OSError；僵尸窗口很短（父进程是 daemon 会正常回收）。
- 证据：代码阅读 + build_process._pid_alive 与 static_gateway._pid_alive 的差异（后者有 waitpid 回收，前者没有）。
- 建议：轮询循环内先尝试 waitpid 回收（非子进程则忽略 ChildProcessError）；getpgid 捕 PermissionError。

### build_queue.py

**[minor] build_queue.py:262-298 `_reclaim_dead`：在 BEGIN IMMEDIATE 写事务内执行进程终止（最长 ~10s/个 × 多个死 owner）**
- 说明：`_try_acquire` 的 BEGIN IMMEDIATE 事务内调用 `kill_pid_tree_if_matches`（TERM 5s + KILL 5s）。期间 build-locks.db 写锁被长期持有，其他进程的 `BEGIN IMMEDIATE` 等 busy_timeout 30s 后抛 OperationalError。
- 影响：多个 owner 崩溃残留且 worker 杀得慢时，其他进程获取槽位可能失败并把任务误标 failed。
- 证据：代码阅读 + sqlite busy_timeout=30000 设置（205 行）。
- 建议：把 kill 操作移出事务（先收集死槽位/任务，提交后再 kill）。

**[minor] build_queue.py:234-260 / 300-324：sqlite3.OperationalError（database is locked）不重试，直接上抛**
- 说明：`_try_acquire` 的 except 分支 ROLLBACK 后 re-raise；`acquire()` 轮询只处理"槽位满"，不处理锁冲突异常 → run() 把任务标 failed。
- 影响：正常短事务下几乎不会触发（busy_timeout 30s 足够），仅与上一条叠加时出现。
- 建议：`_try_acquire` 对 OperationalError 返回 None（视为暂不可得）让 acquire 轮询重试。

**[minor] build_queue.py:636 `run()`：`if self._persist_task(task, status="building") is False:` 对返回 None（持久化异常）不拦截**
- 说明：`_persist_task` 异常时返回 None（997-999 行 catch-all 记日志），`is False` 判定为 False → 继续执行构建，但 DB 行可能缺失/陈旧，跨进程取消协调被削弱（is_cancel_requested 读不到本代际行）。
- 影响：持久化故障时取消可靠性下降；正常路径不受影响。
- 建议：改为 `is not True`。

**[minor] build_queue.py:1122-1139 `_shared_gate`：同 db_path 不同 concurrency 的旧 gate 被 close，可能关掉仍在使用它的 BuildQueue**
- 说明：新建不同 concurrency 的 BuildQueue 时，`stale_keys` 会 close 旧 gate（其连接）；若旧 BuildQueue 仍在并发构建，下一次 `_conn_or_open` 抛 RuntimeError("已关闭")（193-194 行）。生产主路径走 `get_build_queue` 单例化，通常先关旧队列再建新队列，冲突窗口小；但直接构造多个不同 concurrency 的 BuildQueue（如测试/插件）会踩中。
- 证据：代码阅读。
- 建议：close 前检查 gate 是否有活跃槽位，或在 BuildQueue 侧对 RuntimeError 重建 gate。

**[minor] build_queue.py:974-999 + daemon.read_pid_cmdline：Windows 上取消排队任务多 ~1s 延迟（owner_process_identity 每次 persist 都 spawn PowerShell）**
- 说明：`_persist_task` 每次都调 `owner_process_identity()` → Windows 上 `read_pid_cmdline` spawn PowerShell（Get-CimInstance），单次约 0.5-1s。取消排队任务后 owner 线程要再 persist 1-2 次（_finish_task_local + except 收尾），导致"取消→任务终结"延迟 ~1s。
- 证据：本机实测——patch 掉 owner_process_identity 后取消响应 0~17ms，不 patch 时 ~0.9-1.0s；状态机最终状态正确（cancelled + LifecycleError），仅延迟大。
- 影响：Windows 上取消排队构建的用户感知延迟；并导致 `test_cross_process_cancel_interrupts_gate_wait_immediately`（预算 0.5s）在 Windows 上确定性失败（见下）。
- 建议：owner_identity 进程内缓存（线程级缓存一次即可，cmdline 不变）；或 Windows 分支换用快路径。

### 测试结果与环境归属（非源码 bug，但需知悉）

- tests/test_static_gateway.py：`85 passed, 1 failed`（1 个失败 = 测试对 os.killpg 无条件打桩，Windows 平台问题，见上）。
- tests/test_build_queue.py：`28 passed, 3 skipped, 4 failed`。4 个失败中 3 个（含 `test_cross_process_gate_serializes_two_processes`、`test_cross_process_gate_reclaims_dead_holder_slot`）为 Windows 测试环境子进程 `ModuleNotFoundError: No module named 'local_webpage_access'`（子进程未继承 PYTHONPATH，非源码问题）；1 个（`test_cross_process_cancel_interrupts_gate_wait_immediately`）为上述 owner_identity PowerShell 慢导致取消响应超 0.5s 预算（源码状态机正确，Windows 性能 + 测试预算过紧）。
- tests/test_docker_runtime.py + test_compose.py + test_build_process.py：`90 passed, 2 skipped`（跳过项为 POSIX 时序用例），无失败。

## 3) 无问题文件

- **compose.py**：未发现明显逻辑错误（仅 1 个 minor 见上）。模板渲染、SQLite 迁移、密钥注入逻辑均自洽；`str.format` 值侧含 `{}` 不会二次解析，安全。
- 其余文件结论：docker_runtime.py、build_process.py、build_queue.py 未发现 critical/major 级确定性逻辑错误；build_queue 的跨进程闸门与状态机（含 token 代际防覆盖、queued→cancelled/building、building→cancelling→cancelled/cancel_failed 迁移表）设计健壮。

## 4) 建议修复优先级

1. （major）daemon.read_pid_cmdline Windows 非 ASCII 编码崩溃（影响 static_gateway 停止 builtin 的可靠性）。
2. （major 疑似，需 Linux 实测）static_gateway `pgrep -lf` 在 Linux 的孤儿枚举失效。
3. （minor）docker_runtime build 超时不收尾 builds 行；build_queue _reclaim_dead 长事务；owner_identity Windows 慢（取消延迟）。
4. 其余 minor 按需处理；两个测试文件的 Windows 平台适配（skipif/子进程 PYTHONPATH）。
