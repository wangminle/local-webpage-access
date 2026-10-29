"""工作区构建代理：配置、管理页、Compose 构建参数与构建子进程环境。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from local_webpage_access.build_proxy import (
    merge_into,
    subprocess_env,
    write_build_proxy,
)
from local_webpage_access.compose import generate_compose
from local_webpage_access.config import BuildProxy, Config, example_config_text, load_config
from local_webpage_access.docker_runtime import ComposeResult, DockerRuntime
from local_webpage_access.dockerfile_templates import generate_dockerfile
from local_webpage_access.errors import ConfigError
from local_webpage_access.manager_api import create_app, ensure_token
from local_webpage_access.models import (
    ContainerConfig,
    EntryConfig,
    InstanceManifest,
    Kind,
    ResourceProfile,
    Runtime,
    ServingMode,
)
from local_webpage_access.paths import Workspace


def _proxy_config(url: str = "http://user:secret@127.0.0.1:10812") -> Config:
    return Config(buildProxy=BuildProxy(enabled=True, url=url))


def _node_manifest() -> InstanceManifest:
    return InstanceManifest(
        id="api",
        name="api",
        version="1",
        kind=Kind.NODE,
        stack=["express"],
        runtime=Runtime.DOCKER_COMPOSE,
        servingMode=ServingMode.CONTAINER,
        resourceProfile=ResourceProfile.SMALL,
        container=ContainerConfig(
            projectName="lwa-api",
            internalPort=3000,
            composePath="docker/compose.yaml",
            dockerfilePath="docker/Dockerfile",
        ),
        entry=EntryConfig(install="npm ci", start="npm run start"),
    )


def test_disabled_proxy_is_a_no_op() -> None:
    config = Config()
    assert config.buildProxy.proxy_env() == {}
    assert config.buildProxy.container_proxy_env() == {}
    assert subprocess_env(config) is None
    assert merge_into({"HTTP_PROXY": "http://ambient"}, config) == {
        "HTTP_PROXY": "http://ambient"
    }


def test_enabled_proxy_requires_a_url() -> None:
    with pytest.raises(ValueError, match="必须填写代理地址"):
        BuildProxy(enabled=True, url="")
    with pytest.raises(ValueError, match="http://"):
        BuildProxy(enabled=True, url="127.0.0.1:10812")


def test_rejects_illegal_or_out_of_range_ports() -> None:
    """BUG-972：非法端口必须在保存前被拒绝，而不是构建期在 redacted_url 炸开。"""
    with pytest.raises(ValueError, match="端口非法"):
        BuildProxy(enabled=True, url="http://127.0.0.1:bad")
    with pytest.raises(ValueError, match="1-65535"):
        BuildProxy(enabled=True, url="http://127.0.0.1:99999")
    with pytest.raises(ValueError, match="1-65535"):
        BuildProxy(enabled=True, url="http://127.0.0.1:0")
    assert BuildProxy(enabled=True, url="http://127.0.0.1:10812").redacted_url()


def test_proxy_env_sets_build_variables_and_bypasses_loopback() -> None:
    env = _proxy_config().buildProxy.proxy_env()
    assert env["HTTP_PROXY"] == "http://user:secret@127.0.0.1:10812"
    assert env["http_proxy"] == env["HTTP_PROXY"]
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"]
    assert "127.0.0.1" in env["NO_PROXY"]
    assert "localhost" in env["no_proxy"]
    assert _proxy_config().buildProxy.redacted_url() == "http://127.0.0.1:10812"


def test_container_env_rewrites_loopback_but_host_env_keeps_it() -> None:
    """BUG-968：构建容器里的 127.0.0.1 指向容器自身，容器视角改指宿主网关。"""
    proxy = _proxy_config().buildProxy
    assert proxy.uses_loopback_host() is True
    assert proxy.proxy_env()["HTTP_PROXY"] == "http://user:secret@127.0.0.1:10812"
    container_env = proxy.container_proxy_env()
    assert (
        container_env["HTTP_PROXY"]
        == "http://user:secret@host.docker.internal:10812"
    )
    assert container_env["HTTPS_PROXY"] == container_env["HTTP_PROXY"]
    assert "127.0.0.1" in container_env["NO_PROXY"]

    lan = _proxy_config("http://192.168.1.10:8080").buildProxy
    assert lan.uses_loopback_host() is False
    assert lan.container_proxy_env()["HTTP_PROXY"] == "http://192.168.1.10:8080"


def test_merge_into_lets_later_instance_env_override_the_workspace_proxy() -> None:
    config = _proxy_config("http://127.0.0.1:10808")
    merged = merge_into({"PATH": "/usr/bin", "HTTP_PROXY": "http://ambient"}, config)
    assert merged["PATH"] == "/usr/bin"
    assert merged["HTTP_PROXY"] == "http://127.0.0.1:10808"
    merged.update({"HTTP_PROXY": "http://instance"})
    assert merged["HTTP_PROXY"] == "http://instance"


def test_subprocess_env_copies_the_parent_and_adds_the_proxy() -> None:
    env = subprocess_env(_proxy_config("http://127.0.0.1:10812"))
    assert env is not None
    assert env["PATH"] == os.environ["PATH"]
    assert env["HTTP_PROXY"] == "http://127.0.0.1:10812"


def test_dockerfile_never_declares_proxy_args(workspace: Workspace) -> None:
    """BUG-969：显式 ARG 会把代理地址与凭据留在 docker history。

    代理只经 compose build.args 以 Docker 内置代理构建参数传入，Dockerfile
    不得出现代理 ARG/ENV，也不得出现地址本身。
    """
    dockerfile = generate_dockerfile(
        _node_manifest(), workspace, config=_proxy_config()
    ).read_text(encoding="utf-8")
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy"):
        assert f"ARG {key}" not in dockerfile
        assert f"ENV {key}" not in dockerfile
    assert "10812" not in dockerfile
    assert "secret" not in dockerfile


def test_generate_dockerfile_and_compose_keep_proxy_out_of_the_running_container(
    workspace: Workspace,
) -> None:
    config = _proxy_config("http://127.0.0.1:10812")
    dockerfile = generate_dockerfile(_node_manifest(), workspace, config=config).read_text(
        encoding="utf-8"
    )
    assert "ARG HTTP_PROXY" not in dockerfile
    assert "ENV HTTP_PROXY" not in dockerfile
    assert "10812" not in dockerfile

    compose = generate_compose(
        _node_manifest(), workspace, host_port=39000, config=config
    ).read_text(encoding="utf-8")
    build_section, runtime_section = compose.split("container_name:", 1)
    assert "HTTP_PROXY:" in build_section
    # BUG-968：回环地址在构建参数里改写为宿主网关名，并注入 host-gateway 映射
    assert "host.docker.internal" in build_section
    assert "10812" in build_section
    assert "://user:secret@127.0.0.1" not in build_section
    assert "host.docker.internal:host-gateway" in build_section
    assert "HTTP_PROXY" not in runtime_section
    assert "10812" not in runtime_section


def test_compose_build_args_keep_lan_proxy_without_extra_hosts(
    workspace: Workspace,
) -> None:
    """非回环代理不改写地址，也不需要 host-gateway 映射（BUG-968）。"""
    compose = generate_compose(
        _node_manifest(),
        workspace,
        host_port=39000,
        config=_proxy_config("http://192.168.1.10:8080"),
    ).read_text(encoding="utf-8")
    assert "http://192.168.1.10:8080" in compose
    assert "host.docker.internal" not in compose


def test_generate_without_proxy_omits_build_args(workspace: Workspace) -> None:
    dockerfile = generate_dockerfile(_node_manifest(), workspace).read_text(encoding="utf-8")
    compose = generate_compose(_node_manifest(), workspace, host_port=39000).read_text(
        encoding="utf-8"
    )
    assert "ARG HTTP_PROXY" not in dockerfile
    assert "HTTP_PROXY" not in compose
    assert "host.docker.internal" not in compose


def test_write_build_proxy_replaces_only_its_block(workspace: Workspace) -> None:
    path = workspace.config_path
    path.write_text(
        "managerPort: 17800\n"
        "# 保留这行注释\n"
        "buildProxy:\n"
        "  enabled: false\n"
        "  url: \"\"\n"
        "buildMirrors:\n"
        "  enabled: true\n",
        encoding="utf-8",
    )
    write_build_proxy(path, BuildProxy(enabled=True, url=r"http://127.0.0.1:10812"))
    text = path.read_text(encoding="utf-8")
    assert "managerPort: 17800" in text
    assert "# 保留这行注释" in text
    assert "buildMirrors:" in text
    loaded = load_config(workspace)
    assert loaded.buildProxy.enabled is True
    assert loaded.buildProxy.url == "http://127.0.0.1:10812"
    assert loaded.managerPort == 17800


def test_write_build_proxy_replaces_across_blank_lines(workspace: Workspace) -> None:
    """BUG-970：节点内空行不能截断替换范围，旧 url 不得残留。"""
    path = workspace.config_path
    original = (
        "managerPort: 17800\n"
        "buildProxy:\n"
        "  enabled: false\n"
        "\n"
        "  url: \"\"\n"
        "buildMirrors:\n"
        "  enabled: true\n"
    )
    path.write_text(original, encoding="utf-8")
    write_build_proxy(path, BuildProxy(enabled=True, url=r"http://127.0.0.1:10812"))
    text = path.read_text(encoding="utf-8")
    assert text.count("url:") == 1
    assert "buildMirrors:" in text
    loaded = load_config(workspace)
    assert loaded.buildProxy.enabled is True
    assert loaded.buildProxy.url == "http://127.0.0.1:10812"


def test_write_build_proxy_rejects_duplicate_sections_without_touching_disk(
    workspace: Workspace,
) -> None:
    """BUG-970：残留重复 buildProxy 段时拒绝写入，磁盘保持原样。"""
    path = workspace.config_path
    original = (
        "buildProxy:\n"
        "  enabled: false\n"
        "  url: \"\"\n"
        "buildProxy:\n"
        "  enabled: false\n"
        "  url: \"http://stale:1\"\n"
    )
    path.write_text(original, encoding="utf-8")
    with pytest.raises(ConfigError, match="重复的 buildProxy 段"):
        write_build_proxy(path, BuildProxy(enabled=True, url=r"http://127.0.0.1:10812"))
    assert path.read_text(encoding="utf-8") == original


def test_docker_build_passes_proxy_env_only_when_enabled(workspace: Workspace, monkeypatch) -> None:
    calls: list[dict] = []

    def fake_execute(args, *, cwd, log_path=None, timeout=60, env=None):
        calls.append({"args": list(args), "env": env})
        return ComposeResult(args=list(args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr("local_webpage_access.docker_runtime._execute", fake_execute)
    DockerRuntime(workspace).build("api")
    assert calls[-1]["env"] is None

    write_build_proxy(
        workspace.config_path,
        BuildProxy(enabled=True, url="http://127.0.0.1:10808"),
    )
    DockerRuntime(workspace).build("api")
    assert calls[-1]["env"]["HTTP_PROXY"] == "http://127.0.0.1:10808"
    assert "build" in calls[-1]["args"]


def test_manager_dialog_api_persists_and_rejects_a_bad_url(
    workspace: Workspace, registry
) -> None:
    workspace.config_path.write_text(example_config_text(), encoding="utf-8")
    config = load_config(workspace)
    token = ensure_token(workspace)
    app = create_app(workspace, config, registry, token=token)
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        current = client.get("/api/build-proxy", headers=headers)
        assert current.status_code == 200
        assert current.json() == {"enabled": False, "url": ""}

        rejected = client.put(
            "/api/build-proxy",
            headers=headers,
            json={"enabled": True, "url": "ftp://127.0.0.1:10812"},
        )
        assert rejected.status_code == 400

        # BUG-972：非法端口同样在保存前 400，且配置文件不被改动
        before = workspace.config_path.read_text(encoding="utf-8")
        bad_port = client.put(
            "/api/build-proxy",
            headers=headers,
            json={"enabled": True, "url": "http://127.0.0.1:bad"},
        )
        assert bad_port.status_code == 400
        assert workspace.config_path.read_text(encoding="utf-8") == before

        saved = client.put(
            "/api/build-proxy",
            headers=headers,
            json={"enabled": True, "url": " http://127.0.0.1:10812 "},
        )
        assert saved.status_code == 200
        assert saved.json()["enabled"] is True
        assert saved.json()["url"] == "http://127.0.0.1:10812"

    reloaded = load_config(workspace)
    assert reloaded.buildProxy.proxy_env()["HTTP_PROXY"] == "http://127.0.0.1:10812"
    assert app.state.config.buildProxy.enabled is True


@pytest.mark.parametrize(
    ("proxies", "expected"),
    [
        ({"http": "127.0.0.1:7890", "https": "http://other:8080"}, "http://127.0.0.1:7890"),
        ({"http": "socks5://localhost:1080", "https": "http://localhost:7891"}, "http://localhost:7891"),
        ({"http": "http://localhost:bad"}, ""),
        ({"all": "socks5://localhost:1080"}, ""),
        ({}, ""),
    ],
)
def test_detect_system_build_proxy_without_saving(
    workspace: Workspace, registry, monkeypatch, proxies: dict, expected: str
) -> None:
    import urllib.request

    monkeypatch.setattr(urllib.request, "getproxies", lambda: proxies)
    workspace.config_path.write_text(example_config_text(), encoding="utf-8")
    before = workspace.config_path.read_text(encoding="utf-8")
    token = ensure_token(workspace)
    app = create_app(workspace, load_config(workspace), registry, token=token)
    with TestClient(app) as client:
        response = client.get(
            "/api/build-proxy/detect", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 200
    assert response.json() == {"url": expected}
    assert workspace.config_path.read_text(encoding="utf-8") == before


def test_manager_build_proxy_rejects_string_bool(workspace: Workspace, registry) -> None:
    """字符串 "false" 不得被 bool(...) 当成 True：严格解析，非 bool 直接 400。"""
    workspace.config_path.write_text(example_config_text(), encoding="utf-8")
    config = load_config(workspace)
    token = ensure_token(workspace)
    app = create_app(workspace, config, registry, token=token)
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        resp = client.put(
            "/api/build-proxy",
            headers=headers,
            json={"enabled": "false", "url": "http://127.0.0.1:10812"},
        )
        assert resp.status_code == 400
        current = client.get("/api/build-proxy", headers=headers)
        assert current.json() == {"enabled": False, "url": ""}


def test_manager_build_proxy_get_reflects_cli_disk_changes(
    workspace: Workspace, registry
) -> None:
    """CLI 直接改 config.json 后内存配置已过期；GET 须读磁盘最新值。"""
    workspace.config_path.write_text(example_config_text(), encoding="utf-8")
    config = load_config(workspace)
    token = ensure_token(workspace)
    app = create_app(workspace, config, registry, token=token)
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        assert client.get("/api/build-proxy", headers=headers).json() == {
            "enabled": False,
            "url": "",
        }
        # 模拟 CLI 直接写 config.json（未经过 API，内存 app.state 保持旧值）。
        write_build_proxy(
            workspace.config_path,
            BuildProxy(enabled=True, url="http://127.0.0.1:10812"),
        )
        assert app.state.config.buildProxy.enabled is False
        current = client.get("/api/build-proxy", headers=headers)
        assert current.json() == {
            "enabled": True,
            "url": "http://127.0.0.1:10812",
        }


def test_proxy_save_does_not_recreate_the_running_container(
    workspace: Workspace, registry, config: Config
) -> None:
    """BUG-976：仅保存/修改构建代理，下一次 start/restart/recover 不得重建容器。

    复现审查探针的路径：部署后记录指纹 → 保存代理 → _deployed_action 决策
    必须仍为 none（运行指纹已排除 Compose build 段）。
    """
    from local_webpage_access.compose import generate_env
    from local_webpage_access.lifecycle import (
        _compute_deployment_fingerprints,
        _deployed_action,
    )

    manifest = _node_manifest()
    workspace.ensure_app_dirs(manifest.id)
    manifest.container.hostPort = 39000
    manifest.container.composePath = str(workspace.app_compose_path(manifest.id))
    manifest.container.dockerfilePath = str(workspace.app_dockerfile_path(manifest.id))
    generate_dockerfile(manifest, workspace, config=config)
    generate_compose(manifest, workspace, host_port=39000, config=config)
    generate_env(manifest, workspace, host_port=39000)
    manifest.deploymentFingerprints = _compute_deployment_fingerprints(workspace, manifest)
    manifest.save(workspace.app_manifest_path(manifest.id))
    registry.upsert_from_manifest(manifest)

    assert _deployed_action(workspace, config, registry, manifest.id, manifest) == ("none", [])

    write_build_proxy(
        workspace.config_path,
        BuildProxy(enabled=True, url="http://10.0.0.1:1080"),
    )
    # _deployed_action 会先按最新代理重渲染 compose（build.args 变化），
    # 但运行决策不得因此重建容器
    assert _deployed_action(workspace, config, registry, manifest.id, manifest) == ("none", [])
    compose = workspace.app_compose_path(manifest.id).read_text(encoding="utf-8")
    assert "10.0.0.1:1080" in compose


def test_rejects_socks_proxy_urls() -> None:
    """默认 Python 构建没有 PySocks，pip 遇到 socks5 会在装依赖前失败。"""
    for url in ("socks5://127.0.0.1:10808", "socks5h://127.0.0.1:10808"):
        with pytest.raises(ValueError, match="http://"):
            BuildProxy(enabled=True, url=url)


def test_instance_uppercase_proxy_replaces_lowercase_workspace_value(
    workspace: Workspace,
) -> None:
    """pip 优先读小写 http_proxy。实例只改大写时，小写不能继续留工作区地址。"""
    manifest = _node_manifest()
    manifest.buildEnv = {
        "HTTP_PROXY": "http://10.1.2.3:9",
        "HTTPS_PROXY": "http://10.1.2.3:9",
    }
    compose = generate_compose(
        manifest,
        workspace,
        host_port=39000,
        config=_proxy_config("http://127.0.0.1:10812"),
    ).read_text(encoding="utf-8")
    assert 'HTTP_PROXY: "http://10.1.2.3:9"' in compose
    assert 'http_proxy: "http://10.1.2.3:9"' in compose
    assert 'HTTPS_PROXY: "http://10.1.2.3:9"' in compose
    assert 'https_proxy: "http://10.1.2.3:9"' in compose
    assert "10812" not in compose


def test_frontend_build_reloads_proxy_and_aligns_case(
    workspace: Workspace, registry, config: Config, monkeypatch
) -> None:
    """daemon 持有启动时的 Config。保存代理后的下一次前端构建必须读盘。"""
    from local_webpage_access.hosting import build_and_host_frontend, stop_instance
    from local_webpage_access.importer import build_manifest_from_detection
    from local_webpage_access.models import EntryConfig
    from local_webpage_access.scanner import DetectionResult

    workspace.ensure_app_dirs("spa")
    current = workspace.app_current("spa")
    (current / "package.json").write_text(
        '{"scripts":{"build":"vite build"}}', encoding="utf-8"
    )
    detection = DetectionResult(
        kind=Kind.NODE,
        runtime=Runtime.SHARED_STATIC,
        servingMode=ServingMode.SHARED_STATIC,
        resourceProfile=ResourceProfile.TINY,
        form="frontend-static",
        confidence="high",
        stack=["react"],
        entry=EntryConfig(install="npm ci", build="npm run build"),
    )
    manifest = build_manifest_from_detection(
        instance_id="spa",
        display_name="Spa",
        detection=detection,
        workspace=workspace,
    )
    manifest.buildEnv = {"HTTP_PROXY": "http://10.1.2.3:9", "HTTPS_PROXY": "http://10.1.2.3:9"}
    manifest.save(workspace.app_manifest_path("spa"))
    registry.upsert_from_manifest(manifest)

    stale = config.model_copy(update={"buildProxy": BuildProxy()})
    write_build_proxy(
        workspace.config_path,
        BuildProxy(enabled=True, url="http://127.0.0.1:10812"),
    )
    seen: list[dict[str, str]] = []

    def fake_run(cmd, *, cwd, log_path, env=None, **kw):
        seen.append(env or {})
        if "build" in cmd:
            dist = Path(cwd) / "dist"
            dist.mkdir(exist_ok=True)
            (dist / "index.html").write_text("<html>built</html>", encoding="utf-8")
        from subprocess import CompletedProcess

        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("local_webpage_access.hosting.run_command", fake_run)
    try:
        build_and_host_frontend(workspace, stale, registry, "spa")
    finally:
        stop_instance(workspace, config, registry, "spa")

    assert seen
    for env in seen:
        assert env["HTTP_PROXY"] == "http://10.1.2.3:9"
        assert env["http_proxy"] == "http://10.1.2.3:9"
        assert env["HTTPS_PROXY"] == "http://10.1.2.3:9"
        assert env["https_proxy"] == "http://10.1.2.3:9"
        assert "10812" not in env["http_proxy"]


def test_container_render_reloads_proxy_saved_after_startup(
    workspace: Workspace, registry, config: Config
) -> None:
    """保存代理后，daemon 内存里的旧 Config 不能继续生成 Compose 构建参数。"""
    from local_webpage_access.hosting import _ContainerDeploy, _render_container_artifacts

    manifest = _node_manifest()
    manifest.save(workspace.app_manifest_path(manifest.id))
    registry.upsert_from_manifest(manifest)
    workspace.ensure_app_dirs(manifest.id)
    stale = config.model_copy(update={"buildProxy": BuildProxy()})
    write_build_proxy(
        workspace.config_path,
        BuildProxy(enabled=True, url="http://127.0.0.1:10812"),
    )
    ctx = _ContainerDeploy(
        workspace=workspace,
        config=stale,
        registry=registry,
        instance_id=manifest.id,
        manifest=manifest,
        runtime=None,  # type: ignore[arg-type]
        build_id=1,
        op_started_at="2026-10-23T00:00:00",
        operation_kind="deploy",
        txn=False,
    )
    _render_container_artifacts(ctx)
    compose = workspace.app_compose_path(manifest.id).read_text(encoding="utf-8")
    assert "host.docker.internal:10812" in compose
    assert ctx.config.buildProxy.enabled is True
