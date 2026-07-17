# 运维手册（Operations Playbook）

lwa 在局域网小主机上的日常运维、选型与排障速查。面向已 `lwa init` 并跑过若干实例的维护者。

> 配套阅读：[Runtime 工作区说明](runtime-workspace.md)（目录结构 / 端口）、[管理页说明](manager-page.md)、[开机自启](autostart.md)。

---

## 一、静态网关选型：Caddy vs builtin

lwa 的静态站点 / 路径别名由"静态网关"承载，两种后端二选一（`local-web.yml` → `staticGateway`）：

| 维度 | `caddy`（推荐） | `builtin`（兜底） |
| --- | --- | --- |
| 统一入口 / 路径别名 | ✅ `:<staticGatewayPort>` 站点块 + import 别名片段 | ❌ 多端口模式，**无统一入口**；`lwa alias set` 会被拦截（IMP-022） |
| 单点监听 | 1 个端口（默认 8080）聚合所有别名 | 每个静态实例各占 1 个 hostPort |
| master 生命周期 | `lwa gateway on/off/status` 管理（admin :2019 探活） | 无；每实例 `python -m http.server` 子进程 |
| 自愈 | reload 失败自愈、stale pid 清理（BUG-069/070） | daemon reconcile 重 spawn 死掉的静态进程 |
| 安装前提 | 需安装 Caddy ≥ `MIN_CADDY_VERSION` 并在 PATH | 无外部依赖，纯 Python |

**选型建议**：

- **生产 / 局域网共享访问** → 用 **Caddy**：路径别名、统一入口、可观测性（IMP-024 浏览量统计的 JSON access log）都依赖它。
- **临时 / 无 Caddy 环境** → 自动降级 `builtin`：每个静态站点独立端口可达，但**别名不可用**，浏览量统计仅能解析 `gateway.log`（CLF）。

切换：改 `local-web.yml` 的 `staticGateway` 后重启 manager/daemon。`lwa gateway off` 不校验版本，即便刚切到 builtin 也能关掉残留 Caddy master（避免"切 builtin 后关不掉 Caddy"死局）。

> 决策记录见 `task-list.md` CHK-013：阶段 0（P0）已把 Caddy 生命周期/原子配置/自愈落地，迁移 nginx 不省工作量，**维持 Caddy**。

---

## 二、inbox 投放规范（避免冗余实例）

`inbox/` 是 zip 投放区，daemon 与 `lwa import` 都扫这里。避免以下误用：

1. **勿放测试 zip**：daemon 会尝试导入 `inbox/` 下**所有** zip。测试样例请放 `tests/fixtures/`，不要丢进运行工作区的 `inbox/`，否则会被自动建实例。
2. **同包勿重复投放**：同一 zip 重复导入会按 `sourceZipHash` 指纹判定为冗余（IMP-012）。daemon 路径（IMP-011）slug 冲突时**不再自动建 `-2/-3`**，而是记 `import_conflict` 事件并提示用 `--update`。
3. **新版本用 update**：同一项目的新版本应 `lwa import inbox/foo-v2.zip --update <slug>`（保留 id/hostPort/data/别名），而非重复 import。
   - **容器实例**（DEV-067）：源码换入后会清空旧 `containerId`/`imageId`；若原为 running 且未传 `--no-restart`，会走 **`lwa rebuild`**（重建镜像），**不会**轻量 `restart`——后者不重建镜像，会造成「磁盘已新、容器仍旧」假绿。
   - **静态 / 前端**：换源码后 `restart` 即可同步 public。
   - `--no-restart`：只换源码；容器需稍后手动 `lwa rebuild <id>`。

### 导入成功后的归档

daemon 导入成功（started/pending/conflict 终态）后会把 zip **移入 `inbox/processed/`**（同名加时间戳），从扫描视野移除。手动 `lwa import` 不自动归档——导入后可自行移走或删除 zip。

### 批量清理冗余实例

```bash
lwa remove --redundant          # 预览同指纹冗余（保留每组最早者）
lwa remove --redundant --purge  # 确认后连磁盘一起清
```

管理页也可：实例列表「仅冗余」勾选 → 行内删除单条，或顶部「批量删除冗余」。

---

## 三、容器实例路径别名（IMP-014）

容器实例（docker-compose）同样支持路径别名，把 `hostPort` 反代到统一入口。步骤：

1. **前提**：`staticGateway=caddy` 且 `lwa gateway on`（builtin 不支持别名，IMP-022 会拦截）。
2. **部署容器**：`lwa start <id>` 拿到 hostPort（别名 reverse_proxy 的目标端口）。
3. **设置别名**：

   ```bash
   lwa alias set <id> <slug>
   # 或管理页 → 实例操作区「路径别名」（容器实例按钮现已可用，BUG-085 已修）
   ```

