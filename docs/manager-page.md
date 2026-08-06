# 管理页说明（WBS-30.06）

默认用 `lwa manager on` 后台启动管理页（`lwa init` 在 `managerEnabled=true` 时也会自动拉起）。
需要前台调试时可用 `lwa manager start`。
管理页由 FastAPI 后端（`src/local_webpage_access/manager_api.py`）与单页前端
（`src/local_webpage_access/manager_static/`：Vue 3 + `boot.js` / `helpers.js` / vendored Vue）组成。

## 启动

```bash
lwa manager on          # 推荐：后台启动（默认流程）
lwa manager status      # 查看是否在跑
lwa manager logs        # 查看管理页运行时日志（logs/manager.log）
lwa manager off         # 停止
# 前台调试（Ctrl+C 退出）：
# lwa manager start
```

* 默认监听 `0.0.0.0:17800`（由 `local-web.yml` 的 `managerPort` / `managerHost` 控制）。
* **本机读免 token、写需同源或 token**：浏览器打开 http://127.0.0.1:17800/ 即可进入（IMP-003）；本机 `GET`/`HEAD`/`OPTIONS` 免 token，**写操作**（POST/PATCH/…）须同源 Fetch Metadata（`Sec-Fetch-Site: same-origin`）或匹配的 `Origin`，否则 403 `csrf_forbidden`；也可直接带 token。
* 从局域网 IP 访问时仍须 token。token 写入工作区 `run/manager-token.json`（权限 `0600`）；
  `lwa manager on` / `lwa manager start` 首次启动会生成并**仅在终端打印**（不会写入
  `logs/lwa.log` / `logs/manager.log`），例如：

  ```
  token：ab12cd34-...
  本机：http://127.0.0.1:17800/
  局域网：http://192.168.1.10:17800/
  ```

* 也可事后查阅 `run/manager-token.json`。重置：删除该文件后重启管理页。
* 页眉使用 `manager_static/logo.svg`；浏览器标签栏图标为 `favicon.png`。

## 鉴权

* 所有 `/api/*` 路由（`/api/health` 除外）默认要求请求头 `Authorization: Bearer <token>`（WBS-22.12）。
* 另支持 `X-LWA-Token` 头，以及查询参数 `?token=`（管理页打开新标签等场景；见下方泄漏面说明）。
* **本机调试例外（IMP-003 / BUG-295）**：从 `127.0.0.1` / `localhost` / `::1` 访问时，**读请求**（GET/HEAD/OPTIONS）免 token；**写请求**无有效 token 时须 `Sec-Fetch-Site: same-origin` 或 `Origin` 与请求 host 一致，否则 **403 `csrf_forbidden`**。浏览器打开同源管理页仍可正常操作；裸 `curl`/跨站脚本对本机写 API 须带 token。局域网 IP 访问仍须 token。
* `/api/health` **无需 token 即可探活**；但完整 CapabilityReport（`capabilities` / `action` 等）仅对本机客户端或**携带有效 token** 的局域网请求返回（BUG-236）。未鉴权 LAN 仅见 `profile` / `overall`；`workspaceRoot` 仍仅本机可见（BUG-169）。
* 缺失或错误 token 返回 `401`，统一错误格式 `{"error": {"code": "unauthorized", "message": "..."}}`。
* token 为一次性生成的随机串，仅在本工作区有效；重置方式：删除 `run/` 下的 token 文件后重启管理页。
* 管理页登录框默认隐藏输入；可用眼睛图标切换可见/隐藏，便于核对粘贴结果。

### Token 自动轮换（IMP-046）

管理页启动后，后台线程以 **168 小时（7×24）** 为周期自动轮换 API token（可通过 `local-web.yml` 的 `managerTokenRotateHours` 配置，单位小时，默认 168）。

