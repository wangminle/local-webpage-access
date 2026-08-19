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
lwa doctor          # 检查 Python / Docker / Compose / 端口池 / registry / 磁盘 / 内存 / workspace_path_consistency 等
lwa doctor <id>     # 对单个实例做深度诊断（日志、状态、文件）
lwa doctor --json   # 机器可读报告（含 platformSupport；未 init 亦可输出平台诊断）
```

`doctor` 有 fail 时退出码为 1，可在脚本/CI 中用作门禁。未初始化工作区时，人类可读模式仍需工作区；`--json` 会尽量输出 `platformSupport` 供平台排障。`workspace_path_consistency`（V0.6.12）会核对派生路径、Caddy 引用是否落在当前工作区、SQLite data mount；**V0.6.13** 起 registry 不可读、Docker 不可用或挂载观测失败时对应子项返回 SKIP（而非假绿 OK）。

`doctor` 是唯一豁免平台门禁的命令（其余命令在平台 unsupported 时会直接 exit 2），所以它的人类可读输出**总会**在末尾追加「平台支持」段——平台不达标时，这里是你唯一能看到原因的地方：

```text
── 平台支持 ──
  platform=wsl supported=False wslVersion=unknown distro=ubuntu 24.04
  - 无法确定 WSL 包版本（wslVersion=unknown）；写操作 fail-closed，请在 Windows 侧执行 wsl --version（需 ≥ 2.1.5）
  建议：无法在 WSL 内读取 Windows 侧 WSL 包版本（多为 interop 被禁用）：…
