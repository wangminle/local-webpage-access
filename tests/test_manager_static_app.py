"""管理页前端脚本测试（DEV-046 Vue 3 重构后）。

DEV-046 把纯渲染函数抽到 ``helpers.js``（``window.LWA`` / ``__LWA_TEST_HOOKS__``），
``app.js`` 改为 Vue 3 工厂（``createManagerApp``，依赖注入）。本测试：

* 在 Node vm 中加载 helpers.js，验证纯函数输出（无 DOM/Vue 依赖）；
* 加载 app.js，用桩 createApp + 桩 deps 构造组件，验证响应式结构与方法挂接
  （冒烟测试：模板/数据/方法齐全，不真正挂载）。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "local_webpage_access" / "manager_static"
HELPERS_JS = STATIC / "helpers.js"
APP_JS = STATIC / "app.js"
STYLE_CSS = STATIC / "style.css"


# 共享的 vm 上下文骨架（window/document/location 等桩）
_VM_PRELUDE = """
const fs = require("node:fs");
const vm = require("node:vm");
"""


def _run(script: str, env: dict[str, str] | None = None) -> None:
    run_env = {**os.environ, **(env or {})}
    subprocess.run(["node", "-e", _VM_PRELUDE + script], check=True, env=run_env)


def _load_helpers_body() -> str:
    return f'fs.readFileSync({str(HELPERS_JS)!r}, "utf8")'


def _load_app_body() -> str:
    return f'fs.readFileSync({str(APP_JS)!r}, "utf8")'


def _css_rule(css: str, selector: str) -> str:
    """返回简单选择器对应的声明块。"""
    marker = selector + " {"
    start = css.index(marker) + len(marker)
    return css[start : css.index("}", start)]


def test_instance_table_matches_v069_layout_except_two_line_name_clamp() -> None:
    """BUG-414：完整恢复 V0.6.9 列宽规则，仅保留名称两行省略。"""
    css = STYLE_CSS.read_text(encoding="utf-8")
    app_js = APP_JS.read_text(encoding="utf-8")
    table_rule = _css_rule(css, "table.instances")
    name_cell_rule = _css_rule(css, ".cell-name")
    name_button_rule = _css_rule(css, ".cell-name-btn")
    ops_column_rule = _css_rule(css, ".col-ops")
    name_cell_declarations = {line.strip() for line in name_cell_rule.splitlines()}
    name_button_declarations = {line.strip() for line in name_button_rule.splitlines()}

    assert "table-layout: fixed" not in table_rule
    assert ".col-name" not in css
    assert "col-name" not in app_js
    assert "max-width: 220px" in name_cell_rule
    assert not any(line.startswith("width:") for line in name_cell_declarations)
    assert not any(line.startswith("min-width:") for line in name_cell_declarations)
    assert "min-width: 200px" in ops_column_rule
    assert "width: 240px" not in ops_column_rule
    assert "display: -webkit-box" in name_button_rule
    assert "max-width: 100%" in name_button_rule
    assert not any(line.startswith("width:") for line in name_button_declarations)
    assert "-webkit-line-clamp: 2" in name_button_rule
    assert "-webkit-box-orient: vertical" in name_button_rule
    assert "white-space: normal" in name_button_rule
    assert "overflow-wrap: anywhere" in name_button_rule
    assert "white-space: nowrap" not in name_button_rule


def test_helpers_format_local_date_time() -> None:
    """更新时间一律换算到本机时区显示（带时区的 ISO 也要转本地，而非保留源时区）。"""
    # 固定本机时区，避免测试机时区差异导致断言漂移
    _run(
        f"""
const assert = require("node:assert");
const context = {{
  window: {{ __LWA_TEST_HOOKS__: {{}} }},
  console: console,
}};
vm.runInNewContext({_load_helpers_body()}, context);
const f = context.window.__LWA_TEST_HOOKS__.formatLocalDateTime;
// UTC 时间换算到本地（+08:00）
assert.strictEqual(f("2026-07-15T02:00:00+00:00"), "2026-07-15 10:00:00(UTC+8)");
assert.strictEqual(f("2026-07-15T02:00:00Z"), "2026-07-15 10:00:00(UTC+8)");
// 源时区即本地时区，时间不变
assert.strictEqual(f("2026-07-07T10:08:00+08:00"), "2026-07-07 10:08:00(UTC+8)");
// 源时区非本地需换算：02:38+05:30 == 05:08+08:00
assert.strictEqual(f("2026-07-07T02:38:00+05:30"), "2026-07-07 05:08:00(UTC+8)");
assert.strictEqual(f(""), "—");
""",
        env={"TZ": "Asia/Shanghai"},
    )


def test_helpers_status_badges_and_actionable() -> None:
    """DEV-043：gateway_down/config_invalid 应有中文徽章与连字符 class。"""
    _run(
        f"""
const assert = require("node:assert");
const context = {{ window: {{ __LWA_TEST_HOOKS__: {{}} }}, console: console }};
vm.runInNewContext({_load_helpers_body()}, context);
const h = context.window.__LWA_TEST_HOOKS__;

assert.strictEqual(h.statusLabel("gateway_down"), "网关不可达");
assert.strictEqual(h.statusLabel("config_invalid"), "配置无效");
assert.strictEqual(h.statusLabel("running"), "运行中");

const gw = h.badgeHtml("gateway_down");
assert.ok(gw.indexOf("badge-gateway-down") !== -1, gw);
assert.ok(gw.indexOf("网关不可达") !== -1, gw);
const cfg = h.badgeHtml("config_invalid");
assert.ok(cfg.indexOf("badge-config-invalid") !== -1, cfg);

