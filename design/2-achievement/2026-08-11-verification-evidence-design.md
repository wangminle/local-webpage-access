# 部署能力实证与 Node 子目录识别修复设计

## 背景

CHK-193 后续复核确认两个未闭环问题：

1. 容器首页 HTTP 存活后，验证器直接根据 `CapabilityContract` 声明补齐 `api`、`database`、`migrations`，使契约既是要求又成为证据。
2. Node 子目录候选只在依赖命中固定后端框架集合时生成，只有有效 `start` 脚本的 Node 服务仍被遗漏。

同一轮质量门禁还报告 `ruff` 23 项和 `mypy` 7 项错误。

## 目标

- 能力只能由运行时证据观测，不得从契约声明反向推导。
- 保持猜测探针的非阻断语义：失败只告警，实际成功响应可作为 API 观测证据。
- SQLite 能力通过实际挂载文件的可读性验证。
- Alembic 迁移只在可以证明迁移命令成功时记录，并补齐子目录 Alembic 证据。
- Node 子目录的已知后端依赖或明确服务启动脚本均可生成候选，但不将纯前端 `dev`/`preview` 脚本误判为后端。
- 本轮 `ruff` 和 `mypy` 门禁清零。

## 方案

### 1. 能力证据聚合

`health.evaluate_success_predicate()` 和 `hosting._evaluate_container_verification()` 统一使用运行时结果组装 `observed`：

- `ui`：基础存活探针成功。
- `api`：任一声明/发现探针成功，或猜测 API 探针真实获得有效响应。单独首页存活不记录 `api`。
- `database`：调用方明确传入已验证的数据库结果。SQLite 使用宿主挂载目录中的目标文件，以只读模式打开并执行轻量查询。
- `migrations`：调用方明确传入已验证的迁移结果。对当前 Alembic 启动模式，仅当命令以 `alembic upgrade ... && <server>` 的受控顺序存在且服务已存活时认定迁移成功。

能力不足时返回准确缺口信息，而不统一误报“必选探针未通过”。

### 2. Node 启动脚本识别

新增一个保守的服务启动脚本判定器：

- 接受 `node <file>`、`tsx <file>`、`ts-node <file>`、`nest start`、`next start`、`nuxt start`等明确长驻服务命令。
- 排除 `vite`、`webpack serve`、`react-scripts start`、`next dev`、`nuxt dev`等开发或前端开发服务器。
- 已知 `_NODE_BACKEND` 依赖仍保持最高优先级。脚本判定只补足依赖不可见或使用 Node 内置 HTTP 服务的项目。

### 3. 子目录 Alembic 证据

`SubdirSignal` 增加 Alembic 信号，`evidence_collector` 在子目录发现 `alembic.ini` 时记录。生成部署计划时，根目录或当前后端子目录的 Alembic 信号均可使 `requiresMigrations=True`。

### 4. 质量门禁

仅处理当前 `ruff`/`mypy` 报告的确定性问题：未使用导入/变量、类型缩窄、前向类型引用、错误的模型构造参数及可空配置。不做与门禁无关的格式化或重构。

## 错误处理

- SQLite 文件不存在、无法打开或查询失败：不记录 `database`，验证结果为 failed 并附带能力缺口。
- 迁移命令不是受控顺序或无法证明已成功：不记录 `migrations`。
- 猜测 API 全部失败：保留诊断告警；若契约确实要求 API，则因能力缺口失败。

## 测试策略

- 先增加无 API/DB/迁移证据不得通过的失败测试。
- 分别覆盖 API 探针成功、SQLite 可读、SQLite 损坏、受控 Alembic 命令、无法证明迁移的情形。
- 增加 Node 子目录 `start: node server.js` 正例和 Vite/开发脚本反例。
- 增加子目录 Alembic 契约测试。
- 最后执行定向 pytest、全量 pytest、`ruff check src tests`、`mypy src/local_webpage_access`和 `git diff --check`。
