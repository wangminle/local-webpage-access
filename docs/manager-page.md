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

## 局域网 / Tailscale 地址切换

实例表头“访问地址”右侧的浅色开关默认显示“局域网”，点击可切换为“tailscale”。
局域网模式沿用原有地址；Tailscale 模式将列表“端口”、路径别名及详情抽屉“打开路径入口”的主机地址替换为
LWA 运行环境自身的 Tailscale IP，并隐藏“本机”回环链接。切回局域网会恢复原链接；
抽屉已打开时也会随模式或组网地址更新，不额外请求详情。端口数字、映射、路径及原协议保持不变；IPv4 优先，也支持仅 IPv6 的组网地址。

管理服务每次启动即在后台检查 Tailscale，之后每30秒检查一次；页面沿用15秒刷新读取缓存，
点击开关本身不执行系统探测。正常前台页面通常在约47秒内反映状态变化。
未发现可用组网时无法切换，点击鼠标上方会显示3秒提示：
“lwa的运行环境中未发现tailscale组网”。键盘触发时提示位于开关上方。

选中 Tailscale 后，新快照确认断开、注销或探测失败会自动回到局域网并提示一次；
此后恢复需手动再次切换。缓存超过60秒仅标记过期，页面从后台恢复时会重新读取状态；
过期或读取失败本身不判定为未组网，也不打断当前 Tailscale 选择。切换选择不跨页面保存。

此开关仅更改管理页链接，不安装、启动或管理 Tailscale，也不修改监听、防火墙和网关配置。
应在 **LWA 进程所在环境** 安装并连接 Tailscale；仅在 Windows 宿主机安装时，
不能据此认定 WSL/容器内的 LWA 已组网。macOS 支持常规 CLI 和 `/Applications/Tailscale.app` 安装。
组网可用不代表每个应用都已从另一节点验证可达；原有别名 `passed` 徽标不代表 Tailscale 远程验收。
原有 HTTPS 链接保留 HTTPS，替换 IP 后可能受证书或网关绑定限制，需要另行验证；不会自动降级到 HTTP。

## 鉴权

