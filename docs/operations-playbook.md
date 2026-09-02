# 运维手册（Operations Playbook）

lwa 在局域网小主机上的日常运维、选型与排障速查。面向已 `lwa init` 并跑过若干实例的维护者。

> 配套阅读：[Runtime 工作区说明](runtime-workspace.md)（目录结构 / 端口）、[管理页说明](manager-page.md)、[开机自启](autostart.md)、[工作区改名迁移](workspace-rename.md)。

---

## 零、宿主机装配（IMP-031/032/033）

首次部署或换机时，用 `lwa setup` / `lwa init` 的装配档位装齐依赖：

| 档位 | 命令 | 行为 |
| --- | --- | --- |
| default（缺省） | `lwa setup` / `lwa init` | 检测 Python / Docker / Compose / Caddy / Node 并打印指引；缺 Docker Engine 时 TTY 询问是否执行内置安装脚本。**`lwa setup`（default）无需先有工作区** |
| full | `lwa setup --full` / `lwa init --full` | 按 `MIN_*` 检查并安装 Caddy + Docker Engine + Compose；固化 `serviceUser`；验收 **CapabilityReport.overall=ready**（CLI + manager/daemon/gateway 真实上下文、Caddy owner/工作区访问）后才把 `local-web.yml` 的 `profile` 写成 `full`（未闭环可写 `full-setup-state.json`，**不**提前抬成 full）；未闭环 **不得** exit 0。**`--full` 需要已初始化工作区**（推荐 `lwa init --full --yes`，或先 `lwa init` 再 `lwa setup --full`） |

常用开关：

```bash
lwa setup --script                 # 打印内置脚本路径（不自动执行）
lwa setup --install-docker         # default 档强制跑 Docker 安装脚本
lwa setup --no-install-docker      # default 档跳过询问
lwa init --full --yes              # 初始化工作区并走 Full；倾向 staticGateway: caddy
lwa setup --full --yes             # 已有工作区时：安装 + 能力闭环（CI / 无人值守）
lwa setup --full --resume          # 重登 / newgrp 后继续验收（exit 2 时）
lwa doctor --profile full          # 输出 CapabilityReport 与修复建议
lwa capabilities --json            # 机器可读能力快照
```

### Full Profile 能力闭环（IMP-033）

Full 不仅「装上二进制」，还要求：

1. **统一身份**：固化 `serviceUser`（真实登录用户，而非临时 sudo 的 root）。
2. **多上下文 Docker**：CLI / manager / daemon 各自探测并写入 `run/capability-{manager,daemon,gateway}.json`；父 CLI **不得冒充**后台写缓存。后台三角色均会周期刷新；daemon/gateway 写缓存时合并存活 peer（BUG-406/407）。角色快照 `overall` 按本角色职责计算（ADJ-035）；以 `lwa doctor --profile full`（CLI 实时）为准排查假红，见 [FAQ](faq.md)。
3. **Caddy 严格托管**：禁用冲突的系统 `caddy.service`；`:2019` 须为本工作区 LWA Caddy（owner / euid / pid）；工作区配置与日志路径可读写。`caddy start` 在 pingback 等待期并行探 admin/pidfile（ADJ-036），就绪即成功。
4. **退出码**：`ready`→0；`session_refresh_required`（组权限未生效）→2；`unready`→1。

Linux 上 `usermod -aG docker` 后若当前进程仍无 docker 组：按提示重登或 `newgrp docker`，再 `--resume`。  
若使用 **system-level** systemd 单元监管 LWA，推荐 `User=<serviceUser>` + `SupplementaryGroups=docker`，可绕过「重登才刷新组」；当前 `lwa autostart` 默认仍是 **user unit**，依赖登录会话组（见 [autostart.md](autostart.md)）。

内置脚本（包内 `scripts/`，默认国内源）：

- **Docker**：`install-docker-linux.sh`（Debian 系走官方 apt 流程、Fedora 走 dnf 仓库 + 默认阿里云 docker-ce；`--official` 切官方源；默认写入 `registry-mirrors`）、`install-docker-macos.sh`（brew cask Docker Desktop）。
- **Caddy**：`install-caddy-linux.sh` / `install-caddy-macos.sh`（Linux 脚本会停用冲突的系统 caddy.service；Debian 系用 Cloudsmith apt 源，Fedora 用官方仓库 dnf 包，均回退 GitHub Release 二进制）。

Windows **原生进程不受支持**，无内置安装脚本。请在 **WSL2**（Ubuntu LTS 22.04/24.04/26.04、Debian Stable 12/13 或 Fedora 43/44）内执行 `lwa setup` / `lwa init`。装完后建议 `lwa setup` 复核，再 `lwa doctor`（需已 `init`；未初始化时 `lwa doctor --json` 仍可输出平台诊断）；Full 环境用 `lwa doctor --profile full`。WSL 下请勿把工作区放在 `/mnt/<drive>` 再跑 `--full` / `autostart`（写入前 fail-closed）。

