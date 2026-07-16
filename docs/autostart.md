# 开机自启（IMP-030）

让 `daemon`、`manager`（以及可选的 Caddy `gateway`）在开机/登录后自动可用，并由
**服务管理器直接监管前台进程**——崩溃即拉起（修复 BUG-138：旧方案把
`lwa daemon/manager on` 这种"快速返回的 detached 启动器"当作 `ExecStart`，监管的是
秒退的 CLI 而非真实 watcher/uvicorn）。

## 统一命令：`lwa autostart`

```bash
lwa autostart install [--with-caddy] [--no-enable] [--linger]  # 生成并默认启用
lwa autostart enable | disable                                  # 加载/卸载单元
lwa autostart status                                            # 单元 + 前台进程状态
lwa autostart check [--json]                                    # 完备性深检
lwa autostart repair [--with-caddy]                             # 重写路径/迁移旧单元/重新启用
lwa autostart uninstall [--purge-linger]                        # 停服务 + 删单元
lwa autostart doctor-hints                                      # 人工待办（Docker Desktop/WSL）
```

`install` 生成的单元用**前台入口**：

```
python -m local_webpage_access.<daemon|manager_service|gateway_service> --workspace <工作区根>
```

并固化 `sys.executable`、工作区绝对路径、`PATH`（含 Homebrew，修复 BUG-139：caddy
不在 launchd/systemd 默认 PATH 中）。**真正启用时**（默认 `install` 或随后
`enable`）会把 `daemon` 置 `enabled=true`，前台 watcher 才会在监管下运行；
`install --no-enable` **只生成单元**，不改 `daemon.json` 运行意图。

> `lwa setup --autostart` 仍可用，但已**委托**给 `lwa autostart install`（行为一致，
> 推荐直接用新命令）。

## 平台支持

| 平台 | 后端 | 级别 | 说明 |
| --- | --- | --- | --- |
| macOS | launchd LaunchAgent | 用户登录触发 | `RunAtLoad` + `KeepAlive`（前台进程）；非无人值守系统服务 |
| Linux（Ubuntu 24.04+） | systemd user unit | 用户服务 | `Type=simple` + `Restart=on-failure`；建议 `enable-linger` 登出保活 |
| WSL 2.7+ | systemd user unit | 用户服务 | 同 Linux；发行版不随 Windows 开机自启，需 Windows 登录任务唤醒 |
| Windows（原生） | 任务计划程序 | 登录触发 | 见文末手动配置（本期不自动生成任务） |

## macOS（launchd）

```bash
cd <工作区根>
lwa autostart install            # daemon + manager（managerEnabled 时）
lwa autostart install --with-caddy   # 额外监管 gateway（仅 staticGateway=caddy）
```

生成物 `~/Library/LaunchAgents/`：

- `com.fenix.lwa.daemon.plist`
- `com.fenix.lwa.manager.plist`（`managerEnabled=true` 时）
- `com.fenix.lwa.gateway.plist`（`--with-caddy` 且 `staticGateway=caddy` 时）

