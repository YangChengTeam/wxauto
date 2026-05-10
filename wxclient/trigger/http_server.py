from __future__ import annotations

from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel

from wxclient.config import Settings
from wxclient.queue import Task, TaskStore, TaskType


def run_http_server(settings: Settings, store: TaskStore) -> None:
    uvicorn.run(create_app(settings, store), host=settings.host, port=settings.port)


def create_app(settings: Settings, store: TaskStore) -> FastAPI:
    app = FastAPI(title="wxclient", version="0.1.0")

    def require_token(authorization: str | None = Header(default=None)) -> None:
        if settings.webhook_token and authorization != f"Bearer {settings.webhook_token}":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="unauthorized",
            )

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/tasks")
    def list_tasks() -> dict[str, list[dict[str, Any]]]:
        return {"tasks": [_task_to_dict(task) for task in store.list_tasks()]}

    @app.post("/tasks/add-friend", status_code=status.HTTP_202_ACCEPTED)
    def add_friend(
        payload: AddFriendRequest,
        _: None = Depends(require_token),
    ) -> dict[str, str]:
        task = store.create_task(
            TaskType.ADD_FRIEND,
            {
                "number": payload.number,
                "greetings": payload.greetings or "你好",
                "remark": payload.remark,
                "dry_run": _default_dry_run(payload.dry_run, settings),
            },
            payload.account_id or settings.default_account_id,
        )
        return {"task_id": task.id, "status": task.status.value}

    @app.post("/tasks/send-message", status_code=status.HTTP_202_ACCEPTED)
    def send_message(
        payload: SendMessageRequest,
        _: None = Depends(require_token),
    ) -> dict[str, str]:
        task = store.create_task(
            TaskType.SEND_MESSAGE,
            {
                "friend": payload.friend,
                "text": payload.text,
                "dry_run": _default_dry_run(payload.dry_run, settings),
            },
            payload.account_id or settings.default_account_id,
        )
        return {"task_id": task.id, "status": task.status.value}

    @app.post("/tasks/check-new-friends", status_code=status.HTTP_202_ACCEPTED)
    def check_new_friends(
        payload: CheckNewFriendsRequest,
        _: None = Depends(require_token),
    ) -> dict[str, str]:
        task = store.create_task(
            TaskType.CHECK_NEW_FRIENDS,
            {
                "verify": payload.verify,
                "limit": payload.limit,
                "dry_run": _default_dry_run(payload.dry_run, settings),
            },
            payload.account_id or settings.default_account_id,
        )
        return {"task_id": task.id, "status": task.status.value}

    @app.post("/tasks/accept-new-friends", status_code=status.HTTP_202_ACCEPTED)
    def accept_new_friends(
        payload: AcceptNewFriendsRequest,
        _: None = Depends(require_token),
    ) -> dict[str, str]:
        task = store.create_task(
            TaskType.ACCEPT_NEW_FRIENDS,
            {
                "limit": payload.limit,
                "dry_run": _default_dry_run(payload.dry_run, settings),
            },
            payload.account_id or settings.default_account_id,
        )
        return {"task_id": task.id, "status": task.status.value}

    @app.post("/tasks/listen-chat", status_code=status.HTTP_202_ACCEPTED)
    def listen_chat(
        payload: ListenChatRequest,
        _: None = Depends(require_token),
    ) -> dict[str, str]:
        task = store.create_task(
            TaskType.SCAN_MESSAGES,
            {
                "friend": payload.friend,
                "duration": payload.duration,
                "select_sender": payload.select_sender,
                "save_file": payload.save_file,
                "save_media": payload.save_media,
                "dry_run": _default_dry_run(payload.dry_run, settings),
            },
            payload.account_id or settings.default_account_id,
        )
        return {"task_id": task.id, "status": task.status.value}

    @app.post("/tasks/listen-chats", status_code=status.HTTP_202_ACCEPTED)
    def listen_chats(
        payload: ListenChatsRequest,
        _: None = Depends(require_token),
    ) -> dict[str, str]:
        friends = _clean_friends(payload.friends)
        task = store.create_task(
            TaskType.WATCH_CHATS,
            {
                "friends": friends,
                "duration": payload.duration,
                "auto_reply": payload.auto_reply,
                "reply_all": payload.reply_all,
                "select_sender": False,
                "dry_run": _default_dry_run(payload.dry_run, settings),
            },
            payload.account_id or settings.default_account_id,
        )
        return {"task_id": task.id, "status": task.status.value}

    return app


class BaseTaskRequest(BaseModel):
    account_id: str | None = None
    dry_run: bool | None = None


class AddFriendRequest(BaseTaskRequest):
    number: str
    greetings: str | None = None
    remark: str | None = None


class SendMessageRequest(BaseTaskRequest):
    friend: str
    text: str


class CheckNewFriendsRequest(BaseTaskRequest):
    verify: bool = False
    limit: int = 8


class AcceptNewFriendsRequest(BaseTaskRequest):
    limit: int = 8


class ListenChatRequest(BaseTaskRequest):
    friend: str
    duration: str = "30s"
    select_sender: bool = False
    save_file: bool = False
    save_media: bool = False


class ListenChatsRequest(BaseTaskRequest):
    friends: list[str]
    duration: str = "30s"
    auto_reply: bool = True
    reply_all: bool = False


def _default_dry_run(value: bool | None, settings: Settings) -> bool:
    return settings.dry_run if value is None else value


def _clean_friends(values: list[str]) -> list[str]:
    rows: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        rows.append(text)
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="friends must contain at least one chat name",
        )
    return rows


def _task_to_dict(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "type": task.type.value,
        "status": task.status.value,
        "account_id": task.account_id,
        "attempts": task.attempts,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "last_error": task.last_error,
        "result": task.result,
    }
