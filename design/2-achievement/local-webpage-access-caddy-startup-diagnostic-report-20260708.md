# LWA/Caddy 启动故障排查报告（2026-07-08）

> 报告时间：2026-07-08  
> 范围：`runtime/` 工作区、LWA manager/daemon、Caddy 静态网关、3 个运行实例  
> 触发：上午重新运行 LWA 并启用 Caddy 后，至少两个网页无法启动

## 1. 结论摘要

当前故障不在 LWA manager 或 `prd-workflow` 容器本身，而在 Caddy/共享静态网关链路。

| 组件 | 当前状态 | 证据 |
| --- | --- | --- |
| LWA manager | 正常 | `http://127.0.0.1:17800/api/health` 返回 200 |
| LWA daemon | 正常运行 | `runtime/run/daemon.json` 记录 pid=35425，进程存活 |
| `prd-workflow` | 正常 | `http://127.0.0.1:18002/` 返回 200 |
| `demo-static` | 期望运行，实际停止 | `desired=running`，`status=stopped`，`18000` 无监听 |
| `voiceprint-v3-demo` | 期望运行，实际停止 | `desired=running`，`status=stopped`，`18001` 无监听 |
| Caddy 统一入口 | 不可用 | `8080` 无监听 |
| Caddy admin | 不可用 | `2019` 无监听 |

核心错误：

```text
Error: adapting config using caddyfile:
File to import not found:
runtime/static-gateway/sites/demo-static.conf
```

当前 `runtime/static-gateway/Caddyfile` 仍然导入已经不存在的站点/别名片段，导致 Caddy 冷启动或 validate 直接失败。

## 2. 当前状态快照

`lwa status` 显示：

```text
ID                   KIND     RUNTIME          STATUS     DESIRED    PORT   NAME
demo-static          static   shared-static    stopped    running    18000  demo-static
voiceprint-v3-demo   node     shared-static    stopped    running    18001  声纹管理页面V3演示
prd-workflow         python   docker-compose   running    running    18002  PRD需求评审工作流
```

端口监听情况：

| 端口 | 预期用途 | 当前结果 |
| --- | --- | --- |
| 17800 | manager | Python 进程监听 |
| 18000 | `demo-static` | 无监听 |
| 18001 | `voiceprint-v3-demo` | 无监听 |
| 18002 | `prd-workflow` | Docker 监听 |
| 8080 | Caddy path alias 入口 | 无监听 |
| 2019 | Caddy admin | 无监听 |

`runtime/run/` 中存在 stale pid：

- `caddy.pid` 指向的进程不存在。
- `static-demo-static.pid` 指向的进程不存在。
- `static-voiceprint-v3-demo.pid` 指向的进程不存在。

## 3. 上午启动时间线

| 时间 | 事件 | 结果 |
| --- | --- | --- |
| 09:47:22 | `manager_service` 启动 | 正常，pid=35422 |
| 09:47:23 | `daemon` 启动 | 正常，pid=35425 |
| 09:47:42 | `demo-static` 启动失败 | `caddy reload` 超时 |
| 09:47:54 | `voiceprint-v3-demo` 构建成功 | `npm ci` + `vite build` 成功 |
| 09:47:58 | `voiceprint-v3-demo` 网关启用失败 | `[GATEWAY_ERROR] Caddy reload 失败` |
| 09:48:46~09:49:24 | `demo-static` 多次重试 | 均为 Caddy reload 失败 |
| 09:51:54 | `demo-static` 写入启动成功事件 | 后续又被观测为停止 |
| 09:51:57 | `voiceprint-v3-demo` 写入启动成功事件 | 后续又被观测为停止 |
| 09:52:08 | 两个静态实例状态变更 | `running -> stopped` |
| 09:55:33 | `voiceprint-v3-demo` 再次构建成功后失败 | Caddy reload 失败 |
| 09:55:39 | `demo-static` 再次失败 | Caddy reload 失败 |

