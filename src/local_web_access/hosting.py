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

import shutil
import subprocess
from pathlib import Path

from local_web_access.config import Config
from local_web_access.errors import BuildError, GatewayError, HostingError
from local_web_access.logging import get_logger, write_instance_log
from local_web_access.models import (
    DesiredState,
    InstanceManifest,
    NetworkConfig,
    StaticConfig,
    Status,
)
from local_web_access.paths import Workspace
from local_web_access.ports import PortAllocator, build_network_entry
from local_web_access.registry import Registry
from local_web_access.static_gateway import StaticGateway

log = get_logger("hosting")

_BUILD_TIMEOUT = 600
_BUILD_OUTPUT_DIRS = ("dist", "build", "out", ".output", ".svelte-kit")
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
    """根据 manifest 的 runtime/form 自动选择静态或前端流程。

    Phase 2 只处理 ``shared-static`` 运行形态；容器形态留给 Phase 3。
    """
    manifest = _load_manifest(workspace, instance_id)
    form = _infer_form(manifest)

    if manifest.runtime.value != "shared-static":
        raise HostingError(
            f"实例 {instance_id} 的 runtime={manifest.runtime.value} 暂不支持（Phase 2 仅支持 shared-static）",
            instance_id=instance_id,
            runtime=manifest.runtime.value,
        )

    if form == "frontend-static":
        return build_and_host_frontend(workspace, config, registry, instance_id)
    return host_static(workspace, config, registry, instance_id)


def stop_instance(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    instance_id: str,
) -> InstanceManifest:
    """停止（禁用）静态实例。"""
    manifest = _load_manifest(workspace, instance_id)
    if manifest.runtime.value == "shared-static":
        gateway = StaticGateway(workspace, config)
        gateway.disable(instance_id)
        # 释放端口
        allocator = PortAllocator(config, registry)
        allocator.release_instance(instance_id)
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
    else:
        raise HostingError(
            f"实例 {instance_id} 的 runtime={manifest.runtime.value} 暂不支持停止"
            f"（Phase 2 仅支持 shared-static）",
            instance_id=instance_id,
            runtime=manifest.runtime.value,
        )
    return manifest


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
    allocator = PortAllocator(config, registry)
    gateway = StaticGateway(workspace, config)
    # 重启用场景：先停掉可能仍在运行的旧静态进程，
    # 否则旧进程会成为孤儿（PID 文件被新进程覆盖）、旧端口继续被占用
    if gateway.is_enabled(instance_id):
        gateway.disable(instance_id)
    # 若已有端口先释放，重新分配
    allocator.release_instance(instance_id)
    host_port = allocator.allocate(instance_id)

    backend = gateway.detect_backend()
    gateway.enable(instance_id, host_port, public_dir)

    manifest.static = StaticConfig(
        root="public",
        gateway=backend,
        hostPort=host_port,
        gatewayConfigPath=str(gateway.site_config_path(instance_id)),
        enabled=True,
    )
    entry = build_network_entry(config, host_port)
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


def run_command(
    cmd: str,
    *,
    cwd: Path,
    log_path: Path,
    timeout: int = _BUILD_TIMEOUT,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    """运行 shell 命令，stdout/stderr 追加写入 log_path。

    命令来自项目识别器的确定性推断，``shell=True`` 可接受。
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"\n$ {cmd}\n")
        fh.flush()
        try:
            result = subprocess.run(
                cmd,
                cwd=str(cwd),
                shell=True,
                stdout=fh,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise BuildError(
                f"命令超时（{timeout}s）：{cmd}",
                command=cmd,
                timeout=timeout,
            ) from exc
    if result.returncode != 0:
        raise BuildError(
            f"命令失败（exit {result.returncode}）：{cmd}",
            command=cmd,
            exit_code=result.returncode,
            log_path=str(log_path),
        )
    return result


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
    "stop_instance",
    "find_index_html",
    "find_build_output",
    "sync_static_to_public",
    "sync_dir",
    "run_command",
]
