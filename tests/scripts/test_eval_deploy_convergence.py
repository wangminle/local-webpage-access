"""CHK-418～428 / DEV-145/147/148：真实评测样本驱动的部署收敛。

产品取舍：
- Flask init-db 只在库文件缺失时执行，不每次启动清空教程库。
- 数据库变量引导写入已有 docker/.env.local，不新增配置出口。
- 构建日志分段定位，不截断已有轮转。
- 缓存走工作区通道，不默认保留 current/ 任意子目录。
"""

from __future__ import annotations

import inspect
import io
import json
import os
import zipfile
from pathlib import Path

import pytest

from local_webpage_access.build_sources import (
    diagnose_proxy_path,
    explain_source_path,
    host_build_timeout,
    inject_pkg_cache_env,
    manual_build_blocks_heal,
    mark_build_owner,
    pkg_cache_dir,
    record_source_failure,
    record_source_success,
    source_in_cooldown,
    timeout_hint,
    write_build_segment,
)
from local_webpage_access.candidate_generator import _discover_health_probes, generate_candidates
from local_webpage_access.config import Config
from local_webpage_access.evidence_collector import collect
from local_webpage_access.folder_source import compute_source_hash, pack_source_dir
from local_webpage_access.node_toolchain import describe_import_toolchain, inspect_node_toolchain
from local_webpage_access.paths import Workspace
from local_webpage_access.scanner import Scanner, start_command_wraps_migration

EVALS = Path(__file__).resolve().parents[1] / "evals"


def _eval(rel: str) -> Path:
    path = EVALS / rel
    if not path.is_dir():
        pytest.skip(f"缺少评测样本 {rel}")
    return path


# ---- CHK-418 / CHK-421 识别与配置 -------------------------------------------


def test_flask_tutorial_infers_sqlite_and_once_only_init_db() -> None:
    root = _eval("flask/old/examples/tutorial")
    result = Scanner().detect(root)
    assert result.hasDatabase is True
    assert result.database is not None
    assert result.database.type == "sqlite"
    start = result.entry.start or ""
    assert "init-db" in start
    assert "/app/instance/flaskr.sqlite" in start
    assert "flask --app flaskr init-db" in start
    assert "gunicorn" in start
    assert start_command_wraps_migration(start)


def test_flask_init_db_skipped_when_sqlite_path_unconfirmed(tmp_path: Path) -> None:
    """BUG-924：唯一 init-db 但未确认 SQLite 路径时不得默认 flaskr.sqlite。"""
    pkg = tmp_path / "webapp"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "import click\n"
        "from flask import Flask\n"
        "def create_app():\n"
        "    app = Flask(__name__)\n"
        "    app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://db/app'\n"
        "    return app\n"
        "@click.command('init-db')\n"
        "def init_db():\n"
        "    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='webapp'\nversion='0'\ndependencies=['flask']\n",
        encoding="utf-8",
    )
    result = Scanner().detect(tmp_path)
    start = result.entry.start or ""
    assert "init-db" not in start
    assert "flaskr.sqlite" not in start
    assert "gunicorn" in start


def test_flask_init_db_keeps_instance_relative_sqlite_path(tmp_path: Path) -> None:
    """库文件在 instance/db/ 时不得只检查 /app/instance/prod.sqlite。"""
    pkg = tmp_path / "webapp"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "import click\n"
        "import os\n"
        "from flask import Flask\n"
        "def create_app():\n"
        "    app = Flask(__name__)\n"
        "    app.config['DATABASE'] = os.path.join(app.instance_path, 'db/prod.sqlite')\n"
        "    return app\n"
        "@click.command('init-db')\n"
        "def init_db():\n"
        "    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='webapp'\nversion='0'\ndependencies=['flask']\n",
        encoding="utf-8",
    )
    result = Scanner().detect(tmp_path)
    start = result.entry.start or ""
    assert "init-db" in start
    assert "/app/instance/db/prod.sqlite" in start
    assert "/app/instance/prod.sqlite" not in start.replace("/app/instance/db/prod.sqlite", "")


def test_flask_without_init_db_command_does_not_invent_one(tmp_path: Path) -> None:
    pkg = tmp_path / "webapp"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "from flask import Flask\n"
        "def create_app():\n"
        "    return Flask(__name__)\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='webapp'\nversion='0'\ndependencies=['flask']\n",
        encoding="utf-8",
    )
    result = Scanner().detect(tmp_path)
    start = result.entry.start or ""
    assert "init-db" not in start
    assert "gunicorn" in start


def test_linkding_sqlite_settings_set_has_database() -> None:
    root = _eval("linkding/old")
    result = Scanner().detect(root)
    assert result.hasDatabase is True
    assert result.database is not None
    assert result.database.type == "sqlite"


def test_linkding_django_routes_are_discovered() -> None:
    root = _eval("linkding/old")
    evidence = collect(root)
    candidates = generate_candidates(evidence)
    backend = next(c for c in candidates if c.kind == "python")
    _discover_health_probes(evidence, backend)
    texts = " ".join(backend.reasoning)
    assert "Django" in texts and "路由" in texts


