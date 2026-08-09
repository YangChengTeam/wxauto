from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class TaskType(str, Enum):
    SEARCH_CONTACTS = "search_contacts"
    GET_CONTACT_PROFILE = "get_contact_profile"
    LIST_GROUP_MEMBERS = "list_group_members"
    ADD_FRIEND = "add_friend"
    CHECK_NEW_FRIENDS = "check_new_friends"
    ACCEPT_NEW_FRIENDS = "accept_new_friends"
    SEND_MESSAGE = "send_message"
    SEND_FILES = "send_files"
    SCAN_MESSAGES = "scan_messages"
    GET_UNREAD_MESSAGES = "get_unread_messages"
    READ_CHAT_SESSIONS = "read_chat_sessions"
    READ_CHAT_HISTORY = "read_chat_history"
    READ_VISIBLE_MESSAGES = "read_visible_messages"
    WATCH_CHATS = "watch_chats"
    POST_MOMENTS = "post_moments"
    READ_RECENT_MOMENTS = "read_recent_moments"


REAL_WRITE_TASK_TYPES = frozenset(
    {
        TaskType.ADD_FRIEND,
        TaskType.ACCEPT_NEW_FRIENDS,
        TaskType.SEND_MESSAGE,
        TaskType.SEND_FILES,
        TaskType.POST_MOMENTS,
    }
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Task:
    id: str
    type: TaskType
    payload: dict[str, Any]
    status: TaskStatus
    account_id: str
    idempotency_key: str
    attempts: int
    max_attempts: int
    created_at: str
    updated_at: str
    available_at: str
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    last_error: str | None = None
    result: dict[str, Any] | None = None

    @classmethod
    def new(
        cls,
        task_type: TaskType,
        payload: dict[str, Any],
        account_id: str = "default",
        max_attempts: int = 3,
    ) -> "Task":
        now = utcnow()
        return cls(
            id=str(uuid4()),
            type=task_type,
            payload=payload,
            status=TaskStatus.PENDING,
            account_id=account_id,
            idempotency_key=task_idempotency_key(task_type, payload, account_id),
            attempts=0,
            max_attempts=max_attempts,
            created_at=now,
            updated_at=now,
            available_at=now,
            lease_owner=None,
            lease_expires_at=None,
        )


def task_idempotency_key(
    task_type: TaskType,
    payload: dict[str, Any],
    account_id: str,
) -> str:
    """Return a stable identity for one logical request.

    Queue-owned continuation fields are intentionally ignored so yielding a
    long-running task does not change its dedupe identity.
    """
    public_payload = {
        key: value
        for key, value in payload.items()
        if not str(key).startswith("_wxclient_")
    }
    canonical_payload = json.dumps(
        public_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    identity = f"{account_id}\0{task_type.value}\0{canonical_payload}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def lease_deadline(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(0.0, seconds))).isoformat()


def is_real_write(task: Task) -> bool:
    return task.type in REAL_WRITE_TASK_TYPES and not bool(
        task.payload.get("dry_run", False)
    )
