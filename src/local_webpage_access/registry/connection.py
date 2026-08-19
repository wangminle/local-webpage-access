"""SQLite 连接管理与 schema 迁移。

- 连接默认启用 WAL 模式，适合 daemon 与 CLI 交替访问。
- 迁移基于 ``schema_version`` 表，按版本号顺序执行 DDL。
- DDL 对应 V1 设计说明第 9 节的七张表。
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from local_webpage_access.errors import RegistryError
from local_webpage_access.logging import get_logger

log = get_logger("registry")

CURRENT_SCHEMA_VERSION = 2

# ---- DDL --------------------------------------------------------------------

_SCHEMAS: dict[int, list[str]] = {
    1: [
        # 版本记录
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version     INTEGER PRIMARY KEY,
            applied_at  TEXT NOT NULL
        )
        """,
        # 实例主表
        """
        CREATE TABLE IF NOT EXISTS instances (
            id                     TEXT PRIMARY KEY,
            name                   TEXT NOT NULL,
            version                TEXT NOT NULL,
            kind                   TEXT NOT NULL,
            runtime                TEXT NOT NULL,
            serving_mode           TEXT NOT NULL,
            resource_profile       TEXT NOT NULL DEFAULT 'small',
            stack_json             TEXT NOT NULL DEFAULT '[]',
            has_database           INTEGER NOT NULL DEFAULT 0,
            database_type          TEXT,
            database_json          TEXT,
            desired_state          TEXT NOT NULL DEFAULT 'stopped',
            status                 TEXT NOT NULL DEFAULT 'pending',
            app_path               TEXT,
            source_zip_path        TEXT,
            created_at             TEXT NOT NULL,
            updated_at             TEXT NOT NULL,
            last_started_at        TEXT,
            last_health_check_at   TEXT,
            last_error             TEXT
        )
        """,
        # 容器配置
        """
        CREATE TABLE IF NOT EXISTS containers (
            instance_id     TEXT PRIMARY KEY REFERENCES instances(id) ON DELETE CASCADE,
            compose_project TEXT NOT NULL,
            service_name    TEXT NOT NULL DEFAULT 'app',
            image           TEXT,
            image_id        TEXT,
            container_id    TEXT,
            internal_port   INTEGER,
            host_port       INTEGER,
            route_mode      TEXT NOT NULL DEFAULT 'port',
            route_host      TEXT,
            compose_path    TEXT,
            dockerfile_path TEXT,
            memory_limit    TEXT,
            cpu_limit       TEXT
        )
        """,
        # 静态站点
        """
        CREATE TABLE IF NOT EXISTS static_sites (
            instance_id          TEXT PRIMARY KEY REFERENCES instances(id) ON DELETE CASCADE,
            root_path            TEXT NOT NULL DEFAULT 'public',
            gateway              TEXT NOT NULL DEFAULT 'caddy',
            route_mode           TEXT NOT NULL DEFAULT 'port',
            host_port            INTEGER,
            route_host           TEXT,
            gateway_config_path  TEXT,
            enabled              INTEGER NOT NULL DEFAULT 1
        )
        """,
        # 端口占用
        """
        CREATE TABLE IF NOT EXISTS ports (
            port         INTEGER PRIMARY KEY,
            instance_id  TEXT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
            status       TEXT NOT NULL DEFAULT 'allocated',
            created_at   TEXT NOT NULL
        )
        """,
        # 事件
        """
        CREATE TABLE IF NOT EXISTS events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id  TEXT REFERENCES instances(id) ON DELETE CASCADE,
            event_type   TEXT NOT NULL,
            message      TEXT NOT NULL,
            created_at   TEXT NOT NULL
        )
        """,
        # 构建记录
        """
        CREATE TABLE IF NOT EXISTS builds (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id    TEXT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
            status         TEXT NOT NULL,
            started_at     TEXT,
            finished_at    TEXT,
            log_path       TEXT,
            error_summary  TEXT
        )
        """,
        # 资源快照
        """
        CREATE TABLE IF NOT EXISTS resources (
            instance_id          TEXT PRIMARY KEY REFERENCES instances(id) ON DELETE CASCADE,
            source_size_bytes    INTEGER,
            public_size_bytes    INTEGER,
            data_size_bytes      INTEGER,
            image_size_bytes     INTEGER,
            last_memory_bytes    INTEGER,
            last_cpu_percent     REAL,
            updated_at           TEXT NOT NULL
        )
        """,
        # 常用索引
        "CREATE INDEX IF NOT EXISTS idx_instances_status ON instances(status)",
        "CREATE INDEX IF NOT EXISTS idx_events_instance ON events(instance_id)",
        "CREATE INDEX IF NOT EXISTS idx_builds_instance ON builds(instance_id)",
        "CREATE INDEX IF NOT EXISTS idx_ports_instance ON ports(instance_id)",
    ],
    # IMP-033 / 033.02：观测态与最后可信状态（兼容保留 status）
    2: [
        "ALTER TABLE instances ADD COLUMN observed_state TEXT",
        "ALTER TABLE instances ADD COLUMN observation_error TEXT",
        "ALTER TABLE instances ADD COLUMN last_trusted_state TEXT",
        "ALTER TABLE instances ADD COLUMN last_observed_at TEXT",
        "ALTER TABLE instances ADD COLUMN runtime_access TEXT",
    ],
}


