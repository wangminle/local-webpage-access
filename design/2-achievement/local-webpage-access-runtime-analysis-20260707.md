# LWA Runtime 运维复盘与改进清单

> 分析时间：2026-07-07  
> 范围：`runtime/` 工作区、Caddy 网关、daemon inbox、prd-workflow 部署  
> 触发：路径别名 8080 间歇不可达、管理页出现 12 个实例（仅 3 个有效）

---

## 1. 结论摘要

| 维度 | 现状 | 严重程度 |
| --- | --- | --- |
| **有效实例** | 3 个：`voiceprint-v3-demo`、`prd-workflow`、（`demo-static` 重启后 registry 与 Caddy 状态不一致） | — |
| **冗余实例** | 9 个：均为 inbox 测试 zip 被 daemon **重复 import + 自动改名** 产生 | 高 |
| **Caddy 网关** | 无 LWA 内置生命周期；`caddy reload` 在 master 未运行时直接失败；与 builtin 静态服务端口冲突 | 高 |
| **prd-workflow** | 容器已运行且 API 正常，但 scanner 误判、路径别名需手工配置、业务 `.env` 未注入 | 中 |

**核心判断**：冗余实例不是用户主动部署，而是 `inbox/` 内历史测试 zip + daemon 默认 `on_conflict=rename` 策略叠加 Caddy 启动失败后的重试/import 风暴所致。Caddy 问题本质是 **LWA 只实现了 reload、未实现 start/stop/健康守护**，与运维实际操作（手动 `caddy run` / `caddy start`）脱节。

---

## 2. 实例清单与来源

### 2.1 应保留的实例（3）

| ID | 形态 | 端口 | 用途 |
| --- | --- | --- | --- |
| `voiceprint-v3-demo` | frontend-static | **18004**（原 18001，重启后漂移） | 声纹 V3 演示 |
| `prd-workflow` | python / docker-compose | **18002**（8000→18002） | PRD 需求评审工作流 v0.3.1 |
| `demo-static` | static | **18000** | 开发仓内置 demo（可选保留） |

### 2.2 应清理的冗余实例（9）

| ID | 来源 zip | lastError | 说明 |
| --- | --- | --- | --- |
| `demo-static-2` | `inbox/demo-static.zip` | `Caddy reload 失败` | 与 `demo-static` 同包重复导入 |
| `demo-static-3` | 同上 | 同上 | |
| `demo-static-4` | 同上 | 曾短暂 running 占 18003 | |
| `voiceprint-v3-clean` | `inbox/voiceprint-v3-clean.zip` | `Caddy reload 失败` | 测试包，非生产演示 |
| `voiceprint-v3-clean-2` | 同上 | 同上 | |
| `voiceprint-v3-clean-3` | 同上 | 占 18005 | |
| `voiceprint-v3` | `inbox/voiceprint-v3.zip` | `Caddy reload 失败` | 旧版声纹包 |
| `voiceprint-v3-2` | 同上 | 同上 | |
| `voiceprint-v3-3` | 同上 | 占 18006 | |

**inbox 归档位置**（2026-07-07 手动移出）：  
`runtime/inbox/archived-20260707/`  
含：`demo-static.zip`、`voiceprint-v3-clean.zip`、`voiceprint-v3.zip`

**daemon 已处理标记**：`runtime/run/daemon-processed.json` 仍记录上述 zip 的旧 inbox 路径指纹；zip 若再次放回 inbox 且指纹变化，会 **再次 import 出 `-5`、`-4` 实例**。

### 2.3 冗余产生机制（代码路径）

```
inbox/*.zip
  → daemon poll（5s）
  → Importer.import_zip(on_conflict="rename")   # daemon 默认 rename，非 CLI 的 error
  → slug 冲突 → demo-static-2 / voiceprint-v3-2 ...
  → 识别为 static/frontend-static（tiny）→ daemon 自动 lwa start
  → staticGateway=caddy 但 Caddy master 未运行
  → gateway.enable → caddy reload 失败 → 实例 failed/stopped，registry 条目残留
```

相关代码：

- `daemon.py`：`process_zip` → `importer.import_zip`（无 `--update`，无冲突报错）
- `importer.py`：`_resolve_unique_id` 在 slug 占用时追加 `-2`、`-3`…
- `importer.py`：`on_conflict="error"` 仅 CLI `lwa import` 使用

---

## 3. Caddy 问题深度分析

### 3.1 现象时间线（2026-07-07）

