"""网页浏览量统计测试（IMP-024 / DEV-061）。

覆盖：解析器（CLF / Caddy JSON / 容器）、存储聚合与截断、惰性摄入游标推进、
Caddy 共享日志按别名前缀归属。直接测试摄入函数，避免依赖完整 registry 登记流程。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_webpage_access.pageviews import (
    ACCESS_LOG_REL,
    AccessHit,
    PageviewStore,
    _ingest_builtin,
    _ingest_caddy_shared,
    _InstanceSource,
    parse_caddy_json_line,
    parse_clf_line,
    parse_container_log_line,
)
from local_webpage_access.paths import Workspace


# ---- 解析器 ------------------------------------------------------------------


def test_parse_clf_line_standard_and_http_server() -> None:
    """标准 CLF（带时区）与 http.server（空格、无时区）都能解析。"""
    std = '127.0.0.1 - - [09/Jul/2026:12:00:00 +0800] "GET /index.html HTTP/1.1" 200 1024'
    h = parse_clf_line(std)
    assert h is not None
    assert h.method == "GET"
    assert h.path == "/index.html"
    assert h.status == 200
    assert h.remote == "127.0.0.1"

    httpd = '10.0.0.5 - - [09/Jul/2026 12:00:00] "POST /api HTTP/1.1" 404 -'
    h2 = parse_clf_line(httpd)
    assert h2 is not None
    assert h2.method == "POST"
    assert h2.path == "/api"
    assert h2.status == 404


def test_parse_clf_line_rejects_non_access() -> None:
    """非访问行（如启动日志）应返回 None。"""
    assert parse_clf_line("启动内置静态服务 pid=123") is None
    assert parse_clf_line("") is None
    assert parse_clf_line("Traceback (most recent call last):") is None


def test_parse_caddy_json_line_basic() -> None:
    line = (
        '{"ts":1752043200.0,"request":{"method":"GET","uri":"/demo/",'
        '"remote_ip":"192.168.1.9"},"status":200}'
    )
    h = parse_caddy_json_line(line)
    assert h is not None
    assert h.method == "GET"
    assert h.path == "/demo/"
    assert h.status == 200
    assert h.remote == "192.168.1.9"
    assert "T" in h.ts  # ISO8601


def test_parse_caddy_json_line_rejects_garbage() -> None:
    assert parse_caddy_json_line("not json") is None
    assert parse_caddy_json_line('{"foo":1}') is None  # 缺 request
    assert parse_caddy_json_line("") is None


def test_parse_container_log_line_various() -> None:
    """容器应用 access 行：CLF、uvicorn、tornado 风格尽力识别。"""
    clf = '172.17.0.1 - - [09/Jul/2026:12:00:00 +0000] "GET / HTTP/1.1" 200 -'
    h = parse_container_log_line(clf)
    assert h is not None and h.status == 200

    uvicorn = 'INFO: 172.17.0.1:0 - "GET /docs HTTP/1.1" 200'
    h2 = parse_container_log_line(uvicorn)
    assert h2 is not None
    assert h2.method == "GET"
    assert h2.path == "/docs"
    assert h2.status == 200

    assert parse_container_log_line("Application startup complete.") is None


def test_parse_container_log_line_rejects_non_resource_text() -> None:
    """BUG-092：path 不以 ``/`` 开头时返回 None，避免普通日志被误计为浏览量。

    正则会从 ``'INFO build finished: 200 GET healthcheck'`` 误匹配出
    ``path='healthcheck'``，修复前仍返回 AccessHit 导致虚高。
    """
    assert parse_container_log_line("INFO build finished: 200 GET healthcheck") is None
    assert parse_container_log_line("200 GET healthcheck") is None
    # 正常 / 开头 path 仍能解析（tornado 风格）
    h = parse_container_log_line("200 GET /health 172.17.0.1")
    assert h is not None
    assert h.path == "/health"
    assert h.status == 200


def test_detect_static_backend_mirrors_gateway_caddy_missing(monkeypatch) -> None:
    """BUG-091：配置 caddy 但二进制缺失时必须返回 builtin（与 detect_backend 一致）。

    否则 builtin 模式的 per-instance gateway.log 访问量因 source 被标为 caddy 而漏统计。
    """
    from local_webpage_access.pageviews import _detect_static_backend

    class _CaddyCfg:
        staticGateway = "caddy"

    monkeypatch.setattr(
        "local_webpage_access.pageviews.shutil.which", lambda _name: None
    )
    assert _detect_static_backend(_CaddyCfg()) == "builtin"

    monkeypatch.setattr(
        "local_webpage_access.pageviews.shutil.which", lambda _name: "/usr/bin/caddy"
    )
    assert _detect_static_backend(_CaddyCfg()) == "caddy"

    class _NginxCfg:
        staticGateway = "nginx"  # 未实现，应降级 builtin

    assert _detect_static_backend(_NginxCfg()) == "builtin"

    class _BuiltinCfg:
        staticGateway = "builtin"

    assert _detect_static_backend(_BuiltinCfg()) == "builtin"


# ---- 存储 ------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> PageviewStore:
    s = PageviewStore(tmp_path / "pageviews.db")
    yield s
    s.close()


def test_store_aggregates_by_day_and_unique_ips(store: PageviewStore) -> None:
    hits = [
        AccessHit("2026-07-09T10:00:00+08:00", "GET", "/", 200, "1.1.1.1"),
        AccessHit("2026-07-09T11:00:00+08:00", "GET", "/x", 200, "1.1.1.1"),
        AccessHit("2026-07-09T12:00:00+08:00", "GET", "/", 200, "2.2.2.2"),
        AccessHit("2026-07-08T09:00:00+08:00", "GET", "/", 200, "3.3.3.3"),
    ]
    assert store.record_hits("demo", "builtin", hits) == 4
    summ = store.summary()
    assert summ["demo"]["hits"] == 4
    # 实例级独立 IP（COUNT DISTINCT remote，跨天去重）：1.1.1.1/2.2.2.2/3.3.3.3 = 3
    assert summ["demo"]["uniqueIps"] == 3
    assert summ["demo"]["source"] == "builtin"

    detail = store.detail("demo")
    by_day = {d["day"]: d for d in detail["byDay"]}
    assert by_day["2026-07-09"]["hits"] == 3
    assert by_day["2026-07-09"]["uniqueIps"] == 2  # 当天 1.1.1.1 + 2.2.2.2
    assert by_day["2026-07-08"]["hits"] == 1
    assert len(detail["recent"]) == 4


def test_unique_ips_dedup_across_batches_same_day(store: PageviewStore) -> None:
    """BUG-087：同一 IP 同一天分多批摄入，uniqueIps 仍只算 1（不可累加）。"""
    day = "2026-07-09T10:00:00+08:00"
    store.record_hits("demo", "builtin", [AccessHit(day, "GET", "/", 200, "1.1.1.1")])
    store.record_hits("demo", "builtin", [AccessHit(day, "GET", "/x", 200, "1.1.1.1")])
    store.record_hits("demo", "builtin", [AccessHit(day, "GET", "/y", 200, "1.1.1.1")])
    assert store.summary()["demo"]["uniqueIps"] == 1
    # hits 仍正常累加
    assert store.summary()["demo"]["hits"] == 3


def test_unique_ips_instance_level_not_sum_of_daily(store: PageviewStore) -> None:
    """BUG-089：同一 IP 跨天回访，实例级 uniqueIps 只算 1，不是各天之和。"""
    store.record_hits(
        "demo",
        "builtin",
        [
            AccessHit("2026-07-08T10:00:00+08:00", "GET", "/", 200, "9.9.9.9"),
            AccessHit("2026-07-09T10:00:00+08:00", "GET", "/", 200, "9.9.9.9"),
        ],
    )
    # 各天各 1 个唯一 IP（和=2），但实例级 distinct=1
    assert store.summary()["demo"]["uniqueIps"] == 1


def test_clear_instance_does_not_overmatch_sibling_cursors(
    store: PageviewStore, tmp_path: Path
) -> None:
    """clear_instance 游标清理不可误伤同前缀兄弟实例（如 demo vs demo-2）。"""
    store.record_hits("demo", "builtin", [AccessHit("2026-07-09T10:00:00+08:00", "GET", "/", 200, "1.1.1.1")])
    store.record_hits("demo-2", "builtin", [AccessHit("2026-07-09T10:00:00+08:00", "GET", "/", 200, "2.2.2.2")])
    # 手动建两条 builtin 游标
    store.set_cursor("builtin:demo:/x/gateway.log", 100, None)
    store.set_cursor("builtin:demo-2:/x/gateway.log", 200, None)

    store.clear_instance("demo")

    assert store.summary() == {"demo-2": store.summary()["demo-2"]}  # demo 被清空
    assert store.summary().get("demo-2", {}).get("hits") == 1
    # demo-2 的游标必须保留（旧 LIKE '%:demo%' 会误删）
    off, _ = store.get_cursor("builtin:demo-2:/x/gateway.log")
    assert off == 200


def test_tail_new_lines_handles_partial_trailing_line(
    workspace: Workspace, store: PageviewStore
) -> None:
    """半行保护：末尾无换行的未完成行不推进游标，续写后能完整读到。"""
    from local_webpage_access.pageviews import _tail_new_lines

    log_path = workspace.app_logs("demo") / "gateway.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # 写一条完整行 + 一条未完成（无换行）
    log_path.write_bytes(
        b'127.0.0.1 - - [09/Jul/2026 10:00:00] "GET / HTTP/1.1" 200 -\n'
        b'127.0.0.1 - - [09/Jul/2026 10:01:00] "GET /partial'
    )
    first = _tail_new_lines(log_path, "k1", store)
    assert len(first) == 1  # 只返回已完成的 1 行，partial 不返回
    # 续写完成 partial 行
    with log_path.open("ab") as fh:
        fh.write(b' HTTP/1.1" 200 -\n')
    second = _tail_new_lines(log_path, "k1", store)
    assert len(second) == 1  # 之前 partial 的行现在完整读到
    assert "partial" in second[0]


def test_ingest_container_idempotent_no_double_count(
    workspace: Workspace, store: PageviewStore, monkeypatch
) -> None:
    """BUG-088：同一容器日志被拉两次（--since 边界重叠），hits 不应翻倍。"""
    import local_webpage_access.pageviews as pv

    log_text = (
        'INFO: 172.17.0.1:0 - "GET /docs HTTP/1.1" 200\n'
        'INFO: 172.17.0.1:0 - "GET / HTTP/1.1" 200\n'
    )

    class _FakeRuntime:
        @staticmethod
        def is_available() -> bool:
            return True

        def __init__(self, ws, reg) -> None:  # noqa: ANN001
            pass

        def logs(self, instance_id, *, tail=400, since=None):  # noqa: ANN001
            return log_text  # 每次都返回同样的两行（模拟 --since 边界重叠）

    monkeypatch.setattr(
        "local_webpage_access.docker_runtime.DockerRuntime", _FakeRuntime
    )
    src = pv._InstanceSource("api", "container")

    pv._ingest_container(workspace, object(), src, store)
    assert store.summary()["api"]["hits"] == 2
    # 第二次摄入同样的日志：去重集生效，不再翻倍
    pv._ingest_container(workspace, object(), src, store)
    assert store.summary()["api"]["hits"] == 2


def test_store_truncates_detail_to_retain_window(store: PageviewStore) -> None:
    import local_webpage_access.pageviews as pv

    n = pv._DETAIL_RETAIN + 50
    hits = [
        AccessHit("2026-07-09T10:00:00+08:00", "GET", f"/p{i}", 200, "1.1.1.1")
        for i in range(n)
    ]
    store.record_hits("big", "caddy", hits)
    detail = store.detail("big", limit=9999)
    assert len(detail["recent"]) <= pv._DETAIL_RETAIN


def test_store_summary_empty_for_unknown(store: PageviewStore) -> None:
    assert store.summary() == {}
    assert store.detail("nope")["byDay"] == []
    assert store.detail("nope")["recent"] == []


def test_store_accumulates_across_calls(store: PageviewStore) -> None:
    store.record_hits(
        "demo", "builtin", [AccessHit("2026-07-09T10:00:00+08:00", "GET", "/", 200, "a")]
    )
    store.record_hits(
        "demo", "builtin", [AccessHit("2026-07-09T11:00:00+08:00", "GET", "/", 200, "b")]
    )
    assert store.summary()["demo"]["hits"] == 2


# ---- 摄入：builtin gateway.log 增量解析 ------------------------------------


def test_ingest_builtin_advances_cursor(workspace: Workspace, store: PageviewStore) -> None:
    """builtin：从 gateway.log 增量解析 CLF，游标推进后不重复计数。"""
    log_path = workspace.app_logs("demo") / "gateway.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        '127.0.0.1 - - [09/Jul/2026 10:00:00] "GET / HTTP/1.1" 200 -\n'
        '127.0.0.1 - - [09/Jul/2026 10:01:00] "GET /about HTTP/1.1" 200 -\n',
        encoding="utf-8",
    )
    src = _InstanceSource("demo", "builtin")

    _ingest_builtin(workspace, src, store)
    assert store.summary()["demo"]["hits"] == 2

    # 追加一行后再次摄入：只新增 1（游标已推进）
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write('192.168.1.5 - - [09/Jul/2026 10:02:00] "GET / HTTP/1.1" 200 -\n')
    _ingest_builtin(workspace, src, store)
    assert store.summary()["demo"]["hits"] == 3


def test_ingest_builtin_handles_missing_log(
    workspace: Workspace, store: PageviewStore
) -> None:
    """日志不存在时安全跳过，不报错不计数。"""
    _ingest_builtin(workspace, _InstanceSource("ghost", "builtin"), store)
    assert store.summary() == {}


# ---- 摄入：caddy 共享日志按别名前缀归属 --------------------------------------


def test_ingest_caddy_shared_routes_by_alias_prefix(
    workspace: Workspace, store: PageviewStore
) -> None:
    """caddy：共享 access log 按 /<alias>/ 前缀归属到对应实例。"""
    access_log = workspace.root / ACCESS_LOG_REL
    access_log.parent.mkdir(parents=True, exist_ok=True)
    access_log.write_text(
        '{"ts":1752043200.0,"request":{"method":"GET","uri":"/demo/","remote_ip":"1.2.3.4"},"status":200}\n'
        '{"ts":1752043201.0,"request":{"method":"GET","uri":"/demo/x","remote_ip":"1.2.3.4"},"status":304}\n'
        '{"ts":1752043202.0,"request":{"method":"GET","uri":"/other/","remote_ip":"9.9.9.9"},"status":200}\n',
        encoding="utf-8",
    )
    # 只有 demo 别名可归属；/other/ 无对应实例 → 不计入任何实例
    alias_to_id = {"/demo": "demo"}
    sources = [_InstanceSource("demo", "caddy", alias="demo")]

    _ingest_caddy_shared(workspace, access_log, alias_to_id, store, sources)
    summ = store.summary()
    assert summ.get("demo", {}).get("hits") == 2  # /demo/ 与 /demo/x
    # /other/ 不归属任何实例
    assert all(v["hits"] == 2 or k == "demo" for k, v in summ.items())


def test_ingest_caddy_shared_prefix_specificity(
    workspace: Workspace, store: PageviewStore
) -> None:
    """长别名不被短别名前缀吞掉（/api-v2 不应算到 /api）。"""
    access_log = workspace.root / ACCESS_LOG_REL
    access_log.parent.mkdir(parents=True, exist_ok=True)
    access_log.write_text(
        '{"ts":1.0,"request":{"method":"GET","uri":"/api/"},"status":200}\n'
        '{"ts":2.0,"request":{"method":"GET","uri":"/api-v2/"},"status":200}\n',
        encoding="utf-8",
    )
    alias_to_id = {"/api": "api", "/api-v2": "apiv2"}
    sources = [
        _InstanceSource("api", "caddy", alias="api"),
        _InstanceSource("apiv2", "caddy", alias="api-v2"),
    ]
    _ingest_caddy_shared(workspace, access_log, alias_to_id, store, sources)
    summ = store.summary()
    assert summ["api"]["hits"] == 1
    assert summ["apiv2"]["hits"] == 1