* **本机 loopback 访问不受影响**——`127.0.0.1` / `localhost` / `::1` 继续免 token（IMP-003）。
* **局域网访问**：轮换后旧 token 立即失效，使用旧 token 的请求收到 `401`。局域网用户需获取新 token。
* **获取新 token**：
  * 在管理页所在机器上运行 `lwa manager token`（打印明文 token + 颁发时间 + 下次轮换时间）。
  * 或在本机浏览器打开管理页（loopback 免 token），在页面内查看。
  * `--json` 选项输出 JSON 格式，便于 Agent 解析：`lwa manager token --json`。
* **轮换不重置周期**：manager 重启不会重新计时——若 token 未到期，重启后继续使用原 token 和原计时。仅当 token 文件丢失或 `createdAt` 缺失时才重新颁发。
* 轮换后 `_verify_token` 每次请求从磁盘读取，故新 token 立即生效，**无需重启管理页**。
* **不做多 token 宽限期**：旧 token 一旦轮换即刻失效，不支持新旧 token 并存。

### `?token=` 查询参数泄漏面（有意保留）

产品策略：**保留** `?token=`，方便从已登录页用带 token 的链接打开新标签；同时须知：

* token 会进入浏览器历史、可能的 Referer，以及反向代理 / 访问日志。
* 服务端已对回环**写请求**强制同源 Fetch Metadata / `Origin`（无 token 时 403 `csrf_forbidden`）；`?token=` 仍是额外泄漏面，日常优先用 Header。
* 推荐日常请求只用 `Authorization: Bearer` / `X-LWA-Token`；用完 `?token=` 后尽快从地址栏去掉（管理页会在读入后尝试 `history.replaceState` 清除）。

## API 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查（无需 token；能力细节见上文鉴权说明） |
| GET | `/api/capability` | 鉴权能力报告（`?refresh=true` 同步重探，IMP-033） |
| GET | `/api/stats` | 顶部统计：实例计数、类型分布、数据库实例数、端口池、主机资源 |
| POST | `/api/access/refresh` | 用当前 LAN IP 重算并落盘各实例 `lanUrl`/`routeUrl`（IMP-038/040） |
| POST | `/api/gateway/switch` | 网关后端原子切换（body `{"backend":"caddy"|"builtin"}`，IMP-037） |
| GET | `/api/instances` | 实例列表（先观测回写状态再取快照；含 `redundant` 布尔字段，IMP-019） |
| GET | `/api/instances/{id}` | 实例详情：状态快照 + manifest + 构建/事件/资源记录 |
| GET | `/api/instances/{id}/logs?category=&tail=` | 日志内容（build/run/gateway/import/scan） |
| GET | `/api/instances/{id}/resources` | 实例级资源占用 |
| POST | `/api/instances/{id}/start` | 启动实例；容器在 Docker 能力降级时返回 `409 capability_denied`（BUG-237） |
| POST | `/api/instances/{id}/stop` | 停止实例（同上） |
| POST | `/api/instances/{id}/restart` | 重启实例（同上） |
| POST | `/api/instances/{id}/rebuild` | 重建实例（经构建队列限流；同上） |
| POST | `/api/instances/{id}/cancel-build` | 取消排队中或进行中的构建（IMP-039）；返回 `outcome`：`cancelled` / `cancel_failed` / `noop` / `already_done`；`cancel_failed` 为 **409**；不删缓存/镜像/用户数据；`cancelling` 期间其它生命周期操作返回 409 |
| POST | `/api/instances/{id}/recover` | 一键恢复 `gateway_down`/`config_invalid`；容器路径同样受能力门禁；CLI：`lwa recover <id>` |
| POST | `/api/instances/{id}/update` | 用 inbox 内新 zip 原地更新实例（IMP-009） |
| POST | `/api/instances/{id}/update-from-dir` | 从关联文件夹源同步更新实例（IMP-047；仅 sourceKind=folder） |
| POST | `/api/import-from-dir` | 从本机文件夹导入新实例（IMP-047） |
| POST | `/api/pick-directory` | 在 **LWA 宿主机**打开原生目录选择器（IMP-051）；**仅 loopback** 客户端可用，成功返回 `{path}`；局域网即使有 token 也 403 `loopback_required` |
| POST | `/api/instances/{id}/remove?purge=&force=` | 移除单个实例（IMP-019 / IMP-035）；默认仅清 registry（`purge=false`）；`purge=true` 删 `apps/<id>/`；非空 `data/` 且未 `force` 时返回 **409 `data_nonempty`**；成功体回显 `instanceId/action/purge/force` |
| PATCH | `/api/instances/{id}/path-alias` | 设置或清除路径别名（IMP-006 / IMP-014 / IMP-022） |
| GET | `/api/instances/{id}/pageviews?limit=` | 单实例浏览量详情：按天分布 + 最近命中 + `uniqueIpList`（IMP-024/026；page 级过滤见 IMP-025）；CLI：`lwa pageviews <id>` |
| GET | `/api/pageviews` | 全部实例浏览量汇总（惰性摄入日志后返回，IMP-024；Caddy 无别名直连端口见 IMP-028）；CLI：`lwa pageviews` |
| GET | `/api/redundant` | 冗余实例列表（同 `sourceZipHash` 分组中非最早者，IMP-019） |
| POST | `/api/redundant/remove?purge=&force=` | 批量移除冗余实例，保留每组最早者（IMP-019） |
| GET | `/api/pending` | pending 与 failed 实例队列 |
| GET | `/api/port-pool` | 端口池占用摘要 |

