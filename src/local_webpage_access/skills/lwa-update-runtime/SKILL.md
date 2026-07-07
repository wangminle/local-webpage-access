# lwa-update-runtime

> 在 **lwa 源代码已更新**（git pull、切换分支、本地改代码）后，刷新安装并重载 Runtime 工作区，使管理页/daemon/CLI 立即生效。

## 何时触发

- 用户刚 `git pull` 或修改了 `local-webpage-access` 仓库代码。
- 管理页版本号与 `lwa version` 不一致，或新功能在 UI 上看不到。
- 用户问：「代码更新了怎么更新 runtime」「怎么热重载」「setup/init 之后怎么 upgrade」。

## 与 setup / init 的区别

| 命令 | 用途 |
| --- | --- |
| `lwa setup` | 检测**宿主机**工具（Docker/Node/Caddy 等） |
| `lwa init` | **首次**创建工作区（目录、registry、配置） |
| **`lwa update`（V0.4.0 起）** | 已有工作区 + **lwa 包升级** + skills/config 同步 + 重启 manager/daemon |

当前已实现 `lwa update` CLI（V0.4.1 为当前版本）；本 skill 应优先调用它。只有在 `lwa update`
执行失败、需要定位具体步骤，或用户明确要求手动处理时，才使用下方手动兜底步骤。

## 输入

1. lwa 源码目录路径（含 `pyproject.toml` 的仓库根）。
2. Runtime 工作区路径（含 `local-web.yml`，如 `runtime/`）。
3. （可选）是否需重启业务实例（仅当 hosting/网关/import 逻辑变更时）。

## 输出

- 向用户返回可执行清单与预期结果（新版本号、管理页 URL）。
- **不**删除 `apps/`、`registry/` 中的实例数据。

## 推荐流程（V0.4.0 起）

```bash
cd /path/to/runtime
lwa update

# 托管/网关/import 行为变更后，才额外重启业务实例：
lwa update --restart-instances

# 已手动安装包、只想同步工作区附属物时：
lwa update --skip-pip

# 需要机器可读摘要时：
lwa update --json
```

预期结果：

- `lwa version` 与管理页 `/api/health` 的 `version` 一致；
- 工作区 `skills/` 已同步新增/更新的内置 skill；
- 新增配置字段已非破坏性补齐，并在需要时生成 `.bak`；
- manager / daemon 仅在原本启用或运行时重启；
- 默认不重启业务实例，除非显式传 `--restart-instances`。

## 手动兜底流程

```bash
# ── A. 刷新 lwa Python 包（editable 安装）──
cd /path/to/local-webpage-access
pip install -e .

# ── B. 同步 skills 到工作区（可选，与 init 行为一致）──
# 可手动从 src/local_webpage_access/skills/ 复制新增 SKILL
# 到 runtime/skills/，勿删用户自定义 skill。

# ── C. 重启 lwa 自有后台服务（必须，否则仍跑旧代码）──
cd /path/to/runtime   # 含 local-web.yml 的目录
lwa manager off && lwa manager on
# 若使用过 inbox 自动导入：
lwa daemon off && lwa daemon on

# ── D. 业务实例（默认不必重启）──
# 仅当静态网关、import、构建逻辑变更时：
lwa restart <instance-id>

# ── E. 校验 ──
lwa version          # 应与 Git 最新 commit 主题 V0.x.x 一致
lwa doctor
curl -s http://127.0.0.1:17800/api/health   # version 字段应已更新
```

## 禁止事项

- **不要**对业务实例执行 `remove --purge` 作为「更新」手段。
- **不要**在无备份需求时 `lwa init --force` 覆盖整个工作区配置。
- **不要**假设 `pip install -e .` alone 足够——必须通过 `lwa update` 或手动命令重启 **manager/daemon 子进程**。

## 故障排查

| 现象 | 处理 |
| --- | --- |
| 管理页仍显示旧版本 | 先运行 `lwa update`；若仍异常，再确认 `lwa manager off/on` 已执行并检查 17800 端口 PID |
| `lwa version` 已新但页面旧 | 浏览器强刷；确认访问的是本机 127.0.0.1 而非旧 tab 缓存 |
| 代码变更后实例行为异常 | `lwa restart <id>` 或 `lwa update --restart-instances` |

## 相关文档

- [待改进 IMP-008](../../../../docs/plan/待改进功能点记录-20260706.md)
- [Runtime 工作区说明](../../../../docs/runtime-workspace.md)
- [lwa-setup-host-environment](../lwa-setup-host-environment/SKILL.md)
