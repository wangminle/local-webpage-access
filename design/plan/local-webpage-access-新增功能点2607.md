# 新增功能点计划 IMP-025 / IMP-026（202607）

> **状态**：2026-07-15 规划文档，尚未实施；同日完成实现前审阅并补强统计口径、IP 真相表、迁移原子性与本机地址判定。编号续接 IMP-024（见已归档的 [`local-webpage-access-imp010-021-plan-20260707.md`](../archive/local-webpage-access-imp010-021-plan-20260707.md)）。
> **范围**：管理页浏览量统计的两处改进；会补充内部 HTTP 探针标记，但不涉及运行形态识别与生命周期编排。

---

## 0. 需求

### IMP-025 — 访问次数从「资源级」改为「page 级」

当前管理页详情抽屉里的「累计访问 N 次」与「按天分布」计的是**对资源的访问次数**：每一条 HTTP 请求（`.js` / `.css` / `.png` / `.svg` / `.woff` 等静态资源请求）都被算 1 次。打开一个页面会触发几十条资源请求，导致数字严重虚高、与「浏览量」直觉不符。

**期望**：改为统计**对 page（HTML 文档）的访问次数**——一次页面打开只算 1 次（即对页面导航请求计数，不计资源文件请求）。

**口径补充**：LWA 自身为启动等待、健康检查、访问复核发起的 HTTP 探针不属于用户浏览，必须显式排除；否则 builtin 模式仍会因后台探测持续增长，无法真正达到 page 级口径。

### IMP-026 — 独立 IP 数加可点击入口，弹小面板列出全部 IP（含「本机」标记）

详情抽屉里「独立 IP N 个」当前是纯文本。**期望**：把它做成可点击链接，点击后弹出一个小面板（tips 形式），列出所有访问过的 IP 地址（带每 IP 的访问次数与最近时间）。

**补充要求（2026-07-15）**：在该 IP 列表里，凡是**本机实际持有的地址**——无论是 `127.0.0.1`、本机的局域网 IP（如 `10.181.239.115`），还是将来的 Tailscale 地址（通常落在 `100.64.0.0/10`）——都要在 IP 后标记「（本机）」。不能把整个 Tailscale CGNAT 网段都视为本机，否则其他 tailnet 节点也会被误标。

---

## 1. 现状分析

浏览量统计实现于 [`src/local_webpage_access/pageviews.py`](../../src/local_webpage_access/pageviews.py)（IMP-024 / DEV-061），数据落独立 SQLite `run/pageviews.db`，核心表：

| 表 | 字段 | 用途 |
| --- | --- | --- |
| `pageviews` | `instance_id, day, hits, unique_ips, last_seen, source` | 按天聚合的命中数与独立 IP 数 |
| `pageview_detail` | `instance_id, ts, method, path, status, remote` | 每实例最近 N 条命中明细（`_DETAIL_RETAIN=500`） |
| `pageview_ips` | `instance_id, day, remote` | 当天 IP 真相集（`INSERT OR IGNORE`，跨批去重） |
| `container_seen` | `instance_id, line_hash` | 容器日志幂等去重集；重摄入时必须同步重置 |
| `ingest_cursor` | `source_key, offset_bytes, last_ts` | 惰性摄入游标，防全量重读 |

**计数链路（IMP-025 根因所在）**：

1. 解析器把**每一条**访问日志解析成 `AccessHit`：
   - `parse_caddy_json_line`（`pageviews.py:113`）—— Caddy 统一入口 JSON access log，取 `request.method/uri/remote_ip` + `status/ts`。
   - `parse_clf_line`（`pageviews.py:84`）—— builtin `http.server` 的 CLF。
   - `parse_container_log_line`（`pageviews.py:150`）—— 容器应用 access 行（尽力解析）。
2. `record_hits`（`pageviews.py:293`）对**每一条** `AccessHit` 做 `bucket["hits"] += 1`（`pageviews.py:314`），不区分页面、资源或 LWA 自身的 GET 探针 → 这就是「资源级」且会被后台探测抬高的根源。
3. `summary()`（`pageviews.py:373`）`SUM(hits)` 得「累计访问」；`detail()`（`pageviews.py:402`）返回 `byDay[].hits` 与 `recent[]`（含 `remote`）。

