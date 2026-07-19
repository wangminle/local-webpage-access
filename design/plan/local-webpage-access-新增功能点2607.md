# 新增功能点计划 IMP-025～IMP-028 / IMP-030 / IMP-031～034（202607）

> **状态**：IMP-025～028 已落地（见 `task-list` DEV-068～072）；**IMP-030 跨平台自启动已落地（2026-07-16，见 `task-list` DEV-073～076，关闭 BUG-138/139）**；**IMP-031 / IMP-032 已落地（2026-07-17，DEV-074 / DEV-075）**；**IMP-033 Full Profile 权限与能力闭环主路径已落地（2026-07-19，DEV-076/078，关闭 BUG-231；033.13 实机验收与 system unit SupplementaryGroups 完整路径可后续补强）**；**IMP-034 日志可观测性补强已落地（2026-07-19，DEV-077/079）**。编号续接 IMP-024（见已归档的 [`local-webpage-access-imp010-021-plan-20260707.md`](../archive/local-webpage-access-imp010-021-plan-20260707.md)）；IMP-029 见 [`待改进功能点记录-20260706.md`](./待改进功能点记录-20260706.md)。
> **范围**：§0～§9 为管理页浏览量统计改进；§10 为 macOS / Linux（含 WSL）自启动配置与完备性检查；§11 为 Docker 国内源安装脚本；§12 为 setup/init 的 `--default` / `--full` 环境装配档位；§13 为 `--full` 下 LWA、Caddy、Docker 的统一权限契约、运行协作与可执行 WBS；§14 为日志可观测性补强（CLI/daemon 落盘、生命周期阶段事件、能力探测结构化日志）。

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

---

## 10. IMP-030 — macOS / Linux 自启动配置与完备性检查

> **状态**：2026-07-16 规划，2026-07-16 落地（DEV-073～076，关闭 BUG-138/139）。承接 CHK-048 三平台自启动评估结论；关联 BUG-138 / BUG-139 / DEV-073。
> **产品口径（已确认）**：
>
> | 平台 | LWA 运行 | 当前一键配置 | 目标（IMP-030） |
> | --- | --- | --- | --- |
> | macOS | 支持 | 部分（仅写 LaunchAgent plist） | 登录触发型自启动：**可安装 / 启用 / 检查 / 修复 / 卸载**；崩溃恢复与 PATH 完备 |
> | Ubuntu 24.04+ | 支持 | 不支持（仅文档模板，且模板有监管缺陷） | systemd user 服务一键配置 + linger；**直接监管前台进程** |
> | WSL 2.7.0+ | 支持 | 不支持、不识别 | 识别 WSL；Linux 侧同 Ubuntu；Windows 唤醒任务与网络检查给出明确指引（本期可生成脚本/检查清单，不强制改 Windows 注册表） |
>
> **不宣称**：macOS LaunchAgent ≠ 无人值守系统级服务；WSL systemd ≠ Windows 开机自动保活发行版。

### 10.1 评估结论确认（CHK-048）

以下结论经源码与文档复核，**予以确认**，作为本需求的事实基线：

1. **`--with-caddy` 非 manager 绝对必需**：`manager on` 成功后会 `maybe_start_gateway()`（`manager_service.py`）；但显式 gateway 自启仍更清晰，且当前存在 Caddy PATH 风险（BUG-139）。
2. **Compose `restart: unless-stopped`**（`compose.py`）在 Docker 引擎自启后可恢复未被显式停止的容器——**正确**；不依赖 LWA daemon 在线。
3. **现有 systemd 模板不具备崩溃恢复**（BUG-138）：`ExecStart=… daemon/manager on` 会拉起脱离子进程后迅速退出；`Restart=on-failure` 监管的是快速退出的 CLI，不是真实 watcher/uvicorn。
4. **daemon `reconcile` 只恢复 `desired=running` 的业务实例**，不复活 daemon / manager / Caddy 自身。
5. **macOS**：`lwa setup --autostart` 只生成 plist（绝对 Python + 工作区 + `RunAtLoad`），无 `KeepAlive`、无 PATH、无 enable/status/repair/uninstall；适合「用户登录后恢复」，不适合无人值守高可用。
6. **Ubuntu**：适合 `systemd --user` + `loginctl enable-linger`；但 CLI 直接拒绝 Linux，文档模板有上述监管缺陷；Python 须用固定 venv（≥3.13），不可假设 `/usr/bin/python3`。
7. **WSL**：systemd 能力满足，但发行版生命周期由 Windows 侧决定；完整链路需「Windows 登录任务 → 唤醒发行版 → systemd → LWA」。当前不探测 WSL、不生成 Windows 任务。
8. **Caddy 所有权**：Linux 上不能同时启用发行版 `caddy.service` 与 `lwa gateway on`（争用 `:2019`）。必须二选一：**由 LWA 托管**（推荐，与现有 admin API / PID / 状态一致）或 **由 systemd 托管且 LWA 只消费**（本期不选，避免双轨）。

页面恢复依赖分层（实现验收时按层检查）：

| 层 | 恢复条件 |
| --- | --- |
| Docker 实例 | Docker 引擎自启 + 容器 `unless-stopped` + bind mount 路径仍在 |
| builtin 静态 | daemon 受服务管理器监管并成功启动 → reconcile |
| Caddy 静态 / 别名 | Caddy 可执行且路径稳定；Caddyfile/站点可读；`:2019` 无冲突 |
| manager | uvicorn 前台进程受监管 |
| 网络 | IP/端口/防火墙/WSL 转发正确；IP 变更后需 `lwa access refresh` + `review` |

### 10.2 需求描述

#### 10.2.1 用户故事

1. 作为 macOS / Ubuntu 用户，我希望用一条命令完成「安装 + 启用」自启动，重启或重新登录后 daemon、manager（及按需 gateway）自动可用，管理页与业务页面可访问。
2. 作为运维/排查者，我希望用一条命令**检查自启动是否完备**（配置存在、已启用、解释器/路径有效、服务管理器状态、关键依赖、与运行态一致性），并得到可执行的修复建议或一键 `repair`。
3. 作为 WSL 用户，我希望工具识别我在 WSL 中，配置 Linux 侧自启动，并明确告知还缺哪些 Windows 侧步骤（唤醒发行版、Docker Desktop、网络）。

#### 10.2.2 功能范围

| 能力 | 必须 | 说明 |
| --- | --- | --- |
| 统一 CLI `lwa autostart …` | 是 | 见 §10.4；逐步替代 `lwa setup --autostart` 的「只写文件」语义 |
| 服务管理器**直接监管前台进程** | 是 | 修复 BUG-138；macOS / Linux 同源策略 |
| 完备性检查 `status` / `check` | 是 | 结构化报告（文本 + `--json`） |
| `install` / `enable` / `disable` / `uninstall` / `repair` | 是 | 生命周期完整 |
| 固化绝对 Python（venv）与工作区路径 | 是 | 路径移动后 `repair` 可重写 |
| 固化 Caddy 绝对路径或 `PATH`（macOS Homebrew） | 是 | 修复 BUG-139 |
| WSL 探测 + Windows 唤醒指引/脚本生成 | 是（指引）；脚本生成为强烈建议 | 不强制静默改 Windows |
| 崩溃后由服务管理器重启真实进程 | 是 | launchd KeepAlive / systemd `Restart=` 作用于**前台入口** |
| 无人登录的 macOS 系统级 LaunchDaemon | 否（本期） | 产品口径保持「登录触发」 |
| 改写 Docker Desktop「登录时启动」 | 否 | 仅检查并提示用户手动开启 |
| 同时托管发行版 `caddy.service` | 否 | 禁止双重托管 |

#### 10.2.3 非目标

- 不把自启动做成管理页 UI（CLI + Skill 优先）。
- 不保证 NAT 模式下 WSL IP 永久不变；只检查并提示 `access refresh`。
- 不降低 Docker ≥29 / Python ≥3.13 门槛；Ubuntu 24.04 官方 python3.12 / 旧 docker.io 在检查项中明确提示改用官方源或固定 venv。
- 不在本期实现完整 Windows 原生（非 WSL）任务计划一键安装（文档模板可保留；WSL 的 `.ps1`/`.bat` 生成优先）。

### 10.3 关键决策

| 编号 | 决策点 | 方案 |
| --- | --- | --- |
| **030.a** | 监管对象 | **前台入口**：`python -m local_webpage_access.daemon --workspace <abs>`、`…manager_service --workspace <abs>`、以及 LWA 持有的 Caddy 前台（现有 gateway 子进程入口或 `caddy run --config …`，须与 `gateway_service` 状态机一致）。**禁止**再把 `lwa daemon/manager/gateway on`（快速返回的 detached 启动器）作为 systemd/launchd 的主 `ExecStart`/`ProgramArguments`。 |
| **030.b** | 与 `lwa X off` 的关系 | `off` 必须先 disable 自启动单元或写入「用户显式停止」标记，再停进程，避免 KeepAlive/Restart 立刻拉回。`autostart disable` 与运行态 `off` 语义分离但可组合。 |
| **030.c** | Caddy 所有权 | **LWA 所有**：自启只拉起 LWA gateway 前台；`status/check` 若发现系统 `caddy.service` active 且争用 `:2019`，判 fail 并提示停用系统单元。 |
| **030.d** | macOS 级别 | 继续 **LaunchAgent（用户登录）**；可选后续文档说明 LaunchDaemon，但不纳入 CLI 默认路径。 |
| **030.e** | Linux 级别 | **systemd user unit** + 推荐 `loginctl enable-linger`；`check` 对未 linger 给出 warn（登出后服务会停）。 |
| **030.f** | 平台探测 | `detect_platform()` 扩展：`macos` / `linux` / `wsl` / `windows`。WSL 依据 `/proc/version`、`WSL_INTEROP`、`/run/WSL` 等启发式。 |
| **030.g** | 兼容旧 plist / 旧文档模板 | `install`/`repair` 检测旧「`… on` 启动器」配置 → 迁移为前台监管；`docs/autostart.md` 同步改写并去掉错误的崩溃恢复表述。 |
| **030.h** | `setup --autostart` | 保留为薄封装：调用 `autostart install`（或打印弃用提示指向新命令）；行为与文档对齐，避免两套生成逻辑。 |

### 10.4 CLI 开发计划

新增子命令组（建议模块 `cli/autostart.py` + 核心库 `autostart.py` 或扩展 `setup.py`）：

