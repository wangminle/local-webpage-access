"""IMP-066：Flask/Django 生产服务器模板（唯一 WSGI 入口推断 + 平台 Gunicorn 分层）。"""

from __future__ import annotations

from pathlib import Path

from local_webpage_access.dockerfile_templates import generate_dockerfile
from local_webpage_access.scanner import Scanner, dev_server_fallback_warning
from tests.scripts._helpers import make_container_manifest


def _flask_project(root: Path, *, files: dict[str, str] | None = None) -> None:
    (root / "requirements.txt").write_text("flask==3.0.0\n", encoding="utf-8")
    for name, body in (files or {"app.py": "from flask import Flask\napp = Flask(__name__)\n"}).items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")


def test_flask_unique_app_entry_gets_gunicorn(tmp_path: Path) -> None:
    _flask_project(tmp_path)
    result = Scanner().detect(tmp_path)
    assert result.entry.start == "gunicorn --workers 1 --threads 4 -b 0.0.0.0:5000 app:app"
    assert any("IMP-066" in n for n in result.notes)


def test_flask_factory_entry(tmp_path: Path) -> None:
    _flask_project(tmp_path, files={"app/__init__.py": "from flask import Flask\ndef create_app():\n    return Flask(__name__)\n"})
    result = Scanner().detect(tmp_path)
    # 工厂形态含 ()，preflight 会自动 sh -c 包裹（预期行为）
    assert result.entry.start.startswith("sh -c 'gunicorn")
    assert "app:create_app()" in result.entry.start


def test_flask_multiple_candidates_falls_back_with_warning(tmp_path: Path) -> None:
    _flask_project(
        tmp_path,
        files={
            "app.py": "from flask import Flask\napp = Flask(__name__)\n",
            "wsgi.py": "from flask import Flask\napp = Flask(__name__)\n",
        },
    )
    result = Scanner().detect(tmp_path)
    assert result.entry.start.startswith("flask --app")
    assert any(n.startswith("[预检警告]") and "IMP-066" in n for n in result.notes)
    assert dev_server_fallback_warning(result.entry.start)


