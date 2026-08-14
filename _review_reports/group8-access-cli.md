# LWA 代码审阅报告 — Group 8（access / path_alias / access_workflow / pageviews / stats / directory_picker / cli 全部）

审阅范围：src\local_webpage_access\ 下 access.py、path_alias.py、access_workflow.py、pageviews.py、stats.py、directory_picker.py 及 cli\ 全部 15 个文件。全部文件已完整通读（无跳读）。
验证方式：仅静态阅读 + 运行相关单测文件（test_access / test_pageviews / test_path_alias_spa_guard / test_access_workflow / test_stats / test_directory_picker / test_cli_validation / test_cli_registry）。**未运行全量套件**（父进程正在跑）。

## 0) 测试运行结论（环境说明）

- tests\test_access.py（43 项）全部通过。
- tests\test_path_alias_spa_guard.py、test_access_workflow.py、test_stats.py 全部通过。
- tests\test_pageviews.py 有 3 项失败：`test_pageview_timezone_uses_local_day_and_utc_ordering`（Windows 无 `time.tzset()`，POSIX-only 测试）、`test_read_new_lines_legacy_cursor_does_not_reread_preexisting_archives` 与 `test_read_new_lines_does_not_mark_unreadable_archive_consumed`（Windows 上 `Path.write_text` 把 `\n` 写成 `\r\n`，文件字节数与测试内 `len(content.encode())` 不一致导致断言失败）。三者均为 **Windows 测试环境伪影**，非源码缺陷（产品目标平台为 Linux/WSL）。
- tests\test_cli_registry.py / test_cli_validation.py 多处 ERROR：`init_workspace` 在 Windows 上拒绝拉起 manager 子进程（IMP-036 不支持 Windows 原生），属 POSIX-only 测试，非源码缺陷。
- tests\test_directory_picker.py 1 项失败：`Path('/Users/me/my-site').is_absolute()` 在 Windows 上恒 False（POSIX 路径在 Windows 不算绝对），测试平台伪影。

结论：**本次未通过测试发现任何源码级 bug**；下列发现均来自静态阅读推理，按疑似度标注。

---

## 1) 每个文件一行总结

| 文件 | 职责 | 疑点 |
|---|---|---|
| access.py | 访问地址刷新（G1）与真探活复核（G2/G5/G6，IMP-023/IMP-055/API 错位检测） | 有（若干边界项，多为无害/不确定） |
| path_alias.py | 路径别名设置/清除（锁、IMP-023 守卫、Caddy 片段同步） | 有（1 条可复现崩溃路径 + 死代码） |
| access_workflow.py | refresh/review 共享编排 + 节流单飞（IMP-038/040） | 未发现明显问题 |
| pageviews.py | 浏览量摄入（CLF/Caddy JSON/容器日志、轮转归档补读、去重、SQLite 聚合） | 有（3 条边界问题 + 2 条不确定项） |
| stats.py | 整机/实例资源采集（WBS-19） | 未发现明显问题（1 条防御性不足） |
| directory_picker.py | 宿主机原生目录选择器（IMP-051） | 未发现明显问题 |
| cli\__init__.py | 根入口、全局回调、init 命令 | 未发现明显问题 |
| cli\__main__.py | `-m local_webpage_access.cli` 入口委托 | 未发现明显问题 |
| cli\_common.py | 日志/工作区/格式化/自启动协调工具 | 未发现明显问题 |
| cli\system.py | setup / doctor / capabilities / update | 未发现明显问题 |
| cli\importing.py | import / scan（含 --from-dir / --update） | 未发现明显问题 |
| cli\lifecycle.py | start/stop/restart/recover/rebuild/cancel-build/remove/logs | 未发现明显问题 |
| cli\gateway.py | gateway on/off/status/switch | 有（1 条注释与行为不符，无害） |
| cli\autostart.py | autostart 子命令组 | 未发现明显问题 |
| cli\workspace.py | workspace relocate（IMP-042） | 未发现明显问题 |
| cli\manager.py | manager on/off/status/token/start/logs | 未发现明显问题 |
| cli\status.py | status / stats / list / pageviews | 有（1 条退出码不一致，低影响） |
| cli\registry.py | registry check/repair（BUG-473） | 未发现明显问题 |
| cli\access.py | access refresh / review | 未发现明显问题 |
| cli\daemon.py | daemon on/off/status | 未发现明显问题 |
| cli\alias.py | alias set / clear | 未发现明显问题（经手触发 path_alias #1 疑点） |

