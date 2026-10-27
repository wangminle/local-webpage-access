# WSL 跨 Windows 版本网络适配：需求与验收草案

## 当前完成度核对（2026-10-27）

PLN-054 继续进行中，整案不归档。产品主干和 BUG-980/981、989/990/992 等修复已有实现与分项证据；本 Win11 mirrored 批次已有原生 Engine 和十一项目安装成果，不再以“缺 Docker”作为阻点。

剩余：Win10 修复后同路径独立 LAN 复验；Win11 mirrored 独立设备 HTTP/HTTPS、资源/管理鉴权和明文旁路正向；N10-2/N11-2/N11-3/M11-2/X10-M/X-UNK 等其余目标组合及权限不足/interop 关闭/未知模式等真实退化；休眠、WSL 关闭重开、guest 地址变化、Windows 重启与自启恢复。详见 §4/§5 的原矩阵。

BUG-997 已修复并在 Mac/Ubuntu 补齐六资源与业务证据，但本 Win11/WSL 组合修复后的原六资源及持久化复验尚缺，不能沿用别的宿主结论。原 Win11 首次安装[执行提纲已归档](../2-achievement/2026-10-23-Win11-mirrored-Debian十一项目安装验收-执行提纲.md)，保留[成果报告](../2-achievement/2026-10-23-Win11-mirrored-Debian十一项目安装验收.md)的历史部分通过事实。


创建日期：2026-10-19。验证结果汇总更新至：**2026-10-23**。

第一、二批核心适配与策略模拟已落地，但逐条核对仍有产品集成缺口（见 §3.5），不能将全文功能标为全部开发完成；第三批已有两台 Debian 实机的部分证据：**N10-3（Win10 + WSL 包 3.0.3.0 + NAT）**与 **M11-3（Win11 + WSL 包 3.0.4.0 + mirrored）**。两台均为 **WSL2 架构**，软件包 3.x 不表示 WSL3 架构。Win10 已补 portproxy 解码实测、真实 Docker 与样本部署；Win11 已补本机 HTTP/HTTPS、鉴权及独立物理 LAN 客户端负向实测。

**2026-10-23更新：N10-3 独立 Mac 的 HTTP/HTTPS 页面、资源与代表容器数据读取通过；BUG-980/981及配套BUG-982/983已修复，修复后同路径独立LAN复验仍待。M11-3 当晚补最新快照本机HTTP/HTTPS与鉴权、多网卡实测；独立LAN仍超时，另发现BUG-989生效防火墙策略采集缺口。** 历史分层结果见 §4.1～§4.4，最新实测见 §4.5～§4.6；不宣称全部组合支持。LWA没有自动修改Windows网络；N10临时规则由管理员手动添加与撤销，撤销及应用清理均已核对。

## 1. 目标与版本口径

用户明确：首次 WSL 正式部署发现的问题，必须放进 Windows 10/11 与 WSL 软件包 2.x/3.x 的组合中解决，不能仅为当前机器补一个地址或脚本。

用户随后明确网络边界：适应现有网络，不为 LWA 安装或托管额外组网/转发/注入工具，也不自动修改 Windows 网络。当前实现范围为只读发现、地址与证据语义、诊断、现有入口适配及用户可审阅的配置说明。

分别记录以下维度，不用单个“WSL 版本”混称：

| 维度 | 含义 | 判定要求 |
| --- | --- | --- |
| WSL 架构 | WSL1 / WSL2 | 本项目现有正式范围仍为 WSL2，不因软件包 3.x 而称作 WSL3 架构 |
| WSL 软件包 | 例如 2.x、3.x 的完整版本 | 保留当前最低包版本 2.1.5，使用现有 wslPackageVersion，不与 Linux 内核版本混淆 |
| Windows 主机 | 产品、发行版本、Build、修订号 | 不用 major=10 或字符串“10.0”区分 Win10/Win11；两者都可能返回 10.0 |
| Linux 发行版 | Ubuntu/Debian 等、版本、架构 | 保留现有正式发行版、systemd 和路径门禁 |
| 当前网络模式 | NAT、mirrored、其他运行态模式或 unknown | 优先实际探测，配置意图不替代运行状态；网络模式名称变动/回退必须可降级 |
| 操作能力 | interop、读取网卡/规则、权限、localhost 转发、外部探测 | 每项独立记录支持/不可用/未知及原因、来源与采集时间 |

WSL 软件包版本升级不会使 Win10 自动拥有 Win11 的 mirrored 能力；也不能假定所有 Win11 或所有 3.x 环境都启用了相同网络特性。

## 2. 方案选择

| 方案 | 优点 | 代价 |
| --- | --- | --- |
| 每个 Windows/WSL 版本组合配置策略表 | 规则明确，便于已知环境复现 | 补丁、配置差异和模式回退需要持续补表；仍需运行态验证 |
| 运行态能力探测，共用策略与版本验收矩阵（建议） | 可处理同版本不同配置以及权限/命令缺失；已知版本保留可追踪验收证据 | 要把检测结果、地址含义和证据分开建模，测试需覆盖降级路径 |

