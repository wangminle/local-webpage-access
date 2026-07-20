# 新增功能点计划 IMP-025～IMP-028 / IMP-030 / IMP-031～041（202607）

> **状态**：IMP-025～028 已落地（见 `task-list` DEV-068～072）；**IMP-030 跨平台自启动已落地（2026-07-16，见 `task-list` DEV-073～076，关闭 BUG-138/139）**；**IMP-031 / IMP-032 已落地（2026-07-17，DEV-074 / DEV-075）**；**IMP-033 Full Profile 权限与能力闭环主路径已落地（2026-07-19，DEV-076/078，关闭 BUG-231；033.13 实机验收与 system unit SupplementaryGroups 完整路径可后续补强）**；**IMP-034 日志可观测性补强已落地（2026-07-19，DEV-077/079）**；**IMP-035 管理页安全删除主路径已落地（2026-07-20，DEV-080 / DOC-052；035.06 浏览器实机可后续补）**；**IMP-036 正式支持平台收敛主路径已落地（2026-07-20，DEV-081；036.08 实机清单与 036.09 Windows 分支清理可后续补）**；**IMP-037 / IMP-038 / IMP-039 / IMP-040 / IMP-041 已落地（2026-07-20，DEV-082 / DEV-083 / DEV-084 / DEV-087 / DEV-088）**；原 IMP-040 `update --pull` / IMP-041 Vite 端口元数据已从范围删除。编号续接 IMP-024（见已归档的 [`local-webpage-access-imp010-021-plan-20260707.md`](../archive/local-webpage-access-imp010-021-plan-20260707.md)）；IMP-029 见 [`待改进功能点记录-20260706.md`](./待改进功能点记录-20260706.md)。
> **范围**：§0～§9 为管理页浏览量统计改进；§10 为 macOS / Linux（含 WSL）自启动配置与完备性检查；§11 为 Docker 国内源安装脚本；§12 为 setup/init 的 `--default` / `--full` 环境装配档位；§13 为 `--full` 下 LWA、Caddy、Docker 的统一权限契约、运行协作与可执行 WBS；§14 为日志可观测性补强；§15 为管理页任意项目的二次确认安全删除；§16 为正式支持平台矩阵；§17 为 `design/achievement/` 全量功能反查；§18～§20 依次为网关后端原子切换、升级后访问闭环、进行中构建取消；§21 为管理页/访问地址在 LAN IP 变化后的新鲜度与自愈；§22 为删除/purge 阶段日志与容器别名清理（IMP-034 后续 + BUG-268）。

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
- **Windows 原生明确不支持**（由 IMP-036 §16 取代早期“本期不做”的临时口径）；仅允许为 WSL2 Linux 生成宿主侧唤醒发行版所需的 `.ps1`/`.bat` 指引，LWA 本体仍运行在 WSL2 内。

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
| Windows 原生一键安装脚本 | 否（不支持） | 不再提供原生 Windows / Docker Desktop 安装指引；WSL2 内按 Linux 路径处理，见 IMP-036 §16 |
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
| Windows 原生 full 一键装 | 否 | 否（不支持） | 原生 Windows fail-fast；WSL2 内走 Linux 脚本与支持矩阵，见 IMP-036 §16 |

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
| **D：产品化与验收** | 033.09～10、033.13～14 | doctor/API/UI/文档一致，Ubuntu/Debian/WSL2/macOS 支持矩阵验收完成；不含 Windows 原生 |

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
> **状态（2026-07-19）**：**已落地**。CLI/daemon/manager/gateway 分文件落盘、生命周期阶段事件、能力探测结构化日志与 FAQ 排障地图已完成，见 `DEV-077` / `DEV-079`；后续增强随对应能力项继续维护，不再把本节标记为待开发。**删除/purge 路径的阶段日志与破坏性 API 审计见 §22 IMP-041**（与 BUG-268 同批）。

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


## 15. IMP-035 — 管理页任意项目安全删除（二次确认）

> **提出背景（2026-07-20）**：管理页后端已有 `POST /api/instances/{id}/remove`，CLI 也已有 `lwa remove`，但 `17800` 管理页只对 `redundant=true` 的实例显示“删除”按钮。普通已导入项目无法从页面移除；现有网页删除还固定使用默认参数，只会停服并清 registry，不能选择是否删除 `apps/<id>/`。
>
> **已确认方案**：采用“C 方案”——第一步选择“仅移除”或“彻底删除”，第二步输入完整项目 ID 完成高风险确认。两种路径都必须经过二次确认；彻底删除对非空 `data/` 再明确提示不可恢复后才允许 `force=true`。
>
> **状态**：主路径已落地（2026-07-20，DEV-080 / DOC-052）；035.06 浏览器实机验收可后续补强。

### 15.1 目标与非目标

**目标**：

1. 所有已导入项目均显示删除入口，不再把“是否冗余”作为显示条件。
2. 用户可明确选择：
   - **仅移除**：停止服务、删除 registry 及关联记录，保留 `apps/<id>/`，对应 `purge=false`；
   - **彻底删除**：停止服务、删除 registry，并删除 `apps/<id>/`，对应 `purge=true`。
3. 两种路径均执行二次确认；第二次确认必须输入完整项目 ID，不能只依赖浏览器原生 `confirm()`。
4. 彻底删除必须把 `data/` 非空、删除不可恢复、容器/静态服务将停止等影响写清楚；用户显式勾选“理解数据不可恢复”后才允许发送 `force=true`。
5. 删除期间禁用重复操作；成功后关闭相关详情/日志弹层并刷新列表；失败时保留当前页面状态并显示后端原始错误。

**非目标**：

- 不新增“回收站”或服务端软删除机制；“仅移除 + 保留 apps 目录”承担可恢复路径。
- 不改变 `lwa remove` 的既有默认语义。
- 不改变“批量删除冗余”的目标选择规则；批量入口仍只处理冗余实例，单项目删除入口则对全部实例开放。
- 不允许前端自行拼接或删除文件；所有破坏性动作仍必须经过后端 `remove_instance()` 的路径边界、实例锁、停服和 `data/` 保护。

### 15.2 交互与数据流

1. 用户点击项目行“删除”。按钮对所有实例可见；实例正在 `building/starting/stopping/removing` 时禁用。
2. **第一次确认（选择范围）**：弹出受控模态框，展示项目名称、项目 ID、当前状态，并提供：
   - “仅移除（保留项目文件）”；
   - “彻底删除（删除项目文件与数据）”。
   默认选中“仅移除”，避免把危险选项作为默认值。
3. 用户继续后进入**第二次确认（身份复核）**：再次展示最终动作摘要，要求输入完整项目 ID；输入不一致时最终按钮保持禁用。
4. 若选择“彻底删除”，第二步同时显示不可恢复警告与独立复选框；勾选后调用：
   - 普通目录：`POST /api/instances/{id}/remove?purge=true`；
   - 后端返回 HTTP `409` 且稳定错误码为 `data_nonempty` 时，不自动重试。页面转为显式的“包含非空 data/”警告；用户再次确认不可恢复后才调用 `purge=true&force=true`。其他错误不得进入 force 分支。
5. 若选择“仅移除”，调用 `POST /api/instances/{id}/remove?purge=false`。
6. 前端为目标 ID 设置 `removing` 本地操作态，阻止重复提交；请求结束后清理该状态。

> 二次确认是两阶段业务状态，不使用两个连续、内容相似的原生 `confirm()` 糊弄验收。第一阶段回答“删到什么程度”，第二阶段回答“确认目标和后果”；彻底删除非空 `data/` 时的 force 提升必须由新的明确用户动作触发，不能捕获错误后自动追加 `force=true`。

### 15.3 安全与错误边界

