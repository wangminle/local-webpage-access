"""资源档位 → 容器资源限制映射（IMP-018 / WBS-20260708 阶段2.2）。

把扫描器推断的 ``resourceProfile``（tiny/small/medium/heavy）映射为 Compose
可消费的 ``mem_limit`` / ``cpus``，避免所有容器实例恒为默认 512m（runtime
复盘 §4.2-P8）。映射表在单处定义，由 :func:`build_manifest_from_detection`
在构造 :class:`~local_webpage_access.models.ContainerConfig` 时注入。

档位取值面向"小主机本地部署"档板（多数为 Mac mini / 迷你主机）：

* ``tiny``    —— 纯静态/极轻量（实际静态实例不走容器，保留映射仅为对称）；
* ``small``   —— 普通 Web 后端（FastAPI/Flask/Django 基础栈）；
* ``medium``  —— 含重依赖（lancedb/pyarrow/torch/openai …）或 streamlit/gradio；
* ``heavy``   —— 预留高档位（当前 scanner 未自动赋予，可由 skill 手动提升）。
"""

from __future__ import annotations

from local_webpage_access.models import ResourceLimits, ResourceProfile

_RESOURCE_PROFILE_LIMITS: dict[ResourceProfile, ResourceLimits] = {
    ResourceProfile.TINY: ResourceLimits(memory="128m", cpus="0.25"),
    ResourceProfile.SMALL: ResourceLimits(memory="256m", cpus="0.5"),
    ResourceProfile.MEDIUM: ResourceLimits(memory="1g", cpus="1.5"),
    ResourceProfile.HEAVY: ResourceLimits(memory="2g", cpus="3"),
}


def profile_to_limits(profile: ResourceProfile | str) -> ResourceLimits:
    """把资源档位映射为容器资源限制。

    接受 :class:`ResourceProfile` 枚举或其字符串值（如 ``"medium"``），便于
    从 manifest 的 ``resourceProfile``（可能被序列化为字符串）直接传入。未知
    档位回退到 :class:`ResourceProfile.SMALL`，保证 Compose 始终拿到合法限制。
    """
    if isinstance(profile, ResourceProfile):
        key = profile
    else:
        try:
            key = ResourceProfile(str(profile))
        except ValueError:
            return _RESOURCE_PROFILE_LIMITS[ResourceProfile.SMALL]
    return _RESOURCE_PROFILE_LIMITS.get(key, _RESOURCE_PROFILE_LIMITS[ResourceProfile.SMALL])


__all__ = ["profile_to_limits"]
