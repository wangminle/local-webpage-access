# IT-Tools：Mac 与 Ubuntu 浏览器深链验收（2026-10-28）

## 结论与验收边界

Mac 与 haier-ubuntu 均完成了 IT-Tools 的两种受支持部署配置验收：根路径编译产物的端口直连，以及显式 `BASE_URL=/chk480/` 编译产物的路径别名。四配置均在真实 Chromium 中通过确定性编码、解码、history 深链直接进入、刷新后保持路由及再次操作，浏览器加载的 CSS/JS 均为 200、正确 MIME，正文 SHA-256 逐条匹配各自构建产物，无加载失败。

按此前 Debian 的“同一项目、分别使用根路径直连与带 base 别名实例”口径，本轮已补齐 Mac/Ubuntu 的 CHK-480 待验项，可以完成 CHK-480。这里不表示同一份带别名 base 的产物同时兼容端口根路径；该额外尝试在两机均失败，完整保留在证据中，属于现有文档声明的边界。

本报告只覆盖冻结的 IT-Tools **new** 版本 `2024.10.22-7ca5933` 的浏览器业务操作；old 归档重新下载并校验哈希，未重新部署或进行浏览器业务验收。Ubuntu 浏览器观察位置是 Mac，经专属 SSH 隧道到 Ubuntu 回环入口，**不计作局域网网络验收**。

## 冻结输入与隔离方式

- 产品基线：HEAD `11027c1b05310e60d574fe5f2ec68c97b83fd2b1` 加当前 WIP，版本 `0.9.17`。复用主任务构建的 wheel；161 个产品文件（含 `pyproject.toml`）逐项比对源哈希一致。结束时重新核对仍匹配。
- 产品源码归档 SHA-256：`75cfaa7d7893ca403e30f36bfe7ef2b176507fede5fd53b2e81d989004eefcd4`。
- wheel SHA-256：`bf9fe6cdcb705497249be5a4d3e426a753d59a6a5d9b3941ccdce85faefa3dc9`。两机传输后哈希一致；wheel 解包到本轮 `site/`，通过 `PYTHONPATH` 加载，复用已有 Python 3.13 venv 的依赖，不修改生产环境包。
- IT-Tools old commit：`e876d0360812eab1255e414102a44d65f7c1bfaf`，归档 SHA-256 `9d1e74c7cf1da1f485bd3d573226ab7fae780830abbdc918a5dcfbb517ac2278`。
- IT-Tools new commit：`5732483fc24a6e6818839060bdf3cc7d9d324b9f`，归档 SHA-256 `b79c6241dc58019bb8a053a713ab454578506dab7d44699fedc6d6544e443b77`。
- 两机专属目录均为 `/tmp/lwa-chk480-20261028/`（Mac 实际规范路径 `/private/tmp/...`）。先盘点服务，然后以独立配置执行 LWA `init`、`import --from-dir`、`configure`、`start/rebuild`、`alias set`。均识别为 `frontend-static / shared-static`，使用各机 Node 24.19.0、corepack pnpm 8.15.3、Caddy。manager 关闭，实例绑定回环；无容器构建。
- 安装按上游 `pnpm-lock.yaml --frozen-lockfile`，新工作区首次安装缓存为空，后续重建与另一实例复用本轮工作区 `run/pkg-cache/`，全程重新执行产品构建路径，没有复用生产 IT-Tools 产物。两机已有 corepack pnpm 8.15.3 工具缓存参与，既有 Python 依赖与 Caddy 可执行文件只读复用。

完整逐文件清单与归档见 [source-freeze.json](../5-checkpoints/2026-10-28-it-tools-mac-ubuntu/source-freeze.json)、[product-source.tar.gz](../5-checkpoints/2026-10-28-it-tools-mac-ubuntu/product-source.tar.gz)、[freeze-verification.json](../5-checkpoints/2026-10-28-it-tools-mac-ubuntu/freeze-verification.json)；两机传输哈希见 [transfer-hashes.json](../5-checkpoints/2026-10-28-it-tools-mac-ubuntu/transfer-hashes.json)。上游归档也保留在专属证据目录的 `input-old.tar.gz` 与 `input-new.tar.gz`。

## 浏览器结果

按 webapp-testing 技能，先等待 `networkidle` 侦察 DOM、记录截图与文本框，再选择实际 placeholder locator 操作。使用 Mac 安装的 headless Chromium 1243 与 bundled Playwright，新浏览器 context 禁止 Service Worker，避免旧 SW 产物介入。每配置从完整深链直接打开，输入 `hello lwa chk480`，断言编码为 `aGVsbG8gbHdhIGNoazQ4MA==`；再将该串解码，断言返回原文。reload 后再次执行同一对转换。

| 宿主 / 配置 | 实例 | 浏览器实际 URL | 编解码 / 刷新后重做 | JS / CSS 响应 | 资源正文匹配 |
| --- | --- | --- | --- | --- | --- |
| Mac 根路径直连 | `chk480-direct`，`BASE_URL=/` | `http://127.0.0.1:34942/base64-string-converter` | 通过 | 16 JS + 2 CSS，全部 200、正确 MIME | 18/18 SHA 匹配 |
| Mac 路径别名 | `chk480-it-tools`，`BASE_URL=/chk480/`，alias `chk480` | `http://127.0.0.1:34940/chk480/base64-string-converter` | 通过 | 同上，路径全部带 `/chk480/` | 18/18 SHA 匹配 |
| Ubuntu 根路径直连 | `chk480-direct`，`BASE_URL=/` | `http://127.0.0.1:34962/base64-string-converter` → SSH → Ubuntu `127.0.0.1:34942` | 通过 | 16 JS + 2 CSS，全部 200、正确 MIME | 18/18 SHA 匹配 |
| Ubuntu 路径别名 | `chk480-it-tools`，`BASE_URL=/chk480/`，alias `chk480` | `http://127.0.0.1:34960/chk480/base64-string-converter` → SSH → Ubuntu `127.0.0.1:34940` | 通过 | 同上，路径全部带 `/chk480/` | 18/18 SHA 匹配 |

