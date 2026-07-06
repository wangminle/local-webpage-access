# 管理页说明（WBS-30.06）

`lwa manager start` 启动内置管理页，提供图形化的实例管理与监控能力。
管理页由 FastAPI 后端（`src/local_web_access/manager_api.py`）与单页前端
（`src/local_web_access/manager_static/`）组成。

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

* 所有 `/api/*` 路由（`/api/health` 除外）都要求请求头 `Authorization: Bearer <token>`（WBS-22.12）。
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
| GET | `/api/pending` | pending 与 failed 实例队列 |
| GET | `/api/port-pool` | 端口池占用摘要 |

### 错误格式

所有错误统一为：

```json
{"error": {"code": "not_found", "message": "实例 xxx 不存在"}}
```

常见 code：`unauthorized`、`not_found`、`bad_request`、`lifecycle_error`、`internal`。

## 前端功能

单页前端（`/`）提供：

* **概览面板**：实例总数、各状态计数、类型分布、主机 CPU/内存/磁盘、端口池占用。
* **实例列表**：每行显示 ID、名称、形态、状态、端口、LAN URL，附带 start/stop/restart/rebuild 操作按钮。
* **实例详情**：manifest、构建记录、事件流、资源占用、分类日志查看器。
* **待处理区**：pending 实例（可重扫 `lwa scan`）与 failed 实例（显示 `lastError`）。

## 与 CLI 一致性

管理页的生命周期操作直接调用 `local_web_access.lifecycle` 的同名函数，
**与 CLI `lwa start/stop/restart/rebuild` 走完全相同的代码路径**（验收标准 3）。
因此管理页展示的状态与 `lwa status` 始终一致。

## 绑定安全

`managerHost` 默认 `0.0.0.0`（便于局域网访问）。`local_web_access/security.py`
的 `validate_manager_binding` 会在启动时校验：若绑定到 LAN/通配地址，
**必须存在 token**，否则拒绝启动。详见 [安全边界](security-boundary.md)。
