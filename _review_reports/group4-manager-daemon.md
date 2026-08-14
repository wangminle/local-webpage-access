# LWA 代码审查报告：manager_api / manager_service / daemon / gateway_service / gateway_switch

审查方式：5 个目标文件全部逐行读完（read 工具，无跳读）；交叉阅读 logs.py / lifecycle.py / static_gateway.py / paths.py / importer.py / path_alias.py / pageviews.py / access_workflow.py / errors.py / cli/gateway.py 中与疑点相关的函数；运行了 `tests/test_gateway_switch.py`（其余 4 个测试文件因会话两次中断未能取得结果，见文末"测试证据"）。
未修改任何源码/测试文件，仅本报告一个文件。

## 1) 每个文件一行总结

- **manager_api.py**（1917 行）：管理页 FastAPI 后端（token 鉴权与轮换、health/capability、实例 CRUD/生命周期/日志/资源/冗余/路径别名 API）。鉴权主体设计严谨（常数时间 token 比较、loopback 免 token + CSRF 门禁、instance_id/category/zipPath 穿越防护齐全、LwaError 显式类映射），**有疑点**：body 布尔强转、fallback_policy 未校验、批量删除无确认门禁、DNS rebinding 加固缺口。
- **manager_service.py**（703 行）：管理页后台服务启停/状态（子进程 spawn、manager.json、启动锁与单实例锁、健康探活归属校验）。**有疑点（均 minor）**：陈旧锁回收竞态、PID 复用误归属兜底、stop 身份不匹配误报。
- **daemon.py**（1487 行）：inbox watcher + 周期 reconcile 自愈（心跳、失败死信、指数退避、processed LRU）。整体非常健壮，**有疑点（均 minor）**：is_file_stable 未来 mtime 卡死、stop_daemon 身份不匹配删锁、reconcile 与用户 stop 的 TOCTOU。
- **gateway_service.py**（608 行）：Caddy 网关服务层（admin :2019 在线判定、owner/workspace 归属校验、前台监管循环）。**有疑点**：start_gateway 停旧启新失败不回滚（major，见 #14）、status 对外来 master 误报、gateway_start_lock 死代码、write_state 缺 mkdir。
- **gateway_switch.py**（593 行）：网关后端原子切换事务（预检→快照→停旧→写 YAML→启新→同步 manifest→access 复核→回滚/degraded）。状态机主体完整（含 BUG-353 别名 reload 失败上抛、BUG-326 回滚复核失败标 degraded），**有疑点**：事务级无跨进程锁（major）、回滚不还原 manifest/registry（major）、损坏 manifest 静默跳过（major）、孤儿片段残留。

未发现 critical 级鉴权绕过；token 校验路径（_verify_token/read_token/rotate）与路径穿越防护（validate_instance_id / validate_log_category / zipPath relative_to 检查）均经代码阅读确认安全。

## 2) 发现清单

### major

**[major] gateway_switch.py:445-593 切换事务无跨进程互斥锁（并发交错）**
- 说明：`switch_gateway` 全流程（stop_gateway → 写 YAML → start_gateway → rebuild/enable → sync_manifests → access_pass）没有任何事务级锁；仅 `start_gateway`/`stop_gateway` 内部各自持有 `gateway_start_lock`，粒度不足以串行化整个切换。两个并发切换（管理页 `POST /api/gateway/switch` + CLI `lwa gateway switch`，或双终端）会交错执行：A 写 YAML=builtin 后 B 写 YAML=caddy，A 再 enable builtin 而 B 已把 Caddy master 拉起 → 结束态 YAML、manifest、进程三方不一致（如 YAML=builtin + master 在线 + manifest=caddy）。
- 证据：代码阅读——switch_gateway 函数体内无任何锁原语；manager_api.py:837-870 与 cli/gateway.py:225-349 都直接调用它，无串行化。
- 建议：整个事务加一把工作区级文件锁（如 `run/gateway-switch.lock`，flock 方式），或复用 gateway_start_lock 覆盖全事务。

