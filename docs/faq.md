# 常见问题与排障（WBS-30.09）

## 快速排障入口

**首次部署**请先运行：

```bash
lwa setup             # 检测宿主机工具，打印安装指引（default，无需工作区）
lwa setup --script    # 打印内置 Docker/Caddy 安装脚本路径（需人工审阅后执行）
lwa init              # 初始化工作区
# Full 二选一：
lwa init --full --yes              # 初始化并装齐依赖 + 能力闭环（非 TTY 必须 --yes）
# 或先 init，再在工作区内：
lwa setup --full --yes             # 装齐依赖并做 Full 能力闭环（需要已 init）
lwa setup --full --resume          # 重登后继续验收（exit 2 时）
```
| 档位 | 命令 | 行为 |
| --- | --- | --- |
| default（缺省） | `lwa setup` / `lwa init` | 检测+指引；缺 Docker 时 TTY 可询问是否跑内置脚本；`setup` 无需工作区 |
| full | `lwa setup --full` / `lwa init --full` | 安装 Caddy/Docker/Compose，并验收 Full 能力闭环（见下）；**需已初始化工作区**；非 TTY 必须 `--yes` |

`--default` 与 `--full` 互斥。内置脚本覆盖 macOS / Ubuntu LTS（22.04/24.04/26.04）/ Debian Stable（12/13）（含 WSL2）。**Windows 原生不受支持**——请在 WSL2 内安装运行。详见 [运维手册 · 宿主机装配](operations-playbook.md#零宿主机装配imp-031032033) 与 [已知限制](known-limitations.md)。

遇到问题时，第一步永远是：

```bash
lwa doctor          # 检查 Python / Docker / Compose / 端口池 / registry / 磁盘 / 内存
lwa doctor <id>     # 对单个实例做深度诊断（日志、状态、文件）
lwa doctor --json   # 机器可读报告（含 platformSupport；未 init 亦可输出平台诊断）
```

`doctor` 有 fail 时退出码为 1，可在脚本/CI 中用作门禁。未初始化工作区时，人类可读模式仍需工作区；`--json` 会尽量输出 `platformSupport` 供平台排障。

### Full Profile（IMP-033）

```bash
lwa doctor --profile full     # 输出 overall / 各上下文 Docker / Caddy owner / 建议动作
lwa capabilities --json       # 与 doctor full 同源 CapabilityReport
lwa setup --full --resume     # 组权限刷新后继续；exit 2=session_refresh_required，exit 1=unready
```

| overall | 含义 | 常见动作 |
| --- | --- | --- |
| `ready` | Full 强制能力均已证明 | 可无人值守跑容器与 Caddy |
| `session_refresh_required` / exit 2 | 已加 docker 组但当前会话未继承 | 重登或 `newgrp docker` 后 `--resume`，并重启 manager/daemon |
| `unready` / `degraded` | 缺组件、权限不足、Caddy owner 不匹配、后台缓存未闭环等 | 按 `action` 字段提示修复；**不得**当作安装成功 |

Full 下系统 `caddy.service` / 外部占用 `:2019` 会 fail-closed；须由 LWA gateway 以 `serviceUser` 托管。

## 症状 → 日志文件 → 命令（IMP-034）

| 症状 | 先看哪个文件 | 命令 |
| --- | --- | --- |
| CLI 操作后无痕迹 / 关终端丢日志 | 工作区 `logs/lwa.log` | 任意 `lwa status` / `lwa start` 后再 `tail logs/lwa.log` |
| daemon 不导入 inbox | `logs/daemon.log` | `lwa daemon status`；`lwa doctor` |
| 管理页异常 / 能力降级横幅 | `logs/manager.log`、`/api/health`（须本机或带 token） | `lwa manager status`；`lwa capabilities --json` |
| 构建无输出、`build.log` 长时间为空 | `apps/<id>/logs/build.log` + `logs/lwa.log`（阶段事件）+ registry events | `lwa logs <id> --category build`；`lwa doctor <id>` |
| 管理页显示 stopped 但容器仍在跑 | `observationError` / `runtimeAccess=permission_denied`；`capability probe` | `lwa doctor --profile full`；`newgrp docker` 后重启 manager/daemon |
| 容器 API 返回 409 `capability_denied` | 管理页或 curl 直调生命周期 | 先修 Docker 能力再操作；静态实例不受此阻断 |
| Caddy reload 权限失败 / owner 不匹配 | `logs/gateway.log`；系统 `caddy.service` | `lwa gateway status`；`lwa setup --full --resume` |

## 环境类问题

### Python 版本不满足

```
[fail] python_version: 需要 Python ≥ 3.13
```

`lwa` 依赖 Python 3.13+（pydantic v2 / typing 特性）。升级 Python 或用 `pyenv`/`uv` 管理。

### Docker 不可用

```
[fail] docker: Docker 不可用
```

* 确认 `docker` 命令在 PATH 中：`docker version`。
* Linux：确认 dockerd 已启动（`systemctl status docker`），当前用户在 `docker` 组。
* macOS：确认 Docker Desktop 已启动；WSL2：确认 Desktop integration 或发行版内 Engine 仅保留一套。
* 未安装时可用内置脚本：`lwa setup --script` 查看路径，或 `lwa setup --full --yes` / `lwa setup --install-docker`（macOS/Linux）。
* 静态/前端实例不需要 Docker，可继续使用。

### Docker 权限不足（管理页显示 stopped，容器实际在跑）

```
[fail] docker: Docker 权限不足（无法访问 docker.sock）
```

或实例 `last_error` 含「Docker 权限不足」。

常见于刚执行 `usermod -aG docker` 后：**当前 shell / manager / daemon 尚未继承 docker 组**。

1. `newgrp docker` 或注销后重新登录；
2. 若刚跑过 `setup --full` 且 exit 2：执行 `lwa setup --full --resume`；
3. 重启后台进程，使与 CLI 权限一致：

```bash
lwa manager off && lwa manager on
lwa daemon off && lwa daemon on
# 若用了自启动：
# systemctl --user restart lwa-manager.service lwa-daemon.service
```

4. 再 `lwa capabilities --json` / `lwa status <实例>` / 刷新管理页。

LWA 以安装用户（`serviceUser`）身份运行，不要求 root；CLI、manager、daemon 须共享同一 docker 组身份。观测失败时 registry 写 `observedState=unknown` / `runtimeAccess=permission_denied`，**不会**把运行中容器误标成 stopped；daemon reconcile 也会跳过自动纠正。管理页与 API 在能力降级时阻断容器启停（前端横幅 + 后端 `409 capability_denied`）。

macOS 通常走 Docker Desktop 用户态 socket，较少出现 Linux 式 docker 组问题；权限异常时仍以 `lwa doctor --profile full` 为准。

### Docker Compose 不可用

```
[fail] docker_compose: Docker Compose 不可用
```

V1 要求 Docker Compose 插件（`docker compose` 子命令）。安装 `docker-compose-plugin`，
或升级 Docker Desktop。检测到 v1 独立二进制时会提示改用插件。

### 磁盘空间不足

```
[fail] disk_space: 磁盘剩余 0.8 GB，低于阈值 1.0 GB
```

清理 `inbox/`（已导入的原始 zip）、`logs/`、或 `apps/<id>/source/`（原始快照）。
也可迁移整个工作区到更大磁盘。

## 导入类问题

### zip 导入失败：路径穿越（zip slip）

```
ZipImportError: 检测到路径穿越（zip slip）：../../etc/passwd
```

导入器对所有 zip 成员做 `_safe_extract` 检查，任何成员解析后落在解压目录之外都会被拒绝。
这是 [安全边界](security-boundary.md) 的强制保护。请用正规工具重新打包。

### 实例识别为 pending

```
status: pending
```

扫描器没能确定运行形态。常见原因：

* 项目根目录缺少 `package.json` / `requirements.txt` / `pyproject.toml` 等特征文件。
* zip 内有多层嵌套目录且特征文件不在拍平后的根。
* 项目结构特殊（自定义构建）。

处理：`lwa scan <id>` 重新识别；仍 pending 时检查 `local-web.json` 的 `lastError`，
或手工补特征文件后重扫。pending 实例会写入「未知 zip 来源」风险提示事件。

### slug 冲突与冗余实例

* **手动 `lwa import`**：同名 slug 已存在时会**报错**，提示使用 `lwa import <zip> --update <id>` 原地升级；不会静默覆盖，也不会自动建 `my-site-2`。
* **daemon 自动导入（IMP-011）**：slug 冲突时记 `import_conflict` 事件并提示 `--update`，**不再**自动追加 `-2/-3`；导入成功后 zip 会移入 `inbox/processed/`。
* **`--update` 后容器仍是旧版？**：容器实例必须 **rebuild 镜像** 才会跑新源码。V0.5.2 起，running 容器的 `--update` 默认走 `lwa rebuild`（不再轻量 `restart`）。若用了 `--no-restart`，请手动 `lwa rebuild <id>`。
* **同包重复导入**：同一 zip 指纹（`sourceZipHash`）会产生冗余实例。清理：

  ```bash
  lwa remove --redundant          # 预览并清理（保留每组最早者）
  lwa remove --redundant --purge  # 连磁盘一起清
  ```

  或在管理页勾选「仅冗余」后「批量删除冗余」。任意项目的行内「删除」走 IMP-035 双阶段确认（可仅移除或彻底删除）。详见 [管理页](manager-page.md) 与 [运维手册](operations-playbook.md)。

## 容器类问题

### 构建失败（OOM）

小主机并发构建易 OOM。`local-web.yml` 的 `buildConcurrency` 默认 1，
**不建议调高**。仍 OOM 时：

* 降低 `defaultResourceLimits.memory`（但需保证应用能启动）。
* 用资源 profile 更小的实例（`resourceProfile: tiny`）。
* 查看 `apps/<id>/logs/build.log` 定位具体失败步骤。

### 取消进行中的构建

排队中或正在 `npm`/`pip`/`docker compose build` 时可用：

* CLI：`lwa cancel-build <id>`
* 管理页：实例行「取消构建」
* API：`POST /api/instances/{id}/cancel-build`

取消只停止当前工作，**不会**自动删除构建缓存、旧镜像或用户数据。
排队任务直接 `cancelled`；进行中会先进入 `cancelling`，再落到
`cancelled` 或 `cancel_failed`（不会仅因发出请求就假报已停）。

### 容器启动后立即退出

```
status: failed, lastError: 容器退出码 1
```

* `lwa logs <id> --category run` 看应用日志。
* 常见：应用监听 `127.0.0.1` 而非 `0.0.0.0`（容器内需监听 `0.0.0.0` 才能被端口映射访问）。
* 常见：`internalPort` 与应用实际监听端口不一致。检查 `local-web.json` 的 `container.internalPort`。

### 端口池耗尽

```
PortError: 端口池 [18000, 19999] 已耗尽
```

* `lwa stats` 查看端口池占用。
* 多数情况是僵尸进程持有端口（异常退出未释放）。Linux：`ss -tlnp | grep 180`；
  （若在 Windows 宿主侧查端口）`netstat -ano | findstr 180`，`taskkill /PID <pid> /F`；正式运行请在 WSL2 内操作。
* 必要时扩大 `portPool` 范围（修改 `local-web.yml` 后重启管理页/daemon）。

## 管理页类问题

### 管理 token 丢失

token 存在工作区 `run/manager-token.json`。删除该文件后 `lwa manager on`（或 `lwa manager start`）会重新生成。
**重置 token 会使旧 token 失效**。完整 token 只出现在 CLI 终端输出，不会写入 `logs/`。

### 管理页打不开 / 401

* 确认端口未被占用：`lwa doctor` 的 port_pool 检查会覆盖 managerPort。
* 确认 token 正确（复制时勿带前后空格）。
* 若绑定到 `0.0.0.0` 但无 token，启动会被 `validate_manager_binding` 拒绝。

### 管理页状态与 CLI 不一致

管理页每次 `GET /api/instances` 都会先观测回写状态，理论上始终一致。
若仍不一致，运行 `lwa status` 强制刷新，或 `lwa doctor <id>` 诊断该实例。

## 访问类问题

### 管理页「端口」打开旧局域网地址

换 Wi-Fi / DHCP 续约后，本机 LAN IP 变了，但实例 `local-web.json` 里可能仍是旧 `lanUrl`：

* **即时可用**：管理页列表的「端口」链接按**当前** LAN IP 读时合成（IMP-040），一般无需手动操作即可点开。
* **落盘自愈**：列表轮询会节流调用 `access refresh`；也可 `POST /api/access/refresh` 或 CLI `lwa access refresh`。
* **升级后**：`lwa update` 在重启 manager/daemon 之后固定 refresh（可选 `--no-review-access` 跳过轻量复核）。
* **诊断**：`lwa doctor --json` 含 `currentLanIp` / `driftedInstanceIds`；深度探活用 `lwa doctor --access` 或 `lwa access review`（与 update report 同源）。
* **`lanIpStrategy=manual`**：不会自动改写落盘；请确认 `manualLanIp` 仍正确。

### 别名入口白屏（页面空白 / 资源空 200 或 404）

经路径别名访问 `http://<LAN-IP>:8080/<alias>/` 白屏，但端口直连 `http://<LAN-IP>:<hostPort>/` 正常：

* **根因 A — SPA 绝对路径（IMP-023）**：Vite/Vue/React 等构建产物若用默认 `base: '/'`，HTML 里是 `/assets/app.js`（绝对）。别名 `/<alias>/` 是子路径，绝对路径会绕过别名打到入口根，Caddy 对未匹配路由返回**空 200**（0 字节）→ JS 为空 → 白屏。
  * 自查：`curl -i http://127.0.0.1:8080/<alias>/`，看 HTML 里 `src=` 是 `/assets/...`（绝对＝有问题）还是 `./assets/...`（相对＝正常）；再 `curl -i http://127.0.0.1:8080/assets/<file>`，若返回 `200` 且 `Content-Length: 0` 即命中。
  * 修复：构建时设相对 base（Vite `base: './'`）后 `lwa rebuild <id>`；或 `lwa access review --rebuild-if-needed` 自动检出并重建命中实例。
* **根因 B — 浏览器缓存了旧 HTML**：产物已重建为相对路径，但浏览器仍用重建前的旧 HTML（绝对路径 + 旧 hash）→ 同样白屏。重启 lwa / 网关无效（服务端已正确，问题在客户端缓存）。
  * 自查：访问日志 `logs/static-access.log` 中出现 `GET /assets/<旧hash>.js`、`size=0` 且 referer 为别名页，即为缓存旧 HTML。
  * 修复：浏览器**硬刷新**（macOS `Cmd+Shift+R` / Windows `Ctrl+F5`），或无痕窗口 / 清该源缓存。
* **统一排查**：`lwa access review` 对每个别名实例做入口 + 绝对路径子资源空 200 对照，直接指出哪些实例需要 rebuild；`lwa gateway on` / `lwa gateway switch` 也会在交接后默认跑一次。

### 如何在 Caddy 与 builtin 之间切换网关后端

不要手改 YAML 再猜顺序，用原子命令（IMP-037）：

```bash
lwa gateway switch caddy                 # 升回 Caddy（需 PATH 中有合格版本）
lwa gateway switch builtin               # 降级 builtin（Caddy 坏掉时也可用）
lwa gateway switch builtin --dry-run     # 只看将影响的实例
```

- 切到 **builtin**：保留路径别名元数据（`routeHost`），但统一入口不可用；站点仍走各 hostPort。
- 切回 **caddy**：按 manifest 重建别名片段并 reload。
- 失败会回滚；若回滚也失败，结果带 `degraded` + `repairHint`，**不会**假报成功。
- `--json` / `POST /api/gateway/switch` 返回中：`ok=true` 表示切换事务本身成功，但 **`ok` ≠ `fullyOk`**；`accessOk=false` 表示后端已切成功、访问复核仍有风险（不假绿）。`fullyOk` 需切换与访问复核均通过。
- 管理页等价：`POST /api/gateway/switch`（body `{"backend":"caddy"|"builtin"}`）。

## 数据与清理

### `lwa remove` / 管理页删除后磁盘文件还在

**仅移除**（CLI 默认；管理页选「仅移除」/`purge=false`）只删 registry 索引并停服，**保留** `apps/<id>/` 全部文件（含 `data/`），便于误删恢复或重新导入。

**彻底删除**（CLI `--purge`；管理页选「彻底删除」/`purge=true`）会再删掉 `apps/<id>/`。若 `data/` 非空：

* CLI 需额外 `--force`；
* 管理页首次会收到 HTTP 409 / `data_nonempty`，须在对话框中再次勾选「强制删除非空 data/」后才会带 `force=true`（不会自动重试）。

两种路径都要二次确认项目 ID；取消任一步不会发请求。批量「删除冗余」仍只针对冗余实例，与单项目删除入口独立。

**删除后如何对账（IMP-041）**：

* `manager.log` / `lwa.log` 中按时间序 grep `remove stage=`，可见 `begin` → `stop` / `compose_down` / `alias_cleanup` → `registry_delete` → `done`；失败或跳过会标 `result=warn|skip|fail`。
* registry `events` 在实例行删除后仍保留 orphan 总览（`event_type=remove`）与阶段事件（`event_type=remove_stage`），message 含实例 ID。
* 管理页破坏性请求另有一行 `audit remove instance=… status=… code=…`（不含 token）。
* 若删除后路径别名入口仍 502：确认 `static-gateway/aliases/<id>.conf` 已删、主 Caddyfile 无悬空 import；新版本 remove 会自动清理，历史残留可手工删片段后 `lwa gateway on` / reload。

### 如何备份

* 关键数据：每个实例的 `apps/<id>/data/`（SQLite 等）。
* 元数据：`apps/<id>/local-web.json` 与 `registry/local-web.db`。
* 冷备份：`stop` 所有实例后直接打包整个工作区目录。
