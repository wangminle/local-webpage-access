# LWA 工作区迁移运维手册（DOC-081）

把 **LWA Runtime 工作区**从一个绝对路径迁到另一个（例如  
`~/local-webpage-access-20260717` → `~/local-webpage-access`）时使用本手册。  
「文件夹改名」只是同盘原子 rename 的最简场景；完整名称是 **LWA 工作区迁移**。

> **优先 CLI（IMP-042 / DEV-089 已落地）**：同卷迁移请先  
> `lwa workspace relocate <NEW> --dry-run` → 确认后 `lwa workspace relocate <NEW> [--yes]`；  
> Skill [`lwa-relocate-workspace`](../src/local_webpage_access/skills/lwa-relocate-workspace/SKILL.md) **只调用 CLI**，禁止直接 `sed`/`mv`。  
> **本手册**仍是：CLI 失败时的逃生舱、跨盘/跨机人工路径，以及理解「为何不能整树 sed」的契约说明。  
> **配套**：目录结构见 [runtime-workspace.md](runtime-workspace.md)；自启见 [autostart.md](autostart.md)；日常运维见 [operations-playbook.md](operations-playbook.md)；排障见 [faq.md](faq.md)。

---

## 1. 结论先说清

- 改名在「服务恢复、容器 bind mount、业务 `data/`、HTTP 访问」上**可以做成功**。
- 但依赖点分散：systemd/launchd、pip editable、Docker 容器身份、manifest、registry、生成式 Caddy、能力缓存。
- **不要**把某次现场的 `sed` 流水线当成通用标准；本手册纠正高风险步骤，并给出可复查清单。
- 已修代码前提（升级到含下列修复的版本后再迁更稳）：
  - **BUG-382**：外部 `compose down` 后，`lwa start` 可在 compose/env/镜像仍在时 `up -d` 自愈（不必整镜像 `rebuild`）。
  - **BUG-383**：pageviews 游标不再绑定日志绝对路径；升级时 v3→v4 保留 offset。
  - **BUG-384**：`lwa autostart repair` **默认保留**已安装的 gateway；`--with-caddy` 只表示「没有时新增」。
  - **V0.6.12（BUG-420/421/422/423/424 + DEV-094）**：`gateway on` 启动前按当前工作区原子落盘主 Caddyfile；`start` 检测 SQLite data mount 漂移并 fail-safe 救援（`down` 失败或两侧数据冲突立即中止）；成功 host/start 回写可确定派生路径；`lwa doctor` 的 `workspace_path_consistency` 主动暴露裸 `mv` 残留（含「旧路径仍存在但不属于当前工作区」）。
  - **V0.6.13（BUG-428 挂载查询 fail-safe / BUG-429·430 registry SKIP / BUG-431～433 pageviews 轮转）**：容器 `ps` 失败不得当作无容器继续 start；doctor 在 registry 不可读时 SKIP 而非假绿；浏览量多轮转/旧游标/归档暂不可读时的补读已加固。

---

## 2. 何时用本手册

| 场景 | 是否适用 |
| --- | --- |
| 仅改工作区**目录名**或挪到同机**同一卷**内另一路径（macOS / Linux） | ✅ |
| WSL：从 `/mnt/<drive>/…` 迁到 `~/…`（Linux 盘） | ⚠️ 多为跨设备，CLI v1 将拒绝自动；人工可按本手册（先确认磁盘与备份） |
| 同机跨外置盘 / 不同卷 | ❌ CLI v1 不自动；见后续 IMP-042.b |
| 换机器、拷贝整个工作区到新主机 | ⚠️ 可参考，但还须处理 Docker 镜像、LAN IP、token、自启用户 |
| 只改实例名 / slug | ❌ 用 `lwa import --update` 等实例级流程 |
| 只升级 lwa 代码版本 | ❌ 用 `lwa update` |

---

## 3. 硬编码路径清单（改名前必摸清）

设：

```bash
OLD=/绝对路径/旧工作区根
NEW=/绝对路径/新工作区根
```

### 3.1 不改就会阻断启动

