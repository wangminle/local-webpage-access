"""资源档位映射测试（IMP-018 / WBS-20260708 阶段2.2）。"""

from __future__ import annotations

from local_webpage_access.models import ResourceProfile
from local_webpage_access.resource_profiles import profile_to_limits


def test_resource_profile_maps_to_mem_limit() -> None:
    """各档位映射到预期的 mem/cpus（WBS 阶段2.2 验收）。"""
    assert profile_to_limits(ResourceProfile.TINY).memory == "128m"
    assert profile_to_limits(ResourceProfile.SMALL).memory == "256m"
    assert profile_to_limits(ResourceProfile.MEDIUM).memory == "1g"
    assert profile_to_limits(ResourceProfile.HEAVY).memory == "2g"
    # cpus 同步映射
    assert profile_to_limits(ResourceProfile.MEDIUM).cpus == "1.5"
    assert profile_to_limits(ResourceProfile.HEAVY).cpus == "3"


def test_resource_profile_accepts_string_value() -> None:
    """manifest 的 resourceProfile 可能序列化为字符串，应同样解析。"""
    limits = profile_to_limits("medium")
    assert limits.memory == "1g"


def test_resource_profile_unknown_falls_back_to_small() -> None:
    """未知档位回退 small，保证 Compose 始终拿到合法限制。"""
    limits = profile_to_limits("does-not-exist")
    assert limits.memory == "256m"