| 场景 | 要求 |
| --- | --- |
| 项目不存在或已被并发删除 | 后端返回稳定错误；前端提示并刷新列表，不继续调用 force |
| 停服失败 | 沿用 `remove_instance()` 既有“记录告警后清 registry”的语义；响应/事件需能说明是否可能残留容器或进程 |
| `data/` 非空 | 后端返回 HTTP `409` + `data_nonempty`；首次 purge 不带 force，只有用户在明确看到该风险后再次确认才允许 force |
| ID 含特殊字符 | URL 使用 `encodeURIComponent`；确认输入按原始完整 ID 严格相等比较 |
| 重复点击/慢请求 | 前端操作态去重；后端继续依赖实例锁保证串行 |
| 目录越界或符号链接 | 继续由后端 `remove_instance()` 校验解析路径位于 `apps/` 内；前端不得绕过 |
| 批量冗余删除 | 保持独立入口和既有规则，不复用单项目二次确认状态导致目标漂移 |

**删除 API 契约补齐**：当前通用 `LifecycleError` 会被管理 API 映射为 `internal` / HTTP 500，前端无法稳定区分“非空 data 需要进一步确认”和真正的内部故障。实现时必须先为该业务分支建立专用、可测试的错误契约（专用异常或等价的结构化错误），仅将非空 `data/` 映射为 HTTP `409`、错误码 `data_nonempty`；其余 `LifecycleError` 保持原有故障语义，不得通过解析中文 message 驱动状态机。删除成功响应由 `{instanceId, action}` 补齐为至少 `{instanceId, action, purge, force}`，便于验收和排障回显。

### 15.4 可执行 WBS

| WBS | 优先级 | 任务 | 主要触点 | 完成定义 |
| --- | --- | --- | --- | --- |
| **035.01** | P0 | 前置补齐删除 API 稳定错误与成功响应契约 | `manager_api.py`、`lifecycle.py` | 非空 `data/` 唯一映射为 HTTP 409 + `data_nonempty`；其他 `LifecycleError` 不误映射；成功响应回显 `instanceId/action/purge/force` |
| **035.02** | P0 | 先写后端删除契约失败测试 | `tests/test_manager_api.py`、lifecycle tests | 覆盖 purge false/true、非空 data、force、成功参数回显、目录越界、并发/不存在实例 |
| **035.03** | P0 | 先写前端显示条件与双阶段状态机失败测试 | `tests/test_manager_static_app.py` | 普通实例有删除入口；未输入 ID、未完成风险确认时不能提交；仅 `data_nonempty` 可进入 force 确认；进行中状态禁用 |
| **035.04** | P0 | 实现所有实例删除模态框与请求映射 | `manager_static/helpers.js`、`manager_static/app.js`、`manager_static/style.css` | 不再以 `redundant` 控制入口；`purge/force` 与状态机严格对应；`building/starting/stopping/removing` 及请求中禁用；键盘焦点、Esc、窄屏可用 |
| **035.05** | P1 | 更新管理页和 FAQ 文档 | `docs/manager-page.md`、`docs/faq.md` | 清楚区分“仅移除”和“彻底删除”，写明不可恢复边界 |
| **035.06** | P0 | 浏览器验收 | Playwright / 手工验收清单 | 普通、冗余、静态、容器实例各走一次取消/仅移除/彻底删除路径 |

### 15.5 验收标准

1. 普通实例和冗余实例均显示单项目“删除”按钮。
2. 第一步未选择删除范围不能继续；第二步未输入完整项目 ID 不能提交。
3. “仅移除”后 registry 不再显示实例，但原 `apps/<id>/` 完整保留。
4. “彻底删除”后 registry 与 `apps/<id>/` 均不存在；非空 `data/` 未经过 force 风险确认时必须被后端阻断。
5. 取消任意一步不产生 API 请求、不改变实例状态。
6. 首次遇到非空 `data/` 时响应为 HTTP `409`、错误码 `data_nonempty`；只有该错误码可进入 force 再确认，网络失败、其他后端失败或并发删除均不会自动升级为 force。
7. 成功响应准确回显实际 `purge/force`；管理页在 `building/starting/stopping/removing` 和删除请求期间禁止重复提交。
8. 批量冗余删除行为不变；现有 CLI 删除行为不变。

### 15.6 task-list 编号映射

| task-list | 关系 |
| --- | --- |
| `PLN-016` | IMP-035 交互、安全边界与 WBS 规划 |
| `DOC-050` / `DOC-051` | 本文 §15/§16 初始写入及 CHK-090 契约、WBS、口径复审修订 |
| `DEV-080` | IMP-035 主开发项（WBS-035.01～05 已完成；035.06 浏览器实机可后续补） |


## 16. IMP-036 — 正式支持平台收敛、最低版本与 Windows 原生阻断

> **产品决策（2026-07-20）**：LWA 只正式支持 **Linux 裸机、WSL2 中运行的 Linux、macOS**。**Windows 原生进程明确不支持**；Windows 只可以作为 WSL2 的宿主系统，LWA 必须安装和运行在 WSL2 Linux 发行版内部。WSL1、未知操作系统和未纳入支持矩阵的发行版/架构均不承诺可用。
>
> **现状缺口（落地前）**：`platform_detect.py` 能识别 Windows，但 CLI 无统一门禁；文档/setup 曾暗示 Windows 原生；Linux 安装脚本仅接受 Ubuntu。
>
> **状态**：主路径已落地（2026-07-20，DEV-081）。PlatformSupportReport + CLI/服务门禁、Debian apt、WSL2/systemd/`/mnt` fail-closed、文档去 Windows 原生推销已完成。**可后续补强**：036.08 实机验收清单、036.09 无效 Windows 运行分支清理。

### 16.1 依赖分析与最低版本结论

LWA 自身是 Python 应用，但平台下限应由完整功能链中最严格的依赖决定：

