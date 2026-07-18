"""Docker Compose 模板与 ``.env`` 生成（WBS-13）。

为容器实例生成 Compose project，作为容器实例的管理单元。输出到
``apps/<id>/docker/compose.yaml`` 与 ``apps/<id>/docker/.env``。

设计要点（对应 V1 设计说明第 13 节）：

1. 构建上下文是实例目录 ``apps/<id>/``（``context: ..``），Dockerfile 在 ``docker/``。
2. 端口映射 ``${HOST_PORT}:${INTERNAL_PORT}`` 由 ``.env`` 插值，避免硬编码。
3. SQLite 项目挂载 ``../data``：默认 ``/app/data`` + ``DATABASE_URL``；若应用使用
   ``RUNTIME_ROOT`` / ``runtime_paths``，则挂载 ``../data:/app/runtime/data`` 并注入
   ``RUNTIME_ROOT=/app/runtime``（BUG-198）。
4. 资源限制使用 Compose legacy 顶层字段 ``mem_limit`` / ``cpus``（单机模式直接生效），
   ``local-web.json`` 的 ``resourceLimits.{memory,cpus}`` 在渲染时映射为这两个字段。
5. ``restart: unless-stopped`` 配合 ``desiredState`` 实现"开机自启但 stop 后不拉起"。
6. 顶层 ``name:`` 固定 Compose project name，避免依赖目录名推断。

compose.yaml 用字符串模板而非 ``yaml.safe_dump`` 渲染，保证 ``${}`` 插值与
``mem_limit``/``cpus`` 字段被 ``docker compose config`` 原样接受（YAML dumper
会对含 ``:`` ``{`` ``}`` 的值加引号，破坏 Compose 变量插值）。
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path

from local_webpage_access.logging import get_logger
from local_webpage_access.models import InstanceManifest
from local_webpage_access.paths import Workspace

log = get_logger("compose")

_SERVICE_NAME = "app"
_DATA_VOLUME_APP = "../data:/app/data"
_DATA_VOLUME_RUNTIME = "../data:/app/runtime/data"
_SQLITE_DB_URL = "sqlite:////app/data/app.sqlite"
# IMP-015：业务密钥可选注入文件（用户按 docker/.env.example 填写后放入 docker/.env.local）。
# 用 Compose env_file 的对象形式 required:false，缺失时不报错（WBS-20260708 阶段3.2 决策）。
_ENV_LOCAL_BLOCK = "      - path: .env.local\n        required: false\n"

_COMPOSE_TEMPLATE = """\
# 由 lwa 自动生成，请勿手动编辑。
# 实例：{instance_id}（host_port={host_port}, internal_port={internal_port}）
# 端口/资源/DATABASE_URL 来自同目录 .env；业务密钥可放 .env.local（可选，缺失不报错）。
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
{env_local_block}{extra_environment}    volumes:
      - {data_volume}
    mem_limit: ${{MEMORY_LIMIT:-{memory}}}
    cpus: "${{CPU_LIMIT:-{cpus}}}"
    restart: unless-stopped
