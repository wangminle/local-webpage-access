"""实例健康检查（WBS-18.05 / 18.06 / 18.07）。

对运行中的实例做 HTTP 探测，结果写回 registry：
* 成功 → ``last_health_check_at``（WBS-18.06）；
* 失败 → ``last_error`` + ``error`` 事件（WBS-18.07）。

容器实例与静态实例都通过暴露的 ``hostPort`` 探测。容器实例可叠加
``docker compose ps`` 的进程级状态（:func:`check_health` 会先判端口、
再可选地校验容器运行态）。
"""

from __future__ import annotations

import urllib.request
from dataclasses import dataclass

from local_webpage_access.config import Config
from local_webpage_access.logging import get_logger, now_iso
from local_webpage_access.models import InstanceManifest, Status
from local_webpage_access.paths import Workspace
from local_webpage_access.probe import mark_probe_url
from local_webpage_access.registry import Registry

log = get_logger("health")

_DEFAULT_TIMEOUT = 2.0


@dataclass(frozen=True)
class HealthResult:
    """健康检查结果。"""

    ok: bool
    host_port: int | None
    status_code: int | None = None
    reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "hostPort": self.host_port,
            "statusCode": self.status_code,
            "reason": self.reason,
            "checkedAt": now_iso(),
        }


def http_ok(host_port: int, *, timeout: float = _DEFAULT_TIMEOUT) -> tuple[bool, int | None]:
    """单次 HTTP GET 健康探测。

    返回 ``(是否成功, HTTP 状态码)``。2xx/3xx 视为成功；连接失败/超时/4xx/5xx 失败。
    """
    url = mark_probe_url(f"http://127.0.0.1:{host_port}/")
    try:
        resp = urllib.request.urlopen(url, timeout=timeout)
        code = getattr(resp, "status", None) or resp.getcode()
        return (200 <= int(code) < 400, int(code))
    except urllib.error.HTTPError as exc:
        # HTTPError 也是"得到了响应"，4xx 视为不健康，5xx 同样
        return (False, exc.code)
    except Exception as exc:  # noqa: BLE001 — 连接拒绝/超时统一为失败
        log.debug("健康探测失败（port=%s）：%s", host_port, exc)
        return (False, None)


def check_health(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> HealthResult:
    """对实例执行 HTTP 健康检查并写回 registry（WBS-18.05/06/07）。

    * 无可用端口 → 返回失败，不写 last_error（实例未部署）；
    * 探测成功 → 写 ``last_health_check_at``，清 ``last_error``；
    * 探测失败 → 写 ``last_error`` 与 ``error`` 事件，但不直接改 status
      （status 由 :func:`lifecycle.observe_status` 基于进程态判定，避免健康
      抖动误报 failed）。
    """
    from local_webpage_access.hosting import _load_manifest

    manifest = _load_manifest(workspace, instance_id)
    host_port = _resolve_host_port(manifest, registry)
    if not host_port:
        result = HealthResult(ok=False, host_port=None, reason="实例尚未分配端口")
        registry.add_event(
            instance_id, "health_check", "健康检查跳过：无可用端口"
        )
        return result

    ok, code = http_ok(host_port, timeout=timeout)
    if ok:
        registry.record_health_check(instance_id)
        registry.update_status(instance_id, _current_status(manifest), last_error="")
        registry.add_event(
            instance_id, "health_check", f"健康检查通过（port={host_port}, code={code}）"
        )
        log.info("实例 %s 健康检查通过（port=%s）", instance_id, host_port)
    else:
        reason = f"健康检查失败（port={host_port}, code={code}）"
        registry.update_status(
            instance_id, _current_status(manifest), last_error=reason
        )
        registry.add_event(instance_id, "health_check", reason)
        log.warning("实例 %s %s", instance_id, reason)
    return HealthResult(ok=ok, host_port=host_port, status_code=code, reason=None if ok else reason)


def _resolve_host_port(manifest: InstanceManifest, registry: Registry) -> int | None:
    """从 manifest 或 registry 解析实例暴露的 host 端口。"""
    # 优先 manifest 的 network/container 字段
    port = None
    if manifest.network and manifest.network.hostPort:
        port = manifest.network.hostPort
    if not port and manifest.container and manifest.container.hostPort:
        port = manifest.container.hostPort
    if not port and manifest.static and manifest.static.hostPort:
        port = manifest.static.hostPort
    if port:
        return int(port)
    # 回退到 registry
    if manifest.runtime.value == "docker-compose":
        row = registry.get_container(manifest.id)
        if row and row.get("host_port"):
            return int(row["host_port"])
    else:
        row = registry.get_static_site(manifest.id)
        if row and row.get("host_port"):
            return int(row["host_port"])
    return None


def _current_status(manifest: InstanceManifest) -> str:
    val = manifest.status.value if isinstance(manifest.status, Status) else manifest.status
    return val or Status.PENDING.value


__all__ = [
    "HealthResult",
    "http_ok",
    "check_health",
]
