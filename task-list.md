# 任务跟踪列表

记录本项目所有任务：代码 bug、bug 转需求、新增需求、需求调整、功能开发、代码审查、测试数据、文档维护、配置运维等。

> 说明：本文件是当前项目的任务清单。所有新增事项、状态变更和完成记录都应同步写入本文件。
> 字段说明：动作字段只允许以下 8 个固定枚举：修复、开发、优化、调整、规划、检查、文档、运维。
> 时间说明：发现时间和完成时间分开记录，格式为 YYYY-MM-DD HH:MM，使用机器本地时区的 24 小时制时间；未完成事项的完成时间填 -。
> 归并规则：审计、复核、核查、审查、验证、评估统一记为“检查”；重构、清理统一记为“优化”；方案、梳理统一记为“规划”；记录类文档事项统一记为“文档”。

## 代码 Bug

| ID | 动作 | 问题描述 | 发现时间 | 完成时间 | 状态 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| BUG-001 | 修复 | 重复启动同一静态实例会泄漏旧的 builtin 静态服务进程 | 2026-07-05 10:26 | 2026-07-05 10:42 | 已完成 | hosting._enable_static 在分配端口前先 is_enabled→disable 停旧进程；e2e 验证再次 start 后旧 PID 已终止、registry 仅 1 端口、无孤儿（对应用户走查 bug #2） |
| BUG-002 | 修复 | 端口占用检测可能将已监听端口误判为空闲 | 2026-07-05 10:26 | 2026-07-05 11:15 | 已完成 | is_port_in_use 移除 SO_REUSEADDR 改独占 bind 探测，Windows 不再把已监听端口判为空闲；新增 0.0.0.0 监听回归测试，全量 168 通过 |
| BUG-003 | 修复 | staticGateway 配置未被实际尊重 | 2026-07-05 10:26 | 2026-07-05 11:15 | 已完成 | detect_backend 改为读 config.staticGateway：builtin 强制 builtin；caddy 有则用、无则降级 builtin+告警；nginx 等未实现降级 builtin+告警；3 条配置尊重回归测试 |
| BUG-004 | 修复 | 子目录 index.html 被识别但托管根目录仍指向 current 根 | 2026-07-05 10:26 | 2026-07-05 10:42 | 已完成 | host_static 改为以 index.parent 为静态根同步（嵌套时拍平该层）；e2e 验证 public/index.html 在根、GET / 返回真实页面而非目录列表（对应用户走查 bug #1） |
| BUG-005 | 修复 | runtime 切换后 registry 残留旧子表记录 | 2026-07-05 10:26 | 2026-07-05 11:15 | 已完成 | 新增 delete_container/delete_static_site DAO；upsert_from_manifest 在 upsert 当前子表后删除另一侧旧行；双向切换回归测试 |
| BUG-006 | 修复 | builtin 网关 gateway.log 文件句柄泄漏，导致实例目录无法删除 | 2026-07-05 10:30 | 2026-07-05 11:15 | 已完成 | _start_builtin 改 try/finally 在 Popen 后关闭父进程侧 log_fh（子进程已继承句柄，父进程关闭安全）；新增 enable+disable 后 gateway.log 可删除回归测试（Windows PermissionError 场景） |
| BUG-007 | 修复 | scanner.summarize 的 total_files 双重计数顶层文件 | 2026-07-05 10:30 | 2026-07-05 10:42 | 已完成 | summarize 遍历时跳过 path.parent==root 的顶层项；实测 3 顶层文件 total_files=3、sqlite_files 不重复（对应用户走查 bug #4） |
| BUG-008 | 修复 | 导入把 zip 文件大小写进 data_size_bytes（该列语义为 data/ 目录大小，WBS-19.08），管理页数据目录大小显示错误 | 2026-07-05 10:42 | 2026-07-05 10:42 | 已完成 | importer 改为写 data/ 目录真实大小（导入时为 0），不再塞 zip 体积（用户走查 bug #3） |
| BUG-009 | 修复 | Pipfile 用 requirements 行解析器解析（Pipfile 实为 TOML），[[source]] 的 name/url/verify_ssl 被误当依赖，可能污染框架识别 | 2026-07-05 10:42 | 2026-07-05 10:42 | 已完成 | 新增 _read_pipfile 按 TOML 解析 [packages]/[dev-packages] 段键名（用户走查 bug #5） |
| BUG-010 | 修复 | stop_instance 对容器实例静默无操作，CLI 仍打印已停止，用户误以为成功 | 2026-07-05 10:42 | 2026-07-05 10:42 | 已完成 | Phase 3 起改为派发到 stop_container（docker compose stop），不再静默无操作；静态实例走 gateway.disable，端口登记保留供 start 复用（BUG-045 后不再 release_instance）。原「抛 HostingError」方案已被容器 stop 实现取代（用户走查 bug #6） |
| BUG-011 | 修复 | reload_all 首次无旧配置且 reload 失败时，坏的新 Caddyfile 残留原地，影响后续 reload | 2026-07-05 10:42 | 2026-07-05 10:42 | 已完成 | previous is None 分支失败时 unlink 主配置；新增两条 reload 回归测试（用户走查 bug #7） |
| BUG-012 | 修复 | has_manage_py 已采集但从未参与 Django 识别（仅靠依赖判断），信号被浪费 | 2026-07-05 10:42 | 2026-07-05 10:42 | 已完成 | _detect_python 在依赖未命中 django 时据 has_manage_py 补识别（用户走查 bug #8） |
| BUG-013 | 修复 | 嵌套 index.html + 根目录同级资源（如 current/shared.css 与 current/site/index.html 同级）会丢失 sibling | 2026-07-05 11:15 | 2026-07-05 11:20 | 已完成 | host_static 改为同步整个 current/，再把 index 所在子目录内容提升到 public/ 根（新增 _copy_item/_promote_to_root）；e2e 验证 GET /shared.css、/style.css、/site/style.css 均可访问，169 测试通过（BUG-004 边界，用户批准方案） |
| BUG-014 | 修复 | Caddy 主配置关闭 admin 会导致后续 reload 失败 | 2026-07-05 13:09 | 2026-07-05 14:55 | 已完成 | _assemble_main_config 移除 admin off 全局块，保留默认 admin 端点 :2019；caddy reload 后续 enable/disable 不再失败。回归：test_assemble_main_config_has_no_admin_off（附件 P1） |
| BUG-015 | 修复 | builtin 静态服务停止时未等待并校验进程真正退出 | 2026-07-05 13:09 | 2026-07-05 14:55 | 已完成 | _kill_process 改返回 bool，taskkill 非零不立即判败，新增 _wait_for_exit 轮询 _pid_alive 校验真正退出；_stop_builtin 仅在成功时清 PID，失败保留 PID 文件便于排查。回归 3 条（附件 P2） |
| BUG-016 | 修复 | gateway.enable 失败后已分配端口未回滚释放 | 2026-07-05 13:09 | 2026-07-05 14:55 | 已完成 | _enable_static 在 gateway.enable 抛错时 allocator.release(host_port)；host_container except 块首行 release_instance 释放 FAILED 实例端口。回归 3 条（附件 P2） |
| BUG-017 | 修复 | 并发端口分配使用 INSERT OR REPLACE 会覆盖端口归属 | 2026-07-05 13:09 | 2026-07-05 14:55 | 已完成 | allocate_port 改 INSERT OR IGNORE + rowcount + 归属校验返回 bool；PortAllocator.allocate 与 _ensure_container_port 检查返回值，竞争输家跳到下一候选。回归 4 条（附件 P2） |
| BUG-018 | 修复 | Python 3.10 环境缺少 tomllib 回退导致 pyproject-only Python Web 项目误识别 | 2026-07-05 13:09 | 2026-07-05 14:44 | 已完成 | 按预设锁定 Python 3.13：pyproject requires-python 提升到 >=3.13、target-version=py313、移除 tomli 条件依赖；scanner 直接 import tomllib（3.11+ 标准库），删除 3.10 回退与 None 守卫，隐患消除 |
| BUG-019 | 修复 | package.json 扫描未合并 devDependencies，导致 Vite/Svelte 等前端项目误识别 | 2026-07-05 13:09 | 2026-07-05 14:55 | 已完成 | summarize 合并 devDependencies + dependencies 进 node_deps（dependencies 版本优先）；Vite/Svelte 等 dev-only 前端模板现可命中 frontend-static。回归 2 条（附件 P2） |
| BUG-020 | 修复 | Caddy import/root 路径未安全引用，工作区路径含空格时 reload 失败 | 2026-07-05 13:09 | 2026-07-05 14:55 | 已完成 | 新增 _caddy_quote：默认反引号（Caddyfile 原始字符串）包裹路径，含反引号时回退双引号+转义；import 与 root 路径均经引用。回归 3 条（附件 P2） |
| BUG-021 | 修复 | lifecycle.remove_instance/restart_instance 只捕获 HostingError，容器 stop 抛 DockerError 时移除/重启失败 | 2026-07-05 14:30 | 2026-07-05 14:30 | 已完成 | DockerError/GatewayError 与 HostingError 是 LwaError 平级子类；Docker 不可用/compose 缺失/从未部署时 stop_container 抛 DockerError 未被兜底，导致 remove 无法仅删索引；两处"先停"兜底放宽到 except LwaError，全量测试通过 |
| BUG-022 | 修复 | BuildQueue 在 rebuild_instance 中每次新建，buildConcurrency 不能跨实例全局限流 | 2026-07-05 14:42 | 2026-07-05 22:09 | 已完成 | get_build_queue() 提供进程内单例，同一 Python 进程内所有 rebuild 共享 BoundedSemaphore，buildConcurrency 生效；lifecycle.rebuild_instance 已改用。V1 边界：多个独立 lwa CLI 进程之间仍可能并行构建，跨进程全局限流留待 daemon（Phase 5）。回归：test_separate_queue_instances_share_concurrency，全量 382 通过 |
| BUG-023 | 修复 | 构建排队超时后实例状态会停留在 queued | 2026-07-05 14:42 | 2026-07-05 22:09 | 已完成 | _mark_timeout 现在写 Status.FAILED 与 last_error，并记录超时事件；回归断言超时后 registry.status=failed 且 last_error 含排队超时，全量 382 通过 |
| BUG-024 | 修复 | Pipfile-only Python Web 项目会生成复制 requirements.txt 的不可构建 Dockerfile | 2026-07-05 14:42 | 2026-07-05 22:09 | 已完成 | scanner 对 Pipfile-only 返回 pipenv 安装命令；Dockerfile 模板新增 Pipfile 分支，复制 current/Pipfile* 并执行 pipenv install --system --skip-lock，不再复制 requirements.txt。回归 2 条，全量 382 通过 |
| BUG-025 | 修复 | remove --purge 未校验 instance_id 路径边界，可能删除 apps/ 外目录 | 2026-07-05 14:42 | 2026-07-05 22:09 | 已完成 | 按倒序核查时已存在修复痕迹：Workspace.validate_instance_id 校验 slug；instance_lock/app_dir 入口拒绝非法 ID；remove_instance purge 前 resolve 并确认位于 apps/ 内；已有 BUG-025 回归测试覆盖，未重复改动 |
| BUG-026 | 修复 | lwa status 未先观测回写，registry 状态可能长期陈旧 | 2026-07-05 21:51 | 2026-07-05 22:09 | 已完成 | cli.status 展示前调用 sync_status(ws, config, reg, instance_id)，单实例和全量状态都会先观测回写；新增 CLI 回归验证 running 陈旧状态会输出 stopped，全量 382 通过 |
| BUG-027 | 修复 | stats 容器资源统计按 instance_id 子串匹配，可能误归属到其他实例 | 2026-07-05 21:51 | 2026-07-05 22:09 | 已完成 | _parse_container_stats 改为精确匹配 lwa-{id} / lwa-{id}-app，并去除可能的前导 /；新增 api 不误命中 api2 的回归测试，全量 382 通过 |
| BUG-028 | 修复 | 管理页 `/api/instances` 缺少 stack/database/servingMode/容器资源字段，列表「技术栈」「数据库」「资源」永远为空 | 2026-07-06 00:46 | 2026-07-06 11:20 | 已完成 | `InstanceStatus` 合并 registry 的 stack_json、database_type、serving_mode、resource_profile 与 resources 快照；回归覆盖列表 API 与状态序列化字段 |
| BUG-029 | 修复 | `validate_manager_binding` 未接入 `run_manager`/CLI 启动流程，与文档承诺不符 | 2026-07-06 00:46 | 2026-07-06 11:20 | 已完成 | `run_manager` 与 CLI `manager start` 均调用 `validate_manager_binding` + `assert_no_critical`；新增 LAN 绑定无 token 拒绝启动回归 |
| BUG-030 | 修复 | `/api/stats` 和 `/api/pending` 未 sync_status，`/api/instances` 会 sync，前端并行 refresh 导致统计、待处理列表与实例列表不一致 | 2026-07-06 00:46 | 2026-07-06 11:20 | 已完成 | stats/pending 读取前同步状态；`sync_status` 跳过 pending/queued/building，避免待处理实例被观测覆盖；新增 stats/pending 回归，全量通过 |
| BUG-031 | 修复 | `doctor.check_port_pool` 仍用 SO_REUSEADDR，Windows 可能误判端口可用（BUG-002 同类） | 2026-07-06 00:46 | 2026-07-06 11:20 | 已完成 | 复核已修复：`_default_port_in_use` 委托 `ports.is_port_in_use` 独占 bind；现有 wildcard listener 回归保持通过 |
| BUG-032 | 修复 | `audit_compose` 遗漏 Compose dict 格式 volumes 的宿主路径审计 | 2026-07-06 00:46 | 2026-07-06 11:20 | 已完成 | `_audit_volume` 支持 service volumes dict 形式 target→source；新增 `/app/data: /etc/passwd` 触发 host_sensitive_mount 回归 |
| BUG-033 | 修复 | `start_daemon` 无互斥，并发 `lwa daemon on` 可能 state.pid 与实际 watcher 不一致 | 2026-07-06 00:46 | 2026-07-06 11:20 | 已完成 | 新增 `daemon-start.lock` 串行化启动，启动锁内二次确认运行态；并发 start 回归验证只 spawn 一次 |
| BUG-034 | 修复 | daemon 处理失败 zip 仍写入 daemon-processed.json，无法自动重试 | 2026-07-06 00:46 | 2026-07-06 11:20 | 已完成 | `run_watcher` 对 process_fn 返回 failed 不写 processed，保留待下轮重试；新增失败 zip 不落标记回归 |
| BUG-035 | 修复 | 管理页实例列表「形态」列误显示 runtime，与「运行层」列重复 | 2026-07-06 00:46 | 2026-07-06 11:20 | 已完成 | `app.js` 形态列改读 `servingMode`，运行层仍读 `runtime`；与 BUG-028 列表字段回归一并覆盖 |
| BUG-036 | 修复 | watcher 子进程异常退出后 state 仍 enabled=True，CLI 误报 daemon 已启动 | 2026-07-06 00:46 | 2026-07-06 11:20 | 已完成 | `start_daemon` 等待子进程拿到 watcher 锁；启动失败/立即退出时回滚 state.enabled=False 并清理；新增回归 |
| BUG-037 | 修复 | `_load_manifest_dict` 返回类型标注为 dict 但文件不存在时返回 None | 2026-07-06 00:46 | 2026-07-06 11:20 | 已完成 | 类型标注改为 `dict[str, Any] \| None`，语义与实现一致；compileall 与全量 pytest 通过 |
| BUG-038 | 修复 | daemon 仅按 zip 路径去重，同名新包覆盖后会被永久跳过，无法再次自动导入 | 2026-07-06 09:47 | 2026-07-06 11:20 | 已完成 | processed key 改为路径+大小+mtime_ns 指纹，并兼容旧路径标记；同名覆盖 zip 重新处理回归通过 |
| BUG-039 | 修复 | `lwa doctor` 会把本系统已分配并正常使用的端口也判为冲突失败 | 2026-07-06 09:47 | 2026-07-06 11:20 | 已完成 | `run_doctor` 读取 registry 已分配端口并传给 `check_port_pool` 排除实例端口；管理端口仍严格检查；新增两条回归 |
| BUG-040 | 修复 | 日志接口 category 未校验，存在路径穿越读取实例日志目录外文件的风险 | 2026-07-06 09:47 | 2026-07-06 11:20 | 已完成 | `logs.validate_log_category` 限制为 LOG_CATEGORIES，并校验 resolve 后路径仍在 app_logs；API/模块回归返回 400/PathError |
| BUG-041 | 修复 | pnpm/yarn 锁文件项目会被误判为 npm ci，导致 Dockerfile 构建失败 | 2026-07-06 09:47 | 2026-07-06 11:20 | 已完成 | scanner 识别 npm/pnpm/yarn 包管理器；Dockerfile 按包管理器复制对应 lock 文件并运行对应 install；新增 scanner/Dockerfile 回归 |
| BUG-042 | 修复 | Compose 安全审计漏掉 Windows 盘符 bind mount | 2026-07-06 09:47 | 2026-07-06 11:20 | 已完成 | Compose 短格式 volume 解析兼容 Windows 盘符；`C:\Users:/app/host` 触发 host_sensitive_mount 回归 |
| BUG-043 | 修复 | 管理页详情中的构建记录和事件字段名与 API 返回不匹配，时间、类型、错误摘要显示为空 | 2026-07-06 09:47 | 2026-07-06 11:20 | 已完成 | 详情 API 返回 camelCase builds/events/resources；前端兼容 camelCase 与 snake_case；新增详情字段回归 |
| BUG-044 | 修复 | 管理 API 遇非法实例 ID 返回 500 而不是 400 | 2026-07-06 09:47 | 2026-07-06 11:20 | 已完成 | 复核已修复：PathError/SchemaError/ConfigError 等映射 bad_request；新增 `Bad_ID` 返回 400 回归 |
| BUG-045 | 修复 | 静态实例 stop 后端口无法稳定复用：(1) stop 释放端口登记，旧端口可被重分配给别的实例（跨实例内容混淆）；(2) stop 后端口残留 TIME_WAIT，独占 bind 的 `is_port_in_use` 误判占用，复用判定恒为假，重启报健康检查失败 | 2026-07-06 09:46 | 2026-07-06 11:30 | 已完成 | (1) `stop_instance` 静态分支不再 `release_instance`，保留端口登记供 start 复用（与容器路径对称）；(2) 新增 `is_port_listening`（connect 探测，TIME_WAIT 不影响），`_ensure_static_port`/`_ensure_container_port` 复用判定改用它；分配器 `PortAllocator.allocate` 仍用严格 `is_port_in_use` 避开 TIME_WAIT；(3) `StaticGateway` 缓存 builtin 子进程 `Popen` 句柄 + `waitpid` 回收僵尸，避免 `_pid_alive` 误判存活导致 kill 失败、端口无法释放；builtin 启动后改 `_wait_until_healthy` 轮询健康检查，避免偶发误回滚。注：此前代码以 BUG-028 引用本问题，与清单 BUG-028（管理页列表字段）撞号，已统一改为 BUG-045；`static_gateway.py` 中误标 BUG-016 的注释亦已更正。回归 test_stop_static_then_restart_reuses_port/test_stopped_static_port_not_reassigned，全量 577 passed/4 skipped |
| BUG-046 | 修复 | lifecycle 文件锁长耗时 rebuild/build 期间无心跳，30 分钟后可被误回收导致跨进程并发操作同一实例 | 2026-07-06 17:35 | 2026-07-06 18:35 | 已完成 | 新增 `_LOCK_HEARTBEAT_INTERVAL=min(1800/3,300)=300s` 与 `_touch_lock_heartbeat`（参考 daemon BUG-030，临时文件 + os.replace 原子刷新）；`instance_lock` 持锁期间启动 daemon 后台线程按间隔刷新锁文件时间戳，`finally` 中 Event 停止并 join(5s)；持锁超过 stale 阈值后 `_lock_is_stale` 仍返回 False（心跳持续刷新）。回归：test_instance_lock_heartbeat_refreshes_timestamp / test_instance_lock_heartbeat_keeps_lock_fresh，全量 609 passed/4 skipped |
| BUG-047 | 修复 | `remove_instance` 写入的 remove 事件被 `ON DELETE CASCADE` 级联删除，审计链断裂 | 2026-07-06 17:35 | 2026-07-06 18:35 | 已完成 | 采用 orphan event 方案（events.instance_id 列本就 nullable，add_event 签名已支持 None）：remove 事件以 instance_id=NULL 写入，不受级联影响；message 中保留实例 ID 文本便于追溯。回归：test_remove_keeps_audit_event_as_orphan 断言 remove 事件存留且 instance_id 为 NULL，全量 609 passed/4 skipped |
| BUG-048 | 修复 | 构建进程崩溃后实例可能永久卡在 `building`，`sync_status` 跳过 building 状态 | 2026-07-06 17:35 | 2026-07-06 18:35 | 已完成 | `sync_status` 不再无条件跳过 building：新增 `_recover_stale_building` 检测孤儿 building（判据：最新 builds 行 status=running 且 started_at 超 `_STALE_BUILDING_SECONDS=3600s`；无 builds 行时用 instances.updated_at 兜底），超时则回写 failed + last_error + build_recover 事件 + 收尾孤儿 builds 行。pending/queued 仍跳过（构建前过渡态，不可纠正）。回归 3 条：有 builds 行回收 / 未超时保留 / 无 builds 行兜底回收，全量 609 passed/4 skipped |
| BUG-049 | 修复 | zip 解压未防御 symlink 类 zip slip，`audit_zip_members` 未被 importer 调用 | 2026-07-06 17:35 | 2026-07-06 18:35 | 已完成 | 三层防御：(1) `audit_zip_members` 新增 `modes` 参数 + `_is_symlink_mode`（S_ISLNK）检测符号链接成员，返回 `zip_symlink` critical 发现；(2) importer `_safe_extract` 解压前调用 `audit_zip_members`（含 modes），critical 级 `has_critical` 拒绝解压并抛 ZipImportError，WARN 级记录日志；(3) 解压后 `rglob("*")` 深度防御扫描 symlink（兜底 external_attr 未声明的异常 zip）。回归：test_import_rejects_zip_slip 改匹配 zip_slip、新增 test_import_rejects_zip_symlink；security 新增 symlink 检测 + modes 短缺边界回归，全量 609 passed/4 skipped |
| BUG-050 | 修复 | 管理页 `building`/`queued` 时仍允许点击「启动」，易触发并发操作与锁竞争 | 2026-07-06 17:35 | 2026-07-06 18:35 | 已完成 | `app.js` `opsHtml` 新增 `inProgress` 判定（building/queued/pending），对 start/stop/restart/rebuild 四个操作按钮在 inProgress 时统一禁用（start 保留 running 禁用，stop 保留非 running 禁用）；「打开」「日志」不受影响。顺带补 `.badge-queued` CSS（原缺失）。全量 609 passed/4 skipped |
| BUG-051 | 修复 | 环境检查把可用 Compose v2.40 环境和无 Caddy 静态托管环境误判为失败 | 2026-07-06 18:07 | 2026-07-06 18:07 | 已完成 | Compose 拆分最低线 2.40.2 与推荐线 5.2.0：runtime 只按最低线阻断，doctor/setup 对低于推荐给 WARN；Caddy 缺失按运行时 fallback 降级 builtin 并给 WARN；同步 README/FAQ/known-limitations/setup skill/CLI 文案；新增 Compose v2.40、低版本失败、Caddy 缺失回归；全量测试 600 passed/4 skipped |
| BUG-053 | 修复 | `manager on`/`init` 在 17800 已有其他工作区管理页时会误恢复为当前工作区管理页 | 2026-07-06 20:23 | 2026-07-06 20:30 | 已完成 | `/api/health` 增加 `workspaceRoot`；`health_matches_workspace` 校验归属，`start_manager` 仅本工作区恢复状态，否则抛端口占用；`is_running` 同步校验；回归 test_start_manager_rejects_foreign_workspace_on_port 等 |
| BUG-054 | 修复 | `manager off` 终止失败仍把状态写成 disabled，CLI 仍提示已停止 | 2026-07-06 20:23 | 2026-07-06 20:30 | 已完成 | `stop_manager` 先 `_terminate_pid` 成功后再写 `enabled=false`；`cli.manager_off` 检查返回值并 exit 1；回归 test_stop_manager_keeps_enabled_when_terminate_fails |
| BUG-055 | 修复 | 新增 `lwa-update-runtime` 内置 skill 后测试仍硬编码 13 个 skills，导致全量 pytest 失败 | 2026-07-06 20:23 | 2026-07-06 20:30 | 已完成 | `test_init_copies_skills`/`test_e2e_init_creates_clean_workspace` 改为 14 并断言 `lwa-update-runtime`；全量 pytest 625 passed/4 skipped |
| BUG-052 | 修复 | 管理页并发刷新时 registry 只读查询未串行化，误把运行中静态实例标为 stopped（开声纹 demo 后 demo-static 显示/感知为关闭） | 2026-07-06 20:07 | 2026-07-06 20:14 | 已完成 | 根因：`/api/stats` 与 `/api/instances` 并行 `sync_status`，共享 SQLite 连接裸 `conn.execute` 触发 `InterfaceError` 与状态抖动；`Registry._fetchone/_fetchall` + `locked_connection` 串行化读；`_observe_static_status` 增加 HTTP 健康兜底。回归 test_registry_concurrent_reads_thread_safe、test_observe_static_status_uses_health_when_pid_missing；hammer 60 并发请求 0 错误 |
| BUG-056 | 修复 | update_zip 在 current 已换入后若 manifest/registry/original.zip 写入失败，不会回滚到旧 current | 2026-07-06 23:26 | 2026-07-07 00:10 | 已完成 | current 换入后异常时恢复旧 current、manifest、original.zip，并尽力恢复 registry 资源；新增 test_update_failure_after_current_swap_rolls_back；目标回归通过 |
| BUG-057 | 修复 | 管理页实例更新 API 的相对 zipPath 可通过 ../ 逃出 inbox，且绝对路径未限制来源 | 2026-07-06 23:26 | 2026-07-07 00:10 | 已完成 | update API 对相对/绝对 zipPath resolve 后强制 relative_to(workspace.inbox)，越界返回 400；新增相对 ../ 与绝对路径越界回归；目标回归通过 |
| BUG-058 | 修复 | 版本解析测试仍断言 0.3.1，但当前最新 Git commit 主题为 V0.3.2，导致全量 pytest 失败 | 2026-07-06 23:26 | 2026-07-07 00:10 | 已完成 | tests/test_version_info.py 改为断言 0.3.2；pyproject、version_info fallback 与 CLI 说明同步到 V0.3.2；目标回归通过 |
| BUG-059 | 修复 | update_zip --no-keep-data 先写资源统计再清空 data，管理页 dataSizeBytes 可能显示旧值 | 2026-07-06 23:26 | 2026-07-07 00:10 | 已完成 | keep_data=False 时先清空 data/ 再 upsert_resources，data_size_bytes 归零；test_update_no_keep_data_clears_data 增加 registry 断言；目标回归通过 |
| BUG-060 | 修复 | Docker Engine 最低版本门槛过高，误挡 Docker Desktop 4.55.0 / Engine 29.1.3 的真实容器验收 | 2026-07-07 00:10 | 2026-07-07 00:10 | 已完成 | MIN_DOCKER_VERSION 调整为 29.0.0；同步 README/setup/skill 文案；新增 Docker Desktop 4.55 Engine 29.1.3 回归；真实 Docker 自检 4/4 与 WBS-29.09/29.11/29.12 手工验收通过 |
| BUG-061 | 修复 | daemon_start_lock 只有文件锁没有进程内互斥，同进程并发 daemon on 可重复 spawn watcher | 2026-07-07 00:15 | 2026-07-07 00:15 | 已完成 | 新增模块级 threading.Lock 包裹 daemon_start_lock 文件锁；test_start_daemon_serializes_concurrent_start 通过 |
| BUG-062 | 修复 | update_zip 暂存区清理测试断言路径错误，且 --force-kind-change 静态转容器时旧 hostPort 未迁移 | 2026-07-07 08:48 | 2026-07-07 08:48 | 已完成 | test_update_failure_rolls_back 改查 current.new/current.old；_preserve_hostport 新形态子表查不到时回退旧形态子表，静态→容器迁移保留 hostPort；同步 CLI/Skill 文案；全量 pytest 通过，compileall 与 node --check 通过 |
| BUG-063 | 修复 | `updater.migrate_config_defaults` 对嵌套字段做浅合并，旧配置只写了部分子键（如 `portPool.start`）时整体覆盖默认，缺失子键（如 `end`）在写回文件中丢失（代码审查发现） | 2026-07-07 08:52 | 2026-07-07 08:52 | 已完成 | 新增 `_deep_merge_defaults` 深层合并（同为 dict 的键递归补齐 defaults 子键，用户值优先）；`portPool`/`defaultResourceLimits`/`staticRateLimit` 等嵌套字段不再被整体覆盖；新增回归 `test_migrate_config_deep_merges_nested_dict`（用户 start 保留 + end 从默认补齐）；全量 pytest 通过 |
| BUG-064 | 优化 | pyflakes 报 8 处问题：`health.py` 冗余海象运算符 + 7 处未使用导入（代码审查发现） | 2026-07-07 08:52 | 2026-07-07 08:52 | 已完成 | `health.py` 移除 `instance_id :=` 未用海象；清理 `hosting`(GatewayError)/`config`(CONFIG_FILENAME)/`importer`(now_iso)/`setup`(STATUS_SKIP)/`init_workspace`(LwaError)/`cli`(__version__)/`lifecycle`(DesiredState) 共 7 处未使用导入；`python3 -m pyflakes src/local_webpage_access/` 退出码 0 全清；全量 pytest 通过 |

