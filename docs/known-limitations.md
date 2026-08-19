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
* **monorepo（IMP-057 Gate-1）**：支持 **npm workspaces** 格式的 monorepo 自动识别与主包选择。扫描根 package.json 的 workspaces 字段或 packages/*/package.json 目录结构，对每个子包按 6 值类型（electron_desktop / library / web_server / frontend_build / runtime_data / unknown）分类，并根据优先级规则自动选择主部署包。入口命令使用 -w <pkg-name> 语义（install 在根目录执行）。
  * **已知限制**：
    * 仅支持 **npm workspaces**；**pnpm workspaces**、**yarn workspaces**、**Nx**、**Turborepo** 等其他 monorepo 工具**不自动识别**，按普通项目处理（可能标记 pending）。
    * 多 web_server 子包时标记 pending（需用户手动选择）。
    * 无可部署子包（纯 electron_desktop / library / runtime_data）时标记 pending。
    * frontend_build 子包的构建产物路径（packages/<name>/dist）通过 buildOutputDir 传递给托管流程；若构建工具输出到非标准目录（如 .output），需用户手动修正。
    * 不自动识别 lerna.json / pnpm-workspace.yaml 等替代配置文件。

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
* **路径别名**：统一入口依赖 Caddy；`builtin` 下设置别名会被拦截（IMP-022）。容器实例支持别名（IMP-014），但须先 start。路径别名的前提是应用侧显式、可配置的 base path（方案 B）；LWA 做门禁与探测，不代改应用源码（IMP-055）。无源码时优先 hostPort 或未来主机名别名。
* **别名下 SPA 绝对资源路径（IMP-023 / IMP-055）**：别名入口 `handle_path` 去掉 `/<alias>/` 前缀转发，相对路径资源（`./assets/…`）正常；但 Vue/React 等 SPA 若构建时用绝对 `base: '/'`，资源（`/assets/…`）会绕过别名打到入口根 -> **空 200、404 或错误 MIME（如 JS 请求得到 text/html）**，页面白屏。

  **正解为方案 B（应用侧显式、可配置的 base path）**：
  - Vite 构建：`vite build --base=/<alias>/`（或等价可配置基址）
  - Vue Router：`createWebHistory(import.meta.env.BASE_URL)`
  - 前端 API 客户端：从 `BASE_URL` 派生请求路径（如 `/<alias>/api/v1`）
  - 后端 HTTP 路由在 Caddy `handle_path` 去前缀模型下通常保持 `/api`、`/assets`，不必改成相对路径
  - `base: './'` 可消除绝对资源路径但**不推荐作为最终方案**（Router/API 仍需跟 `BASE_URL`）

  **三类结果（IMP-055）**：

  | 类 | 含义 | 例 | LWA 动作 |
  | --- | --- | --- | --- |
  | **A** | 开箱可别名（资源已相对或已带正确 base） | prd-review 页面壳（`./js`…） | 允许设别名；仍可提示 API 若仍为绝对根路径 |
  | **B** | 现不可用，**显式 base path** 后可成功 | home-bookshelf（Vite `/assets` + `/api/v1`） | 设别名时**硬失败**并指向改造步骤；作者改完后可通过 |
  | **C** | 路径别名模型下无解（无源码/硬编码/要双入口全完整等） | 无法重建的绝对根 SPA | 硬失败；建议 hostPort 或未来主机名别名 |

  **设别名时**（`lwa alias set` / 管理页）：对 **shared-static 与 docker-compose** 实例均跑守卫；若能拉到入口 HTML 且检出绝对路径资源，会**硬失败**并提示 `--base=/<alias>/`、Router `BASE_URL`、API 派生、同步构建产物、hostPort 兜底等改造步骤（IMP-023 / IMP-055）；探不到入口时不拦截但提示「未验证入口 HTML」。

  **IMP-055 收敛 BUG-465 回退**：此前曾为 docker-compose 实例追加全局 `/assets`/`favicon` 回退路由作为临时方案，现**默认关闭**（多实例争抢 `/assets`、管不住 `/api` 与 Router）。旧回退片段在下次 `alias set`/rebuild 别名时自动消失。逃生舱可通过 config 显式开启，但不作为长期方案。

  `lwa access review` 除静态子资源外，还抽样绝对 `/api/...` 与「带别名前缀 API」对照（IMP-055）；绝对 API 根空 200 且前缀成功时降 overall，不再假绿。瞬时连接失败（TIMEOUT/REFUSED）不计 IMP-023。**深层路由抽样未覆盖**（055.12）：`access review` 不对 `/<alias>/` 下每条子路由逐一刷新验证 SPA fallback 行为，仅检测入口 HTML 与已知 API 模式；深层路由问题需人工浏览确认。

* **浏览量统计**：Caddy 模式下别名入口与无别名静态站点的直连端口均可计入（IMP-028 按 `request.host` 端口归属；探测请求 `__lwa_probe` 排除）；builtin 解析各实例 `gateway.log`；有别名的容器优先走 Caddy 日志（IMP-027），无别名容器仍为 docker logs 尽力解析（近似）。游标为路径无关稳定 key（工作区改名不致重复计入）。**V0.6.13** 起 Caddy `-size.log.gz` 多轮转/旧游标迁移/归档暂时不可读时的补读逻辑已加固（避免双计或永久漏计）。
* **工作区迁移（IMP-042）**：`lwa workspace relocate` **仅同卷**原子改名（macOS / Linux / WSL Linux 盘）；跨盘 / 跨机不自动，见 [工作区迁移手册](workspace-rename.md)。勿只做 `mv`。**V0.6.12** 起代码侧加固裸 mv 残留（gateway 启动前写主配置、SQLite mount 漂移 fail-safe、派生路径回写、doctor `workspace_path_consistency`），**V0.6.13** 起容器查询失败禁止绕过挂载 fail-safe、registry 不可读时一致性检查 SKIP，但仍不能替代正式 relocate 事务。
* **文件夹源导入（IMP-047）**：`lwa import --from-dir` 从本机文件夹**复制**进工作区（非就地运行）；关联目录是只读源，LWA 不会监听其变更，需手动执行 `--from-dir --update <id>` 或管理页「从源更新」同步。源目录被删除 / 移动后 update 会报错（不回退到 mount 模式）。`sourceKind=zip` 的实例不能用 `--from-dir --update`。请选项目根或 `dist/`，不要只选 `src/`。
* **GitHub 源导入的锁语义（IMP-065 / CHK-239）**：git 导入/更新在
  `import_activity` 全局锁内完成探测（ls-remote ≤30s）与克隆（≤180s）——设计取舍：
  并发导入按既有闸门排队、staging 互不干扰。窗口内其它导入（zip/文件夹）会等待；
  `lwa update` 重启 manager/daemon 前等待导入空闲（约 180s），若克隆恰在此时进行，
  超时后跳过重启并提示 doctor 复核（不会打断导入）。长克隆期间管理页「克隆中…」属预期。
* **GitHub 源导入（IMP-065）**：仅支持 `https://github.com`（精确 host、仅 443；MVP 不放开 GitHub Enterprise / Gitea / GitLab，常量预留）。一次性浅克隆（`--depth 1`）不做仓库缓存与增量 fetch；实例内不保留 `.git`；不递归 submodule；Git LFS 对象只保留指针文件（`GIT_LFS_SKIP_SMUDGE=1`）。克隆超 180s 或源码 >2 GiB 拒绝导入。凭据与代理零托管：私有仓靠 LWA 宿主机的 credential helper，代理靠 `https_proxy` / git `http.proxy`；从 LAN 浏览器导入私有仓失败属预期。git 源实例不能用 zip `--update` / `--from-dir --update` 更新；`--from-git --update` 传入的仓库必须与实例记录一致。git 导入/更新全程持全局导入锁（含约 30s 探测 + 最长 180s 克隆的网络等待），期间 `lwa update` 会等待导入空闲（最多 180s）。webhook / 定时拉取 / PR preview / GitHub App / `git push` receive-pack 一律不做。
* **选择文件夹（IMP-051 / V0.7.1）**：管理页原生目录对话框仅 **loopback** 可用；局域网即使持有 token 也不能远程弹窗，须手输宿主机绝对路径。
* **升级与导入互斥（V0.7.1）**：`lwa update` 重启 manager/daemon 前会等待导入空闲（约 180s）；超时跳过重启，避免打断进行中的导入。
* **多工作区（IMP-053）**：默认端口（`managerPort=17800` / `staticGatewayPort` / `portPool`）按**一机一工作区**设计。`lwa init` 在默认端口已有其它工作区管理页运行时，会输出黄色软提示建议复用既有工作区（不阻断）。确需第二工作区，须改全三段端口，否则会抢同一端口、实例列表分裂。
* **实证校验降级（IMP-058 Gate-C）**：`lwa start` 对首次部署的 docker-compose 实例执行实证校验状态机（VERIFYING → RUNNING / DEGRADED / FAILED）。必选存活探针超时 → FAILED（不假报 running）；可选探针失败 → DEGRADED。**证据驱动探针语义**（§6.5）：只有 `source="declared"` 或 `source="discovered"` 的探针可设为必选门槛；`source="guessed"` 的通用探针（如猜测的 `/health`、`/`）仅产生诊断，不通过不判失败。**V0.7.9**：discovered 探针仅来自源码 GET/HEAD 健康类路径（忽略 POST、注释与文档字符串）；探针 2xx/3xx 视为成功；无声明/发现探针时 `servesApi` 不得由 guessed 探针证明，整体 DEGRADED。**能力推断**：后端容器存活（HTTP 2xx）即视为 API / 数据库 / 迁移能力已就绪——大多数 Web 框架在启动时连接数据库并执行迁移，连接失败会 crash 而非返回 200。top-1 候选 build/start 失败时按 ``fallback_policy`` 策略降级：默认 ``confirm`` 需用户确认（非交互调用返回 ``FallbackConfirmationRequired``）；``auto-equivalent`` 仅在能力契约等价且回滚成功时自动降级（最多 3 个等价候选）；``disabled`` 不降级。**能力守恒**（§6.1.1）：后端候选不得降级到静态/前端候选。**回滚边界**（§6.5）：``rollback_succeeded`` 只表示基础设施已回滚；若 attempt 执行了数据库迁移或外部写入，``automatic_fallback_safe`` 保持 False，不自动降级。全部失败时输出 Layer 4 诊断报告（每个候选失败在哪一步、回滚结果、能力差异）写入 lastError + registry 事件。健康检查增强 API 路径探测（`/health`、`/api/`、`/api/v1/`、`/api/health`、`/healthz`），用于 Gate-C 实证校验的 API 可用性判定——通用猜测探针仅产生诊断，不单独判定部署失败。**V0.7.9 识别边界**：`frontend/` / `server/` 子目录按候选 kind 识别；Poetry 依赖纳入扫描；`sourceSubdir` 限制在 `current/` 内（绝对路径、`..`、符号链接逃逸拒绝）；预检 REJECTED 置 pending。**V0.7.10/11 状态边界**：Node 脚本声明的端口非法（越出 1..65535）置 pending 不再静默兜底（BUG-509）；健康检查只写 `last_error`、不覆盖 registry status，状态由进程观测判定（BUG-521/525，start 成功后旧 `lastError` 被清空）。

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

* 当前内置的 **19** 个 SKILL.md 覆盖常见场景，但**不保证**特定 AI 工具能正确消费；
  Skills 是提示工程资产，效果取决于模型与上下文窗口。
* Skills 不会自动执行带副作用的操作，所有变更需人工确认。
* Full Profile / 宿主机装配排障优先走 [`lwa-setup-host-environment`](../src/local_webpage_access/skills/lwa-setup-host-environment/SKILL.md) 与 FAQ。
* Skills 总览见 [`skills/README.md`](../src/local_webpage_access/skills/README.md)。

## 升级路径

* V1 不提供内置的版本迁移工具。跨版本升级前请备份工作区，
  并关注版本变更说明。
* `local-web.json` schema 有版本字段（`version: "1"`），
  未来破坏性变更会升版本号并提供迁移脚本。

## 一键更新通道（IMP-063 / V0.8.0）

* **仅 fast-forward**：`lwa update` 的源码阶段只做 `git merge --ff-only <固定候选 OID>`。tracked 文件有本地修改、与远端分叉、detached HEAD、浅克隆历史不足时**拒绝快进**（工作树零改动，结构化 `errorKind` + 下一步指引）；不做 merge/rebase/reset/stash 代操作，无 `--force` 类破坏性旗标。
* **`--ref` 仅接受远端分支**（MVP）：不接受 tag / 任意 commit；tag 化发布体系成熟后再议。
* **无 upstream 时须显式给全目标**：必须同时提供 `--remote <name>` 与 `--ref <branch>`，只给其一报 `target_incomplete`。
* **`--skip-pip` 门禁**：检测到落后（behind）又传 `--skip-pip` 会在快进前拒绝（`skip_pip_conflict`）；`--no-pull --skip-pip`、已是最新 + `--skip-pip`、fetch warning + `--skip-pip` 保留旧语义。
* **fetch 失败是降级不是故障**：断网/代理/凭据问题 → `sourceUpdate warning`，以本地代码完成 Runtime 刷新（离线可用）；代理与凭据沿用 git 自身机制（`https_proxy` / credential helper / SSH remote），lwa 不内置代理配置、不存 token。
* **非 git 克隆安装**（如 release zip 解包）：`sourceUpdate skipped` 并提示迁移到 clone + `pip install -e .`，不会自动 clone。
* **并发互斥**：可变更的 update 全程持 repo（git common-dir）+ workspace 双锁（固定顺序）；两个 update 并发时后者 fail-fast 报「更新锁被占用」。会 fetch 的 `--check` 也取 repo 锁；`--dry-run` 严格零写入（不联网、不 fetch、不取锁，数据标 `fresh=false`）。
* **不自动回滚**：快进后 pip/接力失败只在报告中给人工恢复链（`git status` 复查 → 干净时 `git reset --keep <oldHead>` → 重跑 `lwa update`）。
* **服务拉起边界（IMP-059）**：仅当 `run/*.json` 持久化 `enabled=true`（含 config 交叉校验）的自有服务会被 update 拉起；`--no-reconcile` 回到纯观察态。裸进程模式（无自启单元）重启后仍不会自动恢复——靠 `lwa doctor` 的 `restart_resilience` WARN（IMP-060）与 IMP-061 引导收敛。