**[major] gateway_switch.py:256-274,345-360 回滚不还原 manifest 与 registry 行（契约不一致）**
- 说明：`_sync_manifests_gateway` 逐实例改写 `manifest.static.gateway` 文件并 upsert registry `static_sites`；而 `_restore_snapshot_files` 只还原 YAML / gateway.json / sites / aliases 片段。若切换在 manifest 同步中途失败（IO 异常、进程被杀），回滚后部分实例的 manifest/registry 仍写新后端值，而 config.staticGateway 已回旧值 → 状态机后续（is_enabled 判定、下次 plan_switch 预检、status 展示、reconcile）读到互相矛盾的 gateway 字段。
- 证据：`_take_snapshot`（223-239）只读 yml/gateway.json/sites/aliases；`_restore_snapshot_files`（345-360）只写这四类；manifest 文件与 registry 行不在快照范围。
- 建议：快照阶段同时备份被 `_sync_manifests_gateway` 触及的 manifest 文本与 registry 行（或改为"全部同步成功后再落 YAML 切换点"，缩小不一致窗口），回滚时一并恢复。

**[major] gateway_switch.py:125-149 损坏 manifest 的实例被静默跳过（fail-open，站点下线却不报错）**
- 说明：`_iter_static_instances` 对 `InstanceManifest.load` 失败的实例仅记 warning 并 continue；切到 builtin 时 `_enable_running_builtin` 同样跳过该实例。结果：Caddy master 被停止后，该实例无 builtin 进程承接 → 站点下线，而 `switch_gateway` 仍返回 `ok=True`（前端/CLI 显示成功）。
- 证据：代码阅读——125-149 行 load 异常 → log.warning + continue；502-524 行没有对"被跳过的 running 静态实例"做任何计数或失败传播。
- 建议：预检阶段若存在无法加载 manifest 的 running 静态实例，直接 fail（或至少把 `ok` 保持 True 但 `fully_ok=False` 并带 warning 列表），绝不静默丢站点。

**[major] gateway_service.py:254-271 start_gateway"停旧启新"失败不回滚（站点下线）**
- 说明：`start_gateway` 先 `stop_all_builtin()` 停掉全部 builtin 静态进程，再 `caddy_start()`；若 Caddy 启动失败（二进制缺失/损坏、admin :2019 被外来占用）→ raise LifecycleError，被停的 builtin **不恢复**。触发场景：config.staticGateway=caddy 但 Caddy 不可用，用户 `lwa gateway on` / `lwa manager on` 联动 maybe_start_gateway 时，正在服务的 builtin 孤儿进程被停掉后 Caddy 起不来 → 站点持续不可用；daemon reconcile 在 caddy 配置下重启实例走 Caddy 分支（master 不在则 reload 失败），无法自愈。
- 证据：代码阅读——255 行 stop_all_builtin、267 行 caddy_start、268-271 行直接 raise，无回滚路径；对比 gateway_switch._rollback（382-391）在切回 builtin 时专门用 `_enable_running_builtin` 补拉进程，说明该风险是已知的但 start_gateway 直调路径未覆盖（影响面依赖触发频率，故标注 uncertain）。
- 建议：caddy_start 失败时按 `stop_all_builtin` 的返回列表逐个 `_start_builtin` 恢复（复用 enable 的 BUG-216 恢复逻辑），或把"先停旧"推迟到 caddy 就绪确认之后。

### minor

**[minor] manager_api.py:848-849,1273-1275,1352-1354 body 布尔字段用 `bool()` 强转，字符串 "false"/"0" 被当作 True**
- 说明：`bool(payload.get("dryRun", False))`、`bool(payload.get("restart", True))` 等对 JSON 字符串 `"false"` 求值为 True。前端正常发 JSON 布尔没问题，但畸形输入会静默反转语义：`"restart": "false"` 实际重启实例、`"dryRun": "false"` 实际执行 dry-run。
- 证据：Python `bool("false") is True`；该文件 body 均直接 `payload.get` 后 `bool()`，无类型校验（对比 remove_op 的 purge/force 走 Query 由 FastAPI/pydantic 正确解析）。
- 建议：统一用严格解析（如 `isinstance(v, bool) and v`，或为 body 定义 pydantic 模型）。

