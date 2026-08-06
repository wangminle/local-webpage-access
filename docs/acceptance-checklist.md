# V1 验收清单（WBS-29）

本清单覆盖 `lwa` V1 端到端验收的核心子任务（WBS-29.01~18），并增补浏览量 / 冗余清理抽查项（29.19~20）。
其中**不依赖 Docker 守护进程**的部分已由自动化测试
`tests/test_e2e_acceptance.py` 覆盖（见下表「自动化」列）；
**依赖真实 Docker** 的容器构建/启动部分需按本清单手工验收。

## 验收前置

```bash
# 安装
pip install -e ".[dev]"

# 确认 Docker 可用
docker version
docker compose version

# 准备一个干净工作区
lwa init ./acceptance-ws
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
| 29.01 | 干净工作区 init | ✓ `test_e2e_init_creates_clean_workspace` | `lwa init ./ws` | 生成 `local-web.yml`、`registry/local-web.db`、`apps/`、`inbox/`、`skills/`（**19** 个 SKILL.md） |
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
| 自动化结果 | `tests/test_e2e_acceptance.py` 全部通过（11/11） |
| 全量回归 | `python3 -m pytest`：734 passed / 4 skipped；`LWA_RUN_DOCKER_TESTS=1 python3 -m pytest tests/test_docker_integration.py -q`：4 passed |
| Docker 手工验收 | 已通过 29.09 / 29.11 / 29.12；临时工作区 `/tmp/lwa-acceptance-oMtPaK`；Node 端口 18002，FastAPI 端口 18003，容器内 `/app/data/persist.txt` stop/start 后仍为 `persisted` |

## 问题清单

| 编号 | 描述 | 影响 | 状态 |
| --- | --- | --- | --- |
| — | 暂无阻塞性问题 | — | — |

> 如手工验收发现新问题，请在此表追加，并在对应代码/文档中修复后回归。

## Full Profile / 平台 / 删除补强验收（033.13 · 035.06 · 036.08）

主路径代码已落地；下列为**实机**补强项（本机单元/集成测试不能替代）。验收时勾选并填「验收记录」附录。

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
| WSL2 Ubuntu | 工作区在 Linux 盘；拒绝 `/mnt/<drive>` Full 写路径 | systemd user + doctor ready |
| macOS arm64 | 同上 smoke | Docker Desktop 可用时 Full |
| macOS Intel（仍承诺时） | smoke 即可 | 不要求与 arm64 同等 Full |

Windows 原生：任意实际 `lwa` 命令须在写工作区前 fail-fast（已由门禁覆盖；实机抽查即可）。

### IMP-042.b — 跨盘 / 跨机迁移（延期）

**不在本期实现。** `lwa workspace relocate` 仅同卷；跨盘预检 blocking。人工路径见 [workspace-rename.md](workspace-rename.md)。V0.6.12 起对裸 `mv` 提供代码防复发与 doctor 诊断，V0.6.13 加固挂载查询 fail-safe 与 registry SKIP，但仍不能替代正式 relocate。

## 自动化测试运行

```bash
# 仅 E2E 验收
python3 -m pytest tests/test_e2e_acceptance.py -v

# 含真实 Docker 的端到端（需 Docker 守护进程）
export LWA_RUN_DOCKER_TESTS=1
python3 -m pytest tests/test_docker_integration.py -v
```
