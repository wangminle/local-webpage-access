# 待改进功能实施计划 IMP-010~021 + BUG-068/069（20260707）

承接 Runtime 运维复盘 [`docs/local-webpage-access-runtime-analysis-20260707.md`](../../docs/local-webpage-access-runtime-analysis-20260707.md)（DOC-012）第 6 节改进项清单，把 **IMP-010~021 + BUG-068/069 共 14 项** 拆解为可直接实现的子任务，并记录 2026-07-07 规划会话已确认的所有关键决策。

> **状态**：本文件为实施计划，尚未动工。执行时按 5 阶段顺序推进，每阶段独立可测、可提交、可写入 `task-list.md`。
> **前置**：IMP-001~009 已完成（见 [`待改进功能点记录-20260706.md`](待改进功能点记录-20260706.md)），本批次编号续接。

---

## 0. 关键决策（2026-07-07 规划会话确认）

下列决策已与用户逐项确认，实现时不再二次猜测：

| 编号 | 决策点 | 已确认方案 |
| --- | --- | --- |
| **IMP-010** | Caddy 命令结构 | 独立 `lwa gateway on/off/status` 命令组 + `reload_all` 失败自愈（探测 admin，死了 `caddy start` 再 reload）；`manager on` **联动启 Caddy**，`manager off` **不联动停 Caddy**（避免停管理页顺带下线所有别名业务） |
| **IMP-011** | daemon inbox 防污染 | daemon 改 `on_conflict="error"`；冲突时**不自动 update**（无人值守覆盖实例危险），改为写 `pending` + 事件「已存在，如需更新用 `lwa import --update`」；import 成功后 `mv zip → inbox/processed/`，同名加时间戳后缀 |
| **IMP-012 / IMP-019** | 冗余判定口径 | 按 **`sourceZipHash`** 去重（同 hash 多实例时保留 `createdAt` 最早者为主，其余标冗余）；**不**按 stopped/failed 状态批量删，避免误删有效但 stopped 的 `demo-static` |
| **IMP-015** | 业务 .env 注入 | 检测到 `current/.env.example` → 复制为 `docker/.env.example`（不覆盖已存在）+ compose `env_file` 多层（`.env` + `.env.local`）+ CLI 打印提示用户填密钥；**不自动填密钥**（无法填） |
| **IMP-016** | Python 全栈 Dockerfile + Node | 运行时含 Node：base 保持 `python:3.13-slim`，追加 `apt-get install nodejs npm` + `COPY package*.json` + `npm ci --omit=dev`；Pi Agent 等运行时 Node 依赖在容器内可用 |
| **IMP-018** | 资源档位映射 | 引入 `resourceProfile → mem/cpus` 映射表（tiny=128m/0.25, small=256m/0.5, medium=1g/1.5, heavy=2g/3）；检测到 `lancedb`/`pyarrow`/`torch`/`transformers`/`openai` 等重依赖自动升 medium；**已部署实例需 `lwa update` 或 rebuild 才生效** |

---

## 1. 与既有 IMP 的关系

| 既有 | 关系 |
| --- | --- |
| IMP-006 路径别名 | 仅 `shared-static` 官方支持 → 容器场景暴露缺口 → **IMP-014** 扩容器 |
| IMP-008 lwa update | 管理页版本陈旧时执行；IMP-012 冗余清理会调 `lwa update` 路径 |
| IMP-009 zip 更新 | daemon 应优先识别为 update 而非 rename → **IMP-011** 防重复 |
| daemon WBS-21 | rename 策略 + 不搬移 zip → 冗余实例根因 → **IMP-011** 修正策略 |
| BUG-016 | enable 失败回滚 release 端口 → **BUG-068** 改为保留端口登记便于重试 |

---

## 2. 五阶段实施拆分

### 阶段 1：Caddy 生命周期（IMP-010 + BUG-068 + BUG-069 + IMP-020）

**依赖根因**：`StaticGateway` 仅调 `caddy reload`，从不 `caddy start`/`stop`；master 退出后 reload 必失败（复盘 C1）；reload 失败后 failed 实例占端口（BUG-068）；disable 依赖 pidfile，缺则 orphan 进程残留（BUG-069）。