## 调整事项

| ID | 动作 | 事项 | 发现时间 | 完成时间 | 状态 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| ADJ-001 | 调整 | task-list.md 存在重复 BUG-052，导致任务清单校验失败 | 2026-07-06 23:26 | 2026-07-06 23:35 | 已完成 | 删除行 65 简略重复项，保留行 69 含根因与回归的 BUG-052；DEV-032 移入功能开发；DEV-029/PLN-008 进行中项完成时间改 -；task-list check 通过 |
| ADJ-002 | 调整 | 待改进功能计划文档状态仍为待规划，与代码和 task-list 完成记录不一致 | 2026-07-06 23:27 | 2026-07-06 23:35 | 已完成 | docs/plan/待改进功能点记录-20260706.md 中 IMP-001/005/006/007/008/009 状态已同步为已完成（DEV-034~039） |
| ADJ-003 | 调整 | IMP-001 计划要求剥离 env/，实现中明确排除裸 env，验收口径需统一 | 2026-07-06 23:27 | 2026-07-06 23:35 | 已完成 | 计划文档清理规则移除裸 env/，注明保留原因（环境配置目录）；与 security.py _STRIPPABLE_SEGMENTS 及注释一致 |

## 检查事项

| ID | 动作 | 事项 | 发现时间 | 完成时间 | 状态 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| CHK-001 | 检查 | Phase 5-7 全面代码 bug 审查（daemon/manager_api/security/doctor/管理页前端） | 2026-07-06 00:46 | 2026-07-06 09:46 | 已完成 | 全量 pytest 540 passed/4 skipped；发现 BUG-028~037 共 10 项待修复，已写入本清单 |
| CHK-002 | 检查 | 全量代码 bug 复审（lifecycle/hosting/importer/daemon/build_queue/前端） | 2026-07-06 17:35 | 2026-07-06 17:35 | 已完成 | 全量 pytest 577 passed/4 skipped；历史 BUG-001~045 已修复；新发现 BUG-046~050 共 5 项待修复；跨进程 buildConcurrency 为 BUG-022 已知 V1 边界 |
| CHK-003 | 检查 | 当前未提交代码 bug 审查 | 2026-07-06 20:23 | 2026-07-06 20:23 | 已完成 | 审查未提交 diff，执行 compileall、node --check、目标 pytest 与全量 pytest；发现 BUG-053~055 待修复，确认 BUG-052 相关测试通过；全量 pytest 619 passed/4 skipped/2 failed |
| CHK-004 | 检查 | 待改进功能开发完成性与待提交代码 bug 审查 | 2026-07-06 23:26 | 2026-07-06 23:26 | 已完成 | 对照 docs/plan/待改进功能点记录-20260706.md 与 WBS，审阅待提交 diff；执行 compileall、node --check、pytest、task-list check；发现 update_zip 回滚窗口、管理 API zipPath 越界、版本断言失败、task-list 重复 ID 等问题 |
| CHK-005 | 检查 | 未提交代码 bug 复审（IMP-001~009 / updater / 全量回归） | 2026-07-07 08:26 | 2026-07-07 08:31 | 已完成 | 全量 pytest 738 passed/4 skipped；compileall + node --check 通过；未发现新的 critical bug；遗留：test_update_failure_rolls_back 暂存区断言路径错误（应 current.new 非 {id}.current.new）、force_kind_change 跨形态 hostPort 迁移未覆盖 |

