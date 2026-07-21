---
name: lwa-setup-host-environment
description: >-
  Prepare a supported macOS, Linux, or WSL2 host for lwa and initialize the runtime workspace. Use on a new machine, when lwa setup or lwa doctor reports missing or outdated Python, Docker, Compose, Node, or Caddy, or when the user asks which dependencies and setup profile are required.
---

# lwa-setup-host-environment

> 在新机器或全新环境中，引导用户安装 `lwa` 所需的宿主机工具，并完成工作区初始化。

## 何时触发

- 用户第一次在目标主机上使用 `lwa`。
- `lwa doctor` 或 `lwa setup` 报告 Docker / Compose / Python / Caddy / Node 缺失或版本过低。
- 用户询问「怎么装环境」「需要什么依赖」「初始化流程是什么」。

## 输入

1. `lwa setup` 或 `lwa setup --json` 的输出（当前平台、各组件状态、安装指引）。
2. （可选）`lwa setup --script` 生成的参考安装脚本。
3. 用户操作系统：macOS / Linux（Ubuntu LTS 22.04/24.04/26.04、Debian Stable 12/13）/ WSL2（Windows 仅作宿主，原生不支持）。
4. 预期用途：仅静态托管 / 前端构建 / 容器托管（决定哪些组件为必需）。

## 正式支持平台（IMP-036）

| 平台 | 要求 | 备注 |
| --- | --- | --- |
| Ubuntu 裸机 | LTS：22.04/24.04/26.04（版本↔代号配对） | 非 LTS（如 23.10）拒绝 |
| Debian 裸机 | Stable：12=bookworm / 13=trixie | sid/testing 与错配拒绝 |
| WSL2 | 同上发行版；包 ≥2.1.5；systemd=PID 1 | Full/autostart 禁止工作区在 `/mnt/<drive>` |
| macOS | 14+ | 滚动下限 |
| Windows 原生 | **不支持** | 引导改用 WSL2 |

排障：未 init 也可 `lwa doctor --json` 看 `platformSupport`。

## 输出

- **不修改任何工作区文件**（本 skill 只产出操作步骤与命令，不直接写盘）。
- 向用户返回分步清单：
  1. 安装/升级宿主机工具
  2. `pip install -e .` 安装 `lwa`
  3. `lwa setup` 复核
  4. `lwa init` 初始化工作区
  5. `lwa doctor` 完整诊断
  6. `lwa import` / `lwa start` 导入并运行

## 可修改文件

- 无（环境安装在宿主机层面，由用户或脚本执行）。

## 禁止事项

- **不代替用户执行** `sudo`、`brew install`、`winget`、修改系统 PATH 等特权操作（除非用户明确要求）。
- **不自动运行** `lwa setup --script` 输出的脚本；必须先展示脚本内容供用户审阅。
- **不修改** `apps/<id>/`、`registry/`、`local-web.yml`（那是 `lwa init` 之后的事）。
- **不把** Docker socket、privileged 挂载等不安全配置写进安装步骤。

## 组件要求速查