1. **13:40** 声纹 demo 更新成功，日志：`staticGateway=caddy 但未找到 caddy，降级 builtin`
2. **16:43** 安装 Caddy 2.10.0，手动 `nohup caddy run` 启动 8080 别名
3. **16:54** 用户反馈别名不可达 → 根因：**Caddy 进程已退出**，8080 `Connection refused`
4. **16:58** LWA 重启 → 批量 `lwa restart` → 多次 `Caddy reload 失败` → demo-static / voiceprint 变 stopped
5. **16:59~17:01** 改用 `caddy start` + 精简 Caddyfile 后恢复；但 **demo-static registry 仍可能显示 stopped**

### 3.2 根因分类

| # | 根因 | 说明 |
| --- | --- | --- |
| C1 | **无 Caddy master 生命周期** | `StaticGateway.reload_all()` 仅调用 `caddy reload`；首次启用或进程退出后 reload 必失败。LWA 从未调用 `caddy start`/`caddy stop`。 |
| C2 | **builtin 与 Caddy 双栈冲突** | 早期无 Caddy 时，18000/18001 由 Python `http.server` 占用；安装 Caddy 后完整 Caddyfile 同时监听 `:18000`/`:18001`/`:8080`，与 orphan builtin 进程冲突。 |
| C3 | **reload 杀死 run 模式进程** | 日志反复出现 `servers shutting down with eternal grace period` + `stopped previous server`；在 master 未以 systemd/launchd 守护时，一次失败 reload 可导致整个 Caddy 退出。 |
| C4 | **Caddyfile 累积脏配置** | 每次 enable 失败/成功都会在 `static-gateway/sites/` 生成 `demo-static-4.conf`、`voiceprint-v3-3.conf` 等；`_assemble_main_config` import 全部 `*.conf`，加剧端口占用与 reload 失败。 |
| C5 | **版本不满足 doctor** | 已装 Caddy **v2.10.0**，项目要求 **≥ 2.11.2**（`lwa doctor` FAIL）。 |
| C6 | **容器实例别名不在官方 API 内** | `path_alias.set_instance_path_alias` 仅允许 `runtime=shared-static`；`prd-workflow` 容器需手工写 `aliases/*.conf` + 启动 Caddy。 |

### 3.3 Caddy 日志关键片段

`runtime/logs/caddy.log` 中典型模式：

```text
"msg":"server running","name":"srv0" ... addr":":18000"
"msg":"server running","name":"srv2" ... addr":":8080"
"msg":"servers shutting down with eternal grace period"
"msg":"stopped previous server","address":"localhost:2019"
```

说明：reload 触发了旧 server 关停；若新配置无效或端口被占，Caddy 整体不可用。

### 3.4 当前推荐运维姿势（临时）

在 IMP 未落地前：

1. 使用 **`caddy start --config runtime/static-gateway/Caddyfile`**（非 `nohup caddy run`）
2. Caddyfile **仅保留运行中实例**的 site + alias import；定期清理 `sites/` 与 `aliases/` 中废弃 `*.conf`
3. 启用 Caddy 模式前，确认 18000/18001 **无 orphan Python http.server**（`lsof -i :18000`）
4. `local-web.yml` 若坚持 Caddy：升级至 **≥ 2.11.2**（`brew upgrade caddy`）

---

## 4. prd-workflow 专项分析

### 4.1 项目特征

- **技术栈**：FastAPI + SQLite + 内置静态前端（`src/static/`）+ Pi Agent（Node 可选依赖）
- **zip 大小**：约 10MB 源码；Docker 首次 build **~5 分钟**（lancedb / pyarrow / openai 等）
- **容器**：`lwa-prd-workflow`，端口 18002，数据卷 `apps/prd-workflow/data → /app/data`

### 4.2 部署过程问题

