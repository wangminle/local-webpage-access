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
    // 评审-组9#8：补单引号转义（当前数据源受后端枚举约束，属防御性补全）
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
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
    cancelling: "取消中",
    cancelled: "已取消",
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
    // 浏览量按钮：图标/数字入口须有可访问名称（BUG-170）
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
      '" aria-label="' +
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
    var accessible = title || label;
    return (
      '<button class="btn btn-sm" data-op="' +
      op +
      '" data-id="' +
      id +
      '"' +
      (disabled ? " disabled" : "") +
      (title ? ' title="' + LWA.esc(title) + '"' : "") +
      (accessible ? ' aria-label="' + LWA.esc(accessible) + '"' : "") +
      ">" +
      label +
      "</button>"
    );
  };

  // ---- IMP-035：删除对话框状态机纯函数 ----

  LWA.canSubmitRemove = function (dlg) {
    if (!dlg || dlg.submitting) return false;
    if (dlg.step !== 2 && dlg.step !== 3) return false;
    if (dlg.confirmId !== dlg.instanceId) return false;
    if (dlg.mode === "purge" && !dlg.acknowledgeIrreversible) return false;
    if (dlg.needForce && !dlg.acknowledgeForce) return false;
    return true;
  };

  LWA.shouldElevateRemoveForce = function (errorCode) {
    return errorCode === "data_nonempty";
  };

  LWA.buildRemoveQuery = function (purge, force) {
    return (
      "purge=" +
      (purge ? "true" : "false") +
      "&force=" +
      (force ? "true" : "false")
    );
  };

  LWA.opsHtml = function (i) {
    var id = LWA.esc(i.id);
    var isRunning = i.status === "running";
    var inProgress =
      i.status === "building" ||
      i.status === "cancelling" ||
      i.status === "queued" ||
      i.status === "pending";
    // IMP-035：删除在启停流转中也禁用（含 starting/stopping/removing）
    var removeBusy =
      inProgress ||
      i.status === "starting" ||
      i.status === "stopping" ||
      i.status === "removing";
    var supportsAlias =
      i.runtime === "shared-static" || i.runtime === "docker-compose";
    var canCancelBuild =
      i.status === "building" || i.status === "queued";
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
    // IMP-047：文件夹源实例显示「从源更新」按钮
    // BUG-445：pending 不算更新忙态——允许修好源码后从源更新自愈；
    // 启动仍由上方 inProgress 禁用。
    if (i.sourceKind === "folder") {
      var updateBusy =
        i.status === "building" ||
        i.status === "cancelling" ||
        i.status === "queued";
      html += LWA.opBtn(
        id,
        "update-from-dir",
        "从源更新",
        updateBusy,
        updateBusy ? "实例正在构建/流转，暂时不能更新" : "从关联文件夹源同步更新"
      );
    }
    // IMP-065：git 源实例显示「从源更新」（走 update-from-git API；
    // 与 folder 分流，folder 实例不会打到 git 端点）
    if (i.sourceKind === "git") {
      var gitUpdateBusy =
        i.status === "building" ||
        i.status === "cancelling" ||
        i.status === "queued";
      html += LWA.opBtn(
        id,
        "update-from-git",
        "从源更新",
        gitUpdateBusy,
        gitUpdateBusy
          ? "实例正在构建/流转，暂时不能更新"
          : "探测 GitHub 远端，有新提交时原地更新（无变更不重建）"
      );
    }
    if (canCancelBuild || i.status === "cancelling") {
      html += LWA.opBtn(
        id,
        "cancel-build",
        i.status === "cancelling" ? "取消中…" : "取消构建",
        i.status === "cancelling",
        i.status === "cancelling"
          ? "正在终止构建进程树"
          : "取消排队或进行中的构建（不删缓存/镜像/用户数据）"
      );
    }
    // IMP-035：所有实例显示删除入口（不再仅 redundant）
    html += LWA.opBtn(
      id,
      "remove",
      "删除",
      removeBusy,
      removeBusy
        ? "实例正在启停/构建，暂时不能删除"
        : "移除实例（可选择仅清 registry 或彻底删除项目文件）"
    );
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
    var displayName = LWA.esc(i.name || i.id);
    var nameCell =
      '<td class="cell-name">' +
      '<button type="button" class="cell-name-btn" data-detail="' +
      LWA.esc(i.id) +
      '" aria-label="查看 ' +
      displayName +
      ' 详情">' +
      displayName +
      (i.redundant
        ? ' <span class="redundant-badge" title="与同源 zip 的更早实例重复">冗余</span>'
        : "") +
      "</button></td>";
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

  // 管理页展示：剥掉 [ZIP_IMPORT_ERROR] 等 LwaError 前缀（code 已在 JSON 里）
  LWA.friendlyApiMessage = function (msg) {
    if (msg == null) return "";
    var text = String(msg);
    return text.replace(/^\[[A-Z][A-Z0-9_]*\]\s*/, "");
  };

  // IMP-051.b：文件夹导入结果文案（识别失败勿冒充成功）
  LWA.describeFolderImportOutcome = function (data) {
    return LWA._describeImportOutcome(data, {
      successPrefix: "已从文件夹导入：",
      pendingToast: "未能识别为可部署项目：",
      pendingHint:
        "常见原因：只选了 src/ 源码子目录——请改选项目根或 dist/，并删除待识别实例后重试。",
      pendingError:
        "未能识别该目录。请选择含 index.html / package.json / dist 的目录（不要只选 src/）。已创建的待识别实例可在列表中删除。",
    });
  };

  // IMP-065（065.24）：GitHub 导入结果文案（与 folder 同一 pending/成功护栏：
  // 真·未识别 status=pending 才报错；autoStart.action=pending 是成功——
  // 识别成功但档位 medium/heavy 不自动启动，不得误报「未能识别」）
  LWA.describeGitImportOutcome = function (data) {
    return LWA._describeImportOutcome(data, {
      successPrefix: "已从 GitHub 导入：",
      pendingToast: "未能识别为可部署项目：",
      pendingHint:
        "常见原因：仓库根没有可部署内容（只有文档/脚本），或 --subdir 指到了源码子目录（如 src/）。请删除待识别实例后，改用项目根目录或正确子目录重试。",
      pendingError:
        "未能识别该仓库。请确认仓库含 index.html / package.json / 可构建前端（子目录用「子目录」字段指定，不要指向 src/）。已创建的待识别实例可在列表中删除。",
    });
  };

  // 两种导入对话框共用的结果判定（BUG-449：仅 status=pending 才算未识别）
  LWA._describeImportOutcome = function (data, labels) {
    data = data || {};
    var id =
      data.instanceId ||
      (data.instance && (data.instance.id || data.instance.name)) ||
      "";
    var auto = data.autoStart || {};
    var status = (data.instance && data.instance.status) || "";
    // 只有 status=pending 才是真·未识别。autoStart.action="pending" 还覆盖
    // 「识别成功但档位 medium/heavy 不自动启动」——那种实例是 stopped 可启动的，
    // 绝不能报成「未能识别/请删除重试」（BUG-449）。
    if (status === "pending") {
      var note = auto.note || "未能识别项目类型";
      return {
        ok: false,
        toastKind: "error",
        toast: labels.pendingToast + note + "（实例 " + id + " 为待识别，无法启动）。" + labels.pendingHint,
        keepOpen: true,
        error: labels.pendingError,
      };
    }
    if (auto.action === "pending") {
      // 识别成功但资源档位较高，未自动启动：如实告知，可手动启动
      return {
        ok: true,
        toastKind: "success",
        toast:
          labels.successPrefix + id + "（" +
          (auto.note || "未自动启动，可在列表中手动启动") +
          "）",
        keepOpen: false,
        error: "",
      };
    }
    return {
      ok: true,
      toastKind: "success",
      toast: labels.successPrefix + id,
      keepOpen: false,
      error: "",
    };
  };

  // IMP-065（065.23）：git 源 errorKind 闭集 → 人话提示。
  // 未知 kind 走通用失败文案，不说「地址无效」（065.p）。
  LWA.gitErrorKindMessages = {
    invalid_url:
      "仓库地址无效：请使用 https://github.com/<owner>/<repo> 形式的仓库根地址（网页地址 /tree/、/blob/ 需改用「分支/标签」与「子目录」字段）",
    host_not_allowed:
      "仅支持 GitHub（github.com）仓库：请检查地址是否为 github.com 本站，且未带非 443 端口",
    userinfo_forbidden:
      "地址不能包含用户名/密码：私有仓凭据请配置在 LWA 宿主机的 git credential helper（不是这台浏览器所在的机器）",
    git_missing:
      "LWA 所在机器未安装 git：请在宿主机安装 git 后重试（zip / 文件夹导入不受影响）",
    remote_unreachable:
      "远端仓库不可达：请检查网络/代理（如需代理，在 LWA 宿主机配置 https_proxy 或 git http.proxy）；私有仓需宿主机已配置访问凭据",
    ref_not_found: "远端不存在该分支/标签：请核对「分支/标签」填写",
    clone_timeout: "克隆超时：仓库过大或网络过慢，请稍后重试",
    size_exceeded: "仓库超过 2 GiB 体积上限，无法导入",
    source_mismatch:
      "传入的仓库与该实例关联的仓库不一致：如需更换来源，请删除实例后重新导入",
  };

  LWA.describeGitError = function (err) {
    if (!err) return "";
    var kind =
      (err.detail && err.detail.kind) || err.kind || "";
    if (kind && LWA.gitErrorKindMessages[kind]) {
      return LWA.gitErrorKindMessages[kind];
    }
    // 未知 kind：通用失败，不冒充「地址无效」
    return err.message || "GitHub 导入失败，请稍后重试";
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
      window.__LWA_TEST_HOOKS__.canSubmitRemove = LWA.canSubmitRemove;
      window.__LWA_TEST_HOOKS__.shouldElevateRemoveForce = LWA.shouldElevateRemoveForce;
      window.__LWA_TEST_HOOKS__.buildRemoveQuery = LWA.buildRemoveQuery;
      window.__LWA_TEST_HOOKS__.describeFolderImportOutcome =
        LWA.describeFolderImportOutcome;
      window.__LWA_TEST_HOOKS__.describeGitImportOutcome =
        LWA.describeGitImportOutcome;
      window.__LWA_TEST_HOOKS__.describeGitError = LWA.describeGitError;
      window.__LWA_TEST_HOOKS__.friendlyApiMessage = LWA.friendlyApiMessage;
    }
  }
  if (typeof module !== "undefined" && module.exports) {
    module.exports = LWA;
  }
})();
