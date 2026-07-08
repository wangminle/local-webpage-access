# Local Webpage Access —— 待整改与待开发综合 WBS

> **编制时间**：2026-07-08
> **当前版本**：V0.4.1（task-list 170 项 / 已完成 168 / 待办 2）
> **代码规模**：源码 33 个 Python 模块 + 单页前端 + 15 个 SKILL.md
> **参考文档**：
> - 7/7｜[技术分析报告](local-webpage-access-analysis-20260707.md)（`analysis-20260707`）
> - 7/7｜[Runtime 运维复盘](local-webpage-access-runtime-analysis-20260707.md)（`runtime-analysis-20260707`，DOC-012）
> - 7/7｜[IMP-010~021 实施计划](local-webpage-access-imp010-021-plan-20260707.md)（`imp010-021-plan-20260707`）
> - 7/8｜[启动故障诊断报告](local-webpage-access-startup-failure-20260708.md)（`startup-failure-20260708`，DOC-015）
> - 7/8｜[Caddy 启动故障复盘](local-webpage-access-caddy-startup-incident-20260708.md)（`caddy-startup-incident-20260708`，DOC-013）
> - 7/8｜[Caddy 启动故障排查报告](local-webpage-access-caddy-startup-diagnostic-report-20260708.md)（`caddy-startup-diagnostic-report-20260708`，DOC-014）

---

## 一、编制说明与目标

本 WBS 在通读上述 6 份文档、核对 `task-list.md` 与当前源码后编制，目的是把**所有已发现但尚未落地的整改项与开发项**收敛为一份**唯一、可执行、可追踪**的工作分解结构。

**核对结论（当前代码事实）**：

| 触点 | 文档主张 | 代码实际状态 | 结论 |
| --- | --- | --- | --- |
| `static_gateway.py::reload_all` | 需 `ensure_caddy_running` 前置探测 | 无该函数，reload 前不保证 master 存活 | ❌ 未落地 |
| `static_gateway.py` `caddy start/stop` | 需纳入 Caddy 生命周期 | 全库无 `caddy start/run/stop` 调用 | ❌ 未落地 |
| `static_gateway.py::reload_all` 回滚 | reload 失败回滚到 `previous` | 确认回滚 `previous`（旧配置含 import 行） | ⚠️ BUG-069 根因确认 |
| `static_gateway.py::enable` catch | 删 site/alias 后未重组主配置 | 确认仅 `remove_site_config`，主 Caddyfile 保留悬空 import | ⚠️ BUG-069 根因确认 |
| `gateway_service.py` | 计划新建 | 文件不存在 | ❌ 未落地 |
| `daemon.py::process_zip` | 改 `on_conflict="error"` | 仍为默认 rename 策略 | ❌ 未落地 |
| `daemon.py` inbox processed | 需 `mv → inbox/processed/` | 无 `inbox_processed`，无搬移 | ❌ 未落地 |
| `daemon.py` 开机自愈 | 需 `reconcile()` 恢复 desired=running | `run_watcher` 仅扫 inbox，无自愈 | ❌ 未落地 |
| `lifecycle.py` 冗余清理 | `list_redundant/remove_redundant` | 无相关函数 | ❌ 未落地 |
| `scanner.py` Python 优先 | `_is_real_node` 信号强度判定 | 仍为 `if package.json → elif python` 硬顺序 | ❌ 未落地 |
| 资源档位映射 | `resource_profiles.py`/`profile_to_limits` | 无相关符号，容器恒 512m | ❌ 未落地 |
| `path_alias.py` 容器别名 | 放开 `SHARED_STATIC` 限制 | 仍限 shared-static | ❌ 未落地 |
| `doctor.py` Caddy 健康探针 | validate/admin/8080 探测 | 仅 `caddy version` 探测 | ❌ 未落地 |
| BUG-068（IPv6 admin） | `--address 127.0.0.1:2019` | 已在 `reload_all` 实现 | ✅ 已完成 |

**已完成、无需重做**（运维层）：7/8 上午的现场恢复已由 OPS-021（11:27）完成（清理 stale pid、重置 Caddyfile、`caddy run`、重启 manager、恢复两个静态实例，18000/18001/8080/18002 均 200）。**因此本 WBS 只聚焦代码层根治与功能开发，不含一次性运维恢复动作。**

**目标**：一次性、按依赖顺序解决网关稳定性根因（P0），杜绝问题再生（P1），改善大项目部署与可观测性（P1/P2），并完成技术债重构（P2/P3）。