## 测试数据

| ID | 动作 | 事项 | 发现时间 | 完成时间 | 状态 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |

## 文档维护

| ID | 动作 | 事项 | 发现时间 | 完成时间 | 状态 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| DOC-001 | 文档 | 统一文档命名：5 个 local-web-access-* 历史文档重命名为 local-webpage-access-*，同步更新引用 | 2026-07-06 17:46 | 2026-07-06 17:46 | 已完成 | docs/plan 2 个（v1-design/v1-wbs）+ docs/discussion 3 个（proposal/方案/设计意见）；git mv 保留历史；README/config.py/文档内交叉引用共 10 处全替换、0 残留；config 测试通过 |
| DOC-002 | 文档 | Runtime 工作区说明 + 待改进功能点 backlog（IMP-001~007） | 2026-07-06 19:50 | 2026-07-06 19:52 | 已完成 | docs/runtime-workspace.md；docs/plan/待改进功能点记录-20260706.md；README/manager-page 链接；IMP-003 本机免 token 已实现并重启管理页验证 |
| DOC-003 | 文档 | IMP-006 路径别名规则：用户/CLI/Skill 显式指定才启用，默认仍端口 | 2026-07-06 19:55 | 2026-07-06 19:55 | 已完成 | 待改进功能点记录 IMP-006 更新；规划 `--path-alias` 可选参数 |
| DOC-004 | 文档 | IMP-008 `lwa update` 工作区热重载规划 + Skill 草案 | 2026-07-06 19:58 | 2026-07-06 19:58 | 已完成 | 待改进 IMP-008；skills/lwa-update-runtime；runtime-workspace 开发期重载说明；README/setup skill 交叉引用 |
| DOC-005 | 文档 | 检查并补充待改进功能点记录细节 | 2026-07-06 20:03 | 2026-07-06 20:03 | 已完成 | docs/plan/待改进功能点记录-20260706.md：新增维护说明；补强 IMP-001/005/006/007/008 的非目标、边界、风险、数据口径、失败处理与验收 |
| DOC-006 | 文档 | IMP-009 实例 zip 包更新（再导入识别与原地升级）规划 | 2026-07-06 20:20 | 2026-07-06 20:20 | 已完成 | 待改进功能点记录新增 IMP-009：`--update`/reimport、hash+slug 识别、保留 id/端口/data、与 IMP-008 区分；管理页/Skill/验收口径 |

