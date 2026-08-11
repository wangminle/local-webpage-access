# 创建 Scanner 架构设计分析文档

## 目标
在 `design/plans/` 下创建一份与现有 `导入预检与Monorepo识别增强-20260810.md` 同级、同等详细程度的架构设计文档，分析当前 scanner"即时贪婪判定"范式的问题，并提出"多候选 + 实证校验"的新范式完整设计。

## 文件
- 新建：`design/plans/Scanner架构设计分析-多候选与实证校验-20260811.md`

## 文档结构（15 章，约 2500-3000 行）

### 第一部分：问题分析（§1-§4）

**§1 背景与触发案例**
- home-bookshelf-management-v1 部署全过程复盘（作为实证锚点）
- 调试过程中暴露的 5 层卡点链：识别→COPY→CMD→cwd→镜像重建
- 每一层"看似成功实则失败"的假信号分析
- 一句话根因：scanner 缺少"布局"维度

**§2 当前架构剖析（带代码锚点）**
- scanner.py 的判定流程：summarize() → detect() → _detect_*() 的贪婪即时分支
- 数据流：DetectionResult（单值）→ build_manifest_from_detection（单值）→ manifest（单值）
- "识别成功"与"能跑起来"之间无验证关系的精确论证
- 当前架构的 5 个隐含假设表格（每个假设 + 对哪些项目不成立 + 代码位置）

**§3 失败模式分类（核心创新——系统化而非零散）**
按"识别器的知识盲区"分类，而非按 bug 编号：
- A 类：未知布局（代码在子目录、monorepo、前后端分仓）
- B 类：命令语义盲区（&&/|| 被 exec 拆碎、entrypoint 脚本引用、环境变量前缀）
- C 类：路径相对性盲区（SQLite 相对路径、alembic script_location、COPY 源路径）
- D 类：运行时一致性盲区（cwd 编排、DATABASE_URL 注入、镜像重建与 CMD cache）
- E 类：生命周期盲区（Dockerfile 覆盖无感知、轻量 start 跳过 build、别名孤儿残留）
每类给出：特征 → 现有代码哪里不设防 → 表现症状 → home-bookshelf 中的实证

**§4 为什么"打补丁"修不好**
- 当前 BUG-4xx 系列的修复模式：每个 bug 针对性加一个 if/sh -c 包裹
- 这种模式的累积成本：scanner.py/dockerfile_templates.py 里越来越多的特殊分支
- 论证：需要范式转换而非增量补丁，但也要诚实说明范式转换的代价

### 第二部分：新范式设计（§5-§9）

**§5 范式对比：即时判定 vs 候选校验**
- 当前范式的时间线图（识别→登记→start→失败→人工排查）
- 新范式的时间线图（证据收集→候选生成→静态预检→实证校验→收敛/降级）
- 核心差异：从"一个结论"到"有序候选列表 + 验证循环"

**§6 四层模型详设**

*Layer 0：证据收集（毫秒级）*
- 目标：收集所有客观事实，零解释
- 证据类型清单：文件布局证据、依赖证据、脚本证据、数据库证据、路径证据
- 数据结构：`ProjectEvidence` dataclass 精确定义（含每个字段的采集方式和代码锚点）
- 与现有 `FileSummary` 的关系：FileSummary 是 Layer 0 的子集，需扩展

*Layer 1：候选生成（毫秒级）*
- 目标：证据 → 有序候选列表
- 候选配置数据结构：`DeploymentCandidate` 精确定义（kind/runtime/form/entry/sourceSubdir/confidence/reasoning）
- 候选生成规则表：哪些证据组合 → 哪些候选（含子目录布局、monorepo、传统单根目录）
- 置信度算法：不是精确数值，而是有序分组（primary / alternate / fallback）
- 多候选决策矩阵：0/1/≥2 个 primary 候选的行为

*Layer 2：静态预检（毫秒级，最高性价比）*
- 目标：不 build，快速淘汰/修正不可行候选
- 预检项清单（每项给出检查逻辑 + 自动修正策略 + 对应 BUG-4xx）：
  - CHK-V01：COPY 源路径文件存在性（修正：加 sourceSubdir 前缀）
  - CHK-V02：CMD shell 操作符安全（修正：sh -c 包裹）— 对应 BUG-471
  - CHK-V03：DB 路径与 volume 挂载点一致性（修正：注入绝对路径 DATABASE_URL）— 对应 BUG-474
  - CHK-V04：alembic script_location 可达性（修正：编排 cwd 序列）— 对应 BUG-474
  - CHK-V05：entrypoint 脚本 COPY 完整性
  - CHK-V06：项目自带 Dockerfile 检测（警告/保留）— 对应 BUG-472
- 预检结果数据结构：`PreflightResult`（passed/autofixed/warned/rejected + 修正动作列表）
- home-bookshelf 走查：Layer 2 如何自动解决 5 层卡点中的 4 层