* 所有 `/api/*` 路由（`/api/health` 除外）默认要求请求头 `Authorization: Bearer <token>`（WBS-22.12）。
* 另支持 `X-LWA-Token` 头，以及查询参数 `?token=`（管理页打开新标签等场景；见下方泄漏面说明）。
* **本机调试例外（IMP-003 / BUG-295）**：从 `127.0.0.1` / `localhost` / `::1` 访问时，**读请求**（GET/HEAD/OPTIONS）免 token；**写请求**无有效 token 时须 `Sec-Fetch-Site: same-origin` 或 `Origin` 与请求 host 一致，否则 **403 `csrf_forbidden`**。浏览器打开同源管理页仍可正常操作；裸 `curl`/跨站脚本对本机写 API 须带 token。局域网 IP 访问仍须 token。loopback 免鉴权还要求 **Host 头主机名为 `127.0.0.1` / `localhost` / `::1`**（BUG-576 DNS rebinding 加固：外部域名解析到 127.0.0.1 冒充同源的请求一律按非本机处理）。**WSL NAT 例外（BUG-980）**：Windows portproxy → `127.0.0.1` 的回环转发会丢失真实来源，回环无法证明本机身份——WSL NAT（或网络模式探测失败）时上述回环豁免整体停用，本机访问同样须带 token；WSL mirrored 与其他平台不受影响。
* `/api/health` **无需 token 即可探活**；但完整 CapabilityReport（`capabilities` / `action` 等）仅对本机客户端或**携带有效 token** 的局域网请求返回（BUG-236）。未鉴权 LAN 仅见 `profile` / `overall`；`workspaceRoot` 仍仅本机可见（BUG-169；WSL NAT 下回环不可信，改为凭有效 token 解锁，BUG-980）。
* 缺失或错误 token 返回 `401`，统一错误格式 `{"error": {"code": "unauthorized", "message": "..."}}`。
* **HTTPS 模式（V0.9.0）**：`gatewayTls: internal` 时本页经 `https://<LAN-IP>:<managerTlsPort>/` 访问（独立 HTTPS origin，manager 收敛仅回环监听），实例访问地址按 TLS 入口显示 `https://…:8443/<别名>/`（lanUrl 直连地址保持明文语义）；客户端需先 `lwa ca export` 导出并信任根证书——见 [https.md](https.md)。V0.9.1 起 LAN IP 变化后，在线 Caddy 会随地址刷新重载 8443/9443 绑定，不必再手工 `gateway on` 才能恢复别名入口。
* **Agent 端点（V0.8.18）**：`/api/agent/v1/*` 不继承回环免 token，也不接受 `?token=`——仅回环 + Header 凭据双条件，缺失任一即 401/403；见 [Agent 指南](agent-guide.md) §9。
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
| GET | `/api/network-context` | 本机Tailscale状态缓存（遵循上文鉴权规则，`Cache-Control: no-store`）；返回可用性、自身IP、检查时间与剩余有效期，不返回peer信息、不触发即时探测。后台30秒更新，60秒过期，IMP-069 |
| GET | `/api/build-proxy` | 读取构建代理当前配置，返回 `enabled` 和 `url` |
| PUT | `/api/build-proxy` | 校验并保存构建代理配置；body为 `enabled`（布尔值）及 `url`，对后续构建生效 |
| GET | `/api/build-proxy/detect` | 检测宿主系统代理，返回候选 `url`（无候选时为空字符串）；不自动启用或保存配置 |
| GET | `/api/stats` | 顶部统计：实例计数、类型分布、数据库实例数、端口池、主机资源 |
| POST | `/api/access/refresh` | 用当前 LAN IP 重算并落盘各实例 `lanUrl`/`routeUrl`（IMP-038/040） |
| POST | `/api/gateway/switch` | 网关后端原子切换（body `{"backend":"caddy"|"builtin"}`，IMP-037） |
| GET | `/api/instances` | 实例列表（先观测回写状态再取快照；含 `redundant` 布尔字段，IMP-019） |
| GET | `/api/instances/{id}` | 实例详情：状态快照 + manifest + 构建/事件/资源记录。**V0.9.4（S3-01）**：响应新增 `versions` 字段（容器实例的版本留档负载；非容器为 `null`）——`retention`（保留数）、`versions[]`（`id/startedAt/finishedAt/status/imageRef/imageId/sourceHash/imagePresent/inUse`）、`diskUsageBytes`、`requiresConfirm`（preStart 含迁移命令时回滚需显式确认）、`scopeNote`（D8 范围说明） |
| POST | `/api/instances/{id}/lan-observation` | 记录独立客户端的LAN观测；body含 `vantage`、`protocol`、`evidence`、`statusCode`及 `url`，按当前入口与网络指纹判定有效性；不代替实际远端探测，不自动配置系统网络 |
| GET | `/api/instances/{id}/versions` | **S3-01 / V0.9.4**：版本留档列表（同详情 `versions` 负载；非容器实例 409 `not_container`） |
| GET | `/api/instances/{id}/timeline?limit=` | **S3-04 / V0.9.4**：操作时间线（事件 / 构建 / 最近操作 `lastOperation` 合并，倒序，每条含 `source` 发起方；`limit` 1–200，默认 50）；CLI：`lwa logs <id> --timeline` |
| GET | `/api/instances/{id}/logs?category=&tail=` | 日志内容（build/run/gateway/import/scan） |
| GET | `/api/instances/{id}/resources` | 实例级资源占用 |
| POST | `/api/instances/{id}/start` | 启动实例（`?fallback_policy=confirm\|auto-equivalent\|disabled`，非法值 400，C.R03；top-1 失败且有等价候选时默认返回 `pendingConfirmation` 结构，前端弹窗确认降级后调用 confirm-fallback）；容器在 Docker 能力降级时返回 `409 capability_denied`（BUG-237）。**V0.9.3（S2-09）**：可选 `?expectedRevision=<int>` 乐观并发，过期返回 **409 `revision_conflict`** |
| POST | `/api/instances/{id}/confirm-fallback` | Gate-C 降级确认（C.R03）：降级弹窗确认后调用，以 `auto-equivalent` 策略重试启动；幂等--实例已在 fallback 后运行则直接返回当前状态；同样支持 `?expectedRevision=`（S2-09） |
| POST | `/api/instances/{id}/stop` | 停止实例（同上，含 `?expectedRevision=`，S2-09） |
| POST | `/api/instances/{id}/restart` | 重启实例（同上，含 `?expectedRevision=`，S2-09） |
| POST | `/api/instances/{id}/rebuild` | 重建实例（经构建队列限流；同上，含 `?expectedRevision=`，S2-09）。V0.8.5 起响应带 `sourceStaleWarnings`（关联 folder/git 源已漂移的提示，前端暂未展示）。旧产物拷贝 `buildHooks` 会删除模板保护的 `static/` 时返回 **409 `destructive_build_hooks`**；确认后加 `?force=true` |
| POST | `/api/instances/{id}/cancel-build` | 取消排队中或进行中的构建（IMP-039）；返回 `outcome`：`cancelled` / `cancel_failed` / `noop` / `already_done`；`cancel_failed` 为 **409**；不删缓存/镜像/用户数据；`cancelling` 期间其它生命周期操作返回 409 |
| POST | `/api/instances/{id}/rollback` | **S3-01 / V0.9.4**：有限回滚到指定（默认上一可用）留档版本；body `{"toBuildId": null, "expectedRevision": null, "confirm": false}`（`confirm` 必须是 JSON 布尔值，字符串 `"false"` 返回 400）；D8：只回滚镜像与生成配置，**不回滚 `data/`**；preStart 含迁移命令且未 `confirm=true` 时 409 `rollback_needs_yes`；`expectedRevision` 过期 409 `revision_conflict`；回滚后验证失败自动尝试恢复原版本，恢复结果经实证验证（未通过时实例标 `failed`、错误如实报「未能恢复原版本」） |
| POST | `/api/instances/{id}/recover` | 一键恢复 `gateway_down`/`config_invalid`；容器路径同样受能力门禁；CLI：`lwa recover <id>`；支持 `?expectedRevision=`（S2-09，仅锁外预检） |
| POST | `/api/instances/{id}/update` | 用 inbox 内新 zip 原地更新实例（IMP-009）；body 可选 `expectedRevision`（S2-09，非整数 400，过期 **409 `revision_conflict`**） |
| POST | `/api/instances/{id}/update-from-dir` | 从关联文件夹源同步更新实例（IMP-047；仅 sourceKind=folder；支持 body `expectedRevision`，S2-09） |
| POST | `/api/import-from-dir` | 从本机文件夹导入新实例（IMP-047） |
| POST | `/api/import-from-git` | 从 GitHub 仓库一键导入新实例（IMP-065；不限 loopback，LAN + token 可用；body：`url`/`ref`/`subdir`/`name`/`pathAlias`） |
| POST | `/api/instances/{id}/update-from-git` | 从 GitHub 远端更新 git 源实例（IMP-065；`ls-remote` 无变更探测，OID 未变返回 `skipped=true`；按完整 commit SHA 钉版的实例不探测远端、始终 `skipped=true`，换版本须用 CLI `--ref` + `--allow-source-change`；仅 sourceKind=git） |
| GET | `/api/instances/{id}/verification` | 读取实例验证探针覆盖层（继承探针 + 用户覆盖 + 合并后的有效探针，CHK-252） |
| PATCH | `/api/instances/{id}/verification` | 更新实例 `verificationOverrides`（写入即持久化，重扫/rebuild 不覆盖；CLI：`lwa probe show\|set\|reset\|auto`） |
| POST | `/api/pick-directory` | 在 **LWA 宿主机**打开原生目录选择器（IMP-051）；**仅 loopback** 客户端可用，成功返回 `{path}`；局域网即使有 token 也 403 `loopback_required` |
| POST | `/api/instances/{id}/remove?purge=&force=` | 移除单个实例（IMP-019 / IMP-035）；默认仅清 registry（`purge=false`）；`purge=true` 删 `apps/<id>/`；非空 `data/` 且未 `force` 时返回 **409 `data_nonempty`**；成功体回显 `instanceId/action/purge/force` |
| PATCH | `/api/instances/{id}/path-alias` | 设置或清除路径别名（IMP-006 / IMP-014 / IMP-022） |
| PATCH | `/api/instances/{id}/settings` | **S3-05 / V0.9.4**：实例配置部分更新（buildEnv、buildBaseFromAlias、redundancyAcknowledged、systemDeps、buildHooks、hostBuild/preSync、extraVolumes）；未知字段 400；响应含 `extraVolumes`（容器实例，改后 `lwa restart` 生效） |
| GET | `/api/instances/{id}/pageviews?limit=` | 单实例浏览量详情：按天分布 + 最近命中 + `uniqueIpList`（IMP-024/026；page 级过滤见 IMP-025）；CLI：`lwa pageviews <id>` |
| GET | `/api/pageviews` | 全部实例浏览量汇总（惰性摄入日志后返回，IMP-024；Caddy 无别名直连端口见 IMP-028）；CLI：`lwa pageviews` |
| GET | `/api/redundant` | 冗余实例列表（同 `sourceZipHash` 分组中非最早者，IMP-019；每项附 `configLossReasons`，issue #31） |
| POST | `/api/redundant/remove?purge=&force=&allowConfigLoss=` | 批量移除冗余实例，保留每组最早者（IMP-019；携带独立配置者默认跳过并记入响应 `skipped`，issue #31） |
| GET | `/api/pending` | pending 与 failed 实例队列 |
| GET | `/api/port-pool` | 端口池占用摘要 |
| GET/POST | `/api/agent/v1/*` | **Agent 专用通道（M1，V0.8.18）**：能力快照 / 实例分页与详情 / 访问地址 / 脱敏日志 / 部署计划与应用 / 异步操作与生命周期。鉴权比管理页更严——**仅本机回环且必须携带 token**（仅 `Authorization: Bearer` / `X-LWA-Token`，无 `?token=` 通道）；写操作要求幂等键并支持 `expectedRevision` 乐观锁。端点清单与语义详见 [Agent 指南](agent-guide.md) §9 |