```text
lwa autostart install [--with-caddy] [--linger]   # 生成并可选启用
lwa autostart enable | disable
lwa autostart status | check [--json]             # check = 完备性深检
lwa autostart repair [--with-caddy]
lwa autostart uninstall [--purge-linger]          # 卸载单元；linger 默认不动
lwa autostart doctor-hints                        # 可选：只输出人工步骤（Docker Desktop / WSL 网络）
```

| 子命令 | 行为要点 |
| --- | --- |
| `install` | 探测平台；写入 LaunchAgent plist 或 `~/.config/systemd/user/lwa-*.service`；固化 `sys.executable`、workspace、`Environment=PATH=…` 或 `CaddyBinary=`；可选 `--enable` 默认 true；Linux 打印 linger 建议，`--linger` 时尝试 `loginctl enable-linger`（失败则指引）。WSL 额外写出 `windows/lwa-wsl-autostart.ps1`（或打印到 stdout）供用户在 Windows 注册登录任务。 |
| `enable`/`disable` | macOS：`launchctl bootstrap/bootout`（或 load/unload，按当前 macOS 版本选稳定 API）；Linux：`systemctl --user enable/disable --now`。 |
| `status` | 单元是否存在/enabled/loaded；对应 PID 是否为前台入口；与 `lwa daemon/manager/gateway status` 对照。 |
| `check` | **完备性清单**（见 §10.5）；任一项 fail → 非零退出码，便于脚本/CI。 |
| `repair` | 重写失效绝对路径、补 PATH/Caddy、迁移旧启动器单元、重新 enable；不擅自改 Docker Desktop 设置。 |
| `uninstall` | 停服务、删 plist/unit、`daemon-reload`；不删除工作区数据。 |

退出码约定：`0` 完备；`1` 配置/运行不完备；`2` 平台不支持或前置缺失（无工作区 / 无 Python）。

### 10.5 完备性检查项（`autostart check`）

| 类别 | 检查项 | fail / warn |
| --- | --- | --- |
| 平台 | 识别 macos/linux/wsl；WSL 时 systemd 是否可用 | systemd 不可用 → fail |
| 解释器 | 单元内 Python 绝对路径存在且 `≥3.13`、可 import `local_webpage_access` | fail |
| 工作区 | `WorkingDirectory`/`--workspace` 存在且含 `local-web.yml` | fail |
| 单元形态 | ExecStart/ProgramArguments 为**前台入口**，不是 `… on` | 旧模板 → fail（可 repair） |
| 启用态 | launchd/systemd enabled + loaded（systemd 仅认 `enabled`/`enabled-runtime`，不含 `static`） | 未启用 → fail |
| 进程 | MainPID 存活且 cmdline 含本工作区前台模块；服务状态可探测 | 单元 active 但进程死/身份不符 → fail |
| PATH | 单元 PATH 目录真实存在；含解释器目录或 `/usr/bin`/`/bin`；gateway 须能按该 PATH 解析 caddy | 无效 PATH → fail |
| Caddy | 若 `staticGateway=caddy`：二进制可执行；`:2019` 无外国进程；无系统 caddy.service 冲突 | 冲突 → fail |
| Docker | 有容器实例时：引擎可达；提示 Desktop「登录时启动」/ Engine enable | 引擎不可达 → warn/fail 视场景 |
| linger | Linux：`loginctl show-user` Linger=yes | 否 → warn |
| WSL | 发行版名；是否建议 mirrored networking；关键端口入站提示；工作区是否在 `/mnt/c`（warn） | 指引性 warn |
| 业务恢复 | `desired=running` 实例抽样：Docker 容器 Up / builtin 可探活 / Caddy 站点可 GET（带 `__lwa_probe=1`） | 抽样失败 → warn（配置完备但业务未起） |

输出格式对齐 `lwa doctor`：分项 `ok/warn/fail` + 修复命令建议；`--json` 供 Skill 消费。

### 10.6 Skill 开发计划

| Skill | 动作 | 内容 |
| --- | --- | --- |
| **新建 `lwa-setup-autostart`** | Create | 触发：用户要开机自启、登录后页面没起来、问「怎么设置自启动」。流程：读 `lwa autostart check --json` → 按平台给出最小命令序列 → **允许**指导用户执行 `lwa autostart install/enable/repair`（写用户级单元，非 sudo 改系统）；WSL 时分「Linux 侧 / Windows 侧」两段清单；明确禁止同时启用系统 `caddy.service`；崩溃恢复与 Docker Desktop 登录启动的边界说明。禁止事项：不代替用户改 Windows 任务计划（除非用户明确要求并已提供管理员权限语境）；不宣称无人值守 macOS。 |
| **`lwa-setup-host-environment`** | Modify | 「开机自启」一节改为指向 `lwa autostart` + 新 Skill；删除「Linux 仅文档模板」的过时表述。 |
| **`lwa-diagnose-health-check` / doctor 相关** | Modify | 若 `autostart check` 有 fail，在排障路径中建议先修自启；避免只修实例不修 OS 服务。 |
| **（可选）`lwa-repair-autostart`** | Create 或并入上者 | 若 `setup-autostart` 过长，可拆「仅修复」Skill：只消费 `check --json` 的 fail 项跑 `repair` 并复核。 |

Skill 输出必须包含：**产品口径一句**（登录触发 vs 系统服务 vs WSL 需 Windows 唤醒），避免用户误解 SLA。

### 10.7 实施拆分

#### 阶段 A — 根基：前台监管 + 平台探测（修复 BUG-138 心智）

| 子任务 | 触点 | 说明 |
| --- | --- | --- |
| **030.01** | `setup.py` / 新 `platform_detect.py` | `detect_platform` 区分 `wsl`；单测 mock `/proc/version` 等。 |
| **030.02** | `autostart.py`（新） | 抽象 `AutostartBackend`：`MacLaunchdBackend` / `SystemdUserBackend`；生成前台 `ProgramArguments`/`ExecStart`；单元名 `com.fenix.lwa.*` / `lwa-daemon.service` 等保持稳定。 |
| **030.03** | daemon / manager_service / gateway | 确认前台模块入口适合 Type=simple；必要时补 `--foreground` 文档化约定；保证信号可优雅退出以便 Restart 干净。 |
| **030.04** | `docs/autostart.md` | 重写：删除「on + Restart=on-failure 即崩溃恢复」；改为前台监管示例；标明 Caddy 所有权。 |

#### 阶段 B — macOS 完备化（修复 BUG-139）

| 子任务 | 触点 | 说明 |
| --- | --- | --- |
| **030.05** | plist 生成 | `EnvironmentVariables.PATH` 含 `/opt/homebrew/bin:/usr/local/bin:…`；若 `which caddy` 成功则写入专用键或 gateway 参数绝对路径。 |
| **030.06** | KeepAlive / ThrottleInterval | 对**前台** daemon/manager/gateway 启用 KeepAlive（或等价）；与 `autostart disable` + `lwa X off` 联调，防止「off 被拉回」。 |
| **030.07** | CLI `autostart *` macOS 路径 | install/enable/status/check/repair/uninstall 全流程；`setup --autostart` 委托。 |
| **030.08** | 测试 | plist 含 PATH/绝对 caddy；拒绝旧 `on` 启动器；check 对缺失 PATH 报 fail。 |

#### 阶段 C — Linux / Ubuntu 一键自启

| 子任务 | 触点 | 说明 |
| --- | --- | --- |
| **030.09** | systemd user unit 模板 | `Type=simple`、`Restart=on-failure`、`WorkingDirectory`、`ExecStart` 前台；`After=network-online.target`；manager `After=lwa-daemon.service`（可选）。 |
| **030.10** | linger 处理 | install 提示；`--linger` 调用；check 读 Linger 状态。 |
| **030.11** | CLI Linux 路径 | 不再对 Linux `raise`「仅 macOS」；全量子命令可用。 |
| **030.12** | 测试 | unit 文件内容断言；旧模板检测；假 `systemctl` runner 测 enable/disable 调用序列。 |

#### 阶段 D — WSL 与完备性深检 + Skill

| 子任务 | 触点 | 说明 |
| --- | --- | --- |
| **030.13** | WSL 附加产物 | 生成 Windows 侧唤醒示例脚本 + 文档段落（`wsl.exe -d <distro> -- …` 或 `wsl.exe -d <distro>` 空启动）；check 列出 Windows 待办。 |
| **030.14** | `autostart check` 全表 | 实现 §10.5；与 `lwa doctor` 字段风格一致；可被 doctor 摘要引用（可选一行「自启动：ok/warn/fail」）。 |
| **030.15** | Skill | 新建 `lwa-setup-autostart`；更新 `lwa-setup-host-environment`；按需更新诊断类 Skill。 |
| **030.16** | 回归门禁 | 单测覆盖生成物与 check 矩阵；**不**把真实重启/登录列入 CI；手工验收清单写入 `docs/autostart.md`（登录一次 / `systemctl --user` 杀进程看 Restart / WSL 从 Windows 唤醒）。 |

### 10.8 验收标准

- macOS：`lwa autostart install --with-caddy && enable` 后，用户重新登录（或模拟 bootout/bootstrap）→ daemon/manager/gateway 前台进程在跑；拔掉 PATH 中的 caddy 仅留绝对路径仍能起 gateway；`check` 全绿。
- Ubuntu：同一 CLI 在 Linux 成功；`systemctl --user kill` 后进程被 Restart 拉起；`lwa manager off` 前先 `autostart disable`（或 off 自动协调）不会被立刻拉回。
- 旧配置：曾按旧文档安装的 `… on` 单元，`check` 报 fail，`repair` 后变为前台监管。
- WSL：`detect_platform()=="wsl"`；`check` 输出 Windows 唤醒与网络待办；Linux 侧单元行为与 Ubuntu 一致。
- 文档与 Skill：不再声称「当前 systemd 模板具备崩溃恢复」；产品口径与 §10 表一致。
- 测试：`pytest` 覆盖生成与 check；全量回归绿；task-list 关闭 BUG-138/139，DEV-073 按阶段拆分为完成态子项（或本号收口）。

### 10.9 风险与边界

| 风险 | 处理 |
| --- | --- |
| KeepAlive 与用户 `off` 冲突 | disable 自启与 stop 进程的顺序写进 CLI；单测模拟 |
| Caddy 双重托管 | check 硬失败；Skill 明确禁止 |
| Ubuntu 自带 Python 3.12 | check 校验单元内解释器版本；install 使用 `sys.executable`（须为 3.13+ venv） |
| Docker Desktop 默认不登录启动 | check warn + 文档截图级说明（文字） |
| WSL 网络/IP 变化 | check warn + 指向 `lwa access refresh` |
| 真实重启无法进 CI | 文档手工验收清单；单测保证生成物正确 |

