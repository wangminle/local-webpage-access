# lwa-generate-static-gateway-config

> 为静态实例（纯静态或构建后 SPA）生成共享网关配置（Caddy 优先，可选 nginx）。

## 何时触发

- 实例 `runtime: shared-static`，但 `static-gateway/<id>.*` 配置不存在。
- 端口池分配了宿主端口，需要网关把请求转发到静态产物。

## 输入

1. `local-web.json` 的 `static` 段（`root`、`hostPort`、`gateway`）。
2. 静态产物目录：`apps/<id>/current/<static.root>/`。
3. 工作区配置 `static-gateway` 类型（`caddy` 或 `nginx`）。

## 输出

- 生成 `static-gateway/<id>.caddy`（或 `<id>.nginx.conf`）。
- 修改 `local-web.json` 的 `static.gateway`、`static.hostPort`。

## 可修改文件

- `static-gateway/<id>.*`（仅本实例的片段）。
- `apps/<id>/local-web.json`。

## 禁止事项

- 不修改网关主配置文件（`Caddyfile` / `nginx.conf`）—— 由 `lwa` 汇总各片段后 reload。
- 不暴露管理端口到公网。
- 不启用 TLS 自动签发（V1 局域网，`http://` 即可）。

## 处理流程

1. 确认网关类型（默认 Caddy）。
2. 确认 `static.root` 存在且有 `index.html`。
3. 生成片段配置：
   - Caddy：`:<hostPort> { root * /绝对路径/到/static.root file_server }`
   - nginx：`server { listen <hostPort>; root /绝对路径/; location / { try_files $uri $uri/ /index.html; } }`
4. SPA 额外加 history fallback（`try_files ... /index.html`）。
5. 写回 `local-web.json`，由 `lwa` reload 网关。

## 示例

Caddy（SPA + history fallback）：

```caddy
:21002 {
  root * /var/lib/lwa/apps/my-spa/current/dist
  try_files {path} /index.html
  file_server
}
```