**运行前（尤其 WSL）**：先核对宿主内存配额、工作区是否在 Linux 盘、防火墙是否放行业务口——见 [已知限制 · WSL2 宿主准备](known-limitations.md#wsl2-宿主准备运行前)；自启与可选 mirrored 网络见 [开机自启](autostart.md#wsl-的-windows-侧)。

日志排障见 [FAQ · 症状→日志](faq.md#症状--日志文件--命令imp-034)；保留策略见下文「日志与 journal 保留」。

---

## 零（附）、运行前检查清单（建议）

首次部署或换机时，在 `lwa setup --full` / `lwa autostart install` 之外建议勾选：

| 项 | 命令 / 动作 | 说明 |
| --- | --- | --- |
| 工作区路径 | `pwd` 应在 `/home/…`，非 `/mnt/c/…` | 正确性 + 9p 性能；见 [known-limitations](known-limitations.md#工作区放在-linux-文件系统正确性--性能) |
| WSL 内存 | Windows 侧查 `%UserProfile%\.wslconfig` | 建议 ≥6GB（小主机按表调整）；改后 `wsl --shutdown` |
| systemd | `/etc/wsl.conf` 含 `[boot] systemd=true` | 否则 `lwa autostart` 不可用 |
| 自启 + 唤醒 | `lwa autostart install --with-caddy` + Windows 登录任务 | 见 [autostart](autostart.md) |
| 防火墙 | 放行实际在用的 `:8080` / 管理页 / 实例口；**不**放行 `:2019`；**不**默认全开 portPool | Mirrored：Hyper-V firewall 逐端口；另见 ufw。详见 [LAN 与防火墙](known-limitations.md#lan-访问与防火墙) |
| 能力闭环 | `lwa doctor --profile full`；`test -f run/capability-gateway.json` | Full 红条多为能力缓存/监管，未必是 Docker 坏了 |
| 日志空间 | 见下一节 | 避免 journal + 应用日志双份撑满磁盘 |

---

## 零（附 2）、日志与 journal 保留

LWA **应用侧**日志已按大小滚动，一般无需再配 logrotate：

| 位置 | 机制 | 量级（默认） |
| --- | --- | --- |
| `logs/lwa.log`、`logs/manager.log`、`logs/daemon.log`、`logs/gateway.log` 等 | `RotatingFileHandler` | 单文件约 10MB × 保留 3 份备份 |
| `apps/<id>/logs/*`、`static-gateway` 相关 access/gateway 日志 | 打开前按大小滚动（`logs.rotate_*`） | 避免单文件无限增长 |

仍建议定期巡检磁盘：

```bash
du -sh logs apps/*/logs static-gateway 2>/dev/null
df -h .
```

**systemd journal**（`journalctl --user -u lwa-*.service`）与上述文件日志是**两套**存储。长期常驻时请限制 journal，避免与 `logs/` 双份无限增长：

```bash
# 用户级（示例）：限制 journal 占用
mkdir -p ~/.config/systemd
# 在 ~/.config/systemd/user.conf 或系统 /etc/systemd/journald.conf.d/ 中设置，例如：
# [Journal]
# SystemMaxUse=200M
# RuntimeMaxUse=100M
sudo systemctl restart systemd-journald   # 改系统级配置后
```

查看占用：`journalctl --user --disk-usage`（或 `journalctl --disk-usage`）。

Docker 构建缓存 / 镜像 / WSL VHD 也会胀盘；可人工 `docker system df` 查看，**不要**在无人值守脚本里默认 `docker system prune -a`（破坏性）。WSL 磁盘回收见微软 WSL 文档中的精简 VHD 说明。

症状→文件对照仍以 [FAQ](faq.md#症状--日志文件--命令imp-034) 为准。

---

## 一、静态网关选型：Caddy vs builtin

lwa 的静态站点 / 路径别名由"静态网关"承载，两种后端二选一（`local-web.yml` → `staticGateway`）：

| 维度 | `caddy`（推荐） | `builtin`（兜底） |
| --- | --- | --- |
| 统一入口 / 路径别名 | ✅ `:<staticGatewayPort>` 站点块 + import 别名片段 | ❌ 多端口模式，**无统一入口**；`lwa alias set` 会被拦截（IMP-022） |
| 单点监听 | 1 个端口（默认 8080）聚合所有别名 | 每个静态实例各占 1 个 hostPort |
| master 生命周期 | `lwa gateway on/off/status` 管理（admin :2019 探活） | 无；每实例 `python -m http.server` 子进程 |
| 自愈 | reload 失败自愈、stale pid 清理（BUG-069/070） | daemon reconcile 重 spawn 死掉的静态进程 |
| 安装前提 | 需安装 Caddy ≥ `MIN_CADDY_VERSION` 并在 PATH | 无外部依赖，纯 Python |

**选型建议**：

- **生产 / 局域网共享访问** → 用 **Caddy**：路径别名、统一入口、可观测性都依赖它。浏览量读 `static-access.log`：别名按路径、无别名静态按直连端口归属（IMP-024/028）；仅计 page（IMP-025）；有别名容器也走 Caddy 源（IMP-027）。
- **临时 / 无 Caddy 环境（default 档）** → 可降级 `builtin`：每个静态站点独立端口可达，但**别名不可用**，浏览量仅解析各实例 `gateway.log`（CLF）。**Full 档**要求 Caddy 严格可用，不得静默降级。

切换：优先用原子命令（IMP-037），无需手改 YAML：

```bash
lwa gateway switch caddy              # builtin → caddy（校验 Caddy 版本）
lwa gateway switch builtin            # caddy → builtin（Caddy 坏掉时也可降级）
lwa gateway switch builtin --dry-run  # 只看变更摘要
lwa gateway switch caddy --json       # 机器可读结果（含 accessOk / fullyOk）
```

事务内会：停旧后端 → 写 `staticGateway` → 启新后端 → 批量回写
`manifest.static.gateway` / registry → access refresh（默认 review）。切到
builtin 时**保留** `routeHost` 元数据但别名入口未激活；切回 caddy 按 manifest
重建别名片段。失败会回滚 YAML/进程/manifest/registry；回滚失败标 `degraded`
并给出修复提示（不假绿）。
幂等：目标与当前相同则 noop，不重启。`--json` 中 `ok` 表示切换事务成功；
`accessOk` 表示访问复核通过；`fullyOk` 需二者皆真（`ok=true` 且 `accessOk=false`
表示后端已切、访问仍有风险）。

护栏（V0.7.10/V0.7.11）：① 切换全程持跨进程锁 `run/gateway-switch.lock`
（BUG-514），CLI 与管理页并发切换时后者约 15s 后快速失败
（`GATEWAY_SWITCH_LOCKED`）；② 切 builtin 时存在运行中但 manifest 无法加载的
静态实例则 fail-closed 拒切（`GATEWAY_MANIFEST_UNLOADABLE`，BUG-516），先修复
或删除这些实例；③ 切 caddy 启动失败时把切换前停掉的 builtin 静态服务尽力拉回
（BUG-517/523），站点不留下线。

仍可用手改 `local-web.yml` 的 `staticGateway` 后 `lwa gateway on/off`（旧路径）；
`lwa gateway off` 不校验版本，即便刚切到 builtin 也能关掉残留 Caddy master。

> 决策记录见 `task-list.md` CHK-013：阶段 0（P0）已把 Caddy 生命周期/原子配置/自愈落地，迁移 nginx 不省工作量，**维持 Caddy**。

---

## 二、inbox 投放规范（避免冗余实例）

`inbox/` 是 zip 投放区，daemon 与 `lwa import` 都扫这里。避免以下误用：

1. **勿放测试 zip**：daemon 会尝试导入 `inbox/` 下**所有** zip。测试样例请放 `tests/fixtures/`，不要丢进运行工作区的 `inbox/`，否则会被自动建实例。
2. **同包勿重复投放**：同一 zip 重复导入会按 `sourceZipHash` 指纹判定为冗余（IMP-012）。daemon 路径（IMP-011）slug 冲突时**不再自动建 `-2/-3`**，而是记 `import_conflict` 事件并提示用 `--update`。
3. **新版本用 update**：同一项目的新版本应 `lwa import inbox/foo-v2.zip --update <slug>`（保留 id/hostPort/data/别名），而非重复 import。
   - **容器实例**（DEV-067）：源码换入后会清空旧 `containerId`/`imageId`；若原为 running 且未传 `--no-restart`，会走 **`lwa rebuild`**（重建镜像），**不会**轻量 `restart`——后者不重建镜像，会造成「磁盘已新、容器仍旧」假绿。
   - **静态 / 前端**：换源码后 `restart` 即可同步 public。
   - `--no-restart`：只换源码；容器需稍后手动 `lwa rebuild <id>`。

### 导入成功后的归档

daemon 导入成功（started/pending/conflict 终态）后会把 zip **移入 `inbox/processed/`**（同名加时间戳），从扫描视野移除。手动 `lwa import` 不自动归档——导入后可自行移走或删除 zip。

### 连续失败死信（BUG-297）

同一 zip 指纹被 daemon 连续导入失败达到阈值（默认 5 次）后，会移入 **`inbox/failed/`**，停止无限重试。处理：查 `logs/daemon.log` → 修好 zip/环境 → 移回 `inbox/` 根目录再试。

### 批量清理冗余实例

```bash
lwa remove --redundant          # 预览同指纹冗余（保留每组最早者）
lwa remove --redundant --purge  # 确认后连磁盘一起清
```

管理页也可：实例列表「仅冗余」勾选 → 行内删除单条，或顶部「批量删除冗余」。

### 文件夹源导入与更新（IMP-047；选目录 IMP-051）

除 zip 外，LWA 也支持从本机文件夹直接导入。**关联目录是只读复制源**，LWA 会将内容复制进 `apps/<id>/current/`，不会就地运行。

```bash
# 从文件夹导入
lwa import --from-dir /home/user/my-site
lwa import --from-dir /home/user/my-site --name "My App" --path-alias myapp

# 从关联源目录更新（内容指纹未变化时自动跳过）
lwa import --from-dir /home/user/my-site --update my-app

# 只换源码不重启
lwa import --from-dir /home/user/my-site --update my-app --no-restart

# 预演
lwa import --from-dir /home/user/my-site --update my-app --dry-run
```

管理页也可：实例列表顶部「导入文件夹」按钮；`sourceKind=folder` 实例行内「从源更新」按钮。本机（loopback）打开管理页时可用「选择文件夹」（IMP-051）；局域网须手输绝对路径。

注意事项：
- 请选**项目根或 dist/**，不要只选 `src/`。
- 源目录必须为绝对路径；`node_modules/`、`.git/`、`__pycache__/` 等会被自动剥离。
- `--update` 时传入的 `--from-dir` 路径须与实例关联目录一致，否则 Exit 2（不会静默用错目录）；更换关联目录请删实例后重新导入。
- 源目录被删除 / 移动后 update 会**报错**（不会回退到 mount 模式）；需确认路径或改用 zip 更新。
- `sourceKind=zip` 的实例不能用 `--from-dir --update`（会报错）。
- 导入进行中勿立刻 `lwa update`（会等待或跳过重启，避免打断导入）。
- Agent 协作见 Skill `lwa-import-folder`。

### GitHub 源导入与更新（IMP-065）

从 GitHub 仓库一键导入与更新（仅 `https://github.com`、443 端口；私有仓凭据请配在 LWA 宿主机的 git credential helper，不是浏览器所在机器）：

```bash
# 从 GitHub 仓库导入
lwa import --from-git https://github.com/<owner>/<repo>
lwa import --from-git https://github.com/<owner>/<repo> --ref dev --subdir frontend
lwa import --from-git https://github.com/<owner>/<repo> --name "My App" --path-alias myapp

# 从 GitHub 远端更新（ls-remote 无变更探测，远端 OID 未变则零操作跳过）
lwa import --from-git https://github.com/<owner>/<repo> --update my-app
```

管理页也可：实例列表顶部「从 GitHub 导入」（LAN + token 可用，不限 loopback）；`sourceKind=git` 实例行内「从 GitHub 更新」按钮。

注意事项：
- 一次性浅克隆进临时 staging（克隆超 180s 或源码 >2 GiB 拒绝导入），实例内不保留 `.git`。
- `--update` 传入的仓库须与实例记录一致（Exit 2，换源请删实例重导）；git 源实例不能用 zip `--update` / `--from-dir --update` 更新。
- **导入/更新全程持有全局导入锁**（含 `ls-remote` 探测约 30s + 浅克隆最长 180s 的网络等待）。期间 `lwa update` 会等待导入空闲（最多 180s）再决定重启或跳过--慢网络下表现为 update 等待，属预期；确需立即升级可等导入完成后重跑 `lwa update`。
- Agent 协作见 Skill `lwa-import-git`。

### rebuild 与源码陈旧检测（issue #8）

`lwa rebuild <id>` 只重建镜像 / 产物，**不会**同步上游源码。若 folder / git 源
在导入后又有变更，rebuild 出来的仍是 `current/` 里的旧产物。为此：

- rebuild 前自动做**源码陈旧检测**：folder 源比对源目录内容指纹与上次同步
  指纹（`sourceSyncHash`）；git 源做短超时 `ls-remote` 比对远端 OID。检出
  陈旧时打印黄色警告 + 写 registry `source_stale` 事件，**不阻断重建**；
  git 探测离线 / 失败时不警告、不阻断（网络问题不会变成 rebuild 障碍）。
- 检出陈旧后的两种修法：

  ```bash
  lwa rebuild --sync <id>                          # 先走更新管线同步源码，再重建
  lwa import --from-dir <目录> --update <id>        # 或显式更新（git 源用 --from-git）
  ```

  `--sync` 复用 `import --update` 的既有更新管线（folder→`update_from_dir`、
  git→`update_from_git`），自带无变更短路；`--sync` 只支持 folder / git 源
  实例，zip / 无源实例会被拒绝（Exit 2）。
- `lwa doctor` 的 `source_freshness` 检查（WARN 级，纯离线）批量列出源码
  漂移 / 源目录丢失的 folder 源实例；git 源不触网，仅 SKIP 提示。

### 构建慢 / pip 下载失败（issue #18，V0.8.9）

生成的 Dockerfile pip 层按源链执行：**主源 → `pipFallbacks` 逐段 `||`**（china
默认阿里云 → 官方 PyPI → 腾讯云），每段 `--retries 3 --timeout 60`。排障口径：

- **构建报「依赖安装失败」并带换源提示** → 三段源全部硬故障：查 LWA 机器的
  出网/代理（`https_proxy` 对 git 生效，对 Docker 构建内的 pip 不生效——构建
  网络由 Docker 管理），或临时 `buildMirrors.enabled: false` 走官方源重试。
- **构建极慢但最终成功** → 主源限速（`||` 不切走慢而能通的源）：换主源
  （`buildMirrors.pip`，如清华 `https://pypi.tuna.tsinghua.edu.cn/simple`），
  或给 Docker 配 registry 代理之外再配 build 网络代理。
- **requirements 变更后全量重下** → 已知镜像缓存未命中行为（见
  [known-limitations](known-limitations.md)），不是故障；减少无谓的
  requirements 变更或接受该成本。
- 调整参数：`buildMirrors.pipFallbacks`（`[]` 只走主源）/ `pipRetries` /
  `pipTimeout`；改完 `lwa rebuild <id>` 生效。

---

## 三、容器实例路径别名（IMP-014）

容器实例（docker-compose）同样支持路径别名，把 `hostPort` 反代到统一入口。步骤：

1. **前提**：`staticGateway=caddy` 且 `lwa gateway on`（builtin 不支持别名，IMP-022 会拦截）。
2. **部署容器**：`lwa start <id>` 拿到 hostPort（别名 reverse_proxy 的目标端口）。
3. **设置别名**：

   ```bash
   lwa alias set <id> <slug>
   # 或管理页 → 实例操作区「路径别名」（容器实例按钮现已可用，BUG-085 已修）
   ```

4. **访问**：`http://<LAN-IP>:<staticGatewayPort>/<slug>/`。
5. **端口漂移**：容器 restart 后 hostPort 若变化，`_sync_alias_port`（IMP-021）会自动重写别名片段并 reload，无需手动处理。

> SPA 子路径提示（IMP-023）：Vue/React 等用绝对资源路径（`/assets/…`）在 `/<slug>/` 下会 404 白屏；构建时设相对 base（Vite `base: './'`）或 `--base=/<slug>/`。纯静态 HTML 不受影响。

---

## 四、Caddy master 排障

| 现象 | 排查 | 处置 |
| --- | --- | --- |
| `lwa gateway status` 显示未运行 | admin :2019 不可达 | `lwa gateway on`（自动 validate→start→探活） |
| 别名 502 / 站点不通 | 实例 hostPort 未监听 / 容器未起 | `lwa status <id>` 看状态；`lwa start <id>`；实例 `gateway_down` 用 `lwa recover <id>`（或管理页「恢复」/ `POST /api/instances/{id}/recover`） |
| `caddy validate` 报悬空 import | BUG-069 类残留（已根治，偶发于历史脏配置） | `lwa gateway off` 再 `on`，会基于实际存在的 conf 重组主 Caddyfile |
| 切 builtin 后 Caddy 还在跑 | stale pid / 旧 master | `lwa gateway off`（不校验版本，强制 `caddy stop` + 清 `run/gateway.json`） |
| `lwa doctor` 报 Caddy 健康 FAIL | admin/validate/站点端口探测 | 按 doctor 提示处置；常见为 master 未起（`lwa gateway on`） |

健康探针（IMP-020）：`lwa doctor` 在 caddy 模式会探 admin :2019 + 主 Caddyfile `caddy validate` + 别名入口 / 各 enabled 站点 hostPort 可达性 + stale pid 提示。

### Caddy 配置位置

- 主配置：`static-gateway/Caddyfile`（由 `_assemble_main_config` 基于实际存在的片段组装，**永不 import 不存在文件**）。
- 站点片段：`static-gateway/sites/<id>.conf`。
- 别名片段：`static-gateway/aliases/<id>.conf`（`reverse_proxy 127.0.0.1:<hostPort>`）。

---

## 五、开机自启（IMP-030）

跨平台统一入口是 **`lwa autostart`**（前台监管单元，非旧版 detached `on`）：

```bash
# V0.8.0 / IMP-061 缺省安全：caddy 在用时 gateway 单元默认纳入、linger 默认尝试
lwa autostart install                  # daemon + manager（+ gateway 当 staticGateway=caddy）
lwa autostart install --with-caddy --linger   # 显式等价写法（Linux/WSL 常用）
lwa autostart install --no-with-caddy  # 显式排除 gateway（旧 --with-caddy 语义仍是"纳入"）
lwa autostart check                     # 完备性深检（解释器 / 单元 / 进程身份 / Caddy…）
lwa autostart status [--json]           # 单元 + 进程 + 运行模式（systemd/launchd 监管 vs 裸进程）
```

`lwa setup --autostart` 仍可用，但已**委托**给 `lwa autostart install`（行为一致）。`lwa init` / `setup` 收尾在 Linux systemd 环境（TTY）会交互询问是否安装自启（非 TTY 零阻塞）。完整平台差异、停服协调与验收见 [开机自启](autostart.md)。

要点：

- **停服**：先 `lwa autostart disable`，再 `lwa daemon/manager/gateway off`（`off` 已内置 `coordinated_disable`）。
- **升级重启/拉起**：`lwa update` 对自有服务走三态 reconcile（IMP-059）——运行中交 `coordinated_restart`（监督器 `kickstart -k` / `systemctl restart`）；enabled 但意外未运行交 `coordinated_start` 拉起并标注中断时长（V0.8.2 起按 live 证据估算：存活 pidfile / systemd `InactiveEnterTimestamp`，陈旧 `run/*.json` 不采信、不确定不虚报）；enabled=false 跳过。均不与 KeepAlive 抢锁。重启后先**等待服务就绪**（`waitReady`，最多约 30s）再进入 access/doctor 收尾，超时降级 warning 并提示稍后 `lwa doctor` 复核（V0.8.2 / issue #5）。
- **daemon 自愈**（DEV-042）：watcher 启动时与每 60s 执行 `reconcile()`，恢复 `desired=running` 但状态偏离的实例。Caddy 后端且网关被显式 `lwa gateway off` 时跳过 caddy 静态。V0.8.8（issue #16）起自愈起止与 `lifecycle_stage` 阶段会镜像一份到 `logs/lwa.log`（完整日志仍在 `logs/daemon.log`）——构建失败后盯主日志即可看到"自动恢复开始 / 已自动重试并恢复"，不再表现为静默恢复。
- **Linux**：systemd user + 建议 `enable-linger`；**WSL** 另需 Windows 登录任务唤醒发行版；**Windows 原生不支持**自启（见 [autostart.md](autostart.md)）。

---

## 五（附）、取消进行中的构建（IMP-039）

排队过久、误触发 rebuild、或构建卡住占槽时：

```bash
lwa cancel-build <id>          # 取消 queued / building
# 管理页实例行「取消构建」；API POST /api/instances/{id}/cancel-build
```

- 取消**只停当前工作**，不删 Docker 构建缓存、旧镜像或 `apps/<id>/data/`。
- 排队任务直接 `cancelled`；进行中先 `cancelling`，再落到 `cancelled` 或 `cancel_failed`。
- `cancelling` 期间其它生命周期操作返回冲突（CLI/API 409），勿并行 start/rebuild。
- 修好 Dockerfile / 依赖后，再 `lwa rebuild <id>`。

---

## 六、日常巡检清单

```bash
lwa doctor               # 环境 + 实例健康（含 Caddy 探针）
lwa status               # 全部实例状态
lwa stats                # 整机 + 实例资源占用
lwa gateway status       # Caddy 网关状态
lwa manager status       # 管理页状态 + token
lwa daemon status        # daemon 自动导入状态
lwa list                 # 实例清单
```

异常态识别（DEV-043）：`gateway_down`（master 不可达）/ `config_invalid`（站点路由异常）会单独标注，管理页标"需恢复"并提供一键 recover；CLI 对齐为 `lwa recover <id>`。

---

## 七、网关切换交接与访问地址复核（gateway-switch-access-review）

复盘（2026-07-09）确认缺口：换网后管理页链接指向失效 LAN IP（G1）；「入口 HTML 200」≠「页面可渲染」（G2/G5，IMP-023 SPA 绝对资源空 200 / 404 / 错误 MIME）；builtin↔caddy 切换未彻底交接导致同端口双开（G3）；切换后应检查是否需 rebuild（G6，默认只提示）。下列能力已落地：

### 7.1 切换事务（G3 / IMP-037）

**推荐**：`lwa gateway switch <caddy|builtin>` 一次完成双向原子切换（写 YAML、停旧启新、
同步 manifest/registry、access 收尾）。也可用手改 `staticGateway` 后 `lwa gateway on/off`：

- 切到 **caddy**：`switch` / `gateway on` 停掉所有残留 builtin 静态进程（含 pid 文件已丢失的孤儿——按「服务本工作区 `apps/` 的 `http.server` 进程」枚举捕获，§2.7 现场即此类），再拉起/确认 Caddy master；有 `routeHost` 的实例会重建别名片段。
- 切到 **builtin**：`switch` / `gateway off` 强制停 Caddy master（不校验版本，BUG-077）；保留别名元数据但不宣称入口可用。
- `enable()` 启用单个站点前也会先停掉该实例仍存活的 builtin，杜绝同 hostPort 双开。

> 已知限制：切换事务的孤儿枚举用 POSIX `pgrep`（正式支持平台：macOS / Linux / WSL2）。无 pid 文件的孤儿由 doctor `backend_handoff` 检测并提示人工处置。Windows 原生不在支持矩阵内。

### 7.2 访问地址刷新（G1 / IMP-038 / IMP-040）

```bash
lwa access refresh   # 用当前 LAN IP 重算所有实例 lanUrl/routeUrl 并落盘
lwa update           # 一键升级（V0.8.0 含源码快进）收尾：后台重启 -> 等待就绪（V0.8.2）-> refresh（+ 默认轻量 review）
lwa doctor --access  # 诊断同时复用 access review
```

DHCP 换网后管理页「端口」链接会**读时合成**当前 IP（无需先 refresh）；落盘由管理页列表旁路 / daemon reconcile **节流**自愈。`lwa gateway on` 与 `lwa update` 也会刷新。doctor 的 `lan_url_stale` 检查会告警漂移；`--json` 含 `currentLanIp` / `driftedInstanceIds`。

`lanIpStrategy=manual` 时不自动覆盖落盘。AI 协作流程见 Skill `lwa-review-access-urls`。

### 7.3 访问可用性复核（G2/G5）

```bash
lwa access review    # 对声明 URL 做真探活（含 SPA 别名资源错位：空 200 / 404 / 错误 MIME）
lwa access review --json   # 机器可读
```

逐实例探测：回环 `127.0.0.1:<hostPort>`（权威，区分「服务没起」vs「LAN URL 陈旧」）、lanUrl、routeUrl；对别名入口解析 HTML 的绝对路径 `src`/`href`，对照「无前缀」与「带别名前缀」两种请求——前者空 200 / 404 / 错误 MIME、后者有正确实体 → **IMP-023 风险**（SPA 需构建时设相对 base，如 Vite `base: './'`）。瞬时连接失败（TIMEOUT/REFUSED）**不计** IMP-023。这才是别名下「可用」的真实口径，而非仅看入口 HTML 200。

**主动停止的实例**（`desiredState=stopped`）标 `[SKIP]`，不探活、不计入 FAIL（BUG-301），避免拖垮 `gateway on` / `gateway switch` 的 overall。

`lwa gateway on` 交接收尾后会**默认**跑一次 access review（见 7.6）。

### 7.4 切换后 rebuild 兼容检查（G6）

产品共识：**默认只检查并提示，不自动 rebuild**；需要时显式加开关。

```bash
lwa gateway on                         # 交接 + 默认 access review，仅提示需 rebuild 的实例
lwa gateway on --rebuild-if-needed     # 对 IMP-023 命中实例自动 rebuild
lwa access review                      # 单独复核；文末列出建议 rebuild
lwa access review --rebuild-if-needed  # 复核后对命中实例自动 rebuild
```

- **触发 rebuild 建议 / 自动重建的条件**：仅 IMP-023 别名资源错位（空 200 / 404 / 错误 MIME）。LAN 漂移、端口双开、回环不通等只提示对应命令，不触发 rebuild。
- **rebuild 后复检**：自动重建成功后会再探别名入口；若仍命中 mismatch，报告 `[WARN] rebuild 完成但 IMP-023 仍命中`（退出码非零），不会假绿成「已修好」。
- **注意**：自动 rebuild 不会改应用源码里的 Vite `base`；若未固化 `base: './'`（或等价），重建后别名下仍可能白屏——请先改构建配置再 rebuild。

### 7.5 doctor 新增检查项（建议 F/H）

`lwa doctor` 新增：

- `lan_url_stale`——实例 lanUrl 是否指向失效（漂移）LAN IP（WARN，提示 `lwa access refresh`）。
- `backend_handoff`——enabled 静态 hostPort 上是否 builtin + caddy 双开（FAIL，提示 `lwa gateway off` 再 `on`）。
- `port_contention`——`:2019` / 别名入口上是否有非预期监听者（测试/外部孤儿，§2.7 现场即 pytest 泄漏的 Caddy 占 :2019）；仅 caddy 后端检查。
- `port_pool`（建议 H）——排除 lwa 自用端口（managerPort、staticGatewayPort、registry 已分配 hostPort），不再把这些合法自用端口误报为冲突。
- `workspace_path_consistency`（V0.6.12 / DEV-094；**V0.6.13** 加固）——活跃 manifest/registry 派生路径是否等于当前工作区规范值；Caddy 主配置/sites/aliases 引用是否落在当前工作区且存在；Docker 可用时 SQLite data bind mount Source 是否指向 `apps/<id>/data`。外部 `sourceZipPath`、历史 builds/events 不告警。Docker 不可用、挂载观测失败或 **registry 不可用/读取失败** 时对应子项 SKIP（整体不报假绿 OK）。容器查询失败会中止启动（禁止当作「无容器」绕过挂载 fail-safe）。提示优先 `lwa workspace relocate --verify`，已裸 mv 场景按 `rebuild` / `recover` / `gateway on` 修复。
- `base_image_readiness`（V0.8.7 / issue #14）——未缓存基础镜像是否可拉取；镜像来自第三方 registry（ghcr/quay 等）或 Docker Hub 不可达时按 registry 归类提示（配代理 / 离线预置，而非一律建议 Hub mirror）。
- `database_url_alignment`（V0.8.8 / issue #15，WARN 级纯离线）——SQLite 实例 `docker/.env` 的 `DATABASE_URL` 指向缺失/空库但同目录存在其他非空库时提示疑似错位（数据仍在 `data/`，只是未被指向）；带引号/query 的 URL 与非 sqlite scheme 不误报。

### 7.6 管理页兜底链接（建议 D）

实例列表除 LAN「端口」链接外，额外提供「本机」(`http://127.0.0.1:<hostPort>/`) 链接——LAN IP 漂移失效时仍可本机访问。`caddy start --pingback` 超时假失败（BUG-102）已修复：回退 admin :2019 探活，admin 在线即视为启动成功。

---

## 八、工作区迁移（IMP-042）

同卷改名 / 搬目录优先 CLI，**不要**只 `mv`：

```bash
lwa workspace relocate /abs/NEW --dry-run --json   # 零副作用预演
lwa workspace relocate /abs/NEW --yes              # 执行
cd /abs/NEW && lwa workspace relocate --verify
# 失败：--resume（可读 journal；可显式传 NEW）/ --rollback
```

事务：预检 → 快照备份（SQLite online backup）→ 停服 → 同卷 rename → 结构化改写 manifest/registry/sites/aliases → 有自启才 repair → 恢复 running 意图与 detached 控制面 → 验收。Skill：`lwa-relocate-workspace`。跨盘见 [工作区迁移手册](workspace-rename.md)。

### 8.1 若已经裸 `mv`（V0.6.12 防复发）

代码侧已加固，但**仍应优先用 `relocate`**。若已手工搬迁：

1. 新路径 `pip install -e .` + `lwa autostart install --with-caddy`（或 `repair`）。
2. `lwa doctor`：关注 `workspace_path_consistency`（路径陈旧 / Caddy 引用落在旧根 / data mount 漂移）。
3. `lwa gateway on`：启动前会按当前工作区原子落盘主 Caddyfile（BUG-420），避免首次加载旧绝对路径。
4. SQLite 容器：`lwa start` / `rebuild` 会检测 data bind mount 漂移；漂移时先 fail-safe 救援，`down` 失败或数据冲突会中止并要求人工确认（BUG-421/423/424），不要强行继续。
5. 成功 host/start 后会回写 `appPath` / compose / dockerfile / gatewayConfigPath（BUG-422）；外部合法 `sourceZipPath` 不会被无条件改写。

---

## 相关文档

- [Runtime 工作区说明](runtime-workspace.md) — 目录结构、端口、`.env.local`、资源档位
- [工作区迁移](workspace-rename.md) — 优先 `lwa workspace relocate`；人工手册 DOC-081
- [管理页说明](manager-page.md) — 筛选 / 冗余清理 / 路径别名 / 浏览量 / 取消构建 / LAN stale / gateway switch
- [开机自启](autostart.md) — launchd / systemd；WSL 唤醒与可选 mirrored
- [已知限制](known-limitations.md) — 含 WSL2 宿主准备（内存 / 防火墙 / 文件系统）
- [排障 FAQ](faq.md) — 含 Full Profile / `setup --full --resume` / 症状→日志 / 内置安装脚本