采用第二种思路：版本给出功能候选和已知限制，实际模式及可读能力决定当前策略；版本不能替代探测，也不能把未知版本静默当作完整验收通过。

工具背景与边界修订另见 [网络辅助工具与内置边界讨论](../1-discussions/2026-10-19-Win10下WSL网络补足工具与LWA内置边界.md)：不内置第三方网络工具，也不开发自动维护 Windows 转发规则的常驻组件；工具资料仅作背景，当前以适应用户已有网络为目标。

## 3. 统一的适配行为

### 3.1 采集环境与能力

- 分别识别 WSL 架构、包版本、Windows Build、Linux 发行版及 Docker 后端；保留现有 JSON 字段的兼容性，补充明确缺失维度，不重复创建已有字段。
- 网络模式优先使用 `wslinfo --networking-mode` 的实际结果，兼容命令位置/不可用/异常/超时。`.wslconfig` 仅是配置意图；模式回退或新名称应保持原始值与明确解释，不强归 NAT 或 mirrored。
- Windows 侧通过只读 interop 获取结构化信息，复用已有解码/路径回退经验；各能力可独立失败。例如网卡和 portproxy 可读而防火墙规则不可读时，仅后者标 unknown。
- 一次请求共享一次环境快照，设置有限超时与缓存；普通状态轮询不逐实例调用 Windows 进程，不触发 UAC，不修改网络配置。

### 3.2 按能力提供网络策略

| 环境/实际模式 | LWA 的行为 |
| --- | --- |
| Win10 + NAT | 将 guest 地址标为内部地址；发现 Windows 入口候选、实际监听与映射；缺少 LAN 转发时说明缺口并给出有条件的操作指引 |
| Win11 + NAT | 使用同一 NAT 能力策略；不因 Win11 身份就假设 mirrored 或 LAN 可达 |
| Win11 + mirrored | 根据当前接口、监听与 Hyper-V/Windows 防火墙可读证据判断；不要求 portproxy，也不因 mirrored 就判为外部已可达 |
| 其他/回退模式 | 保留模式值；使用已证明可用的能力，给出该模式的限制；未实现的网络暴露语义保持 unknown |
| interop 关闭或读取被拒 | 保留可工作的 Linux 本地能力；Windows 入口/规则无法确认时明确原因，并允许用户提供入口信息作为候选，不标作自动验证通过 |
| 低于现有最低包版本、WSL1 或其他现有平台门槛不满足 | 延续当前明确的支持边界与处置指引，不在本项中静默放宽 |

Windows 的 listen IP/port 与 guest 的 connect IP/port 分开记录；不要假定两边端口相同。多网卡/VPN/多发行版有歧义时展示候选及选择依据，不擅自覆盖用户设置。

### 3.3 地址含义与可达证据

- 内部服务/health check、Windows 本机入口、外部 LAN 入口分别建模。不能把全局 resolve_lan_ip 直接替换成 Windows IP；guest 不能绑定自己不持有的 Windows IP。
- CLI 导入/启动/status/list、status DTO、管理页、doctor、access refresh/review 消费同一结论；list 若只展示端口，应加必要的网络范围提示，而不是虚构它已经展示 URL。
- lanAddressStale 仍只表示落盘地址是否过期；lanUrlSource 仍只表示来源。另行表达地址范围和暴露状态：内部可用、入口未配置、候选已配置但待外部验证、外部观察已验证、未知。
- 每个成功探测标记观察位置、协议、响应证据和时间。guest 能访问 Windows 端口，不能证明另一台 LAN 设备能访问；不同探测点失败也不能互相替代。历史“外部已验证”在网络/映射变化后要失效或重新验证。
- HTTPS 对外地址需覆盖 Caddy 站点与证书 SAN/名称，并验证客户端信任；回环、别名和管理入口的 scheme/端口不得混用。

### 3.4 操作指引与权限

- 根据实际启用的 HTTP/HTTPS 入口生成建议，不凭默认端口清单要求用户全部开放。
- 建议仅供用户或管理员审阅执行；LWA 不安装网络工具，不改网卡/路由/DNS/VPN、网络模式、防火墙或端口转发，不新增 Windows 网络辅助任务/服务。
- 转发到 guest IP 的方案需说明 IP 漂移与修复路径；回环目标方案必须先验证 localhostForwarding、监听冲突/闭环、多发行版以及重启行为，不能写死为跨版本通用规则。
- 区分 Domain/Private/Public 网络配置文件及规则读取权限；不自动改用户网络类别、关闭防火墙、覆盖既有转发或开放整个实例端口池。
- Windows 管理权限仅在用户执行对应变更时需要；诊断阶段保持只读、拒绝或缺失能力明确降级。
- 管理面经 TCP 转发后要验收客户端身份和鉴权，远程请求不能因 peer/Host/XFF 变化获得本机免鉴权或本机专用 API 权限。

