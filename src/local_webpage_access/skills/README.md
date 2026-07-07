# Local Webpage Access Skills

正式产品名称：**Local Webpage Access**（CLI：`lwa`）。Skill 目录统一 `lwa-` 前缀。

这些 skills 是写给大模型（LLM）的**操作手册**，用于处理 `lwa` 自动化流程中
"判断不准"或"需要修复"的环节。每个 skill 只负责**判断、生成、修复配置**，
最终执行（构建、启动、停端口）仍由 `lwa` 完成（设计 §18）。

**命名约定**：目录与 skill 名统一使用 `lwa-` 前缀（共 14 个），避免与其他项目的 skill 撞名。

## 总览

| Skill | 触发场景 | 输出 |
| --- | --- | --- |
| [`lwa-import-zip`](lwa-import-zip/SKILL.md) | 拿到 zip 要部署 / 同项目出新版本 | 判断 import vs `--update`，避免重复新建 |
| [`lwa-detect-stack`](lwa-detect-stack/SKILL.md) | 识别项目技术栈 | 修改 `local-web.json` 的 `stack`/`kind` |
| [`lwa-detect-internal-port`](lwa-detect-internal-port/SKILL.md) | 找不到应用监听端口 | 修改 `local-web.json` 的 `internalPort` |
| [`lwa-build-frontend-static`](lwa-build-frontend-static/SKILL.md) | 前端 SPA 需构建为静态产物 | 修改 `local-web.json` + 构建脚本 |
| [`lwa-dockerize-node-app`](lwa-dockerize-node-app/SKILL.md) | Node 后端容器化 | 生成 `Dockerfile` |
| [`lwa-dockerize-python-app`](lwa-dockerize-python-app/SKILL.md) | Python 后端容器化 | 生成 `Dockerfile` |
| [`lwa-dockerize-fullstack-sqlite`](lwa-dockerize-fullstack-sqlite/SKILL.md) | 全栈 + SQLite 容器化 | 生成 `Dockerfile` + `docker-compose.yml` |
| [`lwa-generate-static-gateway-config`](lwa-generate-static-gateway-config/SKILL.md) | 静态实例需网关 | 生成 Caddy/nginx 配置 |
| [`lwa-generate-compose`](lwa-generate-compose/SKILL.md) | 多服务需编排 | 生成 `docker-compose.yml` |
| [`lwa-fix-docker-build-failure`](lwa-fix-docker-build-failure/SKILL.md) | 构建失败 | 诊断 + 修复 `Dockerfile`/依赖 |
| [`lwa-fix-container-startup-failure`](lwa-fix-container-startup-failure/SKILL.md) | 容器启动失败 | 诊断 + 修复启动配置 |
| [`lwa-fix-port-binding`](lwa-fix-port-binding/SKILL.md) | 端口冲突 | 修改端口映射 |
| [`lwa-diagnose-health-check`](lwa-diagnose-health-check/SKILL.md) | 健康检查失败 | 诊断说明 + 修复建议 |
| [`lwa-setup-host-environment`](lwa-setup-host-environment/SKILL.md) | 新机器首次部署 / 环境缺失 | 宿主机工具安装指引 + init/doctor 流程 |

## 输入约定

所有 skill 的输入均来自 `lwa` 工作区，无需额外采集：

1. **项目目录结构** —— `apps/<id>/current/` 的文件树。
2. **初始 `local-web.json`** —— `apps/<id>/local-web.json`。
3. **构建日志** —— `logs/<id>/build.log`。
4. **启动日志** —— `logs/<id>/run.log`。
5. **健康检查结果** —— registry 的 `last_health_check_at` / `last_error`。

## 输出约定

skill 的输出**只**落到以下位置（设计 §18）：

1. 修改后的 `local-web.json`。
2. `Dockerfile`（容器实例）。
3. `docker-compose.yml`（多服务实例）。
4. 静态网关配置（`static-gateway/` 下）。
5. 诊断说明（写入事件日志或返回给 `lwa`）。

## 通用禁止事项

适用于所有 skill：

- **不直接运行长期服务**（`docker run -d`、`npm start` 守护进程等由 `lwa` 决定）。
- **不修改 `data/` 内容**（用户数据，只读）。
- **不引入 privileged、Docker socket 挂载、宿主敏感目录**（安全边界，§17）。
- **不在容器内以 root 运行**（如非必要）。
- **不改动工作区外的文件**（`apps/<id>/` 和 `static-gateway/` 之外只读）。
- **不修改 registry SQLite**（由 `lwa` 通过 lifecycle 写入）。

## pending 实例处理流程（WBS-24.15）

**新环境首次部署**请先走 [`lwa-setup-host-environment`](lwa-setup-host-environment/SKILL.md)：
`lwa setup` → 安装缺失工具 → `pip install -e .` → `lwa init` → `lwa doctor`。

当 `lwa import` 或 `lwa daemon` 把实例标记为 `pending` 时，按以下流程处理：

```text
1. lwa scan <id> 重新识别
   ├── 识别成功（detection.pending=False）→ lwa start
   └── 仍 pending → 进入大模型介入
2. 选择 skill：
   - 不知技术栈          → lwa-detect-stack
   - 知道栈但不知端口    → lwa-detect-internal-port
   - 前端项目            → lwa-build-frontend-static
   - 后端需容器化        → lwa-dockerize-{node-app,python-app,fullstack-sqlite}
   - 多服务              → lwa-generate-compose
   - 静态需网关          → lwa-generate-static-gateway-config
3. skill 产出配置后，执行 lwa rebuild / lwa start 验证
   ├── 成功 → 实例进入 running
   └── 失败 → 进入修复 skill：
       - 构建失败        → lwa-fix-docker-build-failure
       - 启动失败        → lwa-fix-container-startup-failure
       - 端口冲突        → lwa-fix-port-binding
       - 健康检查不过    → lwa-diagnose-health-check
4. 修复后回到步骤 3，最多重试 2 轮后转人工
```
