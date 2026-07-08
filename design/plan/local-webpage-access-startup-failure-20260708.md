# Local Webpage Access 启动故障诊断报告

> **日期**：2026-07-08
> **范围**：2026-07-08 上午开机后，运行 Caddy 后两个网页无法启动的全部问题
> **状态**：诊断完成，待修复
> **关联模块**：`static_gateway.py` / `path_alias.py` / `daemon.py` / `lifecycle.py`

---

## 一、一句话结论

**今天上午开机后，Caddy 进程没有被拉起（lwa 从不管理 Caddy 的生命周期），lwa 自动尝试恢复两个静态实例时 `caddy reload` 全部失败；而 `enable()` 的回滚逻辑与 `reload_all()` 的回滚逻辑相互矛盾，删掉了被引用的 `sites/*.conf`，导致主 Caddyfile 出现悬空 import——从此 Caddy 彻底无法启动，两个网页陷入"恢复 → 失败 → 回滚"死循环。**

---

## 二、故障全景

| 实例 | 注册表状态 | 进程 | 端口 | last_error |
|------|-----------|------|------|-----------|
| `demo-static` | desired=**running** / status=**stopped** | pid 93888 **已死** | 18000 **未监听** | `[GATEWAY_ERROR] Caddy reload 失败` (09:55:53) |
| `voiceprint-v3-demo` | desired=**running** / status=**stopped** | pid 96928 **已死** | 18001 **未监听** | `[GATEWAY_ERROR] Caddy reload 失败` (09:55:37) |
| `prd-workflow` | running / running | docker 容器 | 18002 ✅ | 无（Docker restart policy 自启） |
| **Caddy 主进程** | — | pid 79233 **已死**（7/7 16:59 起就没更新过日志） | 8080 **未监听** | — |
| daemon | enabled pid 35425 | ✅ 09:47 起 | — | — |
| manager | enabled pid 35422 | ✅ 09:47 起 | 17800 | — |

**关键观察**：

- **三个目标网页 = `demo-static` + `voiceprint-v3-demo` + `prd-workflow`**；前两个死了，`prd-workflow` 靠 Docker restart policy 侥幸存活，但它的别名入口 `:8080/prd-workflow/` 也因 Caddy 没起而不通。
- Caddy 进程 pid 79233 是 7/7 16:59 启动的，mac 重启后丢失，且没有任何机制重新拉起它。
- daemon / manager 在 09:47 正常自启，但它们既不管理 Caddy，也不恢复死掉的静态实例进程。

### 配置一致性损坏

当前主 `Caddyfile` 引用了 3 个**已不存在**的文件：

```
import `.../sites/demo-static.conf`          # ❌ sites/ 目录为空
import `.../sites/voiceprint-v3-demo.conf`   # ❌ 同上
:8080 {
    import `.../aliases/prd-workflow.conf`        # ✅ 存在
    import `.../aliases/voiceprint-v3-demo.conf`  # ❌ 不存在
}
```

`caddy validate` 实测：

```
Error: adapting config using caddyfile: File to import not found:
.../sites/demo-static.conf, at Caddyfile:1
```

即 **Caddy 已处于无法启动状态**——即使手动 `caddy run`，也会因 import 悬空而拒绝加载配置。

---

## 三、时间线（来自 builds 表 + registry，均为 2026-07-08）

| 时间 | 事件 | 结果 |
|------|------|------|
| 09:47:22–23 | daemon / manager 开机启动 | ✅ |
| 09:47:51→58 | voiceprint 恢复尝试 #1（build#21） | ❌ `[GATEWAY_ERROR] Caddy reload 失败` |
| 09:48:01 | prd-workflow 容器健康检查通过 | ✅（Docker 自启） |
| 09:51:54→57 | voiceprint build 步骤成功（build#22） | npm build 本身没问题 |
| 09:55:27→33 | voiceprint 恢复尝试 #2（build#23） | ❌ 同样 reload 失败 |
| 09:55:37 / 09:55:53 | 两个实例最终落盘 status=stopped | 期望运行却停止 |
| 之后 | 用户手动运行 Caddy | ❌ Caddyfile 悬空，启动失败 |

---

## 四、根因（三层叠加）