---

## 二、问题总清单（去重后）

将 6 份文档的所有改进项去重、合并、编号，映射到优先级与源文档。**编号沿用 `imp010-021-plan` 既有 IMP/BUG 号，新增项以 DEV/OPS/DOC/CHK 前缀续接**，与 `task-list.md` 保持一致。

### 2.1 P0 —— 网关稳定性根因（必须最先做）

| 编号 | 类型 | 问题 | 来源 | 现状 |
| --- | --- | --- | --- | --- |
| **BUG-069** | 修复 | reload 失败回滚 `previous` + enable 删片段 → 主 Caddyfile 悬空 import，Caddy 冷启动/validate 失败，形成"恢复→失败→回滚"死循环 | startup-failure §4B、caddy-startup-diagnostic §5-C2、runtime-analysis §6.1 | 待修复 |
| **IMP-010 / DEV-041** | 开发 | Caddy master 无生命周期：全库只有 `caddy reload`，master 退出后 reload 必失败；无 `caddy start/stop`、无 pid 管理、无自愈 | startup-failure §4A、caddy-startup-incident §3.1、caddy-startup-diagnostic §5-C1、imp010-021 阶段1 | 待开发 |
| **BUG-070**（新） | 修复 | stale pid 未清理：切换 builtin↔caddy 后 `run/static-*.pid`、`run/caddy.pid` 指向已死进程，`is_enabled()` 仅看 pid 存活致状态误判 | caddy-startup-diagnostic §5-C3、caddy-startup-incident §3.4 | 待修复 |

### 2.2 P1 —— 防复发、自愈与可观测性

| 编号 | 类型 | 问题 | 来源 | 现状 |
| --- | --- | --- | --- | --- |
| **IMP-011** | 开发 | daemon inbox 防污染：`process_zip` 默认 rename 致 `-2/-3` 冗余实例；import 后不搬移 zip，归档后仍按旧指纹重复导入 | runtime-analysis §2.3/§6.1、imp010-021 阶段2 | 待开发 |
| **IMP-012** | 开发 | 冗余实例批量清理：按 `sourceZipHash` 去重，保留最早者，`lwa remove --redundant --purge` + 同步删网关片段 | runtime-analysis §6.1、imp010-021 阶段2 | 待开发 |
| **DEV-042**（新） | 开发 | 开机/守护自愈 `reconcile()`：扫描 `desired=running ∧ status≠running` 实例逐一 restart；builtin 静态服务进程存活监管 | startup-failure §4C/§6-P1、caddy-startup-diagnostic §7-P1 | 待开发 |
| **IMP-020** | 开发 | doctor / 管理页 Caddy 健康探针：Caddyfile validate、admin :2019、:8080、enabled 站点 hostPort 可达、stale pid 提示 | runtime-analysis §6.3、caddy-startup-diagnostic §5-C4/§7、imp010-021 阶段1 | 待开发 |
| **BUG-071**（新） | 修复 | Caddy 模式状态观测失真：master 挂掉后 `_observe_static_status` 用 `health_check(port)` 兜底把 enabled 实例标 stopped，未区分"网关不可达" | caddy-startup-incident §3.5、caddy-startup-diagnostic §7-P1 | 待修复 |
| **DEV-043**（新） | 开发 | 状态模型区分 `stopped` / `gateway_down` / `config_invalid`（enabledButUnreachable），管理页显式呈现 | caddy-startup-diagnostic §7-P1/§9、startup-failure | 待开发 |
| **OPS-022**（新） | 运维 | 开机自启：`lwa setup` 生成 launchd plist（macOS）/等价机制，开机自启 daemon + manager（+ 可选 caddy） | startup-failure §6-P1 | 待开发 |

### 2.3 P1 —— 大项目部署（prd-workflow 类）