`runtime/logs/caddy.log` 没有今天的新日志，最后停在 `2026-07-07 16:59`。这说明今天上午的失败并不是一个健康 Caddy master 正常输出日志后的 reload 失败，而是 Caddy master/admin 本身已不在线或未受 LWA 管理。

## 4. 关键证据

### 4.1 Caddyfile 导入缺失文件

当前 `runtime/static-gateway/Caddyfile` 仍包含：

```caddyfile
import `/.../runtime/static-gateway/sites/demo-static.conf`
import `/.../runtime/static-gateway/sites/voiceprint-v3-demo.conf`

:8080 {
	import `/.../runtime/static-gateway/aliases/prd-workflow.conf`
	import `/.../runtime/static-gateway/aliases/voiceprint-v3-demo.conf`
}
```

但磁盘上实际只剩：

```text
runtime/static-gateway/Caddyfile
runtime/static-gateway/Caddyfile.bak
runtime/static-gateway/aliases/prd-workflow.conf
```

`sites/demo-static.conf`、`sites/voiceprint-v3-demo.conf`、`aliases/voiceprint-v3-demo.conf` 均不存在。

### 4.2 Registry 与运行态不一致

`static_sites` 表仍登记两个静态站点 enabled：

| instance_id | host_port | gateway | enabled |
| --- | --- | --- | --- |
| `demo-static` | 18000 | caddy | 1 |
| `voiceprint-v3-demo` | 18001 | caddy | 1 |

但真实进程与端口均不存在，因此管理页会呈现“期望运行但实际停止”的不一致状态。

### 4.3 构建不是失败点

`voiceprint-v3-demo` 的 `build.log` 显示三次构建都成功：

```text
npm ci
npm run build
vite v6.4.1 building for production...
✓ built
```

因此 `voiceprint-v3-demo` 无法访问不是前端构建失败，而是构建完成后网关启用失败。

### 4.4 Caddy 可执行文件本身可用

当前 Caddy 安装正常：

```text
/usr/local/bin/caddy
v2.11.4
```

这排除了“未安装 Caddy”或“版本不满足最低要求”的根因。

## 5. 根因分析

### C1. LWA 缺少 Caddy master 生命周期管理

`StaticGateway.reload_all()` 当前只执行 `caddy reload`，假设 Caddy admin 已经在线。但今天上午 `2019` admin 不监听，`8080` 也不监听，说明 Caddy master 没有运行。

结果是：

```text
LWA 启动静态实例
  -> 生成站点片段
  -> 调用 caddy reload
  -> admin 不在线 / Caddy 不可用
  -> reload 失败
  -> 实例 failed/stopped
```

LWA 需要在 Caddy 模式下拥有自己的 `gateway on/off/status` 或至少在 `reload_all()` 内确保 Caddy master 存活。

### C2. reload 失败后留下半启用配置

`enable()` 的 Caddy 路径大致流程是：

```text
generate_site_config()
generate_alias_config()
reload_all()
失败 -> remove_site_config()
失败 -> remove_alias_config()
```

问题是：删除 site/alias 片段后，没有重新组装主 `Caddyfile`，于是主配置保留了 import 行，但目标文件已删除。下一次 Caddy validate/start/reload 会先被坏 import 卡住。

这就是当前 `caddy validate` 报 `File to import not found` 的直接原因。

### C3. stale pid 未清理

`runtime/run/caddy.pid`、两个 `static-*.pid` 均指向不存在进程。状态文件没有被自动清理，会影响后续判断和人工排障。

### C4. doctor 覆盖不足

`lwa doctor` 当前认为 Caddy OK，因为它只检查了 Caddy 可执行文件与版本；`static_gateway` 也只是检查目录存在。它没有检查：

- `Caddyfile` 是否能 `validate/adapt`。
- admin `2019` 是否在线。
- `8080` 是否监听。
- registry 中 enabled 的静态实例端口是否真的可达。

因此这次事故里 doctor 未能提前暴露关键问题。

## 6. 立即恢复方案

在代码修复前，可以按以下方式恢复当前 runtime：