### 10.10 落地节奏与编号映射

建议顺序：**A（前台监管根基）→ B（macOS 修 PATH + CLI）→ C（Linux systemd）→ D（WSL + check 深检 + Skill）**。

| task-list | 关系 |
| --- | --- |
| CHK-048 | 评估已完成，本计划输入 |
| BUG-138 | 阶段 A/C 修复 |
| BUG-139 | 阶段 B 修复 |
| DEV-073 | 本 IMP 主开发项；可按阶段拆 DEV-074+ 或在备注中勾选阶段 |

预计主要触点：`setup.py`、新 `autostart.py`、`cli/autostart.py`（或 `cli/system.py` 扩展）、`docs/autostart.md`、`skills/lwa-setup-autostart/SKILL.md`、`skills/lwa-setup-host-environment/SKILL.md`、`tests/test_setup.py` / 新 `tests/test_autostart.py`。

---

## 11. IMP-031 — setup / init 内置 Docker Engine + Compose 国内源安装脚本

> **状态**：2026-07-17 规划，**已落地（DEV-074）**。
> **背景**：`lwa setup` 当前仅给出官方文档链接与参考脚本片段（`setup.py` 的 `_docker_install_hint` / `_SCRIPT_MACOS` / `_SCRIPT_LINUX`），国内环境拉官方源常超时；`lwa init` 只初始化工作区，不探测 Docker，用户常到 `import` / `start` 容器实例时才发现引擎未装。
> **目标**：在包内提供 **macOS / Linux 两套**可执行安装脚本（Docker Engine + Compose 插件），默认配置 **阿里云国内源**；在 `setup` / `init` 流程中检测本地是否已有 Docker Engine，若缺失则**主动询问**用户是否执行内置脚本协助安装。

### 11.1 需求描述

#### 11.1.1 用户故事

1. 作为国内 macOS / Linux 用户，我希望在 `lwa setup` 或 `lwa init` 时，若本机没有 Docker Engine，工具能明确告知并**询问是否用内置脚本安装**，而不是只甩官方外网链接。
2. 作为运维/开发者，我希望脚本分开维护（macOS 一份、Linux 一份），安装 Engine 与 Compose 插件，并预先配好阿里云 apt/yum（或等价）与镜像加速，减少手动改源。
3. 作为已安装 Docker 的用户，我希望流程**静默跳过**，不被反复打扰。

#### 11.1.2 功能范围

| 能力 | 必须 | 说明 |
| --- | --- | --- |
| 内置安装脚本 ×2 | 是 | `install-docker-macos.sh`、`install-docker-linux.sh`（路径建议见 §11.3） |
| 安装 Docker Engine | 是 | 满足 `MIN_DOCKER_VERSION`（当前 ≥ 29.0.0） |
| 安装 Compose 插件 | 是 | `docker compose` 插件，满足 `MIN_COMPOSE_VERSION`（推荐线仍提示） |
| 配置阿里云国内源 | 是 | 包仓库源 + daemon `registry-mirrors`（见 §11.2） |
| setup / init 检测 Engine | 是 | 无 Engine（或 `docker` 不可用）时进入询问分支 |
| 交互询问是否执行脚本 | 是 | 默认否（保守）；用户确认后才执行；非 TTY / `--yes`/`--no` 有明确行为 |
| Windows 原生一键安装脚本 | 否（本期） | 仍指向 Docker Desktop 文档；WSL 走 Linux 脚本 |
| 静默改 Docker Desktop「登录时启动」 | 否 | 与 IMP-030 一致，仅提示 |

#### 11.1.3 非目标

- 不替代 Docker Desktop 的 GUI 许可与首次启动向导（macOS 装完仍可能需用户打开一次 Desktop）。
- 不保证覆盖全部 Linux 发行版；本期以 **Debian/Ubuntu（含 WSL Ubuntu）** 为主路径，其他发行版脚本内明确报错或给出手动指引。
- 不在非交互 CI 中默认自动装 Docker（无 TTY 且未传 `--install-docker` 时跳过询问并打印脚本路径）。
- 不把镜像加速地址写成不可配置硬编码唯一值；允许环境变量 / 脚本参数覆盖，默认值为阿里云文档推荐地址。

### 11.2 关键决策

| 编号 | 决策点 | 方案 |
| --- | --- | --- |
| **031.a** | 脚本形态 | **仓库内独立 `.sh` 文件**（随包分发 / `importlib.resources` 定位），非仅 `setup.py` 字符串常量；`lwa setup --script` 可打印路径或 cat 内容。 |
| **031.b** | macOS 安装方式 | 优先 **Docker Desktop**（Homebrew cask 或官方 pkg）；脚本内配置 `~/.docker/daemon.json`（或 Desktop settings 等价路径）的 `registry-mirrors` 为阿里云加速；Compose 随 Desktop 捆绑，脚本结束用 `docker compose version` 校验。 |
| **031.c** | Linux 安装方式 | 使用 **阿里云 Docker CE 镜像站** 配置 apt/yum 源，安装 `docker-ce`、`docker-ce-cli`、`containerd.io`、`docker-compose-plugin`；写 `/etc/docker/daemon.json` 的 `registry-mirrors`；`usermod -aG docker` 并提示重新登录。 |
| **031.d** | 阿里云源范围 | **双源**：(1) **软件包源**（docker-ce 安装包）；(2) **镜像拉取加速**（`registry-mirrors`）。具体 URL 以阿里云当前文档为准，脚本顶部常量集中维护，便于失效时一处替换。 |
| **031.e** | 触发时机 | **`lwa setup` 与 `lwa init` 均检测**。`setup`：检测报告后若 docker fail 则询问；`init`：工作区初始化成功后、打印「下一步」之前询问。已安装且 `docker version` 可达则跳过。 |
| **031.f** | 询问交互 | 文案示例：「未检测到 Docker Engine。是否执行内置安装脚本（阿里云源，macOS/Linux）？[y/N]」。确认后：选平台脚本 → 打印将执行命令 → 再确认一次（或 `--yes` 一次过）→ `subprocess` 调用 bash。拒绝则打印脚本路径与 `bash <path>` 手动命令。 |
| **031.g** | 权限 | Linux 脚本内需要 `sudo`；macOS Desktop 安装可能需管理员密码。LWA **不**自己提权，由脚本内 `sudo` 交互。 |
| **031.h** | 与现有参考脚本关系 | 现有 `_SCRIPT_*` 中 Docker 段改为「调用 / 指向」新脚本；安装 hint 文案同步改为内置脚本优先、官方文档兜底。 |
| **031.i** | CLI 开关 | 细粒度开关（`--install-docker` 等）可保留作调试；**产品主入口以 IMP-032 的 `--default` / `--full` 为准**（见 §12）。无 profile 时：TTY 对缺失 Docker 询问，非 TTY 跳过。 |

### 11.3 脚本与代码触点

| 产物 / 触点 | 说明 |
| --- | --- |
| `scripts/install-docker-macos.sh`（或 `src/local_webpage_access/scripts/…`） | macOS：检测已装则 exit 0；装 Desktop；写 registry-mirrors；校验 `docker` + `docker compose` |
| `scripts/install-docker-linux.sh` | Linux：发行版探测；配阿里云 docker-ce 源；装 Engine + compose-plugin；daemon.json；用户组；校验版本 ≥ 门槛 |
| `setup.py` / 新 `docker_install.py` | 探测、定位脚本路径、询问、执行、更新 hint/`render_setup_script` |
| `cli/__init__.py` `init`、`cli/system.py` `setup` | 接线探测与 flags |
| `docs/` / `lwa-setup-host-environment` Skill | 文档化国内源安装路径与「拒绝后如何手动跑」 |
| `tests/test_setup.py` 等 | mock 无 docker → 询问分支；`--no-install-docker` 不执行；脚本内容含阿里云关键字与 compose 包名断言 |

### 11.4 实施拆分

| 子任务 | 触点 | 说明 |
| --- | --- | --- |
| **031.01** | 两份 shell 脚本 | 实现 macOS / Linux 安装 + 阿里云双源；`set -euo pipefail`；幂等（已装且版本够则跳过安装步骤仍可补配 mirror） |
| **031.02** | 脚本打包与定位 | 确保 `pip install` / editable 后 CLI 能找到脚本；失败时给出仓库相对路径兜底 |
| **031.03** | `detect_docker_engine()` | 复用或薄封装 `check_docker`：区分「未安装」「已装但 daemon 未起」「版本过低」——仅「未安装 / 命令不存在」默认进入安装询问；daemon 未起提示启动而非重装；版本过低提示升级路径 |
| **031.04** | setup / init 询问流 | 实现 §11.2.e/f/i；TTY/`--yes`/`--no`/`--install-docker` 矩阵 |
| **031.05** | 文案与 `--script` | hint 与参考脚本指向内置安装器；执行前后打印校验命令 |
| **031.06** | Skill / README | 更新宿主机环境 setup 说明；强调国内源与手动执行方式 |
| **031.07** | 测试 | 单元：探测分支、flag 短路、脚本存在性与关键片段；不在 CI 真实安装 Docker |

### 11.5 验收标准

- 干净 macOS / Ubuntu（无 docker）：`lwa init`（或 `setup`）提示询问；选 N → 打印脚本路径且不改系统；选 Y（或 `--install-docker`）→ 跑对应平台脚本后 `docker version` 与 `docker compose version` 可用且满足最低版本。
- 已安装 Docker：setup/init **不询问**安装。
- 脚本默认使用阿里云包源与 `registry-mirrors`；可用参数/环境变量覆盖。
- 非 TTY 默认不自动安装；`--install-docker` 可显式触发。
- 现有 `lwa setup` 检测项与 doctor 的 Docker 检查不被破坏；全量相关单测绿。

### 11.6 风险与边界