**[minor] manager_api.py:1006-1009 fallback_policy 无枚举校验，任意字符串静默启用自动降级**
- 说明：`fallback_policy` 是自由 Query 字符串；lifecycle 对非 "confirm"/"disabled" 的值一律落入 auto-equivalent 分支（lifecycle.py:436,523 之后）。`?fallback_policy=typo` 会让"需要用户确认的降级"变成"静默自动降级"。
- 证据：lifecycle.py:435-443（disabled 分支）与 523-531（confirm 分支）之后的代码即自动降级路径，无 else 报错。
- 建议：API 层用 Literal 枚举校验三个合法值，非法值返回 400。

**[minor] manager_api.py:1651-1691 /api/redundant/remove 无 confirmId 确认门禁（契约不一致）**
- 说明：单实例 `POST /api/instances/{id}/remove`（1173 行）对 `purge/force` 要求 body 携带与实例 ID 相同的 confirmId；批量 `POST /api/redundant/remove?purge=true&force=true` 无任何确认门禁，可直接删除一组冗余实例的磁盘数据（含 data/）。有审计日志但无确认。
- 证据：1651-1691 行直接调用 `remove_redundant(purge=purge, force=force)`，无 confirmId 参数；lifecycle.remove_redundant（2062-2086）也不检查。
- 建议：与单实例对齐，批量 purge/force 也要求 confirmId（如 JSON `"confirmCount": N`）。

**[minor] manager_api.py:929-964 _docker_ops_blocked_reason 注释与实现不符 + 每次操作同步跑全量能力探测**
- 说明：注释（942-944 行）声称"再看 registry runtime_access：若曾观测到权限失败仍阻断"，但实现从不读 registry 的 runtime_access/observation_error 字段，而是每次生命周期操作同步执行一次 `collect_capability_report`（内部探测 Docker/Caddy，可达数秒），把性能开销摊到每次请求上；探测失败时 fail-closed 属正确方向。
- 证据：函数体只有 collect_capability_report 一个数据源。
- 建议：按注释实现 registry 缓存短路（有 permission_denied 观测记录直接拒绝），或删除误导注释。

**[minor/uncertain] manager_api.py:403-445 loopback 免 token 的 DNS rebinding 加固缺口（无 Host 校验）**
- 说明：本机免 token（GET/HEAD/OPTIONS 全免）与非 GET 的 CSRF 门禁（sec-fetch-site=same-origin 或 Origin 匹配）是设计行为；但当攻击者域名经 DNS rebinding 解析到 127.0.0.1、且 manager 恰好监听在浏览器视为同源的同端口（如用户把 managerPort 配成 80/8080）时，浏览器会判定 same-origin 直接放行，非 GET 写请求无 token 也可达，GET 可读。现代浏览器下该场景需要自定义 DNS + 常用端口，属加固缺口而非常见漏洞，故标 uncertain。
- 证据：425-432 行 `sec-fetch-site == "same-origin"` 在 Origin 校验之前短路放行；全文件无 Host 头白名单校验。
- 建议：加 Host 头校验（仅接受绑定地址/localhost），这是对抗 rebinding 的标准手段。

**[minor] manager_service.py:336-381 manager_instance_lock 陈旧锁回收竞态：第二次 os.open 未捕获 FileExistsError**
- 说明：两个进程同时发现陈旧锁：A unlink 后成功二次 open，B unlink（被 suppress）后二次 `os.open(O_EXCL)` 抛 FileExistsError——该异常不在任何 try/except 内，直接冒泡到 run_service_main（只捕 LifecycleError）→ 子进程入口带 traceback 崩溃退出，而不是干净的"已有实例在运行，退出"。
- 证据：366 行二次 open 无保护；355-370 行的 FileExistsError 处理块只覆盖第一次 open。
- 建议：把二次 open 也包进 try，FileExistsError 转 LifecycleError。

**[minor/uncertain] manager_service.py:147-179 health_matches_workspace 的 BUG-065 兜底存在 PID 复用误归属**
- 说明：旧版 health 响应无 workspaceRoot 时，只要 state.enabled 且 state.pid 存活即认定端口归属本工作区（171-178 行）。若 state.pid 已被系统复用（原管理页死后的新进程恰好存活），且端口上实际是其他工作区/无关服务的健康响应，start_manager 会走"恢复状态"分支（413-428）而不报端口占用。
- 证据：代码阅读；现代版本 health 恒带 workspaceRoot（manager_api.py:708-709），该分支仅兼容旧版，故概率低、标 uncertain。
- 建议：归属证明只信任 workspaceRoot；删除 pid 存活兜底或要求 pid cmdline 同时匹配。

