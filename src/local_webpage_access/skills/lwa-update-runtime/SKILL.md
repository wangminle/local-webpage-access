---
name: lwa-update-runtime
description: >-
  Refresh an installed lwa package and runtime workspace after source changes, then synchronize built-in skills and safely reload manager and daemon. Use after git pull, branch switches, or local code edits, when CLI and manager versions differ, or when new behavior is not visible in the runtime.
---

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

当前已实现 `lwa update` CLI（**V0.8.1** 为当前版本）；本 skill 应优先调用它。只有在 `lwa update`
执行失败、需要定位具体步骤，或用户明确要求手动处理时，才使用下方手动兜底步骤。

## 输入

1. lwa 源码目录路径（含 `pyproject.toml` 的仓库根）。
2. Runtime 工作区路径（含 `local-web.yml`，如 `runtime/`）。
3. （可选）是否需重启业务实例（仅当 hosting/网关/import 逻辑变更时）。

## 输出

- 向用户返回可执行清单与预期结果（新版本号、管理页 URL）。
- **不**删除 `apps/`、`registry/` 中的实例数据。

## 推荐流程（IMP-063 一键更新通道）

**注意：** 管理页/CLI 正在导入（文件夹或 zip）时，不要立刻 `lwa update`——重启会打断导入。
`lwa update` 会在重启 manager/daemon 前最多等待约 180s；若仍忙则跳过重启并记失败步骤。

**`lwa update` 已内置源码更新**（IMP-063）：自动完成
`fetch → 固定候选 OID → --ff-only 安全快进 → pip install -e . → 新解释器接力执行 Runtime 后半段`。
**不再需要先人工 `git pull`**；代理/凭据沿用 git 自身机制（如 `https_proxy` 环境变量）。

```bash
cd /path/to/runtime
lwa update            # 一键：源码快进 + pip + 同步 + 重启 + 复核

# 只看远端有什么新版本（不要求已 init 工作区；fetch 但不改工作树）：
lwa update --check [--repo /path/to/lwa] [--json]

# 零写入预览（不联网、不 fetch、不取锁）：
lwa update --dry-run

# 离线 / 代理不可用时的逃生舱（仅用本地代码刷新 Runtime，语义与旧版一致）：
lwa update --no-pull

# 无 upstream 的仓库须显式给全目标：
lwa update --remote origin --ref main

# 托管/网关/import 行为变更后，才额外重启业务实例：
lwa update --restart-instances

# 需要机器可读摘要时：
lwa update --json

# 跳过升级后的访问复核（仍会 refresh 地址）：
lwa update --no-review-access

# 跳过重启 gateway（仅当确认 Caddy master 无需加载本次升级时）：
lwa update --no-restart-gateway
```

安全边界与降级语义：

- 只做 **fast-forward**；tracked 文件有本地修改、与远端分叉、detached HEAD、
  浅克隆历史不足时**拒绝快进**（工作树零改动），报告结构化 `errorKind` 与下一步指引；
- 断网 / 代理失效 / 凭据问题 → `sourceUpdate warning`，**离线可用**：仍以本地代码完成
  pip 与 Runtime 刷新；
- 远端分支不存在 / `--ref` 传了 tag 或 commit → 结构化报错（MVP 只接受远端分支）；
- `--skip-pip` **只允许用于不改 HEAD 的路径**：检测到落后又传 `--skip-pip` 会在快进前
  拒绝（`skip_pip_conflict`），避免新旧代码混跑；`--no-pull --skip-pip` 组合仍可用；
- 快进后 pip / 接力失败：停止 Runtime 后半段并给出人工恢复链
  （`git status` 复查 → 干净时 `git reset --keep <oldHead>` → 重跑 `lwa update`），
  **不自动回滚**；
- 两个 `lwa update` 并发：repo/workspace 双锁 fail-fast，后者直接报「更新锁被占用」。

预期结果：

- `lwa version` 与管理页 `/api/health` 的 `version` 一致；
- 工作区 `skills/` 已同步新增/更新的内置 skill；
- 新增配置字段已非破坏性补齐，并在需要时生成 `.bak`；
- manager / daemon / gateway 按**三态 reconcile**（IMP-059）：运行中 → 重启；
  enabled 但意外未运行 → 自动拉起并标注「意外未运行（中断约 X），已恢复」；
  enabled=false → 跳过（`--no-reconcile` 可关掉拉起语义，仅排障用）；
