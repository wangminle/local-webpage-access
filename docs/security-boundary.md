# 安全边界与默认保护（WBS-30.10）

`lwa` 在小主机上以"导入即用"为目标，但同时对潜在风险做了多层防御。
本文说明 V1 的安全边界、默认保护策略，以及哪些操作会**被拒绝**、哪些只会**告警**。

> 安全审计的集中实现见 `src/local_webpage_access/security.py`（WBS-25）。

## 审计分级

每个安全发现（`SecurityFinding`）分三个级别：

| 级别 | 含义 | 处置 |
| --- | --- | --- |
| `critical` | 严重风险，可导致逃逸/提权/数据泄露 | **拒绝执行**（`assert_no_critical` 抛 `SecurityError`） |
| `warn` | 中等风险，需人工确认 | 记录日志与事件，允许执行 |
| `info` | 提示性信息 | 仅记录 |

## Compose 审计（`audit_compose`）

`generate_compose` 在写出 compose.yaml **之前**做自检；任何 critical 都会阻止文件写入。

| 检查项 | 级别 | code |
| --- | --- | --- |
| `privileged: true` | critical | `privileged` |
| 挂载 Docker socket（`/var/run/docker.sock` 等） | critical | `docker_socket` |
| bind mount 宿主敏感目录（`/`、`/etc`、`/var`、`/root`、`/proc`、`/var/lib/docker` 等） | critical | `host_sensitive_mount` |
| 非 `data/` 的 host bind mount | warn | `unexpected_host_mount` |
| `network_mode: host` | warn | `host_network` |
| 危险 `cap_add`（`SYS_ADMIN` / `NET_ADMIN` / `SYS_PTRACE` 等） | warn | `dangerous_cap` |
| 以 root 用户运行（`user: root` 或 `user: 0`） | warn | `run_as_root` |
| Compose YAML 非法 / 结构错误 | critical | `invalid_yaml` |

**允许的 host bind mount**：仅 `./data`、`../data`（实例自己的持久化目录）。
这是 V1 唯一允许的 bind mount 源，保证容器只能写自己的 `data/`。

## Dockerfile 审计（`audit_dockerfile`）

| 检查项 | 级别 | code |
| --- | --- | --- |
| 显式 `USER root` 或 `USER 0` | warn | `dockerfile_user_root` |
| `ADD <url>`（远程下载，不可复现且有供应链风险） | warn | `dockerfile_add_url` |
| `RUN` 中含 `curl ... \| sh` / `wget ... \| sh` 模式 | warn | `dockerfile_curl_pipe_sh` |

> V1 生成的 Dockerfile 默认非 root（`node:24-alpine` 用 `node` 用户，
> `python:3.13-slim` 创建 `app` 用户并切换）。审计主要针对用户/skill 覆盖的模板。

## zip 成员审计（`audit_zip_members`）

作为 `_safe_extract` 的**纵深防御**第二层，`audit_zip_members` 在成员名层面
独立检查路径穿越：

| 检查项 | 级别 | code |
| --- | --- | --- |
| 绝对路径（`/etc/...`） | critical | `zip_absolute_path` |
| Windows 盘符（`C:\...`） | critical | `zip_drive_path` |
| `..` 穿越序列 | critical | `zip_traversal` |
| 反斜杠路径 | warn | `zip_backslash` |

导入器在 `_safe_extract` 中对每个成员做 `resolve().relative_to(target)` 校验，
**即便审计层漏过，解压层也会拒绝**。

## 未识别 zip 风险提示

导入后若实例为 `pending`（未能识别运行形态），导入器会写入一条 `security` 事件：

> ⚠️ 未知 zip 来源：未能识别项目类型，请人工确认内容安全后再启动。

提示用户在 `lwa start` 前检查 `apps/<id>/current/` 的实际内容。

## 管理页绑定校验（`validate_manager_binding`）

`lwa manager on` / `lwa manager start` 时校验绑定地址与 token：

| 绑定地址 | 无 token | 有 token |
| --- | --- | --- |
| `127.0.0.1` / `localhost` | 允许 | 允许 |
| `0.0.0.0` / LAN IP / 通配 | **拒绝**（critical） | 允许 |

> 默认 `managerHost: 0.0.0.0`（局域网访问），因此**首次启动会自动生成 token**。
> 若手动删除 token 文件又绑定到 LAN，启动会被拒绝，强制用户先解决鉴权。

* token 文件：`run/manager-token.json`，权限 `0600`。
* 应用日志（`logs/lwa.log`、`logs/manager.log`）同样收紧为 `0600`，且**不落盘完整 token**
  （仅 CLI 终端打印）。查看管理页运行日志用 `lwa manager logs`。
* 轮换：删除 `run/manager-token.json` 后 `lwa manager off && lwa manager on`（旧 token 立即失效）。

## 默认资源限制

V1 生成的容器统一带资源限额（`local-web.yml` 的 `defaultResourceLimits`）：

```yaml
defaultResourceLimits:
  memory: 512m
  cpus: "0.75"
```

* 防止单实例吃满小主机内存/CPU。
* 每实例 `restart: unless-stopped`，崩溃自动重启。
* 构建并发限流（`buildConcurrency: 1`），避免并发构建 OOM。

## 非 root 容器

所有生成的 Dockerfile 都切换到非 root 用户：

* Node 基线：`node:24-alpine`，内置 `node` 用户（UID 1000）。
* Python 基线：`python:3.13-slim`，创建 `app` 用户并 `USER app`。

容器内进程不具备 root 权限，即便应用有漏洞也降低了逃逸面。

## V1 不做的安全承诺

详见 [已知限制](known-limitations.md)。重点：

* **不做多租户隔离**：所有实例共享同一 Docker daemon 与主机内核。
* **不做网络隔离**：默认 bridge 网络，实例间可通信（V1 未启用自定义网络隔离）。
* **不做镜像签名校验**：基线镜像来自 Docker Hub，未做签名验证。
* **token 为静态随机串**：无过期、无细粒度权限（V2 规划）；可通过删除 token 文件并重启管理页轮换。