```

平台 `supported=False` 时 `doctor` 退出码为 1。

### Full Profile（IMP-033）

```bash
lwa doctor --profile full     # 输出 overall / 各上下文 Docker / Caddy / Gateway / 建议动作
lwa capabilities --json       # 与 doctor full 同源 CapabilityReport
lwa setup --full --resume     # 组权限刷新后继续；exit 2=session_refresh_required，exit 1=unready
```

`--profile full` 的人类可读摘要含 8 行能力：CLI Docker、Manager Docker、Daemon Docker、Caddy binary、Caddy runtime、Caddy owner、Caddy workspace、Gateway access。若平台同时 `supported=False`，「建议」会改为提示先解决平台问题，**不会**引导你直接跑 `lwa setup --full`（那会被平台门禁挡在 exit 2）。

`CapabilityReport.overall` 只有三个取值：

| overall | 含义 | 常见动作 |
| --- | --- | --- |
| `ready` | Full 强制能力均已证明 | 可无人值守跑容器与 Caddy |
| `degraded` | 能力可用但未完全闭环 | 按 `action` 修复后复验 |
| `unready` | 缺组件、权限不足、Caddy owner 不匹配、后台缓存未闭环，或 `sessionRefreshRequired=true` | 按 `action` 字段提示修复；**不得**当作安装成功 |

`sessionRefreshRequired` 是 CapabilityReport 里的**布尔字段**（不是 overall 取值）；Full 下为 `true` 时 overall 一定是 `unready`。它对应 `lwa setup --full` 的退出码 2：

| `lwa setup --full` 退出码 | 含义 | 动作 |
| --- | --- | --- |
| 0 | 装配完成且能力闭环 | — |
| 2 | `sessionRefreshRequired`：已加 docker 组但当前会话未继承 | 重登或 `newgrp docker` 后 `--resume`，并重启 manager/daemon |
| 1 | 其余 unready | 按输出提示修复后 `--resume` |

Full 下系统 `caddy.service` / 外部占用 `:2019` 会 fail-closed；须由 LWA gateway 以 `serviceUser` 托管。

### 后台 capability 缓存 `overall=unready`，但 `lwa doctor --profile full` 已是 ready？

**以 `lwa doctor --profile full`（CLI 实时探活）为准。** 后台 `run/capability-{daemon,manager,gateway}.json` 的 `overall` 是各进程自己的合并视角，历史上曾出现「假红」：

| 误判（勿再采用） | 实情 |
| --- | --- |
| 「`cliDockerAccess` 后台恒 unknown → 永远 unready」 | **不成立**。BUG-239 已让后台角色不把 `cliDockerAccess` 计入 Full `required`。 |
| 「后台完全不周期刷新」 | **不成立**。manager（BUG-379）与 gateway（约 300s）会周期刷新；**仅 daemon** 曾只在启动探一次（BUG-407，已修）。 |
| 真因 | **BUG-406**：gateway/daemon 写缓存曾 `include_backend_cached=False`，Full 仍要求 peer Docker/gateway 字段 → peer=`unknown` → overall 永久 unready（gateway 每 5 分钟只是重复假红）。 |

**V0.6.10** 起：gateway/daemon 与 manager 一样合并存活 peer 缓存（BUG-406）；daemon 启动后约 15s 再探一次，随后约每 300s 刷新（BUG-407）；角色快照 `overall` 按本角色职责计算（ADJ-035），CLI/manager 仍做全局聚合。若仍见假红，先确认已部署本版并 `systemctl --user restart lwa-daemon.service lwa-manager.service lwa-gateway.service`。

### `lwa-gateway` 卡死：Caddy 已通但 `gateway.json` 仍 `enabled=false`、无 capability 缓存？

**V0.6.10 初版回归（BUG-412 / ADJ-036）**：`caddy_start` 曾用 `Popen(stdout/stderr=PIPE)` + 无超时 `communicate()`；`caddy start` daemonize 后 master 继承 pipe 写端，gateway 进程永久阻塞在启动，探活循环与缓存写入都不跑——但 Caddy 本身可能已起来（`:8080` 仍 200）。

修复后（含本修复的构建）：`caddy_start` 改为 `DEVNULL` + `poll`/`wait(timeout=)`，不再读 PIPE。升级后执行：

```bash
systemctl --user restart lwa-gateway.service
# 数秒内应出现「gateway 前台监管就绪」；run/gateway.json 为 enabled=true
# run/capability-gateway.json 应写出；lwa doctor --profile full 的 Gateway 不再长期 unknown
```

若仍卡住：`systemctl --user stop lwa-gateway.service`，确认无残留 `caddy`/`gateway_service`，清理 `run/gateway-start.lock` 后再 start。

## 症状 → 日志文件 → 命令（IMP-034）

| 症状 | 先看哪个文件 | 命令 |
| --- | --- | --- |
| CLI 操作后无痕迹 / 关终端丢日志 | 工作区 `logs/lwa.log` | 任意 `lwa status` / `lwa start` 后再 `tail logs/lwa.log` |
| daemon 不导入 inbox | `logs/daemon.log`；`inbox/failed/`（连续失败死信） | `lwa daemon status`；修好 zip 后移回 `inbox/`；`lwa doctor` |
| 管理页异常 / 能力降级横幅 | `logs/manager.log`、`/api/health`（须本机或带 token） | `lwa manager status`；`lwa capabilities --json` |
| 构建无输出、`build.log` 长时间为空 | `apps/<id>/logs/build.log` + `logs/lwa.log`（阶段事件）+ registry events | `lwa logs <id> --category build`；`lwa doctor <id>` |
| 管理页显示 stopped 但容器仍在跑 | `observationError` / `runtimeAccess=permission_denied`；`capability probe` | `lwa doctor --profile full`；`newgrp docker` 后重启 manager/daemon |
| 容器 API 返回 409 `capability_denied` | 管理页或 curl 直调生命周期 | 先修 Docker 能力再操作；静态实例不受此阻断 |
| Caddy reload 权限失败 / owner 不匹配 | `logs/gateway.log`；系统 `caddy.service` | `lwa gateway status`；`lwa setup --full --resume` |

## 环境类问题

### `lwa init` 提示「管理页 :17800 已在运行」怎么办？（IMP-053）

`lwa init` 会探测默认端口 `127.0.0.1:17800`；若已有其它工作区的管理页在跑，会输出**黄色软提示**（不阻断），建议复用而非另建：

```bash
curl -s http://127.0.0.1:17800/api/health   # 看返回的 workspaceRoot
```

* 有 `workspaceRoot` → **cd 到该目录**再 `lwa import` / `lwa start` / `lwa update`，不要新建第二套。
* 默认端口（`managerPort=17800` / `staticGatewayPort` / `portPool`）按**一机一工作区**设计；两套工作区抢同一端口会让管理页只看到一半实例。
* 确需第二工作区：在新区 `local-web.yml` 改全三段端口（`managerPort` / `staticGatewayPort` / `portPool`）后再 `lwa init`。

### Python 版本不满足

```
[fail] python_version: Python 3.x.y 不满足最低要求 ≥ 3.13
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

### Python / Node 后端能否不用 Docker、本机直跑？

**不能（V1 架构边界）。** lwa 运行时只有：

* 静态 / 前端产物 → `shared-static`（Caddy 或 builtin）；
* 含后端的全栈 → **仅** `docker-compose`。