| 编号 | 类型 | 问题 | 来源 | 现状 |
| --- | --- | --- | --- | --- |
| **IMP-013** | 开发 | Scanner 误判：`package.json` 仅辅助工具（如 pi-agent）时应优先识别 Python，而非 pending/static | runtime-analysis §4.2-P1、imp010-021 阶段3 | 待开发 |
| **IMP-014** | 开发 | 容器实例路径别名：放开 `path_alias` 的 `SHARED_STATIC` 限制，支持 `docker-compose` 反代 hostPort | runtime-analysis §6.2、imp010-021 阶段4 | 待开发 |
| **IMP-015** | 开发 | 业务 `.env` 合并：检测 `current/.env.example` → 复制为 `docker/.env.example` + compose 多层 env_file + CLI 提示（不自动填密钥） | runtime-analysis §4.2-P4、imp010-021 阶段4 | 待开发 |
| **IMP-016** | 开发 | Python 全栈 Dockerfile 增强：运行时含 Node 时追加 `apt nodejs npm` + `npm ci --omit=dev`（Pi Agent 可用） | runtime-analysis §4.2-P5、imp010-021 阶段3 | 待开发 |
| **IMP-017** | 开发 | 生产依赖分离：优先 `requirements-prod.txt`，否则构建时 strip `pytest*` 测试包 | runtime-analysis §4.2-P6、imp010-021 阶段3 | 待开发 |
| **IMP-018** | 开发 | 资源档位映射：`resourceProfile → mem/cpus` 映射表；命中 lancedb/pyarrow/torch/openai 等重依赖自动升 medium | runtime-analysis §4.2-P8、imp010-021 阶段3 | 待开发 |
| **IMP-021** | 开发 | 端口漂移可视化：容器 restart 后 hostPort 变化时自动重写别名 conf + reload | runtime-analysis §6.3、imp010-021 阶段4 | 待开发 |

### 2.4 P1/P2 —— 路径别名一致性（analysis-20260707 P1）

| 编号 | 类型 | 问题 | 来源 | 现状 |
| --- | --- | --- | --- | --- |
| **IMP-022**（新） | 开发 | 路径别名 Caddy 依赖显式化：`lwa alias set` 在 `backend != caddy` 时明确报错/WARN，消除"设置成功但访问失败"的割裂 | analysis §5.1-P1/§6.2 | 待开发 |
| **IMP-023**（新） | 开发 | SPA 子路径资源加载：Vite/Vue 绝对资源路径（`/assets/…`）在 `/<alias>/` 下白屏；`lwa alias set` 输出 `--base=/<alias>/` 重构建提示 + SKILL 记录 | analysis §5.1-P1 | 待开发 |

### 2.5 P2/P3 —— 技术债与体验（analysis-20260707）

| 编号 | 类型 | 问题 | 来源 | 现状 |
| --- | --- | --- | --- | --- |
| **IMP-019** | 开发 | 管理页实例分组/过滤：文本搜索 + 状态/形态下拉 + 隐藏冗余副本 + 冗余徽章 + 一键批量删冗余 | runtime-analysis §6.3、imp010-021 阶段5 | 待开发 |
| **DEV-044**（新） | 优化 | 拆分 `cli.py`（1112 行）：按功能域 `add_typer` 拆分为 import/lifecycle/status/manager/daemon/system 子模块 | analysis §4.3/§6.1 | 待开发 |
| **DEV-045**（新） | 优化 | 拆分 `importer.py`（981 行）：抽出 `zip_processor.py`（sanitize+audit+extract），`Importer` 专注实例目录与 registry | analysis §4.2/§6.1 | 待开发 |
| **DEV-046**（新） | 开发 | 管理页前端引入 Vue 3 + importmap（无 npm build），解决 vanilla `app.js` 组件化与维护上限 | analysis §5.2/§6.2 | 待开发（可延后） |
| **DEV-047**（新） | 开发 | 跨进程构建并发：将 rebuild 统一路由到 daemon 队列，daemon 作为单例调度中枢，解决进程内 `BoundedSemaphore` 局限 | analysis §5.2/§6.2 | 待开发（可延后） |

### 2.6 决策待定项（需先定方向）

| 编号 | 类型 | 决策点 | 来源 |
| --- | --- | --- | --- |
| **CHK-013**（新） | 检查 | 网关技术选型：维持 Caddy（完善 IMP-010）vs 回退 builtin + 独立 Caddy 只做 8080 别名 vs 换 nginx。CHK-009/011/012 已初步结论：换 nginx 可简化 admin/IPv6/限流插件问题，但 master 生命周期、原子配置回滚、stale pid、自愈仍需自研——**换网关不能省掉 P0**。建议先做 P0 再评估是否迁移 | CHK-009/011/012、caddy-startup-incident §4.3、caddy-startup-diagnostic §7 | 待决策 |

### 2.7 文档与任务同步