assert.strictEqual(h.isActionableStatus("pending"), true);
assert.strictEqual(h.isActionableStatus("failed"), true);
assert.strictEqual(h.isActionableStatus("gateway_down"), true);
assert.strictEqual(h.isActionableStatus("config_invalid"), true);
assert.strictEqual(h.isActionableStatus("running"), false);
assert.strictEqual(h.isActionableStatus("stopped"), false);
"""
    )


def test_helpers_applyfilters_with_explicit_state() -> None:
    """IMP-019：applyFilters(rows, filters) 支持文本搜索/状态/形态/仅冗余/仅待处理。"""
    _run(
        f"""
const assert = require("node:assert");
const context = {{ window: {{ __LWA_TEST_HOOKS__: {{}} }}, console: console }};
vm.runInNewContext({_load_helpers_body()}, context);
const apply = context.window.__LWA_TEST_HOOKS__.applyFilters;

const rows = [
  {{ id: "alpha", name: "Alpha", status: "running", servingMode: "shared-static", runtime: "shared-static", kind: "static", stack: ["vite"], routeHost: "alpha", redundant: false }},
  {{ id: "beta", name: "Beta", status: "stopped", servingMode: "container", runtime: "docker-compose", kind: "python", stack: ["fastapi"], routeHost: "", redundant: true }},
  {{ id: "gamma", name: "Gamma", status: "failed", servingMode: "shared-static", runtime: "shared-static", kind: "static", stack: [], routeHost: "", redundant: true }}
];
function ids(r) {{ return r.map(function (x) {{ return x.id; }}); }}
const all = {{ search: "", status: "", form: "", pending: false, redundant: false }};

assert.deepStrictEqual(ids(apply(rows, all)), ["alpha", "beta", "gamma"]);
assert.deepStrictEqual(ids(apply(rows, {{ search: "beta" }})), ["beta"]);
assert.deepStrictEqual(ids(apply(rows, {{ status: "running" }})), ["alpha"]);
assert.deepStrictEqual(ids(apply(rows, {{ form: "container" }})), ["beta"]);
assert.deepStrictEqual(ids(apply(rows, {{ redundant: true }})), ["beta", "gamma"]);
assert.deepStrictEqual(ids(apply(rows, {{ pending: true }})), ["gamma"]);
assert.deepStrictEqual(ids(apply(rows, {{ status: "stopped", redundant: true }})), ["beta"]);
"""
    )


def test_helpers_opshtml_enables_path_alias_for_container() -> None:
    """BUG-085：容器实例路径别名按钮应可用；进行中态禁用。"""
    _run(
        f"""
const assert = require("node:assert");
const context = {{ window: {{ __LWA_TEST_HOOKS__: {{}} }}, console: console }};
vm.runInNewContext({_load_helpers_body()}, context);
const opsHtml = context.window.__LWA_TEST_HOOKS__.opsHtml;

function btn(html, op) {{
  var i = html.indexOf('data-op="' + op + '"');
  assert.ok(i !== -1, "missing op=" + op);
  return html.slice(i, html.indexOf(">", i) + 1);
}}

var container = opsHtml({{ id: "api", name: "api", status: "running", runtime: "docker-compose", servingMode: "container", stack: ["fastapi"], redundant: false }});
assert.ok(btn(container, "path-alias").indexOf("disabled") === -1);

var stat = opsHtml({{ id: "demo", name: "demo", status: "running", runtime: "shared-static", servingMode: "shared-static", stack: [], redundant: false }});
assert.ok(btn(stat, "path-alias").indexOf("disabled") === -1);

var pending = opsHtml({{ id: "p", name: "p", status: "pending", runtime: "docker-compose", servingMode: "container", stack: [], redundant: false }});
assert.ok(btn(pending, "path-alias").indexOf("disabled") !== -1);
assert.ok(btn(pending, "start").indexOf("disabled") !== -1, "真·pending 仍禁用启动");

var stopped = opsHtml({{ id: "s", name: "s", status: "stopped", runtime: "shared-static", servingMode: "shared-static", stack: [], redundant: false }});
assert.ok(btn(stopped, "start").indexOf("disabled") === -1, "识别成功后的 stopped 必须可启动");
"""
    )


def test_helpers_opshtml_allows_update_from_dir_when_pending() -> None:
    """BUG-445：folder 源 pending 时「从源更新」须可用，启动仍禁用。"""
    _run(
        f"""
const assert = require("node:assert");
const context = {{ window: {{ __LWA_TEST_HOOKS__: {{}} }}, console: console }};
vm.runInNewContext({_load_helpers_body()}, context);
const opsHtml = context.window.__LWA_TEST_HOOKS__.opsHtml;

function btn(html, op) {{
  var i = html.indexOf('data-op="' + op + '"');
  assert.ok(i !== -1, "missing op=" + op);
  return html.slice(i, html.indexOf(">", i) + 1);
}}

var pendingFolder = opsHtml({{
  id: "pf", name: "pf", status: "pending",
  runtime: "shared-static", servingMode: "shared-static", stack: [],
  redundant: false, sourceKind: "folder"
}});
assert.ok(btn(pendingFolder, "update-from-dir").indexOf("disabled") === -1,
  "folder+pending 须允许从源更新以自愈");
assert.ok(btn(pendingFolder, "start").indexOf("disabled") !== -1,
  "pending 启动仍须禁用");

var buildingFolder = opsHtml({{
  id: "bf", name: "bf", status: "building",
  runtime: "docker-compose", servingMode: "container", stack: [],
  redundant: false, sourceKind: "folder"
}});
assert.ok(btn(buildingFolder, "update-from-dir").indexOf("disabled") !== -1,
  "building 时仍须禁用从源更新");
"""
    )


def test_helpers_rowhtml_includes_pageview_cell() -> None:
    """DEV-061：整行 HTML 应包含浏览量单元格，且点击按钮带 data-op=pageview。"""
    _run(
        f"""
const assert = require("node:assert");
const context = {{ window: {{ __LWA_TEST_HOOKS__: {{}} }}, console: console }};
vm.runInNewContext({_load_helpers_body()}, context);
const h = context.window.__LWA_TEST_HOOKS__;