响应数包括进入与刷新两轮。所有配置的 reload URL 均与 reload 前一致，HTTP 200。IT-Tools 上游将文本框状态存于 Vue `ref`，刷新后输入/结果正常清空；刷新后工具界面仍在，再次编码和解码均准确。本轮验收的是“路由保持且可再操作”，不声称刷新持久保存输入。四配置没有 request failure、page error 或 console error；资源正文不含回退 HTML。

主断言见 [browser-content-verification.json](../5-checkpoints/2026-10-28-it-tools-mac-ubuntu/browser-content-verification.json)、[browser-verification-final.log](../5-checkpoints/2026-10-28-it-tools-mac-ubuntu/browser-verification-final.log)。各 `mac/ubuntu-{alias,root-direct}.json` 保存逐阶段输入、输出、URL、每个资源的状态/MIME/正文开头/hash；对应 `*-convert-after-reload.png` 为刷新后再次转换截图。`*-chk480-*/public-hashes.json` 为构建产物基准。

## 保留的失败、原因与修正

1. 采集 Mac 进程清单时进程名存在不可解码字节，采集脚本初次失败；改为容错解码，保留 `setup-initial-failure.txt`。初始测试端口池 `34941..34949` 触发配置范围校验，保留 `mac-init-attempt1.log`，扩至 `34941..34959` 后初始化成功。这两项属于验收脚本配置问题。
2. Ubuntu 首轮 pnpm 安装在 npmmirror 的 `prosemirror-collab-1.3.1.tgz` 超时，保留 `ubuntu-start-attempt1.log`、`ubuntu-build-attempt1.log`。对照官方 registry 可返回 200 后，仅通过本轮实例 `buildEnv` 将 registry 改为 `https://registry.npmjs.org`，重试安装及后续构建通过。Mac 原 npmmirror 首轮安装成功。没有改系统代理、宿主防火墙或信任。
3. 初次仅设置 `VITE_BASE=/chk480/`，上游 `vite.config.ts` 实际读取 `BASE_URL`，所以构建仍为 `/assets/...`。两机 `alias set` 按预期拒绝该产物；`*-alias-attempt1.log` 保留守卫原文。改用产品 `configure --build-env BASE_URL=/chk480/` 并 `rebuild` 后，`alias set` 活验证通过。后续若使用 `--follow-alias-base`，产品已按文档同时注入 `VITE_BASE/BASE_URL/PUBLIC_URL`，本轮未修改源码绕过守卫。
4. **额外边界复现**：在带 `/chk480/` 编译 base 的同一个实例上，访问其 hostPort 根深链：Mac `:34941/base64-string-converter`，Ubuntu 通过 SSH 的 Mac `:34961/base64-string-converter`。HTML 返回 200，但 `/chk480/assets/*.js`、CSS 经直连静态根无法命中文件，SPA 回退成 `text/html`，浏览器无文本框并报资源 MIME 错误。两机 `*-same-instance-direct.json`、`*-failed.png` 保留完整失败，不能用首页 200 判通过。

第 4 项源于静态产物的编译 base 与端口根路径不一致；Caddy 直连站点以 `public/` 为根并执行 SPA fallback，路径别名入口另行去前缀。它与 [known-limitations.md 的路径别名分档](../../docs/known-limitations.md) 已声明的 C 类“要双入口全完整等”边界一致，未据此新增产品缺陷。`docs/path-alias.md` 不存在；相关正式约定集中在 `known-limitations.md`。本轮以各自受支持的根 base 与别名 base 两个实例验收，未把同实例失败替换或删除。建议继续保持该口径；如未来要求同一编译产物同时完整支持两种路径，应另行设计动态 base 或主机名别名支持。

## 清理与生产保护

两机分别通过本轮 wheel 执行 `stop chk480-it-tools`、`stop chk480-direct`、`gateway off`，六条命令退出码均 0；测试端口 34940/34941/34942 均确认关闭，然后删除各自 `/tmp/lwa-chk480-20261028/`。Ubuntu 仅为收回清理日志短暂创建的 `/home/user/lwa-chk480-evidence-20261028/` 也已删除。Mac 的 SSH 进程已 SIGTERM，34960/34961/34962 转发端口均关闭，浏览器正常关闭。

Mac 原生产 daemon/manager/gateway supervisor/Caddy 的 PID 2415/2424/2426/2787 保持运行，启动时间未变；Ubuntu 原 manager/Caddy PID 3838254/3838272 保持运行，两只生产容器 ID 与名称保持不变，仍为 Up 4 days。清理前后生产 `local-web.yml` 和 Caddyfile 的 SHA 均一致。本轮没有创建镜像，没有修改产品源码、生产配置、生产容器、宿主信任/防火墙/全局代理、分支、提交、推送或外部 issue。任务台账由主任务统一同步。

证据见 [mac-cleanup.json](../5-checkpoints/2026-10-28-it-tools-mac-ubuntu/mac-cleanup.json)、[ubuntu-cleanup.json](../5-checkpoints/2026-10-28-it-tools-mac-ubuntu/ubuntu-cleanup.json)、[ssh-tunnel-cleanup.json](../5-checkpoints/2026-10-28-it-tools-mac-ubuntu/ssh-tunnel-cleanup.json)、`mac-production-pids-after.log` 与 `ubuntu-after-cleanup.log`。全部证据只在 `design/5-checkpoints/2026-10-28-it-tools-mac-ubuntu/`。
