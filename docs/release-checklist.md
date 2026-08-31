# V1 发布清单（WBS-30.11）

本清单用于 V1 正式发布前的最终核对。逐项确认后方可打 tag 发布。

## 代码与版本

- [ ] `pyproject.toml` 的 `version` 已设为目标发布版本（如 `0.8.0` / `1.0.0`）；该值在 pip 安装时固化为包元数据，是 git 不可用时的版本来源。
- [ ] 发布提交主题为 `V<x.y.z>-Build<NNNN>-<YYYYMMDD>` 格式：`resolve_version()`（version_info.py）在 git 检出内优先解析最新 commit 主题，其次包元数据，最后 `_FALLBACK_VERSION`；主题不带 `V` 版本前缀时 `lwa --version` 与管理页将显示旧版本（DOC-120）。
- [ ] `src/local_webpage_access/cli/` 包入口（`cli/__init__.py` 的 `version` 命令 / `version_info.py`）可正常解析版本；`python3 -m local_webpage_access` 与 `python3 -m local_webpage_access.cli` 均可调用。
- [ ] `README.md` 的特性、命令、路线图与实际实现一致（Phase 0~7 全部「已完成」；含浏览量 / 冗余 / 运维手册 / 自启动 `lwa autostart`（含 `doctor-hints`）/ 宿主机装配 `setup|init --default|--full|--resume` / `doctor --profile full` / `lwa capabilities` / Full Profile 能力闭环与周期刷新 / `lwa update` 默认重启 gateway / IMP-023 别名资源错位检测 / IMP-035 安全删除 / IMP-036 正式平台矩阵 / IMP-037 `gateway switch` / IMP-038·040 访问复核与 LAN 新鲜度 / IMP-039 `cancel-build` / IMP-041 remove 阶段日志 / IMP-042 `lwa workspace relocate` / IMP-043 显示名（含产物目录 title） / `lwa recover` / `lwa pageviews` / V0.6.12 裸 mv 防复发 / V0.6.13 挂载 fail-safe、pageviews 轮转与显示名回填加固 / **V0.7.0 IMP-046 Token 168h 自动轮换** / **IMP-047 本机文件夹源导入与更新** / **V0.7.1 IMP-051 选择文件夹（仅 loopback）与导入/`lwa update` 护栏** / **V0.7.2 IMP-014 容器别名 / IMP-052 Python 启动推断 / IMP-053 多工作区复用提示 / BUG-454~458 修复** / **V0.7.3 IMP-055 路径别名兼容性门禁（撤销容器豁免、收敛 /assets 回退、access review API 对照、导入期守卫）/ BUG-467~469 修复** / **V0.7.7 IMP-058 五层流水线（Gate-A 静态预检 CHK-V02~V06 / Gate-B 多候选与能力契约 / Gate-C 实证校验状态机 VERIFYING→RUNNING·DEGRADED·FAILED / 证据驱动探针 / 能力守恒 / 降级策略 / CHK-192~193 BUG-475~485 修复）** / **V0.7.8 BUG-491 更新实例时保留已有 DATABASE_URL / BUG-492 Gate-C SQLite 回退扫描 / BUG-494 Docker 集成测试修复** / **V0.7.9 BUG-495～507 子目录 frontend/server 识别、Poetry 依赖、预检 REJECTED 置 pending、健康探针仅 GET/HEAD 且 2xx/3xx 通过、sourceSubdir 限制在 current 内** / **V0.7.10 BUG-508～522 网关切换加锁与完整回滚、损坏 manifest fail-closed、Caddy 失败恢复 builtin、迁移路径边界与 verify、回滚仅释本轮端口、status 以 registry 为准** / **V0.7.11 BUG-523～527 CHK-215 复审修复：builtin 恢复直接 `_start_builtin`、切换异常阶段不再误标 switch_lock、start 成功清 stale lastError、预检识别无空格 `-rfile`、registry 路径改写 substr 精确匹配** / **V0.8.0 IMP-059 update 三态 reconcile（enabled 未运行自动拉起 + `--no-reconcile`）/ IMP-060 doctor `service_runtime_state`·`restart_resilience` / IMP-061 自启缺省安全（with_caddy·linger 三态、init/setup 引导、运行模式标注）/ IMP-063 一键 GitHub 更新通道（fetch→固定 OID ff-only→pip→新解释器接力；`--check`/`--dry-run`/`--no-pull`/`--remote`/`--ref`；repo+workspace 双锁；skip-pip 门控；恢复链）** / **IMP-065 GitHub 源一键导入与更新（--from-git / --ref / --subdir；浅克隆 staging + ls-remote 无变更探测；管理页「从 GitHub 导入」不限 loopback；doctor git 检查 WARN）**；skills 数为 **20**） / **V0.8.1 stdlib-http 弱信号与识别优先级修复（BUG-533～544）** / **V0.8.2 自启幂等与 update 等待就绪（issues #2–#5，BUG-549～552）** / **V0.8.4 全量代码审阅收口（CHK-245，BUG-574～579）** / **V0.8.5 buildHooks/preStart 声明式构建钩子、rebuild 源码陈旧检测 + `--sync` + doctor `source_freshness`、.dockerignore 根级收窄（BUG-581）、别名注入 X-Real-IP（issues #6–#9）** / **V0.8.9 issue #18 pip 三源 `||` 切源与超时重试（`pipFallbacks`/`pipRetries`/`pipTimeout`）、issue #21 已验证别名保留 + 统一入口 404 兜底 + Gate-C 快照含别名、issue #22 非 root 模板 USER 前 `chown`、issue #23 钩子重建透传、IMP-064 服务意图去污染（`lastStartError`/熔断/内部停止原语/联动查意图）、IMP-062 doctor `version_freshness`（复用 `update --check` + 24h 缓存）、IMP-056 C.01–C.03（list/status 兼容性提示、scan 刷新 findings、alias 拒绝附线索）**。
- [ ] 工作区无未提交的调试代码、`print`、`TODO` 残留（`grep -rn "TODO\|print(" src/`）。

