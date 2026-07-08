/* Local Webpage Access Manager — 前端逻辑（WBS-23）。
   原生 JS，无框架无构建。15s 低频轮询（WBS-23.13）。 */

(function () {
  "use strict";

  var TOKEN_KEY = "lwa-token";
  var POLL_MS = 15000; // 15s（WBS-23.13：10-30s 区间）
  var pollTimer = null;
  var lastInstances = [];
  var currentDetailId = null;

  function isLocalhostAccess() {
    var h = location.hostname;
    return h === "localhost" || h === "127.0.0.1" || h === "[::1]";
  }

  // ---- token（WBS-22.12 前端配合）------------------------------------------

  function getToken() {
    var stored = sessionStorage.getItem(TOKEN_KEY);
    if (stored) return stored;
    // 支持 http://host:17800/?token=xxx 直接进入
    var params = new URLSearchParams(location.search);
    var fromUrl = params.get("token");
    if (fromUrl) {
      sessionStorage.setItem(TOKEN_KEY, fromUrl);
      // 从地址栏抹掉 token，避免泄露到历史/分享
      params.delete("token");
      var clean = params.toString();
      history.replaceState(null, "", location.pathname + (clean ? "?" + clean : ""));
      return fromUrl;
    }
    return null;
  }

  function requireToken(onReady) {
    if (isLocalhostAccess()) {
      onReady(null);
      return;
    }
    var token = getToken();
    if (token) {
      onReady(token);
      return;
    }
    showTokenModal(onReady);
  }

  function showTokenModal(onReady) {
    var modal = document.getElementById("token-modal");
    modal.hidden = false;
    var input = document.getElementById("token-input");
    input.value = "";
    input.focus();
    function submit() {
      var val = input.value.trim();
      if (!val) return;
      sessionStorage.setItem(TOKEN_KEY, val);
      modal.hidden = true;
      onReady(val);
    }
    document.getElementById("token-submit").onclick = submit;
    input.onkeydown = function (e) {
      if (e.key === "Enter") submit();
    };
  }

  // ---- API 封装 ------------------------------------------------------------

  function apiFetch(path, opts) {
    opts = opts || {};
    opts.headers = opts.headers || {};
    var token = getToken();
    if (token) {
      opts.headers["Authorization"] = "Bearer " + token;
    }
    return fetch(path, opts).then(function (resp) {
      if (resp.status === 401) {
        // token 失效，重新要求输入（局域网访问）
        if (isLocalhostAccess()) {
          throw new Error("unauthorized");
        }
        sessionStorage.removeItem(TOKEN_KEY);
        toast("token 无效，请重新输入", "error");
        setTimeout(function () {
          requireToken(start);
        }, 800);
        throw new Error("unauthorized");
      }
      if (!resp.ok) {
        return resp.json().then(
          function (body) {
            throw new Error(
              (body && body.error && body.error.message) || resp.statusText
            );
          },
          function () {
            throw new Error(resp.statusText);
          }
        );
      }
      return resp.json();
    });
  }

  // ---- 渲染：顶部统计（WBS-23.02）-----------------------------------------

  function renderStats(data) {
    var c = data.counts || {};
    setText("stat-total", c.total || 0);
    setText("stat-running", c.running || 0);
    setText("stat-stopped", c.stopped || 0);
    setText("stat-pending", c.pending || 0);
    setText("stat-failed", c.failed || 0);
    // BUG-081：网关不可达 + 配置无效合并为"需恢复"
    setText("stat-needs-recover", (c.gateway_down || 0) + (c.config_invalid || 0));
    setText("stat-db", data.databaseCount || 0);

    var pp = data.portPool || {};
    setText(
      "stat-ports",
      pp.allocated + " / " + pp.total + " 已分配（" + pp.start + "-" + pp.end + "）"
    );

    var types = data.typeDistribution || {};
    var typeStr = Object.keys(types)
      .map(function (k) {
        return k + " ×" + types[k];
      })
      .join("， ");
    setText("stat-types", typeStr || "—");

    var host = data.host || {};
    setText(
      "stat-mem",
      host.memTotalBytes
        ? fmtBytes(host.memUsedBytes) + " / " + fmtBytes(host.memTotalBytes)
        : "（非 Linux，已跳过）"
    );
    setText(
      "stat-disk",
      host.diskTotalBytes
        ? fmtBytes(host.diskUsedBytes) + " / " + fmtBytes(host.diskTotalBytes)
        : "—"
    );
  }

  // ---- 渲染：实例表格（WBS-23.03/04/12）-----------------------------------

  // BUG-081：可恢复/待处理的异常态统一纳入"仅待处理/失败"筛选视图
  function isActionableStatus(status) {
    return (
      status === "pending" ||
      status === "failed" ||
      status === "gateway_down" ||
      status === "config_invalid"
    );
  }

  function renderInstances(instances) {
    lastInstances = instances || [];
    var filterPending = document.getElementById("filter-pending").checked;
    var rows = lastInstances;
    if (filterPending) {
      rows = rows.filter(function (i) {
        return isActionableStatus(i.status);
      });
    }
    var body = document.getElementById("instances-body");
    if (!rows.length) {
      body.innerHTML =
        '<tr class="empty-row"><td colspan="12">' +
        (lastInstances.length ? "没有匹配的实例" : "暂无实例，把 zip 放进 inbox/ 或用 lwa import 导入") +
        "</td></tr>";
      return;
    }
    body.innerHTML = rows.map(rowHtml).join("");
    // 绑定事件
    body.querySelectorAll("[data-detail]").forEach(function (el) {
      el.onclick = function () {
        openDetail(el.getAttribute("data-detail"));
      };
    });
  }

  function rowHtml(i) {
    var rowClass = "";
    if (i.status === "failed") rowClass = "row-failed";
    else if (i.status === "pending") rowClass = "row-pending";
    else if (i.status === "gateway_down" || i.status === "config_invalid")
      rowClass = "row-warn";
    return (
      '<tr class="' + rowClass + '">' +
      '<td class="cell-name" data-detail="' + esc(i.id) + '">' + esc(i.name || i.id) + "</td>" +
      "<td>" + badgeHtml(i.status) + "</td>" +
      '<td class="cell-muted">' + esc(i.desiredState || "—") + "</td>" +
      '<td class="cell-muted">' + esc(i.servingMode || "—") + "</td>" +
      "<td>" + esc(i.kind || "—") + "</td>" +
      '<td class="cell-muted">' + esc(i.runtime || "—") + "</td>" +
      "<td>" + stackHtml(i.stack, i.database) + "</td>" +
      '<td class="cell-url">' + urlHtml(i) + "</td>" +
      "<td>" + portHtml(i) + "</td>" +
      '<td class="cell-muted">' + resourceHtml(i) + "</td>" +
      '<td class="cell-muted">' + esc(formatLocalDateTime(i.updatedAt)) + "</td>" +
      '<td class="col-ops"><div class="ops">' + opsHtml(i) + "</div></td>" +
      "</tr>"
    );
  }

  // DEV-043：状态中文标签（含 gateway_down / config_invalid）
  var STATUS_LABELS = {
    running: "运行中",
    stopped: "已停止",
    pending: "待识别",
    building: "构建中",
    failed: "失败",
    queued: "排队中",
    gateway_down: "网关不可达",
    config_invalid: "配置无效",
  };

  function statusLabel(status) {
    return STATUS_LABELS[status] || status || "—";
  }

  function badgeHtml(status) {
    // class 用连字符（gateway_down → badge-gateway-down），文本用中文标签
    var cls = "badge-" + String(status || "pending").replace(/_/g, "-");
    return (
      '<span class="badge ' + cls + '">' + esc(statusLabel(status)) + "</span>"
    );
  }

  function stackHtml(stack, database) {
    var html = "";
    if (database) {
      html += '<span class="stack-tag db" title="数据库">' + esc(database) + "</span>";
    }
    if (stack && stack.length) {
      html += stack
        .slice(0, 4)
        .map(function (s) {
          return '<span class="stack-tag">' + esc(s) + "</span>";
        })
        .join("");
    }
    return html || '<span class="cell-muted">—</span>';
  }

  function urlHtml(i) {
    var parts = [];
    if (i.lanUrl) {
      parts.push(
        '<a href="' +
          esc(i.lanUrl) +
          '" target="_blank" rel="noopener" title="宿主端口访问">端口</a>'
      );
    }
    if (i.routeUrl) {
      parts.push(
        '<a href="' +
          esc(i.routeUrl) +
          '" target="_blank" rel="noopener" title="路径别名入口">/' +
          esc(i.routeHost || "") +
          "/</a>"
      );
    }
    if (!parts.length) return '<span class="cell-muted">—</span>';
    return parts.join('<span class="cell-muted"> · </span>');
  }

  function portHtml(i) {
    // IMP-007：主显示 hostPort，副信息为后端格式化的 portMappingLabel
    // （internalPort→hostPort，容器/前端项目且二者不同时存在）。
    if (!i.hostPort) return '<span class="cell-muted">—</span>';
    var main = esc(String(i.hostPort));
    if (i.portMappingLabel) {
      return (
        '<span class="port-cell" title="应用内部端口 → 宿主访问端口">' +
        '<span class="port-main">' + main + "</span>" +
        '<span class="port-sub">映射 ' + esc(i.portMappingLabel) + "</span>" +
        "</span>"
      );
    }
    return main;
  }

  function resourceHtml(i) {
    var parts = [];
    if (i.lastMemoryBytes) parts.push(fmtBytes(i.lastMemoryBytes));
    if (i.lastCpuPercent != null) parts.push(i.lastCpuPercent.toFixed(1) + "%");
    return parts.length ? esc(parts.join(" ")) : "—";
  }

  function opsHtml(i) {
    var id = esc(i.id);
    var isRunning = i.status === "running";
    // 进行中态（BUG-050）：这些状态下点击启动/停止/重启/重建会与
    // 正在进行的构建或导入竞争，引发锁冲突或并发构建。统一禁用。
    var inProgress =
      i.status === "building" ||
      i.status === "queued" ||
      i.status === "pending";
    var isStatic = i.runtime === "shared-static";
    var html = "";
    // "打开" 已由"访问地址"列承载，操作区不再重复
    html += opBtn(id, "logs", "日志", false);
    html += opBtn(
      id,
      "path-alias",
      "路径别名",
      !isStatic || inProgress,
      !isStatic ? "路径别名仅支持静态站点" : ""
    );
    // DEV-043：网关不可达 / 配置无效时给一键 recover（先拉 master 再 restart）
    if (i.status === "gateway_down" || i.status === "config_invalid") {
      html += opBtn(
        id,
        "recover",
        "恢复",
        inProgress,
        i.status === "gateway_down"
          ? "Caddy master 不可达，点此拉起网关并重启实例"
          : "站点路由/配置疑似异常，点此重启并重新加载配置"
      );
    }
    html += opBtn(id, "start", "启动", isRunning || inProgress);
    html += opBtn(id, "stop", "停止", !isRunning || inProgress);
    html += opBtn(id, "restart", "重启", inProgress);
    html += opBtn(id, "rebuild", "重建", inProgress);
    return html;
  }

  function opBtn(id, op, label, disabled, title) {
    return (
      '<button class="btn btn-sm" data-op="' +
      op +
      '" data-id="' +
      id +
      '"' +
      (disabled ? " disabled" : "") +
      (title ? ' title="' + esc(title) + '"' : "") +
      ">" +
      label +
      "</button>"
    );
  }

  // ---- 操作（WBS-23.06）----------------------------------------------------

  function handleOpsClick(e) {
    var btn = e.target.closest("[data-op]");
    if (!btn) return;
    var op = btn.getAttribute("data-op");
    var id = btn.getAttribute("data-id");
    if (op === "logs") {
      openLogs(id);
      return;
    }
    if (op === "path-alias") {
      openPathAlias(id);
      return;
    }
    doOperation(id, op);
  }

  function doOperation(id, op) {
    toast("正在" + opLabel(op) + "…");
    apiFetch("/api/instances/" + encodeURIComponent(id) + "/" + op, {
      method: "POST",
    })
      .then(function () {
        toast(opLabel(op) + "完成", "success");
        refresh();
      })
      .catch(function (e) {
        toast(opLabel(op) + "失败：" + e.message, "error");
      });
  }

  function opLabel(op) {
    return (
      {
        start: "启动",
        stop: "停止",
        restart: "重启",
        rebuild: "重建",
        recover: "恢复",
      }[op] || op
    );
  }

  // ---- 详情抽屉（WBS-23.08~11）---------------------------------------------

  function openDetail(id) {
    currentDetailId = id;
    apiFetch("/api/instances/" + encodeURIComponent(id))
      .then(function (data) {
        renderDetail(data);
        document.getElementById("drawer").hidden = false;
        document.getElementById("drawer").setAttribute("aria-hidden", "false");
        document.getElementById("drawer-mask").hidden = false;
      })
      .catch(function (e) {
        toast("加载详情失败：" + e.message, "error");
      });
  }

  function closeDetail() {
    currentDetailId = null;
    document.getElementById("drawer").hidden = true;
    document.getElementById("drawer").setAttribute("aria-hidden", "true");
    document.getElementById("drawer-mask").hidden = true;
  }

  function renderDetail(data) {
    var inst = data.instance || {};
    var manifest = data.manifest || {};
    document.getElementById("drawer-title").textContent =
      inst.name || inst.id || "实例详情";

    var body = document.getElementById("drawer-body");
    var html = "";

    // 基本信息
    html += section("基本信息", kvList(inst, [
      ["id", "ID"],
      ["name", "名称"],
      ["status", "状态"],
      ["desiredState", "期望状态"],
      ["kind", "技术族"],
      ["runtime", "运行层"],
      ["lanUrl", "访问地址"],
      ["hostPort", "宿主端口"],
      ["internalPort", "内部端口"],
      ["portMappingLabel", "端口映射"],
      ["routeHost", "路径别名"],
      ["routeUrl", "路径入口"],
      ["lastError", "最近错误"],
      ["lastHealthCheckAt", "最近健康检查"],
      ["updatedAt", "更新时间"],
    ]));

    // local-web.json 摘要（WBS-23.09）
    if (manifest && !manifest._error) {
      html += section(
        "local-web.json 摘要",
        kvList(manifest, [
          ["id", "ID"],
          ["name", "名称"],
          ["version", "版本"],
          ["kind", "技术族"],
          ["runtime", "运行层"],
          ["servingMode", "服务模式"],
          ["resourceProfile", "资源档位"],
        ]) +
          (manifest.stack && manifest.stack.length
            ? '<div class="detail-kv"><dt>技术栈</dt><dd>' +
              manifest.stack.map(esc).join("、 ") +
              "</dd></div>"
            : "")
      );

      // Dockerfile / Compose / 静态配置摘要（WBS-23.10）
      if (manifest.container) {
        html += section(
          "容器配置",
          kvList(manifest.container, [
            ["image", "镜像"],
            ["hostPort", "宿主端口"],
            ["internalPort", "内部端口"],
          ])
        );
      }
      if (manifest.static) {
        html += section(
          "静态配置",
          kvList(manifest.static, [
            ["root", "根目录"],
            ["gateway", "网关"],
            ["hostPort", "宿主端口"],
            ["routeMode", "路由模式"],
            ["routeHost", "路径别名"],
          ])
        );
      }
    }

    // IMP-006：路径别名说明
    if (inst.runtime === "shared-static" && !inst.routeHost) {
      html += section(
        "路径别名",
        '<p class="detail-hint">当前未启用路径别名，仅可通过宿主端口访问。</p>' +
          '<p class="detail-hint">可在列表操作区点击「路径别名」设置，或导入时在 CLI 指定：<code>lwa import inbox/foo.zip --path-alias my-slug</code></p>'
      );
    } else if (inst.routeHost) {
      html += section(
        "路径别名",
        '<p class="detail-hint">已启用 <code>/' +
          esc(inst.routeHost) +
          "/</code>" +
          (inst.routeUrl
            ? ' · <a href="' +
              esc(inst.routeUrl) +
              '" target="_blank" rel="noopener">打开路径入口</a>'
            : "") +
          "</p>" +
          '<p class="detail-hint cell-muted">可在操作区「路径别名」在线修改；原地更新 zip 会保留当前别名。</p>'
      );
    }

    // 构建记录（WBS-23.11）
    if (data.builds && data.builds.length) {
      html += section(
        "构建记录",
        '<ul class="detail-events">' +
          data.builds
            .slice(0, 8)
            .map(function (b) {
              return (
                "<li><time>" +
                esc(formatLocalDateTime(b.startedAt || b.started_at)) +
                "</time>" +
                esc(b.status || "") +
                (b.errorSummary || b.error_summary
                  ? " — " + esc(b.errorSummary || b.error_summary)
                  : "") +
                "</li>"
              );
            })
            .join("") +
          "</ul>"
      );
    }

    // 健康检查 / 事件（WBS-23.11）
    if (data.events && data.events.length) {
      html += section(
        "最近事件",
        '<ul class="detail-events">' +
          data.events
            .slice(0, 15)
            .map(function (ev) {
              return (
                "<li><time>" +
                esc(formatLocalDateTime(ev.createdAt || ev.created_at)) +
                "</time>[" +
                esc(ev.eventType || ev.event_type || "") +
                "] " +
                esc(ev.message || "") +
                "</li>"
              );
            })
            .join("") +
          "</ul>"
      );
    }

    body.innerHTML = html;
  }

  function section(title, content) {
    return (
      '<div class="detail-section"><h3>' +
      esc(title) +
      "</h3>" +
      content +
      "</div>"
    );
  }

  function kvList(obj, pairs) {
    var dl = '<dl class="detail-kv">';
    pairs.forEach(function (p) {
      var key = p[0];
      var label = p[1];
      var val = obj ? obj[key] : null;
      if (val === null || val === undefined || val === "") val = "—";
      else if (isDateTimeKey(key)) val = formatLocalDateTime(val);
      dl += "<dt>" + esc(label) + "</dt><dd>" + esc(String(val)) + "</dd>";
    });
    return dl + "</dl>";
  }

  // ---- 日志（WBS-23.07）----------------------------------------------------

  function openLogs(id, category) {
    var modal = document.getElementById("logs-modal");
    document.getElementById("logs-title").textContent = "日志：" + id;
    document.getElementById("logs-category").value = category || "run";
    modal.hidden = false;
    modal.dataset.instanceId = id;
    fetchLogs(id, category || "run");
  }

  function fetchLogs(id, category) {
    apiFetch(
      "/api/instances/" +
        encodeURIComponent(id) +
        "/logs?category=" +
        encodeURIComponent(category) +
        "&tail=300"
    )
      .then(function (data) {
        var pre = document.getElementById("logs-content");
        pre.textContent = data.content || "（日志为空）";
      })
      .catch(function (e) {
        document.getElementById("logs-content").textContent =
          "加载失败：" + e.message;
      });
  }

  function closeLogs() {
    document.getElementById("logs-modal").hidden = true;
  }

  // ---- 路径别名（IMP-006 / WBS-006.07）-------------------------------------

  var pathAliasInstanceId = null;

  function openPathAlias(id) {
    pathAliasInstanceId = id;
    var modal = document.getElementById("path-alias-modal");
    var input = document.getElementById("path-alias-input");
    var errEl = document.getElementById("path-alias-error");
    errEl.hidden = true;
    errEl.textContent = "";
    document.getElementById("path-alias-title").textContent =
      "路径别名 · " + id;
    var inst = lastInstances.find(function (x) {
      return x.id === id;
    });
    input.value = (inst && inst.routeHost) || "";
    modal.hidden = false;
    input.focus();
  }

  function closePathAlias() {
    pathAliasInstanceId = null;
    document.getElementById("path-alias-modal").hidden = true;
    document.getElementById("path-alias-error").hidden = true;
  }

  function showPathAliasError(msg) {
    var errEl = document.getElementById("path-alias-error");
    errEl.textContent = msg;
    errEl.hidden = false;
  }

  function submitPathAlias(alias) {
    if (!pathAliasInstanceId) return;
    var savedId = pathAliasInstanceId;
    var errEl = document.getElementById("path-alias-error");
    errEl.hidden = true;
    apiFetch(
      "/api/instances/" + encodeURIComponent(savedId) + "/path-alias",
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ alias: alias }),
      }
    )
      .then(function (data) {
        closePathAlias();
        if (data.aliasEntryEnabled === false && data.alias) {
          toast("别名已保存（builtin 模式仅端口可达）", "success");
        } else {
          toast("路径别名已更新", "success");
        }
        refresh();
        if (currentDetailId === savedId) {
          openDetail(savedId);
        }
      })
      .catch(function (e) {
        showPathAliasError(e.message);
      });
  }

  function savePathAlias() {
    var raw = document.getElementById("path-alias-input").value.trim();
    submitPathAlias(raw || null);
  }

  function clearPathAlias() {
    submitPathAlias(null);
  }

  // ---- 轮询与刷新（WBS-23.13）----------------------------------------------

  function refresh() {
    apiFetch("/api/stats").then(renderStats).catch(function () {});
    apiFetch("/api/instances")
      .then(function (data) {
        renderInstances(data.instances);
        // 若详情抽屉打开，顺带刷新
        if (currentDetailId) {
          openDetail(currentDetailId);
        }
      })
      .catch(function () {});
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(refresh, POLL_MS);
  }

  // ---- 工具 ----------------------------------------------------------------

  function setText(id, text) {
    var el = document.getElementById(id);
    if (el) el.textContent = text;
  }

  function fmtBytes(n) {
    if (n == null) return "—";
    n = Number(n);
    var units = ["B", "KiB", "MiB", "GiB", "TiB"];
    for (var i = 0; i < units.length; i++) {
      if (Math.abs(n) < 1024) return n.toFixed(1) + units[i];
      n /= 1024;
    }
    return n.toFixed(1) + "PiB";
  }

  function isDateTimeKey(key) {
    return (
      key === "createdAt" ||
      key === "updatedAt" ||
      key === "lastHealthCheckAt" ||
      key === "lastStartedAt" ||
      key === "startedAt" ||
      key === "finishedAt"
    );
  }

  function formatLocalDateTime(value) {
    if (value == null) return "—";
    var text = String(value).trim();
    if (!text) return "—";

    var match = text.match(
      /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.\d+)?(?:(Z)|([+-])(\d{2}):?(\d{2}))?$/
    );
    if (match && (match[3] || match[4])) {
      return (
        match[1] +
        " " +
        match[2] +
        "(" +
        (match[3] ? "UTC" : formatUtcOffset(match[4], match[5], match[6])) +
        ")"
      );
    }

    var d = new Date(text);
    if (isNaN(d.getTime())) return text;
    return (
      d.getFullYear() +
      "-" +
      pad2(d.getMonth() + 1) +
      "-" +
      pad2(d.getDate()) +
      " " +
      pad2(d.getHours()) +
      ":" +
      pad2(d.getMinutes()) +
      ":" +
      pad2(d.getSeconds()) +
      "(" +
      formatOffsetMinutes(-d.getTimezoneOffset()) +
      ")"
    );
  }

  function formatUtcOffset(sign, hours, minutes) {
    var h = String(Number(hours));
    var m = Number(minutes);
    return "UTC" + sign + h + (m ? ":" + pad2(m) : "");
  }

  function formatOffsetMinutes(totalMinutes) {
    if (totalMinutes === 0) return "UTC";
    var sign = totalMinutes >= 0 ? "+" : "-";
    var abs = Math.abs(totalMinutes);
    var hours = Math.floor(abs / 60);
    var minutes = abs % 60;
    return "UTC" + sign + hours + (minutes ? ":" + pad2(minutes) : "");
  }

  function pad2(n) {
    return String(n).padStart(2, "0");
  }

  function esc(s) {
    if (s == null) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  var toastTimer = null;
  function toast(msg, kind) {
    var el = document.getElementById("toast");
    el.textContent = msg;
    el.className = "toast" + (kind ? " toast-" + kind : "");
    el.hidden = false;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      el.hidden = true;
    }, 3000);
  }

  if (typeof window !== "undefined" && window.__LWA_TEST_HOOKS__) {
    window.__LWA_TEST_HOOKS__.formatLocalDateTime = formatLocalDateTime;
    window.__LWA_TEST_HOOKS__.statusLabel = statusLabel;
    window.__LWA_TEST_HOOKS__.badgeHtml = badgeHtml;
    window.__LWA_TEST_HOOKS__.isActionableStatus = isActionableStatus;
  }

  // ---- 启动 ----------------------------------------------------------------

  function start() {
    // 版本号
    apiFetch("/api/health").then(function (data) {
      setText("version", data.version || "");
    });

    // 绑定全局事件
    document.getElementById("instances-body").onclick = handleOpsClick;
    document.getElementById("refresh-btn").onclick = refresh;
    document.getElementById("filter-pending").onchange = function () {
      renderInstances(lastInstances);
    };
    document.getElementById("drawer-close").onclick = closeDetail;
    document.getElementById("drawer-mask").onclick = closeDetail;
    document.getElementById("logs-close").onclick = closeLogs;
    document.getElementById("logs-refresh").onclick = function () {
      var modal = document.getElementById("logs-modal");
      fetchLogs(modal.dataset.instanceId, document.getElementById("logs-category").value);
    };
    document.getElementById("logs-category").onchange = function () {
      var modal = document.getElementById("logs-modal");
      fetchLogs(modal.dataset.instanceId, this.value);
    };
    document.onkeydown = function (e) {
      if (e.key === "Escape") {
        closeLogs();
        closePathAlias();
        closeDetail();
      }
    };

    document.getElementById("path-alias-close").onclick = closePathAlias;
    document.getElementById("path-alias-cancel").onclick = closePathAlias;
    document.getElementById("path-alias-save").onclick = savePathAlias;
    document.getElementById("path-alias-clear").onclick = clearPathAlias;
    document.getElementById("path-alias-input").onkeydown = function (e) {
      if (e.key === "Enter") savePathAlias();
    };

    refresh();
    startPolling();
  }

  // 入口
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      requireToken(start);
    });
  } else {
    requireToken(start);
  }
})();