var html = h.rowHtml({{ id: "demo", name: "demo", status: "running", runtime: "shared-static", servingMode: "shared-static", stack: [], updatedAt: "2026-07-09T10:00:00+08:00", redundant: false }}, {{ demo: {{ hits: 42, uniqueIps: 3, source: "builtin" }} }});
assert.ok(html.indexOf("pageview-btn") !== -1, html);
assert.ok(html.indexOf("data-op=\\"pageview\\"") !== -1, html);
assert.ok(html.indexOf(">42<") !== -1, html);

// 无浏览量数据 → 占位横线
var html2 = h.rowHtml({{ id: "x", name: "x", status: "stopped", runtime: "docker-compose", servingMode: "container", stack: [], redundant: false }}, {{}});
assert.ok(html2.indexOf("cell-muted") !== -1, html2);
"""
    )


def test_helpers_rowhtml_detail_entry_is_keyboard_accessible() -> None:
    """BUG-170：详情入口须为可聚焦按钮（非仅 click 的 td）。"""
    _run(
        f"""
const assert = require("node:assert");
const context = {{ window: {{ __LWA_TEST_HOOKS__: {{}} }}, console: console }};
vm.runInNewContext({_load_helpers_body()}, context);
const h = context.window.__LWA_TEST_HOOKS__;
var html = h.rowHtml({{ id: "demo", name: "演示站", status: "running", runtime: "shared-static", servingMode: "shared-static", stack: [], redundant: false }}, {{}});
assert.ok(html.indexOf("<button") !== -1, html);
assert.ok(html.indexOf('data-detail="demo"') !== -1, html);
assert.ok(html.indexOf("aria-label") !== -1, html);
assert.ok(html.indexOf('type="button"') !== -1, html);
"""
    )


def test_vue_template_has_basic_a11y_hooks() -> None:
    """BUG-170：筛选控件、图标按钮与 toast 须有基础无障碍属性。"""
    _run(
        f"""
const assert = require("node:assert");
const context = {{
  window: {{ __LWA_TEST_HOOKS__: {{}}, LWA: undefined }},
  document: null,
  fetch: function () {{ throw new Error("no fetch"); }},
  location: {{ hostname: "127.0.0.1", search: "", pathname: "/" }},
  sessionStorage: {{ getItem: function () {{ return null; }}, setItem: function () {{}}, removeItem: function () {{}} }},
  history: {{ replaceState: function () {{}} }},
  setInterval: function () {{ return 0; }},
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  URLSearchParams: URLSearchParams,
  console: console,
}};
vm.runInNewContext({_load_helpers_body()}, context);
vm.runInNewContext({_load_app_body()}, context);
let capturedRoot = null;
context.window.LWA.createManagerApp(
  {{ createApp: function (root) {{ capturedRoot = root; return {{ mount: function () {{}} }}; }} }},
  {{
    document: null,
    fetch: function () {{ throw new Error("no fetch"); }},
    location: context.location,
    sessionStorage: context.sessionStorage,
    history: context.history,
    setInterval: function () {{ return 0; }},
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    URLSearchParams: URLSearchParams,
  }}
);
const t = capturedRoot.template;
assert.ok(t.indexOf('aria-label="搜索实例"') !== -1 || t.indexOf("aria-label='搜索实例'") !== -1, t);
assert.ok(t.indexOf('aria-label="按状态筛选"') !== -1, t);
assert.ok(t.indexOf('aria-label="按形态筛选"') !== -1, t);
assert.ok(t.indexOf('aria-live="polite"') !== -1, t);
assert.ok(t.indexOf('aria-label="立即刷新"') !== -1, t);
assert.ok(t.indexOf('aria-label="关闭详情"') !== -1, t);
"""
    )


def test_vue_app_factory_builds_valid_root_component() -> None:
    """DEV-046：createManagerApp 用桩 createApp 构造组件，结构与方法齐全（冒烟）。

    不真正挂载（无 DOM/Vue 运行时），仅验证：
    * 工厂返回 {app, mount}；
    * 根组件含 data/methods/computed/template；
    * 关键方法（onTableClick/refresh/openPageview 等）已挂接；
    * tbodyHtml 计算属性复用 helpers.rowHtml 输出（含浏览量列）。
    """
    _run(
        f"""
const assert = require("node:assert");

const context = {{
  window: {{ __LWA_TEST_HOOKS__: {{}}, LWA: undefined }},
  document: null,
  fetch: function () {{ throw new Error("no fetch"); }},
  location: {{ hostname: "127.0.0.1", search: "", pathname: "/" }},
  sessionStorage: {{ getItem: function () {{ return null; }}, setItem: function () {{}}, removeItem: function () {{}} }},
  history: {{ replaceState: function () {{}} }},
  setInterval: function () {{ return 0; }},
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  URLSearchParams: URLSearchParams,
  console: console,
}};
// 先加载 helpers.js（定义 window.LWA），再加载 app.js（定义 createManagerApp）
vm.runInNewContext({_load_helpers_body()}, context);
vm.runInNewContext({_load_app_body()}, context);
const createManagerApp = context.window.LWA.createManagerApp;
assert.strictEqual(typeof createManagerApp, "function");

let capturedRoot = null;
const stubApp = {{ mount: function (el) {{ return {{ el: el }}; }} }};
const handle = createManagerApp(
  {{ createApp: function (root) {{ capturedRoot = root; return stubApp; }} }},
  {{
    document: null,
    fetch: function () {{ throw new Error("no fetch"); }},
    location: context.location,
    sessionStorage: context.sessionStorage,
    history: context.history,
    setInterval: function () {{ return 0; }},
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    URLSearchParams: URLSearchParams,
  }}
);
assert.strictEqual(typeof handle.mount, "function");
assert.ok(capturedRoot, "createApp 应收到根组件");
assert.strictEqual(typeof capturedRoot.data, "function");
assert.strictEqual(typeof capturedRoot.template, "string");
assert.ok(capturedRoot.template.indexOf("v-html=\\"tbodyHtml\\"") !== -1, "模板应含表格 v-html");
assert.ok(capturedRoot.template.indexOf("importmap") === -1);

