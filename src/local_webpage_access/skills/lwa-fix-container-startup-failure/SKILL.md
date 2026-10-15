---
name: lwa-fix-container-startup-failure
description: >-
  Diagnose an lwa container that builds successfully but exits, crashes, restarts, or is killed for resource use, then correct its startup configuration. Use when run.log has a nonzero exit, docker compose ps shows Restarting, or the declared command, environment, permissions, and resource limits disagree.
---

# lwa-fix-container-startup-failure

> 诊断容器启动后立即退出/OOM/崩溃的原因，修正 manifest 里的启动配置。
>
> **生成物不是真相。** `docker/Dockerfile` 与 `docker/compose.yaml` 会在 `lwa start` / `lwa rebuild` 时按 manifest **重新渲染**。改 CMD、`command` 或往镜像里加 `USER`/`RUN` 都不会留下来。启动命令写 `entry.start`，启动前步骤写 `preStart`，端口写 `container.internalPort`。

## 何时触发

- 实例 `failed`，构建成功但 `run.log` 显示容器启动即退出（exit code ≠ 0）。
- `docker compose ps` 显示反复重启（Restarting）。

## 输入

1. `apps/<id>/logs/run.log`（容器 stdout/stderr）。
2. `apps/<id>/docker/compose.yaml` / `apps/<id>/docker/Dockerfile`。
3. `local-web.json`（`container`、`entry.start`、资源限制）。
4. registry 的 `last_error`。

## 输出

- 修正后的 `entry.start` / `preStart` / `container.internalPort` / `resourceProfile`。
- 诊断说明（事件日志）。

## 可修改文件

- `apps/<id>/local-web.json`。
- 不要修改 `apps/<id>/docker/Dockerfile` 或 `docker/compose.yaml`。

## 禁止事项

- 不为绕过崩溃而 `--privileged` 或挂载 Docker socket。
- 不把 OOM 简单归零为"无限制"（4G/8G 主机要保守）。
- 不删除 `restart` 策略来掩盖反复重启。
- 不修改 `data/` 内容。

## 处理流程

1. 读 `run.log`，归类：
   - **配置缺失**：缺环境变量、缺配置文件、连不上依赖服务。
   - **命令错误**：`CMD` 指向不存在的脚本、`entry.start` 与实际不符。
   - **OOM**：内存限制过低，或进程本身泄漏。
   - **权限**：非 root 用户写不了 `/app/data`、绑定 <1024 端口。
2. 针对性修复：
   - 缺环境变量 → 写 `docker/.env` 里应用需要的键（该文件会保留），或在诊断里说明用户要补的变量。不要改生成的 compose `environment` 块。
   - 命令错误 → 改 `entry.start`（如 `python -m uvicorn app:app --host 0.0.0.0 --port 8000`）。迁移类步骤用 `preStart`。
   - OOM → 在 `local-web.json` 调整 `resourceProfile`（small → medium），不超主机上限。
   - 权限 → 新实例默认 `container.runAsNonRoot: true`，数据目录由生成流程处理。不要在 Dockerfile 里手写 `USER`。
3. 写回 manifest，提示 `lwa restart <id>` 或 `lwa rebuild <id>`。重启会重新渲染 Dockerfile 与 compose。

## 示例

`run.log` 含 `python: can't open file '/app/main.py'`，实际入口是 `server.py`：

```json
{ "entry": { "start": "python server.py" } }
```
