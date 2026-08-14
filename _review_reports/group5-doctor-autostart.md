# 代码审阅报告：doctor / autostart / capability / setup（组 5）

审阅范围：`src/local_webpage_access/{doctor,autostart,capability,setup}.py` 全文逐行阅读（doctor 1754 行、autostart 1853 行、capability 889 行、setup 444 行），并交叉核对 `static_gateway.py`、`gateway_service.py`、`daemon.py`、`paths.py`、`config.py`、`ports.py`、`docker_runtime.py`、`registry/dao.py`、`hosting.py`、`platform_detect.py`、`version_requirements.py` 及五个测试文件。疑点用本机（Windows + Python 3.13）实测验证（`os.getuid` 可用性、`MacLaunchdBackend._domain()` 调用链、test_capability.py / test_autostart.py 单文件运行）。未修改/未创建任何源码文件。

## 1) 每个文件一行总结

- **doctor.py**（环境/实例只读诊断聚合）：结构严谨、fail-closed 意识强（BUG-230/331/426/427/428/430 均有显式处理）；有 6 条 minor 级疑点（端口探测口径漂移、runner 异常面不全、MemAvailable 误报、实例诊断写库副作用、SQLite mount 漏报、入口探测只探首别名）。
- **autostart.py**（macOS launchd / Linux-WSL systemd 前台监管单元生成与生命周期）：整体设计成熟、回归测试覆盖深（BUG-138~168、338/384/389）；有 4 条 minor 级疑点（os.getuid 无本地守卫、systemd 单元读侧不反转义、WSL 保活脚本内插与 systemctl --user 失效风险、registry 异常 fail-open）。
- **capability.py**（CapabilityReport 能力快照与 role 合并状态机）：状态机契约与缓存新鲜度/存活校验（BUG-253/256/257/258/284）处理细致；无真实 bug，有 2 条观察项（default 档 overall 对 daemon_unavailable 返回 ready 的设计权衡、探测路径含写日志文件副作用）。
- **setup.py**（`lwa setup` 环境检测与安装指引）：无问题——detect_platform 模块级重导出兼容 monkeypatch、caddy optional 语义、node 版本解析、平台分支文案均正确。

## 2) 发现清单

### doctor.py

**[minor] doctor.py:351-403（探测实现 154-161）check_port_pool 与端口分配器探测口径漂移（TIME_WAIT 误报 FAIL）**
- 说明：`check_port_pool` 经 `_default_port_in_use` → `ports.is_port_in_use`（独占 bind 探测，bind 失败即判占用，含 TIME_WAIT）；而 `PortAllocator.allocate` 自 BUG-364 起改用 `is_port_listening`（connect 探测，明确不把 TIME_WAIT 当占用）。docstring 声称"保证 doctor 与分配器口径一致"（BUG-029），但 BUG-364 改分配器后两者已漂移：刚停止的实例端口处于 TIME_WAIT（约 60–240s）期间，`lwa doctor` 会报 `port_pool` FAIL"外部占用"，而分配器认为该端口可复用。
- 证据：ports.py:281-286 分配器用 `is_port_listening`（注释 BUG-364"把 TIME_WAIT 误判为占用…以是否有活跃监听者为准"）；ports.py:31-46 `is_port_in_use` 为独占 bind 语义；doctor.py:154-161 注释仍称"与分配器口径一致"。
- 建议修法：`_default_port_in_use` 改委托 `is_port_listening(port)`（或按检查语义新增参数），并同步 docstring；保持与分配器 BUG-364 语义一致。

**[minor] doctor.py:133-151 `_default_runner` 异常面不全 → run_doctor 崩溃**
- 说明：`subprocess.run` 在可执行文件存在但不可执行/被替换为损坏文件时抛 `PermissionError`（非 FileNotFoundError），`_default_runner` 只捕获 FileNotFoundError 与 TimeoutExpired；`check_docker/check_docker_compose/check_caddy/check_caddy_health` 直接调用 runner 无 try/except，异常会穿透 `run_doctor` 使整个诊断崩溃而非产出 CheckResult。
- 证据：doctor.py:133-151 捕获清单；doctor.py:183-224 等调用点无保护；测试 `_failing_runner` 只模拟 returncode=127。
- 建议修法：`_default_runner` 增加 `except OSError`（含 PermissionError）统一映射为非零 CompletedProcess，与 FileNotFoundError 同路径处理。

