# LWA 代码审查报告（组 6：迁移 / 更新 / 平台 / 初始化 / 版本）

审查对象（全部逐行读完，未跳读）：
`workspace_migrate.py`（1433 行）、`updater.py`（1039 行）、`host_bootstrap.py`（853 行）、
`platform_support.py`（820 行）、`platform_detect.py`（122 行）、`init_workspace.py`（201 行）、
`version_info.py`（124 行）、`version_requirements.py`（77 行）。

对应测试已读：`test_workspace_migrate.py`、`test_updater.py`、`test_host_bootstrap.py`、`test_platform_support.py`、`test_init.py`、`test_version_info.py`、`test_version_requirements.py`。

## 0) 测试运行情况（证据基础）

- `tests/test_updater.py`：40 通过。
- `tests/test_version_info.py` + `tests/test_version_requirements.py` + `tests/test_init.py`：21 通过，2 跳过（Windows 上 CLI init e2e 按设计跳过）。
- `tests/test_workspace_migrate.py`：11 个失败，**均为本机（Windows）环境因素**，非支持平台逻辑错误：
  - `test_run_migrate_happy_path / rollback / resume_* / backup_dir_remapped`：MOVE 阶段 `os.rename` 抛 `[WinError 5] 拒绝访问` —— `_run_migrate_locked` 在 rename 期间保持 registry SQLite 连接打开，Windows 不允许对含打开文件的目录改名（POSIX 语义允许，故 macOS/Linux/WSL 不受影响）。
  - `test_verify_detects_old_path_in_manifest`：`verify_migrate` 用原始文本子串 `old in text` 检查，而 manifest 由 `json.dumps` 写入时反斜杠被转义为 `\\`，单反斜杠的 old 匹配不到（实验确认 `contains old: False`）。
  - `test_cli_*`：CLI 层 `require_supported_platform` 在 Windows 直接 `SystemExit(2)`，符合设计门禁。
- `tests/test_platform_support.py`：5 个失败，同为 Windows 环境因素：`Path("/mnt/c/...").resolve()` 在 Windows 上解析为 `C:\mnt\c\...`，`is_wsl_drvfs_path` 判 False（在 Linux/WSL 上正常）。
- 额外实验确认（python -c，见各条目"证据"）：registry 前缀越界改写损坏；old 为新路径前缀时 verify 误报；`2.40.2-rc1 >= 2.40.2` 返回 True。

结论：父进程在 Linux/WSL 上跑全量应能通过；本报告发现的 bug 均来自代码推演 + 上述实验，与平台无关的部分在支持平台上同样成立。

---

## 1) 每个文件一行总结

| 文件 | 职责 | 结论 |
|---|---|---|
| `workspace_migrate.py` | 工作区迁移事务（preflight/备份/停服/rename/改写/重生/恢复/验收/回滚），journal+锁+快照 | 有疑点（7 条，含 2 条 major） |
| `updater.py` | `lwa update` 热重载编排（pip → 同步 → 配置迁移 → 重启 → doctor） | 有疑点（2 条 minor） |
| `host_bootstrap.py` | 宿主机 Docker/Caddy 安装编排（default/full 档） | 未发现明显问题（仅死代码/文案小瑕疵，见 2.9） |
| `platform_support.py` | 支持平台矩阵、distro/WSL 判定与门禁 | 有疑点（1 条 minor，不确定） |
| `platform_detect.py` | 跨平台识别（macOS/Linux/WSL/Windows） | 未发现明显问题 |
| `init_workspace.py` | `lwa init` 目录/配置/registry 初始化 | 未发现明显问题（1 处极边缘 import 未保护，见 2.10） |
| `version_info.py` | Git/元数据版本解析与展示 | 未发现明显问题（1 处设计权衡备注，见 2.11） |
| `version_requirements.py` | 版本字符串解析与最低版本比较 | 有疑点（1 条 minor：预发布版本放行） |

---

## 2) 发现清单

### workspace_migrate.py