### 错误格式

所有错误统一为：

```json
{"error": {"code": "not_found", "message": "实例 xxx 不存在"}}
```

常见 code：`unauthorized`、`csrf_forbidden`（回环非同源写请求）、`not_found`、`bad_request`、`conflict`、`data_nonempty`（IMP-035：purge 遇非空 data/）、`lifecycle_error`、`capability_denied`、`recognition_error`、`internal`。

### 能力降级（IMP-033）

`GET /api/health`（本机或已鉴权）可含：

```json
{
  "ok": true,
  "profile": "full",
  "overall": "unready",
  "capabilities": {
    "managerDockerAccess": "permission_denied",
    "daemonDockerAccess": "unknown",
    "caddyRuntime": "owner_mismatch",
    "caddyWorkspaceAccess": "ready",
    "gatewayAccess": "ready",
    "sessionRefreshRequired": false
  },
  "action": "执行：lwa doctor --profile full 与 lwa setup --full --resume"
}
```

`/api/health` **只读缓存**，不会同步跑 Docker/Caddy 探测（否则存活检查会被拖慢）。两点排障须知：

- **`overall: "unknown"` 是启动占位**，表示 manager 的后台能力自检还没跑完，既不是 ready 也不是故障；它不属于 `CapabilityReport.overall` 的正式取值。
- gateway 通常比 manager 晚就绪，因此 health 会用新鲜的 gateway 缓存纠偏 `gatewayAccess`。纠偏后 `overall` / `action` 会**一并重算**，与 `capabilities` 保持一致；但仅在 manager 已有真实探测结果时才重算——占位期间不会凭 gateway 缓存推出 ready。
- manager 另有后台周期刷新完整能力（默认约 5 分钟），使 Caddy 后续恢复/掉线后 `/api/health` 的 `caddyRuntime` 等字段也能收敛；探测失败不覆盖磁盘上最后一份可解析快照。
- 鉴权后也可 `GET /api/capability?refresh=true` 同步重探。

前端据此显示降级横幅并禁用容器按钮；**后端**对容器 start/stop/restart/rebuild/recover 同样拒绝，避免绕过 UI。静态实例不受 Docker 能力门禁影响。实例快照可含 `observedState` / `observationError` / `runtimeAccess`（观测失败 ≠ stopped）。
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

### 文件夹源导入与更新（IMP-047）

#### 从文件夹导入

```http
POST /api/import-from-dir
Authorization: Bearer <token>
Content-Type: application/json

{"sourceDir": "/home/user/my-site", "name": "可选", "pathAlias": "可选"}
```