没有「venv + uvicorn/gunicorn + systemd」式的本地 Python 宿主路径。这与 PRODUCT 定位一致（小主机上 zip 导入后容器化隔离），不是遗漏的 bug。若环境禁止 Docker，只能托管纯静态/前端包，或在机外自行跑后端、不走 lwa 生命周期。详见 [已知限制 · 托管与容器](known-limitations.md#托管与容器)。

### 已有的 nginx + systemd 站点能否被 lwa 纳管？

**不能。** lwa 不提供 `adopt` / 登记外部进程：不会接管系统 nginx、用户自建 systemd 单元或非本工作区的 Caddy。`lwa autostart` 只监管本工作区的 manager/daemon/gateway。

要把旧站纳入：把应用打成 zip，用 `lwa import` 重新部署（静态走统一入口，全栈走 Compose）。继续用外部 nginx 时，请与 lwa 端口错开，且 **不要** 把 Caddy admin `:2019` 或系统 `caddy.service` 与 Full Profile 工作区混用。详见 [已知限制](known-limitations.md#托管与容器)。

### 磁盘空间不足

```
[fail] disk_space: 磁盘剩余 0.8 GB，低于阈值 1.0 GB
```

清理 `inbox/`（已导入的原始 zip）、`logs/`、或 `apps/<id>/source/`（原始快照）。
也可迁移整个工作区到更大磁盘。

## 更新与服务韧性

### `lwa update` 现在会自动 git pull 吗？（IMP-063 / V0.8.0）

会。默认执行 `fetch → 固定候选 OID → git merge --ff-only → pip install -e . → 新解释器接力 Runtime 后半段`。
只做快进：tracked 文件有本地修改、与远端分叉、detached HEAD、浅克隆历史不足都会**拒绝快进**
（工作树零改动）并给出下一步指引；`--skip-pip` 与「检测到落后」组合会在快进前拒绝
（`skip_pip_conflict`）。`--no-pull --skip-pip`、已是最新 + `--skip-pip`、断网 + `--skip-pip`
保持旧语义。

* `lwa update --check [--repo ...] [--json]`：只探测远端版本（会 fetch，但不改工作树与
  Runtime；不要求已 init 工作区）。退出码：0 探测完成、1 本地仓库/参数不合法、2 远端不可达。
* `lwa update --dry-run`：零写入预览（不联网、不 fetch、不取锁），数据标 `fresh=false`。
* 无 upstream 的仓库：必须同时给 `--remote <name> --ref <branch>`（MVP 不接受 tag/commit）。
* 代理与凭据：沿用 git 自身机制（`https_proxy` 环境变量、credential helper、SSH remote），
  lwa 不内置代理配置、不存 token。
* 断网/代理失效：`sourceUpdate warning`，仍以本地代码完成全部 Runtime 刷新（离线可用）。
* 非 git 克隆安装：`sourceUpdate skipped` 并提示迁移到 clone + `pip install -e .`。
* 快进后 pip/接力失败：报告附人工恢复链（`git status` 复查 → 干净时
  `git reset --keep <oldHead>` → 重跑 `lwa update`）；不自动回滚。

### `lwa update` 收尾 access/doctor 偶报瞬时失败？（V0.8.2 / issue #5）

重启后台服务后立刻刷新访问地址/doctor 存在竞态窗口。V0.8.2 起 update 在重启与自检之间
**等待服务就绪**（daemon/gateway 逐项探活，最多约 30s，报告 `waitReady` 步骤）；超时仅降级
warning 不阻断，提示稍后 `lwa doctor` 复核。若仍见瞬时 FAIL，手动重跑一次即可，持续 FAIL
才需要按 [运维手册](operations-playbook.md) 排障。

### 机器重启后 manager/网关没了、别名入口失效？（IMP-059/060/061）

三个互补机制：

1. `lwa update` 三态 reconcile（IMP-059）：`run/*.json` 标记 enabled 但未运行的自有服务会
   在 update 时自动拉起，报告标注「意外未运行（中断约 X），已恢复」；`--no-reconcile` 仅排障用。
   **V0.8.2 / issue #4**：中断时长 X 改按 live 证据估算（存活的 `run/caddy.pid`、systemd
   `InactiveEnterTimestamp`），detached 时代的陈旧 `run/gateway.json` 不再采信；无法确定时
   不再虚报时长。
2. `lwa doctor`（IMP-060）：`service_runtime_state` 对 enabled 未运行直接 **FAIL**（附
   `lwa manager on` 等恢复命令），对**已停用但进程残留**的服务 WARN（建议 `lwa X off` 清理）；
   `restart_resilience` 对任一 enabled 服务缺自启单元（逐项差集，含 gateway）/ 单元已装未启用 /
   无 linger / 容器 restart 策略不符给出 **WARN** 与实证修复命令。
3. 缺省安全（IMP-061）：`lwa autostart install` 在 caddy 环境默认纳入 gateway 单元、默认
   尝试 linger；`lwa init`/`setup` 收尾在 Linux systemd 环境 TTY 交互引导安装。一条命令修复：
   `lwa autostart install --with-caddy --linger`。

## 导入类问题

### 零依赖 stdlib Python 服务如何导入（issue#1）

纯标准库 `http.server` 服务（无任何第三方依赖）会被 scanner 弱信号识别为
`backend-container`（stack=`stdlib-http`，置信度 medium）：顶层 `server.py` /
`app.py` / `main.py` 任一文件的 **import 语句**（AST 解析，非字符串包含，BUG-534）
import 了 `http.server` 或 `socketserver` 即命中，启动命令为
`python <入口文件>`，且零依赖项目不落任何 pip 依赖层（BUG-540）。无需再
"伪造"框架依赖绕过。

**弱信号优先级最低**：若源码根存在 `index.html` 或其他 HTML 静态证据，会优先
识别为 `static`（BUG-541）--stdlib 信号只在无静态证据、无 Python 工程文件
（`requirements.txt` / `pyproject.toml` 等）时才作为兜底生效。带预览脚本的
静态站不会因此被降级为容器应用。

注意：应用应从 **`PORT` 环境变量**读监听端口（compose 已统一注入
`PORT=${INTERNAL_PORT}`，默认 8000），否则探针探测的端口与应用实际监听端口
不一致，首次启动会判定失败。

### zip 导入失败：路径穿越（zip slip）

```
ZipImportError: 检测到路径穿越（zip slip）：../../etc/passwd
```

导入器对所有 zip 成员做 `safe_extract` 检查，任何成员解析后落在解压目录之外都会被拒绝。
这是 [安全边界](security-boundary.md) 的强制保护。请用正规工具重新打包。

### 生成 Dockerfile / Compose 失败：critical 安全问题

```
RuntimeError: 生成的 Dockerfile 含 critical 安全问题（pipe_to_shell），已拒绝写出
RuntimeError: 生成的 compose.yaml 含 critical 安全问题（privileged），已拒绝写出
```

`generate_dockerfile` / `generate_compose` 在落盘前跑审计：Dockerfile 的 `ADD <url>`、
`curl|sh` / `wget|sh`，以及 Compose 的 privileged / Docker socket 等为 **critical**，
直接拒绝写出。常见原因：

* `local-web.json` 的 `entry.install` / `entry.build` 含管道装脚本（如 `curl … | sh`）。
* Skill 或手工改写的模板引入了远程 `ADD` / 危险 Compose 字段。

处理：去掉供应链风险指令，改用 `COPY` + 包管理器安装；详见 [安全边界](security-boundary.md)。

### 实例识别为 pending

```
status: pending
```

扫描器没能确定运行形态。常见原因：

* 项目根目录缺少 `package.json` / `requirements.txt` / `pyproject.toml` 等特征文件。
* zip 内有多层嵌套目录且特征文件不在拍平后的根。
* 项目结构特殊（自定义构建）。
* **V0.7.9 已覆盖的误判**：仅 `frontend/` 的 Vite 不再被当成纯静态；`server/` Express 不再 pending；仅 Poetry 声明的 FastAPI 会解析 `[tool.poetry.dependencies]`；预检 REJECTED（如 COPY 源缺失）会置 pending 而不静默放行。子目录含 psycopg2 等重型库仍会 pending。

处理：`lwa scan <id>` 重新识别；仍 pending 时检查 `local-web.json` 的 `lastError`，
或手工补特征文件后重扫。pending 实例会写入「未知 zip 来源」风险提示事件。

### slug 冲突与冗余实例

* **手动 `lwa import`**：同名 slug 已存在时会**报错**，提示使用 `lwa import <zip> --update <id>` 原地升级；不会静默覆盖，也不会自动建 `my-site-2`。
* **daemon 自动导入（IMP-011）**：slug 冲突时记 `import_conflict` 事件并提示 `--update`，**不再**自动追加 `-2/-3`；导入成功后 zip 会移入 `inbox/processed/`。
* **连续失败死信（BUG-297）**：同一 zip 指纹连续失败默认 5 次后移入 `inbox/failed/`；修好后移回 `inbox/` 根目录即可再试。
* **`--update` 后容器仍是旧版？**：容器实例必须 **rebuild 镜像** 才会跑新源码。V0.5.2 起，running 容器的 `--update` 默认走 `lwa rebuild`（不再轻量 `restart`）。若用了 `--no-restart`，请手动 `lwa rebuild <id>`。
* **更新后数据库指向空库？**：**V0.7.8** 起 `generate_env` 重新生成 `.env` 时会保留已有 `DATABASE_URL`，不再被源目录占位 SQLite 文件（如 `_empty_check.db`）覆盖。若需手动修改 `DATABASE_URL`，直接编辑 `docker/.env` 后 `lwa rebuild <id>`。
* **Gate-C 报 database 能力未通过？**：**V0.7.8** 起 `_verify_sqlite_database` 在 manifest 声明的文件未命中时，回退扫描 `data/` 目录下所有 `.db`/`.sqlite`/`.sqlite3` 文件，只要存在一个有效 SQLite 数据库即视为能力满足。
* **同包重复导入**：同一 zip 指纹（`sourceZipHash`）会产生冗余实例。清理：

  ```bash
  lwa remove --redundant          # 预览并清理（保留每组最早者）
  lwa remove --redundant --purge  # 连磁盘一起清
  ```

  或在管理页勾选「仅冗余」后「批量删除冗余」。任意项目的行内「删除」走 IMP-035 双阶段确认（可仅移除或彻底删除）。详见 [管理页](manager-page.md) 与 [运维手册](operations-playbook.md)。

### 文件夹源导入（IMP-047 / V0.7.0+；管理页选目录 IMP-051 / V0.7.1）

```bash
lwa import --from-dir /abs/path/to/my-site
lwa import --from-dir /abs/path/to/my-site --update <id>
```

* 路径必须是**绝对路径**；相对路径会被拒绝。
* 请选**项目根或 `dist/`**（含 `index.html` / `package.json`），不要只选 `src/`——否则易落入「待识别」且无法启动。
* LWA **复制**进 `apps/<id>/`，不会在关联目录就地运行。
* `--update` 时传入的目录须与实例关联路径一致，否则 Exit 2（不会静默改用别的目录）。
* 内容未变会跳过更新。详见 [运维手册 · 文件夹源](operations-playbook.md) 与 Skill `lwa-import-folder`。
* **管理页「选择文件夹」**（IMP-051）：仅用 `http://127.0.0.1:…` 打开管理页时可用；局域网访问须手输 LWA 机器上的绝对路径。
* 识别失败（pending）时管理页会报错并保持对话框打开，不会冒充「导入成功」。
* 纯中文显示名时，实例 ID 优先取文件夹名（避免多次撞成 `instance`）。
* 导入进行中不要立刻 `lwa update`：升级会先等待导入空闲（约 180s），超时则跳过重启 manager/daemon。

### GitHub 源导入（IMP-065）

```bash
lwa import --from-git https://github.com/<owner>/<repo>
lwa import --from-git https://github.com/<owner>/<repo> --ref dev --subdir frontend
lwa import --from-git https://github.com/<owner>/<repo> --update <id>
```

* **仅支持 `https://github.com`**（MVP；SSH / `git@` / gitlab / gitea / 带
  userinfo 或 `?`/`#` 的地址一律拒绝）。`/tree/<branch>/...` 网页地址请改用
  仓库根地址 + `--ref` + `--subdir`。
* LWA 在宿主机**一次性浅克隆**到工作区外临时目录后复制进工作区（与 zip 同
  管线）；实例内不保留 `.git`，不缓存仓库。
* **更新**＝ `git ls-remote` 探测远端：无新提交提示「无需更新」（零重建）；
  有新提交重新克隆并原地升级（保留 id / 端口 / data / 别名）。`--update` 传入
  的仓库必须与实例记录一致（换仓库先删除再导入）。
* **无凭据快速失败**：git 子进程带 `GIT_TERMINAL_PROMPT=0` 且关闭 stdin——私有仓 401 时不会在后台等终端输入直到超时，而是立即失败（credential helper
是非交互的，不受影响；克隆超时杀整个进程组，不残留 `git-remote-https`）。
* **私有仓 / 代理**：凭据（credential helper / `gh auth`）与 `https_proxy` 都
  配置在 **LWA 所在机器**上——从手机/其他电脑的浏览器导入私有仓时，用的是
  LWA 机器的凭据，不是浏览器那台机器的。LWA 不托管任何凭据。
* 仓库过大（>2 GiB 或克隆超 180s）会拒绝导入；Git LFS 对象只保留指针文件；
  submodule 不递归（需要时改用文件夹导入）。
* 管理页「从 GitHub 导入」对话框**不限本机**（LAN + token 可用）；git 实例
  卡片展示仓库地址 / 分支 / 短 SHA，「从源更新」按来源类型自动分流。
* 缺 git 可执行时：导入入口直接报错（`git_missing`），`lwa doctor` 出 WARN
  （不影响 zip / 文件夹导入）。详见 Skill `lwa-import-git`。

## 容器类问题

### 容器需要额外挂载宿主目录（extraVolumes，issue#1）

手工编辑 `docker/compose.yaml` 追加的挂载会在下次 `lwa start` 重生成时被
抹掉。持久化的出口是 `apps/<id>/local-web.json` 的 `container.extraVolumes`
（字符串数组，compose 短格式 `宿主路径:容器路径[:ro]`）：

```json
"container": {
  "extraVolumes": ["/home/user/.openclaw/workspace:/workspace:ro"],
  ...
}
```

改完后 `lwa restart <id>` 生效。安全边界仍在：家目录整体（`/home`、`/Users`）、
`/etc`、`docker.sock`、`.ssh`/`.aws` 等凭据目录挂载会被 compose 安全审计拒绝。

### "实例 xx 正在被其他操作占用，等待超时"（issue#1）

lifecycle 锁在同一实例的操作间互斥（build/探针/回滚全程持锁）。探针失败后的
回滚收尾、daemon 自愈重建都会短时间占锁。V0.7.11+ 的报错会带持有者 PID 与
心跳信息；若持有者存活且心跳新鲜，说明上一次操作尚未收尾，稍候重试即可，
不是死锁。另外 daemon 对**刚失败**的实例会先退避一个周期（默认 60s）再自动
重建，避免与手工 `lwa restart` 相撞。

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

### 实例状态为 VERIFYING / DEGRADED / FAILED（IMP-058 Gate-C）

**V0.7.7** 起 `lwa start` 对首次部署的容器实例执行实证校验状态机：

* **VERIFYING**：容器已 `compose up` 但存活探针尚未通过（等待 HTTP 响应）。正常情况下几秒内自动转为 RUNNING。
* **FAILED**：必选存活探针超时（容器进程已启动但端口不响应，或容器在超时窗口内退出）。不假报 running。查看 `lwa logs <id> --category run` 排查根因（常见：应用启动 crash、端口不对、启动脚本被 shell 操作符拆碎）。
* **DEGRADED**：必选探针通过但可选探针未通过（如猜测的 `/health` 端点返回 404）。实例可用但管理页显示降级横幅。

**关于猜测探针**：Flask/Django/Express 不保证提供 `/health` 端点。`source="guessed"` 的探针（如 `/health`、`/`）仅产生诊断，不通过不判失败。只有 `source="declared"` 或 `source="discovered"` 的探针可作成功门槛。**V0.7.9**：源码扫描只采纳可确认的 GET/HEAD 健康路径（`/health`、`/healthz`、`/ping`、`/ready` 等），忽略 POST 与注释文本；探针对 2xx/3xx（含 204）一律通过。无声明/发现探针时 `servesApi` 降为 DEGRADED（「API 无法实证」），不再因 guessed `/health` 假红或假绿。`frontend/` 子目录的 npm 构建在 `current/<subdir>` 执行；`sourceSubdir` 不得越出 `current/`。

**关于降级**：top-1 候选 build/start 失败时，按 `fallback_policy` 策略处理（默认 `confirm` 需用户确认；`auto-equivalent` 在能力等价时可自动降级；`disabled` 不降级）。后端候选不会自动降级为静态/前端候选（能力守恒）。

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

* 确认端口未被占用：`lwa doctor` 的 port_pool 检查**排除** lwa 自用端口（`managerPort`、`staticGatewayPort`、registry 已分配 hostPort），只报外部冲突；管理页端口请单独用 `ss`/`lsof` 或 `lwa manager status` 核对。
* **本机** `http://127.0.0.1:17800/` 免 token；**局域网**访问须带有效 Bearer token。
* **V0.7.0 / IMP-046**：token 默认每 168h 自动轮换，旧 token 立即失效。本机执行 `lwa manager token`（或 `--json`）取新 token；也可用 `managerTokenRotateHours` 调整周期。详见 [管理页 · Token 自动轮换](manager-page.md#token-自动轮换imp-046)。
* 确认 token 正确（复制时勿带前后空格）。
* 若绑定到 `0.0.0.0` 但无 token，启动会被 `validate_manager_binding` 拒绝。

### 管理页状态与 CLI 不一致

管理页每次 `GET /api/instances` 都会先观测回写状态，理论上始终一致。
若仍不一致，运行 `lwa status` 强制刷新，或 `lwa doctor <id>` 诊断该实例。

### `lwa manager off` 后提示「端口仍有健康响应」（BUG-456）

本工作区管理页已正常停止，但 `managerPort`（默认 17800）仍被**另一工作区**的管理页占用时，会输出黄色提示而非绿字「已停止」：

```
本工作区管理页未在运行，但端口 17800 仍有健康响应（可能是其他工作区的管理页）。
请到对应工作区执行 lwa manager off，或修改 local-web.yml 的 managerPort
```

这是正常保护——避免绿字「已停止」掩盖端口实际仍被占用。要到占用方工作区执行 `lwa manager off`，或改本工作区的 `managerPort`。

### 管理页操作返回 422 Unprocessable Content（BUG-457）

带请求体的 POST（如「从源更新」「从文件夹导入」）若缺少 `Content-Type: application/json`，浏览器会以 `text/plain` 提交，FastAPI 返回 422。**V0.7.2** 起前端 `apiFetch` 对带 body 的请求自动补 `Content-Type: application/json`，此问题已修复。

若仍遇到 422（多为手动 curl 或第三方调用）：

```bash
# 手动调用 API 时务必显式声明：
curl -X POST http://127.0.0.1:17800/api/instances/<id>/update-from-dir \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"restart": true, "keepData": true}'
```

### 浏览量数字对不上 / 「直连端口不算」？

管理页浏览量已不是「仅别名入口」：

* **只计 page**：静态资源、API、带 `__lwa_probe` 的探活不计（IMP-025）。
* **独立 IP**：详情含 `uniqueIpList`（含本机标记，IMP-026）。
* **有路径别名的容器**：走 Caddy `static-access.log`（IMP-027），不是 docker logs。
* **无别名静态 + Caddy**：经 Caddy 伺服的 **hostPort 直连**也会写入同一 access log，按端口归属（IMP-028）——**会计入**。
* **builtin** 或无别名容器：仍分别靠 `gateway.log` / docker logs（后者为近似）。
* **V0.6.11 归档补读回归（BUG-418）**：若升级后 hits 每次打开管理页都暴涨，是同一 `-size.log.gz` 归档被反复补读；代码已修。脏数据可删 `run/pageviews.db`（下次打开管理页按日志重计），或手工改聚合表后把对应 `ingest_cursor.last_ts` 置为已消费归档指纹。
* **多归档 / 冷启动漏计（BUG-419）**：多份 `-size.log.gz` 并存时须全部补读；`offset=0` 重置也会读齐未消费归档。清脏时四表一起看：`pageviews` / `pageview_detail` / `pageview_ip_stats`（弹窗 IP 次数）/ `pageview_ips`（按天去重，一般不用清）。删库重建前停 manager，并去掉 `pageviews.db-wal`/`-shm`，避免 WAL 盖回。
* **V0.6.13 轮转游标加固（BUG-431/432/433）**：多轮转按最旧可读归档切尾；旧版无 rotarch 元数据的游标升级时不会把历史 gzip 误当新轮转双计；解压失败的归档不会标成已消费（恢复可读后可补读）。

详见 [manager-page 浏览量](manager-page.md) 与 [known-limitations](known-limitations.md)。

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

* **根因 A — SPA 绝对路径（IMP-023 / IMP-055）**：Vite/Vue/React 等构建产物若用默认 `base: '/'`，HTML 里是 `/assets/app.js`（绝对）。别名 `/<alias>/` 是子路径，绝对路径会绕过别名打到入口根，常见结果是**空 200**、**404**，或被 SPA/回落页吃成 **200 + text/html** -> JS 无法执行 -> 白屏。同样地，前端 API 客户端若用绝对 `/api/v1`，也会打到入口根而非后端。
  * **设别名时拦截**：`lwa alias set` / 管理页设别名时，对 **shared-static 与 docker-compose** 实例均跑守卫；若检出绝对 `src`/`href`，会直接失败并提示改造步骤（探不到入口时不拦但提示「未验证入口 HTML」）。
  * 自查：`curl -i http://127.0.0.1:8080/<alias>/`，看 HTML 里 `src=` 是 `/assets/...`（绝对＝有问题）还是 `./assets/...`（相对＝正常）；再分别 `curl -i` 无前缀与带 `/<alias>` 前缀的资源 URL，对照状态码 / Content-Length / Content-Type。
  * **修复（方案 B - 显式、可配置的 base path）**：
    - Vite 构建：`vite build --base=/<alias>/`，同步重建静态产物后重新设置别名
    - Vue Router：`createWebHistory(import.meta.env.BASE_URL)`
    - 前端 API 客户端：从 `import.meta.env.BASE_URL` 派生请求路径（如 `/<alias>/api/v1`）
    - `base: './'` 可消除绝对资源路径但**不推荐作为最终方案**（Router/API 仍需跟 `BASE_URL`）
    - 若无源码或无法重建（C 类），请继续用 hostPort 端口直达
  * `lwa access review --rebuild-if-needed` 可自动检出静态资源错位并重建命中实例（但 API 路径错位需应用侧改造，rebuild 无法自动修复）。

* **根因 B — 浏览器缓存了旧 HTML**：产物已重建为相对路径，但浏览器仍用重建前的旧 HTML（绝对路径 + 旧 hash）→ 同样白屏。重启 lwa / 网关无效（服务端已正确，问题在客户端缓存）。
  * 自查：访问日志 `logs/static-access.log` 中出现 `GET /assets/<旧hash>.js`、`size=0` 且 referer 为别名页，即为缓存旧 HTML。
  * 修复：浏览器**硬刷新**（macOS `Cmd+Shift+R` / Windows `Ctrl+F5`），或无痕窗口 / 清该源缓存。
* **统一排查**：`lwa access review` 对每个别名实例做入口 + 绝对路径子资源对照（空 200 / 404 / 错误 MIME）+ 绝对 API 路径对照（IMP-055），直接指出哪些实例需要 rebuild 或 API 改造；`lwa gateway on` / `lwa gateway switch` 也会在交接后默认跑一次。瞬时连接失败（TIMEOUT / REFUSED）**不**算 IMP-023，避免误触发 `--rebuild-if-needed`。

### 如何在 Caddy 与 builtin 之间切换网关后端

不要手改 YAML 再猜顺序，用原子命令（IMP-037）：

```bash
lwa gateway switch caddy                 # 升回 Caddy（需 PATH 中有合格版本）
lwa gateway switch builtin               # 降级 builtin（Caddy 坏掉时也可用）
lwa gateway switch builtin --dry-run     # 只看将影响的实例
```

- 切到 **builtin**：保留路径别名元数据（`routeHost`），但统一入口不可用；站点仍走各 hostPort。若存在**运行中**但 manifest 损坏/无法加载的静态实例，切换会 fail-closed 拒绝执行（`GATEWAY_MANIFEST_UNLOADABLE`，BUG-516）——这些实例切过去无进程承接会静默下线，请先修复或删除。
- 切回 **caddy**：按 manifest 重建别名片段并 reload。若 Caddy master 启动失败，会把切换前停掉的 builtin 静态服务尽力原样拉回（`http.server`，BUG-517/523），不留下站点下线。
- 切换事务全程持有跨进程锁 `run/gateway-switch.lock`（BUG-514）：CLI 与管理页并发切换时，后到者在锁上等待约 15s 后快速失败并提示稍后重试（`GATEWAY_SWITCH_LOCKED`），不会交错写 YAML/manifest。
- 失败会回滚（含 manifest 与 registry `static_sites` 行，BUG-515）；若回滚也失败，结果带 `degraded` + `repairHint`，**不会**假报成功。
- `--json` / `POST /api/gateway/switch` 返回中：`ok=true` 表示切换事务本身成功，但 **`ok` ≠ `fullyOk`**；`accessOk=false` 表示后端已切成功、访问复核仍有风险（不假绿）。`fullyOk` 需切换与访问复核均通过。
- **主动停止的实例**（`desiredState=stopped`）在 access review 中标 `[SKIP]`，不会因回环 REFUSED 拖垮 switch/review 的 overall（BUG-301）。
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

### 如何做 LWA 工作区迁移（改名 / 搬目录）

准确说法是 **LWA 工作区迁移**：不要只做 `mv` 再 `lwa start`。自启单元、`pip editable`、Docker Mounts、manifest/registry 绝对路径、生成式 Caddyfile 都会绑旧路径。

- **优先 CLI**：`lwa workspace relocate <NEW> --dry-run` → 确认后执行；Skill `lwa-relocate-workspace` 只调 CLI。
- **中断恢复**：失败后 `lwa workspace relocate --resume`（读 journal；可显式传 NEW）；必要时 `--rollback`（同卷 rename 回旧路径并逆改写配置）。
- **dry-run**：只做预检与计划，不改磁盘（不建 pageviews.db、不以写模式开 registry）。
- **人工逃生舱 / 跨盘**：见 **[LWA 工作区迁移手册](workspace-rename.md)**（DOC-081）。
- **能力范围**：`lwa workspace relocate`（IMP-042）支持 **macOS / Linux / WSL 同卷**原子改名；跨盘/跨机不自动，请按 [工作区迁移手册](workspace-rename.md) 人工处理。
- 若迁前已 `docker compose down`：含 BUG-382 的版本用 `lwa start` 即可 `up -d`；旧版本可能需临时 `lwa rebuild`。
- **V0.6.12 起裸 mv 防复发**：`lwa doctor` 的 `workspace_path_consistency` 会报告陈旧派生路径、落在旧工作区的 Caddy 引用、SQLite data mount 漂移；`gateway on` 启动前按当前工作区落盘主配置；容器 start 遇挂载漂移会 fail-safe 救援，`down` 失败或两侧数据冲突时中止并要求人工确认。**V0.6.13** 起：容器状态查询失败禁止当作「无容器」继续 start；registry 不可读时一致性检查 SKIP。修复入口：`relocate --verify` / `rebuild` / `recover` / `gateway on`。
