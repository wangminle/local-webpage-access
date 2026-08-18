"""``lwa update`` 工作区热重载（IMP-008）。

把"开发者 ``git pull`` / 改代码后刷新运行态"收敛成一条命令：

::

    识别上下文 → 预检（dry-run）→ pip install -e . → 同步 skills/templates
    → 配置缺省字段补齐 → 重启 manager/daemon/gateway → 可选重启实例 → lwa doctor

设计约束（见 ``docs/plan/待改进功能点记录-20260706.md`` IMP-008；IMP-059 修订）：

* 每步**独立失败不中断后续**，最终退出码反映是否存在失败；
* pip 成功但 manager 重启失败**不回滚** Python 包；提示查 ``run/manager.json``；
* 重启 manager/daemon/gateway 按三态 reconcile（IMP-059）：运行中→重启；
  **enabled=true 且未运行→拉起**并标注「意外未运行，已恢复」；enabled=false→
  跳过（``--no-reconcile`` 回到纯观察态）；
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
from local_webpage_access.import_activity import (
    DEFAULT_IDLE_WAIT,
    wait_until_import_idle,
)
from local_webpage_access.logging import get_logger
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry
from local_webpage_access.version_info import _PACKAGE_NAME as _PROJECT_NAME
from local_webpage_access.version_info import _is_lwa_repo

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
    restart_gateway: bool = True
    restart_instances: bool = False
    run_doctor: bool = True
    review_access: bool = True  # IMP-038：升级后默认轻量 access review
    repo: str | None = None  # 显式 --repo，覆盖自动识别
    # IMP-059：服务级期望态 reconcile——enabled 但未运行的自有服务在 update 时
    # 自动拉起并标注「意外未运行，已恢复」；--no-reconcile 回到纯观察态。
    reconcile_services: bool = True
    # IMP-063：源码阶段开关
    pull: bool = True  # --no-pull：不联网，仅用本地代码刷新 Runtime
    remote: str | None = None  # --remote：覆盖 upstream 解析的远端
    ref: str | None = None  # --ref：覆盖 upstream 解析的分支


@dataclass
class StepResult:
    """单步执行结果。"""

    name: str
    status: str  # ok | warning | failed | skipped | pending
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

    @property
    def has_warnings(self) -> bool:
        """IMP-063：warning 计入 hasWarnings 但不计入 hasFailures。"""
        return any(s.status == "warning" for s in self.steps)

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
            "hasFailures": self.has_failures,
            "hasWarnings": self.has_warnings,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UpdateReport":
        """从 continuation 子报告 JSON 重建（IMP-063 父进程合并用）。"""
        steps: list[StepResult] = []
        for s in data.get("steps") or []:
            extra = {k: v for k, v in s.items() if k not in ("name", "status", "message")}
            steps.append(
                StepResult(
                    name=str(s.get("name", "?")),
                    status=str(s.get("status", "skipped")),
                    message=str(s.get("message", "")),
                    extra=extra,
                )
            )
        return cls(
            workspace=str(data.get("workspace", "")),
            repo=data.get("repo"),
            version_before=str(data.get("versionBefore", "")),
            version_after=str(data.get("versionAfter", "")),
            steps=steps,
            manager_url=data.get("managerUrl"),
            doctor_status=data.get("doctorStatus"),
        )


# ---- 上下文识别 ------------------------------------------------------------


def locate_repo(explicit: str | None = None) -> Path | None:
    """识别 lwa 源码根（IMP-008.01）。

    优先级：``--repo`` 显式 > editable 安装路径（``src/local_webpage_access`` 上两级，
    存在 ``pyproject.toml``）> 当前 git 工作区根。三者都无法定位时返回 ``None``。
    """
    if explicit:
        p = Path(explicit).resolve()
        if p.is_dir() and _is_lwa_repo(p):
            return p
        # 给出明确错误而非静默降级，避免在错误目录跑 pip
        raise FileNotFoundError(
            f"--repo 指定的目录不是 {_PROJECT_NAME} 源码根"
            f"（缺少有效 pyproject.toml 或 project.name 不匹配）：{p}"
        )

    # editable 安装路径
    here = Path(__file__).resolve().parent
    candidate = here.parent.parent
    if _is_lwa_repo(candidate):
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
            if root.is_dir() and _is_lwa_repo(root):
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
            f"pip install -e . 失败（exit {result.returncode}）：\n" + "\n".join(tail)
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

    # BUG-356：只把缺失键追加/嵌入原文，保留注释、键顺序与用户格式。
    # CHK-115：flow-style（portPool: {start: …}）也须补齐嵌套缺省子键。
    updated_raw = raw
    for key, default_value in defaults.items():
        current_value = existing.get(key)
        if not isinstance(default_value, dict) or not isinstance(current_value, dict):
            continue
        nested_missing = {
            child: value for child, value in default_value.items() if child not in current_value
        }
        if not nested_missing:
            continue
        lines = updated_raw.splitlines(keepends=True)
        start = None
        flow_style = False
        key_prefix = f"{key}:"
        for i, line in enumerate(lines):
            stripped = line.rstrip("\r\n")
            if stripped == key_prefix:
                start = i
                flow_style = False
                break
            # flow-style：`portPool: {start: 19000}`（允许 key 后空白）
            if stripped.startswith(key_prefix):
                remainder = stripped[len(key_prefix) :].lstrip()
                if remainder.startswith("{"):
                    start = i
                    flow_style = True
                    break
        if start is None:
            continue
        if flow_style:
            merged = _deep_merge_defaults(default_value, current_value)
            fragment = yaml.safe_dump(
                {key: merged},
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            )
            replacement = list(fragment.splitlines(keepends=True))
            if not replacement[-1].endswith("\n"):
                replacement[-1] = replacement[-1] + "\n"
            # 尽量沿用原行结尾风格
            if lines[start].endswith("\r\n"):
                replacement = [
                    (ln if ln.endswith("\r\n") else ln.rstrip("\n") + "\r\n") for ln in replacement
                ]
            lines[start : start + 1] = replacement
        else:
            end = start + 1
            while end < len(lines) and (
                lines[end].startswith((" ", "\t")) or not lines[end].strip()
            ):
                end += 1
            fragment = yaml.safe_dump(nested_missing, allow_unicode=True, sort_keys=False)
            lines[end:end] = ["  " + line for line in fragment.splitlines(keepends=True)]
        updated_raw = "".join(lines)
    addition = yaml.safe_dump(
        {key: defaults[key] for key in missing},
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    prefix = updated_raw if updated_raw.endswith("\n") else updated_raw + "\n"
    config_path.write_text(prefix + addition, encoding="utf-8")
    log.info("配置迁移：补齐 %d 个缺失字段（备份 → %s）", len(missing), backup.name)
    return missing, True


def run_migrate_config_defaults(ws: Workspace) -> tuple[list[str], bool]:
    """在**新解释器进程**中执行 :func:`migrate_config_defaults`（BUG-357）。

    ``lwa update`` 的 pip 之后，当前进程仍持有升级前的 ``Config`` 类；同进程
    迁移会漏掉新版本新增的缺省字段。子进程重新 import 即可拿到新 schema。
    """
    import json
    import sys

    script = (
        "import json, sys\n"
        "from local_webpage_access.paths import Workspace\n"
        "from local_webpage_access.config import load_config\n"
        "from local_webpage_access.updater import migrate_config_defaults\n"
        "ws = Workspace(sys.argv[1])\n"
        "cfg = load_config(ws)\n"
        "missing, written = migrate_config_defaults(ws, cfg)\n"
        "print(json.dumps({'missing': missing, 'written': written}))\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script, str(ws.root)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("配置迁移子进程启动失败：%s", exc)
        raise
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit={proc.returncode}"
        raise RuntimeError(f"配置迁移子进程失败：{detail}")
    line = (proc.stdout or "").strip().splitlines()
    if not line:
        return [], False
    try:
        payload = json.loads(line[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"配置迁移子进程输出无法解析：{(proc.stdout or '')[:200]}") from exc
    missing = payload.get("missing") or []
    if not isinstance(missing, list):
        missing = []
    return [str(x) for x in missing], bool(payload.get("written"))


def verify_manager_version(
    config: Config,
    *,
    expected: str | None = None,
    timeout: float = 15.0,
) -> tuple[bool, str | None]:
    """轮询 ``/api/health``，确认 ``version`` 与期望一致（BUG-451）。

    * health 不可达 / ``ok`` 不为真 → ``(False, None)``
    * 响应无 ``version`` 字段（旧版管理页）→ 视为通过，返回 ``(True, None)``
    * ``version`` 与期望不一致 → ``(False, actual)``
    * 一致 → ``(True, actual)``
    """
    import time

    from local_webpage_access.manager_service import _fetch_health
    from local_webpage_access.version_info import (
        display_version,
        normalize_version_label,
    )

    want = normalize_version_label(expected if expected is not None else display_version())
    deadline = time.monotonic() + timeout
    last_actual: str | None = None
    while time.monotonic() <= deadline:
        data = _fetch_health(config.managerHost, config.managerPort, timeout=0.5)
        if data and data.get("ok"):
            raw = data.get("version")
            if raw is None or str(raw).strip() == "":
                return True, None
            last_actual = str(raw).strip()
            if normalize_version_label(last_actual) == want:
                return True, last_actual
        time.sleep(0.1)
    return False, last_actual


def _down_since_fields(name: str, ws: Workspace) -> tuple[float | None, str]:
    """IMP-059.04：中断时长估算，返回 (epoch 秒 | None, 文案后缀)。"""
    from local_webpage_access.service_intent import (
        estimate_down_since,
        format_down_duration,
    )

    down_since = estimate_down_since(name, ws)
    return down_since, format_down_duration(down_since)


def _down_since_iso(down_since: float | None) -> str | None:
    if down_since is None:
        return None
    from datetime import datetime, timezone

    return datetime.fromtimestamp(down_since, tz=timezone.utc).isoformat()


def _service_ready(name: str, ws: Workspace, config: Config) -> bool:
    """单个自有服务当前是否就绪（live 探测，issue #5）。"""
    try:
        if name == "daemon":
            from local_webpage_access import daemon as daemon_mod

            return bool(daemon_mod.is_running(ws))
        if name == "gateway":
            from local_webpage_access.gateway_service import is_gateway_running

            return bool(is_gateway_running(ws, config))
        if name == "manager":
            from local_webpage_access.manager_service import _fetch_health

            data = _fetch_health(config.managerHost, config.managerPort, timeout=0.5)
            return bool(data and data.get("ok"))
    except Exception:  # noqa: BLE001 - 探测异常按未就绪处理，交给轮询
        return False
    return False


