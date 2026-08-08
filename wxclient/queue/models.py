from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
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
    ADD_FRIEND = "add_friend"
    CHECK_NEW_FRIENDS = "check_new_friends"
    ACCEPT_NEW_FRIENDS = "accept_new_friends"
    SEND_MESSAGE = "send_message"
    SCAN_MESSAGES = "scan_messages"
    READ_CHAT_SESSIONS = "read_chat_sessions"
    READ_VISIBLE_MESSAGES = "read_visible_messages"
    WATCH_CHATS = "watch_chats"
    POST_MOMENTS = "post_moments"
    READ_RECENT_MOMENTS = "read_recent_moments"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Task:
    id: str
    type: TaskType
    payload: dict[str, Any]
    status: TaskStatus
    account_id: str
    attempts: int
    max_attempts: int
    created_at: str
    updated_at: str
    available_at: str
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
            attempts=0,
            max_attempts=max_attempts,
            created_at=now,
            updated_at=now,
            available_at=now,
        )
