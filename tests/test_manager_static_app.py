"""管理页静态前端脚本的轻量回归测试。"""

from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "src" / "local_webpage_access" / "manager_static" / "app.js"


def test_app_formats_iso_timestamps_with_host_offset() -> None:
    """更新时间应显示为本地主机标准时间格式，而不是原始 ISO 字符串。"""
    script = f"""
const assert = require("node:assert");
const fs = require("node:fs");
const vm = require("node:vm");

const context = {{
  window: {{ __LWA_TEST_HOOKS__: {{}} }},
  document: {{
    readyState: "loading",
    addEventListener: function () {{}},
    getElementById: function () {{
      throw new Error("DOM should not be touched in this test");
    }}
  }},
  location: {{ hostname: "127.0.0.1", search: "", pathname: "/" }},
  sessionStorage: {{
    getItem: function () {{ return null; }},
    setItem: function () {{}},
    removeItem: function () {{}}
  }},
  URLSearchParams: URLSearchParams,
  history: {{ replaceState: function () {{}} }},
  fetch: function () {{ throw new Error("fetch should not be called"); }},
  setInterval: setInterval,
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  console: console
}};

vm.runInNewContext(fs.readFileSync({str(APP_JS)!r}, "utf8"), context);

const formatLocalDateTime = context.window.__LWA_TEST_HOOKS__.formatLocalDateTime;
assert.strictEqual(typeof formatLocalDateTime, "function");
assert.strictEqual(
  formatLocalDateTime("2026-07-07T10:08:00+08:00"),
  "2026-07-07 10:08:00(UTC+8)"
);
assert.strictEqual(
  formatLocalDateTime("2026-07-06T20:12:09+08:00"),
  "2026-07-06 20:12:09(UTC+8)"
);
assert.strictEqual(
  formatLocalDateTime("2026-07-07T02:38:00+05:30"),
  "2026-07-07 02:38:00(UTC+5:30)"
);
assert.strictEqual(formatLocalDateTime(""), "—");
"""
    subprocess.run(["node", "-e", script], check=True)


def test_app_renders_gateway_status_badges() -> None:
    """DEV-043：gateway_down/config_invalid 应有中文徽章标签与连字符 class。"""
    script = f"""
const assert = require("node:assert");
const fs = require("node:fs");
const vm = require("node:vm");

const context = {{
  window: {{ __LWA_TEST_HOOKS__: {{}} }},
  document: {{
    readyState: "loading",
    addEventListener: function () {{}},
    getElementById: function () {{ throw new Error("no DOM"); }}
  }},
  location: {{ hostname: "127.0.0.1", search: "", pathname: "/" }},
  sessionStorage: {{
    getItem: function () {{ return null; }},
    setItem: function () {{}},
    removeItem: function () {{}}
  }},
  URLSearchParams: URLSearchParams,
  history: {{ replaceState: function () {{}} }},
  fetch: function () {{ throw new Error("no fetch"); }},
  setInterval: setInterval,
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  console: console
}};

vm.runInNewContext(fs.readFileSync({str(APP_JS)!r}, "utf8"), context);
const hooks = context.window.__LWA_TEST_HOOKS__;

assert.strictEqual(hooks.statusLabel("gateway_down"), "网关不可达");
assert.strictEqual(hooks.statusLabel("config_invalid"), "配置无效");
assert.strictEqual(hooks.statusLabel("running"), "运行中");
assert.strictEqual(hooks.statusLabel("unknown_status"), "unknown_status");

const gw = hooks.badgeHtml("gateway_down");
assert.ok(gw.indexOf("badge-gateway-down") !== -1, "class 用连字符: " + gw);
assert.ok(gw.indexOf("网关不可达") !== -1, "文本用中文: " + gw);

const cfg = hooks.badgeHtml("config_invalid");
assert.ok(cfg.indexOf("badge-config-invalid") !== -1, cfg);
assert.ok(cfg.indexOf("配置无效") !== -1, cfg);

// BUG-081：可恢复态纳入"仅待处理/失败"筛选
assert.strictEqual(hooks.isActionableStatus("pending"), true);
assert.strictEqual(hooks.isActionableStatus("failed"), true);
assert.strictEqual(hooks.isActionableStatus("gateway_down"), true);
assert.strictEqual(hooks.isActionableStatus("config_invalid"), true);
assert.strictEqual(hooks.isActionableStatus("running"), false);
assert.strictEqual(hooks.isActionableStatus("stopped"), false);
"""
    subprocess.run(["node", "-e", script], check=True)
