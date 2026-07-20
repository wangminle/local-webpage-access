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
    assert version_info.resolve_version() == "0.6.5"


def test_display_version_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(version_info, "_version_from_git", lambda root: "0.5.3")
    assert version_info.display_version() == "V0.5.3"


def test_version_from_git_subject() -> None:
    assert version_info._version_from_git(None) is None
    match = version_info._VERSION_PREFIX.match("V0.5.3-Build0567-20260715")
    assert match is not None
    assert match.group(1) == "0.5.3"