## 功能开发

| ID | 动作 | 事项 | 发现时间 | 完成时间 | 状态 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| DEV-001 | 开发 | WBS-01 工程结构与 Python 项目骨架（pyproject/CLI 入口/模块边界/异常/日志/测试框架） | 2026-07-04 23:55 | 2026-07-05 00:06 | 已完成 | Phase 0；CLI 名 lwa；errors/logging/cli 已建，70 测试通过 |
| DEV-002 | 开发 | WBS-02 全局配置与路径管理（local-web.yml/默认值/路径工具） | 2026-07-04 23:55 | 2026-07-05 00:06 | 已完成 | Phase 0；config.py + paths.py |
| DEV-003 | 开发 | WBS-04 local-web.json Schema 与实例模型（字段定义/校验/读写/迁移预留） | 2026-07-04 23:55 | 2026-07-05 00:06 | 已完成 | Phase 0；models.py，枚举+一致性校验 |
| DEV-004 | 开发 | WBS-05 SQLite Registry（连接/迁移/七张表/DAO/状态同步） | 2026-07-04 23:55 | 2026-07-05 00:06 | 已完成 | Phase 0；instances/containers/static_sites/ports/events/builds/resources |
| DEV-005 | 开发 | WBS-03 lwa init 初始化（目录/配置/SQLite/幂等） | 2026-07-04 23:55 | 2026-07-05 00:06 | 已完成 | Phase 0；端到端验证通过 |
| DEV-006 | 开发 | WBS-06 端口池与访问入口（端口分配/冲突检查/LAN IP/URL 生成） | 2026-07-04 23:55 | 2026-07-05 00:14 | 已完成 | Phase 1；ports.py 17 测试通过 |
| DEV-007 | 开发 | WBS-07 zip 导入与实例目录管理（lwa import/hash/slug/zip slip 防护） | 2026-07-04 23:55 | 2026-07-05 00:22 | 已完成 | Phase 1；importer.py 19 测试通过，含 zip slip/单层根拍平/同名冲突/失败清理 |
| DEV-008 | 开发 | WBS-08 项目扫描与运行形态识别（static/node/python/sqlite 识别/lwa scan） | 2026-07-04 23:55 | 2026-07-05 00:22 | 已完成 | Phase 1；scanner.py 18 测试通过；四样例识别正确 |
| DEV-009 | 开发 | WBS-09 静态网关基础能力（Caddy 优先/内置兜底/配置生成/reload） | 2026-07-04 23:55 | 2026-07-05 00:32 | 已完成 | Phase 2；static_gateway.py，Caddy+builtin http.server 双后端，9 测试通过（含真实子进程启停/健康检查/回滚） |
| DEV-010 | 开发 | WBS-10 纯静态 HTML 托管流程（index.html/public/网关配置/健康检查） | 2026-07-04 23:55 | 2026-07-05 00:32 | 已完成 | Phase 2；hosting.host_static，e2e 验证 HTTP 200 可访问 |
| DEV-011 | 开发 | WBS-11 纯前端 SPA 构建托管流程（npm ci/build/dist/public/构建日志） | 2026-07-04 23:55 | 2026-07-05 00:32 | 已完成 | Phase 2；hosting.build_and_host_frontend，真实 npm 构建验证通过，失败标记 build_failed+builds/events 表 |
| DEV-012 | 开发 | WBS-12 Dockerfile 模板体系（Node/Python/SQLite 通用模板 + .dockerignore） | 2026-07-05 09:30 | 2026-07-05 13:00 | 已完成 | Phase 3；dockerfile_templates.py，按 kind/stack 选模板，注入 internalPort/resourceProfile；非 root 用户 + 健康探测钩子 |
| DEV-013 | 开发 | WBS-13 Docker Compose 编排（compose.yaml 模板 + .env + 项目名隔离） | 2026-07-05 09:30 | 2026-07-05 13:10 | 已完成 | Phase 3；compose.py，projectName=lwa-<id>，hostPort 映射 + bind mount data/ + 资源限额 mem_limit/cpus + restart unless-stopped |
| DEV-014 | 开发 | WBS-14 Docker Runtime 封装（compose build/up/stop/down/ps/image inspect 封装） | 2026-07-05 09:30 | 2026-07-05 13:20 | 已完成 | Phase 3；docker_runtime.py，_execute 统一 subprocess 封装，ensure_available/is_available/is_running/start/stop/down/build/up/container_id/image_id，所有命令写 build.log/run.log |
| DEV-015 | 开发 | WBS-15 Node 前端容器流程（host_container 分支：模板→build→up→网络观测） | 2026-07-05 09:30 | 2026-07-05 13:40 | 已完成 | Phase 3；hosting.host_container 统一编排，Node/Python/SQLite 走同一入口；compose start 轻量启动；测试用 fake runtime，不依赖真实 Docker |
| DEV-016 | 开发 | WBS-16 Python 后端容器流程（含 SQLite 持久化 bind mount + internalPort 8000） | 2026-07-05 09:30 | 2026-07-05 13:40 | 已完成 | Phase 3；与 DEV-015 共用 host_container；SQLite 数据卷 bind mount 至 data/，internalPort 默认 8000；create_build_record 落库 |
| DEV-017 | 开发 | WBS-17 生命周期编排（start/stop/restart/rebuild/remove + desiredState + 实例级锁） | 2026-07-05 13:40 | 2026-07-05 14:00 | 已完成 | Phase 4；lifecycle.py，双层锁（threading.RLock + O_EXCL 文件锁 + PID staleness 回收），start 分发轻量 start_container vs 全量 host_instance，remove 保护 data/（purge 需 --force），observe_status 回写；21 测试 |
| DEV-018 | 开发 | WBS-18 日志、状态与健康检查（分类日志/轮转/HTTP 健康/状态聚合） | 2026-07-05 13:40 | 2026-07-05 14:00 | 已完成 | Phase 4；logs.py（5 分类 + tail + 轮转 rotate_all）+ health.py（http_ok + check_health 写 last_health_check_at/last_error）+ status.py（instance_status/all_statuses/sync_status）；真实 HTTPServer 健康测试，24 测试 |
| DEV-019 | 开发 | WBS-19 资源监控与统计（整机 mem/load/disk + 实例目录/镜像/容器 stats） | 2026-07-05 13:40 | 2026-07-05 14:00 | 已完成 | Phase 4；stats.py，host_resources 读 /proc/meminfo/loadavg + disk_usage；instance_resources 量目录 + docker image inspect/stats（按 instance_id 匹配）；upsert_resources 持久化；16 测试 |
| DEV-020 | 开发 | WBS-20 构建队列与并发限制（信号量限流 + queued 状态 + 超时/取消预留） | 2026-07-05 13:40 | 2026-07-05 14:00 | 已完成 | Phase 4；build_queue.py，BoundedSemaphore(buildConcurrency) 默认 1，拿不到立即槽位→QUEUED+事件，wait_timeout 默认 1800s，cancel V1 占位不抢占进行中构建；12 测试 |
| DEV-021 | 开发 | WBS-21 Daemon 与 Inbox Watcher（后台守护进程/inbox 自动导入/轻量实例自动启动） | 2026-07-05 22:30 | 2026-07-06 00:30 | 已完成 | Phase 5；daemon.py + inbox_watcher，lwa daemon on/off/status；只自动启动可确定的轻量实例（static/frontend-static），pending/container 留待人工；PID 文件 + 状态查询 |
| DEV-022 | 开发 | WBS-22 管理页后端 API（FastAPI/token 鉴权/全部 /api 端点/统一错误格式） | 2026-07-05 22:30 | 2026-07-06 00:30 | 已完成 | Phase 5；manager_api.py，stats/instances/detail/logs/resources/start-stop-restart-rebuild/pending/port-pool；复用 lifecycle 同 CLI 代码路径；WAL+单连接线程安全 |
| DEV-023 | 开发 | WBS-23 管理页前端（单页应用/概览/列表/详情/操作/待处理区） | 2026-07-05 22:30 | 2026-07-06 00:30 | 已完成 | Phase 5；manager_static/ 单页前端，token 登录，覆盖统计面板/实例列表/详情日志/生命周期操作/pending+failed 队列 |
| DEV-024 | 开发 | WBS-24 大模型 Skills 文档（12 个 SKILL.md 协作场景） | 2026-07-05 22:30 | 2026-07-06 00:30 | 已完成 | Phase 6；skills/ 下 12 个 SKILL.md，覆盖导入/识别/静态/前端/容器/生命周期/排障/安全等；lwa init 复制到工作区 |
| DEV-025 | 开发 | WBS-25 安全、权限与默认保护（compose/dockerfile/zip 审计 + 管理绑定校验） | 2026-07-05 22:30 | 2026-07-06 00:30 | 已完成 | Phase 6；security.py，critical/warn/info 三级；audit_compose/dockerfile/zip_members；validate_manager_binding（LAN 绑定必须 token）；compose 生成 critical 自检拒绝写出；pending 写风险事件；42 测试 |
| DEV-026 | 开发 | WBS-26 lwa doctor 与排障辅助（环境检查 + 实例深度诊断） | 2026-07-05 22:30 | 2026-07-06 00:30 | 已完成 | Phase 6；doctor.py，CheckResult/DoctorReport；python/docker/compose/port_pool/registry/static_gateway/disk/memory 检查；diagnose_instance 深度诊断；runner/port_in_use 注入式可测；lwa doctor [--json] [ID]，fail 退出码 1；28 测试 |
| DEV-027 | 开发 | WBS-27 样例项目与测试夹具（6 个样例 dict 打包 + build_zip/build_all） | 2026-07-05 22:30 | 2026-07-06 00:30 | 已完成 | Phase 7；tests/fixtures/，6 样例（static_html/vite_react/node_express/fastapi_sqlite/build_failure/pending_unknown），dict[str,str] 按需打包；EXPECTED_KIND 映射；18 测试 |
| DEV-028 | 开发 | WBS-28 单元测试与集成测试（全模块覆盖 + Docker 双重守卫） | 2026-07-05 22:30 | 2026-07-06 00:30 | 已完成 | Phase 7；conftest requires_docker marker + LWA_RUN_DOCKER_TESTS 双守卫；WBS-28.01~15 全覆盖；test_security/test_doctor/test_fixtures/test_integration_phase57；529 passed/4 skipped |
| DEV-029 | 开发 | WBS-29 端到端验收（E2E 自动化 + 手工验收清单） | 2026-07-06 00:30 | 2026-07-07 00:10 | 已完成 | Phase 7；自动化 E2E 11/11 已覆盖 init/import/静态 HTTP/start-stop-restart/logs-status-stats/管理页一致/failed-pending/doctor；真实 Docker 手工验收 29.09/29.11/29.12 通过（Node 18002、FastAPI 18003、/app/data/persist.txt stop/start 后仍为 persisted）；详见 docs/acceptance-checklist.md |
| DEV-030 | 开发 | WBS-30 文档与发布准备（README 更新 + 管理/FAQ/安全/限制/发布清单） | 2026-07-06 01:30 | 2026-07-06 02:00 | 已完成 | Phase 7；README 全面更新（Phase 0~7 全完成，新增 manager/daemon/doctor/skills 章节）；docs/manager-page.md、faq.md、security-boundary.md、known-limitations.md、release-checklist.md；文档索引齐全 |
| DEV-031 | 开发 | 管理页默认后台启动（managerEnabled 配置 + lwa manager on/off/status + init 自动拉起） | 2026-07-06 19:28 | 2026-07-06 19:30 | 已完成 | config.managerEnabled 默认 true；manager_service 后台子进程；init 自动 maybe_start_manager；CLI 新增 on/off/status；保留 manager start 前台；runtime 实测 pid=79502 17800 健康 200；6 条单测通过 |
| DEV-032 | 开发 | 应用版本号与 Git commit 主题对齐（管理页/CLI 显示 V0.3.1） | 2026-07-06 19:55 | 2026-07-06 19:56 | 已完成 | version_info.py 解析 `V0.3.1-Build...`；pyproject 0.3.1 兜底；管理页/API/lwa version 显示 V0.3.1；3 条单测 |
| DEV-033 | 开发 | 管理页前端风格优化（OKLCH 双色板 + 视觉比例/信息密度重构，克制动效） | 2026-07-06 20:04 | 2026-07-06 20:04 | 已完成 | 落地 PRODUCT.md（register=product）；style.css 全量重写为 OKLCH 明暗板：移除状态卡 border-left 侧条（违反设计禁令）改状态值着色、徽章加状态点（色盲友好）、detail 区去 uppercase eyebrow、修正 logs 模态缺 .modal-inner 致内容浮在遮罩上的 bug；index.html+app.js：技术栈列合并数据库徽章、移除与"访问地址"重复的"打开"按钮（表 13→12 列）；impeccable detect 返回 [] 无 slop、node --check 通过；动效仅 0.12s hover/focus + prefers-reduced-motion 守卫 |
| DEV-034 | 开发 | IMP-001 zip 导入前自动剥离冗余包与缓存（node_modules/__pycache__/.venv/.git/__MACOSX 等） | 2026-07-06 20:03 | 2026-07-06 20:35 | 已完成 | importer.sanitize_zip_members 在解压前按「可剥离前缀」分类剔除可重建依赖目录与平台 junk（被剥离成员不落盘），保留成员继续走 audit_zip_members 的 zip slip/symlink 防御；CLI 输出剥离摘要；含 node_modules/.bin symlink 的原版 zip 可一键 import+start；安全回归（源码目录恶意 symlink/zip slip）仍拒绝 |
| DEV-035 | 开发 | IMP-005 Caddy 静态站点简易访问频率限制（内网防护，约 3 次/秒/客户端） | 2026-07-06 20:03 | 2026-07-06 20:50 | 已完成 | config.staticRateLimit {enabled,rps,burst} 默认关；Caddy 模式经 caddy_site.conf.tpl 注入 rate_limit directive，能力不可用时 WARN 降级保持站点可访问（不因插件缺失下线）；builtin 模式输出「暂不支持」说明；reload 失败回滚旧 Caddyfile |
| DEV-036 | 开发 | IMP-006 路径别名路由（--path-alias 可选，默认仍仅端口访问） | 2026-07-06 19:55 | 2026-07-06 21:05 | 已完成 | lwa import --path-alias <slug>；manifest pathAlias 写入 registry 索引；Caddy 反向代理 /<alias>/ → upstream 去前缀转发，/<alias> 301 到带尾斜杠；slug 格式 + 全局唯一 + reserved 路径（/api 等）冲突拒绝；remove 清理别名路由；不传别名时输出与 V1 完全一致 |
| DEV-037 | 开发 | IMP-007 管理页展示端口映射关系（原端口 → hostPort） | 2026-07-06 20:03 | 2026-07-06 21:20 | 已完成 | InstanceStatus 与 /api/instances、/api/instances/{id} 统一返回 hostPort + internalPort + portMappingLabel；前端列表/详情端口列主显示 hostPort、副标「internalPort → hostPort」；静态实例不显示误导性内部端口；scanner 无法确定 internalPort 时不报错不空白 |
| DEV-038 | 开发 | IMP-008 lwa update 工作区热重载（pip 刷新 + skills 同步 + 重启 lwa 自有服务） | 2026-07-06 19:58 | 2026-07-06 21:40 | 已完成 | updater.py：pip install -e . → 同步包内 skills/ → 工作区 → 重启 manager/daemon 子进程（解决 git pull 后旧子进程仍持旧代码）；--dry-run/--skip-pip/--sync-skills/--sync-templates/--restart-instances；默认不重建 apps/ 用户实例；配套 lwa-update-runtime skill |
| DEV-039 | 开发 | IMP-009 实例 zip 包更新（再导入识别与原地升级） | 2026-07-06 20:20 | 2026-07-06 22:20 | 已完成 | importer.update_zip 原子换入（解压到 current.new → os.replace 双段交换，失败自动回滚 current/）；sourceZipHash 相同则跳过不 rebuild；kind/runtime 形态变化默认拒绝（--force-kind-change 显式确认）；保留 instance_id/hostPort（端口登记不动重启复用）/data/路径别名/desiredState；CLI import --update/-u + --dry-run/--no-restart/--no-keep-data；管理 API POST /api/instances/{id}/update；新增 lwa-import-zip skill；importer 16 + manager_api 5 单测 |