| 风险 | 处理 |
| --- | --- |
| 阿里云镜像站 URL / 加速器策略变更 | 脚本顶部常量 + 文档注明核对官方镜像站页；失效时 hint 回退官方文档 |
| macOS 仅装 CLI 无 Desktop 体验差 | 本期明确走 Desktop；不承诺 colima/rancher 等替代运行时（可后续扩展） |
| Linux 非 Debian 系 | 脚本检测后友好退出并给手动指引，不半装损坏源列表 |
| sudo / 图形密码打断自动化 | 文档说明需交互终端；CI 用 `--no-install-docker` |
| 装完 daemon 未启动误判失败 | 脚本末尾 `docker info` 重试/提示启动 Desktop 或 `systemctl start docker` |
| 与用户已有 `daemon.json` 冲突 | 合并写入 `registry-mirrors`，不粗暴覆盖整个文件；无法解析则备份后写入并告警 |

### 11.7 落地节奏与编号映射

建议顺序：**031.01 脚本 → 031.02 打包定位 → 031.03/031.04 CLI 询问 → 031.05/031.06 文案 Skill → 031.07 测试**。

| task-list | 关系 |
| --- | --- |
| PLN-012 / DOC-038 | 本 §11 规划与文档记录 |
| DEV-074 | 本 IMP 主开发项，落地时按子任务拆分或本号收口 |
| 关联 | 增强现有 `setup` 安装 hint；与 IMP-030 的 Docker Desktop「登录时启动」检查互补（本项负责「装上」，030 负责「自启提示」） |

预计主要触点：`scripts/install-docker-*.sh`、`setup.py`（或新 `docker_install.py`）、`cli/__init__.py`、`cli/system.py`、`skills/lwa-setup-host-environment/SKILL.md`、相关 docs 与测试。

---

## 12. IMP-032 — setup / init 环境装配档位：`--default` 与 `--full`

> **状态**：2026-07-17 规划，**已落地（DEV-075）**。与 IMP-031（Docker 安装脚本）配套；本项定义 **CLI 档位语义** 与 **full 路径下 Caddy + Docker Engine + Compose 的检查/安装闭环**。
> **背景**：当前 `lwa setup` 只检测并打印指引，`lwa init` 只建工作区；缺 Docker / Caddy 时用户要自行翻文档安装。国内环境还需通用、可内置的下载安装脚本。用户希望用两个明确档位区分「维持现状」与「一次装齐容器托管全套前置」。
> **目标**：`lwa setup` 与 `lwa init` 均支持 `--default` / `--full`；**default = 当前行为**；**full = 按最低版本要求检查并装齐 Caddy、Docker Engine、Compose**（内置最通用的分平台下载/安装脚本）。

### 12.1 需求描述

#### 12.1.1 用户故事

1. 作为新用户，我执行 `lwa init --default`（或不带参数，等价 default）时，行为与今天一致：初始化工作区 / 检测环境并给指引，不强制装 Caddy/Docker。
2. 作为要跑容器实例 + Caddy 别名的用户，我执行 `lwa setup --full` 或 `lwa init --full`，希望工具**检查** Caddy / Docker Engine / Compose 是否达到 `MIN_*` 门槛；任一缺失或过低则用**内置通用脚本**装好（国内源优先，见 IMP-031），而不是只给外链。
3. 作为 CI / 脚本调用方，我希望档位语义稳定、可非交互（`--full` 在无 TTY 时需显式确认策略，见 §12.2）。

#### 12.1.2 档位定义

| 档位 | CLI | 行为摘要 |
| --- | --- | --- |
| **default** | `--default`（**缺省即此档**） | **等同当前** `setup` / `init`：检测 Python、lwa、Docker、Compose、Caddy、Node 并输出报告/hint；Caddy 缺失可降级 builtin；Docker 缺失不阻断工作区初始化。可叠加 IMP-031：缺失 Docker 时 **TTY 询问**是否装，默认不自动装。 |
| **full** | `--full` | 在 default 检测基础上，将 **Caddy、Docker Engine、Docker Compose** 视为本阶段**必须达标**项：对照 `MIN_CADDY_VERSION` / `MIN_DOCKER_VERSION` / `MIN_COMPOSE_VERSION`；未安装或低于最低要求 → **执行内置安装脚本装齐**（装后复检）。Python / Node / lwa 包仍按现有规则检测（本期 full **不**自动装 Python/Node，除非后续单列）。 |

互斥：`--default` 与 `--full` 不能同时传；同时传则 CLI 报错退出。

#### 12.1.3 功能范围

| 能力 | default | full | 说明 |
| --- | --- | --- | --- |
| 工作区初始化（仅 `init`） | 是 | 是 | 目录 / 配置 / registry 逻辑不变 |
| 环境检测报告 | 是 | 是 | 复用 `run_setup` / doctor 同源检查 |
| Caddy 低于门槛 → 自动安装 | 否 | **是** | 内置 macOS/Linux 通用安装脚本 |
| Docker Engine 低于门槛 → 自动安装 | 否（可询问，IMP-031） | **是** | 复用 / 扩展 IMP-031 脚本 |
| Compose 低于门槛 → 自动安装 | 否（可询问） | **是** | 通常随 Engine/Desktop；Linux 显式装 plugin |
| 阿里云等国内源 | 询问安装时用 | 安装脚本默认用 | 与 IMP-031 一致 |
| 自动装 Python / Node | 否 | 否（本期） | 仍只给 hint |
| Windows 原生 full 一键装 | 否 | 否（本期） | WSL 走 Linux 脚本；Win 原生给 Desktop 指引 |

#### 12.1.4 非目标

- full 不保证装完即可无人值守跑业务（macOS 仍可能要手动开一次 Docker Desktop；Linux 可能要重新登录以生效 `docker` 组）。
- full 不把 `staticGateway` 强制改成 caddy 写回配置以外的隐式行为；建议 full 成功后提示「已具备 Caddy，可将 `staticGateway` 设为 caddy」或在 `init --full` 时默认生成 `staticGateway: caddy`（**决策见 032.d**）。
- 不在 full 中静默升级「已达标但低于推荐线」的 Compose（推荐线仍 WARN，与 BUG-051 口径一致）。

### 12.2 关键决策

| 编号 | 决策点 | 方案 |
| --- | --- | --- |
| **032.a** | 缺省档位 | **default**。不传 `--default`/`--full` 时行为与现网完全一致，避免破坏脚本与文档。 |
| **032.b** | full 安装触发条件 | 对 Caddy / Docker / Compose 三项：`command` 缺失 **或** 已装但 `version < MIN_*` → 进入安装；daemon 未起但二进制达标 → **不重装**，提示启动。 |
| **032.c** | full 交互 | TTY：列出将安装组件 → 一次确认 `[y/N]`（可用 `--yes` 跳过）；非 TTY：**必须** `--yes` 才执行安装，否则打印脚本路径并以非零退出（避免 CI 静默改机器）。 |
| **032.d** | `init --full` 与 `staticGateway` | **推荐**：`init --full` 在生成 `local-web.yml` 时默认 `staticGateway: caddy`（因已承诺装 Caddy）；`init --default` 保持现有默认。若用户显式 `--static-gateway builtin` 则尊重。 |
| **032.e** | 脚本集合 | 在 IMP-031 Docker 脚本之外，增加 **Caddy** 最通用安装脚本（macOS：`brew install caddy` 或官方二进制；Linux：官方 apt 仓库或静态二进制 + 校验 `MIN_CADDY_VERSION`）。尽量少分支、可幂等。 |
| **032.f** | 与 IMP-031 关系 | IMP-031 = 安装器实现；IMP-032 = **档位编排**。`--full` 调用同一套脚本且对缺失项**默认执行**（经确认），不再逐项反复问「是否装 Docker」。 |
| **032.g** | 退出码 | default：与现网一致（必需项未就绪 → 1）。full：安装或复检仍未达标 → 1；用户拒绝确认 → 1；成功且三项均 ≥ MIN → 0。 |
| **032.h** | `setup --script` | 可增加 `--full` 时输出「完整装配」脚本（串联 Caddy+Docker+Compose）；或分别打印各脚本路径。 |

### 12.3 CLI 形态（示意）

```text
lwa setup [--default | --full] [--yes] [--script] ...
lwa init  [--default | --full] [--yes] [--force] [--workspace ...]
```

| 命令示例 | 预期 |
| --- | --- |
| `lwa setup` / `lwa setup --default` | 仅检测 + hint（+ IMP-031 询问 Docker） |
| `lwa setup --full` | 检测 → 确认 → 装缺失的 Caddy/Docker/Compose → 复检 |
| `lwa init --full --yes` | 建工作区 + 非交互装齐三项（需 root/sudo 场景仍可能停在密码提示） |
| `lwa init --default` | 仅建工作区；可选询问 Docker |

### 12.4 实施拆分

| 子任务 | 触点 | 说明 |
| --- | --- | --- |
| **032.01** | `cli/system.py`、`cli/__init__.py` | 增加互斥的 `--default` / `--full`；`--yes`；接线编排函数 |
| **032.02** | `setup.py` 或 `host_bootstrap.py` | `BootstrapProfile = default \| full`；full 路径：检查 → 计划 → 确认 → 调脚本 → 复检 |
| **032.03** | Caddy 安装脚本 ×2 | macOS / Linux 最通用路径；版本校验；国内可访问的下载优先（文档注明） |
| **032.04** | 对接 IMP-031 | full 复用 Docker/Compose 脚本；统一日志与失败回滚文案（失败不留下半配置源为佳） |
| **032.05** | `init` + `staticGateway` | 实现 032.d；单测覆盖 default/full 生成配置差异 |
| **032.06** | 文档 / Skill | README、`lwa-setup-host-environment`：两档对比表；强调 full 权限与 Desktop 首次启动 |
| **032.07** | 测试 | profile 互斥、default 不调安装器、full+yes mock 调用序列、复检失败退出码；不在 CI 真装 |

### 12.5 验收标准

- `lwa setup` / `lwa init` 不传参与显式 `--default` 行为与改前一致（含退出码）。
- `lwa setup --full`（TTY 确认或 `--yes`）后：`caddy version`、`docker version`、`docker compose version` 均 ≥ 对应 `MIN_*`；已达标组件不被无故重装。
- `lwa init --full` 工作区可用且（按 032.d）默认倾向 caddy；`--default` 不强制改网关。
- `--default --full` 同时出现 → 清晰错误、非零退出。
- 非 TTY 的 `--full` 无 `--yes` → 不改装系统，给出可执行脚本路径。
- 相关单测绿；task-list 落地记 DEV- 完成态。

### 12.6 风险与边界