| # | 位置 | 典型内容 | 正确处理 |
| --- | --- | --- | --- |
| 1 | systemd user / launchd 单元 | `WorkingDirectory`、`ExecStart`/`ProgramArguments` 的 `--workspace OLD` | 改名后用 **`lwa autostart repair`**（保留已装服务含 gateway）重写；不要长期依赖手改 unit |
| 2 | pip editable（若 CLI 装在工作区内） | `site-packages` 下 `__editable__*.pth` / finder 指向 `OLD/.../src` | 在**新路径**重跑 `pip install -e …`；**生产建议 CLI 与工作区解耦**（见 §11） |
| 3 | Docker 容器 Mounts | `docker inspect` 的 `Mounts.Source` 为 OLD 下 `apps/<id>/data` | 改名前对该实例 `compose down`（或等价移除容器）；改名后用 **`lwa start`**（BUG-382：优先 `up -d`）重建容器 |
| 4 | `apps/*/local-web.json` | `composePath` / `dockerfilePath` / `sourceZipPath` / `appPath` 等绝对路径 | **按字段结构化改写并原子写回**；禁止整文件盲目 `sed` |
| 5 | `registry/local-web.db` | `instances.app_path`、`source_zip_path`；`containers.compose_path`、`dockerfile_path`；`static_sites.gateway_config_path` 等 | **与 manifest 同步更新**；只改 JSON 不够 |

### 3.2 应重生 / 可清缓存（不要手改当长期方案）

| # | 位置 | 说明 |
| --- | --- | --- |
| 6 | `static-gateway/Caddyfile`、`sites/*.conf`、`aliases/*.conf` | **生成式配置**。应在新 Workspace 下由网关逻辑按 manifest **重新生成并 reload**，不要 `sed` 当标准手段 |
| 7 | `run/capability-{manager,daemon,gateway}.json` | 能力缓存；可删，重启/探测后重写。Full 环境清空后可能短暂 `overall=unready`，需按 [faq](faq.md) / 运维手册刷新 |
| 8 | 进程 cmdline、历史 `logs/*.log` 旧行 | 历史记录；不影响新进程。新日志应出现 `NEW` |

### 3.3 通常不含工作区绝对路径

- `local-web.yml`（全局配置）——改名前仍建议 `grep` 确认。
- `apps/*/docker/compose.yaml` 的 volumes 多为相对路径（如 `../data`）——文件本身往往不用改；**容器身份**仍须重建。

### 3.4 摸底命令（示例）

```bash
cd "$OLD"
# 自启单元
systemctl --user cat lwa-daemon.service lwa-manager.service lwa-gateway.service 2>/dev/null | grep -E 'WorkingDirectory|workspace'
# 或 macOS：
# plutil -p ~/Library/LaunchAgents/com.fenix.lwa.*.plist | grep -i workspace

# editable
python3 -c "import local_webpage_access,inspect; import local_webpage_access as m; print(m.__file__)"

# 运行中容器与挂载
docker ps --format '{{.Names}}' | grep '^lwa-' || true
# 对每个容器实例：docker inspect <cid> --format '{{json .Mounts}}'

# 工作区内绝对路径残留（排除巨大日志可另加 --exclude）
grep -R --line-number -F "$OLD" \
  --include='*.json' --include='*.yml' --include='Caddyfile' \
  --include='*.conf' --include='*.service' \
  . 2>/dev/null | head -80

# registry 路径列
sqlite3 registry/local-web.db \
  "SELECT id, app_path, source_zip_path FROM instances;"
sqlite3 registry/local-web.db \
  "SELECT instance_id, compose_path, dockerfile_path FROM containers;" 2>/dev/null || true
```

---

## 4. 禁止与纠正（现场高风险步骤）

