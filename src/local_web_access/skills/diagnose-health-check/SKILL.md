# diagnose-health-check

> 诊断健康检查（HTTP 探测）失败原因，给出修复建议或调整探测配置。

## 何时触发

- 实例 `running` 但健康检查持续失败（`last_error` 含"健康检查失败"）。
- 管理页实例详情显示健康检查红点。

## 输入

1. 健康检查结果：HTTP 状态码、超时、连接拒绝（由 `lwa` 提供）。
2. `logs/<id>/run.log`（确认应用是否真的起来了）。
3. `local-web.json`（`container.hostPort`、`static.hostPort`、健康路径）。
4. 应用路由线索（是否有 `/health`、`/healthz`、根路径是否 200）。

## 输出

- 诊断说明（事件日志）：根因分类 + 建议。
- 必要时修改 `local-web.json`：
  - 调整健康检查路径（若应用只有 `/health` 而默认探 `/`）。
  - 调整超时（启动慢的应用）。

## 可修改文件

- `apps/<id>/local-web.json`（健康检查相关字段）。

## 禁止事项

- 不为通过健康检查而把应用改成永远返回 200。
- 不关闭健康检查（设 `enabled: false`）来掩盖问题。
- 不修改 `data/`。

## 处理流程

1. 按状态码/现象归类：
   - **连接拒绝**：应用没起来或没监听该端口 → 转 `fix-container-startup-failure`。
   - **404**：应用起来了但探测路径不存在 → 改健康路径为 `/health` 等。
   - **5xx**：应用内部错误 → 查 `run.log`，建议修业务代码或依赖。
   - **超时**：启动慢（Java/Next）→ 延长启动宽限时间或超时。
2. 给出最小修改：
   - 路径不对 → `local-web.json` 设健康路径。
   - 超时 → 调整 `healthCheck.timeout` / `startPeriod`。
3. 写诊断到事件日志，提示用户确认后 `lwa restart`。

## 示例

健康检查返回 404，应用实际有 `/healthz`：

```json
{ "healthCheck": { "path": "/healthz", "enabled": true } }
```

FastAPI 启动慢导致初次探测超时 → 延长 `startPeriod` 到 30s，并在诊断中说明"非故障，仅启动慢"。
