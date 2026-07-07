"""version_info 模块测试。"""

from __future__ import annotations

from local_webpage_access import version_info


def test_resolve_version_from_git_in_repo() -> None:
    version_info.resolve_version.cache_clear()
    ver = version_info.resolve_version()
    assert ver == "0.4.0"


def test_display_version_prefix() -> None:
    version_info.resolve_version.cache_clear()
    assert version_info.display_version() == "V0.4.0"


def test_version_from_git_subject() -> None:
    assert version_info._version_from_git(None) is None
    root = version_info._repo_root()
    assert root is not None
    assert version_info._version_from_git(root) == "0.4.0"