| 编号 | 类型 | 事项 | 来源 |
| --- | --- | --- | --- |
| **DOC-016**（新） | 文档 | 新建 `docs/operations-playbook.md`：Caddy vs builtin 选型、inbox 勿放测试 zip、容器别名步骤、Caddy master 排障、开机自启 | imp010-021 阶段5、runtime-analysis §6.3 | 待开发 |
| **DOC-017**（新） | 文档 | 更新 `docs/runtime-workspace.md`：gateway 命令、inbox/processed 目录、.env.local 用法、资源档位说明 | imp010-021 阶段5 | 待开发 |
| **任务同步** | 文档 | 每阶段完成后按 `task-list-standard` 写入 task-list.md | imp010-021 §2 | 持续 |

---

## 三、WBS 分解（按阶段与依赖）

> 每阶段独立可测、可提交。子任务编号 `阶段.序号`，标注触点文件、验收标准、依赖。工作量为相对估算（S≈0.5d，M≈1d，L≈2d+）。

### 阶段 0：网关稳定性根因修复（P0，最高优先级）

**目标**：让 Caddy 网关链路"永不自锁"——master 缺失能自愈、reload 失败不留悬空配置、stale pid 不误判。这是 7/8 事故的直接根因，必须最先根治。

| 子任务 | 触点 | 说明 | 验收 | 量 |
| --- | --- | --- | --- | --- |
| 0.1 | `static_gateway.py` `reload_all` / `enable` / `_assemble_main_config` | **修 BUG-069**：reload 失败不再盲回滚 `previous`；改为 reload 前 `caddy validate` 临时配置，成功再替换正式 Caddyfile；enable catch 删片段后调用 `_assemble_main_config()` 重写主配置（基于实际存在的 conf）。主 Caddyfile 永不 import 不存在文件 | 构造 reload 失败（占端口）后 `caddy validate` 仍通过；`sites/` 与 import 行一致 | M |
| 0.2 | 新建 `gateway_service.py` | **IMP-010 核心**：镜像 `manager_service.py`，复用 Caddy 原生 daemonize。`GatewayState{enabled,pid,startedAt,port=8080,adminPort=2019}`；`start_gateway`（validate→`caddy start --pidfile run/caddy.pid`→轮询 admin :2019→健康 :8080→写 `run/gateway.json`）、`stop_gateway`（`caddy stop`）、`is_gateway_running`、`gateway_status`、`maybe_start_gateway`（吞错降级 builtin 不阻断） | 单测覆盖启停/探活/降级 | L |
| 0.3 | `static_gateway.py` 新增 `ensure_caddy_running()` + `caddy_start()`/`caddy_stop()` | reload 前探测 admin :2019，不可达则 `caddy start`（加载当前主配或最小配置）；stale pid 先清理；薄封装集中 caddy 子命令 | kill caddy 后 `lwa start` 自动拉起并成功 reload | M |
| 0.4 | `static_gateway.py` `reload_all` | 自愈：reload 非零→探 admin→死则 `caddy start` 再 reload 一次；仍失败才抛 `GatewayError` | `test_reload_all_self_heals_when_master_dead` | S |
| 0.5 | `static_gateway.py` `enable` except | reload 失败**不调 `release_instance`**（BUG-068 语义：保留端口登记便于重试），仅 remove site/alias conf 后重组主配置 | `test_enable_failure_keeps_port` | S |
| 0.6 | `static_gateway.py` `disable` + `is_enabled` | **修 BUG-070/071**：Caddy 模式 enable 前删 `run/static-<id>.pid`；`is_enabled()` 在 caddy 模式改查 `health_check(host_port)`；disable 时 orphan builtin 兜底 kill（跨平台：ps/tasklist，无 psutil 则仅 pidfile） | `test_disable_kills_orphan_builtin`、`test_is_enabled_caddy_mode_uses_health` | M |
| 0.7 | `cli.py` | 新增 `gateway_app`（on/off/status），`app.add_typer(gateway_app, name="gateway")`；on/off 前校验 `MIN_CADDY_VERSION` | `lwa gateway on/off/status` 可用 | S |
| 0.8 | `manager_service.py` `start_manager` / `stop_manager` | 成功后调 `maybe_start_gateway`（联动启 Caddy）；`stop_manager` **不联动停**（业务入口优先） | manager on 后 :8080 可达 | S |
| 0.9 | `tests/test_gateway_service.py`、`tests/test_static_gateway.py` | 全部上述回归 + `test_caddyfile_never_dangling_import`（BUG-069 核心回归） | 目标 + 全量 pytest 不退化 | M |

**阶段验收**：`lwa gateway on` 启 Caddy 且 :8080 可达；kill Caddy 后 `reload_all` 自愈；reload 失败后 `caddy validate` 主配置永远通过；failed 实例不占端口但保留登记；stale pid 被清理；orphan builtin 被 kill。

