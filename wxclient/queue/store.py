from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import (
    Column,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    create_engine,
    delete,
    select,
    update,
)
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.pool import NullPool

from .models import (
    Task,
    TaskStatus,
    TaskType,
    is_real_write,
    lease_deadline,
    task_idempotency_key,
    utcnow,
)

logger = structlog.get_logger(__name__)


class TaskStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        database_url = f"sqlite+pysqlite:///{self.database_path.resolve().as_posix()}"
        self.engine: Engine = create_engine(database_url, future=True, poolclass=NullPool)
        self.metadata = MetaData()
        self.tasks = Table(
            "tasks",
            self.metadata,
            Column("id", String, primary_key=True),
            Column("type", String, nullable=False),
            Column("payload", Text, nullable=False),
            Column("status", String, nullable=False),
            Column("account_id", String, nullable=False),
            Column("idempotency_key", String),
            Column("attempts", Integer, nullable=False),
            Column("max_attempts", Integer, nullable=False),
            Column("created_at", String, nullable=False),
            Column("updated_at", String, nullable=False),
            Column("available_at", String, nullable=False),
            Column("lease_owner", String),
            Column("lease_expires_at", String),
            Column("last_error", Text),
            Column("result", Text),
            Index("idx_tasks_pick", "status", "available_at", "created_at"),
            Index(
                "idx_tasks_dedupe",
                "account_id",
                "type",
                "idempotency_key",
                "status",
            ),
        )
        self.worker_leases = Table(
            "worker_leases",
            self.metadata,
            Column("name", String, primary_key=True),
            Column("owner_id", String, nullable=False),
            Column("lease_expires_at", String, nullable=False),
            Column("updated_at", String, nullable=False),
        )
        self._init_schema()

    def _init_schema(self) -> None:
        self.metadata.create_all(self.engine)
        migration_now = utcnow()
        with self.engine.begin() as conn:
            columns = {
                str(row[1])
                for row in conn.exec_driver_sql("PRAGMA table_info(tasks)").fetchall()
            }
            if "idempotency_key" not in columns:
                conn.exec_driver_sql("ALTER TABLE tasks ADD COLUMN idempotency_key VARCHAR")
            if "lease_owner" not in columns:
                conn.exec_driver_sql("ALTER TABLE tasks ADD COLUMN lease_owner VARCHAR")
            if "lease_expires_at" not in columns:
                conn.exec_driver_sql("ALTER TABLE tasks ADD COLUMN lease_expires_at VARCHAR")
            legacy_rows = conn.execute(
                select(
                    self.tasks.c.id,
                    self.tasks.c.type,
                    self.tasks.c.payload,
                    self.tasks.c.account_id,
                ).where(self.tasks.c.idempotency_key.is_(None))
            ).mappings().fetchall()
            if legacy_rows:
                # A previous release may already have created this partial
                # index while legacy rows still had NULL keys. Drop it inside
                # the schema transaction so every key can be backfilled before
                # duplicate active rows are resolved and uniqueness is restored.
                conn.exec_driver_sql(
                    "DROP INDEX IF EXISTS uq_tasks_active_idempotency"
                )
                for row in legacy_rows:
                    payload = json.loads(row["payload"])
                    conn.execute(
                        update(self.tasks)
                        .where(self.tasks.c.id == row["id"])
                        .values(
                            idempotency_key=task_idempotency_key(
                                TaskType(row["type"]),
                                payload,
                                row["account_id"],
                            )
                        )
                    )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS idx_tasks_dedupe "
                "ON tasks (account_id, type, idempotency_key, status)"
            )
            conn.exec_driver_sql(
                "UPDATE tasks SET status = 'cancelled', "
                "last_error = '迁移时取消重复的活动任务' WHERE id IN ("
                "SELECT id FROM (SELECT id, ROW_NUMBER() OVER ("
                "PARTITION BY account_id, type, idempotency_key ORDER BY "
                "CASE status WHEN 'running' THEN 0 ELSE 1 END, created_at, id"
                ") AS duplicate_number FROM tasks WHERE idempotency_key IS NOT NULL "
                "AND status IN ('pending', 'running')) WHERE duplicate_number > 1)"
            )
            conn.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_active_idempotency "
                "ON tasks (account_id, type, idempotency_key) "
                "WHERE idempotency_key IS NOT NULL AND status IN ('pending', 'running')"
            )
            # Databases created before leases can contain RUNNING rows without
            # an expiry. Treat them as expired on upgrade: reads may be retried,
            # while real writes are moved to an unknown terminal outcome.
            conn.execute(
                update(self.tasks)
                .where(
                    and_(
                        self.tasks.c.status == TaskStatus.RUNNING.value,
                        self.tasks.c.lease_expires_at.is_(None),
                    )
                )
                .values(lease_expires_at=migration_now)
            )
        self.recover_expired()

    def create_task(self, task_type: TaskType, payload: dict[str, Any], account_id: str) -> Task:
        idempotency_key = task_idempotency_key(task_type, payload, account_id)
        with self.engine.begin() as conn:
            active_row = conn.execute(
                select(self.tasks)
                .where(
                    and_(
                        self.tasks.c.account_id == account_id,
                        self.tasks.c.type == task_type.value,
                        self.tasks.c.idempotency_key == idempotency_key,
                        self.tasks.c.status.in_(
                            [TaskStatus.PENDING.value, TaskStatus.RUNNING.value]
                        ),
                    )
                )
                .order_by(self.tasks.c.created_at.asc())
                .limit(1)
            ).mappings().fetchone()
            if active_row:
                active_task = self._from_row(active_row)
                logger.info(
                    "task.duplicate_skipped",
                    task_type=task_type.value,
                    account_id=account_id,
                    active_task_id=active_task.id,
                    active_task_status=active_task.status.value,
                )
                return active_task

            task = Task.new(task_type, payload, account_id=account_id)
            inserted = conn.execute(
                sqlite_insert(self.tasks)
                .values(self._to_row(task))
                .on_conflict_do_nothing()
            )
            if inserted.rowcount:
                return task
            active_row = conn.execute(
                select(self.tasks)
                .where(
                    and_(
                        self.tasks.c.account_id == account_id,
                        self.tasks.c.type == task_type.value,
                        self.tasks.c.idempotency_key == idempotency_key,
                        self.tasks.c.status.in_(
                            [TaskStatus.PENDING.value, TaskStatus.RUNNING.value]
                        ),
                    )
                )
                .limit(1)
            ).mappings().one()
        return self._from_row(active_row)

    def claim_next(self, owner_id: str, lease_seconds: float = 30.0) -> Task | None:
        self.recover_expired()
        now = utcnow()
        candidate_id = (
            select(self.tasks.c.id)
            .where(
                and_(
                    self.tasks.c.status == TaskStatus.PENDING.value,
                    self.tasks.c.available_at <= now,
                )
            )
            .order_by(
                self.tasks.c.available_at.asc(),
                self.tasks.c.created_at.asc(),
            )
            .limit(1)
            .scalar_subquery()
        )
        with self.engine.begin() as conn:
            row = conn.execute(
                update(self.tasks)
                .where(
                    and_(
                        self.tasks.c.id == candidate_id,
                        self.tasks.c.status == TaskStatus.PENDING.value,
                    )
                )
                .values(
                    status=TaskStatus.RUNNING.value,
                    attempts=self.tasks.c.attempts + 1,
                    updated_at=now,
                    lease_owner=owner_id,
                    lease_expires_at=lease_deadline(lease_seconds),
                )
                .returning(*self.tasks.c)
            ).mappings().fetchone()
        return self._from_row(row) if row else None

    def next_pending(self) -> Task | None:
        """Compatibility wrapper for callers that have not adopted leases yet."""
        return self.claim_next("legacy-worker")

    def recover_expired(self) -> int:
        now = utcnow()
        recovered = 0
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(self.tasks)
                .where(
                    and_(
                        self.tasks.c.status == TaskStatus.RUNNING.value,
                        self.tasks.c.lease_expires_at.is_not(None),
                        self.tasks.c.lease_expires_at <= now,
                    )
                )
            ).mappings().fetchall()
            for row in rows:
                task = self._from_row(row)
                values: dict[str, Any] = {
                    "updated_at": now,
                    "lease_owner": None,
                    "lease_expires_at": None,
                }
                if is_real_write(task):
                    values.update(
                        status=TaskStatus.FAILED.value,
                        last_error="真实写操作的任务租约过期，执行结果未知，已禁止自动重试",
                        result=json.dumps(
                            {
                                "outcome": "unknown",
                                "retry_suppressed": True,
                                "reason": "lease_expired",
                            },
                            ensure_ascii=False,
                        ),
                    )
                elif task.attempts < task.max_attempts:
                    values.update(
                        status=TaskStatus.PENDING.value,
                        available_at=now,
                        last_error="任务租约过期，已等待恢复",
                    )
                else:
                    values.update(
                        status=TaskStatus.FAILED.value,
                        last_error="任务租约过期且已达到最大尝试次数",
                    )
                result = conn.execute(
                    update(self.tasks)
                    .where(
                        and_(
                            self.tasks.c.id == task.id,
                            self.tasks.c.status == TaskStatus.RUNNING.value,
                            self.tasks.c.lease_expires_at <= now,
                        )
                    )
                    .values(**values)
                )
                recovered += int(result.rowcount or 0)
        return recovered

    def renew_task(self, task_id: str, owner_id: str, lease_seconds: float) -> bool:
        with self.engine.begin() as conn:
            result = conn.execute(
                update(self.tasks)
                .where(
                    and_(
                        self.tasks.c.id == task_id,
                        self.tasks.c.status == TaskStatus.RUNNING.value,
                        self.tasks.c.lease_owner == owner_id,
                    )
                )
                .values(
                    lease_expires_at=lease_deadline(lease_seconds),
                    updated_at=utcnow(),
                )
            )
        return bool(result.rowcount)

    def yield_task(
        self,
        task_id: str,
        owner_id: str,
        payload: dict[str, Any],
        delay_seconds: float = 0.1,
    ) -> bool:
        """Return a cooperative continuation without spending a retry attempt."""
        now = utcnow()
        with self.engine.begin() as conn:
            result = conn.execute(
                update(self.tasks)
                .where(
                    and_(
                        self.tasks.c.id == task_id,
                        self.tasks.c.status == TaskStatus.RUNNING.value,
                        self.tasks.c.lease_owner == owner_id,
                    )
                )
                .values(
                    payload=json.dumps(payload, ensure_ascii=False),
                    status=TaskStatus.PENDING.value,
                    attempts=self.tasks.c.attempts - 1,
                    available_at=lease_deadline(delay_seconds),
                    updated_at=now,
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )
        return bool(result.rowcount)

    def acquire_worker(self, owner_id: str, lease_seconds: float = 30.0) -> bool:
        now = utcnow()
        expires_at = lease_deadline(lease_seconds)
        with self.engine.begin() as conn:
            updated = conn.execute(
                update(self.worker_leases)
                .where(
                    and_(
                        self.worker_leases.c.name == "wechat-ui",
                        (
                            (self.worker_leases.c.owner_id == owner_id)
                            | (self.worker_leases.c.lease_expires_at <= now)
                        ),
                    )
                )
                .values(owner_id=owner_id, lease_expires_at=expires_at, updated_at=now)
            )
            if updated.rowcount:
                return True
            existing = conn.execute(
                select(self.worker_leases.c.owner_id).where(
                    self.worker_leases.c.name == "wechat-ui"
                )
            ).scalar_one_or_none()
            if existing is not None:
                return False
            inserted = conn.execute(
                sqlite_insert(self.worker_leases)
                .values(
                    name="wechat-ui",
                    owner_id=owner_id,
                    lease_expires_at=expires_at,
                    updated_at=now,
                )
                .on_conflict_do_nothing()
            )
        return bool(inserted.rowcount)

    def renew_worker(self, owner_id: str, lease_seconds: float = 30.0) -> bool:
        return self.acquire_worker(owner_id, lease_seconds)

    def release_worker(self, owner_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                delete(self.worker_leases).where(
                    and_(
                        self.worker_leases.c.name == "wechat-ui",
                        self.worker_leases.c.owner_id == owner_id,
                    )
                )
            )

    def complete(
        self,
        task_id: str,
        result: dict[str, Any] | None = None,
        owner_id: str | None = None,
    ) -> bool:
        return self._set_terminal(
            task_id,
            TaskStatus.SUCCEEDED,
            result=result,
            owner_id=owner_id,
        )

    def fail(
        self,
        task_id: str,
        error: str,
        retry: bool = False,
        owner_id: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> bool:
        task = self.get(task_id)
        if retry and task and task.attempts < task.max_attempts:
            status = TaskStatus.PENDING
        else:
            status = TaskStatus.FAILED
        return self._set_terminal(
            task_id,
            status,
            error=error,
            result=result,
            owner_id=owner_id,
        )

    def block(self, task_id: str, error: str, owner_id: str | None = None) -> bool:
        return self._set_terminal(
            task_id,
            TaskStatus.BLOCKED,
            error=error,
            owner_id=owner_id,
        )

    def list_tasks(
        self,
        limit: int = 50,
        task_types: set[TaskType | str] | None = None,
        statuses: set[TaskStatus | str] | None = None,
    ) -> list[Task]:
        query = select(self.tasks)
        type_values = _task_type_values(task_types)
        if type_values:
            query = query.where(self.tasks.c.type.in_(type_values))
        status_values = _task_status_values(statuses)
        if status_values:
            query = query.where(self.tasks.c.status.in_(status_values))
        with self.engine.connect() as conn:
            rows = conn.execute(
                query.order_by(self.tasks.c.updated_at.desc()).limit(limit)
            ).mappings().fetchall()
        return [self._from_row(row) for row in rows]

    def delete_tasks(
        self,
        task_types: set[TaskType | str] | None = None,
        statuses: set[TaskStatus | str] | None = None,
    ) -> int:
        query = delete(self.tasks)
        clauses: list[Any] = []
        type_values = _task_type_values(task_types)
        if type_values:
            clauses.append(self.tasks.c.type.in_(type_values))
        status_values = _task_status_values(statuses)
        if status_values:
            clauses.append(self.tasks.c.status.in_(status_values))
        if clauses:
            query = query.where(and_(*clauses))
        with self.engine.begin() as conn:
            result = conn.execute(query)
        return int(result.rowcount or 0)

    def get(self, task_id: str) -> Task | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(self.tasks).where(self.tasks.c.id == task_id)
            ).mappings().fetchone()
        return self._from_row(row) if row else None

    def _set_terminal(
        self,
        task_id: str,
        status: TaskStatus,
        error: str | None = None,
        result: dict[str, Any] | None = None,
        owner_id: str | None = None,
    ) -> bool:
        conditions = [self.tasks.c.id == task_id]
        if owner_id is not None:
            conditions.extend(
                [
                    self.tasks.c.status == TaskStatus.RUNNING.value,
                    self.tasks.c.lease_owner == owner_id,
                ]
            )
        with self.engine.begin() as conn:
            update_result = conn.execute(
                update(self.tasks)
                .where(and_(*conditions))
                .values(
                    status=status.value,
                    updated_at=utcnow(),
                    available_at=utcnow(),
                    lease_owner=None,
                    lease_expires_at=None,
                    last_error=error,
                    result=json.dumps(result, ensure_ascii=False)
                    if result is not None
                    else None,
                )
            )
        return bool(update_result.rowcount)

    def _to_row(self, task: Task) -> dict[str, Any]:
        return {
            "id": task.id,
            "type": task.type.value,
            "payload": json.dumps(task.payload, ensure_ascii=False),
            "status": task.status.value,
            "account_id": task.account_id,
            "idempotency_key": task.idempotency_key,
            "attempts": task.attempts,
            "max_attempts": task.max_attempts,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "available_at": task.available_at,
            "lease_owner": task.lease_owner,
            "lease_expires_at": task.lease_expires_at,
            "last_error": task.last_error,
            "result": json.dumps(task.result, ensure_ascii=False)
            if task.result is not None
            else None,
        }

    def _from_row(self, row: RowMapping) -> Task:
        return Task(
            id=row["id"],
            type=TaskType(row["type"]),
            payload=json.loads(row["payload"]),
            status=TaskStatus(row["status"]),
            account_id=row["account_id"],
            idempotency_key=row["idempotency_key"]
            or task_idempotency_key(
                TaskType(row["type"]),
                json.loads(row["payload"]),
                row["account_id"],
            ),
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            available_at=row["available_at"],
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            last_error=row["last_error"],
            result=json.loads(row["result"]) if row["result"] else None,
        )


def _task_type_values(values: set[TaskType | str] | None) -> list[str]:
    if not values:
        return []
    return [
        value.value if isinstance(value, TaskType) else str(value)
        for value in values
    ]


def _task_status_values(values: set[TaskStatus | str] | None) -> list[str]:
    if not values:
        return []
    return [
        value.value if isinstance(value, TaskStatus) else str(value)
        for value in values
    ]
