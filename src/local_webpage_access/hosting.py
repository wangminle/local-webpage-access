"""静态托管与前端构建流程（WBS-10 / WBS-11）。

两条静态路径：
1. **纯静态 HTML**（WBS-10）：识别入口 HTML（``index.html`` 优先，否则任意
   ``*.html``）→ 同步到 ``public/`` → 分配端口 → 启用网关 → 健康检查。
2. **纯前端 SPA**（WBS-11）：``npm ci``/``install`` → ``npm run build`` →
   识别 ``dist/`` 等产物 → 复制到 ``public/`` → 启用网关 → 健康检查；
   构建失败时标记 ``build_failed`` 并写入 builds/events 表。

两条路径最终都通过 :class:`StaticGateway` 暴露到 hostPort。
"""

from __future__ import annotations

import contextlib
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
from pathlib import Path
from typing import Any

from local_webpage_access.compose import generate_compose, generate_env
from local_webpage_access.config import Config
from local_webpage_access.docker_runtime import DockerRuntime
from local_webpage_access.dockerfile_templates import generate_dockerfile
from local_webpage_access.errors import (
    BuildCancelled,
    BuildError,
    DockerError,
    HostingError,
    PathError,
)
from local_webpage_access.logging import get_logger, write_instance_log
from local_webpage_access.models import (
    CapabilityContract,
    DesiredState,
    InstanceManifest,
    NetworkConfig,
    RouteMode,
    StaticConfig,
    Status,
)
from local_webpage_access.paths import Workspace, resolve_source_workdir
from local_webpage_access.probe import mark_probe_url, urlopen_direct
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
        # 1. 识别入口 HTML（index.html 优先，否则任意 .html）
        index = find_index_html(current_dir)
        if index is None:
            raise HostingError(
                f"未找到可托管的 HTML：{current_dir}",
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
        _ensure_public_index(public_dir, index, current_dir)

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

    # BUG-503：sourceSubdir 识别为 frontend/ 等子目录时，install/build
    # 必须在子目录执行，否则 npm ci / npm run build 找不到子包 package.json。
    # BUG-507：拒绝绝对路径、.. 与符号链接逃逸，禁止 npm 在 current 外执行。
    work_dir = current_dir
    source_subdir = getattr(manifest, "sourceSubdir", None)
    if source_subdir:
        try:
            work_dir = resolve_source_workdir(current_dir, source_subdir)
        except PathError as exc:
            raise BuildError(str(exc), instance_id=instance_id) from exc

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
            run_command(manifest.entry.install, cwd=work_dir, log_path=build_log)
        if manifest.entry.build:
            write_instance_log(
                workspace.apps, instance_id, "build", f"构建：{manifest.entry.build}"
            )
            run_command(manifest.entry.build, cwd=work_dir, log_path=build_log)
        else:
            raise BuildError(
                "缺少 build 脚本，无法构建前端项目",
                instance_id=instance_id,
            )

        # 6-8. 识别产物 + 复制到 public/（WBS-11.07/08）
        dist = find_build_output(current_dir, hint=manifest.entry.buildOutputDir)
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
    except BuildCancelled as exc:
        registry.finish_build(
            build_id,
            status="cancelled",
            error_summary=str(exc)[:500],
        )
        registry.update_status(instance_id, Status.CANCELLED.value, last_error=str(exc)[:500])
        manifest.status = Status.CANCELLED
        manifest.lastError = str(exc)[:500]
        manifest.touch()
        with contextlib.suppress(Exception):
            manifest.save(workspace.app_manifest_path(instance_id))
        raise
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
    *,
    strict: bool = False,
) -> None:
    """BUG-205：重建 ``down`` 前把容器内数据救出到宿主 ``data/``。

    既有容器实例的数据库可能写在容器可写层（旧版未挂载 ``data/``、或挂载路径与新
    版不同），重建 ``down`` 删容器会丢库。此处 best-effort 用 ``docker cp`` 把候选
    路径的内容拷出；宿主 ``data/`` 已有内容（挂载已持久化）或无容器时跳过。

    默认（``strict=False``，普通重建）失败仅记日志、不抛错——迁移是保护性措施，
    不得阻断重建。``strict=True``（BUG-424，挂载漂移修复）改为 fail-safe：宿主
    ``data/`` 非空视为两侧数据冲突、未救出任何文件、或过程异常，均抛
    :class:`HostingError` 中止，要求人工确认数据归属后再 ``lwa rebuild``——
    禁止带着不确定性继续 down/up（新容器可能改用旧版或种子库，造成 split-brain）。
    """
    from local_webpage_access.compose import _is_sqlite, container_data_paths

    if not _is_sqlite(manifest):
        return  # 非 SQLite 文件库无 data/ 挂载，无需迁移
    try:
        host_data = workspace.app_data(instance_id)
        try:
            host_has_data = host_data.is_dir() and any(host_data.iterdir())
        except OSError as exc:
            if strict:
                raise HostingError(
                    f"实例 {instance_id} 无法读取当前工作区 data/（{host_data}），"
                    "挂载漂移修复已中止，请人工检查",
                    instance_id=instance_id,
                ) from exc
            host_has_data = False  # 非 strict：交给 rescue_container_data 自行处理
        if strict and host_has_data:
            # 两侧都可能有数据且无法自动判定哪份更新：中止，要求人工确认（BUG-424）
            raise HostingError(
                f"实例 {instance_id} 数据挂载漂移，且当前工作区 data/ 非空"
                f"（{host_data}）——无法自动判定哪侧数据更新，已中止自动修复。"
                f"请人工比对新旧两侧数据后执行 lwa rebuild {instance_id}",
                instance_id=instance_id,
            )
        candidates = container_data_paths(workspace.app_current(instance_id), manifest)
        rescued = runtime.rescue_container_data(instance_id, host_data, candidates)
        if strict and rescued <= 0:
            raise HostingError(
                f"实例 {instance_id} 数据挂载漂移，但未能从旧容器救出任何数据"
                f"（当前工作区 data/ 为空），已中止自动修复。"
                f"请人工确认旧挂载中的数据后执行 lwa rebuild {instance_id}",
                instance_id=instance_id,
            )
        if rescued:
            log.warning(
                "BUG-205：实例 %s 宿主 data/ 原为空，已从旧容器救出 %d 个文件，"
                "重建将复用（避免丢库）",
                instance_id,
                rescued,
            )
    except HostingError:
        raise
    except Exception as exc:  # noqa: BLE001
        if strict:
            log.exception("BUG-424 挂载漂移数据救援异常（fail-safe，中止修复）")
            raise HostingError(
                f"实例 {instance_id} 挂载漂移数据救援异常，已中止自动修复：{exc}",
                instance_id=instance_id,
            ) from exc
        log.exception("BUG-205 重建前数据迁移异常（忽略，继续重建）")


