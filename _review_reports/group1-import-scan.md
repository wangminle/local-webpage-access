# LWA 代码审查报告（第 1 组：导入/扫描链路）

审查范围：`src/local_webpage_access/` 下 11 个文件（importer / scanner / evidence_collector / candidate_generator / preflight / package_classifier / compatibility_checker / zip_processor / folder_source / import_activity / probe），并交叉核对了 `models.py`、`security.py`、`file_lock.py`、`paths.py`、`registry/dao.py`、`dockerfile_templates.py`（辅助函数）与相关测试。
方法：逐文件完整阅读（read，大文件分段读完）+ 针对性 `python -c` 推理验证 + 运行单测文件（`test_import_activity.py`、`test_zip_processor.py`、`test_folder_source.py`、`test_scanner.py`、`test_preflight.py` 均通过；`test_folder_source.py` 中 1 个 CLI 用例因"Windows 原生不支持 manager"环境限制失败，与代码无关）。
结论：未发现 critical 级问题；发现 major 级 2 项、minor 级 16 项。

---

## 1) 每个文件一行总结

- **probe.py**（内部 HTTP 探针标记 `__lwa_probe=1` + 直连 opener）：职责单一、实现正确——未发现明显问题。
- **import_activity.py**（跨进程 `import.lock` 互斥 + 同线程可重入）：主体正确、测试全过；有 2 处 minor（获取锁异常路径 fd 泄漏；`wait_until_import_idle` 释放探测锁后存在 TOCTOU 窗口）。
- **folder_source.py**（源目录校验/打包/指纹）：逻辑与 zip 剥离规则对齐；有 2 处 minor（dest_zip 在源目录内的自打包未防御；hash 与 pack 分离执行的轻微 TOCTOU；仅拒绝"源在工作区内"）。
- **zip_processor.py**（zip 校验/哈希/安全解压）：zip-slip、symlink、炸弹防护扎实（反斜杠穿越已实测拦截），测试全过；1 处 minor（`zf.extract` 异常未包装，加密/损坏 zip 错误信息丢失）。
- **candidate_generator.py**（Gate-B 候选 + Gate-C 计划生成）：结构清晰；有 4 处 minor（bun.lockb→npm ci 错误、无 lockfile 也 npm ci、与 scanner 的 Node 默认端口不一致、plan 组件端口仅存 manifest）。
- **preflight.py**（Layer 2 静态预检 CHK-V01~V06）：主体正确、测试全过；1 处 minor（CHK-V02 单引号包裹会拆碎含单引号的命令）。
- **package_classifier.py**（monorepo 6 值分类 + 选主）：分类规则与决策表实现一致；有 2 处疑点（workspaces 指向仓库外目录时越界路径未被过滤——major 链路的源头；glob 只展开第一个 `*`）。
- **compatibility_checker.py**（Gate-2 兼容性预检 CHK-P03/P04）：正则与扫描逻辑正确；1 处 minor（嵌套 dist/build 噪音目录未被排除）。
- **evidence_collector.py**（Layer 0 证据收集）：实现正确；1 处 minor（`isRelative` 对 Windows 盘符绝对路径误判）。
- **scanner.py**（项目类型识别）：主流程与 BUG-181/322 等修复点一致、测试全过；有 3 处疑点（非法端口不置 pending——major；summarize 顶层 iterdir 无异常防护；monorepo 无 start 脚本时 `node server.js` 兜底可能跑错 cwd）。
- **importer.py**（zip/文件夹导入与原地更新）：原子换入 + 回滚 + 锁设计严谨；有 3 处 minor（非法端口静默兜底 8000——与 scanner 合为 major #2；folder 导入返回的 manifest 未含 folder 元数据；update_zip 锁外 IO 裸抛）。

---

## 2) 发现清单

### [major]