def _wait_services_ready(
    ws: Workspace,
    config: Config,
    *,
    names: list[str],
    timeout: float = 30.0,
    poll_interval: float = 0.5,
) -> tuple[bool, float, list[str]]:
    """重启后等待服务就绪（issue #5 / L3：禁止 stop->start 后立即探测一次定终身）。

    manager 的 health 在 ``restart_manager`` 内已轮询校验过，一般不需要再等；
    daemon（锁+心跳）与 gateway（admin :2019 绑定）在监督器重启后需要数百毫秒
    到数秒才真正就绪。轮询直至全部就绪或超时；返回 (是否就绪, 等待秒数, 未就绪列表)。
    """
    import time as time_mod

    deadline = time_mod.monotonic() + timeout
    pending = list(names)
    waited_start = time_mod.monotonic()
    while pending and time_mod.monotonic() <= deadline:
        pending = [n for n in pending if not _service_ready(n, ws, config)]
        if pending:
            time_mod.sleep(poll_interval)
    waited = time_mod.monotonic() - waited_start
    return (not pending), waited, pending


def restart_manager(ws: Workspace, config: Config, *, reconcile: bool = True) -> dict[str, Any]:
    """幂等重启管理页：仅当原本 running 时 stop→start（IMP-059 升级为三态）。

    三态语义（059.02）：

    * running → 重启（BUG-191 协调 + BUG-451 版本校验，行为不变）；
    * enabled=true 且未运行 → **拉起**，报告标注「意外未运行（中断约 X），已恢复」；
    * enabled=false → 跳过，文案不变（"原本未运行，跳过重启"）。

    ``reconcile=False``（``--no-reconcile``）时回到纯观察态：未运行一律跳过。
    """
    from local_webpage_access.cli._common import (
        coordinated_autostart_restart,
        coordinated_autostart_start,
    )
    from local_webpage_access.manager_service import (
        is_running,
        start_manager,
        stop_manager,
    )
    from local_webpage_access.service_intent import (
        INTENT_ENABLED,
        service_intent,
    )
    from local_webpage_access.version_info import display_version

    was_running = is_running(ws, config)
    if not was_running:
        if not reconcile or service_intent(ws, config).manager != INTENT_ENABLED:
            return {"wasRunning": False, "pid": None, "message": "管理页原本未运行，跳过重启"}
        # IMP-059：enabled 但意外未运行 → 拉起并标注（不掩盖事实）
        down_since, down_note = _down_since_fields("manager", ws)
        note, _ok, managed = coordinated_autostart_start(ws, "manager")
        if managed:
            ok, actual = verify_manager_version(config)
            if not ok:
                coordinated_autostart_start(ws, "manager")
                ok, actual = verify_manager_version(config)
            if not ok:
                raise RuntimeError(
                    f"管理页拉起后版本不一致：期望 {display_version()}，"
                    f"实际 {actual or '未知'}；可 `lwa manager off` 后 `lwa manager on`"
                )
            msg = f"管理页意外未运行{down_note}，已恢复（{note or '通过自启动单元拉起'}）"
            if actual:
                msg = f"{msg}（version={actual}）"
            return {
                "wasRunning": False,
                "reconciled": True,
                "unexpectedDown": True,
                "downSince": _down_since_iso(down_since),
                "pid": None,
                "version": actual,
                "message": msg,
            }
        pid = start_manager(ws, config)
        ok, actual = verify_manager_version(config)
        if not ok:
            if not stop_manager(ws):
                raise RuntimeError(
                    f"管理页版本不一致（实际 {actual or '未知'}）且二次停止失败；"
                    f"期望 {display_version()}"
                )
            pid = start_manager(ws, config)
            ok, actual = verify_manager_version(config)
        if not ok:
            raise RuntimeError(
                f"管理页拉起后版本不一致：期望 {display_version()}，"
                f"实际 {actual or '未知'}；可 `lwa manager off` 后重试 `lwa manager on`"
            )
        return {
            "wasRunning": False,
            "reconciled": True,
            "unexpectedDown": True,
            "downSince": _down_since_iso(down_since),
            "pid": pid,
            "version": actual,
            "message": f"管理页意外未运行{down_note}，已恢复（pid={pid}）",
        }
    # BUG-191：自启动在管时交监督器重启（单一进程），否则 stop 杀掉后被
    # KeepAlive/Restart 立即拉回、再与 start_manager 的 detached spawn 抢状态。
    note, _ok, managed = coordinated_autostart_restart(ws, "manager")
    if managed:
        ok, actual = verify_manager_version(config)
        if not ok:
            # 监督器已拉起一次仍不对：再协调重启一次
            coordinated_autostart_restart(ws, "manager")
            ok, actual = verify_manager_version(config)
        if not ok:
            raise RuntimeError(
                f"管理页自启动重启后版本不一致：期望 {display_version()}，"
                f"实际 {actual or '未知'}；可 `lwa manager off` 后 `lwa manager on`"
            )
        msg = note or "管理页已通过自启动重启"
        if actual:
            msg = f"{msg}（version={actual}）"
        return {
            "wasRunning": True,
            "pid": None,
            "version": actual,
            "message": msg,
        }
    # BUG-192：stop 失败不得报成重启成功（旧进程仍在跑）；抛错由 run_update 标 failed。
    if not stop_manager(ws):
        raise RuntimeError(
            "管理页停止失败（旧进程可能仍在运行），已跳过重启；"
            "可 `lwa manager off` 后重试 `lwa manager on`"
        )
    pid = start_manager(ws, config)
    ok, actual = verify_manager_version(config)
    if not ok:
        if not stop_manager(ws):
            raise RuntimeError(
                f"管理页版本不一致（实际 {actual or '未知'}）且二次停止失败；"
                f"期望 {display_version()}"
            )
        pid = start_manager(ws, config)
        ok, actual = verify_manager_version(config)
    if not ok:
        raise RuntimeError(
            f"管理页重启后版本不一致：期望 {display_version()}，"
            f"实际 {actual or '未知'}；可 `lwa manager off` 后重试 `lwa manager on`"
        )
    ver_note = f", version={actual}" if actual else ""
    return {
        "wasRunning": True,
        "pid": pid,
        "version": actual,
        "message": f"管理页已重启（pid={pid}{ver_note}）",
    }