### 错误格式

所有错误统一为：

```json
{"error": {"code": "not_found", "message": "实例 xxx 不存在"}}
```

常见 code：`unauthorized`、`csrf_forbidden`（回环非同源写请求）、`not_found`、`bad_request`、`conflict`、`data_nonempty`（IMP-035：purge 遇非空 data/）、`destructive_build_hooks`（issue #141：旧产物拷贝钩子）、`lifecycle_error`、`capability_denied`、`recognition_error`、`internal`。

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
* **乐观并发（S2-09 / issue #57-3，V0.9.3）**：body 可带 `expectedRevision`（列表/详情快照里的 `revision`，整数；类型非法 400）。实例在该快照之后被其它通道（CLI / daemon / Agent / 另一个管理页标签）更新过 → **409 `revision_conflict`**，错误体含 `expected` / `actual`。管理页前端每次更新都自动携带；update-from-dir / update-from-git 同样支持。更新成功后由更新流程自身 CAS 递增 `revision`。

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
  自动跳过（`skipped=true`），不 rebuild / restart；存在待处理预构建时不跳过，允许重试。
* 源目录不存在时返回错误（不会回退到 mount 模式）。
* 与 CLI `lwa import --from-dir <path> --update <id>` 共用 `importer.update_from_dir` 代码路径。
* 容器实例若配置了 `hostBuild` / `preSync`，更新触发的 rebuild 会先在 `apps/<id>/current/` 执行该宿主命令，再构建镜像（DEV-170 / issue #61 阶段二）。zip 与 GitHub 源更新端点同此行为。
* 配置或修改生效命令会置待处理标记，清空命令会清标记。源码更新失败后可重试同一 ZIP、文件夹或 Git 提交；停止状态或关闭自动重启时，可用管理页重建按钮消费该标记。

