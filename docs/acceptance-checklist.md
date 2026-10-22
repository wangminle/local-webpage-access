# V1 验收清单（WBS-29）

本清单覆盖 `lwa` V1 端到端验收的核心子任务（WBS-29.01~18），并增补浏览量 / 冗余清理抽查项（29.19~20）。
其中**不依赖 Docker 守护进程**的部分已由自动化测试
`tests/scripts/test_e2e_acceptance.py` 覆盖（见下表「自动化」列）；
**依赖真实 Docker** 的容器构建/启动部分需按本清单手工验收。

## 验收前置

```bash
# 安装
pip install -e ".[dev]"

# 确认 Docker 可用
docker version
docker compose version

# 准备一个干净工作区
lwa init --workspace ./acceptance-ws
cd ./acceptance-ws
```

样例 zip 由 `tests/fixtures` 生成：

```bash
python3 -c "from tests.fixtures import build_all, SAMPLES; build_all('acceptance-ws/inbox'); print(list(SAMPLES))"
```

执行后 `inbox/` 内会生成 6 个 zip：
`static_html.zip`、`vite_react.zip`、`node_express.zip`、
`fastapi_sqlite.zip`、`build_failure.zip`、`pending_unknown.zip`。

## 验收项

| WBS | 项 | 自动化 | 手工步骤 | 通过标准 |
| --- | --- | --- | --- | --- |
| 29.01 | 干净工作区 init | ✓ `test_e2e_init_creates_clean_workspace` | `lwa init --workspace ./ws` | 生成 `local-web.yml`、`registry/local-web.db`、`apps/`、`inbox/`、`skills/`（**20** 个 SKILL.md） |
| 29.02 | 导入静态 HTML | ✓ `test_e2e_static_html_import_and_structure` | `lwa import inbox/static_html.zip` | `apps/static-html/` 出现，`local-web.json` 的 `kind=static` |
| 29.03 | 静态目录结构 | ✓ 同上 | 检查 `apps/static-html/{source,current,logs,data}/` | 四个子目录齐全，`current/index.html` 存在 |
| 29.04 | 静态 HTTP 可访问 | ✓ `test_e2e_static_html_accessible_via_http` | `lwa start static-html`，浏览器访问分配端口 | 返回 HTML 内容，`lwa status` 显示 `running` |
| 29.05 | 导入 Vite/React | ✓ `test_e2e_vite_react_detected_as_frontend` | `lwa import inbox/vite_react.zip` | 识别为 `node` + `frontend-static` |
| 29.06 | 前端构建产物 | — | `lwa start vite-react` | 宿主机执行 `npm run build`（`hosting.py`），`dist/` 生成并同步至 `public/`，静态托管可访问 |
| 29.07 | 前端形态正确 | ✓ 同 29.05 | 检查 `local-web.json` | `servingMode=shared-static`，有 build 命令 |
| 29.08 | 导入 Node/Express | ✓ `test_e2e_node_express_detected_and_compose_generated` | `lwa import inbox/node_express.zip` | 识别为 `node` + `backend-container`，`docker/compose.yaml` 生成 |
| 29.09 | Node 容器构建启动 | ✓ 手工验收（2026-07-07） | `lwa start node-express` | `docker compose up` 成功，`lwa status` 显示 `running`，HTTP 可访问 |
| 29.10 | 导入 FastAPI+SQLite | ✓ `test_e2e_fastapi_sqlite_detected_and_compose_generated` | `lwa import inbox/fastapi_sqlite.zip` | 识别为 `python`，`docker/compose.yaml` 含 `../data:/app/data` |
| 29.11 | FastAPI 容器构建启动 | ✓ 手工验收（2026-07-07） | `lwa start fastapi-sqlite` | 容器构建并启动，HTTP 可访问 |
| 29.12 | 数据持久化 | ✓ 手工验收（2026-07-07） | 写入数据 → `lwa stop` → `lwa start` → 再读 | 数据不丢失（`data/` 卷保留） |
| 29.13 | start/stop/restart | ✓ `test_e2e_start_stop_restart_static` | `lwa stop`、`lwa restart` | 状态在 `running`/`stopped` 间正确切换 |
| 29.14 | logs/status/stats | ✓ `test_e2e_logs_status_stats_queryable` | `lwa status`、`lwa logs <id>`、`lwa stats` | 三类信息均可查询且非空 |
| 29.15 | 管理页与 CLI 一致 | ✓ `test_e2e_manager_api_matches_cli_status` | `lwa manager on` 或 `lwa manager start` 打开管理页，对比 `lwa status` | 实例列表与状态一致 |
| 29.16 | doctor 排障 | ✓ `test_e2e_doctor_diagnoses_instance` | `lwa doctor`、`lwa doctor <id>` | 环境检查全 ok/warn；实例诊断无 fail |
| 29.17 | failed/pending 展示 | ✓ `test_e2e_failed_and_pending_display` | 导入 `build_failure.zip`、`pending_unknown.zip`，查管理页 | failed 显示错误原因；pending 显示在待处理区 |
| 29.18 | 记录结果与问题 | — | 填写下表 | 完成本清单 |
| 29.19 | 浏览量 API/UI（IMP-024～028） | ✓ `test_pageviews.py` / `test_manager_api.py` | 管理页列表见浏览量列；`GET /api/pageviews` 有汇总；详情含 `uniqueIpList` | page 级 hits/source；Caddy 无别名静态直连端口可计入；有别名容器走 Caddy 源 |
| 29.20 | 冗余清理（IMP-019） | ✓ `test_lifecycle.py` / `test_manager_api.py` | 重复导入同 zip 后 `lwa remove --redundant` 或管理页批量删除 | 每组仅保留最早者 |

