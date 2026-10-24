# Win11 / Debian mirrored 第三批续验

日期：2026-10-23。关联 PLN-054、CHK-489、BUG-989，承接 CHK-462/463。

**结果：本机网络识别、HTTP/HTTPS与鉴权复验通过，独立LAN仍超时。21:31追加原生Docker安装和代表容器持久化验收通过；独立LAN正向与宿主恢复仍待，PLN-054保持进行中。**

下表Docker缺失为20时批次的初探快照，已由后续原生安装解除；不再要求必须开启Docker Desktop集成。最新结果见 [原生Docker安装与容器验收](2026-10-23-Win11-Debian原生Docker安装与容器验收.md)，关联OPS-221、CHK-490。

## 环境与快照

- Windows 11 专业版 Insider Preview 10.0.28020.3262；WSL2 架构、软件包 3.0.4.0、内核 6.18.54.1-1；Debian 13.7，运行态 `wslinfo` 为 mirrored，与配置一致。
- 产品 V0.9.13，HEAD `02696f90cb36c5855ab5b9d89c8f6d51c905ddcd` 加工作区已有待提交改动，不能归为纯 HEAD 结果。冻结355个源码/文档/测试文件，文件清单指纹 `9133fa8878cca227930d045e92647c5913fdf463b93a7f6ab1d9943d00ac5242`。产品代码在本轮验证期间未修改。
- Linux 原生文件系统中创建独立源码与运行工作区，使用新测试端口；复用已有 Python3.13 venv 和 Caddy 2.11.7，不安装工具、系统信任或自启单元。
- 独立 Linux 设备通过已有 SSH 入口执行客户端探测，请求沿物理 WLAN 直连测试机 LAN 地址，没有建立 HTTP 代理或 SSH 端口隧道。客户端路由和 ARP 为实际 LAN 路径。
- 私有地址、SSH 接入、token、CA和原始证据仅保留在已忽略检查点 `design/5-checkpoints/2026-10-23-m11-local-recheck/`；公开材料只记录泛化结果。

## 本轮结果

| 分项 | 结果 | 边界 |
| --- | --- | --- |
| 版本、模式与多网卡 | 十项采集能力 available；默认路由选物理 WLAN，排除虚拟网卡和 Tailscale 候选；guest 与 Windows LAN 地址一致，portproxy 实际为空 | 仅新增当前 Win11 / 包3.x / mirrored 组合证据，不代替其他版本或模式 |
| Debian HTTP | 回环/LAN 地址各自的直连和别名 HTML/CSS/JS，共12项200且哈希一致 | LAN 地址上的本机请求仍是 guest 观察点 |
| Windows HTTP | localhost 直连和别名6项资源匹配；管理页与本机 API 可用 | Windows 自身 WLAN 地址的11个请求仍超时 |
| Debian HTTPS | 回环和 LAN 地址的别名 HTML/CSS/JS，共6项200且哈希一致，显式 CA 和地址身份校验成功 | 未安装系统 CA；未做浏览器交互 |
| Windows HTTPS | localhost 别名3项资源匹配，管理页/API 可用，显式 CA 验证成功；不加载 CA 时证书链验证失败 | 自身 WLAN 地址的两个 TLS 入口仍超时，不能写成该地址在 Windows 上通过 TLS |
| 管理鉴权 | Debian 按 LAN 地址访问 HTTP/HTTPS 能力 API：无 token、无效 token、伪造回环 Host/XFF/同源头均401；有效 token 为200 | mirrored 的真实 localhost 按现有可信本机策略免 token，不能将本机200计为远端鉴权通过；未重跑 Win10 NAT 回环转发路径 |
| 明文旁路 | TLS阶段回环绑定并重启实例后，Debian 按 LAN 地址访问实例明文、管理后端及旧 HTTP 别名口均连接拒绝，HTTPS继续成功 | 独立设备明文请求亦超时，但它连 TLS入口也不可达，故不足以单独证明远端明文收敛 |
| 独立 LAN HTTP | 6项页面/资源、管理页及4种token/Host组合，共11请求全部超时 | 没有取得远端内容或鉴权结果 |
| 独立 LAN HTTPS | 3项资源、管理页及4种鉴权请求、1项不信任CA负例，共9个TLS请求全部超时；3个明文请求也超时 | 未进入TLS握手，CA正反向与远端鉴权仍待验 |
| 只读入站定位 | ActiveStore：WSL Hyper-V启用、默认入站Block、出站Allow、回环启用；规则无测试端口TCP放行。15秒头部计数抓包的重叠窗口有本机测试流量，没有独立客户端SYN | 尚未定位具体丢包规则；抓包未覆盖所有远端请求全过程，不下唯一根因结论 |
| 产品诊断 | CLI保持candidate-unverified，doctor的WSL项为WARN，没有外部验证假绿；整机doctor为FAIL，Docker/Compose不可用 | 新发现防火墙采集存储语义缺口，另记BUG-989 |
| Docker与真实容器 | Debian 的docker命令返回“请开启Docker Desktop WSL集成”，Compose不可用，Linux socket不存在 | 当前无法完成本Debian真实容器验收；Windows Docker存在、其他发行版或其他主机容器不代替此项 |