**展示链路（IMP-026 触点）**：

- API：`GET /api/instances/{id}/pageviews`（`manager_api.py:720`）→ `store.detail(id, limit)`。
- 前端：`app.js:237 renderPageviewHtml` 渲染详情抽屉；`app.js:246` 渲染「独立 IP N 个」为静态 `<dd>` 文本；抽屉本体由 `app.js:729` 的 `.modal.pageview-box` 承载，内容经 `v-html` 注入（`app.js:733`），故弹出的 HTML **不经过 Vue 编译**，交互需用原生 DOM / 事件委托 / 纯 HTML（如 `<details>`）。

**IP 列表数据缺口**：`pageview_detail` 每实例只保留最近 500 条。若直接从该表 `GROUP BY remote`，只能得到“最近 500 次中的 IP”，无法兑现“全部 IP”，且较早访问的 IP 次数与最近时间都会失真。`pageview_ips` 虽保留按天去重集合，但没有每 IP 的累计次数与最后访问时间。因此需要新增独立的全量 IP 聚合真相表，而不是复用明细窗口。

**本机 IP 探测现状**：[`ports.py`](../../src/local_webpage_access/ports.py) 仅有 `detect_lan_ip()`（`ports.py:72`，UDP socket 连外部地址取**单个**出口网卡 IP）与 `resolve_lan_ip()`（`ports.py:92`），**不枚举全部本机地址**，无法识别 loopback 之外的多个本地地址或 Tailscale。另需注意 `100.64.0.0/10` 是共享网段，网段命中本身不能证明地址属于本机。

---

## 2. 关键决策

| 编号 | 决策点 | 已确认方案 |
| --- | --- | --- |
| **IMP-025.a** | 「page」判定口径 | **基于方法 + 状态码 + 请求路径**做保守启发式判定，不依赖 content-type（CLF 未记录响应 content-type）。规则见下「page 判定规则」。 |
| **IMP-025.b** | 过滤时机 | **摄入期过滤**：`record_hits` 内部把命中分为 page / 非 page，**只把 page 命中**写入 `pageviews.hits` / `pageview_ips` / `pageview_detail`。使「累计访问 / 独立 IP / 按天 / 最近命中 / IP 列表」**全部一致为 page 口径**，而非新旧并列。 |
| **IMP-025.c** | 独立 IP 是否同步改 page 口径 | **是**。独立访客理应只统计「看过页面」的 IP，而非抓过某个 CSS 的 IP。与 hits 共用同一摄入过滤天然一致。 |
| **IMP-025.d** | LWA 自身探针 | 给会命中实例 access log 的内部请求统一追加保留查询参数 `__lwa_probe=1`，page 判定显式排除。比按 loopback IP 排除更准确，因为用户本机访问也应计数。 |
| **IMP-025.e** | 历史数据 | 旧 `hits` 为资源级、已聚合入库且游标防重读 → **一次性迁移**：首次引入 `_PAGEVIEW_SCHEMA_VERSION = 1`；旧版 `user_version=0` 时在事务内重建全部派生表并清空所有游标/去重状态，触发按 page 口径全量重摄入（见 §4）。 |
| **IMP-026.a** | IP 列表数据来源 | 新增 `pageview_ip_stats(instance_id, remote, hits, last_seen)` 全量聚合真相表，随 page hit 在同一事务内 UPSERT；`pageview_detail` 仍只承担“最近命中”，不再被误用为全量统计源。 |
| **IMP-026.b** | 传给前端的方式 | **扩展现有 `detail()` 响应**，新增 `uniqueIpList: [{ip, count, lastSeen, local}]`。本工具为本地部署基座，直接返回当前可重建日志窗口内的全部 IP；按 `count DESC, lastSeen DESC, ip ASC` 稳定排序，不设置与“全部”冲突的静默上限。若未来转为公网高流量场景，再拆分页端点。 |
| **IMP-026.c** | 本机判定 | 新增 `detect_local_ips()` 一次性构造本机**实际地址集合**：loopback ∪ `detect_lan_ip()` ∪ hostname 地址 ∪ 可用时 `tailscale ip -4/-6` 输出；`is_local_ip(ip, local_ips=...)` 做规范化后精确集合匹配。不能仅因落入 `100.64.0.0/10` 就判本机。 |
| **IMP-026.d** | 弹出交互形式 | 用原生 `<details>` + `<summary>`，展开内容用 CSS 定位成小面板（而非把长列表直接撑开概要区）。**零 JS 接线、在 `v-html` 下稳定**；面板设最大高度和内部滚动，小屏降级为文档流布局。 |

