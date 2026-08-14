# LWA 代码审查报告（组 7：registry/data/security）

审查范围：src\local_webpage_access\ 下 14 个文件。Python 3.13，只读审查 + 运行现有测试（未跑全量套件）。
运行过的测试：test_registry.py、test_models.py、test_config.py、test_ports.py、test_logs.py、test_security.py、test_paths.py、test_health_status.py、test_status_lan_freshness.py、test_resource_profiles.py（单文件逐一运行）。
环境注：test_paths.py 1 例失败（symlink 权限 WinError 1314，Windows 环境限制，非代码 bug）；test_health_status.py 1 例 CLI 失败（"Windows 原生不受支持"平台守卫，环境限制，非代码 bug）。

---

## 1) 每个文件一行总结

| 文件 | 职责 | 是否有疑点 |
|---|---|---|
| registry/dao.py | Registry 七张表 DAO（事务写、并发安全端口登记、孤儿清理、资源快照） | 有（runtime 切换端口残留、动态列名 SQL、别名唯一性 TOCTOU、updated_at 语义） |
| registry/connection.py | SQLite 连接（WAL/FK/busy_timeout）、事务上下文、连接级锁、schema 迁移 | 有（init_db 失败路径锁泄漏、COMMIT 失败时 ROLLBACK 掩盖原异常） |
| models.py | local-web.json 的 pydantic 模型与读写 | 有（save 非原子写；枚举/序列化整体一致，无 camelCase 错位） |
| status.py | 实例状态快照与 sync_status 观测回写 | 有（major：sync_status 报变化但不持久化的分叉；logger 命名空间错位；若干 minor） |
| config.py | local-web.yml 模型与加载 | 有（未知配置键静默忽略，属设计权衡） |
| paths.py | 工作区/实例路径解析与 ID/别名/subdir 校验 | 有（ID/别名正则 `$` 接受结尾换行，minor） |
| ports.py | 端口池分配、监听探测、LAN IP 与 URL 合成 | 有（docstring 与实现自相矛盾；TIME_WAIT 分配权衡） |
| logs.py | 日志读取（尾读）与按大小滚动 | 有（stat/open TOCTOU，minor） |
| security.py | Compose/Dockerfile/zip 审计、zip 剥离、管理页绑定策略 | 有（ADD http 大小写绕过；`./${VAR}` bind 仅 warn，uncertain） |
| logging.py | 全局日志（Rich 控制台 + RotatingFileHandler）与实例日志写入 | 有（force 重配不 close 旧 handler，minor） |
| errors.py | 统一异常层级与错误码 | 未发现明显问题 |
| file_lock.py | 跨平台文件互斥（flock/msvcrt）+ PID/心跳写入 | 未发现明显问题（锁由内核随进程死亡自动释放，无陈旧锁问题） |
| health.py | HTTP 健康探测、Gate-C 探针评估、check_health 回写 | 有（major-latent：check_health 用 manifest 陈旧 status 覆盖 registry 运行态，与 docstring 矛盾；当前 src 未接线） |
| resource_profiles.py | 资源档位 → 容器资源限制映射 | 未发现明显问题 |

---

## 2) 发现清单

### [严重度: major] status.py:262-313（关键 277-312）— sync_status 报"状态变化"但不负责持久化，registry 与 manifest 分叉时状态永久卡死
- 说明：`sync_status` 自身从不写库，仅依赖 `lifecycle.observe_status` 内部回写；而 `_observe_status_locked`（lifecycle.py:2176-2181）的回写条件是 `observed.value != manifest.status`（比较对象是 **manifest**，不是 registry）。一旦 `registry.status` 与 `manifest.status` 分叉且观测值恰等于 manifest 值，observe 不落库，但 `sync_status` 仍因 `observed.value != before`（before 取自 registry）把该实例放入 changed 返回值——每次调用都报告变化、永远不持久化。
- 触发条件：存在"只写 registry 不写 manifest"的路径，例如 build_queue 取消路径（build_queue.py:753-777 写 CANCELLED/CANCELLING 不碰 manifest）、`_recover_stale_building`（status.py:410 只写 registry=failed）、stop 流程在 manifest.save 与 registry.update_status 之间崩溃（hosting.py:126-133 两处非原子）等。实测复现：registry=building、manifest=stopped、observe 桩返回 stopped → `sync_status` 返回 `{'demo': 'stopped'}` 而 `registry.get_instance()['status']` 仍为 `building`。
- 证据：上述 python 复现脚本输出 `changed: {'demo': 'stopped'}` / `registry status after sync: building`。现有测试未覆盖 registry≠manifest 分叉场景。
- 建议修法：observe 回写比较对象改为 registry 当前 status（或让 `sync_status` 在 observe 后显式 `registry.update_status(iid, observed.value)` 当 observed≠registry status）；同时给 build_queue/`_recover_stale_building` 等只写 registry 的路径补 manifest 同步，维护"registry.status==manifest.status"不变式。

