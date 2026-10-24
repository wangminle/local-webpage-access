# Win11 mirrored / Debian 十一项目首次安装验收

实测：2026-10-23～24（Asia/Shanghai）。关联 CHK-491、PLN-054，承接 OPS-221 / CHK-490。结果为 **7 项安装及基本操作通过、1 项部分通过、3 项安全拒绝或形态支持边界**，不是 11 项全部部署成功。

已按用户要求发布 [完整验收与问题跟踪 #151](https://github.com/wangminle/local-webpage-access/issues/151)，并分别关联下文四项产品问题；没有提交或推送本地源码。

## 环境、源码与范围

Win11 Pro Insider Preview 10.0.28020.3262，WSL2 架构、WSL 包 3.0.4.0，Debian 13.7（trixie），实际 `wslinfo --networking-mode=mirrored`。Debian 原生 Docker Engine 29.9.2、Compose 5.6.1，普通用户直接使用 Linux socket。Caddy 2.11.7；宿主工具链为经官方校验的 Node 24.22.1 / 22.23.4、Corepack 0.36.0。不要求启用 Docker Desktop 集成。

本轮 11 项使用 HEAD `02696f90cb36c5855ab5b9d89c8f6d51c905ddcd` 加当时待提交工作树的独立 Linux 副本，177 个文件的冻结指纹：

`ae3b3d27ff94fad37062941621422a53ab13cffc5bc077dda3ffe7ad68a598ce`

11 个 old.tar.gz 均按既有锁清单逐项 SHA-256 校验，再从原始归档解压，生成保留符号链接属性的 ZIP；不使用历史污染目录。Astro / Flask 分别选 portfolio / tutorial 子目录，其他样本保留完整仓库。版本身份见 [样本清单](../../tests/evals/evals-readme.md)。构建并发为 1，使用隔离工作区、实例 ID、端口和测试数据。

只验证固定旧源码快照的首次安装、页面和所抽查本地资源、下表基本操作；没有执行全部 old/new 升降级、切源、失败保旧、三平台相同快照或宿主恢复矩阵。成功项包含显式环境准备、应用配置与下载重试，不能理解为所有环境默认开箱通过。

## 逐项结果

| 样本 / 固定源码身份 | 安装结果 | 实际验证与限制 |
| --- | --- | --- |
| A01 2048 / cf01ca7e8bf6 | 通过，静态托管 | HTML 与 8 个抽查资源 200；Windows 浏览器方向键计分 0→12，刷新保留分数与棋盘。另补真实 HTTPS 别名 HTML/CSS/JS 与显式 CA 正向、默认信任拒绝反向验证 |
| A02 IT-Tools / v2024.5.13-a0bc346 | 经下载重试通过，宿主构建静态站 | 固定 pnpm 8.15.9；首页与 8 个资源 200。Windows 浏览器 Base64 输入 WSL mirrored，输出 V1NMIG1pcnJvcmVk；工具深链刷新仍正确加载。不声称输入刷新后持久化 |
| A03 Astro portfolio / astro@7.3.7 源码快照 | 通过，宿主构建静态站 | 构建约 585 秒；首页、样式、Work / About 路径 200，浏览器 Work 导航和刷新通过。样例依赖无精确锁，本次实际解析 Astro 7.4.2，不算 7.3.7 框架运行或升级验收 |
| A04 Flaskr / Flask 3.1.2 仓库 tutorial | 通过，真实 Python 容器 | Python 3.13.17、Gunicorn 22.0.0，实际 Flask 3.1.3。自动初始化 SQLite；注册、登录、创建文章、产品 restart 后读取及 Windows 浏览器查看通过。未把框架版本未锁定写成 3.1.2 运行通过 |
| A05 Uptime Kuma / 2.5.5 | 经构建代理重试通过，真实 Node 容器 | Node 24.22.1。源码前端构建、初始化页及抽查资源 200；通过应用流程选择 SQLite、Socket.IO 创建测试用户与本容器回环 HTTP 监测，收到 UP 心跳。产品 restart 后登录、同一监测 ID 保留；没有外部通知或完整监测类型验收 |
| A06 Linkding / v1.46.2 | **部分通过**，真实 Python 容器 | 代理重试后启动，迁移、登录 HTML、书签 API 创建/搜索及 restart 数据保留通过。但 8 个资源抽查中 4 图标、2 CSS 为 404，静态构建与生产托管不完整，登记 BUG-997 / [#149](https://github.com/wangminle/local-webpage-access/issues/149) |
| A07 Excalidraw / v0.18.0 | 经兼容 Node 与缓存重试通过，宿主构建静态站 | 明确使用 Node 22.23.4，不忽略 engines。冷安装 1800 秒超时，缓存重试约 116 秒成功；首页与 7 个抽查资源 200。Windows 浏览器画矩形、写文字，刷新后两者保留。只验浏览器本地保存，未验协作后端、导出或远程协作 |
| A08 Homarr / v2.4.0 | **安全拒绝 / 支持边界** | 原源码 ZIP 符号链接被 zip_symlink 拒绝；未改源码的文件夹另行导入成功但状态 pending、start 拒启。不绕过守卫，不把复杂 Bun/Turbo 应用算成已部署 |
| A09 HedgeDoc / 1.11.1 | 经应用配置通过，真实 Node 容器 | 生成镜像采用 Node 22.23.4。配置 SQLite 与实际外部入口；首页和 8 个资源 200。HTTP 创建/下载笔记、产品 restart 后原文保留；Windows 浏览器编辑再刷新也保留。旧快照应用环境变更需 Compose 重建，命中 BUG-992；后续另冻修复快照做真实产品 restart 对照通过 |
| A10 FastAPI Template / 0.11.1 | **安全拒绝 / 支持边界** | 原 ZIP 中 4 个符号链接被拒；原始文件夹可导入但 pending 拒启。不只部署 frontend 来代替完整多服务应用 |
| A11 Memos / v0.30.0 | **支持边界，pending 拒启** | 完整 Go/前端仓库未被确认成可部署形态，导入成功后 start 正确拒绝；没有把源码壳或前端壳作为完整服务成功 |

8 个实际启动项目均另从 Windows Python 直连 localhost 读取首页，返回 200；这是宿主访问验证，不替代独立 LAN。资源检查为上表实际抽查数量，不是站点全部资源爬取。

## 构建问题与恢复

A02 首次 Corepack 下载连接中断，A05 国内源直连停流，A06 固定依赖直连慢，A07 冷 Yarn 下载超时；保留首轮失败及重试日志。A05/A06 首轮为核对进程归属后人工中断，不算产品超时。容器构建通过仅监听 Docker bridge 的临时 TCP 转接使用已有代理；显式 NO_PROXY 不再让国内源绕过代理。A02/A05/A06/A07 重试 start 分别约 74、487、58、116 秒。A09 首轮 start 约 615 秒。

应用配置、安全边界、未精确锁定依赖以及所有采证脚本错误逐项见 [本轮全部问题汇总](../4-issues/2026-10-24-mirrored十一项目构建问题汇总.md)。脚本错误已纠正并保留原日志，不计产品 Bug；不发布令牌、完整环境文件、私有网络或第三方源码。

## 修复后独立实机复验

执行期间工作树新增了其他会话完成的修复。原始十一项目副本保持不变；2026-10-24 另冻 src / pyproject / uv.lock，复验快照指纹为：

`7588b7359f2fb613ddd4fe7c3f41f7bd25f03c108ad2a33bbc2e6f6b878480be`

| 缺陷 | 本轮另冻修复快照的实机结果 |
| --- | --- |
| BUG-990 / [#147](https://github.com/wangminle/local-webpage-access/issues/147) | 新隔离实例首次 CLI start 已进入真实 compose build 的受控 sleep 钩子，cancel-build 返回已取消，start 非零退出、已采集的专属 compose 进程全部退出。不是仅替身测试 |
| BUG-992 / [#148](https://github.com/wangminle/local-webpage-access/issues/148) | HedgeDoc 环境测试键新增 v1、修改 v2、移除均经产品 restart 生效，触发容器重建；不变文件再 restart 保持容器 ID。镜像 ID、挂载和此前浏览器编辑笔记均保留。实机只移除测试键，未删除必需业务环境文件 |
| BUG-989 / [#150](https://github.com/wangminle/local-webpage-access/issues/150) | 真实 Windows 互操作采集得到 hyperv_firewall available/source=active、policyStore=active、defaultInbound=Block 及生效规则；只读验证，不修改网络、不宣称 LAN 入站已修好 |

这三项台账已有修复，本轮只补独立实机证据；GitHub issue 保留待提交/发布的交付跟踪。Linkding BUG-997 仍待修复。本轮没有重跑全量自动化门禁，不把台账中其他修复会话的全门禁结果算成本轮执行；也没有用这三项复验宣称新快照全部十一项目重跑通过。

## 收尾与剩余范围

已停止本轮 8 个应用、Caddy 和 manager，核对原实例/网关/管理/TLS 端口均关闭；关闭临时构建代理配置与 bridge 转接，移除本轮已退出的 4 个容器及其专属 Compose 网络。没有全局 prune 或删除数据卷；保留原生 Engine（active）、镜像、依赖缓存、隔离源码/工作区和业务数据。原始十一项目冻结文件哈希复核未改变。

原始日志与结构化结果位于本地忽略目录 `design/5-checkpoints/2026-10-23-m11-eleven-install/`，含逐项 result/business、Windows localhost、TLS、修复快照、取消/环境/防火墙复验、清理前清单及清理后端口/容器/网络证明。

本平台已经能够实际构建和运行多类真实应用，无 Docker 阻点已解除。PLN-054 仍保持进行中：独立 LAN 正向、其他 Windows/WSL 包/NAT/mirrored 组合、权限/未知模式等实机退化及休眠/WSL 重开/地址变化/Windows 重启自启仍须按原矩阵验证。本轮只覆盖当前这一套 Win11 Insider / WSL 3.0.4 / Debian mirrored 环境。
