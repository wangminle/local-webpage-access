# LWA Agent 部署闭环指南

> 适用版本：V0.9.1。面向通过 `/api/agent/v1/*`（或 `lwa mcp` 适配器）操作 LWA 的 Agent。
> 前置阅读：`quickstart.md`（三条底线与工作区确认）。

## 1. 部署是一个两段式闭环：plan → apply → 轮询 operation

**不要**期待一次调用完成部署。正确顺序：

1. `lwa_plan_deployment`（`POST /api/agent/v1/plans`）——预检并生成计划：
   - `intent: "create"`：新建实例；**不得**携带 `targetInstanceId` / `expectedRevision`。
   - `intent: "update"`：更新既有实例；**必须**携带 `targetInstanceId` 与
     `expectedRevision`（用 `lwa_get_instance` 读到的当前 `revision`）；不接受 `displayName`。
   - 返回 `PlanRecord`：`planId`、`risks`（保留数据 / 入口变更 / 可能停机 / 能力缺口）、
     `requiredCapabilities`、`expiresAt`（计划有效期 30 分钟，过期须重新 plan）。
2. **先把 `risks` 呈现给用户**（尤其 `possibleDowntime` / `capabilityGaps`），确认后再 apply。
3. `lwa_apply_deployment`（`POST /api/agent/v1/deployments`）——受理部署，返回
   `OperationAccepted{operationId, status}`。受理 ≠ 完成。
4. `lwa_get_operation` 轮询，直到 `status` 进入终态：
   - `succeeded`：完成，`result` 里有实例信息；用 `lwa_get_access_urls` 取访问地址。
   - `failed` / `interrupted`：读 `error`（见 troubleshooting.md），修复后用**新的 plan**
     重试，不要裸重放。
   - `needs_input`：操作暂停等待人工；只能 `lwa_cancel_operation` 取消，然后用新计划重来。

## 2. 幂等键规则（写操作必带）

- `apply` 与所有生命周期工具（start/stop/restart/rebuild）都要求 `idempotencyKey`。
- 同一 `idempotencyKey` + 相同请求内容 → 返回**同一个** operation（安全重试）。
- 同一 `idempotencyKey` + **不同**请求内容 → `idempotency_conflict`（409）。
- 建议：`uuid4` 生成一次，本次部署的重试全程复用；换意图必须换键。
- 生命周期工具还带 `expectedRevision`：先 `lwa_get_instance` 读 `revision` 再提交；
  过期会得 `revision_conflict`，重新读取后再试。

## 3. 来源（source）限制

| 类型 | 形状 | 限制 |
| --- | --- | --- |
| `server_directory` | `{type, path}` | LWA **服务器**上的绝对路径，须落在管理员配置的 `allowedSourceRoots` 内 |
| `git` | `{type, url, ref?, subdir?}` | 仅 HTTPS `github.com`；clone 超时 180s |
| `artifact` | `{type, artifactId}` | M2 才可用，当前会失败 |

## 4. 部署后

- `lwa_get_access_urls` 返回的是**服务器视角**观测；`localhost` 地址仅服务器本机有效，
  `lan` 地址请自行验证客户端可达性（`clientReachability` 字段只是提示）。
- `lwa_get_logs`（`instanceId` 或 `operationId` 二选一）排查运行问题。
