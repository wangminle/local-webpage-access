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
| **`lwa update`（规划中，IMP-008）** | 已有工作区 + **lwa 包升级** + 重启 manager/daemon |

当前 V1 **尚未实现** `lwa update` CLI；本 skill 提供**手动步骤**，实现后改为优先调用 `lwa update`。

## 输入

1. lwa 源码目录路径（含 `pyproject.toml` 的仓库根）。
2. Runtime 工作区路径（含 `local-web.yml`，如 `runtime/`）。
3. （可选）是否需重启业务实例（仅当 hosting/网关/import 逻辑变更时）。

## 输出

- 向用户返回可执行清单与预期结果（新版本号、管理页 URL）。
- **不**删除 `apps/`、`registry/` 中的实例数据。

## 手动流程（`lwa update` 实现前）

```bash
# ── A. 刷新 lwa Python 包（editable 安装）──
cd /path/to/local-webpage-access
pip install -e .

# ── B. 同步 skills 到工作区（可选，与 init 行为一致）──
# 实现 lwa update 前可手动：从 src/local_webpage_access/skills/ 复制新增 SKILL
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

## 实现后的目标命令（IMP-008）

```bash
cd runtime
lwa update                    # 默认：pip + sync skills + 重启 manager/daemon + doctor
lwa update --restart-instances   # 额外 restart 所有 running 实例
lwa update --skip-pip --json
```

## 禁止事项

- **不要**对业务实例执行 `remove --purge` 作为「更新」手段。
- **不要**在无备份需求时 `lwa init --force` 覆盖整个工作区配置。
- **不要**假设 `pip install -e .`  alone 足够——必须重启 **manager/daemon 子进程**。

## 故障排查

| 现象 | 处理 |
| --- | --- |
| 管理页仍显示旧版本 | 确认 `lwa manager off/on` 已执行；检查 17800 端口 PID 是否为新进程 |
| `lwa version` 已新但页面旧 | 浏览器强刷；确认访问的是本机 127.0.0.1 而非旧 tab 缓存 |
| 实例行为异常 after 代码变更 | `lwa restart <id>` 或规划中的 `lwa update --restart-instances` |

## 相关文档

- [待改进 IMP-008](../../../../docs/plan/待改进功能点记录-20260706.md)
- [Runtime 工作区说明](../../../../docs/runtime-workspace.md)
- [lwa-setup-host-environment](../lwa-setup-host-environment/SKILL.md)
