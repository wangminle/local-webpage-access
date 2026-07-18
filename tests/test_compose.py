"""Docker Compose 与 .env 模板测试（WBS-13）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_webpage_access.compose import generate_compose, generate_env, service_name
from local_webpage_access.models import (
    ContainerConfig,
    DatabaseConfig,
    EntryConfig,
    InstanceManifest,
    Kind,
    ResourceLimits,
    ResourceProfile,
    Runtime,
    ServingMode,
)
from local_webpage_access.paths import Workspace


def _mk_manifest(
    *,
    mid: str = "api",
    kind: Kind = Kind.PYTHON,
    internal_port: int = 8000,
    memory: str = "512m",
    cpus: str = "0.75",
    has_database: bool = False,
    database_type: str | None = None,
) -> InstanceManifest:
    return InstanceManifest(
        id=mid,
        name=mid,
        version="1",
        kind=kind,
        stack=[],
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.SMALL,
        container=ContainerConfig(
            projectName=f"lwa-{mid}",
            internalPort=internal_port,
            composePath="docker/compose.yaml",
            dockerfilePath="docker/Dockerfile",
            resourceLimits=ResourceLimits(memory=memory, cpus=cpus),
        ),
        entry=EntryConfig(install="pip install -r requirements.txt"),
        hasDatabase=has_database,
        database=DatabaseConfig(type=database_type) if has_database and database_type else None,
    )


# ---- compose.yaml 渲染 ------------------------------------------------------


def test_compose_basic_structure(workspace: Workspace) -> None:
    m = _mk_manifest(internal_port=8000)
    path = generate_compose(m, workspace, host_port=18000)
    assert path == workspace.app_compose_path("api")
    content = path.read_text(encoding="utf-8")

    # 顶层 name = projectName
    assert "name: lwa-api" in content
    # 服务名固定 app
    assert "services:" in content
    assert "  app:" in content
    # 构建上下文 = .. dockerfile = docker/Dockerfile
    assert "context: .." in content
    assert "dockerfile: docker/Dockerfile" in content
    # container_name = lwa-<id>
    assert "container_name: lwa-api" in content
    # 端口映射走 .env 插值
    assert '"${HOST_PORT}:${INTERNAL_PORT}"' in content
    # env_file
    assert "- .env" in content
    # data 卷
    assert "- ../data:/app/data" in content
    # 资源限制（默认值）
    assert "${MEMORY_LIMIT:-512m}" in content
    assert '"${CPU_LIMIT:-0.75}"' in content
    # restart
    assert "restart: unless-stopped" in content


def test_compose_pythonpath_for_src_main(workspace: Workspace) -> None:
    """current/src/main.py 存在时注入 PYTHONPATH=src（FastAPI 常见布局）。"""
    workspace.ensure_app_dirs("api")
    src = workspace.app_current("api") / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "main.py").write_text("app = None\n")
    m = _mk_manifest(internal_port=8000)
    content = generate_compose(m, workspace, host_port=18000).read_text(encoding="utf-8")
    assert "environment:" in content
    assert "- PYTHONPATH=src" in content


def test_compose_custom_resource_limits(workspace: Workspace) -> None:
    m = _mk_manifest(memory="1g", cpus="1.5")
    content = generate_compose(m, workspace, host_port=18000).read_text(encoding="utf-8")
    assert "${MEMORY_LIMIT:-1g}" in content
    assert '"${CPU_LIMIT:-1.5}"' in content


def test_compose_header_records_ports(workspace: Workspace) -> None:
    m = _mk_manifest(internal_port=8501)
    content = generate_compose(m, workspace, host_port=18200).read_text(encoding="utf-8")
    assert "host_port=18200" in content
    assert "internal_port=8501" in content
    assert "由 lwa 自动生成" in content


def test_compose_uses_project_name_from_manifest(workspace: Workspace) -> None:
    """container.projectName 应作为顶层 name，避免依赖目录名推断。"""
    m = _mk_manifest(mid="myapi")
    content = generate_compose(m, workspace, host_port=18000).read_text(encoding="utf-8")
    assert "name: lwa-myapi" in content
    assert "container_name: lwa-myapi" in content


def test_compose_rejects_missing_container(workspace: Workspace) -> None:
    """manifest 无 container 配置应直接报错。"""
    m = _mk_manifest()
    m.container = None
    with pytest.raises(ValueError, match="container"):
        generate_compose(m, workspace, host_port=18000)


def test_compose_yaml_is_docker_compose_parseable(workspace: Workspace) -> None:
    """生成的 compose.yaml 必须能被 yaml.safe_load 解析（结构合法）。"""
    import yaml

    m = _mk_manifest(internal_port=3000, memory="256m", cpus="0.5")
    path = generate_compose(m, workspace, host_port=19000)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert data["name"] == "lwa-api"
    svc = data["services"]["app"]
    assert svc["build"] == {"context": "..", "dockerfile": "docker/Dockerfile"}
    assert svc["container_name"] == "lwa-api"
    # 端口串含 ${} 插值（解析后是字符串，未被 YAML 误处理）
    assert svc["ports"] == ["${HOST_PORT}:${INTERNAL_PORT}"]
    # IMP-015：env_file 含可选 .env.local（对象形式 required:false，缺失不报错）
    assert svc["env_file"] == [".env", {"path": ".env.local", "required": False}]
    assert svc["volumes"] == ["../data:/app/data"]
    assert svc["mem_limit"] == "${MEMORY_LIMIT:-256m}"
    assert svc["cpus"] == "${CPU_LIMIT:-0.5}"
    assert svc["restart"] == "unless-stopped"


# ---- .env 渲染 ---------------------------------------------------------------


def test_env_basic_fields(workspace: Workspace) -> None:
    m = _mk_manifest(internal_port=8000, memory="512m", cpus="0.75")
    path = generate_env(m, workspace, host_port=18000)
    assert path == workspace.app_env_path("api")
    text = path.read_text(encoding="utf-8")

    assert "HOST_PORT=18000" in text
    assert "INTERNAL_PORT=8000" in text
    assert "MEMORY_LIMIT=512m" in text
    assert "CPU_LIMIT=0.75" in text
    assert "由 lwa 自动生成" in text


def test_env_sqlite_includes_database_url(workspace: Workspace) -> None:
    m = _mk_manifest(has_database=True, database_type="sqlite")
    text = generate_env(m, workspace, host_port=18000).read_text(encoding="utf-8")
    assert "DATABASE_URL=sqlite:////app/data/app.sqlite" in text


def test_compose_runtime_root_volume_and_env(workspace: Workspace) -> None:
    """BUG-198：runtime_paths 应用挂载 ../data:/app/runtime/data 并注入 RUNTIME_ROOT。"""
    workspace.ensure_app_dirs("api")
    rp = workspace.app_current("api") / "src" / "app"
    rp.mkdir(parents=True, exist_ok=True)
    (rp / "runtime_paths.py").write_text("def get_runtime_root(): ...\n")
    (workspace.app_current("api") / "src" / "main.py").write_text("app=None\n")
    m = _mk_manifest(has_database=True, database_type="sqlite")
    m.database.dataDir = "runtime/data"
    content = generate_compose(m, workspace, host_port=18004).read_text(encoding="utf-8")
    assert "../data:/app/runtime/data" in content
    assert "RUNTIME_ROOT=/app/runtime" in content
    assert "PYTHONPATH=src" in content
    env = generate_env(m, workspace, host_port=18004).read_text(encoding="utf-8")
    assert "DATABASE_URL" not in env


def test_env_local_jwt_secret_auto_generated(workspace: Workspace) -> None:
    """BUG-199：有空 JWT_SECRET 的 .env.example 时自动生成 .env.local。"""
    workspace.ensure_app_dirs("api")
    (workspace.app_current("api") / ".env.example").write_text(
        "JWT_SECRET=\nOPENAI_API_KEY=\n", encoding="utf-8"
    )
    m = _mk_manifest()
    generate_env(m, workspace, host_port=18000)
    local = workspace.app_dir("api") / "docker" / ".env.local"
    assert local.is_file()
    text = local.read_text(encoding="utf-8")
    assert "JWT_SECRET=" in text
    secret = text.split("JWT_SECRET=", 1)[1].strip().splitlines()[0]
    assert len(secret) >= 32
    # 不覆盖已有
    local.write_text("JWT_SECRET=keep-me\n", encoding="utf-8")
    generate_env(m, workspace, host_port=18000)
    assert "keep-me" in local.read_text(encoding="utf-8")


def test_env_local_generated_after_project_update_adds_jwt(workspace: Workspace) -> None:
    """BUG-208：项目更新后 current/.env.example 新增 JWT_SECRET 时仍要生成 .env.local。

    复现：首次导入时 current/.env.example 不含 JWT_SECRET，generate_env 把它复制为
    docker/.env.example（缓存）。项目更新后 current/.env.example 新增空的
    JWT_SECRET，但上方 copy 仅在 docker/.env.example 缺失时复制——旧缓存（无
    JWT_SECRET）不会被刷新。密钥检测必须读"当前源" current/.env.example，否则漏
    生成 .env.local，重建后 token 失效。
    """
    workspace.ensure_app_dirs("api")
    src = workspace.app_current("api") / ".env.example"
    # v1：无 JWT_SECRET
    src.write_text("OPENAI_API_KEY=\n", encoding="utf-8")
    m = _mk_manifest()
    generate_env(m, workspace, host_port=18000)
    cached = workspace.app_dir("api") / "docker" / ".env.example"
    assert cached.is_file()
    assert "JWT_SECRET" not in cached.read_text(encoding="utf-8")
    # v1 阶段无 .env.local（example 无 JWT_SECRET）
    local = workspace.app_dir("api") / "docker" / ".env.local"
    assert not local.exists()

    # v2：项目更新，current/.env.example 新增空 JWT_SECRET（docker/.env.example 仍为旧缓存）
    src.write_text("JWT_SECRET=\nOPENAI_API_KEY=\n", encoding="utf-8")
    generate_env(m, workspace, host_port=18000)
    # 旧缓存未被刷新（copy 仅在缺失时复制）
    assert "JWT_SECRET" not in cached.read_text(encoding="utf-8")
    # BUG-208 修复后：读源 example → 仍生成 .env.local
    assert local.is_file()
    assert "JWT_SECRET=" in local.read_text(encoding="utf-8")


def test_container_data_paths_order_by_layout(workspace: Workspace) -> None:
    """BUG-205：候选容器内数据路径——以新挂载目标优先，兜底历史布局。"""
    from local_webpage_access.compose import container_data_paths

    # 非 RUNTIME_ROOT：新挂载目标是 /app/data，兜底 /app/runtime/data
    m_plain = _mk_manifest(has_database=True, database_type="sqlite")
    assert container_data_paths(workspace.app_current("api"), m_plain) == [
        "/app/data",
        "/app/runtime/data",
    ]
    # RUNTIME_ROOT（dataDir 以 runtime 开头）：新挂载目标是 /app/runtime/data
    m_rt = _mk_manifest(has_database=True, database_type="sqlite")
    m_rt.database.dataDir = "runtime/data"
    assert container_data_paths(workspace.app_current("api"), m_rt) == [
        "/app/runtime/data",
        "/app/data",
    ]


def test_env_non_sqlite_omits_database_url(workspace: Workspace) -> None:
    m = _mk_manifest(has_database=False)
    text = generate_env(m, workspace, host_port=18000).read_text(encoding="utf-8")
    assert "DATABASE_URL" not in text


def test_env_other_db_omits_database_url(workspace: Workspace) -> None:
    """非 sqlite 数据库不注入 DATABASE_URL（V1 只为 SQLite 注入路径）。"""
    m = _mk_manifest(has_database=True, database_type="postgres")
    text = generate_env(m, workspace, host_port=18000).read_text(encoding="utf-8")
    assert "DATABASE_URL" not in text


def test_env_rejects_missing_container(workspace: Workspace) -> None:
    m = _mk_manifest()
    m.container = None
    with pytest.raises(ValueError, match="container"):
        generate_env(m, workspace, host_port=18000)


# ---- service_name + 文件位置 ------------------------------------------------


def test_service_name_constant() -> None:
    assert service_name() == "app"


def test_compose_and_env_in_same_docker_dir(workspace: Workspace) -> None:
    m = _mk_manifest()
    compose_path = generate_compose(m, workspace, host_port=18000)
    env_path = generate_env(m, workspace, host_port=18000)
    assert compose_path.parent == workspace.app_docker("api")
    assert env_path.parent == workspace.app_docker("api")
    assert compose_path.is_file()
    assert env_path.is_file()


# ---- 一起生成时一致性 --------------------------------------------------------


def test_compose_and_env_consistent_ports(workspace: Workspace) -> None:
    """compose.yaml 与 .env 写入的端口必须互相匹配（.env 的值是真实端口）。"""
    m = _mk_manifest(internal_port=3000)
    generate_compose(m, workspace, host_port=19500)
    generate_env(m, workspace, host_port=19500)

    compose_text = workspace.app_compose_path("api").read_text(encoding="utf-8")
    env_text = workspace.app_env_path("api").read_text(encoding="utf-8")

    # compose 引用 .env 变量
    assert "${HOST_PORT}" in compose_text
    assert "${INTERNAL_PORT}" in compose_text
    # .env 提供真实值
    env_vars = dict(
        line.split("=", 1)
        for line in env_text.splitlines()
        if "=" in line and not line.startswith("#")
    )
    assert env_vars["HOST_PORT"] == "19500"
    assert env_vars["INTERNAL_PORT"] == "3000"


# ---- IMP-015：业务 .env.example 合并 + 多层 env_file ------------------------


def test_env_example_copied_to_docker(workspace: Workspace) -> None:
    """IMP-015：current/.env.example 存在 → 复制为 docker/.env.example。"""
    workspace.ensure_app_dirs("api")
    env_example = workspace.app_current("api") / ".env.example"
    env_example.write_text("API_KEY=changeme\nDB_URL=sqlite:///app.db\n", encoding="utf-8")

    m = _mk_manifest(internal_port=8000)
    generate_env(m, workspace, host_port=18000)

    copied = workspace.app_env_path("api").parent / ".env.example"
    assert copied.is_file()
    assert "API_KEY=changeme" in copied.read_text(encoding="utf-8")


def test_env_example_not_overwritten_if_exists(workspace: Workspace) -> None:
    """IMP-015：docker/.env.example 已存在时不覆盖（保留用户改动）。"""
    workspace.ensure_app_dirs("api")
    (workspace.app_current("api") / ".env.example").write_text(
        "SOURCE=upstream\n", encoding="utf-8"
    )
    target = workspace.app_env_path("api").parent / ".env.example"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("USER_EDITED=keep\n", encoding="utf-8")

    m = _mk_manifest(internal_port=8000)
    generate_env(m, workspace, host_port=18000)

    assert target.read_text(encoding="utf-8") == "USER_EDITED=keep\n"


def test_env_local_in_compose_env_file(workspace: Workspace) -> None:
    """IMP-015：compose env_file 含可选 .env.local（required:false，缺失不报错）。"""
    m = _mk_manifest(internal_port=8000)
    path = generate_compose(m, workspace, host_port=18000)
    content = path.read_text(encoding="utf-8")
    assert "path: .env.local" in content
    assert "required: false" in content
    # .env 仍是必需的第一层
    assert "- .env" in content
