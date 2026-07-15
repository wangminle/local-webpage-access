# LWA 网关切换与访问地址可用性复盘

> 分析时间：2026-07-09  
> 范围：`runtime/` 工作区、Caddy ↔ builtin 静态后端切换、管理页访问地址、路径别名（IMP-006 / IMP-023）  
> 触发：管理页 `http://127.0.0.1:17800/` 上四个访问链接均打不开；此前「入口 200」报告与浏览器实测白屏/长时间加载不一致  
> 相关历史：`local-webpage-access-caddy-startup-incident-20260708.md`、`local-webpage-access-runtime-analysis-20260707.md`、IMP-010 / BUG-069~077 / IMP-020 / IMP-022 / IMP-023

---

## 1. 结论摘要

本次复盘确认三类**能力缺口**（用户归纳三点均成立），并补充网关切换时应完成的交接清单：

| # | 缺口 | 现状 | 严重度 |
|---|------|------|--------|
| **G1** | 网关重启 / 后端切换时**主动校验并刷新**实例访问地址（LAN IP） | `lanUrl`/`routeUrl` 仅在 start/enable 时写入；换网后管理页仍链旧 IP | P0 |
| **G2** | CLI / doctor / skill **review 访问配置是否真可用**（含子资源，不只 HTML 200） | `lwa doctor` 只探 127.0.0.1 端口存活；无「管理页 URL 真探活 + SPA 资源」检查 | P1 |
| **G3** | Caddy ↔ builtin **彻底交接**：停旧进程、独占端口、清 pid / 服务态 | 切到 Caddy 时**不杀仍存活的 builtin `http.server`**；现场已双开；另见 §2.7 测试泄漏占 :2019 | P0 |
| **G4**（补充） | 切换后同步 **别名入口 / pageviews 日志源 / manifest.static.gateway** 与真实后端一致 | 部分有（IMP-022 拦截设别名、pageviews 对齐 detect_backend），缺统一「切换事务」 | P1 |
| **G5**（补充） | 健康探测与运维报告勿把「入口 HTML 200」当成「页面可渲染」；亦勿把「CLI 报 FAIL」当成「真失败」 | IMP-023 已文档化但缺子资源验收；`caddy start --pingback` 超时误报见 §2.7 / 候选 BUG-102 | P1 |
| **G6**（补充） | 切换 / access review 后**自动检查是否需要 rebuild**；默认只提示，可选 `--rebuild-if-needed` 自动重建 | 已有 `lwa access review` 能检出 IMP-023，但 `gateway on` 未默认挂 review；无「检查→可选 rebuild」闭环 | P1 |

**一句话**：服务「在跑」≠ 管理页链接可用 ≠ 别名下 SPA 可渲染；CLI「报失败」≠ 进程未起来；后端切换必须做成**原子交接**，并带访问配置 review、端口独占断言，以及「是否需 rebuild」的检查（默认提示、可选自动重建）。

---

## 2. 分析过程

### 2.1 现象

1. 管理页 V0.5.0 显示 3 实例均为「运行中」，访问地址列有四个可点链接：
   - demo-static「端口」
   - voiceprint「端口」+ `/vp-app-demo-v3/`
   - prd-workflow「端口」
2. 用户点击后长时间停滞在加载状态，进不了页面。
3. 此前运维/助手报告「多个地址返回 200」，与用户实测矛盾。

### 2.2 取证步骤

1. `lwa list` / `lwa gateway status`：实例 running，后端 `caddy`，别名入口宣称可用。  
2. 读管理页 API `/api/instances`：四个链接的 `href` 来自 `lanUrl` / `routeUrl`。  
3. 对比本机网卡 IP 与 manifest 中 IP。  
4. 分别 curl：旧 LAN IP、当前 LAN IP、`127.0.0.1` hostPort、`:8080/<alias>/`。  
5. 拉取别名入口 HTML，解析 `src`/`href`，再 curl 浏览器会实际请求的绝对路径资源。  
6. `lsof` 核对 18000 / 18001 监听进程是否双开。  
7. 对照源码：`helpers.js`（链接生成）、`ports.py`（LAN URL）、`static_gateway.enable/disable`、`gateway_service.start/stop`、`doctor.check_caddy_health`、IMP-023 文档与 skill。

### 2.3 根因 A — 陈旧 LAN IP（管理页四链接全挂）

