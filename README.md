# Local Webpage Access (`lwa`)

**English** | [中文](#中文文档)

<a id="english"></a>

---

## Overview

A **local webpage deployment base** for small home servers: import a packaged `zip` project with one command, auto-detect how it should run, allocate a port, and expose it on your LAN.

- Pure static HTML -> served directly by the shared static gateway (Caddy preferred, built-in `http.server` fallback)
- Pure-frontend SPA (Vite / React / Vue / Svelte ...) -> automatic `npm install` + `build`, serving the built assets
- Node / Python backends and SQLite full-stack projects -> generated Dockerfile + Docker Compose, run in containers

Designed for 4–8 GB mini hosts: port-pool isolation, build concurrency limiting, resource monitoring, and log hygiene -- aiming for "import and it just works".

V1 is feature-complete (Phases 0–7): CLI, web manager (HTTP API + SPA frontend), auto-import daemon, security auditing, and `lwa doctor` diagnostics. See the [roadmap](#roadmap).

## Features

- **One-command import**: `lwa import xxx.zip` or `lwa import --from-dir /abs/path` -- extraction with zip-slip protection, sha256/content fingerprinting, single-root flattening, instance registration. Zips dropped into `inbox/` are imported automatically by the daemon. Folder sources are copied read-only into the workspace.
- **Automatic project-type detection**: scans `package.json` / `requirements.txt` / `pyproject.toml` / `Pipfile` / `manage.py` etc. to classify projects as `static` / `node` / `python` (with/without database). A five-layer pipeline (IMP-058) adds static prechecks (shell-operator guarding, SQLite path checks), multi-candidate generation with capability contracts, and empirical verification probes; undetectable projects are marked `pending`. Zero-dependency `http.server` backends are recognized via AST weak signals (V0.8.1).
- **Port-pool management**: ports allocated from a configured pool, skipping registered and actually-listening ports; generates `lanUrl` / `healthUrl`. Optional **path aliases** (`/<slug>/` unified entry, requires Caddy) with SPA absolute-path and JS-bundle API-path compatibility checks (IMP-055).
- **Static hosting**: uses Caddy when available (default profile), otherwise falls back to the built-in `http.server`; the Full profile requires Caddy strictly (no silent degradation). Nested `index.html` supported.
- **Container hosting**: generated Dockerfile (non-root, `EXPOSE` internal port) and Compose (ports / resource limits / `restart: unless-stopped` / SQLite `data/` persistent bind mount).
- **Lifecycle orchestration**: `start` / `stop` / `restart` / `recover` / `rebuild` / `cancel-build` / `remove`; per-instance double locking (in-process `RLock` + cross-process file lock with stale-lock recovery); containers reuse registered ports so `lanUrl` stays stable.
- **Observability**: categorized logs (build / run / gateway / import / scan) with size-based rotation, HTTP health checks, state aggregation, per-host and per-instance resource stats, and **pageview analytics** in the web manager.
- **Build queue**: cross-process gate limiting (default concurrency 1), `queued` marking with controllable timeout; `cancel-build` cancels queued/running builds without touching caches, images, or user data.
- **Atomic gateway switching** (IMP-037): `lwa gateway switch <caddy|builtin>` -- precheck / stop-old-start-new / rollback / `degraded` marking; results distinguish `accessOk` / `fullyOk`.
- **Access-URL freshness** (IMP-038/040): `lanUrl` synthesized at read time; `lwa access refresh` / `doctor --access` / post-update review; throttled LAN-drift self-healing. `access review` detects SPA alias misalignment (empty 200 / 404 / wrong MIME) and JS-bundle API-path misalignment.
- **Full Profile capability freshness**: capability snapshots written by `lwa gateway on` / supervised foreground; manager / gateway / daemon refresh capability caches periodically; internal probes bypass `http_proxy`.
- **SQLite registry**: seven tables (instances / containers / static_sites / ports / events / builds / resources), foreign-key cascades, WAL mode.
- **Web manager** (WBS-22/23): built-in HTTP API + Vue SPA, token auth (auto-rotation every 168h since V0.7.0; loopback reads are token-free, LAN requires a valid token; see `lwa manager token`). Covers instance list / details / logs / resources / lifecycle / cancel-build / path aliases / pageviews / redundant cleanup / two-step safe delete / LAN stale banner / pending queue / port pool / stats. Optional native folder picker on loopback (IMP-051); folder-source instances support "update from source".
- **Auto-import daemon** (WBS-21): `lwa daemon on` watches `inbox/`, imports zips, and starts determinable lightweight instances.
- **Security auditing** (WBS-25): critical/warn/info grading for generated Compose / Dockerfile / zip members; critical issues blocked before writing; zip path traversal / symlinks / zip bombs rejected at import.
- **Diagnostics** (WBS-26 / IMP-033/034): `lwa doctor` checks Python / Docker / Compose / port pool / registry / disk / memory; `--profile full` / `lwa capabilities` emit a unified CapabilityReport plus a platform matrix (works via `--json` even before init). Since V0.8.0 also checks `service_runtime_state` (enabled-but-not-running -> FAIL with recovery command; stopped-but-residual-process -> WARN), `restart_resilience` (missing/disabled autostart units, no linger, wrong container restart policy -> WARN), and `workspace_path_consistency` (derived paths, Caddy refs, SQLite bind-mount drift).
- **Host provisioning** (IMP-031/032/033): `lwa setup` / `lwa init` ship built-in Docker Engine + Compose and Caddy install scripts for macOS/Linux (China mirrors by default); `--full` installs everything and closes the **Full Profile capability loop** (no fake green); `--resume` continues acceptance after re-login/permission refresh.
- **Supported platform gate** (IMP-036): Ubuntu LTS / Debian Stable / WSL2 / macOS only; native Windows hard-fails.
- **Safe autostart defaults** (IMP-030/061): `lwa autostart install` includes the gateway unit by default when `staticGateway=caddy`, tries linger by default, and offers TTY-guided install on Linux systemd. `lwa status` / `autostart status --json` mark each service's run mode (supervised vs bare). V0.8.2: enable/install are idempotent (supervised services are no longer needlessly stopped); macOS bootstrap error 5 is retried with a fail-safe pull-up; the `:2019` conflict check recognizes its own Caddy via live pidfile/owner.
- **Workspace migration** (IMP-042): `lwa workspace relocate` -- same-volume atomic rename with precheck / snapshot / service stop / path rewrite / gated repair / verify / rollback (`--dry-run` / `--resume` / `--verify` / `--rollback`). Since V0.6.12: gateway configs re-pinned to the current workspace before start; SQLite bind-mount drift detected and fail-safed at container start.
- **LLM skills** (WBS-24): 19 SKILL.md files covering setup, import, hosting, containers, lifecycle, autostart, migration, access review, and troubleshooting for AI coding assistants.

## Installation

Requires Python 3.13+, **fastapi ≥ 0.138.0**, and **uvicorn ≥ 0.45.0** (see `pyproject.toml`). Hosting container instances requires **Docker ≥ 29.0.0** and **Docker Compose ≥ 2.40.2** (≥ 5.2.0 recommended); the static gateway prefers Caddy (**≥ 2.10.0**) and falls back to the built-in server without it. Generated container base images: `node:24-alpine` and `python:3.13-slim`. Run `lwa doctor` to verify each of these.

```bash
# From the repo root
pip install -e .

# Dev dependencies (tests)
pip install -e ".[dev]"
```

This installs the `lwa` command; `python3 -m local_webpage_access` also works.

After installing, run `lwa setup` (default profile, no workspace needed) to detect host tools, then `lwa init`; use `lwa init --full --yes` for the full capability loop. If you hit `sessionRefreshRequired` after joining the `docker` group, re-login and run `lwa setup --full --resume`. See the [FAQ](docs/faq.md) and the [operations playbook](docs/operations-playbook.md).

## Supported Platforms (IMP-036)

- **Bare-metal Linux**: Ubuntu **LTS** (22.04/24.04/26.04) and Debian **Stable** (12/13); x86_64 / arm64; kernel ≥ 5.15, glibc ≥ 2.35, systemd
- **WSL2**: same distros; WSL package ≥ 2.1.5 with systemd as PID 1; workspace on the Linux filesystem (autostart fail-closes on `/mnt/<drive>`); check `.wslconfig` memory, Hyper-V firewall port rules in Mirrored mode, and boot wake-up -- see [WSL2 host prep](docs/known-limitations.md)
- **macOS**: 14 Sonoma+
- **Not supported**: native Windows (use WSL2), WSL1, non-LTS Ubuntu, Debian sid/testing, other distros/architectures

`lwa doctor` prints a "platform support" section (platform / supported / reasons / advice) at the end of human-readable output.

## Quick Start

```bash
# 0. (first time) detect host tools; install Docker / Compose / Caddy / Node as prompted
lwa setup

# 1. Initialize a workspace (creates local-web.yml, directories, SQLite registry)
#    Probe for an existing workspace first: curl -s http://127.0.0.1:17800/api/health
lwa init                      # or: lwa init --full --yes

# 2. Import a packaged project zip
lwa import ./inbox/my-site.zip --name my-site
lwa import ./inbox/my-site.zip --path-alias my-site   # optional path alias (needs Caddy)
# lwa import --from-dir /abs/path/to/my-site          # or import from a local folder

# 2b. New version of the same project: in-place update (keeps id / ports / data / alias)
lwa import ./inbox/my-site-v2.zip --update my-site

# 3. (optional) rescan instances marked pending
lwa scan

# 4. Start the instance (unified entry for static / frontend / container)
lwa start my-site

# 5. Status, logs, resources
lwa status
lwa logs my-site --category run --tail 200
lwa stats

# 6. stop / restart / recover / rebuild / remove
lwa stop my-site
lwa restart my-site
lwa recover my-site           # one-shot recovery (matches the web manager)
lwa rebuild my-site
lwa remove my-site            # keeps apps/<id>/ on disk by default
lwa remove my-site --purge --force   # also delete disk files incl. non-empty data/

# 7. (optional) web manager, daemon, Caddy gateway
lwa manager on                # start manager in the background
lwa daemon on                 # watch inbox/, auto-import and self-heal
lwa gateway on                # start Caddy master (:8080 alias entry)

# 7b. (optional) autostart daemon + manager (+ gateway): macOS launchd / Linux·WSL systemd
lwa autostart install         # see docs/autostart.md
lwa autostart check           # deep completeness check

# 8. (optional) diagnostics
lwa doctor                    # all environment checks
lwa doctor --profile full     # Full Profile capability report
lwa doctor my-site            # deep-dive a single instance
lwa access review             # verify access URLs (alias blank-page / API misalignment)
lwa pageviews                 # pageview summary (matches the web manager)
```

The manager token is generated on first `lwa manager on` / `start` (auto-rotated every 168h since V0.7.0); view it locally with `lwa manager token`. See the [manager docs](docs/manager-page.md).

## Command Reference

| Command | Description |
| --- | --- |
| `lwa init [-w DIR] [--force] [--default\|--full] [--yes]` | Initialize a workspace (directories / config / registry / skills), idempotent; `--full` installs dependencies and writes `profile: full` only **after** the capability loop is ready |
| `lwa update` | One-command update channel (IMP-063): fetch -> pinned OID -> `--ff-only` fast-forward -> pip -> interpreter handoff; refuses to fast-forward dirty/diverged/detached/shallow trees; offline degrades to warning; `--check` / `--dry-run` / `--no-pull` / `--remote` / `--ref` escape hatches. Restarts follow three-state reconcile (IMP-059, down-duration estimated from live evidence since V0.8.2); waits up to ~30s for services to be ready before the access/doctor finale (V0.8.2) |
| `lwa workspace relocate <NEW> [--dry-run] [--yes] [--resume\|--verify\|--rollback]` | Same-volume atomic workspace migration (IMP-042); see docs/workspace-rename.md |
| `lwa import <zip> [-n NAME] [--path-alias SLUG] [--update ID]` | Import a zip; optional path alias (compatibility-gated, IMP-055); `--update` upgrades in place (containers auto-rebuild; `--no-restart` swaps source only) |
| `lwa import --from-dir <ABS> [-n NAME] [--path-alias SLUG] [--update ID]` | Import/update from a local folder (IMP-047; read-only copy; `--update` path must match the linked directory) |
| `lwa alias set <ID> <slug>` / `lwa alias clear <ID>` | Set/clear path aliases for static or container instances (needs Caddy; compatibility-checked) |
| `lwa scan [ID]` | Rescan instances (all `pending` if ID omitted) |
| `lwa start <ID>` | Start an instance (lightweight `compose start` if already deployed) |
| `lwa stop <ID>` | Stop an instance (static: disable gateway + release port; container: `compose stop`, data kept) |
| `lwa restart <ID>` | Stop then start (containers: lightweight start, no image rebuild) |
| `lwa recover <ID>` | One-shot recovery (static: pull up Caddy master if needed, then restart) |
| `lwa rebuild <ID>` | Force-rebuild image/artifacts through the build queue |
| `lwa cancel-build <ID>` | Cancel a queued or running build (keeps caches/images/user data) |
| `lwa remove <ID> [--purge] [--force]` | Remove an instance; `--purge` deletes disk files (non-empty `data/` needs `--force`) |
| `lwa remove --redundant [--purge]` | Batch-clean redundant instances (dedup by `sourceZipHash`, keep earliest) |
| `lwa logs <ID> [-c CATEGORY] [-n TAIL]` | View instance logs (build/run/gateway/import/scan) |
| `lwa status [ID]` | View instance status (all if ID omitted) |
| `lwa stats [ID]` | Resource usage (host-wide + instance dir/image/container) |
| `lwa pageviews [ID] [-n LIMIT]` | Pageview summary / per-instance detail (lazy log ingestion) |
| `lwa list` | List all instances and ports |
| `lwa setup [--script] [--json] [--default\|--full] [--yes] [--resume] [--autostart] [--with-caddy]` | Detect host tools; `--full` installs and runs the capability loop; `--resume` continues after permission refresh |
| `lwa doctor [ID] [--json] [--profile default\|full] [--access]` | Diagnose environment and instances; exit code 1 on any fail / Full unready |
| `lwa capabilities [--json]` | Output the workspace CapabilityReport |
| `lwa manager on / off / status` | Start / stop / inspect the web manager |
| `lwa manager token [--json]` | Print the current API token, issue time, and next rotation (IMP-046) |
| `lwa manager start` | Run the manager in the foreground (Ctrl+C to exit) |
| `lwa manager logs [-n TAIL]` | View manager runtime logs |
| `lwa daemon on / off / status` | Control the inbox/ auto-import daemon (self-healing on start + periodic reconcile) |
| `lwa gateway on / off / status` | Control the Caddy gateway master (admin :2019 liveness; `on` reviews access by default) |
| `lwa gateway switch <caddy\|builtin> [--dry-run] [--json] [--no-review]` | Atomically switch gateway backend (IMP-037): precheck -> swap -> sync -> access review; rollback on failure, `degraded` if rollback fails |
| `lwa access refresh` | Recompute all lanUrl/routeUrl from the current LAN IP (DHCP drift self-healing) |
| `lwa access review [--json] [--rebuild-if-needed]` | Verify declared URLs really work (loopback / lanUrl / routeUrl + SPA alias & API-path misalignment) |
| `lwa autostart install [--with-caddy] [--no-enable] [--linger]` | Generate boot/login autostart units (enabled by default; macOS launchd / Linux·WSL systemd foreground supervision) |
| `lwa autostart enable / disable / status` | Load / persistently disable / inspect autostart units and their foreground processes |
| `lwa autostart check [--json]` | Deep completeness check (interpreter / PATH / workspace / unit form / enablement / MainPID identity / process / Caddy·:2019 / linger / WSL / Docker) |
| `lwa autostart repair [--with-caddy]` | Rewrite broken paths, migrate legacy detached launchers, re-enable |
| `lwa autostart uninstall [--purge-linger]` | Stop services + delete unit files (workspace data untouched) |
| `lwa autostart doctor-hints` | Print autostart-related doctor hints |
| `lwa version` | Show the version |

Global option `-v/--verbose` prints DEBUG logs. Use `lwa <command> --help` for full argument details.

## Configuration (`local-web.yml`)

Generated by `lwa init`; key fields:

```yaml
managerPort: 17800          # manager port (must not fall inside the port pool)
managerHost: 0.0.0.0
portPool:                   # instance port pool
  start: 18000
  end: 19999
staticGateway: caddy        # caddy | builtin
staticGatewayPort: 8080     # path-alias unified entry port (Caddy mode)
profile: default            # default | full (written by setup/init --full after the loop is ready)
serviceUser: null           # pinned run identity for Full
buildConcurrency: 1         # build concurrency (keep 1 on mini hosts)
defaultResourceLimits:
  memory: 512m
  cpus: "0.75"
buildMirrors:               # container-build mirrors (China preset; disable overseas)
  enabled: true
  preset: china
lanIpStrategy: auto         # auto (probe) | manual
manualLanIp: null
logLevel: INFO
```

## Workspace Layout

```
<workspace>/
├─ local-web.yml            # global config
├─ inbox/                   # zips to import (daemon watches; success->processed/, repeated failure->failed/)
├─ apps/                    # imported instances
├─ registry/
│  ├─ local-web.db          # SQLite registry
│  └─ build-locks.db        # cross-process build queue gate
├─ static-gateway/sites/    # static site gateway config
├─ static-gateway/aliases/  # path-alias route snippets (Caddy mode)
├─ run/                     # runtime PID / locks / token / pageviews.db / capability snapshots
├─ logs/                    # global logs: lwa.log / manager.log / daemon.log / gateway.log ...
├─ templates/               # user-editable template copies
├─ manager/                 # manager static assets
├─ skills/                  # 19 LLM collaboration SKILL.md files
└─ apps/<id>/
   ├─ local-web.json        # instance metadata (source of truth)
   ├─ source/               # original zip & extraction snapshot
   ├─ current/              # current project source
   ├─ public/               # static/frontend hosting artifacts
   ├─ data/                 # persistent data (SQLite etc., bind-mounted into containers)
   ├─ docker/               # generated Dockerfile / compose.yaml / .env
   └─ logs/                 # categorized logs
```

## Web Manager

`lwa manager on` starts the manager in the background (or `lwa manager start` in the foreground): FastAPI + single-page frontend on `0.0.0.0:17800`. A token is generated on first start and printed to the terminal; loopback **read** APIs are token-free, writes require same-origin or a token (auto-rotated every 168h). The UI covers instance list, details, logs, resources, lifecycle, cancel-build, path aliases, pageviews, redundant cleanup, pending/failed/recoverable queues, port pool, and stats. Under Caddy, instance state additionally distinguishes **gateway unreachable** (`gateway_down`), **invalid config** (`config_invalid`), and `unknown` (observation failure, never misreported as stopped), with a one-click **Recover** action. LAN drift and capability degradation each show a top banner. See the [manager docs](docs/manager-page.md) and [operations playbook](docs/operations-playbook.md).

## Auto-import Daemon

`lwa daemon on` watches `inbox/`: zips are imported automatically, and determinable lightweight instances (pure static, recognized frontends) are started right away. On start and every 60s the daemon runs `reconcile()` to restore instances whose `desired=running` but whose process/gateway dropped. Container auto-correction is skipped when observation fails or Full capabilities are not ready, to avoid mislabeling running containers. `lwa daemon off` / `lwa daemon status` control and inspect it.

## Development & Testing

```bash
pip install -e ".[dev]"
python3 -m pytest           # full unit & integration tests (no real Docker needed)
```

Code lives in `src/local_webpage_access/`, tests in `tests/`. Container-related tests use fake runtimes; real Docker integration tests need `LWA_RUN_DOCKER_TESTS=1` -- see the [testing guide](docs/testing.md). Sample fixtures live in `tests/fixtures/` (static HTML, Vite/React, Node/Express, FastAPI+SQLite, failing build, unrecognized pending).

## Documentation Index

| Doc | Contents |
| --- | --- |
| [docs/runtime-workspace.md](docs/runtime-workspace.md) | Runtime workspace directories, ports, resource tiers |
| [docs/workspace-rename.md](docs/workspace-rename.md) | Workspace migration handbook (prefer `lwa workspace relocate`) |
| [docs/operations-playbook.md](docs/operations-playbook.md) | Daily operations quick reference (provisioning / checklists / logs / gateway / inbox / aliases / Caddy troubleshooting) |
| [docs/manager-page.md](docs/manager-page.md) | Manager API endpoints, auth, usage |
| [docs/faq.md](docs/faq.md) | FAQ & troubleshooting (symptom -> log mapping) |
| [docs/security-boundary.md](docs/security-boundary.md) | Security boundaries and default protections |
| [docs/release-checklist.md](docs/release-checklist.md) | V1 release checklist |
| [docs/known-limitations.md](docs/known-limitations.md) | Known limitations (incl. WSL2 host prep) |
| [docs/autostart.md](docs/autostart.md) | Autostart (macOS launchd / Linux·WSL2 systemd) |
| [docs/testing.md](docs/testing.md) | Test system and how to run it |
| [docs/acceptance-checklist.md](docs/acceptance-checklist.md) | V1 end-to-end acceptance checklist |
| [skills/README.md](src/local_webpage_access/skills/README.md) | Built-in LLM skills overview (19) |

## Roadmap

| Phase | Scope | Status |
| --- | --- | --- |
| Phase 0 | CLI skeleton / config / registry / schema | Done |
| Phase 1 | zip import / type detection / port pool | Done |
| Phase 2 | static gateway / pure static / frontend build hosting | Done |
| Phase 3 | Dockerfile / Compose / runtime / Node & Python containers | Done |
| Phase 4 | lifecycle / logs / health / resources / build queue | Done |
| Phase 5 | daemon & manager (API + frontend) | Done |
| Phase 6 | skills, security, diagnostics | Done |
| Phase 7 | testing, acceptance, release | Done |

Task tracking lives in `task-list.md` (Chinese).

## License

MIT

---

# 中文文档

[English](#english) | **中文**

## 简介

面向局域网小主机的**本地网页部署基座**：把一个打包好的 `zip` 项目，一条命令导入、自动识别运行形态、分配端口并对局域网暴露访问入口。

- 纯静态 HTML -> 经共享静态网关（Caddy 优先，内置 `http.server` 兜底）直接托管
- 纯前端 SPA（Vite / React / Vue / Svelte 等）-> 自动 `npm install` + `build`，托管构建产物
- Node / Python 后端、含 SQLite 的全栈项目 -> 自动生成 Dockerfile + Docker Compose 并起容器

面向 4G/8G 内存的小主机设计：端口池隔离、构建并发限流、资源监控与日志治理，尽量做到“导入即用”。

V1 已完成全部功能（Phase 0~7），提供 CLI、管理页（HTTP API + 前端）、自动导入守护进程、安全审计与 `lwa doctor` 排障。详见文末[路线图](#路线图)。

## 特性

- **一键导入**：`lwa import xxx.zip` 或 `lwa import --from-dir /abs/path`（IMP-047）完成解压/复制、zip-slip 防护、sha256/内容指纹校验、单层根目录拍平、实例登记；放入 `inbox/` 由 daemon 自动导入。文件夹源为只读复制进工作区，禁止就地运行关联目录。
- **运行形态自动识别**：扫描 `package.json` / `requirements.txt` / `pyproject.toml` / `Pipfile` / `manage.py` 等，判定 `static` / `node` / `python` 与是否含数据库；识别失败标记 `pending` 并写入风险提示事件。五层流水线（IMP-058）：静态预检（shell 操作符/SQLite 路径/Alembic 编排防护）-> 多候选生成与能力契约校验 -> 实证校验探针（2xx/3xx、能力守恒、回滚边界）；零依赖 `http.server` 后端经 AST 弱信号识别（V0.8.1）。
- **端口池管理**：从配置端口池中分配，跳过 registry 已登记端口与宿主机实际监听端口，生成 `lanUrl` / `healthUrl`；静态与容器实例均可选**路径别名**（`/<slug>/` 统一入口，**需 Caddy**），设置时自动检测 SPA 绝对路径资源与 JS bundle 内 API 路径错位，不兼容则拒绝（IMP-055）。
- **静态托管**：default 档在 Caddy 可用时用 Caddy，否则降级内置 `http.server`；Full 档要求 Caddy 严格可用（不静默降级）。支持嵌套 `index.html`。
- **容器托管**：按技术栈生成 Dockerfile（非 root、`EXPOSE` 内部端口）与 Compose（端口/资源限额/`restart: unless-stopped`/SQLite `data/` 持久化 bind mount）。
- **生命周期编排**：`start` / `stop` / `restart` / `recover` / `rebuild` / `cancel-build` / `remove`，实例级双层锁（进程内 `RLock` + 跨进程文件锁 + 陈旧锁回收）串行化同一实例操作；容器复用已登记端口保证 `lanUrl` 稳定。
- **可观测性**：分类日志（build / run / gateway / import / scan）与按大小滚动、HTTP 健康检查、状态聚合、整机与实例级资源统计；管理页**浏览量统计**。
- **构建队列**：跨进程闸门限流（默认并发 1），拿不到槽位即标记 `queued`，排队超时可控；`cancel-build` 可取消排队/进行中构建（不删缓存/镜像/用户数据）。
- **网关原子切换（IMP-037）**：`lwa gateway switch <caddy|builtin>` 事务切换后端（预检/停旧启新/回滚/`degraded`）；结果区分 `accessOk` / `fullyOk`。
- **访问地址新鲜度（IMP-038/040）**：管理页读时合成 `lanUrl`；`lwa access refresh` / `doctor --access` / update 收尾 review；LAN 漂移节流自愈。`access review` 检测 SPA 别名资源错位（空 200 / 404 / 错误 MIME）与 JS bundle 内 API 路径错位。
- **Full Profile 能力新鲜度**：`lwa gateway on` / 前台监管写能力快照；manager / gateway / daemon 周期刷新能力缓存；内部探针直连（不受 `http_proxy` 影响）。
- **SQLite Registry**：七张表（instances / containers / static_sites / ports / events / builds / resources），外键级联、WAL 模式。
- **管理页（WBS-22/23）**：内置 HTTP API + Vue 单页前端，token 鉴权（**V0.7.0** 起默认每 168h 自动轮换，本机 loopback 免 token，LAN 须有效 token；`lwa manager token` 查询），覆盖实例列表 / 详情 / 日志 / 资源 / 生命周期 / **取消构建** / 路径别名 / **浏览量** / **冗余清理** / **安全删除（双阶段确认）** / LAN stale 横幅 / pending 队列 / 端口池 / 统计；显示名优先 `--name`，其次主页 HTML `<title>`；文件夹源实例可「从源更新」；本机 loopback 下可用「选择文件夹」原生对话框（IMP-051）。
- **自动导入守护进程（WBS-21）**：`lwa daemon on` 后监听 `inbox/`，自动导入并启动可确定的轻量实例。
- **安全审计（WBS-25）**：对生成的 Compose / Dockerfile / zip 成员做 critical/warn/info 分级审计；critical 在写出前拦截；zip 路径穿越/符号链接/炸弹拒绝导入。
- **排障辅助（WBS-26 / IMP-033/034）**：`lwa doctor` 检查 Python / Docker / Compose / 端口池 / registry / 磁盘 / 内存；`--profile full` / `lwa capabilities` 输出统一 CapabilityReport 与平台矩阵报告（`--json` 未 init 亦可）。V0.8.0 起新增 `service_runtime_state`（enabled 未运行 -> FAIL 附恢复命令；已停用但进程残留 -> WARN）、`restart_resilience`（自启单元缺失/未启用/无 linger/容器策略不符 -> WARN）与 `workspace_path_consistency`（派生路径、Caddy 引用、SQLite bind mount 漂移核对）。
- **宿主机装配（IMP-031/032/033）**：`lwa setup` / `lwa init` 内置 macOS/Linux 的 Docker Engine+Compose、Caddy 安装脚本（默认国内源）；`--full` 装齐并做 **Full Profile 能力闭环**（未闭环不假绿）；`--resume` 在重登/权限刷新后续跑验收。
- **正式平台门禁（IMP-036）**：仅 Ubuntu LTS / Debian Stable / WSL2 / macOS；Windows 原生 hard fail。
- **自启缺省安全（IMP-030/061）**：`lwa autostart install` 在 `staticGateway=caddy` 时 gateway 单元默认纳入、linger 默认尝试；Linux systemd 环境 TTY 交互引导安装；`lwa status`/`autostart status --json` 标注每个服务的运行模式（监管 vs 裸进程）。**V0.8.2**：enable/install 幂等（监督器已在管的服务不再误停重迁）；macOS bootstrap 偶发 error 5 自动短重试、失败且 gateway 意图 enabled 时 fail-safe 直接拉起；`:2019` 冲突检查按 live pidfile/owner 识别自家 caddy。
- **工作区迁移（IMP-042）**：`lwa workspace relocate` 同卷原子改名（预检 / 快照 / 停服 / rename / 路径改写 / 门控 repair / 验收；`--dry-run` / `--resume` / `--verify` / `--rollback`）；跨盘见[迁移手册](docs/workspace-rename.md)。V0.6.12 起防复发：gateway 配置启动前按当前工作区重新落盘；容器启动检测 SQLite bind mount 漂移并 fail-safe 救援。
- **大模型 Skills（WBS-24）**：19 个 SKILL.md 覆盖环境初始化、导入、托管、容器、生命周期、自启动、迁移、访问复核、排障等场景，供 AI 编程助手协作。

## 安装

需要 Python 3.13+，以及 **fastapi ≥ 0.138.0**、**uvicorn ≥ 0.45.0**（见 `pyproject.toml`）。托管容器实例需要 **Docker ≥ 29.0.0** 与 **Docker Compose ≥ 2.40.2**（推荐 ≥ 5.2.0）；静态网关优先 Caddy（**≥ 2.10.0**），无 Caddy 时自动用内置服务。容器基线镜像为 `node:24-alpine` 与 `python:3.13-slim`。`lwa doctor` 可逐项校验。

```bash
# 克隆后在项目根目录
pip install -e .

# 开发依赖（测试）
pip install -e ".[dev]"
```

安装后提供 `lwa` 命令；也可用 `python3 -m local_webpage_access` 调用。

建议先 `lwa setup`（default，无需工作区）检测宿主机工具，再 `lwa init`；需要 Full 能力闭环时用 `lwa init --full --yes`。Linux 加入 `docker` 组后若仍 `sessionRefreshRequired`，重登后执行 `lwa setup --full --resume`。详见[排障指南](docs/faq.md)与[运维手册](docs/operations-playbook.md)。

## 正式支持平台（IMP-036）

- **Linux 裸机**：Ubuntu **LTS**（22.04/24.04/26.04）与 Debian **Stable**（12/13）；x86_64 / arm64；kernel ≥5.15、glibc ≥2.35、systemd
- **WSL2**：同上发行版；WSL 包 ≥2.1.5 且 systemd 为 PID 1；工作区放 Linux 文件系统（autostart 对 `/mnt/<drive>` fail-closed）；运行前核对 `.wslconfig` 内存、Mirrored 网络防火墙逐端口放行与自启唤醒，见 [WSL2 宿主准备](docs/known-limitations.md)
- **macOS**：14 Sonoma+
- **不支持**：Windows 原生（请改用 WSL2）、WSL1、Ubuntu 非 LTS、Debian sid/testing、未列入的发行版/架构

`lwa doctor` 会在输出末尾打印「平台支持」段（platform / supported / 原因 / 建议）；未初始化工作区时仍可用 `lwa doctor --json` 查看。

## 快速开始

```bash
# 0.（首次）检测宿主机环境，按提示安装 Docker / Compose / Caddy / Node 等
lwa setup

# 1. 初始化工作区（生成 local-web.yml、目录结构、SQLite registry）
#    先探测是否已有工作区：curl -s http://127.0.0.1:17800/api/health
lwa init                      # 可选 Full：lwa init --full --yes

# 2. 导入一个打包好的项目 zip
lwa import ./inbox/my-site.zip --name my-site
lwa import ./inbox/my-site.zip --path-alias my-site   # 可选路径别名（需 Caddy）
# lwa import --from-dir /abs/path/to/my-site          # 或从本机文件夹源导入

# 2b. 同项目新版本：原地更新（保留 id / 端口 / data / 路径别名）
lwa import ./inbox/my-site-v2.zip --update my-site

# 3.（可选）对 pending 实例重新识别
lwa scan

# 4. 启动实例（静态 / 前端 / 容器统一入口）
lwa start my-site

# 5. 查看状态、日志、资源
lwa status
lwa logs my-site --category run --tail 200
lwa stats

# 6. 停止 / 重启 / 恢复 / 重建 / 移除
lwa stop my-site
lwa restart my-site
lwa recover my-site           # 一键恢复（对齐管理页）
lwa rebuild my-site
lwa remove my-site            # 默认保留 apps/<id>/ 磁盘文件
lwa remove my-site --purge --force   # 连同磁盘文件与非空 data/ 一起删除

# 7.（可选）打开管理页、守护进程与 Caddy 网关
lwa manager on                # 后台启动管理页
lwa daemon on                 # 监听 inbox/，自动导入并自愈
lwa gateway on                # 启动 Caddy master（:8080 别名入口）

# 7b.（可选）开机/登录自启：macOS launchd / Linux·WSL systemd
lwa autostart install         # 见 docs/autostart.md
lwa autostart check           # 完备性深检

# 8.（可选）环境/实例排障
lwa doctor                    # 全部环境检查
lwa doctor --profile full     # Full Profile 能力契约报告
lwa doctor my-site            # 单实例深度诊断
lwa access review             # 复核访问地址（别名白屏 / API 路径错位）
lwa pageviews                 # 浏览量汇总
```

管理页 token 在首次 `lwa manager on` / `start` 时生成（V0.7.0 起默认每 168h 自动轮换）；本机用 `lwa manager token` 查看。详见[管理页说明](docs/manager-page.md)。

## 命令参考

| 命令 | 说明 |
| --- | --- |
| `lwa init [-w DIR] [--force] [--default\|--full] [--yes]` | 初始化工作区（目录 / 配置 / registry / skills），幂等；`--full` 装齐依赖并在能力闭环 **ready 后**写入 `profile: full` |
| `lwa update` | **一键更新通道（IMP-063）**：fetch -> 固定候选 OID -> `--ff-only` 快进 -> pip -> 新解释器接力；tracked 脏/分叉/detached/浅历史拒绝快进；断网降级 warning；`--check`/`--dry-run`/`--no-pull`/`--remote`/`--ref` 逃生舱。服务重启按三态 reconcile（IMP-059；V0.8.2 起中断时长按 live 证据估算）；重启后**等待服务就绪**（最多约 30s，V0.8.2）再收尾刷新访问地址与 doctor |
| `lwa workspace relocate <NEW> [--dry-run] [--yes] [--resume\|--verify\|--rollback]` | 同卷原子迁移工作区根（IMP-042）；见 docs/workspace-rename.md |
| `lwa import <zip> [-n NAME] [--path-alias SLUG] [--update ID]` | 导入 zip；可选路径别名（IMP-055 兼容性门禁）；`--update` 原地升级（容器自动 rebuild，`--no-restart` 仅换源码） |
| `lwa import --from-dir <ABS> [-n NAME] [--path-alias SLUG] [--update ID]` | 本机文件夹源导入/更新（IMP-047；只读复制；`--update` 路径须与关联目录一致） |
| `lwa alias set <ID> <slug>` / `lwa alias clear <ID>` | 设置/清除路径别名（需 Caddy；设置时兼容性检测，不兼容则拒绝） |
| `lwa scan [ID]` | 重新扫描实例（省略 ID 则扫所有 `pending`） |
| `lwa start <ID>` | 启动实例（容器已部署走轻量 `compose start`） |
| `lwa stop <ID>` | 停止实例（静态禁用网关+释放端口；容器 `compose stop`，不删数据） |
| `lwa restart <ID>` | 先停再启（容器走轻量 start，不重建镜像） |
| `lwa recover <ID>` | 一键恢复（静态：必要时先拉起 Caddy master 再 restart） |
| `lwa rebuild <ID>` | 强制重建镜像/产物，经构建队列限流 |
| `lwa cancel-build <ID>` | 取消排队/进行中的构建（不删缓存/镜像/用户数据） |
| `lwa remove <ID> [--purge] [--force]` | 移除实例；`--purge` 删磁盘文件，非空 `data/` 需 `--force` |
| `lwa remove --redundant [--purge]` | 批量清理冗余实例（按 `sourceZipHash` 去重保留最早者） |
| `lwa logs <ID> [-c CATEGORY] [-n TAIL]` | 查看实例日志（build/run/gateway/import/scan） |
| `lwa status [ID]` | 查看实例状态（省略 ID 显示全部） |
| `lwa stats [ID]` | 资源占用（整机 + 实例目录/镜像/容器） |
| `lwa pageviews [ID] [-n LIMIT]` | 浏览量汇总 / 单实例详情 |
| `lwa list` | 列出所有实例及端口 |
| `lwa setup [--script] [--json] [--default\|--full] [--yes] [--resume] [--autostart] [--with-caddy]` | 检测宿主机工具；`--full` 安装并做能力闭环验收；`--resume` 权限刷新后续跑 |
| `lwa doctor [ID] [--json] [--profile default\|full] [--access]` | 诊断环境与实例；有 fail / Full unready 时退出码 1 |
| `lwa capabilities [--json]` | 输出当前工作区 CapabilityReport |
| `lwa manager on / off / status` | 后台启动 / 停止 / 查看管理页状态 |
| `lwa manager token [--json]` | 打印当前 API token、颁发时间与下次轮换时间（IMP-046） |
| `lwa manager start` | 前台启动管理页（Ctrl+C 退出） |
| `lwa manager logs [-n TAIL]` | 查看管理页运行时日志 |
| `lwa daemon on / off / status` | 控制 inbox/ 自动导入守护进程（启动即自愈 + 周期 reconcile） |
| `lwa gateway on / off / status` | 控制 Caddy 网关 master（admin :2019 探活；`on` 默认复核访问地址） |
| `lwa gateway switch <caddy\|builtin> [--dry-run] [--json] [--no-review]` | 原子切换网关后端（IMP-037）：预检 -> 停旧启新 -> 同步 -> access 收尾；失败回滚，回滚失败标 `degraded` |
| `lwa access refresh` | 用当前 LAN IP 重算所有实例 lanUrl/routeUrl（漂移自愈） |
| `lwa access review [--json] [--rebuild-if-needed]` | 复核各实例声明 URL 真实可用性（回环 / lanUrl / routeUrl + SPA 别名与 API 路径错位检测） |
| `lwa autostart install [--with-caddy] [--no-enable] [--linger]` | 生成开机/登录自启单元（默认启用；macOS launchd / Linux·WSL systemd 前台监管） |
| `lwa autostart enable / disable / status` | 加载 / 停用（持久 disable）/ 查看自启单元与前台进程 |
| `lwa autostart check [--json]` | 完备性深检（解释器 / PATH / 工作区 / 单元形态 / 启用态 / MainPID 身份 / Caddy·:2019 / linger / WSL / Docker） |
| `lwa autostart repair [--with-caddy]` | 重写失效路径、迁移旧 detached 启动器并重新启用 |
| `lwa autostart uninstall [--purge-linger]` | 停服务 + 删单元文件（不删工作区数据） |
| `lwa autostart doctor-hints` | 打印自启相关 doctor 提示文案 |
| `lwa version` | 显示版本号 |

全局选项 `-v/--verbose` 输出 DEBUG 日志。各命令参数细节用 `lwa <command> --help` 查看。

## 配置（`local-web.yml`）

由 `lwa init` 生成，关键字段：

```yaml
managerPort: 17800          # 管理页端口（不能落在端口池内）
managerHost: 0.0.0.0
portPool:                   # 实例端口池
  start: 18000
  end: 19999
staticGateway: caddy        # caddy | builtin
staticGatewayPort: 8080     # 路径别名统一入口端口（Caddy 模式）
profile: default            # default | full（能力闭环 ready 后写入）
serviceUser: null           # Full 固化的运行身份
buildConcurrency: 1         # 构建并发数（小主机建议保持 1）
defaultResourceLimits:
  memory: 512m
  cpus: "0.75"
buildMirrors:               # 容器构建国内镜像（海外可 enabled: false）
  enabled: true
  preset: china
lanIpStrategy: auto         # auto（自动探测）| manual
manualLanIp: null
logLevel: INFO
```

## 工作区目录布局

```
<workspace>/
├─ local-web.yml            # 全局配置
├─ inbox/                   # 待导入的 zip（daemon 自动监听；成功->processed/，连续失败->failed/）
├─ apps/                    # 已导入实例
├─ registry/
│  ├─ local-web.db          # SQLite registry
│  └─ build-locks.db        # 构建队列跨进程闸门
├─ static-gateway/sites/    # 静态站点网关配置
├─ static-gateway/aliases/  # 路径别名路由片段（Caddy 模式）
├─ run/                     # 运行期 PID / 锁 / token / pageviews.db / 能力快照
├─ logs/                    # 全局日志：lwa.log / manager.log / daemon.log / gateway.log 等
├─ templates/               # 用户可编辑模板副本
├─ manager/                 # 管理页静态资源
├─ skills/                  # 19 个大模型协作 SKILL.md
└─ apps/<id>/
   ├─ local-web.json        # 实例元数据（真相文件）
   ├─ source/               # 原始 zip 与解压快照
   ├─ current/              # 当前项目源码
   ├─ public/               # 静态/前端托管产物
   ├─ data/                 # 持久化数据（SQLite 等，bind mount 进容器）
   ├─ docker/               # 生成的 Dockerfile / compose.yaml / .env
   └─ logs/                 # 分类日志
```

## 管理页

`lwa manager on` 后台启动管理页（或 `lwa manager start` 前台）：FastAPI + 单页前端，默认 `0.0.0.0:17800`。首次启动生成 token 并打印到终端；本机 loopback 的**读** API 免 token，**写**操作需同源（浏览器管理页）或带 token（每 168h 自动轮换）。管理页覆盖实例列表、详情、日志、资源、生命周期、取消构建、路径别名、浏览量、冗余清理、pending/failed/可恢复队列、端口池与统计。Caddy 模式下实例状态额外区分**网关不可达**（`gateway_down`）、**配置无效**（`config_invalid`）与 `unknown`（观测失败，不误写成 stopped），可一键**恢复**。LAN 漂移与能力降级各有顶部横幅。详见[管理页说明](docs/manager-page.md)与[运维手册](docs/operations-playbook.md)。

## 自动导入守护进程

`lwa daemon on` 开启 inbox/ 自动监听：zip 放进 `inbox/` 即自动导入，可确定的轻量实例（纯静态、已识别前端）直接启动；启动时与每 60s 执行 `reconcile()`，恢复 `desired=running` 但进程/网关掉线的实例。观测失败或 Full 能力未 ready 时**跳过容器自动纠正**，避免误写 stopped。`lwa daemon off` 关闭，`lwa daemon status` 查看状态。

## 开发与测试

```bash
pip install -e ".[dev]"
python3 -m pytest           # 全量单元测试与集成测试（不依赖真实 Docker）
```

代码位于 `src/local_webpage_access/`，测试位于 `tests/`。容器相关测试用替身（fake runtime）运行；真实 Docker 集成测试需设置 `LWA_RUN_DOCKER_TESTS=1`，详见[测试指南](docs/testing.md)。样例项目夹具见 `tests/fixtures/`（6 个样例：静态 HTML、Vite/React、Node/Express、FastAPI+SQLite、构建失败、未识别 pending）。

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [docs/runtime-workspace.md](docs/runtime-workspace.md) | Runtime 工作区目录、端口、资源档位 |
| [docs/workspace-rename.md](docs/workspace-rename.md) | 工作区迁移手册（优先 `lwa workspace relocate`） |
| [docs/operations-playbook.md](docs/operations-playbook.md) | 日常运维速查（装配 / 清单 / 日志 / 网关 / inbox / 别名 / Caddy 排障） |
| [docs/manager-page.md](docs/manager-page.md) | 管理页 API 端点、鉴权与使用 |
| [docs/faq.md](docs/faq.md) | 常见问题与排障路径（症状->日志） |
| [docs/security-boundary.md](docs/security-boundary.md) | 安全边界与默认保护 |
| [docs/release-checklist.md](docs/release-checklist.md) | V1 发布清单 |
| [docs/known-limitations.md](docs/known-limitations.md) | 已知限制（含 WSL2 宿主准备） |
| [docs/autostart.md](docs/autostart.md) | 开机自启（macOS launchd / Linux·WSL2 systemd） |
| [docs/testing.md](docs/testing.md) | 测试体系与运行方式 |
| [docs/acceptance-checklist.md](docs/acceptance-checklist.md) | V1 端到端验收清单 |
| [skills/README.md](src/local_webpage_access/skills/README.md) | 内置大模型 Skills 总览（19 个） |

## 路线图

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| Phase 0 | CLI 骨架 / 配置 / registry / schema | 已完成 |
| Phase 1 | zip 导入 / 形态识别 / 端口池 | 已完成 |
| Phase 2 | 静态网关 / 纯静态 / 前端构建托管 | 已完成 |
| Phase 3 | Dockerfile / Compose / Runtime / Node & Python 容器 | 已完成 |
| Phase 4 | 生命周期 / 日志 / 健康 / 资源 / 构建队列 | 已完成 |
| Phase 5 | 守护进程与管理页（API + 前端） | 已完成 |
| Phase 6 | Skills、安全与排障 | 已完成 |
| Phase 7 | 测试、验收与发布 | 已完成 |

任务跟踪见 `task-list.md`。

## 许可

MIT