**[major] `workspace_migrate.py:634-638` `rewrite_registry_paths` 前缀改写不设边界，会损坏兄弟目录路径（与 manifest 改写不对称）**
- 说明：`UPDATE ... SET col=REPLACE(col, old, new) WHERE col LIKE old||'%'` 对"以 old 开头的任意字符串"做整体替换，而 `_rewrite_str`（manifest 路径，BUG-403）要求 `value == old` 或 `startswith(old + sep)` 才改写。两者语义不一致：当 registry 里存有与工作区同名前缀的兄弟目录绝对路径（如 `/home/u/lwa-backup/...`，常见于旧迁移备份、`lwa-old` 等）时会被误改成 `/home/u/lwa2-backup/...`；`_list_path_holders`（`:339-341`）的 `LIKE old||'%'` 同样把这些兄弟路径误报为本工作区路径持有者。
- 触发条件：工作区 `/home/u/lwa` 旁存在 `/home/u/lwa-backup` 等前缀共享目录，且其路径出现在 registry 的 `app_path/source_zip_path/compose_path/dockerfile_path/gateway_config_path/log_path`（例如从该目录导入过实例）；或迁移目标恰好是旧路径加后缀（见下条）。
- 影响：registry 内路径被静默改写为错误位置，后续 start/rebuild 指向不存在路径；数据损坏。
- 证据：实验——对含 `/home/u/lwa-backup/apps/x/current` 的库执行 `rewrite_registry_paths(db, "/home/u/lwa", "/data/lwa2")`，结果变为 `/data/lwa2-backup/apps/x/current`；同参数 `_rewrite_str` 正确保留原文。测试 `test_rewrite_str_respects_path_boundary` 只覆盖了 manifest 侧，无 registry 侧等价用例。
- 建议修法：改写前按边界过滤（值等于 old 或以 `old + os.sep` / `old + "/"` 开头）——SQL 侧用 `WHERE col = ? OR col LIKE ?`（`old + '/%'`），或逐行读出、用与 `_rewrite_str` 相同的函数改写后写回。

**[major] `workspace_migrate.py:933`（配合 `:634-638`）old 恰为新路径前缀时：verify 误报"仍含旧路径" + registry 二次改写再损坏**
- 说明：迁移 `/srv/lwa` → `/srv/lwa2`（new 以 old 为前缀）时，rebind 后配置里是 `/srv/lwa2/...`，而 `verify_migrate` 用裸子串 `old in text` 检查"不得再含旧路径"，`/srv/lwa2/...` 天然包含 `/srv/lwa` → 误报迁移失败（工作区实际已搬完、已处于 COMPLETE 阶段）。同时 registry 改写不幂等：若 REBIND 重跑（resume 中断恢复）或 rollback 反向改写（`_rollback_migrate:1355`），`/srv/lwa2` 又匹配 `LIKE '/srv/lwa%'` 被替换成 `/srv/lwa22`。
- 触发条件：新旧路径存在字符串前缀包含关系（如同一父目录下 `lwa` → `lwa2`）。
- 影响：迁移验收错误失败（CLI 报错但目录已搬）；重跑/回滚时 registry 路径二次污染。
- 证据：实验——`verify_migrate(ws, "/srv/lwa", "/srv/lwa2", ...)` 对含 `/srv/lwa2/...` 的 manifest 返回 ok=False 且 notes 含"仍含旧路径"；registry 对已改写值再执行一次 `rewrite_registry_paths` 得到 `/srv/lwa22/...`。
- 建议修法：verify 改用边界感知检查（如 `old + os.sep`、`old + "/"`、正则 `re.escape(old) + r'(?![A-Za-z0-9_./\\-])'`，与 `_rewrite_text_paths` 对齐）；registry 改写保证幂等（见上条）。

**[minor] `workspace_migrate.py:933` `verify_migrate` 用原始文本子串检查路径，Windows 上因 JSON 转义漏报**
- 说明：manifest/配置文件由 `json.dumps` 写出，反斜杠在文件中是 `\\`；`old in text` 用单反斜杠的 old 匹配不到，陈旧路径被漏检。Windows 原生虽被 CLI 门禁挡住，但 `verify_migrate` / `--verify` 作为库函数直接调用时失效。
- 证据：实验——manifest 写入 `{"appPath": "C:\\Users\\...\\old-ws/apps/demo/current"}` 后 `old in text` 为 False；`test_verify_detects_old_path_in_manifest` 在 Windows 上失败（Linux 上反斜杠不参与，不受影响）。
- 建议修法：对 manifest 解析 JSON 后按 `_MANIFEST_PATH_KEYS` 结构比较路径值；对文本类文件保留 `old in text` 并兼容 `\\` 形态。

