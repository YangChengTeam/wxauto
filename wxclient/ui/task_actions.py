from __future__ import annotations

from dataclasses import dataclass

from wxclient.queue import Task, TaskStore, TaskType


@dataclass(frozen=True)
class AutomationSpec:
    name: str
    interval_seconds: int
    task_type: TaskType
    payload: dict


def enqueue_add_friend(
    store: TaskStore,
    account_id: str,
    number: str,
    greetings: str,
    remark: str | None,
    real: bool,
) -> Task:
    return store.create_task(
        TaskType.ADD_FRIEND,
        {
            "number": number,
            "greetings": greetings,
            "remark": remark,
            "dry_run": not real,
        },
        account_id,
    )


def enqueue_check_new_friends(
    store: TaskStore,
    account_id: str,
    verify: bool,
    limit: int,
    real: bool,
) -> Task:
    return store.create_task(
        TaskType.CHECK_NEW_FRIENDS,
        {
            "verify": verify,
            "limit": limit,
            "dry_run": not real,
        },
        account_id,
    )


def enqueue_accept_new_friends(
    store: TaskStore,
    account_id: str,
    limit: int,
    real: bool,
) -> Task:
    return store.create_task(
        TaskType.ACCEPT_NEW_FRIENDS,
        {
            "limit": limit,
            "dry_run": not real,
        },
        account_id,
    )


def enqueue_reply_unread(
    store: TaskStore,
    account_id: str,
    friend: str | None,
    reply_all: bool,
    real: bool,
) -> Task:
    return store.create_task(
        TaskType.REPLY_UNREAD,
        {
            "friend": friend or None,
            "reply_all": reply_all,
            "dry_run": not real,
        },
        account_id,
    )


def enqueue_check_unread_messages(
    store: TaskStore,
    account_id: str,
    real: bool,
) -> Task:
    return store.create_task(
        TaskType.CHECK_UNREAD_MESSAGES,
        {"dry_run": not real},
        account_id,
    )


def enqueue_watch_chats(
    store: TaskStore,
    account_id: str,
    friends: list[str],
    duration: str,
    auto_reply: bool,
    reply_all: bool,
    select_sender: bool,
    real: bool,
) -> Task:
    return store.create_task(
        TaskType.WATCH_CHATS,
        {
            "friends": friends,
            "duration": duration,
            "auto_reply": auto_reply,
            "reply_all": reply_all,
            "select_sender": select_sender,
            "dry_run": not real,
        },
        account_id,
    )
