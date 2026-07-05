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
| BUG-010 | 修复 | stop_instance 对容器实例静默无操作，CLI 仍打印已停止，用户误以为成功 | 2026-07-05 10:42 | 2026-07-05 10:42 | 已完成 | stop_instance 对非 shared-static runtime 改抛 HostingError，与 host_instance 行为一致（用户走查 bug #6） |
| BUG-011 | 修复 | reload_all 首次无旧配置且 reload 失败时，坏的新 Caddyfile 残留原地，影响后续 reload | 2026-07-05 10:42 | 2026-07-05 10:42 | 已完成 | previous is None 分支失败时 unlink 主配置；新增两条 reload 回归测试（用户走查 bug #7） |
| BUG-012 | 修复 | has_manage_py 已采集但从未参与 Django 识别（仅靠依赖判断），信号被浪费 | 2026-07-05 10:42 | 2026-07-05 10:42 | 已完成 | _detect_python 在依赖未命中 django 时据 has_manage_py 补识别（用户走查 bug #8） |
| BUG-013 | 修复 | 嵌套 index.html + 根目录同级资源（如 current/shared.css 与 current/site/index.html 同级）会丢失 sibling | 2026-07-05 11:15 | 2026-07-05 11:20 | 已完成 | host_static 改为同步整个 current/，再把 index 所在子目录内容提升到 public/ 根（新增 _copy_item/_promote_to_root）；e2e 验证 GET /shared.css、/style.css、/site/style.css 均可访问，169 测试通过（BUG-004 边界，用户批准方案） |

## 调整事项

| ID | 动作 | 事项 | 发现时间 | 完成时间 | 状态 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |

## 检查事项

| ID | 动作 | 事项 | 发现时间 | 完成时间 | 状态 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |

## 测试数据

| ID | 动作 | 事项 | 发现时间 | 完成时间 | 状态 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |

## 文档维护

| ID | 动作 | 事项 | 发现时间 | 完成时间 | 状态 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |

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

## 配置运维

| ID | 动作 | 事项 | 发现时间 | 完成时间 | 状态 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |

## 规划事项

| ID | 动作 | 事项 | 发现时间 | 完成时间 | 状态 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| PLN-001 | 规划 | Phase 0：准备与底座（WBS-00~WBS-05，CLI 骨架/配置/registry/schema） | 2026-07-04 23:49 | 2026-07-05 00:06 | 已完成 | 详见 docs/plan/local-web-access-v1-wbs-20260704.md 第 5 节；DEV-001~005 全部完成，70 测试通过 |
| PLN-002 | 规划 | Phase 1：导入、识别与端口（WBS-06~WBS-08，端口池/zip 导入/项目识别） | 2026-07-04 23:49 | 2026-07-05 00:22 | 已完成 | DEV-006~008 全部完成；e2e 验证四样例正确识别为 static/frontend-static/backend-container/fullstack-sqlite |
| PLN-003 | 规划 | Phase 2：静态路径闭环（WBS-09~WBS-11，静态网关/纯静态/前端构建） | 2026-07-04 23:49 | 2026-07-05 00:32 | 已完成 | DEV-009~011 全部完成；e2e 验证静态 HTML 与前端 SPA 构建均可经 builtin 网关访问，151 测试通过 |
| PLN-004 | 规划 | Phase 3：Docker Compose 路径闭环（WBS-12~WBS-16，Dockerfile/Compose/Runtime/Node/Python） | 2026-07-04 23:49 | - | 待开发 | 对应最小开发切片 D |
| PLN-005 | 规划 | Phase 4：生命周期、日志、资源与队列（WBS-17~WBS-20） | 2026-07-04 23:49 | - | 待开发 | 对应最小开发切片 E/F，构建并发默认为 1 |
| PLN-006 | 规划 | Phase 5：自动化与管理页（WBS-21~WBS-23，daemon/管理页 API/前端） | 2026-07-04 23:49 | - | 待开发 | 管理页端口 17800 |
| PLN-007 | 规划 | Phase 6：Skills、安全与排障（WBS-24~WBS-26） | 2026-07-04 23:49 | - | 待开发 | skill 只生成/修复配置，最终执行交给 lwa |
| PLN-008 | 规划 | Phase 7：测试、验收与发布（WBS-27~WBS-30，样例/单测集成/E2E/文档发布） | 2026-07-04 23:49 | - | 待开发 | V1 验收总清单见 WBS 第 7 节 |

## 统计摘要

| 分类 | 总数 | 已完成 | 待开发/待修复 | 完成率 |
| --- | --- | --- | --- | --- |
| 代码 Bug | 13 | 13 | 0 | 100% |
| 调整事项 | 0 | 0 | 0 | 0% |
| 检查事项 | 0 | 0 | 0 | 0% |
| 测试数据 | 0 | 0 | 0 | 0% |
| 文档维护 | 0 | 0 | 0 | 0% |
| 功能开发 | 11 | 11 | 0 | 100% |
| 配置运维 | 0 | 0 | 0 | 0% |
| 规划事项 | 8 | 3 | 5 | 38% |
| **总计** | 32 | 27 | 5 | 84% |