**[minor] `workspace_migrate.py:1252-1263`（配合 `run_migrate` resume 分支）verify 失败后 `--resume` 直接报成功，不再复验**
- 说明：首次运行 VERIFY 失败时仍推进到 COMPLETE（journal.verify_ok=False）；随后 `--resume` 时 phase=COMPLETE 不满足 `if phase == VERIFY`，`verify_ok` 保持初始化值 True，COMPLETE 块重写 journal 后返回 `ok=True`——journal 里已有的 verify_ok=False 完全被忽略。
- 触发条件：一次迁移 verify 未过（如误报、pageviews 对账异常）后用户按提示执行 `--resume`。
- 影响：报告"成功"但验收实际未复跑，误导用户；`--verify` 可绕过，故影响有限。
- 证据：代码推演（VERIFY 块仅在 `phase == VERIFY` 时执行；resume 分支 `:1163-1172` 把 COMPLETE 也切到新工作区但跳过校验）。本机因 Windows rename 限制无法端到端复现，推演路径与测试 `test_resume_reuses_journal_snapshot` 的 phase 推进逻辑一致。
- 建议修法：resume 时若 journal 中 `verify_ok is False`（或 phase==COMPLETE 且上次 verify 失败），强制重跑 `verify_migrate` 后再决定 COMPLETE。

**[minor] `workspace_migrate.py:597-613` `rewrite_manifest_paths` 恒返回 True，与 docstring"返回是否有变更"不符；`:606-608` 为死代码**
- 说明：无任何变更时也重写文件并返回 True；`if rewritten == data ...: pass` 分支是空操作。当前 `rebind_workspace_paths` 未消费返回值，无实际危害，但语义错误且误导调用方/测试。
- 建议修法：按 `rewritten != data or 容器 id 被清` 返回实际变更标志，删除死分支。

**[minor] `workspace_migrate.py:741-776` `quiesce_workspace` dry-run 动作清单与实际执行不一致**
- 说明：`autostart_disable` 在 dry-run 分支外无条件 append，而 `stop:daemon` / `stop:manager` / `stop:gateway` 只在 `if not dry_run:` 分支内 append；dry-run 预演计划漏列这三项，用户看到的"计划"不完整。
- 建议修法：把三个 stop 动作的 append 移到 dry-run 判断之外（执行仍受 `not dry_run` 保护）。

**[minor] `workspace_migrate.py:1021-1032` + `:272-278` rollback 后旧路径残留迁移锁文件**
- 说明：rollback 分支锁在 NEW（若存在），`_rollback_migrate` 把整个目录 rename 回 OLD，锁文件随目录树回到 OLD/run/；`migrate_lock` 的 finally 仍按 `holder[0]`（NEW 路径）尝试删除——NEW 已不存在，删除静默失败，OLD/run/workspace-migrate.lock 残留。下次运行会按"死 PID"自愈（`_pid_alive` False → 清掉重试），属不彻底清理而非死锁。
- 证据：代码推演；测试 `test_migrate_lock_dead_pid_takeover` 证明死 PID 接管路径存在。
- 建议修法：rollback 流程 move 后更新 `lock_holder[0]` 指向 OLD 的锁路径（与正向 MOVE 的做法一致）。

**[minor] `workspace_migrate.py:1174-1227` 原生 Windows 上 MOVE 必失败（registry 连接未关）且库入口无平台门禁**
- 说明：`reg.open()` 从 BACKUP 前一直持有到 MOVE 后重开，Windows 上对含打开 SQLite 文件的目录 rename 抛 WinError 5。CLI 层有 `require_supported_platform` 门禁（exit 2），但 `run_migrate()` 库入口无任何平台检查，被直接调用时得到难懂的"工作区改名失败"。
- 证据：`test_run_migrate_happy_path` 等在本机失败于 `[WinError 5]`，rename 前未关闭 reg。
- 建议修法：库入口（或 `preflight_migrate`）增加对原生 Windows 的 fail-closed 提示；或 MOVE 窗口内短暂关闭并重开 registry。

### version_requirements.py