# ---- 连接 -------------------------------------------------------------------


def connect(db_path: Path) -> sqlite3.Connection:
    """创建并配置 SQLite 连接（WAL + 外键 + Row 工厂）。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(db_path),
        check_same_thread=False,
        isolation_level=None,  # 手动事务
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    # BUG-363：显式 busy_timeout，避免与 pageviews 等并发写时立即抛 database is locked。
    # （sqlite3.connect 默认 timeout=5.0 等价 5000ms，此处写明意图并与调用方对齐。）
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """事务上下文：成功 commit，异常 rollback。"""
    lock = _get_lock(conn)
    with lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            # 评审-组7：COMMIT 失败后事务可能已不存在，裸 ROLLBACK 再抛会
            # 掩盖原始异常（磁盘 I/O 等）；抑制回滚失败、保留原错误上抛。
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise


# 每个连接绑定一个锁，避免同连接并发写。
# sqlite3.Connection 既不可弱引用、也不可 setattr，故用 id(conn) 字典。
# 实践中每个 Registry 长持一个连接，条目≈活跃 Registry；短生命周期测试
# 场景下泄漏有限，可接受（无法用 WeakKeyDictionary / 实例属性）。
_LOCKS: dict[int, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _get_lock(conn: sqlite3.Connection) -> threading.RLock:
    key = id(conn)
    with _LOCKS_GUARD:
        if key not in _LOCKS:
            _LOCKS[key] = threading.RLock()
        return _LOCKS[key]


def release_connection_lock(conn: sqlite3.Connection) -> None:
    """连接关闭时清理对应锁条目，避免 ``_LOCKS`` 无限增长。"""
    key = id(conn)
    with _LOCKS_GUARD:
        _LOCKS.pop(key, None)


@contextmanager
def locked_connection(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """串行化同一 SQLite 连接上的所有访问（读/写）。

    ``transaction()`` 已用连接级锁保护写事务；裸 ``conn.execute`` 读操作
    也必须走此锁，否则管理页多线程并发 ``/api/stats`` + ``/api/instances``
    时会触发 ``InterfaceError`` 并把运行中实例误判为 stopped（BUG-052）。
    """
    with _get_lock(conn):
        yield conn


# ---- 迁移 -------------------------------------------------------------------


def get_schema_version(conn: sqlite3.Connection) -> int:
    """读取当前 schema 版本；未初始化时返回 0。"""
    try:
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    except sqlite3.OperationalError:
        return 0
    if row is None or row["v"] is None:
        return 0
    return int(row["v"])


def migrate(conn: sqlite3.Connection) -> int:
    """执行所有待应用的迁移，返回应用后的版本号。"""
    from local_webpage_access.logging import now_iso

    target = max(_SCHEMAS)
    while True:
        with transaction(conn) as tx:
            # BUG-294：版本读取必须在 BEGIN IMMEDIATE 之后；否则两个进程都读到
            # 旧版本，后获得写锁者会重复执行 ALTER TABLE。
            current = get_schema_version(tx)
            if current >= target:
                log.debug("schema 已是最新版本 %d", current)
                return current
            version = current + 1
            statements = _SCHEMAS[version]
            for stmt in statements:
                tx.execute(stmt)
            # schema_version 表在 v1 第一批 DDL 中创建
            tx.execute(
                "INSERT OR REPLACE INTO schema_version(version, applied_at) VALUES (?, ?)",
                (version, now_iso()),
            )
        log.info("已应用 schema 迁移到版本 %d", version)


def init_db(db_path: Path) -> sqlite3.Connection:
    """连接数据库并迁移到最新版本。"""
    conn: sqlite3.Connection | None = None
    try:
        conn = connect(db_path)
        migrate(conn)
    except (OSError, sqlite3.Error) as exc:
        if conn is not None:
            release_connection_lock(conn)  # 评审-组7：与 Registry.close 对齐，防 _LOCKS 泄漏
            conn.close()
        # BUG-316：包装原生 sqlite3/OSError，附路径与排查建议，避免 traceback 直抛用户。
        raise RegistryError(
            f"数据库初始化失败：{exc}；请检查路径权限、磁盘空间与是否被其他进程占用",
            path=str(db_path),
        ) from exc
    assert conn is not None
    return conn


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "connect",
    "transaction",
    "get_schema_version",
    "migrate",
    "init_db",
]