## 配置运维

| ID | 动作 | 事项 | 发现时间 | 完成时间 | 状态 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| OPS-001 | 运维 | 版本基线对齐预设（Python 3.13 / Node 24.16 / Docker 最新稳定版） | 2026-07-05 14:44 | 2026-07-05 14:44 | 已完成 | pyproject requires-python>=3.13、target-version=py313、移除 tomli 条件依赖；dockerfile 基线镜像改 node:24-alpine + python:3.13-slim；同步 test_dockerfile_templates/test_host_container 断言与 README；本机实测 Python 3.13.13 / Node v24.16.0 / Docker 29.5.2 / Compose v5.1.3 均匹配 |
| OPS-002 | 运维 | 安装 task-list 维护规则与 Stop hook 保证层，并本地化 Claude 配置（不进 GitHub） | 2026-07-06 16:33 | 2026-07-06 16:33 | 已完成 | CLAUDE.md 写入中文「会话结束任务同步」规则；.claude/settings.json 注册 Stop hook + .claude/hooks/tasklist_sync_reminder.sh（session_id 守卫，每会话首次 Stop 触发一次 block 提醒）；脚本验证输出正确且守卫不重复；standardize 检测『规则已安装 + Stop hook 已安装』；.gitignore 追加 CLAUDE.md/.claude/ 不同步 GitHub；hook 需重启会话生效 |
| OPS-003 | 运维 | .gitignore 增补 .codex/ 与 AGENTS.md（AI 工具本地配置，不同步 GitHub） | 2026-07-06 18:03 | 2026-07-06 18:03 | 已完成 | 与 CLAUDE.md/.claude/ 同类，归入「AI 工具本地配置（不同步到 GitHub）」段并将段注释由 Claude Code 泛化为 AI 工具；git check-ignore 命中 .gitignore:70/71，git status 已无未跟踪项 |
| OPS-004 | 运维 | 应用版本号提升至 V0.4.0 | 2026-07-07 09:52 | 2026-07-07 09:52 | 已完成 | pyproject.toml、version_info fallback/docstring、cli.py 说明、test_version_info 与 IMP-008 文档示例同步为 0.4.0 / V0.4.0 |

