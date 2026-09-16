"""BUG-678～683：issue #36～#39 复核缺陷回归。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from local_webpage_access.build_diagnostics import classify_build_failure
from local_webpage_access.errors import RecognitionError
from local_webpage_access.path_alias import verify_alias_live
from tests.test_issue37_38_alias import _ABS_HTML, _seed_aliased
from tests.test_issue39_container_build_env import _seed_python_container


_GOOD_HTML = "<!doctype html><html><body><h1>ok</h1></body></html>"


def test_verify_alias_live_uses_alias_http_body_not_entry_html(config, monkeypatch) -> None:
    """BUG-678：别名实际返回坏 HTML 时，不得因传入的直达 HTML 而通过。"""
    from local_webpage_access import path_alias

    def fake_probe(url, *, timeout=3.0):
        if url.rstrip("/").endswith("funasr-workbench"):
            return True, 200, "text/html", ("<!doctype html>" + _ABS_HTML).encode()
        return True, 200, "application/javascript", b"ok"

    monkeypatch.setattr(path_alias, "_http_probe_alias_resource", fake_probe)
    config.staticGatewayPort = 8080
    with pytest.raises(RecognitionError, match="绝对路径|白屏"):
        verify_alias_live(
            config,
            "funasr-workbench",
            entry_html=_GOOD_HTML,
            instance_id="funasr-workbench",
        )


def test_restart_records_alias_guard_result(
    workspace, registry, config, monkeypatch
) -> None:
    """BUG-679：restart 必须跑守卫并写入本次 aliasGuardResult。"""
    from local_webpage_access import lifecycle, path_alias
    from local_webpage_access.models import InstanceManifest

    manifest = _seed_aliased(workspace, registry)
    iid = manifest.id
    manifest.aliasGuardResult = "passed"
    manifest.aliasLiveVerifiedAt = "2026-09-15T00:00:00+08:00"
    manifest.aliasLiveVerifiedFor = iid
    manifest.save(workspace.app_manifest_path(iid))

    monkeypatch.setattr("local_webpage_access.hosting.stop_instance", lambda *a, **k: None)

    def fake_host(*a, **k):
        return InstanceManifest.load(workspace.app_manifest_path(iid))

    monkeypatch.setattr("local_webpage_access.hosting.host_instance", fake_host)
    monkeypatch.setattr(lifecycle, "_sync_alias_port", lambda *a, **k: False)
    monkeypatch.setattr(path_alias, "StaticGateway", path_alias.StaticGateway)
    from tests.test_issue21_alias_preservation import _CaddyFakeGW

    monkeypatch.setattr(path_alias, "StaticGateway", _CaddyFakeGW)
    monkeypatch.setattr(
        path_alias, "_fetch_entrypoint_html_for_alias_guard", lambda **_k: _ABS_HTML
    )
    monkeypatch.setattr(
        path_alias,
        "_http_probe_alias_resource",
        lambda *_a, **_k: (True, 200, "text/html", ("<!doctype html>" + _ABS_HTML).encode()),
    )
    config.staticGatewayPort = 8080
    restarted = lifecycle.restart_instance(workspace, config, registry, iid)
    reloaded = InstanceManifest.load(workspace.app_manifest_path(restarted.id))
    assert reloaded.aliasGuardResult == "failed"
    assert reloaded.static is not None
    assert reloaded.static.routeHost == iid


def test_rebuild_caddy_aliases_records_guard(
    workspace, registry, config, monkeypatch
) -> None:
    """BUG-679：网关切换重建别名后必须记录本次守卫结果。"""
    from local_webpage_access import gateway_switch, path_alias
    from local_webpage_access.models import InstanceManifest
    from tests.test_issue21_alias_preservation import _CaddyFakeGW

    manifest = _seed_aliased(workspace, registry)
    iid = manifest.id
    manifest.aliasGuardResult = "passed"
    manifest.save(workspace.app_manifest_path(iid))
    gw = _CaddyFakeGW(workspace, config)
    monkeypatch.setattr(path_alias, "StaticGateway", _CaddyFakeGW)
    monkeypatch.setattr(
        path_alias, "_fetch_entrypoint_html_for_alias_guard", lambda **_k: _ABS_HTML
    )
    monkeypatch.setattr(
        path_alias,
        "_http_probe_alias_resource",
        lambda *_a, **_k: (True, 200, "text/html", ("<!doctype html>" + _ABS_HTML).encode()),
    )
    config.staticGatewayPort = 8080
    rebuilt = gateway_switch._rebuild_caddy_aliases(workspace, registry, gw)
    assert iid in rebuilt
    reloaded = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert reloaded.aliasGuardResult == "failed"


def test_build_env_rejects_managed_host_port(workspace, registry) -> None:
    """BUG-680：不得用 buildEnv 改写 LWA 分配的 HOST_PORT。"""
    from local_webpage_access.instance_settings import update_instance_settings

    _seed_python_container(workspace, registry)
    with pytest.raises(ValueError, match="HOST_PORT"):
        update_instance_settings(
            workspace, registry, "funasr", {"buildEnv": {"HOST_PORT": "19999"}}
        )


def test_build_env_does_not_override_allocated_port_in_env(workspace, registry) -> None:
    """BUG-680：即使 manifest 带上 HOST_PORT，.env 仍用 LWA 分配端口。"""
    from local_webpage_access.compose import generate_env
    from local_webpage_access.models import InstanceManifest

    _seed_python_container(workspace, registry)
    path = workspace.app_manifest_path("funasr")
    manifest = InstanceManifest.load(path)
    manifest.buildEnv = {"HOST_PORT": "19999", "VITE_BASE": "/funasr/"}
    manifest.save(path)
    env = generate_env(manifest, workspace, host_port=18006).read_text(encoding="utf-8")
    assert "HOST_PORT=18006" in env
    assert "HOST_PORT=19999" not in env
    assert "VITE_BASE=" not in env


def _compose_published_port(compose_dir: Path, *, env: dict[str, str] | None = None) -> int:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("需要 docker compose 解析最终端口")
    cmd_env = {**os.environ, **(env or {})}
    for key in ("HOST_PORT", "INTERNAL_PORT", "MEMORY_LIMIT", "CPU_LIMIT"):
        if not env or key not in env:
            cmd_env.pop(key, None)
    proc = subprocess.run(
        [docker, "compose", "-f", "compose.yaml", "config", "--format", "json"],
        cwd=compose_dir,
        capture_output=True,
        text=True,
        env=cmd_env,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip(f"docker compose config 不可用：{proc.stderr[-300:]}")
    data = json.loads(proc.stdout)
    service = next(iter(data["services"].values()))
    published = service["ports"][0]["published"]
    return int(published)


def test_compose_config_keeps_allocated_host_port(workspace, registry) -> None:
    """BUG-680：Compose 最终解析端口等于 LWA 分配值。"""
    from local_webpage_access.compose import generate_compose, generate_env
    from local_webpage_access.models import InstanceManifest

    _seed_python_container(workspace, registry)
    path = workspace.app_manifest_path("funasr")
    manifest = InstanceManifest.load(path)
    manifest.buildEnv = {"HOST_PORT": "19999", "VITE_BASE": "/funasr/"}
    manifest.save(path)
    generate_compose(manifest, workspace, host_port=18006)
    generate_env(manifest, workspace, host_port=18006)
    published = _compose_published_port(workspace.app_dir("funasr") / "docker")
    assert published == 18006


def test_build_args_literal_survives_host_env_and_dollar(workspace, registry, monkeypatch) -> None:
    """BUG-681：构建参数按 manifest 字面值渲染，不受宿主同名变量与 $ 二次展开影响。"""
    from local_webpage_access.compose import generate_compose, generate_env
    from local_webpage_access.instance_settings import update_instance_settings
    from local_webpage_access.models import InstanceManifest

    _seed_python_container(workspace, registry)
    update_instance_settings(
        workspace,
        registry,
        "funasr",
        {"buildEnv": {"VITE_BASE": "/right/", "FLAG": "prefix$LWA_REVIEW_MISSING"}},
    )
    manifest = InstanceManifest.load(workspace.app_manifest_path("funasr"))
    compose_path = generate_compose(manifest, workspace, host_port=18006)
    generate_env(manifest, workspace, host_port=18006)
    text = compose_path.read_text(encoding="utf-8")
    assert "${VITE_BASE}" not in text
    assert "/right/" in text
    assert "$$LWA_REVIEW_MISSING" in text or "prefix$LWA_REVIEW_MISSING" in text

    docker = shutil.which("docker")
    if docker is None:
        return
    proc = subprocess.run(
        [docker, "compose", "-f", "compose.yaml", "config", "--format", "json"],
        cwd=workspace.app_dir("funasr") / "docker",
        capture_output=True,
        text=True,
        env={**os.environ, "VITE_BASE": "/wrong/", "LWA_REVIEW_MISSING": "SHOULD_NOT_EXPAND"},
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip(f"docker compose config 不可用：{proc.stderr[-300:]}")
    data = json.loads(proc.stdout)
    service = next(iter(data["services"].values()))
    args = service["build"]["args"]
    assert args["VITE_BASE"] == "/right/"
    # Compose v5 的 config JSON 会把字面 $ 再写成 $$；实际值仍是单美元。
    flag = str(args["FLAG"]).replace("$$", "$")
    assert flag == "prefix$LWA_REVIEW_MISSING"
    assert "SHOULD_NOT_EXPAND" not in str(args["FLAG"])


def test_classify_success_registry_then_apt_is_apt() -> None:
    """BUG-682：成功拉取 registry token 后的 apt 失败不得判 registry。"""
    text = (
        "#5 [auth] library/python:pull token for registry-1.docker.io\n"
        "#5 DONE 0.1s\n"
        "#8 213.6 Err:4 http://mirrors.aliyun.com/debian trixie/main arm64 Packages\n"
        "#8 ERROR: process \"/bin/sh -c apt-get install\" did not complete successfully\n"
        "E: Unable to fetch some archives, maybe run apt-get update\n"
        "failed to solve: process did not complete successfully\n"
    )
    hint = classify_build_failure(text)
    assert hint is not None
    assert hint.kind == "apt"


def test_classify_success_registry_then_disk_is_disk() -> None:
    """BUG-682：成功 registry 日志后的磁盘错误按 disk 归因。"""
    text = (
        "#5 [auth] library/python:pull token for registry-1.docker.io\n"
        "#5 DONE\n"
        "#8 ERROR: write /var/cache/apt: No space left on device\n"
        "failed to solve: No space left on device\n"
    )
    hint = classify_build_failure(text)
    assert hint is not None
    assert hint.kind == "disk"


def test_python_vite_hint_does_not_recommend_entry_build(workspace, registry) -> None:
    """BUG-683：Python 容器不得建议改 entry.build；须说明预编译产物与 buildHooks。"""
    from local_webpage_access.errors import RecognitionError
    from local_webpage_access.path_alias import _append_lwa_build_hint

    manifest = _seed_python_container(workspace, registry)
    manifest.stack = ["fastapi", "vite"]
    exc = RecognitionError("入口 HTML 含未带别名前缀的加载型绝对路径资源")
    hinted = _append_lwa_build_hint(exc, manifest, "funasr")
    assert "entry.build" not in hinted.message
    assert "buildHooks" in hinted.message
    assert "预编译" in hinted.message or "不会改写" in hinted.message


def test_python_frontend_build_step_uses_vite_base(workspace, registry) -> None:
    """BUG-683：存在 frontend/ 时 Dockerfile 必须执行前端构建并消费 VITE_BASE。"""
    from local_webpage_access.dockerfile_templates import generate_dockerfile
    from local_webpage_access.instance_settings import update_instance_settings
    from local_webpage_access.models import InstanceManifest

    _seed_python_container(workspace, registry)
    frontend = workspace.app_current("funasr") / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text(
        '{"scripts":{"build":"node build.mjs"}}', encoding="utf-8"
    )
    # BUG-684：npm ci 需要 npm 锁文件；契约完整才自动构建。
    (frontend / "package-lock.json").write_text('{"lockfileVersion":3}', encoding="utf-8")
    (frontend / "build.mjs").write_text(
        "const fs=require('fs');const path=require('path');\n"
        "const base=process.env.VITE_BASE||'/';\n"
        "const out=path.join('..','backend','static');\n"
        "fs.mkdirSync(out,{recursive:true});\n"
        "fs.writeFileSync(path.join(out,'index.html'),"
        "`<script src=\"${base}assets/app.js\"></script>`);\n"
        "fs.mkdirSync(path.join(out,'assets'),{recursive:true});\n"
        "fs.writeFileSync(path.join(out,'assets','app.js'),'console.log(1)');\n",
        encoding="utf-8",
    )
    update_instance_settings(
        workspace, registry, "funasr", {"buildEnv": {"VITE_BASE": "/funasr/"}}
    )
    manifest = InstanceManifest.load(workspace.app_manifest_path("funasr"))
    docker = generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")
    assert "frontend" in docker
    assert "npm run build" in docker
    assert "VITE_BASE" in docker


def test_frontend_source_build_alias_assets_e2e(monkeypatch) -> None:
    """端到端：源码构建产出带别名前缀的 HTML/JS，活验证探针可达。"""
    import tempfile

    from local_webpage_access import path_alias
    from local_webpage_access.config import Config

    tmp = Path(tempfile.mkdtemp())
    frontend = tmp / "frontend"
    frontend.mkdir()
    backend_static = tmp / "backend" / "static"
    (frontend / "build.cjs").write_text(
        "const fs=require('fs');const path=require('path');\n"
        "const base=process.env.VITE_BASE||'/';\n"
        "const out=path.join('..','backend','static');\n"
        "fs.mkdirSync(path.join(out,'assets'),{recursive:true});\n"
        "fs.writeFileSync(path.join(out,'index.html'),"
        "`<!doctype html><script src=\"${base}assets/app.js\"></script>`);\n"
        "fs.writeFileSync(path.join(out,'assets','app.js'),'export default 1');\n",
        encoding="utf-8",
    )
    env = {**os.environ, "VITE_BASE": "/funasr/"}
    proc = subprocess.run(
        ["node", "build.cjs"], cwd=frontend, env=env, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    html = (backend_static / "index.html").read_text(encoding="utf-8")
    js = (backend_static / "assets" / "app.js").read_bytes()
    assert "/funasr/assets/app.js" in html
    assert b"export default 1" in js

    def fake_probe(url, *, timeout=3.0):
        if url.rstrip("/").endswith("funasr"):
            return True, 200, "text/html", html.encode()
        if url.endswith("/funasr/assets/app.js"):
            return True, 200, "application/javascript", js
        return False, 404, None, b""

    monkeypatch.setattr(path_alias, "_http_probe_alias_resource", fake_probe)
    cfg = Config()
    cfg.staticGatewayPort = 8080
    verify_alias_live(cfg, "funasr", entry_html=None, instance_id="funasr")


# ---- BUG-684：前端自动构建契约门禁 ----


def _render_python_frontend(
    workspace, registry, *, lock: str | None, build_script: str | None = "node build.mjs"
) -> str:
    from local_webpage_access.dockerfile_templates import generate_dockerfile
    from local_webpage_access.models import InstanceManifest

    _seed_python_container(workspace, registry)
    frontend = workspace.app_current("funasr") / "frontend"
    frontend.mkdir(parents=True, exist_ok=True)
    pkg = {"scripts": {"build": build_script}} if build_script else {"name": "x"}
    (frontend / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    if lock:
        (frontend / lock).write_text("{}", encoding="utf-8")
    manifest = InstanceManifest.load(workspace.app_manifest_path("funasr"))
    return generate_dockerfile(manifest, workspace).read_text(encoding="utf-8")


@pytest.mark.parametrize("lock", [None, "pnpm-lock.yaml", "yarn.lock"])
def test_python_frontend_auto_build_skipped_without_npm_contract(
    workspace, registry, lock
) -> None:
    """BUG-684：无 npm 锁文件 / pnpm·yarn 项目不得强制 npm ci（无锁必失败）。"""
    docker = _render_python_frontend(workspace, registry, lock=lock)
    assert "前端自动构建跳过" in docker
    assert "RUN npm ci" not in docker


def test_python_frontend_auto_build_skipped_without_build_script(
    workspace, registry
) -> None:
    """BUG-684：无 build 脚本的 frontend 只作为静态依赖，不自动构建。"""
    docker = _render_python_frontend(
        workspace, registry, lock="package-lock.json", build_script=None
    )
    assert "前端自动构建跳过" in docker
    assert "无 build 脚本" in docker
    assert "npm ci" not in docker


def test_python_frontend_auto_build_accepts_npm_shrinkwrap(workspace, registry) -> None:
    """BUG-684：npm-shrinkwrap.json 同为 npm 锁文件，契约完整则构建。"""
    docker = _render_python_frontend(workspace, registry, lock="npm-shrinkwrap.json")
    assert "RUN npm ci" in docker
    assert "npm run build" in docker


# ---- BUG-685：Python 提示的别名跟随迁移顺序 ----


def test_python_vite_hint_follow_alias_migration_order(workspace, registry) -> None:
    """BUG-685：开跟随时手动 VITE_BASE 被覆盖，提示须先关跟随再设 base。"""
    from local_webpage_access.instance_settings import effective_build_env
    from local_webpage_access.path_alias import _append_lwa_build_hint

    manifest = _seed_python_container(workspace, registry)
    manifest.stack = ["fastapi", "vite"]
    manifest.buildBaseFromAlias = True
    manifest.buildEnv = {"VITE_BASE": "/new/"}
    # 陷阱实证：当前无别名时跟随把 effective VITE_BASE 覆盖为 /。
    assert effective_build_env(manifest)["VITE_BASE"] == "/"
    exc = RecognitionError("入口 HTML 含未带别名前缀的加载型绝对路径资源")
    msg = _append_lwa_build_hint(exc, manifest, "target").message
    assert "--no-follow-alias-base" in msg
    assert "被当前别名或 / 覆盖" in msg
    assert msg.index("--no-follow-alias-base") < msg.index("lwa rebuild")
    assert msg.index("lwa rebuild") < msg.index("--follow-alias-base")


def test_python_vite_hint_without_follow_keeps_short_steps(workspace, registry) -> None:
    """BUG-685：未开跟随时保持短指引，不引入多余迁移步骤。"""
    from local_webpage_access.path_alias import _append_lwa_build_hint

    manifest = _seed_python_container(workspace, registry)
    manifest.stack = ["fastapi", "vite"]
    exc = RecognitionError("入口 HTML 含未带别名前缀的加载型绝对路径资源")
    msg = _append_lwa_build_hint(exc, manifest, "target").message
    assert "--build-env VITE_BASE=/target/" in msg
    assert "--no-follow-alias-base" not in msg


# ---- BUG-686：切 builtin 记录本次守卫结果 ----


def test_enable_running_builtin_records_skipped_guard(
    workspace, registry, config
) -> None:
    """BUG-686：Caddy→builtin 后不得沿用旧 passed；记 skipped + 本次时间。"""
    from local_webpage_access import gateway_switch
    from local_webpage_access.models import InstanceManifest

    manifest = _seed_aliased(workspace, registry)
    iid = manifest.id
    old_checked = "2026-09-01T00:00:00+08:00"
    manifest.aliasGuardResult = "passed"
    manifest.aliasGuardCheckedAt = old_checked
    manifest.save(workspace.app_manifest_path(iid))

    class _FakeBuiltinGW:
        def __init__(self) -> None:
            self.enabled: list[tuple[str, int, str | None]] = []

        def enable(self, iid_, port, public, *, wait_health=True, alias=None) -> None:
            self.enabled.append((iid_, port, alias))

    gw = _FakeBuiltinGW()
    enabled = gateway_switch._enable_running_builtin(workspace, config, registry, gw)
    assert iid in enabled
    assert gw.enabled and gw.enabled[0][2] == iid
    reloaded = InstanceManifest.load(workspace.app_manifest_path(iid))
    assert reloaded.aliasGuardResult == "skipped"
    assert reloaded.aliasGuardCheckedAt not in (None, old_checked)


# ---- BUG-687：失败特征位于 ERROR 行之前 ----


def test_classify_killed_before_error_is_killed_uncertain() -> None:
    """BUG-687（复核复现）：Killed 在同步骤 ERROR 前不得被截掉。"""
    text = (
        "#8 1.0 /bin/sh: 1: Killed\n"
        '#8 ERROR: process "/bin/sh -c npm run build" did not complete successfully:'
        " exit code: 137\n"
        'failed to solve: process "/bin/sh -c npm run build" did not complete'
        " successfully: exit code: 137\n"
    )
    hint = classify_build_failure(text)
    assert hint is not None
    assert hint.kind == "killed"
    assert hint.confidence == "uncertain"
    assert "Killed" in (hint.evidence or "")


def test_classify_feature_before_error_still_matched() -> None:
    """BUG-687：oom / disk / apt 特征位于 ERROR 前同样归因正确。"""
    oom = (
        "#8 5.5 /bin/sh: 1: cannot allocate memory\n"
        "#8 ERROR: process did not complete successfully: exit code: 1\n"
        "failed to solve: process did not complete successfully\n"
    )
    assert classify_build_failure(oom).kind == "oom"
    disk = (
        "#8 9.9 write build/index.js: No space left on device\n"
        "#8 ERROR: process did not complete successfully: exit code: 1\n"
        "failed to solve: process did not complete successfully\n"
    )
    assert classify_build_failure(disk).kind == "disk"
    apt = (
        "#8 213.6 Err:4 http://mirrors.aliyun.com/debian trixie/main arm64 Packages\n"
        "#8 ERROR: process \"/bin/sh -c apt-get install\" did not complete successfully\n"
        "failed to solve: process did not complete successfully\n"
    )
    assert classify_build_failure(apt).kind == "apt"


def test_failure_region_excludes_other_step_registry_noise() -> None:
    """BUG-682 回归：仅保留失败步骤前文，其它步骤的 registry 日志仍被排除。"""
    from local_webpage_access.build_diagnostics import _failure_region

    blob = (
        "#5 [auth] library/python:pull token for registry-1.docker.io\n"
        "#5 DONE 0.1s\n"
        "#8 1.0 /bin/sh: 1: Killed\n"
        "#8 ERROR: process failed: exit code: 137\n"
        "failed to solve: process failed\n"
    )
    region = _failure_region(blob)
    assert "Killed" in region
    assert "registry-1.docker.io" not in region