### GitHub 源导入与更新（IMP-065）

#### 从 GitHub 导入

```http
POST /api/import-from-git
Authorization: Bearer <token>
Content-Type: application/json

{"url": "https://github.com/<owner>/<repo>", "ref": "可选分支/标签",
 "subdir": "可选子目录", "name": "可选", "pathAlias": "可选"}
```

* 仅支持 `https://github.com/<owner>/<repo>`（精确 host、仅 443；拒绝 userinfo /
  query / fragment / `/tree/` 等网页段——网页地址请改用 `ref` + `subdir` 字段）。
* LWA 在**宿主机**一次性浅克隆（`--depth 1`，空 template + 禁 hooks +
  `GIT_LFS_SKIP_SMUDGE=1`）到工作区外临时目录，打包（跳过 `.git` 与 symlink）后
  走与 zip 完全相同的识别管线；**不限 loopback**（LAN + token 可用）。
* 成功体含 `autoStart`（对齐 import-from-dir：识别成功且 tiny/small 自动启动；
  `status=pending` 才是真·未识别，前端会报错并保持对话框）。
* 失败体 `error.detail.kind` 携带闭集 errorKind：`invalid_url` /
  `host_not_allowed` / `userinfo_forbidden` / `git_missing` /
  `remote_unreachable` / `ref_not_found` / `clone_timeout` / `size_exceeded`。
* 并发导入经 `import_activity` 闸门排队；staging 每路独立 tempfile，用完即删。
* 私有仓凭据与代理（`https_proxy`）都配置在 **LWA 宿主机**（不是浏览器所在
  机器）；LWA 不保存、不回显凭据。

#### 从 GitHub 更新

```http
POST /api/instances/{id}/update-from-git
Authorization: Bearer <token>
Content-Type: application/json

{"url": "可选（规范化后须与实例记录一致）", "restart": true, "keepData": true}
```

* 仅适用于 `sourceKind=git` 的实例；无 `url` 时用 manifest 存储的仓库地址与
  ref/tag 类型做 `git ls-remote` 探测。zip/folder 源实例显式传 `url` 时
  原地切换为 git 源（issue #28，换源不换实例）。
* 远端 OID 未变 → `skipped=true`（「无需更新」，零克隆零重建）；有新提交 →
  重新浅克隆 → 既有原地升级（保留 id / 端口 / data / 别名）。按完整 commit SHA
  钉版导入的实例不探测远端、始终 `skipped=true`；换版本须用 CLI 显式传新 `--ref`
  并加 `--allow-source-change`（管理页按钮不接受 ref / subdir）。
* git 实例传入 `url` 与实例 `sourceGitUrl` 不一致 → 400（`source_mismatch`）；
  更换仓库请先删除实例再导入。