## 测试

- [ ] `python3 -m pytest` 全绿，无 unexpected skip。
- [ ] 静态门禁：`python3 -m ruff check src tests` 与 `python3 -m mypy src/local_webpage_access` 均为 0（配置见 `pyproject.toml`，ADJ-033）。
- [ ] 端到端验收 `tests/test_e2e_acceptance.py` 全部通过。
- [ ] 在具备 Docker 的 Linux 主机上执行 `LWA_RUN_DOCKER_TESTS=1 python3 -m pytest tests/test_docker_integration.py`。
- [ ] 按 [acceptance-checklist.md](acceptance-checklist.md) 完成手工验收（尤其 WBS-29.09/11/12 容器构建启动与数据持久化；以及 033.13 Full/systemd、035.06 删除浏览器、036.08 平台矩阵）。
- [ ] 验收记录与问题清单已填写（acceptance-checklist.md 的「验收记录」「问题清单」两节）。
- [ ] 036.09：确认 daemon/manager 在 win32 上拒绝 DETACHED 启动（单元测试覆盖）；文档无 Windows 原生推销。
- [ ] IMP-042.b 跨盘/跨机迁移仍标延期，勿在发布说明中宣称已支持。

## 文档

- [ ] [README.md](../README.md) 已更新（含管理页、daemon、doctor、capabilities、Full Profile、skills、浏览量、冗余、运维手册、`setup|init --full --resume`、正式支持平台索引、`gateway switch` / `cancel-build` / `doctor --access` / LAN stale / `workspace relocate` / IMP-043 显示名 / V0.6.12 工作区一致性与挂载漂移防护 / V0.6.13 pageviews 轮转与显示名回填加固 / **IMP-046 token 轮换** / **IMP-047 `--from-dir`** / **IMP-051 选择文件夹** / **IMP-058 五层流水线 Gate-A/B/C** / **V0.8.0 IMP-059 三态 reconcile·IMP-060 doctor 新检查·IMP-061 缺省安全·IMP-063 一键更新通道 / **IMP-065 `--from-git` GitHub 源导入与更新**） / **V0.8.5 rebuild `--sync` 与源码陈旧检测、buildHooks/preStart、X-Real-IP**。
- [ ] [faq.md](faq.md) / [operations-playbook.md](operations-playbook.md) / [known-limitations.md](known-limitations.md) / [manager-page.md](manager-page.md) / [autostart.md](autostart.md) / [workspace-rename.md](workspace-rename.md) 与 IMP-033/034/035/036/037/038/039/040/041/042/043/**046/047/051**/**058**/**059/060/061/063** 行为一致。
- [ ] [docs/manager-page.md](manager-page.md) API 端点表与实际路由一致（含 pageviews / redundant / remove / path-alias IMP-022；`POST .../cancel-build`；`POST /api/gateway/switch`；`POST /api/access/refresh`；删除模态焦点管理；前端取消构建与 LAN stale 横幅；IMP-043 显示名/`nameSource`/产物目录 title；**IMP-046 token 轮换**；**IMP-047 import-from-dir / update-from-dir**；**IMP-051 pick-directory**）。
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
- [ ] `lwa manager on`（或前台 `lwa manager start`）能打开管理页；本机读 API 免 token、写需同源或 token，局域网访问需 token 登录。
- [ ] `lwa doctor` 在干净环境全部 ok/warn，无 fail。
- [ ] `lwa daemon on` 能自动导入 `inbox/` 中的 zip。

## 安全核对

- [ ] 生成的 compose.yaml 通过 `audit_compose` 无 critical（`test_security.py::test_generated_compose_passes_audit`）。
- [ ] 生成的 Dockerfile 通过 `audit_dockerfile` 无 critical（`test_security.py::test_generated_dockerfile_passes_audit`）；含 `curl|sh` / `ADD https://` 时拒绝写出（`test_generate_dockerfile_rejects_*`）。
- [ ] 管理 token 在绑定 `0.0.0.0` 时必须存在（`validate_manager_binding`）。
- [ ] zip slip 防护在导入层与审计层双重生效。
- [ ] 容器以非 root 用户运行（Dockerfile 模板确认）。

## 发布动作

- [ ] 在 `main` 之外的发布分支上操作（或按团队流程）。
- [ ] 更新 CHANGELOG（如有）。
- [ ] 打 tag：`git tag -a v1.0.0 -m "V1 release"`。
- [ ] 推送 tag 与分支。
- [ ] **源码发布 zip（BUG-202）**：在仓库根执行 `bash scripts/pack-release-zip.sh`，产物必须含 `pyproject.toml` + `src/`（可选 `docs/`）。禁止只打 `src/`+`docs/` 的残缺包（会导致 `pip install -e` 丢失 `lwa` 入口）。
- [ ]（可选）构建 wheel：`python3 -m build`，校验产物。
- [ ]（可选）发布到 PyPI / 内部源。
- [ ] 在仓库 Release Notes 中链接到 `docs/acceptance-checklist.md` 的验收记录。

## 回滚预案

- [ ] 确认 `git revert` 可回到上一个稳定 commit。
- [ ] 确认工作区数据（`apps/<id>/data/`、`registry/local-web.db`）在回滚后仍可被旧版本读取（schema 未变）。
- [ ] 记录发布负责人与联系方式，便于线上问题响应。
