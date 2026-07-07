# 管理页说明（WBS-30.06）

`lwa manager start` 启动内置管理页，提供图形化的实例管理与监控能力。
管理页由 FastAPI 后端（`src/local_webpage_access/manager_api.py`）与单页前端
（`src/local_webpage_access/manager_static/`）组成。

## 启动

```bash
lwa manager start
```

* 默认监听 `0.0.0.0:17800`（由 `local-web.yml` 的 `managerPort` / `managerHost` 控制）。
* 前台运行，`Ctrl+C` 退出。
* 首次启动自动生成访问 token 并打印到终端，例如：

  ```
  管理 token：ab12cd34-...
  请访问 http://192.168.1.10:17800/ 并输入上述 token
  ```

* token 同时写入工作区 `run/` 目录，便于事后查阅。

浏览器打开打印的 URL，在登录框输入 token 即可进入管理页。

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
| GET | `/api/instances` | 实例列表（先观测回写状态再取快照） |
| GET | `/api/instances/{id}` | 实例详情：状态快照 + manifest + 构建/事件/资源记录 |
| GET | `/api/instances/{id}/logs?category=&tail=` | 日志内容（build/run/gateway/import/scan） |
| GET | `/api/instances/{id}/resources` | 实例级资源占用 |
| POST | `/api/instances/{id}/start` | 启动实例 |
| POST | `/api/instances/{id}/stop` | 停止实例 |
| POST | `/api/instances/{id}/restart` | 重启实例 |
| POST | `/api/instances/{id}/rebuild` | 重建实例（经构建队列限流） |
| POST | `/api/instances/{id}/update` | 用 inbox 内新 zip 原地更新实例（IMP-009） |
| PATCH | `/api/instances/{id}/path-alias` | 设置或清除静态实例路径别名（IMP-006） |
| GET | `/api/pending` | pending 与 failed 实例队列 |
| GET | `/api/port-pool` | 端口池占用摘要 |

### 错误格式

所有错误统一为：

```json
{"error": {"code": "not_found", "message": "实例 xxx 不存在"}}
```

常见 code：`unauthorized`、`not_found`、`bad_request`、`lifecycle_error`、`internal`。

### 实例更新（IMP-009）

```http
POST /api/instances/{id}/update
Authorization: Bearer <token>
Content-Type: application/json

{"zipPath": "foo-v2.zip", "restart": true, "keepData": true, "forceKindChange": false}
```

* `zipPath`：相对路径以 `inbox/` 为根；也支持 inbox 内的绝对路径。
* 成功响应含 `skipped` / `rebuilt` / `restarted` 与最新 `instance` 快照。
* 与 CLI `lwa import inbox/foo.zip --update <id>` 共用 `importer.update_zip` 代码路径。

### 路径别名（IMP-006）

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

* 仅 **shared-static** 实例可用；容器实例返回 `400`。
* slug 格式：`^[a-z0-9]+(-[a-z0-9]+)*$`，长度 ≤ 63。
* 保留字（如 `api`、`health`）与全局唯一性校验；改别名时**排除当前实例自身**。
* 写入 manifest `static.routeMode` / `routeHost` 与 `network.routeMode` / `routeHost` / `routeUrl`；同步 registry。
* 实例 **running** 且静态后端为 **Caddy** 时，regenerate `static-gateway/aliases/<id>.conf` 并 `reload_all`。
* **builtin** 模式：别名登记成功，但统一入口不可用，响应 `aliasEntryEnabled: false`（仍可通过 hostPort 访问）。
* **SPA 限制**：构建产物若使用绝对路径资源（如 `/assets/app.js`），在 `/<alias>/` 下可能 404；相对路径或 Vite `base: './'` 等配置可正常使用。

列表与详情 API 额外返回（IMP-007 / IMP-006）：

| 字段 | 说明 |
| --- | --- |
| `hostPort` | 实例宿主端口 |
| `internalPort` | manifest 中的内部/期望端口（容器或 scanner 识别） |
| `portMappingLabel` | 形如 `33001 → 18001` 的映射说明 |
| `routeHost` | 路径别名 slug（无则为 null） |
| `routeUrl` | 统一入口 URL（`routeMode=name` 且 Caddy 可用时） |

## 前端功能

单页前端（`/`）提供：

* **概览面板**：实例总数、各状态计数、类型分布、主机 CPU/内存/磁盘、端口池占用。
* **实例列表**：每行显示 ID、名称、形态、状态、端口映射、访问地址（hostPort 与路径别名入口），附带日志 / **路径别名** / start / stop / restart / rebuild 操作。
* **路径别名对话框**（V0.4.1）：静态实例操作区「路径别名」按钮；输入 slug 保存或清除；校验错误在对话框内展示；容器实例与 pending/building/queued 态置灰。
* **实例详情**：manifest、构建记录、事件流、资源占用、分类日志查看器；含路径别名说明与 CLI 等价命令提示。
* **待处理区**：pending 实例（可重扫 `lwa scan`）与 failed 实例（显示 `lastError`）。

## 与 CLI 一致性

管理页的生命周期操作直接调用 `local_webpage_access.lifecycle` 的同名函数，
**与 CLI `lwa start/stop/restart/rebuild` 走完全相同的代码路径**（验收标准 3）。
路径别名与 zip 更新分别调用 `path_alias.set_instance_path_alias` 与 `importer.update_zip`，
与 CLI `lwa alias set/clear`、`lwa import --update` 一致。
因此管理页展示的状态与 `lwa status` 始终一致。

## 绑定安全

`managerHost` 默认 `0.0.0.0`（便于局域网访问）。`local_webpage_access/security.py`
的 `validate_manager_binding` 会在启动时校验：若绑定到 LAN/通配地址，
**必须存在 token**，否则拒绝启动。详见 [安全边界](security-boundary.md)。