**[minor/uncertain] manager_service.py:468-511 stop_manager 身份不匹配时误报已停止**
- 说明：state.pid 存活但 cmdline 不匹配（PID 复用）时，481-490 行记日志"按陈旧状态清理"且 `stopped` 恒为 True，随即写 enabled=False 并清能力缓存；若真实 manager 其实以新 PID 仍在运行（PID 复用 + 监督器已重启），`lwa manager off` 会假报已停止而进程仍在端口上。state.pid 通常由子进程自写（run_service_main 642-652），不一致窗口很窄，标 uncertain。
- 证据：代码阅读；manager 无端口归属复核（对比 daemon stop 有 BUG-192 的锁文件保留逻辑）。
- 建议：身份不匹配时用 `find_listening_pid` + health 归属复核后再决定是否清状态。

**[minor] daemon.py:543-569 is_file_stable 对未来 mtime 的文件永久判定"未稳定"**
- 说明：稳定性要求 `now_ts - mtime >= stable_seconds`；若 zip 的 mtime 比墙钟新（时钟偏移、`cp -p`/rsync 保留未来 mtime），差值恒为负 → 永不满足 → zip 永久滞留 inbox 不处理。另 566-568 行 `(now_ts - previous.mtime) >= ... or (now_ts - current.mtime) >= ...` 两分支恒等（两 mtime 已判相等），冗余。
- 证据：代码阅读；watcher 主循环 1042-1049 对不稳定文件只是 continue。
- 建议：对差值取 `abs()` 或钳制下限（未来 mtime 视为已稳定）；简化恒等分支。

**[minor/uncertain] daemon.py:1254-1294 stop_daemon 身份不匹配时无条件删运行锁（潜在双 watcher）**
- 说明：1270-1276 行 state.pid 存活但 cmdline 不匹配 → `stopped = True`（不终止进程），随后 1282-1284 行 unlink 锁文件。若"真实 watcher 仍存活但 PID 与 state.pid 不一致"（如监督器重启后 state 未及时回写、PID 复用恰好指向另一 watcher），删锁后下一次 `daemon on` 用**新 inode** 起第二个 watcher（flock 按 inode 区分，老 watcher 的 flock 不受影响）→ 双 watcher 并发扫描/导入/自愈。因 state.pid 由 watcher 自写（_main 1402-1410），正常路径一致，窗口极窄，标 uncertain。
- 证据：daemon_lock 释放不 unlink（BUG-213，保持同 inode）正是为防止此问题，stop_daemon 的 unlink 与之矛盾。
- 建议：身份不匹配时只清状态、不删锁；或删锁前先确认锁持有者确已死亡。

**[minor/uncertain] daemon.py:842-925 reconcile 与用户 stop 的 TOCTOU（可能拉回刚停止的实例）**
- 说明：reconcile 读 `desired_state == "running"`（843 行）后，经 observe、backoff 等步骤才在 904 行调用 `do_restart`（内部才拿 instance_lock）；期间另一进程 `lwa stop` 把 desired_state 置 stopped 时，reconcile 仍按旧快照拉起实例，违背用户意图（下次 reconcile 看到 stopped 才停）。反向（stop 读旧 desired 而 reconcile 已 start）同样存在。窗口为 reconcile 单轮内，标 uncertain。
- 证据：代码阅读——843-904 之间无锁、无 desired_state 复核。
- 建议：restart 前在 instance_lock 内重读 desired_state 复核，已非 running 则跳过。

**[minor] gateway_service.py:153-179 gateway_start_lock 的陈旧回收是死代码（注释与实现不符）**
- 说明：GATEWAY_START_LOCK_STALE_SECONDS（53 行）定义后从未使用；BUG-175 注释声称"持锁进程被 SIGKILL 后锁文件残留可回收"，但实现只有 `try_acquire_exclusive` 重试至超时报错。功能上无碍（flock 在进程死亡时内核自动释放，锁文件残留不影响下一次获取），属文档/常量误导。
- 证据：grep 该常量仅 53 行一处出现；153-179 行无 mtime/pid 陈旧判定。
- 建议：删除未用常量并修正注释，或按注释补陈旧回收。

