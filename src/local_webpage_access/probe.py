"""内部 HTTP 探针标记（IMP-025.d）。

LWA 自身为启动等待、健康检查、访问复核发起的 HTTP 请求不属于用户浏览，
但会落进实例 access log（builtin 的 ``gateway.log`` / Caddy 的 ``static-access.log``
/ 容器应用日志），若不区分会让浏览量统计被后台探测持续抬高。

统一方案：所有会命中实例日志的内部 GET 请求追加保留查询参数
``__lwa_probe=1``；浏览量摄入端（:func:`pageviews._is_page_view`）据此排除。

为何不按 loopback IP 排除：用户在本机用浏览器访问也应计数，loopback 里既有
探针也有真实浏览，无法区分；显式标记更准确。
"""

from __future__ import annotations

import ssl
import urllib.request
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

LWA_PROBE_PARAM = "__lwa_probe"
LWA_PROBE_VALUE = "1"

# BUG-380：内部本机/LAN 探测不得继承 http_proxy/https_proxy，否则代理异常会假阴性。
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def mark_probe_url(url: str) -> str:
    """给 ``url`` 追加/覆盖 ``__lwa_probe=1``，保留其余 query 与 fragment。

    幂等：重复调用不会叠加多个同名参数。
    """
    parts = urlsplit(url)
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != LWA_PROBE_PARAM
    ]
    query.append((LWA_PROBE_PARAM, LWA_PROBE_VALUE))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def urlopen_direct(
    url: str | urllib.request.Request,
    *,
    timeout: float | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> Any:
    """``urllib.request.urlopen`` 的直连封装：忽略环境代理（BUG-380）。

    HTTPS 首版交付（W08）：``ssl_context`` 提供时启用**完整证书验证**
    （错误证书必须拒绝——禁止 verify=False 降级）。未提供时 urllib 默认
    验证 https。返回的对象据此需要 ``context`` 传递——
    :class:`urllib.request.HTTPSHandler` 按 URL scheme 自动接管。
    """
    if ssl_context is None:
        return _DIRECT_OPENER.open(url, timeout=timeout)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl_context),
    )
    return opener.open(url, timeout=timeout)


__all__ = [
    "LWA_PROBE_PARAM",
    "LWA_PROBE_VALUE",
    "mark_probe_url",
    "urlopen_direct",
]