| # | 问题 | 详情 | 影响 |
| --- | --- | --- | --- |
| P1 | **Scanner 误判** | zip 含 `package.json`（仅 `@earendil-works/pi-coding-agent`），scanner 优先走 `_detect_node`，因无 frontend/backend 特征 → `pending` + `Kind.STATIC` 错误默认值 | 无法 `import --path-alias`；需手工改 `local-web.json` |
| P2 | **路径别名导入被拒** | `importer` 在 import 时校验：别名仅 `shared-static`；识别为 unknown 时直接 `[ZIP_IMPORT_ERROR]` | 必须先 import，再单独配别名 |
| P3 | **容器别名无官方 API** | `lwa alias set` 要求 `Runtime.SHARED_STATIC` | 容器实例只能手工 Caddy 或未来 IMP |
| P4 | **业务 .env 未注入** | LWA 生成的 `docker/.env` 仅含端口/资源/`DATABASE_URL`；`.env.example` 中 LLM API Key、JWT_SECRET 等 **未写入** | LLM / Embedding / Pi Agent 功能不可用或行为异常 |
| P5 | **Dockerfile 不含 Node** | 自动生成模板仅 `pip install` + `uvicorn src.main:app` | Pi Agent CLI（`node_modules/.bin/pi`）在容器内不可用 |
| P6 | **生产依赖含测试包** | `requirements.txt` 含 `pytest`、`pytest-asyncio` | 镜像体积增大、攻击面增大 |
| P7 | **端口约定不一致** | 项目 `start.sh` 默认 `SERVER_PORT=17957`；LWA 容器固定 **8000** | 仅影响本地脚本习惯，容器内已统一 8000 |
| P8 | **512m 内存档位** | `resourceProfile=small` + `mem_limit=512m` | lancedb + pyarrow 向量检索可能 OOM，需观察 |
| P9 | **静态资源绝对路径** | 前端资源为 `/css/`、`/js/` 绝对路径 | 经 Caddy `handle_path /prd-workflow/*` 去前缀后，**页面 HTML 可加载**；若 SPA 构建使用 `base:'/'` 的 chunk 可能 404（当前传统多页静态，实测 200） |

### 4.3 容器运行日志（正常部分）

`docker logs lwa-prd-workflow` 显示：

- 启动迁移 SQLite、`ToolRegistry` 注册 7 个内置工具
- `/api/health` → 200，`version=0.3.1`
- 用户注册/登录、workspace、chat models 等 API 有真实流量
- 经 8080 别名访问时，静态资源与 API 均 200（Caddy 运行正常时）

### 4.4 prd-workflow build 日志

`apps/prd-workflow/logs/build.log`：Docker build 成功，依赖安装约 9s pip 阶段 + 后续大包（lancedb/pyarrow）耗时主要部分；无 build 失败记录。

---

## 5. 其他日志与观察

| 日志路径 | 内容摘要 |
| --- | --- |
| `runtime/logs/caddy.log` | 多次 start/reload/shutdown 循环；端口从 8080 扩至 18000~18006 |
| `runtime/run/daemon-processed.json` | 3 个 inbox zip 的路径+指纹，已归档但条目未清理 |
| `apps/*/logs/gateway.log` | demo-static / voiceprint 早期 builtin 模式记录 |
| `apps/voiceprint-v3-clean/logs/build.log` | daemon 自动 import 后 `npm ci` + `vite build` 成功，但因 Caddy 失败未对外服务 |
| 管理页截图 | 显示 **V0.4.0**（当前代码 V0.4.1，需 `lwa update` 刷新管理页静态资源） |

---

## 6. 改进项清单（建议纳入 IMP / BUG）

### 6.1 高优先级（P0）— 运维稳定性

