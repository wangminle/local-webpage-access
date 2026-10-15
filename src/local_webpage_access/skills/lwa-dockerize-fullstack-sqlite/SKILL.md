---
name: lwa-dockerize-fullstack-sqlite
description: >-
  Generate Dockerfile and docker-compose.yml for an lwa full-stack application that uses SQLite, with a durable data mount and safe database path. Use when servingMode is fullstack-sqlite, SQLite data must survive rebuilds, or an existing container writes its database outside the mounted data directory.
---

# lwa-dockerize-fullstack-sqlite

> 为「后端 + SQLite」核对 manifest，交由 `lwa` 重新渲染 `Dockerfile` 与单服务 compose，并把数据目录挂到 `data/`。
>
> **生成物不是真相。** `docker/Dockerfile` 与 `docker/compose.yaml` 会在 `lwa start` / `lwa rebuild` 时按 manifest **重新渲染**。持久字段是 `entry.start`、`hasDatabase`、`database`、`container.internalPort`。`fullstack-sqlite` 是口头分类，不是 `servingMode` 的合法值；容器实例写 `servingMode: container`。

## 何时触发

- 识别为带 SQLite 的容器后端（`kind` 为 `node` 或 `python`，`runtime: docker-compose`，`servingMode: container`，`hasDatabase: true`）。口头分类叫 fullstack-sqlite。
- 需要持久化 SQLite 数据，且实例走容器形态。

## 输入

1. 后端框架线索（同 `lwa-dockerize-python-app` / `lwa-dockerize-node-app`）。
2. SQLite 文件位置线索：源码中的 `sqlite:///` 路径、`*.db` 文件、ORM 配置。
3. 初始 `local-web.json`（`database.type` 应为 `sqlite`）。

## 输出

- 生成 `apps/<id>/docker/Dockerfile`。
- 生成 `apps/<id>/docker/compose.yaml`。
- 修改 `local-web.json`：
  - `container.internalPort`、`container.hostPort`（由端口池分配）。
  - `database.path`：容器内 SQLite 路径（如 `/app/data/app.sqlite`）。
  - `mounts`：`data/` → `/app/data`（仅自己的数据目录）。

## 可修改文件

- `apps/<id>/local-web.json`。
- 不要手改 `apps/<id>/docker/Dockerfile` 或 `docker/compose.yaml`。

## 禁止事项

- **只挂载自己的 `data/`**，绝不挂载宿主其他目录（安全边界 §17）。
- 不挂载 Docker socket、不 privileged。
- 不使用 `ADD https://...` 或 `curl|sh` / `wget|sh` 安装依赖（Dockerfile 生成门禁会拒绝写出）。
- 不把 SQLite 文件放进镜像层（必须挂载到 `data/` 以持久化）。
- 不以 root 运行；`data/` 目录属主与容器用户一致。

## 处理流程

1. 按后端语言选择基础镜像（Python/Node）。
2. 生成 Dockerfile（同 `lwa-dockerize-*-app`；数据目录约定 `/app/data`，由 Compose 挂载）。
3. 生成 `docker/compose.yaml`（优先走 `compose.generate_compose`，勿手写危险字段）：
   - 服务 `app`，`build.context: ..`，`dockerfile: docker/Dockerfile`。
   - volumes：`../data:/app/data`（或 RUNTIME_ROOT 布局的 `/app/runtime/data`）。
   - ports：`${HOST_PORT}:${INTERNAL_PORT}`（值来自 `docker/.env`）。
   - restart policy：`unless-stopped`。
   - 无 `privileged`、无 `docker.sock`。
4. 确保应用把 SQLite 建在挂载目录（默认 `sqlite:////app/data/app.sqlite`；若源码写死其他路径，在诊断中提示调整）。
5. 写回 `local-web.json`。

## 示例

```yaml
services:
  app:
    build:
      context: ..
      dockerfile: docker/Dockerfile
    ports:
      - "${HOST_PORT}:${INTERNAL_PORT}"
    volumes:
      - ../data:/app/data
    env_file:
      - .env
    restart: unless-stopped
```
