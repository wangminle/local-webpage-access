"""管理页前端脚本测试（DEV-046 Vue 3 重构后）。

DEV-046 把纯渲染函数抽到 ``helpers.js``（``window.LWA`` / ``__LWA_TEST_HOOKS__``），
``app.js`` 改为 Vue 3 工厂（``createManagerApp``，依赖注入）。本测试：

* 在 Node vm 中加载 helpers.js，验证纯函数输出（无 DOM/Vue 依赖）；
* 加载 app.js，用桩 createApp + 桩 deps 构造组件，验证响应式结构与方法挂接
  （冒烟测试：模板/数据/方法齐全，不真正挂载）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "local_webpage_access" / "manager_static"
HELPERS_JS = STATIC / "helpers.js"
APP_JS = STATIC / "app.js"


# 共享的 vm 上下文骨架（window/document/location 等桩）
_VM_PRELUDE = """
const fs = require("node:fs");
const vm = require("node:vm");
"""


def _run(script: str) -> None:
    subprocess.run(["node", "-e", _VM_PRELUDE + script], check=True)


def _load_helpers_body() -> str:
    return f'fs.readFileSync({str(HELPERS_JS)!r}, "utf8")'


def _load_app_body() -> str:
    return f'fs.readFileSync({str(APP_JS)!r}, "utf8")'


def test_helpers_format_local_date_time() -> None:
    """更新时间应显示为本地主机标准时间格式，而不是原始 ISO 字符串。"""
    _run(
        f"""
const assert = require("node:assert");
const context = {{
  window: {{ __LWA_TEST_HOOKS__: {{}} }},
  console: console,
}};
vm.runInNewContext({_load_helpers_body()}, context);
const f = context.window.__LWA_TEST_HOOKS__.formatLocalDateTime;
assert.strictEqual(f("2026-07-07T10:08:00+08:00"), "2026-07-07 10:08:00(UTC+8)");
assert.strictEqual(f("2026-07-06T20:12:09+08:00"), "2026-07-06 20:12:09(UTC+8)");
assert.strictEqual(f("2026-07-07T02:38:00+05:30"), "2026-07-07 02:38:00(UTC+5:30)");
assert.strictEqual(f(""), "—");
"""
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
["refresh", "onTableClick", "openDetail", "openLogs", "openPathAlias", "openPageview", "removeRedundant", "submitToken"].forEach(function (m) {{
  assert.strictEqual(typeof capturedRoot.methods[m], "function", "缺方法 " + m);
}});

// data 初值
const state = capturedRoot.data();
assert.ok(Array.isArray(state.instances));
assert.strictEqual(state.filters.search, "");
assert.strictEqual(state.pageview.open, false);

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