### [严重度: major] health.py:308-322（311、318-320；docstring 292-295）— check_health 用 manifest 的陈旧 status 覆盖 registry 运行态，与自身 docstring 矛盾
- 说明：成功与失败两条路径都执行 `registry.update_status(instance_id, _current_status(manifest), ...)`，即把 local-web.json 里的 status（可能落后于 registry）整体写回。docstring 明言"不直接改 status（status 由 lifecycle.observe_status 判定）"，代码却直接改写。当 manifest 陈旧（如构建已把 registry 置 running 而 manifest 尚未 save；或 _recover_stale_building 只改了 registry）时，健康检查会把 registry 状态回退（running→building/stopped），再由下一轮 observe 纠正，形成状态抖动；且每次调用都刷新 updated_at，若对 building 实例运行会不断刷新，使 status.py:399-404 的"无 builds 行兜底（updated_at 年龄>120s）"永不触发，实例可永久卡 building。
- 证据：阅读推理（代码与 docstring 直接矛盾）；grep 确认 src 内除 health.py 自身与测试外无 check_health 调用方——当前为潜在死代码中的 bug，一旦 daemon/API 复用即触发。现有测试只断言 last_health_check_at/last_error，未断言 status 不回退。
- 建议修法：check_health 只写 last_health_check_at/last_error（成功清 last_error），status 一律交给 observe_status；若必须写，用 registry 当前 status 而非 manifest.status。

### [严重度: minor] status.py:31 — logger 命名空间错位，warning 不进日志文件
- 说明：`log = logging.getLogger("lwa.status")` 不在 `local_webpage_access` 命名空间下；`setup_logging`（logging.py:45-83）只给 `local_webpage_access` logger 挂 handler 且 `propagate=False`，"lwa.status" 直接挂 root，root 无 handler → warning（_recover_stale_building/_recover_orphan_running_builds 的告警）只走 lastResort 打到 stderr，不落 lwa.log/manager.log/daemon.log。
- 证据：脚本验证 `get_logger("status").name == "local_webpage_access.status"`，而 `logging.getLogger("lwa.status")` 不是其子 logger（parent 为 root）。
- 建议修法：改用 `from local_webpage_access.logging import get_logger`，`log = get_logger("status")`。

### [严重度: minor] paths.py:17（及 20 _PATH_ALIAS_RE）— 实例 ID/别名正则 `$` 接受结尾换行
- 说明：Python re 中 `$` 匹配字符串结尾或结尾换行符之前，`^[a-z0-9]+(-[a-z0-9]+)*$` 会让 `"abc\n"` 通过校验，随后 `app_dir("abc\n")` 生成带换行的目录名（POSIX 合法），破坏命名约定并可能让 gateway/alias 片段名异常。
- 证据：实测 `re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", "abc\n")` 返回 match。
- 建议修法：正则结尾改 `\Z`（`^[a-z0-9]+(-[a-z0-9]+)*\Z`）或改用 `re.fullmatch`。

### [严重度: minor] ports.py:60-61 vs 282-284 — is_port_listening docstring 与 allocator 实现/注释自相矛盾（文档过期）
- 说明：docstring 称"分配器 allocate 仍用 is_port_in_use（要避开 TIME_WAIT）"，但 `PortAllocator.allocate` 实际用 `is_port_listening`（connect 探测），其内注释（BUG-364）与 test_ports.test_allocate_ignores_bind_only_not_listening 都确认这是有意设计。后果是：仅 bind（TIME_WAIT 残留）无监听者的端口会被判空闲并分配，随后真正 bind 时若进程未设 SO_REUSEADDR 可能 EADDRINUSE 启动失败（Docker/Caddy 通常已设，builtin Python 网关未必）。属 BUG-364 接受的权衡，但文档误导。
- 证据：代码阅读 + test_ports.py:216-229 断言"bind-only 无监听 → 可分配"。
- 建议修法：修正 is_port_listening docstring；如要更稳可在 connect 探测失败后追加一次 bind 复核（bind 失败即跳过）。

