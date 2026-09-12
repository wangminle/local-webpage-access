# 导入预览、构建配置及冗余清理修复计划

目标：完成用户明确列出的 #30、DEV-132 buildEnv 和 #31 遗留项，不提交或重写现有未提交改动。

设计：全新导入不支持 dry-run 时在打开工作区前拒绝，退出码2。配置入口提供 buildEnv 设置/删除、可选 buildBaseFromAlias（构建时从当前别名推导 VITE_BASE，项目需读取该变量）、redundancyAcknowledged 保留标记；这些显式配置在重扫和更新时保留。buildEnv 限定宿主前端构建，容器不支持时配置入口明确拒绝，文档说明边界。冗余清理在实例锁内复核运行/过渡态及独立配置，运行态不受 allowConfigLoss 覆盖；保留标记使实例退出冗余候选。管理页补别名/更新时间、保留操作、配置损失复选框和零可删目标禁用。

实施与验证：
1. tests/test_review_followup_fixes.py 先写参数拒绝、字段校验、配置持久化、动态别名映射、运行保护回归，确认红测。
2. 修改 cli/importing.py、models.py，新增实例配置服务与CLI入口，hosting.py 注入有效构建环境，importer.py 保留字段。
3. lifecycle.py、manager_api.py 和管理页实现清理护栏及交互，同步CLI预览。
4. 更新 faq/known-limitations/manager-page/operations-playbook，定向测试、Ruff、mypy、JavaScript语法及全量pytest验证。
5. task-list.md追加与状态同步，记录测试证据和明确边界。