def test_hedgedoc_env_hint_points_to_existing_env_local() -> None:
    from local_webpage_access.cli.importing import _database_env_hints

    root = _eval("hedgedoc/old")
    hints = _database_env_hints(root)
    joined = "\n".join(hints)
    assert "docker/.env.local" in joined
    assert "CMD_DB_URL" in joined
    assert "DB_TYPE" in joined
    assert "、DB_URL、" not in joined


# ---- CHK-420 / 423 / 424 工具链 ---------------------------------------------


def test_frontend_subdir_uses_root_pnpm_lockfile(tmp_path: Path) -> None:
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - frontend\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"private": True, "workspaces": ["frontend"]}),
        encoding="utf-8",
    )
    app = tmp_path / "frontend"
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
    tool = inspect_node_toolchain(tmp_path, workdir=app)
    assert tool.manager == "pnpm"
    candidates = generate_candidates(collect(tmp_path))
    frontend = next(c for c in candidates if c.form == "frontend-static")
    install = frontend.entry.install or ""
    assert "pnpm" in install
    assert install.strip() != "npm install"
    assert "npm ci" not in install


def test_workspace_child_lockfile_does_not_override_root_pnpm(tmp_path: Path) -> None:
    """子包残留 package-lock.json 不得把仓库根 pnpm workspace 改成 npm ci。"""
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - web\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"private": True, "scripts": {"build": "pnpm --filter web run build"}}),
        encoding="utf-8",
    )
    web = tmp_path / "web"
    web.mkdir()
    (web / "package-lock.json").write_text("{}", encoding="utf-8")
    (web / "package.json").write_text(
        json.dumps(
            {
                "name": "web",
                "dependencies": {"astro": "^5.0.0"},
                "scripts": {"build": "astro build"},
            }
        ),
        encoding="utf-8",
    )
    tool = inspect_node_toolchain(tmp_path, workdir=web)
    assert tool.manager == "pnpm"
    assert tool.workspace_root.resolve() == tmp_path.resolve()
    result = Scanner().detect(tmp_path)
    install = result.entry.install or ""
    assert "pnpm" in install
    assert "npm ci" not in install


def test_homarr_import_notes_mention_bun_and_dockerfile() -> None:
    root = _eval("homarr/old")
    notes = describe_import_toolchain(root)
    joined = "\n".join(notes)
    assert "bun" in joined.lower()
    assert "Dockerfile" in joined
    result = Scanner().detect(root)
    text = "\n".join(result.notes)
    assert "bun" in text.lower()
    assert "Dockerfile" in text


def test_astro_portfolio_isolated_without_lockfile_stays_npm() -> None:
    root = _eval("astro/old/examples/portfolio")
    result = Scanner().detect(root)
    assert result.form == "frontend-static"
    assert result.entry.install == "npm install"


def test_static_failure_hint_does_not_blame_dockerfile_syntax() -> None:
    from local_webpage_access.lifecycle import _suggest_fix

    class Fake(Exception):
        pass

    msg = Fake("HOSTING_ERROR 未找到可托管的 HTML；源码含 bun.lock 与 Dockerfile")
    hint = _suggest_fix(msg)
    assert "不要把失败归因于 Dockerfile 语法" in hint or "而非 Dockerfile 语法" in hint
    assert "bun" in hint.lower() or "工具链" in hint or "静态" in hint


# ---- CHK-419 / 428 更新与指纹 -----------------------------------------------


