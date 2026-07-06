/* Local Web Access Manager — 前端逻辑（WBS-23）。
   原生 JS，无框架无构建。15s 低频轮询（WBS-23.13）。 */

(function () {
  "use strict";

  var TOKEN_KEY = "lwa-token";
  var POLL_MS = 15000; // 15s（WBS-23.13：10-30s 区间）
  var pollTimer = null;
  var lastInstances = [];
  var currentDetailId = null;

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
    opts.headers["Authorization"] = "Bearer " + getToken();
    return fetch(path, opts).then(function (resp) {
      if (resp.status === 401) {
        // token 失效，重新要求输入
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

  function renderInstances(instances) {
    lastInstances = instances || [];
    var filterPending = document.getElementById("filter-pending").checked;
    var rows = lastInstances;
    if (filterPending) {
      rows = rows.filter(function (i) {
        return i.status === "pending" || i.status === "failed";
      });
    }
    var body = document.getElementById("instances-body");
    if (!rows.length) {
      body.innerHTML =
        '<tr class="empty-row"><td colspan="13">' +
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
    return (
      '<tr class="' + rowClass + '">' +
      '<td class="cell-name" data-detail="' + esc(i.id) + '">' + esc(i.name || i.id) + "</td>" +
      "<td>" + badgeHtml(i.status) + "</td>" +
      '<td class="cell-muted">' + esc(i.desiredState || "—") + "</td>" +
      '<td class="cell-muted">' + esc(i.servingMode || "—") + "</td>" +
      "<td>" + esc(i.kind || "—") + "</td>" +
      '<td class="cell-muted">' + esc(i.runtime || "—") + "</td>" +
      "<td>" + stackHtml(i.stack) + "</td>" +
      '<td class="cell-muted">' + esc(i.database || "—") + "</td>" +
      '<td class="cell-url">' + urlHtml(i.lanUrl) + "</td>" +
      "<td>" + portHtml(i.hostPort, i.internalPort) + "</td>" +
      '<td class="cell-muted">' + resourceHtml(i) + "</td>" +
      '<td class="cell-muted">' + esc(i.updatedAt || "—") + "</td>" +
      '<td class="col-ops"><div class="ops">' + opsHtml(i) + "</div></td>" +
      "</tr>"
    );
  }

  function badgeHtml(status) {
    var cls = "badge-" + (status || "pending");
    return '<span class="badge ' + cls + '">' + esc(status || "—") + "</span>";
  }

  function stackHtml(stack) {
    if (!stack || !stack.length) return '<span class="cell-muted">—</span>';
    return stack
      .slice(0, 4)
      .map(function (s) {
        return '<span class="stack-tag">' + esc(s) + "</span>";
      })
      .join("");
  }

  function urlHtml(lanUrl) {
    if (!lanUrl) return '<span class="cell-muted">—</span>';
    return (
      '<a href="' + esc(lanUrl) + '" target="_blank" rel="noopener">打开</a>'
    );
  }

  function portHtml(hostPort, internalPort) {
    if (!hostPort) return '<span class="cell-muted">—</span>';
    if (internalPort && String(internalPort) !== String(hostPort)) {
      return esc(hostPort + "→" + internalPort);
    }
    return esc(String(hostPort));
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
    var html = "";
    if (i.lanUrl) {
      html +=
        '<a class="btn btn-sm btn-ghost" href="' +
        esc(i.lanUrl) +
        '" target="_blank" rel="noopener">打开</a>';
    }
    html += opBtn(id, "logs", "日志", false);
    html += opBtn(id, "start", "启动", isRunning);
    html += opBtn(id, "stop", "停止", !isRunning);
    html += opBtn(id, "restart", "重启", false);
    html += opBtn(id, "rebuild", "重建", false);
    return html;
  }

  function opBtn(id, op, label, disabled) {
    return (
      '<button class="btn btn-sm" data-op="' +
      op +
      '" data-id="' +
      id +
      '"' +
      (disabled ? " disabled" : "") +
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
      { start: "启动", stop: "停止", restart: "重启", rebuild: "重建" }[op] || op
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
          ])
        );
      }
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
                esc(b.startedAt || b.started_at || "") +
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
                esc(ev.createdAt || ev.created_at || "") +
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

  // ---- 启动 ----------------------------------------------------------------

  function start() {
    // 版本号
    apiFetch("/api/health").then(function (data) {
      setText("version", data.version ? "v" + data.version : "");
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
        closeDetail();
      }
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