### page 判定规则（IMP-025.a）

`_is_page_view(method: str, path: str, status: int) -> bool`：

1. 仅 `GET` 计数。`HEAD` 没有页面正文，通常来自探活/预检，不视为一次页面打开；`POST/PUT/…` 同样不计。
2. 仅计正常文档响应：`200–299`（排除 `204/206`）与缓存复用 `304`；重定向与错误响应不计，避免一次导航的 `3xx → 200` 被算两次。
3. 用 `urllib.parse.urlsplit` 拆出 path/query；查询参数含保留标记 `__lwa_probe=1` 时排除。HTTP 请求不会携带 fragment，代码无需处理 `#fragment`。
4. 对 URL-decoded path 的末段做小写后缀匹配，**命中资源扩展名黑名单则排除**：`.js .mjs .css .map .png .jpg .jpeg .gif .webp .avif .svg .ico .bmp .woff .woff2 .ttf .eot .otf .mp4 .webm .mov .mp3 .ogg .wav .wasm .json .xml .txt .pdf .zip .gz .webmanifest`。
5. 排除常见非页面端点：精确 `/api`、`/graphql`、`/health`、`/healthz`、`/metrics`，以及前缀 `/api/`。这是容器 access log 没有 content-type 时的必要兜底。
6. 其余视为 page：`/`（目录索引→index.html）、`/index.html`、`/about`、`/post/123`（无扩展名 SPA 路由）均计数。

> 该规则仍是启发式而非浏览器级精确 PV：CLF 没有 `Content-Type` / `Sec-Fetch-Dest`，自定义无扩展名 JSON 端点仍可能误计。采用“资源后缀黑名单 + 常见接口前缀”是兼顾静态站、SPA clean URL 与容器日志能力的折中；文档和 UI 继续保留“近似值”提示。

---

## 3. 实施拆分

### 阶段 A：IMP-025 — page 级访问次数

| 子任务 | 触点 | 说明 |
| --- | --- | --- |
| **025.01** | `pageviews.py` 新增 `_is_page_view(method, path, status)` | 纯函数（便于单测），实现 §2 判定规则；资源扩展名与非页面路由提为模块常量。 |
| **025.02** | 内部 HTTP 探针统一加 `__lwa_probe=1` | 盘点 `health.py`、`hosting.py`、`static_gateway.py`、`access.py` 中会访问实例根路径/别名路径的探针，由共享 helper 追加保留查询参数；不改 manager 自身 `/api/health`。 |
| **025.03** | `pageviews.py:293 record_hits` | 入参仍收全部 `AccessHit`；内部仅保留 `_is_page_view(h.method, h.path, h.status)` 命中，后续 `per_day` 聚合、`pageview_ips`、`pageview_detail` 与 `pageview_ip_stats` 全部基于同一 `page_hits`；空则直接返回 0。 |
| **025.04** | `pageviews.py` schema 版本 | 连接打开后、建表前读 `PRAGMA user_version`；按 §4 在单事务内完成旧派生数据重建。 |
| **025.05** | `tests/test_pageviews.py`（扩充） | 覆盖 GET/HEAD、200/304/3xx/404、大小写与编码后缀、query、`__lwa_probe=1`、常见 API 路径；混合命中断言四类统计均只含 page；验证内部探针不增长。 |
| **025.06** | 迁移回归 | 构造 `user_version=0` 且含旧表、旧游标、旧 `container_seen` 的 DB，断言全部重建并可从文件日志与容器日志重新摄入；另测未来版本 DB 不被旧代码删除。 |