| 项 | 值 |
|----|-----|
| 本机当前 LAN IP（`en1` / `detect_lan_ip()`） | **`10.181.239.115`** |
| 实例 `local-web.json` / API 中的 `lanUrl`/`routeUrl` | 仍为 **`10.181.239.49`** |
| 对 `.49` 的探测 | 超时 / `No route to host` / `Host is down` |
| 对 `127.0.0.1` 与 `.115` 的 hostPort | **HTTP 200** |

管理页「端口」「/vp-app-demo-v3/」仅渲染已持久化的 URL（`manager_static/helpers.js` → `LWA.urlHtml`），**不会**在列表刷新时用当前 `resolve_lan_ip()` 重算。  
`build_network_entry()` 只在 start / enable / 改别名等路径调用；换 Wi‑Fi / DHCP 后地址漂移不会自愈。

因此：用户从管理页点的四个地址，**全部指向已失效的旧 IP**——与「服务其实在跑」不矛盾。

### 2.4 根因 B — IMP-023：别名下 SPA 绝对资源路径（「200」报告不完整）

以 `voiceprint-v3-demo` 为例，`:8080/vp-app-demo-v3/` 返回的 HTML 含：

```html
<script type="module" crossorigin src="/assets/index-DBjU_3z5.js"></script>
<link rel="stylesheet" crossorigin href="/assets/index-C5cTP705.css">
```

浏览器在子路径下解析绝对路径 `/assets/...` → 请求 **`:8080/assets/...`（无别名前缀）**。

| 实际请求 | 返回 |
|----------|------|
| `:8080/assets/index-DBjU_3z5.js`（浏览器行为） | **HTTP 200，Content-Length: 0** |
| `:8080/vp-app-demo-v3/assets/index-DBjU_3z5.js`（带前缀） | **HTTP 200，约 120999 字节** |

`:8080` 站点块仅 `handle_path` 匹配已配置别名（如 `/vp-app-demo-v3/*`、`/prd-workflow/*`）；未匹配路径可落到空 200。主 JS 为 0 字节 → Vue 不启动 → 白屏 / 「加载中」。  
`prd-workflow` 的 `/css/`、`/js/`、`/favicon.svg` 等同理。

**直连 hostPort**（无路径前缀剥离）时，绝对路径可正确命中，页面可渲染——故「端口直达可用、别名入口坏」是预期限制，不是网关未启动。

IMP-023 的既有「修复」仅为：`lwa alias set` 提示用 Vite `base: './'` 或 `--base=/<alias>/` **重构建**；若应用未按提示重建，别名下就会坏。此前「都能访问」只测了入口 HTML，**未测子资源**——属于探测口径疏漏。

### 2.5 根因 C — builtin ↔ Caddy 切换后进程双开（G3 现场证据）

2026-07-09 约 12:37 本机监听（节选）：

| 端口 | 进程 |
|------|------|
| **18000** | Python `http.server` pid **65599**（builtin）**与** Caddy pid **93524** 同时 LISTEN |
| **18001** | Python `http.server` pid **65793** **与** Caddy 同时 LISTEN |
| 8080 / 2019 | 仅 Caddy |

代码路径：

- `StaticGateway.enable()` 在 Caddy 分支：**只** `_clear_stale_static_pid`（清**已死** pid 文件）→ 生成 site/alias → `reload_all()`；**不调用** `_stop_builtin()`。  
- `disable()` 仅在 `detect_backend()=="builtin"` 时 `_stop_builtin()`；切到 Caddy 后 disable 也不会杀旧 Python。  
- BUG-070 明确只处理 stale（死）pid；BUG-077 覆盖「切到 builtin 后仍停残留 Caddy master」，**反向「切到 Caddy 后停残留 builtin」缺失**。

结果：同一 hostPort 上行为不确定（IPv4/IPv6 分流、谁先 accept 等），排障极难。

> **延伸（见 §2.7）**：陈旧监听不只来自 builtin↔caddy 切换，也可来自 **pytest 泄漏的 Caddy 孤儿**占 `:2019`。端口独占断言必须按「端口上是谁在听」拦截**所有**非目标监听者，否则 `port_contention`（§5-F）会漏测。

### 2.6 现有能力对照（为何「好像有检查」却漏掉）