- **自启单元在管时**由监督器重启/拉起（`kickstart -k` / `systemctl restart`），不 stop+detached spawn，避免与 KeepAlive 抢锁；
- 默认不重启业务实例，除非显式传 `--restart-instances`；
- **升级收尾（IMP-038）**：后台重启后自动 **access refresh**，并默认跑一次轻量 **access review**（`--no-review-access` 可跳过 review）；Full Profile 收尾额外验收合并后的能力缓存；访问复核细节见 Skill [`lwa-review-access-urls`](../lwa-review-access-urls/SKILL.md)。

> 非 git 克隆安装（如 release zip 解包）：`sourceUpdate skipped` 并提示迁移到
> clone + `pip install -e .`；不会自动 clone。

## 手动兜底流程

仅当 `lwa update` 失败或用户明确要求逐步操作时使用。**自启在管时优先继续用 `lwa update`，不要手搓 `off && on`。**

```bash
# ── A. 刷新 lwa Python 包（editable 安装）──
cd /path/to/local-webpage-access
pip install -e .

# ── B. 同步 skills 到工作区（可选，与 init 行为一致）──
# 可手动从 src/local_webpage_access/skills/ 复制新增 SKILL
# 到 runtime/skills/，勿删用户自定义 skill。

# ── C. 重启 lwa 自有后台服务（必须，否则仍跑旧代码）──
cd /path/to/runtime   # 含 local-web.yml 的目录
# 若已 lwa autostart install：先 disable，再 off/on；否则 KeepAlive 会立刻拉回
lwa autostart disable
lwa manager off && lwa manager on
# 若使用过 inbox 自动导入：
lwa daemon off && lwa daemon on
# 需要继续自启：lwa autostart enable

# ── D. 业务实例（默认不必重启）──
# 仅当静态网关、import、构建逻辑变更时：
lwa restart <instance-id>

# ── E. 访问地址收尾（手搓 off/on 不会自动跑 IMP-038）──
lwa access refresh
lwa access review            # 或 lwa doctor --access

# ── F. 校验 ──
lwa version          # 应与 Git 最新 commit 主题 V0.x.x 一致
lwa doctor
curl -s http://127.0.0.1:17800/api/health   # version 字段应已更新
```

## 禁止事项

- **不要**对业务实例执行 `remove --purge` 作为「更新」手段。
- **不要**在无备份需求时 `lwa init --force` 覆盖整个工作区配置。
- **不要**假设 `pip install -e .` alone 足够——必须通过 `lwa update` 或手动命令重启 **manager/daemon 子进程**。
- **不要**在自启单元仍启用时手搓 `manager/daemon off && on` 做「升级重启」——会与 KeepAlive/Restart 抢锁；用 `lwa update` 或先 `lwa autostart disable`。

## 故障排查

| 现象 | 处理 |
| --- | --- |
| 管理页仍显示旧版本 | 先运行 `lwa update`（自启在管时由其协调重启）；勿直接 `manager off/on` 除非已 `autostart disable`；再查 17800 端口 PID |
| `lwa version` 已新但页面旧 | 浏览器强刷；确认访问的是本机 127.0.0.1 而非旧 tab 缓存 |
| 代码变更后实例行为异常 | `lwa restart <id>` 或 `lwa update --restart-instances` |
| update 后出现双 daemon/manager | 多为自启未协调的旧路径残留；改用 `lwa update`，或 `autostart disable` 后清理进程再 enable |

## 示例对话

> 用户：我刚 `git pull`，CLI 是新版本，但管理页还是旧的。
> Agent：在 Runtime 工作区根目录执行 `lwa update`；它会同步内置 skills，并仅重启原本运行的 manager/daemon。完成后对比 `lwa version` 与 `/api/health` 的 `version`。

## 相关文档

- [待改进 IMP-008](../../../../design/plan/待改进功能点记录-20260706.md)
- [Runtime 工作区说明](../../../../docs/runtime-workspace.md)
- [开机自启（停服/update 协调）](../../../../docs/autostart.md)
- [访问地址复核](../lwa-review-access-urls/SKILL.md)
- [lwa-setup-host-environment](../lwa-setup-host-environment/SKILL.md)