---

## 2) 发现清单

### [minor] path_alias.py:199-214 — `manifest.network` 为 None 时 `_apply_manifest_alias` 抛 AttributeError
- **说明**：`_resolve_host_port` 返回 `host_port=None`（实例无任何 hostPort）时走 else 分支，直接 `manifest.network.model_copy(...)`；若 `manifest.network is None`（最小化 manifest，如 tests/test_access.py 中 `test_refresh_skips_instance_without_hostport` 构造的无 network 实例），抛 `AttributeError: 'NoneType' object has no attribute 'model_copy'`。该异常不是 `LwaError`，CLI（alias.py）只 catch `LwaError`，会打出裸 traceback。
- **触发条件**：`lwa alias set/clear` 作用于「无 hostPort 且无 network 段」的实例（runtime 为 shared-static/docker-compose、无端口登记的边缘实例）。
- **证据**：代码阅读；else 分支两条路径（alias=None 与 alias≠None）都假设 `manifest.network` 非空，而前文 `host_port is None` 分支无任何初始化保障。
- **建议修法**：`manifest.network = (manifest.network or NetworkConfig()).model_copy(update=...)`，或先校验 `manifest.network is None` 时构造默认 `NetworkConfig()`。

### [minor] pageviews.py:1050-1210 — 日志被原地截断且无轮转归档时游标不重置，后续命中永久漏计
- **说明**：docstring 声称「文件不存在或被截断（体积小于游标）则重置游标」，但实现只在「存在未消费归档（`recent_unconsumed` 非空）」时才触发 catchup/重置（第 1124-1155 行分支均要求 `recent_unconsumed` 非空）。若日志被外部工具（如 logrotate copytruncate）原地截断、无归档产生，`fh.seek(offset)` 越过 EOF 返回空批、`next_offset` 保持旧值；新内容 [0..offset) 永远不被摄入，且待文件重新长过 offset 后从旧 offset 续读，可能从行中开始并跳过新文件开头。
- **触发条件**：builtin 的 `gateway.log` 或 Caddy `static-access.log` 被无归档方式截断（copytruncate 等）。
- **证据**：`_read_new_lines` 全路径推演：`recent_unconsumed` 为空时所有 catchup 分支均为 False，直接 `seek(offset)`。
- **建议修法**：在 `size < offset` 且无可读归档时，把 `offset` 重置为 0（配合现有半行保护），或至少记录 WARN 日志提示游标已失效。

### [minor] pageviews.py:1422-1434 — 别名根路径带 query 时按前缀归属失败
- **说明**：`if hit.path == pfx or hit.path.startswith(pfx + "/")` 比较的是含 query 的完整 uri。`uri="/demo?x=1"` 既不等于 `/demo` 也不以 `/demo/` 开头 → 不归属任何实例（除非该实例还有端口匹配），该次访问被丢弃。
- **触发条件**：浏览器/客户端请求别名根不带尾斜杠且带 query（`/demo?x=1`）；Caddy 通常把 `/demo` 重定向为 `/demo/`，故为低概率场景。
- **证据**：代码阅读；`parse_caddy_json_line` 的 `path` 字段直接取 `request.uri`（含 query）。
- **建议修法**：比较前先 `path.split("?", 1)[0]` 剥 query，或在 `hit.path == pfx` 处用 `startswith(pfx + "?")` 兜底。