* `sourceDir`：本机文件夹绝对路径（必须以 `/` 开头）。
* LWA 将文件夹内容**复制**进工作区 `apps/<id>/current/`（非就地运行）。
* 导入后实例 `sourceKind=folder`，`sourceDirPath` 记录关联目录路径。
* 与 CLI `lwa import --from-dir <path>` 共用 `importer.import_from_dir` 代码路径。

#### 选择文件夹（IMP-051）

```http
POST /api/pick-directory
Authorization: Bearer <token>
```

* 由 **manager 进程在 LWA 宿主机**调起系统目录对话框（macOS 访达 / Linux zenity|kdialog），返回 `{"path":"/abs/..."}`。
* **仅 loopback**（`127.0.0.1` / `::1`）可调用；局域网访问即使持有 token 也返回 **403 `loopback_required`**（避免对话框弹在服务器屏幕上、或误用访问机路径）。
* 用户取消 → 400 `cancelled`；无 GUI/缺工具 → 400 `unavailable`；超时 → 400 `timeout`。管理页在非本机地址时禁用「选择文件夹」按钮，仍可手输绝对路径。

#### 从源目录更新

```http
POST /api/instances/{id}/update-from-dir
Authorization: Bearer <token>
Content-Type: application/json

{"restart": true, "keepData": true, "forceKindChange": false}
```

* 仅适用于 `sourceKind=folder` 的实例；zip 源实例返回 400。
* 内容指纹（SHA256 of sorted file paths + contents）与 `sourceSyncHash` 一致时
  自动跳过（`skipped=true`），不 rebuild / restart。
* 源目录不存在时返回错误（不会回退到 mount 模式）。
* 与 CLI `lwa import --from-dir <path> --update <id>` 共用 `importer.update_from_dir` 代码路径。

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
* **SPA 限制（IMP-023）**：构建产物若使用绝对路径资源（如 `/assets/app.js`），在 `/<alias>/` 下可能空 200 / 404 / 错误 MIME 白屏；相对路径或 Vite `base: './'` 等配置可正常使用。`lwa access review` 会检出 `aliasResourceMismatch`。

列表与详情 API 额外返回（IMP-007 / IMP-006 / IMP-019 / IMP-043）：

| 字段 | 说明 |
| --- | --- |
| `name` | 显示名（优先 `--name` / 主页 `<title>`（含 `dist/`/`build/` 等产物入口） / slug 美化；见 IMP-043） |
| `hostPort` | 实例宿主端口 |
| `internalPort` | manifest 中的内部/期望端口（容器或 scanner 识别） |
| `portMappingLabel` | 形如 `33001 → 18001` 的映射说明 |
| `routeHost` | 路径别名 slug（无则为 null） |
| `routeUrl` | 统一入口 URL（`routeMode=name` 且 Caddy 可用时） |
| `lanUrl` | 当前应打开的局域网直达 URL（读时按当前 LAN IP 合成，不盲信落盘） |
| `localhostUrl` | 本机回环兜底 URL（`http://127.0.0.1:<hostPort>/`，LAN 不通时可用） |
| `currentLanIp` | 当前探测/配置的 LAN IP（IMP-040） |
| `persistedLanIp` | 落盘 `lanUrl` 中的 host（可能已陈旧） |
| `lanAddressStale` | 落盘 host 与当前 LAN IP 是否不一致 |
| `lanUrlSource` | `live` / `manual` / `manifest` |
| `redundant` | 是否为同 zip 指纹分组中的冗余实例（非最早者，IMP-019） |

详情中的 `manifest.nameSource` 为 `user` / `html_title` / `slug`（或旧数据 `null`），用于判断是否允许 title 回填。

### 刷新访问地址（IMP-040）

```http
POST /api/access/refresh
Authorization: Bearer <token>
```

立即用当前 LAN IP 重算并落盘各实例 `lanUrl`/`routeUrl`（与 `lwa access refresh` 同源）。管理页在 `lanAddressStale` 时会提示并可一键调用。

### 网关后端切换（IMP-037）