def restart_daemon(ws: Workspace, config: Config, *, reconcile: bool = True) -> dict[str, Any]:
    """幂等重启 daemon：running 才 stop→start；IMP-059 三态 reconcile。

    enabled=true 且未运行 → 拉起并标注「意外未运行（中断约 X），已恢复」；
    enabled=false / ``--no-reconcile`` → 跳过（文案不变）。
    """
    from local_webpage_access import daemon as daemon_mod
    from local_webpage_access.cli._common import (
        coordinated_autostart_restart,
        coordinated_autostart_start,
    )
    from local_webpage_access.service_intent import INTENT_ENABLED, service_intent

    was_running = daemon_mod.is_running(ws)
    if not was_running:
        if not reconcile or service_intent(ws, config).daemon != INTENT_ENABLED:
            return {"wasRunning": False, "pid": None, "message": "daemon 原本未运行，跳过重启"}
        down_since, down_note = _down_since_fields("daemon", ws)
        note, _ok, managed = coordinated_autostart_start(ws, "daemon")
        if managed:
            return {
                "wasRunning": False,
                "reconciled": True,
                "unexpectedDown": True,
                "downSince": _down_since_iso(down_since),
                "pid": None,
                "message": f"daemon 意外未运行{down_note}，已恢复（{note or '通过自启动单元拉起'}）",
            }
        pid = daemon_mod.start_daemon(ws, config)
        return {
            "wasRunning": False,
            "reconciled": True,
            "unexpectedDown": True,
            "downSince": _down_since_iso(down_since),
            "pid": pid,
            "message": f"daemon 意外未运行{down_note}，已恢复（pid={pid}）",
        }
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
            "daemon 停止失败（pid 仍存活），已跳过重启；可 `lwa daemon off` 后重试 `lwa daemon on`"
        )
    pid = daemon_mod.start_daemon(ws, config)
    return {"wasRunning": True, "pid": pid, "message": f"daemon 已重启（pid={pid}）"}