* git 源实例不能用 zip `update` 更新（会被拒绝）；可用 CLI
  `--from-dir <目录> --update` 原地切换为文件夹源。

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
| `lanUrl` | 当前合成的直达 URL（读时按 guest/LAN 探测 IP 合成，不盲信落盘）。**WSL NAT 下这是仅宿主可达的地址，不是局域网入口** |
| `localhostUrl` | 本机回环兜底 URL（`http://127.0.0.1:<hostPort>/`，LAN 不通时可用） |
| `currentLanIp` | 当前探测/配置的 IP（IMP-040；WSL NAT 下为 guest 地址） |
| `persistedLanIp` | 落盘 `lanUrl` 中的 host（可能已陈旧） |
| `lanAddressStale` | 落盘 host 与当前探测 IP 是否不一致（只表示漂移，不表示局域网可达） |
| `lanUrlSource` | `live` / `manual` / `manifest` |
| `lanReachability` | 地址范围：`loopback` / `windows-host` / `lan` / `unknown`（非 WSL 为 null） |
| `lanExposure` | 暴露状态：`entry-missing` / `candidate-unverified` / `externally-verified` / `unknown`（portproxy 探测超时或读取被拒时保留 `unknown`，不武断判 `entry-missing`）。只有观察位置为 `lan-client`、且协议/主机/端口/路径与**当前入口**一致的成功观测才能升格为 `externally-verified`；guest 或 Windows 本机成功不能代替；HTTP 观测不能给随后改成的 HTTPS 入口当已验证 |
| `hostLanIp` / `hostLanUrl` | Windows 物理网卡 IP，以及已发现转发时的局域网候选 URL |
| `lanObservation` / `lanObservationStale` | 最近一次受约束的外部观测（位置、协议、响应证据、时间）；网络/映射变化，或观测的协议/主机/端口/路径与当前入口不一致时 `lanObservationStale=true`，暴露状态回到待验证。重启/重建会保留该记录，不会静默删除 |
| `windowsListening` | Windows 是否在**对外监听端口**上有 TCP 监听（有 portproxy 时用规则的 `listen_port`，没有映射时才用实例/网关端口）；监听读取失败时为 `null`，不与「没有入口」混为一谈 |
| `wslNetworkMode` / `accessScopeNote` | 实际网络模式（nat/mirrored/…）与给用户看的范围说明 |
| `dynamicIpForwarding` | 已有 portproxy 规则指向会随 WSL 重启漂移的 guest IP 时为 true（doctor `wsl_lan_exposure` 与 `lwa status` 的「转发风险」同步提示；非 WSL 恒为 false） |
| `redundant` | 是否为同 zip 指纹分组中的冗余实例（非最早者，IMP-019） |
| `configLossReasons` | 冗余实例携带的「删除即丢失」独立配置清单（路径别名 / buildEnv，issue #31；非冗余实例为空数组） |
| `reconcileCircuit` | 构建熔断提示（issue #35）：连续构建失败进入小时级退避、达人工阈值后停止自动重试；`lwa start` / `lwa rebuild` 清熔断。仅熔断激活时返回，详情面板显示为「构建熔断」 |
| `lastOperation` | 操作态（S2-02 / issue #57-2，V0.9.3）：最近一次生命周期操作的结果 `{kind, status, buildId, startedAt, finishedAt, error, source}`，`status` 取 `queued` / `building` / `succeeded` / `failed` / `cancelled`。实例 `status` 只反映观测（运行中/已停止/未在服务），构建排队超时、构建失败等不再把仍在服务的实例标成「失败」——改为状态徽标旁叠加「最近一次重建失败」徽标（含时间与原因），详情面板「最近操作」一行展示完整记录；`lwa status` 同步输出该行 |
| `revision` | 实例内容/配置修订号（S2-09 / issue #57-3，V0.9.3）：registry 权威计数，更新（zip/文件夹/git）与 rebuild 成功 CAS 递增；start/stop/restart/recover 与状态观测不改。生命周期操作可带 `expectedRevision` 做乐观并发，过期返回 **409 `revision_conflict`**；管理页每次操作自动携带列表快照的 revision，遇 409 提示「实例已被其他操作更新，请刷新后重试」并自动刷新列表 |
| `aliasGuardResult` | 别名守卫三态（V0.9.3，issue #52）：`passed` / `passed(api-only)`（纯 API 契约，N5）/ `failed(content)` / `failed(transient)` / `skipped(非 Caddy 后端|显式跳过|实例未运行)`；实例卡片路径别名旁显示状态徽标，悬浮可见失败原因 |
| `aliasGuardCheckedAt` | 最近一次别名守卫检查时间 |
| `aliasGuardError` | 最近一次守卫失败原因（passed/skipped 时为 null） |
| `aliasLiveVerifiedAt` | 最近一次别名活验证通过时间（issue #21） |
| `aliasProbeCoverage` | 采样覆盖（issue #58）：`{checked, found, truncated, skippedExternal}`，上限 20 条 |

详情中的 `manifest.nameSource` 为 `user` / `html_title` / `slug`（或旧数据 `null`），用于判断是否允许 title 回填。

### 刷新访问地址（IMP-040）

```http
POST /api/access/refresh
Authorization: Bearer <token>
```