HTTP/HTTPS各阶段都有WSL监听与逐请求证据；显式CA校验没有使用忽略证书错误选项。管理员尚未处置入站路径，本轮没有添加portproxy、放行防火墙或切换WSL模式。

## 回归与测试修订

首轮七个模块结果为 **361 passed / 1 failed**。失败项 `test_cli_manager_start_rejects_lan_binding_without_token` 在Linux调用init时先启动真实后台manager并颁发token，之后只mock ensure_token，CLI读到了既有token，已不满足“无token”前提。这是测试夹具副作用，不是已经证明产品允许无token绑定。

本轮仅调整该测试：初始化时禁用后台manager启动，并显式mock read_token返回None；未修改产品鉴权。首轮日志和XML保留。修订后的测试文件单独记录哈希，产品源码仍为原冻结快照。

修订后七个模块 **362 passed / 0 skipped / 0 failed**，其中显式启用两个真实Caddy TLS用例；运行于同一产品源码快照，仅测试夹具更新并单独留存哈希。修改测试的Ruff检查、Git差异空白检查及台账CLI check通过。本轮未跑完整门禁，不以历史全量门禁替代当前验证。

## 清理与剩余验收

实验实例、网关和manager已通过产品CLI停止；首轮失败测试遗留的后台manager按精确PID与pytest工作区归属核对后停止。最终仅余原有DNS监听、无用户级运行服务，测试端口及该测试manager端口均释放。Hyper-V ActiveStore设置、规则与portproxy的前后快照逐字节一致。保留实验数据、CA和源码供续跑；未删除既有工作区，不触碰其他发行版或生产容器。

下一轮按以下条件续验：

1. 管理员审阅并提供仅测试端口、仅观察机来源的mirrored入站路径后，重跑独立LAN页面、资源、TLS身份/CA正反向、鉴权、明文旁路与观测记录；不得直接把默认入站改为全Allow。
2. 已于21:31通过原生Engine29.9.2/Compose5.6.1完成本Debian代表容器、HTTPS写入与产品restart数据保留，并由Windows localhost读取同一记录；无需开启Docker Desktop集成。后续真实应用矩阵继续使用该原生Engine，不借用其他发行版结果。
3. Win10 NAT修复后同路径独立设备复验、WSL包2.x/Win11 NAT/其他组合、实机权限不足/未知模式/错误映射继续待测；多网卡仅当前真实环境分项通过，现有策略模拟不能填充实机矩阵。
4. 休眠、WSL关闭重开、guest地址变化、Windows重启与系统自启另需维护窗口。本轮只执行测试实例重启与网关切换，不计作宿主恢复通过。

未提交Git、未修改远端issue。第二步部署相关工作可继续，不被上述环境缺口阻断。