### 阶段 B：IMP-026 — 独立 IP 列表弹窗 + 本机标记

| 子任务 | 触点 | 说明 |
| --- | --- | --- |
| **026.01** | `ports.py` 新增 `detect_local_ips()` / `is_local_ip()` | 使用 `ipaddress.ip_address` 规范化 IPv4、IPv6、IPv4-mapped IPv6 与 zone-id；集合来源为 loopback、`detect_lan_ip()`、`socket.getaddrinfo(hostname, AF_UNSPEC)`、可用时 `tailscale ip -4/-6`。命令缺失/超时/输出异常静默降级；一次列表请求只探测一次，不按 IP 重复执行。 |
| **026.02** | `pageviews.py` 新增 `pageview_ip_stats` | 主键 `(instance_id, remote)`；每个 page hit 在 `record_hits` 同一事务内 UPSERT `hits += 1`、`last_seen = max(old, new)`；空 remote 不入表。`clear_instance` 与 schema 重建必须覆盖该表。 |
| **026.03** | `pageviews.py:402 detail()` | 从 `pageview_ip_stats` 查询全量 IP 列表，稳定排序并一次性附 `local` 标记；响应新增 `uniqueIpList`，保持既有字段不变。接口里的 `uniqueIps` 必须与列表长度一致。 |
| **026.04** | `manager_api.py:720` | 端点签名不变（已透传 `detail()`）；仅确认 Swagger 注释补 `uniqueIpList`。无需新端点。 |
| **026.05** | `app.js:246 renderPageviewHtml` | 把静态 `<dd>` 改为 `<details class="ip-list"><summary>独立 IP N 个</summary><div class="ip-list-panel">…</div></details>`；每项显示 IP、次数、最近时间与本机徽标。所有 IP/时间文本继续经 `LWA.esc`，避免日志字段进入 `v-html` 形成注入。空列表显示「暂无」。 |
| **026.06** | `style.css` | 小面板使用绝对定位、边框/阴影、`max-height` + 内部滚动；`.ip-local` 使用语义色且不只靠颜色表达（保留「本机」文字）；键盘焦点、小屏文档流与深色模式均覆盖。 |
| **026.07** | 测试 | 用超过 `_DETAIL_RETAIN` 的命中证明 IP 次数/最近时间仍完整；覆盖跨批累加、空 remote、稳定排序、`clear_instance`。`test_is_local_ip_*` 断言本机实际 Tailscale 地址为真、同网段其他地址为假，并覆盖 loopback/LAN/IPv6/非法输入。前端测试断言转义、空态、本机徽标与小面板结构。 |

---

## 4. 数据迁移（IMP-025.e）

`pageviews.db` 为可重建数据（源 = 各 access log）。采用**自动 schema 版本迁移**，无需用户手动操作：

1. `_conn_or_open()` 打开连接后、创建业务表前，在 `BEGIN IMMEDIATE` 事务中读取 `PRAGMA user_version`。
2. `user_version=0`（当前未版本化旧库）时，以 `DROP TABLE IF EXISTS` 删除 `pageviews`、`pageview_detail`、`pageview_ips`、`container_seen`、`ingest_cursor`，再创建上述表及新增 `pageview_ip_stats`，最后设置 `PRAGMA user_version=1` 并提交。**不能漏删 `container_seen`**，否则容器旧行会被去重集拦截，重摄入后统计为空。
3. 新库同样在一个事务内建表并写版本；中途失败回滚，不能留下“游标已清但聚合表未建好”的半迁移状态。
4. 若发现 `user_version > 1`，视为新代码创建的未来版本：记录明确错误并让浏览量功能降级，**禁止旧代码直接 drop**。
5. 下次 `/api/pageviews` 或实例详情请求摄入时，游标已空 → 从 offset 0 重读当前仍保留的 `logs/static-access.log` / `apps/<id>/logs/gateway.log`，按 page 口径重新聚合。

**边界**：