def restart_gateway(ws: Workspace, config: Config, *, reconcile: bool = True) -> dict[str, Any]:
    """幂等重启 Caddy Gateway：仅对已运行的 Caddy 执行重启；IMP-059 三态 reconcile。

    自启动监督器在管时由监督器重启/拉起 gateway_service，确保升级后的监督与能力缓存
    刷新逻辑生效；未托管时 stop→start / detached start。enabled=false（或
    ``staticGateway!=caddy``）跳过，文案不变。
    """
    from local_webpage_access.cli._common import (
        coordinated_autostart_restart,
        coordinated_autostart_start,
    )
    from local_webpage_access.gateway_service import (
        is_gateway_running,
        start_gateway,
        stop_gateway,
    )
    from local_webpage_access.service_intent import INTENT_ENABLED, service_intent

    if config.staticGateway != "caddy":
        return {
            "wasRunning": False,
            "pid": None,
            "message": f"staticGateway={config.staticGateway}，无需重启 Caddy Gateway",
        }
    if not is_gateway_running(ws, config):
        if not reconcile or service_intent(ws, config).gateway != INTENT_ENABLED:
            return {
                "wasRunning": False,
                "pid": None,
                "message": "Gateway 原本未运行，跳过重启",
            }
        down_since, down_note = _down_since_fields("gateway", ws)
        note, ok, managed = coordinated_autostart_start(ws, "gateway")
        if managed:
            if not ok:
                raise RuntimeError(note or "Gateway 自启动监督器拉起失败")
            return {
                "wasRunning": False,
                "reconciled": True,
                "unexpectedDown": True,
                "downSince": _down_since_iso(down_since),
                "pid": None,
                "message": f"Gateway 意外未运行{down_note}，已恢复（{note or '通过自启动单元拉起'}）",
            }
        pid = start_gateway(ws, config)
        return {
            "wasRunning": False,
            "reconciled": True,
            "unexpectedDown": True,
            "downSince": _down_since_iso(down_since),
            "pid": pid,
            "message": f"Gateway 意外未运行{down_note}，已恢复（pid={pid}）",
        }

    note, ok, managed = coordinated_autostart_restart(ws, "gateway")
    if managed:
        if not ok:
            raise RuntimeError(note or "Gateway 自启动监督器重启失败")
        return {
            "wasRunning": True,
            "pid": None,
            "message": note or "Gateway 已通过自启动重启",
        }
    if not stop_gateway(ws, config):
        raise RuntimeError(
            "Gateway 停止失败（Caddy master 可能仍在运行），已跳过重启；"
            "可 `lwa gateway off` 后重试 `lwa gateway on`"
        )
    pid = start_gateway(ws, config)
    return {"wasRunning": True, "pid": pid, "message": f"Gateway 已重启（pid={pid}）"}


