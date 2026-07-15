# LWA Caddy 启动故障复盘与修复方案

> 分析时间：2026-07-08  
> 范围：`runtime/` 工作区、Caddy 网关、今天上午（09:47~09:55）`lwa update` / restart 链路  
> 触发：用户反馈 Caddy 模式下至少两个网页无法启动

---

## 1. 当前实况（09:59 左右）

| 实例 | registry 状态 | 期望 | 实际可达 | 问题 |
|------|--------------|------|---------|------|
| **demo-static** | stopped | running | 18000 无响应 | Caddy 站点配置已被回滚删除 |
| **voiceprint-v3-demo** | stopped | running | 18001 / 8080 别名均无响应 | 站点 + 别名片段均已删除 |
| **prd-workflow** | running | running | **18002 正常 (200)** | **8080/prd-workflow/** 不可用（Caddy 未运行） |

**基础设施状态：**

- **Caddy 进程**：未运行（`pgrep caddy` 为空）
- **Caddyfile**：仍 `import` 已不存在的 `sites/demo-static.conf`、`sites/voiceprint-v3-demo.conf`，`caddy validate` 直接失败
- **sites/**、**aliases/voiceprint-v3-demo.conf**：空 / 缺失
- **run/static-*.pid**：仍指向 7 月 7 日 builtin 模式的旧进程（93888、96928，已不存在）
- **registry**：`static_sites.enabled=1`，但 `status=stopped`，状态不一致

---

## 2. 今天上午启动时间线（registry 事件）

```
09:47  lwa update --restart-instances
       ├─ demo-static restart：caddy reload 超时 15s（IPv6 [::1]:2019 + master 不稳定）
       ├─ voiceprint restart：Caddy reload 失败
       └─ prd-workflow restart：成功 ✓

09:48~09:49  多次 lwa start 失败（Caddy master 未运行 / reload 失败）

09:51  修复 BUG-068（--address 127.0.0.1:2019）+ 手动 nohup caddy run
       ├─ demo-static start ✓
       └─ voiceprint start ✓

09:52:08  sync_status（管理页刷新触发）
       ├─ demo-static：running → stopped
       └─ voiceprint：running → stopped
       （Caddy 已不在 / 端口无监听，健康探测失败）

09:55  管理页/API 再次 restart（推测）
       ├─ voiceprint：build 成功 → Caddy reload 失败 → 回滚删配置
       └─ demo-static：Caddy reload 失败 → 回滚删配置
```

**关键结论：** 09:51 曾短暂成功，约 **11 秒后被 sync_status 打回 stopped**；09:55 再次 restart 失败后，进入现在的「半残配置」状态。

---

## 3. 根因分析（6 个问题）

### 3.1 问题 1：Caddy master 无生命周期（IMP-010，P0）

LWA 只做 `caddy reload`，**从不保证 Caddy 主进程在跑**。  
主进程退出后，所有静态实例的 enable/disable/restart 都会失败。

`runtime/logs/caddy.log` 可见多次 start/reload/shutdown 循环，但没有 LWA 侧的守护或自动拉起。

### 3.2 问题 2：reload 失败后的回滚逻辑有缺陷（原 DOC BUG-068 语义）

`enable()` 失败时会：

1. 删除刚生成的 `sites/<id>.conf`
2. 把 Caddyfile **回滚到上一版**

上一版 Caddyfile 仍 import 这些文件 → **主配置引用已删文件** → Caddy 无法 validate/start，形成死锁。

这就是现在 Caddyfile 还在 import 不存在文件的原因。

### 3.3 问题 3：macOS IPv6 reload（BUG-068，已部分修复）

上午 09:47 的失败主因：`caddy reload` 连 `[::1]:2019` 超时/拒绝。  
代码里已加 `--address 127.0.0.1:2019`，但 **manager/daemon 是 09:47 启动的**，若之后未 `lwa update` 重启 manager，管理页触发的 restart 可能仍用旧逻辑。

### 3.4 问题 4：builtin 遗留 PID 污染（BUG-069）

7 月 7 日 OPS-014 曾切到 `staticGateway: builtin`，留下：

- `run/static-demo-static.pid` → 93888
- `run/static-voiceprint-v3-demo.pid` → 96928
- `gateway.log` 里的 Python `http.server` 记录

切回 Caddy 后这些 PID 文件**未清理**。  
`is_enabled()` 只看 PID 存活，Caddy 模式又不写新 PID → 状态判断与观测逻辑混乱。

### 3.5 问题 5：Caddy 模式下状态观测不准确

`_observe_static_status` 在 PID 失效后会 fallback 到 `health_check(port)`（BUG-052）。  
但 Caddy master 挂掉后 18000/18001 无监听 → **09:52:08 被标为 stopped**，尽管 registry 里 `enabled=1`、`desired=running`。

### 3.6 问题 6：历史 builtin 与 Caddy 端口冲突（上午加剧）

上午 restart 前，18000/18001 被 orphan 的 builtin `http.server` 占用，导致：

- 端口分配漂移到 18003
- Caddy 配置膨胀（18003~18006）
- reload 更易失败

`runtime/logs/caddy.log` 中可见 reload 后端口从 8080 扩至 18000~18006 的记录。

---

## 4. 修复方案

### 4.1 立即恢复（运维，约 5 分钟）

```bash
cd runtime

# 1. 清理 stale 状态
rm -f run/static-demo-static.pid run/static-voiceprint-v3-demo.pid run/caddy.pid

# 2. 重置 Caddyfile（仅保留 prd 别名，避免 import 缺失文件）
cat > static-gateway/Caddyfile <<'EOF'

# IMP-006 路径别名统一入口（端口 8080）
:8080 {
	import `/Users/fenix-macmini/Documents/VSCode/1-AI-Coding/6-自制小工具/8-本地简单网页部署基座/local-webpage-access/runtime/static-gateway/aliases/prd-workflow.conf`
}
EOF

# 3. 启动 Caddy master
nohup caddy run --config static-gateway/Caddyfile --adapter caddyfile \
  >> logs/caddy.log 2>&1 &
echo $! > run/caddy.pid

# 4. 确保 CLI 含 BUG-068 修复
cd .. && pip install -e . -q && cd runtime

# 5. 重启 manager（加载新代码）
lwa manager off && lwa manager on

# 6. 拉起静态实例
lwa restart demo-static
lwa restart voiceprint-v3-demo
```

**验证：** `curl 127.0.0.1:18000/`、`18001/`、`8080/vp-app-demo-v3/`、`8080/prd-workflow/api/health` 均应 200。

**临时兜底：** 若 Caddy 仍不稳定，可将 `local-web.yml` 改 `staticGateway: builtin`（参考 OPS-014），demo/voiceprint 走端口直连，**8080 路径别名会失效**，仅 prd 需另做反代。

### 4.2 代码修复（按优先级）

| 优先级 | ID | 改动 | 说明 |
|--------|-----|------|------|
| **P0** | **IMP-010** | `ensure_caddy_running()` | `reload_all()` 前检测 admin :2019；不在则 `caddy start`；reload 失败再 start+reload；写入 `run/caddy.pid` |
| **P0** | **BUG-069** | 回滚一致性 | enable 失败时：**重新 `_assemble_main_config()`** 写 Caddyfile，而非盲回滚旧文件；避免 import 幽灵文件 |
| **P0** | **BUG-069** | PID 清理 | Caddy 模式 enable 前删除 `run/static-<id>.pid`；`is_enabled()` 在 caddy 模式下改查 `health_check(host_port)` |
| **P1** | **BUG-068** | manager 热重载 | `lwa update` 后强制重启 manager，或 manager 启动时校验自身代码版本 |
| **P1** | **IMP-020** | doctor 探针 | 增加「Caddy master / 8080 别名入口」检查，失败给 WARN + 修复指引 |
| **P1** | 自动恢复 | desired=running 且 observed=stopped | manager 定时或 sync 后提示「一键 recover」，或 daemon 自动 `lwa start` |
| **P2** | **IMP-011** | daemon inbox | 避免测试 zip 重复 import 污染（历史 9 个冗余实例根因，见 20260707 复盘） |

### 4.3 架构建议（中长期）

当前 Caddy 模式把 **站点端口（:18000/:18001）和别名入口（:8080）** 都交给同一个 Caddy master，但 LWA 不管理该 master，运维上容易「CLI 说 running、浏览器打不开」。

两种方向：

1. **完善 Caddy 托管**（推荐）：IMP-010 + `lwa gateway status/on/off`，与 manager/daemon 同级
2. **回退 builtin + 独立 Caddy 只做 8080 别名**（OPS-012 做法）：静态站走 builtin，Caddyfile 仅 `:8080 { reverse_proxy ... }`，职责分离、冲突更少

---

## 5. 相关日志与证据

| 路径 | 内容摘要 |
| --- | --- |
| `runtime/registry/local-web.db` events 表 | 09:47~09:55 完整 restart/error/status_change 链 |
| `runtime/logs/caddy.log` | 多次 start/reload/shutdown；reload 后端口扩至 18003~18006 |
| `runtime/static-gateway/Caddyfile` | import 指向已删 site/alias 文件，validate 失败 |
| `runtime/run/static-*.pid` | 指向已死 builtin 进程 |
| `runtime/apps/*/local-web.json` | `lastError: [GATEWAY_ERROR] Caddy reload 失败`，`updatedAt` 09:55 |

---

## 6. 结论摘要

今天上午的核心不是「网页本身坏了」，而是 **Caddy 网关链路的系统性缺陷**：

1. 09:47 `update --restart-instances` 在 Caddy 不稳定时把两个静态站打挂  
2. 09:51 临时修复后，Caddy master 未纳入 LWA 守护，11 秒后被 sync 判 stopped  
3. 09:55 再次 restart，失败回滚留下 **无效 Caddyfile**，现在两个静态站 + 8080 别名全部不可用  
4. prd-workflow 容器正常，仅 **8080 路径别名** 因 Caddy 挂掉不可达  

**建议下一步：** 先执行 §4.1 运维恢复，再排期 IMP-010 + BUG-069 代码修复。

---

## 7. 关联文档

- [Runtime 运维复盘（2026-07-07）](../../docs/local-webpage-access-runtime-analysis-20260707.md)
- [IMP-010~021 改进规划](./local-webpage-access-imp010-021-plan-20260707.md)
- [Runtime 工作区说明](../../docs/runtime-workspace.md)
