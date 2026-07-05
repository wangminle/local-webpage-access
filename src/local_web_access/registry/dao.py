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

from local_web_access.errors import RegistryError
from local_web_access.logging import get_logger, now_iso
from local_web_access.models import InstanceManifest
from local_web_access.registry.connection import init_db, transaction

log = get_logger("registry.dao")


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

    def close(self) -> None:
        if self._conn is not None:
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

    # ---- 实例 ---------------------------------------------------------------

    def upsert_instance(self, row: dict[str, Any]) -> None:
        """插入或更新实例主表行。

        ``row`` 应包含 instances 表的所有列（id 必填）。
        """
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
        self.upsert_instance(row)

        # 同步容器/静态配置；runtime 切换时清理另一侧残留子表（BUG-005）
        if data.get("container"):
            self.upsert_container(data["id"], data["container"])
            self.delete_static_site(data["id"])
        elif data.get("static"):
            self.upsert_static_site(data["id"], data["static"])
            self.delete_container(data["id"])

    def get_instance(self, instance_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM instances WHERE id = ?", (instance_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_instances(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM instances ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def instance_exists(self, instance_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM instances WHERE id = ?", (instance_id,)
        ).fetchone()
        return row is not None

    def update_status(
        self,
        instance_id: str,
        status: str,
        *,
        last_error: str | None = None,
        desired_state: str | None = None,
    ) -> None:
        """更新实例状态（WBS-05.11）。"""
        sets = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, now_iso()]
        if last_error is not None:
            sets.append("last_error = ?")
            params.append(last_error)
        if desired_state is not None:
            sets.append("desired_state = ?")
            params.append(desired_state)
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

    def delete_instance(self, instance_id: str) -> None:
        """删除实例（级联删除关联行，WBS-05.10）。"""
        with self.txn() as tx:
            tx.execute("DELETE FROM instances WHERE id = ?", (instance_id,))

    # ---- 容器 ---------------------------------------------------------------

    def upsert_container(self, instance_id: str, container: dict[str, Any]) -> None:
        rl = container.get("resourceLimits") or {}
        row = {
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
        cols = ", ".join(row.keys())
        placeholders = ", ".join(["?"] * len(row))
        updates = ", ".join(f"{c}=excluded.{c}" for c in row if c != "instance_id")
        sql = (
            f"INSERT INTO containers ({cols}) VALUES ({placeholders})"
            f" ON CONFLICT(instance_id) DO UPDATE SET {updates}"
        )
        with self.txn() as tx:
            tx.execute(sql, tuple(row.values()))

    def get_container(self, instance_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM containers WHERE instance_id = ?", (instance_id,)
        ).fetchone()
        return dict(row) if row else None

    def delete_container(self, instance_id: str) -> None:
        """删除容器子表行（runtime 切换清理用，BUG-005）。不存在时为空操作。"""
        with self.txn() as tx:
            tx.execute("DELETE FROM containers WHERE instance_id = ?", (instance_id,))

    # ---- 静态站点 -----------------------------------------------------------

    def upsert_static_site(self, instance_id: str, static: dict[str, Any]) -> None:
        row = {
            "instance_id": instance_id,
            "root_path": static.get("root", "public"),
            "gateway": static.get("gateway", "caddy"),
            "route_mode": static.get("routeMode", "port"),
            "host_port": static.get("hostPort"),
            "route_host": static.get("routeHost"),
            "gateway_config_path": static.get("gatewayConfigPath"),
            "enabled": 1 if static.get("enabled", True) else 0,
        }
        cols = ", ".join(row.keys())
        placeholders = ", ".join(["?"] * len(row))
        updates = ", ".join(f"{c}=excluded.{c}" for c in row if c != "instance_id")
        sql = (
            f"INSERT INTO static_sites ({cols}) VALUES ({placeholders})"
            f" ON CONFLICT(instance_id) DO UPDATE SET {updates}"
        )
        with self.txn() as tx:
            tx.execute(sql, tuple(row.values()))

    def get_static_site(self, instance_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM static_sites WHERE instance_id = ?", (instance_id,)
        ).fetchone()
        return dict(row) if row else None

    def set_static_enabled(self, instance_id: str, enabled: bool) -> None:
        with self.txn() as tx:
            tx.execute(
                "UPDATE static_sites SET enabled = ? WHERE instance_id = ?",
                (1 if enabled else 0, instance_id),
            )

    def delete_static_site(self, instance_id: str) -> None:
        """删除静态站点子表行（runtime 切换清理用，BUG-005）。不存在时为空操作。"""
        with self.txn() as tx:
            tx.execute(
                "DELETE FROM static_sites WHERE instance_id = ?", (instance_id,)
            )

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
            row = tx.execute(
                "SELECT instance_id FROM ports WHERE port = ?", (port,)
            ).fetchone()
            return row is not None and row["instance_id"] == instance_id

    def release_port(self, port: int) -> None:
        with self.txn() as tx:
            tx.execute("DELETE FROM ports WHERE port = ?", (port,))

    def release_instance_ports(self, instance_id: str) -> None:
        with self.txn() as tx:
            tx.execute("DELETE FROM ports WHERE instance_id = ?", (instance_id,))

    def allocated_ports(self) -> list[int]:
        rows = self.conn.execute("SELECT port FROM ports ORDER BY port").fetchall()
        return [int(r["port"]) for r in rows]

    def port_owner(self, port: int) -> str | None:
        row = self.conn.execute(
            "SELECT instance_id FROM ports WHERE port = ?", (port,)
        ).fetchone()
        return row["instance_id"] if row else None

    # ---- 事件（WBS-05.13）-------------------------------------------------

    def add_event(
        self, instance_id: str | None, event_type: str, message: str
    ) -> int:
        with self.txn() as tx:
            cur = tx.execute(
                "INSERT INTO events(instance_id, event_type, message, created_at) "
                "VALUES (?, ?, ?, ?)",
                (instance_id, event_type, message, now_iso()),
            )
            return int(cur.lastrowid)

    def list_events(
        self, instance_id: str | None = None, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        if instance_id is None:
            rows = self.conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE instance_id = ? ORDER BY id DESC LIMIT ?",
                (instance_id, limit),
            ).fetchall()
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
                "INSERT INTO builds(instance_id, status, started_at, log_path) "
                "VALUES (?, ?, ?, ?)",
                (instance_id, status, started_at or now_iso(), log_path),
            )
            return int(cur.lastrowid)

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
            rows = self.conn.execute(
                "SELECT * FROM builds ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM builds WHERE instance_id = ? ORDER BY id DESC LIMIT ?",
                (instance_id, limit),
            ).fetchall()
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
        updates = ", ".join(f"{c}=excluded.{c}" for c in row if c != "instance_id")
        sql = (
            f"INSERT INTO resources ({cols}) VALUES ({placeholders})"
            f" ON CONFLICT(instance_id) DO UPDATE SET {updates}"
        )
        with self.txn() as tx:
            tx.execute(sql, tuple(row.values()))

    def get_resources(self, instance_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM resources WHERE instance_id = ?", (instance_id,)
        ).fetchone()
        return dict(row) if row else None

    # ---- 统计（供管理页，WBS-05 观测）------------------------------------

    def status_counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM instances GROUP BY status"
        ).fetchall()
        return {r["status"]: int(r["n"]) for r in rows}

    def total_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM instances").fetchone()
        return int(row["n"])


__all__ = ["Registry"]