"""


def uses_runtime_root(source_dir: Path | None, manifest: InstanceManifest) -> bool:
    """BUG-198：是否按 RUNTIME_ROOT / runtime/data 持久化。

    ``source_dir`` 为 ``None`` 时仅依据 manifest.database.dataDir 判断。
    """
    data_dir = (manifest.database.dataDir if manifest.database else None) or ""
    if data_dir.replace("\\", "/").startswith("runtime"):
        return True
    if source_dir is None:
        return False
    return (
        (source_dir / "src" / "app" / "runtime_paths.py").is_file()
        or (source_dir / "app" / "runtime_paths.py").is_file()
    )


def _uses_runtime_root(source_dir: Path, manifest: InstanceManifest) -> bool:
    """内部别名，保持既有调用点。"""
    return uses_runtime_root(source_dir, manifest)


def container_data_paths(source_dir: Path, manifest: InstanceManifest) -> list[str]:
    """容器内数据目录的候选路径（BUG-205 重建前数据迁移用）。

    以"新 compose 将挂载的目标"优先，再兜底另一种历史布局，覆盖 RUNTIME_ROOT 与
    非 RUNTIME_ROOT 两类既有容器——旧实例的库可能写在容器可写层（旧版未挂载
    data/ 或挂载路径不同），重建 down 前需把这些路径的内容救出到宿主 data/。
    """
    if _uses_runtime_root(source_dir, manifest):
        return ["/app/runtime/data", "/app/data"]
    return ["/app/data", "/app/runtime/data"]


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

    source_dir = workspace.app_current(manifest.id)
    env_lines: list[str] = []
    # FastAPI 常见 src/ 布局：不重建镜像时也要能找到 main（与 Dockerfile ENV 对齐）。
    if (source_dir / "src" / "main.py").is_file():
        env_lines.append("      - PYTHONPATH=src")

    data_volume = _DATA_VOLUME_APP
    if _is_sqlite(manifest) and _uses_runtime_root(source_dir, manifest):
        data_volume = _DATA_VOLUME_RUNTIME
        env_lines.append("      - RUNTIME_ROOT=/app/runtime")

    extra_environment = ""
    if env_lines:
        extra_environment = "    environment:\n" + "\n".join(env_lines) + "\n"

    content = _COMPOSE_TEMPLATE.format(
        project_name=container.projectName,
        instance_id=manifest.id,
        service=_SERVICE_NAME,
        host_port=host_port,
        internal_port=container.internalPort,
        data_volume=data_volume,
        memory=limits.memory,
        cpus=limits.cpus,
        env_local_block=_ENV_LOCAL_BLOCK,
        extra_environment=extra_environment,
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
    SQLite 项目额外注入 ``DATABASE_URL``（RUNTIME_ROOT 布局除外，库文件走挂载目录）。
    """
    container = manifest.container
    if container is None:
        raise ValueError(f"实例 {manifest.id} 缺少 container 配置，无法生成 .env")
    limits = container.resourceLimits
    source_dir = workspace.app_current(manifest.id)

    lines = [
        "# 由 lwa 自动生成，请勿手动编辑。",
        f"HOST_PORT={host_port}",
        f"INTERNAL_PORT={container.internalPort}",
        f"MEMORY_LIMIT={limits.memory}",
        f"CPU_LIMIT={limits.cpus}",
    ]
    if _is_sqlite(manifest) and not _uses_runtime_root(source_dir, manifest):
        lines.append(f"DATABASE_URL={_SQLITE_DB_URL}")

    out_path = workspace.app_env_path(manifest.id)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("已生成 .env：%s", out_path)

    # IMP-015（WBS-20260708 阶段3.2）：业务 .env.example 复制为 docker/.env.example。
    # 用户据此填写 docker/.env.local（由 compose env_file 的 required:false 可选注入）。
    # 不覆盖已存在的 .env.example（避免吞掉用户改动）；不自动填密钥。
    import shutil

    source_env_example = workspace.app_current(manifest.id) / ".env.example"
    target_env_example = out_path.parent / ".env.example"
    if source_env_example.is_file() and not target_env_example.exists():
        try:
            shutil.copy2(source_env_example, target_env_example)
            log.info("已复制业务 .env.example → %s", target_env_example)
        except OSError as exc:
            log.warning("复制 .env.example 失败（忽略）：%s", exc)

    # BUG-199：缺 .env.local 时为 JWT_SECRET 等空密钥生成持久值，避免重建后 token 失效。
    # BUG-208：密钥检测须读"当前源" current/.env.example，而非首次导入时缓存的
    # docker/.env.example——后者在项目更新（新增 JWT_SECRET）后不会刷新（上方 copy
    # 仅在缺失时复制），导致检测读到旧缓存、.env.local 漏生成，重建后 token 失效。
    ensure_env_local_secrets(out_path.parent, source_env_example)
    return out_path


def ensure_env_local_secrets(docker_dir: Path, env_example: Path | None = None) -> Path | None:
    """若 ``.env.local`` 不存在且 example 声明了空的 ``JWT_SECRET``，则生成并写入。

    不覆盖已有 ``.env.local``。返回写入路径或 None。
    """
    local_path = docker_dir / ".env.local"
    if local_path.exists():
        return None
    example = env_example if env_example is not None else docker_dir / ".env.example"
    if not example.is_file():
        return None
    try:
        text = example.read_text(encoding="utf-8")
    except OSError:
        return None
    if not re.search(r"(?m)^JWT_SECRET=", text):
        return None
    # 已在 example 里填了非空值则不代填
    m = re.search(r"(?m)^JWT_SECRET=(.*)$", text)
    if m and m.group(1).strip():
        return None
    jwt = secrets.token_hex(32)
    local_path.write_text(
        "# 由 lwa 自动生成（BUG-199）；可按 .env.example 补充其它密钥。\n"
        f"JWT_SECRET={jwt}\n",
        encoding="utf-8",
    )
    try:
        local_path.chmod(0o600)
    except OSError:
        pass
    log.info("已生成业务密钥文件：%s（含 JWT_SECRET）", local_path)
    return local_path


def service_name() -> str:
    """返回 Compose 服务名（固定为 ``app``）。"""
    return _SERVICE_NAME


def _is_sqlite(manifest: InstanceManifest) -> bool:
    return bool(
        manifest.hasDatabase
        and manifest.database
        and manifest.database.type == "sqlite"
    )


__all__ = [
    "generate_compose",
    "generate_env",
    "ensure_env_local_secrets",
    "container_data_paths",
    "service_name",
    "uses_runtime_root",
]