### 3.5 逐条实现核对与待验边界（2026-10-22）

核心地址语义、模式与版本识别、网卡/portproxy/网络类别只读采集、CLI/API/管理页/doctor、HTTPS 多 SAN 及配置建议已实现。此前“第一、二批产品实现已落地”只概括这些主干，不能代替第3节逐条交付判定。

以下两项补充产品功能已由 DEV-168 完成；后续并发、入口绑定与观测保留等缺口已由 BUG-954～961 修复并经 CHK-468 回归核验。下面的勾选表示产品实现完成，不表示独立设备或版本组合实机验收通过：

- [x] **Windows 实际监听与防火墙证据**（对应 §3.1、§3.2，DEV-168）：共享快照采集 Windows TCP 监听、Windows 防火墙配置文件默认入站、Hyper-V 防火墙设置/规则；各自失败保留独立 unknown，不把网卡或 portproxy 误报为不可读。网络类别仍不等于防火墙规则证据；检查点中的人工取证也不等于产品自动采集。正向 LAN 实机仍待验。
- [x] **外部验证证据闭环**（对应 §3.3，DEV-168）：`describe_instance_access` 消费持久化观测并调用 `snapshot_fingerprint` / `exposure_after_network_change`。受约束入口为 `lwa access record-observation` 与 `POST /api/instances/{id}/lan-observation`（位置/协议/证据枚举；仅 `lan-client` 成功可升格 `externally-verified`）。CLI/API/管理页/doctor 展示同一记录；网络/映射变化后读时失效，不删除历史记录。不能凭本机 `access review` 或辅助函数单测宣称局域网已通。正向 LAN 实机仍待验。

这些缺口不改变“不自动修改 Windows 网络”的边界。第三批中尚未完成的正向 LAN、版本组合与恢复事件验收仍单独保留；功能开发完成与实机验收完成分别判断。

## 4. 主要组合验收矩阵

以下是目标矩阵，**不是已支持/已通过的声明**。每次运行填写完整 Windows Build、WSL 包版本、发行版与实际模式；不只填写“2.x/3.x”。

| 编号 | Windows | WSL 软件包 | 网络策略 | 当前证据 |
| --- | --- | --- | --- | --- |
| N10-2 | Windows 10 22H2 | 2.x（不低于现有最低线） | NAT | 2026-10-21 策略模拟通过（包 2.4.13.0 / NAT / 仅宿主可达）。待实机 |
| N10-3 | Windows 10 22H2 | 3.x | NAT | 2026-10-20～22 部分实机：Build 19045.6456 / 包 3.0.3.0 / Debian 13.7。guest 仅宿主可达，两条 SSH 转发可读，网页规则缺失。本轮 V0.9.12 隔离快照补齐 WSL 与 Windows 本机 HTTP/HTTPS 共24项资源内容匹配、显式 CA 与无 SNI Windows IP 校验、非回环管理鉴权、明文收敛和测试进程恢复。独立 Mac 的网页/管理端口均未连通，真实外部转发及恢复矩阵仍待验；历史 Docker/八个样本结果另记 §4 |
| N11-2 | Windows 11 | 2.x（不低于现有最低线） | NAT | 2026-10-21 策略模拟通过：Win11 + NAT 不假设 mirrored。待实机 |
| N11-3 | Windows 11 | 3.x | NAT | 2026-10-21 策略模拟通过（包 3.0.3.0 / Win11 NAT）。待实机 |
| M11-2 | Windows 11 22H2+ 且实际具备相应能力 | 2.x（满足该功能的实际前置条件） | mirrored | 2026-10-21 策略模拟通过：无 portproxy 仍为候选未验证。待实机 |
| M11-3 | Windows 11 22H2+ 且实际具备相应能力 | 3.x | mirrored | 2026-10-22 部分实机：Win11 Insider Preview 28020.3262 / 包 3.0.4.0 / Debian 13.7。运行态 mirrored，空 portproxy 仍为 candidate-unverified；Debian 内和 Windows localhost 的 HTTP/HTTPS 页面/CSS/JS、显式 CA 与本机鉴权通过。独立物理 LAN 设备 ARP 可解析，HTTP/HTTPS 网页及管理入口均 TCP 超时；Windows 自身 WLAN 地址同样超时。本 Debian Docker、恢复和独立 LAN 正向验收仍待完成，见 §4 |
| X10-M | Windows 10 | 支持范围内版本 | 请求/误配 mirrored | 2026-10-21 策略模拟通过：声明 mirrored 实际 NAT，不声称已启用、不自动修改全局配置。待实机确认 wslinfo 回退 |
| X-UNK | 支持范围内 Windows | 支持范围内版本 | 新模式/模式回退/探测失败 | 2026-10-21 策略模拟通过：保留 future-mode-xyz / consomme 原名，超时 unknown，不崩溃、不假绿。待可获得的实机 |

Win11 的版本/Build 与能力边界应覆盖；不能把“Windows 11”四个字当作所有网络功能均可用的证明。未取得的环境保留待测，不用模拟测试替代实机通过记录。