立即用当前 LAN IP 重算并落盘各实例 `lanUrl`/`routeUrl`（与 `lwa access refresh` 同源）。管理页在 `lanAddressStale` 时会提示并可一键调用。已记录的外部观测不会被这次刷新删掉；网络指纹或入口协议/主机/端口/路径变了会在读时标为失效。

### 记录外部观测

```http
POST /api/instances/{id}/lan-observation
Authorization: Bearer <token>
Content-Type: application/json

{
  "vantage": "lan-client",
  "protocol": "https",
  "evidence": "content-match",
  "statusCode": 200,
  "url": "https://<windows-lan-ip>:8443/alias/"
}
```

记录一次受约束的外部观测（与 `lwa access record-observation` 同源）。`vantage` 只接受 `lan-client` / `windows-host` / `guest`；`evidence` 只接受 `content-match` / `page` / `timeout` / `connection-refused` / `tls-error` / `auth-401`。LWA 不会自己从其他设备探测，也不会把 guest 或 Windows 本机成功写成局域网已通。

`url` 须为完整 HTTP(S) URL，且协议与 `protocol` 一致。非法 URL 返回 **400 `LAN_OBSERVATION_INVALID`**，不会覆盖已有观测。
只有 URL 的协议、主机、端口、路径都与当前候选入口一致，成功观测才可升格为外部已验证。
省略 `url` 时只保存证据、不升格；旧的非法 URL 记录仍可查看，但 `lanObservationStale=true`，不会让列表或详情返回 500。

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
并发护栏（V0.7.10）：切换全程持跨进程锁，与 CLI 或另一个管理页切换并发时，
后到者约 15s 后失败并提示稍后重试（BUG-514）；切 builtin 遇运行中但 manifest
损坏的静态实例时拒绝执行（BUG-516），详情见切换结果 `error`。

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

返回 `{"instances": [...], "count": N}`，每项含 `id` / `name` / `sourceZipHash` / `createdAt` / `updatedAt` / `pathAlias` / `skipReasons` / `configLossReasons`。`configLossReasons` 非空表示删除该实例会丢失独立配置（路径别名 / buildEnv）。

```http
POST /api/redundant/remove?purge=false&force=false&allowConfigLoss=false
Authorization: Bearer <token>
```

批量移除冗余（保留每组最早导入者），与 CLI `lwa remove --redundant` 同路径。`purge` / `force` 语义同单个 `remove`。

**护栏（issue #31）**：携带独立配置的冗余实例（`configLossReasons` 非空）默认**跳过**；仅传 `allowConfigLoss=true` 才会删除。响应形如：

```json
{
  "action": "remove-redundant",
  "removed": ["plain-dup"],
  "count": 1,
  "skipped": [{"id": "alias-dup", "reasons": ["配置了路径别名 /my-alias/"]}]
}
```

## 前端功能

单页前端（`/`，Vue 3）提供：

