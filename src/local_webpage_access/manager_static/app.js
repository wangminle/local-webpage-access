/* Local Webpage Access Manager — Vue 3 前端（DEV-046）。

通过 importmap 以 ESM 引入 Vue 3（无 npm build），把原生 app.js 重写为
响应式组件：状态/轮询/弹窗由 Vue 管理，表格行仍复用 helpers.js 的纯函数
（``window.LWA.rowHtml``）经 ``v-html`` 渲染，保证输出与原生版本逐字节一致，
降低迁移期视觉回归风险。

依赖注入（``createManagerApp(vue, deps)``）：``vue.createApp`` 由 importmap 提供；
``deps`` 注入 document/fetch/location 等宿主能力，使本工厂可在 Node vm 中用
桩对象构造（不真正挂载），用于冒烟测试（见 test_manager_static_app.py）。
真正挂载由 boot.js 完成。 */

(function () {
  "use strict";

  var POLL_MS = 15000;
  var TOKEN_KEY = "lwa-token";

  function createManagerApp(vue, deps) {
    var LWA = (typeof window !== "undefined" && window.LWA) || {};
    deps = deps || {};
    var doc = deps.document || (typeof document !== "undefined" ? document : null);
    var fetchFn = deps.fetch || (typeof fetch !== "undefined" ? fetch : null);
    var loc = deps.location || (typeof location !== "undefined" ? location : null);
    var storage =
      deps.sessionStorage ||
      (typeof sessionStorage !== "undefined" ? sessionStorage : null);
    var historyObj = deps.history || (typeof history !== "undefined" ? history : null);
    var setIntervalFn = deps.setInterval || (typeof setInterval !== "undefined" ? setInterval : function () { return 0; });
    var setTimeoutFn = deps.setTimeout || (typeof setTimeout !== "undefined" ? setTimeout : function (fn) { fn(); });
    var clearTimeoutFn = deps.clearTimeout || (typeof clearTimeout !== "undefined" ? clearTimeout : function () {});
    var clearIntervalFn = deps.clearInterval || (typeof clearInterval !== "undefined" ? clearInterval : function () {});
    var URLSearchParamsCtor =
      deps.URLSearchParams ||
      (typeof URLSearchParams !== "undefined" ? URLSearchParams : null);

    // ---- token ----

    function isLocalhostAccess() {
      if (!loc) return true;
      var h = loc.hostname;
      return h === "localhost" || h === "127.0.0.1" || h === "[::1]";
    }

    function getToken() {
      var stored = storage ? storage.getItem(TOKEN_KEY) : null;
      if (stored) return stored;
      if (loc && URLSearchParamsCtor) {
        var params = new URLSearchParamsCtor(loc.search);
        var fromUrl = params.get("token");
        if (fromUrl) {
          if (storage) storage.setItem(TOKEN_KEY, fromUrl);
          params.delete("token");
          var clean = params.toString();
          if (historyObj) {
            historyObj.replaceState(
              null,
              "",
              loc.pathname + (clean ? "?" + clean : "")
            );
          }
          return fromUrl;
        }
      }
      return null;
    }

    // ---- API ----

    function apiFetch(self, path, opts) {
      opts = opts || {};
      opts.headers = opts.headers || {};
      var token = getToken();
      if (token) opts.headers["Authorization"] = "Bearer " + token;
      return fetchFn(path, opts).then(function (resp) {
        if (resp.status === 401) {
          if (isLocalhostAccess()) throw new Error("unauthorized");
          if (storage) storage.removeItem(TOKEN_KEY);
          self.toast("token 无效，请重新输入", "error");
          setTimeoutFn(function () {
            self.requireToken();
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

    // ---- 详情/弹窗渲染辅助（HTML 字符串，经 v-html 注入）----

    function section(title, content) {
      return (
        '<div class="detail-section"><h3>' +
        LWA.esc(title) +
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
        else if (LWA.isDateTimeKey(key)) val = LWA.formatLocalDateTime(val);
        dl += "<dt>" + LWA.esc(label) + "</dt><dd>" + LWA.esc(String(val)) + "</dd>";
      });
      return dl + "</dl>";
    }

    var opLabels = {
      start: "启动",
      stop: "停止",
      restart: "重启",
      rebuild: "重建",
      recover: "恢复",
      remove: "删除",
    };
    function opLabel(op) {
      return opLabels[op] || op;
    }

    function renderDetailHtml(data) {
      var inst = data.instance || {};
      var manifest = data.manifest || {};
      var html = "";
      html += section(
        "基本信息",
        kvList(inst, [
          ["id", "ID"], ["name", "名称"], ["status", "状态"],
          ["desiredState", "期望状态"], ["kind", "技术族"], ["runtime", "运行层"],
          ["lanUrl", "访问地址"], ["hostPort", "宿主端口"], ["internalPort", "内部端口"],
          ["portMappingLabel", "端口映射"], ["routeHost", "路径别名"],
          ["routeUrl", "路径入口"], ["lastError", "最近错误"],
          ["lastHealthCheckAt", "最近健康检查"], ["updatedAt", "更新时间"],
        ])
      );
      if (manifest && !manifest._error) {
        html += section(
          "local-web.json 摘要",
          kvList(manifest, [
            ["id", "ID"], ["name", "名称"], ["version", "版本"], ["kind", "技术族"],
            ["runtime", "运行层"], ["servingMode", "服务模式"], ["resourceProfile", "资源档位"],
          ]) +
            (manifest.stack && manifest.stack.length
              ? '<div class="detail-kv"><dt>技术栈</dt><dd>' +
                manifest.stack.map(LWA.esc).join("、 ") +
                "</dd></div>"
              : "")
        );
        if (manifest.container) {
          html += section(
            "容器配置",
            kvList(manifest.container, [
              ["image", "镜像"], ["hostPort", "宿主端口"], ["internalPort", "内部端口"],
            ])
          );
        }
        if (manifest.static) {
          html += section(
            "静态配置",
            kvList(manifest.static, [
              ["root", "根目录"], ["gateway", "网关"], ["hostPort", "宿主端口"],
              ["routeMode", "路由模式"], ["routeHost", "路径别名"],
            ])
          );
        }
      }
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
            LWA.esc(inst.routeHost) +
            "/</code>" +
            (inst.routeUrl
              ? ' · <a href="' + LWA.esc(inst.routeUrl) +
                '" target="_blank" rel="noopener">打开路径入口</a>'
              : "") +
            "</p>" +
            '<p class="detail-hint cell-muted">可在操作区「路径别名」在线修改；原地更新 zip 会保留当前别名。</p>'
        );
      }
      if (data.builds && data.builds.length) {
        html += section(
          "构建记录",
          '<ul class="detail-events">' +
            data.builds.slice(0, 8).map(function (b) {
              return (
                "<li><time>" +
                LWA.esc(LWA.formatLocalDateTime(b.startedAt || b.started_at)) +
                "</time>" + LWA.esc(b.status || "") +
                (b.errorSummary || b.error_summary
                  ? " — " + LWA.esc(b.errorSummary || b.error_summary) : "") +
                "</li>"
              );
            }).join("") + "</ul>"
        );
      }
      if (data.events && data.events.length) {
        html += section(
          "最近事件",
          '<ul class="detail-events">' +
            data.events.slice(0, 15).map(function (ev) {
              return (
                "<li><time>" +
                LWA.esc(LWA.formatLocalDateTime(ev.createdAt || ev.created_at)) +
                "</time>[" + LWA.esc(ev.eventType || ev.event_type || "") + "] " +
                LWA.esc(ev.message || "") + "</li>"
              );
            }).join("") + "</ul>"
        );
      }
      return html;
    }

    function renderPageviewHtml(id, data, pv) {
      var byDay = data.byDay || [];
      var recent = data.recent || [];
      var total = (pv && pv.hits) || byDay.reduce(function (a, d) { return a + (d.hits || 0); }, 0);
      var html = "";
      html += section(
        "概要",
        '<dl class="detail-kv">' +
          "<dt>累计访问</dt><dd>" + Number(total).toLocaleString() + " 次</dd>" +
          "<dt>独立 IP</dt><dd>" + ((pv && pv.uniqueIps) || 0) + " 个</dd>" +
          "<dt>数据来源</dt><dd>" + LWA.esc(LWA.sourceLabel((pv && pv.source) || data.source)) + "</dd>" +
          "<dt>最近访问</dt><dd>" + LWA.esc(LWA.formatLocalDateTime(pv && pv.lastSeen)) + "</dd>" +
          "</dl>"
      );
      if (byDay.length) {
        var maxHits = Math.max.apply(null, byDay.map(function (d) { return d.hits; }));
        html += section(
          "按天分布（近 30 天）",
          '<ul class="pv-bars">' +
            byDay.map(function (d) {
              var pct = maxHits ? Math.round((d.hits / maxHits) * 100) : 0;
              return (
                '<li><span class="pv-day">' + LWA.esc(d.day) +
                '</span><span class="pv-track"><span class="pv-fill" style="width:' +
                pct + '%"></span></span><span class="pv-num">' + d.hits + "</span></li>"
              );
            }).join("") + "</ul>"
        );
      }
      if (recent.length) {
        html += section(
          "最近命中（" + recent.length + " 条）",
          '<table class="pv-recent"><thead><tr><th>时间</th><th>方法</th><th>路径</th><th>状态</th><th>来源 IP</th></tr></thead><tbody>' +
            recent.map(function (r) {
              return (
                "<tr><td>" + LWA.esc(LWA.formatLocalDateTime(r.ts)) +
                "</td><td>" + LWA.esc(r.method || "") +
                "</td><td>" + LWA.esc(r.path || "") +
                "</td><td>" + LWA.esc(String(r.status || "")) +
                "</td><td>" + LWA.esc(r.remote || "") + "</td></tr>"
              );
            }).join("") + "</tbody></table>"
        );
      } else {
        html += section("最近命中", '<p class="cell-muted">暂无明细记录。</p>');
      }
      html +=
        '<p class="detail-hint cell-muted">数据为按日志惰性统计的近似值：静态站点读网关/访问日志，'
        + "容器实例尽力解析应用 access 行。访问量统计不影响业务运行。</p>";
      return html;
    }

    // ---- 根组件 ----

    var root = {
      data: function () {
        return {
          version: "",
          stats: {
            counts: { total: 0, running: 0, stopped: 0, pending: 0, failed: 0, gateway_down: 0, config_invalid: 0 },
            typeDistribution: {},
            databaseCount: 0,
            portPool: { allocated: 0, total: 0, start: 0, end: 0 },
            host: {},
          },
          instances: [],
          pageviewMap: {},
          filters: { search: "", status: "", form: "", pending: false, redundant: false },
          // 弹窗/抽屉状态
          needToken: false,
          tokenInput: "",
          drawer: { open: false, title: "", body: "" },
          currentDetailId: null,
          logs: { open: false, title: "", category: "run", content: "", instanceId: null },
          pathAlias: { open: false, title: "", value: "", error: "", instanceId: null },
          pageview: { open: false, title: "", body: "", instanceId: null },
          toastState: { show: false, msg: "", kind: "" },
          _detailReq: 0, // 详情请求竞态令牌（旧响应到达时丢弃）
          _pageviewReq: 0,
        };
      },
      computed: {
        counts: function () {
          return (this.stats && this.stats.counts) || {};
        },
        needsRecover: function () {
          var c = this.counts;
          return (c.gateway_down || 0) + (c.config_invalid || 0);
        },
        statPortsText: function () {
          var pp = this.stats.portPool || {};
          return (pp.allocated || 0) + " / " + (pp.total || 0) + " 已分配（" +
            (pp.start || 0) + "-" + (pp.end || 0) + "）";
        },
        statTypesText: function () {
          var t = this.stats.typeDistribution || {};
          var keys = Object.keys(t);
          if (!keys.length) return "—";
          return keys.map(function (k) { return k + " ×" + t[k]; }).join("， ");
        },
        statMemText: function () {
          var h = this.stats.host || {};
          return h.memTotalBytes
            ? LWA.fmtBytes(h.memUsedBytes) + " / " + LWA.fmtBytes(h.memTotalBytes)
            : "（非 Linux，已跳过）";
        },
        statDiskText: function () {
          var h = this.stats.host || {};
          return h.diskTotalBytes
            ? LWA.fmtBytes(h.diskUsedBytes) + " / " + LWA.fmtBytes(h.diskTotalBytes)
            : "—";
        },
        filteredInstances: function () {
          return LWA.applyFilters(this.instances, this.filters);
        },
        tbodyHtml: function () {
          var self = this;
          if (!this.instances.length) {
            return '<tr class="empty-row"><td colspan="13">' +
              (this.instances.length ? "没有匹配的实例" : "暂无实例，把 zip 放进 inbox/ 或用 lwa import 导入") +
              "</td></tr>";
          }
          var rows = this.filteredInstances;
          if (!rows.length) {
            return '<tr class="empty-row"><td colspan="13">没有匹配的实例</td></tr>';
          }
          return rows.map(function (i) { return LWA.rowHtml(i, self.pageviewMap); }).join("");
        },
        redundantCount: function () {
          return this.instances.filter(function (i) { return i.redundant; }).length;
        },
      },
      methods: {
        // ---- token ----
        requireToken: function () {
          if (isLocalhostAccess()) {
            this.bootstrap();
            return;
          }
          var token = getToken();
          if (token) {
            this.bootstrap();
            return;
          }
          this.needToken = true;
          var self = this;
          setTimeoutFn(function () {
            var el = doc.getElementById("token-input");
            if (el) el.focus();
          }, 0);
        },
        submitToken: function () {
          var val = this.tokenInput.trim();
          if (!val) return;
          if (storage) storage.setItem(TOKEN_KEY, val);
          this.needToken = false;
          this.bootstrap();
        },

        // ---- 启动 ----
        bootstrap: function () {
          var self = this;
          // 幂等：401 后 requireToken→submitToken 会再次进 bootstrap；先清旧定时器，
          // 避免多个轮询叠加（每次刷新重复请求、负载翻倍）。
          if (this._timer) { clearIntervalFn(this._timer); this._timer = null; }
          apiFetch(this, "/api/health").then(function (data) {
            self.version = data.version || "";
          }).catch(function () {});
          this.refresh();
          this._timer = setIntervalFn(function () { self.refresh(); }, POLL_MS);
        },

        // ---- 刷新 ----
        refresh: function () {
          var self = this;
          apiFetch(this, "/api/stats").then(function (data) {
            self.stats = data;
          }).catch(function () {});
          apiFetch(this, "/api/pageviews").then(function (data) {
            self.pageviewMap = (data && data.instances) || {};
          }).catch(function () {});
          apiFetch(this, "/api/instances").then(function (data) {
            self.instances = data.instances || [];
            if (self.currentDetailId) self.openDetail(self.currentDetailId);
          }).catch(function () {});
        },

        // ---- 表格事件委托 ----
        // 先判 data-op 再判 data-detail：操作按钮（位于操作列，无 data-detail）
        // 优先；避免点按钮时冒泡到行名单元格的 data-detail 误开详情。
        onTableClick: function (e) {
          var btn = e.target.closest ? e.target.closest("[data-op]") : null;
          if (btn) {
            var op = btn.getAttribute("data-op");
            var id = btn.getAttribute("data-id");
            if (op === "logs") { this.openLogs(id); return; }
            if (op === "path-alias") { this.openPathAlias(id); return; }
            if (op === "remove") { this.removeSingleInstance(id); return; }
            if (op === "pageview") { this.openPageview(id); return; }
            this.doOperation(id, op);
            return;
          }
          var detailEl = e.target.closest ? e.target.closest("[data-detail]") : null;
          if (detailEl) {
            this.openDetail(detailEl.getAttribute("data-detail"));
          }
        },

        doOperation: function (id, op) {
          var self = this;
          this.toast("正在" + opLabel(op) + "…");
          apiFetch(this, "/api/instances/" + encodeURIComponent(id) + "/" + op, { method: "POST" })
            .then(function () { self.toast(opLabel(op) + "完成", "success"); self.refresh(); })
            .catch(function (e) { self.toast(opLabel(op) + "失败：" + e.message, "error"); });
        },

        removeSingleInstance: function (id) {
          if (!confirm("确认删除实例 " + id + "？\n（停服并清理登记；保留 apps/ 目录便于排查。同源最早者会保留。）")) return;
          var self = this;
          this.toast("正在删除…");
          apiFetch(this, "/api/instances/" + encodeURIComponent(id) + "/remove", { method: "POST" })
            .then(function () {
              self.toast("已删除 " + id, "success");
              if (self.currentDetailId === id) self.closeDetail();
              self.refresh();
            })
            .catch(function (e) { self.toast("删除失败：" + e.message, "error"); });
        },

        removeRedundant: function () {
          var n = this.redundantCount;
          if (!n) { this.toast("当前没有冗余实例", "error"); return; }
          if (!confirm("确认批量删除 " + n + " 个冗余实例？\n（同源 zip 仅保留最早导入者；唯一实例不受影响。）")) return;
          var self = this;
          this.toast("正在批量清理冗余…");
          apiFetch(this, "/api/redundant/remove", { method: "POST" })
            .then(function (data) { self.toast("已清理 " + (data.count || 0) + " 个冗余实例", "success"); self.refresh(); })
            .catch(function (e) { self.toast("批量清理失败：" + e.message, "error"); });
        },

        // ---- 详情抽屉 ----
        openDetail: function (id) {
          var self = this;
          this.currentDetailId = id;
          var myReq = ++this._detailReq; // 竞态令牌：只接受最新一次请求的响应
          apiFetch(this, "/api/instances/" + encodeURIComponent(id))
            .then(function (data) {
              if (self._detailReq !== myReq) return; // 已有更新的请求，丢弃旧响应
              var inst = data.instance || {};
              self.drawer.title = inst.name || inst.id || "实例详情";
              self.drawer.body = renderDetailHtml(data);
              self.drawer.open = true;
            })
            .catch(function (e) {
              if (self._detailReq !== myReq) return;
              self.toast("加载详情失败：" + e.message, "error");
            });
        },
        closeDetail: function () {
          this.currentDetailId = null;
          this.drawer.open = false;
        },

        // ---- 日志 ----
        openLogs: function (id, category) {
          this.logs.instanceId = id;
          this.logs.title = "日志：" + id;
          this.logs.category = category || "run";
          this.logs.open = true;
          this.fetchLogs(id, this.logs.category);
        },
        fetchLogs: function (id, category) {
          var self = this;
          apiFetch(this, "/api/instances/" + encodeURIComponent(id) +
            "/logs?category=" + encodeURIComponent(category) + "&tail=300")
            .then(function (data) { self.logs.content = data.content || "（日志为空）"; })
            .catch(function (e) { self.logs.content = "加载失败：" + e.message; });
        },
        onLogsCategoryChange: function () {
          this.fetchLogs(this.logs.instanceId, this.logs.category);
        },
        closeLogs: function () { this.logs.open = false; },

        // ---- 路径别名 ----
        openPathAlias: function (id) {
          this.pathAlias.instanceId = id;
          var inst = this.instances.filter(function (x) { return x.id === id; })[0];
          this.pathAlias.value = (inst && inst.routeHost) || "";
          this.pathAlias.error = "";
          this.pathAlias.title = "路径别名 · " + id;
          this.pathAlias.open = true;
        },
        closePathAlias: function () { this.pathAlias.open = false; this.pathAlias.error = ""; },
        submitPathAlias: function (alias) {
          if (!this.pathAlias.instanceId) return;
          var savedId = this.pathAlias.instanceId;
          var self = this;
          this.pathAlias.error = "";
          apiFetch(this, "/api/instances/" + encodeURIComponent(savedId) + "/path-alias", {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ alias: alias }),
          })
            .then(function (data) {
              self.pathAlias.open = false;
              if (data.aliasEntryEnabled === false && data.alias) {
                self.toast("别名已保存（builtin 模式仅端口可达）", "success");
              } else {
                self.toast("路径别名已更新", "success");
              }
              self.refresh();
              if (self.currentDetailId === savedId) self.openDetail(savedId);
            })
            .catch(function (e) { self.pathAlias.error = e.message; });
        },
        savePathAlias: function () {
          this.submitPathAlias(this.pathAlias.value.trim() || null);
        },
        clearPathAlias: function () { this.submitPathAlias(null); },

        // ---- 浏览量详情 ----
        openPageview: function (id) {
          var self = this;
          this.pageview.instanceId = id;
          this.pageview.title = "浏览量 · " + id;
          this.pageview.body = '<p class="cell-muted">加载中…</p>';
          this.pageview.open = true;
          var myReq = ++this._pageviewReq; // 竞态令牌
          apiFetch(this, "/api/instances/" + encodeURIComponent(id) + "/pageviews?limit=50")
            .then(function (data) {
              if (self._pageviewReq !== myReq) return;
              self.pageview.body = renderPageviewHtml(id, data, self.pageviewMap[id]);
            })
            .catch(function (e) {
              if (self._pageviewReq !== myReq) return;
              self.pageview.body = '<p class="cell-muted">加载失败：' + LWA.esc(e.message) + "</p>";
            });
        },
        closePageview: function () { this.pageview.open = false; },

        // ---- 全局键盘 ----
        // Escape 只关最上层一层（而非一次全关），符合弹窗层叠直觉，
        // 也避免在多弹窗叠加时一按 Esc 把背后的抽屉一起关掉。
        onKeydown: function (e) {
          if (e.key !== "Escape") return;
          if (this.pageview.open) this.closePageview();
          else if (this.pathAlias.open) this.closePathAlias();
          else if (this.logs.open) this.closeLogs();
          else if (this.drawer.open) this.closeDetail();
        },

        // ---- toast ----
        toast: function (msg, kind) {
          this.toastState = { show: true, msg: msg, kind: kind || "" };
          var self = this;
          if (this._toastTimer) clearTimeoutFn(this._toastTimer);
          this._toastTimer = setTimeoutFn(function () {
            self.toastState = { show: false, msg: "", kind: "" };
          }, 3000);
        },

        esc: LWA.esc,
        statusLabel: LWA.statusLabel,
      },
      template: ROOT_TEMPLATE,
      mounted: function () {
        this.requireToken();
        if (doc) doc.addEventListener("keydown", this.onKeydown);
      },
      unmounted: function () {
        if (this._timer) clearIntervalFn(this._timer);
        if (doc) doc.removeEventListener("keydown", this.onKeydown);
      },
    };

    var app = vue.createApp(root);
    return {
      app: app,
      _root: root,
      mount: function (el) {
        return app.mount(el);
      },
    };
  }

  // ---- 根模板（复刻 index.html 主体结构，动态部分由 Vue 绑定）----
  // 行/单元格经 v-html 注入 window.LWA.* 输出，与原生版本逐字节一致。
  var ROOT_TEMPLATE = [
    '<header class="topbar">',
    '  <div class="topbar-title">',
    '    <h1>Local Webpage Access</h1>',
    '    <span class="version">{{ version }}</span>',
    "  </div>",
    '  <div class="topbar-actions">',
    '    <button class="btn btn-ghost" title="立即刷新" @click="refresh">↻ 刷新</button>',
    "  </div>",
    "</header>",
    // 顶部统计
    '<section class="stats" aria-label="整机与实例统计">',
    '  <div class="stat-cards">',
    '    <div class="stat-card"><span class="stat-label">实例</span><span class="stat-value">{{ counts.total || 0 }}</span></div>',
    '    <div class="stat-card stat-running"><span class="stat-label">运行中</span><span class="stat-value">{{ counts.running || 0 }}</span></div>',
    '    <div class="stat-card stat-stopped"><span class="stat-label">已停止</span><span class="stat-value">{{ counts.stopped || 0 }}</span></div>',
    '    <div class="stat-card stat-pending"><span class="stat-label">待处理</span><span class="stat-value">{{ counts.pending || 0 }}</span></div>',
    '    <div class="stat-card stat-failed"><span class="stat-label">失败</span><span class="stat-value">{{ counts.failed || 0 }}</span></div>',
    '    <div class="stat-card stat-warn"><span class="stat-label">需恢复</span><span class="stat-value">{{ needsRecover }}</span></div>',
    '    <div class="stat-card"><span class="stat-label">数据库实例</span><span class="stat-value">{{ stats.databaseCount || 0 }}</span></div>',
    "  </div>",
    '  <div class="stat-detail">',
    '    <div class="stat-line"><span>端口池</span><span>{{ statPortsText }}</span></div>',
    '    <div class="stat-line"><span>类型分布</span><span>{{ statTypesText }}</span></div>',
    '    <div class="stat-line"><span>整机内存</span><span>{{ statMemText }}</span></div>',
    '    <div class="stat-line"><span>整机磁盘</span><span>{{ statDiskText }}</span></div>',
    "  </div>",
    "</section>",
    // 实例列表
    '<main class="main"><section class="panel">',
    '  <div class="panel-head">',
    "    <h2>实例</h2>",
    '    <div class="filter">',
    '      <input type="search" class="filter-input" placeholder="搜索名称 / ID / 技术栈…" autocomplete="off" v-model="filters.search" />',
    '      <select class="filter-select" title="按状态筛选" v-model="filters.status">',
    '        <option value="">全部状态</option>',
    '        <option value="running">运行中</option><option value="stopped">已停止</option>',
    '        <option value="pending">待识别</option><option value="building">构建中</option>',
    '        <option value="failed">失败</option><option value="queued">排队中</option>',
    '        <option value="gateway_down">网关不可达</option><option value="config_invalid">配置无效</option>',
    "      </select>",
    '      <select class="filter-select" title="按形态筛选" v-model="filters.form">',
    '        <option value="">全部形态</option>',
    '        <option value="shared-static">静态站点</option><option value="container">容器</option>',
    "      </select>",
    '      <label><input type="checkbox" v-model="filters.pending" /> 仅待处理/失败</label>',
    '      <label><input type="checkbox" v-model="filters.redundant" /> 仅冗余</label>',
    '      <button class="btn btn-sm btn-warn" title="移除同包重复导入的冗余实例（保留每组最早者），不删最早者与唯一实例" @click="removeRedundant">批量删除冗余</button>',
    "    </div>",
    "  </div>",
    '  <div class="table-wrap">',
    '    <table class="instances">',
    "      <thead><tr>",
    "        <th>名称</th><th>状态</th><th>期望</th><th>形态</th><th>族</th><th>运行层</th>",
    '        <th>技术栈</th><th>访问地址</th><th>端口</th><th>资源</th><th>浏览量</th><th>更新时间</th><th class="col-ops">操作</th>',
    "      </tr></thead>",
    '      <tbody v-html="tbodyHtml" @click="onTableClick"></tbody>',
    "    </table>",
    "  </div>",
    "</section></main>",
    // 详情抽屉
    '<aside class="drawer" :hidden="!drawer.open" :aria-hidden="String(!drawer.open)">',
    '  <div class="drawer-head"><h2>{{ drawer.title || "实例详情" }}</h2>',
    '    <button class="btn btn-ghost" title="关闭" @click="closeDetail">✕</button></div>',
    '  <div class="drawer-body" v-html="drawer.body"></div>',
    "</aside>",
    '<div class="drawer-mask" :hidden="!drawer.open" @click="closeDetail"></div>',
    // 日志
    '<div class="modal" :hidden="!logs.open">',
    '  <div class="modal-inner"><div class="modal-head">',
    '    <h2>{{ logs.title }}</h2>',
    '    <div class="logs-controls">',
    '      <select v-model="logs.category" @change="onLogsCategoryChange">',
    '        <option value="run">run</option><option value="build">build</option><option value="gateway">gateway</option><option value="import">import</option><option value="scan">scan</option>',
    "      </select>",
    '      <button class="btn btn-ghost" @click="fetchLogs(logs.instanceId, logs.category)">↻</button>',
    '      <button class="btn btn-ghost" @click="closeLogs">✕</button>',
    "    </div></div>",
    '    <pre class="logs-content">{{ logs.content }}</pre>',
    "  </div></div>",
    // token
    '<div class="modal" :hidden="!needToken">',
    '  <div class="token-box"><h2>需要 API token</h2>',
    '    <p>请输入管理页 token（由 <code>lwa manager on</code> / <code>lwa manager start</code> 输出；本机 127.0.0.1 访问免 token）：</p>',
    '    <input type="password" placeholder="API token" autocomplete="off" v-model="tokenInput" @keydown.enter="submitToken" />',
    '    <div class="token-actions"><button class="btn btn-primary" @click="submitToken">进入</button></div>',
    '    <p class="token-hint">token 保存在浏览器 sessionStorage，关闭标签页即清除。</p>',
    "  </div></div>",
    // 路径别名
    '<div class="modal" :hidden="!pathAlias.open">',
    '  <div class="modal-inner path-alias-box"><div class="modal-head">',
    '    <h2>{{ pathAlias.title }}</h2>',
    '    <button class="btn btn-ghost" type="button" @click="closePathAlias">✕</button></div>',
    '    <p class="path-alias-hint">设置后可通过统一入口 <code>http://&lt;LAN-IP&gt;:&lt;staticGatewayPort&gt;/&lt;slug&gt;/</code> 访问（Caddy 模式）。</p>',
    '    <label class="path-alias-field"><span>别名 slug</span>',
    '      <input type="text" placeholder="my-app-demo" autocomplete="off" spellcheck="false" v-model="pathAlias.value" @keydown.enter="savePathAlias" /></label>',
    '    <p class="path-alias-error" :hidden="!pathAlias.error">{{ pathAlias.error }}</p>',
    '    <div class="path-alias-actions">',
    '      <button class="btn btn-ghost" type="button" @click="clearPathAlias">清除别名</button>',
    '      <div class="path-alias-actions-main">',
    '        <button class="btn btn-ghost" type="button" @click="closePathAlias">取消</button>',
    '        <button class="btn btn-primary" type="button" @click="savePathAlias">保存</button>',
    "      </div></div></div></div>",
    // 浏览量
    '<div class="modal" :hidden="!pageview.open">',
    '  <div class="modal-inner pageview-box"><div class="modal-head">',
    '    <h2>{{ pageview.title }}</h2>',
    '    <button class="btn btn-ghost" type="button" @click="closePageview">✕</button></div>',
    '    <div class="pageview-body" v-html="pageview.body"></div>',
    "  </div></div>",
    // toast
    '<div class="toast" :hidden="!toastState.show" :class="toastState.kind ? \'toast toast-\' + toastState.kind : \'toast\'">{{ toastState.msg }}</div>',
  ].join("\n");

  // ---- 导出 ----
  if (typeof window !== "undefined") {
    window.LWA = window.LWA || {};
    window.LWA.createManagerApp = createManagerApp;
  }
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { createManagerApp: createManagerApp };
  }
})();