| 风险 | 处理 |
| --- | --- |
| full 权限/交互打断 | 文档写明需管理员密码；CI 用 default 或预装镜像 |
| 装齐后 Desktop 未启动 | 复检区分「二进制 OK / daemon 不可达」，提示启动而非报「安装失败」 |
| Caddy 与系统 `caddy.service` 冲突 | 安装后提示与 IMP-030 一致：由 LWA 托管时勿并行启用系统单元 |
| 脚本「最通用」仍覆盖不全 | 非目标发行版失败时打印手动步骤，不假装成功 |
| 与旧 `--install-docker` 并存 | 文档标明 deprecated 或映射到 full 子集；避免三套语义 |

### 12.7 落地节奏与编号映射

建议：**先 IMP-031 脚本可跑 → 再 IMP-032 档位编排 + Caddy 脚本 → 文档/Skill**。亦可同一迭代交付，但测试上先单测安装器再测 profile。

| task-list | 关系 |
| --- | --- |
| PLN-013 / DOC-039 | 本 §12 规划与文档记录 |
| DEV-075 | 本 IMP 主开发项（依赖 / 并行 DEV-074） |
| DEV-074（IMP-031） | Docker/Compose 安装器；被 `--full` 调用 |

预计主要触点：`cli/system.py`、`cli/__init__.py`、`setup.py`（或 `host_bootstrap.py`）、`scripts/install-caddy-*.sh`、`scripts/install-docker-*.sh`、Skill/README、测试。


## 13. IMP-033 — Full Profile 权限契约与 LWA / Caddy / Docker 能力闭环

> **提出背景（2026-07-18）**：Ubuntu 实机通过 `sg docker` 执行 `lwa start` 后，容器实际运行，但 systemd user 启动的 manager / daemon 未继承 `docker` 组权限；后台 `docker compose ps` 访问 Docker socket 失败，LWA 又把“无权观测”误判并回写为 `stopped`，造成 CLI、管理页、Docker 实际状态互相矛盾。
>
> **与 IMP-032 的关系**：IMP-032 已完成“安装 Caddy / Docker / Compose 并检查版本”的第一阶段；IMP-033 将 `--full` 从“组件安装档”提升为“完整运行能力契约”。本节口径覆盖 §12.1.4 中“full 不保证装完即可无人值守跑业务”以及仅提示重新登录的旧边界：今后 `full` 未完成运行身份、权限继承、后台能力和重启验收时，必须判定为 `unready`，不得宣称安装成功。
>
> **当前状态（2026-07-19）**：BUG-230 止血之后，IMP-033 **主路径已落地**（CapabilityReport、观测 unknown、Caddy owner fail-closed、`setup --full --resume`、doctor/capabilities、管理页 degraded；见 DEV-076/078）。**可后续补强**：033.13 Ubuntu 实机验收全链路、system unit `SupplementaryGroups=docker` 完整路径。不得把「仅组件安装成功」写成 Full ready。

### 13.1 问题分析

#### 13.1.1 已复现故障链

1. Docker 安装脚本执行 `usermod -aG docker <user>`，但已经运行的 shell、`systemd --user` 和其子进程不会自动刷新 supplementary groups。
2. 用户用 `sg docker` 或 `newgrp docker` 启动 CLI，只让该临时子进程获得 Docker 权限；既有 manager / daemon 仍使用旧权限上下文。
3. CLI 能成功执行 `docker compose build/up`，manager / daemon 却无法访问 `/var/run/docker.sock`。
4. `DockerRuntime.status()` 把 `docker compose ps` 的任何非零结果压缩为 `None`，没有区分“容器不存在”和“权限不足 / daemon 不可达 / 超时”。
5. `_observe_container_status()` 再把 `None` 或异常统一解释为 `STOPPED`，覆盖最后一次可信运行态；管理页因此显示已停止，即使 Docker 中容器仍在运行。
6. 当前 setup / doctor 多数检查发生在 CLI 当前进程，只能证明“当前终端可用”，不能证明 manager / daemon / gateway 的真实运行上下文可用。

#### 13.1.2 根因归类

| 类别 | 根因 | 后果 |
| --- | --- | --- |
| 档位语义 | `--full` 只保证组件安装与版本，不保证整体可运行 | 安装结果假绿 |
| 身份分裂 | CLI、manager、daemon 可能由不同权限上下文启动 | 同一 LWA 对 Docker 的结论不一致 |
| 权限刷新 | `docker` 组变更未传播到已有会话 / user manager | 后台永久 permission denied |
| 状态建模 | “观测失败”与“确认 stopped”没有区分 | 实际运行容器被错误回写 stopped |
| Caddy 策略 | 显式 `staticGateway: caddy` 仍可能降级 builtin | full 对外入口能力不确定 |
| 验收缺口 | 缺少后台进程自检、重启后检查和最小真实闭环 | 当前会话可用但重启后失效 |

#### 13.1.3 为何 macOS 不易复现、Ubuntu 易踩中

本故障**不是**「LWA 在 macOS 上更正确」，而是平台 Docker 权限模型不同：

| 平台 | Docker 形态 | 权限特点 | 与本次故障关系 |
| --- | --- | --- | --- |
| **macOS** | Docker Desktop，用户态 socket（如 `~/.docker/run/docker.sock`） | 登录用户天然可访问；一般无 `usermod -aG docker` + 重登刷新组 | CLI / launchd 拉起的 manager/daemon 通常同属已登录用户会话，**很少出现「CLI 有权、后台无权」** |
| **Ubuntu / Linux** | Docker Engine，系统 socket `/var/run/docker.sock`（`root:docker`） | 须加入 `docker` 组；组变更**只对之后新建登录会话**生效 | 中途加组 + CLI 用 `sg docker` 临时提权，而 systemd `--user` 仍持旧组 → **正是本次复现路径** |
| **WSL** | 常对接 Docker Desktop 或 WSL 内 Engine | Desktop 路径接近 macOS；Engine 路径接近 Ubuntu | 验收须标明实际后端，勿用「WSL 一次绿」覆盖两种模型 |

设计含义：

1. **macOS 验收不能代替 Ubuntu Full 权限闭环**：Desktop「能跑」只证明用户态 socket 路径，不证明 Linux `docker` 组 + 后台进程继承已修好。
2. **IMP-033 主验收机以 Ubuntu（含可选 WSL-Engine）为准**；macOS 侧重 Desktop 启动慢、二进制/daemon 区分（见 §13.10）。
3. 文档与 Skill 须写清：用户在 Mac 上「从没遇到权限问题」**不能**作为 Full Profile 已完成的证据。

#### 13.1.4 对 IMP-032 旧边界的覆盖（口径废止）

§12.1.4 / §12.6 中下列表述在 **IMP-033 落地后对 Full Profile 不再成立**（Default Profile 仍可保留宽松语义）：

| IMP-032 旧表述 | IMP-033 新口径 |
| --- | --- |
| full 不保证装完即可无人值守跑业务（Linux 可能要重登才生效 docker 组） | Full **必须**把「重登 / resume / 后台复检」纳入安装状态机；未闭环 → `session_refresh_required` 或 `unready`，**不得** exit 0 假绿 |
| full 成功后仅「提示」可将 `staticGateway` 设为 caddy | `init --full` 默认 caddy（032.d）且运行期 **禁止** caddy→builtin 静默降级（§13.3.3） |
| doctor / setup 在 CLI 进程检查 Docker 即可 | 必须分别验证 **CLI / manager / daemon** 真实上下文（§13.5、§13.7） |

§12 作为「装组件」史仍保留；实现与文档引用 Full 语义时以 **本节为准**。

#### 13.1.5 Ubuntu 新复现：系统 Caddy 身份侵入 LWA 工作区

第二台 Ubuntu 在管理页设置路径别名时出现 `[GATEWAY_ERROR] Caddy reload 失败`，核心错误为：

```text
mkdir /home/<serviceUser>/local-webpage-access: permission denied
```

现场身份与资源关系：

| 对象 | 实际身份 / 权限 | 需要访问 | 结果 |
| --- | --- | --- | --- |
| LWA manager / daemon | 登录用户 `serviceUser` | 工作区、Docker socket | Docker 权限问题修复后可用 |
| 发行版 `caddy.service` | 系统用户 `caddy`（如 UID 997） | 用户 home 下的 Caddyfile、aliases、sites、`logs/static-access.log` | home 为 `0750/0700` 时无法穿越或写入，reload 失败 |
| Docker daemon | root（由 socket 授权控制） | 容器运行资源 | 与 Caddy 文件权限相互独立 |

这不是简单的“日志目录少一个 ACL”，而是 **Caddy master 所有权错误**：LWA 预期自行启动并管理工作区 Caddy，但 `:2019` 上实际可能已经是发行版 systemd 以 `caddy` 用户启动的 master。当前风险点包括：

1. Linux Caddy 安装脚本在“已安装版本达标”的 `already_good` 快速路径直接退出，没有执行 `disable_system_caddy_service`。
2. 禁用系统 `caddy.service` 使用 best-effort `|| true`，失败也可能继续宣称安装完成。
3. `ensure_caddy_running()` 只要发现 admin `:2019` 在线就返回成功，没有同时要求本工作区 pidfile、进程身份和配置根归属匹配。
4. reload 会向在线 admin 推送 LWA 工作区配置；若 master 属于 `caddy` 用户，它既无法访问用户 home，也可能让 LWA误操作用户原有的系统 Caddy。

**不采用的默认修复**：不应默认递归 `setfacl -R`、把工作区 `chown` 给 `caddy:caddy`，也不应执行 `chmod o+rx /home/<user>`。这些做法会扩大整个用户工作区和 home 的可见范围，破坏 LWA service identity 与文件所有权，且不能解决“LWA 正在借用外部 Caddy master”的控制权问题。

**正确口径**：Full Profile 的 Caddy master 必须由 LWA 明确认领并以 `serviceUser` 运行；若检测到外部/系统 Caddy，先进入 `caddy_owner_mismatch`，在用户确认的安装阶段完成停用、接管和复检。在未来明确支持“外部 Caddy 集成模式”之前，不把 ACL 共享工作区作为 Full 默认方案。

### 13.2 产品口径：Full Profile 是强制基础能力集合

`--default` 与 `--full` 是两套整体运行契约，而不是同一契约上的临时安装选项：

| 契约 | 强制基础能力 | 不满足时行为 |
| --- | --- | --- |
| **Default Profile** | 工作区、CLI、manager、daemon、builtin gateway、非特权端口静态托管 | 按现有必需项决定 ready；Docker / Caddy 可缺失 |
| **Full Profile** | Default 全部能力 + Caddy + Docker Engine + Docker Compose + CLI/manager/daemon 的统一 Docker 控制权 + Caddy 严格托管 + 自启动与重启后恢复 | 任一强制项不满足即 `unready` / `degraded`，安装命令非零退出，不允许假绿 |