- 静态 + Caddy：仅能重建 `logs/static-access.log` 当前仍保留的窗口；若日志已轮转/截断，更早历史不可恢复。
- 静态 + builtin：`apps/<id>/logs/gateway.log` 当前仍保留的窗口可重建；滚动掉的历史丢失。
- 容器：`docker logs` 仅回看 `_DOCKER_LOG_TAIL` / `--since` 可取得的当前窗口；迁移时清空 `container_seen` 后重建，仍是 best-effort。
- 因源日志保留边界不同，文档中的“全部 IP”指**当前可重建日志窗口内的全部 IP**，不是永久审计日志。

> 备选手动兜底：CLI 加 `lwa pageviews reset`（drop + 清游标），供用户强制重统计。本期可不做，自动迁移已够。

---

## 5. 验收标准

- **IMP-025**：打开任一静态实例首页一次，刷新详情抽屉，「累计访问」与「按天分布」**只 +1**（而非 +几十）；资源、API、HEAD、重定向、错误响应与带 `__lwa_probe=1` 的内部探针均不增长；`byDay`、`recent`、unique IP 与 IP 列表口径一致。
- **IMP-026**：详情抽屉「独立 IP N 个」可点击，展开小面板列出当前可重建窗口内全部 IP（含准确累计次数与最近时间）；即使命中超过 500 条也不截断聚合。`127.0.0.1`、本机 LAN IP、本机实际 Tailscale IP 显示「本机」并高亮；同一 Tailscale 网段的其他节点与外网 IP 不带标记。
- **一致性**：概要 `uniqueIps == uniqueIpList.length`；每项 `count` 之和等于所有带非空 remote 的 page hits；`lastSeen` 取该 IP 最大时间；排序确定。
- **回归**：既有浏览量列、`/api/pageviews` 汇总、删除实例清数据（`clear_instance`）行为不变；schema 迁移后旧 `pageviews.db` 自动重建，未来版本库不被破坏。
- **测试**：目标切片 `pytest -q tests/test_pageviews.py tests/test_ports.py tests/test_manager_static_app.py tests/test_manager_api.py` 全绿；`python3 -m compileall -q src`、`node --check src/local_webpage_access/manager_static/app.js`、`git diff --check` 与 task-list `check` 通过。项目未声明 `pyflakes` 依赖，不把不可重复的命令列为强制门禁。

---

## 6. 风险与边界

| 风险 | 处理 |
| --- | --- |
| 无扩展名自定义 JSON 端点被误计为 page | 默认排除常见 `/api/`、GraphQL、health、metrics；受限于 CLF 字段仍可能漏网，UI 保留“近似值”说明。若后续要求严格精度，再扩展 Caddy 字段或引入可配置排除规则。 |
| 内部探针遗漏导致后台计数 | 实现前用 `rg` 盘点所有访问实例根/别名的 `urlopen` 调用；共享 helper 追加 `__lwa_probe=1`，并做端到端回归。 |
| `<details>` 小面板被抽屉滚动容器裁切 | 面板定位以概要 `<dd>` 为 containing block，并做窄屏文档流降级；验收桌面/窄屏与键盘操作。若实际仍被裁切，再升级为事件委托控制的顶层浮层。 |
| 本机网卡枚举跨平台不全 | stdlib 探测 + `tailscale ip` 尽力覆盖；失败只影响「本机」标记，不影响统计。禁止以整个 CGNAT 网段兜底，避免更严重的误标。 |
| 全量 IP 响应未来过大 | 当前产品是本地小规模部署，优先满足“全部 IP”；若实测单实例达到千级以上独立 IP，再拆按需分页端点并在 UI 明示总数/已加载数。 |
| 全量重摄入拖慢首请求 | 只在 schema 升级发生一次，之后恢复增量；迁移测试覆盖日志轮转与容器 tail 边界，不承诺恢复已丢失历史。 |

---

## 7. 落地节奏建议

