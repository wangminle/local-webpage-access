"""网关后端原子切换（IMP-037 / DEV-082）。

将预检 → 快照 → 停旧 → 写 YAML → 启新 → 批量回写 manifest/registry →
access refresh/review → 审计事件收敛为单一事务；失败时回滚 YAML/进程，
回滚失败则标 degraded 并给出修复提示。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from local_webpage_access.access_workflow import AccessPassResult, run_access_pass
from local_webpage_access.config import Config, load_config
from local_webpage_access.errors import LifecycleError, LwaError
from local_webpage_access.file_lock import (
    ensure_lockable,
    release_exclusive,
    try_acquire_exclusive,
    write_lock_payload,
)
from local_webpage_access.gateway_service import start_gateway, stop_gateway
from local_webpage_access.logging import get_logger
from local_webpage_access.models import InstanceManifest, RouteMode, Runtime
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry
from local_webpage_access.static_gateway import StaticGateway

log = get_logger("gateway_switch")

_ALLOWED_BACKENDS = frozenset({"caddy", "builtin"})

SWITCH_LOCK_FILENAME = "gateway-switch.lock"
SWITCH_LOCK_TIMEOUT = 15.0


@contextlib.contextmanager
def _switch_lock(
    workspace: Workspace, *, timeout: float = SWITCH_LOCK_TIMEOUT
) -> Iterator[None]:
    """串行化完整 gateway 切换事务（BUG-514：并发切换会交错 YAML/manifest/进程）。"""
    path = workspace.run / SWITCH_LOCK_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    ensure_lockable(fd)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                try_acquire_exclusive(fd)
                write_lock_payload(fd, f"{os.getpid()}\n{time.time():.3f}\n".encode())
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LwaError(
                        "网关切换锁被占用，可能有另一个切换正在进行，请稍后重试",
                        code="GATEWAY_SWITCH_LOCKED",
                    )
                time.sleep(0.05)
        yield
    finally:
        release_exclusive(fd)
        with contextlib.suppress(OSError):
            os.close(fd)


def _caddy_available() -> bool:
    """PATH 中是否有 caddy 可执行文件（版本校验留给 CLI 切到 caddy 时）。"""
    return shutil.which("caddy") is not None


@dataclass
class GatewaySwitchPlan:
    """切换预检摘要（dry-run 亦返回此结构）。"""

    from_backend: str
    to_backend: str
    noop: bool = False
    instances: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fromBackend": self.from_backend,
            "toBackend": self.to_backend,
            "noop": self.noop,
            "instances": list(self.instances),
            "notes": list(self.notes),
        }


@dataclass
class GatewaySwitchSnapshot:
    """事务快照：YAML / gateway.json / 站点与别名片段 / manifest / registry。"""

    yml_text: str | None = None
    gateway_json_text: str | None = None
    site_confs: dict[str, str] = field(default_factory=dict)
    alias_confs: dict[str, str] = field(default_factory=dict)
    manifests: dict[str, str] = field(default_factory=dict)
    static_sites: dict[str, dict[str, Any]] = field(default_factory=dict)
    from_backend: str = ""


@dataclass
class GatewaySwitchResult:
    """切换结果：``ok`` 表示后端事务成功；访问复核单独见 ``access`` / ``access_ok``。"""

    ok: bool
    degraded: bool = False
    noop: bool = False
    from_backend: str = ""
    to_backend: str = ""
    stages: list[dict[str, Any]] = field(default_factory=list)
    access: AccessPassResult | None = None
    access_ok: bool | None = None
    error: str | None = None
    repair_hint: str | None = None
    plan: GatewaySwitchPlan | None = None

    @property
    def fully_ok(self) -> bool:
        """后端成功且（若跑了 access）无 refresh/review 失败。"""
        if not self.ok or self.degraded:
            return False
        if self.access_ok is False:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "ok": self.ok,
            "fullyOk": self.fully_ok,
            "degraded": self.degraded,
            "noop": self.noop,
            "fromBackend": self.from_backend,
            "toBackend": self.to_backend,
            "stages": list(self.stages),
        }
        if self.access_ok is not None:
            d["accessOk"] = self.access_ok
        if self.access is not None:
            d["access"] = self.access.to_dict()
        if self.error:
            d["error"] = self.error
        if self.repair_hint:
            d["repairHint"] = self.repair_hint
        if self.plan is not None:
            d["plan"] = self.plan.to_dict()
        return d


def _normalize_target(target: str) -> str:
    t = (target or "").strip().lower()
    if t not in _ALLOWED_BACKENDS:
        raise LwaError(
            f"不支持的网关后端：{target!r}（仅允许 caddy / builtin）",
            code="GATEWAY_BACKEND_INVALID",
            suggestion="使用：lwa gateway switch caddy 或 lwa gateway switch builtin",
        )
    return t


def _unloadable_running_static_manifests(
    workspace: Workspace, registry: Registry
) -> list[str]:
    """返回「运行中」但 manifest 无法加载的静态实例 ID（BUG-516）。

    切到 builtin 时这些实例无进程承接，会静默下线。已停止的损坏 manifest
    不会导致下线，不阻断切换。
    """
    ids: list[str] = []
    for row in registry.list_instances():
        if row.get("runtime") != Runtime.SHARED_STATIC.value:
            continue
        status = row.get("status")
        desired = row.get("desired_state")
        if status != "running" and desired != "running":
            continue
        iid = row["id"]
        path = workspace.app_manifest_path(iid)
        if not path.is_file():
            continue
        try:
            InstanceManifest.load(path)
        except Exception:  # noqa: BLE001
            ids.append(iid)
    return ids


def _iter_static_instances(
    workspace: Workspace, registry: Registry
) -> list[tuple[str, InstanceManifest]]:
    out: list[tuple[str, InstanceManifest]] = []
    for row in registry.list_instances():
        iid = row["id"]
        path = workspace.app_manifest_path(iid)
        if not path.is_file():
            continue
        try:
            manifest = InstanceManifest.load(path)
        except Exception as exc:  # noqa: BLE001
            log.warning("跳过无法加载的 manifest %s：%s", iid, exc)
            continue
        rt = (
            manifest.runtime.value
            if hasattr(manifest.runtime, "value")
            else str(manifest.runtime)
        )
        if rt != Runtime.SHARED_STATIC.value:
            continue
        if manifest.static is None:
            continue
        out.append((iid, manifest))
    return out


def _is_running_static(manifest: InstanceManifest, row: dict[str, Any] | None) -> bool:
    """是否视为运行中（需在切到 builtin 时 enable）。"""
    status = (row or {}).get("status") or (
        manifest.status.value if hasattr(manifest.status, "value") else str(manifest.status)
    )
    desired = (row or {}).get("desired_state") or (
        manifest.desiredState.value
        if hasattr(manifest.desiredState, "value")
        else str(manifest.desiredState)
    )
    return status == "running" or desired == "running"


def plan_switch(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    target: str,
) -> GatewaySwitchPlan:
    """预检并输出变更摘要（不写盘）。"""
    to_backend = _normalize_target(target)
    from_backend = (config.staticGateway or "caddy").strip().lower()
    if from_backend not in _ALLOWED_BACKENDS:
        from_backend = "caddy" if from_backend == "nginx" else from_backend

    plan = GatewaySwitchPlan(from_backend=from_backend, to_backend=to_backend)
    if from_backend == to_backend:
        plan.noop = True
        plan.notes.append(f"已是 {to_backend}，无需切换")
        return plan

    if to_backend == "caddy" and not _caddy_available():
        raise LwaError(
            "未找到 caddy 可执行文件，无法切换到 caddy 后端",
            code="GATEWAY_CADDY_MISSING",
            suggestion="安装 Caddy 并加入 PATH，或改用 lwa gateway switch builtin",
        )

    # BUG-516：损坏 manifest 的*运行中*静态实例切到 builtin 时无进程承接。
    # 仅在该场景 fail-closed；切到 caddy 或已停止实例不因此阻断。
    if to_backend == "builtin":
        unloadable = _unloadable_running_static_manifests(workspace, registry)
        if unloadable:
            raise LwaError(
                f"以下运行中静态实例 manifest 无法加载，切到 builtin 后可能下线："
                f"{', '.join(unloadable)}；请先修复或删除这些实例",
                code="GATEWAY_MANIFEST_UNLOADABLE",
            )

    for iid, manifest in _iter_static_instances(workspace, registry):
        st = manifest.static
        assert st is not None
        row = registry.get_instance(iid)
        plan.instances.append(
            {
                "id": iid,
                "hostPort": st.hostPort,
                "routeHost": st.routeHost,
                "routeMode": st.routeMode,
                "gateway": st.gateway,
                "running": _is_running_static(manifest, row),
                "action": (
                    "enable_builtin"
                    if to_backend == "builtin" and _is_running_static(manifest, row)
                    else (
                        "rebuild_alias"
                        if to_backend == "caddy"
                        and st.routeMode == RouteMode.NAME.value
                        and st.routeHost
                        else "sync_gateway_field"
                    )
                ),
            }
        )

    if to_backend == "builtin":
        plan.notes.append("切到 builtin：保留 routeHost 元数据，别名入口未激活")
    else:
        plan.notes.append("切到 caddy：按 manifest 重建 name 模式别名片段")
    return plan


def _take_snapshot(
    workspace: Workspace, registry: Registry, from_backend: str
) -> GatewaySwitchSnapshot:
    snap = GatewaySwitchSnapshot(from_backend=from_backend)
    cfg_path = workspace.config_path
    if cfg_path.is_file():
        snap.yml_text = cfg_path.read_text(encoding="utf-8")
    gw_path = workspace.run / "gateway.json"
    if gw_path.is_file():
        snap.gateway_json_text = gw_path.read_text(encoding="utf-8")
    sites = workspace.static_gateway / "sites"
    if sites.is_dir():
        for p in sites.glob("*.conf"):
            snap.site_confs[p.name] = p.read_text(encoding="utf-8")
    aliases = workspace.static_gateway / "aliases"
    if aliases.is_dir():
        for p in aliases.glob("*.conf"):
            snap.alias_confs[p.name] = p.read_text(encoding="utf-8")
    # BUG-515：快照 manifest 文件与 registry static_sites 行；_sync_manifests_gateway
    # 会改写它们，回滚时必须一并还原，否则留下 manifest/registry 与 config 后端矛盾。
    for iid, _manifest in _iter_static_instances(workspace, registry):
        mpath = workspace.app_manifest_path(iid)
        if mpath.is_file():
            snap.manifests[iid] = mpath.read_text(encoding="utf-8")
        row = registry.get_static_site(iid)
        if row is not None:
            snap.static_sites[iid] = dict(row)
    return snap


def _backup_yml(workspace: Workspace, yml_text: str | None) -> Path | None:
    if yml_text is None:
        return None
    backup = workspace.config_path.with_suffix(".yml.bak")
    backup.write_text(yml_text, encoding="utf-8")
    return backup


def _write_config_backend(workspace: Workspace, config: Config, backend: str) -> Config:
    config.staticGateway = backend
    config.save(workspace.config_path)
    return load_config(workspace)


def _sync_manifests_gateway(
    workspace: Workspace,
    registry: Registry,
    backend: str,
) -> list[str]:
    """批量回写 manifest.static.gateway + registry static_sites.gateway。"""
    updated: list[str] = []
    for iid, manifest in _iter_static_instances(workspace, registry):
        assert manifest.static is not None
        if manifest.static.gateway == backend:
            # 仍 upsert 一次，确保 registry 一致
            registry.upsert_static_site(iid, manifest.static.model_dump())
            continue
        manifest.static.gateway = backend
        manifest.touch()
        manifest.save(workspace.app_manifest_path(iid))
        registry.upsert_static_site(iid, manifest.static.model_dump())
        updated.append(iid)
    return updated


def _enable_running_builtin(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    gateway: StaticGateway,
) -> list[str]:
    enabled: list[str] = []
    for iid, manifest in _iter_static_instances(workspace, registry):
        row = registry.get_instance(iid)
        if not _is_running_static(manifest, row):
            continue
        st = manifest.static
        assert st is not None
        if st.hostPort is None:
            log.warning("实例 %s 无 hostPort，跳过 builtin enable", iid)
            continue
        public = workspace.app_public(iid)
        if not public.is_dir():
            # 兜底：部分测试/历史布局把 public 放在 current/public
            alt = workspace.app_current(iid) / "public"
            public = alt if alt.is_dir() else public
        alias = (
            st.routeHost
            if st.routeMode == RouteMode.NAME.value and st.routeHost
            else None
        )
        gateway.enable(
            iid,
            int(st.hostPort),
            public,
            wait_health=True,
            alias=alias,
        )
        enabled.append(iid)
    return enabled


def _rebuild_caddy_aliases(
    workspace: Workspace,
    registry: Registry,
    gateway: StaticGateway,
) -> list[str]:
    rebuilt: list[str] = []
    for iid, manifest in _iter_static_instances(workspace, registry):
        st = manifest.static
        assert st is not None
        if st.routeMode != RouteMode.NAME.value or not st.routeHost:
            continue
        if st.hostPort is None:
            continue
        gateway.generate_alias_config(iid, st.routeHost, int(st.hostPort))
        rebuilt.append(iid)
    if rebuilt:
        try:
            gateway.reload_all()
        except Exception as exc:  # noqa: BLE001
            log.warning("重建别名后 reload 失败：%s", exc)
            # 主配置缺失时尝试 sync；sync 本身会吞掉 reload 失败（软失败），
            # 必须再调用 reload_all 把失败上抛，避免 switch 报 ok 但别名未生效（BUG-353）。
            try:
                gateway._sync_main_config()
                gateway.reload_all()
            except Exception as exc2:  # noqa: BLE001
                log.warning("sync/reload 别名主配置失败：%s", exc2)
                raise
    return rebuilt


def _static_row_to_upsert_dict(row: dict[str, Any]) -> dict[str, Any]:
    """把 static_sites 表行还原为 ``upsert_static_site`` 需要的 manifest.static 形态。"""
    return {
        "root": row.get("root_path", "public"),
        "gateway": row.get("gateway", "caddy"),
        "routeMode": row.get("route_mode", "port"),
        "hostPort": row.get("host_port"),
        "routeHost": row.get("route_host"),
        "gatewayConfigPath": row.get("gateway_config_path"),
        "enabled": bool(row.get("enabled", 1)),
    }


def _restore_snapshot(
    workspace: Workspace, registry: Registry, snap: GatewaySwitchSnapshot
) -> None:
    if snap.yml_text is not None:
        workspace.config_path.write_text(snap.yml_text, encoding="utf-8")
    gw_path = workspace.run / "gateway.json"
    if snap.gateway_json_text is not None:
        gw_path.parent.mkdir(parents=True, exist_ok=True)
        gw_path.write_text(snap.gateway_json_text, encoding="utf-8")
    sites = workspace.static_gateway / "sites"
    sites.mkdir(parents=True, exist_ok=True)
    for name, text in snap.site_confs.items():
        (sites / name).write_text(text, encoding="utf-8")
    aliases = workspace.static_gateway / "aliases"
    aliases.mkdir(parents=True, exist_ok=True)
    for name, text in snap.alias_confs.items():
        (aliases / name).write_text(text, encoding="utf-8")
    # BUG-515：还原 manifest 文件与 registry static_sites 行，避免回滚后矛盾状态。
    for iid, text in snap.manifests.items():
        path = workspace.app_manifest_path(iid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for iid, row in snap.static_sites.items():
        registry.upsert_static_site(iid, _static_row_to_upsert_dict(row))


def _rollback(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    snap: GatewaySwitchSnapshot,
    *,
    stages: list[dict[str, Any]],
) -> tuple[bool, str | None]:
    """恢复 YAML/片段并尝试拉起旧后端。返回 (rollback_ok, repair_hint)。"""
    try:
        _restore_snapshot(workspace, registry, snap)
        stages.append({"stage": "rollback_files", "ok": True})
    except Exception as exc:  # noqa: BLE001
        stages.append({"stage": "rollback_files", "ok": False, "error": str(exc)})
        return False, (
            f"回滚配置文件失败：{exc}；请手工恢复 {workspace.config_path} "
            f"（或 .yml.bak）后执行 lwa gateway {'on' if snap.from_backend == 'caddy' else 'off'}"
        )

    restored = load_config(workspace)
    try:
        if snap.from_backend == "caddy":
            start_gateway(workspace, restored, registry=None)
            stages.append({"stage": "rollback_start_caddy", "ok": True})
        else:
            # 旧后端 builtin：确保无残留 caddy master
            stop_gateway(workspace, restored)
            gw = StaticGateway(workspace, restored)
            _enable_running_builtin(workspace, restored, registry, gw)
            stages.append({"stage": "rollback_enable_builtin", "ok": True})
        review_result = run_access_pass(
            workspace, restored, registry, review=True, dry_run=False
        )
        if _access_ok(review_result) is False:
            raise LifecycleError("回滚后的访问复核失败")
        return True, None
    except Exception as exc:  # noqa: BLE001
        stages.append({"stage": "rollback_process", "ok": False, "error": str(exc)})
        hint = (
            f"配置已恢复为 {snap.from_backend}，但进程回滚失败：{exc}。"
            f"请执行：lwa gateway {'on' if snap.from_backend == 'caddy' else 'off'}；"
            f"必要时检查 lwa doctor"
        )
        return False, hint


def _access_ok(access: AccessPassResult | None) -> bool | None:
    if access is None:
        return None
    if access.refresh_error or access.review_error:
        return False
    if access.review is not None and getattr(access.review, "has_failures", False):
        return False
    return True


def _record_event(
    registry: Registry,
    *,
    from_backend: str,
    to_backend: str,
    ok: bool,
    degraded: bool,
    noop: bool,
    error: str | None = None,
) -> None:
    try:
        parts = [
            f"from={from_backend}",
            f"to={to_backend}",
            f"ok={'yes' if ok else 'no'}",
        ]
        if noop:
            parts.append("noop=yes")
        if degraded:
            parts.append("degraded=yes")
        if error:
            parts.append(f"error={error[:200]}")
        registry.add_event(None, "gateway_backend_switch", "；".join(parts))
    except Exception as exc:  # noqa: BLE001
        log.debug("记录 gateway_backend_switch 失败：%s", exc)


def switch_gateway(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    target: str,
    *,
    dry_run: bool = False,
    review: bool = True,
) -> GatewaySwitchResult:
    """执行网关后端原子切换。

    ``ok``：后端事务成功（含 noop / dry-run 预检通过）。
    ``access_ok`` / ``fully_ok``：访问复核是否一并成功（review 失败不伪装整体全绿）。
    """
    stages: list[dict[str, Any]] = []
    try:
        plan = plan_switch(workspace, config, registry, target)
    except LwaError as exc:
        return GatewaySwitchResult(
            ok=False,
            from_backend=(config.staticGateway or ""),
            to_backend=_normalize_target(target) if target.strip().lower() in _ALLOWED_BACKENDS else target,
            error=str(exc),
            stages=[{"stage": "precheck", "ok": False, "error": str(exc)}],
        )

    result = GatewaySwitchResult(
        ok=True,
        from_backend=plan.from_backend,
        to_backend=plan.to_backend,
        plan=plan,
        stages=stages,
    )

    if plan.noop:
        result.noop = True
        stages.append({"stage": "noop", "ok": True})
        _record_event(
            registry,
            from_backend=plan.from_backend,
            to_backend=plan.to_backend,
            ok=True,
            degraded=False,
            noop=True,
        )
        return result

    if dry_run:
        stages.append({"stage": "dry_run", "ok": True, "plan": plan.to_dict()})
        return result

    try:
        # BUG-514：完整切换事务持跨进程锁，防止 CLI 与 API 并发交错。
        with _switch_lock(workspace):
            return _switch_gateway_locked(
                workspace, config, registry, plan, result, stages, review
            )
    except Exception as exc:  # noqa: BLE001
        is_lock = (
            isinstance(exc, LwaError) and exc.code == "GATEWAY_SWITCH_LOCKED"
        )
        stage = "switch_lock" if is_lock else "failed"
        log.exception(
            "网关切换%s失败：%s",
            "锁获取" if is_lock else "",
            exc,
        )
        result.ok = False
        result.error = str(exc)
        stages.append({"stage": stage, "ok": False, "error": str(exc)})
        _record_event(
            registry,
            from_backend=plan.from_backend,
            to_backend=plan.to_backend,
            ok=False,
            degraded=False,
            noop=False,
            error=str(exc),
        )
        return result


def _switch_gateway_locked(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    plan: GatewaySwitchPlan,
    result: GatewaySwitchResult,
    stages: list[dict[str, Any]],
    review: bool,
) -> GatewaySwitchResult:
    """在切换锁内执行事务：快照 → 停旧 → 写 YAML → 启新 → 回写 → 访问复核 → 回滚。"""
    try:
        snap = _take_snapshot(workspace, registry, plan.from_backend)
        stages.append({"stage": "snapshot", "ok": True})
    except Exception as exc:  # noqa: BLE001
        log.exception("创建网关切换快照失败：%s", exc)
        result.ok = False
        result.error = str(exc)
        stages.append({"stage": "snapshot", "ok": False, "error": str(exc)})
        _record_event(
            registry,
            from_backend=plan.from_backend,
            to_backend=plan.to_backend,
            ok=False,
            degraded=False,
            noop=False,
            error=str(exc),
        )
        return result

    try:
        _backup_yml(workspace, snap.yml_text)
    except Exception as exc:  # noqa: BLE001
        log.exception("备份网关配置失败：%s", exc)
        result.ok = False
        result.error = str(exc)
        stages.append({"stage": "backup_yml", "ok": False, "error": str(exc)})
        _record_event(
            registry,
            from_backend=plan.from_backend,
            to_backend=plan.to_backend,
            ok=False,
            degraded=False,
            noop=False,
            error=str(exc),
        )
        return result

    live_config = config
    try:
        if plan.to_backend == "builtin":
            # caddy → builtin
            if not stop_gateway(workspace, live_config):
                raise LwaError(
                    "停止 Caddy master 失败",
                    code="GATEWAY_STOP_FAIL",
                    suggestion="检查 admin :2019；可重试 lwa gateway off",
                )
            stages.append({"stage": "stop_caddy", "ok": True})

            live_config = _write_config_backend(workspace, live_config, "builtin")
            stages.append({"stage": "write_yaml", "ok": True, "backend": "builtin"})

            gw = StaticGateway(workspace, live_config)
            enabled = _enable_running_builtin(workspace, live_config, registry, gw)
            stages.append(
                {"stage": "enable_builtin", "ok": True, "instances": enabled}
            )

            synced = _sync_manifests_gateway(workspace, registry, "builtin")
            stages.append(
                {"stage": "sync_manifests", "ok": True, "updated": synced}
            )

        else:
            # builtin → caddy
            live_config = _write_config_backend(workspace, live_config, "caddy")
            stages.append({"stage": "write_yaml", "ok": True, "backend": "caddy"})

            # 先启 master（不传 registry，避免 finalize 与本事务重复 refresh）
            pid = start_gateway(workspace, live_config, registry=None)
            stages.append({"stage": "start_caddy", "ok": True, "pid": pid})

            gw = StaticGateway(workspace, live_config)
            rebuilt = _rebuild_caddy_aliases(workspace, registry, gw)
            stages.append(
                {"stage": "rebuild_aliases", "ok": True, "instances": rebuilt}
            )

            synced = _sync_manifests_gateway(workspace, registry, "caddy")
            stages.append(
                {"stage": "sync_manifests", "ok": True, "updated": synced}
            )

        # access 收尾
        access = run_access_pass(
            workspace, live_config, registry, review=review, dry_run=False
        )
        result.access = access
        result.access_ok = _access_ok(access)
        stages.append(
            {
                "stage": "access_pass",
                "ok": result.access_ok is not False,
                "accessOk": result.access_ok,
            }
        )

        _record_event(
            registry,
            from_backend=plan.from_backend,
            to_backend=plan.to_backend,
            ok=True,
            degraded=False,
            noop=False,
        )
        return result

    except Exception as exc:  # noqa: BLE001
        log.exception("网关切换失败，尝试回滚：%s", exc)
        stages.append({"stage": "failed", "ok": False, "error": str(exc)})
        rb_ok, hint = _rollback(
            workspace, config, registry, snap, stages=stages
        )
        result.ok = False
        result.error = str(exc)
        if not rb_ok:
            result.degraded = True
            result.repair_hint = hint
        else:
            result.repair_hint = None
            result.degraded = False
        _record_event(
            registry,
            from_backend=plan.from_backend,
            to_backend=plan.to_backend,
            ok=False,
            degraded=result.degraded,
            noop=False,
            error=str(exc),
        )
        return result