**1. package_classifier.py:84-102（+ scanner.py:575 + hosting.py:1591-1598）— monorepo workspaces 指向仓库外目录时越界路径未过滤，可致构建产物目录逃逸 current/**
- 说明：`detect_workspaces` 对 workspaces 中的 `"../shared"` 这类**合法但指向仓库外**的 npm 模式不校验解析后是否仍在 `root` 内。Python 3.12+ 的 `Path.relative_to`（非严格模式）对越界目录会返回 `'../shared'` 而非抛错，该字符串被当作子包路径。若该外部包被分类为 `frontend_build`/web_server 并被选为主包，`scanner._apply_workspace_entry`（scanner.py:574-575）会把 `entry.buildOutputDir = "../shared/dist"` 写入 manifest；`hosting.find_build_output` 对 hint 只做 `project_dir / hint` 拼接，**无 containment 校验**，命中即把仓库外目录内容经 `sync_dir` 复制进 `public/` 对外服务（信息暴露）。
- 触发条件：用户仓库的 `package.json` 声明 `workspaces: ["../shared"]`（npm 允许）且外部目录存在 `package.json`（可被分类为主包）。少见但合法输入。
- 证据：实测 `detect_workspaces(root)` 对 `workspaces:['../shared']`（外部目录存在）返回 `['../shared']`；`classify_and_select` 正常返回 `is_monorepo=True`。`hosting.find_build_output`（hosting.py:1592）与 `manifest.sourceSubdir` 的 `validate_source_subdir`（paths.py:97，能挡住 `..`）不是同一校验面——`buildOutputDir` 无任何校验。
- 建议修法：`detect_workspaces` 展开/解析后统一 `resolve()` 并过滤所有不在 `root` 内的包；或对 `entry.buildOutputDir`（及所有写入 manifest 的相对路径字段）复用 `validate_source_subdir` 语义做 `..` 段与绝对路径拒绝。
- 标注：严重度按"路径逃逸"判 major；实际被选主且 build 成功的概率依赖外部包内容，属防御纵深缺口而非必然利用。

**2. scanner.py:695,716 + importer.py:1652 — Node 后端声明非法端口时 internalPort=None 但 confidence=high、不置 pending，importer 静默兜底 8000**
- 说明：`_infer_node_port` 对非法端口（如 `PORT=99999`）返回 `None`，BUG-322 注释明确写"非法端口 → None（留给 pending/人工确认）"，但 `_detect_node` 后端分支（scanner.py:687-701）在 `internalPort=None` 时仍置 `confidence="high"`、`pending=False`；`build_manifest_from_detection`（importer.py:1652）随后用 `detection.internalPort or 8000` 静默替换为 8000。容器 `EXPOSE`/探针端口与真实监听端口不一致，部署后探针失败，错误提示还会误导为 8000。
- 触发条件：Node 后端 `package.json` 的 start 脚本显式声明越界端口。
- 证据：实测 `Scanner()._detect_node`（express + `start: "PORT=99999 node server.js"`）→ `internalPort=None, confidence='high', pending=False`；`test_scanner.py:210` 仅断言 `internalPort is None`，未断言 pending。
- 建议修法：`_detect_node` 在 `internalPort is None` 时置 `pending=True`/降 confidence（兑现 BUG-322 注释），或 importer 在 `detection.internalPort is None` 时拒绝/询问而非静默兜底。

### [minor]

**3. candidate_generator.py:442 — `bun.lockb` 被映射为 `npm ci`（无 package-lock.json 时必失败）**
- 说明：`_node_install_command` 把 `bun.lockb` 与 `package-lock.json` 并列返回 `"npm ci"`；纯 Bun 项目没有 package-lock.json，`npm ci` 直接报错，构建失败。
- 证据：实测 `_node_install_command(ProjectEvidence(rootFiles=['bun.lockb']))` → `'npm ci'`。
- 建议修法：`bun.lockb` 单独分支（`bun install` 或回退 `npm install`）；scanner 侧 `_node_install_command` 未识别 bun.lockb（回退 npm install），两侧应一致。

**4. candidate_generator.py:348-353 — Node 子目录候选以 `bool(nodeDeps)` 当"有锁文件"决定 `npm ci`**
- 说明：`has_lock = bool(signal.nodeDeps)` 是"有依赖"而非"有 lockfile"；子目录只有 package.json 无锁文件时仍生成 `npm ci`，构建必失败。scanner 子目录流程会覆写 install（`_node_install_command` 查锁文件），但 Gate-C `generate_plans` 直接消费该候选。
- 建议修法：检查子目录是否存在 `package-lock.json`/`pnpm-lock.yaml`/`yarn.lock` 再决定 `npm ci`/`npm install`。

**5. candidate_generator.py:334,365 vs scanner.py:695（默认端口双真相）— express 无显式端口时 internalPort 不一致（8080 vs 3000）**
- 说明：`_make_node_root_candidate`/`_make_node_subdir_candidate` 默认 `internalPort=8080`，scanner `_infer_node_port` 默认 3000（test_scanner.py:146 断言 3000）。运行时 compose/dockerfile/探针都以 `manifest.container.internalPort`（scanner 值）为准，plan 内 8080 仅存 `manifest.deploymentPlans`，当前无实际危害，但两份"真相"漂移易在后续改动中踩坑。
- 建议修法：candidate 复用 scanner 的 `_infer_node_port`/`_node_install_command`，消除重复实现。

**6. preflight.py:364 — CHK-V02 `sh -c '{start}'` 单引号包裹会把含单引号的命令拆碎**
- 说明：自动修正直接 `f"sh -c '{start}'"`，start 内含单引号时引号不配对，生成非法 shell 命令。
- 证据：实测 `_check_cmd_safety` 对 `start="python -c 'print(1);print(2)'"` 输出 `sh -c 'python -c 'print(1);print(2)''`。
- 触发条件：用户编辑过 `entry.start` 且含单引号（scanner 生成的命令均无单引号，属潜在缺陷）。
- 建议修法：用 `shlex.quote(start)` 或改写为 `sh -c "..."` 并转义内部引号。

**7. import_activity.py:56-75 — 获取锁过程中非 BlockingIOError 异常路径 fd 泄漏、已获锁不释放**
- 说明：`ensure_lockable`/`write_lock_payload` 抛 `OSError`（如 ENOSPC）时既不 `os.close(fd)` 也不释放已获取的排他锁，仅靠 CPython 引用计数 GC 兜底关闭 fd；非引用计数解释器下锁会滞留。
- 建议修法：整个获取循环用 `try/finally` 收尾（失败即 close；已获锁先 `release_exclusive`）。

**8. import_activity.py:94-129 — `wait_until_import_idle` 释放探测锁后才返回，与真正重启之间存在 TOCTOU 窗口**
- 说明：探测成功（`release_exclusive` 后立即返回）与 updater 实际重启 manager/daemon 之间，导入可以启动并被打断；这是"探测而非持有"的设计权衡，窗口极小，但注释宣称的"避免打断进行中的导入"并非严格保证。
- 建议修法：若可接受，改由 updater 在重启窗口内持有 `import_activity_lock`（让导入等待而非被打断）；或文档明示弱保证。

**9. scanner.py:104 — `summarize` 顶层 `root.iterdir()` 无异常防护**
- 说明：`evidence_collector.collect` 对同一操作有 `try (PermissionError, OSError)` 防护，scanner 没有；对无读权限目录执行 `lwa scan`/识别会直接抛 PermissionError（导入流程中 current/ 为自建目录，不受影响）。
- 建议修法：与 evidence_collector 对齐，包 try/except 或记录 warning 后返回空摘要。

**10. scanner.py:561-567 — monorepo web_server 无 start/server 脚本时保留 `node server.js` 兜底，cwd 可能不在主包子目录**
- 说明：`_apply_workspace_entry` 对无 `start`/`server` 脚本的主包保留 `_detect_node` 的兜底 `start="node server.js"`，但该命令在仓库根 cwd 执行，而 `server.js` 位于 `packages/<name>/`；若 Dockerfile/容器未设置子包 workdir，启动即失败（"Cannot find module"）。
- 标注：未验证 monorepo Dockerfile 是否对主包设置 workdir，结论不确定；若无 workdir，建议无 start/server 脚本时置 pending 或改用 `-w` 语义提示。

**11. zip_processor.py:206-207 — `zf.extract` 无异常包装，加密/损坏成员抛裸异常**
- 说明：加密 zip 的成员 `zf.extract` 抛 `RuntimeError("...encrypted...")`、损坏成员抛 `BadZipFile`/`OSError`，均不转换为 `ZipImportError`；importer 虽会兜底包装为通用"导入失败：...",但错误分类与路径信息（`member=`）丢失。
- 建议修法：`safe_extract` 内对 extract 循环包 try/except，把 `BadZipFile`/`RuntimeError`/`OSError` 统一转 `ZipImportError`。

**12. compatibility_checker.py:154-171 — `_walk_source` 只精确匹配顶层排除目录，嵌套 dist/build 仍被扫描**
- 说明：`rel` 以扫描根为基准，`rel.lower() in skip or startswith(s+"/")` 只排除顶层命中；嵌套子目录（如 `sub/dist`）中的噪音目录会被扫描，与模块注释"排除 dist/build 等噪音目录"意图不符，大仓易撞 `_MAX_FILES=500` 上限并产生噪音 finding。
- 建议修法：按路径段匹配（任意层级的 dist/build/node_modules 均跳过）。

**13. folder_source.py:118-170 — `pack_source_dir` 未防御 dest_zip 位于源目录内（自打包）**
- 说明：若调用方把 `dest_zip` 传成源目录内路径，`os.walk` 会遍历到正在增长的 zip 并尝试写入自身（异常或膨胀）；当前内部调用方均传系统临时路径，属防御性缺口。另：`compute_source_hash` 与 `pack_source_dir` 分两次执行，两调用间文件变更会使指纹与 zip 内容短暂不一致（下次更新自愈，无害）。
- 建议修法：`dest_zip` resolve 后与 `source_dir` 做包含性校验，拒绝在源内；或把 hash 计算改为基于打包结果。

**14. folder_source.py:103-113 — 仅拒绝"源位于工作区内"，不拒绝"工作区位于源内"**
- 说明：用户选工作区的祖先目录（如主目录）作为源时，打包会把整个 LWA 工作区（apps/、registry/ 等）一并导入实例。属设计权衡（用户显式选择的目录），但与"红线：源目录只读、防止把工作区自身当源"的相邻意图存在缺口。
- 建议修法：提示而非强制拒绝（如检测到源内含工作区特征目录时 warning）。

**15. evidence_collector.py:402 — `isRelative = not default_url.startswith("/")` 对 Windows 盘符绝对路径误判**
- 说明：`sqlite:///C:/data/x.db` 不以 `/` 开头被判为相对路径；Windows 上 config 中的盘符绝对 SQLite URL 会被错误标记。A.R01 依赖此标志决定注入策略，误判只影响提示语义。
- 建议修法：用 `Path(default_url).is_absolute()` 或同时识别 `X:/` 前缀。

**16. importer.py:1076-1081 — `import_from_dir` 返回的 `ImportResult.manifest` 不含 folder 元数据**
- 说明：`sourceKind/sourceDirPath/sourceSyncHash` 在 `_import_zip_locked` 返回后才写盘，返回值里的 manifest 仍是 `sourceKind="zip"` 的旧对象；调用方若直接读 `result.manifest` 会看到与磁盘不一致的状态。
- 建议修法：写盘后重新 `InstanceManifest.load` 再返回（或就地更新返回对象的字段）。

**17. importer.py:766-779 — `update_zip` 的锁内前置 IO（manifest 快照读取、registry 快照）位于 try 之外**
- 说明：`manifest_path.read_bytes()`/`get_resources` 抛 OSError 时直接裸抛，不会走统一的 `ZipImportError("更新失败")` 包装，异常类型与其余路径不一致。
- 建议修法：把快照读取移入 try，或包一层转换成 ZipImportError。

**18. package_classifier.py:84-94 — glob 展开只处理首个 `*`**
- 说明：`pattern.split("*")[0]` 只取第一个通配符之前的目录做一层展开，`"apps/*/packages/*"` 只匹配 `apps/*` 层；`**` 退化等同 `*`；负模式（`!` 排除）不支持。npm 合法模式下会漏检部分子包（分类退回 unknown/pending）。
- 建议修法：按段展开（支持任意层通配）或明确文档化限制。

---

## 3) 无问题文件

- **probe.py**：未发现明显问题（幂等标记、直连 opener、fragment/query 保留均正确）。
- **models.py**：未发现明显问题——`InstanceManifest` 的 `sourceZipHash` 经 `extra="allow"` 存取、`sourceSubdir` 校验、`runtime/servingMode` 一致性校验均正常；`ProbeSpec`/`CapabilityContract`/`CommandSpec` 字段与 candidate_generator、hosting 消费方一致。
- **file_lock.py**：未发现明显问题（Windows `msvcrt.locking` 与 POSIX `flock` 分支正确，`ensure_lockable` 的 1 字节占位与 `write_lock_payload` 的 ftruncate 配合正确）。
- **security.py**（zip 审计/剥离部分）：未发现明显问题——`audit_zip_members` 对 `..`、绝对路径、盘符、`S_IFLNK`、反斜杠形态均能拦截（反斜杠穿越已实测被 `zip_slip` 拒绝）；`sanitize_zip_members`/`_strip_rule_for` 的剥离规则与 zip_processor、folder_source 的跳过集合对齐。

## 4) 测试佐证

- `tests/test_import_activity.py` + `tests/test_zip_processor.py`：17 passed。
- `tests/test_folder_source.py`（排除 1 个 CLI 环境用例）：49 passed。
- `tests/test_scanner.py` + `tests/test_preflight.py`：全部 passed。
- 实测推理脚本（python -c，未落盘）：见各发现条目证据。
