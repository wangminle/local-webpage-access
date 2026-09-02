# BUG-622 Fedora docker-cli 遗漏修复实施计划

> **执行要求：** 按 TDD 顺序逐项实施并在完成前运行完整验证。

**目标：** Fedora 43/44 上即使 `docker-cli` 是用户显式安装的，Docker CE 安装脚本也会先移除该冲突包，避免 `/usr/bin/docker` 与 `docker-ce-cli` 冲突。

**方案：** 保持 Docker 官方十项旧包清单不变，在已有的 Fedora 额外兼容包数组中加入有官方 Fedora 包元数据支撑的 `docker-cli`。不恢复对 `containerd` / `runc` 的扩大卸载，也不使用 `--allowerasing`。测试直接约束额外清单包含 `docker-cli`，并保留卸载失败必须向上传播的既有语义。

**技术栈：** Bash、pytest 静态命令结构测试。

---

### 任务 1：建立失败回归

**文件：**

- 修改：`tests/test_install_docker_scripts.py`

1. 在 BUG-622 回归中断言 `docker-cli` 出现在 Fedora 冲突包函数内。
2. 单独运行该测试，确认旧实现因缺少 `docker-cli` 而失败。

### 任务 2：最小修复

**文件：**

- 修改：`src/local_webpage_access/scripts/install-docker-linux.sh`

1. 在 `extra` 数组加入 `docker-cli`。
2. 补充注释，说明 Fedora 43/44 的独立子包拥有 `/usr/bin/docker`，显式安装时不会随 `moby-engine` 自动移除。
3. 重跑红测，确认转绿。

### 任务 3：验证与台账

1. 运行 `tests/test_install_docker_scripts.py` 及 BUG-622/623/624 三个相关测试文件。
2. 运行 Ruff、两份 Linux 脚本 `bash -n`、`git diff --check`。
3. 将 `BUG-622` 标记为已修复，并新增本次实施检查记录；运行 task-list 校验。
