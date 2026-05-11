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
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.pool import NullPool

from .models import Task, TaskStatus, TaskType, utcnow

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
            Column("attempts", Integer, nullable=False),
            Column("max_attempts", Integer, nullable=False),
            Column("created_at", String, nullable=False),
            Column("updated_at", String, nullable=False),
            Column("available_at", String, nullable=False),
            Column("last_error", Text),
            Column("result", Text),
            Index("idx_tasks_pick", "status", "available_at", "created_at"),
        )
        self.message_cursors = Table(
            "message_cursors",
            self.metadata,
            Column("account_id", String, primary_key=True),
            Column("chat_name", String, primary_key=True),
            Column("source", String, primary_key=True),
            Column("cursor", Text, nullable=False),
            Column("updated_at", String, nullable=False),
        )
        self._init_schema()

    def _init_schema(self) -> None:
        self.metadata.create_all(self.engine)

    def create_task(self, task_type: TaskType, payload: dict[str, Any], account_id: str) -> Task:
        with self.engine.begin() as conn:
            active_row = conn.execute(
                select(self.tasks)
                .where(
                    and_(
                        self.tasks.c.account_id == account_id,
                        self.tasks.c.type == task_type.value,
                        self.tasks.c.status == TaskStatus.PENDING.value,
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
            conn.execute(insert(self.tasks).values(self._to_row(task)))
        return task

    def next_pending(self) -> Task | None:
        now = utcnow()
        with self.engine.begin() as conn:
            row = conn.execute(
                select(self.tasks)
                .where(
                    and_(
                        self.tasks.c.status == TaskStatus.PENDING.value,
                        self.tasks.c.available_at <= now,
                    )
                )
                .order_by(self.tasks.c.created_at.asc())
                .limit(1)
            ).mappings().fetchone()
            if not row:
                return None
            task = self._from_row(row)
            conn.execute(
                update(self.tasks)
                .where(
                    and_(
                        self.tasks.c.id == task.id,
                        self.tasks.c.status == TaskStatus.PENDING.value,
                    )
                )
                .values(
                    status=TaskStatus.RUNNING.value,
                    attempts=self.tasks.c.attempts + 1,
                    updated_at=now,
                )
            )
        return self.get(task.id)

    def complete(self, task_id: str, result: dict[str, Any] | None = None) -> None:
        self._set_terminal(task_id, TaskStatus.SUCCEEDED, result=result)

    def fail(self, task_id: str, error: str, retry: bool = False) -> None:
        task = self.get(task_id)
        if retry and task and task.attempts < task.max_attempts:
            status = TaskStatus.PENDING
        else:
            status = TaskStatus.FAILED
        self._set_terminal(task_id, status, error=error)

    def block(self, task_id: str, error: str) -> None:
        self._set_terminal(task_id, TaskStatus.BLOCKED, error=error)

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

    def get_message_cursor(
        self,
        account_id: str,
        chat_name: str,
        source: str = "visible",
    ) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(self.message_cursors.c.cursor).where(
                    and_(
                        self.message_cursors.c.account_id == account_id,
                        self.message_cursors.c.chat_name == chat_name,
                        self.message_cursors.c.source == source,
                    )
                )
            ).mappings().fetchone()
        if not row:
            return None
        return json.loads(row["cursor"])

    def set_message_cursor(
        self,
        account_id: str,
        chat_name: str,
        cursor: dict[str, Any],
        source: str = "visible",
    ) -> None:
        now = utcnow()
        payload = json.dumps(cursor, ensure_ascii=False)
        with self.engine.begin() as conn:
            existing = conn.execute(
                select(self.message_cursors.c.account_id).where(
                    and_(
                        self.message_cursors.c.account_id == account_id,
                        self.message_cursors.c.chat_name == chat_name,
                        self.message_cursors.c.source == source,
                    )
                )
            ).fetchone()
            if existing:
                conn.execute(
                    update(self.message_cursors)
                    .where(
                        and_(
                            self.message_cursors.c.account_id == account_id,
                            self.message_cursors.c.chat_name == chat_name,
                            self.message_cursors.c.source == source,
                        )
                    )
                    .values(cursor=payload, updated_at=now)
                )
                return
            conn.execute(
                insert(self.message_cursors).values(
                    account_id=account_id,
                    chat_name=chat_name,
                    source=source,
                    cursor=payload,
                    updated_at=now,
                )
            )

    def list_message_cursor_chats(
        self,
        account_id: str,
        source: str = "visible",
        limit: int = 20,
    ) -> list[str]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(self.message_cursors.c.chat_name)
                .where(
                    and_(
                        self.message_cursors.c.account_id == account_id,
                        self.message_cursors.c.source == source,
                    )
                )
                .order_by(self.message_cursors.c.updated_at.desc())
                .limit(limit)
            ).mappings().fetchall()
        return [str(row["chat_name"]) for row in rows]

    def _set_terminal(
        self,
        task_id: str,
        status: TaskStatus,
        error: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(self.tasks)
                .where(self.tasks.c.id == task_id)
                .values(
                    status=status.value,
                    updated_at=utcnow(),
                    last_error=error,
                    result=json.dumps(result, ensure_ascii=False)
                    if result is not None
                    else None,
                )
            )

    def _to_row(self, task: Task) -> dict[str, Any]:
        return {
            "id": task.id,
            "type": task.type.value,
            "payload": json.dumps(task.payload, ensure_ascii=False),
            "status": task.status.value,
            "account_id": task.account_id,
            "attempts": task.attempts,
            "max_attempts": task.max_attempts,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "available_at": task.available_at,
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
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            available_at=row["available_at"],
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
