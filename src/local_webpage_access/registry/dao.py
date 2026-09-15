"""Registry 数据访问对象。

封装七张表的增删改查，以及对 ``InstanceManifest`` 的同步。
所有写操作都在事务中执行。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from local_webpage_access.errors import RegistryError
from local_webpage_access.logging import get_logger, now_iso
from local_webpage_access.models import InstanceManifest
from local_webpage_access.registry.connection import (
    init_db,
    locked_connection,
    release_connection_lock,
    transaction,
)

log = get_logger("registry.dao")


def _canonical_json(payload: Any) -> str:
    """规范化 JSON（与 agent.contracts.canonical_json 同规则；registry 不反向依赖 agent 包）。"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

# BUG-473：instance 子表（列 instance_id 引用 instances.id）。
# delete_instance 显式清理这些表，不再依赖外键级联；find/purge_orphan_rows
# 也按此清单扫描存量孤儿。
_CHILD_TABLES: tuple[str, ...] = (
    "containers",
    "static_sites",
    "ports",
    "events",
    "builds",
    "resources",
)


class Registry:
    """SQLite registry 的高层访问接口。

    用法::

        reg = Registry(db_path)
        reg.open()
        try:
            reg.upsert_instance(...)
        finally:
            reg.close()
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    # ---- 生命周期 ----------------------------------------------------------

    def open(self) -> Registry:
        if self._conn is None:
            self._conn = init_db(self.db_path)
        return self

    def open_readonly(self, *, immutable: bool = False) -> Registry:
        """只读打开既有 registry，不创建文件、不迁移 schema（BUG-331）。

        ``immutable=True`` 时使用 SQLite ``immutable=1``，避免只读打开 WAL 库时
        物化 ``-wal``/``-shm`` 旁路文件（dry-run 零副作用，BUG-394）。
        """
        if self._conn is None:
            if not self.db_path.is_file():
                raise RegistryError(f"Registry 数据库不存在：{self.db_path}")
            q = "mode=ro"
            if immutable:
                q += "&immutable=1"
            uri = f"file:{quote(str(self.db_path))}?{q}"
            try:
                conn = sqlite3.connect(
                    uri,
                    uri=True,
                    check_same_thread=False,
                    isolation_level=None,
                )
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only=ON")
                conn.execute("PRAGMA busy_timeout=5000")
                self._conn = conn
            except (OSError, sqlite3.Error) as exc:
                raise RegistryError(f"只读打开 Registry 失败（{self.db_path}）：{exc}") from exc
        return self

    def close(self) -> None:
        if self._conn is not None:
            release_connection_lock(self._conn)
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Registry:
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RegistryError("Registry 未打开，请先调用 open() 或使用 with 语句")
        return self._conn

    @contextmanager
    def txn(self) -> Iterator[sqlite3.Connection]:
        try:
            with transaction(self.conn) as tx:
                yield tx
        except sqlite3.IntegrityError as exc:
            raise RegistryError(f"数据库完整性约束失败：{exc}") from exc
        except sqlite3.DatabaseError as exc:
            raise RegistryError(f"数据库操作失败：{exc}") from exc

    def _fetchone(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> sqlite3.Row | None:
        """线程安全的单行查询（BUG-052）。"""
        with locked_connection(self.conn) as conn:
            return conn.execute(sql, params).fetchone()

    def _fetchall(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> list[sqlite3.Row]:
        """线程安全的多行查询（BUG-052）。"""
        with locked_connection(self.conn) as conn:
            return conn.execute(sql, params).fetchall()

    # ---- 实例 ---------------------------------------------------------------

    def upsert_instance(self, row: dict[str, Any]) -> None:
        """插入或更新实例主表行。

        ``row`` 应包含 instances 表的所有列（id 必填）。
        """
        # 评审-组7：空 dict 会拼出 `INSERT INTO instances () VALUES ()` 非法 SQL
        if not row:
            raise RegistryError("upsert_instance 收到空行，拒绝生成非法 SQL")
        cols = ", ".join(row.keys())
        placeholders = ", ".join(["?"] * len(row))
        updates = ", ".join(f"{c}=excluded.{c}" for c in row if c != "id")
        sql = (
            f"INSERT INTO instances ({cols}) VALUES ({placeholders})"
            f"{' ON CONFLICT(id) DO UPDATE SET ' + updates if updates else ''}"
        )
        with self.txn() as tx:
            tx.execute(sql, tuple(row.values()))

    def upsert_from_manifest(
        self,
        manifest: InstanceManifest,
        *,
        app_path: str | None = None,
        source_zip_path: str | None = None,
    ) -> None:
        """把 :class:`InstanceManifest` 同步到 registry（WBS-05.16）。"""
        data = manifest.to_dict()
        row: dict[str, Any] = {
            "id": data["id"],
            "name": data["name"],
            "version": data["version"],
            "kind": data["kind"],
            "runtime": data["runtime"],
            "serving_mode": data["servingMode"],
            "resource_profile": data["resourceProfile"],
            "stack_json": json.dumps(data.get("stack", []), ensure_ascii=False),
            "has_database": 1 if data.get("hasDatabase") else 0,
            "database_type": data.get("database", {}).get("type") if data.get("database") else None,
            "database_json": (
                json.dumps(data["database"], ensure_ascii=False) if data.get("database") else None
            ),
            "desired_state": data["desiredState"],
            "status": data["status"],
            "app_path": app_path or data.get("appPath"),
            "source_zip_path": source_zip_path or data.get("sourceZipPath"),
            "created_at": data["createdAt"],
            "updated_at": data["updatedAt"],
            "last_started_at": data.get("lastStartedAt"),
            "last_health_check_at": data.get("lastHealthCheckAt"),
            "last_error": data.get("lastError"),
        }
        # BUG-333：主表、当前 runtime 子表及另一侧清理必须同一事务提交。
        with self.txn() as tx:
            self._upsert_mapping(tx, "instances", row, "id")
            if data.get("container"):
                container = self._container_row(data["id"], data["container"])
                self._upsert_mapping(tx, "containers", container, "instance_id")
                tx.execute("DELETE FROM static_sites WHERE instance_id = ?", (data["id"],))
            elif data.get("static"):
                static = self._static_row(data["id"], data["static"])
                self._upsert_mapping(tx, "static_sites", static, "instance_id")
                tx.execute("DELETE FROM containers WHERE instance_id = ?", (data["id"],))

    @staticmethod
    def _upsert_mapping(tx: sqlite3.Connection, table: str, row: dict[str, Any], key: str) -> None:
        cols = ", ".join(row.keys())
        placeholders = ", ".join(["?"] * len(row))
        updates = ", ".join(f"{c}=excluded.{c}" for c in row if c != key)
        tx.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT({key}) DO UPDATE SET {updates}",
            tuple(row.values()),
        )

    def get_instance(self, instance_id: str) -> dict[str, Any] | None:
        row = self._fetchone("SELECT * FROM instances WHERE id = ?", (instance_id,))
        return dict(row) if row else None

    def list_instances(self) -> list[dict[str, Any]]:
        rows = self._fetchall("SELECT * FROM instances ORDER BY created_at ASC")
        return [dict(r) for r in rows]

    def instance_exists(self, instance_id: str) -> bool:
        row = self._fetchone("SELECT 1 FROM instances WHERE id = ?", (instance_id,))
        return row is not None

    def update_status(
        self,
        instance_id: str,
        status: str,
        *,
        last_error: str | None = None,
        desired_state: str | None = None,
        observed_state: str | None = None,
        observation_error: str | None = ...,  # type: ignore[assignment]
        last_trusted_state: str | None = None,
        last_observed_at: str | None = None,
        runtime_access: str | None = None,
        clear_observation_error: bool = False,
        clear_last_error: bool = False,
    ) -> None:
        """更新实例状态（WBS-05.11；IMP-033 扩展观测字段）。

        ``observation_error`` 默认哨兵 ``...`` 表示不改该列；传 ``None`` 且
        ``clear_observation_error=True`` 时清空。
        ``last_error=None`` 同样表示不改该列（BUG-525）；要清空须
        ``clear_last_error=True``。
        """
        sets = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, now_iso()]
        if last_error is not None:
            sets.append("last_error = ?")
            params.append(last_error)
        elif clear_last_error:
            sets.append("last_error = ?")
            params.append(None)
        if desired_state is not None:
            sets.append("desired_state = ?")
            params.append(desired_state)
        if observed_state is not None:
            sets.append("observed_state = ?")
            params.append(observed_state)
        if observation_error is not ...:
            sets.append("observation_error = ?")
            params.append(observation_error)
        elif clear_observation_error:
            sets.append("observation_error = ?")
            params.append(None)
        if last_trusted_state is not None:
            sets.append("last_trusted_state = ?")
            params.append(last_trusted_state)
        if last_observed_at is not None:
            sets.append("last_observed_at = ?")
            params.append(last_observed_at)
        if runtime_access is not None:
            sets.append("runtime_access = ?")
            params.append(runtime_access)
        params.append(instance_id)
        with self.txn() as tx:
            tx.execute(f"UPDATE instances SET {', '.join(sets)} WHERE id = ?", tuple(params))

    def touch_instance(self, instance_id: str) -> None:
        """仅更新 updated_at。"""
        with self.txn() as tx:
            tx.execute(
                "UPDATE instances SET updated_at = ? WHERE id = ?",
                (now_iso(), instance_id),
            )

    def update_name(self, instance_id: str, name: str) -> None:
        """仅更新显示名（BUG-410：勿走 upsert_from_manifest，以免清空端口子表）。"""
        with self.txn() as tx:
            tx.execute(
                "UPDATE instances SET name = ?, updated_at = ? WHERE id = ?",
                (name, now_iso(), instance_id),
            )

    def record_started(self, instance_id: str) -> None:
        with self.txn() as tx:
            tx.execute(
                "UPDATE instances SET last_started_at = ?, updated_at = ? WHERE id = ?",
                (now_iso(), now_iso(), instance_id),
            )

    def record_health_check(self, instance_id: str) -> None:
        with self.txn() as tx:
            tx.execute(
                "UPDATE instances SET last_health_check_at = ?, updated_at = ? WHERE id = ?",
                (now_iso(), now_iso(), instance_id),
            )

    def set_last_error(self, instance_id: str, error: str | None) -> None:
        """仅写/清 ``last_error``，不改 ``status``（BUG-521）。

        健康检查只应记录探测结果，状态由 :func:`lifecycle.observe_status`
        基于进程态判定，避免健康抖动把实例状态打回旧值。
        """
        with self.txn() as tx:
            # 不刷新 updated_at：避免健康检查把 stale-building 兜底计时器重置。
            tx.execute(
                "UPDATE instances SET last_error = ? WHERE id = ?",
                (error, instance_id),
            )

    def delete_instance(self, instance_id: str) -> None:
        """删除实例（显式清理全部子表，WBS-05.10 / BUG-473）。

        不依赖外键 ``ON DELETE CASCADE``：级联只在执行删除的连接开了
        ``PRAGMA foreign_keys=ON`` 时生效，历史上子表行因连接绕过 ``connect()``
        而静默残留成孤儿（BUG-473）。此处同事务内逐表 DELETE 兜底，行为不再
        依赖 FK 开关。
        """
        with self.txn() as tx:
            for table in _CHILD_TABLES:
                tx.execute(f"DELETE FROM {table} WHERE instance_id = ?", (instance_id,))
            tx.execute("DELETE FROM instances WHERE id = ?", (instance_id,))

    # ---- 孤儿数据（BUG-473）-------------------------------------------------

    def find_orphan_rows(self) -> list[dict[str, Any]]:
        """扫描子表，返回引用了不存在 ``instances.id`` 的孤儿行（BUG-473）。

        孤儿 = 子表行的 ``instance_id`` 不在 ``instances`` 主表中（多为历史版本
        删除未级联残留，或备份/迁移拷入的脏数据）。返回
        ``[{"table": <表名>, "instance_id": <孤儿 id>}, ...]``。
        """
        orphans: list[dict[str, Any]] = []
        for table in _CHILD_TABLES:
            rows = self._fetchall(
                f"SELECT instance_id AS iid FROM {table} "
                "WHERE instance_id IS NOT NULL "
                "AND instance_id NOT IN (SELECT id FROM instances)"
            )
            for row in rows:
                orphans.append({"table": table, "instance_id": row["iid"]})
        return orphans

    def purge_orphan_rows(self) -> int:
        """删除全部孤儿子表行（无对应主表行），返回删除条数（BUG-473）。

        单事务内逐表 DELETE，不依赖外键级联。**破坏性操作**，调用方须先取得
        用户确认（见 ``lwa registry repair``）。
        """
        total = 0
        with self.txn() as tx:
            for table in _CHILD_TABLES:
                cur = tx.execute(
                    f"DELETE FROM {table} "
                    "WHERE instance_id IS NOT NULL "
                    "AND instance_id NOT IN (SELECT id FROM instances)"
                )
                if cur.rowcount > 0:
                    total += cur.rowcount
        return total

    # ---- 容器 ---------------------------------------------------------------

    def upsert_container(self, instance_id: str, container: dict[str, Any]) -> None:
        row = self._container_row(instance_id, container)
        with self.txn() as tx:
            self._upsert_mapping(tx, "containers", row, "instance_id")

    @staticmethod
    def _container_row(instance_id: str, container: dict[str, Any]) -> dict[str, Any]:
        rl = container.get("resourceLimits") or {}
        return {
            "instance_id": instance_id,
            "compose_project": container["projectName"],
            "service_name": container.get("serviceName", "app"),
            "image": container.get("image"),
            "image_id": container.get("imageId"),
            "container_id": container.get("containerId"),
            "internal_port": container.get("internalPort"),
            "host_port": container.get("hostPort"),
            "route_mode": container.get("routeMode", "port"),
            "route_host": container.get("routeHost"),
            "compose_path": container.get("composePath"),
            "dockerfile_path": container.get("dockerfilePath"),
            "memory_limit": rl.get("memory"),
            "cpu_limit": rl.get("cpus"),
        }

    def get_container(self, instance_id: str) -> dict[str, Any] | None:
        row = self._fetchone("SELECT * FROM containers WHERE instance_id = ?", (instance_id,))
        return dict(row) if row else None

    def delete_container(self, instance_id: str) -> None:
        """删除容器子表行（runtime 切换清理用，BUG-005）。不存在时为空操作。"""
        with self.txn() as tx:
            tx.execute("DELETE FROM containers WHERE instance_id = ?", (instance_id,))

    # ---- 静态站点 -----------------------------------------------------------

    def upsert_static_site(self, instance_id: str, static: dict[str, Any]) -> None:
        row = self._static_row(instance_id, static)
        with self.txn() as tx:
            self._upsert_mapping(tx, "static_sites", row, "instance_id")

    @staticmethod
    def _static_row(instance_id: str, static: dict[str, Any]) -> dict[str, Any]:
        return {
            "instance_id": instance_id,
            "root_path": static.get("root", "public"),
            "gateway": static.get("gateway", "caddy"),
            "route_mode": static.get("routeMode", "port"),
            "host_port": static.get("hostPort"),
            "route_host": static.get("routeHost"),
            "gateway_config_path": static.get("gatewayConfigPath"),
            "enabled": 1 if static.get("enabled", True) else 0,
        }

    def get_static_site(self, instance_id: str) -> dict[str, Any] | None:
        row = self._fetchone("SELECT * FROM static_sites WHERE instance_id = ?", (instance_id,))
        return dict(row) if row else None

    def list_route_hosts(self, *, exclude_instance: str | None = None) -> dict[str, str]:
        """IMP-006 / IMP-014：返回 ``{route_host: instance_id}`` 映射（仅 route_mode='name'）。

        用于路径别名全局唯一性校验，**跨静态站点与容器实例**（IMP-014 放开容器别名后，
        两类实例共用同一别名命名空间，避免重名）。``exclude_instance`` 指定的实例被跳过，
        便于实例更新自身别名时不与自身冲突。

        BUG-473：过滤孤儿子表行（``instance_id`` 不在 ``instances`` 主表），避免
        历史残留孤儿占用别名却 ``lwa list`` 查不到、挡住重新导入。
        """
        result: dict[str, str] = {}
        for table in ("static_sites", "containers"):
            rows = self._fetchall(
                f"SELECT instance_id, route_host FROM {table} "
                "WHERE route_mode = 'name' AND route_host IS NOT NULL "
                "AND instance_id IN (SELECT id FROM instances)"
            )
            for row in rows:
                iid = row["instance_id"]
                if exclude_instance is not None and iid == exclude_instance:
                    continue
                result[row["route_host"]] = iid
        return result

    def set_static_enabled(self, instance_id: str, enabled: bool) -> None:
        with self.txn() as tx:
            tx.execute(
                "UPDATE static_sites SET enabled = ? WHERE instance_id = ?",
                (1 if enabled else 0, instance_id),
            )

    def delete_static_site(self, instance_id: str) -> None:
        """删除静态站点子表行（runtime 切换清理用，BUG-005）。不存在时为空操作。"""
        with self.txn() as tx:
            tx.execute("DELETE FROM static_sites WHERE instance_id = ?", (instance_id,))

    # ---- 端口（WBS-05.12）--------------------------------------------------

    def allocate_port(self, instance_id: str, port: int) -> bool:
        """登记端口占用（并发安全，BUG-017）。

        返回 ``True`` 表示端口可由 ``instance_id`` 占用（首次登记或已由本实例
        占用）；返回 ``False`` 表示端口已被**其他实例**占用，调用方应跳过该
        端口。此前用 ``INSERT OR REPLACE``，两个并发分配会同时选中同一空闲
        端口，后写者覆盖前者的归属记录。改用 ``INSERT OR IGNORE`` 配合
        ``rowcount`` + 归属校验，让竞争中的输家得知并重试下一个端口。
        """
        with self.txn() as tx:
            cur = tx.execute(
                "INSERT OR IGNORE INTO ports(port, instance_id, status, created_at) "
                "VALUES (?, ?, 'allocated', ?)",
                (port, instance_id, now_iso()),
            )
            if cur.rowcount > 0:
                return True
            # 该端口已有记录但不是本次插入：判断归属
            row = tx.execute("SELECT instance_id FROM ports WHERE port = ?", (port,)).fetchone()
            return row is not None and row["instance_id"] == instance_id

    def release_port(self, port: int) -> None:
        with self.txn() as tx:
            tx.execute("DELETE FROM ports WHERE port = ?", (port,))

    def release_instance_ports(self, instance_id: str) -> None:
        with self.txn() as tx:
            tx.execute("DELETE FROM ports WHERE instance_id = ?", (instance_id,))

    def instance_ports(self, instance_id: str) -> list[int]:
        """返回实例当前登记的所有端口（BUG-510：回滚仅释放本轮新分配端口）。"""
        rows = self._fetchall(
            "SELECT port FROM ports WHERE instance_id = ? ORDER BY port",
            (instance_id,),
        )
        return [int(r["port"]) for r in rows]

    def allocated_ports(self) -> list[int]:
        rows = self._fetchall("SELECT port FROM ports ORDER BY port")
        return [int(r["port"]) for r in rows]

    def port_owner(self, port: int) -> str | None:
        row = self._fetchone("SELECT instance_id FROM ports WHERE port = ?", (port,))
        return row["instance_id"] if row else None

    # ---- 事件（WBS-05.13）-------------------------------------------------

    def add_event(self, instance_id: str | None, event_type: str, message: str) -> int:
        with self.txn() as tx:
            cur = tx.execute(
                "INSERT INTO events(instance_id, event_type, message, created_at) "
                "VALUES (?, ?, ?, ?)",
                (instance_id, event_type, message, now_iso()),
            )
            row_id = cur.lastrowid
            if row_id is None:
                raise RuntimeError("INSERT events 未返回 lastrowid")
            return int(row_id)

    def list_events(
        self, instance_id: str | None = None, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        if instance_id is None:
            rows = self._fetchall("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = self._fetchall(
                "SELECT * FROM events WHERE instance_id = ? ORDER BY id DESC LIMIT ?",
                (instance_id, limit),
            )
        return [dict(r) for r in rows]

    # ---- 构建记录（WBS-05.14）---------------------------------------------

    def add_build(
        self,
        instance_id: str,
        *,
        status: str = "running",
        started_at: str | None = None,
        log_path: str | None = None,
    ) -> int:
        with self.txn() as tx:
            cur = tx.execute(
                "INSERT INTO builds(instance_id, status, started_at, log_path) VALUES (?, ?, ?, ?)",
                (instance_id, status, started_at or now_iso(), log_path),
            )
            row_id = cur.lastrowid
            if row_id is None:
                raise RuntimeError("INSERT builds 未返回 lastrowid")
            new_id = int(row_id)
            # BUG-119：新构建启动时立刻关闭同实例其它 running 行，避免被后续
            # 成功记录遮蔽后永久残留。
            if status == "running":
                tx.execute(
                    "UPDATE builds SET status = ?, finished_at = ?, error_summary = ? "
                    "WHERE instance_id = ? AND status = 'running' AND id != ?",
                    (
                        "failed",
                        now_iso(),
                        "被后续构建取代",
                        instance_id,
                        new_id,
                    ),
                )
            return new_id

    def finish_build(
        self,
        build_id: int,
        *,
        status: str,
        error_summary: str | None = None,
    ) -> None:
        with self.txn() as tx:
            tx.execute(
                "UPDATE builds SET status = ?, finished_at = ?, error_summary = ? WHERE id = ?",
                (status, now_iso(), error_summary, build_id),
            )

    def list_builds(
        self, instance_id: str | None = None, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        if instance_id is None:
            rows = self._fetchall("SELECT * FROM builds ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = self._fetchall(
                "SELECT * FROM builds WHERE instance_id = ? ORDER BY id DESC LIMIT ?",
                (instance_id, limit),
            )
        return [dict(r) for r in rows]

    # ---- 资源快照（WBS-05.15）--------------------------------------------

    def upsert_resources(
        self,
        instance_id: str,
        *,
        source_size_bytes: int | None = None,
        public_size_bytes: int | None = None,
        data_size_bytes: int | None = None,
        image_size_bytes: int | None = None,
        last_memory_bytes: int | None = None,
        last_cpu_percent: float | None = None,
    ) -> None:
        """写入资源统计。``None`` 表示「本次不更新该列」，冲突时保留旧值。

        importer / update_zip 通常只传 source/data 体积；daemon 采集的
        ``image_size_bytes`` / ``last_memory_bytes`` / ``last_cpu_percent``
        不得被全量覆盖清成 NULL。
        """
        row = {
            "instance_id": instance_id,
            "source_size_bytes": source_size_bytes,
            "public_size_bytes": public_size_bytes,
            "data_size_bytes": data_size_bytes,
            "image_size_bytes": image_size_bytes,
            "last_memory_bytes": last_memory_bytes,
            "last_cpu_percent": last_cpu_percent,
            "updated_at": now_iso(),
        }
        cols = ", ".join(row.keys())
        placeholders = ", ".join(["?"] * len(row))
        # updated_at 始终刷新；其余列 COALESCE 保留已有非空值
        updates = ", ".join(
            (
                f"{c}=excluded.{c}"
                if c == "updated_at"
                else f"{c}=COALESCE(excluded.{c}, resources.{c})"
            )
            for c in row
            if c != "instance_id"
        )
        sql = (
            f"INSERT INTO resources ({cols}) VALUES ({placeholders})"
            f" ON CONFLICT(instance_id) DO UPDATE SET {updates}"
        )
        with self.txn() as tx:
            tx.execute(sql, tuple(row.values()))

    def get_resources(self, instance_id: str) -> dict[str, Any] | None:
        row = self._fetchone("SELECT * FROM resources WHERE instance_id = ?", (instance_id,))
        return dict(row) if row else None

    # ---- 统计（供管理页，WBS-05 观测）------------------------------------

    def status_counts(self) -> dict[str, int]:
        rows = self._fetchall("SELECT status, COUNT(*) AS n FROM instances GROUP BY status")
        return {r["status"]: int(r["n"]) for r in rows}

    def total_count(self) -> int:
        row = self._fetchone("SELECT COUNT(*) AS n FROM instances")
        if row is None:
            return 0
        return int(row["n"])

    # ---- Agent 协作（AGC-W05，schema v3）----------------------------------

    _AGENT_OP_COLS = (
        "operation_id, principal_id, workspace_id, action, target_instance_id, "
        "request_hash, idempotency_key, plan_id, status, phase, created_at, "
        "updated_at, worker_identity, lease_until, build_token, result_json, error_json"
    )

    def get_or_create_workspace_id(self) -> str:
        """返回本工作区稳定 UUID（R01：两工作区不误连的判据）。

        首次调用生成并写入 ``workspace_meta``；``INSERT OR IGNORE`` 保证
        多进程并发时只有一个 UUID 胜出，随后统一读回。
        """
        import uuid

        candidate = uuid.uuid4().hex
        with self.txn() as tx:
            tx.execute(
                "INSERT OR IGNORE INTO workspace_meta(key, value) VALUES ('workspace_id', ?)",
                (candidate,),
            )
        row = self._fetchone("SELECT value FROM workspace_meta WHERE key = 'workspace_id'")
        assert row is not None  # INSERT OR IGNORE 后必有值
        return str(row["value"])

    @staticmethod
    def _agent_op_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["result"] = json.loads(data.pop("result_json")) if data.get("result_json") else None
        data["error"] = json.loads(data.pop("error_json")) if data.get("error_json") else None
        return data

    def create_agent_operation(self, record: dict[str, Any]) -> dict[str, Any]:
        """幂等创建操作记录（§6.3）。

        - 相同 ``request_hash`` 复用键：返回既有记录（调用方生成的 operationId 被丢弃）；
        - 不同 ``request_hash`` 复用键：抛 ``RegistryError(code="idempotency_conflict")``；
        - 并发安全依赖 UNIQUE(principal_id, workspace_id, idempotency_key)——
          落败方进入事务重读路径，两连接同键并发只产生一行。
        """
        row = {
            "operation_id": record["operation_id"],
            "principal_id": record["principal_id"],
            "workspace_id": record["workspace_id"],
            "action": record["action"],
            "target_instance_id": record.get("target_instance_id"),
            "request_hash": record["request_hash"],
            "idempotency_key": record["idempotency_key"],
            "plan_id": record.get("plan_id"),
            "status": record["status"],
            "phase": record.get("phase"),
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "worker_identity": record.get("worker_identity"),
            "lease_until": record.get("lease_until"),
            "build_token": record.get("build_token"),
            "result_json": (
                None if record.get("result") is None else _canonical_json(record["result"])
            ),
            "error_json": (
                None if record.get("error") is None else _canonical_json(record["error"])
            ),
        }
        cols = ", ".join(row.keys())
        placeholders = ", ".join(["?"] * len(row))
        try:
            with self.txn() as tx:
                tx.execute(
                    f"INSERT INTO agent_operations ({cols}) VALUES ({placeholders})",
                    tuple(row.values()),
                )
        except RegistryError:
            # txn() 会把 UNIQUE 冲突包成通用 RegistryError；以"同键行是否存在"
            # 判定语义（不匹配异常文本）。不存在则属其他完整性问题，原样上抛。
            existing = self.get_agent_operation_by_idempotency(
                row["principal_id"], row["workspace_id"], row["idempotency_key"]
            )
            if existing is None:
                raise
            if existing["request_hash"] != row["request_hash"]:
                raise RegistryError(
                    "幂等键已绑定不同请求内容",
                    code="idempotency_conflict",
                    idempotency_key=row["idempotency_key"],
                ) from None
            return existing
        inserted = self._fetchone(
            "SELECT * FROM agent_operations WHERE operation_id = ?", (row["operation_id"],)
        )
        assert inserted is not None  # 刚插入必存在
        return self._agent_op_row_to_dict(inserted)

    def get_agent_operation(self, operation_id: str) -> dict[str, Any] | None:
        row = self._fetchone(
            f"SELECT {self._AGENT_OP_COLS} FROM agent_operations WHERE operation_id = ?",
            (operation_id,),
        )
        return self._agent_op_row_to_dict(row) if row else None

    def get_agent_operation_by_idempotency(
        self, principal_id: str, workspace_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        row = self._fetchone(
            f"SELECT {self._AGENT_OP_COLS} FROM agent_operations"
            " WHERE principal_id = ? AND workspace_id = ? AND idempotency_key = ?",
            (principal_id, workspace_id, idempotency_key),
        )
        return self._agent_op_row_to_dict(row) if row else None

    def update_agent_operation(
        self, operation_id: str, *, updated_at: str, **fields: Any
    ) -> bool:
        """更新操作的可变列；``updated_at`` 必传以维持 §6.3 的更新时间语义。

        返回是否命中（operation 不存在返回 False）。
        """
        allowed = {
            "status", "phase", "worker_identity", "lease_until",
            "build_token", "result", "error",
        }
        illegal = set(fields) - allowed
        if illegal:
            raise RegistryError(f"不允许更新的列: {sorted(illegal)}", code="AGENT_OP_FIELD")
        sets = ["updated_at = ?"]
        params: list[Any] = [updated_at]
        for key, value in fields.items():
            if key in ("result", "error"):
                sets.append(f"{key}_json = ?")
                params.append(_canonical_json(value) if value is not None else None)
            else:
                sets.append(f"{key} = ?")
                params.append(value)
        params.append(operation_id)
        with self.txn() as tx:
            cur = tx.execute(
                f"UPDATE agent_operations SET {', '.join(sets)} WHERE operation_id = ?",
                tuple(params),
            )
            return cur.rowcount > 0

    def insert_agent_plan(self, record: dict[str, Any]) -> None:
        cols = ", ".join(record.keys())
        placeholders = ", ".join(["?"] * len(record))
        with self.txn() as tx:
            tx.execute(
                f"INSERT INTO agent_plans ({cols}) VALUES ({placeholders})",
                tuple(record.values()),
            )

    def get_agent_plan(self, plan_id: str) -> dict[str, Any] | None:
        row = self._fetchone("SELECT * FROM agent_plans WHERE plan_id = ?", (plan_id,))
        return dict(row) if row else None


__all__ = ["Registry"]