| 能力 | 实际做什么 | 能否发现本次问题 |
|------|------------|------------------|
| `lwa gateway on/status` | 启停 Caddy；打印**当前** `resolve_lan_ip()` 拼的别名入口模板 | ❌ 不刷新各实例已存 `lanUrl`/`routeUrl`；不杀 builtin；见 §2.7 对孤儿 :2019 的误报 |
| `lwa doctor` + IMP-020 `check_caddy_health` | admin :2019、Caddyfile validate、stale caddy.pid、**127.0.0.1** 上 hostPort / `:<port>/<alias>/` | ❌ 回环通即 OK；不比对 LAN IP；不探子资源 |
| `lwa doctor` → `port_pool` | 抽样探测池端口 + `managerPort` 是否「被占用」 | ⚠️ **既有误报**：常把 **17800（manager）/ 8080（别名入口）/ 已分配 hostPort（18000–18002）** 等合法自用端口报成 FAIL（OPS-005 / OPS-030 / OPS-031 均有记录）；与「访问真可用」无关，但干扰切换后巡检 |
| `lwa-diagnose-health-check` skill | 实例健康检查失败排障 | ❌ 不 review 访问 URL 配置 |
| IMP-023 / alias set 提示 | 文档 + CLI 提示 SPA base | ⚠️ 不强制、不验收别名下资源 |
| BUG-070 / BUG-077 | 死 pid 清理；builtin 配置下仍停 Caddy master | ⚠️ 单向/半套，无完整切换事务 |

### 2.7 切换过程次要发现（OPS-031 当日，强化 G3 / 镜像 G2·G5）

以下两项发生在 **2026-07-09 runtime 由 builtin 切到 caddy**（`task-list` **OPS-031**），与 §2.3–2.5 主因并列，纳入本文以保持与运维记录自洽。

#### 补充 1 — pytest 残留 Caddy 占 `:2019`（强化 G3）

切换前发现 **admin `:2019` 已被测试泄漏的 Caddy 孤儿占用**（pid **75224**，来自 `test_process_zip_starts_determ0` 一类用例向全局 `:2019` 泄漏真实 master——与 CHK-016 所述 flaky 根因同类）。

影响：

- `lwa gateway status` **误报「运行中、pid=?」**（admin 在线但非本工作区 `run/caddy.pid` 所记进程）；
- 一度阻挡真正的工作区 Caddy 绑定 `:2019`；
- 运维需**手工杀掉孤儿**后，`lwa gateway on` 才能干净拉起 pid **93524**。

与 §2.5 的 builtin+caddy 双开是**不同来源的同类问题**：陈旧监听 ≠ 仅切换残留。  
因此 §4.1 / §5-F 的 **端口独占 / `port_contention`** 必须断言：关键端口（至少 `:2019`、`staticGatewayPort`、各 enabled `hostPort`）上**不得存在非当前后端、非当前工作区预期的监听者**——不论来自切换、崩溃还是测试泄漏。

#### 补充 2 — `caddy start --pingback` 超时误报启动失败（镜像 G2/G5；候选 BUG-102）

当日 `lwa gateway on` 报：

```text
[LIFECYCLE_ERROR] Caddy master 启动失败（caddy start … pingback 超时约 20s）
```

但事后核对：

| 信号 | 结果 |
|------|------|
| `run/caddy.pid` | 已写入 **93524** |
| `:2019` / `:8080` | 在服务 |
| 实例经 Caddy 可达 | doctor `caddy_health` OK（入口 HTML 层） |

这是 **「报告 FAIL ≠ 真失败」**——与本文主题 **「报告 OK ≠ 真可用」**（入口 200 / 假绿）互为镜像，同属**状态报告缺口**，会误导运维以为网关没起来而重复操作或误判回滚。

**候选修复（BUG-102）**：`caddy start` 因 `--pingback` 超时返回非零时，**勿直接**抛 `LIFECYCLE_ERROR`；应回退用 **admin `:2019` 探活**（及可选读 pidfile）判定：admin 已在线则视为启动成功（可 WARN「pingback 超时但 admin 已就绪」）；仅当 admin 仍不可达才判失败。与 `StaticGateway.caddy_start` 在 returncode==0 后轮询 admin 的意图对齐，补上 returncode≠0 但 master 已起来的分支。

---

## 3. 用户三点诉求（确认）

1. **重启网关时就应主动检查**访问地址是否仍正确（至少：当前 LAN IP vs 各实例 `lanUrl`/`routeUrl`；可选：对声明 URL 做 HTTP 探活）。  
2. **应有 CLI 或 skill** 专门 review「内部网页访问配置是否可用」（管理页同源 URL + 别名入口 + 关键子资源，而非仅 HTML 200）。  
3. **无论 caddy→builtin 还是 builtin→caddy**，都必须彻底切换本地服务与资源占用，禁止旧服务继续占端口不释放。