核心规则：

1. **选择前可选，选择后强制**：Docker / Caddy 在用户选择档位前是可选能力；执行 `setup/init --full` 后即成为该工作区和该安装实例的基础能力。
2. **LWA 是统一控制面**：Caddy 与 Docker 不互相管理，统一由 LWA 编排、观测、启停与诊断。
3. **权限必须覆盖整体**：不能只让 CLI 临时获得权限；manager、daemon 也必须在其真实后台上下文中具备相同 Docker 能力。
4. **运行期不依赖临时提权**：禁止把 `sudo lwa ...`、`sg docker lwa ...` 或 `chmod 666 /var/run/docker.sock` 当成正式运行方案。
5. **安装期与运行期分离**：安装系统包、写系统单元可在用户确认后临时使用 sudo；安装完成后的 LWA 业务进程仍以确定的非 root 运行身份常驻。

### 13.3 统一运行身份与权限边界

#### 13.3.1 LWA service identity

一次 Full Profile 安装必须固化唯一的 `serviceUser`（默认发起安装的真实登录用户，而非临时 sudo 的 root）：

```text
LWA serviceUser
├── lwa CLI
├── lwa manager
├── lwa daemon
├── lwa gateway（Caddy 控制器）
└── lwa runtime controller（Docker 控制器）
```

工作区配置或安装状态中记录：`profile=full`、`serviceUser`、`workspaceRoot`、Docker endpoint、Caddy executable/config root、安装版本和最近一次能力验收时间。启动任何后台单元时必须校验这些身份信息与当前进程一致，发现 workspace / user 漂移即拒绝假启动。

#### 13.3.2 Docker 权限

优先级建议：

1. **Rootless Docker（优先）**：使用 serviceUser 自己的 socket，并固化 `DOCKER_HOST` 到 LWA 单元环境；权限天然与用户一致。
2. **Rootful Docker + docker 组（兼容主路径）**：把 serviceUser 加入 `docker` 组；所有需要容器能力的后台进程必须实际继承该 supplementary group。
3. **Linux Full 推荐监管方式**：安装 system-level LWA unit，但用 `User=<serviceUser>`、`Group=<primaryGroup>`、`SupplementaryGroups=docker` 启动 LWA 进程；systemd 管理单元不等于业务进程以 root 运行，可避免既有 `systemd --user` 组缓存导致权限漂移。若继续使用 user unit，则 `setup --full` 必须进入 `session_refresh_required`，在重新登录 / 重启并复检前不得 ready。

安全说明：rootful Docker 的 `docker` 组近似宿主机 root 能力。Full Profile 必须在安装确认页明确告知；仅允许受信任用户导入受信任项目，继续保留 Compose 敏感挂载、Docker socket、宿主路径等安全审计。

#### 13.3.3 Caddy 权限

- 默认监听 LWA 约定的非特权端口（如 `staticGatewayPort` 和实例 hostPort），以 serviceUser 运行，不需要 root。
- 若未来直接监听 80/443，只向 Caddy 进程精确授予 `CAP_NET_BIND_SERVICE` 或交给独立系统代理；不得因此让 manager / daemon / CLI 全体 root 化。
- Full Profile 由 `lwa-gateway` 唯一托管 Caddy，安装流程应检测并处理发行版自带 `caddy.service` 冲突，避免两个 master 争用 admin `:2019` 或业务端口。
- `staticGateway: auto` 才允许降级 builtin；`staticGateway: caddy` 和 Full Profile 必须严格使用 Caddy，启动或 reload 失败即整体 degraded，不静默降级。
- Caddy 所有权不能仅凭“`:2019` 可连接”判断；必须同时验证：本工作区 `run/caddy.pid` 存活、进程可执行文件为预期 Caddy、进程 euid 等于 `serviceUser`、启动参数/config root 指向本工作区、gateway state 的 workspaceRoot 一致。
- LWA 启动 Caddy 前应以 `serviceUser` 对 Caddyfile、sites、aliases 做读测试，对 `logs/static-access.log` 及其父目录做创建/追加测试；任一失败返回 `workspace_access_denied`，禁止执行 reload。
- 检测到 `caddy.service` active/enabled 或外部 `:2019` 时，Full setup 必须 fail closed：展示 PID/euid/unit/config，取得确认后 `disable --now`，确认 admin 端口释放，再由 `lwa-gateway` 接管；停用失败不得继续 ready。
- 独立系统用户 `caddy` 访问 LWA 工作区仅作为未来显式 `external-caddy` 模式研究项；该模式需独立配置根、日志传递与最小 ACL 设计，不与当前 Full Profile 混用。

#### 13.3.4 文件与密钥权限

| 对象 | 建议权限 | 所有者 |
| --- | --- | --- |
| 工作区根、`apps/`、`data/`、`run/` | `0700`（需要协作时显式放宽） | serviceUser |
| token、`.env.local`、状态密钥 | `0600` | serviceUser |
| 普通配置、Caddy 片段 | `0600` 或最小可读 | serviceUser |
| 日志 | 默认 `0600`，按现有滚动策略管理 | serviceUser |
| 系统 unit | `0644` | root（仅定义如何以 serviceUser 启动） |

### 13.4 LWA / Caddy / Docker 协作模型

```text
用户 / 管理页
      │
      ▼
LWA Manager ───────► LWA lifecycle / registry
      │                        │
      │                        ├──► Docker Engine / Compose（容器实例）
      │                        │
      └────────────────────────└──► LWA Gateway / Caddy（统一入口、别名、静态实例）

LWA Daemon ─► 导入 / 调度 ─► 同一 lifecycle
LWA Doctor ─► 分别验证 CLI、manager、daemon、gateway 的真实能力
```

协作约束：

- manager / daemon 不自行拼接另一套 Docker 或 Caddy 行为，统一调用 lifecycle/runtime 层。
- Docker 负责镜像、容器、网络和容器日志；Caddy 负责统一入口、静态站点、路径别名和访问日志；registry 保存 LWA 的期望状态与最后可信观测。
- Full Profile 启停顺序：Docker daemon ready → LWA runtime capability ready → Caddy ready → manager / daemon ready → 实例 reconcile。
- Docker 或 Caddy 暂时不可用时，LWA 控制面仍应可打开并展示 degraded 原因，但不得执行会扩大状态偏差的自动纠正。

### 13.5 能力模型与状态语义

#### 13.5.1 能力状态

建议增加统一 `CapabilityReport`，至少包含：

```text
profile: default | full
overall: ready | degraded | unready
dockerEngine: ready | unavailable | version_unsupported
dockerCompose: ready | unavailable | version_unsupported
dockerAccess: ready | permission_denied | daemon_unavailable | timeout | unknown
caddyBinary: ready | unavailable | version_unsupported
caddyRuntime: ready | admin_unavailable | config_invalid | port_conflict | owner_mismatch | workspace_access_denied | unknown
caddyOwner: lwa_service_user | system_caddy | foreign_process | unknown
caddyProcessUser: <user-or-uid>
caddyWorkspaceAccess: ready | read_denied | write_denied | unknown
managerDockerAccess: ...
daemonDockerAccess: ...
gatewayAccess: ...
sessionRefreshRequired: true | false
```

该报告由 setup、doctor、autostart check、manager `/api/health` 共用同一判定源，避免四套口径漂移。

#### 13.5.2 实例状态必须区分“停止”与“观测失败”

建议把现有单一 status 语义拆开：

```text
desiredState: running | stopped
observedState: running | stopped | exited | missing | unknown
observationError: null | permission_denied | daemon_unavailable | timeout | ...
lastObservedAt: timestamp
lastTrustedState: running | stopped | ...
```

兼容现有 API 时可以先增加 `runtimeAccess` / `observationError` 字段，并遵循：

- `docker compose ps` 明确返回无容器，才能判 `missing/stopped`。
- 权限不足、Docker daemon 不可达、超时、输出无法解析均判 `unknown`。
- `unknown` 不覆盖 `lastTrustedState`，不得写成 stopped，不触发自动 stop/start/rebuild。
- 管理页显示“运行状态未知：manager 无 Docker 权限”，并给出可执行修复命令。
- `desiredState` 始终只表达用户意图，不因观测失败被反向修改。

#### 13.5.3 daemon reconcile 与观测失败的交互

`daemon.reconcile` 在 Full / 容器路径上必须遵守：

| `observationError` / `runtimeAccess` | reconcile 行为 |
| --- | --- |
| `null` 且 `observedState=stopped\|exited\|missing`，且 `desiredState=running` | 允许按现有逻辑尝试轻量恢复（`start` / restart） |
| `permission_denied` / `daemon_unavailable` / `timeout` / `unknown` | **禁止**自动 start/stop/rebuild；记事件；保持 `lastTrustedState` |
| Full Profile 且 `CapabilityReport.overall≠ready` | 整轮 reconcile **跳过容器实例**的自动纠正；静态 builtin 是否自愈可另议，但不得因 Docker 不可用以「恢复」名义写 stopped |

理由：权限/引擎故障属于**控制面降级**，自动 reconcile 只会放大 CLI 与管理页的状态分裂（正是 Ubuntu 故障的放大器）。

### 13.6 `setup --full` 原子安装与验收流程

```text
解析 full 契约
  → 确定 serviceUser / workspace
  → 安装并校验 Docker、Compose、Caddy
  → 配置 serviceUser 的持久 Docker 权限
  → 安装/更新 LWA 后台监管单元
  → 检测并停用冲突的系统 Caddy，确认 :2019 释放
  → 以 serviceUser 启动 LWA Caddy，验证 owner + workspace 读写 + reload
  → 启动/确认 Docker
  → 从 CLI、manager、daemon 的真实上下文分别做能力自检
  → 执行最小 Docker build/up/ps/down 闭环
  → 执行 Caddy validate/reload/HTTP 闭环
  → 执行系统/会话重启后的 autostart 复检
  → 全部通过才写 profile=full、overall=ready
```

必须支持幂等与可恢复：若 Linux 组权限需要重新登录，保存 `full-setup-state`（已完成步骤、待刷新原因、serviceUser），返回明确非零状态和 `lwa setup --full --resume` 指引；恢复后从能力复检继续，而不是重复安装或假装完成。

建议退出语义：