### 4.1 两台已验证机器的环境口径

这里按实际软件包版本归入 N10-3 / M11-3；用户所称 Win10 的“WSL2”和 Win11 的“WSL3”分别涉及架构与软件包号，不能直接当成两个架构比较。

| 维度 | 另一台机器：N10-3 | 本机：M11-3 |
| --- | --- | --- |
| Windows | Windows 10 22H2，10.0.19045.6456 | Windows 11 专业版 Insider Preview，10.0.28020.3262 |
| WSL 架构 / 软件包 | WSL2 / 3.0.3.0 | WSL2 / 3.0.4.0 |
| Linux 与实际模式 | Debian 13.7 / NAT（wslinfo 实测） | Debian 13.7 / mirrored（配置与 wslinfo 一致） |
| 证据时间 / 产品口径 | 2026-10-20～22 历史 V0.9.11 网络及 BUG-943～948 修复；本轮为 V0.9.12 基线 8e4f99c 加本地 BUG-949/950 的独立快照，不混作一次运行 | 2026-10-22；V0.9.12 基线 8e4f99c，另含本轮未提交 BUG-949/950 修复 |
| Docker 后端 | Debian 内 Docker Engine 29.7.2 / Compose 5.5.0，已有真实测试 | 本 Debian 无可用 Docker 集成；Windows 存在 Docker Desktop 不等于该发行版可用 |

两台均为 Debian WSL，不能作为原生 Debian、Ubuntu 或其他 Windows/WSL 组合的完成证据；Win11 实测还需保留 Insider Preview 身份，不推广为所有 Win11 Build 均通过。

### 4.2 分层验证结果对照

| 验证层 | N10-3：Win10 / NAT | M11-3：Win11 / mirrored |
| --- | --- | --- |
| 网络识别与地址语义 | guest 标为仅 Windows 宿主可达；物理 WLAN 作为 Windows 入口候选来源；缺网页转发为 entry-missing | 正确区分架构/包号，七项只读采集 available；按默认路由选物理 WLAN；guest 与 Windows LAN IP 相同；无 portproxy 仍为 candidate-unverified |
| 已有转发识别 | BUG-943 曾使中文输出误解码；修复后同机确认两条 SSH 转发可见，网页转发仍缺失。不能继续用修复前空列表代表当前规则 | portproxy 实际为空；mirrored 不要求添加 NAT 转发，也未因模式启用就声称外部可达 |
| Debian 内 HTTP | 最小页面、相对 CSS/JS 为 200 且内容一致；真实样本首页另见下行 | 实例直连与路径别名为 200；页面、相对 CSS/JS 哈希与样本一致 |
| Windows localhost HTTP | 页面、CSS、JS 为 200 且与 Debian 内容一致 | 实例直连与路径别名的页面、CSS、JS 均为 200，哈希一致 |
| HTTPS / CA | 本轮 WSL 与 Windows 本机 HTTPS 页面/CSS/JS 共12项内容匹配，显式 CA 通过、不指定 CA 失败；Windows IP 无 SNI 校验以 connect-to 映射 localhost 验证，实际 LAN 转发入口尚未连通 | Debian 内 localhost/LAN 地址及 Windows localhost 的 HTTPS 页面、CSS、JS 通过显式 CA 校验；不指定 CA 的反向校验失败。未导入系统/浏览器信任库 |
| 管理鉴权与明文旁路 | 本轮 guest 地址及保留 Windows Host 的本机连接映射请求无 token 为401、有效 token 为200；guest 非回环来源伪造 Host/XFF 仍401。回环绑定并重启实例后，guest 上三个明文口拒绝连接。实际 localhost + 回环 Host 仍为本机免 token 路径，不能替代真实外部转发鉴权 | Debian 经 LAN 地址访问 HTTP/HTTPS 管理 API：无 token / 伪造回环 Host、XFF 为 401，有效 token 为 200。回环绑定并重启实例后，Debian LAN 地址不能访问明文直连口、管理后端和旧 HTTP 别名口。仍不等于独立设备正向鉴权验收 |
| 独立物理 LAN 客户端 | 本轮独立 Mac 直连：HTTP阶段3个端口、HTTPS阶段两个TLS与三个明文端口均未连通，未取得页面或鉴权正向证据。网页映射缺失，未新增规则；不将超时唯一归因为防火墙 | 经用户指定物理设备、物理 WLAN 直连，无 SSH 隧道；ARP 可解析，HTTP 实例/别名/管理与 HTTPS 别名/管理均 TCP 超时。Windows 自身 WLAN 地址也超时；Hyper-V 默认入站 Block 是排查线索，尚未定位具体丢弃规则 |
| 浏览器样例交互 | 本轮通过临时仅回环SSH通道在真实Chromium浏览器点击按钮0→1→2，已留AX与截图；只证明样例JS执行，不计LAN或真实应用业务验收 | 已验证资源字节，交互式浏览器业务仍待验 |
| Docker 与真实应用（独立于网络验收） | 真实 Docker 20 项及 M1 容器 1 项通过；A01～A07、A09 共八个样本首页 200，A08/A10/A11 保持 pending 且 start 被拒绝。A06/A09 修复后同机复跑通过，A09 仍需应用自身 CMD_DB_URL。不是 11 项完整生命周期全部通过 | 仅静态验证；本 Debian Docker、Compose、宿主前端构建和有状态应用未验，不借用 N10-3 结果填充 |
| 恢复与自启 | 历史重启后未安装自启单元、手动恢复。本轮仅在测试工作区执行实例 restart、网关 off/on，三个HTTPS资源内容不变且CA未变；不算WSL/Windows重启、换网、休眠或自动恢复通过 | 未执行 Windows/WSL 重启、休眠或地址变更；临时服务已停止，未安装自启 |