以上三点当前**均未完整实现**；第 3 点已有部分相关 bugfix，但现场双开证明交接未闭环。

---

## 4. 网关切换时应完成的操作清单（目标态）

以下为「切换事务」建议清单：改 `local-web.yml` 的 `staticGateway`、执行 `lwa gateway on/off`、`lwa update` 导致后端变化、或 `detect_backend` 降级/升回时，均应走同一套交接（可做成 `StaticGateway.switch_backend` / `gateway_service.reconcile_backend`）。

### 4.1 进程与端口（P0，对应 G3）

| 步骤 | builtin → caddy | caddy → builtin |
|------|-----------------|-----------------|
| 停止旧静态服务 | 对每个 enabled 静态实例 `_stop_builtin`（含**仍存活**进程，不只 stale pid） | `caddy_stop` / `stop_gateway`（已有 BUG-077 方向） |
| 确认端口独占 | `lsof`/connect：`:2019`、`staticGatewayPort`、各 hostPort 上**无非目标监听者**（切换残留 **或** pytest/手工孤儿，见 §2.7） | 同左；确认 :2019 / :8080 已退出 |
| 清理 pid / 服务态 | 删 `run/static-*.pid`；写/更新 `run/caddy.pid`、`run/gateway.json` | 清 `caddy.pid`、gateway.json enabled=false；写新的 `static-*.pid` |
| 再拉新后端 | `caddy_start`（失败时按 BUG-102 用 admin 探活兜底）+ `_sync_main_config` + reload；或对实例 `enable(..., caddy)` | 各实例 `_start_builtin`；**不**保留 Caddy 的 `:hostPort` site 监听（见下） |

### 4.2 配置与路由（P0/P1）

| 步骤 | 说明 |
|------|------|
| 重组主 Caddyfile | 按磁盘 `sites/`、`aliases/` 实际文件 `_sync_main_config`（延续 BUG-069，杜绝悬空 import） |
| 别名策略 | 切到 **builtin**：别名入口不可用——应 WARN/doctor FAIL；可选自动清 alias 片段或保留片段但标记「未启用」（IMP-022 已拦截**新设**别名） |
| 切到 **caddy** | 若 manifest 仍有 `routeHost`，应重新 `generate_alias_config` + reload，保证 `:8080/<alias>/` 与 hostPort 一致（IMP-021 端口漂移） |
| `manifest.static.gateway` | 写回真实后端（`caddy`/`builtin`），避免 UI/日志源与运行时不一致 |
| registry `static_sites` | enabled / host_port 与真实监听一致 |

### 4.3 访问地址（P0，对应 G1）

| 步骤 | 说明 |
|------|------|
| 重算 LAN IP | `resolve_lan_ip(config)` |
| 重写 network | 对 running 实例 `build_network_entry(...)` 更新 `lanUrl` / `routeUrl`（保留 hostPort、routeHost） |
| 管理页同源 | API 列表立即反映新 URL；可选同时展示 `127.0.0.1` 本机链接作兜底 |
| 漂移告警 | 若旧 `lanUrl` host ≠ 当前 IP → doctor WARN + gateway on 结束时打印差异表 |

### 4.4 可观测性与浏览量（P1，对应 G4）

| 步骤 | 说明 |
|------|------|
| pageviews 日志源 | 与 `StaticGateway.detect_backend()` 对齐（BUG-091 已修探测；切换后应确认 ingest 读 gateway.log vs static-access.json） |
| access log | Caddy 统一入口 JSON log（IMP-024）在切走 caddy 后停止写入；切回时确保 log 指令仍在主配置 |
| 事件审计 | registry 记 `gateway_backend_switch`（from/to、pid 清理结果、IP 刷新结果） |

### 4.5 验收探测（P1，对应 G2 / G5）

切换或 `lwa doctor --access-review`（名称待定）结束时应至少：

