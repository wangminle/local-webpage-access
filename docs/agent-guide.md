# Agent 接入指南（Agent Guide）

> **状态：本文描述 V0.8.17 当前实际可用的接入方式。**
> LWA 尚未提供 MCP 服务器与 `/api/agent/v1/*` 等专用 Agent 接口——相关能力处于规划实施阶段（见[第 9 节](#9-规划中的-agent-通道未实现)）。在它们落地前，Agent 通过本文所述的既有 HTTP API 与 CLI 工作；**不要**按第 9 节的命令操作，它们尚不存在。V0.8.15 起管理页提供公开发现入口 `GET /llms.txt`、`GET /agent-info.json` 与 `GET /agent-guide`（即本指南精简版），可用来自动确认目标运行的是 LWA。

**读者：** 代表用户操作 LWA 的 LLM Agent（本机或局域网），以及配置、监督这些 Agent 的人。

**一句话定位：** LWA（Local Webpage Access，CLI 命令 `lwa`）是部署在家庭/团队局域网小主机上的本地网页部署基座：把静态站点、前端项目或容器化后端导入工作区，自动构建、托管并生成局域网访问地址。

## 1. 接入前必须知道的三件事

1. **LWA 会执行代码。** 导入的项目可能触发依赖安装、构建脚本与 Docker 镜像构建。静态安全审计（见 [security-boundary.md](security-boundary.md)）会拒绝显式高危配置，但**不是沙箱隔离**。只部署你或用户信任的代码。
2. **定位是可信局域网。** LWA 面向同一所有者/可信团队的家庭或内网环境，不是面向匿名租户的公网托管平台。不要把管理入口暴露到公网。
3. **管理 token 是完整管理权限。** 当前没有按 Agent 划分的细粒度授权：持有 token 即可读写全部实例。拿到 token 的 Agent 等同于管理员，请像对待管理员密码一样对待它。

## 2. 如何找到服务、确认连到哪个工作区

服务入口由用户提供（不要自行扫描局域网并信任发现结果）：

- **管理 API 基址**：`http://<主机>:17800/`（默认端口 `17800`，配置项 `managerPort` 可改；以用户给定的 URL 为准）。
- **确认服务与版本**：`GET /api/health` **无需鉴权**，任何客户端都可调用，返回 `{"ok": true, "version": "...", "profile": ..., "overall": ...}`。先用它确认地址正确、服务在线。
- **确认工作区**：本机（loopback）访问时 health 额外返回 `workspaceRoot`。一台主机理论上可有多个 LWA 工作区、各自独立 manager 与 token——**连接前向用户确认目标工作区**，避免把项目部署进错误的工作区。
- **确认能力**：`GET /api/capability`（需鉴权）返回 Docker、Caddy 等依赖的就绪状态。部署容器项目前先看它，缺 Docker 时应报告能力缺口，而不是尝试自行安装。

## 3. 鉴权与权限获取

### 3.1 token 从哪里来

token 由 LWA 工作区管理员提供，存放于服务器工作区 `run/manager-token.json`（权限 0600，仅工作区所有者可读）。管理员在服务器本机可用 `lwa manager token`（或 `--json`）查看当前 token 与轮换时间；token 会周期性自动轮换（见 §3.4）。

**作为 Agent：向用户（管理员）申请 token，不要尝试读取文件自行获取；没有 token 的回环只读探测之外的任何写操作都会被拒绝。**

### 3.2 token 如何传递

| 方式 | 用法 | 说明 |
| --- | --- | --- |
| `Authorization: Bearer <token>` | 标准头 | **推荐** |
| `X-LWA-Token: <token>` | 专用头 | 等效 |
| `?token=<token>` | URL 查询参数 | 仅为浏览器便利通道，会进入历史/日志，Agent 不要使用 |

### 3.3 谁必须带 token（精确规则）

| 客户端位置 | 方法 | 是否免 token |
| --- | --- | --- |
| 本机 loopback（127.0.0.1 / localhost / ::1，且 Host 头为本机名） | GET / HEAD / OPTIONS | 免 |
| 本机 loopback | 写方法（POST/PATCH/DELETE） | **不免**：非浏览器客户端无 token 会得到 `403 csrf_forbidden`（浏览器同源请求除外） |
| 局域网其他机器 | 任何方法 | **全部需要 token**，缺失/无效返回 `401 unauthorized` |

注意：本机免 token 读仅是调试便利，**不等于**"本机进程天然拥有管理权限"；本机脚本写请求同样要带 token。

### 3.4 token 会自动轮换

默认每 7 天（168 小时，配置 `managerTokenRotateHours`）manager 自动轮换 token，旧 token 立即失效、无需重启。Agent 遇到原本正常的请求突然返回 401 时，应考虑 token 已被轮换，向管理员重新获取，而不是重试或降级为无 token 请求。

### 3.5 错误语义

- `401 unauthorized`：token 无效或缺失 → 获取/更新凭据后重试。
- `403 csrf_forbidden`：回环地址的非同源写请求缺 token → 补 token。
- 其余错误统一为 `{"error": {"code": ..., "message": ..., "detail": ...}}` 结构。

## 4. 现有 HTTP API 能做什么

完整接口契约见管理页自带的 OpenAPI：`/docs`（Swagger UI）、`/openapi.json`。部分历史请求体是弱类型的 `dict[str, Any]`，语义以 [manager-page.md](manager-page.md) 与 OpenAPI 描述为准。

### 4.1 常用只读端点（GET）

| 端点 | 用途 |
| --- | --- |
| `/api/health` | 存活与版本（免鉴权，见 §2） |
| `/api/capability` | 依赖能力就绪状态（Docker/Caddy 等） |
| `/api/instances` | 实例列表与摘要 |
| `/api/instances/{id}` | 单实例详情 |
| `/api/instances/{id}/logs` | 构建与运行日志（分页） |
| `/api/instances/{id}/resources` | 资源档位信息 |
| `/api/stats` / `/api/pageviews*` | 统计与浏览量 |
| `/api/pending` / `/api/redundant` | 待确认项 / 冗余实例 |

### 4.2 常用写端点（POST/PATCH）

| 端点 | 用途 |
| --- | --- |
| `/api/import-from-dir` / `/api/import-from-git` | 新建导入（输入限制见 §5） |
| `/api/instances/{id}/update` / `update-from-dir` / `update-from-git` | 更新既有实例 |
| `/api/instances/{id}/start` / `stop` / `restart` / `rebuild` / `cancel-build` | 生命周期操作 |
| `/api/instances/{id}/settings` | 实例配置（buildEnv、followAliasBase 等） |
| `/api/instances/{id}/path-alias` | 路径别名 |
| `/api/instances/{id}/remove` | 移除实例（破坏性，先向用户确认） |
| `/api/access/refresh` / `/api/gateway/switch` | 访问地址刷新 / 网关切换 |

### 4.3 重试与并发的现状（重要）

当前写操作**没有** operation 对象与幂等键：请求超时后重试同一创建操作可能产生重复实例。稳妥做法是——写之前先 `GET /api/instances` 查目标是否已存在；超时后先查询实际结果再决定重试。多客户端并发由内部实例锁串行化，不会损坏数据，但"最后写入获胜"，更新前应确认实例当前状态符合预期。专门的 plan/apply + operation 语义是规划能力（§9），尚未提供。

## 5. 部署输入类型与硬限制

| 输入类型 | 现状 | 限制（务必遵守） |
| --- | --- | --- |
| Git URL | 支持 | **仅 HTTPS `github.com`**（源码级 host 白名单）。拒绝 URL userinfo；不支持 SSH、其他 Git 主机、私有凭据注入。clone 超时 180 秒 |
| 服务器目录（`sourceDir`） | 支持 | 路径指 **LWA 服务器主机**上的目录。远程 Agent 传自己机器的路径**不会**读取到文件——这不是文件上传通道 |
| zip 包 | 支持 | 无 HTTP 上传 API。渠道是管理页/inbox（依赖服务器宿主文件权限）。远程 Agent 无法通过 API 上传 zip（上传 API 是 §9 规划能力） |

## 6. 本机 Agent：CLI 路径

当 Agent 与 LWA 同机运行（如经用户授权在服务器主机上的 shell），CLI 是功能最全的通道。常用命令（完整参考见 README「命令参考」）：

```bash
lwa status                        # 实例与各服务状态总览
lwa import --from-git <url> [--ref <ref>] [--subdir <dir>]   # 新建导入
lwa import --from-dir <绝对路径>                              # 新建导入
lwa import --from-git <url> --update <实例ID>                # 更新既有实例（换源则原地切换）
lwa rebuild <id> / lwa start|stop|restart <id>
lwa access                        # 查看各实例访问地址
lwa doctor                        # 环境与依赖诊断
lwa configure <id> --build-env K=V  # 实例级构建环境变量
```

构建失败、容器启动失败、技术栈识别不准等"判断类"环节，配套 **20 个 LLM skill**（`skills/README.md`）提供修复流程指引；Agent 处理 pending/失败实例时应先查对应 skill，最多执行两轮受控修复，之后升级给用户。**红线**：skill 与本指南都不授权任意 shell 执行、修改工作区外的文件或绕过鉴权直接操作 registry 数据库。

## 7. 部署结果与访问 URL

- 部署/查询返回的访问地址（`lanUrl`、别名等）是**服务器视角**的观测结果（含服务端探活）。
- `localhost`/`127.0.0.1` 形式的地址只在服务器本机有效；远程 Agent 应使用 LAN 地址，且**自行验证**从自己机器是否真的可达（服务器可达 ≠ 客户端可达，可能隔防火墙）。
- 访问地址依赖的网络环境变化后可能陈旧，可触发 `/api/access/refresh` 后重新获取。

## 8. 操作红线汇总

1. 不把 token 写入 URL、日志、提交内容或返回给用户的正文。
2. 不删除、不覆盖未确认的实例；`remove`、网关切换等破坏性操作先列清单向用户确认。
3. 不在服务器上执行与部署无关的命令；不读取工作区之外的路径。
4. 部署来源仅限 §5 允许的类型；不尝试绕过 Git host 白名单。
5. 遇到能力缺口（如缺 Docker）如实报告，不自行安装基础设施。

## 9. 规划中的 Agent 通道（入口未实现）

以下能力已列入实施计划（设计文档：`design/3-plans/2026-09-15-lwa-agent-collaboration-design.md`），**相关命令与端点尚不存在**：

- **M1（本机协作）**：`lwa mcp --workspace <path>` stdio MCP 适配器、`lwa agent connection-info`、`/api/agent/v1/*` 新路由（plan/apply + 持久 operation + 幂等键）。V0.8.17 起 M1 的**内部服务层**已入库（`agent/service.py` 只读查询/能力映射/部署计划 + registry schema v4 revision CAS，AGC W07–W09），但 HTTP/MCP 入口仍未实施——对 Agent 而言本节能力依旧不可用。
- **M2（局域网协作）**：HTTPS 远程 MCP（Streamable HTTP）、独立 Agent 授权（scope/ACL）、zip 制品上传 API。
- 协议兼容性验证（AGC-W01）已完成：目标 MCP 协议版本 2026-07-28，兼容旧握手至 2024-11-05。

本文将在上述能力落地时更新，未实现条目不会混入前述操作章节。

## 10. 参考

- [manager-page.md](manager-page.md)——管理页与 API 鉴权细节
- [security-boundary.md](security-boundary.md)——安全审计分级与默认保护
- [known-limitations.md](known-limitations.md)——已知限制
- [operations-playbook.md](operations-playbook.md)——日常运维手册
- [runtime-workspace.md](runtime-workspace.md)——工作区目录与端口结构
- README「命令参考」——CLI 全量命令
