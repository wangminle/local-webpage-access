# generate-compose

> 为多服务实例（后端 + 缓存 + 数据库等）生成 `docker-compose.yml`。

## 何时触发

- 实例含多个服务（如后端 + Redis + Postgres），但无 `docker-compose.yml`。
- 单容器不够，需要编排。

## 输入

1. `docker-compose.yml`（若已存在但需修正）或项目结构线索。
2. 各服务的依赖与端口需求。
3. 初始 `local-web.json`。
4. 工作区端口池可用区间。

## 输出

- 生成或修正 `apps/<id>/current/docker-compose.yml`。
- 修改 `local-web.json`：
  - `container.hostPort` / `internalPort`（主服务）。
  - `database` 段（若含数据库服务）。
  - `mounts`（仅各服务自己的 `data/`）。

## 可修改文件

- `apps/<id>/current/docker-compose.yml`。
- `apps/<id>/local-web.json`。

## 禁止事项

- 不使用 `privileged`、不挂载 `docker.sock`。
- 每个服务**只挂载自己的数据卷**，不共享宿主目录。
- 不把数据库密码硬编码到 compose（用 `.env` 或 `environment`，并在诊断中提示用户修改默认值）。
- 不 `network_mode: host`（用默认 bridge 网络）。
- heavy 资源档位不自动启动（由用户确认）。

## 处理流程

1. 枚举服务：应用本体 + 数据库 + 缓存等。
2. 为每个服务分配容器内端口；主服务分配宿主端口（端口池）。
3. 定义 `volumes`：`./data/<service>:/data/<service>`。
4. 定义 `networks`：默认 bridge，服务间用服务名互通。
5. `restart: unless-stopped`。
6. 写回 `local-web.json`。

## 示例

后端 + Redis：

```yaml
services:
  app:
    build: .
    ports:
      - "21003:8000"
    volumes:
      - ./data/app:/data/app
    depends_on: [redis]
    restart: unless-stopped
  redis:
    image: redis:7-alpine
    volumes:
      - ./data/redis:/data
    restart: unless-stopped
```
