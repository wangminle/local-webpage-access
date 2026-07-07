"""version_requirements 模块测试。"""

from __future__ import annotations

from local_webpage_access.version_requirements import (
    MIN_DOCKER_VERSION,
    parse_version_string,
    version_ge,
)


def test_parse_version_string_strips_prefix() -> None:
    assert parse_version_string("v2.11.3") == (2, 11, 3)


def test_version_ge_and_gt() -> None:
    assert version_ge("29.6.1", "29.6.1")
    assert version_ge("29.7.0", "29.6.1")
    assert not version_ge("29.6.0", "29.6.1")
    assert version_ge("2.11.2", "2.11.2")
    assert version_ge("2.11.3", "2.11.2")
    assert not version_ge("2.11.1", "2.11.2")
    assert version_ge("2.40.3", "2.40.2")
    assert not version_ge("2.39.9", "2.40.2")
    assert version_ge("5.2.0", "5.2.0")
    assert not version_ge("5.1.9", "5.2.0")


def test_docker_desktop_55_engine_version_meets_minimum() -> None:
    """Docker Desktop 4.55.0 捆绑的 Engine 29.1.3 不应被运行时误拒绝。"""
    assert version_ge("29.1.3", MIN_DOCKER_VERSION)