两功能耦合（`pageview_ip_stats` 必须与 IMP-025 的 page 过滤在同一写事务中），**建议同一批次实施**：先分类器与探针标记 → schema/聚合写入 → IP 判定与展示 → 迁移/端到端回归。预计改动文件：`pageviews.py`、`ports.py`、内部探针相关模块（至少 `health.py`、`hosting.py`、`static_gateway.py`、`access.py`）、`manager_api.py`（注释）、`manager_static/app.js`、`manager_static/style.css`，并扩充现有测试文件。完成后按 AGENTS.md 同步 `task-list.md`（IMP-025 / IMP-026 记 `DEV-` 完成态）。

---

## 8. IMP-027 — Docker 容器真实访客 IP 统计（经 Caddy 别名日志）

> **状态**：2026-07-15 补充。承接 §0/§1 之后对「容器 IP 统计」的核查结论。
> **背景**：IMP-025 的 page 过滤对容器**已生效**（`_is_page_view` → `record_hits` 对所有源统一），但容器走 `_ingest_container` 读 `docker logs`，存在两个 IP 维度的固有问题：(1) `parse_container_log_line` 兜底正则不抓行首 IP，`remote` 常为空 → `pageview_ip_stats` 为 0 条；(2) 即便抓到，源 IP 恒为 Docker 桥接网关（如 `192.168.65.1`），分不出真实访客。实测 prd-workflow：22 次 hits、`uniqueIpList` 为空。

### 8.1 方案

带 Caddy 别名的容器实例**改走 Caddy 共享 access log**（与静态站同口径），无别名容器才回退 `docker logs`。Caddy 路由 `handle_path /<alias>/*` 已剥前缀转发，但 access log 记录的是**原始 URI** `/prd-workflow/api/data`（含真实 client IP + query），这份现成数据此前被 `_instance_sources` 把 `docker-compose` 一律分到 container 源而忽略。

### 8.2 must-handle（落地必须处理）

| # | 要点 | 处理 |
| --- | --- | --- |
| 1 | 互斥分流，避免双计 | 容器：有别名 + caddy 后端 → 只读 caddy 日志；否则 → 只读 docker 日志。**一实例一源**。 |
| 2 | 容器别名读取 | `_instance_alias()` 当前只查 `get_static_site`；扩展为同时查 `get_container`，取 `route_mode='name'` 的 `route_host`。 |
| 3 | 分类前剥前缀（硬正确性） | `_ingest_caddy_shared` 按前缀归属后，**剥掉 `/<alias>` 前缀**再交给 `record_hits`。否则 `/prd-workflow/api/data` 不命中裸 `/api/` 规则、又无扩展名 → 被误算为 page（容器多为 API，尤其要命）。 |
| 4 | 数据源切换迁移 | `_PAGEVIEW_SCHEMA_VERSION` 升 1→2：现有迁移在单事务内 drop 全部派生表 + `ingest_cursor`（含共享 caddy 游标）→ 重建。**先清数据再重摄入，无双计**（双计只发生在「重置游标但不清数据」时，schema 机制天然规避）。 |
| 5 | 口径 | 有别名容器只统计 Caddy 别名入口流量；直连 hostPort 不在 caddy log 中，天然不计（与静态站 caddy 模式一致）。 |

### 8.3 实施拆分

| 子任务 | 触点 | 说明 |
| --- | --- | --- |
| **027.01** | `pageviews.py` `_instance_alias` | 先查 `get_static_site`（保持既有行为），未命中再查 `get_container`，取 `route_mode='name'` 的 `route_host`；异常/空返回 `None`。 |
| **027.02** | `pageviews.py` `_instance_sources` | `docker-compose` 分支：`backend=='caddy' and alias` → `caddy` 源（带 alias）；否则 → `container` 源。与静态分支共用 alias 解析。 |
| **027.03** | `pageviews.py` `_ingest_caddy_shared` | 命中前缀 `pfx` 后，构造 `path = hit.path[len(pfx):] or "/"` 的新 `AccessHit`（query 保留），再 append。剥前缀对静态与容器统一生效（也修正静态别名下 `/api/` 子路径的边角误计）。 |
| **027.04** | `pageviews.py` `_PAGEVIEW_SCHEMA_VERSION = 2` | 仅升版本号；复用 §4 的 drop-重建-重摄入迁移，无需新增迁移代码。 |
| **027.05** | `tests/test_pageviews.py` | `_instance_alias` 返回容器 route_host；`_instance_sources` 容器按别名分流；`_ingest_caddy_shared` 剥前缀后 `/alias/api/data` 不计 page、`/alias/` 计 page、query 保留（探针仍排除）；schema 1→2 迁移重摄入。 |
| **027.06** | e2e | 重启 manager 触发迁移，确认 prd-workflow 经 caddy 日志获得真实访客 IP（`uniqueIpList` 非空、含本机标记），`source` 由 container 切到 caddy。 |