// 方法挂接
["refresh", "onTableClick", "openDetail", "openLogs", "openPathAlias", "openPageview", "removeRedundant", "submitToken", "openFolderImport", "pickFolder", "canPickFolder"].forEach(function (m) {{
  assert.strictEqual(typeof capturedRoot.methods[m], "function", "缺方法 " + m);
}});

// data 初值
const state = capturedRoot.data();
assert.ok(Array.isArray(state.instances));
assert.strictEqual(state.filters.search, "");
assert.strictEqual(state.pageview.open, false);
assert.strictEqual(state.folderImport.picking, false);

// IMP-051：模板含选择文件夹与 path-with-browse
assert.ok(capturedRoot.template.indexOf("选择文件夹") !== -1, "模板应含选择文件夹按钮");
assert.ok(capturedRoot.template.indexOf("path-with-browse") !== -1);
assert.ok(capturedRoot.template.indexOf("pickFolder") !== -1);
// loopback location → canPickFolder true（闭包读 deps.location）
assert.strictEqual(capturedRoot.methods.canPickFolder.call({{}}), true);

// computed.filteredInstances 走 helpers.applyFilters
const ctx = {{ instances: [
  {{ id: "a", name: "A", status: "running", servingMode: "shared-static", stack: [] }},
  {{ id: "b", name: "B", status: "stopped", servingMode: "container", stack: [], redundant: true }}
], filters: {{ search: "", status: "", form: "", pending: false, redundant: true }} }};
const filtered = capturedRoot.computed.filteredInstances.call(ctx);
assert.deepStrictEqual(filtered.map(function (x) {{ return x.id; }}), ["b"]);

// computed.tbodyHtml 复用 rowHtml（含浏览量列 13 列）
const tbodyCtx = {{
  instances: [{{ id: "a", name: "A", status: "running", runtime: "shared-static", servingMode: "shared-static", stack: [], redundant: false }}],
  filteredInstances: [{{ id: "a", name: "A", status: "running", runtime: "shared-static", servingMode: "shared-static", stack: [], redundant: false }}],
  pageviewMap: {{ a: {{ hits: 5 }} }},
}};
const html = capturedRoot.computed.tbodyHtml.call(tbodyCtx);
assert.ok(html.indexOf("pageview-btn") !== -1, html);
assert.ok(html.indexOf("data-op=\\"pageview\\"") !== -1, html);
"""
    )


def test_render_pageview_ip_list_panel_and_local_badge() -> None:
    """IMP-026：独立 IP 列表用 details 展开、本机徽标、转义、空态。"""
    _run(
        f"""
const assert = require("node:assert");
const context = {{
  window: {{ __LWA_TEST_HOOKS__: {{}}, LWA: undefined }},
  document: null,
  fetch: function () {{ throw new Error("no fetch"); }},
  location: {{ hostname: "127.0.0.1", search: "", pathname: "/" }},
  sessionStorage: {{ getItem: function () {{ return null; }}, setItem: function () {{}}, removeItem: function () {{}} }},
  history: {{ replaceState: function () {{}} }},
  setInterval: function () {{ return 0; }},
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  URLSearchParams: URLSearchParams,
  console: console,
}};
vm.runInNewContext({_load_helpers_body()}, context);
vm.runInNewContext({_load_app_body()}, context);
// renderPageviewHtml 定义在 createManagerApp 作用域内，调用一次工厂以填充测试钩子
context.window.LWA.createManagerApp(
  {{ createApp: function (root) {{ return {{ mount: function () {{}} }}; }} }},
  {{
    document: null,
    fetch: function () {{ throw new Error("no fetch"); }},
    location: context.location,
    sessionStorage: context.sessionStorage,
    history: context.history,
    setInterval: function () {{ return 0; }},
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    URLSearchParams: URLSearchParams,
  }}
);
const render = context.window.__LWA_TEST_HOOKS__.renderPageviewHtml;
assert.strictEqual(typeof render, "function");

var html = render("demo", {{
  byDay: [{{ day: "2026-07-15", hits: 2, uniqueIps: 2 }}],
  recent: [],
  source: "caddy",
  uniqueIpList: [
    {{ ip: "10.0.0.5", count: 1, lastSeen: "2026-07-15T10:00:00+08:00", local: true }},
    {{ ip: "8.8.8.8", count: 1, lastSeen: "2026-07-15T10:01:00+08:00", local: false }},
    {{ ip: "<x>", count: 1, lastSeen: "2026-07-15T10:02:00+08:00", local: false }}
  ],
}}, {{ hits: 2, uniqueIps: 2, source: "caddy", lastSeen: "2026-07-15T10:02:00+08:00" }});

assert.ok(html.indexOf('class="ip-list"') !== -1, "应有 details.ip-list 容器");
assert.ok(html.indexOf("独立 IP") !== -1, "应保留「独立 IP」标签");
assert.ok(html.indexOf("ip-local") !== -1, "本机 IP 行应有 ip-local 类");
assert.ok(html.indexOf("本机") !== -1, "应显示本机徽标文字");
assert.ok(html.indexOf("8.8.8.8") !== -1, "应列出非本机 IP");
assert.ok(html.indexOf("<x>") === -1, "IP 文本应被转义，不得出现裸 <x>");
assert.ok(html.indexOf("&lt;x&gt;") !== -1, "转义后应含 &lt;x&gt;");