---

### 阶段 1：防复发 + 自愈 + 可观测性（P1）

**目标**：杜绝冗余实例再生，开机自动恢复期望运行实例，doctor 提前暴露网关问题。依赖阶段 0 的 gateway 自愈。

| 子任务 | 触点 | 说明 | 验收 | 量 |
| --- | --- | --- | --- | --- |
| 1.1 | `daemon.py` `process_zip` | **IMP-011**：改 `on_conflict="error"`；冲突时不自动 update，写 `pending` + 事件「实例已存在，如需更新用 `lwa import <zip> --update <slug>`」 | `test_daemon_conflict_error_no_rename` | S |
| 1.2 | `daemon.py` `run_watcher`、`paths.py` | import 成功后 `zip → inbox/processed/`（同名加时间戳）；`Workspace.inbox_processed` 属性 + `ensure_workspace_dirs` 创建；`scan_inbox` 不递归 processed | `test_daemon_moves_processed_zip` | S |
| 1.3 | `lifecycle.py` | **IMP-012**：`list_redundant_instances`（按 `sourceZipHash` 分组保留最早者，空 hash 不参与）+ `remove_redundant`（批量 purge） | `test_list_redundant_keeps_oldest`、`test_redundant_ignores_empty_hash` | M |
| 1.4 | `cli.py` `lwa remove` | 增 `--redundant` flag，执行前打印待删列表 + sourceZipHash 确认 | `lwa remove --redundant --purge` 删冗余保留有效 | S |
| 1.5 | `daemon.py` / 新增 `reconcile()` | **DEV-042**：`run_watcher` 启动时扫描 `desired=running ∧ status≠running` 实例逐一 restart；builtin 静态进程存活监管 | 停所有静态进程后重启 daemon 自动恢复 | M |
| 1.6 | `doctor.py` `check_static_gateway` | **IMP-020**：Caddyfile validate + admin :2019 + :8080 可达 + enabled 站点 hostPort 探测 + stale pid 提示；挂掉给 WARN + 修复指引 | `test_doctor_caddy_alive_probe` | M |
| 1.7 | `status.py` `_observe_static_status`、`models.py` | **修 BUG-071 + DEV-043**：Caddy 模式下区分 `stopped` / `gateway_down` / `config_invalid`；enabled 但不可达时不误标普通 stopped | `test_observe_distinguishes_gateway_down` | M |
| 1.8 | `manager_static/app.js`、`manager_api.py` | 管理页显式呈现"期望运行但网关不可达"，提供"一键 recover"入口 | 前端 `node --check` + DOM 单测 | M |
| 1.9 | `setup.py` | **OPS-022**：`lwa setup` 生成 launchd plist（macOS）开机自启 daemon+manager（+可选 caddy）；其他平台文档说明 | plist 生成且格式正确 | M |
| 1.10 | 对应 `tests/*` | 上述全部回归 | 目标 + 全量不退化 | M |

**阶段验收**：冲突 zip 不再建 `-2/-3` 且成功 zip 进 `inbox/processed/`；`lwa remove --redundant --purge` 精准清理；重启 daemon 后 desired=running 实例自动恢复；doctor 能提前报出 Caddy master/悬空配置/端口不可达。

---

### 阶段 2：大项目部署增强（P1）

**目标**：让 prd-workflow 类"Python + 辅助 Node + 重依赖"项目开箱即用。依赖阶段 0（gateway 稳定）。

| 子任务 | 触点 | 说明 | 验收 | 量 |
| --- | --- | --- | --- | --- |
| 2.1 | `scanner.py` `detect` | **IMP-013**：抽 `_is_real_node`（命中 NODE_FRONTEND/NODE_BACKEND 才算真 Node）；判定顺序改为真 Node → Python 信号 → static；辅助 package.json 走 Python | `test_scanner_prefers_python_when_package_json_auxiliary` | M |
| 2.2 | 新建 `resource_profiles.py`（或 `models.py`） | **IMP-018**：`_RESOURCE_PROFILE_LIMITS`（tiny 128m/0.25、small 256m/0.5、medium 1g/1.5、heavy 2g/3）+ `profile_to_limits` | `test_resource_profile_maps_to_mem_limit` | S |
| 2.3 | `scanner.py` `_detect_heavy_deps` | 命中 `HEAVY_RUNTIMES={lancedb,pyarrow,torch,transformers,tensorflow,openai,anthropic,chromadb,pymilvus}` → 自动升 medium（已 medium/heavy 不降） | `test_heavy_deps_upgrade_profile` | S |
| 2.4 | `importer.py` `build_manifest_from_detection` | 构造 `ContainerConfig` 传 `resourceLimits=profile_to_limits(...)`，compose `${MEMORY_LIMIT}` 读取 | mem_limit 随档位生效 | S |
| 2.5 | `dockerfile_templates.py` `_render_python` | **IMP-016**：检测 package.json → base 保持 python:3.13-slim，追加 `apt nodejs npm` + `COPY package*.json` + `npm ci --omit=dev` | `test_dockerfile_python_with_node` | M |
| 2.6 | `dockerfile_templates.py` `_render_python` | **IMP-017**：优先 `requirements-prod.txt`；否则构建时 strip `pytest*` 测试包 | `test_dockerfile_strips_pytest` | S |
| 2.7 | 对应 `tests/*` | 上述全部回归 | 目标 + 全量不退化 | M |

