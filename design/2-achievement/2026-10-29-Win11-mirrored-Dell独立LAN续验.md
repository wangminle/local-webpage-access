# Win11 mirrored：Dell 独立 LAN 续验

日期：2026-10-29～30。关联 CHK-521、BUG-1016、PLN-054 / M11-3。独立 LAN HTTP/HTTPS 正向、临时规则撤销及专属服务清理已完成。撤销时发现的历史观测假绿已修复，修复后的状态转换使用实采规则回放验证，并补新鲜负向实机读数；未为修复版重新开放 LAN。其他组合、宿主恢复和完整双设备浏览器/IPv6 清单保持独立待测。

## 被测端与客户端

- 被测端为本机 Windows 11 / Debian 13 / WSL2 mirrored，运行态模式已重新采集。使用当前 0.9.18 工作树的冻结副本，包含本轮开始前已有未提交修改，不宣称纯 HEAD 或已发布版本。
- 观察端为用户授权使用的独立 Dell Ubuntu 物理设备。已通过已有 SSH 连接；请求路由为物理 WLAN 直接到被测 Windows LAN 地址，邻居解析成功。SSH 仅用于执行探针，不转发网页端口；HTTP 探针禁用代理。
- 新建 Linux 原生目录，源码、实例、manager、Caddy 管理端口及证据与既有服务隔离；复用已有 Python 3.13 venv 和 Caddy 二进制。未安装系统 CA、修改 WSL 配置或停止既有服务。
- 私有地址、连接信息、完整命令、运行态及原始证据仅存已忽略的 `design/5-checkpoints/2026-10-29-m11-lan-retest/` 和 Debian 专属运行目录，不导出 token 或私钥。

## 放行前实测

| 范围 | 结果 |
| --- | --- |
| Debian 自身 LAN 地址 | 20 项断言通过，含 HTML/CSS/JS 哈希、管理资源、无/错 token 与伪造头拒绝、有效 token、信息脱敏、Agent/目录本机边界 |
| Windows localhost | 15 项断言通过，含三份资源正文哈希、管理静态资源及可信本机请求行为；不代替远端鉴权 |
| Dell 独立 LAN | 16 个 HTTP 请求全部 TCP 超时，未进入应用鉴权或读取内容；邻居仍可解析 |
| Windows / Hyper-V ActiveStore | 默认入站 Block、出站 Allow；WSL AllowHostPolicyMerge 已启用。只读快照列出了默认阻断及其他规则，但尚不能断言唯一命中的丢弃规则 |
| 产品诊断 | 本隔离实例 status、doctor 命令均退出 0，完整输出已留证；未把本机成功记作独立 LAN 已验证 |

## 采证脚本修订

首次探针的两个旧预期不适用于当前 mirrored 契约：远端有效 token 也不解锁 `/api/health` 的 workspaceRoot；可信 Windows localhost 无 token、无来源头的写请求被 CSRF 检查拒绝为 403。核对现有产品实现后，只修订检查点中的采证断言并复测，原始失败 JSON 保留。没有为使探针通过而修改产品代码。

## 下一阶段与收尾条件（准备时记录，现已执行）

已准备并语法检查 `windows-test-entry.ps1`，由用户在管理员 PowerShell 手工执行。AddWindows 只添加 Windows 防火墙的三条本轮规则；外部复测后，必要时再执行 AddHyperV。两层均只允许独立 Dell 来源访问本机三个专用 TCP 入口，不开放 manager 后端或实例明文端口，不创建 portproxy，不更改默认策略。Remove 核验登记对象归属和过滤条件后，只撤销本轮规则。

接下来依次完成分层入站复测、独立 LAN HTTP/HTTPS 资源内容、CA 信任及 IP 身份、错误 SNI、管理鉴权与明文旁路。结束时先撤销规则并保留服务运行，对照 Dell 不可达与 guest 仍健康；再停止本轮服务并核对原服务与规则。完整链条完成前，CHK-521 保持进行中，M11-3 与 PLN-054 均不标记完成。

## 用户放行后实测

用户已在管理员 PowerShell 手工执行 AddWindows。首条命令因变量尚未赋值而没有运行脚本；后续完成变量赋值后执行成功。归属文件确认仅创建三条 Windows 规则，Hyper-V 专用规则列表为空。

| 范围 | 结果 |
| --- | --- |
| Dell HTTP | 20 项断言全部通过：资源正文 SHA256、管理页/静态资源、无/错 token 与伪造头 401、有效 token 200、故意不存在实例的授权写请求 404、目录与 Agent 本机边界 403 |
| Dell HTTPS | 首轮 27 项通过；补充规范别名根入口正文后 28 项通过，包含 HTTP 阶段同类鉴权及资源断言 |
| CA 与 IP 身份 | 请求级显式 CA 验证成功；两个入口的叶证书 SAN 均包含目标 LAN IP；默认系统 CA 拒绝内部 CA，未安装系统根证书 |
| 错误 SNI | TLS 握手被拒，保存真实 SSL 错误；不将发证前握手失败误写成取得证书后的主机名校验失败 |
| 明文旁路 | Dell 对旧 HTTP 别名、manager 后端和静态实例明文端口均不能取得 HTTP 响应；同阶段 HTTPS 资源及管理入口正常 |
| Debian LAN 地址 HTTPS | 27 项断言通过；与 Dell 的独立观察点分开留证 |
| 入站规则差分 | 只新增三条指定来源/目标/端口的 Windows 规则；Hyper-V ActiveStore 出现同名合并规则。Windows 默认策略、Hyper-V VM 设置与 portproxy 均与放行前相同 |

