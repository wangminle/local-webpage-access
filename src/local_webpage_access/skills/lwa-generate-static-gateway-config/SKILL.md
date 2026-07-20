# lwa-generate-static-gateway-config

> 为静态实例（纯静态或构建后 SPA）生成共享网关**站点片段**（Caddy 优先）。
> 主 `Caddyfile`、别名入口与 access log 由 `lwa` / `StaticGateway` 组装，本 skill **不手写主配置**。

## 何时触发

- 实例 `runtime: shared-static`，但 `static-gateway/sites/<id>.conf` 不存在或与 `public/` 根不一致。
- 端口池已分配宿主端口，需要网关把请求转到静态产物。
- 用户问「怎么给静态站写 Caddy 配置」——优先引导用 `lwa start` / `lwa gateway switch` / `lwa gateway on`，仅在排障时手工核对片段。

## 输入

1. `local-web.json` 的 `static` 段（`root`、`hostPort`、`gateway`）。
2. 静态产物目录：通常为 `apps/<id>/public/`（或 manifest 中的 `static.root`）。
3. 工作区 `local-web.yml` 的 `staticGateway`（`caddy` / `builtin`；nginx 未充分验证）。

## 输出

- 期望存在：`static-gateway/sites/<id>.conf`（由 `StaticGateway.generate_site_config` 渲染模板）。
- 若启用路径别名：另有 `static-gateway/aliases/<id>.conf`（`reverse_proxy` 到 hostPort）。
- 主配置 `static-gateway/Caddyfile` 由 `lwa` 按磁盘上**实际存在**的片段组装并 reload（含统一入口 JSON access log，IMP-024）。

## 可修改文件

- 排障时仅核对 / 必要时触发生成：`static-gateway/sites/<id>.conf`、`static-gateway/aliases/<id>.conf`。
- `apps/<id>/local-web.json`（端口 / gateway 字段）。

## 禁止事项

- **不修改**网关主配置文件（`Caddyfile` / `nginx.conf`）—— 由 `lwa` 汇总各片段后 reload；手写易引入悬空 `import`（BUG-069）。
- **不手写**统一入口的 JSON access log；`logs/static-access.log` 由 `_assemble_main_config` 自动注入。
- 不暴露管理端口到公网；不启用 TLS 自动签发（V1 局域网，`http://` 即可）。

## 处理流程

1. 确认后端：切换优先用 `lwa gateway switch caddy`（或 `builtin`），**勿手改 YAML 再猜顺序**（IMP-037）。目标为 caddy 时再 `lwa gateway on`（若尚未运行）；builtin 下每实例独立 hostPort，无统一入口。
2. 确认静态根存在且有 `index.html`（通常 `apps/<id>/public/`）。
3. **优先**：`lwa start <id>`（或 rebuild）让 `StaticGateway` 生成片段并 reload，不要手写。
4. 排障时对照模板 `templates/static/caddy_site.conf.tpl`：站点片段为 `:<hostPort> { root * <abs> ; file_server ; encode gzip }`（**无** `try_files`）。
5. SPA history fallback：当前内置模板不做 `try_files`；若应用依赖客户端路由，需在应用侧配置或扩展模板——不要在主 Caddyfile 里散改。
6. 路径别名：见 `lwa-import-zip`（IMP-022/023）；别名片段由 `generate_alias_config` 生成，勿手写。

## 示例（与内置模板对齐，仅供对照）

```caddy
# static-gateway/sites/<id>.conf —— 由 lwa 生成，勿手改主 Caddyfile
:18001 {
	root * /abs/path/to/apps/<id>/public
	file_server
	encode gzip
}
```

别名片段（`aliases/<id>.conf`）形如对 `/<slug>/*` 去前缀后 `reverse_proxy 127.0.0.1:<hostPort>`，由 `lwa alias set` / 管理页触发。

## 相关

- [运维手册](../../../../docs/operations-playbook.md) — Caddy vs builtin、排障
- `lwa-import-zip` — 路径别名与 SPA base
