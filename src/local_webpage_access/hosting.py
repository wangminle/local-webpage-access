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
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from local_webpage_access import rebuild_txn
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
from local_webpage_access.logging import get_logger, now_iso, write_instance_log
from local_webpage_access.models import (
    CapabilityContract,
    DesiredState,
    InstanceManifest,
    NetworkConfig,
    RouteMode,
    StaticConfig,
    Status,
)
from local_webpage_access.ownership import Ownership
from local_webpage_access.paths import Workspace, resolve_source_workdir
from local_webpage_access.probe import mark_probe_url, urlopen_direct, urlopen_no_redirect
from local_webpage_access.ports import PortAllocator, build_network_entry, is_port_listening
from local_webpage_access.registry import Registry
from local_webpage_access.static_gateway import StaticGateway
from local_webpage_access.verification_config import effective_capability_contract

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


def stop_or_abort(
    runtime: DockerRuntime,
    instance_id: str,
    *,
    purpose: str,
    down: bool = True,
    only_if_running: bool = True,
) -> None:
    """统一停机语义（issue #54 / N3，WBS S2-01）。

    停机（默认 ``compose down``；``down=False`` 时用 ``compose stop``）失败且实例
    可能仍在运行时抛出 :class:`HostingError`，文案写明「实例仍在运行，
    <purpose> 已中止」。调用方必须先调用本函数、成功之后才允许修改 manifest
    容器身份。「本来就没在跑」视为成功；运行状态查询失败同样中止（fail-safe，
    BUG-429）。``only_if_running=False`` 时无论是否在跑都执行停机命令——挂载
    漂移修复与迁移需要移除「已停止但仍存在」的旧容器。
    """
    try:
        running = runtime.is_running(instance_id)
    except DockerError as exc:
        raise HostingError(
            f"实例 {instance_id} 运行状态无法确认，{purpose} 已中止：{exc}",
            instance_id=instance_id,
        ) from exc
    if only_if_running and not running:
        return
    try:
        if down:
            runtime.down(instance_id)
        else:
            runtime.stop(instance_id)
    except DockerError as exc:
        raise HostingError(
            f"实例仍在运行，{purpose} 已中止：{exc}",
            instance_id=instance_id,
        ) from exc


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
        # S2-08 / issue #59：静态路径也写阶段记录（与容器路径同一记录器）。
        _record_stage(workspace, registry, instance_id, "host_start")
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
        _record_stage(workspace, registry, instance_id, "sync_done")

        # 3-4. 分配端口 + 启用网关（WBS-10.03/04；enable 内部重载网关配置）
        manifest = _enable_static(workspace, config, registry, instance_id, manifest, public_dir)
        _record_stage(workspace, registry, instance_id, "gateway_reload_done")

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


# BUG-897：packageManager 的版本号来自导入的 package.json（外部内容），会被
# 拼进宿主 ``sh -c`` 命令与 Dockerfile ``RUN``——只接受纯版本字符，含其余
# 字符（空格/引号/;/$ 等）一律按未声明处理，杜绝命令注入。
_PM_VERSION_RE = re.compile(r"[0-9A-Za-z._-]+")


