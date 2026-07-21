---
name: lwa-diagnose-health-check
description: >-
  Diagnose lwa HTTP health-check failures and recommend the smallest safe configuration or application fix. Use when a running instance stays unhealthy, the manager shows a red health indicator, or probing http://127.0.0.1:{hostPort}/ times out, refuses connections, or returns a failing status.
---

# lwa-diagnose-health-check

> 诊断健康检查（HTTP 探测）失败原因，给出修复建议。
> **口径**：`lwa` 对容器/静态实例的探活固定请求 `http://127.0.0.1:{hostPort}/`（带 `__lwa_probe=1`），默认超时约 2s；**没有**可配置的 `healthCheck.path` / `timeout` / `startPeriod` / `enabled` 字段。

## 何时触发

- 实例 `running` 但健康检查持续失败（`last_error` 含「健康检查失败」）。
- 管理页实例详情显示健康检查红点。

## 输入

1. 健康检查结果：HTTP 状态码、超时、连接拒绝（由 `lwa` 提供）。
2. `logs/<id>/run.log`（确认应用是否真的起来了）。
3. `local-web.json`（`container.hostPort` / `static.hostPort`）。
4. 应用路由线索（根路径 `/` 是否返回 2xx；是否只有 `/health`、`/healthz` 可探）。

## 输出

- 诊断说明（事件日志）：根因分类 + 建议。
- **不**通过改 `local-web.json`「换探测路径」——当前实现不支持；应修应用或启动配置。

## 可修改文件

- 应用代码 / 启动命令 / Dockerfile（使 **`/` 根路径**在宿主机映射端口上可返回正常文档响应）。
- 必要时配合 `lwa-fix-container-startup-failure` 修入口。

## 禁止事项

- 不为通过健康检查而把应用改成永远返回 200。
- **不要**编造或写入 `healthCheck` / `enabled: false`——manifest **无此字段**，无效且误导。
- 不修改 `data/`。

## 处理流程

1. 按状态码/现象归类：
   - **连接拒绝**：应用没起来或没监听该端口 → 转 `lwa-fix-container-startup-failure`。
   - **404 / 非 2xx 根路径**：探活固定打 `/`。若应用只暴露 `/healthz` 等，应在应用侧增加根路径健康响应，或调整反向代理/静态入口，使 `/` 可用；**不要**指望改 JSON 探测路径。
   - **5xx**：应用内部错误 → 查 `run.log`，建议修业务代码或依赖。
   - **超时**：启动慢 → 先确认进程是否最终监听成功；可稍后 `lwa restart` / 拉长应用自身就绪后再观察。LWA **无** `startPeriod` 配置项。
   - **管理页显示 stopped / unknown 但容器其实在跑**：先查 `runtimeAccess` /
     `observationError=permission_denied` 与 `lwa capabilities --json`，
     勿按「已停止」去 start；转 Docker 权限排障（FAQ / `lwa-setup-host-environment`）。
   - **API 409 capability_denied**：Docker 能力未 ready，先修权限再操作。
2. 写诊断到事件日志，提示用户确认后 `lwa restart`（或修应用后再 rebuild）。

## 示例

健康检查返回 404，应用实际只有 `/healthz`：

- **错误做法**：在 `local-web.json` 写 `{ "healthCheck": { "path": "/healthz" } }`（字段不存在，无效）。
- **正确做法**：让应用或网关对 `/` 返回 200（或等价可探文档），再 `lwa restart <id>`。

FastAPI 启动慢导致初次探测超时 → 查 `run.log` 是否最终起来；起来后 `lwa restart` 或等下一轮观测；说明「非配置开关问题，是启动窗口」。
