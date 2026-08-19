---
name: lwa-import-git
description: >-
  Import a public GitHub repository into lwa by shallow-cloning into a temp
  staging dir (outside the workspace) and copying into the workspace, or update
  an existing git-source instance via git ls-remote change detection. Use when
  the user gives a https://github.com/<owner>/<repo> URL and wants one-command
  import / update instead of manually cloning + folder import.
---

# lwa-import-git

> 输入 `https://github.com/<owner>/<repo>` → LWA 在宿主机**浅克隆**到工作区外临时目录 → 打包后走与 zip 完全相同的识别/部署管线。git 源实例的「更新」＝ `git ls-remote` 探测远端 OID，无变更零重建，有新提交重克隆后原地升级。

## 何时触发

- 用户给 GitHub 仓库地址，说「部署 / 导入这个仓库」。
- 已有 `sourceKind=git` 实例，需要「从源更新」。
- 不想手工 `git clone` 再走文件夹导入。

## 红线（必须遵守）

0. **先确认工作区（IMP-053）**：导入前 `curl -s http://127.0.0.1:17800/api/health`。
   若已有 `workspaceRoot`，在该目录下执行本 skill 的命令；**不要**另开
   `~/lwa-workspace` 再 `lwa init`。
1. **仅支持 `https://github.com`**（MVP；精确 host 匹配，仅 443）。SSH / `git@` /
   其他平台一律拒绝，不要试图绕过。
2. **一次性浅克隆**：staging 用完即删，不缓存仓库、不做增量 fetch；实例内
   不保留 `.git`。
3. **凭据与代理零托管**：私有仓凭据、`https_proxy` 都配置在 **LWA 宿主机**
   （不是浏览器那台机器）。LWA 不保存/不回显任何凭据。
4. 不做 webhook / 定时拉取 / GitHub App / `git push`——更新永远由用户/Agent 触发。
5. URL 里出现 `/tree/`、`/blob/` 等网页段时：改用仓库根地址 + `--ref`（分支/
   标签）+ `--subdir`（子目录），不要硬传网页 URL。

## 导入（新建）

```bash
# 公开仓（默认分支）
lwa import --from-git https://github.com/<owner>/<repo>

# 指定分支/标签与子目录（monorepo 只部署某个子目录）
lwa import --from-git https://github.com/<owner>/<repo> --ref dev
lwa import --from-git https://github.com/<owner>/<repo> --ref v1.0
lwa import --from-git https://github.com/<owner>/<repo> --subdir frontend

# 实例名 / 路径别名
lwa import --from-git https://github.com/<owner>/<repo> -n "My App" --path-alias myapp
```

导入后：`sourceKind=git`，`sourceDirPath` 恒为空；manifest 记录规范化 url、
真实分支/tag 名（`sourceGitRef`/`sourceGitRefKind`，不是 `HEAD`）、完整 commit
OID（`sourceGitCommit`）；内容在 `apps/<id>/current/`。

## 更新

```bash
lwa import --from-git https://github.com/<owner>/<repo> --update <instance-id>
```

- 远端 OID 未变 → 「无需更新」跳过，不 clone、不 rebuild、不重启。
- 有新提交 → 重新浅克隆 → 原地升级（保留 id / 端口 / data / 别名）。
- 传入 URL 规范化后必须与实例记录一致（换仓库会被拒，`source_mismatch`）；
  换源请先 `lwa remove` 再重新导入。
- git 源实例不能用 zip `--update` / `--from-dir --update` 更新（会被拒绝）。

## 失败排查（结构化 errorKind）

| kind | 原因 / 处置 |
| --- | --- |
| `invalid_url` | 地址不是仓库根（含 `/tree/` 等网页段、query/fragment）→ 改用仓库根 + `--ref`/`--subdir` |
| `host_not_allowed` | 非 github.com 或非 443 端口 |
| `userinfo_forbidden` | URL 带用户名/密码 → 凭据配到宿主机 credential helper |
| `git_missing` | 宿主机没装 git → 先装 git |
| `remote_unreachable` | 网络/代理/凭据问题；私有仓需宿主机已配凭据（无凭据会快速失败，不会卡住） |
| `ref_not_found` | 分支/标签不存在 → 核对 `--ref` |
| `clone_timeout` / `size_exceeded` | 仓库过大（>180s / >2GiB）→ 建议改用文件夹导入 |
| `source_mismatch` | 更新时传了另一个仓库 → 先删除实例再导入 |

## 管理页

管理页「从 GitHub 导入」按钮（**不限本机**，LAN + token 可用）：仓库地址必填，
分支/标签、子目录、实例名、路径别名选填。git 实例卡展示 url / ref / 短 SHA；
「从源更新」走 `POST /api/instances/{id}/update-from-git`。API：
`POST /api/import-from-git`（body：`url` / `ref` / `subdir` / `name` / `pathAlias`）。
识别失败（pending）时管理页报错并保持对话框，不冒充成功；档位 medium/heavy
不自动启动但导入成功，可手动启动。

## 明确不做

- 不解析 Git LFS 对象（指针文件当普通文本）。
- 不递归 submodule（需要时改用文件夹导入）。
- 不做仓库缓存 / 对浅仓增量 fetch / merge / rebase。
- `github.com` 之外的 host（Gitea/GitLab/GHE）暂不支持。