**[minor] doctor.py:1348-1369 check_memory `/proc/meminfo` MemAvailable 缺失 → 误报 FAIL**
- 说明：内核 < 3.14（或部分精简容器/老 WSL1）无 `MemAvailable` 行，`info.get("MemAvailable", 0)` 得 0 → `avail_gb = 0` → 判 FAIL"可用内存仅 0.00 GB"，属假阴性误报；psutil 缺失且 Linux 时必走此分支。
- 证据：doctor.py:1354 `avail = info.get("MemAvailable", 0)`；无降级为 MemFree 的逻辑。
- 建议修法：MemAvailable 缺失时回退 `MemFree`，仍缺失则 SKIP 而非 FAIL。

**[minor] doctor.py:1666-1674 实例诊断路径 `reg.open()` 可创建 registry DB（违背"只读"承诺）**
- 说明：`run_doctor(instance_id=...)` 对实例诊断用 `Registry(ws.db_path).open()`（读写打开），而 `dao.open()` 内部 `init_db()` 在库文件缺失时会**创建**空库；对照 `_allocated_ports_for_workspace` 有 `is_file` 守卫、`check_registry`/`caddy_probe_registry` 均走 `open_readonly`（缺失即报错，不创建，BUG-331）。触发条件：registry 库被删但实例目录仍在时执行 `lwa doctor <id>`。
- 证据：dao.py:60-63 `open()` → `init_db(self.db_path)`；dao.py:65-93 `open_readonly` 显式"不创建文件、不迁移 schema（BUG-331）"；doctor.py:1686-1697 与 1598-1602 的守卫对比。
- 建议修法：实例诊断改为 `open_readonly()`（诊断只读语义），或在打开前先 `is_file()` 校验并明确报 FAIL。

**[minor] doctor.py:997-1010 SQLite data mount 整体缺失不告警（漏报）**
- 说明：挂载漂移只对"已观测到的 managed mounts"（destination 命中 `container_data_paths`）比对 source；若 SQLite 实例的 data bind mount 整体不存在（volume 丢失/未挂载），`managed=[]` → 无 finding，该项静默 OK。
- 证据：doctor.py:1001-1010 只遍历 `managed`；无"期望 mount 缺失"计数。
- 建议修法：对每个 sqlite 实例，若 `destinations` 非空而匹配到的 mount 为空，追加一条 finding 或 SKIP 注记。

**[minor] doctor.py:568-579 check_caddy_health 入口探测只探首个别名（漏报边界）**
- 说明：`probe_alias = next(iter(aliases))` 仅对字典第一个 route_host 探测 `/<alias>/`；若入口对部分别名 404（reload 未生效/片段缺失）而首个别名恰好正常，漏报。为探测成本权衡，但属"宁可漏报"方向。
- 证据：doctor.py:571-579；`list_route_hosts` 返回全部 route_mode='name' 别名（dao.py:432-456）。
- 建议修法：遍历全部（或抽样）别名探测，任一不可达即记 entry_unreachable；别名多时可用数量上限截断。

**[观察] doctor.py:301-309 check_caddy 空版本消息**：`caddy version` 无输出时 version=""，`version_ge("", min)` 恒 False → FAIL 消息 "Caddy  不满足最低要求 ≥ …"（双空格、无实际版本号），仅消息质量。建议版本为空时直接报"无法解析 Caddy 版本"。

**[观察] doctor.py:1227-1243 check_port_contention caddy.pid 缺失 + admin 在线判 FAIL**：本工作区 master 正常监听 :2019 但 pid 文件缺失/被删时，全部监听者视为"非 self"且 admin alive → FAIL"存在非本工作区 caddy.pid 记录的监听者"。判定方向正确（pid 缺失本身是异常），但消息易被误解为外部占用。属设计权衡。

### autostart.py