4. **访问**：`http://<LAN-IP>:<staticGatewayPort>/<slug>/`。
5. **端口漂移**：容器 restart 后 hostPort 若变化，`_sync_alias_port`（IMP-021）会自动重写别名片段并 reload，无需手动处理。

> SPA 子路径提示（IMP-023）：Vue/React 等用绝对资源路径（`/assets/…`）在 `/<slug>/` 下会 404 白屏；构建时设相对 base（Vite `base: './'`）或 `--base=/<slug>/`。纯静态 HTML 不受影响。

---

## 四、Caddy master 排障

| 现象 | 排查 | 处置 |
| --- | --- | --- |
| `lwa gateway status` 显示未运行 | admin :2019 不可达 | `lwa gateway on`（自动 validate→start→探活） |
| 别名 502 / 站点不通 | 实例 hostPort 未监听 / 容器未起 | `lwa status <id>` 看状态；`lwa start <id>`；实例 `gateway_down` 用管理页「恢复」或 `POST /api/instances/{id}/recover` |
| `caddy validate` 报悬空 import | BUG-069 类残留（已根治，偶发于历史脏配置） | `lwa gateway off` 再 `on`，会基于实际存在的 conf 重组主 Caddyfile |
| 切 builtin 后 Caddy 还在跑 | stale pid / 旧 master | `lwa gateway off`（不校验版本，强制 `caddy stop` + 清 `run/gateway.json`） |
| `lwa doctor` 报 Caddy 健康 FAIL | admin/validate/站点端口探测 | 按 doctor 提示处置；常见为 master 未起（`lwa gateway on`） |

健康探针（IMP-020）：`lwa doctor` 在 caddy 模式会探 admin :2019 + 主 Caddyfile `caddy validate` + 别名入口 / 各 enabled 站点 hostPort 可达性 + stale pid 提示。

### Caddy 配置位置

- 主配置：`static-gateway/Caddyfile`（由 `_assemble_main_config` 基于实际存在的片段组装，**永不 import 不存在文件**）。
- 站点片段：`static-gateway/sites/<id>.conf`。
- 别名片段：`static-gateway/aliases/<id>.conf`（`reverse_proxy 127.0.0.1:<hostPort>`）。

---

## 五、开机自启（IMP-030）

跨平台统一入口是 **`lwa autostart`**（前台监管单元，非旧版 detached `on`）：

```bash
lwa autostart install                  # 生成并启用 daemon + manager（managerEnabled 时）
lwa autostart install --with-caddy      # 额外监管 gateway（仅 staticGateway=caddy）
lwa autostart check                     # 完备性深检（解释器 / 单元 / 进程身份 / Caddy…）
lwa autostart status
```

`lwa setup --autostart` 仍可用，但已**委托**给 `lwa autostart install`（行为一致）。完整平台差异、停服协调与验收见 [开机自启](autostart.md)。

要点：

- **停服**：先 `lwa autostart disable`，再 `lwa daemon/manager/gateway off`（`off` 已内置 `coordinated_disable`）。
- **升级重启**：`lwa update` 重启 manager/daemon 时走 `coordinated_restart`——自启在管则交监督器（`kickstart -k` / `systemctl restart`），避免与 KeepAlive 抢锁。
- **daemon 自愈**（DEV-042）：watcher 启动时与每 60s 执行 `reconcile()`，恢复 `desired=running` 但状态偏离的实例。Caddy 后端且网关被显式 `lwa gateway off` 时跳过 caddy 静态。
- **Linux**：systemd user + 建议 `enable-linger`；**WSL** 另需 Windows 登录任务唤醒；**Windows 原生**见任务计划程序（[autostart.md](autostart.md)）。

---

## 六、日常巡检清单

```bash
lwa doctor               # 环境 + 实例健康（含 Caddy 探针）
lwa status               # 全部实例状态
lwa stats                # 整机 + 实例资源占用
lwa gateway status       # Caddy 网关状态
lwa manager status       # 管理页状态 + token
lwa daemon status        # daemon 自动导入状态
lwa list                 # 实例清单
```

异常态识别（DEV-043）：`gateway_down`（master 不可达）/ `config_invalid`（站点路由异常）会单独标注，管理页标"需恢复"并提供一键 recover。

---

## 七、网关切换交接与访问地址复核（gateway-switch-access-review）

复盘（2026-07-09）确认缺口：换网后管理页链接指向失效 LAN IP（G1）；「入口 HTML 200」≠「页面可渲染」（G2/G5，IMP-023 SPA 绝对资源空 200）；builtin↔caddy 切换未彻底交接导致同端口双开（G3）；切换后应检查是否需 rebuild（G6，默认只提示）。下列能力已落地：