def test_flask_declared_gunicorn_wins_without_inference(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text(
        "flask==3.0.0\ngunicorn==21.2.0\n", encoding="utf-8"
    )
    (tmp_path / "random.py").write_text("x = 1\n", encoding="utf-8")
    result = Scanner().detect(tmp_path)
    assert result.entry.start.startswith("gunicorn -b 0.0.0.0:5000")


def test_django_unique_wsgi_gets_gunicorn(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("django==5.0\n", encoding="utf-8")
    (tmp_path / "manage.py").write_text("# manage\n", encoding="utf-8")
    proj = tmp_path / "mysite"
    proj.mkdir()
    (proj / "wsgi.py").write_text(
        "import os\nfrom django.core.wsgi import get_wsgi_application\napplication = get_wsgi_application()\n",
        encoding="utf-8",
    )
    result = Scanner().detect(tmp_path)
    assert result.entry.start == (
        "gunicorn --workers 1 --threads 4 -b 0.0.0.0:8000 mysite.wsgi:application"
    )


def test_django_multiple_wsgi_falls_back(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("django==5.0\n", encoding="utf-8")
    (tmp_path / "manage.py").write_text("# manage\n", encoding="utf-8")
    for pkg in ("site_a", "site_b"):
        d = tmp_path / pkg
        d.mkdir()
        (d / "wsgi.py").write_text("application = object()\n", encoding="utf-8")
    result = Scanner().detect(tmp_path)
    assert result.entry.start.startswith("python manage.py runserver")
    assert any("多个候选" in n for n in result.notes)


def test_dockerfile_installs_platform_gunicorn_layer(workspace) -> None:
    """066.b/066.03：CMD 为 Gunicorn 且项目未声明时分层补装；已声明则不装。"""
    from local_webpage_access.models import EntryConfig

    manifest = make_container_manifest("demo")
    manifest.entry = EntryConfig(
        install="pip install -r requirements.txt",
        build=None,
        start="gunicorn --workers 1 --threads 4 -b 0.0.0.0:5000 app:app",
    )
    current = workspace.app_current("demo")
    current.mkdir(parents=True, exist_ok=True)
    (current / "requirements.txt").write_text("flask==3.0.0\n", encoding="utf-8")
    (current / "app.py").write_text("from flask import Flask\napp = Flask(__name__)\n", encoding="utf-8")

    out = generate_dockerfile(manifest, workspace)
    text = out.read_text(encoding="utf-8")
    assert 'pip install "gunicorn>=21,<23"' in text
    assert 'CMD ["gunicorn"' in text or "CMD gunicorn" in text
    # 用户依赖文件未被改写
    assert (current / "requirements.txt").read_text(encoding="utf-8") == "flask==3.0.0\n"

    # 项目已声明 gunicorn：尊重项目版本，不再补装
    (current / "requirements.txt").write_text("flask==3.0.0\ngunicorn==21.2.0\n", encoding="utf-8")
    out2 = generate_dockerfile(manifest, workspace)
    assert "gunicorn>=21" not in out2.read_text(encoding="utf-8")


def test_dockerfile_installs_gunicorn_for_factory_sh_c(workspace, tmp_path: Path) -> None:
    """工厂 ``create_app()`` 经预检包成 ``sh -c`` 后，仍要分层安装 Gunicorn。"""
    import shutil

    project = tmp_path / "project"
    project.mkdir()
    _flask_project(
        project,
        files={
            "app/__init__.py": (
                "from flask import Flask\n"
                "def create_app():\n"
                "    return Flask(__name__)\n"
            )
        },
    )
    result = Scanner().detect(project)
    assert result.entry.start is not None
    assert result.entry.start.startswith("sh -c '")
    assert "app:create_app()" in result.entry.start

    manifest = make_container_manifest("demo")
    manifest.entry = result.entry
    current = workspace.app_current("demo")
    shutil.copytree(project, current, dirs_exist_ok=True)

    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert 'gunicorn>=21,<23' in text
    assert "app:create_app()" in text


def test_uv_prefixed_sh_c_gunicorn_still_installs(workspace) -> None:
    """uv 平台补装与项目安装共用镜像回退链、超时和缓存。"""
    from local_webpage_access.config import BuildMirrors, Config
    from local_webpage_access.models import EntryConfig

    manifest = make_container_manifest("demo")
    manifest.entry = EntryConfig(
        install="uv sync --frozen",
        build=None,
        start="sh -c 'gunicorn --workers 1 --threads 4 -b 0.0.0.0:5000 app:create_app()'",
    )
    current = workspace.app_current("demo")
    current.mkdir(parents=True, exist_ok=True)
    (current / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (current / "uv.lock").write_text("", encoding="utf-8")
    config = Config(
        buildMirrors=BuildMirrors(
            pip="https://primary.example/simple",
            pipFallbacks=["https://fallback.example/simple"],
            pipTimeout=17,
        )
    )

    text = generate_dockerfile(manifest, workspace, config=config).read_text(encoding="utf-8")
    install = next(line for line in text.splitlines() if 'uv pip install "gunicorn' in line)
    primary = "UV_HTTP_TIMEOUT=17 UV_DEFAULT_INDEX=https://primary.example/simple"
    fallback = "UV_HTTP_TIMEOUT=17 UV_DEFAULT_INDEX=https://fallback.example/simple"
    assert primary in install and fallback in install
    assert install.index(primary) < install.index(fallback)
    assert "target=/root/.cache/uv" in text


def test_pyproject_prose_does_not_suppress_platform_gunicorn(workspace) -> None:
    """描述或注释里出现 gunicorn 一词，不算项目已声明依赖。"""
    from local_webpage_access.models import EntryConfig

    manifest = make_container_manifest("demo")
    manifest.entry = EntryConfig(
        install="pip install -r requirements.txt",
        build=None,
        start="gunicorn --workers 1 --threads 4 -b 0.0.0.0:5000 app:app",
    )
    current = workspace.app_current("demo")
    current.mkdir(parents=True, exist_ok=True)
    (current / "requirements.txt").write_text("flask==3.0.0\n", encoding="utf-8")
    (current / "pyproject.toml").write_text(
        '[project]\nname = "demo"\n'
        'description = "A service without gunicorn"\n',
        encoding="utf-8",
    )

    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert 'gunicorn>=21,<23' in text


def test_pyproject_dependency_declarations_skip_platform_gunicorn(workspace) -> None:
    """PEP 621 引号依赖与 Poetry 表项都视为项目已声明，不重复安装。"""
    from local_webpage_access.models import EntryConfig

    manifest = make_container_manifest("demo")
    manifest.entry = EntryConfig(
        install="pip install -r requirements.txt",
        build=None,
        start="gunicorn --workers 1 --threads 4 -b 0.0.0.0:5000 app:app",
    )
    current = workspace.app_current("demo")
    current.mkdir(parents=True, exist_ok=True)
    (current / "requirements.txt").write_text("flask==3.0.0\n", encoding="utf-8")

    (current / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndependencies = ["gunicorn>=21"]\n',
        encoding="utf-8",
    )
    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert "gunicorn>=21,<23" not in text

    (current / "pyproject.toml").write_text(
        '[tool.poetry.dependencies]\npython = "^3.11"\ngunicorn = "^21.2.0"\n',
        encoding="utf-8",
    )
    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert "gunicorn>=21,<23" not in text


def test_dot_slash_manage_py_is_dev_server() -> None:
    """相对路径 ``./manage.py runserver`` 与裸 ``manage.py`` 一样算开发服务器。"""
    assert dev_server_fallback_warning("./manage.py runserver 0.0.0.0:8000")
    assert dev_server_fallback_warning("python manage.py runserver 0.0.0.0:8000")
    assert dev_server_fallback_warning(
        "gunicorn --workers 1 --threads 4 -b 0.0.0.0:8000 app:app"
    ) is None
