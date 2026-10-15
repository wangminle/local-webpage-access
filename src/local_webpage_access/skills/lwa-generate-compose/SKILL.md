---
name: lwa-generate-compose
description: >-
  Generate or repair docker-compose.yml for an lwa instance that needs multiple coordinated services such as an application, cache, and database. Use when one container is insufficient, service dependencies or networks are missing, or existing Compose ports, volumes, and resource limits conflict with local-web.json.
---

# lwa-generate-compose

> 核对单服务 Compose 是否与 manifest 一致。`compose.generate_compose` 只产出**一个 app 服务**。
>
> **生成物不是真相。** `docker/compose.yaml` 与 `docker/Dockerfile` 会在 `lwa start` / `lwa rebuild` 时按 manifest **重新渲染**。不要往生成文件里人工追加 Redis、Postgres 或其他服务：下一次生成会丢掉这些服务。MySQL / PostgreSQL / Redis 不会由 LWA 自动起容器，见 `docs/known-limitations.md`。需要额外挂载时用 `container.extraVolumes` 或 `lwa configure <id> --extra-volume`。

## 何时触发

- 容器实例缺少可用的单服务 `docker/compose.yaml`，或端口、卷、资源限制与 `local-web.json` 不一致。
- 有人准备往生成的 compose 里加 Redis / Postgres。这时应停下来说明平台边界，而不是追加服务。

## 输入

1. `apps/<id>/docker/compose.yaml`（若已存在但需修正）或项目结构线索。
2. 各服务的依赖与端口需求。
3. 初始 `local-web.json`。
4. 工作区端口池可用区间。

## 输出

- 修改 `local-web.json` 的 `container.internalPort`、`entry.start`、`container.extraVolumes`。
- 由 `lwa start` / `lwa rebuild` 重新渲染单服务 `docker/compose.yaml`。不要自己维护那份文件。

## 可修改文件

- `apps/<id>/local-web.json`。
- 不要把 `apps/<id>/docker/compose.yaml` 当作可持久化的修改目标。

## 禁止事项

- 不使用 `privileged`、不挂载 `docker.sock`（`generate_compose` / `audit_compose` 会因 critical 拒绝写出）。
- 每个服务**只挂载自己的数据卷**，不共享宿主目录。
- 不把数据库密码硬编码到 compose（用 `docker/.env` 或 `environment`，并在诊断中提示用户修改默认值）。
- 不 `network_mode: host`（用默认 bridge 网络）。
- heavy 资源档位不自动启动（由用户确认）。

## 处理流程

1. 确认实例是 `runtime: docker-compose`、`servingMode: container`，并已有 `container.internalPort` 与 `entry.start`。
2. 需要额外只读或数据卷时，写入 `container.extraVolumes`（`宿主路径:容器绝对路径[:ro|rw]`），或执行 `lwa configure <id> --extra-volume <规格>`。
3. 执行 `lwa rebuild <id>` 或 `lwa start <id>`，让平台重新渲染单服务 compose。
4. 若需求是 Redis / Postgres / MySQL：向用户说明当前生成器不保留这些服务，不要追加到 `docker/compose.yaml`。

## 示例

只追加一个只读配置目录，而不是追加 Redis 服务：

```bash
lwa configure <id> --extra-volume /path/on/host:/app/config:ro
```