## 验收标准（来自 WBS-29）

1. 四个核心样例（static_html、vite_react、node_express、fastapi_sqlite）能完整完成 **导入 → 运行 → 展示**。
2. `stop` / `restart` 不会丢失数据。
3. 管理页展示与 CLI `lwa status` 一致。
4. 失败路径（build_failure、pending_unknown）可解释、可排障。

## 验收记录

| 字段 | 值 |
| --- | --- |
| 验收日期 | 2026-07-07 |
| 验收人 | fenix-wangminle |
| 环境 | macOS 26.6，Python 3.13.13，Node 24.16.0，Docker Desktop 4.55.0 / Engine 29.1.3，Docker Compose 2.40.3 |
| 自动化结果 | `tests/scripts/test_e2e_acceptance.py` 全部通过（11/11） |
| 全量回归 | `python3 -m pytest`：734 passed / 4 skipped；`LWA_RUN_DOCKER_TESTS=1 python3 -m pytest tests/scripts/test_docker_integration.py -q`：4 passed |
| Docker 手工验收 | 已通过 29.09 / 29.11 / 29.12；临时工作区 `/tmp/lwa-acceptance-oMtPaK`；Node 端口 18002，FastAPI 端口 18003，容器内 `/app/data/persist.txt` stop/start 后仍为 `persisted` |

## 问题清单

| 编号 | 描述 | 影响 | 状态 |
| --- | --- | --- | --- |
| — | 暂无阻塞性问题 | — | — |

> 如手工验收发现新问题，请在此表追加，并在对应代码/文档中修复后回归。

## Full Profile / 平台 / 删除 / 更新通道补强验收（033.13 · 035.06 · 036.08 · 063.13）

主路径代码已落地；下列为**实机**补强项（本机单元/集成测试不能替代）。验收时勾选并填「验收记录」附录。

### 063.13 — 一键 GitHub 更新通道实机（IMP-063 / V0.8.0）

自动化测试（`tests/scripts/test_update_source.py`，41 例）全部基于临时 bare remote 夹具；以下为**真实远端 + 真实代理环境**的实机验收（§14 事故机形态复现）。

| 项 | 手工步骤 | 通过标准 |
| --- | --- | --- |
| A | 事故形态环境（克隆安装、落后 ≥2 个版本、`https_proxy` 指向 mihomo 等代理）执行一条 `lwa update` | 报告含 `sourceUpdate ok（V0.7.x → V0.8.0）`；全程无人工 git 操作；Runtime 步骤由 continuation 完成，版本一致 |
| B | 已是最新时执行 `lwa update` | `sourceUpdate skipped（已是最新）`，其余步骤照旧 |
| C | 断开代理/网络后执行 `lwa update` | `sourceUpdate warning`，本地代码完成全部 Runtime 步骤，退出码 0 |
| D | `lwa update --check`（未 init 工作区的目录） | 输出 SourceCheckReport；不改工作树与 Runtime；退出码 0/1/2 语义正确 |
| E | kill 一个 enabled 服务后 `lwa update`（IMP-059 联动） | 报告标注「意外未运行（中断约 X），已恢复」；accessReview 转绿 |
| F | 两个 `lwa update` 并发 | 后者 fail-fast「更新锁被占用」，不触碰 pip/进程/registry |