### [minor] pageviews.py:890-897 — `_delete_instance_rows` 的 LIKE 通配符未转义，可能误删兄弟实例游标
- **说明**：`DELETE FROM ingest_cursor WHERE ... OR source_key LIKE 'builtin:{instance_id}:%'` —— 若 instance_id 含 SQL LIKE 通配符（`_`、`%`），如 `demo_1`，`LIKE 'builtin:demo_1:%'` 会匹配 `builtin:demoX1:...` 等兄弟实例的游标（`_` 匹配任意单字符）。实例 ID 由导入 zip/目录名派生 slug，可含下划线。
- **触发条件**：删除实例时另一个实例 ID 与该 ID 仅在 `_`/`%` 位置不同（如 `demo_1` vs `demoX1`）。
- **证据**：代码阅读；该函数本身就是为了避免「demo vs demo-2」前缀误伤而改为精确 key + 精确前缀，但 LIKE 段仍存在通配符漏洞。
- **建议修法**：用 `ESCAPE` 转义 `_`/`%`（`LIKE ? ESCAPE '\'`，并把 `\`、`_`、`%` 转义），或改为先查全表再在 Python 侧按 `startswith(f"builtin:{instance_id}:")` 过滤删除。

### [minor] access.py:1110-1112 — `staticGatewayPort=None` 时仍拼接 `:None` 端口 URL 探测
- **说明**：`_review_instance` 在 route 分支内无条件执行 `entry_html = _fetch_text(f"http://127.0.0.1:{config.staticGatewayPort}/{path_alias}/")`；若 `staticGatewayPort` 为 None，生成 `http://127.0.0.1:None/alias/`。`_fetch_text` 的兜底 except 会吞掉异常返回 None，随后 `_check_api_paths` 自身也因 entry_port None 提前 return，故**无实际危害**，仅属死路径/可读性问题。
- **证据**：代码阅读（f-string 把 None 渲染为 "None"）。
- **建议修法**：先 `if config.staticGatewayPort is not None:` 再 fetch，或让 `_fetch_text` 前的判断与 `_check_api_paths` 一致。

### [minor] cli\gateway.py:103-110 — `gateway_on` 注释与行为不符：`run_access_pass(review=True)` 仍会再执行一次 refresh
- **说明**：注释声称「finalize 已 refresh；此处仅 review 避免重复写」，但 `run_access_pass(..., review=True, dry_run=False)` 的语义是「先 refresh 再 review」，refresh_network_entries 会被再次执行（幂等、写盘）。行为无害（刷新幂等），但注释误导，与「避免重复写」的意图矛盾。
- **证据**：access_workflow.py:104-117 `run_access_pass` 中 refresh 无条件先跑。
- **建议修法**：改用仅 review 的入口（或给 run_access_pass 加 `skip_refresh` 参数），并同步修正注释。

### [minor] pageviews.py:204-229 — Caddy `ts` 非数值时回退 `now_iso()`，导入延迟导致时间戳漂移
- **说明**：`iso_ts = _unix_to_iso(ts) if isinstance(ts, (int, float)) else now_iso()`。若 Caddy 配置输出字符串时间戳（或其他编码），摄入时统一用「当前时刻」代替真实请求时刻，批量补读历史日志时按天分桶可能全部落入「今天」。
- **触发条件**：Caddy 日志 ts 字段非数值；或归档补读时（轮转归档内 ts 为数值则不受影响）。
- **证据**：代码阅读（`now_iso()` 兜底）。
- **建议修法**：尽力解析字符串 RFC3339 后再回退 now_iso；或至少对非数值 ts 记 DEBUG 便于排查。

### [minor] access.py:700-707 — 相对脚本路径 `../x.js` 被规范化为 `/../x.js` 探测
- **说明**：`_normalize_script_src` 只处理 `./` 前缀；`../assets/x.js` 会变成 `/../assets/x.js`，作为 bundle fetch URL 探测（大概率 404/被服务端归一化），被 `_fetch_javascript` 静默吞掉，仅导致该 bundle 的 API 路径抽不到，不崩溃。
- **触发条件**：入口 HTML 的 `<script src="../...">` 相对路径写法。
- **证据**：代码阅读。
- **建议修法**：对 `../` 前缀做 URL join 归一化（基于入口 URL 目录解析），或直接跳过此类 src。

### [minor] cli\status.py:162-164 — `pageviews --limit` 越界退出码为 1 而非 2
- **说明**：`if limit < 1 or limit > 500: raise typer.Exit(code=1)` —— 参数校验类错误其余 CLI 均用 exit 2（如 init 的互斥参数、doctor 的 --profile），此处用 1（运行时错误语义），不一致。
- **证据**：与 cli\__init__.py:128、cli\system.py:100、cli\importing.py:79 等对比。
- **建议修法**：改为 `typer.Exit(code=2)`（或使用 typer 参数级校验）。

