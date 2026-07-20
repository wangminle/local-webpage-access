# lwa-review-access-urls

> 升级、换网或网关切换后，复核各实例声明访问地址的**真实可用性**，并在必要时安全触发重建。

## 何时触发

- 刚执行 `lwa update` / `lwa gateway on`，或换 Wi-Fi / DHCP 后管理页「端口」打不开。
- `lwa doctor` 报 `lan_url_stale` WARN，或 `doctor --access` / `access review` 标出 IMP-023。
- 用户问：「局域网链接失效」「别名白屏但端口正常」「升级后地址对不对」。

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
5. **端口双开 / 网关残留** → `lwa gateway on` / `doctor` 的 `backend_handoff`。

## 输出

- 向用户返回：当前 LAN IP、漂移实例、复核 overall、是否需要 rebuild。
- **不**自动删除实例、**不**改 `data/`、**不**在未确认时 rebuild。
