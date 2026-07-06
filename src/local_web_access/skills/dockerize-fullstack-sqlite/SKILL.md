# dockerize-fullstack-sqlite

> 为"后端 + SQLite"全栈项目生成 `Dockerfile` 与 `docker-compose.yml`，并正确挂载数据目录。

## 何时触发

- 识别为 `servingMode: fullstack-sqlite`（后端框架 + SQLite 文件）。
- 需要持久化 SQLite 数据，且实例走容器形态。

## 输入

1. 后端框架线索（同 `dockerize-python-app` / `dockerize-node-app`）。
2. SQLite 文件位置线索：源码中的 `sqlite:///` 路径、`*.db` 文件、ORM 配置。
3. 初始 `local-web.json`（`database.type` 应为 `sqlite`）。

## 输出

- 生成 `apps/<id>/current/Dockerfile`。
- 生成 `apps/<id>/current/docker-compose.yml`。
- 修改 `local-web.json`：
  - `container.internalPort`、`container.hostPort`（由端口池分配）。
  - `database.path`：容器内 SQLite 路径（如 `/data/app.db`）。
  - `mounts`：`data/` → `/data`（仅自己的数据目录）。

## 可修改文件

- `apps/<id>/current/Dockerfile`。
- `apps/<id>/current/docker-compose.yml`。
- `apps/<id>/local-web.json`。

## 禁止事项

- **只挂载自己的 `data/`**，绝不挂载宿主其他目录（安全边界 §17）。
- 不挂载 Docker socket、不 privileged。
- 不把 SQLite 文件放进镜像层（必须挂载到 `data/` 以持久化）。
- 不以 root 运行；`data/` 目录属主与容器用户一致。

## 处理流程

1. 按后端语言选择基础镜像（Python/Node）。
2. 生成 Dockerfile（同 `dockerize-*-app`，但 CMD 中 SQLite 路径指向 `/data/`）。
3. 生成 `docker-compose.yml`：
   - 服务 `app`，build context 为当前目录。
   - volumes：`./data:/data`（相对 compose 文件）。
   - ports：`<hostPort>:<internalPort>`。
   - restart policy：`unless-stopped`。
   - 无 `privileged`、无 `docker.sock`。
4. 确保应用初始化逻辑把 SQLite 创建到 `/data/`（若源码写死其他路径，在诊断中提示需调整）。
5. 写回 `local-web.json`。

## 示例

```yaml
services:
  app:
    build: .
    ports:
      - "21001:8000"
    volumes:
      - ./data:/data
    environment:
      - DATABASE_URL=sqlite:////data/app.db
    restart: unless-stopped
```
