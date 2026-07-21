---
name: lwa-fix-docker-build-failure
description: >-
  Diagnose an lwa Docker build failure and repair the Dockerfile or dependency declarations before retrying with lwa rebuild. Use when build.log contains package, image, COPY, network, architecture, or native-extension errors, or an instance remains failed after a rebuild.
---

# lwa-fix-docker-build-failure

> 诊断 `docker build` 失败原因，修正 `Dockerfile` / 依赖声明，交 `lwa rebuild` 重试。

## 何时触发

- 实例状态为 `failed`，`logs/<id>/build.log` 含构建错误。
- 管理页"重建"操作后构建仍失败。
- 构建长时间卡在 `queued` / `building`，需先停掉再改 Dockerfile。

## 输入

1. `logs/<id>/build.log`（完整构建输出）。
2. `apps/<id>/current/Dockerfile`。
3. 依赖清单：`package.json` / `requirements.txt` / `pyproject.toml`。
4. `apps/<id>/local-web.json`。

## 输出

- 修正后的 `Dockerfile`（和/或 `.dockerignore`、依赖清单）。
- 诊断说明（写入事件日志）：根因 + 修复措施。

## 可修改文件

- `apps/<id>/current/Dockerfile`。
- `apps/<id>/current/.dockerignore`。
- `apps/<id>/current/package.json` / `requirements.txt`（仅当构建错误源于依赖声明时）。
- `apps/<id>/local-web.json`。

## 禁止事项

- 不为"让构建通过"而删除业务代码或测试。
- 不放宽安全约束（如改回 root、加 privileged）来规避权限错误。
- 不引入 `--no-cache` 之外的危险构建参数。
- 同一错误重试超过 2 轮应转人工，不死循环。

## 处理流程

0. 若仍在 `queued` / `building`：先 `lwa cancel-build <id>`（或管理页「取消构建」）停掉当前工作，**不删**缓存/镜像/用户数据；`cancelling` 结束后再改文件。
1. 解析 `build.log`，按常见模式归类：
   - 依赖安装失败（网络/版本冲突/平台不匹配）。
   - `COPY`/`ADD` 路径不存在（`.dockerignore` 误排除、源文件缺失）。
   - 编译错误（原生模块、TypeScript、C 扩展）。
   - 端口/EXPOSE 与实际不符（不影响构建，记录备用）。
2. 针对性修复：
   - 依赖：固定版本、加 `--platform`、换镜像 tag（`slim` → 带 `gcc` 的 `bullseye`）。
   - COPY：修正 `.dockerignore`、调整 Dockerfile 中路径顺序。
   - 原生模块：补 `build-essential` / 切到 `musl`/`glibc` 匹配的镜像。
3. 写回修改，记录诊断到事件日志。
4. 提示 `lwa rebuild <id>` 重试。

## 示例

`build.log` 含 `gyp ERR! find Python` → Node 原生模块缺 Python，修复：

```dockerfile
FROM node:20-slim
RUN apt-get update && apt-get install -y python3 make g++ && rm -rf /var/lib/apt/lists/*
```
