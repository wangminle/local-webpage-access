"""副本一致性巡检（S3-02 / DEV-158 / issue #57-5 P-2）。

LWA 的同一事实在多个副本中各存一份：manifest（local-web.json）、registry（SQLite）、
生成物（compose / Dockerfile / .env）、运行中容器的挂载、网关别名片段与主 Caddyfile。
任一副本漂移都会表现为“配置改了不生效”或“残留片段仍在服务”。本模块只读检查这些副本，
输出 :class:`Drift` 列表；**不自动修复**，修复动作由用户或既有命令（restart / gateway on）执行。

检查族：

* ``manifest_registry``：manifest 缺失/损坏，status / desiredState / runtime / hostPort 与 registry 不一致；
* ``artifacts``：生成物疑似手工修改（盘上 configHash 与部署基线不一致，且生成物比 manifest 新，或 Dockerfile/.env 缺失）；
* ``mounts``：extraVolumes 未出现在运行容器中（需 docker，仅 doctor 传入 runner 时执行）；
* ``alias``：运行中实例的别名片段缺失、与 manifest 渲染结果不一致，或实例已无别名却残留片段（已停止的实例缺少片段不算漂移）；
* ``fragments``：孤儿片段（文件存在但实例已不在登记中）；
* ``caddy``：主 Caddyfile 的 import 指向不存在的文件，或片段未被主配置引用（不生效）。

``mounts`` 的 docker 探测只在 doctor 中执行；daemon 周期巡检不调用 docker，避免常驻进程高频查询。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_webpage_access.config import Config
from local_webpage_access.logging import get_logger
from local_webpage_access.models import InstanceManifest
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry

log = get_logger("consistency")

FAMILY_MANIFEST = "manifest_registry"
FAMILY_ARTIFACTS = "artifacts"
FAMILY_MOUNTS = "mounts"
FAMILY_ALIAS = "alias"
FAMILY_FRAGMENTS = "fragments"
FAMILY_CADDY = "caddy"

FAMILY_LABELS = {
    FAMILY_MANIFEST: "manifest/registry",
    FAMILY_ARTIFACTS: "生成物",
    FAMILY_MOUNTS: "容器挂载",
    FAMILY_ALIAS: "别名片段",
    FAMILY_FRAGMENTS: "孤儿片段",
    FAMILY_CADDY: "主 Caddyfile",
}

#: daemon 周期巡检的漂移事件类型（事件表 event_type）
CONSISTENCY_EVENT_TYPE = "consistency_drift"

_IMPORT_RE = re.compile(r"^\s*import\s+(?P<target>`[^`]*`|\"(?:[^\"\\]|\\.)*\"|\S+)\s*$")
_VOLUME_RE = re.compile(r"^(?P<src>.+?):(?P<dst>/[^:]*)(?::(?:ro|rw))?$")


@dataclass(frozen=True)
class Drift:
    family: str
    instance_id: str | None
    message: str
    suggestion: str | None = None

    def to_line(self) -> str:
        scope = self.instance_id or "全局"
        return f"[{FAMILY_LABELS.get(self.family, self.family)}] {scope}：{self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "instanceId": self.instance_id,
            "message": self.message,
            "suggestion": self.suggestion,
        }


def _enum_value(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _unquote_caddy(raw: str) -> str:
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("`", '"'):
        inner = raw[1:-1]
        if raw[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return raw


def check_manifest_registry(
    ws: Workspace, registry: Registry, manifests: dict[str, InstanceManifest]
) -> list[Drift]:
    """manifest 与 registry 的身份字段比对；成功读取的 manifest 写入 ``manifests``。"""
    drifts: list[Drift] = []
    for row in registry.list_instances():
        iid = row["id"]
        path = ws.app_manifest_path(iid)
        if not path.is_file():
            drifts.append(
                Drift(
                    FAMILY_MANIFEST,
                    iid,
                    "registry 有登记，但 local-web.json 缺失",
                    "确认实例是否仍需要；需要则从备份恢复 manifest，不需要则 lwa remove",
                )
            )
            continue
        try:
            manifest = InstanceManifest.load(path)
        except Exception as exc:  # noqa: BLE001 — 损坏 manifest 作为漂移项呈现
            drifts.append(
                Drift(
                    FAMILY_MANIFEST,
                    iid,
                    f"manifest 读取失败：{exc}",
                    "修复或从备份恢复 local-web.json 后重新执行 lwa status 确认",
                )
            )
            continue
        manifests[iid] = manifest

        for field_name, expected, actual in (
            ("status", _enum_value(manifest.status), row.get("status")),
            ("desiredState", _enum_value(manifest.desiredState), row.get("desired_state")),
            ("runtime", _enum_value(manifest.runtime), row.get("runtime")),
        ):
            if actual is not None and expected != actual:
                drifts.append(
                    Drift(
                        FAMILY_MANIFEST,
                        iid,
                        f"{field_name} 不一致：manifest={expected} registry={actual}",
                        "执行 lwa status <id> 让 registry 从观测结果回写；仍不一致时检查最近事件",
                    )
                )

        port_row: dict[str, Any] | None = None
        expected_port: int | None = None
        if manifest.container is not None:
            port_row = registry.get_container(iid)
            expected_port = manifest.container.hostPort
            if port_row is None:
                drifts.append(
                    Drift(
                        FAMILY_MANIFEST,
                        iid,
                        "manifest 为容器实例，但 registry 缺少 containers 记录",
                        "执行 lwa status <id> 或 lwa restart <id> 重新登记",
                    )
                )
        elif manifest.static is not None:
            port_row = registry.get_static_site(iid)
            expected_port = manifest.static.hostPort
        if port_row is not None:
            actual_port = port_row.get("host_port")
            if (
                expected_port is not None
                and actual_port is not None
                and int(expected_port) != int(actual_port)
            ):
                drifts.append(
                    Drift(
                        FAMILY_MANIFEST,
                        iid,
                        f"hostPort 不一致：manifest={expected_port} registry={actual_port}",
                        "执行 lwa restart <id> 以 manifest 端口重新登记",
                    )
                )
    return drifts


def check_artifacts(
    ws: Workspace, manifests: dict[str, InstanceManifest]
) -> list[Drift]:
    """生成物疑似手工修改：盘上 configHash 偏离部署基线，且生成物比 manifest 新。

    mtime 条件用于排除“刚 configure 改了 manifest、尚未 restart”的正常待生效状态。
    """
    from local_webpage_access.lifecycle import _compute_config_fingerprint

    drifts: list[Drift] = []
    for iid, manifest in manifests.items():
        if manifest.container is None:
            continue
        fps = getattr(manifest, "deploymentFingerprints", None) or {}
        stored = fps.get("configHash") if isinstance(fps, dict) else None
        if not stored:
            continue
        compose = ws.app_compose_path(iid)
        if not compose.is_file():
            drifts.append(
                Drift(
                    FAMILY_ARTIFACTS,
                    iid,
                    "部署基线存在，但 compose.yaml 缺失",
                    "lwa restart <id> 按 manifest 重新渲染生成物",
                )
            )
            continue
        current = _compute_config_fingerprint(
            compose, ws.app_dockerfile_path(iid), ws.app_env_path(iid)
        )
        if current == stored:
            continue
        manifest_path = ws.app_manifest_path(iid)
        dockerfile = ws.app_dockerfile_path(iid)
        env_path = ws.app_env_path(iid)
        missing = [p.name for p in (dockerfile, env_path) if not p.is_file()]
        existing_times = [
            p.stat().st_mtime for p in (compose, dockerfile, env_path) if p.is_file()
        ]
        artifact_mtime = max(existing_times) if existing_times else 0.0
        newer = (
            not manifest_path.is_file()
            or artifact_mtime > manifest_path.stat().st_mtime
            or bool(missing)
        )
        if newer:
            drifts.append(
                Drift(
                    FAMILY_ARTIFACTS,
                    iid,
                    "生成物与最近一次部署基线不一致（疑似手工修改，下次 start/restart 会按 manifest 覆盖）",
                    "需要持久化的改动请用 manifest 字段（如 lwa configure --extra-volume）",
                )
            )
    return drifts


def check_mounts(
    registry: Registry,
    manifests: dict[str, InstanceManifest],
    runner: Any,
) -> list[Drift]:
    """extraVolumes 的目标路径是否出现在运行容器的 Mounts 中（docker inspect）。"""
    drifts: list[Drift] = []
    for iid, manifest in manifests.items():
        if manifest.container is None or not manifest.container.extraVolumes:
            continue
        row = registry.get_container(iid) or {}
        container_id = row.get("container_id")
        if not container_id:
            continue
        try:
            result = runner(
                ["docker", "inspect", "-f", "{{json .Mounts}}", str(container_id)],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if result.returncode != 0:
                continue
            mounts = json.loads(result.stdout or "[]")
        except (OSError, ValueError, TypeError) as exc:
            log.debug("挂载巡检跳过 %s：%s", iid, exc)
            continue
        destinations = {str(item.get("Destination")) for item in mounts if isinstance(item, dict)}
        for entry in manifest.container.extraVolumes:
            match = _VOLUME_RE.match(entry.strip())
            if match is None:
                continue
            dst = match.group("dst")
            if dst not in destinations:
                drifts.append(
                    Drift(
                        FAMILY_MOUNTS,
                        iid,
                        f"extraVolumes 目标 {dst} 未出现在运行容器中（配置已改但未生效）",
                        f"lwa restart {iid}（挂载变更在 restart 时生效）",
                    )
                )
    return drifts


def check_aliases(ws: Workspace, config: Config, registry: Registry) -> list[Drift]:
    """别名片段与 manifest 的别名/端口一致性（渲染结果逐字比对）。

    以**实际后端**为准（BUG-815）：配置 caddy 但二进制缺失被降级 builtin 时，
    别名不走 Caddy 片段，缺失/不一致/残留均不构成漂移。只对在服务
    （running/degraded/verifying）的实例做逐字比对；未在服务实例的片段按形态
    区分：静态 ``lwa stop`` 经 gateway.disable 撤掉片段，明确 stopped 后残片段
    是异常；building 等操作中状态的片段去向由操作收尾决定，不预判（BUG-825）；
    容器 ``lwa stop`` 有意不动网关（片段保留、start 即恢复），不报。
    """
    from local_webpage_access.gateway_switch import _iter_alias_route_instances
    from local_webpage_access.static_gateway import StaticGateway

    drifts: list[Drift] = []
    gateway = StaticGateway(ws, config)
    try:
        backend = gateway.detect_backend()
    except Exception:  # noqa: BLE001 — 后端无法判定（如 Full 配置冲突）时不做片段级巡检
        return []
    if backend != "caddy":
        return []
    expected: set[str] = set()
    serving = {"running", "degraded", "verifying"}
    alias_info: dict[str, tuple[str, str]] = {}  # iid → (runtime, status)
    for iid, manifest, alias, host_port in _iter_alias_route_instances(ws, registry):
        row = registry.get_instance(iid) or {}
        status = str(row.get("status") or _enum_value(manifest.status) or "").lower()
        alias_info[iid] = (_enum_value(manifest.runtime) or "", status)
        if status not in serving:
            # 未在服务的实例缺少片段不算漂移（静态 stop 撤片段 / 容器 stop 留片段）
            continue
        expected.add(iid)
        path = ws.app_alias_config(iid)
        if not path.is_file():
            drifts.append(
                Drift(
                    FAMILY_ALIAS,
                    iid,
                    f"别名 /{alias}/ 缺少片段文件 {path.name}",
                    "执行 lwa gateway on 或 lwa restart <id> 重新生成片段",
                )
            )
            continue
        desired = gateway.render_alias_fragment(
            iid, alias, host_port, runtime=_enum_value(manifest.runtime) or ""
        )
        if path.read_text(encoding="utf-8") != desired:
            drifts.append(
                Drift(
                    FAMILY_ALIAS,
                    iid,
                    f"别名片段与 manifest 不一致（别名 /{alias}/，端口 {host_port}）",
                    "执行 lwa gateway on 重写片段并 reload",
                )
            )
    for path in sorted(ws.static_aliases.glob("*.conf")):
        iid = path.stem
        if iid in expected or not registry.instance_exists(iid):
            continue
        info = alias_info.get(iid)
        if info is None:
            # 实例不再配置任何路径别名，片段属于上一形态的残留
            drifts.append(
                Drift(
                    FAMILY_ALIAS,
                    iid,
                    "实例已无路径别名，但别名片段仍残留",
                    "执行 lwa gateway on 重新生成主配置（残留片段将不再被 import）",
                )
            )
        elif info[0] == "shared-static" and info[1] == "stopped":
            # 仅明确 stopped 才判残留：building/failed/quiescing 等操作中状态
            # 的片段去向由操作收尾决定，预判「撤除未完成」会误报（BUG-825）。
            drifts.append(
                Drift(
                    FAMILY_ALIAS,
                    iid,
                    "实例已停止，但别名片段仍残留"
                    "（静态 stop 会撤掉片段，残留说明上次撤除未完成）",
                    f"重新执行 lwa stop {iid} 收敛残留，或 lwa start {iid} 恢复服务",
                )
            )
        # docker-compose（任何状态）：stop 有意保留别名片段，不报
    return drifts


def check_fragments(ws: Workspace, registry: Registry) -> list[Drift]:
    """孤儿片段：站点 / 别名片段存在，但对应实例已不在登记中。"""
    drifts: list[Drift] = []
    for folder, label in ((ws.static_sites, "站点"), (ws.static_aliases, "别名")):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.conf")):
            if registry.instance_exists(path.stem):
                continue
            drifts.append(
                Drift(
                    FAMILY_FRAGMENTS,
                    path.stem,
                    f"{label}片段 {path.name} 的实例已不在登记中",
                    "确认无用后删除该片段文件，再执行 lwa gateway on 重新加载",
                )
            )
    return drifts


def check_caddy(ws: Workspace, config: Config, registry: Registry) -> list[Drift]:
    """主 Caddyfile：import 目标须存在；已登记实例的片段须被 import（否则不生效）。

    以**实际后端**为准（BUG-815）：配置 caddy 但被降级 builtin 时，Caddyfile
    不被任何进程加载，import 关系不构成运行漂移。
    """
    from local_webpage_access.static_gateway import StaticGateway

    try:
        backend = StaticGateway(ws, config).detect_backend()
    except Exception:  # noqa: BLE001 — 后端无法判定时不做主配置巡检
        return []
    if backend != "caddy":
        return []
    main = ws.static_gateway / "Caddyfile"
    if not main.is_file():
        return []
    imported: set[Path] = set()
    drifts: list[Drift] = []
    for line in main.read_text(encoding="utf-8").splitlines():
        match = _IMPORT_RE.match(line)
        if match is None:
            continue
        target = Path(_unquote_caddy(match.group("target")))
        imported.add(target.resolve())
        if not target.is_file():
            drifts.append(
                Drift(
                    FAMILY_CADDY,
                    None,
                    f"主 Caddyfile import 的文件不存在：{target}",
                    "执行 lwa gateway on 重新生成主配置并 reload",
                )
            )
    for folder in (ws.static_sites, ws.static_aliases):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.conf")):
            if not registry.instance_exists(path.stem):
                continue
            if path.resolve() not in imported:
                drifts.append(
                    Drift(
                        FAMILY_CADDY,
                        path.stem,
                        f"片段 {path.name} 未被主 Caddyfile 引用，不会生效",
                        "执行 lwa gateway on 重新生成主配置并 reload",
                    )
                )
    return drifts


def collect_drifts(
    ws: Workspace,
    config: Config,
    registry: Registry,
    *,
    runner: Any | None = None,
) -> list[Drift]:
    """运行全部副本检查；``runner`` 为空时跳过容器挂载检查（不触发 docker）。"""
    manifests: dict[str, InstanceManifest] = {}
    drifts = check_manifest_registry(ws, registry, manifests)
    drifts += check_artifacts(ws, manifests)
    if runner is not None:
        drifts += check_mounts(registry, manifests, runner)
    drifts += check_aliases(ws, config, registry)
    drifts += check_fragments(ws, registry)
    drifts += check_caddy(ws, config, registry)
    return drifts


_LAST_SIGNATURE: dict[str, tuple[str, ...]] = {}


def run_consistency_patrol(workspace: Workspace, config: Config, registry: Registry) -> list[Drift]:
    """daemon 周期巡检入口：只读检查，并在漂移集合变化时写 ``consistency_drift`` 事件。

    同一组漂移只记录一次（按签名去重），避免每 30 分钟重复刷屏；漂移消除时记 INFO 日志。
    """
    drifts = collect_drifts(workspace, config, registry)
    signature = tuple(sorted(d.to_line() for d in drifts))
    key = str(workspace.root)
    previous = _LAST_SIGNATURE.get(key)
    if previous == signature:
        return drifts
    for drift in drifts:
        # BUG-798：events.instance_id 有外键（REFERENCES instances ON DELETE
        # CASCADE）——孤儿片段等未登记实例的漂移必须落 instance_id=NULL，
        # 否则 add_event 直接违反约束抛错；消息行内已含片段/实例名，可读性不损失。
        target_iid = drift.instance_id if (
            drift.instance_id is not None and registry.instance_exists(drift.instance_id)
        ) else None
        registry.add_event(
            target_iid,
            CONSISTENCY_EVENT_TYPE,
            drift.to_line()[:300],
            source="daemon",
        )
    # 去重签名只在事件成功写入后缓存：中途失败时下一轮重记（宁可重复，不可静默）。
    _LAST_SIGNATURE[key] = signature
    if drifts:
        log.warning("副本一致性巡检发现 %d 项漂移（只读，未自动修复）", len(drifts))
    elif previous:
        log.info("副本一致性巡检：此前的漂移已消除")
    return drifts


__all__ = [
    "CONSISTENCY_EVENT_TYPE",
    "Drift",
    "collect_drifts",
    "run_consistency_patrol",
]
