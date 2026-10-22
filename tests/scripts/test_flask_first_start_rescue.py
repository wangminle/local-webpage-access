"""Flask 首次部署：缺少 Compose 时仍须严格确认旧容器及其数据。"""

from pathlib import Path

import pytest

from local_webpage_access.compose import generate_compose
from local_webpage_access.docker_runtime import ComposeResult, DockerRuntime
from local_webpage_access.errors import DockerError, HostingError
from local_webpage_access.hosting import _rescue_container_data_before_rebuild
from tests.scripts.test_bug903_908 import _write_flask_instance_app
from tests.scripts.test_compose import _mk_manifest


@pytest.fixture()
def first_start(workspace):
    workspace.ensure_app_dirs("api")
    _write_flask_instance_app(workspace.app_current("api"))
    manifest = _mk_manifest(has_database=True, database_type="sqlite")
    manifest.container.projectName = "saved-project"
    manifest.save(workspace.app_manifest_path("api"))
    # 父目录非空不能证明旧容器的 instance 数据已经持久化（BUG-912）。
    (workspace.app_data("api") / "instance").mkdir()
    return workspace, manifest, DockerRuntime(workspace)


def _labels(project="saved-project", service="app"):
    return [
        "--filter", f"label=com.docker.compose.project={project}",
        "--filter", f"label=com.docker.compose.service={service}",
    ]


def test_first_start_without_compose_proves_no_old_container(first_start, monkeypatch):
    workspace, manifest, runtime = first_start
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        if args[:2] == ["docker", "compose"]:
            return ComposeResult(args=args, returncode=1, stderr="compose.yaml: no such file")
        assert args == ["docker", "ps", "--all", "-q", *_labels()]
        return ComposeResult(args=args, returncode=0, stdout="")

    monkeypatch.setattr("local_webpage_access.docker_runtime._execute", execute)
    assert not workspace.app_compose_path("api").exists()
    _rescue_container_data_before_rebuild(workspace, manifest, "api", runtime)
    assert len(calls) == 1
    # 只有观测成功且无容器后，首次部署才能继续到 Compose 生成。
    assert generate_compose(manifest, workspace, host_port=18000).is_file()


def test_missing_compose_still_rescues_old_stopped_container(first_start, monkeypatch):
    workspace, manifest, runtime = first_start
    manifest.container.containerId = "stale-manifest-id"
    manifest.save(workspace.app_manifest_path("api"))
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        if args[:2] == ["docker", "compose"]:
            return ComposeResult(args=args, returncode=1, stderr="compose.yaml: no such file")
        if args[:2] == ["docker", "ps"]:
            assert args == ["docker", "ps", "--all", "-q", *_labels()]
            return ComposeResult(args=args, returncode=0, stdout="old-stopped\n")
        assert args[:3] == ["docker", "cp", "old-stopped:/app/instance/."]
        (Path(args[-1]) / "flaskr.sqlite").write_bytes(b"live-container-data")
        return ComposeResult(args=args, returncode=0)

    monkeypatch.setattr("local_webpage_access.docker_runtime._execute", execute)
    _rescue_container_data_before_rebuild(workspace, manifest, "api", runtime)
    rescued = workspace.app_data("api") / "instance" / "flaskr.sqlite"
    assert rescued.read_bytes() == b"live-container-data"
    assert len(calls) == 2
    assert not workspace.app_compose_path("api").exists()


@pytest.mark.parametrize("detail", ["daemon unreachable", "permission denied", "query timeout"])
def test_missing_compose_query_failure_aborts_rescue(first_start, monkeypatch, detail):
    workspace, manifest, runtime = first_start
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        if args[:2] == ["docker", "compose"]:
            return ComposeResult(args=args, returncode=1, stderr="compose.yaml: no such file")
        if detail == "query timeout":
            raise DockerError(detail)
        return ComposeResult(args=args, returncode=1, stderr=detail)

    monkeypatch.setattr("local_webpage_access.docker_runtime._execute", execute)
    with pytest.raises(HostingError, match=detail):
        _rescue_container_data_before_rebuild(workspace, manifest, "api", runtime)
    assert calls == [["docker", "ps", "--all", "-q", *_labels()]]
    assert not workspace.app_compose_path("api").exists()
    assert not list((workspace.app_data("api") / "instance").iterdir())