**阶段验收**：prd-workflow zip 识别为 python/docker-compose 而非 pending；含 Node 的 Python 容器 Dockerfile 带 nodejs；lancedb 项目自动 1g mem_limit（旧实例需 rebuild，文档说明）。

---

### 阶段 3：容器别名 + .env 多层 + 端口漂移（P1）

**目标**：容器实例享受路径别名与业务密钥注入，重启后别名跟随端口。依赖阶段 0（reload 稳定）+ 阶段 2（scanner 正确识别）。

| 子任务 | 触点 | 说明 | 验收 | 量 |
| --- | --- | --- | --- | --- |
| 3.1 | `path_alias.py`、`importer.py` | **IMP-014**：放开 `SHARED_STATIC` 限制，允许 `DOCKER_COMPOSE`；`generate_alias_config` 已 runtime 无关（reverse_proxy hostPort）；manifest 容器 alias 复用/新增字段 | `test_container_path_alias` | M |
| 3.2 | `compose.py` `generate_env` / `_COMPOSE_TEMPLATE` | **IMP-015**：生成 LWA `.env` 后，若 `current/.env.example` 存在 → 复制为 `docker/.env.example`（不覆盖）；compose `env_file: [.env, .env.local]`（缺失文件报错则回退单 `.env` + 文档指导）；CLI 提示业务密钥填 `docker/.env.local` | `test_env_example_copied_to_docker`、`test_env_local_in_compose_env_file` | M |
| 3.3 | `importer.py` / `cli.py` | import 检测 `.env.example` → CLI 醒目提示 + 写事件 `env_example_detected` | 提示与事件可见 | S |
| 3.4 | `lifecycle.py` `restart_instance` + `_sync_alias_port` | **IMP-021**：重启后若实例有别名且 hostPort 变化 → 重写 alias conf + reload_all | `test_restart_syncs_alias_port` | M |
| 3.5 | 对应 `tests/*` | 上述全部回归 | 目标 + 全量不退化 | M |

**阶段验收**：容器实例可设别名；`.env.example` 自动复制 + CLI 提示；容器重启后别名跟随端口漂移。

---

### 阶段 4：路径别名一致性 + 管理页体验（P1/P2）

**目标**：消除别名"设置成功但访问失败"的体验割裂，管理页可过滤/隐藏冗余。

| 子任务 | 触点 | 说明 | 验收 | 量 |
| --- | --- | --- | --- | --- |
| 4.1 | `path_alias.py` `set_instance_path_alias`、`cli.py` `alias set` | **IMP-022**：`backend != caddy` 时明确报错/WARN，提示先 `lwa gateway on`/装 Caddy；不再无声写元数据 | `test_alias_set_blocks_builtin` | S |
| 4.2 | `cli.py` `alias set`、SKILL（lwa-import-zip/generate-static-gateway-config） | **IMP-023**：输出 SPA 子路径 `--base=/<alias>/` 重构建提示；SKILL.md 记录白屏限制与规避 | 提示可见 + SKILL 更新 | S |
| 4.3 | `manager_static/index.html` / `app.js` / `style.css` | **IMP-019**：文本搜索 + 状态/形态下拉 + "显示冗余"checkbox（冗余行黄色边框 + 徽章 + 行内删除）+ 顶部"批量删除冗余" | `node --check` + DOM 单测 | M |
| 4.4 | `manager_api.py` | `GET /api/redundant`；`/api/instances` 每项补 `redundant: bool` | `test_api_redundant`、`test_instances_redundant_flag` | S |
| 4.5 | 对应 `tests/*` | 上述全部回归 | 目标 + 全量不退化 | S |

