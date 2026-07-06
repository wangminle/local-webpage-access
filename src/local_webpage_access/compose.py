"""Docker Compose 模板与 ``.env`` 生成（WBS-13）。

为容器实例生成 Compose project，作为容器实例的管理单元。输出到
``apps/<id>/docker/compose.yaml`` 与 ``apps/<id>/docker/.env``。

设计要点（对应 V1 设计说明第 13 节）：

1. 构建上下文是实例目录 ``apps/<id>/``（``context: ..``），Dockerfile 在 ``docker/``。
2. 端口映射 ``${HOST_PORT}:${INTERNAL_PORT}`` 由 ``.env`` 插值，避免硬编码。
3. SQLite 项目挂载 ``../data:/app/data`` 并注入 ``DATABASE_URL``。
4. 资源限制使用 Compose legacy 顶层字段 ``mem_limit`` / ``cpus``（单机模式直接生效），
   ``local-web.json`` 的 ``resourceLimits.{memory,cpus}`` 在渲染时映射为这两个字段。
5. ``restart: unless-stopped`` 配合 ``desiredState`` 实现"开机自启但 stop 后不拉起"。
6. 顶层 ``name:`` 固定 Compose project name，避免依赖目录名推断。

compose.yaml 用字符串模板而非 ``yaml.safe_dump`` 渲染，保证 ``${}`` 插值与
``mem_limit``/``cpus`` 字段被 ``docker compose config`` 原样接受（YAML dumper
会对含 ``:`` ``{`` ``}`` 的值加引号，破坏 Compose 变量插值）。
"""

from __future__ import annotations

from pathlib import Path

from local_webpage_access.logging import get_logger
from local_webpage_access.models import InstanceManifest
from local_webpage_access.paths import Workspace

log = get_logger("compose")

_SERVICE_NAME = "app"
_DATA_VOLUME = "../data:/app/data"
_SQLITE_DB_URL = "sqlite:////app/data/app.sqlite"

_COMPOSE_TEMPLATE = """\
# 由 lwa 自动生成，请勿手动编辑。
# 实例：{instance_id}（host_port={host_port}, internal_port={internal_port}）
# 端口/资源/DATABASE_URL 来自同目录 .env。
name: {project_name}
services:
  {service}:
    build:
      context: ..
      dockerfile: docker/Dockerfile
    container_name: lwa-{instance_id}
    ports:
      - "${{HOST_PORT}}:${{INTERNAL_PORT}}"
    env_file:
      - .env
    volumes:
      - {data_volume}
    mem_limit: ${{MEMORY_LIMIT:-{memory}}}
    cpus: "${{CPU_LIMIT:-{cpus}}}"
    restart: unless-stopped
"""


def generate_compose(
    manifest: InstanceManifest,
    workspace: Workspace,
    *,
    host_port: int,
) -> Path:
    """渲染 ``docker/compose.yaml``（WBS-13.01~10）。

    Args:
        manifest: 实例元数据（需有 container 配置）。
        workspace: 工作区。
        host_port: 宿主机端口（写入注释，实际映射走 .env 插值）。

    Returns:
        写入的 compose.yaml 路径。
    """
    container = manifest.container
    if container is None:
        raise ValueError(f"实例 {manifest.id} 缺少 container 配置，无法生成 compose.yaml")
    limits = container.resourceLimits

    content = _COMPOSE_TEMPLATE.format(
        project_name=container.projectName,
        instance_id=manifest.id,
        service=_SERVICE_NAME,
        host_port=host_port,
        internal_port=container.internalPort,
        data_volume=_DATA_VOLUME,
        memory=limits.memory,
        cpus=limits.cpus,
    )
    # WBS-25.03/04/05：自检生成的 compose 是否含 critical 安全问题
    # （模板本身安全；此检查防止模板被改动或 skill 覆盖后引入风险）。
    from local_webpage_access.security import audit_compose, has_critical

    findings = audit_compose(content)
    if has_critical(findings):
        codes = ", ".join(f.code for f in findings if f.level == "critical")
        raise RuntimeError(
            f"生成的 compose.yaml 含 critical 安全问题（{codes}），已拒绝写出"
        )
    for f in findings:
        log.warning("compose 安全审计 [%s] %s", f.code, f.message)

    out_path = workspace.app_compose_path(manifest.id)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    log.info("已生成 compose.yaml：%s", out_path)
    return out_path


def generate_env(
    manifest: InstanceManifest,
    workspace: Workspace,
    *,
    host_port: int,
) -> Path:
    """渲染 ``docker/.env``（WBS-13.08）。

    包含 ``HOST_PORT`` / ``INTERNAL_PORT`` / ``MEMORY_LIMIT`` / ``CPU_LIMIT``；
    SQLite 项目额外注入 ``DATABASE_URL``。
    """
    container = manifest.container
    if container is None:
        raise ValueError(f"实例 {manifest.id} 缺少 container 配置，无法生成 .env")
    limits = container.resourceLimits

    lines = [
        "# 由 lwa 自动生成，请勿手动编辑。",
        f"HOST_PORT={host_port}",
        f"INTERNAL_PORT={container.internalPort}",
        f"MEMORY_LIMIT={limits.memory}",
        f"CPU_LIMIT={limits.cpus}",
    ]
    if _is_sqlite(manifest):
        lines.append(f"DATABASE_URL={_SQLITE_DB_URL}")

    out_path = workspace.app_env_path(manifest.id)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("已生成 .env：%s", out_path)
    return out_path


def service_name() -> str:
    """返回 Compose 服务名（固定为 ``app``）。"""
    return _SERVICE_NAME


def _is_sqlite(manifest: InstanceManifest) -> bool:
    return bool(
        manifest.hasDatabase
        and manifest.database
        and manifest.database.type == "sqlite"
    )


__all__ = ["generate_compose", "generate_env", "service_name"]
