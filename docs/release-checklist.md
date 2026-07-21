# V1 发布清单（WBS-30.11）

本清单用于 V1 正式发布前的最终核对。逐项确认后方可打 tag 发布。

## 代码与版本

- [ ] `pyproject.toml` 的 `version` 已设为目标发布版本（如 `0.6.6` / `1.0.0`）。
- [ ] `src/local_webpage_access/cli/` 包入口（`cli/__init__.py` 的 `version` 命令 / `version_info.py`）读取该版本号；`python3 -m local_webpage_access` 与 `python3 -m local_webpage_access.cli` 均可调用。
- [ ] `README.md` 的特性、命令、路线图与实际实现一致（Phase 0~7 全部「已完成」；含浏览量 / 冗余 / 运维手册 / 自启动 `lwa autostart` / 宿主机装配 `setup|init --default|--full|--resume` / `doctor --profile full` / `lwa capabilities` / Full Profile 能力闭环 / IMP-035 安全删除 / IMP-036 正式平台矩阵 / IMP-037 `gateway switch` / IMP-038·040 访问复核与 LAN 新鲜度 / IMP-039 `cancel-build` / IMP-041 remove 阶段日志；skills 数为 17）。
- [ ] 工作区无未提交的调试代码、`print`、`TODO` 残留（`grep -rn "TODO\|print(" src/`）。

## 测试

- [ ] `python3 -m pytest` 全绿，无 unexpected skip。
- [ ] 端到端验收 `tests/test_e2e_acceptance.py` 全部通过。
- [ ] 在具备 Docker 的 Linux 主机上执行 `LWA_RUN_DOCKER_TESTS=1 python3 -m pytest tests/test_docker_integration.py`。
- [ ] 按 [acceptance-checklist.md](acceptance-checklist.md) 完成手工验收（尤其 WBS-29.09/11/12 容器构建启动与数据持久化）。
- [ ] 验收记录与问题清单已填写（acceptance-checklist.md 的「验收记录」「问题清单」两节）。

## 文档

- [ ] [README.md](../README.md) 已更新（含管理页、daemon、doctor、capabilities、Full Profile、skills、浏览量、冗余、运维手册、`setup|init --full --resume`、正式支持平台索引、`gateway switch` / `cancel-build` / `doctor --access` / LAN stale）。
- [ ] [faq.md](faq.md) / [operations-playbook.md](operations-playbook.md) / [known-limitations.md](known-limitations.md) / [manager-page.md](manager-page.md) / [autostart.md](autostart.md) 与 IMP-033/034/035/036/037/038/039/040/041 行为一致。
- [ ] [docs/manager-page.md](manager-page.md) API 端点表与实际路由一致（含 pageviews / redundant / remove / path-alias IMP-022；`POST .../cancel-build`；`POST /api/gateway/switch`；`POST /api/access/refresh`；删除模态焦点管理；前端取消构建与 LAN stale 横幅）。
- [ ] [docs/operations-playbook.md](operations-playbook.md) 与网关选型 / `gateway switch` / 访问复核 / `cancel-build` / 宿主机装配档位 / 冗余 / 容器别名 / Caddy 排障一致。
- [ ] [docs/faq.md](faq.md) 覆盖导入/容器/管理页/端口/磁盘各类排障（含 slug 冲突与 `--update`、内置 Docker/Caddy 安装，不再写自动 `-2/-3`；`doctor --json` 未初始化亦可输出 platformSupport；取消构建；网关切换 `accessOk`/`fullyOk`；删除对账）。
- [ ] [docs/security-boundary.md](security-boundary.md) 审计项与 `security.py` 实现一致。
- [ ] [docs/known-limitations.md](known-limitations.md) 明确 V1 不支持范围（含 `.env.local`、冗余批量例外、Ubuntu LTS / Debian Stable 矩阵）。
- [ ] [docs/testing.md](testing.md) 测试分层与跳过条件准确（含 pageviews / build_queue / zip_processor）。
- [ ] [docs/acceptance-checklist.md](acceptance-checklist.md) 18 个子任务有结论。

## 安装与冒烟

- [ ] 干净虚拟环境中 `pip install -e .` 成功，`lwa version` 输出版本号。
- [ ] `pip install -e ".[dev]"` 成功，`python3 -m pytest` 可运行。
- [ ] 全新目录 `lwa init` → `lwa import <样例 zip>` → `lwa start` → `lwa status` 全链路通过。
- [ ] `lwa manager on`（或前台 `lwa manager start`）能打开管理页；本机免 token，局域网访问需 token 登录。
- [ ] `lwa doctor` 在干净环境全部 ok/warn，无 fail。
- [ ] `lwa daemon on` 能自动导入 `inbox/` 中的 zip。

## 安全核对

- [ ] 生成的 compose.yaml 通过 `audit_compose` 无 critical（`test_security.py::test_generated_compose_passes_audit`）。
- [ ] 管理 token 在绑定 `0.0.0.0` 时必须存在（`validate_manager_binding`）。
- [ ] zip slip 防护在导入层与审计层双重生效。
- [ ] 容器以非 root 用户运行（Dockerfile 模板确认）。

## 发布动作

- [ ] 在 `main` 之外的发布分支上操作（或按团队流程）。
- [ ] 更新 CHANGELOG（如有）。
- [ ] 打 tag：`git tag -a v1.0.0 -m "V1 release"`。
- [ ] 推送 tag 与分支。
- [ ] **源码发布 zip（BUG-202）**：在仓库根执行 `bash scripts/pack-release-zip.sh`，产物必须含 `pyproject.toml` + `src/`（可选 `docs/`）。禁止只打 `src/`+`docs/` 的残缺包（会导致 `pip install -e` 丢失 `lwa` 入口）。
- [ ]（可选）构建 wheel：`python -m build`，校验产物。
- [ ]（可选）发布到 PyPI / 内部源。
- [ ] 在仓库 Release Notes 中链接到 `docs/acceptance-checklist.md` 的验收记录。

## 回滚预案

- [ ] 确认 `git revert` 可回到上一个稳定 commit。
- [ ] 确认工作区数据（`apps/<id>/data/`、`registry/local-web.db`）在回滚后仍可被旧版本读取（schema 未变）。
- [ ] 记录发布负责人与联系方式，便于线上问题响应。
