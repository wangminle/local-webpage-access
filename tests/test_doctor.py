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
        {("docker", "version"): _proc(0, stdout="29.1.3\n")}
    )
    r = check_docker(runner=runner)
    assert r.status == STATUS_FAIL
    assert "29.6.1" in r.message


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


# ---- WBS-26.04 Docker Compose ---------------------------------------------


def test_check_docker_compose_v2_ok() -> None:
    runner = _runner_from_map(
        {("docker", "compose", "version"): _proc(0, stdout="5.2.0\n")}
    )
    r = check_docker_compose(runner=runner)
    assert r.status == STATUS_OK
    assert "5.2.0" in r.message


def test_check_docker_compose_version_too_low() -> None:
    runner = _runner_from_map(
        {("docker", "compose", "version"): _proc(0, stdout="2.40.3\n")}
    )
    r = check_docker_compose(runner=runner)
    assert r.status == STATUS_FAIL
    assert "5.2.0" in r.message


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
    runner = _runner_from_map({("caddy", "version"): _proc(0, stdout="v2.11.2\n")})
    r = check_caddy(config, runner=runner)
    assert r.status == STATUS_OK


def test_check_caddy_version_too_low(env, monkeypatch) -> None:
    _ws, config, _reg = env
    config.staticGateway = "caddy"
    monkeypatch.setattr("local_webpage_access.doctor.shutil.which", lambda _: "/usr/bin/caddy")
    runner = _runner_from_map({("caddy", "version"): _proc(0, stdout="v2.11.1\n")})
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


def test_check_port_pool_still_checks_manager_port_when_busy() -> None:
    from local_webpage_access.config import Config, PortPool

    cfg = Config(managerPort=22000, portPool=PortPool(start=21000, end=21010))

    def manager_busy(port: int) -> bool:
        return port == 22000

    r = check_port_pool(cfg, port_in_use=manager_busy, allocated_ports={22000})
    assert r.status == STATUS_FAIL
    assert "22000" in (r.detail or "")


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

    data = json.loads(result.output)
    assert "overall" in data
    assert "checks" in data
    assert isinstance(data["checks"], list)
    assert len(data["checks"]) >= 8
