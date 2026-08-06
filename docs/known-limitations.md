# V1 已知限制（WBS-30.12）

本文明确 `lwa` V1 **不支持**或**有条件支持**的范围，帮助用户正确设定期望。
不在列表中的能力即为 V1 支持范围。

## 平台与运行环境

* **正式支持操作系统（IMP-036）**：
  * Linux 裸机：Ubuntu **LTS**（当前：22.04 jammy / 24.04 noble / 26.04 resolute）、Debian **Stable**（12 bookworm / 13 trixie）；kernel ≥5.15、glibc ≥2.35、systemd。版本与代号须配对；非 LTS、sid/testing、未纳入矩阵的未来版本一律拒绝。
  * WSL2 Linux（同上发行版；WSL 包 ≥2.1.5 且 systemd 为 PID 1）；Windows **仅作 WSL2 宿主**。WSL 下 Full Profile / `lwa autostart` 写路径禁止工作区位于 `/mnt/<drive>`（写入前 fail-closed）；只读诊断与普通 CLI 不因路径形似 `/mnt` 而全局阻断。运行前宿主准备见下文「WSL2 宿主准备」。
  * macOS：**14 Sonoma+**（滚动下限，对齐 Docker Desktop「当前及前两版」；截至 2026-07）
  * 架构仅 **x86_64/amd64**、**arm64/aarch64**
* **明确不支持**：**Windows 原生**进程（CLI/服务入口 hard fail，请改用 WSL2）；WSL1；Ubuntu 非 LTS；Debian sid/testing；Alpine/musl、Fedora/RHEL/Arch 等未纳入矩阵的发行版；32 位 / ARMv7 等。
* **平台诊断**：`lwa doctor --json` 在**未初始化工作区**时仍输出 `platformSupport`（reasons / action / supported），便于排障；已初始化时人类可读模式也会在末尾打印「平台支持」段（`doctor` 是唯一豁免平台门禁的命令）。
* **WSL 包版本探测**：优先 PATH 中的 `wsl.exe`，并回退 `/mnt/<drive>/Windows/System32/wsl.exe`（覆盖 `appendWindowsPath=false`）；已处理 UTF-16LE 输出与中文本地化。若 `[interop] enabled=false` 导致 Windows 二进制完全不可执行，则仍会 `wslVersion=unknown` 并 fail-closed——此时应在 Windows 侧确认版本。
* **Python**：要求 3.13+，不支持更早版本。
* **Docker**：要求 Docker + Docker Compose 插件（`docker compose` 子命令）。
  Compose v1 独立二进制不支持；低于推荐版本时仅告警，不阻断已满足最低线的环境。
  `lwa setup --full` / 内置安装脚本覆盖 **macOS / Ubuntu / Debian（含 WSL2）**。`--full` **需要已初始化工作区**（`lwa init --full` 或先 `init` 再 `setup --full`）；default 的 `lwa setup` 可无工作区。
  Full Profile（`profile: full`）还会验收 manager/daemon/gateway 真实 Docker 权限与 Caddy owner；未闭环不假绿（见 FAQ「Full Profile」）。
  Linux 上 LWA 以 `serviceUser` 访问 `docker.sock`（须在 `docker` 组）；`usermod -aG docker` 后须重登并重启 manager/daemon，或对 system unit 使用 `SupplementaryGroups=docker`（`lwa autostart` 默认 user unit 仍依赖会话组）。
  观测失败写 `unknown` / `runtimeAccess`，不把运行中容器误标 stopped；管理页与 API 在能力降级时阻断容器操作。
* **Caddy 所有权（Full）**：禁止静默复用系统 `caddy.service`；`:2019` 必须属于本工作区 LWA Caddy。Default 档在无 Caddy 时可降级 builtin；Full 不得假绿。
* **架构**：基线镜像 `node:24-alpine` / `python:3.13-slim` 以 x86_64 / arm64 为主；
  其他架构需用户自备镜像或调整模板。

## WSL2 宿主准备（运行前）

下列项**不在** `lwa setup` 自动改写范围内，但会直接影响 Full Profile 稳定性、Docker 构建与 LAN 访问。建议在首次 `lwa init --full` / `lwa autostart install` **之前**完成。

### 工作区放在 Linux 文件系统（正确性 + 性能）

* Full / `autostart` **禁止**工作区位于 `/mnt/<drive>/…`（写入前 fail-closed）。
* 即使不做 Full，也不要把仓库、venv、`node_modules`、Docker 构建上下文、SQLite、`logs/` 放在 `/mnt/c` 等 Windows 盘：经 **9p** 跨文件系统访问时 I/O 明显变慢，构建与热路径容易卡顿。
* 推荐路径形如 `/home/<user>/…` 或其它 Linux ext4 卷。微软亦建议：由 Linux 工具操作的项目放在 WSL 的 Linux 文件系统中。

