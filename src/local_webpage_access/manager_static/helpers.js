/* Local Webpage Access Manager — 纯渲染辅助函数（DEV-046）。
   把所有不依赖运行时 DOM/Vue 的纯函数集中在此，便于：
   1) Vue 组件与原生脚本共用同一套转义/格式化逻辑（输出一致）；
   2) Node vm 单测无需 DOM/Vue 即可验证渲染输出（test_manager_static_*.py）。
   挂在 window.LWA 与 window.__LWA_TEST_HOOKS__。 */
(function () {
  "use strict";

  var LWA = {};

  // ---- 转义与格式化 ----

  LWA.esc = function (s) {
    if (s == null) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  };

  LWA.pad2 = function (n) {
    return String(n).padStart(2, "0");
  };

  LWA.fmtBytes = function (n) {
    if (n == null) return "—";
    n = Number(n);
    var units = ["B", "KiB", "MiB", "GiB", "TiB"];
    for (var i = 0; i < units.length; i++) {
      if (Math.abs(n) < 1024) return n.toFixed(1) + units[i];
      n /= 1024;
    }
    return n.toFixed(1) + "PiB";
  };

  LWA.isDateTimeKey = function (key) {
    return (
      key === "createdAt" ||
      key === "updatedAt" ||
      key === "lastHealthCheckAt" ||
      key === "lastStartedAt" ||
      key === "startedAt" ||
      key === "finishedAt"
    );
  };

  function formatOffsetMinutes(totalMinutes) {
    if (totalMinutes === 0) return "UTC";
    var sign = totalMinutes >= 0 ? "+" : "-";
    var abs = Math.abs(totalMinutes);
    var hours = Math.floor(abs / 60);
    var minutes = abs % 60;
    return "UTC" + sign + hours + (minutes ? ":" + LWA.pad2(minutes) : "");
  }

  LWA.formatLocalDateTime = function (value) {
    if (value == null) return "—";
    var text = String(value).trim();
    if (!text) return "—";

    // 统一换算到本机时区：new Date() 解析出绝对时刻，再用 getHours() 等
    // 本地方法渲染。带时区的 ISO（如 Caddy 的 +00:00 / Z）也会转为本地时间，
    // 而非保留源时区原样显示。
    var d = new Date(text);
    if (isNaN(d.getTime())) return text;
    return (
      d.getFullYear() +
      "-" +
      LWA.pad2(d.getMonth() + 1) +
      "-" +
      LWA.pad2(d.getDate()) +
      " " +
      LWA.pad2(d.getHours()) +
      ":" +
      LWA.pad2(d.getMinutes()) +
      ":" +
      LWA.pad2(d.getSeconds()) +
      "(" +
      formatOffsetMinutes(-d.getTimezoneOffset()) +
      ")"
    );
  };

  // ---- 状态与徽章 ----

  LWA.STATUS_LABELS = {
    running: "运行中",
    stopped: "已停止",
    pending: "待识别",
    building: "构建中",
    failed: "失败",
    queued: "排队中",
    gateway_down: "网关不可达",
    config_invalid: "配置无效",
  };

  LWA.statusLabel = function (status) {
    return LWA.STATUS_LABELS[status] || status || "—";
  };

  LWA.isActionableStatus = function (status) {
    return (
      status === "pending" ||
      status === "failed" ||
      status === "gateway_down" ||
      status === "config_invalid"
    );
  };

  LWA.badgeHtml = function (status) {
    var cls = "badge-" + String(status || "pending").replace(/_/g, "-");
    return (
      '<span class="badge ' + cls + '">' + LWA.esc(LWA.statusLabel(status)) + "</span>"
    );
  };

  // ---- 表格单元格 ----

  LWA.stackHtml = function (stack, database) {
    var html = "";
    if (database) {
      html += '<span class="stack-tag db" title="数据库">' + LWA.esc(database) + "</span>";
    }
    if (stack && stack.length) {
      html += stack
        .slice(0, 4)
        .map(function (s) {
          return '<span class="stack-tag">' + LWA.esc(s) + "</span>";
        })
        .join("");
    }
    return html || '<span class="cell-muted">—</span>';
  };

  LWA.urlHtml = function (i) {
    var parts = [];
    if (i.lanUrl) {
      parts.push(
        '<a href="' +
          LWA.esc(i.lanUrl) +
          '" target="_blank" rel="noopener" title="宿主端口访问（LAN IP）">端口</a>'
      );
    }
    // 建议项 D：始终提供 127.0.0.1 本机链接作兜底——DHCP/换网后 LAN IP 漂移，
    // 旧 lanUrl 打不开时，本机回环链接仍可用。
    if (i.localhostUrl) {
      parts.push(
        '<a href="' +
          LWA.esc(i.localhostUrl) +
          '" target="_blank" rel="noopener" title="本机回环访问（127.0.0.1，LAN IP 漂移时兜底）">本机</a>'
      );
    }
    if (i.routeUrl) {
      parts.push(
        '<a href="' +
          LWA.esc(i.routeUrl) +
          '" target="_blank" rel="noopener" title="路径别名入口">/' +
          LWA.esc(i.routeHost || "") +
          "/</a>"
      );
    }
    if (!parts.length) return '<span class="cell-muted">—</span>';
    return parts.join('<span class="cell-muted"> · </span>');
  };

  LWA.portHtml = function (i) {
    if (!i.hostPort) return '<span class="cell-muted">—</span>';
    var main = LWA.esc(String(i.hostPort));
    if (i.portMappingLabel) {
      return (
        '<span class="port-cell" title="应用内部端口 → 宿主访问端口">' +
        '<span class="port-main">' + main + "</span>" +
        '<span class="port-sub">映射 ' + LWA.esc(i.portMappingLabel) + "</span>" +
        "</span>"
      );
    }
    return main;
  };

  LWA.resourceHtml = function (i) {
    var parts = [];
    if (i.lastMemoryBytes) parts.push(LWA.fmtBytes(i.lastMemoryBytes));
    if (i.lastCpuPercent != null) parts.push(i.lastCpuPercent.toFixed(1) + "%");
    return parts.length ? LWA.esc(parts.join(" ")) : "—";
  };

  // IMP-024（DEV-061）：浏览量单元格——命中数可点击展开详情。
  LWA.pageviewHtml = function (i, pageviewMap) {
    var pv = pageviewMap && pageviewMap[i.id];
    if (!pv || !pv.hits) {
      return '<span class="cell-muted" title="暂无访问记录（静态站点访问后即统计）">—</span>';
    }
    var tip =
      "共 " + pv.hits + " 次访问，独立 IP " + (pv.uniqueIps || 0) + " 个" +
      (pv.source ? "（来源：" + LWA.sourceLabel(pv.source) + "）" : "");
    return (
      '<button class="pageview-btn" data-op="pageview" data-id="' +
      LWA.esc(i.id) +
      '" title="' +
      LWA.esc(tip) +
      '">' +
      Number(pv.hits).toLocaleString() +
      "</button>"
    );
  };

  LWA.sourceLabel = function (src) {
    return (
      {
        builtin: "内置网关日志",
        caddy: "Caddy 访问日志",
        container: "容器日志（近似）",
      }[src] || src
    );
  };

  // ---- 操作区按钮 ----

  LWA.opBtn = function (id, op, label, disabled, title) {
    return (
      '<button class="btn btn-sm" data-op="' +
      op +
      '" data-id="' +
      id +
      '"' +
      (disabled ? " disabled" : "") +
      (title ? ' title="' + LWA.esc(title) + '"' : "") +
      ">" +
      label +
      "</button>"
    );
  };

  LWA.opsHtml = function (i) {
    var id = LWA.esc(i.id);
    var isRunning = i.status === "running";
    var inProgress =
      i.status === "building" ||
      i.status === "queued" ||
      i.status === "pending";
    var supportsAlias =
      i.runtime === "shared-static" || i.runtime === "docker-compose";
    var html = "";
    html += LWA.opBtn(id, "logs", "日志", false);
    html += LWA.opBtn(
      id,
      "path-alias",
      "路径别名",
      !supportsAlias || inProgress,
      !supportsAlias ? "该形态暂不支持路径别名" : ""
    );
    if (i.status === "gateway_down" || i.status === "config_invalid") {
      html += LWA.opBtn(
        id,
        "recover",
        "恢复",
        inProgress,
        i.status === "gateway_down"
          ? "Caddy master 不可达，点此拉起网关并重启实例"
          : "站点路由/配置疑似异常，点此重启并重新加载配置"
      );
    }
    html += LWA.opBtn(id, "start", "启动", isRunning || inProgress);
    html += LWA.opBtn(id, "stop", "停止", !isRunning || inProgress);
    html += LWA.opBtn(id, "restart", "重启", inProgress);
    html += LWA.opBtn(id, "rebuild", "重建", inProgress);
    if (i.redundant) {
      html += LWA.opBtn(
        id,
        "remove",
        "删除",
        false,
        "移除此冗余实例（registry 记录 + 停服）；同源最早者保留"
      );
    }
    return html;
  };

  // ---- 整行 HTML（Vue 组件用 v-html 渲染，保证与原生版本输出一致）----

  LWA.rowHtml = function (i, pageviewMap) {
    var classes = [];
    if (i.status === "failed") classes.push("row-failed");
    else if (i.status === "pending") classes.push("row-pending");
    else if (i.status === "gateway_down" || i.status === "config_invalid")
      classes.push("row-warn");
    if (i.redundant) classes.push("row-redundant");
    var rowClass = classes.join(" ");
    var nameCell =
      '<td class="cell-name" data-detail="' +
      LWA.esc(i.id) +
      '">' +
      LWA.esc(i.name || i.id) +
      (i.redundant
        ? ' <span class="redundant-badge" title="与同源 zip 的更早实例重复">冗余</span>'
        : "") +
      "</td>";
    return (
      '<tr class="' + rowClass + '">' +
      nameCell +
      "<td>" + LWA.badgeHtml(i.status) + "</td>" +
      '<td class="cell-muted">' + LWA.esc(i.desiredState || "—") + "</td>" +
      '<td class="cell-muted">' + LWA.esc(i.servingMode || "—") + "</td>" +
      "<td>" + LWA.esc(i.kind || "—") + "</td>" +
      '<td class="cell-muted">' + LWA.esc(i.runtime || "—") + "</td>" +
      "<td>" + LWA.stackHtml(i.stack, i.database) + "</td>" +
      '<td class="cell-url">' + LWA.urlHtml(i) + "</td>" +
      "<td>" + LWA.portHtml(i) + "</td>" +
      '<td class="cell-muted">' + LWA.resourceHtml(i) + "</td>" +
      "<td>" + LWA.pageviewHtml(i, pageviewMap) + "</td>" +
      '<td class="cell-muted">' + LWA.esc(LWA.formatLocalDateTime(i.updatedAt)) + "</td>" +
      '<td class="col-ops"><div class="ops">' + LWA.opsHtml(i) + "</div></td>" +
      "</tr>"
    );
  };

  // ---- 筛选（纯函数：filters 由调用方注入，Vue 传响应式状态）----

  LWA.applyFilters = function (rows, filters) {
    filters = filters || {};
    var search = (filters.search || "").trim().toLowerCase();
    var status = filters.status || "";
    var form = filters.form || "";
    var filterPending = !!filters.pending;
    var filterRedundant = !!filters.redundant;
    return rows.filter(function (i) {
      if (status && i.status !== status) return false;
      if (form && i.servingMode !== form) return false;
      if (filterPending && !LWA.isActionableStatus(i.status)) return false;
      if (filterRedundant && !i.redundant) return false;
      if (search) {
        var hay = [
          i.name || "",
          i.id || "",
          i.kind || "",
          i.runtime || "",
          i.servingMode || "",
          (i.stack && i.stack.join(" ")) || "",
          i.routeHost || "",
        ]
          .join(" ")
          .toLowerCase();
        if (hay.indexOf(search) === -1) return false;
      }
      return true;
    });
  };

  // ---- 导出 ----

  if (typeof window !== "undefined") {
    window.LWA = LWA;
    if (window.__LWA_TEST_HOOKS__) {
      window.__LWA_TEST_HOOKS__.formatLocalDateTime = LWA.formatLocalDateTime;
      window.__LWA_TEST_HOOKS__.statusLabel = LWA.statusLabel;
      window.__LWA_TEST_HOOKS__.badgeHtml = LWA.badgeHtml;
      window.__LWA_TEST_HOOKS__.isActionableStatus = LWA.isActionableStatus;
      window.__LWA_TEST_HOOKS__.applyFilters = LWA.applyFilters;
      window.__LWA_TEST_HOOKS__.opsHtml = LWA.opsHtml;
      window.__LWA_TEST_HOOKS__.rowHtml = LWA.rowHtml;
      window.__LWA_TEST_HOOKS__.pageviewHtml = LWA.pageviewHtml;
      window.__LWA_TEST_HOOKS__.sourceLabel = LWA.sourceLabel;
    }
  }
  if (typeof module !== "undefined" && module.exports) {
    module.exports = LWA;
  }
})();