#### S4-02 实机记录（2026-10-15）

对照清单 A–F。版本目标是已发布的 V0.9.4，不是计划稿里的 V0.10.0。代理是本机 v2rayN/xray 的 `socks5h://127.0.0.1:10808`（清单写的 mihomo 用同等 SOCKS 代理代替）。验收克隆在 `/tmp/lwa-s4-chk285`，独立 venv，工作区 `/tmp/lwa-s4-ws`，不接管现网 launchd。

| 项 | 结果 |
| --- | --- |
| A | 从 `d757384`（V0.9.1-Build4310）一条 `lwa update --remote origin --ref main` 快进到 `c39d3f8`。报告：`sourceUpdate` 已快进 origin/main：V0.9.1 → V0.9.4；日志有 continuation；结束后 `lwa version` 为 V0.9.4。退出码 0。 |
| B | 再次更新：`sourceUpdate` 已是最新（origin/main @ V0.9.4），pip / syncSkills / accessRefresh / accessReview 照旧。退出码 0。 |
| C | 仅把 GitHub 的 git 代理指到 `127.0.0.1:9`。`sourceUpdate` 为 warning（fetch 连接失败），工作树仍停在 `c39d3f8`，其余 Runtime 步骤完成。退出码 0。 |
| D | 在非工作区目录执行 `lwa update --check --repo <克隆>`：SourceCheckReport，关系 equal，ahead 0 / behind 0。退出码 0。检查后克隆工作树无改动。 |
| E | 现网 runtime 的 manager 被结束后执行 `lwa update --no-pull --skip-pip`。`restartManager`：管理页意外未运行，中断约 65 秒，已恢复（version=V0.9.4）。`/api/health` 200，新 pid 93558。 |
| F | 两个更新同时启动。后者 `sourceUpdate` failed：更新锁被占用（repo），未进入 pip。 |

临时工作区的 doctor 总体 FAIL（端口池被现网占用、manager 因 17800 已被占用而未能启动、本工作区没有自己的 Caddy master）。这不是更新通道失败。现网 `lwa update --no-pull` 刷新 13 个实例、漂移 0，`lwa status` 13 行均为 running；`http://127.0.0.1:8080/prd-review/` 与管理页 `/api/health` 均为 200。现网 doctor 0 失败 / 1 警告（`gateway_tls` 明文旁路，原有警告）。access review 0 失败 / 4 警告（prd-review-v035、backend、patent-disclosure-workflow-v1、funasr-workbench），演练未对它们 rebuild，因此 E 的「accessReview 转绿」这一句没有达到。

**E 项口径调整（2026-10-15，CHK-401 收口）**：上述 4 条 WARN 已逐条实测定性——prd-review-v035、funasr-workbench、patent-disclosure-workflow-v1 为 access review 探测口径误报（仅 GET 探测、字面量正则盲区、疑似分支推理缺陷；登记 BUG-843，排 V0.9.6 修复）；backend 为真缺陷，根因在前端源仓以 `VITE_BASE=/` 预构建的绝对路径产物（入口 HTML 引用 `/assets/index-*.js`，别名下白屏），属 issue #61 范围——LWA 生成的 Python 模板 Dockerfile 无前端构建阶段，任何 `lwa rebuild` 只会重新拷贝同一份产物。E 项按「0 失败 + WARN 逐条定性」收口，backend 保持 `aliasGuardResult=failed` 如实记录。

### 033.13 — Full Profile Ubuntu / systemd 完整链路