### 7.1 切换事务（G3）

改 `staticGateway` 后执行 `lwa gateway on` 即完成**原子交接**：

- 切到 **caddy**：停掉所有残留 builtin 静态进程（含 pid 文件已丢失的孤儿——按「服务本工作区 `apps/` 的 `http.server` 进程」枚举捕获，§2.7 现场即此类），再拉起/确认 Caddy master。
- 切到 **builtin**：`lwa gateway off` 强制停 Caddy master（不校验版本，BUG-077）。
- `enable()` 启用单个站点前也会先停掉该实例仍存活的 builtin，杜绝同 hostPort 双开。

> 已知限制：切换事务的孤儿枚举用 POSIX `pgrep`；Windows 上仅靠 pid 文件（无 pid 文件的孤儿由 doctor `backend_handoff` 检测并提示人工处置）。

### 7.2 访问地址刷新（G1）

```bash
lwa access refresh   # 用当前 LAN IP 重算所有实例 lanUrl/routeUrl 并落盘
```

DHCP 换网 / 重启网关后管理页旧链接打不开时运行此命令即可自愈，无需逐实例 restart。`lwa gateway on` 启动时也会自动刷新。doctor 的 `lan_url_stale` 检查会告警漂移的实例。

### 7.3 访问可用性复核（G2/G5）

```bash
lwa access review    # 对声明 URL 做真探活（含 SPA 子资源空 200 检测）
lwa access review --json   # 机器可读
```

逐实例探测：回环 `127.0.0.1:<hostPort>`（权威，区分「服务没起」vs「LAN URL 陈旧」）、lanUrl、routeUrl；对别名入口解析 HTML 的绝对路径 `src`/`href`，对照「无前缀」与「带别名前缀」两种请求——前者 200 但 0 字节、后者有实体 → **IMP-023 风险**（SPA 需构建时设相对 base，如 Vite `base: './'`）。这才是别名下「可用」的真实口径，而非仅看入口 HTML 200。

`lwa gateway on` 交接收尾后会**默认**跑一次 access review（见 7.6）。

### 7.4 切换后 rebuild 兼容检查（G6）

产品共识：**默认只检查并提示，不自动 rebuild**；需要时显式加开关。

```bash
lwa gateway on                         # 交接 + 默认 access review，仅提示需 rebuild 的实例
lwa gateway on --rebuild-if-needed     # 对 IMP-023 命中实例自动 rebuild
lwa access review                      # 单独复核；文末列出建议 rebuild
lwa access review --rebuild-if-needed  # 复核后对命中实例自动 rebuild
```

- **触发 rebuild 建议 / 自动重建的条件**：仅 IMP-023 空 200。LAN 漂移、端口双开、回环不通等只提示对应命令，不触发 rebuild。
- **rebuild 后复检**：自动重建成功后会再探别名入口；若仍空 200，报告 `[WARN] rebuild 完成但 IMP-023 仍命中`（退出码非零），不会假绿成「已修好」。
- **注意**：自动 rebuild 不会改应用源码里的 Vite `base`；若未固化 `base: './'`（或等价），重建后别名下仍可能空 200——请先改构建配置再 rebuild。

### 7.5 doctor 新增检查项（建议 F/H）

`lwa doctor` 新增：

- `lan_url_stale`——实例 lanUrl 是否指向失效（漂移）LAN IP（WARN，提示 `lwa access refresh`）。
- `backend_handoff`——enabled 静态 hostPort 上是否 builtin + caddy 双开（FAIL，提示 `lwa gateway off` 再 `on`）。
- `port_contention`——`:2019` / 别名入口上是否有非预期监听者（测试/外部孤儿，§2.7 现场即 pytest 泄漏的 Caddy 占 :2019）；仅 caddy 后端检查。
- `port_pool`（建议 H）——排除 lwa 自用端口（managerPort、staticGatewayPort、registry 已分配 hostPort），不再把这些合法自用端口误报为冲突。

### 7.6 管理页兜底链接（建议 D）

实例列表除 LAN「端口」链接外，额外提供「本机」(`http://127.0.0.1:<hostPort>/`) 链接——LAN IP 漂移失效时仍可本机访问。`caddy start --pingback` 超时假失败（BUG-102）已修复：回退 admin :2019 探活，admin 在线即视为启动成功。

---

## 相关文档

- [Runtime 工作区说明](runtime-workspace.md) — 目录结构、端口、`.env.local`、资源档位
- [管理页说明](manager-page.md) — 筛选 / 冗余清理 / 路径别名 / 浏览量
- [开机自启](autostart.md) — launchd 细节
- [已知限制](known-limitations.md)