1. **声明 URL 探活**：对每个实例的 `lanUrl`、`routeUrl`（若有）发 HTTP GET（超时短），记录状态码与耗时；失败则 FAIL/WARN。  
2. **回环对照**：`http://127.0.0.1:<hostPort>/` 必须通（区分「服务死了」vs「仅 LAN URL 陈旧」）。  
3. **LAN IP 一致性**：`lanUrl`/`routeUrl` 的 host ∈ {当前 resolve_lan_ip, 127.0.0.1} 或与 `manualLanIp` 一致。  
4. **别名子资源抽检（SPA）**：若存在 `routeUrl`，解析入口 HTML 中以 `/` 开头的 `src`/`href`，请求 `http://127.0.0.1:<staticGatewayPort><绝对路径>`；若得到 0 字节或 404，而 `routeUrl + 相对路径` 有实体 → 报 **IMP-023 风险**（WARN），勿标「别名可用」。  
5. **端口独占**：`:2019` / `staticGatewayPort` / 各 hostPort 上监听进程符合当前后端与工作区预期（禁止 builtin+caddy 双开；亦禁止测试/外部孤儿占 admin，见 §2.7）。  
6. **启动结果可信**：若刚执行 `gateway on`，CLI 退出码与 admin 探活一致（避免 pingback 假 FAIL，候选 BUG-102）。
7. **是否需 rebuild（G6）**：见 §4.6；默认只列出建议重建的实例，不自动 `lwa rebuild`。

### 4.6 切换后 rebuild 兼容检查（P1，对应 G6）

产品共识：**换网关默认做 access / 资源兼容检查，不要默认自动 rebuild**；用户可显式选择自动重建。

| 项 | 约定 |
|----|------|
| **触发「需要 rebuild」** | 仅 **IMP-023 空 200**（别名入口下绝对路径资源 200 且 0 字节，带前缀有实体）。LAN 漂移、端口双开、回环不通等只提示对应命令，**不**触发 rebuild |
| **默认行为** | 检查并打印报告 +「建议 rebuild」实例列表与命令（如 `lwa rebuild <id>`） |
| **可选自动** | `--rebuild-if-needed`：对命中实例依次调用 `rebuild_instance`，汇总成败；仍不修改应用源码中的 Vite `base`（需运维自行固化） |
| **入口（本轮）** | ① `lwa gateway on`：交接收尾后**默认**跑 `review_access`，支持同一开关；② `lwa access review`：现有复核 + 同一开关。不挂 `gateway off` / `lwa update`（后续可扩） |
| **不做** | 无开关时绝不自动 rebuild；本轮不统一 `switch_backend`（仍属 I3） |

```text
lwa gateway on
lwa gateway on --rebuild-if-needed
lwa access review
lwa access review --rebuild-if-needed
```

### 4.7 运维操作顺序（建议 playbook）

**builtin → caddy（推荐顺序）**

```text
1. 备份 local-web.yml
2. 设置 staticGateway: caddy
3. 停止所有静态实例的 builtin（或统一 switch 事务内完成）
4. 确认 18000–19999 相关端口无 Python http.server
5. lwa gateway on（或 switch 内 caddy_start）
6. 对 desired=running 静态实例 enable/restart（生成 sites/aliases + reload）
7. 刷新全部实例 lanUrl/routeUrl
8. access-review（含 SPA 子资源；默认提示需 rebuild 的实例，见 §4.6 / G6）
9. （可选）`lwa gateway on --rebuild-if-needed` 或对命中实例 `lwa rebuild`
10. lwa doctor
```

**caddy → builtin**

```text
1. 设置 staticGateway: builtin
2. lwa gateway off（须真正停 master，BUG-077）
3. 确认 :8080 / :2019 无监听；可选保留或归档 aliases/*.conf
4. 对静态实例 start/enable → _start_builtin
5. 刷新 lanUrl；routeUrl 标不可用或清空展示
6. access-review + doctor（别名入口不可用时不应再因 IMP-023 触发 rebuild）
```

---

## 5. 建议落地项（供后续 WBS / task-list）

> 建议 A–F 为方向编号；**BUG-102** 已在评审后纳入候选（见下表与 `task-list`）。