// 空态
var html2 = render("demo", {{ byDay: [], recent: [], source: "caddy", uniqueIpList: [] }}, {{ hits: 0, uniqueIps: 0 }});
assert.ok(html2.indexOf("暂无") !== -1, "空列表显示「暂无」");
"""
    )


def test_close_ip_list_on_outside_click() -> None:
    """IMP-026 修订：点击 IP 面板外的任意位置应收起所有展开的 details。"""
    _run(
        f"""
const assert = require("node:assert");
const context = {{
  window: {{ __LWA_TEST_HOOKS__: {{}}, LWA: undefined }},
  document: null,
  fetch: function () {{ throw new Error("no fetch"); }},
  location: {{ hostname: "127.0.0.1", search: "", pathname: "/" }},
  sessionStorage: {{ getItem: function () {{ return null; }}, setItem: function () {{}}, removeItem: function () {{}} }},
  history: {{ replaceState: function () {{}} }},
  setInterval: function () {{ return 0; }},
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  URLSearchParams: URLSearchParams,
  console: console,
}};
vm.runInNewContext({_load_helpers_body()}, context);
vm.runInNewContext({_load_app_body()}, context);

// 两个已展开的面板（details.open=true）
var panelA = {{ open: true }};
var panelB = {{ open: true }};
var fakeDoc = {{ querySelectorAll: function () {{ return [panelA, panelB]; }} }};

// 用 fakeDoc 构造工厂，使闭包内的 doc 指向它
context.window.LWA.createManagerApp(
  {{ createApp: function () {{ return {{ mount: function () {{}} }}; }} }},
  {{
    document: fakeDoc,
    fetch: function () {{ throw new Error("no fetch"); }},
    location: context.location,
    sessionStorage: context.sessionStorage,
    history: context.history,
    setInterval: function () {{ return 0; }},
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    URLSearchParams: URLSearchParams,
  }}
);
const close = context.window.__LWA_TEST_HOOKS__.closeIpListsOnOutsideClick;

// 命中点在 .ip-list 之外 → 关闭所有展开面板
close({{ target: {{ closest: function () {{ return null; }} }} }});
assert.strictEqual(panelA.open, false);
assert.strictEqual(panelB.open, false);

// 命中点在 .ip-list 之内 → 不收起
panelA.open = true;
close({{ target: {{ closest: function (sel) {{ return sel === ".ip-list" ? {{}} : null; }} }} }});
assert.strictEqual(panelA.open, true);
"""
    )


def test_helpers_opshtml_remove_for_all_instances() -> None:
    """IMP-035：所有实例显示删除；building/starting/stopping/removing 禁用。"""
    _run(
        f"""
const assert = require("node:assert");
const context = {{ window: {{ __LWA_TEST_HOOKS__: {{}} }}, console: console }};
vm.runInNewContext({_load_helpers_body()}, context);
const opsHtml = context.window.__LWA_TEST_HOOKS__.opsHtml;

function btn(html, op) {{
  var i = html.indexOf('data-op="' + op + '"');
  assert.ok(i !== -1, "missing op=" + op + " in " + html);
  return html.slice(i, html.indexOf(">", i) + 1);
}}

var normal = opsHtml({{ id: "demo", name: "demo", status: "running", runtime: "shared-static", servingMode: "shared-static", stack: [], redundant: false }});
assert.ok(btn(normal, "remove").indexOf("disabled") === -1);

var redundant = opsHtml({{ id: "dup", name: "dup", status: "stopped", runtime: "shared-static", servingMode: "shared-static", stack: [], redundant: true }});
assert.ok(btn(redundant, "remove").indexOf("disabled") === -1);

["building", "starting", "stopping", "removing"].forEach(function (st) {{
  var html = opsHtml({{ id: "x", name: "x", status: st, runtime: "docker-compose", servingMode: "container", stack: [], redundant: false }});
  assert.ok(btn(html, "remove").indexOf("disabled") !== -1, "status=" + st + " should disable remove");
}});
"""
    )


def test_helpers_remove_dialog_state_machine() -> None:
    """IMP-035：双阶段确认；未输完整 ID / 未勾选风险不可提交；仅 data_nonempty 升 force。"""
    _run(
        f"""
const assert = require("node:assert");
const context = {{ window: {{ __LWA_TEST_HOOKS__: {{}} }}, console: console }};
vm.runInNewContext({_load_helpers_body()}, context);
const h = context.window.__LWA_TEST_HOOKS__;
assert.strictEqual(typeof h.canSubmitRemove, "function");
assert.strictEqual(typeof h.shouldElevateRemoveForce, "function");
assert.strictEqual(typeof h.buildRemoveQuery, "function");

var base = {{
  step: 2,
  instanceId: "proj-a",
  mode: "keep",
  confirmId: "",
  acknowledgeIrreversible: false,
  needForce: false,
  acknowledgeForce: false,
  submitting: false,
}};
assert.strictEqual(h.canSubmitRemove(base), false);
assert.strictEqual(h.canSubmitRemove(Object.assign({{}}, base, {{ confirmId: "proj-a" }})), true);
assert.strictEqual(h.canSubmitRemove(Object.assign({{}}, base, {{ confirmId: "proj-a", submitting: true }})), false);

var purge = Object.assign({{}}, base, {{ mode: "purge", confirmId: "proj-a" }});
assert.strictEqual(h.canSubmitRemove(purge), false);
assert.strictEqual(h.canSubmitRemove(Object.assign({{}}, purge, {{ acknowledgeIrreversible: true }})), true);

var forceStep = Object.assign({{}}, purge, {{
  acknowledgeIrreversible: true,
  needForce: true,
  acknowledgeForce: false,
}});
assert.strictEqual(h.canSubmitRemove(forceStep), false);
assert.strictEqual(h.canSubmitRemove(Object.assign({{}}, forceStep, {{ acknowledgeForce: true }})), true);

