from __future__ import annotations

import threading
from typing import Any

import structlog

from wxclient.wechat_api import WeChatApi, WeChatApiError

from .models import Task, TaskType
from .store import TaskStore

logger = structlog.get_logger(__name__)


class OperationWorker:
    """The single consumer allowed to operate the WeChat UI."""

    def __init__(
        self,
        store: TaskStore,
        api: WeChatApi,
        poll_interval: float = 1.0,
    ) -> None:
        self.store = store
        self.api = api
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_forever, name="wx-mcp-worker", daemon=True)
        self._thread.start()

    def stop(self, wait: bool = True, timeout: float | None = 5.0) -> None:
        self._stop.set()
        if wait and self._thread:
            self._thread.join(timeout=timeout)

    def run_forever(self) -> None:
        logger.info("worker.started", poll_interval=self.poll_interval)
        while not self._stop.is_set():
            if not self.run_once():
                self._stop.wait(self.poll_interval)
        logger.info("worker.stopped")

    def run_once(self) -> bool:
        if self._stop.is_set():
            return False
        task = self.store.next_pending()
        if task is None:
            return False
        self._busy = True
        task_logger = logger.bind(task_id=task.id, task_type=task.type.value)
        task_logger.info("task.running")
        try:
            result = self.dispatch(task)
        except WeChatApiError as exc:
            self.store.block(task.id, str(exc))
            task_logger.warning("task.blocked", error=str(exc))
        except Exception as exc:
            self.store.fail(task.id, str(exc), retry=True)
            task_logger.exception("task.failed", error=str(exc))
        else:
            self.store.complete(task.id, result)
            task_logger.info("task.succeeded", action=result.get("action"))
        finally:
            self._busy = False
        return True

    def dispatch(self, task: Task) -> dict[str, Any]:
        payload = task.payload
        if task.type == TaskType.ADD_FRIEND:
            return self.api.add_friend(
                number=str(payload["number"]),
                greetings=str(payload.get("greetings") or "你好"),
                remark=payload.get("remark"),
                dry_run=payload.get("dry_run"),
            )
        if task.type in {TaskType.CHECK_NEW_FRIENDS, TaskType.ACCEPT_NEW_FRIENDS}:
            return self.api.check_new_friends(
                verify=task.type == TaskType.ACCEPT_NEW_FRIENDS,
                limit=int(payload.get("limit", 8)),
                dry_run=payload.get("dry_run"),
            )
        if task.type == TaskType.SEND_MESSAGE:
            return self.api.send_message(
                friend=str(payload["friend"]),
                text=str(payload["text"]),
                dry_run=payload.get("dry_run"),
            )
        if task.type == TaskType.SCAN_MESSAGES:
            return self._scan_messages(payload)
        if task.type == TaskType.WATCH_CHATS:
            return self._watch_chats(payload)
        if task.type == TaskType.READ_CHAT_SESSIONS:
            return self.api.read_chat_sessions(dry_run=payload.get("dry_run"))
        if task.type == TaskType.READ_VISIBLE_MESSAGES:
            return self.api.read_visible_messages(
                friend=str(payload["friend"]),
                dry_run=payload.get("dry_run"),
            )
        if task.type == TaskType.POST_MOMENTS:
            return self.api.post_moments(
                text=str(payload.get("text") or ""),
                medias=payload.get("medias") or [],
                dry_run=payload.get("dry_run"),
            )
        if task.type == TaskType.READ_RECENT_MOMENTS:
            return self.api.read_recent_moments(
                limit=int(payload.get("limit", 5)),
                dry_run=payload.get("dry_run"),
            )
        raise ValueError(f"Unsupported task type: {task.type}")

    def _scan_messages(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.api.scan_messages(
            friend=str(payload["friend"]),
            duration=str(payload.get("duration") or "30s"),
            select_sender=bool(payload.get("select_sender", False)),
            save_file=bool(payload.get("save_file", False)),
            save_media=bool(payload.get("save_media", False)),
            dry_run=payload.get("dry_run"),
        )

    def _watch_chats(self, payload: dict[str, Any]) -> dict[str, Any]:
        friends = _payload_friends(payload)
        if not friends:
            raise ValueError("friends must contain at least one chat name")
        results: list[dict[str, Any]] = []
        error_count = 0
        message_count = 0
        for friend in friends:
            scan_payload = dict(payload, friend=friend)
            try:
                result = self._scan_messages(scan_payload)
            except Exception as exc:
                error_count += 1
                results.append({"friend": friend, "error": str(exc)})
                continue
            result.setdefault("friend", friend)
            messages = result.get("messages")
            count = result.get("message_count")
            if count is None and isinstance(messages, list):
                count = len(messages)
            message_count += int(count or 0)
            results.append(result)
        return {
            "action": "watch_chats",
            "dry_run": bool(payload.get("dry_run", True)),
            "chat_count": len(friends),
            "message_count": message_count,
            "error_count": error_count,
            "results": results,
        }


def _payload_friends(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("friends")
    values = raw if isinstance(raw, list) else [payload.get("friend")]
    friends: list[str] = []
    seen: set[str] = set()
    for value in values:
        friend = str(value or "").strip()
        if friend and friend not in seen:
            seen.add(friend)
            friends.append(friend)
    return friends
