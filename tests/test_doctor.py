"""doctor 模块测试（WBS-26 lwa doctor 与排障辅助）。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence

import pytest

from local_webpage_access.config import load_config
from local_webpage_access.doctor import (
    STATUS_FAIL,
    STATUS_OK,
    STATUS_SKIP,
    STATUS_WARN,
    CheckResult,
    diagnose_instance,
    check_docker,
    check_docker_compose,
    check_caddy,
    check_caddy_health,
    check_lan_url_stale,
    check_backend_handoff,
    check_port_contention,
    check_disk_space,
    check_memory,
    check_port_pool,
    check_python_packages,
    check_python_version,
    check_registry,
    check_static_gateway,
    format_report,
    run_doctor,
)
from local_webpage_access.init_workspace import init_workspace
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry


# ---- 辅助：可注入的假 runner / port checker --------------------------------


def _proc(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _runner_from_map(
    mapping: dict[tuple[str, ...], subprocess.CompletedProcess],
):
    """构造一个按 args 前缀匹配返回结果的 runner。"""

    def runner(args: Sequence[str]):
        key = tuple(args)
        # 精确匹配优先，再退到前缀
        if key in mapping:
            return mapping[key]
        for prefix, result in mapping.items():
            if key[: len(prefix)] == prefix:
                return result
        return _proc(127, stderr="not found")

    return runner


def _failing_runner(args: Sequence[str]) -> subprocess.CompletedProcess:
    return _proc(127, stderr="command not found")


def _all_ports_free(port: int) -> bool:
    return False


def _port_busy(port: int) -> bool:
    return True


# ---- 通用 fixture ---------------------------------------------------------


@pytest.fixture
def env(tmp_path: Path):
    """初始化一个工作区，返回 (ws, config, reg)。"""
    root = tmp_path / "ws"
    init_workspace(root)
    ws = Workspace(root)
    config = load_config(ws)
    reg = Registry(ws.db_path)
    reg.open()
    yield ws, config, reg
    reg.close()


# ---- WBS-26.02 Python 版本 ------------------------------------------------


def test_check_python_version_ok() -> None:
    r = check_python_version()
    assert r.status == STATUS_OK
    assert "Python" in r.message


def test_pid_alive_local_windows_does_not_os_kill(monkeypatch) -> None:
    """BUG-178：Windows 上 _pid_alive_local 用 OpenProcess(SYNCHRONIZE)，不调 os.kill。

    os.kill(pid, 0) 在 Windows 走 TerminateProcess 会真的杀进程；只读诊断
    （check_caddy_health 探 caddy.pid）若走它将误杀 Caddy master。
    """
    import ctypes
    import os

    import local_webpage_access.doctor as doc

    monkeypatch.setattr(doc.sys, "platform", "win32")
    killed: list[tuple] = []
    monkeypatch.setattr(doc.os, "kill", lambda *a, **k: killed.append(a))

    class _FakeKernelAlive:
        def OpenProcess(self, access, inherit, pid):
            return 42  # 非零句柄 → 视为存活

        def CloseHandle(self, handle):
            return 1

    class _FakeKernelDead:
        def OpenProcess(self, access, inherit, pid):
            return 0  # 零句柄 → 进程不存在

        def CloseHandle(self, handle):
            return 1

    monkeypatch.setattr(
        ctypes, "WinDLL", lambda name, **kw: _FakeKernelAlive(), raising=False
    )
    assert doc._pid_alive_local(1234) is True
    monkeypatch.setattr(
        ctypes, "WinDLL", lambda name, **kw: _FakeKernelDead(), raising=False
    )
    assert doc._pid_alive_local(1234) is False
    # 关键：win32 分支绝不走 os.kill（否则 TerminateProcess 误杀）
    assert killed == []


# ---- WBS-26.03 Docker -----------------------------------------------------


def test_check_docker_ok() -> None:
    runner = _runner_from_map(
        {("docker", "version"): _proc(0, stdout="29.6.1\n")}
    )
    r = check_docker(runner=runner)
    assert r.status == STATUS_OK
    assert "29.6.1" in r.message


def test_check_docker_version_too_low() -> None:
    runner = _runner_from_map(
        {("docker", "version"): _proc(0, stdout="28.9.9\n")}
    )
    r = check_docker(runner=runner)
    assert r.status == STATUS_FAIL
    assert "29.0.0" in r.message


def test_check_docker_unavailable() -> None:
    """WBS-26 验收：Docker 未安装时 doctor 能明确指出。"""
    r = check_docker(runner=_failing_runner)
    assert r.status == STATUS_FAIL
    assert "Docker 不可用" in r.message
    assert r.suggestion is not None


def test_check_docker_daemon_down() -> None:
    runner = _runner_from_map(
        {("docker", "version"): _proc(1, stderr="Cannot connect to the Docker daemon")}
    )
    r = check_docker(runner=runner)
    assert r.status == STATUS_FAIL


def test_check_docker_permission_denied_suggests_newgrp() -> None:
    """BUG-230：docker.sock 权限不足时 doctor 给出 newgrp + 重启 manager/daemon 指引。"""
    runner = _runner_from_map(
        {
            ("docker", "version"): _proc(
                1,
                stderr=(
                    "permission denied while trying to connect to the Docker daemon "
                    "socket at unix:///var/run/docker.sock"
                ),
            )
        }
    )
    r = check_docker(runner=runner)
    assert r.status == STATUS_FAIL
    assert "权限不足" in r.message
    assert r.suggestion is not None
    assert "newgrp" in r.suggestion
    assert "manager" in r.suggestion


# ---- WBS-26.04 Docker Compose ---------------------------------------------


def test_check_docker_compose_v2_ok() -> None:
    runner = _runner_from_map(
        {("docker", "compose", "version"): _proc(0, stdout="v5.2.0\n")}
    )
    r = check_docker_compose(runner=runner)
    assert r.status == STATUS_OK
    assert "5.2.0" in r.message


def test_check_docker_compose_supported_v2_warns_without_failing() -> None:
    runner = _runner_from_map(
        {("docker", "compose", "version"): _proc(0, stdout="v2.40.3\n")}
    )
    r = check_docker_compose(runner=runner)
    assert r.status == STATUS_WARN
    assert "推荐" in r.message
    assert "5.2.0" in (r.suggestion or "")


def test_check_docker_compose_version_too_low() -> None:
    runner = _runner_from_map(
        {("docker", "compose", "version"): _proc(0, stdout="2.39.9\n")}
    )
    r = check_docker_compose(runner=runner)
    assert r.status == STATUS_FAIL
    assert "2.40.2" in r.message


def test_check_docker_compose_unavailable() -> None:
    r = check_docker_compose(runner=_failing_runner)
    assert r.status == STATUS_FAIL
    assert "Compose" in r.message


def test_check_docker_compose_v1_fallback_fails() -> None:
    runner = _runner_from_map(
        {
            ("docker", "compose", "version"): _proc(1, stderr="no such command"),
            ("docker-compose", "version"): _proc(0, stdout="1.29.2\n"),
        }
    )
    r = check_docker_compose(runner=runner)
    assert r.status == STATUS_FAIL
    assert "v1" in r.message


# ---- Caddy / Python 包版本 --------------------------------------------------


def test_check_caddy_skipped_for_builtin(env) -> None:
    _ws, config, _reg = env
    config.staticGateway = "builtin"
    r = check_caddy(config, runner=_failing_runner)
    assert r.status == STATUS_SKIP


def test_check_caddy_required_and_ok(env, monkeypatch) -> None:
    _ws, config, _reg = env
    config.staticGateway = "caddy"
    monkeypatch.setattr("local_webpage_access.doctor.shutil.which", lambda _: "/usr/bin/caddy")
    runner = _runner_from_map({("caddy", "version"): _proc(0, stdout="v2.10.0\n")})
    r = check_caddy(config, runner=runner)
    assert r.status == STATUS_OK


def test_check_caddy_missing_warns_and_falls_back(env, monkeypatch) -> None:
    """默认 caddy 缺失时运行时会降级 builtin，doctor 只能告警。"""
    _ws, config, _reg = env
    config.staticGateway = "caddy"
    monkeypatch.setattr("local_webpage_access.doctor.shutil.which", lambda _: None)

    r = check_caddy(config, runner=_failing_runner)

    assert r.status == STATUS_WARN
    assert "builtin" in r.message


def test_check_caddy_version_too_low(env, monkeypatch) -> None:
    _ws, config, _reg = env
    config.staticGateway = "caddy"
    monkeypatch.setattr("local_webpage_access.doctor.shutil.which", lambda _: "/usr/bin/caddy")
    runner = _runner_from_map({("caddy", "version"): _proc(0, stdout="v2.9.9\n")})
    r = check_caddy(config, runner=runner)
    assert r.status == STATUS_FAIL


def test_check_python_packages_ok() -> None:
    from importlib.metadata import version as pkg_version

    from local_webpage_access.version_requirements import (
        MIN_FASTAPI_VERSION,
        MIN_UVICORN_VERSION,
        version_ge,
    )

    if not version_ge(pkg_version("fastapi"), MIN_FASTAPI_VERSION):
        pytest.skip("fastapi 版本低于项目最低要求")
    if not version_ge(pkg_version("uvicorn"), MIN_UVICORN_VERSION):
        pytest.skip("uvicorn 版本低于项目最低要求")
    r = check_python_packages()
    assert r.status == STATUS_OK


# ---- WBS-26.05 端口池 -----------------------------------------------------


def test_check_port_pool_all_free() -> None:
    from local_webpage_access.config import Config

    cfg = Config()  # 默认配置
    r = check_port_pool(cfg, port_in_use=_all_ports_free)
    assert r.status == STATUS_OK


def test_check_port_pool_conflict() -> None:
    """WBS-26 验收：端口冲突时 doctor 能定位。"""
    from local_webpage_access.config import Config

    cfg = Config()
    r = check_port_pool(cfg, port_in_use=_port_busy)
    assert r.status == STATUS_FAIL
    assert "占用" in r.message
    assert r.suggestion is not None


def test_check_port_pool_ignores_allocated_instance_ports() -> None:
    """BUG-039：当前工作区实例已登记端口不应被 doctor 判为冲突。"""
    from local_webpage_access.config import Config, PortPool

    cfg = Config(portPool=PortPool(start=21000, end=21010))

    def busy_allocated_only(port: int) -> bool:
        return port == 21000

    r = check_port_pool(
        cfg, port_in_use=busy_allocated_only, allocated_ports={21000}
    )
    assert r.status == STATUS_OK


def test_check_port_pool_excludes_manager_port_when_busy() -> None:
    """建议 H：managerPort 由 lwa 自用，被占用不应判为端口池冲突。"""
    from local_webpage_access.config import Config, PortPool

    cfg = Config(managerPort=22000, portPool=PortPool(start=21000, end=21010))

    def manager_busy(port: int) -> bool:
        return port == 22000

    r = check_port_pool(cfg, port_in_use=manager_busy, allocated_ports={22000})
    assert r.status == STATUS_OK  # managerPort 是自用端口，排除


def test_check_port_pool_excludes_static_gateway_port_when_busy() -> None:
    """建议 H：staticGatewayPort（别名入口）由 caddy 自用，不判为冲突。"""
    from local_webpage_access.config import Config, PortPool

    cfg = Config(
        staticGatewayPort=8090, portPool=PortPool(start=21000, end=21010)
    )

    def entry_busy(port: int) -> bool:
        return port == 8090

    r = check_port_pool(cfg, port_in_use=entry_busy)
    assert r.status == STATUS_OK


def test_check_port_pool_flags_external_squatter_in_pool() -> None:
    """建议 H：端口池范围内的外部占用仍应判 FAIL。"""
    from local_webpage_access.config import Config, PortPool

    cfg = Config(portPool=PortPool(start=21000, end=21010))

    def external_busy(port: int) -> bool:
        return port == 21005  # 非自用、非已分配

    r = check_port_pool(cfg, port_in_use=external_busy)
    assert r.status == STATUS_FAIL
    assert "21005" in (r.detail or "")


def test_check_port_pool_custom_config() -> None:
    from local_webpage_access.config import Config

    cfg = Config()
    cfg.portPool.start = 21000
    cfg.portPool.end = 21010
    r = check_port_pool(cfg, port_in_use=_all_ports_free)
    assert r.status == STATUS_OK
    assert "21000" in r.message


def test_default_port_in_use_detects_wildcard_listener() -> None:
    """BUG-029 回归：_default_port_in_use 不得用 SO_REUSEADDR。

    此前 _default_port_in_use 自行 setsockopt(SO_REUSEADDR)，在 Windows 上会
    把已监听端口判为空闲（BUG-002 的回归）。修复后应委托给 is_port_in_use，
    对 0.0.0.0 监听的端口如实返回 True。
    """
    import socket

    from local_webpage_access.doctor import _default_port_in_use

    s = socket.socket()
    s.bind(("0.0.0.0", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert _default_port_in_use(port) is True
    finally:
        s.close()


# ---- WBS-26.06 SQLite registry --------------------------------------------


def test_check_registry_ok(env) -> None:
    ws, _config, _reg = env
    # 关闭后再检查（doctor 自行打开）
    r = check_registry(ws)
    assert r.status == STATUS_OK
    assert "schema" in r.message


def test_check_registry_missing_db(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    ws = Workspace(root)
    ws.ensure_workspace_dirs()
    r = check_registry(ws)
    assert r.status == STATUS_FAIL
    assert "不存在" in r.message


# ---- WBS-26.07 静态网关 ----------------------------------------------------


def test_check_static_gateway_ok(env) -> None:
    ws, _config, _reg = env
    r = check_static_gateway(ws)
    assert r.status == STATUS_OK


def test_check_static_gateway_missing(tmp_path: Path) -> None:
    ws = Workspace(tmp_path / "ws")
    # 不创建目录
    r = check_static_gateway(ws)
    assert r.status == STATUS_WARN


# ---- WBS-26.08 磁盘空间 ----------------------------------------------------


def test_check_disk_space_ok(env) -> None:
    ws, _config, _reg = env
    r = check_disk_space(ws)
    assert r.status in (STATUS_OK, STATUS_WARN)
    assert "GB" in r.message


def test_check_disk_space_high_threshold_triggers_fail(env) -> None:
    ws, _config, _reg = env
    r = check_disk_space(ws, min_gb=1e9)  # 不可能满足
    assert r.status == STATUS_FAIL
    assert r.suggestion is not None


# ---- WBS-26.09 内存 --------------------------------------------------------


def test_check_memory_returns_result() -> None:
    r = check_memory()
    # 在 CI 上可能 SKIP（无 psutil 且非 Linux），但不应抛异常
    assert r.status in (STATUS_OK, STATUS_WARN, STATUS_FAIL, STATUS_SKIP)


# ---- WBS-26.10/11 单实例诊断 -----------------------------------------------


def test_diagnose_instance_unknown_id(env) -> None:
    ws, _config, reg = env
    results = diagnose_instance(ws, reg, "does-not-exist")
    assert results
    assert any(r.status == STATUS_FAIL for r in results)


def test_diagnose_instance_invalid_id(env) -> None:
    ws, _config, reg = env
    results = diagnose_instance(ws, reg, "../etc/passwd")
    assert results
    assert all(r.status == STATUS_FAIL for r in results)


def _import_static(env):
    """导入一个可识别的静态 zip，返回 instance_id。"""
    import zipfile

    from local_webpage_access.importer import Importer

    ws, config, reg = env
    zip_path = ws.inbox / "demo.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("index.html", "<h1>hello</h1>")
    importer = Importer(ws, config, reg)
    result = importer.import_zip(str(zip_path))
    return result.instance_id


def test_diagnose_instance_known(env) -> None:
    ws, _config, reg = env
    instance_id = _import_static(env)
    results = diagnose_instance(ws, reg, instance_id)
    statuses = [r.status for r in results]
    # manifest 与文件应 ok；logs 可能 warn（未启动）
    assert STATUS_OK in statuses
    # 不应有 fail
    assert STATUS_FAIL not in statuses


def test_diagnose_instance_failed_status(env) -> None:
    """WBS-26 验收：实例失败时 doctor 能列出最近错误与建议。"""
    from tests._helpers import make_static_manifest

    ws, _config, reg = env
    reg.upsert_from_manifest(make_static_manifest("broken"))
    reg.update_status("broken", "failed", last_error="健康检查连续失败")
    results = diagnose_instance(ws, reg, "broken")
    status_check = [r for r in results if r.name.endswith(":status")]
    assert status_check
    assert status_check[0].status == STATUS_FAIL
    assert status_check[0].detail == "健康检查连续失败"
    assert status_check[0].suggestion is not None


# ---- run_doctor 聚合 -------------------------------------------------------


def test_run_doctor_full_report(env) -> None:
    ws, config, _reg = env
    report = run_doctor(
        ws, config, runner=_runner_from_map(
            {
                ("docker", "version"): _proc(0, stdout="29.6.1\n"),
                ("docker", "compose", "version"): _proc(0, stdout="5.2.1\n"),
            }
        ),
        port_in_use=_all_ports_free,
    )
    assert len(report.checks) >= 10
    # Python + registry + static_gateway + disk 应 ok
    names = [c.name for c in report.checks]
    assert "python_version" in names
    assert "python_packages" in names
    assert "docker" in names
    assert "docker_compose" in names
    assert "caddy" in names
    assert "port_pool" in names
    assert "registry" in names
    assert "disk_space" in names


def test_run_doctor_with_instance(env) -> None:
    ws, config, _reg = env
    instance_id = _import_static(env)
    report = run_doctor(
        ws, config, instance_id=instance_id,
        runner=_failing_runner, port_in_use=_all_ports_free,
    )
    assert report.instance_id == instance_id
    assert len(report.instance_checks) > 0


def test_run_doctor_overall_fail(env) -> None:
    ws, config, _reg = env
    report = run_doctor(
        ws, config, runner=_failing_runner, port_in_use=_all_ports_free
    )
    # Docker / Compose 失败 → overall 应 fail
    assert report.has_failures
    assert report.overall == STATUS_FAIL
    assert len(report.failures()) >= 2


def test_format_report_renders(env) -> None:
    ws, config, _reg = env
    report = run_doctor(
        ws, config, runner=_failing_runner, port_in_use=_all_ports_free
    )
    text = format_report(report)
    assert "环境检查" in text
    assert "总体" in text
    assert "FAIL" in text or "WARN" in text or "OK" in text


def test_check_result_to_dict_and_passed() -> None:
    r = CheckResult("x", STATUS_OK, "ok", detail="d", suggestion="s")
    d = r.to_dict()
    assert d == {
        "name": "x", "status": "ok", "message": "ok", "detail": "d", "suggestion": "s"
    }
    assert r.passed
    fail = CheckResult("x", STATUS_FAIL, "boom")
    assert not fail.passed


# ---- CLI 端到端 ------------------------------------------------------------


def test_cli_doctor_command(env, monkeypatch) -> None:
    """`lwa doctor` 应可执行并返回 0（环境基本健康时）。"""
    from typer.testing import CliRunner

    from local_webpage_access.cli import app

    ws, _config, _reg = env
    monkeypatch.chdir(ws.root)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    # Docker 可能不可用 → 可能 exit 1，但不应崩溃
    assert result.exit_code in (0, 1)
    assert "环境检查" in result.output or "总体" in result.output


def test_cli_doctor_json_output(env, monkeypatch) -> None:
    from typer.testing import CliRunner

    from local_webpage_access.cli import app

    ws, _config, _reg = env
    monkeypatch.chdir(ws.root)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--json"])
    import json

    # BUG-241 / IMP-036：--json 纯 stdout；stderr 可能有日志，勿用 result.output
    data = json.loads(result.stdout)
    assert "overall" in data
    assert "checks" in data
    assert isinstance(data["checks"], list)
    assert len(data["checks"]) >= 8
    assert "platformSupport" in data
    for key in (
        "platform",
        "distroId",
        "architecture",
        "supported",
        "reasons",
        "action",
    ):
        assert key in data["platformSupport"]


def test_cli_doctor_full_json_stdout_is_pure_json(env, monkeypatch) -> None:
    """BUG-241：--json --profile full 不得混入 Full Profile 人类文本。"""
    from typer.testing import CliRunner

    from local_webpage_access.cli import app

    ws, _config, _reg = env
    monkeypatch.chdir(ws.root)
    result = CliRunner().invoke(app, ["doctor", "--json", "--profile", "full"])
    import json

    data = json.loads(result.stdout)
    assert "checks" in data
    assert "capabilities" in data
    assert "[Full Profile]" not in result.stdout


def test_cli_doctor_json_valid_when_caddy_hidden(env, monkeypatch) -> None:
    """BUG-075：caddy 缺失时 check_caddy_health 不得 log.warning 污染 --json 输出。"""
    from typer.testing import CliRunner

    from local_webpage_access.cli import app

    ws, _config, _reg = env
    # 默认配置即 staticGateway=caddy；隐藏 caddy 使 check_caddy_health 走"降级 builtin"WARN
    monkeypatch.setattr(
        "local_webpage_access.doctor.shutil.which", lambda name: None
    )
    monkeypatch.chdir(ws.root)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--json"])
    import json

    data = json.loads(result.output)  # 不应 JSONDecodeError（无 warning 污染 stdout）
    assert "checks" in data
    names = {c["name"]: c["status"] for c in data["checks"]}
    assert names.get("caddy_health") == "warn"


# ---- IMP-020：Caddy 健康探针 -------------------------------------------------


def _patch_static_gateway(monkeypatch, ws, *, backend="caddy", admin_alive=True, health=None):
    """把 doctor 内 lazy import 的 StaticGateway 换成可控替身，返回共享状态。

    同时把 ``doctor.shutil.which`` 桩为 caddy 存在，使 ok/fail/validate/stale
    用例确定性地越过 check_caddy_health 的 caddy-缺失 WARN 分支（不依赖机器是否
    装了 caddy）。``health`` 为 ``port -> bool``，控制 IMP-020 站点/入口探测结果。
    """
    state = {"backend": backend, "admin_alive": admin_alive, "health": health}

    class _Fake:
        def __init__(self, w, c):
            self._ws = w

        def detect_backend(self):
            return state["backend"]

        def _admin_alive(self, **kw):
            return state["admin_alive"]

        def health_check(self, port, *, timeout=1.0, path="/", **kw):
            fn = state["health"]
            return bool(fn(port, path)) if fn else True

        def main_config_path(self):
            return ws.static_gateway / "Caddyfile"

        def caddy_pid_path(self):
            return ws.run / "caddy.pid"

    monkeypatch.setattr("local_webpage_access.static_gateway.StaticGateway", _Fake)
    monkeypatch.setattr(
        "local_webpage_access.doctor.shutil.which", lambda name: "/usr/bin/caddy"
    )
    return state


def test_check_caddy_health_skip_for_builtin(env, monkeypatch) -> None:
    ws, config, _reg = env
    config.staticGateway = "builtin"
    r = check_caddy_health(ws, config, runner=_failing_runner)
    assert r.status == STATUS_SKIP


def test_check_caddy_health_warns_when_caddy_missing(env, monkeypatch) -> None:
    """staticGateway=caddy 但 PATH 无 caddy（shutil.which=None）→ WARN，不污染 stdout。"""
    ws, config, _reg = env
    config.staticGateway = "caddy"
    monkeypatch.setattr("local_webpage_access.doctor.shutil.which", lambda name: None)
    r = check_caddy_health(ws, config, runner=_failing_runner)
    assert r.status == STATUS_WARN
    assert "builtin" in r.message


def test_check_caddy_health_ok_when_admin_up_and_validate_passes(
    env, monkeypatch
) -> None:
    ws, config, _reg = env
    config.staticGateway = "caddy"
    _patch_static_gateway(monkeypatch, ws, backend="caddy", admin_alive=True)
    main = ws.static_gateway / "Caddyfile"
    main.parent.mkdir(parents=True, exist_ok=True)
    main.write_text(":8080 {}\n")
    runner = _runner_from_map({("caddy", "validate"): _proc(0, stdout="valid")})
    r = check_caddy_health(ws, config, runner=runner)
    assert r.status == STATUS_OK


def test_check_caddy_health_fail_when_admin_down(env, monkeypatch) -> None:
    """admin :2019 不可达 → FAIL，suggestion 提示 lwa gateway on。"""
    ws, config, _reg = env
    config.staticGateway = "caddy"
    _patch_static_gateway(monkeypatch, ws, backend="caddy", admin_alive=False)
    runner = _runner_from_map({("caddy", "validate"): _proc(0)})
    r = check_caddy_health(ws, config, runner=runner)
    assert r.status == STATUS_FAIL
    assert "gateway on" in (r.suggestion or "")


def test_check_caddy_health_fail_when_validate_fails(env, monkeypatch) -> None:
    """主 Caddyfile validate 失败 → FAIL，提示悬空 import（BUG-069）。"""
    ws, config, _reg = env
    config.staticGateway = "caddy"
    _patch_static_gateway(monkeypatch, ws, backend="caddy", admin_alive=True)
    main = ws.static_gateway / "Caddyfile"
    main.parent.mkdir(parents=True, exist_ok=True)
    main.write_text("bad config")
    runner = _runner_from_map(
        {("caddy", "validate"): _proc(1, stderr="Caddyfile:3 invalid directive")}
    )
    r = check_caddy_health(ws, config, runner=runner)
    assert r.status == STATUS_FAIL
    assert "validate" in r.message


def test_check_caddy_health_warn_on_stale_pid(env, monkeypatch) -> None:
    """admin 在线、validate 通过，但 caddy.pid 指向已死进程 → WARN（BUG-070）。"""
    ws, config, _reg = env
    config.staticGateway = "caddy"
    _patch_static_gateway(monkeypatch, ws, backend="caddy", admin_alive=True)
    runner = _runner_from_map({("caddy", "validate"): _proc(0)})
    pid_path = ws.run / "caddy.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("999999999")  # 几乎不可能存活的 pid
    r = check_caddy_health(ws, config, runner=runner)
    assert r.status == STATUS_WARN


# ---- IMP-020：master 在线时的站点/入口可达性探测 -----------------------------


def _seed_static_for_doctor(ws, reg, iid, *, host_port, alias=None, lan_url=None):
    """落一个 enabled 静态实例到 registry，供 check_caddy_health 站点探测。"""
    from local_webpage_access.models import (
        DesiredState,
        InstanceManifest,
        Kind,
        NetworkConfig,
        ResourceProfile,
        Runtime,
        ServingMode,
        StaticConfig,
        Status,
    )

    ws.ensure_app_dirs(iid)
    static = StaticConfig(hostPort=host_port, enabled=True)
    if alias:
        static.routeMode = "name"
        static.routeHost = alias
    network = NetworkConfig(hostPort=host_port)
    if alias:
        network.routeMode = "name"
        network.routeHost = alias
    if lan_url:
        network.lanUrl = lan_url
    m = InstanceManifest(
        id=iid,
        name=iid,
        version="1",
        kind=Kind.STATIC,
        runtime=Runtime.SHARED_STATIC,
        servingMode=ServingMode.SHARED_STATIC,
        resourceProfile=ResourceProfile.TINY,
        desiredState=DesiredState.RUNNING,
        status=Status.RUNNING,
        static=static,
        network=network,
    )
    m.save(ws.app_manifest_path(iid))
    reg.upsert_from_manifest(m)
    reg.set_static_enabled(iid, True)


def test_check_caddy_health_warns_on_unreachable_site(env, monkeypatch) -> None:
    """IMP-020：master 在线但 enabled 站点 hostPort 不可达 → WARN。"""
    ws, config, reg = env
    config.staticGateway = "caddy"
    _seed_static_for_doctor(ws, reg, "demo", host_port=21100)
    _patch_static_gateway(
        monkeypatch, ws, backend="caddy", admin_alive=True, health=lambda p, path: False
    )
    main = ws.static_gateway / "Caddyfile"
    main.parent.mkdir(parents=True, exist_ok=True)
    main.write_text(":8080 {}\n")
    runner = _runner_from_map({("caddy", "validate"): _proc(0, stdout="valid")})
    r = check_caddy_health(ws, config, runner=runner, registry=reg)
    assert r.status == STATUS_WARN
    assert "21100" in r.message


def test_check_caddy_health_warns_on_unreachable_alias_entry(env, monkeypatch) -> None:
    """IMP-020：有别名但 :staticGatewayPort 入口不可达 → WARN。"""
    ws, config, reg = env
    config.staticGateway = "caddy"
    _seed_static_for_doctor(ws, reg, "demo", host_port=21100, alias="myapp")
    # 站点 hostPort 可达，但别名入口端口不可达（任何路径都不通）
    entry_port = config.staticGatewayPort

    def _health(port, path="/"):
        return port != entry_port

    _patch_static_gateway(
        monkeypatch, ws, backend="caddy", admin_alive=True, health=_health
    )
    main = ws.static_gateway / "Caddyfile"
    main.parent.mkdir(parents=True, exist_ok=True)
    main.write_text(":8080 {}\n")
    runner = _runner_from_map({("caddy", "validate"): _proc(0, stdout="valid")})
    r = check_caddy_health(ws, config, runner=runner, registry=reg)
    assert r.status == STATUS_WARN
    assert str(entry_port) in r.message


def test_check_caddy_health_entry_probe_uses_alias_path(env, monkeypatch) -> None:
    """BUG-080：入口探测应打 /<alias>/ 而非 /（根路径无路由会 404 误报 WARN）。"""
    ws, config, reg = env
    config.staticGateway = "caddy"
    _seed_static_for_doctor(ws, reg, "demo", host_port=21100, alias="myapp")
    entry_port = config.staticGatewayPort
    seen: list[str] = []

    def _health(port, path="/"):
        seen.append(path)
        # 站点 hostPort(21100) 正常服务；入口端口仅 /myapp/ 可达（模拟别名路由），
        # 根 / 返回 404——BUG-080 正是要求入口探测打 /myapp/ 而非 /
        if port == 21100:
            return True
        return path == "/myapp/"

    _patch_static_gateway(
        monkeypatch, ws, backend="caddy", admin_alive=True, health=_health
    )
    main = ws.static_gateway / "Caddyfile"
    main.parent.mkdir(parents=True, exist_ok=True)
    main.write_text(":8080 {}\n")
    runner = _runner_from_map({("caddy", "validate"): _proc(0, stdout="valid")})
    r = check_caddy_health(ws, config, runner=runner, registry=reg)
    assert r.status == STATUS_OK  # 不因根路径 404 误报
    assert "/myapp/" in seen


def test_check_caddy_health_ok_when_all_sites_reachable(env, monkeypatch) -> None:
    """IMP-020：master 在线 + 站点/入口均可达 → OK。"""
    ws, config, reg = env
    config.staticGateway = "caddy"
    _seed_static_for_doctor(ws, reg, "demo", host_port=21100)
    _patch_static_gateway(
        monkeypatch, ws, backend="caddy", admin_alive=True, health=lambda p, path: True
    )
    main = ws.static_gateway / "Caddyfile"
    main.parent.mkdir(parents=True, exist_ok=True)
    main.write_text(":8080 {}\n")
    runner = _runner_from_map({("caddy", "validate"): _proc(0, stdout="valid")})
    r = check_caddy_health(ws, config, runner=runner, registry=reg)
    assert r.status == STATUS_OK


# ---- 建议项 F：lan_url_stale / backend_handoff / port_contention -----------


def test_check_lan_url_stale_warns_on_drift(env, monkeypatch) -> None:
    """lanUrl host 与当前 LAN IP 不一致 → WARN。"""
    ws, config, reg = env
    _seed_static_for_doctor(ws, reg, "demo", host_port=18000,
                            lan_url="http://10.0.0.99:18000")
    monkeypatch.setattr("local_webpage_access.ports.resolve_lan_ip",
                        lambda cfg: "192.168.1.50")
    r = check_lan_url_stale(ws, config, reg)
    assert r.status == STATUS_WARN
    assert "refresh" in (r.suggestion or "")
    assert "demo" in (r.detail or "")


def test_check_lan_url_stale_ok_when_current(env, monkeypatch) -> None:
    """lanUrl host 与当前 LAN IP 一致 → OK。"""
    ws, config, reg = env
    _seed_static_for_doctor(ws, reg, "demo", host_port=18000,
                            lan_url="http://192.168.1.50:18000")
    monkeypatch.setattr("local_webpage_access.ports.resolve_lan_ip",
                        lambda cfg: "192.168.1.50")
    r = check_lan_url_stale(ws, config, reg)
    assert r.status == STATUS_OK


def test_check_port_contention_skips_for_builtin(env) -> None:
    """builtin 后端不占用 :2019/别名入口 → SKIP。"""
    ws, config, _reg = env
    config.staticGateway = "builtin"
    r = check_port_contention(ws, config)
    assert r.status == STATUS_SKIP


def test_check_port_contention_detects_orphan_admin(env, monkeypatch) -> None:
    """§2.7：:2019 被非本工作区进程占用（测试孤儿）且 admin 不可达 → FAIL。"""
    ws, config, _reg = env
    config.staticGateway = "caddy"
    # 无 caddy.pid（caddy_pid=None），:2019 被某 caddy 占，admin 不可达（非健康 master）
    monkeypatch.setattr("local_webpage_access.doctor._list_listeners",
                        lambda port: [("caddy", "75224")] if port == 2019 else [])

    class _FakeGateway:
        def __init__(self, w, c):
            self._ws = w

        def caddy_pid_path(self):
            return self._ws.run / "caddy.pid"  # 不存在

        def _admin_alive(self, **kw):
            return False

    # check_port_contention 内部 lazy `from static_gateway import StaticGateway`
    monkeypatch.setattr("local_webpage_access.static_gateway.StaticGateway",
                        _FakeGateway)
    r = check_port_contention(ws, config)
    assert r.status == STATUS_FAIL
    assert "2019" in r.message


def test_check_port_contention_fails_on_mixed_entry_listeners(
    env, monkeypatch
) -> None:
    """BUG-107：别名入口同时有 caddy 与 python 监听 → FAIL（不得因有 caddy 放行）。"""
    ws, config, _reg = env
    config.staticGateway = "caddy"
    entry = int(config.staticGatewayPort)

    def fake_listeners(port):
        if port == entry:
            return [("caddy", "1"), ("python", "2")]
        return []

    monkeypatch.setattr(
        "local_webpage_access.doctor._list_listeners", fake_listeners
    )

    class _FakeGateway:
        def __init__(self, w, c):
            self._ws = w

        def caddy_pid_path(self):
            return self._ws.run / "caddy.pid"

        def _admin_alive(self, **kw):
            return True

    monkeypatch.setattr(
        "local_webpage_access.static_gateway.StaticGateway", _FakeGateway
    )
    r = check_port_contention(ws, config)
    assert r.status == STATUS_FAIL
    assert str(entry) in r.message
    assert "python" in r.message


def test_check_port_contention_ok_when_entry_only_caddy(
    env, monkeypatch
) -> None:
    """别名入口仅 caddy 监听 → OK。"""
    ws, config, _reg = env
    config.staticGateway = "caddy"
    entry = int(config.staticGatewayPort)

    monkeypatch.setattr(
        "local_webpage_access.doctor._list_listeners",
        lambda port: [("caddy", "1")] if port == entry else [],
    )

    class _FakeGateway:
        def __init__(self, w, c):
            self._ws = w

        def caddy_pid_path(self):
            return self._ws.run / "caddy.pid"

        def _admin_alive(self, **kw):
            return True

    monkeypatch.setattr(
        "local_webpage_access.static_gateway.StaticGateway", _FakeGateway
    )
    r = check_port_contention(ws, config)
    assert r.status == STATUS_OK


def test_check_backend_handoff_detects_double_serve(env, monkeypatch) -> None:
    """同一 hostPort 上 caddy + python 同时监听 → FAIL（切换残留）。"""
    ws, config, reg = env
    config.staticGateway = "caddy"
    _seed_static_for_doctor(ws, reg, "demo", host_port=18000,
                            lan_url="http://127.0.0.1:18000")

    def fake_listeners(port):
        if port == 18000:
            return [("caddy", "1"), ("python", "2")]
        return []

    monkeypatch.setattr("local_webpage_access.doctor._list_listeners",
                        fake_listeners)
    r = check_backend_handoff(ws, config, reg)
    assert r.status == STATUS_FAIL
    assert "18000" in (r.detail or "")


def test_check_backend_handoff_ok_single_listener(env, monkeypatch) -> None:
    """hostPort 仅 caddy 监听（无 builtin 残留）→ OK。"""
    ws, config, reg = env
    config.staticGateway = "caddy"
    _seed_static_for_doctor(ws, reg, "demo", host_port=18000,
                            lan_url="http://127.0.0.1:18000")

    monkeypatch.setattr("local_webpage_access.doctor._list_listeners",
                        lambda port: [("caddy", "1")] if port == 18000 else [])
    r = check_backend_handoff(ws, config, reg)
    assert r.status == STATUS_OK


# ---- BUG-093：cli 包 -m 执行入口 ------------------------------------------


def test_cli_dash_m_executable() -> None:
    """BUG-093：``python3 -m local_webpage_access.cli`` 必须可执行。

    DEV-044 把单文件 ``cli.py`` 拆成 ``cli/`` 包后缺少 ``__main__.py``，``-m`` 执行
    报 ``No module named local_webpage_access.cli.__main__``。
    """
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "local_webpage_access.cli", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    # typer --help 输出包含根 app 用法
    assert "Usage:" in result.stdout or "Usage:" in result.stderr


# ---- IMP-038/040：doctor --access 与 JSON 漂移字段 ---------------------------


def test_run_doctor_json_fields_current_lan_and_drifted(env, monkeypatch) -> None:
    """040.06：DoctorReport 暴露 currentLanIp / driftedInstanceIds。"""
    from local_webpage_access.doctor import run_doctor

    ws, config, reg = env
    _seed_static_for_doctor(
        ws, reg, "demo", host_port=18000, lan_url="http://10.0.0.99:18000"
    )
    monkeypatch.setattr(
        "local_webpage_access.ports.resolve_lan_ip", lambda cfg: "192.168.1.50"
    )
    report = run_doctor(ws, config)
    assert report.current_lan_ip == "192.168.1.50"
    assert "demo" in report.drifted_instance_ids


def test_doctor_access_reuses_review_access(env, monkeypatch) -> None:
    """038.03：doctor --access 委托 review_access，不重写探测。"""
    from local_webpage_access.access import AccessReviewReport
    from local_webpage_access.doctor import run_doctor

    ws, config, reg = env
    _seed_static_for_doctor(ws, reg, "demo", host_port=18000)
    called = {"n": 0}

    def fake_review(ws_, cfg, registry):
        called["n"] += 1
        return AccessReviewReport(lan_ip="192.168.1.50")

    monkeypatch.setattr(
        "local_webpage_access.access_workflow.review_access", fake_review
    )
    report = run_doctor(ws, config, access_review=True)
    assert called["n"] == 1
    assert report.access_review is not None
    assert report.access_review.lan_ip == "192.168.1.50"

