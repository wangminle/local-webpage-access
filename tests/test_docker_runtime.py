"""Docker Runtime 封装测试（WBS-14）。

绝大多数用例通过 monkeypatch ``docker_runtime._execute`` 模拟 docker 命令，
不依赖真实 Docker。少量真实 subprocess 用例验证日志写入与超时路径。
真实 Docker 集成测试见 ``tests/test_docker_integration.py``（skipif 守卫）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from local_web_access.docker_runtime import (
    ComposeResult,
    ContainerStatus,
    DockerRuntime,
    _execute,
    _extract_ports,
    _iter_ps_json,
    ensure_available,
    is_available,
)
from local_web_access.errors import DockerError
from local_web_access.paths import Workspace
from local_web_access.registry import Registry


# ---- fixtures ----------------------------------------------------------------


@pytest.fixture()
def workspace(workspace_root: Path) -> Workspace:
    ws = Workspace(workspace_root)
    ws.ensure_workspace_dirs()
    return ws


@pytest.fixture()
def registry(workspace_root: Path) -> Registry:
    workspace_root.joinpath("registry").mkdir(parents=True, exist_ok=True)
    reg = Registry(workspace_root / "registry" / "local-web.db")
    reg.open()
    yield reg
    reg.close()


def _seed_compose_files(workspace: Workspace, iid: str = "api") -> None:
    """放置 compose.yaml 与 .env，使 _compose_cmd 能识别 --env-file。"""
    workspace.ensure_app_dirs(iid)
    workspace.app_compose_path(iid).write_text("name: lwa-api\nservices: {}\n")
    workspace.app_env_path(iid).write_text("HOST_PORT=18000\n")


def _seed_instance(registry: Registry, iid: str = "api") -> None:
    """向 instances 表插一条最小行，满足 events/builds 外键约束。"""
    from local_web_access.logging import now_iso

    registry.upsert_instance(
        {
            "id": iid,
            "name": iid,
            "version": "1",
            "kind": "python",
            "runtime": "docker-compose",
            "serving_mode": "container",
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
    )


class _FakeExecute:
    """记录所有调用并按规则返回 ComposeResult。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        # 默认成功
        self.default = ComposeResult(args=[], returncode=0, stdout="", stderr="")
        # 按子命令定制返回
        self.by_subcmd: dict[str, ComposeResult] = {}

    def __call__(self, args, *, cwd, log_path=None, timeout=60, **kw):
        self.calls.append(
            {"args": list(args), "cwd": cwd, "log_path": log_path, "timeout": timeout}
        )
        # 命令的子命令关键字（build/up/stop/start/restart/down/logs/ps/version/inspect/images）
        for key, result in self.by_subcmd.items():
            if key in args:
                result = ComposeResult(
                    args=list(args),
                    returncode=result.returncode,
                    stdout=result.stdout,
                    stderr=result.stderr,
                )
                break
        else:
            result = self.default
        # 模拟真实 _execute：把命令与输出追加写入 log_path
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"$ {' '.join(args)}\n")
                if result.stdout:
                    fh.write(result.stdout)
                if result.stderr:
                    fh.write(result.stderr)
        return result


# ---- is_available / ensure_available（WBS-14.01）---------------------------


def test_is_available_true(workspace, monkeypatch) -> None:
    fake = _FakeExecute()
    monkeypatch.setattr("local_web_access.docker_runtime._execute", fake)
    assert is_available() is True
    assert "version" in fake.calls[0]["args"]
    assert "compose" in fake.calls[0]["args"]


def test_is_available_false_when_nonzero(workspace, monkeypatch) -> None:
    fake = _FakeExecute()
    fake.default = ComposeResult(args=[], returncode=1)
    monkeypatch.setattr("local_web_access.docker_runtime._execute", fake)
    assert is_available() is False


def test_is_available_false_when_docker_missing(workspace, monkeypatch) -> None:
    def boom(*a, **kw):
        raise DockerError("docker 未找到")

    monkeypatch.setattr("local_web_access.docker_runtime._execute", boom)
    assert is_available() is False