**[minor] autostart.py:351-352 `_domain()` 使用 `os.getuid`，无本地平台守卫（生产有传递守卫，直接构造/测试路径会 AttributeError）**
- 说明：`os.getuid` 仅 Unix 存在。实测本机（Windows，`hasattr(os,'getuid')==False`）：`MacLaunchdBackend()._domain()` 直接 `AttributeError: module 'os' has no attribute 'getuid'`。生产路径有传递守卫——`select_backend()` 仅在 `detect_platform()==macos` 时才返回 MacLaunchdBackend，Windows 上抛 AutostartError，且 `coordinated_disable/coordinated_restart/is_service_loaded/run_check` 均捕获 AutostartError（实测 `is_service_loaded('daemon')` 在 Windows 返回 False，不崩）。因此**运行时不会在 Windows 上经正常路径触发**；但守卫在"构造处"而非"调用处"，任何直接构造 MacLaunchdBackend 或未来绕过 select_backend 的新调用都会 AttributeError，mypy 报 Windows 缺失正确。另注：本机跑 test_autostart.py 的首个失败是 `init_workspace → maybe_start_manager` 的 Windows 门禁（RuntimeError"Windows 原生不受支持"），非 os.getuid——该测试套件本面向 macOS/Linux/WSL 运行。
- 证据：实测输出；autostart.py:581-596 `select_backend` 平台门禁；autostart.py:1016-1027、1060-1075、991-998 的 AutostartError 捕获。
- 建议修法：`_domain()` 内 `getattr(os, "getuid", lambda: 0)()` 兜底，或类属性显式声明平台前置；至少消除 mypy 噪音并防止未来直接调用崩溃。

**[minor] autostart.py:218/219/224（写侧） vs 1282-1349/1471-1492（读侧）systemd 单元 % 转义只做了写侧，读侧不反转义 → 含 % 路径下 check 误报 FAIL**
- 说明：`build_systemd_unit` 把 ExecStart/WorkingDirectory/Environment 中的 `%` 转成 `%%`（BUG-338，正确）；但 `_extract_workspace_from_unit`/`_unit_python`/`_unit_path_env`/`_grep_exec_start` 读取单元文件时不反转义。工作区路径含 `%`（如 `~/ws%prod`）时：check 的"单元工作区(ws%%prod) ≠ 当前(ws%prod)"或"PATH 目录均不存在/无法解析 caddy"会**误报 FAIL**（path 值同样存 `%%`）。测试 `test_build_systemd_unit_escapes_percent_and_quotes` 只断言写侧。
- 证据：autostart.py:218 `replace("%", "%%")`；autostart.py:1282-1304 直接 `shlex.split` 取回含 `%%` 的 workspace 值并与 `ws.root` 比较；autostart.py:1485-1489 读取 PATH 不还原 `%%`/`\"`。
- 建议修法：读侧对 `%%`→`%`（systemd 规范语义）与 `\"`→`"` 做反转义后再比较/校验；或比较时按 systemd 解码规则归一化。

**[minor] autostart.py:1175-1192 render_wsl_windows_script：发行版名内插 + `systemctl --user` 失败被吞（不确定，WSL 环境相关）**
- 说明：(a) `$distro = "{distro}"` 把发行版名直接内插进 PowerShell 双引号字符串，含 `"` 的发行版名会破坏脚本（本地可控，注入风险低）；(b) Windows 登录任务（Task Scheduler 非交互）触发的 `wsl.exe … bash -lc 'systemctl --user start …'` 可能因用户会话/`XDG_RUNTIME_DIR`/user bus 未就绪而失败，错误被 `2>/dev/null` 吞掉，脚本仅"保活"发行版而实际未启动 lwa 服务——功能缺口。后者依赖具体 WSL/systemd 配置，标注不确定。
- 证据：autostart.py:1189-1191；docstring 自述"登录任务…运行本工具"。
- 建议修法：(a) distro 名做引号转义或用单引号/参数化；(b) 脚本内 `export XDG_RUNTIME_DIR=/run/user/$(id -u)` 并显式 `loginctl enable-linger` 前置检查，失败时把错误写入日志而非静默。

**[minor] autostart.py:1698-1715 `_check_docker` registry 异常 fail-open**
- 说明：`reg.open()` 或 `list_instances` 抛异常时返回 `STATUS_OK` "无 registry，跳过"——DB 损坏/被锁时 docker 子检查假装通过，与项目其他处 fail-closed 基调（如 doctor BUG-427/430）不一致。属 warn 级检查，影响有限。
- 证据：autostart.py:1714-1715 `except Exception: return CheckItem(..., STATUS_OK, "无 registry，跳过")`。
- 建议修法：区分"库文件确实不存在"（OK/跳过）与"存在但读取失败"（WARN"registry 读取失败，Docker 检查未完成"）。