上述表格保留2026-10-22历史批次：当时两台均只有本机成功与独立LAN负向。2026-10-23 N10-3已取得部分独立LAN正向，见§4.5；M11-3当晚续验仍超时，见§4.6。NAT的网页转发缺口与mirrored的入站链路超时分别记录，不能合并成同一根因，也不能因后者不需要portproxy就跳过防火墙和独立客户端验收。

### 4.3 修复、回归与当前验收边界

- **BUG-943 已修复且同机读规则验证完成**；后续待办是网页入口正向访问，不再是修复解码或重读 SSH 规则。
- **BUG-944 已修复**；本轮在 N10-3 隔离工作区补了真实 Windows 本机显式 CA、按 Windows IP 校验的无 SNI 连接与内容验证；连接映射到 localhost，不升格为实际 LAN 转发入口通过，后者仍待可用网页入口。
- **BUG-945～948** 的 Debian Docker 安装、首次部署、构建依赖等后续结果按台账及 Eval 方案保留；不将容器构建/首页 200 升格为局域网网络验收。
- **BUG-949/950** 来自 M11-3：移除候选“已发现转发”的无证据表述；无 Git 源码或取锁不可用时版本检查降级 SKIP，不中断整份 doctor。修复后 doctor 正常输出；其 overall=FAIL 仍因本 Debian Docker/Compose 不可用，不能写成整机健康通过。
- N10-3 当时 Debian 常规门禁为 **4077 passed / 27 skipped**，真实 Docker 另跑 **20 + 1 项通过**；M11-3 九个相关模块为 **446 passed / 0 skipped / 0 failed**，含显式启用的真实 Caddy TLS 测试。两者对应不同时间、代码与测试范围，不合并成一次门禁，也不将跳过项算通过。

### 4.4 证据索引与汇总方式

- N10-3 网络与真实容器：台账 CHK-455、CHK-456、CHK-458；HTTPS 机制：CHK-459。样本范围与后续状态见 [真实应用跨系统 Eval 方案](2026-10-12-LWA真实应用跨系统Eval方案.md) 的 H-DEB-WSL 行。
- M11-3 本机验证与独立 LAN 负向结果：台账 CHK-462、CHK-463，详细结论见 [Win11 / WSL 包 3.x / Debian mirrored 验证记录](../2-achievement/2026-10-22-Win11-WSL软件包3.x-Debian-mirrored本机验证.md)。
- 原始快照、SSH/实际主机信息、监听、请求及测试日志仅留在已忽略的本地检查点。本规划仅保留版本、类型化结论、关联编号和后续工作，不复制真实地址或接入信息。
- 既有汇总任务本身没有重新执行测试；本轮 N10-3 新增实测另见 [第三批续验记录](../2-achievement/2026-10-22-WSL第三批N10-3实机验收续验.md)，包括24项资源内容、独立 Mac 负向访问、测试进程恢复及清理核验。新增执行计划或环境初探不替代尚未执行步骤的通过证据。

### 4.5 2026-10-23 N10-3 独立设备续验

本次使用HEAD `02696f9`加当前待提交源码的冻结快照，未把旧11样本结果冒充新快照通过。管理员手工创建仅允许独立Mac来源的临时网页转发；LWA不修改Windows规则。Win11按用户安排跳过，留记录在另一台机器续跑。

- 已取得真实LAN正向：HTTP/HTTPS各3项资源200且内容哈希一致；浏览器相对资源、CSS与点击交互通过；HTTPS校验Windows入口IP、显式CA与不信任反向通过；独立Mac通过别名读取真实SQLite容器的持久记录。
- 隔离容器产品restart、daemon启动reconcile恢复后数据仍在；测试manager进程崩溃经临时systemd单元恢复，CA不变。仅为进程/容器恢复，不替代休眠、WSL关闭、guest地址变化、Windows重启或自启。
- **BUG-980/P1：** Windows回环转发丢失来源，外部伪造Host与同源头可越过管理免token判定。普通无token401、有效token200不消除这一失败；管理鉴权保持未通过。
- **BUG-981/P2：** 网络与转发不变时，无关Windows回环监听消失也使已记录的LAN证据失效。已用两侧可读快照与API验证，不能写作真实地址变化验收通过。
- 相关自动回归114 passed / 2 skipped；版本组合、权限/未知模式/多网卡的模拟不升格实机。完整记录与后续机器复跑清单见 [本轮验收记录](../2-achievement/2026-10-23-WSL第三批N10-3独立设备验收.md)。