def _read_declared_package_manager(work_dir: Path) -> tuple[str, str] | None:
    """读取 ``work_dir/package.json`` 的 ``packageManager`` 字段（BUG-877）。

    返回 ``(包管理器名, 版本)``（形如 ``("pnpm", "11.0.1")``）；未声明、
    文件缺失或解析失败返回 ``None``。
    """
    import json

    pkg_path = work_dir / "package.json"
    if not pkg_path.is_file():
        return None
    try:
        data = json.loads(pkg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    declared = str(data.get("packageManager") or "").strip()
    # 形如 "pnpm@11.0.1"（name@version）；两段都非空才生效
    if "@" not in declared:
        return None
    name, _, version = declared.rpartition("@")
    name = name.strip()
    # Corepack 规范允许带哈希校验后缀（"pnpm@9.12.0+sha512...."）；CLI 参数
    # <pm>@<version> 只接受纯版本号，须剥离 "+sha..." 段。
    version = version.split("+", 1)[0].strip()
    if not name or not version:
        return None
    if not _PM_VERSION_RE.fullmatch(version):
        return None
    return name, version


def _resolve_host_install_command(install: str, work_dir: Path) -> str:
    """宿主前端安装命令的工具链解析（BUG-860 / BUG-877）。

    scanner 生成的 ``corepack enable && pnpm install --frozen-lockfile`` 原样在
    宿主执行有两个高频失败：macOS 上 ``corepack enable`` 写 /usr/local/bin 需
    root（EACCES）；Ubuntu apt 版 node 未自带 corepack（127）。宿主侧改写优先级：

    1. ``package.json`` 声明了 ``packageManager: <pm>@<version>`` 且 corepack
       可用 → ``corepack <pm>@<version> install ...``（BUG-877：corepack 按需取
       声明版本，避免宿主老版本 pnpm 与 lockfile 不匹配）；
    2. ``<pm>`` 已在 PATH → 去掉 ``corepack enable &&`` 前缀直接执行（免 root）；
    3. corepack 可用 → ``corepack <pm> install ...``（免 enable 形式）；
    4. 都不可用 → 原样执行（如实失败，BUILD_ERROR 消息已带 build.log 路径）。

    非 ``corepack enable && ...`` 形态的命令（``npm ci`` / ``npm install``）原样返回。
    """
    import re

    m = re.match(
        r"^\s*corepack\s+enable\s*&&\s*(pnpm|yarn)(\s+.*?)\s*$", install, re.IGNORECASE
    )
    if not m:
        return install
    pm = m.group(1).lower()
    rest = m.group(2).strip()
    declared = _read_declared_package_manager(work_dir)
    corepack = shutil.which("corepack")
    if declared and declared[0].lower() == pm and corepack:
        return f"corepack {pm}@{declared[1]} {rest}"
    if shutil.which(pm):
        if declared and declared[0].lower() == pm:
            # corepack 不可用但声明了版本：PATH 版本未必满足 lockfile，告警留痕
            log.warning(
                "工作区声明 packageManager=%s@%s，但 corepack 不可用且 PATH 中 %s "
                "版本未验证；退回 PATH 版本执行（失败时请安装 corepack）",
                pm,
                declared[1],
                pm,
            )
        return f"{pm} {rest}"
    if corepack:
        return f"corepack {pm} {rest}"
    log.warning("corepack 与 %s 均不在 PATH，安装命令按原样执行（大概率失败）：%s", pm, install)
    return install


def _resolve_host_build_command(build: str, work_dir: Path) -> str:
    """宿主前端构建命令的工具链解析（BUG-889，与 install 侧 BUG-860/877 同策）。

    scanner 生成的 ``entry.build`` 多以 ``yarn ``/``pnpm `` 开头（如 monorepo
    的 ``yarn workspace <pkg> run build``）。install 已被
    :func:`_resolve_host_install_command` 改写为 corepack 包装执行，但 build
    裸跑时宿主可能没有全局 yarn/pnpm（只有 corepack），或 PATH 版本与
    lockfile 声明版本不一致——同一工程两条命令必须用同一工具链。解析优先级
    与 install 侧一致：声明版本+corepack 固定版本 → PATH 原样 → corepack
    代理 → 原样如实失败。非 yarn/pnpm 开头的命令（npm run build 等）原样返回。

    BUG-896：monorepo 拓扑构建是 ``&&`` 连接的多段命令
    （``yarn workspace a run build && yarn workspace b run build``）——按
    ``&&`` 逐段解析后原样拼回（未命中 yarn/pnpm 前缀的分段保持原文，引号内
    含 ``&&`` 的普通命令不会被改写）。
    """
    if "&&" not in build:
        return _resolve_build_segment(build, work_dir)
    return "&&".join(_resolve_build_segment(part, work_dir) for part in build.split("&&"))


def _resolve_build_segment(segment: str, work_dir: Path) -> str:
    """单个构建分段的工具链解析（BUG-889/896；保留分段前后空白）。"""
    m = re.match(r"^(\s*)(pnpm|yarn)(\s+.*?)(\s*)$", segment, re.IGNORECASE)
    if not m:
        return segment
    lead = m.group(1)
    pm = m.group(2).lower()
    rest = m.group(3).strip()
    trail = m.group(4)
    declared = _read_declared_package_manager(work_dir)
    corepack = shutil.which("corepack")
    if declared and declared[0].lower() == pm and corepack:
        return f"{lead}corepack {pm}@{declared[1]} {rest}{trail}"
    if shutil.which(pm):
        return segment
    if corepack:
        return f"{lead}corepack {pm} {rest}{trail}"
    return segment


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

    # BUG-833 / S2-02 / D4：重建已运行的纯前端实例时，install/build 失败发生在
    # 产物同步（sync_dir）之前，public/ 旧产物原样保留、仍在服务——失败分支
    # 据此保留观测态，不把仍在服务的实例标成 FAILED。
    prior_row = registry.get_instance(instance_id)
    was_serving = (
        prior_row is not None
        and str(prior_row.get("status") or "") == Status.RUNNING.value
        and public_dir.is_dir()
        and any(public_dir.iterdir())
    )
    content_sync_started = False

    registry.update_status(instance_id, Status.BUILDING.value)
    build_id = registry.add_build(
        instance_id,
        status="running",
        log_path=str(build_log),
    )
    op_started_at = now_iso()  # S2-02 / D4：操作态 startedAt

    try:
        # DEV-132：实例级构建环境变量（manifest.buildEnv，如 VITE_BASE）注入
        # 安装/构建命令。subprocess env 是整体替换语义——须以 os.environ 为底
        # 合并，否则 npm/node 失去 PATH 而找不到可执行文件。
        import os

        from local_webpage_access.instance_settings import effective_build_env

        build_env = {**os.environ, **effective_build_env(manifest)}
        # S2-08 / issue #59：前端构建路径也写阶段记录（与容器/静态同一记录器）。
        _record_stage(workspace, registry, instance_id, "host_start")
        # 1-5. 安装 + 构建（WBS-11.01~06）
        if manifest.entry.install:
            # BUG-860 / BUG-877：宿主工具链解析（corepack enable 免 root 改写 /
            # packageManager 声明版本优先），失败回退原命令如实报错。
            install_cmd = _resolve_host_install_command(manifest.entry.install, work_dir)
            if install_cmd != manifest.entry.install:
                write_instance_log(
                    workspace.apps,
                    instance_id,
                    "build",
                    f"安装命令已按宿主工具链改写：{install_cmd}",
                )
                if install_cmd.startswith("corepack"):
                    # corepack 按需下载声明版本时会在交互环境弹下载确认，
                    # 构建子进程非 TTY，统一关闭提示避免挂起
                    build_env.setdefault("COREPACK_ENABLE_DOWNLOAD_PROMPT", "0")
            write_instance_log(
                workspace.apps, instance_id, "build", f"安装：{install_cmd}"
            )
            run_command(install_cmd, cwd=work_dir, log_path=build_log, env=build_env)
            _record_stage(workspace, registry, instance_id, "install_done")
        if manifest.entry.build:
            # BUG-889：build 与 install 共用同一工具链解析——install 已改写为
            # corepack 包装时，裸跑 yarn/pnpm build 仍会 127 或版本不匹配。
            build_cmd = _resolve_host_build_command(manifest.entry.build, work_dir)
            if build_cmd != manifest.entry.build:
                write_instance_log(
                    workspace.apps,
                    instance_id,
                    "build",
                    f"构建命令已按宿主工具链改写：{build_cmd}",
                )
                if build_cmd.startswith("corepack"):
                    build_env.setdefault("COREPACK_ENABLE_DOWNLOAD_PROMPT", "0")
            write_instance_log(
                workspace.apps, instance_id, "build", f"构建：{build_cmd}"
            )
            run_command(build_cmd, cwd=work_dir, log_path=build_log, env=build_env)
            _record_stage(workspace, registry, instance_id, "build_done")
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
        # sync_dir 先清空旧产物；中途复制失败也不能再声明旧版本可用。
        content_sync_started = True
        sync_dir(dist, public_dir)
        _record_stage(workspace, registry, instance_id, "sync_done")
        registry.finish_build(build_id, status="success")
        registry.add_event(instance_id, "build", f"构建成功，产物来自 {dist.name}/")

        # 9-10. 启用网关 + 健康检查（WBS-11.09/10；enable 内部重载网关配置）
        manifest = _enable_static(workspace, config, registry, instance_id, manifest, public_dir)
        _record_stage(workspace, registry, instance_id, "gateway_reload_done")
        manifest.status = Status.RUNNING
        manifest.desiredState = DesiredState.RUNNING
        manifest.lastError = None
        # S2-02 / D4：操作态——前端构建部署成功
        manifest.lastOperation = {
            "kind": "deploy",
            "status": "succeeded",
            "buildId": build_id,
            "startedAt": op_started_at,
            "finishedAt": now_iso(),
            "source": "host_static",
        }
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
        # S2-02 / D4：取消是操作态——旧产物仍在服务，实例 status 保留观测值。
        registry.update_last_operation(
            instance_id,
            {
                "kind": "deploy",
                "status": "cancelled",
                "buildId": build_id,
                "startedAt": op_started_at,
                "finishedAt": now_iso(),
                "error": str(exc)[:500],
                "source": "host_static",
            },
        )
        registry.update_last_error(instance_id, str(exc)[:500])
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
        if was_serving and not content_sync_started:
            # BUG-833 / S2-02 / D4：旧产物未被替换且仍在服务——实例 status 保留
            # 观测值（running，含把 BUILDING 恢复为运行），失败只进操作态与
            # lastError；与取消分支同语义。
            error_summary = str(exc)[:500]
            failed_operation = {
                "kind": "deploy",
                "status": "failed",
                "buildId": build_id,
                "startedAt": op_started_at,
                "finishedAt": now_iso(),
                "error": error_summary,
                "source": "host_static",
            }
            registry.update_last_operation(instance_id, failed_operation)
            registry.update_last_error(instance_id, error_summary)
            manifest.lastError = error_summary
            manifest.lastOperation = dict(failed_operation)
            manifest.touch()
            with contextlib.suppress(Exception):
                manifest.save(workspace.app_manifest_path(instance_id))
            registry.update_status(instance_id, Status.RUNNING.value)
            registry.add_event(
                instance_id, "error", f"前端重建失败，旧版本仍在服务：{error_summary}"
            )
            raise
        _mark_failed(
            workspace,
            registry,
            instance_id,
            manifest,
            exc,
            operation_kind="deploy",
            operation_source="host_static",
            build_id=build_id,
        )
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


def _mirror_stage_to_global_log(workspace: Workspace, instance_id: str, stage: str) -> None:
    """issue #16 / BUG-603、S2-08 / issue #59：按操作来源镜像 lifecycle_stage 到 ``lwa.log``。

    CLI 触发的重建本就把阶段写进 lwa.log（进程日志落点）；manager/daemon/agent
    触发的操作（reconcile 自动恢复 / inbox 处理 / 管理页与 Agent 通道的重建）
    同样经历完整阶段，但其进程日志只落 manager.log / daemon.log（IMP-034），
    运维盯 lwa.log 会误判构建卡死。非 CLI 来源用 O_APPEND 裸写镜像一行
    ``source=<来源> lifecycle_stage …``（append_global_log，防日志轮转覆盖）；
    CLI 进程零影响（不重复镜像）。
    """
    from local_webpage_access.logging import append_global_log, current_op_source

    source = current_op_source()
    if source == "cli":
        return
    append_global_log(
        workspace.logs,
        f"source={source} lifecycle_stage instance={instance_id} stage={stage}",
        logger_name="local_webpage_access.hosting",
    )


# S2-08 / issue #59：阶段事件写入失败每进程每实例只告警一次（原 host_container
# 闭包的 nonlocal 语义，抽出共用函数后提升为模块级去重）。
_STAGE_EVENT_WARNED: set[str] = set()


def _record_stage(workspace: Workspace, registry: Registry, instance_id: str, stage: str) -> None:
    """S2-08：统一的阶段记录器——进程日志 + 按来源镜像 lwa.log + registry 事件。

    事件写失败不阻断操作，但须可见（#59-2，S1-07）：首次失败按实例告警一次。
    """
    msg = f"stage={stage}"
    log.info("lifecycle_stage instance=%s %s", instance_id, msg)
    _mirror_stage_to_global_log(workspace, instance_id, stage)
    try:
        registry.add_event(instance_id, "lifecycle_stage", msg)
    except Exception as exc:  # noqa: BLE001 — 事件写失败不阻断操作，但须可见（#59-2）
        if instance_id not in _STAGE_EVENT_WARNED:
            _STAGE_EVENT_WARNED.add(instance_id)
            log.warning("lifecycle_stage 事件写入失败（instance=%s）：%s", instance_id, exc)


def _build_first_applies(
    runtime: DockerRuntime,
    manifest: InstanceManifest,
    operation_kind: str,
) -> bool:
    """S2-03：本次重建是否走先建后换（旧容器在跑、策略为 build-first）。

    无法确认运行状态时返回 False，回到旧顺序；旧顺序的 ``stop_or_abort`` 会对
    不确定状态 fail-safe 中止，不会在状态未知时继续销毁旧容器。
    """
    if operation_kind != "rebuild" or rebuild_txn.rebuild_strategy(manifest) != "build-first":
        return False
    try:
        return bool(runtime.is_running(manifest.id))
    except Exception:  # noqa: BLE001
        return False


def _finalize_build_artifacts(
    workspace: Workspace,
    registry: Registry,
    instance_id: str,
    manifest: InstanceManifest,
    build_id: int,
) -> None:
    """S3-01：成功构建留档——新镜像打 ``b<build_id>`` 标签、生成物快照、builds 行补记产物。

    留档失败（无法解析镜像 / 打标签失败）只告警不阻断部署——回滚只是缺失一个
    候选版本，不影响本次服务。
    """
    from local_webpage_access import versions as versions_mod

    repo = rebuild_txn.image_repo(manifest)
    image_id = versions_mod.resolve_repo_image_id(repo)
    if not image_id:
        log.warning(
            "实例 %s 构建 #%s：无法解析 %s:latest 的镜像 ID，跳过版本留档",
            instance_id, build_id, repo,
        )
        return
    ref = versions_mod.build_image_ref(repo, build_id)
    try:
        versions_mod.tag_build_image(image_id, ref)
    except DockerError as exc:
        log.warning("实例 %s 构建 #%s：镜像打标签 %s 失败（%s），跳过版本留档",
                    instance_id, build_id, ref, exc)
        return
    try:
        snapshot = versions_mod.snapshot_config(workspace, instance_id, build_id)
        from local_webpage_access.rollback import archive_version_migration

        archive_version_migration(workspace, instance_id, build_id, manifest)
        source_hash = None
        try:
            from local_webpage_access.lifecycle import _compute_source_fingerprint

            source_hash = _compute_source_fingerprint(str(workspace.app_current(instance_id)))
        except Exception:  # noqa: BLE001 — 指纹失败不阻塞留档
            log.debug("实例 %s 构建 #%s：源码哈希计算失败", instance_id, build_id)
        registry.record_build_artifacts(
            build_id,
            image_ref=ref,
            image_id=image_id,
            source_hash=source_hash or None,
            config_snapshot=str(snapshot),
        )
    except Exception as exc:  # noqa: BLE001 — 留档失败只告警，不阻断已成功的构建
        log.warning(
            "实例 %s 构建 #%s：版本留档失败（%s），撤回未登记标签（本次部署不中断）",
            instance_id, build_id, exc,
        )
        with contextlib.suppress(Exception):
            versions_mod._image_rm(ref)
        shutil.rmtree(
            versions_mod.build_snapshot_dir(workspace, instance_id, build_id),
            ignore_errors=True,
        )


def _gc_after_deploy(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
    in_use_image_id: str | None,
) -> None:
    """S3-01：部署成功后 GC 超保留期的历史版本镜像与快照；失败仅告警。"""
    from local_webpage_access import versions as versions_mod

    try:
        versions_mod.gc_build_artifacts(
            workspace, config, registry, instance_id, in_use_image_id=in_use_image_id
        )
    except Exception:  # noqa: BLE001 — GC 不得阻断已成功部署
        log.warning("实例 %s 部署后版本 GC 失败（不阻断部署）", instance_id, exc_info=True)


def _revoke_unverified_build_archive(
    workspace: Workspace,
    registry: Registry,
    instance_id: str,
    build_id: int,
) -> None:
    """部署未通过验证时撤销该构建的可回滚留档。

    镜像构建成功会把 builds 行标成 success；若此时已经打了版本标签并写入
    产物列，失败版本仍会被选版和 GC 当成可用版本。这里删标签、清快照、置空
    产物列，使 ``is_rollback_candidate`` 不再成立。
    """
    from local_webpage_access import versions as versions_mod

    try:
        rows = registry.list_builds(instance_id, limit=200)
    except Exception:  # noqa: BLE001 — 撤销失败不得掩盖原始部署错误
        log.warning("实例 %s 构建 #%s：读取留档以便撤销失败", instance_id, build_id, exc_info=True)
        return
    row = next((item for item in rows if int(item["id"]) == int(build_id)), None)
    if row is None or not row.get("image_ref"):
        return
    ref = str(row["image_ref"])
    try:
        versions_mod._image_rm(ref)
    except Exception:  # noqa: BLE001
        log.warning(
            "实例 %s 构建 #%s：撤销留档时删除镜像 %s 失败",
            instance_id, build_id, ref, exc_info=True,
        )
    snap_paths = {versions_mod.build_snapshot_dir(workspace, instance_id, int(build_id))}
    stored = row.get("config_snapshot")
    if stored:
        snap_paths.add(Path(stored))
    for snap in snap_paths:
        shutil.rmtree(snap, ignore_errors=True)
    with contextlib.suppress(Exception):
        registry.clear_build_artifacts(int(build_id))


def _clear_container_identity(workspace: Workspace, manifest: InstanceManifest) -> None:
    """BUG-300：旧容器已停后立即清空身份并落盘。

    此后若 build/up 失败，start_instance 会走完整重建，而不是对已删除的旧
    containerId 执行 compose start。
    """
    manifest.container.containerId = None
    manifest.container.imageId = None
    manifest.touch()
    manifest.save(workspace.app_manifest_path(manifest.id))


def _restore_generated_for_txn(workspace: Workspace, instance_id: str) -> None:
    """build 前后失败：把生成物恢复为快照（旧版本仍在服务，不改容器）。"""
    try:
        rebuild_txn.restore_generated(workspace, instance_id)
    except OSError as exc:
        log.warning("实例 %s 恢复生成物失败：%s", instance_id, exc)


def _compensate_build_first(
    workspace: Workspace,
    registry: Registry,
    runtime: DockerRuntime,
    manifest: InstanceManifest,
    host_port: int,
    build_id: int,
    reason: Exception,
) -> NoReturn:
    """S2-03：旧容器已停之后新版本失败，补偿回到旧版本（D8：只回滚镜像与生成配置）。

    顺序：down 新容器 → 恢复生成物 → ``:prev`` 打回 ``:latest`` → up → 健康检查。
    成功则实例回到 RUNNING 并记录失败的操作；任一步失败则列出残留项并标记 FAILED。
    """
    iid = manifest.id
    notes: list[str] = []
    try:
        runtime.down(iid)
    except Exception as exc:  # noqa: BLE001 — 清理失败继续尝试拉起旧版本，结果由健康检查定
        notes.append(f"清理新容器失败：{exc}")
    restored = False
    if not rebuild_txn.restore_generated(workspace, iid):
        notes.append("缺少生成物快照，无法恢复 compose")
    else:
        try:
            rebuild_txn.restore_previous_image_tag(manifest)
            runtime.up(iid)
            restored = _wait_for_http(host_port)
            if not restored:
                notes.append("旧版本拉起后健康检查未通过")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"拉起旧版本失败：{exc}")

    from local_webpage_access.logging import now_iso

    failure = str(reason)[:500]
    operation = {
        "kind": "rebuild",
        "status": "failed",
        "buildId": build_id,
        "finishedAt": now_iso(),
        "error": failure,
        "source": "host_container",
        "rollback": "restored" if restored else "failed",
        "rollbackNotes": notes,
    }
    if restored:
        manifest.container.containerId = _safe(lambda: runtime.container_id(iid))
        manifest.container.imageId = _safe(lambda: runtime.image_id(iid))
        manifest.container.hostPort = host_port
        manifest.status = Status.RUNNING
        manifest.lastError = None
        manifest.lastOperation = operation
        manifest.touch()
        manifest.save(workspace.app_manifest_path(iid))
        registry.upsert_from_manifest(manifest)
        registry.update_status(iid, Status.RUNNING.value, desired_state=DesiredState.RUNNING.value)
        registry.update_last_operation(iid, operation)
        registry.update_last_error(iid, failure)
        registry.add_event(
            iid,
            "lifecycle_stage",
            f"重建失败，已回滚到旧版本并恢复服务：{failure[:200]}",
        )
        raise HostingError(
            f"实例 {iid} 重建失败，已回滚到旧版本并恢复服务：{reason}",
            instance_id=iid,
        ) from reason

    residual = HostingError(
        f"重建失败且回滚未完成：{reason}；" + "；".join(notes),
        instance_id=iid,
    )
    _mark_failed(
        workspace,
        registry,
        iid,
        manifest,
        residual,
        operation_kind="rebuild",
        operation_source="host_container",
        build_id=build_id,
    )
    # _mark_failed 会覆盖 lastOperation；补写回滚结果，让 UI/CLI 能看到残留项
    manifest.lastOperation = operation
    with contextlib.suppress(Exception):
        manifest.save(workspace.app_manifest_path(iid))
        registry.update_last_operation(iid, operation)
    raise residual from reason