## 规划事项

| ID | 动作 | 事项 | 发现时间 | 完成时间 | 状态 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| PLN-001 | 规划 | Phase 0：准备与底座（WBS-00~WBS-05，CLI 骨架/配置/registry/schema） | 2026-07-04 23:49 | 2026-07-05 00:06 | 已完成 | 详见 docs/plan/local-webpage-access-v1-wbs-20260704.md 第 5 节；DEV-001~005 全部完成，70 测试通过 |
| PLN-002 | 规划 | Phase 1：导入、识别与端口（WBS-06~WBS-08，端口池/zip 导入/项目识别） | 2026-07-04 23:49 | 2026-07-05 00:22 | 已完成 | DEV-006~008 全部完成；e2e 验证四样例正确识别为 static/frontend-static/backend-container/fullstack-sqlite |
| PLN-003 | 规划 | Phase 2：静态路径闭环（WBS-09~WBS-11，静态网关/纯静态/前端构建） | 2026-07-04 23:49 | 2026-07-05 00:32 | 已完成 | DEV-009~011 全部完成；e2e 验证静态 HTML 与前端 SPA 构建均可经 builtin 网关访问，151 测试通过 |
| PLN-004 | 规划 | Phase 3：Docker Compose 路径闭环（WBS-12~WBS-16，Dockerfile/Compose/Runtime/Node/Python） | 2026-07-04 23:49 | 2026-07-05 14:00 | 已完成 | DEV-012~016 全部完成；host_container 统一编排 Node/Python/SQLite 容器，fake runtime 全量 hermetic 测试，无真实 Docker 依赖 |
| PLN-005 | 规划 | Phase 4：生命周期、日志、资源与队列（WBS-17~WBS-20） | 2026-07-04 23:49 | 2026-07-05 14:04 | 已完成 | DEV-017~020 全部完成；lifecycle 双层锁 + logs/health/status/stats/build_queue；CLI 新增 restart/rebuild/remove/logs/status/stats；328 测试通过 |
| PLN-006 | 规划 | Phase 5：自动化与管理页（WBS-21~WBS-23，daemon/管理页 API/前端） | 2026-07-04 23:49 | 2026-07-06 00:30 | 已完成 | DEV-021~023 全部完成；daemon inbox 自动导入 + 管理页 FastAPI + 单页前端，管理页端口 17800 |
| PLN-007 | 规划 | Phase 6：Skills、安全与排障（WBS-24~WBS-26） | 2026-07-04 23:49 | 2026-07-06 00:30 | 已完成 | DEV-024~026 全部完成；12 个 SKILL.md + security.py 审计 + doctor 排障 |
| PLN-008 | 规划 | Phase 7：测试、验收与发布（WBS-27~WBS-30，样例/单测集成/E2E/文档发布） | 2026-07-04 23:49 | 2026-07-07 00:10 | 已完成 | DEV-027~030 全部完成；6 样例夹具、单测集成、自动化 E2E、真实 Docker 容器验收与 V1 文档套件均已同步；Docker 手工验收见 docs/acceptance-checklist.md |

## 统计摘要

| 分类 | 总数 | 已完成 | 待开发/待修复 | 完成率 |
| --- | --- | --- | --- | --- |
| 代码 Bug | 64 | 64 | 0 | 100% |
| 调整事项 | 3 | 3 | 0 | 100% |
| 检查事项 | 4 | 4 | 0 | 100% |
| 测试数据 | 0 | 0 | 0 | 0% |
| 文档维护 | 6 | 6 | 0 | 100% |
| 功能开发 | 39 | 39 | 0 | 100% |
| 配置运维 | 3 | 3 | 0 | 100% |
| 规划事项 | 8 | 8 | 0 | 100% |
| **总计** | 127 | 127 | 0 | 100% |