def test_pack_source_dir_hash_ignores_walk_order_and_mode_bits(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    for root in (a, b):
        (root / "z").mkdir(parents=True)
        (root / "m").mkdir()
        (root / "z" / "one.txt").write_text("one", encoding="utf-8")
        (root / "m" / "two.txt").write_text("two", encoding="utf-8")
    os.chmod(a / "z" / "one.txt", 0o600)
    os.chmod(b / "z" / "one.txt", 0o644)
    za = pack_source_dir(a, dest_zip=tmp_path / "a.zip")
    zb = pack_source_dir(b, dest_zip=tmp_path / "b.zip")
    assert za.read_bytes() == zb.read_bytes()
    names_a = zipfile.ZipFile(za).namelist()
    names_b = zipfile.ZipFile(zb).namelist()
    assert names_a == sorted(names_a)
    assert names_a == names_b
    assert compute_source_hash(a) == compute_source_hash(b)


def test_git_switch_from_zip_can_pin_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from local_webpage_access.cli import app
    from local_webpage_access.init_workspace import init_workspace

    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    monkeypatch.setenv("LWA_WORKSPACE", str(ws_root))
    monkeypatch.chdir(ws_root)
    init_workspace(ws_root)
    runner = CliRunner()
    # 尚无实例时 --update 会失败，但不得再因 --ref 与 --update 互斥而在解析阶段拒绝
    result = runner.invoke(
        app,
        [
            "import",
            "--from-git",
            "https://github.com/gabrielecirulli/2048",
            "--ref",
            "cf01ca7e8bf670822c55b0a4b04cd5c2b0271b3e",
            "--update",
            "game-2048",
        ],
    )
    assert "不能与 --from-git --update 同时使用" not in result.output
    assert result.exit_code != 2 or "不存在" in result.output or "不是 git" in result.output


def test_looks_like_commit_ref_accepted_by_git_source() -> None:
    from local_webpage_access.git_source import looks_like_commit_ref

    assert looks_like_commit_ref("cf01ca7e8bf670822c55b0a4b04cd5c2b0271b3e")
    assert not looks_like_commit_ref("cf01ca7")
    assert not looks_like_commit_ref("20261020")
    assert not looks_like_commit_ref("main")
    assert not looks_like_commit_ref("v1.2.3")


# ---- CHK-422/426/427 DEV-145/147/148 ----------------------------------------


def test_host_build_timeout_budget_and_hint() -> None:
    cfg = Config(hostBuildTimeout=600)
    assert host_build_timeout(cfg, monorepo=True) >= 1800
    hint = timeout_hint(600, "yarn")
    assert "600" in hint
    assert "hostBuildTimeout" in hint


def test_pkg_cache_is_outside_current(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    ws.ensure_workspace_dirs()
    env = inject_pkg_cache_env({"PATH": "/usr/bin"}, ws, "yarn")
    cache = Path(env["YARN_CACHE_FOLDER"])
    assert cache == pkg_cache_dir(ws, "yarn")
    assert "current" not in cache.parts
    assert cache.is_relative_to(ws.run)


def test_build_log_segment_keeps_history(tmp_path: Path) -> None:
    buf = io.StringIO()
    buf.write("old failure\n")
    write_build_segment(buf, instance_id="demo", attempt="3", command="yarn install", owner="manual")
    write_build_segment(buf, instance_id="demo", attempt="4", command="yarn install", owner="heal")
    text = buf.getvalue()
    assert "old failure" in text
    assert "attempt=3" in text
    assert "attempt=4" in text
    assert text.count("LWA-BUILD") == 2


def test_download_source_host_stops_at_following_error_text() -> None:
    from local_webpage_access.build_sources import extract_download_sources

    found = extract_download_sources(
        "registry.npmjs.org ECONNRESET during install"
    )
    assert found == ["registry.npmjs.org"]


def test_source_health_cooldown_and_success_remain_uncertain(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    ws.ensure_workspace_dirs()
    record_source_failure(ws, "https://registry.yarnpkg.com", error="502", cooldown_sec=3600)
    assert source_in_cooldown(ws, "registry.yarnpkg.com") is not None
    record_source_success(ws, "registry.yarnpkg.com")
    explain = explain_source_path(ws, "registry.yarnpkg.com")
    assert "失败" in explain
    assert "不确定" in explain or "不清除" in explain or "冷却" in explain


def test_manual_owner_blocks_self_heal(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    ws.ensure_workspace_dirs()
    assert manual_build_blocks_heal(ws, "demo") is None
    mark_build_owner(ws, "demo", owner="manual", reason="lwa rebuild")
    blocked = manual_build_blocks_heal(ws, "demo")
    assert blocked is not None
    assert blocked["owner"] == "manual"


def test_dead_manual_owner_pid_is_recycled(tmp_path: Path) -> None:
    """BUG-925：构建进程已退出时不得凭残留 owner=manual 永久挡住自愈。"""
    ws = Workspace(tmp_path)
    ws.ensure_workspace_dirs()
    mark_build_owner(ws, "demo", owner="manual", reason="killed")
    path = ws.run / "build-owners" / "demo.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["pid"] = 1_000_000_001
    path.write_text(json.dumps(data), encoding="utf-8")
    assert manual_build_blocks_heal(ws, "demo") is None
    assert not path.is_file()


def test_fail_keep_old_and_heal_priority_are_wired() -> None:
    """部署失败保旧与手动构建优先于自愈：复用既有闭环，不截断日志、不覆盖 current/。"""
    from local_webpage_access.hosting import build_and_host_frontend
    from local_webpage_access.lifecycle import rebuild_instance

    host_src = inspect.getsource(build_and_host_frontend)
    assert "was_serving and not content_sync_started" in host_src
    rebuild_src = inspect.getsource(rebuild_instance)
    assert "mark_build_owner" in rebuild_src
    assert "manual" in rebuild_src


def test_proxy_diagnosis_uses_build_env_not_assumption() -> None:
    text = diagnose_proxy_path(
        env={"HTTPS_PROXY": "http://127.0.0.1:7890", "NO_PROXY": "localhost"},
        command="docker pull ghcr.io/example/app:1",
    )
    assert "127.0.0.1" in text
    assert "ghcr.io" in text
    bare = diagnose_proxy_path(env={}, command="npm install")
    assert "未设置" in bare


def test_docker_error_summary_proxy_only_on_network_failures() -> None:
    from local_webpage_access.docker_runtime import _error_summary_with_hint

    syntax = _error_summary_with_hint("Dockerfile:3 unknown instruction")
    assert "HTTP_PROXY" not in syntax
    net = _error_summary_with_hint(
        "failed to resolve docker.io/library/python:3.13-slim: i/o timeout"
    )
    assert "docker.io" in net
    assert "代理" in net or "HTTP_PROXY" in net or "构建环境" in net