assert.strictEqual(h.shouldElevateRemoveForce("data_nonempty"), true);
assert.strictEqual(h.shouldElevateRemoveForce("internal"), false);
assert.strictEqual(h.shouldElevateRemoveForce("conflict"), false);
assert.strictEqual(h.shouldElevateRemoveForce(""), false);

assert.strictEqual(h.buildRemoveQuery(false, false), "purge=false&force=false");
assert.strictEqual(h.buildRemoveQuery(true, false), "purge=true&force=false");
assert.strictEqual(h.buildRemoveQuery(true, true), "purge=true&force=true");
"""
    )


def test_app_remove_dialog_methods_and_no_native_confirm() -> None:
    """IMP-035：app 暴露双阶段删除方法；源码不含连续原生 confirm 糊弄。"""
    src = APP_JS.read_text(encoding="utf-8")
    assert "openRemoveDialog" in src
    assert "submitRemoveDialog" in src
    assert "advanceRemoveDialog" in src
    # 单项目删除不得依赖原生 confirm；批量冗余可保留
    # 定位 removeSingleInstance / openRemoveDialog 区域：不得出现 confirm(
    assert (
        "removeSingleInstance: function" not in src
        or "confirm(" not in src.split("removeSingleInstance")[0][-200:]
    )
    # 更稳：删除流程应打开模态而非 confirm
    assert "openRemoveDialog" in src
    assert "removeDialog" in src
    # BUG-264：打开/关闭须管理焦点（初始焦点或恢复触发点）
    assert (
        "_removeFocusBefore" in src
        or "_focusRemoveDialog" in src
        or "_restoreRemoveFocus" in src
        or "focusRemoveDialog" in src
    ), "删除模态须管理键盘焦点"
    # 源码中单实例删除路径不应调用 confirm（允许 removeRedundant 仍用 confirm）
    # 简单启发式：openRemoveDialog 定义存在，且不存在 `if (!confirm("确认删除实例`
    assert 'confirm("确认删除实例' not in src

    _run(
        f"""
const assert = require("node:assert");
const context = {{
  window: {{ __LWA_TEST_HOOKS__: {{}}, LWA: undefined }},
  document: null,
  fetch: function () {{ throw new Error("no fetch"); }},
  location: {{ hostname: "127.0.0.1", search: "", pathname: "/" }},
  sessionStorage: {{ getItem: function () {{ return null; }}, setItem: function () {{}}, removeItem: function () {{}} }},
  history: {{ replaceState: function () {{}} }},
  setInterval: function () {{ return 0; }},
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  URLSearchParams: URLSearchParams,
  console: console,
}};
vm.runInNewContext({_load_helpers_body()}, context);
var capturedRoot = null;
vm.runInNewContext({_load_app_body()}, context);
context.window.LWA.createManagerApp(
  {{ createApp: function (root) {{ capturedRoot = root; return {{ mount: function () {{}} }}; }} }},
  {{
    document: null,
    fetch: function () {{ throw new Error("no fetch"); }},
    location: context.location,
    sessionStorage: context.sessionStorage,
    history: context.history,
    setInterval: function () {{ return 0; }},
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    URLSearchParams: URLSearchParams,
  }}
);
assert.ok(capturedRoot);
assert.strictEqual(typeof capturedRoot.methods.openRemoveDialog, "function");
assert.strictEqual(typeof capturedRoot.methods.advanceRemoveDialog, "function");
assert.strictEqual(typeof capturedRoot.methods.submitRemoveDialog, "function");
assert.strictEqual(typeof capturedRoot.methods.closeRemoveDialog, "function");
const state = capturedRoot.data();
assert.ok(state.removeDialog);
assert.strictEqual(state.removeDialog.open, false);
assert.strictEqual(state.removeDialog.mode, "keep");
assert.ok(capturedRoot.template.indexOf("removeDialog.open") !== -1);
"""
    )


def test_token_input_has_visibility_toggle() -> None:
    """审查 M6 产品策略：保留 ?token=；登录框须支持显隐切换图标按钮。"""
    src = APP_JS.read_text(encoding="utf-8")
    assert "toggleTokenVisibility" in src
    assert "tokenVisible" in src
    assert "token-visibility" in src or "token-visibility-btn" in src

    _run(
        f"""
const assert = require("node:assert");
const context = {{
  window: {{ __LWA_TEST_HOOKS__: {{}}, LWA: undefined }},
  document: null,
  fetch: function () {{ throw new Error("no fetch"); }},
  location: {{ hostname: "127.0.0.1", search: "", pathname: "/" }},
  sessionStorage: {{ getItem: function () {{ return null; }}, setItem: function () {{}}, removeItem: function () {{}} }},
  history: {{ replaceState: function () {{}} }},
  setInterval: function () {{ return 0; }},
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  URLSearchParams: URLSearchParams,
  console: console,
}};
vm.runInNewContext({_load_helpers_body()}, context);
var capturedRoot = null;
vm.runInNewContext({_load_app_body()}, context);
context.window.LWA.createManagerApp(
  {{ createApp: function (root) {{ capturedRoot = root; return {{ mount: function () {{}} }}; }} }},
  {{
    document: null,
    fetch: function () {{ throw new Error("no fetch"); }},
    location: context.location,
    sessionStorage: context.sessionStorage,
    history: context.history,
    setInterval: function () {{ return 0; }},
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    URLSearchParams: URLSearchParams,
  }}
);
assert.ok(capturedRoot);
const state = capturedRoot.data();
assert.strictEqual(state.tokenVisible, false);
assert.strictEqual(typeof capturedRoot.methods.toggleTokenVisibility, "function");
const ctx = {{ tokenVisible: false }};
capturedRoot.methods.toggleTokenVisibility.call(ctx);
assert.strictEqual(ctx.tokenVisible, true);
capturedRoot.methods.toggleTokenVisibility.call(ctx);
assert.strictEqual(ctx.tokenVisible, false);
const t = capturedRoot.template;
assert.ok(t.indexOf('id="token-input"') !== -1, t);
assert.ok(
  t.indexOf(':type="tokenVisible ? \\'text\\' : \\'password\\'"') !== -1
    || t.indexOf(':type="tokenVisible') !== -1,
  "token 输入框须按 tokenVisible 切换 type，实际：" + t
);
assert.ok(
  t.indexOf("toggleTokenVisibility") !== -1,
  "须有显隐切换按钮绑定 toggleTokenVisibility"
);
assert.ok(
  t.indexOf("aria-label") !== -1 && t.indexOf("token") !== -1,
  "显隐按钮须有无障碍标签"
);
"""
    )


def test_bootstrap_surfaces_non_401_errors() -> None:
    """BUG-289：后端 5xx 时不得静默失败，须提示并标记加载失败。

    BUG-281 的用户可见症状即此：token 含非 ASCII 时后端返回 500，前端只处理
    401，于是关掉 token 弹窗、显示空壳页面（无版本号、无实例列表），用户误以为
    已经登录成功。
    """
    _run(
        f"""