| 项 | 手工步骤 | 通过标准 |
| --- | --- | --- |
| A | Ubuntu 22.04+/Debian 12+ 或 WSL2 Ubuntu（标明 Desktop vs Engine）执行 `lwa setup --full`（可 `--resume`） | 组件安装/接管完成；不得把「仅组件安装成功」写成 Full ready |
| B | 预启系统 `caddy.service` 后再 `setup --full` | LWA 接管 Caddy owner=`lwa_service_user`；系统 unit 被 disable 或明确不再冲突 |
| C | `systemctl --user enable --now lwa-daemon lwa-manager lwa-gateway`（或等价 autostart） | 三 unit 启动；`lwa doctor --profile full` overall=ready |
| D | 设别名 → reload → 写 log → 重启三服务 | 别名可达、容器/管理页状态一致 |
| E | system unit `SupplementaryGroups=docker` | **user unit 无法直接设 SupplementaryGroups**；以用户进 `docker` 组 + 重新登录/`newgrp` 验证 `daemonDockerAccess=ready`。system unit 路径仅在改用 system scope 时验收 |

> **macOS 不替代本项权限验收**（见 plan §13.1.3）。

### 035.06 — 管理页安全删除浏览器实机

| 场景 | 步骤 | 通过标准 |
| --- | --- | --- |
| 普通实例 | 打开删除模态 → 取消 | 实例仍在；无删除事件 |
| 普通实例 | 仅移除（不 purge） | 实例消失；`data/` 按契约保留或按 UI 文案 |
| 普通实例 | 彻底删除（purge） | 目录与 registry 清空 |
| 冗余实例 | 冗余批量删除路径 | 仅删冗余；保留最早者（若走 redundant API） |
| 静态 / 容器 | 各走一次取消 / 仅移除 / 彻底删除 | 无 500；`data_nonempty` → HTTP 409 + 可理解提示 |

可用 Playwright 或手工；记录浏览器与 OS。

### 036.08 — 正式平台实机矩阵

| 平台 | 最低冒烟 | Full / autostart（若宣称） |
| --- | --- | --- |
| Ubuntu 22.04 | init → import → start → status | setup --full + doctor --profile full |
| Ubuntu 24.04 | 同上 | 同上 |
| Debian 12 | 同上；apt 源不为 Ubuntu | 同上 |
| WSL2 Ubuntu / Debian | 工作区在 Linux 盘；拒绝 `/mnt/<drive>` Full 写路径。记录 Windows Build、WSL 包版本、**实际**网络模式（NAT/mirrored）。NAT 下 guest IP 不得当作局域网已通；`wsl_lan_exposure` 不得假绿。最终 LAN 须另一台设备验收 | systemd user + doctor ready；LAN 暴露按实际模式与已有转发分层记录 |
| macOS arm64 | 同上 smoke | Docker Desktop 可用时 Full |
| macOS Intel（仍承诺时） | smoke 即可 | 不要求与 arm64 同等 Full |

Windows 原生：任意实际 `lwa` 命令须在写工作区前 fail-fast（已由门禁覆盖；实机抽查即可）。

#### WSL 网络分层验收

每个组合单独记录 Windows 完整 Build、WSL 架构与软件包完整版本、Linux 发行版、`wslinfo --networking-mode` 的实际结果、LWA 提交和测试工作区。NAT 与 mirrored 分开验收；软件包升级或 `.wslconfig` 的声明不能替代实际模式。

使用独立测试工作区、专用端口和带有唯一标记的静态页面、相对 CSS/JS。先核对现有服务和端口，测试后只清理本次创建的实例与服务。不要在业务工作区上反复切换 TLS、绑定地址或重启实例。

| 观察点 | HTTP 与资源 | HTTPS 与信任 | 管理鉴权 |
| --- | --- | --- | --- |
| WSL 内 | 回环与 guest 地址分别获取页面/CSS/JS，记录内容哈希 | 按实际入口名称校验 SAN 和工作区 CA；不指定 CA 时应出现预期的不信任结果 | 分别记录回环和非回环来源的现有鉴权行为 |
| Windows 本机 | localhost 与 Windows 网卡入口分别记录，不能互相代替 | 显式指定导出的工作区 CA；按 IP 访问须覆盖无 SNI 场景 | 有效 token、无 token、伪造回环 Host/XFF 分开记录 |
| 另一台物理 LAN 设备 | 直接访问实际 Windows 入口和端口，页面与资源内容必须一致 | 直接访问 HTTPS 入口并校验证书；HTTPS 成功后仍须验证管理面 | 无 token 和伪造回环头须拒绝，有效 token 可读预期实例；不记录 token 原文 |

