"""普通命令可用 --workspace / 默认工作区，而不必先 cd 到 runtime。"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_webpage_access.errors import PathError
from local_webpage_access.init_workspace import init_workspace
from local_webpage_access.paths import (
    Workspace,
    pop_cli_workspace,
    push_cli_workspace,
    require_workspace,
    write_default_workspace,
)


@pytest.fixture(autouse=True)
def _isolate_workspace_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """路径选择测试只初始化文件，不启动 manager 或联动网关。"""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    monkeypatch.delenv("LWA_WORKSPACE", raising=False)
    monkeypatch.setattr(
        "local_webpage_access.manager_service.maybe_start_manager", lambda *a, **k: None
    )


def _ws(root: Path) -> Path:
    init_workspace(root, static_gateway="builtin")
    return root.resolve()


def test_workspace_setup_does_not_start_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_start(*args, **kwargs):
        pytest.fail("路径选择测试不得启动真实管理服务")

    monkeypatch.setattr("local_webpage_access.manager_service.start_manager", forbidden_start)
    assert (_ws(tmp_path / "runtime") / "local-web.yml").is_file()


def test_explicit_workspace_overrides_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    here = _ws(tmp_path / "here")
    other = _ws(tmp_path / "other")
    monkeypatch.chdir(here)
    tokens = push_cli_workspace(str(other))
    try:
        assert require_workspace().root == other
    finally:
        pop_cli_workspace(tokens)


def test_saved_default_used_when_cwd_has_no_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _ws(tmp_path / "runtime")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("LWA_WORKSPACE", raising=False)
    write_default_workspace(target)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    tokens = push_cli_workspace(None)
    try:
        assert require_workspace().root == target
    finally:
        pop_cli_workspace(tokens)


def test_cwd_workspace_wins_over_saved_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    here = _ws(tmp_path / "here")
    other = _ws(tmp_path / "other")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("LWA_WORKSPACE", raising=False)
    write_default_workspace(other)
    monkeypatch.chdir(here)
    tokens = push_cli_workspace(None)
    try:
        assert require_workspace().root == here
    finally:
        pop_cli_workspace(tokens)


def test_env_workspace_wins_over_saved_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_target = _ws(tmp_path / "from-file")
    env_target = _ws(tmp_path / "from-env")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    write_default_workspace(file_target)
    monkeypatch.setenv("LWA_WORKSPACE", str(env_target))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    tokens = push_cli_workspace(None)
    try:
        assert require_workspace().root == env_target
    finally:
        pop_cli_workspace(tokens)


def test_missing_workspace_message_does_not_prescribe_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("LWA_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)
    tokens = push_cli_workspace(None)
    try:
        with pytest.raises(PathError) as exc:
            require_workspace()
    finally:
        pop_cli_workspace(tokens)
    text = str(exc.value)
    assert "--workspace" in text
    assert "workspace use" in text
    assert "不要在项目目录里执行 lwa init" in text


def test_cli_status_uses_workspace_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from local_webpage_access.cli import app

    runtime = _ws(tmp_path / "runtime")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("LWA_WORKSPACE", raising=False)

    missing = CliRunner().invoke(app, ["status"])
    assert missing.exit_code == 1
    assert "--workspace" in (missing.stderr or missing.output)
    assert "不要在项目目录里执行 lwa init" in (missing.stderr or missing.output)

    found = CliRunner().invoke(app, ["--workspace", str(runtime), "status"])
    assert found.exit_code == 0, found.output
    assert "暂无实例" in found.stdout


def test_cli_workspace_use_then_status_from_project_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from local_webpage_access.cli import app

    runtime = _ws(tmp_path / "runtime")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("LWA_WORKSPACE", raising=False)
    runner = CliRunner()

    saved = runner.invoke(app, ["workspace", "use", str(runtime)])
    assert saved.exit_code == 0, saved.output
    assert str(runtime) in saved.stdout

    status = runner.invoke(app, ["status"])
    assert status.exit_code == 0, status.output
    assert "暂无实例" in status.stdout


def test_workspace_use_rejects_uninitialized_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from local_webpage_access.cli import app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "empty").mkdir()
    result = CliRunner().invoke(app, ["workspace", "use", str(tmp_path / "empty")])
    assert result.exit_code == 1
    assert "local-web.yml" in (result.stderr or result.output)
    assert not (tmp_path / "cfg" / "lwa" / "default-workspace").exists()


def test_require_workspace_without_cli_context_ignores_default_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """库调用不读用户默认工作区，避免测试或子进程误绑到别人的 runtime。"""
    target = _ws(tmp_path / "runtime")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("LWA_WORKSPACE", raising=False)
    write_default_workspace(target)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    with pytest.raises(PathError):
        require_workspace()
    assert Workspace(target).root == target