const assert = require("node:assert");
const context = {{
  window: {{ __LWA_TEST_HOOKS__: {{}}, LWA: undefined }},
  document: null,
  fetch: function () {{ throw new Error("no fetch"); }},
  location: {{ hostname: "10.0.0.5", search: "", pathname: "/" }},
  sessionStorage: {{ getItem: function () {{ return "tok"; }}, setItem: function () {{}}, removeItem: function () {{}} }},
  history: {{ replaceState: function () {{}} }},
  setInterval: function () {{ return 0; }},
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  URLSearchParams: URLSearchParams,
  console: console,
}};
vm.runInNewContext({_load_helpers_body()}, context);
vm.runInNewContext({_load_app_body()}, context);

let capturedRoot = null;
context.window.LWA.createManagerApp(
  {{ createApp: function (root) {{ capturedRoot = root; return {{ mount: function () {{}} }}; }} }},
  {{
    document: null,
    fetch: function () {{
      return Promise.resolve({{
        status: 500,
        ok: false,
        statusText: "Internal Server Error",
        json: function () {{ return Promise.reject(new Error("not json")); }},
      }});
    }},
    location: context.location,
    sessionStorage: context.sessionStorage,
    history: context.history,
    setInterval: function () {{ return 0; }},
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    URLSearchParams: URLSearchParams,
  }}
);

const state = capturedRoot.data();
assert.strictEqual(state.loadError, "", "初始不应有加载错误");

const toasts = [];
const ctx = Object.assign({{}}, state, {{
  toast: function (msg, kind) {{ toasts.push([msg, kind]); }},
  requireToken: function () {{}},
  refresh: capturedRoot.methods.refresh,
  noteLoadError: capturedRoot.methods.noteLoadError,
  openDetail: function () {{}},
}});

capturedRoot.methods.bootstrap.call(ctx);

setTimeout(function () {{
  assert.ok(
    toasts.some(function (t) {{ return t[1] === "error"; }}),
    "500 时应给出错误 toast，实际：" + JSON.stringify(toasts)
  );
  assert.ok(ctx.loadError, "500 时应设置 loadError，实际：" + JSON.stringify(ctx.loadError));
  // 表格应区分「加载失败」与「暂无实例」
  const html = capturedRoot.computed.tbodyHtml.call(ctx);
  assert.ok(
    html.indexOf("加载失败") !== -1,
    "加载失败时表格不应显示「暂无实例」，实际：" + html
  );
}}, 60);
"""
    )


def test_folder_import_pick_disabled_on_lan_hostname() -> None:
    """IMP-051：非 loopback hostname 时 canPickFolder 为 false。"""
    _run(
        f"""
const assert = require("node:assert");
const context = {{
  window: {{ __LWA_TEST_HOOKS__: {{}}, LWA: undefined }},
  document: null,
  fetch: function () {{ throw new Error("no fetch"); }},
  location: {{ hostname: "192.168.1.10", search: "", pathname: "/" }},
  sessionStorage: {{ getItem: function () {{ return null; }}, setItem: function () {{}}, removeItem: function () {{}} }},
  history: {{ replaceState: function () {{}} }},
  setInterval: function () {{ return 0; }},
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  URLSearchParams: URLSearchParams,
  console: console,
}};
vm.runInNewContext({_load_helpers_body()}, context);
vm.runInNewContext({_load_app_body()}, context);
let capturedRoot = null;
const stubApp = {{ mount: function () {{ return {{}}; }} }};
context.window.LWA.createManagerApp(
  {{ createApp: function (root) {{ capturedRoot = root; return stubApp; }} }},
  {{
    document: null,
    fetch: function () {{ throw new Error("no fetch"); }},
    location: context.location,
    sessionStorage: context.sessionStorage,
    history: context.history,
    setInterval: function () {{ return 0; }},
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    URLSearchParams: URLSearchParams,
  }}
);
assert.strictEqual(capturedRoot.methods.canPickFolder.call({{}}), false);
assert.ok(capturedRoot.template.indexOf("选择文件夹") !== -1);
assert.ok(capturedRoot.template.indexOf("path-browse-note") !== -1);
assert.ok(
  capturedRoot.template.indexOf("不要只选 src/") !== -1,
  "导入对话框应提前提示勿只选 src/"
);
"""
    )


def test_friendly_api_message_strips_error_code_prefix() -> None:
    """管理页展示错误时剥掉 [ZIP_IMPORT_ERROR] 等前缀。"""
    _run(
        f"""