def _managed_sqlite_data_mount_drifted(
    workspace: Workspace,
    manifest: InstanceManifest,
    instance_id: str,
    runtime: DockerRuntime,
) -> bool:
    """BUG-421：检查 LWA 管理的 SQLite data bind mount 是否相对当前工作区漂移。

    仅比较 ``compose.container_data_paths`` 中的管理目标（``/app/data`` 与/或
    ``/app/runtime/data``）对应 bind 的 Source 是否等于
    ``workspace.app_data(instance_id).resolve()``。

    Returns:
        True 表示已漂移，调用方应 rescue + down + up；False 表示无需处理
        （非 SQLite、无容器、或无比对的管理挂载）。

    Raises:
        HostingError: 容器状态查询或挂载观测失败——fail-safe，禁止把查询失败
            当作"无容器/无漂移"继续启动（BUG-429），也禁止据此做破坏性重建。
    """
    from local_webpage_access.compose import _is_sqlite, container_data_paths

    if not _is_sqlite(manifest):
        return False
    try:
        existing = runtime.container_id_strict(instance_id, all_containers=True)
    except DockerError as exc:
        raise HostingError(
            f"实例 {instance_id} 无法查询容器状态（禁止据此判定无漂移并启动）：{exc}",
            instance_id=instance_id,
        ) from exc
    if not existing:
        return False
    try:
        mounts = runtime.bind_mounts(instance_id, all_containers=True)
    except DockerError as exc:
        raise HostingError(
            f"实例 {instance_id} 无法检查数据挂载是否漂移（禁止自动重建）：{exc}",
            instance_id=instance_id,
        ) from exc

    expected = workspace.app_data(instance_id).resolve()
    destinations = set(container_data_paths(workspace.app_current(instance_id), manifest))
    managed = [m for m in mounts if m.destination in destinations]
    if not managed:
        return False
    for mount in managed:
        actual = Path(mount.source).resolve() if mount.source else None
        if actual != expected:
            log.warning(
                "BUG-421：实例 %s 数据挂载漂移 destination=%s actual=%s expected=%s",
                instance_id,
                mount.destination,
                mount.source,
                expected,
            )
            return True
    return False


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
    build_id = registry.add_build(instance_id, status="running", log_path=str(build_log))
    _up_completed = False  # 评审-组2
    fresh_port = False

    def _stage(name: str) -> None:
        msg = f"stage={name}"
        log.info("lifecycle_stage instance=%s %s", instance_id, msg)
        with contextlib.suppress(Exception):
            registry.add_event(instance_id, "lifecycle_stage", msg)

    try:
        _stage("host_start")
        # BUG-205：重建 down 前先把容器内数据救出到宿主 data/，避免旧库随容器删除丢失
        _rescue_container_data_before_rebuild(workspace, manifest, instance_id, runtime)

        # BUG-300：从这一刻起旧容器即将被 down，旧身份不能继续表示“可轻量启动”。
        # 先落盘清空；若后续 build/up 失败，start_instance 会走完整重建而不是
        # 对已经删除的旧 containerId 执行 compose start。
        manifest.container.containerId = None
        manifest.container.imageId = None
        manifest.touch()
        manifest.save(workspace.app_manifest_path(instance_id))

        # 2. 重建场景：先停掉旧容器，释放端口绑定
        try:
            if runtime.is_running(instance_id):
                runtime.down(instance_id)
        except DockerError as exc:  # 旧容器清理失败不阻塞重建，仅记录
            log.warning("重建前清理旧容器失败（忽略）：%s", exc)

        # 3. 生成 Dockerfile（BUG-200：注入 config.buildMirrors，避免手改被覆盖）
        generate_dockerfile(manifest, workspace, config=config)
        _stage("dockerfile_ready")

        # 4. 分配/复用端口
        host_port, fresh_port = _ensure_container_port(config, registry, instance_id)

        # 5. 生成 Compose + .env（含 SQLite DATABASE_URL / RUNTIME_ROOT 与 data/ 挂载）
        generate_compose(manifest, workspace, host_port=host_port)
        generate_env(manifest, workspace, host_port=host_port)
        _stage("compose_ready")

        # 6. build + up
        _stage("compose_build_start")
        runtime.build(instance_id, build_id=build_id)
        _stage("compose_build_done")
        _stage("compose_up_start")
        runtime.up(instance_id)
        _up_completed = True  # 评审-组2：up 后写失败须回滚停容器
        _stage("compose_up_done")
    except BuildCancelled as exc:
        if fresh_port:
            try:
                PortAllocator(config, registry).release_instance(instance_id)
            except Exception:  # noqa: BLE001
                log.warning("取消回滚释放实例 %s 端口失败", instance_id)
        try:
            latest = registry.list_builds(instance_id, limit=1)
            if latest and latest[0]["id"] == build_id and latest[0]["status"] == "running":
                registry.finish_build(build_id, status="cancelled", error_summary=str(exc)[:500])
        except Exception:  # noqa: BLE001
            log.exception("取消 finish build 失败")
        registry.update_status(instance_id, Status.CANCELLED.value, last_error=str(exc)[:500])
        manifest.status = Status.CANCELLED
        manifest.lastError = str(exc)[:500]
        manifest.touch()
        with contextlib.suppress(Exception):
            manifest.save(workspace.app_manifest_path(instance_id))
        raise
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
                registry.finish_build(build_id, status="failed", error_summary=str(exc)[:500])
        except Exception:  # noqa: BLE001
            log.exception("兜底 finish build 失败")
        # 评审-组2：up 成功后的写盘/DB 失败会留孤儿运行容器（实例标 FAILED 但
        # 容器在跑）；best-effort down，与 _liveness_failed_rollback 对称。
        if _up_completed:
            with contextlib.suppress(Exception):
                runtime.down(instance_id)
                log.info("实例 %s 部署后置写失败，已回滚停掉容器", instance_id)
        _mark_failed(workspace, registry, instance_id, manifest, exc)
        raise

    # 7. 观测 containerId / imageId（失败不阻塞，仅记录 None）
    container_id = _safe(lambda: runtime.container_id(instance_id))
    image_id = _safe(lambda: runtime.image_id(instance_id))
    if container_id is None:
        # BUG-344 与 BUG-300 联合约束：旧 ID 已因 down 失效，不能恢复；新 ID
        # 又未观测到时也不能假报 running。清理本轮容器并留在 failed，使下次
        # start 走完整重建，而不是对陈旧身份 compose start。
        observation_error = HostingError(
            f"实例 {instance_id} 启动后未能观测到新 containerId",
            instance_id=instance_id,
        )
        with contextlib.suppress(Exception):
            runtime.down(instance_id)
        if fresh_port:
            with contextlib.suppress(Exception):
                PortAllocator(config, registry).release_instance(instance_id)
        _mark_failed(workspace, registry, instance_id, manifest, observation_error)
        raise observation_error
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

    # Gate-C C.04：状态机 VERIFYING → RUNNING/DEGRADED/FAILED。
    # build/up 成功后不立即写 RUNNING，先进入 VERIFYING 评估成功谓词。
    manifest.status = Status.VERIFYING
    manifest.desiredState = DesiredState.RUNNING
    manifest.lastError = None
    # BUG-422：成功落盘前刷新可确定派生路径（裸 mv 后陈旧绝对路径）
    _refresh_manifest_workspace_paths(workspace, manifest)
    manifest.touch()
    manifest.save(workspace.app_manifest_path(instance_id))
    registry.upsert_from_manifest(manifest)
    registry.update_status(
        instance_id,
        Status.VERIFYING.value,
        desired_state=DesiredState.RUNNING.value,
    )

    # 9. Gate-C C.04/C.05：实证校验——评估成功谓词。
    #    必选探针失败 → FAILED（不写 RUNNING）。
    #    可选探针失败 → DEGRADED。
    #    首页 200 不代替 API/DB 验证（§6.5）。
    verification = _evaluate_container_verification(
        host_port,
        manifest,
        workspace,
        registry,
        instance_id,
    )

    # C.R05：收集本次 attempt 的外部副作用记录
    side_effects = _collect_side_effect_records(
        manifest,
        liveness_ok=verification.get("liveness_passed", False),
        verification_status=verification["overall_status"],
    )
    verification["side_effect_records"] = side_effects
    verification["side_effects_auto_recoverable"] = _side_effects_auto_recoverable(side_effects)

    if verification["overall_status"] == "failed":
        # 必选探针失败 → 回滚到 FAILED
        _liveness_failed_rollback(
            workspace,
            config,
            registry,
            instance_id,
            manifest,
            host_port,
            fresh_port,
            verification.get("error", "必选探针未通过"),
        )
        # C.R05：将副作用记录附加到异常 context，供 lifecycle 回滚判断使用
        se_context: dict[str, Any] = {}
        if side_effects:
            se_context["side_effect_records"] = [r.model_dump() for r in side_effects]
            se_context["side_effects_auto_recoverable"] = _side_effects_auto_recoverable(
                side_effects
            )
        raise HostingError(
            f"实例 {instance_id} 必选探针未通过（host_port={host_port}）："
            f"{verification.get('error', 'liveness timeout')}",
            instance_id=instance_id,
            **se_context,
        )

    # 确定最终状态
    if verification["overall_status"] == "degraded":
        final_status = Status.DEGRADED
        status_detail = "DEGRADED（可选探针失败）"
    else:
        final_status = Status.RUNNING
        status_detail = "RUNNING"

    # CHK-192/P2：verificationSummary 必须在 manifest.save() 之前赋值，
    # 否则不会被持久化（原代码在 save 之后赋值，reload 后丢失）。
    manifest.verificationSummary = {
        "overallStatus": verification["overall_status"],
        "livenessPassed": verification.get("liveness_passed", False),
        "mandatoryAllPassed": verification.get("mandatory_all_passed", False),
        "optionalWarnings": verification.get("optional_warnings", []),
        "observedCapabilities": verification.get("observed_capabilities", []),
    }

    # C.R06：成功部署后持久化四类指纹，供下次 start 时判断是否可走轻量路径
    try:
        from local_webpage_access.lifecycle import _compute_deployment_fingerprints

        manifest.deploymentFingerprints = _compute_deployment_fingerprints(
            workspace,
            manifest,
        )
    except Exception:  # noqa: BLE001 - 指纹计算失败不阻塞部署成功
        log.debug("实例 %s 指纹计算失败（不阻塞部署）", instance_id)

    manifest.status = final_status
    manifest.lastError = None
    manifest.touch()
    manifest.save(workspace.app_manifest_path(instance_id))
    registry.update_status(
        instance_id,
        final_status.value,
        desired_state=DesiredState.RUNNING.value,
        clear_last_error=True,
    )
    registry.record_started(instance_id)

    # 健康检查通过 → 记录（必选探针包含基础存活）
    if verification.get("liveness_passed"):
        registry.record_health_check(instance_id)

    registry.add_event(
        instance_id,
        "start",
        f"容器实例已启动（{status_detail}，host_port={host_port}）",
    )
    log.info("容器实例 %s 已启动（%s），端口 %d", instance_id, status_detail, host_port)
    return manifest


