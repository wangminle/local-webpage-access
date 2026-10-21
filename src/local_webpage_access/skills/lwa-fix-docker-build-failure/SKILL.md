---
name: lwa-fix-docker-build-failure
description: >-
  Diagnose an lwa Docker build failure and repair the Dockerfile or dependency declarations before retrying with lwa rebuild. Use when build.log contains package, image, COPY, network, architecture, or native-extension errors, or an instance remains failed after a rebuild.
---

# lwa-fix-docker-build-failure

> 诊断 `docker build` 失败原因，改 manifest 或项目依赖声明，再交 `lwa rebuild` 重试。
>
> **生成物不是真相。** `docker/Dockerfile`、`.dockerignore` 与 `docker/compose.yaml` 会在 `lwa start` / `lwa rebuild` 时按 manifest **重新渲染**。手改这些文件会在下一次生成时消失。持久修改写在 `entry.install` / `entry.build` / `entry.start`、`buildHooks`、`preStart`、`systemDeps`、`buildEnv`、`container.internalPort`、`container.extraVolumes`。`lwa configure` 可写 `buildEnv`、`systemDeps`、`buildHooks`、`extraVolumes`。其中后四类以及 `preStart` 在 scan / import --update 时保留；`entry.*` 会按源码重新推断。

## 何时触发

- 实例状态为 `failed`，`apps/<id>/logs/build.log` 含构建错误。
- 管理页"重建"操作后构建仍失败。
- 构建长时间卡在 `queued` / `building`，需先停掉再改 Dockerfile。

## 输入

1. `apps/<id>/logs/build.log`（完整构建输出）。
2. `apps/<id>/docker/Dockerfile`。
3. 依赖清单：`package.json` / `requirements.txt` / `pyproject.toml`。
4. `apps/<id>/local-web.json`。

## 输出

- 修正后的 `local-web.json`（`systemDeps` / `buildHooks` / `entry.*`）和必要时的源码依赖清单。
- 诊断说明（写入事件日志）：根因 + 修复措施。

## 可修改文件

- `apps/<id>/local-web.json`。
- `apps/<id>/current/` 下的 `package.json` / `requirements.txt` / `pyproject.toml`（仅当构建错误源于依赖声明时）。
- 不要把 `apps/<id>/docker/Dockerfile`、`.dockerignore`、`docker/compose.yaml` 当作可持久化的修改目标。

## 禁止事项

- 不为"让构建通过"而删除业务代码或测试。
- 不放宽安全约束（如改回 root、加 privileged、`curl|sh`、`ADD https://...`）来规避权限或依赖错误；`generate_dockerfile` 会对后两者 critical 拒绝写出。
- 不引入 `--no-cache` 之外的危险构建参数。
- 同一错误重试超过 2 轮应转人工，不死循环。

## 处理流程

0. 若仍在 `queued` / `building`：先 `lwa cancel-build <id>`（或管理页「取消构建」）停掉当前工作，**不删**缓存/镜像/用户数据；`cancelling` 结束后再改文件。
1. 解析 `build.log`，按常见模式归类（日志按 `======== LWA-BUILD <时间> instance=… attempt=… ========` 分段追加，只看最后一段）：
   - 依赖安装失败（网络/版本冲突/平台不匹配）。失败摘要会附下载源列表与代理路径诊断；
     源失败冷却记录在工作区 `run/source-health.json`，先判断是源不可达还是依赖本身有问题。
   - `COPY`/`ADD` 路径不存在（`.dockerignore` 误排除、源文件缺失）。
   - 编译错误（原生模块、TypeScript、C 扩展）。
   - 端口/EXPOSE 与实际不符（不影响构建，记录备用）。
   - 摘要提示「缺少 bun 工具链」「静态托管未找到 HTML」时，是形态/工具链未选容器路径，
     **不要归因于 Dockerfile 语法**；先按 [lwa-detect-stack](../lwa-detect-stack/SKILL.md) 确认 `kind` / `runtime`。
2. 针对性修复（写入 manifest 或源码依赖，不写生成的 Dockerfile）：
   - 依赖版本冲突：改 `current/` 里的锁文件或依赖声明，必要时收紧 `entry.install`。
   - 缺少系统库 / 编译器：写入 `systemDeps`（如 `["python3", "make", "g++"]`），或 `lwa configure <id> --system-deps python3 --system-deps make --system-deps g++`。
   - 需要额外构建命令：写入 `buildHooks`，不要在 Dockerfile 里追加 `RUN`。注意 `buildHooks` 在依赖安装与构建**之后**执行，救不了 npm install 期的失败（缺编译工具一律走 `systemDeps`）。
   - `COPY` 找不到文件：核对源码是否真的在 `current/`，不要手改生成的 COPY 行。
3. 写回修改，记录诊断到事件日志。
4. 提示 `lwa rebuild <id>` 重试。重建会重新渲染 Dockerfile。

## 示例

`build.log` 含 `gyp ERR! find Python` → Node 原生模块缺编译工具。写入 manifest：

```json
{ "systemDeps": ["python3", "make", "g++"] }
```

然后 `lwa rebuild <id>`。不要往 Dockerfile 里贴 `apk add` / `apt-get install`——V0.9.7 起 Node（alpine 基镜像）同样消费 `systemDeps`，以 `apk add --no-cache` 渲染在 npm 依赖安装层之前；Python（Debian）走 apt 切源链。