### [严重度: minor] registry/dao.py:190-201 — upsert_from_manifest 切换 container↔static 时不清理 ports 子表旧登记
- 说明：切换侧只 DELETE 对侧子表行（static_sites 或 containers），不释放 ports 表中旧 hostPort 登记；若切换后分配了新端口，旧端口行残留（直到 delete_instance 才清），端口池被幽灵端口占用、allocated_ports 失真。stop 流程有意保留端口（BUG-045 注释），但形态切换与 stop 不同。
- 证据：阅读推理；不确定点——若切换流程始终复用同一 hostPort 则无影响，需调用方确认。
- 建议修法：runtime/servingMode 切换前对旧 hostPort 显式 release，或切换流程统一先 release_instance_ports 再登记新端口。

### [严重度: minor] registry/connection.py:269-272 — init_db 失败路径未 release_connection_lock，_LOCKS 泄漏
- 说明：init_db 捕获迁移/连接异常后直接 `conn.close()`，未调 `release_connection_lock(conn)`（Registry.close 有调）；失败重试场景下 _LOCKS 条目随 id 累积（短生命周期测试场景内存有限，泄漏有限，但 id 复用后新连接会拿到旧锁条目，功能无害）。
- 证据：阅读推理。
- 建议修法：close 前 `release_connection_lock(conn)`（与 Registry.close 对齐）。

### [严重度: minor] registry/connection.py:180-184 — COMMIT 失败时 ROLLBACK 可能掩盖原始异常
- 说明：`conn.execute("COMMIT")` 抛错会进入 except 执行 `conn.execute("ROLLBACK")`；若此时已无活动事务，ROLLBACK 再抛 "cannot rollback - no transaction is active"，掩盖原始错误（磁盘 I/O 等罕见场景）。
- 证据：阅读推理。
- 建议修法：except 内 ROLLBACK 用 `contextlib.suppress(sqlite3.Error)` 包裹后再 raise 原异常。

### [严重度: minor] security.py:432 — audit_dockerfile 的 ADD 远程 URL 检测大小写可绕过
- 说明：`upper = line.upper()` 已算出，但 ADD 判断用原始大小写 `"http://" in line or "https://" in line`；Dockerfile 指令与 URL scheme 均大小写不敏感，`ADD HTTPS://...` 可绕过 critical 级 add_remote_url 检测。
- 证据：阅读推理。
- 建议修法：改为 `"HTTP://" in upper or "HTTPS://" in upper`。

### [严重度: minor] security.py:284-302（配合 334-371）— `./${VAR}` 形式 bind source 仅判 warn（不确定）
- 说明：BUG-184 只对 src 以 `~`/`$` 开头做敏感片段检测；`./${HOME}`、`./${DATA}` 等变量在中间/前缀带点的相对路径按普通相对路径处理，展开后指向宿主敏感目录也最多 warn（如 `./${HOME}` → /root 只 warn 不 critical）。静态无法展开属已知权衡（注释已说明），但确为绕过面。
- 证据：阅读推理；不确定点——compose 渲染后为宿主路径，实际风险取决于调用方是否允许 warn 级通过。
- 建议修法：相对路径中含 `$`/`~` 的统一按"不可静态展开"处理并至少 warn，敏感名片段命中升 critical。

### [严重度: minor] config.py:144-236 — 未知配置键静默忽略（设计权衡）
- 说明：Config 未设置 extra 策略（pydantic v2 默认 ignore），YAML 拼写错误（如 `portpool:` 写成 `portPool:` 的反面/少字母）被静默丢弃并回退默认值，配置与预期不符时难以发现。
- 证据：阅读推理。
- 建议修法：`model_config = ConfigDict(extra="forbid")`，或加载时对未知键记录 warning（保留向后兼容可先 warn）。

### [严重度: minor] logs.py:96-100 — tail_text_file 的 stat 与 open 之间 TOCTOU
- 说明：先 `path.stat().st_size` 再 `path.open("rb")`，文件在两步间被删除/滚动时 stat 抛 FileNotFoundError 未捕获，read_log 本应返回空串却抛异常。
- 证据：阅读推理；概率低（写日志并发场景）。
- 建议修法：`try/except OSError: return ""` 包裹 stat/open。

