# fix-port-binding

> 解决宿主端口被占用或端口映射错误导致实例无法启动的问题。

## 何时触发

- `run.log` 含 `bind: address already in use` / `port is already allocated`。
- 实例 `failed` 且 `last_error` 提示端口冲突。
- `lwa doctor` 报告端口池存在异常占用。

## 输入

1. `run.log` / 构建日志中的端口错误。
2. `local-web.json`（`container.hostPort`、`internalPort`）。
3. registry 的 `allocated_ports`（端口池当前占用）。
4. 宿主端口占用情况（由 `lwa` 提供，非 skill 自行探测）。

## 输出

- 修改 `local-web.json` 的 `container.hostPort`（换到端口池内空闲端口）。
- 或修正 `internalPort` 与应用实际监听端口的不一致。
- 诊断说明。

## 可修改文件

- `apps/<id>/local-web.json`（仅端口字段）。

## 禁止事项

- 不把端口改到端口池范围之外（破坏端口池统一管理）。
- 不抢占其他实例已分配的端口。
- 不修改系统级端口占用（如让用户停掉别的服务）—— 只调整本实例端口。
- 不绑定 <1024 的特权端口。

## 处理流程

1. 区分两类冲突：
   - **宿主端口被外部进程占用**（非 lwa 实例）→ 换端口池内空闲端口。
   - **internalPort 与应用实际监听不符**（映射对了但容器内没人 listen）→ 调 `internalPort`。
2. 从 registry 查询端口池已分配集合，选一个 `[start, end]` 内未占用的端口。
3. 写回 `local-web.json` 的 `hostPort`。
4. 提示 `lwa restart <id>`；若为 internalPort 问题，需 `lwa rebuild`。

## 示例

21001 被外部占用 → 改为端口池内空闲的 21015：

```json
{ "container": { "hostPort": 21015, "internalPort": 8000 } }
```
