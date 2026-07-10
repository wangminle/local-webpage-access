# 管理页说明（WBS-30.06）

默认用 `lwa manager on` 后台启动管理页（`lwa init` 在 `managerEnabled=true` 时也会自动拉起）。
需要前台调试时可用 `lwa manager start`。
管理页由 FastAPI 后端（`src/local_webpage_access/manager_api.py`）与单页前端
（`src/local_webpage_access/manager_static/`：Vue 3 + `boot.js` / `helpers.js` / vendored Vue）组成。

## 启动

```bash
lwa manager on          # 推荐：后台启动（默认流程）
lwa manager status      # 查看是否在跑
lwa manager off         # 停止
# 前台调试（Ctrl+C 退出）：
# lwa manager start
```

* 默认监听 `0.0.0.0:17800`（由 `local-web.yml` 的 `managerPort` / `managerHost` 控制）。
* **本机访问免 token**：浏览器打开 http://127.0.0.1:17800/ 即可进入（IMP-003）。
* 从局域网 IP 访问时仍须 token。token 写入工作区 `run/` 目录；`lwa manager on` /
  `lwa manager start` 首次启动会生成并打印，例如：

  ```
  管理 token：ab12cd34-...
  请访问 http://192.168.1.10:17800/ 并输入上述 token
  ```

* 也可事后查阅 `run/` 下的 token 文件。

## 鉴权

* 所有 `/api/*` 路由（`/api/health` 除外）默认要求请求头 `Authorization: Bearer <token>`（WBS-22.12）。
* **本机调试例外（IMP-003）**：从 `127.0.0.1` / `localhost` / `::1` 访问时免 token；从局域网 IP 访问时仍须 token。
* 缺失或错误 token 返回 `401`，统一错误格式 `{"error": {"code": "unauthorized", "message": "..."}}`。
* token 为一次性生成的随机串，仅在本工作区有效；重置方式：删除 `run/` 下的 token 文件后重启管理页。

## API 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查（无需 token） |
| GET | `/api/stats` | 顶部统计：实例计数、类型分布、数据库实例数、端口池、主机资源 |
| GET | `/api/instances` | 实例列表（先观测回写状态再取快照；含 `redundant` 布尔字段，IMP-019） |
| GET | `/api/instances/{id}` | 实例详情：状态快照 + manifest + 构建/事件/资源记录 |
| GET | `/api/instances/{id}/logs?category=&tail=` | 日志内容（build/run/gateway/import/scan） |
| GET | `/api/instances/{id}/resources` | 实例级资源占用 |
| POST | `/api/instances/{id}/start` | 启动实例 |
| POST | `/api/instances/{id}/stop` | 停止实例 |
| POST | `/api/instances/{id}/restart` | 重启实例 |
| POST | `/api/instances/{id}/rebuild` | 重建实例（经构建队列限流） |
| POST | `/api/instances/{id}/recover` | 一键恢复 `gateway_down`/`config_invalid` 实例（DEV-043）：先拉起 Caddy master 再 restart |
| POST | `/api/instances/{id}/update` | 用 inbox 内新 zip 原地更新实例（IMP-009） |
| POST | `/api/instances/{id}/remove?purge=&force=` | 移除单个实例（IMP-019）；默认仅清 registry，`purge=true` 删磁盘，非空 `data/` 需 `force=true` |
| PATCH | `/api/instances/{id}/path-alias` | 设置或清除路径别名（IMP-006 / IMP-014 / IMP-022） |
| GET | `/api/instances/{id}/pageviews?limit=` | 单实例浏览量详情：按天分布 + 最近命中（IMP-024） |
| GET | `/api/pageviews` | 全部实例浏览量汇总（惰性摄入日志后返回，IMP-024） |
| GET | `/api/redundant` | 冗余实例列表（同 `sourceZipHash` 分组中非最早者，IMP-019） |
| POST | `/api/redundant/remove?purge=&force=` | 批量移除冗余实例，保留每组最早者（IMP-019） |
| GET | `/api/pending` | pending 与 failed 实例队列 |
| GET | `/api/port-pool` | 端口池占用摘要 |

