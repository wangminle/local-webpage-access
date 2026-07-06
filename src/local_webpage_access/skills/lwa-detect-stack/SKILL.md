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

修改 `local-web.json` 的以下字段：

- `kind`：`static` / `node` / `python`（其他语言设 `unknown` 并保持 pending）。
- `runtime`：`shared-static` / `docker-compose`。
- `servingMode`：`static` / `frontend-static` / `backend-container` / `fullstack-sqlite`。
- `stack`：数组，如 `["React", "Vite", "FastAPI", "SQLite"]`。
- `resourceProfile`：`tiny` / `small` / `medium` / `heavy`。
- `database.type`：`none` / `sqlite` / `postgres` / `mysql` / `redis` / `unknown`。

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
  "kind": "static",
  "runtime": "shared-static",
  "servingMode": "frontend-static",
  "stack": ["React", "Vite"],
  "resourceProfile": "tiny",
  "database": { "type": "none" }
}
```