1. 停止/清理 stale Caddy pid。
2. 重新生成两个静态站点片段：
   - `runtime/static-gateway/sites/demo-static.conf`
   - `runtime/static-gateway/sites/voiceprint-v3-demo.conf`
3. 重新生成 `voiceprint-v3-demo` 的别名片段：
   - `runtime/static-gateway/aliases/voiceprint-v3-demo.conf`
4. 重新组装 `runtime/static-gateway/Caddyfile`，只 import 实际存在的片段。
5. 先 validate：

```bash
caddy validate --config runtime/static-gateway/Caddyfile --adapter caddyfile
```

6. 再启动 Caddy：

```bash
caddy start \
  --pidfile runtime/run/caddy.pid \
  --config runtime/static-gateway/Caddyfile \
  --adapter caddyfile
```

7. 验证：

```bash
curl -I http://127.0.0.1:18000/
curl -I http://127.0.0.1:18001/
curl -I http://127.0.0.1:18002/
curl -I http://127.0.0.1:8080/prd-workflow/api/health
curl -I http://127.0.0.1:8080/vp-app-demo-v3/
```

## 7. 长期修复方案

### P0：修复半启用 Caddyfile（BUG-069）

目标：任何 reload 失败都不能留下 dangling import。

建议改动：

- `reload_all()` 写临时主配置，先 `caddy adapt/validate`。
- validate 成功后再替换正式 `Caddyfile`。
- reload 失败时恢复旧主配置。
- `enable()` 删除 site/alias 片段后必须重新 `_assemble_main_config()` 并写回主配置。
- 增加回归测试：site/alias 片段被回滚后，主 `Caddyfile` 不再 import 不存在文件。

### P0：纳入 Caddy master 生命周期（DEV-041 / IMP-010）

目标：LWA 在 Caddy 模式下能自己启动、检测、重启 Caddy。

建议能力：

- `StaticGateway.ensure_caddy_running()`：
  - 若 `runtime/run/caddy.pid` 存在但进程不在，删除 stale pid。
  - 若 admin `127.0.0.1:2019` 不通，执行 `caddy start --pidfile ...`。
  - 若 Caddyfile validate 失败，阻止启动并返回明确错误。
- `reload_all()`：
  - validate 当前配置。
  - admin 不通时先 start。
  - reload 失败时可尝试一次 start/reload 恢复。
- CLI/manager：
  - 增加 `lwa gateway status/on/off` 或 manager 启动时自动 ensure。

### P1：增强 doctor/管理页可观测性

建议新增检查：

- Caddy 可执行文件与版本。
- Caddyfile validate。
- Caddy admin 是否在线。
- `staticGatewayPort` 是否监听。
- enabled 静态站点的 hostPort 是否监听且 `/` 返回 2xx/3xx。
- stale pid 文件提示。

### P1：修复状态同步

当前 registry 中 `static_sites.enabled=1` 但实例 `status=stopped`。建议：

- 观测到静态实例 stopped 时，同步 `static_sites.enabled=0`，或至少在 API 返回中明确 `enabledButUnreachable`。
- UI 显示“期望运行但网关不可达”，不要只显示普通 stopped。

## 8. 已同步任务

本次排查已同步到 `task-list.md`：

| ID | 类型 | 状态 | 内容 |
| --- | --- | --- | --- |
| `CHK-008` | 检查 | 已完成 | 2026-07-08 上午 LWA/Caddy 启动故障日志复查 |
| `BUG-069` | 修复 | 待修复 | Caddy reload 失败后主 Caddyfile 残留不存在 import |
| `DEV-041` | 开发 | 待开发 | IMP-010 Caddy master 生命周期纳入 LWA |

## 9. 建议执行顺序

1. 先做 BUG-069，保证失败不会继续污染主 `Caddyfile`。
2. 再做 DEV-041，把 Caddy master 生命周期纳入 LWA。
3. 再补 doctor/manager 的 Caddy 健康检查。
4. 最后做状态模型优化，区分 `stopped`、`gateway_down`、`config_invalid`。

