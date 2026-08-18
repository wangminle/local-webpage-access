"""LWA 工作区迁移事务（IMP-042 / DEV-089）。

状态机：preflight → backup → quiesce → move → rebind → regenerate →
restore → verify → complete。

v1 仅支持同卷原子改名（macOS / Linux / WSL Linux 盘）；跨卷/跨机 fail-closed。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

from local_webpage_access.config import Config, load_config
from local_webpage_access.errors import MigrateError
from local_webpage_access.logging import get_logger, now_iso
from local_webpage_access.models import DesiredState
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry

log = get_logger("workspace_migrate")

JOURNAL_NAME = "workspace-migrate-journal.json"
LOCK_NAME = "workspace-migrate.lock"
SNAPSHOT_NAME = "workspace-migrate-snapshot.json"

_MANIFEST_PATH_KEYS = frozenset(
    {
        "composePath",
        "dockerfilePath",
        "sourceZipPath",
        "appPath",
        "gatewayConfigPath",
    }
)

_PHASE_ORDER: tuple[str, ...] = (
    "preflight",
    "backup",
    "quiesce",
    "move",
    "rebind",
    "regenerate",
    "restore",
    "verify",
    "complete",
)


class MigratePhase(str, Enum):
    PREFLIGHT = "preflight"
    BACKUP = "backup"
    QUIESCE = "quiesce"
    MOVE = "move"
    REBIND = "rebind"
    REGENERATE = "regenerate"
    RESTORE = "restore"
    VERIFY = "verify"
    COMPLETE = "complete"


@dataclass
class MigrateIssue:
    code: str
    message: str
    blocking: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MigrateSnapshot:
    restore_instance_ids: list[str] = field(default_factory=list)
    desired_states: dict[str, str] = field(default_factory=dict)
    autostart_installed: list[str] = field(default_factory=list)
    #: 迁前正在跑的控制面（daemon/manager/gateway）；未装自启时 restore 据此拉回（BUG-395）
    control_plane_running: list[str] = field(default_factory=list)
    pageview_hits: dict[str, int] = field(default_factory=dict)
    captured_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrateSnapshot:
        return cls(
            restore_instance_ids=list(data.get("restore_instance_ids") or []),
            desired_states=dict(data.get("desired_states") or {}),
            autostart_installed=list(data.get("autostart_installed") or []),
            control_plane_running=list(data.get("control_plane_running") or []),
            pageview_hits={str(k): int(v) for k, v in (data.get("pageview_hits") or {}).items()},
            captured_at=str(data.get("captured_at") or ""),
        )


@dataclass
class PreflightReport:
    ok: bool
    old: str
    new: str
    blocking: list[MigrateIssue] = field(default_factory=list)
    warnings: list[MigrateIssue] = field(default_factory=list)
    planned_phases: list[str] = field(default_factory=lambda: list(_PHASE_ORDER))
    path_holders: list[str] = field(default_factory=list)
    same_device: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "old": self.old,
            "new": self.new,
            "blocking": [i.to_dict() for i in self.blocking],
            "warnings": [i.to_dict() for i in self.warnings],
            "planned_phases": list(self.planned_phases),
            "path_holders": list(self.path_holders),
            "same_device": self.same_device,
        }


@dataclass
class MigrateResult:
    ok: bool
    dry_run: bool = False
    old: str = ""
    new: str = ""
    phase: str = ""
    preflight: PreflightReport | None = None
    snapshot: MigrateSnapshot | None = None
    started: list[str] = field(default_factory=list)
    verify_ok: bool | None = None
    verify_notes: list[str] = field(default_factory=list)
    error: str | None = None
    planned_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "old": self.old,
            "new": self.new,
            "phase": self.phase,
            "preflight": self.preflight.to_dict() if self.preflight else None,
            "snapshot": self.snapshot.to_dict() if self.snapshot else None,
            "started": list(self.started),
            "verify_ok": self.verify_ok,
            "verify_notes": list(self.verify_notes),
            "error": self.error,
            "planned_actions": list(self.planned_actions),
        }


# ---- journal / lock --------------------------------------------------------


def journal_path(workspace: Workspace) -> Path:
    return workspace.run / JOURNAL_NAME


def lock_path(workspace: Workspace) -> Path:
    return workspace.run / LOCK_NAME


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp.write_text(text, encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def read_journal(workspace: Workspace) -> dict[str, Any] | None:
    path = journal_path(workspace)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MigrateError(f"无法读取迁移 journal：{exc}") from exc


def write_journal(workspace: Workspace, data: dict[str, Any]) -> None:
    data = dict(data)
    data["updated_at"] = now_iso()
    _atomic_write_json(journal_path(workspace), data)


def next_phase(current: str | MigratePhase) -> str | None:
    name = current.value if isinstance(current, MigratePhase) else str(current)
    if name not in _PHASE_ORDER:
        raise MigrateError(f"非法迁移阶段：{name}")
    idx = _PHASE_ORDER.index(name)
    if idx + 1 >= len(_PHASE_ORDER):
        return None
    return _PHASE_ORDER[idx + 1]


def assert_phase_transition(current: str | None, target: str) -> None:
    if target not in _PHASE_ORDER:
        raise MigrateError(f"非法目标阶段：{target}")
    if current is None:
        if target != MigratePhase.PREFLIGHT.value:
            raise MigrateError(f"迁移必须从 preflight 开始，不能直接进入 {target}")
        return
    if current == target:
        return
    nxt = next_phase(current)
    if nxt != target and current != MigratePhase.COMPLETE.value:
        # allow resume to re-enter same failed phase or advance one step
        if target in _PHASE_ORDER and current in _PHASE_ORDER:
            if _PHASE_ORDER.index(target) <= _PHASE_ORDER.index(current):
                return
            if nxt == target:
                return
        raise MigrateError(f"非法阶段跳转：{current} → {target}")


@contextlib.contextmanager
def migrate_lock(workspace: Workspace) -> Iterator[list[Path]]:
    """PID 文件锁（O_CREAT|O_EXCL 防 TOCTOU，BUG-396）。

    yield 一个可变的 ``[lock_path]`` 列表，move 后调用方可把路径改到新工作区，
    以便 finally 仍能正确释锁。
    """
    path = lock_path(workspace)
    workspace.run.mkdir(parents=True, exist_ok=True)

    def _try_acquire() -> None:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            fd = os.open(str(path), flags, 0o600)
        except FileExistsError:
            try:
                old_pid = int(path.read_text(encoding="utf-8").strip() or "0")
            except (ValueError, OSError):
                old_pid = 0
            if old_pid and _pid_alive(old_pid) and old_pid != os.getpid():
                raise MigrateError(
                    f"工作区迁移锁被占用（pid={old_pid}）；若确认无迁移在跑可删除 {path}"
                )
            # 死锁或本进程残留：清掉后重试一次
            with contextlib.suppress(OSError):
                path.unlink()
            try:
                fd = os.open(str(path), flags, 0o600)
            except FileExistsError as exc:
                raise MigrateError(f"工作区迁移锁被占用；若确认无迁移在跑可删除 {path}") from exc
        try:
            os.write(fd, str(os.getpid()).encode("utf-8"))
        finally:
            os.close(fd)

    _try_acquire()
    holder: list[Path] = [path]
    try:
        yield holder
    finally:
        with contextlib.suppress(OSError):
            lp = holder[0]
            if lp.is_file() and lp.read_text(encoding="utf-8").strip() == str(os.getpid()):
                lp.unlink()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# ---- preflight -------------------------------------------------------------


def _device_id(path: Path) -> int | None:
    try:
        target = path if path.exists() else path.parent
        return target.resolve().stat().st_dev
    except OSError:
        return None


def _same_device(old: Path, new: Path) -> bool | None:
    a, b = _device_id(old), _device_id(new)
    if a is None or b is None:
        return None
    return a == b


def _list_path_holders(workspace: Workspace) -> list[str]:
    holders: list[str] = []
    old = str(workspace.root.resolve())
    apps = workspace.apps
    if apps.is_dir():
        for manifest_path in apps.glob("*/local-web.json"):
            try:
                text = manifest_path.read_text(encoding="utf-8")
            except OSError:
                continue
            if old in text:
                holders.append(str(manifest_path.relative_to(workspace.root)))
    db = workspace.db_path
    if db.is_file():
        try:
            conn = sqlite3.connect(db)
            try:
                for table, cols in (
                    ("instances", ("app_path", "source_zip_path")),
                    ("containers", ("compose_path", "dockerfile_path")),
                    ("static_sites", ("gateway_config_path",)),
                    ("builds", ("log_path",)),
                ):
                    for col in cols:
                        try:
                            row = conn.execute(
                                f"SELECT 1 FROM {table} WHERE {col} LIKE ? LIMIT 1",
                                (old + "%",),
                            ).fetchone()
                        except sqlite3.Error:
                            continue
                        if row:
                            holders.append(f"registry:{table}.{col}")
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    return holders


def preflight_migrate(old: Path, new: Path) -> PreflightReport:
    """只读预检。"""
    old_r = old.resolve()
    new_r = new.resolve()
    blocking: list[MigrateIssue] = []
    warnings: list[MigrateIssue] = []

    if not (old_r / "local-web.yml").is_file():
        blocking.append(
            MigrateIssue("not_workspace", f"旧路径不是 LWA 工作区（缺 local-web.yml）：{old_r}")
        )

    if new_r.exists():
        blocking.append(MigrateIssue("target_exists", f"目标路径已存在：{new_r}"))

    same = _same_device(old_r, new_r)
    if same is False:
        blocking.append(
            MigrateIssue(
                "cross_device",
                "旧路径与新路径不在同一文件系统/卷；v1 仅支持同卷原子改名"
                "（见 docs/workspace-rename.md / IMP-042.b）",
            )
        )

    try:
        from local_webpage_access.platform_support import is_wsl_drvfs_path

        if is_wsl_drvfs_path(new_r) or is_wsl_drvfs_path(old_r):
            blocking.append(
                MigrateIssue(
                    "wsl_drvfs",
                    "工作区位于 /mnt/<drive>（Windows 文件系统）；请迁到 Linux 盘后再用本命令",
                )
            )
    except Exception:  # noqa: BLE001
        pass

    path_holders: list[str] = []
    if (old_r / "local-web.yml").is_file():
        path_holders = _list_path_holders(Workspace(old_r))

    try:
        import local_webpage_access as pkg

        pkg_file = getattr(pkg, "__file__", None) or ""
        if pkg_file and str(old_r) in str(Path(pkg_file).resolve()):
            warnings.append(
                MigrateIssue(
                    "editable_inside_workspace",
                    "当前 import 的 local_webpage_access 位于旧工作区内；迁后需在新路径 "
                    "pip install -e，或改用独立 venv",
                    blocking=False,
                )
            )
    except Exception:  # noqa: BLE001
        pass

    ok = not blocking
    return PreflightReport(
        ok=ok,
        old=str(old_r),
        new=str(new_r),
        blocking=blocking,
        warnings=warnings,
        path_holders=path_holders,
        same_device=same,
    )


# ---- snapshot / backup -----------------------------------------------------


def capture_snapshot(
    workspace: Workspace,
    registry: Registry,
    *,
    read_only: bool = False,
) -> MigrateSnapshot:
    restore: list[str] = []
    desired: dict[str, str] = {}
    for row in registry.list_instances():
        iid = row["id"]
        ds = str(row.get("desired_state") or "")
        st = str(row.get("status") or "")
        desired[iid] = ds
        if ds == DesiredState.RUNNING.value or st == "running":
            restore.append(iid)

    pageview_hits: dict[str, int] = {}
    pv_path = workspace.run / "pageviews.db"
    # dry-run / read_only：不得因采样而创建 pageviews.db（BUG-394）
    if pv_path.is_file() or not read_only:
        try:
            from local_webpage_access.pageviews import PageviewStore

            if read_only and not pv_path.is_file():
                pass
            else:
                store = PageviewStore.for_workspace(workspace)
                try:
                    for iid, info in store.summary().items():
                        pageview_hits[iid] = int(info.get("hits") or 0)
                finally:
                    store.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("采集 pageviews 基线失败（忽略）：%s", exc)

    installed: list[str] = []
    try:
        from local_webpage_access import autostart as asm

        installed = list(asm.installed_services(workspace))
    except Exception as exc:  # noqa: BLE001
        log.debug("采集自启清单失败（忽略）：%s", exc)

    control_plane: list[str] = []
    cfg: Config | None = None
    try:
        cfg = load_config(workspace)
    except Exception as exc:  # noqa: BLE001
        log.debug("加载配置失败（控制面探测降级）：%s", exc)
    try:
        from local_webpage_access.daemon import is_running as daemon_is_running

        if daemon_is_running(workspace):
            control_plane.append("daemon")
    except Exception as exc:  # noqa: BLE001
        log.debug("探测 daemon 运行态失败（忽略）：%s", exc)
    if cfg is not None:
        try:
            from local_webpage_access.manager_service import (
                is_running as manager_is_running,
            )

            if manager_is_running(workspace, cfg):
                control_plane.append("manager")
        except Exception as exc:  # noqa: BLE001
            log.debug("探测 manager 运行态失败（忽略）：%s", exc)
        try:
            from local_webpage_access.gateway_service import is_gateway_running

            if is_gateway_running(workspace, cfg):
                control_plane.append("gateway")
        except Exception as exc:  # noqa: BLE001
            log.debug("探测 gateway 运行态失败（忽略）：%s", exc)

    return MigrateSnapshot(
        restore_instance_ids=restore,
        desired_states=desired,
        autostart_installed=installed,
        control_plane_running=control_plane,
        pageview_hits=pageview_hits,
        captured_at=now_iso(),
    )


def _backup_sqlite_file(src: Path, dest: Path) -> None:
    """用 SQLite Online Backup API 拷贝，避免 WAL 热拷贝撕裂/丢提交（BUG-392）。"""
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest_conn = sqlite3.connect(str(dest))
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        src_conn.close()


def write_backup(
    workspace: Workspace,
    snapshot: MigrateSnapshot,
    *,
    backup_root: Path | None = None,
) -> Path:
    """备份关键配置到 workspace.run/migrate-backup-<ts> 或指定目录。"""
    ts = time.strftime("%Y%m%d%H%M%S")
    dest = backup_root or (workspace.run / f"migrate-backup-{ts}")
    dest.mkdir(parents=True, exist_ok=True)

    _atomic_write_json(dest / SNAPSHOT_NAME, snapshot.to_dict())

    yml = workspace.config_path
    if yml.is_file():
        shutil.copy2(yml, dest / "local-web.yml")

    reg = workspace.db_path
    if reg.is_file():
        _backup_sqlite_file(reg, dest / "local-web.db")

    pv = workspace.run / "pageviews.db"
    if pv.is_file():
        _backup_sqlite_file(pv, dest / "pageviews.db")

    manifests = dest / "manifests"
    manifests.mkdir(exist_ok=True)
    if workspace.apps.is_dir():
        for mp in workspace.apps.glob("*/local-web.json"):
            shutil.copy2(mp, manifests / f"{mp.parent.name}.json")

    # 自启 unit 副本（best-effort）
    try:
        from local_webpage_access import autostart as asm

        backend = asm.select_backend()
        units_dir = dest / "autostart-units"
        units_dir.mkdir(exist_ok=True)
        for name in asm.installed_services(workspace, backend):
            up = backend.unit_path(name)
            if up.is_file():
                shutil.copy2(up, units_dir / up.name)
    except Exception as exc:  # noqa: BLE001
        log.debug("备份自启单元失败（忽略）：%s", exc)

    return dest


# ---- path rewrite ----------------------------------------------------------


def _rewrite_str(value: str, old: str, new: str) -> str:
    if value == old or value.startswith(old + os.sep) or value.startswith(old + "/"):
        return new + value[len(old) :]
    return value


def _rewrite_obj(obj: Any, old: str, new: str, *, only_keys: bool) -> Any:
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if only_keys and k in _MANIFEST_PATH_KEYS and isinstance(v, str):
                out[k] = _rewrite_str(v, old, new)
            else:
                out[k] = _rewrite_obj(v, old, new, only_keys=only_keys)
        return out
    if isinstance(obj, list):
        return [_rewrite_obj(x, old, new, only_keys=only_keys) for x in obj]
    return obj


def rewrite_manifest_paths(manifest_path: Path, old: str, new: str) -> bool:
    """结构化改写 manifest；返回是否有变更。"""
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    rewritten = _rewrite_obj(data, old, new, only_keys=True)
    # 清陈旧容器身份，便于 start 走 up -d（BUG-382）
    container = rewritten.get("container")
    if isinstance(container, dict) and container.get("containerId"):
        container["containerId"] = None
        container["imageId"] = None
    if rewritten == data and data.get("container", {}).get("containerId") is None:
        # still may need clear when paths unchanged but id present — handled above
        pass
    text = json.dumps(rewritten, ensure_ascii=False, indent=2) + "\n"
    tmp = manifest_path.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, manifest_path)
    return True


def rewrite_registry_paths(db_path: Path, old: str, new: str) -> None:
    if not db_path.is_file():
        return
    if not old or old == new:
        return
    conn = sqlite3.connect(db_path)
    try:
        # BUG-473：可写 registry 连接统一开外键不变量（与 connect() 对齐），
        # 防止任何写入意外留下破坏引用完整性的行。
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        updates = (
            ("instances", ("app_path", "source_zip_path")),
            ("containers", ("compose_path", "dockerfile_path")),
            ("static_sites", ("gateway_config_path",)),
            ("builds", ("log_path",)),
        )
        for table, cols in updates:
            for col in cols:
                # BUG-518/527：边界安全 + 幂等改写。用 substr 精确匹配 = old
                # 或 old/、old\ 前缀，避免 LIKE 把路径中的 _/% 当通配符
                # （用户名含下划线会多选出兄弟路径）。
                try:
                    prefix_len = len(old) + 1
                    rows = conn.execute(
                        f"SELECT rowid, {col} FROM {table} "
                        f"WHERE {col} IS NOT NULL AND "
                        f"({col} = ? OR substr({col}, 1, ?) = ? "
                        f"OR substr({col}, 1, ?) = ?)",
                        (old, prefix_len, old + "/", prefix_len, old + "\\"),
                    ).fetchall()
                except sqlite3.Error:
                    continue
                for row in rows:
                    value = row[1]
                    if not isinstance(value, str) or not value:
                        continue
                    rewritten = _rewrite_text_paths(value, old, new)
                    if rewritten != value:
                        conn.execute(
                            f"UPDATE {table} SET {col}=? WHERE rowid=?",
                            (rewritten, row[0]),
                        )
        # 清空陈旧 container_id
        with contextlib.suppress(sqlite3.Error):
            conn.execute("UPDATE containers SET container_id=NULL, image_id=NULL")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _rewrite_text_paths(text: str, old: str, new: str) -> str:
    """边界安全地替换文本中的工作区路径前缀（避免 /lwa 误伤 /lwa-backup）。"""
    if old == new or not old:
        return text
    out = text
    for sep in ("/", "\\"):
        needle = old + sep
        if needle in out:
            out = out.replace(needle, new + sep)
    # 精确等于 old 的片段（其后非路径字符）
    out = re.sub(
        re.escape(old) + r"(?![A-Za-z0-9_./\\-])",
        new,
        out,
    )
    return out


def _contains_old_path(text: str, old: str) -> bool:
    """边界安全地判断文本是否仍含旧工作区路径（与 :func:`_rewrite_text_paths` 同口径）。

    BUG-519：裸 ``old in text`` 会把 ``old`` 为 ``new`` 前缀（``lwa→lwa2``）或兄弟
    目录（``/lwa-backup``）的情况误判为“仍含旧路径”，导致 verify 恒报失败。
    """
    if not old:
        return False
    for sep in ("/", "\\"):
        if (old + sep) in text:
            return True
    return re.search(re.escape(old) + r"(?![A-Za-z0-9_./\\-])", text) is not None


def rewrite_gateway_fragment_paths(workspace: Workspace, old: str, new: str) -> list[str]:
    """改写 static-gateway sites/aliases 片段内的绝对路径（BUG-404）。"""
    changed: list[str] = []
    for pattern in (
        "static-gateway/sites/*.conf",
        "static-gateway/aliases/*.conf",
    ):
        for path in workspace.root.glob(pattern):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            rewritten = _rewrite_text_paths(text, old, new)
            if rewritten != text:
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(rewritten, encoding="utf-8")
                os.replace(tmp, path)
                changed.append(str(path.relative_to(workspace.root)))
    return changed


def rebind_workspace_paths(workspace: Workspace, old: str, new: str) -> list[str]:
    changed: list[str] = []
    if workspace.apps.is_dir():
        for mp in workspace.apps.glob("*/local-web.json"):
            rewrite_manifest_paths(mp, old, new)
            changed.append(str(mp.relative_to(workspace.root)))
    if workspace.db_path.is_file():
        rewrite_registry_paths(workspace.db_path, old, new)
        changed.append("registry/local-web.db")
    changed.extend(rewrite_gateway_fragment_paths(workspace, old, new))
    return changed


# ---- quiesce / move / regenerate / restore / verify ------------------------


def quiesce_workspace(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    snapshot: MigrateSnapshot,
    *,
    dry_run: bool = False,
) -> list[str]:
    """停业务实例、down 容器、停自启/gateway。不删 daemon-processed.json。"""
    actions: list[str] = []
    from local_webpage_access.lifecycle import stop_instance_op

    for iid in snapshot.restore_instance_ids:
        actions.append(f"stop:{iid}")
        if dry_run:
            continue
        with contextlib.suppress(Exception):
            stop_instance_op(workspace, config, registry, iid)

    for iid in snapshot.restore_instance_ids:
        compose = workspace.app_compose_path(iid)
        if not compose.is_file():
            continue
        actions.append(f"compose_down:{iid}")
        if dry_run:
            continue
        try:
            from local_webpage_access.docker_runtime import DockerRuntime

            DockerRuntime.ensure_available()
            DockerRuntime(workspace, registry).down(iid)
        except Exception as exc:  # noqa: BLE001
            log.warning("compose down %s 失败（继续）：%s", iid, exc)

    actions.append("autostart_disable")
    if not dry_run:
        try:
            from local_webpage_access import autostart as asm

            with contextlib.suppress(Exception):
                asm.disable(workspace, config)
        except Exception as exc:  # noqa: BLE001
            log.warning("autostart disable 失败（继续）：%s", exc)

        try:
            from local_webpage_access.daemon import stop_daemon

            with contextlib.suppress(Exception):
                stop_daemon(workspace)
            actions.append("stop:daemon")
        except Exception as exc:  # noqa: BLE001
            log.warning("stop daemon 失败（继续）：%s", exc)

        try:
            from local_webpage_access.manager_service import stop_manager

            with contextlib.suppress(Exception):
                stop_manager(workspace)
            actions.append("stop:manager")
        except Exception as exc:  # noqa: BLE001
            log.warning("stop manager 失败（继续）：%s", exc)

        try:
            from local_webpage_access.gateway_service import stop_gateway

            with contextlib.suppress(Exception):
                stop_gateway(workspace, config)
            actions.append("stop:gateway")
        except Exception as exc:  # noqa: BLE001
            log.warning("stop gateway 失败（继续）：%s", exc)

        # 显式保留 daemon-processed.json
        processed = workspace.run / "daemon-processed.json"
        if processed.is_file():
            actions.append("keep:daemon-processed.json")

    return actions


def move_workspace_root(old: Path, new: Path) -> None:
    """同卷原子改名；跨设备抛 MigrateError。"""
    old_r = old.resolve()
    new_r = new.resolve()
    if new_r.exists():
        raise MigrateError(f"目标已存在：{new_r}")
    new_r.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(old_r, new_r)
    except OSError as exc:
        # EXDEV = cross-device
        if getattr(exc, "errno", None) == getattr(__import__("errno"), "EXDEV", 18):
            raise MigrateError(
                "跨文件系统改名不被 v1 支持；请同卷迁移或参见 DOC-081 人工流程"
            ) from exc
        raise MigrateError(f"工作区改名失败：{exc}") from exc


def regenerate_after_move(
    workspace: Workspace,
    config: Config,
    *,
    snapshot: MigrateSnapshot | None = None,
) -> list[str]:
    actions: list[str] = []
    # capability 缓存
    for name in (
        "capability-manager.json",
        "capability-daemon.json",
        "capability-gateway.json",
    ):
        p = workspace.run / name
        if p.is_file():
            p.unlink()
            actions.append(f"rm:{name}")

    installed = list((snapshot or MigrateSnapshot()).autostart_installed)
    if installed:
        try:
            from local_webpage_access import autostart as asm

            # 仅当迁前已装过对应单元时才 repair；with_caddy 跟快照走（BUG-388）
            asm.repair(
                workspace,
                config,
                with_caddy="gateway" in installed,
            )
            actions.append("autostart_repair")
        except Exception as exc:  # noqa: BLE001
            log.warning("autostart repair 失败：%s", exc)
            actions.append(f"autostart_repair_failed:{exc}")
    else:
        actions.append("autostart_skipped")

    try:
        from local_webpage_access.static_gateway import StaticGateway

        gw = StaticGateway(workspace, config)
        gw._sync_main_config()  # noqa: SLF001 — 生成器重生主 Caddyfile
        actions.append("caddy_sync_main")
    except Exception as exc:  # noqa: BLE001
        log.warning("Caddy 主配置重生失败：%s", exc)
        actions.append(f"caddy_sync_failed:{exc}")

    return actions


def restore_instances(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    snapshot: MigrateSnapshot,
) -> list[str]:
    started: list[str] = []
    try:
        from local_webpage_access import autostart as asm

        if snapshot.autostart_installed:
            with contextlib.suppress(Exception):
                asm.enable(workspace, config)
        else:
            # 未装自启：按迁前控制面运行态拉回 detached 进程（BUG-395）
            for name in snapshot.control_plane_running:
                try:
                    if name == "daemon":
                        from local_webpage_access.daemon import start_daemon

                        start_daemon(workspace, config)
                    elif name == "manager":
                        from local_webpage_access.manager_service import (
                            maybe_start_manager,
                        )

                        maybe_start_manager(workspace, config)
                except Exception as exc:  # noqa: BLE001
                    log.warning("恢复 detached %s 失败：%s", name, exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("autostart/控制面恢复失败：%s", exc)

    if config.staticGateway == "caddy" or "gateway" in snapshot.control_plane_running:
        try:
            from local_webpage_access.gateway_service import maybe_start_gateway

            maybe_start_gateway(workspace, config)
        except Exception as exc:  # noqa: BLE001
            log.warning("gateway 启动失败：%s", exc)

    from local_webpage_access.lifecycle import start_instance

    for iid in snapshot.restore_instance_ids:
        try:
            start_instance(workspace, config, registry, iid)
            started.append(iid)
        except Exception as exc:  # noqa: BLE001
            log.error("恢复实例 %s 失败：%s", iid, exc)
            raise MigrateError(
                f"恢复实例 {iid} 失败：{exc}；可 lwa start / rebuild 后 "
                "lwa workspace relocate --verify"
            ) from exc
    return started


def verify_migrate(
    workspace: Workspace,
    old: str,
    new: str,
    snapshot: MigrateSnapshot,
) -> tuple[bool, list[str]]:
    notes: list[str] = []
    ok = True
    root = str(workspace.root.resolve())
    if root != str(Path(new).resolve()):
        notes.append(f"当前工作区根 {root} ≠ 期望 {new}")
        ok = False

    # 关键配置不应再含 OLD（日志除外）；含 sites/aliases 片段（BUG-404）
    for pattern in (
        "apps/*/local-web.json",
        "static-gateway/Caddyfile",
        "static-gateway/sites/*.conf",
        "static-gateway/aliases/*.conf",
    ):
        for path in workspace.root.glob(pattern):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if _contains_old_path(text, old):
                notes.append(f"仍含旧路径：{path.relative_to(workspace.root)}")
                ok = False

    # pageviews 不翻倍
    try:
        from local_webpage_access.pageviews import PageviewStore

        store = PageviewStore.for_workspace(workspace)
        try:
            summary = store.summary()
            for iid, base in snapshot.pageview_hits.items():
                cur = int((summary.get(iid) or {}).get("hits") or 0)
                # 允许少量新流量；禁止接近翻倍
                if base > 0 and cur > max(base * 1.05 + 20, base + 20):
                    notes.append(f"pageviews[{iid}] 异常增长：基线 {base} → {cur}")
                    ok = False
        finally:
            store.close()
    except Exception as exc:  # noqa: BLE001
        notes.append(f"pageviews 对账跳过：{exc}")

    processed = workspace.run / "daemon-processed.json"
    if not processed.exists():
        # 不一定必须存在；仅当备份时有才警告——此处只记录提示
        notes.append("info: daemon-processed.json 当前不存在（若迁前也没有则正常）")

    if snapshot.control_plane_running and not snapshot.autostart_installed:
        notes.append(
            "info: 迁前控制面为 detached 运行（"
            + ",".join(snapshot.control_plane_running)
            + "）；已尝试拉回，请用 lwa daemon/manager/gateway status 确认"
        )

    try:
        from local_webpage_access import autostart as asm

        report = asm.run_check(workspace, load_config(workspace))
        if getattr(report, "overall", "") == "fail":
            notes.append("autostart check 存在 fail 项")
            # 不强制 ok=False：单元可能尚未 enable
    except Exception as exc:  # noqa: BLE001
        notes.append(f"autostart check 跳过：{exc}")

    return ok, notes


# ---- orchestration ---------------------------------------------------------


def run_migrate(
    old: Path,
    new: Path,
    *,
    dry_run: bool = False,
    yes: bool = False,  # noqa: ARG001 — CLI 层处理确认
    snapshot_out: Path | None = None,
    resume: bool = False,
    verify_only: bool = False,
    rollback: bool = False,
) -> MigrateResult:
    """执行或预演工作区迁移。"""
    if verify_only:
        ws = Workspace(old if (old / "local-web.yml").is_file() else new)
        if not (ws.root / "local-web.yml").is_file():
            # 尝试 new
            ws = Workspace(new)
        journal = read_journal(ws) or {}
        snap_data = journal.get("snapshot") or {}
        snapshot = MigrateSnapshot.from_dict(snap_data) if snap_data else MigrateSnapshot()
        old_s = str(journal.get("old") or old)
        new_s = str(journal.get("new") or ws.root)
        vok, notes = verify_migrate(ws, old_s, new_s, snapshot)
        return MigrateResult(
            ok=vok,
            old=old_s,
            new=new_s,
            phase=MigratePhase.VERIFY.value,
            snapshot=snapshot,
            verify_ok=vok,
            verify_notes=notes,
        )

    if rollback:
        # 与正向迁移共用锁，避免与并发 relocate 互踩（BUG-396）
        old_r, new_r = old.resolve(), new.resolve()
        lock_ws = Workspace(new_r) if (new_r / "local-web.yml").is_file() else Workspace(old_r)
        if not lock_ws.root.exists():
            raise MigrateError("回滚目标工作区不存在")
        with migrate_lock(lock_ws):
            return _rollback_migrate(old, new)

    old_r = old.resolve()
    new_r = new.resolve()
    preflight = preflight_migrate(old_r, new_r)
    planned = [
        "preflight",
        "backup",
        "quiesce",
        "move",
        "rebind",
        "regenerate",
        "restore",
        "verify",
        "complete",
    ]

    if dry_run:
        actions = list(planned)
        if preflight.ok:
            try:
                ws = Workspace(old_r)
                # 零副作用：只读打开；registry 不存在则跳过采样（BUG-394）
                if not ws.db_path.is_file():
                    actions.append("snapshot_preview_skipped:no_registry")
                else:
                    reg = Registry(ws.db_path)
                    reg.open_readonly(immutable=True)
                    try:
                        snap = capture_snapshot(ws, reg, read_only=True)
                        actions.extend(
                            quiesce_workspace(ws, load_config(ws), reg, snap, dry_run=True)
                        )
                    finally:
                        reg.close()
            except Exception as exc:  # noqa: BLE001
                actions.append(f"snapshot_preview_failed:{exc}")
        return MigrateResult(
            ok=preflight.ok,
            dry_run=True,
            old=preflight.old,
            new=preflight.new,
            phase=MigratePhase.PREFLIGHT.value,
            preflight=preflight,
            planned_actions=actions,
            error=None if preflight.ok else "; ".join(i.message for i in preflight.blocking),
        )

    if not preflight.ok and not resume:
        return MigrateResult(
            ok=False,
            old=preflight.old,
            new=preflight.new,
            phase=MigratePhase.PREFLIGHT.value,
            preflight=preflight,
            error="; ".join(i.message for i in preflight.blocking),
        )

    # 真实执行：锁在旧工作区上获取；move 后 journal 跟到新路径
    ws = Workspace(old_r)
    if resume:
        # journal 可能已在 new
        if (new_r / "run" / JOURNAL_NAME).is_file():
            ws = Workspace(new_r)
        elif not (old_r / "local-web.yml").is_file() and (new_r / "local-web.yml").is_file():
            ws = Workspace(new_r)

    lock_ws = ws if ws.root.exists() else Workspace(old_r)
    with migrate_lock(lock_ws) as lock_holder:
        return _run_migrate_locked(
            old_r,
            new_r,
            preflight=preflight,
            resume=resume,
            snapshot_out=snapshot_out,
            lock_holder=lock_holder,
        )


def _run_migrate_locked(
    old_r: Path,
    new_r: Path,
    *,
    preflight: PreflightReport,
    resume: bool,
    snapshot_out: Path | None,
    lock_holder: list[Path],
) -> MigrateResult:
    ws = Workspace(old_r)
    journal = read_journal(ws) if (ws.run / JOURNAL_NAME).is_file() else None
    if resume and journal is None and (new_r / "run" / JOURNAL_NAME).is_file():
        ws = Workspace(new_r)
        journal = read_journal(ws)
        lock_holder[0] = lock_path(ws)

    phase = (journal or {}).get("phase", MigratePhase.PREFLIGHT.value)
    snapshot = MigrateSnapshot.from_dict((journal or {}).get("snapshot") or {})
    backup_dir = (journal or {}).get("backup_dir")
    reg: Registry | None = None

    def _save(p: str, **extra: Any) -> None:
        nonlocal journal
        data = {
            "phase": p,
            "old": str(old_r),
            "new": str(new_r),
            "snapshot": snapshot.to_dict(),
            "backup_dir": backup_dir,
            **extra,
        }
        write_journal(ws, data)
        journal = data

    try:
        # PREFLIGHT
        if not resume or phase in (MigratePhase.PREFLIGHT.value, None):
            if not preflight.ok:
                raise MigrateError("; ".join(i.message for i in preflight.blocking))
            ws.run.mkdir(parents=True, exist_ok=True)
            _save(MigratePhase.PREFLIGHT.value)
            phase = MigratePhase.BACKUP.value

        # resume 时若已过 move，ws 已在 new；否则仍在 old
        if resume and phase in (
            MigratePhase.REBIND.value,
            MigratePhase.REGENERATE.value,
            MigratePhase.RESTORE.value,
            MigratePhase.VERIFY.value,
            MigratePhase.COMPLETE.value,
        ):
            if (new_r / "local-web.yml").is_file():
                ws = Workspace(new_r)
                lock_holder[0] = lock_path(ws)

        config = load_config(ws)
        reg = Registry(ws.db_path)
        reg.open()

        # BUG-519：resume 且 journal 已是 complete 时，禁止把 verify_ok 默认成 True。
        # 首次 verify 失败仍会把 phase 写成 complete；COMPLETE 写入若丢掉
        # verify_ok，resume 会直接报成功。缺字段或明确失败时重新验收。
        if resume and phase == MigratePhase.COMPLETE.value:
            stored_ok = (journal or {}).get("verify_ok")
            stored_notes = list((journal or {}).get("verify_notes") or [])
            resume_started = list((journal or {}).get("restored_ids") or [])
            if stored_ok is True:
                resume_verify_ok, resume_verify_notes = True, stored_notes
            else:
                resume_verify_ok, resume_verify_notes = verify_migrate(
                    ws, str(old_r), str(new_r), snapshot
                )
            _save(
                MigratePhase.COMPLETE.value,
                completed_at=now_iso(),
                verify_ok=resume_verify_ok,
                verify_notes=resume_verify_notes,
                restored_ids=resume_started,
            )
            return MigrateResult(
                ok=resume_verify_ok,
                old=str(old_r),
                new=str(new_r),
                phase=MigratePhase.COMPLETE.value,
                preflight=preflight,
                snapshot=snapshot,
                started=resume_started,
                verify_ok=resume_verify_ok,
                verify_notes=resume_verify_notes,
            )

        # BACKUP
        if phase == MigratePhase.BACKUP.value:
            # resume：仅当 journal 已有真正采集过的快照时复用（captured_at 非空），
            # 避免 PREFLIGHT 写入的空 snapshot={} 导致跳过采样（BUG-387）
            prior = (journal or {}).get("snapshot") if resume else None
            prior_ok = isinstance(prior, dict) and bool(str(prior.get("captured_at") or "").strip())
            if prior_ok:
                snapshot = MigrateSnapshot.from_dict(prior)  # type: ignore[arg-type]
                if not backup_dir:
                    backup_dir = (journal or {}).get("backup_dir")
            else:
                snapshot = capture_snapshot(ws, reg)
                bdir = write_backup(ws, snapshot)
                backup_dir = str(bdir)
                if snapshot_out is not None:
                    snapshot_out.parent.mkdir(parents=True, exist_ok=True)
                    _atomic_write_json(snapshot_out, snapshot.to_dict())
            _save(MigratePhase.BACKUP.value)
            phase = MigratePhase.QUIESCE.value

        # QUIESCE
        if phase == MigratePhase.QUIESCE.value:
            actions = quiesce_workspace(ws, config, reg, snapshot, dry_run=False)
            _save(MigratePhase.QUIESCE.value, quiesce_actions=actions)
            phase = MigratePhase.MOVE.value

        # MOVE
        if phase == MigratePhase.MOVE.value:
            if not (new_r / "local-web.yml").is_file():
                _save(MigratePhase.MOVE.value, moving=True)
                move_workspace_root(old_r, new_r)
            ws = Workspace(new_r)
            lock_holder[0] = lock_path(ws)
            # backup_dir 随目录树搬到 NEW，刷新 journal 内绝对路径（BUG-393）
            if backup_dir:
                old_s = str(old_r)
                if (
                    backup_dir == old_s
                    or backup_dir.startswith(old_s + os.sep)
                    or backup_dir.startswith(old_s + "/")
                ):
                    backup_dir = str(new_r) + backup_dir[len(old_s) :]
            config = load_config(ws)
            reg.close()
            reg = Registry(ws.db_path)
            reg.open()
            _save(MigratePhase.MOVE.value, moved_at=now_iso())
            phase = MigratePhase.REBIND.value

        # REBIND
        if phase == MigratePhase.REBIND.value:
            changed = rebind_workspace_paths(ws, str(old_r), str(new_r))
            _save(MigratePhase.REBIND.value, rewritten=changed)
            phase = MigratePhase.REGENERATE.value

        # REGENERATE
        if phase == MigratePhase.REGENERATE.value:
            actions = regenerate_after_move(ws, config, snapshot=snapshot)
            _save(MigratePhase.REGENERATE.value, regenerate_actions=actions)
            phase = MigratePhase.RESTORE.value

        # RESTORE
        started: list[str] = []
        if phase == MigratePhase.RESTORE.value:
            started = restore_instances(ws, config, reg, snapshot)
            _save(MigratePhase.RESTORE.value, restored_ids=started)
            phase = MigratePhase.VERIFY.value
        else:
            started = list((journal or {}).get("restored_ids") or [])

        # VERIFY
        verify_ok = True
        verify_notes: list[str] = []
        if phase == MigratePhase.VERIFY.value:
            verify_ok, verify_notes = verify_migrate(ws, str(old_r), str(new_r), snapshot)
            _save(
                MigratePhase.VERIFY.value,
                verify_ok=verify_ok,
                verify_notes=verify_notes,
            )
            phase = MigratePhase.COMPLETE.value

        # COMPLETE（必须带上 verify_ok，避免 resume 丢失验收结果）
        _save(
            MigratePhase.COMPLETE.value,
            completed_at=now_iso(),
            verify_ok=verify_ok,
            verify_notes=verify_notes,
            restored_ids=started,
        )
        return MigrateResult(
            ok=verify_ok,
            old=str(old_r),
            new=str(new_r),
            phase=MigratePhase.COMPLETE.value,
            preflight=preflight,
            snapshot=snapshot,
            started=started,
            verify_ok=verify_ok,
            verify_notes=verify_notes,
        )
    except Exception as exc:
        err = str(exc)
        with contextlib.suppress(Exception):
            write_journal(
                ws,
                {
                    "phase": phase,
                    "old": str(old_r),
                    "new": str(new_r),
                    "snapshot": snapshot.to_dict(),
                    "backup_dir": backup_dir,
                    "error": err,
                },
            )
        return MigrateResult(
            ok=False,
            old=str(old_r),
            new=str(new_r),
            phase=phase,
            preflight=preflight,
            snapshot=snapshot,
            error=err,
        )
    except KeyboardInterrupt:
        # Ctrl-C 不是 Exception；必须落盘 journal 才能 --resume（BUG-387）
        with contextlib.suppress(Exception):
            write_journal(
                ws,
                {
                    "phase": phase,
                    "old": str(old_r),
                    "new": str(new_r),
                    "snapshot": snapshot.to_dict(),
                    "backup_dir": backup_dir,
                    "error": "KeyboardInterrupt",
                },
            )
        raise
    finally:
        if reg is not None:
            with contextlib.suppress(Exception):
                reg.close()


def _rollback_migrate(old: Path, new: Path) -> MigrateResult:
    """v1 回滚：rename 回 OLD，并反向改写路径 / 重生配置（BUG-386）。"""
    old_r, new_r = old.resolve(), new.resolve()
    ws_new = Workspace(new_r) if (new_r / "local-web.yml").is_file() else None
    journal = read_journal(ws_new) if ws_new else None
    if journal is None and (old_r / "run" / JOURNAL_NAME).is_file():
        journal = read_journal(Workspace(old_r))

    if not journal:
        raise MigrateError("无迁移 journal，无法自动回滚；请见 docs/workspace-rename.md")

    j_old = Path(str(journal.get("old") or old_r))
    j_new = Path(str(journal.get("new") or new_r))
    snap = MigrateSnapshot.from_dict(journal.get("snapshot") or {})
    notes: list[str] = []
    if j_new.exists() and not j_old.exists():
        # quiesce best-effort on new then rename back
        try:
            ws = Workspace(j_new)
            config = load_config(ws)
            reg = Registry(ws.db_path)
            reg.open()
            try:
                quiesce_workspace(ws, config, reg, snap, dry_run=False)
            finally:
                reg.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("回滚前停服失败：%s", exc)
        move_workspace_root(j_new, j_old)

        # 反向改写：NEW → OLD（REBIND 的逆操作）
        ws = Workspace(j_old)
        try:
            changed = rebind_workspace_paths(ws, str(j_new), str(j_old))
            notes.append(f"已反向改写 {len(changed)} 处路径")
        except Exception as exc:  # noqa: BLE001
            log.warning("回滚反向改写失败：%s", exc)
            notes.append(f"反向改写失败：{exc}")

        config = load_config(ws)
        try:
            regen = regenerate_after_move(ws, config, snapshot=snap)
            notes.append(f"regenerate: {', '.join(regen) or 'ok'}")
        except Exception as exc:  # noqa: BLE001
            log.warning("回滚 regenerate 失败：%s", exc)
            notes.append(f"regenerate 失败：{exc}")

        started: list[str] = []
        reg = Registry(ws.db_path)
        reg.open()
        try:
            started = restore_instances(ws, config, reg, snap)
        except Exception as exc:  # noqa: BLE001
            log.warning("回滚后恢复实例失败：%s", exc)
            notes.append(f"恢复实例失败：{exc}")
        finally:
            reg.close()

        with contextlib.suppress(Exception):
            write_journal(
                ws,
                {
                    "phase": "rollback",
                    "old": str(j_new),
                    "new": str(j_old),
                    "snapshot": snap.to_dict(),
                    "rolled_back_at": now_iso(),
                },
            )

        notes.insert(0, "已将工作区 rename 回旧路径并反向改写配置")
        return MigrateResult(
            ok=True,
            old=str(j_new),
            new=str(j_old),
            phase="rollback",
            snapshot=snap,
            started=started,
            verify_notes=notes,
        )
    raise MigrateError("自动回滚条件不满足（需 NEW 在、OLD 不在）；请按 DOC-081 人工回滚")


__all__ = [
    "JOURNAL_NAME",
    "MigrateIssue",
    "MigratePhase",
    "MigrateResult",
    "MigrateSnapshot",
    "PreflightReport",
    "assert_phase_transition",
    "capture_snapshot",
    "journal_path",
    "migrate_lock",
    "move_workspace_root",
    "next_phase",
    "preflight_migrate",
    "quiesce_workspace",
    "read_journal",
    "rebind_workspace_paths",
    "regenerate_after_move",
    "restore_instances",
    "rewrite_gateway_fragment_paths",
    "rewrite_manifest_paths",
    "rewrite_registry_paths",
    "run_migrate",
    "verify_migrate",
    "write_backup",
    "write_journal",
]