### 4.6 2026-10-23 M11-3 本机与独立LAN续验

承接CHK-489：[Win11 / Debian mirrored续验](../2-achievement/2026-10-23-Win11-Debian-mirrored续验.md)。Win11 Build28020.3262 / WSL包3.0.4.0 / Debian13.7，冻结当前V0.9.13待提交源码。Debian HTTP12项、HTTPS6项以及Windows localhost HTTP6项、HTTPS3项资源内容匹配；Debian按LAN地址的无/无效token及伪造Host/XFF均401，有效token200。TLS阶段三个明文口在Debian LAN地址连接拒绝，多网卡按默认路由选择物理WLAN正确。

独立Linux沿物理WLAN直连：HTTP11请求、HTTPS阶段12请求全部超时，未进入远端TLS/鉴权验收；Windows自身WLAN地址也不可达。只读ActiveStore显示Hyper-V默认入站Block，抓包重叠窗口未见远端SYN；尚未定位具体丢包规则。新增BUG-989：产品默认存储查询显示NotConfigured，遗漏实际生效Block策略，不能因采集available就当作读到了生效规则。

本Debian仍无Docker Desktop集成，真实容器待验；未修改Windows网络、启用系统信任或重启宿主。其余组合、实机退化情形与完整恢复仍待测，不关闭PLN-054。

21:31后续（OPS-221/CHK-490）：已在本Debian安装原生Engine29.9.2/Compose5.6.1并验证普通用户可用，代表SQLite容器经LWA生成/构建/启动、HTTPS写入、产品restart数据保留及Windows localhost读取通过。本机Docker环境缺口解除，无需开启Docker Desktop集成；前文“本Debian容器待验”为此前历史快照。独立LAN正向与宿主恢复仍待，见 [原生Docker安装与容器验收](../2-achievement/2026-10-23-Win11-Debian原生Docker安装与容器验收.md)。

## 5. 每组验收的内容与分层

1. 最小静态页、相对 CSS/JS，从 WSL 内、Windows 本机和另一台 LAN 设备分别读取，核对业务内容，不能只用 TCP 成功或 HTTP 200 判定。
2. 覆盖映射缺失、错误目标、端口冲突、不同 listen/connect 端口、多网卡/VPN、规则只读被拒、interop 关闭和非登录 SSH PATH 差异。
3. HTTP 与启用后的 HTTPS 分开验收：证书匹配/信任、路径别名及管理鉴权，无额外明文旁路。
4. 休眠恢复、guest IP 变化、WSL 关闭重开、Windows 重启分开记录；自启与 Windows 唤醒条件分别验证。不得在承载业务的机器上擅自重启来补矩阵。
5. 在正式支持的 Linux 发行版中覆盖 Ubuntu 24.04 和 Debian 13；网络最小用例先跨主要组合验证，真实前端/有状态容器再覆盖代表组合及不同 Docker 后端，随后对照既有 11 项目计划扩展回归。
6. Docker 26.1.5 / Compose 2.26.1 的完整验证属于 CHK-439，独立保留其未完成状态。当前静态 WSL 网络核查不能替代容器、Compose、数据库持久化或最低版本验收。
7. 非 WSL 的 macOS/Linux 地址、端口与安全行为做回归验证；不因增加 WSL 适配改变原平台策略。

测试层级：解析与策略单测 → CLI/API/页面/doctor 合约测试 → 各主要组合实机。发布说明分别列出功能实现、模拟覆盖、实测组合和待测组合。

## 6. 后续工作划分