plist 要点：`RunAtLoad=true`、`KeepAlive={SuccessfulExit:false}`（前台进程异常退出即
拉起）、`EnvironmentVariables.PATH` 含 `/opt/homebrew/bin` 等。`lwa autostart install`
默认即 `bootstrap` 加载单元；也可手动：

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.fenix.lwa.daemon.plist
launchctl bootout      gui/$(id -u)/com.fenix.lwa.daemon
```

## Linux / WSL（systemd user）

```bash
cd <工作区根>
lwa autostart install            # 生成 ~/.config/systemd/user/lwa-*.service 并 enable --now
sudo loginctl enable-linger $USER   # 登出后 user 服务仍保活（强烈建议）
```

单元 `~/.config/systemd/user/lwa-daemon.service`、`lwa-manager.service`、
`lwa-gateway.service`：`Type=simple`、`Restart=on-failure`、`RestartSec=5`、
`After=network-online.target`（manager 额外 `After=lwa-daemon.service`）。WSL 需
`/etc/wsl.conf` 启用 `[boot] systemd=true`，否则 `lwa autostart` 报 systemd 不可用。

> Python 必须用固定 venv（≥3.13），`install` 固化的是 `sys.executable`。Ubuntu 自带
> 3.12 不满足门槛——`check` 会报 fail，请用 pyenv/uv/Deadsnakes 的 3.13 venv 后 `repair`。

### WSL 的 Windows 侧

WSL 发行版不会随 Windows 开机自动启动。`lwa autostart install` 在 WSL 下会打印一段
Windows 登录任务用的 PowerShell 脚本（也可用 `doctor-hints` 查看），核心是唤醒发行版：

```powershell
# 唤醒发行版并长驻保活：sleep infinity 维持生命周期，systemd 拉起 lwa-*.service
wsl.exe -d <Distro> -- bash -c 'sleep infinity'
```

把它注册到 Windows 任务计划程序（触发器"登录时"）。WSL 网络可能变化，IP 变更后执行
`lwa access refresh` + `lwa access review`。

## 停服与自启的协调（重要）

由于单元 `KeepAlive`/`Restart` 会把崩溃的进程拉回，**停服前必须先停用自启**，否则
`lwa daemon/manager/gateway off` 后进程会被立刻拉起：

```bash
lwa autostart disable     # 先停用单元（macOS 持久 launchctl disable + bootout；systemd disable --now）
lwa daemon off            # 再停进程
```

为避免踩坑，`lwa daemon/manager/gateway off` 已内置协调（`coordinated_disable`）：若对应
自启单元已**加载或启用**（含"已启用但当前 inactive"），会先尝试停用它；**停用成功**才继续
停进程，**停用失败则阻断**（退出码 1，提示先 `lwa autostart disable`）——否则停掉的进程会被
KeepAlive/Restart 立刻拉回，`off` 形同未生效。

## Caddy 所有权

Caddy 由 **LWA 托管**（`lwa-gateway` 单元跑 `gateway_service` 前台，持有 master +
admin :2019）。**切勿同时启用发行版 `caddy.service`**——会争用 `:2019`，`check` 会判
fail。若你已用系统 `caddy.service`，请停用它再 `lwa autostart install --with-caddy`。

## 完备性检查 `lwa autostart check`

逐项检查（任一 fail → 退出码 1；`--json` 供脚本/Skill 消费）：

- 平台 / systemd 可用性（WSL）
- 单元内 Python ≥3.13 且能 `import local_webpage_access`
- 工作区含 `local-web.yml`
- 单元形态为**前台入口**（旧 `… on` 启动器 → fail，可 `repair`）
- 单元已加载/启用，且前台进程真正存活（单元 active 但 MainPID 已死 / **身份不符本工作区前台模块** / 服务进程探测不到 → fail，杜绝假绿）
- 单元 PATH 已固化且**可用**（目录真实存在、含解释器或基础系统目录；gateway 须能按该 PATH 解析 caddy）
- Caddy 二进制可执行；**无系统 `caddy.service` 与外部 `:2019` 占用冲突**（`staticGateway=caddy` 时）
- linger（Linux/WSL：未 linger → warn）
- WSL 发行版 / `/mnt/×` 工作区 / 网络变化提示
- Docker 引擎可达（有容器实例时，warn）

> 重复 `install` 缩减服务集合（如去掉 `--with-caddy`，或关闭 `managerEnabled`）时会
> **差量卸载**不再需要的单元，避免 manifest 外孤儿。迁移 detached 失败时**不会**再
> `enable`/`bootstrap` 该服务，以免监管抢锁失败形成重启循环。

## 旧配置迁移

曾按旧文档（`lwa daemon on` 作 `ExecStart`）安装的单元没有崩溃恢复。`lwa autostart check`
会识别为旧 detached 启动器并报 fail；`lwa autostart repair` 把它改写为前台监管单元并
重新启用。

## Windows（原生，手动配置）

`lwa autostart` 暂不自动生成 Windows 任务（本期非目标）。用任务计划程序创建任务，
触发器"登录时"，操作"启动程序"，**起始于设工作区根**：

| 任务 | 程序 | 参数 |
| --- | --- | --- |
| lwa-daemon | `python.exe` | `-m local_webpage_access.daemon --workspace <工作区根>` |
| lwa-manager | `python.exe` | `-m local_webpage_access.manager_service --workspace <工作区根>` |
| lwa-gateway（可选） | `python.exe` | `-m local_webpage_access.gateway_service --workspace <工作区根>` |

> Windows 下 daemon 的 `O_EXCL` 单实例锁同样生效，重复触发安全。任务计划程序需在
> "操作"里设"起始于(WorkingDirectory)"为工作区根。

## 手工验收清单（不进 CI）

- macOS：`lwa autostart install --with-caddy` 后重新登录 → daemon/manager/gateway 前台
  进程在跑；`lwa autostart check` 全绿。
- Linux：`systemctl --user kill` 杀进程后 `Restart` 自动拉起；`lwa manager off` 不会被
  立刻拉回（协调先 disable）。
- 旧配置：`check` 报 fail，`repair` 后变前台监管。
- WSL：`detect_platform()=="wsl"`；`check` 输出 Windows 唤醒与网络待办。

## 验证

```bash
lwa autostart status          # 单元 + 前台进程
lwa autostart check           # 完备性
curl -s http://127.0.0.1:<managerPort>/api/health   # version 字段确认运行版本
```