| 依赖/能力 | 当前项目门槛 | 上游约束与结论 |
| --- | --- | --- |
| Python | `>=3.13` | Python 3.13 的 macOS 官方安装器最低仅需 10.13，但这不是完整产品下限；Docker Desktop / Node 24 更严格。[Python 3.13 发布页](https://www.python.org/downloads/release/python-3130/) |
| Node.js | `>=24.0.0`（仅前端 SPA 构建） | Node 24 官方 GNU/Linux x64/arm64 Tier 1 要求 kernel ≥4.18、glibc ≥2.28；macOS x64/arm64 要求 ≥13.5。它决定不支持 32 位 Linux 正式路径。[Node 24 BUILDING](https://github.com/nodejs/node/blob/v24.x/BUILDING.md) |
| Docker Engine / Compose | `>=29.0.0` / `>=2.40.2` | Docker 官方当前支持 Ubuntu 22.04/24.04/26.04 LTS 等和 Debian 11/12/13；LWA 取更窄、可维护的 LTS/Stable 子集。[Ubuntu](https://docs.docker.com/engine/install/ubuntu/) / [Debian](https://docs.docker.com/engine/install/debian/) |
| Caddy | `>=2.10.0` | 官方提供 Debian/Ubuntu/Raspbian apt 路径及多架构二进制；不单独抬高下述发行版基线。[Caddy 安装文档](https://caddyserver.com/docs/install) |
| 自启动 | launchd / systemd user | Linux/WSL 必须有可用 systemd user manager；WSL 必须让 systemd 成为 PID 1。微软给出的 systemd 最低 WSL 为 0.67.6，但 Docker Desktop WSL 后端要求 ≥2.1.5，故产品统一采用更高门槛 2.1.5。[微软 systemd](https://learn.microsoft.com/windows/wsl/systemd) / [Docker WSL](https://docs.docker.com/desktop/features/wsl/) |
| macOS Docker | Docker Desktop | Docker Desktop 支持当前及前两个 macOS 大版本，并同时提供 Apple silicon 与 Intel 安装包；最低 4GB RAM。[Docker Desktop Mac](https://docs.docker.com/desktop/setup/install/mac-install/) |
| macOS 安装器 | Homebrew | 当前 Tier 1 同时覆盖 Apple Silicon 与 Intel x86_64，但 Homebrew 已说明 Intel 支持窗口将在后续 macOS 周期结束，不能永久承诺 Intel。[Homebrew Support Tiers](https://docs.brew.sh/Support-Tiers) |

**结论**：需要最低版本要求，并采用“固定能力下限 + 滚动上游支持窗口”组合。Linux kernel 只写 Node 的绝对下限 4.18 不够稳健；LWA 的 Docker 29、systemd、自启动和长期维护统一采用 **kernel ≥5.15** 的产品基线。

### 16.2 正式支持矩阵

| 运行环境 | 最低要求 | 正式架构 | 说明 |
| --- | --- | --- | --- |
| **Ubuntu 裸机** | Ubuntu **22.04 LTS+**；Linux kernel **5.15+**；glibc **2.35+**；systemd 可用 | `x86_64/amd64`、`arm64/aarch64` | 只承诺官方 Ubuntu LTS，不承诺 Mint 等衍生版；当前与未来 LTS 须仍在厂商标准支持期 |
| **Debian 裸机** | Debian **12 (Bookworm)+**；Linux kernel **5.15+**；glibc **2.35+**；systemd 可用 | `x86_64/amd64`、`arm64/aarch64` | 当前安装脚本尚拒绝 Debian；WBS-036.05 完成且 WBS-036.08 Debian 实机闭环通过前，不得对外宣称 Debian 已落地支持 |
| **WSL2 Linux** | WSL **2**；WSL 包版本 **2.1.5+**；默认 Microsoft WSL kernel **5.15+**；发行版为 Ubuntu 22.04 LTS+ 或 Debian 12+；systemd 为 PID 1 | 与发行版一致的 x86_64/arm64 | WSL1 不支持；工作区应放 Linux 文件系统（如 `~/lwa`），不放 `/mnt/c`；若用 Docker Desktop WSL integration，不得再在发行版内并装第二套 Engine |
| **macOS** | Docker Desktop 所支持的“当前及前两个 macOS 大版本”；**截至 2026-07 为 macOS 14 Sonoma+**；4GB RAM 最低、8GB+ 推荐 | Apple silicon `arm64`、Intel `x86_64` | **不强制 M 系列芯片**。Apple silicon 为推荐和主要验收架构；Intel 仅在 Python/Node/Docker/Homebrew 仍提供官方支持的窗口内承诺，随上游退役同步收缩 |

统一要求：Python 3.13+；FastAPI 0.138.0+；Uvicorn 0.45.0+。容器能力还要求 Docker 29.0.0+、Compose 2.40.2+；Caddy 模式要求 Caddy 2.10.0+；前端 SPA 本机构建要求 Node 24+。缺少可选能力时可进入明确 degraded/pending，但操作系统、架构和 Python 基线不满足时必须 fail-fast。

**不支持清单**：

- Windows 10/11/Server 原生 Python 进程（包括 PowerShell、CMD、Windows Terminal 中直接运行 `lwa`）；
- WSL1；
- 32 位 Linux、ARMv7/ARMv6，以及未列入的 ppc64le/s390x/riscv64 等架构；
- Alpine/musl、CentOS/RHEL/Fedora、Arch、openSUSE、Linux Mint/Kali 等未纳入实机矩阵的发行版；
- Hackintosh、OpenCore Legacy Patcher、macOS 虚拟机作为正式验收环境；
- 低于上述版本、厂商已 EOL 或上游依赖已经停止支持的系统。

### 16.3 运行时门禁设计

新增统一的 `PlatformSupportReport`（建议放 `platform_detect.py` 或独立 `platform_support.py`），至少包含：

```text
platform, distroId, distroVersion, kernelVersion, libcVersion,
architecture, wslVersion, systemdAvailable, supported, reasons, action
```

门禁分层：

1. **允许导入**：任何平台都可以 import 包，便于构建文档、读取版本和运行模拟测试；禁止在模块 import 时 `sys.exit()`。
2. **CLI 统一门禁**：除 `--help` / `version` 外，所有实际命令在读取/创建工作区、写 registry、启动子进程之前调用 `require_supported_platform()`。
3. **服务直入口门禁**：`manager_service`、`daemon`、`gateway_service` 的 `run_service_main()` 同样调用门禁，防止 `python -m ...` 绕过 CLI。
4. **Windows 原生 hard fail**：返回非零退出码，中文提示“Windows 原生不受支持；请在 WSL2 的 Ubuntu/Debian 中安装运行”，不得继续创建目录、写配置、探测 Windows Docker named pipe 或生成任务计划。
5. **WSL2 单独识别**：`detect_platform()==wsl` 视为支持候选，不因宿主为 Windows 而阻断；继续检查 WSL2、发行版、kernel、systemd 和工作区路径。
6. **未知/过低版本 fail-closed**：无法证明满足硬基线时不执行写操作，输出检测字段和修复建议；`doctor --json` 可只读输出完整报告供排障。
7. **滚动 macOS 下限**：代码不永久硬编码“14”；运行时常量的权威更新来源是 release checklist，每次发布根据 Docker Desktop 当前+前两版策略刷新常量与测试夹具，并记录当次下限。文档保留“截至日期”的快照。

### 16.4 Linux / WSL 安装器一致性

1. 将当前 `detect_ubuntu()` 抽象为 Debian-family 发行版检测，严格识别 `ID=ubuntu|debian` 和版本下限。
2. Docker apt 源按发行版选择 `/linux/ubuntu` 或 `/linux/debian`，不能把 Debian 伪装成 Ubuntu codename。
3. Caddy Cloudsmith 源可复用 Debian/Ubuntu 官方路径，但仍需验证 systemd、架构、Caddy owner 与工作区权限。
4. WSL 中先识别 Docker 后端：
   - Docker Desktop WSL integration 已可用：复用它，不安装发行版内 Engine；
   - 无 Desktop integration 且用户确认：才在发行版内安装 Docker Engine；
   - 两套同时存在视为冲突，Full Profile 不得假绿。
5. 检测 `/mnt/<drive>` 工作区并给出明确风险；本期可允许只读诊断，但 Full/autostart 正式路径要求工作区位于 Linux 文件系统。

### 16.5 可执行 WBS

| WBS | 优先级 | 任务 | 主要触点 | 完成定义 |
| --- | --- | --- | --- | --- |
| **036.01** | P0 | 先写平台矩阵与 fail-fast 失败测试 | `tests/test_platform_detect.py`（新建）、CLI/service tests | Windows 原生、WSL1、旧 kernel/发行版/架构先红；支持矩阵样例通过 |
| **036.02a** | P0 | 实现 `PlatformSupportReport` 与版本比较 | `platform_detect.py` 或新 `platform_support.py` | Ubuntu/Debian/WSL/macOS/Windows/unknown 输出稳定结构与中文 action；macOS 滚动下限由 release checklist 刷新常量和测试夹具 |
| **036.02b** | P0 | 将完整平台报告接入只读 doctor JSON | `doctor.py`、CLI、API/serialization tests | `doctor --json` 稳定输出全部检测事实、reasons、action 与 supported，不因 unsupported 先退出或缺字段 |
| **036.03** | P0 | CLI 统一门禁 | `cli/__init__.py`、`cli/_common.py` | 除 help/version/只读 platform doctor 外，unsupported 在任何工作区写入前退出非零 |
| **036.04** | P0 | 服务直接入口门禁 | `manager_service.py`、`daemon.py`、`gateway_service.py` | Windows 原生或 unsupported 无法绕过 CLI 启动后台服务 |
| **036.07** | P0 | 与门禁同迭代清理 Windows 对外承诺和安装提示 | README、`docs/faq.md`、`docs/autostart.md`、`docs/known-limitations.md`、`setup.py`、Skills | 036.03/04 合入时同步删除原生 Windows 安装、任务计划、自启、快捷键和“部分支持”表述；保留且明确 WSL2 宿主说明，不允许出现“代码已拒绝、文档仍推销”的中间发布 |
| **036.05** | P0 | Linux 安装脚本支持 Ubuntu 22.04+ / Debian 12+ | `install-docker-linux.sh`、`install-caddy-linux.sh`、`host_bootstrap.py` | apt 源按发行版正确选择；不支持发行版明确拒绝；shell 回归覆盖两个家族 |
| **036.06a** | P0 | WSL 识别、包版本与 systemd 门禁 | `platform_detect.py`、`autostart.py` | WSL1、WSL 包过旧/unknown、kernel 过低、systemd 关闭均给出确定状态与修复建议；WSL2 不被 Windows 门禁误杀 |
| **036.06b** | P0 | WSL Docker 后端识别与双 Engine 冲突检查 | `platform_detect.py`、`host_bootstrap.py` | 区分 Desktop integration 与发行版内 Engine；已有 Desktop 时不重复安装，两套并存时 Full 不得假绿 |
| **036.06c** | P0 | WSL 工作区路径与 autostart 策略 | paths/workspace、`autostart.py`、`host_bootstrap.py` | `/mnt/<drive>` 可只读诊断，但 Full/autostart 写路径 fail-closed 并提示迁移到 Linux 文件系统 |
| **036.08** | P0 | 平台实机验收矩阵 | release/acceptance checklist | Ubuntu 22.04/24.04、Debian 12、WSL2 Ubuntu、macOS arm64 全闭环；macOS Intel 在仍承诺时至少 smoke |
| **036.09** | P1 | 删除或隔离无效 Windows 运行分支 | `daemon.py`、`manager_service.py`、`static_gateway.py`、tests | 先门禁后清理；测试通过 monkeypatch 注入平台事实，不依赖真实宿主；只保留通用 subprocess 工具真正需要的分支，不再形成支持暗示 |

推荐执行顺序：**036.01 → 036.02a/02b → 036.03/04**，并将 **036.07 与 036.03/04 放在同一迭代、同一发布门槛**；随后执行 **036.05（Debian 宣称阻断）→ 036.06a/06b/06c → 036.08 → 036.09**。IMP-035 与 IMP-036 无硬依赖，可并行推进。

### 16.6 验收标准

1. 原生 Windows 执行任意实际 `lwa` 命令，在创建工作区、registry、日志或子进程前退出非零，并提示改用 WSL2 Ubuntu/Debian。
2. WSL2 不被误判为 Windows；满足 WSL 2.1.5+、发行版、kernel、systemd 和工作区要求时正常运行。
3. Ubuntu 22.04/24.04 与 Debian 12 的 setup、init、manager、daemon、gateway、Docker/Caddy Full 闭环通过；Debian apt 源不引用 Ubuntu。
4. kernel <5.15、glibc <2.35、不支持架构、WSL1、macOS 低于滚动下限均在写操作前 fail-fast。
5. macOS Apple silicon 完成主验收；Intel x86_64 在当前上游仍支持时可安装 Python 3.13、Node 24、Docker Desktop 并通过 smoke，不要求 M 系列芯片。
6. `doctor --json` 能输出完整平台检测事实和建议动作；错误信息不只给“unsupported”单词。
7. 全仓用户文档不再宣称或暗示 Windows 原生可用；所有 Windows 宿主说明都明确指向 WSL2 Linux 内运行。
8. 发布清单每次复核并刷新 Docker Desktop 当前+前两版 macOS 下限常量与测试夹具、Docker Ubuntu/Debian 支持列表和 Intel 上游状态；Intel 仍对外承诺时必须保留 smoke 项，避免静态最低版本随时间失真。

### 16.7 风险与边界

| 风险 | 处理 |
| --- | --- |
| “Linux”范围过宽导致无法验收 | 正式范围只含 Ubuntu LTS 与 Debian Stable 指定下限；其他发行版明确不支持 |
| WSL 是 Windows 宿主，被 Windows 门禁误杀 | 先执行 WSL 识别；Linux kernel + WSL 标志命中后走 WSL2 矩阵，不走 Windows native 分支 |
| WSL 包版本在禁用 interop 时难读取 | 报告 `wslVersion=unknown`，结合 kernel/systemd 只读诊断；写操作 fail-closed，并提示在 Windows 侧执行 `wsl --version` |
| macOS 大版本每年漂移 | 使用“当前+前两版”策略 + 发布清单更新常量；文档快照带“截至 2026-07” |
| Intel Mac 上游即将退役 | 不要求 M 系列，但承诺以 Python/Node/Docker/Homebrew 同时支持为前提；任何一项退役即在下个版本降级/移除 Intel 支持 |
| 门禁妨碍跨平台测试 | import 不退出；所有门禁接受注入/monkeypatch 的平台事实；测试不得依赖真实宿主系统 |
| Windows 文档与代码门禁错位 | 036.07 与 036.03/04 同迭代、同发布门槛；不得发布“代码已拒绝但文档仍提供原生安装/任务计划”的版本 |
| Debian 名义支持但安装器仍只支持 Ubuntu | 036.05 加 036.08 Debian 实机闭环是正式宣称支持前的发布阻断项，不允许仅改文档即标完成 |

### 16.8 task-list 编号映射

| task-list | 关系 |
| --- | --- |
| `PLN-017` | IMP-036 支持矩阵、最低版本、门禁与 WBS 规划 |
| `DOC-050` / `DOC-051` | 本文 §15/§16 初始写入及 CHK-090 契约、WBS、口径复审修订 |
| `DEV-081` | IMP-036 主开发项（WBS-036.01～07 / 036.05 / 036.06 已完成；036.08/036.09 可后续补） |


## 17. `design/achievement/` 功能反查结论（2026-07-20）

### 17.1 审计口径

本轮逐份检查 `design/achievement/` 下 13 份 Markdown 文档（另有一个非文档 `.DS_Store`），并与当前源码、测试、`task-list.md` 交叉核对。收录规则为：

1. 收录文档明确声明的“部分实现”、“能力缺口”、“未本轮”、“后续 P1”或者已定义目标态但代码尚未闭环的功能。
2. 不重复收录已有代码+测试+task-list 完成记录的历史事故项。
3. 不收录文档明确列为“非目标 / V1 不交付”的功能，也不把 V1.1/V2 纯路线图畅想自动转成当前承诺。
4. 历史文档的过时“待修复”状态不作为现状证据；以当前代码、回归测试和最新 task-list 为准。

### 17.2 文档级判定

| achievement 文档 | 当前判定 | 处理 |
| --- | --- | --- |
| `2026-07-10-logo-svg-design.md` | 3 个 logo SVG 均已存在 | 不新增 |
| `2026-07-10-lettermark-svg-design.md` | 3 个 lettermark SVG 均已存在 | 不新增 |
| `local-webpage-access-analysis-20260707.md` | 别名依赖、SPA 子路径、CLI/importer 拆分、Vue 迁移、跨进程构建门禁已完成；仍留“进行中构建无法中止”；“update 不拉源码 / Vite 开发端口元数据”已明确不纳入本轮 | 取消缺口归 IMP-039；后两项已删出范围 |
| `local-webpage-access-gateway-switch-access-review-20260709.md` | G1/G2/G3/G5/G6、BUG-102、C1/C2/I1/I2 已完成；I3/I4 仍明确为后续 P1；I5 因 Windows 原生已不支持而失效 | I3 归 IMP-037；I4 归 IMP-038；I5 关闭为过时边界 |
| `local-webpage-access-runtime-analysis-20260707.md` / `local-webpage-access-imp010-021-plan-20260707.md` | IMP-010～021 已有完成记录和回归 | 不新增 |
| `local-webpage-access-caddy-startup-diagnostic-report-20260708.md` / `local-webpage-access-caddy-startup-incident-20260708.md` / `local-webpage-access-startup-failure-20260708.md` | BUG-069～071、IMP-010/020、自愈、自启动均已落地 | 不新增 |
| `local-webpage-access-整改与开发WBS-20260708.md` | 文首已声明当时全部落地；DEV-041～061 可追溯 | 不新增 |
| `待改进功能-WBS-20260706.md` | IMP-001/005/006/007/008/009 已完成 | 不新增 |
| `local-webpage-access-v1-design-20260704.md` / `local-webpage-access-v1-wbs-20260704.md` | V1 交付项已完成；构建取消仅作“预留”，并未实现进行中抢占中止；V1.1/V2 与“不交付”按本轮口径排除 | 取消缺口归 IMP-039；V1 预留的源码拉取/Vite 端口元数据不纳入 |

### 17.3 待开发功能总览

| IMP | 建议优先级 | 功能 | 主要来源 | 现状 |
| --- | --- | --- | --- | --- |
| **IMP-038** | **P0（下迭代优先）** | `lwa update` 后访问地址刷新、访问复核与 Skill/doctor 闭环 | gateway review I4 | **已落地**（DEV-083，2026-07-20） |
| **IMP-039** | **P0（下迭代优先）** | 进行中构建的可控取消 | analysis §2.3 / V1 WBS-20.08 | **已落地**（DEV-084，2026-07-20）：queued/building 取消、进程树终止、CLI/API/管理页入口 |
| **IMP-040** | **P0（与 038 同批）** | 管理页/状态 DTO 的 LAN 地址新鲜度与漂移自愈 | 用户反馈 2026-07-20：换 LAN IP 后点「端口」仍开旧地址 | **已落地**（DEV-087，2026-07-20） |
| **IMP-041** | **P0（与 BUG-268 同批，可插队）** | 删除/purge 阶段日志 + 容器路径别名清理 | CHK-094 / 用户反馈；BUG-268 | **已落地**（DEV-088，2026-07-20；BUG-268 已关） |
| **IMP-037** | **P1** | 网关后端原子切换与 manifest/registry 一致性 | gateway review I3/G4 | **已落地**（DEV-082，2026-07-20）：`lwa gateway switch` / `POST /api/gateway/switch`；事务回滚与 degraded；manifest/registry 批量回写 |

> **已从本计划删除（2026-07-20）**：原 IMP-040 `lwa update --pull`、原「Vite `sourceDevPort`」条目——价值偏低 / 非刚需，不再排期（对应 DEV-085/086 关闭）。**IMP-041 编号已复用于**「删除路径阶段日志与别名清理」（§22），勿与已删的 Vite 元数据混淆。

### 17.4 优先级评审结论（2026-07-20，修订）

按 **用户痛点 × 事故面 × 复用底座 / 实现成本** 排序。

| 排序 | IMP | 结论 | 理由 |
| --- | --- | --- | --- |
| 1 | **041** | **做（可插队，与 BUG-268 同批）** | 实机删除后别名 502 + 日志过简；改动面小、风险高路径，宜尽快修。 |
| 2 | **038** | **做** | 升级后 URL 漂移；底座已齐，主要是 updater/doctor/Skill 接线。 |
| 3 | **040** | **做** | 管理页「端口」链到落盘旧 `lanUrl`；与 038 共享 refresh。 |
| 4 | **039** | **做** | 长构建无法取消；进程树实现更重。 |
| 5 | **037** | **值得做，别插队** | 双向切换频率低；手改 YAML + `gateway on/off` 可撑。 |

**推荐落地顺序**：`041（+BUG-268）→ 038 + 040 → 039 → 037`。

## 18. IMP-037 — 网关后端原子切换与状态一致性

> **状态**：**已落地**（2026-07-20，DEV-082）
>
> **建议优先级**：**P1**（在 IMP-038/040/039 之后；见 §17.4）
>
> **来源**：`local-webpage-access-gateway-switch-access-review-20260709.md` §10.3 I3，以及 G4 的“manifest.static.gateway / 别名入口 / pageviews 日志源与真实后端一致”目标。
>
> **现状**：用户仍需手工编辑 `local-web.yml.staticGateway`，再分别执行 `gateway on/off`。`start_gateway()` 能停残留 builtin、启 Caddy、刷新 URL 和写事件，但没有统一 `switch_backend` 事务，也没有批量修正已有 manifest/registry 中的有效 gateway 事实。

### 18.1 目标与非目标

**目标**：

1. 提供单一命令 `lwa gateway switch <caddy|builtin>`，不再要求用户手改 YAML 后猜测操作顺序。
2. 将预检、停旧后端、写配置、启新后端、重建站点/别名片段、刷新 URL、回写 manifest/registry、access review 和审计事件收敛为一个可测事务。
3. 切到 builtin 时保留别名元数据，但明确标记别名路由未激活；切回 Caddy 时可按 manifest 重建片段。
4. 任一阶段失败都不得留下“YAML 说 builtin、进程跑 Caddy、manifest 说 caddy”的混合态。

**非目标**：

- 不自动改应用源码里的 SPA `base`；继续交给 `access review` / IMP-023。
- 不引入 Nginx/Traefik 第三后端。
- 不删除别名元数据，除非用户显式清除别名。

### 18.2 原子性与错误契约

| 阶段 | 要求 |
| --- | --- |
| 预检 | 验证目标后端、Caddy 能力、端口独占、当前实例运行态；输出变更摘要 |
| 事务快照 | 备份 `local-web.yml`、受影响 manifest 网关字段、gateway state 和 Caddy 主配置/片段清单 |
| 切换 | 严格停旧再启新；同一 hostPort 不允许 builtin/Caddy 重叠监听 |
| 状态收口 | 对每个实例回写“实际生效后端”，registry 与 manifest 同步；地址刷新后再 access review |
| 失败回滚 | 新后端未就绪时恢复旧 YAML/片段/运行态；回滚也失败则标为 `degraded` 并给出确定修复命令 |

### 18.3 可执行 WBS

| WBS | 优先级 | 任务 | 主要触点 | 完成定义 |
| --- | --- | --- | --- | --- |
| **037.01** | P0 | 先写双向切换与回滚失败测试 | 新建 `tests/test_gateway_switch.py` | caddy→builtin、builtin→caddy、无 Caddy、端口冲突、回滚失败先红 |
| **037.02** | P0 | 定义 `GatewaySwitchPlan/Result` 与快照 | 新建 `gateway_switch.py`、`models.py` | dry-run 可列出实例、进程、片段和状态变更，不写盘 |
| **037.03** | P0 | 实现停旧→改配置→启新→回滚事务 | `gateway_switch.py`、`gateway_service.py`、`static_gateway.py` | 同端口不双开；中途失败恢复旧后端；事件记录每阶段 |
| **037.04** | P0 | 批量同步 manifest/registry 网关事实 | `models.py`、`registry/dao.py`、`access.py` | 运行/停止静态实例均与实际后端一致；别名激活态可区分 |
| **037.05** | P1 | 增加 CLI/API 及幂等语义 | `cli/gateway.py`、`manager_api.py` | `switch` 支持 `--dry-run` / JSON；重复切到当前后端不重启、不破坏状态 |
| **037.06** | P1 | 切换收尾地址刷新与访问复核 | `access.py`、`gateway_switch.py` | URL 已刷新；review 失败不伪装切换成功，报告区分后端与应用风险 |
| **037.07** | P1 | 文档与实机验收 | `docs/operations-playbook.md`、`docs/acceptance-checklist.md` | macOS/Linux 双向切换，含 running/stopped/别名实例闭环 |

### 18.4 验收标准

1. 用户无需手改 YAML 即可双向切换。
2. 切换后配置、进程、manifest、registry、站点/别名片段和访问 URL 一致。
3. 任一注入失败都能回滚，或进入可解释的 degraded 态，不得假绿。
4. 别名元数据在 builtin 时保留但不宣称可用，切回 Caddy 后可恢复。

### 18.5 task-list 编号映射

| task-list | 关系 |
| --- | --- |
| `PLN-018` | IMP-037 规划 |
| `DEV-082` | IMP-037 开发主项 |


## 19. IMP-038 — 升级后访问地址与可用性复核闭环

> **建议优先级**：**P0（下迭代优先）**（见 §17.4）
>
> **来源**：gateway review §10.3 I4：`lwa update` 不自动 `access refresh`，没有 access-review Skill，doctor 也没有可选深度 access 复核入口。
>
> **现状**：`lwa gateway on` 会 refresh+review，但 `lwa update` 在 builtin 或未重启 gateway 时不保证刷新 URL；现有内置 Skills 无专门的访问链路复核流程。

### 19.1 目标与边界

1. `lwa update` 在工作区和 registry 可用时固定执行 `access refresh`，且在 manager/daemon/gateway 重启完成后执行，避免又被旧进程回写。
2. 新增 `--review-access/--no-review-access`，默认做轻量回环与声明 URL 复核，慢或外网失败不与包安装失败混为一类。
3. doctor 增加显式 `--access`，复用 `review_access()` 而不重写一套探测。
4. 新增 `lwa-review-access-urls` Skill，将 refresh→review→定位端口/网关/SPA→可选 rebuild 变成可重复运维流程。
5. 不把 DHCP 漂移解读为必须 rebuild；仍只有 IMP-023 空 200 可建议重建。

### 19.2 可执行 WBS

| WBS | 优先级 | 任务 | 主要触点 | 完成定义 |
| --- | --- | --- | --- | --- |
| **038.01** | P0 | 先写 updater 步骤顺序和错误分类失败测试 | `tests/test_updater.py` | 后台重启后才 refresh；refresh/review 结果分步记录 |
| **038.02** | P0 | 把 access refresh/review 接入 updater | `updater.py`、`cli/system.py` | report/JSON 增 `access_refresh` / `access_review`；dry-run 绝不写 registry/manifest |
| **038.03** | P1 | doctor `--access` 委托现有探测 | `cli/system.py`、`doctor.py`、`access.py` | human/JSON 字段稳定；不重复计算逻辑 |
| **038.04** | P1 | 新增 access review 内置 Skill | `skills/lwa-review-access-urls/SKILL.md`、`skills/README.md` | 包含安全读操作、错误分层、`--rebuild-if-needed` 的确认边界 |
| **038.05** | P1 | 去重 gateway/update/doctor 的后处理编排 | 新建 `access_workflow.py` 或等价抽象 | 三个入口共用相同结果模型、超时与退出码契约 |
| **038.06** | P1 | 文档、打包和端到端验收 | README、operations/FAQ、packaging tests | 升级后管理页 URL 不漂移；新 Skill 能被 init/update 同步 |

### 19.3 验收标准

1. 模拟 LAN IP 变化后执行 `lwa update`，所有实例的 `lanUrl/routeUrl` 已更新。
2. `--dry-run` 不发起网络探测、不改 manifest/registry。
3. `doctor --access --json`、`lwa access review --json`、update report 对同一实例给出一致的诊断。
4. Skill 说明默认只读，自动 rebuild 必须由显式开关触发。

### 19.4 task-list 编号映射

| task-list | 关系 |
| --- | --- |
| `PLN-019` | IMP-038 规划 |
| `DEV-083` | IMP-038 开发主项 |


## 20. IMP-039 — 进行中构建的可控取消

> **建议优先级**：**P0（下迭代优先）**（见 §17.4；实现复杂度高于 038）
>
> **来源**：`local-webpage-access-analysis-20260707.md` §2.3 和 V1 WBS-20.08。
>
> **现状**：`BuildQueue.cancel()` 已能让排队任务在拿到槽位后跳过，并对已进入 `building` 的 `npm/pip/docker compose build` 走 `cancelling → cancelled|cancel_failed` 杀进程树；CLI `lwa cancel-build`、API `/cancel-build`、管理页「取消构建」已接通（IMP-039 / DEV-084）。

### 20.1 取消契约

1. 状态区分 `queued → cancelled` 和 `building → cancelling → cancelled|cancel_failed`，不得取消请求一来就假报已停。
2. 构建子进程必须以独立进程组/会话运行；取消先温和终止，超时后强制终止完整进程树。
3. 持久化 owner PID/process identity、build ID 和取消时间；管理进程重启后不得对 PID 复用的无关进程发信号。
4. 取消后关闭日志句柄、释放跨进程槽位、收尾 builds 行，实例不留在 building。
5. 不自动删除构建缓存、旧镜像或用户数据；取消只停止当前工作。

### 20.2 可执行 WBS

| WBS | 优先级 | 任务 | 主要触点 | 完成定义 |
| --- | --- | --- | --- | --- |
| **039.01** | P0 | 先写 queued/running/竞态/进程重启取消测试 | `tests/test_build_queue.py`、lifecycle tests | 排队取消、进行中取消、PID 复用、取消与正常完成竞态先红 |
| **039.02** | P0 | 扩展跨进程构建任务持久化 | `build_queue.py`、build-locks DB | 持久化 build ID、owner PID/start time、process group、状态和 cancel request |
| **039.03** | P0 | 将构建执行改为可中止子进程组 | `docker_runtime.py`、`hosting.py`、command runner | POSIX 可 TERM→KILL 整棵树；超时和返回码可解释 |
| **039.04** | P0 | 实现幂等 `cancel_build(instance_id)` | `build_queue.py`、`lifecycle.py` | 首次请求发起取消；重复请求返回相同最终态；已完成任务不被篡改 |
| **039.05** | P1 | 增加 CLI/API/管理页入口 | `cli/lifecycle.py`、`manager_api.py`、manager static | building/queued 显示取消；cancelling 禁止其他生命周期操作 |
| **039.06** | P0 | 保证状态、日志和槽位收尾 | registry/builds/events/logging | 取消后无槽位泄漏、无孤儿进程、无 building 残留 |
| **039.07** | P1 | 端到端验收与文档 | tests/e2e、manager/FAQ | 真实长时构建可取消，之后可重新 rebuild |

### 20.3 验收标准

1. queued 任务取消后永不调用 builder。
2. building 任务在取消超时内退出，其后无子进程、无槽位泄漏。
3. 取消成功和取消失败在 CLI/API/UI 中有不同结果，不假报。
4. 管理进程崩溃后可回收任务，且不会终止 PID 复用的无关进程。

### 20.4 task-list 编号映射

| task-list | 关系 |
| --- | --- |
| `PLN-020` | IMP-039 规划 |
| `DEV-084` | IMP-039 开发主项 |

## 21. IMP-040 — 管理页 LAN 地址新鲜度与漂移自愈

> **建议优先级**：**P0（与 IMP-038 同批）**（见 §17.4）
>
> **来源**：用户反馈（2026-07-20）——主机局域网 IP 变化后，管理页点击「端口」仍打开旧 `lanUrl`；本机「本机」链接可用，但局域网分享/手机访问失效。`doctor.check_lan_url_stale` 仅 WARN 并提示手动 `lwa access refresh`，管理页 15s 轮询只重读落盘字段，不会重算 IP。
>
> **现状证据**：
> - `status._resolve_lan_url` / `_resolve_route`：**只读** `manifest.network.lanUrl|routeUrl`，不调用 `resolve_lan_ip`。
> - 前端 `helpers.js` → `LWA.urlHtml`：「端口」=`href={i.lanUrl}`；「本机」=`localhostUrl`（现算，故同机仍可用）。
> - 写盘刷新仅发生在 `lwa access refresh`、`gateway on` finalize、以及 start/enable/alias 时的 `build_network_entry`；**daemon / manager / `lwa update` 均不刷新**。

### 21.1 问题陈述

`lanUrl` / `routeUrl` 是 **生命周期事件写入的持久化快照**，不是读时现算。换 Wi-Fi、有线/无线切换、DHCP 续约后：

1. 管理页列表仍展示并链到旧 IP；
2. CLI `lwa status` / 导出的地址同样陈旧；
3. 用户若不跑 doctor 或不知道 `access refresh`，会认为「服务挂了」。

### 21.2 目标与非目标

**目标**：

1. 管理页（及同等 DTO）在 `lanIpStrategy=auto` 时，**展示与可点击链接始终对应当前 LAN IP**（或合法回退），不依赖用户记得手动 refresh。
2. 发现漂移后 **节流写回** 各实例 `local-web.json`（及别名 `routeUrl`），使 CLI/文件真相与 UI 一致。
3. 明确 **检查/刷新阶段**（见 §21.4），并与 IMP-038（update 收尾 refresh）共用同一套探测与结果模型。
4. `lanIpStrategy=manual` 时 **不自动改写**；仅提示当前探测 IP 与 `manualLanIp` 不一致。

**非目标**：

- 不监听 OS 网络变更事件（跨平台复杂，收益有限）。
- 不修正用户浏览器书签/外部文档里的旧 URL。
- 不把 DHCP 漂移当成必须 rebuild（仍归 IMP-023 / access review）。
- 不恢复已删除的 `update --pull` / Vite 开发端口元数据。

### 21.3 方案选型（已选 A）

| 方案 | 做法 | 优点 | 缺点 |
| --- | --- | --- | --- |
| **A. 读时现算 + 节流落盘（推荐）** | API/status 用当前 `resolve_lan_ip` + `hostPort`/`alias` **合成** `lanUrl`/`routeUrl`；若与落盘 host 不一致则节流调用 `refresh_network_entries` | 用户点「端口」**立刻**正确；落盘随后自愈；复用现有 refresh | 需小心 detect 成本与写盘节流 |
| B. 仅后台落盘 | daemon/manager 周期比对后 refresh；DTO 仍读 manifest | 改动面小 | 在下一次 refresh 完成前链接仍坏；轮询窗口内假死 |
| C. 前端自拼 | API 下发 `currentLanIp` + `hostPort`，前端拼 href | 前端可控 | 易与 routeUrl/别名/https 分叉；CLI/status 仍旧 |

**选定 A**：对「点端口开旧地址」是最小闭环；B 作为 A 的落盘侧实现细节保留；C 仅作可选增强（页眉展示当前 LAN）。

### 21.4 检查与刷新阶段（契约）

| 阶段 | 触发点 | 做什么 | 写盘？ |
| --- | --- | --- | --- |
| **R1 读时合成（P0）** | `InstanceStatus` / `GET /api/instances` / detail | `auto`：用 `resolve_lan_ip(config)` + `hostPort` 生成 `lanUrl`；`routeMode=name` 时同步合成 `routeUrl`（host 换新、path 保留）。DTO 增 `currentLanIp`、`lanUrlSource=live|manifest|manual`。`manual`：继续用配置的 manual IP 合成，不静默改。 | 否 |
| **R2 旁路比对（P0）** | manager 处理 instances 列表（或 `/api/health`）时 | 内存缓存 `lastResolvedLanIp`；若与本次探测不同 → 标记 drift | 否（只记标志） |
| **R3 节流落盘（P0）** | R2 发现 drift 后 | 调用已有 `refresh_network_entries`；**同一进程内最短间隔**（建议默认 60s，可配）；并发只跑一次（单飞锁） | 是 |
| **R4 daemon 周期（P1）** | daemon 已有 tick | 同样做 R2→R3，保证无管理页打开时也能自愈（CLI/手机书签文件层） | 条件写 |
| **R5 显式运维（已有）** | `lwa access refresh`、`gateway on` | 立即全量 refresh | 是 |
| **R6 升级收尾（IMP-038）** | `lwa update` 重启后台之后 | 固定 refresh（+ 可选 review） | 是 |
| **R7 诊断（增强）** | `doctor` / `doctor --json` | 保留 `lan_url_stale`；JSON 增加 `currentLanIp`、`driftedInstanceIds`；管理页可在 drift 时出非阻断提示条 +「立即刷新地址」按钮（调 R5 API） | 按钮触发时写 |

**探测成本**：`detect_lan_ip` 单次 UDP 很轻；列表接口内每请求最多探测一次（进程内短 TTL 缓存，建议 5～15s），避免 N 实例 × N 探测。

### 21.5 数据与 API 契约

1. **DTO 字段（向后兼容）**  
   - 继续返回 `lanUrl` / `routeUrl` / `localhostUrl`（语义变为「当前应打开的地址」）。  
   - 新增可选：`currentLanIp`、`persistedLanIp`（从旧 lanUrl 解析）、`lanAddressStale: bool`。  
2. **管理页**  
   - 「端口」继续绑 `lanUrl`（R1 后自然正确）。  
   - 当 `lanAddressStale` 为 true 时，页眉/横幅提示「检测到局域网地址已变化，正在同步…」；提供手动「刷新访问地址」→ `POST /api/access/refresh`（薄封装 `refresh_network_entries`）。  
3. **错误**  
   - 探测失败：回退 `127.0.0.1` 或保留上次成功 IP，并在 DTO/`doctor` 标明 `lanIpUnknown`；**不得**用错误探测结果批量写坏所有 manifest。  
4. **与 pageviews / 别名**  
   - 落盘 refresh 后别名片段无需因纯 IP 变化而 rebuild 站点（host 在 URL 展示层）；若 Caddy 配置写死旧 IP（当前一般不），需在 refresh 报告中列出。

### 21.6 可执行 WBS

| WBS | 优先级 | 任务 | 主要触点 | 完成定义 |
| --- | --- | --- | --- | --- |
| **040.01** | P0 | 先写「落盘旧 IP / 探测新 IP」下 DTO 链接正确与节流写盘测试 | `tests/test_status.py`、`test_manager_api.py`、access tests | 旧 manifest + mock 新 IP → API `lanUrl` 已是新地址；60s 内二次列表不重复全量写盘 |
| **040.02** | P0 | status/DTO 读时合成 `lanUrl`/`routeUrl` | `status.py`、`ports.py` | `_resolve_lan_url` 不再盲信落盘；`manual` 路径单测覆盖 |
| **040.03** | P0 | manager 旁路 drift 检测 + 单飞节流 `refresh_network_entries` | `manager_api.py` / `manager_service.py`、`access.py` | 换 IP 后首次列表触发落盘；并发列表只 refresh 一次 |
| **040.04** | P1 | daemon 周期复用同一 helper | `daemon.py` | 无管理页时文件层也会自愈 |
| **040.05** | P1 | `POST /api/access/refresh` + 前端 stale 横幅/按钮 | `manager_api.py`、`manager_static/*` | 手动一键与自动路径结果一致 |
| **040.06** | P1 | doctor JSON 字段与文档 | `doctor.py`、faq/manager-page/operations | 说明各阶段；与 IMP-038 交叉引用 |
| **040.07** | P0 | 与 IMP-038 共享 refresh 编排，避免双份逻辑 | `access_workflow.py` 或等价 | update/manager/daemon/CLI 同一结果模型 |

### 21.7 验收标准

1. 将本机 LAN IP 从 A 改到 B（或 mock `resolve_lan_ip`）后，**无需**手动 `access refresh`，管理页「端口」href 已是 `http://B:<port>`（最多一次列表轮询延迟）。
2. 随后各实例 `local-web.json` 的 `lanUrl`/`routeUrl` 在节流窗口内被写为 B；`doctor` 不再 WARN `lan_url_stale`。
3. `lanIpStrategy=manual` 时不自动覆盖；UI/doctor 提示探测值与配置值差异。
4. 探测失败不批量写坏 manifest；`localhostUrl` 始终可用。
5. 15s 轮询在 IP 未变时不额外打满磁盘写。

### 21.8 风险与边界

| 风险 | 缓解 |
| --- | --- |
| 多网卡 / VPN 导致 `detect_lan_ip`「跳变」 | 保持现有 UDP 出口策略；跳变则跟出口走；文档说明可用 `manual` |
| 列表接口延迟 | IP 探测短 TTL 缓存；refresh 异步/后台线程，响应先返回 live URL |
| 与 IMP-038 重复 | 040.07 强制抽取共享编排；038 负责 update 时机，040 负责常驻自愈与读时正确 |
| WSL `/mnt` 与镜像网络 | 不改变平台门禁；仅刷新 URL 字符串 |

### 21.9 task-list 编号映射

| task-list | 关系 |
| --- | --- |
| `PLN-024` | IMP-040（LAN 新鲜度）规划 |
| `DEV-087` | IMP-040 开发主项 |
| `DEV-085` / `DEV-086` | 原 update --pull / Vite 端口 —— **已关闭（移出范围）** |

## 22. IMP-041 — 删除路径阶段日志与容器别名清理（含 BUG-268）

> **建议优先级**：**P0（可插队；与 BUG-268 同批修复）**（见 §17.4）
>
> **来源**：CHK-094 实机复核删除 `prd-workflow`；用户确认需补删除阶段可观测性，并与别名残留一并修。
>
> **与既有项关系**：IMP-034 已覆盖分文件落盘与构建/启动心跳，**未覆盖** remove/purge；IMP-035 已落地删除 UI/契约，本项补「删完之后人能对账、网关不留幽灵路由」。关闭 **BUG-268**。

### 22.1 问题陈述

实机彻底删除容器实例后：

1. `manager.log` 往往只有一句 `已移除（含磁盘文件）`；stop / down / 清 pageviews / 清别名无过程 INFO；中间 `suppress(Exception)` 失败可静默。
2. registry 中 stop 等事件挂在 `instance_id` 上，随 `delete_instance` **CASCADE 消失**，审计只剩一条 orphan `remove`（BUG-047 设计如此，但阶段细节丢失）。
3. uvicorn `access_log=False`，无法从日志还原 `purge` / `force` / 409 `data_nonempty` 请求路径。
4. **BUG-268**：`remove` → `stop_container` 不调用 `StaticGateway.remove_alias_config`；`aliases/<id>.conf` 与 `Caddyfile` import 残留，别名 URL 返回 502。

### 22.2 目标与非目标

**目标**：

1. `remove_instance`（及管理页/CLI 入口）对关键阶段输出可 grep 的 **INFO/WARNING**，写入 `manager.log` / `lwa.log`。
2. 阶段结果同时写入 **orphan registry events**（`instance_id=NULL`，message 含实例 ID 与 stage），避免 CASCADE 后只剩一句。
3. 修复 BUG-268：任意 runtime 的 remove/purge 均清理路径别名片段并 `_sync_main_config` + best-effort reload。
4. 管理页对 **破坏性 API**（单实例 remove、批量冗余删除）打一行无 token 的审计日志。

**非目标**：

- 不恢复全站 uvicorn access_log。
- 不上集中式日志栈；不把 DEBUG 刷满磁盘。
- 不记录 Authorization / token / `.env` 密钥。
- 不在本项重做 IMP-035 交互；不改 `data_nonempty` 契约。

### 22.3 阶段契约（日志 + orphan event）

统一 message 形态（便于 grep）：

`remove stage=<name> instance=<id> purge=<bool> force=<bool> result=<ok|skip|warn|fail> detail=...`

| stage | 何时 | result 语义 |
| --- | --- | --- |
| `begin` | 通过 data_nonempty 门禁后、开始清理前 | ok |
| `data_guard` | 触发 `DataNonemptyError`（未 force） | fail（抛错前记 orphan event + WARNING） |
| `stop` | `stop_instance` 结束 | ok / warn（继续清理） |
| `compose_down` | docker-compose 的 `down` | ok / skip（非容器）/ warn |
| `alias_cleanup` | 删除 aliases 片段并同步 Caddyfile | ok / skip（本无别名）/ warn（reload 失败但文件已删） |
| `pageviews_clear` | `clear_instance_pageviews` | ok / warn |
| `registry_delete` | `delete_instance` 前后 | ok |
| `purge_tree` | `rmtree(apps/<id>)` | ok / skip（非 purge）/ fail |
| `done` | 全部完成 | ok（替代或补充现有「已移除…」一句，须含 purge/force） |

规则：

- 现有收尾 `log.info("实例 … 已移除…")` **保留语义**，可并入 `done` 或紧随 `done`。
- 原 `contextlib.suppress` 改为：捕获后 `log.warning` + orphan event `result=warn`，再继续（除非安全上必须中止）。
- orphan `remove` 总览事件（现有 BUG-047）保留；阶段事件用 `event_type=remove_stage`（或 `lifecycle_stage` + message 含 `op=remove`），实现时二选一并写进文档/测试，避免随意字符串。

### 22.4 BUG-268 修复要点

1. 在 `remove_instance` 中（stop/down 之后、删 registry 之前或之后均可，建议 **删盘前**）对**所有** runtime 调用 `StaticGateway(workspace, config).remove_alias_config(instance_id)`（或抽出 `cleanup_instance_gateway_routes`），再 `_sync_main_config()` + best-effort `reload_all()`。
2. 即使 manifest 缺失 / 已 stop 失败，仍 best-effort 清别名（按 instance_id 文件名），避免幽灵 import。
3. 单测：容器实例带 `routeHost` → remove/purge → `aliases/<id>.conf` 不存在且主 Caddyfile 无对应 import；静态实例回归仍走 `disable` 路径不双重报错。
4. 实机验收：清理当前 runtime 残留 `aliases/prd-workflow.conf`（一次性运维或随修复后的 reconcile 命令；至少在测试/文档写明手动删除 + reload）。

### 22.5 破坏性 API 审计（P1）

在 `manager_api` 处理 `POST /api/instances/{id}/remove`（及批量 redundant remove）时：

```
audit remove instance=<id> purge=<bool> force=<bool> status=<http> code=<error_code|ok>
```

- 不写 token；可写 `request_id`（若已有）或短随机 correlation id 与阶段日志对齐。
- 409 `data_nonempty`、403、404 均应有一行，便于对照前端双阶段确认。

### 22.6 可执行 WBS

| WBS | 优先级 | 任务 | 主要触点 | 完成定义 |
| --- | --- | --- | --- | --- |
| **041.01** | P0 | 先写：容器带别名 remove 后别名文件与 Caddyfile 无残留；阶段 orphan events 条数/字段 | `tests/test_lifecycle.py`、gateway tests | BUG-268 与阶段事件先红 |
| **041.02** | P0 | `remove_instance` 阶段 INFO/WARNING + orphan events | `lifecycle.py` | 覆盖 §22.3 各 stage；suppress 改为可观测 warn |
| **041.03** | P0 | 全 runtime 别名清理 + sync/reload | `lifecycle.py`、`static_gateway.py`、`hosting.py` | 关闭 BUG-268；静态路径无回归 |
| **041.04** | P1 | 管理页破坏性 API 审计一行 | `manager_api.py` | remove/批量删除可见 status/code |
| **041.05** | P1 | FAQ/manager-page：删除后看哪些日志；排障地图补一行 | `docs/faq.md`、`docs/manager-page.md` | 症状「别名 502 / 删了但日志只有一句」有索引 |
| **041.06** | P0 | 实机：清 `prd-workflow` 残留别名并 reload；再跑一次 purge 样例核对日志 | runtime / acceptance | `/prd-workflow/` 不再 502；manager.log 含多 stage |

### 22.7 验收标准

1. 删除（仅移除 / 彻底删除）后，`manager.log`（或 CLI 时 `lwa.log`）能按时间序看到至少 `begin` → `stop`/`alias_cleanup` → `registry_delete` → `done`。
2. `events` 在实例行删除后仍能查到该次删除的 orphan 总览 + 阶段事件；message 含实例 ID。
3. 带路径别名的容器实例 purge 后：无 `aliases/<id>.conf`、主配置无悬空 import、别名 URL 非 502（期望 404 或网关默认未匹配行为）。
4. `data_nonempty` 拒绝时有 WARNING + orphan event，且不删盘。
5. 安全：日志/events 无 API token。

### 22.8 task-list 编号映射

| task-list | 关系 |
| --- | --- |
| `PLN-025` | IMP-041 规划 |
| `DEV-088` | IMP-041 开发主项（含关闭 BUG-268） |
| `BUG-268` | 别名残留 —— 由本项 041.03 关闭 |
| `CHK-094` | 发现问题的实机复核（已完成） |