| 组件 | 最低版本 | 何时必需 |
| --- | --- | --- |
| Python | ≥ 3.13 | 始终 |
| fastapi / uvicorn | ≥ 0.138.0 / ≥ 0.45.0 | 始终（`pip install -e .`） |
| Docker | ≥ 29.0.0 | 容器实例 |
| Docker Compose | ≥ 2.40.2，推荐 ≥ 5.2.0（[docker/compose](https://github.com/docker/compose)） | 容器实例 |
| Caddy | ≥ 2.10.0 | **路径别名 / 统一入口 / 浏览量（page 级，Caddy access log）** 的硬依赖：需 `staticGateway=caddy`、Caddy 在 PATH。切换后端用 **`lwa gateway switch caddy`**（勿只手改 YAML）；master 未起时再 `lwa gateway on`。仅临时预览、不用别名时，**default** 档缺失会降级 `builtin`（每实例独立 hostPort；`lwa alias set` 会被 IMP-022 拦截）。**Full** 档要求 Caddy 严格可用，不静默降级 |
| Node.js | ≥ 24（推荐） | 前端 SPA 构建 |

## 处理流程

```text
1. 让用户运行：lwa setup（或 lwa setup --json）（default，无需工作区）
2. 按 fail 项逐条给出平台相关安装命令
   - 优先：内置脚本（见 lwa setup --script）
   - Docker：install-docker-linux.sh / install-docker-macos.sh（默认阿里云 docker-ce 源）
   - Caddy：install-caddy-linux.sh / install-caddy-macos.sh
3. 安装 lwa：pip install -e .
4. 复核：lwa setup → 全部必需项 ok
5. 初始化工作区：
   - default：lwa init
   - Full：优先 `lwa init --full --yes`（一次完成 init + 安装 + 能力闭环）
   - 或先 `lwa init`，再在工作区内 `lwa setup --full --yes`
   - 注意：`lwa setup --full` **需要已初始化工作区**，不可在 init 前单独跑通
6. Full 环境：lwa doctor --profile full / lwa capabilities --json
   - exit 2 / sessionRefreshRequired → 重登或 newgrp 后 lwa setup --full --resume
7. 导入样例：lwa import inbox/xxx.zip → lwa start
8. **升级 lwa 源码后**：优先运行 `lwa update`；需要 AI 协助时见 [lwa-update-runtime](../lwa-update-runtime/SKILL.md)
```

### 装配档位（IMP-032/033）

| 档位 | 命令 | 行为 |
| --- | --- | --- |
| default（缺省） | `lwa setup` / `lwa init` | 检测+指引；缺 Docker 时 TTY 询问是否跑内置脚本；`setup` 无需工作区 |
| full | `lwa setup --full` / `lwa init --full` | 安装 Caddy+Docker+Compose，并验收 Full 能力闭环（CLI/manager/daemon/gateway + Caddy owner）；**需已初始化工作区**；非 TTY 需 `--yes` |

`--default` 与 `--full` 互斥。CI 请用 `--default` 或预装镜像，避免无 `--yes` 的 `--full` 改机器。

Full 闭环要点（对用户输出时必提）：

1. 退出码：`0=ready`，`2=session_refresh_required`（重登/`newgrp docker` 后 `lwa setup --full --resume`），`1=unready`。
2. 验收看 `lwa doctor --profile full` / `lwa capabilities --json`，**不要**只凭「脚本跑完」判断成功。
3. Linux：manager/daemon 须与 CLI 同属 docker 组；改组后重启后台；禁止用系统 `caddy.service` 冒充 LWA Caddy。
4. 日志：`logs/lwa.log` / `manager.log` / `daemon.log` / `gateway.log`（见 FAQ 症状表）。

若用户**只做静态 HTML**、不用容器：

- Docker / Compose 可暂不装；
- 切换到 builtin：用 **`lwa gateway switch builtin`**（IMP-037 原子事务），**不要**手改 YAML 再猜 `on/off` 顺序——但 builtin **不能使用路径别名**，浏览量仅能尽力解析各实例 gateway.log；需要 `/<slug>/` 统一入口时请安装 Caddy 后执行 **`lwa gateway switch caddy`**（必要时再 `lwa gateway on`）；
- Node 仅在前端 SPA 时需要。

选型细节见 [运维手册](../../../../docs/operations-playbook.md)。

## 开机自启（可选）

工作区就绪后，用户希望开机/登录自动拉起 daemon + manager（+ 可选 Caddy），统一用
`lwa autostart`（IMP-030，跨平台前台监管；详见 [lwa-setup-autostart](../lwa-setup-autostart/SKILL.md)）：

- 在工作区目录执行 `lwa autostart install [--with-caddy]`（默认即启用），再
  `lwa autostart check` 复核完备性。
- **macOS**：launchd LaunchAgent，登录触发；**Linux/WSL**：systemd user 服务，建议
  `sudo loginctl enable-linger $USER`；**WSL** 额外需 Windows 登录任务唤醒发行版。
- 停服前先 `lwa autostart disable`（`lwa X off` 已内置协调）；`lwa setup --autostart`
  仍可用但已委托给 `lwa autostart install`。
- 完整说明见 [开机自启文档](../../../../docs/autostart.md)。

## 示例对话

用户：「我刚 clone 下来，怎么开始？」

助手应回复：

```bash
# 1. 检测宿主机环境
lwa setup

# 2. 按提示安装缺失组件后，安装 lwa
pip install -e .

# 3. 再次检测
lwa setup

# 4. 初始化工作区
lwa init

# 5. 完整环境诊断
lwa doctor
```

需要脚本参考时：`lwa setup --script` 打印内置 Docker/Caddy 脚本路径。Full 一次装齐并闭环：
优先 `lwa init --full --yes`，或先 `lwa init` 再 `lwa setup --full --yes`
（需管理员权限，macOS 可能仍要手动开一次 Docker Desktop）。
权限刷新后续跑：`lwa setup --full --resume`；验收：`lwa doctor --profile full`。