**阶段验收**：builtin 模式设别名被明确拦截；SPA 子路径有清晰指引；管理页可搜索/过滤/隐藏冗余并一键批量删。

---

### 阶段 5：技术债重构（P2/P3，可增量推进）

**目标**：降低单文件复杂度，为后续演进减负。**独立于业务，可择机进行，不阻塞前 4 阶段。**

| 子任务 | 触点 | 说明 | 验收 | 量 |
| --- | --- | --- | --- | --- |
| 5.1 | `cli.py` → `cli/` 包 | **DEV-044**：`add_typer` 拆分为 import/lifecycle/status/manager/daemon/system 子模块，`cli.py` 退化为路由注册 | 全量 CLI 行为不变 + 全量 pytest | L |
| 5.2 | `importer.py` → `zip_processor.py` | **DEV-045**：抽出 sanitize+audit+extract，`Importer` 专注实例目录与 registry | 导入行为不变 + 全量 pytest | L |
| 5.3 | `manager_static/`（Vue 3 importmap） | **DEV-046**（可延后）：无 npm build 引入 Vue 3 组件化 | 管理页功能对齐现状 | L |
| 5.4 | daemon 构建调度中枢 | **DEV-047**（可延后）：rebuild 统一路由 daemon 队列，跨进程互斥 | 跨进程 rebuild 串行 | L |

---

### 阶段 6：文档与任务同步（贯穿全程）

| 子任务 | 触点 | 说明 |
| --- | --- | --- |
| 6.1 | 新建 `docs/operations-playbook.md` | **DOC-016**：Caddy vs builtin 选型、inbox 勿放测试 zip、容器别名步骤、Caddy master 排障、开机自启 |
| 6.2 | `docs/runtime-workspace.md` | **DOC-017**：gateway 命令、inbox/processed、.env.local、资源档位 |
| 6.3 | `task-list.md` | 每阶段完成写入 BUG/IMP/DEV/DOC/OPS 条目，遵循 task-list-standard（8 动作枚举、`YYYY-MM-DD HH:MM`、未完成填 `-`）；更新统计摘要 |

---

## 四、决策项（动工前需确认）

| 决策 | 选项 | 建议 |
| --- | --- | --- |
| **CHK-013 网关选型** | ① 维持 Caddy + 完善 IMP-010（推荐）；② 回退 builtin + 独立 Caddy 只做 8080 别名；③ 换 nginx | **先做阶段 0 P0（三种方案都需要网关生命周期/原子配置/自愈），再评估是否迁移**。CHK-009/011/012 已确认换网关不能省掉 P0 工作量。 |
| **IMP-015 env_file 缺失处理** | compose `env_file: [.env, .env.local]` 对缺失 `.env.local` 报错 | 优先多层；若报错则回退单 `.env` + 文档指导用户合并（imp010-021 §4 已列两种姿势） |
| **DEV-046/047 是否本轮做** | 前端框架化 / daemon 调度中枢 | 建议**延后**至前 4 阶段稳定后，避免扩大本轮改动面 |

---

## 五、实施顺序与依赖图

```
阶段0 (P0 网关根因)  ──►  阶段1 (防复发+自愈+可观测)
   │                          │
   │                          ├──►  阶段2 (大项目部署)  ──►  阶段3 (容器别名/.env/端口漂移)
   │                          │
   └──────────────────────────┴──►  阶段4 (别名一致性+管理页)

阶段5 (技术债重构)  ── 独立，可择机穿插
阶段6 (文档+任务同步)  ── 贯穿每阶段收尾
```

**推荐节奏**（每阶段一个提交）：

1. **阶段 0**（P0）——最高优先级，解决 7/8 事故根因，独立可测，**先行合入**。
2. **阶段 1**（P1）——防再生 + 自愈，依赖阶段 0 的 gateway 自愈。
3. **阶段 2**（P1）——大项目部署，独立于 1。
4. **阶段 3**（P1）——依赖阶段 2 的 scanner 修正。
5. **阶段 4**（P1/P2）——别名一致性 + 管理页收尾，依赖前序 API。
6. **阶段 5**（P2/P3）——技术债，择机推进。

---

## 六、验收与回归策略