TCP 连通、HTTP 200 和资源成功下载分别保留证据；200 返回错误页、登录页或资源内容不匹配均不能算通过。页面交互须另用真实浏览器执行并记录结果，下载 JS 文件不等于执行了 JS。

在独立 LAN 客户端可按实际端口运行以下命令，命令中的占位符先替换为本次工作区信息：

```bash
# 禁用客户端代理，证明走实际 LAN 入口；不通过 SSH 隧道或端口转发验证 LAN。
curl --noproxy '*' --fail --show-error --max-time 20 \
  'http://<Windows入口IP>:<HTTP入口端口>/<路径别名>/index.html'
curl --noproxy '*' --fail --show-error --max-time 20 \
  --cacert ./lwa-root-ca.crt \
  'https://<Windows入口IP>:<HTTPS入口端口>/<路径别名>/index.html'
```

CA 通过 `lwa ca export` 导出，先核对来自正确测试工作区；不使用 `--insecure` 绕过验证。Windows localhost 的连接映射测试可用于验证证书选择机制，但须注明实际连接位置，不能作为独立 LAN 通过证据。

NAT 下同时核对已有 portproxy 的 listen 地址/端口和 connect 地址/端口，二者可以不同；读取失败记 unknown，不能记为无规则。缺少网页映射时记录入口缺失，供管理员审阅实际业务端口的配置建议。mirrored 不以有无 portproxy 判定可达；分别核对监听、Hyper-V/Windows 防火墙可读证据和外部实测。LWA 不自动更改这些规则。

启用 HTTPS 并收敛 `instanceBindHost=127.0.0.1` 后，重启测试实例使新绑定生效，再核对 HTTPS 页面和管理面仍可用、原实例明文口和管理后端不向 LAN 暴露。仅重新渲染网关不能替代实例绑定配置更新；外部超时也不能单独证明 Linux 监听已收敛。

#### WSL 恢复场景验收

恢复场景应在用户安排的测试维护窗口逐项执行，不擅自重启承载业务的 Windows/WSL。每项同时保存事件前后版本、实际网络模式、guest/Windows 地址、映射、监听、服务状态、`lwa doctor` 和三处观察点结果。

| 场景 | 额外核对 | 通过或保留边界 |
| --- | --- | --- |
| 仅测试实例/网关停止再启动 | 内容、端口、别名、TLS 与管理鉴权 | 只证明产品进程恢复，不计作 WSL 或 Windows 重启 |
| 地址变化或网络切换 | `lwa access refresh` 后的地址、候选及证书；旧的外部验证证据失效 | 未发生真实变化时记待测，不能用模拟地址替代 |
| 休眠恢复 | Windows 与 WSL 是否恢复、监听与规则是否仍匹配 | 从另一台设备重新验证，宿主本机结果单列 |
| WSL 关闭后重新启动 | Windows 唤醒条件、已安装 systemd 单元、guest IP 与转发目标 | 手动拉起通过不能写成自动恢复通过 |
| Windows 重启 | 登录/唤醒任务、WSL 生命周期、自启、规则和客户端信任 | 无维护窗口或环境时保留待测 |

未提供的 Windows/WSL 组合、不可读规则、不可达入口和未安排的恢复事件分别记录。模拟回归、单机实测、双设备通过三类结论不能混写；Docker/Compose 最低版本验收按独立专项处理。


### IMP-042.b — 跨盘 / 跨机迁移（延期）

**不在本期实现。** `lwa workspace relocate` 仅同卷；跨盘预检 blocking。人工路径见 [workspace-rename.md](workspace-rename.md)。V0.6.12 起对裸 `mv` 提供代码防复发与 doctor 诊断，V0.6.13 加固挂载查询 fail-safe 与 registry SKIP，但仍不能替代正式 relocate。

## 自动化测试运行

```bash
# 仅 E2E 验收
python3 -m pytest tests/scripts/test_e2e_acceptance.py -v

# 含真实 Docker 的端到端（需 Docker 守护进程）
export LWA_RUN_DOCKER_TESTS=1
python3 -m pytest tests/scripts/test_docker_integration.py -v
```
