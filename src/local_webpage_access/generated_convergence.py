"""生成物收敛（PLN-052 S2-04 / #48 #57-1 N4 / D1）。

start / restart / recover 在计算部署指纹之前，先按 manifest 重新渲染
Dockerfile、compose.yaml 与 .env。这样：

- 指纹比较的是「manifest 当前应有的产物」，而不是盘上可能被手改过的文件；
- 渲染经过 :func:`generate_compose` 内置的安全审计（N4：手改 compose 不再绕过审计）；
- extraVolumes 等运行时字段变化 → runtimeConfigHash 变化 → recreate；
  镜像相关字段变化 → buildConfigHash 变化 → full rebuild（D1）。

手改检测：上次成功部署时记录的 ``configHash`` 是盘上生成物的基线。若重渲染前盘上
内容既不等于基线、也不等于新渲染结果，说明它被手工修改过，本次按 manifest 覆盖并告警。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from local_webpage_access.compose import generate_compose, generate_env
from local_webpage_access.config import Config
from local_webpage_access.dockerfile_templates import generate_dockerfile
from local_webpage_access.logging import get_logger
from local_webpage_access.models import InstanceManifest
from local_webpage_access.paths import Workspace

log = get_logger("generated_convergence")


@dataclass
class ConvergeResult:
    rendered: bool
    manual_overwritten: bool = False
    notes: list[str] = field(default_factory=list)


def _config_hash(workspace: Workspace, manifest: InstanceManifest) -> str:
    # 延迟导入：lifecycle 依赖本模块，避免循环
    from local_webpage_access.lifecycle import _compute_config_fingerprint

    iid = manifest.id
    return _compute_config_fingerprint(
        workspace.app_compose_path(iid),
        workspace.app_dockerfile_path(iid),
        workspace.app_env_path(iid),
    )


def _stored_config_hash(manifest: InstanceManifest) -> str | None:
    fps = getattr(manifest, "deploymentFingerprints", None) or {}
    value = fps.get("configHash") if isinstance(fps, dict) else None
    return str(value) if value else None


def converge_generated_artifacts(
    workspace: Workspace,
    config: Config,
    manifest: InstanceManifest,
) -> ConvergeResult:
    """按 manifest 重渲染生成物并覆盖盘上内容；返回是否发生了手改覆盖。

    无 ``hostPort`` 时无法渲染 compose（端口尚未分配），跳过并返回 ``rendered=False``；
    调用方应按“需要完整重建”处理。渲染失败（例如审计拒绝 manifest 中的配置）原样抛出，
    启动随之失败，不会在不安全的生成物上继续。
    """
    container = manifest.container
    if container is None or container.hostPort is None:
        return ConvergeResult(rendered=False, notes=["无 hostPort，跳过生成物重渲染"])

    before = _config_hash(workspace, manifest)
    stored = _stored_config_hash(manifest)

    generate_dockerfile(manifest, workspace, config=config)
    generate_compose(manifest, workspace, host_port=container.hostPort, config=config)
    generate_env(manifest, workspace, host_port=container.hostPort)

    after = _config_hash(workspace, manifest)
    manual = stored is not None and before != stored and before != after
    if manual:
        log.warning(
            "实例 %s 的生成物被手工修改，已按 manifest 覆盖；持久化修改请使用 manifest 字段"
            "（如 local-web.json 的 container.extraVolumes、buildHooks），再执行 lwa restart",
            manifest.id,
        )
    notes = ["生成物已按 manifest 重渲染"]
    if manual:
        notes.append("检测到手工修改的生成物，已覆盖")
    return ConvergeResult(rendered=True, manual_overwritten=manual, notes=notes)