| 子任务 | 触点 | 说明 |
| --- | --- | --- |
| 010.01 | **新建 `src/local_webpage_access/gateway_service.py`** | 镜像 `manager_service.py` 模式，但复用 Caddy 原生 daemonize（不自己 fork 子进程）。`GatewayState{enabled, pid, startedAt, port=8080, adminPort=2019}`；`start_gateway(ws,config)`: `caddy validate` → `caddy start --config <Caddyfile>`（Caddy 自管 pid）→ 轮询 admin `:2019` 存活 → 健康检查 `:8080` → 写 `run/gateway.json`；`stop_gateway`: `caddy stop`（admin API 优雅停）；`is_gateway_running`：探测 admin + pidfile；`gateway_status`；`maybe_start_gateway`（被 manager 调用，吞错降级 builtin 不阻断） |
| 010.02 | `static_gateway.py` `reload_all`（`:386`） | 增加自愈：reload 非零返回时探测 admin，若死则 `caddy start --config` 再 reload 一次；仍失败才抛 `GatewayError`（修 C1） |
| 010.03 | `static_gateway.py` 新增 `caddy_start()`/`caddy_stop()` | 薄封装供 gateway_service 调用，集中 caddy 子命令 |
| 010.04 | `static_gateway.py` `enable`（`:321`） | except 分支 reload 失败时**不调 `release_instance`**（修 BUG-068），仅 remove site/alias conf；保留端口登记便于重试 |
| 010.05 | `static_gateway.py` `disable`（`:332`） | orphan builtin 兜底：pidfile 缺失时按端口 + `http.server`/`python -m http.server` 命令行特征扫描 kill（macOS/Linux `ps`，Windows `tasklist`/`wmic`，无 psutil 则仅 pidfile）（修 BUG-069） |
| 010.06 | `cli.py` | 新增 `gateway_app = typer.Typer()` 子命令组（on/off/status），注册 `app.add_typer(gateway_app, name="gateway")`；on/off 前校验 `MIN_CADDY_VERSION`（不降级门槛） |
| 010.07 | `manager_service.py` `start_manager`（`:245`） | 成功后调 `maybe_start_gateway`（联动启 Caddy）；`stop_manager` **不联动停**（业务入口可用性优先） |
| 010.08 | `doctor.py` `check_static_gateway`（`:408`） | 增加 admin `:2019` 存活探测 + 别名入口 `:8080` 可达性探测（IMP-020）；Caddy 挂掉时 WARN |
| 010.09 | `tests/test_gateway_service.py`、`tests/test_static_gateway.py` | `test_reload_all_self_heals_when_master_dead`、`test_enable_failure_keeps_port`（BUG-068）、`test_disable_kills_orphan_builtin`（BUG-069）、`test_doctor_caddy_alive_probe`（IMP-020）；caddy 子命令用 subprocess mock |

**验收**：`lwa gateway on` 启 Caddy 且 `:8080` 可达；kill Caddy 后 `reload_all` 自愈重启；failed 实例不占端口但保留登记；orphan http.server 被 kill；doctor 报告 Caddy alive。

---

### 阶段 2：daemon 防污染 + 冗余清理（IMP-011 + IMP-012）

**依赖根因**：daemon `process_zip` 默认 `on_conflict="rename"`（`daemon.py:421` 未传参）→ slug 冲突建 -2/-3…；import 后不搬移 zip → 归档后 processed.json 仍持旧指纹，再次入站重复 import（复盘第 2 节）。

| 子任务 | 触点 | 说明 |
| --- | --- | --- |
| 011.01 | `daemon.py` `process_zip`（`:421`） | 改 `importer.import_zip(str(zip_path), on_conflict="error")`；捕获 `ZipImportError`（冲突）：不抛、不自动 update，给实例写 `pending` + 事件「实例 {slug} 已存在，如需更新用 `lwa import <zip> --update {slug}`」，返回 failed 让 watcher 下轮再判 |
| 011.02 | `daemon.py` `run_watcher`（`:520`） | import 成功后 `zip_path.rename(workspace.inbox_processed / <原名>)`，同名冲突加时间戳后缀；move 失败不阻塞（仅 WARN，processed_key 已记录不会重复处理） |
| 011.03 | `paths.py` | `Workspace.inbox_processed` 属性（`root/inbox/processed`）；`ensure_workspace_dirs`（`:215`）增加创建；`scan_inbox`（`daemon.py:314`）glob 顶层 `*.zip` 天然不递归，processed 子目录不会被扫 |
| 012.01 | `lifecycle.py` | 新增 `list_redundant_instances(registry) -> list[str]`：按 `sourceZipHash` 分组，同 hash 多实例保留 `createdAt` 最早者，其余标冗余；hash 为空（无 original.zip）的不参与 |
| 012.02 | `lifecycle.py` | 新增 `remove_redundant(workspace, config, registry, *, force=False) -> list[str]`：批量调 `remove_instance(..., purge=True, force=force)`；返回已删 id 列表 |
| 012.03 | `cli.py` `lwa remove` | 增 `--redundant` flag（与 `--purge` 同列），`lwa remove --redundant --purge` 一键清理；执行前打印待删列表 + sourceZipHash 让用户确认 |
| 012.04 | `tests/test_daemon.py`、`tests/test_lifecycle.py` | `test_daemon_conflict_error_no_rename`、`test_daemon_moves_processed_zip`、`test_list_redundant_keeps_oldest`、`test_remove_redundant_batch`、`test_redundant_ignores_empty_hash`（demo-static 无 hash 不误判） |