### 错误格式

所有错误统一为：

```json
{"error": {"code": "not_found", "message": "实例 xxx 不存在"}}
```

常见 code：`unauthorized`、`not_found`、`bad_request`、`lifecycle_error`、`recognition_error`、`internal`。

### 实例更新（IMP-009）

```http
POST /api/instances/{id}/update
Authorization: Bearer <token>
Content-Type: application/json

{"zipPath": "foo-v2.zip", "restart": true, "keepData": true, "forceKindChange": false}
```

* `zipPath`：相对路径以 `inbox/` 为根；也支持 inbox 内的绝对路径。
* 成功响应含 `skipped` / `rebuilt` / `restarted` 与最新 `instance` 快照。
  - **容器**（`runtime=docker-compose`）且 `restart=true`、原为 running：走 **rebuild**（`rebuilt=true`），不轻量 restart。
  - **静态 / 前端**：`restarted=true`。
  - `restart=false`（对应 CLI `--no-restart`）：只换源码；容器需稍后 `lwa rebuild` / `POST .../rebuild`。
* 与 CLI `lwa import inbox/foo.zip --update <id>` 共用 `importer.update_zip` 代码路径。

### 路径别名（IMP-006 / IMP-014 / IMP-022）

```http
PATCH /api/instances/{id}/path-alias
Authorization: Bearer <token>
Content-Type: application/json

{"alias": "voiceprint-demo"}
```

清除别名：

```json
{"alias": null}
```

规则与 CLI `--path-alias` / `lwa alias set` **完全一致**：

* **适用形态**：`shared-static`（纯静态 / 前端构建产物）与 **`docker-compose` 容器实例**（IMP-014）均可设置；其它形态返回 `400`。
* **Caddy 硬依赖（IMP-022）**：设置别名（`alias` 非 null）时，静态后端必须为 **caddy**。`builtin`（或 caddy 未安装而降级）下会 **明确报错拦截**，不再无声写元数据。清除别名（`alias: null`）在任何后端下均允许。
* slug 格式：`^[a-z0-9]+(-[a-z0-9]+)*$`，长度 ≤ 63。
* 保留字（如 `api`、`health`）与全局唯一性校验；改别名时**排除当前实例自身**。
* 写入 manifest `static.routeMode` / `routeHost`（或容器侧 network 字段）与 `network.routeMode` / `routeHost` / `routeUrl`；同步 registry。
* 实例 **running** 且后端为 **Caddy** 时，regenerate `static-gateway/aliases/<id>.conf` 并 `reload_all`。
* **SPA 限制（IMP-023）**：构建产物若使用绝对路径资源（如 `/assets/app.js`），在 `/<alias>/` 下可能 404；相对路径或 Vite `base: './'` 等配置可正常使用。

列表与详情 API 额外返回（IMP-007 / IMP-006 / IMP-019）：

| 字段 | 说明 |
| --- | --- |
| `hostPort` | 实例宿主端口 |
| `internalPort` | manifest 中的内部/期望端口（容器或 scanner 识别） |
| `portMappingLabel` | 形如 `33001 → 18001` 的映射说明 |
| `routeHost` | 路径别名 slug（无则为 null） |
| `routeUrl` | 统一入口 URL（`routeMode=name` 且 Caddy 可用时） |
| `lanUrl` | 局域网直达 URL（`http://<LAN-IP>:<hostPort>`） |
| `localhostUrl` | 本机回环兜底 URL（`http://127.0.0.1:<hostPort>/`，LAN 不通时可用） |
| `redundant` | 是否为同 zip 指纹分组中的冗余实例（非最早者，IMP-019） |

### 浏览量统计（IMP-024）

```http
GET /api/pageviews
Authorization: Bearer <token>
```

