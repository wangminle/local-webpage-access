"""静态托管与前端构建流程（WBS-10 / WBS-11）。

两条静态路径：
1. **纯静态 HTML**（WBS-10）：识别 ``index.html`` → 同步到 ``public/`` →
   分配端口 → 启用网关 → 健康检查。
2. **纯前端 SPA**（WBS-11）：``npm ci``/``install`` → ``npm run build`` →
   识别 ``dist/`` 等产物 → 复制到 ``public/`` → 启用网关 → 健康检查；
   构建失败时标记 ``build_failed`` 并写入 builds/events 表。

两条路径最终都通过 :class:`StaticGateway` 暴露到 hostPort。
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from local_webpage_access.compose import generate_compose, generate_env
from local_webpage_access.config import Config
from local_webpage_access.docker_runtime import DockerRuntime
from local_webpage_access.dockerfile_templates import generate_dockerfile
from local_webpage_access.errors import BuildError, DockerError, HostingError
from local_webpage_access.logging import get_logger, write_instance_log
from local_webpage_access.models import (
    DesiredState,
    InstanceManifest,
    NetworkConfig,
    RouteMode,
    StaticConfig,
    Status,
)
from local_webpage_access.paths import Workspace
from local_webpage_access.probe import mark_probe_url
from local_webpage_access.ports import PortAllocator, build_network_entry, is_port_listening
from local_webpage_access.registry import Registry
from local_webpage_access.static_gateway import StaticGateway

log = get_logger("hosting")

_BUILD_TIMEOUT = 600
_BUILD_OUTPUT_DIRS = ("dist", "build", "out", ".output", ".svelte-kit")
# 容器启动后等待 HTTP 就绪的最大尝试次数与间隔（小主机性能弱，留足预热时间）
_CONTAINER_HEALTH_ATTEMPTS = 30
_CONTAINER_HEALTH_DELAY = 1.0
# 同步到 public/ 时跳过的非静态文件
_STATIC_SKIP = {
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "node_modules",
    ".git",
    "requirements.txt",
    "pyproject.toml",
    "Pipfile",
    "uv.lock",
    "Dockerfile",
}


# ---- 公开入口 --------------------------------------------------------------


def host_instance(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
) -> InstanceManifest:
    """根据 manifest 的 runtime/form 自动选择静态、前端或容器流程。

    * ``shared-static`` → :func:`host_static` 或 :func:`build_and_host_frontend`；
    * ``docker-compose`` → :func:`host_container`（Phase 3）。
    """
    manifest = _load_manifest(workspace, instance_id)
    runtime = manifest.runtime.value

    if runtime == "shared-static":
        form = _infer_form(manifest)
        if form == "frontend-static":
            return build_and_host_frontend(workspace, config, registry, instance_id)
        return host_static(workspace, config, registry, instance_id)

    if runtime == "docker-compose":
        return host_container(workspace, config, registry, instance_id)

    raise HostingError(
        f"实例 {instance_id} 的 runtime={runtime} 暂不支持",
        instance_id=instance_id,
        runtime=runtime,
    )


def stop_instance(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
) -> InstanceManifest:
    """停止实例：静态实例禁用网关，容器实例执行 ``docker compose stop``。"""
    manifest = _load_manifest(workspace, instance_id)
    runtime = manifest.runtime.value

    if runtime == "shared-static":
        gateway = StaticGateway(workspace, config)
        gateway.disable(instance_id)
        # 注意：不释放端口，start 恢复时复用（与容器路径一致，BUG-045）。
        # 此前 stop 这里会调用 allocator.release_instance，导致 ports 表归属被清空，
        # 但 static_sites.host_port 与 manifest.static.hostPort 仍保留旧值，
        # 于是该端口可被重新分配给别的实例，而旧实例的网关配置/字段仍指向它，
        # 造成跨实例内容混淆。保留端口登记即可让 _ensure_static_port 复用。
        manifest.status = Status.STOPPED
        manifest.desiredState = DesiredState.STOPPED
        if manifest.static is not None:
            manifest.static.enabled = False
        manifest.touch()
        manifest.save(workspace.app_manifest_path(instance_id))
        registry.upsert_from_manifest(manifest)
        registry.update_status(instance_id, Status.STOPPED.value)
        registry.set_static_enabled(instance_id, False)
        registry.add_event(instance_id, "stop", "静态实例已停止")
        return manifest

    if runtime == "docker-compose":
        return stop_container(workspace, config, registry, instance_id)

    raise HostingError(
        f"实例 {instance_id} 的 runtime={runtime} 暂不支持停止",
        instance_id=instance_id,
        runtime=runtime,
    )


# ---- WBS-10 纯静态 ---------------------------------------------------------


def host_static(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
) -> InstanceManifest:
    """纯静态 HTML 托管流程。"""
    manifest = _load_manifest(workspace, instance_id)
    current_dir = workspace.app_current(instance_id)
    public_dir = workspace.app_public(instance_id)

    registry.update_status(instance_id, Status.BUILDING.value)
    try:
        # 1. 识别入口 index.html（WBS-10.01）
        index = find_index_html(current_dir)
        if index is None:
            raise HostingError(
                f"未找到 index.html：{current_dir}",
                instance_id=instance_id,
            )

        # 2. 同步到 public/（WBS-10.02）
        # 同步整个 current/（保留根目录同级资源与子目录结构）；
        # index 嵌套于子目录时，再把该子目录内容提升到 public/ 根，
        # 保证 GET / 命中首页、且同级资源在根与原路径均可访问（BUG-004 边界）
        static_root = index.parent
        sync_static_to_public(current_dir, public_dir)
        if static_root != current_dir:
            _promote_to_root(static_root, public_dir)

        # 3-4. 分配端口 + 启用网关（WBS-10.03/04）
        manifest = _enable_static(workspace, config, registry, instance_id, manifest, public_dir)

        # 5-7. 更新 manifest + registry + 健康检查（WBS-10.05/06/07）
        manifest.status = Status.RUNNING
        manifest.desiredState = DesiredState.RUNNING
        manifest.lastError = None
        manifest.touch()
        manifest.save(workspace.app_manifest_path(instance_id))
        registry.upsert_from_manifest(manifest)
        registry.update_status(instance_id, Status.RUNNING.value)
        registry.record_started(instance_id)

        if manifest.network.hostPort is not None:
            gateway = StaticGateway(workspace, config)
            if gateway.health_check(manifest.network.hostPort):
                registry.record_health_check(instance_id)
        registry.add_event(instance_id, "start", "静态实例已启动")
        log.info("静态实例 %s 已启动", instance_id)
        return manifest
    except Exception as exc:
        _mark_failed(workspace, registry, instance_id, manifest, exc)
        raise


# ---- WBS-11 前端构建 -------------------------------------------------------


def build_and_host_frontend(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
) -> InstanceManifest:
    """纯前端 SPA 构建托管流程。"""
    manifest = _load_manifest(workspace, instance_id)
    current_dir = workspace.app_current(instance_id)
    public_dir = workspace.app_public(instance_id)
    build_log = workspace.app_logs(instance_id) / "build.log"
    build_log.parent.mkdir(parents=True, exist_ok=True)

    registry.update_status(instance_id, Status.BUILDING.value)
    build_id = registry.add_build(
        instance_id,
        status="running",
        log_path=str(build_log),
    )

    try:
        # 1-5. 安装 + 构建（WBS-11.01~06）
        if manifest.entry.install:
            write_instance_log(
                workspace.apps, instance_id, "build", f"安装：{manifest.entry.install}"
            )
            run_command(manifest.entry.install, cwd=current_dir, log_path=build_log)
        if manifest.entry.build:
            write_instance_log(
                workspace.apps, instance_id, "build", f"构建：{manifest.entry.build}"
            )
            run_command(manifest.entry.build, cwd=current_dir, log_path=build_log)
        else:
            raise BuildError(
                "缺少 build 脚本，无法构建前端项目",
                instance_id=instance_id,
            )

        # 6-8. 识别产物 + 复制到 public/（WBS-11.07/08）
        dist = find_build_output(current_dir)
        if dist is None:
            raise BuildError(
                f"构建完成但未找到产物目录（dist/build/out）：{current_dir}",
                instance_id=instance_id,
            )
        sync_dir(dist, public_dir)
        registry.finish_build(build_id, status="success")
        registry.add_event(instance_id, "build", f"构建成功，产物来自 {dist.name}/")

        # 9-10. 启用网关 + 健康检查（WBS-11.09/10）
        manifest = _enable_static(workspace, config, registry, instance_id, manifest, public_dir)
        manifest.status = Status.RUNNING
        manifest.desiredState = DesiredState.RUNNING
        manifest.lastError = None
        manifest.touch()
        manifest.save(workspace.app_manifest_path(instance_id))
        registry.upsert_from_manifest(manifest)
        registry.update_status(instance_id, Status.RUNNING.value)
        registry.record_started(instance_id)

        if manifest.network.hostPort is not None:
            gateway = StaticGateway(workspace, config)
            if gateway.health_check(manifest.network.hostPort):
                registry.record_health_check(instance_id)
        registry.add_event(instance_id, "start", "前端实例已构建并启动")
        log.info("前端实例 %s 构建并启动", instance_id)
        return manifest
    except Exception as exc:
        # WBS-11.11/12/13：构建失败标记 + 写表 + 上下文
        registry.finish_build(
            build_id,
            status="failed",
            error_summary=str(exc)[:500],
        )
        _mark_failed(workspace, registry, instance_id, manifest, exc)
        raise


# ---- WBS-15 / WBS-16 容器托管（Node / Python / SQLite）---------------------


def _rescue_container_data_before_rebuild(
    workspace: Workspace,
    manifest: InstanceManifest,
    instance_id: str,
    runtime: DockerRuntime,
) -> None:
    """BUG-205：重建 ``down`` 前把容器内数据救出到宿主 ``data/``。

    既有容器实例的数据库可能写在容器可写层（旧版未挂载 ``data/``、或挂载路径与新
    版不同），重建 ``down`` 删容器会丢库。此处 best-effort 用 ``docker cp`` 把候选
    路径的内容拷出；宿主 ``data/`` 已有内容（挂载已持久化）或无容器时跳过。失败仅
    记日志、不抛错——迁移是保护性措施，不得阻断重建。
    """
    from local_webpage_access.compose import _is_sqlite, container_data_paths

    if not _is_sqlite(manifest):
        return  # 非 SQLite 文件库无 data/ 挂载，无需迁移
    try:
        host_data = workspace.app_data(instance_id)
        candidates = container_data_paths(workspace.app_current(instance_id), manifest)
        rescued = runtime.rescue_container_data(instance_id, host_data, candidates)
        if rescued:
            log.warning(
                "BUG-205：实例 %s 宿主 data/ 原为空，已从旧容器救出 %d 个文件，"
                "重建将复用（避免丢库）",
                instance_id,
                rescued,
            )
    except Exception:  # noqa: BLE001 — 迁移失败不阻断重建
        log.exception("BUG-205 重建前数据迁移异常（忽略，继续重建）")


def host_container(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
) -> InstanceManifest:
    """Docker Compose 容器实例托管流程（WBS-15 / WBS-16）。

    流程：
    1. 前置条件检查（Docker 可用）；
    2. 若旧容器在跑，先 ``down`` 释放端口绑定（重建场景）；
    3. 生成 Dockerfile（WBS-15.03 / 16.10）；
    4. 分配/复用 host 端口（WBS-15.02 / 16.06）；
    5. 生成 Compose + .env（WBS-15.04 / 16.08/09/11）；
    6. ``build`` + ``up``（WBS-15.05/06 / 16.12）；
    7. HTTP 健康检查（WBS-15.07 / 16.13）；
    8. 观测 containerId/imageId；
    9. 更新 manifest + registry（WBS-15.08/09 / 16.14）。

    失败时标记 failed 并写诊断上下文（WBS-15.10 / 16.15）。
    """
    manifest = _load_manifest(workspace, instance_id)
    if manifest.runtime.value != "docker-compose" or manifest.container is None:
        raise HostingError(
            f"实例 {instance_id} 不是容器实例（runtime={manifest.runtime.value}）",
            instance_id=instance_id,
            runtime=manifest.runtime.value,
        )

    # 1. Docker 前置条件（WBS-15.05 前置）
    DockerRuntime.ensure_available()
    runtime = DockerRuntime(workspace, registry)

    registry.update_status(instance_id, Status.BUILDING.value)
    build_log = workspace.app_logs(instance_id) / "build.log"
    build_id = registry.add_build(
        instance_id, status="running", log_path=str(build_log)
    )
    fresh_port = False

    try:
        # BUG-205：重建 down 前先把容器内数据救出到宿主 data/，避免旧库随容器删除丢失
        _rescue_container_data_before_rebuild(workspace, manifest, instance_id, runtime)

        # 2. 重建场景：先停掉旧容器，释放端口绑定
        try:
            if runtime.is_running(instance_id):
                runtime.down(instance_id)
        except DockerError as exc:  # 旧容器清理失败不阻塞重建，仅记录
            log.warning("重建前清理旧容器失败（忽略）：%s", exc)

        # 3. 生成 Dockerfile（BUG-200：注入 config.buildMirrors，避免手改被覆盖）
        generate_dockerfile(manifest, workspace, config=config)

        # 4. 分配/复用端口
        host_port, fresh_port = _ensure_container_port(config, registry, instance_id)

        # 5. 生成 Compose + .env（含 SQLite DATABASE_URL / RUNTIME_ROOT 与 data/ 挂载）
        generate_compose(manifest, workspace, host_port=host_port)
        generate_env(manifest, workspace, host_port=host_port)

        # 6. build + up
        runtime.build(instance_id, build_id=build_id)
        runtime.up(instance_id)
    except Exception as exc:
        # 端口回滚：仅释放本轮新分配的端口（与 _enable_static / BUG-182 对称）。
        # 复用旧端口是上一轮成功部署的登记，失败时清掉会破坏 lanUrl 稳定性。
        if fresh_port:
            try:
                PortAllocator(config, registry).release_instance(instance_id)
            except Exception:  # noqa: BLE001
                log.warning("失败回滚释放实例 %s 端口失败", instance_id)
        # DockerRuntime.build 成功/失败都会 finish 该 build 行；
        # 这里只兜底"build 尚未执行就被打断"的情况（如生成文件/分配端口失败），
        # 此时 build 行仍为 running，需要标记 failed。避免与 build() 双重 finish。
        try:
            latest = registry.list_builds(instance_id, limit=1)
            if latest and latest[0]["id"] == build_id and latest[0]["status"] == "running":
                registry.finish_build(
                    build_id, status="failed", error_summary=str(exc)[:500]
                )
        except Exception:  # noqa: BLE001
            log.exception("兜底 finish build 失败")
        _mark_failed(workspace, registry, instance_id, manifest, exc)
        raise

    # 7. 观测 containerId / imageId（失败不阻塞，仅记录 None）
    container_id = _safe(lambda: runtime.container_id(instance_id))
    image_id = _safe(lambda: runtime.image_id(instance_id))
    manifest.container.containerId = container_id
    manifest.container.imageId = image_id
    manifest.container.hostPort = host_port

    # 8. 更新 manifest + registry（先 upsert，再 record_health_check，
    #    否则 upsert_from_manifest 会用 manifest 的 lastHealthCheckAt=None 覆盖 DB 时间戳）
    # BUG-084：写回 network 时保留容器已配置的路径别名，否则别名入口对状态/API 不可见。
    entry = build_network_entry(
        config,
        host_port,
        internal_port=manifest.container.internalPort,
        path_alias=_container_path_alias(manifest),
    )
    manifest.network = NetworkConfig(**entry)
    manifest.status = Status.RUNNING
    manifest.desiredState = DesiredState.RUNNING
    manifest.lastError = None
    manifest.touch()
    manifest.save(workspace.app_manifest_path(instance_id))
    registry.upsert_from_manifest(manifest)
    registry.update_status(
        instance_id,
        Status.RUNNING.value,
        desired_state=DesiredState.RUNNING.value,
    )
    registry.record_started(instance_id)

    # 9. 健康检查（best-effort，不阻塞 RUNNING 标记；放在 registry 写回之后，
    #    避免被 upsert_from_manifest 覆盖）
    if _wait_for_http(host_port):
        registry.record_health_check(instance_id)
    registry.add_event(
        instance_id,
        "start",
        f"容器实例已启动（host_port={host_port}）",
    )
    log.info("容器实例 %s 已启动，端口 %d", instance_id, host_port)
    return manifest


def start_container(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
) -> InstanceManifest:
    """轻量启动已部署的容器实例：``docker compose start``（不重建镜像）。

    与 :func:`host_container`（全量部署/重建）的区别：
    - 不重新生成 Dockerfile/Compose/.env、不 ``build``；
    - 仅 ``compose start`` 已存在的容器，复用已登记端口与 lanUrl。

    前提：实例此前已被 :func:`host_container` 部署过（``containerId`` 已落库）。
    若从未部署，应走 :func:`host_instance` 全量流程。
    """
    manifest = _load_manifest(workspace, instance_id)
    if manifest.runtime.value != "docker-compose" or manifest.container is None:
        raise HostingError(
            f"实例 {instance_id} 不是容器实例（runtime={manifest.runtime.value}）",
            instance_id=instance_id,
            runtime=manifest.runtime.value,
        )

    DockerRuntime.ensure_available()
    runtime = DockerRuntime(workspace, registry)

    # 已在跑：直接同步状态，避免重复 start
    if runtime.is_running(instance_id):
        log.info("容器实例 %s 已在运行，跳过 start", instance_id)
    else:
        runtime.start(instance_id)

    # 端口：复用此前部署登记的 hostPort
    host_port = manifest.container.hostPort
    if not host_port:
        host_port, _fresh = _ensure_container_port(config, registry, instance_id)
        manifest.container.hostPort = host_port

    # 观测 containerId / imageId
    container_id = _safe(lambda: runtime.container_id(instance_id)) or manifest.container.containerId
    image_id = _safe(lambda: runtime.image_id(instance_id)) or manifest.container.imageId
    manifest.container.containerId = container_id
    manifest.container.imageId = image_id

    # 更新 manifest + registry
    # BUG-084：写回 network 时保留容器路径别名（与 host_container 一致）。
    entry = build_network_entry(
        config,
        host_port,
        internal_port=manifest.container.internalPort,
        path_alias=_container_path_alias(manifest),
    )
    manifest.network = NetworkConfig(**entry)
    manifest.status = Status.RUNNING
    manifest.desiredState = DesiredState.RUNNING
    manifest.lastError = None
    manifest.touch()
    manifest.save(workspace.app_manifest_path(instance_id))
    registry.upsert_from_manifest(manifest)
    registry.update_status(
        instance_id,
        Status.RUNNING.value,
        desired_state=DesiredState.RUNNING.value,
    )
    registry.record_started(instance_id)

    # 健康检查（best-effort，放在 registry 写回之后避免被覆盖）
    if _wait_for_http(host_port):
        registry.record_health_check(instance_id)
    registry.add_event(instance_id, "start", f"容器实例已启动（start，host_port={host_port}）")
    log.info("容器实例 %s 已 start，端口 %d", instance_id, host_port)
    return manifest


def stop_container(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
) -> InstanceManifest:
    """停止容器实例：``docker compose stop``，**不删容器、不释放端口**。

    端口保留是为了 ``start`` 恢复时复用同一 lanUrl（WBS-17.09）。
    彻底清理用 :func:`stop_instance` 之外的 ``down``/``remove``（WBS-17）。
    """
    manifest = _load_manifest(workspace, instance_id)
    if manifest.runtime.value != "docker-compose":
        raise HostingError(
            f"实例 {instance_id} 不是容器实例",
            instance_id=instance_id,
            runtime=manifest.runtime.value,
        )
    runtime = DockerRuntime(workspace, registry)
    runtime.stop(instance_id)

    manifest.status = Status.STOPPED
    manifest.desiredState = DesiredState.STOPPED
    manifest.touch()
    manifest.save(workspace.app_manifest_path(instance_id))
    registry.upsert_from_manifest(manifest)
    registry.update_status(
        instance_id,
        Status.STOPPED.value,
        desired_state=DesiredState.STOPPED.value,
    )
    # 注意：不释放端口，start 恢复时复用
    return manifest


# ---- 容器辅助 --------------------------------------------------------------


def _container_path_alias(manifest: InstanceManifest) -> str | None:
    """读取容器实例已配置的路径别名（IMP-014）。

    host_container / start_container 写回 ``manifest.network`` 时据此保留
    ``routeMode=name`` + ``routeHost`` + ``routeUrl``，避免重建 network 后别名
    丢失（BUG-084：状态/API 经 network 读别名，丢失后入口不可见）。
    """
    c = manifest.container
    if c is not None and c.routeMode == RouteMode.NAME.value and c.routeHost:
        return c.routeHost
    return None


def _ensure_container_port(
    config: Config,
    registry: Registry,
    instance_id: str,
) -> tuple[int, bool]:
    """容器端口分配：优先复用已登记端口，否则新分配。

    复用保证重建后 lanUrl 稳定；端口被外部占用时回退到新分配。复用登记用
    :meth:`Registry.allocate_port` 的并发安全语义：若旧端口已被其他实例抢走
    （BUG-017），返回 False，回退到全新分配。

    返回 ``(port, fresh)``：``fresh=False`` 表示复用了上一轮成功部署的登记，
    调用方在本次 build/up 失败时**不得**释放它（与 :func:`_ensure_static_port`
    / BUG-182 对称）。
    """
    allocator = PortAllocator(config, registry)
    row = registry.get_container(instance_id)
    existing = row.get("host_port") if row else None
    if existing and not is_port_listening(int(existing)):
        if registry.allocate_port(instance_id, int(existing)):
            log.info("复用容器实例 %s 的端口 %d", instance_id, existing)
            return int(existing), False
        log.warning(
            "实例 %s 的旧端口 %d 已被其他实例占用，重新分配",
            instance_id,
            existing,
        )
    # 全新分配：先清掉该实例可能残留的端口登记
    allocator.release_instance(instance_id)
    return allocator.allocate(instance_id), True


def _ensure_static_port(
    config: Config,
    registry: Registry,
    instance_id: str,
) -> tuple[int, bool]:
    """静态端口分配：优先复用已登记端口，否则新分配。

    与 :func:`_ensure_container_port` 对称（BUG-045）。``stop_instance`` 不再
    释放静态实例的端口登记，因此重启时此处的复用路径会命中：旧端口仍归本实例
    所有、且无活跃监听者（``is_port_listening`` 为 False），:meth:`allocate_port`
    的并发安全语义确认归属后直接复用，保持 lanUrl 稳定。

    若旧端口被外部进程占用或归属已丢失（极端情况），回退到全新分配。

    返回 ``(port, fresh)``：``fresh=False`` 表示复用了上一轮成功部署的登记，
    调用方在本次启用失败时**不得**释放它（否则破坏 BUG-045 端口保留语义、
    可致跨实例内容混淆，BUG-182）。
    """
    allocator = PortAllocator(config, registry)
    row = registry.get_static_site(instance_id)
    existing = row.get("host_port") if row else None
    if existing and not is_port_listening(int(existing)):
        if registry.allocate_port(instance_id, int(existing)):
            log.info("复用静态实例 %s 的端口 %d", instance_id, existing)
            return int(existing), False
        log.warning(
            "实例 %s 的旧端口 %d 已被其他实例占用，重新分配",
            instance_id,
            existing,
        )
    # 全新分配：先清掉该实例可能残留的端口登记
    allocator.release_instance(instance_id)
    return allocator.allocate(instance_id), True


def _wait_for_http(
    host_port: int,
    *,
    attempts: int = _CONTAINER_HEALTH_ATTEMPTS,
    delay: float = _CONTAINER_HEALTH_DELAY,
) -> bool:
    """轮询 ``http://127.0.0.1:<port>/`` 直到响应或超时。

    容器刚 up 时进程可能还在预热，需要等待。返回是否最终成功。
    """
    for _ in range(max(1, attempts)):
        if _http_ok(host_port):
            return True
        time.sleep(delay)
    return False


def _http_ok(host_port: int, *, timeout: float = 2.0) -> bool:
    """单次 HTTP GET 健康探测（2xx/3xx 视为成功）。"""
    url = mark_probe_url(f"http://127.0.0.1:{host_port}/")
    try:
        resp = urllib.request.urlopen(url, timeout=timeout)
        return 200 <= resp.status < 400
    except Exception:  # noqa: BLE001
        return False


def _safe(fn):
    """执行可能抛 DockerError 的观测调用，失败返回 None。"""
    try:
        return fn()
    except DockerError:
        return None


# ---- 共享：启用静态网关 ----------------------------------------------------


def _enable_static(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
    manifest: InstanceManifest,
    public_dir: Path,
) -> InstanceManifest:
    """分配端口、启用网关、更新 manifest 的 static/network 字段。"""
    gateway = StaticGateway(workspace, config)
    # IMP-006：从既有 manifest 读取路径别名（import 时写入 static.routeHost）。
    # 重启用场景下 routeMode/routeHost 已落盘，需在重建 StaticConfig 时保留。
    existing_static = manifest.static
    path_alias: str | None = None
    if (
        existing_static is not None
        and existing_static.routeMode == RouteMode.NAME.value
        and existing_static.routeHost
    ):
        path_alias = existing_static.routeHost
    # 端口分配：优先复用已登记端口（stop 后保留），否则全新分配（BUG-045）
    host_port, fresh_port = _ensure_static_port(config, registry, instance_id)
    allocator = PortAllocator(config, registry)

    # 不在 enable 前 disable：enable 会覆盖站点配置并停掉残留 builtin；
    # 若先 disable 再 enable 失败，会留下「既无旧也无新」的悬空实例。
    backend = gateway.detect_backend()
    try:
        gateway.enable(instance_id, host_port, public_dir, alias=path_alias)
    except Exception:
        # 网关启用失败：仅释放本轮新分配的端口，避免连续失败耗尽端口池（BUG-016）。
        # 复用的旧端口是上一轮成功部署的登记，释放会破坏 BUG-045 端口保留语义、
        # 可致跨实例内容混淆（BUG-182）。gateway.enable 已对其子进程/站点配置回滚。
        if fresh_port:
            allocator.release(host_port)
        raise

    manifest.static = StaticConfig(
        root="public",
        gateway=backend,
        routeMode=(RouteMode.NAME.value if path_alias else RouteMode.PORT.value),
        routeHost=path_alias,
        hostPort=host_port,
        gatewayConfigPath=str(gateway.site_config_path(instance_id)),
        enabled=True,
    )
    entry = build_network_entry(config, host_port, path_alias=path_alias)
    manifest.network = NetworkConfig(**entry)
    registry.set_static_enabled(instance_id, True)
    return manifest


# ---- 辅助函数 --------------------------------------------------------------


def find_index_html(directory: Path) -> Path | None:
    """寻找入口 ``index.html``（顶层优先，深一层兜底）。"""
    top = directory / "index.html"
    if top.is_file():
        return top
    try:
        for sub in sorted(directory.iterdir()):
            if sub.is_dir():
                candidate = sub / "index.html"
                if candidate.is_file():
                    return candidate
    except (PermissionError, OSError):
        pass
    return None


def find_build_output(project_dir: Path) -> Path | None:
    """识别构建产物目录（dist/、build/、out/ 等）。"""
    for name in _BUILD_OUTPUT_DIRS:
        candidate = project_dir / name
        if candidate.is_dir():
            try:
                if any(candidate.iterdir()):
                    return candidate
            except (PermissionError, OSError):
                continue
    return None


def sync_static_to_public(current_dir: Path, public_dir: Path) -> None:
    """把 current/ 的静态文件同步到 public/（跳过非静态工程文件）。"""
    sync_dir(current_dir, public_dir, skip=_STATIC_SKIP)


def sync_dir(
    src: Path,
    dst: Path,
    *,
    skip: set[str] | None = None,
) -> None:
    """把 src/ 的内容整体复制到 dst/（先清空 dst）。"""
    skip = skip or set()
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    for item in src.iterdir():
        if item.name in skip:
            continue
        _copy_item(item, dst / item.name)


def _copy_item(src: Path, dst: Path) -> None:
    """复制单个文件/目录到 ``dst``，覆盖同名项。"""
    if dst.exists():
        if dst.is_dir():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)


def _promote_to_root(src: Path, public_dir: Path) -> None:
    """把 ``src/`` 的内容提升（复制）到 ``public_dir/`` 根，覆盖同名。

    用于嵌套 ``index.html`` 场景：``sync_static_to_public`` 已同步整个
    ``current/``，再把 ``index`` 所在子目录的内容额外铺到 ``public/`` 根，
    使首页与同级资源既可从根访问、也保留原子目录路径（BUG-004 边界）。
    """
    for item in src.iterdir():
        if item.name in _STATIC_SKIP:
            continue
        _copy_item(item, public_dir / item.name)


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """终止整个进程树（BUG-183）。

    旧 ``subprocess.run(shell=True, timeout=...)`` 超时只 kill 直接 shell 子进程，
    npm/pnpm/node 等孙进程成孤儿继续跑（构建槽位已释放后与后续 rebuild 并发，
    击穿 buildConcurrency=1 的 OOM 保护；Windows 上孤儿还锁住 build.log 致
    --purge 删除失败）。新进程组 + 组级 SIGKILL（POSIX）/ taskkill /T（Windows）
    一并清掉子孙。
    """
    if proc.poll() is not None:
        return
    pid = proc.pid
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
                check=False,
            )
        else:
            try:
                pgid = os.getpgid(pid)
            except ProcessLookupError:
                return
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(pgid, sig)
                except ProcessLookupError:
                    return
                except PermissionError:
                    break
                try:
                    proc.wait(timeout=5)
                    return
                except subprocess.TimeoutExpired:
                    continue
    except Exception:  # noqa: BLE001 — best-effort 清理，兜底 kill 直接进程
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def run_command(
    cmd: str,
    *,
    cwd: Path,
    log_path: Path,
    timeout: int = _BUILD_TIMEOUT,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    """运行 shell 命令，stdout/stderr 追加写入 log_path。

    命令来自项目识别器的确定性推断，``shell=True`` 可接受。超时时杀整个进程树
    （BUG-183），不残留 npm/node 孙进程孤儿。
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    popen_kwargs: dict = {
        "cwd": str(cwd),
        "shell": True,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "env": env,
        "text": True,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    from local_webpage_access.logs import open_append

    with open_append(log_path) as fh:
        fh.write(f"\n$ {cmd}\n")
        fh.flush()
        proc = subprocess.Popen(cmd, **popen_kwargs)
        try:
            stdout_data, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            try:
                stdout_data, _ = proc.communicate(timeout=10)
            except Exception:  # noqa: BLE001
                stdout_data = ""
            if stdout_data:
                fh.write(stdout_data)
            fh.flush()
            raise BuildError(
                f"命令超时（{timeout}s）：{cmd}",
                command=cmd,
                timeout=timeout,
            )
        if stdout_data:
            fh.write(stdout_data)
        fh.flush()
    if proc.returncode != 0:
        raise BuildError(
            f"命令失败（exit {proc.returncode}）：{cmd}",
            command=cmd,
            exit_code=proc.returncode,
            log_path=str(log_path),
        )
    return subprocess.CompletedProcess(args=cmd, returncode=proc.returncode, stdout="")


def _load_manifest(workspace: Workspace, instance_id: str) -> InstanceManifest:
    path = workspace.app_manifest_path(instance_id)
    if not path.is_file():
        raise HostingError(
            f"实例 {instance_id} 缺少 local-web.json",
            instance_id=instance_id,
        )
    return InstanceManifest.load(path)


def _infer_form(manifest: InstanceManifest) -> str:
    """从 stack/kind 推断是否为前端构建形态。"""
    if manifest.runtime.value != "shared-static":
        return "container"
    stack_lower = {s.lower() for s in manifest.stack}
    frontend_markers = {"vite", "react", "react-dom", "vue", "svelte", "preact", "@vitejs/plugin-react"}
    if stack_lower & frontend_markers:
        return "frontend-static"
    if manifest.entry.build:
        return "frontend-static"
    return "static"


def _mark_failed(
    workspace: Workspace,
    registry: Registry,
    instance_id: str,
    manifest: InstanceManifest,
    exc: Exception,
) -> None:
    """把实例标记为 failed，写 error summary 与事件。"""
    error_summary = str(exc)[:500]
    manifest.status = Status.FAILED
    manifest.lastError = error_summary
    manifest.touch()
    try:
        manifest.save(workspace.app_manifest_path(instance_id))
        registry.upsert_from_manifest(manifest)
        registry.update_status(instance_id, Status.FAILED.value, last_error=error_summary)
        registry.add_event(instance_id, "error", error_summary)
    except Exception:  # noqa: BLE001
        log.exception("写入 failed 状态时出错")


__all__ = [
    "host_instance",
    "host_static",
    "build_and_host_frontend",
    "host_container",
    "start_container",
    "stop_container",
    "stop_instance",
    "find_index_html",
    "find_build_output",
    "sync_static_to_public",
    "sync_dir",
    "run_command",
]