def test_ensure_available_raises_when_unavailable(workspace, monkeypatch) -> None:
    fake = _FakeExecute()
    fake.default = ComposeResult(args=[], returncode=127)
    monkeypatch.setattr("local_web_access.docker_runtime._execute", fake)
    with pytest.raises(DockerError, match="不可用"):
        ensure_available()


def test_ensure_available_passes_when_available(workspace, monkeypatch) -> None:
    monkeypatch.setattr("local_web_access.docker_runtime._execute", _FakeExecute())
    ensure_available()  # 不抛异常


# ---- _compose_cmd ------------------------------------------------------------


def test_compose_cmd_includes_env_file_and_compose_path(workspace, monkeypatch) -> None:
    _seed_compose_files(workspace, "api")
    fake = _FakeExecute()
    monkeypatch.setattr("local_web_access.docker_runtime._execute", fake)
    rt = DockerRuntime(workspace)
    rt.up("api")

    args = fake.calls[0]["args"]
    assert args[0:2] == ["docker", "compose"]
    assert "--env-file" in args
    env_idx = args.index("--env-file")
    assert args[env_idx + 1].endswith("docker" + "\\" + ".env") or args[env_idx + 1].endswith(
        "docker/.env"
    )
    assert "-f" in args
    f_idx = args.index("-f")
    assert args[f_idx + 1].endswith("compose.yaml")
    assert "up" in args
    assert "-d" in args  # 默认后台


def test_compose_cmd_skips_env_file_when_missing(workspace, monkeypatch) -> None:
    """compose.yaml 存在但 .env 不存在时，不传 --env-file。"""
    workspace.ensure_app_dirs("api")
    workspace.app_compose_path("api").write_text("name: lwa-api\n")
    fake = _FakeExecute()
    monkeypatch.setattr("local_web_access.docker_runtime._execute", fake)
    DockerRuntime(workspace).up("api")
    assert "--env-file" not in fake.calls[0]["args"]


# ---- build（WBS-14.02/.13/.14）---------------------------------------------


def test_build_success_finishes_build_and_writes_log(workspace, registry, monkeypatch) -> None:
    _seed_compose_files(workspace, "api")
    _seed_instance(registry, "api")
    fake = _FakeExecute()
    fake.by_subcmd["build"] = ComposeResult(args=[], returncode=0, stdout="built\n")
    monkeypatch.setattr("local_web_access.docker_runtime._execute", fake)

    build_id = registry.add_build("api", status="running", log_path="x")
    rt = DockerRuntime(workspace, registry)
    result = rt.build("api", build_id=build_id)

    assert result.ok
    # builds 表标记 success
    builds = registry.list_builds("api")
    assert builds[0]["status"] == "success"
    # 日志写入 build.log
    assert (workspace.app_logs("api") / "build.log").is_file()
    # 成功不写 error 事件
    events = registry.list_events("api")
    assert not any(e["event_type"] == "error" for e in events)


def test_build_failure_raises_and_marks_failed(workspace, registry, monkeypatch) -> None:
    _seed_compose_files(workspace, "api")
    _seed_instance(registry, "api")
    fake = _FakeExecute()
    fake.by_subcmd["build"] = ComposeResult(
        args=[], returncode=1, stderr="npm error: ENOTFOUND\n"
    )
    monkeypatch.setattr("local_web_access.docker_runtime._execute", fake)

    build_id = registry.add_build("api", status="running")
    rt = DockerRuntime(workspace, registry)
    with pytest.raises(DockerError, match="build 失败"):
        rt.build("api", build_id=build_id)

    builds = registry.list_builds("api")
    assert builds[0]["status"] == "failed"
    assert builds[0]["error_summary"]  # 含 stderr 摘要
    events = registry.list_events("api")
    assert any(e["event_type"] == "error" for e in events)