### 🔴 根因 A：Caddy 进程无生命周期管理（架构缺口）

全代码库**没有任何一处 `caddy run` / `caddy start`**（全局 grep 确认）。lwa 只调 `caddy reload`，而 reload **要求 Caddy admin API（127.0.0.1:2019）已在运行**。

- 没有 launchd plist（`launchctl list | grep caddy` 为空）。
- Caddy 进程是 7/7 手动拉起的，mac 重启后自然消失。
- → 今天 `caddy reload` 找不到 admin API → **100% 必失败**。

源码中 `caddy` 仅出现在三处，均为被动角色：

| 文件 | 用途 | 是否拉起进程 |
|------|------|-------------|
| `config.py` | 配置项 `staticGateway = "caddy"` | 否 |
| `doctor.py` | `caddy version` / `caddy list-modules` 探测 | 否 |
| `static_gateway.py` | `caddy reload`（仅此一种调用） | 否（要求已运行） |

### 🔴 根因 B：双重回滚矛盾（产生悬空 import）

这是让 Caddy "再也起不来"的致命点，位于 `static_gateway.py`：

```python
# enable() 的 Caddy 分支（行 321–329）
try:
    self.reload_all()          # ← 内部失败会把主 Caddyfile 回滚到 previous
except GatewayError:
    self.remove_site_config(instance_id)   # ← 再删掉 sites/X.conf 文件
    self.remove_alias_config(instance_id)
    raise
```

而 `reload_all()`（行 386–438）失败时的回滚行为：

```python
if result.returncode != 0:
    if previous is not None:
        main.write_text(previous, encoding="utf-8")   # 主 Caddyfile 回滚到 previous
        ...
    raise GatewayError("Caddy reload 失败", ...)
```

**两层回滚叠加产生矛盾**：

1. `reload_all()` 把主 Caddyfile 回滚到 `previous`——而 `previous` 是**上次成功时的主配置，里面仍写着 `import sites/X.conf`**。
2. `enable()` 紧接着把 `sites/X.conf` 文件删除。
3. 最终状态 = 主 Caddyfile 引用一个刚刚被删除的文件 = **悬空 import**。

`caddy validate` 已实测验证（见第二节）。

### 🟡 根因 C：缺少开机自愈 / 进程监管

`daemon.run_watcher`（`daemon.py:464`）只扫描 inbox 处理新 zip，**不恢复 `desired=running` 但已死的实例**：

```python
while not stop_event.is_set():
    ...
    for zip_path in scan_inbox(workspace):
        ...  # 只处理新导入的 zip
```

`manager_service.run_service_main` 也只启动 FastAPI，不做实例恢复。

今天 09:47–09:55 的恢复尝试（来源未完全定位，疑似 manager 启动或手动操作）撞上了根因 A+B，反而把配置越弄越坏。

---

## 五、立即修复方案（手动恢复当前服务，约 3 分钟）

**核心思路**：先让 Caddy 用最小配置跑起来（打通 admin API）→ 再让 lwa 重新 enable 实例（重新生成 conf + 组装正确 Caddyfile）。

```bash
# 0. 进入项目目录
cd /Users/fenix-macmini/Documents/VSCode/1-AI-Coding/6-自制小工具/8-本地简单网页部署基座/local-webpage-access

# 1. 备份并重置悬空的主 Caddyfile（只保留 admin，让 caddy 能裸启）
cp runtime/static-gateway/Caddyfile runtime/static-gateway/Caddyfile.broken-20260708
cat > runtime/static-gateway/Caddyfile <<'EOF'
{
	admin 127.0.0.1:2019
}
EOF

# 2. 拉起 Caddy（后台），验证 admin API
caddy start --config runtime/static-gateway/Caddyfile --adapter caddyfile
curl -s http://127.0.0.1:2019/config/ >/dev/null && echo "✅ caddy admin OK"

# 3. 重新启动两个静态实例 —— lwa 会重新生成 sites/*.conf + alias conf 并 reload
lwa start demo-static
lwa start voiceprint-v3-demo

# 4. 验证
lwa status
curl -sI http://127.0.0.1:18000/ | head -1                              # demo-static
curl -sI http://127.0.0.1:18001/ | head -1                              # voiceprint 直连
curl -sI http://127.0.0.1:8080/voiceprint-v3-demo/ | head -1            # 别名入口
```

