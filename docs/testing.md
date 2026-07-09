# 测试运行指南

本文档说明 `lwa`（Local Webpage Access）的测试体系、运行方式与跳过条件（WBS-28）。

## 快速开始

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行全部非 Docker 测试
python -m pytest

# 查看详细输出
python -m pytest -ra -v

# 运行单个模块
python -m pytest tests/test_security.py
python -m pytest tests/test_doctor.py
```

## 测试分层

| 层 | 说明 | 是否需要 Docker | 典型文件 |
| --- | --- | --- | --- |
| 单元测试 | 纯逻辑，无 IO | 否 | `test_config.py`、`test_paths.py`、`test_models.py`、`test_registry.py`、`test_ports.py`、`test_scanner.py` |
| 模块集成 | 模块间串联，mock 外部进程 | 否 | `test_importer.py`、`test_compose.py`、`test_daemon.py`、`test_manager_api.py`、`test_security.py`、`test_doctor.py`、`test_lifecycle.py`（mock DockerRuntime）、`test_pageviews.py`、`test_build_queue.py`、`test_zip_processor.py`、`test_manager_static_app.py` |
| 样例夹具 | 验证 6 个样例识别正确 | 否 | `test_fixtures.py`、`tests/fixtures/` |
| 跨模块集成 | daemon×manager×security×doctor | 否 | `test_integration_phase57.py` |
| 真实 Docker | 端到端容器构建与运行 | **是** | `test_docker_integration.py` |

## Docker 测试跳过条件（WBS-28.15）

`tests/test_docker_integration.py` 使用双重守卫，默认跳过：

1. `requires_docker` —— PATH 中存在 `docker` 命令（`shutil.which`）。
2. `LWA_RUN_DOCKER_TESTS=1` —— 显式环境变量，避免在仅安装 docker
   但守护进程未运行时误触发。

启用方式：

```bash
# Linux / macOS
export LWA_RUN_DOCKER_TESTS=1
python -m pytest tests/test_docker_integration.py

# Windows (Git Bash)
LWA_RUN_DOCKER_TESTS=1 python -m pytest tests/test_docker_integration.py
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
| 28.08 项目识别 | `test_scanner.py` | static / node / python / pending |
| 28.09 静态配置 | `test_static_gateway.py` | 网关路由、端口 |
| 28.10 Dockerfile | `test_dockerfile_templates.py` | 模板渲染 |
| 28.11 Compose | `test_compose.py` | 模板、env、安全审计 |
| 28.12 生命周期 | `test_lifecycle.py`、`test_health_status.py` | start/stop/restart/rebuild/remove / 冗余清理 |
| 28.13 资源统计 | `test_stats.py` | 磁盘、内存解析 |
| 28.14 管理页 API | `test_manager_api.py` | token、全部端点（含 pageviews / redundant / remove / path-alias） |
| 28.15 Docker 跳过 | `conftest.py`、`test_docker_integration.py` | `requires_docker` / `LWA_RUN_DOCKER_TESTS` |
| — 浏览量（IMP-024） | `test_pageviews.py` | CLF/Caddy JSON/容器日志解析、store 聚合、摄入游标 |
| — 构建闸门（DEV-047） | `test_build_queue.py` | `CrossProcessBuildGate` 跨进程互斥与死进程回收 |
| — zip 处理抽取 | `test_zip_processor.py` | validate / hash / safe_extract |
| — 管理页前端 | `test_manager_static_app.py` | helpers / Vue 根组件冒烟、冗余徽章、浏览量渲染 |

## 常见问题

### 端口池耗尽（PortError）

症状：`[PORT_ERROR] 端口池 [21000, 21050] 已耗尽`。

原因：测试用的静态托管服务器在异常退出时未释放端口，残留进程占用
21000-21050。

解决（Windows）：

```bash
# 查看占用端口的进程
netstat -ano | grep -E "210[0-4][0-9]|21050"

# 批量清理
for pid in $(netstat -ano | grep -E "210[0-4][0-9]|21050" | grep LISTENING | awk '{print $5}' | sort -u); do
  taskkill //PID $pid //F
done
```

### 验收标准

* 非 Docker 单测在本机稳定执行（WBS-28 验收 1）。✓
* Docker 集成测试在具备 Docker 环境时可执行（WBS-28 验收 2）。✓
* 核心路径有覆盖，失败路径有基本覆盖（WBS-28 验收 3）。✓