| 建议 | 类型 | 内容 |
|------|------|------|
| **A. 切换事务** | 功能/修复 | `enable(caddy)` 前强制停活着的 builtin；`gateway on` / 配置变更后跑 backend reconcile；端口独占断言（含测试孤儿，§2.7） |
| **B. LAN URL 刷新** | 功能 | `lwa doctor` 检测 stale LAN IP；`lwa gateway on` / `lwa update` / 新命令 `lwa access refresh` 批量重写 network |
| **C. access review** | 功能 | CLI：`lwa doctor --access` 或 `lwa access review`；skill：`lwa-review-access-urls`；覆盖声明 URL + 子资源 + 双开 |
| **D. 管理页兜底** | 体验 | 列表同时给「本机 127.0.0.1」链接；或展示时用当前 LAN IP 重写展示（持久化仍可后台修） |
| **E. SPA 别名验收** | 文档/测试 | 别名「可用」定义改为「HTML + 抽样绝对路径资源在别名语义下可达」；回归用例固定 0 字节空 200 场景 |
| **F. 切换审计** | 可观测 | 事件 + doctor 项 `backend_handoff` / `lan_url_stale` / `port_contention`（contention 含任意来源陈旧监听） |
| **G. pingback 假失败** | 修复 | **候选 BUG-102**：`caddy start` pingback 超时后回退 admin `:2019` 探活，勿直接 `LIFECYCLE_ERROR`（§2.7） |
| **H. port_pool 误报** | 修复/调整 | doctor 排除 manager / 别名入口 / registry 已分配 hostPort 等合法自用端口（既有 OPS-005 类问题，切换巡检时易误导） |
| **I. 切换后 rebuild 检查（G6）** | 功能 | `gateway on` 默认挂 `review_access`；`access review` / `gateway on` 支持 `--rebuild-if-needed`；仅 IMP-023 空 200 触发建议/自动 rebuild（见 §4.6） |

---

## 6. 当前环境快照（2026-07-09）

| 项 | 值 |
|----|-----|
| 工作区 | `.../local-webpage-access/runtime` |
| `staticGateway` | `caddy`（由 builtin 切来，见 OPS-031） |
| Caddy | pid 93524；`:8080` / `:2019` / `:18000` / `:18001`（`gateway on` 曾 pingback 超时误报，见 §2.7） |
| 切换前孤儿 | pytest 泄漏 Caddy pid **75224** 曾占 `:2019`（已手工清理，OPS-031） |
| 残留 builtin | pid 65599→18000，65793→18001（**双开未清**） |
| 当前 LAN IP | `10.181.239.115` |
| 管理页链接 IP | 仍为 `10.181.239.49`（失效） |
| 别名 | `vp-app-demo-v3` → 18001；`prd-workflow` → 18002 |
| 直连可用（回环） | `127.0.0.1:18000/18001/18002` HTML+资源完整 |
| 别名入口 | HTML 200；绝对 `/assets`/`/css` 在 `:8080` 根上为空 200（IMP-023） |

**临时可用地址（不依赖管理页旧链接）：**

- demo-static：`http://127.0.0.1:18000/` 或 `http://10.181.239.115:18000/`  
- voiceprint：`http://127.0.0.1:18001/` 或 `http://10.181.239.115:18001/`  
- prd-workflow：`http://127.0.0.1:18002/` 或 `http://10.181.239.115:18002/`  
- 别名：需 SPA 按 IMP-023 重构建后才建议作为主入口

---

## 7. 关键代码与文档索引

| 路径 | 相关性 |
|------|--------|
| `src/local_webpage_access/static_gateway.py` | enable/disable、builtin 启停、Caddyfile 组装；**缺切 Caddy 时停活 builtin** |
| `src/local_webpage_access/gateway_service.py` | gateway on/off；BUG-077 反向停 Caddy |
| `src/local_webpage_access/ports.py` | `detect_lan_ip` / `build_network_entry` / `build_route_url` |
| `src/local_webpage_access/doctor.py` | `check_caddy_health`（回环探活，无 LAN/子资源） |
| `src/local_webpage_access/manager_static/helpers.js` | `LWA.urlHtml` 直接绑 `lanUrl`/`routeUrl` |
| `src/local_webpage_access/cli/alias.py` | IMP-023 SPA base 提示 |
| `docs/operations-playbook.md` | Caddy vs builtin 选型；可增「切换交接」节 |
| `docs/manager-page.md` / `docs/known-limitations.md` | 路径别名、SPA 限制 |
| `design/plan/local-webpage-access-caddy-startup-incident-20260708.md` | 前序 Caddy 生命周期事故 |
| `design/plan/local-webpage-access-runtime-analysis-20260707.md` | P9 绝对路径等 |

---

## 8. 非目标（本次文档不展开）

- 自定义域名 / Host 头路由（已知不支持）。  
- 自动改写已构建 SPA 的 `/assets` 路径（产品选择仍是「构建时设 base」，非运行时 HTML 重写）。  
- 容器业务 `.env` / 资源档位（阶段 2/3 另案）。

---

## 9. 附录：探测口径建议（避免再次「假 200」）