**[minor] gateway_service.py:106-112 write_state 不创建父目录**
- 说明：对比 manager_service.write_state（104 行有 mkdir），gateway_service.write_state 直接 `path.write_text`；若 `run/` 缺失（新工作区未 ensure_workspace_dirs、run/ 被删），start_gateway 在线路径（219-231）与成功路径（286 行）写状态时抛 FileNotFoundError（start_gateway 本身不保证 run/ 存在）。
- 证据：代码阅读；run_gateway_foreground 在 491 行才 ensure_workspace_dirs，而 CLI 直调 start_gateway 不经过它。
- 建议：write_state 内补 `path.parent.mkdir(parents=True, exist_ok=True)`。

**[minor] gateway_service.py:409-438 gateway_status 对外来 Caddy master 误报 running**
- 说明：`running = admin_alive`（421 行），即 :2019 上任意 master 在线都报 running；`orphanMaster` 只在 `backend != "caddy"` 时置位。若 backend==caddy 但 :2019 被**其他工作区/外部** master 占用，status 显示 running=True 且无任何占用提示（is_gateway_running 才做 owner+workspace 校验，但 status 不用它）→ CLI/管理页状态误导，掩盖"端口被抢"。
- 证据：代码阅读——418-422 行无 owner 判定；对比 is_gateway_running 135-150 行有完整校验。
- 建议：status 复用 owner 判定，外来占用时标 `foreignMaster`/降低 running 语义。

**[minor] gateway_switch.py:314-342,345-360 回滚残留孤儿别名片段**
- 说明：`_rebuild_caddy_aliases` 在 builtin→caddy 切换中新建/覆盖 aliases 片段；回滚到 builtin 时 `_restore_snapshot_files` 只重写快照中已存在的文件，**不删除**本次新建的片段（builtin 模式下快照为空）→ 孤儿 alias conf 残留；下次切回 caddy 时可能加载陈旧别名路由。
- 证据：快照（223-239）与恢复（345-360）均按"存在的文件"遍历，无反向删除。
- 建议：回滚时删除"快照中不存在但本次事务创建"的 conf 文件（基于事务开始前的目录清单）。

**[minor] gateway_switch.py:461-469 plan 异常分支对非字符串 target 掩盖原始错误**
- 说明：`plan_switch` 抛 LwaError 的 except 分支执行 `target.strip().lower()`；若调用方直接传 None/非字符串（库级调用），AttributeError 会掩盖原始 LwaError。管理页 API（manager_api.py:842 已 str()）与 CLI（cli/gateway.py:251）均已保证字符串，仅直接库调用受影响。
- 证据：代码阅读。
- 建议：先 `str(target or "")` 再 strip。

## 3) 无问题的文件

五个文件均有疑点（如上），无"未发现明显问题"项；其中 **manager_service.py 与 daemon.py** 仅有 minor 级问题，未发现 major/critical。整体未发现 token 校验绕过、路径穿越、越权等 critical 级缺陷。

## 测试证据（附注）

- `python -m pytest tests/test_gateway_switch.py -q`：运行完成，**1 个失败**：`test_cli_switch_dry_run_precheck_failure_shows_error`（期望 exit_code 1，实际 2）。用内联脚本复现确认 exit 2 来自 CLI 平台门禁 `require_supported_platform()` 的"Windows 原生不受支持；请在 WSL2…"输出——即在本机（Windows 原生）环境下 CLI 在命令执行前就退出，属**测试环境问题而非切换逻辑缺陷**（该测试在 Linux/WSL2 目标平台应通过）；其余用例（含回滚 YAML、review 失败不假绿、回滚复核失败标 degraded、noop、dry-run 不写盘、非法 target 拒绝等）未报失败。
- 另外四个测试文件（test_manager_api / test_manager_service / test_daemon / test_gateway_service）的两次运行均因会话中断未取得结果，未纳入证据；上述发现均基于完整代码阅读。

## 建议修复优先级

1. gateway_switch：事务级锁、回滚还原 manifest/registry、损坏 manifest 不静默跳过（#major ×3）
2. gateway_service：start_gateway 失败回滚 builtin（#major）
3. manager_api：body 布尔/枚举参数校验、批量删除确认门禁（#minor 但用户可见）
4. 其余 minor 按性价比择机处理。