- [x] 评审统一环境快照与能力字段，确认兼容现有配置/API 的演进方式。
- [x] 验证对用户已有网络、转发/监听和权限的只读识别，以及缺少入口时的清晰说明；不把安装第三方工具或自动规则维护纳入本次实现。
- [x] 实现模式/Windows 能力采集与超时降级，补齐 2.x/3.x、Win10/11 的解析与策略测试。
- [x] 统一用户可见地址范围、诊断状态和配置建议；补齐 HTTPS/鉴权配套。
- [x] 扩展现有 WSL 用户指南、已知限制、平台与自启验收矩阵，不另建冲突说明。
- [x] 2026-10-21：补 HTTPS SAN（Windows 入口 IP + bind 0.0.0.0）、manualLanIp 仅作候选、localhostForwarding / Public 配置文件只读提示，以及 N10-2/N11/M11/X10-M/X-UNK 策略模拟。
- [ ] 准备主要组合的隔离验收环境，按本矩阵记录真实证据；不足环境保持待测。
- [x] 2026-10-21～22：N10-3 完成 BUG-943 修复后的同机规则读取，确认两条既有 SSH 转发可见、网页规则仍缺失；Docker Engine 与真实容器、样本结果按 §4 汇总。N10-3 不再描述为“尚无 Docker”或“BUG-943 待修复”。
- [x] 2026-10-22：M11-3 完成本机分层 HTTP/HTTPS、静态资源哈希、显式 CA 信任与不信任反向验证、管理鉴权及回环绑定后的明文旁路检查；修复 BUG-949/950，446 项相关回归通过。Windows 自身 WLAN 地址超时；恢复和本 Debian 容器仍待验。
- [x] 2026-10-22 同日补测独立物理 LAN 客户端：经用户指定 SSH 别名执行客户端直连，无隧道。ARP 可解析，HTTP 实例/别名/管理及 HTTPS 别名/管理入口均 TCP 超时；Debian 本机各阶段页面仍正常。Hyper-V 防火墙默认入站 Block，未自动改规则，具体丢弃层尚未定位。记录负向实测，正向 LAN 验收继续待完成。
- [x] 关联 BUG-923 的产品实现与组合验收边界；已取得 N10-3 和 M11-3 的部分实机证据，不能标记完整组合验收完成。
- [ ] N10-3 正向 LAN：由用户/管理员审阅已有入口与实际所需端口，具备网页入口后从独立 LAN 设备核对页面、CSS/JS、端口映射与鉴权。既有 SSH 转发不代替网页入口，LWA 不自动添加转发或防火墙规则。
- [x] 2026-10-23 N10-3：管理员提供临时入口后，独立Mac的HTTP/HTTPS资源、浏览器交互及代表SQLite容器数据读取通过；manager/容器进程恢复分项通过。整体验收因新发现BUG-980鉴权与BUG-981观测缺口继续未完成，修复后须同路径复验。
- [x] 2026-10-22 N10-3 本机 HTTPS：隔离工作区启用 internal CA，补齐 WSL/Windows localhost 内容、按 Windows IP 校验的无 SNI 本机连接、显式 CA/不信任反向验证、非回环鉴权及明文收敛；测试进程恢复与清理已留证。
- [ ] N10-3 实际 LAN 转发入口 HTTPS：网页入口可用后，从独立设备核对真实 Windows URL 的证书、内容和管理鉴权；本机 connect-to 验证不能代替实际转发入口。
- [ ] M11-3 正向 LAN：只读定位 Windows/Hyper-V 入站链路的超时原因；用户/管理员完成必要网络处置后，复测独立设备 HTTP/HTTPS、资源内容与管理鉴权。当前不得改写为“已外部验证”。
- [ ] 两台的恢复场景：分别补休眠、WSL 关闭重开、地址变化、Windows 重启与自启；N10-3 的一次手动拉起观察不替代完整恢复矩阵。
- [ ] 容器与 Eval 独立收口：N10-3 已有真实 Docker 和八个样本首页证据，完整生命周期及持久化仍按 Eval 方案逐项确认；M11-3 本 Debian 的 Docker 集成/容器仍未验。Docker 26.1.5 / Compose 2.26.1 最低版本完整验收继续归 CHK-439，不用较新版本结果代替。

本草案不触发安装、降级/升级 WSL、修改 Windows 全局设置或产品代码提交。真实 SSH/主机信息与原始证据只放已忽略的本地检查点；本文件仅含通用版本、规则和验收目标。

## 7. 开发就绪度与关联 issue（2026-10-20 核查）

第一、二批已落地：以 BUG-923 / issue #140 为主线，共享 `wsl_network` 快照，统一地址范围与诊断证据，并只读发现 Windows 网卡与 portproxy。NAT 的 guest 地址不宣传为局域网入口；缺少匹配转发时 doctor 报 WARN，mirrored 的候选也不等于外部已验证。2026-10-21～22 后续已补 BUG-943 同机规则读取、BUG-944 多 SAN 证书机制，以及 N10-3 / M11-3 分层实机证据，当前结果统一见 §4，不再沿用“已有规则不可读”“尚无 Docker”作为 N10-3 当前状态。两台正向 LAN、其他主要组合和完整恢复仍未闭环；本节 issue 状态表仍是 2026-10-20 的历史关联核查，不是本次重新查询远端状态。

### 7.1 开发批次

| 批次 | 交付范围 | 验收重点 |
| --- | --- | --- |
| 第一批：模式与地址语义 | 细化共享环境快照、网络模式/地址范围/证据字段及旧字段兼容规则；统一 CLI、DTO/API、管理页、doctor 的展示与判断 | NAT guest 地址不再宣传为已可用 LAN 入口；stale=false 不充当可达证明；命令缺失、interop 超时与权限不足明确降级；macOS/普通 Linux 保持回归 |
| 第二批：现有入口适配 | 只读发现 Windows 网卡、监听及已有转发；支持端口转换、多候选与用户显式提供入口；联动 access 与 HTTPS 的地址契约 | 区分 guest 绑定地址与 Windows 外部地址；候选入口未获外部证据不标通过；地址变化后旧证据失效；转发后的鉴权边界不放宽 |
| 第三批：实机与用户文档 | 先复验已具备的 Win10 / WSL 包3.x / Debian NAT 环境，再补主要版本/模式组合；整理系统类型化结论 | 页面、CSS/JS、业务动作、HTTPS/鉴权及适用恢复场景分项留证；未具备组合继续待测，不能将单机通过写成全部组合支持 |