```text
对每个「声称可用」的入口 URL U：
  1. GET U → 记录 status、bytes、time
  2. 若 Content-Type 像 HTML：
       解析绝对路径资源 R（src/href 以 / 开头且非 //）
       GET origin(U) + R → 若 status∈{200,304} 且 bytes==0 → 记 EMPTY_BODY
       GET dirname(U) + R 的「带前缀」变体（若 U 含 alias）→ 对比
  3. 报告：入口 OK ∧ 无 EMPTY_BODY ∧ 无端口双开/孤儿监听 ∧ lanUrl host 未过期
     才允许对外宣称「可访问」
  4. 对称：gateway on / caddy_start 若 CLI 报失败，须用 admin :2019（及 pidfile）复核，
     避免「报告 FAIL ≠ 真失败」（§2.7 / BUG-102）
```

---

## 10. 落地状态与二次审查（2026-07-09 下午）

DEV-062~064 / BUG-102 / OPS-033 / CHK-031 已宣称落地，并对未提交实现做了对照审查。  
**结论：P0 主路径大体可用，但尚不能宣称「全部开发完成」——下列 Critical / Important 须先修。**

### 10.1 已确认落地

| 项 | 实现要点 |
|----|----------|
| G1 / B | `access.refresh_network_entries`、`lwa access refresh`；`gateway on` 收尾调用 |
| G2 / C / E | `lwa access review`：声明 URL 探活 + IMP-023 空 200 子资源（voiceprint 实测 WARN） |
| G3 / A（部分） | `enable` 前 `_stop_live_builtin_if_any`；`stop_all_builtin`；doctor `backend_handoff` |
| BUG-102（意图） | `caddy_start` 非零/超时时回退 admin 探活 |
| D / F / H | `localhostUrl` +「本机」链接；`lan_url_stale` / `port_contention`；`port_pool` 排除自用端口 |

### 10.2 Critical（阻断「完成」宣称）

#### C1 — macOS 上 `pgrep -af` 无法枚举 pid-less 孤儿（打穿 G3）

`StaticGateway._enumerate_workspace_builtin_pids` 使用 `pgrep -af http.server`。  
Darwin 实测：`pgrep -af` **只输出 PID**；`pgrep -lf` / `-fl` 才带完整命令行。  
代码要求行内含 `http.server` 与工作区 `apps/` 路径 → **枚举恒为空**，复盘现场那类无 `static-*.pid` 的孤儿清不掉。  
单测 mock 了枚举方法，故未暴露。

**修复方向**：改用 `pgrep -lf`（或 `ps`）；补不 mock 的输出格式回归；`tests/conftest.py` 同类调用一并改。

#### C2 — BUG-102 admin 兜底过宽，可能假绿

`caddy_start` 对 `FileNotFoundError`（PATH 无 caddy）也走 admin 探活；且成功时**不校验**本工作区 `caddy.pid`。  
叠加 §2.7：pytest 孤儿占 `:2019` 且 admin 可达时，可能误报启动成功并写 `gateway.json`。

**修复方向**：`FileNotFoundError` 立即失败；仅对 `TimeoutExpired` / 非零退出（pingback 类）做 admin 兜底；admin 在线时优先确认工作区 pidfile 进程存活，pidfile 指向已死进程则勿认领孤儿 admin。

### 10.3 Important

| ID | 问题 | 修复方向 |
|----|------|----------|
| **I1** | `start_gateway` 先 `caddy_start` 再 `stop_all_builtin`，与 §4.1「先停旧再拉新」相反；停后未 `reload_all` | 先停 builtin → 再 `caddy_start`；若清理了占用 hostPort 的进程则 reload |
| **I2** | `review_access` 要求 `routeUrl and path_alias`；`prd-workflow` 磁盘有别名片段且 `/css` 空 200，但 `network.routeUrl=null`、`container.routeMode=port`，review 标 OK 漏检 | `_extract_host_port_alias` 从 `static.routeHost` / `network.routeHost` 兜底；无 `routeUrl` 时用 `127.0.0.1:entry/<alias>/` 合成探测 |
| **I3** | 切到 caddy 未批量同步 `manifest.static.gateway`、未统一 `switch_backend`（G4） | 后续：finalize 写回真实 backend；或独立 reconcile（本轮可记缺口） |
| **I4** | `lwa update` 不自动 `access refresh`；无 access-review skill / doctor `--access` | 后续 P1 |
| **I5** | Windows 无 `pgrep`/`lsof` 时孤儿清理与端口争用检查能力弱 | known-limitations 明示 |

### 10.4 运行时观察（审查时）

