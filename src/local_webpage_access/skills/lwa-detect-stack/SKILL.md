---
name: lwa-detect-stack
description: >-
  Identify an imported project's technology stack, serving mode, runtime kind, and resource profile, then update local-web.json. Use after lwa import or lwa scan leaves an instance pending, stack is empty, or lwa reports that it cannot recognize the project type.
---

# lwa-detect-stack

> 识别项目的技术栈、运行形态与资源档位，回写 `local-web.json`。

## 何时触发

- `lwa import` / `lwa scan` 后实例仍为 `pending`，且 `lastError` 提示"无法识别项目类型"。
- 管理页显示实例为 pending 且 `stack` 为空。

## 输入

1. `apps/<id>/current/` 的完整目录结构（关注根目录与一级子目录）。
2. 关键清单文件：
   - `package.json`（Node：dependencies/scripts）
   - `requirements.txt` / `pyproject.toml` / `Pipfile`（Python）
   - `Dockerfile` / `docker-compose.yml`（已有容器化）
   - `index.html` / `vite.config.*` / `next.config.*`（前端）
   - `go.mod` / `Cargo.toml`（其他语言，V1 仅记录，不自动容器化）
3. 初始 `apps/<id>/local-web.json`。

## 输出

只改 `local-web.json` 里 schema 接受的字段。`form`（`static` / `frontend-static` / `backend-container` / `fullstack-sqlite`）是识别分类用语，**不是**可写入的字段；写进 `servingMode` 会导致校验失败。

持久化枚举：

- `kind`：`static` / `node` / `python`。Go / Rust 等其他语言保持 `pending`，不要写非法 kind。
- `runtime`：`shared-static` / `docker-compose`。
- `servingMode`：`shared-static` / `container`。
- `stack`：数组，如 `["React", "Vite"]` 或 `["node-stdlib-http"]`。
- `resourceProfile`：`tiny` / `small` / `medium` / `heavy`。
- `database.type`：发现 SQLite 文件，或源码声明 SQLite（Django `ENGINE=django.db.backends.sqlite3` / Flask `instance_path` 下的库）时写 `sqlite`，库文件尚未生成也算。MySQL / PostgreSQL / Redis 只在诊断里说明，不要为此改生成的 compose。
- `entry.install` / `entry.build` / `entry.start`：命令在 `entry` 对象内，不是顶层字段。
- `container.internalPort`：容器监听端口，不是顶层 `internalPort`。
- `buildHooks`（issue#7）：构建钩子命令数组，镜像构建期在依赖安装后逐条执行（如 `["python scripts/build_skills_bundle.py"]`）；仅在有明确构建期脚本证据时填写，值不得含换行。
- `preStart`（issue#7）：启动前命令，容器启动时先于 `entry.start` 执行（如 `"alembic upgrade head"`）；仅在项目声明了启动前迁移/初始化步骤时填写，值不得含换行。

分类怎么落到枚举：

| 口头分类 | kind | runtime | servingMode |
| --- | --- | --- | --- |
| static | `static` | `shared-static` | `shared-static` |
| frontend-static | `node` | `shared-static` | `shared-static` |
| backend-container | `node` 或 `python` | `docker-compose` | `container` |
| fullstack-sqlite | `node` 或 `python`，并设 `hasDatabase: true` | `docker-compose` | `container` |

## 可修改文件

- `apps/<id>/local-web.json`（仅上述字段）。

## 禁止事项

- 不创建或删除任何源文件。
- 不修改 `data/`。
- 不猜测没有证据的字段（如无 `requirements.txt` 不要假设 Python 版本）。

## 处理流程

1. 列出目录树（深度 2-3 层）。
2. 按优先级匹配清单文件：`docker-compose.yml` > `Dockerfile` > `package.json` > `pyproject.toml`/`requirements.txt` > 前端配置。
3. 从依赖推导 `stack`（如 `react` → React，`fastapi` → FastAPI）。
4. 根据 §16.1 资源档位表推断 `resourceProfile`：
   - 纯静态/SPA → `tiny`
   - 小型 Node/Python 单进程 → `small`
   - Next.js/Streamlit/Gradio → `medium`
   - 多服务/大模型/重型数据库 → `heavy`
5. 写回 `local-web.json`，并返回"已识别"说明。

## 示例

输入项目含 `package.json`（`react`、`vite`）+ `index.html`：

```json
{
  "kind": "node",
  "runtime": "shared-static",
  "servingMode": "shared-static",
  "stack": ["React", "Vite"],
  "resourceProfile": "tiny",
  "entry": { "install": "npm ci", "build": "npm run build", "start": null }
}
```

零依赖 Node 内置 `http`（只有 `package.json` + `server.js`，`require('http')` / `createServer` / `listen`）被识别成 pending 静态草稿时，按容器后端补全后再 `lwa start`。不要改 `docker/Dockerfile`：

```json
{
  "kind": "node",
  "runtime": "docker-compose",
  "servingMode": "container",
  "stack": ["node-stdlib-http"],
  "resourceProfile": "tiny",
  "status": "stopped",
  "entry": { "install": null, "build": null, "start": "node server.js" },
  "container": {
    "projectName": "lwa-demo",
    "internalPort": 3000,
    "composePath": "docker/compose.yaml",
    "dockerfilePath": "docker/Dockerfile"
  }
}
```
