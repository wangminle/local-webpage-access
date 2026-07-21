---
name: lwa-dockerize-python-app
description: >-
  Generate a minimal Dockerfile for a small FastAPI, Flask, Django, or other Python backend managed by lwa. Use when a Python instance is classified for docker-compose but has no Dockerfile, or when dependency files, interpreter version, application entrypoint, and container port need to be aligned.
---

# lwa-dockerize-python-app

> 为小型 Python 后端（FastAPI/Flask/Django）生成最小化 `Dockerfile`。

## 何时触发

- 识别为 `kind: python`、`runtime: docker-compose`，但无 `Dockerfile`。
- daemon 或用户要把 Python 后端容器化（资源档位 small/medium）。

## 输入

1. 依赖清单：`pyproject.toml`（PEP 621）、`requirements.txt`、`Pipfile`、`poetry.lock`。
2. 入口线索：`main.py`/`app.py`、`pyproject.toml` 的 `[project.scripts]`、ASGI/WSGI 配置。
3. Python 版本：`pyproject.toml` 的 `requires-python`、`.python-version`、`runtime.txt`。
4. 初始 `local-web.json`（`container.internalPort`）。

## 输出

- 生成 `apps/<id>/docker/Dockerfile`（由 `dockerfile_templates.generate_dockerfile` 渲染）。
- 同步生成 `apps/<id>/.dockerignore`（排除 `node_modules` / `.git` / `__pycache__` / `.env` 等）。
- 修改 `local-web.json` 的 `container` 字段（`image`、`internalPort`、`entry.start`）。

## 可修改文件

- `apps/<id>/docker/Dockerfile`。
- `apps/<id>/.dockerignore`。
- `apps/<id>/local-web.json`。
- 必要时手工微调上述生成文件（改模板可惠及全部 Python 实例）。

## 禁止事项

- 不使用 `privileged`、不挂载 Docker socket。
- 不以 root 运行（创建非 root user，如 `appuser`）。
- 不 `pip install` 到系统 site-packages（用虚拟环境或 `--user`）。
- 不用 `python:3` 这类浮动 tag（固定到 `python:3.13-slim` 等）。
- 不 `--reload` 生产启动（FastAPI/Flask 生产模式）。

## 处理流程

1. 确定 Python 版本：`requires-python` > `.python-version` > 默认 `3.13`。
2. 确定依赖安装方式：
   - `pyproject.toml` + `poetry.lock` → `poetry install --no-dev --no-root`
   - `pyproject.toml`（无 lock）→ `pip install .`
   - `requirements.txt` → `pip install -r requirements.txt`（模板使用 BuildKit
     `--mount=type=cache,target=/root/.cache/pip` 加速重复构建）
3. 推断启动命令：
   - FastAPI：`uvicorn app:app --host 0.0.0.0 --port <internalPort>`
   - Flask：`gunicorn app:app -b 0.0.0.0:<internalPort>`（或 `flask run`）
   - Django：`gunicorn project.wsgi -b 0.0.0.0:<internalPort>`
4. 生成 Dockerfile：依赖层（含可选 Node 工具链）在完整 `COPY current/` **之前**，
   避免源码改动打掉 pip/npm 缓存。
5. 写回 `local-web.json`。

## 示例

FastAPI + `requirements.txt`：

```dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m appuser && chown -R appuser /app
USER appuser
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```
