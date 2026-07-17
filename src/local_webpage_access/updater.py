"""``lwa update`` 工作区热重载（IMP-008）。

把"开发者 ``git pull`` / 改代码后刷新运行态"收敛成一条命令：

::

    识别上下文 → 预检（dry-run）→ pip install -e . → 同步 skills/templates
    → 配置缺省字段补齐 → 重启 manager/daemon → 可选重启实例 → lwa doctor

设计约束（见 ``docs/plan/待改进功能点记录-20260706.md`` IMP-008）：

* 每步**独立失败不中断后续**，最终退出码反映是否存在失败；
* pip 成功但 manager 重启失败**不回滚** Python 包；提示查 ``run/manager.json``；
* 重启 manager/daemon **仅在原本运行时**才 stop→start，原本 stopped 不自动开启；
* 实例默认**不动**；``--restart-instances`` 时跳过 building/queued/pending；
* ``--dry-run`` 不产生任何文件、进程、registry 变更；
* ``--sync-templates`` 默认关闭（避免覆盖用户改过的模板）。
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from local_webpage_access.config import Config
from local_webpage_access.logging import get_logger
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry

log = get_logger("updater")

# 打包内置 skills / templates 目录（与 init_workspace 同源）
_BUNDLED_SKILLS = Path(__file__).parent / "skills"
_BUNDLED_TEMPLATES = Path(__file__).parent / "templates"

# 实例状态白名单：restart-instances 仅对这些状态的实例执行（跳过
# building/queued/pending，避免误中断长时构建或尚未就绪的实例）。
_RESTARTABLE_STATUSES = frozenset({"running", "stopped", "failed"})

# pip install 超时（大依赖网络慢，留足窗口）
_PIP_TIMEOUT = 300


# ---- 数据结构 --------------------------------------------------------------


@dataclass
class UpdateOptions:
    """``lwa update`` 的全部开关。"""

    dry_run: bool = False
    skip_pip: bool = False
    sync_skills: bool = True
    sync_templates: bool = False
    restart_manager: bool = True
    restart_daemon: bool = True
    restart_instances: bool = False
    run_doctor: bool = True
    repo: str | None = None  # 显式 --repo，覆盖自动识别


@dataclass
class StepResult:
    """单步执行结果。"""

    name: str
    status: str  # ok | failed | skipped | pending
    message: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "message": self.message, **self.extra}


@dataclass
class UpdateReport:
    """``lwa update`` 整体报告。"""

    workspace: str
    repo: str | None
    version_before: str
    version_after: str
    steps: list[StepResult] = field(default_factory=list)
    manager_url: str | None = None
    doctor_status: str | None = None

    @property
    def has_failures(self) -> bool:
        return any(s.status == "failed" for s in self.steps)

    def step(self, name: str) -> StepResult | None:
        for s in self.steps:
            if s.name == name:
                return s
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "repo": self.repo,
            "versionBefore": self.version_before,
            "versionAfter": self.version_after,
            "steps": [s.to_dict() for s in self.steps],
            "managerUrl": self.manager_url,
            "doctorStatus": self.doctor_status,
        }


# ---- 上下文识别 ------------------------------------------------------------


def locate_repo(explicit: str | None = None) -> Path | None:
    """识别 lwa 源码根（IMP-008.01）。

    优先级：``--repo`` 显式 > editable 安装路径（``src/local_webpage_access`` 上两级，
    存在 ``pyproject.toml``）> 当前 git 工作区根。三者都无法定位时返回 ``None``。
    """
    if explicit:
        p = Path(explicit).resolve()
        if p.is_dir() and (p / "pyproject.toml").is_file():
            return p
        # 给出明确错误而非静默降级，避免在错误目录跑 pip
        raise FileNotFoundError(
            f"--repo 指定的目录不是 lwa 源码根（缺少 pyproject.toml）：{p}"
        )

    # editable 安装路径
    here = Path(__file__).resolve().parent
    candidate = here.parent.parent
    if (candidate / "pyproject.toml").is_file():
        return candidate

    # git 根兜底
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode == 0:
            root = Path(result.stdout.strip())
            if root.is_dir() and (root / "pyproject.toml").is_file():
                return root
    except (OSError, subprocess.SubprocessError):
        pass
    return None


# ---- 单步动作 --------------------------------------------------------------


def run_pip_install(repo: Path) -> str:
    """在源码根执行 ``pip install -e .``，返回 stdout 摘要。

    抛 ``RuntimeError`` 让上层捕获为 step failed；不吞掉 pip 的原始错误。
    """
    log.info("pip install -e . （cwd=%s）", repo)
    result = subprocess.run(
        [sys_executable(), "-m", "pip", "install", "-e", "."],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=_PIP_TIMEOUT,
        check=False,
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-5:]
        raise RuntimeError(
            f"pip install -e . 失败（exit {result.returncode}）：\n"
            + "\n".join(tail)
        )
    # 成功：取最后几行（含 Successfully installed ...）
    lines = (result.stdout or "").strip().splitlines()
    return lines[-1] if lines else "pip install 完成"


def sys_executable() -> str:
    """当前 Python 解释器（抽出便于测试 mock）。"""
    import sys

    return sys.executable


def _sync_bundled(
    bundled: Path, dst_root: Path, *, force: bool
) -> tuple[list[str], list[str], list[str]]:
    """同步打包目录到工作区，返回 (added, updated, skipped)。

    * 新文件（dst 不存在）→ added；
    * 内容变化 → updated（仅 force=True 时覆盖；force=False 跳过）；
    * 内容相同 → skipped；
    * **不删除**用户自建的自定义文件（不在 bundled 中的）。
    """
    added: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    if not bundled.is_dir():
        return added, updated, skipped

    for src in bundled.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(bundled)
        dst = dst_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        rel_str = str(rel).replace("\\", "/")
        if not dst.exists():
            shutil.copy2(src, dst)
            added.append(rel_str)
            continue
        try:
            same = dst.read_bytes() == src.read_bytes()
        except OSError:
            same = False
        if same:
            skipped.append(rel_str)
        elif force:
            shutil.copy2(src, dst)
            updated.append(rel_str)
        else:
            skipped.append(rel_str)
    return added, updated, skipped


def sync_skills(ws: Workspace) -> tuple[list[str], list[str], list[str]]:
    """同步包内 skills/ → 工作区 skills/（force=True 覆盖陈旧副本）。"""
    return _sync_bundled(_BUNDLED_SKILLS, ws.skills, force=True)


def sync_templates(ws: Workspace) -> tuple[list[str], list[str], list[str]]:
    """同步 templates/（force=True；默认调用方不启用以保护用户改过的模板）。"""
    return _sync_bundled(_BUNDLED_TEMPLATES, ws.templates, force=True)


def _deep_merge_defaults(defaults: dict, existing: dict) -> dict:
    """深层合并：existing 的值优先；同为 dict 的键递归补齐 defaults 的子键。

    避免 ``{**defaults, **existing}`` 对 ``portPool``/``defaultResourceLimits``/
    ``staticRateLimit`` 等嵌套字段做整体覆盖——旧配置只写了部分子键时，缺失子键
    从 defaults 补齐而非丢失（已有的子键仍保留用户值）。
    """
    merged = {**defaults}
    for key, value in existing.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_defaults(merged[key], value)
        else:
            merged[key] = value
    return merged


def migrate_config_defaults(ws: Workspace, config: Config) -> tuple[list[str], bool]:
    """补齐 ``local-web.yml`` 缺失的顶层字段（IMP-008.02，非破坏性）。

    pydantic 加载时已用默认值填充缺失字段，但**文件本身**仍可能缺键——
    此函数把缺失的顶层键写回文件，使配置文件反映当前 schema。已有键**不改动**，
    原始文件先备份为 ``local-web.yml.bak``。

    返回 (缺失并补齐的键列表, 是否发生了写盘)。
    """
    config_path = ws.config_path
    if not config_path.is_file():
        return [], False

    try:
        raw = config_path.read_text(encoding="utf-8")
        existing = yaml.safe_load(raw) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("读取 %s 失败，跳过配置迁移：%s", config_path, exc)
        return [], False
    if not isinstance(existing, dict):
        return [], False

    # Config 模型的全部顶层字段 → 默认值
    defaults = Config().model_dump()
    missing = [k for k in defaults if k not in existing]
    if not missing:
        return [], False

    # 备份后写回：用 existing + 缺失键的默认值合并，保留用户已有键的值
    backup = config_path.with_suffix(".yml.bak")
    try:
        backup.write_text(raw, encoding="utf-8")
    except OSError as exc:
        log.warning("配置迁移备份失败，中止写回：%s", exc)
        return missing, False

    merged = _deep_merge_defaults(defaults, existing)  # existing 优先；嵌套字段深层补齐
    config_path.write_text(
        yaml.safe_dump(merged, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    log.info("配置迁移：补齐 %d 个缺失字段（备份 → %s）", len(missing), backup.name)
    return missing, True


def restart_manager(ws: Workspace, config: Config) -> dict[str, Any]:
    """幂等重启管理页：仅当原本 running 时 stop→start。

    原本 stopped 不自动开启（避免意外拉起用户故意关闭的服务）。
    返回 ``{"wasRunning": bool, "pid": int|None, "message": str}``。
    """
    from local_webpage_access.cli._common import coordinated_autostart_restart
    from local_webpage_access.manager_service import (
        is_running,
        start_manager,
        stop_manager,
    )

    was_running = is_running(ws, config)
    if not was_running:
        return {"wasRunning": False, "pid": None, "message": "管理页原本未运行，跳过重启"}
    # BUG-191：自启动在管时交监督器重启（单一进程），否则 stop 杀掉后被
    # KeepAlive/Restart 立即拉回、再与 start_manager 的 detached spawn 抢状态。
    note, _ok, managed = coordinated_autostart_restart(ws, "manager")
    if managed:
        return {
            "wasRunning": True,
            "pid": None,
            "message": note or "管理页已通过自启动重启",
        }
    # BUG-192：stop 失败不得报成重启成功（旧进程仍在跑）；抛错由 run_update 标 failed。
    if not stop_manager(ws):
        raise RuntimeError(
            "管理页停止失败（旧进程可能仍在运行），已跳过重启；"
            "可 `lwa manager off` 后重试 `lwa manager on`"
        )
    pid = start_manager(ws, config)
    return {"wasRunning": True, "pid": pid, "message": f"管理页已重启（pid={pid}）"}


def restart_daemon(ws: Workspace, config: Config) -> dict[str, Any]:
    """幂等重启 daemon：仅当原本 running 时 stop→start。

    原本 stopped 不自动开启。
    """
    from local_webpage_access import daemon as daemon_mod
    from local_webpage_access.cli._common import coordinated_autostart_restart

    was_running = daemon_mod.is_running(ws)
    if not was_running:
        return {"wasRunning": False, "pid": None, "message": "daemon 原本未运行，跳过重启"}
    # BUG-191：自启动在管时交监督器重启，避免 KeepAlive/Restart 拉回 + detached 抢锁
    # 产生重复 watcher（叠加 BUG-173）。
    note, _ok, managed = coordinated_autostart_restart(ws, "daemon")
    if managed:
        return {
            "wasRunning": True,
            "pid": None,
            "message": note or "daemon 已通过自启动重启",
        }
    # BUG-192：stop 失败不得报成重启成功（旧进程/锁仍在，重复 watcher 风险）。
    if not daemon_mod.stop_daemon(ws):
        raise RuntimeError(
            "daemon 停止失败（pid 仍存活），已跳过重启；"
            "可 `lwa daemon off` 后重试 `lwa daemon on`"
        )
    pid = daemon_mod.start_daemon(ws, config)
    return {"wasRunning": True, "pid": pid, "message": f"daemon 已重启（pid={pid}）"}


def restart_instances(
    ws: Workspace, config: Config, registry: Registry
) -> dict[str, Any]:
    """逐个重启 running/stopped/failed 实例，跳过 building/queued/pending。

    每个实例独立失败不中断后续；返回 ``{"restarted": [...], "skipped": [...], "failed": {...}}``。
    """
    from local_webpage_access.lifecycle import restart_instance

    restarted: list[str] = []
    skipped: list[str] = []
    failed: dict[str, str] = {}
    for row in registry.list_instances():
        iid = row["id"]
        status = row.get("status") or ""
        if status not in _RESTARTABLE_STATUSES:
            skipped.append(f"{iid}（{status}）")
            continue
        try:
            restart_instance(ws, config, registry, iid)
            restarted.append(iid)
        except Exception as exc:  # noqa: BLE001 — 单实例失败不中断后续
            failed[iid] = str(exc)[:200]
            log.warning("重启实例 %s 失败：%s", iid, exc)
    return {"restarted": restarted, "skipped": skipped, "failed": failed}


def run_doctor_check(ws: Workspace, config: Config) -> str:
    """跑 ``lwa doctor``，返回 overall（ok/warn/fail）。失败抛 RuntimeError。"""
    from local_webpage_access.doctor import run_doctor

    report = run_doctor(ws, config)
    return report.overall


# ---- 主流程 ----------------------------------------------------------------


def run_update(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    *,
    options: UpdateOptions,
) -> UpdateReport:
    """编排 ``lwa update`` 全流程（IMP-008.02）。

    每步独立捕获异常写入 :class:`StepResult`，**不中断后续步骤**；
    ``options.dry_run`` 为真时只做识别与计划展示，不执行任何变更。
    """
    from local_webpage_access.version_info import resolve_version

    version_before = resolve_version()
    report = UpdateReport(
        workspace=str(workspace.root),
        repo=None,
        version_before=version_before,
        version_after=version_before,
    )

    # ---- 1. 识别 repo ----
    try:
        repo = locate_repo(options.repo)
        report.repo = str(repo) if repo else None
        if options.skip_pip:
            report.steps.append(
                StepResult("pip", "skipped", "已通过 --skip-pip 跳过")
            )
        elif repo is None:
            report.steps.append(
                StepResult(
                    "pip",
                    "skipped",
                    "未识别到 lwa 源码根（editable 安装 / git 根 / --repo）；"
                    "如已手动安装可用 --skip-pip",
                )
            )
        else:
            report.steps.append(StepResult("pip", "pending", str(repo)))
    except FileNotFoundError as exc:
        report.repo = options.repo
        report.steps.append(StepResult("pip", "failed", str(exc)))

    # dry-run：到此为止，只展示计划
    if options.dry_run:
        # 把 pending 的 pip 标记为 skipped（dry-run 不执行）
        for s in report.steps:
            if s.status == "pending":
                s.status = "skipped"
                s.message = f"[dry-run] 将执行：{s.message}"
        report.steps.append(
            StepResult("syncSkills", "skipped", "[dry-run] 计划同步 skills/")
        )
        if options.sync_templates:
            report.steps.append(
                StepResult("syncTemplates", "skipped", "[dry-run] 计划同步 templates/")
            )
        if options.restart_manager:
            report.steps.append(
                StepResult("restartManager", "skipped", "[dry-run] 计划重启管理页（若原本运行）")
            )
        if options.restart_daemon:
            report.steps.append(
                StepResult("restartDaemon", "skipped", "[dry-run] 计划重启 daemon（若原本运行）")
            )
        if options.restart_instances:
            report.steps.append(
                StepResult("restartInstances", "skipped", "[dry-run] 计划重启可重启实例")
            )
        if options.run_doctor:
            report.steps.append(
                StepResult("doctor", "skipped", "[dry-run] 计划运行 lwa doctor")
            )
        return report

    # ---- 2. pip install -e . ----
    pip_step = report.step("pip")
    if pip_step and pip_step.status == "pending":
        try:
            summary = run_pip_install(Path(pip_step.message))
            pip_step.status = "ok"
            pip_step.message = summary
            # 清版本缓存，让 version_after 反映新代码
            resolve_version.cache_clear()
        except Exception as exc:  # noqa: BLE001
            pip_step.status = "failed"
            pip_step.message = str(exc)

    # ---- 3. 同步 skills ----
    if options.sync_skills:
        try:
            added, updated, skipped = sync_skills(workspace)
            report.steps.append(
                StepResult(
                    "syncSkills",
                    "ok",
                    f"新增 {len(added)}，更新 {len(updated)}，未变 {len(skipped)}",
                    extra={"added": len(added), "updated": len(updated)},
                )
            )
        except Exception as exc:  # noqa: BLE001
            report.steps.append(StepResult("syncSkills", "failed", str(exc)))

    # ---- 4. 同步 templates（默认关）----
    if options.sync_templates:
        try:
            added, updated, skipped = sync_templates(workspace)
            report.steps.append(
                StepResult(
                    "syncTemplates",
                    "ok",
                    f"新增 {len(added)}，更新 {len(updated)}，未变 {len(skipped)}",
                    extra={"added": len(added), "updated": len(updated)},
                )
            )
        except Exception as exc:  # noqa: BLE001
            report.steps.append(StepResult("syncTemplates", "failed", str(exc)))

    # ---- 5. 配置缺省字段补齐 ----
    try:
        missing, written = migrate_config_defaults(workspace, config)
        if missing:
            report.steps.append(
                StepResult(
                    "migrateConfig",
                    "ok" if written else "skipped",
                    f"补齐 {len(missing)} 个缺失字段：{', '.join(missing)}",
                    extra={"missing": missing, "written": written},
                )
            )
    except Exception as exc:  # noqa: BLE001
        report.steps.append(StepResult("migrateConfig", "failed", str(exc)))

    # ---- 6. 重启 manager ----
    if options.restart_manager:
        try:
            info = restart_manager(workspace, config)
            status = "ok" if info["wasRunning"] else "skipped"
            report.steps.append(
                StepResult("restartManager", status, info["message"], extra=info)
            )
            if info["pid"]:
                report.manager_url = f"http://127.0.0.1:{config.managerPort}/"
        except Exception as exc:  # noqa: BLE001
            report.steps.append(
                StepResult(
                    "restartManager",
                    "failed",
                    f"{exc}（pip 已更新；查 run/manager.json、logs/ 后可手动 lwa manager on）",
                )
            )

    # ---- 7. 重启 daemon ----
    if options.restart_daemon:
        try:
            info = restart_daemon(workspace, config)
            status = "ok" if info["wasRunning"] else "skipped"
            report.steps.append(
                StepResult("restartDaemon", status, info["message"], extra=info)
            )
        except Exception as exc:  # noqa: BLE001
            report.steps.append(StepResult("restartDaemon", "failed", str(exc)))

    # ---- 8. 重启实例（默认关）----
    if options.restart_instances:
        try:
            info = restart_instances(workspace, config, registry)
            n_fail = len(info["failed"])
            report.steps.append(
                StepResult(
                    "restartInstances",
                    "failed" if n_fail else "ok",
                    f"重启 {len(info['restarted'])}，跳过 {len(info['skipped'])}"
                    + (f"，失败 {n_fail}" if n_fail else ""),
                    extra=info,
                )
            )
        except Exception as exc:  # noqa: BLE001
            report.steps.append(StepResult("restartInstances", "failed", str(exc)))

    # ---- 9. doctor ----
    if options.run_doctor:
        try:
            report.doctor_status = run_doctor_check(workspace, config)
            report.steps.append(
                StepResult("doctor", "ok", f"总体：{report.doctor_status.upper()}")
            )
        except Exception as exc:  # noqa: BLE001
            report.steps.append(StepResult("doctor", "failed", str(exc)))

    # ---- 版本收尾 ----
    report.version_after = resolve_version()
    return report


def format_report(report: UpdateReport) -> str:
    """人可读的 ``lwa update`` 摘要（供 CLI 输出）。"""
    lines: list[str] = []
    lines.append("── lwa update ──")
    lines.append(f"  工作区     {report.workspace}")
    lines.append(f"  源码根     {report.repo or '（未识别）'}")
    if report.version_before != report.version_after:
        lines.append(
            f"  版本       {report.version_before} → {report.version_after}"
        )
    else:
        lines.append(f"  版本       {report.version_after}")
    lines.append("")
    lines.append("── 步骤 ──")
    for s in report.steps:
        icon = {"ok": "✓", "failed": "✗", "skipped": "·", "pending": "…"}.get(s.status, "?")
        lines.append(f"  {icon} {s.name:<18} {s.message}")
    if report.manager_url:
        lines.append("")
        lines.append(f"  管理页     {report.manager_url}")
    if report.has_failures:
        lines.append("")
        lines.append("  存在失败步骤，详见上方 ✗ 行；退出码非零。")
    return "\n".join(lines)


__all__ = [
    "UpdateOptions",
    "StepResult",
    "UpdateReport",
    "locate_repo",
    "run_pip_install",
    "sync_skills",
    "sync_templates",
    "migrate_config_defaults",
    "restart_manager",
    "restart_daemon",
    "restart_instances",
    "run_doctor_check",
    "run_update",
    "format_report",
]
