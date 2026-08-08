from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from wxclient.queue import OperationWorker, Task, TaskStatus, TaskStore, TaskType


class _FakeApi:
    def __init__(self) -> None:
        self.scanned: list[str] = []

    def scan_messages(
        self,
        friend: str,
        duration: str,
        select_sender: bool,
        save_file: bool,
        save_media: bool,
        dry_run: bool | None,
    ) -> dict[str, Any]:
        self.scanned.append(friend)
        return {
            "action": "scan_messages",
            "friend": friend,
            "duration": duration,
            "dry_run": bool(dry_run),
            "messages": [{"content": f"message from {friend}"}],
            "message_count": 1,
        }


class TaskStoreTests(unittest.TestCase):
    def test_pending_tasks_of_same_type_are_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            first = store.create_task(
                TaskType.SEND_MESSAGE,
                {"friend": "a", "text": "one"},
                "default",
            )
            second = store.create_task(
                TaskType.SEND_MESSAGE,
                {"friend": "b", "text": "two"},
                "default",
            )
        self.assertEqual(first.id, second.id)

    def test_worker_completes_a_serialized_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            api = _FakeApi()
            worker = OperationWorker(store, api)  # type: ignore[arg-type]
            task = store.create_task(
                TaskType.SCAN_MESSAGES,
                {"friend": "chat-a", "duration": "1s", "dry_run": True},
                "default",
            )
            self.assertTrue(worker.run_once())
            completed = store.get(task.id)
        self.assertIsNotNone(completed)
        self.assertEqual(completed.status, TaskStatus.SUCCEEDED)  # type: ignore[union-attr]
        self.assertEqual(completed.result["message_count"], 1)  # type: ignore[union-attr,index]


class WorkerDispatchTests(unittest.TestCase):
    def test_watch_chats_is_pure_monitoring(self) -> None:
        api = _FakeApi()
        with tempfile.TemporaryDirectory() as tmp:
            worker = OperationWorker(
                TaskStore(Path(tmp) / "tasks.db"),
                api,  # type: ignore[arg-type]
            )
            task = Task.new(
                TaskType.WATCH_CHATS,
                {
                    "friends": ["chat-a", "chat-b", "chat-a"],
                    "duration": "1s",
                    "dry_run": True,
                },
            )
            result = worker.dispatch(task)

        self.assertEqual(api.scanned, ["chat-a", "chat-b"])
        self.assertEqual(result["chat_count"], 2)
        self.assertEqual(result["message_count"], 2)
        self.assertNotIn("reply_count", result)


if __name__ == "__main__":
    unittest.main()