本轮实际系统读数为 Windows `10.0.28020.3292` / WSL 包 `3.0.4.0` / Debian 13 / mirrored，不沿用历史 Windows 补丁版本。

结果证明：在本机启用 AllowHostPolicyMerge 的条件下，限定来源的 Windows 入站规则足以使同一 Dell 物理 LAN 路径从超时变为成功，无需单独添加 Hyper-V 规则。Windows 规则合并到 Hyper-V 的过程也意味着，不能据此断言唯一的内核丢包层或 WFP 过滤器 ID。默认 Block 和物理 LAN 本身故障不能混为一谈。

外部观测第一次使用 `index.html` 子路径，被产品按“与当前展示的别名根入口不一致”保守记为 stale。保留首次日志，随后实际从 Dell 校验规范入口正文，再使用完全匹配的 URL 记录，达到 externally-verified / stale=false。未用人工改写成功标记代替真实外部证据。

## 撤销、缺陷修复与最终清理

用户执行 Remove 后，Windows 三条临时规则、Hyper-V 同名合并规则及归属文件均消失。完整规则、Windows 默认策略、Hyper-V VM 设置和 portproxy 与放行前相同。

先保留服务运行采证：Dell 全部请求无 HTTP 响应，TLS 入口 TCP 超时；同阶段 Debian 按 LAN 地址的 28 项仍通过。随后产品 CLI 的 stop、gateway off、manager off 均退出 0。包括 Caddy admin 在内的本轮监听全部释放，原四个业务监听与 PID 均保持，无新增容器。本轮隔离数据、源码副本、CA 和首次失败记录保留审计，不残留运行服务或临时网络规则。

**BUG-1016 实机复现**：旧冻结版本撤销规则后仍显示 externally-verified / stale=false。解析器只接受 WSL GUID，遗漏 VMCreatorId=Any 的宿主合并规则，导致放行撤销未进入网络指纹。修复为存在 WSL 设置时同时保留显式 Any 规则，继续排除其他 VM 的专属规则和混合环境的无标识规则。新增回归覆盖 merged 规则撤销、历史证据保留、Any 大小写、外来 VM 隔离及缺失 WSL 设置的边界。

验证严格区分两层：HTTP20/HTTPS28 正向和撤销后的 Dell 失败/guest 健康来自原冻结版本真实物理 LAN；修复后的“放行已验证 → 撤销 stale”转换来自本轮实际 Windows 规则快照回放。新修复源码也在本机 WSL 新鲜采集中确认旧历史观测已降级为 candidate-unverified / stale=true。没有重开 Windows 入站规则，因此不声称修复版做过新一轮独立 LAN 正向。

`closure-verification.json` 的 18 项收尾断言全部通过。正向阶段冻结的 162 份产品文件中，收尾仅 `wsl_network.py` 发生上述有意修复，其余 161 份一致；既有用户改动均保留。Ruff 通过。相关六模块回归的首次结果为 311 passed / 1 failed：失败是 Windows 子进程缺少源码 PYTHONPATH，未找到包；原始 XML 保留，补正确源码路径后重新执行为 **312 passed / 0 skipped / 0 failed**。未跑整项目全量门禁，未提交发布 Git 或操作远端 issue。

## 2026-10-30 BUG-1016复核与原第2项最终收尾

用户要求修复BUG-1016并完成原第2项收尾。本轮保留并复核远端已同步的修复，不重复改写正确逻辑：存在WSL设置时接纳显式Any creator合并规则，其他VM/无标识/缺失WSL设置边界继续隔离。

| 检查 | 结果 |
| --- | --- |
| 修复前负对照 | 仅在独立Python进程内恢复旧解析条件，不改工作区文件；3项回归按预期失败，确认用例能捕获真实缺陷 |
| 修复后专项 | 合并规则撤销、Any/ANY、其他VM隔离及缺失WSL设置，共4项通过 |
| 原始Windows规则回放 | 放行时externally-verified、撤销后candidate-unverified/stale=true、历史证据保留、匹配规则恢复后指纹一致及原18项清理证据，5项检查均通过 |
| 独立代码审查 | 未发现重要问题，独立复跑test_wsl_network.py共101项通过 |
| 当前工作区完整门禁 | bash tests/gate.sh：4755 passed / 23 skipped，118.71秒；Ruff、工作区与暂存区diff-check通过 |

新证据单独保存在 `design/5-checkpoints/2026-10-30-bug1016-closure/`，包含旧逻辑失败记录、独立回放脚本和JSON结果；不覆盖昨晚采证文件。全量门禁包括当前既有未提交改动，不宣称纯HEAD或已发布版本。

**收尾结论**：BUG-1016修复验证完成；结合昨晚Dell HTTP20/HTTPS28正向、真实撤销负向、guest健康及规则/服务清理证据，原第2项/N2对应范围完成收尾，CHK-521维持已完成。未再次操作Windows网络或重新开放LAN，本轮修复后转换属于真实规则回放，不能表述为修复版新一轮物理LAN正向已测。该范围不再阻塞原顺序转入第3/4项及后续独立工作；PLN-054的其他组合/真实退化/宿主恢复、完整HTTPS浏览器/IPv6与统一快照三机复验继续待办，CHK-520的Tailscale跨节点验收也不由本项替代。代码保持待提交，未发布或操作远端issue。