**[minor] `version_requirements.py:19-54` 预发布/后缀版本可突破最低版本门禁（semver 语义偏差）**
- 说明：`parse_version_string` 只取前导数字段，忽略 `-rc`/`-alpha`/`-beta`/`+build` 等标签，`_compare` 对 `(2,40,2)` 与 `(2,40,2)` 判等 → `version_ge("2.40.2-rc1", "2.40.2")` 为 True；`"1.0.0-alpha" >= "1.0.0"` 亦为 True。按 semver，预发布应小于正式版。作为"最低版本门禁"是放行方向（fail-open），可能放行未正式发布的组件。
- 证据：实验输出 `pre-release 2.40.2-rc1 >= 2.40.2: True`、`29.0.0-rc2 >= 29.0.0: True`。
- 建议修法：若有前缀标签（`-` 或 `+`）且主版本等于最低版本，判为不满足；或引入 PEP440/`packaging` 比较。

### updater.py

**[minor] `updater.py:929` `run_access_pass` 未包 try/except，是全程唯一未防护的步骤**
- 说明：其余每步失败都记入 StepResult 继续，唯独 `run_access_pass(...)` 异常会直接抛出，`run_update` 无报告返回、无失败步骤记录（diff 失败、registry 被锁、config 异常时都可能触发）。
- 建议修法：仿照其他步骤包 try/except，把异常记入 `accessRefresh`（或 `accessReview`）步骤后继续。

**[minor] `updater.py:322-334` `migrate_config_defaults` 块式键带行尾注释时跳过嵌套补齐**
- 说明：顶层键行形如 `portPool:  # 端口池` 时，`stripped == "portPool:"` 精确匹配失败、`stripped.startswith("portPool:")` 后 remainder 不以 `{` 开头 → `start` 保持 None → 该键缺失的嵌套子键静默不补（顶层缺失键仍会追加）。配置模型加载用 pydantic 默认值兜底，故运行时不缺字段，仅文件层面不完整。
- 建议修法：匹配 `key:` 前缀后允许行尾注释（如按 `key:` 切分取左侧比较）。

### platform_support.py

**[minor·不确定] `platform_support.py:233-252` `detect_wsl_kernel_kind` 末尾无条件 `return "2"`，WSL1 可能被误判为 WSL2**
- 说明：WSL1 且开启 interop（`WSL_INTEROP` 已设，默认开启）时，`:246-249` 分支直接返回 "2"；末行兜底也恒返回 "2"。`collect_platform_support_report` 的 `kernel_kind == "1"` → "WSL1 不受支持" 门禁几乎不会触发。但 WSL1 内核版本（4.4.x）会被 `:677` 内核 ≥5.15 门禁兜住，实际影响有限。
- 建议修法：兜底改为在确证 WSL 但无 WSL2 证据时返回 "1"（fail-closed）或 None + 未知提示。

### host_bootstrap.py

未发现明显问题。仅两处无害瑕疵（不单独计为发现）：`_persist_full_config:707-709` 存在 `if ready and staticGateway == "builtin": pass` 死分支；`maybe_offer_docker_install` 在 Docker 已就绪且 `install_docker=True` 时静默返回空消息。复检/暂停/恢复/退出码逻辑、`daemon_down` 不重装、WSL Desktop 复用、drvfs fail-closed 均与测试意图一致。

### platform_detect.py

未发现明显问题。`is_wsl` 的启发式（WSL_INTEROP / /run/WSL / /proc/version 子串）与 `systemd_available` 的判定符合测试与设计。

### init_workspace.py

未发现明显问题。唯一极边缘点：`_schema_version_safe:143-151` 外层 `CURRENT_SCHEMA_VERSION` import（`:144`）不在 try 内，若 registry.connection 模块 import 失败会抛 NameError——但 `Registry.open()` 已先行导入同模块，实际不可达。幂等、force 语义、模板/skills 复制均正确（测试通过）。

### version_info.py

未发现明显问题。一处设计权衡（非 bug）：`resolve_version:79-88` 优先 Git 提交主题、后取元数据——若仓库 HEAD 落后于已安装 wheel，展示版本会偏旧；但在同一环境的 manager/CLI 中取值一致，`verify_manager_version` 不受影响。

---

## 3) 汇总

- critical：0
- major：2（registry 前缀越界改写损坏；old 为新路径前缀时 verify 误报 + 改写不幂等）
- minor：9（verify 文本检查漏报（Windows）、resume 假通过、rewrite_manifest 恒 True、dry-run 清单不一致、rollback 残留锁、Windows MOVE 无门禁、预发布版本放行、run_access_pass 未防护、块式键注释跳过；另含 1 条不确定：WSL1 误判）

建议优先处理两条 major（迁移事务的数据完整性），随后按 minor 逐项收敛。
