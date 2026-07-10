# lwa-import-zip

> 把用户提供的本地 zip 包导入为实例；同一项目的新版本应**原地更新**而非重复新建。

## 何时触发

- 用户给出一个 zip 文件（或放到 `inbox/`），要求"部署 / 跑起来 / 看看这个网页"。
- 用户说"这是 xx 项目的新版本 / v2 / 更新一下"，但历史上已导入过同名实例。
- `lwa import <zip>` 报错"实例 xxx 已存在……请使用 --update"。

## 核心决策：import 还是 update？

**先查 registry 是否已有同 slug 的实例**（`lwa list` 或读 `apps/`）：

| 场景 | 判断 | 命令 |
| --- | --- | --- |
| 全新项目，无同名实例 | import | `lwa import inbox/foo.zip` |
| 同项目新版本，实例已存在 | **update** | `lwa import inbox/foo-v2.zip --update <instance-id>` |
| 不确定是否同一项目 | 看 zip 内 `package.json` / README 项目名 | 不确定时**询问用户**，不要无脑 import |

**关键**：不传 `--update` 且 slug 冲突时，CLI 会**报错**（不再 silent 建 `-2`）。
收到这个报错 = 大概率用户想更新已有实例，应改用 `--update <id>`。

## import（新建）

```bash
lwa import inbox/foo.zip
# 可选：给静态站点起一个路径别名（IMP-006），通过 http://<LAN-IP>:<gatewayPort>/<alias>/ 访问
lwa import inbox/foo.zip --path-alias myapp
# 可选：自定义显示名称（影响 instance id slug）
lwa import inbox/foo.zip --name "My App"
```

导入后也可在线修改路径别名（V0.4.1 起）：

```bash
# 静态 / 前端：import 时可 --path-alias；之后也可改
lwa alias set <static-id> demo-slug

# 容器（docker-compose，IMP-014）：须先 lwa start 拿到 hostPort，再设别名
# import 时对非 static 传 --path-alias 会被拒绝
lwa start <container-id>
lwa alias set <container-id> demo-slug

lwa alias clear <id>
# 或在管理页实例列表操作区点击「路径别名」（容器按钮已可用）
```

> **IMP-022 / IMP-023 路径别名约束（V0.4.4 起）**
>
> - **需要 Caddy 网关**：别名统一入口依赖 Caddy 的 `:<gatewayPort>` 站点块。
>   `lwa alias set` 在 `staticGateway=builtin`（或 caddy 未安装）时会**明确报错**，
>   不再无声写元数据造成"设置成功但访问失败"。先 `lwa gateway on` 启用 Caddy。
> - **SPA 子路径资源（IMP-023）**：别名 `reverse_proxy` 会去掉 `/<alias>/` 前缀，
>   **相对路径资源**（`./assets/...`）正常；但 **绝对路径资源**（`/assets/...`，
>   Vue/React 默认 `base: '/'`）会绕过别名打到入口根 → 404 白屏。受影响项目应在
>   构建时设相对 base（Vite `base: './'`）或显式 `--base=/<alias>/`。纯静态 HTML
>   （相对路径或无外部资源）不受影响。详见 `lwa-build-frontend-static`。

## 误重复导入与冗余清理（IMP-012 / IMP-019）

同一 zip 指纹重复 import 会产生冗余实例。优先用 `--update` 防新建；已产生冗余时：

```bash
lwa remove --redundant          # 预览并清理（每组保留最早者）
lwa remove --redundant --purge  # 连磁盘一起清
```

管理页：勾选「仅冗余」→ 行内删除，或顶部「批量删除冗余」。详见 `docs/operations-playbook.md`。

- IMP-001：导入时会**自动剥离** `node_modules/`、`__pycache__/`、`.venv/`、
  `.git/`、`__MACOSX/`、`.DS_Store` 等冗余成员，并做 zip slip / 符号链接防护。
  无需手动清理 zip。
- 导入后实例为 `pending`（未识别）→ 走 `lwa-detect-stack` 等后续 skill。

## update（原地升级，IMP-009）

```bash
# 推荐：显式指定要更新的实例
lwa import inbox/foo-v2.zip --update foo

# 预演：看 hash 差异和形态变化，不写盘
lwa import inbox/foo-v2.zip --update foo --dry-run

# 维护窗口：只换包，不自动 restart
lwa import inbox/foo-v2.zip --update foo --no-restart

# 重置数据（默认保留 data/）
lwa import inbox/foo-v2.zip --update foo --no-keep-data

# 新 zip 被识别成不同形态（static → container），首版默认拒绝；确认迁移才加：
lwa import inbox/foo-v2.zip --update foo --force-kind-change
```

**update 保留什么**：`instance_id`、`hostPort`（端口登记不动，重启时复用，LAN URL 不变）、
`data/`（SQLite / 上传文件等持久数据）、`desiredState`、IMP-006 路径别名。
**update 替换什么**：`apps/<id>/current/` 全量业务源码、`sourceZipHash`、扫描结果。

**容器实例（DEV-067 / BUG-112）**：源码换入后会清空 `containerId`/`imageId`（作废旧部署）。
若原 `desiredState=running` 且未传 `--no-restart`，调用方走 **`lwa rebuild`**
（`compose build` + `up`），**不会**轻量 `restart`——后者不重建镜像，会造成
「磁盘已新、容器仍旧」假绿。`--no-restart` 时只换源码并提示稍后 `lwa rebuild <id>`。

**hash 相同**：新 zip 与当前 `sourceZipHash` 一致 → 自动跳过，提示"包未变化"，不 rebuild。

**形态变化**：新 zip 的 `kind`/`runtime` 与原实例不同（如 static → python 容器），
默认**拒绝**原地更新（防止误把静态站变成容器）。通常应**新建实例**
（去掉 `--update`，用 `--name` 区分）；只有确认要把同一实例迁移到新形态时，
才加 `--force-kind-change`，此时旧 `hostPort` 登记会迁移到新形态。

## 输入

1. zip 文件路径（`inbox/xxx.zip` 或绝对路径）。
2. 目标实例 id（update 时必需；可从 `lwa list` 获得）。
3. zip 内的项目标识（`package.json` name、README 标题）——用于判断是否同一项目。

## 输出

- 新建：一个 `pending` 实例（交给后续 skill 识别）。
- 更新：同一 instance_id，`current/` 已替换；若原 running 则自动 restart（端口不变）。

## 禁止事项

- 不在未确认时 silent 覆盖 running 实例（daemon inbox 默认也不自动覆盖）。
- 不手动删 `apps/<id>/current/` 再解压——绕过了 `update_zip` 的原子换入与回滚保护。
- 不用 `--force-kind-change` 除非用户明确确认跨形态迁移。