def test_build_without_registry_does_not_crash(workspace, monkeypatch) -> None:
    _seed_compose_files(workspace, "api")
    monkeypatch.setattr("local_web_access.docker_runtime._execute", _FakeExecute())
    rt = DockerRuntime(workspace)  # 无 registry
    result = rt.build("api", build_id=123)
    assert result.ok


def test_build_command_uses_compose_build(workspace, monkeypatch) -> None:
    _seed_compose_files(workspace, "api")
    fake = _FakeExecute()
    monkeypatch.setattr("local_web_access.docker_runtime._execute", fake)
    DockerRuntime(workspace).build("api")
    assert "build" in fake.calls[0]["args"]


# ---- up / stop / start / restart / down（WBS-14.03~07）---------------------


@pytest.mark.parametrize(
    "method,subcmd,extra",
    [
        ("up", "up", ["-d"]),
        ("stop", "stop", []),
        ("start", "start", []),
        ("restart", "restart", []),
    ],
)
def test_lifecycle_commands_dispatch_correct_subcommand(
    workspace, registry, monkeypatch, method, subcmd, extra
) -> None:
    _seed_compose_files(workspace, "api")
    _seed_instance(registry, "api")
    fake = _FakeExecute()
    monkeypatch.setattr("local_web_access.docker_runtime._execute", fake)
    rt = DockerRuntime(workspace, registry)
    result = getattr(rt, method)("api")
    assert result.ok
    args = fake.calls[0]["args"]
    assert subcmd in args
    for token in extra:
        assert token in args
    # 每个生命周期命令都应写一条事件
    assert len(registry.list_events("api")) >= 1


def test_stop_does_not_pass_down(workspace, monkeypatch) -> None:
    """验收#2：stop 用 compose stop，不删容器（不应出现 down）。"""
    _seed_compose_files(workspace, "api")
    fake = _FakeExecute()
    monkeypatch.setattr("local_web_access.docker_runtime._execute", fake)
    DockerRuntime(workspace).stop("api")
    args = fake.calls[0]["args"]
    assert "stop" in args
    assert "down" not in args
    assert "-v" not in args


def test_down_passes_down_and_optional_volumes(workspace, monkeypatch) -> None:
    _seed_compose_files(workspace, "api")
    fake = _FakeExecute()
    monkeypatch.setattr("local_web_access.docker_runtime._execute", fake)
    DockerRuntime(workspace).down("api", remove_volumes=True)
    args = fake.calls[0]["args"]
    assert "down" in args
    assert "-v" in args


def test_up_failure_raises_docker_error(workspace, monkeypatch) -> None:
    _seed_compose_files(workspace, "api")
    fake = _FakeExecute()
    fake.by_subcmd["up"] = ComposeResult(args=[], returncode=1, stderr="port already allocated")
    monkeypatch.setattr("local_web_access.docker_runtime._execute", fake)
    with pytest.raises(DockerError, match="up 失败"):
        DockerRuntime(workspace).up("api")


def test_up_not_detached_when_requested(workspace, monkeypatch) -> None:
    _seed_compose_files(workspace, "api")
    fake = _FakeExecute()
    monkeypatch.setattr("local_web_access.docker_runtime._execute", fake)
    DockerRuntime(workspace).up("api", detached=False)
    assert "-d" not in fake.calls[0]["args"]


def test_lifecycle_writes_to_run_log(workspace, monkeypatch) -> None:
    _seed_compose_files(workspace, "api")
    fake = _FakeExecute()
    monkeypatch.setattr("local_web_access.docker_runtime._execute", fake)
    DockerRuntime(workspace).stop("api")
    assert (workspace.app_logs("api") / "run.log").is_file()


# ---- logs（WBS-14.08）-------------------------------------------------------


def test_logs_returns_stdout(workspace, monkeypatch) -> None:
    _seed_compose_files(workspace, "api")
    fake = _FakeExecute()
    fake.by_subcmd["logs"] = ComposeResult(
        args=[], returncode=0, stdout="line1\nline2\n"
    )
    monkeypatch.setattr("local_web_access.docker_runtime._execute", fake)
    text = DockerRuntime(workspace).logs("api", tail=50)
    assert "line1" in text
    args = fake.calls[0]["args"]
    assert "--tail" in args
    assert "50" in args