| 结果 | 退出码建议 | 含义 |
| --- | --- | --- |
| `ready` | 0 | Full 契约全部满足 |
| `session_refresh_required` | 2 | 系统变更已完成，但权限上下文尚未生效，需要重登/重启后 resume |
| `unready` | 1 | 安装、版本、权限或闭环验收失败 |

### 13.7 CLI、管理页与诊断设计

新增或增强：

```text
lwa setup --full [--resume] [--yes]
lwa doctor --profile full
lwa autostart check --profile full
lwa capabilities [--json]
```

Full doctor 输出至少包括：

```text
Workspace                 ready
Service identity          ready (user=...)
Docker Engine             ready
Docker Compose            ready
CLI Docker access         ready
Manager Docker access     ready
Daemon Docker access      ready
Caddy binary              ready
Caddy admin/reload        ready
Gateway ownership         ready
Autostart                 ready
Overall                   READY
```

管理页顶部增加 Full Profile 健康状态；若 degraded，实例列表仍可读，但容器操作按钮禁用并显示原因。权限修复后支持“重新检测能力”，检测通过再恢复操作。

#### 13.7.1 最小 API / health JSON 草案（实现契约）

`GET /api/health`（及 `lwa capabilities --json`）在 Full 相关字段上建议兼容扩展：

```json
{
  "version": "V0.x.x",
  "workspaceRoot": "/path/to/workspace",
  "profile": "full",
  "overall": "degraded",
  "serviceUser": "fenix",
  "capabilities": {
    "dockerEngine": "ready",
    "dockerCompose": "ready",
    "cliDockerAccess": "ready",
    "managerDockerAccess": "permission_denied",
    "daemonDockerAccess": "permission_denied",
    "caddyBinary": "ready",
    "caddyRuntime": "owner_mismatch",
    "caddyOwner": "system_caddy",
    "caddyProcessUser": "caddy",
    "caddyWorkspaceAccess": "write_denied",
    "sessionRefreshRequired": true
  },
  "action": "refresh login/systemd user session, then: lwa setup --full --resume"
}
```

实例详情 / 列表项建议增加（可与现有 `status` 并存一个版本周期）：

```json
{
  "id": "prd-...",
  "desiredState": "running",
  "status": "running",
  "observedState": "unknown",
  "runtimeAccess": "permission_denied",
  "lastTrustedState": "running",
  "lastError": "Docker 权限不足：manager 无法访问 docker.sock"
}
```

兼容规则：旧前端只读 `status` 时，**不得**在 `runtimeAccess=permission_denied` 时把 `status` 写成 `stopped`（BUG-230 止血约束）；新前端优先展示 `observedState` + `runtimeAccess`。

### 13.8 可执行 WBS

| WBS | 优先级 | 任务 | 主要触点 | 完成定义 |
| --- | --- | --- | --- | --- |
| **033.01** | P0 | 固化 Full Profile 强契约与持久安装状态 | `config.py`、`models.py`、`host_bootstrap.py` | 能持久区分 default/full，记录 serviceUser、endpoint、验收时间；旧配置平滑迁移 |
| **033.02** | P0 | 在 BUG-230 止血修复上补全正式观测状态模型 | `docker_runtime.py`、`lifecycle.py`、`status.py`、registry schema | 权限/daemon/timeout/解析失败统一进入 unknown；持久化最后可信状态、lastObservedAt 与 observationError，API 兼容迁移 |
| **033.03** | P0 | 建立统一 `CapabilityReport` | 新建 capability 模块，接入 setup/doctor/status | CLI 当前进程可准确区分版本、权限、daemon、超时和 Caddy 故障 |
| **033.04** | P0 | manager / daemon 真实上下文能力自检 | `manager_service.py`、`daemon.py`、`manager_api.py` | 后台进程自行探测 Docker；健康 API 与状态文件可查询，不能用 CLI 结果冒充 |
| **033.05** | P0 | Linux 持久 Docker 权限与 service identity 编排 | Docker 安装脚本、`autostart.py`、systemd unit | rootless 或 `User` + `SupplementaryGroups=docker` 路径可用；无 `sg docker` 依赖；运行进程非 root |
| **033.06** | P0 | `setup --full` 分阶段状态机与 `--resume` | `host_bootstrap.py`、`setup.py`、CLI | session refresh 可恢复；未闭环不写 ready；退出码与提示稳定可测 |
| **033.07** | P0 | Caddy 严格模式、唯一所有权与 serviceUser 接管 | `config.py`、`static_gateway.py`、`gateway_service.py`、安装脚本 | full / 显式 caddy 不降级；所有 install 路径处理 caddy.service；admin 在线须校验 pid/euid/workspace；owner mismatch fail closed；以 serviceUser 接管后读写日志并 reload 成功 |
| **033.08** | P1 | Full autostart 依赖顺序与重启恢复 | `autostart.py`、systemd/launchd 生成器 | Docker/Caddy/LWA 顺序正确；重启后 manager/daemon 仍有权限且只运行一份 |
| **033.09** | P1 | `doctor --profile full` / `capabilities --json` | `doctor.py`、CLI | 输出组件、身份、各后台上下文能力、修复建议与整体结论 |
| **033.10** | P1 | 管理页 degraded 展示与危险操作阻断 | `manager_api.py`、`manager_static/*` | 权限未知时不显示 stopped、不允许误操作；原因和恢复入口清晰 |
| **033.11** | P1 | 文件、token、日志与 Caddy 配置权限收紧 | paths/setup/logging/gateway 相关模块 | 新建文件权限符合 §13.3.4；Caddy 以 serviceUser 访问，无递归开放 home/工作区或转移所有权；升级不破坏用户数据 |
| **033.12** | P0 | 单元与集成测试 | `tests/` | 覆盖 permission denied、daemon down、timeout、身份漂移、session refresh、系统 caddy.service 已启用、foreign :2019、owner/euid 不匹配、工作区读写拒绝、严格 Caddy、resume |
| **033.13** | P0 | Ubuntu / WSL / macOS 实机验收 | 验收脚本、`docs/acceptance-checklist.md` | Ubuntu 主路径：预启系统 caddy.service → full 接管 → alias reload/log 写入 → 重启 → 容器/管理页一致；**macOS 不替代 Ubuntu 权限验收**（§13.1.3）；WSL 须标明 Desktop vs Engine |
| **033.14** | P1 | 文档与 Skill 同步 | README、autostart/operations/security 文档、setup Skill | 明确 Full 权限含义、Docker 组风险、故障恢复；写明 Mac「很少遇权限问题」≠ Full 已验收 |

#### 13.8.1 建议实施阶段

| 阶段 | 范围 | 前置 / 交付门槛 |
| --- | --- | --- |
| **A：停止错误扩散** | 033.02～04、033.12 对应用例 | 先修“权限失败→stopped”；管理页不再污染状态 |
| **B：权限与安装闭环** | 033.01、033.05～06 | Full 可持久识别 serviceUser，安装可 resume，后台真实具备 Docker 权限 |
| **C：Caddy 与自启动闭环** | 033.07～08、033.11 | Caddy 严格托管，重启后 Docker/Caddy/LWA 全链路恢复 |
| **D：产品化与验收** | 033.09～10、033.13～14 | doctor/API/UI/文档一致，三平台验收完成 |

推荐执行顺序：**A（P0 止血）→ B（权限根治）→ C（完整协作）→ D（产品化）**。A 完成前不得继续把权限失败按 stopped 处理；B/C 完成前 `setup --full` 应明确标记 legacy/incomplete，不得使用新的 `ready` 口径。

### 13.9 验收标准

1. Ubuntu 新机执行 `lwa setup --full` 后，CLI、manager、daemon 在各自真实上下文中均可访问 Docker；不依赖每次手工 `sudo` / `sg docker` / `newgrp docker`。
2. Full Profile 下 Caddy、Docker、Compose 任一强制能力不满足时，overall 不得为 ready，命令不得以成功退出码假绿。
3. Docker 权限不足时，实际运行容器不得被回写 stopped；API/管理页显示 unknown/degraded 与明确原因。
4. `staticGateway: caddy` 和 Full Profile 不得静默降级 builtin；Caddy reload 失败要保留旧可用配置并报告 degraded。
5. 系统重启或用户重新登录后，Docker、Caddy、manager、daemon 自动恢复且保持单实例；管理页状态与 `docker compose ps` 一致。
6. 最小真实闭环通过：build → up → ps/health → manager 展示 running → stop → start → remove；构建日志持续可见。
7. `lwa doctor --profile full` 与 `/api/health` 对同一能力给出一致结论，JSON 输出可供自动化验收。
8. 安装脚本幂等；中途失败或 session refresh 后可 `--resume`，不重复破坏系统源、用户组、工作区和单元文件。
9. Ubuntu 预先启用发行版 `caddy.service`（`User=caddy`）时，Full setup 必须识别 owner mismatch，未经确认不得 reload；接管后实际 Caddy euid 为 serviceUser，可读取站点/别名配置并追加 `logs/static-access.log`，且不修改 home 的 other 权限、不把工作区 chown 给 caddy。

### 13.10 风险与边界

| 风险 | 处理 |
| --- | --- |
| `docker` 组近似 root | Full 安装前显式告知并确认；优先 Rootless；只接受受信任项目；安全审计保持 critical 阻断 |
| system unit 由 root 写入被误解为 LWA 以 root 运行 | 文档与 doctor 同时展示 unit owner 与进程 euid；业务进程必须是 serviceUser |
| Linux 发行版和 WSL 的 systemd 差异 | 能力探测后选 backend；不支持时走可恢复 user-unit + session refresh 路径，不假绿 |
| macOS Docker Desktop 启动较慢 | capability 状态区分 binary/desktop daemon；后台有限重试，超时 degraded 并提示启动 Desktop |
| **用 macOS 绿路径误判 Linux Full 已完成** | 验收清单强制 Ubuntu（或 WSL-Engine）权限继承项；§13.1.3 写入 Skill「禁止以 Mac 代替」 |
| Caddy 系统服务冲突 | Full 安装前检测；用户确认后 disable 冲突单元，LWA gateway 保持唯一所有权 |
| 用 ACL/chown 快速放行系统 caddy | 不作为默认方案；避免扩大 home 暴露和双所有者漂移。Full 统一以 serviceUser 接管；未来 external-caddy 另立模式 |
| 状态 schema 迁移影响旧 API | 先兼容新增字段并保留 status，再分阶段引入 observedState；管理页兼容两版 |
| 控制面 degraded 时是否可用 | manager 保持只读与诊断可用；只禁用依赖故障能力的变更操作 |
| BUG-230 止血被误当成 IMP-033 完成 | task-list / 本节文首明确：正式 unknown 模型与 CapabilityReport 仍属 DEV-076 |