*Layer 3：实证校验（分钟级，可选）*
- 目标：实际 build + start + health check 确认可行性
- 触发条件：Layer 2 无法确定唯一候选、或用户显式请求
- 失败降级：build 失败/健康检查失败 → 标记候选失败 → 尝试下一候选
- 成本控制：超时、build 失败快速退出、缓存已验证候选
- 与现有 host_container 流程的关系：host_container 本身就是 Layer 3 的单候选版本

*Layer 4：收敛诊断*
- 全部候选失败时的诊断输出
- 每个候选失败在哪一步、错误摘要、修复建议
- 数据结构：`DiagnosisReport`
- 写入 manifest 的 `compatibilityFindings` / `lastError`

**§7 数据模型变更**
- `DetectionResult` → 新增 `candidates: list[DeploymentCandidate]`、`evidence: ProjectEvidence`、`preflight: PreflightResult | None`
- 向后兼容：旧字段（kind/runtime/entry 等）保留，取 candidates[0] 的值
- manifest 新增可选字段：`deploymentCandidates`（fallback 候选）、`preflightResults`
- 与 IMP-056 `compatibilityFindings` 的关系：预检结果写入同一字段，互补不冲突

**§8 模块架构**
- 新增模块：`evidence_collector.py`（Layer 0）、`candidate_generator.py`（Layer 1）、`preflight.py`（Layer 2）
- scanner.py 重构：detect() 改为编排 Layer 0→1→2，保留旧接口兼容
- importer.py 接入：import 后存候选列表，start 时消费
- lifecycle.py / hosting.py 接入：start 失败时降级到下一候选（Layer 3）
- 模块依赖图

**§9 与现有设计计划的关系**
- 与 IMP-057（Monorepo 识别）的关系：IMP-057 的候选生成是 Layer 1 的一个规则集
- 与 IMP-056（兼容性预检）的关系：IMP-056 的 CHK-P03/P04 是 Layer 2 的预检项
- 融合策略：本文档定义框架，IMP-057/056 填充具体规则

### 第三部分：实施路线（§10-§12）

**§10 三阶段渐进路径**
- 阶段 1（Gate-A）：Layer 2 静态预检 — 不改 scanner 输出模型，识别后加预检道
  - 解决 BUG-471（CMD 拆碎）、BUG-474（SQLite 路径）
  - 改动面：新增 preflight.py，scanner.detect() 末尾调用
  - Gate 退出标准：home-bookshelf 预检自动修正全部问题
- 阶段 2（Gate-B）：Layer 0+1 多候选识别 — scanner 重构
  - 解决 DEV-105（子目录识别）
  - 改动面：scanner.py 重构、新增 evidence_collector/candidate_generator
  - Gate 退出标准：backend/+frontend/ 布局自动识别为 python 候选
- 阶段 3（Gate-C）：Layer 3+4 实证校验循环 — start 失败自动降级
  - 改动面：lifecycle.py / hosting.py 接入候选降级
  - Gate 退出标准：识别错误时自动尝试 fallback 候选并成功

**§11 WBS（每阶段可执行任务表）**
- 与 IMP-057 文档的 WBS 格式一致：ID / 任务 / 规模 / 文件 / 验收标准 / 依赖

**§12 测试策略**
- 每层的测试方法不同：Layer 0 测事实采集、Layer 1 测候选排序、Layer 2 测预检修正、Layer 3 测 build 验证
- 回归矩阵：现有项目（纯静态、根目录 Flask、根目录 Vite）行为不变
- 新增夹具：home-bookshelf 类子目录全栈项目夹具

### 第四部分：风险与开放问题（§13-§15）

**§13 风险与缓解**
- 性能：多候选 build 的最坏情况耗时
- 复杂度：候选列表对 manifest/CLI/管理页的影响
- 确定性：预检自动修正可能改变用户预期
- 与现有 470 个 BUG 修复的冲突

**§14 开放问题**
- 候选降级是自动还是需用户确认？
- Layer 3 在 import 时触发还是 start 时触发？
- 预检修正应写回 manifest 还是临时生效？

**§15 编号与追踪**
- IMP-058（建议）：Scanner 多候选与实证校验架构
- 与 BUG-471~474、DEV-105 的关系
- task-list.md 登记 DEV 条目的建议

## 写作原则
1. **每个论断都有代码锚点**（file:line）或实证来源（home-bookshelf 部署记录）
2. **数据结构用 Python dataclass 伪代码**精确定义，不是模糊描述
3. **诚实标注代价**：范式转换的成本、与现有架构的冲突、不确定的开放问题
4. **不重复 IMP-057 已有的内容**：monorepo 分类规则引用 IMP-057，本文聚焦框架
5. **格式与 IMP-057 对齐**：章节编号、表格风格、WBS 格式、风险表格式

## 不写代码
本文档是纯设计文档，不包含实现代码（除了数据结构的伪代码定义）。文档完成后，按 AGENTS.md 约定登记 DOC 条目到 task-list.md。