* **概览面板**：实例总数、各状态计数（含「需恢复」）、类型分布、主机 CPU/内存/磁盘、端口池占用；**能力降级横幅**（Full / Docker / Caddy overall≠ready 时显示原因与建议命令）；任一实例 `lanAddressStale` 时另有 **LAN 地址漂移**横幅，并可一键「刷新访问地址」（`POST /api/access/refresh`，IMP-040）；任一实例 `lanReachability=windows-host` 或 `lanExposure=entry-missing` 时另有 **「WSL 地址不是局域网入口」**横幅（展示 `accessScopeNote` 与 Windows 局域网 IP；LWA 不代改 Windows 网络，详见 `lwa doctor`）。
* **实例列表**：每行显示名称（**IMP-043**：导入优先显式 `--name`，其次主页 HTML `<title>`——含 `dist/`/`build/` 等托管产物入口，实体按浏览器语义解码——否则 slug 美化；`nameSource` 区分 `user` / `html_title` / `slug`；旧自动名回填持实例锁；名称最多两行省略，列宽随内容自然分配；**ADJ-048**：名称列上限 220→440px，宽度来自访问地址改两行排版后的让渡）、冗余实例带「冗余」徽章与行高亮、状态、期望态、形态、运行层、技术栈、访问地址（**ADJ-048**：两行排版——第一行「端口 · 本机」，第二行路径别名；列宽取两行中较宽者，即端口/本机为下限、最长别名为上限；WSL NAT 下端口链接显示为「宿主」并提示仅 Windows 宿主可达，存在 `hostLanUrl` 时追加未经外部验证的「局域网候选」链接，详情面板相应新增局域网候选 / Windows 局域网 IP / 地址范围 / 暴露状态 / 网络说明 / WSL 网络模式六行）、端口、资源、**浏览量**、更新时间；操作区含日志 / **路径别名** / 浏览量详情 / start / stop / restart / rebuild / **取消构建** / **删除**（**所有实例**均有入口，不再仅冗余；`building/starting/stopping/removing/cancelling` 时相应禁用）；状态为 `queued` / `building` / `cancelling` 时显示「取消构建」（IMP-039）；Docker 能力降级时容器启停按钮禁用；状态为 `网关不可达`（gateway_down）或 `配置无效`（config_invalid）时额外显示「恢复」按钮（DEV-043；CLI：`lwa recover <id>`）。
* **筛选**：按状态 / 形态搜索；「仅待处理/失败」与「仅冗余」勾选；顶部可「批量删除冗余」（仍只处理冗余，每组保留最早者；工具栏同时显示琥珀竖条释义）。
* **批量删除冗余确认（issue #31）**：点「批量删除冗余」不再弹原生 confirm，改为模态逐项列出处置——表格含实例 / 状态 / 别名 / 更新时间 / 处置 / 保留六列；运行或过渡态始终跳过，独立配置默认跳过。底部可显式勾选允许损失配置，可删数为0时确认禁用；「有意保留」使实例退出候选。提交后 toast 汇总「已清理 N 个，跳过 M 个」。
* **删除确认（IMP-035）**：受控双阶段模态——① 选择「仅移除」（默认，`purge=false`）或「彻底删除」（`purge=true`）；② 输入完整项目 ID；彻底删除须勾选「理解数据不可恢复」。非空 `data/` 首次 purge 得 409 `data_nonempty` 后，再勾选强制确认才发 `force=true`（不自动重试）。打开时焦点进入对话框，Tab 限制在模态内，Esc/关闭后恢复触发按钮焦点。
* **路径别名对话框**：`shared-static` 与 `docker-compose` 实例操作区「路径别名」按钮可用（pending/building/queued 态禁用）；输入 slug 保存或清除；校验错误在对话框内展示。builtin 后端下设置会失败并展示后端错误信息（IMP-022）。
* **从文件夹导入（IMP-047 / IMP-051）**：顶栏「导入文件夹」打开对话框；请选**项目根或 dist/**（含 `index.html` / `package.json`），不要只选 `src/`。源路径右侧「选择文件夹」仅在 **loopback** 可用（宿主机原生选目录）；局域网访问时按钮禁用，请粘贴 LWA 机器绝对路径。导入仍复制进工作区，非就地运行。API 错误文案不含 `[ZIP_IMPORT_ERROR]` 等前缀。
* **构建代理**：顶栏「构建代理」打开对话框，填写 `http://` 或 `https://` 地址并勾选启用（不接受 `socks5://` / `socks5h://`，默认构建里的 pip 没有 SOCKS 依赖；端口须为 1-65535 的合法数字，非法值保存时直接拒绝且不改动配置文件）。点击「检测系统代理」可读取**管理服务所在主机**的代理设置并填入地址；检测不会自动启用或保存。它优先读取管理服务进程的代理环境变量，在 macOS 和 Windows 上没有代理环境变量时还会读取系统代理；Linux/WSL 依赖进程环境变量，不会读取桌面设置或 Windows 代理。仅支持直接设置的 HTTP/HTTPS 代理，不解析 PAC 自动配置。保存后写入工作区 `local-web.yml` 的 `buildProxy`。下一次镜像构建的**容器内安装命令**与宿主机前端安装会使用它；已经运行的应用不走这个代理。代理只经 Compose `build.args` 以 Docker 内置代理构建参数传入构建，不在 Dockerfile 声明 `ARG`，因此地址与凭据不会留在 `docker history`。填宿主机回环地址（如 `http://127.0.0.1:10808`）时，构建参数会自动改写为 `host.docker.internal` 并注入 host-gateway 映射——代理需开启「允许局域网连接」（监听 `0.0.0.0`），仅监听 `127.0.0.1` 的代理容器内仍连不上。**基础镜像拉取由 Docker daemon 执行，不走该代理**：拉镜像需要代理时请配置 Docker daemon/Desktop 代理或 registry-mirrors（构建时若检测到 daemon 未配代理会输出提示）。关闭启用后，下一次构建恢复直连。
* **浏览量**：列表列展示累计访问；点击打开按天分布、最近命中与独立 IP 列表（IMP-024/026；page 级过滤 IMP-025）。

> **状态说明（DEV-043 / BUG-071 / IMP-033 / IMP-058）**：Caddy 模式下，enabled 静态实例在 master（admin :2019）不可达时显示 `网关不可达`，在 master 在线但站点端口不通时显示 `配置无效`——二者均不再被误标为普通「已停止」。Docker 观测失败时显示 unknown / 权限提示，不误写 stopped。**V0.7.7** 起容器实例新增 `verifying`（Gate-C 存活探针等待中）、`degraded`（必选探针通过但可选探针失败）状态，参见 [FAQ](faq.md#实例状态为-verifying--degraded--failedimp-058-gate-c)。**V0.7.9**：无源码声明/发现的健康端点时，API 能力无法实证会显示降级而非失败。**V0.8.6（第二批 CHK-252）**：`degraded` 不再写入进程状态——验证有警告时实例保持「运行中」，状态徽标旁叠加琥珀色「验证有警告」徽标，明细见实例 `verificationSummary`；就绪门槛探针可用 `lwa probe set` 显式声明。**V0.9.3（S2-02 / issue #57-2）**：`queued` / `cancelled` 不再作为实例状态出现——实例 `status` 只由观测决定；排队、取消、构建失败写入操作态 `lastOperation`（列表字段表与详情「最近操作」），运行中实例的构建失败以「最近一次重建失败（时间、原因）」徽标叠加展示，不再整体标红。点击「恢复」会先尝试拉起 Caddy master 再 restart 实例（容器路径仍受能力门禁）。
* **实例详情**：manifest、构建记录、事件流、资源占用、分类日志查看器；含路径别名说明与 CLI 等价命令提示。**V0.9.4（S3-01）**：容器实例新增「**版本**」面板——列出留档版本（构建号 / 时间 / 源码哈希 / 镜像是否仍在 / 构建结果，当前在用的版本标注「在用」、不可回滚），底部显示保留数与合计磁盘占用；每个可回滚版本带「回滚」按钮，点击弹二次确认窗——固定展示 D8 范围说明（只回滚镜像与生成配置，**不回滚 `data/`**；回滚后验证失败自动尝试恢复原版本，恢复结果实证验证后如实上报），preStart 含迁移命令时追加红色警示「旧版本代码可能无法兼容当前数据库结构」并要求勾选「我已了解风险」后才可提交；提交携带列表快照的 `expectedRevision`，冲突时提示刷新后重试。**V0.9.4（S3-04）**：详情中新增「**最近操作**」区块，按时间倒序合并生命周期事件、构建记录与最近一次操作态（`lastOperation`），每行标注发起方（`cli` / `manager` / `daemon` / `agent`；历史事件显示 `unknown`），构建行附耗时。完整时间线用 `lwa logs <id> --timeline`，接口为 `GET /api/instances/{id}/timeline?limit=`。
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

### 冗余清理补充保护与实例配置

清理弹窗列出实例、状态、别名、更新时间和处置。默认跳过独立配置；勾选“允许丢失独立配置”才向
`POST /api/redundant/remove?allowConfigLoss=true` 传递显式授权。运行/过渡态保护始终有效，
可清理数量为 0 时确认按钮禁用。预览后的状态或配置变化会在后端删除锁内重新检查。
返回的 `skipped` 也包含运行态保护和单实例删除失败，不只表示独立配置。

点击“有意保留”将 `redundancyAcknowledged` 设为 true，实例退出冗余候选与徽标；
可用 `lwa configure <id> --no-acknowledge-redundancy` 撤销。
`GET /api/redundant` 及列表中的冗余实例新增 `skipReasons`（不可覆盖的保护原因）、
`pathAlias`、`updatedAt`；原 `configLossReasons` 保留。

经现有 API 鉴权的 `PATCH /api/instances/<id>/settings` 接受以下字段的部分更新，拒绝未知字段：

```json
{"buildEnv":{"VITE_BASE":"/demo/"},"buildBaseFromAlias":false,"redundancyAcknowledged":true,"systemDeps":["ffmpeg"],"buildHooks":["apt-get update"],"hostBuild":"cd frontend && npm run build","preSync":null,"extraVolumes":["/mnt/share:/data/share:ro"]}
```

`extraVolumes`（S3-05）为容器实例的额外 bind 挂载整组替换（`SRC:DST[:ro]` 字符串数组，非容器实例拒绝）；保存前执行与渲染相同的结构校验与安全审计，返回体含 `extraVolumes`。挂载变化在 `lwa restart` 时生效。

`buildEnv` 为整组替换，null 清空；布尔字段必须是真正布尔值。`buildEnv` / 别名 base
跟随支持宿主前端构建，也支持 docker-compose 容器构建（Dockerfile ARG/ENV + Compose
`build.args`，issue #39）。`systemDeps` 支持两类容器：Python（Debian，apt 切源链，
渲染在 `COPY current/` 之前）与 Node（Alpine，`apk add --no-cache`，渲染在 npm 依赖安装之前）。
`buildHooks` 在依赖安装之后执行，补不了 node-gyp 这类安装期失败（DOC-201）。其他类型保存非空
`systemDeps` 会被拒绝。
`systemDeps` / `buildHooks` 为整组替换（数组），接口不会自动 rebuild。设置弹窗暂未
暴露 `systemDeps` / `buildHooks`，推荐用 `lwa configure <id> --system-deps /
--build-hook`（含 `--clear-*` 清空变体）操作。