响应形如 `{"instances": {"<id>": {"hits": N, "uniqueIps": N, "lastSeen": "...", "source": "caddy|builtin|container"}}}`。
请求时惰性摄入最新访问日志（Caddy 统一入口 JSON log、builtin `gateway.log`、容器 stdout 尽力解析），再返回聚合。

```http
GET /api/instances/{id}/pageviews?limit=50
Authorization: Bearer <token>
```

返回按天分布（`byDay`）与最近命中明细（`recent`）。数据落在工作区 `run/pageviews.db`；Caddy 别名入口 access log 为 `logs/static-access.log`。

> 容器路径为尽力解析，数字可能近似；直连 hostPort 的访问默认不计入 Caddy 别名入口统计。

### 冗余实例（IMP-019）

```http
GET /api/redundant
Authorization: Bearer <token>
```

返回 `{"instances": [...], "count": N}`，每项含 `id` / `name` / `sourceZipHash` / `createdAt`。

```http
POST /api/redundant/remove?purge=false&force=false
Authorization: Bearer <token>
```

批量移除冗余（保留每组最早导入者），与 CLI `lwa remove --redundant` 同路径。`purge` / `force` 语义同单个 `remove`。

## 前端功能

单页前端（`/`，Vue 3）提供：

* **概览面板**：实例总数、各状态计数（含「需恢复」）、类型分布、主机 CPU/内存/磁盘、端口池占用。
* **实例列表**：每行显示名称（冗余实例带「冗余」徽章与行高亮）、状态、期望态、形态、运行层、技术栈、访问地址、端口、资源、**浏览量**、更新时间；操作区含日志 / **路径别名** / 浏览量详情 / start / stop / restart / rebuild / **删除**；状态为 `网关不可达`（gateway_down）或 `配置无效`（config_invalid）时额外显示「恢复」按钮（DEV-043）。
* **筛选**：按状态 / 形态搜索；「仅待处理/失败」与「仅冗余」勾选；顶部可「批量删除冗余」。
* **路径别名对话框**：`shared-static` 与 `docker-compose` 实例操作区「路径别名」按钮可用（pending/building/queued 态禁用）；输入 slug 保存或清除；校验错误在对话框内展示。builtin 后端下设置会失败并展示后端错误信息（IMP-022）。
* **浏览量**：列表列展示累计访问；点击打开按天分布与最近命中弹窗（IMP-024）。

> **状态说明（DEV-043 / BUG-071）**：Caddy 模式下，enabled 静态实例在 master（admin :2019）不可达时显示 `网关不可达`，在 master 在线但站点端口不通时显示 `配置无效`——二者均不再被误标为普通「已停止」。点击「恢复」会先尝试拉起 Caddy master 再 restart 实例。

* **实例详情**：manifest、构建记录、事件流、资源占用、分类日志查看器；含路径别名说明与 CLI 等价命令提示。
* **待处理区**：pending 实例（可重扫 `lwa scan`）与 failed 实例（显示 `lastError`）。

## 与 CLI 一致性

管理页的生命周期操作直接调用 `local_webpage_access.lifecycle` 的同名函数，
**与 CLI `lwa start/stop/restart/rebuild/remove` 走完全相同的代码路径**（验收标准 3）。
路径别名与 zip 更新分别调用 `path_alias.set_instance_path_alias` 与 `importer.update_zip`，
与 CLI `lwa alias set/clear`、`lwa import --update` 一致；冗余清理与 `lwa remove --redundant` 一致。
因此管理页展示的状态与 `lwa status` 始终一致。

## 绑定安全

`managerHost` 默认 `0.0.0.0`（便于局域网访问）。`local_webpage_access/security.py`
的 `validate_manager_binding` 会在启动时校验：若绑定到 LAN/通配地址，
**必须存在 token**，否则拒绝启动。详见 [安全边界](security-boundary.md)。

## 相关文档

- [运维手册](operations-playbook.md) — 网关选型、冗余清理、容器别名、浏览量与 Caddy 排障
- [Runtime 工作区说明](runtime-workspace.md)
