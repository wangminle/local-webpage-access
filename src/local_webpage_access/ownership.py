"""端口/资源归属的三态判定枚举（S2-07 / BUG-748）。

二态判定（``True/False``）在探测手段缺失（如没有 lsof）时只能悄悄偏向一边，
用户看不到判定依据是否充分。三态把「证据不足」显式化：

* ``OURS``——证据链完整，确认归本工作区/本实例；
* ``FOREIGN``——证据链完整，确认不归本工作区/本实例；
* ``UNKNOWN``——探测手段缺失或异常，无法确认归属。

UNKNOWN 的处置由各调用方按场景决定（放行+可见 / 阻断+说明），
本模块只负责表达判定结果。
"""

from __future__ import annotations

from enum import Enum


class Ownership(Enum):
    """归属三态判定结果（S2-07）。"""

    OURS = "ours"
    FOREIGN = "foreign"
    UNKNOWN = "unknown"
