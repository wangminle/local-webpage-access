---
name: lwa-generate-compose
description: >-
  Generate or repair docker-compose.yml for an lwa instance that needs multiple coordinated services such as an application, cache, and database. Use when one container is insufficient, service dependencies or networks are missing, or existing Compose ports, volumes, and resource limits conflict with local-web.json.
---

# lwa-generate-compose

> 为多服务实例（后端 + 缓存 + 数据库等）生成或扩展 `apps/<id>/docker/compose.yaml`。
> **口径**：V1 `compose.generate_compose` 只产出**单服务**模板；多服务需在此基础上人工扩展，并过 `audit_compose`。

## 何时触发

- 实例含多个服务（如后端 + Redis + Postgres），但无可用的 `docker/compose.yaml`。
- 单容器不够，需要编排。

## 输入

1. `apps/<id>/docker/compose.yaml`（若已存在但需修正）或项目结构线索。
2. 各服务的依赖与端口需求。
3. 初始 `local-web.json`。
4. 工作区端口池可用区间。

## 输出

- 生成或修正 `apps/<id>/docker/compose.yaml`（V1 默认同服务；多服务人工扩展）。
- 修改 `local-web.json`：
  - `container.hostPort` / `internalPort`（主服务）。
  - `database` 段（若含数据库服务）。
  - `mounts`（仅各服务自己的 `data/`）。

## 可修改文件

- `apps/<id>/docker/compose.yaml`。
- `apps/<id>/local-web.json`。

## 禁止事项

- 不使用 `privileged`、不挂载 `docker.sock`（`generate_compose` / `audit_compose` 会因 critical 拒绝写出）。
- 每个服务**只挂载自己的数据卷**，不共享宿主目录。
- 不把数据库密码硬编码到 compose（用 `docker/.env` 或 `environment`，并在诊断中提示用户修改默认值）。
- 不 `network_mode: host`（用默认 bridge 网络）。
- heavy 资源档位不自动启动（由用户确认）。

## 处理流程

1. 优先用 `compose.generate_compose` 生成单服务基线（`build.context: ..`，`dockerfile: docker/Dockerfile`，`../data:/app/data`，ports 来自 `.env`）。
2. 若确需多服务：在基线上人工追加依赖服务，主服务端口仍用 `${HOST_PORT}:${INTERNAL_PORT}`。
3. 附加服务的 volumes 仅挂各自数据目录（相对 compose 文件，如 `../data/redis:/data`）。
4. 定义 `networks`：默认 bridge，服务间用服务名互通。
5. `restart: unless-stopped`；写回前跑 `audit_compose`。
6. 写回 `local-web.json`。

## 示例

后端（对齐 V1 布局）+ Redis（人工扩展）：

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
    depends_on: [redis]
    restart: unless-stopped
  redis:
    image: redis:7-alpine
    volumes:
      - ../data/redis:/data
    restart: unless-stopped
```
