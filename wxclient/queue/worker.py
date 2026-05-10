from __future__ import annotations

import threading
import time
from typing import Any

import structlog

from wxclient.agentbridge.reply_flow import (
    reply_to_chat_messages,
    reply_to_unread_messages,
    watch_chats_messages,
)
from wxclient.agents import ChatAgent, build_chat_agent
from wxclient.config import Settings
from wxclient.wechat_api import WeChatApi, WeChatApiError

from .models import Task, TaskType
from .store import TaskStore

logger = structlog.get_logger(__name__)


class OperationWorker:
    def __init__(
        self,
        store: TaskStore,
        api: WeChatApi,
        settings: Settings | None = None,
        agent: ChatAgent | None = None,
        poll_interval: float = 1.0,
    ) -> None:
        self.store = store
        self.api = api
        self.settings = settings or Settings()
        self._agent = agent
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._busy = False
        self._visible_watch_chats: list[str] = []

    @property
    def busy(self) -> bool:
        return self._busy

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_forever, name="wx-operation-worker")
        self._thread.daemon = True
        self._thread.start()

    def stop(self, wait: bool = True, timeout: float = 5.0) -> None:
        self._stop.set()
        if wait and self._thread:
            self._thread.join(timeout=timeout)

    def run_forever(self) -> None:
        logger.info("worker.started", poll_interval=self.poll_interval)
        while not self._stop.is_set():
            did_work = self.run_once()
            if not did_work:
                time.sleep(self.poll_interval)
        logger.info("worker.stopped")

    def run_once(self) -> bool:
        task = self.store.next_pending()
        if task is None:
            return False
        self._busy = True
        task_logger = logger.bind(
            task_id=task.id,
            task_type=task.type.value,
            account_id=task.account_id,
            attempts=task.attempts,
        )
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
            if task.type == TaskType.REPLY_UNREAD:
                self._remember_visible_watch_chats(result)
            self.store.complete(task.id, result)
            task_logger.info("task.succeeded", result=self._summarize_result(task.type, result))
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
        if task.type == TaskType.CHECK_NEW_FRIENDS:
            return self.api.check_new_friends(
                verify=bool(payload.get("verify", False)),
                limit=int(payload.get("limit", 8)),
                dry_run=payload.get("dry_run"),
            )
        if task.type == TaskType.ACCEPT_NEW_FRIENDS:
            return self.api.check_new_friends(
                verify=True,
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
            return self.api.scan_messages(
                friend=str(payload["friend"]),
                duration=str(payload.get("duration") or "30s"),
                select_sender=bool(payload.get("select_sender", False)),
                save_file=bool(payload.get("save_file", False)),
                save_media=bool(payload.get("save_media", False)),
                dry_run=payload.get("dry_run"),
            )
        if task.type == TaskType.READ_VISIBLE_MESSAGES:
            return self.api.read_visible_messages(
                friend=str(payload["friend"]),
                dry_run=payload.get("dry_run"),
            )
        if task.type == TaskType.CHECK_UNREAD_MESSAGES:
            return self.api.check_unread_messages(dry_run=payload.get("dry_run"))
        if task.type == TaskType.REPLY_CHAT:
            return reply_to_chat_messages(
                api=self.api,
                agent=self._chat_agent(),
                friend=str(payload["friend"]),
                duration=str(payload.get("duration") or "30s"),
                real=not bool(payload.get("dry_run", True)),
                reply_all=bool(payload.get("reply_all", False)),
                select_sender=bool(payload.get("select_sender", False)),
                source=str(payload.get("source") or "new"),
                context_message_limit=self.settings.agent_context_message_count,
            )
        if task.type == TaskType.REPLY_UNREAD:
            friend = payload.get("friend")
            return reply_to_unread_messages(
                api=self.api,
                agent=self._chat_agent(),
                real=not bool(payload.get("dry_run", True)),
                reply_all=bool(payload.get("reply_all", False)),
                friend=str(friend) if friend else None,
                cursor_store=self.store,
                account_id=task.account_id,
                visible_chats=[],
                context_message_limit=self.settings.agent_context_message_count,
            )
        if task.type == TaskType.WATCH_CHATS:
            return watch_chats_messages(
                api=self.api,
                agent=self._chat_agent(),
                friends=_payload_friends(payload),
                duration=str(payload.get("duration") or "30s"),
                real=not bool(payload.get("dry_run", True)),
                auto_reply=bool(payload.get("auto_reply", True)),
                reply_all=bool(payload.get("reply_all", False)),
                select_sender=bool(payload.get("select_sender", False)),
                context_message_limit=self.settings.agent_context_message_count,
            )
        raise ValueError(f"Unsupported task type: {task.type}")

    def _chat_agent(self) -> ChatAgent:
        if self._agent is None:
            self._agent = build_chat_agent(self.settings)
        return self._agent

    def _visible_watch_targets(self, account_id: str, friend: str | None) -> list[str]:
        targets = list(self._visible_watch_chats)
        try:
            targets.extend(self.store.list_message_cursor_chats(account_id))
        except Exception:
            pass
        if friend:
            targets.insert(0, friend)
        return _dedupe(targets)

    def _remember_visible_watch_chats(self, result: dict[str, Any]) -> None:
        chats = result.get("processed_chats")
        if not isinstance(chats, list):
            return
        combined = [str(chat) for chat in chats if str(chat or "").strip()]
        combined.extend(self._visible_watch_chats)
        self._visible_watch_chats = _dedupe(combined)[:20]

    @staticmethod
    def _summarize_result(task_type: TaskType, result: dict[str, Any]) -> dict[str, Any]:
        summary: dict[str, Any] = {"action": result.get("action")}
        for key in (
            "dry_run",
            "reply_count",
            "chat_count",
            "message_count",
            "new_message_count",
            "error_count",
        ):
            if key in result:
                summary[key] = result.get(key)
        if task_type == TaskType.REPLY_CHAT:
            summary["message_source"] = result.get("message_source")
        if task_type == TaskType.REPLY_UNREAD:
            summary["friend_filter"] = result.get("friend_filter")
            summary["visible_chat_count"] = result.get("visible_chat_count")
            summary["current_chat"] = result.get("current_chat")
            summary["current_chat_replied"] = result.get("current_chat_replied")
            summary["current_chat_latest_is_today"] = result.get("current_chat_latest_is_today")
        return summary


def _dedupe(values: list[str]) -> list[str]:
    rows: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        rows.append(text)
    return rows


def _payload_friends(payload: dict[str, Any]) -> list[str]:
    value = payload.get("friends")
    if isinstance(value, list):
        return [str(item) for item in value]
    single = payload.get("friend")
    if single:
        return [str(single)]
    return []
