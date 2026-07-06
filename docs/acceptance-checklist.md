# V1 验收清单（WBS-29）

本清单覆盖 `lwa` V1 端到端验收的 18 个子任务（WBS-29.01~18）。
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
python -c "from tests.fixtures import build_all, SAMPLES; build_all('acceptance-ws/inbox'); print(list(SAMPLES))"
```

执行后 `inbox/` 内会生成 6 个 zip：
`static_html.zip`、`vite_react.zip`、`node_express.zip`、
`fastapi_sqlite.zip`、`build_failure.zip`、`pending_unknown.zip`。

## 验收项

| WBS | 项 | 自动化 | 手工步骤 | 通过标准 |
| --- | --- | --- | --- | --- |
| 29.01 | 干净工作区 init | ✓ `test_e2e_init_creates_clean_workspace` | `lwa init ./ws` | 生成 `local-web.yml`、`registry/local-web.db`、`apps/`、`inbox/`、`skills/`（13 个 SKILL.md） |
| 29.02 | 导入静态 HTML | ✓ `test_e2e_static_html_import_and_structure` | `lwa import inbox/static_html.zip` | `apps/static-html/` 出现，`local-web.json` 的 `kind=static` |
| 29.03 | 静态目录结构 | ✓ 同上 | 检查 `apps/static-html/{source,current,logs,data}/` | 四个子目录齐全，`current/index.html` 存在 |
| 29.04 | 静态 HTTP 可访问 | ✓ `test_e2e_static_html_accessible_via_http` | `lwa start static-html`，浏览器访问分配端口 | 返回 HTML 内容，`lwa status` 显示 `running` |
| 29.05 | 导入 Vite/React | ✓ `test_e2e_vite_react_detected_as_frontend` | `lwa import inbox/vite_react.zip` | 识别为 `node` + `frontend-static` |
| 29.06 | 前端构建产物 | — | `lwa start vite-react` | 容器内执行 `npm run build`，`dist/` 生成，静态托管可访问 |
| 29.07 | 前端形态正确 | ✓ 同 29.05 | 检查 `local-web.json` | `servingMode=shared-static`，有 build 命令 |
| 29.08 | 导入 Node/Express | ✓ `test_e2e_node_express_detected_and_compose_generated` | `lwa import inbox/node_express.zip` | 识别为 `node` + `backend-container`，`docker/compose.yaml` 生成 |
| 29.09 | Node 容器构建启动 | **手工** | `lwa start node-express` | `docker compose up` 成功，`lwa status` 显示 `running`，HTTP 可访问 |
| 29.10 | 导入 FastAPI+SQLite | ✓ `test_e2e_fastapi_sqlite_detected_and_compose_generated` | `lwa import inbox/fastapi_sqlite.zip` | 识别为 `python`，`docker/compose.yaml` 含 `../data:/app/data` |
| 29.11 | FastAPI 容器构建启动 | **手工** | `lwa start fastapi-sqlite` | 容器构建并启动，HTTP 可访问 |
| 29.12 | 数据持久化 | **手工** | 写入数据 → `lwa stop` → `lwa start` → 再读 | 数据不丢失（`data/` 卷保留） |
| 29.13 | start/stop/restart | ✓ `test_e2e_start_stop_restart_static` | `lwa stop`、`lwa restart` | 状态在 `running`/`stopped` 间正确切换 |
| 29.14 | logs/status/stats | ✓ `test_e2e_logs_status_stats_queryable` | `lwa status`、`lwa logs <id>`、`lwa stats` | 三类信息均可查询且非空 |
| 29.15 | 管理页与 CLI 一致 | ✓ `test_e2e_manager_api_matches_cli_status` | `lwa manager` 打开管理页，对比 `lwa status` | 实例列表与状态一致 |
| 29.16 | doctor 排障 | ✓ `test_e2e_doctor_diagnoses_instance` | `lwa doctor`、`lwa doctor <id>` | 环境检查全 ok/warn；实例诊断无 fail |
| 29.17 | failed/pending 展示 | ✓ `test_e2e_failed_and_pending_display` | 导入 `build_failure.zip`、`pending_unknown.zip`，查管理页 | failed 显示错误原因；pending 显示在待处理区 |
| 29.18 | 记录结果与问题 | — | 填写下表 | 完成本清单 |

## 验收标准（来自 WBS-29）

1. 四个核心样例（static_html、vite_react、node_express、fastapi_sqlite）能完整完成 **导入 → 运行 → 展示**。
2. `stop` / `restart` 不会丢失数据。
3. 管理页展示与 CLI `lwa status` 一致。
4. 失败路径（build_failure、pending_unknown）可解释、可排障。

## 验收记录

| 字段 | 值 |
| --- | --- |
| 验收日期 | 2026-07-06 |
| 验收人 | fenix-wangminle |
| 环境 | Windows 11 Pro，Python 3.13.13 |
| 自动化结果 | `tests/test_e2e_acceptance.py` 全部通过（11/11） |
| 全量回归 | `python -m pytest`：见 `docs/testing.md` 最新统计 |
| Docker 手工验收 | 待在具备 Docker 的 Linux 主机上执行 29.09 / 29.11 / 29.12 |

## 问题清单

| 编号 | 描述 | 影响 | 状态 |
| --- | --- | --- | --- |
| — | 暂无阻塞性问题 | — | — |

> 如手工验收发现新问题，请在此表追加，并在对应代码/文档中修复后回归。

## 自动化测试运行

```bash
# 仅 E2E 验收
python -m pytest tests/test_e2e_acceptance.py -v

# 含真实 Docker 的端到端（需 Docker 守护进程）
export LWA_RUN_DOCKER_TESTS=1
python -m pytest tests/test_docker_integration.py -v
```