| 做法 | 为何危险 | 正确做法 |
| --- | --- | --- |
| `sed -i` 直接改 `local-web.json` | JSON 结构易坏、漏字段、无原子写 | 用 Python/`jq` 按字段替换后原子写回（见 §7.3） |
| 只改 manifest，不改 registry | `lwa list`/管理页仍可能指向 OLD | manifest 与 registry **一起**迁 |
| `sed` 改生成式 Caddyfile / sites / aliases | 与磁盘片段、别名、日志路径易不一致 | 停网关 → 改名 → 用新路径 **重生配置** 再 `gateway on` / reload |
| 删除 `run/daemon-processed.json` | 这是 inbox 投递**去重状态**，不是路径缓存 | **保留**；改名不依赖删它 |
| 无快照就对所有实例 `lwa stop` | `desiredState` 会变成 `stopped`，迁完不知道该启谁 | 先导出「原为 running 的实例列表」，迁后再**只恢复这些** |
| 改名后仍对旧容器 `docker restart` | Mounts.Source 仍是 OLD | 必须先移除容器，再在 NEW 下重建 |
| 生产把 editable 源码放在工作区里 | 每次改名都要重装 `.pth` | CLI 装独立 venv；工作区只放配置/实例/数据（§11） |
| 未升级时用裸 `lwa start` 指望容器已 down 能起来 | 旧代码会 `compose start` → `no container to start` | 升级含 BUG-382 后用 `lwa start`；未升级则临时 `lwa rebuild`（数据一般安全，但是重操作） |

---

## 5. 迁移前快照（必做）

在 **`$OLD`** 且服务仍可查询时执行。

### 5.1 运行意图（desiredState）

```bash
cd "$OLD"
lwa list
# 记下 STATUS=running 且业务上应恢复的实例 ID，例如：
# RUNNING_IDS=(ai-review-prd-v0-2-7 distributed-wake-demo-portable)
```

也可用 registry：

```bash
sqlite3 registry/local-web.db \
  "SELECT id, status, desired_state FROM instances ORDER BY id;"
```

把「迁移前 `desired_state=running` 或实际 running」的 ID 写入安全位置（不要只放在即将搬走的目录内唯一副本）。

### 5.2 浏览量基线（BUG-383 对账）

升级到含 BUG-383 的版本后，改名一般**不会**因游标路径变化而双计；但仍建议对账：

```bash
# 管理页或 API（需 token / 本机）：GET /api/pageviews
# 或 sqlite：
sqlite3 run/pageviews.db \
  "SELECT instance_id, SUM(hits) FROM pageviews GROUP BY instance_id;"
```

记下各实例 hits；迁完后再比一次。若未升级到 v4 游标修复就改名，**可能**重复累计——应先升级、打开一次 pageviews 触发迁移，再改名。

### 5.3 数据与配置备份

```bash
# 至少备份：配置、registry、各实例 data、pageviews
tar -C "$(dirname "$OLD")" -czf "/tmp/lwa-ws-backup-$(date +%Y%m%d%H%M).tgz" \
  "$(basename "$OLD")/local-web.yml" \
  "$(basename "$OLD")/registry" \
  "$(basename "$OLD")/run/pageviews.db" \
  "$(basename "$OLD")/apps"
# 大工作区可改为 rsync 到另一磁盘；务必在停写后或接受短窗口不一致
```

### 5.4 目标路径

```bash
test ! -e "$NEW"   # 目标必须不存在，避免 mv 并入已有目录
mkdir -p "$(dirname "$NEW")"
```

---

## 6. 停服顺序（上层 → 下层）

