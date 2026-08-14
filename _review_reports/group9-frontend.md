# LWA 管理页前端代码审查报告（group9-frontend）

- 审查人：代码审查子代理（只读审查）
- 日期：2026-07-17
- 审查范围：`src/local_webpage_access/manager_static/` 下 index.html、boot.js、helpers.js、app.js（1447 行，逐段读完）、style.css（920 行）；`vue.esm-browser.prod.js` 为第三方 vendored 库按约定跳过。
- 后端对照：`manager_api.py` 全部路由与错误格式、`models.py` Status 枚举、`status.py` snapshot `to_dict`、`pageviews.py` `summary/detail`、`capability.py` 能力字段、`lifecycle.py` FallbackConfirmationRequired、`stats.py` HostResources 字段。
- 测试验证：`python -m pytest tests/test_manager_static_app.py -q` → **23 项全部通过**（未跑 test_manager_api.py，避免与父进程全量套件冲突；契约核对改用源码比对）。

## 1) 每个文件一行总结

| 文件 | 职责 | 疑点 |
|---|---|---|
| `index.html` | 页面骨架 + importmap 引入 Vue | 未发现明显问题 |
| `boot.js` | 3 行胶水：createApp → mount(#app) | 未发现明显问题 |
| `helpers.js` | 纯渲染函数（转义/徽章/操作按钮/删除状态机） | 2 项防御性小疑点（见 #8、#9） |
| `app.js` | Vue 3 根组件：token/轮询/弹窗/操作 | 1 项 major + 5 项 minor 疑点（见 #1、#3~#7） |
| `style.css` | 全部视觉样式 | 2 项 CSS 变量/类名错配（见 #1、#2） |

## 2) 发现清单

### [严重度: major]

**#1. `app.js:1413-1434`（对照 `style.css`）C.R03「部署降级确认」弹窗无任何样式——CSS 类名错配**
- 说明：fallbackDialog 模板使用 `.modal-card` / `.modal-header` / `.modal-body` / `.modal-footer`，而 style.css 只为 `.modal` / `.modal-inner` / `.modal-head` 定义样式，这四个类**完全不存在**。弹窗内容直接渲染在 50% 半透明黑遮罩上：无面板背景、无内边距、无 max-width，正文以 `--text`（浅色主题下近黑）压在深色遮罩上，对比度极差、几乎不可读。
- 触发条件：`start` 返回 `pendingConfirmation`（top-1 候选失败且存在等价 fallback）时弹出；这是 C.R03 降级确认的核心流程。
- 影响：该弹窗功能可用（按钮可点）但视觉损坏，用户难以阅读 primaryFailure 与候选列表；深色主题下同样对比不足。
- 证据：`grep "modal-card|modal-header|modal-body|modal-footer" style.css` 零命中；app.js 中其它 6 个弹窗全部用 `.modal-inner`（已定义样式），唯独此弹窗用了另一套类名。
- 建议修法：给 `.modal-card` 等补样式（`background: var(--panel); border-radius: var(--radius); max-width: 560px; padding: 16px;`，并排版 `.modal-header/.modal-body/.modal-footer`），或直接复用 `.modal-inner` / `.modal-head`。

### [严重度: minor]

**#2. `style.css:611` `var(--font-mono)` 未定义——CSS 变量引用错误**
- 说明：`:root`（style.css:39）只定义了 `--monospace`，`.path-alias-field input` 却写 `font-family: var(--font-mono)`（无 fallback）。按 CSS 规范该声明在计算值阶段失效，回退继承字体。路径别名输入框不显示等宽字体。
- 证据：`grep --font-mono` 全库仅此一处引用，无定义；`--monospace` 在 :39、:102、:834 正常使用。
- 建议修法：改为 `var(--monospace)`。

**#3. `app.js:89-98`（结合 `app.js:604`）token 失效后轮询不停止 → 每 15s 重复弹 toast**
- 说明：`apiFetch` 收到 401（非 loopback）时清 token + toast + 800ms 后调 `requireToken`，但 `bootstrap` 建立的 `_timer` 未清理，`refresh()` 继续每 15s 发起 3 个请求，每个都 401 → 每轮弹 3 次相同 toast（toast 3s 计时器被反复重置，持续可见），直到用户输入新 token。
- 触发条件：局域网访问且 token 过期/被自动轮换后停留在页面。
- 影响：UX 噪声 + 无谓请求；功能不受损。
- 建议修法：401 时 `clearInterval(this._timer)`，提交新 token 成功后由 `bootstrap` 重建轮询。

**#4. `app.js:608-621` refresh() 三个并行 fetch 无竞态令牌——并发刷新互相覆盖**
- 说明：`/api/stats`、`/api/pageviews`、`/api/instances` 每轮并行发出，跨轮次响应乱序时，旧一轮的慢响应可覆盖新一轮已写入的新数据（`/api/instances` 后端还会做 `sync_status` + `maybe_throttled_lan_refresh`，可能明显慢）。详情(`_detailReq`)和浏览量(`_pageviewReq`)已有令牌，唯独列表刷新没有。
- 影响：极端情况下列表/统计短暂回退到上一轮快照，最长 15s，下一轮自愈；不崩溃。
- 建议修法：给 refresh 加递增序号，只应用最新一次的响应。

**#5. `app.js:981-990` fetchLogs 无请求令牌——类别切换竞态**
- 说明：快速切换 run→build 时，先发的 run 请求若后返回，会覆盖 build 内容。
- 影响：日志弹窗瞬时显示错误类别的旧内容。
- 建议修法：加请求序号，或写入前校验 `logs.category` 仍与请求一致。

**#6. `app.js:1145-1157` Escape 键分支漏掉 fallbackDialog——降级确认弹窗无法用 Esc 关闭**
- 说明：`onKeydown` 的 Escape 链覆盖 removeDialog/folderImport/pageview/pathAlias/logs/drawer，唯独没有 fallbackDialog；且该弹窗也没有 Tab 焦点陷阱（removeDialog 有 `_trapRemoveDialogFocus`）。
- 影响：弹窗声明了 `role="dialog" aria-modal="true"`，用户按 Esc 无反应，只能点 ✕ 或「取消」；键盘可达性缺口。
- 建议修法：Escape 链中加入 `fallbackDialog`，并补焦点约束。

**#7. `app.js:706-716` cancel-build 的 `cancel_failed` 成功分支是死代码**
- 说明：后端 `cancel_build_op` 在 outcome=="cancel_failed" 时直接抛 409（manager_api.py:1115-1125），前端 `.then` 里的 `outcome === "cancel_failed"` 分支永远不可达，实际走 catch 的「取消构建失败」。
- 影响：无功能 bug，仅冗余且具误导性。
- 建议修法：删除该分支，统一由 catch 处理。

**#8. `helpers.js:13-20` 与 `helpers.js:113-118`（防御性）esc() 不转义单引号；badgeHtml 的 class 未转义**
- 说明：`LWA.esc` 未转义 `'`；`badgeHtml` 把 `String(status)` 直接拼进 `class="badge <cls>"`。当前 status 受后端 Status 枚举（models.py:57-72）约束不含引号，且属性均用双引号包裹，**暂不可利用**；但属转义不完整。
- 建议修法：esc 增加 `'` → `&#39;`；badge 的 cls 过一遍 esc（低优先）。

**#9. `helpers.js:275-279`（信息性）`starting`/`stopping`/`removing` 状态为死检查**
- 说明：后端 Status 枚举不存在这三个值，removeBusy 的这些分支永不命中（tests/test_manager_static_app.py 特意传入这些值断言禁用，属前瞻性代码）。
- 影响：无。仅标注，便于未来新增状态时知晓。

### 未确认为 bug 的观察（信息性，标注不确定）

- `app.js:48-68` getToken 优先返回 sessionStorage 旧 token，URL 中更新的 `?token=` 被忽略且 URL 未清理；依赖 401 自愈。低影响。
- `app.js:59-63` replaceState 清理 URL token 时会丢失原 URL 的 `#hash`。极轻微。
- `app.js:616-630` 同轮 stats 失败但 instances 成功时，`loadError` 清空/设置的先后顺序不定；仅影响空表提示文案。极轻微。
- `style.css:756-758` toast kind="info" 无专属样式（仅 .toast-error/.toast-success），info 回退基础样式。极轻微。

## 3) 明确声明（未发现问题）

- **XSS：未发现可利用漏洞。** 所有用户/实例数据进入 HTML 均经 `LWA.esc`（属性/文本）或 Vue `{{ }}` 插值（自动转义）；`v-html` 注入的 `rowHtml`/`drawer.body`/`pageview.body` 内容全部由转义函数构造，href/title/aria-label 均为双引号包裹 + esc。
- **token 处理：** 存储（sessionStorage）、URL 传入清理、401 失效行为整体正确；唯一问题见 #3。
- **字段名一致性：** 前端引用的 camelCase 字段（lanUrl/localhostUrl/routeUrl/routeHost/lanAddressStale/currentLanIp/lastCpuPercent/lastMemoryBytes/portMappingLabel/desiredState/servingMode/observedState/observationError/runtimeAccess/`host.memTotalBytes`/`autoStart.action`/`startedAt` 等）与后端全部一致，builds/events 还做了 snake_case 兜底。
- **除零/空数据：** `maxHits` 有 0 保护、`byDay.length` 守卫、`counts.x || 0` 兜底齐全，未发现渲染崩溃路径。
- **URL 拼接：** id/category 均 `encodeURIComponent`，无拼接错误。
- **内存泄漏：** setInterval 在重进 bootstrap 时先清旧、unmount 时清理，document 级监听对称增删，未发现泄漏。
- **index.html / boot.js：未发现明显问题。**（boot.js 依赖 app.js 先注册 `LWA.createManagerApp`，模块脚本延迟执行保证顺序；importmap 路径与 vendored 文件一致。）

## 4) 备注

- 遵守硬约束，全程只读 + 跑既有测试，未修改/创建任何业务文件（本报告文件除外）。
- 因原任务硬约束「不改任何文件」，未执行 AGENTS.md/CLAUDE.md 约定的 `task-list.md` 会话同步；如需记录本次审查条目请父代理按需处理。