**[观察] autostart.py:1042-1057 coordinated_disable 中 `backend.disable` 调用未包 try/except**：is_loaded/is_enabled 有异常兜底，但 disable 自身异常会外抛；CLI 层 `coordinated_autostart_disable` 已有 try/except（BUG-154 测试覆盖），生产安全。属防御性完善点，非 bug。

### capability.py

**[观察] capability.py:777-786 default 档 overall 对 `daemon_unavailable` 返回 ready（设计权衡，与 full 档不一致）**
- 说明：default profile 的 `_compute_overall` 仅 `permission_denied`/`session_refresh_required` 判 degraded，其余（含 `daemon_unavailable`、`unavailable`）一律 ready；而 full 档把 `daemon_unavailable` 列为 hard_fail → unready。docker 守护进程停摆时 default 档仍报 ready，与用户直觉冲突，但注释声明为"default 宽松、由调用方决定"的设计。标注为设计权衡而非 bug。
- 证据：capability.py:778-786 vs 824-838；测试未覆盖该分支语义。

**[观察] capability.py:209-281 + static_gateway.py:1226-1232 能力探测含写副作用**：`probe_caddy_runtime_fields → verify_workspace_caddy_access` 会 `mkdir logs/` 并创建 `static-access.log`（"只读探测"产生文件）。该文件本由 Caddy 按需创建，且 doctor 的 BUG-428 豁免逻辑兼容（文件存在与否都不告警），仅违背"探测无副作用"直觉；若磁盘只读则返回 write_denied（fail-closed 方向正确）。

**[观察] capability.py:565-572 gateway 缓存合并可与实时 caddy_runtime 矛盾**：gateway 缓存只合并 `gatewayAccess`，不覆盖实时 `caddy*` 字段（BUG-253 有意设计，测试 `test_live_caddy_not_overwritten_even_when_gateway_cache_alive` 显式断言 gatewayAccess=ready 与 caddy_runtime=admin_unavailable 并存）；overall 由实时字段兜底，不会假绿。属设计权衡。

**[观察] capability.py:235-242 workspace_access 未知值强转 `workspace_access_denied`**：`verify_workspace_caddy_access` 当前只返回 None/read_denied/write_denied，白名单全覆盖；强转分支为防御性代码，无实际触发路径。

### setup.py

**未发现明显问题。** 逐项核对：`detect_platform` 模块级重导出（setup.py:84）保证 monkeypatch 生效（tests 依赖此）；`_from_doctor_check` 透传 runner/config；caddy 项 `optional=static_gateway != "caddy"` 与"缺失降级 builtin 不阻断"语义一致；`_check_node` 的 `stdout or stderr` 与 `version_ge("v24.x", …)` 解析正确；`has_failures` 只计非 optional FAIL，与测试 `test_run_setup_supported_compose_v2_is_ready_with_warning` 等吻合；`render_setup_script` 平台分支与 `_SCRIPT_*` 模板无注入面（纯静态文案）。

## 3) 无问题文件

- **setup.py：未发现明显问题。**
- 其余三个文件的问题见上，均为 minor/观察级；未发现 critical 或 major 级缺陷（最接近 major 的是 doctor 端口探测口径漂移与 autostart 读侧 % 反转义缺失，但触发条件窄、且方向均为"保守误报/窄场景"，故定 minor）。

## 4) 环境备注（实测）

- 本机为 Windows + Python 3.13：`os.getuid` 不存在（已实测），印证 autostart.py:352 的 mypy 告警；生产路径有 `select_backend` 传递守卫，正常运行不会触发 AttributeError。
- test_autostart.py 在本机首个失败为 `init_workspace → maybe_start_manager` 的 Windows 门禁（RuntimeError），非 os.getuid——测试套件面向 macOS/Linux/WSL。
- test_capability.py 在本机仅 `test_write_capability_cache_uses_atomic_replace` 失败，原因是 Windows 上 `chmod(0o600)` 不产生 POSIX 权限位（st_mode 断言 33206 & 511 != 384），属测试宿主语义差异，非代码 bug。