const assert = require("node:assert");
const context = {{ window: {{ __LWA_TEST_HOOKS__: {{}} }}, console: console }};
vm.runInNewContext({_load_helpers_body()}, context);
const friendly = context.window.__LWA_TEST_HOOKS__.friendlyApiMessage;
assert.strictEqual(
  friendly("[ZIP_IMPORT_ERROR] 实例 demo 已存在"),
  "实例 demo 已存在"
);
assert.strictEqual(
  friendly("[FOLDER_SOURCE_ERROR] 目录不存在"),
  "目录不存在"
);
assert.strictEqual(friendly("普通错误"), "普通错误");
assert.strictEqual(friendly(""), "");
assert.strictEqual(friendly(null), "");
"""
    )


def test_style_has_path_with_browse() -> None:
    """IMP-051：样式含路径+浏览同一行布局。"""
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".path-with-browse" in css
    assert ".path-browse-note" in css


def test_describe_folder_import_outcome_pending_is_failure() -> None:
    """识别失败（pending）不得冒充导入成功。"""
    _run(
        f"""
const assert = require("node:assert");
const context = {{ window: {{ __LWA_TEST_HOOKS__: {{}} }}, console: console }};
vm.runInNewContext({_load_helpers_body()}, context);
const describe = context.window.__LWA_TEST_HOOKS__.describeFolderImportOutcome;

var pending = describe({{
  instanceId: "instance",
  autoStart: {{ action: "pending", note: "未识别（无法识别项目类型）" }},
  instance: {{ id: "instance", status: "pending" }},
}});
assert.strictEqual(pending.ok, false);
assert.strictEqual(pending.keepOpen, true);
assert.strictEqual(pending.toastKind, "error");
assert.ok(pending.toast.indexOf("未能识别") !== -1, pending.toast);
assert.ok(pending.error.indexOf("src") !== -1, pending.error);

var ok = describe({{
  instanceId: "demo",
  autoStart: {{ action: "started", note: "ok" }},
  instance: {{ id: "demo", status: "running" }},
}});
assert.strictEqual(ok.ok, true);
assert.strictEqual(ok.keepOpen, false);
assert.strictEqual(ok.toastKind, "success");
assert.ok(ok.toast.indexOf("demo") !== -1, ok.toast);
"""
    )


def test_describe_folder_import_outcome_medium_profile_is_success() -> None:
    """BUG-449：识别成功但档位 medium（action=pending、status=stopped）不得报「未能识别」。"""
    _run(
        f"""
const assert = require("node:assert");
const context = {{ window: {{ __LWA_TEST_HOOKS__: {{}} }}, console: console }};
vm.runInNewContext({_load_helpers_body()}, context);
const describe = context.window.__LWA_TEST_HOOKS__.describeFolderImportOutcome;

var medium = describe({{
  instanceId: "my-next-app",
  autoStart: {{ action: "pending", note: "资源档位 medium，不自动启动，请人工确认后 lwa start" }},
  instance: {{ id: "my-next-app", status: "stopped" }},
}});
assert.strictEqual(medium.ok, true);
assert.strictEqual(medium.toastKind, "success");
assert.strictEqual(medium.keepOpen, false);
assert.ok(medium.toast.indexOf("my-next-app") !== -1, medium.toast);
assert.ok(medium.toast.indexOf("未能识别") === -1, medium.toast);
assert.ok(medium.toast.indexOf("medium") !== -1, medium.toast);

// status 缺失但 action=pending：同样不得冒充未识别
var noStatus = describe({{
  instanceId: "x",
  autoStart: {{ action: "pending", note: "资源档位 heavy" }},
}});
assert.strictEqual(noStatus.ok, true);
assert.ok(noStatus.toast.indexOf("未能识别") === -1, noStatus.toast);
"""
    )


def test_update_from_dir_sets_json_content_type() -> None:
    """BUG-457：从源更新须带 Content-Type: application/json，否则 FastAPI 422。"""
    _run(
        f"""
const assert = require("node:assert");
let captured = null;
const fetchMock = function (path, opts) {{
  captured = {{ path: path, opts: opts || {{}} }};
  return Promise.resolve({{
    status: 200,
    ok: true,
    json: function () {{
      return Promise.resolve({{ skipped: true, instanceId: "backend" }});
    }},
  }});
}};
const context = {{
  window: {{ __LWA_TEST_HOOKS__: {{}}, LWA: undefined }},
  document: null,
  fetch: fetchMock,
  location: {{ hostname: "127.0.0.1", search: "", pathname: "/" }},
  sessionStorage: {{
    getItem: function () {{ return "tok"; }},
    setItem: function () {{}},
    removeItem: function () {{}},
  }},
  history: {{ replaceState: function () {{}} }},
  setInterval: function () {{ return 0; }},
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  URLSearchParams: URLSearchParams,
  console: console,
}};
vm.runInNewContext({_load_helpers_body()}, context);
vm.runInNewContext({_load_app_body()}, context);
let capturedRoot = null;
context.window.LWA.createManagerApp(
  {{ createApp: function (root) {{ capturedRoot = root; return {{ mount: function () {{}} }}; }} }},
  {{
    document: null,
    fetch: fetchMock,
    location: context.location,
    sessionStorage: context.sessionStorage,
    history: context.history,
    setInterval: function () {{ return 0; }},
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    URLSearchParams: URLSearchParams,
  }}
);
const self = Object.assign(capturedRoot.data(), {{
  toast: function () {{}},
  refresh: function () {{}},
  requireToken: function () {{}},
}});
// doUpdateFromDir 不返回 Promise，但 fetch 调用是同步发起的
capturedRoot.methods.doUpdateFromDir.call(self, "backend");
assert.ok(captured, "应发起 fetch");
assert.ok(
  captured.path.indexOf("/api/instances/backend/update-from-dir") !== -1,
  captured.path
);
const headers = captured.opts.headers || {{}};
const ct = headers["Content-Type"] || headers["content-type"] || "";
assert.strictEqual(
  ct,
  "application/json",
  "从源更新必须声明 JSON Content-Type，否则会 422 Unprocessable Content"
);
assert.ok(captured.opts.body && captured.opts.body.indexOf("keepData") !== -1);
"""
    )
