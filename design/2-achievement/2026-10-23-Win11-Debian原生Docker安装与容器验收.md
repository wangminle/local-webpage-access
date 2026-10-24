# Win11 WSL Debian 原生 Docker 安装与容器验收

日期：2026-10-23。关联 OPS-221、CHK-490、PLN-054，续接 CHK-489 的本机 Docker 缺口。

**结果：原生 Docker 已安装且普通用户可用，本 Debian 的代表有状态容器、LWA 构建/启动、HTTPS 别名与重启数据保留验收通过。原先“必须开启 Docker Desktop 集成才能继续”的判断收回；独立 LAN 入站和真实宿主恢复仍是另外的待验项。**

## 安装与运行环境

- Windows 11 Insider Preview 10.0.28020.3262 / WSL2 架构、包3.0.4.0 / Debian13.7，运行态 mirrored。
- 安装前没有 Debian 内 Docker Engine、containerd、socket、容器数据或相关服务；仅 PATH 中存在不可用的 Docker Desktop 包装入口。systemd 已为 PID1。
- 普通用户 sudo 需要密码；通过现有 `wsl -d Debian -u root` 能正常执行安装，因此不需要用户提供密码或修改 sudoers。
- 复用项目 `install-docker-linux.sh --official`，按 Docker 官方 Debian apt 仓库安装。先补系统缺少的 curl，未卸载已有包、未执行整机升级；完整包清单前后快照保留。
- 安装完成：Docker Engine/CLI **29.9.2**、Compose **5.6.1**、Buildx **0.38.1**、containerd **2.4.2**。docker、containerd服务均active/enabled，仅配置随Debian systemd启动，没有新增Windows启动任务，未执行WSL/Windows重启。
- 原普通用户加入docker组，新启动的WSL会话已拥有该组；`/usr/bin/docker`、默认context、Linux Unix socket及普通用户的`docker version`/`docker compose version`均验证成功。不依赖Docker Desktop集成，未改动Docker Desktop设置。

官方Docker Hub直连拉取曾超时；终端已有的Windows本机HTTP代理可达。按官方daemon代理配置方式，将同一现有代理写入本Debian新建的`/etc/docker/daemon.json`，通过`dockerd --validate`后重启新Engine；未新建代理服务、未配置公共镜像加速器、未修改Windows网络。随后官方 `python:3.13-slim` 拉取成功，摘要为 `sha256:57b29a7c050c4e679e4bea572e14b3965cf4b5839de2da49d55e49ffc4024f39`。后续Docker Hub出站仍依赖该现有代理可用。

操作参考：[Docker Debian安装](https://docs.docker.com/engine/install/debian/)、[Linux用户组与自启](https://docs.docker.com/engine/install/linux-postinstall/)、[daemon代理配置](https://docs.docker.com/engine/daemon/proxy/)。

## 真实容器验收

沿用CHK-489冻结的产品源码，基线为HEAD `02696f9`加当时待提交改动；产品源码未在本轮修改。新建Linux文件系统隔离工作区和专用端口，用标准库HTTP/SQLite机制样本直接登记manifest/registry，再由LWA真实生成Dockerfile/Compose并执行构建、启动、别名配置、重启。

| 验证项 | 结果 |
| --- | --- |
| 本Debian真实Engine与Compose | 普通用户客户端和daemon均29.9.2，Compose5.6.1；实际容器运行于本Debian原生Engine |
| LWA生成与构建 | Python模板、Compose、非root用户和SQLite数据目录挂载实际生成；build/up成功，容器running |
| HTTPS别名 | 显式加载隔离工作区CA并校验身份，返回200 JSON，产品别名活验证为passed(api-only) |
| SQLite写入 | 经HTTPS POST写入唯一标记，响应200并可读取；数据库文件在宿主管理的data目录真实落盘 |
| 产品restart后保留 | `lwa restart`成功，HTTPS读取仍含同一条记录，CA哈希不变 |
| Windows localhost | HTTPS验证200，读取到相同标记和持久化记录；不是SSH隧道 |
| doctor | 命令exit0、overall=warn；Docker/Compose均OK，WSL入口仍candidate-unverified/WARN，不因容器通过而声称独立LAN成功 |

首轮容器已经build/up并running，但新工作区的网关意图仍为disabled，别名活验证返回HTTP None，`start`退出1。通过产品`gateway on`显式启用后，重试start、alias set、gateway on及restart均成功；首轮日志保留，不将整个过程写成首次全部成功。安装过程中apt提示缺少python3-apt，项目脚本按既有逻辑跳过对应钩子；部分附属包的用户bus提示亦保留，不影响最终Engine/Compose active与真实容器验证。

这是自建最小有状态机制样本，**不代表导入识别、全部真实应用、更新/回退、其他Windows/WSL组合、独立LAN或宿主恢复矩阵全部通过**。未跑完整门禁；本轮没有产品代码修改。

## 清理与后续

临时容器与Compose专属网络、LWA网关及manager按归属清理；保留已安装的Engine/Compose、基础镜像/缓存、隔离数据和CA供后续使用，不卸载刚安装的环境。Docker服务继续运行，符合本次安装目标。

M11-3“本Debian没有Docker而无法测试容器”的环境阻点已解除，改用本原生Engine继续后续应用验收，不再要求开启Docker Desktop集成。独立LAN入站、BUG-989防火墙生效策略诊断、Win10修复后同路径复验、其他组合与维护窗口恢复仍待，PLN-054保持进行中，第二步可继续。

原始安装日志、包清单、代理配置、镜像摘要、Docker上下文、容器inspect、产品命令日志、持久化JSON及Windows读取结果仅存放在已忽略的 `design/5-checkpoints/2026-10-23-m11-docker-native/`。未提交Git或修改远端issue。
