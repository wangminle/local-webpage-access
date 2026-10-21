# WSL 跨 Windows 版本网络适配：需求与验收草案

日期：2026-10-19。状态：第一、二批产品实现已落地；2026-10-21 补齐 HTTPS SAN、用户入口候选、localhostForwarding / 网络配置文件只读与策略模拟。N10-3 已复验产品口径（Win10 22H2 Build 19045.6456 / WSL 包 3.0.3.0 / Debian 13 / NAT）：guest 地址标为仅 Windows 宿主可达，缺 :8080 转发时 lan_url_stale 与 wsl_lan_exposure 为 WARN。独立 LAN 客户端页面/CSS/HTTPS 与其余组合仍待实机，不把模拟或单机通过写成矩阵全支持。

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

## 4. 主要组合验收矩阵

以下是目标矩阵，**不是已支持/已通过的声明**。每次运行填写完整 Windows Build、WSL 包版本、发行版与实际模式；不只填写“2.x/3.x”。

| 编号 | Windows | WSL 软件包 | 网络策略 | 当前证据 |
| --- | --- | --- | --- | --- |
| N10-2 | Windows 10 22H2 | 2.x（不低于现有最低线） | NAT | 2026-10-21 策略模拟通过（包 2.4.13.0 / NAT / 仅宿主可达）。待实机 |
| N10-3 | Windows 10 22H2 | 3.x | NAT | 2026-10-20 产品口径复验通过：Build 19045.6456 / 包 3.0.3.0 / Debian 13。status/list/API/doctor 将 172.22.148.94 标为仅 Windows 宿主可达；hostLanIp 为物理 WLAN；无 :8080 portproxy 时暴露为入口未发现。2026-10-21 从当前 macOS 探测该 Windows IP 无响应，独立 LAN 客户端页面/CSS/HTTPS 仍未做，不升格为矩阵全通过 |
| N11-2 | Windows 11 | 2.x（不低于现有最低线） | NAT | 2026-10-21 策略模拟通过：Win11 + NAT 不假设 mirrored。待实机 |
| N11-3 | Windows 11 | 3.x | NAT | 2026-10-21 策略模拟通过（包 3.0.3.0 / Win11 NAT）。待实机 |
| M11-2 | Windows 11 22H2+ 且实际具备相应能力 | 2.x（满足该功能的实际前置条件） | mirrored | 2026-10-21 策略模拟通过：无 portproxy 仍为候选未验证。待实机 |
| M11-3 | Windows 11 22H2+ 且实际具备相应能力 | 3.x | mirrored | 与 M11-2 同一策略模拟；待实机 |
| X10-M | Windows 10 | 支持范围内版本 | 请求/误配 mirrored | 2026-10-21 策略模拟通过：声明 mirrored 实际 NAT，不声称已启用、不自动修改全局配置。待实机确认 wslinfo 回退 |
| X-UNK | 支持范围内 Windows | 支持范围内版本 | 新模式/模式回退/探测失败 | 2026-10-21 策略模拟通过：保留 future-mode-xyz / consomme 原名，超时 unknown，不崩溃、不假绿。待可获得的实机 |

Win11 的版本/Build 与能力边界应覆盖；不能把“Windows 11”四个字当作所有网络功能均可用的证明。未取得的环境保留待测，不用模拟测试替代实机通过记录。

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
- [x] 关联 BUG-923 的现象修复与本跨版本适配范围，分别核对实现完成和组合验收完成（实现已完成；组合验收仅 N10-3 口径复验）。

本草案不触发安装、降级/升级 WSL、修改 Windows 全局设置或产品代码提交。真实 SSH/主机信息与原始证据只放已忽略的本地检查点；本文件仅含通用版本、规则和验收目标。

## 7. 开发就绪度与关联 issue（2026-10-20 核查）

第一、二批已落地：以 BUG-923 / issue #140 为主线，共享 `wsl_network` 快照，统一地址范围与诊断证据，并只读发现 Windows 网卡与 portproxy。lanUrl 仍是 guest 绑定地址，不再宣传为局域网入口；缺少匹配转发时 doctor 报 WARN。2026-10-21 补齐 TLS SAN、用户入口候选与只读能力字段，并用模拟输出覆盖主要组合策略；N10-3 已复验产品口径，独立 LAN 客户端与其余组合仍待实机。

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

## 8. 官方依据

- [WSL 架构与软件包更新](https://learn.microsoft.com/en-us/windows/wsl/compare-versions)
- [WSL 软件包发布记录](https://github.com/microsoft/WSL/releases)
- [WSL 网络与 mirrored 前置条件](https://learn.microsoft.com/en-us/windows/wsl/networking)
- [WSL 网络配置及模式回退](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)
- [Windows 系统版本号](https://learn.microsoft.com/en-us/windows/win32/sysinfo/operating-system-version)
- [Windows Build 与运行时 API 能力](https://learn.microsoft.com/en-us/windows/apps/get-started/versioning-overview)