### 8.4 边界与风险

- 容器别名要求 `route_mode='name'`（IMP-006/IMP-014）；`port` 模式容器无别名，仍走 docker logs（近似值，UI 保留说明）。
- Caddy 共享日志若已轮转/截断，更早历史不可恢复（与静态站同）。
- 前缀剥离改变静态实例**存储路径**（`/alias/...` → `/...`），属数据形态变化，schema 2 重摄入使其一致；计数口径不回退。
- `_ingest_caddy_shared` 前缀匹配仍按长度降序（短别名不被长别名吞）；剥前缀用实际命中的 `pfx`。

## 9. IMP-028：无别名的直连端口静态站点也统计浏览量

### 9.1 背景

demo-static 这类 `shared-static` 实例由 Caddy 独立站点块（`:{host_port}`）伺服、未挂 `:8080` 路径别名。其站点配置**没有 `log` 指令**，访问不写入共享 `static-access.log`；而浏览量摄入**只读该日志**（§8 的 `caddy-shared` 游标），故直连端口流量永不归属 → 管理页浏览量恒空。走 `:8080` 别名的实例（3d-demo / prd-workflow / voiceprint）则被 `:8080` 块的 `log` 记录，正常统计。

### 9.2 方案（用户选定的方案 b）

| # | 触点 | 说明 |
| --- | --- | --- |
| **028.01** | 站点模板 `caddy_site.conf.tpl`（src + runtime）与 `_FALLBACK_TEMPLATE` | 新增 `log { output file {access_log} { roll_size 10mb; roll_keep 3 }; format json }` 块，直连端口站点也写共享 access log。`generate_site_config` 传入 `access_log`（`ws.logs/static-access.log`）。 |
| **028.02** | `pageviews.py` `AccessHit` / `parse_caddy_json_line` | `AccessHit` 增加 `host: str = ""`；解析器提取 `request.host`（含端口）。 |
| **028.03** | `pageviews.py` `_port_from_host` / `_static_host_port` | 从 `request.host` 取端口（兼容 IPv4 `[::1]:18000`）；从 `static_sites.host_port` 取实例对外端口。 |
| **028.04** | `pageviews.py` `_instance_sources` / `ingest_all` | 无别名的 caddy 静态站点填 `host_port`；`ingest_all` 建 `port_to_id`（仅无别名站点）。 |
| **028.05** | `pageviews.py` `_ingest_caddy_shared` | 别名前缀未命中时，按 `request.host` 端口归属；有别名实例仍按前缀归属（不变）。 |
| **028.06** | 测试 | `_port_from_host`、host 捕获、按端口归属（别名/端口不串扰）3 用例；站点配置含 log 指令断言。 |

### 9.3 边界与风险

- **无双计**：同一实例要么进 `alias_to_id`（有别名）要么进 `port_to_id`（无别名），互斥；`elif` 保证不会同时进两个表。
- **别名实例的直连端口流量不计**：有别名的实例（3d-demo 等）不在 `port_to_id`，其直连端口访问日志被读但归属不到任何实例即丢弃（与既有口径一致，不改其计数）。
- 极端边角：在某实例端口上请求 `/<其他别名>/` 路径会被前缀规则误归到别名实例——属既有"按路径前缀归属"的固有特性，直连端口开启日志后理论上同样存在，但正常访问不会触发。
- schema 不变（无新表/新列），无需迁移；已运行的 `caddy-shared` 游标继续向前推进，仅统计日志开启后的新增流量。


