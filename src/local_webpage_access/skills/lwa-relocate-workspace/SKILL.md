---
name: lwa-relocate-workspace
description: >-
  Migrate an LWA runtime workspace to a new absolute path on the same volume
  using `lwa workspace relocate`. Use when renaming or moving the workspace
  folder, after path-related autostart/Caddy/pageviews issues, or when the user
  asks to relocate local-webpage-access runtime.
---

# lwa-relocate-workspace

> 把 **LWA Runtime 工作区**迁到同机**同一卷**上的新绝对路径。  
> **唯一执行入口**是 CLI；本 Skill 只负责解释风险、确认、调用 CLI、汇总报告。

## 何时触发

- 用户要改名/挪动工作区目录（如 `~/local-webpage-access-20260717` → `~/local-webpage-access`）。
- 自启单元、Caddy、Docker bind mount 仍指向旧绝对路径。
- 用户问：「工作区怎么迁移」「能不能改文件夹名」。

## 硬性禁止

- **禁止**直接 `sed` 改 `local-web.json` / Caddyfile / unit 文件。
- **禁止**直接 `mv` / `rm` 工作区（尤其 **不要删** `run/daemon-processed.json`）。
- **禁止**临场拼接第二套迁移脚本；一律走：

```bash
lwa workspace relocate <NEW> [--dry-run] [--json] [--yes] [--from OLD]
lwa workspace relocate --resume | --verify | --rollback
```

## 范围（v1）

| 场景 | 是否支持 |
| --- | --- |
| 同卷原子改名（macOS / Linux / WSL Linux 盘） | ✅ |
| 跨盘 / 跨卷 / 跨机 | ❌ CLI 预检 blocking；见 `docs/workspace-rename.md` |
| WSL `/mnt/<drive>/…` → `~/…` | ❌ 通常跨设备，自动拒绝 |

## 推荐流程

1. **确认 OLD / NEW**（绝对路径；NEW 必须尚不存在）。
2. **预演**：

```bash
cd /path/to/OLD
lwa workspace relocate /path/to/NEW --dry-run --json
```

3. 向用户解释 dry-run 中的：
   - `blocking` / `warnings`（尤其 `cross_device`、`wsl_drvfs`、`editable_inside_workspace`）
   - 停机范围（业务实例 + daemon/manager/gateway + 自启）
   - `planned_phases` / `planned_actions`
   - dry-run **零副作用**（不建 pageviews.db、不以写模式开 registry）
4. 用户确认后执行：

```bash
lwa workspace relocate /path/to/NEW --yes
# 或交互确认（TTY）省略 --yes
```

5. 迁后：

```bash
cd /path/to/NEW
lwa workspace relocate --verify
lwa version
lwa autostart check   # 仅当迁前用了自启
```

6. 失败时：
   - 先 `lwa workspace relocate --resume`（可显式传 NEW；journal 权威 old/new）
   - 同卷且 journal 完整：`--rollback`（会逆改写路径，不只 rename）
   - 仍失败：打开 [`docs/workspace-rename.md`](../../../../docs/workspace-rename.md) 人工逃生舱

## editable 安装提示

若 dry-run 出现 `editable_inside_workspace`：迁后在**新路径**重新 `pip install -e .`，或改用独立 venv（生产 CLI 与工作区解耦）。不要假装「只改名、包路径自动跟着变」。

## 输出

向用户返回迁移报告：OLD→NEW、phase、恢复的实例 ID、verify 笔记、journal 路径（`run/workspace-migrate-journal.json`）。
