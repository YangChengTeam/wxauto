from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import structlog

from wxclient.wechat_api import WeChatApi, WeChatApiError, WeChatApiUnavailableError

from .models import Task, TaskType, is_real_write
from .store import TaskStore

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class TaskContinuation:
    payload: dict[str, Any]
    delay_seconds: float = 0.1


class OperationWorker:
    """The single consumer allowed to operate the WeChat UI."""

    def __init__(
        self,
        store: TaskStore,
        api: WeChatApi,
        poll_interval: float = 1.0,
        lease_seconds: float = 30.0,
        worker_id: str | None = None,
        watch_slice_seconds: float = 5.0,
        retry_base_seconds: float = 1.0,
        retry_max_seconds: float = 30.0,
    ) -> None:
        self.store = store
        self.api = api
        self.poll_interval = poll_interval
        self.lease_seconds = max(1.0, lease_seconds)
        self.worker_id = worker_id or str(uuid4())
        self.watch_slice_seconds = max(0.1, watch_slice_seconds)
        self.retry_base_seconds = max(0.0, retry_base_seconds)
        self.retry_max_seconds = max(self.retry_base_seconds, retry_max_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._busy = False
        self._lifecycle_lock = threading.Lock()
        self._execution_lock = threading.Lock()

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        with self._lifecycle_lock:
            if self.running:
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self.run_forever,
                name="wx-worker",
                daemon=True,
            )
            self._thread.start()

    def stop(self, wait: bool = True, timeout: float | None = 5.0) -> None:
        with self._lifecycle_lock:
            self._stop.set()
            if wait and self._thread:
                self._thread.join(timeout=timeout)
            if not self.running:
                self.store.release_worker(self.worker_id)

    def run_forever(self) -> None:
        logger.info("worker.started", poll_interval=self.poll_interval)
        try:
            while not self._stop.is_set():
                if not self.run_once():
                    self._stop.wait(self.poll_interval)
        finally:
            self.store.release_worker(self.worker_id)
            logger.info("worker.stopped")

    def run_once(self) -> bool:
        if self._stop.is_set():
            return False
        if not self._execution_lock.acquire(blocking=False):
            return False
        try:
            return self._run_once_locked()
        finally:
            self._execution_lock.release()

    def _run_once_locked(self) -> bool:
        if self._stop.is_set():
            return False
        if not self.store.acquire_worker(self.worker_id, self.lease_seconds):
            return False
        task = self.store.claim_next(self.worker_id, self.lease_seconds)
        if task is None:
            return False
        self._busy = True
        task_logger = logger.bind(task_id=task.id, task_type=task.type.value)
        task_logger.info("task.running")
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._renew_leases,
            args=(task.id, heartbeat_stop),
            name="wx-lease-heartbeat",
            daemon=True,
        )
        heartbeat.start()
        try:
            result = self.dispatch(task)
        except WeChatApiUnavailableError as exc:
            self.store.block(task.id, str(exc), owner_id=self.worker_id)
            task_logger.warning("task.blocked", error=str(exc))
        except WeChatApiError as exc:
            self._record_execution_failure(task, exc)
            task_logger.warning("task.failed", error=str(exc))
        except Exception as exc:
            self._record_execution_failure(task, exc)
            task_logger.exception("task.failed", error=str(exc))
        else:
            if isinstance(result, TaskContinuation):
                self.store.yield_task(
                    task.id,
                    self.worker_id,
                    result.payload,
                    result.delay_seconds,
                )
                task_logger.info("task.yielded")
            else:
                self.store.complete(task.id, result, owner_id=self.worker_id)
                task_logger.info("task.succeeded", action=result.get("action"))
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=1.0)
            self._busy = False
        return True

    def _record_execution_failure(self, task: Task, exc: Exception) -> None:
        if is_real_write(task):
            self.store.fail(
                task.id,
                f"真实写操作执行结果未知，已禁止自动重试：{exc}",
                retry=False,
                owner_id=self.worker_id,
                result={
                    "outcome": "unknown",
                    "retry_suppressed": True,
                    "error": str(exc),
                },
            )
            return
        self.store.fail(
            task.id,
            str(exc),
            retry=True,
            owner_id=self.worker_id,
            retry_delay_seconds=self._retry_delay(task.attempts),
        )

    def _retry_delay(self, attempts: int) -> float:
        exponent = max(0, attempts - 1)
        return min(self.retry_base_seconds * (2**exponent), self.retry_max_seconds)

    def _renew_leases(self, task_id: str, stop: threading.Event) -> None:
        interval = max(0.25, self.lease_seconds / 3)
        while not stop.wait(interval):
            worker_ok = self.store.renew_worker(self.worker_id, self.lease_seconds)
            task_ok = self.store.renew_task(task_id, self.worker_id, self.lease_seconds)
            if not worker_ok or not task_ok:
                logger.error(
                    "worker.lease_lost",
                    worker_id=self.worker_id,
                    task_id=task_id,
                    worker_lease=worker_ok,
                    task_lease=task_ok,
                )
                return

    def dispatch(self, task: Task) -> dict[str, Any] | TaskContinuation:
        payload = task.payload
        if task.type == TaskType.SEARCH_CONTACTS:
            return self.api.search_contacts(
                query=str(payload["query"]),
                limit=int(payload.get("limit", 10)),
            )
        if task.type == TaskType.GET_CONTACT_PROFILE:
            return self.api.get_contact_profile(friend=str(payload["friend"]))
        if task.type == TaskType.LIST_GROUP_MEMBERS:
            return self.api.list_group_members(group=str(payload["group"]))
        if task.type == TaskType.ADD_FRIEND:
            return self.api.add_friend(
                number=str(payload["number"]),
                # 「没给」兜底「你好」，「明确给了空串」就保持空——空的时候
                # 驱动层不碰验证语输入框，微信用它自己预填的那句（092）。
                greetings=(payload["greetings"]
                           if isinstance(payload.get("greetings"), str) else "你好"),
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
        if task.type == TaskType.SEND_FILES:
            return self.api.send_files(
                friend=str(payload["friend"]),
                files=[str(path) for path in payload.get("files") or []],
                text=str(payload.get("text") or ""),
                dry_run=payload.get("dry_run"),
            )
        if task.type == TaskType.SCAN_MESSAGES:
            return self._scan_messages(payload)
        if task.type == TaskType.GET_UNREAD_MESSAGES:
            return self.api.get_unread_messages()
        if task.type == TaskType.WATCH_CHATS:
            return self._watch_chats(payload)
        if task.type == TaskType.READ_CHAT_SESSIONS:
            return self.api.read_chat_sessions(dry_run=payload.get("dry_run"))
        if task.type == TaskType.READ_CHAT_HISTORY:
            return self.api.read_chat_history(
                friend=str(payload["friend"]),
                limit=int(payload.get("limit", 50)),
            )
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
        if task.type == TaskType.LIKE_MOMENTS:
            return self.api.like_recent_moments(
                limit=int(payload.get("limit", 20)),
                dry_run=payload.get("dry_run"),
            )
        if task.type == TaskType.COMMENT_MOMENTS:
            return self.api.comment_recent_moments(
                comments=list(payload.get("comments") or []),
                dry_run=payload.get("dry_run"),
            )
        raise ValueError(f"不支持的任务类型：{task.type}")

    def _scan_messages(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.api.scan_messages(
            friend=str(payload["friend"]),
            duration=str(payload.get("duration") or "30s"),
            select_sender=bool(payload.get("select_sender", False)),
            save_file=bool(payload.get("save_file", False)),
            save_media=bool(payload.get("save_media", False)),
            dry_run=payload.get("dry_run"),
        )

    def _watch_chats(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any] | TaskContinuation:
        friends = _payload_friends(payload)
        if not friends:
            raise ValueError("friends 至少需要包含一个会话名称")
        duration_seconds = _duration_seconds(str(payload.get("duration") or "30s"))
        raw_state = payload.get("_wxclient_watch_state")
        state = dict(raw_state) if isinstance(raw_state, dict) else {}
        index = int(state.get("index", 0))
        remaining = float(state.get("remaining", duration_seconds))
        results = list(state.get("results") or [])
        error_count = int(state.get("error_count", 0))
        current = dict(state.get("current") or {})

        friend = friends[index]
        slice_seconds = min(remaining, self.watch_slice_seconds)
        scan_payload = dict(
            payload,
            friend=friend,
            duration=_format_duration(slice_seconds),
        )
        scan_payload.pop("_wxclient_watch_state", None)
        try:
            slice_result = self._scan_messages(scan_payload)
        except Exception as exc:
            error_count += 1
            results.append({"friend": friend, "error": str(exc)})
            index += 1
            remaining = duration_seconds
            current = {}
        else:
            current.update(
                {
                    key: value
                    for key, value in slice_result.items()
                    if key not in {"messages", "message_count", "duration"}
                }
            )
            current["friend"] = friend
            current["duration"] = str(payload.get("duration") or "30s")
            current_messages = list(current.get("messages") or [])
            slice_messages = slice_result.get("messages")
            if isinstance(slice_messages, list):
                current_messages.extend(slice_messages)
            current["messages"] = current_messages
            count = slice_result.get("message_count")
            if count is None and isinstance(slice_messages, list):
                count = len(slice_messages)
            current["message_count"] = int(current.get("message_count", 0)) + int(
                count or 0
            )
            remaining -= slice_seconds
            if remaining <= 0.000001:
                results.append(current)
                index += 1
                remaining = duration_seconds
                current = {}

        if index < len(friends):
            continuation_payload = dict(payload)
            continuation_payload["_wxclient_watch_state"] = {
                "index": index,
                "remaining": remaining,
                "results": results,
                "error_count": error_count,
                "current": current,
            }
            return TaskContinuation(continuation_payload)

        message_count = sum(int(item.get("message_count") or 0) for item in results)
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


def _duration_seconds(value: str) -> float:
    text = value.strip().lower()
    if text.endswith("min"):
        seconds = float(text[:-3]) * 60
    elif text.endswith("ms"):
        seconds = float(text[:-2]) / 1000
    elif text.endswith("s"):
        seconds = float(text[:-1])
    elif text.endswith("h"):
        seconds = float(text[:-1]) * 3600
    else:
        seconds = float(text)
    if seconds <= 0:
        raise ValueError("duration 必须大于 0")
    return seconds


def _format_duration(seconds: float) -> str:
    return f"{seconds:g}s"