**每阶段闭环**：
1. `python -m compileall src/local_webpage_access/` 全清；
2. `python -m pyflakes src/local_webpage_access/` 退出码 0；
3. `node --check manager_static/app.js`（涉及前端阶段）；
4. 目标 pytest（每 IMP/BUG/DEV 配套回归，见各阶段测试点）全绿；
5. 全量 pytest 不退化（当前基线约 744 passed / 4 skipped）；
6. 写入 `task-list.md` 并跑校验。

**runtime 工作区实测**（开发仓 `runtime/`，需真实 Caddy/Docker）：

| 阶段 | 实测动作 |
| --- | --- |
| 0 | `lwa gateway on/off/status`；kill Caddy 验证自愈；构造 reload 失败验证 `caddy validate` 主配置仍通过；重启机器验证不再自锁 |
| 1 | 丢冲突 zip 进 inbox 验证不建 `-2` + zip 进 `inbox/processed/`；停所有静态进程后重启 daemon 验证自愈；`lwa doctor` 报出 Caddy 健康 |
| 2 | rebuild prd-workflow 验证 mem_limit=1g + Dockerfile 含 nodejs；prd-workflow zip 识别为 python |
| 3 | 容器实例设别名 + 重启验证端口漂移同步；`.env.example` 自动复制 + 提示 |
| 4 | builtin 模式设别名被拦截；管理页过滤/隐藏/批量删冗余 |

---

## 七、任务编号映射（写入 task-list.md 时对齐）

| 本 WBS | task-list 现状 | 备注 |
| --- | --- | --- |
| 阶段0 BUG-069 | 已存在（待修复） | 直接推进，完成后填完成时间 |
| 阶段0 IMP-010 | DEV-041（待开发） | IMP-010 即 DEV-041 |
| 阶段0 BUG-070/071 | 新增 | stale pid / 状态观测失真 |
| 阶段1 IMP-011/012/020 | 新增 DEV 条目 | daemon 防污染 / 冗余清理 / doctor 探针 |
| 阶段1 DEV-042/043、OPS-022 | 新增 | 自愈 reconcile / 状态模型 / 开机自启 |
| 阶段2 IMP-013/016/017/018 | 新增 DEV 条目 | scanner / Dockerfile / 依赖分离 / 资源档位 |
| 阶段3 IMP-014/015/021 | 新增 DEV 条目 | 容器别名 / .env / 端口漂移 |
| 阶段4 IMP-019/022/023 | 新增 DEV 条目 | 管理页 / 别名拦截 / SPA 子路径 |
| 阶段5 DEV-044~047 | 新增 | 重构（P2/P3） |
| 阶段6 DOC-016/017 | 新增 | playbook / runtime-workspace |
| CHK-013 | 新增 | 网关选型决策 |

> 说明：`imp010-021-plan-20260707` 原按 5 阶段拆分；本 WBS 依据 7/8 事故与代码现状**重排优先级**——将 BUG-069（悬空 import 死锁）与 IMP-010（Caddy 生命周期）合并为**阶段 0（P0）先行**，并新增开机自愈（DEV-042）、状态模型（DEV-043）、stale pid（BUG-070）、状态观测（BUG-071）、开机自启（OPS-022）、别名一致性（IMP-022/023）等 7/8 复盘暴露的新项，同时纳入 `analysis-20260707` 的重构建议（DEV-044~047）。

---

## 八、汇总

| 优先级 | 项目 | 数量 | 阶段 |
| --- | --- | --- | --- |
| **P0** | BUG-069、IMP-010(DEV-041)、BUG-070 | 3 | 阶段 0 |
| **P1** | IMP-011/012/013/014/015/016/017/018/020/021、DEV-042/043、BUG-071、OPS-022 | 14 | 阶段 1/2/3 |
| **P1/P2** | IMP-019/022/023 | 3 | 阶段 4 |
| **P2/P3** | DEV-044/045/046/047 | 4 | 阶段 5 |
| **决策/文档** | CHK-013、DOC-016/017 | 3 | 贯穿 |
| **合计** | | **27 项** | 6 阶段 |

**一句话结论**：当前 V0.4.1 功能完整、测试扎实，唯一致命短板是 **Caddy 网关链路无生命周期管理 + reload 失败自锁（BUG-069/IMP-010）**——这是 7/8 事故根因，必须作为**阶段 0（P0）最先根治**；其后按"防复发→大项目部署→容器别名→体验优化→技术债"顺序推进，全程同步 `task-list.md`。

---

*本 WBS 由 2026-07-08 综合分析会话编制，整合 7/7、7/8 共 6 份文档 + 当前源码核对结论。执行时以此为唯一依据，偏差需回写本文档并同步 task-list。*