**验收**：daemon 冲突 zip 不再建 -2/-3；成功 import 后 zip 在 `inbox/processed/`；`lwa remove --redundant --purge` 删掉 9 个冗余实例保留 3 个有效（含 stopped 的 demo-static 若其 hash 唯一）。

---

### 阶段 3：Scanner + Dockerfile + 资源档位（IMP-013 + IMP-016 + IMP-017 + IMP-018）

**依赖根因**：`scanner.detect`（`scanner.py:263`）是 `if package.json → elif python → elif index` 硬顺序，无信号强度比较（复盘 P1）；Dockerfile Python 模板无 Node（`dockerfile_templates.py:110`）；`resourceProfile` 与 `mem_limit` 完全脱钩（`importer.py:932` 构造 ContainerConfig 不传 resourceLimits，始终 512m）。

| 子任务 | 触点 | 说明 |
| --- | --- | --- |
| 013.01 | `scanner.py` `detect`（`:263`） | 抽 `_is_real_node(summary) -> bool`：`node_deps` 命中 `NODE_FRONTEND` 或 `NODE_BACKEND` 才算真 Node。改判定：`if _is_real_node → _detect_node；elif python 信号 → _detect_python；elif index → _detect_static`。辅助 package.json（如 prd-workflow 仅 `@earendil-works/pi-coding-agent`，无 frontend/backend 标记）走 Python |
| 013.02 | `dockerfile_templates.py` `_render_python`（`:110`） | 检测 `package.json`（且非纯前端构建场景）→ base 保持 `python:3.13-slim`，追加 `RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm && rm -rf /var/lib/apt/lists/*`；`COPY package*.json ./` + `RUN npm ci --omit=dev`（Pi Agent 运行时可用）（IMP-016） |
| 013.03 | `dockerfile_templates.py` `_render_python` | IMP-017：默认优先 `requirements-prod.txt`（若存在）；否则 requirements.txt 分支内 strip 测试包：`RUN pip install --no-cache-dir -r requirements.txt` 改为生成临时 cleaned 文件（grep -vE `^(pytest|pytest-.*)`），或 Dockerfile 内 `sed` 过滤 |
| 018.01 | `models.py` 或新建 `resource_profiles.py` | 新增 `_RESOURCE_PROFILE_LIMITS` 映射：`{TINY:("128m","0.25"), SMALL:("256m","0.5"), MEDIUM:("1g","1.5"), HEAVY:("2g","3")}`；`profile_to_limits(profile) -> ResourceLimits` |
| 018.02 | `importer.py` `build_manifest_from_detection`（`:932`） | 构造 `ContainerConfig` 时传 `resourceLimits=profile_to_limits(resource_profile)`；compose `${MEMORY_LIMIT:-{memory}}` 天然读取该值 |
| 018.03 | `scanner.py` | 新增 `_detect_heavy_deps(summary) -> bool`（与 `_detect_heavy_db:291` 平级）：deps 命中 `HEAVY_RUNTIMES = {lancedb, pyarrow, torch, transformers, tensorflow, openai, anthropic, chromadb, pymilvus}` → 自动升 `resourceProfile = MEDIUM`（已 MEDIUM/HEAVY 不降） |
| 018.04 | `tests/test_scanner.py`、`tests/test_dockerfile_templates.py`、`tests/test_importer.py` | `test_scanner_prefers_python_when_package_json_auxiliary`、`test_dockerfile_python_with_node`、`test_dockerfile_strips_pytest`、`test_resource_profile_maps_to_mem_limit`（tiny/small/medium/heavy 四档）、`test_heavy_deps_upgrade_profile`（lancedb → medium） |