```http
POST /api/gateway/switch
Authorization: Bearer <token>
Content-Type: application/json

{"backend": "caddy"|"builtin", "dryRun": false, "review": true}
```

与 `lwa gateway switch` 同源原子事务。成功返回 `GatewaySwitchResult`（含 `ok` /
`fullyOk` / `accessOk` / `stages`）；后端切换失败时 HTTP 409 + detail 为同一结构。
`ok=true` 但 `accessOk=false` 表示后端已切成功、访问复核有风险（不假绿）。

### 浏览量统计（IMP-024 / 025 / 026 / 027 / 028）

```http
GET /api/pageviews
Authorization: Bearer <token>
```

响应形如 `{"instances": {"<id>": {"hits": N, "uniqueIps": N, "lastSeen": "...", "source": "caddy|builtin|container"}}}`。
请求时惰性摄入最新访问日志再返回聚合：

| 来源 | 何时 | 说明 |
| --- | --- | --- |
| `caddy` | `staticGateway=caddy` 且（静态，或容器有路径别名） | 读 `logs/static-access.log`；有别名按 `/<alias>/`，无别名静态按 host 端口（IMP-028）；容器别名见 IMP-027 |
| `builtin` | builtin / Caddy 不可用降级 | 每实例 `gateway.log`（CLF） |
| `container` | 容器且无 Caddy 别名 | docker logs 尽力解析（近似） |

仅 **page** 级命中计入 `hits`（静态资源 / `__lwa_probe` 探测排除，IMP-025）。`uniqueIps` 为实例级去重（IMP-026，与详情 `uniqueIpList` 长度一致）。

```http
GET /api/instances/{id}/pageviews?limit=50
Authorization: Bearer <token>
```

返回 `byDay`、`recent`、以及全量 `uniqueIpList`（含 `ip` / `count` / `lastSeen` / `local`）。数据在工作区 `run/pageviews.db`。

### 单个实例删除（IMP-035）

```http
POST /api/instances/{id}/remove?purge=false&force=false
Authorization: Bearer <token>
```

两种语义（与 CLI `lwa remove` 一致）：

| 参数 | 含义 |
| --- | --- |
| `purge=false`（默认） | **仅移除**：停服 + 清 registry，**保留** `apps/<id>/` |
| `purge=true` | **彻底删除**：在仅移除基础上再删 `apps/<id>/` |
| `force=true` | 仅当 `purge=true` 且 `data/` 非空时需要；跳过非空保护 |

成功响应至少含：`{"instanceId","action":"remove","purge","force"}`。

若 `purge=true&force=false` 且 `data/` 非空，返回 HTTP **409**、错误码 **`data_nonempty`**（不是 500）。管理页据此进入「强制删除」再确认，**不会**自动带 `force=true` 重试。其他错误码不得进入 force 分支。

IMP-041：每次删除请求会在 `manager.log` 写一行无 token 的 `audit remove instance=… status=… code=…`；服务层另有可 grep 的 `remove stage=…` 阶段日志与 orphan `remove_stage` 事件（详见 [FAQ](faq.md)「删除后如何对账」）。

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

