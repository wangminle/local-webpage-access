# lwa-setup-host-environment

> 在新机器或全新环境中，引导用户安装 `lwa` 所需的宿主机工具，并完成工作区初始化。

## 何时触发

- 用户第一次在目标主机上使用 `lwa`。
- `lwa doctor` 或 `lwa setup` 报告 Docker / Compose / Python / Caddy / Node 缺失或版本过低。
- 用户询问「怎么装环境」「需要什么依赖」「初始化流程是什么」。

## 输入

1. `lwa setup` 或 `lwa setup --json` 的输出（当前平台、各组件状态、安装指引）。
2. （可选）`lwa setup --script` 生成的参考安装脚本。
3. 用户操作系统：macOS / Linux / Windows。
4. 预期用途：仅静态托管 / 前端构建 / 容器托管（决定哪些组件为必需）。

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
| Caddy | ≥ 2.10.0 | Caddy 模式；缺失时 `staticGateway=caddy` 会降级 builtin |
| Node.js | ≥ 24（推荐） | 前端 SPA 构建 |

## 处理流程

```text
1. 让用户运行：lwa setup（或 lwa setup --json）
2. 按 fail 项逐条给出平台相关安装命令（可参考 lwa setup --script）
3. 安装 lwa：pip install -e .
4. 复核：lwa setup → 全部必需项 ok
5. 初始化工作区：lwa init
6. 完整诊断：lwa doctor（含端口池、registry、磁盘）
7. 导入样例：lwa import inbox/xxx.zip → lwa start
8. **升级 lwa 源码后**：优先运行 `lwa update`；需要 AI 协助时见 [lwa-update-runtime](../lwa-update-runtime/SKILL.md)
```

若用户**只做静态 HTML**、不用容器：

- Docker / Compose 可暂不装；
- 将 `local-web.yml` 的 `staticGateway` 设为 `builtin` 可跳过 Caddy；
- Node 仅在前端 SPA 时需要。

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

需要脚本参考时：`lwa setup --script > setup-host.sh`，审阅后执行。
