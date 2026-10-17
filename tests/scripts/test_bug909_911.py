"""BUG-909～911：迁移包装漏装平台 Gunicorn、stop 清 pending 绕过启动门禁、
工厂引号兼容破坏合法 shell 函数。"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from local_webpage_access.dockerfile_templates import generate_dockerfile
from local_webpage_access.errors import LifecycleError
from local_webpage_access.lifecycle import start_instance, stop_instance_op
from local_webpage_access.models import InstanceManifest, Kind, Status
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry
from local_webpage_access.scanner import Scanner

from tests.scripts._helpers import make_container_manifest, make_static_manifest
from tests.scripts.test_dockerfile_templates import _mk_manifest


def _sh_n(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", "-n", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )


def _cmd_script(text: str) -> str:
    cmd_line = next(line for line in text.splitlines() if line.startswith("CMD "))
    cmd = json.loads(cmd_line[len("CMD ") :])
    assert cmd[0] == "sh" and cmd[1] == "-c"
    return cmd[2]


def _write_django_with_migrations(root: Path) -> None:
    """Django 项目：未声明 gunicorn，且存在 migrations 包（触发 BUG-907 迁移包装）。"""
    (root / "requirements.txt").write_text("django\n", encoding="utf-8")
    (root / "manage.py").write_text("import os\n", encoding="utf-8")
    project = root / "mysite"
    project.mkdir()
    (project / "__init__.py").write_text("", encoding="utf-8")
    (project / "wsgi.py").write_text(
        "import os\napplication = os.environ.get('WSGI')\n",
        encoding="utf-8",
    )
    mig = root / "bookmarks" / "migrations"
    mig.mkdir(parents=True)
    (mig / "__init__.py").write_text("", encoding="utf-8")


def _migrate_wrapped_start(tmp_path: Path) -> str:
    """Scanner→Dockerfile 组合基线：返回迁移包装的启动命令。"""
    project = tmp_path / "project"
    project.mkdir()
    _write_django_with_migrations(project)
    result = Scanner().detect(project)
    start = result.entry.start or ""
    assert start.startswith('sh -c "python manage.py migrate --noinput && exec gunicorn'), start
    return start


# ---- BUG-909：迁移包装后平台不得漏装 Gunicorn --------------------------------


def test_migrate_wrapped_gunicorn_gets_platform_install_pip(
    workspace: Workspace, tmp_path: Path
) -> None:
    """BUG-909：pip 路径——迁移包装的启动命令首段是 python manage.py，
    平台依赖识别必须看 exec 后的实际命令，Django 项目未声明 gunicorn
    时仍要补装平台 Gunicorn。"""
    start = _migrate_wrapped_start(tmp_path)
    manifest = _mk_manifest(
        install="pip install -r requirements.txt",
        start=start,
    )
    shutil.copytree(tmp_path / "project", workspace.app_current("api"), dirs_exist_ok=True)
    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert 'pip install "gunicorn' in text
    # 迁移包装本身不受本修复影响：CMD 仍含 migrate 链且 sh -n 通过。
    proc = _sh_n(_cmd_script(text))
    assert proc.returncode == 0, proc.stderr
    assert "python manage.py migrate --noinput" in text


def test_migrate_wrapped_gunicorn_gets_platform_install_uv(
    workspace: Workspace, tmp_path: Path
) -> None:
    """BUG-909：uv 路径——uv run + sh -c + 迁移包装三层叠加时同样补装。"""
    project = tmp_path / "project"
    project.mkdir()
    _write_django_with_migrations(project)
    start = "uv run sh -c " + json.dumps(
        "python manage.py migrate --noinput && exec gunicorn -b 0.0.0.0:8000 mysite.wsgi:application"
    )
    manifest = _mk_manifest(install="uv sync", start=start)
    shutil.copytree(project, workspace.app_current("api"), dirs_exist_ok=True)
    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert 'uv pip install "gunicorn' in text


def test_exec_wrapped_non_gunicorn_still_no_platform_install(workspace: Workspace) -> None:
    """反向验证：exec 包装里最终命令不是 gunicorn 时不补装。"""
    manifest = _mk_manifest(
        install="pip install -r requirements.txt",
        start='sh -c "alembic upgrade head && exec uvicorn main:app --host 0.0.0.0 --port 8000"',
    )
    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert 'pip install "gunicorn' not in text


# ---- BUG-910：stop 不得清除 pending ------------------------------------------


def test_pending_static_stop_preserves_pending_gate(
    workspace: Workspace, registry: Registry, config
) -> None:
    """BUG-910：pending→stop→start 不得进入静态托管路径（BUG-908 绕过）。"""
    manifest = make_static_manifest("demo")
    manifest.save(workspace.app_manifest_path("demo"))
    registry.upsert_from_manifest(manifest)
    workspace.ensure_app_dirs("demo")
    (workspace.app_current("demo") / "index.html").write_text("<html>hi</html>")

    with pytest.raises(LifecycleError, match="pending"):
        start_instance(workspace, config, registry, "demo")

    stopped = stop_instance_op(workspace, config, registry, "demo")
    assert stopped.status == Status.PENDING
    reloaded = InstanceManifest.load(workspace.app_manifest_path("demo"))
    assert reloaded.status == Status.PENDING

    with pytest.raises(LifecycleError, match="pending"):
        start_instance(workspace, config, registry, "demo")
    assert registry.get_instance("demo")["status"] == Status.PENDING.value


def test_pending_container_stop_preserves_pending(
    workspace: Workspace, registry: Registry, config, monkeypatch
) -> None:
    """BUG-910：pending 容器实例 stop 保留 pending，且不调用 docker。"""

    class _NoDocker:
        calls: list[str] = []

        def __init__(self, *args, **kwargs) -> None:
            _NoDocker.calls.append("init")

        def stop(self, *args, **kwargs) -> None:
            _NoDocker.calls.append("stop")

    monkeypatch.setattr("local_webpage_access.hosting.DockerRuntime", _NoDocker)
    manifest = make_container_manifest("api")
    manifest.status = Status.PENDING
    manifest.save(workspace.app_manifest_path("api"))
    registry.upsert_from_manifest(manifest)
    workspace.ensure_app_dirs("api")

    stopped = stop_instance_op(workspace, config, registry, "api")
    assert stopped.status == Status.PENDING
    assert _NoDocker.calls == []
    reloaded = InstanceManifest.load(workspace.app_manifest_path("api"))
    assert reloaded.status == Status.PENDING


def test_confirmed_static_stop_still_marks_stopped(
    workspace: Workspace, registry: Registry, config
) -> None:
    """反向验证：已确认（stopped）实例的 stop 语义不变。"""
    manifest = make_static_manifest("demo", status=Status.STOPPED)
    manifest.save(workspace.app_manifest_path("demo"))
    registry.upsert_from_manifest(manifest)
    workspace.ensure_app_dirs("demo")
    (workspace.app_current("demo") / "index.html").write_text("<html>hi</html>")

    stopped = stop_instance_op(workspace, config, registry, "demo")
    assert stopped.status == Status.STOPPED


# ---- BUG-911：工厂引号不得破坏 shell 函数 ------------------------------------


def test_shell_function_start_script_survives_factory_quoting(
    workspace: Workspace,
) -> None:
    """BUG-911：合法 shell 函数定义 ``start() { ... }`` 不得被当成
    工厂目标加引号——渲染后脚本必须仍通过 sh -n。"""
    script = "start() { exec node server.js; }; start"
    manifest = _mk_manifest(kind=Kind.NODE, install="npm ci", start=script)
    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    inner = _cmd_script(text)
    assert inner == script  # 逐字保留（无 "start()" 引号化）
    proc = _sh_n(inner)
    assert proc.returncode == 0, proc.stderr


def test_gunicorn_factory_target_still_quoted(workspace: Workspace) -> None:
    """反向验证：带冒号的 Gunicorn 工厂目标 module:factory() 仍补引号。"""
    manifest = _mk_manifest(
        install="pip install -r requirements.txt",
        start="sh -c 'gunicorn -b 0.0.0.0:8000 flaskr:create_app()'",
    )
    text = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    inner = _cmd_script(text)
    assert '"flaskr:create_app()"' in inner
    proc = _sh_n(inner)
    assert proc.returncode == 0, proc.stderr
