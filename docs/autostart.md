# 开机自启

让 `daemon`（inbox 自动导入 + 自愈 reconcile）、`manager`（管理页）以及可选的
Caddy 网关在开机/登录后自动拉起。三条 `on` 命令本身幂等——已运行则无操作，进程
崩溃恢复由 daemon 的 `reconcile` 与心跳锁保证，故自启只需"登录时跑一次"。

## macOS（launchd，推荐）

在工作区目录执行：

```bash
lwa setup --autostart            # 生成 daemon + manager plist
lwa setup --autostart --with-caddy   # 额外含 caddy 网关（仅 staticGateway=caddy）
```

生成物位于 `~/Library/LaunchAgents/`：

- `com.fenix.lwa.daemon.plist`
- `com.fenix.lwa.manager.plist`（`managerEnabled=true` 时）
- `com.fenix.lwa.gateway.plist`（`--with-caddy` 且 `staticGateway=caddy` 时）

启用 / 取消：

```bash
launchctl load   ~/Library/LaunchAgents/com.fenix.lwa.daemon.plist
launchctl unload ~/Library/LaunchAgents/com.fenix.lwa.daemon.plist
```

plist 用绝对 python 路径执行 `python -m local_webpage_access <daemon|manager|gateway> on`，
`WorkingDirectory` 指向工作区根，`RunAtLoad=true`、**不设 `KeepAlive`**（避免与
`lwa X off` 冲突）。stdout/stderr 写入 `<workspace>/logs/launchd-*.out|err`。

## Linux（systemd user service）

`lwa setup --autostart` 在 Linux 会报错指引到此。手写 user unit（无需 root），
放入 `~/.config/systemd/user/`：

`lwa-daemon.service`：

```ini
[Unit]
Description=lwa inbox watcher (daemon)
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/workspace
ExecStart=/usr/bin/python3 -m local_webpage_access.daemon on
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

`lwa-manager.service`（`managerEnabled=true` 时）：

```ini
[Unit]
Description=lwa manager page
After=network.target lwa-daemon.service

[Service]
Type=simple
WorkingDirectory=/path/to/workspace
ExecStart=/usr/bin/python3 -m local_webpage_access.manager on
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

启用（登录后自启 + linger 保证未登录也运行）：

```bash
systemctl --user daemon-reload
systemctl --user enable --now lwa-daemon.service lwa-manager.service
loginctl enable-linger "$USER"   # 允许用户服务在未登录时运行
```

> `WorkingDirectory` 必须是含 `local-web.yml` 的工作区根（`lwa daemon/manager on`
> 通过当前目录定位工作区）。`ExecStart` 的 python 路径换成实际解释器
> （`which python3`，虚拟环境用绝对 venv 路径）。Caddy 网关建议直接用系统
> `caddy` service，或在 `lwa-gateway.service` 里跑 `python -m local_webpage_access gateway on`。

## Windows（任务计划程序）

`lwa setup --autostart` 在 Windows 会报错指引到此。用任务计划程序创建三个任务，
触发器"登录时"，操作均为"启动程序"：

| 任务 | 程序 | 参数 | 起始于 |
| --- | --- | --- | --- |
| lwa-daemon | `C:\path\to\python.exe` | `-m local_webpage_access daemon on` | 工作区根 |
| lwa-manager | `C:\path\to\python.exe` | `-m local_webpage_access manager on` | 工作区根 |
| lwa-gateway（可选） | `C:\path\to\python.exe` | `-m local_webpage_access gateway on` | 工作区根 |

PowerShell 等价（示例）：

```powershell
$ws = "D:\path\to\workspace"
$py  = "C:\path\to\python.exe"
Register-ScheduledTask -TaskName "lwa-daemon" -Trigger (New-ScheduledTaskTrigger -AtLogOn) `
  -Action (New-ScheduledTaskAction -Execute $py -Argument "-m local_webpage_access daemon on" -WorkingDirectory $ws)
```

> "起始于/WorkingDirectory" 必须设为工作区根。Windows 下 daemon 的 `O_EXCL`
> 单实例锁同样生效，重复触发安全。

## 验证

```bash
lwa daemon status     # watcher 运行中
lwa manager status    # 管理页运行中
lwa gateway status    # staticGateway=caddy 时 master 在线
curl -s http://127.0.0.1:<managerPort>/api/health   # version 字段确认运行版本
```
