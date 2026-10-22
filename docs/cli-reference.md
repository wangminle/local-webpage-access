# 命令参考

本文集中说明 `lwa` 的常用命令与配置入口。首次使用请先读 [README](../README.md#中文文档)。

命令示例中的 `<ID>` 是导入结果中的实例 ID，尖括号表示需要替换的参数。执行 `lwa <命令> --help` 查看全部选项；全局 `-v` 打开 DEBUG 日志。

## 工作区与安装

| 命令 | 用途 |
| --- | --- |
| `lwa setup` | 检测宿主工具，无需已有工作区；缺失依赖时给出安装指引，交互环境可询问安装 Docker |
| `lwa init --workspace <目录>` | 创建工作区、配置与 registry；默认目录为当前目录 |
| `lwa init --workspace <目录> --full --yes` | 初始化后安装 Docker/Caddy，并验收运行能力 |
| `lwa setup --full --yes` | 在已有工作区执行完整装配与能力验收 |
| `lwa setup --full --resume` | 权限或会话刷新后继续验收 |
| `lwa --workspace <目录> <命令>` | 从任意目录操作指定的已有工作区 |
| `lwa workspace use <目录>` / `lwa workspace use --clear` | 记住或清除默认工作区 |
| `lwa workspace relocate <新目录> --dry-run` | 预检同卷工作区迁移；正式迁移与恢复见[迁移手册](workspace-rename.md) |
| `lwa update` | 更新 LWA 自身；仅接受干净、未分叉、非浅克隆且未 detached 的源码仓库 |
| `lwa version` | 显示版本 |

工作区定位顺序：显式 `--workspace` → 当前目录向上查找 `local-web.yml` → `LWA_WORKSPACE` → `lwa workspace use` 保存的目录。

`--full` 以运行能力验收为完成条件。若提示 `sessionRefreshRequired`，按提示重登或刷新组权限后再执行 `--resume`。详见[装配与运维](operations-playbook.md)。

## 导入与更新

| 命令 | 用途 |
| --- | --- |
| `lwa import <zip> --name <名称>` | 从 zip 创建实例 |
| `lwa import --from-dir <目录>` | 复制本机文件夹到工作区，不在原始目录内运行 |
| `lwa import --from-git <URL> --ref <分支、标签或完整 commit SHA> --subdir <子目录>` | 从 GitHub 导入；`--ref` 与 `--subdir` 可省略，仅支持 github.com |
| `lwa import <zip> --update <ID>` | 原地更新 zip 源，保留实例 ID、端口、别名及数据目录 |
| `lwa import --from-dir <目录> --update <ID>` | 从文件夹更新；也可把 zip/GitHub 源实例切换为文件夹源 |
| `lwa import --from-git <URL> --update <ID>` | 从 GitHub 更新；也可切换既有实例的源类型。按完整 commit SHA 钉版的实例不探测远端、始终报无变更 |
| `lwa import --from-git <URL> --update <ID> --ref <新ref> --allow-source-change` | 更换已有 git 实例的分支 / 标签 / commit 或 `--subdir`，保留原实例 |
| `lwa import --from-dir <新目录> --update <ID> --allow-source-change` | 更换已关联的文件夹路径，保留原实例 |
| `lwa import <zip> --update <ID> --dry-run` | 预览更新；全新导入不支持 `--dry-run` |
| `lwa scan <ID>` | 重新识别指定实例；省略 ID 时扫描待识别实例 |

更新默认保留 `data/`。使用 `--no-keep-data` 会清空 `data/`，使用前应备份并查看帮助。未识别或未确认的项目保持 `pending`，不会被当作静态站强行启动。支持的项目布局见[已知限制](known-limitations.md)。

## 实例运行、版本与清理

| 命令 | 用途 |
| --- | --- |
| `lwa list` / `lwa status [ID]` | 查看实例列表、状态与端口；status 额外显示已配置的别名地址 |
| `lwa start <ID>` / `lwa stop <ID>` / `lwa restart <ID>` | 启动、停止、重启 |
| `lwa recover <ID>` | 尝试恢复实例，必要时先拉起网关 |
| `lwa rebuild <ID>` | 通过构建队列强制重建当前工作区中的项目副本 |
| `lwa rebuild <ID> --sync` | 先同步关联文件夹或 GitHub 源，再重建 |
| `lwa rebuild <ID> --force` | 确认继续执行会删除前端产物目录的旧 `buildHooks`（默认拒绝，见 issue #141） |
| `lwa cancel-build <ID>` | 取消排队或进行中的构建，不删除缓存、镜像与数据 |
| `lwa versions <ID>` | 查看留档的容器镜像版本 |
| `lwa rollback <ID> [--to BUILD_ID]` | 回到上一可用容器版本，或指定留档版本 |
| `lwa remove <ID>` | 移除实例登记；`--purge` 同时清理磁盘文件，非空数据目录需要加 `--force` |
| `lwa remove --redundant [--purge]` | 清理冗余实例；默认跳过运行中、过渡态或带独立配置的实例 |

容器回滚还原镜像与生成配置，**不回滚数据库**。构建钩子或启动前命令包含迁移操作时，需要 `--yes` 确认。冗余清理的 `--allow-config-loss` 允许损失独立配置，但不会绕过运行中与过渡态保护。

## 实例配置

`lwa configure <ID>` 显示当前配置。构建参数改动后执行 `lwa rebuild <ID>`；额外挂载改动后执行 `lwa restart <ID>`。

| 选项 | 用途 |
| --- | --- |
| `--build-env KEY=VALUE` / `--clear-build-env` | 设置或清空构建环境变量；同时用于宿主前端命令和容器构建 |
| `--follow-alias-base` / `--no-follow-alias-base` | 让构建时的 `VITE_BASE` 跟随当前别名；项目需主动读取这个变量 |
| `--system-deps 包名` / `--clear-system-deps` | 设置容器系统依赖；Python 使用 apt，Node 使用 apk，均在项目依赖安装之前执行 |
| `--build-hook 命令` / `--clear-build-hooks` | 设置依赖安装后的构建钩子；安装原生编译工具优先用 `--system-deps` |
| `--extra-volume SRC:DST[:ro]` / `--clear-extra-volumes` | 设置额外 bind 挂载，源与容器目标均使用绝对路径，并经过安全审计 |
| `--acknowledge-redundancy` / `--no-acknowledge-redundancy` | 标记或撤销有意保留的重复实例 |
| `--kind <类型> --runtime <运行时>` | 确认 pending 实例形态；两项须一起使用，不能与其他配置项组合 |

`--build-env`、`--system-deps`、`--build-hook`、`--extra-volume` 都是**整组替换**，可在一次命令中重复选项来传入完整列表。确认形态时，类型取 `node`、`python` 或 `static`，运行时取 `docker-compose` 或 `shared-static`；确认后仍需核对实际启动入口。

工作区级端口池、镜像源、并发数等在 `local-web.yml` 配置。应用的 `buildHooks`、`preStart` 与持久化目录说明见[工作区文档](runtime-workspace.md)和[运维手册](operations-playbook.md)。

## 访问、网关与探针

| 命令 | 用途 |
| --- | --- |
| `lwa alias set <ID> <别名>` / `lwa alias clear <ID>` | 设置或清除路径别名；设置需要 Caddy，并经过路径兼容性检查 |
| `lwa access refresh` | 换网或 IP 变化后刷新访问地址 |
| `lwa access review [--json] [--rebuild-if-needed]` | 复核实际访问入口、资源和 API 路径 |
| `lwa access record-observation <ID> --vantage <lan-client/windows-host/guest> --protocol <http/https> --evidence <证据> [--status N] [--url URL]` | 记录一次来自独立设备的外部观测（LWA 不自行跨设备探测）；成功观测仅在协议/端口/路径与当前入口一致且网络指纹未变时升格为已验证 |
| `lwa gateway on` / `lwa gateway off` / `lwa gateway status` | 管理工作区的 Caddy 网关 |
| `lwa gateway switch <caddy或builtin> [--dry-run]` | 预检或切换静态网关后端，失败回滚 |
| `lwa probe show <ID>` | 查看继承、覆盖及最终生效的探针 |
| `lwa probe set <ID> <路径> [--expected-status N]` | 增加必选就绪探针 |
| `lwa probe reset <ID>` / `lwa probe auto <ID> <on或off>` | 清空探针覆盖，或切换自动契约探针 |
| `lwa ca export [--out PATH]` | 导出内部 CA 根证书、指纹及客户端信任指引 |

默认 HTTP 别名入口为 `:8080`。内部 CA 的 HTTPS 配置、端口与客户端信任步骤见 [HTTPS 指南](https.md)。Caddy admin 端口只用于本机管理，不应开放到局域网。

## 管理页、自动导入与自启

| 命令 | 用途 |
| --- | --- |
| `lwa manager on` / `off` / `status` / `logs` | 启停、查看管理页服务与日志；`start` 为前台运行 |
| `lwa manager token [--json]` | 查看当前 token 及轮换时间 |
| `lwa daemon on` / `off` / `status` | 控制 inbox 监听与实例自愈 |
| `lwa services restart [--no-reconcile]` | 协调重启后台服务加载当前代码，保留自启配置，不拉取源码或重装包 |
| `lwa autostart install [--with-caddy] [--no-enable] [--linger]` | 安装 systemd / launchd 单元 |
| `lwa autostart enable` / `disable` / `status` | 管理自启；enable/disable 可加 `--service manager`、`daemon` 或 `gateway` 限定单个服务 |
| `lwa autostart check [--json]` / `lwa autostart repair [--with-caddy]` | 检查或修复自启配置 |
| `lwa autostart uninstall [--purge-linger]` / `lwa autostart doctor-hints` | 卸载自启或查看诊断提示 |

默认管理页为 `http://127.0.0.1:17800/`，LAN 访问需要 token；默认每 168 小时轮换。daemon 监听工作区 `inbox/`，导入 zip 并启动符合自动启动条件的轻量实例。详见[管理页](manager-page.md)、[工作区](runtime-workspace.md)和[自启指南](autostart.md)。

## 诊断与统计

| 命令 | 用途 |
| --- | --- |
| `lwa logs <ID> [-c 分类] [-n 行数] [--timeline]` | 查看 build/run/gateway/import/scan 日志或操作时间线 |
| `lwa doctor [ID] [--json] [--profile full] [--access]` | 检查环境、实例、服务与访问；未初始化时可用 `--json` 获取平台诊断 |
| `lwa capabilities [--json]` | 查看工作区运行能力 |
| `lwa stats [ID]` / `lwa pageviews [ID] [-n 条数]` | 查看资源占用或浏览量 |
| `lwa migrate-user <ID> [--root]` | 显式迁移旧容器的运行身份，先检查数据目录写权限 |
| `lwa registry check [--json]` / `lwa registry repair [-y]` | 只读检查 registry 孤儿行，或确认后修复 |

症状、日志位置与处理步骤见 [FAQ](faq.md)。

## 本机 Agent 与 MCP

| 命令 | 用途 |
| --- | --- |
| `lwa agent connection-info --workspace <绝对路径> [--json]` | 输出接入地址、工作区标识及配置自检，不输出 token |
| `lwa mcp --workspace <绝对路径>` | 启动 stdio MCP 适配器；需要安装 `[mcp]` 可选依赖 |

在仓库根目录执行 `python -m pip install -e '.[mcp]'` 安装 MCP 依赖。Agent 通道只允许本机回环且要求有效 token，部署流程、鉴权和源目录授权见 [Agent 指南](agent-guide.md)。