def start_container(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
) -> InstanceManifest:
    """启动已部署过的容器实例（优先轻量恢复）。

    与 :func:`host_container`（全量部署/重建）的区别：
    - 容器仍在（含 stopped）→ ``compose start``，不 build；
    - 容器已被外部 ``compose down`` 移除，但 compose/env/镜像仍在
      → 清空陈旧身份后 ``compose up -d`` 重建容器（BUG-382）；
    - 生成文件或镜像缺失 → 回退 :func:`host_container` 完整重建。

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

    # BUG-421：在 running skip / stopped start 之前检查 SQLite data mount 漂移。
    # 漂移时禁止轻量 start，必须 rescue → down → 清身份 → up。
    if _managed_sqlite_data_mount_drifted(workspace, manifest, instance_id, runtime):
        log.info(
            "容器实例 %s 数据挂载已漂移，先救援再 down/up 重建",
            instance_id,
        )
        # BUG-424：fail-safe 救援——两侧数据冲突 / 救援失败 / 异常均抛
        # HostingError 中止，要求人工确认，不进入 down。
        _rescue_container_data_before_rebuild(
            workspace, manifest, instance_id, runtime, strict=True
        )
        try:
            runtime.down(instance_id)
        except DockerError as exc:
            # BUG-423：down 失败仍继续 up 可能复用旧容器/旧挂载或报冲突，
            # 后续却把实例标记为运行——必须立即失败，不得继续。
            raise HostingError(
                f"实例 {instance_id} 挂载漂移修复中 down 失败，已中止"
                f"（禁止继续 up 复用旧挂载）：{exc}",
                instance_id=instance_id,
            ) from exc
        manifest.container.containerId = None
        manifest.container.imageId = None
        manifest.touch()
        manifest.save(workspace.app_manifest_path(instance_id))
        runtime.up(instance_id)
        action = "up"
    elif runtime.is_running(instance_id):
        log.info("容器实例 %s 已在运行，跳过 start", instance_id)
        action = "start"
    else:
        existing = _safe(lambda: runtime.container_id(instance_id, all_containers=True))
        if existing:
            runtime.start(instance_id)
            action = "start"
        elif _can_recreate_container_without_rebuild(workspace, runtime, instance_id):
            # BUG-382：外部 down 后 manifest 仍有陈旧 containerId。
            log.info(
                "容器实例 %s 已无容器但 compose/镜像仍在，使用 compose up -d 重建",
                instance_id,
            )
            manifest.container.containerId = None
            manifest.container.imageId = None
            manifest.touch()
            manifest.save(workspace.app_manifest_path(instance_id))
            runtime.up(instance_id)
            action = "up"
        else:
            log.info(
                "容器实例 %s 无法轻量恢复（compose/镜像缺失），回退完整重建",
                instance_id,
            )
            return host_container(workspace, config, registry, instance_id)

    # 端口：复用此前部署登记的 hostPort
    host_port = manifest.container.hostPort
    if not host_port:
        host_port, _fresh = _ensure_container_port(config, registry, instance_id)
        manifest.container.hostPort = host_port

    # 观测 containerId / imageId；up 重建后必须写回新身份（BUG-382）。
    # 轻量 start 观测失败时保留已落库身份（BUG-344）。
    observed_cid = _safe(lambda: runtime.container_id(instance_id))
    observed_iid = _safe(lambda: runtime.image_id(instance_id))
    if action == "up":
        if not observed_cid:
            observation_error = HostingError(
                f"实例 {instance_id} 重建容器后未能观测到新 containerId",
                instance_id=instance_id,
            )
            with contextlib.suppress(Exception):
                runtime.down(instance_id)
            _mark_failed(workspace, registry, instance_id, manifest, observation_error)
            raise observation_error
        manifest.container.containerId = observed_cid
        manifest.container.imageId = observed_iid
    else:
        manifest.container.containerId = observed_cid or manifest.container.containerId
        manifest.container.imageId = observed_iid or manifest.container.imageId

    # 更新 manifest + registry
    # BUG-084：写回 network 时保留容器路径别名（与 host_container 一致）。
    entry = build_network_entry(
        config,
        host_port,
        internal_port=manifest.container.internalPort,
        path_alias=_container_path_alias(manifest),
    )
    manifest.network = NetworkConfig(**entry)

    # Gate-C C.04：轻量 start 也至少执行必选存活探针（§6.5 触发条件表）。
    # 先进入 VERIFYING，再根据存活探针结果定状态。
    manifest.status = Status.VERIFYING
    manifest.desiredState = DesiredState.RUNNING
    manifest.lastError = None
    _refresh_manifest_workspace_paths(workspace, manifest)
    manifest.touch()
    manifest.save(workspace.app_manifest_path(instance_id))
    registry.upsert_from_manifest(manifest)
    registry.update_status(
        instance_id,
        Status.VERIFYING.value,
        desired_state=DesiredState.RUNNING.value,
    )
    registry.record_started(instance_id)

    # BUG-500：轻量 start 也要重跑必选能力校验（API/DB/迁移），不能只探 GET /。
    # 否则已部署容器只要首页 200 就假绿，API/DB/迁移失败被掩盖为 RUNNING。
    verification = _evaluate_container_verification(
        host_port,
        manifest,
        workspace,
        registry,
        instance_id,
    )
    overall = verification["overall_status"]
    if overall == "passed":
        manifest.status = Status.RUNNING
        registry.record_health_check(instance_id)
        status_detail = "RUNNING"
    elif overall == "degraded":
        manifest.status = Status.DEGRADED
        status_detail = "DEGRADED（可选探针失败）"
        registry.add_event(
            instance_id,
            "lifecycle_stage",
            f"Gate-C 轻量 start 可选探针失败（host_port={host_port}）",
        )
    else:
        manifest.status = Status.FAILED
        manifest.lastError = verification.get("error", "必选探针未通过")[:500]
        status_detail = "FAILED（必选探针未通过）"
        registry.add_event(
            instance_id,
            "lifecycle_stage",
            f"Gate-C 轻量 start 必选探针失败（host_port={host_port}）",
        )

    # CHK-192/P2：verificationSummary 必须在 save 前赋值，否则 reload 后丢失。
    manifest.verificationSummary = {
        "overallStatus": overall,
        "livenessPassed": verification.get("liveness_passed", False),
        "mandatoryAllPassed": verification.get("mandatory_all_passed", False),
        "optionalWarnings": verification.get("optional_warnings", []),
        "observedCapabilities": verification.get("observed_capabilities", []),
    }

    manifest.touch()
    manifest.save(workspace.app_manifest_path(instance_id))
    registry.update_status(
        instance_id,
        manifest.status.value,
        desired_state=DesiredState.RUNNING.value,
        last_error=manifest.lastError,
        clear_last_error=manifest.lastError is None,
    )

    registry.add_event(
        instance_id,
        "start",
        f"容器实例已启动（{action}，{status_detail}，host_port={host_port}）",
    )
    log.info("容器实例 %s 已 %s（%s），端口 %d", instance_id, action, status_detail, host_port)
    return manifest


def _can_recreate_container_without_rebuild(
    workspace: Workspace,
    runtime: DockerRuntime,
    instance_id: str,
) -> bool:
    """compose/env 与镜像仍在时，可用 ``up -d`` 重建容器而无需 build（BUG-382）。"""
    if not workspace.app_compose_path(instance_id).is_file():
        return False
    if not workspace.app_env_path(instance_id).is_file():
        return False
    image_id = _safe(lambda: runtime.image_id(instance_id))
    return bool(image_id)


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


def expected_workspace_derived_paths(workspace: Workspace, instance_id: str) -> dict[str, str]:
    """返回当前 workspace 下可确定派生路径（不含 sourceZipPath）。"""
    return {
        "appPath": str(workspace.app_current(instance_id)),
        "composePath": str(workspace.app_compose_path(instance_id)),
        "dockerfilePath": str(workspace.app_dockerfile_path(instance_id)),
        "gatewayConfigPath": str(workspace.app_gateway_config(instance_id)),
    }


def _refresh_manifest_workspace_paths(workspace: Workspace, manifest: InstanceManifest) -> None:
    """就地刷新可确定派生路径；不改写 sourceZipPath。"""
    paths = expected_workspace_derived_paths(workspace, manifest.id)
    manifest.appPath = paths["appPath"]
    if manifest.container is not None:
        manifest.container.composePath = paths["composePath"]
        manifest.container.dockerfilePath = paths["dockerfilePath"]
    if manifest.static is not None:
        manifest.static.gatewayConfigPath = paths["gatewayConfigPath"]


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


# ---- Gate-C C.04/C.05：实证校验辅助 -----------------------------------------


def _is_valid_sqlite_file(path: Path) -> bool:
    """只读打开 SQLite 文件并执行 PRAGMA schema_version，验证它是有效数据库。"""
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            connection.execute("PRAGMA schema_version").fetchone()
    except (OSError, sqlite3.Error):
        return False
    return True


def _verify_sqlite_database(
    manifest: InstanceManifest,
    workspace: Workspace,
) -> bool:
    """只读打开容器挂载的 SQLite 文件，作为数据库能力证据。

    BUG-492：当 manifest 的 dbFilename 为 null 或指向不存在的占位文件时，
    回退扫描 data 目录下所有 .db/.sqlite/.sqlite3 文件，只要存在一个有效
    SQLite 数据库即视为能力满足。避免 Gate-C 对文件型数据库误判为 FAILED。
    """
    database = manifest.database
    if not manifest.hasDatabase or database is None or database.type != "sqlite":
        return False

    data_dir = workspace.app_data(manifest.id)

    # 优先检查 manifest 声明的文件
    db_filename = Path(database.dbFilename or "app.sqlite").name
    db_path = data_dir / db_filename
    if db_path.is_file() and _is_valid_sqlite_file(db_path):
        return True

    # BUG-492 回退：扫描 data 目录寻找任意有效 SQLite 文件
    if data_dir.is_dir():
        for candidate in sorted(data_dir.iterdir()):
            if candidate.is_file() and candidate.suffix.lower() in (
                ".db",
                ".sqlite",
                ".sqlite3",
            ):
                if _is_valid_sqlite_file(candidate):
                    log.info(
                        "Gate-C SQLite 回退检测：manifest 声明 %s 未命中，"
                        "在 data 目录找到有效数据库 %s（实例 %s）",
                        db_filename,
                        candidate.name,
                        manifest.id,
                    )
                    return True

    return False


def _migration_command_succeeded(
    manifest: InstanceManifest,
    *,
    liveness_ok: bool,
) -> bool:
    """判定 Alembic 是否作为服务启动的受控前置命令已成功。"""
    if not liveness_ok:
        return False
    command = (manifest.entry.start or "").strip()
    lowered = command.lower()
    alembic_pos = lowered.find("alembic upgrade")
    if alembic_pos < 0:
        return False
    guard_pos = lowered.find("&&", alembic_pos)
    if guard_pos < 0:
        return False
    return bool(command[guard_pos + 2 :].strip(" '\""))


def _collect_side_effect_records(
    manifest: InstanceManifest,
    *,
    liveness_ok: bool,
    verification_status: str,
) -> list[Any]:
    """C.R05：收集本次 attempt 产生的外部副作用记录。

    目前检测的副作用类型：
    - migration：Alembic 迁移作为容器启动前置命令执行
    - pre_start：未来扩展（manifest.entry.preStart）

    返回 ``SideEffectRecord`` 列表。未知写入默认 ``autoRecoverable=False``。
    """
    from local_webpage_access.models import SideEffectRecord
    from datetime import datetime, timezone

    records: list[SideEffectRecord] = []
    now = datetime.now(timezone.utc).isoformat()

    # 检测 Alembic 迁移
    start_cmd = (manifest.entry.start or "").strip() if manifest.entry else ""
    if "alembic upgrade" in start_cmd.lower():
        migration_succeeded = _migration_command_succeeded(
            manifest,
            liveness_ok=liveness_ok,
        )
        # 如果存活探针未通过，迁移可能已执行但应用未启动
        # 如果存活探针通过且 guard 后有命令，迁移已成功
        result = "succeeded" if migration_succeeded else "unknown"  # 评审-组2：两分支恒等，化简
        records.append(
            SideEffectRecord(
                kind="migration",
                description=f"Alembic 迁移作为启动前置命令执行（{start_cmd[:100]}）",
                intent="容器启动时执行 alembic upgrade head 以更新数据库 schema",
                executedAt=now,
                result=result,
                compensationMethod="alembic downgrade（需人工执行，无法自动确定回退目标版本）",
                recoveryEvidence=None,
                # 迁移不可自动恢复--schema 变更可能影响数据完整性
                autoRecoverable=False,
            )
        )

    # 未来扩展：检测 pre_start 钩子
    # if manifest.entry and manifest.entry.preStart:
    #     records.append(SideEffectRecord(...))

    return records


def _side_effects_auto_recoverable(records: list[Any]) -> bool:
    """C.R05：判断所有副作用是否可自动恢复。

    只要有任一副作用 ``autoRecoverable=False``，整体不可自动恢复。
    """
    if not records:
        return True
    return all(getattr(r, "autoRecoverable", False) for r in records)


def _evaluate_container_verification(
    host_port: int,
    manifest: InstanceManifest,
    workspace: Workspace,
    registry: Registry,
    instance_id: str,
) -> dict:
    """Gate-C C.04/C.05：评估容器的成功谓词（§6.5）。

    先等待基础存活，再执行证据驱动的探针评估。

    返回字典包含：
    - ``overall_status``: ``"passed"`` / ``"degraded"`` / ``"failed"``
    - ``liveness_passed``: bool
    - ``mandatory_all_passed``: bool
    - ``optional_warnings``: list[str]
    - ``observed_capabilities``: list[str]
    - ``error``: str | None
    """
    # 先等待 HTTP 就绪（复用 hosting 模块的 _wait_for_http，
    # 与测试中 monkeypatch _http_ok 对齐）
    liveness_ok = _wait_for_http(host_port)
    if not liveness_ok:
        return {
            "overall_status": "failed",
            "liveness_passed": False,
            "mandatory_all_passed": False,
            "optional_warnings": [],
            "observed_capabilities": [],
            "error": f"基础存活探针超时（host_port={host_port}，{_CONTAINER_HEALTH_ATTEMPTS} 次未响应）",
        }

    # 基础存活通过 -> 收集能力
    observed: set[str] = set()
    observed.add("ui")  # 存活即服务可达

    # CHK-192/P1：从 manifest 加载持久化的能力契约（含 requiredProbes），
    # 不再临时推断（原 _infer_capability_contract 丢失 requiredProbes 且无探针执行）。
    contract = _load_capability_contract(manifest)
    required = contract.required_capabilities

    # 执行 mandatory 探针（如 /health），结果作成功门槛。
    # BUG-499：只有 source in ("declared", "discovered") 的探针通过才可作为
    # API 能力证据；guessed 探针（如通用 /health、api_probe）仅诊断，
    # 不得满足 servesApi（否则偶然 /health 200 假绿、无标准路径 API 假红）。
    mandatory_all_passed = True
    optional_warnings: list[str] = []
    successful_business_probe = False
    for spec in contract.requiredProbes:
        passed, code = _probe_path(
            host_port,
            spec.path,
            expected_status=spec.expectedStatus,
        )
        if passed and spec.source in ("declared", "discovered"):
            successful_business_probe = True
        if spec.isMandatory:
            if not passed:
                mandatory_all_passed = False
        else:
            if not passed:
                optional_warnings.append(f"可选探针 {spec.path} 未通过（code={code}）")

    # BUG-481：契约是要求，不是证据。首页存活不再自动补齐 API/DB/迁移。
    if successful_business_probe:
        observed.add("api")
    if contract.requiresDatabase and _verify_sqlite_database(manifest, workspace):
        observed.add("database")
    if contract.requiresMigrations and _migration_command_succeeded(
        manifest,
        liveness_ok=liveness_ok,
    ):
        observed.add("migrations")

    # BUG-504：api 能力唯一证据来源是 declared/discovered 探针。契约要求
    # servesApi 但无此类探针时，该能力无法实证——不得构成不可满足的成功谓词
    # （正常后端会稳定 failed 假红），降级为告警 + DEGRADED，保持诚实可见。
    has_api_evidence_source = any(
        probe.source in ("declared", "discovered") for probe in contract.requiredProbes
    )
    if contract.servesApi and not has_api_evidence_source:
        optional_warnings.append(
            "API 能力无法实证：契约要求 servesApi 但无声明/发现探针，"
            "仅以存活探针作为容器健康证据（如有 /health 端点，请在源码中声明）"
        )

    verifiable_required = {
        capability for capability in required if capability != "api" or has_api_evidence_source
    }
    capabilities_covered = verifiable_required.issubset(observed)

    # 总体判定（CHK-192/P1：能力未覆盖 -> failed，不再假报 passed）
    if not liveness_ok or not mandatory_all_passed:
        overall = "failed"
    elif not capabilities_covered:
        overall = "failed"
    elif optional_warnings:
        overall = "degraded"
    else:
        overall = "passed"

    return {
        "overall_status": overall,
        "liveness_passed": liveness_ok,
        "mandatory_all_passed": mandatory_all_passed,
        "optional_warnings": optional_warnings,
        "observed_capabilities": sorted(observed),
        "error": (
            None
            if overall != "failed"
            else (
                "必选探针未通过"
                if not mandatory_all_passed
                else f"未观测到所需能力：{', '.join(sorted(verifiable_required - observed))}"
            )
        ),
    }


def _load_capability_contract(
    manifest: InstanceManifest,
) -> "CapabilityContract":
    """Gate-C C.04：从 manifest 加载持久化的能力契约。

    CHK-192/P1：优先使用导入时由 candidate_generator 生成并持久化到
    ``manifest.capabilityContract``（dict）的契约，其中包含 ``requiredProbes``。
    若 manifest 未存储契约（旧实例或测试用例），返回最小契约
    ``CapabilityContract(servesUi=True)``--仅要求存活可达，
    避免在无探针信息时假报能力未覆盖为失败。
    """
    from local_webpage_access.models import ProbeSpec

    raw = getattr(manifest, "capabilityContract", None)
    if isinstance(raw, dict) and raw:
        probes = [
            ProbeSpec(**p) if isinstance(p, dict) else p for p in raw.get("requiredProbes", [])
        ]
        return CapabilityContract(
            servesUi=raw.get("servesUi", False),
            servesApi=raw.get("servesApi", False),
            requiresDatabase=raw.get("requiresDatabase", False),
            requiresMigrations=raw.get("requiresMigrations", False),
            requiredProbes=probes,
        )
    # 兜底：无持久化契约时仅要求存活可达
    return CapabilityContract(servesUi=True)


def _probe_path(
    host_port: int,
    path: str,
    *,
    expected_status: int = 200,
    timeout: float = 2.0,
) -> tuple[bool, int | None]:
    """Gate-C C.05：对指定路径执行 HTTP GET 探针。

    CHK-192/P1：区别于 :func:`_http_ok`（固定命中 ``/``），本函数命中
    :class:`ProbeSpec` 声明的路径（如 ``/health``），作为 mandatory 探针门槛。

    返回 ``(passed, status_code)``：
    - 状态码等于 ``expected_status`` 或同为 2xx/3xx -> ``passed=True``
    - 其它 -> ``passed=False``
    """
    url = mark_probe_url(f"http://127.0.0.1:{host_port}{path}")
    try:
        resp = urlopen_direct(url, timeout=timeout)
        code = getattr(resp, "status", None) or resp.getcode()
        code_int = int(code)
        if expected_status and code_int == expected_status:
            return (True, code_int)
        if not expected_status and 200 <= code_int < 400:
            return (True, code_int)
        if 200 <= code_int < 400:
            return (True, code_int)
        return (False, code_int)
    except urllib.error.HTTPError as exc:
        return (False, exc.code)
    except Exception:  # noqa: BLE001
        return (False, None)


def _liveness_failed_rollback(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
    manifest: InstanceManifest,
    host_port: int,
    fresh_port: bool,
    error: str,
) -> None:
    """Gate-C C.04：必选探针失败时回滚容器与端口。"""
    import contextlib

    # 停止容器
    with contextlib.suppress(Exception):
        runtime = DockerRuntime(workspace, registry)
        if runtime.is_running(instance_id):
            runtime.down(instance_id)

    # 释放端口
    if fresh_port:
        with contextlib.suppress(Exception):
            PortAllocator(config=config, registry=registry).release_instance(instance_id)

    manifest.status = Status.FAILED
    manifest.lastError = error[:500]
    manifest.touch()
    with contextlib.suppress(Exception):
        manifest.save(workspace.app_manifest_path(instance_id))
    registry.update_status(instance_id, Status.FAILED.value, last_error=error[:500])
    registry.add_event(
        instance_id,
        "lifecycle_stage",
        f"Gate-C 必选探针失败：{error[:200]}",
    )


def _http_ok(host_port: int, *, timeout: float = 2.0) -> bool:
    """单次 HTTP GET 健康探测（2xx/3xx 视为成功）。"""
    url = mark_probe_url(f"http://127.0.0.1:{host_port}/")
    try:
        resp = urlopen_direct(url, timeout=timeout)
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
    # BUG-422：防御性一致化派生路径（含 gatewayConfigPath / appPath）
    _refresh_manifest_workspace_paths(workspace, manifest)
    entry = build_network_entry(config, host_port, path_alias=path_alias)
    manifest.network = NetworkConfig(**entry)
    registry.set_static_enabled(instance_id, True)
    return manifest


# ---- 辅助函数 --------------------------------------------------------------


def find_index_html(directory: Path) -> Path | None:
    """寻找入口 HTML：优先 ``index.html``，否则任意顶层/一层 ``*.html``。"""
    top = directory / "index.html"
    if top.is_file():
        return top
    # 顶层任意 .html（非 index）：字典序稳定选一个
    try:
        top_html = sorted(
            p for p in directory.iterdir() if p.is_file() and p.name.lower().endswith(".html")
        )
    except (PermissionError, OSError):
        top_html = []
    if top_html:
        return top_html[0]
    try:
        for sub in sorted(directory.iterdir()):
            if sub.is_dir():
                candidate = sub / "index.html"
                if candidate.is_file():
                    return candidate
        # 一层子目录内任意 .html
        for sub in sorted(directory.iterdir()):
            if not sub.is_dir():
                continue
            try:
                nested = sorted(
                    p for p in sub.iterdir() if p.is_file() and p.name.lower().endswith(".html")
                )
            except (PermissionError, OSError):
                continue
            if nested:
                return nested[0]
    except (PermissionError, OSError):
        pass
    return None


def _ensure_public_index(public_dir: Path, entry: Path, current_dir: Path) -> None:
    """保证 ``public/index.html`` 存在，使网关根路径可打开。

    入口若本就叫 index.html，sync/promote 后通常已在位；否则把入口页复制为
    ``public/index.html``（相对资源路径仍按同源目录解析）。
    """
    dest = public_dir / "index.html"
    if dest.is_file():
        return
    src: Path | None = None
    if (public_dir / entry.name).is_file():
        src = public_dir / entry.name
    else:
        with contextlib.suppress(ValueError):
            rel = entry.relative_to(current_dir)
            candidate = public_dir / rel
            if candidate.is_file():
                src = candidate
    if src is None and entry.is_file():
        src = entry
    if src is not None and src.is_file():
        shutil.copy2(src, dest)


def find_build_output(project_dir: Path, hint: str | None = None) -> Path | None:
    """识别构建产物目录（dist/、build/、out/ 等）。

    若 *hint* 非空（monorepo 子包的产物相对路径，如 packages/web/dist），
    优先检查该路径，再回退到常规扫描。
    """
    if hint:
        candidate = project_dir / hint
        if candidate.is_dir():
            try:
                # BUG-508：拒绝越界 hint（如 ../shared/dist），防止把仓库外目录当产物对外服务。
                if any(candidate.iterdir()) and candidate.resolve().is_relative_to(
                    project_dir.resolve()
                ):
                    return candidate
            except (PermissionError, OSError):
                pass
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
    # 评审-组2：dst 被外部破坏成普通文件时 rmtree 抛 NotADirectoryError，无自愈
    if dst.is_dir():
        shutil.rmtree(dst)
    elif dst.exists():
        dst.unlink()
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
    """终止整个进程树（BUG-183 / IMP-039）。委托 :mod:`build_process`。"""
    from local_webpage_access.build_process import kill_process_tree

    kill_process_tree(proc)


def run_command(
    cmd: str,
    *,
    cwd: Path,
    log_path: Path,
    timeout: int = _BUILD_TIMEOUT,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    """运行 shell 命令，stdout/stderr 追加写入 log_path。

    命令来自项目识别器的确定性推断，``shell=True`` 可接受。以独立进程组运行；
    超时或 IMP-039 取消时杀整棵进程树（BUG-183），不残留 npm/node 孙进程孤儿。
    """
    from local_webpage_access.build_process import (
        current_build_instance_id,
        current_build_token,
        get_build_process_hub,
        kill_process_tree,
        popen_new_session_kwargs,
        worker_identity_token,
    )
    from local_webpage_access.errors import BuildCancelled
    from local_webpage_access.logs import open_append

    log_path.parent.mkdir(parents=True, exist_ok=True)
    popen_kwargs: dict = {
        "cwd": str(cwd),
        "shell": True,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "env": env,
        "text": True,
        **popen_new_session_kwargs(),
    }

    hub = get_build_process_hub()
    instance_id = current_build_instance_id()
    build_token = current_build_token()

    def _should_cancel() -> bool:
        if instance_id is None:
            return False
        if hub.is_cancel_requested(instance_id):
            return True
        # 跨进程取消：读 build-locks 持久化标志
        try:
            from local_webpage_access.build_queue import _gates

            for gate in list(_gates.values()):
                if build_token is not None and gate.is_cancel_requested(
                    instance_id, build_token=build_token
                ):
                    return True
        except Exception:  # noqa: BLE001
            pass
        return False

    with open_append(log_path) as fh:
        fh.write(f"\n$ {cmd}\n")
        fh.flush()
        proc = subprocess.Popen(cmd, **popen_kwargs)
        identity = worker_identity_token(cmd)
        if instance_id is not None:
            hub.register(instance_id, proc, identity=identity)
            _persist_worker(instance_id, proc, identity)
        stdout_data = ""
        timed_out = False
        cancelled = False
        completed_normally = False
        try:
            deadline = time.monotonic() + timeout
            while True:
                if _should_cancel():
                    cancelled = True
                    kill_process_tree(proc)
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    kill_process_tree(proc)
                    break
                try:
                    chunk, _ = proc.communicate(timeout=min(0.25, remaining))
                    if chunk:
                        stdout_data = chunk
                    completed_normally = True
                    break
                except subprocess.TimeoutExpired:
                    # 部分输出由 Popen 内部缓冲保留，下次 communicate 自动累加，
                    # 此处无需手动收集（手动收集会与最终全量返回重复）。
                    continue
            if not completed_normally:
                # 仅在取消/超时（进程被杀、首次 communicate 未完成）时 drain
                # 剩余输出。正常完成时 communicate() 已返回全量，二次调用会
                # 重复返回同样的全量数据，导致构建日志翻倍（BUG-273）。
                try:
                    more, _ = proc.communicate(timeout=5)
                    if more:
                        stdout_data = (stdout_data or "") + more
                except Exception:  # noqa: BLE001
                    pass
            if stdout_data:
                fh.write(stdout_data)
            fh.flush()
        finally:
            if instance_id is not None:
                hub.unregister(instance_id, proc)
                _clear_worker(instance_id)

        if cancelled or (instance_id and _should_cancel()):
            raise BuildCancelled(
                f"构建已取消：{cmd}",
                command=cmd,
                instance_id=instance_id,
            )
        if timed_out:
            raise BuildError(
                f"命令超时（{timeout}s）：{cmd}",
                command=cmd,
                timeout=timeout,
            )
    if proc.returncode != 0:
        # 取消杀树后 returncode 常非零：若已请求取消，优先报 BuildCancelled
        if instance_id and _should_cancel():
            raise BuildCancelled(
                f"构建已取消：{cmd}",
                command=cmd,
                instance_id=instance_id,
            )
        raise BuildError(
            f"命令失败（exit {proc.returncode}）：{cmd}",
            command=cmd,
            exit_code=proc.returncode,
            log_path=str(log_path),
        )
    return subprocess.CompletedProcess(args=cmd, returncode=proc.returncode, stdout="")


def _persist_worker(instance_id: str, proc: subprocess.Popen, identity: str) -> None:
    try:
        import os

        pgid = None
        if sys.platform != "win32":
            try:
                pgid = os.getpgid(proc.pid)
            except ProcessLookupError:
                pgid = None
        from local_webpage_access.build_process import current_build_token
        from local_webpage_access.build_queue import _gates

        build_token = current_build_token()
        if build_token is None:
            return
        for gate in list(_gates.values()):
            gate.update_build_task(
                instance_id,
                build_token=build_token,
                worker_pid=proc.pid,
                worker_pgid=pgid,
                worker_identity=identity,
            )
    except Exception:  # noqa: BLE001
        log.debug("持久化 worker pid 失败", exc_info=True)


def _clear_worker(instance_id: str) -> None:
    try:
        from local_webpage_access.build_process import current_build_token
        from local_webpage_access.build_queue import _gates

        build_token = current_build_token()
        if build_token is None:
            return
        for gate in list(_gates.values()):
            gate.update_build_task(instance_id, build_token=build_token, clear_worker=True)
    except Exception:  # noqa: BLE001
        pass


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
    frontend_markers = {
        "vite",
        "react",
        "react-dom",
        "vue",
        "svelte",
        "preact",
        "@vitejs/plugin-react",
    }
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
    "expected_workspace_derived_paths",
    "find_index_html",
    "find_build_output",
    "sync_static_to_public",
    "sync_dir",
    "run_command",
]
