"""网页浏览量统计（IMP-024 / DEV-061）。

按实例汇总访问量，供管理页展示「浏览量」列与详情抽屉。

数据源矩阵（按实例运行态分流）：

* **静态 + builtin**：解析 ``apps/<id>/logs/gateway.log``（``python -m http.server``
  写入的 CLF）。每实例独立日志，无需路由归属。
* **静态 + caddy**：解析统一入口块写入的 JSON access log
  （``run/logs/static-access.log``），按请求路径前缀 ``/<alias>/`` 归属到实例。
  仅覆盖**别名入口**流量；Caddy 模式下直连 hostPort 的访问不计入（hostPort
  多用于本机预览，别名才是「公开浏览」入口）。
* **容器**：尽力解析 ``docker compose logs``（CLF / uvicorn / flask / gunicorn
  常见 access 行）。容器日志格式不可控，统计为**近似值**——能识别的 HTTP 行
  计为命中，识别不到则该实例显示「—」而非误导性 0。

设计要点：

* **惰性摄入**：API 请求时按游标（``ingest_cursor``）只解析新增行，避免每次
  全量重读。游标按"数据源文件路径 + 实例 id"持久化到 ``run/pageviews.db``。
* **跨进程安全**：独立 SQLite（``run/pageviews.db``，WAL），与 registry DB 分库，
  避免写放大；多请求并发摄入靠 WAL + 连接级锁兜底。
* **可降级**：任何单实例摄入失败（日志缺失 / docker 不可用 / 解析异常）只记
  DEBUG 并跳过，绝不让浏览量统计阻断管理页。
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import threading
import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from local_webpage_access.config import Config
from local_webpage_access.logging import get_logger, now_iso
from local_webpage_access.paths import Workspace
from local_webpage_access.registry import Registry
from local_webpage_access.registry.connection import connect

log = get_logger("pageviews")

PAGEVIEW_DB_FILENAME = "pageviews.db"  # 相对 run/
ACCESS_LOG_REL = "logs/static-access.log"  # 相对工作区根，Caddy 统一入口 access log
_DETAIL_RETAIN = 500  # 每实例保留最近 N 条明细行
_DOCKER_LOG_TAIL = 400  # 容器日志单次摄入最大行数（防巨量日志拖慢请求）
_CONTAINER_SEEN_RETAIN = 3000  # 每实例容器日志去重集保留的哈希数上限

# 跨请求摄入串行化（BUG-游标竞态）：FastAPI 把同步端点丢进线程池，两个
# /api/pageviews 并发会让双方读到同一游标、重复摄入同一批行。进程内单锁串行化。
_ingest_lock = threading.Lock()

# ---- 解析器（纯函数，便于单测）-------------------------------------------------

# 兼容 http.server 的 "[09/Jul/2026 12:00:00]" 与标准 CLF 的
# "[09/Jul/2026:12:00:00 +0800]"：方括号内任意非 ] 内容。
_CLF_RE = re.compile(
    r'^(\S+)\s+\S+\s+\S+\s+\[([^\]]+)\]\s+"([A-Z]+)\s+(\S+)[^"]*"\s+(\d{3})'
)

_CLF_DATE_FORMATS = (
    "%d/%b/%Y:%H:%M:%S %z",  # 标准 CLF（冒号 + 时区）
    "%d/%b/%Y %H:%M:%S",  # http.server（空格、无时区）
)


@dataclass
class AccessHit:
    """一条解析出的访问命中。"""

    ts: str  # ISO8601（带时区，未知则 UTC）
    method: str
    path: str
    status: int
    remote: str


def parse_clf_line(line: str) -> AccessHit | None:
    """解析 CLF / http.server 访问行；不匹配返回 ``None``。"""
    m = _CLF_RE.match(line)
    if not m:
        return None
    remote, date_str, method, path, status = m.groups()
    return AccessHit(
        ts=_parse_clf_ts(date_str),
        method=method,
        path=path,
        status=int(status),
        remote=remote,
    )


def _parse_clf_ts(date_str: str) -> str:
    """把 CLF 时间串转 ISO8601；解析失败回退当前 UTC 时间。"""
    for fmt in _CLF_DATE_FORMATS:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            continue
    return now_iso()


# Caddy JSON access log：取 request.* / status / ts 字段。ts 为 unix 秒（float）。
def parse_caddy_json_line(line: str) -> AccessHit | None:
    """解析 Caddy JSON access log 单行；非 JSON 或缺字段返回 ``None``。"""
    line = line.strip()
    if not line or line[0] != "{":
        return None
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return None
    req = obj.get("request") or {}
    method = str(req.get("method") or "").upper()
    uri = str(req.get("uri") or "")
    if not method or not uri:
        return None
    remote = str(req.get("remote_ip") or "")
    status = obj.get("status")
    try:
        status_int = int(status) if status is not None else 0
    except (ValueError, TypeError):
        status_int = 0
    ts = obj.get("ts")
    iso_ts = _unix_to_iso(ts) if isinstance(ts, (int, float)) else now_iso()
    return AccessHit(ts=iso_ts, method=method, path=uri, status=status_int, remote=remote)


# 容器应用日志尽力解析：uvicorn / flask / gunicorn / Django / 普通 CLF。
# 例如：
#   "GET /api/x HTTP/1.1" 200 OK         (uvicorn access)
#   127.0.0.1 - - [..] "GET / HTTP/1.1" 200 -   (gunicorn/CLF)
#   [I 220101 12:00:00] 200 GET / (1.2ms) (tornado)
_CONTAINER_RE = re.compile(
    r'"([A-Z]+)\s+(\S+)[^"]*"\s+(\d{3})'
    r'|'
    r'\b(\d{3})\b\s+([A-Z]+)\s+(\S+)'
)


def parse_container_log_line(line: str) -> AccessHit | None:
    """尽力解析容器应用 access 行；不匹配返回 ``None``（容器日志格式不可控）。"""
    if not line.strip():
        return None
    # 先按 CLF（含远端 IP）解析，命中则更完整
    clf = parse_clf_line(line)
    if clf is not None:
        return clf
    m = _CONTAINER_RE.search(line)
    if not m:
        return None
    if m.group(1):  # "METHOD path" "status" 形式
        method, path, status = m.group(1), m.group(2), int(m.group(3))
    else:  # status METHOD path 形式（tornado 等）
        status, method, path = int(m.group(4)), m.group(5), m.group(6)
    if not path.startswith("/"):
        # 排除非资源文本（如 'INFO build finished: 200 GET healthcheck'，
        # 正则会误匹配出 path='healthcheck'）——容器 access 行的 path 必以 / 开头（BUG-092）
        return None
    return AccessHit(ts=now_iso(), method=method, path=path, status=status, remote="")


def _unix_to_iso(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return now_iso()


def _day_of(iso_ts: str) -> str:
    """从 ISO8601 取 ``YYYY-MM-DD``；失败回退当天。"""
    try:
        return iso_ts[:10]
    except Exception:  # noqa: BLE001
        return now_iso()[:10]


# ---- 存储 ------------------------------------------------------------------


class PageviewStore:
    """``run/pageviews.db`` 的访问层（IMP-024）。

    独立于 registry DB，避免把高频 append 的明细写进核心索引库。WAL 模式 +
    连接级锁使其在管理页多线程请求下安全共享。
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._closed = False

    @classmethod
    def for_workspace(cls, workspace: Workspace) -> "PageviewStore":
        return cls(workspace.run / PAGEVIEW_DB_FILENAME)

    def _conn_or_open(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = connect(self.db_path)
            self._init_schema(self._conn)
        return self._conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pageviews (
                instance_id TEXT NOT NULL,
                day TEXT NOT NULL,
                hits INTEGER NOT NULL DEFAULT 0,
                unique_ips INTEGER NOT NULL DEFAULT 0,
                last_seen TEXT,
                source TEXT NOT NULL DEFAULT 'builtin',
                PRIMARY KEY (instance_id, day)
            );
            CREATE TABLE IF NOT EXISTS pageview_detail (
                instance_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                method TEXT,
                path TEXT,
                status INTEGER,
                remote TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_detail_instance
                ON pageview_detail(instance_id, ts);
            -- BUG-087：跨批次 IP 去重的真相集。同一天同一 IP 多次出现只算 1 个独立访客；
            -- 跨天回访也通过该集合在 summary 用 COUNT(DISTINCT remote) 正确去重（BUG-089）。
            CREATE TABLE IF NOT EXISTS pageview_ips (
                instance_id TEXT NOT NULL,
                day TEXT NOT NULL,
                remote TEXT NOT NULL,
                PRIMARY KEY (instance_id, day, remote)
            );
            -- BUG-088：容器日志幂等去重集。docker logs --since 边界包含会重返回上批末行，
            -- 同一日志行重复计入会让 hits 随每次刷新膨胀；按"原始行哈希"去重保证至多一次。
            CREATE TABLE IF NOT EXISTS container_seen (
                instance_id TEXT NOT NULL,
                line_hash TEXT NOT NULL,
                PRIMARY KEY (instance_id, line_hash)
            );
            CREATE TABLE IF NOT EXISTS ingest_cursor (
                source_key TEXT PRIMARY KEY,
                offset_bytes INTEGER NOT NULL DEFAULT 0,
                last_ts TEXT,
                updated_at TEXT
            );
            """
        )

    def close(self) -> None:
        with self._lock:
            if self._conn is not None and not self._closed:
                try:
                    self._conn.close()
                finally:
                    self._closed = True

    # ---- 游标 ----

    def get_cursor(self, source_key: str) -> tuple[int, str | None]:
        conn = self._conn_or_open()
        with self._lock:
            row = conn.execute(
                "SELECT offset_bytes, last_ts FROM ingest_cursor WHERE source_key=?",
                (source_key,),
            ).fetchone()
        return (int(row[0]) if row else 0, (row[1] if row else None))

    def set_cursor(self, source_key: str, offset: int, last_ts: str | None) -> None:
        conn = self._conn_or_open()
        with self._lock:
            conn.execute(
                "INSERT INTO ingest_cursor(source_key, offset_bytes, last_ts, updated_at) "
                "VALUES(?,?,?,?) "
                "ON CONFLICT(source_key) DO UPDATE SET "
                "offset_bytes=excluded.offset_bytes, last_ts=excluded.last_ts, "
                "updated_at=excluded.updated_at",
                (source_key, offset, last_ts, now_iso()),
            )
            conn.commit()

    # ---- 写入 ----

    def record_hits(
        self, instance_id: str, source: str, hits: Iterable[AccessHit]
    ) -> int:
        """按天聚合写入命中；返回写入条数。明细同时落表并截断保留窗。

        BUG-087 修复：独立访客不再用 ``unique_ips += 本批去重值`` 累加（同一天
        同一 IP 分批摄入会被重复算多次），改把 ``(day, remote)`` 写入
        ``pageview_ips`` 真相集（``INSERT OR IGNORE``），当天 ``unique_ips`` 由
        该集合的 ``COUNT(*)`` 重算——天然跨批次去重。
        """
        hits = list(hits)
        if not hits:
            return 0
        conn = self._conn_or_open()
        per_day: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"hits": 0, "ips": set(), "last": ""}
        )
        detail_rows: list[tuple] = []
        for h in hits:
            day = _day_of(h.ts)
            bucket = per_day[day]
            bucket["hits"] += 1
            if h.remote:
                bucket["ips"].add(h.remote)
            if h.ts > bucket["last"]:
                bucket["last"] = h.ts
            detail_rows.append(
                (instance_id, h.ts, h.method, h.path, h.status, h.remote)
            )
        with self._lock:
            for day, b in per_day.items():
                # 累加命中数；unique_ips 稍后由 pageview_ips 集合重算
                conn.execute(
                    "INSERT INTO pageviews(instance_id, day, hits, unique_ips, "
                    "last_seen, source) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(instance_id, day) DO UPDATE SET "
                    "hits=pageviews.hits+excluded.hits, "
                    "last_seen=excluded.last_seen, source=excluded.source",
                    (instance_id, day, b["hits"], 0, b["last"], source),
                )
                # 写入当天 IP 真相集（跨批次去重），再据此重算当天 unique_ips
                if b["ips"]:
                    conn.executemany(
                        "INSERT OR IGNORE INTO pageview_ips(instance_id, day, remote) "
                        "VALUES(?,?,?)",
                        [(instance_id, day, ip) for ip in b["ips"]],
                    )
                cnt = conn.execute(
                    "SELECT COUNT(*) FROM pageview_ips WHERE instance_id=? AND day=?",
                    (instance_id, day),
                ).fetchone()[0]
                conn.execute(
                    "UPDATE pageviews SET unique_ips=? WHERE instance_id=? AND day=?",
                    (cnt, instance_id, day),
                )
            conn.executemany(
                "INSERT INTO pageview_detail(instance_id, ts, method, path, status, "
                "remote) VALUES(?,?,?,?,?,?)",
                detail_rows,
            )
            self._truncate_detail(conn, instance_id)
            conn.commit()
        return len(hits)

    def _truncate_detail(self, conn: sqlite3.Connection, instance_id: str) -> None:
        """每实例仅保留最近 N 条明细，防明细表无限膨胀。

        用 ``rowid``（插入顺序单调）而非 ``ts``：同一秒内多条命中时 ``ts`` 重复，
        ``ts NOT IN (...)`` 会把同 ts 的行要么全留要么全删，无法精确截断；
        ``rowid`` 天然唯一，按其倒序留最近 N 条即可。
        """
        conn.execute(
            "DELETE FROM pageview_detail WHERE instance_id=? AND rowid NOT IN "
            "(SELECT rowid FROM pageview_detail WHERE instance_id=? "
            "ORDER BY rowid DESC LIMIT ?)",
            (instance_id, instance_id, _DETAIL_RETAIN),
        )

    # ---- 查询 ----

    def summary(self) -> dict[str, dict[str, Any]]:
        """每个实例的总命中数 / 实例级独立 IP / 最近命中时间 / 来源。

        BUG-089 修复：``uniqueIps`` 不再是"各天 unique_ips 之和"（同一 IP 跨天
        回访会被重复算），改用 ``pageview_ips`` 的 ``COUNT(DISTINCT remote)``
        得到真正的实例级独立访客数。
        """
        conn = self._conn_or_open()
        with self._lock:
            rows = conn.execute(
                "SELECT instance_id, SUM(hits) AS total, MAX(last_seen) AS last_seen, "
                "source FROM pageviews GROUP BY instance_id"
            ).fetchall()
            ip_rows = conn.execute(
                "SELECT instance_id, COUNT(DISTINCT remote) AS ips "
                "FROM pageview_ips GROUP BY instance_id"
            ).fetchall()
        ip_map = {r[0]: int(r[1]) for r in ip_rows}
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            iid = r[0]
            out[iid] = {
                "hits": int(r[1] or 0),
                "uniqueIps": ip_map.get(iid, 0),
                "lastSeen": r[2],
                "source": r[3],
            }
        return out

    def detail(self, instance_id: str, *, limit: int = 50) -> dict[str, Any]:
        """单实例详情：按天分布 + 最近明细行 + 数据来源。"""
        conn = self._conn_or_open()
        with self._lock:
            src_row = conn.execute(
                "SELECT source FROM pageviews WHERE instance_id=? LIMIT 1",
                (instance_id,),
            ).fetchone()
            days = conn.execute(
                "SELECT day, hits, unique_ips FROM pageviews WHERE instance_id=? "
                "ORDER BY day DESC LIMIT 30",
                (instance_id,),
            ).fetchall()
            recent = conn.execute(
                "SELECT ts, method, path, status, remote FROM pageview_detail "
                "WHERE instance_id=? ORDER BY ts DESC LIMIT ?",
                (instance_id, limit),
            ).fetchall()
        src = src_row[0] if src_row else None
        return {
            "instanceId": instance_id,
            "source": src,
            "byDay": [
                {"day": d[0], "hits": int(d[1] or 0), "uniqueIps": int(d[2] or 0)}
                for d in days
            ],
            "recent": [
                {
                    "ts": r[0],
                    "method": r[1],
                    "path": r[2],
                    "status": r[3],
                    "remote": r[4],
                }
                for r in recent
            ],
        }

    def clear_instance(self, instance_id: str) -> None:
        """清空实例所有浏览量数据（删除实例时调用，避免残留 / 同 ID 复用串数据）。

        游标按精确前缀匹配：``container:<iid>`` 与 ``builtin:<iid>:<path>``。
        ``caddy-shared:<path>`` 是共享游标（多实例共用同一 access log），不随单
        实例删除——历史用 ``%:<iid>%`` 的 LIKE 会把 ``builtin:demo-2:...`` 误伤
        （``:demo`` 是 ``:demo-2`` 的子串）。
        """
        conn = self._conn_or_open()
        with self._lock:
            conn.execute("DELETE FROM pageviews WHERE instance_id=?", (instance_id,))
            conn.execute(
                "DELETE FROM pageview_detail WHERE instance_id=?", (instance_id,)
            )
            conn.execute(
                "DELETE FROM pageview_ips WHERE instance_id=?", (instance_id,)
            )
            conn.execute(
                "DELETE FROM container_seen WHERE instance_id=?", (instance_id,)
            )
            conn.execute(
                "DELETE FROM ingest_cursor WHERE source_key=? "
                "OR source_key LIKE ?",
                (f"container:{instance_id}", f"builtin:{instance_id}:%"),
            )
            conn.commit()

    def filter_new_container_lines(
        self, instance_id: str, line_hashes: list[str]
    ) -> list[int]:
        """返回 ``line_hashes`` 中"首次见到"的下标，并把它们标记为已见（含裁剪）。

        用 ``INSERT OR IGNORE`` + ``rowcount`` 判定是否新插入；新插入即"之前没见过"。
        容器日志无可靠 per-request 唯一键，按原始行哈希去重可保证至多一次计数
        （BUG-088），代价是无时间戳的重复行（如 uvicorn 默认 access）会被保守
        去重——下溢优于上溢（管理页数字不会随刷新膨胀）。
        """
        if not line_hashes:
            return []
        conn = self._conn_or_open()
        new_idx: list[int] = []
        with self._lock:
            for i, lh in enumerate(line_hashes):
                cur = conn.execute(
                    "INSERT OR IGNORE INTO container_seen(instance_id, line_hash) "
                    "VALUES(?,?)",
                    (instance_id, lh),
                )
                if cur.rowcount > 0:
                    new_idx.append(i)
            conn.execute(
                "DELETE FROM container_seen WHERE instance_id=? AND rowid NOT IN "
                "(SELECT rowid FROM container_seen WHERE instance_id=? "
                "ORDER BY rowid DESC LIMIT ?)",
                (instance_id, instance_id, _CONTAINER_SEEN_RETAIN),
            )
            conn.commit()
        return new_idx


# ---- 摄入编排 ----------------------------------------------------------------


@dataclass
class _InstanceSource:
    instance_id: str
    kind: str  # builtin / caddy / container
    alias: str | None = None
    host_port: int | None = None


def _tail_new_lines(path: Path, cursor_key: str, store: PageviewStore) -> list[str]:
    """从游标处读取文件新增行，返回行列表并推进游标。

    文件不存在或被截断（体积小于游标）则重置游标。

    半行保护：写入方可能正写到最后一条行、尚未落 ``\\n``。若把游标推进到
    EOF，下次只会读到这条行的"续写"片段而丢失整行；故末尾无换行时回退到最后
    一个换行处，未完成行留给下次再读。
    """
    if not path.is_file():
        store.set_cursor(cursor_key, 0, None)
        return []
    offset, _ = store.get_cursor(cursor_key)
    size = path.stat().st_size
    if size < offset:
        # 日志轮转 / 被截断 → 从头开始
        offset = 0
    with path.open("rb") as fh:
        fh.seek(offset)
        new_bytes = fh.read()
    if not new_bytes:
        return []
    if not new_bytes.endswith(b"\n"):
        last_nl = new_bytes.rfind(b"\n")
        if last_nl == -1:
            # 尚无任何完整行：不推进游标，等写入方补完换行
            return []
        new_bytes = new_bytes[: last_nl + 1]
    store.set_cursor(cursor_key, offset + len(new_bytes), None)
    return new_bytes.decode("utf-8", "replace").splitlines()


def _line_hash(line: str) -> str:
    """容器日志行的稳定指纹（去重用）。"""
    return hashlib.md5(line.encode("utf-8", "replace")).hexdigest()


def _instance_sources(
    workspace: Workspace, config: Config, registry: Registry
) -> list[_InstanceSource]:
    """枚举所有实例及其访问量数据源类型。"""
    backend = _detect_static_backend(config)
    sources: list[_InstanceSource] = []
    for row in registry.list_instances():
        iid = str(row.get("id"))
        runtime = str(row.get("runtime") or "")
        serving = str(row.get("serving_mode") or "")
        if runtime == "docker-compose":
            sources.append(_InstanceSource(iid, "container"))
        elif serving == "shared-static" or runtime == "shared-static":
            kind = "caddy" if backend == "caddy" else "builtin"
            alias = _instance_alias(registry, iid)
            sources.append(_InstanceSource(iid, kind, alias=alias))
    return sources


def _detect_static_backend(config: Config) -> str:
    """与 :meth:`StaticGateway.detect_backend` 一致地判断静态后端。

    仅做 ``shutil.which("caddy")`` PATH 查找（无进程派生），保证 pageviews
    选择的日志源与网关层实际运行的后端一致：配置 caddy 但二进制缺失、或配置
    nginx 等未实现网关时，网关会降级 builtin 写 per-instance ``gateway.log``，
    此处也必须返回 ``builtin``，否则 builtin 的访问量会漏统计（BUG-091）。
    """
    configured = config.staticGateway
    if configured == "builtin":
        return "builtin"
    if configured == "caddy":
        if shutil.which("caddy"):
            return "caddy"
        return "builtin"
    # nginx 等尚未实现的网关：与 detect_backend 一致降级 builtin
    return "builtin"


def _instance_alias(registry: Registry, instance_id: str) -> str | None:
    try:
        row = registry.get_static_site(instance_id)
    except Exception:  # noqa: BLE001
        return None
    if not row:
        return None
    return str(row.get("route_host") or row.get("path_alias") or "") or None


def ingest_all(
    workspace: Workspace,
    config: Config,
    registry: Registry,
    store: PageviewStore,
) -> None:
    """惰性摄入所有实例的新增访问行（IMP-024）。

    逐实例分流到对应解析器；任何单实例失败只记 DEBUG 并跳过。Caddy 共享
    access log 在此处统一解析一次，再按别名前缀归属。

    进程内串行（:data:`_ingest_lock`）：避免并发请求读到同一游标重复摄入。
    """
    with _ingest_lock:
        sources = _instance_sources(workspace, config, registry)
        alias_to_id: dict[str, str] = {}

        # 1. Caddy 共享 access log（统一入口块写入）—— 按路径前缀归属
        caddy_log = workspace.root / ACCESS_LOG_REL
        if caddy_log.is_file():
            for s in sources:
                if s.kind == "caddy" and s.alias:
                    alias_to_id["/" + s.alias] = s.instance_id
            if alias_to_id:
                _ingest_caddy_shared(workspace, caddy_log, alias_to_id, store, sources)

        # 2. 逐实例：builtin gateway.log + container docker logs
        for s in sources:
            try:
                if s.kind == "builtin":
                    _ingest_builtin(workspace, s, store)
                elif s.kind == "container":
                    _ingest_container(workspace, registry, s, store)
            except Exception as exc:  # noqa: BLE001 — 单实例失败不阻断
                log.debug("摄入 %s 浏览量失败：%s", s.instance_id, exc)


def _ingest_builtin(
    workspace: Workspace, src: _InstanceSource, store: PageviewStore
) -> None:
    log_path = workspace.app_logs(src.instance_id) / "gateway.log"
    cursor_key = f"builtin:{src.instance_id}:{log_path.as_posix()}"
    lines = _tail_new_lines(log_path, cursor_key, store)
    hits = [h for h in (parse_clf_line(ln) for ln in lines) if h]
    if hits:
        store.record_hits(src.instance_id, "builtin", hits)


def _ingest_caddy_shared(
    workspace: Workspace,
    log_path: Path,
    alias_to_id: dict[str, str],
    store: PageviewStore,
    sources: list[_InstanceSource],
) -> None:
    """解析 Caddy 统一入口 access log，按 ``/<alias>/`` 前缀路由到实例。"""
    cursor_key = f"caddy-shared:{log_path.as_posix()}"
    lines = _tail_new_lines(log_path, cursor_key, store)
    if not lines:
        return
    # 按前缀长度降序，避免短别名被长别名前缀误吞（如 /api 与 /api-v2）
    prefixes = sorted(alias_to_id.keys(), key=len, reverse=True)
    per_instance: dict[str, list[AccessHit]] = defaultdict(list)
    for ln in lines:
        hit = parse_caddy_json_line(ln)
        if hit is None:
            continue
        for pfx in prefixes:
            if hit.path == pfx or hit.path.startswith(pfx + "/"):
                per_instance[alias_to_id[pfx]].append(hit)
                break
    for iid, hits in per_instance.items():
        store.record_hits(iid, "caddy", hits)


def _ingest_container(
    workspace: Workspace,
    registry: Registry,
    src: _InstanceSource,
    store: PageviewStore,
) -> None:
    """尽力摄入容器实例访问行（docker compose logs，近 N 行 + since 游标）。

    BUG-088 修复：``--since`` 直接用完整 ``last_ts``（RFC3339），不再截到日——
    否则同一天再次摄入会把当天旧日志整日重拉。docker ``--since`` 边界对上一秒
    的行会重复返回，故再用 ``container_seen`` 按原始行哈希去重，保证至多一次。
    """
    from local_webpage_access.docker_runtime import DockerRuntime

    if not DockerRuntime.is_available():
        return
    runtime = DockerRuntime(workspace, registry)
    cursor_key = f"container:{src.instance_id}"
    _, last_ts = store.get_cursor(cursor_key)
    since = last_ts  # 完整 RFC3339，不再截到日
    try:
        text = runtime.logs(src.instance_id, tail=_DOCKER_LOG_TAIL, since=since)
    except Exception as exc:  # noqa: BLE001
        log.debug("读取容器 %s 日志失败：%s", src.instance_id, exc)
        return
    raw_lines = text.splitlines()
    pairs: list[tuple[str, AccessHit]] = []
    for ln in raw_lines:
        h = parse_container_log_line(ln)
        if h is not None:
            pairs.append((_line_hash(ln), h))
    if not pairs:
        return
    new_idx = store.filter_new_container_lines(
        src.instance_id, [p[0] for p in pairs]
    )
    new_hits = [pairs[i][1] for i in new_idx]
    if new_hits:
        store.record_hits(src.instance_id, "container", new_hits)
        store.set_cursor(cursor_key, 0, max(h.ts for h in new_hits))


def clear_instance_pageviews(workspace: Workspace, instance_id: str) -> None:
    """删除实例时清空其浏览量数据（BUG-090）。

    开一个临时 :class:`PageviewStore`、清完即关；任何异常只记 DEBUG，绝不阻断
    实例删除主流程。CLI 与管理页两条删除路径都应调用此函数。
    """
    try:
        store = PageviewStore.for_workspace(workspace)
        try:
            store.clear_instance(instance_id)
        finally:
            store.close()
    except Exception as exc:  # noqa: BLE001
        log.debug("清理实例 %s 浏览量失败（忽略）：%s", instance_id, exc)


__all__ = [
    "PAGEVIEW_DB_FILENAME",
    "ACCESS_LOG_REL",
    "AccessHit",
    "PageviewStore",
    "clear_instance_pageviews",
    "parse_clf_line",
    "parse_caddy_json_line",
    "parse_container_log_line",
    "ingest_all",
]