@pytest.mark.parametrize("all_containers", [False, True])
def test_missing_compose_uses_saved_service_identity(first_start, monkeypatch, all_containers):
    workspace, manifest, runtime = first_start
    manifest.container.serviceName = "saved-service"
    manifest.save(workspace.app_manifest_path("api"))
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        return ComposeResult(args=args, returncode=0, stdout="old-container\n")

    monkeypatch.setattr("local_webpage_access.docker_runtime._execute", execute)
    assert runtime.container_id_strict("api", all_containers=all_containers) == "old-container"
    ps_args = ["ps", "--all", "-q"] if all_containers else ["ps", "-q"]
    assert calls == [["docker", *ps_args, *_labels(service="saved-service")]]


def test_missing_compose_matches_generated_project_name_fallback(first_start, monkeypatch):
    workspace, manifest, runtime = first_start
    manifest.container.projectName = "invalid: project"
    manifest.save(workspace.app_manifest_path("api"))
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        return ComposeResult(args=args, returncode=0)

    monkeypatch.setattr("local_webpage_access.docker_runtime._execute", execute)
    assert runtime.container_id_strict("api", all_containers=True) is None
    assert calls == [["docker", "ps", "--all", "-q", *_labels(project="api")]]
    assert "name: api\n" in generate_compose(manifest, workspace, host_port=18000).read_text()


@pytest.mark.parametrize("project,normalized", [("Saved.Project", "savedproject"), ("LWA-Api", "lwa-api")])
def test_missing_compose_uses_compose_normalized_project_identity(
    first_start, monkeypatch, project, normalized
):
    """Compose 会规范化顶层 name；未经规范化的 label 会漏掉旧容器。"""
    workspace, manifest, runtime = first_start
    manifest.container.projectName = project
    manifest.save(workspace.app_manifest_path("api"))
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        return ComposeResult(args=args, returncode=0, stdout="old-container\n")

    monkeypatch.setattr("local_webpage_access.docker_runtime._execute", execute)
    assert runtime.container_id_strict("api", all_containers=True) == "old-container"
    assert calls == [["docker", "ps", "--all", "-q", *_labels(project=normalized)]]


@pytest.mark.parametrize("identity", ["missing", "malformed", "no-container", "wrong-id", "empty-service"])
def test_missing_compose_without_reliable_manifest_identity_aborts(
    first_start, monkeypatch, identity
):
    workspace, manifest, runtime = first_start
    path = workspace.app_manifest_path("api")
    if identity == "missing":
        path.unlink()
    elif identity == "malformed":
        path.write_text("{invalid")
    else:
        if identity == "no-container":
            manifest.container = None
        elif identity == "wrong-id":
            manifest.id = "other-instance"
        else:
            manifest.container.serviceName = ""
        manifest.save(path)

    def execute(args, **kwargs):
        pytest.fail("身份不可靠时不得猜测 project/service 并视为空容器")

    monkeypatch.setattr("local_webpage_access.docker_runtime._execute", execute)
    with pytest.raises(DockerError, match="身份"):
        runtime.container_id_strict("api", all_containers=True)


def test_missing_compose_multiple_candidates_aborts(first_start, monkeypatch):
    _, _, runtime = first_start
    monkeypatch.setattr(
        "local_webpage_access.docker_runtime._execute",
        lambda args, **kw: ComposeResult(args=args, returncode=0, stdout="old-1\nold-2\n"),
    )
    with pytest.raises(DockerError, match="多个"):
        runtime.container_id_strict("api", all_containers=True)


@pytest.mark.parametrize("all_containers", [False, True])
@pytest.mark.parametrize("returncode,stdout", [(0, ""), (0, "old-container\n"), (1, "")])
def test_existing_compose_keeps_strict_query_behavior(
    first_start, monkeypatch, all_containers, returncode, stdout
):
    workspace, _, runtime = first_start
    workspace.app_compose_path("api").write_text("name: original-project\nservices: {}\n")
    workspace.app_env_path("api").write_text("HOST_PORT=18000\n")
    # 已有 Compose 查询不应依赖 manifest，也不能在失败后切换观测来源。
    workspace.app_manifest_path("api").unlink()
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        return ComposeResult(args=args, returncode=returncode, stdout=stdout, stderr="daemon error")

    monkeypatch.setattr("local_webpage_access.docker_runtime._execute", execute)
    if returncode:
        with pytest.raises(DockerError, match="compose ps.*exit 1.*daemon error"):
            runtime.container_id_strict("api", all_containers=all_containers)
    else:
        assert runtime.container_id_strict("api", all_containers=all_containers) == (
            stdout.strip() or None
        )
    ps_args = ["ps", "--all", "-q"] if all_containers else ["ps", "-q"]
    assert calls == [[
        "docker", "compose", "--env-file", str(workspace.app_env_path("api")),
        "-f", str(workspace.app_compose_path("api")), *ps_args,
    ]]
