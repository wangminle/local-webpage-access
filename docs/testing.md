# 测试运行指南

本文档说明 `lwa`（Local Webpage Access）的测试体系、运行方式与跳过条件（WBS-28）。

## 快速开始

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 统一检查入口：Ruff + pytest + Git 差异检查
bash tests/gate.sh

# 运行全部非 Docker 测试
python3 -m pytest

# 查看详细输出
python3 -m pytest -ra -v

# 运行单个模块
python3 -m pytest tests/scripts/test_security.py
python3 -m pytest tests/scripts/test_doctor.py
```

`tests/gate.sh` 自动定位仓库根目录，依次运行 `ruff check src tests`、pytest、工作区与暂存区的 `git diff --check`，汇总结果；任一步失败则返回非零退出码。pytest 参数可以直接透传，例如 `bash tests/gate.sh -k alias -x`。pytest 默认经 pytest-xdist 并行（默认 3 个 worker：`-n 3 --dist loadgroup`，以控制 CPU/内存占用；`LWA_GATE_JOBS=auto` 可用满全部核；占用固定测试端口池的文件固定同组串行，其余按文件分组；`tests/scripts/conftest.py` 的 `_REAL_PORT_FILES` 是手工清单——新增会真实占用固定测试端口的测试文件时必须把文件名加进去，否则并行时可能出现间歇性端口冲突）；调试 `-s` / `--pdb` 或排查可疑并行问题时用 `LWA_GATE_JOBS=1 bash tests/gate.sh` 强制串行（约 4 分钟），未安装 xdist 时自动退回串行。Caddy 语义测试在缺少 Caddy 时跳过，真实 Docker 测试（含耗时约 35s 的 `test_agent_m1_acceptance.py::test_container_operation_reaches_terminal_state`）默认跳过，设置 `LWA_RUN_DOCKER_TESTS=1` 才启用。真实 Caddy TLS 握手端到端（`test_bug944_tls_multi_san.py` 两个用例，会拉起临时 Caddy 进程）默认跳过，设置 `LWA_RUN_CADDY_TLS_E2E=1` 才启用。

GitHub Actions（`.github/workflows/ci.yml`）在 `ubuntu-latest` 上复现同一道门禁：安装固定版本 Caddy、`uv sync --extra dev --extra mcp`，并在 uv 虚拟环境中安装 `pip`。`mcp` 是门禁会导入的可选依赖；uv 创建的解释器默认没有 `pip` 模块，而 `lwa update` 会在快进前做 pip 预检。`report` job 安装同一套 Caddy 后记录 mypy 与覆盖率，不阻断合并。

`tests/scripts/` 保存自动化测试，`tests/evals/` 保存真实应用部署评测的本地源码；后者按 pytest 配置排除自动收集。

## 测试分层

| 层 | 说明 | 是否需要 Docker | 典型文件 |
| --- | --- | --- | --- |
| 单元测试 | 纯逻辑，无 IO | 否 | `test_config.py`、`test_paths.py`、`test_models.py`、`test_registry.py`、`test_ports.py`、`test_scanner.py` |
| 模块集成 | 模块间串联，mock 外部进程 | 否 | `test_importer.py`、`test_folder_source.py`（IMP-047）、`test_compose.py`、`test_daemon.py`（含 daemon→manager API）、`test_manager_api.py`、`test_security.py`（含 generate_compose 审计）、`test_doctor.py`、`test_lifecycle.py`（mock DockerRuntime）、`test_hosting.py`、`test_container_identity.py`（issue #20 非 root 身份：UID/GID 对齐、预检、migrate-user）、`test_pageviews.py`、`test_build_queue.py`、`test_zip_processor.py`、`test_manager_static_app.py` |
| 样例夹具 | 验证 6 个样例识别正确 | 否 | `test_fixtures.py`、`tests/fixtures/` |
| 真实 Docker | 端到端容器构建与运行（含 issue #20 非 root 验证：`id -u` 非 0、`/app/data` 与 RUNTIME_ROOT 布局非 root 写 SQLite） | **是** | `test_docker_integration.py` |

## Docker 测试跳过条件（WBS-28.15）

`tests/scripts/test_docker_integration.py` 使用双重守卫，默认跳过：

1. `requires_docker` —— PATH 中存在 `docker` 命令（`shutil.which`）。
2. `LWA_RUN_DOCKER_TESTS=1` —— 显式环境变量，避免在仅安装 docker
   但守护进程未运行时误触发。

启用方式：

```bash
# 正式支持平台：Linux / macOS / WSL2（不要在 Windows 原生跑）
export LWA_RUN_DOCKER_TESTS=1
python3 -m pytest tests/scripts/test_docker_integration.py
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
| - WSL 网络只读快照（issue #140 / BUG-923～937，V0.9.10 / PLN-054） | `test_wsl_network.py`、`test_https_first_delivery.py`、`test_status_lan_freshness.py` | NAT / mirrored / 未知模式判定（Windows Build 与 WSL 包版本区分）、N10-2/N11/M11/X10-M/X-UNK 策略模拟、portproxy 规则解析与目标地址/端口校验、读取超时保留 `unknown`、无别名实例按 hostPort 取指导端口、`manualLanIp` 仅作候选、`localhostForwarding` 与 Public 配置文件只读提示、TLS SAN 含 Windows IP 且 `bind 0.0.0.0`、`lanReachability` / `lanExposure` / `dynamicIpForwarding` 字段；纯函数夹具，**不占端口**。模拟不能填作矩阵实机通过 |
| - WSL 监听/防火墙证据与外部观测闭环（DEV-168 / PLN-054） | `test_wsl_network.py`、`test_status_lan_freshness.py`、`test_doctor.py`、`test_manager_api.py`、`test_manager_static_app.py`、`test_hosting.py` | Windows TCP 监听与 Windows/Hyper-V 防火墙只读采集、各自失败保留独立 `unknown`；有 portproxy 时 `windowsListening` 核对 `listen_port`；监听/防火墙指纹稳定排序；`lan-client` 成功观测须协议/主机/端口/路径与当前入口一致才升格 `externally-verified`，guest/Windows 本机不能升格；网络指纹或入口变化后读时失效；记录观测持实例锁，重启/重建保留观测字段。模拟不能填作矩阵实机通过 |
| - 破坏性 buildHooks 检测与导入期别名豁免（issue #141/#142，BUG-951/952/955/958，V0.9.13） | `test_bug951_build_hooks.py`、`test_bug952_import_alias_guard.py` | 历史 `rm -rf` 产物目录钩子重建拒绝（HTTP 409）且 `--force` 显式放行并记事件、doctor 告警、Dockerfile 仍渲染原钩子；检测按 `&&`/`||`/`;` 拆段仅核对 rm 操作数，清临时目录再拷贝的钩子不误拦；导入期 `--path-alias` 对 Vite 源码入口（`/src/main.*` + package.json/vite.config）豁免并设 `buildBaseFromAlias`，预构建 `/assets/` 即使带 build 脚本仍拒绝、纯绝对 SPA 资产仍拒绝 |
| - WSL NAT 多 SAN 入口证书（BUG-944，V0.9.12） | `test_bug944_tls_multi_san.py` | 含不属于 guest 的入口地址时改用工作区 CA 根直签的单张多 SAN 证书（`run/tls/entry/`，`tls <cert> <key>`）、非 WSL 保持逐地址 `tls internal`、全新工作区按 Caddy pki 布局自举 root 且沿用已有 root、`openssl verify` 链验证与 serverAuth/≤825 天、SAN 或 root 变化重签而未变复用、重签后 `caddy reload --force`、缺 openssl / 签发失败退回 `tls internal` 且不留半成品；`caddy validate` 真实加载证书（缺 caddy 跳过）；`LWA_RUN_CADDY_TLS_E2E=1` 时另起真实 Caddy 做无 SNI 握手：修复前按 Windows IP 校验失败、修复后三地址通过，并验证 Caddy 采用自举 root（默认跳过，占临时端口） |
| - Eval 部署收敛（CHK-444～450，V0.9.10） | `test_eval_deploy_convergence.py`、`test_bug931_936_937.py` | Flask init-db 全路径证明（子目录 / 自定义 `instance_path` 字面量 / 配置覆盖与构造参数不确定时不包装 / 双库歧义）与 compose 托管挂载跟随字面量及重建前救援、Django/Flask 声明式 SQLite 识别、`.env.local` 数据库变量引导、pnpm/yarn workspace 根与独立子目录锁文件归属、bun/静态失败不归因 Dockerfile、Git 切源钉 ref 与 commit ref 判定、`hostBuildTimeout` 预算与提示、`run/pkg-cache` 独立缓存、build.log 分段、下载源冷却、手动构建 owner 阻断自愈与死 PID 回收、代理诊断仅在网络失败时附带、doctor WSL 缺口文案按实例端口 |
| - 三机实测部署修复（BUG-966/967/978/984/985/986/987/988） | `test_bug966_967_node_toolchain.py`、`test_bug978_flask_metadata.py`、`test_bug985_alias_base_env.py`、`test_bug986_987_node_native.py`、`test_bug988_apk_mirror_build_timeout.py` | pnpm 钉版（packageManager 声明优先含 +sha 剥离；lockfileVersion 精确映射 5.0~5.3→pnpm6.35.1、5.4→7.33.7、6.0→8.15.9，<5 不钉）、钉版必带 `corepack enable &&` 前缀保镜像内 pnpm shim（BUG-984）、宿主钉版解析三分支（兼容 enable+钉版与无前缀钉版两形式）与 corepack 缺失可操作提示、npm engines range 求值（连字符/通配/^~/`\|\|`，caret 短范围 `^22.12`/`^0.2`/`^0` npm 语义）与过新过旧方向、engines 失配默认阻断含 nodeEnginesIgnore 提示、显式忽略下 yarn `--ignore-engines` 与 pnpm engine-strict 受控解除、钉版 berry 不追加、`--registry` 注入与锁文件 COPY 兼容钉版、镜像真实版本/区间交集选择（`>=24.0.0 <24.10.0`→node:24.9-alpine、`>=22.12.0 <23`→node:22-alpine、线内上界钉次版本、18 线候选、无交集保持默认+告警）；别名 base 同值注入 VITE_BASE/BASE_URL/PUBLIC_URL 三键（BUG-985）；原生依赖包名边界识别（/sqlite3@ 可匹配、sqlite3-cli 不误判）触发 apk 追加 python3/make/g++、yarn berry 检测（.yarn/releases 或 lock `__metadata`）降 node:22-alpine（BUG-986/987）；apk 层 apkMirror 仓库主机替换（china 默认阿里/禁用不注/自定义 host:port/非法主机不注入/端到端渲染）、`buildTimeout` 配置默认 1800 与 build() 取值优先级（BUG-988）；AppleDouble `._*` 过滤后 init-db 证明恢复、0 字节空库/无表 sqlite 真实 sh 守卫自愈（注册用户+文章数据随后逐行保留）、不可解析文件与 config.py 守卫保护 |

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
