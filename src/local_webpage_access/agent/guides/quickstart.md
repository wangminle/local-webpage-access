# LWA Agent 接入快速指南

> 适用版本：{{PRODUCT_VERSION}}。本指南面向代表用户操作 LWA 的 LLM Agent。
> LWA（Local Webpage Access，CLI `lwa`）是局域网小主机上的本地网页部署基座：
> 导入静态站点 / 前端项目 / 容器化后端，自动构建、托管并生成访问地址。
> 完整版见仓库 `docs/agent-guide.md`。

## 0. 三条底线

1. **LWA 会执行代码**（依赖安装、构建脚本、Docker 构建）；静态安全审计不是沙箱。只部署可信代码。
2. **定位是可信局域网**，不是公网托管平台；不要把入口暴露公网。
3. **管理 token = 完整管理权限**，像对待管理员密码一样对待它。

## 1. 找到服务并确认工作区

- 服务基址由用户提供（默认端口 `17800`）。
- `GET /api/health` 免鉴权：`{"ok": true, "version": ...}`。先探活再干活。
- 一台主机可能有多个工作区（各自独立 manager/token）；部署前向用户确认目标工作区。
- `GET /api/capability`（需鉴权）确认 Docker 等能力；缺 Docker 时报告能力缺口，不要自行安装。

## 2. 鉴权

向用户（工作区管理员）申请 token；不要试图自行读取服务器上的 token 文件。

| 传递方式 | 用法 | 说明 |
| --- | --- | --- |
| `Authorization: Bearer <token>` | 标准头 | 推荐 |
| `X-LWA-Token: <token>` | 专用头 | 等效 |
| `?token=` | URL 参数 | 不要用（会进日志/历史） |

精确规则：

- 本机 loopback 的 GET/HEAD/OPTIONS 免 token；本机**写**请求仍需 token（无 token 得 `403 csrf_forbidden`）。
- 局域网其他机器一律需要 token（否则 `401 unauthorized`）。
- token 默认每 7 天自动轮换；突然 401 先考虑 token 已换，向管理员重新获取。

## 3. 部署输入限制（硬规则）

| 输入 | 规则 |
| --- | --- |
| Git | 仅 HTTPS `github.com`；不支持 SSH、其他 Git 主机、凭据注入；clone 超时 180s |
| 目录（sourceDir） | 指 **LWA 服务器**上的路径；远程 Agent 传本机路径无效（不是上传通道） |
| zip | 无 HTTP 上传 API；渠道是管理页/inbox（需服务器宿主文件权限） |

## 4. 现有 API 要点

- OpenAPI：`/docs`、`/openapi.json`。
- 只读：`GET /api/instances`、`/api/instances/{id}`、`/api/instances/{id}/logs`、`/api/stats`、`/api/pending` 等。
- 写：`POST /api/import-from-dir`、`/api/import-from-git`、`/api/instances/{id}/start|stop|restart|rebuild|update*`、`PATCH /api/instances/{id}/settings`、`POST /api/instances/{id}/remove`（破坏性，先确认）。
- **当前没有幂等键**：创建类操作超时后盲目重试可能产生重复实例——写之前先查列表，超时后先查结果再决定。
- 错误统一为 `{"error": {"code", "message", "detail"}}`。

## 5. 结果与 URL

返回的访问地址是**服务器视角**观测；`localhost` 地址仅服务器本机有效；远程 Agent 拿 LAN 地址后应自行验证可达性。

## 6. M1 本机 Agent 专用通道（本机可用）

同机 Agent 优先使用专用通道（仅回环 + token）：

- 接入引导：`lwa agent connection-info --workspace <绝对路径> --json` → `apiBase`/`workspaceId`/契约版本/配置自检；响应 `workspaceId` 须与其一致。
- 鉴权：仅回环连接 + 有效管理 token（`Authorization: Bearer` 头；回环**不**免 token；非回环一律 `403`）。
- HTTP：基址 `/api/agent/v1`——`GET /capabilities|/instances|/instances/{id}|/instances/{id}/access-urls|/logs?instanceId=…`，`POST /plans`（计划）→ `POST /deployments`（应用，202+operation），`POST /instances/{id}/start|stop|restart|rebuild`，`GET /operations/{id}`、`POST /operations/{id}/cancel`。
- MCP stdio：`lwa mcp --workspace <绝对路径>`（工具与端点一一对应；需 `pip install 'local-webpage-access[mcp]'`）。
- 写操作必带 `idempotencyKey`（同键同内容安全重试）；update/生命周期带 `expectedRevision`（冲突得 `revision_conflict`，先取最新 revision）。
- 部署源：`server_directory` 限 `agent.allowedSourceRoots` 内；`git` 仅 HTTPS github.com；`artifact` 属 M2 未开放。

## 7. 规划中（M2 未实现，勿调用）

远程 MCP（Streamable HTTP）、独立 Agent 授权、上传 API 均在实施计划中，当前版本不存在。本机 MCP stdio 通道的可用性以实际探测为准：`/agent-info.json` 的 `mcp.enabled` 按运行环境探测得出（为 false 时先 `pip install 'local-webpage-access[mcp]'` 再复核，不要仅凭一次 false 放弃接入）；接入命令模板见同响应的 `mcp.command`，接入引导用 `lwa agent connection-info`。