### 宿主资源配额（`.wslconfig`）

应用容器另有 `resourceProfile` / `defaultResourceLimits`（见 [runtime-workspace](runtime-workspace.md#资源档位imp-018)）；那是**容器级**限额。WSL2 虚拟机本身的内存/CPU/Swap 由 Windows 侧 `%UserProfile%\.wslconfig` 控制。小主机上若 Linux 只看到约 2～3GB，Docker + Python manager/daemon + Caddy 会同时吃紧。

示例（按宿主机实际内存调整；改完执行 `wsl --shutdown` 再生效）：

```ini
# %UserProfile%\.wslconfig
[wsl2]
memory=8GB
processors=4
swap=4GB
```

经验值：

| Windows 物理内存 | 建议给 WSL `memory` | `swap` | `processors` |
| --- | --- | --- | --- |
| 16GB | 6～8GB | 2～4GB | 宿主逻辑核的一半左右 |
| 32GB | 8～12GB | 2～4GB | 同上 |

不要把 `swap=0` 当作默认（内存尖峰时易 OOM）。官方项说明见 [WSL 设置配置](https://learn.microsoft.com/windows/wsl/wsl-config)。

> 容器内的 `mem_limit` / `cpus` **不能**替代上述 VM 配额；两者需同时合理。

### LAN 访问与防火墙

* LWA 面向**局域网 HTTP**：统一入口默认 `:8080`（`staticGatewayPort`），管理页默认 `:17800`（`managerPort`），实例另占端口池（常见 `18000–19999`）。
* **端口最小化**：防火墙只放行**实际在用**的业务口（例如当前实例 `18000`），不要把 `portPool` 整段 `18000–19999` 默认全开；管理页 `17800` 仅在确需 LAN 管理时开放，并依赖 manager token。
* **不要**把 Caddy **admin `:2019`** 暴露到 LAN 或公网——仅供本机 `127.0.0.1` 管理 reload。
* **Windows / WSL2 Mirrored（Windows 11 22H2+、WSL ≥2.0.9）**：入站由 **Hyper-V firewall** 管理，不是普通「Windows 防火墙」规则 alone。应在 **PowerShell（管理员）** 按 WSL `VMCreatorId` 逐端口放行，例如：

  ```powershell
  # 常用 WSL VMCreatorId；若环境不同以 Get-NetFirewallHyperVVMCreator 为准
  $wslId = "{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}"

  New-NetFirewallHyperVRule `
    -Name "WSL-In-TCP-8080" `
    -DisplayName "WSL-In-TCP-8080" `
    -Direction Inbound `
    -VMCreatorId $wslId `
    -Protocol TCP `
    -LocalPorts 8080 `
    -Action Allow
  ```

  优先**逐端口 Allow**；不要默认把整台 WSL 的 `DefaultInboundAction` 改为 Allow。可用 `Get-NetFirewallHyperVRule` 核对是否已有 `WSL-In-TCP-*`。发行版内若启用了 `ufw`/`firewalld`，以及企业 GPO/EDR，仍可能叠加拦截——但不要把普通 Defender 入站规则写成 Mirrored 的唯一控制层。
* **验收分层**（须逐层记录，不可跳级宣称「LAN 已通」）：
  1. WSL 内 `127.0.0.1:<port>` —— 证明服务自身；
  2. Windows `localhost:<port>` —— 证明宿主/WSL localhost 互通；
  3. Windows **自身 NIC IP** —— 只记录现场结果，**不作为最终 LAN 结论**（本机自测假阴性暂缺充分官方证据，只能写作观察）；
  4. **另一台物理机/手机**访问 `http://<共享 LAN IP>:<port>` —— 最终 LAN 验收。
* 换网 / DHCP 后 LAN IP 可能变：执行 `lwa access refresh` 与 `lwa access review`（见 [运维手册](operations-playbook.md#72-访问地址刷新g1--imp-038--imp-040)）。可选 mirrored 网络模式见 [开机自启 · WSL 网络](autostart.md#wsl-网络可选-mirrored)。
* **代理**：WSL `autoProxy=true` 便于发行版访问互联网依赖源；LWA **内部**健康/access/Caddy admin 探针走直连，不依赖用户 `unset http_proxy`。人工 `curl` 仍可能受代理影响。

### PATH / interop（可选精简）

* `lwa autostart` 生成的 systemd/launchd 单元已**固化 PATH**（含 caddy 常见路径，修 BUG-139），交互终端不必长期手动塞 `/mnt/c/Windows/System32`。
* 若几乎不依赖 Windows CLI，可在 `/etc/wsl.conf` 设 `[interop] appendWindowsPath=false` 减少 PATH 污染；版本探测仍会回退绝对路径找 `wsl.exe`（见上文「WSL 包版本探测」）。`[interop] enabled=false` 会导致包版本探测失败，Full 写路径 fail-closed——一般不要关。

## 项目识别

* **支持识别**：纯静态 HTML、纯前端 SPA（Vite/React/Vue/Svelte 等基于 `package.json` 的项目）、
  Node 后端（Express/Fastify 等）、Python 后端（FastAPI/Flask/Django 等）、含 SQLite 的全栈项目。
* **不自动识别**：Go / Rust / Java / .NET / PHP / Ruby 等其他生态（导入后标记 `pending`，
  需用户手动配置或扩展扫描器）。
* **数据库**：仅自动识别 SQLite（文件型）。MySQL / PostgreSQL / Redis 等网络数据库
  **不自动起容器**，需用户在项目内自行编排。
* **monorepo**：多项目工作区不自动拆分，按 zip 根目录整体识别一个实例。

## 托管与容器

* **运行时只有两条路径（V1 架构边界）**：
  * **纯静态 / 前端构建产物** → `shared-static`（Caddy 或 builtin `http.server`）；
  * **含 Python / Node 等后端的全栈** → **仅** `docker-compose`（`host_container`）。**没有**「本机 venv / systemd 直接跑 uvicorn/gunicorn」的本地 Python 运行时；坚持不装 Docker 则无法用 lwa 托管此类后端（见 FAQ「Python 后端是否支持本地直跑」）。
* **不纳管外部既有站点**：已在机上的 nginx+systemd、独立进程、非本工作区 Caddy 等，**不能** `adopt` / 登记进 lwa 生命周期。`lwa autostart` 只监管 **本工作区** 的 manager/daemon/gateway；要把旧站纳入 lwa，须改为 zip 导入并由 lwa 重新部署（静态走网关，全栈走 Compose）。与 Full Profile「禁止静默复用系统 caddy.service / 外部 `:2019`」一致。
* **静态网关**：默认 Caddy 优先；Default 档无 Caddy 时可降级内置 `http.server`。
  Full Profile 要求可用的 LWA 托管 Caddy（见上）。`staticGateway=nginx` 枚举保留但未实现（无 nginx 模板），会降级 builtin（Full/严格模式则拒绝降级）。
* **HTTPS**：V1 仅 HTTP。HTTPS / 证书自动化（Let's Encrypt）不在范围内。
* **自定义域名**：不支持。通过 `IP:端口` 访问。
* **WebSocket**：静态网关路径不做专门代理；容器路径依赖 Docker 端口映射，原则上可用但未专项测试。
* **数据持久化**：仅自动 bind mount `data/` 目录。其他路径（如日志、上传目录）需用户在项目内处理。
* **环境变量**：生成的 `.env` 仅含端口与资源限额等基础设施变量；应用所需业务密钥请写入 `docker/.env.local`（IMP-015，compose 可选注入，缺失不报错），不要改写由 lwa 生成的 `.env`。
* **路径别名**：统一入口依赖 Caddy；`builtin` 下设置别名会被拦截（IMP-022）。容器实例支持别名（IMP-014），但须先 start。
* **别名下 SPA 绝对资源路径（IMP-023）**：别名入口 `handle_path` 去掉 `/<alias>/` 前缀转发，相对路径资源（`./assets/…`）正常；但 Vue/React 等 SPA 若构建时用绝对 `base: '/'`，资源（`/assets/…`）会绕过别名打到入口根 → **空 200、404 或错误 MIME（如 JS 请求得到 text/html）**，页面白屏。受影响项目应构建时设相对 base（Vite `base: './'`）或 `--base=/<alias>/`，或继续用 hostPort 直达。`lwa access review` 会对照「无前缀 vs 带前缀」子资源并告警（入口 HTML 200 ≠ 别名下可渲染）；瞬时连接失败（TIMEOUT/REFUSED）不计 IMP-023。
* **浏览量统计**：Caddy 模式下别名入口与无别名静态站点的直连端口均可计入（IMP-028 按 `request.host` 端口归属；探测请求 `__lwa_probe` 排除）；builtin 解析各实例 `gateway.log`；有别名的容器优先走 Caddy 日志（IMP-027），无别名容器仍为 docker logs 尽力解析（近似）。游标为路径无关稳定 key（工作区改名不致重复计入）。**V0.6.13** 起 Caddy `-size.log.gz` 多轮转/旧游标迁移/归档暂时不可读时的补读逻辑已加固（避免双计或永久漏计）。
* **工作区迁移（IMP-042）**：`lwa workspace relocate` **仅同卷**原子改名（macOS / Linux / WSL Linux 盘）；跨盘 / 跨机不自动，见 [工作区迁移手册](workspace-rename.md)。勿只做 `mv`。**V0.6.12** 起代码侧加固裸 mv 残留（gateway 启动前写主配置、SQLite mount 漂移 fail-safe、派生路径回写、doctor `workspace_path_consistency`），**V0.6.13** 起容器查询失败禁止绕过挂载 fail-safe、registry 不可读时一致性检查 SKIP，但仍不能替代正式 relocate 事务。
* **文件夹源导入（IMP-047）**：`lwa import --from-dir` 从本机文件夹**复制**进工作区（非就地运行）；关联目录是只读源，LWA 不会监听其变更，需手动执行 `--from-dir --update <id>` 或管理页「从源更新」同步。源目录被删除 / 移动后 update 会报错（不回退到 mount 模式）。`sourceKind=zip` 的实例不能用 `--from-dir --update`。请选项目根或 `dist/`，不要只选 `src/`。
* **选择文件夹（IMP-051 / V0.7.1）**：管理页原生目录对话框仅 **loopback** 可用；局域网即使持有 token 也不能远程弹窗，须手输宿主机绝对路径。
* **升级与导入互斥（V0.7.1）**：`lwa update` 重启 manager/daemon 前会等待导入空闲（约 180s）；超时跳过重启，避免打断进行中的导入。

## 管理页与 API

* **鉴权**：单一 API token；**每 168h（可配 `managerTokenRotateHours`）自动轮换**（IMP-046），旧 token 立即失效；本机 loopback 免 token，LAN 须带有效 token。查询/取新 token：`lwa manager token`（含 `--json`）。不做多 token 宽限期、无角色分级。多用户场景不适用。
* **实例显示名（IMP-043）**：导入优先显式 `--name`，其次主页 `<title>`（含托管入口 `dist/`/`build/`/`out/` 等产物目录的 `index.html`；HTML 实体按浏览器语义解码），否则 slug 美化；管理页名称列随内容自然分配宽度（V0.6.9 `table-layout: auto`，名称最多两行省略）。旧 slug 美化名会在拉列表时一次性回填（不会覆盖用户手工名；**V0.6.13** 起回填持实例锁并重读 manifest，避免覆盖并发 start/stop 状态）。
* **`?token=` 查询参数**：有意保留以便新标签带入鉴权；会进入浏览器历史 / Referer / 反代 access log。日常 API 请优先用 Header。详见 [管理页](manager-page.md#鉴权)。
* **并发写入**：registry 用 SQLite WAL + 连接级锁，适合单机管理页并发；
  不适合多进程/多机水平扩展。
* **实时推送**：无 WebSocket / SSE，前端通过轮询刷新状态。
* **国际化**：前端仅中文。

## 安全

* **多租户隔离**：不支持。所有实例共享同一 Docker daemon 与主机内核。
* **网络隔离**：默认 bridge 网络，实例间默认可通信（V1 未启用自定义网络隔离）。
* **镜像签名**：基线镜像来自 Docker Hub，不做签名/校验和验证。
* **审计日志留存**：安全发现写入事件表与日志，无独立审计后台或告警通道。
* **资源限额强度**：Docker 的 memory/cpu 限制为软约束（cgroup），不防恶意 fork bomb 级别的滥用。

## 数据与备份

* **自动备份**：不提供。备份需用户自行打包工作区（见 [FAQ](faq.md#如何备份)）。
* **registry 迁移**：SQLite 单文件，可整体复制；但跨架构/跨 Docker 版本时不保证容器配置兼容。
* **历史版本**：一个实例一份 `current/`，不支持多版本切换或回滚到旧构建产物。

## CLI 与自动化

* **批量操作**：无通用批量 start/stop；需借助 shell 循环或管理页 API。**例外**：`lwa remove --redundant` 与管理页「批量删除冗余」可按 zip 指纹批量清理冗余实例（IMP-012 / IMP-019）。
* **滚动更新**：不支持蓝绿/滚动发布，`rebuild` 是停机重建。
* **CI/CD 集成**：无原生 webhook 触发器；可通过 inbox/ + daemon 或 API 自行实现。

## 大模型 Skills

* 当前内置的 **18** 个 SKILL.md 覆盖常见场景，但**不保证**特定 AI 工具能正确消费；
  Skills 是提示工程资产，效果取决于模型与上下文窗口。
* Skills 不会自动执行带副作用的操作，所有变更需人工确认。
* Full Profile / 宿主机装配排障优先走 [`lwa-setup-host-environment`](../src/local_webpage_access/skills/lwa-setup-host-environment/SKILL.md) 与 FAQ。
* Skills 总览见 [`skills/README.md`](../src/local_webpage_access/skills/README.md)。

## 升级路径

* V1 不提供内置的版本迁移工具。跨版本升级前请备份工作区，
  并关注版本变更说明。
* `local-web.json` schema 有版本字段（`version: "1"`），
  未来破坏性变更会升版本号并提供迁移脚本。
