# 测试运行指南

本文档说明 `lwa`（Local Webpage Access）的测试体系、运行方式与跳过条件（WBS-28）。

## 快速开始

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行全部非 Docker 测试
python3 -m pytest

# 查看详细输出
python3 -m pytest -ra -v

# 运行单个模块
python3 -m pytest tests/test_security.py
python3 -m pytest tests/test_doctor.py
```

## 测试分层

| 层 | 说明 | 是否需要 Docker | 典型文件 |
| --- | --- | --- | --- |
| 单元测试 | 纯逻辑，无 IO | 否 | `test_config.py`、`test_paths.py`、`test_models.py`、`test_registry.py`、`test_ports.py`、`test_scanner.py` |
| 模块集成 | 模块间串联，mock 外部进程 | 否 | `test_importer.py`、`test_folder_source.py`（IMP-047）、`test_compose.py`、`test_daemon.py`（含 daemon→manager API）、`test_manager_api.py`、`test_security.py`（含 generate_compose 审计）、`test_doctor.py`、`test_lifecycle.py`（mock DockerRuntime）、`test_pageviews.py`、`test_build_queue.py`、`test_zip_processor.py`、`test_manager_static_app.py` |
| 样例夹具 | 验证 6 个样例识别正确 | 否 | `test_fixtures.py`、`tests/fixtures/` |
| 真实 Docker | 端到端容器构建与运行 | **是** | `test_docker_integration.py` |

## Docker 测试跳过条件（WBS-28.15）

`tests/test_docker_integration.py` 使用双重守卫，默认跳过：

1. `requires_docker` —— PATH 中存在 `docker` 命令（`shutil.which`）。
2. `LWA_RUN_DOCKER_TESTS=1` —— 显式环境变量，避免在仅安装 docker
   但守护进程未运行时误触发。

启用方式：

```bash
# 正式支持平台：Linux / macOS / WSL2（不要在 Windows 原生跑）
export LWA_RUN_DOCKER_TESTS=1
python3 -m pytest tests/test_docker_integration.py
```

绝大多数 `docker_runtime` / `lifecycle` 测试通过 monkeypatch 模拟
`docker` 命令（见 `test_docker_runtime.py`、`test_lifecycle.py`），
不依赖真实 Docker，可在任何环境稳定执行。

## 模块覆盖对照（WBS-28.01~15）

| WBS | 测试文件 | 覆盖点 |
| --- | --- | --- |
| 28.01 测试运行命令 | `pyproject.toml` `[tool.pytest.ini_options]` | testpaths / pythonpath / addopts |
| 28.02 配置加载 | `test_config.py` | 默认值、自定义、校验 |
| 28.03 路径解析 | `test_paths.py` | Workspace、slug 校验（BUG-025） |
| 28.04 schema 校验 | `test_models.py` | pydantic v2 模型 |
| 28.05 registry DAO | `test_registry.py` | CRUD、事件、构建记录 |
| 28.06 端口分配 | `test_ports.py` | 分配、释放、并发（BUG-017） |
| 28.07 zip 导入 | `test_importer.py` | 解压、zip slip、slug 冲突 |
| — 文件夹源（IMP-047） | `test_folder_source.py` | 绝对路径校验、打包/指纹、import/update、scan 保元数据、CLI 路径一致性、隔离红线 |
| — 选择文件夹 / 导入护栏（IMP-051） | `test_directory_picker.py`、`test_import_activity.py`、`test_manager_static_app.py` | 原生选目录、导入活动锁、`lwa update` 等空闲、管理页提示与错误前缀 |
| 28.08 项目识别 | `test_scanner.py` | static / node / python / pending |
| 28.09 静态配置 | `test_static_gateway.py` | 网关路由、端口 |
| 28.10 Dockerfile | `test_dockerfile_templates.py`、`test_security.py`（生成门禁） | 模板渲染；`audit_dockerfile` critical 拒绝写出 |
| 28.11 Compose | `test_compose.py`、`test_security.py` | 模板、env、安全审计 |
| 28.12 生命周期 | `test_lifecycle.py`、`test_health_status.py` | start/stop/restart/rebuild/remove / 冗余清理 |
| 28.13 资源统计 | `test_stats.py` | 磁盘、内存解析 |
| 28.14 管理页 API | `test_manager_api.py` | token、全部端点（含 pageviews / redundant / remove / path-alias） |
| 28.15 Docker 跳过 | `conftest.py`、`test_docker_integration.py` | `requires_docker` / `LWA_RUN_DOCKER_TESTS` |
| — 浏览量（IMP-024～028） | `test_pageviews.py` | page 判定、IP 聚合、`uniqueIpList`、别名容器 Caddy 源、无别名静态直连端口归属、CLF/JSON 解析与摄入游标（含 Caddy gzip 轮转/多归档，V0.6.13） |
| — 能力 / Full（IMP-033） | `test_capability.py`、`test_host_bootstrap.py` | CapabilityReport、setup --full/--resume |
| — 网关切换（IMP-037） | `test_gateway_switch.py` | 原子切换事务、access 收尾 |
| — 访问复核（IMP-038/040） | `test_access.py`、`test_access_workflow.py`、`test_status_lan_freshness.py` | refresh/review、LAN 新鲜度 |
| — 取消构建（IMP-039） | `test_build_process.py`、`test_build_queue.py` | cancel / cancelling |
| — 自启（IMP-030） | `test_autostart.py` | install/check/linger |
| — 平台矩阵（IMP-036） | `test_platform_support.py` | Windows 原生 hard fail、LTS/WSL 门禁 |
| — 构建闸门（DEV-047） | `test_build_queue.py` | `CrossProcessBuildGate` 跨进程互斥与死进程回收 |
| — zip 处理抽取 | `test_zip_processor.py` | validate / hash / safe_extract |
| — 管理页前端 | `test_manager_static_app.py` | helpers / Vue 根组件冒烟、冗余徽章、浏览量渲染 |
| — 实证校验（IMP-058 Gate-C） | `test_gate_c_verification.py`、`test_health_status.py`、`test_hosting.py` | 探针发现（GET/HEAD）、2xx/3xx、sourceSubdir 边界、guessed 不得满足 servesApi |
| — 子目录识别（BUG-495～503） | `test_scanner.py`、`test_hosting.py`、`test_importer.py`、`test_paths.py` | frontend/server 子目录、Poetry 依赖、预检 REJECTED、npm cwd |
| — 服务期望态 reconcile（IMP-059） | `test_service_intent.py` | 意图判定（enabled/disabled/n.a.、交叉校验）、三态重启决策、监督器拉起无双进程、中断时长估算（V0.8.2 起按 live 证据链：pidfile 存活/陈旧 json 弃用/systemd InactiveEnterTimestamp/裸进程回退 started_at）、`--no-reconcile` |
| - doctor 服务韧性（IMP-060） | `test_doctor_service_checks.py`（19 用例） | `service_runtime_state` FAIL 矩阵与反向不一致（残留进程 WARN）、`restart_resilience` 四类 WARN（自启单元逐项差集/未启用/无 linger/容器策略）、runner 解耦（CHK-225 高③④）、接入主报告 |
| — 自启缺省安全（IMP-061） | `test_autostart_defaults.py` | with_caddy/linger 三态缺省、旧旗标兼容、init/setup 引导（TTY/非 TTY/已装跳过）、运行模式标注 |
| — 一键更新通道（IMP-063） | `test_update_source.py` | 目标解析（upstream/显式/拒绝 SHA）、双锁互斥、九态关系（含 shallow→unknown）、SourceCheckReport v1 与退出码、固定 OID 快进、skip-pip 门控、bootstrap/接力门控（HEAD 变化不跑旧进程 Runtime）、fetch warning 离线降级、dry-run 零写入、CLI `--check`/互斥；全部基于临时 bare remote 夹具，**零外网** |
| - GitHub 源导入与更新（IMP-065） | `test_git_source.py` | URL 解析（https/短路径/subdir）、clone 暂存与守卫、`ls-remote` 探测、导入/更新工作流（dry-run/事件/manifest git 字段）、CLI `lwa import git` 守卫、manager API 导入/更新端点、doctor git 检查、非交互与进程组；基于本地 bare remote 夹具，**零外网** |
| - 源码陈旧检测（V0.8.5 / issue #8，DEV-122） | `test_source_staleness.py`（22 用例） | folder 指纹比对（含旧 manifest 退化比对 `current/`）、git `ls-remote` OID 比对与离线静默、`rebuild --sync` 复用更新管线（zip+`--sync` exit 2）、doctor `source_freshness` WARN 纯离线、manager API `sourceStaleWarnings` 透出 |
| - 构建钩子（V0.8.5 / issue #7，DEV-121） | `test_dockerfile_templates.py`、`test_models.py` | `buildHooks`/`preStart` 换行符校验防注入、三渲染器 RUN 层与 `sh -c` CMD 拼接、旧 manifest 兼容、`audit_dockerfile` 钩子把关（`curl\|sh` 拒绝）、`.dockerignore` 根级收窄（嵌套 dist 保留） |
| - 评审修复回归（CHK-245，BUG-574～578） | `test_chk245_review_fixes.py` | 安全审计、数据层原子写/回滚、版本门禁、API 校验（`fallback_policy` 枚举 400、DNS rebinding Host 校验）、daemon 文件稳定性、doctor 修复、pageviews/access/autostart systemd 反转义/统计解析/迁移等修复的回归锁定 |

## 常见问题

### 端口池耗尽（PortError）

症状：`[PORT_ERROR] 端口池 [21000, 21050] 已耗尽`。

原因：测试用的静态托管服务器在异常退出时未释放端口，残留进程占用
21000-21050。

解决（macOS / Linux / WSL2）：

```bash
# 查看占用端口的进程
lsof -nP -iTCP:21000-21050 -sTCP:LISTEN
# 或：ss -lptn 'sport >= :21000 and sport <= :21050'

# 结束残留（把 PID 换成上一步结果）
kill <PID>
```

### 验收标准

* 非 Docker 单测在本机稳定执行（WBS-28 验收 1）。✓
* Docker 集成测试在具备 Docker 环境时可执行（WBS-28 验收 2）。✓
* 核心路径有覆盖，失败路径有基本覆盖（WBS-28 验收 3）。✓
