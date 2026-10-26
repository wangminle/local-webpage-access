"""DEV-170 / issue #61 阶段二：宿主预构建执行器。

在容器镜像构建之前，把用户显式配置的 ``hostBuild`` / ``preSync`` 命令放到
``apps/<id>/current/``（同步后的源码根）执行一次。适用于「后端服务托管前端
编译产物」的同仓项目：前端源码更新后，先由宿主工具链重新生成后端静态目录，
随后 Docker 构建才能把新产物打进镜像。

边界：
- 命令只来自 manifest（用户通过 ``lwa configure`` / 管理 API 显式写入），
  不从源码或 ZIP 推导；由 :class:`InstanceManifest` 校验拒绝换行/NUL；
- 在显式更新路径，或手动 start/rebuild 消费待处理标记时触发；没有待处理
  标记的普通重建、daemon 自愈与 restart 不会执行宿主命令；
- 复用 :func:`hosting.run_command`，获取同一构建队列的取消、进程树清理、
  build.log 分段与超时语义；
- 在 ``host_container`` 之前执行，失败时旧容器仍在服务（失败保旧）。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from local_webpage_access.build_proxy import (
    config_with_latest_build_proxy,
    merge_into,
    overlay_build_env,
)
from local_webpage_access.build_sources import (
    host_build_timeout,
    inject_pkg_cache_env,
    timeout_hint,
)
from local_webpage_access.config import Config
from local_webpage_access.errors import BuildError
from local_webpage_access.hosting import run_command
from local_webpage_access.instance_settings import effective_build_env
from local_webpage_access.logging import write_instance_log
from local_webpage_access.models import InstanceManifest, Runtime
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry


@dataclass(frozen=True)
class HostBuildResult:
    """一次成功的宿主预构建记录。"""

    command: str
    cwd: Path
    timeout: int
    durationSeconds: float
    logPath: Path


def configured_host_build(manifest: InstanceManifest) -> str | None:
    """返回生效的宿主预构建命令；两个同义字段互斥由模型校验保证。"""
    command = (manifest.hostBuild or manifest.preSync or "").strip()
    return command or None


def run_host_build(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    manifest: InstanceManifest,
) -> HostBuildResult | None:
    """执行配置的宿主预构建；未配置返回 ``None``。

    失败抛 :class:`BuildError`（超时/取消沿用 ``run_command`` 的语义），
    调用方必须在 ``host_container`` 之前调用，以保证旧服务不被提前停止。
    """
    command = configured_host_build(manifest)
    if command is None:
        return None

    runtime = (
        manifest.runtime.value
        if isinstance(manifest.runtime, Runtime)
        else str(manifest.runtime)
    )
    if runtime != Runtime.DOCKER_COMPOSE.value:
        raise BuildError(
            "hostBuild/preSync 仅支持 docker-compose 容器实例",
            instance_id=manifest.id,
        )

    current_dir = workspace.app_current(manifest.id)
    if not current_dir.is_dir():
        raise BuildError(
            f"宿主预构建缺少源码目录 {current_dir}；请先同步/导入源码",
            instance_id=manifest.id,
        )

    config = config_with_latest_build_proxy(workspace, config)
    env = overlay_build_env(
        merge_into({**os.environ}, config),
        effective_build_env(manifest),
    )

    # 与宿主前端构建同口径：按项目锁文件识别包管理器，把缓存指到工作区
    # run/pkg-cache/，避免 rebuild/同步重建 current/ 后从零下载。
    tool = None
    try:
        from local_webpage_access.node_toolchain import inspect_node_toolchain

        tool = inspect_node_toolchain(current_dir)
        env = inject_pkg_cache_env(env, workspace, tool.manager)
    except Exception:  # noqa: BLE001 - 非 Node 项目或探测失败不应阻断自定义命令
        tool = None
    timeout = host_build_timeout(config, monorepo=bool(tool and tool.workspaces))
    log_path = workspace.app_logs(manifest.id) / "build.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if config.buildProxy.proxy_env():
        write_instance_log(
            workspace.apps,
            manifest.id,
            "build",
            f"宿主预构建使用工作区代理 {config.buildProxy.redacted_url()}",
        )
    write_instance_log(
        workspace.apps,
        manifest.id,
        "build",
        f"宿主预构建开始：{command}（cwd={current_dir}，timeout={timeout}s）",
    )
    registry.add_event(manifest.id, "host_build", f"宿主预构建开始：{command}")

    started = time.monotonic()
    try:
        run_command(
            command,
            cwd=current_dir,
            log_path=log_path,
            timeout=timeout,
            env=env,
        )
    except BuildError as exc:
        try:
            registry.add_event(
                manifest.id, "host_build_failed", f"宿主预构建失败：{exc}"
            )
            write_instance_log(
                workspace.apps,
                manifest.id,
                "build",
                f"宿主预构建失败：{exc}",
            )
        except Exception:  # noqa: BLE001
            pass
        # 超时错误已带 timeout_hint；其余命令失败保留 run_command 的
        # exit/log 路径，调用方据此提示旧服务未受影响。
        if "命令超时" in str(exc):
            raise BuildError(
                f"{timeout_hint(timeout)} 命令：{command}；详细日志见 {log_path}",
                command=command,
                timeout=timeout,
                instance_id=manifest.id,
            ) from exc
        raise

    duration = round(time.monotonic() - started, 2)
    summary = f"宿主预构建完成：{command}（{duration}s）"
    try:
        registry.add_event(manifest.id, "host_build", summary)
        write_instance_log(workspace.apps, manifest.id, "build", summary)
    except Exception:  # noqa: BLE001
        pass
    return HostBuildResult(
        command=command,
        cwd=current_dir,
        timeout=timeout,
        durationSeconds=duration,
        logPath=log_path,
    )