* **概览面板**：实例总数、各状态计数（含「需恢复」）、类型分布、主机 CPU/内存/磁盘、端口池占用；**能力降级横幅**（Full / Docker / Caddy overall≠ready 时显示原因与建议命令）；任一实例 `lanAddressStale` 时另有 **LAN 地址漂移**横幅，并可一键「刷新访问地址」（`POST /api/access/refresh`，IMP-040）。
* **实例列表**：每行显示名称（**IMP-043**：导入优先显式 `--name`，其次主页 HTML `<title>`——含 `dist/`/`build/` 等托管产物入口，实体按浏览器语义解码——否则 slug 美化；`nameSource` 区分 `user` / `html_title` / `slug`；旧自动名回填持实例锁；名称最多两行省略，列宽随内容自然分配）、冗余实例带「冗余」徽章与行高亮、状态、期望态、形态、运行层、技术栈、访问地址、端口、资源、**浏览量**、更新时间；操作区含日志 / **路径别名** / 浏览量详情 / start / stop / restart / rebuild / **取消构建** / **删除**（**所有实例**均有入口，不再仅冗余；`building/starting/stopping/removing/cancelling` 时相应禁用）；状态为 `queued` / `building` / `cancelling` 时显示「取消构建」（IMP-039）；Docker 能力降级时容器启停按钮禁用；状态为 `网关不可达`（gateway_down）或 `配置无效`（config_invalid）时额外显示「恢复」按钮（DEV-043；CLI：`lwa recover <id>`）。
* **筛选**：按状态 / 形态搜索；「仅待处理/失败」与「仅冗余」勾选；顶部可「批量删除冗余」（仍只处理冗余，规则不变）。
* **删除确认（IMP-035）**：受控双阶段模态——① 选择「仅移除」（默认，`purge=false`）或「彻底删除」（`purge=true`）；② 输入完整项目 ID；彻底删除须勾选「理解数据不可恢复」。非空 `data/` 首次 purge 得 409 `data_nonempty` 后，再勾选强制确认才发 `force=true`（不自动重试）。打开时焦点进入对话框，Tab 限制在模态内，Esc/关闭后恢复触发按钮焦点。
* **路径别名对话框**：`shared-static` 与 `docker-compose` 实例操作区「路径别名」按钮可用（pending/building/queued 态禁用）；输入 slug 保存或清除；校验错误在对话框内展示。builtin 后端下设置会失败并展示后端错误信息（IMP-022）。
* **从文件夹导入（IMP-047 / IMP-051）**：顶栏「导入文件夹」打开对话框；请选**项目根或 dist/**（含 `index.html` / `package.json`），不要只选 `src/`。源路径右侧「选择文件夹」仅在 **loopback** 可用（宿主机原生选目录）；局域网访问时按钮禁用，请粘贴 LWA 机器绝对路径。导入仍复制进工作区，非就地运行。API 错误文案不含 `[ZIP_IMPORT_ERROR]` 等前缀。
* **浏览量**：列表列展示累计访问；点击打开按天分布、最近命中与独立 IP 列表（IMP-024/026；page 级过滤 IMP-025）。

> **状态说明（DEV-043 / BUG-071 / IMP-033）**：Caddy 模式下，enabled 静态实例在 master（admin :2019）不可达时显示 `网关不可达`，在 master 在线但站点端口不通时显示 `配置无效`——二者均不再被误标为普通「已停止」。Docker 观测失败时显示 unknown / 权限提示，不误写 stopped。点击「恢复」会先尝试拉起 Caddy master 再 restart 实例（容器路径仍受能力门禁）。
* **实例详情**：manifest、构建记录、事件流、资源占用、分类日志查看器；含路径别名说明与 CLI 等价命令提示。
* **待处理区**：pending 实例（可重扫 `lwa scan`）与 failed 实例（显示 `lastError`）。

## 与 CLI 一致性

管理页的生命周期操作直接调用 `local_webpage_access.lifecycle` 的同名函数，
**与 CLI `lwa start/stop/restart/rebuild/cancel-build/remove` 走完全相同的代码路径**（验收标准 3）。
路径别名与 zip 更新分别调用 `path_alias.set_instance_path_alias` 与 `importer.update_zip`，
与 CLI `lwa alias set/clear`、`lwa import --update`、`lwa access refresh`、`lwa gateway switch` 一致；冗余清理与 `lwa remove --redundant` 一致。
因此管理页展示的状态与 `lwa status` 始终一致。

## 绑定安全

`managerHost` 默认 `0.0.0.0`（便于局域网访问）。`local_webpage_access/security.py`
的 `validate_manager_binding` 会在启动时校验：若绑定到 LAN/通配地址，
**必须存在 token**，否则拒绝启动。详见 [安全边界](security-boundary.md)。

## 相关文档

- [运维手册](operations-playbook.md) — 网关选型、冗余清理、容器别名、浏览量与 Caddy 排障
- [Runtime 工作区说明](runtime-workspace.md)