@dataclass
class _ContainerDeploy:
    """``host_container`` 一次部署/重建的共享状态（S3-06）。

    原先是 ``host_container`` 内的一组局部标志；拆成阶段函数后经本对象传递，
    各阶段与异常收尾读写同一份，语义与拆分前逐行一致。
    """

    workspace: Workspace
    config: Config
    registry: Registry
    instance_id: str
    manifest: InstanceManifest
    runtime: DockerRuntime
    build_id: int
    op_started_at: str
    # S2-02 / D4：操作态记录——有旧容器为「重建」，否则为「首次部署」
    operation_kind: str
    # S2-03：已在跑的实例重建走先建后换
    txn: bool
    host_port: int = 0
    fresh_port: bool = False
    # 旧容器已停、身份已清空：此后失败须补偿回旧版本
    old_stopped: bool = False
    # S2-01/#54：停机失败中止时不做 failed 标记（旧容器仍在服务）
    stop_aborted: bool = False
    # 评审-组2：up 后写失败须回滚停容器
    up_completed: bool = False
    # BUG-834：观测/验证的设计内失败路径已自行完成补偿/收尾并抛出——
    # 外层统一收尾据此跳过，避免二次补偿（down 旧版本、重复恢复快照）。
    outcome_settled: bool = False


def host_container(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
) -> InstanceManifest:
    """Docker Compose 容器实例托管流程（WBS-15 / WBS-16）。

    流程（首次部署，无旧容器）：
    1. 前置条件检查（Docker 可用）；
    2. 生成 Dockerfile（WBS-15.03 / 16.10）；
    3. 分配/复用 host 端口（WBS-15.02 / 16.06）；
    4. 生成 Compose + .env（WBS-15.04 / 16.08/09/11）；
    5. ``build`` + ``up``（WBS-15.05/06 / 16.12）；
    6. HTTP 健康检查（WBS-15.07 / 16.13）；
    7. 观测 containerId/imageId；
    8. 更新 manifest + registry（WBS-15.08/09 / 16.14）。

    已在跑的实例重建（S2-03，issue #51）改走「先建后换」（manifest
    ``rebuildStrategy: stop-first`` 时保留上面的旧顺序）：快照生成物到
    ``docker/.prev/``、旧镜像打 ``:prev`` 标签 → build（旧容器持续服务）
    → 数据救援 → 停机 → 清身份 → up → 验证；停机后失败按
    :func:`_compensate_build_first` 补偿回旧版本。build 失败时旧容器从未被
    动，恢复快照即可。失败时标记 failed 并写诊断上下文（WBS-15.10 / 16.15）。

    S3-06：主流程按阶段拆为 ``_begin_container_deploy`` →
    ``_prepare_previous_container`` → ``_render_container_artifacts`` →
    ``_build_container_image`` → ``_swap_in_new_container``（失败走
    ``_handle_container_deploy_cancelled`` / ``_handle_container_deploy_failed``）→
    ``_observe_new_container`` → ``_commit_verifying_state`` →
    ``_verify_container_deployment`` → ``_finalize_container_deploy``；
    共享状态见 :class:`_ContainerDeploy`，行为与拆分前一致。
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

    ctx = _begin_container_deploy(workspace, config, registry, instance_id, manifest, runtime)

    try:
        _record_stage(workspace, registry, instance_id, "host_start")
        # issue #20：非 root 身份预检必须先于任何状态改变（数据救出、down、
        # 生成器覆写）——runAsNonRoot=True 而 data/ 属主为 root 时直接拒绝，
        # 提示先修正属主；否则停机后才发现，留下无法恢复的已停现场。
        from local_webpage_access.container_identity import ensure_non_root_identity_ready

        ensure_non_root_identity_ready(manifest, workspace)

        _prepare_previous_container(ctx)
        _render_container_artifacts(ctx)
        _build_container_image(ctx)
        _swap_in_new_container(ctx)
        # BUG-834 / S2-03：up 之后的观测、VERIFYING 落盘、验证与最终提交同样
        # 在事务范围内——任何一步失败都进入统一收尾/补偿（先建后换则回旧
        # 版本），不再让异常裸抛把生成物/容器留在新版本半提交状态。观测与
        # 验证的设计内失败路径已自行收尾（outcome_settled），跳过二次补偿。
        _observe_new_container(ctx)
        _commit_verifying_state(ctx)
        verification = _verify_container_deployment(ctx)
        return _finalize_container_deploy(ctx, verification)
    except BuildCancelled as exc:
        _handle_container_deploy_cancelled(ctx, exc)
        raise
    except Exception as exc:
        if not ctx.outcome_settled:
            _handle_container_deploy_failed(ctx, exc)
        raise


def _begin_container_deploy(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
    manifest: InstanceManifest,
    runtime: DockerRuntime,
) -> _ContainerDeploy:
    """登记 build 行与操作态，返回各阶段共享的 :class:`_ContainerDeploy`。"""
    assert manifest.container is not None  # host_container 入口已校验
    build_log = workspace.app_logs(instance_id) / "build.log"
    build_id = registry.add_build(instance_id, status="running", log_path=str(build_log))
    op_started_at = now_iso()
    operation_kind = "rebuild" if manifest.container.containerId is not None else "deploy"
    # S2-03：已在跑的实例重建走先建后换，旧容器服务期间构建；实例 status 不改为 BUILDING，
    # 构建进度只记 lastOperation（D4：实例状态反映实际服务，操作另记）。
    txn = _build_first_applies(runtime, manifest, operation_kind)
    if txn:
        registry.update_last_operation(
            instance_id,
            {
                "kind": operation_kind,
                "status": "building",
                "buildId": build_id,
                "startedAt": op_started_at,
                "source": "host_container",
            },
        )
    else:
        registry.update_status(instance_id, Status.BUILDING.value)
    return _ContainerDeploy(
        workspace=workspace,
        config=config,
        registry=registry,
        instance_id=instance_id,
        manifest=manifest,
        runtime=runtime,
        build_id=build_id,
        op_started_at=op_started_at,
        operation_kind=operation_kind,
        txn=txn,
    )


def _prepare_previous_container(ctx: _ContainerDeploy) -> None:
    """处理旧容器：先建后换只做快照与留档；旧顺序则救数据 → 停机 → 清身份。"""
    if ctx.txn:
        # S2-03：先建后换——旧镜像保留为 :prev、生成物快照在前，失败即中止，
        # 旧容器全程不动。
        rebuild_txn.prepare_build_first(ctx.workspace, ctx.runtime, ctx.manifest)
        return
    # BUG-205：重建 down 前先把容器内数据救出到宿主 data/，避免旧库随容器删除丢失
    _rescue_container_data_before_rebuild(
        ctx.workspace, ctx.manifest, ctx.instance_id, ctx.runtime
    )

    # S2-01/#54：先停机、成功后才动身份——停机失败立即中止且 manifest 身份
    # 保持完整，旧容器继续服务；「本来就没在跑」视为成功。
    try:
        stop_or_abort(ctx.runtime, ctx.instance_id, purpose="重建")
    except HostingError:
        ctx.stop_aborted = True
        raise
    _clear_container_identity(ctx.workspace, ctx.manifest)


def _render_container_artifacts(ctx: _ContainerDeploy) -> None:
    """生成 Dockerfile、分配/复用端口、生成 Compose 与 .env。"""
    # 3. 生成 Dockerfile（BUG-200：注入 config.buildMirrors，避免手改被覆盖）
    generate_dockerfile(ctx.manifest, ctx.workspace, config=ctx.config)
    _record_stage(ctx.workspace, ctx.registry, ctx.instance_id, "dockerfile_ready")

    # 4. 分配/复用端口（先建后换下旧容器仍占用旧端口，须按登记复用以保持
    # lanUrl 稳定，BUG-790）
    ctx.host_port, ctx.fresh_port = _ensure_container_port(
        ctx.config, ctx.registry, ctx.instance_id, allow_listening_reuse=ctx.txn
    )

    # 5. 生成 Compose + .env（含 SQLite DATABASE_URL / RUNTIME_ROOT 与 data/ 挂载）
    generate_compose(ctx.manifest, ctx.workspace, host_port=ctx.host_port, config=ctx.config)
    generate_env(ctx.manifest, ctx.workspace, host_port=ctx.host_port)
    _record_stage(ctx.workspace, ctx.registry, ctx.instance_id, "compose_ready")


def _build_container_image(ctx: _ContainerDeploy) -> None:
    """build 镜像（先建后换时旧容器仍在服务）并为成功构建留档。"""
    _record_stage(ctx.workspace, ctx.registry, ctx.instance_id, "compose_build_start")
    try:
        ctx.runtime.build(ctx.instance_id, build_id=ctx.build_id)
    except DockerError as exc:
        if ctx.txn:
            # S2-03：先建后换下 build 失败——旧容器全程未动，文案明确
            # 「构建失败，旧版本仍在服务」，避免用户误以为服务已被破坏
            raise DockerError(
                f"构建失败，旧版本仍在服务：{exc}",
                instance_id=ctx.instance_id,
            ) from exc
        raise
    _record_stage(ctx.workspace, ctx.registry, ctx.instance_id, "compose_build_done")
    # 可回滚资格延后到部署验证通过（_finalize_container_deploy）。此处只表示
    # 镜像构建成功；启动或必选探针失败的版本不得进入选版与 GC。


def _swap_in_new_container(ctx: _ContainerDeploy) -> None:
    """（先建后换）停旧容器并清身份，然后 ``up`` 新容器。"""
    if ctx.txn:
        # 新镜像已就绪，才动旧容器：救数据（需旧容器在跑）→ 停机 → 清身份
        _rescue_container_data_before_rebuild(
            ctx.workspace, ctx.manifest, ctx.instance_id, ctx.runtime
        )
        try:
            stop_or_abort(ctx.runtime, ctx.instance_id, purpose="重建")
        except HostingError:
            ctx.stop_aborted = True
            raise
        # S2 审计 P1-1（S2-03）：停机成功即记录事务阶段——随后的身份清空/写盘
        # 失败同样属于「旧容器已停」，必须走补偿，不能按「旧版本仍在服务」收尾
        # （否则 registry 回 running 而服务实际已下线，且无 up 恢复）。
        ctx.old_stopped = True
        _clear_container_identity(ctx.workspace, ctx.manifest)
    _record_stage(ctx.workspace, ctx.registry, ctx.instance_id, "compose_up_start")
    ctx.runtime.up(ctx.instance_id)
    ctx.up_completed = True  # 评审-组2：up 后写失败须回滚停容器
    _record_stage(ctx.workspace, ctx.registry, ctx.instance_id, "compose_up_done")


def _handle_container_deploy_cancelled(ctx: _ContainerDeploy, exc: BuildCancelled) -> None:
    """构建被取消的收尾（调用方随后 ``raise``）。"""
    workspace, registry, instance_id = ctx.workspace, ctx.registry, ctx.instance_id
    manifest = ctx.manifest
    _revoke_unverified_build_archive(workspace, registry, instance_id, ctx.build_id)
    if ctx.txn and not ctx.old_stopped:
        _restore_generated_for_txn(workspace, instance_id)
    if ctx.fresh_port:
        try:
            PortAllocator(ctx.config, registry).release_instance(instance_id)
        except Exception:  # noqa: BLE001
            log.warning("取消回滚释放实例 %s 端口失败", instance_id)
    try:
        latest = registry.list_builds(instance_id, limit=1)
        if latest and latest[0]["id"] == ctx.build_id and latest[0]["status"] == "running":
            registry.finish_build(ctx.build_id, status="cancelled", error_summary=str(exc)[:500])
    except Exception:  # noqa: BLE001
        log.exception("取消 finish build 失败")
    # S2-02 / D4：取消是操作态——不改写实例 status（保留操作前观测值），
    # 结果记 lastOperation 与 lastError。
    registry.update_last_operation(
        instance_id,
        {
            "kind": ctx.operation_kind,
            "status": "cancelled",
            "buildId": ctx.build_id,
            "startedAt": ctx.op_started_at,
            "finishedAt": now_iso(),
            "error": str(exc)[:500],
            "source": "host_container",
        },
    )
    registry.update_last_error(instance_id, str(exc)[:500])
    manifest.lastError = str(exc)[:500]
    manifest.touch()
    with contextlib.suppress(Exception):
        manifest.save(workspace.app_manifest_path(instance_id))


def _handle_container_deploy_failed(ctx: _ContainerDeploy, exc: Exception) -> None:
    """build/up 阶段失败的收尾（调用方随后 ``raise``；补偿路径自身会抛出）。"""
    workspace, registry, instance_id = ctx.workspace, ctx.registry, ctx.instance_id
    manifest, runtime = ctx.manifest, ctx.runtime
    _revoke_unverified_build_archive(workspace, registry, instance_id, ctx.build_id)
    if ctx.txn and ctx.old_stopped:
        # S2-03：旧容器已停，新版本失败——补偿回到旧版本（总会抛出）
        _compensate_build_first(
            workspace, registry, runtime, manifest, ctx.host_port, ctx.build_id, exc
        )
    # S2-01/#54：停机失败即中止——旧容器仍在服务，不得标 FAILED、不得
    # 释放端口；registry 回到本次操作前的观测状态，manifest 不落盘任何变更。
    # S2-03：先建后换下 build 失败同样属于「旧版本仍在服务」，走同一收尾。
    if ctx.stop_aborted or (ctx.txn and not ctx.old_stopped):
        if ctx.txn:
            _restore_generated_for_txn(workspace, instance_id)
        # 停机失败即中止：旧容器仍在服务，不得标 FAILED、不得释放端口；
        # build 行如实收尾，registry 状态回到本次操作前的观测值。
        try:
            latest = registry.list_builds(instance_id, limit=1)
            if latest and latest[0]["id"] == ctx.build_id and latest[0]["status"] == "running":
                registry.finish_build(ctx.build_id, status="failed", error_summary=str(exc)[:500])
        except Exception:  # noqa: BLE001
            log.exception("停机中止收尾 build 行失败")
        registry.update_status(instance_id, manifest.status.value)
        # S2-02 / D4：操作失败补记操作态（registry-only；manifest 不落盘，
        # 与上面「manifest 不落盘任何变更」的 S2-01 约束一致）。
        with contextlib.suppress(Exception):
            registry.update_last_operation(
                instance_id,
                {
                    "kind": ctx.operation_kind,
                    "status": "failed",
                    "buildId": ctx.build_id,
                    "startedAt": ctx.op_started_at,
                    "finishedAt": now_iso(),
                    "error": str(exc)[:500],
                    "source": "host_container",
                },
            )
            registry.update_last_error(instance_id, str(exc)[:500])
        return
    # 端口回滚：仅释放本轮新分配的端口（与 _enable_static / BUG-182 对称）。
    # 复用旧端口是上一轮成功部署的登记，失败时清掉会破坏 lanUrl 稳定性。
    if ctx.fresh_port:
        try:
            PortAllocator(ctx.config, registry).release_instance(instance_id)
        except Exception:  # noqa: BLE001
            log.warning("失败回滚释放实例 %s 端口失败", instance_id)
    # DockerRuntime.build 成功/失败都会 finish 该 build 行；
    # 这里只兜底"build 尚未执行就被打断"的情况（如生成文件/分配端口失败），
    # 此时 build 行仍为 running，需要标记 failed。避免与 build() 双重 finish。
    try:
        latest = registry.list_builds(instance_id, limit=1)
        if latest and latest[0]["id"] == ctx.build_id and latest[0]["status"] == "running":
            registry.finish_build(ctx.build_id, status="failed", error_summary=str(exc)[:500])
    except Exception:  # noqa: BLE001
        log.exception("兜底 finish build 失败")
    # 评审-组2：up 成功后的写盘/DB 失败会留孤儿运行容器（实例标 FAILED 但
    # 容器在跑）；best-effort down，与 _liveness_failed_rollback 对称。
    if ctx.up_completed:
        with contextlib.suppress(Exception):
            runtime.down(instance_id)
            log.info("实例 %s 部署后置写失败，已回滚停掉容器", instance_id)
    _mark_failed(
        workspace,
        registry,
        instance_id,
        manifest,
        exc,
        operation_kind=ctx.operation_kind,
        operation_source="host_container",
        build_id=ctx.build_id,
    )


def _observe_new_container(ctx: _ContainerDeploy) -> None:
    """7. 观测 containerId / imageId 并写回 manifest（观测不到则补偿/失败）。"""
    workspace, registry, instance_id = ctx.workspace, ctx.registry, ctx.instance_id
    manifest, runtime = ctx.manifest, ctx.runtime
    assert manifest.container is not None  # host_container 入口已校验
    # 失败不阻塞，仅记录 None
    container_id = _safe(lambda: runtime.container_id(instance_id))
    image_id = _safe(lambda: runtime.image_id(instance_id))
    if container_id is None:
        # BUG-344 与 BUG-300 联合约束：旧 ID 已因 down 失效，不能恢复；新 ID
        # 又未观测到时也不能假报 running。清理本轮容器并留在 failed，使下次
        # start 走完整重建，而不是对陈旧身份 compose start。
        # BUG-834：以下收尾（补偿/清容器/释放端口/_mark_failed）在本分支内
        # 完成——外层统一收尾据 outcome_settled 跳过，避免二次补偿。
        ctx.outcome_settled = True
        observation_error = HostingError(
            f"实例 {instance_id} 启动后未能观测到新 containerId",
            instance_id=instance_id,
        )
        _revoke_unverified_build_archive(workspace, registry, instance_id, ctx.build_id)
        if ctx.txn:
            # S2-03：旧容器已停，无法确认新版本身份——补偿回到旧版本（总会抛出）
            _compensate_build_first(
                workspace,
                registry,
                runtime,
                manifest,
                ctx.host_port,
                ctx.build_id,
                observation_error,
            )
        with contextlib.suppress(Exception):
            runtime.down(instance_id)
        if ctx.fresh_port:
            with contextlib.suppress(Exception):
                PortAllocator(ctx.config, registry).release_instance(instance_id)
        _mark_failed(
            workspace,
            registry,
            instance_id,
            manifest,
            observation_error,
            operation_kind=ctx.operation_kind,
            operation_source="host_container",
            build_id=ctx.build_id,
        )
        raise observation_error
    manifest.container.containerId = container_id
    manifest.container.imageId = image_id
    manifest.container.hostPort = ctx.host_port


def _commit_verifying_state(ctx: _ContainerDeploy) -> None:
    """8. 更新 manifest + registry 到 VERIFYING（验证结论由下一阶段决定）。"""
    workspace, registry, instance_id = ctx.workspace, ctx.registry, ctx.instance_id
    manifest = ctx.manifest
    assert manifest.container is not None  # host_container 入口已校验
    # 先 upsert，再 record_health_check，
    # 否则 upsert_from_manifest 会用 manifest 的 lastHealthCheckAt=None 覆盖 DB 时间戳
    # BUG-084：写回 network 时保留容器已配置的路径别名，否则别名入口对状态/API 不可见。
    entry = build_network_entry(
        ctx.config,
        ctx.host_port,
        internal_port=manifest.container.internalPort,
        path_alias=_container_path_alias(manifest),
    )
    manifest.network = NetworkConfig(**entry)

    # Gate-C C.04：状态机 VERIFYING → RUNNING/DEGRADED/FAILED。
    # build/up 成功后不立即写 RUNNING，先进入 VERIFYING 评估成功谓词。
    manifest.status = Status.VERIFYING
    manifest.desiredState = DesiredState.RUNNING
    manifest.lastError = None
    # S2-02 / D4：build+up 成功——操作态记 succeeded（验证结论另由
    # verificationSummary 承载，不混进实例 status）。
    manifest.lastOperation = {
        "kind": ctx.operation_kind,
        "status": "succeeded",
        "buildId": ctx.build_id,
        "startedAt": ctx.op_started_at,
        "finishedAt": now_iso(),
        "source": "host_container",
    }
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


def _verify_container_deployment(ctx: _ContainerDeploy) -> dict[str, Any]:
    """9. Gate-C C.04/C.05：实证校验——评估成功谓词。

    必选探针失败 → FAILED（不写 RUNNING；先建后换则补偿回旧版本）；
    可选探针失败 → DEGRADED。首页 200 不代替 API/DB 验证（§6.5）。
    返回含副作用记录的 verification 结论。
    """
    workspace, registry, instance_id = ctx.workspace, ctx.registry, ctx.instance_id
    manifest, host_port = ctx.manifest, ctx.host_port
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
        # BUG-834：本分支内自行完成补偿（先建后换）或存活失败回滚后抛出，
        # 外层统一收尾据 outcome_settled 跳过，避免二次补偿。
        ctx.outcome_settled = True
        _revoke_unverified_build_archive(workspace, registry, instance_id, ctx.build_id)
        if ctx.txn:
            # S2-03：新版本必选探针未通过——旧容器已停，补偿回到旧版本（总会抛出）
            _compensate_build_first(
                workspace,
                registry,
                ctx.runtime,
                manifest,
                host_port,
                ctx.build_id,
                HostingError(
                    f"实例 {instance_id} 必选探针未通过（host_port={host_port}）："
                    f"{verification.get('error', 'liveness timeout')}",
                    instance_id=instance_id,
                ),
            )
        # 必选探针失败 → 回滚到 FAILED
        _liveness_failed_rollback(
            workspace,
            ctx.config,
            registry,
            instance_id,
            manifest,
            host_port,
            ctx.fresh_port,
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
    return verification


def _finalize_container_deploy(
    ctx: _ContainerDeploy, verification: dict[str, Any]
) -> InstanceManifest:
    """验证通过后的落盘：最终状态、verificationSummary、指纹、事件与 GC。"""
    workspace, registry, instance_id = ctx.workspace, ctx.registry, ctx.instance_id
    manifest, host_port = ctx.manifest, ctx.host_port
    # 第二批 CHK-252：验证结论（degraded）不再写入进程状态——Status 只表达
    # 进程/部署态；验证警告经 verificationSummary 由 UI 叠加展示
    # （「运行中 · 验证有警告」）。
    if verification["overall_status"] == "degraded":
        final_status = Status.RUNNING
        status_detail = "RUNNING（验证有警告）"
        registry.add_event(
            instance_id,
            "lifecycle_stage",
            "Gate-C 验证有警告（可选探针/能力证据），详见 verificationSummary",
        )
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
        "probeNotes": verification.get("probe_notes", []),
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
    # 验证通过后才赋予可回滚资格，再按保留数 GC。失败版本没有产物列，
    # 不会被当成上一可用版本保护。
    try:
        _finalize_build_artifacts(workspace, registry, instance_id, manifest, ctx.build_id)
    except Exception:  # noqa: BLE001 — 可选留档不得阻断已通过验证的部署
        log.warning(
            "实例 %s 构建 #%s：版本留档阶段异常（本次部署不中断）",
            instance_id, ctx.build_id, exc_info=True,
        )
    image_id = manifest.container.imageId if manifest.container else None
    _gc_after_deploy(workspace, ctx.config, registry, instance_id, image_id)
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
        # S2-01：统一停机语义——down 失败即中止（BUG-423 语义不变，文案统一）。
        # 漂移修复必须移除「已停止但仍存在」的旧容器，故无论是否在跑都执行 down。
        stop_or_abort(runtime, instance_id, purpose="挂载漂移修复", only_if_running=False)
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
        # 第二批 CHK-252：验证警告不再降级进程状态，保持 RUNNING。
        manifest.status = Status.RUNNING
        status_detail = "RUNNING（验证有警告）"
        registry.add_event(
            instance_id,
            "lifecycle_stage",
            f"Gate-C 轻量 start 验证有警告（host_port={host_port}），详见 verificationSummary",
        )
    else:
        manifest.status = Status.FAILED
        manifest.lastError = verification.get("error", "必选探针未通过")[:1800]
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
        "probeNotes": verification.get("probe_notes", []),
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


def recreate_container_runtime(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
) -> InstanceManifest:
    """CHK-252：仅 compose/.env 变化时 force-recreate，不 rebuild 镜像。"""
    manifest = _load_manifest(workspace, instance_id)
    if manifest.runtime.value != "docker-compose" or manifest.container is None:
        raise HostingError(
            f"实例 {instance_id} 不是容器实例",
            instance_id=instance_id,
            runtime=manifest.runtime.value,
        )
    DockerRuntime.ensure_available()
    runtime = DockerRuntime(workspace, registry)
    if not _can_recreate_container_without_rebuild(workspace, runtime, instance_id):
        log.info("实例 %s 无法 runtime recreate，回退完整重建", instance_id)
        return host_container(workspace, config, registry, instance_id)

    # S2-01/#54：先停机、成功后才动身份——停机失败即中止且 manifest 身份保持完整。
    stop_or_abort(runtime, instance_id, purpose="runtime recreate", down=False)
    manifest.container.containerId = None
    manifest.container.imageId = None
    manifest.touch()
    manifest.save(workspace.app_manifest_path(instance_id))
    try:
        runtime.up(instance_id, force_recreate=True)
    except Exception as exc:
        # S2-04：先恢复快照中的旧 compose 并拉起旧容器；无快照时收敛为 failed（BUG-589）。
        _recreate_restore_previous(workspace, registry, runtime, manifest, exc)
    action = "recreate"

    host_port = manifest.container.hostPort
    fresh_port = False
    if not host_port:
        host_port, fresh_port = _ensure_container_port(config, registry, instance_id)
        manifest.container.hostPort = host_port

    observed_cid = _safe(lambda: runtime.container_id(instance_id))
    observed_iid = _safe(lambda: runtime.image_id(instance_id))
    if observed_cid:
        manifest.container.containerId = observed_cid
    if observed_iid:
        manifest.container.imageId = observed_iid

    verification = _evaluate_container_verification(
        host_port, manifest, workspace, registry, instance_id
    )
    overall = verification.get("overall_status", "failed")
    if overall == "failed":
        error = verification.get("error") or "runtime recreate 后验证失败"
        # BUG-589：与 _liveness_failed_rollback 对称——清理新容器、释放本轮
        # 新分配端口、写 FAILED 并记录诊断。
        if fresh_port:
            with contextlib.suppress(Exception):
                PortAllocator(config, registry).release_instance(instance_id)
        failure = HostingError(error, instance_id=instance_id)
        _recreate_restore_previous(workspace, registry, runtime, manifest, failure)

    # 第二批 CHK-252：验证警告不再降级进程状态（failed 已在上面 raise）。
    manifest.status = Status.RUNNING
    manifest.desiredState = DesiredState.RUNNING
    manifest.lastError = None
    from local_webpage_access.lifecycle import _compute_deployment_fingerprints

    manifest.deploymentFingerprints = _compute_deployment_fingerprints(workspace, manifest)
    manifest.verificationSummary = {
        "overallStatus": overall,
        "livenessPassed": verification.get("liveness_passed", False),
        "mandatoryAllPassed": verification.get("mandatory_all_passed", False),
        "optionalWarnings": verification.get("optional_warnings", []),
        "probeNotes": verification.get("probe_notes", []),
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
        f"容器实例 runtime recreate（{action}，host_port={host_port}）",
    )
    log.info("容器实例 %s runtime recreate 完成，端口 %d", instance_id, host_port)
    return manifest


def _recreate_restore_previous(
    workspace: Workspace,
    registry: Registry,
    runtime: DockerRuntime,
    manifest: InstanceManifest,
    reason: Exception,
) -> NoReturn:
    """S2-04：runtime recreate 失败后，恢复快照中的旧 compose 并拉起旧容器（镜像未变，无需 build）。

    恢复成功 → RUNNING 并记录失败的操作；失败 → 退回 :func:`_recreate_failed_rollback`（FAILED）。
    """
    from local_webpage_access.logging import now_iso

    iid = manifest.id
    host_port = manifest.container.hostPort if manifest.container else None
    notes: list[str] = []
    restored = False
    try:
        runtime.down(iid)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"清理新容器失败：{exc}")
    if not rebuild_txn.restore_generated(workspace, iid):
        notes.append("缺少生成物快照，无法恢复旧 compose")
    elif host_port:
        try:
            runtime.up(iid)
            restored = _wait_for_http(host_port)
            if not restored:
                notes.append("旧版本拉起后健康检查未通过")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"拉起旧版本失败：{exc}")
    else:
        notes.append("无 hostPort，无法验证旧版本")

    failure = str(reason)[:500]
    if not restored:
        _recreate_failed_rollback(
            workspace,
            registry,
            iid,
            manifest,
            runtime,
            HostingError(f"{failure}；回滚未完成：{'；'.join(notes)}", instance_id=iid),
        )
        raise reason

    manifest.container.containerId = _safe(lambda: runtime.container_id(iid))
    manifest.container.imageId = _safe(lambda: runtime.image_id(iid))
    manifest.status = Status.RUNNING
    manifest.desiredState = DesiredState.RUNNING
    manifest.lastError = None
    manifest.lastOperation = {
        "kind": "recreate",
        "status": "failed",
        "finishedAt": now_iso(),
        "error": failure,
        "source": "recreate_container_runtime",
        "rollback": "restored",
        "rollbackNotes": notes,
    }
    manifest.touch()
    manifest.save(workspace.app_manifest_path(iid))
    registry.upsert_from_manifest(manifest)
    registry.update_status(iid, Status.RUNNING.value, desired_state=DesiredState.RUNNING.value)
    registry.update_last_operation(iid, manifest.lastOperation)
    registry.update_last_error(iid, failure)
    registry.add_event(
        iid,
        "lifecycle_stage",
        f"runtime recreate 失败，已恢复旧 compose 并拉起旧容器：{failure[:200]}",
    )
    raise HostingError(
        f"实例 {iid} runtime recreate 失败，已回滚到旧配置并恢复服务：{reason}",
        instance_id=iid,
    ) from reason


def _recreate_failed_rollback(
    workspace: Workspace,
    registry: Registry,
    instance_id: str,
    manifest: InstanceManifest,
    runtime: DockerRuntime,
    exc: Exception,
) -> None:
    """BUG-589：runtime recreate 失败收敛——清理新容器、写 FAILED 并记录诊断。

    旧容器在 recreate 前已 stop 且身份已清空，不可恢复；如实记录为
    failed（清空容器身份，下次 start 走完整重建），而不是留下
    manifest/registry/实际容器三方分叉。与 :func:`_liveness_failed_rollback`
    对称。CHK-326：先保存 inspect/日志再 down。
    """
    from local_webpage_access.build_diagnostics import format_probe_failure

    extra = _container_probe_context(workspace, registry, instance_id)
    diag = format_probe_failure(str(exc), stage="runtime_recreate", **extra)
    with contextlib.suppress(Exception):
        runtime.down(instance_id)
    if manifest.container is not None:
        manifest.container.containerId = None
        manifest.container.imageId = None
    _mark_failed(workspace, registry, instance_id, manifest, Exception(diag))
    with contextlib.suppress(Exception):
        registry.add_event(
            instance_id,
            "lifecycle_stage",
            f"runtime recreate 失败已回滚（新容器已清理）：{diag[:200]}",
        )


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
    *,
    allow_listening_reuse: bool = False,
) -> tuple[int, bool]:
    """容器端口分配：优先复用已登记端口，否则新分配。

    复用保证重建后 lanUrl 稳定；端口被外部占用时回退到新分配。复用登记用
    :meth:`Registry.allocate_port` 的并发安全语义：若旧端口已被其他实例抢走
    （BUG-017），返回 False，回退到全新分配。

    ``allow_listening_reuse=True``（S2-03 先建后换）：分配发生在 build 之前，
    旧容器仍占用旧端口是预期形态——登记仍归本实例时直接复用，不能因为
    「正在监听」就换端口（否则重建后 lanUrl 漂移，BUG-790）。

    返回 ``(port, fresh)``：``fresh=False`` 表示复用了上一轮成功部署的登记，
    调用方在本次 build/up 失败时**不得**释放它（与 :func:`_ensure_static_port`
    / BUG-182 对称）。
    """
    allocator = PortAllocator(config, registry)
    row = registry.get_container(instance_id)
    existing = row.get("host_port") if row else None
    if existing and (allow_listening_reuse or not is_port_listening(int(existing))):
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


def _listen_process_names(port: int) -> list[str] | None:
    """列出端口上的监听进程名。lsof 不可用或没有结论时返回 None。"""
    if shutil.which("lsof") is None:
        return None
    try:
        proc = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 and not (proc.stdout or "").strip():
        return None
    names: list[str] = []
    for line in (proc.stdout or "").splitlines()[1:]:
        parts = line.split()
        if parts:
            names.append(parts[0])
    return names or None


def _site_config_binds_port(text: str, port: int) -> bool:
    """站点片段是否把 ``port`` 写成监听地址（``:port`` 后是空白或块起始）。"""
    marker = f":{port}"
    start = 0
    while True:
        idx = text.find(marker, start)
        if idx < 0:
            return False
        after = text[idx + len(marker) : idx + len(marker) + 1]
        if after in ("", " ", "\t", "\n", "\r", "{"):
            return True
        start = idx + len(marker)


def _gateway_holds_static_port(
    workspace: Workspace,
    config: Config,
    instance_id: str,
    port: int,
) -> Ownership:
    """本工作区网关是否仍实际持有该静态端口（BUG-737；三态 S2-07/BUG-748）。

    登记为 enabled 不够：Caddy 已停止时，外部进程可以占着原端口。
    Caddy 后端要求 admin 在线、站点片段仍绑该端口，且 lsof 能看清时监听者是 caddy。
    builtin 后端要求本实例 http.server 仍存活且命令行对得上端口与 public 目录。

    三态语义：证据链完整的确证不持有（FOREIGN，如本网关已停、监听者身份不符）
    与确证持有（OURS）；lsof 缺失导致监听者身份不可见时为 UNKNOWN——
    调用方维持既有放行行为，但必须让用户可见（写事件 / doctor WARN）。
    """
    from local_webpage_access.daemon import pid_cmdline_contains
    from local_webpage_access.errors import GatewayError

    gateway = StaticGateway(workspace, config)
    try:
        backend = gateway.detect_backend()
    except GatewayError:
        return Ownership.FOREIGN
    names = _listen_process_names(port)
    if backend == "caddy":
        if not gateway._admin_alive():
            return Ownership.FOREIGN
        site = gateway.site_config_path(instance_id)
        try:
            text = site.read_text(encoding="utf-8") if site.is_file() else ""
        except OSError:
            return Ownership.FOREIGN
        if not _site_config_binds_port(text, port):
            return Ownership.FOREIGN
        if names is None:
            # lsof 缺失：监听者身份不可见，无法确证——维持放行但标记 UNKNOWN。
            return Ownership.UNKNOWN
        if any("caddy" in name.lower() for name in names):
            return Ownership.OURS
        return Ownership.FOREIGN
    pid = gateway._read_pid(instance_id)
    if pid is None or not StaticGateway._pid_alive(pid):
        return Ownership.FOREIGN
    public = str(workspace.app_public(instance_id))
    if not pid_cmdline_contains(pid, "http.server", str(port), public):
        return Ownership.FOREIGN
    if names is None:
        # lsof 缺失：监听者身份不可见，无法确证——维持放行但标记 UNKNOWN。
        return Ownership.UNKNOWN
    if any("python" in name.lower() or "http.server" in name.lower() for name in names):
        return Ownership.OURS
    return Ownership.FOREIGN


def _static_port_is_own_live_site(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
    port: int,
) -> Ownership:
    """监听中的端口是否仍是本实例自己的活跃静态站点（BUG-631 / BUG-737；三态 S2-07）。

    登记仍归本实例、站点仍启用、``host_port`` 就是该端口，并且本网关仍实际
    持有监听。Caddy 已停止而外部进程占用原端口时，不得仅凭登记复用。
    登记层面的不符是确证否定（FOREIGN）；网关持有性判定可能因 lsof 缺失
    落在 UNKNOWN（身份不可见，维持放行但写事件保持可见）。
    """
    row = registry.get_static_site(instance_id)
    if not row or not row.get("enabled"):
        return Ownership.FOREIGN
    host_port = row.get("host_port")
    if host_port is None or int(host_port) != port:
        return Ownership.FOREIGN
    if registry.port_owner(port) != instance_id:
        return Ownership.FOREIGN
    return _gateway_holds_static_port(workspace, config, instance_id, port)


def _ensure_static_port(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
) -> tuple[int, bool]:
    """静态端口分配：优先复用已登记端口，否则新分配。

    与 :func:`_ensure_container_port` 对称（BUG-045）。``stop_instance`` 不再
    释放静态实例的端口登记，因此重启时此处的复用路径会命中：旧端口仍归本实例
    所有、且无活跃监听者（``is_port_listening`` 为 False），:meth:`allocate_port`
    的并发安全语义确认归属后直接复用，保持 lanUrl 稳定。

    BUG-631：rebuild 期间旧站点仍在监听。若该监听者就是本网关仍持有的本实例
    站点，同样复用。Caddy 已停止、或 lsof 看到的不是本网关进程时，按外部占用
    重新分配（BUG-737）。

    返回 ``(port, fresh)``：``fresh=False`` 表示复用了上一轮成功部署的登记，
    调用方在本次启用失败时**不得**释放它（否则破坏 BUG-045 端口保留语义、
    可致跨实例内容混淆，BUG-182）。
    """
    allocator = PortAllocator(config, registry)
    row = registry.get_static_site(instance_id)
    existing = row.get("host_port") if row else None
    if existing:
        port = int(existing)
        listening = is_port_listening(port)
        own_live = _static_port_is_own_live_site(workspace, config, registry, instance_id, port)
        # UNKNOWN（缺少 lsof、监听者身份不可见）维持放行复用，但写事件保持可见（S2-07）
        if (not listening or own_live is not Ownership.FOREIGN) and registry.allocate_port(
            instance_id, port
        ):
            if listening and own_live is Ownership.UNKNOWN:
                try:
                    registry.add_event(
                        instance_id,
                        "port_ownership_unknown",
                        f"端口 {port} 归属未确认（缺少 lsof，监听者身份不可见），按复用放行",
                    )
                except Exception as exc:  # noqa: BLE001 — 事件写失败不阻断复用（S1-07 限流告警另管）
                    log.warning("实例 %s 端口归属 UNKNOWN 事件写入失败：%s", instance_id, exc)
            log.info("复用静态实例 %s 的端口 %d", instance_id, port)
            return port, False
        if listening and own_live is Ownership.FOREIGN:
            log.warning(
                "实例 %s 的旧端口 %d 被占用且不是本实例活跃站点，重新分配",
                instance_id,
                port,
            )
        else:
            log.warning(
                "实例 %s 的旧端口 %d 已被其他实例占用，重新分配",
                instance_id,
                port,
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


def _container_probe_context(
    workspace: Workspace, registry: Registry, instance_id: str
) -> dict[str, Any]:
    """探针失败时采集容器状态、inspect 与日志尾部（issue #35 / CHK-326）。"""
    extra: dict[str, Any] = {
        "build_log": str(workspace.app_dir(instance_id) / "logs" / "build.log"),
    }
    try:
        runtime = DockerRuntime(workspace, registry)
        st = runtime.status(instance_id)
        if st is not None:
            extra["container_state"] = st.state
        ins = runtime.inspect_state(instance_id)
        if ins.get("exit_code") is not None:
            extra["exit_code"] = ins["exit_code"]
        if ins.get("oom_killed") is not None:
            extra["oom_killed"] = bool(ins["oom_killed"])
        if isinstance(ins.get("restart_count"), int):
            extra["restart_count"] = ins["restart_count"]
        extra["logs"] = runtime.logs(instance_id, tail=80)
    except Exception:  # noqa: BLE001
        log.debug("采集容器探针上下文失败", exc_info=True)
    return extra


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
        extra = _container_probe_context(workspace, registry, instance_id)
        from local_webpage_access.build_diagnostics import format_probe_failure

        return {
            "overall_status": "failed",
            "liveness_passed": False,
            "mandatory_all_passed": False,
            "optional_warnings": [],
            "observed_capabilities": [],
            "error": format_probe_failure(
                f"基础存活探针超时（host_port={host_port}，{_CONTAINER_HEALTH_ATTEMPTS} 次未响应）",
                **extra,
            ),
        }

    # 基础存活通过 -> 收集能力
    observed: set[str] = set()
    observed.add("ui")  # 存活即服务可达

    # CHK-192/P1：从 manifest 加载持久化的能力契约（含 requiredProbes），
    # 不再临时推断（原 _infer_capability_contract 丢失 requiredProbes 且无探针执行）。
    contract = effective_capability_contract(manifest)
    required = contract.required_capabilities

    # 第二批 CHK-252：探针三层语义——可达性（_wait_for_http，任意 HTTP 响应即
    # 存活）/ 就绪性（只有 isMandatory ∧ source="declared" 的用户显式探针按
    # 期望状态通过才算就绪，失败 => failed）/ 能力证据（契约探针与能力覆盖只
    # 产生告警或备注，不再构成失败条件）。
    # BUG-499：只有 source in ("declared", "discovered") 的探针通过才可作为
    # API 能力证据；guessed 探针（如通用 /health、api_probe）仅诊断，
    # 不得满足 servesApi（否则偶然 /health 200 假绿、无标准路径 API 假红）。
    mandatory_all_passed = True
    optional_warnings: list[str] = []
    probe_notes: list[str] = []
    successful_business_probe = False
    for spec in contract.requiredProbes:
        # S2-06（#50 后半）：源码扫描合成路径的低置信度探针在 probeNotes 留痕——
        # 挂载前缀可能不完整；由 S1-04 内容校验兜底（返回 HTML 另有提示），
        # 低置信度本身不构成失败条件。
        if spec.source == "discovered" and spec.confidence == "low":
            probe_notes.append(
                f"{spec.path} 由静态路由扫描合成，置信度低（挂载前缀可能不完整）"
            )
        # BUG-769：declared（用户/项目显式声明）的期望状态严格精确匹配；
        # discovered/guessed 自动探针保留 BUG-506 的 200 宽松族兼容。
        passed, code, ctype = _probe_path(
            host_port,
            spec.path,
            expected_status=spec.expectedStatus,
            lenient_status=spec.source in ("discovered", "guessed"),
        )
        # N2（S1-04）/ BUG-772/779：状态门槛与 API 证据分层——状态码达标只说
        # 明 HTTP 就绪；declared/discovered 探针要成为 API 证据还须响应有实质
        # 内容：HTML（SPA 兜底页或路由前缀缺失）与纯重定向指令（3xx，无内容）
        # 都不算证据。证据否决不撤销就绪结果，servesApi 是全应用能力声明，
        # 不给单个探针制造非 HTML 内容契约，mandatory 探针返回 HTML 不判失败。
        api_eligible = spec.source in ("declared", "discovered")
        html_content = passed and api_eligible and _is_html_content_type(ctype)
        redirect_only = passed and api_eligible and code is not None and 300 <= code < 400
        if html_content:
            probe_notes.append(f"{spec.path} 返回 HTML（疑似 SPA 兜底或缺少路由前缀）")
        if redirect_only:
            probe_notes.append(f"{spec.path} 返回重定向（code={code}），跳转目标不计入 API 证据")
        if passed:
            if api_eligible and not html_content and not redirect_only:
                successful_business_probe = True
            elif api_eligible and not (spec.isMandatory and spec.source == "declared"):
                optional_warnings.append(f"可选探针 {spec.path} 未计入 API 证据")
            continue
        if spec.isMandatory and spec.source == "declared":
            # 用户显式声明的就绪门槛：期望状态必须满足，失败 => failed。
            mandatory_all_passed = False
            continue
        if code in (401, 403):
            # 端点存在但受保护：服务在响应，鉴权是应用语义——不算成功、不降级。
            probe_notes.append(f"端点 {spec.path} 存在但受保护（code={code}）")
            continue
        if spec.source == "guessed":
            # BUG-781：guessed 探针仅诊断（BUG-499 语义补全）——失败一律不降级：
            # 404 中性丢弃，其余（5xx/超时/拒连）记入 probe_notes 供排查。
            if code != 404:
                probe_notes.append(f"猜测探针 {spec.path} 未通过（code={code}），仅诊断不影响状态")
            continue
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
    missing_capabilities = sorted(verifiable_required - observed)
    if missing_capabilities:
        # 第二批 CHK-252：能力证据是证据不是门槛——未覆盖只告警，不判失败。
        optional_warnings.append(
            f"能力证据未覆盖：{', '.join(missing_capabilities)}"
            "（能力证据仅作告警；可用 lwa probe 显式声明就绪探针）"
        )

    # 总体判定：只有不可达或用户声明的就绪探针失败才 failed。
    if not liveness_ok or not mandatory_all_passed:
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
        "probe_notes": probe_notes,
        "observed_capabilities": sorted(observed),
        "error": (
            None
            if overall != "failed"
            else (
                "必选探针未通过（用户声明的就绪门槛）"
                if not mandatory_all_passed
                else f"基础存活探针失败（host_port={host_port}）"
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


def _status_matches(
    code: int, expected_status: int | None, *, lenient_family: bool = False
) -> bool:
    """ProbeSpec 状态判定（PLN-052 S1-04 / N2 / BUG-769）。

    用户显式声明（``source="declared"``）的期望状态严格精确匹配：
    ``lwa probe set --expected-status 200`` 声明的是 200 本身，端点返回
    204/302 不得判通过。只有自动探针（``lenient_family=True``，即
    discovered/guessed）保留 BUG-506 的「健康 OK」哨兵兼容：期望 200 时
    接受任意 2xx/3xx（静态扫描无法判断端点返回语义，避免假红）。
    """
    if expected_status is None or (lenient_family and expected_status == 200):
        return 200 <= code < 400
    return code == expected_status


def _is_html_content_type(content_type: str | None) -> bool:
    if not content_type:
        return False
    base = content_type.split(";", 1)[0].strip().lower()
    return base in ("text/html", "application/xhtml+xml")


def _probe_path(
    host_port: int,
    path: str,
    *,
    expected_status: int = 200,
    timeout: float = 2.0,
    lenient_status: bool = False,
) -> tuple[bool, int | None, str | None]:
    """Gate-C C.05：对指定路径执行 HTTP GET 探针。

    CHK-192/P1：区别于 :func:`_http_ok`（固定命中 ``/``），本函数命中
    :class:`ProbeSpec` 声明的路径（如 ``/health``），作为 mandatory 探针门槛。

    BUG-770：禁止自动重定向（:func:`urlopen_no_redirect`），按原端点响应判定；
    用户明确期望重定向状态时按 3xx 原样匹配，最终响应不得冒充原路径证据。
    BUG-771：错误响应同样从 ``HTTPError.headers`` 保留 Content-Type，
    成功与错误响应应用相同内容验证规则；``HTTPError`` 正确关闭。

    返回 ``(passed, status_code, content_type)``：
    - 状态码按 :func:`_status_matches` 判定；``passed`` 不含 Content-Type 判断，
      API 证据的 HTML 判定由调用方完成（N2：SPA 兜底页不得冒充 API）。
    - ``lenient_status=True`` 仅供自动探针（discovered/guessed）保留 BUG-506
      宽松族；用户显式声明的探针严格精确匹配（BUG-769）。
    - 连接失败 -> ``(False, None, None)``
    """
    url = mark_probe_url(f"http://127.0.0.1:{host_port}{path}")
    try:
        with urlopen_no_redirect(url, timeout=timeout) as resp:
            code_int = int(getattr(resp, "status", None) or resp.getcode())
            ctype = resp.headers.get("Content-Type") if resp.headers is not None else None
            return (
                _status_matches(code_int, expected_status, lenient_family=lenient_status),
                code_int,
                ctype,
            )
    except urllib.error.HTTPError as exc:
        code_int = int(exc.code)
        ctype = exc.headers.get("Content-Type") if exc.headers is not None else None
        try:
            return (
                _status_matches(code_int, expected_status, lenient_family=lenient_status),
                code_int,
                ctype,
            )
        finally:
            exc.close()
    except Exception:  # noqa: BLE001
        return (False, None, None)


def _liveness_failed_rollback(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
    manifest: InstanceManifest,
    host_port: int,
    fresh_port: bool,
    error: str,
) -> list[str]:
    """Gate-C C.04：必选探针失败时回滚容器与端口。

    S1-05 / #53：每一项独立尝试，失败项作为残留返回并写 ``rollback_failed`` 事件，
    同时追加到 ``lastError``，不再被 ``suppress`` 静默吞掉。
    """
    extra = _container_probe_context(workspace, registry, instance_id)
    from local_webpage_access.build_diagnostics import format_probe_failure

    saved = format_probe_failure(error, stage="liveness", **extra)
    residuals: list[str] = []

    def _attempt(label: str, fn) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — 回滚为尽力而为，失败须可见
            residuals.append(f"{label}：{exc}")
            log.warning("回滚残留（%s）：%s：%s", instance_id, label, exc)

    def _stop_container() -> None:
        runtime = DockerRuntime(workspace, registry)
        if runtime.is_running(instance_id):
            runtime.down(instance_id)

    _attempt("停止容器", _stop_container)
    if fresh_port:
        _attempt(
            "释放端口",
            lambda: PortAllocator(config=config, registry=registry).release_instance(instance_id),
        )
    if residuals:
        saved = f"{saved}（回滚残留：{'；'.join(residuals)}）"

    manifest.status = Status.FAILED
    manifest.lastError = saved[:1800]
    manifest.touch()
    _attempt("保存 manifest", lambda: manifest.save(workspace.app_manifest_path(instance_id)))
    registry.update_status(instance_id, Status.FAILED.value, last_error=saved[:1800])
    registry.add_event(
        instance_id,
        "lifecycle_stage",
        f"Gate-C 必选探针失败：{saved[:200]}",
    )
    for item in residuals:
        registry.add_event(instance_id, "rollback_failed", item[:300])
    return residuals


def _http_ok(host_port: int, *, timeout: float = 2.0) -> bool:
    """单次 HTTP GET 可达性探测（第二批 CHK-252：三层语义之「可达性」）。

    收到任意 HTTP 状态码（含 401/404/5xx）即视为进程和端口活着——
    就绪与否交由显式声明的探针判定；连接拒绝/超时才是不可达。
    """
    url = mark_probe_url(f"http://127.0.0.1:{host_port}/")
    try:
        resp = urlopen_direct(url, timeout=timeout)
        return 100 <= resp.status < 600
    except urllib.error.HTTPError:
        # 收到了 HTTP 响应（状态码任意）＝ 服务在监听。
        return True
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
    host_port, fresh_port = _ensure_static_port(workspace, config, registry, instance_id)
    allocator = PortAllocator(config, registry)

    # 不在 enable 前 disable：enable 会覆盖站点配置并停掉残留 builtin；
    # 若先 disable 再 enable 失败，会留下「既无旧也无新」的悬空实例。
    backend = gateway.detect_backend()
    # BUG-861：前端构建形态（有 entry.build，即 SPA）注入 try_files history
    # 路由回退，深链接刷新不再 404；纯静态站（无 entry.build）不加，保持
    # 缺失文件 404 语义（回退会把 miss 变成 index.html 200，可能掩盖断链）。
    spa_fallback = bool(manifest.entry.build)
    try:
        gateway.enable(
            instance_id, host_port, public_dir, alias=path_alias, spa_fallback=spa_fallback
        )
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
    优先检查该路径；hint 带子目录且精确路径不存在时，先在子包目录内按
    常规候选名找（BUG-887），再回退到根级常规扫描。
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
        # BUG-887：hint 带子目录（monorepo 子包，如 excalidraw-app/dist）且该
        # 精确路径不存在时，先在子包目录内按常规候选名找（如 excalidraw-app/
        # build）——真实项目会把产物输出到非 dist 目录（vite build.outDir、
        # CRA/Docusaurus 的 build/），直接回退根级扫描会误报未找到产物。
        hint_parent = Path(hint).parent
        if str(hint_parent) not in ("", "."):
            parent_dir = project_dir / hint_parent
            try:
                in_bounds = parent_dir.resolve().is_relative_to(project_dir.resolve())
            except OSError:
                in_bounds = False
            if in_bounds and parent_dir.is_dir():
                for name in _BUILD_OUTPUT_DIRS:
                    sub_candidate = parent_dir / name
                    if sub_candidate.is_dir():
                        try:
                            if any(sub_candidate.iterdir()):
                                return sub_candidate
                        except (PermissionError, OSError):
                            continue
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
                f"命令超时（{timeout}s）：{cmd}；详细日志见 {log_path}",
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
        # BUG-875：错误消息附上 build.log 完整路径，用户可直接定位细节日志
        raise BuildError(
            f"命令失败（exit {proc.returncode}）：{cmd}；详细日志见 {log_path}",
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
    *,
    operation_kind: str = "deploy",
    operation_source: str = "hosting",
    build_id: int | None = None,
) -> None:
    """把实例标记为 failed，写 error summary、操作态与事件。

    S2-02 / D4：``failed`` 此处仍是诚实观测——调用点（host_container /
    build_and_host_frontend 失败分支）在 build 前已完成停机或从未部署，
    实例确实未在服务；操作结果另记 ``lastOperation``，UI/CLI 据此展示
    「最近一次部署/重建失败」，而非把仍在服务的实例误标 FAILED。
    """
    from local_webpage_access.docker_runtime import _error_summary_with_hint
    from local_webpage_access.logging import now_iso

    error_summary = _error_summary_with_hint(str(exc), limit=1200) or str(exc)[:500]
    manifest.status = Status.FAILED
    manifest.lastError = error_summary
    manifest.lastOperation = {
        "kind": operation_kind,
        "status": "failed",
        "buildId": build_id,
        "finishedAt": now_iso(),
        "error": error_summary[:500],
        "source": operation_source,
    }
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
