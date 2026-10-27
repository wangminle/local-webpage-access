"""BUG-997：Django 前端构建、collectstatic 与生产静态托管的自动配方。

覆盖：
- 契约完整（manage.py + 根 package.json build + npm 锁 + 可达 STATIC_ROOT +
  单一 wsgi.py）时 Dockerfile 的构建顺序与 CMD 重写；
- 契约不全（pnpm/yarn、缺锁、STATIC_ROOT 不可达）时只留可操作注释，不盲目执行；
- 生成的 WSGI 包装确实能读 STATIC_ROOT，并拒绝穿越与符号链接逃逸。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from local_webpage_access.dockerfile_templates import generate_dockerfile
from local_webpage_access.models import (
    ContainerConfig,
    DesiredState,
    EntryConfig,
    InstanceManifest,
    Kind,
    ResourceProfile,
    Runtime,
    ServingMode,
    Status,
)
from local_webpage_access.paths import Workspace


def _manifest(
    *,
    mid: str = "linkding",
    start: str = (
        'sh -c "python manage.py migrate --noinput && '
        "exec gunicorn --workers 1 --threads 4 -b 0.0.0.0:8000 "
        'mysite.wsgi:application"'
    ),
    source_subdir: str | None = None,
) -> InstanceManifest:
    return InstanceManifest(
        id=mid,
        name=mid,
        version="1",
        kind=Kind.PYTHON,
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.SMALL,
        container=ContainerConfig(
            projectName=f"lwa-{mid}",
            internalPort=8000,
            composePath="docker/compose.yaml",
            dockerfilePath="docker/Dockerfile",
        ),
        entry=EntryConfig(install="pip install -r requirements.txt", start=start),
        desiredState=DesiredState.STOPPED,
        status=Status.PENDING,
        sourceSubdir=source_subdir,
    )


def _write_tree(
    root: Path,
    *,
    lock: str | None = "package-lock.json",
    settings_files: dict[str, str] | None = None,
    build_script: str | None = "esbuild app.js --outdir=mysite/static",
    wsgi_modules: tuple[str, ...] = ("mysite",),
    with_static_root: bool = True,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "manage.py").write_text(
        'import os\nos.environ.setdefault("DJANGO_SETTINGS_MODULE", "mysite.settings")\n',
        encoding="utf-8",
    )
    pkg: dict = {"name": "demo", "scripts": {}}
    if build_script is not None:
        pkg["scripts"]["build"] = build_script
    (root / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    if lock is not None:
        (root / lock).write_text("{}", encoding="utf-8")

    settings = settings_files
    if settings is None:
        base = "STATIC_ROOT = BASE_DIR / 'static'\n" if with_static_root else ""
        settings = {
            "__init__.py": "from .base import *\n",
            "base.py": "STATIC_URL = '/static/'\n" + base,
        }
    settings_dir = root / "mysite" / "settings"
    settings_dir.mkdir(parents=True, exist_ok=True)
    for name, text in settings.items():
        (settings_dir / name).write_text(text, encoding="utf-8")

    (root / "mysite" / "__init__.py").write_text("", encoding="utf-8")
    for module in wsgi_modules:
        pkg_dir = root.joinpath(*module.split("."))
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
        (pkg_dir / "wsgi.py").write_text(
            "def _application(environ, start_response):\n"
            "    start_response('404 Not Found', [('Content-Type', 'text/plain')])\n"
            "    return [b'app-fallback']\n"
            "application = _application\n",
            encoding="utf-8",
        )
    (root / "mysite" / "static").mkdir(parents=True, exist_ok=True)
    (root / "mysite" / "static" / "favicon.svg").write_text("<svg/>", encoding="utf-8")


def _app_root(workspace: Workspace, mid: str = "linkding") -> Path:
    root = workspace.app_current(mid)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _dockerfile(workspace: Workspace, manifest: InstanceManifest | None = None) -> str:
    m = manifest or _manifest()
    return generate_dockerfile(m, workspace).read_text(encoding="utf-8")


def _cmd_argv(content: str) -> list[str]:
    cmd = next(line for line in content.splitlines() if line.startswith("CMD "))
    return json.loads(cmd[len("CMD ") :])


# ---- Dockerfile 配方 ---------------------------------------------------------


def test_recipe_builds_collects_and_serves_static(workspace: Workspace) -> None:
    _write_tree(_app_root(workspace))
    m = _manifest()
    content = _dockerfile(workspace, m)

    assert "RUN npm ci --omit=dev" not in content
    assert "RUN npm ci" in content
    assert "# BUG-997：Django 静态资源配方" in content
    assert "RUN npm run build" in content
    assert (
        "RUN DJANGO_SETTINGS_MODULE=mysite.settings python manage.py "
        "collectstatic --noinput" in content
    )
    assert "COPY docker/lwa_static_wsgi.py /app/lwa_static_wsgi.py" in content
    assert "RUN npm prune --omit=dev --no-audit --no-fund" in content

    order = [
        content.index("RUN npm ci"),
        content.index("RUN npm run build"),
        content.index("python manage.py collectstatic --noinput"),
        content.index("COPY docker/lwa_static_wsgi.py"),
        content.index("RUN npm prune --omit=dev --no-audit --no-fund"),
        content.index("CMD "),
    ]
    assert order == sorted(order)

    helper = workspace.app_docker("linkding") / "lwa_static_wsgi.py"
    assert helper.is_file()
    assert '_APP_MODULE = "mysite.wsgi"' in helper.read_text(encoding="utf-8")


def test_cmd_rewrites_gunicorn_target_without_eating_quotes(
    workspace: Workspace,
) -> None:
    _write_tree(_app_root(workspace))
    content = _dockerfile(workspace)

    argv = _cmd_argv(content)
    assert argv[:2] == ["sh", "-c"]
    command = argv[2]
    assert "lwa_static_wsgi:application" in command
    assert "mysite.wsgi:application" not in command
    assert command.startswith("python manage.py migrate")


def test_nested_sh_c_keeps_closing_quote_when_target_rewritten(
    workspace: Workspace,
) -> None:
    """BUG-997 回归：``uv run … sh -c "… module:application"`` 的收尾引号
    曾被重写正则当作目标引号一起吞掉，生成的 CMD 在 Dockerfile 中被截断。"""
    _write_tree(_app_root(workspace))
    start = (
        'uv run --no-sync --no-dev sh -c "python manage.py migrate --noinput && '
        "exec gunicorn --workers 1 --threads 4 -b 0.0.0.0:8000 "
        'mysite.wsgi:application"'
    )
    content = _dockerfile(workspace, _manifest(start=start))
    argv = _cmd_argv(content)

    assert argv[:2] == ["sh", "-c"]
    command = argv[2]
    assert 'sh -c "python manage.py migrate' in command
    assert command.endswith('lwa_static_wsgi:application"')
    assert command.count('"') % 2 == 0


def test_recipe_follows_source_subdir(workspace: Workspace) -> None:
    root = _app_root(workspace) / "backend"
    _write_tree(root)
    m = _manifest(source_subdir="backend")
    content = _dockerfile(workspace, m)

    assert "COPY current/backend/package*.json ./" in content
    assert "COPY current/backend/ ./" in content
    assert "RUN npm run build" in content
    assert "python manage.py collectstatic --noinput" in content
    helper = workspace.app_docker("linkding") / "lwa_static_wsgi.py"
    assert helper.is_file()


def test_no_wsgi_target_in_start_still_builds_but_does_not_wrap(
    workspace: Workspace,
) -> None:
    _write_tree(_app_root(workspace))
    m = _manifest(start="python server.py")
    content = _dockerfile(workspace, m)

    assert "RUN npm run build" in content
    assert "python manage.py collectstatic --noinput" in content
    assert "COPY docker/lwa_static_wsgi.py" not in content
    assert not (workspace.app_docker("linkding") / "lwa_static_wsgi.py").exists()
    assert _cmd_argv(content)[-2:] == ["python", "server.py"]


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"lock": "pnpm-lock.yaml"}, "pnpm/yarn"),
        ({"lock": "yarn.lock"}, "pnpm/yarn"),
        ({"lock": None}, "缺少 package-lock.json"),
    ],
)
def test_non_npm_or_missing_lock_only_leaves_note(
    workspace: Workspace, kwargs: dict, reason: str
) -> None:
    _write_tree(_app_root(workspace), **kwargs)
    content = _dockerfile(workspace)

    assert "# BUG-997：Django 静态资源自动配方跳过" in content
    assert reason in content
    assert "RUN npm run build" not in content
    assert "collectstatic" not in content
    assert "lwa_static_wsgi" not in content
    assert not (workspace.app_docker("linkding") / "lwa_static_wsgi.py").exists()


def test_static_root_must_be_reachable_from_selected_settings(
    workspace: Workspace,
) -> None:
    # prod.py 声明 STATIC_ROOT，但 settings/__init__.py 只导入 base（base 没有），
    # 选中的 dev/base 链实际没有 STATIC_ROOT，collectstatic 会失败 → 不应自动启用。
    _write_tree(
        _app_root(workspace),
        settings_files={
            "__init__.py": "from .base import *\n",
            "base.py": "DEBUG = True\n",
            "prod.py": "STATIC_ROOT = '/tmp/static'\n",
        },
    )
    content = _dockerfile(workspace)

    assert "Django 静态资源自动配方跳过" in content
    assert "未声明 STATIC_ROOT" in content
    assert "collectstatic" not in content


def test_static_root_reached_through_star_import_chain_is_accepted(
    workspace: Workspace,
) -> None:
    _write_tree(
        _app_root(workspace),
        settings_files={
            "__init__.py": "from .dev import *\n",
            "dev.py": "from .base import *\nDEBUG = True\n",
            "base.py": "STATIC_ROOT = BASE_DIR / 'static'\n",
            "prod.py": "STATIC_ROOT = '/tmp/unused'\n",
        },
    )
    content = _dockerfile(workspace)

    assert "Django 静态资源自动配方跳过" not in content
    assert "DJANGO_SETTINGS_MODULE=mysite.settings python manage.py collectstatic" in content


def test_no_build_script_stays_silent(workspace: Workspace) -> None:
    _write_tree(_app_root(workspace), build_script=None)
    content = _dockerfile(workspace)

    assert "BUG-997" not in content
    assert "collectstatic" not in content


def test_ambiguous_wsgi_candidates_do_not_guess(workspace: Workspace) -> None:
    """BUG-1002：WSGI 入口歧义时整体跳过——npm build / collectstatic 也不执行。"""
    _write_tree(_app_root(workspace), wsgi_modules=("mysite", "other"))
    content = _dockerfile(workspace)

    # 交付约定「唯一 wsgi 才启用」：歧义时不跑本应保守跳过的构建，也不留
    # 无静态托管的半成品，只留可操作注释交 buildHooks 显式处理。
    assert "RUN npm run build" not in content
    assert "manage.py collectstatic" not in content
    assert "整体跳过" in content
    assert "COPY docker/lwa_static_wsgi.py" not in content
    assert "lwa_static_wsgi:application" not in content
    assert not (workspace.app_docker("linkding") / "lwa_static_wsgi.py").exists()


def test_helper_is_removed_when_recipe_no_longer_applies(
    workspace: Workspace,
) -> None:
    root = _app_root(workspace)
    _write_tree(root)
    generate_dockerfile(_manifest(), workspace)
    helper = workspace.app_docker("linkding") / "lwa_static_wsgi.py"
    assert helper.is_file()

    (root / "mysite" / "settings" / "base.py").write_text(
        "DEBUG = True\n", encoding="utf-8"
    )
    content = _dockerfile(workspace)
    assert "collectstatic" not in content
    assert not helper.exists()


# ---- 生成的 WSGI 包装运行时 --------------------------------------------------


class _Settings:
    def __init__(self, static_root: Path) -> None:
        self.STATIC_ROOT = str(static_root)
        self.STATIC_URL = "/static/"


def _load_helper(
    monkeypatch: pytest.MonkeyPatch,
    helper: Path,
    tmp: Path,
    static_root_value: object = ...,
) -> ModuleType:
    """在隔离的 sys.path 上加载生成的 helper，用桩 django.conf.settings 代替 Django。

    ``static_root_value`` 显式指定 settings.STATIC_ROOT（BUG-1001 空值/相对路径
    场景）；缺省用桩收集目录。
    """
    static_root = tmp / "collected"
    static_root.mkdir()
    (static_root / "theme-dark.css").write_text("body{color:#000}", encoding="utf-8")
    (static_root / "favicon.svg").write_text("<svg/>", encoding="utf-8")

    effective = (
        str(static_root) if static_root_value is ... else static_root_value
    )
    django_pkg = tmp / "stub" / "django"
    django_pkg.mkdir(parents=True)
    (django_pkg / "__init__.py").write_text("", encoding="utf-8")
    (django_pkg / "conf.py").write_text(
        "from types import SimpleNamespace\n"
        f"settings = SimpleNamespace(STATIC_ROOT={effective!r}, "
        "STATIC_URL='/static/')\n",
        encoding="utf-8",
    )

    app_pkg = tmp / "stub" / "mysite"
    app_pkg.mkdir(parents=True)
    (app_pkg / "__init__.py").write_text("", encoding="utf-8")
    (app_pkg / "wsgi.py").write_text(
        "def application(environ, start_response):\n"
        "    start_response('404 Not Found', [('Content-Type', 'text/plain')])\n"
        "    return [b'app-fallback']\n",
        encoding="utf-8",
    )

    monkeypatch.syspath_prepend(str(tmp / "stub"))
    for name in ("django", "django.conf", "mysite", "mysite.wsgi", "lwa_static_wsgi"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    spec = importlib.util.spec_from_file_location("lwa_static_wsgi", helper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["lwa_static_wsgi"] = module
    spec.loader.exec_module(module)
    return module


def _get(module: ModuleType, path: str, method: str = "GET") -> tuple[str, list, bytes]:
    captured: dict = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = headers

    body = b"".join(
        module.application({"REQUEST_METHOD": method, "PATH_INFO": path}, start_response)
    )
    return captured["status"], captured["headers"], body


def test_generated_wsgi_serves_collected_static(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_tree(_app_root(workspace))
    generate_dockerfile(_manifest(), workspace)
    helper = workspace.app_docker("linkding") / "lwa_static_wsgi.py"
    module = _load_helper(monkeypatch, helper, tmp_path)

    status, headers, body = _get(module, "/static/theme-dark.css")
    assert status == "200 OK"
    assert dict(headers)["Content-Type"] == "text/css"
    assert body == b"body{color:#000}"

    status, headers, body = _get(module, "/static/favicon.svg")
    assert status == "200 OK"
    assert dict(headers)["Content-Type"] == "image/svg+xml"
    assert body == b"<svg/>"

    status, headers, body = _get(module, "/static/theme-dark.css", method="HEAD")
    assert status == "200 OK"
    assert dict(headers)["Content-Length"] == str(len(b"body{color:#000}"))
    assert body == b""


def test_generated_wsgi_rejects_traversal_and_symlink_escape(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_tree(_app_root(workspace))
    generate_dockerfile(_manifest(), workspace)
    helper = workspace.app_docker("linkding") / "lwa_static_wsgi.py"
    module = _load_helper(monkeypatch, helper, tmp_path)

    static_root = tmp_path / "collected"
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET", encoding="utf-8")
    (static_root / "escape.css").symlink_to(secret)

    for path in (
        "/static/../secret.txt",
        "/static/%2e%2e/secret.txt",
        "/static/escape.css",
        "/static/",
        "/static",
        "/other/theme-dark.css",
    ):
        status, _headers, body = _get(module, path)
        assert status == "404 Not Found", path
        assert body == b"app-fallback", path

    status, _headers, body = _get(module, "/static/theme-dark.css", method="POST")
    assert status == "404 Not Found"
    assert body == b"app-fallback"

# ---- BUG-1001：空 / 相对 STATIC_ROOT 不得退化成工作目录 -----------------------


@pytest.mark.parametrize("static_root_value", [None, "", "   ", "static", "./collected"])
def test_generated_wsgi_disables_static_when_root_empty_or_relative(
    workspace: Workspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    static_root_value: object,
) -> None:
    """BUG-1001：``os.path.realpath("")`` 会变成工作目录，/static/ 不得因此读到应用文件。

    独立核查复现：STATIC_ROOT 为空、工作目录放一个虚构私有文件，请求
    ``/static/private-config.txt`` 返回 200 与文件内容。修复后原始值非空且为
    绝对路径才启用静态托管，否则一律交回原 WSGI 应用。
    """
    _write_tree(_app_root(workspace))
    generate_dockerfile(_manifest(), workspace)
    helper = workspace.app_docker("linkding") / "lwa_static_wsgi.py"

    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "private-config.txt").write_text("SECRET_KEY=do-not-leak", encoding="utf-8")
    (cwd / "collected").mkdir()
    (cwd / "collected" / "leak.css").write_text("x", encoding="utf-8")
    monkeypatch.chdir(cwd)

    module = _load_helper(monkeypatch, helper, tmp_path, static_root_value=static_root_value)
    assert module._STATIC_ROOT == ""

    for path in ("/static/private-config.txt", "/static/collected/leak.css", "/static/leak.css"):
        status, _headers, body = _get(module, path)
        assert status == "404 Not Found", path
        assert body == b"app-fallback", path


def test_generated_wsgi_absolute_root_still_serves(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BUG-1001 对照：绝对路径 STATIC_ROOT 的正常托管不受影响。"""
    _write_tree(_app_root(workspace))
    generate_dockerfile(_manifest(), workspace)
    helper = workspace.app_docker("linkding") / "lwa_static_wsgi.py"
    module = _load_helper(monkeypatch, helper, tmp_path)
    assert module._STATIC_ROOT == str((tmp_path / "collected").resolve())
    status, _headers, body = _get(module, "/static/favicon.svg")
    assert status == "200 OK" and body == b"<svg/>"