| ID | 类型 | 改进项 | 建议方案 |
| --- | --- | --- | --- |
| **IMP-010** | 功能 | **Caddy master 生命周期纳入 LWA** | `lwa manager on` 或独立 `lwa gateway on`：检测 Caddy → 若无进程则 `caddy start` → 写入 pid；`manager off` 可选停 Caddy；reload 失败时自动 start 而非直接报错 |
| **IMP-011** | 功能 | **daemon inbox 导入策略** | 默认改为 `on_conflict=error` 或「同 stem zip 提示 `--update`」；import 成功后 **mv zip → inbox/processed/**；管理页展示 inbox 队列 |
| **IMP-012** | 功能 | **冗余实例批量清理** | `lwa remove <id> --purge` 批量脚本或管理页「删除已停止实例」；remove 时同步删 `static-gateway/sites|aliases/<id>.conf` |
| **BUG-068** | Bug | **Caddy reload 失败不应留下 half-enabled 状态** | enable 失败时回滚 site/alias 片段且 **不释放端口** 或完整回滚；避免 18003~18006 被 failed 实例占用 |
| **BUG-069** | Bug | **restart 时 orphan builtin 进程未清理** | disable 时确保 kill http.server；避免 Caddy 与 builtin 同端口 |

### 6.2 中优先级（P1）— prd-workflow / 大项目

| ID | 类型 | 改进项 | 建议方案 |
| --- | --- | --- | --- |
| **IMP-013** | 功能 | **Scanner：Python + 辅助 package.json** | 当 `requirements.txt` 含 fastapi 且 `package.json` 无 frontend/backend 依赖时，**优先 Python** |
| **IMP-014** | 功能 | **容器实例路径别名** | 扩展 `path_alias` 支持 `docker-compose`：Caddy 反代 hostPort，不要求 shared-static |
| **IMP-015** | 功能 | **业务 .env 合并** | import 时若 zip 含 `.env.example`，提示用户复制到 `docker/.env` 或 `apps/<id>/data/.env`；compose `env_file` 支持多层 |
| **IMP-016** | 功能 | **Python 全栈 Dockerfile 增强** | 检测 `package.json` + `start.sh` 中 pi/node 引用 → 多阶段或 slim+nodejs 镜像；可选 `npm ci` |
| **IMP-017** | 功能 | **生产依赖分离** | Dockerfile 支持 `requirements-prod.txt` 或自动 strip pytest from install |
| **IMP-018** | 功能 | **大项目资源档位** | 含 lancedb/pyarrow/openai 时自动 `resourceProfile=medium`、`mem_limit=1g` |

### 6.3 低优先级（P2）— 体验与可观测性

| ID | 类型 | 改进项 | 建议方案 |
| --- | --- | --- | --- |
| **IMP-019** | 功能 | **管理页实例分组/过滤** | 默认隐藏 `stopped + 无端口` 的 failed 实例；显示「有效 / 冗余」标签 |
| **IMP-020** | 功能 | **Caddy 健康探针** | 管理页 / doctor 增加「8080 别名入口可达」检查；Caddy 挂掉时黄色告警 |
| **IMP-021** | 功能 | **端口漂移可视化** | 路径别名配置存 upstream 端口；restart 后若 hostPort 变化，自动更新 alias conf |
| **DOC-010** | 文档 | **运维 playbook** | Caddy vs builtin 选型、inbox 勿放测试 zip、容器别名手工步骤（直至 IMP-014） |

---

## 7. 建议立即执行的运维动作

### 7.1 清理冗余实例（9 个）

```bash
cd runtime
for id in demo-static-2 demo-static-3 demo-static-4 \
  voiceprint-v3 voiceprint-v3-2 voiceprint-v3-3 \
  voiceprint-v3-clean voiceprint-v3-clean-2 voiceprint-v3-clean-3; do
  lwa remove "$id" --purge
done
```

清理后预期：**实例总数 3**（或 2，若不需要 demo-static）。

### 7.2 清理静态网关脏配置

```bash
cd runtime/static-gateway/sites
# 仅保留 demo-static.conf、voiceprint-v3-demo.conf
# 删除 demo-static-4.conf、voiceprint-v3-*.conf 等

cd ../aliases
# 保留 prd-workflow.conf、voiceprint-v3-demo.conf
```

然后：`caddy reload --config runtime/static-gateway/Caddyfile`（或 `caddy start` 若未运行）。

### 7.3 prd-workflow 业务配置

```bash
# 复制并编辑业务密钥
cp runtime/apps/prd-workflow/current/.env.example \
   runtime/apps/prd-workflow/docker/.env.local
# 将 LLM API Key、JWT_SECRET 写入后，合并进 docker/.env 或 compose env_file

lwa restart prd-workflow
```

### 7.4 防止 inbox 再次污染

- **不要**将测试 zip 长期放在 `runtime/inbox/`
- 或 `lwa daemon off`，改用手动 `lwa import`
- 已归档 zip 保留在 `inbox/archived-20260707/`

### 7.5 升级 Caddy

```bash
brew upgrade caddy   # 目标 ≥ 2.11.2
lwa doctor
```

---

## 8. 有效访问地址（清理 + Caddy 正常后）

| 实例 | 端口直达 | 路径别名（Caddy 8080） |
| --- | --- | --- |
| prd-workflow | http://127.0.0.1:18002/ | http://127.0.0.1:8080/prd-workflow/ |
| voiceprint-v3-demo | http://127.0.0.1:18004/ | http://127.0.0.1:8080/vp-app-demo-v3/ |
| demo-static | http://127.0.0.1:18000/ | — |
| 管理页 | http://127.0.0.1:17800/ | — |

---

## 9. 与现有 IMP 的关系

| 已有 IMP | 本次复盘关系 |
| --- | --- |
| IMP-006 路径别名 | 仅 static 官方支持；容器场景暴露设计缺口 → **IMP-014** |
| IMP-008 lwa update | 管理页仍显示 V0.4.0 时需执行 `lwa update` |
| IMP-009 zip 更新 | prd-workflow 二次部署应使用 `--update prd-workflow`，避免 rename 新实例 |
| daemon WBS-21 | rename 策略 + 不搬移 zip → 冗余实例根因 → **IMP-011** |

---

*文档由 2026-07-07 运维会话整理，对应 task-list OPS-011~013。*