def restart_instances(ws: Workspace, config: Config, registry: Registry) -> dict[str, Any]:
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
    """跑基础 doctor，并在 Full 档位核验合并后的能力缓存。

    Full 报告会合并 manager/daemon/gateway 的新鲜缓存且校验对应角色仍存活；
    任一能力未就绪都让 update 的 doctor 步骤失败，避免基础环境检查全绿但
    ``gatewayAccess=unknown`` 的假通过。
    """
    from local_webpage_access.capability import (
        collect_capability_report,
        resolve_profile_name,
    )
    from local_webpage_access.doctor import run_doctor

    report = run_doctor(ws, config)
    profile = resolve_profile_name(config.profile, ws.root)
    if profile == "full":
        capability = collect_capability_report(
            workspace_root=ws.root,
            profile="full",
            role="cli",
            include_backend_cached=True,
        )
        if capability.overall != "ready":
            raise RuntimeError(
                "Full 能力验收未就绪："
                f"overall={capability.overall}, "
                f"managerDockerAccess={capability.manager_docker_access}, "
                f"daemonDockerAccess={capability.daemon_docker_access}, "
                f"gatewayAccess={capability.gateway_access}"
                + (f"；建议：{capability.action}" if capability.action else "")
            )
    return report.overall


# ---- 主流程 ----------------------------------------------------------------