def test_logs_since_argument(workspace, monkeypatch) -> None:
    _seed_compose_files(workspace, "api")
    fake = _FakeExecute()
    monkeypatch.setattr("local_web_access.docker_runtime._execute", fake)
    DockerRuntime(workspace).logs("api", since="10m")
    assert "--since" in fake.calls[0]["args"]
    assert "10m" in fake.calls[0]["args"]


def test_logs_failure_raises(workspace, monkeypatch) -> None:
    _seed_compose_files(workspace, "api")
    fake = _FakeExecute()
    fake.by_subcmd["logs"] = ComposeResult(args=[], returncode=1, stderr="no container")
    monkeypatch.setattr("local_web_access.docker_runtime._execute", fake)
    with pytest.raises(DockerError, match="获取日志失败"):
        DockerRuntime(workspace).logs("api")


# ---- container_id / image_id / status（WBS-14.09~11）----------------------


def test_container_id_returns_first_line(workspace, monkeypatch) -> None:
    _seed_compose_files(workspace, "api")
    fake = _FakeExecute()
    fake.by_subcmd["ps"] = ComposeResult(
        args=[], returncode=0, stdout="abc123def\n"
    )
    monkeypatch.setattr("local_web_access.docker_runtime._execute", fake)
    assert DockerRuntime(workspace).container_id("api") == "abc123def"


def test_container_id_none_when_empty(workspace, monkeypatch) -> None:
    _seed_compose_files(workspace, "api")
    fake = _FakeExecute()
    monkeypatch.setattr("local_web_access.docker_runtime._execute", fake)
    assert DockerRuntime(workspace).container_id("api") is None


def test_image_id_via_inspect(workspace, monkeypatch) -> None:
    _seed_compose_files(workspace, "api")
    fake = _FakeExecute()
    # ps -q 返回容器 id，inspect 返回镜像 sha
    fake.by_subcmd["ps"] = ComposeResult(args=[], returncode=0, stdout="abc123\n")
    fake.by_subcmd["inspect"] = ComposeResult(
        args=[], returncode=0, stdout="sha256:deadbeef\n"
    )
    monkeypatch.setattr("local_web_access.docker_runtime._execute", fake)
    assert DockerRuntime(workspace).image_id("api") == "sha256:deadbeef"


def test_image_id_fallback_to_docker_images(workspace, monkeypatch) -> None:
    """无容器时回退 docker images -q <project>-<service>。"""
    _seed_compose_files(workspace, "api")
    fake = _FakeExecute()
    fake.by_subcmd["ps"] = ComposeResult(args=[], returncode=0, stdout="")  # 无容器
    fake.by_subcmd["images"] = ComposeResult(args=[], returncode=0, stdout="img999\n")
    monkeypatch.setattr("local_web_access.docker_runtime._execute", fake)
    assert DockerRuntime(workspace).image_id("api") == "img999"
    # 确认查询的是默认镜像名 <project>-<service>
    images_call = [c for c in fake.calls if "images" in c["args"]][0]
    assert "lwa-api-app" in images_call["args"]


def test_status_parses_json_lines(workspace, monkeypatch) -> None:
    _seed_compose_files(workspace, "api")
    payload = {
        "Service": "app",
        "Name": "lwa-api",
        "Image": "lwa-api-app",
        "State": "running",
        "Status": "Up 2 minutes",
        "Health": "healthy",
        "Publishers": [
            {"URL": "0.0.0.0:18000", "PublishedPort": 18000, "TargetPort": 8000}
        ],
    }
    fake = _FakeExecute()
    fake.by_subcmd["ps"] = ComposeResult(
        args=[], returncode=0, stdout=json.dumps(payload) + "\n"
    )
    monkeypatch.setattr("local_web_access.docker_runtime._execute", fake)
    st = DockerRuntime(workspace).status("api")
    assert isinstance(st, ContainerStatus)
    assert st.is_running
    assert st.service == "app"
    assert st.health == "healthy"
    assert any("18000" in p for p in st.ports)


