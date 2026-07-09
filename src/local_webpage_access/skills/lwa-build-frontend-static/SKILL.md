# lwa-build-frontend-static

> 把前端 SPA（React/Vue/Vite/Next 静态导出）构建为静态产物，交由共享静态网关托管。

## 何时触发

- 识别为 `frontend-static`（含 `package.json` 且有 `build` 脚本）。
- 用户或 daemon 要求托管前端项目，但 `apps/<id>/current/public/`（或 `dist/`）不存在或过期。

## 输入

1. `package.json`（`scripts.build`、`scripts.install`、依赖列表）。
2. 前端配置：`vite.config.*`、`next.config.*`、`angular.json` 等（确认输出目录）。
3. 初始 `local-web.json`。

## 输出

- 修改 `local-web.json`：
  - `runtime` = `shared-static`
  - `servingMode` = `frontend-static`
  - `static.root` = 构建产物目录（如 `dist`、`build`、`out`）
  - `entry.build` = 构建命令（如 `npm run build`）
  - `entry.install` = 安装命令（如 `npm ci`）
- 不直接执行构建（由 `lwa rebuild` 在隔离环境执行）。

## 可修改文件

- `apps/<id>/local-web.json`。

## 禁止事项

- 不直接运行 `npm install`/`npm run build`（只产出**计划**，执行由 `lwa` 完成）。
- 不修改 `package.json` 的依赖（除非为修正明显错误，并在诊断中说明）。
- 不在容器中构建（V1 前端走宿主构建 + 共享静态网关，节省资源）。

## 处理流程

1. 读取 `package.json`，确认 `build` 脚本存在。
2. 从配置文件推断输出目录：
   - Vite 默认 `dist`
   - CRA 默认 `build`
   - Next 静态导出 `out`
3. 设定 `entry.install`（优先 `npm ci`，无 `package-lock.json` 时 `npm install`）。
4. 设定 `entry.build` 为 `package.json` 的 `scripts.build`。
5. 设定 `static.root` 为输出目录相对路径。
6. 写回 `local-web.json`。

## 示例

Vite + React 项目：

```json
{
  "runtime": "shared-static",
  "servingMode": "frontend-static",
  "static": { "root": "dist", "gateway": "caddy", "hostPort": null },
  "entry": { "install": "npm ci", "build": "npm run build" }
}
```

## IMP-023：路径别名下的 SPA 资源 base（V0.4.4 起）

若该前端实例会配置**路径别名**（`http://<LAN-IP>:<gatewayPort>/<alias>/`），
**必须在构建时设置正确的 base**，否则子路径下绝对资源路径会 404 白屏：

- 别名 `reverse_proxy` 会去掉 `/<alias>/` 前缀转发到 upstream，因此
  **相对路径资源**（`./assets/...`、`assets/...`）能正确解析为 `/<alias>/assets/...`；
- 但 **绝对路径资源**（`/assets/...`，即 `base: '/'`）会绕过别名直接打到入口根 → 404。

按框架设置（构建产物里资源引用变为相对路径或带 `/<alias>/` 前缀）：

- **Vite**：`vite.config.js` 设 `base: './'`（相对，推荐，别名无关），或
  `base: '/<alias>/'`（绑定特定别名）。
- **CRA**：不支持相对 base，需 `PUBLIC_URL=/<alias>` 构建。
- **Next 静态导出**：`next.config.js` 设 `assetPrefix: '/<alias>/'` +
  `trailingSlash: true`。

纯静态 HTML（相对路径或无外部资源）不受影响，无需调整。
`lwa alias set` 成功后会输出此提示；如已用绝对 base 托管，改用 hostPort 端口直达可绕过该限制。
