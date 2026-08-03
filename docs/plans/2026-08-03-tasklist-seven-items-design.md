# Task List 七项待办收口设计

> **状态（V0.6.12）**：七项（BUG-369/370/371/420/421/422 + DEV-094）及审查跟进 BUG-423～427 已落地；详见同目录 implementation 计划与 `task-list.md`。

## 目标与范围

一次性核实并收口 `BUG-369`、`BUG-370`、`BUG-371`、`BUG-420`、`BUG-421`、`BUG-422` 与 `DEV-094`。对已被后续代码修复的历史项，以回归证据关闭，不重复改动；对仍存在的问题，按 TDD 逐项修复。

## 总体方案

采用“按风险分阶段、共享少量基础能力”的方案：

1. 先复核三个历史低优先项，区分真实遗留与任务状态陈旧。
2. 修复网关启动时序，保证 Caddy 首次加载当前工作区配置。
3. 建立只读的 Docker 挂载观测与规范路径计算能力，供容器生命周期和 doctor 共用。
4. 修复挂载漂移时的数据保护与容器重建流程。
5. 统一回写 manifest/registry 中可确定的工作区派生路径。
6. 将同一口径接入 doctor，在运维阶段主动暴露裸 `mv` 残留。

## 逐项设计

### BUG-369：并发观测的可信状态

`_observe_container_status()` 在入口读取 `last_trusted_state`，异常发生时内层函数仍使用该快照。改为在记录 unknown 前重读 registry，优先保留并发更新后的 `last_trusted_state/status`。新增回归模拟观测过程中另一写者更新状态。

### BUG-370：导入 ID 原子认领

当前 `_claim_unique_id(..., on_conflict="error")` 已将 registry 检查与 `mkdir(exist_ok=False)` 原子认领合并，并发失败直接抛错，对应 `BUG-313` 回归已存在。本项不重复修改产品代码，通过定向并发回归确认后关闭。

### BUG-371：镜像 ID 兜底查询

有容器时仍优先 `docker inspect` 读取真实镜像 ID。无容器时不再拼接 `<project>-<service>` 假设镜像名，改用当前 Compose 文件的 `docker compose images -q <service>` 查询，自然支持顶层 `name`、自定义 `projectName`、显式 `image` 和 BuildKit。

### BUG-420：Caddy 首次加载的配置

在 `StaticGateway` 中抽出“按磁盘片段组装并原子落盘主 Caddyfile”的纯写入方法，不 reload、不自愈启动。`start_gateway()` 在 `caddy_start()` 前无条件调用。`_sync_main_config()` 复用该方法后再 reload。保留已在线 master 的不重启语义。

### BUG-421：Docker bind mount 漂移与数据保护

`DockerRuntime` 新增只读挂载观测，从容器 `inspect` 返回 bind mount 的 source/destination。`start_container()` 在“已运行直接返回”与“已停止直接 start”之前检查 LWA 管理的 SQLite data mount。

若 source 与当前 `workspace.app_data(instance_id)` 不同：

1. 调用现有 SQLite 数据救援；
2. `down` 删除旧容器；
3. 清空陈旧容器身份；
4. 使用当前 Compose `up -d` 显式重建；
5. 观测并回写新身份。

挂载观测失败时不执行破坏性操作，返回可诊断错误；无 SQLite data mount 的容器保持原行为。

### BUG-422：manifest/registry 派生路径一致性

提供小型路径同步 helper，在容器成功托管/重建与静态站点成功启用后，回写：

- `manifest.appPath`
- `manifest.container.composePath`
- `manifest.container.dockerfilePath`
- `manifest.static.gatewayConfigPath`

完成后与 manifest 一起 upsert 到 registry。`sourceZipPath` 允许指向工作区外部，不做无条件改写。

### DEV-094：doctor 工作区一致性检查

doctor 新增单一聚合检查，检查：

- 活跃 manifest/registry 的可确定派生路径是否等于当前 workspace 规范值；
- 主 Caddyfile 和 sites 片段中引用的本地路径是否存在；
- Docker 可用时，LWA 管理的 SQLite data mount 是否指向当前 data 目录。

历史 builds/events 和合法外部 `sourceZipPath` 不触发 WARN。诊断输出实例、字段、实际值、期望值和修复建议。

## 错误处理与安全边界

- 不对未知旧根路径执行全文盲替换。
- 容器挂载状态未知时不自动 down/recreate。
- 破坏性重建前复用现有 SQLite 数据救援保护。
- doctor 只读，仅诊断不自动修改。
- 不改写合法的外部源 ZIP 路径。

## 验证策略

每项遵循红-绿-重构：先写最小失败回归，确认失败原因，再实施最小修复。最终执行：

- 七项相关定向测试；
- 容器、网关、doctor、导入、生命周期相关套件；
- 全量 pytest；
- ruff 与 mypy；
- 语法/构建检查；
- Docker 环境可用时执行真实挂载回归。

验证通过后逐条更新 `task-list.md` 的完成时间、状态、修改文件和测试结果。
