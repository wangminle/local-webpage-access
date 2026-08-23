"""Docker Compose 与 .env 模板测试（WBS-13）。"""

from __future__ import annotations


from pathlib import Path

import pytest

from local_webpage_access.compose import (
    ensure_env_local_secrets,
    generate_compose,
    generate_env,
    service_name,
)
from local_webpage_access.errors import ConfigError
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
    consumes_db_url: bool = True,
) -> InstanceManifest:
    """构造测试 manifest。

    Parameters
    ----------
    consumes_db_url
        A.R01：是否模拟应用消费 DATABASE_URL 的证据。
        默认 True（向后兼容已有测试）；设 False 测试无消费证据场景。
    """
    m = InstanceManifest(
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
    if has_database and consumes_db_url:
        m.databaseConfig = {"consumesDatabaseUrl": True, "sourcePath": "config.py"}
    elif has_database and not consumes_db_url:
        m.databaseConfig = {"consumesDatabaseUrl": False, "sourcePath": None}
    return m


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


# ---- issue#1：PORT 注入 / extraVolumes 挂载出口 -----------------------------


def test_compose_injects_port_env(workspace: Workspace) -> None:
    """issue#1 附加观察：统一注入 PORT=${INTERNAL_PORT}，PORT 约定应用与探针对齐。"""
    import yaml

    m = _mk_manifest(internal_port=8000)
    path = generate_compose(m, workspace, host_port=18000)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    svc = data["services"]["app"]
    assert "environment" in svc
    assert "PORT=${INTERNAL_PORT}" in svc["environment"]


def test_compose_merges_extra_volumes(workspace: Workspace) -> None:
    """issue#1 问题2：container.extraVolumes 合并进 volumes，重生成不丢业务挂载。"""
    import yaml

    m = _mk_manifest(internal_port=8000)
    assert m.container is not None
    m.container.extraVolumes = ["/home/fenix-wang/.openclaw/workspace:/workspace:ro"]
    path = generate_compose(m, workspace, host_port=18000)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    svc = data["services"]["app"]
    assert svc["volumes"] == [
        "../data:/app/data",
        "/home/fenix-wang/.openclaw/workspace:/workspace:ro",
    ]


def test_compose_rejects_malformed_extra_volume(workspace: Workspace) -> None:
    """extraVolumes 含换行（YAML 注入向量）时拒绝写出。"""
    m = _mk_manifest(internal_port=8000)
    assert m.container is not None
    m.container.extraVolumes = ["../data:/app/data\n    privileged: true"]
    with pytest.raises(ValueError, match="extraVolumes"):
        generate_compose(m, workspace, host_port=18000)


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


def test_env_sqlite_preserves_source_db_filename(workspace: Workspace) -> None:
    """IMP-058 Gate-A CHK-V03：DATABASE_URL 保留 scanner 扫描到的源文件名。"""
    m = _mk_manifest(has_database=True, database_type="sqlite")
    m.database.dbFilename = "bookshelf.db"
    text = generate_env(m, workspace, host_port=18000).read_text(encoding="utf-8")
    assert "DATABASE_URL=sqlite:////app/data/bookshelf.db" in text
    assert "app.sqlite" not in text


def test_env_sqlite_no_consumption_skips_injection(workspace: Workspace) -> None:
    """A.R01 反例：应用不消费 DATABASE_URL 时不注入，添加注释提示。"""
    m = _mk_manifest(has_database=True, database_type="sqlite", consumes_db_url=False)
    text = generate_env(m, workspace, host_port=18000).read_text(encoding="utf-8")
    assert "DATABASE_URL=sqlite:" not in text
    assert "A.R01" in text
    assert "未检测到应用消费 DATABASE_URL" in text


def test_env_sqlite_no_config_skips_injection(workspace: Workspace) -> None:
    """A.R01：manifest 无 databaseConfig 字段时不注入。"""
    m = _mk_manifest(has_database=True, database_type="sqlite")
    m.databaseConfig = None
    text = generate_env(m, workspace, host_port=18000).read_text(encoding="utf-8")
    assert "DATABASE_URL=sqlite:" not in text
    assert "A.R01" in text


def test_env_sqlite_preserves_existing_database_url_on_regen(workspace: Workspace) -> None:
    """BUG-491：重新生成 .env 时保留已有 DATABASE_URL，避免被源目录占位文件覆盖。"""
    m = _mk_manifest(has_database=True, database_type="sqlite")
    m.database.dbFilename = "_empty_check.db"

    # 首次生成：指向 scanner 检测到的占位文件
    first_path = generate_env(m, workspace, host_port=18000)
    first_text = first_path.read_text(encoding="utf-8")
    assert "DATABASE_URL=sqlite:////app/data/_empty_check.db" in first_text

    # 模拟用户手动修正 DATABASE_URL 指向真实数据库
    corrected_url = "sqlite:////app/data/app.sqlite"
    first_path.write_text(
        first_text.replace(
            "DATABASE_URL=sqlite:////app/data/_empty_check.db",
            f"DATABASE_URL={corrected_url}",
        ),
        encoding="utf-8",
    )

    # 重新生成（模拟更新实例）：应保留已修正的 DATABASE_URL
    second_path = generate_env(m, workspace, host_port=18000)
    second_text = second_path.read_text(encoding="utf-8")
    assert f"DATABASE_URL={corrected_url}" in second_text
    assert "_empty_check.db" not in second_text


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
    # BUG-474: 所有 SQLite 项目都注入绝对路径 DATABASE_URL，包括 RUNTIME_ROOT 布局。
    assert "DATABASE_URL=sqlite:////app/data/app.sqlite" in env


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


# ---- CHK-193/P1：sourceSubdir SQLite 源库复制 --------------------------------


def test_env_sqlite_subdir_source_db_copied(workspace: Workspace) -> None:
    """CHK-193/P1：sourceSubdir 设定时，SQLite 源文件从子目录复制。"""
    workspace.ensure_app_dirs("api")
    current = workspace.app_current("api")
    # 模拟 backend/ 子目录布局
    backend_dir = current / "backend"
    backend_dir.mkdir(parents=True)
    (backend_dir / "requirements.txt").write_text("flask\n")
    # 源 SQLite 文件在 backend/ 子目录
    (backend_dir / "bookshelf.db").write_bytes(b"SQLite dummy")

    m = _mk_manifest(has_database=True, database_type="sqlite")
    m.database.dbFilename = "bookshelf.db"
    m.sourceSubdir = "backend"

    text = generate_env(m, workspace, host_port=18000).read_text(encoding="utf-8")
    assert "DATABASE_URL=sqlite:////app/data/bookshelf.db" in text

    # 源文件应被复制到宿主 data 目录
    target = workspace.app_data("api") / "bookshelf.db"
    assert target.is_file(), f"源 SQLite 未复制到 {target}"


def test_env_sqlite_subdir_relative_path_source_db_copied(workspace: Workspace) -> None:
    """CHK-193/P1：sourceSubdir + 相对路径 dbFilename 组合。"""
    workspace.ensure_app_dirs("api")
    current = workspace.app_current("api")
    backend_dir = current / "backend"
    backend_dir.mkdir(parents=True)
    (backend_dir / "data").mkdir()
    # 源文件在 backend/data/app.sqlite
    (backend_dir / "data" / "app.sqlite").write_bytes(b"SQLite dummy")

    m = _mk_manifest(has_database=True, database_type="sqlite")
    m.database.dbFilename = "data/app.sqlite"
    m.sourceSubdir = "backend"

    text = generate_env(m, workspace, host_port=18000).read_text(encoding="utf-8")
    # basename 提取后 DATABASE_URL 指向 app.sqlite
    assert "DATABASE_URL=sqlite:////app/data/app.sqlite" in text

    target = workspace.app_data("api") / "app.sqlite"
    assert target.is_file(), f"源 SQLite 未复制到 {target}"


def test_env_sqlite_no_subdir_uses_root_path(workspace: Workspace) -> None:
    """CHK-193/P1：无 sourceSubdir 时，源文件从根目录查找（向后兼容）。"""
    workspace.ensure_app_dirs("api")
    current = workspace.app_current("api")
    (current / "requirements.txt").write_text("flask\n")
    (current / "bookshelf.db").write_bytes(b"SQLite dummy")

    m = _mk_manifest(has_database=True, database_type="sqlite")
    m.database.dbFilename = "bookshelf.db"
    # sourceSubdir 未设置

    generate_env(m, workspace, host_port=18000)
    target = workspace.app_data("api") / "bookshelf.db"
    assert target.is_file(), f"源 SQLite 未复制到 {target}"


# ---- issue #11：业务键安全迁移 -------------------------------------------------


def test_env_business_keys_migrate_to_env_local_on_regen(workspace: Workspace) -> None:
    """issue #10/#11 主诉：重生成 .env 不再整文件覆盖，业务键迁入 .env.local。"""
    m = _mk_manifest()
    first = generate_env(m, workspace, host_port=18000)
    # 模拟用户在 .env 手写业务键（此前重生成会被整体抹掉）。
    first.write_text(
        first.read_text(encoding="utf-8")
        + "JWT_SECRET=my-secret\nOPENAI_API_KEY=sk-test\n",
        encoding="utf-8",
    )
    second = generate_env(m, workspace, host_port=18001)
    env_text = second.read_text(encoding="utf-8")
    # 管理键按新值重写。
    assert "HOST_PORT=18001" in env_text
    # 业务键不再滞留在 .env，也不被静默丢弃。
    assert "JWT_SECRET=" not in env_text
    assert "OPENAI_API_KEY=" not in env_text
    local = second.parent / ".env.local"
    local_text = local.read_text(encoding="utf-8")
    assert "JWT_SECRET=my-secret" in local_text
    assert "OPENAI_API_KEY=sk-test" in local_text


def test_env_local_existing_value_wins_on_conflict(workspace: Workspace) -> None:
    """issue #11：.env.local 已有同名键时现有值优先，迁移只报告冲突。"""
    m = _mk_manifest()
    first = generate_env(m, workspace, host_port=18000)
    docker_dir = first.parent
    # 用户已把密钥填在 .env.local，同时旧 .env 里还留着一份旧值。
    (docker_dir / ".env.local").write_text("JWT_SECRET=local-wins\n", encoding="utf-8")
    first.write_text(
        first.read_text(encoding="utf-8") + "JWT_SECRET=stale-value\n", encoding="utf-8"
    )
    generate_env(m, workspace, host_port=18001)
    local_text = (docker_dir / ".env.local").read_text(encoding="utf-8")
    assert "JWT_SECRET=local-wins" in local_text
    assert "stale-value" not in local_text


def test_env_unparseable_lines_backed_up_not_dropped(workspace: Workspace) -> None:
    """issue #11：无法按 KEY=VALUE 解析的行整体备份并告警，绝不静默丢弃。"""
    m = _mk_manifest()
    first = generate_env(m, workspace, host_port=18000)
    first.write_text(
        "HOST_PORT=18000\n"
        "THIS IS NOT AN ASSIGNMENT\n"
        "API_KEY=keep\n",
        encoding="utf-8",
    )
    generate_env(m, workspace, host_port=18001)
    backups = sorted(p for p in first.parent.glob(".env.lwa-backup-*"))
    assert len(backups) == 1
    backup_text = backups[0].read_text(encoding="utf-8")
    assert "THIS IS NOT AN ASSIGNMENT" in backup_text
    # 可解析的业务键正常迁移，坏行只留在备份里。
    local_text = (first.parent / ".env.local").read_text(encoding="utf-8")
    assert "API_KEY=keep" in local_text
    assert "THIS IS NOT AN ASSIGNMENT" not in local_text
    assert "THIS IS NOT AN ASSIGNMENT" not in first.read_text(encoding="utf-8")


def test_env_and_env_local_file_modes_are_0600(workspace: Workspace) -> None:
    """issue #11：.env / .env.local 含密钥类值，原子写入统一 0600 权限。"""
    workspace.ensure_app_dirs("api")
    (workspace.app_current("api") / ".env.example").write_text(
        "JWT_SECRET=\n", encoding="utf-8"
    )
    m = _mk_manifest()
    first = generate_env(m, workspace, host_port=18000)
    first.write_text(
        first.read_text(encoding="utf-8") + "API_KEY=keep\n", encoding="utf-8"
    )
    second = generate_env(m, workspace, host_port=18001)
    assert (second.stat().st_mode & 0o777) == 0o600
    assert ((second.parent / ".env.local").stat().st_mode & 0o777) == 0o600


def test_env_non_sqlite_database_url_migrates_as_business_key(workspace: Workspace) -> None:
    """issue #11：非 SQLite 实例的 DATABASE_URL 是用户键（LWA 从不写它），迁移不丢弃。"""
    m = _mk_manifest(has_database=True, database_type="postgres")
    first = generate_env(m, workspace, host_port=18000)
    first.write_text(
        first.read_text(encoding="utf-8")
        + "DATABASE_URL=postgres://user:pw@db:5432/app\n",
        encoding="utf-8",
    )
    second = generate_env(m, workspace, host_port=18001)
    assert "DATABASE_URL=" not in second.read_text(encoding="utf-8")
    local_text = (second.parent / ".env.local").read_text(encoding="utf-8")
    assert "DATABASE_URL=postgres://user:pw@db:5432/app" in local_text


def test_env_local_migration_then_jwt_secret_append(workspace: Workspace) -> None:
    """issue #11：业务键迁移先创建 .env.local（无 JWT_SECRET）时，自动密钥改为追加。"""
    workspace.ensure_app_dirs("api")
    (workspace.app_current("api") / ".env.example").write_text(
        "JWT_SECRET=\n", encoding="utf-8"
    )
    m = _mk_manifest()
    first = generate_env(m, workspace, host_port=18000)
    # 旧 .env 里用户手写了业务密钥（迁移后 .env.local 无 JWT_SECRET）。
    first.write_text(
        first.read_text(encoding="utf-8") + "API_KEY=keep\n", encoding="utf-8"
    )
    second = generate_env(m, workspace, host_port=18001)
    local = second.parent / ".env.local"
    local_text = local.read_text(encoding="utf-8")
    # 迁移的业务键保留，且自动追加了 JWT_SECRET（此前文件已存在就直接跳过）。
    assert "API_KEY=keep" in local_text
    assert "JWT_SECRET=" in local_text
    # 追加位置在业务键之后，且只有一条 JWT_SECRET。
    assert local_text.count("JWT_SECRET=") == 1


def test_env_invalid_key_names_backed_up_not_migrated(workspace: Workspace) -> None:
    """含空格等非法字符的键名不能迁入 .env.local（compose 解析 env_file 可能整体报错）。"""
    m = _mk_manifest()
    first = generate_env(m, workspace, host_port=18000)
    first.write_text(
        "HOST_PORT=18000\n"
        "SECRET VALUE=1\n"
        "API KEY=x\n"
        "API_KEY=keep\n",
        encoding="utf-8",
    )
    generate_env(m, workspace, host_port=18001)
    local_text = (first.parent / ".env.local").read_text(encoding="utf-8")
    assert "API_KEY=keep" in local_text
    assert "SECRET VALUE=1" not in local_text
    assert "API KEY=x" not in local_text
    # 坏键行进备份，不静默丢弃。
    backups = sorted(p for p in first.parent.glob(".env.lwa-backup-*"))
    assert len(backups) == 1
    backup_text = backups[0].read_text(encoding="utf-8")
    assert "SECRET VALUE=1" in backup_text
    assert "API KEY=x" in backup_text


def test_env_local_export_prefixed_jwt_secret_not_duplicated(workspace: Workspace) -> None:
    """export 前缀的 JWT_SECRET 须识别为已有密钥，不得追加第二条（后值会顶掉用户值）。"""
    workspace.ensure_app_dirs("api")
    (workspace.app_current("api") / ".env.example").write_text(
        "JWT_SECRET=\nAPI_KEY=\n", encoding="utf-8"
    )
    m = _mk_manifest()
    first = generate_env(m, workspace, host_port=18000)
    # 用户在 .env.local 手写（或迁移带入）了 export 前缀形式的 JWT_SECRET。
    local = first.parent / ".env.local"
    local.write_text("export JWT_SECRET=my-secret\n", encoding="utf-8")
    first.write_text(
        first.read_text(encoding="utf-8") + "API_KEY=keep\n", encoding="utf-8"
    )
    generate_env(m, workspace, host_port=18001)
    local_text = local.read_text(encoding="utf-8")
    assert "export JWT_SECRET=my-secret" in local_text
    assert "API_KEY=keep" in local_text
    # 修复前：检测用 ^JWT_SECRET= 严格匹配识别不到 export 行，会追加自动
    # 生成的第二条 JWT_SECRET，compose env_file 后值覆盖前值，用户值被顶掉。
    assert local_text.count("JWT_SECRET=") == 1


# ---- BUG-585：重生成 fail-closed -------------------------------------------------


def test_env_regen_aborts_when_old_env_unreadable(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BUG-585：旧 .env 存在但读取抛 OSError 时中止重生成，原 .env 内容不变。"""
    m = _mk_manifest()
    env_path = generate_env(m, workspace, host_port=18000)
    env_path.write_text(
        env_path.read_text(encoding="utf-8") + "JWT_SECRET=my-secret\n",
        encoding="utf-8",
    )
    original = env_path.read_text(encoding="utf-8")

    real_read_text = Path.read_text

    def boom(self: Path, *args: object, **kwargs: object) -> str:
        if self == env_path:
            raise OSError("模拟瞬时 I/O 错误")
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", boom)
    with pytest.raises(ConfigError):
        generate_env(m, workspace, host_port=18001)
    monkeypatch.undo()
    assert env_path.read_text(encoding="utf-8") == original


def test_env_regen_aborts_when_env_local_unreadable(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BUG-585：.env.local 存在但读取失败时中止，.env 与 .env.local 内容均不变。"""
    m = _mk_manifest()
    env_path = generate_env(m, workspace, host_port=18000)
    env_path.write_text(
        env_path.read_text(encoding="utf-8") + "JWT_SECRET=my-secret\n",
        encoding="utf-8",
    )
    local_path = env_path.parent / ".env.local"
    local_path.write_text("JWT_SECRET=local-wins\n", encoding="utf-8")
    original_env = env_path.read_text(encoding="utf-8")
    original_local = local_path.read_text(encoding="utf-8")

    real_read_text = Path.read_text

    def boom(self: Path, *args: object, **kwargs: object) -> str:
        if self == local_path:
            raise OSError("模拟权限错误")
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", boom)
    with pytest.raises(ConfigError):
        generate_env(m, workspace, host_port=18001)
    monkeypatch.undo()
    assert env_path.read_text(encoding="utf-8") == original_env
    assert local_path.read_text(encoding="utf-8") == original_local


def test_env_regen_aborts_when_backup_fails(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BUG-585：含无法解析行的旧 .env 备份失败时中止，原 .env 内容不变。"""
    m = _mk_manifest()
    env_path = generate_env(m, workspace, host_port=18000)
    env_path.write_text(
        "HOST_PORT=18000\nTHIS IS NOT AN ASSIGNMENT\nAPI_KEY=keep\n",
        encoding="utf-8",
    )
    original = env_path.read_text(encoding="utf-8")

    real_write_text = Path.write_text

    def boom(self: Path, *args: object, **kwargs: object) -> int:
        if self.name.startswith(".env.lwa-backup-"):
            raise OSError("模拟磁盘写失败")
        return real_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", boom)
    with pytest.raises(ConfigError):
        generate_env(m, workspace, host_port=18001)
    assert env_path.read_text(encoding="utf-8") == original


# ---- ensure_env_local_secrets fail-closed（BUG-585 收尾）------------------------


def test_ensure_env_local_secrets_aborts_when_env_local_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ensure_env_local_secrets：.env.local 存在但读取失败时抛 ConfigError，不静默跳过。"""
    local_path = tmp_path / ".env.local"
    local_path.write_text("API_KEY=x\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("JWT_SECRET=\n", encoding="utf-8")
    original = local_path.read_text(encoding="utf-8")

    real_read_text = Path.read_text

    def boom(self: Path, *args: object, **kwargs: object) -> str:
        if self == local_path:
            raise OSError("模拟权限错误")
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", boom)
    with pytest.raises(ConfigError):
        ensure_env_local_secrets(tmp_path)
    monkeypatch.undo()
    assert local_path.read_text(encoding="utf-8") == original


def test_ensure_env_local_secrets_aborts_when_example_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ensure_env_local_secrets：.env.example 读取失败时抛 ConfigError，且不创建 .env.local。"""
    example_path = tmp_path / ".env.example"
    example_path.write_text("JWT_SECRET=\n", encoding="utf-8")

    real_read_text = Path.read_text

    def boom(self: Path, *args: object, **kwargs: object) -> str:
        if self == example_path:
            raise OSError("模拟瞬时 I/O 错误")
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", boom)
    with pytest.raises(ConfigError):
        ensure_env_local_secrets(tmp_path)
    assert not (tmp_path / ".env.local").exists()