> 第 3 步 `lwa start` 走的是 `enable()` 完整流程，这次 Caddy 已在跑、Caddyfile 已不悬空，reload 会成功，conf 会正常落盘。

---

## 六、代码层修复方案（防止复发，按优先级）

| 优先级 | BUG | 位置 | 修复方案 |
|--------|-----|------|---------|
| **P0** | Caddy 进程无生命周期管理 | `static_gateway.py` | 新增 `ensure_caddy_running()`：`reload_all()` 前先探测 `127.0.0.1:2019`，不可达则 `caddy start` 拉起（加载当前主 Caddyfile 或最小配置）。reload 不再裸奔。 |
| **P0** | 双重回滚矛盾 | `static_gateway.py:321–329` | enable catch 里 `remove_site_config` 后，**同步从主 Caddyfile 删除对应 import 行**；或主 Caddyfile 改用 glob `import sites/*.conf`（空目录天然不悬空）。 |
| **P1** | 缺少开机自愈 | `daemon.py:run_watcher` | 启动时增加 `reconcile()`：扫描 `desired=running ∧ status≠running` 的实例，逐一 restart；并对 builtin 静态服务做进程存活监管。 |
| **P1** | 无开机自启 | `setup.py` | `lwa setup` 生成 launchd plist，开机自启 daemon + manager（+ 可选 caddy）。 |

### P0 详细设计

**`ensure_caddy_running()`（新增）**：

```python
def ensure_caddy_running(self) -> bool:
    """reload 前确保 Caddy admin API 可达；不可达则用当前主配置拉起。"""
    # 1. 探测 admin API
    try:
        urllib.request.urlopen("http://127.0.0.1:2019/config/", timeout=1)
        return True  # 已在运行
    except Exception:
        pass
    # 2. 拉起：优先当前 Caddyfile，否则最小配置
    main = self.main_config_path()
    config = str(main) if main.exists() else _MIN_CADDYFILE
    result = subprocess.run(
        ["caddy", "start", "--config", config, "--adapter", "caddyfile"],
        capture_output=True, timeout=15,
    )
    if result.returncode != 0:
        raise GatewayError("Caddy 进程拉起失败", stderr=...)
    return True
```

`reload_all()` 第一行改为 `self.ensure_caddy_running()` 后再执行 reload。

**悬空 import 修复**：`_assemble_main_config` 改用 glob 引用，或 enable 回滚时调用 `self._assemble_main_config()` 重新组装（基于实际存在的 conf 文件 / static_sites 表过滤）。

---

## 七、验证方法

修复后应通过以下检查：

1. **Caddy 自愈**：杀掉 caddy 进程 → `lwa start <任意静态实例>` → 应自动拉起 caddy 并成功 reload。
2. **回滚不悬空**：构造 reload 失败场景（如占用端口）→ 检查 `caddy validate` 主 Caddyfile 通过、`sites/` 与 import 行一致。
3. **开机自愈**：停掉所有静态服务进程 → 重启 daemon → desired=running 的实例自动恢复。
4. **`caddy validate`**：主 Caddyfile 始终可解析（无悬空 import）。
5. **端到端**：`http://<host>:8080/voiceprint-v3-demo/`、`:8080/prd-workflow/`、`:18000`、`:18001` 均可访问。

---

## 附录：诊断证据来源

- `runtime/logs/caddy.log`（Caddy 仅 7/7 16:59 有从 Caddyfile 加载记录，之后无任何活动）
- `runtime/registry/local-web.db`（`instances` 表 last_error、`static_sites` 表端口分配、`builds` 表失败时间线）
- 进程存活检查（`ps -p`、`lsof -nP -iTCP -sTCP:LISTEN`、`pgrep caddy`）
- `caddy validate` 实测（确认悬空 import）
- `static_gateway.py` 源码：`enable()`（行 270–330）、`reload_all()`（行 386–438）回滚逻辑
- `daemon.py:run_watcher`（行 464–529）、`manager_service.run_service_main`（行 347–382）

---

*报告由 Claude Fable 5 生成 · 2026-07-08*
