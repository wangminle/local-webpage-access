# lwa-detect-internal-port

> 找到应用在容器内监听的端口，回写 `local-web.json` 的 `internalPort`。

## 何时触发

- 实例识别为容器形态（`backend-container` / `fullstack-sqlite`），但 `internalPort` 缺失或为 0。
- 容器启动后健康检查失败，日志提示"连接被拒绝"。

## 输入

1. 项目源码中可能写死端口的文件：
   - Node：`package.json` 的 `scripts.start`、`.env`、`server.js`/`app.js`/`index.js`。
   - Python：`main.py`/`app.py`、`.env`、ASGI 配置。
   - `Dockerfile` 的 `EXPOSE`、`CMD`。
   - `docker-compose.yml` 的 `ports`。
2. 初始 `local-web.json`。

## 输出

- 修改 `local-web.json` 的 `container.internalPort`（或 `static.hostPort`，静态实例）。
- 若端口来自环境变量，需在容器配置中补上对应 `env`。

## 可修改文件

- `apps/<id>/local-web.json`。

## 禁止事项

- 不修改源码文件（除非是 `lwa` 托管的 `Dockerfile`/`compose`）。
- 不把 `internalPort` 设为特权端口（<1024），除非源码明确要求。
- 不臆造端口；找不到时保持 pending 并在诊断中说明。

## 处理流程

1. 按优先级查找端口证据：
   - `Dockerfile` 的 `EXPOSE <port>` / `CMD ... --port <port>`。
   - `docker-compose.yml` 的 `ports: ["<host>:<internal>"]`。
   - 源码中的 `listen(<port>)`、`uvicorn ... --port <port>`、`PORT=<port>`。
   - 框架默认值（Express 常用 3000，Flask 5000，FastAPI 8000，Vite dev 5173）。
2. 交叉验证：若多个来源冲突，以源码 `listen`/`run` 调用为准。
3. 写回 `internalPort`；若端口绑定了 `0.0.0.0` 则确认可从容器外访问。

## 示例

Node 项目 `server.js` 含 `app.listen(3000)`：

```json
{ "container": { "internalPort": 3000 } }
```

FastAPI 项目用 `uvicorn app:app --port 8000` 但无显式参数 → 推断 8000，并在诊断中标注"基于框架默认值，建议确认"。