**验收**：prd-workflow zip 识别为 python/docker-compose 而非 pending；含 Node 的 Python 容器 Dockerfile 带 `apt nodejs`；lancedb 项目自动 1g mem_limit；新 import 生效（旧实例需 rebuild，文档说明）。

---

### 阶段 4：容器别名 + .env 多层 + 端口漂移（IMP-014 + IMP-015 + IMP-021）

| 子任务 | 触点 | 说明 |
| --- | --- | --- |
| 014.01 | `path_alias.py`（`:197`）、`importer.py`（`:214`） | 放开 `Runtime.SHARED_STATIC` 限制，允许 `DOCKER_COMPOSE`；`generate_alias_config` 已写 `reverse_proxy 127.0.0.1:<host_port>`（runtime 无关，直接可用）；manifest 容器 path_alias 复用 `network.routeHost` 或新增 `container.routeAlias` |
| 014.02 | `static_gateway.py` | `generate_alias_config` 已 runtime 无关，无需改；确认容器 stop 时 alias conf 清理（`disable` 已 remove_alias_config） |
| 015.01 | `compose.py` `generate_env`（`:109`） | 生成 LWA 管理 `.env` 后追加：若 `current/.env.example` 存在 → 复制为 `docker/.env.example`（不覆盖已存在）；CLI 打印提示「业务密钥请填入 docker/.env.local」 |
| 015.02 | `compose.py` `_COMPOSE_TEMPLATE`（`:35`） | `env_file` 改为 `[- .env, - .env.local]`（若 compose 对缺失 `.env.local` 报错，备选：保持单 `.env` + 文档指导用户合并业务密钥，或 `docker compose --env-file`） |
| 015.03 | `importer.py` / `cli.py` | import 时检测 `.env.example` → CLI 醒目提示 + 写事件 `env_example_detected` |
| 021.01 | `lifecycle.py` `restart_instance`（`:273`） | 重启后若实例有 path_alias 且 host_port 变化 → 重写 alias conf + reload_all（当前 restart 不重写，是端口漂移根因） |
| 021.02 | `lifecycle.py` | 抽 `_sync_alias_port(workspace, config, instance_id, manifest)`：读 manifest 当前 hostPort，比对 alias conf 内 host_port，不同则重写 + reload |
| 014.04 | `tests/test_path_alias.py`、`tests/test_compose.py`、`tests/test_lifecycle.py` | `test_container_path_alias`、`test_env_example_copied_to_docker`、`test_env_local_in_compose_env_file`、`test_restart_syncs_alias_port` |

**验收**：容器实例可设别名；`.env.example` 自动复制 + CLI 提示；容器重启后别名跟随端口漂移。

---

### 阶段 5：管理页 + 文档 + 任务记录（IMP-019 + DOC-013 + task-list 同步）

| 子任务 | 触点 | 说明 |
| --- | --- | --- |
| 019.01 | `manager_static/index.html`（`:43` filter 区）、`app.js` | 新增文本搜索 input（按 id/name/stack 客户端过滤 `lastInstances`）+ 状态下拉（all/running/stopped/failed/pending）+ 形态下拉 |
| 019.02 | `app.js` `renderInstances`（`:149`）、`rowHtml`（`:175`） | 新增"显示冗余"checkbox（默认勾选，关闭后隐藏 `sourceZipHash` 冗余副本，保留主实例）；冗余行黄色左边框 + "冗余"徽章 + 行内"删除冗余副本"按钮 |
| 019.03 | `manager_static/index.html` | 新增"批量删除冗余"按钮（顶部），调 `/api/redundant` 批量 |
| 019.04 | `manager_api.py` | 新增 `GET /api/redundant`（返回冗余实例 id 列表）；`/api/instances`（`:346`）每个实例 dict 补 `redundant: bool`（同 hash 多实例且非最早者） |
| 019.05 | `manager_static/style.css` | `.row-redundant` 黄色左边框 + `.badge-redundant` 徽章 |
| DOC-013 | `docs/runtime-workspace.md`、**新建 `docs/operations-playbook.md`** | runtime-workspace 补 gateway 命令、inbox/processed 目录、.env.local 用法；operations-playbook：Caddy vs builtin 选型、inbox 勿放测试 zip、容器别名步骤、Caddy master 排障 |
| 任务同步 | `task-list.md` | 每阶段完成写入 IMP-010~021 + BUG-068/069 共 14 条 + 配套 DOC/CHK/OPS，遵循 `task-list-standard.md`（动作枚举、YYYY-MM-DD HH:MM、未完成填 -） |
| 019.06 | `tests/test_manager_api.py` | `test_api_redundant`、`test_instances_redundant_flag`；前端 `node --check app.js` + DOM 逻辑单测 |

