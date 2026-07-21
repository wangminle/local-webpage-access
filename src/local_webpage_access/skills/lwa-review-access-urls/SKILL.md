---
name: lwa-review-access-urls
description: >-
  Refresh and review the real availability of lwa loopback, LAN, and path-alias URLs after upgrades, network changes, or gateway switches. Use when links stop opening, lwa doctor reports lan_url_stale, a path alias is blank while the port URL works, or a rebuild may be needed for SPA base-path issue IMP-023.
---

# lwa-review-access-urls

> 升级、换网或网关切换后，复核各实例声明访问地址的**真实可用性**，并在必要时安全触发重建。

## 何时触发

- 刚执行 `lwa update` / `lwa gateway switch` / `lwa gateway on`，或换 Wi-Fi / DHCP 后管理页「端口」打不开。
- `lwa doctor` 报 `lan_url_stale` WARN，或 `doctor --access` / `access review` 标出 IMP-023。
- 用户问：「局域网链接失效」「别名白屏但端口正常」「升级后地址对不对」。

## 输入

1. `lwa access review --json` 或 `lwa doctor --access --json` 的报告。
2. 实例 `local-web.json` 中的 `lanUrl`、`routeUrl`、hostPort 与路径别名。
3. 当前 LAN IP、网关后端（`caddy` / `builtin`）与最近的 update / 换网 / switch 操作。
4. 用户是否明确同意对 IMP-023 命中实例执行 rebuild。

## 安全边界（默认只读）

| 操作 | 默认 | 说明 |
| --- | --- | --- |
| `lwa access refresh` | 写盘 | 仅重写 `lanUrl`/`routeUrl`，**不** rebuild、不改业务代码 |
| `lwa access review` | **只读** | HTTP 探活 + SPA 空 200 检测；不改文件 |
| `lwa doctor --access` | 只读复核 | 复用同一套 `review_access()`，不另写探测 |
| `--rebuild-if-needed` | **显式才写** | 仅对 IMP-023 命中实例 rebuild；**必须**经用户确认 |

禁止把 DHCP / LAN IP 漂移解读为必须 rebuild。只有 IMP-023（绝对路径子资源空 200）才建议重建。

## 推荐流程

```bash
# 1. 用当前 LAN IP 刷新落盘地址（换网 / update 后）
lwa access refresh

# 2. 轻量复核（回环 + lanUrl + routeUrl + SPA 子资源）
lwa access review --json

# 或与 doctor 合并：
lwa doctor --access --json

# 3. 仅当报告明确 IMP-023 / needsRebuild 时，经确认后：
lwa access review --rebuild-if-needed
# 或对单个实例：
lwa rebuild <id>
```

升级路径（IMP-038）：`lwa update` 在重启 manager/daemon **之后**固定 refresh，默认轻量 review；
可用 `--no-review-access` 跳过复核。`--dry-run` 不探测、不写盘。

常驻自愈（IMP-040）：管理页列表读时合成当前 `lanUrl`；漂移后节流落盘。仍可用本 skill 做显式复核。

## 错误分层

1. **安装/升级失败**（pip、配置迁移）→ 查 `lwa update --json` 的 `pip` / `migrateConfig` 步骤。
2. **地址漂移**（`lan_url_stale` / `lanUrlStale`）→ `access refresh`；不必 rebuild。
3. **服务未起**（回环探活失败）→ `lwa start` / 查 run 日志；与 LAN 无关。
4. **IMP-023 空 200** → 修前端 `base` 后显式 `--rebuild-if-needed` 或 `lwa rebuild`。
5. **端口双开 / 后端不一致 / 网关残留** → 优先 `lwa gateway switch <caddy|builtin>`（IMP-037）；仅启停 master 用 `lwa gateway on/off`；并用 `doctor` 的 `backend_handoff` 核对。

## 输出

- 向用户返回：当前 LAN IP、漂移实例、复核 overall、是否需要 rebuild。
- **不**自动删除实例、**不**改 `data/`、**不**在未确认时 rebuild。

## 禁止事项

- 不把 LAN IP 漂移当成必须 rebuild；先用 `lwa access refresh` 更新地址。
- 不在用户未确认时传 `--rebuild-if-needed`，也不批量重启与 IMP-023 无关的实例。
- 不通过删除实例、清空 `data/` 或手写主 `Caddyfile` 修复访问地址。
- 不将内部探针请求计为真实访问量。

## 示例对话

> 用户：换 Wi-Fi 后局域网链接都打不开，别名也白屏。
> Agent：先执行 `lwa access refresh`，再用 `lwa access review --json` 区分地址漂移、服务未起与 IMP-023。只有报告标记 `needsRebuild` 且你确认后，才会执行 `--rebuild-if-needed`。