def run_update(
    workspace: Workspace,
    config: Config,
    registry: Registry | None,
    *,
    options: UpdateOptions,
) -> UpdateReport:
    """编排 ``lwa update`` 全流程（IMP-008.02）。

    ``registry`` 允许为 ``None``：仅 dry-run 调用方使用（BUG-530 零写入——
    dry-run 分支在触碰 registry 前即返回）；非 dry-run 传 ``None`` 直接报错。

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
            report.steps.append(StepResult("pip", "skipped", "已通过 --skip-pip 跳过"))
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
        report.steps.append(StepResult("syncSkills", "skipped", "[dry-run] 计划同步 skills/"))
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
        if options.restart_gateway:
            report.steps.append(
                StepResult("restartGateway", "skipped", "[dry-run] 计划重启 Gateway（若原本运行）")
            )
        if options.restart_instances:
            report.steps.append(
                StepResult("restartInstances", "skipped", "[dry-run] 计划重启可重启实例")
            )
        report.steps.append(StepResult("accessRefresh", "skipped", "[dry-run] 计划刷新访问地址"))
        if options.review_access:
            report.steps.append(StepResult("accessReview", "skipped", "[dry-run] 计划轻量访问复核"))
        if options.run_doctor:
            report.steps.append(StepResult("doctor", "skipped", "[dry-run] 计划运行 lwa doctor"))
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

    if registry is None:
        raise ValueError("非 dry-run update 需要已打开的 registry")
    _run_runtime_phase(workspace, config, registry, options, report)
    return report


def _run_runtime_phase(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    options: UpdateOptions,
    report: UpdateReport,
) -> None:
    """Runtime 后半段：skills/templates → migrate → 重启 → access → doctor。

    IMP-063：旧进程改了自身源码（HEAD 变化）后**不得**继续执行本段——它依赖
    新 schema/新函数；必须由新解释器接力执行（continuation）。HEAD 未变化的
    内联路径与 continuation 共用本函数。
    """
    from local_webpage_access.version_info import resolve_version

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
        # BUG-357：pip 后须在新进程迁移，否则旧 Config 类漏补新字段
        missing, written = run_migrate_config_defaults(workspace)
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

    # ---- 6. 重启 manager（先等导入空闲，避免打断进行中的导入）----
    if options.restart_manager or options.restart_daemon:
        try:
            waited = wait_until_import_idle(workspace, timeout=DEFAULT_IDLE_WAIT)
            if waited >= 0.05:
                report.steps.append(
                    StepResult(
                        "waitImportIdle",
                        "ok",
                        f"等待导入完成 {waited:.1f}s",
                    )
                )
        except Exception as exc:  # noqa: BLE001
            # 超时则跳过重启，避免强杀导入中的 manager/daemon
            msg = str(exc)
            if options.restart_manager:
                report.steps.append(
                    StepResult(
                        "restartManager",
                        "failed",
                        f"{msg}（已跳过重启管理页；pip 可能已更新）",
                    )
                )
            if options.restart_daemon:
                report.steps.append(
                    StepResult(
                        "restartDaemon",
                        "failed",
                        f"{msg}（已跳过重启 daemon；pip 可能已更新）",
                    )
                )
            import dataclasses

            options = dataclasses.replace(options, restart_manager=False, restart_daemon=False)

    if options.restart_manager:
        try:
            info = restart_manager(workspace, config, reconcile=options.reconcile_services)
            status = "ok" if (info.get("wasRunning") or info.get("reconciled")) else "skipped"
            report.steps.append(StepResult("restartManager", status, info["message"], extra=info))
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
            info = restart_daemon(workspace, config, reconcile=options.reconcile_services)
            status = "ok" if (info.get("wasRunning") or info.get("reconciled")) else "skipped"
            report.steps.append(StepResult("restartDaemon", status, info["message"], extra=info))
        except Exception as exc:  # noqa: BLE001
            report.steps.append(StepResult("restartDaemon", "failed", str(exc)))

    # ---- 8. 重启 Gateway ----
    if options.restart_gateway:
        try:
            info = restart_gateway(workspace, config, reconcile=options.reconcile_services)
            status = "ok" if (info.get("wasRunning") or info.get("reconciled")) else "skipped"
            report.steps.append(StepResult("restartGateway", status, info["message"], extra=info))
        except Exception as exc:  # noqa: BLE001
            report.steps.append(StepResult("restartGateway", "failed", str(exc)))

    # ---- 9. 重启实例（默认关）----
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

    # ---- 9.2 issue #5：重启后等就绪再做 access/doctor 自检 ----
    # stop->start 后立即探测会把「服务还在绑端口/加载配置」的瞬态当成 FAIL；
    # 轮询等待（复用实例探针的思路），超时降级为 warning 并提示复检（L3/L7）。
    wait_names: list[str] = []
    if options.restart_daemon:
        wait_names.append("daemon")
    if options.restart_gateway:
        wait_names.append("gateway")
    if wait_names:
        ready, waited, pending = _wait_services_ready(workspace, config, names=wait_names)
        if ready:
            report.steps.append(
                StepResult("waitReady", "ok", f"等待服务就绪 {waited:.1f}s（{', '.join(wait_names)}）")
            )
        else:
            report.steps.append(
                StepResult(
                    "waitReady",
                    "warning",
                    f"等待 {waited:.0f}s 后仍未就绪：{', '.join(pending)}；"
                    "后续 access/doctor 可能报瞬时失败，稍后请 lwa doctor 复核（issue #5）",
                )
            )

    # ---- 9.5 IMP-038：后台重启后再 refresh（+ 可选 review），避免旧进程回写 ----
    from local_webpage_access.access_workflow import run_access_pass

    access = run_access_pass(
        workspace,
        config,
        registry,
        review=options.review_access,
        dry_run=False,
    )
    if access.refresh_error:
        report.steps.append(StepResult("accessRefresh", "failed", access.refresh_error))
    elif access.refresh is not None:
        report.steps.append(
            StepResult(
                "accessRefresh",
                "ok",
                f"LAN IP={access.refresh.lan_ip or '(无)'}，"
                f"刷新 {len(access.refresh.refreshed)}，"
                f"漂移 {access.refresh.drifted_count}",
                extra={"accessRefresh": access.refresh.to_dict()},
            )
        )
    else:
        report.steps.append(StepResult("accessRefresh", "skipped", "未执行访问地址刷新"))

    if options.review_access:
        if access.review_error:
            report.steps.append(StepResult("accessReview", "failed", access.review_error))
        elif access.review is not None:
            report.steps.append(
                StepResult(
                    "accessReview",
                    "ok",
                    f"总体：{access.review.overall.upper()}",
                    extra={"accessReview": access.review.to_dict()},
                )
            )
        else:
            report.steps.append(StepResult("accessReview", "skipped", "未执行访问复核"))
    else:
        report.steps.append(StepResult("accessReview", "skipped", "已通过 --no-review-access 跳过"))

    # ---- 10. doctor / Full 能力缓存验收 ----
    if options.run_doctor:
        try:
            report.doctor_status = run_doctor_check(workspace, config)
            report.steps.append(StepResult("doctor", "ok", f"总体：{report.doctor_status.upper()}"))
        except Exception as exc:  # noqa: BLE001
            report.steps.append(StepResult("doctor", "failed", str(exc)))

    # ---- 版本收尾 ----
    report.version_after = resolve_version()


def format_report(report: UpdateReport) -> str:
    """人可读的 ``lwa update`` 摘要（供 CLI 输出）。"""
    lines: list[str] = []
    lines.append("── lwa update ──")
    lines.append(f"  工作区     {report.workspace}")
    lines.append(f"  源码根     {report.repo or '（未识别）'}")
    if report.version_before != report.version_after:
        lines.append(f"  版本       {report.version_before} → {report.version_after}")
    else:
        lines.append(f"  版本       {report.version_after}")
    lines.append("")
    lines.append("── 步骤 ──")
    for s in report.steps:
        icon = {"ok": "✓", "failed": "✗", "warning": "!", "skipped": "·", "pending": "…"}.get(
            s.status, "?"
        )
        lines.append(f"  {icon} {s.name:<18} {s.message}")
    if report.manager_url:
        lines.append("")
        lines.append(f"  管理页     {report.manager_url}")
    if report.has_failures:
        lines.append("")
        lines.append("  存在失败步骤，详见上方 ✗ 行；退出码非零。")
    elif report.has_warnings:
        lines.append("")
        lines.append("  存在 warning 步骤（详见上方 ! 行）；整体仍视为成功。")
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
    "run_migrate_config_defaults",
    "restart_manager",
    "verify_manager_version",
    "restart_daemon",
    "restart_gateway",
    "restart_instances",
    "run_doctor_check",
    "run_update",
    "format_report",
    "wait_until_import_idle",
]