### [严重度: minor] registry/dao.py:139-152、203-214 — 动态列名拼接 SQL + 空 dict 生成非法 SQL（防御性提示）
- 说明：`upsert_instance`/`_upsert_mapping` 用 `row.keys()` 拼列名，列名不可参数化。当前调用方均为内部构造的固定键 dict（grep 确认 src 内 upsert_instance 无生产调用方，仅文档示例），非用户输入，无注入面；但空 dict 会生成 `INSERT INTO instances () VALUES ()` 语法错误。
- 证据：阅读推理。
- 建议修法：入口断言 row 非空且键是白名单列；或弃用通用拼接改为显式列清单。

### [严重度: minor] models.py:679-686 — InstanceManifest.save 非原子写（设计权衡）
- 说明：直接 `write_text` 覆盖写，进程中途崩溃可能留下损坏的 local-web.json，后续 load 抛 SchemaError 导致实例不可读。
- 证据：阅读推理。
- 建议修法：写临时文件 + `os.replace` 原子替换。

### [严重度: minor] registry/dao.py:432-456 — list_route_hosts 别名唯一性为"先校验后写入"的 TOCTOU（不确定）
- 说明：全局唯一性靠调用方 `validate_path_alias`（读快照）+ 随后 upsert 两步完成，非原子；两个并发请求可通过校验后先后写同一 route_host，list_route_hosts 的 dict 后者覆盖前者。与 ports.allocate 不同（后者有 INSERT OR IGNORE+归属校验兜底），alias 登记无等价机制。
- 证据：阅读推理；不确定点——写路径是否全程持实例锁（path_alias.py 在实例锁内更新，跨实例并发仍需验证）。
- 建议修法：route_host 列加唯一约束，或登记时用 INSERT OR IGNORE + 冲突重试。

### [严重度: minor] status.py:589、617-621 — persisted_lan 用 urlparse.hostname 与 current_ip 直接比较，IPv6 zone/主机名会误判 stale（不确定）
- 说明：`persisted_lan = urlparse(net.lanUrl).hostname` 未做规范化（对比 ports._normalize_ip）：落盘 lanUrl 为带 zone 的 IPv6（`fe80::1%eth0`）或主机名（非 IP）时与 current_ip 恒不等 → 恒 stale=True；IPv4-mapped IPv6（`::ffff:1.2.3.4`）与 `1.2.3.4` 比较也不等。
- 证据：阅读推理；现有测试仅覆盖纯 IPv4。
- 建议修法：比较前用 `ports._normalize_ip` 归一化两侧。

### [严重度: minor] status.py:419-427 — _age_seconds 对 naive/aware 时间戳混合（不确定）
- 说明：`datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()` 对单个时间戳自洽；但若库中混存带时区（now_iso 产物）与不带时区的时间戳（如测试/旧数据 "2020-01-01T00:00:00"），不同实例间年龄比较口径不一致（本地时区偏移如 +08:00 造成偏差）。生产写入均为 now_iso 带时区，风险低。
- 证据：阅读推理。
- 建议修法：统一约定所有时间戳带时区，或 _age_seconds 一律按 naive 处理并记录。

### [严重度: minor] logging.py:51-53 — setup_logging(force=True) 移除旧 handler 不 close（设计权衡）
- 说明：force 重配时只 removeHandler 不 close 旧 RotatingFileHandler，长驻进程反复 force 配置会累积打开的文件描述符。
- 证据：阅读推理。
- 建议修法：remove 后 `handler.close()`（测试 test_setup_logging_uses_rotating_file_handler 已有 close 先例）。

---

## 3) 无问题文件

- **errors.py**：未发现明显问题（DataNonemptyError 的 code 小写是文档声明的稳定错误码，非 bug）。
- **file_lock.py**：未发现明显问题（POSIX flock / Windows msvcrt 锁随进程退出由内核自动释放，不存在陈旧锁判定缺口；write_lock_payload 的 PID/心跳仅供 daemon._lock_is_stale 诊断，锁互斥本身可靠）。
- **resource_profiles.py**：未发现明显问题（未知档位回退 SMALL，行为符合文档）。

其余文件（models.py 枚举/序列化、dao 并发安全端口登记、connection 迁移、security zip slip/符号链接检测、logs 滚动链、config 校验、ports URL 合成）经代码阅读与针对性测试（test_registry / test_models / test_ports / test_logs / test_config / test_security / test_status_lan_freshness 全绿；test_paths 仅环境性失败）未发现额外缺陷。

---

### 严重度小结
- critical：0
- major：2（status.py sync_status 分叉不持久化；health.py check_health 状态覆盖——后者当前 src 未接线，属潜在）
- minor：15（其中 4 条标注"不确定"，2 条为设计权衡/防御性提示）