### [minor] stats.py:336-358 — `_parse_size` 对畸形数字抛 ValueError（依赖调用方兜底）
- **说明**：`num = float(m.group(1))` 在 try 之外；`"1.5.2MiB"` 这类畸形值会抛 ValueError。当前唯一调用方 `_parse_mem_usage` 有 `except Exception` 兜底，故不会实际崩溃，但函数本身不健壮、无法独立复用。
- **证据**：代码阅读 + tests\test_stats.py 仅覆盖正常值。
- **建议修法**：把 `float(m.group(1))` 移入 try/except 返回 None。

### [minor/不确定] pageviews.py:1157-1194 — 截断+轮转复合场景下，非 pivot 归档被全量重读可能双计
- **说明**：offset>0 的 catchup 中，非 pivot 归档一律 `_decode_archive_chunk(raw)` 全量读入。若存在「比 pivot 更旧、且未压缩长度 < offset 的未消费归档」（由外部截断后再轮转产生），其内容本应已被游标消费（[0..len) ⊂ [0..offset)），全量重读会造成已消费前缀双计。极罕见（需外部截断 + 归档并存），测试未覆盖该组合。
- **证据**：代码阅读；tests 覆盖了 pivot 选择（最旧优先）、不可读延后等，但未覆盖「旧归档短于 offset 且存在可读 pivot」组合。
- **建议修法**：对非 pivot 归档，若 `len(raw) < offset` 视为历史已消费（仅记指纹不读内容），与 line 1136-1155 分支的 migrated 语义对齐。

### [minor/不确定] access.py:1074-1081 — LAN IP 探测失败（lan_ip=None）时非 loopback lanUrl 一律标 stale
- **说明**：`if lan_ip and lan_host and lan_host not in (lan_ip, "127.0.0.1")` —— lan_ip 为 None 时条件短路不标 stale（我复核了：`lan_ip and ...` 中 lan_ip=None 为 False，整表达式为 False，**不会**标 stale）。此条撤销：无问题。（保留说明：原疑点经复核不成立，见下）
- **证据**：条件 `lan_ip and lan_host ...` 在 lan_ip=None 时短路为 False。
- **结论**：误报撤销，不计入发现。

### [minor/不确定] access.py:536-574 / 874-877 — 探测 URL 追加 `__lwa_probe=1` 可能改变严格 API 端点的响应
- **说明**：`_http_get`/`_check_api_paths` 的 API 探测（`/api/v1/members` 等）经 `mark_probe_url` 追加 query 参数。FastAPI 等忽略未知参数，但若后端对未知 query 严格校验（400），会导致「绝对路径本可用却被判 mismatch」的假阳性。属低概率、防御性关注点。
- **证据**：probe.py:27-41 `mark_probe_url` 实现 + `_check_api_paths` 调用链。
- **建议修法**：对 API 端点探测可考虑不加 probe 标记（或仅对页面/资源探测加标记）。

### 无问题/设计权衡说明（非发现）
- access.py `_http_get` 跟随 urllib 默认重定向、读全 body、连接级失败不判 IMP-023（BUG-399）——符合测试矩阵，行为正确。
- pageviews.py 轮转补读的 pivot/指纹/迁移态逻辑经 8 个专项测试覆盖，未发现可复现缺陷。
- access_workflow.py `_lan_ip_cache`/`_inflight` 全局变量无锁读写——GIL 下可接受，最多重复计算一次，非 bug。
- 死代码（非 bug）：path_alias.py:307 `_alias_lock_is_stale`（未使用）；pageviews.py:899 `filter_new_container_lines`（未使用）；access.py:1145 `_check_subresources` 的 `route_probe` 参数（未使用）。
- directory_picker.py 中 zenity 非零退出统一按「已取消」处理（可能掩盖无显示器等其他错误）——设计权衡。

---

## 3) 总体结论

未发现 critical/major 级缺陷。共 12 条 minor 级发现（其中 2 条标注「不确定」；另有 1 条初拟疑点经复核撤销，见上），最值得优先处理的是：
1. path_alias.py 的 `manifest.network=None` AttributeError（可复现、产生裸 traceback）；
2. pageviews.py 无归档截断不重置游标（与 docstring 不符、静默漏计）；
3. pageviews.py 游标清理 LIKE 通配符未转义（低概率数据误删）。

其余为注释误导、退出码不一致、防御性不足等低影响项。所有测试失败均为 Windows 环境伪影，未发现源码缺陷由测试证实。