第一批实现时可用模拟 Windows 输出覆盖解析与策略，但模拟结果不能填作主要组合实机通过。Docker 最低版本验证仍归 CHK-439，不与本项合并。

### 7.2 issue 对照

以下状态来自 2026-10-20 的 GitHub 实时核查；“已关闭”表示远端 issue 状态，本轮没有重新证明对应功能在所有 WSL 组合下均通过。

| 关联程度 | issue | 当前状态 | 本项如何使用 |
| --- | --- | --- | --- |
| 主问题 | [#140：WSL NAT 地址、诊断假绿与 LAN 入口发现](https://github.com/wangminle/local-webpage-access/issues/140) | 开放 | BUG-923 的修复主线；按本草案收紧并扩展为跨版本适配，不照搬正文中尚未证实的通用转发建议 |
| 必需回归 | [#44：LAN IP 漂移后的 Caddy TLS 绑定不同步](https://github.com/wangminle/local-webpage-access/issues/44) | 已关闭 | 展示地址、Caddy 站点和证书匹配共同验证；不能只更新 manifest 中的 URL |
| 必需回归 | [#9：反代后的客户端身份与转发头](https://github.com/wangminle/local-webpage-access/issues/9) | 已关闭 | 验证外部入口下的来源身份与鉴权；历史结论明确转发头不可作为鉴权凭据，未交付 HMAC 签名头 |
| 诊断呈现回归 | [#52：已检测失败却未在用户结果中收口](https://github.com/wangminle/local-webpage-access/issues/52) | 已关闭 | 网络限制不能只写日志，CLI/API/管理页/doctor 要表达同一证据范围；原问题是别名兼容，不是 WSL 网络根因 |
| 采集实现参考 | [#55：外部系统命令无超时](https://github.com/wangminle/local-webpage-access/issues/55) | 已关闭 | Windows interop 与模式采集必须有超时和失败降级，不能阻塞普通状态轮询 |
| 状态语义参考 | [#56：无法判定被误当通过或冲突](https://github.com/wangminle/local-webpage-access/issues/56) | 已关闭 | 读取规则被拒或命令不支持时保留 unknown；实际监听/鉴权仍按各自安全策略处理，不从 unknown 推导自动放行 |
| 相邻专项 | [#36：构建下载源与代理路径诊断](https://github.com/wangminle/local-webpage-access/issues/36) | 已关闭，部分增强另在台账待办 | 区分容器构建出站与网页 LAN 入站；DEV-148 的代理策略/构建路径诊断不纳入本次首批 |
| 恢复专项参考 | [#29：运行时晚就绪与能力缓存收敛](https://github.com/wangminle/local-webpage-access/issues/29) | 已关闭 | 主要是 macOS/Docker 启动时序；WSL 恢复后的重新采集与诊断缓存失效可参考，不视为 NAT 地址缺陷 |

不因上述历史关联而批量重开 issue，也不把所有带“网络”字样的问题并入 BUG-923。最新其他部署 issue 应按各自根因独立处理。

### 7.3 2026-10-24 本机安装与修复续验

2026-10-24实机补充（CHK-491）：当前Win11 Insider/WSL3.0.4/Debian13.7 mirrored可执行十一真实项目首次安装，7项通过、1项静态资源部分通过、3项门禁/形态边界；含4个真实原生Engine容器及Windows localhost读取。BUG-989工作树修复已在实际互操作采集中读到ActiveStore/Block与生效规则，BUG-990/992另冻修复快照的取消/环境更新实机复验通过；BUG-997静态资源缺口待修。详见[本轮成果](../2-achievement/2026-10-23-Win11-mirrored-Debian十一项目安装验收.md)和[全部问题](../4-issues/2026-10-24-mirrored十一项目构建问题汇总.md)。本机成功不替代独立LAN正向、其他组合与宿主恢复，PLN-054保持进行中。

## 8. 官方依据

- [WSL 架构与软件包更新](https://learn.microsoft.com/en-us/windows/wsl/compare-versions)
- [WSL 软件包发布记录](https://github.com/microsoft/WSL/releases)
- [WSL 网络与 mirrored 前置条件](https://learn.microsoft.com/en-us/windows/wsl/networking)
- [WSL 网络配置及模式回退](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)
- [Windows 系统版本号](https://learn.microsoft.com/en-us/windows/win32/sysinfo/operating-system-version)
- [Windows Build 与运行时 API 能力](https://learn.microsoft.com/en-us/windows/apps/get-started/versioning-overview)
