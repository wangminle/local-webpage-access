---
name: lwa-setup-autostart
description: >-
  Set up, inspect, and repair lwa login or boot autostart for daemon, manager, and optional gateway through launchd or systemd user services. Use when users ask for automatic startup, services disappear after login or reboot, lwa autostart check fails, or an older detached configuration must be migrated.
---

# lwa-setup-autostart

> 引导用户用 `lwa autostart` 完成开机/登录自启动：跨平台（macOS launchd / Linux·WSL
> systemd user）**前台监管** daemon + manager（+ 可选 gateway），并做完备性检查与修复。
> 对应 IMP-030 / BUG-138 / BUG-139。

## 何时触发

- 用户问「怎么设置开机自启 / 登录后自动启动 / 开机自动跑」。
- 登录或重启后管理页 / 业务页面没起来，怀疑自启没生效。
- `lwa autostart check` 有 fail 项，或曾按旧文档（`lwa daemon on` 作 `ExecStart`）装过。
- 用户从 detached 启动方式迁移到前台监管。

## 输入

1. `lwa autostart check --json` 的输出（平台、各检查项 ok/warn/fail）。
2. 工作区根（含 `local-web.yml`，`lwa autostart` 需在工作区目录运行）。
3. 是否需要 Caddy 网关自启（`staticGateway=caddy` 且需要别名入口时）。

## 产品口径（每次输出必含一句）

- **macOS**：用户**登录触发**型自启（LaunchAgent），**不是**无人值守系统级服务。
- **Linux**：systemd **user** 服务，登出后需 `enable-linger` 才保活。
- **WSL**：Linux 侧同 Ubuntu；但发行版**不随 Windows 开机自启**，需 Windows 登录任务唤醒。

## 输出

- **不修改任何工作区业务文件**；只指导用户执行 `lwa autostart …`（写用户级单元，非 sudo 改系统）。
- 分平台给出最小命令序列；WSL 时分「Linux 侧 / Windows 侧」两段清单。
- 必含：停服前先 `lwa autostart disable`（否则 `lwa X off` 被立刻拉回）；**`lwa update` 重启已内置 `coordinated_restart`（自启在管时交监督器重启，勿手搓 stop+detached start）**；Caddy 由 LWA 托管、
  **禁止同时启用系统 `caddy.service`**。

## 可修改文件

- 无（自启单元由 `lwa autostart install` 写入 `~/Library/LaunchAgents/` 或
  `~/.config/systemd/user/`；本 skill 只产出命令与步骤）。

## 禁止事项

- **不支持 Windows 原生自启**；WSL 的 Windows 侧唤醒脚本只给清单让用户自行注册登录任务。
- **不宣称 macOS 无人值守高可用**（LaunchAgent 是登录触发）。
- **不建议同时启用发行版 `caddy.service`**（与 LWA gateway 争用 `:2019`）。
- **不**为绕过 Python ≥3.13 门槛而改用系统旧 Python；指导用 3.13 venv 后 `repair`。
- **不**在自启仍启用时教用户用 `manager/daemon off && on` 做代码升级——应走 `lwa update`。

## 处理流程

```text
1. 让用户在工作区目录运行：lwa autostart check --json
2. 按 platform 与 fail 项给最小命令：
   - macos：lwa autostart install [--with-caddy]  →  lwa autostart check
   - linux：lwa autostart install [--linger] → （若未 --linger）sudo loginctl enable-linger $USER → check
   - wsl  ：Linux 侧同上 + Windows 侧注册登录任务（lwa autostart install 会打印脚本）
3. 旧 detached 单元（check 报 unit 身份 fail）：lwa autostart repair
4. 停服说明：lwa autostart disable 再 lwa X off
5. 升级/热重载：优先 lwa update（自启在管时 coordinated_restart，无需手动 off/on）
6. 复核：lwa autostart status / curl /api/health
7. Linux + 容器：确认 docker 组已生效（newgrp/重登）并重启 manager/daemon；
   Full 环境再跑 lwa doctor --profile full / lwa capabilities --json
```

## 平台要点

| 平台 | 一键命令 | 关键点 |
| --- | --- | --- |
| macOS | `lwa autostart install --with-caddy` | LaunchAgent，登录触发；KeepAlive 崩溃即拉起 |
| Linux | `lwa autostart install [--linger]` | systemd **user** 单元；`--linger` 尝试 `enable-linger`（也可手写 `sudo loginctl enable-linger $USER`）；docker 组靠登录会话；Python 须 3.13 venv |
| WSL | 同 Linux + Windows 唤醒任务 | 需 `/etc/wsl.conf` `[boot] systemd=true`；包 ≥2.1.5；工作区勿放 `/mnt/<drive>`（Full/autostart fail-closed） |
| Windows 原生 | **不支持** | 仅作 WSL2 宿主；见 [开机自启文档](../../../../docs/autostart.md) |

> Full Profile 进阶：若改用 **system** unit 并以 `User=` + `SupplementaryGroups=docker` 启动，可减少「重登才继承 docker 组」问题；当前 `lwa autostart` 默认仍生成 user unit。

## 示例对话

用户：「我希望开机后 lwa 自动起来。」

助手（macOS）应回复：

```bash
cd <工作区根>
lwa autostart install --with-caddy   # 生成并启用 launchd 单元（前台监管）
# 仅生成、暂不启用：lwa autostart install --no-enable（不改 daemon.json）
lwa autostart check                  # 复核解释器/PATH/进程身份/Caddy 完备
```

> macOS 是**登录触发**型自启（非无人值守系统服务）。停 daemon/manager 前先
> `lwa autostart disable`，否则 KeepAlive 会立刻拉回。

详见 [开机自启文档](../../../../docs/autostart.md) 与 [lwa-setup-host-environment](../lwa-setup-host-environment/SKILL.md)。