def test_status_none_when_no_container(workspace, monkeypatch) -> None:
    _seed_compose_files(workspace, "api")
    fake = _FakeExecute()
    fake.by_subcmd["ps"] = ComposeResult(args=[], returncode=0, stdout="")
    monkeypatch.setattr("local_web_access.docker_runtime._execute", fake)
    assert DockerRuntime(workspace).status("api") is None


def test_is_running_reflects_state(workspace, monkeypatch) -> None:
    _seed_compose_files(workspace, "api")
    payload_running = {"Service": "app", "State": "running"}
    payload_exited = {"Service": "app", "State": "exited"}
    fake = _FakeExecute()

    def ps_handler(args, *, cwd, log_path=None, timeout=60, **kw):
        if "--all" in args:
            return ComposeResult(args=args, returncode=0, stdout=json.dumps(current[0]) + "\n")
        return ComposeResult(args=args, returncode=0, stdout="")

    current = [payload_running]
    monkeypatch.setattr("local_web_access.docker_runtime._execute", ps_handler)
    rt = DockerRuntime(workspace)
    assert rt.is_running("api") is True
    current[0] = payload_exited
    assert rt.is_running("api") is False


# ---- _execute 真实路径（WBS-14.12/.13）------------------------------------


def test_execute_writes_log_file(tmp_path: Path) -> None:
    log = tmp_path / "out.log"
    if sys.platform == "win32":
        args = ["cmd", "/c", "echo hello"]
    else:
        args = ["echo", "hello"]
    r = _execute(args, cwd=tmp_path, log_path=log)
    assert r.ok
    assert log.is_file()
    content = log.read_text(encoding="utf-8")
    assert "hello" in content
    assert "$" in content  # 命令头


def test_execute_nonzero_returncode_recorded(tmp_path: Path) -> None:
    if sys.platform == "win32":
        args = ["cmd", "/c", "exit 3"]
    else:
        args = ["false"]
    r = _execute(args, cwd=tmp_path)
    assert r.returncode != 0
    assert not r.ok


def test_execute_timeout_raises_docker_error(tmp_path: Path) -> None:
    if sys.platform == "win32":
        args = ["cmd", "/c", "ping -n 10 127.0.0.1"]
    else:
        args = ["sleep", "10"]
    with pytest.raises(DockerError, match="超时"):
        _execute(args, cwd=tmp_path, timeout=1)


def test_execute_missing_binary_raises_docker_error(tmp_path: Path) -> None:
    with pytest.raises(DockerError, match="未找到"):
        _execute(["this-binary-does-not-exist-xyz"], cwd=tmp_path)


# ---- 解析辅助 ----------------------------------------------------------------


def test_iter_ps_json_array_format() -> None:
    """Compose 输出 JSON 数组的情况。"""
    stdout = json.dumps([{"Service": "a", "State": "running"}])
    items = list(_iter_ps_json(stdout))
    assert len(items) == 1
    assert items[0]["Service"] == "a"


def test_iter_ps_json_line_format() -> None:
    stdout = '{"Service": "a"}\n{"Service": "b"}\n'
    items = list(_iter_ps_json(stdout))
    assert [i["Service"] for i in items] == ["a", "b"]


def test_iter_ps_json_empty() -> None:
    assert list(_iter_ps_json("")) == []
    assert list(_iter_ps_json("   ")) == []


def test_extract_ports_from_publishers() -> None:
    data = {"Publishers": [{"URL": "0.0.0.0:18000", "PublishedPort": 18000, "TargetPort": 8000}]}
    ports = _extract_ports(data)
    assert len(ports) == 1
    assert "18000" in ports[0]


def test_extract_ports_legacy_string() -> None:
    data = {"Ports": "0.0.0.0:18000->8000/tcp"}
    assert _extract_ports(data) == ["0.0.0.0:18000->8000/tcp"]


def test_extract_ports_empty() -> None:
    assert _extract_ports({}) == []
