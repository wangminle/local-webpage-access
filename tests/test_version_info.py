"""version_info 模块测试。"""

from __future__ import annotations

import pytest

from local_webpage_access import version_info


@pytest.fixture(autouse=True)
def _clear_version_cache() -> None:
    version_info.resolve_version.cache_clear()
    yield
    version_info.resolve_version.cache_clear()


def test_resolve_version_prefers_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(version_info, "_version_from_git", lambda root: "0.5.3")
    monkeypatch.setattr(version_info, "_version_from_metadata", lambda: "0.5.2")
    assert version_info.resolve_version() == "0.5.3"


def test_resolve_version_uses_metadata_when_git_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(version_info, "_version_from_git", lambda root: None)
    monkeypatch.setattr(version_info, "_version_from_metadata", lambda: "0.5.3")
    assert version_info.resolve_version() == "0.5.3"


def test_resolve_version_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(version_info, "_version_from_git", lambda root: None)
    monkeypatch.setattr(version_info, "_version_from_metadata", lambda: None)
    assert version_info.resolve_version() == "0.6.9"


def test_display_version_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(version_info, "_version_from_git", lambda root: "0.5.3")
    assert version_info.display_version() == "V0.5.3"


def test_version_from_git_subject() -> None:
    assert version_info._version_from_git(None) is None
    match = version_info._VERSION_PREFIX.match("V0.5.3-Build0567-20260715")
    assert match is not None
    assert match.group(1) == "0.5.3"


def test_repo_identity_rejects_unrelated_pyproject(tmp_path) -> None:
    """BUG-306：版本号 Git 兜底不得信任任意 Python 仓库。"""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='unrelated-project'\n",
        encoding="utf-8",
    )

    assert version_info._is_lwa_repo(tmp_path) is False


def test_repo_root_git_fallback_rejects_unrelated_project(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BUG-306：_repo_root 的 git 兜底必须校验 project.name。"""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='unrelated-project'\n",
        encoding="utf-8",
    )
    fake_here = tmp_path / "src" / "local_webpage_access" / "version_info.py"
    monkeypatch.setattr(version_info, "__file__", str(fake_here))

    class _Result:
        returncode = 0
        stdout = str(tmp_path) + "\n"

    monkeypatch.setattr(
        version_info.subprocess,
        "run",
        lambda *a, **k: _Result(),
    )

    assert version_info._repo_root() is None