- 18000/18001 已无 Python+Caddy 双开；LAN IP 已刷新为 `10.181.239.115`。  
- `voiceprint` `public/index.html` 可能再次变为绝对 `/assets/...`（未固化 `vite.config.js` 的 `base: './'`）→ `access review` 仍应 WARN。  
- `prd-workflow`：`static.routeHost=prd-workflow` 但 `network.routeUrl=null`；`:8080/css/main.css` 空 200、带前缀有实体——**I2 漏检实例**（修复后应 WARN）。

### 10.5 修复落地（同日续）

| ID | 状态 | 改动要点 |
|----|------|----------|
| **C1** | ✅ 已修 | `pgrep -lf`；`test_enumerate_workspace_builtin_pids_parses_pgrep_lf`；`conftest` 同步 |
| **C2** | ✅ 已修 | `FileNotFoundError` 硬失败；admin 兜底须 `_workspace_caddy_pid_alive`；拒认领孤儿 admin |
| **I1** | ✅ 已修 | `start_gateway`：先 `stop_all_builtin` 再 `caddy_start`；清孤儿后 `reload_all` |
| **I2** | ✅ 已修 | `_extract_host_port_alias` 多段兜底；无 `routeUrl` 时合成 `127.0.0.1:entry/<alias>/` 探测 |
| **I3–I5** | 未本轮 | G4 统一 switch、`lwa update` 钩子、skill、Windows 枚举——仍为后续 P1 |

---

## 11. G6 实现规格（切换后 rebuild 兼容检查）

> 用户确认（2026-07-09）：默认只检查；`--rebuild-if-needed` 时对命中实例自动 rebuild。入口先落 **`lwa gateway on` + `lwa access review`**。

### 11.1 行为

1. `review_access` 报告中，任一子资源 `empty_200=true` → 该实例 `needs_rebuild=true`。  
2. 人类可读报告末尾增加「建议重建」段：实例 ID、原因（IMP-023）、建议命令；并提示可加 `--rebuild-if-needed`。  
3. `lwa access review [--rebuild-if-needed] [--json]`：无开关仅检查；有开关则对 `needs_rebuild` 实例调用 `lifecycle.rebuild_instance`，打印每实例成败。  
4. `lwa gateway on [--rebuild-if-needed]`：现有交接成功后**默认**调用 `review_access` 并打印报告；有开关则同上自动 rebuild。  
5. JSON 输出增加 `needsRebuild`（实例级）与汇总列表，便于脚本。  
6. 退出码：`access review` 仍在有 `fail` 时非零；自动 rebuild 调用失败**或**重建后复检仍命中 IMP-023（`still_imp023`）时非零。`gateway on`：网关已起则主路径成功；review `fail` / rebuild 失败 / `still_imp023` 时非零（仅建议 rebuild、未开开关时 WARN 不导致失败）。  
7. **rebuild 后复检**：调用成功后对该实例别名入口重拉 HTML + 绝对路径空 200 对照；仍命中则 `still_imp023=True`，报告 `[WARN] rebuild 完成但 IMP-023 仍命中`（避免 base 未固化时假绿）。

### 11.2 非目标

- 不因 LAN 漂移 / 双开 / 回环失败自动 rebuild。  
- 不改应用仓库的 `vite.config` / 构建参数。  
- 本轮不挂 `lwa update` / `gateway off`。

---

## 12. 文档修订记录

| 时间 | 说明 |
|------|------|
| 2026-07-09 | 初版：G1–G5、根因 A/B/C、切换清单、探测口径 |
| 2026-07-09 | 吸纳交叉评审：新增 **§2.7**（pytest 占 :2019、pingback 假 FAIL）；强化 §4.1/§4.5/§5/§6/§9；§2.6 并入 `port_pool` 既有误报 |
| 2026-07-09 | 二次审查：新增 **§10**（落地状态 + C1/C2/I1–I5）；阻断「全部完成」宣称直至 Critical 修复 |
| 2026-07-09 | §10.5：C1/C2/I1/I2 代码修复与回归测试落地 |
| 2026-07-09 | 新增 **G6 / §4.6 / 建议 I / §11**：切换后默认 access 兼容检查 + 可选 `--rebuild-if-needed` |
| 2026-07-09 | G6 增强：rebuild 后复检 IMP-023（`still_imp023`），避免「调用成功但产物未变」假绿 |

---

*文档结束。实现上述建议项时，请同步更新 `task-list.md`、`docs/operations-playbook.md` 与相关验收用例。*
