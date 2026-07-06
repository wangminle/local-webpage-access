# 常见问题与排障（WBS-30.09）

## 快速排障入口

**首次部署**请先运行：

```bash
lwa setup             # 检测宿主机工具，打印安装指引（无需工作区）
lwa setup --script    # 输出当前平台参考安装脚本（需人工审阅后执行）
```

遇到问题时，第一步永远是：

```bash
lwa doctor          # 检查 Python / Docker / Compose / 端口池 / registry / 磁盘 / 内存
lwa doctor <id>     # 对单个实例做深度诊断（日志、状态、文件）
lwa doctor --json   # 机器可读报告，便于脚本化
```

`doctor` 有 fail 时退出码为 1，可在脚本/CI 中用作门禁。

## 环境类问题

### Python 版本不满足

```
[fail] python_version: 需要 Python ≥ 3.13
```

`lwa` 依赖 Python 3.13+（pydantic v2 / typing 特性）。升级 Python 或用 `pyenv`/`uv` 管理。

### Docker 不可用

```
[fail] docker: Docker 不可用
```

* 确认 `docker` 命令在 PATH 中：`docker version`。
* Linux：确认 dockerd 已启动（`systemctl status docker`），当前用户在 `docker` 组。
* macOS / Windows：确认 Docker Desktop 已启动。
* 静态/前端实例不需要 Docker，可继续使用。

### Docker Compose 不可用

```
[fail] docker_compose: Docker Compose 不可用
```

V1 要求 Docker Compose 插件（`docker compose` 子命令）。安装 `docker-compose-plugin`，
或升级 Docker Desktop。检测到 v1 独立二进制时会提示改用插件。

### 磁盘空间不足

```
[fail] disk_space: 磁盘剩余 0.8 GB，低于阈值 1.0 GB
```

清理 `inbox/`（已导入的原始 zip）、`logs/`、或 `apps/<id>/source/`（原始快照）。
也可迁移整个工作区到更大磁盘。

## 导入类问题

### zip 导入失败：路径穿越（zip slip）

```
ZipImportError: 检测到路径穿越（zip slip）：../../etc/passwd
```

导入器对所有 zip 成员做 `_safe_extract` 检查，任何成员解析后落在解压目录之外都会被拒绝。
这是 [安全边界](security-boundary.md) 的强制保护。请用正规工具重新打包。

### 实例识别为 pending

```
status: pending
```

扫描器没能确定运行形态。常见原因：

* 项目根目录缺少 `package.json` / `requirements.txt` / `pyproject.toml` 等特征文件。
* zip 内有多层嵌套目录且特征文件不在拍平后的根。
* 项目结构特殊（自定义构建）。

处理：`lwa scan <id>` 重新识别；仍 pending 时检查 `local-web.json` 的 `lastError`，
或手工补特征文件后重扫。pending 实例会写入「未知 zip 来源」风险提示事件。

### slug 冲突

同名 zip 重复导入时，`lwa` 自动追加序号（`my-site`、`my-site-2`、`my-site-3`），
不会覆盖既有实例。如需替换，先 `lwa remove <旧实例> --purge`。

## 容器类问题

### 构建失败（OOM）

小主机并发构建易 OOM。`local-web.yml` 的 `buildConcurrency` 默认 1，
**不建议调高**。仍 OOM 时：

* 降低 `defaultResourceLimits.memory`（但需保证应用能启动）。
* 用资源 profile 更小的实例（`resourceProfile: tiny`）。
* 查看 `apps/<id>/logs/build.log` 定位具体失败步骤。

### 容器启动后立即退出

```
status: failed, lastError: 容器退出码 1
```

* `lwa logs <id> --category run` 看应用日志。
* 常见：应用监听 `127.0.0.1` 而非 `0.0.0.0`（容器内需监听 `0.0.0.0` 才能被端口映射访问）。
* 常见：`internalPort` 与应用实际监听端口不一致。检查 `local-web.json` 的 `container.internalPort`。

### 端口池耗尽

```
PortError: 端口池 [18000, 19999] 已耗尽
```

* `lwa stats` 查看端口池占用。
* 多数情况是僵尸进程持有端口（异常退出未释放）。Linux：`ss -tlnp | grep 180`；
  Windows：`netstat -ano | findstr 180`，`taskkill /PID <pid> /F`。
* 必要时扩大 `portPool` 范围（修改 `local-web.yml` 后重启管理页/daemon）。

## 管理页类问题

### 管理 token 丢失

token 存在工作区 `run/` 目录下。删除该文件后 `lwa manager start` 会重新生成。
**重置 token 会使旧 token 失效**。

### 管理页打不开 / 401

* 确认端口未被占用：`lwa doctor` 的 port_pool 检查会覆盖 managerPort。
* 确认 token 正确（复制时勿带前后空格）。
* 若绑定到 `0.0.0.0` 但无 token，启动会被 `validate_manager_binding` 拒绝。

### 管理页状态与 CLI 不一致

管理页每次 `GET /api/instances` 都会先观测回写状态，理论上始终一致。
若仍不一致，运行 `lwa status` 强制刷新，或 `lwa doctor <id>` 诊断该实例。

## 数据与清理

### `lwa remove` 后磁盘文件还在

`remove` 默认只删 registry 索引，保留 `apps/<id>/` 全部文件（含 `data/`），
便于误删恢复。彻底删除用 `--purge`；`data/` 非空时需额外 `--force` 确认。

### 如何备份

* 关键数据：每个实例的 `apps/<id>/data/`（SQLite 等）。
* 元数据：`apps/<id>/local-web.json` 与 `registry/local-web.db`。
* 冷备份：`stop` 所有实例后直接打包整个工作区目录。