### 13.11 task-list 编号映射

| task-list | 关系 |
| --- | --- |
| `PLN-014` | IMP-033 问题分析、功能设计与 WBS 规划 |
| `DOC-043` | 本文 §13 权限与能力闭环文档 |
| `BUG-230` | WBS-033.02 的已完成止血基线：Docker 权限失败不再误写 stopped；正式状态模型仍归 DEV-076 |
| `BUG-231` | WBS-033.07：系统 caddy.service / 外部 admin 被误当 LWA Caddy，导致 owner 与工作区权限不一致 |
| `DEV-076` | IMP-033 主开发项，按 WBS-033.01～14 推进 |


## 14. IMP-034 — 日志可观测性补强（排障可读）

> **提出背景（2026-07-18）**：Ubuntu 实机排障时，全局 `logs/` 常看不到 CLI 操作痕迹；构建期曾出现 `build.log` 长时间为空（BUG-229 已流式止血）；Docker 权限失败时难从日志一眼区分「卡在锁/排队/构建/权限」。现有分类日志与 registry events **骨架已在**，但关键路径可观测性仍偏弱。
>
> **与既有项关系**：BUG-229（build 流式落盘）为实例构建日志基线；IMP-033 `CapabilityReport` / `observationError` 为本项结构化能力日志的数据源；本项**不替代** IMP-033，只保证「人能读、机器能对账」。
>
> **状态**：待开发。

### 14.1 问题分析

| 缺口 | 现状 | 排障后果 |
| --- | --- | --- |
| CLI 不落盘 | `cli/_common.bootstrap()` 调用 `setup_logging(level=...)` **未传 `log_dir`**，多数 CLI 操作只进终端 | 关终端后 `logs/lwa.log` 空或陈旧，无法事后复盘 |
| daemon 文件日志路径分裂 | watcher 有 `daemon.log` FileHandler（BUG-189），但入口 `setup_logging` 仍常不带工作区 `logs/` | 命名空间/级别与 manager 不完全一致 |
| 生命周期少阶段心跳 | `host_container` 在生成文件 → 获构建槽 → `compose build` 之间 INFO/事件不足 | 长时间无 `build.log` 时误判「调度卡死」 |
| 权限/能力不可读 | BUG-230 有 `last_error` + 启动 WARN；缺统一「谁探测、结论、建议动作」的结构化记录 | CLI 有权、manager 无权时日志对不上 |
| 排障地图缺失 | 文档列了目录，但无「按症状看哪个文件」索引 | 用户不知先看 build / run / manager / events |

### 14.2 目标与非目标

**目标**：

1. CLI、manager、daemon、gateway **凡绑定工作区的进程**，默认把 `local_webpage_access.*` 日志追加到工作区 `logs/` 下约定文件（权限 `0600`，沿用滚动策略）。
2. 容器/静态关键路径输出**可检索的阶段 INFO**，并写入 registry `events`（稳定 `event_type`），使「卡在哪一步」不依赖猜。
3. 能力探测（Docker/Caddy）输出**结构化一行日志 + 可选 events**，字段对齐 IMP-033 `CapabilityReport`（即便 Full 未全落地，也可先落最小子集）。
4. 文档 / FAQ / Skill 增加「症状 → 日志文件 → 命令」索引。

**非目标（本期）**：

- 不上集中式日志栈（ELK/Loki）；不改默认把 DEBUG 刷满磁盘。
- 不把完整 token、`.env.local` 密钥写入任何日志（保持现有安全边界）。
- 不替代 `docker compose` 自身输出；实例 `build.log`/`run.log` 仍是命令 stdout 真源（BUG-229）。
- 不在本期做管理页「实时日志 tail WebSocket」（可后续单列）；现有 `lwa logs` / API tail 足够。

### 14.3 关键决策

| 编号 | 决策点 | 方案 |
| --- | --- | --- |
| **034.a** | 工作区文件落盘 | `open_workspace_registry` / 各前台入口在已知 `workspace.logs` 后调用 `setup_logging(..., log_dir=workspace.logs, force=…)`；CLI 主文件 **`logs/lwa.log`**；daemon **`logs/daemon.log`**（已有则统一格式）；manager **`logs/manager.log`**；gateway 保持现有或并入约定名 |
| **034.b** | 幂等与多进程 | 同一进程内 handler 不重复添加（现有 `_CONFIGURED`）；多进程各写各文件，**禁止**多进程共写同一 `lwa.log` 无锁——CLI 短生命周期可接受；长驻进程只用自己的文件 |
| **034.c** | 级别 | 默认跟 `config.logLevel` / CLI `--log-level`；阶段心跳用 **INFO**；能力探测失败用 **WARNING**；不在 INFO 打印 compose 全量 stdout（仍进实例 log） |
| **034.d** | 生命周期事件类型 | 稳定枚举（示例）：`lifecycle_stage`（message 含 stage=…）、或细分为 `dockerfile_ready` / `compose_ready` / `build_slot_acquired` / `compose_build_start` / `compose_build_done` / `compose_up_start` / `observe_degraded`；实现时选「少量稳定类型 + message 结构化」或「多类型」，须在 Skill/文档固定，避免随意字符串 |
| **034.e** | 能力日志格式 | 单行可 grep，建议：`capability probe role=manager dockerAccess=permission_denied sessionRefreshRequired=true hint=...`；与 `/api/health.capabilities` 字段名一致（IMP-033 §13.7.1） |
| **034.f** | 与 IMP-033 顺序 | **可并行**：034.01～02 不依赖 Full 闭环；034.03 在 CapabilityReport 落地前可用 `probe_docker_permission()` 过渡，Report 就绪后改读同一判定源 |

### 14.4 优先级与实施拆分（建议三档）

#### P0 — ① CLI / daemon 统一写入工作区文件日志

| WBS | 任务 | 主要触点 | 完成定义 |
| --- | --- | --- | --- |
| **034.01** | CLI bootstrap 绑定工作区后写入 `logs/lwa.log` | `cli/_common.py`、`logging.py` | 任意 `lwa start/status/...` 后 `logs/lwa.log` 有带时间戳的对应记录；权限 0600 |
| **034.02** | daemon / gateway 入口与 FileHandler 格式对齐 | `daemon.py`、`gateway_service.py` | 与 manager 相同 formatter；启动一行写明 log 路径；单测不依赖真实长跑 |

#### P1 — ② 生命周期阶段 INFO + registry 事件

| WBS | 任务 | 主要触点 | 完成定义 |
| --- | --- | --- | --- |
| **034.03** | `host_container` / `host_static` / `host_frontend` 阶段心跳 | `hosting.py`、`lifecycle.py`、`build_queue.py` | 至少覆盖：开始托管、Dockerfile/compose 已生成、获得构建槽、compose build 开始/结束、up 开始/结束、观测降级；INFO 与 events 双写 |
| **034.04** | 构建排队/锁等待可观测 | `build_queue.py`、`lifecycle.instance_lock` | 进入排队、排队超时、锁等待超时均有 WARNING + event，避免「只有空 build.log」 |

#### P2 — ③ 权限/能力探测结构化日志（对齐 CapabilityReport）

| WBS | 任务 | 主要触点 | 完成定义 |
| --- | --- | --- | --- |
| **034.05** | 统一 `log_capability_probe(role, report_or_subset)` | 新建小模块或放 `logging.py` / 未来 `capability.py` | manager/daemon 启动、doctor、observe 降级路径复用；字段名与 IMP-033 一致 |
| **034.06** | observe 降级写 event | `lifecycle._observe_container_status` | `observationError=permission_denied` 时 event + WARNING 含 role 暗示（manager sync vs CLI） |
| **034.07** | 文档排障地图 | `docs/faq.md`、`docs/runtime-workspace.md`、`docs/operations-playbook.md`、相关 Skill | 「构建无输出 / 管理页 stopped 容器在跑 / daemon 不导入」→ 看哪个文件 + 哪条 `lwa logs` / `lwa doctor` |

### 14.5 验收标准

1. 执行 `lwa status`（或任意写路径命令）后，工作区 `logs/lwa.log` 出现本命令相关 INFO/WARNING，而不仅是终端输出。
2. `lwa start` 容器实例时，在 `build.log` 仍为空的时间窗内，`lwa.log` 或 `events` 至少能看到「已生成 compose / 等待槽位 / 开始 build」之一。
3. 模拟 Docker 权限失败：`last_error`、WARNING 行、registry event 三者信息一致，且能 grep 到 `permission_denied` 或等价字段。
4. manager/daemon 启动时各写一条 capability 探测摘要到各自 log 文件。
5. FAQ 增加排障地图；相关单测覆盖「带 log_dir 的 setup_logging」与阶段 event 写入（可用 tmp workspace）。
6. 不引入密钥/token 落盘回归（现有安全测试保持绿）。

### 14.6 风险与边界

| 风险 | 处理 |
| --- | --- |
| 小主机磁盘被 INFO 打满 | 沿用 10MB×3 滚动；阶段日志短消息；禁止把 compose 全文打进 `lwa.log` |
| CLI 与 daemon 抢写同一文件 | 分文件：`lwa.log` / `daemon.log` / `manager.log` |
| event_type 膨胀导致前端难展示 | 固定白名单；管理页可先只展示 message |
| 与 IMP-033 字段日后更名 | 034.f：以 CapabilityReport 为单一命名源，本项跟随 |

### 14.7 与 Ubuntu 故障的对应关系

| 当时现象 | 本项对应 |
| --- | --- |
| 全局日志没有 build 相关 | 034.01 落盘 + 034.03 阶段事件 |
| `build.log` 空却以为没 build | BUG-229 流式 + 034.03/034.04 阶段/排队 |
| manager 无 Docker 权限看不出 | 034.05/034.06 结构化 capability + observe 事件 |
| 不知先看哪个文件 | 034.07 排障地图 |

### 14.8 task-list 编号映射

| task-list | 关系 |
| --- | --- |
| `PLN-015` | 本 §14 规划 |
| `DOC-046` | 本文 §14 写入 |
| `DEV-077` | IMP-034 主开发项（WBS-034.01～07） |
| `BUG-229` | 构建流式日志基线（已完成，本项不重复实现） |
| `BUG-230` / `DEV-076` | 权限观测与 CapabilityReport；034.05 对齐其字段 |