**验收**：管理页可搜索/过滤/隐藏冗余；冗余实例可一键批量删；文档齐全；task-list 同步。

---

## 3. 验收与回归策略

**每阶段**：
1. `python -m compileall src/local_webpage_access/` 全清
2. `node --check manager_static/app.js`（涉及前端阶段）
3. 目标 pytest（每项 IMP/BUG 配套回归测试，见各阶段测试点）
4. 全量 pytest 不退化
5. 写入 `task-list.md`（CLI `add` + `check` 校验）

**runtime 工作区实测**（开发仓 `runtime/`）：
- 阶段 1 后：`lwa gateway on/off/status`，kill Caddy 验证自愈
- 阶段 2 后：丢冲突 zip 进 inbox，验证不建 -2 + zip 进 `inbox/processed/`
- 阶段 3 后：rebuild prd-workflow，验证 mem_limit=1g + Dockerfile 含 nodejs
- 阶段 4 后：容器实例设别名 + 重启验证端口漂移同步
- 阶段 5 后：管理页过滤/批量删冗余

---

## 4. 风险与边界

| 风险 | 缓解 |
| --- | --- |
| **IMP-018 已部署实例** | 映射只在新 import/rebuild 时生效；旧实例需 `lwa update` 或 `lwa rebuild`。文档（DOC-013）明确说明，不自动迁移避免破坏运行中实例 |
| **BUG-069 orphan 扫描跨平台** | macOS/Linux `ps aux \| grep http.server`；Windows `tasklist`/`wmic`；无 psutil 则仅 pidfile 路径（降级不报错） |
| **阶段 1 manager 联动** | Caddy 不可用（未装/版本低）时 `maybe_start_gateway` 吞错降级 builtin，不阻断 manager 启动 |
| **Caddy 版本门槛** | 仍 ≥ 2.11.2（`MIN_CADDY_VERSION`），`gateway on` 前校验；不降低门槛 |
| **IMP-016 apt nodejs 版本** | Debian slim 仓库 nodejs 可能偏旧（如 18.x），但对 Pi Agent（CLI 工具）够用；不追求与 host Node 24 版本一致；若需新版本可在文档说明改用 nodesource |
| **IMP-015 .env.local 缺失** | docker compose `env_file: [.env, .env.local]` 对缺失文件默认报错；备选方案：保持单 `.env` + 用户手动合并业务密钥（在 DOC-013 给两种姿势） |
| **IMP-014 容器别名 reload** | 容器 stop 时 Caddy 仍反向代理到死端口 → 502；V1 接受此行为（与 static stop 一致），管理页状态可见 |

---

## 5. 实施顺序与提交粒度

按依赖与风险，**每阶段一个提交**：

1. **阶段 1**（Caddy 生命周期）→ 最高优先级，解决稳定性根因，独立可测
2. **阶段 2**（daemon 防污染 + 冗余清理）→ 防止问题再生，依赖阶段 1 的 gateway 自愈
3. **阶段 3**（Scanner + Dockerfile + 资源档位）→ 改善大项目部署，独立于 1/2
4. **阶段 4**（容器别名 + .env + 端口漂移）→ 依赖阶段 3 的 scanner 修正（prd-workflow 正确识别）
5. **阶段 5**（管理页 + 文档 + 任务记录）→ 收尾，依赖前 4 阶段的 API

每阶段完成：补单测 → 跑目标 pytest → 全量回归 → 写 `task-list.md`（DEV/IMP/BUG 前缀）→ 可选 git commit。

---

*本计划由 2026-07-07 运维复盘 + 规划会话整理。执行时以此为依据，偏差需更新本文档并同步 task-list。*
