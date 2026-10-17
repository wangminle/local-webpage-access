"""BUG-903～908：uv 补装 Gunicorn、工厂引号、Flask instance 挂载、
Node mkdir、Django migrate、pending 启动门禁。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

from local_webpage_access.compose import generate_compose
from local_webpage_access.dockerfile_templates import generate_dockerfile
from local_webpage_access.models import Kind
from local_webpage_access.paths import Workspace
from local_webpage_access.scanner import Scanner

from tests.scripts.test_dockerfile_templates import _mk_manifest
from tests.scripts.test_compose import _mk_manifest as _compose_manifest


def _inner_sh_c(start: str) -> str:
    assert start.startswith("sh -c '") and start.endswith("'"), start
    return start[len("sh -c '") : -1]


def _sh_n(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", "-n", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )


def test_flask_factory_inner_script_passes_shell_syntax(tmp_path: Path) -> None:
    """BUG-904：工厂目标的括号必须在 sh -c 内层被引号包住，sh -n 不得报错。"""
    pkg = tmp_path / "flaskr"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "from flask import Flask\n\ndef create_app():\n    return Flask(__name__)\n",
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("flask\n", encoding="utf-8")
    result = Scanner().detect(tmp_path)
    start = result.entry.start or ""
    inner = _inner_sh_c(start)
    proc = _sh_n(inner)
    assert proc.returncode == 0, proc.stderr
    assert '"flaskr:create_app()"' in inner


def test_flask_factory_dockerfile_cmd_script_passes_shell_syntax(
    workspace: Workspace, tmp_path: Path
) -> None:
    """BUG-904：Dockerfile CMD 交给 sh -c 的脚本同样要通过 sh -n。"""
    import shutil

    project = tmp_path / "project"
    project.mkdir()
    pkg = project / "flaskr"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "from flask import Flask\n\ndef create_app():\n    return Flask(__name__)\n",
        encoding="utf-8",
    )
    (project / "requirements.txt").write_text("flask\n", encoding="utf-8")
    result = Scanner().detect(project)
    manifest = _mk_manifest(start=result.entry.start, install="pip install -r requirements.txt")
    shutil.copytree(project, workspace.app_current("api"), dirs_exist_ok=True)
    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    cmd_line = next(line for line in text.splitlines() if line.startswith("CMD "))
    cmd = json.loads(cmd_line[len("CMD ") :])
    assert cmd[0] == "sh" and cmd[1] == "-c"
    proc = _sh_n(cmd[2])
    assert proc.returncode == 0, proc.stderr


def test_stored_unquoted_factory_command_is_quoted_in_dockerfile(workspace: Workspace) -> None:
    """BUG-904：导入时已经写进 manifest 的裸工厂命令，重建 Dockerfile 时也要可执行。"""
    manifest = _mk_manifest(
        install="pip install -r requirements.txt",
        start="sh -c 'gunicorn --workers 1 --threads 4 -b 0.0.0.0:8000 flaskr:create_app()'",
    )
    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    cmd_line = next(line for line in text.splitlines() if line.startswith("CMD "))
    cmd = json.loads(cmd_line[len("CMD ") :])
    proc = _sh_n(cmd[2])
    assert proc.returncode == 0, proc.stderr
    assert '"flaskr:create_app()"' in cmd[2]


def test_uv_platform_gunicorn_survives_project_sync(workspace: Workspace) -> None:
    """BUG-903：完整源码后的 uv sync 会卸掉锁文件未声明的包，
    平台 Gunicorn 必须装在这次同步之后。"""
    manifest = _mk_manifest(
        install="uv sync",
        start="gunicorn --workers 1 --threads 4 -b 0.0.0.0:8000 app:app",
    )
    content = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    copy_idx = content.index("COPY current/ ./")
    project_idx = content.index("uv sync --frozen --no-dev", copy_idx)
    assert "--no-install-project" not in content[project_idx:].splitlines()[0]
    gunicorn_idx = content.index('uv pip install "gunicorn', project_idx)
    assert project_idx < gunicorn_idx


def test_pip_platform_gunicorn_stays_before_source_copy(workspace: Workspace) -> None:
    """非 uv 路径仍把平台 Gunicorn 放在完整源码 COPY 之前，避免源码改动打掉该层。"""
    manifest = _mk_manifest(
        install="pip install -r requirements.txt",
        start="gunicorn --workers 1 --threads 4 -b 0.0.0.0:5000 app:app",
    )
    content = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    gunicorn_idx = content.index('pip install "gunicorn')
    copy_idx = content.index("COPY current/ ./")
    assert gunicorn_idx < copy_idx


def test_node_sqlite_mkdir_before_chown(workspace: Workspace) -> None:
    """BUG-906：非 root 的 Node SQLite 镜像必须先 mkdir /app/data，再 chown。"""
    manifest = _mk_manifest(
        kind=Kind.NODE,
        install="npm ci",
        start="node server.js",
        has_database=True,
        database_type="sqlite",
    )
    assert manifest.container is not None
    manifest.container.runAsNonRoot = True
    content = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    lines = content.splitlines()
    mkdir_idx = next(i for i, line in enumerate(lines) if line == "RUN mkdir -p /app/data")
    chown_idx = next(i for i, line in enumerate(lines) if line.startswith("RUN chown -R "))
    assert mkdir_idx < chown_idx
    assert lines[chown_idx].endswith(" /app/data")


def test_node_without_sqlite_does_not_mkdir_data(workspace: Workspace) -> None:
    manifest = _mk_manifest(kind=Kind.NODE, install="npm ci", start="node server.js")
    content = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert "RUN mkdir -p /app/data" not in content


def _write_flask_instance_app(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    pkg = root / "flaskr"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "import os\n"
        "from flask import Flask\n"
        "def create_app():\n"
        "    app = Flask(__name__, instance_relative_config=True)\n"
        "    app.config['DATABASE'] = os.path.join(app.instance_path, 'flaskr.sqlite')\n"
        "    return app\n",
        encoding="utf-8",
    )


def test_flask_instance_sqlite_is_mounted(workspace: Workspace) -> None:
    """未安装的 Flask 包：instance 与顶层包并列，容器路径是 /app/instance。"""
    _write_flask_instance_app(workspace.app_current("api"))
    manifest = _compose_manifest(has_database=True, database_type="sqlite")
    content = generate_compose(manifest, workspace, host_port=18000).read_text(encoding="utf-8")
    assert "- ../data:/app/data" in content
    assert "- ../data/instance:/app/instance" in content
    assert "/app/flaskr/instance" not in content
    assert (workspace.app_data("api") / "instance").is_dir()


def test_nested_nonpackage_parent_instance_sits_beside_package(workspace: Workspace) -> None:
    """src/ 不是包时，未安装包的 instance 在 src/instance，而不是 src/<pkg>/instance。"""
    pkg = workspace.app_current("api") / "src" / "my_pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        "import os\nfrom flask import Flask\n"
        "def create_app():\n"
        "    app = Flask(__name__)\n"
        "    app.config['DATABASE'] = os.path.join(app.instance_path, 'app.sqlite')\n"
        "    return app\n",
        encoding="utf-8",
    )
    manifest = _compose_manifest(has_database=True, database_type="sqlite")
    content = generate_compose(manifest, workspace, host_port=18000).read_text(encoding="utf-8")
    assert "- ../data/instance:/app/src/instance" in content
    assert "/app/src/my_pkg/instance" not in content


def test_flask_module_instance_mounts_app_instance(workspace: Workspace) -> None:
    """模块文件在复制根上时，Flask instance 就是 /app/instance。"""
    current = workspace.app_current("api")
    current.mkdir(parents=True, exist_ok=True)
    (current / "app.py").write_text(
        "import os\nfrom flask import Flask\n"
        "app = Flask(__name__)\n"
        "app.config['DATABASE'] = os.path.join(app.instance_path, 'app.sqlite')\n",
        encoding="utf-8",
    )
    manifest = _compose_manifest(has_database=True, database_type="sqlite")
    content = generate_compose(manifest, workspace, host_port=18000).read_text(encoding="utf-8")
    assert "- ../data/instance:/app/instance" in content


def test_flask_source_subdir_module_mounts_app_instance(workspace: Workspace) -> None:
    """sourceSubdir 的内容被 COPY 到 /app，子目录里的 app.py 对应 /app/instance。"""
    backend = workspace.app_current("api") / "backend"
    backend.mkdir(parents=True)
    (backend / "app.py").write_text(
        "import os\nfrom flask import Flask\n"
        "app = Flask(__name__)\n"
        "app.config['DATABASE'] = os.path.join(app.instance_path, 'app.sqlite')\n",
        encoding="utf-8",
    )
    manifest = _compose_manifest(has_database=True, database_type="sqlite")
    manifest.sourceSubdir = "backend"
    content = generate_compose(manifest, workspace, host_port=18000).read_text(encoding="utf-8")
    assert "- ../data/instance:/app/instance" in content
    assert "/app/backend/instance" not in content


def test_flask_instance_seed_is_copied_without_clobber(workspace: Workspace) -> None:
    """源码自带的 instance/ 要进宿主挂载目录，已有文件不被覆盖。"""
    current = workspace.app_current("api")
    current.mkdir(parents=True, exist_ok=True)
    (current / "app.py").write_text(
        "import os\nfrom flask import Flask\n"
        "app = Flask(__name__)\n"
        "app.config['DATABASE'] = os.path.join(app.instance_path, 'app.sqlite')\n",
        encoding="utf-8",
    )
    instance = current / "instance"
    instance.mkdir()
    (instance / "config.py").write_text("SECRET = 'seed'\n", encoding="utf-8")
    host = workspace.app_data("api") / "instance"
    host.mkdir(parents=True)
    (host / "config.py").write_text("SECRET = 'user'\n", encoding="utf-8")
    (instance / "extra.txt").write_text("seed-only\n", encoding="utf-8")
    manifest = _compose_manifest(has_database=True, database_type="sqlite")
    generate_compose(manifest, workspace, host_port=18000)
    assert (host / "config.py").read_text(encoding="utf-8") == "SECRET = 'user'\n"
    assert (host / "extra.txt").read_text(encoding="utf-8") == "seed-only\n"


def test_flask_instance_scan_skips_oversized_py(workspace: Workspace) -> None:
    current = workspace.app_current("api")
    current.mkdir(parents=True, exist_ok=True)
    (current / "huge.py").write_text(
        "x = 0\n" + ("# pad\n" * 200_000) + "os.path.join(app.instance_path, 'app.sqlite')\n",
        encoding="utf-8",
    )
    manifest = _compose_manifest(has_database=True, database_type="sqlite")
    content = generate_compose(manifest, workspace, host_port=18000).read_text(encoding="utf-8")
    assert "/app/instance" not in content


def test_flask_without_instance_sqlite_keeps_data_volume_only(workspace: Workspace) -> None:
    current = workspace.app_current("api")
    current.mkdir(parents=True, exist_ok=True)
    (current / "app.py").write_text(
        "from flask import Flask\napp = Flask(__name__)\n",
        encoding="utf-8",
    )
    manifest = _compose_manifest(has_database=True, database_type="sqlite")
    content = generate_compose(manifest, workspace, host_port=18000).read_text(encoding="utf-8")
    assert "- ../data:/app/data" in content
    assert "/app/instance" not in content


def test_rebuild_rescue_fetches_instance_when_data_dir_exists(workspace: Workspace) -> None:
    """BUG-912：渲染已经建出 data/instance 后，重建仍要向旧容器要这份目录。"""
    from local_webpage_access.hosting import _rescue_container_data_before_rebuild

    _write_flask_instance_app(workspace.app_current("api"))
    (workspace.app_data("api") / "instance").mkdir(parents=True)
    manifest = _compose_manifest(has_database=True, database_type="sqlite")
    calls: list[tuple[str, str]] = []

    class _Runtime:
        def rescue_container_data(self, *args, **kwargs):
            return 0

        def rescue_bound_directory(self, instance_id, host_dir, container_path, **kwargs):
            calls.append((str(host_dir), container_path))
            return 1

    _rescue_container_data_before_rebuild(workspace, manifest, "api", _Runtime())
    assert calls == [(str(workspace.app_data("api") / "instance"), "/app/instance")]


def test_uv_prefixed_legacy_factory_sh_c_is_quoted() -> None:
    """BUG-904：uv run 前缀包住存量 sh -c 时，内层工厂括号仍要被引号包住。"""
    from local_webpage_access.dockerfile_templates import _to_exec_form

    cmd = (
        "uv run --no-sync --no-dev sh -c "
        "'gunicorn --workers 1 --threads 4 -b 0.0.0.0:8000 flaskr:create_app()'"
    )
    rendered = json.loads(_to_exec_form(cmd))
    assert rendered[0:2] == ["sh", "-c"]
    proc = _sh_n(rendered[2])
    assert proc.returncode == 0, proc.stderr
    assert '"flaskr:create_app()"' in rendered[2]


def test_to_exec_form_short_sh_c_does_not_raise(monkeypatch) -> None:
    """parts 只有 sh 与 -c 时不得访问 parts[2]。"""
    import local_webpage_access.dockerfile_templates as templates

    monkeypatch.setattr(templates.shlex, "split", lambda _cmd: ["sh", "-c"])
    rendered = templates._to_exec_form("sh -c ()")
    assert "sh" in rendered


def test_prestart_quotes_legacy_factory_commands() -> None:
    """BUG-904：preStart 自己拼 sh -c，存量裸工厂目标和内层 sh -c 都要能通过 sh -n。"""
    from local_webpage_access.dockerfile_templates import _cmd_line

    for start in ("gunicorn flaskr:create_app()", "sh -c 'gunicorn flaskr:create_app()'"):
        manifest = _mk_manifest(start=start)
        manifest.preStart = "echo ready"
        argv = json.loads(_cmd_line(manifest, start))
        assert argv[0:2] == ["sh", "-c"]
        proc = _sh_n(argv[2])
        assert proc.returncode == 0, proc.stderr
        assert '"flaskr:create_app()"' in argv[2]


def test_unmigrated_sqlite_has_no_auth_user() -> None:
    """BUG-907 直接后果：未 migrate 的空库没有 auth_user。"""
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("select 1 from auth_user")
    except sqlite3.OperationalError as exc:
        assert "no such table: auth_user" in str(exc)
    else:
        raise AssertionError("空库不应已有 auth_user")
    finally:
        connection.close()


def _write_django(root: Path, *, migrations: bool) -> None:
    (root / "requirements.txt").write_text("django\n", encoding="utf-8")
    (root / "manage.py").write_text("import os\n", encoding="utf-8")
    project = root / "mysite"
    project.mkdir()
    (project / "__init__.py").write_text("", encoding="utf-8")
    (project / "wsgi.py").write_text(
        "import os\napplication = os.environ.get('WSGI')\n",
        encoding="utf-8",
    )
    if migrations:
        mig = root / "bookmarks" / "migrations"
        mig.mkdir(parents=True)
        (mig / "__init__.py").write_text("", encoding="utf-8")


def test_django_with_migrations_migrates_before_gunicorn(tmp_path: Path) -> None:
    """BUG-907：唯一推断出的 Django WSGI，且存在 migrations 包时，
    沿用 Alembic 的失败即不启动语义。没有 migrations 目录则不猜测。"""
    _write_django(tmp_path, migrations=True)
    result = Scanner().detect(tmp_path)
    start = result.entry.start or ""
    assert start.startswith('sh -c "python manage.py migrate --noinput && exec gunicorn')
    assert "mysite.wsgi:application" in start
    proc = _sh_n(start[len('sh -c "') : -1])
    assert proc.returncode == 0, proc.stderr


def test_django_without_migrations_does_not_auto_migrate(tmp_path: Path) -> None:
    _write_django(tmp_path, migrations=False)
    result = Scanner().detect(tmp_path)
    start = result.entry.start or ""
    assert "migrate" not in start
    assert "gunicorn" in start
