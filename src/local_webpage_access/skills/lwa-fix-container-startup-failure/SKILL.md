# lwa-fix-container-startup-failure

> 诊断容器启动后立即退出/OOM/崩溃的原因，修正启动配置。

## 何时触发

- 实例 `failed`，构建成功但 `run.log` 显示容器启动即退出（exit code ≠ 0）。
- `docker compose ps` 显示反复重启（Restarting）。

## 输入

1. `logs/<id>/run.log`（容器 stdout/stderr）。
2. `docker-compose.yml` / `Dockerfile`。
3. `local-web.json`（`container`、`entry.start`、资源限制）。
4. registry 的 `last_error`。

## 输出

- 修正后的 `Dockerfile` CMD / `docker-compose.yml` command / `entry.start`。
- 资源限制调整建议（若 OOM）。
- 诊断说明（事件日志）。

## 可修改文件

- `apps/<id>/current/Dockerfile`。
- `apps/<id>/current/docker-compose.yml`。
- `apps/<id>/local-web.json`。

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
   - **权限**：非 root 用户写不了 `/data`、绑定 <1024 端口。
2. 针对性修复：
   - 缺环境变量 → 在 compose `environment` 补齐（默认值 + 提示修改）。
   - CMD 错误 → 修正为实际入口（如 `python -m uvicorn app:app`）。
   - OOM → 在 `local-web.json` 调整资源档位（small → medium），不超主机上限。
   - 权限 → `chown` 在构建期完成，或调整挂载点属主。
3. 写回修改 + 诊断，提示 `lwa restart <id>`。

## 示例

`run.log` 含 `permission denied: /data/app.db` → 非 root 用户无写权限：

```dockerfile
RUN mkdir -p /data && chown -R appuser /data
USER appuser
```
