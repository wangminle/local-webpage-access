<h1>
  <img src="src/local_webpage_access/manager_static/logo.svg" alt="LWA" width="48" height="48" align="absmiddle">
  Local Webpage Access (<code>lwa</code>)
</h1>

A mini platform for deploying web projects on a LAN. Import a zip (or a local folder); LWA detects the stack, assigns a port, and gives you a LAN URL.

**English** · [中文](#中文文档)

| English | 中文 |
| --- | --- |
| [Overview](#overview) · [Features](#features) · [Install](#installation) · [Platforms](#supported-platforms) · [Quick start](#quick-start) | [简介](#简介) · [特性](#特性) · [安装](#安装) · [平台](#支持的平台) · [快速开始](#快速开始) |
| [Commands](#command-reference) · [Config](#configuration) · [Layout](#workspace-layout) · [Manager](#web-manager) | [命令](#命令参考) · [配置](#配置) · [布局](#工作区布局) · [管理页](#管理页) |
| [Daemon](#auto-import-daemon) · [Develop](#development-and-testing) · [Docs](#documentation) · [Roadmap](#roadmap) | [守护进程](#自动导入守护进程) · [开发](#开发与测试) · [文档](#文档) · [路线图](#路线图) |

---

## Overview

Built for 4–8 GB home servers: **import and it runs**.

- Pure static HTML → shared static gateway (Caddy, with built-in `http.server` as fallback)
- Frontend SPA (Vite / React / Vue / Svelte, …) → `npm install` + `build`, then serve the output
- Node / Python backends and SQLite full-stack apps → generated Dockerfile + Compose, run in containers

CLI, web manager, inbox auto-import, import-time security checks, and `lwa doctor` are all in V1. Details live in [docs/](docs/faq.md); this page is the on-ramp.

![LWA Web Manager](docs/images/lwa-manager.png)

## Features

- **Import a zip, a local folder, or drop files into `inbox/`** — zip-slip protection, content fingerprints, read-only copy into the workspace (never run from your source tree).
- **Detects how to run it** — `static` / Node / Python, with or without SQLite; unknown projects stay `pending` until you rescan.
- **Port pool + optional path aliases** — stable `lanUrl`; `/{slug}/` via Caddy, refused if the SPA’s absolute paths would break. Alias entries inject `X-Real-IP` for the backend. Verified aliases survive a failed post-start live check (V0.8.9, issue #21), and unmatched alias routes return 404 instead of an empty 200.
- **Static and container hosting** — Caddy or builtin for static; generated non-root Dockerfile/Compose with SQLite `data/` mounts for apps (UID/GID aligned with the host `data/` owner; legacy instances migrate explicitly via `lwa migrate-user`, issue #20; image-layer dirs get `chown` before `USER`, issue #22). Optional manifest `buildHooks` / `preStart` hooks (issue #7) that survive `scan` / `import --update` / `rebuild --sync` (issue #23). pip layers chain mirrors with per-source retries/timeouts and `||` fallback (aliyun → PyPI → Tencent by default; `pipFallbacks` / `pipRetries` / `pipTimeout` configurable, issue #18).
- **Lifecycle on a small host** — start / stop / recover / rebuild / cancel-build; default build concurrency 1; instance ports stay put. `rebuild` warns when the linked folder/git source has drifted (`--sync` refreshes it first; doctor `source_freshness` audits all instances).
- **Web manager + inbox daemon** — Vue UI on `:17800` (loopback reads are token-free; LAN needs a rotating token). The daemon imports zips and heals dropped lightweight instances.
- **Won’t silently write dangerous images** — generated Compose/Dockerfile audited before write; zip traversal / symlinks / bombs rejected.
- **`lwa doctor` does not fake green** — Python / Docker / Compose / ports / disk, plus service runtime and autostart resilience; failed service starts report `lastStartError` with a 3-in-24h auto-restart circuit breaker (V0.8.9, IMP-064), and `version_freshness` reuses `lwa update --check` with a 24h cache to flag outdated installs (IMP-062). `lwa list` / `status` surface compatibility-preflight findings (IMP-056 C.01–C.03). `--json` works even before `init`.
- **Gateway starts stay single-master (V0.8.10, issue #26)** — all Caddy start/stop recovery paths share one reentrant process/thread lock; update additionally counts admin listeners and reports duplicate masters instead of passing a false-green health check.
- **Fedora joins the supported matrix (V0.8.11)** — platform gate, Docker install script (dnf repo written directly, no config-manager plugin dependency), and Caddy install script (dnf package with GitHub-binary fallback) cover Fedora 43/44, a rolling window of current + previous stable; Rawhide and out-of-window versions are rejected.
- **`lwa update` recovers from a missing pip instead of dead-looping (V0.8.11, issue #27)** — pip availability is probed **before** fast-forwarding, so a venv that lost pip fails fast with zero mutations; when pip fails after the fast-forward, the recovery chain leads with `ensurepip --upgrade` instead of "rerun lwa update / pip install", both of which depend on the missing pip itself.
- **Security hardening batch (V0.8.11)** — npm monorepo workspace names are shell-quoted before reaching the build command; new port allocations bind-probe (bind-only occupants are no longer misallocated); `extraVolumes` are structurally validated and rendered as quoted YAML scalars; `cap_add: ALL` and downloader→interpreter chains (`curl && sh`, `wget | python`, …) are audited, with docs stating plainly the audit is a pattern gate, not a sandbox.
- **In-place source-kind switch (V0.8.12, issue #28)** — `lwa import --from-dir/--from-git --update <id>` now switches a zip instance to folder/git source in place: id, hostPort, path alias, `data/`, and desired state all survive; no more remove+reimport data loss. Identical content still completes the identity switch.
- **Fast convergence when Docker comes up late (V0.8.12, issue #29)** — when Docker capability is unavailable (e.g. macOS Docker Desktop starting after a reboot), daemon/manager probes back off 10s→20s→40s (capped 60s) instead of the 300s cadence; `setup --full --resume` triggers an immediate manager re-probe and waits for daemon convergence instead of parroting the same advice; `overall=ready` clears stale action text. Backoff counters are exponent-capped so long-running unavailability can never overflow a background thread.
- **Instance-level build environment variables (V0.8.13, DEV-132)** — manifest top-level `buildEnv` (e.g. `{"VITE_BASE": "/<alias>/"}`) is merged over `os.environ` when running install/build commands, and — like `buildHooks` / `preStart` — survives `scan` / `import --update` / `rebuild --sync` re-detection instead of being silently wiped (the root cause of alias-site white screens after updates). Vite instances refused by the path-alias guard get the LWA-side remedy appended to the error: `lwa configure <id> --build-env VITE_BASE=/<alias>/` (whole-map replace, so pass the other variables along; requires the project's vite.config to read `process.env.VITE_BASE`), or `--base` in `entry.build` (only when the build script calls Vite directly; not preserved on re-scan), then `lwa rebuild`. If `--follow-alias-base` is already on, the manual `VITE_BASE` is overridden by the current alias — disable it first (`--no-follow-alias-base`), complete the target-base build and the alias change, then re-enable following (BUG-640; the four-step hint in the error already follows this order).
- **Redundant cleanup won't delete curated instances (V0.8.13, issue #31)** — batch removal of redundant instances skips any that carry their own config (path alias, `buildEnv`) and reports them as skipped; `lwa remove --redundant --allow-config-loss` (or the API's `allowConfigLoss=true`) opts in explicitly. The manager's 「批量删除冗余」 button now opens a dialog listing every instance's fate instead of a bare confirm, with a visible legend explaining the amber redundancy stripe.
- **Fresh imports refuse `--dry-run` (V0.8.13, issue #30)** — `lwa import` on a brand-new zip/folder/git target with `--dry-run` is rejected before the workspace is even opened (exit 2, hint points to `--update`), closing the gap where a "preview" silently performed a real import; the `--update` path keeps its non-destructive preview semantics.
- **`lwa configure`: per-instance settings (V0.8.13)** — new command (plus `PATCH /api/instances/{id}/settings`) to view, whole-map replace, or clear `buildEnv`; `--follow-alias-base` derives `VITE_BASE` from the current path alias at every build (`/` without one, overriding the manual value — the project must read the variable, applied by `lwa rebuild`); `--acknowledge-redundancy` marks an intentional duplicate and exits redundancy candidacy (reversible). Every toggle has a `--no-` inverse. Host frontend builds inject install/build command env; docker-compose instances write Dockerfile `ARG`/`ENV` and Compose `build.args` (issue #39).
- **Redundancy removal hardening (V0.8.13, issue #31)** — running or transitioning instances are always skipped (`allowConfigLoss` / `--force` cannot override), and the guard is re-checked under the instance lock so mid-flight config edits abort the deletion; the manager dialog adds per-instance alias and updatedAt columns, an intentional-keep button, and an explicit config-loss checkbox, and disables confirm when nothing is deletable.
- **Manager table: two-line access column (V0.8.13, ADJ-048)** — the 访问地址 column now stacks 「端口 · 本机」 on line 1 and the path alias on line 2; the column is only as wide as the wider of the two, and the freed space goes to the name column (cap 220→440px).
- **Manager locks are kernel-enforced (V0.8.14, issue #32)** — the manager single-instance and start locks now use kernel file locks (flock): dead PIDs and PIDs reused by unrelated processes no longer block startup (empty/corrupt records are briefly watched and self-heal by inode age), and no signal is ever sent to an unrelated process; lock files are never unlinked, and a live holder can no longer be stolen by file age. A lock conflict is classified by health at the service entry: a healthy same-workspace duplicate exits 0, while unhealthy holders or lock I/O failures exit non-zero with the holder PID, lock path, and log location (no more silent offline). Handover from ≤V0.8.13 processes uses a protocol marker plus a bounded wait that ends when the old flow unlinks the lock path (not when its process exits — a released lock is never falsely reported as held); an empty record from the old version's create-before-write window is watched, never pre-claimed (the old version would clobber the record and unlink the new holder's path), and is claimed once its inode age reaches the old 60-second staleness window. The lock layer itself never terminates processes; stopping an old-version manager is left to the coordinated restart (`lwa update`) or `lwa manager off`.
- **Recovery guidance keeps autostart; scoped enable/disable (V0.8.14)** — update failure hints now lead with retrying `lwa update` (supervisor-coordinated restart preserves autostart); if `off`→`on` is used, the hint states it disables autostart and how to restore exactly that service: `lwa autostart enable|disable --service manager|daemon|gateway` touches only the target unit and keeps every other service's original state (a bare `lwa autostart enable` enables all installed units). `lwa update`'s waitReady step now also waits for the manager when it was actually restarted, with workspace-scoped readiness.
- **Redundant cleanup protects `buildHooks` / `preStart` (V0.8.14)** — instances carrying their own build hooks or pre-start commands join the config-loss guard alongside path alias / `buildEnv`: batch purge skips them by default with concrete reasons, `--allow-config-loss` still overrides, and the preview and the in-lock recheck share the same policy.
- **Agent collaboration foundation (V0.8.15, AGC M0+M1)** — discovery endpoints `/llms.txt` / `/agent-info.json` / `/agent-guide` let AI assistants find this LWA; strict request/response contracts with 13 tool metadata specs serve as the single source for OpenAPI and MCP schemas; agent endpoints authenticate the local owner only (loopback + Bearer/X-LWA-Token) and validate `allowedSourceRoots` deny-by-default; registry schema v3 adds `workspace_meta` / `agent_plans` / `agent_operations` with an idempotent DAO. See [docs/agent-guide.md](docs/agent-guide.md).
- **Complex-deployment hardening (V0.8.16, issues #33–#35)** — `lwa doctor` gains `service_version_drift` (flags running services left on older code; align with `lwa services restart`, a coordinated manager/daemon/gateway restart that keeps autostart and pulls no code, reporting restarted / skipped / circuit-blocked per service). Container builds get an apt source chain symmetric with pip (`aptFallbacks` / `aptRetries` / `aptTimeout`; the official source snapshot is written once and every candidate switches from that immutable original) and a manifest `systemDeps` layer rendered **before** `COPY current/` with a BuildKit apt cache mount (`lwa configure --system-deps / --build-hook`, rejected on Node/Alpine images); `buildHooks` render verbatim — move system packages to `systemDeps`. A reusable pip dependency layer is split only when the install command is exactly `pip install .` (extras, extra flags, or compound commands fall back to a full copy that preserves the original command, BUG-660). Build failures are classified into `lastError` (network / memory / disk; bare `Killed` stays "uncertain"), probe failures attach container log tails and inspect evidence (`RestartCount` read from the container top level, unknown never faked), `desiredAlias` survives alias live-verification rollback and is auto-registered after a successful start/rebuild (`alias clear` also clears it; auto-restore re-reads the on-disk intent first), and daemon self-healing honors a build circuit breaker (≈3 consecutive failures → hourly backoff, ≈5 → manual intervention; `lwa start` / `lwa rebuild` resets it, visible in `lwa status` and the manager page).
- **Alias guard and container build closed-loop (V0.8.17, issues #36–#39)** — live alias verification now reads the HTML **served under the alias** (not the passed-in entry HTML) and empty probe sets can no longer stamp `aliasLiveVerified` (issue #37); the content guard runs on `restart` / `recover` and on **both** gateway-switch directions — switching back to builtin records `skipped` with the current time instead of keeping a stale `passed` (issue #38). docker-compose instances accept `buildEnv` / alias-follow (`lwa configure`): values flow into Dockerfile `ARG`/`ENV` and Compose `build.args` as **literals** (host env cannot override them, `$` is never re-expanded, and LWA management keys like `HOST_PORT` are rejected) (issue #39); Python containers auto-build `frontend|web|client` **only when the npm contract is complete** (build script + `package-lock.json`/`npm-shrinkwrap.json`; pnpm/yarn or missing pieces skip with a comment and defer to `buildHooks`), with the alias-follow migration hint using the same four-step order as host builds (BUG-684/685). Build-failure attribution no longer mistakes Docker Hub token fetches for apt errors and keeps the failing step's preceding lines (a `Killed` before `ERROR:` survives, BUG-677/682/687). Post-release hardening: service `bind_version` write-back race, circuit persistence under the instance lock, Node tarball URL follows each apt source attempt, doctor drift check no longer false-greens, `restart`/`recover` clears the build circuit (BUG-667~673). Agent M1 query/planning service layer and registry schema v4 (revision CAS) landed internally — HTTP/MCP entry points still pending (AGC W07–W09).
- **Agent M1 local-collaboration channel (V0.8.18, AGC W10–W14)** — the agent-only HTTP API `GET/POST /api/agent/v1/*` is live on the manager: capabilities snapshot, paginated instance list/detail, access URLs, redacted logs, `POST /plans` → `POST /deployments` (source snapshot + digest, 30-min TTL, no build at plan time), async operations (202 + operation; poll/cancel — queued cancels immediately, running interrupts only in the build phase), and async start/stop/restart/rebuild. Every write requires an `idempotencyKey` (same key + body → same operationId; same key + different body → 409), update-class calls carry `expectedRevision` optimistic locking, and a full queue returns 429 `busy` with `retryAfterMs`. Auth is stricter than the manager page: loopback only **and** a valid token via `Authorization: Bearer` / `X-LWA-Token` — no `?token=`. `lwa mcp --workspace <path>` exposes the same endpoints as a stdio MCP server (`pip install 'local-webpage-access[mcp]'`), `lwa agent connection-info` prints apiBase / workspaceId / contract version plus config self-checks (never the token), and a single-thread AgentWorker executes persistent operations with leases and crash recovery (in-flight work becomes `interrupted`; resubmitting with the same idempotency key is safe). Remote/LAN agents (M2) remain planned — see [docs/agent-guide.md](docs/agent-guide.md).
- **Host setup and autostart** — Docker/Caddy install scripts (China mirrors by default); launchd / systemd units; Ubuntu LTS, Debian Stable, Fedora, WSL2, macOS only.
- **HTTPS transport encryption (V0.9.0, CHK-352)** — set `gatewayTls: internal` (Caddy-only) to serve the alias entry and the manager over HTTPS via Caddy's internal CA: aliases on `https://<LAN-IP>:8443/`, manager on its own HTTPS origin `https://<LAN-IP>:9443/` (independent port, not a same-origin `/manager/` path) with the manager process itself collapsed to loopback-only. `lwa ca export` ships the root cert with its SHA-256 fingerprint and per-OS trust instructions — clients must actually trust the CA (never click through warnings). Plain-text entries close by default (`gatewayPlainPort` optional, not a security boundary), instance direct ports converge via `instanceBindHost` (builtin bind / Caddy site bind / Docker host-IP publish, IPv6-aware), URL synthesis + probes go https with full certificate validation, proxied auth parses X-Forwarded-For so LAN clients cannot impersonate loopback, and TLS failures fail loudly instead of falling back to HTTP. `lwa doctor` gains a `gateway_tls` check. Remote agents stay closed until M2 — see [docs/https.md](docs/https.md).
- **LAN drift reloads Caddy; static rebuild keeps its port (V0.9.1)** — when `gatewayTls: internal` and Caddy is already up, address refresh rewrites the main Caddyfile if port 8443 or 9443 is still bound to the old LAN IP or to loopback only (each port is judged on its own) and reloads; a stopped gateway is not started by that check. `lwa rebuild` of a static instance reuses the port its own live site is still listening on, so the host port no longer drifts. Folder sources can change the linked directory without a remove+reimport: `lwa import --from-dir <new> --update <id> --allow-source-change` (a TTY can confirm instead; `--dry-run` only prints the plan). A relative path that resolves to the recorded directory is the same source; a relative new directory is stored as its resolved absolute path.
- **Move the workspace, update LWA, talk to an agent** — `lwa workspace relocate`, `lwa update` (fast-forward only), 20 SKILL.md files for AI assistants, plus the M1 agent channel (`lwa mcp`, `/api/agent/v1/*`, V0.8.18).

## Installation

Python 3.13+, **fastapi ≥ 0.138.0**, **uvicorn ≥ 0.45.0**. Containers need **Docker ≥ 29.0.0** and **Docker Compose ≥ 2.40.2** (5.2+ recommended). Static sites prefer **Caddy ≥ 2.10.0**. Image baselines: `node:24-alpine`, `python:3.13-slim`. `lwa doctor` checks all of this.

```bash
pip install -e .              # from the repo root; also: python3 -m local_webpage_access
pip install -e ".[dev]"       # tests
lwa setup                     # detect host tools (no workspace required)
lwa init                      # then: lwa init --full --yes  for the Full capability loop
```

If joining the `docker` group still reports `sessionRefreshRequired`, re-login and run `lwa setup --full --resume`. See the [FAQ](docs/faq.md) and [operations playbook](docs/operations-playbook.md).

## Supported Platforms

- **Linux**: Ubuntu LTS (22.04 / 24.04 / 26.04), Debian Stable (12 / 13), and Fedora (43 / 44, rolling window of current + previous); x86_64 / arm64; kernel ≥ 5.15, glibc ≥ 2.35, systemd
- **WSL2**: same distros; WSL ≥ 2.1.5 with systemd as PID 1; keep the workspace on the Linux filesystem (autostart fail-closes on `/mnt/<drive>`). See [WSL2 host prep](docs/known-limitations.md)
- **macOS**: 14 Sonoma+
- **Not supported**: native Windows (use WSL2), WSL1, non-LTS Ubuntu, Debian sid/testing, Fedora Rawhide

`lwa doctor` always prints a platform-support section; `lwa doctor --json` works before `init`.

## Quick Start

```bash
lwa setup
lwa init
lwa import ./inbox/my-site.zip --name my-site
lwa start my-site
lwa status
```

That is the whole happy path. Folder import: `lwa import --from-dir /abs/path`; GitHub import: `lwa import --from-git https://github.com/<owner>/<repo>`. Optional next steps (`manager on`, `daemon on`, `gateway on`, `autostart install`, `doctor`) are in the [command reference](#command-reference).

## Command Reference

Use `lwa <command> --help` for flags. Global `-v` turns on DEBUG logs.

### Install and workspace

| Command | Description |
| --- | --- |
| `lwa setup [--default\|--full] [--yes] [--resume] [--script] [--json] [--autostart] [--with-caddy]` | Detect host tools; `--full` installs and runs the capability loop |
| `lwa init [-w DIR] [--force] [--default\|--full] [--yes]` | Create workspace (dirs / config / registry / skills); `--full` writes `profile: full` only when the loop is ready |
| `lwa update` | Update LWA itself (fetch → fast-forward → pip). Refuses dirty, diverged, detached, or shallow trees |
| `lwa workspace relocate <NEW> [--dry-run] [--yes] [--resume\|--verify\|--rollback]` | Same-volume atomic move. See [workspace-rename.md](docs/workspace-rename.md) |
| `lwa version` | Print version |

### Import

| Command | Description |
| --- | --- |
| `lwa import <zip> [-n NAME] [--path-alias SLUG] [--update ID]` | Import a zip; `--update` upgrades in place (keeps id / ports / data / alias) |
| `lwa import --from-dir <path> [-n NAME] [--path-alias SLUG] [--update ID] [--allow-source-change]` | Import or update from a local folder (read-only copy). `--update` must match the linked dir, or pass `--allow-source-change` to retarget it while keeping id / port / alias / data. A relative path is resolved first |
| `lwa import --from-git <URL> [--ref REF] [--subdir DIR] [-n NAME] [--path-alias SLUG] [--update ID]` | Import or update from a GitHub repo (github.com only; one-shot shallow clone into temp staging, then same zip pipeline; `--update` probes via `git ls-remote` and no-ops when OID unchanged) |
| `lwa alias set <ID> <slug>` / `lwa alias clear <ID>` | Path alias (needs Caddy; compatibility-checked) |
| `lwa scan [ID]` | Rescan `pending` instances (or one id) |

### Instance lifecycle

| Command | Description |
| --- | --- |
| `lwa start` / `stop` / `restart` `<ID>` | Start, stop, or restart (containers reuse the registered port) |
| `lwa recover <ID>` | One-shot recovery (pulls Caddy up if needed, then restart) |
| `lwa rebuild [--sync] <ID>` | Force-rebuild through the build queue; `--sync` refreshes folder/git sources first (stale sources are detected and warned) |
| `lwa cancel-build <ID>` | Cancel a queued or running build (keeps caches / images / data) |
| `lwa configure <ID> [--build-env KEY=VALUE] [--clear-build-env] [--follow-alias-base] [--acknowledge-redundancy] [--system-deps PKG] [--clear-system-deps] [--build-hook CMD] [--clear-build-hooks]` | Show or change per-instance settings: `buildEnv`, alias-base follow, intentional duplicate, container `systemDeps` (apt fallback chain before `COPY current/`), and `buildHooks` (each list flag is a whole-map replace, repeatable); apply with `lwa rebuild` |
| `lwa remove <ID> [--purge] [--force]` | Remove instance; `--purge` deletes disk (non-empty `data/` needs `--force`) |
| `lwa remove --redundant [--purge] [--allow-config-loss]` | Drop duplicate zips, keep the earliest; skips curated (alias / `buildEnv`) and running instances — `--allow-config-loss` opts in for curated ones |
| `lwa logs <ID> [-c CATEGORY] [-n TAIL]` | Logs: build / run / gateway / import / scan |
| `lwa status [ID]` / `lwa list` | Status of one or all; list ids and ports |
| `lwa stats [ID]` | Host + instance disk / image / container usage |
| `lwa pageviews [ID] [-n LIMIT]` | Pageview summary (same data as the manager) |

### Gateway, manager, and access

| Command | Description |
| --- | --- |
| `lwa gateway on` / `off` / `status` | Caddy master (`:8080` aliases, admin `:2019`) |
| `lwa gateway switch <caddy\|builtin> [--dry-run] [--json] [--no-review]` | Atomic backend switch with rollback |
| `lwa access refresh` | Recompute `lanUrl` from the current LAN IP |
| `lwa access review [--json] [--rebuild-if-needed]` | Probe declared URLs (blank alias pages, API-path mismatch) |
| `lwa manager on` / `off` / `status` / `start` / `logs` | Web UI (`:17800`); `start` is foreground |
| `lwa manager token [--json]` | Show token, issued-at, next rotation (168h) |
| `lwa daemon on` / `off` / `status` | Watch `inbox/`; import and self-heal |
| `lwa services restart [--no-reconcile]` | Coordinated restart of manager / daemon / gateway so running services load current code (keeps autostart; no pull / pip; V0.8.16, issue #33) |

### Agent (M1, local)

| Command | Description |
| --- | --- |
| `lwa agent connection-info --workspace <path> [--json]` | Agent onboarding: apiBase / workspaceId / contract version + config self-checks (never prints the token; V0.8.18) |
| `lwa ca export [--out PATH]` | Export the internal-CA root cert with SHA-256 fingerprint + per-OS trust instructions (gatewayTls=internal; V0.9.0) |
| `lwa mcp --workspace <path>` | stdio MCP adapter over `/api/agent/v1/*` (requires `pip install 'local-webpage-access[mcp]'`; V0.8.18) |

### Autostart

| Command | Description |
| --- | --- |
| `lwa autostart install [--with-caddy] [--no-enable] [--linger]` | Write launchd / systemd units (enabled by default) |
| `lwa autostart enable` / `disable` / `status` | Load, persistently disable, or inspect (`enable/disable --service <name>` scopes the op to one service) |
| `lwa autostart check [--json]` | Deep completeness check |
| `lwa autostart repair [--with-caddy]` | Fix stale paths and re-enable |
| `lwa autostart uninstall [--purge-linger]` | Stop units and delete files (workspace kept) |
| `lwa autostart doctor-hints` | Autostart-related doctor copy |

### Diagnostics

| Command | Description |
| --- | --- |
| `lwa doctor [ID] [--json] [--profile default\|full] [--access]` | Environment / instance checks; exit 1 on fail. `--access` reviews URLs |
| `lwa migrate-user <ID> [--root]` | Migrate a legacy instance to non-root (precheck first; `--root` opts back into root, issue #20) |
| `lwa capabilities [--json]` | Workspace CapabilityReport |
| `lwa registry check [--json]` | Scan registry sub-tables for orphan rows (read-only, BUG-473) |
| `lwa registry repair [-y]` | Delete orphan rows (destructive; interactive confirm, `--yes` required non-TTY) |

## Configuration

`lwa init` writes `local-web.yml`. Important keys:

```yaml
managerPort: 17800          # must not sit inside the port pool
managerHost: 0.0.0.0
portPool: { start: 18000, end: 19999 }
staticGateway: caddy        # caddy | builtin
staticGatewayPort: 8080     # alias entry (Caddy)
profile: default            # default | full
serviceUser: null           # identity pinned by Full setup
buildConcurrency: 1
defaultResourceLimits: { memory: 512m, cpus: "0.75" }
buildMirrors:
  enabled: true             # false → official sources everywhere
  preset: china             # china | none
  # pip source chain (V0.8.9, issue #18): primary + || fallbacks, per-source retries/timeout.
  # pipFallbacks: []        # empty list → primary only
  # pipRetries: 3
  # pipTimeout: 60
  # apt source chain (V0.8.16, issues #34/#35): systemDeps layers; aliyun → tuna → debian.org.
  # aptFallbacks: []        # empty list → primary only
  # aptRetries: 2
  # aptTimeout: 30
lanIpStrategy: auto         # auto | manual
manualLanIp: null
logLevel: INFO
```

## Workspace Layout

```
<workspace>/
├─ local-web.yml            # config
├─ inbox/                   # drop zips here (processed/ / failed/)
├─ apps/<id>/               # current/, public/, data/, docker/, logs/, local-web.json
├─ registry/                # local-web.db + build-locks.db
├─ static-gateway/          # sites/ + aliases/
├─ run/                     # pid, token, pageviews, capability snapshots
├─ logs/                    # lwa.log, manager.log, daemon.log, gateway.log
├─ templates/  manager/  skills/
```

## Web Manager

```bash
lwa manager on          # http://127.0.0.1:17800/  — token printed once; loopback reads need none
```

Instance list, logs, resources, start/stop/recover, aliases, pageviews, pending queue, port pool. LAN clients need the current token (`lwa manager token`). Native “choose folder” is loopback-only. Full API: [docs/manager-page.md](docs/manager-page.md).

## Auto-import Daemon

`lwa daemon on` watches `inbox/`, imports zips, and starts lightweight instances it can determine. Every 60s it reconciles `desired=running` processes that died. It will not auto-correct containers when observation fails or Full capabilities are not ready.

## Development and Testing

```bash
pip install -e ".[dev]"
python3 -m pytest           # no real Docker required
```

Code: `src/local_webpage_access/`. Tests: `tests/` (fake runtimes; set `LWA_RUN_DOCKER_TESTS=1` for real Docker — [testing.md](docs/testing.md)). Fixtures in `tests/fixtures/`.

## Documentation

| Doc | Contents |
| --- | --- |
| [docs/faq.md](docs/faq.md) | Troubleshooting (symptom → log) |
| [docs/operations-playbook.md](docs/operations-playbook.md) | Day-2 ops: setup, logs, gateway, inbox, Caddy |
| [docs/manager-page.md](docs/manager-page.md) | Manager API and auth |
| [docs/agent-guide.md](docs/agent-guide.md) | Agent/LLM onboarding: auth, input limits, safety rules |
| [docs/autostart.md](docs/autostart.md) | launchd / systemd |
| [docs/runtime-workspace.md](docs/runtime-workspace.md) | Directories, ports, resource tiers |
| [docs/workspace-rename.md](docs/workspace-rename.md) | Relocate handbook |
| [docs/security-boundary.md](docs/security-boundary.md) | Default protections |
| [docs/known-limitations.md](docs/known-limitations.md) | Limits + WSL2 host prep |
| [docs/testing.md](docs/testing.md) | How tests run |
| [docs/release-checklist.md](docs/release-checklist.md) / [acceptance-checklist.md](docs/acceptance-checklist.md) | Release / E2E |
| [skills/README.md](src/local_webpage_access/skills/README.md) | 20 LLM skills |

## Roadmap

Phases 0–7 (CLI → import → static → containers → lifecycle → manager/daemon → skills/security/doctor → tests/release) are **done**. Maintainer log: `task-list.md`.

## License

MIT

---

# 中文文档

[English](#overview) · **中文**

## 简介

面向局域网小主机的**本地网页部署基座**：导入 zip（或本机文件夹），自动识别运行形态、分配端口、给出局域网地址。面向 4G/8G 机器，目标是**导入即用**。

- 纯静态 HTML → 共享静态网关（Caddy 优先，内置 `http.server` 兜底）
- 纯前端 SPA（Vite / React / Vue / Svelte 等）→ 自动 `npm install` + `build` 后托管产物
- Node / Python 后端、含 SQLite 的全栈 → 生成 Dockerfile + Compose，容器运行

CLI、管理页、inbox 自动导入、导入期安全检查、`lwa doctor` 均已在 V1。细节在 [docs/](docs/faq.md)，本页只做入口。

![LWA 管理页](docs/images/lwa-manager.png)

## 特性

- **zip、本机文件夹、或丢进 `inbox/`** — zip-slip 防护、内容指纹；只读复制进工作区，禁止在你的源码树里就地运行。
- **自动识别运行形态** — `static` / Node / Python，是否带 SQLite；认不出的标 `pending`，可再 `scan`。
- **端口池 + 可选路径别名** — `lanUrl` 稳定；Caddy 下 `/{slug}/`，SPA 绝对路径会打坏时直接拒绝。 别名入口为后端注入 `X-Real-IP`。已验证别名在启动后活验证失败时保留、不再被静默清除，未命中的别名路由返回 404 而非空 200（V0.8.9，issue #21）。
- **静态与容器托管** — 静态走 Caddy 或内置服务；应用生成非 root Dockerfile/Compose（UID/GID 对齐宿主 `data/` 属主；旧实例经 `lwa migrate-user` 显式迁移，issue #20；镜像内非挂载目录在 `USER` 前 `chown`，issue #22），SQLite 挂 `data/`。 支持 manifest 声明式 `buildHooks` / `preStart` 构建钩子（issue #7），且 `scan` / `import --update` / `rebuild --sync` 重建后不再被清空（issue #23）。 pip 层按源链重试/超时并 `||` 切源（默认阿里 → 官方 PyPI → 腾讯；`pipFallbacks` / `pipRetries` / `pipTimeout` 可配，issue #18）。
- **小主机上的生命周期** — start / stop / recover / rebuild / cancel-build；默认构建并发 1；端口不漂移。 `rebuild` 检出关联 folder/git 源码漂移时警告（`--sync` 先同步再重建；doctor `source_freshness` 批量审计）。
- **管理页 + inbox 守护进程** — `:17800` 的 Vue 界面（本机读免 token，局域网用自动轮换的 token）。daemon 导入 zip 并拉起掉线的轻量实例。
- **危险镜像不会默写出** — 生成的 Compose/Dockerfile 写出前审计；zip 穿越 / 符号链接 / 炸弹拒绝导入。
- **`lwa doctor` 不假绿** — Python / Docker / Compose / 端口 / 磁盘，以及服务是否在跑、自启是否装好；服务启动失败附 `lastStartError` 原因与「连续 3 次/24h 熔断自动拉起」（V0.8.9，IMP-064），`version_freshness` 复用 `lwa update --check` 加 24h 缓存提示版本滞后（IMP-062）。 `lwa list` / `status` 直接展示兼容性预检发现（IMP-056 C.01–C.03）。未 `init` 也可用 `--json`。
- **Gateway 启动保持单 master（V0.8.10，issue #26）** — Caddy 启动、停止与自愈路径共用可重入的进程/线程锁；update 额外统计 admin 监听者，发现重复 master 时明确失败，不再健康假绿。
- **Fedora 纳入正式支持矩阵（V0.8.11）** — 平台门禁、Docker 安装脚本（直写 dnf 仓库，不依赖 config-manager 插件）与 Caddy 安装脚本（dnf 包 + GitHub 二进制兜底）覆盖 Fedora 43/44（「当前 + 前一稳定版」滚动窗口）；Rawhide 与窗口外版本拒绝。
- **`lwa update` 对 venv 缺失 pip 自愈，不再死循环（V0.8.11，issue #27）** — 快进**之前**预检 pip 可用性，缺失时零变更快速失败；快进后 pip 失败时恢复链第一步改为 `ensurepip --upgrade`，不再指引「重跑 lwa update / pip install」——两者都依赖被缺失的 pip 本身。
- **安全加固批次（V0.8.11）** — npm monorepo 包名经 shell 转义后再进入构建命令；新端口分配改 bind 探测（bind-only 占用不再被误分配）；`extraVolumes` 结构化校验并以带引号 YAML 标量渲染；审计补 `cap_add: ALL` 与下载-解释执行链（`curl && sh`、`wget | python` 等），文档明示审计是高危模式门禁而非沙箱。
- **源类型原地切换（V0.8.12，issue #28）** — `lwa import --from-dir/--from-git --update <id>` 可把 zip 实例原地切换为 folder/git 源：id、hostPort、路径别名、`data/`、desiredState 全部保留，不再需要 remove+reimport 丢数据；内容完全一致时同样完成身份切换。
- **Docker 晚就绪快速收敛（V0.8.12，issue #29）** — Docker 能力 unavailable（如 macOS 重启后 Docker Desktop 晚起）时 daemon/manager 探针改 10s→20s→40s 短退避（封顶 60s）而非 300s 长周期；`setup --full --resume` 主动触发 manager 即时重探并等 daemon 收敛，不再复读同一句建议；`overall=ready` 后清掉残留 action 文案；退避指数封顶，长期不可用也不会溢出杀死后台线程。
- **实例级构建环境变量（V0.8.13，DEV-132）** — manifest 顶层 `buildEnv`（如 `{"VITE_BASE": "/<别名>/"}`）在安装/构建命令执行时以 `{**os.environ, **buildEnv}` 注入，且与 `buildHooks` / `preStart` 同属重扫保留清单，`scan` / `import --update` / `rebuild --sync` 重建后不再被静默清空（别名站更新后白屏的根因）。Vite 实例被路径别名守卫拒绝时，错误文案直接附上 LWA 侧解法：`lwa configure <id> --build-env VITE_BASE=/<别名>/`（buildEnv 整组替换，其他变量需一并传入；要求 vite.config 读取 `process.env.VITE_BASE`）、或 `entry.build` 改 `--base`（仅适用于 build 脚本直接调用 Vite；重扫时不保留）后 `lwa rebuild`。若已开启 `--follow-alias-base`，手动 `VITE_BASE` 会被当前别名覆盖——须先 `--no-follow-alias-base` 关闭跟随，完成目标 base 构建与别名变更后再重新开启（BUG-640，拒绝提示中的四步指引已按此顺序）。
- **冗余清理不再误删带独立配置的实例（V0.8.13，issue #31）** — 批量删除冗余默认跳过携带独立配置（路径别名 / buildEnv）的目标并逐一报 skipped；确要删除需 `lwa remove --redundant --allow-config-loss`（API 传 `allowConfigLoss=true`）显式覆盖。管理页「批量删除冗余」改为弹窗逐项列明处置（移除 / 将跳过 + 原因），并附琥珀竖条可见释义。
- **全新导入拒绝 `--dry-run`（V0.8.13，issue #30）** — 全新 zip/folder/git 导入带 `--dry-run` 时在打开工作区前即拒绝（退出码 2，提示改用 `--update`），堵住「预览」静默真实落盘的缺口；`--update` 路径保持无损预览语义。
- **`lwa configure`：实例级配置（V0.8.13）** — 新命令（及 `PATCH /api/instances/{id}/settings`）查看、整组替换或清空 `buildEnv`；`--follow-alias-base` 每次构建按当前路径别名推导 `VITE_BASE`（无别名为 `/`，优先于手动值——项目须读取该变量，`lwa rebuild` 生效）；`--acknowledge-redundancy` 标记有意保留的重复实例并退出冗余候选（可撤销）。每个开关均有 `--no-` 反向形式。宿主前端注入安装/构建命令环境；docker-compose 容器写入 Dockerfile `ARG`/`ENV` 与 Compose `build.args`（issue #39）。
- **冗余删除加固（V0.8.13，issue #31）** — 运行/过渡态实例始终跳过（`allowConfigLoss` / `--force` 均不可覆盖），且删除前在实例锁内复查守卫，构建中途的配置编辑会中止删除；管理页弹窗补每实例别名与更新时间列、「有意保留」按钮及显式的允许损失配置勾选框，无可删目标时禁用确认。
- **管理页访问地址两行排版（V0.8.13，ADJ-048）** — 访问地址列第一行「端口 · 本机」、第二行路径别名，列宽只取两行中较宽者；省出的宽度让给名称列（上限 220→440px）。
- **manager 两把锁内核化（V0.8.14，issue #32）** — manager 单实例锁与启动锁改用内核文件锁（flock）：死 PID、被无关进程复用的 PID 都不再拒启（空/损坏记录改为限时观察、按 inode 年龄自愈认领），也绝不向无关进程发信号；锁文件永不删除，存活持有者不再被按文件年龄抢占。锁冲突由服务入口按健康分类：同工作区健康重复实例 exit 0，不健康持锁或锁 I/O 故障非零退出并给出持有 PID、锁路径与日志位置（不再静默离线）。对 ≤V0.8.13 旧版进程的交接用协议标记 + 有限等待，等待以旧版流程 unlink 锁路径为临界区结束标志（不是其进程退出——已释放的锁不再被误报占用）；旧版「已建文件、未写 PID」窗口留下的空记录只观察、绝不提前认领（旧版恢复后会覆写记录并 unlink 新版持锁路径），超时仍空按 inode 年龄达到旧版 60 秒陈旧阈值后才认领。锁层自身不终止任何进程，旧版 manager 的停止交给协调重启（`lwa update`）或 `lwa manager off`。
- **恢复指引保留自启；按服务启用/停用自启（V0.8.14）** — 更新失败指引优先建议重试 `lwa update`（监督器协调重启保留自启状态）；确需 off→on 时明示 off 会停用自启，并按服务恢复：`lwa autostart enable|disable --service manager|daemon|gateway` 只动目标单元、保留其他服务原状态（不带 `--service` 会启用全部已安装单元）。`lwa update` 的 waitReady 在 manager 实际被重启过时也纳入就绪等待，且就绪判定区分本工作区。
- **冗余清理保护 buildHooks / preStart（V0.8.14）** — 携带独立构建钩子或启动前命令的实例与路径别名 / buildEnv 同入配置保护：批量 purge 默认跳过并给出具体理由，`--allow-config-loss` 仍可显式覆盖；预览与删除锁内复核共用同一判定。
- **Agent 协作基座（V0.8.15，AGC M0+M1）** — 发现入口 `/llms.txt` / `/agent-info.json` / `/agent-guide` 供 AI 助手自动找到本 LWA；严格请求/响应契约与 13 个工具元数据规格作为 OpenAPI 与 MCP schema 的共源；agent 端点仅认本机 owner（回环 + Bearer/X-LWA-Token），`allowedSourceRoots` 源根校验默认拒绝；registry schema v3 新增 `workspace_meta` / `agent_plans` / `agent_operations` 与幂等 DAO。见 [docs/agent-guide.md](docs/agent-guide.md)。
- **复杂实例部署加固（V0.8.16，issues #33–#35）** — `lwa doctor` 新增 `service_version_drift`（发现运行中服务仍跑旧代码；用 `lwa services restart` 协调重启 manager/daemon/gateway 对齐，保留自启、不拉代码，并按服务区分已重启/跳过/熔断）。容器构建获得与 pip 对称的 apt 源链（`aptFallbacks` / `aptRetries` / `aptTimeout`；官方源快照一次写入不可变，每个候选源都从原始源独立切换）与 manifest `systemDeps` 层——渲染在 `COPY current/` **之前**并带 BuildKit apt cache mount（`lwa configure --system-deps / --build-hook`，Node/Alpine 镜像拒绝）；`buildHooks` 原样渲染，系统包请迁 `systemDeps`。仅当安装命令精确为 `pip install .` 时才拆可复用依赖层（extras、附加参数或复合命令回退整包 COPY 并保留原命令，BUG-660）。构建失败分类写入 `lastError`（网络 / 内存 / 磁盘；单凭 `Killed` 标注「不确定」），探针失败附容器日志尾部与 inspect 证据（`RestartCount` 取容器顶层，未知不伪造），`desiredAlias` 在别名活验证回滚后保留并在 start/rebuild 成功后自动补登记（显式 `alias clear` 一并清空；自动恢复先重读磁盘意图），daemon 自愈遵循构建熔断（连续失败约 3 次小时级退避、约 5 次转人工；`lwa start` / `lwa rebuild` 清熔断，`lwa status` 与管理页可见）。
- **别名守卫与容器构建闭环（V0.8.17，issues #36–#39）** — 别名活验证改为读取**别名实际服务**的 HTML（而非传入的入口 HTML），空探针集不再盖 `aliasLiveVerified` 印章（issue #37）；内容守卫覆盖 `restart`/`recover` 与**双向**网关切换——切回 builtin 记 `skipped` + 本次时间，不再沿用旧 `passed`（issue #38）。docker-compose 容器实例接受 `buildEnv`/别名跟随（`lwa configure`）：值以**字面量**写入 Dockerfile `ARG`/`ENV` 与 Compose `build.args`（宿主同名环境不覆盖、`$` 不二次展开、`HOST_PORT` 等 LWA 管理键拒绝写入）（issue #39）；Python 容器仅在 **npm 契约完整**（build 脚本 + `package-lock.json`/`npm-shrinkwrap.json`）时自动构建 `frontend|web|client`，pnpm/yarn 或要素缺失则跳过并留注释、交给 `buildHooks`，别名跟随的迁移提示与宿主同为四步顺序（BUG-684/685）。构建失败归因不再把 Docker Hub token 拉取误判为 apt，并保留失败步骤此前的前文（`ERROR:` 之前的 `Killed` 不再丢失，BUG-677/682/687）。发布后加固：服务 `bind_version` 回写竞态、熔断持久化入实例锁、Node tarball URL 随 apt 源逐次切换、doctor 漂移检查不再假绿、`restart`/`recover` 清构建熔断（BUG-667~673）。Agent M1 查询/计划服务层与 registry schema v4（revision CAS）已入库——HTTP/MCP 入口仍待实施（AGC W07–W09）。
- **Agent M1 本机协作通道落地（V0.8.18，AGC W10–W14）** — Agent 专用 HTTP API `GET/POST /api/agent/v1/*` 随管理页上线：能力快照、实例分页列表/详情、访问地址、脱敏日志、`POST /plans` → `POST /deployments`（源码快照 + digest，TTL 30 分钟，计划阶段不构建）、异步操作（202 + operation，轮询/取消——排队中直接取消，运行中仅构建相位可中断）与异步 start/stop/restart/rebuild。所有写操作要求 `idempotencyKey`（同键同内容重试返回同一 operationId；同键不同内容 409），更新类调用带 `expectedRevision` 乐观锁，队列超限返回 429 `busy` + `retryAfterMs`。鉴权比管理页更严：仅本机回环 **且** 必须携带有效 token（`Authorization: Bearer` / `X-LWA-Token`，无 `?token=` 通道）。新增 `lwa mcp --workspace <path>` stdio MCP 适配器（端点一一对应，`pip install 'local-webpage-access[mcp]'`）与 `lwa agent connection-info` 接入引导（输出 apiBase / workspaceId / 契约版本与配置自检，绝不输出 token）；单线程 AgentWorker 以租约执行持久操作并支持崩溃恢复（未完成任务标记 `interrupted`，同幂等键重提安全）。远程/LAN Agent（M2）仍处规划——见 [docs/agent-guide.md](docs/agent-guide.md)。
- **宿主机装配与自启** — Docker/Caddy 安装脚本（默认国内源）；launchd / systemd；仅 Ubuntu LTS、Debian Stable、Fedora、WSL2、macOS。
- **HTTPS 传输加密（V0.9.0，CHK-352）** — `gatewayTls: internal`（仅 Caddy）用 Caddy 内嵌 CA 打开传输加密：别名入口 `https://<LAN-IP>:8443/`，管理面独立 HTTPS origin `https://<LAN-IP>:9443/`（独立端口而非同源 `/manager/` 路径），manager 进程收敛为仅回环监听。`lwa ca export` 导出根证书 + SHA-256 指纹 + 各平台信任指引——客户端必须真正信任（禁止点穿告警）。明文入口默认关闭（`gatewayPlainPort` 可选保留，非安全边界），实例直连口经 `instanceBindHost` 收敛（builtin/Caddy 站点/Docker host-IP 发布，覆盖 IPv6），URL 合成与探活走 https 且完整证书验证，反代鉴权解析 X-Forwarded-For 防 LAN 冒充回环，TLS 失败显式报错绝不回退明文；`lwa doctor` 新增 gateway_tls 检查。远程 Agent 仍待 M2——见 [docs/https.md](docs/https.md)。
- **LAN 漂移重载 Caddy；静态 rebuild 保留端口（V0.9.1）** — `gatewayTls: internal` 且 Caddy 已在线时，地址刷新若发现主 Caddyfile 的 8443 或 9443 仍绑旧 LAN IP、或只剩回环，会按端口分别重写并 reload；关掉的网关不会被这次检查拉起。静态实例 `lwa rebuild` 在本实例站点仍监听原端口时复用该端口，不再漂移。folder 源更换关联目录不必删实例重导：`lwa import --from-dir <新目录> --update <id> --allow-source-change`（交互终端可确认；`--dry-run` 只展示计划）。相对路径若解析后就是已记录目录，视为同一源；换到相对新目录时写入解析后的绝对路径。
- **搬工作区、升级 LWA、交给 Agent** — `lwa workspace relocate`、`lwa update`（只允许快进）、20 份 SKILL.md，以及 M1 Agent 通道（`lwa mcp`、`/api/agent/v1/*`，V0.8.18）。

## 安装

需要 Python 3.13+、**fastapi ≥ 0.138.0**、**uvicorn ≥ 0.45.0**。容器需要 **Docker ≥ 29.0.0**、**Docker Compose ≥ 2.40.2**（推荐 5.2+）。静态站优先 **Caddy ≥ 2.10.0**。基线镜像：`node:24-alpine`、`python:3.13-slim`。用 `lwa doctor` 逐项核对。

```bash
pip install -e .              # 在仓库根目录；也可用 python3 -m local_webpage_access
pip install -e ".[dev]"       # 跑测试
lwa setup                     # 检测宿主机工具（无需工作区）
lwa init                      # Full 能力闭环：lwa init --full --yes
```

加入 `docker` 组后若仍报 `sessionRefreshRequired`，重登再执行 `lwa setup --full --resume`。见 [排障](docs/faq.md) 与 [运维手册](docs/operations-playbook.md)。

## 支持的平台

- **Linux**：Ubuntu LTS（22.04 / 24.04 / 26.04）、Debian Stable（12 / 13）与 Fedora（43 / 44，按「当前 + 前一版」滚动窗口）；x86_64 / arm64；kernel ≥ 5.15、glibc ≥ 2.35、systemd
- **WSL2**：同上发行版；WSL ≥ 2.1.5 且 systemd 为 PID 1；工作区放 Linux 文件系统（autostart 对 `/mnt/<drive>` 直接失败）。见 [WSL2 宿主准备](docs/known-limitations.md)
- **macOS**：14 Sonoma+
- **不支持**：Windows 原生（请用 WSL2）、WSL1、Ubuntu 非 LTS、Debian sid/testing、Fedora Rawhide

`lwa doctor` 末尾有「平台支持」段；未初始化时用 `lwa doctor --json`。

## 快速开始

```bash
lwa setup
lwa init
lwa import ./inbox/my-site.zip --name my-site
lwa start my-site
lwa status
```

这就是主路径。文件夹导入：`lwa import --from-dir /abs/path`；GitHub 导入：`lwa import --from-git https://github.com/<owner>/<repo>`。管理页、daemon、网关、自启、`doctor` 等可选步骤见 [命令参考](#命令参考)。

## 命令参考

细节用 `lwa <command> --help`。全局 `-v` 打开 DEBUG。

### 安装与工作区

| 命令 | 说明 |
| --- | --- |
| `lwa setup [--default\|--full] [--yes] [--resume] [--script] [--json] [--autostart] [--with-caddy]` | 检测宿主机工具；`--full` 安装并做能力闭环 |
| `lwa init [-w DIR] [--force] [--default\|--full] [--yes]` | 初始化工作区；`--full` 仅在闭环 ready 后写入 `profile: full` |
| `lwa update` | 升级 LWA 自身（fetch → 快进 → pip）。工作区脏、分叉、detached、浅克隆会拒绝 |
| `lwa workspace relocate <NEW> [--dry-run] [--yes] [--resume\|--verify\|--rollback]` | 同卷原子迁移，见 [workspace-rename.md](docs/workspace-rename.md) |
| `lwa version` | 版本号 |

### 导入

| 命令 | 说明 |
| --- | --- |
| `lwa import <zip> [-n NAME] [--path-alias SLUG] [--update ID]` | 导入 zip；`--update` 原地升级（保留 id / 端口 / data / 别名） |
| `lwa import --from-dir <路径> [-n NAME] [--path-alias SLUG] [--update ID] [--allow-source-change]` | 本机文件夹导入/更新（只读复制）。`--update` 须与关联目录一致，或加 `--allow-source-change` 换目录并保留 id / 端口 / 别名 / data。相对路径会先解析 |
| `lwa import --from-git <URL> [--ref REF] [--subdir DIR] [-n NAME] [--path-alias SLUG] [--update ID]` | 从 GitHub 仓库导入/更新（仅 github.com；一次性浅克隆到临时暂存后走同一 zip 管线；`--update` 经 `git ls-remote` 探测，OID 未变则不做任何操作） |
| `lwa alias set <ID> <slug>` / `lwa alias clear <ID>` | 路径别名（需 Caddy；不兼容则拒绝） |
| `lwa scan [ID]` | 重扫 `pending`（或指定实例） |

### 实例生命周期

| 命令 | 说明 |
| --- | --- |
| `lwa start` / `stop` / `restart` `<ID>` | 启动、停止、重启（容器复用已登记端口） |
| `lwa recover <ID>` | 一键恢复（必要时先拉起 Caddy） |
| `lwa rebuild [--sync] <ID>` | 经构建队列强制重建；`--sync` 先同步 folder/git 源码（漂移时自动警告） |
| `lwa cancel-build <ID>` | 取消排队/进行中的构建（不删缓存/镜像/数据） |
| `lwa configure <ID> [--build-env KEY=VALUE] [--clear-build-env] [--follow-alias-base] [--acknowledge-redundancy] [--system-deps PKG] [--clear-system-deps] [--build-hook CMD] [--clear-build-hooks]` | 查看/修改实例配置：`buildEnv`、别名 base 跟随、有意保留重复实例、容器 `systemDeps`（COPY 前走 apt 切源链）与 `buildHooks`（列表选项均为整组替换、可重复）；`lwa rebuild` 生效 |
| `lwa remove <ID> [--purge] [--force]` | 移除实例；`--purge` 删磁盘（非空 `data/` 需 `--force`） |
| `lwa remove --redundant [--purge] [--allow-config-loss]` | 按 zip 指纹去重，保留最早者；带独立配置（别名 / `buildEnv`）或运行中的实例默认跳过，`--allow-config-loss` 显式覆盖 |
| `lwa logs <ID> [-c CATEGORY] [-n TAIL]` | 日志：build / run / gateway / import / scan |
| `lwa status [ID]` / `lwa list` | 状态；列出 id 与端口 |
| `lwa stats [ID]` | 整机 + 实例磁盘/镜像/容器占用 |
| `lwa pageviews [ID] [-n LIMIT]` | 浏览量（与管理页同一数据） |

### 网关、管理页与访问

| 命令 | 说明 |
| --- | --- |
| `lwa gateway on` / `off` / `status` | Caddy master（`:8080` 别名，admin `:2019`） |
| `lwa gateway switch <caddy\|builtin> [--dry-run] [--json] [--no-review]` | 原子切换后端，失败回滚 |
| `lwa access refresh` | 按当前 LAN IP 重算 `lanUrl` |
| `lwa access review [--json] [--rebuild-if-needed]` | 探活声明的 URL（别名白屏、API 路径错位） |
| `lwa manager on` / `off` / `status` / `start` / `logs` | 管理页（`:17800`）；`start` 为前台 |
| `lwa manager token [--json]` | 查看 token、颁发时间、下次轮换（168h） |
| `lwa daemon on` / `off` / `status` | 监听 `inbox/`，导入并自愈 |
| `lwa services restart [--no-reconcile]` | 协调重启 manager / daemon / gateway，使运行中服务加载当前代码（保留自启；不拉源码 / 不重装 pip；V0.8.16，issue #33） |

### Agent（M1 本机）

| 命令 | 说明 |
| --- | --- |
| `lwa agent connection-info --workspace <path> [--json]` | Agent 接入引导：apiBase / workspaceId / 契约版本 + 配置自检（绝不输出 token；V0.8.18） |
| `lwa ca export [--out PATH]` | 导出内部 CA 根证书 + SHA-256 指纹 + 各平台信任指引（gatewayTls=internal；V0.9.0） |
| `lwa mcp --workspace <path>` | 基于 `/api/agent/v1/*` 的 stdio MCP 适配器（需 `pip install 'local-webpage-access[mcp]'`；V0.8.18） |

### 自启

| 命令 | 说明 |
| --- | --- |
| `lwa autostart install [--with-caddy] [--no-enable] [--linger]` | 写入 launchd / systemd 单元（默认启用） |
| `lwa autostart enable` / `disable` / `status` | 加载、持久停用、查看 |
| `lwa autostart check [--json]` | 完备性深检 |
| `lwa autostart repair [--with-caddy]` | 修复失效路径并重新启用 |
| `lwa autostart uninstall [--purge-linger]` | 停单元、删文件（工作区保留） |
| `lwa autostart doctor-hints` | 自启相关 doctor 文案 |

### 诊断

| 命令 | 说明 |
| --- | --- |
| `lwa doctor [ID] [--json] [--profile default\|full] [--access]` | 环境/实例检查；有 fail 则退出码 1。`--access` 复核 URL |
| `lwa migrate-user <ID> [--root]` | 旧实例显式迁移到非 root 运行（先过写权限预检；`--root` 反向选择 root 兼容，issue #20） |
| `lwa capabilities [--json]` | 工作区 CapabilityReport |
| `lwa registry check [--json]` | 只读扫描 registry 子表孤儿行（BUG-473） |
| `lwa registry repair [-y]` | 删除孤儿行（破坏性；默认交互确认，非 TTY 须 `--yes`） |

## 配置

`lwa init` 生成 `local-web.yml`，关键字段：

```yaml
managerPort: 17800          # 不能落在端口池内
managerHost: 0.0.0.0
portPool: { start: 18000, end: 19999 }
staticGateway: caddy        # caddy | builtin
staticGatewayPort: 8080     # 别名入口（Caddy）
profile: default            # default | full
serviceUser: null           # Full 固化的运行身份
buildConcurrency: 1
defaultResourceLimits: { memory: 512m, cpus: "0.75" }
buildMirrors:
  enabled: true             # false → official sources everywhere
  preset: china             # china | none
  # pip source chain (V0.8.9, issue #18): primary + || fallbacks, per-source retries/timeout.
  # pipFallbacks: []        # empty list → primary only
  # pipRetries: 3
  # pipTimeout: 60
  # apt source chain (V0.8.16, issues #34/#35): systemDeps layers; aliyun → tuna → debian.org.
  # aptFallbacks: []        # empty list → primary only
  # aptRetries: 2
  # aptTimeout: 30
lanIpStrategy: auto         # auto | manual
manualLanIp: null
logLevel: INFO
```

## 工作区布局

```
<workspace>/
├─ local-web.yml            # 配置
├─ inbox/                   # 投放 zip（processed/ / failed/）
├─ apps/<id>/               # current/、public/、data/、docker/、logs/、local-web.json
├─ registry/                # local-web.db + build-locks.db
├─ static-gateway/          # sites/ + aliases/
├─ run/                     # pid、token、pageviews、能力快照
├─ logs/                    # lwa.log、manager.log、daemon.log、gateway.log
├─ templates/  manager/  skills/
```

## 管理页

```bash
lwa manager on          # http://127.0.0.1:17800/  — token 只打一次；本机读请求免 token
```

实例列表、日志、资源、启停/恢复、别名、浏览量、pending 队列、端口池。局域网访问需要当前 token（`lwa manager token`）。「选择文件夹」仅 loopback。API 见 [docs/manager-page.md](docs/manager-page.md)。

## 自动导入守护进程

`lwa daemon on` 监听 `inbox/`，导入 zip，并启动能确定的轻量实例。每 60s 调和 `desired=running` 但已掉线的进程。观测失败或 Full 能力未就绪时，**不会**自动纠正容器。

## 开发与测试

```bash
pip install -e ".[dev]"
python3 -m pytest           # 不依赖真实 Docker
```

代码在 `src/local_webpage_access/`，测试在 `tests/`（替身运行时；真实 Docker 设 `LWA_RUN_DOCKER_TESTS=1`，见 [testing.md](docs/testing.md)）。夹具在 `tests/fixtures/`。

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/faq.md](docs/faq.md) | 排障（症状 → 日志） |
| [docs/operations-playbook.md](docs/operations-playbook.md) | 日常运维：装配、日志、网关、inbox、Caddy |
| [docs/manager-page.md](docs/manager-page.md) | 管理页 API 与鉴权 |
| [docs/agent-guide.md](docs/agent-guide.md) | Agent/LLM 接入指南：鉴权、输入限制、操作红线 |
| [docs/autostart.md](docs/autostart.md) | launchd / systemd |
| [docs/runtime-workspace.md](docs/runtime-workspace.md) | 目录、端口、资源档位 |
| [docs/workspace-rename.md](docs/workspace-rename.md) | 迁移手册 |
| [docs/security-boundary.md](docs/security-boundary.md) | 默认保护 |
| [docs/known-limitations.md](docs/known-limitations.md) | 已知限制 + WSL2 宿主准备 |
| [docs/testing.md](docs/testing.md) | 如何跑测试 |
| [docs/release-checklist.md](docs/release-checklist.md) / [acceptance-checklist.md](docs/acceptance-checklist.md) | 发布 / 端到端 |
| [skills/README.md](src/local_webpage_access/skills/README.md) | 20 个 LLM Skill |

## 路线图

Phase 0–7（CLI → 导入 → 静态 → 容器 → 生命周期 → 管理页/daemon → Skills/安全/doctor → 测试/发布）均**已完成**。维护台账见 `task-list.md`。

## 许可

MIT
