# V1 已知限制（WBS-30.12）

本文明确 `lwa` V1 **不支持**或**有条件支持**的范围，帮助用户正确设定期望。
不在列表中的能力即为 V1 支持范围。

## 平台与运行环境

* **操作系统**：主要面向 Linux 小主机（树莓派、NUC、ARM 盒子等）开发与测试。
  macOS / Windows 可用于开发与静态/前端实例，容器路径在 Windows 上未充分验证。
* **Python**：要求 3.13+，不支持更早版本。
* **Docker**：要求 Docker + Docker Compose 插件（`docker compose` 子命令）。
  Compose v1 独立二进制不支持；低于推荐版本时仅告警，不阻断已满足最低线的环境。
  `lwa setup --full` / 内置安装脚本覆盖 **macOS / Linux（含 WSL）**；**Windows 原生**无内置脚本，需按 `lwa setup` 指引手动安装。
* **架构**：基线镜像 `node:24-alpine` / `python:3.13-slim` 以 x86_64 / arm64 为主；
  其他架构需用户自备镜像或调整模板。

## 项目识别

* **支持识别**：纯静态 HTML、纯前端 SPA（Vite/React/Vue/Svelte 等基于 `package.json` 的项目）、
  Node 后端（Express/Fastify 等）、Python 后端（FastAPI/Flask/Django 等）、含 SQLite 的全栈项目。
* **不自动识别**：Go / Rust / Java / .NET / PHP / Ruby 等其他生态（导入后标记 `pending`，
  需用户手动配置或扩展扫描器）。
* **数据库**：仅自动识别 SQLite（文件型）。MySQL / PostgreSQL / Redis 等网络数据库
  **不自动起容器**，需用户在项目内自行编排。
* **monorepo**：多项目工作区不自动拆分，按 zip 根目录整体识别一个实例。

## 托管与容器

* **静态网关**：默认 Caddy 优先，无 Caddy 时降级到内置 `http.server`。
  nginx 模板存在但 V1 未充分验证自动配置。
* **HTTPS**：V1 仅 HTTP。HTTPS / 证书自动化（Let's Encrypt）不在范围内。
* **自定义域名**：不支持。通过 `IP:端口` 访问。
* **WebSocket**：静态网关路径不做专门代理；容器路径依赖 Docker 端口映射，原则上可用但未专项测试。
* **数据持久化**：仅自动 bind mount `data/` 目录。其他路径（如日志、上传目录）需用户在项目内处理。
* **环境变量**：生成的 `.env` 仅含端口与资源限额等基础设施变量；应用所需业务密钥请写入 `docker/.env.local`（IMP-015，compose 可选注入，缺失不报错），不要改写由 lwa 生成的 `.env`。
* **路径别名**：统一入口依赖 Caddy；`builtin` 下设置别名会被拦截（IMP-022）。容器实例支持别名（IMP-014），但须先 start。
* **别名下 SPA 绝对资源路径（IMP-023）**：别名入口 `handle_path` 去掉 `/<alias>/` 前缀转发，相对路径资源（`./assets/…`）正常；但 Vue/React 等 SPA 若构建时用绝对 `base: '/'`，资源（`/assets/…`）会绕过别名打到入口根 → 空 200，页面白屏。受影响项目应构建时设相对 base（Vite `base: './'`）或 `--base=/<alias>/`，或继续用 hostPort 直达。`lwa access review` 会检测该空 200 并告警（入口 HTML 200 ≠ 别名下可渲染）。
* **浏览量统计**：别名入口流量计入（Caddy JSON log）；直连 hostPort 默认不计入；容器路径为尽力解析，数字可能近似。

## 管理页与 API

* **鉴权**：单一静态 token，无过期、无轮换、无角色分级。多用户场景不适用。
* **并发写入**：registry 用 SQLite WAL + 连接级锁，适合单机管理页并发；
  不适合多进程/多机水平扩展。
* **实时推送**：无 WebSocket / SSE，前端通过轮询刷新状态。
* **国际化**：前端仅中文。

## 安全

* **多租户隔离**：不支持。所有实例共享同一 Docker daemon 与主机内核。
* **网络隔离**：默认 bridge 网络，实例间默认可通信（V1 未启用自定义网络隔离）。
* **镜像签名**：基线镜像来自 Docker Hub，不做签名/校验和验证。
* **审计日志留存**：安全发现写入事件表与日志，无独立审计后台或告警通道。
* **资源限额强度**：Docker 的 memory/cpu 限制为软约束（cgroup），不防恶意 fork bomb 级别的滥用。

## 数据与备份

* **自动备份**：不提供。备份需用户自行打包工作区（见 [FAQ](faq.md#如何备份)）。
* **registry 迁移**：SQLite 单文件，可整体复制；但跨架构/跨 Docker 版本时不保证容器配置兼容。
* **历史版本**：一个实例一份 `current/`，不支持多版本切换或回滚到旧构建产物。

## CLI 与自动化

* **批量操作**：无通用批量 start/stop；需借助 shell 循环或管理页 API。**例外**：`lwa remove --redundant` 与管理页「批量删除冗余」可按 zip 指纹批量清理冗余实例（IMP-012 / IMP-019）。
* **滚动更新**：不支持蓝绿/滚动发布，`rebuild` 是停机重建。
* **CI/CD 集成**：无原生 webhook 触发器；可通过 inbox/ + daemon 或 API 自行实现。

## 大模型 Skills

* 当前内置的 15 个 SKILL.md 覆盖常见场景，但**不保证**特定 AI 工具能正确消费；
  Skills 是提示工程资产，效果取决于模型与上下文窗口。
* Skills 不会自动执行带副作用的操作，所有变更需人工确认。

## 升级路径

* V1 不提供内置的版本迁移工具。跨版本升级前请备份工作区，
  并关注版本变更说明。
* `local-web.json` schema 有版本字段（`version: "1"`），
  未来破坏性变更会升版本号并提供迁移脚本。
