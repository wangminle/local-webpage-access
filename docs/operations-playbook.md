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
3. **Caddy 严格托管**：禁用冲突的系统 `caddy.service`；admin 端口（V0.9.3 起按工作区分配，登记 `run/admin-port`，未分配为 legacy `:2019`）须为本工作区 LWA Caddy（owner / euid / pid）；工作区配置与日志路径可读写。`caddy start` 在 pingback 等待期并行探 admin/pidfile（ADJ-036），就绪即成功。
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
| 防火墙 | 放行实际在用的 `:8080` / 管理页 / 实例口；**不**放行 Caddy admin 端口（:2019 或 `run/admin-port` 分配端口）；**不**默认全开 portPool | Mirrored：Hyper-V firewall 逐端口；另见 ufw。详见 [LAN 与防火墙](known-limitations.md#lan-访问与防火墙) |
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
| master 生命周期 | `lwa gateway on/off/status` 管理（admin 分配端口探活，未分配为 :2019） | 无；每实例 `python -m http.server` 子进程 |
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

**护栏（issue #31）**：携带独立配置的冗余实例——配置了**路径别名**或 **buildEnv**——默认**跳过**不删（预览列表会标注「将跳过」及原因）。如确要删除，追加 `--allow-config-loss`。

管理页也可：实例列表「仅冗余」勾选 → 行内删除单条，或顶部「批量删除冗余」（确认弹窗会列出每个实例的处置；携带独立配置者同样自动跳过）。

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
- 源目录建议写绝对路径。相对路径会先解析：与已记录目录相同则按原绝对路径更新；`--allow-source-change` 换新目录时写入解析后的绝对路径。`node_modules/`、`.git/`、`__pycache__/` 等会被自动剥离。
- `--update` 时传入的 `--from-dir` 路径默认须与实例关联目录一致，否则 Exit 2（不会静默用错目录）。确要更换关联目录并保留 instance id、端口、路径别名和 `data/` 时，加 `--allow-source-change`（交互终端会先确认）；`--dry-run` 只展示「将更换关联目录」，不写盘。不要用删除实例再重新导入来换目录，那会丢掉上述身份。
- 源目录被删除 / 移动后 update 会**报错**（不会回退到 mount 模式）；需确认路径或改用 zip 更新。
- `sourceKind=zip`/`git` 的实例可用 `--from-dir <目录> --update <id>` 原地切换为文件夹源（issue #28，换源不换实例：保留 id / 端口 / 别名 / data/）；不带目录的 `--from-dir --update` 仍仅对 folder 源有效。
- 导入进行中勿立刻 `lwa update`（会等待或跳过重启，避免打断导入）。
- Agent 协作见 Skill `lwa-import-folder`。

### GitHub 源导入与更新（IMP-065）

从 GitHub 仓库一键导入与更新（仅 `https://github.com`、443 端口；私有仓凭据请配在 LWA 宿主机的 git credential helper，不是浏览器所在机器）：

```bash
# 从 GitHub 仓库导入
lwa import --from-git https://github.com/<owner>/<repo>
lwa import --from-git https://github.com/<owner>/<repo> --ref dev --subdir frontend
lwa import --from-git https://github.com/<owner>/<repo> --name "My App" --path-alias myapp

# 从 GitHub 远端更新（ls-remote 无变更探测，远端 OID 未变则零操作跳过；
# 按完整 commit SHA 钉版导入的实例不探测远端、始终报无变更，换版本须显式 --ref 并加 --allow-source-change）
lwa import --from-git https://github.com/<owner>/<repo> --update my-app
```

管理页也可：实例列表顶部「从 GitHub 导入」（LAN + token 可用，不限 loopback）；`sourceKind=git` 实例行内「从 GitHub 更新」按钮。

注意事项：
- 一次性浅克隆进临时 staging（克隆超 180s 或源码 >2 GiB 拒绝导入），实例内不保留 `.git`。
- `--update` 传入的仓库须与实例记录一致（Exit 2，git 实例换仓库请删实例重导）；git 源实例不能用 zip `--update` 更新；zip/folder 源实例传 `--from-git <url> --update` 可原地切换为 git 源（issue #28，换源不换实例：保留 id / 端口 / 别名 / data/）。
- **导入/更新全程持有全局导入锁**（含 `ls-remote` 探测约 30s + 浅克隆最长 180s 的网络等待）。期间 `lwa update` 会等待导入空闲（最多 180s）再决定重启或跳过--慢网络下表现为 update 等待，属预期；确需立即升级可等导入完成后重跑 `lwa update`。
- Agent 协作见 Skill `lwa-import-git`。

### rebuild 与源码陈旧检测（issue #8）

`lwa rebuild <id>` 只重建镜像 / 产物，**不会**同步上游源码。若 folder / git 源
在导入后又有变更，rebuild 出来的仍是 `current/` 里的旧产物。为此：

- rebuild 前自动做**源码陈旧检测**：folder 源比对源目录内容指纹与上次同步
  指纹（`sourceSyncHash`）；git 源做短超时 `ls-remote` 比对远端 OID。检出
  陈旧时打印黄色警告 + 写 registry `source_stale` 事件，**不阻断重建**；
  git 探测离线 / 失败时不警告、不阻断（网络问题不会变成 rebuild 障碍）。
- 静态实例 rebuild 不会先停旧站点。原端口若仍由本实例自己的站点监听，会复用该端口（V0.9.1），不释放登记、不换新 hostPort。站点已停用或端口已被别的进程占用时才重新分配。
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

### 停机失败语义（issue #54 / N3，统一停机语义）

rebuild、挂载漂移修复、restart、runtime recreate、工作区迁移五条路径共用同一
停机语义（`hosting.stop_or_abort`）：

- **停机失败即中止**：`compose down/stop` 报错或容器运行状态无法确认时，抛出
  「实例仍在运行，<操作> 已中止」。先停成功后才会清空 manifest 的
  `containerId`/`imageId`——停机失败时身份保持完整，旧容器继续服务；
- **本来就没在跑视为成功**（漂移修复/迁移除外：这两类需要移除「已停止但仍
  存在」的旧容器，因此无论是否在跑都执行 down）；
- 停机中止不算构建失败：实例不被标 FAILED，registry 状态回到操作前观测值，
  本轮 build 行如实收尾为 failed；
- restart 停机失败直接报错，**不再**吞异常后误报「已重启」；
- 迁移路径与 S1-05 停机安全门互补：停机失败的 HostingError 记入 quiesce
  errors，后置严格校验（`container_id_strict`）发现容器仍存活时计入
  failures 阻断 MOVE（D6），`--force-quiesce` 可显式越过。

### 重建事务化：先建后换 + 失败补偿（issue #51 / N7，S2-03）

对**已在跑的容器实例**执行 `lwa rebuild`（以及触发完整重建的 `--update`）
时，编排顺序为**先建后换**（首次部署、实例未在跑时没有旧容器可保，仍走原流程）：

1. 非 root 身份预检（issue #20）先于任何改动；
2. 当前 `docker/` 下的 compose.yaml / Dockerfile / .env 复制到 `docker/.prev/`，
   旧镜像打上 `<project>-<service>:prev` 标签——这两份快照是补偿的锚点；
3. 渲染新生成物并 `docker compose build`。**旧容器全程继续服务**（期间实例
   status 保持观测值，构建进度只记 `lastOperation=building`）；
4. build 失败：恢复 `.prev` 生成物，`lastOperation=failed`，命令非零退出并提示
   「构建失败，旧版本仍在服务」——实例状态、端口登记、manifest 身份一律不动；
5. build 成功后才轮到旧容器：数据救援（BUG-205，`docker cp` 把容器内 SQLite
   救出到宿主 `data/`）→ `stop_or_abort` 停机（失败即中止、身份保持完整）→
   停机成功后才清空 `containerId`/`imageId` 并落盘（BUG-300）；
6. `up` 新版本并做健康/探针验证。

第 6 步失败（或停机后任何一步失败）时自动**补偿回旧版本**：
down 新容器 → 恢复 `.prev` 生成物 → `:prev` 重新打回 `:latest` → up 旧版本
→ 验证 HTTP。补偿成功：实例回到 RUNNING，操作仍记 `lastOperation=failed`
（带 `rollback: restored`），CLI 报「重建失败，已回滚到旧版本并恢复服务」；
补偿也失败：实例标 FAILED，`rollbackNotes` 列出残留项（未清掉的容器、缺失的
快照等），需人工介入。runtime recreate（配置指纹变化触发的 force-recreate）
同样先快照生成物，失败时恢复旧 compose 并拉起旧容器（镜像未变，无需 build）。

**逃生口**：低内存小主机若担心「新旧镜像并存」的磁盘/内存压力，可在实例
manifest 的 `container` 段设置 `"rebuildStrategy": "stop-first"`，恢复旧的
先停后建顺序（此时构建失败即没有旧版本可回退）。默认未设置为 `build-first`。
`docker/.prev/` 与 `:prev` 标签由下一轮重建自动覆盖，平时无需手动清理。

### 镜像版本留档与有限回滚（issue #51 远期，S3-01 / DEV-157，V0.9.4）

容器实例**每次成功构建**自动留档：新镜像打 `<project>-<service>:b<build_id>`
标签，三件生成物（compose.yaml / Dockerfile / .env）快照到
`apps/<id>/docker/builds/<build_id>/`，镜像引用 / 镜像 ID / 源码哈希回写到
registry 的 builds 行。`latest` 始终只是指针。

```bash
lwa versions <id>    # 列出构建号 / 时间 / 源码哈希 / 镜像是否仍在 / 构建结果 / 占用
lwa status <id>      # 单实例视图追加「保留版本：N 个 · 占用 X」一行
lwa rollback <id> [--to <build-id>] [--yes]
```

要点：

* **范围（D8）**：只回滚镜像与生成配置，**不回滚 `data/`**。回滚用快照生成物 +
  对应 `b<build_id>` 镜像执行 up，随后跑实证验证与别名守卫（退出码语义同
  rebuild：内容级别名失败为 4）。回滚后验证失败时自动补偿——恢复原生成物、
  原镜像打回 `:latest`、重新 up，并对恢复结果做实证验证（基础存活 + 必选探针）：
  通过才报「已恢复原版本」；未通过则如实报「未能恢复原版本」、实例标 failed，
  不谎报 running。
* **迁移命令守门**：`preStart` / `buildHooks` 含迁移命令（alembic / migrate /
  prisma）时，回滚必须显式 `--yes`，CLI 会醒目提示「旧版本代码可能无法兼容
  当前数据库结构」。管理页对应勾选「我已了解风险」。
* **保留与 GC**：全局配置 `imageRetention`（默认 3）控制每个实例保留的最近
  成功版本数；**部署成功后** GC 超出部分。正在使用的镜像与上一个可用版本
  **永不删除**（GC 只删 `b<build_id>` 标签，共享层不受影响；快照目录一并清理）。
  留档占磁盘属预期，`lwa status <id>` 可见占用；需要更多历史就调大
  `imageRetention`。
* **revision**：回滚是生命周期操作，管理页/API 携带 `expectedRevision` 乐观
  并发（过期 409 `revision_conflict`），成功后 revision +1。成功落盘在实例锁内完成。
* **回滚钉住**：成功回滚后 start/restart **不会**按 `current/` 里的新源码重渲染生成物
  （否则等于把刚退掉的版本再部署回来）。要回到当前源码请显式 `lwa rebuild`。
  钉住期间 `lwa configure --extra-volume` 也要等 rebuild 才生效。
* **daemon 自愈**：全局配置 `selfHealPreferLastGood`（默认 true）——daemon 发现
  「最近一次操作失败且容器缺失」时，优先拉回上一可用镜像版本（事件
  `rollback_self_heal`），而不是按当前源码重建；拉回失败自动回退常规恢复。
  容器仍在（如旧版本持续服务）时不触发。

### Flask/Django 生产服务器与 WSGI 入口推断（V0.9.6 / IMP-066）

无显式启动命令的 Flask/Django 实例，LWA 在扫描时推断 WSGI 入口并生成
**Gunicorn** 生产命令（低配参数 `--workers 1 --threads 4`，不按 CPU 放大）：

* **命令优先级**：用户显式 `entry.start` > 项目已声明 Gunicorn > LWA 可证明的
  WSGI 生成命令 > 开发服务器 fallback。显式使用 Uvicorn 等 ASGI 服务器的项目
  不改写。
* **入口推断（只认唯一可证明）**：Flask 认根 `app.py` / `main.py` / `wsgi.py` /
  `app/__init__.py` 中的 `app = Flask(...)` 或 `create_app` 工厂（多候选不猜）；
  Django 认 `manage.py` 根 + 唯一 `*/wsgi.py`。
* **平台依赖分层**：生成 Gunicorn 命令且项目未声明 Gunicorn 时，Dockerfile 内
  分层安装平台选定的版本范围（`gunicorn>=21,<23`）；不回写用户的
  requirements/pyproject。uv 项目装进 uv venv。
* **fallback 警告**：无法唯一推断时保留 `flask run` / `manage.py runserver` 不
  阻断导入，但扫描备注（`[预检警告]`，入 manifest `preflightSummary`）与
  doctor `dev_server_fallback` 同源告警。
* **存量兼容**：仅 `rebuild` / 源码更新后重生成时应用新规则；升级 LWA 不改已
  运行容器的 CMD。

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

### 构建慢 / apt 系统包失败（issue #34 / #35）

系统包请用 `lwa configure <id> --system-deps ffmpeg`（渲染在 `COPY current/` 之前，
带 apt 源链与 cache mount），不要只写 `buildHooks` 里的裸 `apt-get`。china 默认
阿里云 → 清华 → `deb.debian.org`，每源快速失败后 `||` 切源。排障口径：

- **镜像源不可达**（`Failed to fetch` / `Connection failed`）→ 调 `aptFallbacks` /
  `aptRetries` / `aptTimeout`，或把包装入 `systemDeps` 后 rebuild。构建长时间卡在 apt 而不报错时，
  设 `aptBudget`（每个源的总时间预算，秒）让慢源到点切换。
- **cannot allocate memory / OOMKilled** → 提高 Docker Desktop 内存，避免并发构建。
- **仅出现 Killed** → 原因未确认，结合 ExitCode / OOMKilled 与日志，不要直接当成 VM 内存不足。
- **No space left on device** → `docker builder prune`。
- **运行中服务版本落后于磁盘代码** → `lwa doctor` 的 `service_version_drift`；对齐用
  `lwa services restart`（保留自启动意图），需要拉代码时再用 `lwa update`。
- **daemon 反复自动重建** → 连续**构建**失败会小时级熔断，5 次后需 `lwa start` / `lwa rebuild`
  人工清熔断。轻量 `compose start` 失败只走内存退避。`lwa status` 与管理页详情「构建熔断」字段可见。

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
5. **端口漂移**：容器 restart 后 hostPort 若变化，`_sync_alias_port`（IMP-021）会自动重写别名片段并 reload，无需手动处理。静态实例 rebuild 在本站点仍监听时复用原端口（V0.9.1）。

> SPA 子路径提示（IMP-023）：Vue/React 等用绝对资源路径（`/assets/…`）在 `/<slug>/` 下会 404 白屏；构建时设相对 base（Vite `base: './'`）或 `--base=/<slug>/`。纯静态 HTML 不受影响。

> 无斜杠访问排障：`http://<LAN-IP>:<staticGatewayPort>/<slug>`（不带结尾 `/`）应返回 `301`，`Location` 为 `/<slug>/`。若返回 `200` 且响应体为空（浏览器白屏），通常是旧版本写下的片段 `redir /<slug>/ permanent`，被 Caddy 当成路径匹配器。V0.9.2 起修复写法为 `redir * /<slug>/ permanent`；升级后执行 `lwa update`（收尾会重写存量片段）或 `lwa gateway on`，再用 `curl -sI http://127.0.0.1:<staticGatewayPort>/<slug>` 确认 `301` 与 `Location: /<slug>/`。

---

## 四、Caddy master 排障

> **admin 端口（V0.9.3 / BUG-783 起）**：每个工作区的 Caddy admin API 使用独立分配端口
> （20201–20500，登记在 `run/admin-port`，主 Caddyfile 全局选项块渲染
> `admin 127.0.0.1:<port>`），不再默认共享 `:2019`。旧版本工作区在未分配前仍用
> `:2019`；升级后首次 `lwa gateway on` / reload 会把仍在线的本工作区 `:2019` 旧
> master 优雅停掉并迁移到分配端口（不归本工作区的 `:2019` 进程不会被触碰）。
> 该改动在结构上阻断「其他工作区（含已删除工作区）的孤儿 daemon 向共享 :2019
> 推配置清空本网关」的事故模式。

| 现象 | 排查 | 处置 |
| --- | --- | --- |
| `lwa gateway status` 显示未运行 | admin 端口不可达（`lwa gateway status` 输出 `adminPort`） | `lwa gateway on`（自动 validate→start→探活） |
| 别名 502 / 站点不通 | 实例 hostPort 未监听 / 容器未起 | `lwa status <id>` 看状态；`lwa start <id>`；实例 `gateway_down` 用 `lwa recover <id>`（或管理页「恢复」/ `POST /api/instances/{id}/recover`） |
| `caddy validate` 报悬空 import | BUG-069 类残留（已根治，偶发于历史脏配置） | `lwa gateway off` 再 `on`，会基于实际存在的 conf 重组主 Caddyfile |
| 切 builtin 后 Caddy 还在跑 | stale pid / 旧 master | `lwa gateway off`（不校验版本，强制 `caddy stop` + 清 `run/gateway.json`） |
| `lwa doctor` 报 Caddy 健康 FAIL | admin/validate/站点端口探测 | 按 doctor 提示处置；常见为 master 未起（`lwa gateway on`） |
| `run/caddy.pid` 丢失但 admin 端口仍在监听 | 孤儿 master（进程在、pidfile 无） | `lwa gateway on` 会在四重安全闸通过后自动认领并补写 pidfile；不要先 `kill` |
| 其他工作区的 daemon 疑似向 :2019 推配置（真实工作区网关配置被清空、8080/8443 全断） | BUG-783 事故模式：旧版本（V0.9.3 前）多工作区共享 admin :2019，孤儿进程 POST /load 可达本网关 | 本工作区升级到 V0.9.3+（admin 独立分配端口，:2019 与真实工作区解绑）；kill 孤儿进程后 `lwa gateway on` 恢复 |

健康探针（IMP-020）：`lwa doctor` 在 caddy 模式会探 admin（分配端口，未分配为 :2019）+ 主 Caddyfile `caddy validate` + 别名入口 / 各 enabled 站点 hostPort 可达性 + stale pid 提示；端口监听者身份因缺少 lsof 无法校验时，`port_contention` 给 WARN「归属未确认（缺少 lsof）」而非静默跳过。

### Caddy 配置位置

- admin 端口登记：`run/admin-port`（首次 reload/start 分配，勿手改）。
- 主配置：`static-gateway/Caddyfile`（由 `_assemble_main_config` 基于实际存在的片段组装，**永不 import 不存在文件**；含全局选项块 `admin 127.0.0.1:<分配端口>`）。
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
- **daemon 自愈**（DEV-042）：watcher 启动时与每 60s 执行 `reconcile()`，恢复 `desired=running` 但状态偏离的实例。Caddy 后端且网关被显式 `lwa gateway off` 时跳过 caddy 静态。V0.8.8（issue #16）起自愈起止与 `lifecycle_stage` 阶段会镜像一份到 `logs/lwa.log`（完整日志仍在 `logs/daemon.log`）；V0.9.3（issue #59 / S2-08）起镜像按操作来源区分，行首带 `source=`（`source=daemon` / `source=manager` / `source=agent`）——manager 与 Agent 通道触发的部署阶段同样落主日志，CLI 触发的本就在 `lwa.log` 不重复镜像——构建失败后盯主日志即可看到"自动恢复开始 / 已自动重试并恢复"，不再表现为静默恢复。
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

### 6.1 操作时间线与副本一致性（V0.9.4 / S3-04、S3-02）

- **操作时间线**：`lwa logs <id> --timeline`（`--tail` 控制条数）按时间倒序列出生命周期事件、构建记录与最近一次操作态，每行标注发起方 `[cli]` / `[manager]` / `[daemon]` / `[agent]`，构建行附耗时。历史事件没有来源记录，显示 `[unknown]`。管理页实例详情的「最近操作」区块展示同一数据。排查"谁触发了这次重建、失败在哪一步"时优先看它，再看 `lwa logs <id> --category build`。
- **副本一致性**：`lwa doctor` 的 `state_consistency` 检查只读地核对 manifest 与 registry（状态、desiredState、runtime、hostPort）、生成物是否被手改、运行容器的 `extraVolumes` 挂载（目标路径、宿主源路径与读写模式逐项比对；Docker Desktop 的 `/host_mnt` 翻译前缀自动归一）、别名片段与 manifest 是否一致、孤儿片段、主 Caddyfile 的 import 是否有效。别名片段与主 Caddyfile 的检查以**实际网关后端**为准（配置 caddy 但二进制缺失被降级 builtin 时不做片段级判定）；未在服务实例的片段按停止语义判定（容器停止有意保留片段、不算残留，静态实例仅明确 stopped 后的残片段才报告）。发现漂移时 WARN 并给出修复建议，**不会自动修复**。daemon 每 30 分钟巡检一次，漂移集合变化时写入 `consistency_drift` 事件（同一组漂移只记一次），可用 `lwa logs <id> --timeline` 查看。已知边界（CHK-402）：生成物比对以部署基线 configHash + mtime 判定，「先手改生成物、之后又执行过 `lwa configure`」会被判为待生效而不报告——该场景由配置收敛在下一次 `start`/`restart` 时覆盖并告警兜底（S2-04）；挂载检查只在 `doctor` 执行，daemon 巡检不做 Docker 探测。
- 无主锁文件属于设计上的正常状态（锁文件按设计保留、不删除），不纳入漂移判定。

### 6.2 调整容器额外挂载（V0.9.4 / S3-05）

- 设置：`lwa configure <id> --extra-volume /宿主路径:/容器路径[:ro]`（可重复；每次都是整组替换）。
- 清空：`lwa configure <id> --clear-extra-volumes`。
- 保存前会做结构校验并执行与生成 compose 相同的安全审计；敏感路径（如 `/etc`、家目录凭据目录）直接拒绝，manifest 不变。
- 挂载变化在 `lwa restart <id>` 时生效（无需 rebuild）。之后可用 `lwa doctor <id>` 或 `lwa doctor` 的 `state_consistency` 检查确认运行容器已挂上目标路径。

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
lwa access refresh   # 用当前 LAN IP 重算所有实例 lanUrl/routeUrl 并落盘；TLS 开启且 Caddy 在线时同步重载站点绑定（V0.9.1）
lwa update           # 一键升级（V0.8.0 含源码快进）收尾：后台重启 -> 等待就绪（V0.8.2）-> refresh（+ 默认轻量 review）
lwa doctor --access  # 诊断同时复用 access review
```

DHCP 换网后管理页「端口」链接会**读时合成**当前 IP（无需先 refresh）；落盘由管理页列表旁路 / daemon reconcile **节流**自愈。`lwa gateway on` 与 `lwa update` 也会刷新。doctor 的 `lan_url_stale` 检查会告警漂移；`--json` 含 `currentLanIp` / `driftedInstanceIds`。WSL NAT 下该「当前 IP」是仅宿主可达的 guest 地址（链接文案显示为「宿主」），refresh 只刷新 guest 地址落盘、不能替代 Windows 侧转发——局域网入口判断见 §7.5 的 `wsl_lan_exposure`。

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

- `lan_url_stale`——实例 lanUrl 是否指向失效（漂移）LAN IP（WARN，提示 `lwa access refresh`）。在 **WSL NAT** 下，即使 host 与当前 guest 地址一致也会 WARN：该地址仅 Windows 宿主可达，不能当作局域网已通。
- `wsl_lan_exposure`——WSL 下只读发现 Windows 网卡、portproxy 与实际网络模式（SKIP 非 WSL）。缺少局域网转发、仅有未经验证的候选、或模式/权限未知时 WARN；建议命令供管理员审阅，LWA 不会自动执行。`--json` 另含 `wslNetwork`。
- `backend_handoff`——enabled 静态 hostPort 上是否 builtin + caddy 双开（FAIL，提示 `lwa gateway off` 再 `on`）。
- `port_contention`——admin 分配端口（未分配为 :2019，§2.7 现场即 pytest 泄漏的 Caddy 占 :2019）/ 别名入口上是否有非预期监听者（测试/外部孤儿）；仅 caddy 后端检查；缺少 lsof 无法校验监听者身份时给 WARN「归属未确认（缺少 lsof）」（S2-07），不再静默 SKIP。
- `port_pool`（建议 H）——排除 lwa 自用端口（managerPort、staticGatewayPort、registry 已分配 hostPort），不再把这些合法自用端口误报为冲突。
- `workspace_path_consistency`（V0.6.12 / DEV-094；**V0.6.13** 加固）——活跃 manifest/registry 派生路径是否等于当前工作区规范值；Caddy 主配置/sites/aliases 引用是否落在当前工作区且存在；Docker 可用时 SQLite data bind mount Source 是否指向 `apps/<id>/data`。外部 `sourceZipPath`、历史 builds/events 不告警。Docker 不可用、挂载观测失败或 **registry 不可用/读取失败** 时对应子项 SKIP（整体不报假绿 OK）。容器查询失败会中止启动（禁止当作「无容器」绕过挂载 fail-safe）。提示优先 `lwa workspace relocate --verify`，已裸 mv 场景按 `rebuild` / `recover` / `gateway on` 修复。
- `base_image_readiness`（V0.8.7 / issue #14）——未缓存基础镜像是否可拉取；镜像来自第三方 registry（ghcr/quay 等）或 Docker Hub 不可达时按 registry 归类提示（配代理 / 离线预置，而非一律建议 Hub mirror）。
- `database_url_alignment`（V0.8.8 / issue #15，WARN 级纯离线）——SQLite 实例 `docker/.env` 的 `DATABASE_URL` 指向缺失/空库但同目录存在其他非空库时提示疑似错位（数据仍在 `data/`，只是未被指向）；带引号/query 的 URL 与非 sqlite scheme 不误报。
- `caddy_health` admin 归属诊断（V0.9.6 / BUG-794）——master 未运行时用 lsof 诊断登记 admin 端口的占用者（name/pid），被外部进程占用时 WARN 并给出针对性处置建议；缺 lsof 时如实标注「归属未确认」。不做自动换端口（admin 端口已渲染进主配置）。
- `dev_server_fallback`（V0.9.6 / IMP-066，WARN 级）——容器实例仍以框架开发服务器（`flask run` / `manage.py runserver`，含 `./manage.py` 相对路径形态）启动时告警；判定谓词与扫描期 fallback 备注同源。
- `frontend_artifacts_staleness`（V0.9.6 / #61 检查部分，WARN 级）——预构建 `static/` 产物时效性分级：同仓布局前端源码晚于产物记「疑似」（WARN + 重建建议）；前端源不在导入树内（跨仓）或 mtime 不可区分记「无法判断」（信息性汇总，不升 WARN）；确证需构建期源码指纹（#61 预构建落地后接入）。只告警不自动构建，与别名资源检查（base 配错）相互独立。
- `node_toolchain`（V0.9.8 / BUG-860·877，WARN 级）——宿主前端构建（frontend-static 实例）依赖的 Node 工具链预检：node / corepack / pnpm / yarn 可达性与版本如实列出，缺失只影响宿主构建路径（纯静态 / 容器实例不受影响）。宿主构建按 `packageManager` 声明经 corepack 协商版本（如 `corepack pnpm@<ver>`），不再要求 `corepack enable`——macOS 无 sudo 写 `/usr/local/bin`、Ubuntu apt 版 node 无 corepack 均可工作；构建失败提示附 `apps/<id>/logs/build.log` 路径。

### 7.6 管理页兜底链接（建议 D）

实例列表除 LAN「端口」链接外，额外提供「本机」(`http://127.0.0.1:<hostPort>/`) 链接——LAN IP 漂移失效时仍可本机访问。`caddy start --pingback` 超时假失败（BUG-102）已修复：回退 admin 探活，admin 在线即视为启动成功。

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
- [HTTPS 传输加密](https.md) — `gatewayTls: internal`、根证书导出与信任、明文收敛

### 导入预览和清理前确认

- 全新导入不要传 `--dry-run`（退出码 2）；只对 `--update <id>` 使用更新预演。
- 用 `lwa configure <id>` 查看 buildEnv/别名跟随/重复保留配置；构建参数修改后手动 rebuild。
- 有意部署的重复实例可执行 `lwa configure <id> --acknowledge-redundancy` 保留；反向选项可撤销。
- 批量清理前先停止待删实例。`--allow-config-loss` 仅允许损失独立配置，不会突破运行态保护；管理页同样适用。
