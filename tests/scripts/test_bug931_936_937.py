"""BUG-931/936/937：Flask init-db 守卫路径完整证明、独立子目录工具链边界、
WSL doctor 缺口端口与建议同源。"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import shutil
from pathlib import Path

import pytest

from local_webpage_access.candidate_generator import generate_candidates
from local_webpage_access.config import load_config
from local_webpage_access.doctor import STATUS_WARN, check_wsl_lan_exposure
from local_webpage_access.evidence_collector import collect
from local_webpage_access.init_workspace import init_workspace
from local_webpage_access.node_toolchain import inspect_node_toolchain
from local_webpage_access.paths import Workspace
from local_webpage_access.ports import lan_entry_port
from local_webpage_access.registry import Registry
from local_webpage_access.scanner import Scanner
from local_webpage_access.wsl_network import describe_instance_access
from local_webpage_access.compose import generate_compose
from tests.scripts.test_compose import _mk_manifest as _compose_manifest

# ---- BUG-931：init-db 守卫必须检查源码能完整证明的实际库路径 -----------------

_DEFAULT_CTOR = "Flask(__name__, instance_relative_config=True)"


def _write_factory(root: Path, database_expr: str, *, ctor: str = _DEFAULT_CTOR) -> None:
    """flask 教程式工厂包：__init__.py 配 DATABASE，db.py 含唯一 click init-db。"""
    pkg = root / "flaskr"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "from flask import Flask\n"
        "from . import db\n"
        "\n"
        "def create_app():\n"
        f"    app = {ctor}\n"
        "    app.config.from_mapping(\n"
        "        SECRET_KEY='dev',\n"
        f"        DATABASE={database_expr},\n"
        "    )\n"
        "    db.init_app(app)\n"
        "    return app\n",
        encoding="utf-8",
    )
    (pkg / "db.py").write_text(
        "import click\n"
        "\n"
        "@click.command('init-db')\n"
        "def init_db_command():\n"
        "    pass\n"
        "\n"
        "def init_app(app):\n"
        "    app.cli.add_command(init_db_command)\n",
        encoding="utf-8",
    )
    (root / "requirements.txt").write_text("flask\n", encoding="utf-8")


def _detect_start(root: Path) -> tuple[str, list[str]]:
    result = Scanner().detect(root)
    return result.entry.start or "", list(result.notes)


@pytest.mark.parametrize(
    "expr",
    [
        "os.path.join(app.instance_path, 'db', 'prod.sqlite')",
        "os.path.join(app.instance_path, 'db/prod.sqlite')",
        "app.instance_path + '/db/prod.sqlite'",
        "app.instance_path + '/db' + '/prod.sqlite'",
        "Path(app.instance_path) / 'db' / 'prod.sqlite'",
        'f"{app.instance_path}/db/prod.sqlite"',
    ],
    ids=["join-multi", "join-single-dir", "concat", "concat-2seg", "pathlib", "fstring"],
)
def test_init_db_guard_uses_full_instance_relative_path(tmp_path: Path, expr: str) -> None:
    """四种字面量拼接形态都要得到 /app/instance/db/prod.sqlite，而不是只取 basename。"""
    _write_factory(tmp_path, expr)
    start, _notes = _detect_start(tmp_path)
    assert "init-db" in start
    assert "[ ! -f /app/instance/db/prod.sqlite ]" in start
    assert "/app/instance/prod.sqlite" not in start


@pytest.mark.parametrize(
    "expr",
    [
        "os.path.join(app.instance_path, DB_NAME)",
        "os.path.join(app.instance_path, 'db', DB_NAME)",
        "os.path.join(app.instance_path, '/tmp/prod.sqlite')",
        "app.instance_path + '/db/' + name + '.sqlite'",
        "Path(app.instance_path) / 'db' / name",
        'f"{app.instance_path}/{name}.sqlite"',
    ],
    ids=["join-var", "join-dir-var", "join-abs-seg", "concat-var", "pathlib-var", "fstring-var"],
)
def test_init_db_guard_skipped_when_path_unprovable(tmp_path: Path, expr: str) -> None:
    """任一段不是字符串字面量（或绝对段）→ 路径不确定 → 不包装 init-db。"""
    _write_factory(tmp_path, expr)
    start, _notes = _detect_start(tmp_path)
    assert "init-db" not in start
    assert start.startswith("gunicorn") or start.startswith("sh -c 'gunicorn")


def test_init_db_guard_notes_unprovable_sqlite_path(tmp_path: Path) -> None:
    """检测到 instance SQLite 但无法证明路径时，notes 要说明为何没包装。"""
    _write_factory(tmp_path, "os.path.join(app.instance_path, 'db', DB_NAME, 'prod.sqlite')")
    start, notes = _detect_start(tmp_path)
    assert "init-db" not in start
    assert any("无法从源码完整证明" in note for note in notes)


def test_init_db_guard_honors_custom_literal_instance_path(tmp_path: Path) -> None:
    """Flask(..., instance_path='/data') 时守卫检查 /data/<库>，不再硬编码 /app/instance。"""
    _write_factory(
        tmp_path,
        "os.path.join(app.instance_path, 'prod.sqlite')",
        ctor="Flask(__name__, instance_path='/data')",
    )
    start, _notes = _detect_start(tmp_path)
    assert "[ ! -f /data/prod.sqlite ]" in start
    assert "/app/instance" not in start


def test_init_db_guard_skipped_when_instance_path_is_not_literal(tmp_path: Path) -> None:
    _write_factory(
        tmp_path,
        "os.path.join(app.instance_path, 'prod.sqlite')",
        ctor="Flask(__name__, instance_path=os.environ['LWA_INSTANCE'])",
    )
    start, _notes = _detect_start(tmp_path)
    assert "init-db" not in start


def test_init_db_guard_two_distinct_databases_is_ambiguous(tmp_path: Path) -> None:
    pkg = tmp_path / "flaskr"
    _write_factory(tmp_path, "os.path.join(app.instance_path, 'a.sqlite')")
    (pkg / "extra.py").write_text(
        "import os\n"
        "def other(app):\n"
        "    return os.path.join(app.instance_path, 'b.sqlite')\n",
        encoding="utf-8",
    )
    start, _notes = _detect_start(tmp_path)
    assert "init-db" not in start


@pytest.mark.parametrize("expr", [
    "os.path.join(app.instance_path, 'prod.sqlite') + '.backup'",
    "os.path.join(app.instance_path, 'prod.sqlite') + suffix",
    "(Path(app.instance_path) / 'prod.sqlite').with_name('actual.sqlite')",
    'f"{app.instance_path}/prod.sqlite" + suffix',
])
def test_init_db_does_not_accept_a_path_subexpression(tmp_path: Path, expr: str) -> None:
    _write_factory(tmp_path, expr)
    start, _ = _detect_start(tmp_path)
    assert "init-db" not in start


@pytest.mark.parametrize("override", [
    "app.config['DATABASE'] = os.environ['DATABASE_PATH']",
    "app.config.update(DATABASE=os.environ['DATABASE_PATH'])",
    "app.config.from_mapping(DATABASE=os.path.join(app.instance_path, DB_NAME))",
    "app.config.from_object('settings')",
])
def test_init_db_skipped_for_unproven_configuration_override(
    tmp_path: Path, override: str,
) -> None:
    _write_factory(tmp_path, "os.path.join(app.instance_path, 'prod.sqlite')")
    init = tmp_path / "flaskr" / "__init__.py"
    init.write_text(init.read_text().replace("    return app", f"    {override}\n    return app"))
    start, _ = _detect_start(tmp_path)
    assert "init-db" not in start


@pytest.mark.parametrize("ctor", [
    "Flask(__name__, **{'instance_path': '/data'})",
    "Flask(__name__, **options)",
    "build_app()",
])
def test_init_db_skipped_for_unproven_constructor(tmp_path: Path, ctor: str) -> None:
    _write_factory(tmp_path, "os.path.join(app.instance_path, 'prod.sqlite')", ctor=ctor)
    start, _ = _detect_start(tmp_path)
    assert "init-db" not in start


def test_mutated_factory_default_does_not_hide_config_override(tmp_path: Path) -> None:
    _write_factory(tmp_path, "os.path.join(app.instance_path, 'prod.sqlite')")
    init = tmp_path / "flaskr" / "__init__.py"
    text = init.read_text().replace("def create_app():", "def create_app(test_config=None):")
    text = text.replace("    return app", (
        "    test_config = os.environ\n"
        "    if test_config is not None:\n"
        "        app.config.update(test_config)\n"
        "    return app"
    ))
    init.write_text(text)
    start, _ = _detect_start(tmp_path)
    assert "init-db" not in start


def test_reassigned_app_does_not_use_another_objects_instance_path(tmp_path: Path) -> None:
    _write_factory(tmp_path, "os.path.join(app.instance_path, 'prod.sqlite')")
    init = tmp_path / "flaskr" / "__init__.py"
    init.write_text(init.read_text().replace("    return app", "    app = make_app()\n    return app"))
    start, _ = _detect_start(tmp_path)
    assert "init-db" not in start


def test_optional_instance_config_blocks_init_when_present(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write_factory(project, "os.path.join(app.instance_path, 'prod.sqlite')")
    init = project / "flaskr" / "__init__.py"
    init.write_text(init.read_text().replace(
        "    return app", "    app.config.from_pyfile('config.py', silent=True)\n    return app",
    ))
    start, _ = _detect_start(project)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "init.called"
    _make_stub(bin_dir, "flask", 'touch "$MARKER"\nexit 0\n')
    _make_stub(bin_dir, "gunicorn", "exit 0\n")
    instance = tmp_path / "instance"
    instance.mkdir()
    # config.py 可将 DATABASE 指向任意已有库；存在时不得自动初始化默认路径。
    (instance / "config.py").write_text("DATABASE='/data/existing.sqlite'\n")
    assert _run_guard(start, instance_root=instance, bin_dir=bin_dir, marker=marker) == 0
    assert not marker.exists()
    (instance / "config.py").unlink()
    assert _run_guard(start, instance_root=instance, bin_dir=bin_dir, marker=marker) == 0
    assert marker.exists()


@pytest.mark.parametrize("expr", [
    "os.path.join(app.instance_path, 'prod.sqlite')",
    "os.path.join(app.instance_path, 'db', 'prod.sqlite')",
])
def test_custom_flask_instance_directory_is_persisted(workspace: Workspace, expr: str) -> None:
    _write_factory(workspace.app_current("api"), expr, ctor="Flask(__name__, instance_path='/data')")
    manifest = _compose_manifest(has_database=True, database_type="sqlite")
    content = generate_compose(manifest, workspace, host_port=18000).read_text()
    assert "../data/instance:/data" in content
    assert "../data/instance:/app/instance" not in content
    persisted = workspace.app_data("api") / "instance" / "prod.sqlite"
    persisted.write_bytes(b"existing database")
    generate_compose(manifest, workspace, host_port=18000)
    assert persisted.read_bytes() == b"existing database"


def test_unknown_instance_path_does_not_get_a_guessed_mount(workspace: Workspace) -> None:
    _write_factory(workspace.app_current("api"), "os.path.join(app.instance_path, 'prod.sqlite')",
                   ctor="Flask(__name__, instance_path=DATA_DIR)")
    manifest = _compose_manifest(has_database=True, database_type="sqlite")
    content = generate_compose(manifest, workspace, host_port=18000).read_text()
    assert "../data/instance:" not in content


def test_explicit_custom_instance_mount_is_preserved(workspace: Workspace, tmp_path: Path) -> None:
    _write_factory(workspace.app_current("api"), "os.path.join(app.instance_path, 'prod.sqlite')",
                   ctor="Flask(__name__, instance_path='/data')")
    manifest = _compose_manifest(has_database=True, database_type="sqlite")
    manifest.container.extraVolumes = [f"{tmp_path}/custom:/data"]
    content = generate_compose(manifest, workspace, host_port=18000).read_text()
    assert f"{tmp_path}/custom:/data" in content
    assert "../data/instance:" not in content


def test_custom_instance_inside_managed_data_uses_existing_mount(workspace: Workspace) -> None:
    _write_factory(workspace.app_current("api"), "os.path.join(app.instance_path, 'prod.sqlite')",
                   ctor="Flask(__name__, instance_path='/app/data')")
    manifest = _compose_manifest(has_database=True, database_type="sqlite")
    content = generate_compose(manifest, workspace, host_port=18000).read_text()
    assert content.count(":/app/data") == 1
    assert "../data/instance:" not in content


def test_custom_instance_is_rescued_before_rebuild(workspace: Workspace, tmp_path: Path, monkeypatch):
    from local_webpage_access.docker_runtime import DockerRuntime, ComposeResult
    from local_webpage_access.hosting import _rescue_container_data_before_rebuild

    _write_factory(workspace.app_current("api"), "os.path.join(app.instance_path, 'prod.sqlite')",
                   ctor="Flask(__name__, instance_path='/data')")
    manifest = _compose_manifest(has_database=True, database_type="sqlite")
    generate_compose(manifest, workspace, host_port=18000)
    old = tmp_path / "old-instance"
    old.mkdir()
    (old / "prod.sqlite").write_bytes(b"live container database")

    def execute(args, **kwargs):
        assert args[:2] == ["docker", "cp"]
        assert "/data/." in args[2]
        shutil.copytree(old, Path(args[-1]), dirs_exist_ok=True)
        return ComposeResult(args=args, returncode=0)

    runtime = DockerRuntime(workspace)
    monkeypatch.setattr(runtime, "container_id_strict", lambda *a, **k: "old")
    monkeypatch.setattr(runtime, "rescue_container_data", lambda *a, **k: 0)
    monkeypatch.setattr("local_webpage_access.docker_runtime._execute", execute)
    _rescue_container_data_before_rebuild(workspace, manifest, "api", runtime)
    assert (workspace.app_data("api") / "instance" / "prod.sqlite").read_bytes() == b"live container database"


def _make_stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_guard(start: str, *, instance_root: Path, bin_dir: Path, marker: Path) -> int:
    """把生成的 sh -c 守卫脚本里的 /app/instance 替换为临时目录后真实执行。"""
    assert start.startswith("sh -c '") and start.endswith("'"), start
    inner = start[len("sh -c '") : -1].replace("/app/instance", str(instance_root))
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}", "MARKER": str(marker)}
    return subprocess.run(["sh", "-c", inner], env=env, capture_output=True, text=True).returncode


def test_init_db_guard_executes_only_when_real_database_missing(tmp_path: Path) -> None:
    """复核方的复现：真实 instance/db/prod.sqlite 已存在时不得再调用 init-db。"""
    project = tmp_path / "project"
    project.mkdir()
    _write_factory(project, "os.path.join(app.instance_path, 'db', 'prod.sqlite')")
    start, _notes = _detect_start(project)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "init-db.called"
    _make_stub(bin_dir, "flask", 'echo "$@" > "$MARKER"\nexit 0\n')
    _make_stub(bin_dir, "gunicorn", "exit 0\n")
    instance_root = tmp_path / "instance"
    (instance_root / "db").mkdir(parents=True)
    (instance_root / "db" / "prod.sqlite").write_bytes(b"existing-data")

    assert _run_guard(start, instance_root=instance_root, bin_dir=bin_dir, marker=marker) == 0
    assert not marker.exists(), "库已存在仍执行了 init-db"
    assert (instance_root / "db" / "prod.sqlite").read_bytes() == b"existing-data"

    (instance_root / "db" / "prod.sqlite").unlink()
    assert _run_guard(start, instance_root=instance_root, bin_dir=bin_dir, marker=marker) == 0
    assert marker.exists(), "库不存在时应执行 init-db"
    assert "init-db" in marker.read_text(encoding="utf-8")


# ---- BUG-936：独立子目录的锁文件不被根目录非 workspace 锁覆盖 ----------------


def _write_frontend(root: Path, *, lockfile: str | None) -> Path:
    app = root / "frontend"
    app.mkdir(parents=True)
    (app / "package.json").write_text(
        json.dumps(
            {
                "name": "portfolio",
                "dependencies": {"astro": "^5.0.0"},
                "scripts": {"build": "astro build"},
            }
        ),
        encoding="utf-8",
    )
    if lockfile:
        (app / lockfile).write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    return app


def _write_tooling_root(root: Path) -> None:
    """根目录只是工具型 package.json（无 workspaces），带自己的 npm 锁。"""
    (root / "package.json").write_text(
        json.dumps({"name": "repo-tooling", "private": True, "devDependencies": {"prettier": "^3"}}),
        encoding="utf-8",
    )
    (root / "package-lock.json").write_text("{}", encoding="utf-8")


def test_independent_frontend_keeps_own_pnpm_lockfile(tmp_path: Path) -> None:
    """根 package-lock.json（无 workspaces）不得把 frontend/pnpm-lock.yaml 改成 npm ci。"""
    _write_tooling_root(tmp_path)
    app = _write_frontend(tmp_path, lockfile="pnpm-lock.yaml")
    tool = inspect_node_toolchain(tmp_path, workdir=app)
    assert tool.manager == "pnpm"
    assert tool.workspace_root.resolve() == app.resolve()
    assert tool.workspaces is False
    candidates = generate_candidates(collect(tmp_path))
    frontend = next(
        c for c in candidates if c.form == "frontend-static" and c.sourceSubdir == "frontend"
    )
    install = frontend.entry.install or ""
    assert "pnpm" in install
    assert "npm ci" not in install


def test_independent_frontend_without_lockfile_falls_back_to_npm_install(tmp_path: Path) -> None:
    """独立 frontend 无锁文件：按自己的目录用 npm install，而不是借根目录的 npm ci。"""
    _write_tooling_root(tmp_path)
    app = _write_frontend(tmp_path, lockfile=None)
    tool = inspect_node_toolchain(tmp_path, workdir=app)
    assert tool.workspace_root.resolve() == app.resolve()
    assert tool.manager == "npm"
    assert tool.install_command == "npm install"


def test_workspace_root_still_wins_over_child_lockfile(tmp_path: Path) -> None:
    """BUG-933 不回归：根声明 workspaces 时，子包残留 npm 锁仍以根 pnpm 为准。"""
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - frontend\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"private": True, "workspaces": ["frontend"]}), encoding="utf-8"
    )
    app = _write_frontend(tmp_path, lockfile="package-lock.json")
    tool = inspect_node_toolchain(tmp_path, workdir=app)
    assert tool.manager == "pnpm"
    assert tool.workspace_root.resolve() == tmp_path.resolve()


def test_subfolder_without_package_json_uses_root_lockfile(tmp_path: Path) -> None:
    """workdir 只是根项目下的普通子目录（无 package.json）时仍以根为安装根。"""
    (tmp_path / "package.json").write_text(json.dumps({"name": "app"}), encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    tool = inspect_node_toolchain(tmp_path, workdir=src)
    assert tool.workspace_root.resolve() == tmp_path.resolve()
    assert tool.manager == "npm"
    assert tool.install_command == "npm ci"


@pytest.mark.parametrize("declaration", [
    {"workspaces": ["packages/*"]},
    {"workspaces": {"packages": ["packages/*"]}},
    {"workspaces": ["**", "!frontend"]},
])
def test_unrelated_or_excluded_workspace_does_not_own_frontend(
    tmp_path: Path, declaration: dict,
) -> None:
    _write_tooling_root(tmp_path)
    (tmp_path / "package.json").write_text(json.dumps({"private": True, **declaration}))
    app = _write_frontend(tmp_path, lockfile="pnpm-lock.yaml")
    tool = inspect_node_toolchain(tmp_path, workdir=app)
    assert tool.manager == "pnpm"
    assert tool.workspace_root == app.resolve()
    result = Scanner().detect(tmp_path)
    assert result.source_subdir == "frontend"
    assert "pnpm" in (result.entry.install or "")


@pytest.mark.parametrize("patterns, included", [
    (["packages/**"], True),
    (["packages/{group,other}/web"], True),
    (["packages/*"], False),
    (["**", "!packages/group/*"], False),
])
def test_pnpm_workspace_membership_obeys_depth_and_exclusions(
    tmp_path: Path, patterns: list[str], included: bool,
) -> None:
    (tmp_path / "package.json").write_text('{"private":true}')
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    (tmp_path / "pnpm-workspace.yaml").write_text(
        "packages:\n" + "".join(f"  - '{pattern}'\n" for pattern in patterns),
    )
    app = tmp_path / "packages" / "group" / "web"
    app.mkdir(parents=True)
    (app / "package.json").write_text('{"name":"web"}')
    (app / "package-lock.json").write_text('{}')
    tool = inspect_node_toolchain(tmp_path, workdir=app)
    assert tool.manager == ("pnpm" if included else "npm")
    assert tool.workspace_root == (tmp_path if included else app).resolve()


# ---- BUG-937：WSL doctor 缺口端口与 guidance 命令同源 ------------------------


@pytest.fixture
def doctor_env(tmp_path: Path):
    root = tmp_path / "ws"
    init_workspace(root)
    ws = Workspace(root)
    config = load_config(ws)
    reg = Registry(ws.db_path)
    reg.open()
    yield ws, config, reg
    reg.close()


def test_expected_port_follows_instance_without_alias() -> None:
    from local_webpage_access.config import Config
    from tests.scripts.test_wsl_network import _yi14_snapshot

    config = Config()
    snap = _yi14_snapshot()
    view = describe_instance_access(snap, host_port=18000, route_host=None, config=config)
    assert view.expected_port == 18000
    aliased = describe_instance_access(snap, host_port=18000, route_host="demo", config=config)
    assert aliased.expected_port == lan_entry_port(config)


def test_wsl_doctor_missing_entry_names_instance_port_without_alias(
    doctor_env, monkeypatch
) -> None:
    """无路径别名、hostPort=18000：标题缺口端口必须是 18000，不能是网关 8080。"""
    from tests.scripts.test_doctor import _seed_static_for_doctor
    from tests.scripts.test_wsl_network import _yi14_snapshot

    ws, config, reg = doctor_env
    _seed_static_for_doctor(ws, reg, "hello", host_port=18000)
    snap = _yi14_snapshot()
    monkeypatch.setattr(
        "local_webpage_access.wsl_network.collect_wsl_network_snapshot", lambda **_k: snap
    )
    result = check_wsl_lan_exposure(ws, config, reg)
    assert result.status == STATUS_WARN
    assert "缺 :18000 转发" in result.message
    gateway_port = lan_entry_port(config)
    if gateway_port and gateway_port != 18000:
        assert f":{gateway_port}" not in result.message
    assert "18000" in (result.suggestion or "")


def test_wsl_doctor_missing_entry_names_gateway_port_with_alias(doctor_env, monkeypatch) -> None:
    """有路径别名时入口是网关端口，标题与建议同为该端口。"""
    from tests.scripts.test_doctor import _seed_static_for_doctor
    from tests.scripts.test_wsl_network import _yi14_snapshot

    ws, config, reg = doctor_env
    _seed_static_for_doctor(ws, reg, "hello", host_port=18000, alias="hello")
    snap = _yi14_snapshot()
    monkeypatch.setattr(
        "local_webpage_access.wsl_network.collect_wsl_network_snapshot", lambda **_k: snap
    )
    result = check_wsl_lan_exposure(ws, config, reg)
    gateway_port = lan_entry_port(config)
    assert gateway_port
    assert f"缺 :{gateway_port} 转发" in result.message
