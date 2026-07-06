# lwa-dockerize-node-app

> 为小型 Node 后端生成最小化 `Dockerfile`，交由 `lwa` 构建运行。

## 何时触发

- 识别为 `kind: node`、`runtime: docker-compose`，但 `apps/<id>/current/Dockerfile` 不存在。
- daemon 或用户要把 Node 后端容器化（资源档位 small）。

## 输入

1. `package.json`（`engines.node`、`scripts.start`、依赖、是否含原生模块）。
2. `package-lock.json` / `yarn.lock` / `pnpm-lock.yaml`（锁定包管理器）。
3. 初始 `local-web.json`（`container.internalPort` 应已由 lwa-detect-internal-port 填好）。
4. `.nvmrc` / `.node-version`（可选 Node 版本）。

## 输出

- 生成 `apps/<id>/current/Dockerfile`。
- 修改 `local-web.json` 的 `container.image` / `container.internalPort`。

## 可修改文件

- `apps/<id>/current/Dockerfile`（新建或覆盖）。
- `apps/<id>/local-web.json`。
- 必要时新增 `.dockerignore`。

## 禁止事项

- 不使用 `privileged`、不挂载 Docker socket、不挂载宿主敏感目录。
- 不以 root 运行应用进程（创建非 root user）。
- 不把 `node_modules` 提交进镜像源码层（用多阶段或 `.dockerignore` 排除）。
- 不 `latest` 基础镜像 tag（固定到具体版本）。
- 不引入 `nodemon`/`ts-node-dev` 等开发期工具到运行镜像。

## 处理流程

1. 确定 Node 版本：`engines.node` > `.nvmrc` > 默认 `20`。
2. 确定包管理器：有 `pnpm-lock.yaml` → pnpm；`yarn.lock` → yarn；否则 npm。
3. 生成多阶段 Dockerfile：
   - 阶段 1（deps）：`npm ci --omit=dev`（或等价）到 `/app/node_modules`。
   - 阶段 2（runtime）：复制源码 + deps，`USER node`，`EXPOSE <internalPort>`，`CMD ["npm", "start"]`。
4. 生成 `.dockerignore`（排除 `node_modules`、`.git`、`.env`、`dist`）。
5. 写回 `local-web.json` 的 `container` 字段。

## 示例

```dockerfile
FROM node:20-slim AS deps
WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev
FROM node:20-slim
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
USER node
EXPOSE 3000
CMD ["npm", "start"]
```