# ---- BUG-997（复核重开）：uv 项目的 collectstatic 必须进项目 venv --------------


def test_uv_project_collectstatic_runs_inside_project_venv(workspace: Workspace) -> None:
    """uv 项目依赖装在 venv，系统 python 没有 Django；collectstatic 须经 uv run。"""
    root = _app_root(workspace)
    _write_tree(root)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0"\ndependencies = ["django"]\n',
        encoding="utf-8",
    )
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    manifest = _manifest()
    manifest.entry.install = "uv sync --frozen --no-dev"

    content = _dockerfile(workspace, manifest)

    collect = next(line for line in content.splitlines() if "collectstatic" in line)
    assert collect == (
        "RUN DJANGO_SETTINGS_MODULE=mysite.settings "
        "uv run --no-sync --no-dev python manage.py collectstatic --noinput"
    )
    # collectstatic 层必须排在项目本体 sync（uv sync --frozen --no-dev）之后，
    # 否则 --no-sync 进入的 venv 里还没有项目包。
    lines = content.splitlines()
    sync_idx = max(i for i, line in enumerate(lines) if "uv sync --frozen --no-dev" in line)
    collect_idx = lines.index(collect)
    assert sync_idx < collect_idx


def test_pip_project_collectstatic_keeps_system_python(workspace: Workspace) -> None:
    """pip 形态依赖装在系统解释器，collectstatic 不加 uv 前缀。"""
    _write_tree(_app_root(workspace))
    content = _dockerfile(workspace)
    collect = next(line for line in content.splitlines() if "collectstatic" in line)
    assert collect == (
        "RUN DJANGO_SETTINGS_MODULE=mysite.settings python manage.py collectstatic --noinput"
    )
    assert "uv run" not in collect
