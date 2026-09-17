# LWA Agent 错误处置指南

> 适用版本：V0.8.18。所有错误统一为
> `{"error": {"code", "message", "detail?", "retryable", "retryAfterMs?", "nextActions?"}}`。
> `retryable=true` 才可自动重试；重试须有界（建议按 `retryAfterMs`，无则指数退避，≤3 次）。

## 通用入口错误

| code | 含义 | 处置 |
| --- | --- | --- |
| `manager_unavailable` | manager 不在线或回环不可达（retryable） | 提示管理员在工作区执行 `lwa manager on`；stdio 模式要求回环可达——若 message 提到"只绑定了 LAN 地址"，说明 manager 绑了具体 LAN IP，本机适配器出于凭据安全拒绝连接，请改回回环/通配绑定 |
| `unauthenticated` | token 无效或缺失 | token 会周期轮换（默认 168h）；向管理员重新获取，**不要**无凭据重试 |
| `permission_denied` | 主体无权（M1 仅本机 owner 回环） | 确认从 LWA 主机本机发起；检查 `--workspace` 是否指向正确工作区 |
| `needs_input` | 参数不符合契约 / 需人工确认 | 读 `detail.issues` 修参数；这是输入错误，重试前必须改请求 |

## 部署闭环错误

| code | 含义 | 处置 |
| --- | --- | --- |
| `revision_conflict` | `expectedRevision` 已过期 | 重新 `lwa_get_instance` 读当前 `revision` 后再提交；**不要**猜测递增 |
| `idempotency_conflict` | 同一幂等键携带了不同请求内容 | 换**新** `idempotencyKey`；若想重试原请求，用原键+原内容原样重发 |
| `source_not_allowed` | 源路径不在 `allowedSourceRoots` / Git 非 github.com | 请管理员调整允许根，或换合规来源 |
| `capability_unavailable` | 缺 Docker 等运行能力 | 报告能力缺口，**不要**尝试自行安装依赖 |
| `quota_exceeded` | 端口池/实例数等配额已满（retryable） | 提示管理员清理冗余实例后再试 |
| `busy` | 服务繁忙（retryable，常带 `retryAfterMs`） | 按 `retryAfterMs` 有界退避重试 |
| `build_failed` / `healthcheck_failed` | 操作级失败（体现在 operation.error） | `lwa_get_logs`（按 operationId）定位；修复后用新 plan 重来 |
| `interrupted` | 结果未知（retryable=false！） | **先核对实际状态**（`lwa_get_instance` / `lwa_get_operation`）再决定恢复路径，绝不盲目重放 |

## 排障顺序建议

1. `lwa_get_capabilities` 确认契约版本与 runtime 能力；
2. `lwa_get_operation` 看 operation 的 `phase`（validate/import/build/start/healthcheck）缩小范围；
3. `lwa_get_logs`（operationId 优先）取脱敏日志；
4. 仍不明：把 `error.code` + `operationId` 报告给管理员，不要扩大重试范围。