> 若已安装自启单元：先理解 [autostart · 停服协调](autostart.md#停服与自启的协调重要)。  
> `lwa daemon/manager/gateway off` 在单元已加载时会协调 disable；失败则勿强杀后假装已停。

```bash
cd "$OLD"
unset http_proxy https_proxy HTTPS_PROXY HTTP_PROXY   # 避免本机探测走代理假阴性

# 6.1 按快照停止「需要搬的」业务实例（会写 desiredState=stopped——这是预期，靠快照恢复）
for id in "${RUNNING_IDS[@]}"; do
  lwa stop "$id"
done

# 6.2 容器实例：移除容器，释放 OLD 上的 bind mount 身份
# （compose volumes 常用相对路径，但 Docker 记录的 Mounts.Source 是绝对路径）
for id in "${RUNNING_IDS[@]}"; do
  if [[ -f "apps/$id/docker/compose.yaml" ]]; then
    ( cd "apps/$id/docker" && docker compose --env-file .env -f compose.yaml down )
  fi
done

# 6.3 停自启监管的后台（daemon / manager / 可选 gateway）
# 推荐：
lwa autostart disable
# 或：
# systemctl --user stop lwa-manager.service lwa-daemon.service lwa-gateway.service
# launchctl bootout …（macOS）

# 6.4 若 gateway 未进自启、用后台 on：确保 caddy 已停
lwa gateway off 2>/dev/null || true

# 6.5 确认无残留
ps -eo args | grep -E 'local_webpage_access|caddy.*'"$OLD" | grep -v grep || echo "无残留进程"
docker ps -a --filter "name=lwa-" --format '{{.Names}} {{.Status}}' || true
```

说明：

- **为何要先 down 容器**：主要是冻结 DB 写入、避免进程继续持有旧目录对象、并保证之后重建时 Mounts 指向 NEW。Linux 往往允许重命名仍被占用的目录，但「先停再迁」对一致性更安全。
- **不要**在这一步删除 `run/daemon-processed.json`。

---

## 7. 改名与修依赖

### 7.1 改名

```bash
mv "$OLD" "$NEW"
cd "$NEW"
```

### 7.2 自启单元（daemon + manager + gateway）

优先：

```bash
cd "$NEW"
# 用「已经能 import 的」解释器；若 editable 仍指向 OLD，先做 §7.5 再 repair
lwa autostart repair
# 若改名前从未装过 gateway、且 staticGateway=caddy、现在要一并监管：
# lwa autostart repair --with-caddy
```

`repair`（含 BUG-384）会：

- 重写已安装单元的解释器 / 工作区 / PATH；
- **保留**已安装的 gateway（即使未传 `--with-caddy`）；
- `--with-caddy` 仅在原先没有时**新增** gateway。

若 `lwa` 因 editable 失效暂时不可用：可临时用绝对模块路径修好 unit，或先修 pip 再 `repair`。

### 7.3 manifest（`apps/*/local-web.json`）——结构化迁移

**禁止**整文件 `sed`。示例（Python）：

```bash
cd "$NEW"
python3 <<'PY'
import json, os
from pathlib import Path

old, new = os.environ["OLD"], os.environ["NEW"]
# 调用前: export OLD=... NEW=...

keys = (
    "composePath", "dockerfilePath", "sourceZipPath", "appPath",
    "gatewayConfigPath",  # 若存在
)

def rewrite(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, str) and (
                v == old or v.startswith(old + "/") or v.startswith(old + os.sep)
            ):
                obj[k] = new + v[len(old):]
            else:
                rewrite(v)
    elif isinstance(obj, list):
        for x in obj:
            rewrite(x)

for path in Path("apps").glob("*/local-web.json"):
    data = json.loads(path.read_text(encoding="utf-8"))
    rewrite(data)
    # 也遍历 container / static 嵌套
    if isinstance(data.get("container"), dict):
        rewrite(data["container"])
    if isinstance(data.get("static"), dict):
        rewrite(data["static"])
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    print("updated", path)
PY
```

改完后抽查：

```bash
grep -R -F "$OLD" apps/*/local-web.json && echo "仍有旧路径" || echo "manifest 已无 OLD"
```

### 7.4 registry（`registry/local-web.db`）

与 manifest 同步（在 NEW 上、服务仍停时）：

边界安全改写（与 BUG-518/527 的 `rewrite_registry_paths` 同口径）：不能用裸
`REPLACE(col, '$OLD', ...)` + `LIKE '$OLD%'`——`LIKE` 会把路径里的 `_`/`%` 当
通配符，裸 `REPLACE` 会误伤 `$OLD-backup` 这类兄弟目录前缀。统一按
「列值 = `$OLD` 或以 `$OLD/` 开头」选行，替换时只替换带边界的 `$OLD/` 前缀：

```bash
cd "$NEW"
sqlite3 registry/local-web.db <<SQL
UPDATE instances
SET app_path = CASE WHEN app_path = '$OLD' THEN '$NEW'
                    ELSE REPLACE(app_path, '$OLD/', '$NEW/') END,
    source_zip_path = CASE WHEN source_zip_path = '$OLD' THEN '$NEW'
                           ELSE REPLACE(source_zip_path, '$OLD/', '$NEW/') END
WHERE app_path = '$OLD' OR substr(app_path, 1, LENGTH('$OLD') + 1) = '$OLD/'
   OR source_zip_path = '$OLD' OR substr(source_zip_path, 1, LENGTH('$OLD') + 1) = '$OLD/';

UPDATE containers
SET compose_path = CASE WHEN compose_path = '$OLD' THEN '$NEW'
                        ELSE REPLACE(compose_path, '$OLD/', '$NEW/') END,
    dockerfile_path = CASE WHEN dockerfile_path = '$OLD' THEN '$NEW'
                           ELSE REPLACE(dockerfile_path, '$OLD/', '$NEW/') END
WHERE compose_path = '$OLD' OR substr(compose_path, 1, LENGTH('$OLD') + 1) = '$OLD/'
   OR dockerfile_path = '$OLD' OR substr(dockerfile_path, 1, LENGTH('$OLD') + 1) = '$OLD/';

UPDATE static_sites
SET gateway_config_path = CASE WHEN gateway_config_path = '$OLD' THEN '$NEW'
                               ELSE REPLACE(gateway_config_path, '$OLD/', '$NEW/') END
WHERE gateway_config_path = '$OLD' OR substr(gateway_config_path, 1, LENGTH('$OLD') + 1) = '$OLD/';

-- 可选：构建日志绝对路径（不影响启动，但避免 doctor 指向 OLD）
UPDATE builds
SET log_path = CASE WHEN log_path = '$OLD' THEN '$NEW'
                    ELSE REPLACE(log_path, '$OLD/', '$NEW/') END
WHERE log_path = '$OLD' OR substr(log_path, 1, LENGTH('$OLD') + 1) = '$OLD/';
SQL
```

（表名：`instances` / `containers` / `static_sites` / `builds`。改前可用 `sqlite3 registry/local-web.db ".schema"` 确认。）

```bash
sqlite3 registry/local-web.db \
  "SELECT id, app_path FROM instances WHERE app_path LIKE '%$(basename "$OLD")%';"
# 应无行；或仅含 NEW
```

### 7.5 pip editable（仅当 CLI 装在工作区内时）

```bash
# 示例：源码在 NEW/安装包/归档_解压 或 NEW 仓库根
pip3 install -e "$NEW/安装包/归档_解压"   # 按你的真实布局调整
python3 -c "import local_webpage_access as m; print(m.__file__)"
# 必须打印 NEW 下的路径
lwa version
```

### 7.6 Caddy 配置：重生，不手改

推荐流程：

1. 可删除或移走旧的主配置备份干扰项（可选）：保留 `sites/`、`aliases/` 也可，但主 Caddyfile 应以新路径重生为准。
2. 启动 manager/daemon 后，对静态实例 `lwa start` / `lwa restart`，或 `lwa gateway on`，让 LWA 按新 Workspace **组装主 Caddyfile**（日志路径、`import` 绝对路径均应变为 NEW）。
3. 验证：

```bash
grep -F "$OLD" static-gateway/Caddyfile && echo "Caddyfile 仍含 OLD — 未重生成功" || echo "Caddyfile OK"
grep -R -F "$OLD" static-gateway/sites static-gateway/aliases 2>/dev/null || echo "sites/aliases OK"
```

若紧急窗口必须先改再启：临时替换路径后**仍应**在服务起来后触发一次正式重生，避免与生成器分叉。

### 7.7 能力缓存

```bash
rm -f run/capability-manager.json run/capability-daemon.json run/capability-gateway.json
# 不要删 daemon-processed.json
```

Full 环境启动后若 `overall=unready`，按运维/FAQ 对 gateway/manager 做能力探测刷新（改名本身不引入新缺口，只是清空缓存会复现已知现象）。

---

## 8. 重启顺序（下层 → 上层）

```bash
cd "$NEW"
unset http_proxy https_proxy HTTPS_PROXY HTTP_PROXY

# 8.1 自启 / 后台
lwa autostart enable          # 若使用自启；会拉起已安装的 daemon/manager/gateway
# 或手动：
# systemctl --user start lwa-daemon.service
# sleep 2
# systemctl --user start lwa-manager.service
# systemctl --user start lwa-gateway.service   # 若已安装

# 若 gateway 不在自启内：
lwa gateway on

# 8.2 仅恢复迁移前应运行的实例（不要盲目 start 全部）
for id in "${RUNNING_IDS[@]}"; do
  lwa start "$id"    # 含 BUG-382：容器已 down 时优先 up -d，无需默认 rebuild
done

# 8.3 访问地址
lwa access refresh
lwa list
```

未升级 BUG-382 时：若 `lwa start` 报 `service "app" has no container to start`，再对该实例 `lwa rebuild`（`data/` bind mount 通常保留；仍建议先有备份）。

---

## 9. 验收清单

| 项 | 命令 / 期望 |
| --- | --- |
| 工作区 | `pwd` / `lwa list` 在 `$NEW` |
| 自启工作区 | `lwa autostart check`；unit 内路径为 NEW |
| import 路径 | `python3 -c "import local_webpage_access as m; print(m.__file__)"` 含 NEW |
| 管理页 | `curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:17800/api/health` → 200 |
| 实例 | 快照中的 ID 均为 running；直连 hostPort / 别名入口 200 |
| 容器挂载 | `docker inspect … Mounts` 的 Source 均在 `$NEW/apps/.../data` |
| 业务数据 | 如 SQLite 用户数、登录接口与迁前一致 |
| Caddy | 主配置与 import **无 OLD**；`:8080` / admin `:2019` 符合预期 |
| pageviews | 各实例 hits ≥ 迁前基线且**未异常翻倍**（升级 BUG-383 后应接近相等） |
| 残留扫描 | `grep -R -F "$OLD" --include='*.json' --include='Caddyfile' --include='*.conf' .` 运行配置为空；日志旧行可忽略 |
| 能力 | Full：`overall=ready`（必要时刷新 capability 缓存） |

---

## 10. 回滚（简版）

仅在**新路径服务已再次停干净**且旧路径备份仍可用时：

1. 停 NEW 上全部 lwa/自启/容器（同 §6）。
2. 若 `mv` 可逆且 NEW 无额外写入：`mv "$NEW" "$OLD"`；否则从 §5.3 备份恢复到 OLD。
3. 将自启 / editable / manifest / registry / Caddy 再改回 OLD（或从备份还原整个树）。
4. 按 §8 在 OLD 拉起，并只恢复快照中的 running 集合。

回滚成本高——**迁前备份与 desiredState 快照不可省**。

---

## 11. 生产布局建议（减少以后再改名的痛）

| 组件 | 建议 |
| --- | --- |
| CLI / 库代码 | 独立 venv 或系统工具目录：`pip install local-webpage-access`（或固定路径 `pip install -e /opt/lwa/src`） |
| Runtime 工作区 | 仅 `local-web.yml`、`apps/`、`registry/`、`run/`、`logs/`、`static-gateway/`、`inbox/` |
| 工作区路径 | Linux 原生文件系统；WSL **不要**放 `/mnt/<drive>` |
| 自启 | `lwa autostart install [--with-caddy]`，解释器指向上述 venv |

这样改名主要影响「工作区根」与 Docker Mounts，而不再绑架 `import local_webpage_access`。

---

## 12. 与 `lwa workspace relocate`（IMP-042）的关系

**CLI 已落地**（DEV-089）。同卷迁移优先：

```bash
lwa workspace relocate <NEW> --dry-run [--json]
lwa workspace relocate <NEW> [--yes]
lwa workspace relocate --verify
# 失败：--resume / --rollback
```

事务覆盖：状态机 `preflight → … → complete`、锁（`O_CREAT|O_EXCL`）与 journal、SQLite online backup、quiesce、同卷 rename、manifest/registry/**sites·aliases** 结构化改写、主 Caddyfile 重生、**仅迁前已装自启时** `autostart repair`（不重启用 config 已关服务）、恢复 running 意图与 detached 控制面、pageviews 对账。`--dry-run` 零副作用；`--resume` 复用 journal 快照；`--rollback` 逆改写回 OLD。Skill `lwa-relocate-workspace` 只调 CLI。

**本手册**用于：CLI 中断后的人工续作、跨盘/跨机、以及理解禁止整树 sed 的契约。日常操作与排障还可对照 [operations-playbook.md](operations-playbook.md)、[faq.md](faq.md)。

---

## 13. 相关文档

- [Runtime 工作区](runtime-workspace.md)
- [开机自启](autostart.md)（含 `repair` 保留 gateway）
- [运维手册](operations-playbook.md)
- [FAQ](faq.md)
- [已知限制](known-limitations.md)
